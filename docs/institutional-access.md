# Institutional access (EZproxy) via `client_factory`

Reaching **subscription** content (a publisher PDF behind an institutional
licence) is the consumer's job, not litfetch's. litfetch never acquires or holds
a credential; it fetches a `File`'s `uri` through whatever client the `Session`
was given. Institutional access is therefore a `client_factory` the consumer
supplies (see [ADR 0001](adr/0001-http-session-seam.md)). This page documents the
pattern that client must implement; litfetch ships no proxy code.

## litfetch's side

A source that yields entitled content marks the `File` with
`credential_key=`[`INSTITUTIONAL`](api.md#credentials) (e.g.
`SpringerFileSource` on a non-OA article). The consumer routes those files
through its entitled `Session` and everything else (OA, metadata) through a plain
one:

```python
import litfetch

file = ...  # from litfetch.list_files(...)
session = entitled if file.credential_key == litfetch.INSTITUTIONAL else plain
blob = await session.fetch_file(file)
```

## The client the consumer provides

An EZproxy-style proxy is a **reverse proxy keyed on hostname rewriting**: a
request must go to the rewritten host (`www.nature.com` →
`www-nature-com.wwwproxy1.library.unsw.edu.au`) carrying a session cookie scoped
to the proxy domain; the proxy then forwards from the institution's IP. So the
`client_factory`'s client must do two things:

1. **Carry the session cookie** (obtained out-of-band — see below).
2. **Rewrite only subscription-publisher hosts** to the proxy form, via a custom
   transport or a request event hook. Update the `Host` header to match.

```python
# illustrative -- the consumer owns this; litfetch does not ship it
PROXY_SUFFIX = 'wwwproxy1.library.unsw.edu.au'
PROXY_HOSTS = {'www.nature.com', 'link.springer.com', 'www.sciencedirect.com', ...}  # allowlist

def _proxy_host(host: str) -> str:
    return host.replace('-', '--').replace('.', '-') + '.' + PROXY_SUFFIX  # EZproxy encoding

async def _rewrite(request):
    if request.url.host in PROXY_HOSTS:
        new = _proxy_host(request.url.host)
        request.url = request.url.copy_with(host=new)
        request.headers['Host'] = new

def factory():
    return httpx.AsyncClient(headers={'Cookie': cookie}, event_hooks={'request': [_rewrite]})

async with litfetch.Session(client_factory=factory) as entitled:
    ...
```

## Which hosts to rewrite

An **allowlist of subscription-publisher content hosts only** — never metadata,
OA, or API hosts. The proxy is provisioned only for licensed domains; routing
anything else through it is pointless or breaks (and rewriting an OA host that
mixes free and paywalled content — e.g. `www.nature.com` hosts Nature
Communications — would push OA fetches through the proxy and *fail* them if the
session is stale). Leave `api.crossref.org`, `api.unpaywall.org`,
`api.springernature.com`, PMC S3, `doi.org`, and OA PDF hosts **direct**.

Do not use a denylist ("proxy everything except the APIs") — it would route
unknown/OA hosts through the proxy and fail.

## Operational caveats (the consumer's)

- **The session is not IP-bound.** Verified from a cloud VM (an IP other than the
  one that minted the cookie): the cookie fetched paywalled PDFs. So a provisioned
  cookie works from a cloud deployment.
- **Cookie lifetime.** EZproxy sessions expire; the deployment must refresh the
  cookie before it lapses (periodic SSO, or a service identity with proxy
  access). This is the real gating concern for a durable deployment.
- **Provisioning.** Deliver the cookie to the service as a managed secret; never
  commit or log it.
- **OA first.** Use the entitled path only as a fallback, fetch politely, and
  honour the institution's acceptable-use terms. No credential silently bypasses
  a paywall.
