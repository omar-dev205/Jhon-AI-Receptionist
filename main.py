import os
import re
import asyncio
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from clinic_config import get_booking_mode, get_clinic_info
from clinic_data import CLINIC_DOCTORS, find_doctor_by_name, get_departments, find_doctors_by_department
from doctor_router import find_doctors_for_problem
from booking_service import check_booking, create_booking
from ai_classifier import ai_is_booking_intent, ai_detect_day_name
from voice import transcribe_voice
from notifications import notify_new_booking
from reports import run_daily_report
from scheduler import start_scheduler
from messages import t, detect_lang
from web_agent import _direct_symptom_match
import database


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing")

client = Groq(api_key=GROQ_API_KEY)
MODEL_NAME = "openai/gpt-oss-20b"

# Telegram user IDs allowed to run /sheet (comma-separated in .env,
# e.g. ADMIN_TELEGRAM_IDS=123456789,987654321). This is a REQUIRED
# access control: /sheet exposes patient names and phone numbers, and
# this bot is otherwise open to any patient who messages it.
ADMIN_TELEGRAM_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if uid.strip().isdigit()
}


# ---------------------------------------------------------
# DATE
# ---------------------------------------------------------
DAY_ALIASES = {
    "sunday": "Sunday", "robibar": "Sunday", "রবিবার": "Sunday",
    "monday": "Monday", "sombar": "Monday", "shombar": "Monday",
    "সোমবার": "Monday",
    "tuesday": "Tuesday", "mongolbar": "Tuesday", "mangolbar": "Tuesday",
    "মঙ্গলবার": "Tuesday",
    "wednesday": "Wednesday", "budbar": "Wednesday", "budhbar": "Wednesday",
    "budhabar": "Wednesday", "বুধবার": "Wednesday",
    "thursday": "Thursday", "brihospotibar": "Thursday",
    "brihoshpotibar": "Thursday", "brihospotibar": "Thursday",
    "বৃহস্পতিবার": "Thursday",
    "friday": "Friday", "sukrobar": "Friday", "shukrobar": "Friday",
    "শুক্রবার": "Friday",
    "saturday": "Saturday", "shonibar": "Saturday", "sonibar": "Saturday",
    "শনিবার": "Saturday",
}


def detect_day_name(text):
    text_lower = text.lower().strip()
    for keyword, day in DAY_ALIASES.items():
        if keyword in text_lower:
            return day
    # Keyword list doesn't cover every real-world spelling variant of
    # a Banglish day name — ask the LLM as a fallback (same pattern
    # used for department/language/booking-intent detection).
    return ai_detect_day_name(text)


def get_next_date_for_day(day_name, skip_weeks=0):
    from datetime import timedelta
    today = datetime.now().date()
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]
    target = weekdays.index(day_name)
    current = today.weekday()
    days_ahead = (target - current) % 7
    days_ahead += skip_weeks * 7
    return today + timedelta(days=days_ahead)

