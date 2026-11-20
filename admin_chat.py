"""
Admin-specific conversation logic for Telegram.

Features:
  1. Natural-language queries about clinic data
  2. Add / remove doctors via natural language
  3. Patient search (by name or phone)
  4. Weekly and monthly stats
"""

import os
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing")

client = Groq(api_key=GROQ_API_KEY)
MODEL_NAME = "openai/gpt-oss-20b"


# =========================================================
# CLINIC CONTEXT BUILDER
# =========================================================
def _build_admin_context():
    """Builds a rich context string for the admin LLM."""
    import database
    from clinic_data import get_all_doctors
    from clinic_config import get_clinic_info

    info = get_clinic_info()
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    lines = []
    lines.append("=== CLINIC INFO ===")
    lines.append(f"Name: {info['name']}")
    lines.append(f"Address: {info['address']}")
    lines.append(f"Phone: {info['phone']}")
    lines.append("")

    # ---- Doctors ----
    doctors = get_all_doctors()
    lines.append(f"=== DOCTORS ({len(doctors)} total) ===")
    by_dept = {}
    for doc in doctors:
        by_dept.setdefault(doc["department"], []).append(doc)

    for dept in sorted(by_dept.keys()):
        lines.append(f"\n{dept}:")
        for doc in by_dept[dept]:
            schedule_str = ", ".join(
                f"{day} {h['start']}-{h['end']}"
                for day, h in doc.get("schedule", {}).items()
            )
            lines.append(
                f"  - {doc['name']} ({doc.get('specialization', '')})"
                f" | Schedule: {schedule_str or 'N/A'}"
            )
    lines.append("")

    # ---- Today ----
    today_str = today.strftime("%Y-%m-%d")
    today_bookings = database.get_appointments_for_date(today_str)
    lines.append(f"=== TODAY ({today_str}) — {len(today_bookings)} bookings ===")
    for b in today_bookings:
        lines.append(
            f"  #{b['serial_number']} | {b['doctor_name']} | "
            f"{b['patient_name']} | {b['phone']} | {b['estimated_time']}"
        )
    lines.append("")

    # ---- Tomorrow ----
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    tomorrow_bookings = database.get_appointments_for_date(tomorrow_str)
    lines.append(f"=== TOMORROW ({tomorrow_str}) — {len(tomorrow_bookings)} bookings ===")
    for b in tomorrow_bookings:
        lines.append(
            f"  #{b['serial_number']} | {b['doctor_name']} | "
            f"{b['patient_name']} | {b['phone']} | {b['estimated_time']}"
        )
    lines.append("")

    # ---- Last 7 days ----
    lines.append("=== LAST 7 DAYS ===")
    for i in range(7):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        bookings = database.get_appointments_for_date(d_str)
        lines.append(f"{d_str}: {len(bookings)} booking(s)")

    return "\n".join(lines)


# =========================================================
# ADMIN SYSTEM PROMPT
# =========================================================
ADMIN_PROMPT = """You are Jhon's admin assistant for HealthCare Hospital.

The person you're talking to is a CLINIC ADMIN (not a patient).
They have full control of the clinic's doctors, schedules, and bookings.

You have REAL-TIME access to clinic data (see CONTEXT below).
Use it to answer naturally and accurately.

--- YOUR ROLE ---
- Answer admin questions about doctors, schedules, bookings, patients.
- Be concise, professional, and factual.
- Reply in the same language the admin uses (English/Bangla/Banglish).
- Do NOT behave like a patient-booking bot.
- If admin asks about a specific doctor's bookings, show schedule
  and any bookings today/tomorrow for that doctor.
- If admin asks "koto booking hoyeche", count today's bookings.
- If admin asks about a patient, search bookings for the name/phone.
- Use plain text only. No Markdown (** or _ or `).
- IMPORTANT: you CANNOT change any data yourself. Never say you
  added, updated, removed or changed a doctor, schedule or booking.
  If the admin asks to add/change/remove something, tell them the
  exact command format instead (e.g. "Add Dr. Samiya as Urology,
  Sunday 8am-11am, limit 40").

--- CONTEXT (REAL-TIME DATA) ---
{context}
--- END CONTEXT ---
"""


