#!/usr/bin/env python3
"""Trace the SDK client Mem0 or the voice LLM actually constructs.

The OpenAI constructor builds its own httpx wrapper, timeout, limits, and
retry count. This script does not pass another client or another timeout.
It only replaces ``send`` so each attempt appends trace names. The info dict
from httpcore is discarded. A sample is summarized after the SDK call returns,
after the first streamed text token, or after an exception.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

PHASES = (
    "connect_tcp",
    "start_tls",
    "send_request_headers",
    "receive_response_headers",
    "send_request_body",
    "receive_response_body",
)


class TraceLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, float]] = []

    def add(self, name: str) -> None:
        self.events.append((name, time.perf_counter()))

    def clear(self) -> None:
        self.events.clear()

    def summarize(self) -> dict:
        return summarize_events(self.events)


def attach_trace(http_client) -> TraceLog:
    """Record every send on this client without replacing its transport."""
    log = TraceLog()
    original = http_client.send

    def send(request, *args, **kwargs):
        def trace(name, info):
            del info
            log.add(name)

        request.extensions["trace"] = trace
        return original(request, *args, **kwargs)

    http_client.send = send
    return log


def summarize_events(events: list[tuple[str, float]]) -> dict:
    pending: dict[str, list[float]] = {}
    durations: dict[str, list[float]] = {}
    started: dict[str, int] = {}
    for name, stamp in events:
        if name.endswith(".started"):
            key = name[: -len(".started")]
            pending.setdefault(key, []).append(stamp)
            phase = key.rsplit(".", 1)[-1]
            started[phase] = started.get(phase, 0) + 1
            continue
        if name.endswith(".complete") or name.endswith(".failed"):
            key = name.rsplit(".", 1)[0]
            starts = pending.get(key)
            if not starts:
                continue
            phase = key.rsplit(".", 1)[-1]
            durations.setdefault(phase, []).append(round((stamp - starts.pop(0)) * 1000.0, 1))
    incomplete = sorted(key.rsplit(".", 1)[-1] for key, starts in pending.items() if starts)
    report: dict = {"incomplete_phases": incomplete}
    for phase in PHASES:
        if phase in incomplete:
            report[f"{phase}_ms"] = None
        else:
            report[f"{phase}_ms"] = durations.get(phase, [])
    report["http_attempts"] = started.get("send_request_headers", 0)
    report["connection_reused"] = (
        "connect_tcp" not in incomplete
        and "start_tls" not in incomplete
        and not report["connect_tcp_ms"]
        and not report["start_tls_ms"]
    )
    return report


def classify(samples: list[dict]) -> str:
    if not samples:
        return "no_samples"
    if any(item.get("receive_response_headers_ms") is None for item in samples):
        return "unknown"
    if any(item.get("http_attempts", 0) > 1 for item in samples):
        return "retries_inside_call"
    connect = [
        sum(item.get("connect_tcp_ms") or []) + sum(item.get("start_tls_ms") or []) for item in samples
    ]
    ttfb = [sum(item.get("receive_response_headers_ms") or []) for item in samples]
    later_reused = all(item.get("connection_reused") for item in samples[1:]) if len(samples) > 1 else False
    if connect[0] >= 1000 and later_reused and all(value < 500 for value in ttfb[1:]):
        return "cold_connect_then_fast"
    if all(value >= 1000 for value in connect):
        return "connect_or_tls_every_call"
    if all(value >= 1000 for value in ttfb) and all(value < 200 for value in connect):
        return "ttfb"
    return "mixed"


def transport_category(http_client, base_url: str) -> str:
    """direct, http_proxy, socks, or unknown. No proxy address is read."""
    try:
        import httpx

        url = httpx.URL(base_url)
        transport = http_client._transport_for_url(url)
        pool = getattr(transport, "_pool", None)
        if pool is None:
            return "unknown"
        name = type(pool).__name__.lower()
        if "socks" in name:
            return "socks"
        if "proxy" in name:
            return "http_proxy"
        if name == "connectionpool":
            return "direct"
        return "unknown"
    except Exception:
        return "unknown"


def proxy_env_source() -> str:
    present = [name for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy") if os.environ.get(name)]
    if any(name.lower().startswith("https_") for name in present):
        return "env_https"
    if any(name.lower().startswith("all_") for name in present):
        return "env_all"
    if present:
        return "env_http"
    return "none"


def hostname_of(base_url: str) -> str | None:
    return urllib.parse.urlsplit(base_url).hostname


def llm_timeout_from_block(block: dict):
    """Same branches as core.providers.llm.openai.LLMProvider."""
    import httpx

    timeout_config = block.get("timeout")
    if isinstance(timeout_config, dict):
        return httpx.Timeout(
            pool=timeout_config.get("pool", 2.0),
            connect=timeout_config.get("connect", 3.0),
            write=timeout_config.get("write", 5.0),
            read=timeout_config.get("read", 60.0),
        )
    if isinstance(timeout_config, (int, float)) and timeout_config > 0:
        return httpx.Timeout(timeout_config)
    return httpx.Timeout(300)


def make_embed_sdk():
    import openai

    sdk = openai.OpenAI()
    return sdk, attach_trace(sdk._client)


def make_llm_sdk(api_key: str, base_url: str, timeout):
    import openai

    sdk = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    return sdk, attach_trace(sdk._client)


def _close(sdk) -> None:
    closer = getattr(sdk, "close", None)
    if closer:
        closer()


def _sample_after(log: TraceLog, started: float, status, error: str | None, extra: dict) -> dict:
    sample = log.summarize()
    sample.update(extra)
    sample["status"] = status
    sample["error"] = error
    sample["call_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return sample


def embed_samples(model: str, dimensions: int | None, repeats: int) -> dict:
    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or ""
    sdk, log = make_embed_sdk()
    samples = []
    category = "unknown"
    try:
        category = transport_category(sdk._client, str(sdk.base_url))
        for index in range(1, repeats + 1):
            log.clear()
            started = time.perf_counter()
            kwargs = {"input": [f"diag embed {index}"], "model": model}
            if dimensions:
                kwargs["dimensions"] = dimensions
            try:
                created = sdk.embeddings.create(**kwargs)
                status = 200
                vector_len = len(created.data[0].embedding) if created.data else 0
                error = None
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                vector_len = None
                error = type(exc).__name__
            samples.append(_sample_after(log, started, status, error, {"vector_len": vector_len}))
    finally:
        _close(sdk)
    return {
        "kind": "embed",
        "hostname": hostname_of(str(sdk.base_url)),
        "base_url_source": "env" if base_url else "sdk_default",
        "model": model,
        "dimensions": dimensions,
        "max_retries": sdk.max_retries,
        "transport": category,
        "proxy_env_source": proxy_env_source(),
        "samples": samples,
        "classification": classify(samples),
    }


def llm_samples(api_key: str, base_url: str, model: str, timeout, repeats: int) -> dict:
    sdk, log = make_llm_sdk(api_key, base_url, timeout)
    extra = {"enable_thinking": False} if "aliyuncs.com" in (hostname_of(base_url) or "") else None
    samples = []
    category = "unknown"
    try:
        category = transport_category(sdk._client, str(sdk.base_url))
        for _ in range(repeats):
            log.clear()
            started = time.perf_counter()
            first_text_ms = None
            error = None
            status = None
            stream = None
            try:
                kwargs = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with one short word."}],
                    "max_tokens": 1,
                    "stream": True,
                }
                if extra:
                    kwargs["extra_body"] = extra
                stream = sdk.chat.completions.create(**kwargs)
                status = 200
                for chunk in stream:
                    choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
                    text = getattr(getattr(choice, "delta", None), "content", None) if choice else None
                    if text:
                        first_text_ms = round((time.perf_counter() - started) * 1000.0, 1)
                        break
            except Exception as exc:
                error = type(exc).__name__
                status = getattr(getattr(exc, "response", None), "status_code", None)
            finally:
                if stream is not None:
                    stream.close()
            samples.append(
                _sample_after(log, started, status, error, {"first_text_ms": first_text_ms})
            )
    finally:
        _close(sdk)
    return {
        "kind": "llm",
        "hostname": hostname_of(str(sdk.base_url)),
        "model": model,
        "max_retries": sdk.max_retries,
        "transport": category,
        "proxy_env_source": proxy_env_source(),
        "samples": samples,
        "classification": classify(samples),
    }


def llm_settings_from_config(path: Path) -> tuple[str, str, str, object]:
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    selected = loaded["selected_module"]["LLM"]
    block = loaded["LLM"][selected]
    base_url = block.get("base_url") or block.get("url")
    return block["api_key"], base_url, block["model_name"], llm_timeout_from_block(block)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace the production OpenAI client")
    parser.add_argument("--mode", choices=("embed", "llm"), required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dimensions", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=0)
    args = parser.parse_args()
    report = {"credentials_printed": False}
    if args.mode == "embed":
        model = args.model or os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL") or ""
        if not model:
            report["error"] = "model_absent"
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 2
        dims = args.dimensions or (int(os.environ["MEM0_EMBEDDING_DIMS"]) if os.environ.get("MEM0_EMBEDDING_DIMS") else None)
        report.update(embed_samples(model, dims, args.repeats or 3))
    else:
        if args.config:
            api_key, base_url, model, timeout = llm_settings_from_config(args.config)
        else:
            report["error"] = "llm_settings_absent"
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 2
        if not api_key or not base_url or not model:
            report["error"] = "llm_settings_absent"
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 2
        report.update(llm_samples(api_key, base_url, model, timeout, args.repeats or 2))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
