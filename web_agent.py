

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from clinic_data import (
    CLINIC_DOCTORS,
    find_doctor_by_name,
)
from doctor_router import find_doctors_for_problem
from booking_service import check_booking, create_booking
from ai_classifier import ai_detect_day_name
from clinic_data import (
    CLINIC_DOCTORS,
    find_doctor_by_name,
    find_doctors_by_department,
    get_all_doctors,
)
from messages import detect_lang

# ---------------------------------------------------------
# DAY PARSING
# ---------------------------------------------------------
DAY_ALIASES = {
    "sunday": "Sunday", "robibar": "Sunday", "robbar": "Sunday",
    "monday": "Monday", "sombar": "Monday", "shombar": "Monday",
    "tuesday": "Tuesday", "mongolbar": "Tuesday",
    "wednesday": "Wednesday", "budbar": "Wednesday", "budhbar": "Wednesday",
    "thursday": "Thursday", "brihospotibar": "Thursday",
    "brihoshpotibar": "Thursday",
    "friday": "Friday", "sukrobar": "Friday", "shukrobar": "Friday",
    "saturday": "Saturday", "shonibar": "Saturday", "sonibar": "Saturday",
}


def detect_day_name(text):
    text_lower = text.lower().strip()
    for keyword, day in DAY_ALIASES.items():
        if keyword in text_lower:
            return day
    return ai_detect_day_name(text)


def get_next_date_for_day(day_name, skip_weeks=0):
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
            values = [int(v) for v in match.groups()]
            if len(str(values[0])) == 4:
                year, month, day = values
            else:
                day, month, year = values
            return datetime(year, month, day).date()
        except ValueError:
            return None
    return None


def parse_booking_date(text):
    explicit = parse_explicit_date(text)
    if explicit:
        return explicit

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
    # Database first (Telegram/code changes show up here);
    # get_all_doctors() falls back to the built-in list when the
    # database has not been seeded yet.
    doctors = candidate_doctors or get_all_doctors() or CLINIC_DOCTORS

    for doctor in doctors:
        full_name = doctor["name"].lower()
        if full_name in text:
            return doctor
        name_no_title = full_name.replace("dr. ", "")
        if name_no_title in text:
            return doctor
        parts = name_no_title.split()
        if len(parts) >= 2:
            if parts[0] in text and parts[-1] in text:
                return doctor

    # Space-insensitive: "ayshakhan" (no space, missing letter)
    # should match "Ayesha Khan". Compare each word against the
    # full name with spaces/titles removed.
    text_nospace = re.sub(r"[^a-z0-9]", "", text)
    for doctor in doctors:
        name_nospace = re.sub(
            r"[^a-z0-9]", "", doctor["name"].lower().replace("dr.", "")
        )
        if name_nospace and name_nospace in text_nospace:
            return doctor
    words = re.findall(r"[a-zA-Z]+", text)
    # Single glued word vs the full name: take the BEST doctor,
    # not the first one over the line.
    _best = None
    _best_score = 0.0
    for doctor in doctors:
        name_nospace = re.sub(
            r"[^a-z0-9]", "", doctor["name"].lower().replace("dr.", "")
        )
        for word in words:
            word_nospace = re.sub(r"[^a-z0-9]", "", word.lower())
            if not word_nospace or len(word_nospace) < 4:
                continue
            score = SequenceMatcher(None, word_nospace, name_nospace).ratio()
            if score > _best_score:
                _best_score = score
                _best = doctor
    if _best is not None and _best_score >= 0.88:
        return _best

    # Combine the evidence across ALL words: one strong word
    # ("ahamed" -> "ahmed" 0.909) matches TWO doctors, so a single
    # global best is decided by list order. "sara ahamed" must
    # match Sarah Ahmed (sara->sarah 0.889 + ahamed->ahmed 0.909),
    # not Ahmed Hassan (only the 0.909).
    best_doctor = None
    best_total = 0.0
    for doctor in doctors:
        name = doctor["name"].lower().replace("dr. ", "")
        parts = name.split()
        total = 0.0
        max_single = 0.0
        for word in words:
            word_best = 0.0
            for name_part in parts:
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
# NAME + PHONE
# ---------------------------------------------------------
def parse_name_and_phone(text):
    phone_match = re.search(r"(\+?\d[\d\-\s]{8,14}\d)", text)
    phone = None
    name_part = text

    if phone_match:
        raw = phone_match.group(1)
        candidate = re.sub(r"[\s\-\(\)]", "", raw)
        if re.fullmatch(r"\+?\d{10,15}", candidate):
            phone = candidate
            name_part = text[:phone_match.start()] + text[phone_match.end():]

    name = re.sub(r"\s+", " ", name_part).strip(" ,.-\n\t")
    # A name never contains a long digit run: "mina ra 012369898"
    # is a name plus a mistyped/partial number, not a name. Strip
    # any 4+ digit run so it can't be stored as the patient name.
    name = re.sub(r"\d{4,}", "", name)
    name = re.sub(r"\s+", " ", name).strip(" ,.-\n\t")
    name = name if len(name) >= 2 else None
    return name, phone


# ---------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------
def format_doctors(doctors):
    return "\n".join([f"- {d['name']} — {d['specialization']}"
                      for d in doctors])


def doctor_schedule_text(doctor):
    return "\n".join([f"- {day}: {s['start']} - {s['end']}"
                      for day, s in doctor["schedule"].items()])

def format_doctor_days_with_status(doctor):
    """
    Doctor's upcoming available days with actual dates.

    Rule:
      - If TODAY is one of the doctor's chamber days AND its
        cutoff has already passed:
          -> SKIP today's occurrence entirely
          -> Show NEXT WEEK'S same day (with future date)
      - All other days shown with their next-occurrence date.
    """
    today_date = datetime.now().date()
    today_name = today_date.strftime("%A")
    now_time = datetime.now().time()

    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]

    lines = []
    today_idx = weekdays.index(today_name)

    # Start from tomorrow so today's weekday is naturally last
    ordered_days = weekdays[today_idx + 1:] + weekdays[:today_idx + 1]

    for day_name in ordered_days:
        if day_name not in doctor["schedule"]:
            continue

        hours = doctor["schedule"][day_name]

        # Compute next-occurrence date for this weekday
        target_idx = weekdays.index(day_name)
        days_ahead = (target_idx - today_date.weekday()) % 7
        actual_date = today_date + timedelta(days=days_ahead)

        # If this is TODAY's weekday:
        if actual_date == today_date:
            # Check if cutoff has passed
            cutoff_passed = False
            try:
                start_dt = datetime.strptime(hours["start"], "%I:%M %p")
                cutoff_dt = start_dt - timedelta(minutes=30)
                if now_time > cutoff_dt.time():
                    cutoff_passed = True
            except Exception:
                pass

            if cutoff_passed:
                # Skip today, show NEXT WEEK'S same day
                actual_date = actual_date + timedelta(days=7)

        lines.append(
            f"- {day_name} ({actual_date.strftime('%d %b')}): "
            f"{hours['start']} - {hours['end']}"
        )

    return "\n".join(lines)