# =========================================================
# MAIN ENTRYPOINT
# =========================================================
# =========================================================
# ADMIN INTENT ROUTER — the admin panel understands plain talk,
# like a human assistant: an LLM classifies the intent, and a
# keyword fallback covers the same intents when the LLM is
# unreachable. No exact phrase is required any more.
# =========================================================
_ADMIN_INTENTS = (
    "ADD_DOCTOR", "REMOVE_DOCTOR", "ADD_DEPARTMENT", "MOVE_DOCTOR",
    "ADD_SLOT", "REPORT", "LIST_DOCTORS", "BOOKING_LIST",
    "PATIENT_SEARCH", "GENERAL",
)


def _classify_admin_intent(message):
    """LLM intent classification for an admin message.

    Returns one of _ADMIN_INTENTS, or None when the LLM cannot
    decide (the caller then uses the keyword fallback).
    """
    system_prompt = (
        "You classify an admin's message for a hospital receptionist bot. "
        "Reply with ONLY one of these words, nothing else:\n"
        "ADD_DOCTOR - add a new doctor (e.g. 'Add Dr. Sufan as Urology, Saturday 12:40pm-2pm')\n"
        "REMOVE_DOCTOR - remove/delete a doctor (e.g. 'Remove Dr. Rana', 'ENT theke Dr. Rana ke remove kore felo')\n"
        "ADD_DEPARTMENT - create a new department (e.g. 'ekta notun department banow, naam Physiotherapy')\n"
        "MOVE_DOCTOR - move a doctor to another department (e.g. 'Dr. Karim ke Physiotherapy te boshow')\n"
        "ADD_SLOT - add a time slot to an existing doctor (e.g. 'Add Friday 5-9pm to Dr. Karim')\n"
        "REPORT - ask for today's report (e.g. 'report dao', 'aajker report')\n"
        "LIST_DOCTORS - list the doctors (e.g. 'doctor list dao', 'koyjon doctor ache')\n"
        "BOOKING_LIST - how many bookings / who booked (e.g. 'ajker booking koto', 'booking list dao')\n"
        "PATIENT_SEARCH - search a patient by name or phone (e.g. 'patient search 017...')\n"
        "GENERAL - anything else (a question, greeting, or unclear request)\n"
    )
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=10,
        )
        intent = response.choices[0].message.content.strip().upper()
        if intent in _ADMIN_INTENTS:
            return intent
    except Exception:
        pass
    return None


def _intent_by_keywords(msg):
    """Deterministic fallback when the LLM cannot classify.

    Order matters: report / remove / department / move first,
    because their phrases can also contain 'doctor' or 'add'.
    """
    if "report" in msg:
        return "REPORT"
    if any(k in msg for k in (
        "remove", "delete", "soriye", "soraw", "bad dao", "bad deo",
    )):
        return "REMOVE_DOCTOR"
    if any(k in msg for k in (
        "notun department", "new department", "department banow",
        "department banao", "department toiri", "add department",
        "create department", "notun dept", "department khulo",
    )):
        return "ADD_DEPARTMENT"
    if any(k in msg for k in (
        "boshow", "boshao", "boshiye", "move", "shift koro",
    )):
        return "MOVE_DOCTOR"
    if "slot" in msg:
        return "ADD_SLOT"
    if any(k in msg for k in (
        "add doctor", "add dr", "notun doctor", "doctor add",
        "add a doctor", "new doctor", "notun daktar", "daktar add",
    )):
        return "ADD_DOCTOR"
    if any(k in msg for k in (
        "list doctor", "doctor list", "doctor gula", "koyjon doctor",
        "sob doctor", "doctor der list",
    )):
        return "LIST_DOCTORS"
    if any(k in msg for k in (
        "koto booking", "booking hoyeche", "booking list",
        "ajker booking", "booking dekhaw", "booking gula",
    )):
        return "BOOKING_LIST"
    if any(k in msg for k in (
        "patient", "rogi", "phone", "number", "history",
        "booking history",
    )):
        return "PATIENT_SEARCH"
    return "GENERAL"


