"""The identifier bundle shared across resolvers and full-text sources."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ArticleIds:
    """An immutable bundle of the identifiers litfetch can act on.

    Every field is optional: a caller may enter with only a PMID, only a DOI (a
    non-PubMed paper), or a fully-populated bundle.  Resolvers enrich a bundle;
    sources consume whichever identifier they declare in ``requires``.
    """

    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None

    def merge(self, other: ArticleIds) -> ArticleIds:
        """Return a bundle that fills this one's gaps from ``other``.

        Known identifiers are never overwritten: a resolver can add a DOI but
        cannot correct a PMCID the caller supplied.
        """
        return ArticleIds(
            pmid=self.pmid or other.pmid,
            pmcid=self.pmcid or other.pmcid,
            doi=self.doi or other.doi,
        )

    def has(self, fields: Iterable[str]) -> bool:
        """Return whether every identifier named in ``fields`` is present."""
        return all(getattr(self, field) for field in fields)