# ---------------------------------------------------------
# INFO REQUEST DETECTION
# ---------------------------------------------------------
INFO_REQUEST_PHRASES = [
    "doctor der nam", "doctor er nam", "doctor nam",
    "doctors name", "doctor names", "list of doctor",
    "doctor list", "doctor der list", "doctor er list",
    "koto doctor", "koto daktar", "koy doctor", "koy daktar",
    "how many doctor", "how many doctors",
    "ki ki doctor", "kon kon doctor",
    "which doctors", "what doctors",
    "koto department", "koto dept", "koy department",
    "how many department", "how many departments",
    "ki ki department", "kon kon department",
    "list of department", "department list",
    "which departments", "what departments",
    "hospital somporke", "clinic somporke",
    "hospital er kotha", "clinic er kotha",
    "about the hospital", "about the clinic",
    "about hospital", "about clinic",
    "tell me about the hospital", "tell me about the clinic",
    "hospital info", "clinic info",
    "hospital ta ki", "clinic ta ki",
    "tomar hospital", "apnader hospital",
    "apnader clinic", "tomar clinic",
    "hospital information", "clinic information",
    "hospital sombondhe", "clinic sombondhe",
]

def is_info_request(text):
    """
    True if the message asks for clinic info, NOT a symptom.
    Also returns True for questions like "which department is
    Dr. X in", "what is Dr. X's schedule", etc.
    """
    message = text.lower().strip()

    # Check standard info phrases
    if any(phrase in message for phrase in INFO_REQUEST_PHRASES):
        return True

    # Doctor-related questions ("which department is Dr. X in",
    # "Dr. X kothay", "what is Dr. X's specialty", etc.)
    question_words = [
        "which department", "what department", "kon department",
        "ki department", "dr er department",
        "department of dr", "department from dr",
        "kothay", "kon dept",
        "specialization", "speciality", "specialty",
        "biography", "bio of",
    ]
    if any(q in message for q in question_words):
        return True

    return False


def _looks_like_general_question(text):
    """
    True if the message looks like a general question / chit-chat
    rather than a booking input (day, name, phone, yes/no).
    """
    msg = text.lower().strip()

    question_starters = [
        "can i", "can you", "could you", "will you", "would you",
        "what", "where", "when", "why", "how", "who", "which",
        "is there", "are there", "do you", "does ", "did ",
        "tell me", "explain", "ask you", "question",
        "ki ", "kothay", "kobe", "kivabe", "keno", "kar",
        "bolen", "bolo", "jante chai", "jante cai",
        "সালাম", "কেমন", "কি", "কোথায়", "কেন",
    ]

    if "?" in msg:
        return True

    return any(
        msg.startswith(s) or f" {s} " in f" {msg} "
        for s in question_starters
    )
def detect_global_intent(message):
    """
    Detects common global intents that should override the
    current state, regardless of where the conversation is.

    Returns:
      "cancel"  - user wants to cancel the current booking
      "change_doctor" - user wants to change the doctor
      "change_day" - user wants to change the day/date
      "restart"  - user wants to start over
      None       - no global intent detected
    """
    msg = message.lower().strip()

    # Cancel
    cancel_phrases = [
        "cancel", "cancel booking", "cancel appointment",
        "cancel koro", "cancel korbo", "cancel korte chai",
        "না করব", "বাতিল", "বাদ দাও",
    ]
    if any(p in msg for p in cancel_phrases):
        return "cancel"

    # Change doctor
    change_doctor_phrases = [
        "change doctor", "change the doctor", "change dr",
        "different doctor", "another doctor", "onno doctor",
        "doctor change", "doctor bodlao", "daktar change",
        "change to dr", "switch doctor", "switch to dr",
        "onno daktar",
    ]
    if any(p in msg for p in change_doctor_phrases):
        return "change_doctor"

    # Change day
    change_day_phrases = [
        "change day", "change the day", "change date",
        "different day", "another day", "onno din",
        "din change", "din bodlao", "date change",
        "porer din", "switch day",
    ]
    if any(p in msg for p in change_day_phrases):
        return "change_day"

    # Restart
    restart_phrases = [
        "start over", "restart", "reset", "shuru theke",
        "abotar shuru", "new booking", "notun booking",
        "/start", "/restart", "/reset",
    ]
    if any(p in msg for p in restart_phrases):
        return "restart"

    return None
def _direct_symptom_match(message):
    """
    Direct keyword-based symptom → department mapping.
    Reliable — no LLM involved.

    Returns a department name (string) or None.
    """
    msg = message.lower()

    # Map: keywords -> department
    rules = [
        # General Medicine
        (["fever", "fiver", "fivver", "jor", "joor", "jhor",
          "cold", "cough", "kashi", "sordi", "sardi",
          "weakness", "durbolota", "diabetes", "pressure",
          "body pain", "shorir betha", "body ache"],
         "General Medicine"),

        # Urology
        (["urine", "urinary", "prossab", "prosrab", "prashab",
          "porosab", "poroshab", "kidney", "kidni",
          "bladder", "prostate", "urology"],
         "Urology"),

        # Neurology
        (["headache", "matha betha", "matha batha", "matha byatha",
          "head pain", "head hurts", "migraine", "মাথা ব্যথা",
          "matha ghura", "dizziness", "vertigo"],
         "Neurology"),

        # Cardiology
        (["chest pain", "buke betha", "buk betha", "buk dhorfor",
          "heart", "হার্ট", "বুকে ব্যথা", "palpitation"],
         "Cardiology"),

        # Gastroenterology
        (["stomach", "pet betha", "pete betha", "gastric", "gas",
          "acidity", "digestion", "পেট ব্যথা", "hajma"],
         "Gastroenterology"),

        # ENT
        (["ear", "kan betha", "kane betha", "sore throat",
          "gola betha", "nose", "nak", "sinus", "কান ব্যথা"],
         "ENT"),

        # Ophthalmology
        (["eye", "chokh", "chokhe", "চোখ", "vision",
          "blurred", "cataract"],
         "Ophthalmology"),

        # Dermatology
        (["skin", "chamra", "chulkani", "rash", "acne",
          "allergy", "চামড়া", "চুলকানি"],
         "Dermatology"),

        # Orthopedics
        (["knee", "hatu", "back pain", "komor betha",
          "joint pain", "haddi", "bone", "fracture"],
         "Orthopedics"),

        # Dentistry
        (["tooth", "teeth", "dat betha", "dater betha",
          "gum", "দাঁত", "dental"],
         "Dentistry"),

        # Pediatrics
        (["child", "baby", "baccha", "bachcha", "শিশু", "বাচ্চা"],
         "Pediatrics"),

        # Gynecology
        (["pregnancy", "pregnant", "period", "মাসিক",
          "women health", "গর্ভবতী"],
         "Gynecology"),
    ]

    for keywords, department in rules:
        for kw in keywords:
            if kw in msg:
                return department

    return None
