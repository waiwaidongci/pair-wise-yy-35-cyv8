from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def lease_expiry(now: str, lease_seconds: int) -> str:
    instant = datetime.fromisoformat(now)
    return (instant + timedelta(seconds=int(lease_seconds))).isoformat()


def calculate_hash(previous_hash: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256((previous_hash + ":").encode("utf-8") + raw).hexdigest()


def make_entry(action: str, entity_type: str, entity_id: int, actor: str,
               detail: dict, previous_hash: str) -> dict:
    payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor": actor,
        "detail": detail,
        "created_at": utc_now(),
    }
    return dict(payload, previous_hash=previous_hash,
                entry_hash=calculate_hash(previous_hash, payload))
