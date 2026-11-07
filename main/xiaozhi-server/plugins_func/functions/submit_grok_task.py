"""Explicit handoff of one task to the Grok Bot routine webhook.

Ordinary turns never call this. The tool arguments are untrusted: nothing
is sent until the user repeats a one-time confirmation token for the same
task_id, text, and time. HTTP 200 is accepted, not completed.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Optional

from core.utils.grok_webhook import deliver_task, load_grok_settings, validate_task
from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
_TOKEN_TTL_S = 120
_MAX_MISMATCH = 3
_LOGS: list[str] = []

submit_grok_task_function_desc = {
    "type": "function",
    "function": {
        "name": "submit_grok_task",
        "description": (
            "仅当用户明确要求把一件待办交给「Taky 的灵魂」时调用。"
            "普通聊天、问答、记忆、设备控制都不要调用。"
            "先 action=preview，向用户复述 task_id、事项和时间，并索取确认口令；"
            "用户原样复述后才 action=confirm。"
            "确认后只表示 webhook 接受了请求，不表示事项已完成，结果留在机器人对话里。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "preview 或 confirm"},
                "task_id": {"type": "string", "description": "短任务编号，字母数字下划线或横线"},
                "text": {"type": "string", "description": "要送出的事项原文"},
                "time": {"type": "string", "description": "用户说的时间，原样传递"},
                "confirmation": {"type": "string", "description": "用户原样复述的确认口令"},
            },
            "required": ["action"],
        },
    },
}


def _log(message: str) -> None:
    _LOGS.append(message)
    try:
        from config.logger import setup_logging

        setup_logging().bind(tag=TAG).info(message)
    except Exception:
        return


def _reply(text: str) -> ActionResponse:
    return ActionResponse(Action.REQLLM, result=text, response=None)


def _fingerprint(task_id: str, text: str, when: str) -> str:
    raw = f"{task_id}\n{text}\n{when}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@register_function("submit_grok_task", submit_grok_task_function_desc, ToolType.SYSTEM_CTL)
async def submit_grok_task(
    conn,
    action: str = "",
    task_id: str = "",
    text: str = "",
    time: str = "",
    confirmation: str = "",
    **_kwargs,
):
    action_name = (action or "").strip().lower()
    if action_name == "preview":
        return _preview(conn, task_id or "", text or "", time or "")
    if action_name == "confirm":
        return await _confirm(conn, task_id or "", text or "", time or "", confirmation or "")
    _log("grok refuse reason=bad_action")
    return _reply("需要 action=preview 或 action=confirm。没有向外发送。")


def _preview(conn, task_id: str, text: str, when: str) -> ActionResponse:
    settings = load_grok_settings(getattr(conn, "config", None))
    if not settings["configured"]:
        _log("grok preview refuse reason=unconfigured")
        return _reply("Grok 任务 webhook 还是占位配置，没有发送。")
    problem = validate_task(task_id, text, when)
    if problem:
        _log(f"grok preview refuse reason={problem}")
        return _reply("任务编号、内容或时间不符合长度限制。没有发送。")
    token = secrets.token_hex(3)
    conn._grok_task_pending = {
        "token": token,
        "fingerprint": _fingerprint(task_id, text, when),
        "task_id": task_id,
        "text": text,
        "time": when,
        "expires": time_monotonic() + _TOKEN_TTL_S,
        "mismatches": 0,
    }
    _log(f"grok preview task_len={len(task_id)} text_len={len(text)}")
    return _reply(
        "准备交给「Taky 的灵魂」。请向用户复述事项和时间，并让用户原样复述口令 "
        f"{token}。在此之前不要 confirm，也不要声称已经送出。"
        f"\ntask_id={task_id}\ntext={text}\ntime={when}"
    )


async def _confirm(conn, task_id: str, text: str, when: str, confirmation: str) -> ActionResponse:
    settings = load_grok_settings(getattr(conn, "config", None))
    if not settings["configured"]:
        _log("grok confirm refuse reason=unconfigured")
        return _reply("Grok 任务 webhook 还是占位配置，没有发送。")
    pending = getattr(conn, "_grok_task_pending", None)
    if not isinstance(pending, dict):
        _log("grok confirm refuse reason=no_preview")
        return _reply("还没有待确认的任务。没有发送。")
    if time_monotonic() > float(pending.get("expires", 0)):
        conn._grok_task_pending = None
        _log("grok confirm refuse reason=expired")
        return _reply("确认已过期。没有发送。")
    problem = validate_task(task_id, text, when)
    if problem or _fingerprint(task_id, text, when) != pending.get("fingerprint"):
        return _mismatch(conn, pending, "payload")
    if confirmation.strip() != str(pending.get("token") or ""):
        return _mismatch(conn, pending, "token")

    conn._grok_task_pending = None
    transport = getattr(conn, "_grok_transport", None)
    report = await deliver_task(
        url=settings["url"],
        secret=settings["secret"],
        task_id=task_id,
        text=text,
        when=when,
        max_attempts=settings["max_attempts"],
        timeout_seconds=settings["timeout_seconds"],
        transport=transport,
    )
    _log(
        "grok deliver delivery={delivery} http_status={http_status} attempts={attempts}".format(
            delivery=report.get("delivery"),
            http_status=report.get("http_status"),
            attempts=report.get("attempts"),
        )
    )
    return _reply(_spoken(report))


def _mismatch(conn, pending: dict, reason: str) -> ActionResponse:
    pending["mismatches"] = int(pending.get("mismatches", 0)) + 1
    if pending["mismatches"] >= _MAX_MISMATCH:
        conn._grok_task_pending = None
        _log(f"grok confirm refuse reason={reason} cleared=1")
        return _reply("确认内容和预览不一致，已作废。没有发送。")
    _log(f"grok confirm refuse reason={reason}")
    return _reply("口令或事项和预览不一致。没有发送。")


def _spoken(report: dict) -> str:
    delivery = report.get("delivery")
    if delivery == "accepted":
        return (
            "webhook 返回 200，只表示接受了请求，事项还没有完成。"
            "结果先留在「Taky 的灵魂」的对话里，语音这里没有回调。"
        )
    if delivery == "accepted_uncertain":
        return (
            "后来的请求返回 200，但前面有过超时或服务器错误，对方不保证去重，"
            "所以可能送出了一次，也可能多于一次。事项仍未完成，结果在机器人对话里。"
        )
    if delivery == "rejected":
        return f"webhook 拒绝了这次请求（HTTP {report.get('http_status')}）。没有重试。事项未送出。"
    return "发送结果不确定。没有假定对方会去重，请不要立刻再送同一件。事项不能算完成。"


def time_monotonic() -> float:
    return time.monotonic()
