"""Memory lookup stays inside chat() and never becomes a silent empty string."""

import asyncio
import time
import warnings

from core.utils.memory_gate import (
    MEMORY_TIMEOUT_SPOKEN,
    resolve_memory_blocking,
    should_call_llm,
)
from core.utils.utterance_timing import note_asr_final


class _Log:
    def bind(self, **_kwargs):
        return self

    def info(self, _message):
        return None

    def error(self, _message):
        return None


class _Memory:
    def __init__(self, delay=0, result="", block=False, explode=False):
        self.delay = delay
        self.result = result
        self.block = block
        self.explode = explode
        self.calls = 0

    async def query_memory(self, query):
        self.calls += 1
        if self.block:
            time.sleep(self.delay)
        elif self.delay:
            await asyncio.sleep(self.delay)
        if self.explode:
            raise RuntimeError("backend down")
        return self.result


def _conn(memory, timeout):
    conn = type("Conn", (), {})()
    conn.memory = memory
    conn.config = {"memory_query_timeout_seconds": timeout}
    conn.logger = _Log()
    conn._timing_lines = []
    return conn


def test_asr_final_does_not_start_a_memory_task():
    async def run():
        memory = _Memory(delay=0.2, result="secret-body")
        conn = _conn(memory, 2)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 4)
        await asyncio.sleep(0.05)
        return memory.calls

    assert asyncio.run(run()) == 0


def test_timeout_discards_a_late_string_instead_of_using_empty():
    async def run():
        memory = _Memory(delay=0.4, result="late-secret", block=True)
        conn = _conn(memory, 0.1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 2)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            decision = await asyncio.to_thread(resolve_memory_blocking, conn, "question")
            await asyncio.sleep(0.5)
        return decision, caught, conn

    decision, caught, conn = asyncio.run(run())
    assert decision.status == "timeout"
    assert decision.text is None
    assert should_call_llm(decision.status) is False
    blob = "\n".join(conn._timing_lines)
    assert "late-secret" not in blob
    assert "question" not in blob
    assert "memory=timeout" in blob
    assert not any("never retrieved" in str(item.message) for item in caught)
    assert "question" not in MEMORY_TIMEOUT_SPOKEN


def test_provider_exception_is_not_an_empty_memory():
    async def run():
        memory = _Memory(explode=True)
        conn = _conn(memory, 1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 1)
        return await asyncio.to_thread(resolve_memory_blocking, conn, "q")

    decision = asyncio.run(run())
    assert decision.status == "error"
    assert decision.text is None
    assert should_call_llm("error") is False
    assert should_call_llm("empty") is True


def test_real_empty_string_still_reaches_the_llm():
    async def run():
        memory = _Memory(result="")
        conn = _conn(memory, 1)
        conn.loop = asyncio.get_running_loop()
        note_asr_final(conn, 1)
        return await asyncio.to_thread(resolve_memory_blocking, conn, "q"), memory.calls

    decision, calls = asyncio.run(run())
    assert calls == 1
    assert decision.status == "empty"
    assert decision.text == ""
    assert should_call_llm(decision.status)
