"""Shared test fixtures: a scripted, offline httpx transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest


class RecordingTransport(httpx.AsyncBaseTransport):
    """Drive a scripted sequence of responses keyed by ``METHOD path``."""

    def __init__(self, scripts: dict[str, list[httpx.Response]]) -> None:
        self._scripts = scripts
        self.calls: list[tuple[str, str, dict | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request and return the next scripted response for it."""
        key = f'{request.method} {request.url.path}'
        body: dict | None = None
        if request.content:
            body = json.loads(request.content)
        self.calls.append((key, str(request.url), body))
        queue = self._scripts.get(key) or self._scripts.get(request.url.path)
        if not queue:
            raise AssertionError(f'unexpected request: {key}')
        return queue.pop(0)


InstallTransport = Callable[[dict[str, list[httpx.Response]]], RecordingTransport]


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch) -> InstallTransport:
    """Return an installer that routes all ``httpx.AsyncClient`` traffic to a script."""

    def install(scripts: dict[str, list[httpx.Response]]) -> RecordingTransport:
        transport = RecordingTransport(scripts)
        original = httpx.AsyncClient.__init__

        def patched_init(client: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
            kwargs['transport'] = transport
            original(client, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx.AsyncClient, '__init__', patched_init)
        return transport

    return install
