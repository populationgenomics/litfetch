"""The fetch seam: the source backends behind the file-set.

A :class:`Fetcher` declares the identifiers it needs (``requires``) and returns
the article **body** as a :class:`~litfetch.artifacts.Blob` -- a
:class:`~litfetch.artifacts.File` plus its bytes -- without converting to
markdown.  A :class:`FileSource` lists the article's files (body renditions
*and* supplementary material, distinguished by
:class:`~litfetch.artifacts.FileKind`) and fetches any one of them, with
per-source authentication.  PMC's Open Access bucket is openly listable and
fetchable; publisher assets reuse the same ``credentials`` as their full text.

Both take the :class:`~litfetch._http.Http` to issue requests on; the ladder
walk and file-set union that drive them live as methods on
:class:`~litfetch.sessions.Session` (which supplies ``http=self``).

Registered fetchers, in priority order (:func:`default_fetchers`):

* :class:`PmcOaFetcher` -- the PMC Open Access S3 bucket (JATS body and open
  file-set).  Needs a ``pmcid``.
* :class:`EuropePmcFetcher` -- Europe PMC's REST endpoint.  Needs a ``pmcid``.
* :class:`ElsevierFetcher` -- Elsevier's article API, keyed on the caller's own
  ``credentials['elsevier_api_key']``.  Needs a ``doi``.
* :class:`SpringerFetcher` -- Springer Nature's OpenAccess JATS API, keyed on
  ``credentials['springer_api_key']``.  Needs a ``doi``.

:class:`BiorxivFetcher` (bioRxiv/medRxiv preprints; needs ``litfetch[biorxiv]``)
is *not* registered in :func:`default_fetchers` -- it uses browser-fingerprint
impersonation and an extra dependency, so a caller adds it explicitly.

PMC's S3 layout is article-versioned: each article lives at
``s3://pmc-oa-opendata/PMC{id}.{version}/``.  We probe ``PMC{id}.1.xml`` first
(the vast majority have a single version) and fall through to ``.2`` / ``.3``
for the rare correction case.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import urllib.parse
from collections.abc import Mapping
from typing import Protocol

import defusedxml.ElementTree
import httpx

from litfetch import _doi, _http, artifacts, crossref, ids, semantic_scholar, unpaywall

logger = logging.getLogger(__name__)

_PMC_S3_BASE = 'https://pmc-oa-opendata.s3.amazonaws.com'
_EUROPE_PMC_BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
_ELSEVIER_HOST = 'api.elsevier.com'
_ELSEVIER_CREDENTIAL_KEY = 'elsevier_api_key'
_S2_CREDENTIAL_KEY = 'semantic_scholar_api_key'
_SPRINGER_BASE = 'https://api.springernature.com/openaccess/jats'
_SPRINGER_CREDENTIAL_KEY = 'springer_api_key'
_SPRINGER_META_BASE = 'https://api.springernature.com/meta/v2/json'
_SPRINGER_META_CREDENTIAL_KEY = 'springer_meta_api_key'
_BIORXIV_DETAILS_BASE = 'https://api.biorxiv.org/details'
_BIORXIV_SERVERS = ('biorxiv', 'medrxiv')
# Cold Spring Harbor preprint DOI prefixes (bioRxiv/medRxiv): older 10.1101, newer 10.64898.
_BIORXIV_DOI_PREFIXES = ('10.1101/', '10.64898/')
_BIORXIV_IMPERSONATE = 'chrome'

# The default XML namespace on an S3 ListObjectsV2 response.
_S3_NS = '{http://s3.amazonaws.com/doc/2006-03-01/}'

# Versions to probe under the article-versioned layout.  PMC documents that
# "the majority of articles in PMC have a single version and it is version 1";
# the cap is a generous bound on the rare correction case.
_PMC_OA_MAX_VERSION = 3


class Fetcher(Protocol):
    """A pluggable body-retrieval backend.

    ``requires`` names the :class:`~litfetch.ids.ArticleIds` fields the fetcher
    needs to even attempt a fetch; the ladder skips it when they are absent.
    ``credentials`` carries the caller's per-user publisher keys.  ``http`` is
    the :class:`~litfetch._http.Http` to issue requests on -- the session running
    the ladder supplies it.
    """

    name: str
    requires: frozenset[str]

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the body Blob; return ``None`` to defer to the next fetcher."""
        ...


