# Springer Nature: API surface and media

What Springer exposes for full text and assets, and how litfetch uses it. Facts
verified against the live API on 2026-07-02 unless marked *(per docs/client)*.
The recurring conclusion: **full text is JATS, served only by the API; figures
and supplementary files are off-API, on an undocumented CDN.**

## API surface

| Endpoint | Host | Returns | Full text | Figures |
| --- | --- | --- | --- | --- |
| `openaccess/jats` | `api.springernature.com` | JATS body, OA only | yes | refs only (`MediaObjects/…`) |
| `openaccess/json` | `api.springernature.com` | metadata + abstract | no | no |
| `meta/v2/json` | `api.springernature.com` | metadata + `url`(openURL html/pdf) + `openaccess` flag | no | no |
| `metadata/json` | `api.springernature.com` | metadata *(per client)* | no | no |
| `xmldata/jats` (TDM / "Full Text API") | `api.springernature.com` *(migrating, below)* | JATS body, OA + subscription | yes | refs only |

Query is `?q=doi:<doi>&api_key=<key>` (also `q=<field>:<value>`, `p` page size,
`s` start). All wrap results in a `<response>`/`records` envelope.

## Keys and entitlement

- Keys are **per product and not interchangeable**: the Meta key 401s on
  `openaccess/json`. Register at `dev.springernature.com`; OpenAccess is free-tier.
- TDM / Full Text (`xmldata/jats`) needs a **text-mining entitlement** and passes
  the key as `api_key=<KEY>/<METRIC>` *(per docs)*. It is the only JATS route for
  **subscription** content; OA content uses `openaccess/jats`.

## Full Text API migration *(per migration guide)*

`xmldata/jats` is moving host: `spdi.public.springernature.app` →
`api.springernature.com`. Path, query params, auth, and response format are
unchanged. New endpoint live 2026-07-22; parallel window to 2026-08-07; old host
retired 2026-08-07. The official `springernature_api_client` still hardcodes the
old `spdi.public.springernature.app` — stale after that date.

## JATS envelope (openaccess/jats, xmldata/jats)

```text
<?xml ...?><?xml-stylesheet href="/resources/spdi-openaccess-jats.xsl"?>
<!DOCTYPE response [ <!ENTITY % article SYSTEM "…JATS…dtd"> … ]>
<response><apiMessage/><query/><result/><records><article …>…</article></records><facets/></response>
```

The DOCTYPE declares `<!ENTITY % …>` **parameter entities**, which `defusedxml`
refuses (`EntitiesForbidden`) — so parsing the whole payload fails. The
`<article>` carries its own namespaces (`xmlns:mml`, `xmlns:xlink`) and is in no
default namespace, so it is self-contained once sliced out. litfetch's
`SpringerFetcher` byte-slices `<article>…</article>`, drops the envelope and
DOCTYPE, and gates on `<body>` presence (body, not an OA flag, is the gate —
matching Elsevier).

## Full text vs metadata

Full text is present **only in the JATS formats**. `openaccess/json` returns
metadata + a structured `abstract`, no body. `meta`/`metadata` are metadata. So
for a renderable body, use JATS; JSON is a metadata/abstract view.

## Figures and supplementary: off-API, undocumented CDN

No Springer API returns figure/supplementary binaries or their URLs — the JATS
carries only relative `MediaObjects/<file>` hrefs, and nothing in any response
(checked across all four APIs) references a media host. They are fetchable off a
CDN whose path is **reverse-engineered** (not advertised anywhere):

```text
https://static-content.springer.com/<seg>/art:<doi>/MediaObjects/<file>
https://media.springernature.com/<size>/springer-static/<seg>/art:<doi>/MediaObjects/<file>
```

- `art:<doi>` is URL-encoded: `art%3A10.1007%2Fs00125-026-06750-1`.
- `<seg>` is keyed by the JATS element and **must match** (cross-segment 403/404):
  - `<graphic>` (images) → `image`
  - `<media>` / `<supplementary-material>` (ESM PDFs, datasets) → `esm`
- `<size>` (media.springernature.com only) ∈ `lw685`, `lw1200`, `full`, …
- Openly fetchable for OA content, no key.

Because the CDN base and the `image`/`esm` segment are undocumented and absent
from every API response, any use is best-effort: construct the URL, fetch on
demand, and **degrade to `None` on 403/404** — never assert the URL is valid.

## XML is API-only, never on the CDN

The article page declares its JATS source in a meta tag —
`citation_springer_api_url = …/xmldata/jats?q=doi:<doi>` — i.e. the API, not a
CDN object. There is no `content/xml/<doi>` (404), and `openurl/xml` falls back
to the HTML article page. The CDN holds only binary assets (images, ESM), which
have stable `MediaObjects/` filenames; the JATS has no static filename (generated
per request by DOI), so it is an API resource.

## PDF

openURL resolver: `https://link.springer.com/openurl/pdf?id=doi:<doi>` →
redirects to `content/pdf/<doi>` → `application/pdf`. Public for OA; needs
entitlement (institutional / EZproxy) for subscription. `meta/v2/json` exposes
this as `url[format=pdf]`; `openaccess/json`'s `url` gives only the `dx.doi.org`
resolver.

## OA at Springer ≠ deposited in PMC

`openAccess: true` does not imply a PMC deposit — deposit is driven by funder
mandates / journal participation, independently. Example:
`10.1007/s00125-026-06750-1` is `openAccess` at Springer but has no PMCID and is
`inPMC: N` at Europe PMC. So the clean figure route (PMC's enumerated files)
covers only the deposited subset; OA-Springer-not-in-PMC has no clean figure route.

## litfetch usage

- **`SpringerFetcher`** (body ladder) uses `openaccess/jats` with the OA key
  (`credentials['springer_api_key']`); returns the sliced `<article>` as
  `media_type=JATS_XML`; the stored `File.uri` excludes the key.
- **Figures / supplementary**: off-API. Prefer the PMC route when the article is
  deposited there; otherwise best-effort CDN as above.
- **`SpringerFileSource`** (file-set) uses Meta JSON
  (`credentials['springer_meta_api_key']`) for the openURL PDF + `openaccess`
  flag, and yields it as a `BODY` `application/pdf` `File`. A non-OA article's
  file carries `credential_key=INSTITUTIONAL` (the per-`File` entitlement marker)
  so the consumer routes the fetch through an EZproxy-style `client_factory`; an
  OA article leaves it `None`. `fetch_file` follows redirects (openURL →
  `content/pdf`). See [ADR 0001](../adr/0001-http-session-seam.md).
