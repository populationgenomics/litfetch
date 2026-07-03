"""Crossref works fetch, shared by the Elsevier link locator, relations, and TDM.

One DOI-keyed GET against the Crossref works API returns a ``message`` that
several callers read differently: the Elsevier full-text fetcher for its
``text/xml`` text-mining link, :mod:`litfetch.relations` for ``relation``
entries, and :class:`~litfetch.fetchers.CrossrefFileSource` for the
text-mining ``link[]`` renditions.  All go through :func:`fetch_work`; inside a
:meth:`~litfetch.sessions.Session.scope` the duplicate GET is served from cache.
"""

from __future__ import annotations

import logging

import httpx

from litfetch import _doi, _http

logger = logging.getLogger(__name__)

_CROSSREF_BASE = 'https://api.crossref.org/works'


async def fetch_work(doi: str, *, http: _http.Http, mailto: str | None = None) -> dict | None:
    """Return the Crossref ``message`` object for ``doi``, or ``None``.

    Args:
        doi: The DOI to look up.
        http: The :class:`~litfetch._http.Http` to issue the request on.
        mailto: Identifies the caller for Crossref's polite pool; defaults to
            ``http.contact``. Omitted (Crossref still answers, just not in the
            polite pool) when neither is set.

    Returns:
        The parsed ``message`` object, or ``None`` when the lookup fails or the
        response is not JSON.
    """
    mailto = mailto or http.contact
    params = {'mailto': mailto} if mailto else {}
    try:
        resp = await http.get(f'{_CROSSREF_BASE}/{_doi.encode_doi_path(doi)}', params=params)
    except httpx.HTTPError:
        logger.exception('Crossref lookup failed for %s', doi)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get('message')
    except ValueError:
        logger.warning('Crossref returned a non-JSON response for %s', doi)
        return None
