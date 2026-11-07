"""Own-user filtering for memory delete previews.

Records without a user id are not deletable. Records for a different user
are dropped. This module does not talk to a memory server.
"""

from __future__ import annotations

from typing import Any, Optional


def extract_records(payload: Any) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "memories", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "id" in payload or "memory" in payload or "memory_id" in payload:
            return [payload]
    return []


def owner_id(record: dict) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in ("user_id", "userId"):
        value = record.get(key)
        if value:
            return str(value)
    meta = record.get("metadata")
    if isinstance(meta, dict):
        for key in ("user_id", "userId"):
            value = meta.get(key)
            if value:
                return str(value)
    return None


def record_id(record: dict) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    for key in ("id", "memory_id"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def record_text(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    for key in ("memory", "text", "content"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return ""


def scoped_records(payload: Any, role_id: str) -> list[dict]:
    """Keep records whose owner is exactly role_id."""
    role = str(role_id or "")
    kept = []
    if not role:
        return kept
    for record in extract_records(payload):
        owner = owner_id(record)
        rid = record_id(record)
        if owner != role or not rid:
            continue
        kept.append({"id": rid, "text": record_text(record)})
    return kept


def deletion_allowed(record: Optional[dict], role_id: str) -> bool:
    if not record or not role_id:
        return False
    return owner_id(record) == str(role_id) and bool(record_id(record))
