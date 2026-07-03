"""The data types that flow through the fetch seam.

An article is modelled as a *file-set*: a collection of :class:`File` references
sharing one identity.  A :class:`File` is either the article **body** (in one of
its media types) or a piece of **supplementary** material, hosted upstream with a
``uri`` and the ``credential_key`` a fetch needs.  A :class:`Blob` is a File once
its bytes are in hand.
"""

from __future__ import annotations

import dataclasses
import enum

# Well-known media types an artifact can carry: the JATS or Elsevier body
# dialects, or a PDF rendition.
JATS_XML = 'application/jats+xml'
ELSEVIER_XML = 'application/vnd.elsevier+xml'
PDF = 'application/pdf'

# A ``File.credential_key`` value meaning the fetch needs *institutional
# entitlement* (a subscription reached via an EZproxy-style client), rather than
# a key in the ``credentials`` map.  The consumer routes such a file through its
# entitled client; an openly-fetchable file leaves ``credential_key`` ``None``.
INSTITUTIONAL = 'institutional'


class FileKind(enum.Enum):
    """What a :class:`File` is within the article's file-set.

    ``BODY`` -- the article full text itself, in one of its media types.
    ``SUPPLEMENTARY`` -- additional material (figures, datasets, tables), not
    the body.
    """

    BODY = 'body'
    SUPPLEMENTARY = 'supplementary'


@dataclasses.dataclass(frozen=True)
class File:
    """A reference to one file in an article's file-set -- not its bytes.

    ``source`` names the source that can retrieve it (routes
    :func:`~litfetch.fetchers.fetch_file`).  A File is hosted upstream: it carries
    a ``uri`` and the ``credential_key`` a fetch needs (``None`` when openly
    accessible).  ``credential_key`` is either a key in the caller's
    ``credentials`` map (e.g. a publisher API key) or :data:`INSTITUTIONAL`, which
    marks a fetch that needs institutional entitlement (an EZproxy-style client)
    rather than a map key.  ``uri`` is fetched on demand, never eagerly.
    """

    kind: FileKind
    source: str
    media_type: str | None = None
    uri: str | None = None
    filename: str | None = None
    credential_key: str | None = None
    size_bytes: int | None = None
    description: str | None = None


@dataclasses.dataclass(frozen=True)
class SourceMetadata:
    """Access terms for a fetched artifact: its licence and how that was known.

    litfetch returns the licence *raw* (the CC URL, JATS ``license-type``, or
    licence text as found upstream); mapping to an SPDX id is the consumer's --
    describe, don't own.  ``basis`` records provenance: ``'artifact'`` when read
    from the fetched bytes (authoritative for exactly those bytes), or an
    authority name (e.g. ``'unpaywall'``) when asserted for a paper whose bytes
    ship no licence (a PDF).  A ``None`` field means unknown.
    """

    licence: str | None = None
    access: str | None = None
    basis: str | None = None


@dataclasses.dataclass(frozen=True)
class Blob:
    """A materialised :class:`File`: its reference plus the fetched bytes."""

    file: File
    content: bytes