class FileSource(Protocol):
    """Enumerates and materialises the files in an article's file-set.

    ``list_files`` returns both body renditions and supplementary material as
    :class:`~litfetch.artifacts.File` references, each tagged with its
    :class:`~litfetch.artifacts.FileKind`; ``fetch_file`` downloads one of them.
    Authentication is per-source and reuses the same ``credentials`` map as body
    fetching; :func:`fetch_file` routes a file back to its source by
    :attr:`~litfetch.artifacts.File.source`.
    """

    name: str

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """Enumerate the article's file references (no bytes)."""
        ...

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download one file's bytes as a Blob; ``None`` when unavailable."""
        ...


def _pmc_numeric(pmc_id: str) -> str:
    """Return the PMC ID with any leading ``PMC`` stripped."""
    s = pmc_id.strip()
    if s.upper().startswith('PMC'):
        return s[3:]
    return s


def _pmc_versioned_xml_url(numeric: str, version: int) -> str:
    """Construct the JATS XML URL for ``PMC{numeric}.{version}``."""
    stem = f'PMC{numeric}.{version}'
    return f'{_PMC_S3_BASE}/{stem}/{stem}.xml'


def _is_article_rendition(key: str) -> bool:
    """Report whether an S3 key is an alternate rendition of the article body.

    PMC stores the body under ``PMC{id}.{v}/`` as several stem-named renditions
    -- ``PMC{id}.{v}.xml`` (JATS), ``.pdf``, ``.txt``, ``.json`` -- each sharing
    the folder's stem.  Those are body Files; anything else under the prefix
    (figures, datasets, media) is supplementary.
    """
    parts = key.split('/')
    if len(parts) != 2:
        return False
    folder, filename = parts
    return filename.rsplit('.', 1)[0] == folder


async def fetch_jats_xml(
    pmc_id: str,
    *,
    http: _http.Http,
) -> tuple[bytes, str] | None:
    """Fetch the JATS XML for ``pmc_id`` from PMC's public S3 bucket.

    Probes the article-versioned layout starting at ``.1`` and falling through
    to ``.2`` / ``.3`` on 404.  Returns ``(xml_bytes, source_url)`` on the first
    200, or ``None`` when no version is present in the bucket.
    """
    numeric = _pmc_numeric(pmc_id)
    for version in range(1, _PMC_OA_MAX_VERSION + 1):
        url = _pmc_versioned_xml_url(numeric, version)
        try:
            resp = await http.get(url)
        except httpx.HTTPError:
            logger.exception('PMC OA fetch failed for %s', url)
            continue
        if resp.status_code == 200:
            return resp.content, url
        if resp.status_code != 404:
            logger.warning('Unexpected status %d from %s', resp.status_code, url)
    return None


async def crossref_elsevier_xml_link(http: _http.Http, doi: str) -> str | None:
    """Return the Elsevier text/xml TDM link for ``doi`` via Crossref.

    Crossref records publisher text-mining links in ``message.link[]``;
    Elsevier-hosted articles carry a ``text/xml`` entry pointing at
    ``api.elsevier.com/content/article/PII:...``.  This both identifies the
    article as Elsevier-hosted and hands us the exact fetch URL.  Returns
    ``None`` for non-Elsevier DOIs.
    """
    message = await crossref.fetch_work(doi, http=http)
    if message is None:
        return None
    for link in message.get('link', []) or []:
        url = link.get('URL', '')
        if link.get('content-type') == 'text/xml' and urllib.parse.urlparse(url).netloc.endswith(_ELSEVIER_HOST):
            return url
    return None


def _elsevier_has_body(xml_bytes: bytes) -> bool:
    """Report whether an Elsevier article XML response carries full text.

    Full text is wrapped in ``<ce:sections>`` containing ``<ce:para>``
    elements; an unentitled response (e.g. fetched from a non-institutional IP)
    is coredata + a ``<dc:description>`` abstract only.  Body presence -- not
    the ``openaccess`` flag -- is the gate: the OA-only guarantee is enforced
    at the deploy layer (the caller's egress IP).
    """
    return b'<ce:sections' in xml_bytes or xml_bytes.count(b'<ce:para') >= 3


