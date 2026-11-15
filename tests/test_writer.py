"""Tests for the production writer's safety gates and crash/timeout/concurrency recovery.
NEVER performs a real HTTP call -- http_post and read_live_quota_fn are always mocked.
"""
import json
import os
import sqlite3
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import writer as w
import credentials as credentials_mod


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ops.sqlite3"))
    w.ensure_operations_schema(c)
    yield c
    c.close()


CREDS = w.ApiCredentials(base_url="https://example.invalid/index.php", api_key="k", api_pass="p")


def approved_marker(tmp_path):
    p = tmp_path / "WRITE_APPROVED"
    p.write_text("approved")
    return str(p)


def unapproved_marker(tmp_path):
    return str(tmp_path / "WRITE_APPROVED")  # never created


# 1. Structural gates ---------------------------------------------------------------------

def test_apply_renewal_blocked_without_confirm_flag(conn, tmp_path):
    with pytest.raises(w.WriteBlockedError):
        w.apply_renewal(conn, "n1", 1, "p1", Decimal("100"), CREDS, approved_marker(tmp_path),
                         confirm=False, read_live_quota_fn=lambda v: Decimal("0"))


def test_apply_renewal_blocked_without_approval_marker(conn, tmp_path):
    with pytest.raises(w.WriteBlockedError):
        w.apply_renewal(conn, "n1", 1, "p1", Decimal("100"), CREDS, unapproved_marker(tmp_path),
                         confirm=True, read_live_quota_fn=lambda v: Decimal("0"))


# 2. Happy path (fully mocked) ----------------------------------------------------------------

def test_apply_renewal_success_records_applied(conn, tmp_path):
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("150")])  # before, after
    result = w.apply_renewal(conn, "n1", 7, "p1", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              confirm_read_delay_seconds=0)
    assert result["status"] == "APPLIED"
    row = conn.execute("SELECT status, verified_after_quota_gb FROM operations WHERE operation_id=?",
                        (result["operation_id"],)).fetchone()
    assert row == ("APPLIED", "150")


def test_apply_renewal_accepts_string_quota_without_false_mismatch(conn, tmp_path):
    """Regression test: a real bug was found where a caller passing new_quota_gb as a str
    (e.g. straight out of a JSON-serialized policy preview) caused Decimal("200") == "200"
    to evaluate False, incorrectly marking a genuinely successful write as
    UNKNOWN_NEEDS_VERIFICATION. apply_renewal must coerce to Decimal defensively."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("200")])
    result = w.apply_renewal(conn, "n1", 8, "p1", "200", CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              confirm_read_delay_seconds=0)
    assert result["status"] == "APPLIED"


# 3. Timeout/exception during the API call -> UNKNOWN, never FAILED, never silently retried -----

def test_api_timeout_is_unknown_not_failed(conn, tmp_path):
    marker = approved_marker(tmp_path)

    def boom(url, data, timeout):
        raise TimeoutError("simulated network timeout")

    result = w.apply_renewal(conn, "n1", 7, "p1", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"), http_post=boom)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    row = conn.execute("SELECT status FROM operations WHERE operation_id=?",
                        (result["operation_id"],)).fetchone()
    assert row[0] == "UNKNOWN_NEEDS_VERIFICATION"


def test_post_write_verification_mismatch_is_unknown(conn, tmp_path):
    """API claimed success but the live read-back doesn't match -- never trust the API's
    word alone; this is exactly the kind of partial-success case that must never be assumed
    either way."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("999")])  # after-write read doesn't match requested 150
    result = w.apply_renewal(conn, "n1", 7, "p1", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: '{"done":1}')
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"


# 4. Duplicate/concurrent execution -- never start a second operation while one is unresolved ---

