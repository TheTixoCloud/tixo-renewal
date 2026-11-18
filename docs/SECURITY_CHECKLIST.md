# Security Checklist (Final QA Pass)

This is a real review of the code, not an inference from "zero third-party dependencies"
or "no `shell=True`" — those are necessary but not sufficient conditions for security.

## Findings and fixes from this pass

| Area | Finding | Status |
|---|---|---|
| Session cookie | Never marked `Secure`, even when TLS was enabled | **Fixed** — `WebApp.tls_enabled` marks the login cookie `Secure` whenever a real cert/key pair is configured |
| Request size | `web_server.py`/`relay_worker.py`/`agent_server.py` all trusted client `Content-Length` unboundedly before reading into memory | **Fixed** — all three now cap body size and return `413` before reading past the cap |
| Package reproducibility | `build_package.sh` produced a different checksum every run (tar/gzip embedded mtimes) | **Fixed** — deterministic `--sort=name --mtime=@0 --owner=0 --group=0` + `gzip -n` |

No other critical or high-severity issue was found in this pass. Areas reviewed and found
sound (with reasoning):

## Authentication and session handling
- PBKDF2 password hashing (`security.hash_password`/`verify_password`), opaque random
  session tokens in an `HttpOnly` cookie, server-side SQLite sessions that re-read the live
  user record on every request (a disabled account or role change takes effect on the very
  next request, not at next login).
- Login rate limiting via `security.check_rate_limit`, keyed per remote address.

## Authorization boundaries
- Two roles (`admin`/`viewer`), checked via `_require_admin()` on every mutating route.
- The remote agent's query allowlist contains **zero** mutating actions — reviewed
  explicitly; there is no code path from a signed query envelope to a renewal/grant/
  credential change.
- Emergency-grant/manual-review mutations require `admin` + a valid CSRF token.

## CSRF and request validation
- Deterministic per-session HMAC-derived tokens (`derive_csrf_token`), checked via
  constant-time comparison on every mutating POST — verified by dedicated
  reject-missing/reject-wrong/accept-correct tests against a real route.

## Secret management
- Every secret (Virtualizor API creds, Telegram bot token/SOCKS5 password/relay secret,
  Web user store/CSRF key, relay/agent shared secrets, backup passphrase) is written
  atomically with `0600` and every `load_*` function fails closed if permissions have
  loosened. `install-repair` re-tightens drifted permissions.
- No hardcoded secret, credential, or token found anywhere in `implementation/` (grep
  swept for `password=`/`secret=`/`token=` literals).

## Encryption
- GPG symmetric AES256 via subprocess, passphrase piped over **stdin**
  (`--passphrase-fd 0`), never a CLI argument `ps` could reveal, never hand-rolled crypto.
- Relay/agent protocols use HMAC-SHA256 signed envelopes with durable (SQLite) replay
  protection and timestamp-freshness checking.

## Backup restore safety
- `restore_full_backup_archive()` integrity-verifies the archive and extracts to a staging
  directory only — never overwrites live data on its own.
- `install-rollback` (which DOES apply a restore) safety-copies the current state first.

## TLS configuration
- Manual certificate paths only; `validate_https_prerequisites()` confirms the cert/key
  load together (catches a mismatched pair before a confusing runtime failure) and the
  port is free — no network calls, no CA trust assertion.
- `request_automatic_certificate()` exists, is unit-tested via dependency injection, and
  has **never executed a real `certbot` call** — no public domain was available to test
  against safely in this environment.

## Path traversal
- No web route accepts a filename/path from request input and opens it directly — the
  only path-like input (`/vps/<id>`) is strictly parsed as an integer before any lookup.
- CLI-level `--backup-file` arguments are not sanitized against `../`, but this is CLI
  input from an operator who already has equivalent filesystem access as the user running
  the command — no privilege boundary is crossed.

## Command execution
- No `shell=True` anywhere in `implementation/`. Every `subprocess.run`/`Popen` call uses
  list-args. `gpg`/`certbot` invocations use injectable `run_fn`s in tests, real
  subprocess calls in production, with the secret passed over stdin, never argv.

## SQL injection
- Every query uses `?` placeholders. The one place an f-string touches `.execute()`
  (`inventory.py`'s column-migration helper) interpolates only hardcoded, internal
  `(table, column, type)` tuples from a fixed tuple in the source — never user input.

## Sensitive information in logs
- Grepped every `print()`/`_audit()`/`record_event()` call site touching
  password/secret/token-shaped data: audit details are always non-secret identifiers
  (usernames, archive filenames, review IDs), never a password or token value. Exception
  messages from secret-loading functions never include the secret itself.

## Resource consumption on a busy hypervisor
- Backup/document delivery size caps checked **before** reading a file into memory.
- All three HTTP servers now cap request body size before reading (see fix above).
- Live network monitoring reads one raw kernel counter per poll — no persistent background
  thread beyond the explicitly-started HTTP server threads, no busy-loop (every CLI
  `*-run` command sleeps 1s per iteration).

## Known accepted risk (documented, not silently ignored)
- An operator with root/filesystem access to the host has equivalent access to every
  secret this software stores — `0600` protects against other unprivileged local users,
  not against the host's own root. This is inherent to a single-host secrets-at-rest model
  and is explicitly documented, not a defect.
- Relay worker / remote agent provide authenticity+integrity, not confidentiality, over
  plain HTTP by default (see `REMOTE_AND_RELAY.md`).
