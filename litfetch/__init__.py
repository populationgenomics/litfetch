"""litfetch: identifier -> the retrievable artifacts of a scholarly article.

Hand :func:`fetch_body` an :class:`ArticleIds` bundle (any of pmid / pmcid / doi)
and, optionally, a :data:`~litfetch.resolvers.Resolver` to fill in missing
identifiers on demand.  A :class:`~litfetch.fetchers.Fetcher` ladder is tried in
priority order; the first to serve the body yields a :class:`Blob` (a
:class:`File` plus its bytes).  Supplementary material is discovered with
:func:`list_files` and fetched with :func:`fetch_file`.

An article is modelled as a *file-set*: a collection of :class:`File` references
(body renditions and supplementary material, by :class:`FileKind`) sharing one
identity, each hosted upstream.  litfetch fetches the raw artifacts and reports
their access terms (:class:`SourceMetadata`); rendering them (e.g. XML ->
markdown via litdown) and storing them are the consumer's concern.  The bundled
identifier resolvers (Europe PMC, NCBI ID Converter, Semantic Scholar) live in
:mod:`litfetch.resolvers`; file-set listing and fetching live in
:mod:`litfetch.fetchers`.
"""

from __future__ import annotations

from litfetch._http import Http, Rate, RetryPolicy
from litfetch.artifacts import (
    INSTITUTIONAL,
    Blob,
    File,
    FileKind,
    SourceMetadata,
)
from litfetch.fetchers import (
    BiorxivFetcher,
    CrossrefFileSource,
    ElsevierFetcher,
    EuropePmcFetcher,
    Fetcher,
    FileSource,
    PmcOaFetcher,
    SemanticScholarFileSource,
    SpringerFetcher,
    SpringerFileSource,
    UnpaywallFileSource,
    default_fetchers,
    default_file_sources,
)
from litfetch.ids import ArticleIds
from litfetch.relations import Related, RelationType
from litfetch.sessions import (
    Session,
    fetch_body,
    fetch_file,
    list_files,
    related_ids,
    resolve_access,
)
from litfetch.source_metadata import extract_source_metadata

__version__ = '0.2.0'

__all__ = [
    'INSTITUTIONAL',
    'ArticleIds',
    'BiorxivFetcher',
    'Blob',
    'CrossrefFileSource',
    'ElsevierFetcher',
    'EuropePmcFetcher',
    'Fetcher',
    'File',
    'FileKind',
    'FileSource',
    'Http',
    'PmcOaFetcher',
    'Rate',
    'Related',
    'RelationType',
    'RetryPolicy',
    'SemanticScholarFileSource',
    'Session',
    'SourceMetadata',
    'SpringerFetcher',
    'SpringerFileSource',
    'UnpaywallFileSource',
    '__version__',
    'default_fetchers',
    'default_file_sources',
    'extract_source_metadata',
    'fetch_body',
    'fetch_file',
    'list_files',
    'related_ids',
    'resolve_access',
]