def admin_reply(message):
    """Routes an admin message to the right handler.

    Understands plain talk like a human assistant: the LLM
    classifies the intent, and a keyword fallback covers the
    same intents when the LLM is unreachable.
    """
    # The user often pastes the bot's own example in quotes:
    # "'Add Dr. Sufan as Urology, ...'" — strip them first.
    message = message.strip().strip("'\"")
    msg = message.lower().strip()

    intent = _classify_admin_intent(message) or _intent_by_keywords(msg)

    if intent == "ADD_DOCTOR":
        return _handle_add_doctor(message)
    if intent == "REMOVE_DOCTOR":
        return _handle_remove_doctor(message)
    if intent == "ADD_DEPARTMENT":
        return _handle_add_department(message)
    if intent == "MOVE_DOCTOR":
        return _handle_move_doctor(message)
    if intent == "ADD_SLOT":
        slot_result = _handle_add_slot(message)
        if slot_result:
            return slot_result
        return _answer_query(message)
    if intent == "REPORT":
        return _handle_report(message)
    if intent == "LIST_DOCTORS":
        return _handle_list_doctors(message)
    if intent == "BOOKING_LIST":
        return _handle_report(message)
    if intent == "PATIENT_SEARCH":
        result = _handle_patient_search(message)
        if result:
            return result
        return _answer_query(message)

    # GENERAL — a question, greeting, or anything else.
    return _answer_query(message)


# =========================================================
# NORMAL QUERY — LLM with clinic context
# =========================================================
def _answer_query(message):
    """Normal admin query using LLM with clinic context."""
    try:
        context = _build_admin_context()
        system_prompt = ADMIN_PROMPT.format(context=context)

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0.3,
            max_completion_tokens=500,
        )
        reply = response.choices[0].message.content
        return reply.strip() if reply else None
    except Exception:
        print("Admin Chat Error:")
        print(traceback.format_exc())
        return None


# =========================================================
# PARSING HELPERS (shared by add-doctor + add-slot)
# =========================================================
_DAY_ALIASES = {
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


def _extract_day_and_time(message):
    """Returns (day_name, start, end) or (None, None, None).

    Understands 'sunday 8am to 11am', 'friday 5pm-9pm',
    'sonibar 8am-11am' and Bangla script day names.
    """
    import re

    msg = message.lower()
    day = None
    for alias in sorted(_DAY_ALIASES, key=len, reverse=True):
        if alias in msg:
            day = _DAY_ALIASES[alias]
            break

    start = end = None
    tm = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|-)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        msg,
    )
    if tm:
        sh, sm, sap, eh, em, eap = tm.groups()
        sap = sap or eap  # "5-9pm" means 5pm-9pm
        start = f"{int(sh):02d}:{sm or '00'} {sap.upper()}"
        end = f"{int(eh):02d}:{em or '00'} {eap.upper()}"

    return day, start, end


def _parse_add_doctor_regex(message):
    """Fallback parser when the LLM extraction fails.

    Handles: 'add doctor samiya to Urologgy at sunday 8am to 11am
    limit 40' — including department typos (Urologgy -> Urology).
    Returns a doctor dict or None.
    """
    import re
    from difflib import SequenceMatcher

    msg = message.lower()
    m = re.search(r"(?:dr\.?|doctor)\s+([a-z]+)", msg)
    if not m:
        return None
    first = m.group(1).strip()

    # If that first name belongs to an existing doctor, use the
    # full name from the database ("karim" -> "Dr. Karim Hossain").
    name = "Dr. " + first.title()
    try:
        from clinic_data import get_all_doctors
        for doc in get_all_doctors():
            norm = re.sub(r"[^a-z0-9]", "", doc["name"].lower())
            if first in norm:
                name = doc["name"]
                break
    except Exception:
        pass

    # Department: fuzzy against the real department list.
    dept = None
    try:
        from clinic_data import get_departments
        departments = get_departments()
    except Exception:
        departments = []
    best, best_score = None, 0.0
    for w in re.findall(r"[a-z]+", msg):
        for d in departments:
            s = SequenceMatcher(None, w, d.lower()).ratio()
            if s > best_score:
                best, best_score = d, s
    if best_score >= 0.7:
        dept = best

    day, start, end = _extract_day_and_time(message)
    maxp = 20
    lm = re.search(r"limit\s*(\d+)", msg)
    if lm:
        maxp = int(lm.group(1))
    # "10 minutes per patient" -> average_patient_minutes (default 10).
    minutes = 10
    mm = re.search(r"(\d+)\s*minutes?\s*per\s*patient", msg)
    if mm:
        minutes = int(mm.group(1))

    if not dept or not day or not start:
        return None
    return {
        "name": name,
        "department": dept,
        "specialization": dept,
        "average_patient_minutes": minutes,
        "max_patients_per_session": maxp,
        "schedule": {day: {"start": start, "end": end}},
        "confidence": "high",
    }


