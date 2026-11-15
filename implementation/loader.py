"""Loaders that turn the sanitized evidence files (local copies only, never live production
reads) into the normalized Event / ReceiptRecord objects the engine consumes.
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
from decimal import Decimal

from models import Identity, Event, ReceiptRecord, to_decimal

UNKNOWN_CREATED = "UNKNOWN"


def load_fixed_reset_keys(path) -> set:
    data = json.loads(open(path).read())
    out = set()
    for x in data:
        if x.get("status") == "MANUALLY_FIXED":
            out.add((int(x["vpsid"]), str(x["reset_time"])))
    return out


def build_created_lookup(events: list) -> dict:
    """Derive a per-vpsid identity anchor from the earliest VPS_ADDED event seen for it.

    NOTE: this single-value-per-vpsid form only anchors the *first* era of a vpsid. It exists
    for callers that only care about "is this vpsid's whole observed history anchored at all,"
    not full reuse-safety. For reuse-safety across multiple eras of the same vpsid within one
    event stream, use `assign_identity_anchors()` instead, which is time-aware.
    """
    lookup = {}
    for e in events:
        if e.type == "VPS_ADDED":
            vpsid = str(e.identity.vpsid)
            if vpsid not in lookup:  # first VPS_ADDED wins; events are pre-sorted by after_ts
                lookup[vpsid] = e.after_ts
    return lookup


def assign_identity_anchors(events: list) -> list:
    """Time-aware identity anchoring: walks each vpsid's chronological event stream and
    assigns `created` = the after_ts of the most recent VPS_ADDED seen *so far* for that
    vpsid. A later VPS_ADDED for the same vpsid (an ID-reuse case) re-anchors every
    subsequent event to the NEW era, correctly separating it from the old era's identity key.

    Fixes the Phase-1 identity gap: the production ledger never writes a `created` field, so
    without this every vpsid used one constant UNKNOWN_CREATED string for its entire
    history -- meaning a real-world VPS_REMOVED followed by a different customer reusing the
    same vpsid would have silently merged into one identity, and a VPS_ID_REUSED event would
    have nothing distinguishing to re-anchor to. Events for a vpsid before its first observed
    VPS_ADDED keep UNKNOWN_CREATED -- that residual gap (the VPS already existed before
    monitoring started) is a hard evidentiary limit, not a bug, and must be reported as such,
    never silently assumed safe.

    `events` must already be sorted by after_ts (as load_ledger_events() returns them).
    """
    current_created = {}  # vpsid(int) -> created anchor, updated as we walk forward in time
    out = []
    for e in events:
        vpsid = e.identity.vpsid
        if e.type == "VPS_ADDED":
            current_created[vpsid] = e.after_ts
        anchor = current_created.get(vpsid, UNKNOWN_CREATED)
        if e.identity.created == anchor:
            out.append(e)
        else:
            new_identity = Identity(vpsid=vpsid, created=anchor)
            out.append(dataclasses.replace(e, identity=new_identity))
    return out


def load_ledger_events(path, created_lookup=None) -> list:
    """created_lookup: optional dict vpsid(str) -> created(str), used when the production
    ledger-events.jsonl line doesn't carry a 'created' field (the known Phase 1 gap). Falls
    back to UNKNOWN_CREATED, which degrades identity tracking to vpsid-only for that event --
    this degradation is explicit and reported, never silently assumed safe.
    """
    created_lookup = created_lookup or {}
    events = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            vpsid = int(d["vpsid"])
            created = str(d.get("created") or created_lookup.get(str(vpsid), UNKNOWN_CREATED))
            ident = Identity(vpsid=vpsid, created=created)

            def dec(v):
                return to_decimal(v) if v is not None else None

            events.append(Event(
                type=d["type"], identity=ident,
                before_ts=d["before"], after_ts=d["after"],
                old_quota_gb=dec(d.get("old_quota")), new_quota_gb=dec(d.get("new_quota")),
                old_used_gb=dec(d.get("old_used_gb")), new_used_gb=dec(d.get("new_used_gb")),
                source="ledger", raw=d,
            ))
    events.sort(key=lambda e: e.after_ts)
    return events


def load_ledger_events_with_identity_anchors(path) -> list:
    """Load events, then time-aware re-anchor each one's identity.created to the most recent
    VPS_ADDED seen so far for its vpsid. See assign_identity_anchors() for why this matters
    and what it does NOT fix (pre-monitoring-window VPS have no anchor at all)."""
    raw = load_ledger_events(path)
    return assign_identity_anchors(raw)


def scan_verified_created_epochs(receipts_dir):
    """Scan the receipt/manual-fix evidence corpus for genuine Virtualizor `created` epochs
    (sourced originally from the real `vps.time` DB column -- see evidence/scripts/
    virt-creation-times.py -- these files snapshot that column's value per vpsid at the time
    of a manual top-up/fix, they do NOT originate from this engine's own monitoring).

    This is deliberately NOT the same thing as the VPS_ADDED ledger timestamp used for
    identity anchoring in assign_identity_anchors() -- that is merely "when our monitoring
    first observed this vpsid," not the VPS's actual creation date, and must never be
    substituted for it when deciding monthly-anniversary renewal eligibility.

    Returns (verified_epochs, conflicts):
    - verified_epochs: dict vpsid(int) -> created_epoch(int), only for vpsids where every
      evidence file that mentions a `created` value for that vpsid agrees.
    - conflicts: vpsid -> sorted list of the distinct conflicting epoch values seen. A
      conflicting vpsid is fail-closed: it is excluded from verified_epochs entirely rather
      than guessing which value is correct.
    """
    seen = {}  # vpsid -> set of created epoch values seen across all evidence
    for path in sorted(glob.glob(os.path.join(receipts_dir, "*.json"))):
        try:
            d = json.load(open(path))
        except Exception:
            continue
        _collect_vpsid_created_pairs(d, seen)

    verified = {}
    conflicts = {}
    for vpsid, epochs in seen.items():
        if len(epochs) == 1:
            verified[vpsid] = next(iter(epochs))
        else:
            conflicts[vpsid] = sorted(epochs)
    return verified, conflicts


def _collect_vpsid_created_pairs(node, seen):
    """Recursively walk a parsed JSON structure for any dict that carries both a vpsid-like
    key (`vpsid` or `vid`) and a `created` key, and record the pair into `seen`."""
    if isinstance(node, dict):
        vpsid = node.get("vpsid", node.get("vid"))
        created = node.get("created")
        if vpsid is not None and created is not None:
            try:
                seen.setdefault(int(vpsid), set()).add(int(created))
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _collect_vpsid_created_pairs(v, seen)
    elif isinstance(node, list):
        for v in node:
            _collect_vpsid_created_pairs(v, seen)


def load_receipts(receipts_dir):
    """Re-implements the Phase 2 reconciliation extraction rules as first-class engine input
    (requirement #14: reconciliation is an engine capability, not a one-off script).

    Returns (receipts, malformed_files, skipped_backup_files):
    - malformed_files: files that failed to parse as JSON at all. Previously these were
      silently `continue`d past -- a corrupted receipt (confirmed on production itself, see
      PRODUCTION_RISKS.md #8) would vanish with no trace. Now every such file is reported so
      a human can be told "this receipt could not be read" instead of it just going missing.
    - skipped_backup_files: a `*-before-*` snapshot file whose own basename is referenced by
      another file's `"backup"` field is the SAME transaction's pre-state copy, not an
      independent purchase. Two real production file pairs (customer-topup-20261111-...-before
      and customer-topup-20261112-...-before) each carry their own `targets` block with the
      SAME add-amounts as their paired main receipt -- parsing both as independent receipts
      double-counts a single real purchase. Confirmed by reading both files' contents; this
      was a real latent double-credit bug in the original loader, not a hypothetical.
    """
    paths = sorted(glob.glob(os.path.join(receipts_dir, "*.json")))
    parsed = {}
    malformed_files = []
    for path in paths:
        fname = os.path.basename(path)
        try:
            parsed[fname] = json.load(open(path))
        except Exception as e:
            malformed_files.append({"file": fname, "error": str(e)})

    backup_basenames = set()
    for fname, d in parsed.items():
        backup = d.get("backup")
        if backup:
            backup_basenames.add(os.path.basename(backup))

    out = []
    skipped_backup_files = sorted(backup_basenames & set(parsed.keys()))
    for fname, d in parsed.items():
        if fname in backup_basenames:
            continue  # this file is itself a pre-state backup of another receipt; skip it
        ts = d.get("time") or d.get("timestamp") or d.get("created_at") or ""

        def add(vpsid, delta, detail, before_gb=None, after_gb=None):
            if vpsid is None or delta is None:
                return
            out.append(ReceiptRecord(
                vpsid=int(vpsid), delta_gb=to_decimal(delta), timestamp=ts,
                source_file=fname, detail=detail,
                before_gb=to_decimal(before_gb) if before_gb is not None else None,
                after_gb=to_decimal(after_gb) if after_gb is not None else None,
            ))

        if "items" in d and isinstance(d["items"], list):
            for it in d["items"]:
                if "added_gb" in it:
                    add(it.get("vpsid"), it["added_gb"], "items[].added_gb",
                        before_gb=it.get("before_gb"), after_gb=it.get("after_gb"))
        if "targets" in d:
            t = d["targets"]
            if isinstance(t, dict):
                for k, v in t.items():
                    if isinstance(v, dict) and "add" in v:
                        add(v.get("vpsid"), v["add"], "targets{}.add",
                            before_gb=v.get("before"), after_gb=v.get("after"))
                    elif isinstance(v, list) and len(v) == 3:
                        add(v[0], v[2], "targets{}=[vpsid,before,add]",
                            before_gb=v[1], after_gb=(to_decimal(v[1]) + to_decimal(v[2])))
            elif isinstance(t, list):
                for v in t:
                    if isinstance(v, dict) and "new" in v and "old" in v:
                        pass  # manual-fix (correction), not a purchase -- handled separately below
                    elif isinstance(v, list) and len(v) >= 3 and str(v[0]).isdigit():
                        pass  # manual-fix [vpsid, old, new] -- a correction, not a purchase
        if "add_gb_per_vps" in d and isinstance(d.get("targets"), list):
            for v in d["targets"]:
                add(v.get("vpsid"), d["add_gb_per_vps"], "add_gb_per_vps flat")
        if d.get("operation") in ("ADD_500_GB_TO_EACH", "MANUAL_TOPUP"):
            for v in d.get("targets", []):
                delta = v.get("add", (v.get("after", 0) - v.get("before", 0)) if "after" in v else None)
                add(v.get("vpsid"), delta, d["operation"])
        if "added" in d and "vpsid" in d:
            add(d["vpsid"], d["added"], "single topup receipt")
        if "add_gb" in d and "vpsid" in d:
            add(d["vpsid"], d["add_gb"], "single topup (vps19/69 style)")
    return out, malformed_files, skipped_backup_files
