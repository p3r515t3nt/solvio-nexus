# Node Background Jobs — Execution Plane

**MAC OWNS INTENT. NODE OWNS EXECUTION.** The node persists a durable operational
queue so a job survives a lost connection or a node restart, but this is **ephemeral
operational state only** — never canonical memory, standing intent, user preference,
or authority. The Mac Core is the authoritative control plane.

## API (mTLS-only, WireGuard-only, like the rest of the node)

```
POST /v1/jobs               submit (idempotent) a background job
GET  /v1/jobs/{id}          job metadata (owner only)
GET  /v1/jobs/{id}/result   job result (owner only)
POST /v1/jobs/{id}/cancel   request cancellation (owner only)
```

There is **no generic action API**: a job may reference ONLY a registered capability
whose `supports_background` is True (in 21.2G: `research.fetch_public_url`). No
eval/exec/subprocess/shell/dynamic-import, no caller-supplied code.

## Identity & ownership

Job ownership is bound to a **CA-signed URI SAN** identity — a SPIFFE-form URI
identity (a CA-signed logical identity carried in the URI SAN, **not** a full
SPIFFE/SPIRE deployment or a verified X.509-SVID). Exactly **one** URI SAN is
required
(`spiffe://solvio/client/<name>`, e.g. `spiffe://solvio/client/mac-core`) — never a
CN, IP, header, caller payload, or the specific certificate/fingerprint. **Certificate
!= identity**: a rotated certificate keeping the same SAN keeps access to the same
jobs; a cert with the same CN but a different SAN is a different client (no CN
spoofing); a valid CA-signed cert without a SOLVIO URI SAN is rejected by the job API
(no CN fallback). A client may read/cancel **only its own** jobs; cross-client access
returns `NOT_FOUND`.

## Durable store

`/var/lib/solvio-node/jobs.sqlite3` (separate from any other DB), WAL +
`foreign_keys` + `busy_timeout`, file `0600`. All operations run in a single-thread
executor, which serialises them so a claim (pick next `QUEUED` + set `RUNNING`) is
atomic. On an `integrity_check` failure the store fails closed and never
destructively recreates itself.

## States

`QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED | RESULT_EXPIRED`, with
`CANCEL_REQUESTED` for a cancel while running and `RUNNING → QUEUED` for recovery/
retry. Transitions are explicit; terminal states are final.

## Privacy (fail-closed)

Only `REMOTE_CONTROLLED_ALLOWED` jobs are accepted. `HOME_ONLY` / `LAN_ALLOWED` /
`CLOUD_THIRD_PARTY_POLICY_REQUIRED` are rejected (`PRIVACY_REJECTED`). Background
persistence never weakens the privacy policy.

## Workers, retries, recovery

Bounded pool (default **2** workers); the store bounds the queue (default **100**
non-terminal → `QUEUE_FULL`). Retries are **classified**: transient (timeout/DNS/
connection) get bounded exponential backoff + jitter up to `max_attempts` (≤5);
permanent content failures do not retry. **At-least-once**, safe because
`research.fetch_public_url` is read-only (future side-effect capabilities must NOT
inherit this). On start the store recovers: `RUNNING`→`QUEUED` (attempts remaining)
or `FAILED`; `CANCEL_REQUESTED`→`CANCELLED`. No job hangs in `RUNNING` forever.

## Retention

Input payload is **purged the moment a job is terminal**. Result payload TTL **72h**
(then `RESULT_EXPIRED`, payload dropped). Metadata TTL **7 days** (terminal jobs only).
No permanent result archive.

## Integrity & logging

Result carries a SHA-256 `result_digest` (integrity/diagnostic metadata, not a trust
signal). Logs are **metadata only** (job_id, status, capability, attempt, sizes,
duration, error_code) — never a URL, payload, result body, or idempotency key
(stored only hashed).

## systemd

Unchanged hardening: non-root `solvio-node`, `NoNewPrivileges`, `ProtectSystem=strict`,
empty `CapabilityBoundingSet`, no sudo/docker. The job DB lives under the existing
`ReadWritePaths=/var/lib/solvio-node`; no new writable path and no public inbound
route (the API stays WireGuard-only + mTLS).
