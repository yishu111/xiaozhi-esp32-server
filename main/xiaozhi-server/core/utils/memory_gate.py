"""Bounded memory lookup for one voice turn.

The lookup still finishes before the LLM is called. A timeout or a raised
error does not continue with an empty memory string, because that would look
like "this user has no memories". An empty string from the provider is kept
as a real empty result, matching v0.9.7 mem0ai.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Optional

from core.utils.utterance_timing import mark_current

MEMORY_TIMEOUT_SPOKEN = "记忆没有及时返回，为避免记错，这句话我先不回答。"
_LLM_STATUSES = {"ok", "empty", "skipped"}


@dataclass(frozen=True)
class MemoryDecision:
    status: str
    text: Optional[str]


def should_call_llm(status: str) -> bool:
    return status in _LLM_STATUSES


def _timeout_seconds(conn) -> float:
    config = getattr(conn, "config", None) or {}
    raw = config.get("memory_query_timeout_seconds", 15)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 15.0
    if value < 0:
        return 15.0
    return value


def _digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def _utt_id(conn) -> Optional[str]:
    utt = getattr(conn, "_utterance_timing", None)
    return getattr(utt, "id", None)


def schedule_memory_lookup(conn, query: str) -> None:
    """Start query_memory on the running loop, before later preamble work."""
    memory = getattr(conn, "memory", None)
    mark_current(conn, "memory_lookup_start")
    if memory is None or not query:
        mark_current(conn, "memory_lookup_end", memory="skipped")
        conn._memory_lookup = {
            "id": _utt_id(conn),
            "digest": _digest(query or ""),
            "task": None,
            "status": "skipped",
        }
        return

    task = asyncio.create_task(memory.query_memory(query))
    conn._memory_lookup = {
        "id": _utt_id(conn),
        "digest": _digest(query),
        "task": task,
        "status": "pending",
    }


def cancel_memory_lookup(conn) -> None:
    pending = getattr(conn, "_memory_lookup", None)
    if not isinstance(pending, dict):
        return
    task = pending.get("task")
    if task is not None and not task.done():
        task.cancel()
    pending["task"] = None
    pending["status"] = "cancelled"
    mark_current(conn, "memory_lookup_end", memory="cancelled")


async def _await_task(task, timeout: float):
    if timeout > 0:
        return await asyncio.wait_for(task, timeout)
    return await task


async def _resolve_async(conn, query: str) -> MemoryDecision:
    pending = getattr(conn, "_memory_lookup", None)
    digest = _digest(query or "")
    timeout = _timeout_seconds(conn)
    task = None
    if (
        isinstance(pending, dict)
        and pending.get("id") == _utt_id(conn)
        and pending.get("digest") == digest
    ):
        if pending.get("status") == "skipped":
            return MemoryDecision("skipped", None)
        if pending.get("status") == "cancelled":
            pending = None
        else:
            task = pending.get("task")

    memory = getattr(conn, "memory", None)
    if task is None:
        if memory is None or not query:
            mark_current(conn, "memory_lookup_start")
            mark_current(conn, "memory_lookup_end", memory="skipped")
            return MemoryDecision("skipped", None)
        mark_current(conn, "memory_lookup_start")
        task = asyncio.create_task(memory.query_memory(query))

    try:
        text = await _await_task(task, timeout)
    except asyncio.TimeoutError:
        mark_current(conn, "memory_lookup_end", memory="timeout")
        return MemoryDecision("timeout", None)
    except asyncio.CancelledError:
        mark_current(conn, "memory_lookup_end", memory="cancelled")
        return MemoryDecision("cancelled", None)
    except Exception:
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)

    if not isinstance(text, str):
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)
    status = "empty" if text == "" else "ok"
    mark_current(conn, "memory_lookup_end", memory=status)
    return MemoryDecision(status, text)


def resolve_memory_blocking(conn, query: str) -> MemoryDecision:
    loop = getattr(conn, "loop", None)
    if loop is None:
        return MemoryDecision("error", None)
    timeout = _timeout_seconds(conn)
    outer = timeout + 1 if timeout > 0 else None
    future = asyncio.run_coroutine_threadsafe(_resolve_async(conn, query), loop)
    try:
        return future.result(timeout=outer)
    except asyncio.TimeoutError:
        mark_current(conn, "memory_lookup_end", memory="timeout")
        return MemoryDecision("timeout", None)
    except Exception:
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)
