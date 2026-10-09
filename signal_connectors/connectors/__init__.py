"""Connector registry. Each module here registers its class with @register."""

from __future__ import annotations

import importlib
from typing import TypeVar

from ..base import BaseConnector

_REGISTRY: dict[str, type[BaseConnector]] = {}

C = TypeVar("C", bound=type[BaseConnector])


def register(cls: C) -> C:
    _REGISTRY[cls.connector] = cls
    return cls


def get_connector_class(connector: str) -> type[BaseConnector]:
    if connector not in _REGISTRY:
        try:
            importlib.import_module(f"{__name__}.{connector}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{connector}":
                raise
            raise LookupError(f"no connector module signal_connectors/connectors/{connector}.py") from None
    try:
        return _REGISTRY[connector]
    except KeyError:
        raise LookupError(f"module {connector}.py did not register a connector named {connector!r}") from None


def registered() -> dict[str, type[BaseConnector]]:
    return dict(_REGISTRY)