def test_duplicate_run_same_period_is_blocked_while_unresolved(conn, tmp_path):
    marker = approved_marker(tmp_path)

    def boom(url, data, timeout):
        raise TimeoutError("simulated")

    first = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                             read_live_quota_fn=lambda v: Decimal("50"), http_post=boom)
    assert first["status"] == "UNKNOWN_NEEDS_VERIFICATION"

    # A second run (e.g. systemd timer firing again, or a restart) for the SAME period must
    # not attempt a fresh write -- the prior attempt's true outcome is still unknown.
    second = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"),
                              http_post=lambda *a, **k: pytest.fail("must not call the API again"))
    assert second["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    assert second["operation_id"] == first["operation_id"]


def test_duplicate_run_same_period_blocked_after_applied(conn, tmp_path):
    """REGRESSION: after a successful APPLIED, a second apply for the same (node, vpsid,
    period_key) must be blocked. Previously, find_unresolved_operation() excluded APPLIED
    rows from its query, so the duplicate slipped through and called the API again."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("50"), Decimal("150")])

    first = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                             read_live_quota_fn=lambda v: next(reads),
                             http_post=lambda url, data, timeout: '{"done":1}',
                             confirm_read_delay_seconds=0)
    assert first["status"] == "APPLIED"

    second = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"),
                              http_post=lambda *a, **k: pytest.fail("must not call the API again"))
    assert second["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    assert second["operation_id"] == first["operation_id"]
    assert second["prior_status"] == "APPLIED"


def test_duplicate_run_same_period_blocked_after_unknown(conn, tmp_path):
    """After UNKNOWN_NEEDS_VERIFICATION (e.g. API timeout), the same period must still be
    blocked -- the operator must resolve it explicitly, never silently retried."""
    marker = approved_marker(tmp_path)

    def boom(url, data, timeout):
        raise TimeoutError("simulated")

    first = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                             read_live_quota_fn=lambda v: Decimal("50"), http_post=boom)
    assert first["status"] == "UNKNOWN_NEEDS_VERIFICATION"

    second = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"),
                              http_post=lambda *a, **k: pytest.fail("must not call the API again"))
    assert second["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    assert second["operation_id"] == first["operation_id"]
    assert second["prior_status"] == "UNKNOWN_NEEDS_VERIFICATION"

    # A second run (e.g. systemd timer firing again, or a restart) for the SAME period must
    # not attempt a fresh write -- the prior attempt's true outcome is still unknown.
    second = w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"),
                              http_post=lambda *a, **k: pytest.fail("must not call the API again"))
    assert second["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    assert second["operation_id"] == first["operation_id"]


def test_different_period_same_vps_is_not_blocked(conn, tmp_path):
    """Cooldown is disabled here on purpose -- this test isolates the same-period-duplicate
    check only. See test_rapid_second_write_same_vps_blocked_by_cooldown for the cooldown
    behavior itself."""
    marker = approved_marker(tmp_path)

    def boom(url, data, timeout):
        raise TimeoutError("simulated")

    w.apply_renewal(conn, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                     read_live_quota_fn=lambda v: Decimal("50"), http_post=boom)

    reads = iter([Decimal("10"), Decimal("110")])
    second = w.apply_renewal(conn, "n1", 7, "period-B", Decimal("110"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: "ok",
                              write_cooldown_seconds=0, confirm_read_delay_seconds=0)
    assert second["status"] == "APPLIED"  # a different renewal period is independent


def test_restart_sees_unknown_operation_and_still_blocks(tmp_path):
    """Simulates a process crash: operation recorded as UNKNOWN in one connection/process,
    then a FRESH connection (as if restarted) must still see and respect it."""
    db_path = str(tmp_path / "restart.sqlite3")
    marker = approved_marker(tmp_path)

    conn1 = sqlite3.connect(db_path)
    w.ensure_operations_schema(conn1)

    def boom(url, data, timeout):
        raise TimeoutError("simulated")

    w.apply_renewal(conn1, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                     read_live_quota_fn=lambda v: Decimal("50"), http_post=boom)
    conn1.close()

    conn2 = sqlite3.connect(db_path)  # fresh "process"
    result = w.apply_renewal(conn2, "n1", 7, "period-A", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"),
                              http_post=lambda *a, **k: pytest.fail("must not call the API again"))
    assert result["status"] == "BLOCKED_UNRESOLVED_PRIOR_OPERATION"
    conn2.close()


# 5. Pre-write read failure (e.g. SSH/local DB unreachable) also -> UNKNOWN, no API call --------

def test_pre_write_read_failure_never_calls_the_api(conn, tmp_path):
    marker = approved_marker(tmp_path)

    def fail_read(vpsid):
        raise ConnectionError("cannot reach host")

    result = w.apply_renewal(conn, "n1", 7, "p1", Decimal("150"), CREDS, marker, confirm=True,
                              read_live_quota_fn=fail_read,
                              http_post=lambda *a, **k: pytest.fail("must not reach the API"))
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"


# 6. POST body shape must match the real, already-verified-working manual script exactly -------

def test_managevps_post_body_includes_required_theme_edit_and_editvps_fields():
    """Regression test: a real production script (/root/virt-api-test.py, read-only
    reviewed) that successfully applies quota changes POSTs `theme_edit=1` and `editvps=1`
    alongside `vpsid`/`bandwidth`. An earlier version of call_managevps_api omitted both --
    Virtualizor may silently ignore an edit request missing these fields. Captured here via
    the injectable http_post so this is enforced by a real test, not just a docstring."""
    captured = {}

    def capture_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return '{"done": 1}'

    w.call_managevps_api(CREDS, 42, "200", http_post=capture_post)
    body_params = dict(pair.split("=") for pair in captured["data"].split("&"))
    assert body_params.get("theme_edit") == "1"
    assert body_params.get("editvps") == "1"
    assert body_params.get("bandwidth") == "200"
    assert body_params.get("vpsid") == "42"


def test_call_managevps_api_parses_realistic_success_json():
    result = w.call_managevps_api(CREDS, 42, "200",
                                   http_post=lambda *a, **k: '{"done": 1, "vps": {"vpsid": "42"}}')
    assert result["parsed"]["done"] == 1


def test_call_managevps_api_parses_realistic_error_json():
    result = w.call_managevps_api(CREDS, 42, "200",
                                   http_post=lambda *a, **k: '{"done": 0, "error": "VPS does not exist"}')
    assert result["parsed"]["done"] == 0
    assert "does not exist" in result["parsed"]["error"]


def test_call_managevps_api_handles_non_json_response_gracefully():
    result = w.call_managevps_api(CREDS, 42, "200",
                                   http_post=lambda *a, **k: "<html>500 Internal Server Error</html>")
    assert result["parsed"] is None
    assert "500" in result["raw"]


# 7. probe_api_reachability / call_listvs_api: validate credentials with ZERO mutation params --

def test_listvs_request_has_no_vpsid_and_no_mutation_params():
    """The probe request itself must carry no vpsid, no editvps, no theme_edit, and no POST
    body at all -- safety here comes from the request shape, not from assuming Virtualizor
    will reject an edit on a nonexistent VPS."""
    captured = {}

    def capture_get(url, timeout):
        captured["url"] = url
        return '{"vs": {}}'

    w.call_listvs_api(CREDS, http_get=capture_get)
    assert "act=listvs" in captured["url"]
    assert "vpsid" not in captured["url"]
    assert "editvps" not in captured["url"]
    assert "theme_edit" not in captured["url"]


def detailed(body, status=200, content_type="application/json"):
    return lambda url, timeout: {"status": status, "content_type": content_type, "body": body}


def test_probe_never_calls_managevps():
    """probe_api_reachability must never go through the POST-based, mutation-shaped
    managevps call under any circumstances."""
    def fail_if_called(*a, **k):
        pytest.fail("probe_api_reachability must never call call_managevps_api")

    import unittest.mock as mock
    with mock.patch.object(w, "call_managevps_api", fail_if_called):
        w.probe_api_reachability(CREDS, http_get_detailed=detailed('{"vs": {}}'))


def test_probe_detects_credentials_likely_valid_via_nonempty_object():
    """Deliberately NOT dependent on a specific key name like `vs` -- any non-empty,
    error-free JSON object is treated as an authenticated-looking response, since the real
    production response shape was found to differ from an earlier `vs`-key assumption."""
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed('{"vs": {"42": {}, "43": {}}}'))
    assert result["confidence"] == "CREDENTIALS_LIKELY_VALID"


def test_probe_detects_credentials_likely_valid_via_different_key_name():
    """Regression test for the real incident: a response shaped differently than `{"vs": ...}`
    (here, an arbitrary other key) must still be recognized as likely-valid, since the
    classification no longer depends on guessing the exact field name."""
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed('{"virtualservers": {"1": {}}}'))
    assert result["confidence"] == "CREDENTIALS_LIKELY_VALID"


def test_probe_detects_invalid_credentials_via_error_string():
    result = w.probe_api_reachability(
        CREDS, http_get_detailed=detailed('{"error": "Invalid API Key"}'))
    assert result["confidence"] == "CREDENTIALS_LIKELY_INVALID"


def test_probe_detects_invalid_credentials_via_http_status():
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed("", status=401))
    assert result["confidence"] == "CREDENTIALS_LIKELY_INVALID"
    result403 = w.probe_api_reachability(CREDS, http_get_detailed=detailed("", status=403))
    assert result403["confidence"] == "CREDENTIALS_LIKELY_INVALID"


def test_probe_unparseable_response_is_unknown_not_a_false_positive():
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed("not json at all"))
    assert result["confidence"] == "UNKNOWN"


def test_probe_empty_object_is_unknown_not_valid():
    """An empty `{}` has no keys at all -- not enough signal to call it 'likely valid'."""
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed("{}"))
    assert result["confidence"] == "UNKNOWN"


# 8. SECURITY REGRESSION: the exact incident -- malformed URL / network error must never
#    expose adminapikey or adminapipass in any exception, log, or returned string. -----------

SECRET_CREDS = w.ApiCredentials(base_url="https://host/index.php",
                                 api_key="SECRET-KEY-VALUE", api_pass="SECRET-PASS-VALUE")


def test_network_exception_message_never_contains_raw_secrets():
    """Reproduces the real incident: the injected http_get raises a ValueError whose message
    (as real urllib errors do) contains the full URL, including the query-string secrets.
    call_listvs_api must never let that raw message escape."""
    def leaky_get(url, timeout):
        raise ValueError(f"unknown url type: {url}")  # url contains adminapikey=/adminapipass=

    with pytest.raises(w.SanitizedNetworkError) as exc_info:
        w.call_listvs_api(SECRET_CREDS, http_get=leaky_get)
    message = str(exc_info.value)
    assert "SECRET-KEY-VALUE" not in message
    assert "SECRET-PASS-VALUE" not in message
    assert "adminapikey=***REDACTED***" in message or "adminapikey" not in message


def test_network_exception_during_managevps_call_also_sanitized():
    def leaky_post(url, data, timeout):
        raise ConnectionError(f"could not connect: {url}")

    with pytest.raises(w.SanitizedNetworkError) as exc_info:
        w.call_managevps_api(SECRET_CREDS, 42, "100", http_post=leaky_post)
    message = str(exc_info.value)
    assert "SECRET-KEY-VALUE" not in message
    assert "SECRET-PASS-VALUE" not in message


def test_probe_api_reachability_never_raises_and_never_leaks_on_network_failure():
    """The exact call path the real production incident went through: probe_api_reachability
    must swallow the sanitized error and return a dict, never propagate anything -- and
    whatever it returns must not contain the secrets either."""
    def leaky_get(url, timeout):
        raise ValueError(f"malformed url, full target was: {url}")

    result = w.probe_api_reachability(SECRET_CREDS, http_get_detailed=leaky_get)
    assert result["confidence"] == "ERROR"
    assert "SECRET-KEY-VALUE" not in str(result)
    assert "SECRET-PASS-VALUE" not in str(result)


def test_probe_with_malformed_base_url_never_leaks_and_never_raises():
    bad_creds = w.ApiCredentials(base_url="not-a-valid-url", api_key="SECRET-KEY-VALUE",
                                  api_pass="SECRET-PASS-VALUE")
    result = w.probe_api_reachability(bad_creds)
    assert result["confidence"] == "ERROR"
    assert "SECRET-KEY-VALUE" not in str(result)
    assert "SECRET-PASS-VALUE" not in str(result)


def test_call_listvs_api_with_malformed_base_url_raises_sanitized_not_raw():
    with pytest.raises(credentials_mod.InvalidBaseUrlError) as exc_info:
        w.call_listvs_api(w.ApiCredentials(base_url="bad url with space", api_key="SECRET-KEY-VALUE",
                                             api_pass="SECRET-PASS-VALUE"))
    assert "SECRET-KEY-VALUE" not in str(exc_info.value)
    assert "SECRET-PASS-VALUE" not in str(exc_info.value)


def test_redact_url_patterns_strips_adminapikey_and_adminapipass_generically():
    text = "GET https://host/index.php?act=listvs&adminapikey=abc123&adminapipass=xyz789 failed"
    redacted = w.redact_url_patterns(text)
    assert "abc123" not in redacted
    assert "xyz789" not in redacted
    assert "adminapikey=***REDACTED***" in redacted
    assert "adminapipass=***REDACTED***" in redacted


# 9. Quota DECREASES, write cooldown, and false-success responses -------------------------
# Added after a real incident on ir1 (2026-11-14): a decrease (101->100 GB) issued ~8s after
# an increase on the same vpsid never took effect, even though the API call raised no
# exception and a byte-identical payload worked when issued by Virtualizor's own admin panel
# with more spacing between requests. See WRITE_COOLDOWN_SECONDS / CONFIRM_READ_DELAY_SECONDS
# docstrings in writer.py.

def test_decrease_quota_applied_correctly_when_reads_confirm(conn, tmp_path):
    """A decrease is not structurally different from an increase -- it must still reach
    APPLIED when both the immediate and the confirmatory read-back agree."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("101"), Decimal("100"), Decimal("100")])  # before, after, confirm
    result = w.apply_renewal(conn, "n1", 131, "restore", Decimal("100"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              write_cooldown_seconds=0, sleep_fn=lambda s: None)
    assert result["status"] == "APPLIED"
    assert result["verified_after_quota_gb"] == Decimal("100")


def test_decrease_quota_write_silently_fails_is_unknown_not_applied(conn, tmp_path):
    """Reproduces the real ir1 incident: the API call raises nothing and returns a
    success-shaped body, but the live quota never actually changes. Must never be reported as
    APPLIED, and must never be retried automatically by this function itself."""
    marker = approved_marker(tmp_path)
    # Quota stays at 101 on every single read, across pre-write, post-write, AND any
    # confirmatory read -- exactly what was observed live (including a re-read seconds later).
    result = w.apply_renewal(conn, "n1", 131, "restore", Decimal("100"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: Decimal("101"),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              write_cooldown_seconds=0, sleep_fn=lambda s: None)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    assert result["verified_after_quota_gb"] == Decimal("101")
    row = conn.execute("SELECT status FROM operations WHERE operation_id=?",
                        (result["operation_id"],)).fetchone()
    assert row[0] == "UNKNOWN_NEEDS_VERIFICATION"


def test_false_success_reverts_on_confirmatory_reread_is_unknown_not_applied(conn, tmp_path):
    """The symmetric failure mode to the one above: the FIRST read-after happens to match the
    requested value (e.g. a race against another in-flight write, or a stale cache that
    briefly agrees), but a confirmatory re-read after the settle delay shows it reverted. Must
    never be reported as APPLIED on the strength of a single matching read."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("101"), Decimal("100"), Decimal("101")])  # before, after, confirm(reverted)
    slept = []
    result = w.apply_renewal(conn, "n1", 131, "restore", Decimal("100"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              write_cooldown_seconds=0, sleep_fn=lambda s: slept.append(s))
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    assert result["verified_after_quota_gb"] == Decimal("101")
    assert slept == [w.CONFIRM_READ_DELAY_SECONDS]  # the settle delay was actually used


def test_api_explicit_error_response_never_applied_even_if_live_read_happens_to_match(conn, tmp_path):
    """Response validation: an explicit `error` field from Virtualizor is trusted as a failure
    signal on its own -- even in the edge case where the live read coincidentally already
    equals the requested value (e.g. the VPS never changed at all), this must not be reported
    as APPLIED, since the API itself said the request was rejected."""
    marker = approved_marker(tmp_path)
    result = w.apply_renewal(
        conn, "n1", 131, "restore", Decimal("100"), CREDS, marker, confirm=True,
        read_live_quota_fn=lambda v: Decimal("100"),
        http_post=lambda url, data, timeout: '{"done": 0, "error": "VPS does not exist"}',
        write_cooldown_seconds=0)
    assert result["status"] == "API_REPORTED_ERROR"
    row = conn.execute("SELECT status FROM operations WHERE operation_id=?",
                        (result["operation_id"],)).fetchone()
    assert row[0] == "API_REPORTED_ERROR"


def test_rapid_second_write_same_vps_blocked_by_cooldown(conn, tmp_path):
    """The core fix for the ir1 incident: a second write to the SAME vpsid under a DIFFERENT
    period_key, issued too soon after the first, must be blocked before any API call -- rather
    than risk tripping whatever caused the real decrease to silently fail."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("100"), Decimal("101")])
    first = w.apply_renewal(conn, "n1", 131, "increase", Decimal("101"), CREDS, marker,
                             confirm=True, read_live_quota_fn=lambda v: next(reads),
                             http_post=lambda url, data, timeout: '{"done":1}',
                             confirm_read_delay_seconds=0)
    assert first["status"] == "APPLIED"

    second = w.apply_renewal(
        conn, "n1", 131, "restore", Decimal("100"), CREDS, marker, confirm=True,
        read_live_quota_fn=lambda v: pytest.fail("must not read live quota during a cooldown block"),
        http_post=lambda *a, **k: pytest.fail("must not call the API during a cooldown block"))
    assert second["status"] == "BLOCKED_WRITE_COOLDOWN"
    assert second["operation_id"] == first["operation_id"]
    assert second["seconds_remaining"] > 0


def test_cooldown_does_not_apply_once_elapsed(conn, tmp_path):
    """The cooldown is a temporary spacing guard, not a permanent block -- once enough wall
    time has passed (simulated here via now_fn), a different period for the same vpsid must
    proceed normally."""
    marker = approved_marker(tmp_path)
    reads = iter([Decimal("100"), Decimal("101")])
    first = w.apply_renewal(conn, "n1", 131, "increase", Decimal("101"), CREDS, marker,
                             confirm=True, read_live_quota_fn=lambda v: next(reads),
                             http_post=lambda url, data, timeout: '{"done":1}',
                             confirm_read_delay_seconds=0)
    assert first["status"] == "APPLIED"

    from datetime import datetime, timedelta, timezone
    later = datetime.now(timezone.utc) + timedelta(seconds=w.WRITE_COOLDOWN_SECONDS + 1)
    reads2 = iter([Decimal("101"), Decimal("100")])
    second = w.apply_renewal(conn, "n1", 131, "restore", Decimal("100"), CREDS, marker,
                              confirm=True, read_live_quota_fn=lambda v: next(reads2),
                              http_post=lambda url, data, timeout: '{"done":1}',
                              confirm_read_delay_seconds=0, now_fn=lambda: later)
    assert second["status"] == "APPLIED"


def test_apply_renewal_api_exception_recorded_in_operations_table_is_sanitized(conn, tmp_path):
    """The operations table is a durable audit log an operator will read later -- it must
    never contain a real secret even if the underlying API call failed with a leaky message."""
    marker = approved_marker(tmp_path)

    def leaky_post(url, data, timeout):
        raise ValueError(f"connection refused: {url}")

    secret_creds = w.ApiCredentials(base_url="https://host/index.php",
                                     api_key="SECRET-KEY-VALUE", api_pass="SECRET-PASS-VALUE")
    result = w.apply_renewal(conn, "n1", 7, "p1", Decimal("150"), secret_creds, marker, confirm=True,
                              read_live_quota_fn=lambda v: Decimal("50"), http_post=leaky_post)
    assert result["status"] == "UNKNOWN_NEEDS_VERIFICATION"
    row = conn.execute("SELECT api_response FROM operations WHERE operation_id=?",
                        (result["operation_id"],)).fetchone()
    assert "SECRET-KEY-VALUE" not in row[0]
    assert "SECRET-PASS-VALUE" not in row[0]


# 9. sanitize_json_structure: shape only, never values -------------------------------------

def test_sanitize_structure_never_includes_actual_values():
    data = {"vs": {"42": {"ip": "203.0.113.5", "bandwidth": 17473}}, "done": 1}
    structure = w.sanitize_json_structure(data)
    text = json.dumps(structure)
    assert "203.0.113.5" not in text
    assert "17473" not in text
    assert "42" not in text  # dict KEYS one level down from a nested object are also
    # structural metadata only at depth, but the literal vpsid "42" used as a key name here
    # must not leak either once nested past the top level's own _keys listing boundary --
    # covered precisely by the depth-limited key disclosure below.


def test_sanitize_structure_reports_top_level_key_names_only():
    structure = w.sanitize_json_structure({"vs": {}, "done": 1})
    assert set(structure["_keys"].keys()) == {"vs", "done"}
    assert structure["_key_count"] == 2


def test_sanitize_structure_describes_types_correctly():
    structure = w.sanitize_json_structure({"a": "text", "b": 1, "c": True, "d": None, "e": [1, 2, 3]})
    assert structure["_keys"]["a"]["_type"] == "str"
    assert structure["_keys"]["b"]["_type"] == "int"
    assert structure["_keys"]["c"]["_type"] == "boolean"
    assert structure["_keys"]["d"]["_type"] == "null"
    assert structure["_keys"]["e"]["_type"] == "array"
    assert structure["_keys"]["e"]["_length"] == 3


def test_sanitize_structure_truncates_beyond_max_depth():
    deeply_nested = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
    structure = w.sanitize_json_structure(deeply_nested, max_depth=2)
    # depth 0 = top dict (keys shown), depth 1 = "a"'s dict (keys redacted, sample shown),
    # depth 2 = sample's dict -> truncated
    assert structure["_keys"]["a"]["_sample_value"].get("_truncated") is True


def test_sanitize_structure_never_discloses_nested_dict_key_names():
    """The exact gap found and fixed: a nested dict keyed by something customer-identifying
    (vpsid, IP, hostname -- Virtualizor responses are known to use IP-as-dict-key in some
    structures) must never have its key names disclosed below the top level."""
    data = {"vs": {"87.107.168.150": {"bandwidth": 1000}, "87.107.168.185": {"bandwidth": 2000}}}
    structure = w.sanitize_json_structure(data)
    text = json.dumps(structure)
    assert "87.107.168.150" not in text
    assert "87.107.168.185" not in text
    assert structure["_keys"]["vs"]["_keys_redacted"] is True
    assert structure["_keys"]["vs"]["_key_count"] == 2


# 10. diagnose_listvs_response: strictly read-only, sanitized diagnostic -------------------

def test_diagnose_reports_status_content_type_and_keys():
    fetch = detailed('{"vs": {"1": {}}, "done": 1}', status=200, content_type="application/json")
    diag = w.diagnose_listvs_response(CREDS, http_get_detailed=fetch)
    assert diag["status"] == 200
    assert diag["content_type"] == "application/json"
    assert diag["top_level_keys"] == ["done", "vs"]
    assert diag["error"] is None


def test_diagnose_never_includes_raw_body_or_values():
    fetch = detailed('{"vs": {"42": {"ip": "203.0.113.5"}}}')
    diag = w.diagnose_listvs_response(CREDS, http_get_detailed=fetch)
    text = json.dumps(diag)
    assert "203.0.113.5" not in text
    assert "raw" not in diag


def test_diagnose_handles_unparseable_body_safely():
    diag = w.diagnose_listvs_response(CREDS, http_get_detailed=detailed("<html>error</html>"))
    assert diag["structure"]["_type"] == "unparseable"
    assert diag["top_level_keys"] is None


def test_diagnose_malformed_base_url_returns_sanitized_error_not_raise():
    bad_creds = w.ApiCredentials(base_url="not a url", api_key="SECRET-KEY-VALUE", api_pass="SECRET-PASS-VALUE")
    diag = w.diagnose_listvs_response(bad_creds)
    assert diag["error"] is not None
    assert "SECRET-KEY-VALUE" not in diag["error"]


def test_diagnose_network_failure_returns_sanitized_error_not_raise():
    def leaky(url, timeout):
        raise ValueError(f"connection failed for {url}")

    diag = w.diagnose_listvs_response(SECRET_CREDS, http_get_detailed=leaky)
    assert diag["error"] is not None
    assert "SECRET-KEY-VALUE" not in diag["error"]
    assert "SECRET-PASS-VALUE" not in diag["error"]


# 11. REAL production response shape (confirmed via diagnose-api, 2026-11-14) ------------------
# Top-level keys: servers (array, len 1), stats (object, 62 entries), time_taken, timenow,
# title. HTTP 200, Content-Type: text/html; charset=UTF-8 (not application/json -- Virtualizor
# serves JSON with an HTML content-type header; this must not affect classification).

def _real_listvs_body():
    return json.dumps({
        "servers": [{"id": 1}],
        "stats": {str(i): {} for i in range(62)},
        "time_taken": "0.5",
        "timenow": 1234567890,
        "title": "Virtualizor",
    })


def test_probe_recognizes_real_production_response_shape():
    fetch = detailed(_real_listvs_body(), status=200, content_type="text/html; charset=UTF-8")
    result = w.probe_api_reachability(CREDS, http_get_detailed=fetch)
    assert result["confidence"] == "CREDENTIALS_LIKELY_VALID"


def test_probe_real_shape_classification_ignores_content_type():
    """Same real body, three different Content-Type headers -- classification must not
    depend on it, since the real server sends text/html for a JSON payload."""
    for ct in ("text/html; charset=UTF-8", "application/json", "", None):
        fetch = detailed(_real_listvs_body(), status=200, content_type=ct)
        result = w.probe_api_reachability(CREDS, http_get_detailed=fetch)
        assert result["confidence"] == "CREDENTIALS_LIKELY_VALID", f"failed for content_type={ct!r}"


def test_diagnose_reports_real_shape_without_leaking_stats_entries():
    fetch = detailed(_real_listvs_body(), status=200, content_type="text/html; charset=UTF-8")
    diag = w.diagnose_listvs_response(CREDS, http_get_detailed=fetch)
    assert diag["top_level_keys"] == ["servers", "stats", "time_taken", "timenow", "title"]
    assert diag["structure"]["_keys"]["stats"]["_key_count"] == 62
    assert diag["structure"]["_keys"]["stats"].get("_keys_redacted") is True  # the 62 entry
    # keys (vpsids in the real data) are never disclosed, matching the nested-key-redaction fix


def test_invalid_credentials_not_misclassified_despite_similar_object_shape():
    """A response with the SAME general shape (multi-key object) as the real success case,
    but carrying an explicit string `error` field, must still be classified invalid -- the
    generalized 'any non-empty object' rule must not swallow this."""
    body = json.dumps({"error": "Invalid API Key", "time_taken": "0.1", "timenow": 123})
    result = w.probe_api_reachability(CREDS, http_get_detailed=detailed(body))
    assert result["confidence"] == "CREDENTIALS_LIKELY_INVALID"


def test_invalid_credentials_via_401_not_misclassified_even_with_json_looking_body():
    """An HTTP 401 must win over body content, even if the body happens to parse as a
    plausible-looking JSON object (e.g. a generic web-server error page's JSON wrapper)."""
    fetch = detailed('{"servers": [], "stats": {}}', status=401)
    result = w.probe_api_reachability(CREDS, http_get_detailed=fetch)
    assert result["confidence"] == "CREDENTIALS_LIKELY_INVALID"
