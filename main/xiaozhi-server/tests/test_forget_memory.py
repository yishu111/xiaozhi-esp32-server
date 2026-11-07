"""Forget only the confirmed ids of one fake user."""

import asyncio
import time

from plugins_func.functions import forget_memory


class _Log:
    def bind(self, **_kwargs):
        return self

    def info(self, _message):
        return None


class _Store:
    def __init__(self):
        self.rows = {
            "tea": {"id": "tea", "user_id": "fake-user", "text": "UNIQUE_MEMORY_TEA"},
            "city": {"id": "city", "user_id": "fake-user", "text": "UNIQUE_MEMORY_CITY"},
            "real": {"id": "real", "user_id": "real-user", "text": "DO_NOT_DELETE_REAL"},
        }
        self.delete_all_called = False

    def list_own_memories(self, query):
        matched = []
        for row in self.rows.values():
            if row["user_id"] != self.role_id:
                continue
            if query and query not in row["text"] and query not in row["id"]:
                continue
            matched.append({"id": row["id"], "text": row["text"]})
        return {"supported": True, "records": matched}

    def delete_own_memories(self, memory_ids):
        deleted = []
        refused = []
        for memory_id in memory_ids:
            row = self.rows.get(memory_id)
            if row is None or row["user_id"] != self.role_id:
                refused.append(memory_id)
                continue
            deleted.append(memory_id)
            del self.rows[memory_id]
        return {"supported": True, "deleted": deleted, "refused": refused}


class _Provider:
    def __init__(self, store, role):
        self.store = store
        self.role_id = role
        store.role_id = role

    async def list_own_memories(self, query):
        return self.store.list_own_memories(query)

    async def delete_own_memories(self, memory_ids):
        return self.store.delete_own_memories(memory_ids)


def _conn(provider):
    conn = type("Conn", (), {})()
    conn.memory = provider
    conn.device_id = provider.role_id
    conn.logger = _Log()
    return conn


def _run(coro):
    return asyncio.run(coro)


def setup_function():
    forget_memory._LOGS.clear()


def test_confirm_deletes_only_the_previewed_fake_user_rows():
    store = _Store()
    conn = _conn(_Provider(store, "fake-user"))
    preview = _run(forget_memory.forget_memory(conn, action="preview", query="UNIQUE_MEMORY"))
    assert "DO_NOT_DELETE_REAL" not in preview.result
    token = preview.result.split("口令 ", 1)[1].split("。", 1)[0]
    wrong = _run(
        forget_memory.forget_memory(
            conn,
            action="confirm",
            memory_ids=["tea", "city", "real"],
            confirmation=token,
        )
    )
    assert "没有删除" in wrong.result
    assert set(store.rows) == {"tea", "city", "real"}
    done = _run(
        forget_memory.forget_memory(
            conn,
            action="confirm",
            memory_ids=["city", "tea"],
            confirmation=token,
        )
    )
    assert "已删除 2" in done.result
    assert set(store.rows) == {"real"}
    assert store.delete_all_called is False
    assert all("UNIQUE_MEMORY" not in line and "DO_NOT_DELETE" not in line for line in forget_memory._LOGS)


def test_wrong_token_and_other_role_delete_nothing():
    store = _Store()
    conn = _conn(_Provider(store, "fake-user"))
    preview = _run(forget_memory.forget_memory(conn, action="preview", query="TEA"))
    token = preview.result.split("口令 ", 1)[1].split("。", 1)[0]
    refused = _run(
        forget_memory.forget_memory(
            conn,
            action="confirm",
            memory_ids=["tea"],
            confirmation="000000",
        )
    )
    assert token not in refused.result or "没有删除" in refused.result
    assert "tea" in store.rows
    conn.device_id = "real-user"
    conn.memory.role_id = "real-user"
    switched = _run(
        forget_memory.forget_memory(
            conn,
            action="confirm",
            memory_ids=["tea"],
            confirmation=token,
        )
    )
    assert "没有删除" in switched.result or "不会" in switched.result
    assert "tea" in store.rows


def test_expired_preview_and_unsupported_backend_delete_nothing():
    store = _Store()
    conn = _conn(_Provider(store, "fake-user"))
    _run(forget_memory.forget_memory(conn, action="preview", query="TEA"))
    conn._forget_pending["expires"] = time.monotonic() - 1
    expired = _run(
        forget_memory.forget_memory(
            conn,
            action="confirm",
            memory_ids=["tea"],
            confirmation=conn._forget_pending["token"],
        )
    )
    assert "过期" in expired.result
    assert "tea" in store.rows

    bare = type("Conn", (), {})()
    bare.memory = type("Memory", (), {"role_id": "fake-user"})()
    bare.device_id = "fake-user"
    bare.logger = _Log()
    unsupported = _run(forget_memory.forget_memory(bare, action="preview", query="TEA"))
    assert "不会删除" in unsupported.result
