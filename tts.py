"""Human-like voice for Jhon — Microsoft Edge TTS.

Free, no API key, no account, no card. Microsoft's neural voices:
  Bangla  -> bn-BD-PradeepNeural (male — Jhon is a male name)
  English -> en-US-GuyNeural      (male)

Used by web_server.py's POST /speak endpoint. The website tries this
server voice first and falls back to the browser's built-in voice
whenever the server voice is unavailable, so voice never breaks chat.

Install on the PC:  pip install edge-tts
(Already added to requirements.txt.)
"""

import asyncio
import re

VOICE_BN = "bn-BD-PradeepNeural"
VOICE_EN = "en-US-GuyNeural"

_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+"
)


def clean_text(text):
    """Strip markdown + emojis so the voice doesn't read them aloud
    (the agent's replies contain **bold** markers and ✅/😊)."""
    text = (text or "").strip()
    text = text.replace("**", "").replace("*", "").replace("#", "")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def pick_voice(text):
    """Bangla script -> Bangla voice; anything else -> English."""
    return VOICE_BN if _BENGALI_RE.search(text or "") else VOICE_EN


async def _synthesize_async(text, voice):
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            audio.extend(chunk.get("data") or b"")
    return bytes(audio)


def synthesize(text, voice=None):
    """Text -> MP3 bytes (synchronous wrapper for Flask).

    Raises ImportError when edge-tts is not installed, and any
    network/API error from edge-tts propagates to the caller — the
    /speak endpoint turns both into a 503 so the website can fall
    back to the browser voice.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return b""
    voice = voice or pick_voice(cleaned)
    return asyncio.run(_synthesize_async(cleaned, voice))
