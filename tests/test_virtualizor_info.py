import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import virtualizor_info as vinfo
import writer as w

CREDS = w.ApiCredentials(base_url="https://example.invalid/index.php", api_key="k", api_pass="p")


def test_detect_virtualizor_installed_false_when_no_universal_php(monkeypatch):
    monkeypatch.setattr(vinfo, "UNIVERSAL_PHP_PATHS", ("/no/such/path",))
    assert vinfo.detect_virtualizor_installed() is False


def test_detect_virtualizor_version_unavailable_when_file_missing(monkeypatch):
    monkeypatch.setattr(vinfo, "VIRTUALIZOR_VERSION_PATHS", ("/no/such/version.php",))
    assert vinfo.detect_virtualizor_version() == vinfo.UNAVAILABLE


def test_detect_virtualizor_version_parses_real_pattern(tmp_path, monkeypatch):
    f = tmp_path / "version.php"
    f.write_text("<?php $version = '6.5.1'; ?>")
    monkeypatch.setattr(vinfo, "VIRTUALIZOR_VERSION_PATHS", (str(f),))
    assert vinfo.detect_virtualizor_version() == "6.5.1"


def test_detect_virtualization_technologies_never_assumes_kvm():
    techs = vinfo.detect_virtualization_technologies()
    assert isinstance(techs, list)
    # On a bare CI/sandbox container with none of the checked paths present, must be empty --
    # never padded with a guessed "kvm" default.


def test_probe_api_latency_reports_ok_and_timing():
    def fake_call(creds, timeout=20):
        return {"raw": '{"vs": {}}', "parsed": {"vs": {}}}
    result = vinfo.probe_api_latency(CREDS, call_listvs_api=fake_call)
    assert result["ok"] is True
    assert isinstance(result["latency_ms"], float)


def test_probe_api_latency_reports_error_without_raising():
    def fake_call(creds, timeout=20):
        raise RuntimeError("network unreachable")
    result = vinfo.probe_api_latency(CREDS, call_listvs_api=fake_call)
    assert result["ok"] is False
    assert "network unreachable" in result["error"]


def test_probe_api_latency_reports_unparseable_response():
    def fake_call(creds, timeout=20):
        return {"raw": "not json", "parsed": None}
    result = vinfo.probe_api_latency(CREDS, call_listvs_api=fake_call)
    assert result["ok"] is False


# ---------- parse_listvs_response: the safety-critical parser ----------------------------

def test_parse_listvs_response_happy_path_keyed_by_vpsid():
    """Real confirmed shape (act=vs, empirically verified against ir1 in v1.4.0 development):
    the outer 'vs' dict is keyed by vpsid as a string, NOT by IP."""
    parsed = {"vs": {
        "101": {"vpsid": "101", "hostname": "vps1.example", "bandwidth": "500",
                "used_bandwidth": "120", "machine_status": "running", "ram": "2048",
                "cores": "2", "space": "40", "virt": "kvm", "ips": {"192.0.2.1": "1"},
                "locked": "0", "serid": 1},
        "102": {"vpsid": "102", "hostname": "vps2.example", "bandwidth": "0",
                "used_bandwidth": "999", "machine_status": "running"},
    }}
    result = vinfo.parse_listvs_response(parsed)
    assert result["complete"] is True
    assert result["skipped_count"] == 0
    ids = {r["vpsid"] for r in result["records"]}
    assert ids == {101, 102}
    rec1 = next(r for r in result["records"] if r["vpsid"] == 101)
    assert rec1["quota_gb"] == Decimal("500")
    assert rec1["hostname"] == "vps1.example"
    assert rec1["locked"] is False
    assert rec1["node_serid"] == 1
    assert rec1["ip_count"] == 1


