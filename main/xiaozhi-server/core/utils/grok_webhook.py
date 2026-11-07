"""POST a confirmed task to a routine webhook.

HTTP 200 means the webhook accepted the request, not that the task finished.
A retry after a timeout or 5xx is reported as uncertain: the remote side is
not assumed to be idempotent.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Awaitable, Callable, Optional

MAX_ATTEMPTS_CAP = 3
TIMEOUT_CAP_SECONDS = 10.0

Transport = Callable[[str, str, bytes, float], Awaitable[int]]


def is_unconfigured(value: Optional[str]) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if "你" in text:
        return True
    lowered = text.lower()
    if lowered in {"placeholder", "changeme", "secret", "your-secret", "none", "null"}:
        return True
    if text.startswith("${") or text.startswith("<"):
        return True
    return False


def url_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    if not parsed.hostname:
        return False
    return True


def load_grok_settings(config: Optional[dict]) -> dict:
    plugins = (config or {}).get("plugins") or {}
    raw = plugins.get("submit_grok_task") or {}
    url = os.environ.get("GROK_TASK_WEBHOOK_URL") or raw.get("webhook_url") or ""
    secret = os.environ.get("GROK_TASK_WEBHOOK_SECRET") or raw.get("webhook_secret") or ""
    try:
        attempts = int(raw.get("max_attempts", 2))
    except (TypeError, ValueError):
        attempts = 2
    try:
        timeout = float(raw.get("timeout_seconds", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    attempts = max(1, min(attempts, MAX_ATTEMPTS_CAP))
    timeout = min(max(timeout, 0.2), TIMEOUT_CAP_SECONDS)
    configured = not is_unconfigured(url) and not is_unconfigured(secret) and url_allowed(str(url))
    return {
        "url": str(url).strip(),
        "secret": str(secret),
        "max_attempts": attempts,
        "timeout_seconds": timeout,
        "configured": configured,
    }


def validate_task(task_id: str, text: str, when: str) -> Optional[str]:
    if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
        return "bad_task_id"
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in task_id):
        return "bad_task_id"
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        return "bad_text"
    if not isinstance(when, str) or not when.strip() or len(when) > 80:
        return "bad_time"
    return None


async def _urllib_transport(url: str, secret: str, body: bytes, timeout: float) -> int:
    def _post() -> int:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(256)
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    return await asyncio.to_thread(_post)


async def deliver_task(
    *,
    url: str,
    secret: str,
    task_id: str,
    text: str,
    when: str,
    max_attempts: int,
    timeout_seconds: float,
    transport: Optional[Transport] = None,
) -> dict:
    """Return a delivery report. Never includes the secret or the task text."""
    sender = transport or _urllib_transport
    body = json.dumps(
        {"task_id": task_id, "text": text, "time": when},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    attempts_used = 0
    saw_ambiguous = False
    last_status = None
    capped = max(1, min(int(max_attempts), MAX_ATTEMPTS_CAP))
    timeout = min(float(timeout_seconds), TIMEOUT_CAP_SECONDS)

    for _ in range(capped):
        attempts_used += 1
        try:
            status = int(await sender(url, secret, body, timeout))
        except asyncio.CancelledError:
            return {
                "delivery": "uncertain",
                "http_status": last_status,
                "attempts": attempts_used,
                "ambiguous": True,
            }
        except Exception:
            saw_ambiguous = True
            last_status = None
            continue
        last_status = status
        if status == 200:
            delivery = "accepted_uncertain" if saw_ambiguous or attempts_used > 1 else "accepted"
            return {
                "delivery": delivery,
                "http_status": 200,
                "attempts": attempts_used,
                "ambiguous": delivery != "accepted",
            }
        if 500 <= status <= 599:
            saw_ambiguous = True
            continue
        return {
            "delivery": "rejected",
            "http_status": status,
            "attempts": attempts_used,
            "ambiguous": False,
        }

    return {
        "delivery": "uncertain",
        "http_status": last_status,
        "attempts": attempts_used,
        "ambiguous": True,
    }
