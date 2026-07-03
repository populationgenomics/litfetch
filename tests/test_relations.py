"""Tests for preprint <-> published relation lookups."""

from __future__ import annotations

import httpx

from litfetch import ids, relations, sessions
from tests import conftest

_PRE = '10.1101/2020.11.30.403378'
_PUB = '10.1002/adhm.202100934'


async def test_related_ids_follows_preprint_to_published(patch_transport: conftest.InstallTransport) -> None:
    patch_transport(
        {
            f'GET /details/biorxiv/{_PRE}': [httpx.Response(200, json={'collection': [{'published': _PUB}]})],
            f'GET /works/{_PRE}': [httpx.Response(200, json={'message': {'relation': {}}})],
        }
    )
    related = await sessions.related_ids(ids.ArticleIds(doi=_PRE))
    assert len(related) == 1
    assert related[0].relation is relations.RelationType.PUBLISHED
    assert related[0].ids == ids.ArticleIds(doi=_PUB)


async def test_related_ids_finds_preprint_from_published(patch_transport: conftest.InstallTransport) -> None:
    # A published (non-preprint-prefix) DOI: no bioRxiv call, Crossref has-preprint.
    patch_transport(
        {
            f'GET /works/{_PUB}': [
                httpx.Response(200, json={'message': {'relation': {'has-preprint': [{'id': _PRE, 'id-type': 'doi'}]}}})
            ],
        }
    )
    related = await sessions.related_ids(ids.ArticleIds(doi=_PUB))
    assert len(related) == 1
    assert related[0].relation is relations.RelationType.PREPRINT
    assert related[0].ids == ids.ArticleIds(doi=_PRE)


async def test_related_ids_dedupes_biorxiv_and_crossref(patch_transport: conftest.InstallTransport) -> None:
    # bioRxiv and Crossref both name the same published DOI -> one entry.
    patch_transport(
        {
            f'GET /details/biorxiv/{_PRE}': [httpx.Response(200, json={'collection': [{'published': _PUB}]})],
            f'GET /works/{_PRE}': [
                httpx.Response(
                    200, json={'message': {'relation': {'is-preprint-of': [{'id': _PUB, 'id-type': 'doi'}]}}}
                )
            ],
        }
    )
    related = await sessions.related_ids(ids.ArticleIds(doi=_PRE))
    assert len(related) == 1
    assert related[0].ids.doi == _PUB


async def test_related_ids_dedupes_across_case(patch_transport: conftest.InstallTransport) -> None:
    # bioRxiv and Crossref name the same published DOI in different case; DOIs are
    # case-insensitive, so this is one entry, keeping bioRxiv's first-seen casing.
    patch_transport(
        {
            f'GET /details/biorxiv/{_PRE}': [httpx.Response(200, json={'collection': [{'published': _PUB}]})],
            f'GET /works/{_PRE}': [
                httpx.Response(
                    200,
                    json={'message': {'relation': {'is-preprint-of': [{'id': _PUB.upper(), 'id-type': 'doi'}]}}},
                )
            ],
        }
    )
    related = await sessions.related_ids(ids.ArticleIds(doi=_PRE))
    assert len(related) == 1
    assert related[0].ids.doi == _PUB


async def test_related_ids_noop_without_doi() -> None:
    assert await sessions.related_ids(ids.ArticleIds(pmid='1')) == ()
