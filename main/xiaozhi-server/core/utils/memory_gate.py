"""Wait for one memory lookup before the LLM call.

The lookup starts here, on the same worker-thread to event-loop hop as
v0.9.7. Nothing is scheduled during VAD, ASR, or intent handling. A
synchronous provider can still block the loop once this wait begins; the
timeout cannot cut that short, and a late result is discarded.

A timeout or a raised error does not continue with an empty memory string.
An empty string from the provider is still a real empty result.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
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


def _drain_future(future) -> None:
    if not future.done():
        return
    try:
        future.result()
    except Exception:
        return


async def _bounded_query(memory, query: str, timeout: float):
    if timeout > 0:
        return await asyncio.wait_for(memory.query_memory(query), timeout)
    return await memory.query_memory(query)


def resolve_memory_blocking(conn, query: str) -> MemoryDecision:
    memory = getattr(conn, "memory", None)
    if memory is None or not query:
        mark_current(conn, "memory_lookup_start")
        mark_current(conn, "memory_lookup_end", memory="skipped")
        return MemoryDecision("skipped", None)

    loop = getattr(conn, "loop", None)
    mark_current(conn, "memory_lookup_start")
    if loop is None:
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)

    timeout = _timeout_seconds(conn)
    future = asyncio.run_coroutine_threadsafe(
        _bounded_query(memory, query, timeout), loop
    )
    future.add_done_callback(_drain_future)
    try:
        text = future.result(timeout=timeout if timeout > 0 else None)
    except (TimeoutError, asyncio.TimeoutError, concurrent.futures.TimeoutError):
        # 3.10 keeps these as distinct classes. A late result is drained, not used.
        future.cancel()
        mark_current(conn, "memory_lookup_end", memory="timeout")
        return MemoryDecision("timeout", None)
    except Exception:
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)

    if not isinstance(text, str):
        mark_current(conn, "memory_lookup_end", memory="error")
        return MemoryDecision("error", None)
    status = "empty" if text == "" else "ok"
    mark_current(conn, "memory_lookup_end", memory=status)
    return MemoryDecision(status, text)
