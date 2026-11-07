#!/usr/bin/env python3
"""Split certificate-context construction from TCP and from the TLS handshake.

The previous probe folded ssl.create_default_context() into handshake_ms.
httpx 0.28.1 and edge_tts 7.2.6 do not call that zero-argument constructor.
Both call ssl.create_default_context with the certifi CA file, unless
SSL_CERT_FILE or SSL_CERT_DIR is set for httpx. This script times those
calls separately. It does not send an HTTP request or a credential.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import socket
import ssl
import sys
import time


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def context_samples(repeat: int) -> dict:
    """Time context constructors only. No socket is opened."""
    repeat = max(1, min(int(repeat), 5))
    system_ms = []
    certifi_where_ms = []
    certifi_context_ms = []
    certifi_error = None
    cafile = None
    try:
        import certifi
    except Exception as exc:
        certifi = None
        certifi_error = type(exc).__name__

    for _ in range(repeat):
        started = time.perf_counter()
        try:
            ssl.create_default_context()
            system_ms.append(_ms(started))
        except Exception as exc:
            system_ms.append({"ms": _ms(started), "error": type(exc).__name__})
        if certifi is None:
            continue
        started = time.perf_counter()
        try:
            cafile = certifi.where()
            certifi_where_ms.append(_ms(started))
        except Exception as exc:
            certifi_where_ms.append({"ms": _ms(started), "error": type(exc).__name__})
            continue
        started = time.perf_counter()
        try:
            ssl.create_default_context(cafile=cafile)
            certifi_context_ms.append(_ms(started))
        except Exception as exc:
            certifi_context_ms.append({"ms": _ms(started), "error": type(exc).__name__})

    report = {
        "system_context_ms": system_ms,
        "certifi_importable": certifi is not None,
        "certifi_where_ms": certifi_where_ms,
        "certifi_context_ms": certifi_context_ms,
        "ssl_cert_file_set": bool(os.environ.get("SSL_CERT_FILE")),
        "ssl_cert_dir_set": bool(os.environ.get("SSL_CERT_DIR")),
        "ca_bytes": None,
    }
    if certifi_error:
        report["certifi_error"] = certifi_error
    if cafile:
        try:
            report["ca_bytes"] = os.path.getsize(cafile)
        except OSError as exc:
            report["ca_bytes_error"] = type(exc).__name__
    return report


def _resolve(host: str, port: int) -> dict:
    started = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        return {"ok": False, "dns_ms": _ms(started), "error": type(exc).__name__}
    if not infos:
        return {"ok": False, "dns_ms": _ms(started), "error": "empty"}
    family, _, _, _, sockaddr = infos[0]
    return {"ok": True, "dns_ms": _ms(started), "family": family, "sockaddr": sockaddr}


def handshake_samples(host: str, port: int, repeat: int, timeout: float) -> dict:
    """TCP and wrap_socket on one already-built certifi context, then a fresh context."""
    repeat = max(1, min(int(repeat), 5))
    try:
        import certifi
    except Exception as exc:
        return {"skipped": True, "reason": type(exc).__name__}

    started = time.perf_counter()
    shared = ssl.create_default_context(cafile=certifi.where())
    shared_build_ms = _ms(started)
    reused = []
    fresh = []
    for _ in range(repeat):
        resolved = _resolve(host, port)
        if not resolved["ok"]:
            reused.append({"ok": False, "dns_ms": resolved["dns_ms"], "error": resolved["error"]})
            continue
        reused.append(
            _one_handshake(
                host, resolved["family"], resolved["sockaddr"], shared, timeout, resolved["dns_ms"]
            )
        )
        started = time.perf_counter()
        try:
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception as exc:
            fresh.append({"ok": False, "context_ms": _ms(started), "error": type(exc).__name__})
            continue
        context_ms = _ms(started)
        resolved = _resolve(host, port)
        if not resolved["ok"]:
            fresh.append(
                {
                    "ok": False,
                    "context_ms": context_ms,
                    "dns_ms": resolved["dns_ms"],
                    "error": resolved["error"],
                }
            )
            continue
        sample = _one_handshake(
            host, resolved["family"], resolved["sockaddr"], ctx, timeout, resolved["dns_ms"]
        )
        sample["context_ms"] = context_ms
        fresh.append(sample)
    return {
        "shared_context_build_ms": shared_build_ms,
        "reused_context": reused,
        "fresh_context_each_time": fresh,
    }


def _one_handshake(host: str, family: int, sockaddr, context, timeout: float, dns_ms: float) -> dict:
    raw = None
    tcp_ms = None
    try:
        raw = socket.socket(family)
        raw.settimeout(timeout)
        started = time.perf_counter()
        raw.connect(sockaddr)
        tcp_ms = _ms(started)
        started = time.perf_counter()
        tls = context.wrap_socket(raw, server_hostname=host)
        handshake_ms = _ms(started)
        tls.close()
        raw = None
        return {"ok": True, "dns_ms": dns_ms, "tcp_ms": tcp_ms, "tls_handshake_ms": handshake_ms}
    except Exception as exc:
        return {"ok": False, "dns_ms": dns_ms, "tcp_ms": tcp_ms, "error": type(exc).__name__}
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass


def sdk_build_samples(repeat: int) -> dict:
    """Construct the same client objects the SDKs construct. Do not send a request."""
    repeat = max(1, min(int(repeat), 5))
    report = {"request_sent": False}
    try:
        import httpx
        from httpx._config import create_ssl_context
    except Exception as exc:
        report["httpx_importable"] = False
        report["httpx_error"] = type(exc).__name__
        return report

    context_ms = []
    client_ms = []
    for _ in range(repeat):
        started = time.perf_counter()
        create_ssl_context(verify=True)
        context_ms.append(_ms(started))
        started = time.perf_counter()
        client = httpx.Client()
        client_ms.append(_ms(started))
        client.close()
    report.update(
        {
            "httpx_importable": True,
            "httpx_version": getattr(httpx, "__version__", None),
            "httpx_create_ssl_context_ms": context_ms,
            "httpx_client_construct_ms": client_ms,
        }
    )
    try:
        import openai
    except Exception as exc:
        report["openai_importable"] = False
        report["openai_error"] = type(exc).__name__
        return report
    openai_ms = []
    for _ in range(repeat):
        started = time.perf_counter()
        client = openai.OpenAI(api_key="diagnostic-not-a-secret", base_url="http://127.0.0.1:9/v1")
        openai_ms.append(_ms(started))
        close = getattr(client, "close", None)
        if close is not None:
            close()
    report["openai_importable"] = True
    report["openai_version"] = getattr(openai, "__version__", None)
    report["openai_client_construct_ms"] = openai_ms
    return report


def _calls(source: str, needle: str) -> int:
    return source.count(needle)


def library_facts() -> dict:
    """Read installed sources. The booleans are call-site counts, not timings."""
    facts = {}
    try:
        import httpx
        from httpx._transports.default import HTTPTransport

        facts["httpx_version"] = getattr(httpx, "__version__", None)
        facts["transport_init_create_ssl_context"] = _calls(inspect.getsource(HTTPTransport.__init__), "create_ssl_context")
        facts["transport_handle_request_create_ssl_context"] = _calls(
            inspect.getsource(HTTPTransport.handle_request), "create_ssl_context"
        )
    except Exception as exc:
        facts["httpx_error"] = type(exc).__name__

    try:
        import openai
        from openai._base_client import SyncAPIClient

        facts["openai_version"] = getattr(openai, "__version__", None)
        init_source = inspect.getsource(SyncAPIClient.__init__)
        facts["openai_sync_client_builds_http_client"] = "SyncHttpxClientWrapper(" in init_source
    except Exception as exc:
        facts["openai_error"] = type(exc).__name__

    try:
        import mem0.embeddings.openai as embedder

        init_source = inspect.getsource(embedder.OpenAIEmbedding.__init__)
        embed_source = inspect.getsource(embedder.OpenAIEmbedding.embed)
        facts["mem0_embedder_builds_client_in_init"] = "OpenAI(" in init_source
        facts["mem0_embed_builds_client"] = "OpenAI(" in embed_source
        facts["mem0_embed_reuses_self_client"] = "self.client.embeddings.create" in embed_source
    except Exception as exc:
        facts["mem0_embedder_error"] = type(exc).__name__

    try:
        import edge_tts

        stream_source = inspect.getsource(edge_tts.Communicate.stream)
        init_source = inspect.getsource(edge_tts.Communicate.__init__)
        inner = getattr(edge_tts.Communicate, "_Communicate__stream", None)
        inner_source = inspect.getsource(inner) if inner is not None else ""
        facts["edge_tts_version"] = getattr(edge_tts, "__version__", None)
        facts["edge_stream_create_default_context"] = _calls(stream_source, "create_default_context")
        facts["edge_init_create_default_context"] = _calls(init_source, "create_default_context")
        facts["edge_inner_stream_create_default_context"] = _calls(inner_source, "create_default_context")
        facts["edge_inner_stream_uses_certifi"] = "certifi.where()" in inner_source
        module = inspect.getsource(inspect.getmodule(edge_tts.Communicate))
        facts["edge_module_create_default_context"] = _calls(module, "create_default_context")
        facts["edge_module_reuses_ssl_ctx"] = "_SSL_CTX" in module
    except Exception as exc:
        facts["edge_tts_error"] = type(exc).__name__
    return facts


def build_report(host: str | None, port: int, repeat: int, timeout: float) -> dict:
    report = {
        "credentials_used": False,
        "http_request_sent": False,
        "direct_socket_is_not_the_sdk_connection": True,
        "contexts": context_samples(repeat),
        "sdk_build": sdk_build_samples(repeat),
        "libraries": library_facts(),
    }
    if host:
        report["handshake"] = handshake_samples(host, port, repeat, timeout)
    else:
        report["handshake"] = {"skipped": True, "reason": "host_not_requested"}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Split SSL context, TCP, and TLS timings")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    report = build_report(args.host or None, args.port, args.repeat, args.timeout)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
