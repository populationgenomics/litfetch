# Batch resolution plan

Plan for adding a **batched** resolver surface to litfetch: enrich a whole
sequence of `ArticleIds` in one pass, amortizing each source's rate domain
across N papers instead of paying it per paper.

## Principle

**Identifier resolution is rate-domain-bound, and the rate domain is per-source,
not per-paper.** NCBI's ID Converter maps ~200 ids in one request; OpenAlex's
works filter maps ~50. Resolving one `ArticleIds` at a time throws that away —
it hits the same NCBI/OpenAlex budget once per paper. Batching collapses N
lookups into `ceil(N / cap)` requests.

Batching is a property of the **resolver**, not of the consumer. The bundled
resolvers (Europe PMC, NCBI ID Converter, Semantic Scholar) are general cross-id
sources with no consumer coupling; their batched forms are equally general and
belong here, by the same scope rule that puts the per-item forms here. A
consumer should not have to reimplement `doi -> pmcid` in bulk just because
litfetch only exposes it one id at a time.

Batch resolution is an **upfront pre-pass**, distinct from the fetch ladder.
`fetch_body` is a single-article operation with demand-driven, memoised
per-item resolution; it stays exactly as-is. The batch pre-pass enriches every
bundle first, then the caller fetches each body with an already-complete bundle
(`fetch_body(enriched, resolver=None)`). The two are complementary, not
competing: the pre-pass owns the shared rate domain; `fetch_body`'s per-item
resolver owns the one-off single fetch.

## Current state

- **`Resolver` is per-item.** `Resolver = Callable[[ArticleIds, Http],
  Awaitable[ArticleIds]]` (`resolvers.py`). It takes one bundle, returns one
  enriched bundle, and must never overwrite a known id (`ArticleIds.merge`).
- **`chain()` early-stops on completion.** It runs resolvers in order over one
  bundle until `pmid`, `pmcid`, and `doi` are all known, then stops.
  `default_resolver()` is `chain(EuropePmcResolver(), NcbiIdConverterResolver())`.
- **`NcbiIdConverterResolver` already covers `doi`.** `_idconv_query` sends
  whichever of `pmid`/`pmcid`/`doi` the bundle carries with an explicit
  `idtype`; the converter echoes the other two. `doi -> pmcid` is not a gap — it
  is one request per DOI.
- **`fetch_body` invokes the resolver once per article** (`sessions.py:205`),
  memoised across the ladder for that one article. There is no path to resolve
  across a set of articles in one call.

So the gap is not a missing *mapping* — it is a missing *fan-in*. A consumer
resolving thousands of DOI-only papers must either pay NCBI/OpenAlex once per
paper through the per-item resolvers, or reimplement the batched calls itself.
The latter has already happened downstream (a batched idconv at 200/call and a
batched OpenAlex works resolver at 50/call), which is the signal that these
belong in litfetch.

## Design decisions

1. **A distinct `BatchResolver` type — not a batch method with a per-item
   default.**

   ```python
   BatchResolver = Callable[
       [Sequence[ArticleIds], Http],
       Awaitable[tuple[Sequence[ArticleIds], set[int]]],
   ]
   ```

   The sequence is length- and order-preserving: element `i` of the result is
   element `i` of the input, enriched (per-element `merge`, never overwriting a
   known id). The `set[int]` is the **abandoned indices** — elements whose
   lookup was given up on after retry-exhaustion, still un-answered, distinct
   from a definitive no-match (decisions 3 and 7). The failure signal rides this
   tuple; it never enters `ArticleIds`, which stays `str | None` (decision 7).

   Rejected: adding `resolve_many` to `Resolver` whose default maps `__call__`
   over the sequence. A per-item resolver would then satisfy the batch type
   while silently issuing N requests against the exact rate domain the batch
   surface exists to collapse — the N+1 footgun, type-laundered. Make bulk
   explicit so a per-item resolver cannot masquerade as batched. A source with
   no bulk endpoint (e.g. Europe PMC search, one query per pmid) simply is not
   offered as a `BatchResolver`; a caller wanting it in bulk composes it per-item
   itself and owns that cost visibly.

