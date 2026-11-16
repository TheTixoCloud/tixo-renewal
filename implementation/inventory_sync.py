"""Orchestrates one read-only inventory refresh: non-overlapping lock, rate-limited/bounded-
retry API call, parsing, and ingestion into inventory.py's separate snapshot database.

This is deliberately a THIN, separate module from both inventory.py (pure data) and
writer.py/renewal_cli.py (the renewal engine) -- a dashboard refresh must never share a lock,
a rate limiter, or a retry budget with the renewal apply path, so a slow/misbehaving
Virtualizor API can never make the dashboard starve or interfere with actual renewals.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

FRESH, REFRESHING, STALE, ERROR, PARTIAL = "FRESH", "REFRESHING", "STALE", "ERROR", "PARTIAL"

DEFAULT_REFRESH_INTERVAL_MIN = 10
DEFAULT_API_RETRY_COUNT = 2
DEFAULT_API_RETRY_BASE_DELAY = 1.0


class InventoryLockHeld(Exception):
    pass


def _lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, "inventory-sync.lock")


def acquire_inventory_lock(state_dir: str):
    """Non-blocking flock, mirroring the same non-overlap convention already used by the
    renewal timers (just in-process here rather than shell `flock`, since this is invoked
    directly from Python -- the menu's manual refresh and a future scheduled refresh must
    never run concurrently and corrupt/duplicate a snapshot)."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fh = open(_lock_path(state_dir), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EACCES, errno.EAGAIN):
            raise InventoryLockHeld("another inventory sync is already running")
        raise
    return fh


def release_inventory_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _sync_state_path(state_dir: str) -> str:
    return os.path.join(state_dir, "inventory-sync-state.json")


def load_sync_state(state_dir: str) -> dict:
    path = _sync_state_path(state_dir)
    if not os.path.exists(path):
        return {"last_attempt_status": None, "last_attempt_at": None, "last_error": None}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"last_attempt_status": None, "last_attempt_at": None, "last_error": None}


def save_sync_state(state_dir: str, status: str, error: str = None) -> None:
    path = _sync_state_path(state_dir)
    payload = {"last_attempt_status": status,
               "last_attempt_at": datetime.now(timezone.utc).isoformat(),
               "last_error": error}
    tmp = path + ".tmp"
    Path(tmp).write_text(json.dumps(payload))
    os.replace(tmp, path)


def classify_freshness(last_sync_captured_at: str, last_attempt_status: str,
                        refresh_interval_minutes: int, lock_held: bool = False,
                        now: datetime = None) -> str:
    """One of FRESH/REFRESHING/STALE/ERROR/PARTIAL -- never silently collapsed to a generic
    "ok"/"not ok", per the requirement to distinguish these explicitly in the UI."""
    if lock_held:
        return REFRESHING
    if last_attempt_status == "REJECTED_INCOMPLETE":
        return PARTIAL
    if last_attempt_status == "ERROR":
        return ERROR
    if not last_sync_captured_at:
        return STALE
    now = now or datetime.now(timezone.utc)
    try:
        captured = datetime.fromisoformat(last_sync_captured_at)
    except ValueError:
        return STALE
    age_minutes = (now - captured).total_seconds() / 60
    return FRESH if age_minutes <= max(refresh_interval_minutes * 2, 1) else STALE


def _call_with_bounded_retry(fn, max_retries=DEFAULT_API_RETRY_COUNT,
                              base_delay=DEFAULT_API_RETRY_BASE_DELAY, sleep_fn=time.sleep):
    """At most `max_retries` additional attempts after the first, with linear backoff. Never
    retries forever -- a slow/down Virtualizor API must eventually surface as ERROR, not hang
    the dashboard indefinitely."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- any transport failure is retried, then surfaced
            last_exc = e
            if attempt < max_retries:
                sleep_fn(base_delay * (attempt + 1))
    raise last_exc


def run_inventory_sync(conn, node_id: str, creds, state_dir: str, call_listvs_api=None,
                        parse_listvs_response=None, max_retries=DEFAULT_API_RETRY_COUNT,
                        sleep_fn=time.sleep, page_size: int = None, max_pages: int = None) -> dict:
    """The full read-only refresh: acquire lock -> bounded-retry, FULLY PAGINATED fetch
    (see virtualizor_info.fetch_vs_inventory -- act=vs silently truncates at a default page
    size with no in-response indicator) -> ingest -> release lock -> persist attempt state.
    Never raises WriteBlockedError or touches WRITE_APPROVED -- this path performs no write
    of any kind to Virtualizor, ever.

    `call_listvs_api` (kept under its historical parameter name for test-injection
    compatibility) is passed through as fetch_vs_inventory's `call_vs_api` -- the real
    default is writer.call_vs_api (`act=vs`), not the similarly-named writer.call_listvs_api
    (`act=listvs`, which lists physical servers/nodes, not VPSs, on the installation this was
    verified against -- see writer.call_listvs_api's docstring)."""
    import inventory as inv
    import virtualizor_info as vinfo
    call_vs_api = call_listvs_api or __import__("writer").call_vs_api
    fetch_vs_inventory = parse_listvs_response or vinfo.fetch_vs_inventory
    kwargs = {}
    if page_size is not None:
        kwargs["page_size"] = page_size
    if max_pages is not None:
        kwargs["max_pages"] = max_pages

    try:
        lock = acquire_inventory_lock(state_dir)
    except InventoryLockHeld as e:
        return {"status": "ALREADY_RUNNING", "error": str(e)}

    try:
        try:
            parse_result = _call_with_bounded_retry(
                lambda: fetch_vs_inventory(creds, call_vs_api=call_vs_api, sleep_fn=sleep_fn, **kwargs),
                max_retries=max_retries, sleep_fn=sleep_fn)
        except Exception as e:
            save_sync_state(state_dir, "ERROR", str(e))
            return {"status": "ERROR", "error": str(e)}

        outcome = inv.ingest_snapshot(conn, node_id, parse_result)
        if outcome["status"] == "REJECTED_INCOMPLETE":
            save_sync_state(state_dir, "REJECTED_INCOMPLETE", outcome["reason"])
            return {"status": "PARTIAL", "error": outcome["reason"],
                    "fetched_count": parse_result.get("fetched_count"),
                    "authoritative_total": parse_result.get("authoritative_total")}

        save_sync_state(state_dir, "OK", None)
        return {"status": "OK", "error": None, "diff": outcome["diff"],
                "fetched_count": parse_result.get("fetched_count"),
                "authoritative_total": parse_result.get("authoritative_total")}
    finally:
        release_inventory_lock(lock)
