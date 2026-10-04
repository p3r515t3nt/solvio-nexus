# Node Search Provider — research.search_public_web

The node can query an **external third-party search provider** and return normalised,
bounded candidates. **The search provider is external to SOLVIO** — its
titles/snippets/urls and ranking are DATA, not truth; the Mac Core classifies them as
UNTRUSTED_WEB and never treats provider rank as a truth signal.

## Capability

`research.search_public_web` (background-enabled, read-only, stateless):
- input: `{query (≤400 chars), count (≤10), country?, language?, freshness?}` —
  `extra="forbid"`, so a caller can never inject a provider URL, endpoint, API key,
  headers, proxy, or raw params.
- output: `{query_digest, provider, candidates[], duration_ms, provider_request_id}`;
  each candidate is `{candidate_id, url, title, snippet, provider, provider_rank,
  published_at?, language?}`.

## Provider interface (vendor-neutral)

`SearchProvider` (`providers/base.py`): `provider_id`, `search(SearchRequest) ->
SearchResponse`, `health()`, `capabilities()`. Brave is the **first** provider, not an
architectural dependency; Tavily / OpenAI web search / others can be added via the
explicit `KNOWN_PROVIDERS` registry (no dynamic/arbitrary plugin loading). The Core's
contracts never see a provider-specific model.

## Brave adapter (`providers/brave.py`)

Only the official endpoint `https://api.search.brave.com/res/v1/web/search` with the
`X-Subscription-Token` header. The host is fixed (not caller-controlled) — no SSRF
surface. A `429` becomes a rate-limit error (respecting `Retry-After`); a timeout /
connection error becomes a provider-unavailable error (distinct from a node transport
error); a malformed response is rejected.

## Secret handling

The API token lives ONLY in the provider instance, read at runtime from a **file** —
the systemd `LoadCredential` dir (`$CREDENTIALS_DIRECTORY/brave_token`) or an explicit
token-file path — never a plain env var or config JSON. The token is **never** in git,
config, logs, exception text, the HTTP result, or the Core. If no token is present,
the capability is registered but `configured=false` and `invoke` fails closed with a
provider-unavailable error — the node stays healthy and never crashes.

## Logging

Metadata only: `query_digest`, result count, provider, latency, status, rate-limit
metadata. Never the API key, the full query text, snippets, or web content.
