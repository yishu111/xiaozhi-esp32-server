"""Forget deletes only a previewed id after the backend confirms the owner."""

import asyncio

from plugins_func.functions import forget_memory


class _Memory:
    def __init__(self):
        self.role_id = "fake-user"
        self.base_url = "http://mem0.local"
        self.rows = {
            "tea": {"user_id": "fake-user", "memory": "UNIQUE_MEMORY_TEA"},
            "real": {"user_id": "real-user", "memory": "DO_NOT_DELETE_REAL"},
        }
        self.deleted = []

    async def search_own(self, query, top_k=3):
        found = []
        for memory_id, row in self.rows.items():
            if row["user_id"] != self.role_id:
                continue
            if query not in row["memory"] and query not in memory_id:
                continue
            found.append({"id": memory_id, "text": row["memory"], "owner_in_search": True})
        return found[:top_k]

    async def delete_owned(self, memory_id):
        row = self.rows.get(memory_id)
        if row is None or row["user_id"] != self.role_id:
            return False
        self.deleted.append(memory_id)
        del self.rows[memory_id]
        return True


def _conn(memory):
    conn = type("Conn", (), {})()
    conn.memory = memory
    conn.device_id = memory.role_id
    conn.introduced_speakers = set()
    return conn


def setup_function():
    forget_memory._LOGS.clear()


def test_arbitrary_id_is_not_deleted_until_preview_token_and_owner_agree():
    memory = _Memory()
    conn = _conn(memory)
    preview = asyncio.run(forget_memory.forget_memory(conn, query="UNIQUE_MEMORY"))
    assert "DO_NOT_DELETE_REAL" not in preview.result
    token = preview.result.split("口令 ", 1)[1].split("。", 1)[0]
    refused = asyncio.run(
        forget_memory.forget_memory(conn, query="UNIQUE_MEMORY", memory_id="real", confirmation=token)
    )
    assert "没有删除" in refused.result
    assert memory.deleted == []
    assert "real" in memory.rows
    done = asyncio.run(
        forget_memory.forget_memory(conn, query="UNIQUE_MEMORY", memory_id="tea", confirmation=token)
    )
    assert "已删除" in done.result
    assert memory.deleted == ["tea"]
    assert "real" in memory.rows
    assert all("UNIQUE_MEMORY" not in line and "DO_NOT_DELETE" not in line for line in forget_memory._LOGS)


def test_delete_without_preview_does_nothing():
    memory = _Memory()
    conn = _conn(memory)
    result = asyncio.run(
        forget_memory.forget_memory(conn, query="tea", memory_id="tea", confirmation="000000")
    )
    assert "没有删除" in result.result or "还没有" in result.result
    assert memory.deleted == []
