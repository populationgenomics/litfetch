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

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

import httpx

from litfetch import _http, ids, semantic_scholar

logger = logging.getLogger(__name__)

_EUROPE_PMC_BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
_NCBI_IDCONV_BASE = 'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/'
_OPENALEX_WORKS = 'https://api.openalex.org/works'

# A resolver enriches a bundle: given the known ids and an Http, it returns a
# (possibly) fuller ArticleIds and preserves every identifier it was given.
Resolver = Callable[[ids.ArticleIds, _http.Http], Awaitable[ids.ArticleIds]]

# A batch resolver enriches a whole sequence in one pass, amortizing a source's
# per-source rate domain across N papers.  It returns the enriched sequence
# (element i is input element i, merged -- length and order preserved) and the
# set of indices whose lookup was *abandoned* after retry-exhaustion: still
# un-answered, distinct from a definitive no-match.  A caller retries only the
# abandoned slice rather than re-running the whole batch.  The failure signal
# rides this tuple, never the ArticleIds value object (which stays str | None).
BatchResolver = Callable[
    [Sequence[ids.ArticleIds], _http.Http],
    Awaitable[tuple[Sequence[ids.ArticleIds], set[int]]],
]

_ID_FIELDS = frozenset(field.name for field in dataclasses.fields(ids.ArticleIds))


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
        logger.warning('%s returned a non-JSON response', context)
        return None


class _ChunkAbandonedError(Exception):
    """A batch chunk's lookup was given up on after retry-exhaustion.

    Distinct from a definitive no-match: the source never answered (a transport
    failure, or a 429/5xx that survived every retry), so retrying the chunk later
    may still resolve it.  :func:`_run_chunked` catches this and marks the
    chunk's keys abandoned rather than treating them as absences.
    """


