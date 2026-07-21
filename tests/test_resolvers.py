"""Tests for the bundled identifier resolvers and chaining."""

from __future__ import annotations

import httpx
import pytest

from litfetch import _http, ids, resolvers, sessions
from tests import conftest

_EPMC_SEARCH_PATH = '/europepmc/webservices/rest/search'
_IDCONV_PATH = '/tools/idconv/api/v1/articles/'
_DOI = '10.1016/j.test.2024.01.001'


class _NoHttp:
    """An Http stub that fails if used -- for resolver tests that make no request."""

    contact: str | None = None

    async def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError('no request expected')


# --- Europe PMC resolver -------------------------------------------------


async def test_europe_pmc_resolver_resolves_pmid_to_pmcid(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': [{'pmcid': 'PMC9'}]}})],
        }
    )
    async with sessions.Session() as s:
        result = await resolvers.EuropePmcResolver()(ids.ArticleIds(pmid='9'), s)
    assert result == ids.ArticleIds(pmid='9', pmcid='PMC9')


async def test_europe_pmc_resolver_noop_when_pmcid_present() -> None:
    article_ids = ids.ArticleIds(pmid='9', pmcid='PMC9')
    assert await resolvers.EuropePmcResolver()(article_ids, _NoHttp()) == article_ids


async def test_europe_pmc_resolver_noop_without_pmid() -> None:
    article_ids = ids.ArticleIds(doi=_DOI)
    assert await resolvers.EuropePmcResolver()(article_ids, _NoHttp()) == article_ids


async def test_europe_pmc_resolver_returns_input_on_empty_result(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': []}})]})
    article_ids = ids.ArticleIds(pmid='9')
    async with sessions.Session() as s:
        assert await resolvers.EuropePmcResolver()(article_ids, s) == article_ids


# --- NCBI ID Converter resolver ------------------------------------------


async def test_ncbi_resolver_maps_pmid_to_pmcid_and_doi(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_IDCONV_PATH}': [
                # The live endpoint returns pmid as an int; the resolver must coerce it to str.
                httpx.Response(200, json={'records': [{'pmid': 9, 'pmcid': 'PMC9', 'doi': _DOI}]})
            ],
        }
    )
    async with sessions.Session() as s:
        result = await resolvers.NcbiIdConverterResolver()(ids.ArticleIds(pmid='9'), s)
    assert result == ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)
    assert transport.calls[0][2] is None  # GET, no body


async def test_ncbi_resolver_noop_on_error_record(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_IDCONV_PATH}': [httpx.Response(200, json={'records': [{'pmid': '9', 'status': 'error'}]})],
        }
    )
    article_ids = ids.ArticleIds(pmid='9')
    async with sessions.Session() as s:
        assert await resolvers.NcbiIdConverterResolver()(article_ids, s) == article_ids


async def test_ncbi_resolver_noop_without_any_id() -> None:
    assert await resolvers.NcbiIdConverterResolver()(ids.ArticleIds(), _NoHttp()) == ids.ArticleIds()


# --- Semantic Scholar resolver -------------------------------------------


async def test_s2_resolver_enriches_from_external_ids(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            'GET /graph/v1/paper/PMID:9': [
                httpx.Response(200, json={'externalIds': {'DOI': _DOI, 'PubMedCentral': '9', 'PubMed': 9}}),
            ],
        }
    )
    async with sessions.Session() as s:
        result = await resolvers.SemanticScholarResolver()(ids.ArticleIds(pmid='9'), s)
    assert result == ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)


async def test_s2_resolver_noop_without_any_id() -> None:
    assert await resolvers.SemanticScholarResolver()(ids.ArticleIds(), _NoHttp()) == ids.ArticleIds()


# --- chain ---------------------------------------------------------------


class _Fixed:
    def __init__(self, returns: ids.ArticleIds) -> None:
        self._returns = returns
        self.calls = 0

    async def __call__(self, article_ids: ids.ArticleIds, http: object) -> ids.ArticleIds:
        del http  # a fixed resolver makes no request
        self.calls += 1
        return article_ids.merge(self._returns)


