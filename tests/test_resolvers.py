"""Tests for the bundled identifier resolvers and chaining."""

from __future__ import annotations

import httpx

from litfetch.ids import ArticleIds
from litfetch.resolvers import (
    EuropePmcResolver,
    NcbiIdConverterResolver,
    SemanticScholarResolver,
    chain,
    default_resolver,
)
from tests.conftest import InstallTransport

_EPMC_SEARCH_PATH = '/europepmc/webservices/rest/search'
_IDCONV_PATH = '/pmc/utils/idconv/v1.0/'
_DOI = '10.1016/j.test.2024.01.001'


# --- Europe PMC resolver -------------------------------------------------


async def test_europe_pmc_resolver_resolves_pmid_to_pmcid(patch_transport: InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': [{'pmcid': 'PMC9'}]}})],
        }
    )
    ids = await EuropePmcResolver()(ArticleIds(pmid='9'))
    assert ids == ArticleIds(pmid='9', pmcid='PMC9')


async def test_europe_pmc_resolver_noop_when_pmcid_present() -> None:
    ids = ArticleIds(pmid='9', pmcid='PMC9')
    assert await EuropePmcResolver()(ids) == ids


async def test_europe_pmc_resolver_noop_without_pmid() -> None:
    ids = ArticleIds(doi=_DOI)
    assert await EuropePmcResolver()(ids) == ids


async def test_europe_pmc_resolver_returns_input_on_empty_result(patch_transport: InstallTransport) -> None:
    patch_transport({f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': []}})]})
    ids = ArticleIds(pmid='9')
    assert await EuropePmcResolver()(ids) == ids


# --- NCBI ID Converter resolver ------------------------------------------


async def test_ncbi_resolver_maps_pmid_to_pmcid_and_doi(patch_transport: InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_IDCONV_PATH}': [
                httpx.Response(200, json={'records': [{'pmid': '9', 'pmcid': 'PMC9', 'doi': _DOI}]})
            ],
        }
    )
    ids = await NcbiIdConverterResolver()(ArticleIds(pmid='9'))
    assert ids == ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)
    assert transport.calls[0][2] is None  # GET, no body


async def test_ncbi_resolver_noop_on_error_record(patch_transport: InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_IDCONV_PATH}': [httpx.Response(200, json={'records': [{'pmid': '9', 'status': 'error'}]})],
        }
    )
    ids = ArticleIds(pmid='9')
    assert await NcbiIdConverterResolver()(ids) == ids


async def test_ncbi_resolver_noop_without_any_id() -> None:
    assert await NcbiIdConverterResolver()(ArticleIds()) == ArticleIds()


# --- Semantic Scholar resolver -------------------------------------------


async def test_s2_resolver_enriches_from_external_ids(patch_transport: InstallTransport) -> None:
    patch_transport(
        {
            'GET /graph/v1/paper/PMID:9': [
                httpx.Response(200, json={'externalIds': {'DOI': _DOI, 'PubMedCentral': '9', 'PubMed': 9}}),
            ],
        }
    )
    ids = await SemanticScholarResolver()(ArticleIds(pmid='9'))
    assert ids == ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)


async def test_s2_resolver_noop_without_any_id() -> None:
    assert await SemanticScholarResolver()(ArticleIds()) == ArticleIds()


# --- chain ---------------------------------------------------------------


class _Fixed:
    def __init__(self, returns: ArticleIds) -> None:
        self._returns = returns
        self.calls = 0

    async def __call__(self, ids: ArticleIds) -> ArticleIds:
        self.calls += 1
        return ids.merge(self._returns)


async def test_chain_runs_resolvers_in_order() -> None:
    composed = chain(_Fixed(ArticleIds(pmcid='PMC1')), _Fixed(ArticleIds(doi=_DOI)))
    assert await composed(ArticleIds(pmid='1')) == ArticleIds(pmid='1', pmcid='PMC1', doi=_DOI)


async def test_chain_stops_early_once_complete() -> None:
    complete = _Fixed(ArticleIds(pmcid='PMC1', doi=_DOI))
    spy = _Fixed(ArticleIds(pmid='other'))
    result = await chain(complete, spy)(ArticleIds(pmid='1'))
    assert result == ArticleIds(pmid='1', pmcid='PMC1', doi=_DOI)
    assert complete.calls == 1
    assert spy.calls == 0


async def test_default_resolver_composes_europe_pmc_then_ncbi(patch_transport: InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_EPMC_SEARCH_PATH}': [httpx.Response(200, json={'resultList': {'result': [{'pmcid': 'PMC9'}]}})],
            f'GET {_IDCONV_PATH}': [
                httpx.Response(200, json={'records': [{'pmid': '9', 'pmcid': 'PMC9', 'doi': _DOI}]})
            ],
        }
    )
    ids = await default_resolver()(ArticleIds(pmid='9'))
    assert ids == ArticleIds(pmid='9', pmcid='PMC9', doi=_DOI)
