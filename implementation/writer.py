"""Production writer: applies a quota change via Virtualizor's own admin HTTP API
(`act=managevps`), NEVER direct SQL -- reusing the exact call pattern already established and
used manually in `virt-apply-quotas.py` (same `base`/`act`/params shape).

STRUCTURALLY BLOCKED by design, independent of any policy/config state:
  1. `is_write_approved()` must be True (operator creates renewal-config/WRITE_APPROVED).
  2. The caller must pass `confirm=True` explicitly (no default path reaches this).
  3. `call_managevps_api()` requires live API credentials at CALL time. Interactively-typed
     credentials are never stored. For genuinely unattended runs, credentials may instead come
     from the secure local store (see credentials.py) -- but that store is populated ONLY by
     an operator running `store-credentials` themselves, never automatically.

Crash/timeout/restart/concurrency safety is implemented via a durable `operations` table
(see shadow_monitor.SCHEMA): every attempt is recorded BEFORE the HTTP call, so a crash
between "recorded" and "API responded" leaves an UNKNOWN_NEEDS_VERIFICATION row that the next
run (or operator) must resolve with a live read -- it is NEVER retried blindly, because we
cannot know whether the previous call actually reached Virtualizor before failing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import credentials as credentials_mod
import policy as policy_mod


class SanitizedNetworkError(Exception):
    """Raised instead of letting a raw urllib/network exception propagate. Its message has
    already been scrubbed of any credential value and of adminapikey=/adminapipass= query
    fragments -- safe to print, log, or let bubble up to an uncaught-exception traceback."""
    pass


def redact_url_patterns(text: str) -> str:
    """Pattern-only redaction, usable even when the exact secret value isn't available in
    scope (e.g. a top-level catch-all that doesn't have a `creds` object) -- strips any
    adminapikey=/adminapipass= query fragment generically."""
    text = str(text)
    text = re.sub(r"adminapikey=[^&\s'\"]*", "adminapikey=***REDACTED***", text)
    text = re.sub(r"adminapipass=[^&\s'\"]*", "adminapipass=***REDACTED***", text)
    return text


def _redact_secret_text(text: str, *secrets: str) -> str:
    """Removes every occurrence of each secret value verbatim, then also strips any
    adminapikey=.../adminapipass=... query fragments generically (belt-and-suspenders, in
    case urlencoding or partial-string truncation ever splits a secret across a boundary the
    exact-match pass wouldn't catch)."""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
            text = text.replace(urllib.parse.quote(secret), "***REDACTED***")
    text = re.sub(r"adminapikey=[^&\s'\"]*", "adminapikey=***REDACTED***", text)
    text = re.sub(r"adminapipass=[^&\s'\"]*", "adminapipass=***REDACTED***", text)
    return text


def _safe_call(creds: "ApiCredentials", fn, *args, **kwargs):
    """Runs fn(*args, **kwargs) and, on ANY exception, re-raises a SanitizedNetworkError
    whose message has had creds.api_key/creds.api_pass (and any adminapikey=/adminapipass=
    fragment) stripped out -- using `raise ... from None` so the original exception (which
    may still carry the credential-bearing URL in ITS own message/args) is never chained and
    therefore never printed by a later `traceback.print_exc()` or default exception handler."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        safe_message = _redact_secret_text(str(e), creds.api_key, creds.api_pass)
        raise SanitizedNetworkError(safe_message) from None

OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    vpsid INTEGER NOT NULL,
    period_key TEXT NOT NULL,
    requested_new_quota_gb TEXT NOT NULL,
    status TEXT NOT NULL,          -- PENDING | IN_PROGRESS | APPLIED | FAILED | UNKNOWN_NEEDS_VERIFICATION
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    api_response TEXT,
    verified_after_quota_gb TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_vpsid_period ON operations(node_id, vpsid, period_key);
"""


class WriteBlockedError(Exception):
    pass


def is_write_approved(approval_marker_path: str) -> bool:
    return os.path.exists(approval_marker_path)


def ensure_operations_schema(conn) -> None:
    conn.executescript(OPERATIONS_SCHEMA)
    # Idempotent migration for upgrades from v1.1 and earlier, whose operations table has no
    # retry_count column -- SQLite has no "ADD COLUMN IF NOT EXISTS", so the duplicate-column
    # error is the normal, expected outcome on every run after the first and is swallowed
    # deliberately. Never touches existing rows' other data; new rows default to 0.
    try:
        conn.execute("ALTER TABLE operations ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # CRITICAL real duplicate-renewal fix (found and proven during the v1.10.4 engineering
    # mission, 2026-11-24): apply_renewal()'s own dedup check (find_any_prior_operation, a
    # plain SELECT) and its claim (record_operation_start, a SEPARATE INSERT transaction)
    # were NOT atomic -- two genuinely concurrent processes (e.g. a manually-invoked
    # `apply-due` run overlapping with the systemd-timer-triggered one, which bypasses the
    # unit's own `flock` wrapper entirely since that lock is only applied to the generated
    # systemd ExecStart line, never inside the Python code itself) could both pass the
    # SELECT, then both successfully INSERT a row for the IDENTICAL (node_id, vpsid,
    # period_key) -- there was no UNIQUE constraint stopping that -- and both proceed to
    # issue a real write to Virtualizor for the SAME renewal period. Reproduced directly
    # (two separate sqlite3 connections to the same file, racing the exact two statements)
    # before this fix existed; see test_writer.py's
    # test_concurrent_apply_renewal_for_the_same_period_key_is_never_double_applied for the
    # permanent regression test. This UNIQUE index turns the fix into one the DATABASE
    # itself enforces (the only thing that can truly make check-then-insert atomic across
    # processes) rather than relying on apply-level sequencing alone; apply_renewal() now
    # also explicitly catches the resulting sqlite3.IntegrityError as "someone else claimed
    # this period first," never letting it surface as an uncaught exception.
    #
    # Best-effort migration for an existing database that might already (from before this
    # fix existed) contain genuine duplicate (node_id, vpsid, period_key) rows: CREATE
    # UNIQUE INDEX would itself raise on top of pre-existing duplicate data. That is NOT
    # swallowed silently -- it is a real, pre-existing data integrity problem an operator
    # must investigate (most likely via `resolve-operation`) -- but it must also never crash
    # every single command that calls this function (effectively the whole application) on
    # startup, so the failure is caught and surfaced as a clear, one-time stderr warning
    # rather than an unhandled exception.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_operations_unique_period "
            "ON operations(node_id, vpsid, period_key)")
    except sqlite3.IntegrityError as e:
        import sys as _sys
        print(f"WARNING: could not create the duplicate-prevention unique index on "
              f"operations(node_id, vpsid, period_key) -- this database already contains "
              f"conflicting duplicate rows: {e}. Duplicate-renewal protection is NOT fully "
              f"enforced by the database until this is resolved manually (inspect and "
              f"resolve the conflicting rows, e.g. via `resolve-operation`, then restart).",
              file=_sys.stderr)


def find_any_prior_operation(conn, node_id: str, vpsid: int, period_key: str):
    """Returns any prior operation for this (node, vpsid, period_key), regardless of status.
    A new operation must NEVER be started while one already exists for the same period --
    that would be the double-application this system exists to prevent.

    APPLIED  -> already granted, block duplicate (exactly-once semantics).
    FAILED   -> operator must resolve via `resolve-operation` before any retry.
    Terminal statuses are resolved by the operator explicitly, never silently bypassed."""
    row = conn.execute(
        "SELECT operation_id, status FROM operations WHERE node_id=? AND vpsid=? AND period_key=? "
        "ORDER BY created_at DESC LIMIT 1",
        (node_id, vpsid, period_key),
    ).fetchone()
    return row


# Minimum time required between two writes to the SAME vpsid, even across DIFFERENT
# period_keys (find_any_prior_operation above only protects the same period_key). Added after
# a real incident on ir1 (2026-11-14): an increase (100->101 GB) and a decrease (101->100 GB)
# issued ~8 seconds apart against the same vpsid -- the increase was verified APPLIED, but the
# decrease's live read-back never changed, even on re-reads several seconds later. The exact
# same decrease payload, issued by Virtualizor's own admin panel with more spacing between
# requests, did succeed. Virtualizor's managevps implementation is IonCube-encoded and cannot
# be inspected, so the precise mechanism (a per-vpsid edit lock, a stale-cache read during
# request processing, or something else) cannot be proven -- but the request payload was
# verified byte-identical to a known-working call (see api_log), so the defect is on
# Virtualizor's side, not in this project's request shape. Spacing writes out is the only
# available mitigation without access to Virtualizor's internals.
WRITE_COOLDOWN_SECONDS = 15.0

# A single matching live read immediately after the API call is not proof the write will
# stick -- add one confirmatory re-read after a short settle delay before trusting it. This
# guards against the symmetric failure mode: a "false success" where the first read-after
# happens to match (e.g. a race against another in-flight write) but the value then reverts.
CONFIRM_READ_DELAY_SECONDS = 2.0


def find_recent_operation_for_vpsid(conn, node_id: str, vpsid: int):
    """Returns the most recently updated operation for this (node, vpsid) across ALL
    period_keys, regardless of status -- used only for the write-cooldown check below, never
    for the same-period duplicate check (that remains find_any_prior_operation's job)."""
    return conn.execute(
        "SELECT operation_id, period_key, status, updated_at FROM operations "
        "WHERE node_id=? AND vpsid=? ORDER BY updated_at DESC LIMIT 1",
        (node_id, vpsid),
    ).fetchone()


# Substrings matched case-insensitively against Virtualizor's own `error` field text to
# recognize a write rejected because the VPS is temporarily locked (backup/restore/migration
# in progress). IMPORTANT, STATED HONESTLY: this list is based on Virtualizor's general,
# documented/community-observed error-message conventions for rejected admin actions -- it
# has NOT been empirically confirmed against a real locked VPS on ir1 (doing so would require
# deliberately triggering a real backup against a real customer VPS purely to observe the
# error text, which this project will not do). If a real locked-VPS error message is ever
# observed in production, update this list to match it exactly and note the evidence here.
LOCK_ERROR_SUBSTRINGS = ("backup", "lock", "in progress", "migrat", "restor", "busy")

DEFAULT_MAX_DEFERRAL_HOURS = 72


def classify_api_error_text(error_text) -> str:
    """Given Virtualizor's own `error` field (any type -- never assume str), returns
    'DEFERRED_LOCKED' if it looks like a temporary VPS lock (backup/restore/migration),
    else the generic 'API_REPORTED_ERROR'. Never used to decide between success/failure on
    its own -- callers still require the live read-back to ever reach APPLIED."""
    text = str(error_text).lower()
    if any(s in text for s in LOCK_ERROR_SUBSTRINGS):
        return "DEFERRED_LOCKED"
    return "API_REPORTED_ERROR"


def find_deferred_operation_for_vpsid(conn, node_id: str, vpsid: int):
    """Returns the most recent DEFERRED_LOCKED operation for this (node, vpsid), across any
    period_key -- a locked VPS may not show a fresh reset pattern on the exact period it was
    first deferred for while still locked, so apply_due checks this BEFORE computing a new
    baseline, not just for the one specific period that was active at deferral time."""
    return conn.execute(
        "SELECT operation_id, period_key, requested_new_quota_gb, created_at, retry_count "
        "FROM operations WHERE node_id=? AND vpsid=? AND status='DEFERRED_LOCKED' "
        "ORDER BY created_at DESC LIMIT 1",
        (node_id, vpsid),
    ).fetchone()


def record_operation_start(conn, operation_id: str, node_id: str, vpsid: int, period_key: str,
                            requested_new_quota_gb) -> None:
    """Raises sqlite3.IntegrityError (caller's job to handle -- see apply_renewal) if the
    UNIQUE index on (node_id, vpsid, period_key) rejects this insert because another,
    genuinely concurrent process already claimed this period first. Always rolls back
    cleanly on ANY failure so `conn` is never left mid-transaction -- a caller that catches
    the IntegrityError and keeps using the same connection (as apply_renewal does, to
    immediately re-query the winning row) must get a connection back in a clean state."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO operations (operation_id, node_id, vpsid, period_key, requested_new_quota_gb, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?, 'IN_PROGRESS', ?, ?)",
            (operation_id, node_id, vpsid, period_key, str(requested_new_quota_gb), now, now),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def record_operation_result(conn, operation_id: str, status: str, api_response: str = None,
                             verified_after_quota_gb=None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN")
    conn.execute(
        "UPDATE operations SET status=?, updated_at=?, api_response=?, verified_after_quota_gb=? "
        "WHERE operation_id=?",
        (status, now, api_response,
         str(verified_after_quota_gb) if verified_after_quota_gb is not None else None, operation_id),
    )
    conn.execute("COMMIT")


@dataclass
class ApiCredentials:
    base_url: str   # e.g. "https://your-virtualizor-host.example.com:4085/index.php"
    api_key: str
    api_pass: str


def call_managevps_api(creds: ApiCredentials, vpsid: int, new_bandwidth_gb, timeout: int = 20,
                        http_post=None) -> dict:
    """Calls Virtualizor's documented `act=managevps` endpoint to set a VPS's bandwidth quota
    -- the SAME API surface and POST body shape already verified working in production's own
    manual apply scripts (confirmed by reading `/root/virt-api-test.py` read-only: it POSTs
    `vpsid`, `bandwidth`, `theme_edit=1`, `editvps=1` -- the `theme_edit`/`editvps` fields are
    REQUIRED for Virtualizor to actually process the edit, not optional; an earlier version of
    this function omitted them). `http_post` is injectable for tests; defaults to a real HTTP
    POST (never called by tests).

    Returns {"raw": <response text>, "parsed": <dict or None>}. `parsed` is Virtualizor's own
    JSON response (with `done`/`error` keys) when the response is valid JSON -- used by
    apply_renewal() for a FASTER, clearer failure signal, but the live DB re-read remains the
    ultimate authority (matching the same belt-and-suspenders pattern the real manual script
    already uses: it also re-reads the DB after a 'done' response before trusting it)."""
    # Re-validate defensively every call, not just at store-credentials time -- a stored
    # credentials file could predate this check or have been hand-edited. A malformed
    # base_url is rejected HERE, before any urllib call that could embed it (and the
    # adjacent adminapikey/adminapipass) in an unhandled exception's message.
    safe_base_url = credentials_mod.validate_and_normalize_base_url(creds.base_url)
    params = urllib.parse.urlencode({
        "act": "managevps", "vpsid": vpsid, "api": "json",
        "adminapikey": creds.api_key, "adminapipass": creds.api_pass,
    })
    body = urllib.parse.urlencode({
        "vpsid": vpsid, "bandwidth": str(new_bandwidth_gb), "theme_edit": 1, "editvps": 1,
    })
    url = f"{safe_base_url}?{params}"
    if http_post is None:
        def http_post(url, data, timeout):
            req = urllib.request.Request(url, data=data.encode(), method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
    raw = _safe_call(creds, http_post, url, body, timeout)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    return {"raw": raw, "parsed": parsed}


def _http_get_with_metadata(url: str, timeout: int) -> dict:
    """Real GET, capturing HTTP status and Content-Type alongside the body -- used by the
    diagnostic path. Never used for the actual credential-bearing request's secret handling;
    the caller is responsible for sanitizing before this is ever logged or printed."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode(errors="replace")
        return {"status": resp.status, "content_type": resp.headers.get("Content-Type"), "body": body}


def sanitize_json_structure(obj, max_depth: int = 3, _depth: int = 0):
    """Describes the SHAPE of a parsed JSON value without ever including any actual value OR
    any dict KEY NAME below the top level.

    Dict key names are only ever disclosed at `_depth == 0` (the API response's own top-level
    field names -- a small, fixed, generic vocabulary like "done"/"error"/"vs", effectively
    guaranteed non-sensitive). At any deeper level, a dict's keys are NOT real field names --
    in Virtualizor-style responses a nested dict is commonly keyed by vpsid OR by IP address
    (confirmed pattern in real production receipt data, see analysis/ -- a dict literally
    keyed by customer IP). Disclosing those key names would leak customer-identifying data
    through a 'just structure' diagnostic, exactly what this function exists to prevent. So
    below the top level, only a COUNT and a representative value-structure are shown -- never
    the keys themselves.
    """
    if _depth >= max_depth:
        return {"_type": type(obj).__name__, "_truncated": True}
    if isinstance(obj, dict):
        if _depth == 0:
            return {"_type": "object", "_key_count": len(obj),
                    "_keys": {k: sanitize_json_structure(v, max_depth, _depth + 1) for k, v in obj.items()}}
        # Below the top level: never disclose key names (they may be vpsid/IP/hostname --
        # customer-identifying). Show count + the structure of one representative value only.
        sample_value = next(iter(obj.values())) if obj else None
        return {"_type": "object", "_key_count": len(obj), "_keys_redacted": True,
                "_sample_value": sanitize_json_structure(sample_value, max_depth, _depth + 1) if obj else None}
    if isinstance(obj, list):
        sample = sanitize_json_structure(obj[0], max_depth, _depth + 1) if obj else None
        return {"_type": "array", "_length": len(obj), "_sample_element": sample}
    if isinstance(obj, bool):
        return {"_type": "boolean"}
    if obj is None:
        return {"_type": "null"}
    return {"_type": type(obj).__name__}  # "str" / "int" / "float" -- type only, never the value


def diagnose_listvs_response(creds: ApiCredentials, timeout: int = 20, http_get_detailed=None) -> dict:
    """STRICTLY READ-ONLY diagnostic: calls the exact same `act=listvs` endpoint as
    `call_listvs_api`/`probe_api_reachability` (no vpsid, no mutation parameter) and returns
    a SANITIZED report: HTTP status, response Content-Type, top-level JSON key names, and a
    structural summary (types/counts/shapes only -- see sanitize_json_structure()). Never
    includes the raw response body, any field VALUE, customer data, credentials, or the
    credential-bearing request URL. Any exception is caught and reduced to a secret-free
    message via the same mechanism as call_listvs_api/call_managevps_api.
    """
    try:
        safe_base_url = credentials_mod.validate_and_normalize_base_url(creds.base_url)
    except credentials_mod.InvalidBaseUrlError as e:
        return {"status": None, "content_type": None, "top_level_keys": None,
                "structure": None, "error": str(e)}

    params = urllib.parse.urlencode({
        "act": "listvs", "api": "json",
        "adminapikey": creds.api_key, "adminapipass": creds.api_pass,
    })
    url = f"{safe_base_url}?{params}"
    fetch = http_get_detailed or _http_get_with_metadata
    try:
        meta = _safe_call(creds, fetch, url, timeout)
    except SanitizedNetworkError as e:
        return {"status": None, "content_type": None, "top_level_keys": None,
                "structure": None, "error": str(e)}

    status = meta.get("status")
    content_type = meta.get("content_type")
    body = meta.get("body", "")
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {"status": status, "content_type": content_type, "top_level_keys": None,
                "structure": {"_type": "unparseable", "_body_length": len(body)}, "error": None}

    top_level_keys = sorted(parsed.keys()) if isinstance(parsed, dict) else None
    return {"status": status, "content_type": content_type, "top_level_keys": top_level_keys,
            "structure": sanitize_json_structure(parsed), "error": None}


def call_listvs_api(creds: ApiCredentials, timeout: int = 20, http_get=None) -> dict:
    """Calls Virtualizor's `act=listvs` admin API action -- a pure LISTING/read action with
    NO vpsid, NO `editvps`, NO `theme_edit`, and NO POST body of any kind. It cannot mutate
    any VPS under any response behavior, because the request itself carries no mutation
    parameter -- unlike probing `managevps` with a sentinel vpsid, which is still a write-
    shaped request and would only be safe if Virtualizor happens to reject it correctly.

    NAMING CAVEAT (found empirically on the real ir1 installation in v1.4.0 development,
    read-only, key-names-only inspection -- never customer values): despite its name,
    `act=listvs` on this Virtualizor version returns PHYSICAL-SERVER/NODE listing data
    (`servers`/`stats` keys), NOT individual VPS records. It has been kept, unrenamed, purely
    because it has always only ever been used as a lightweight "are these credentials/this
    URL valid" smoke test (validate-credentials/diagnose-api) -- for which any authenticated,
    error-free JSON response works equally well regardless of which admin entity it lists.
    For actual per-VPS inventory data, use `call_vs_api()` below (`act=vs`), which was
    confirmed by the same empirical inspection to return the real per-VPS listing.

    Method: HTTP GET. Endpoint: `{base_url}?act=listvs&api=json&adminapikey=...&adminapipass=...`.
    No request body. `http_get` is injectable for tests; defaults to a real GET (never called
    by tests)."""
    safe_base_url = credentials_mod.validate_and_normalize_base_url(creds.base_url)
    params = urllib.parse.urlencode({
        "act": "listvs", "api": "json",
        "adminapikey": creds.api_key, "adminapipass": creds.api_pass,
    })
    url = f"{safe_base_url}?{params}"
    if http_get is None:
        def http_get(url, timeout):
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
    raw = _safe_call(creds, http_get, url, timeout)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    return {"raw": raw, "parsed": parsed}


def call_vs_api(creds: ApiCredentials, timeout: int = 20, http_get=None,
                 extra_params: dict = None) -> dict:
    """Calls Virtualizor's `act=vs` admin API action -- the REAL per-VPS listing endpoint
    (confirmed empirically, read-only key-names-only, against the real ir1 installation in
    v1.4.0 development: top-level `vs` dict keyed by vpsid, one entry per VPS, with fields
    including vpsid/hostname/vps_name/ram/cores/space/bandwidth/used_bandwidth/virt/suspended/
    machine_status/locked/serid). Same safety shape as call_listvs_api: pure GET, api=json,
    NO vpsid/editvps/theme_edit/POST body -- cannot mutate anything.

    PAGINATION (found empirically in v1.5.0 development: this endpoint silently caps results
    at a default page size -- confirmed ~50 on the real ir1 installation -- with NO
    pagination indicator in a single response; the only way to detect truncation is the
    `servers[].numvps` authoritative count). `extra_params` lets callers (virtualizor_info.
    fetch_vs_inventory) pass `reslen`/`page` to walk all pages; never used to pass any
    mutation-shaped parameter.

    Method: HTTP GET. Endpoint: `{base_url}?act=vs&api=json&adminapikey=...&adminapipass=...`.
    `http_get` is injectable for tests; defaults to a real GET (never called by tests)."""
    safe_base_url = credentials_mod.validate_and_normalize_base_url(creds.base_url)
    query = {"act": "vs", "api": "json", "adminapikey": creds.api_key, "adminapipass": creds.api_pass}
    if extra_params:
        query.update(extra_params)
    params = urllib.parse.urlencode(query)
    url = f"{safe_base_url}?{params}"
    if http_get is None:
        def http_get(url, timeout):
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
    raw = _safe_call(creds, http_get, url, timeout)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    return {"raw": raw, "parsed": parsed}


def probe_api_reachability(creds: ApiCredentials, timeout: int = 20, http_get_detailed=None) -> dict:
    """Validates that the base URL and credentials are usable WITHOUT any possibility of
    writing anything: calls the exact same strictly-read-only `act=listvs` endpoint as
    `diagnose_listvs_response()` (no vpsid, no mutation parameter) and reuses ITS sanitized
    structural summary to classify confidence -- never printing/returning raw response
    content, only shape (HTTP status, presence/absence of an `error` key, whether the
    top-level object has any keys at all).

    Classification, in order (deliberately NOT dependent on guessing an exact Virtualizor
    field name like `vs`, since the real response shape was found to differ from an earlier
    assumption -- see docs/native-reset-investigation.md history):
      1. Network/URL error (sanitized) -> 'ERROR'.
      2. HTTP status 401/403 -> 'CREDENTIALS_LIKELY_INVALID' (status-code signal, most reliable).
      3. Valid JSON object with an `error` key whose text matches a known auth-failure
         phrase -> 'CREDENTIALS_LIKELY_INVALID'.
      4. Valid JSON object/array with NO `error` key and at least one key/element -> treated
         as a genuine authenticated response of SOME recognized shape -> 'CREDENTIALS_LIKELY_VALID'.
      5. Anything else (unparseable, empty object, unrecognized shape) -> 'UNKNOWN' -- use
         `diagnose_listvs_response()` / the `diagnose-api` CLI command to see the actual
         (sanitized) shape and refine this function once the real format is confirmed.
    Treat 'confidence' as a hint for a human, never an automated go/no-go signal.
    """
    try:
        diag = diagnose_listvs_response(creds, timeout=timeout, http_get_detailed=http_get_detailed)
    except (credentials_mod.InvalidBaseUrlError, SanitizedNetworkError) as e:
        return {"confidence": "ERROR", "detail": str(e)}
    if diag.get("error"):
        return {"confidence": "ERROR", "detail": diag["error"]}

    status = diag.get("status")
    if status in (401, 403):
        return {"confidence": "CREDENTIALS_LIKELY_INVALID", "detail": f"HTTP {status}"}

    structure = diag.get("structure")
    top_level_keys = diag.get("top_level_keys")
    if structure is None or structure.get("_type") == "unparseable":
        return {"confidence": "UNKNOWN", "detail": "response was not valid JSON"}

    if top_level_keys is not None and "error" in top_level_keys:
        error_node = structure["_keys"]["error"]
        # We never read the actual error text here (that would require the raw body, which
        # this function deliberately never has) -- a string-typed `error` key is itself a
        # strong enough signal to flag likely-invalid without inspecting its content.
        if error_node.get("_type") == "str":
            return {"confidence": "CREDENTIALS_LIKELY_INVALID",
                    "detail": "response contains a string `error` field"}
        return {"confidence": "UNKNOWN", "detail": "response has an `error` key of unexpected type"}

    if structure.get("_type") == "object" and structure.get("_key_count", 0) > 0:
        return {"confidence": "CREDENTIALS_LIKELY_VALID",
                "detail": f"authenticated-looking response with keys: {top_level_keys}"}
    if structure.get("_type") == "array" and structure.get("_length", 0) >= 0:
        return {"confidence": "CREDENTIALS_LIKELY_VALID",
                "detail": f"authenticated-looking array response (length {structure.get('_length')})"}

    return {"confidence": "UNKNOWN",
            "detail": f"unrecognized response shape: {structure.get('_type')} -- run "
                       f"`diagnose-api` for a full sanitized structural report"}


def apply_renewal(conn, node_id: str, vpsid: int, period_key: str, new_quota_gb,
                   creds: ApiCredentials, approval_marker_path: str, confirm: bool,
                   read_live_quota_fn, http_post=None,
                   write_cooldown_seconds: float = WRITE_COOLDOWN_SECONDS,
                   confirm_read_delay_seconds: float = CONFIRM_READ_DELAY_SECONDS,
                   sleep_fn=time.sleep, now_fn=lambda: datetime.now(timezone.utc)) -> dict:
    """The full safe-apply workflow. `read_live_quota_fn(vpsid) -> Decimal` must perform a
    fresh READ-ONLY query immediately before and after the write, to detect a native-reset
    race (something else changed the quota between proposal time and apply time) and to
    verify the write actually took effect.

    Returns a result dict; never raises on API failure (that is recorded as FAILED), but
    raises WriteBlockedError if the structural safety gates are not satisfied -- that is a
    programming/configuration error, not a normal outcome, and must not be silently caught.
    """
    if not confirm:
        raise WriteBlockedError("apply_renewal() called without confirm=True")
    # Defense in depth: a caller passing a str (e.g. straight from a policy preview dict,
    # which serializes Decimals to strings for JSON/printing) must not silently fail the
    # post-write verification comparison just because Decimal("200") != "200" in Python.
    new_quota_gb = Decimal(new_quota_gb) if not isinstance(new_quota_gb, Decimal) else new_quota_gb
    # Unconditional defensive rounding, regardless of caller: this is the ONE choke point every
    # real write path goes through (manual `apply`, `apply-due`, the integration test script),
    # so it is also the only place that can GUARANTEE no fractional GB ever reaches
    # Virtualizor's integer-only `bandwidth` column -- policy_integration.preview_renewal
    # already rounds too (so apply-due's displayed preview matches what actually gets sent),
    # but the manual `apply` CLI command takes --new-quota-gb directly from the operator and
    # bypasses preview_renewal entirely. That exact gap is what produced the real VPS 106
    # incident (2026-11-15: requested 1009.59, Virtualizor silently truncated to 1009, live
    # read-back correctly refused to call it APPLIED). Rounding here closes it for every
    # caller, present and future, without relying on each one remembering to round first.
    new_quota_gb = policy_mod.round_quota_for_virtualizor(new_quota_gb)
    # CRITICAL, unconditional structural guard: Virtualizor's bandwidth<=0 is empirically
    # confirmed to mean UNLIMITED (see MANUAL_ONLY_UNLIMITED detection and
    # policy_integration.preview_renewal's matching check). Writing a calculated 0 GB target
    # (e.g. a VPS with zero remaining balance and an explicitly configured 0 GB gift) would
    # silently make the VPS unlimited -- the opposite of the intended "no further allowance."
    # This is the SAME shared choke point the fractional-rounding fix uses, so it protects the
    # manual `apply` CLI path too, which bypasses preview_renewal's own check entirely. A
    # programming/configuration error if reached with confirm=True, not a recoverable outcome
    # -- there is no safe alternative value this function is authorized to substitute.
    if new_quota_gb <= 0:
        raise WriteBlockedError(
            f"refusing to write bandwidth={new_quota_gb} GB: on this platform, a value <=0 "
            f"means UNLIMITED, not 'zero allowance' -- this would remove the VPS's bandwidth "
            f"limit entirely. Blocked structurally; requires an explicitly-approved "
            f"alternative (e.g. a minimum floor above 0) before any write can proceed.")
    if not is_write_approved(approval_marker_path):
        raise WriteBlockedError(f"write not approved: {approval_marker_path} does not exist")

    ensure_operations_schema(conn)
    existing = find_any_prior_operation(conn, node_id, vpsid, period_key)
    if existing:
        return {"status": "BLOCKED_UNRESOLVED_PRIOR_OPERATION", "operation_id": existing[0],
                "prior_status": existing[1]}

    # Cooldown against a DIFFERENT period_key for the SAME vpsid -- see WRITE_COOLDOWN_SECONDS
    # docstring. Purely advisory: no operation row is created for a cooldown block, so it never
    # shows up as an unresolved operation needing resolve-operation.
    recent = find_recent_operation_for_vpsid(conn, node_id, vpsid)
    if recent is not None:
        recent_operation_id, recent_period_key, recent_status, recent_updated_at = recent
        if recent_period_key != period_key:
            elapsed = (now_fn() - datetime.fromisoformat(recent_updated_at)).total_seconds()
            if elapsed < write_cooldown_seconds:
                return {"status": "BLOCKED_WRITE_COOLDOWN", "operation_id": recent_operation_id,
                        "seconds_remaining": write_cooldown_seconds - elapsed}

    operation_id = f"{node_id}:{vpsid}:{period_key}:{int(time.time())}"
    try:
        record_operation_start(conn, operation_id, node_id, vpsid, period_key, new_quota_gb)
    except sqlite3.IntegrityError:
        # The UNIQUE index on (node_id, vpsid, period_key) just did its job: another,
        # genuinely concurrent process won the race and claimed this period between our
        # find_any_prior_operation() check above and this INSERT -- this is the exact
        # TOCTOU window that check-then-insert alone cannot close; the database constraint
        # is what actually makes this atomic. Report it exactly like the ordinary
        # already-claimed case, never as an uncaught exception, and never proceed to call
        # the live write API ourselves.
        existing = find_any_prior_operation(conn, node_id, vpsid, period_key)
        return {"status": "BLOCKED_UNRESOLVED_PRIOR_OPERATION",
                "operation_id": existing[0] if existing else None,
                "prior_status": existing[1] if existing else None,
                "detail": "lost a concurrent race to claim this period_key"}
    return _execute_write_and_verify(conn, operation_id, vpsid, new_quota_gb, creds, http_post,
                                      confirm_read_delay_seconds, sleep_fn, read_live_quota_fn)


def _execute_write_and_verify(conn, operation_id: str, vpsid: int, new_quota_gb: Decimal,
                               creds: ApiCredentials, http_post, confirm_read_delay_seconds: float,
                               sleep_fn, read_live_quota_fn) -> dict:
    """The actual API-call-and-verify tail, shared by apply_renewal() (fresh operation) and
    retry_deferred_renewal() (resuming an existing DEFERRED_LOCKED operation) -- exactly one
    implementation of the fail-closed/false-success/lock-classification logic, never two
    copies that could silently drift apart. The caller is responsible for all pre-checks
    (structural gates, dedup, cooldown, age limits) and for having already called
    record_operation_start (or equivalent) for `operation_id`."""
    try:
        read_live_quota_fn(vpsid)  # pre-write read: surfaces an unreachable host immediately
    except Exception as e:
        record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                 api_response=f"pre-write read failed: {e}")
        return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id}

    try:
        api_result = call_managevps_api(creds, vpsid, new_quota_gb, http_post=http_post)
    except Exception as e:
        # We truly do not know if Virtualizor received and applied the request before the
        # exception (e.g. a timeout can occur after the server already processed it). Never
        # assume FAILED -- that could cause a dangerous blind retry/double-apply later.
        record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                 api_response=f"API call raised: {e}")
        return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id}

    # Response validation: an explicit error field is a strong, direct signal of failure --
    # never let a coincidentally-matching live read override it. This is deliberately NOT the
    # only check (Virtualizor's own response shape cannot be fully trusted either way -- see
    # WRITE_COOLDOWN_SECONDS docstring for a real incident where the API gave no error at all
    # for a write that silently did not take effect), so a clean response still goes on to the
    # live read-back checks below rather than being trusted by itself.
    parsed = api_result.get("parsed") if isinstance(api_result, dict) else None
    if isinstance(parsed, dict) and parsed.get("error"):
        # Classify BEFORE recording -- a temporary lock (backup/restore/migration) is a
        # distinct, expected, retryable outcome, never conflated with a generic rejection
        # (bad vpsid, permission error, etc.), which stays terminal until an operator acts.
        status = classify_api_error_text(parsed.get("error"))
        record_operation_result(conn, operation_id, status,
                                 api_response=f"API explicitly reported an error: {api_result}")
        return {"status": status, "operation_id": operation_id}

    try:
        live_after = read_live_quota_fn(vpsid)
    except Exception as e:
        record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                 api_response=f"API responded ({api_result}) but post-write "
                                              f"verification read failed: {e}")
        return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id}

    if live_after != new_quota_gb:
        record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                 api_response=f"API responded ({api_result}) but live read "
                                              f"after write ({live_after}) does not match "
                                              f"requested ({new_quota_gb})")
        return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id,
                "verified_after_quota_gb": live_after}

    # Authoritative read-back, part 2: a single matching read is not proof the write stuck --
    # confirm it survives a short settle delay before trusting it (false-success guard; see
    # CONFIRM_READ_DELAY_SECONDS docstring).
    if confirm_read_delay_seconds > 0:
        sleep_fn(confirm_read_delay_seconds)
        try:
            live_after_confirm = read_live_quota_fn(vpsid)
        except Exception as e:
            record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                     api_response=f"API responded ({api_result}); first read-back "
                                                  f"matched ({live_after}) but the confirmatory "
                                                  f"re-read failed: {e}")
            return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id}
        if live_after_confirm != new_quota_gb:
            record_operation_result(conn, operation_id, "UNKNOWN_NEEDS_VERIFICATION",
                                     api_response=f"API responded ({api_result}); first read-back "
                                                  f"matched ({live_after}) but a confirmatory "
                                                  f"re-read after {confirm_read_delay_seconds}s "
                                                  f"diverged ({live_after_confirm}) -- treating as "
                                                  f"unverified, not APPLIED (false-success guard)")
            return {"status": "UNKNOWN_NEEDS_VERIFICATION", "operation_id": operation_id,
                    "verified_after_quota_gb": live_after_confirm}
        live_after = live_after_confirm

    record_operation_result(conn, operation_id, "APPLIED", api_response=str(api_result),
                             verified_after_quota_gb=live_after)
    return {"status": "APPLIED", "operation_id": operation_id, "verified_after_quota_gb": live_after}


