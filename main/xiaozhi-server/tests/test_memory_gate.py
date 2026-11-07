"""Memory lookup stays on the critical path and fails closed on timeout."""

import asyncio
import time

from core.utils.memory_gate import (
    MEMORY_TIMEOUT_SPOKEN,
    cancel_memory_lookup,
    resolve_memory_blocking,
    schedule_memory_lookup,
    should_call_llm,
)
from core.utils.utterance_timing import note_asr_final


class _Log:
    def bind(self, **_kwargs):
        return self

    def info(self, _message):
        return None


class _Memory:
    def __init__(self, delay, result):
        self.delay = delay
        self.result = result
        self.calls = 0
        self.seen = []

    async def query_memory(self, query):
        self.calls += 1
        self.seen.append(query)
        await asyncio.sleep(self.delay)
        return self.result


def _conn(memory, timeout):
    conn = type("Conn", (), {})()
    conn.memory = memory
    conn.config = {"memory_query_timeout_seconds": timeout}
    conn.logger = _Log()
    conn._timing_lines = []
    return conn


def test_prefetch_overlaps_preamble_and_hides_memory_text_from_logs():
    async def run():
        memory = _Memory(0.4, "secret-memory-body")
        conn = _conn(memory, 2)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 6)
        started = time.monotonic()
        schedule_memory_lookup(conn, "user text")
        await asyncio.sleep(0.25)
        decision = await asyncio.to_thread(resolve_memory_blocking, conn, "user text")
        return decision, time.monotonic() - started, memory, conn

    decision, elapsed, memory, conn = asyncio.run(run())
    assert decision.status == "ok"
    assert decision.text == "secret-memory-body"
    assert should_call_llm(decision.status)
    assert memory.calls == 1
    assert elapsed < 0.6
    blob = "\n".join(conn._timing_lines)
    assert "secret-memory-body" not in blob
    assert "user text" not in blob
    assert "memory=ok" in blob


def test_timeout_is_not_an_empty_memory():
    async def run():
        memory = _Memory(1.0, "should-not-be-used")
        conn = _conn(memory, 0.1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 2)
        schedule_memory_lookup(conn, "question")
        return await asyncio.to_thread(resolve_memory_blocking, conn, "question")

    decision = asyncio.run(run())
    assert decision.status == "timeout"
    assert decision.text is None
    assert decision.text != ""
    assert should_call_llm("timeout") is False
    assert should_call_llm("empty") is True
    assert "question" not in MEMORY_TIMEOUT_SPOKEN


def test_non_string_memory_does_not_reach_the_llm():
    async def run():
        memory = _Memory(0, {"memory": "secret"})
        conn = _conn(memory, 1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 1)
        schedule_memory_lookup(conn, "q")
        return await asyncio.to_thread(resolve_memory_blocking, conn, "q")

    decision = asyncio.run(run())
    assert decision.status == "error"
    assert should_call_llm(decision.status) is False


def test_cancel_before_chat_marks_cancelled():
    async def run():
        memory = _Memory(0.5, "body")
        conn = _conn(memory, 2)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 1)
        schedule_memory_lookup(conn, "q")
        cancel_memory_lookup(conn)
        await asyncio.sleep(0)
        return conn

    conn = asyncio.run(run())
    blob = "\n".join(conn._timing_lines)
    assert "memory=cancelled" in blob


def test_empty_string_still_counts_as_a_real_empty_result():
    async def run():
        memory = _Memory(0, "")
        conn = _conn(memory, 1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 1)
        schedule_memory_lookup(conn, "q")
        return await asyncio.to_thread(resolve_memory_blocking, conn, "q")

    decision = asyncio.run(run())
    assert decision.status == "empty"
    assert decision.text == ""
    assert should_call_llm(decision.status)
