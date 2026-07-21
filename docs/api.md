# litfetch API reference

The public surface of `litfetch` — everything in `litfetch.__all__`, plus the
bundled resolvers in `litfetch.resolvers`. For a task-oriented walkthrough see
the [README](../README.md); this page is the reference.

Conventions:

- Every network call is `async`.
- The operations are **methods on [`Session`](#session)** — `fetch_body`,
  `list_files`, `fetch_file`, `resolve_access`, `related_ids`. Hold a session
  (`async with litfetch.Session() as s: await s.fetch_body(...)`) to pool the
  connection and pace across calls; open a [`scope`](#sessionscope) per unit of
  work to coalesce duplicate requests. Module-level functions of the same names
  are one-shot conveniences that open an ephemeral session for a single call.
  Signatures below show the one-shot form; the method form is identical without
  the implicit session. See [Sessions and HTTP](#sessions-and-http).
- The source and resolver protocols take an `http: Http` — the session running
  them supplies it.
- `credentials: Mapping[str, object]` carries per-user publisher keys; see
  [Credentials](#credentials).
- All data types are frozen dataclasses (or `NamedTuple`); treat them as values.

## Identity

### `ArticleIds`

```python
@dataclasses.dataclass(frozen=True)
class ArticleIds:
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
```

The immutable identity bundle — any subset of `pmid` / `pmcid` / `doi`.
Resolvers enrich it; fetchers consume whichever identifier they `require`.

- `merge(self, other: ArticleIds) -> ArticleIds` — fill this bundle's gaps from
  `other`; a known identifier is never overwritten.
- `has(self, fields: Iterable[str]) -> bool` — whether every named identifier is
  present (e.g. `ids.has({'pmcid'})`).

## Fetching the body

### `fetch_body`

```python
async def fetch_body(          # also Session.fetch_body(self, ...)
    article_ids: ArticleIds,
    *,
    resolver: Resolver | None = None,
    fetchers: Sequence[Fetcher] | None = None,
    credentials: Mapping[str, object] | None = None,
) -> Blob | None
```

Walk the fetcher ladder in priority order and return the first non-`None` body
`Blob`, or `None` when nothing serves it. Resolution is **demand-driven**: when
the next fetcher needs an identifier `article_ids` lacks, `resolver` is invoked
**once** (memoised) to enrich the bundle, then the walk continues. `fetchers`
defaults to [`default_fetchers()`](#default_fetchers). The blob carries raw
bytes — rendering (e.g. XML → markdown) is the caller's concern.

### `Fetcher` protocol

```python
class Fetcher(Protocol):
    name: str
    requires: frozenset[str]

    async def fetch(
        self,
        article_ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: Http,
    ) -> Blob | None
```

`requires` names the `ArticleIds` fields the fetcher needs to act; `fetch_body`
skips a fetcher (or runs the resolver) until they are present. Return the body
`Blob` or `None` if this source can't serve the article.

### Bundled fetchers

| Class | `name` | `requires` | Source |
| --- | --- | --- | --- |
| `PmcOaFetcher()` | `pmc_oa_s3` | `{'pmcid'}` | PMC Open Access S3 bucket (JATS body; also backs the file-set) |
| `EuropePmcFetcher()` | `europe_pmc` | `{'pmcid'}` | Europe PMC REST (`/{pmcid}/fullTextXML`) |
| `ElsevierFetcher()` | `elsevier_oa` | `{'doi'}` | Elsevier article TDM API (needs `elsevier_api_key`); Crossref locates the XML link |
| `SpringerFetcher()` | `springer_oa` | `{'doi'}` | Springer Nature OpenAccess JATS API (needs `springer_api_key`) |
| `BiorxivFetcher(*, impersonate='chrome')` | `biorxiv` | `{'doi'}` | bioRxiv / medRxiv preprints; needs the `biorxiv` extra (`curl_cffi`) to pass Cloudflare |

`BiorxivFetcher` is **not** in `default_fetchers()` — add it explicitly to a
`fetchers=` list.

### `default_fetchers`

```python
def default_fetchers() -> tuple[Fetcher, ...]
```

The production ladder in priority order: `PmcOaFetcher`, `EuropePmcFetcher`,
`ElsevierFetcher`, `SpringerFetcher`. A function (not a module constant) so
callers can prepend their own fetchers without import-time side effects. A
publisher fetcher with no matching credential is a no-op.

## The file-set

An article's file-set is its body renditions plus supplementary material, each a
`File` reference (no bytes). `list_files` enumerates; `fetch_file` materialises.

### `list_files`

```python
async def list_files(         # also Session.list_files(self, ...)
    article_ids: ArticleIds,
    *,
    sources: Sequence[FileSource] | None = None,
    kind: FileKind | None = None,
    credentials: Mapping[str, object] | None = None,
) -> tuple[File, ...]
```

Enumerate the file-set across **all** `sources` (a union, not first-wins).
`kind` optionally filters to `BODY` or `SUPPLEMENTARY`. `sources` defaults to
[`default_file_sources()`](#default_file_sources).

### `fetch_file`

```python
async def fetch_file(         # also Session.fetch_file(self, ...)
    file: File,
    *,
    sources: Sequence[FileSource] | None = None,
    credentials: Mapping[str, object] | None = None,
) -> Blob | None
```

Download one `File`'s bytes, routing to the source whose `name` matches
`file.source`. Returns `None` when no registered source claims it.

### `FileSource` protocol

```python
class FileSource(Protocol):
    name: str

    async def list_files(
        self,
        article_ids: ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: Http,
    ) -> tuple[File, ...]

    async def fetch_file(
        self,
        file: File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: Http,
    ) -> Blob | None
```

### Bundled file sources

| Class | `name` | Needs | Yields |
| --- | --- | --- | --- |
| `PmcOaFetcher()` | `pmc_oa_s3` | `pmcid` | JATS/PDF body renditions + supplementary material |
| `UnpaywallFileSource(*, email=None)` | `unpaywall` | `doi` + a contact | the best-OA-location PDF as a `BODY` `application/pdf` `File` |
| `SemanticScholarFileSource()` | `semantic_scholar` | any id | the `openAccessPdf` URL as a `BODY` `application/pdf` `File` |
| `CrossrefFileSource()` | `crossref_tdm` | `doi` | text-mining `link[]` (PDF + XML) as `BODY` renditions, `media_type` from `content-type` |
| `SpringerFileSource()` | `springer` | `doi` + `springer_meta_api_key` | the Springer openURL PDF as a `BODY` `application/pdf` `File` |

`PmcOaFetcher` appears here as well as in the fetcher ladder — one class
implementing both `Fetcher` and `FileSource`, because the PMC OA bucket serves
the body *and* enumerates the file-set from the same S3 prefix. There is no
separate `PmcOaFileSource`.

A discovered PDF surfaces in the file-set as a `BODY` rendition
(`media_type='application/pdf'`) — never through `fetch_body`, which stays
XML-only. `UnpaywallFileSource` reuses the same DOI-keyed record as
`resolve_access`; inside a [`scope`](#sessionscope) the record is fetched once.
`SemanticScholarFileSource` reads an optional S2 key from
`credentials['semantic_scholar_api_key']` (higher pace; the keyless endpoint
works without it). `SpringerFileSource` uses the Meta API
(`credentials['springer_meta_api_key']`) for the openURL PDF + `openaccess`
flag; a non-OA article's `File` is marked `credential_key=INSTITUTIONAL` (see
[Credentials](#credentials)) so the consumer routes the fetch through an
entitled client. `fetch_file` follows redirects (the openURL resolves to the
PDF).

### `default_file_sources`

```python
def default_file_sources() -> tuple[FileSource, ...]
```

The sources `list_files`/`fetch_file` query by default: `PmcOaFetcher`,
`UnpaywallFileSource`, `SemanticScholarFileSource`, and `CrossrefFileSource`. A
source with no usable identifier is a no-op.

## Resolvers

A resolver enriches an `ArticleIds` bundle so the fetch ladder can act.

### `Resolver`

```python
Resolver = Callable[[ArticleIds, Http], Awaitable[ArticleIds]]
```

Any async `(ArticleIds, Http) -> ArticleIds` is a resolver. It should fill gaps
via `ArticleIds.merge` and never overwrite a known identifier. The `Http` is
threaded at call time — the session running the resolver supplies it — so a
resolver holds no client of its own.

### `chain`

```python
def chain(*resolvers: Resolver) -> Resolver
```

Compose resolvers into one, run in order, stopping early once all three
identifiers are known. Put your own resolver first, fallbacks after.

### `default_resolver`

```python
def default_resolver() -> Resolver
```

A batteries-included, keyless chain: `EuropePmcResolver` + `NcbiIdConverterResolver`.

### Bundled resolvers

Each is importable from `litfetch.resolvers`, constructed with its config, then
passed to `fetch_body(resolver=...)` (directly or via `chain`). Each is a no-op
when the bundle lacks an identifier it can key on.

```python
EuropePmcResolver()
# pmid -> pmcid via the Europe PMC search API.

NcbiIdConverterResolver(*, tool='litfetch')
# any of pmid/pmcid/doi cross-referenced via NCBI ID Converter; always keyless.
# tool + the session contact (if any) identify the caller per NCBI policy.

SemanticScholarResolver(*, api_key=None)
# identifiers cross-referenced via Semantic Scholar's externalIds endpoint;
# api_key optional (the public endpoint is keyless but rate-limited). Paces at
# the keyed or unkeyed S2 rate depending on api_key.
```

Resolvers stand alone as cross-reference tools — call one directly on an
`ArticleIds` inside a session (`await EuropePmcResolver()(ids, session)`) without
fetching anything.

### Batch resolution

Resolving one `ArticleIds` at a time re-pays each source's per-source rate domain
per paper. A `BatchResolver` enriches a whole sequence in one pass, collapsing N
lookups into `ceil(N / cap)` requests. Use it as an upfront pre-pass over a
corpus, then fetch each body with an already-complete bundle
(`fetch_body(enriched, resolver=None)`); `fetch_body` itself is unchanged.

```python
BatchResolver = Callable[
    [Sequence[ArticleIds], Http],
    Awaitable[tuple[Sequence[ArticleIds], set[int]]],
]
```

Length- and order-preserving: result element `i` is input element `i`, merged
(never overwriting a known id). The `set[int]` is the **abandoned indices** —
elements whose lookup was given up on after retry-exhaustion (a transport failure
or a 429/5xx that outlived every retry), still un-answered and *distinct from a
definitive no-match*. Retry only that slice rather than re-running the whole
batch; a genuine absence is never in the set. The failure signal rides this
tuple — `ArticleIds` stays `str | None`.

```python
def chain_batch(*resolvers: BatchResolver, required=('pmid', 'pmcid', 'doi')) -> BatchResolver
```

Composes batch resolvers, feeding each only the elements still missing a
`required` field (the per-element analogue of `chain`'s early-stop). `required`
is parameterizable: a caller resolving for the PMC ladder passes `('pmcid',)` so
later resolvers don't chase a `doi`/`pmid` the ladder never keys on. An index is
in the returned abandoned set iff the element is *still* incomplete on `required`
and some resolver abandoned it; one a later resolver completed is dropped.

```python
def default_batch_resolver() -> BatchResolver
```

The batteries-included keyless chain, symmetric with `default_resolver` and
reaching the same coverage: `NcbiIdConverterBatchResolver` →
`EuropePmcBatchResolver` → `OpenAlexResolver`.

```python
NcbiIdConverterBatchResolver(*, tool='litfetch')
# any of pmid/pmcid/doi, one auto-detecting call per 200-id chunk (no idtype, so
# a mixed-scheme batch resolves together). Shares the record mapping with the
# per-item NcbiIdConverterResolver.

EuropePmcBatchResolver()
# pmid -> pmcid, OR-ing each 100-pmid chunk into one EXT_ID search. Covers the
# UKPMC-only PMC ids NCBI lacks — why the batch chain reaches per-item parity.

OpenAlexResolver()
# doi -> pmid/pmcid via the works filter, 50 DOIs/call, id-only (no bibliographic
# record). Covers the doi-bearing papers NCBI could not route. The session
# contact is sent as mailto (polite pool); paces at Rate.OPENALEX.
```

Each dedups repeated ids on the wire (one lookup per distinct id, fanned back to
every matching index) and self-chunks at its own cap; concurrency is bounded by
the session's per-host pacer, not a per-resolver knob. A source with no bulk
endpoint is simply not offered as a `BatchResolver` — compose it per-item and own
that cost visibly rather than laundering N requests behind a batch signature.

## Access terms

litfetch reports the licence / OA status of a fetched artifact (see `CONTEXT.md`)
but returns it **raw** — mapping to an SPDX id is the consumer's job. The
`basis` field records where the term came from.

### `extract_source_metadata`

```python
def extract_source_metadata(blob: Blob) -> SourceMetadata
```

Read the licence *from the body bytes* — JATS `<permissions>/<license>` or
Elsevier `<openaccessUserLicense>` — with `basis='artifact'` (authoritative for
exactly those bytes). Returns an empty `SourceMetadata` (all `None`) for a media
type that carries no licence (e.g. PDF) or when none is found. Synchronous — it
parses bytes you already hold.

### `resolve_access`

```python
async def resolve_access(     # also Session.resolve_access(self, ...)
    article_ids: ArticleIds,
    *,
    email: str | None = None,
) -> SourceMetadata
```

Assert the licence / OA status from **Unpaywall**, keyed on the DOI, with
`basis='unpaywall'` — for a paper whose bytes carry none. Returns an empty
`SourceMetadata` when there is no DOI, the lookup fails, or Unpaywall has no
record. Unpaywall **requires** an `email`; it defaults to the session `contact`
and can be overridden here — without either, the lookup is skipped.

## Relations

### `related_ids`

```python
async def related_ids(        # also Session.related_ids(self, ...)
    article_ids: ArticleIds,
) -> tuple[Related, ...]
```

Find preprint/published counterparts, keyed on the DOI: follows bioRxiv/medRxiv
preprints to their published version (details API) and consults Crossref
relations bidirectionally. Each hit is a single-DOI `ArticleIds` tagged with its
`RelationType`. Empty when there is no DOI or nothing links.

### `Related`

```python
class Related(NamedTuple):
    relation: RelationType
    ids: ArticleIds
```

### `RelationType`

```python
class RelationType(enum.Enum):
    PREPRINT = 'preprint'
    PUBLISHED = 'published'
```

## Data types

### `Blob`

```python
@dataclasses.dataclass(frozen=True)
class Blob:
    file: File       # the reference this blob materialises
    content: bytes   # the fetched bytes
```

### `File`

```python
@dataclasses.dataclass(frozen=True)
class File:
    kind: FileKind
    source: str                      # name of the source that can fetch it
    media_type: str | None = None
    uri: str | None = None           # upstream location; fetched on demand
    filename: str | None = None
    credential_key: str | None = None
    size_bytes: int | None = None
    description: str | None = None
```

A reference to one file in the file-set, hosted upstream — not its bytes. Only
litfetch can construct one (it knows the upstream layout and auth).

### `FileKind`

```python
class FileKind(enum.Enum):
    BODY = 'body'                    # the article full text
    SUPPLEMENTARY = 'supplementary'  # figures, datasets, tables
```

### `SourceMetadata`

```python
@dataclasses.dataclass(frozen=True)
class SourceMetadata:
    licence: str | None = None   # raw: a CC URL, JATS license-type, or licence text
    access: str | None = None    # raw: e.g. 'open-access', an Unpaywall oa_status
    basis: str | None = None     # 'artifact' (from bytes) or 'unpaywall' (asserted)
```

## Credentials

`credentials` is a plain mapping of per-user keys, passed through to whichever
fetcher/source needs one. Recognised keys:

| Key | Used by | For |
| --- | --- | --- |
| `elsevier_api_key` | `ElsevierFetcher` | a per-user [dev.elsevier.com](https://dev.elsevier.com) key (not a shared service key) |
| `springer_api_key` | `SpringerFetcher` | a per-user [dev.springernature.com](https://dev.springernature.com) OpenAccess API key |
| `springer_meta_api_key` | `SpringerFileSource` | a Springer Meta API key (distinct from the OpenAccess key; the two are not interchangeable) |
| `semantic_scholar_api_key` | `SemanticScholarFileSource` | an optional S2 key; raises the request pace (the endpoint is keyless otherwise) |

A fetcher whose credential is absent simply declines (returns `None`); it is not
an error.

**`INSTITUTIONAL` marker.** A `File.credential_key` is normally a key in this
map. The exception is `litfetch.INSTITUTIONAL` (`'litfetch:institutional'` — the
`litfetch:` prefix keeps it from colliding with a user credentials key): it marks a
file that needs *institutional entitlement* — a subscription reached through an
EZproxy-style client — rather than a map key. `SpringerFileSource` sets it on a
non-OA article's PDF. The consumer routes such a file through its entitled
`client_factory` (see [Sessions and HTTP](#sessions-and-http)); an openly-fetchable
file leaves `credential_key` `None`.

## Serialisation

`litfetch.serde` maps the data types to/from JSON-able `dict`s so a consumer can
persist a file-set in its own store. litfetch owns the *structure*, not the wire
format or storage. Each type has a `*_to_dict` / `*_from_dict` pair:
`article_ids`, `file`, `source_metadata`. These live in `litfetch.serde` and are
not re-exported at the top level — reach them via `from litfetch import serde`.

## Sessions and HTTP

A `Session` owns the HTTP client and the per-host pacing state, and it is the
object callers hold to run litfetch (see [ADR 0001](adr/0001-http-session-seam.md)).
The operations are methods on it; the source and resolver layers receive it as
the `Http` they issue requests on. `Http`, `Rate`, and `RetryPolicy` live in
`litfetch._http` and are re-exported at the top level.

### `Session`

```python
class Session:
    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        retry: RetryPolicy = <default>,
        timeout: float = 30.0,
        contact: str | None = None,
    ) -> None
    async def __aenter__(self) -> Session   # builds the client via client_factory
    async def __aexit__(self, *exc) -> None  # closes it (a scope leaves it open)
    def scope(self) -> Session               # child with its own cache; see below
    @property
    def client(self) -> httpx.AsyncClient    # escape hatch; valid only in-context
    async def get(self, url, *, params=None, headers=None, rate=Rate.DEFAULT,
                  follow_redirects=False) -> httpx.Response
    # operations: fetch_body, list_files, fetch_file, resolve_access, related_ids
```

An async context manager. `client_factory` is the injection point for a proxy,
an institutional EZproxy (see [Institutional access](institutional-access.md)),
or CA-cert configuration; the default builds a client with a litfetch
`User-Agent` and `timeout`. `contact` (an email) is the caller's polite-pool
identity — see [Contact](#contact) below. `get` paces per `rate` then issues a
retrying GET (see [`RetryPolicy`](#retrypolicy)) — and, inside a `scope`, serves
a repeat request from cache; `client` exposes the raw `httpx.AsyncClient` for
what `get` doesn't cover (POST, streaming). `follow_redirects` is off by default
(an API move should surface, not be chased silently); `fetch_file` downloads
pass it through to follow publisher PDF redirects.

```python
async with litfetch.Session() as s:
    blob = await s.fetch_body(ids)
    files = await s.list_files(ids)   # same session: shares pool + pacing
```

### `Session.scope`

```python
def scope(self) -> Session
```

Returns a child session sharing the parent's client and pacing state but with
its own short-lived response cache. Enter one per unit of work (a paper): inside
it, `get` caches a *deterministic* response (2xx and 4xx-except-429; never a
transient 5xx/429) keyed by URL + params + headers, so a duplicate request is
served without a round-trip. The cache dies when the scope exits, so it cannot
grow across a run; a bare session (no scope) does not cache.

```python
async with litfetch.Session() as session:      # long-lived: pool + pacing
    for pid in paper_ids:
        async with session.scope() as s:        # short-lived: cache
            await s.fetch_body(ArticleIds(pmid=pid), resolver=resolver)
```

### `Http`

```python
class Http(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        rate: Rate = Rate.DEFAULT,
        follow_redirects: bool = False,
    ) -> httpx.Response
```

The one-method surface a source or resolver depends on. `Session` implements it.
A third-party fetcher receives an `Http`, calls `.get(...)`, and is faked in a
test with any object exposing that method.

### `Rate`

```python
class Rate(enum.Enum):
    DEFAULT       # no throttle (S3, publisher CDNs)
    NCBI_UNKEYED  # ~3 req/s
    NCBI_KEYED    # ~10 req/s
    S2_UNKEYED    # ~1 req/s (Semantic Scholar public pool)
    S2_KEYED      # ~10 req/s
```

A named politeness rate chosen at the call site (key-presence decides
keyed/unkeyed). `rate.min_interval` is the minimum seconds between requests to
one host; the timing state is shared per host on the `Session`.

### `RetryPolicy`

```python
@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5   # seconds; exponential-with-jitter backoff
    max_delay: float = 8.0
```

Governs `Session.get`'s retry of a transport error or a retryable status (429,
500, 502, 503, 504); a 429/503 `Retry-After` (integer seconds) overrides the
backoff, capped at `max_delay`. Lives in `litfetch._http`; pass a custom one via
`Session(retry=...)`.

### Contact

litfetch ships **no default contact email** — the maintainer's address is not
baked into the package. Set `Session(contact='you@example.org')` to identify
your calls; it flows to two places:

- the default `client_factory`'s `User-Agent` — `litfetch/<version>
  (mailto:<contact>)` (bare `litfetch/<version>` when unset), which Crossref
  reads for its polite pool;
- the polite-pool **params** — Unpaywall's required `email`, NCBI's `email` —
  via `http.contact`, which the sources read at request time.

A `scope` inherits its parent's `contact`. Without a contact: the User-Agent
carries no `mailto`, NCBI/Crossref omit the param (they still answer, just
outside the polite pool), and **Unpaywall is skipped** (it requires an email, so
`resolve_access` and `UnpaywallFileSource` return empty). For a one-shot,
`resolve_access(..., email=...)` and `UnpaywallFileSource(email=...)` take the
contact directly.
