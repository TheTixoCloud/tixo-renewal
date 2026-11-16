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

def test_parse_listvs_response_happy_path_keyed_by_ip():
    parsed = {"vs": {
        "87.107.168.150": {"vpsid": "101", "hostname": "vps1.example", "bandwidth": "500",
                            "used_bandwidth": "120", "status": "running", "ram": "2048",
                            "cores": "2", "space": "40", "virt": "kvm", "ips": ["87.107.168.150"]},
        "87.107.168.185": {"vpsid": "102", "hostname": "vps2.example", "bandwidth": "0",
                            "used_bandwidth": "999", "status": "running"},
    }}
    result = vinfo.parse_listvs_response(parsed)
    assert result["complete"] is True
    assert result["skipped_count"] == 0
    ids = {r["vpsid"] for r in result["records"]}
    assert ids == {101, 102}
    rec1 = next(r for r in result["records"] if r["vpsid"] == 101)
    assert rec1["quota_gb"] == Decimal("500")
    assert rec1["hostname"] == "vps1.example"


def test_parse_listvs_response_missing_vpsid_is_skipped_not_fabricated():
    parsed = {"vs": {"1.2.3.4": {"hostname": "no-id-vps", "bandwidth": "100"}}}
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
    parsed = {"vs": {"1.2.3.4": {"vpsid": "5", "hostname": "x"}}}
    result = vinfo.parse_listvs_response(parsed)
    rec = result["records"][0]
    assert rec["quota_gb"] == vinfo.UNAVAILABLE
    assert rec["used_gb"] == vinfo.UNAVAILABLE


def test_parse_listvs_response_suspended_flag_maps_to_status():
    parsed = {"vs": {"1.2.3.4": {"vpsid": "5", "bandwidth": "10", "suspended": "1"}}}
    result = vinfo.parse_listvs_response(parsed)
    assert result["records"][0]["status"] == "suspended"