async def test_chain_runs_resolvers_in_order() -> None:
    composed = resolvers.chain(_Fixed(ids.ArticleIds(pmcid='PMC1')), _Fixed(ids.ArticleIds(doi=_DOI)))
    assert await composed(ids.ArticleIds(pmid='1'), _NoHttp()) == ids.ArticleIds(pmid='1', pmcid='PMC1', doi=_DOI)


async def test_chain_stops_early_once_complete() -> None:
    complete = _Fixed(ids.ArticleIds(pmcid='PMC1', doi=_DOI))
    spy = _Fixed(ids.ArticleIds(pmid='other'))
    result = await resolvers.chain(complete, spy)(ids.ArticleIds(pmid='1'), _NoHttp())
    assert result == ids.ArticleIds(pmid='1', pmcid='PMC1', doi=_DOI)
    assert complete.calls == 1
    assert spy.calls == 0


async def test_default_resolver_composes_europe_pmc_then_ncbi(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': [{'pmcid': 'PMC9'}]}})],
            f'GET {_IDCONV_PATH}': [
                httpx.Response(200, json={'records': [{'pmid': '9', 'pmcid': 'PMC9', 'doi': _DOI}]})
            ],
        }
    )
    async with sessions.Session() as s:
        resolved = await resolvers.default_resolver()(ids.ArticleIds(pmid='9'), s)
    assert resolved == ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)


# --- chain_batch ---------------------------------------------------------


class _BatchStub:
    """A batch resolver stub: enrich each keyed element, optionally abandon some.

    ``fill`` is merged into every element the stub is given; ``abandon`` names
    input keys (any present identifier) whose position is reported abandoned.
    Records each call's received sequence for order/subset assertions.
    """

    def __init__(self, fill: ids.ArticleIds, *, abandon: frozenset[str] = frozenset()) -> None:
        self._fill = fill
        self._abandon = abandon
        self.received: list[list[ids.ArticleIds]] = []

    async def __call__(self, article_ids: object, http: object) -> tuple[list[ids.ArticleIds], set[int]]:
        del http
        batch = list(article_ids)  # type: ignore[arg-type]
        self.received.append(batch)
        enriched = [item.merge(self._fill) for item in batch]
        abandoned = {i for i, item in enumerate(batch) if {item.pmid, item.pmcid, item.doi} & self._abandon}
        return enriched, abandoned


async def test_chain_batch_preserves_order_and_length() -> None:
    composed = resolvers.chain_batch(_BatchStub(ids.ArticleIds(pmcid='PMCx')))
    resolved, abandoned = await composed([ids.ArticleIds(pmid='1'), ids.ArticleIds(pmid='2')], _NoHttp())
    assert resolved == [ids.ArticleIds(pmid='1', pmcid='PMCx'), ids.ArticleIds(pmid='2', pmcid='PMCx')]
    assert abandoned == set()


async def test_chain_batch_feeds_later_resolver_only_incomplete_elements() -> None:
    first = _BatchStub(ids.ArticleIds(pmcid='PMC1', doi=_DOI))
    second = _BatchStub(ids.ArticleIds(pmid='resolved'))
    # required defaults to all three: element 0 (pmid+pmcid+doi) is complete after `first`.
    composed = resolvers.chain_batch(first, second)
    await composed([ids.ArticleIds(pmid='0'), ids.ArticleIds(doi='only')], _NoHttp())
    assert first.received == [[ids.ArticleIds(pmid='0'), ids.ArticleIds(doi='only')]]
    # `second` sees only the still-incomplete element (index 1: doi-only, no pmid/pmcid).
    assert second.received == [[ids.ArticleIds(doi='only', pmcid='PMC1')]]


async def test_chain_batch_respects_required_subset() -> None:
    # pmid-only becomes pmcid-complete after one fill; the second stub must not see it.
    stub = _BatchStub(ids.ArticleIds(pmcid='PMCx'))
    second = _BatchStub(ids.ArticleIds(doi=_DOI))
    composed = resolvers.chain_batch(stub, second, required=('pmcid',))
    resolved, _ = await composed([ids.ArticleIds(pmid='1')], _NoHttp())
    assert resolved == [ids.ArticleIds(pmid='1', pmcid='PMCx')]
    assert second.received == []


