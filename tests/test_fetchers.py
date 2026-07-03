"""Tests for the fetch seam: fetchers, the ladder, and the file-set."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from litfetch import _http, artifacts, fetchers, ids, sessions
from tests import conftest

_DOI = '10.1016/j.test.2024.01.001'
_ELS_CREDS = {'elsevier_api_key': 'k'}
_EMAIL = 'test@example.org'  # Unpaywall requires an email; litfetch ships no default


def _xml_path(numeric: str, version: int) -> str:
    return f'/PMC{numeric}.{version}/PMC{numeric}.{version}.xml'


async def _fetch(
    fetcher: fetchers.Fetcher,
    article_ids: ids.ArticleIds,
    *,
    credentials: Mapping[str, object] | None = None,
) -> artifacts.Blob | None:
    """Run a fetcher directly inside an ephemeral session (built after patch_transport)."""
    async with sessions.Session() as s:
        return await fetcher.fetch(article_ids, credentials=credentials, http=s)


# --- PMC OA S3 fetch -----------------------------------------------------


async def test_pmc_oa_fetcher_short_circuits_when_no_pmcid() -> None:
    assert await _fetch(fetchers.PmcOaFetcher(), ids.ArticleIds(pmid='1'), credentials=None) is None


async def test_pmc_oa_fetcher_returns_jats_artifact(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_xml_path("9", 1)}': [httpx.Response(200, content=conftest.MINIMAL_JATS)]})
    blob = await _fetch(fetchers.PmcOaFetcher(), ids.ArticleIds(pmcid='PMC9'), credentials=None)
    assert blob is not None
    assert blob.file.kind is artifacts.FileKind.BODY
    assert blob.file.source == 'pmc_oa_s3'
    assert blob.file.media_type == artifacts.JATS_XML
    assert blob.file.uri == fetchers._pmc_versioned_xml_url('9', 1)
    assert blob.content == conftest.MINIMAL_JATS


async def test_pmc_oa_fetcher_falls_through_404s(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_xml_path("9", 1)}': [httpx.Response(404)],
            f'GET {_xml_path("9", 2)}': [httpx.Response(404)],
            f'GET {_xml_path("9", 3)}': [httpx.Response(200, content=conftest.MINIMAL_JATS)],
        }
    )
    blob = await _fetch(fetchers.PmcOaFetcher(), ids.ArticleIds(pmcid='PMC9'), credentials=None)
    assert blob is not None
    assert len(transport.calls) == 3


async def test_pmc_oa_fetcher_returns_none_when_all_404(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {f'GET {_xml_path("9", v)}': [httpx.Response(404)] for v in range(1, fetchers._PMC_OA_MAX_VERSION + 1)}
    )
    assert await _fetch(fetchers.PmcOaFetcher(), ids.ArticleIds(pmcid='PMC9'), credentials=None) is None


# --- fetch_body dispatcher -----------------------------------------------


class _FakeFetcher:
    def __init__(
        self,
        name: str,
        blob: artifacts.Blob | None,
        requires: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.requires = requires
        self._blob = blob
        self.calls: list[ids.ArticleIds] = []
        self.last_credentials: Mapping[str, object] | None = None

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http | None = None,
    ) -> artifacts.Blob | None:
        del http
        self.calls.append(article_ids)
        self.last_credentials = credentials
        return self._blob


class _SpyResolver:
    def __init__(self, returns: ids.ArticleIds) -> None:
        self._returns = returns
        self.calls = 0

    async def __call__(self, article_ids: ids.ArticleIds, http: object) -> ids.ArticleIds:
        del http
        self.calls += 1
        return article_ids.merge(self._returns)


def _ok(source: str = 'fake') -> artifacts.Blob:
    return artifacts.Blob(
        file=artifacts.File(
            kind=artifacts.FileKind.BODY,
            source=source,
            media_type=artifacts.JATS_XML,
            uri=f'https://example/{source}',
        ),
        content=b'<x/>',
    )


async def test_dispatcher_first_non_none_wins() -> None:
    f1 = _FakeFetcher('first', blob=None)
    f2 = _FakeFetcher('second', blob=_ok('second'))
    f3 = _FakeFetcher('third', blob=_ok('third'))
    blob = await sessions.fetch_body(ids.ArticleIds(pmcid='PMC1'), fetchers=(f1, f2, f3))
    assert blob is not None
    assert blob.file.source == 'second'
    assert len(f1.calls) == 1
    assert len(f2.calls) == 1
    assert f3.calls == []


async def test_dispatcher_returns_none_when_all_none() -> None:
    ladder = (_FakeFetcher('a', None), _FakeFetcher('b', None))
    assert await sessions.fetch_body(ids.ArticleIds(pmcid='PMC1'), fetchers=ladder) is None


async def test_dispatcher_passes_ids_and_credentials() -> None:
    f = _FakeFetcher('only', blob=None)
    await sessions.fetch_body(ids.ArticleIds(pmcid='PMC42', doi=_DOI), fetchers=(f,), credentials=_ELS_CREDS)
    assert f.calls[0] == ids.ArticleIds(pmcid='PMC42', doi=_DOI)
    assert f.last_credentials == _ELS_CREDS


async def test_resolver_not_called_when_fetcher_already_satisfied() -> None:
    resolver = _SpyResolver(ids.ArticleIds(pmcid='PMCx'))
    f = _FakeFetcher('s', blob=_ok('s'), requires=frozenset({'pmcid'}))
    blob = await sessions.fetch_body(ids.ArticleIds(pmcid='PMC1'), resolver=resolver, fetchers=(f,))
    assert blob is not None
    assert resolver.calls == 0


async def test_resolver_called_once_and_memoised() -> None:
    resolver = _SpyResolver(ids.ArticleIds(pmcid='PMC1'))
    f1 = _FakeFetcher('first', blob=None, requires=frozenset({'pmcid'}))
    f2 = _FakeFetcher('second', blob=_ok('second'), requires=frozenset({'pmcid'}))
    blob = await sessions.fetch_body(ids.ArticleIds(pmid='1'), resolver=resolver, fetchers=(f1, f2))
    assert blob is not None
    assert blob.file.source == 'second'
    assert resolver.calls == 1
    assert f1.calls[0] == ids.ArticleIds(pmid='1', pmcid='PMC1')


async def test_fetcher_skipped_when_still_unsatisfiable_after_resolve() -> None:
    resolver = _SpyResolver(ids.ArticleIds())  # adds nothing
    f = _FakeFetcher('needs_doi', blob=_ok('needs_doi'), requires=frozenset({'doi'}))
    blob = await sessions.fetch_body(ids.ArticleIds(pmid='1'), resolver=resolver, fetchers=(f,))
    assert blob is None
    assert resolver.calls == 1
    assert f.calls == []


async def test_fetch_body_skips_unsatisfied_fetchers_without_resolver() -> None:
    f = _FakeFetcher('needs_doi', blob=_ok('needs_doi'), requires=frozenset({'doi'}))
    assert await sessions.fetch_body(ids.ArticleIds(pmid='1'), fetchers=(f,)) is None
    assert f.calls == []


async def test_default_dispatcher_returns_pmc_oa_end_to_end(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_xml_path("9", 1)}': [httpx.Response(200, content=conftest.MINIMAL_JATS)]})
    blob = await sessions.fetch_body(ids.ArticleIds(pmcid='PMC9'))
    assert blob is not None
    assert blob.file.source == 'pmc_oa_s3'


def test_default_fetchers_order() -> None:
    assert [f.name for f in fetchers.default_fetchers()] == ['pmc_oa_s3', 'europe_pmc', 'elsevier_oa', 'springer_oa']


# --- Europe PMC ----------------------------------------------------------

_EPMC_FT_PATH = '/europepmc/webservices/rest/PMC9/fullTextXML'


async def test_europe_pmc_fetches_for_known_pmcid(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport({f'GET {_EPMC_FT_PATH}': [httpx.Response(200, content=conftest.MINIMAL_JATS)]})
    blob = await _fetch(fetchers.EuropePmcFetcher(), ids.ArticleIds(pmcid='PMC9'), credentials=None)
    assert blob is not None
    assert blob.file.source == 'europe_pmc'
    assert blob.file.media_type == artifacts.JATS_XML
    assert len(transport.calls) == 1


async def test_europe_pmc_short_circuits_without_pmcid() -> None:
    assert await _fetch(fetchers.EuropePmcFetcher(), ids.ArticleIds(pmid='9'), credentials=None) is None


# --- bioRxiv / medRxiv (opt-in) ------------------------------------------

_BIORXIV_DOI = '10.1101/2020.11.30.403378'
_DETAILS_BIORXIV = f'/details/biorxiv/{_BIORXIV_DOI}'
_DETAILS_MEDRXIV = f'/details/medrxiv/{_BIORXIV_DOI}'
_JATS_URL = 'https://www.biorxiv.org/content/test.source.xml'


def _details(jats_url: str) -> httpx.Response:
    return httpx.Response(200, json={'collection': [{'version': '1', 'jatsxml': jats_url}]})


async def test_biorxiv_short_circuits_without_doi() -> None:
    assert await _fetch(fetchers.BiorxivFetcher(), ids.ArticleIds(pmid='1'), credentials=None) is None


async def test_biorxiv_ignores_non_preprint_doi() -> None:
    # Not a CSH-prefix DOI: returns None without any API or impersonated call.
    assert await _fetch(fetchers.BiorxivFetcher(), ids.ArticleIds(doi=_DOI), credentials=None) is None


async def test_biorxiv_fetches_jats_via_impersonation(
    patch_transport: conftest.InstallTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport({f'GET {_DETAILS_BIORXIV}': [_details(_JATS_URL)]})
    seen = {}

    async def fake_impersonated(url: str, *, impersonate: str) -> bytes:
        seen['url'] = url
        seen['impersonate'] = impersonate
        return conftest.MINIMAL_JATS

    monkeypatch.setattr(fetchers, '_fetch_impersonated', fake_impersonated)
    blob = await _fetch(fetchers.BiorxivFetcher(), ids.ArticleIds(doi=_BIORXIV_DOI), credentials=None)
    assert blob is not None
    assert blob.file.source == 'biorxiv'
    assert blob.file.media_type == artifacts.JATS_XML
    assert blob.file.uri == _JATS_URL
    assert blob.content == conftest.MINIMAL_JATS
    assert seen == {'url': _JATS_URL, 'impersonate': 'chrome'}


async def test_biorxiv_falls_back_to_medrxiv(
    patch_transport: conftest.InstallTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(
        {
            f'GET {_DETAILS_BIORXIV}': [httpx.Response(200, json={'collection': []})],
            f'GET {_DETAILS_MEDRXIV}': [_details(_JATS_URL)],
        }
    )

    async def fake_impersonated(url: str, *, impersonate: str) -> bytes:
        del url, impersonate
        return conftest.MINIMAL_JATS

    monkeypatch.setattr(fetchers, '_fetch_impersonated', fake_impersonated)
    blob = await _fetch(fetchers.BiorxivFetcher(), ids.ArticleIds(doi=_BIORXIV_DOI), credentials=None)
    assert blob is not None
    assert blob.file.source == 'biorxiv'


async def test_biorxiv_returns_none_when_no_jats(
    patch_transport: conftest.InstallTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(
        {
            f'GET {_DETAILS_BIORXIV}': [httpx.Response(200, json={'collection': [{'version': '1'}]})],
            f'GET {_DETAILS_MEDRXIV}': [httpx.Response(200, json={'collection': []})],
        }
    )

    async def boom(url: str, *, impersonate: str) -> bytes | None:
        del url, impersonate
        raise AssertionError('impersonated fetch must not run without a jatsxml link')

    monkeypatch.setattr(fetchers, '_fetch_impersonated', boom)
    assert await _fetch(fetchers.BiorxivFetcher(), ids.ArticleIds(doi=_BIORXIV_DOI), credentials=None) is None


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
    assert fetchers._elsevier_has_body(_ELS_FULL_TEXT) is True
    assert fetchers._elsevier_has_body(_ELS_ABSTRACT_ONLY) is False


async def test_elsevier_returns_none_without_caller_key() -> None:
    fetcher = fetchers.ElsevierFetcher()
    assert await _fetch(fetcher, ids.ArticleIds(doi=_DOI), credentials=None) is None
    assert await _fetch(fetcher, ids.ArticleIds(doi=_DOI), credentials={'wiley_tdm_token': 'w'}) is None


async def test_elsevier_returns_none_without_doi() -> None:
    assert await _fetch(fetchers.ElsevierFetcher(), ids.ArticleIds(pmid='1'), credentials=_ELS_CREDS) is None


async def test_elsevier_fetches_elsevier_xml_artifact(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport(
        {
            f'GET {_CROSSREF_PATH}': [_crossref_elsevier_link_resp()],
            f'GET {_ELS_FETCH_PATH}': [httpx.Response(200, content=_ELS_FULL_TEXT)],
        }
    )
    blob = await _fetch(fetchers.ElsevierFetcher(), ids.ArticleIds(doi=_DOI), credentials=_ELS_CREDS)
    assert blob is not None
    assert blob.file.source == 'elsevier_oa'
    assert blob.file.media_type == artifacts.ELSEVIER_XML
    assert blob.content == _ELS_FULL_TEXT
    assert [c[0] for c in transport.calls] == [f'GET {_CROSSREF_PATH}', f'GET {_ELS_FETCH_PATH}']


async def test_elsevier_returns_none_for_non_elsevier_doi(patch_transport: conftest.InstallTransport) -> None:
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
    assert await _fetch(fetchers.ElsevierFetcher(), ids.ArticleIds(doi=_DOI), credentials=_ELS_CREDS) is None
    assert [c[0] for c in transport.calls] == [f'GET {_CROSSREF_PATH}']


async def test_elsevier_returns_none_on_abstract_only(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_CROSSREF_PATH}': [_crossref_elsevier_link_resp()],
            f'GET {_ELS_FETCH_PATH}': [httpx.Response(200, content=_ELS_ABSTRACT_ONLY)],
        }
    )
    assert await _fetch(fetchers.ElsevierFetcher(), ids.ArticleIds(doi=_DOI), credentials=_ELS_CREDS) is None


# --- Springer OA ---------------------------------------------------------

_SPRINGER_PATH = '/openaccess/jats'
_SPRINGER_CREDS = {'springer_api_key': 'sk'}

# The real OpenAccess response wraps the JATS in a <response> envelope behind a
# DOCTYPE that declares parameter entities (which defusedxml refuses to parse).
_SPRINGER_ENVELOPE = (
    b"<?xml version='1.0'?>\n"
    b'<!DOCTYPE response [\n'
    b'<!ENTITY % article SYSTEM "http://jats.nlm.nih.gov/archiving/1.2/JATS-archivearticle1.dtd">\n]>'
    b'<response><apiMessage>from Springer Nature</apiMessage><records>'
    b'<article dtd-version="1.2" xmlns:xlink="http://www.w3.org/1999/xlink">'
    b'<front><article-meta><title-group><article-title>T</article-title></title-group></article-meta></front>'
    b'<body><sec><p>Full text.</p></sec></body>'
    b'</article></records></response>'
)


async def test_springer_returns_none_without_key() -> None:
    assert await _fetch(fetchers.SpringerFetcher(), ids.ArticleIds(doi=_DOI), credentials=None) is None


async def test_springer_returns_none_without_doi() -> None:
    assert await _fetch(fetchers.SpringerFetcher(), ids.ArticleIds(pmid='1'), credentials=_SPRINGER_CREDS) is None


async def test_springer_fetches_jats_body(patch_transport: conftest.InstallTransport) -> None:
    transport = patch_transport({f'GET {_SPRINGER_PATH}': [httpx.Response(200, content=_SPRINGER_ENVELOPE)]})
    blob = await _fetch(fetchers.SpringerFetcher(), ids.ArticleIds(doi=_DOI), credentials=_SPRINGER_CREDS)
    assert blob is not None
    assert blob.file.source == 'springer_oa'
    assert blob.file.media_type == artifacts.JATS_XML
    # The envelope and its entity-declaring DOCTYPE are stripped; clean JATS remains.
    assert blob.content.lstrip().startswith(b'<?xml')
    assert b'<article' in blob.content
    assert b'<body' in blob.content
    assert b'<response' not in blob.content
    assert b'<!DOCTYPE' not in blob.content
    # The stored uri must not carry the secret api_key; the key travels only on the request.
    assert 'sk' not in (blob.file.uri or '')
    assert 'api_key' not in (blob.file.uri or '')
    assert 'api_key=sk' in transport.calls[0][1]


async def test_springer_returns_none_without_article_body(patch_transport: conftest.InstallTransport) -> None:
    # A response with no OA article (e.g. non-OA DOI) carries no <body>.
    patch_transport({f'GET {_SPRINGER_PATH}': [httpx.Response(200, content=b'<response><records/></response>')]})
    assert await _fetch(fetchers.SpringerFetcher(), ids.ArticleIds(doi=_DOI), credentials=_SPRINGER_CREDS) is None


def test_extract_jats_article_skips_wrapper_elements() -> None:
    # The anchor must land on the real <article> root, not a `<article-set>`/
    # `<article-meta>` wrapper that shares its prefix.
    content = (
        b'<response><records><article-set>'
        b'<article dtd-version="1.2"><front><article-meta/></front>'
        b'<body><sec><p>x</p></sec></body></article>'
        b'</article-set></records></response>'
    )
    extracted = fetchers._extract_jats_article(content)
    assert extracted is not None
    assert b'<article-set' not in extracted
    assert b'<article ' in extracted
    assert b'<body' in extracted


# --- file-set listing + fetching -----------------------------------------

# The .xml/.pdf are stem renditions (BODY); figure1.jpg and data.csv are genuine
# SUPPLEMENTARY assets.
_S3_LIST_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>pmc-oa-opendata</Name>
  <Prefix>PMC9.</Prefix>
  <IsTruncated>false</IsTruncated>
  <Contents><Key>PMC9.1/PMC9.1.xml</Key><Size>1234</Size></Contents>
  <Contents><Key>PMC9.1/PMC9.1.pdf</Key><Size>99999</Size></Contents>
  <Contents><Key>PMC9.1/figure1.jpg</Key><Size>5678</Size></Contents>
  <Contents><Key>PMC9.1/data.csv</Key><Size>42</Size></Contents>
</ListBucketResult>
"""