def parse_explicit_date(text):
    patterns = [
        r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b",
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            values = [int(value) for value in match.groups()]
            if len(str(values[0])) == 4:
                year, month, day = values
            else:
                day, month, year = values
            return datetime(year, month, day).date()
        except ValueError:
            return None
    return None

def parse_booking_date(text):
    explicit_date = parse_explicit_date(text)
    if explicit_date:
        return explicit_date
    
    day_name = detect_day_name(text)
    if day_name:
        text_lower = text.lower()
        next_week_keywords = [
            "next week", "next ", "porer", "pore", "agami", "agamee",
            "পরের", "আগামী", "পরবর্তী",
        ]
        skip_weeks = 0
        for kw in next_week_keywords:
            if kw in text_lower:
                skip_weeks = 1
                break
        
        return get_next_date_for_day(day_name, skip_weeks=skip_weeks)
    
    return None


# ---------------------------------------------------------
# DOCTOR MATCHING
# ---------------------------------------------------------
def find_doctor_from_message(message, candidate_doctors=None):
    text = message.lower().strip()
    doctors = candidate_doctors or CLINIC_DOCTORS

    for doctor in doctors:
        full_name = doctor["name"].lower()
        if full_name in text:
            return doctor
        name_without_title = full_name.replace("dr. ", "")
        if name_without_title in text:
            return doctor
        parts = name_without_title.split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = parts[-1]
            if first_name in text and last_name in text:
                return doctor

    words = re.findall(r"[a-zA-Z]+", text)
    # Combine the evidence across ALL words: one strong word
    # ("ahamed" -> "ahmed" 0.909) matches TWO doctors, so a single
    # global best is decided by list order. "sara ahamed" must
    # match Sarah Ahmed (sara->sarah 0.889 + ahamed->ahmed 0.909),
    # not Ahmed Hassan (only the 0.909).
    best_doctor = None
    best_total = 0.0
    for doctor in doctors:
        name = doctor["name"].lower().replace("dr. ", "")
        name_parts = name.split()
        total = 0.0
        max_single = 0.0
        for word in words:
            word_best = 0.0
            for name_part in name_parts:
                score = SequenceMatcher(None, word, name_part).ratio()
                if score > word_best:
                    word_best = score
            if word_best >= 0.7:
                total += word_best
            if word_best > max_single:
                max_single = word_best
        if max_single >= 0.88 and total > best_total:
            best_total = total
            best_doctor = doctor

    if best_doctor is not None:
        return best_doctor
    return None


# ---------------------------------------------------------
# SYMPTOM / CONVERSATION DETECTION
# ---------------------------------------------------------
RESOLVED_PHRASES = [
    "matha betha cole gese", "matha betha chole gese",
    "headache is gone", "headache gone", "pain is gone", "pain gone",
    "problem is gone", "problem gone", "betha cole gese",
    "betha chole gese", "ব্যথা চলে গেছে", "ব্যথা কমে গেছে",
    "সমস্যা চলে গেছে",
]


def is_resolved_message(text):
    message = text.lower().strip()
    return any(phrase in message for phrase in RESOLVED_PHRASES)


def has_new_symptom(text):
    result = find_doctors_for_problem(text)
    return result["found"]


# ---------------------------------------------------------
# GENERIC "I WANT A DOCTOR / BOOK APPOINTMENT" INTENT
# ---------------------------------------------------------
# Real patients very often say this WITHOUT naming a symptom
# ("Ami doctor dekhatw cai", "book an appointment"). This has no
# department to route on, so instead of letting it fall through to
# the open-ended general AI (which used to invent a generic
# "give me your name, number and department" paragraph, or even
# wrongly claim it doesn't know doctor names), we catch it here and
# ask for the symptom directly — keeping the patient inside the
# actual booking flow.
BOOKING_INTENT_PHRASES = [
    "want to see a doctor", "see a doctor", "book an appointment",
    "book appointment", "make an appointment", "need a doctor",
    "want an appointment", "schedule an appointment",
    "doctor dekhaite cai", "doctor dekhano", "doctor dekhbo",
    "doctor dekhte cai", "doctor dekhatw cai", "doctor dekhaite chai",
    "doctor dekhate cai", "doctor dekhate chai",
    "appointment book", "appointment nite cai", "appointment korte cai",
    "doctor lagbe", "daktar dekhaite cai", "daktar dekhate cai",
    "daktar lagbe", "doctor er sathe dekha", "book koro",
    "ডাক্তার দেখাতে চাই", "অ্যাপয়েন্টমেন্ট বুক", "ডাক্তার লাগবে",
    "ডাক্তারের সাথে দেখা করতে চাই",
]


def is_booking_intent(text):
    """
    Keyword check first (fast, free). If it misses, ask the LLM —
    real patients phrase this in countless spelling variants
    ("dekhate" vs "dekhaite" vs "dekhbo" etc.) that a fixed list can
    never fully cover.
    """
    message = text.lower().strip()
    if any(phrase in message for phrase in BOOKING_INTENT_PHRASES):
        return True
    return ai_is_booking_intent(text)


# ---------------------------------------------------------
# "TELL ME ABOUT THE HOSPITAL / DEPARTMENTS / DOCTORS" INTENT
# ---------------------------------------------------------
INFO_REQUEST_PHRASES = [
    "what departments", "list of departments", "which doctors",
    "list of doctors", "tell me about the hospital",
    "tell me about the clinic", "about your clinic", "about the hospital",
    "what doctors do you have", "departments do you have",
    "kon kon department", "ki ki department", "kon department gula",
    "department gula ki", "doctor list", "shob doctor", "sob doctor",
    "clinic somporke", "hospital somporke", "clinic er shomporke",
    "ki ki doctor ache", "kon kon doctor ache", "kon kon doctor",
    "hospital ta ki", "clinic ta ki",
    "কি কি বিভাগ", "কোন কোন বিভাগ", "কি কি ডাক্তার", "কোন কোন ডাক্তার",
    "হাসপাতাল সম্পর্কে", "ক্লিনিক সম্পর্কে", "ডাক্তারদের তালিকা",
]


def is_info_request(text):
    message = text.lower().strip()
    return any(phrase in message for phrase in INFO_REQUEST_PHRASES)


def build_clinic_overview_text():
    lines = []
    for department in get_departments():
        doctors = find_doctors_by_department(department)
        doctor_names = ", ".join(doctor["name"] for doctor in doctors)
        lines.append(f"- {department}: {doctor_names}")
    return "\n".join(lines)


def build_clinic_info_reply(lang):
    info = get_clinic_info()
    return t(
        "clinic_overview",
        lang,
        clinic_name=info["name"],
        address=info["address"],
        phone=info["phone"],
        departments=build_clinic_overview_text(),
    )


# ---------------------------------------------------------
# NAME + PHONE (asked together)
# ---------------------------------------------------------
def parse_name_and_phone(text):
    """
    Real patients naturally give name and phone together
    ("Karim Rahman, 01700000000"). Extract whichever of the two
    are present in the message; the caller merges this with
    anything already captured from a previous message.
    """
    phone_match = re.search(r"(\+?\d[\d\-\s]{8,14}\d)", text)
    phone = None
    name_part = text

    if phone_match:
        raw_phone = phone_match.group(1)
        candidate_phone = re.sub(r"[\s\-\(\)]", "", raw_phone)
        if re.fullmatch(r"\+?\d{10,15}", candidate_phone):
            phone = candidate_phone
            name_part = text[: phone_match.start()] + text[phone_match.end():]

    name = re.sub(r"\s+", " ", name_part).strip(" ,.-\n\t")
    name = name if len(name) >= 2 else None

    return name, phone


# ---------------------------------------------------------
# SAFETY
# ---------------------------------------------------------
def is_emergency_message(text):
    message = text.lower()
    emergency_terms = [
        "severe chest pain", "crushing chest pain", "difficulty breathing",
        "can't breathe", "cannot breathe", "shortness of breath",
        "unconscious", "passed out", "heavy bleeding", "severe bleeding",
        "stroke", "heart attack", "অসহ্য বুক ব্যথা", "শ্বাস নিতে পারছি না",
        "শ্বাসকষ্ট", "অজ্ঞান", "অনেক রক্তপাত", "হার্ট অ্যাটাক", "স্ট্রোক",
    ]
    return any(term in message for term in emergency_terms)


# ---------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------
def format_doctors(doctors):
    lines = []
    for doctor in doctors:
        lines.append(f"- {doctor['name']} — {doctor['specialization']}")
    return "\n".join(lines)


def doctor_schedule_text(doctor):
    lines = []
    for day, schedule in doctor["schedule"].items():
        lines.append(f"- {day}: {schedule['start']} - {schedule['end']}")
    return "\n".join(lines)


def save_state(context, **values):
    for key, value in values.items():
        context.user_data[key] = value


def clear_booking_state(context):
    keys = [
        "state", "candidate_doctors", "selected_doctor",
        "booking_date", "booking_day", "patient_name", "phone",
        "suggested_doctor",
    ]
    for key in keys:
        context.user_data.pop(key, None)
    # NOTE: "language" is intentionally NOT cleared here — a symptom
    # being resolved mid-conversation shouldn't reset the locked language.


# ---------------------------------------------------------
# GENERAL AI
# ---------------------------------------------------------
SYSTEM_PROMPT = """
You are Jhon, a professional medical clinic receptionist.
Patients may write to you in English, Bangla, or Banglish (Romanized
Bangla) — understand whatever they write, and reply in the same
language the patient uses (English, Bangla, or Banglish).

Important rules:
- You are a receptionist, not a doctor.
- Do not diagnose diseases.
- Do not prescribe medicine.
- Do not invent doctors, schedules, availability, or booking information.
- Do not claim an appointment is confirmed unless Python booking logic confirms it.
- Do NOT give medical advice, home remedies, or care instructions of any
  kind, even simple-sounding ones (e.g. "wash with cold water", "avoid
  rubbing your eyes"). If the patient describes any symptom or discomfort,
  do not comment on it medically at all — just briefly acknowledge it and
  ask them to confirm they'd like help finding the right department/doctor.
- Do not claim you don't know doctor names or departments — you work at
  a real clinic with real doctors; if you're unsure, ask the patient to
  describe their symptom so the system can route them properly, rather
  than saying you can't help.
- CRITICAL: Never suggest, name, or guess which department, specialist,
  or type of doctor (e.g. "eye specialist", "our cardiologist") fits the
  patient's symptom yourself — that decision belongs entirely to the
  clinic's routing system, not to you. If a patient mentions a symptom
  and you are replying (meaning the routing system did not already
  match it), simply and briefly acknowledge what they said and ask them
  to describe their main symptom in one short, plain sentence so it can
  be matched correctly. Do not offer any department/specialist name at
  all, not even as a guess or a question ("would you like an eye
  specialist?").
- Keep answers short and natural.
- Reply in the language specified by the language instruction you are given.
- For emergencies, advise immediate emergency medical care.
"""


def ask_ai(user_message, lang="en"):
    if lang == "bn":
        lang_instruction = ("Reply in Bangla (Bengali script). You may use "
                            "common English words (doctor, appointment, "
                            "schedule, serial) inside Bangla sentences, "
                            "like a Bangladeshi receptionist texting a "
                            "patient. Do NOT reply in English.")
    else:
        lang_instruction = "Reply naturally in English, in a warm, professional tone."
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": lang_instruction},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
            max_completion_tokens=300,
            # NOTE: removed `include_reasoning=False` — this kwarg is not
            # accepted by the current Groq SDK's chat.completions.create()
            # and was raising an exception on every single call, which is
            # why every unmatched message silently fell back to the canned
            # fallback text below instead of a real AI reply.
        )
        reply = response.choices[0].message.content
        if reply:
            return reply.strip()
    except Exception:
        print("AI Error:")
        print(traceback.format_exc())
    return None


