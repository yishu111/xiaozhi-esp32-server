"""Per-utterance monotonic timings.

Stage lines carry durations and counts only. Free-form strings are dropped
so recognized text, memory bodies, and secrets cannot land in the log.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

TAG = "utterance_timing"

STAGES = (
    "end_of_speech",
    "asr_final",
    "memory_lookup_start",
    "memory_lookup_end",
    "llm_request",
    "llm_first_token",
    "llm_first_sentence",
    "tts_request",
    "tts_first_audio",
    "playback_first_audio",
)

_ENUMS = {
    "source": {"vad", "manual"},
    "memory": {"ok", "empty", "timeout", "skipped", "error", "cancelled"},
}

_SUMMARY_GAPS = (
    ("eos_to_asr_ms", "end_of_speech", "asr_final"),
    ("asr_to_memory_ms", "asr_final", "memory_lookup_start"),
    ("memory_ms", "memory_lookup_start", "memory_lookup_end"),
    ("memory_to_llm_ms", "memory_lookup_end", "llm_request"),
    ("llm_to_first_token_ms", "llm_request", "llm_first_token"),
    ("first_token_to_sentence_ms", "llm_first_token", "llm_first_sentence"),
    ("sentence_to_tts_ms", "llm_first_sentence", "tts_request"),
    ("tts_to_first_audio_ms", "tts_request", "tts_first_audio"),
    ("audio_to_playback_ms", "tts_first_audio", "playback_first_audio"),
)


class UtteranceTiming:
    def __init__(self, utterance_id: Optional[str] = None, now: Optional[Callable[[], float]] = None):
        self.id = utterance_id or uuid.uuid4().hex[:12]
        self._now = now or time.monotonic
        self.t0 = self._now()
        self.marks: dict[str, tuple[float, dict[str, Any]]] = {}
        self.lines: list[str] = []
        self.aborted = False
        self.finished = False
        self.complete = False

    def mark(self, stage: str, **meta: Any) -> bool:
        if stage not in STAGES:
            return False
        if stage in self.marks:
            return False
        clean: dict[str, Any] = {}
        redacted = False
        for key, value in meta.items():
            if isinstance(value, bool):
                clean[key] = int(value)
            elif isinstance(value, int):
                clean[key] = value
            elif isinstance(value, float):
                clean[key] = int(value) if value.is_integer() else round(value, 3)
            elif isinstance(value, str) and key in _ENUMS and value in _ENUMS[key]:
                clean[key] = value
            else:
                redacted = True
        if redacted:
            clean["redacted"] = 1
        self.marks[stage] = (self._now(), clean)
        return True

    def delta_ms(self, earlier: str, later: str) -> Optional[int]:
        if earlier not in self.marks or later not in self.marks:
            return None
        return int(round((self.marks[later][0] - self.marks[earlier][0]) * 1000))

    def format_stage(self, stage: str) -> str:
        stamp, meta = self.marks[stage]
        t_ms = int(round((stamp - self.t0) * 1000))
        prev = [item[0] for name, item in self.marks.items() if name != stage]
        since_prev = int(round((stamp - max(prev)) * 1000)) if prev else t_ms
        if since_prev < 0:
            since_prev = 0
        parts = [
            "utterance",
            f"id={self.id}",
            f"stage={stage}",
            f"t_ms={t_ms}",
            f"since_prev_ms={since_prev}",
        ]
        for key in sorted(meta):
            parts.append(f"{key}={meta[key]}")
        return " ".join(parts)

    def format_summary(self) -> str:
        parts = ["utterance", "summary", f"id={self.id}"]
        for name, earlier, later in _SUMMARY_GAPS:
            delta = self.delta_ms(earlier, later)
            parts.append(f"{name}={'na' if delta is None else delta}")
        parts.append(f"aborted={int(self.aborted)}")
        parts.append(f"complete={int(self.complete)}")
        return " ".join(parts)


def _logger_for(conn):
    logger = getattr(conn, "logger", None)
    if logger is not None:
        return logger
    from config.logger import setup_logging

    return setup_logging()


def _emit(conn, utt: UtteranceTiming, line: str) -> None:
    utt.lines.append(line)
    bucket = getattr(conn, "_timing_lines", None)
    if isinstance(bucket, list):
        bucket.append(line)
    try:
        _logger_for(conn).bind(tag=TAG).info(line)
    except Exception:
        return


def _current(conn) -> Optional[UtteranceTiming]:
    utt = getattr(conn, "_utterance_timing", None)
    return utt if isinstance(utt, UtteranceTiming) else None


def begin_end_of_speech(conn, source: str) -> UtteranceTiming:
    """Mark the speech boundary once. A later manual/VAD signal keeps the first mark."""
    utt = _current(conn)
    if utt is not None and "end_of_speech" in utt.marks and "asr_final" not in utt.marks:
        return utt
    utt = UtteranceTiming()
    conn._utterance_timing = utt
    utt.mark("end_of_speech", source=source if source in _ENUMS["source"] else "vad")
    _emit(conn, utt, utt.format_stage("end_of_speech"))
    return utt


def note_asr_final(conn, chars: int) -> UtteranceTiming:
    utt = _current(conn)
    if utt is None or "asr_final" in utt.marks:
        utt = UtteranceTiming()
        conn._utterance_timing = utt
    try:
        count = int(chars)
    except (TypeError, ValueError):
        count = 0
    if count < 0:
        count = 0
    utt.mark("asr_final", chars=count)
    _emit(conn, utt, utt.format_stage("asr_final"))
    return utt


def bind_sentence(conn, sentence_id: Optional[str]) -> Optional[UtteranceTiming]:
    if not sentence_id:
        return _current(conn)
    utt = _current(conn)
    if utt is None:
        utt = UtteranceTiming()
        conn._utterance_timing = utt
    table = getattr(conn, "_timings_by_sentence", None)
    if not isinstance(table, dict):
        table = {}
        conn._timings_by_sentence = table
    table[sentence_id] = utt
    if len(table) > 8:
        for key in list(table)[: len(table) - 8]:
            table.pop(key, None)
    return utt


def timing_for_sentence(conn, sentence_id: Optional[str] = None) -> Optional[UtteranceTiming]:
    table = getattr(conn, "_timings_by_sentence", None)
    if sentence_id and isinstance(table, dict) and sentence_id in table:
        return table[sentence_id]
    return _current(conn)


def mark_sentence(conn, sentence_id: Optional[str], stage: str, **meta: Any) -> None:
    if conn is None:
        return
    utt = timing_for_sentence(conn, sentence_id)
    if utt is None:
        utt = UtteranceTiming()
        conn._utterance_timing = utt
        if sentence_id:
            bind_sentence(conn, sentence_id)
            utt = timing_for_sentence(conn, sentence_id) or utt
    if not utt.mark(stage, **meta):
        return
    _emit(conn, utt, utt.format_stage(stage))
    if stage == "playback_first_audio":
        finish_timing(conn, sentence_id=sentence_id, complete=True)


def mark_current(conn, stage: str, **meta: Any) -> None:
    mark_sentence(conn, getattr(conn, "sentence_id", None), stage, **meta)


def finish_timing(conn, sentence_id: Optional[str] = None, aborted: bool = False, complete: bool = False) -> None:
    utt = timing_for_sentence(conn, sentence_id)
    if utt is None or utt.finished:
        return
    if aborted:
        utt.aborted = True
    utt.complete = bool(complete and "playback_first_audio" in utt.marks)
    utt.finished = True
    _emit(conn, utt, utt.format_summary())
