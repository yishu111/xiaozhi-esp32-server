"""自托管 Mem0 server 记忆模块。

接口对应 mem0 OSS server/main.py：POST /memories、POST /search、
GET /memories/{memory_id}、DELETE /memories/{memory_id}。
鉴权头是 X-API-Key。官方 mem0ai 模块走的是云平台，不用于这个自建服务。

查询失败会抛 MemoryLookupError。调用方不能把失败当成“没有记忆”。
日志只记状态码和字节数，不记响应正文。
"""

import asyncio
import json
import threading

import httpx

from ..base import MemoryProviderBase, logger

TAG = __name__


class MemoryLookupError(RuntimeError):
    """The memory service did not return a usable result."""


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)
        self.base_url = str(config.get("base_url", "http://127.0.0.1:8888")).rstrip("/")
        self.api_key = config.get("api_key", "")
        self.agent_id = config.get("agent_id", "rocky")
        self.top_k = int(config.get("top_k", 8))
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}
        self._clients = {}
        self._client_lock = threading.Lock()

    @staticmethod
    def _text(content):
        if not content:
            return content
        try:
            text = content.strip()
            if text.startswith("{") and text.endswith("}"):
                data = json.loads(text)
                if "content" in data:
                    return data["content"]
        except Exception:
            pass
        return content

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """Reuse one client per running loop. save_memory uses its own loop."""
        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._client_lock:
            client = self._clients.get(key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(timeout=timeout, headers=self.headers)
                self._clients[key] = client
            return client

    async def aclose(self):
        with self._client_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                continue

    def _log_http(self, action: str, response: httpx.Response) -> None:
        logger.bind(tag=TAG).info(
            f"Mem0 {action}: HTTP {response.status_code} bytes={len(response.content)}"
        )

    async def _request(self, method: str, path: str, payload, timeout: float) -> httpx.Response:
        kwargs = {}
        if payload is not None:
            kwargs["json"] = payload
        try:
            return await self._client(timeout).request(
                method, f"{self.base_url}{path}", **kwargs
            )
        except Exception as exc:
            logger.bind(tag=TAG).error(f"Mem0 {method} {path} 失败: {type(exc).__name__}")
            raise MemoryLookupError("transport") from None

    async def save_memory(self, msgs, session_id=None):
        messages = [
            {"role": message.role, "content": self._text(message.content)}
            for message in msgs
            if message.role in ("user", "assistant") and message.content
        ]
        if len(messages) < 2 or not self.role_id:
            return None
        try:
            response = await self._request(
                "POST",
                "/memories",
                {
                    "messages": messages,
                    "user_id": self.role_id,
                    "agent_id": self.agent_id,
                },
                60,
            )
        except MemoryLookupError:
            return None
        self._log_http("保存", response)
        return None

    async def query_memory(self, query: str) -> str:
        if not self.role_id:
            raise MemoryLookupError("no_role")
        response = await self._request(
            "POST",
            "/search",
            {
                "query": self._text(query) or "",
                "filters": {"user_id": self.role_id},
                "top_k": self.top_k,
            },
            10,
        )
        self._log_http("查询", response)
        if response.status_code != 200:
            raise MemoryLookupError(f"http_{response.status_code}")
        try:
            data = response.json()
        except Exception:
            raise MemoryLookupError("bad_json") from None
        results = data.get("results", data if isinstance(data, list) else [])
        lines = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            owner = entry.get("user_id")
            if owner and str(owner) != str(self.role_id):
                continue
            memory = entry.get("memory")
            if not memory:
                continue
            stamp = (entry.get("updated_at") or entry.get("created_at") or "")[:16].replace("T", " ")
            lines.append(f"- [{stamp}] {memory}" if stamp else f"- {memory}")
        return "\n".join(lines)

    async def search_own(self, query: str, top_k: int = 3) -> list:
        """Search candidates for the current role. Does not log memory text."""
        if not self.role_id:
            raise MemoryLookupError("no_role")
        response = await self._request(
            "POST",
            "/search",
            {
                "query": query or "",
                "filters": {"user_id": self.role_id},
                "top_k": top_k,
            },
            10,
        )
        self._log_http("候选", response)
        if response.status_code != 200:
            raise MemoryLookupError(f"http_{response.status_code}")
        try:
            data = response.json()
        except Exception:
            raise MemoryLookupError("bad_json") from None
        results = data.get("results", []) if isinstance(data, dict) else []
        owned = []
        for entry in results:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            owner = entry.get("user_id")
            if owner and str(owner) != str(self.role_id):
                continue
            owned.append(
                {
                    "id": str(entry.get("id")),
                    "text": entry.get("memory") if isinstance(entry.get("memory"), str) else "",
                    "owner_in_search": owner is not None,
                }
            )
        return owned

    async def confirm_owner(self, memory_id: str) -> bool:
        """GET /memories/{id} and require user_id to match this role."""
        if not self.role_id or not memory_id:
            return False
        response = await self._request("GET", f"/memories/{memory_id}", None, 10)
        self._log_http("核对", response)
        if response.status_code != 200:
            return False
        try:
            data = response.json()
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        owner = data.get("user_id")
        if owner is None and isinstance(data.get("metadata"), dict):
            owner = data["metadata"].get("user_id")
        return owner is not None and str(owner) == str(self.role_id)

    async def delete_owned(self, memory_id: str) -> bool:
        """Delete only after confirm_owner has already succeeded."""
        if not await self.confirm_owner(memory_id):
            return False
        response = await self._request("DELETE", f"/memories/{memory_id}", None, 10)
        self._log_http("删除", response)
        return response.status_code == 200