# ---------------------------------------------------------
# START
# ---------------------------------------------------------
async def start_command(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "Hello! I'm Jhon, your AI Medical Clinic Receptionist.\n\n"
        "I can help you find a doctor, check schedules, "
        "and book an appointment.\n\n"
        "How can I help you today?"
    )


async def clear_command(update, context):
    context.user_data.clear()
    await update.message.reply_text("Conversation cleared. How can I help you?")


async def help_command(update, context):
    await update.message.reply_text(
        "You can ask me things like:\n\n"
        "• Amar matha betha kortase.\n"
        "• Amr dater marir moddeh betha.\n"
        "• Amr buke betha.\n"
        "• I want to see Dr. Sarah Ahmed.\n"
        "• I want an appointment on Friday.\n"
        "• I want to book an appointment."
    )


# ---------------------------------------------------------
# ADMIN: PRINTABLE APPOINTMENT SHEET
# ---------------------------------------------------------
async def sheet_command(update, context):
    """
    Staff-only command that generates a printable appointment sheet
    (CSV, opens directly in Excel) for a given date, optionally
    narrowed to one doctor or one department.

    Usage:
        /sheet
            -> today, all doctors
        /sheet 2026-09-10
            -> that date, all doctors
        /sheet 2026-09-10 Dr. Karim Hossain
            -> that date, one doctor
        /sheet 2026-09-10 department Ophthalmology
            -> that date, one department
    """
    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_TELEGRAM_IDS or user_id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text(
            "Sorry, this command is restricted to clinic staff."
        )
        return

    args = context.args
    date_str = datetime.now().date().strftime("%Y-%m-%d")
    doctor_name = None
    department = None

    if args:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[0]):
            date_str = args[0]
            rest = args[1:]
        else:
            rest = args

        if rest:
            if rest[0].lower() == "department" and len(rest) > 1:
                department = " ".join(rest[1:])
            else:
                doctor_name = " ".join(rest)

    rows = database.get_appointments_for_date(date_str, doctor_name=doctor_name, department=department)

    if not rows:
        await update.message.reply_text(f"No bookings found for {date_str}.")
        return

    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    os.makedirs(export_dir, exist_ok=True)

    if doctor_name:
        # A single doctor was named directly — one file, exactly as before.
        doctors_to_export = [doctor_name]
    else:
        # Whole clinic or a whole department: one CSV PER DOCTOR, so
        # each doctor's own patient list can be printed and handed
        # out separately — matching how this actually works in a
        # real clinic, where each doctor's chamber has its own list.
        doctors_to_export = database.get_doctors_with_bookings(date_str, department=department)

    label = date_str
    if doctor_name:
        label += f" — {doctor_name}"
    if department:
        label += f" — {department}"
    await update.message.reply_text(f"{len(rows)} appointment(s) for {label} across {len(doctors_to_export)} doctor(s)")

    for name in doctors_to_export:
        doctor_filepath = os.path.join(
            export_dir,
            f"sheet_{date_str}_{name.replace(' ', '_').replace('.', '')}.csv",
        )
        database.export_appointments_to_csv(date_str, doctor_filepath, doctor_name=name)
        with open(doctor_filepath, "rb") as csv_file:
            await update.message.reply_document(document=csv_file, filename=os.path.basename(doctor_filepath))