# ---------------------------------------------------------
# HELPER: resolve doctor + optional date
# ---------------------------------------------------------
def _resolve_doctor_and_date(doctor, booking_date, session):
    session["last_doctor"] = doctor["name"]  # for "tar schedule" follow-ups
    today_date = datetime.now().date()    # ← এই লাইন
    today = today_date
    ...

    if booking_date is None:
        session["state"] = "waiting_for_date"
        schedule_with_status = format_doctor_days_with_status(doctor)

        # Add a note if today's cutoff has already passed
        note = ""
        today_name = today_date.strftime("%A")
        if today_name in doctor["schedule"]:
            try:
                hours = doctor["schedule"][today_name]
                start_dt = datetime.strptime(hours["start"], "%I:%M %p")
                cutoff_dt = start_dt - timedelta(minutes=30)
                if datetime.now().time() > cutoff_dt.time():
                    note = _T("cutoff_note", _lang(session),
                             day=today_name,
                             time=cutoff_dt.strftime("%I:%M %p"))
            except Exception:
                pass

        return _T("resolve_no_date", _lang(session), doctor=doctor["name"],
                 note=note, schedule=schedule_with_status)

    day_name = booking_date.strftime("%A")

    if day_name not in doctor["schedule"]:
        days_status = format_doctor_days_with_status(doctor)
        session["state"] = "waiting_for_date"
        return _T("resolve_not_day", _lang(session), doctor=doctor["name"],
                 day=day_name, schedule=days_status)

    availability = check_booking(
        doctor["name"], booking_date, day_name, today,
        lang=_lang(session),
    )
    if availability.get("available"):
        session["booking_date"] = booking_date.isoformat()
        session["booking_day"] = day_name
        serial = availability.get("next_serial")
        # Name + phone already known (e.g. after a doctor switch
        # mid-confirmation)? Don't ask again — go straight to the
        # confirmation summary.
        if session.get("patient_name") and session.get("phone"):
            session["state"] = "confirming_booking"
            return _T("details_summary", _lang(session),
                     doctor=doctor["name"],
                     date=booking_date.isoformat(),
                     name=session["patient_name"],
                     phone=session["phone"])
        session["state"] = "waiting_for_details"
        return _T("resolve_available", _lang(session),
                 doctor=doctor["name"], day=day_name,
                 date=booking_date.strftime("%d %B %Y"), serial=serial)

    days_status = format_doctor_days_with_status(doctor)
    session["state"] = "waiting_for_date"
    return _T("resolve_unavailable", _lang(session),
             msg=availability.get("message", "Not available today."),
             schedule=days_status)


# ---------------------------------------------------------
# HELPER: answer an availability question from real data
# ---------------------------------------------------------
# "availab" covers available / availability; the other two are
# common misspellings patients actually type ("abailable").
AVAILABILITY_WORDS = ("availab", "abailable", "availble", "schedule",
                      "kokhon", "koyta", "chamber")


def _answer_availability(doctor, message, session):
    """
    Answer "is Dr. X available (today / on Friday)?" from the
    real schedule + booking data — never from the free-form LLM,
    which has no schedule data and guesses ("I don't have
    real-time schedules") or returns a cut-off fragment.

    - Explicit date (2026-11-06) or a named weekday -> that date.
    - "today" / "ajke" or no day at all -> today by default.
    Sets state to "waiting_for_date" with the doctor selected,
    so the patient can book straight away with a day name.
    """
    session["last_doctor"] = doctor["name"]  # for "tar schedule" follow-ups
    msg_lower = message.lower()

    booking_date = parse_explicit_date(message)
    if booking_date is None:
        day_name = None
        for keyword, day in DAY_ALIASES.items():
            if keyword in msg_lower:
                day_name = day
                break
        if day_name:
            booking_date = get_next_date_for_day(day_name)
        else:
            # "today", "ajke", or no day mentioned -> today
            booking_date = datetime.now().date()

    day_name = booking_date.strftime("%A")
    today_date = datetime.now().date()

    if day_name not in doctor["schedule"]:
        days_status = format_doctor_days_with_status(doctor)
        session["selected_doctor"] = doctor["name"]
        session["state"] = "waiting_for_date"
        return _T("avail_no", _lang(session), doctor=doctor["name"],
                 day=day_name, date=booking_date.strftime("%d %B %Y"),
                 schedule=days_status)

    availability = check_booking(
        doctor["name"], booking_date, day_name, today_date,
        lang=_lang(session),
    )
    if availability.get("available"):
        hours = doctor["schedule"][day_name]
        serial = availability.get("next_serial")
        session["selected_doctor"] = doctor["name"]
        session["booking_date"] = booking_date.isoformat()
        session["booking_day"] = day_name
        session["state"] = "waiting_for_date"
        return _T("avail_yes", _lang(session), doctor=doctor["name"],
                 day=day_name, date=booking_date.strftime("%d %B %Y"),
                 start=hours["start"], end=hours["end"], serial=serial)

    days_status = format_doctor_days_with_status(doctor)
    session["selected_doctor"] = doctor["name"]
    session["state"] = "waiting_for_date"
    return _T("resolve_unavailable", _lang(session),
             msg=availability.get("message", "Not available."),
             schedule=days_status)


# ---------------------------------------------------------
# CONVERSATION MEMORY + DECLINE HELPERS
# ---------------------------------------------------------
def _is_decline(message):
    """True when the patient is saying 'I don't want to book' —
    so the flow must stop gracefully instead of treating the
    words as a name, a day, or a yes/no answer."""
    text = message.lower().strip()
    if text in ("na", "nah", "naa", "no", "nope", "thak"):
        return True
    phrases = (
        "book korbo na", "booking korbo na", "book korte cai na",
        "booking korte cai na", "appointment nite cacchi na",
        "appointment nite chai na", "nite cacchi na", "nite chai na",
        "don't want to book", "do not want to book",
        "won't book", "will not book", "no need", "lagbe na",
        "chai na", "dorkar nai", "cancel booking", "booking cancel",
    )
    if any(p in text for p in phrases):
        return True
    # "thak" as its own word ("thak book korbo na") — not inside
    # a name like "Thakur".
    if re.search(r"\bthak\b", text):
        return True
    return False


def _is_yes(message):
    """A yes in any shape: "yes", "ok yes", "yes please", "haa"."""
    text = message.lower().strip()
    if text in ("yes", "yeah", "yep", "ok", "okay", "sure", "haa", "han",
                "ji", "hea", "heya"):
        return True
    # Word boundaries: "ok" must not match inside "book",
    # "han" must not match inside "khan".
    return bool(re.search(
        r"\b(yes|yeah|yep|ok|okay|sure|haa|han|ji|hea|heya)\b", text
    ))


def _is_no(message):
    """A no: whole message, or starting with no ("na, phone ta bhul")."""
    text = message.lower().strip()
    if text in ("na", "nah", "naa", "no", "nope"):
        return True
    return bool(re.match(r"^(na|nah|no|nope)\b", text))


def _is_book_command(message):
    """'book korun' / 'book koro' / 'confirm koro' — the patient
    is telling us to book, not asking a question."""
    text = message.lower().strip()
    phrases = (
        "book korun", "book koro", "book korte cai", "book korte chai",
        "booking koro", "booking korun", "book kore dao", "book kore din",
        "book it", "book now", "confirm koro", "confirm korun",
        "appointment dao", "appointment din", "appointment nite cai",
        "appointment nite chai", "korte cai", "korte chai",
    )
    return any(p in text for p in phrases)


def _remember_from_reply(session, reply):
    """Remember which doctor the bot's reply just talked about,
    so a follow-up like 'tar schedule bolo' (his schedule) can
    be answered from the real schedule. Only used for the
    free-form LLM replies (the booking flow already knows its
    doctor). Never raises."""
    try:
        doctor = find_doctor_from_message(reply)
        if doctor:
            session["last_doctor"] = doctor["name"]
    except Exception:
        pass


def _find_department_in_message(message):
    """A department name mentioned in the message, or None."""
    try:
        from clinic_data import get_departments
        departments = get_departments()
    except Exception:
        departments = []
    low = message.lower()
    for dept in departments:
        if dept.lower() in low:
            return dept
    return None


