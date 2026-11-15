"""Regression test: a real bug was found where a newly-added module (credentials.py) was
used by renewal_cli.py but not included in build_package.sh's copy list, so the built
deployable package would crash with ModuleNotFoundError the moment anyone ran it on a real
Virtualizor host. This test actually builds the package and imports it in total isolation
(no implementation/ on sys.path) to catch that exact class of drift continuously, not just
once during manual review.
"""
import os
import subprocess
import sys
import tarfile

import pytest

DEPLOY_DIR = os.path.join(os.path.dirname(__file__), "..", "deploy")


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
        "alerts, shadow_monitor, engine, models, storage, credentials\n"
        "print('OK')\n"
    ) % str(extract_dir / "bin")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(extract_dir / "lib")
    result = subprocess.run([sys.executable, "-c", check_script], capture_output=True,
                             text=True, timeout=15, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


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
