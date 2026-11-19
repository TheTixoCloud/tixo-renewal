"""Tests for updater.py (v1.10.2): the real in-app update download/verify/apply/rollback
mechanism. Network calls are always injected fakes here -- no real GitHub access needed to
run this suite, but the download/checksum/extract/rollback LOGIC is exercised for real
against real tarball fixtures (no mocking of the file-level operations)."""
import hashlib
import os
import shutil
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import updater


# ---------- fetch_release_info ------------------------------------------------------------------

def test_fetch_release_info_success(monkeypatch):
    import json
    import io

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({
                "tag_name": "v9.9.9",
                "body": "release notes here",
                "assets": [
                    {"name": "tixo-renewal-package.tar.gz", "browser_download_url": "https://x/pkg.tar.gz"},
                    {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"},
                ],
            }).encode()

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout: FakeResp())
    result = updater.fetch_release_info()
    assert result["ok"] is True
    assert result["tag"] == "v9.9.9"
    assert result["version"] == "9.9.9"
    assert result["notes"] == "release notes here"
    assert "tixo-renewal-package.tar.gz" in result["assets"]


def test_fetch_release_info_network_failure_never_raises(monkeypatch):
    def boom(req, timeout):
        raise OSError("network unreachable")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    result = updater.fetch_release_info()
    assert result["ok"] is False
    assert "network unreachable" in result["error"]


# ---------- download_file ------------------------------------------------------------------------

def test_download_file_writes_content(tmp_path, monkeypatch):
    class FakeResp:
        def __init__(self):
            self._data = [b"hello ", b"world"]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, n=None):
            return self._data.pop(0) if self._data else b""

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout: FakeResp())
    dest = str(tmp_path / "out.bin")
    updater.download_file("https://x/y", dest)
    assert open(dest, "rb").read() == b"hello world"


def test_download_file_refuses_oversized_response(tmp_path, monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, n=None):
            return b"x" * 1000

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout: FakeResp())
    dest = str(tmp_path / "out.bin")
    with pytest.raises(ValueError, match="exceeded"):
        updater.download_file("https://x/y", dest, max_bytes=500)


# ---------- verify_checksum -----------------------------------------------------------------------

def test_verify_checksum_matches(tmp_path):
    content = b"fake package bytes"
    tarball = tmp_path / "pkg.tar.gz"
    tarball.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  tixo-renewal-package.tar.gz\n")
    assert updater.verify_checksum(str(tarball), str(sums)) is True


def test_verify_checksum_mismatch(tmp_path):
    tarball = tmp_path / "pkg.tar.gz"
    tarball.write_bytes(b"real content")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("0" * 64 + "  tixo-renewal-package.tar.gz\n")
    assert updater.verify_checksum(str(tarball), str(sums)) is False


def test_verify_checksum_missing_entry(tmp_path):
    tarball = tmp_path / "pkg.tar.gz"
    tarball.write_bytes(b"content")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("aabbcc  some-other-file.tar.gz\n")
    assert updater.verify_checksum(str(tarball), str(sums)) is False


# ---------- apply_update: full fixture-driven integration ----------------------------------------

def _make_fake_package(tmp_path, version: str, extra_file_content: str = "new") -> str:
    """Builds a real tar.gz shaped like the real deploy/build_package.sh output -- lib/,
    bin/, lib/static/ -- with a renewal_cli.py reporting the given version."""
    src = tmp_path / f"pkg-src-{version}"
    (src / "lib" / "static").mkdir(parents=True)
    (src / "bin").mkdir(parents=True)
    (src / "lib" / "renewal_cli.py").write_text(f'VERSION = "{version}"\n')
    (src / "lib" / "some_module.py").write_text(f"MARKER = {extra_file_content!r}\n")
    (src / "lib" / "static" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    (src / "bin" / "renewal_cli.py").write_text(
        f'import sys, os\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))\n'
        f'from renewal_cli import VERSION\nif __name__ == "__main__": print(VERSION)\n'
    )
    tarball = tmp_path / f"tixo-renewal-package-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src / "lib", arcname="lib")
        tf.add(src / "bin", arcname="bin")
    return str(tarball)


def _make_checksums(tmp_path, tarball_path: str, asset_name: str) -> str:
    digest = hashlib.sha256(open(tarball_path, "rb").read()).hexdigest()
    sums_path = tmp_path / f"SHA256SUMS-{asset_name}"
    sums_path.write_text(f"{digest}  {asset_name}\n")
    return str(sums_path)


def _make_install_dir(tmp_path, old_version: str) -> str:
    """A realistic install_dir: lib/, bin/, renewal-config/ with a policy file that must
    survive the update untouched, shadow-monitor-state/ with a 'database' that must also
    survive."""
    install_dir = tmp_path / "opt-tixo-renewal"
    (install_dir / "lib").mkdir(parents=True)
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "renewal-config").mkdir(parents=True)
    (install_dir / "shadow-monitor-state").mkdir(parents=True)
    (install_dir / "lib" / "renewal_cli.py").write_text(f'VERSION = "{old_version}"\n')
    (install_dir / "bin" / "renewal_cli.py").write_text("# old stub\n")
    (install_dir / "renewal-config" / "policy.json").write_text('{"monthly_grant_gb": "100"}')
    (install_dir / "renewal-config" / "secrets.json").write_text('{"fake": "secret-placeholder"}')
    (install_dir / "shadow-monitor-state" / "self.sqlite3").write_bytes(b"fake-sqlite-bytes")
    return str(install_dir)


