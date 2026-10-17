"""Minimal WebSocket client (RFC 6455) for reading a server's stream: Jetstream's live tail.

Client side only, no extensions except permessage-deflate (RFC 7692), which Jetstream offers and
which cuts the download several times. Handles fragmented messages, pings and close frames; frames
we send are masked as the RFC requires.
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
import struct
import zlib
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 16 * 1024 * 1024


def _ssl_context() -> ssl.SSLContext:
    try:                                   # httpx's CA bundle; python.org builds on macOS ship none
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class WebSocketError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status, self.body = status, body


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, deflate: bool) -> None:
        self.reader, self.writer = reader, writer
        self.inflater = zlib.decompressobj(-zlib.MAX_WBITS) if deflate else None
        self.bytes_received = 0
        self.closed = False

    @classmethod
    async def connect(cls, url: str, *, headers: dict[str, str] | None = None, subprotocols: list[str] | None = None,
                      deflate: bool = True, timeout: float = 30.0) -> WebSocket:
        parts = urlsplit(url)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=_ssl_context() if secure else None,
                                    server_hostname=host if secure else None, limit=MAX_MESSAGE), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        if subprotocols:
            lines.append(f"Sec-WebSocket-Protocol: {', '.join(subprotocols)}")
        if deflate:
            lines.append("Sec-WebSocket-Extensions: permessage-deflate; client_no_context_takeover")
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split()[1])
        got = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in header_lines if h)}
        if status != 101:
            body = b""
            length = int(got.get("content-length") or 0)
            if length:
                body = await asyncio.wait_for(reader.readexactly(min(length, 4096)), timeout)
            writer.close()
            raise WebSocketError(f"WebSocket upgrade refused: HTTP {status}", status, body.decode(errors="replace"))
        accepted = "permessage-deflate" in got.get("sec-websocket-extensions", "")
        return cls(reader, writer, deflate and accepted)

    async def _send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        self.writer.write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        await self.writer.drain()

    async def _frame(self) -> tuple[bool, bool, int, bytes]:
        b0, b1 = await self.reader.readexactly(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if n > MAX_MESSAGE:
            raise WebSocketError(f"frame of {n} bytes is too large")
        mask = await self.reader.readexactly(4) if b1 & 0x80 else None
        payload = await self.reader.readexactly(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.bytes_received += n
        return bool(b0 & 0x80), bool(b0 & 0x40), b0 & 0x0F, payload

    async def messages(self, idle_timeout: float | None = 60.0) -> AsyncIterator[bytes]:
        """Yields each data message (text or binary) as bytes, until the server closes.

        Raises WebSocketError when nothing arrives for `idle_timeout` seconds (a stalled stream)."""
        parts: list[bytes] = []
        compressed = False
        while not self.closed:
            try:
                fin, rsv1, opcode, payload = await asyncio.wait_for(self._frame(), idle_timeout)
            except asyncio.IncompleteReadError:
                self.closed = True
                return
            except (TimeoutError, OSError) as exc:
                self.closed = True
                self.writer.close()
                raise WebSocketError(f"stream stalled or dropped: {exc!r}") from None
            if opcode == OP_PING:
                await self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                await self._close_transport(payload[:2] or b"")
                return
            if opcode in (OP_TEXT, OP_BINARY):
                parts, compressed = [payload], rsv1
            elif opcode == OP_CONT:
                parts.append(payload)
            if not fin:
                continue
            data = b"".join(parts)
            if compressed and self.inflater is not None:
                # one inflater for the whole connection: correct with or without server context takeover
                data = self.inflater.decompress(data + b"\x00\x00\xff\xff")
            parts = []
            yield data

    async def _close_transport(self, code: bytes) -> None:
        try:
            await self._send(OP_CLOSE, code)
        except (ConnectionError, RuntimeError):
            pass
        self.writer.close()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self._close_transport(struct.pack("!H", 1000))
