# litfetch

Resolve a scholarly article identifier to its **retrievable artifacts** — the
full-text body and any supplementary material — and fetch their bytes.

litfetch is two cooperating seams:

- a **fetch ladder** — pluggable `Fetcher` backends (PMC Open Access S3, Europe
  PMC, Elsevier OA) tried in priority order; the first to serve the body wins,
  returning a `Blob` (a `File` plus its bytes);
- an optional **resolver layer** — pluggable `Resolver`s that enrich what you
  know about a paper (`pmid` → `pmcid`/`doi`, etc.) so the ladder can act.

You hand it an `ArticleIds` bundle (any of `pmid` / `pmcid` / `doi`). Resolution
is **demand-driven**: a resolver only runs when the next fetcher needs an
identifier you don't yet have, and runs at most once.

An article is modelled as a **file-set**: a collection of `File` references (the
body in its various media types, plus supplementary material, distinguished by
`FileKind`), each hosted upstream. litfetch fetches the raw artifacts and reports
their access terms; it does **not** render them. To turn a fetched JATS/Elsevier
body into markdown, run [litdown](https://github.com/populationgenomics/litdown)
on the bytes yourself (see [Render to markdown](#render-to-markdown)).

The examples below are a tour; [`docs/api.md`](docs/api.md) is the full
reference for the public surface.

## Install

```sh
pip install litfetch
```

bioRxiv / medRxiv preprint full text needs a browser-fingerprint HTTP client,
enabled by the `biorxiv` extra:

```sh
pip install 'litfetch[biorxiv]'
```

## Usage

### Fetch the body

Hand `fetch_body` an `ArticleIds`; the default ladder serves the first available
body as a `Blob`:

```python
from litfetch import ArticleIds, fetch_body

blob = await fetch_body(ArticleIds(pmcid='PMC5334499'))
if blob:
    print(blob.file.source, blob.file.media_type, len(blob.content))
```

### Render to markdown

litfetch returns raw bytes, not markdown. Convert a JATS/Elsevier body with
[litdown](https://github.com/populationgenomics/litdown) — you pick and pin the
converter:

```python
import io
import litdown
from litfetch import ArticleIds, fetch_body

blob = await fetch_body(ArticleIds(pmcid='PMC5334499'))
if blob:
    markdown = litdown.convert(io.BytesIO(blob.content))
```

### Inject your own resolver

A resolver is an async `(ArticleIds, Http) -> ArticleIds` — the session running
it supplies the `Http`. Enrich from whatever you have — a corpus client, a local
cache, an API — and `merge` it in (this one ignores `Http`, hence `_http`):

```python
from litfetch import ArticleIds, Http, fetch_body

async def my_resolver(ids: ArticleIds, _http: Http) -> ArticleIds:
    if not ids.pmid:
        return ids
    pmcid, doi = await my_corpus.lookup(ids.pmid)
    return ids.merge(ArticleIds(pmcid=pmcid, doi=doi))

blob = await fetch_body(ArticleIds(pmid='29622564'), resolver=my_resolver)
```

### Use a bundled resolver

Bundled resolvers are constructed with their config, then passed in the same
slot. `chain(...)` composes several (yours first, fallbacks after); it stops
once every identifier is known:

```python
from litfetch import ArticleIds, fetch_body
from litfetch.resolvers import SemanticScholarResolver, NcbiIdConverterResolver, chain

resolver = chain(
    my_resolver,                                          # your own
    SemanticScholarResolver(api_key=S2_KEY),              # bundled
    NcbiIdConverterResolver(tool='myapp', email='me@x'),  # bundled
)
blob = await fetch_body(ArticleIds(pmid='29622564'), resolver=resolver)
```

`default_resolver()` is a batteries-included, keyless chain
(Europe PMC search + NCBI ID Converter).

### No resolver — you already hold the IDs

A non-PubMed paper you only have a DOI for, plus your own Elsevier key:

```python
blob = await fetch_body(
    ArticleIds(doi='10.1016/j.cell.2020.01.001'),
    credentials={'elsevier_api_key': key},
)
```

### Supplementary material

`list_files` enumerates the file-set (references, no bytes); `fetch_file`
materialises one:

```python
from litfetch import ArticleIds, FileKind, list_files, fetch_file

files = await list_files(ArticleIds(pmcid='PMC5334499'), kind=FileKind.SUPPLEMENTARY)
for file in files:
    blob = await fetch_file(file)
```

### Access terms

Read the licence from the fetched bytes, falling back to an access authority
(Unpaywall) when the bytes carry none:

```python
from litfetch import extract_source_metadata, resolve_access

meta = extract_source_metadata(blob)          # from the JATS/Elsevier bytes
if meta.licence is None:
    meta = await resolve_access(ArticleIds(doi='10.1016/j.cell.2020.01.001'))
```

### Resolvers stand alone

Each resolver is usable on its own as a cross-reference tool, independent of
fetching. A resolver is given the `Http` to use, so run it inside a session:

```python
from litfetch import ArticleIds, Session
from litfetch.resolvers import SemanticScholarResolver

async with Session() as s:
    ids = await SemanticScholarResolver()(ArticleIds(doi='10.1016/j.cell.2020.01.001'), s)
print(ids.pmid, ids.pmcid)
```

### Batch: one session, a scope per paper

The one-shot functions above each open a throwaway session. For many papers,
hold one `Session` (pooled connection, shared pacing) and open a `scope` per
paper — the scope caches within itself, so a duplicate upstream call (e.g.
Unpaywall for both licence and PDF) is fetched once:

```python
from litfetch import ArticleIds, Session

async with Session() as session:
    for pmid in pmids:
        async with session.scope() as s:
            blob = await s.fetch_body(ArticleIds(pmid=pmid))
            access = await s.resolve_access(ArticleIds(pmid=pmid))
```

## Extending

- **A new body fetcher:** implement the `Fetcher` protocol — a `name`, a
  `requires: frozenset[str]` of the `ArticleIds` fields it needs, and an async
  `fetch(ids, *, credentials, http)` returning a body `Blob` or `None`.
  Add it to a `fetchers=` list (or your own `default_fetchers`).
- **A new file source:** implement the `FileSource` protocol — a `name`, and
  async `list_files(ids, ...)` / `fetch_file(file, ...)` — to enumerate and
  materialise an article's file-set (body renditions and supplementary alike).
- **A new resolver:** write an async `ArticleIds -> ArticleIds` that fills gaps
  via `ArticleIds.merge` and never overwrites a known id.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
```
