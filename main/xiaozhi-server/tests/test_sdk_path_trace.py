"""Production SDK trace keeps retries, finishes the body, and inherits the pool."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from tools.sdk_path_trace import classify, summarize_events


def test_summary_marks_incomplete_body_and_keeps_retry_events():
    secret = "header-secret"
    events = []

    def trace(name, info):
        assert secret in str(info)
        events.append((name, info["stamp"]))

    trace("http11.send_request_headers.started", {"secret": secret, "stamp": 1.0})
    trace("http11.send_request_headers.complete", {"secret": secret, "stamp": 1.01})
    trace("http11.receive_response_headers.started", {"secret": secret, "stamp": 1.01})
    trace("http11.receive_response_headers.complete", {"secret": secret, "stamp": 1.20})
    trace("http11.send_request_headers.started", {"secret": secret, "stamp": 2.0})
    trace("http11.send_request_headers.complete", {"secret": secret, "stamp": 2.01})
    trace("http11.receive_response_body.started", {"secret": secret, "stamp": 2.02})
    partial = summarize_events(events)
    assert secret not in json.dumps(partial)
    assert partial["http_attempts"] == 2
    assert partial["receive_response_body_ms"] is None
    assert "receive_response_body" in partial["incomplete_phases"]
    trace("http11.receive_response_body.complete", {"secret": secret, "stamp": 2.12})
    done = summarize_events([(name, stamp) for name, stamp in events])
    assert done["receive_response_body_ms"] == [100.0]
    assert done["incomplete_phases"] == []


def test_classify_does_not_treat_missing_body_as_zero():
    missing = {
        "connect_tcp_ms": [],
        "start_tls_ms": [],
        "receive_response_headers_ms": None,
        "http_attempts": 1,
        "connection_reused": True,
    }
    assert classify([missing]) == "unknown"


def test_real_sdk_client_retries_pool_and_proxy_route():
    """Run outside the suite's httpx shim so the SDK builds its own wrapper."""
    root = Path(__file__).resolve().parents[1]
    program = textwrap.dedent(
        """
        import json, os
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = "http://example.invalid/v1"
        os.environ["HTTPS_PROXY"] = "http://user:secret-proxy-credential@10.1.1.1:8080"
        os.environ["NO_PROXY"] = "example.invalid"
        import httpx
        import openai
        from openai._constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
        from tools.sdk_path_trace import attach_trace, make_embed_sdk, make_llm_sdk, proxy_env_source, transport_category

        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            trace = request.extensions["trace"]
            trace("http11.send_request_headers.started", {"authorization": "secret-token"})
            trace("http11.send_request_headers.complete", {})
            trace("http11.receive_response_headers.started", {})
            trace("http11.receive_response_headers.complete", {})
            if calls["n"] == 1:
                return httpx.Response(500, json={"error": {"message": "nope", "type": "server_error"}})
            trace("http11.receive_response_body.started", {})
            response = httpx.Response(200, json={
                "object": "list",
                "data": [{"object": "embedding", "embedding": [0.25], "index": 0}],
                "model": "m",
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })
            trace("http11.receive_response_body.complete", {})
            return response

        retry_sdk = openai.OpenAI(
            api_key="test-key",
            base_url="http://example.invalid/v1",
            max_retries=1,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        log = attach_trace(retry_sdk._client)
        created = retry_sdk.embeddings.create(input=["diag"], model="m")
        sample = log.summarize()
        retry_sdk.close()

        embed, _trace = make_embed_sdk()
        vanilla = openai.OpenAI(api_key="test-key", base_url="http://example.invalid/v1")
        llm, _llm_trace = make_llm_sdk("test-key", "http://example.invalid/v1", httpx.Timeout(300))
        try:
            pool = embed._client._transport._pool
            result = {
                "attempts": sample["http_attempts"],
                "header_spans": len(sample["send_request_headers_ms"]),
                "body": sample["receive_response_body_ms"],
                "vector": created.data[0].embedding,
                "same_wrapper": type(embed._client).__name__ == type(vanilla._client).__name__,
                "timeout_match": embed._client.timeout == DEFAULT_TIMEOUT,
                "retries": embed.max_retries == DEFAULT_MAX_RETRIES == vanilla.max_retries,
                "keepalive": pool._max_keepalive_connections,
                "connections": pool._max_connections,
                "expected_keepalive": DEFAULT_CONNECTION_LIMITS.max_keepalive_connections,
                "expected_connections": DEFAULT_CONNECTION_LIMITS.max_connections,
                "connect": embed._client.timeout.connect,
                "read": embed._client.timeout.read,
                "pool_timeout": embed._client.timeout.pool,
                "llm_timeout": llm._client.timeout == httpx.Timeout(300),
                "llm_keepalive": llm._client._transport._pool._max_keepalive_connections,
                "direct": transport_category(embed._client, "http://example.invalid/v1"),
                "proxied": transport_category(embed._client, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                "proxy_env_source": proxy_env_source(),
            }
        finally:
            embed.close()
            vanilla.close()
            llm.close()
        print(json.dumps(result))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "secret-proxy-credential" not in completed.stdout
    assert "secret-token" not in completed.stdout
    result = json.loads(completed.stdout)
    assert result["attempts"] == 2
    assert result["header_spans"] == 2
    assert result["body"] is not None and result["body"] != []
    assert result["vector"] == [0.25]
    assert result["same_wrapper"] is True
    assert result["timeout_match"] is True
    assert result["retries"] is True
    assert result["keepalive"] == result["expected_keepalive"] == 100
    assert result["connections"] == result["expected_connections"] == 1000
    assert result["connect"] == 5.0
    assert result["read"] == 600
    assert result["pool_timeout"] == 600
    assert result["llm_timeout"] is True
    assert result["llm_keepalive"] == 100
    assert result["direct"] == "direct"
    assert result["proxied"] == "http_proxy"
    assert result["proxy_env_source"] == "env_https"
