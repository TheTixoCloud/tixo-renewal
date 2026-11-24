"""Regression test: a real bug was found where a newly-added module (credentials.py) was
used by renewal_cli.py but not included in build_package.sh's copy list, so the built
deployable package would crash with ModuleNotFoundError the moment anyone ran it on a real
Virtualizor host. This test actually builds the package and imports it in total isolation
(no implementation/ on sys.path) to catch that exact class of drift continuously, not just
once during manual review.
"""
import json
import os
import shutil
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

    # Mirrors the REAL install.sh layout: code (lib/+bin/) and data (config/state) live
    # together under ONE install_dir -- install.sh's own `cp -f "$SCRIPT_DIR"/lib/*.py
    # "$INSTALL_DIR/lib/"` (+ bin/) does this before ever invoking `renewal_cli.py install`.
    # A prior version of this test ran renewal_cli.py straight out of extract_dir with
    # TIXO_RENEWAL_INSTALL_DIR pointed at a SEPARATE, code-less directory -- a layout real
    # production never produces, and one the global wrapper (which assumes install_dir/lib
    # + install_dir/bin exist, exactly like install.sh's own layout) can never work with.
    # That mismatch went unnoticed because nothing actually verified the wrapper worked;
    # installer.verify_wrapper_dispatch() (v1.10.9) now does, and correctly flagged it.
    install_dir = tmp_path / "staging-install"
    shutil.copytree(extract_dir / "lib", install_dir / "lib")
    shutil.copytree(extract_dir / "bin", install_dir / "bin")
    cli_path = str(install_dir / "bin" / "renewal_cli.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir / "lib") + ":" + str(install_dir / "bin")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_WRAPPER_PATH"] = str(tmp_path / "fake-usr-local-bin" / "tixo-renewal")
    os.makedirs(tmp_path / "fake-usr-local-bin", exist_ok=True)

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

    # install-health-check (v1.10.3+) genuinely checks real systemd state for this
    # project's own core timers -- before activation, that's correctly UNHEALTHY (none
    # installed yet), not a false "ok: true". See
    # test_install_activate_core_timers_real_systemd_round_trip below for the full
    # activate -> verify -> deactivate cycle against this same real host's systemd.
    r = run("install-health-check")
    assert r.returncode == 1, r.stdout + r.stderr
    assert '"ok": false' in r.stdout
    assert "is not enabled" in r.stdout

    # Re-running install (the idempotent noninteractive path) must never duplicate or error.
    r = run("install")
    assert r.returncode == 0, r.stdout + r.stderr


