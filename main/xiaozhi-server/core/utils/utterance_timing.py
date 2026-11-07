"""Per-utterance monotonic timings.

Stage lines carry durations and counts only. Free-form strings are dropped
so recognized text, memory bodies, and secrets cannot land in the log.
"""

from __future__ import annotations

import threading
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
    "server_audio_sent",
)

_ENUMS = {
    "source": {"vad", "manual"},
    "memory": {"ok", "empty", "timeout", "skipped", "error", "cancelled"},
}

# These stages belong to one synthesis generation. They must not fall back
# onto whatever utterance happens to be current when a callback arrives.
BOUND_STAGES = frozenset(
    {
        "llm_first_sentence",
        "tts_request",
        "tts_first_audio",
        "server_audio_sent",
    }
)

_SUMMARY_GAPS = (
    ("eos_to_asr_ms", "end_of_speech", "asr_final"),
    ("asr_to_memory_ms", "asr_final", "memory_lookup_start"),
    ("memory_ms", "memory_lookup_start", "memory_lookup_end"),
    ("memory_to_llm_ms", "memory_lookup_end", "llm_request"),
    ("llm_to_first_token_ms", "llm_request", "llm_first_token"),
    ("first_token_to_sentence_ms", "llm_first_token", "llm_first_sentence"),
    ("sentence_to_tts_ms", "llm_first_sentence", "tts_request"),
    ("tts_to_first_audio_ms", "tts_request", "tts_first_audio"),
    ("tts_audio_to_server_send_ms", "tts_first_audio", "server_audio_sent"),
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
        self.server_sent = False
        self.order_ok = True
        self._lock = threading.Lock()

    def mark(self, stage: str, **meta: Any) -> bool:
        if stage not in STAGES:
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
        with self._lock:
            if stage in self.marks:
                return False
            self.marks[stage] = (self._now(), clean)
        return True

    def delta_ms(self, earlier: str, later: str) -> Optional[int]:
        with self._lock:
            if earlier not in self.marks or later not in self.marks:
                return None
            delta = int(round((self.marks[later][0] - self.marks[earlier][0]) * 1000))
            if delta < 0:
                self.order_ok = False
                return None
            return delta

    def format_stage(self, stage: str) -> str:
        with self._lock:
            stamp, meta = self.marks[stage]
            previous = [item[0] for name, item in self.marks.items() if name != stage]
        t_ms = int(round((stamp - self.t0) * 1000))
        if previous:
            since_prev = int(round((stamp - max(previous)) * 1000))
        else:
            since_prev = t_ms
        if since_prev < 0:
            self.order_ok = False
        parts = [
            "utterance",
            f"id={self.id}",
            f"stage={stage}",
            f"t_ms={t_ms}",
            f"since_prev_ms={'na' if since_prev < 0 else since_prev}",
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
        parts.append(f"server_sent={int(self.server_sent)}")
        parts.append(f"order_ok={int(self.order_ok)}")
        parts.append("hardware_playback=unmeasured")
        return " ".join(parts)


def _drop(conn, stage: str, reason: str) -> None:
    line = f"utterance drop stage={stage} reason={reason}"
    bucket = getattr(conn, "_timing_lines", None)
    if isinstance(bucket, list):
        bucket.append(line)
    try:
        _logger_for(conn).bind(tag=TAG).info(line)
    except Exception:
        return


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


def timing_for_sentence(conn, sentence_id: Optional[str] = None, *, fallback: bool = True) -> Optional[UtteranceTiming]:
    table = getattr(conn, "_timings_by_sentence", None)
    if sentence_id and isinstance(table, dict) and sentence_id in table:
        return table[sentence_id]
    if not fallback:
        return None
    return _current(conn)


def mark_sentence(conn, sentence_id: Optional[str], stage: str, **meta: Any) -> None:
    if conn is None:
        return
    bound = stage in BOUND_STAGES
    if bound:
        if not sentence_id:
            _drop(conn, stage, "unbound")
            return
        if getattr(conn, "client_abort", False) or getattr(conn, "sentence_id", None) != sentence_id:
            _drop(conn, stage, "stale")
            return
        utt = timing_for_sentence(conn, sentence_id, fallback=False)
        if utt is None:
            _drop(conn, stage, "unbound")
            return
        if stage == "tts_first_audio" and "tts_request" not in utt.marks:
            _drop(conn, stage, "before_request")
            return
    else:
        utt = timing_for_sentence(conn, sentence_id, fallback=True)
        if utt is None:
            utt = UtteranceTiming()
            conn._utterance_timing = utt
            if sentence_id:
                bind_sentence(conn, sentence_id)
                utt = timing_for_sentence(conn, sentence_id, fallback=False) or utt
    if not utt.mark(stage, **meta):
        return
    _emit(conn, utt, utt.format_stage(stage))
    if stage == "server_audio_sent":
        finish_timing(conn, sentence_id=sentence_id, server_sent=True)


def mark_current(conn, stage: str, **meta: Any) -> None:
    mark_sentence(conn, getattr(conn, "sentence_id", None), stage, **meta)


def finish_timing(conn, sentence_id: Optional[str] = None, aborted: bool = False, server_sent: bool = False) -> None:
    utt = timing_for_sentence(conn, sentence_id)
    if utt is None or utt.finished:
        return
    if aborted:
        utt.aborted = True
    # server_sent means the first packet was handed to the socket, not that
    # the device speaker has played it.
    utt.server_sent = bool(server_sent and "server_audio_sent" in utt.marks)
    utt.finished = True
    _emit(conn, utt, utt.format_summary())
