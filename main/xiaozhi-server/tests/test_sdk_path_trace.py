"""The SDK trace keeps clocks and drops httpcore info."""

import json

from tools.sdk_path_trace import classify, proxy_report, summarize_events


def test_trace_summary_drops_info_payload():
    secret = "proxy-secret-value"
    events = []

    def trace(name, info):
        assert secret in str(info)
        events.append((name, info.get("stamp")))

    trace("connection.connect_tcp.started", {"host": secret, "stamp": 10.0})
    trace("connection.connect_tcp.complete", {"host": secret, "stamp": 10.02})
    trace("connection.start_tls.started", {"server_hostname": secret, "stamp": 10.02})
    trace("connection.start_tls.complete", {"server_hostname": secret, "stamp": 10.08})
    trace("http11.send_request_headers.started", {"request": secret, "stamp": 10.09})
    trace("http11.send_request_headers.complete", {"request": secret, "stamp": 10.10})
    trace("http11.receive_response_headers.started", {"headers": secret, "stamp": 10.10})
    trace("http11.receive_response_headers.complete", {"headers": secret, "stamp": 15.40})
    stored = [(name, stamp) for name, stamp in events]
    report = summarize_events(stored)
    rendered = json.dumps(report)
    assert secret not in rendered
    assert report["connect_tcp_ms"] == [20.0]
    assert report["start_tls_ms"] == [60.0]
    assert report["receive_response_headers_ms"] == [5300.0]
    assert report["http_attempts"] == 1
    assert report["connection_reused"] is False


def test_classify_warm_connection_and_provider_wait():
    cold = {
        "connect_tcp_ms": [30.0],
        "start_tls_ms": [1500.0],
        "send_request_headers_ms": [2.0],
        "receive_response_headers_ms": [40.0],
        "http_attempts": 1,
        "connection_reused": False,
    }
    warm = {
        "connect_tcp_ms": [],
        "start_tls_ms": [],
        "send_request_headers_ms": [1.0],
        "receive_response_headers_ms": [80.0],
        "http_attempts": 1,
        "connection_reused": True,
    }
    slow = {
        "connect_tcp_ms": [8.0],
        "start_tls_ms": [20.0],
        "send_request_headers_ms": [1.0],
        "receive_response_headers_ms": [5400.0],
        "http_attempts": 1,
        "connection_reused": True,
    }
    assert classify([cold, warm, warm]) == "cold_connect_then_fast"
    assert classify([slow, slow, slow]) == "ttfb"
    assert classify([{**slow, "http_attempts": 2}]) == "retries_inside_call"


def test_proxy_report_names_source_without_value(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret-proxy-credential@10.1.1.1:8080")
    report = proxy_report()
    assert report == {"proxy_enabled": True, "proxy_source": "env_https", "trust_env": True}
    assert "secret-proxy-credential" not in json.dumps(report)