def _extract_jats_article(content: bytes) -> bytes | None:
    """Slice the JATS ``<article>`` out of a Springer OpenAccess response.

    The OpenAccess ``/jats`` payload wraps the article in a ``<response>``
    envelope behind a DOCTYPE that declares parameter entities -- which
    ``defusedxml`` (and any hardened downstream parser) refuses.  Slicing the
    ``<article>`` element out by bytes yields self-contained JATS (its
    namespaces live on the element) and drops both the envelope and the
    entity-declaring DOCTYPE.  Body presence is the gate, not an OA flag,
    matching the Elsevier path; returns ``None`` for a non-OA/absent article.
    """
    # Anchor on `<article` followed by whitespace or `>` so a wrapper element
    # like `<article-set>` (or `<article-meta>`) can't be mistaken for the root.
    opening = re.search(rb'<article[\s>]', content)
    end = content.rfind(b'</article>')
    if opening is None or end < opening.start():
        return None
    article = content[opening.start() : end + len(b'</article>')]
    if b'<body' not in article:
        return None
    return b"<?xml version='1.0' encoding='UTF-8'?>\n" + article


async def _download(http: _http.Http, file: artifacts.File, *, what: str) -> artifacts.Blob | None:
    """GET ``file.uri`` and wrap the bytes as a Blob; ``None`` on error or non-200.

    Shared by the openly-hosted file sources (PMC renditions, OA PDFs).  ``what``
    labels the log line on a transport error.
    """
    if file.uri is None:
        return None
    try:
        # Publisher PDF links commonly redirect (openURL -> content/pdf, ...); follow them.
        resp = await http.get(file.uri, follow_redirects=True)
    except httpx.HTTPError:
        logger.exception('%s fetch failed for %s', what, file.uri)
        return None
    if resp.status_code != 200:
        return None
    return artifacts.Blob(file=file, content=resp.content)


class PmcOaFetcher:
    """The PMC Open Access S3 bucket: JATS body and open file-set.

    Implements both :class:`Fetcher` and :class:`FileSource`.  The bucket is
    openly accessible, so neither path consults ``credentials``.
    """

    name = 'pmc_oa_s3'
    requires = frozenset({'pmcid'})

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the article-versioned JATS body for ``article_ids.pmcid``."""
        del credentials  # open bucket, no key
        if article_ids.pmcid is None:
            return None
        fetched = await fetch_jats_xml(article_ids.pmcid, http=http)
        if fetched is None:
            return None
        xml_bytes, source_url = fetched
        return artifacts.Blob(
            file=artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.JATS_XML,
                uri=source_url,
            ),
            content=xml_bytes,
        )

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """List every file under the article's S3 prefix, tagged by kind.

        Stem renditions (``PMC{id}.{v}.xml`` / ``.pdf`` / ...) are ``BODY``;
        everything else under the prefix is ``SUPPLEMENTARY``.  One listing
        serves both axes.
        """
        del credentials  # open bucket
        if article_ids.pmcid is None:
            return ()
        numeric = _pmc_numeric(article_ids.pmcid)
        keys = await self._list_keys(http, f'PMC{numeric}.')
        files = []
        for key, size in keys:
            rendition = _is_article_rendition(key)
            media_type = mimetypes.guess_type(key)[0]
            files.append(
                artifacts.File(
                    kind=artifacts.FileKind.BODY if rendition else artifacts.FileKind.SUPPLEMENTARY,
                    source=self.name,
                    media_type=media_type or ('application/octet-stream' if rendition else None),
                    uri=f'{_PMC_S3_BASE}/{urllib.parse.quote(key)}',
                    filename=key.rsplit('/', 1)[-1],
                    credential_key=None,
                    size_bytes=size,
                )
            )
        return tuple(files)

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download an open PMC file (body rendition or supplementary) by ``uri``."""
        del credentials  # open bucket
        return await _download(http, file, what='PMC file')

    async def _list_keys(self, http: _http.Http, prefix: str) -> list[tuple[str, int | None]]:
        """List ``(key, size)`` under ``prefix`` via S3 ListObjectsV2, paging fully."""
        keys: list[tuple[str, int | None]] = []
        token: str | None = None
        while True:
            params = {'list-type': '2', 'prefix': prefix}
            if token:
                params['continuation-token'] = token
            try:
                resp = await http.get(f'{_PMC_S3_BASE}/', params=params)
            except httpx.HTTPError:
                logger.exception('PMC OA list failed for prefix %s', prefix)
                return keys
            if resp.status_code != 200:
                logger.warning('Unexpected status %d listing PMC OA prefix %s', resp.status_code, prefix)
                return keys
            root = defusedxml.ElementTree.fromstring(resp.content)
            for contents in root.findall(f'{_S3_NS}Contents'):
                key_el = contents.find(f'{_S3_NS}Key')
                if key_el is None or not key_el.text:
                    continue
                size_el = contents.find(f'{_S3_NS}Size')
                size = int(size_el.text) if size_el is not None and size_el.text else None
                keys.append((key_el.text, size))
            token = root.findtext(f'{_S3_NS}NextContinuationToken')
            if root.findtext(f'{_S3_NS}IsTruncated') != 'true' or not token:
                return keys


