"""DOI validation and URL-safe path encoding.

Several sources interpolate a DOI into an upstream URL path (Unpaywall,
Crossref, Semantic Scholar, bioRxiv, the future doi.org resolve).  Doing so
raw is wrong twice over: a DOI suffix may contain ``?``, ``#``, spaces, or
``/`` -- which truncate or reshape the URL -- and a crafted ``.``/``..``
segment is a path-traversal vector.  :func:`encode_doi_path` is the one safe
way to place a DOI in a URL path; :func:`normalize_and_validate_doi` is the
shape gate it builds on.
"""

from __future__ import annotations

import re
import urllib.parse

# A DOI is ``10.<registrant>/<suffix>``: the registrant is one or more digits
# with optional dot-separated sub-elements (e.g. ``10.1000.10``); the suffix is
# any non-empty string.  The digit count is left open -- the common 4-9 range is
# Crossref's observed corpus, not a spec rule -- so an unusual registrant is not
# rejected.  DOIs are case-insensitive, so the prefix match is too; the value is
# returned unchanged (suffixes are case-sensitive for many registrants).
_DOI_RE = re.compile(r'^10\.\d+(?:\.\d+)*/.+$', re.IGNORECASE)

# Decorations a caller-supplied DOI may arrive with; stripped before validation.
_RESOLVER_PREFIXES = ('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'http://dx.doi.org/')


def normalize_and_validate_doi(doi: str) -> str:
    """Return the bare, validated DOI, stripping common decorations.

    Accepts a DOI carrying surrounding whitespace, a ``doi:`` scheme, or a
    resolver URL prefix (``https://doi.org/``, ``http://dx.doi.org/``) and
    returns the bare ``10.xxxx/suffix`` form.

    Args:
        doi: The DOI to normalise, possibly decorated.

    Returns:
        The bare DOI.

    Raises:
        ValueError: If the result is not a syntactically valid DOI.
    """
    candidate = doi.strip()
    lowered = candidate.lower()
    for prefix in _RESOLVER_PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    if candidate.lower().startswith('doi:'):
        candidate = candidate[len('doi:') :].strip()
    if not _DOI_RE.match(candidate):
        raise ValueError(f'not a valid DOI: {doi!r}')
    return candidate


def encode_doi_path(doi: str) -> str:
    """Percent-encode a validated DOI for safe interpolation into a URL path.

    Validates via :func:`normalize_and_validate_doi`, then percent-encodes each
    ``/``-separated segment -- so a suffix ``/``, ``?``, ``#``, or space cannot
    reshape the URL -- and rejects a ``.`` or ``..`` segment (path traversal).

    Args:
        doi: The DOI to encode, possibly decorated.

    Returns:
        The encoded DOI, ready to interpolate after a URL's path separator.

    Raises:
        ValueError: If the DOI is invalid or contains a dot-segment.
    """
    normalized = normalize_and_validate_doi(doi)
    segments = normalized.split('/')
    if any(segment in ('.', '..') for segment in segments):
        raise ValueError(f'DOI contains a path-traversal segment: {doi!r}')
    return '/'.join(urllib.parse.quote(segment, safe='') for segment in segments)
