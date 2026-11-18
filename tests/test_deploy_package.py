"""Regression test: a real bug was found where a newly-added module (credentials.py) was
used by renewal_cli.py but not included in build_package.sh's copy list, so the built
deployable package would crash with ModuleNotFoundError the moment anyone ran it on a real
Virtualizor host. This test actually builds the package and imports it in total isolation
(no implementation/ on sys.path) to catch that exact class of drift continuously, not just
once during manual review.
"""
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.request

import pytest

DEPLOY_DIR = os.path.join(os.path.dirname(__file__), "..", "deploy")


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
def test_build_is_byte_for_byte_reproducible():
    """Release-readiness requirement: building the package twice from the same source must
    produce an IDENTICAL archive, so a published SHA256SUMS actually verifies provenance.
    Catches regressions like tar embedding real file mtimes or directory-read order."""
    result1 = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                              capture_output=True, text=True, timeout=30)
    assert result1.returncode == 0, result1.stderr
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    checksum1 = subprocess.run(["sha256sum", pkg], capture_output=True, text=True).stdout.split()[0]

    time.sleep(1.1)  # ensure a real build-to-build time difference would show up if unfixed
    result2 = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                              capture_output=True, text=True, timeout=30)
    assert result2.returncode == 0, result2.stderr
    checksum2 = subprocess.run(["sha256sum", pkg], capture_output=True, text=True).stdout.split()[0]

    assert checksum1 == checksum2, "two builds from identical source produced different archives"


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
def test_built_package_imports_cleanly_in_total_isolation(tmp_path):
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    check_script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import renewal_cli\n"
        "import writer, local_source, policy, policy_integration, nodes, backup, "
        "alerts, shadow_monitor, engine, models, storage, credentials, "
        "web_auth, audit_log, web_config, web_server, relay_worker, agent_config, "
        "agent_server, backup_config, installer, telegram_config, telegram_transport, "
        "telegram_bot, reports, host_time, security, control_center_api\n"
        "print('OK')\n"
    ) % str(extract_dir / "bin")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir / "lib")
    result = subprocess.run([sys.executable, "-c", check_script], capture_output=True,
                             text=True, timeout=15, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
def test_built_package_cli_end_to_end_staging_run(tmp_path):
    """Real staging acceptance smoke test: runs the ACTUAL built, extracted package's
    renewal_cli.py as a genuine subprocess (not an in-process import) against a disposable
    TIXO_RENEWAL_INSTALL_DIR, exercising install -> install-detect -> status -> dry-run ->
    install-upgrade -> install-health-check exactly as a real operator would from the
    command line. Loopback/local-only; never touches a real Virtualizor host (there is
    none reachable from this environment) or production data."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    install_dir = tmp_path / "staging-install"
    cli_path = str(extract_dir / "bin" / "renewal_cli.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir / "lib")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)

    def run(*args):
        return subprocess.run([sys.executable, cli_path, *args], capture_output=True,
                               text=True, timeout=30, env=env)

    r = run("install")
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.exists(install_dir / "renewal-config" / "nodes.json")
    assert os.path.exists(install_dir / "renewal-config" / "policy.json")

    r = run("install-detect")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"existing": true' in r.stdout

    r = run("status")
    assert r.returncode == 0, r.stdout + r.stderr

    r = run("dry-run")
    assert r.returncode == 0, r.stdout + r.stderr

    r = run("install-upgrade")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Upgrade succeeded" in r.stdout

    r = run("install-health-check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"ok": true' in r.stdout

    # Re-running install (the idempotent noninteractive path) must never duplicate or error.
    r = run("install")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
def test_built_package_web_control_center_real_http_staging_acceptance(tmp_path):
    """Real staging acceptance test: builds the package, installs it to a disposable
    directory, enables the Web Control Center, runs the ACTUAL `web-run` CLI command as a
    genuine subprocess bound to 127.0.0.1 only, and drives it with real HTTP requests
    (urllib, not an in-process call) -- login, all 16 pages, language/theme cookies, and
    the live-network endpoint's honest "not configured" response (never a fabricated
    reading). Confirms the real packaged artifact behaves correctly end to end, not just
    the in-repo implementation/ modules the rest of the suite imports directly."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    install_dir = tmp_path / "staging-install"
    config_dir = str(install_dir / "renewal-config")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir / "lib")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    cli_path = str(extract_dir / "bin" / "renewal_cli.py")

    r = subprocess.run([sys.executable, cli_path, "install"], capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

    # web-run must refuse while disabled -- verified via the real CLI, not an import.
    r = subprocess.run([sys.executable, cli_path, "web-run", "--max-iterations", "1"],
                        capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 1
    assert "BLOCKED" in r.stdout

    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    setup_script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import web_config as wc, web_auth as wa\n"
        "wc.save_web_settings(%r, wc.WebSettings(enabled=True, bind_host='127.0.0.1', bind_port=%d))\n"
        "wa.create_user(%r, 'admin', 'stagingpass123', role=wa.ROLE_ADMIN)\n"
    ) % (str(extract_dir / "lib"), config_dir, free_port, config_dir)
    r = subprocess.run([sys.executable, "-c", setup_script], capture_output=True, text=True,
                        timeout=10, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

    proc = subprocess.Popen([sys.executable, cli_path, "web-run", "--max-iterations", "30"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        base = f"http://127.0.0.1:{free_port}"
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{base}/login", timeout=1)
                break
            except Exception as e:
                last_error = e
                time.sleep(0.2)
        else:
            raise AssertionError(f"server never came up: {last_error}")

        # Login returns a 302 -- use http.client directly rather than urllib.request
        # (which auto-follows redirects and would discard the original Set-Cookie header).
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", free_port, timeout=5)
        login_body = "username=admin&password=stagingpass123"
        conn.request("POST", "/login", body=login_body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        resp.read()
        cookie_header = resp.getheader("Set-Cookie")
        conn.close()
        assert cookie_header, "login did not set a session cookie"
        session_cookie = cookie_header.split(";")[0]

        pages = ["/", "/host", "/network", "/traffic", "/inventory", "/renewals",
                 "/emergency", "/review", "/alerts", "/reports", "/telegram", "/backups",
                 "/settings", "/users", "/audit", "/health"]
        for page in pages:
            req = urllib.request.Request(f"{base}{page}", headers={"Cookie": session_cookie})
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200, f"{page} returned {resp.status}"

        # Localization/theme: real cookie-driven rendering, not a hardcoded default.
        req = urllib.request.Request(f"{base}/", headers={"Cookie": f"{session_cookie}; tixo_lang=fa"})
        body = urllib.request.urlopen(req, timeout=5).read().decode()
        assert 'dir="rtl"' in body

        req = urllib.request.Request(f"{base}/", headers={"Cookie": f"{session_cookie}; tixo_theme=light"})
        body = urllib.request.urlopen(req, timeout=5).read().decode()
        assert 'data-theme="light"' in body

        # Live network endpoint must honestly report "not configured" -- never a fabricated
        # RX/TX reading when no interface has been selected.
        req = urllib.request.Request(f"{base}/api/live", headers={"Cookie": session_cookie})
        live = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        assert live.get("configured") is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
def test_built_package_optional_module_cli_registration_and_safe_defaults(tmp_path):
    """Staging acceptance: every optional-module `*-run` CLI command is genuinely
    registered in the real packaged argparse tree AND refuses to start while disabled --
    exercised as real subprocesses against the actual built package, not an in-process
    argparse inspection."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    install_dir = tmp_path / "staging-install"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir / "lib")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    cli_path = str(extract_dir / "bin" / "renewal_cli.py")

    r = subprocess.run([sys.executable, cli_path, "install"], capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr

    r = subprocess.run([sys.executable, cli_path, "--help"], capture_output=True,
                        text=True, timeout=10, env=env)
    assert r.returncode == 0
    for subcommand in ("telegram-bot-run", "web-run", "relay-worker-run", "agent-run",
                        "backup-scheduled-run"):
        assert subcommand in r.stdout, f"{subcommand} not registered in --help output"

    for subcommand, max_iter_flag in (
        ("telegram-bot-run", "--max-iterations"),
        ("web-run", "--max-iterations"),
        ("relay-worker-run", "--max-iterations"),
        ("agent-run", "--max-iterations"),
    ):
        r = subprocess.run([sys.executable, cli_path, subcommand, max_iter_flag, "1"],
                            capture_output=True, text=True, timeout=10, env=env)
        assert r.returncode == 1, f"{subcommand} should refuse to start while disabled"
        assert "BLOCKED" in r.stdout, f"{subcommand} did not print a BLOCKED message"

    r = subprocess.run([sys.executable, cli_path, "backup-scheduled-run"],
                        capture_output=True, text=True, timeout=10, env=env)
    assert r.returncode == 1
    assert "BLOCKED" in r.stdout


def test_every_implementation_module_imported_by_renewal_cli_is_in_build_list():
    """Belt-and-suspenders: statically cross-check build_package.sh's file list against
    every top-level `import X` / `from X import` in renewal_cli.py that resolves to a local
    implementation/ module, so a future new module can't be forgotten again."""
    impl_dir = os.path.join(os.path.dirname(__file__), "..", "implementation")
    local_modules = {f[:-3] for f in os.listdir(impl_dir) if f.endswith(".py")}

    build_script = open(os.path.join(DEPLOY_DIR, "build_package.sh")).read()

    cli_source = open(os.path.join(impl_dir, "renewal_cli.py")).read()
    import re
    imported = set()
    for m in re.finditer(r"^\s*import (\w+)|^\s*from (\w+) import", cli_source, re.MULTILINE):
        name = m.group(1) or m.group(2)
        if name in local_modules:
            imported.add(name)

    missing = [f"{name}.py" for name in imported if f"{name}.py" not in build_script]
    assert not missing, f"modules imported by renewal_cli.py but missing from build_package.sh: {missing}"


def test_bootstrap_default_tag_matches_current_version():
    """Regression test: bootstrap.sh's default pinned TAG has twice been left pointing at the
    PREVIOUS release after a version bump, which silently installs stale code even when a
    user fetches bootstrap.sh from the new tag's own URL (see v1.2.0 and v1.3.0 release
    history). The default tag must always match renewal_cli.VERSION."""
    impl_dir = os.path.join(os.path.dirname(__file__), "..", "implementation")
    sys.path.insert(0, impl_dir)
    import renewal_cli

    bootstrap_text = open(os.path.join(DEPLOY_DIR, "bootstrap.sh")).read()
    import re
    m = re.search(r'TAG="\$\{TIXO_RENEWAL_VERSION:-v([0-9.]+)\}"', bootstrap_text)
    assert m, "could not find bootstrap.sh's default TAG pattern"
    assert m.group(1) == renewal_cli.VERSION, (
        f"bootstrap.sh defaults to v{m.group(1)} but renewal_cli.VERSION is "
        f"{renewal_cli.VERSION} -- bump bootstrap.sh's default TAG to match"
    )
