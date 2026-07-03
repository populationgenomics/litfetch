# litfetch

litfetch resolves a scholarly article identifier to its retrievable files —
full-text body and supplementary material — and fetches their bytes. It owns
*what an article's files are and how to fetch them*; it does not own *where a
consumer stores them* nor *how they are rendered* (e.g. XML → markdown).

## Language

### Identity

**Article**:
A scholarly paper litfetch retrieves, identified by an `ArticleIds` bundle.
*Avoid*: paper, document, work, record (a `record` is the consumer's cached wrapper).

**ArticleIds**:
The immutable identity bundle — any of `pmid`, `pmcid`, `doi`. A thin record;
resolvers enrich it, sources consume whichever identifier they `require`.
*Avoid*: identifiers, keys, ids.

### The file-set

**File-set**:
The collection of files that make up a retrieved article — the body and any
supplementary material, in each of the forms (media types) they are available
in. The unifying model litfetch owns and a consumer de/serialises.
*Avoid*: assets, contents, bundle (a bundle is `ArticleIds`).

**File**:
One file in the set — a body rendition or a supplementary item, in one media
type, with a known `source`, `media_type`, `size`, and an upstream location. The
single ref type; supersedes the former split between `AlternateRepresentation`
and `SupplementaryFile`.
*Avoid*: representation, rendition, supplementary file, artifact (a `RawArtifact`
is a File once its bytes are in hand).

**Body**:
The file-set member that is the article full text itself — the file a consumer
renders (e.g. to markdown). Distinct from supplementary material.
*Avoid*: main file, primary document.

**Supplementary**:
A file-set member that is *additional* material — figures, datasets, tables —
not the article body.
*Avoid*: supplement, attachment, extra.

### Sourcing

Every File is hosted upstream (PMC, a publisher). litfetch holds its `uri`,
owning `source`, and the `credential_key` a fetch needs. The consumer cannot
construct these — only litfetch knows the upstream layout and auth.
*Avoid*: remote file, hosted ref, source file.

### Metadata

**Source metadata**:
Facts about *access and provenance* that litfetch owns: the owning `source`, the
upstream `uri`, the `credential_key` required, and the **licence** / access
terms under which the file may be used. Licence carries a **basis** —
*extracted* from the fetched bytes (JATS `<license>`, Elsevier
`openaccessUserLicense`; authoritative for exactly those bytes) or *asserted* by
an external access authority (Unpaywall) when the bytes carry none (a PDF).
litfetch returns the licence raw; mapping to an SPDX id is the consumer's.
*Avoid*: provenance metadata, access info.

**Bibliographic metadata**:
Descriptive facts about the article — title, authors, journal, date.
**Out of litfetch's scope entirely**: it neither owns the shape nor surfaces the
raw provider results, even though its resolvers' API calls return such fields. A
consumer that wants bibliographic data calls the provider APIs itself (it
controls the scoping) and feeds the resulting identifiers to litfetch. See the
boundary below.
*Avoid*: citation, bib data, article metadata.

## Ownership boundary

The seam between litfetch and its consumers. Data flows *through* litfetch
because the consumer can neither construct the file refs (they need upstream
URLs and per-source auth) nor fetch their bytes without it.

**litfetch owns**: identity (`ArticleIds`); the File-set model; the act of
fetching (uri + credential routing per source); source metadata and license; and
the canonical structural de/serialisation of all of these (a backend-agnostic
dict mapping — not a wire format).

**The consumer owns**: placement — filesystem/store layout, blob storage,
record `status`, leases — the *shape* of bibliographic metadata, and *rendering*
(turning a fetched body into markdown or other derived forms).

**litfetch is not a bibliographic-metadata client**: resolvers return only
`ArticleIds` and stay that way — litfetch will not surface the bibliographic
fields S2/NCBI return, even raw. Doing so would force litfetch to fix each
provider call's scope (e.g. S2's `fields=`) and thereby dictate what is fetched;
the only coherent alternative is consumer-controlled scoping, but a consumer in
control of those calls can resolve identifiers itself and hand them to litfetch
directly. So litfetch touches provider APIs for three purposes only — completing
identifiers, fetching files, and resolving **access terms** (licence / OA status
via Unpaywall, which is access metadata, not bibliographic) — and surfaces only
their results. Bibliographic fields stay out of scope.
