"""
AI-powered medical triage classifier.

Uses the Groq LLM to map ANY patient message (English / Bangla /
Banglish) to one of the clinic's known departments — WITHOUT any
hardcoded keyword list.

This handles:
  - Spelling variations ("fiver" → fever)
  - Typos ("tempure" → temperature)
  - Bangla script ("জ্বর হয়েছে")
  - Banglish ("amr joor hoise")
  - Indirect phrasing ("I don't feel well")
  - Body-part references ("my head is paining")
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


SYSTEM_PROMPT_TEMPLATE = """You are a medical triage classifier for a clinic.

The patient message may be in English, Bangla, or Banglish
(Romanized Bangla). It may have spelling mistakes, typos, or
unusual phrasings.

Your job: read the message and choose the MOST APPROPRIATE
department from the list below.

DEPARTMENTS (choose EXACTLY ONE, or NONE):
{department_list}

RULES:
- If the message describes ANY physical symptom, complaint, pain,
  illness, or body-part problem — even with typos or indirect
  wording — pick the closest matching department.
- Be forgiving of spelling: "fiver" = fever, "joor" = fever,
  "jhor" = fever, "tempure" = temperature, "matha batha" =
  headache, "chokh" = eye, etc.
- If the message names a DOCTOR, or asks about a doctor,
  schedule, or the clinic in general (NOT describing a symptom),
  reply NONE.
- If the message is a greeting, thanks, or small talk, reply NONE.
- If you're unsure but a symptom IS mentioned, still pick the
  closest department. Only reply NONE if there is genuinely no
  symptom.

EXAMPLES:
"fiver" -> General Medicine
"fivver" -> General Medicine
"joor" -> General Medicine
"my body tempure is high" -> General Medicine
"body temperature high" -> General Medicine
"i feel unwell" -> General Medicine
"amr jor hoise" -> General Medicine
"matha betha korche" -> Neurology
"my head hurts" -> Neurology
"chokhe jala jala" -> Ophthalmology
"my eye is red" -> Ophthalmology
"chokh diye pani porse" -> Ophthalmology
"buke betha" -> Cardiology
"chest pain" -> Cardiology
"pet betha" -> Gastroenterology
"stomach pain" -> Gastroenterology
"kan betha" -> ENT
"ear pain" -> ENT
"dat betha" -> Dentistry
"tooth pain" -> Dentistry
"chamra chulkani" -> Dermatology
"skin rash" -> Dermatology
"hatu betha" -> Orthopedics
"knee pain" -> Orthopedics
"bacchar jor" -> Pediatrics
"child has fever" -> Pediatrics
"urine problem" -> Urology
"kidney stone" -> Urology

Reply with ONLY the department name (exactly as written above)
or NONE. No punctuation, no explanation, no extra words."""


# Cache department list to avoid rebuilding on every call
_department_cache = None


def _get_department_list():
    global _department_cache
    if _department_cache is None:
        from doctor_router import SYMPTOM_DEPARTMENT_MAP
        _department_cache = list(SYMPTOM_DEPARTMENT_MAP.keys())
    return _department_cache


def ai_detect_department(message, departments=None):
    """
    Returns the best-matching department, or
    "NEEDS_CLARIFICATION", or None (if NONE).
    """
    if departments is None:
        departments = _get_department_list()

    department_list = "\n".join(f"- {d}" for d in departments)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        department_list=department_list
    )

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=20,
        )
        reply = response.choices[0].message.content
        if not reply:
            return None

        reply = reply.strip().strip(".").strip().strip('"').strip("'")

        # Explicit NEEDS_CLARIFICATION?
        if "NEEDS_CLARIFICATION" in reply.upper():
            return "NEEDS_CLARIFICATION"

        # Explicit NONE?
        if reply.upper() == "NONE":
            return None

        # Match to a department
        reply_lower = reply.lower()
        for dept in departments:
            if dept.lower() == reply_lower:
                return dept
        for dept in departments:
            if dept.lower() in reply_lower:
                return dept

        return None
    except Exception:
        print("AI Classifier Error:")
        print(traceback.format_exc())
        return None


# Fast day-name check (no API call): English + romanized Bangla
# spellings patients actually type, + Bangla script.
# This runs BEFORE the LLM, so "sonibar" works even if the LLM
# misses it. Add a new spelling here when a patient writes it
# differently — no other code needs to change.
_DAY_NAME_ALIASES = {
    "saturday": "Saturday", "sonibar": "Saturday",
    "shonibar": "Saturday", "শনিবার": "Saturday",
    "sunday": "Sunday", "robibar": "Sunday", "robbar": "Sunday",
    "রবিবার": "Sunday",
    "monday": "Monday", "sombar": "Monday", "shombar": "Monday",
    "সোমবার": "Monday",
    "tuesday": "Tuesday", "mongolbar": "Tuesday",
    "মঙ্গলবার": "Tuesday",
    "wednesday": "Wednesday", "budbar": "Wednesday",
    "budhbar": "Wednesday", "বুধবার": "Wednesday",
    "thursday": "Thursday", "brihospotibar": "Thursday",
    "brihoshpotibar": "Thursday", "বৃহস্পতিবার": "Thursday",
    "friday": "Friday", "sukrobar": "Friday", "shukrobar": "Friday",
    "শুক্রবার": "Friday",
}


def ai_detect_day_name(message):
    """
    Returns an English weekday name if the message mentions one,
    else None.
    """
    # 1) Fast check first — covers "sonibar", "shonibar", etc.
    text_lower = message.lower()
    for alias in sorted(_DAY_NAME_ALIASES, key=len, reverse=True):
        if alias in text_lower:
            return _DAY_NAME_ALIASES[alias]

    # 2) Otherwise ask the LLM (handles the spellings not listed above).
    system_prompt = (
        "The patient message may mention a day of the week, in "
        "English, Bangla, or Banglish (Romanized Bangla). "
        "If it does, reply with ONLY the English day name: "
        "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, "
        "or Sunday. "
        "If no day of the week is mentioned, reply with ONLY: NONE."
    )
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=5,
        )
        reply = response.choices[0].message.content
        if not reply:
            return None
        reply = reply.strip().strip(".").title()
        weekdays = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ]
        if reply in weekdays:
            return reply
        return None
    except Exception:
        print("AI Day Detection Error:")
        print(traceback.format_exc())
        return None


def ai_is_booking_intent(text):
    """
    LLM fallback for is_booking_intent() in main.py (Telegram bot).

    main.py first checks a fixed keyword list (fast, free). When no
    keyword matches, it asks this function — real patients phrase
    booking in countless spelling variants that no fixed list can
    cover ("dekhate" vs "dekhaite" vs "dekhbo" ...).

    Returns True if the message asks to book an appointment or see
    a doctor, else False. False on any error (safe default — the
    message just falls through to the normal flow instead of
    crashing the bot).
    """
    system_prompt = (
        "The patient message may be in English, Bangla, or Banglish "
        "(Romanized Bangla). Does the patient want to book an "
        "appointment or see a doctor? Reply with ONLY: YES or NO."
    )
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_completion_tokens=5,
        )
        reply = response.choices[0].message.content
        if not reply:
            return False
        return reply.strip().upper().startswith("YES")
    except Exception:
        print("AI Booking Intent Error:")
        print(traceback.format_exc())
        return False