class EuropePmcFetcher:
    """The Europe PMC REST source.

    A single GET against ``/{pmc_id}/fullTextXML``.  Europe PMC mirrors PMC and
    additionally serves UK funder-deposited Author Manuscripts plus articles
    with direct EBI publisher arrangements.  pmid -> pmcid resolution lives in
    :class:`~litfetch.resolvers.EuropePmcResolver`, not here.
    """

    name = 'europe_pmc'
    requires = frozenset({'pmcid'})

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the Europe PMC full-text body for ``article_ids.pmcid``."""
        del credentials  # unused by this source
        if article_ids.pmcid is None:
            return None
        numeric = _pmc_numeric(article_ids.pmcid)
        url = f'{_EUROPE_PMC_BASE}/PMC{numeric}/fullTextXML'
        try:
            resp = await http.get(url)
        except httpx.HTTPError:
            logger.exception('Europe PMC fetch failed for %s', url)
            return None
        if resp.status_code != 200 or not resp.content:
            if resp.status_code not in (200, 404):
                logger.warning('Unexpected status %d from Europe PMC for %s', resp.status_code, url)
            return None
        return artifacts.Blob(
            file=artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.JATS_XML,
                uri=url,
            ),
            content=resp.content,
        )


class ElsevierFetcher:
    """Elsevier full-text source via the article TDM API.

    Resolves the Elsevier ``text/xml`` link through Crossref (which also
    confirms the article is Elsevier-hosted), fetches it with the caller's own
    API key (``credentials['elsevier_api_key']`` -- a self-serve
    dev.elsevier.com key, per-user, no service-level shared key), and returns
    the ce:/ja: XML body for later conversion.  Returns ``None`` for non-Elsevier
    DOIs, when the caller supplied no Elsevier key, or when the response carries
    no body.
    """

    name = 'elsevier_oa'
    requires = frozenset({'doi'})

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the Elsevier article body for ``article_ids.doi``."""
        raw_key = (credentials or {}).get(_ELSEVIER_CREDENTIAL_KEY)
        api_key = raw_key if isinstance(raw_key, str) and raw_key else None
        if api_key is None or article_ids.doi is None:
            return None
        link = await crossref_elsevier_xml_link(http, article_ids.doi)
        if link is None:
            return None
        try:
            resp = await http.get(link, headers={'X-ELS-APIKey': api_key, 'Accept': 'text/xml'})
        except httpx.HTTPError:
            logger.exception('Elsevier fetch failed for %s', link)
            return None
        if resp.status_code != 200 or not resp.content or not _elsevier_has_body(resp.content):
            return None
        return artifacts.Blob(
            file=artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.ELSEVIER_XML,
                uri=link,
            ),
            content=resp.content,
        )


