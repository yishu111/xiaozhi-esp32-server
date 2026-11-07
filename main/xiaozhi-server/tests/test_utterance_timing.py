"""Monotonic per-utterance timings never carry utterance text."""

from core.utils.llm_stream import consume_until_abort, iter_until_abort
from core.utils.utterance_timing import (
    UtteranceTiming,
    begin_end_of_speech,
    finish_timing,
    note_asr_final,
)


class _Log:
    def __init__(self):
        self.lines = []

    def bind(self, **_kwargs):
        return self

    def info(self, message):
        self.lines.append(message)


class _Conn:
    def __init__(self):
        self.logger = _Log()
        self._timing_lines = []


def test_stage_clock_is_monotonic_and_redacts_text():
    ticks = iter([10.0, 10.25, 10.5, 11.0])

    def now():
        return next(ticks)

    utt = UtteranceTiming(utterance_id="utt-test", now=now)
    assert utt.mark("end_of_speech", source="vad")
    assert utt.mark("asr_final", chars=4, text="private words")
    assert utt.mark("asr_final", chars=9) is False
    line = utt.format_stage("asr_final")
    assert "private words" not in line
    assert "redacted=1" in line
    assert "chars=4" in line
    assert utt.delta_ms("end_of_speech", "asr_final") == 250
    summary = utt.format_summary()
    assert "private words" not in summary
    assert "eos_to_asr_ms=250" in summary
    assert "server_sent=0" in summary
    assert "hardware_playback=unmeasured" in summary


def test_end_of_speech_is_kept_until_asr_final():
    conn = _Conn()
    first = begin_end_of_speech(conn, "vad")
    again = begin_end_of_speech(conn, "manual")
    assert again is first
    assert first.marks["end_of_speech"][1]["source"] == "vad"
    note_asr_final(conn, 3)
    later = begin_end_of_speech(conn, "manual")
    assert later is not first
    assert later.id != first.id
    blob = "\n".join(conn._timing_lines)
    assert "source=vad" in blob
    assert "stage=asr_final" in blob


def test_summary_emitted_once():
    conn = _Conn()
    note_asr_final(conn, 1)
    finish_timing(conn, server_sent=False)
    finish_timing(conn, server_sent=True)
    summaries = [line for line in conn._timing_lines if "summary" in line]
    assert len(summaries) == 1
    assert "server_sent=0" in summaries[0]
    assert "hardware_playback=unmeasured" in summaries[0]


def test_marks_from_two_threads_do_not_drop_stages():
    import threading

    utt = UtteranceTiming(utterance_id="thr", now=lambda: 1.0)
    barrier = threading.Barrier(2)

    def hit(stage):
        barrier.wait()
        utt.mark(stage, chars=1)

    threads = [
        threading.Thread(target=hit, args=("llm_request",)),
        threading.Thread(target=hit, args=("llm_first_token",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert "llm_request" in utt.marks
    assert "llm_first_token" in utt.marks


def test_iter_until_abort_stops_and_lets_errors_surface():
    def chunks():
        yield "a"
        yield "b"
        yield "c"

    seen = []
    aborted = {"value": False}

    def flag():
        return aborted["value"]

    for item in iter_until_abort(chunks(), flag):
        seen.append(item)
        aborted["value"] = True
    assert seen == ["a"]

    def broken():
        yield "a"
        raise RuntimeError("stream failed")

    collected = []
    try:
        for item in iter_until_abort(broken(), lambda: False):
            collected.append(item)
    except RuntimeError as exc:
        assert "stream failed" in str(exc)
    else:
        raise AssertionError("expected the stream error to propagate")
    assert collected == ["a"]


def test_abort_closes_the_upstream_generator():
    closed = {"value": False}

    def chunks():
        try:
            yield "a"
            yield "b"
        finally:
            closed["value"] = True

    seen = []
    aborted = {"value": False}

    for item in consume_until_abort(chunks(), lambda: aborted["value"]):
        seen.append(item)
        aborted["value"] = True
    assert seen == ["a"]
    assert closed["value"] is True
