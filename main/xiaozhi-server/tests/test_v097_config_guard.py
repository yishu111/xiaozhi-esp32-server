"""Stock config and the restored TTS helper stay free of placeholders."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"
TTS_BASE = ROOT / "core/providers/tts/base.py"


def test_stock_config_is_not_a_persona_or_provider_override():
    loaded = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    selected = loaded["selected_module"]
    assert selected["VAD"] == "SileroVAD"
    assert selected["ASR"] == "FunASR"
    assert selected["TTS"] == "EdgeTTS"
    assert selected["Memory"] == "nomem"
    assert selected["Intent"] == "function_call"
    functions = loaded["Intent"]["function_call"]["functions"]
    assert "forget_memory" not in functions
    assert "submit_grok_task" not in functions
    assert "submit_grok_task" not in loaded.get("plugins", {})
    assert "memory_query_timeout_seconds" not in loaded


def test_tts_helper_name_was_restored_and_quote_cut_remains():
    source = TTS_BASE.read_text(encoding="utf-8")
    assert "REDACTED" not in source
    assert source.count("textUtils.get_string_no_punctuation_or_emoji") == 2
    assert "def _quote_safe_cut" in source