class SpringerFetcher:
    """Springer Nature Open Access full text (JATS) via the OpenAccess API.

    Keyed on the caller's own dev.springernature.com key
    (``credentials['springer_api_key']`` -- per-user, no shared service key).
    Queries the OpenAccess JATS endpoint by DOI and returns the JATS response
    bytes when they carry an ``<article>`` with a ``<body>``; ``None`` for a
    non-OA/absent article, no key, or no DOI.  Body presence is the gate, not an
    OA flag, matching the Elsevier path.
    """

    name = 'springer_oa'
    requires = frozenset({'doi'})

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the Springer OA article body for ``article_ids.doi``."""
        raw_key = (credentials or {}).get(_SPRINGER_CREDENTIAL_KEY)
        api_key = raw_key if isinstance(raw_key, str) and raw_key else None
        if api_key is None or article_ids.doi is None:
            return None
        query = f'doi:{article_ids.doi}'
        try:
            resp = await http.get(_SPRINGER_BASE, params={'q': query, 'api_key': api_key})
        except httpx.HTTPError:
            logger.exception('Springer fetch failed for %s', article_ids.doi)
            return None
        if resp.status_code != 200 or not resp.content:
            return None
        article = _extract_jats_article(resp.content)
        if article is None:
            return None
        return artifacts.Blob(
            file=artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.JATS_XML,
                # The request URL carries the secret api_key; record the key-free query instead.
                uri=f'{_SPRINGER_BASE}?q={query}',
            ),
            content=article,
        )


async def _fetch_impersonated(url: str, *, impersonate: str) -> bytes | None:
    """GET ``url`` with a browser TLS fingerprint via curl_cffi.

    bioRxiv's JATS host sits behind Cloudflare's fingerprint gate, which a plain
    httpx client trips; curl_cffi impersonates a real browser's TLS/HTTP-2
    fingerprint to pass it.  Raises a clear error when the optional extra is
    absent; returns ``None`` on a transport error or non-200.
    """
    try:
        from curl_cffi import requests  # noqa: PLC0415 -- optional dep, imported lazily
    except ImportError as e:
        raise RuntimeError('curl_cffi is not installed; install litfetch[biorxiv]') from e
    try:
        async with requests.AsyncSession() as session:
            resp = await session.get(url, impersonate=impersonate, timeout=_http.DEFAULT_TIMEOUT)  # type: ignore[arg-type]
    except Exception:
        logger.exception('bioRxiv impersonated fetch failed for %s', url)
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    return resp.content


class BiorxivFetcher:
    """bioRxiv / medRxiv preprint full text (opt-in; needs ``litfetch[biorxiv]``).

    Preprints carry a Cold Spring Harbor DOI (``10.1101/`` or ``10.64898/``).  The
    details API yields the latest version's ``jatsxml`` link; that JATS host is
    Cloudflare-gated, so the body is fetched with a browser TLS fingerprint
    (curl_cffi).  The XML is HighWire-produced structured JATS, so a litdown
    rendering is ``xml-faithful`` -- though the conversion is bioRxiv's
    best-effort, a provenance the ``biorxiv`` source records.  Kept *off*
    :func:`default_fetchers` (impersonation + an extra dependency): add it
    explicitly.  Returns ``None`` for a non-preprint DOI or when no JATS is on
    offer; raises if curl_cffi is absent when a fetch is actually attempted.
    """

    name = 'biorxiv'
    requires = frozenset({'doi'})

    def __init__(self, *, impersonate: str = _BIORXIV_IMPERSONATE) -> None:
        self._impersonate = impersonate

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Fetch the latest-version JATS for a CSH-prefix ``article_ids.doi``."""
        del credentials  # open preprint server, no key
        doi = article_ids.doi
        if doi is None or not doi.startswith(_BIORXIV_DOI_PREFIXES):
            return None
        jats_url = await self._jats_url(http, doi)
        if jats_url is None:
            return None
        content = await _fetch_impersonated(jats_url, impersonate=self._impersonate)
        if content is None:
            return None
        return artifacts.Blob(
            file=artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.JATS_XML,
                uri=jats_url,
            ),
            content=content,
        )

    async def _jats_url(self, http: _http.Http, doi: str) -> str | None:
        """Return the latest version's ``jatsxml`` link, trying biorxiv then medrxiv.

        The details API is not Cloudflare-gated, so this uses the shared session;
        only the JATS body itself needs the impersonated fetch.
        """
        for server in _BIORXIV_SERVERS:
            url = f'{_BIORXIV_DETAILS_BASE}/{server}/{_doi.encode_doi_path(doi)}'
            try:
                resp = await http.get(url)
            except httpx.HTTPError:
                logger.exception('bioRxiv details lookup failed for %s', url)
                continue
            if resp.status_code != 200:
                continue
            try:
                collection = resp.json().get('collection') or []
            except ValueError:
                logger.warning('bioRxiv details returned a non-JSON response for %s', url)
                continue
            if collection and collection[-1].get('jatsxml'):
                return collection[-1]['jatsxml']
        return None


