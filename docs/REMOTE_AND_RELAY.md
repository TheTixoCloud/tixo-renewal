# Remote Management and Relay Mode — Limitations (Read First)

**Both features below are marked EXPERIMENTAL and are disabled by default.** Their
protocols, authentication, and local behavior are fully implemented and tested — but
**neither has ever been validated across two genuinely separate machines.** Every test
exercising "two hosts" in this project's history has used a simulated second host on the
same localhost. Treat both as "implementation complete, cross-host validation pending,"
never as "production-ready."

## Relay worker (Telegram RELAY connectivity mode)

**Purpose:** lets an Iran-side host that cannot reach `api.telegram.org` directly relay its
Telegram bot traffic through a separate VPS that can.

**What is real and tested:**
- `relay_worker.py` is a complete, runnable program (`renewal_cli.py relay-worker-run`)
  that verifies a signed envelope (HMAC signature, durable SQLite replay protection,
  timestamp freshness), executes the requested Telegram API call, and signs the result
  back — exercised against a real local HTTP server over a genuine socket.
- `telegram_transport.RelayClientTransport` is the Iran-side client, drop-in compatible
  with the Direct/SOCKS5 transports.
- Request bodies are capped (1MB) before being read into memory.

**What is NOT yet done:**
- No real cross-network run. Latency, intermittent connectivity, NAT/firewall behavior,
  and real two-machine clock skew have never been exercised.
- Document/file delivery is not supported over this transport at all (raises
  `NotImplementedError` by design).

**Before relying on this for anything real:** provision a genuine second host, enroll it
as a relay worker, and run a real end-to-end test (send a real Telegram message through it)
before enabling RELAY mode for anything that matters.

## Remote management agent

**Purpose:** lets a separate central management server query a hypervisor host's status
without ever being handed that host's Virtualizor API credentials.

**What is real and tested:**
- `agent_server.py` (`renewal_cli.py agent-run`) exposes exactly one endpoint, `/query`,
  accepting a signed envelope naming one of an explicit **READ-ONLY** query allowlist
  (`snapshot`, `host`, `network`, `inventory`, `renewals`, `health`) — no mutating action
  is reachable through this protocol at all.
- `agent_server.query_agent()` is the management-server-side client.
- Verified never to call the Virtualizor API directly (only `control_center_api.py`).
- Request bodies are capped (1MB).

**What is NOT yet done:**
- No real cross-network run.
- **No actual multi-host aggregating "management server" program exists.** Only the
  protocol and the client-side `query_agent()` helper are implemented — there is no
  dashboard that polls several enrolled agents and presents a combined view. Building that
  would most naturally extend `web_server.py`'s page patterns to a multi-host view.

## Shared limitation: transport confidentiality

Both the relay worker and the remote agent speak **plain HTTP** by default. The HMAC
signing scheme gives **authenticity and integrity** (a tampered or forged request/response
is detected), but **not confidentiality** — anything sent is visible to a network observer
between the two hosts. If either must be reached from outside `127.0.0.1`, put a TLS
reverse proxy or VPN in front. Neither this repository nor its test suite has ever
exercised either feature with real transport encryption in place.

## Secrets and enrollment

Each side's shared secret is stored independently, with the same `0600`-enforced
discipline as every other secret in this project, and rotation is an atomic overwrite (the
old secret stops working immediately once rotated — no grace period). Neither feature ever
shares a secret or a filesystem with the other host.
