#!/usr/bin/env python3
"""Dry-run replay driver. Reads local evidence copies only, writes only to a local output
directory. Never touches production, never touches a network socket or a database.

Usage:
    python3 replay.py --evidence ../evidence --out ../tests/replay_output.json
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from engine import CarryoverEngine
from loader import (load_fixed_reset_keys, load_ledger_events_with_identity_anchors,
                     load_receipts, scan_verified_created_epochs)
from storage import atomic_write_json


def run(evidence_dir: str, out_path: str):
    evidence = Path(evidence_dir)
    fixed_keys = load_fixed_reset_keys(evidence / "already-fixed-resets.json")
    events = load_ledger_events_with_identity_anchors(evidence / "ledger-events.jsonl")
    receipts, malformed_receipts, skipped_backup_receipts = load_receipts(evidence / "receipts")
    verified_created_epochs, creation_epoch_conflicts = scan_verified_created_epochs(evidence / "receipts")

    # historical_mode=True explicitly: this replay only ever reads past monitoring evidence,
    # never a live feed, so every INITIAL_GRANT it produces must be labeled historical/
    # informational (see engine.py CarryoverEngine.__init__ and issue #4 of the 2026-11-14
    # review) -- never presented as a live actionable to-do list.
    engine = CarryoverEngine(fixed_reset_keys=fixed_keys, verified_created_epochs=verified_created_epochs,
                              historical_mode=True)
    for r in receipts:
        engine.ingest_receipt(r)
    for e in events:
        engine.ingest_event(e)

    proposals = engine.get_proposals()
    unmatched = engine.get_unmatched_quota_changes()
    ambiguous = engine.get_ambiguous_quota_changes()
    insufficient = engine.get_insufficient_evidence_quota_changes()

    by_status = {}
    by_kind = {}
    for p in proposals:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1

    result = {
        "total_fixed_reset_keys": len(fixed_keys),
        "total_events_ingested": len(events),
        "total_receipts_loaded": len(receipts),
        "malformed_receipt_files": malformed_receipts,
        "skipped_backup_receipt_files": skipped_backup_receipts,
        "verified_created_epochs_count": len(verified_created_epochs),
        "creation_epoch_conflicts": {str(k): v for k, v in creation_epoch_conflicts.items()},
        "total_proposals": len(proposals),
        "proposals_by_status": by_status,
        "proposals_by_kind": by_kind,
        "unmatched_quota_changes_count": len(unmatched),
        "ambiguous_quota_changes_count": len(ambiguous),
        "insufficient_evidence_quota_changes_count": len(insufficient),
        "proposals": [p.to_json() for p in proposals],
        "unmatched_quota_changes": unmatched,
        "ambiguous_quota_changes": ambiguous,
        "insufficient_evidence_quota_changes": insufficient,
    }
    atomic_write_json(Path(out_path), result)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=os.path.join(os.path.dirname(__file__), "..", "evidence"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "tests", "replay_output.json"))
    args = ap.parse_args()
    res = run(args.evidence, args.out)
    print(f"events={res['total_events_ingested']} receipts={res['total_receipts_loaded']} "
          f"proposals={res['total_proposals']} by_status={res['proposals_by_status']} "
          f"by_kind={res['proposals_by_kind']} "
          f"unmatched_quota_changes={res['unmatched_quota_changes_count']} "
          f"ambiguous_quota_changes={res['ambiguous_quota_changes_count']} "
          f"insufficient_evidence_quota_changes={res['insufficient_evidence_quota_changes_count']} "
          f"malformed_receipts={len(res['malformed_receipt_files'])} "
          f"skipped_backup_receipts={len(res['skipped_backup_receipt_files'])} "
          f"verified_created_epochs={res['verified_created_epochs_count']} "
          f"creation_epoch_conflicts={len(res['creation_epoch_conflicts'])}")