class UnpaywallFileSource:
    """Unpaywall's best-OA-location PDF as a ``BODY`` file-set rendition.

    Reuses the DOI-keyed Unpaywall record (shared with
    :func:`~litfetch.source_metadata.resolve_access` -- inside a session scope
    the record is fetched once).  Lists a single ``application/pdf`` ``BODY``
    :class:`~litfetch.artifacts.File` when the record has a
    ``best_oa_location.url_for_pdf``, empty otherwise.  Needs no credential;
    ``email`` identifies the caller per Unpaywall's policy and defaults to the
    session ``contact`` (Unpaywall requires it -- no email, no listing).

    A discovered PDF is an *additional* file-set member, never the goal of the
    XML body ladder (see ``docs/source-expansion-plan.md``).
    """

    name = 'unpaywall'

    def __init__(self, *, email: str | None = None) -> None:
        self._email = email

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """List the best-OA PDF as a BODY File, or nothing when there is none."""
        del credentials  # no key; email identifies the caller
        record = await unpaywall.fetch_record(article_ids, http=http, email=self._email)
        if record is None:
            return ()
        best = record.get('best_oa_location') or {}
        pdf_url = best.get('url_for_pdf')
        if not pdf_url:
            return ()
        return (
            artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.PDF,
                uri=pdf_url,
            ),
        )

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download the OA PDF by ``uri``."""
        del credentials  # openly hosted OA PDF
        return await _download(http, file, what='Unpaywall PDF')


class SemanticScholarFileSource:
    """Semantic Scholar's open-access PDF as a ``BODY`` file-set rendition.

    A paper lookup for the ``openAccessPdf`` field yields a PDF URL when S2 knows
    an OA copy.  Lists a single ``application/pdf`` ``BODY``
    :class:`~litfetch.artifacts.File`, empty otherwise.  An optional S2 API key
    in ``credentials['semantic_scholar_api_key']`` raises the request pace; the
    keyless endpoint works without it.

    A discovered PDF is an *additional* file-set member, never the goal of the
    XML body ladder (see ``docs/source-expansion-plan.md``).
    """

    name = 'semantic_scholar'

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """List S2's open-access PDF as a BODY File, or nothing when there is none."""
        raw_key = (credentials or {}).get(_S2_CREDENTIAL_KEY)
        api_key = raw_key if isinstance(raw_key, str) and raw_key else None
        record = await semantic_scholar.fetch_paper(article_ids, http=http, fields='openAccessPdf', api_key=api_key)
        if record is None:
            return ()
        pdf_url = (record.get('openAccessPdf') or {}).get('url')
        if not pdf_url:
            return ()
        return (
            artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.PDF,
                uri=pdf_url,
            ),
        )

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download the OA PDF by ``uri``."""
        del credentials  # openly hosted OA PDF
        return await _download(http, file, what='Semantic Scholar PDF')


class CrossrefFileSource:
    """Crossref text-mining links as ``BODY`` file-set renditions.

    Crossref records publisher text-mining links in ``message.link[]`` flagged
    ``intended-application: text-mining`` -- a full-text PDF and/or XML URL.
    Lists each as a ``BODY`` :class:`~litfetch.artifacts.File` with
    ``media_type`` from its ``content-type``; fetching one may need the
    publisher entitlement (egress IP or a TDM token), enforced upstream, so the
    ref is surfaced regardless.  Needs a ``doi``.
    """

    name = 'crossref_tdm'

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """List the text-mining links Crossref advertises, or nothing."""
        del credentials  # the Crossref lookup itself is open
        if not article_ids.doi:
            return ()
        message = await crossref.fetch_work(article_ids.doi, http=http)
        if message is None:
            return ()
        files = []
        for link in message.get('link', []) or []:
            url = link.get('URL')
            if link.get('intended-application') != 'text-mining' or not url:
                continue
            content_type = link.get('content-type')
            files.append(
                artifacts.File(
                    kind=artifacts.FileKind.BODY,
                    source=self.name,
                    media_type=content_type if content_type and content_type != 'unspecified' else None,
                    uri=url,
                )
            )
        return tuple(files)

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download the TDM link by ``uri`` (may 403 without entitlement)."""
        del credentials  # entitlement is by egress IP / upstream token, not a litfetch key
        return await _download(http, file, what='Crossref TDM file')