def _sync_file_quietly():
    """Write the DB's active doctors back into clinic_data.py so
    the file mirrors the live data. Never breaks the admin reply
    if the file write fails — the DB change already happened."""
    try:
        from sync import sync_file_from_db
        sync_file_from_db()
    except Exception as e:
        print(f"[sync] Could not write clinic_data.py: {e}")



def _fuzzy_department_from_message(message):
    """Finds a known department name inside a free message."""
    import re
    from difflib import SequenceMatcher

    try:
        import database
        from clinic_data import get_departments
        known = list(get_departments())
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT name FROM departments WHERE active = 1"
            ).fetchall()
        known += [r["name"] for r in rows]
    except Exception:
        known = []
    if not known:
        return None
    msg = message.lower()
    best, best_score = None, 0.0
    words = re.findall(r"[a-z]+", msg)
    candidates = list(words) + [
        f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)
    ]
    for cand in candidates:
        for d in known:
            s = SequenceMatcher(None, cand, d.lower()).ratio()
            if s > best_score:
                best, best_score = d, s
    if best_score >= 0.7:
        return best
    return None


def _move_doctor_to_department(doctor_name, department):
    """Moves one doctor to another department (DB + file sync).
    Returns (True, canonical_name) or (False, reason).
    """
    import database

    try:
        from clinic_data import find_doctor_by_name
        doc = find_doctor_by_name(doctor_name)
    except Exception:
        doc = None
    if doc is None:
        return False, "not found"
    with database.get_connection() as conn:
        cur = conn.execute(
            "SELECT id FROM doctors WHERE LOWER(name) = ? AND active = 1",
            (doc["name"].lower(),),
        )
        row = cur.fetchone()
        if not row:
            return False, "not found"
        # The target department becomes a real department too.
        conn.execute(
            "INSERT OR IGNORE INTO departments (name, active) VALUES (?, 1)",
            (department,),
        )
        conn.execute(
            "UPDATE doctors SET department = ? WHERE id = ?",
            (department, row["id"]),
        )
        conn.commit()
    _sync_file_quietly()
    return True, doc["name"]


