"""Monotonic per-utterance timings never carry utterance text."""

from core.utils.llm_stream import iter_until_abort
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
    assert "complete=0" in summary


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
    finish_timing(conn, complete=False)
    finish_timing(conn, complete=True)
    summaries = [line for line in conn._timing_lines if "summary" in line]
    assert len(summaries) == 1
    assert "complete=0" in summaries[0]


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
