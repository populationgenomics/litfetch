"""Access terms (licence / OA status) for a fetched artifact.

litfetch owns access terms (see ``CONTEXT.md``): the licence under which the
bytes it fetched may be used.  Two paths, distinguished by
:attr:`~litfetch.artifacts.SourceMetadata.basis`:

* :func:`extract_source_metadata` reads the licence *from the artifact itself* --
  the JATS ``<permissions>/<license>`` or the Elsevier ``<openaccessUserLicense>``
  -- with ``basis='artifact'`` (authoritative for exactly those bytes).
* :func:`resolve_access` asserts the licence / OA status from **Unpaywall**,
  keyed on the DOI, with ``basis='unpaywall'`` -- for a paper whose bytes carry
  none (a PDF).

Both return the licence *raw*; mapping to an SPDX id is the consumer's.
"""

from __future__ import annotations

import logging

import defusedxml.ElementTree

from litfetch import _http, artifacts, ids, unpaywall

logger = logging.getLogger(__name__)


def _localname(tag: str) -> str:
    """Return an XML tag/attribute name without its namespace."""
    return tag.rsplit('}', 1)[-1]


def extract_source_metadata(blob: artifacts.Blob) -> artifacts.SourceMetadata:
    """Read access terms from a body ``blob``, dispatched on its media type.

    Returns a :class:`~litfetch.artifacts.SourceMetadata` with ``basis='artifact'``
    when a licence is present in the bytes, or an empty one (all ``None``) for a
    media type that carries none (e.g. PDF) or when nothing is found.
    """
    media_type = blob.file.media_type
    if media_type == artifacts.JATS_XML:
        return _from_jats(blob.content)
    if media_type == artifacts.ELSEVIER_XML:
        return _from_elsevier(blob.content)
    return artifacts.SourceMetadata()


def _from_jats(content: bytes) -> artifacts.SourceMetadata:
    """Extract the licence from a JATS ``<permissions>/<license>``.

    Prefers the ``xlink:href`` (the canonical CC URL), then ``license-type``,
    then the licence paragraph text.  ``access`` is flagged open only when the
    ``license-type`` itself says so -- OA status proper comes from an authority.
    """
    try:
        root = defusedxml.ElementTree.fromstring(content)
    except Exception:
        logger.exception('JATS source-metadata parse failed')
        return artifacts.SourceMetadata()
    for el in root.iter():
        if _localname(el.tag) != 'license':
            continue
        href = next((v for k, v in el.attrib.items() if _localname(k) == 'href'), None)
        license_type = el.attrib.get('license-type')
        text = ' '.join(el.itertext()).strip() or None
        licence = href or license_type or text
        access = 'open-access' if license_type and 'open' in license_type.lower() else None
        if licence or access:
            return artifacts.SourceMetadata(licence=licence, access=access, basis='artifact')
    return artifacts.SourceMetadata()


def _from_elsevier(content: bytes) -> artifacts.SourceMetadata:
    """Extract the licence from an Elsevier ``<openaccessUserLicense>`` + ``<openaccess>``."""
    try:
        root = defusedxml.ElementTree.fromstring(content)
    except Exception:
        logger.exception('Elsevier source-metadata parse failed')
        return artifacts.SourceMetadata()
    licence: str | None = None
    access: str | None = None
    for el in root.iter():
        name = _localname(el.tag)
        if name == 'openaccessUserLicense' and el.text and el.text.strip():
            licence = el.text.strip()
        elif name == 'openaccess' and el.text and el.text.strip() in ('1', 'true'):
            access = 'open-access'
    if licence or access:
        return artifacts.SourceMetadata(licence=licence, access=access, basis='artifact')
    return artifacts.SourceMetadata()


async def resolve_access(
    article_ids: ids.ArticleIds,
    *,
    http: _http.Http,
    email: str = _http.CONTACT_EMAIL,
) -> artifacts.SourceMetadata:
    """Resolve a paper's licence / OA status from Unpaywall, keyed on its DOI.

    For papers whose bytes carry no licence (a PDF), this asserts one from an
    external authority.  Returns :class:`~litfetch.artifacts.SourceMetadata` with
    ``basis='unpaywall'`` -- ``licence`` from the best OA location's raw
    ``license``, ``access`` from the raw ``oa_status`` -- or an empty one when
    there is no DOI, the lookup fails, or Unpaywall has no record.  ``http`` is
    the :class:`~litfetch._http.Http` to issue the request on; ``email``
    identifies the caller per Unpaywall's usage policy.
    """
    data = await unpaywall.fetch_record(article_ids, http=http, email=email)
    if data is None:
        return artifacts.SourceMetadata()
    best = data.get('best_oa_location') or {}
    licence = best.get('license') or None
    access = data.get('oa_status') or None
    if licence is None and access is None:
        return artifacts.SourceMetadata()
    return artifacts.SourceMetadata(licence=licence, access=access, basis='unpaywall')