def _answer_department_schedule(department, session):
    """'ENT-te koyjon doctor, schedule bolo' — answer from the
    database: how many doctors, who they are, and each one's
    real schedule. Never the LLM (it has no schedule data)."""
    from clinic_data import find_doctors_by_department
    doctors = find_doctors_by_department(department)
    if not doctors:
        return _T("dept_none", _lang(session), dept=department)
    if len(doctors) == 1:
        session["last_doctor"] = doctors[0]["name"]
    lines = [_T("dept_header", _lang(session), dept=department,
               verb="is" if len(doctors) == 1 else "are",
               n=len(doctors), s="" if len(doctors) == 1 else "s"), ""]
    for doc in doctors:
        sched = doc.get("schedule", {}) or {}
        if sched:
            sched_text = ", ".join(
                f"{day} {h['start']}-{h['end']}"
                for day, h in sched.items()
            )
        else:
            sched_text = "no schedule set"
        lines.append(f"• {doc['name']} ({doc.get('specialization', doc['department'])})")
        lines.append(f"  Schedule: {sched_text}")
    lines.append("")
    if len(doctors) == 1:
        lines.append(_T("dept_q_single", _lang(session), doctor=doctors[0]["name"]))
    else:
        lines.append(_T("dept_q_multi", _lang(session)))
    return "\n".join(lines)


# ---------------------------------------------------------
# REPLY LANGUAGE — Bangla (Banglish) vs English
# ---------------------------------------------------------
# The patient writes Banglish -> reply in Bangla (Bengali
# script, common English words mixed in). The patient writes
# English -> reply in English. The language is detected per
# message (messages.detect_lang) and stored in the session,
# so a bare "yes" / "ok" does not flip it mid-conversation.
#
# English strings below are byte-identical to the ones this
# file used before, so English behaviour is unchanged.