async def report_command(update, context):
    """
    Staff-only command that triggers the full daily report on
    demand:

        /report                 -> today
        /report 2026-09-10      -> specific date

    Writes per-(department, doctor) Google Sheets tabs, then
    emails the summary to the admin.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_TELEGRAM_IDS or user_id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text(
            "Sorry, this command is restricted to clinic staff."
        )
        return

    date_str = (
        context.args[0]
        if context.args and re.fullmatch(r"\d{4}-\d{2}-\d{2}", context.args[0])
        else datetime.now().date().strftime("%Y-%m-%d")
    )

    await update.message.reply_text(f"📊 Generating report for {date_str}...")

    report = run_daily_report(date_str, send_email=True)

    await update.message.reply_text(
        f"✅ Report done for {date_str}\n\n"
        f"Sheets written: {report['sheets_written']}\n"
        f"Rows written:   {report['rows_written']}\n\n"
        f"{report['summary']}"
    )

# ---------------------------------------------------------
# MAIN MESSAGE HANDLER
# ---------------------------------------------------------
def _is_yes_like(message):
    """Word-boundary yes / book-command check, same semantics as
    web_agent._is_yes + _is_book_command. A bare "yes" / "ok" /
    "hea" / "korte cai" after a single-doctor suggestion means
    "book with them". Substring matching is NOT used ("ok"
    inside "book", "han" inside "khan" must not fire)."""
    low = message.lower().strip()
    yes_words = (
        "yes", "yeah", "yep", "ok", "okay", "sure", "haa", "ha",
        "hya", "han", "ji", "thik ache", "thik achhe", "hea", "heya",
    )
    if low in yes_words:
        return True
    if re.search(r"\b(yes|yeah|yep|ok|okay|sure|haa|ha|hya|han|ji|hea|heya)\b", low):
        return True
    book_words = (
        "book korun", "book koro", "book korte cai", "book korte chai",
        "book chai", "book cai", "appointment nite cai",
        "appointment nite chai", "appointment nite cacchi",
        "appointment chai", "appointment cai", "confirm koro",
        "confirm korun", "korte cai", "korte chai",
    )
    return any(w in low for w in book_words)


def _is_decline_like(message):
    """'thak book korbo na' / 'nite cacchi na' — a decline,
    not a name. Mirrors web_agent._is_decline."""
    low = message.lower().strip()
    if low in ("na", "nah", "naa", "no", "nope", "thak"):
        return True
    phrases = (
        "book korbo na", "appointment nite cacchi na",
        "appointment nite chai na", "nite cacchi na", "nite chai na",
        "chai na", "cai na", "lagbe na", "dorkar nai", "dorkar nei",
        "cancel booking", "book korte chai na", "book korte cai na",
    )
    if any(p in low for p in phrases):
        return True
    return bool(re.search(r"\bthak\b", low))


async def process_patient_message(update, context, message):
    """
    Core conversation logic, shared by both text messages and
    transcribed voice messages (see handle_message / handle_voice
    below) — so a patient gets the exact same booking experience
    whether they type or speak.
    """
    # Language follows the patient: Banglish -> Bangla replies,
    # English -> English replies. detect_lang returns None for
    # bare confirmations ("yes"/"ok"), so the language set by
    # the last real message stays.
    _detected = detect_lang(message)
    if _detected:
        context.user_data["lang"] = _detected
    lang = context.user_data.get("lang", "en")
    state = context.user_data.get("state", "idle")

    try:
        # -------------------------------------------------
        # EMERGENCY (always checked first, unconditionally)
        # -------------------------------------------------
        if is_emergency_message(message):
            await update.message.reply_text(t("emergency_warning", lang))
            return

        # -------------------------------------------------
        # TRY TO RESOLVE THE CURRENT STATE'S EXPECTED INPUT FIRST
        # -------------------------------------------------
        # If we're mid-booking-flow and expecting a specific kind of
        # reply (a doctor name, a date, name+phone, yes/no), try
        # parsing the message as THAT first. Only if it doesn't fit
        # do we fall through to the global checks below (new
        # symptom, booking intent, general AI, etc).
        #
        # This matters because the global "new symptom" check calls
        # an AI classifier that can occasionally misfire on
        # unrelated text (e.g. a doctor's name typed with a small
        # typo was once misclassified as an ENT symptom) — checking
        # the state's own expected format first prevents that from
        # hijacking a message that was actually valid input for
        # where the conversation already is.
        if state == "waiting_for_doctor":
            candidate_doctors = context.user_data.get("candidate_doctors", [])
            doctor = find_doctor_from_message(message, candidate_doctors)
            if not doctor:
                # Patient may be naming a doctor OUTSIDE the department
                # that was just suggested (e.g. they want a specific
                # doctor for an unrelated reason, or changed their mind
                # about which department). Search the FULL clinic
                # roster before giving up — an explicitly named doctor
                # should always be honored over the narrower candidate
                # list.
                doctor = find_doctor_from_message(message)
            if doctor:
                save_state(context, state="waiting_for_date", selected_doctor=doctor["name"])
                schedule = doctor_schedule_text(doctor)
                reply = t(
                    "doctor_selected_schedule",
                    lang,
                    doctor=doctor["name"],
                    schedule=schedule,
                )
                await update.message.reply_text(reply)
                return

            # No doctor named — but a "yes" / "ok" / "hea" /
            # "korte cai" after a single-doctor suggestion means
            # "book with them": enter the booking flow for the
            # suggested doctor (no need to repeat the name).
            # Single-day doctor -> resolve with that day's next
            # date straight away; multi-day -> show the schedule
            # and ask which day.
            if _is_yes_like(message):
                suggested = context.user_data.get("suggested_doctor")
                if suggested:
                    sdoc = find_doctor_by_name(suggested)
                    if sdoc:
                        booking_date = parse_booking_date(message)
                        if booking_date is None and len(sdoc.get("schedule", {})) == 1:
                            single_day = next(iter(sdoc["schedule"]))
                            booking_date = get_next_date_for_day(single_day)
                        if booking_date is not None:
                            day_name = booking_date.strftime("%A")
                            today = datetime.now().date()
                            availability = check_booking(
                                doctor_name=sdoc["name"],
                                date=booking_date,
                                day=day_name,
                                today=today,
                                lang=lang,
                            )
                            if availability.get("available"):
                                save_state(
                                    context,
                                    state="waiting_for_details",
                                    selected_doctor=sdoc["name"],
                                    booking_date=booking_date,
                                    booking_day=day_name,
                                )
                                serial = availability.get("next_serial")
                                reply = t(
                                    "slot_available_ask_name",
                                    lang,
                                    doctor=sdoc["name"],
                                    day=day_name,
                                    date=booking_date.strftime("%d %B %Y"),
                                    serial=serial,
                                )
                                await update.message.reply_text(reply)
                                return
                            save_state(context, state="waiting_for_date", selected_doctor=sdoc["name"])
                            await update.message.reply_text(
                                availability.get("message", t("no_serials_available", lang, doctor=sdoc["name"], day=day_name))
                            )
                            return
                        save_state(context, state="waiting_for_date", selected_doctor=sdoc["name"])
                        schedule = doctor_schedule_text(sdoc)
                        reply = t(
                            "doctor_selected_schedule",
                            lang,
                            doctor=sdoc["name"],
                            schedule=schedule,
                        )
                        await update.message.reply_text(reply)
                        return
            # not matched -- fall through to global checks below

        elif state == "waiting_for_date":
            doctor_name = context.user_data.get("selected_doctor")
            if not doctor_name:
                clear_booking_state(context)
            else:
                # A different doctor named while picking a day
                # switches to THAT doctor first (before any
                # yes/date handling for the old doctor).
                _new = find_doctor_from_message(message)
                if _new and _new["name"] != doctor_name:
                    save_state(context, state="waiting_for_date", selected_doctor=_new["name"])
                    schedule = doctor_schedule_text(_new)
                    reply = t(
                        "doctor_selected_schedule",
                        lang,
                        doctor=_new["name"],
                        schedule=schedule,
                    )
                    await update.message.reply_text(reply)
                    return
                booking_date = parse_booking_date(message)
                if booking_date is None and _is_yes_like(message):
                    # "yes" to the schedule just shown. A single-day
                    # doctor is unambiguous — book that day's next
                    # occurrence (the same Day-4 logic as the website).
                    _doc = find_doctor_by_name(doctor_name)
                    if _doc and len(_doc.get("schedule", {})) == 1:
                        _single = next(iter(_doc["schedule"]))
                        booking_date = get_next_date_for_day(_single)
                if booking_date:
                    day_name = booking_date.strftime("%A")
                    today = datetime.now().date()
                    availability = check_booking(
                        doctor_name=doctor_name,
                        date=booking_date,
                        day=day_name,
                        today=today,
                        lang=lang,
                    )
                    if not availability.get("available"):
                        await update.message.reply_text(
                            availability.get("message", t("no_serials_available", lang, doctor=doctor_name, day=day_name))
                        )
                        return

                    if context.user_data.get("patient_name") and context.user_data.get("phone"):
                        # Name + phone already known (e.g. after a
                        # doctor switch mid-confirmation)? Don't ask
                        # again — go straight to the confirmation
                        # summary.
                        save_state(
                            context,
                            state="confirming_booking",
                            booking_date=booking_date,
                            booking_day=day_name,
                        )
                        reply = t(
                            "booking_details_confirm",
                            lang,
                            doctor=doctor_name,
                            date=booking_date.strftime("%d %B %Y"),
                            name=context.user_data["patient_name"],
                            phone=context.user_data["phone"],
                        )
                        await update.message.reply_text(reply)
                        return
                    save_state(
                        context,
                        state="waiting_for_details",
                        booking_date=booking_date,
                        booking_day=day_name,
                    )
                    serial = availability.get("next_serial")
                    reply = t(
                        "slot_available_ask_name",
                        lang,
                        doctor=doctor_name,
                        day=day_name,
                        date=booking_date.strftime("%d %B %Y"),
                        serial=serial,
                    )
                    await update.message.reply_text(reply)
                    return
                # not parsed as a date -- fall through to global checks below

        elif state == "waiting_for_details":
            new_name, new_phone = parse_name_and_phone(message)

            if new_name and not context.user_data.get("patient_name"):
                save_state(context, patient_name=new_name)
            if new_phone and not context.user_data.get("phone"):
                save_state(context, phone=new_phone)

            have_name = bool(context.user_data.get("patient_name"))
            have_phone = bool(context.user_data.get("phone"))

            if new_name or new_phone:
                if have_name and have_phone:
                    save_state(context, state="confirming_booking")
                    doctor_name = context.user_data["selected_doctor"]
                    booking_date = context.user_data["booking_date"]
                    patient_name = context.user_data["patient_name"]
                    phone = context.user_data["phone"]
                    reply = t(
                        "booking_details_confirm",
                        lang,
                        doctor=doctor_name,
                        date=booking_date.strftime("%d %B %Y"),
                        name=patient_name,
                        phone=phone,
                    )
                    await update.message.reply_text(reply)
                    return

                if have_phone and not have_name:
                    await update.message.reply_text(t("ask_missing_name", lang))
                    return

                if have_name and not have_phone:
                    await update.message.reply_text(t("ask_missing_phone", lang))
                    return
            # neither name nor phone recognized -- fall through to
            # global checks below (e.g. patient typed a new symptom
            # instead), and the invalid_phone hint fires at the end
            # if nothing else matches either.

        elif state == "confirming_booking":
            normalized = message.lower().strip()
            yes_words = {"yes", "yes please", "ha", "hya", "ji", "ঠিক আছে", "হ্যাঁ", "জি", "confirm"}
            no_words = {"no", "না", "cancel", "বাদ", "change"}

            # A different doctor named mid-confirmation ("sorry,
            # I actually want Dr. Sarah Ahmed") switches to THAT
            # doctor — checked BEFORE the "yes" branch, or a
            # correction containing a yes-word would confirm the
            # OLD doctor. Name/phone already given are kept.
            if not _is_decline_like(message):
                _new = find_doctor_from_message(message)
                if _new and _new["name"] != context.user_data.get("selected_doctor"):
                    save_state(context, state="waiting_for_date", selected_doctor=_new["name"])
                    context.user_data.pop("booking_date", None)
                    context.user_data.pop("booking_day", None)
                    schedule = doctor_schedule_text(_new)
                    reply = t(
                        "doctor_selected_schedule",
                        lang,
                        doctor=_new["name"],
                        schedule=schedule,
                    )
                    await update.message.reply_text(reply)
                    return

            if normalized in yes_words or _is_yes_like(message):
                doctor_name = context.user_data["selected_doctor"]
                booking_date = context.user_data["booking_date"]
                booking_day = context.user_data["booking_day"]
                patient_name = context.user_data["patient_name"]
                phone = context.user_data["phone"]

                result = create_booking(
                    doctor_name=doctor_name,
                    date=booking_date,
                    day=booking_day,
                    patient_name=patient_name,
                    phone=phone,
                    today=datetime.now().date(),
                    lang=lang,
                )
                if result.get("success"):
                    appointment = result["appointment"]
                    clear_booking_state(context)
                    reply = t(
                        "booking_success",
                        lang,
                        doctor=appointment["doctor_name"],
                        date=appointment["date"],
                        day=appointment["day"],
                        serial=appointment["serial_number"],
                        time=appointment["estimated_time"],
                        window=appointment["arrival_window"],
                        name=appointment["patient_name"],
                        phone=appointment["phone"],
                    )
                    await update.message.reply_text(reply)
                    clinic_name = get_clinic_info()["name"]
                    await update.message.reply_text(
                        t("booking_thank_you", lang, clinic_name=clinic_name)
                    )
                    asyncio.create_task(
                        asyncio.to_thread(notify_new_booking, appointment)
                    )
                    return

                await update.message.reply_text(
                    result.get("message", t("booking_failed_generic", lang))
                )
                return

            if normalized in no_words:
                clear_booking_state(context)
                await update.message.reply_text(t("booking_cancelled", lang))
                return
            # neither yes nor no -- fall through to global checks below
            # (patient might be asking something else instead of
            # confirming), and ask_yes_no fires at the end if nothing
            # else matches either.

        # -------------------------------------------------
        # RESOLVED SYMPTOM
        # -------------------------------------------------
        if is_resolved_message(message):
            if state != "idle":
                clear_booking_state(context)
            await update.message.reply_text(t("resolved_symptom", lang))
            return

        # -------------------------------------------------
        # HOSPITAL / DEPARTMENT / DOCTOR INFO REQUEST
        # (doesn't touch booking state — patient can ask this mid-flow
        # and continue booking afterward)
        # -------------------------------------------------
        if is_info_request(message):
            await update.message.reply_text(build_clinic_info_reply(lang))
            return

        # -------------------------------------------------
        # NEW SYMPTOM
        # (find_doctors_for_problem now also has an AI fallback
        # classifier for phrasing that isn't in the keyword list —
        # see doctor_router.py / ai_classifier.py. This only runs
        # once the current state's own expected input has already
        # been tried and didn't match, above.)
        # -------------------------------------------------
        # Deterministic keyword match first (the same rules
        # the website uses) — the LLM route below is the fallback.
        # Common symptoms keep working even when the LLM is
        # unreachable or misclassifies.
        direct_dept = _direct_symptom_match(message)
        if direct_dept:
            _direct_docs = find_doctors_by_department(direct_dept)
            if _direct_docs:
                routing_result = {"found": True, "department": direct_dept,
                                  "doctors": _direct_docs}
            else:
                routing_result = {"found": False}
        else:
            routing_result = find_doctors_for_problem(message)
        if routing_result["found"]:
            clear_booking_state(context)
            doctors = routing_result["doctors"]
            save_state(context, state="waiting_for_doctor", candidate_doctors=doctors)
            department = routing_result["department"]

            if len(doctors) == 1:
                doctor_text = f"{doctors[0]['name']} — {doctors[0]['specialization']}"
            else:
                doctor_text = format_doctors(doctors)

            if len(doctors) == 1:
                # Single doctor: ask straight away whether they
                # want an appointment with them. A "yes" then
                # goes into the booking flow (waiting_for_doctor
                # yes-shortcut above).
                save_state(context, suggested_doctor=doctors[0]["name"])
                reply = t(
                    "symptom_single_doctor_suggestion",
                    lang,
                    department=department,
                    doctor_list=doctor_text,
                    doctor_name=doctors[0]["name"],
                )
            else:
                context.user_data.pop("suggested_doctor", None)
                reply = t(
                    "symptom_department_suggestion",
                    lang,
                    department=department,
                    doctor_list=doctor_text,
                )
            await update.message.reply_text(reply)
            return

        # -------------------------------------------------
        # STATE-SPECIFIC "COULDN'T UNDERSTAND" FALLBACKS
        # (reached only if the state's own input AND a new-symptom
        # check both failed to match this message)
        # -------------------------------------------------
        if state == "waiting_for_doctor":
            await update.message.reply_text(t("doctor_not_identified", lang))
            return

        if state == "waiting_for_date":
            await update.message.reply_text(t("ask_day_examples", lang))
            return

        if state == "waiting_for_details":
            await update.message.reply_text(t("invalid_phone", lang))
            return

        if state == "confirming_booking":
            await update.message.reply_text(t("ask_yes_no", lang))
            return

        # -------------------------------------------------
        # DIRECT DOCTOR SEARCH (idle state)
        # -------------------------------------------------
        doctor = find_doctor_from_message(message)
        if doctor:
            save_state(context, state="waiting_for_date", selected_doctor=doctor["name"])
            schedule = doctor_schedule_text(doctor)
            reply = t(
                "doctor_selected_schedule",
                lang,
                doctor=doctor["name"],
                schedule=schedule,
            )
            await update.message.reply_text(reply)
            return

        # -------------------------------------------------
        # GENERIC BOOKING INTENT (no symptom / doctor named yet)
        # -------------------------------------------------
        if is_booking_intent(message):
            await update.message.reply_text(t("ask_symptom_for_booking", lang))
            return

        # -------------------------------------------------
        # GENERAL AI
        # -------------------------------------------------
        ai_reply = ask_ai(message, lang)
        if not ai_reply:
            ai_reply = t("fallback_unclear", lang)
        await update.message.reply_text(ai_reply)

    except Exception:
        print("Jhon Error:")
        print(traceback.format_exc())
        await update.message.reply_text(t("generic_error", lang))


async def handle_message(update, context):
    if not update.message or not update.message.text:
        return

    message = update.message.text.strip()
    if not message:
        return

    # Admin commands go to the admin assistant (add/remove doctors,
    # time slots, patient search). Everyone else — including admins
    # testing a booking — uses the patient flow below.
    user_id = update.effective_user.id if update.effective_user else None
    if user_id and ADMIN_TELEGRAM_IDS and user_id in ADMIN_TELEGRAM_IDS:
        # The admin panel understands plain talk, so the gate is
        # deliberately broad: any admin message that LOOKS like a
        # command (add / remove / new department / report / list /
        # move a doctor ...) goes to the admin brain, which figures
        # out the exact intent. Plain booking messages ("dr rana",
        # "friday", a name, a phone number, "yes") contain none of
        # these words, so testing a booking still uses the patient
        # flow below.
        admin_text = message.lower().strip().strip("'\"")
        admin_keys = (
            "add", "remove", "delete", "notun", "banow", "banao",
            "create", "department", "dept", "report", "list",
            "sheet", "koto booking", "booking hoyeche", "patient",
            "rogi", "move", "boshow", "boshao", "slot",
            "daktar add", "patient search",
        )
        if any(k in admin_text for k in admin_keys):
            from admin_chat import admin_reply
            reply = admin_reply(message)
            if reply:
                await update.message.reply_text(reply)
            else:
                await update.message.reply_text(
                    "Sorry, I couldn't do that. Please try again."
                )
            return

    await process_patient_message(update, context, message)


async def handle_voice(update, context):
    """
    Handles Telegram voice notes: downloads the .ogg, transcribes it
    with Groq's Whisper model, shows the patient what was understood
    (so they can correct a mis-hearing), then runs the transcribed
    text through the exact same conversation logic as a typed
    message.
    """
    if not update.message or not update.message.voice:
        return

    voice_file = await context.bot.get_file(update.message.voice.file_id)
    ogg_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"_voice_{update.message.voice.file_id}.ogg",
    )

    try:
        await voice_file.download_to_drive(ogg_path)
        transcribed_text = transcribe_voice(ogg_path)
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    if not transcribed_text or not transcribed_text.strip():
        await update.message.reply_text(t("voice_transcription_failed", "en"))
        return

    await update.message.reply_text(f"🎙️ \u201c{transcribed_text.strip()}\u201d")
    await process_patient_message(update, context, transcribed_text.strip())


# ---------------------------------------------------------
# APPLICATION
# ---------------------------------------------------------
def main():
    removed = database.cleanup_past_appointments(datetime.now().date().strftime("%Y-%m-%d"))
    if removed:
        print(f"Cleaned up {removed} past appointment(s) from clinic_bookings.db")

    # Auto-sync: file <-> database, no restarts needed.
    # (Same as the website server — see sync.py.)
    from sync import converge_on_startup, start_file_watcher
    converge_on_startup()
    start_file_watcher()

    # Start background scheduler for daily report
    start_scheduler()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("sheet", sheet_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("reports", report_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    print("Jhon AI Medical Receptionist is running...")
    print(f"Model: {MODEL_NAME}")
    print(f"Booking Mode: {get_booking_mode()}")
    print("Bookings are stored persistently in clinic_bookings.db")
    app.run_polling()


if __name__ == "__main__":
    main()
