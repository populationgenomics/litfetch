"""Tests for the bundled identifier resolvers and chaining."""

from __future__ import annotations

import httpx

from litfetch import ids, resolvers, sessions
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
