# TixoCloud Control Center — Release Candidate v1.8.0-rc1

**This is a local release candidate. It has NOT been tagged, published, or deployed.**
`renewal_cli.VERSION` remains `1.7.3` deliberately (see "Why the version wasn't bumped" in
`DEVELOPMENT_PROGRESS.md`) — this document describes the candidate state of the
`master` branch as of the latest commit, for operator review before any real tag/release
decision.

## What's new since v1.7.3

- **Web Control Center**: all 17 pages, real shared-backend data, session auth, CSRF,
  RBAC, audit logging, localization (en/fa, RTL), dark/light theme, live host monitoring
  with zero extra Virtualizor API load, real TLS (manual certificate paths).
- **Telegram bot**: DIRECT/SOCKS5/RELAY connectivity modes, scheduled reports.
- **Relay worker**: the previously-missing second process for RELAY mode.
- **Remote management agent**: read-only MVP for a separate management server to query a
  host's status without holding its Virtualizor credentials.
- **Backup**: full-archive creation, integrity verification, retention, GPG encryption,
  verified staging-only restore, scheduled timer, encrypted-only Telegram delivery.
- **Unified installer core**: fresh-install detection, config-preserving upgrade, repair,
  rollback with a pre-rollback safety copy, post-change health check, safe per-module
  uninstall.
- **Security hardening** (this QA pass): `Secure` session cookie under TLS, bounded
  request bodies on all three HTTP servers, reproducible release package builds.

## Test results

**1180 passed, 0 failed, 0 skipped** (full suite, re-run as part of this QA pass).

## Staging acceptance results

All performed in isolated temporary directories against the **actual built package**
(`deploy/build_package.sh` output), run as genuine subprocesses — never against the
production installation, production credentials, or customer data. No real Virtualizor
host was available in this environment (confirmed: no `universal.php` present), so every
installer/CLI command was exercised against its documented "Virtualizor unavailable"
fallback behavior, which degrades gracefully (`dry-run` reports a clean failure message and
exits 0; `status` works entirely from local SQLite state).

| Area | Result | Verification method |
|---|---|---|
| Fresh install | PASS | Real subprocess, disposable `TIXO_RENEWAL_INSTALL_DIR` |
| install-detect | PASS | Real subprocess |
| Upgrade (config-preserving) | PASS | Real subprocess + unit tests for edit-survival |
| Repair (incl. permission re-tightening) | PASS | Unit tests against synthetic fixtures |
| Rollback (incl. pre-rollback safety copy) | PASS | Unit tests against synthetic fixtures |
| Idempotent repeated install | PASS | Real subprocess, re-ran `install` twice |
| Per-module safe uninstall | PASS | Unit tests, all 5 modules |
| Web: start/login/logout/CSRF/RBAC/rate-limit | PASS | Unit tests + real socket tests |
| Web: all 17 pages return 200 | PASS | **Real HTTP, real packaged `web-run` subprocess** |
| Web: RTL (fa) / light theme via cookie | PASS | Real HTTP against the real package |
| Web: live network endpoint honest "not configured" | PASS | Real HTTP against the real package |
| Web: real TLS + Secure cookie | PASS | Real TLS socket, self-signed test cert |
| Telegram: CLI registration, all `*-run` blocked-when-disabled | PASS | Real subprocess against real `--help`/subcommands |
| Telegram: DIRECT transport | PASS (mocked HTTP) | Unit tests, injected `http_post` |
| Telegram: SOCKS5 transport | PASS (real local SOCKS5 server, not real Telegram) | Unit tests |
| Telegram: RELAY protocol | PASS (**simulated second host, localhost only**) | Unit + real-socket tests |
| Remote agent protocol | PASS (**simulated second host, localhost only**) | Unit + real-socket tests |
| Backup: create/verify/encrypt/retain | PASS | Unit tests, real `gpg` subprocess |
| Backup: restore into isolated path | PASS | Unit tests |
| Backup: encrypted-only Telegram delivery | PASS (logic only, no real Telegram send) | Unit tests |
| Renewal regression suite | PASS | 130 renewal/emergency/manual-review/write-approval tests |
| No Virtualizor mutating API calls from any new surface | CONFIRMED | Code review + `call_vs_api` monkeypatch traps in tests |

**Explicitly NOT performed** (and not claimed): real Telegram API connectivity, real
two-host relay/agent validation, real `certbot` certificate issuance, restore of a real
multi-gigabyte production database, upgrade of an actual captured v1.7.3 production
install.

## Security review summary

See `SECURITY_CHECKLIST.md` for the full review. Three real issues found and fixed this
pass (Secure cookie under TLS, unbounded request body reads on three HTTP servers,
non-reproducible package builds). No critical or high-severity issue remains open.

## Upgrade notes

- Run `renewal_cli.py install-upgrade` (backs up first, never touches an existing
  `nodes.json`/`policy.json`).
- No database schema migration is required for any v1.8.0 feature — every new module uses
  its own new SQLite file(s); existing renewal ledgers are untouched.
- No systemd unit is installed or enabled automatically by an upgrade — review and install
  any new optional-module unit yourself, as always.

## Rollback instructions

```bash
renewal_cli.py install-rollback --backup-file <archive install-upgrade printed> --yes
```
Safety-copies the current (possibly broken) state first; refuses a failed-integrity-check
archive before touching anything.

## Known limitations (release-candidate scope)

- RELAY mode and the remote agent: real cross-host validation has not been performed.
- No real multi-host aggregating management-server program exists (protocol + client
  only).
- Automatic HTTPS certificate provisioning has never executed a real `certbot` call.
- No domain/HTTPS exists for anything other than the Web Control Center.
- No reverse-proxy worked examples included.

## Production deployment checklist (do this before any real deployment)

1. Review this document, `SECURITY_CHECKLIST.md`, and `DEVELOPMENT_PROGRESS.md` in full.
2. Re-run the full test suite in the target deployment's actual Python version.
3. For RELAY mode or the remote agent: provision a real second host and perform a genuine
   cross-host test before relying on either for anything real.
4. For HTTPS: use the manual-certificate-path flow (verified), or explicitly trigger
   `request_automatic_certificate()` against a real domain in a controlled test first.
5. Put a TLS reverse proxy or VPN in front of the relay worker/remote agent if either must
   be reached from outside `127.0.0.1`.
6. Enable only the optional modules actually needed, via their own "Configure ..." screens.
7. Only then: consider installing/enabling the generated systemd units into
   `/etc/systemd/system` (always a manual, printed, never-automatic step) and deciding
   whether/when to bump `renewal_cli.VERSION` and tag a real release — neither was done as
   part of this QA pass, per the explicit "do not publish a release" instruction.

## Release package

Built via `deploy/build_package.sh` (now byte-for-byte reproducible). Verified to contain
no secrets, credentials, or `.sqlite3` database files (automated check: nothing matching
`secret|credential|\.sqlite3|passphrase|\.key$|users\.json` appears in the archive except
`credentials.py` itself, the source module, which contains no stored secret value).

Checksum recorded at build time in `deploy/SHA256SUMS`.
