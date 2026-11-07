"""The stock module selection stays on the v0.9.7 defaults."""

from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def test_selected_modules_and_optional_tools_stay_opt_in():
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
    grok = loaded["plugins"]["submit_grok_task"]
    assert "你" in grok["webhook_url"]
    assert "你" in grok["webhook_secret"]
    assert loaded["memory_query_timeout_seconds"] == 15
    assert loaded["tts_first_segment_max_chars"] == 0
