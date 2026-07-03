"""Tests for DOI validation and URL-safe path encoding."""

from __future__ import annotations

import pytest

from litfetch import _doi


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('10.1016/j.cell.2020.01.001', '10.1016/j.cell.2020.01.001'),
        ('  10.1/x  ', '10.1/x'),
        ('doi:10.1016/j.cell.2020.01.001', '10.1016/j.cell.2020.01.001'),
        ('DOI:10.1/x', '10.1/x'),
        ('https://doi.org/10.1/x', '10.1/x'),
        ('http://dx.doi.org/10.1/x', '10.1/x'),
        ('10.1000.10/123', '10.1000.10/123'),  # dot-subdivided registrant
    ],
)
def test_normalize_strips_decorations(raw: str, expected: str) -> None:
    assert _doi.normalize_and_validate_doi(raw) == expected


@pytest.mark.parametrize(
    'raw',
    [
        '',
        'not-a-doi',
        '10.1',  # registrant but no suffix
        '10.1/',  # empty suffix
        '11.1234/x',  # wrong directory-indicator
        'https://example.com/paper',
    ],
)
def test_normalize_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match='not a valid DOI'):
        _doi.normalize_and_validate_doi(raw)


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('10.1/x', '10.1/x'),
        # A suffix that would reshape the URL if interpolated raw.
        ('10.1/a?b#c d', '10.1/a%3Fb%23c%20d'),
        # A slash in the suffix stays a path separator; each side is encoded.
        ('10.1/a/b c', '10.1/a/b%20c'),
        # A CC URL as a suffix (real Crossref DOIs do this) is fully encoded.
        ('10.1/S0140-6736(20)30183-5', '10.1/S0140-6736%2820%2930183-5'),
        # A classic Wiley SICI-style DOI: angle brackets, colons, semicolons all
        # reserved characters that must not reach the URL raw.
        (
            '10.1002/1521-3773(20010316)40:6<9999::AID-ANIE9999>3.0.CO;2-A',
            '10.1002/1521-3773%2820010316%2940%3A6%3C9999%3A%3AAID-ANIE9999%3E3.0.CO%3B2-A',
        ),
    ],
)
def test_encode_percent_encodes_segments(raw: str, expected: str) -> None:
    assert _doi.encode_doi_path(raw) == expected


def test_encode_leaves_only_the_registrant_suffix_separator() -> None:
    """No reserved character survives except the single structural ``/``."""
    encoded = _doi.encode_doi_path('10.1002/1521-3773(20010316)40:6<9999>3.0.CO;2-A')
    assert encoded.count('/') == 1
    for reserved in ('(', ')', '<', '>', ':', ';'):
        assert reserved not in encoded


@pytest.mark.parametrize('raw', ['10.1/../secret', '10.1/./x', '10.1/a/../b'])
def test_encode_rejects_dot_segments(raw: str) -> None:
    with pytest.raises(ValueError, match='path-traversal'):
        _doi.encode_doi_path(raw)


def test_encode_rejects_invalid_doi() -> None:
    with pytest.raises(ValueError, match='not a valid DOI'):
        _doi.encode_doi_path('not-a-doi')
