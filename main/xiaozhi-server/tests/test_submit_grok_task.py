"""Grok task webhook: confirmation, bounded retries, no live call."""

import asyncio
import json

import pytest

from core.utils.grok_webhook import deliver_task, load_grok_settings
from plugins_func.functions import submit_grok_task


SECRET = "test-secret-not-real"
URL = "http://127.0.0.1:9/routine"


class _Log:
    def bind(self, **_kwargs):
        return self

    def info(self, _message):
        return None


def _config():
    return {
        "plugins": {
            "submit_grok_task": {
                "webhook_url": URL,
                "webhook_secret": SECRET,
                "timeout_seconds": 5,
                "max_attempts": 2,
            }
        }
    }


def _conn(transport=None, config=None):
    conn = type("Conn", (), {})()
    conn.config = config if config is not None else _config()
    conn.logger = _Log()
    conn._grok_transport = transport
    return conn


@pytest.fixture(autouse=True)
def _clear_grok_env(monkeypatch):
    submit_grok_task._LOGS.clear()
    monkeypatch.delenv("GROK_TASK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("GROK_TASK_WEBHOOK_SECRET", raising=False)


def test_placeholder_and_preview_do_not_call_the_webhook():
    called = {"n": 0}

    async def transport(*_args):
        called["n"] += 1
        return 200

    settings = load_grok_settings(
        {"plugins": {"submit_grok_task": {"webhook_url": "你的routine webhook地址", "webhook_secret": "你的webhook密钥占位符"}}}
    )
    assert settings["configured"] is False
    conn = _conn(transport, {"plugins": {"submit_grok_task": {"webhook_url": "你的地址", "webhook_secret": "占位"}}})
    preview = asyncio.run(submit_grok_task.submit_grok_task(conn, action="preview", task_id="t1", text="buy milk", time="tonight"))
    assert "没有发送" in preview.result
    assert called["n"] == 0

    armed = _conn(transport)
    armed_preview = asyncio.run(
        submit_grok_task.submit_grok_task(armed, action="preview", task_id="task_1", text="buy milk", time="tonight")
    )
    assert called["n"] == 0
    assert "口令" in armed_preview.result
    assert all("buy milk" not in line for line in submit_grok_task._LOGS)


def test_confirm_posts_only_task_fields_and_treats_200_as_accepted():
    seen = []

    async def transport(url, secret, body, timeout):
        seen.append((url, secret, body, timeout))
        return 200

    conn = _conn(transport)
    preview = asyncio.run(
        submit_grok_task.submit_grok_task(conn, action="preview", task_id="task_1", text="buy milk", time="21:00")
    )
    token = preview.result.split("口令 ", 1)[1].split("。", 1)[0]
    mismatch = asyncio.run(
        submit_grok_task.submit_grok_task(
            conn,
            action="confirm",
            task_id="task_1",
            text="buy milk",
            time="22:00",
            confirmation=token,
        )
    )
    assert seen == []
    assert "没有发送" in mismatch.result
    preview = asyncio.run(
        submit_grok_task.submit_grok_task(conn, action="preview", task_id="task_1", text="buy milk", time="21:00")
    )
    token = preview.result.split("口令 ", 1)[1].split("。", 1)[0]
    accepted = asyncio.run(
        submit_grok_task.submit_grok_task(
            conn,
            action="confirm",
            task_id="task_1",
            text="buy milk",
            time="21:00",
            confirmation=token,
        )
    )
    assert "还没有完成" in accepted.result
    assert len(seen) == 1
    url, secret, body, _timeout = seen[0]
    assert url == URL
    assert secret == SECRET
    payload = json.loads(body.decode("utf-8"))
    assert payload == {"task_id": "task_1", "text": "buy milk", "time": "21:00"}
    assert SECRET not in accepted.result
    assert all(SECRET not in line and "buy milk" not in line for line in submit_grok_task._LOGS)


def test_retry_is_bounded_and_does_not_claim_exactly_once():
    async def ambiguous(url, secret, body, timeout):
        ambiguous.calls += 1
        if ambiguous.calls == 1:
            raise TimeoutError("slow")
        return 200

    ambiguous.calls = 0
    report = asyncio.run(
        deliver_task(
            url=URL,
            secret=SECRET,
            task_id="task_1",
            text="buy milk",
            when="21:00",
            max_attempts=99,
            timeout_seconds=99,
            transport=ambiguous,
        )
    )
    assert ambiguous.calls == 2
    assert report["delivery"] == "accepted_uncertain"
    assert report["http_status"] == 200
    assert "buy milk" not in str(report)
    assert SECRET not in str(report)

    async def rejected(*_args):
        rejected.calls += 1
        return 400

    rejected.calls = 0
    denied = asyncio.run(
        deliver_task(
            url=URL,
            secret=SECRET,
            task_id="task_1",
            text="buy milk",
            when="21:00",
            max_attempts=3,
            timeout_seconds=1,
            transport=rejected,
        )
    )
    assert rejected.calls == 1
    assert denied["delivery"] == "rejected"

    async def cancelled(*_args):
        raise asyncio.CancelledError()

    stopped = asyncio.run(
        deliver_task(
            url=URL,
            secret=SECRET,
            task_id="task_1",
            text="buy milk",
            when="21:00",
            max_attempts=3,
            timeout_seconds=1,
            transport=cancelled,
        )
    )
    assert stopped["delivery"] == "uncertain"
    assert stopped["attempts"] == 1
