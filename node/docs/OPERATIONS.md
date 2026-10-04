# SOLVIO Node — Operations

First node: `hetzner-main` on `203.0.113.10` (Ubuntu 24.04). The node runs under a
**dedicated non-root user**, reachable **only** over WireGuard + mTLS.

## 1. WireGuard overlay (`10.91.0.0/24`)
Peers: **Mac `10.91.0.2`**, **Hetzner `10.91.0.1`**. Not a full tunnel — only the
SOLVIO node network; no default-route or DNS changes.

Hetzner (`/etc/wireguard/wg0.conf`):
```
[Interface]
Address = 10.91.0.1/24
ListenPort = 51820
PrivateKey = <hetzner-private>
[Peer]                      # Mac
PublicKey = <mac-public>
AllowedIPs = 10.91.0.2/32
```
Mac (`wg-quick` config): `Address = 10.91.0.2/24`, `[Peer]` = Hetzner public key,
`Endpoint = 203.0.113.10:51820`, `AllowedIPs = 10.91.0.1/32` (only the node — LAN
and Internet are untouched). Bringing the tunnel up on macOS requires `sudo`
(`wg-quick up`) — this is the single human-gated step.

**Firewall:** open only WireGuard UDP `51820` on the public interface; the node API
port is **never** opened publicly. Pre-existing SSH(22)/HTTP(80)/HTTPS(443) are
preserved. After any UFW change, keep the current SSH session open and verify a
second SSH connection before continuing.

## 2. Certificates (SOLVIO CA + mTLS)
```
openssl: CA key+cert (0600 key) ; server cert for node ; client cert for mac-core
```
Server key/cert live on the node (`/etc/solvio-node/certs/`, 0600, owned by the
service user). The CA **private** key stays off the node. No key is committed.

## 3. Service user & files
```
useradd --system --home /var/lib/solvio-node --shell /usr/sbin/nologin solvio-node
# NOT root, no sudo, no docker group, no privileged groups
/opt/solvio-node/            code + venv (read-only to the service)
/etc/solvio-node/config.json runtime config (listen = 10.91.0.1, cert paths, limits)
/var/lib/solvio-node/logs/   JSON logs (rotating)
```

## 4. systemd (`/etc/systemd/system/solvio-node.service`)
Hardened: `User=solvio-node`, `NoNewPrivileges=yes`, `PrivateTmp=yes`,
`ProtectSystem=strict`, `ProtectHome=yes`, `CapabilityBoundingSet=` (empty),
`RestartSec` backoff, `MemoryMax`/`TasksMax` limits, `ReadWritePaths=` only the log
dir. `Restart=on-failure`. It must not affect the existing web-app containers.

## 5. Config (no secrets)
`/etc/solvio-node/config.json` — `node_id`, `listen_host` (WireGuard IP),
`listen_port`, TLS **paths** (not inlined keys), limits, log path,
`enabled_capabilities`.

## 6. Health & failure
- `sudo -u solvio-node curl --cert mac ... https://10.91.0.1:8443/v1/health` (over WG).
- Crash/restart: systemd restarts with backoff; foundation capabilities are stateless
  → no data to corrupt.
- If no local embedding/compute provider is reachable, SOLVIO retrieval falls back to
  FTS — the node is never on the critical path for core memory.

## 7. Observability
Structured JSON logs, rotated (5×5 MB). No payload content, no external telemetry,
no Sentry/analytics.

## 8. Search-provider credential (Brave, systemd `LoadCredential`)
`research.search_public_web` queries an external provider (Brave). The provider is
selected by `Environment=SOLVIO_SEARCH_PROVIDER=brave` in the unit; the **API token is a
runtime secret delivered by systemd, never committed**.

Secret-path contract (root-owned, outside git):
```
/etc/solvio-node/secrets/             directory  root:root  0700
/etc/solvio-node/secrets/brave_token  file       root:root  0600   (raw token, no trailing newline)
```
Fresh-server setup (as root on the node; the token is never echoed to the terminal):
```
install -d -m 700 -o root -g root /etc/solvio-node/secrets
IFS= read -rsp "Brave API key: " K && printf "%s" "$K" > /etc/solvio-node/secrets/brave_token \
  && chmod 600 /etc/solvio-node/secrets/brave_token && unset K
```
The unit line `LoadCredential=brave_token:/etc/solvio-node/secrets/brave_token` makes
systemd copy the secret at start to `/run/credentials/solvio-node.service/brave_token`
(0400, readable only by the service user); the provider reads it via
`$CREDENTIALS_DIRECTORY/brave_token` (see `SEARCH_PROVIDER.md`). The token is never in the
unit, `config.json`, an environment variable, git, or logs. Provider selection is
environment; the secret itself is a file.

Verify: `systemctl is-active solvio-node` = `active`, and `research.search_public_web`
reports `configured=true`. Without the file the node still starts; search is
`configured=false` and fails closed (no crash).
