"""Tests for the ArticleIds bundle."""

from __future__ import annotations

import dataclasses

from litfetch import ids


def test_merge_fills_gaps_without_overwriting() -> None:
    base = ids.ArticleIds(pmid='1', bookid='NBK1')
    other = ids.ArticleIds(pmid='2', pmcid='PMC2', doi='10.1/x', bookid='NBK2')
    assert base.merge(other) == ids.ArticleIds(pmid='1', pmcid='PMC2', doi='10.1/x', bookid='NBK1')


def test_merge_carries_every_field() -> None:
    # Every field of the bundle takes part in merge -- an added field cannot be silently dropped.
    full = ids.ArticleIds(**{field.name: f'v-{field.name}' for field in dataclasses.fields(ids.ArticleIds)})
    assert ids.ArticleIds().merge(full) == full
    assert full.merge(ids.ArticleIds()) == full


def test_resolvable_names_bundle_fields_only() -> None:
    assert ids.RESOLVABLE.issubset(field.name for field in dataclasses.fields(ids.ArticleIds))


def test_has_checks_each_named_field() -> None:
    assert ids.ArticleIds(bookid='NBK1').has({'bookid'})
    assert not ids.ArticleIds(pmid='1').has({'bookid'})
    assert ids.ArticleIds(pmid='1', bookid='NBK1').has({'pmid', 'bookid'})
