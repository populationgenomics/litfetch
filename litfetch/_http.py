"""Shared httpx helpers for sources and resolvers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx

DEFAULT_TIMEOUT = 30.0
USER_AGENT = 'litfetch/0.1 (mailto:toby.sargeant@populationgenomics.org.au)'


@asynccontextmanager
async def client_ctx(
    http_client: httpx.AsyncClient | None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield ``http_client`` if given, else an ephemeral client for one call."""
    if http_client is not None:
        yield http_client
    else:
        async with httpx.AsyncClient(timeout=timeout) as owned:
            yield owned
