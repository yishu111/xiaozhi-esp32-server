"""First-sentence segmentation.

Punctuation cutting matches v0.9.7. A positive max_chars only applies to the
first segment, and unfinished tool-call markup is never cut early.
"""

from __future__ import annotations


def looks_unfinished_markup(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<") or stripped.startswith("{") or stripped.startswith("```")


def cut_first_segment(
    current_text: str,
    *,
    is_first: bool,
    punctuations: tuple[str, ...] | list[str],
    first_punctuations: tuple[str, ...] | list[str],
    max_chars: int = 0,
) -> tuple[str | None, str | None]:
    if not current_text:
        return None, None

    punctuations_to_use = first_punctuations if is_first else punctuations
    last_punct_pos = -1
    for punct in punctuations_to_use:
        pos = current_text.rfind(punct)
        if (pos != -1 and last_punct_pos == -1) or (pos != -1 and pos < last_punct_pos):
            last_punct_pos = pos

    if last_punct_pos != -1:
        return current_text[: last_punct_pos + 1], "punct"

    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 0
    if (
        is_first
        and limit > 0
        and len(current_text) >= limit
        and not looks_unfinished_markup(current_text)
    ):
        return current_text[:limit], "max_chars"
    return None, None
