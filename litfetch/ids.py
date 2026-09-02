"""The identifier bundle shared across resolvers and full-text sources."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

# The identifiers a resolver can supply; resolution is only ever asked for these.
RESOLVABLE = frozenset({'pmid', 'pmcid', 'doi'})


@dataclasses.dataclass(frozen=True)
class ArticleIds:
    """An immutable bundle of the identifiers litfetch can act on.

    Every field is optional: a caller may enter with only a PMID, only a DOI (a
    non-PubMed article), only a Bookshelf accession (a GeneReviews chapter), or
    a fully-populated bundle.  Resolvers enrich a bundle; sources consume
    whichever identifier they declare in ``requires``.

    ``pmid``, ``pmcid`` and ``doi`` are the resolvable identifiers
    (:data:`RESOLVABLE`): a resolver can supply any of them from another, and
    demand-driven resolution, :func:`~litfetch.resolvers.chain`'s stop
    condition and :func:`~litfetch.resolvers.chain_batch`'s ``required`` all
    key on this set.  ``bookid`` is an NCBI Bookshelf accession (``NBK`` +
    digits) naming a book part -- a GeneReviews chapter, a section of any other
    NCBI book.  A book part has no PMCID, and no resolver is asked for its
    accession; PubMed's record carries it, so the caller supplies it.
    """

    # Deliberately a thin record.  The priority orders callers apply over these
    # fields (canonical_key prefers doi; NCBI idconv prefers pmid; S2 prefers
    # doi) are independent caller policy, not a domain ordering -- there is no
    # single intrinsic specificity ranking -- so they stay at the call sites
    # rather than being centralised here behind a generic picker.
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    bookid: str | None = None

    def merge(self, other: ArticleIds) -> ArticleIds:
        """Return a bundle that fills this one's gaps from ``other``.

        Known identifiers are never overwritten: a resolver can add a DOI but
        cannot correct a PMCID the caller supplied.
        """
        return ArticleIds(
            pmid=self.pmid or other.pmid,
            pmcid=self.pmcid or other.pmcid,
            doi=self.doi or other.doi,
            bookid=self.bookid or other.bookid,
        )

    def has(self, fields: Iterable[str]) -> bool:
        """Return whether every identifier named in ``fields`` is present."""
        return all(getattr(self, field) for field in fields)