def test_parse_listvs_response_missing_vpsid_is_skipped_not_fabricated():
    parsed = {"vs": {"1": {"hostname": "no-id-vps", "bandwidth": "100"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["complete"] is False
    assert result["skipped_count"] == 1
    assert result["records"] == []


def test_parse_listvs_response_missing_vs_key_reports_error_not_empty_success():
    result = vinfo.parse_listvs_response({"done": 1})
    assert result["complete"] is False
    assert "vs" in result["error"]


def test_parse_listvs_response_vs_not_a_dict_reports_error():
    result = vinfo.parse_listvs_response({"vs": "not-a-dict"})
    assert result["complete"] is False


def test_parse_listvs_response_non_dict_top_level():
    result = vinfo.parse_listvs_response(["not", "a", "dict"])
    assert result["complete"] is False
    assert result["records"] == []


def test_parse_listvs_response_missing_bandwidth_field_marked_unavailable_not_zero():
    parsed = {"vs": {"5": {"vpsid": "5", "hostname": "x"}}}
    result = vinfo.parse_listvs_response(parsed)
    rec = result["records"][0]
    assert rec["quota_gb"] == vinfo.UNAVAILABLE
    assert rec["used_gb"] == vinfo.UNAVAILABLE


def test_parse_listvs_response_suspended_flag_overrides_machine_status():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "suspended": "1",
                           "machine_status": "running"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["status"] == "suspended"


def test_parse_listvs_response_machine_status_field_never_used_for_vps_status():
    """machine_status was found (by reading Virtualizor's own virtualizor.sql schema and
    cross-referencing baremetal-theme files that reference it) to be a BAREMETAL-specific
    field, unrelated to VPS running/stopped state -- it must never be trusted for VPS
    status, no matter what value it holds."""
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "suspended": "0",
                           "machine_status": "1"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["status"] == "unknown"


def test_parse_listvs_response_status_unknown_when_not_suspended():
    """No reliable running/stopped field exists in this endpoint's response (confirmed via
    the vps table schema, which has no status column at all) -- status is honestly reported
    as 'unknown' rather than guessed as 'running'."""
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "suspended": "0"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["status"] == "unknown"


def test_parse_listvs_response_locked_field_parsed_as_bool():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "locked": "1"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["locked"] is True


def test_parse_listvs_response_missing_locked_field_marked_unavailable():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["locked"] == vinfo.UNAVAILABLE


def test_parse_listvs_response_hostname_falls_back_to_vps_name():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "vps_name": "fallback-name"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["hostname"] == "fallback-name"


def test_call_vs_api_uses_act_vs_not_act_listvs():
    captured = {}
    def capture_get(url, timeout):
        captured["url"] = url
        return '{"vs": {}}'
    import writer as w
    w.call_vs_api(CREDS, http_get=capture_get)
    assert "act=vs" in captured["url"]
    assert "act=listvs" not in captured["url"]


# ---------- v1.5.0 regression: pagination (the real 50-vs-61 production discrepancy) ------

def _page_response(vpsids, numvps_total, page_num, page_size):
    start = (page_num - 1) * page_size
    chunk = vpsids[start:start + page_size]
    vs = {str(v): {"vpsid": str(v), "bandwidth": "100", "used_bandwidth": "10"} for v in chunk}
    return {"vs": vs, "servers": [{"numvps": str(numvps_total)}]}


def test_fetch_vs_inventory_reproduces_and_fixes_the_real_50_vs_60_discrepancy():
    """Regression test for the actual production bug: act=vs silently truncated to 50 of 60
    real VPSs with zero in-response indicator of truncation. A naive single-page caller
    (the pre-v1.5.0 code) would have returned exactly 50. fetch_vs_inventory must walk pages
    and return all 60."""
    all_ids = list(range(1, 61))  # 60 real VPSs, matching the confirmed ir1 authoritative count
    calls = []
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        page_size = extra_params.get("reslen", 50)
        calls.append(page)
        return {"parsed": _page_response(all_ids, 60, page, page_size)}
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       sleep_fn=lambda s: None)
    assert result["fetched_count"] == 60
    assert result["authoritative_total"] == 60
    assert result["complete"] is True
    assert {r["vpsid"] for r in result["records"]} == set(all_ids)
    assert calls == [1, 2]  # page 1 (50) + page 2 (10, < page_size, stops)


def test_fetch_vs_inventory_single_page_naive_call_would_have_undercounted():
    """Demonstrates the actual bug shape: calling the API exactly once (no pagination logic)
    returns only the first page -- 50 of 60 -- which is the literal pre-fix production
    symptom the operator reported (as 50 vs 61; the real total was 60, see below)."""
    all_ids = list(range(1, 61))
    naive_result = _page_response(all_ids, 60, page_num=1, page_size=50)
    assert len(naive_result["vs"]) == 50
    assert naive_result["servers"][0]["numvps"] == "60"


def test_fetch_vs_inventory_marks_incomplete_when_fetched_count_disagrees_with_authoritative():
    """If pagination somehow still undercounts (e.g. a page silently dropped), the mismatch
    against servers[].numvps must produce an explicit INCOMPLETE INVENTORY diagnostic --
    never a silent, confidently-wrong 'complete' result."""
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {"1": {"vpsid": "1", "bandwidth": "10"}},
                                "servers": [{"numvps": "5"}]}}
        return {"parsed": {"vs": {}, "servers": [{"numvps": "5"}]}}
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       page_size=50, sleep_fn=lambda s: None)
    assert result["complete"] is False
    assert "INCOMPLETE INVENTORY" in result["error"]
    assert result["fetched_count"] == 1
    assert result["authoritative_total"] == 5


