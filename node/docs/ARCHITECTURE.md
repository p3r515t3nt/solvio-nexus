# SOLVIO Node — Architecture

## Roles
```
MAC CORE  = AUTHORITY   (identity, canonical memory, privacy ledger, semantic index,
                         trust, permissions, risk approvals, Home Assistant, voice, router)
NODE      = COMPUTE ONLY (runs registered capabilities; returns RESULTS)
```
The Core sends a capability request; the node computes and returns a result. **The
Core decides all actions.** A node never mutates Core state and never holds authority.

## First node
`node_id = hetzner-main` on the 24/7 Hetzner worker (`.177.22`). It is a *remote
worker*, on the public internet → treated as **lower trust than home**. It is
reachable **only** over WireGuard + mTLS, never on the public interface.

## Layers
```
transport (aiohttp, mTLS, bound to WireGuard IP)
  -> runtime  (protocol check -> target check -> lookup -> validate -> limit -> timeout)
    -> registry (explicit capabilities only)
      -> capability.invoke(validated_input) -> validated_output
```
No layer executes arbitrary strings. The protocol/registry/capability layers are
platform-neutral; only deployment (systemd vs. Windows service) is platform-specific.

## Data & statelessness
Foundation capabilities are **stateless** and hold **no persistent data**. The
personal **semantic index stays Mac-only**; remote workers are **stateless compute**
and never receive a persistent copy of personal data.

## What this step proves
Node protocol, identity, capability registry, secure connection (WireGuard + mTLS),
limits, logging, service lifecycle, Mac→Hetzner communication, and the authority
boundary — with two harmless capabilities. No research/agent/Qwen capability yet.

## Background jobs
The node can execute durable background jobs (POST/GET/cancel under /v1/jobs,
WireGuard+mTLS only) for capabilities that opt in via supports_background=True (21.2G:
research.fetch_public_url). State lives in a separate jobs.sqlite3 (ephemeral
operational state — never memory/authority). The Mac Core owns intent, idempotency,
and trust; the node owns queue + execution. See BACKGROUND_JOBS.md.
