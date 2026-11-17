"""Test configuration for the wallet tracer.

Tests run offline: `socket.socket` and `getaddrinfo` raise for internet families, so an accidental network call
fails loudly. Inject `httpx.MockTransport` instead. AF_UNIX stays allowed (asyncio needs it).
"""

import json
import socket
from pathlib import Path
from typing import Any

_BLOCKED_FAMILIES = {socket.AF_INET, socket.AF_INET6}


class _NoNetwork(socket.socket):
    def __init__(self, family: int = socket.AF_INET, *args: object, **kwargs: object) -> None:
        if family in _BLOCKED_FAMILIES:
            raise RuntimeError("A test tried to open a network socket. Inject an httpx.MockTransport instead.")
        super().__init__(family, *args, **kwargs)  # type: ignore[arg-type]


socket.socket = _NoNetwork  # type: ignore[misc]


def _no_dns(*args: object, **kwargs: object) -> object:
    raise RuntimeError("A test tried to resolve a hostname. Tests run offline — see conftest.py.")


socket.getaddrinfo = _no_dns  # type: ignore[assignment]

FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())