def _real_systemd_usable() -> bool:
    if os.geteuid() != 0:
        return False
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(["systemctl", "daemon-reload"], capture_output=True,
                                 text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
@pytest.mark.skipif(not _real_systemd_usable(),
                     reason="requires root + a genuinely functional systemd on this host")
def test_install_activate_core_timers_real_systemd_round_trip(tmp_path):
    """The one test in this suite that genuinely touches the real host's systemd --
    deliberately, since `install-activate-core-timers` is exactly the automation that
    replaces the error-prone manual `systemctl`/file-copy steps `ir2`'s real installation
    required (see service_activation.py's module docstring). Installs the real built
    package's real core timers into THIS test-running host's real /etc/systemd/system,
    verifies they are genuinely enabled+active via `systemctl is-enabled`/`is-active` (not
    a mock), then ALWAYS removes them again in a finally block, regardless of whether an
    assertion above it failed -- this test must never leave residue on the host it runs on.
    Skipped entirely unless running as root with a real, responsive systemd (most CI
    containers lack a real PID 1 systemd; this sandbox genuinely has one)."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    install_dir = tmp_path / "staging-install-real-systemd"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_MENU"] = "1"

    # Run the REAL install.sh, not a direct `renewal_cli.py install` call -- that script is
    # what actually copies lib/bin into install_dir (cmd_install() alone never does), which
    # is what makes install_dir/lib a real PYTHONPATH the generated unit files can resolve
    # against. A direct-subcommand-only test setup (as used by the lighter smoke test above,
    # which never activates any real unit) would silently generate unit files pointing at a
    # lib/ directory that was never populated.
    install_result = subprocess.run(["bash", "install.sh"], cwd=extract_dir,
                                      capture_output=True, text=True, timeout=60, env=env)
    assert install_result.returncode == 0, install_result.stdout + install_result.stderr

    cli_path = str(install_dir / "bin" / "renewal_cli.py")
    env["PYTHONPATH"] = str(install_dir / "lib")

    def run(*args):
        return subprocess.run([sys.executable, cli_path, *args], capture_output=True,
                               text=True, timeout=30, env=env)

    core_timers = ("virt-renewal-monitor.timer", "virt-renewal-inventory-sync.timer",
                   "virt-renewal-hostnet-sync.timer", "virt-renewal-readiness-notifier.timer")
    core_services = ("virt-renewal-monitor.service", "virt-renewal-inventory-sync.service",
                      "virt-renewal-hostnet-sync.service", "virt-renewal-readiness-notifier.service")

    # Defensive: a stale 'failed' ActiveState from an unrelated EARLIER run of this same
    # unit name (systemd caches it until the next completed run or an explicit reset) must
    # never leak into this test's own assertions.
    for name in core_services:
        subprocess.run(["systemctl", "reset-failed", name], capture_output=True, timeout=10)

    try:
        r = run("install-activate-core-timers")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Core timer activation succeeded" in r.stdout

        for timer in core_timers:
            check = subprocess.run(["systemctl", "is-enabled", timer], capture_output=True,
                                    text=True, timeout=10)
            assert check.stdout.strip() == "enabled", f"{timer}: {check.stdout!r} {check.stderr!r}"
            active = subprocess.run(["systemctl", "is-active", timer], capture_output=True,
                                     text=True, timeout=10)
            assert active.stdout.strip() == "active", f"{timer}: {active.stdout!r}"

        # Persistence-across-reboot proxy (can't actually reboot in a test): systemd's
        # [Install] WantedBy=timers.target symlink must exist, which is what makes
        # `systemctl enable` durable rather than a one-shot in-memory `start`.
        for timer in core_timers:
            symlink = f"/etc/systemd/system/timers.target.wants/{timer}"
            assert os.path.islink(symlink), f"{timer} missing its persistence symlink"

        # Deterministically trigger each service ONCE via `systemctl start` (never rely on
        # OnBootSec/OnUnitActiveSec timing -- whether a monotonic timer's trigger point is
        # already in the past at enable time, and therefore fires immediately, depends on
        # how long this host has been up, which this test must not depend on) and wait for
        # the oneshot run to actually complete before checking its result.
        for service in core_services:
            start = subprocess.run(["systemctl", "start", service], capture_output=True,
                                    text=True, timeout=15)
            assert start.returncode == 0, f"{service}: {start.stdout!r} {start.stderr!r}"
        time.sleep(1)
        for service in core_services:
            failed = subprocess.run(["systemctl", "is-failed", service], capture_output=True,
                                     text=True, timeout=10)
            assert failed.stdout.strip() != "failed", (
                f"{service} failed -- journalctl would show: "
                f"{subprocess.run(['journalctl', '-u', service, '--no-pager', '-n', '15'], capture_output=True, text=True, timeout=10).stdout}")

        # Re-running activation must be idempotent -- never error, never duplicate.
        r = run("install-activate-core-timers")
        assert r.returncode == 0, r.stdout + r.stderr

        r = run("install-health-check")
        assert r.returncode == 0, r.stdout + r.stderr
        assert '"ok": true' in r.stdout
    finally:
        for timer in core_timers:
            subprocess.run(["systemctl", "disable", "--now", timer], capture_output=True,
                            text=True, timeout=10)
        for name in core_timers + core_services:
            path = os.path.join("/etc/systemd/system", name)
            if os.path.exists(path):
                os.remove(path)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=10)
        for timer in core_timers:
            leftover = subprocess.run(["systemctl", "is-enabled", timer], capture_output=True,
                                       text=True, timeout=10)
            assert leftover.returncode != 0, f"{timer} was not actually cleaned up"


@pytest.mark.skipif(not os.path.exists(os.path.join(DEPLOY_DIR, "build_package.sh")),
                     reason="deploy/ not present in this checkout")
@pytest.mark.skipif(not _real_systemd_usable(),
                     reason="requires root + a genuinely functional systemd on this host")
def test_uninstall_standard_mode_removes_genuinely_activated_real_units(tmp_path):
    """uninstaller.standard_uninstall's OWNED_SYSTEMD_UNITS handling was only ever exercised
    before against units that existed purely as local template files -- this verifies it
    also correctly stops/disables/removes units that install-activate-core-timers genuinely
    installed into /etc/systemd/system, which real ir1/ir2-shaped installs will now have
    after v1.10.3. Always cleans up in a finally block regardless of outcome."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(pkg) as tf:
        tf.extractall(extract_dir, filter="data")

    install_dir = tmp_path / "staging-install-uninstall-real-systemd"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_MENU"] = "1"

    install_result = subprocess.run(["bash", "install.sh"], cwd=extract_dir,
                                      capture_output=True, text=True, timeout=60, env=env)
    assert install_result.returncode == 0, install_result.stdout + install_result.stderr

    cli_path = str(install_dir / "bin" / "renewal_cli.py")
    env["PYTHONPATH"] = str(install_dir / "lib")

    def run(*args):
        return subprocess.run([sys.executable, cli_path, *args], capture_output=True,
                               text=True, timeout=30, env=env)

    core_timers = ("virt-renewal-monitor.timer", "virt-renewal-inventory-sync.timer",
                   "virt-renewal-hostnet-sync.timer", "virt-renewal-readiness-notifier.timer")
    core_services = ("virt-renewal-monitor.service", "virt-renewal-inventory-sync.service",
                      "virt-renewal-hostnet-sync.service", "virt-renewal-readiness-notifier.service")

    try:
        r = run("install-activate-core-timers")
        assert r.returncode == 0, r.stdout + r.stderr
        for timer in core_timers:
            check = subprocess.run(["systemctl", "is-enabled", timer], capture_output=True,
                                    text=True, timeout=10)
            assert check.stdout.strip() == "enabled"

        r = run("uninstall", "--yes")
        assert r.returncode == 0, r.stdout + r.stderr

        for timer in core_timers:
            check = subprocess.run(["systemctl", "is-enabled", timer], capture_output=True,
                                    text=True, timeout=10)
            assert check.returncode != 0, f"{timer} still enabled after uninstall"
        for name in core_timers + core_services:
            assert not os.path.exists(os.path.join("/etc/systemd/system", name)), (
                f"{name} unit file still present after uninstall")
    finally:
        for timer in core_timers:
            subprocess.run(["systemctl", "disable", "--now", timer], capture_output=True,
                            text=True, timeout=10)
        for name in core_timers + core_services:
            path = os.path.join("/etc/systemd/system", name)
            if os.path.exists(path):
                os.remove(path)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=10)


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

    # Mirrors install.sh's real layout (code + data under one install_dir) -- see
    # test_built_package_cli_end_to_end_staging_run's comment for why this matters: a
    # code/data split is a layout the global wrapper can never dispatch correctly from.
    install_dir = tmp_path / "staging-install"
    shutil.copytree(extract_dir / "lib", install_dir / "lib")
    shutil.copytree(extract_dir / "bin", install_dir / "bin")
    config_dir = str(install_dir / "renewal-config")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir / "lib") + ":" + str(install_dir / "bin")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_WRAPPER_PATH"] = str(tmp_path / "fake-usr-local-bin" / "tixo-renewal")
    os.makedirs(tmp_path / "fake-usr-local-bin", exist_ok=True)
    cli_path = str(install_dir / "bin" / "renewal_cli.py")

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
    shutil.copytree(extract_dir / "lib", install_dir / "lib")
    shutil.copytree(extract_dir / "bin", install_dir / "bin")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir / "lib") + ":" + str(install_dir / "bin")
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_WRAPPER_PATH"] = str(tmp_path / "fake-usr-local-bin" / "tixo-renewal")
    os.makedirs(tmp_path / "fake-usr-local-bin", exist_ok=True)
    cli_path = str(install_dir / "bin" / "renewal_cli.py")

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


