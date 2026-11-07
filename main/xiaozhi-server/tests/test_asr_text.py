"""Live FunASR JSON stays content-only, including the first-speaker exception."""

from core.utils.asr_text import parse_chat_text


def test_funasr_json_passes_content_only():
    introduced = set()
    speaker, text = parse_chat_text(
        '{"content":"hello","language":"zh","emotion":"neutral"}',
        introduced,
    )
    assert speaker is None
    assert text == "hello"
    assert "language" not in text


def test_first_speaker_keeps_json_once():
    introduced = set()
    raw = '{"speaker":"A","content":"hi"}'
    speaker, text = parse_chat_text(raw, introduced)
    assert speaker == "A"
    assert text == raw
    speaker, text = parse_chat_text(raw, introduced)
    assert speaker == "A"
    assert text == "hi"


def test_plain_text_is_unchanged():
    speaker, text = parse_chat_text("你好", set())
    assert speaker is None
    assert text == "你好"
