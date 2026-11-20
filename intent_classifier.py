"""
LLM-powered intent classifier for the clinic chatbot.
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
MODEL_NAME = "openai/gpt-oss-20b"


INTENT_PROMPT = """You are an intent classifier for a medical clinic chatbot.

The patient writes in English, Bangla, or Banglish (Romanized Bangla),
often with typos.

Classify the patient's message into EXACTLY ONE of these intents:

SYMPTOM - patient describes a health problem (fever, headache, pain, cough, cold, stomach, chest, eye, ear, tooth, skin, urine, etc). Even with typos ("fiver" = fever, "jor" = fever, "matha betha" = headache).
BOOK_NEW - patient wants to book, but no symptom mentioned yet.
PROVIDE_DAY - patient is answering a "which day?" question (monday, tomorrow, etc).
PROVIDE_NAME - patient is giving a name and/or phone number.
CONFIRM_YES - patient confirms (yes, ok, ha, thik ache).
CONFIRM_NO - patient declines.
CHANGE_DOCTOR - patient wants a different doctor.
CHANGE_DAY - patient wants to change the day.
CANCEL - patient wants to cancel.
RESTART - patient wants to start over.
DOCTOR_INFO - patient asks about a specific doctor (who, where, schedule, department).
CLINIC_INFO - patient asks about clinic, departments, services, hours.
GREETING - hi, hello, how are you, kemon achen, nam ki.
GENERAL - anything else.

EXAMPLES:
i have fever -> SYMPTOM
fiver -> SYMPTOM
amr jor hoise -> SYMPTOM
amr jor -> SYMPTOM
matha betha -> SYMPTOM
my head hurts -> SYMPTOM
chest pain -> SYMPTOM
i have a health issue -> SYMPTOM
amar shorir kharap -> SYMPTOM
i want to book -> BOOK_NEW
dr rana -> BOOK_NEW
dr rana kothay -> DOCTOR_INFO
which department is dr ayesha in -> DOCTOR_INFO
dr rana kon department -> DOCTOR_INFO
monday -> PROVIDE_DAY
tomorrow -> PROVIDE_DAY
friday -> PROVIDE_DAY
karim rahman 01700000000 -> PROVIDE_NAME
yes -> CONFIRM_YES
ha -> CONFIRM_YES
ok -> CONFIRM_YES
no -> CONFIRM_NO
change the doctor -> CHANGE_DOCTOR
onno daktar -> CHANGE_DOCTOR
change the day -> CHANGE_DAY
cancel koro -> CANCEL
restart -> RESTART
hi -> GREETING
how are you -> GREETING
kemon achen -> GREETING
tomar nam ki -> GREETING
tumi ki korte paro -> CLINIC_INFO
koto doctor ache -> CLINIC_INFO
tell me about the hospital -> CLINIC_INFO

Reply with ONLY the intent name (ONE WORD, uppercase).
No punctuation, no explanation, no quotes, no extra words.
"""


VALID_INTENTS = {
    "SYMPTOM", "BOOK_NEW", "PROVIDE_DAY", "PROVIDE_NAME",
    "CONFIRM_YES", "CONFIRM_NO", "CHANGE_DOCTOR",
    "CHANGE_DAY", "CANCEL", "RESTART", "DOCTOR_INFO",
    "CLINIC_INFO", "GREETING", "GENERAL",
}


def classify_intent(message, state="idle"):
    """
    Returns the intent string.
    The `state` parameter is accepted for API compatibility but
    is no longer injected into the prompt (it caused issues).
    """
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": INTENT_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=200,
        )
        reply = response.choices[0].message.content
        if not reply:
            return "GENERAL"

        # Clean up reply
        reply = reply.strip().upper()
        reply = reply.strip(".").strip('"').strip("'").strip()

        # Direct match
        if reply in VALID_INTENTS:
            return reply

        # Substring match (fallback)
        for intent in VALID_INTENTS:
            if intent in reply:
                return intent

        return "GENERAL"
    except Exception:
        print("Intent Classifier Error:")
        print(traceback.format_exc())
        return "GENERAL"