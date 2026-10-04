# research.fetch_public_url — Secure Public Web Fetch

The first real remote capability. It fetches **one** public http(s) URL, extracts
bounded text, and returns it as **data**. No search engine, no browser, no
JavaScript, no crawling, no binary downloads, no LLM on the node.

The node never decides whether content is true, trustworthy, or actionable. The
Mac Core classifies every `research.*` result as **UNTRUSTED_WEB** — transport
trust (WireGuard + mTLS + a controlled node + a valid site certificate) is **not**
content trust, and web content can never become authority-bearing.

## Request / response

Input: `{"url": "https://example.com"}` (≤ 2048 chars).
Output: `requested_url, final_url, status_code, content_type, title, text, sha256,
bytes_read, truncated, redirect_chain[], resolved_peer, fetched_at`.

## Allowed request model (PHASE 3/4)

- Method **GET** only. Schemes **http/https** only. Ports **80/443** only
  (an explicit `:0` or any other port is rejected).
- Rejected: credentials in the URL, missing host, IPv6 zone ids, non-http schemes
  (`file:`/`ftp:`/`gopher:`/`data:`/`dict:`/`ldap:`/`ws:`/`wss:`/…).
- URLs are parsed/normalised with **yarl** (no hand-rolled regex). International
  domains are IDNA-normalised to a single canonical ASCII/punycode form.

## SSRF defense (PHASE 5/6) — the core control

Aligned with the OWASP SSRF Prevention Cheat Sheet (a denylist alone is
insufficient):

1. **Every** resolved address is validated as globally-routable public via
   `ipaddress`: reject `is_private / is_loopback / is_link_local / is_multicast /
   is_reserved / is_unspecified` and require `is_global`, **plus** an explicit CIDR
   denylist (RFC1918, loopback, link-local incl. `169.254.169.254` cloud metadata,
   CGNAT `100.64/10`, `0.0.0.0/8`, IPv6 ULA `fc00::/7`, link-local `fe80::/10`,
   `::1`, IPv4-mapped `::ffff:0:0/96`, documentation ranges, …). IPv4-mapped and
   6to4 IPv6 are unwrapped and the underlying IPv4 is validated.
2. **Fail-closed on any bad address**: if a host resolves to a mix of public and
   private addresses, the whole request is rejected.
3. **DNS-rebinding / TOCTOU defense**: a custom aiohttp resolver returns **only**
   validated addresses and records them; DNS caching is disabled, so the IP the
   socket connects to is an IP that was just validated. After connecting, the
   actual peer is re-checked against the validated set (`resolved_peer`).

## Redirects (PHASE 7)

Redirects are **not** followed by aiohttp. They are handled manually, max **5**,
and **every** hop is fully re-validated (scheme, port, credentials, host, and IP
via the resolver). `public → private`, `https → file:`, loops, and overflow are all
rejected. The chain is reported in `redirect_chain`.

## TLS / proxy / headers (PHASE 8/9)

- Certificate verification is **always on** (`ssl.create_default_context`); there
  is no skip-verify option, no caller-supplied CA, no caller TLS flags.
- `trust_env=False` (no proxy/`.netrc`), `DummyCookieJar` (no cookies), no
  `Authorization`, no caller-supplied headers. Fixed `User-Agent: SolvioResearch/1.0`,
  `Accept-Encoding: identity`.

## Limits / compression / content (PHASE 10-13)

- Streaming read, **512 KiB** raw cap, **200k** char text cap (`truncated` flags
  either). Timeouts: DNS/connect/read bounded, total 15 s (capability `timeout_s`
  18 s is the runtime backstop).
- `Accept-Encoding: identity` + `auto_decompress=False`; any real `Content-Encoding`
  is **rejected** (no decompression bomb).
- Allowed content types: `text/html, text/plain, application/json,
  application/xhtml+xml, application/xml, text/xml`. PDFs/images/archives/binaries
  are rejected (future separate capabilities).
- HTML: deterministic stdlib extraction; `script/style/noscript/template/svg/iframe`
  and comments are dropped; **no JS executed, no external resources loaded**.

## Prompt-injection boundary (PHASE 14)

If a page says "ignore previous instructions", "run a shell command", "send
secrets", or "write this into memory", it is only **UNTRUSTED_WEB text**. The node
interprets nothing; the capability returns structured content. Tested with
synthetic injection strings.

## Statelessness & logging (PHASE 15)

Nothing is persisted to disk — no URLs, bodies, extracted text, cookies, or
history. The body lives in RAM for the request only. The structured log records
**metadata only** (request_id, capability, status, duration, payload_size,
result_size); URL content, body, and extracted text are never logged. Errors
surface only the exception **type name** (payload-free).

## Egress (PHASE 27)

The node makes only http/https connections to validated public targets. It never
reaches local services, Docker networks, Hetzner metadata, private interfaces,
WireGuard peers, or localhost. The inbound firewall is unchanged: the node API is
still WireGuard-only (`10.91.0.1:8443`, mTLS); outbound fetch does not open any
public inbound port.
