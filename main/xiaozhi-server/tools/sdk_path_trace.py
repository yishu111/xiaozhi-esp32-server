#!/usr/bin/env python3
"""Time one reused SDK client on the process's normal proxy path.

Does not construct Memory, install a patch, or change environment variables.
The trace callback stores event names and clocks only. It does not keep the
info dict, so hosts, headers, and bodies from httpcore are not recorded.
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


def proxy_report() -> dict:
    present = []
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(name):
            present.append(name)
    if any(name.lower().startswith("https_") for name in present):
        source = "env_https"
    elif any(name.lower().startswith("all_") for name in present):
        source = "env_all"
    elif present:
        source = "env_http"
    else:
        source = "none"
    return {"proxy_enabled": bool(present), "proxy_source": source, "trust_env": True}


def hostname_of(base_url: str) -> str | None:
    return urllib.parse.urlsplit(base_url).hostname


def summarize_events(events: list[tuple[str, float]]) -> dict:
    pending: dict[str, list[float]] = {}
    durations: dict[str, list[float]] = {}
    for name, stamp in events:
        if name.endswith(".started"):
            pending.setdefault(name[: -len(".started")], []).append(stamp)
            continue
        if name.endswith(".complete") or name.endswith(".failed"):
            key = name.rsplit(".", 1)[0]
            starts = pending.get(key)
            if not starts:
                continue
            phase = key.rsplit(".", 1)[-1]
            durations.setdefault(phase, []).append(round((stamp - starts.pop(0)) * 1000.0, 1))
    report = {f"{phase}_ms": durations.get(phase, []) for phase in PHASES}
    report["http_attempts"] = len(report["send_request_headers_ms"])
    report["connection_reused"] = not report["connect_tcp_ms"] and not report["start_tls_ms"]
    return report


def classify(samples: list[dict]) -> str:
    """Map traced calls to the next action. Thresholds are the returned numbers."""
    if not samples:
        return "no_samples"
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


class TracingClient:
    """httpx.Client subclass created per process, not per request."""

    def __new__(cls, **kwargs):
        import httpx

        class _Client(httpx.Client):
            def send(self, request, **send_kwargs):
                events: list[tuple[str, float]] = []

                def trace(name, info):
                    del info
                    events.append((name, time.perf_counter()))

                request.extensions["trace"] = trace
                try:
                    return super().send(request, **send_kwargs)
                finally:
                    self.last_trace = summarize_events(events)

        kwargs.setdefault("trust_env", True)
        kwargs.setdefault("timeout", 60.0)
        return _Client(**kwargs)


def _close(client) -> None:
    closer = getattr(client, "close", None)
    if closer:
        closer()


def embed_samples(model: str, dimensions: int | None, repeats: int) -> dict:
    import httpx
    import openai

    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or ""
    http_client = TracingClient()
    sdk = openai.OpenAI(http_client=http_client, timeout=httpx.Timeout(60.0))
    texts = [f"diag embed {index}" for index in range(1, repeats + 1)]
    samples = []
    try:
        for text in texts:
            started = time.perf_counter()
            kwargs = {"input": [text], "model": model}
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
            sample = dict(getattr(http_client, "last_trace", {}))
            sample.update(
                {
                    "status": status,
                    "vector_len": vector_len,
                    "error": error,
                    "call_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
            )
            samples.append(sample)
    finally:
        _close(sdk)
        _close(http_client)
    return {
        "kind": "embed",
        "hostname": hostname_of(base_url),
        "model": model,
        "dimensions": dimensions,
        "samples": samples,
        "classification": classify(samples),
    }


def llm_samples(api_key: str, base_url: str, model: str, repeats: int) -> dict:
    import httpx
    import openai

    http_client = TracingClient()
    sdk = openai.OpenAI(api_key=api_key, base_url=base_url, http_client=http_client, timeout=httpx.Timeout(60.0))
    extra = {"enable_thinking": False} if "aliyuncs.com" in (hostname_of(base_url) or "") else None
    samples = []
    try:
        for _ in range(repeats):
            started = time.perf_counter()
            first_text_ms = None
            error = None
            status = None
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
                stream.close()
            except Exception as exc:
                error = type(exc).__name__
                status = getattr(getattr(exc, "response", None), "status_code", None)
            sample = dict(getattr(http_client, "last_trace", {}))
            sample.update(
                {
                    "status": status,
                    "error": error,
                    "first_text_ms": first_text_ms,
                    "call_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
            )
            samples.append(sample)
    finally:
        _close(sdk)
        _close(http_client)
    return {
        "kind": "llm",
        "hostname": hostname_of(base_url),
        "model": model,
        "samples": samples,
        "classification": classify(samples),
    }


def llm_settings_from_config(path: Path) -> tuple[str, str, str]:
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    selected = loaded["selected_module"]["LLM"]
    block = loaded["LLM"][selected]
    base_url = block.get("base_url") or block.get("url")
    return block["api_key"], base_url, block["model_name"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace the existing SDK connection path")
    parser.add_argument("--mode", choices=("embed", "llm"), required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dimensions", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=0)
    args = parser.parse_args()
    report = {"credentials_printed": False, "proxy": proxy_report()}
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
            api_key, base_url, model = llm_settings_from_config(args.config)
        else:
            api_key = os.environ.get("OPENAI_API_KEY") or ""
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or ""
            model = args.model
        if not api_key or not base_url or not model:
            report["error"] = "llm_settings_absent"
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 2
        os.environ["OPENAI_API_KEY"] = api_key
        report.update(llm_samples(api_key, base_url, model, args.repeats or 2))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
