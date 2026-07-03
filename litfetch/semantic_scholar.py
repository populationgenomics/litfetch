"""Semantic Scholar paper fetch, shared by identifier resolution and the file-set.

One paper lookup returns whichever fields are asked for: ``externalIds`` (for
:class:`~litfetch.resolvers.SemanticScholarResolver`'s cross-referencing) or
``openAccessPdf`` (for a ``BODY`` PDF rendition, via
:class:`~litfetch.fetchers.SemanticScholarFileSource`).  Both go through
:func:`fetch_paper`; the paper id is built from the most specific identifier the
bundle carries.  ``api_key`` is optional -- the public endpoint is keyless but
rate-limited -- and selects the keyed vs unkeyed pace.
"""

from __future__ import annotations

import logging

import httpx

from litfetch import _doi, _http, ids

logger = logging.getLogger(__name__)

_PAPER_BASE = 'https://api.semanticscholar.org/graph/v1/paper'


def paper_id(article_ids: ids.ArticleIds) -> str | None:
    """Build an S2 paper id from the most specific identifier available."""
    if article_ids.doi:
        return f'DOI:{_doi.encode_doi_path(article_ids.doi)}'
    if article_ids.pmid:
        return f'PMID:{article_ids.pmid}'
    if article_ids.pmcid:
        return f'PMCID:{article_ids.pmcid}'
    return None


async def fetch_paper(
    article_ids: ids.ArticleIds,
    *,
    http: _http.Http,
    fields: str,
    api_key: str | None = None,
) -> dict | None:
    """Return the parsed S2 record for ``fields``, or ``None``.

    Args:
        article_ids: The identifiers; the most specific keys the request.
        http: The :class:`~litfetch._http.Http` to issue the request on.
        fields: The S2 ``fields`` selector (e.g. ``'externalIds'``).
        api_key: An optional S2 API key; its presence selects the keyed pace.

    Returns:
        The parsed JSON record, or ``None`` when the bundle carries no id S2 can
        key on, the lookup fails, or the response is not JSON.
    """
    pid = paper_id(article_ids)
    if pid is None:
        return None
    headers = {'x-api-key': api_key} if api_key else None
    rate = _http.Rate.S2_KEYED if api_key else _http.Rate.S2_UNKEYED
    try:
        resp = await http.get(f'{_PAPER_BASE}/{pid}', params={'fields': fields}, headers=headers, rate=rate)
    except httpx.HTTPError:
        logger.exception('Semantic Scholar request failed')
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None
