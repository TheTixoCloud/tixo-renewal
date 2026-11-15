"""Atomic, crash-safe local persistence helpers. No network or DB access."""
from __future__ import annotations

import json
import os
from pathlib import Path

from models import DecimalJSONEncoder


def atomic_write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, cls=DecimalJSONEncoder)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX


def atomic_append_jsonl(path: Path, records) -> None:
    """Append-only, fsync'd. If the process crashes mid-write, the tmp-file dance below
    guarantees the target file is never left truncated or half-written: we write the full
    new content (old + new records) to a temp file, fsync, then atomically replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        existing = path.read_text().splitlines()
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        for line in existing:
            if line.strip():
                f.write(line + "\n")
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, cls=DecimalJSONEncoder) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def read_jsonl(path: Path):
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out