# ---------- v1.10.1: install.sh itself as a real subprocess (not just renewal_cli.py install) --

def test_install_sh_fresh_install_succeeds_end_to_end(tmp_path):
    """Regression test for a real bug found on a second production hypervisor (ir2): running
    the actual `install.sh` SCRIPT (not `renewal_cli.py install` directly, which every other
    test in this file exercises) crashed with `ModuleNotFoundError: No module named
    'renewal_cli'` right after the Virtualizor detection message, because its one-line
    `PYTHONPATH="$INSTALL_DIR/lib" python3 -c 'import renewal_cli'` version-check never
    included `$INSTALL_DIR/bin`, where renewal_cli.py actually lives. The code copy itself
    still succeeded (a genuinely PARTIAL install: application files present, but systemd
    unit templates never generated and the global `tixo-renewal` wrapper never (re)created)
    -- this is exactly why the CLI could still be launched afterward (by invoking the script
    file directly, which doesn't hit this code path) even though the installer itself had
    failed. No existing test caught this because every other install-focused test here calls
    `renewal_cli.py install` as a subprocess directly, bypassing install.sh entirely. This
    test runs install.sh itself, end to end, exactly like a real operator/ir2 would."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"  # never touch this test-runner host's real systemd
    r = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0, (
        f"install.sh failed (exit {r.returncode}) -- this is exactly the ir2 symptom if it "
        f"mentions ModuleNotFoundError:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr
    assert "== Install complete ==" in r.stdout

    # Code copied.
    assert (install_dir / "bin" / "renewal_cli.py").exists()
    assert (install_dir / "lib" / "menu.py").exists()
    # Static assets copied (the OTHER gap found in the same install.sh bug hunt).
    assert (install_dir / "lib" / "static" / "live-bandwidth.js").exists()
    assert (install_dir / "lib" / "static" / "tixocloud.png").exists()
    # Systemd unit templates generated (install.sh crashed BEFORE reaching this step --
    # their absence is the clearest signal of a "partial install").
    assert (install_dir / "systemd" / "virt-renewal-monitor.service").exists()
    assert (install_dir / "systemd" / "virt-renewal-web.service").exists()
    # Config written.
    assert (install_dir / "renewal-config" / "policy.json").exists()
    assert (install_dir / "renewal-config" / "nodes.json").exists()


def test_install_sh_second_run_recovers_and_stays_idempotent(tmp_path):
    """Regression/recovery test: re-running install.sh against an install directory left in
    whatever state the FIRST run produced (e.g. the real ir2 case -- code copied, systemd
    templates missing because the first run crashed) must complete cleanly on the second
    attempt and never overwrite the operator's existing policy/node config. This is the
    documented recovery path for a partial install (`install.sh` is explicitly idempotent --
    safe to re-run)."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"  # never touch this test-runner host's real systemd

    r1 = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                         text=True, timeout=30, env=env)
    assert r1.returncode == 0, r1.stdout + r1.stderr

    # Simulate an operator having edited policy after the first install (must survive).
    policy_path = install_dir / "renewal-config" / "policy.json"
    original_policy = policy_path.read_text()
    edited_policy = original_policy.replace('"auto_renewal_enabled": false',
                                             '"auto_renewal_enabled": false  // operator note')
    policy_path.write_text(edited_policy if edited_policy != original_policy else original_policy)

    # Simulate the exact partial-install state: remove the systemd templates and the global
    # wrapper, as if the first run had crashed before reaching those steps (the real ir2 case).
    import shutil as _shutil
    _shutil.rmtree(install_dir / "systemd")
    (install_dir / "systemd").mkdir()

    r2 = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                         text=True, timeout=30, env=env)
    assert r2.returncode == 0, (
        f"second (recovery) install.sh run failed:\nSTDOUT:\n{r2.stdout}\nSTDERR:\n{r2.stderr}"
    )
    assert "Traceback" not in r2.stdout and "Traceback" not in r2.stderr

    # Recovered: systemd templates regenerated.
    assert (install_dir / "systemd" / "virt-renewal-monitor.service").exists()
    # Preserved: the operator's edited policy was never overwritten.
    assert policy_path.read_text() == (edited_policy if edited_policy != original_policy
                                        else original_policy)