2. **Three batched implementations: NCBI ID Converter, OpenAlex, and Europe
   PMC.** All keyless, all general cross-id sources. Three, not two, because the
   pre-pass replaces the per-item chain (`fetch_body(enriched, resolver=None)`),
   so it must reach parity: dropping Europe PMC would silently lose the
   `pmid -> pmcid` hits only it has (UKPMC-only author-manuscript deposits NCBI
   and OpenAlex do not carry). Europe PMC's search is not intrinsically
   per-item — a Lucene `(EXT_ID:a OR EXT_ID:b OR ...) AND SRC:MED` query maps
   many pmids in one request (`pageSize` to 1000), correlating each result's
   `pmcid` back to its pmid. So it batches like the other two.

   - **NCBI ID Converter (batch path).** Same endpoint and record mapping as the
     per-item `NcbiIdConverterResolver`; only the fan-in differs. Give the class
     both `__call__` (one) and a batch entry point sharing `_pmcid_with_prefix`
     and the record→`ArticleIds` mapping — one source of truth for the NCBI
     shape. The batch request **omits `idtype`**: the converter auto-detects the
     scheme per id, so a mixed-scheme batch (some DOIs, some PMIDs) is one call.
     Chunk the wire list at the converter's 200-id cap internally.

   - **OpenAlex (new `OpenAlexResolver`).** The works endpoint takes a
     `filter=doi:<a>|<b>|...` OR-list (~50 ids/call) and returns each work's
     `ids` (pmid/pmcid). **Id-only**: `select` is restricted to the id fields
     (plus the echoed DOI). It is **doi-keyed**, so the residue it can help is
     *doi-bearing papers NCBI missed* (a DOI in PubMed-but-not-PMC, or not in
     PubMed at all). A pmid-only paper NCBI cannot route has no DOI to hand
     OpenAlex — genuinely unresolvable, not abandoned. Ids come back as URLs;
     strip to the bare id. Chunk at 50 internally.

   - **Europe PMC (batch `EuropePmcBatchResolver`).** OR'd `EXT_ID` search
     (above), pmid-keyed, resolving `pmid -> pmcid`. Chunk the OR-list
     internally.

   Contact: each sends the session contact (`http.contact`) as its etiquette
   parameter (`email` for NCBI, `mailto` for OpenAlex, `email` for Europe PMC),
   omitted when unset — matching the existing per-item resolvers. No hardcoded
   address.

   OpenAlex has no `Rate` member yet; it would default to `Rate.DEFAULT` (no
   throttle) and trip its ~10 req/s polite limit once chunks fan out. Add
   `Rate.OPENALEX`.

3. **A batch-chain driver with per-item completion tracking.**

   ```python
   def chain_batch(*resolvers: BatchResolver, required: Iterable[str] = ('pmid', 'pmcid', 'doi')) -> BatchResolver: ...
   ```

   `chain_batch` returns the `BatchResolver` tuple: the enriched sequence and the
   union abandoned set. Each resolver receives only the elements still missing a
   `required` field; elements already complete pass through untouched and keep
   their index. This is the batch analogue of `chain()`'s early-stop, expressed
   per-element instead of per-call — it lets "OpenAlex only for what NCBI missed"
   fall out naturally and keeps the second source's budget off already-resolved
   papers.

   **Abandoned-set semantics.** An index is in the final set iff the element is
   *still* incomplete on `required` **and** some resolver abandoned it after
   retry-exhaustion. An element a later resolver completes is dropped from the
   set; one every resolver answered with a definitive no-match never enters it
   (a genuine absence — retrying will not help). This is the whole point of
   carrying abandonment separately: a caller retries only the abandoned slice and
   converges, instead of re-running the whole batch and re-paying the budget.

   `required` is parameterizable (default all three) because a caller resolving
   for the PMC ladder needs only `pmcid`; forcing all three would spend calls
   chasing a `doi`/`pmid` the ladder never keys on. Predicate reuses
   `ArticleIds.has`. Empty or unknown `required` raises.

   Rejected: running every resolver over the full batch. It wastes the later
   resolver's rate budget on complete papers and cannot express partial
   fan-out.

4. **Chunking, wire-dedup, and retry live in a shared `_run_chunked` helper, not
   in the driver.** Each resolver self-chunks at its own cap; the driver
   (`chain_batch`) knows neither the cap nor any concurrency knob. The helper
   takes a `key` function (the wire id for an element, or `None` when the source
   cannot key on it — passed through untouched, never queried), dedups distinct
   keys, chunks at the cap, hands each chunk to the resolver's single-chunk
   mapping call, and fans each result back to every index sharing that key. A
   batch with repeats costs one lookup per distinct id; order/length are
   unaffected. Putting these three cross-cutting concerns in one helper keeps
   each resolver a thin "map one wire-chunk → records" unit and keeps
   `BatchResolver` a bare callable (a consumer can still supply one).

   Rejected: driver-side chunking (the driver reads a `chunk_size` off each
   resolver). It couples the driver to resolver internals for no gain — the
   return-tuple already carries the failure signal the driver needs, so the
   driver never has to see inside a chunk.

