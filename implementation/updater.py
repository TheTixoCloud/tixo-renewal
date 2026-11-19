"""updater.py (v1.10.2) -- the real in-app "Check for updates" -> apply mechanism.

Builds entirely on installer.py's existing backup/upgrade/health-check primitives (never a
second, independent backup/restore implementation) plus a GitHub-release download/verify
layer specific to this module. menu.check_for_update() already does the read-only
version-check (GitHub API, semantic comparison) -- this module is what actually DOWNLOADS,
VERIFIES, and APPLIES a selected release once the operator has explicitly confirmed.

SAFETY, EXPLICIT:
  - Never triggers automatically. Every function here is a step a caller (CLI subcommand /
    menu action) invokes only after displaying current vs. available version + release
    notes and getting an explicit operator confirmation.
  - Never touches Virtualizor, customer VPS state, renewal policy, or systemd unit
    installation/enablement -- this module only ever updates THIS application's own code
    (lib/, bin/, lib/static/) and re-runs the existing idempotent config/schema step.
  - A full raw filesystem safety backup (includes secrets -- the only backup format here
    that can restore EVERYTHING, unlike backup.create_full_backup_archive()'s deliberately
    secret-excluding scope) is taken before any file is touched, and the OLD lib/+bin/
    directories are moved aside (never deleted) until the new ones pass a health check --
    so a failed update can always be reverted without re-downloading anything.
  - Checksum verification happens BEFORE extraction, against the SAME release's own
    published SHA256SUMS -- never trusts the tarball alone.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request

GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/TheTixoCloud/tixo-renewal/releases/latest"
GITHUB_API_RELEASE_BY_TAG = "https://api.github.com/repos/TheTixoCloud/tixo-renewal/releases/tags/{tag}"
ASSET_NAME = "tixo-renewal-package.tar.gz"
CHECKSUMS_NAME = "SHA256SUMS"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB -- generous; this package is ~1-2 MB today.


def fetch_release_info(tag: str = None, timeout: float = 10.0) -> dict:
    """Read-only GitHub API call. `tag=None` fetches the latest release; a specific tag
    fetches exactly that release (used right before applying it, so the asset URLs are
    fresh even if a newer release appeared since the operator was first shown this one).
    Returns {"ok", "tag", "version", "notes", "assets": {name: url}, "error"} -- never
    raises."""
    import json
    url = GITHUB_API_LATEST_RELEASE if tag is None else GITHUB_API_RELEASE_BY_TAG.format(tag=tag)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
        rel_tag = data.get("tag_name", "")
        return {"ok": True, "tag": rel_tag,
                "version": rel_tag[1:] if rel_tag.startswith("v") else rel_tag,
                "notes": data.get("body", ""), "assets": assets, "error": None}
    except Exception as e:  # noqa: BLE001 -- network/JSON errors must never crash the caller
        return {"ok": False, "tag": None, "version": None, "notes": None, "assets": {},
                "error": str(e)}


def download_file(url: str, dest_path: str, timeout: float = 60.0,
                   max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
    """Streams a download to disk in bounded chunks -- refuses (raises ValueError) if the
    response exceeds `max_bytes`, so a compromised/misbehaving server can never exhaust
    local disk via this path. Raises on any network error; caller decides how to report it."""
    req = urllib.request.Request(url, headers={"User-Agent": "tixo-renewal-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"download of {url} exceeded {max_bytes} bytes -- aborted")
                f.write(chunk)


def verify_checksum(tarball_path: str, checksums_path: str, asset_name: str = ASSET_NAME) -> bool:
    """Parses a SHA256SUMS file (`<hex>  <filename>` lines, as produced by `sha256sum`) and
    confirms tarball_path's REAL computed sha256 matches the line for `asset_name`. Returns
    False (never raises) for a missing entry, unreadable file, or mismatch -- the caller
    always treats False as "do not proceed"."""
    try:
        want = None
        with open(checksums_path, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == asset_name:
                    want = parts[0].lower()
                    break
        if not want:
            return False
        h = hashlib.sha256()
        with open(tarball_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest().lower() == want
    except OSError:
        return False


def create_full_filesystem_backup(install_dir: str, backup_root: str, label: str) -> str:
    """A raw tar.gz of the ENTIRE install directory (code + config + state + secrets) --
    deliberately broader scope than backup.create_full_backup_archive() (which excludes
    secrets by design for its own, different use case: safe-to-share operational backups).
    This is the one backup format that can restore EVERYTHING an update might touch,
    including files `upgrade()`'s own automatic backup never covers. Stored OUTSIDE
    install_dir (in backup_root) so it survives even if install_dir is later wiped."""
    from datetime import datetime, timezone
    os.makedirs(backup_root, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = os.path.join(backup_root, f"{ts}-{label}-full-filesystem.tar.gz")
    parent = os.path.dirname(os.path.abspath(install_dir.rstrip("/")))
    name = os.path.basename(os.path.abspath(install_dir.rstrip("/")))
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(os.path.join(parent, name), arcname=name)
    return archive_path


def _copy_tree_contents(src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s, d = os.path.join(src, entry), os.path.join(dst, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def apply_update(install_dir: str, config_dir: str, state_dir: str, backup_root: str,
                  tag: str, download_fn=None, fetch_fn=None,
                  run_install_upgrade_fn=None, health_check_fn=None) -> dict:
    """Downloads, verifies, and applies release `tag` to `install_dir`. Every step that can
    fail does so WITHOUT destroying the previous working install:

      1. Re-fetch `tag`'s own release info (fresh asset URLs).
      2. Download the tarball + SHA256SUMS to a temp dir; verify checksum BEFORE touching
         anything in install_dir. Abort (ok=False) on any failure here -- install_dir is
         never touched.
      3. Take a full raw filesystem safety backup of install_dir (see
         create_full_filesystem_backup) -- this exists independently of steps 4-7 below, so
         even a catastrophic failure in this function still leaves a real, tested-format
         recovery path.
      4. Move the CURRENT lib/ and bin/ aside (renamed with a `.pre-update` suffix, never
         deleted) -- so "rollback" in step 7 is a simple directory swap, no re-download,
         no re-extraction.
      5. Copy the new package's lib/, bin/, and lib/static/ (if present) into install_dir.
      6. Run the idempotent config/schema step (installer.upgrade(), via
         run_install_upgrade_fn -- injectable for tests) to refresh templates/schema
         without touching existing policy/nodes/secrets.
      7. Run a health check (health_check_fn, injectable for tests). On failure: delete the
         new lib/+bin/, restore the `.pre-update` ones, return ok=False with full detail --
         the install directory ends up EXACTLY as it was before this function ran, modulo
         the full backup now sitting in backup_root.
      8. On success: remove the `.pre-update` directories, return ok=True.

    Never raises for an expected failure mode (network error, bad checksum, failed health
    check) -- always returns a report dict. Never calls `apply`/`apply-due`, never modifies
    `policy.json`/`nodes.json`, never touches systemd unit installation/enablement."""
    report = {"ok": False, "tag": tag, "stage": None, "error": None,
              "backup_path": None, "rolled_back": False}

    if fetch_fn is None:
        fetch_fn = fetch_release_info
    if download_fn is None:
        download_fn = download_file

    info = fetch_fn(tag=tag)
    if not info["ok"]:
        report["stage"] = "fetch_release_info"
        report["error"] = info["error"]
        return report
    if ASSET_NAME not in info["assets"] or CHECKSUMS_NAME not in info["assets"]:
        report["stage"] = "fetch_release_info"
        report["error"] = f"release {tag} is missing expected assets ({ASSET_NAME}/{CHECKSUMS_NAME})"
        return report

    with tempfile.TemporaryDirectory(prefix="tixo-update-") as tmp:
        tarball_path = os.path.join(tmp, ASSET_NAME)
        checksums_path = os.path.join(tmp, CHECKSUMS_NAME)
        try:
            download_fn(info["assets"][ASSET_NAME], tarball_path)
            download_fn(info["assets"][CHECKSUMS_NAME], checksums_path)
        except Exception as e:  # noqa: BLE001
            report["stage"] = "download"
            report["error"] = str(e)
            return report

        if not verify_checksum(tarball_path, checksums_path):
            report["stage"] = "verify_checksum"
            report["error"] = "downloaded package checksum did not match published SHA256SUMS"
            return report

        # Everything above is purely read/network -- install_dir has not been touched yet.
        try:
            report["backup_path"] = create_full_filesystem_backup(
                install_dir, backup_root, label=f"pre-update-to-{tag}")
        except Exception as e:  # noqa: BLE001
            report["stage"] = "backup"
            report["error"] = str(e)
            return report

        extract_dir = os.path.join(tmp, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(tarball_path) as tf:
            tf.extractall(extract_dir, filter="data")

        lib_dir = os.path.join(install_dir, "lib")
        bin_dir = os.path.join(install_dir, "bin")
        lib_backup = lib_dir + ".pre-update"
        bin_backup = bin_dir + ".pre-update"
        for p in (lib_backup, bin_backup):
            if os.path.exists(p):
                shutil.rmtree(p)

        try:
            if os.path.isdir(lib_dir):
                shutil.move(lib_dir, lib_backup)
            if os.path.isdir(bin_dir):
                shutil.move(bin_dir, bin_backup)

            _copy_tree_contents(os.path.join(extract_dir, "lib"), lib_dir)
            _copy_tree_contents(os.path.join(extract_dir, "bin"), bin_dir)
            for exe in os.listdir(bin_dir):
                os.chmod(os.path.join(bin_dir, exe), 0o755)

            if run_install_upgrade_fn is None:
                import installer as inst
                run_install_upgrade_fn = lambda: inst.upgrade(config_dir, state_dir, backup_root)
            upgrade_result = run_install_upgrade_fn()
            if not upgrade_result.get("ok", False):
                raise RuntimeError(f"post-update install step failed: {upgrade_result}")

            if health_check_fn is None:
                import installer as inst
                health_check_fn = lambda: inst.health_check(config_dir, state_dir)
            health = health_check_fn()
            if not health.get("ok", False):
                raise RuntimeError(f"post-update health check failed: {health}")

        except Exception as e:  # noqa: BLE001 -- ANY failure here triggers rollback
            report["stage"] = "apply_or_verify"
            report["error"] = str(e)
            if os.path.isdir(lib_dir):
                shutil.rmtree(lib_dir)
            if os.path.isdir(bin_dir):
                shutil.rmtree(bin_dir)
            if os.path.isdir(lib_backup):
                shutil.move(lib_backup, lib_dir)
            if os.path.isdir(bin_backup):
                shutil.move(bin_backup, bin_dir)
            report["rolled_back"] = True
            return report

        # Success -- the moved-aside old code is no longer needed.
        if os.path.isdir(lib_backup):
            shutil.rmtree(lib_backup)
        if os.path.isdir(bin_backup):
            shutil.rmtree(bin_backup)

        report["ok"] = True
        report["stage"] = "complete"
        return report