def _as_str(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None`` (JSON is untyped)."""
    return value if isinstance(value, str) and value else None


async def _get_json_or_abandon(
    http: _http.Http,
    url: str,
    *,
    params: Mapping[str, str | int],
    context: str,
    rate: _http.Rate,
) -> dict | None:
    """GET and parse JSON like :func:`_get_json`, but *raise* on abandonment.

    A transport failure or a retryable status (429/5xx) that outlived every retry
    means the source never answered: raise :class:`_ChunkAbandonedError` so the batch
    marks the chunk retryable.  A non-retryable non-200 or non-JSON body is a
    definitive dead end -- logged and returned as ``None`` (no enrichment, not
    abandoned).
    """
    try:
        resp = await http.get(url, params=params, rate=rate)
    except httpx.HTTPError as e:
        raise _ChunkAbandonedError(f'{context}: transport failure') from e
    if resp.status_code in _http.RETRYABLE_STATUS:  # survived retries: never answered
        raise _ChunkAbandonedError(f'{context}: HTTP {resp.status_code} after retries')
    if resp.status_code != 200:
        logger.warning('%s returned HTTP %d', context, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning('%s returned a non-JSON response', context)
        return None


async def _run_chunked(
    article_ids: Sequence[ids.ArticleIds],
    *,
    key: Callable[[ids.ArticleIds], str | None],
    cap: int,
    resolve_chunk: Callable[[Sequence[str], _http.Http], Awaitable[Mapping[str, ids.ArticleIds]]],
    http: _http.Http,
) -> tuple[list[ids.ArticleIds], set[int]]:
    """Resolve a batch through a per-chunk mapping call, deduping ids on the wire.

    ``key`` yields the wire identifier for an element, or ``None`` when the source
    cannot key on it (passed through untouched, never queried).  Distinct keys are
    chunked at ``cap`` and each chunk handed to ``resolve_chunk`` (gathered; the
    Session paces per host), which returns ``{wire id: enrichment}`` for whatever
    it mapped and raises :class:`_ChunkAbandonedError` for a chunk the source never
    answered.  Each result fans back to *every* index sharing its key, so a batch
    with repeats costs one lookup per distinct id; order and length are preserved.

    Returns:
        The enriched sequence and the set of indices whose chunk was abandoned
        (and which therefore stayed un-enriched by this resolver).
    """
    keys = [key(item) for item in article_ids]
    distinct = list(dict.fromkeys(k for k in keys if k))
    if not distinct:
        return list(article_ids), set()
    chunks = [distinct[i : i + cap] for i in range(0, len(distinct), cap)]

    async def _resolve(chunk: Sequence[str]) -> tuple[Sequence[str], Mapping[str, ids.ArticleIds] | None]:
        try:
            return chunk, await resolve_chunk(chunk, http)
        except _ChunkAbandonedError:
            logger.warning('batch chunk abandoned (%d ids)', len(chunk))
            return chunk, None

    mapping: dict[str, ids.ArticleIds] = {}
    abandoned_keys: set[str] = set()
    for chunk, resolved in await asyncio.gather(*(_resolve(chunk) for chunk in chunks)):
        if resolved is None:
            abandoned_keys.update(chunk)
        else:
            mapping.update(resolved)

    enriched: list[ids.ArticleIds] = []
    abandoned: set[int] = set()
    for index, (item, k) in enumerate(zip(article_ids, keys, strict=True)):
        if k is not None and k in mapping:
            enriched.append(item.merge(mapping[k]))
        else:
            enriched.append(item)
            if k is not None and k in abandoned_keys:
                abandoned.add(index)
    return enriched, abandoned


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


def _ncbi_record_to_ids(rec: Mapping[str, object]) -> ids.ArticleIds:
    """Map one NCBI ID Converter record to an :class:`~litfetch.ids.ArticleIds`.

    The one source of truth for the NCBI record shape, shared by the per-item and
    batch paths.  ``pmid`` comes back as an int on the migrated endpoint;
    ``ArticleIds`` holds strings.
    """
    pmid = rec.get('pmid')
    return ids.ArticleIds(
        pmid=str(pmid) if pmid else None,
        pmcid=_pmcid_with_prefix(_as_str(rec.get('pmcid'))),
        doi=_as_str(rec.get('doi')),
    )


def _ncbi_batch_key(article_ids: ids.ArticleIds) -> str | None:
    """The wire id an id-type-less NCBI batch auto-detects for this element.

    Mirrors :func:`_idconv_query`'s pmid > pmcid > doi priority, but normalises a
    PMCID to its ``PMC...`` form: without an explicit ``idtype`` a bare number
    would be misdetected as a PMID.
    """
    return article_ids.pmid or _pmcid_with_prefix(article_ids.pmcid) or article_ids.doi


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
        return article_ids.merge(_ncbi_record_to_ids(records[0]))


class NcbiIdConverterBatchResolver:
    """Batch ``pmid``/``pmcid``/``doi`` cross-referencing via NCBI's ID Converter.

    Same endpoint and record shape as :class:`NcbiIdConverterResolver`; only the
    fan-in differs.  The batch request **omits** ``idtype`` so the converter
    auto-detects each id's scheme, letting a mixed-scheme batch (some DOIs, some
    PMIDs) resolve in one call.  The wire list is deduped and chunked at the
    converter's 200-id cap.
    """

    _CAP = 200

    def __init__(self, *, tool: str = 'litfetch') -> None:
        self._tool = tool

    async def __call__(
        self, article_ids: Sequence[ids.ArticleIds], http: _http.Http
    ) -> tuple[list[ids.ArticleIds], set[int]]:
        """Return the batch enriched with whatever the ID Converter maps, and abandoned indices."""
        return await _run_chunked(
            article_ids, key=_ncbi_batch_key, cap=self._CAP, resolve_chunk=self._resolve_chunk, http=http
        )

    async def _resolve_chunk(self, wire_ids: Sequence[str], http: _http.Http) -> Mapping[str, ids.ArticleIds]:
        """Map one chunk of wire ids to ``{wire id: enrichment}``, keyed by echoed id."""
        params: dict[str, str | int] = {'ids': ','.join(wire_ids), 'format': 'json', 'tool': self._tool}
        if http.contact:
            params['email'] = http.contact
        data = await _get_json_or_abandon(
            http, _NCBI_IDCONV_BASE, params=params, context='NCBI ID Converter batch', rate=_http.Rate.NCBI_UNKEYED
        )
        if data is None:
            return {}
        mapping: dict[str, ids.ArticleIds] = {}
        for rec in data.get('records', []):
            if rec.get('status') == 'error':
                continue
            enrichment = _ncbi_record_to_ids(rec)
            # Index under every echoed id form so an element keyed on any of them fans out.
            for form in (enrichment.pmid, enrichment.pmcid, enrichment.doi):
                if form is not None:
                    mapping[form] = enrichment
        return mapping


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


def _openalex_key(article_ids: ids.ArticleIds) -> str | None:
    """The wire filter key for an element: its DOI, lowercased (OpenAlex is doi-keyed).

    OpenAlex stores and echoes DOIs lowercased, so lowercasing here makes the
    query key, the wire filter, and the result correlation all agree.  A paper
    with no DOI is passed through untouched -- OpenAlex has nothing to key on.
    """
    return article_ids.doi.lower() if article_ids.doi else None


def _openalex_bare_doi(value: object) -> str | None:
    """Strip an OpenAlex DOI URL (``https://doi.org/10.x/y``) to the bare, lowercased id."""
    doi = _as_str(value)
    if doi is None:
        return None
    _, _, rest = doi.partition('doi.org/')
    return (rest or doi).lower()


def _openalex_last_segment(value: object) -> str | None:
    """Strip an OpenAlex id URL to its final path segment (``.../PMC123`` -> ``PMC123``)."""
    url = _as_str(value)
    if url is None:
        return None
    return url.rsplit('/', 1)[-1] or None


class OpenAlexResolver:
    """Resolve ``doi -> pmid``/``pmcid`` via OpenAlex's works endpoint.

    Batch, keyless, and strictly id->id: the works filter takes a
    ``doi:<a>|<b>|...`` OR-list and each work's ``ids`` carries its pmid/pmcid, so
    one request maps up to 50 DOIs.  ``select`` is restricted to the id fields, so
    no bibliographic record crosses litfetch's id surface.  It covers the
    doi-bearing papers NCBI could not route (a DOI in PubMed-but-not-PMC, or not
    in PubMed).  The session ``contact`` is sent as ``mailto`` (the polite-pool
    parameter), omitted when unset.
    """

    _CAP = 50

    async def __call__(
        self, article_ids: Sequence[ids.ArticleIds], http: _http.Http
    ) -> tuple[list[ids.ArticleIds], set[int]]:
        """Return the batch enriched with pmid/pmcid for DOIs OpenAlex knows, and abandoned indices."""
        return await _run_chunked(
            article_ids, key=_openalex_key, cap=self._CAP, resolve_chunk=self._resolve_chunk, http=http
        )

    async def _resolve_chunk(self, dois: Sequence[str], http: _http.Http) -> Mapping[str, ids.ArticleIds]:
        """Map one chunk of DOIs to ``{bare doi: enrichment}`` from the works response."""
        params: dict[str, str | int] = {
            'filter': 'doi:' + '|'.join(dois),
            'select': 'ids,doi',
            'per-page': len(dois),
        }
        if http.contact:
            params['mailto'] = http.contact
        data = await _get_json_or_abandon(
            http, _OPENALEX_WORKS, params=params, context='OpenAlex works', rate=_http.Rate.OPENALEX
        )
        if data is None:
            return {}
        mapping: dict[str, ids.ArticleIds] = {}
        for work in data.get('results', []):
            doi_key = _openalex_bare_doi(work.get('doi'))
            if doi_key is None:
                continue
            work_ids = work.get('ids') or {}
            mapping[doi_key] = ids.ArticleIds(
                pmid=_openalex_last_segment(work_ids.get('pmid')),
                pmcid=_pmcid_with_prefix(_openalex_last_segment(work_ids.get('pmcid'))),
            )
        return mapping


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


def chain_batch(
    *resolvers: BatchResolver,
    required: Iterable[str] = ('pmid', 'pmcid', 'doi'),
) -> BatchResolver:
    """Compose batch resolvers, each fed only the elements still missing a ``required`` field.

    The batch analogue of :func:`chain`'s early-stop, expressed per-element: an
    element complete on ``required`` passes through untouched and keeps its
    index, so a later resolver spends its rate budget only on what earlier ones
    left incomplete (``NcbiIdConverterBatchResolver`` first, ``OpenAlexResolver``
    only for the DOIs NCBI missed).

    ``required`` is parameterizable because a caller resolving for the PMC ladder
    needs only ``pmcid``; forcing all three would spend calls chasing a
    ``doi``/``pmid`` the ladder never keys on.

    The returned abandoned set holds an index iff the element is *still*
    incomplete on ``required`` **and** some resolver abandoned it after
    retry-exhaustion.  An element a later resolver completed is dropped from the
    set; one every resolver answered with a definitive no-match never enters it
    (a genuine absence, not worth retrying).

    Args:
        *resolvers: Batch resolvers run in order.
        required: Identifier fields an element must carry to be considered
            complete and skipped by later resolvers.

    Returns:
        A :data:`BatchResolver` over the composed chain.

    Raises:
        ValueError: If ``required`` is empty or names a field that is not an
            :class:`~litfetch.ids.ArticleIds` identifier.
    """
    required = tuple(required)
    if not required:
        raise ValueError('required must name at least one identifier field')
    unknown = set(required) - _ID_FIELDS
    if unknown:
        raise ValueError(f'required names unknown identifier field(s): {sorted(unknown)}')

    async def _run(
        article_ids: Sequence[ids.ArticleIds], http: _http.Http
    ) -> tuple[Sequence[ids.ArticleIds], set[int]]:
        results = list(article_ids)
        abandoned: set[int] = set()
        for resolver in resolvers:
            pending = [i for i, item in enumerate(results) if not item.has(required)]
            if not pending:
                break
            enriched, sub_abandoned = await resolver([results[i] for i in pending], http)
            for position, index in enumerate(pending):
                results[index] = results[index].merge(enriched[position])
                if position in sub_abandoned:
                    abandoned.add(index)
        # An element completed by a later resolver is no longer abandoned.
        return results, {index for index in abandoned if not results[index].has(required)}

    return _run


def _idconv_query(article_ids: ids.ArticleIds) -> tuple[str, str] | None:
    """Pick the identifier and ``idtype`` to send to NCBI's ID Converter."""
    if article_ids.pmid:
        return article_ids.pmid, 'pmid'
    if article_ids.pmcid:
        return article_ids.pmcid, 'pmcid'
    if article_ids.doi:
        return article_ids.doi, 'doi'
    return None
