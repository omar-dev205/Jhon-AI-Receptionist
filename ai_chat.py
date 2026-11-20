"""
General AI conversation for the clinic chatbot.

Handles messages that are NOT about booking or symptoms:
  - Greetings ("hi", "how are you")
  - Questions about the clinic (doctors, departments, hours)
  - Small talk
  - Any other off-topic questions

Uses the same Groq LLM as the booking flow, but with a
different system prompt (friendly clinic receptionist).
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


def _build_clinic_facts():
    """Builds a short fact-sheet about the clinic for the AI."""
    from clinic_data import CLINIC_DOCTORS, get_departments
    from clinic_config import get_clinic_info

    info = get_clinic_info()
    lines = []
    lines.append(f"Clinic: {info['name']}")
    lines.append(f"Address: {info['address']}")
    lines.append(f"Phone: {info['phone']}")
    lines.append(f"Email: {info['email']}")
    lines.append("")
    lines.append(f"Departments ({len(get_departments())}):")
    for dept in get_departments():
        docs = [d["name"] for d in CLINIC_DOCTORS if d["department"] == dept]
        lines.append(f"  - {dept}: {', '.join(docs)}")
    lines.append("")
    lines.append(f"Total doctors: {len(CLINIC_DOCTORS)}")
    return "\n".join(lines)


SYSTEM_PROMPT = """
You are Jhon, a warm, friendly, professional receptionist at
HealthCare Hospital in Dhaka, Bangladesh.

You help patients in English, Bangla, and Banglish. Reply in
whatever language the patient writes in (or a natural mix).

Your role:
  - Answer general questions about the clinic (doctors, departments,
    hours, location, services).
  - Handle greetings and small talk warmly and briefly.
  - For anything health-related, guide the patient toward booking
    an appointment (the booking flow will handle the rest).

Rules:
  - You are NOT a doctor. Never diagnose, prescribe, or give
    medical advice.
  - If a patient describes a symptom, DO NOT comment medically.
    Just acknowledge briefly and tell them you'll help them find
    the right doctor — then ask for their symptom in one short
    sentence so the booking flow can take over.
  - Never invent doctors, departments, or schedules. Use ONLY the
    clinic facts below.
  - Keep replies SHORT (1-3 sentences). No long paragraphs.
  - For emergencies (severe chest pain, difficulty breathing,
    etc.), advise immediate emergency care.

--- CLINIC FACTS ---
{clinic_facts}
--- END CLINIC FACTS ---
"""


def general_reply(message, lang="en"):
    """
    Returns a reply string for a general (non-booking) message,
    or None on failure.

    lang: "bn" -> reply in Bangla (Bengali script, common English
    words mixed in); "en" -> reply in English. The instruction is
    explicit, because the base prompt's "whatever language the
    patient writes in" was not reliable on its own.

    Guard against cut-off fragments (e.g. "হ্যাঁ, Dr."): if the
    LLM reply is empty or shorter than 20 characters after
    stripping, retry once with a lower temperature (0.2); if it
    is still bad, return a safe fallback instead of the fragment.
    """
    try:
        facts = _build_clinic_facts()
        system_prompt = SYSTEM_PROMPT.replace("{clinic_facts}", facts)
        if lang == "bn":
            system_prompt += (
                "\nIMPORTANT — LANGUAGE: the patient wrote in Bangla "
                "(Banglish or Bengali script). Reply in Bangla "
                "(Bengali script). You may use common English words "
                "(doctor, appointment, schedule, serial) inside "
                "Bangla sentences, like a Bangladeshi receptionist "
                "texting a patient. Do NOT reply in English.\n"
            )
        else:
            system_prompt += (
                "\nIMPORTANT — LANGUAGE: reply in English.\n"
            )
    except Exception:
        print("General AI error:")
        print(traceback.format_exc())
        return None

    for temperature in (0.4, 0.2):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message},
                ],
                temperature=temperature,
                max_completion_tokens=200,
            )
            reply = response.choices[0].message.content
            if reply and len(reply.strip()) >= 20:
                return reply.strip()
        except Exception:
            print("General AI error:")
            print(traceback.format_exc())

    if lang == "bn":
        return ("আমি সাহায্য করতে পারি। আরেকটু বলবেন কী লাগবে — "
                "যেমন কোন doctor, বা কী সমস্যা?")
    return ("I can help with that. Could you tell me a little more "
            "about what you need — for example, which doctor, or "
            "what problem?")