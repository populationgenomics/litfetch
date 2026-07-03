"""Tests for the Session: client lifecycle, escape hatch, pacing, and scoped cache."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from litfetch import _http, sessions


def _counting_client(calls: list[str], responses: list[httpx.Response] | None = None) -> httpx.AsyncClient:
    """A client whose transport records each request URL and returns 200 (or a script)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return responses.pop(0) if responses else httpx.Response(200, text='ok')

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _factory(calls: list[str], responses: list[httpx.Response] | None = None) -> Callable[[], httpx.AsyncClient]:
    return lambda: _counting_client(calls, responses)


async def test_session_builds_and_closes_client() -> None:
    built: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        client = _counting_client([])
        built.append(client)
        return client

    async with sessions.Session(client_factory=factory) as s:
        assert s.client is built[0]
        assert not s.client.is_closed
    assert built[0].is_closed


async def test_client_escape_hatch_unavailable_outside_context() -> None:
    s = sessions.Session(client_factory=_factory([]))
    with pytest.raises(RuntimeError, match='inside the context manager'):
        _ = s.client


async def test_get_delegates_to_client() -> None:
    calls: list[str] = []
    async with sessions.Session(client_factory=_factory(calls)) as s:
        resp = await s.get('https://example/', params={'a': '1'})
    assert resp.status_code == 200
    assert calls == ['https://example/?a=1']


async def test_get_follows_redirects_only_when_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/start':
            return httpx.Response(301, headers={'Location': 'https://h/final'})
        return httpx.Response(200, text='landed')

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    async with sessions.Session(client_factory=factory) as s:
        not_followed = await s.get('https://h/start')
        followed = await s.get('https://h/start', follow_redirects=True)
    assert not_followed.status_code == 301
    assert followed.status_code == 200
    assert followed.text == 'landed'


# --- pacing --------------------------------------------------------------


async def test_pace_does_not_sleep_at_default_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(sessions.asyncio, 'sleep', fake_sleep)
    async with sessions.Session(client_factory=_factory([])) as s:
        await s._pace('https://h/', _http.Rate.DEFAULT)
        await s._pace('https://h/', _http.Rate.DEFAULT)
    assert sleeps == []


async def test_pace_spaces_repeat_requests_to_one_host(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(sessions.asyncio, 'sleep', fake_sleep)
    async with sessions.Session(client_factory=_factory([])) as s:
        await s._pace('https://h/a', _http.Rate.NCBI_KEYED)  # first: no prior send, no wait
        await s._pace('https://h/b', _http.Rate.NCBI_KEYED)  # second: must wait ~interval
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(_http.Rate.NCBI_KEYED.min_interval, abs=0.02)


async def test_pace_is_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(sessions.asyncio, 'sleep', fake_sleep)
    async with sessions.Session(client_factory=_factory([])) as s:
        await s._pace('https://one/', _http.Rate.NCBI_KEYED)
        await s._pace('https://two/', _http.Rate.NCBI_KEYED)  # different host: no wait
    assert sleeps == []


# --- scope and its cache -------------------------------------------------


async def test_scope_shares_client_and_pacing_with_parent() -> None:
    async with sessions.Session(client_factory=_factory([])) as session, session.scope() as s:
        assert s.client is session.client
        assert s._pacers is session._pacers


async def test_scope_caches_repeat_get() -> None:
    calls: list[str] = []
    async with sessions.Session(client_factory=_factory(calls)) as session, session.scope() as s:
        first = await s.get('https://x/')
        second = await s.get('https://x/')
    assert calls == ['https://x/']  # second served from cache, no round-trip
    assert first is second


async def test_bare_session_does_not_cache() -> None:
    calls: list[str] = []
    async with sessions.Session(client_factory=_factory(calls)) as session:
        await session.get('https://x/')
        await session.get('https://x/')
    assert calls == ['https://x/', 'https://x/']


async def test_scope_does_not_cache_transient_status() -> None:
    calls: list[str] = []
    responses = [httpx.Response(503), httpx.Response(200, text='ok')]
    factory = _factory(calls, responses)
    session_cm = sessions.Session(client_factory=factory, retry=_http.RetryPolicy(max_attempts=1))
    async with session_cm as session, session.scope() as s:
        first = await s.get('https://x/')
        second = await s.get('https://x/')
    assert first.status_code == 503  # not cached
    assert second.status_code == 200
    assert calls == ['https://x/', 'https://x/']


async def test_scope_cache_is_dropped_on_exit() -> None:
    calls: list[str] = []
    async with sessions.Session(client_factory=_factory(calls)) as session:
        async with session.scope() as s:
            await s.get('https://x/')
        async with session.scope() as s:
            await s.get('https://x/')  # fresh scope: cache does not carry over
    assert calls == ['https://x/', 'https://x/']
