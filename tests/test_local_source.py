import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import local_source as ls


def write_fake_universal_php(tmp_path):
    content = """<?php
$globals['dbhost'] = 'localhost';
$globals['db'] = 'virtualizor';
$globals['dbuser'] = 'virtualizor';
$globals['dbpass'] = 'fake-local-test-password-not-real';
"""
    path = tmp_path / "universal.php"
    path.write_text(content)
    return str(path)


def test_read_db_config_parses_expected_fields(tmp_path):
    path = write_fake_universal_php(tmp_path)
    cfg = ls._read_db_config(path)
    assert cfg == {"host": "localhost", "db": "virtualizor", "user": "virtualizor",
                   "password": "fake-local-test-password-not-real"}


def test_read_db_config_missing_key_raises(tmp_path):
    path = tmp_path / "universal.php"
    path.write_text("<?php $globals['dbhost'] = 'localhost';")
    with pytest.raises(ls.LocalSourceError):
        ls._read_db_config(str(path))


def test_fetch_live_rows_local_parses_output(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "4\t1790096510\t17473\t15636.51\n19\t1790097470\t16606\t15393.86\n"
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    rows = ls.fetch_live_rows_local(path)
    assert rows == [
        {"vpsid": 4, "created_epoch": 1790096510, "quota_gb": Decimal("17473"), "used_gb": Decimal("15636.51")},
        {"vpsid": 19, "created_epoch": 1790097470, "quota_gb": Decimal("16606"), "used_gb": Decimal("15393.86")},
    ]


def test_fetch_live_rows_local_raises_on_mysql_failure(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Access denied"

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ls.LocalSourceError):
        ls.fetch_live_rows_local(path)


def test_fetch_single_vps_quota(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "17473\n"
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    assert ls.fetch_single_vps_quota(4, path) == Decimal("17473")


def test_fetch_single_vps_used(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "15636.51\n"
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    assert ls.fetch_single_vps_used(4, path) == Decimal("15636.51")


def test_fetch_single_vps_quota_not_found_raises(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ls.LocalSourceError):
        ls.fetch_single_vps_quota(999, path)


def test_no_credentials_ever_logged_or_raised_in_exceptions(tmp_path):
    """Defensive check: the password string must never appear in an exception message from
    this module's normal error paths."""
    path = write_fake_universal_php(tmp_path)
    cfg = ls._read_db_config(path)
    try:
        ls._mysql_select("SELECT 1;", cfg, mysql_bin="/nonexistent/mysql")
    except Exception as e:
        assert "fake-local-test-password-not-real" not in str(e)


def test_fetch_single_vps_all_returns_all_fields(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "131\t1790097000\t100\t47.25\n"
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    row = ls.fetch_single_vps_all(131, path)
    assert row == {
        "vpsid": 131,
        "created_epoch": 1790097000,
        "quota_gb": Decimal("100"),
        "used_gb": Decimal("47.25"),
    }


def test_fetch_single_vps_all_not_found_raises(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ls.LocalSourceError, match="vpsid 999 not found"):
        ls.fetch_single_vps_all(999, path)


def test_fetch_single_vps_all_bad_format_raises(tmp_path, monkeypatch):
    path = write_fake_universal_php(tmp_path)

    class FakeResult:
        returncode = 0
        stdout = "only_two_fields\n"
        stderr = ""

    monkeypatch.setattr(ls.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(ls.LocalSourceError, match="unexpected query result format"):
        ls.fetch_single_vps_all(131, path)


# ---------- v1.2: DATABASE WRITE POLICY static guard --------------------------------------

def test_local_source_module_contains_no_write_sql_keywords():
    """Permanent static guard: this module must NEVER become a fallback write path. Scans
    the actual source file (not just docstrings) for SQL write keywords -- if this ever
    fails, someone added a write capability that must not exist here."""
    import inspect
    source = inspect.getsource(ls)
    forbidden = ("UPDATE ", "INSERT INTO", "DELETE FROM", "DROP TABLE", "ALTER TABLE",
                 "TRUNCATE", "REPLACE INTO")
    # Case-sensitive on purpose -- real SQL keywords in this codebase are always uppercase in
    # the f-string templates (see the existing SELECT statements), so this won't false-positive
    # on lowercase prose mentioning "update" in a comment/docstring.
    hits = [kw for kw in forbidden if kw in source]
    assert not hits, f"found write-SQL keyword(s) in local_source.py: {hits}"