async def test_list_files_supplementary_excludes_renditions(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /': [httpx.Response(200, content=_S3_LIST_BODY)]})
    files = await sessions.list_files(
        ids.ArticleIds(pmcid='PMC9'), sources=(fetchers.PmcOaFetcher(),), kind=artifacts.FileKind.SUPPLEMENTARY
    )
    # The .xml and .pdf renditions are BODY; only true supplementary remain.
    assert [f.filename for f in files] == ['figure1.jpg', 'data.csv']
    by_name = {f.filename: f for f in files}
    assert by_name['figure1.jpg'].media_type == 'image/jpeg'
    assert by_name['data.csv'].media_type == 'text/csv'
    assert by_name['data.csv'].size_bytes == 42
    assert all(f.source == 'pmc_oa_s3' and f.credential_key is None for f in files)
    assert by_name['data.csv'].uri == 'https://pmc-oa-opendata.s3.amazonaws.com/PMC9.1/data.csv'


async def test_list_files_noop_without_pmcid() -> None:
    assert (
        await sessions.list_files(
            ids.ArticleIds(doi=_DOI), sources=(fetchers.PmcOaFetcher(),), kind=artifacts.FileKind.SUPPLEMENTARY
        )
        == ()
    )


async def test_list_files_body_returns_renditions(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /': [httpx.Response(200, content=_S3_LIST_BODY)]})
    reps = await sessions.list_files(
        ids.ArticleIds(pmcid='PMC9'), sources=(fetchers.PmcOaFetcher(),), kind=artifacts.FileKind.BODY
    )
    # Only the stem renditions, not the supplementary assets.
    assert [r.filename for r in reps] == ['PMC9.1.xml', 'PMC9.1.pdf']
    by_name = {r.filename: r for r in reps}
    assert by_name['PMC9.1.pdf'].media_type == 'application/pdf'
    assert by_name['PMC9.1.pdf'].size_bytes == 99999
    assert all(r.source == 'pmc_oa_s3' and r.uri is not None for r in reps)


async def test_list_files_unfiltered_returns_both_kinds(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /': [httpx.Response(200, content=_S3_LIST_BODY)]})
    files = await sessions.list_files(ids.ArticleIds(pmcid='PMC9'), sources=(fetchers.PmcOaFetcher(),))
    kinds = {f.filename: f.kind for f in files}
    assert kinds['PMC9.1.xml'] is artifacts.FileKind.BODY
    assert kinds['data.csv'] is artifacts.FileKind.SUPPLEMENTARY


async def test_fetch_file_routes_to_named_source(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /PMC9.1/data.csv': [httpx.Response(200, content=b'a,b\n1,2\n')]})
    ref = artifacts.File(
        kind=artifacts.FileKind.SUPPLEMENTARY,
        source='pmc_oa_s3',
        filename='data.csv',
        uri='https://pmc-oa-opendata.s3.amazonaws.com/PMC9.1/data.csv',
        media_type='text/csv',
    )
    blob = await sessions.fetch_file(ref)
    assert blob is not None
    assert blob.content == b'a,b\n1,2\n'
    assert blob.file is ref


async def test_fetch_file_returns_none_for_unknown_source() -> None:
    ref = artifacts.File(kind=artifacts.FileKind.SUPPLEMENTARY, filename='x', uri='https://e/x', source='nope')
    assert await sessions.fetch_file(ref) is None


async def test_fetch_file_downloads_rendition_bytes(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /PMC9.1/PMC9.1.pdf': [httpx.Response(200, content=b'%PDF-1.7 body')]})
    rep = artifacts.File(
        kind=artifacts.FileKind.BODY,
        media_type=artifacts.PDF,
        source='pmc_oa_s3',
        uri='https://pmc-oa-opendata.s3.amazonaws.com/PMC9.1/PMC9.1.pdf',
        filename='PMC9.1.pdf',
    )
    blob = await sessions.fetch_file(rep)
    assert blob is not None
    assert blob.file.media_type == artifacts.PDF
    assert blob.content == b'%PDF-1.7 body'


# --- Unpaywall file source -----------------------------------------------

_UNPAYWALL_PATH = f'/v2/{_DOI}'
_OA_PDF = 'https://oa.example/paper.pdf'


def _unpaywall_record(pdf_url: str | None) -> httpx.Response:
    best: dict[str, str] = {'license': 'cc-by'}
    if pdf_url is not None:
        best['url_for_pdf'] = pdf_url
    return httpx.Response(200, json={'oa_status': 'gold', 'best_oa_location': best})


async def test_unpaywall_lists_best_oa_pdf_as_body(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_UNPAYWALL_PATH}': [_unpaywall_record(_OA_PDF)]})
    files = await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.UnpaywallFileSource(email=_EMAIL),))
    assert len(files) == 1
    assert files[0].kind is artifacts.FileKind.BODY
    assert files[0].source == 'unpaywall'
    assert files[0].media_type == artifacts.PDF
    assert files[0].uri == _OA_PDF


async def test_unpaywall_lists_nothing_without_pdf(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_UNPAYWALL_PATH}': [_unpaywall_record(None)]})
    sources = (fetchers.UnpaywallFileSource(email=_EMAIL),)
    assert await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=sources) == ()


async def test_unpaywall_noop_without_doi() -> None:
    sources = (fetchers.UnpaywallFileSource(email=_EMAIL),)
    assert await sessions.list_files(ids.ArticleIds(pmid='1'), sources=sources) == ()


async def test_unpaywall_declines_without_contact(patch_transport: conftest.InstallTransport) -> None:
    # No email/contact: Unpaywall can't be queried, so the source declines with no request.
    transport = patch_transport({f'GET {_UNPAYWALL_PATH}': [_unpaywall_record(_OA_PDF)]})
    assert await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.UnpaywallFileSource(),)) == ()
    assert transport.calls == []


