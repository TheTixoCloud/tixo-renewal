# TixoCloud Control Center v1.8.0 — Stable Release Notes

Supersedes `RELEASE_NOTES_v1.8.0-rc1.md` (kept for history). This is the **stable**
release: `renewal_cli.VERSION = "1.8.0"`, built from the final source after the complete
QA/security/testing pass described below.

**Local status at time of writing: built, tested, and packaged locally. Not yet tagged,
not yet published as a GitHub Release, not yet deployed to any production host.**
Publishing and production deployment each require a SEPARATE, explicit operator approval
— see the Final Approval Report accompanying this release.

## What's in this release (since v1.7.3)

- **Web Control Center**: all 17 pages, real shared-backend data, session auth, CSRF,
  RBAC, audit logging, localization (en/fa, RTL), dark/light theme, live host monitoring
  with zero extra Virtualizor API load, real TLS (manual certificate paths, `Secure`
  session cookie under TLS), version footer.
- **Telegram bot**: DIRECT/SOCKS5/RELAY connectivity modes. **Encrypted backup delivery
  now works over all three modes** — DIRECT and SOCKS5 stream a real multipart upload
  (never loading the archive into memory at once); RELAY delivers through a dedicated,
  independently-capped `/submit-document` endpoint on the relay worker. Bounded per-admin
  retries and duplicate-delivery avoidance.
- **Relay worker** and **remote management agent**: both complete, locally tested
  end-to-end over real sockets. **Still require real cross-host validation** — see
  `REMOTE_AND_RELAY.md`. Both disabled by default.
- **Backup**: full-archive creation, integrity verification, retention, GPG encryption,
  verified staging-only restore, scheduled timer, encrypted-only delivery to Telegram
  admins over any connectivity mode.
- **Unified installer**: fresh-install detection, config-preserving upgrade, repair,
  rollback with a pre-rollback safety copy, post-change health check, safe per-module
  uninstall, installer version display.
- **Network interface display**: compact default view (physical + bridge interfaces
  only), advanced view with category filter (physical/bridge/virtual) and name search —
  never changes which interface the hostnet sampler actually reads.
- **VPS inventory table**: adaptive 3-tier layout (wide/medium/narrow terminal widths),
  Unicode-display-width-aware truncation with ellipsis, unlimited-vs-zero-usage always
  visibly distinct. Pagination/search/filter/sort/detail view unchanged.
- **Security hardening**: `Secure` session cookie under TLS, bounded request bodies on
  every HTTP server (web/relay/agent), reproducible release package builds.

## Test results

**1221 passed, 0 failed, 0 skipped** (full suite, re-run against the final source before
packaging).

## Security review summary

See `SECURITY_CHECKLIST.md` for the full review (authentication/session handling,
authorization boundaries, CSRF, secret management, encryption, backup restore safety,
TLS, path traversal, command execution, SQL injection, sensitive-data-in-logs, installer
rollback, remote-agent authentication, relay replay protection, network binding, resource
consumption). No critical or high-severity issue remains open as of this release.

## Explicitly NOT validated against real infrastructure

- Real cross-host RELAY/remote-agent operation (simulated locally only).
- Real `certbot` automatic certificate issuance (unit-tested via dependency injection
  only — no real domain available).
- A real production Virtualizor host's full data set at scale (restore/upgrade tested
  against small synthetic fixtures and, separately, real production data on
  `ir1.tixocloud.com` — see the accompanying production-preflight report for exactly
  what WAS validated there).
- Real Telegram API connectivity for any mode (all three connectivity modes are tested
  against local mocks/fakes, never the real `api.telegram.org`).

## Upgrade notes (from v1.7.3)

- `renewal_cli.py install-upgrade` backs up config/state first, then re-runs the
  idempotent install steps. **Never overwrites an existing `nodes.json`/`policy.json`.**
- No database schema migration is required for any v1.8.0 feature.
- No systemd unit is installed or enabled automatically by an upgrade.
- New optional modules (Web/Telegram enhancements/relay worker/remote agent/backup
  schedule) are all disabled by default after an upgrade — identical to a fresh install.

## Rollback instructions

```bash
renewal_cli.py install-rollback --backup-file <archive install-upgrade printed> --yes
```

## Release package

Built via `deploy/build_package.sh` (reproducible — verified byte-identical across two
builds). Contains no secrets, credentials, or database files (automated + manual check).

**SHA256: `def4207414cea11c64f979af7691554244206c786d671c99f636ebdc0e63fefc`**
(`deploy/tixo-renewal-package.tar.gz`, also recorded in `deploy/SHA256SUMS`)
