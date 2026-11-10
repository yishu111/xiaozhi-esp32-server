"""Keepalive contrast gates, and the voice client only changes expiry."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tools.keepalive_contrast import _hypothesis
from tools.mem0_keepalive_overlay import EXPECTED_SHA256, apply_source, install, uninstall


def _load_voice_helper():
    path = Path(__file__).resolve().parents[1] / "core/providers/llm/openai/openai.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    segment = ast.get_source_segment(
        text,
        next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_openai_http_client"),
    )
    namespace = {"httpx": httpx, "KEEPALIVE_EXPIRY_S": 120.0}
    exec(segment, namespace)
    return namespace["_openai_http_client"]


def test_voice_client_keeps_pool_sizes_and_timeout(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.DefaultHttpxClient", _Client)
    timeout = httpx.Timeout(300)
    _load_voice_helper()(timeout, "http://example.invalid/v1")
    limits = captured["limits"]
    assert limits.keepalive_expiry == 120.0
    assert limits.max_connections == 1000
    assert limits.max_keepalive_connections == 100
    assert captured["timeout"] == timeout
    assert captured["base_url"] == "http://example.invalid/v1"


def test_sdk_wrapper_records_120s_expiry():
    program = """
import json
import httpx
import openai
from openai._constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT
limits = httpx.Limits(
    max_connections=DEFAULT_CONNECTION_LIMITS.max_connections,
    max_keepalive_connections=DEFAULT_CONNECTION_LIMITS.max_keepalive_connections,
    keepalive_expiry=120.0,
)
http = openai.DefaultHttpxClient(timeout=DEFAULT_TIMEOUT, limits=limits, base_url="http://example.invalid/v1")
pool = http._transport._pool
print(json.dumps({
    "expiry": pool._keepalive_expiry,
    "connections": pool._max_connections,
    "keepalive": pool._max_keepalive_connections,
    "connect": http.timeout.connect,
    "read": http.timeout.read,
}))
http.close()
"""
    completed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "expiry": 120.0,
        "connections": 1000,
        "keepalive": 100,
        "connect": 5.0,
        "read": 600,
    }


def test_hypothesis_gate():
    slow = {"saw_connect": True, "saw_tls": True, "http_attempts": 1, "transport": "direct", "total_ms": 7000, "connection_reused": False}
    fast = {"saw_connect": False, "saw_tls": False, "http_attempts": 1, "transport": "direct", "total_ms": 900, "connection_reused": True}
    assert _hypothesis([slow, slow], [slow, fast]) == "supported"
    assert _hypothesis([slow, fast], [slow, fast]) == "rejected"
    assert _hypothesis([slow, slow], [slow, slow]) == "rejected"
    almost = dict(fast)
    almost["total_ms"] = 6500
    assert _hypothesis([slow, slow], [slow, almost]) == "inconclusive"


def test_overlay_inserts_without_touching_other_lines():
    original = (
        "PASSWORD = 'keep-this-secret-line'\n"
        "def initialize_state(config):\n"
        "    return config\n"
        "initialize_state(DEFAULT_CONFIG)\n"
    )
    updated = apply_source(original)
    assert "keep-this-secret-line" in updated
    assert updated.index("initialize_state(DEFAULT_CONFIG)") < updated.index("_xz_extend_keepalive_expiry()")
    ast.parse(updated)
    with pytest.raises(ValueError):
        apply_source(updated)


def test_overlay_refuses_wrong_hash_and_roundtrips_matching_bytes(tmp_path, monkeypatch):
    target = tmp_path / "main.py"
    target.write_text("initialize_state(DEFAULT_CONFIG)\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        install(target)
    assert "refusing_hash" in str(caught.value)
    assert target.read_text(encoding="utf-8") == "initialize_state(DEFAULT_CONFIG)\n"

    monkeypatch.setattr(
        "tools.mem0_keepalive_overlay.EXPECTED_SHA256",
        __import__("hashlib").sha256(b"initialize_state(DEFAULT_CONFIG)\n").hexdigest(),
    )
    assert EXPECTED_SHA256.startswith("e15b0007")
    assert install(target) == "installed"
    assert "keep-this-secret-line" not in target.read_text(encoding="utf-8")
    assert uninstall(target) == "restored"
    assert target.read_text(encoding="utf-8") == "initialize_state(DEFAULT_CONFIG)\n"
