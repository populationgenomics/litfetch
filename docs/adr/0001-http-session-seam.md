# ADR 0001: A Session owns the HTTP client and per-host pacing

Status: accepted (2026-07-02)

## Context

The [source-ladder expansion plan](../source-expansion-plan.md) calls for HTTP
hygiene shared across every fetcher and resolver: retry/backoff (done, in
`_http.get`) and **per-host polite pacing** — a minimum inter-request interval
per host, with the NCBI keyed/unkeyed split (~3 vs ~10 req/s).

Pacing needs timing state (last-request time per host) shared *across* requests.
That state has no clean home in the current design:

- Every public entry point (`fetch_body`, `list_files`, `fetch_file`,
  `resolve_access`, `related_ids`) and every `Fetcher`/`FileSource` method takes
  `http_client: httpx.AsyncClient | None`. `_http.client_ctx` makes an ephemeral
  client per call when none is passed — so state keyed on "the client" is empty
  for the one-off path.
- A module-level `dict[host, pacer]` would be process-global mutable state,
  which `docs/style/python.md` forbids ("hard to test, surprising under
  concurrency, surprising when imported twice").

The rate that applies to a given request is not a per-host constant: it depends
on whether an API key is present (NCBI keyed vs unkeyed). So the *applicable
interval* is a per-call fact, while the *timing state* is shared per host.

## Decision

`litfetch.Session` is the object a caller holds to run litfetch. It replaces the
`http_client` parameter across the whole surface and is the concrete `Http` the
source and resolver layers issue requests on.

- **`litfetch.Session(...)`** — an async context manager. It owns one
  `httpx.AsyncClient`, built via an injectable
  `client_factory: Callable[[], httpx.AsyncClient]` (default: a litfetch-
  configured client — User-Agent, timeout), plus per-host pacing state. It
  builds the client on entry and closes it on exit.
- **Operations are methods on `Session`** — `fetch_body`, `list_files`,
  `fetch_file`, `resolve_access`, `related_ids`. The method passes `self` as the
  `Http` down to the ladder, so a caller threads no HTTP argument: the object it
  holds *is* the context. Module-level functions of the same names are one-shot
  conveniences that open an ephemeral session for a single call.
- **`Session.get(url, *, params, headers, rate=Rate.DEFAULT) -> httpx.Response`**
  layers pace-before-send (and the scoped cache, below) over `_http.get`
  (retry/backoff/429). The applicable interval is the per-call `rate`, decided
  at the call site where key-presence is known; the timing state is shared per
  host on the `Session`.
- **The HTTP primitives live in `_http`, not `sessions`.** `Http` (the one-method
  request protocol), `Rate` (named politeness levels), `RetryPolicy`, and the
  `get` retry helper are in `_http`. `fetchers`/`resolvers`/`relations` depend
  only on `_http` and never on `sessions`; `sessions` imports *them* to delegate.
  This breaks the cycle that operations-as-methods would otherwise create
  (`fetchers` needs the `Http` type; `Session` needs `fetchers` to run the ladder).
- **Sources depend on the narrow `Http` protocol, not the concrete `Session`.**
  `Fetcher.fetch`, `FileSource.list_files`/`fetch_file` take `http: Http`; a
  third-party implementer learns one method and tests with a trivial stub.
  Resolvers become `Callable[[ArticleIds, Http], Awaitable[ArticleIds]]` — the
  `Http` is threaded at call time (symmetric with fetchers), so `chain()` still
  composes and a resolver holds no client of its own.
- **`Session.client`** exposes the raw `httpx.AsyncClient` as an escape hatch for
  what `get` does not cover (POST, streaming). `BiorxivFetcher` already bypasses
  httpx entirely for curl_cffi, so "do your own thing" has precedent.
- **`client_factory` is the injection point** for proxies, an institutional
  EZproxy (a later plan item), and the native-libcurl CA-cert configuration.

### Scoped response cache

`Session.scope()` returns a child session sharing the parent's client and pacing
state but with its own short-lived response cache. Open one per logical unit of
work (a paper). Inside a scope, `get` caches a *deterministic* response (2xx and
4xx-except-429 — never a transient 5xx/429, which the retry layer owns) keyed by
URL + params + headers, and a repeat request is served from it without a network
round-trip. A bare session does not cache; the cache dies when the scope exits.

This resolves the tension a single cache-bearing object would otherwise face:
pooling and pacing want a long-lived session, but a bounded cache wants a
short-lived one. The long-lived `Session` holds the pool and pacing; the
per-scope cache is bounded by the scope's lifetime, so it cannot grow across a
run. It is what lets two consumers of one upstream call (licence and PDF-URL both
read from one Unpaywall record; the Elsevier link and preprint relations both
read from one Crossref record) stay decoupled — neither threads a shared record;
the scope coalesces the duplicate GET.

litfetch is 0.1.0, so `http_client` is removed rather than kept for back-compat.

## Consequences

- Pacing state has an explicit owner — no module-level mutable state, no
  per-call-client emptiness.
- A caller threads no HTTP argument through the operations; `session.scope()`
  bounds the cache to a unit of work without a second parameter.
- Keyed/unkeyed rate is explicit at each call site via `rate=`; omitting it paces
  at the default, so third-party sources need not learn `Rate`.
- Plugin authors couple to a one-method interface (`Http`), not to Session
  construction; the escape hatch preserves full control.
- `Session` is the library facade (transport + pacing + cache + operations). Its
  method bodies stay thin — the ladder walk, resolver chain, and per-source logic
  remain functions in `fetchers`/`resolvers`/`relations`/`source_metadata`; a
  method delegates, passing `self` as the `Http`.
- Wide but mechanical migration: every public function, both source protocols,
  the resolver signature, and every call site move off `http_client`.
- The `client_factory` seam pre-positions EZproxy and CA-cert handling without
  further API change.

## Alternatives considered

- **Module-level per-host singleton** (`dict[host, pacer]` in `_http`). Simplest,
  fully automatic, zero API change — but process-global mutable state, the
  clearest violation of the style rule, and surprising under tests/concurrency.
- **Caller-owned `Pacer` threaded through the API.** Honours no-global-state, but
  pacing is opt-in — the batteries-included path stays unpaced unless the caller
  builds and passes a `Pacer` — and it is a wide API change without the
  connection-factory payoff the Session gives.
- **Session above the protocols, pacing in a custom transport.** Protocols keep
  receiving a raw `httpx.AsyncClient`; a pacing transport built by the factory
  holds per-host state. Smaller diff, but the transport must *infer* the
  keyed/unkeyed rate from the request (implicit), and pacing hides in a transport
  rather than reading as an explicit Session concern.
- **Session as the concrete protocol type** (not a narrow `Http`). Commits
  third-party implementers to the whole Session API and its lifecycle, and makes
  them harder to test. The narrow `Http` protocol gives the same automatic pacing
  for built-ins without that coupling.
- **Free functions keeping a `session=` parameter** (rather than methods). Reads
  `fetch_body(ids, session=s)` at every call in a batch loop; methods drop the
  repeated argument. The cost is that `Session` becomes the facade and the module
  layering has to split (primitives down into `_http`) to avoid an import cycle.
- **Decoupling the two Unpaywall/Crossref consumers by other means.** Threading a
  shared parsed record couples the two callers; a per-call `cache=True` flag on
  `get` bounds growth but pushes cache vocabulary onto every call site. The
  per-scope cache needs neither: it is automatic within a scope and bounded by
  the scope's lifetime, and needs no TTL because the scope *is* the TTL.
