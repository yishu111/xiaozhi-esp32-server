"""Stop reading an LLM stream after abort, and close it."""

from __future__ import annotations

from typing import Callable, Iterable, Iterator


def iter_until_abort(responses: Iterable, aborted: Callable[[], bool]) -> Iterator:
    for item in responses:
        if aborted():
            return
        yield item


def close_stream(responses) -> None:
    closer = getattr(responses, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


def consume_until_abort(responses: Iterable, aborted: Callable[[], bool]) -> Iterator:
    try:
        yield from iter_until_abort(responses, aborted)
    finally:
        close_stream(responses)