async def test_chain_batch_reports_abandoned_index() -> None:
    stub = _BatchStub(ids.ArticleIds(), abandon=frozenset({'2'}))
    composed = resolvers.chain_batch(stub)
    resolved, abandoned = await composed([ids.ArticleIds(pmid='1'), ids.ArticleIds(pmid='2')], _NoHttp())
    assert resolved == [ids.ArticleIds(pmid='1'), ids.ArticleIds(pmid='2')]
    assert abandoned == {1}


async def test_chain_batch_clears_abandonment_when_later_resolver_completes() -> None:
    # `first` abandons the doi-only element; `second` resolves it to complete.
    first = _BatchStub(ids.ArticleIds(), abandon=frozenset({'only'}))
    second = _BatchStub(ids.ArticleIds(pmid='p', pmcid='PMCx'))
    composed = resolvers.chain_batch(first, second)
    resolved, abandoned = await composed([ids.ArticleIds(doi='only')], _NoHttp())
    assert resolved == [ids.ArticleIds(pmid='p', pmcid='PMCx', doi='only')]
    assert abandoned == set()


async def test_chain_batch_rejects_empty_required() -> None:
    with pytest.raises(ValueError, match='at least one'):
        resolvers.chain_batch(required=())


async def test_chain_batch_rejects_unknown_required_field() -> None:
    with pytest.raises(ValueError, match='unknown identifier'):
        resolvers.chain_batch(required=('pmid', 'issn'))


# --- _run_chunked / abandonment ------------------------------------------


