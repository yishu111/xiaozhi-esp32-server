"""Context construction is timed without a socket and without a credential."""

from tools.ssl_context_split import build_report, context_samples


def test_context_samples_are_separate_from_handshake():
    report = context_samples(2)
    assert len(report["system_context_ms"]) == 2
    assert "tls_handshake_ms" not in report
    assert "tcp_ms" not in report
    assert report["ssl_cert_file_set"] in (True, False)


def test_report_without_a_host_does_not_claim_a_request():
    report = build_report(None, 443, 1, 1.0)
    assert report["credentials_used"] is False
    assert report["http_request_sent"] is False
    assert report["handshake"] == {"skipped": True, "reason": "host_not_requested"}
    assert report["sdk_build"]["request_sent"] is False
    assert "libraries" in report