async def test_unpaywall_fetches_pdf_bytes(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /paper.pdf': [httpx.Response(200, content=b'%PDF-1.7 oa')]})
    ref = artifacts.File(kind=artifacts.FileKind.BODY, source='unpaywall', media_type=artifacts.PDF, uri=_OA_PDF)
    blob = await sessions.fetch_file(ref, sources=(fetchers.UnpaywallFileSource(),))
    assert blob is not None
    assert blob.content == b'%PDF-1.7 oa'


def test_default_file_sources_names() -> None:
    assert [s.name for s in fetchers.default_file_sources()] == [
        'pmc_oa_s3',
        'unpaywall',
        'semantic_scholar',
        'crossref_tdm',
        'springer',
    ]


async def test_unpaywall_record_shared_within_scope(patch_transport: conftest.InstallTransport) -> None:
    # list_files and resolve_access both hit Unpaywall; a scope serves the second from cache.
    transport = patch_transport({f'GET {_UNPAYWALL_PATH}': [_unpaywall_record(_OA_PDF)]})
    # The session contact flows to both the file source and resolve_access (no per-call email).
    async with sessions.Session(contact=_EMAIL) as session, session.scope() as s:
        files = await s.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.UnpaywallFileSource(),))
        meta = await s.resolve_access(ids.ArticleIds(doi=_DOI))
    assert len(files) == 1
    assert meta.basis == 'unpaywall'
    assert len(transport.calls) == 1  # one Unpaywall GET, the second deduped by the scope cache