def _handle_add_department(message):
    """Creates a new department, and optionally moves named doctors
    into it ('ekta notun department banow, naam Physiotherapy,
    Dr. Karim ke boshow').
    """
    import json
    import re
    import database

    dept = None
    doctors_to_move = []
    # 1. LLM extraction
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": (
                    "Extract the department name and any doctor names from the admin's message.\n"
                    "Reply with ONLY valid JSON, no other text:\n"
                    "{\"department\": \"Physiotherapy\", \"doctors\": [\"Dr. Karim\"]}\n"
                    "If no department name is found, set \"department\": null.\n"
                    "If no doctors are named, set \"doctors\": []."
                )},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=150,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        dept = data.get("department")
        doctors_to_move = data.get("doctors") or []
    except Exception:
        dept = None
        doctors_to_move = []

    # 2. Regex fallback for the department name.
    # "naam X" first — "notun department banow" would otherwise
    # capture "banow" as the name.
    if not dept:
        msg = message.lower()
        stop = ("banow", "banao", "toiri", "khulo", "add", "create", "department", "dept")
        patterns = [
            r"naam\s+([a-z]+(?:\s+[a-z]+)?)",
            r"department\s+([a-z]+(?:\s+[a-z]+)?)\s+(banow|banao|toiri|add|create|khulo)",
            r"(?:create|add)\s+department\s+([a-z]+(?:\s+[a-z]+)?)",
            r"notun department\s+([a-z]+(?:\s+[a-z]+)?)",
        ]
        for pat in patterns:
            mm = re.search(pat, msg)
            if mm:
                cand = mm.group(1).strip()
                cand = re.sub(
                    r"\s+(banow|banao|toiri|add|create|khulo|department|dept)$",
                    "", cand, flags=re.I,
                ).strip()
                if cand and cand.split()[0] not in stop:
                    dept = cand.title()
                    break

    # Doctors named in the message ("Dr. Karim ke boshow")
    if not doctors_to_move:
        for mm in re.finditer(r"(?:dr\.?|doctor)\s+([a-z]+)", message.lower()):
            doctors_to_move.append(mm.group(1))

    if not dept:
        return (
            "Notun department er nam ta bolo.\n"
            "Example: 'Ekta notun department banow, naam Physiotherapy'"
        )

    # 3. Create the department (idempotent).
    with database.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO departments (name, active) VALUES (?, 1)",
            (dept,),
        )
        conn.commit()

    # 4. Move any named doctors into it.
    moved = []
    for dname in doctors_to_move:
        ok, cname = _move_doctor_to_department(dname, dept)
        if ok:
            moved.append(cname)

    lines = [f"\u2705 Notun department toiri holo: {dept}"]
    if moved:
        lines.append("Ei doctor der ekhane boshano holo: " + ", ".join(moved))
    lines.append("Website-e department list-e dekhabe.")
    return "\n".join(lines)


def _handle_move_doctor(message):
    """Moves a doctor to another department.
    ('Dr. Karim ke Physiotherapy te boshow')
    """
    import json
    import re

    doctor = None
    dept = None
    # 1. LLM extraction
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": (
                    "Extract the doctor and the target department from the admin's message.\n"
                    "Reply with ONLY valid JSON, no other text:\n"
                    "{\"doctor\": \"Dr. Karim\", \"department\": \"Physiotherapy\"}\n"
                    "If either is missing, set it to null."
                )},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=100,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        doctor = data.get("doctor")
        dept = data.get("department")
    except Exception:
        doctor = None
        dept = None

    # 2. Regex / fuzzy fallback
    if not doctor:
        mm = re.search(r"(?:dr\.?|doctor)\s+([a-z]+)", message.lower())
        if mm:
            doctor = mm.group(1)
    if not dept:
        dept = _fuzzy_department_from_message(message)

    if not doctor or not dept:
        return (
            "Kon doctor ke kon department-e boshabo, duitai bolo.\n"
            "Example: 'Dr. Karim ke Physiotherapy te boshow'"
        )

    ok, name = _move_doctor_to_department(doctor, dept)
    if not ok:
        return f"Doctor '{doctor}' ke khuje pelam na."
    return (
        f"\u2705 {name} ke ekhon {dept} department-e boshano holo.\n"
        "Website updated instantly."
    )


def _handle_report(message):
    """Today's booking report, right in the chat."""
    import database

    today = datetime.now().date().strftime("%Y-%m-%d")
    rows = database.get_appointments_for_date(today)
    if not rows:
        return f"\U0001f4ca Aajker report ({today}): kono booking nei."
    by_doctor = {}
    for r in rows:
        by_doctor.setdefault(r["doctor_name"], []).append(r)
    lines = [f"\U0001f4ca Aajker report ({today}) \u2014 mot booking: {len(rows)}", ""]
    for doctor, appts in by_doctor.items():
        dept = appts[0].get("department", "")
        lines.append(f"\u2022 {doctor} ({dept}) \u2014 {len(appts)} ta")
        for a in appts:
            lines.append(
                f"   {a['serial_number']}. {a['patient_name']} \u2013 "
                f"{a['phone']} \u2013 {a.get('estimated_time', '')}"
            )
    return "\n".join(lines)


