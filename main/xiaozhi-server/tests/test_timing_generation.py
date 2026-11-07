"""Synth callbacks stay on the generation that started them."""

from core.utils.utterance_timing import (
    UtteranceTiming,
    begin_end_of_speech,
    bind_sentence,
    mark_sentence,
    note_asr_final,
)


class _Log:
    def bind(self, **_kwargs):
        return self

    def info(self, _message):
        return None


def _conn():
    conn = type("Conn", (), {})()
    conn.logger = _Log()
    conn._timing_lines = []
    conn.sentence_id = None
    conn.client_abort = False
    conn._timings_by_sentence = {}
    return conn


def _bind_ready(conn, sentence_id):
    note_asr_final(conn, 1)
    conn.sentence_id = sentence_id
    bind_sentence(conn, sentence_id)
    mark_sentence(conn, sentence_id, "tts_request", chars=4)
    return conn._timings_by_sentence[sentence_id]


def test_pending_wake_audio_does_not_mark_the_next_utterance():
    conn = _conn()
    wake = _bind_ready(conn, "wake-sentence")
    speech = begin_end_of_speech(conn, "vad")
    note_asr_final(conn, 3)
    conn.sentence_id = "speech-sentence"
    bind_sentence(conn, "speech-sentence")
    mark_sentence(conn, "wake-sentence", "tts_first_audio", bytes=80)
    mark_sentence(conn, None, "tts_first_audio", bytes=80)
    assert "tts_first_audio" not in speech.marks
    assert "tts_first_audio" not in wake.marks
    assert any("reason=stale" in line or "reason=unbound" in line for line in conn._timing_lines)


def test_stale_callback_after_interrupt_and_new_connection():
    conn = _conn()
    first = _bind_ready(conn, "turn-a")
    conn.client_abort = True
    mark_sentence(conn, "turn-a", "tts_first_audio", bytes=10)
    assert "tts_first_audio" not in first.marks
    other = _conn()
    other.sentence_id = "turn-a"
    mark_sentence(other, "turn-a", "server_audio_sent", bytes=10)
    assert "server_audio_sent" not in first.marks
    assert "server_audio_sent" not in getattr(other, "_utterance_timing", UtteranceTiming()).marks


def test_two_synth_requests_keep_their_own_audio():
    conn = _conn()
    alpha = _bind_ready(conn, "alpha")
    beta_owner = note_asr_final(conn, 2)
    conn.sentence_id = "beta"
    bind_sentence(conn, "beta")
    mark_sentence(conn, "beta", "tts_request", chars=6)
    mark_sentence(conn, "alpha", "tts_first_audio", bytes=4)
    mark_sentence(conn, "beta", "tts_first_audio", bytes=8)
    mark_sentence(conn, "beta", "server_audio_sent", bytes=8)
    assert "tts_first_audio" not in alpha.marks
    assert "tts_first_audio" in beta_owner.marks
    assert beta_owner.delta_ms("tts_request", "tts_first_audio") is not None
    assert beta_owner.delta_ms("tts_request", "tts_first_audio") >= 0
    summary = beta_owner.format_summary()
    assert "order_ok=1" in summary
    assert "-" not in summary.split("id=", 1)[-1]


def test_audio_before_request_and_failed_retry_do_not_invent_a_chunk():
    conn = _conn()
    utt = _bind_ready(conn, "only")
    utt.marks.pop("tts_request")
    mark_sentence(conn, "only", "tts_first_audio", bytes=3)
    assert "tts_first_audio" not in utt.marks
    mark_sentence(conn, "only", "tts_request", chars=2)
    mark_sentence(conn, "only", "tts_request", chars=2)
    mark_sentence(conn, "only", "tts_first_audio", bytes=9)
    mark_sentence(conn, "only", "tts_first_audio", bytes=9)
    assert utt.marks["tts_first_audio"][1]["bytes"] == 9


def test_negative_clock_is_not_reported_as_a_duration():
    ticks = iter([0.0, 20.0, 10.0, 11.0])
    utt = UtteranceTiming(utterance_id="clock", now=lambda: next(ticks))
    utt.mark("tts_request", chars=1)
    utt.mark("tts_first_audio", bytes=1)
    utt.mark("server_audio_sent", bytes=1)
    summary = utt.format_summary()
    assert "tts_to_first_audio_ms=na" in summary
    assert "order_ok=0" in summary
    assert "-1" not in summary
    assert "tts_audio_to_server_send_ms=1000" in summary
