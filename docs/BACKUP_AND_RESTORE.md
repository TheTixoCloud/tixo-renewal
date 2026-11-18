# Backup and Restore Guide

Backup automation is **disabled by default**. Nothing is scheduled, encrypted, retained, or
delivered anywhere unless explicitly enabled.

**Verification status:** archive creation, integrity verification, retention, GPG
encryption/decryption, staging-only restore, and Telegram delivery logic are all verified
with real local execution (real `gpg` subprocess calls, real tar archives, real SQLite
integrity checks). Restoration has only been exercised against small synthetic databases,
never a real multi-gigabyte production dataset.

## Create a backup manually

```bash
renewal_cli.py backup-full                                  # unencrypted
renewal_cli.py backup-full --encrypt                         # requires TIXO_BACKUP_PASSPHRASE env var
renewal_cli.py backup-full --keep 7                           # apply retention after creating
```

The archive bundles a consistent `Connection.backup()` snapshot of every known state
database plus an explicit **ALLOWLIST** of non-secret config files. `credentials/`,
`telegram/secrets.json`, `web/users.json`, `web/csrf_secret.key`, and anything not
explicitly allowlisted are **never** read or included — verified by a dedicated test that
inspects the produced tarball's contents directly.

## Verify archive integrity

Done automatically by every creation path (`backup-full`, `backup-scheduled-run`, the Web
`/backups` page) and printed in the output: tar readability + `PRAGMA integrity_check` on
every bundled database.

## Restore into an isolated path

```python
import backup
result = backup.restore_full_backup_archive("<archive>.tar.gz", "/path/to/staging/dir")
# result["databases"], result["config"], result["staging_dir"]
```

Extracts to the staging directory **only** — never overwrites live state/config. Refuses
(raises `ValueError`) if the archive fails integrity verification. An operator reviews the
staged files and copies them into place deliberately — this is intentional friction, not a
missing feature.

For a full-install rollback (not just one database), use
`renewal_cli.py install-rollback --backup-file <archive> --yes` (see `INSTALL_GUIDE.md`) —
this one DOES apply the restore (after safety-copying the current state first), because
rollback is explicitly meant to undo a failed change.

## Scheduled backups

```
menu: "Configure Backup Schedule (optional)"
```
Sets interval (hours), retention (keep last N), whether to encrypt, and whether to deliver
to Telegram (requires encryption — rejected otherwise at the settings level, not just by
convention). An unattended timer needs its GPG passphrase available without an interactive
prompt, so it is stored at rest with the same `0600`-enforced discipline as every other
secret — this is an explicit, documented tradeoff (root-equivalent access either way).

```bash
renewal_cli.py backup-scheduled-run    # the timer's actual entry point
```
Refuses unless enabled. The generated `virt-renewal-backup.service`/`.timer` pair is
`Type=oneshot` + a periodic timer — generated at install time, never installed/enabled
automatically.

## Encrypted-only Telegram delivery

`backup.send_backup_to_telegram()`:
- **Refuses a non-`.gpg` archive by default** — never sends a plaintext backup.
- Enforces a size cap (Telegram's real 50MB bot-upload limit by default) **before** reading
  the file into memory.
- One admin's delivery failure never blocks delivery to the others.
- Only works over **DIRECT** Telegram connectivity mode — SOCKS5 and RELAY both raise a
  clear `NotImplementedError` rather than attempting (and possibly mishandling) a multipart
  upload over those transports.

## Retention

`backup.apply_retention(backup_root, keep_count)` deletes all but the N most recent
archives (filenames sort chronologically by their timestamp prefix).
