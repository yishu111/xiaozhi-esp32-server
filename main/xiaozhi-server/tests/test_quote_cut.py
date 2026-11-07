"""Quote-safe sentence cuts from the live TTS base stay outside quotation marks."""

import sys
import types


def _install_stubs():
    if "opuslib_next" not in sys.modules:
        opus = types.ModuleType("opuslib_next")
        opus.Encoder = object
        opus.Decoder = object
        opus.APPLICATION_AUDIO = 0
        opus.constants = types.SimpleNamespace()
        sys.modules["opuslib_next"] = opus
    if "numpy" not in sys.modules:
        try:
            import numpy  # noqa: F401
        except Exception:
            sys.modules["numpy"] = types.ModuleType("numpy")
    if "pydub" not in sys.modules:
        try:
            import pydub  # noqa: F401
        except Exception:
            audio = types.ModuleType("pydub")
            audio.AudioSegment = object
            sys.modules["pydub"] = audio


_install_stubs()

from core.providers.tts.base import TTSProviderBase  # noqa: E402


def test_plain_punctuation_still_cuts_and_quotes_wait():
    plain = TTSProviderBase._quote_safe_cut("你好，世界", 2, ("，", "。"))
    assert plain == 2
    quoted = TTSProviderBase._quote_safe_cut("他说“好！”然后走了。", 4, ("！", "。"))
    assert quoted != 4
    assert quoted > 4


def test_restored_helper_is_callable():
    from core.utils import textUtils

    assert callable(textUtils.get_string_no_punctuation_or_emoji)
    assert textUtils.get_string_no_punctuation_or_emoji("你好。") == "你好"