def retry_deferred_renewal(conn, node_id: str, vpsid: int, creds: ApiCredentials,
                            approval_marker_path: str, read_live_quota_fn, http_post=None,
                            max_deferral_hours: float = DEFAULT_MAX_DEFERRAL_HOURS,
                            confirm_read_delay_seconds: float = CONFIRM_READ_DELAY_SECONDS,
                            sleep_fn=time.sleep, now_fn=lambda: datetime.now(timezone.utc)) -> dict:
    """Retries an existing DEFERRED_LOCKED operation for this (node, vpsid) -- never
    recomputes the target quota (that would risk using an already-reset USED value if the
    lock cleared because the VPS's OWN renewal cycle also just turned over), always reuses
    the ORIGINAL requested_new_quota_gb stored at deferral time. Bounded by
    max_deferral_hours: past that age, stops retrying automatically and marks the operation
    DEFERRED_EXPIRED_NEEDS_REVIEW instead -- never retries forever, and never silently drops
    it either. No-op (returns NO_DEFERRED_OPERATION) if nothing is actually deferred.

    SAME structural write-approval gate as apply_renewal -- this function must never be
    reachable just because an operation happens to already exist; WRITE_APPROVED is
    re-checked every single call, exactly like a fresh apply."""
    if not is_write_approved(approval_marker_path):
        raise WriteBlockedError(f"write not approved: {approval_marker_path} does not exist")
    ensure_operations_schema(conn)
    row = find_deferred_operation_for_vpsid(conn, node_id, vpsid)
    if row is None:
        return {"status": "NO_DEFERRED_OPERATION"}
    operation_id, period_key, requested_quota_str, created_at, retry_count = row
    age_hours = (now_fn() - datetime.fromisoformat(created_at)).total_seconds() / 3600.0
    if age_hours > max_deferral_hours:
        record_operation_result(conn, operation_id, "DEFERRED_EXPIRED_NEEDS_REVIEW",
                                 api_response=f"deferred {age_hours:.1f}h, exceeds "
                                              f"max_deferral_hours={max_deferral_hours} -- "
                                              f"stopped automatic retry, needs operator review")
        return {"status": "DEFERRED_EXPIRED_NEEDS_REVIEW", "operation_id": operation_id,
                "period_key": period_key, "age_hours": age_hours}

    # Same class of race as apply_renewal's claim step (see ensure_operations_schema's
    # UNIQUE-index comment): find_deferred_operation_for_vpsid() above is a plain SELECT,
    # so two genuinely concurrent processes could both see the SAME DEFERRED_LOCKED row and
    # both proceed to retry it, both calling the live API for the same operation. The
    # UPDATE's own WHERE clause is the atomic claim here (there is no separate row to
    # insert, unlike apply_renewal, since this reuses an existing operation_id) -- it only
    # succeeds, and only this process proceeds, if the row is STILL in DEFERRED_LOCKED
    # status at the moment this UPDATE actually runs; SQLite serializes concurrent writers
    # to the same row, so exactly one of two racing UPDATEs can match this WHERE clause
    # before the other process's own prior UPDATE (if it reached here first) already moved
    # the status away from DEFERRED_LOCKED.
    # The status flip to 'IN_PROGRESS' (not just the retry_count bump) is what actually
    # closes the race: it is what makes a second, concurrent UPDATE's own
    # `WHERE status='DEFERRED_LOCKED'` fail to match once this one has committed. Without
    # changing status here, status would still read 'DEFERRED_LOCKED' afterward (it is not
    # otherwise touched until _execute_write_and_verify's eventual record_operation_result
    # call), and a second racing UPDATE's WHERE clause would still match -- not a real fix.
    conn.execute("BEGIN")
    cur = conn.execute(
        "UPDATE operations SET retry_count = retry_count + 1, updated_at = ?, status = 'IN_PROGRESS' "
        "WHERE operation_id = ? AND status = 'DEFERRED_LOCKED'",
        (now_fn().isoformat(), operation_id))
    claimed = cur.rowcount == 1
    conn.execute("COMMIT")
    if not claimed:
        return {"status": "BLOCKED_UNRESOLVED_PRIOR_OPERATION", "operation_id": operation_id,
                "period_key": period_key,
                "detail": "lost a concurrent race to retry this deferred operation"}

    new_quota_gb = policy_mod.round_quota_for_virtualizor(Decimal(requested_quota_str))
    result = _execute_write_and_verify(conn, operation_id, vpsid, new_quota_gb, creds, http_post,
                                        confirm_read_delay_seconds, sleep_fn, read_live_quota_fn)
    result["period_key"] = period_key
    result["retry_count"] = retry_count + 1
    return result