def test_readme_install_command_matches_current_version():
    """Regression test: the top-level README.md's one-command install was found pinned to
    v1.1.0 -- nine releases stale -- while deploy/bootstrap.sh's own default tag had stayed
    correctly updated on every release (it has its own drift test above). No equivalent
    check existed for README.md, so this exact staleness went unnoticed through every
    subsequent release until a user reported being pointed at ancient install instructions."""
    impl_dir = os.path.join(os.path.dirname(__file__), "..", "implementation")
    sys.path.insert(0, impl_dir)
    import renewal_cli

    readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
    readme_text = open(readme_path).read()
    import re
    m = re.search(r"tixo-renewal/v([0-9.]+)/deploy/bootstrap\.sh", readme_text)
    assert m, "could not find README.md's pinned bootstrap.sh install URL"
    assert m.group(1) == renewal_cli.VERSION, (
        f"README.md's one-command install is pinned to v{m.group(1)} but "
        f"renewal_cli.VERSION is {renewal_cli.VERSION} -- update README.md's install command"
    )


# ---------- v1.10.2: install.sh auto-launches the menu in a REAL interactive terminal -----------

def _run_install_sh_with_pty(staging_dir, install_dir, extra_env=None, timeout=15):
    """Runs install.sh with its stdin/stdout/stderr attached to a REAL pseudo-terminal (not
    a captured pipe, which is what subprocess.run's capture_output normally gives you and
    is exactly what `[ -t 1 ]` correctly reports as non-interactive). Returns the captured
    output text. This is what actually exercises the `curl | bash`-interactive code path,
    not a simulation of it."""
    import pty
    import select

    master_fd, slave_fd = pty.openpty()
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = install_dir
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"  # never touch this test-runner host's real systemd
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(["bash", "install.sh"], cwd=staging_dir, stdin=slave_fd,
                             stdout=slave_fd, stderr=slave_fd, env=env, start_new_session=True)
    os.close(slave_fd)

    output = b""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                # Drain any remaining buffered output, then stop.
                try:
                    r, _, _ = select.select([master_fd], [], [], 0.3)
                    if r:
                        output += os.read(master_fd, 65536)
                except OSError:
                    pass
                break
            try:
                r, _, _ = select.select([master_fd], [], [], 0.5)
            except OSError:
                break
            if r:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                # Once the menu banner shows up, we've proven the launch happened --
                # terminate the (now-interactive, input()-blocked) menu process rather
                # than waiting out the full timeout.
                if b"Choose an option:" in output:
                    break
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    return output.decode("utf-8", errors="replace")


