"""mem0ai delete stays inside the current user id."""

import asyncio

from core.providers.memory.mem0ai.mem0ai import MemoryProvider
from core.providers.memory.record_scope import scoped_records


class _Client:
    def __init__(self):
        self.rows = {
            "tea": {"id": "tea", "memory": "likes tea", "user_id": "fake-user"},
            "real": {"id": "real", "memory": "DO_NOT_DELETE", "user_id": "real-user"},
        }
        self.deleted = []
        self.delete_all_called = False

    def search(self, _query, filters=None):
        return {"results": list(self.rows.values())}

    def get(self, memory_id):
        return self.rows[memory_id]

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        self.rows.pop(memory_id, None)

    def delete_all(self, **_kwargs):
        self.delete_all_called = True
        self.rows.clear()


def test_scoped_records_drop_other_users_and_missing_owners():
    payload = {
        "results": [
            {"id": "a", "memory": "own", "user_id": "fake-user"},
            {"id": "b", "memory": "other", "user_id": "real-user"},
            {"id": "c", "memory": "unscoped"},
        ]
    }
    records = scoped_records(payload, "fake-user")
    assert [item["id"] for item in records] == ["a"]


def test_provider_delete_refuses_a_different_user_and_never_resets():
    provider = MemoryProvider.__new__(MemoryProvider)
    provider.use_mem0 = True
    provider.role_id = "fake-user"
    provider.client = _Client()
    listed = asyncio.run(provider.list_own_memories("tea"))
    assert [item["id"] for item in listed["records"]] == ["tea"]
    outcome = asyncio.run(provider.delete_own_memories(["tea", "real", "missing"]))
    assert outcome["deleted"] == ["tea"]
    assert set(outcome["refused"]) == {"real", "missing"}
    assert "real" in provider.client.rows
    assert provider.client.delete_all_called is False
    assert provider.client.deleted == ["tea"]