def _handle_list_doctors(message):
    """All active doctors, grouped by department."""
    from clinic_data import get_all_doctors

    doctors = get_all_doctors()
    if not doctors:
        return "Kono active doctor nei."
    by_dept = {}
    for d in doctors:
        by_dept.setdefault(d["department"], []).append(d)
    lines = [f"\U0001f468\u200d\u2695\ufe0f Mot active doctor: {len(doctors)}", ""]
    for dept in sorted(by_dept):
        lines.append(f"\u2022 {dept} ({len(by_dept[dept])})")
        for d in by_dept[dept]:
            days = ", ".join(d.get("schedule", {}).keys())
            lines.append(f"   \u2013 {d['name']} ({days})")
    return "\n".join(lines)


def _handle_add_slot(message):
    """Add a time slot to an EXISTING doctor.

    Example: 'add Friday 5-9pm to Dr. Karim' / 'karim ke
    friday 5pm-9pm add koro'. Returns a reply string, or None
    when the message is not a slot-add (caller falls through
    to the normal query handler).
    """
    import json
    import re
    import database
    from clinic_data import get_all_doctors

    msg = message.lower()
    if "add doctor" in msg or "notun doctor" in msg or "new doctor" in msg:
        return None  # that is a new doctor, not a slot — other handler

    day, start, end = _extract_day_and_time(message)
    if not day or not start:
        return None

    # Which doctor? Match by first name, last name, or full name.
    doctor = None
    msg_nospace = re.sub(r"[^a-z0-9]", "", msg)
    for doc in get_all_doctors():
        plain = doc["name"].lower().replace("dr. ", "").strip()
        parts = plain.split()
        first = parts[0] if parts else ""
        last = parts[-1] if parts else ""
        full_nospace = re.sub(r"[^a-z0-9]", "", plain)
        if (first and first in msg) or (last and last in msg) \
                or (full_nospace and full_nospace in msg_nospace):
            doctor = doc
            break
    if not doctor:
        return None

    with database.get_connection() as conn:
        cur = conn.execute(
            "SELECT schedule FROM doctors WHERE name = ?", (doctor["name"],)
        )
        row = cur.fetchone()
        sched = {}
        if row and row["schedule"]:
            try:
                sched = json.loads(row["schedule"])
            except (json.JSONDecodeError, TypeError):
                sched = {}
        sched[day] = {"start": start, "end": end}
        conn.execute(
            "UPDATE doctors SET schedule = ? WHERE name = ?",
            (json.dumps(sched), doctor["name"]),
        )
        conn.commit()
    _sync_file_quietly()

    return (f"✅ {day} {start}-{end} added to {doctor['name']}'s "
            f"schedule ({doctor['department']}).\n\n"
            "Website updated instantly.")


