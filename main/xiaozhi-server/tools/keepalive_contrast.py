#!/usr/bin/env python3
"""Compare the SDK's own keepalive expiry with the same pool at 120 seconds.

One client opens a connection, waits longer than the default expiry and
shorter than 120s, then makes a second call. A second client changes only
keepalive_expiry. LLM streams are read until the iterator ends.

Does not change endpoint, proxy, model, retries, or memory. Prints timings,
attempt counts, and transport class only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

CANDIDATE_EXPIRY_S = 120.0
IDLE_S = 9.0


def _hostname(base_url: str) -> str:
    return urllib.parse.urlsplit(base_url).hostname or ""


def transport_category(http_client, base_url: str) -> str:
    try:
        import httpx

        transport = http_client._transport_for_url(httpx.URL(base_url))
        pool = getattr(transport, "_pool", None)
        name = type(pool).__name__.lower() if pool is not None else ""
        if "socks" in name:
            return "socks"
        if "proxy" in name:
            return "http_proxy"
        if name == "connectionpool":
            return "direct"
    except Exception:
        return "unknown"
    return "unknown"


def pool_for(http_client, base_url: str):
    import httpx

    transport = http_client._transport_for_url(httpx.URL(base_url))
    return getattr(transport, "_pool", None)


class _Trace:
    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self, name, info) -> None:
        del info
        self.events.append(name)

    def saw(self, needle: str) -> bool:
        return any(needle in name and name.endswith(".started") for name in self.events)

    def attempts(self) -> int:
        return sum(1 for name in self.events if name.endswith("send_request_headers.started"))


def _attach(http_client, traces: list[_Trace]):
    original = http_client.send

    def send(request, *args, **kwargs):
        tracer = _Trace()
        traces.append(tracer)
        request.extensions["trace"] = tracer
        return original(request, *args, **kwargs)

    http_client.send = send


def _limits_from_pool(pool, expiry: float):
    import httpx

    return httpx.Limits(
        max_connections=pool._max_connections,
        max_keepalive_connections=pool._max_keepalive_connections,
        keepalive_expiry=expiry,
    )


def _extended_client(sdk, timeout):
    """Same SDK wrapper and pool sizes. Only keepalive_expiry changes."""
    import openai

    pool = pool_for(sdk._client, str(sdk.base_url))
    if pool is None or not hasattr(pool, "_keepalive_expiry"):
        raise RuntimeError("keepalive_unread")
    http = openai.DefaultHttpxClient(
        timeout=timeout,
        limits=_limits_from_pool(pool, CANDIDATE_EXPIRY_S),
        base_url=str(sdk.base_url),
    )
    return openai.OpenAI(
        api_key=sdk.api_key,
        base_url=str(sdk.base_url),
        timeout=timeout,
        max_retries=sdk.max_retries,
        http_client=http,
    )


def _call_record(trace: _Trace, started: float, transport: str) -> dict:
    return {
        "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "saw_connect": trace.saw("connect_tcp"),
        "saw_tls": trace.saw("start_tls"),
        "http_attempts": trace.attempts(),
        "transport": transport,
        "connection_reused": not trace.saw("connect_tcp") and not trace.saw("start_tls"),
    }


def _hypothesis(default_calls: list[dict], extended_calls: list[dict]) -> str:
    if len(default_calls) != 2 or len(extended_calls) != 2:
        return "inconclusive"
    if any(item["http_attempts"] != 1 or item["transport"] == "unknown" for item in default_calls + extended_calls):
        return "inconclusive"
    default_second = default_calls[1]
    extended_second = extended_calls[1]
    if not (default_second["saw_tls"] or default_second["saw_connect"]):
        return "rejected"
    if extended_second["saw_tls"] or extended_second["saw_connect"]:
        return "rejected"
    if extended_second["total_ms"] > default_second["total_ms"] - 1000:
        return "inconclusive"
    return "supported"


def _run_pair(make_default, call, idle_s: float) -> dict:
    default = make_default()
    pool = pool_for(default._client, str(default.base_url))
    expiry = getattr(pool, "_keepalive_expiry", None)
    if not isinstance(expiry, (int, float)) or not (expiry < idle_s < CANDIDATE_EXPIRY_S):
        default.close()
        return {"error": "idle_not_between_expiries", "keepalive_expiry_default_s": expiry, "idle_s": idle_s}
    timeout = default._client.timeout
    extended = _extended_client(default, timeout)
    groups = []
    try:
        for name, sdk, expected in (
            ("default", default, expiry),
            ("expiry_120", extended, CANDIDATE_EXPIRY_S),
        ):
            traces: list[_Trace] = []
            _attach(sdk._client, traces)
            transport = transport_category(sdk._client, str(sdk.base_url))
            calls = []
            for index in (1, 2):
                before = len(traces)
                started = time.perf_counter()
                call(sdk, index)
                if len(traces) == before:
                    calls.append(
                        {
                            "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
                            "saw_connect": None,
                            "saw_tls": None,
                            "http_attempts": 0,
                            "transport": transport,
                            "connection_reused": None,
                        }
                    )
                else:
                    calls.append(_call_record(traces[-1], started, transport))
                if index == 1:
                    time.sleep(idle_s)
            live_pool = pool_for(sdk._client, str(sdk.base_url))
            groups.append(
                {
                    "name": name,
                    "keepalive_expiry_s": getattr(live_pool, "_keepalive_expiry", None),
                    "expected_keepalive_expiry_s": expected,
                    "calls": calls,
                }
            )
    finally:
        default.close()
        extended.close()
    return {
        "idle_s": idle_s,
        "groups": groups,
        "hypothesis": _hypothesis(groups[0]["calls"], groups[1]["calls"]),
    }


def _embed_call(sdk, index: int) -> None:
    model = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL") or os.environ["KEEPALIVE_EMBED_MODEL"]
    kwargs = {"input": [f"keepalive contrast {index} {time.time_ns()}"], "model": model}
    dims = os.environ.get("MEM0_EMBEDDING_DIMS") or os.environ.get("KEEPALIVE_EMBED_DIMS")
    if dims:
        kwargs["dimensions"] = int(dims)
    created = sdk.embeddings.create(**kwargs)
    if not created.data or not created.data[0].embedding:
        raise RuntimeError("empty_embedding")


def _llm_call(sdk, index: int) -> None:
    del index
    model = os.environ["KEEPALIVE_LLM_MODEL"]
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with one short word."}],
        "max_tokens": 1,
        "stream": True,
    }
    if "aliyuncs.com" in _hostname(str(sdk.base_url)):
        kwargs["extra_body"] = {"enable_thinking": False}
    stream = sdk.chat.completions.create(**kwargs)
    try:
        for _chunk in stream:
            pass
    finally:
        stream.close()


def _llm_timeout_from_config(path: Path):
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = loaded["LLM"][loaded["selected_module"]["LLM"]]
    os.environ["KEEPALIVE_LLM_MODEL"] = block["model_name"]
    timeout_config = block.get("timeout")
    import httpx

    if isinstance(timeout_config, dict):
        timeout = httpx.Timeout(
            pool=timeout_config.get("pool", 2.0),
            connect=timeout_config.get("connect", 3.0),
            write=timeout_config.get("write", 5.0),
            read=timeout_config.get("read", 60.0),
        )
    elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
        timeout = httpx.Timeout(timeout_config)
    else:
        timeout = httpx.Timeout(300)
    return block["api_key"], block.get("base_url") or block.get("url"), timeout


def main() -> int:
    parser = argparse.ArgumentParser(description="Default keepalive versus 120 seconds")
    parser.add_argument("--mode", choices=("embed", "llm"), required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--idle", type=float, default=IDLE_S)
    args = parser.parse_args()
    import openai

    if args.mode == "embed":
        if not (os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL") or os.environ.get("KEEPALIVE_EMBED_MODEL")):
            json.dump({"error": "model_absent"}, sys.stdout)
            sys.stdout.write("\n")
            return 2

        def make_default():
            return openai.OpenAI()

        report = _run_pair(make_default, _embed_call, args.idle)
    else:
        if not args.config:
            json.dump({"error": "config_absent"}, sys.stdout)
            sys.stdout.write("\n")
            return 2
        api_key, base_url, timeout = _llm_timeout_from_config(args.config)

        def make_default():
            return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

        report = _run_pair(make_default, _llm_call, args.idle)
    report["mode"] = args.mode
    report["credentials_printed"] = False
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report.get("hypothesis") else 2


if __name__ == "__main__":
    raise SystemExit(main())
