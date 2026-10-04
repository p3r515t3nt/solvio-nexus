# SOLVIO Node — Capabilities

Every capability declares a contract (see `capabilities/base.py`): `id`, `version`,
`description`, `risk_level`, `privacy_class`, `execution_mode`, `supports_batch`,
`max_concurrency`, `timeout_s`, `persistent_data`, `input_schema`, `output_schema`,
`health()`. Registration is explicit; there is no auto-discovery.

## Foundation capabilities

### `system.health`
- risk `safe`, privacy `none`, `persistent_data=false`
- input: `{}` · output: `{status, uptime_s, load1, ram_available_mb, protocol_version}`
- Exposes NO secrets, env vars, user lists, SSH config, or paths.

### `compute.sha256`
- risk `safe`, privacy `synthetic`, `persistent_data=false`
- input: `{text: str}` (≤ 16 KiB, also bounded by the global request cap)
- output: `{algorithm, hex, input_length}` — stateless, never persisted, never logged.

## Adding a capability (future)
1. Subclass `Capability`, define pydantic `input_model`/`output_model`, implement
   `async invoke(validated_input) -> validated_output`.
2. Add it to `known` in `registry.build_registry` and to `enabled_capabilities`.
3. It runs **only** if both code and config include it (fail-closed).

## Planned (documented only — not implemented)
- Hetzner (Class-C): `background.*`, `monitor.*`, `webhook.*`, `scheduler.*`
- Windows (heavy/agent): `agent.claude-code`, `agent.codex`, `local-model.*`,
  `computer.*`, `embedding.*`

**Agent capabilities (future contract, doc only):** an agent invocation carries
`task`, `workspace_id`, `allowed_tools` (allowlist), `risk_level`, `timeout`, and
context refs — **never** an arbitrary shell command. **Risk approval stays with the
Mac Core; a node can never raise its own authorization.** Claude Code / Codex are
preferred via **locally authenticated subscription/account clients**, not API-key
first. A node must never read browser cookies, extract passwords, or export login
tokens.

### `research.fetch_public_url`
- risk `safe`, privacy `synthetic`, `persistent_data=false`, `max_concurrency=4`
- input: `{url: str}` (http/https, ports 80/443 only) · output: `{requested_url,
  final_url, status_code, content_type, title, text, sha256, bytes_read, truncated,
  redirect_chain, resolved_peer, fetched_at}`
- Fetches ONE public URL and extracts bounded text. SSRF-guarded (public IPs only,
  DNS-pinned, redirects revalidated), TLS-verified, no proxy/cookies/JS/binaries,
  streaming-capped (512 KiB / 200k chars). Stateless; content never logged.
- Result is DATA; the Mac Core classifies it as UNTRUSTED_WEB. See PUBLIC_WEB_FETCH.md.

### `research.search_public_web`
- risk `safe`, privacy `synthetic`, `persistent_data=false`, `supports_background=true`
- input: `{query (<=400), count (<=10), country?, language?, freshness?}` (extra params forbidden)
- output: normalised `{query_digest, provider, candidates[], duration_ms, provider_request_id}`
- Queries an EXTERNAL search provider (Brave first; vendor-neutral interface). Results are UNTRUSTED_WEB; provider rank is not truth. API key is runtime-only (systemd credential/token file), never in git/logs/Core; if unconfigured -> provider-unavailable (node stays healthy). See SEARCH_PROVIDER.md.
