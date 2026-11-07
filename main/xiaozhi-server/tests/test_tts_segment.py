"""First-segment cutting stays on punctuation unless explicitly enabled."""

from core.utils.tts_segment import cut_first_segment


def test_punctuation_cut_matches_the_previous_first_sentence_rule():
    segment, kind = cut_first_segment(
        "你好，世界。",
        is_first=True,
        punctuations=("。",),
        first_punctuations=("，", "。"),
        max_chars=0,
    )
    assert kind == "punct"
    assert segment == "你好，"


def test_max_chars_zero_does_not_flush_early():
    segment, kind = cut_first_segment(
        "没有标点的一句很长的话",
        is_first=True,
        punctuations=("。",),
        first_punctuations=("。",),
        max_chars=0,
    )
    assert segment is None
    assert kind is None


def test_max_chars_flushes_only_the_first_plain_segment():
    segment, kind = cut_first_segment(
        "没有标点的一句很长的话",
        is_first=True,
        punctuations=("。",),
        first_punctuations=("。",),
        max_chars=4,
    )
    assert kind == "max_chars"
    assert segment == "没有标点"
    later, later_kind = cut_first_segment(
        "后续没有标点",
        is_first=False,
        punctuations=("。",),
        first_punctuations=("。",),
        max_chars=2,
    )
    assert later is None
    assert later_kind is None


def test_unfinished_markup_is_not_cut_early():
    segment, kind = cut_first_segment(
        '{"name":"forget"}',
        is_first=True,
        punctuations=("。",),
        first_punctuations=("。",),
        max_chars=4,
    )
    assert segment is None
    assert kind is None