# --- Semantic Scholar file source ----------------------------------------

_S2_PAPER_PATH = f'/graph/v1/paper/DOI:{_DOI}'


async def test_s2_lists_open_access_pdf_as_body(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_S2_PAPER_PATH}': [httpx.Response(200, json={'openAccessPdf': {'url': _OA_PDF}})]})
    files = await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.SemanticScholarFileSource(),))
    assert len(files) == 1
    assert files[0].kind is artifacts.FileKind.BODY
    assert files[0].source == 'semantic_scholar'
    assert files[0].media_type == artifacts.PDF
    assert files[0].uri == _OA_PDF


async def test_s2_lists_nothing_without_pdf(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_S2_PAPER_PATH}': [httpx.Response(200, json={'openAccessPdf': None})]})
    assert await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.SemanticScholarFileSource(),)) == ()


async def test_s2_file_source_noop_without_any_id() -> None:
    assert await sessions.list_files(ids.ArticleIds(), sources=(fetchers.SemanticScholarFileSource(),)) == ()


async def test_s2_fetches_pdf_bytes(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /paper.pdf': [httpx.Response(200, content=b'%PDF-1.7 s2')]})
    ref = artifacts.File(kind=artifacts.FileKind.BODY, source='semantic_scholar', media_type=artifacts.PDF, uri=_OA_PDF)
    blob = await sessions.fetch_file(ref, sources=(fetchers.SemanticScholarFileSource(),))
    assert blob is not None
    assert blob.content == b'%PDF-1.7 s2'


# --- Crossref TDM file source --------------------------------------------

_TDM_PDF = 'https://publisher.example/article.pdf'
_TDM_XML = 'https://publisher.example/article.xml'


def _crossref_tdm_links() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            'message': {
                'link': [
                    {'URL': _TDM_PDF, 'content-type': 'application/pdf', 'intended-application': 'text-mining'},
                    {'URL': _TDM_XML, 'content-type': 'text/xml', 'intended-application': 'text-mining'},
                    {'URL': 'https://x/landing', 'content-type': 'text/html', 'intended-application': 'similarity'},
                ]
            }
        },
    )


