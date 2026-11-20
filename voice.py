"""
Speech-to-text for incoming Telegram voice notes, using Groq's
hosted Whisper model. This is what lets the bot understand a voice
message the same way it understands typed text — a step toward the
"real phone-call receptionist" behavior.
"""

import os
import traceback

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing")

client = Groq(api_key=GROQ_API_KEY)

# whisper-large-v3 handles Bangla, Banglish-accented speech, and
# English all reasonably well without needing a language hint.
STT_MODEL = "whisper-large-v3"


def transcribe_voice(file_path):
    """
    Transcribes an audio file (e.g. a downloaded Telegram .ogg voice
    note) to text. Returns the transcribed string, or None on
    failure (missing file, API error, empty result).
    """
    try:
        with open(file_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                file=audio_file,
                model=STT_MODEL,
                response_format="text",
            )
        # Some SDK versions return a plain string, others an object
        # with a `.text` attribute depending on response_format —
        # handle both defensively.
        if isinstance(transcription, str):
            return transcription
        return getattr(transcription, "text", None)
    except Exception:
        print("Voice Transcription Error:")
        print(traceback.format_exc())
        return None