@pytest.mark.skipif(not hasattr(os, "openpty") and not __import__("pty"),
                     reason="pty not available on this platform")
def test_install_sh_opens_menu_on_real_interactive_terminal(tmp_path):
    """The exact requirement: the published one-command install must open the management
    menu afterward on a REAL interactive SSH-like terminal -- tested here with a genuine
    pseudo-terminal (pty), not a subprocess.run() simulation (which `[ -t 1 ]` correctly
    treats as noninteractive and is exactly why that would NOT exercise this code path)."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    output = _run_install_sh_with_pty(str(staging), str(install_dir))

    assert "Install complete" in output
    assert "Launching the TixoCloud management menu" in output
    # The menu itself actually started (its own banner/title text appeared) -- this is the
    # real proof the menu opened, not just that install.sh printed an intent-to-launch line.
    assert "TixoCloud Control Center" in output


def test_install_sh_skips_menu_in_noninteractive_subprocess(tmp_path):
    """The companion requirement: a noninteractive run (exactly what every OTHER test in
    this file does via subprocess.run with captured/piped stdout) must never attempt to
    launch the menu -- confirmed explicitly here via the printed skip message, not just by
    the test happening not to hang."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"  # never touch this test-runner host's real systemd
    r = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0
    assert "Noninteractive session detected" in r.stdout
    assert "Launching the TixoCloud management menu" not in r.stdout


def test_install_sh_no_auto_menu_env_var_suppresses_launch_even_with_a_pty(tmp_path):
    """TIXO_RENEWAL_NO_AUTO_MENU=1 must suppress the menu launch even on a real pty --
    the explicit opt-out this project's own test suite (and any CI that wants a PTY for
    other reasons) relies on."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    output = _run_install_sh_with_pty(str(staging), str(install_dir),
                                       extra_env={"TIXO_RENEWAL_NO_AUTO_MENU": "1"}, timeout=10)
    assert "TIXO_RENEWAL_NO_AUTO_MENU set" in output
    assert "Choose an option:" not in output  # menu never actually started


def test_install_sh_no_auto_systemd_env_var_suppresses_activation(tmp_path):
    """TIXO_RENEWAL_NO_AUTO_SYSTEMD=1 must suppress the real systemd activation step even
    when running as root -- the explicit opt-out for an operator who wants full manual
    control, and (not coincidentally) exactly what this project's own test suite uses
    everywhere else in this file to avoid touching the real test-runner host's systemd."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_MENU"] = "1"
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"
    r = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TIXO_RENEWAL_NO_AUTO_SYSTEMD set -- skipping automatic core-timer activation" in r.stdout
    assert "Core timer activation succeeded" not in r.stdout
    # Confirm it genuinely never touched the real host's systemd for these unit names.
    for timer in ("virt-renewal-monitor.timer", "virt-renewal-inventory-sync.timer"):
        assert not os.path.exists(os.path.join("/etc/systemd/system", timer))


