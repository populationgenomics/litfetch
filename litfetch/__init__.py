"""litfetch: identifier -> full-text markdown via a pluggable source ladder.

Hand :func:`get_full_text` an :class:`ArticleIds` bundle (any of pmid / pmcid /
doi) and, optionally, a :data:`~litfetch.resolvers.Resolver` to fill in missing
identifiers on demand.  The fetch core lives here; the bundled identifier
resolvers (Europe PMC, NCBI ID Converter, Semantic Scholar) live in
:mod:`litfetch.resolvers`.
"""

from __future__ import annotations

from litfetch.ids import ArticleIds
from litfetch.sources import (
    ElsevierOaSource,
    EuropePmcSource,
    FullTextResult,
    FullTextSource,
    PmcOaSource,
    default_sources,
    fetch_full_text,
    get_full_text,
    jats_to_markdown,
)

__version__ = '0.1.0'

__all__ = [
    'ArticleIds',
    'ElsevierOaSource',
    'EuropePmcSource',
    'FullTextResult',
    'FullTextSource',
    'PmcOaSource',
    '__version__',
    'default_sources',
    'fetch_full_text',
    'get_full_text',
    'jats_to_markdown',
]
