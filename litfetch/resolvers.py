"""Identifier resolvers: enrich an :class:`~litfetch.ids.ArticleIds` bundle.

A resolver is any async callable ``(ArticleIds, Http) -> ArticleIds`` (the
:data:`Resolver` alias).  It takes what is known and the
:class:`~litfetch._http.Http` to issue requests on, and returns a bundle filled
with whatever more it could find; it must never overwrite a known identifier
(use :meth:`~litfetch.ids.ArticleIds.merge`).  Resolvers are usable on their
own as a cross-reference toolkit, independent of the fetch ladder::

    async with litfetch.Session() as s:
        ids = await SemanticScholarResolver()(ArticleIds(doi='10.1016/...'), s)
    print(ids.pmcid)

The bundled resolvers are general (no pubmedifier coupling): Europe PMC search,
NCBI's ID Converter, and Semantic Scholar.  Consumer-specific resolvers (a
local cache, a corpus client) belong in the consumer and slot into the same
:func:`chain`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx

from litfetch import _http, ids, semantic_scholar

logger = logging.getLogger(__name__)

_EUROPE_PMC_BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
_NCBI_IDCONV_BASE = 'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/'

# A resolver enriches a bundle: given the known ids and an Http, it returns a
# (possibly) fuller ArticleIds and preserves every identifier it was given.
Resolver = Callable[[ids.ArticleIds, _http.Http], Awaitable[ids.ArticleIds]]


def _pmcid_with_prefix(value: str | None) -> str | None:
    """Normalise a bare or prefixed PMC id to the ``PMC...`` form."""
    if not value:
        return None
    value = value.strip()
    if value.upper().startswith('PMC'):
        return value
    return f'PMC{value}'


async def _get_json(
    http: _http.Http,
    url: str,
    *,
    params: Mapping[str, str | int],
    context: str,
    rate: _http.Rate = _http.Rate.DEFAULT,
) -> dict | None:
    """GET ``url`` and parse JSON, logging and swallowing transport errors."""
    try:
        resp = await http.get(url, params=params, rate=rate)
    except httpx.HTTPError:
        logger.exception('%s request failed', context)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


class EuropePmcResolver:
    """Resolve ``pmid -> pmcid`` via Europe PMC's search API.

    Europe PMC occasionally records a PMC id for an article a PubMed-XML-sourced
    corpus does not -- typically UKPMC-only author-manuscript deposits.  A no-op
    when the bundle already has a ``pmcid`` or has no ``pmid``.
    """

    async def __call__(self, article_ids: ids.ArticleIds, http: _http.Http) -> ids.ArticleIds:
        """Return ``article_ids`` enriched with a ``pmcid`` where Europe PMC has one."""
        if article_ids.pmcid or not article_ids.pmid:
            return article_ids
        params = {
            'query': f'EXT_ID:{article_ids.pmid} AND SRC:MED',
            'format': 'json',
            'pageSize': 1,
            'resultType': 'lite',
        }
        data = await _get_json(http, f'{_EUROPE_PMC_BASE}/search', params=params, context='Europe PMC search')
        if data is None:
            return article_ids
        records = data.get('resultList', {}).get('result', [])
        if not records:
            return article_ids
        return article_ids.merge(ids.ArticleIds(pmcid=_pmcid_with_prefix(records[0].get('pmcid'))))


class NcbiIdConverterResolver:
    """Cross-reference ``pmid``/``pmcid``/``doi`` via NCBI's ID Converter.

    A single keyless request maps any one of the three identifiers to the
    others.  ``tool`` identifies the caller to NCBI; the ``email`` sent with it
    defaults to the session ``contact`` (``http.contact``) and is omitted when
    unset.  A no-op when the bundle carries none of the three.
    """

    def __init__(self, *, tool: str = 'litfetch') -> None:
        self._tool = tool

    async def __call__(self, article_ids: ids.ArticleIds, http: _http.Http) -> ids.ArticleIds:
        """Return ``article_ids`` enriched with whatever the ID Converter maps."""
        query = _idconv_query(article_ids)
        if query is None:
            return article_ids
        identifier, idtype = query
        params = {'ids': identifier, 'idtype': idtype, 'format': 'json', 'tool': self._tool}
        if http.contact:
            params['email'] = http.contact
        data = await _get_json(
            http, _NCBI_IDCONV_BASE, params=params, context='NCBI ID Converter', rate=_http.Rate.NCBI_UNKEYED
        )
        if data is None:
            return article_ids
        records = data.get('records', [])
        if not records or records[0].get('status') == 'error':
            return article_ids
        rec = records[0]
        return article_ids.merge(
            ids.ArticleIds(
                # The migrated endpoint returns pmid as an int; ArticleIds holds strings.
                pmid=(str(rec['pmid']) if rec.get('pmid') else None),
                pmcid=_pmcid_with_prefix(rec.get('pmcid')),
                doi=rec.get('doi') or None,
            )
        )


class SemanticScholarResolver:
    """Cross-reference identifiers via Semantic Scholar's ``externalIds``.

    One lookup returns DOI / PubMed / PubMedCentral / arXiv ids for the paper.
    ``api_key`` is optional (the public endpoint is rate-limited but keyless).
    A no-op when the bundle carries no identifier S2 can key on.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def __call__(self, article_ids: ids.ArticleIds, http: _http.Http) -> ids.ArticleIds:
        """Return ``article_ids`` enriched from Semantic Scholar's external ids."""
        data = await semantic_scholar.fetch_paper(article_ids, http=http, fields='externalIds', api_key=self._api_key)
        if data is None:
            return article_ids
        external = data.get('externalIds') or {}
        return article_ids.merge(
            ids.ArticleIds(
                pmid=(str(external['PubMed']) if external.get('PubMed') else None),
                pmcid=_pmcid_with_prefix(external.get('PubMedCentral')),
                doi=external.get('DOI') or None,
            )
        )


def chain(*resolvers: Resolver) -> Resolver:
    """Compose resolvers into one, run in order until the bundle is complete.

    Each resolver enriches the bundle in turn; the chain stops early once every
    identifier (``pmid``, ``pmcid``, ``doi``) is known, so later resolvers run
    only while there is still something to find.
    """

    async def _run(article_ids: ids.ArticleIds, http: _http.Http) -> ids.ArticleIds:
        for resolver in resolvers:
            if article_ids.pmid and article_ids.pmcid and article_ids.doi:
                break
            article_ids = article_ids.merge(await resolver(article_ids, http))
        return article_ids

    return _run


def default_resolver() -> Resolver:
    """Build a batteries-included, keyless resolver chain.

    Europe PMC search then NCBI's ID Converter -- both auth-free -- which covers
    the common ``pmid -> pmcid``/``doi`` paths.  Add
    :class:`SemanticScholarResolver` (or a consumer's own resolver) to the
    :func:`chain` for broader coverage.
    """
    return chain(EuropePmcResolver(), NcbiIdConverterResolver())


def _idconv_query(article_ids: ids.ArticleIds) -> tuple[str, str] | None:
    """Pick the identifier and ``idtype`` to send to NCBI's ID Converter."""
    if article_ids.pmid:
        return article_ids.pmid, 'pmid'
    if article_ids.pmcid:
        return article_ids.pmcid, 'pmcid'
    if article_ids.doi:
        return article_ids.doi, 'doi'
    return None
