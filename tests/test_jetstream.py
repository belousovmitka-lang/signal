import asyncio
import base64
import hashlib
import json
import struct
import zlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from signal_connectors.connectors import jetstream as js
from signal_connectors.connectors.jetstream import JetstreamConnector, PainMatcher, web_url
from signal_connectors.industries import load_industries
from signal_connectors.models import RawPost, SourceQuery, make_post_id, utcnow
from signal_connectors.runner import run_source
from signal_connectors.ws import WebSocket, WebSocketError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC11B65"


# --- the WebSocket client against a real local server -------------------------

def frame(opcode: int, payload: bytes, fin: bool = True, rsv1: bool = False) -> bytes:
    head = bytes([(0x80 if fin else 0) | (0x40 if rsv1 else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


async def serve(script, *, deflate=False, status=101):
    received: list[bytes] = []

    async def handle(reader, writer):
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        if status != 101:
            body = b'{"error":"cursor too old"}'
            writer.write(f"HTTP/1.1 {status} Bad Request\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()
            return
        key = next(l.split(":", 1)[1].strip() for l in request.split("\r\n") if l.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        ext = "Sec-WebSocket-Extensions: permessage-deflate\r\n" if deflate else ""
        writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n{ext}\r\n".encode())
        for chunk in script:
            if chunk == "read":                       # read one masked client frame
                b0, b1 = await reader.readexactly(2)
                mask = await reader.readexactly(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(b1 & 0x7F)))
                received.append(bytes([b0 & 0x0F]) + payload)
            elif chunk == "hang":
                await asyncio.sleep(5)
            else:
                writer.write(chunk)
                await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/sub", received


async def test_ws_reads_fragmented_messages_answers_pings_and_closes():
    script = [frame(1, b'{"a":', fin=False), frame(9, b"hi"), "read", frame(0, b"1}"),
              frame(2, b"\x00" * 70000), frame(8, struct.pack("!H", 1000)), "read"]
    server, url, received = await serve(script)
    async with server:
        ws = await WebSocket.connect(url)
        got = [m async for m in ws.messages()]
    assert got[0] == b'{"a":1}' and len(got[1]) == 70000
    assert received[0] == bytes([0xA]) + b"hi"              # pong echoes the ping, masked on the wire
    assert received[1][0] == 0x8


async def test_ws_inflates_permessage_deflate_across_messages():
    comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)          # one context: later messages refer back
    def deflated(data: bytes) -> bytes:
        out = comp.compress(data) + comp.flush(zlib.Z_SYNC_FLUSH)
        return frame(1, out[:-4], rsv1=True)
    script = [deflated(b"hello hello hello"), deflated(b"hello hello again"), frame(8, b"")]
    server, url, _ = await serve(script, deflate=True)
    async with server:
        ws = await WebSocket.connect(url)
        assert [m async for m in ws.messages()] == [b"hello hello hello", b"hello hello again"]


async def test_ws_refused_upgrade_and_stall_raise():
    server, url, _ = await serve([], status=400)
    async with server:
        with pytest.raises(WebSocketError) as exc:
            await WebSocket.connect(url)
    assert exc.value.status == 400 and "too old" in exc.value.body
    server, url, _ = await serve(["hang"])
    async with server:
        ws = await WebSocket.connect(url)
        with pytest.raises(WebSocketError, match="stalled"):
            async for _ in ws.messages(idle_timeout=0.2):
                pass


# --- the pain filter ---------------------------------------------------------------

def test_matcher_keeps_first_person_work_pain_in_an_industry():
    m = PainMatcher(load_industries())
    assert m.match("I'm a dispatcher and I re-enter every rate confirmation by hand. Is there a tool?") == (
        "logistics", "dispatcher")
    assert m.match("We still copy and paste every BOL into the TMS")[0] == "logistics"
    for noise in ["I am so sick of this NATO aggression line.",                  # emotion, no workflow
                  "Ohio State red zone offense continues to be a nightmare",
                  "I write all my DM notes by hand, it helps it stick",          # workflow, no industry
                  "Washing it by hand between uses",                             # no first person
                  "I tend to manually pop the toast when the moment is right",   # "Toast" the POS is a brand
                  "Bring a pod and a bowl, I spend hours every week on this"]:   # "POD" only in capitals
        assert m.match(noise) is None, noise


# --- the connector, with a canned stream ----------------------------------------------

def src(**options):
    return {"sources": {"bluesky": {"connector": "jetstream", "enabled": True, "status": "done", "route": "own_api",
                                    "source_type": "social", "cadence": "daily", "deletion_recheck_days": 7,
                                    "queries": [{"limit": 10000}], "options": options}}}


def event(seq, minutes_ago, op="create", text=None, did="did:plc:aaa", rkey=None, langs=("en",)):
    payload = {"$type": "network.bsky.jetstream.subscribeEvents#commit", "did": did, "seq": seq,
               "time": (utcnow() - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
               "operation": op, "collection": js.POST, "rkey": rkey or f"r{seq}", "cid": f"c{seq}"}
    if op == "create":
        payload["record"] = {"$type": js.POST, "text": text, "langs": list(langs),
                             "createdAt": payload["time"]}
    return json.dumps({"$type": "message", "payload": payload}).encode()


class FakeSocket:
    def __init__(self, url, batches):
        self.url, self.bytes_received, self.closed = url, 0, False
        self.batch = batches.pop(0)

    async def messages(self, idle_timeout=None):
        for item in self.batch:
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self):
        self.closed = True


def fake_stream(monkeypatch, batches):
    urls = []

    async def connect(url, **kw):
        urls.append(url)
        assert kw["subprotocols"] == ["xrpc.v1.json"] and "SignalBot" in kw["headers"]["User-Agent"]
        if isinstance(batches[0], WebSocketError):
            raise batches.pop(0)
        return FakeSocket(url, batches)
    monkeypatch.setattr(js.WebSocket, "connect", connect)
    return urls


def appview(hidden=()):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path.endswith("getProfiles"):
            dids = request.url.params.get_list("actors")
            return httpx.Response(200, json={"profiles": [
                {"did": d, "labels": [{"val": "!no-unauthenticated"}] if d in hidden else []} for d in dids]})
        raise AssertionError(request.url)
    return handler


PAIN = "I'm a dispatcher and I re-enter every rate confirmation by hand. Is there a tool for this?"


async def test_run_keeps_matches_skips_hidden_authors_and_stops_at_run_start(make_services, monkeypatch):
    urls = fake_stream(monkeypatch, [[
        event(1, 50, text=PAIN),
        event(2, 40, text="Ohio State offense is a nightmare"),
        event(3, 30, text=PAIN.replace("rate", "load"), did="did:plc:hidden"),
        event(4, 20, text=PAIN.replace("every", "each"), langs=("pt",)),
        event(5, -1, text=PAIN.replace("every", "all")),             # after the run started: not read
    ]])
    async with make_services(src(), appview(hidden={"did:plc:hidden"})) as svc:
        out = await run_source(svc, svc.sources.get("bluesky"), collect=True)
        assert out.result.status == "ok" and [p.text for p in out.posts] == [PAIN]
        post = out.posts[0]
        assert post.source == "bluesky" and post.url == web_url("did:plc:aaa", "r1")
        assert post.industry_hint == "logistics" and post.author_role == "dispatcher"
        # The DID is in the post's own link (Bluesky permalinks need it); nowhere else as the author.
        assert post.author_hash and "did:plc:aaa" not in post.author_hash
        assert "handle" not in json.dumps(post.raw) and "did" not in post.raw
        assert post.raw["uri"] == "at://did:plc:aaa/app.bsky.feed.post/r1"
        assert svc.store.count_posts("bluesky") == 1
    cursor = int(parse_qs(urlsplit(urls[0]).query)["cursor"][0])
    floor = js.time_us(utcnow() - js.LOOKBACK)
    assert abs(cursor - floor) < 60_000_000                     # first run: the oldest the stream keeps


async def test_delete_events_purge_stored_posts_and_reconnects_resume_from_seq(make_services, monkeypatch):
    stored_url = web_url("did:plc:bbb", "old1")
    urls = fake_stream(monkeypatch, [
        [event(10, 30, text="hello"), WebSocketError("stream stalled or dropped")],
        [event(10, 30, text="hello"), event(11, 20, op="delete", did="did:plc:bbb", rkey="old1")]
        + [event(100 + i, 19, op="delete", did="did:plc:other", rkey=f"x{i}") for i in range(1500)]   # not ours
        + [event(9999, -1)],
    ])
    async with make_services(src(), appview()) as svc:
        svc.store.save_posts([RawPost(id=make_post_id("bluesky", stored_url), source="bluesky", source_type="social",
                                      route="own_api", url=stored_url, title=None, text="an older post",
                                      is_snippet=False, author_hash=None, author_role=None, community="Bluesky",
                                      published_at=utcnow() - timedelta(days=2), fetched_at=utcnow(),
                                      language="en", engagement={}, money=None, industry_hint=None, query=None,
                                      raw={})])
        assert svc.store.count_posts("bluesky") == 1
        out = await run_source(svc, svc.sources.get("bluesky"))
        assert out.result.status == "ok" and svc.store.count_posts("bluesky") == 0
    assert parse_qs(urlsplit(urls[1]).query)["cursor"] == ["10"]      # inclusive resume from the last seq


async def test_too_old_cursor_falls_back_to_the_floor(make_services, monkeypatch):
    urls = fake_stream(monkeypatch, [WebSocketError("refused", 400, "cursor below floor"), [event(1, -1)]])
    async with make_services(src(), appview()) as svc:
        connector = JetstreamConnector(svc.sources.get("bluesky"), svc)
        posts = [p async for p in connector.fetch(SourceQuery(since=utcnow() - timedelta(hours=10)))]
    assert posts == [] and len(urls) == 2


def test_sources_yaml_entry():
    from signal_connectors.config import load_sources
    s = load_sources("sources.yaml").get("bluesky")
    assert s.connector == "jetstream" and s.deletion_recheck_days and s.blocked_reason() is None


async def test_connection_reset_on_connect_is_retried(make_services, monkeypatch):
    monkeypatch.setattr(js, "RETRY_SECONDS", 0)
    urls = fake_stream(monkeypatch, [WebSocketError("connect failed: ConnectionResetError()"),
                                     [event(1, 30, text=PAIN), event(2, -1)]])
    async with make_services(src(), appview()) as svc:
        out = await run_source(svc, svc.sources.get("bluesky"))
    assert out.result.status == "ok" and out.result.new == 1 and len(urls) == 2


async def test_ws_network_failure_on_connect_is_a_websocket_error():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(WebSocketError, match="connect failed") as exc:
        await WebSocket.connect(f"ws://127.0.0.1:{port}/sub")
    assert exc.value.status is None
