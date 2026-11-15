# Test Coverage

Run everything: `python3 -m pytest tests/ -v`

## Scenario coverage (by test file)

| Scenario | Covered in | Notes |
|---|---|---|
| Monthly renewal formula, zero grant, carryover cap/disabled | `test_policy.py`, `test_policy_integration.py` | |
| Exactly-once renewal per (vpsid, created_epoch, period) | `test_engine.py` (duplicate-period tests) | |
| VPS deletion / recreation, ID reuse | `test_engine.py`, `test_shadow_monitor.py` | identity never merges across a reused vpsid |
| Billing-date change (explicit anchor edit) | `test_engine.py::test_billing_anchor_change_reanchors_future_eligibility_checks` | |
| February / month-end / leap year | `test_engine.py` (anniversary tests) | Jan 31 → Apr 30, Feb 29 → Feb 28 |
| Clock drift | `test_engine.py` (clock drift tests) | small drift tolerated, large drift still rejected |
| Concurrent execution (same SQLite file) | `test_shadow_monitor.py::test_concurrent_polls_to_same_db_file_do_not_lose_data` | |
| Native-reset race (quota changes unexpectedly) | `test_shadow_monitor.py::test_concurrent_quota_change_during_usage_drop_is_frozen_not_guessed` | |
| API failure / timeout / partial write | `test_writer.py` | never silently assumes success OR failure |
| Duplicate / restart-safe apply | `test_writer.py` (restart, duplicate-run tests) | |
| Top-up reconciliation, no double credit | `test_engine.py` (receipt-matching tests) | |
| Historical already-fixed resets never re-credited | `test_engine.py::test_historically_fixed_reset_is_excluded_*` | |
| Secure credential storage (0600, refuses insecure perms) | `test_credentials.py` | |
| Policy precedence (global/host/VPS), zero-vs-unset | `test_policy.py` | |
| No retroactive grant for pre-existing VPS | `test_policy_integration.py` | |
| Full install → dry-run → policy → uninstall cycle | `test_renewal_system.py` + manual `deploy/build_package.sh` simulation | caught 2 real bugs during development (systemd env vars, snapshot-pairing in apply-due) not visible from unit tests alone |

## What is NOT covered (explicit gaps)

- No test runs against a real Virtualizor API — `writer.py`'s HTTP call is always mocked.
- No test has observed a real, naturally-occurring Virtualizor renewal event.
- Deep time-series analysis of raw per-day traffic counters for a hidden reset signature was
  not attempted.
