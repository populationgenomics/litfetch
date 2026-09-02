"""Shared test fixtures: a scripted, offline httpx2 transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2
import pytest

MINIMAL_JATS = b"""<?xml version='1.0'?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta>
      <title-group><article-title>A short paper</article-title></title-group>
      <abstract><p>One sentence abstract.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec>
      <title>Intro</title>
      <p>Hello world.</p>
    </sec>
  </body>
</article>
"""

# A BITS book part (the Bookshelf payload shape, DOCTYPE as served): book metadata, then the part.
MINIMAL_BITS = b"""<?xml version='1.0'?>
<!DOCTYPE book-part-wrapper PUBLIC "-//NLM//DTD BITS Book Interchange DTD v2.0 20151225//EN" "BITS-book2.dtd">
<book-part-wrapper xmlns:xlink="http://www.w3.org/1999/xlink" content-type="chapter">
  <book-meta>
    <book-title-group><book-title>A short book</book-title></book-title-group>
  </book-meta>
  <book-part book-part-type="chapter">
    <book-part-meta>
      <title-group><title>A short chapter</title></title-group>
    </book-part-meta>
    <body>
      <sec>
        <title>Intro</title>
        <p>Hello world.</p>
      </sec>
    </body>
  </book-part>
</book-part-wrapper>
"""


class RecordingTransport(httpx2.AsyncBaseTransport):
    """Drive a scripted sequence of responses keyed by ``METHOD path``."""

    def __init__(self, scripts: dict[str, list[httpx2.Response]]) -> None:
        self._scripts = scripts
        self.calls: list[tuple[str, str, dict | None]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
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


InstallTransport = Callable[[dict[str, list[httpx2.Response]]], RecordingTransport]


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch) -> InstallTransport:
    """Return an installer that routes all ``httpx2.AsyncClient`` traffic to a script."""

    def install(scripts: dict[str, list[httpx2.Response]]) -> RecordingTransport:
        transport = RecordingTransport(scripts)
        original = httpx2.AsyncClient.__init__

        def patched_init(client: httpx2.AsyncClient, *args: object, **kwargs: object) -> None:
            kwargs['transport'] = transport
            original(client, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx2.AsyncClient, '__init__', patched_init)
        return transport

    return install
