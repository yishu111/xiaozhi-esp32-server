"""Confirm-then-delete memories that belong to this connection's user.

The model only receives record text inside the tool result so it can ask
the user. Logs stay counts and statuses. Nothing is deleted unless the
confirmation token and the id set match the preview exactly.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Optional

from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
_PREVIEW_LIMIT = 5
_TOKEN_TTL_S = 120
_MAX_MISMATCH = 3
_LOGS: list[str] = []

forget_memory_function_desc = {
    "type": "function",
    "function": {
        "name": "forget_memory",
        "description": (
            "当用户明确要求忘掉某一条自己的记忆时调用。"
            "先用 action=preview 按原话查出最多5条属于当前用户的记录，向用户念出内容和确认口令；"
            "只有用户原样复述口令后，才用 action=confirm 连同当时的 memory_ids 再次调用。"
            "禁止删除全部记忆，禁止删除别人的记忆，查询为空时不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "preview 或 confirm",
                },
                "query": {
                    "type": "string",
                    "description": "preview 用的原话，用来定位要忘掉的内容",
                },
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "confirm 时必须与 preview 返回的 id 完全一致",
                },
                "confirmation": {
                    "type": "string",
                    "description": "用户原样复述的确认口令",
                },
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


def _role(conn) -> str:
    memory = getattr(conn, "memory", None)
    role = getattr(memory, "role_id", None) or ""
    device = getattr(conn, "device_id", None) or ""
    if device and role and device != role:
        return ""
    return str(role)


def _as_id_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return None
    ids = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        ids.append(item.strip())
    return ids


def _reply(text: str) -> ActionResponse:
    return ActionResponse(Action.REQLLM, result=text, response=None)


@register_function("forget_memory", forget_memory_function_desc, ToolType.SYSTEM_CTL)
async def forget_memory(
    conn,
    action: str = "",
    query: str = "",
    memory_ids: Any = None,
    confirmation: str = "",
    **_kwargs,
):
    role = _role(conn)
    if not role:
        _log("forget refuse reason=no_role")
        return _reply("没有当前用户身份，不会查看或删除记忆。")

    memory = getattr(conn, "memory", None)
    if memory is None or not hasattr(memory, "list_own_memories") or not hasattr(memory, "delete_own_memories"):
        _log("forget refuse reason=unsupported")
        return _reply(
            "当前记忆模块没有按条删除接口。不会猜测 mem0 自建服务的私有协议，也不会删除任何记录。"
        )

    action_name = (action or "").strip().lower()
    if action_name == "preview":
        return await _preview(conn, memory, role, query or "")
    if action_name == "confirm":
        return await _confirm(conn, memory, role, memory_ids, confirmation or "")
    _log("forget refuse reason=bad_action")
    return _reply("需要 action=preview 或 action=confirm。没有删除任何记录。")


async def _preview(conn, memory, role: str, query: str) -> ActionResponse:
    needle = query.strip()
    if not needle or len(needle) > 200:
        _log("forget preview refuse reason=query")
        return _reply("请用要忘掉的原话做 preview。空查询不会列出或删除记忆。")

    try:
        listed = await memory.list_own_memories(needle)
    except Exception:
        _log("forget preview refuse reason=list_error")
        return _reply("记忆查询失败。没有删除任何记录。")

    if not isinstance(listed, dict) or not listed.get("supported", False):
        _log("forget preview refuse reason=unsupported")
        return _reply("当前记忆后端不支持按条列出。没有删除任何记录。")

    records = listed.get("records") or []
    if not isinstance(records, list):
        _log("forget preview refuse reason=bad_records")
        return _reply("记忆列表无效。没有删除任何记录。")
    if len(records) > _PREVIEW_LIMIT:
        _log(f"forget preview refuse reason=too_many count={len(records)}")
        return _reply(
            f"匹配到 {len(records)} 条，超过 {_PREVIEW_LIMIT} 条上限。请把原话说得更具体。没有删除任何记录。"
        )
    if not records:
        _log("forget preview count=0")
        return _reply("没有找到属于当前用户的匹配记忆。没有删除任何记录。")

    normalized = []
    for record in records:
        if not isinstance(record, dict):
            continue
        rid = record.get("id")
        text = record.get("text")
        if not isinstance(rid, str) or not rid:
            continue
        if not isinstance(text, str):
            text = ""
        normalized.append({"id": rid, "text": text[:200]})
    if not normalized:
        _log("forget preview count=0")
        return _reply("没有可确认的记忆编号。没有删除任何记录。")

    token = secrets.token_hex(3)
    conn._forget_pending = {
        "token": token,
        "role_id": role,
        "ids": tuple(item["id"] for item in normalized),
        "expires": time.monotonic() + _TOKEN_TTL_S,
        "mismatches": 0,
    }
    _log(f"forget preview count={len(normalized)}")
    lines = [f"找到 {len(normalized)} 条当前用户的记忆。请向用户逐条确认，并让用户原样复述口令 {token}。"]
    for item in normalized:
        lines.append(f"id={item['id']} text={item['text']}")
    lines.append("在用户复述口令之前不要调用 confirm。")
    return _reply("\n".join(lines))


async def _confirm(conn, memory, role: str, memory_ids: Any, confirmation: str) -> ActionResponse:
    pending = getattr(conn, "_forget_pending", None)
    if not isinstance(pending, dict):
        _log("forget confirm refuse reason=no_preview")
        return _reply("还没有可确认的预览。没有删除任何记录。")
    if time.monotonic() > float(pending.get("expires", 0)):
        conn._forget_pending = None
        _log("forget confirm refuse reason=expired")
        return _reply("确认已过期。请重新 preview。没有删除任何记录。")
    if pending.get("role_id") != role:
        conn._forget_pending = None
        _log("forget confirm refuse reason=role")
        return _reply("用户身份和预览时不一致。没有删除任何记录。")

    ids = _as_id_list(memory_ids)
    expected = tuple(pending.get("ids") or ())
    token = str(pending.get("token") or "")
    if ids is None or len(ids) != len(expected) or set(ids) != set(expected):
        return _mismatch(conn, pending, "ids")
    if confirmation.strip() != token:
        return _mismatch(conn, pending, "token")

    try:
        outcome = await memory.delete_own_memories(list(expected))
    except Exception:
        _log("forget confirm refuse reason=delete_error")
        return _reply("删除接口报错。请视为没有删掉，需要的话重新 preview。")

    conn._forget_pending = None
    if not isinstance(outcome, dict) or not outcome.get("supported", False):
        _log("forget confirm refuse reason=unsupported")
        return _reply("后端拒绝按条删除。没有删除任何记录。")

    deleted = [str(item) for item in outcome.get("deleted") or []]
    refused = [str(item) for item in outcome.get("refused") or []]
    if set(deleted) - set(expected):
        _log("forget confirm refuse reason=unexpected_id")
        return _reply("后端返回了预览之外的编号。请人工核对，不要继续删除。")
    _log(f"forget confirm deleted={len(deleted)} refused={len(refused)}")
    if refused:
        return _reply(
            f"已删除 {len(deleted)} 条，另有 {len(refused)} 条因身份校验未删除。没有做重置。"
        )
    return _reply(f"已删除 {len(deleted)} 条当前用户的指定记忆。没有删除其他记录。")


def _mismatch(conn, pending: dict, reason: str) -> ActionResponse:
    pending["mismatches"] = int(pending.get("mismatches", 0)) + 1
    if pending["mismatches"] >= _MAX_MISMATCH:
        conn._forget_pending = None
        _log(f"forget confirm refuse reason={reason} cleared=1")
        return _reply("确认没有对上，预览已作废。没有删除任何记录。")
    _log(f"forget confirm refuse reason={reason}")
    return _reply("口令或编号和预览不一致。没有删除任何记录。")