class _StubHttp:
    """An Http returning one canned response, or raising one canned error."""

    contact: str | None = None

    def __init__(self, *, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


async def test_run_chunked_dedups_and_fans_out_to_repeats() -> None:
    seen: list[list[str]] = []

    async def resolve(chunk: object, _http_arg: object) -> dict[str, ids.ArticleIds]:
        keys = list(chunk)  # type: ignore[arg-type]
        seen.append(keys)
        return {k: ids.ArticleIds(pmcid=f'PMC{k}') for k in keys}

    items = [ids.ArticleIds(pmid='1'), ids.ArticleIds(pmid='2'), ids.ArticleIds(pmid='1')]
    enriched, abandoned = await resolvers._run_chunked(
        items, key=lambda a: a.pmid, cap=10, resolve_chunk=resolve, http=_NoHttp()
    )
    assert seen == [['1', '2']]  # '1' queried once despite appearing twice
    assert enriched[0] == ids.ArticleIds(pmid='1', pmcid='PMC1')
    assert enriched[2] == ids.ArticleIds(pmid='1', pmcid='PMC1')  # fanned back to the repeat
    assert abandoned == set()


async def test_run_chunked_splits_at_cap() -> None:
    seen: list[list[str]] = []

    async def resolve(chunk: object, _http_arg: object) -> dict[str, ids.ArticleIds]:
        seen.append(list(chunk))  # type: ignore[arg-type]
        return {}

    items = [ids.ArticleIds(pmid=str(i)) for i in range(5)]
    await resolvers._run_chunked(items, key=lambda a: a.pmid, cap=2, resolve_chunk=resolve, http=_NoHttp())
    assert [len(chunk) for chunk in seen] == [2, 2, 1]


async def test_run_chunked_marks_abandoned_chunk_indices() -> None:
    async def resolve(_chunk: object, _http_arg: object) -> dict[str, ids.ArticleIds]:
        raise resolvers._ChunkAbandonedError('boom')

    items = [ids.ArticleIds(pmid='1'), ids.ArticleIds(pmid='2')]
    enriched, abandoned = await resolvers._run_chunked(
        items, key=lambda a: a.pmid, cap=10, resolve_chunk=resolve, http=_NoHttp()
    )
    assert enriched == items  # un-enriched, never invented
    assert abandoned == {0, 1}


async def test_run_chunked_passes_through_unkeyable_elements() -> None:
    async def resolve(_chunk: object, _http_arg: object) -> dict[str, ids.ArticleIds]:
        raise AssertionError('no keyed element, no request expected')

    items = [ids.ArticleIds(doi='d'), ids.ArticleIds()]  # neither has a pmid to key on
    enriched, abandoned = await resolvers._run_chunked(
        items, key=lambda a: a.pmid, cap=10, resolve_chunk=resolve, http=_NoHttp()
    )
    assert enriched == items
    assert abandoned == set()


async def test_get_json_or_abandon_raises_on_retryable_status() -> None:
    http = _StubHttp(response=httpx.Response(429))
    with pytest.raises(resolvers._ChunkAbandonedError):
        await resolvers._get_json_or_abandon(http, 'http://x', params={}, context='c', rate=_http.Rate.DEFAULT)


async def test_get_json_or_abandon_raises_on_transport_error() -> None:
    http = _StubHttp(error=httpx.ConnectError('boom'))
    with pytest.raises(resolvers._ChunkAbandonedError):
        await resolvers._get_json_or_abandon(http, 'http://x', params={}, context='c', rate=_http.Rate.DEFAULT)


async def test_get_json_or_abandon_returns_none_on_non_retryable_status() -> None:
    http = _StubHttp(response=httpx.Response(404))
    result = await resolvers._get_json_or_abandon(http, 'http://x', params={}, context='c', rate=_http.Rate.DEFAULT)
    assert result is None  # definitive dead end, not abandoned


# --- NCBI ID Converter batch resolver ------------------------------------


async def test_ncbi_batch_resolver_maps_mixed_scheme_in_one_call(
    patch_transport: conftest.InstallTransport,
) -> None:
    transport = patch_transport(
        {
            f'GET {_IDCONV_PATH}': [
                httpx.Response(
                    200,
                    json={
                        'records': [
                            {'pmid': 9, 'pmcid': 'PMC9', 'doi': _DOI},
                            {'pmid': 10, 'doi': 'other-doi'},
                        ]
                    },
                )
            ],
        }
    )
    async with sessions.Session() as s:
        enriched, abandoned = await resolvers.NcbiIdConverterBatchResolver()(
            [ids.ArticleIds(pmid='9'), ids.ArticleIds(doi='other-doi')], s
        )
    assert enriched == [
        ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI),
        ids.ArticleIds(pmid='10', doi='other-doi'),
    ]
    assert abandoned == set()
    assert len(transport.calls) == 1  # one wire request for the whole batch
    assert 'idtype' not in transport.calls[0][1]  # auto-detect: no idtype sent


async def test_ncbi_batch_resolver_dedups_repeated_doi(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport(
        {f'GET {_IDCONV_PATH}': [httpx.Response(200, json={'records': [{'pmid': 9, 'pmcid': 'PMC9', 'doi': _DOI}]})]}
    )
    async with sessions.Session() as s:
        enriched, _ = await resolvers.NcbiIdConverterBatchResolver()(
            [ids.ArticleIds(doi=_DOI), ids.ArticleIds(doi=_DOI)], s
        )
    assert enriched[0] == enriched[1] == ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)
    assert f'ids={_DOI}' in transport.calls[0][1].replace('%2F', '/').replace('%3A', ':')  # queried once


# --- OpenAlex resolver ---------------------------------------------------

_OPENALEX_PATH = '/works'


def _openalex_work(doi: str, *, pmid: str | None = None, pmcid: str | None = None) -> dict[str, object]:
    """An OpenAlex works record: DOI and pmid/pmcid come back as URLs."""
    work_ids: dict[str, str] = {'doi': f'https://doi.org/{doi}'}
    if pmid is not None:
        work_ids['pmid'] = f'https://pubmed.ncbi.nlm.nih.gov/{pmid}'
    if pmcid is not None:
        work_ids['pmcid'] = f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}'
    return {'doi': f'https://doi.org/{doi}', 'ids': work_ids}