_TEMPLATES = {
    "symptom_single": {
        "en": ("The {dept} department may be suitable for your problem.\n\n"
               "Available doctors:\n{doc}\n\n"
               "Would you like to book an appointment with {name}?"),
        "bn": ("আপনার সমস্যার জন্য {dept} বিভাগ উপযুক্ত হতে পারে।\n\n"
               "ডাক্তার:\n{doc}\n\n"
               "আপনি কি {name}-এর appointment নিতে চান?"),
    },
    "symptom_multi": {
        "en": ("The {dept} department may be suitable for your problem.\n\n"
               "Available doctors:\n{docs}\n\n"
               "Which doctor would you like to see?"),
        "bn": ("আপনার সমস্যার জন্য {dept} বিভাগ উপযুক্ত হতে পারে।\n\n"
               "ডাক্তার:\n{docs}\n\n"
               "আপনি কোন doctor-কে দেখতে চান?"),
    },
    "symptom_vague": {
        "en": ("I understand you're not feeling well. "
               "Could you tell me more specifically what's bothering you?\n\n"
               "For example:\n"
               "- Fever / জ্বর\n"
               "- Headache / মাথা ব্যথা\n"
               "- Stomach pain / পেট ব্যথা\n"
               "- Urine problem / প্রস্রাবের সমস্যা\n"),
        "bn": ("বুঝতে পারছি, আপনার শরীর ভালো লাগছে না। "
               "আরেকটু নির্দিষ্ট করে বলবেন কী সমস্যা হচ্ছে?\n\n"
               "যেমন:\n"
               "- জ্বর / Fever\n"
               "- মাথা ব্যথা / Headache\n"
               "- পেট ব্যথা / Stomach pain\n"
               "- প্রস্রাবের সমস্যা / Urine problem\n"),
    },
    "resolve_no_date": {
        "en": ("Sure. You selected {doctor}.{note}\n\n"
               "Available days:\n{schedule}\n\n"
               "Which day would you like to book?"),
        "bn": ("ঠিক আছে, আপনি {doctor} কে select করেছেন।{note}\n\n"
               "Available দিন:\n{schedule}\n\n"
               "আপনি কোন দিন booking করতে চান?"),
    },
    "cutoff_note": {
        "en": ("\n⚠️ Note: Today ({day})'s online booking "
               "has already closed (cutoff at "
               "{time}). "
               "You can book for the next available days shown below.\n"),
        "bn": ("\n⚠️ Note: আজ ({day}) online booking "
               "বন্ধ হয়ে গেছে (cutoff {time})। "
               "নিচের available দিনগুলোর জন্য booking করতে পারেন।\n"),
    },
    "resolve_not_day": {
        "en": ("Sorry, {doctor} is not available on {day}.\n\n"
               "Available days:\n{schedule}\n\n"
               "Which day would you like to book?"),
        "bn": ("দুঃখিত, {doctor} {day}-এ available নেই।\n\n"
               "Available দিন:\n{schedule}\n\n"
               "আপনি কোন দিন booking করতে চান?"),
    },
    "resolve_available": {
        "en": ("{doctor} is available on {day}, "
               "{date}.\n\n"
               "Current next serial: {serial}\n\n"
               "Please give your name and phone number together.\n"
               "Example: Karim Rahman, 01700000000"),
        "bn": ("{doctor} {day}, {date}-এ available আছেন।\n\n"
               "বর্তমান serial: {serial}\n\n"
               "আপনার নাম আর phone number একসাথে দিন।\n"
               "উদাহরণ: Karim Rahman, 01700000000"),
    },
    "resolve_unavailable": {
        "en": ("{msg}\n\n"
               "Available days:\n{schedule}\n\n"
               "Which day would you like to book?"),
        "bn": ("{msg}\n\n"
               "Available দিন:\n{schedule}\n\n"
               "আপনি কোন দিন booking করতে চান?"),
    },
    "details_summary": {
        "en": ("Booking details:\n\n"
               "Doctor: {doctor}\n"
               "Date: {date}\n"
               "Patient: {name}\n"
               "Phone: {phone}\n\n"
               "Is everything correct? Please say Yes or No."),
        "bn": ("Booking-এর details:\n\n"
               "Doctor: {doctor}\n"
               "Date: {date}\n"
               "Patient: {name}\n"
               "Phone: {phone}\n\n"
               "সব ঠিক আছে তো? Yes বা No বলুন।"),
    },
    "avail_yes": {
        "en": ("Yes, {doctor} is available on {day} "
               "({date}), "
               "{start} - {end}.\n\n"
               "Current next serial: {serial}\n\n"
               "Would you like to book for {day}?"),
        "bn": ("হ্যাঁ, {doctor} {day} "
               "({date}), "
               "{start} - {end} available আছেন।\n\n"
               "বর্তমান serial: {serial}\n\n"
               "আপনি কি {day}-এর জন্য booking করতে চান?"),
    },
    "avail_no": {
        "en": ("No, {doctor} is not available on {day} "
               "({date}).\n\n"
               "Available days:\n{schedule}\n\n"
               "Which day would you like to book?"),
        "bn": ("না, {doctor} {day} "
               "({date})-এ available নেই।\n\n"
               "Available দিন:\n{schedule}\n\n"
               "আপনি কোন দিন booking করতে চান?"),
    },
    "avail_which_doctor": {
        "en": ("Sure — which doctor would you like to check? "
               "Please tell me the doctor's name."),
        "bn": ("ঠিক আছে — কোন doctor-কে check করতে চান? "
               "Doctor-এর নাম বলুন।"),
    },
    "dept_none": {
        "en": "Sorry, there is no doctor in the {dept} department right now.",
        "bn": "দুঃখিত, {dept} বিভাগে এখন কোনো doctor নেই।",
    },
    "dept_header": {
        "en": "In the {dept} department there {verb} {n} doctor{s}:",
        "bn": "{dept} বিভাগে {n} জন doctor আছেন:",
    },
    "dept_q_single": {
        "en": "Would you like to book an appointment with {doctor}?",
        "bn": "আপনি কি {doctor}-এর appointment নিতে চান?",
    },
    "dept_q_multi": {
        "en": "Which doctor would you like to book?",
        "bn": "কোন doctor-এর appointment নিতে চান?",
    },
    "decline": {
        "en": ("No problem! Thank you for talking with us. If you "
               "need an appointment in the future, just message us "
               "anytime. Take care!"),
        "bn": ("কোনো সমস্যা নেই! আমাদের সাথে কথা বলার জন্য ধন্যবাদ। "
               "ভবিষ্যতে appointment লাগলে যেকোনো সময় message করবেন। "
               "ভালো থাকবেন!"),
    },
    "which_day": {
        "en": "Sure! Which day would you like to book?",
        "bn": "ঠিক আছে! আপনি কোন দিন booking করতে চান?",
    },
    "which_day_instead": {
        "en": ("No problem! Which day would you like to book "
               "instead?"),
        "bn": "কোনো সমস্যা নেই! এর বদলে কোন দিন booking করতে চান?",
    },
    "didnt_get_day": {
        "en": ("Sorry, I didn't quite get that. Which day would "
               "you like to book? (For example: Friday, Saturday, "
               "or Sunday)"),
        "bn": ("দুঃখিত, ঠিক বুঝতে পারিনি। আপনি কোন দিন booking করতে "
               "চান? (যেমন: Friday, Saturday, বা Sunday)"),
    },
    "got_phone": {
        "en": "Got the phone number. Please also give your name.",
        "bn": "Phone number পেয়েছি। আপনার নামটাও দিন।",
    },
    "got_name": {
        "en": "Got your name. Please also give your phone number.",
        "bn": "নাম পেয়েছি। আপনার phone number-টাও দিন।",
    },
    "ask_name_phone": {
        "en": ("Please give me your name and phone number together.\n"
               "Example: Karim Rahman, 01700000000"),
        "bn": ("আপনার নাম আর phone number একসাথে দিন।\n"
               "উদাহরণ: Karim Rahman, 01700000000"),
    },
    "confirmed": {
        "en": ("Appointment confirmed! ✅\n\n"
               "Doctor: {doctor}\n"
               "Date: {date}\n"
               "Serial: {serial}\n"
               "Time: {time}\n\n"
               "Do you need an email confirmation?\n"
               "If yes, please share your email address. "
               "If not, just say 'no'."),
        "bn": ("Appointment confirm হয়েছে! ✅\n\n"
               "Doctor: {doctor}\n"
               "Date: {date}\n"
               "Serial: {serial}\n"
               "Time: {time}\n\n"
               "Email-এ confirmation লাগবে কি?\n"
               "লাগলে আপনার email address দিন। না লাগলে 'no' বলুন।"),
    },
    "cancelled": {
        "en": "Booking cancelled. How else can I help you?",
        "bn": "Booking cancel করা হয়েছে। আর কীভাবে সাহায্য করতে পারি?",
    },
    "email_sent": {
        "en": "Confirmation email sent to {email}. ✅",
        "bn": "{email}-এ confirmation email পাঠানো হয়েছে। ✅",
    },
    "email_failed": {
        "en": ("Sorry, email could not be sent. "
               "Booking is still confirmed."),
        "bn": ("দুঃখিত, email পাঠানো যায়নি। "
               "Booking confirm আছে।"),
    },
    "email_unavailable": {
        "en": ("Email service is not available yet. "
               "Booking is still confirmed."),
        "bn": ("Email service এখনো available নেই। "
               "Booking confirm আছে।"),
    },
    "email_prompt": {
        "en": "Please share a valid email, or say 'no' to skip.",
        "bn": "সঠিক email address দিন, বা skip করতে 'no' বলুন।",
    },
    "email_skip": {
        "en": "Okay. Thank you for choosing HealthCare Hospital!",
        "bn": "ঠিক আছে। HealthCare Hospital বেছে নেওয়ার জন্য ধন্যবাদ!",
    },
    "yes_no_prompt": {
        "en": "Please type Yes or No.",
        "bn": "Yes বা No লিখুন।",
    },
    "greeting": {
        "en": "Hello! How can I help you today?",
        "bn": "আসসালামু আলাইকুম! আজ কীভাবে সাহায্য করতে পারি?",
    },
    "start_over": {
        "en": "Okay, let's start over. How can I help you today?",
        "bn": "ঠিক আছে, আবার শুরু করি। আজ কীভাবে সাহায্য করতে পারি?",
    },
    "cancel_flow": {
        "en": "Okay, booking cancelled. How else can I help you?",
        "bn": "ঠিক আছে, booking cancel করা হয়েছে। আর কীভাবে সাহায্য করতে পারি?",
    },
    "cancel_booked": {
        "en": ("Your appointment with {doctor} on "
               "{date} (Serial {serial}) "
               "has been cancelled. If you want a new date, just "
               "tell me the doctor's name."),
        "bn": ("{doctor}-এর সাথে {date}-এর appointment (Serial {serial}) "
               "cancel করা হয়েছে। নতুন date লাগলে doctor-এর নাম বলুন।"),
    },
    "cancel_by_phone": {
        "en": ("Cancelled: {doctor} on {date} "
               "(Serial {serial}, "
               "{patient}). If you want a new date, "
               "just tell me the doctor's name."),
        "bn": ("Cancel করা হয়েছে: {date}-এ {doctor} (Serial {serial}, "
               "{patient})। নতুন date লাগলে doctor-এর নাম বলুন।"),
    },
    "cancel_not_found": {
        "en": ("I couldn't find a booking for that phone number. "
               "Please check the number and try again."),
        "bn": ("এই phone number-এ কোনো booking পাইনি। "
               "Number-টা check করে আবার চেষ্টা করুন।"),
    },
    "old_booking_cancelled": {
        "en": "Your old booking with {doctor} on {date} is cancelled. ",
        "bn": "{doctor}-এর সাথে {date}-এর পুরনো booking cancel করা হয়েছে। ",
    },
    "change_doctor_ask": {
        "en": ("Sure! Which doctor would you like to see instead? "
               "You can tell me the doctor's name, or describe your "
               "symptom and I'll suggest one."),
        "bn": ("ঠিক আছে! এর বদলে কোন doctor-কে দেখতে চান? "
               "Doctor-এর নাম বলুন, বা symptom বলুন — আমি suggest করব।"),
    },
    "change_day_no_doctor": {
        "en": "Sure! Which doctor would you like to see first?",
        "bn": "ঠিক আছে! আগে কোন doctor-কে দেখতে চান বলুন।",
    },
    "change_day_ask": {
        "en": "Okay, which day would you like to book?",
        "bn": "ঠিক আছে, কোন দিন booking করতে চান?",
    },
    "book_new_prompt": {
        "en": ("Sure, I can help you book an appointment. "
               "Could you briefly tell me your main health problem? "
               "That way I can point you to the right doctor."),
        "bn": ("ঠিক আছে, আমি appointment book করতে সাহায্য করতে পারি। "
               "আপনার মূল সমস্যাটা সংক্ষেপে বলবেন? তাহলে সঠিক "
               "doctor-এর কাছে পাঠাতে পারব।"),
    },
    "idle_fallback": {
        "en": ("I can help you book an appointment. "
               "Please tell me your symptoms — for example "
               "'amar jor hoise' or 'matha betha'."),
        "bn": ("আমি appointment book করতে সাহায্য করতে পারি। "
               "আপনার symptom বলুন — যেমন 'amar jor hoise' বা "
               "'matha betha'।"),
    },
    "info_fallback": {
        "en": "Sure, what would you like to know?",
        "bn": "ঠিক আছে, কী জানতে চান?",
    },
    "emergency": {
        "en": ("This sounds like an emergency. Please call 999 or go "
               "to the nearest hospital emergency department RIGHT NOW. "
               "Do not wait for an appointment. Once you are safe, "
               "message us again and we will help you book."),
        "bn": ("এটা emergency মনে হচ্ছে। এখনই 999-এ কল করুন বা কাছের "
               "hospital-এর emergency বিভাগে যান। Appointment-এর জন্য "
               "অপেক্ষা করবেন না। নিরাপদ হলে আবার message করুন, আমরা "
               "booking-এ সাহায্য করব।"),
    },
    "which_doctor_again": {
        "en": "Sure! Which doctor would you like to see?",
        "bn": "ঠিক আছে! কোন doctor-কে দেখতে চান?",
    },
}


