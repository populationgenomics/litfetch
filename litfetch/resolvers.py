"""Identifier resolvers: enrich an :class:`~litfetch.ids.ArticleIds` bundle.

A resolver is any async callable ``ArticleIds -> ArticleIds`` (the
:data:`Resolver` alias).  It takes what is known and returns a bundle filled
with whatever more it could find; it must never overwrite a known identifier
(use :meth:`~litfetch.ids.ArticleIds.merge`).  Resolvers are usable on their
own as a cross-reference toolkit, independent of the fetch ladder::

    ids = await SemanticScholarResolver()(ArticleIds(doi='10.1016/...'))
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

from litfetch._http import DEFAULT_TIMEOUT, USER_AGENT, client_ctx
from litfetch.ids import ArticleIds

logger = logging.getLogger(__name__)

_CONTACT_EMAIL = 'toby.sargeant@populationgenomics.org.au'
_EUROPE_PMC_BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
_NCBI_IDCONV_BASE = 'https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/'
_S2_PAPER_BASE = 'https://api.semanticscholar.org/graph/v1/paper'

# A resolver enriches a bundle: it returns a (possibly) fuller ArticleIds and
# preserves every identifier it was given.
Resolver = Callable[[ArticleIds], Awaitable[ArticleIds]]


def _pmcid_with_prefix(value: str | None) -> str | None:
    """Normalise a bare or prefixed PMC id to the ``PMC...`` form."""
    if not value:
        return None
    value = value.strip()
    if value.upper().startswith('PMC'):
        return value
    return f'PMC{value}'


async def _get_json(
    c: httpx.AsyncClient,
    url: str,
    *,
    params: Mapping[str, str | int],
    context: str,
) -> dict | None:
    """GET ``url`` and parse JSON, logging and swallowing transport errors."""
    try:
        resp = await c.get(url, params=params, headers={'User-Agent': USER_AGENT})
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

    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._http_client = http_client
        self._timeout = timeout

    async def __call__(self, ids: ArticleIds) -> ArticleIds:
        """Return ``ids`` enriched with a ``pmcid`` where Europe PMC has one."""
        if ids.pmcid or not ids.pmid:
            return ids
        params = {
            'query': f'EXT_ID:{ids.pmid} AND SRC:MED',
            'format': 'json',
            'pageSize': 1,
            'resultType': 'lite',
        }
        async with client_ctx(self._http_client, timeout=self._timeout) as c:
            data = await _get_json(c, f'{_EUROPE_PMC_BASE}/search', params=params, context='Europe PMC search')
        if data is None:
            return ids
        records = data.get('resultList', {}).get('result', [])
        if not records:
            return ids
        return ids.merge(ArticleIds(pmcid=_pmcid_with_prefix(records[0].get('pmcid'))))


class NcbiIdConverterResolver:
    """Cross-reference ``pmid``/``pmcid``/``doi`` via NCBI's ID Converter.

    A single keyless request maps any one of the three identifiers to the
    others.  ``tool`` and ``email`` identify the caller to NCBI per its usage
    policy.  A no-op when the bundle carries none of the three.
    """

    def __init__(
        self,
        *,
        tool: str = 'litfetch',
        email: str = _CONTACT_EMAIL,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._tool = tool
        self._email = email
        self._http_client = http_client
        self._timeout = timeout

    async def __call__(self, ids: ArticleIds) -> ArticleIds:
        """Return ``ids`` enriched with whatever the ID Converter maps."""
        query = _idconv_query(ids)
        if query is None:
            return ids
        identifier, idtype = query
        params = {
            'ids': identifier,
            'idtype': idtype,
            'format': 'json',
            'tool': self._tool,
            'email': self._email,
        }
        async with client_ctx(self._http_client, timeout=self._timeout) as c:
            data = await _get_json(c, _NCBI_IDCONV_BASE, params=params, context='NCBI ID Converter')
        if data is None:
            return ids
        records = data.get('records', [])
        if not records or records[0].get('status') == 'error':
            return ids
        rec = records[0]
        return ids.merge(
            ArticleIds(
                pmid=rec.get('pmid') or None,
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

    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._http_client = http_client
        self._timeout = timeout

    async def __call__(self, ids: ArticleIds) -> ArticleIds:
        """Return ``ids`` enriched from Semantic Scholar's external ids."""
        paper_id = _s2_paper_id(ids)
        if paper_id is None:
            return ids
        headers = {'User-Agent': USER_AGENT}
        if self._api_key:
            headers['x-api-key'] = self._api_key
        async with client_ctx(self._http_client, timeout=self._timeout) as c:
            try:
                resp = await c.get(
                    f'{_S2_PAPER_BASE}/{paper_id}',
                    params={'fields': 'externalIds'},
                    headers=headers,
                )
            except httpx.HTTPError:
                logger.exception('Semantic Scholar request failed')
                return ids
        if resp.status_code != 200:
            return ids
        try:
            external = resp.json().get('externalIds') or {}
        except ValueError:
            return ids
        return ids.merge(
            ArticleIds(
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

    async def _run(ids: ArticleIds) -> ArticleIds:
        for resolver in resolvers:
            if ids.pmid and ids.pmcid and ids.doi:
                break
            ids = ids.merge(await resolver(ids))
        return ids

    return _run


def default_resolver() -> Resolver:
    """Build a batteries-included, keyless resolver chain.

    Europe PMC search then NCBI's ID Converter -- both auth-free -- which covers
    the common ``pmid -> pmcid``/``doi`` paths.  Add
    :class:`SemanticScholarResolver` (or a consumer's own resolver) to the
    :func:`chain` for broader coverage.
    """
    return chain(EuropePmcResolver(), NcbiIdConverterResolver())


def _idconv_query(ids: ArticleIds) -> tuple[str, str] | None:
    """Pick the identifier and ``idtype`` to send to NCBI's ID Converter."""
    if ids.pmid:
        return ids.pmid, 'pmid'
    if ids.pmcid:
        return ids.pmcid, 'pmcid'
    if ids.doi:
        return ids.doi, 'doi'
    return None


def _s2_paper_id(ids: ArticleIds) -> str | None:
    """Build a Semantic Scholar paper id from the most specific id available."""
    if ids.doi:
        return f'DOI:{ids.doi}'
    if ids.pmid:
        return f'PMID:{ids.pmid}'
    if ids.pmcid:
        return f'PMCID:{ids.pmcid}'
    return None
