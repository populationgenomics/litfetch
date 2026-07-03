"""Low-level HTTP primitives shared across litfetch.

The public vocabulary the source and resolver layers depend on -- the
:class:`Http` request protocol and the :class:`Rate` politeness levels -- lives
here, together with the retrying GET primitive (:func:`get`) they are built on.
Keeping these here (and not in :mod:`litfetch.sessions`) lets ``fetchers`` and
``resolvers`` depend on the protocol without importing the concrete
:class:`~litfetch.sessions.Session`, which in turn imports them.

:func:`get` is the single choke point for outbound GETs: it adds retry with
exponential backoff and honours a 429/503 ``Retry-After``.  It operates over a
raw client and knows nothing of pacing or caching; :class:`Session` layers those
on top and is the concrete :class:`Http` fetchers and resolvers actually call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import random
from collections.abc import Mapping
from typing import Protocol

import httpx

DEFAULT_TIMEOUT = 30.0
# Base User-Agent, no contact. A caller who sets Session(contact=...) gets a
# `(mailto:...)` appended and that address fed to the polite-pool params; litfetch
# ships no default contact of its own.
USER_AGENT = 'litfetch/0.1'

# Status codes worth retrying: 429 (rate limited) and the transient 5xx family.
# A 4xx other than 429 is the caller's fault and will not fix itself on retry.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class Rate(enum.Enum):
    """A named politeness rate, chosen at the call site.

    The ``KEYED`` variants apply when the caller holds an API key for that host
    (a higher allowance), the ``UNKEYED`` variants are the polite public rate.
    ``DEFAULT`` imposes no throttle -- for hosts (S3, publisher CDNs) with no
    tight per-client limit.  :attr:`min_interval` is the resulting minimum
    inter-request interval in seconds.
    """

    DEFAULT = 'default'
    NCBI_UNKEYED = 'ncbi_unkeyed'
    NCBI_KEYED = 'ncbi_keyed'
    S2_UNKEYED = 's2_unkeyed'
    S2_KEYED = 's2_keyed'

    @property
    def min_interval(self) -> float:
        """Minimum seconds between requests to one host at this rate."""
        return _MIN_INTERVALS[self]


# Seconds between requests per rate.  Distinct members may share an interval
# (NCBI and S2 keyed allowances both land near 10 req/s), so the interval is a
# mapping, not the enum value -- equal values would alias the members.
_MIN_INTERVALS = {
    Rate.DEFAULT: 0.0,
    Rate.NCBI_UNKEYED: 0.34,  # ~3 req/s, NCBI's keyless allowance
    Rate.NCBI_KEYED: 0.1,  # ~10 req/s with an NCBI API key
    Rate.S2_UNKEYED: 1.0,  # Semantic Scholar's shared public pool: stay conservative
    Rate.S2_KEYED: 0.1,  # with a Semantic Scholar API key
}


class Http(Protocol):
    """The request surface a source or resolver needs: a paced, retrying GET.

    :class:`~litfetch.sessions.Session` satisfies this.  A source is handed an
    ``Http``, never the Session's lifecycle, so it depends on one method (and one
    attribute) and is trivially faked in a test.

    ``contact`` is the caller-configured identity (an email) for polite-pool
    parameters -- Unpaywall's required ``email``, Crossref's ``mailto``, NCBI's
    ``email`` -- and ``None`` when the caller set none; a source reads it rather
    than carrying a hardcoded address.
    """

    contact: str | None

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        rate: Rate = Rate.DEFAULT,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """GET ``url``, paced per ``rate`` and retried per the session policy."""
        ...


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    """How :func:`get` retries a transient failure.

    A transient failure is an ``httpx.TransportError`` (timeout, connection
    reset) or a retryable status (429, 500, 502, 503, 504).  Backoff is
    exponential with full jitter -- ``uniform(0, base_delay * 2**attempt)`` --
    capped at ``max_delay``; a 429/503 ``Retry-After`` in integer seconds
    overrides the jitter, also capped.  ``max_attempts`` counts total tries, so
    ``max_attempts=1`` disables retrying.
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0

    def __post_init__(self) -> None:
        # >= 1 so the get() loop always runs at least once (0 would fall through
        # to its `unreachable` guard).
        if self.max_attempts < 1:
            raise ValueError(f'max_attempts must be >= 1, got {self.max_attempts}')


DEFAULT_RETRY = RetryPolicy()


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
    follow_redirects: bool = False,
) -> httpx.Response:
    """GET ``url``, retrying a transient failure per ``retry``.

    Retries an ``httpx.TransportError`` or a retryable status (see
    :class:`RetryPolicy`) with backoff, then returns the final response --
    including a still-failing status, so the caller keeps its own status
    handling.  Re-raises the last transport error when every attempt fails.

    Args:
        client: The httpx client to issue the request on.
        url: The absolute URL to GET.
        params: Query parameters, if any.
        headers: Request headers, if any.
        retry: The backoff/attempt policy.
        follow_redirects: Follow 3xx redirects (off by default; file downloads
            enable it to follow publisher PDF redirects).

    Returns:
        The final :class:`httpx.Response` (a non-retryable status, or the last
        response after exhausting retries).

    Raises:
        httpx.TransportError: If every attempt fails at the transport layer.
    """
    for attempt in range(retry.max_attempts):
        last_attempt = attempt == retry.max_attempts - 1
        retry_after: float | None = None
        try:
            response = await client.get(url, params=params, headers=headers, follow_redirects=follow_redirects)
        except httpx.TransportError:
            if last_attempt:
                raise
        else:
            if response.status_code not in _RETRYABLE_STATUS or last_attempt:
                return response
            retry_after = _retry_after_seconds(response)
        await asyncio.sleep(_backoff(attempt, retry_after, retry))
    raise AssertionError('unreachable: the loop returns or raises on the last attempt')


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header as integer seconds; ``None`` otherwise.

    The HTTP-date form is accepted by the spec but not used by the APIs
    litfetch talks to; it falls through to ``None`` (jittered backoff).
    """
    value = response.headers.get('Retry-After')
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _backoff(attempt: int, retry_after: float | None, policy: RetryPolicy) -> float:
    """Seconds to wait before the next attempt: ``Retry-After`` or jittered backoff."""
    if retry_after is not None:
        return min(retry_after, policy.max_delay)
    jittered = random.uniform(0, policy.base_delay * 2**attempt)  # noqa: S311 -- jitter, not crypto
    return min(jittered, policy.max_delay)
