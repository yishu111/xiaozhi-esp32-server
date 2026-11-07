"""Turn ASR text into the string the LLM should see.

FunASR emits {"content","language","emotion"}. Only content is dialogue.
A speaker JSON is kept for the first time that speaker appears.
"""

import json


def parse_chat_text(text, introduced):
    speaker_name = None
    actual_text = text
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                if speaker_name not in introduced:
                    introduced.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = data["content"]
            elif "content" in data:
                actual_text = data["content"]
    except (json.JSONDecodeError, KeyError):
        pass
    return speaker_name, actual_text
