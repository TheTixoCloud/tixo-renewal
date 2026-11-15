import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import credentials as cr


def test_store_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://example.invalid", "key123", "pass456")
    loaded = cr.load_credentials(path)
    assert loaded.base_url == "https://example.invalid"
    assert loaded.api_key == "key123"
    assert loaded.api_pass == "pass456"


def test_stored_file_has_0600_permissions(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "k", "p")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_refuses_group_readable_file(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "k", "p")
    os.chmod(path, 0o640)  # widen permissions, simulating an insecure deployment mistake
    with pytest.raises(cr.CredentialSecurityError):
        cr.load_credentials(path)


def test_load_refuses_world_readable_file(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "k", "p")
    os.chmod(path, 0o604)
    with pytest.raises(cr.CredentialSecurityError):
        cr.load_credentials(path)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        cr.load_credentials(str(tmp_path / "nope.json"))


def test_repr_never_leaks_secret(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "SECRET_KEY", "SECRET_PASS")
    loaded = cr.load_credentials(path)
    text = repr(loaded)
    assert "SECRET_KEY" not in text
    assert "SECRET_PASS" not in text
    assert str(loaded) == text


def test_delete_credentials(tmp_path):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "k", "p")
    cr.delete_credentials(path)
    assert not os.path.exists(path)
    cr.delete_credentials(path)  # idempotent, no error on missing file


def test_store_creates_parent_dir_with_restricted_permissions(tmp_path):
    path = str(tmp_path / "credentials" / "node1.json")
    cr.store_credentials(path, "https://x", "k", "p")
    parent_mode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    assert parent_mode == 0o700


def test_overwrite_never_leaves_partial_file_on_crash(tmp_path, monkeypatch):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, "https://x", "k1", "p1")

    real_dump = cr.json.dump

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(cr.json, "dump", boom)
    with pytest.raises(RuntimeError):
        cr.store_credentials(path, "https://x", "k2", "p2")
    monkeypatch.setattr(cr.json, "dump", real_dump)

    # original file must be untouched -- the atomic tmp-write-then-replace never got to replace
    loaded = cr.load_credentials(path)
    assert loaded.api_key == "k1"


# ---------- URL validation (the exact incident this module was hardened against) --------------

@pytest.mark.parametrize("bad_url", [
    "",
    "   ",
    "not-a-url-at-all",
    "ftp://example.com/index.php",             # wrong scheme
    "example.com/index.php",                    # missing scheme
    "https://",                                 # missing host
    "https:///index.php",                       # missing host
    "https://host:4085/index.php with space",
    "https://user:pass@host:4085/index.php",    # credentials embedded in URL itself
    "https://host:4085/index.php?already=query",
    "https://host:4085/index.php#frag",
])
def test_invalid_base_urls_are_rejected_at_store_time(tmp_path, bad_url):
    path = str(tmp_path / "creds.json")
    with pytest.raises(cr.InvalidBaseUrlError):
        cr.store_credentials(path, bad_url, "k", "p")
    assert not os.path.exists(path)  # rejected before anything is ever written


@pytest.mark.parametrize("good_url", [
    "https://host.example.com:4085/index.php",
    "http://10.0.0.5:4085/index.php",
    "https://host.example.com/index.php",
])
def test_valid_base_urls_are_accepted(tmp_path, good_url):
    path = str(tmp_path / "creds.json")
    cr.store_credentials(path, good_url, "k", "p")
    assert cr.load_credentials(path).base_url == good_url


def test_validate_and_normalize_strips_surrounding_whitespace():
    assert cr.validate_and_normalize_base_url("  https://host/index.php  ") == "https://host/index.php"


def test_non_string_base_url_rejected():
    with pytest.raises(cr.InvalidBaseUrlError):
        cr.validate_and_normalize_base_url(12345)