def _T(key, lang, **kwargs):
    """Reply template in the patient's language ("en"/"bn")."""
    entry = _TEMPLATES.get(key, {})
    template = entry.get(lang) or entry.get("en", "")
    return template.format(**kwargs) if kwargs else template


def _lang(session):
    return session.get("lang", "en")


def _resolve_suggested_doctor(message, session):
    """'yes' / 'ok' / 'hea' / 'korte cai' after a single-doctor
    suggestion: go straight into the booking flow for THAT
    doctor — the patient must not repeat the name.

    Single-day doctor -> resolve with that day's next date (the
    Day-4 single-day logic: straight to name/phone). Multi-day
    doctor -> resolve with no date (shows the days, asks which).
    A day named in the same message ("yes, Friday") wins.
    Returns the reply string, or None when there is no single
    doctor suggestion to act on.
    """
    suggested = session.get("suggested_doctor")
    if not suggested:
        return None
    doctor = find_doctor_by_name(suggested)
    if not doctor:
        return None
    session["selected_doctor"] = doctor["name"]
    booking_date = parse_booking_date(message)
    if booking_date is None and len(doctor.get("schedule", {})) == 1:
        single_day = next(iter(doctor["schedule"]))
        booking_date = get_next_date_for_day(single_day)
    return _resolve_doctor_and_date(doctor, booking_date, session)


# ---------------------------------------------------------
# MAIN PROCESSOR

