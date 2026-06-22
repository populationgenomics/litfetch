"""Tests for the full-text source ladder and the demand-driven dispatcher."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from litfetch import (
    ArticleIds,
    ElsevierOaSource,
    EuropePmcSource,
    FullTextResult,
    PmcOaSource,
    default_sources,
    fetch_full_text,
    get_full_text,
    jats_to_markdown,
)
from litfetch.sources import _PMC_OA_MAX_VERSION, _elsevier_has_body, _pmc_versioned_xml_url
from tests.conftest import InstallTransport

_DOI = '10.1016/j.test.2024.01.001'
_ELS_CREDS = {'elsevier_api_key': 'k'}


def _xml_path(numeric: str, version: int) -> str:
    return f'/PMC{numeric}.{version}/PMC{numeric}.{version}.xml'


_MINIMAL_JATS = b"""<?xml version='1.0'?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta>
      <title-group><article-title>A short paper</article-title></title-group>
      <abstract><p>One sentence abstract.</p></abstract>
    </article-meta>
  </front>
  <body>
    <sec>
      <title>Intro</title>
      <p>Hello world.</p>
    </sec>
  </body>
</article>
"""


# --- JATS conversion -----------------------------------------------------


def test_jats_to_markdown_smoke() -> None:
    md = jats_to_markdown(_MINIMAL_JATS)
    assert 'A short paper' in md
    assert 'Hello world.' in md
    assert 'Intro' in md


# --- PMC OA S3 fetch -----------------------------------------------------


async def test_pmc_oa_source_short_circuits_when_no_pmcid() -> None:
    assert await PmcOaSource().try_fetch(ArticleIds(pmid='1'), credentials=None) is None


async def test_pmc_oa_source_returns_full_text_result(patch_transport: InstallTransport) -> None:
    patch_transport({f'GET {_xml_path("9", 1)}': [httpx.Response(200, content=_MINIMAL_JATS)]})
    result = await PmcOaSource().try_fetch(ArticleIds(pmcid='PMC9'), credentials=None)
    assert result is not None
    assert result.source == 'pmc_oa_s3'
    assert result.source_url == _pmc_versioned_xml_url('9', 1)
    assert 'A short paper' in result.markdown


async def test_pmc_oa_source_falls_through_404s(patch_transport: InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_xml_path("9", 1)}': [httpx.Response(404)],
            f'GET {_xml_path("9", 2)}': [httpx.Response(404)],
            f'GET {_xml_path("9", 3)}': [httpx.Response(200, content=_MINIMAL_JATS)],
        }
    )
    result = await PmcOaSource().try_fetch(ArticleIds(pmcid='PMC9'), credentials=None)
    assert result is not None
    assert len(transport.calls) == 3


async def test_pmc_oa_source_returns_none_when_all_404(patch_transport: InstallTransport) -> None:
    patch_transport({f'GET {_xml_path("9", v)}': [httpx.Response(404)] for v in range(1, _PMC_OA_MAX_VERSION + 1)})
    assert await PmcOaSource().try_fetch(ArticleIds(pmcid='PMC9'), credentials=None) is None


# --- dispatcher ----------------------------------------------------------


class _FakeSource:
    def __init__(
        self,
        name: str,
        result: FullTextResult | None,
        requires: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.requires = requires
        self._result = result
        self.calls: list[ArticleIds] = []
        self.last_credentials: Mapping[str, object] | None = None

    async def try_fetch(
        self,
        ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FullTextResult | None:
        del http_client
        self.calls.append(ids)
        self.last_credentials = credentials
        return self._result


class _SpyResolver:
    def __init__(self, returns: ArticleIds) -> None:
        self._returns = returns
        self.calls = 0

    async def __call__(self, ids: ArticleIds) -> ArticleIds:
        self.calls += 1
        return ids.merge(self._returns)


def _ok(source: str = 'fake') -> FullTextResult:
    return FullTextResult(
        markdown='# hi',
        source=source,
        source_format='jats',
        source_url=f'https://example/{source}',
        pmc_id='PMC1',
    )


async def test_dispatcher_first_non_none_wins() -> None:
    s1 = _FakeSource('first', result=None)
    s2 = _FakeSource('second', result=_ok('second'))
    s3 = _FakeSource('third', result=_ok('third'))
    result = await get_full_text(ArticleIds(pmcid='PMC1'), sources=(s1, s2, s3))
    assert result is not None
    assert result.source == 'second'
    assert len(s1.calls) == 1
    assert len(s2.calls) == 1
    assert s3.calls == []


async def test_dispatcher_returns_none_when_all_none() -> None:
    sources = (_FakeSource('a', None), _FakeSource('b', None))
    assert await get_full_text(ArticleIds(pmcid='PMC1'), sources=sources) is None


async def test_dispatcher_passes_ids_and_credentials() -> None:
    s = _FakeSource('only', result=None)
    await get_full_text(ArticleIds(pmcid='PMC42', doi=_DOI), sources=(s,), credentials=_ELS_CREDS)
    assert s.calls[0] == ArticleIds(pmcid='PMC42', doi=_DOI)
    assert s.last_credentials == _ELS_CREDS


async def test_resolver_not_called_when_source_already_satisfied() -> None:
    resolver = _SpyResolver(ArticleIds(pmcid='PMCx'))
    s = _FakeSource('s', result=_ok('s'), requires=frozenset({'pmcid'}))
    result = await get_full_text(ArticleIds(pmcid='PMC1'), resolver=resolver, sources=(s,))
    assert result is not None
    assert resolver.calls == 0


async def test_resolver_called_once_and_memoised() -> None:
    resolver = _SpyResolver(ArticleIds(pmcid='PMC1'))
    s1 = _FakeSource('first', result=None, requires=frozenset({'pmcid'}))
    s2 = _FakeSource('second', result=_ok('second'), requires=frozenset({'pmcid'}))
    result = await get_full_text(ArticleIds(pmid='1'), resolver=resolver, sources=(s1, s2))
    assert result is not None
    assert result.source == 'second'
    assert resolver.calls == 1
    assert s1.calls[0] == ArticleIds(pmid='1', pmcid='PMC1')


async def test_source_skipped_when_still_unsatisfiable_after_resolve() -> None:
    resolver = _SpyResolver(ArticleIds())  # adds nothing
    s = _FakeSource('needs_doi', result=_ok('needs_doi'), requires=frozenset({'doi'}))
    result = await get_full_text(ArticleIds(pmid='1'), resolver=resolver, sources=(s,))
    assert result is None
    assert resolver.calls == 1
    assert s.calls == []


async def test_fetch_full_text_skips_unsatisfied_sources_without_resolving() -> None:
    s = _FakeSource('needs_doi', result=_ok('needs_doi'), requires=frozenset({'doi'}))
    assert await fetch_full_text(ArticleIds(pmid='1'), sources=(s,)) is None
    assert s.calls == []


async def test_default_dispatcher_returns_pmc_oa_end_to_end(patch_transport: InstallTransport) -> None:
    patch_transport({f'GET {_xml_path("9", 1)}': [httpx.Response(200, content=_MINIMAL_JATS)]})
    result = await get_full_text(ArticleIds(pmcid='PMC9'))
    assert result is not None
    assert result.source == 'pmc_oa_s3'


def test_default_sources_order() -> None:
    assert [s.name for s in default_sources()] == ['pmc_oa_s3', 'europe_pmc', 'elsevier_oa']


# --- Europe PMC ----------------------------------------------------------

_EPMC_FT_PATH = '/europepmc/webservices/rest/PMC9/fullTextXML'


async def test_europe_pmc_fetches_for_known_pmcid(patch_transport: InstallTransport) -> None:
    transport = patch_transport({f'GET {_EPMC_FT_PATH}': [httpx.Response(200, content=_MINIMAL_JATS)]})
    result = await EuropePmcSource().try_fetch(ArticleIds(pmcid='PMC9'), credentials=None)
    assert result is not None
    assert result.source == 'europe_pmc'
    assert result.pmc_id == 'PMC9'
    assert len(transport.calls) == 1


async def test_europe_pmc_short_circuits_without_pmcid() -> None:
    assert await EuropePmcSource().try_fetch(ArticleIds(pmid='9'), credentials=None) is None


# --- Elsevier OA ---------------------------------------------------------

_CROSSREF_PATH = '/works/10.1016/j.test.2024.01.001'
_ELS_LINK = 'https://api.elsevier.com/content/article/PII:S123?httpAccept=text/xml'
_ELS_FETCH_PATH = '/content/article/PII:S123'

_ELS_FULL_TEXT = (
    b'<full-text-retrieval-response><originalText><xocs:doc><article><body>'
    b'<ce:sections><ce:section><ce:para>Body text here.</ce:para>'
    b'</ce:section></ce:sections></body></article></xocs:doc></originalText>'
    b'</full-text-retrieval-response>'
)
_ELS_ABSTRACT_ONLY = (
    b'<full-text-retrieval-response><coredata>'
    b'<dc:description>Abstract only.</dc:description></coredata>'
    b'</full-text-retrieval-response>'
)


def _crossref_elsevier_link_resp() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            'message': {'link': [{'URL': _ELS_LINK, 'content-type': 'text/xml', 'intended-application': 'text-mining'}]}
        },
    )


def test_elsevier_has_body_distinguishes_full_text_from_abstract() -> None:
    assert _elsevier_has_body(_ELS_FULL_TEXT) is True
    assert _elsevier_has_body(_ELS_ABSTRACT_ONLY) is False


async def test_elsevier_returns_none_without_caller_key() -> None:
    source = ElsevierOaSource()
    assert await source.try_fetch(ArticleIds(doi=_DOI), credentials=None) is None
    assert await source.try_fetch(ArticleIds(doi=_DOI), credentials={'wiley_tdm_token': 'w'}) is None


async def test_elsevier_returns_none_without_doi() -> None:
    assert await ElsevierOaSource().try_fetch(ArticleIds(pmid='1'), credentials=_ELS_CREDS) is None


async def test_elsevier_fetches_and_converts(patch_transport: InstallTransport, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr('litfetch.sources.jats_to_markdown', lambda _b: '# Elsevier MD')
    transport = patch_transport(
        {
            f'GET {_CROSSREF_PATH}': [_crossref_elsevier_link_resp()],
            f'GET {_ELS_FETCH_PATH}': [httpx.Response(200, content=_ELS_FULL_TEXT)],
        }
    )
    result = await ElsevierOaSource().try_fetch(ArticleIds(doi=_DOI), credentials=_ELS_CREDS)
    assert result is not None
    assert result.source == 'elsevier_oa'
    assert result.source_format == 'elsevier-xml'
    assert result.markdown == '# Elsevier MD'
    assert [c[0] for c in transport.calls] == [f'GET {_CROSSREF_PATH}', f'GET {_ELS_FETCH_PATH}']


async def test_elsevier_returns_none_for_non_elsevier_doi(patch_transport: InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_CROSSREF_PATH}': [
                httpx.Response(
                    200,
                    json={
                        'message': {'link': [{'URL': 'https://example.com/a.pdf', 'content-type': 'application/pdf'}]}
                    },
                ),
            ],
        }
    )
    assert await ElsevierOaSource().try_fetch(ArticleIds(doi=_DOI), credentials=_ELS_CREDS) is None
    assert [c[0] for c in transport.calls] == [f'GET {_CROSSREF_PATH}']


async def test_elsevier_returns_none_on_abstract_only(patch_transport: InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_CROSSREF_PATH}': [_crossref_elsevier_link_resp()],
            f'GET {_ELS_FETCH_PATH}': [httpx.Response(200, content=_ELS_ABSTRACT_ONLY)],
        }
    )
    assert await ElsevierOaSource().try_fetch(ArticleIds(doi=_DOI), credentials=_ELS_CREDS) is None
