"""Identifier bundle -> full-text markdown: the pluggable source ladder.

A dispatcher walks a list of :class:`FullTextSource` backends and returns the
first non-``None`` result.  Each source declares the identifiers it can act on
via :attr:`FullTextSource.requires`; the dispatcher skips a source whose
requirements the current :class:`~litfetch.ids.ArticleIds` does not satisfy.

Registered sources, in priority order (:func:`default_sources`):

* :class:`PmcOaSource` -- the PMC Open Access S3 bucket; the bulk of
  NIH-deposited content as JATS XML.  Needs a ``pmcid``.
* :class:`EuropePmcSource` -- Europe PMC's REST endpoint; UK funder-deposited
  Author Manuscripts and articles with direct EBI arrangements.  Needs a
  ``pmcid`` (pmid -> pmcid resolution lives in
  :class:`~litfetch.resolvers.EuropePmcResolver`).
* :class:`ElsevierOaSource` -- Elsevier's article API, keyed on the caller's
  own ``credentials['elsevier_api_key']``; recovers open-access articles not
  deposited in PMC.  Needs a ``doi``.

This module is consumer-agnostic: it neither resolves identifiers (callers
inject a resolver -- see :func:`~litfetch.get_full_text`) nor sources publisher
keys (callers pass ``credentials``).

PMC's S3 layout is article-versioned: each article lives at
``s3://pmc-oa-opendata/PMC{id}.{version}/``.  We probe ``PMC{id}.1.xml`` first
(the vast majority have a single version) and fall through to ``.2`` / ``.3``
for the rare correction case.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Mapping, Sequence
from typing import NamedTuple, Protocol
from urllib.parse import urlparse

import httpx
import litdown

from litfetch._http import USER_AGENT, client_ctx
from litfetch.ids import ArticleIds
from litfetch.resolvers import Resolver

logger = logging.getLogger(__name__)

_CONTACT_EMAIL = 'toby.sargeant@populationgenomics.org.au'
_PMC_S3_BASE = 'https://pmc-oa-opendata.s3.amazonaws.com'
_EUROPE_PMC_BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
_CROSSREF_BASE = 'https://api.crossref.org/works'
_ELSEVIER_HOST = 'api.elsevier.com'

# Versions to probe under the article-versioned layout.  PMC documents that
# "the majority of articles in PMC have a single version and it is version 1";
# the cap is a generous bound on the rare correction case rather than a guess
# at how many versions to expect.
_PMC_OA_MAX_VERSION = 3


class FullTextResult(NamedTuple):
    """A successful full-text retrieval.

    ``source`` is the registered source name (e.g. ``'pmc_oa_s3'``) and
    ``source_format`` is the raw upstream representation (``'jats'`` for JATS
    XML, ``'elsevier-xml'`` for Elsevier ce:/ja:) before conversion to
    markdown.  ``pmc_id`` is populated when the source acted on one; sources
    keyed on DOI leave it ``None``.
    """

    markdown: str
    source: str
    source_format: str
    source_url: str
    pmc_id: str | None = None


class FullTextSource(Protocol):
    """A pluggable full-text retrieval backend.

    ``requires`` names the :class:`~litfetch.ids.ArticleIds` fields the source
    needs to even attempt a fetch; the dispatcher skips the source when they
    are absent.  ``credentials`` carries the caller's per-user publisher keys.
    """

    name: str
    requires: frozenset[str]

    async def try_fetch(
        self,
        ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FullTextResult | None:
        """Attempt a fetch; return ``None`` to defer to the next source."""
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


def jats_to_markdown(xml_bytes: bytes) -> str:
    """Convert scholarly full-text XML bytes to markdown via litdown.

    litdown sniffs the document root and dispatches to its JATS or Elsevier
    (ce:/ja:) dialect.  ``convert`` is declared as taking ``str | Path`` but
    reaches for ``defusedxml.ElementTree.parse``, which accepts any file-like
    object -- feeding it a ``BytesIO`` keeps everything in memory.
    """
    return litdown.convert(io.BytesIO(xml_bytes))  # type: ignore[arg-type]


async def fetch_jats_xml(
    pmc_id: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str] | None:
    """Fetch the JATS XML for ``pmc_id`` from PMC's public S3 bucket.

    Probes the article-versioned layout starting at ``.1`` and falling through
    to ``.2`` / ``.3`` on 404.  Returns ``(xml_bytes, source_url)`` on the
    first 200, or ``None`` when no version is present in the bucket.
    """
    numeric = _pmc_numeric(pmc_id)
    async with client_ctx(http_client) as c:
        for version in range(1, _PMC_OA_MAX_VERSION + 1):
            url = _pmc_versioned_xml_url(numeric, version)
            try:
                resp = await c.get(url)
            except httpx.HTTPError:
                logger.exception('PMC OA fetch failed for %s', url)
                continue
            if resp.status_code == 200:
                return resp.content, url
            if resp.status_code != 404:
                logger.warning('Unexpected status %d from %s', resp.status_code, url)
    return None


class PmcOaSource:
    """The PMC Open Access S3 bucket source."""

    name = 'pmc_oa_s3'
    requires = frozenset({'pmcid'})

    async def try_fetch(
        self,
        ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FullTextResult | None:
        """Fetch article-versioned JATS for ``ids.pmcid`` and convert it."""
        del credentials  # unused by this source
        if ids.pmcid is None:
            return None
        fetched = await fetch_jats_xml(ids.pmcid, http_client=http_client)
        if fetched is None:
            return None
        xml_bytes, source_url = fetched
        return FullTextResult(
            markdown=jats_to_markdown(xml_bytes),
            source=self.name,
            source_format='jats',
            source_url=source_url,
            pmc_id=ids.pmcid,
        )


class EuropePmcSource:
    """The Europe PMC REST source.

    A single GET against ``/{pmc_id}/fullTextXML``.  Europe PMC mirrors PMC and
    additionally serves UK funder-deposited Author Manuscripts plus articles
    with direct EBI publisher arrangements.  pmid -> pmcid resolution lives in
    :class:`~litfetch.resolvers.EuropePmcResolver`, not here.
    """

    name = 'europe_pmc'
    requires = frozenset({'pmcid'})

    async def try_fetch(
        self,
        ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FullTextResult | None:
        """Fetch the Europe PMC full-text XML for ``ids.pmcid``."""
        del credentials  # unused by this source
        if ids.pmcid is None:
            return None
        numeric = _pmc_numeric(ids.pmcid)
        url = f'{_EUROPE_PMC_BASE}/PMC{numeric}/fullTextXML'
        async with client_ctx(http_client) as c:
            try:
                resp = await c.get(url, headers={'User-Agent': USER_AGENT})
            except httpx.HTTPError:
                logger.exception('Europe PMC fetch failed for %s', url)
                return None
        if resp.status_code != 200 or not resp.content:
            if resp.status_code not in (200, 404):
                logger.warning('Unexpected status %d from Europe PMC for %s', resp.status_code, url)
            return None
        return FullTextResult(
            markdown=jats_to_markdown(resp.content),
            source=self.name,
            source_format='jats',
            source_url=url,
            pmc_id=f'PMC{numeric}',
        )


async def crossref_elsevier_xml_link(c: httpx.AsyncClient, doi: str) -> str | None:
    """Return the Elsevier text/xml TDM link for ``doi`` via Crossref.

    Crossref records publisher text-mining links in ``message.link[]``;
    Elsevier-hosted articles carry a ``text/xml`` entry pointing at
    ``api.elsevier.com/content/article/PII:...``.  This both identifies the
    article as Elsevier-hosted and hands us the exact fetch URL.  Returns
    ``None`` for non-Elsevier DOIs.
    """
    try:
        resp = await c.get(f'{_CROSSREF_BASE}/{doi}', params={'mailto': _CONTACT_EMAIL})
    except httpx.HTTPError:
        logger.exception('Crossref lookup failed for %s', doi)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    for link in data.get('message', {}).get('link', []) or []:
        url = link.get('URL', '')
        if link.get('content-type') == 'text/xml' and urlparse(url).netloc.endswith(_ELSEVIER_HOST):
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


class ElsevierOaSource:
    """Elsevier full-text source via the article TDM API.

    Resolves the Elsevier ``text/xml`` link through Crossref (which also
    confirms the article is Elsevier-hosted), fetches it with the caller's own
    API key (``credentials['elsevier_api_key']`` -- a self-serve
    dev.elsevier.com key, per-user, no service-level shared key), and converts
    the ce:/ja: XML to markdown.  Returns ``None`` for non-Elsevier DOIs, when
    the caller supplied no Elsevier key, or when the response carries no body.
    """

    name = 'elsevier_oa'
    requires = frozenset({'doi'})
    _CREDENTIAL_KEY = 'elsevier_api_key'

    async def try_fetch(
        self,
        ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> FullTextResult | None:
        """Fetch the Elsevier article XML for ``ids.doi`` and convert it."""
        raw_key = (credentials or {}).get(self._CREDENTIAL_KEY)
        api_key = raw_key if isinstance(raw_key, str) and raw_key else None
        if api_key is None or ids.doi is None:
            return None
        async with client_ctx(http_client) as c:
            link = await crossref_elsevier_xml_link(c, ids.doi)
            if link is None:
                return None
            try:
                resp = await c.get(link, headers={'X-ELS-APIKey': api_key, 'Accept': 'text/xml'})
            except httpx.HTTPError:
                logger.exception('Elsevier fetch failed for %s', link)
                return None
        if resp.status_code != 200 or not resp.content or not _elsevier_has_body(resp.content):
            return None
        markdown = jats_to_markdown(resp.content)
        if not markdown.strip():
            return None
        return FullTextResult(
            markdown=markdown,
            source=self.name,
            source_format='elsevier-xml',
            source_url=link,
            pmc_id=None,
        )


def default_sources() -> tuple[FullTextSource, ...]:
    """Return the production source list, in priority order.

    Kept as a function so callers can append sources without import-time side
    effects.  The Elsevier source sits last (only reached when PMC + Europe PMC
    both miss) and reads its key from ``credentials``; a caller with no
    Elsevier key makes it a no-op.
    """
    return (PmcOaSource(), EuropePmcSource(), ElsevierOaSource())


async def fetch_full_text(
    ids: ArticleIds,
    *,
    sources: Sequence[FullTextSource] | None = None,
    credentials: Mapping[str, object] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FullTextResult | None:
    """Walk the source ladder with the identifiers already in hand.

    No resolution is performed: a source whose ``requires`` are not satisfied by
    ``ids`` is skipped.  Use :func:`get_full_text` to enrich ``ids`` on demand.
    Returns the first non-``None`` result, or ``None`` when nothing serves it.
    """
    chosen = tuple(sources) if sources is not None else default_sources()
    async with client_ctx(http_client) as c:
        for source in chosen:
            if not ids.has(source.requires):
                continue
            result = await source.try_fetch(ids, credentials=credentials, http_client=c)
            if result is not None:
                return result
    return None


async def get_full_text(
    ids: ArticleIds,
    *,
    resolver: Resolver | None = None,
    sources: Sequence[FullTextSource] | None = None,
    credentials: Mapping[str, object] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FullTextResult | None:
    """End-to-end retrieval with demand-driven identifier resolution.

    Walks the ladder in priority order; when the next source needs an
    identifier ``ids`` lacks, invokes ``resolver`` once (memoised) to enrich the
    bundle, then continues.  The first source to return a result wins, so a
    cheap early source that is already satisfied skips both the resolver and the
    more expensive sources behind it.  Returns ``None`` when no source serves it.
    """
    chosen = tuple(sources) if sources is not None else default_sources()
    resolved = False
    async with client_ctx(http_client) as c:
        for source in chosen:
            if not ids.has(source.requires):
                if resolver is not None and not resolved:
                    ids = ids.merge(await resolver(ids))
                    resolved = True
                if not ids.has(source.requires):
                    continue
            result = await source.try_fetch(ids, credentials=credentials, http_client=c)
            if result is not None:
                return result
    return None
