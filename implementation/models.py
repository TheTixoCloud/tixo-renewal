"""Decimal-safe data model for the bandwidth carryover accounting engine.

Pure data structures only. No I/O, no network, no database access anywhere in this module
or anywhere else in this package -- that is enforced by convention (grep for 'socket',
'subprocess', 'urllib', 'mysql' finds nothing in implementation/).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


def to_decimal(value) -> Decimal:
    """Convert any numeric-ish input to Decimal via its string form, never via float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    # floats are only accepted if they came from json parsing of a receipt; route through
    # str() to avoid binary-float artifacts being baked into the Decimal.
    return Decimal(str(value))


@dataclass(frozen=True)
class Identity:
    """A VPS identity. Requirement #6: never key by vpsid alone."""
    vpsid: int
    created: str  # Virtualizor 'created' timestamp, as observed. Opaque string; not parsed as time.

    def key(self) -> str:
        return f"{self.vpsid}|{self.created}"


@dataclass(frozen=True)
class Event:
    """One normalized input event, derived from a ledger line or a receipt record."""
    type: str  # QUOTA_CHANGE | USAGE_DROP_REVIEW | VPS_ADDED | VPS_REMOVED | VPS_ID_REUSED | UNLIMITED_QUOTA_CHANGE
    identity: Identity
    before_ts: str
    after_ts: str
    old_quota_gb: Optional[Decimal] = None
    new_quota_gb: Optional[Decimal] = None
    old_used_gb: Optional[Decimal] = None
    new_used_gb: Optional[Decimal] = None
    source: str = "ledger"  # 'ledger' or 'receipt:<filename>'
    raw: dict = field(default_factory=dict)

    def event_id(self) -> str:
        payload = "|".join([
            self.type, self.identity.key(), self.before_ts, self.after_ts,
            str(self.old_quota_gb), str(self.new_quota_gb),
            str(self.old_used_gb), str(self.new_used_gb), self.source,
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class ReceiptRecord:
    """A purchase/top-up fact extracted from an admin receipt file."""
    vpsid: int
    delta_gb: Decimal
    timestamp: str
    source_file: str
    detail: str
    is_correction: bool = False
    corrects_source_file: Optional[str] = None
    before_gb: Optional[Decimal] = None  # quota value the receipt itself claims as "before",
    after_gb: Optional[Decimal] = None   # when the source file carries it -- used to confirm a
                                          # candidate match against the actual ledger quota values,
                                          # not just a coincidental vpsid+amount match.


@dataclass
class Proposal:
    """One fully-audited proposed adjustment. Never applied anywhere by this engine."""
    identity_key: str
    vpsid: int
    created: str
    event_id: str
    reset_after_ts: str
    old_quota_gb: Decimal
    old_used_gb: Decimal
    remaining_gb: Decimal
    monthly_base_gb: Decimal
    verified_purchase_credit_gb: Decimal
    contributing_receipts: list
    proposed_new_quota_gb: Decimal
    status: str  # INITIAL_GRANT | READY_FOR_HUMAN_APPLICATION | FROZEN_FOR_REVIEW | HISTORICALLY_EXCLUDED | REJECTED_<reason>
    kind: str  # INITIAL_GRANT_HISTORICAL | INITIAL_GRANT_LIVE | MONTHLY_RENEWAL | NOT_A_RENEWAL | ...
    reasons: list
    renewal_period_key: Optional[str] = None  # set only for a confirmed, exactly-once-granted period

    def to_json(self) -> dict:
        d = {
            "identity_key": self.identity_key,
            "vpsid": self.vpsid,
            "created": self.created,
            "event_id": self.event_id,
            "reset_after_ts": self.reset_after_ts,
            "old_quota_gb": str(self.old_quota_gb),
            "old_used_gb": str(self.old_used_gb),
            "remaining_gb": str(self.remaining_gb),
            "monthly_base_gb": str(self.monthly_base_gb),
            "verified_purchase_credit_gb": str(self.verified_purchase_credit_gb),
            "contributing_receipts": self.contributing_receipts,
            "proposed_new_quota_gb": str(self.proposed_new_quota_gb),
            "status": self.status,
            "kind": self.kind,
            "reasons": self.reasons,
            "renewal_period_key": self.renewal_period_key,
        }
        return d


class DecimalJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)