5. **`fetch_body` is untouched.** Batching bodies is a different concern: a body
   fetch returns one `Blob` and its per-source access is not a shared rate domain
   the way an id-mapping lookup is. The pre-pass + per-item-fetch split is the
   whole integration; no ladder change.

   Rejected: teaching `fetch_body` (or a new `fetch_bodies`) to resolve a set
   upfront. It conflates the demand-driven single-article ladder with the bulk
   id pre-pass and muddies the `Blob | None` return.

6. **OpenAlex stays strictly id→id.** The resolver returns cross-ids, never the
   bibliographic record (title/date/venue), even though one works call carries
   both. A consumer that also wants OpenAlex *metadata* fetches it separately.

   Rejected: returning the raw record as opaque provenance so the consumer skips
   the second call. It pushes a bibliographic payload through litfetch's id
   surface, blurring the id/metadata boundary litfetch draws (litfetch is not a
   metadata client). The duplicate call is cheap in practice: a consumer routes
   any paper with a discovered `pmid` to its own metadata source, so the second
   OpenAlex fetch is bounded to the no-`pmid` residual, not the whole population.

7. **Failure is chunk-level and rides the return tuple; `ArticleIds` stays
   `str | None`.** A batch request that fails transport-wise (or 429s through
   every retry) fails its whole chunk. `_run_chunked` marks that chunk's keys
   abandoned and keeps the enrichment from chunks that succeeded; the retry is
   the chunk (backoff, `Retry-After` — already in `_http.get`), never per-item
   (per-item retry against the rate limit that just pushed back multiplies the
   request count by the chunk size). Abandonment (source never answered) is thus
   distinguishable from absence (source answered no) — the former is in the
   abandoned set, the latter is not.

   Rejected: widening the id fields to `str | None | Missing | Error(reason)`.
   That conflates two orthogonal axes — the *value* of an id (a property of the
   paper) and the *outcome of a lookup* (a property of a request) — and the
   outcome is not even per-field (element `i`'s empty `pmcid` may be a genuine
   NCBI absence *and* an abandoned Europe PMC call). It would force every
   consumer of `.pmcid` — `merge`, `has`, every source's `requires` check, the
   whole per-item path — to pattern-match batch-only failure state. The outcome
   lives in the return tuple instead.

8. **Concurrency needs no per-resolver knob.** `Session._pace` holds a per-host
   lock across its wait and spaces sends by `rate.min_interval`, so gathered
   chunks to one host queue and space themselves; different hosts pace
   independently. A resolver fans its chunks into `asyncio.gather` and the
   Session is the concurrency bound. The only rate work is the `Rate.OPENALEX`
   member (decision 2).

## Public surface

Additions to `litfetch.resolvers` (accessed as a submodule, as today —
`resolvers` is not re-exported through `litfetch.__all__`):

- `BatchResolver` — the type alias, `(Sequence) -> (Sequence, abandoned set)`
  (decision 1).
- `chain_batch(*resolvers, required=...)` — the driver (decision 3).
- `default_batch_resolver()` — the batteries-included keyless batch chain
  (NCBI ID Converter batch → Europe PMC batch → OpenAlex), symmetric with
  `default_resolver()`.
- `OpenAlexResolver` — the new id-only works resolver (decision 2).
- `EuropePmcBatchResolver` — the OR'd `EXT_ID` search resolver (decision 2).
- The batch entry point on `NcbiIdConverterResolver` (decision 2).

Internal: `_run_chunked` (decision 4) — the shared chunk/dedup/retry helper the
resolvers delegate to, not part of the public surface.

Naming mirrors the per-item surface (`chain`/`chain_batch`,
`default_resolver`/`default_batch_resolver`) so the two are discoverable
together.

## Work in dependency order

1. `BatchResolver` type + `chain_batch` driver (completion tracking, `required`,
   index-preserving pass-through, abandoned-set union). Testable against a stub
   resolver. **Done.**
2. `_run_chunked` helper (key-based wire dedup, cap chunking, chunk-level retry,
   abandoned-set), then the `NcbiIdConverterResolver` batch path (idtype-less
   mixed batch, 200-id cap). Shares the record→`ArticleIds` mapping with the
   per-item path.
3. `OpenAlexResolver` (id-only `select`, doi-keyed 50-id OR-list, URL stripping),
   plus the `Rate.OPENALEX` member.
4. `EuropePmcBatchResolver` (pmid-keyed OR'd `EXT_ID` search).
5. `default_batch_resolver()` wiring + `docs/api.md` reference.

## Open questions

- **Semantic Scholar batch.** S2 has a `/paper/batch` POST endpoint that maps
  many ids at once. A batched `SemanticScholarResolver` is a natural fourth
  implementation but is keyed and rate-limited; defer until the keyless three
  are in and a coverage gap is demonstrated.