async def test_openalex_resolver_maps_doi_to_pmid_and_pmcid(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_OPENALEX_PATH}': [
                httpx.Response(200, json={'results': [_openalex_work(_DOI, pmid='9', pmcid='PMC9')]})
            ]
        }
    )
    async with sessions.Session() as s:
        enriched, abandoned = await resolvers.OpenAlexResolver()([ids.ArticleIds(doi=_DOI)], s)
    assert enriched == [ids.ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)]  # URLs stripped to bare ids
    assert abandoned == set()
    filter_param = transport.calls[0][1].replace('%2F', '/').replace('%3A', ':')
    assert f'doi:{_DOI}' in filter_param


async def test_openalex_resolver_correlates_case_insensitively(patch_transport: conftest.InstallTransport) -> None:
    # The element's DOI is upper-case; OpenAlex echoes it lower-case. They must still correlate.
    upper = _DOI.upper()
    patch_transport(
        {f'GET {_OPENALEX_PATH}': [httpx.Response(200, json={'results': [_openalex_work(_DOI, pmid='9')]})]}
    )
    async with sessions.Session() as s:
        enriched, _ = await resolvers.OpenAlexResolver()([ids.ArticleIds(doi=upper)], s)
    assert enriched == [ids.ArticleIds(pmid='9', doi=upper)]  # original-case DOI preserved, pmid added


async def test_openalex_resolver_passes_through_doi_less_element() -> None:
    enriched, abandoned = await resolvers.OpenAlexResolver()([ids.ArticleIds(pmid='9')], _NoHttp())
    assert enriched == [ids.ArticleIds(pmid='9')]  # no DOI to key on: no request
    assert abandoned == set()


async def test_openalex_resolver_leaves_unknown_doi_unenriched(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_OPENALEX_PATH}': [httpx.Response(200, json={'results': []})]})
    async with sessions.Session() as s:
        enriched, abandoned = await resolvers.OpenAlexResolver()([ids.ArticleIds(doi=_DOI)], s)
    assert enriched == [ids.ArticleIds(doi=_DOI)]  # definitive no-match, not abandoned
    assert abandoned == set()


# --- Europe PMC batch resolver -------------------------------------------


async def test_europe_pmc_batch_resolver_maps_many_pmids_in_one_call(
    patch_transport: conftest.InstallTransport,
) -> None:
    transport = patch_transport(
        {
            f'GET {_EPMC_SEARCH_PATH}': [
                httpx.Response(
                    200,
                    json={
                        'resultList': {
                            'result': [
                                {'id': '9', 'pmid': '9', 'pmcid': 'PMC9'},
                                {'id': '10', 'pmid': '10', 'pmcid': 'PMC10'},
                            ]
                        }
                    },
                )
            ],
        }
    )
    async with sessions.Session() as s:
        enriched, abandoned = await resolvers.EuropePmcBatchResolver()(
            [ids.ArticleIds(pmid='9'), ids.ArticleIds(pmid='10')], s
        )
    assert enriched == [ids.ArticleIds(pmid='9', pmcid='PMC9'), ids.ArticleIds(pmid='10', pmcid='PMC10')]
    assert abandoned == set()
    assert len(transport.calls) == 1  # both pmids OR'd into one query
    query = transport.calls[0][1].replace('%3A', ':')
    assert 'EXT_ID:9' in query
    assert 'EXT_ID:10' in query


async def test_europe_pmc_batch_resolver_passes_through_when_pmcid_known() -> None:
    # A pmcid-bearing element mirrors the per-item no-op: it is never queried.
    resolver = resolvers.EuropePmcBatchResolver()
    enriched, abandoned = await resolver([ids.ArticleIds(pmid='9', pmcid='PMC9')], _NoHttp())
    assert enriched == [ids.ArticleIds(pmid='9', pmcid='PMC9')]
    assert abandoned == set()


async def test_europe_pmc_batch_resolver_leaves_no_pmc_hit_unenriched(
    patch_transport: conftest.InstallTransport,
) -> None:
    patch_transport({f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': []}})]})
    async with sessions.Session() as s:
        enriched, abandoned = await resolvers.EuropePmcBatchResolver()([ids.ArticleIds(pmid='9')], s)
    assert enriched == [ids.ArticleIds(pmid='9')]
    assert abandoned == set()
