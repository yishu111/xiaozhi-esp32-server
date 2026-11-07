"""Stop handing LLM chunks to the voice path after the client aborts."""

from __future__ import annotations

from typing import Callable, Iterable, Iterator


def iter_until_abort(responses: Iterable, aborted: Callable[[], bool]) -> Iterator:
    for item in responses:
        if aborted():
            return
        yield item