def test_install_sh_never_launches_menu_after_a_failed_install(tmp_path):
    """A failed install (here: simulated by making install_dir's parent unwritable) must
    never reach the menu-launch code at all -- `set -euo pipefail` aborts the script on the
    first failing command, so this is really a test that nothing downstream of that point
    can still run."""
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    # Point PYTHON_BIN detection at a broken environment by corrupting PATH so python3
    # cannot be found -- install.sh fails fast with its own clear "ERROR: python3 not
    # found" message, well before any code copy or menu-launch logic.
    install_dir = tmp_path / "opt-tixo-renewal"
    bash_path = shutil.which("bash")
    env = {"PATH": "/nonexistent", "TIXO_RENEWAL_INSTALL_DIR": str(install_dir)}
    r = subprocess.run([bash_path, "install.sh"], cwd=str(staging), capture_output=True,
                        text=True, timeout=15, env=env)
    assert r.returncode != 0
    assert "Launching the TixoCloud management menu" not in r.stdout
    assert not install_dir.exists()


# ---------- v1.10.2: real CLI uninstall/purge, end to end against the built package --------

def _install_real_package(tmp_path):
    result = subprocess.run(["bash", "build_package.sh"], cwd=DEPLOY_DIR,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

    staging = tmp_path / "staging"
    staging.mkdir()
    pkg = os.path.join(DEPLOY_DIR, "tixo-renewal-package.tar.gz")
    with tarfile.open(pkg) as tf:
        tf.extractall(staging, filter="data")

    install_dir = tmp_path / "opt-tixo-renewal"
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = str(install_dir)
    env["TIXO_RENEWAL_NO_AUTO_MENU"] = "1"
    env["TIXO_RENEWAL_NO_AUTO_SYSTEMD"] = "1"  # never touch this test-runner host's real systemd
    r = subprocess.run(["bash", "install.sh"], cwd=str(staging), capture_output=True,
                        text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return str(install_dir)


def test_global_wrapper_dispatches_subcommands_to_renewal_cli(tmp_path):
    """Real, reported bug: every piece of this project's own documentation told operators
    to run `renewal_cli.py <command>` as if it were a global command, but the installed
    global `tixo-renewal` wrapper only ever launched the interactive menu -- any other
    command genuinely failed with 'command not found' on a real production host. This
    tests the REAL installed wrapper script (not a simulation of its logic) against
    several real subcommands, including the brand-new readiness/doctor/logs and the
    health-check/update friendly aliases."""
    install_dir = _install_real_package(tmp_path)
    wrapper = "/usr/local/bin/tixo-renewal"
    assert os.path.exists(wrapper), "global wrapper was not installed (this sandbox runs as root)"

    def run_wrapper(*args):
        return subprocess.run([wrapper, *args], capture_output=True, text=True, timeout=30)

    r = run_wrapper("status")
    assert r.returncode == 0, r.stdout + r.stderr

    # rc may be 1 here (core timers were never activated in this test install -- that's
    # correctly reported as unhealthy, see test_install_activate_core_timers_real_systemd_
    # round_trip for the activated-and-healthy case) -- what this test actually proves is
    # that the wrapper dispatched to renewal_cli.py and got a real JSON report back at all.
    r = run_wrapper("install-health-check")
    assert '"ok"' in r.stdout, r.stdout + r.stderr

    r = run_wrapper("health-check")  # friendly alias for install-health-check
    assert '"ok"' in r.stdout, r.stdout + r.stderr

    r = run_wrapper("readiness")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"nodes"' in r.stdout

    r = run_wrapper("doctor")  # rc may be 1 for the same reason as install-health-check above
    assert "Service health" in r.stdout, r.stdout + r.stderr

    r = run_wrapper("logs")
    assert r.returncode == 0, r.stdout + r.stderr

    r = run_wrapper("backup")
    assert r.returncode == 0, r.stdout + r.stderr


def test_global_wrapper_still_routes_menu_flags_to_menu_py(tmp_path):
    """--status/--version/--cli are pre-existing, documented menu.py flags -- the dispatch
    fix must not break them by accidentally routing them to renewal_cli.py's argparse
    (which would reject them outright, since they look like options, not subcommands)."""
    install_dir = _install_real_package(tmp_path)
    wrapper = "/usr/local/bin/tixo-renewal"

    r = subprocess.run([wrapper, "--status"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr

    r = subprocess.run([wrapper, "--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip()  # prints the version number


def _cli_env(install_dir):
    env = dict(os.environ)
    env["TIXO_RENEWAL_INSTALL_DIR"] = install_dir
    env["PYTHONPATH"] = os.path.join(install_dir, "lib")
    return env


def test_cli_uninstall_preview_is_real_and_read_only(tmp_path):
    install_dir = _install_real_package(tmp_path)
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")
    before = sorted(os.listdir(install_dir))

    r = subprocess.run([sys.executable, cli_path, "uninstall-preview"], capture_output=True,
                        text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["mode"] == "standard"
    assert "lib" in data["will_remove_entries"]
    assert "renewal-config" in data["will_preserve_entries"]

    after = sorted(os.listdir(install_dir))
    assert before == after


def test_cli_uninstall_standard_removes_code_preserves_data(tmp_path):
    install_dir = _install_real_package(tmp_path)
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")

    r = subprocess.run([sys.executable, cli_path, "uninstall", "--yes"], capture_output=True,
                        text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Uninstall complete" in r.stdout

    assert not os.path.exists(os.path.join(install_dir, "lib"))
    assert not os.path.exists(os.path.join(install_dir, "bin"))
    assert os.path.exists(os.path.join(install_dir, "renewal-config", "policy.json"))
    assert os.path.exists(os.path.join(install_dir, "renewal-config", "nodes.json"))


def test_cli_uninstall_purge_requires_exact_confirm_phrase(tmp_path):
    install_dir = _install_real_package(tmp_path)
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")

    r = subprocess.run([sys.executable, cli_path, "uninstall", "--purge", "--yes",
                         "--confirm", "purge all tixocloud data"],  # wrong case -- must fail
                        capture_output=True, text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode != 0
    assert os.path.exists(os.path.join(install_dir, "lib"))  # nothing was touched


def test_cli_uninstall_purge_removes_config_and_secrets_never_backups(tmp_path):
    install_dir = _install_real_package(tmp_path)
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")

    # Create a fake backup to confirm it survives the purge.
    backups_dir = os.path.join(install_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    with open(os.path.join(backups_dir, "fake-backup.tar.gz"), "wb") as f:
        f.write(b"fake")

    r = subprocess.run([sys.executable, cli_path, "uninstall", "--purge", "--yes",
                         "--confirm", "PURGE ALL TIXOCLOUD DATA"],
                        capture_output=True, text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode == 0, r.stdout + r.stderr

    assert not os.path.exists(os.path.join(install_dir, "lib"))
    assert not os.path.exists(os.path.join(install_dir, "renewal-config"))
    assert not os.path.exists(os.path.join(install_dir, "shadow-monitor-state"))
    # Backups survive a full purge by default.
    assert os.path.exists(os.path.join(install_dir, "backups", "fake-backup.tar.gz"))


def test_cli_uninstall_purge_then_delete_backups_removes_everything(tmp_path):
    install_dir = _install_real_package(tmp_path)
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")
    backups_dir = os.path.join(install_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    with open(os.path.join(backups_dir, "fake-backup.tar.gz"), "wb") as f:
        f.write(b"fake")

    r = subprocess.run([sys.executable, cli_path, "uninstall", "--purge", "--yes",
                         "--confirm", "PURGE ALL TIXOCLOUD DATA", "--delete-backups"],
                        capture_output=True, text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.path.exists(backups_dir)


def test_cli_uninstall_after_partial_install_handles_missing_units_gracefully(tmp_path):
    """Partial install (code present, nothing ever installed system-wide) must uninstall
    cleanly -- no crash, no error about missing systemd units."""
    install_dir = _install_real_package(tmp_path)
    # Simulate a partial install: remove the renewal-config dir entirely (as if install had
    # failed partway, matching the ir2 scenario from the v1.10.1 fix).
    shutil.rmtree(os.path.join(install_dir, "renewal-config"))
    cli_path = os.path.join(install_dir, "bin", "renewal_cli.py")

    r = subprocess.run([sys.executable, cli_path, "uninstall", "--yes"], capture_output=True,
                        text=True, timeout=15, env=_cli_env(install_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.path.exists(os.path.join(install_dir, "lib"))
