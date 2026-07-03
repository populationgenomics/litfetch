"""Cross-version relations between identifiers: preprint <-> published.

A work can exist as a preprint (bioRxiv / medRxiv, ...) and later as a published
version of record -- two distinct DOIs for one paper.  :func:`related_ids` takes
whatever :class:`~litfetch.ids.ArticleIds` you hold and returns the related works
it can find, each as its own :class:`~litfetch.ids.ArticleIds` tagged with how it
relates -- so the caller need not know whether what it holds is a preprint or a
published DOI.  The equivalence decision ("same paper") is the consumer's;
litfetch only surfaces the links.  The returned bundles are single-DOI and can be
fed straight back through a :data:`~litfetch.resolvers.Resolver` to fill the rest.

Sources: the bioRxiv / medRxiv details API (reliable preprint -> published) and
Crossref relations (``has-preprint`` / ``is-preprint-of``, both directions,
best-effort on publisher metadata).
"""

from __future__ import annotations

import enum
import logging
from typing import NamedTuple

import httpx

from litfetch import _doi, _http, crossref, ids

logger = logging.getLogger(__name__)

_BIORXIV_DETAILS_BASE = 'https://api.biorxiv.org/details'
_BIORXIV_SERVERS = ('biorxiv', 'medrxiv')
# Cold Spring Harbor preprint DOI prefixes (bioRxiv/medRxiv): the older 10.1101
# and the newer 10.64898.
_PREPRINT_DOI_PREFIXES = ('10.1101/', '10.64898/')


class RelationType(enum.Enum):
    """How a related work relates to the one you asked about.

    ``PREPRINT`` -- the related bundle is a preprint of the input; ``PUBLISHED``
    -- the related bundle is the published version of record of the input.
    """

    PREPRINT = 'preprint'
    PUBLISHED = 'published'


class Related(NamedTuple):
    """A related work: its relationship to the input, and its identifiers."""

    relation: RelationType
    ids: ids.ArticleIds


async def related_ids(article_ids: ids.ArticleIds, *, http: _http.Http) -> tuple[Related, ...]:
    """Find the preprint / published counterparts of ``article_ids`` by DOI.

    Returns each related work as a single-DOI :class:`~litfetch.ids.ArticleIds`
    tagged with its :class:`RelationType`; empty when there is no DOI or nothing
    links.  A preprint DOI is followed to its published version via the bioRxiv
    details API; Crossref relations are consulted in either direction.
    """
    doi = article_ids.doi
    if not doi:
        return ()
    found: dict[tuple[RelationType, str], Related] = {}
    if doi.startswith(_PREPRINT_DOI_PREFIXES):
        published = await _biorxiv_published(http, doi)
        if published:
            found[(RelationType.PUBLISHED, published)] = Related(RelationType.PUBLISHED, ids.ArticleIds(doi=published))
    for relation, linked in await _crossref_relations(http, doi):
        found.setdefault((relation, linked), Related(relation, ids.ArticleIds(doi=linked)))
    return tuple(found.values())


async def _biorxiv_published(http: _http.Http, doi: str) -> str | None:
    """Return the published DOI bioRxiv/medRxiv records for preprint ``doi``."""
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
            continue
        if collection:
            published = collection[-1].get('published')
            if published and published != 'NA':
                return published
    return None


async def _crossref_relations(http: _http.Http, doi: str) -> list[tuple[RelationType, str]]:
    """Return ``(RelationType, doi)`` for Crossref ``has-preprint`` / ``is-preprint-of``."""
    message = await crossref.fetch_work(doi, http=http)
    if message is None:
        return []
    relation = message.get('relation', {})
    out: list[tuple[RelationType, str]] = []
    for key, kind in (('has-preprint', RelationType.PREPRINT), ('is-preprint-of', RelationType.PUBLISHED)):
        for entry in relation.get(key, []) or []:
            if entry.get('id') and entry.get('id-type') == 'doi':
                out.append((kind, entry['id']))
    return out
