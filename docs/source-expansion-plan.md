# Source-ladder expansion plan

Plan for widening litfetch's fetch coverage, informed by a behavioural
reconstruction of Claude Science's `fetch_article_fulltext` tool (a DOI-keyed
retrieval ladder over Unpaywall, Semantic Scholar, PMC, Crossref TDM, Elsevier,
Springer, an institutional EZproxy, and a doi.org last resort). That tool makes
some decisions litfetch should adopt and several it should not; this doc records
which, the governing principle, and the work in dependency order.

## Principle

**XML is the goal the ladder works hard for; a PDF is a byproduct to collect,
never a reason to stop searching.** A discovered PDF never short-circuits the
XML hunt — it is recorded as an additional file-set member (a `BODY` rendition
with `media_type='application/pdf'`), exactly as PMC's `.pdf` rendition surfaces
today. XML is primary because it is the convertible form; PDF is a fallback the
consumer may OCR or store, and is out of litfetch's rendering scope regardless.

## Current state

- **`fetch_body` ladder is XML-only.** Every ladder fetcher's `fetch()` returns
  JATS (`PmcOaFetcher`, `EuropePmcFetcher`, `BiorxivFetcher`) or Elsevier XML
  (`ElsevierFetcher`). None yields a PDF.
- **The file-set already handles PDFs — but only for PMC OA.**
  `PmcOaFetcher.list_files` enumerates every rendition under the S3 prefix,
  tagging `.xml`/`.pdf`/… stems as `BODY` with `media_type` from
  `mimetypes.guess_type`, and `fetch_file` downloads any by `uri`. `PmcOaFetcher`
  is the *only* `FileSource`; `default_file_sources()` is just `(PmcOaFetcher(),)`.
- **Unpaywall and Semantic Scholar are already contacted** — Unpaywall in
  `source_metadata.resolve_access` (for the licence), S2 in
  `resolvers.SemanticScholarResolver` (for identifiers) — and both responses
  carry a PDF URL (`best_oa_location.url_for_pdf`, `openAccessPdf.url`) that
  litfetch currently discards.
- **No DOI URL-encoding, no retry, no rate-limiting.** DOIs are interpolated raw
  into URLs (e.g. `source_metadata.py` builds `f'{_UNPAYWALL_BASE}/{doi}'`), which
  breaks or mis-parses for DOIs containing `?`, `#`, or spaces. `_http.py` has
  only a timeout and a shared-client context — no backoff, no 429 handling, no
  per-source pacing.

So the gap is *not* "PDF support" (the model was built for it) but **PDF-bearing
sources beyond the PMC OA bucket**, plus the HTTP hygiene every new source needs.

## Design decisions

1. **Collected PDFs surface in the file-set, not through `fetch_body`.**
   `fetch_body` stays `Blob | None` — the full-effort XML body, unchanged. New
   PDF renditions appear via `list_files` alongside PMC's, preserving litfetch's
   "list refs → fetch bytes on demand" split. Rejected alternative: enriching
   `fetch_body` to return the XML body *plus* collected PDF refs — it duplicates
   the file-set concept and breaks the clean return type. A thin combined helper
   ("give me the XML, and tell me what else exists") can come later if the
   two-call pattern chafes.

2. **Two source flavours, matching how a PDF is revealed:**
   - *Listable* sources reveal a PDF URL cheaply, no download — they become
     `FileSource`s contributing PDF `File` refs (like `PmcOaFetcher`). Unpaywall
     and S2 are here, and cost *zero* extra network: they reuse responses
     litfetch already fetches.
   - *Fetch-to-discover* sources only reveal what they hold by being fetched —
     which is why Elsevier/EuropePMC/bioRxiv are ladder `Fetcher`s. Here "collect
     along the way" means: if such a source returns a PDF where XML was hoped
     for, capture it as a rendition rather than discard it. doi.org resolve is
     the canonical case.

## Work items (dependency order)

### 1. Foundations (needed by every new source)

- **DOI-safe URL handling.** Port the reference's `normalize_and_validate_doi`
  and `encode_doi_path`: validate the `10.xxxx/…` shape, percent-encode each
  path segment, and neutralise `.`/`..` dot-segments against path traversal.
  Apply at every site that puts a DOI in a URL (Unpaywall, Crossref, Elsevier,
  the future doi.org/S2 sources). Fixes a latent correctness/security bug today.
- **HTTP hygiene.** Retry with exponential backoff and 429 handling is done, in
  `_http.get`. Per-host polite pacing — a minimum inter-request interval per
  host, notably the NCBI keyed/unkeyed split (~3 req/s vs ~10 req/s) — is
  decided in [ADR 0001](adr/0001-http-session-seam.md): a `Session` owns the
  HTTP client and the shared timing state, with the applicable interval passed
  per call. That change reshapes the HTTP seam (the `http_client` parameter
  becomes a `Session`), so it lands before any new source is threaded through.

### 2. Unpaywall + Semantic Scholar as `FileSource`s — **done**

`UnpaywallFileSource` and `SemanticScholarFileSource` expose the PDF URL each
returns as a `BODY` `application/pdf` `File`, and both are in
`default_file_sources()`. The Unpaywall record is fetched through
`unpaywall.fetch_record`, shared with `resolve_access`; inside a session scope
the response cache serves the second read, so the two callers stay decoupled
without threading a shared record (resolving the open question below).