def _real_download_fn(fake_files: dict):
    def _dl(url, dest_path, timeout=60.0, max_bytes=None):
        shutil.copy2(fake_files[url], dest_path)
    return _dl


def test_apply_update_success_preserves_config_and_state(tmp_path):
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    new_tarball = _make_fake_package(tmp_path, "1.10.2")
    new_sums = _make_checksums(tmp_path, new_tarball, updater.ASSET_NAME)
    fake_files = {"https://x/pkg.tar.gz": new_tarball, "https://x/sums": new_sums}

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=_real_download_fn(fake_files), fetch_fn=fetch_fn,
        run_install_upgrade_fn=lambda: {"ok": True}, health_check_fn=lambda: {"ok": True},
    )

    assert result["ok"] is True, result
    assert result["stage"] == "complete"
    assert result["backup_path"] and os.path.exists(result["backup_path"])

    # New code applied.
    new_cli = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.2"' in new_cli
    assert os.path.exists(os.path.join(install_dir, "lib", "static", "logo.png"))

    # Config/secrets/state UNTOUCHED.
    assert open(os.path.join(config_dir, "policy.json")).read() == '{"monthly_grant_gb": "100"}'
    assert open(os.path.join(config_dir, "secrets.json")).read() == '{"fake": "secret-placeholder"}'
    assert open(os.path.join(state_dir, "self.sqlite3"), "rb").read() == b"fake-sqlite-bytes"

    # No leftover .pre-update directories after a successful update.
    assert not os.path.isdir(os.path.join(install_dir, "lib.pre-update"))
    assert not os.path.isdir(os.path.join(install_dir, "bin.pre-update"))


def test_apply_update_bad_checksum_never_touches_install_dir(tmp_path):
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    new_tarball = _make_fake_package(tmp_path, "1.10.2")
    bad_sums = tmp_path / "BAD_SUMS"
    bad_sums.write_text("0" * 64 + f"  {updater.ASSET_NAME}\n")
    fake_files = {"https://x/pkg.tar.gz": new_tarball, "https://x/sums": str(bad_sums)}

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=_real_download_fn(fake_files), fetch_fn=fetch_fn,
    )

    assert result["ok"] is False
    assert result["stage"] == "verify_checksum"
    assert result["backup_path"] is None  # never even reached the backup step
    # Old code untouched.
    old_cli = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.1"' in old_cli


def test_apply_update_failed_health_check_rolls_back_cleanly(tmp_path):
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    new_tarball = _make_fake_package(tmp_path, "1.10.2")
    new_sums = _make_checksums(tmp_path, new_tarball, updater.ASSET_NAME)
    fake_files = {"https://x/pkg.tar.gz": new_tarball, "https://x/sums": new_sums}

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=_real_download_fn(fake_files), fetch_fn=fetch_fn,
        run_install_upgrade_fn=lambda: {"ok": True},
        health_check_fn=lambda: {"ok": False, "problems": ["simulated failure"]},
    )

    assert result["ok"] is False
    assert result["stage"] == "apply_or_verify"
    assert result["rolled_back"] is True
    assert "simulated failure" in result["error"]

    # Rolled back to the OLD version -- not left on the new (unhealthy) one.
    cli_text = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.1"' in cli_text
    assert not os.path.isdir(os.path.join(install_dir, "lib.pre-update"))
    assert not os.path.isdir(os.path.join(install_dir, "bin.pre-update"))

    # A real backup was still taken (defense in depth) even though apply failed.
    assert result["backup_path"] and os.path.exists(result["backup_path"])

    # Config/secrets/state still untouched despite the failed update.
    assert open(os.path.join(config_dir, "policy.json")).read() == '{"monthly_grant_gb": "100"}'
    assert open(os.path.join(state_dir, "self.sqlite3"), "rb").read() == b"fake-sqlite-bytes"


def test_apply_update_rejects_release_missing_expected_assets(tmp_path):
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "", "assets": {},
                "error": None}

    result = updater.apply_update(install_dir, config_dir, state_dir, backup_root,
                                   tag="v1.10.2", fetch_fn=fetch_fn)
    assert result["ok"] is False
    assert result["stage"] == "fetch_release_info"
    assert "missing expected assets" in result["error"]


