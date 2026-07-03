"""Canonical, backend-agnostic (de)serialisation of the file-set model.

litfetch owns the *structure* of an article's identity and files -- their fields
and how each round-trips through a plain JSON-able ``dict`` -- but not the wire
format nor where they are stored.  litfetch ships no cache backend; a consumer
composes these mappings into its own record envelope (status, leases, placement)
and never re-lists a dataclass's fields itself.

Every ``*_to_dict`` returns a dict of JSON primitives; every ``*_from_dict``
reconstructs the dataclass from one.  ``from_dict`` inputs are typed ``Any`` --
they sit at the untyped parse boundary (``json.loads`` and friends).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from litfetch import artifacts, ids


def article_ids_to_dict(value: ids.ArticleIds) -> dict[str, Any]:
    """Map an :class:`~litfetch.ids.ArticleIds` to a dict."""
    return dataclasses.asdict(value)


def article_ids_from_dict(data: dict[str, Any]) -> ids.ArticleIds:
    """Reconstruct an :class:`~litfetch.ids.ArticleIds` from a dict."""
    return ids.ArticleIds(**data)


def file_to_dict(file: artifacts.File) -> dict[str, Any]:
    """Map a :class:`~litfetch.artifacts.File` to a dict (``kind`` as its value)."""
    return {
        'kind': file.kind.value,
        'source': file.source,
        'media_type': file.media_type,
        'uri': file.uri,
        'filename': file.filename,
        'credential_key': file.credential_key,
        'size_bytes': file.size_bytes,
        'description': file.description,
    }


def file_from_dict(data: dict[str, Any]) -> artifacts.File:
    """Reconstruct a :class:`~litfetch.artifacts.File` from a dict."""
    return artifacts.File(
        kind=artifacts.FileKind(data['kind']),
        source=data['source'],
        media_type=data['media_type'],
        uri=data['uri'],
        filename=data['filename'],
        credential_key=data['credential_key'],
        size_bytes=data['size_bytes'],
        description=data['description'],
    )


def source_metadata_to_dict(meta: artifacts.SourceMetadata) -> dict[str, Any]:
    """Map a :class:`~litfetch.artifacts.SourceMetadata` to a dict."""
    return {'licence': meta.licence, 'access': meta.access, 'basis': meta.basis}


def source_metadata_from_dict(data: dict[str, Any]) -> artifacts.SourceMetadata:
    """Reconstruct a :class:`~litfetch.artifacts.SourceMetadata` from a dict."""
    return artifacts.SourceMetadata(licence=data['licence'], access=data['access'], basis=data['basis'])
