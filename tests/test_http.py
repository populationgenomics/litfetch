"""Tests for the retrying GET wrapper in :mod:`litfetch._http`."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest

from litfetch import _http

# base_delay/max_delay = 0 so retries do not actually sleep during tests.
_FAST = _http.RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)


class _Script:
    """A MockTransport handler that returns (or raises) scripted items in order."""

    def __init__(self, items: Sequence[httpx.Response | Exception]) -> None:
        self._items = list(items)
        self.calls = 0

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(script: _Script) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(script))


async def test_returns_immediately_on_success() -> None:
    script = _Script([httpx.Response(200, text='ok')])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_FAST)
    assert resp.status_code == 200
    assert script.calls == 1


async def test_does_not_retry_non_retryable_status() -> None:
    script = _Script([httpx.Response(404)])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_FAST)
    assert resp.status_code == 404
    assert script.calls == 1


async def test_retries_retryable_status_then_succeeds() -> None:
    script = _Script([httpx.Response(503), httpx.Response(200, text='ok')])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_FAST)
    assert resp.status_code == 200
    assert script.calls == 2


async def test_returns_final_error_after_exhausting_attempts() -> None:
    script = _Script([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_FAST)
    assert resp.status_code == 503
    assert script.calls == 3


async def test_retries_transport_error_then_succeeds() -> None:
    script = _Script([httpx.ConnectError('boom'), httpx.Response(200, text='ok')])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_FAST)
    assert resp.status_code == 200
    assert script.calls == 2


async def test_reraises_transport_error_after_exhausting_attempts() -> None:
    script = _Script([httpx.ConnectError('boom')] * 3)
    async with _client(script) as c:
        with pytest.raises(httpx.ConnectError):
            await _http.get(c, 'https://x/', retry=_FAST)
    assert script.calls == 3


async def test_max_attempts_one_disables_retry() -> None:
    script = _Script([httpx.Response(503)])
    async with _client(script) as c:
        resp = await _http.get(c, 'https://x/', retry=_http.RetryPolicy(max_attempts=1))
    assert resp.status_code == 503
    assert script.calls == 1


@pytest.mark.parametrize(
    ('header', 'expected'),
    [({'Retry-After': '5'}, 5.0), ({'Retry-After': 'Wed, 21 Oct 2015 07:28:00 GMT'}, None), ({}, None)],
)
def test_retry_after_seconds(header: dict[str, str], expected: float | None) -> None:
    resp = httpx.Response(503, headers=header)
    assert _http._retry_after_seconds(resp) == expected
