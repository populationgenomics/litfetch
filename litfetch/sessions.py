"""The Session facade: the object callers hold to run litfetch.

See [ADR 0001](../docs/adr/0001-http-session-seam.md).  A :class:`Session` owns
one ``httpx.AsyncClient`` (built by an injectable ``client_factory``) and the
per-host pacing state, and it is the concrete :class:`~litfetch._http.Http` the
source and resolver layers issue requests on.  The library's operations --
:meth:`~Session.fetch_body`, :meth:`~Session.list_files`,
:meth:`~Session.fetch_file`, :meth:`~Session.resolve_access`,
:meth:`~Session.related_ids` -- are methods on it, so a caller threads no HTTP
argument: the object it holds *is* the context.

:meth:`Session.scope` returns a child sharing the parent's client and pacing but
with its own short-lived response cache -- open one per logical unit of work (a
paper) so a duplicate GET within that unit is served once and the cache cannot
grow across the run::

    async with litfetch.Session() as session:          # long-lived: pool + pacing
        for pid in paper_ids:
            async with session.scope() as s:            # short-lived: cache
                blob = await s.fetch_body(ArticleIds(pmid=pid), resolver=resolver)

Module-level functions of the same names are one-shot conveniences: each opens
an ephemeral session for a single call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import urllib.parse
from collections.abc import Callable, Mapping, Sequence

import httpx

from litfetch import _http, artifacts, ids, relations, resolvers, source_metadata
from litfetch import fetchers as fetchers_


def _default_client_factory(timeout: float, contact: str | None) -> Callable[[], httpx.AsyncClient]:
    """Build the default client factory: a litfetch User-Agent and ``timeout``.

    A ``contact`` (an email) appends ``(mailto:<contact>)`` to the User-Agent for
    polite-pool identification; ``None`` leaves the bare ``litfetch/<version>``.
    """
    user_agent = f'{_http.USER_AGENT} (mailto:{contact})' if contact else _http.USER_AGENT

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, headers={'User-Agent': user_agent})

    return factory


@dataclasses.dataclass
class _HostPacer:
    """Per-host pacing state: a lock plus the earliest monotonic time to send next."""

    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    next_allowed: float = 0.0


def _is_cacheable(response: httpx.Response) -> bool:
    """Report whether a response is a deterministic outcome worth caching.

    2xx and 4xx-except-429 are stable answers (including a 404 "no record").  A
    5xx or 429 is transient -- the retry layer owns it -- and must never be
    cached, or the whole scope would be poisoned by one blip.
    """
    return response.status_code < 500 and response.status_code != 429


def _cache_key(
    url: str,
    params: Mapping[str, str | int] | None,
    headers: Mapping[str, str] | None,
    follow_redirects: bool,
) -> tuple[str, tuple[tuple[str, str | int], ...], tuple[tuple[str, str], ...], bool]:
    """A hashable key identifying one GET; headers/redirect-mode included so they vary it."""
    return (
        url,
        tuple(sorted(params.items())) if params else (),
        tuple(sorted(headers.items())) if headers else (),
        follow_redirects,
    )


class Session:
    """Owns the HTTP client and per-host pacing, and exposes litfetch's operations.

    Use as an async context manager: it builds the client on entry (via
    ``client_factory``) and closes it on exit.  ``client_factory`` is the
    injection point for a proxy, an institutional EZproxy, or CA-cert
    configuration; the default builds a litfetch-configured client.  :attr:`client`
    exposes the raw client for needs :meth:`get` does not cover (POST, streaming).

    A bare session does not cache; :meth:`scope` returns a child that does.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        retry: _http.RetryPolicy = _http.DEFAULT_RETRY,
        timeout: float = _http.DEFAULT_TIMEOUT,
        contact: str | None = None,
    ) -> None:
        self.contact = contact
        self._factory = client_factory or _default_client_factory(timeout, contact)
        self._retry = retry
        self._client: httpx.AsyncClient | None = None
        self._pacers: dict[str, _HostPacer] = {}
        self._cache: dict[object, httpx.Response] | None = None
        self._parent: Session | None = None

    def scope(self) -> Session:
        """Return a child session with its own response cache, entered per unit of work.

        The child shares this session's client factory, pacing state, and retry
        policy; on exit it drops its cache and leaves the client open.  Caching
        applies only inside a scope.
        """
        child = Session(client_factory=self._factory, retry=self._retry)
        child._adopt(self)
        return child

    def _adopt(self, parent: Session) -> None:
        """Bind this scope to ``parent``: share its client, pacing, and contact; keep own cache."""
        self._parent = parent
        self._pacers = parent._pacers  # share the pacing dict by reference, so pacing spans the run
        self.contact = parent.contact
        self._cache = {}

    async def __aenter__(self) -> Session:  # noqa: PYI034 -- Self needs py311; project targets py310
        if self._parent is None:
            self._client = self._factory()
            return self
        # A scope shares the parent's client; the parent must already be entered.
        # Its `client` property raises when it is not -- re-raise with the message
        # that names the actual mistake.
        try:
            self._client = self._parent.client
        except RuntimeError as e:
            raise RuntimeError('enter the parent session before entering its scope()') from e
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._parent is None and self._client is not None:
            await self._client.aclose()
        self._client = None
        self._cache = None

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying httpx client (escape hatch); valid only inside the context."""
        if self._client is None:
            raise RuntimeError('Session.client is only available inside the context manager')
        return self._client

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        rate: _http.Rate = _http.Rate.DEFAULT,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """GET ``url``, paced per ``rate`` then retried per the session policy.

        ``follow_redirects`` is off by default (an API move should surface, not be
        chased silently); file downloads pass it through to follow publisher
        redirects.  In a :meth:`scope`, a deterministic response (see
        :func:`_is_cacheable`) is cached by ``url`` + params + headers +
        redirect-mode for the scope's life and a repeat is served without a
        round-trip.
        """
        key = _cache_key(url, params, headers, follow_redirects)
        if self._cache is not None and key in self._cache:
            return self._cache[key]
        await self._pace(url, rate)
        response = await _http.get(
            self.client, url, params=params, headers=headers, retry=self._retry, follow_redirects=follow_redirects
        )
        if self._cache is not None and _is_cacheable(response):
            self._cache[key] = response
        return response

    async def _pace(self, url: str, rate: _http.Rate) -> None:
        """Wait until the per-host minimum interval since the last send has elapsed.

        The lock is held across the wait, so concurrent requests to one host
        queue and space out; different hosts pace independently.
        """
        interval = rate.min_interval
        if interval <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc
        pacer = self._pacers.setdefault(host, _HostPacer())
        async with pacer.lock:
            loop = asyncio.get_running_loop()
            wait = pacer.next_allowed - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            pacer.next_allowed = loop.time() + interval

    async def fetch_body(
        self,
        article_ids: ids.ArticleIds,
        *,
        resolver: resolvers.Resolver | None = None,
        fetchers: Sequence[fetchers_.Fetcher] | None = None,
        credentials: Mapping[str, object] | None = None,
    ) -> artifacts.Blob | None:
        """Walk the fetcher ladder, resolving identifiers on demand, return the first hit.

        When the next fetcher needs an identifier ``article_ids`` lacks, invokes
        ``resolver`` once (memoised) to enrich the bundle, then continues.  Returns
        the first non-``None`` body :class:`~litfetch.artifacts.Blob`, or ``None``
        when nothing serves it.  The blob carries raw bytes; rendering it (e.g.
        XML -> markdown) is the caller's concern.
        """
        chosen = tuple(fetchers) if fetchers is not None else fetchers_.default_fetchers()
        resolved = False
        for fetcher in chosen:
            if not article_ids.has(fetcher.requires):
                if resolver is not None and not resolved:
                    article_ids = article_ids.merge(await resolver(article_ids, self))
                    resolved = True
                if not article_ids.has(fetcher.requires):
                    continue
            blob = await fetcher.fetch(article_ids, credentials=credentials, http=self)
            if blob is not None:
                return blob
        return None

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        sources: Sequence[fetchers_.FileSource] | None = None,
        kind: artifacts.FileKind | None = None,
        credentials: Mapping[str, object] | None = None,
    ) -> tuple[artifacts.File, ...]:
        """Enumerate an article's file-set across every source (a union, not first-wins).

        Pass ``kind`` to keep only body renditions or only supplementary material.
        ``sources`` defaults to :func:`~litfetch.fetchers.default_file_sources`.
        """
        chosen = tuple(sources) if sources is not None else fetchers_.default_file_sources()
        found: list[artifacts.File] = []
        for source in chosen:
            found.extend(await source.list_files(article_ids, credentials=credentials, http=self))
        if kind is not None:
            found = [file for file in found if file.kind is kind]
        return tuple(found)

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        sources: Sequence[fetchers_.FileSource] | None = None,
        credentials: Mapping[str, object] | None = None,
    ) -> artifacts.Blob | None:
        """Download one file's bytes, routing to the source whose ``name`` owns it.

        Returns ``None`` when no registered source claims the file.
        """
        chosen = tuple(sources) if sources is not None else fetchers_.default_file_sources()
        for source in chosen:
            if source.name == file.source:
                return await source.fetch_file(file, credentials=credentials, http=self)
        return None

    async def resolve_access(
        self,
        article_ids: ids.ArticleIds,
        *,
        email: str | None = None,
    ) -> artifacts.SourceMetadata:
        """Resolve licence / OA status from Unpaywall (see :func:`~litfetch.source_metadata.resolve_access`).

        Unpaywall requires an email; it defaults to the session ``contact`` and
        can be overridden here. Without either, the lookup is skipped.
        """
        return await source_metadata.resolve_access(article_ids, http=self, email=email)

    async def related_ids(self, article_ids: ids.ArticleIds) -> tuple[relations.Related, ...]:
        """Find preprint / published counterparts (see :func:`~litfetch.relations.related_ids`)."""
        return await relations.related_ids(article_ids, http=self)


async def fetch_body(
    article_ids: ids.ArticleIds,
    *,
    resolver: resolvers.Resolver | None = None,
    fetchers: Sequence[fetchers_.Fetcher] | None = None,
    credentials: Mapping[str, object] | None = None,
) -> artifacts.Blob | None:
    """One-shot :meth:`Session.fetch_body`: opens an ephemeral session for this call."""
    async with Session() as session:
        return await session.fetch_body(article_ids, resolver=resolver, fetchers=fetchers, credentials=credentials)


async def list_files(
    article_ids: ids.ArticleIds,
    *,
    sources: Sequence[fetchers_.FileSource] | None = None,
    kind: artifacts.FileKind | None = None,
    credentials: Mapping[str, object] | None = None,
) -> tuple[artifacts.File, ...]:
    """One-shot :meth:`Session.list_files`: opens an ephemeral session for this call."""
    async with Session() as session:
        return await session.list_files(article_ids, sources=sources, kind=kind, credentials=credentials)


async def fetch_file(
    file: artifacts.File,
    *,
    sources: Sequence[fetchers_.FileSource] | None = None,
    credentials: Mapping[str, object] | None = None,
) -> artifacts.Blob | None:
    """One-shot :meth:`Session.fetch_file`: opens an ephemeral session for this call."""
    async with Session() as session:
        return await session.fetch_file(file, sources=sources, credentials=credentials)


async def resolve_access(
    article_ids: ids.ArticleIds,
    *,
    email: str | None = None,
) -> artifacts.SourceMetadata:
    """One-shot :meth:`Session.resolve_access`: opens an ephemeral session for this call.

    ``email`` is the Unpaywall identity (Unpaywall requires it; without one the
    lookup is skipped). It is passed only to Unpaywall, not promoted to the
    session ``contact``/User-Agent -- hold a :class:`Session` for that.
    """
    async with Session() as session:
        return await session.resolve_access(article_ids, email=email)


async def related_ids(article_ids: ids.ArticleIds) -> tuple[relations.Related, ...]:
    """One-shot :meth:`Session.related_ids`: opens an ephemeral session for this call."""
    async with Session() as session:
        return await session.related_ids(article_ids)