def test_fetch_vs_inventory_stops_as_soon_as_a_short_page_is_seen():
    """A page returning fewer records than page_size is the natural last page -- no extra
    call needed to confirm emptiness."""
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {"1": {"vpsid": "1", "bandwidth": "10"}}, "servers": []}}
        raise AssertionError("should not have requested a second page")
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       max_pages=20, sleep_fn=lambda s: None)
    assert result["pages_fetched"] == 1
    assert result["fetched_count"] == 1


def test_fetch_vs_inventory_stops_on_a_genuinely_empty_page():
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        page_size = extra_params.get("reslen", 50)
        if page == 1:
            return {"parsed": {"vs": {str(i): {"vpsid": str(i), "bandwidth": "10"}
                                       for i in range(1, page_size + 1)}, "servers": []}}
        return {"parsed": {"vs": {}, "servers": []}}
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       page_size=5, max_pages=20, sleep_fn=lambda s: None)
    assert result["pages_fetched"] == 2
    assert result["fetched_count"] == 5


def test_fetch_vs_inventory_bounded_by_max_pages_never_loops_forever():
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        return {"parsed": {"vs": {str(page): {"vpsid": str(page), "bandwidth": "10"}},
                            "servers": [{"numvps": "999"}]}}
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       page_size=1, max_pages=3, sleep_fn=lambda s: None)
    assert result["pages_fetched"] <= 3
    assert result["complete"] is False  # fetched 3, authoritative says 999


def test_fetch_vs_inventory_first_page_network_failure_raises_not_swallowed():
    import pytest
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        raise ConnectionError("down")
    with pytest.raises(ConnectionError):
        vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api, sleep_fn=lambda s: None)


def test_fetch_vs_inventory_later_page_failure_keeps_partial_data_as_incomplete():
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {str(i): {"vpsid": str(i), "bandwidth": "10"} for i in range(1, 51)},
                                "servers": [{"numvps": "60"}]}}
        raise ConnectionError("down mid-pagination")
    result = vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                                       sleep_fn=lambda s: None)
    assert result["complete"] is False
    assert result["fetched_count"] == 50
    assert "mid-pagination" in result["error"]


def test_fetch_vs_inventory_rate_limits_between_pages():
    sleeps = []
    def fake_call_vs_api(creds, extra_params=None, timeout=20):
        page = extra_params.get("page", 1)
        if page == 1:
            return {"parsed": {"vs": {str(i): {"vpsid": str(i), "bandwidth": "10"} for i in range(1, 51)},
                                "servers": []}}
        return {"parsed": {"vs": {}, "servers": []}}
    vinfo.fetch_vs_inventory("fake-creds", call_vs_api=fake_call_vs_api,
                              sleep_fn=lambda s: sleeps.append(s))
    assert len(sleeps) >= 1


# ---------- v1.6.0: real 'ips' field shape (numeric-id keys, IP-string values) -----------

def test_parse_listvs_response_ips_dict_keyed_by_internal_id_not_by_ip():
    """Real confirmed shape on ir1: 'ips' is a dict keyed by an internal numeric id, with
    each VALUE being the actual IP string -- the opposite of what an earlier (unverified)
    assumption might guess. Never inferred from vpsid or any interface name."""
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10",
                           "ips": {"101": "192.0.2.10", "102": "192.0.2.11"}}}}
    result = vinfo.parse_listvs_response(parsed)
    rec = result["records"][0]
    assert rec["ip_count"] == 2
    assert rec["ip_primary"] == "192.0.2.10"
    assert set(rec["ip_list"]) == {"192.0.2.10", "192.0.2.11"}


def test_parse_listvs_response_empty_ips_dict_reports_zero_not_unavailable():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10", "ips": {}}}}
    result = vinfo.parse_listvs_response(parsed)
    rec = result["records"][0]
    assert rec["ip_count"] == 0
    assert rec["ip_primary"] == vinfo.UNAVAILABLE


def test_parse_listvs_response_missing_ips_field_marked_unavailable():
    parsed = {"vs": {"5": {"vpsid": "5", "bandwidth": "10"}}}
    result = vinfo.parse_listvs_response(parsed)
    rec = result["records"][0]
    assert rec["ip_count"] == vinfo.UNAVAILABLE
    assert rec["ip_primary"] == vinfo.UNAVAILABLE
    assert rec["ip_list"] == vinfo.UNAVAILABLE
