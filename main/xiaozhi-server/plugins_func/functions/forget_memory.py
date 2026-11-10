"""忘掉一条当前用户的记忆。

先搜索候选，再让用户复述口令。删除前用 GET /memories/{id} 核对 user_id，
并且编号必须来自这次预览。不会按模型随口给出的 id 删除。
"""

import secrets
import time

from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
_TOKEN_TTL_S = 120
_MAX_MISMATCH = 3
_LOGS = []

forget_memory_function_desc = {
    "type": "function",
    "function": {
        "name": "forget_memory",
        "description": (
            "当用户要求忘掉关于他自己的某一条记忆时调用。"
            "第一次只传 query，把候选原文和口令念给用户。"
            "用户原样复述口令后，才把当时的 memory_id 和 confirmation 一起传入。"
            "不要删除没有出现在候选里的编号，也不要删除别人的记忆。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要忘掉的内容，例如：我喜欢的颜色"},
                "memory_id": {"type": "string", "description": "用户确认后的候选记忆编号"},
                "confirmation": {"type": "string", "description": "用户原样复述的口令"},
            },
            "required": ["query"],
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
    return ActionResponse(Action.REQLLM, text, None)


@register_function("forget_memory", forget_memory_function_desc, ToolType.SYSTEM_CTL)
async def forget_memory(conn, query: str = "", memory_id: str = None, confirmation: str = "", **_kwargs):
    memory = getattr(conn, "memory", None)
    base_url = getattr(memory, "base_url", None)
    role_id = getattr(memory, "role_id", None)
    if not base_url or not role_id or not hasattr(memory, "search_own"):
        _log("forget refuse reason=not_mem0_selfhost")
        return _reply("当前记忆模块不能按条删除。没有删除任何记录。")
    device_id = getattr(conn, "device_id", None)
    if device_id and device_id != role_id:
        _log("forget refuse reason=role")
        return _reply("用户身份不一致。没有删除任何记录。")
    if memory_id:
        return await _confirm(conn, memory, role_id, str(memory_id), confirmation or "")
    return await _preview(conn, memory, role_id, query or "")


async def _preview(conn, memory, role_id: str, query: str) -> ActionResponse:
    needle = query.strip()
    if not needle or len(needle) > 200:
        _log("forget preview refuse reason=query")
        return _reply("请说出要忘掉的内容。没有删除任何记录。")
    try:
        found = await memory.search_own(needle, top_k=3)
    except Exception:
        _log("forget preview refuse reason=search_error")
        return _reply("记忆查询失败。没有删除任何记录。")
    if not found:
        _log("forget preview count=0")
        return _reply("没有找到属于当前用户的匹配记忆。没有删除任何记录。")
    token = secrets.token_hex(3)
    conn._forget_pending = {
        "token": token,
        "role_id": str(role_id),
        "ids": tuple(item["id"] for item in found),
        "expires": time.monotonic() + _TOKEN_TTL_S,
        "mismatches": 0,
    }
    _log(f"forget preview count={len(found)}")
    lines = [f"找到 {len(found)} 条当前用户的记忆。请让用户原样复述口令 {token}。"]
    for item in found:
        lines.append(f"memory_id={item['id']}：{item['text']}")
    lines.append("在用户复述口令之前不要传 memory_id。")
    return _reply("\n".join(lines))


async def _confirm(conn, memory, role_id: str, memory_id: str, confirmation: str) -> ActionResponse:
    pending = getattr(conn, "_forget_pending", None)
    if not isinstance(pending, dict):
        _log("forget confirm refuse reason=no_preview")
        return _reply("还没有可确认的候选。没有删除任何记录。")
    if time.monotonic() > float(pending.get("expires", 0)):
        conn._forget_pending = None
        _log("forget confirm refuse reason=expired")
        return _reply("确认已过期。没有删除任何记录。")
    if pending.get("role_id") != str(role_id):
        conn._forget_pending = None
        _log("forget confirm refuse reason=role")
        return _reply("用户身份和预览时不一致。没有删除任何记录。")
    if memory_id not in set(pending.get("ids") or ()):
        return _mismatch(conn, pending, "id")
    if confirmation.strip() != str(pending.get("token") or ""):
        return _mismatch(conn, pending, "token")
    try:
        deleted = await memory.delete_owned(memory_id)
    except Exception:
        _log("forget confirm refuse reason=delete_error")
        return _reply("删除前的核对失败。没有删除任何记录。")
    conn._forget_pending = None
    if not deleted:
        _log("forget confirm refuse reason=owner")
        return _reply("后端没有确认这条记忆属于当前用户。没有删除。")
    _log("forget confirm deleted=1")
    return _reply("已删除这条当前用户确认过的记忆。没有删除其他记录。")


def _mismatch(conn, pending: dict, reason: str) -> ActionResponse:
    pending["mismatches"] = int(pending.get("mismatches", 0)) + 1
    if pending["mismatches"] >= _MAX_MISMATCH:
        conn._forget_pending = None
        _log(f"forget confirm refuse reason={reason} cleared=1")
        return _reply("确认没有对上，预览已作废。没有删除任何记录。")
    _log(f"forget confirm refuse reason={reason}")
    return _reply("口令或编号和预览不一致。没有删除任何记录。")
