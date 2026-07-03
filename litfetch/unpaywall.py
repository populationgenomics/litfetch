"""Unpaywall record fetch, shared by access resolution and the file-set.

One DOI-keyed GET returns a record that yields two things litfetch wants: the
licence / OA status (:mod:`litfetch.source_metadata`) and the best OA location's
PDF URL (a ``BODY`` file-set rendition, via
:class:`~litfetch.fetchers.UnpaywallFileSource`).  Both callers go through
:func:`fetch_record`; inside a :meth:`~litfetch.sessions.Session.scope` the
duplicate GET is served from cache rather than hitting Unpaywall twice.
"""

from __future__ import annotations

import logging

import httpx

from litfetch import _doi, _http, ids

logger = logging.getLogger(__name__)

_UNPAYWALL_BASE = 'https://api.unpaywall.org/v2'


async def fetch_record(
    article_ids: ids.ArticleIds,
    *,
    http: _http.Http,
    email: str = _http.CONTACT_EMAIL,
) -> dict | None:
    """Return the parsed Unpaywall record for ``article_ids.doi``.

    Args:
        article_ids: The identifiers; only the DOI is used.
        http: The :class:`~litfetch._http.Http` to issue the request on.
        email: Identifies the caller per Unpaywall's usage policy.

    Returns:
        The parsed JSON record, or ``None`` when there is no DOI, the lookup
        fails, or Unpaywall has no record.
    """
    if not article_ids.doi:
        return None
    url = f'{_UNPAYWALL_BASE}/{_doi.encode_doi_path(article_ids.doi)}'
    try:
        resp = await http.get(url, params={'email': email})
    except httpx.HTTPError:
        logger.exception('Unpaywall request failed for %s', article_ids.doi)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None
