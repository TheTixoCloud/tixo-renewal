"""Secure local credential storage for unattended `apply` runs.

Design goal (per explicit requirement): unattended operation must not require a human to type
a password every time a systemd timer fires, but credentials must still be stored with least
privilege and never logged, printed, or committed anywhere.

- Stored as a single JSON file, written atomically, with permissions forced to 0600
  (owner read/write only) immediately after every write.
- `load_credentials()` REFUSES to use a file whose permissions are not exactly 0600 (or looser)
  -- fails closed rather than silently trusting a world/group-readable secrets file.
- Never written to by any dry-run/preview/status/install code path -- only the dedicated
  `store-credentials` CLI command writes here, and only when the operator runs it themselves.
- This module itself never prints, logs, or returns credentials in a repr/str -- callers must
  access the named fields explicitly.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class CredentialSecurityError(Exception):
    pass


class InvalidBaseUrlError(Exception):
    pass


def validate_and_normalize_base_url(url: str) -> str:
    """Rejects anything that isn't a well-formed http(s) URL with a real host, BEFORE it is
    ever stored or used to build a request. This is the primary defense against the exact
    incident this function was added for: a malformed base_url reaching urllib's request
    construction, where the resulting ValueError/URLError message can include the full
    attempted URL -- and that URL carries adminapikey/adminapipass as query parameters.
    Rejecting bad input here means that failure mode can never be reached downstream."""
    if not isinstance(url, str):
        raise InvalidBaseUrlError("base URL must be a string")
    candidate = url.strip()
    if not candidate:
        raise InvalidBaseUrlError("base URL must not be empty")
    if any(c.isspace() for c in candidate):
        raise InvalidBaseUrlError("base URL must not contain whitespace")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise InvalidBaseUrlError(
            f"base URL must start with http:// or https:// (got scheme={parsed.scheme!r})")
    if not parsed.hostname:
        raise InvalidBaseUrlError("base URL must include a host")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        # Credentials must never travel embedded in the URL itself (userinfo component) --
        # Virtualizor's API takes them as separate query parameters, which this project
        # already redacts/validates elsewhere; a userinfo-embedded credential would bypass
        # that and risk being logged by any intermediate HTTP client or proxy.
        raise InvalidBaseUrlError("base URL must not embed credentials (no user:pass@host)")
    if parsed.query or parsed.fragment:
        raise InvalidBaseUrlError(
            "base URL must not already contain a query string or fragment -- provide just "
            "the endpoint, e.g. https://host:4085/index.php")
    return candidate


@dataclass
class StoredCredentials:
    base_url: str
    api_key: str
    api_pass: str

    def __repr__(self):  # never leak secrets into a log line via an accidental repr/print
        return f"StoredCredentials(base_url={self.base_url!r}, api_key=***, api_pass=***)"

    __str__ = __repr__


def default_credentials_path(config_dir: str, node_id: str) -> str:
    return os.path.join(config_dir, "credentials", f"{node_id}.json")


def store_credentials(path: str, base_url: str, api_key: str, api_pass: str) -> None:
    """Writes the credentials file atomically and enforces 0600 permissions immediately,
    before the data is ever readable by anyone but the owner. The base URL is validated and
    normalized FIRST -- a malformed URL is rejected here, before it can ever reach a network
    call where the error message might otherwise expose the credential-bearing URL."""
    base_url = validate_and_normalize_base_url(base_url)
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"base_url": base_url, "api_key": api_key, "api_pass": api_pass}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except Exception:
        os.unlink(tmp)
        raise
    os.chmod(tmp, 0o600)  # belt-and-suspenders in case the umask widened it above
    os.replace(tmp, path)


def load_credentials(path: str) -> StoredCredentials:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no stored credentials at {path} -- run `store-credentials` first")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:  # any group/world bit set at all
        raise CredentialSecurityError(
            f"{path} has insecure permissions ({oct(mode)}) -- expected 0600. Refusing to "
            f"read it. Fix with `chmod 600 {path}` after confirming nobody untrusted already "
            f"had read access.")
    data = json.loads(Path(path).read_text())
    return StoredCredentials(base_url=data["base_url"], api_key=data["api_key"], api_pass=data["api_pass"])


def delete_credentials(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)