# =========================================================
# ADD DOCTOR
# =========================================================
def _handle_add_doctor(message):
    """Parses doctor info from natural language and inserts to DB."""
    import json
    import database

    system_prompt = """Extract doctor info from the admin's message.

Reply with ONLY valid JSON, no other text:
{
  "name": "Dr. X",
  "department": "Urology",
  "specialization": "Urology",
  "average_patient_minutes": 10,
  "max_patients_per_session": 20,
  "schedule": {"Friday": {"start": "05:00 PM", "end": "09:00 PM"}},
  "confidence": "high"
}

Rules:
- If any field is missing, use sensible defaults.
- Schedule format: {"Day": {"start": "HH:MM AM/PM", "end": "HH:MM AM/PM"}}
- If you cannot extract a name, set "confidence": "low".
"""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            temperature=0,
            max_completion_tokens=300,
        )
        raw = response.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = None

        if not data or data.get("confidence") == "low" or not data.get("name"):
            # LLM extraction failed — try the plain parser instead.
            data = _parse_add_doctor_regex(message)

        if not data or not data.get("name"):
            return (
                "I couldn't extract the doctor's name. Please say:\n"
                "'Add Dr. XYZ as Cardiology, Monday 9am-1pm'"
            )

        # UPSERT: new doctor -> INSERT; existing doctor -> merge the
        # new schedule in (so 'add Friday to Dr. Karim' actually works).
        with database.get_connection() as conn:
            cur = conn.execute(
                "SELECT id, schedule, department FROM doctors "
                "WHERE LOWER(name) = ?",
                (data["name"].lower(),),
            )
            row = cur.fetchone()
            existing_dept = row["department"] if row else None

            new_sched = dict(data.get("schedule", {}) or {})
            if row is None:
                conn.execute(
                    """
                    INSERT INTO doctors
                        (name, department, specialization, description,
                         average_patient_minutes, max_patients_per_session,
                         schedule, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        data["name"],
                        data["department"],
                        data.get("specialization", data["department"]),
                        f"Specializes in {data['department']}",
                        data.get("average_patient_minutes", 10),
                        data.get("max_patients_per_session", 20),
                        json.dumps(new_sched),
                    ),
                )
                action = "added"
            else:
                old_sched = {}
                if row["schedule"]:
                    try:
                        old_sched = json.loads(row["schedule"])
                    except (json.JSONDecodeError, TypeError):
                        old_sched = {}
                old_sched.update(new_sched)
                # Existing doctor: merge the new slot(s) only. Keep
                # their department, specialization, minutes and limit
                # exactly as they were.
                conn.execute(
                    """
                    UPDATE doctors SET schedule = ?, active = 1
                    WHERE id = ?
                    """,
                    (json.dumps(old_sched), row["id"]),
                )
                action = "updated"
            conn.commit()
        _sync_file_quietly()

        sched = ", ".join(
            f"{d} {h['start']}-{h['end']}"
            for d, h in data.get("schedule", {}).items()
        ) or "no schedule set"
        dept_shown = existing_dept or data["department"]

        return (
            f"✅ {data['name']} {action} successfully!\n\n"
            f"Name: {data['name']}\n"
            f"Department: {dept_shown}\n"
            f"Schedule: {sched}\n"
            f"Avg minutes: {data.get('average_patient_minutes', 10)}\n"
            f"Max patients: {data.get('max_patients_per_session', 20)}\n\n"
            f"Website updated instantly."
        )
    except Exception as e:
        print(f"Add doctor error: {e}")
        traceback.print_exc()
        return (
            "Sorry, I couldn't parse that. Please try:\n"
            "'Add Dr. Karim as Urology, Friday 5pm-9pm, 10 minutes per patient'"
        )


# =========================================================
# REMOVE DOCTOR
# =========================================================
def _handle_remove_doctor(message):
    """Removes a doctor by name: deactivates them, drops them from
    the website file, and cancels their FUTURE appointments so
    nobody shows up for a removed doctor. Past bookings stay as
    records.
    """
    import re
    import database

    try:
        name = None
        # 1. LLM extraction
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "Extract the doctor's name from this message.\nThe admin wants to remove/delete a doctor.\nReply with ONLY the doctor's name (e.g. \"Dr. Karim\"), nothing else.\nIf no name found, reply: NONE"},
                    {"role": "user", "content": message},
                ],
                temperature=0,
                max_completion_tokens=30,
            )
            name = response.choices[0].message.content.strip().strip(".")
            # The LLM must return a plain name ("Dr. Rana"). Anything
            # else (JSON, braces, a sentence) is not a name — fall
            # through to the regex extractor below.
            if (not name or name.upper() == "NONE"
                    or not re.fullmatch(r"[A-Za-z .'-]{2,40}", name)):
                name = None
        except Exception:
            name = None

        # 2. Regex fallback: "Dr. Rana" / "doctor rana" / "rana ke ..."
        if not name:
            rm = re.search(r"(?:dr\.?|doctor)\s+([a-z]+)", message.lower())
            if rm:
                name = rm.group(1)

        if not name:
            return (
                "Kon doctor ke remove korbo, nam ta bolo.\n"
                "Example: 'Remove Dr. Rana'"
            )

        # 3. Find the doctor (fuzzy, like the patient flow).
        canonical = None
        try:
            from clinic_data import find_doctor_by_name
            doc = find_doctor_by_name(name)
            if doc:
                canonical = doc["name"]
        except Exception:
            pass
        with database.get_connection() as conn:
            if canonical:
                cur = conn.execute(
                    "SELECT id, name FROM doctors WHERE LOWER(name) = ? AND active = 1",
                    (canonical.lower(),),
                )
            else:
                cur = conn.execute(
                    "SELECT id, name FROM doctors WHERE LOWER(name) LIKE ? AND active = 1",
                    (f"%{name.lower()}%",),
                )
            rows = cur.fetchall()

        if not rows:
            return f"Doctor '{name}' ke khuje pelam na. Nam ta thik ache to?"
        if len(rows) > 1:
            names = ", ".join(f"\u2022 {r['name']}" for r in rows)
            return (
                f"'{name}' diye ekadhik doctor pelam:\n{names}\n\n"
                "Puro nam ta bolo."
            )
        doc = rows[0]

        # 4. Deactivate + cancel FUTURE appointments (today onwards).
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE doctors SET active = 0 WHERE id = ?",
                (doc["id"],),
            )
            today = datetime.now().date().strftime("%Y-%m-%d")
            cur = conn.execute(
                "DELETE FROM appointments WHERE doctor_name = ? AND date >= ?",
                (doc["name"], today),
            )
            cancelled = cur.rowcount
            conn.commit()
        _sync_file_quietly()

        lines = [
            f"\u2705 {doc['name']} ke remove kore dilam.",
            "Website thekeo sorano hoyeche.",
        ]
        if cancelled:
            lines.append(f"Aaj theke porer booking cancel holo: {cancelled} ta.")
        else:
            lines.append("Tar kono future booking chilo na.")
        lines.append("Purono booking-er record Sheet ar email-e ache.")
        return "\n".join(lines)
    except Exception as e:
        print("[admin] remove doctor error:", e)
        traceback.print_exc()
        return (
            "Doctor remove korte giye ekta problem holo. "
            "Abar try koro, ba puro nam diye bolo: 'Remove Dr. Rana'"
        )


# =========================================================
def _handle_patient_search(message):
    """
    Search bookings by patient name or phone.
    Returns None if no name/phone found (so normal query runs).
    """
    import re
    import database

    # Try phone first
    phone_match = re.search(r"01[3-9]\d{8}", message)
    if phone_match:
        phone = phone_match.group()
        with database.get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM appointments WHERE phone LIKE ? ORDER BY date DESC",
                (f"%{phone}%",),
            )
            rows = cur.fetchall()

        if not rows:
            return f"No bookings found for phone {phone}."

        lines = [f"Patient phone: {phone}", f"Total bookings: {len(rows)}", ""]
        for r in rows:
            lines.append(
                f"#{r['serial_number']} | {r['doctor_name']} | "
                f"{r['patient_name']} | {r['date']} | {r['estimated_time']}"
            )
        return "\n".join(lines)

    # No phone — try name from LLM
    system_prompt = """Extract the patient name from this message.
Reply with ONLY the name (e.g. "Omar Faruk"), nothing else.
If no clear name found, reply: NONE
"""
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
        name = response.choices[0].message.content.strip().strip(".")

        if not name or name.upper() == "NONE":
            return None  # fall through to normal query

        with database.get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM appointments WHERE LOWER(patient_name) LIKE ? "
                "ORDER BY date DESC",
                (f"%{name.lower()}%",),
            )
            rows = cur.fetchall()

        if not rows:
            return f"No bookings found for patient '{name}'."

        lines = [f"Patient: {name}", f"Total bookings: {len(rows)}", ""]
        for r in rows:
            lines.append(
                f"#{r['serial_number']} | {r['doctor_name']} | "
                f"{r['date']} | {r['estimated_time']} | Phone: {r['phone']}"
            )
        return "\n".join(lines)
    except Exception:
        print("Patient search error:")
        print(traceback.format_exc())
        return None