def process_message(message, session):
    """
    LLM-first conversation handler.

    Every message goes through the intent classifier first,
    which understands natural language without keyword lists.
    """
    from intent_classifier import classify_intent

    state = session.get("state", "idle")
    reply = None

    # Language follows the patient: Banglish -> Bangla replies,
    # English -> English replies. detect_lang returns None for
    # bare confirmations ("yes"/"ok"), so the language set by
    # the last real message stays.
    _detected = detect_lang(message)
    if _detected:
        session["lang"] = _detected
    lang = _lang(session)

    # ---- 0. Emergency — never route through the normal flow ----
    _emergency_words = (
        "emergency", "accident", "durghotona", "unconscious", "oggan",
        "can't breathe", "cant breathe", "shash nite",
        "bleeding", "rokto ber", "heart attack", "stroke",
    )
    if any(w in message.lower() for w in _emergency_words):
        session["state"] = "idle"
        return _T("emergency", lang)

    # ---- 1. Classify intent ----
    intent = classify_intent(message, state=state)

    # ---- 2. Handle global intents (any state) ----
    if intent == "CANCEL":
        import database
        # (a) A booking confirmed in this chat? Cancel it for real.
        last = session.get("last_appointment")
        if last and last.get("phone"):
            if database.cancel_appointment_by_details(
                last["phone"], last["doctor_name"], last["date"]
            ):
                session.clear()
                session["state"] = "idle"
                return _T("cancel_booked", lang,
                         doctor=last["doctor_name"], date=last["date"],
                         serial=last["serial_number"])
        # (b) Phone number in the message? Cancel their soonest booking.
        _n, _phone = parse_name_and_phone(message)
        if _phone:
            upcoming = database.get_upcoming_appointments_for_phone(
                _phone, datetime.now().date().strftime("%Y-%m-%d")
            )
            if upcoming:
                appt = upcoming[0]
                database.cancel_appointment_by_details(
                    _phone, appt["doctor_name"], appt["date"]
                )
                session.clear()
                session["state"] = "idle"
                return _T("cancel_by_phone", lang,
                         doctor=appt["doctor_name"], date=appt["date"],
                         serial=appt["serial_number"],
                         patient=appt["patient_name"])
            session.clear()
            session["state"] = "idle"
            return _T("cancel_not_found", lang)
        # (c) Otherwise just stop the current booking flow.
        session.clear()
        session["state"] = "idle"
        return _T("cancel_flow", lang)

    if intent == "RESTART":
        session.clear()
        session["state"] = "idle"
        return _T("start_over", lang)

    if intent == "CHANGE_DOCTOR":
        # "sorry, I actually want Dr. Sarah Ahmed" — the new doctor
        # is already named: switch to them and KEEP the name/phone
        # the patient already gave, instead of starting over.
        _new = find_doctor_from_message(message)
        if _new and _new["name"] != session.get("selected_doctor"):
            session["selected_doctor"] = _new["name"]
            session.pop("booking_date", None)
            session.pop("booking_day", None)
            session["state"] = "waiting_for_date"
            return _resolve_doctor_and_date(
                _new, parse_booking_date(message), session
            )
        session.pop("selected_doctor", None)
        session.pop("patient_name", None)
        session.pop("phone", None)
        session["state"] = "idle"
        return _T("change_doctor_ask", lang)

    if intent == "CHANGE_DAY":
        # Reschedule: a confirmed booking exists? Cancel it first so
        # the patient is not double-booked, then ask for the new day.
        _last = session.get("last_appointment")
        _note = ""
        if _last and _last.get("phone"):
            import database
            if database.cancel_appointment_by_details(
                _last["phone"], _last["doctor_name"], _last["date"]
            ):
                _note = _T("old_booking_cancelled", lang,
                         doctor=_last["doctor_name"], date=_last["date"])
                session.pop("last_appointment", None)
        session.pop("booking_date", None)
        session.pop("booking_day", None)
        session.pop("patient_name", None)
        session.pop("phone", None)
        if session.get("selected_doctor"):
            session["state"] = "waiting_for_date"
            return _note + _T("change_day_ask", lang)
        else:
            session["state"] = "idle"
            return _T("change_day_no_doctor", lang)

    # ---- 3c. Follow-ups about the doctor just discussed ----
    # "tar schedule bolo" / "tar appointment nite cai" — "tar"
    # means the doctor from the last reply. The free-form LLM has
    # no schedule data, so these must NEVER reach general_reply
    # (it guesses "we don't have the schedule").
    # Also: department questions with a count/schedule
    # ("ENT-te koyjon doctor, schedule bolo") are answered from
    # the database here, not by the LLM.
    if state in ("idle", "waiting_for_doctor", "waiting_for_date"):
        _low = message.lower()
        _last_name = session.get("last_doctor")
        if _last_name:
            _doc = find_doctor_by_name(_last_name)
            if _doc and re.search(r"\b(tar|onar|unake|uni|tader)\b", _low):
                if any(w in _low for w in ("schedule", "kokhon", "koyta",
                                           "chamber", "kobe", "time",
                                           "available", "availab")):
                    return _answer_availability(_doc, message, session)
                if any(w in _low for w in ("appointment", "book", "booking",
                                           "serial", "nite cai", "nite chai",
                                           "nite cacchi")):
                    session["selected_doctor"] = _doc["name"]
                    _bd = parse_booking_date(message)
                    return _resolve_doctor_and_date(_doc, _bd, session)
        _dept = _find_department_in_message(message)
        if _dept and any(w in _low for w in ("koyjon", "kotogulo", "koyta",
                                             "schedule", "ke ke", "kara",
                                             "list", "ache")):
            return _answer_department_schedule(_dept, session)

    # ---- 3. Symptom — always high priority ----
    # Direct keyword-based symptom routing (reliable).
    # The LLM is only used if the keyword list can't classify.
    direct_dept = _direct_symptom_match(message)

    if direct_dept:
        doctors = find_doctors_by_department(direct_dept)
        if doctors:
            session["state"] = "waiting_for_doctor"
            session["candidate_doctors"] = doctors
            session["last_doctor"] = doctors[0]["name"]
            doc_text = (
                f"{doctors[0]['name']} — {doctors[0]['specialization']}"
                if len(doctors) == 1 else format_doctors(doctors)
            )
            if len(doctors) == 1:
                # Single doctor: ask straight away whether they
                # want an appointment with them. A "yes" then
                # goes into the booking flow (see
                # _resolve_suggested_doctor).
                session["suggested_doctor"] = doctors[0]["name"]
                return _T("symptom_single", lang, dept=direct_dept,
                         doc=doc_text, name=doctors[0]["name"])
            session.pop("suggested_doctor", None)
            return _T("symptom_multi", lang, dept=direct_dept, docs=doc_text)

    # Fallback to LLM-based detection
    if intent == "SYMPTOM":
        routing = find_doctors_for_problem(message)

        if routing.get("found"):
            doctors = routing["doctors"]
            session["state"] = "waiting_for_doctor"
            session["candidate_doctors"] = doctors
            session["last_doctor"] = doctors[0]["name"]
            dept = routing["department"]
            doc_text = (
                f"{doctors[0]['name']} — {doctors[0]['specialization']}"
                if len(doctors) == 1 else format_doctors(doctors)
            )
            if len(doctors) == 1:
                session["suggested_doctor"] = doctors[0]["name"]
                return _T("symptom_single", lang, dept=dept,
                         doc=doc_text, name=doctors[0]["name"])
            session.pop("suggested_doctor", None)
            return _T("symptom_multi", lang, dept=dept, docs=doc_text)

        # Vague — ask for details
        session["state"] = "idle"
        return _T("symptom_vague", lang)

    # ---- 3b. Availability questions — real schedule, not the LLM ----
    # "is Dr. X available today?" must be answered from the actual
    # schedule + booking data. The free-form LLM (general_reply) has
    # no schedule data, so it guesses ("I don't have real-time
    # schedules") or returns a cut-off fragment ("হ্যাঁ, Dr.").
    # Only in idle state — mid-booking states already handle a
    # doctor name through their own flow.
    if state == "idle":
        msg_lower = message.lower()
        if any(w in msg_lower for w in AVAILABILITY_WORDS):
            doctor = find_doctor_from_message(message)
            if doctor:
                return _answer_availability(doctor, message, session)
            return _T("avail_which_doctor", lang)

    # ---- 4. Info requests ----
    if intent in ("DOCTOR_INFO", "CLINIC_INFO"):
        # A bare doctor name ("DR AYSHA KAHN") while we are waiting
        # for the patient to pick a doctor is a SELECTION, not an
        # info request — the state-driven flow below must handle it
        # (otherwise the patient gets a doctor bio instead of the
        # booking flow, depending on the LLM's mood).
        _named = find_doctor_from_message(message)
        _qwords = ("?", "who", "about", "ke ", "kemon", "information",
                   "info", "details", "janbo", "janate", "bolben",
                   "ki koren", "specialist", "kothay", "kokhon")
        _low4 = message.lower()
        if _named and state == "waiting_for_doctor":
            pass  # fall through to section 6 (booking flow)
        elif _named and not any(w in _low4 for w in _qwords):
            pass  # bare name — fall through to the booking flow
        else:
            try:
                from ai_chat import general_reply
                ai_response = general_reply(message, lang)
                if ai_response:
                    _remember_from_reply(session, ai_response)
                    return ai_response
                return _T("info_fallback", lang)
            except Exception as e:
                print(f"Info AI error: {e}")
                return _T("info_fallback", lang)

    # ---- 5. Greeting ----
    if intent == "GREETING":
        try:
            from ai_chat import general_reply
            ai_response = general_reply(message, lang)
            return ai_response or _T("greeting", lang)
        except Exception:
            return _T("greeting", lang)

    # ---- 6. State-driven flow ----
    if state == "waiting_for_doctor":
        candidates = session.get("candidate_doctors", [])
        doctor = find_doctor_from_message(message, candidates)
        if not doctor:
            doctor = find_doctor_from_message(message)
        if doctor:
            session["selected_doctor"] = doctor["name"]
            booking_date = parse_booking_date(message)
            return _resolve_doctor_and_date(doctor, booking_date, session)

        # No doctor named — but a "yes" / "ok" / "hea" /
        # "korte cai" after a single-doctor suggestion means
        # "book with them": enter the booking flow for the
        # suggested doctor (no need to repeat the name). After
        # a multi-doctor suggestion a bare "yes" is ambiguous —
        # just ask which doctor again.
        if _is_yes(message) or intent == "CONFIRM_YES" \
                or _is_book_command(message):
            _reply = _resolve_suggested_doctor(message, session)
            if _reply is not None:
                return _reply
            return _T("which_doctor_again", lang)

    if state == "waiting_for_date":
        # "Yes" to the day just offered (e.g. after an availability
        # answer: "Would you like to book for Friday?").
        # Confirm the date + doctor ONLY when the doctor has
        # exactly ONE chamber day a week — then there is no
        # ambiguity about which day "yes" means. Multi-day doctors
        # fall through to the fallback below: the patient must say
        # the day. Day-4: the date is the STORED one when there is
        # one (availability answer); otherwise the single chamber
        # day's next occurrence (bare-doctor branch, e.g. "dr rana",
        # stores no date). Only when no new day is mentioned in
        # the same message — "yes, Saturday" must fall through to
        # the PROVIDE_DAY path below.
        normalized = message.strip().lower()
        yes_words = ("yes", "yeah", "yep", "ok", "okay", "sure",
                     "haa", "han", "ji")
        no_words = ("no", "nah", "na", "nope")

        # A different doctor named mid-flow ("sorry, I want
        # Dr. Sarah Ahmed") switches to THAT doctor — checked
        # BEFORE the "yes" branch, or a correction that contains
        # a yes-word ("...korte cai") would book the OLD doctor.
        # Declines are left for the decline check below.
        if not _is_decline(message):
            _switch = find_doctor_from_message(message)
            if _switch and _switch["name"] != session.get("selected_doctor"):
                session["selected_doctor"] = _switch["name"]
                _bd = parse_booking_date(message)
                return _resolve_doctor_and_date(_switch, _bd, session)

        if (_is_yes(message) or intent == "CONFIRM_YES"
                or _is_book_command(message)) \
                and parse_booking_date(message) is None:
            stored_doctor = session.get("selected_doctor")
            if stored_doctor:
                doctor = find_doctor_by_name(stored_doctor)
                if doctor:
                    # The day just offered ("Would you like to book
                    # for Friday?") is unambiguous for ANY doctor —
                    # "yes" means that day. Without a stored date,
                    # only a single-day doctor is unambiguous
                    # (Day-4: bare-doctor branch, "dr rana", stores
                    # no date — use the single chamber day's next
                    # occurrence). _resolve_doctor_and_date
                    # re-checks availability and cutoff, so a passed
                    # cutoff still returns the cutoff message
                    # instead of booking.
                    booking_date = None
                    stored_date = session.get("booking_date")
                    if stored_date:
                        try:
                            booking_date = datetime.fromisoformat(
                                stored_date
                            ).date()
                        except ValueError:
                            booking_date = None
                    if booking_date is None and len(doctor.get("schedule", {})) == 1:
                        single_day = next(iter(doctor["schedule"]))
                        booking_date = get_next_date_for_day(single_day)
                    if booking_date is not None:
                        return _resolve_doctor_and_date(
                            doctor, booking_date, session
                        )

        # Explicit "I don't want to book" — thank them and stop
        # pushing the booking (real receptionist behaviour).
        # A bare "no" to the offered day is different: that falls
        # through to the "another day?" rule below.
        if _is_decline(message):
            session["state"] = "idle"
            session.pop("selected_doctor", None)
            session.pop("booking_date", None)
            return _T("decline", lang)

        # "No" to the offered day — stay in waiting_for_date and
        # ask for another day. Session is NOT reset.
        if (intent == "CONFIRM_NO" or normalized in no_words) \
                and parse_booking_date(message) is None:
            return _T("which_day_instead", lang)

        # User is answering with a day
        if intent == "PROVIDE_DAY" or parse_booking_date(message):
            current_doctor_name = session.get("selected_doctor")
            if current_doctor_name:
                doctor = find_doctor_by_name(current_doctor_name)
                if doctor:
                    booking_date = parse_booking_date(message)
                    if booking_date:
                        return _resolve_doctor_and_date(
                            doctor, booking_date, session
                        )

        # A bare "yes" / "book korun" for a multi-day doctor (no day
        # named yet) — just ask for the day again, without
        # "I didn't get that".
        if (_is_yes(message) or intent == "CONFIRM_YES"
                or _is_book_command(message)) \
                and parse_booking_date(message) is None:
            return _T("which_day", lang)

        # Couldn't understand — ask for a day, politely
        return _T("didnt_get_day", lang)

    if state == "waiting_for_details":
        # "thak book korbo na" / "na ami appointment nite cacchi na"
        # is a DECLINE, not a name — check before parsing, or the
        # words get stored as the patient's name.
        if _is_decline(message):
            session["state"] = "idle"
            for _k in ("selected_doctor", "booking_date", "booking_day",
                       "patient_name", "phone"):
                session.pop(_k, None)
            return _T("decline", lang)
        new_name, new_phone = parse_name_and_phone(message)
        if new_name and not session.get("patient_name"):
            session["patient_name"] = new_name
        if new_phone and not session.get("phone"):
            session["phone"] = new_phone

        have_name = bool(session.get("patient_name"))
        have_phone = bool(session.get("phone"))

        if have_name and have_phone:
            session["state"] = "confirming_booking"
            return _T("details_summary", lang,
                     doctor=session["selected_doctor"],
                     date=session["booking_date"],
                     name=session["patient_name"],
                     phone=session["phone"])
        elif have_phone and not have_name:
            return _T("got_phone", lang)
        elif have_name and not have_phone:
            return _T("got_name", lang)
        else:
            return _T("ask_name_phone", lang)

    if state == "confirming_booking":
        # A different doctor named mid-confirmation ("sorry, I
        # actually want Dr. Sarah Ahmed") switches to THAT doctor
        # — checked BEFORE the "yes" branch, or a correction
        # containing a yes-word would confirm the OLD doctor.
        # Name/phone already given are kept. Declines are left
        # for the decline check below.
        if not _is_decline(message):
            _new = find_doctor_from_message(message)
            if _new and _new["name"] != session.get("selected_doctor"):
                session["selected_doctor"] = _new["name"]
                session.pop("booking_date", None)
                session.pop("booking_day", None)
                session["state"] = "waiting_for_date"
                return _resolve_doctor_and_date(
                    _new, parse_booking_date(message), session
                )

        if (intent == "CONFIRM_YES" or _is_yes(message)
                or _is_book_command(message)):
            booking_date = datetime.fromisoformat(
                session["booking_date"]
            ).date()
            result = create_booking(
                doctor_name=session["selected_doctor"],
                date=booking_date,
                day=session["booking_day"],
                patient_name=session["patient_name"],
                phone=session["phone"],
                today=datetime.now().date(),
                lang=lang,
            )
            if result.get("success"):
                appt = result["appointment"]
                session["last_appointment"] = appt
                session["state"] = "asking_email"

                try:
                    from notifications import notify_new_booking
                    import threading
                    threading.Thread(
                        target=notify_new_booking,
                        args=(appt,),
                        daemon=True,
                    ).start()
                except Exception as e:
                    print(f"Notification error: {e}")

                return _T("confirmed", lang, doctor=appt["doctor_name"],
                         date=appt["date"], serial=appt["serial_number"],
                         time=appt["estimated_time"])
            else:
                session["state"] = "idle"
                return result.get("message", "Booking failed.")

        if intent == "CONFIRM_NO" or _is_no(message):
            session["state"] = "idle"
            return _T("cancelled", lang)

        if _is_decline(message):
            session["state"] = "idle"
            return _T("decline", lang)

        # Not a yes/no — treat as general
        try:
            from ai_chat import general_reply
            return general_reply(message, lang) or _T("yes_no_prompt", lang)
        except Exception:
            return _T("yes_no_prompt", lang)

    if state == "asking_email":
        normalized = message.lower().strip()
        if normalized in ("no", "না", "skip", "nope"):
            session["state"] = "idle"
            return _T("email_skip", lang)
        email_match = re.search(
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            message,
        )
        if email_match:
            try:
                from email_service import send_patient_confirmation
                email = email_match.group(0)
                success = send_patient_confirmation(
                    email, session.get("last_appointment")
                )
                session["state"] = "idle"
                if success:
                    return _T("email_sent", lang, email=email)
                return _T("email_failed", lang)
            except Exception as e:
                print(f"Email service error: {e}")
                session["state"] = "idle"
                return _T("email_unavailable", lang)
        return _T("email_prompt", lang)

    # ---- 7. Idle state — figure out what they want ----
    # Doctor search?
    doctor = find_doctor_from_message(message)
    if doctor:
        session["selected_doctor"] = doctor["name"]
        booking_date = parse_booking_date(message)
        return _resolve_doctor_and_date(doctor, booking_date, session)

    # "yes" to a single-doctor suggestion, in case the state was
    # reset to idle since the suggestion was made.
    if (_is_yes(message) or intent == "CONFIRM_YES"
            or _is_book_command(message)):
        _reply = _resolve_suggested_doctor(message, session)
        if _reply is not None:
            return _reply

    # Booking intent?
    if intent == "BOOK_NEW":
        return _T("book_new_prompt", lang)

    # Anything else — general AI
    try:
        from ai_chat import general_reply
        ai_response = general_reply(message, lang)
        if ai_response:
            _remember_from_reply(session, ai_response)
            return ai_response
    except Exception as e:
        print(f"General AI error: {e}")

    return _T("idle_fallback", lang)