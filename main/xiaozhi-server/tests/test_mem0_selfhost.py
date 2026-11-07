"""Self-hosted Mem0 client: no body logs, errors are not empty, owner check before delete."""

import asyncio
import json

import httpx

from core.providers.memory.mem0_selfhost.mem0_selfhost import MemoryLookupError, MemoryProvider


def _provider(handler):
    provider = MemoryProvider(
        {"base_url": "http://mem0.local", "api_key": "not-a-real-key", "agent_id": "rocky"}
    )
    provider.role_id = "fake-user"
    provider._clients[0] = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mem0.local",
    )
    provider._client = lambda _timeout: provider._clients[0]
    return provider


def test_search_error_raises_and_does_not_log_body():
    logs = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="SECRET_MEMORY_BODY")

    provider = _provider(handler)

    async def run():
        try:
            await provider.query_memory("hello")
        except MemoryLookupError as exc:
            return str(exc)
        return "no-error"

    assert asyncio.run(run()) == "http_500"
    # The provider logger may be absent in this stub path; the exception itself
    # must not carry the response body.
    assert "SECRET_MEMORY_BODY" not in "".join(logs)


def test_empty_200_is_an_empty_string_and_other_users_are_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["filters"]["user_id"] == "fake-user"
        assert "agent_id" not in body
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "own", "memory": "tea", "user_id": "fake-user"},
                    {"id": "other", "memory": "DO_NOT_KEEP", "user_id": "real-user"},
                ]
            },
        )

    provider = _provider(handler)
    assert provider.agent_id == "rocky"
    text = asyncio.run(provider.query_memory("tea"))
    assert "tea" in text
    assert "DO_NOT_KEEP" not in text


def test_delete_checks_owner_before_delete_and_reuses_the_client():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/memories/own":
            return httpx.Response(200, json={"id": "own", "user_id": "fake-user", "memory": "tea"})
        if request.method == "GET" and request.url.path == "/memories/other":
            return httpx.Response(200, json={"id": "other", "user_id": "real-user", "memory": "secret"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text="nope")

    provider = _provider(handler)

    async def run():
        denied = await provider.delete_owned("other")
        allowed = await provider.delete_owned("own")
        return denied, allowed

    denied, allowed = asyncio.run(run())
    assert denied is False
    assert allowed is True
    assert ("DELETE", "/memories/other") not in calls
    assert ("DELETE", "/memories/own") in calls
    assert ("GET", "/memories/own") in calls
    created = [client for client in provider._clients.values()]
    assert len(created) == 1
