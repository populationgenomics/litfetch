# litfetch

Resolve a scholarly article identifier to **full-text markdown**.

litfetch is two cooperating layers:

- a **fetch ladder** — pluggable `FullTextSource` backends (PMC Open Access S3,
  Europe PMC, Elsevier OA) tried in priority order; the first to return a
  result wins;
- an optional **resolver layer** — pluggable `Resolver`s that enrich what you
  know about a paper (`pmid` → `pmcid`/`doi`, etc.) so the ladder can act.

You hand it an `ArticleIds` bundle (any of `pmid` / `pmcid` / `doi`). Resolution
is **demand-driven**: a resolver only runs when the next source needs an
identifier you don't yet have, and runs at most once.

Conversion of JATS / Elsevier XML to markdown is delegated to
[litdown](https://github.com/populationgenomics/litdown).

## Install

litfetch is distributed as a wheel through the consuming repo's wheelhouse
(it is not on PyPI). Add it as a dependency:

```toml
dependencies = ["litfetch>=0.1"]
```

litfetch depends on `litdown>=0.3`, which you supply the same way.

## Usage

### Inject your own resolver

A resolver is just an async `ArticleIds -> ArticleIds`. Enrich from whatever you
have — a corpus client, a local cache, an API — and `merge` it in:

```python
from litfetch import ArticleIds, get_full_text

async def my_resolver(ids: ArticleIds) -> ArticleIds:
    if not ids.pmid:
        return ids
    pmcid, doi = await my_corpus.lookup(ids.pmid)
    return ids.merge(ArticleIds(pmcid=pmcid, doi=doi))

result = await get_full_text(ArticleIds(pmid='29622564'), resolver=my_resolver)
if result:
    print(result.source, result.markdown)
```

### Use a bundled resolver

Bundled resolvers are constructed with their config, then passed in the same
slot. `chain(...)` composes several (yours first, fallbacks after); it stops
once every identifier is known:

```python
from litfetch import ArticleIds, get_full_text
from litfetch.resolvers import SemanticScholarResolver, NcbiIdConverterResolver, chain

resolver = chain(
    my_resolver,                                          # your own
    SemanticScholarResolver(api_key=S2_KEY),              # bundled
    NcbiIdConverterResolver(tool='myapp', email='me@x'),  # bundled
)
result = await get_full_text(ArticleIds(pmid='29622564'), resolver=resolver)
```

`default_resolver()` is a batteries-included, keyless chain
(Europe PMC search + NCBI ID Converter).

### No resolver — you already hold the IDs

A non-PubMed paper you only have a DOI for, plus your own Elsevier key:

```python
result = await get_full_text(
    ArticleIds(doi='10.1016/j.cell.2020.01.001'),
    credentials={'elsevier_api_key': key},
)
```

### Resolvers stand alone

Each resolver is usable on its own as a cross-reference tool, independent of
fetching:

```python
from litfetch import ArticleIds
from litfetch.resolvers import SemanticScholarResolver

ids = await SemanticScholarResolver()(ArticleIds(doi='10.1016/j.cell.2020.01.001'))
print(ids.pmid, ids.pmcid)
```

## Extending

- **A new source:** implement the `FullTextSource` protocol — a `name`, a
  `requires: frozenset[str]` of the `ArticleIds` fields it needs, and an async
  `try_fetch(ids, *, credentials, http_client)` returning a `FullTextResult` or
  `None`. Add it to a `sources=` list (or your own `default_sources`).
- **A new resolver:** write an async `ArticleIds -> ArticleIds` that fills gaps
  via `ArticleIds.merge` and never overwrites a known id.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
```
