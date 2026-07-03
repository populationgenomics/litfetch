"""Tests for licence extraction from bytes and resolution via Unpaywall."""

from __future__ import annotations

import httpx

from litfetch import artifacts, ids, serde, sessions, source_metadata
from tests import conftest

_JATS_WITH_LICENCE = b"""<?xml version='1.0'?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <permissions>
      <license license-type="open-access" xlink:href="https://creativecommons.org/licenses/by/4.0/">
        <license-p>Open access under the CC BY license.</license-p>
      </license>
    </permissions>
  </article-meta></front>
  <body><sec><p>Body.</p></sec></body>
</article>
"""

_JATS_NO_HREF = b"""<?xml version='1.0'?>
<article>
  <front><article-meta><permissions>
    <license license-type="open-access"><license-p>Open access.</license-p></license>
  </permissions></article-meta></front>
</article>
"""

_ELSEVIER_WITH_LICENCE = (
    b'<full-text-retrieval-response><coredata>'
    b'<openaccess>1</openaccess>'
    b'<openaccessUserLicense>http://creativecommons.org/licenses/by/4.0/</openaccessUserLicense>'
    b'</coredata></full-text-retrieval-response>'
)

_MINIMAL_JATS = b'<article><front><article-meta/></front><body/></article>'


def _blob(content: bytes, media_type: str) -> artifacts.Blob:
    return artifacts.Blob(
        file=artifacts.File(kind=artifacts.FileKind.BODY, source='x', media_type=media_type, uri='u'),
        content=content,
    )


def test_jats_licence_prefers_xlink_href() -> None:
    meta = source_metadata.extract_source_metadata(_blob(_JATS_WITH_LICENCE, artifacts.JATS_XML))
    assert meta.licence == 'https://creativecommons.org/licenses/by/4.0/'
    assert meta.access == 'open-access'
    assert meta.basis == 'artifact'


def test_jats_licence_falls_back_to_license_type() -> None:
    meta = source_metadata.extract_source_metadata(_blob(_JATS_NO_HREF, artifacts.JATS_XML))
    assert meta.licence == 'open-access'
    assert meta.access == 'open-access'
    assert meta.basis == 'artifact'


def test_jats_without_permissions_yields_empty() -> None:
    meta = source_metadata.extract_source_metadata(_blob(_MINIMAL_JATS, artifacts.JATS_XML))
    assert meta == artifacts.SourceMetadata()


def test_elsevier_licence_from_user_license() -> None:
    meta = source_metadata.extract_source_metadata(_blob(_ELSEVIER_WITH_LICENCE, artifacts.ELSEVIER_XML))
    assert meta.licence == 'http://creativecommons.org/licenses/by/4.0/'
    assert meta.access == 'open-access'
    assert meta.basis == 'artifact'


def test_pdf_carries_no_extractable_licence() -> None:
    # A PDF ships no licence in its bytes -- an authority (Unpaywall) is the basis there.
    meta = source_metadata.extract_source_metadata(_blob(b'%PDF-1.7 ...', artifacts.PDF))
    assert meta == artifacts.SourceMetadata()


def test_source_metadata_round_trips_through_serde() -> None:
    meta = artifacts.SourceMetadata(licence='CC-BY-4.0', access='open-access', basis='unpaywall')
    assert serde.source_metadata_from_dict(serde.source_metadata_to_dict(meta)) == meta


# --- Unpaywall access resolution -----------------------------------------

_DOI = '10.1/x'
_UNPAYWALL_PATH = f'/v2/{_DOI}'
_EMAIL = 'test@example.org'  # Unpaywall requires an email; litfetch ships none


async def test_resolve_access_returns_licence_and_oa_status(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_UNPAYWALL_PATH}': [
                httpx.Response(
                    200,
                    json={'is_oa': True, 'oa_status': 'gold', 'best_oa_location': {'license': 'cc-by'}},
                )
            ]
        }
    )
    meta = await sessions.resolve_access(ids.ArticleIds(doi=_DOI), email=_EMAIL)
    assert meta.licence == 'cc-by'
    assert meta.access == 'gold'
    assert meta.basis == 'unpaywall'


async def test_resolve_access_handles_closed_record(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET {_UNPAYWALL_PATH}': [
                httpx.Response(200, json={'is_oa': False, 'oa_status': 'closed', 'best_oa_location': None})
            ]
        }
    )
    meta = await sessions.resolve_access(ids.ArticleIds(doi=_DOI), email=_EMAIL)
    assert meta.licence is None
    assert meta.access == 'closed'
    assert meta.basis == 'unpaywall'


async def test_resolve_access_noop_without_doi() -> None:
    # No DOI: returns empty without any network call (no transport scripted).
    assert await sessions.resolve_access(ids.ArticleIds(pmid='1')) == artifacts.SourceMetadata()


async def test_resolve_access_empty_on_not_found(patch_transport: conftest.InstallTransport) -> None:
    patch_transport({f'GET {_UNPAYWALL_PATH}': [httpx.Response(404)]})
    assert await sessions.resolve_access(ids.ArticleIds(doi=_DOI), email=_EMAIL) == artifacts.SourceMetadata()