### 3. doi.org resolve + Crossref TDM

- **Crossref TDM links** — **done.** `CrossrefFileSource` lists `link[]` entries
  flagged `intended-application: text-mining` (PDF and full-text XML) as `BODY`
  renditions (`media_type` from `content-type`). The shared `crossref.fetch_work`
  now backs the Elsevier XML link locator, `relations`, and this source, so a
  scope dedups the works GET. Wiring XML TDM links into the `fetch_body` ladder
  (generalising `ElsevierFetcher` to any publisher) is deferred: it needs TDM
  token handling (see EZproxy/credentials below), and unentitled links 403.
- **doi.org resolve** — **deferred.** Marginal coverage over Unpaywall's
  `best_oa_location` for real friction: doi.org 30x-redirects (httpx doesn't
  follow by default, and `Http.get` doesn't expose the option), and it is
  fetch-to-discover (a GET to classify by `content-type`, then a second GET to
  download — most redirects land on HTML anyway). If revisited: add
  `follow_redirects` to `Http.get` (opt-in) rather than enabling it globally
  (httpx keeps custom auth headers across cross-origin redirects — a key-leak
  footgun).

### 4. Opportunistic / later

- **Springer** OA API — **done.** `SpringerFetcher` (body ladder), keyed on
  `credentials['springer_api_key']`, queries the OpenAccess JATS endpoint by DOI
  and returns JATS gated on an article body; the stored `uri` excludes the key.
  Full Springer surface (APIs, Full-Text migration, off-API figure/ESM CDN
  scheme, OA≠PMC) is mapped in [`sources/springer.md`](sources/springer.md).
- **EZproxy** — **no litfetch code needed.** The `client_factory` seam already
  covers it: a consumer that holds an institutional session passes a client
  configured with the proxy + cookie. The open problem is *operational* and the
  consumer's, not litfetch's: litfetch runs in the cloud, so the entitled
  session (an EZproxy cookie, obtained via SSO in a user's browser) must be
  provisioned to it — litfetch never acquires credentials, it only uses an
  injected client. Honour the reference's rule: *no credential bypasses a
  paywall; OA first.* The entitled-content *file refs* now exist:
  `SpringerFileSource` marks a non-OA PDF `File` with
  `credential_key=INSTITUTIONAL`, the consumer's cue to fetch it via the entitled
  client. The session-IP-binding question is **resolved**: tested from a GCP VM
  (a different IP than the browser that minted the cookie), the UNSW proxy cookie
  fetched paywalled PDFs — the session is **not** IP-bound, so a provisioned
  cookie works from the cloud. Remaining concerns are operational and the
  consumer's: cookie *lifetime* (sessions expire — refresh before expiry), secure
  provisioning, and honouring OA-first / acceptable-use (entitled as fallback).
  The proxy host-rewrite + cookie live in the consumer's `client_factory`, not
  litfetch.
- **File-set size caps** — **declined.** `list_files` returns each `File`'s
  `media_type` and (where known) `size_bytes`, so the caller decides what to
  fetch; a hard cap in litfetch would pre-empt that choice. (PMC fills
  `size_bytes` from the S3 listing; the API-derived PDF sources leave it `None` —
  the size is not known without fetching.)
- **Structured attempt log** — surface what was tried and why each source
  declined, instead of a bare `None`, for agent/debug callers. Changes/augments
  the return contract, so it needs its own design pass; if adopted, pair it with
  credential-scrubbing (redact secret values, and their URL-encodings, from
  returned reasons) as the reference does.

## Non-goals (deliberately not taken from the reference)

litfetch's boundary (`CONTEXT.md`) excludes these; adopting them would re-expand
the scope the redesign just narrowed:

- **Workspace-safe file writes** (`_secure_write`, symlink/traversal refusal) —
  storage is the consumer's job. A good pattern, wrong layer.
- **Abstract / title extraction** — bibliographic metadata, deliberately out of
  scope; resolvers return only `ArticleIds`.
- **`next_step` hints and the fat single-dict return** — that is the monolithic
  tool shape. litfetch's separation (fetch / resolve / access / relations) and
  its multi-identifier + resolver model are cleaner; cherry-pick the *sources*
  and *HTTP hygiene*, not the architecture.

## Open questions

- ~~Should the Unpaywall record be fetched once and shared between
  `resolve_access` and the new Unpaywall `FileSource`?~~ **Resolved:** both go
  through `unpaywall.fetch_record`; a session `scope`'s response cache dedupes
  the GET, so neither caller threads a shared record (see
  [ADR 0001](adr/0001-http-session-seam.md)).
- ~~doi.org / EZproxy are fetch-to-discover; do they belong on the ladder, in a
  discovery pass, or both?~~ **Resolved for the built sources:** the `FileSource`
  seam *is* the discovery pass — `list_files` is allowed to fetch (PMC lists S3,
  Unpaywall/S2/Crossref fetch a record). A source that yields a PDF contributes a
  `BODY` rendition; one that yields body XML for the ladder is a `Fetcher`. No new
  seam needed. (doi.org, if revisited, is a `FileSource`; EZproxy is a
  `client_factory`, not a source.)
- Is a combined "XML body + file-set in one call" helper worth adding, or is the
  `fetch_body` + `list_files` two-call pattern the intended public shape?