async def _springer_meta_pdf(http: _http.Http, doi: str, api_key: str) -> tuple[str, bool] | None:
    """Return ``(pdf_url, is_open_access)`` from the Springer Meta record, or ``None``.

    The Meta record's ``url`` list carries an ``openURL`` PDF entry
    (``link.springer.com/openurl/pdf?id=doi:<doi>``); ``openaccess`` is the OA
    flag.  Returns ``None`` when the lookup fails or no PDF url is present.
    """
    try:
        resp = await http.get(_SPRINGER_META_BASE, params={'q': f'doi:{doi}', 'api_key': api_key})
    except httpx.HTTPError:
        logger.exception('Springer Meta request failed for %s', doi)
        return None
    if resp.status_code != 200:
        return None
    try:
        records = resp.json().get('records') or []
    except ValueError:
        logger.warning('Springer Meta returned a non-JSON response for %s', doi)
        return None
    if not records:
        return None
    record = records[0]
    pdf_url = next(
        (u.get('value') for u in (record.get('url') or []) if u.get('format') == 'pdf' and u.get('value')), None
    )
    if not pdf_url:
        return None
    return pdf_url, record.get('openaccess') == 'true'


class SpringerFileSource:
    """Springer's article PDF as a ``BODY`` file-set rendition, via the Meta API.

    The Meta API (``credentials['springer_meta_api_key']`` -- distinct from the
    OpenAccess key) yields a stable openURL PDF link plus the ``openaccess`` flag
    for any Springer DOI, OA or subscription.  Lists an ``application/pdf``
    ``BODY`` :class:`~litfetch.artifacts.File`; an OA article is openly fetchable
    (``credential_key=None``), a subscription one is marked
    :data:`~litfetch.artifacts.INSTITUTIONAL` so the consumer routes the fetch
    through its entitled (EZproxy-style) client.  The openURL redirects to the
    PDF, so :func:`_download` follows redirects.
    """

    name = 'springer'

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> tuple[artifacts.File, ...]:
        """List the Springer PDF, marking it entitled when the article is not OA."""
        raw_key = (credentials or {}).get(_SPRINGER_META_CREDENTIAL_KEY)
        api_key = raw_key if isinstance(raw_key, str) and raw_key else None
        if api_key is None or article_ids.doi is None:
            return ()
        found = await _springer_meta_pdf(http, article_ids.doi, api_key)
        if found is None:
            return ()
        pdf_url, is_open_access = found
        return (
            artifacts.File(
                kind=artifacts.FileKind.BODY,
                source=self.name,
                media_type=artifacts.PDF,
                uri=pdf_url,
                credential_key=None if is_open_access else artifacts.INSTITUTIONAL,
            ),
        )

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: _http.Http,
    ) -> artifacts.Blob | None:
        """Download the PDF by ``uri`` (following the openURL redirect).

        An OA article fetches directly; a subscription one succeeds only when
        ``http`` is an entitled client (the ``INSTITUTIONAL`` marker is the
        consumer's cue to route it so).
        """
        del credentials  # entitlement is by the routed client, not a litfetch key
        return await _download(http, file, what='Springer PDF')


def default_fetchers() -> tuple[Fetcher, ...]:
    """Return the production fetcher list, in priority order.

    Kept as a function so callers can prepend their own fetcher (e.g. a
    read-only cache) without import-time side effects.  The Elsevier fetcher
    sits last and reads its key from ``credentials``; a caller with no Elsevier
    key makes it a no-op.
    """
    return (PmcOaFetcher(), EuropePmcFetcher(), ElsevierFetcher(), SpringerFetcher())


def default_file_sources() -> tuple[FileSource, ...]:
    """Return the file sources a session queries by default.

    PMC's Open Access bucket (JATS body, PDF rendition, and supplementary
    material); Unpaywall and Semantic Scholar (a best-OA PDF for the non-PMC long
    tail); Crossref TDM links; and Springer (an openURL PDF via the Meta API,
    marked :data:`~litfetch.artifacts.INSTITUTIONAL` when the article is not OA).
    A source with no usable identifier or credential is simply a no-op.
    """
    return (
        PmcOaFetcher(),
        UnpaywallFileSource(),
        SemanticScholarFileSource(),
        CrossrefFileSource(),
        SpringerFileSource(),
    )