def test_apply_update_interrupted_download_mid_transfer_never_touches_install_dir(tmp_path):
    """Simulates a connection drop partway through the tarball download: download_fn raises
    AFTER having already written SOME bytes to tarball_path. install_dir must remain fully
    untouched (download happens into a tempdir, before any backup or move)."""
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    calls = {"n": 0}

    def flaky_download(url, dest_path, timeout=60.0, max_bytes=None):
        calls["n"] += 1
        with open(dest_path, "wb") as f:
            f.write(b"partial-bytes-before-the-connection-dropped")
        raise ConnectionResetError("connection reset by peer")

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=flaky_download, fetch_fn=fetch_fn,
    )

    assert result["ok"] is False
    assert result["stage"] == "download"
    assert "connection reset" in result["error"]
    assert result["backup_path"] is None  # never reached the backup step
    assert calls["n"] == 1  # failed on the first asset; never attempted the second

    # install_dir completely untouched -- old code, config, and state all intact.
    old_cli = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.1"' in old_cli
    assert open(os.path.join(config_dir, "policy.json")).read() == '{"monthly_grant_gb": "100"}'
    assert open(os.path.join(state_dir, "self.sqlite3"), "rb").read() == b"fake-sqlite-bytes"


def test_apply_update_missing_dependency_during_post_update_install_rolls_back(tmp_path):
    """run_install_upgrade_fn raising ModuleNotFoundError (e.g. a required stdlib-adjacent
    module missing on this host's Python) must be treated exactly like any other
    apply_or_verify failure: full rollback, old version restored, error surfaced."""
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    new_tarball = _make_fake_package(tmp_path, "1.10.2")
    new_sums = _make_checksums(tmp_path, new_tarball, updater.ASSET_NAME)
    fake_files = {"https://x/pkg.tar.gz": new_tarball, "https://x/sums": new_sums}

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    def missing_dependency(*a, **k):
        raise ModuleNotFoundError("No module named 'sqlite3'")

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=_real_download_fn(fake_files), fetch_fn=fetch_fn,
        run_install_upgrade_fn=missing_dependency,
    )

    assert result["ok"] is False
    assert result["stage"] == "apply_or_verify"
    assert result["rolled_back"] is True
    assert "sqlite3" in result["error"]

    cli_text = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.1"' in cli_text
    assert not os.path.isdir(os.path.join(install_dir, "lib.pre-update"))
    assert not os.path.isdir(os.path.join(install_dir, "bin.pre-update"))
    assert open(os.path.join(config_dir, "policy.json")).read() == '{"monthly_grant_gb": "100"}'


def test_apply_update_import_error_in_health_check_rolls_back(tmp_path):
    """health_check_fn raising ImportError (e.g. the newly-copied lib/ has a broken import --
    a bad release artifact) must also be caught as a generic apply_or_verify failure and
    trigger the same clean rollback -- it must not propagate out of apply_update."""
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    config_dir = os.path.join(install_dir, "renewal-config")
    state_dir = os.path.join(install_dir, "shadow-monitor-state")
    backup_root = str(tmp_path / "backups")

    new_tarball = _make_fake_package(tmp_path, "1.10.2")
    new_sums = _make_checksums(tmp_path, new_tarball, updater.ASSET_NAME)
    fake_files = {"https://x/pkg.tar.gz": new_tarball, "https://x/sums": new_sums}

    def fetch_fn(tag=None):
        return {"ok": True, "tag": "v1.10.2", "version": "1.10.2", "notes": "",
                "assets": {updater.ASSET_NAME: "https://x/pkg.tar.gz",
                           updater.CHECKSUMS_NAME: "https://x/sums"},
                "error": None}

    def broken_health_check(*a, **k):
        raise ImportError("cannot import name 'quota_engine' from 'renewal_cli'")

    result = updater.apply_update(
        install_dir, config_dir, state_dir, backup_root, tag="v1.10.2",
        download_fn=_real_download_fn(fake_files), fetch_fn=fetch_fn,
        run_install_upgrade_fn=lambda: {"ok": True},
        health_check_fn=broken_health_check,
    )

    assert result["ok"] is False
    assert result["stage"] == "apply_or_verify"
    assert result["rolled_back"] is True
    assert "quota_engine" in result["error"]

    cli_text = open(os.path.join(install_dir, "lib", "renewal_cli.py")).read()
    assert '"1.10.1"' in cli_text
    assert not os.path.isdir(os.path.join(install_dir, "lib.pre-update"))
    assert not os.path.isdir(os.path.join(install_dir, "bin.pre-update"))
    assert result["backup_path"] and os.path.exists(result["backup_path"])


def test_full_filesystem_backup_includes_secrets_unlike_the_other_backup_format(tmp_path):
    install_dir = _make_install_dir(tmp_path, old_version="1.10.1")
    backup_root = str(tmp_path / "backups")
    path = updater.create_full_filesystem_backup(install_dir, backup_root, label="test")
    assert os.path.exists(path)
    with tarfile.open(path) as tf:
        names = tf.getnames()
    assert any("secrets.json" in n for n in names)
    assert any("self.sqlite3" in n for n in names)
