"""Round-trip tests for the canonical file-set serialisation."""

from __future__ import annotations

from litfetch import artifacts, ids, serde


def test_article_ids_round_trip() -> None:
    value = ids.ArticleIds(pmid='1', pmcid='PMC1', doi='10.1/x')
    assert serde.article_ids_from_dict(serde.article_ids_to_dict(value)) == value


def test_file_round_trip() -> None:
    file = artifacts.File(
        kind=artifacts.FileKind.SUPPLEMENTARY,
        source='pmc_oa_s3',
        media_type='text/csv',
        uri='https://e/data.csv',
        filename='data.csv',
        size_bytes=42,
        description='a table',
    )
    assert serde.file_from_dict(serde.file_to_dict(file)) == file


def test_source_metadata_round_trip() -> None:
    meta = artifacts.SourceMetadata(licence='CC-BY-4.0', access='open', basis='artifact')
    assert serde.source_metadata_from_dict(serde.source_metadata_to_dict(meta)) == meta