async def test_crossref_lists_text_mining_links_as_body(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_CROSSREF_PATH}': [_crossref_tdm_links()]})
    files = await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.CrossrefFileSource(),))
    # Only the two text-mining links; the similarity landing page is excluded.
    assert [(f.media_type, f.uri) for f in files] == [
        (artifacts.PDF, _TDM_PDF),
        ('text/xml', _TDM_XML),
    ]
    assert all(f.kind is artifacts.FileKind.BODY and f.source == 'crossref_tdm' for f in files)


async def test_crossref_noop_without_doi() -> None:
    assert await sessions.list_files(ids.ArticleIds(pmid='1'), sources=(fetchers.CrossrefFileSource(),)) == ()


async def test_crossref_fetches_tdm_bytes(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({'GET /article.pdf': [httpx.Response(200, content=b'%PDF-1.7 tdm')]})
    ref = artifacts.File(kind=artifacts.FileKind.BODY, source='crossref_tdm', media_type=artifacts.PDF, uri=_TDM_PDF)
    blob = await sessions.fetch_file(ref, sources=(fetchers.CrossrefFileSource(),))
    assert blob is not None
    assert blob.content == b'%PDF-1.7 tdm'


# --- Springer file source (Meta-backed PDF) ------------------------------

_META_PATH = '/meta/v2/json'
_SPRINGER_META_CREDS = {'springer_meta_api_key': 'mk'}
_SPRINGER_PDF = f'https://link.springer.com/openurl/pdf?id=doi:{_DOI}'


def _springer_meta(pdf_url: str | None, *, oa: bool) -> httpx.Response:
    url = [{'format': 'html', 'value': 'http://link.springer.com/openurl/fulltext'}]
    if pdf_url:
        url.append({'format': 'pdf', 'value': pdf_url})
    return httpx.Response(200, json={'records': [{'openaccess': 'true' if oa else 'false', 'url': url}]})


async def test_springer_filesource_lists_oa_pdf(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_META_PATH}': [_springer_meta(_SPRINGER_PDF, oa=True)]})
    files = await sessions.list_files(
        ids.ArticleIds(doi=_DOI), sources=(fetchers.SpringerFileSource(),), credentials=_SPRINGER_META_CREDS
    )
    assert len(files) == 1
    assert files[0].kind is artifacts.FileKind.BODY
    assert files[0].source == 'springer'
    assert files[0].media_type == artifacts.PDF
    assert files[0].uri == _SPRINGER_PDF
    assert files[0].credential_key is None  # OA: openly fetchable


async def test_springer_filesource_marks_subscription_entitled(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_META_PATH}': [_springer_meta(_SPRINGER_PDF, oa=False)]})
    files = await sessions.list_files(
        ids.ArticleIds(doi=_DOI), sources=(fetchers.SpringerFileSource(),), credentials=_SPRINGER_META_CREDS
    )
    assert files[0].credential_key == artifacts.INSTITUTIONAL


async def test_springer_filesource_noop_without_key() -> None:
    assert await sessions.list_files(ids.ArticleIds(doi=_DOI), sources=(fetchers.SpringerFileSource(),)) == ()


async def test_springer_filesource_noop_without_pdf(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_META_PATH}': [_springer_meta(None, oa=True)]})
    files = await sessions.list_files(
        ids.ArticleIds(doi=_DOI), sources=(fetchers.SpringerFileSource(),), credentials=_SPRINGER_META_CREDS
    )
    assert files == ()


async def test_springer_filesource_fetch_follows_openurl_redirect() -> None:
    # openURL 301 -> content/pdf -> 200 pdf; _download follows the redirect.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/openurl/pdf':
            location = f'https://link.springer.com/content/pdf/{_DOI}?pdf=openurl'
            return httpx.Response(301, headers={'Location': location})
        return httpx.Response(200, content=b'%PDF-1.7 springer')

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    ref = artifacts.File(kind=artifacts.FileKind.BODY, source='springer', media_type=artifacts.PDF, uri=_SPRINGER_PDF)
    async with sessions.Session(client_factory=factory) as s:
        blob = await fetchers.SpringerFileSource().fetch_file(ref, http=s)
    assert blob is not None
    assert blob.content == b'%PDF-1.7 springer'
