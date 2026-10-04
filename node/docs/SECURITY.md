# SOLVIO Node — Security

## Trust boundary
The node is **compute only** and is treated as **lower trust than the Mac Core**
(it lives on the public internet). It has **no** API for: Home Assistant actions,
memory writes, privacy-ledger access, risk approvals, tool-dispatcher actions, shell
commands, or filesystem mutation. It cannot raise its own authorization.

## No arbitrary execution
There is **no** `/shell`, `/exec`, `/run`, `/eval`, `/cmd`, `/python-exec`, or
equivalent — and none can be added by configuration. Only capabilities explicitly
constructed in `registry.build_registry` exist; unknown ids are rejected at startup
(fail-closed) and at request time (`UNKNOWN_CAPABILITY`).

## Network (defense in depth)
1. **WireGuard** overlay (`10.91.0.0/24`) — only Mac and node are peers. The API
   binds to the node's WireGuard IP; the **public interface exposes no node port**
   (only the WireGuard UDP port, plus the pre-existing SSH/HTTP/HTTPS).
2. **mTLS** — server requires a client certificate signed by the **SOLVIO CA**
   (`CERT_REQUIRED`). A peer without a valid client cert cannot complete the
   handshake and never reaches a handler. The client verifies the server against
   the same CA.

## Certificates (lifecycle)
- **SOLVIO CA** — offline-ish root; its private key is the most protected secret
  (0600, not on the node, never in git).
- **server cert** (`hetzner-main`) and **client cert** (`mac-core`) signed by the CA.
- **Expiry / rotation** — certs are short/medium-lived; rotation = re-issue from the
  CA and redeploy. **Revocation** — remove trust for a client by rotating the CA or
  maintaining a small allowlist of client-cert fingerprints (future).
- Keys have restrictive permissions; **no private key is ever committed**.

## Privacy
Request/result **content** is never logged or persisted. Logs contain only ids,
capability, status, timing, and sizes.

## Negative tests (must hold)
public IPv4 + node port → NOT reachable · WireGuard without mTLS → REJECT · no client
cert → REJECT · wrong CA → REJECT · valid Mac client → PASS · unknown capability →
REJECT · oversize payload → REJECT · malformed request → REJECT · arbitrary command
→ impossible · path-traversal payload → no filesystem effect.

## Public web fetch egress
`research.fetch_public_url` is a bounded outbound-network capability. It may
open http/https connections ONLY to validated globally-public targets and never to
local services, Docker networks, Hetzner metadata, private interfaces, WireGuard
peers, or localhost. Defenses: yarl URL validation (scheme/port/credentials),
`ipaddress` public-only checks + explicit CIDR denylist, a custom DNS-pinning
resolver (rebinding/TOCTOU), manual redirect revalidation, mandatory TLS
verification, `trust_env=False`, no cookies/auth, `Accept-Encoding: identity`,
512 KiB / 200k-char caps. The INBOUND surface is unchanged: the API stays
WireGuard-only + mTLS; outbound fetch opens no public inbound port.

## Background job security
Jobs are mTLS-only; ownership is bound to a CA-signed URI SAN (spiffe://solvio/client/<name>), not the CN/fingerprint (a client reads only
its own jobs; cross-client access returns NOT_FOUND). Only supports_background
capabilities run — no eval/exec/subprocess/shell/dynamic-import. Only
REMOTE_CONTROLLED_ALLOWED jobs are accepted (privacy fail-closed). Input payload is
purged the moment a job is terminal; result TTL 72h; metadata TTL 7d. Logs are
metadata-only (idempotency keys stored hashed; no URL/payload/result content). The job
DB (0600) lives under the existing ReadWritePaths=/var/lib/solvio-node; no new writable
path and no new public inbound route (API stays WireGuard-only + mTLS).

## Identity hardening
Job ownership uses a CA-signed URI SAN in the spiffe:// namespace (a SPIFFE-form URI identity, not a full SPIFFE/SPIRE deployment); exactly one URI SAN is required, not the
certificate CN or fingerprint. Certificate != identity: a rotated cert with the
same SAN keeps ownership; a cert with the same CN but a different SAN is a
different client; a valid CA-signed cert without a SOLVIO URI SAN is rejected by
the job API (no CN fallback). The existing SOLVIO CA is unchanged; the Mac
client cert was reissued with the URI SAN (private key never leaves the PKI dir).

## External search egress
research.search_public_web calls an EXTERNAL third-party provider (Brave). The API token
is runtime-only (systemd LoadCredential dir or a token-FILE path), never in git, config,
logs, exception text, the HTTP result, or the Core. The caller supplies only a bounded
query + params (no url/key/headers/proxy); the endpoint host is fixed (no SSRF surface).
Results are UNTRUSTED_WEB. External egress is additionally gated by the Mac Core
(NO_EXTERNAL_EGRESS default); the node still only enforces bounds and never adds context.
