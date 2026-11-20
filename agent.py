"""
Jhon, the AI receptionist — AGENT version (Jervis-style).

Instead of a fixed state machine + a small intent classifier,
one LLM drives the conversation and calls TOOLS for every fact:
real schedules, real availability, real bookings — all from the
database. The LLM decides WHAT to do; the tools decide what is
TRUE. That is what makes it understand free-form messages
("hea", "ar ent", "na thak ami urologist dekhabo", mid-flow
doctor switches) instead of guessing from keyword lists.

Entry point: process_message(message, session) — the same
signature web_agent.process_message has, so web_server.py can
swap it in with one import. The rule-based web_agent.py stays
in the folder untouched as the fallback.

Model: llama-3.3-70b-versatile on Groq (much better at
understanding + tool use than the 20b model). If that call
fails (rate limit, model error), retry once with
openai/gpt-oss-20b — the proven model. If both fail, a safe
fallback reply is returned instead of crashing.
"""

import json
import os
import re
import threading
import traceback
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY is missing")

_client = Groq(api_key=GROQ_API_KEY)
_MODEL_PRIMARY = "llama-3.3-70b-versatile"
_MODEL_FALLBACK = "openai/gpt-oss-20b"

_MAX_TOOL_ROUNDS = 6
_MAX_HISTORY = 12  # messages kept in the session (6 turns)


# ------------------------------------------------------------------
# Date / doctor / department resolution (deterministic helpers)
# ------------------------------------------------------------------
def _resolve_date(day_or_date):
    """'Friday' / '2026-11-13' / 'tomorrow' / '' -> date or None.

    A bare weekday means its NEXT occurrence (today counts when
    the doctor works today — the availability check then applies
    the cutoff rule, exactly like the old flow).
    """
    from web_agent import get_next_date_for_day, parse_booking_date

    if not day_or_date:
        return None
    text = str(day_or_date).strip()
    low = text.lower()
    today = datetime.now().date()
    if low in ("today", "ajke", "aajke", "আজকে", "আজ"):
        return today
    if low in ("tomorrow", "agamikal", "agami kal", "কালকে", "কাল"):
        return today + timedelta(days=1)
    parsed = parse_booking_date(text)
    if parsed:
        return parsed
    # Romanized Bangla weekday names — the user types romanized
    # Bangla ("sombar", "sonibar"), and the agent used to miss them
    # (2026-11-07: "sombar" fell back to today's closed message).
    # Checked BEFORE the English loop: "mongolbar" starts with
    # "mon", so the English prefix check would wrongly call it Monday.
    roman_days = [
        ("robibar", "Sunday"), ("robbar", "Sunday"),
        ("sombar", "Monday"), ("shombar", "Monday"),
        ("mongolbar", "Tuesday"), ("mongol", "Tuesday"),
        ("budhbar", "Wednesday"), ("budbar", "Wednesday"),
        ("brihospotibar", "Thursday"), ("brihos", "Thursday"),
        ("bihospotibar", "Thursday"),
        ("sukrobar", "Friday"), ("shukrobar", "Friday"),
        ("jummabar", "Friday"),
        ("sonibar", "Saturday"), ("shonibar", "Saturday"),
    ]
    for alias, eng in roman_days:
        if low == alias or low.startswith(alias):
            return get_next_date_for_day(eng)
    if low in ("porshu", "porsu"):
        return today + timedelta(days=2)
    # A bare weekday name the parser missed (e.g. "fri").
    weekdays = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]
    for i, name in enumerate(weekdays):
        if low.startswith(name[:3]):
            return get_next_date_for_day(name.title())
    return None


def _resolve_doctor(name):
    """Fuzzy doctor lookup. Returns (doctor_dict_or_None, all_names)."""
    from clinic_data import find_doctor_by_name, get_all_doctors

    if not name:
        return None, [d["name"] for d in get_all_doctors()]
    doctor = find_doctor_by_name(name)
    if doctor:
        return doctor, []
    return None, [d["name"] for d in get_all_doctors()]


def _resolve_department(name):
    """Department lookup: exact, then fuzzy. Returns name or None."""
    from clinic_data import get_departments

    if not name:
        return None
    departments = get_departments()
    low = name.lower().strip()
    for dept in departments:
        if dept.lower() == low:
            return dept
    best, best_score = None, 0.0
    for dept in departments:
        score = SequenceMatcher(None, low, dept.lower()).ratio()
        if score > best_score:
            best, best_score = dept, score
    # 0.6 is deliberately lenient: "urologgy" -> Urology (0.93),
    # "ent" is exact. A wrong guess here only lists doctors, so
    # a lenient match is safe — the patient picks from the list.
    if best_score >= 0.6:
        return best
    return None


def _doctor_public(doctor):
    return {
        "name": doctor["name"],
        "department": doctor["department"],
        "specialization": doctor.get("specialization", ""),
        "schedule": doctor.get("schedule", {}) or {},
    }


# ------------------------------------------------------------------
# TOOLS — every fact the agent states comes from one of these.
# ------------------------------------------------------------------
def _tool_list_doctors(args, session):
    from clinic_data import find_doctors_by_department, get_all_doctors

    dept = args.get("department")
    if dept:
        resolved = _resolve_department(dept)
        if not resolved:
            return {"found": False,
                    "message": f"No department named '{dept}'.",
                    "departments": [d for d in __import__("clinic_data").get_departments()]}
        doctors = find_doctors_by_department(resolved)
        return {"found": True, "department": resolved,
                "doctors": [_doctor_public(d) for d in doctors]}
    doctors = get_all_doctors()
    return {"found": True, "doctors": [_doctor_public(d) for d in doctors]}


def _tool_get_doctor_schedule(args, session):
    doctor, all_names = _resolve_doctor(args.get("doctor_name"))
    if not doctor:
        return {"found": False,
                "message": f"Could not identify a doctor named '{args.get('doctor_name')}'.",
                "all_doctors": all_names}
    # Also hand the agent today's cutoff status + the real next
    # chamber days, so a schedule answer never has to guess: the
    # old flow showed the FULL schedule and noted a passed cutoff,
    # and the agent must do the same (2026-11-07: it showed only
    # Saturday for 6-day Ayesha Khan and invented Sufan's hours).
    from datetime import datetime as _dt
    from booking_service import check_booking, get_next_available_days
    today_d = _dt.now().date()
    today_name = today_d.strftime("%A")
    sched = doctor.get("schedule", {}) or {}
    today_info = {
        "day": today_name,
        "date": today_d.strftime("%Y-%m-%d"),
        "is_chamber_day": today_name in sched,
        "booking_open": None,
        "note": "",
    }
    if today_info["is_chamber_day"]:
        chk = check_booking(doctor["name"], today_d, today_name,
                            today_d, lang="en")
        today_info["booking_open"] = bool(
            chk.get("available", chk.get("valid", False)))
        today_info["note"] = chk.get("message", "")
    today_info["next_days"] = get_next_available_days(doctor["name"],
                                                       today_d)
    return {"found": True, "doctor": _doctor_public(doctor),
            "today": today_info}


def _tool_find_department_for_symptom(args, session):
    """Deterministic keyword match first, LLM triage as fallback."""
    from clinic_data import find_doctors_by_department
    from web_agent import _direct_symptom_match

    symptom = args.get("symptom") or ""
    dept = _direct_symptom_match(symptom)
    if not dept:
        from doctor_router import find_doctors_for_problem

        routing = find_doctors_for_problem(symptom)
        if routing.get("found"):
            dept = routing["department"]
        elif routing.get("needs_clarification"):
            return {"found": False, "needs_clarification": True,
                    "message": "The symptom is too vague to pick a department."}
        else:
            return {"found": False,
                    "message": "Could not match the symptom to a department."}
    doctors = find_doctors_by_department(dept)
    return {"found": True, "department": dept,
            "doctors": [_doctor_public(d) for d in doctors]}


def _tool_check_availability(args, session):
    from booking_service import check_booking

    doctor, all_names = _resolve_doctor(args.get("doctor_name"))
    if not doctor:
        return {"available": False,
                "message": f"Could not identify a doctor named '{args.get('doctor_name')}'.",
                "all_doctors": all_names}
    date = _resolve_date(args.get("day_or_date"))
    if date is None:
        # No day given: hand the schedule back WITH the real next
        # chamber dates, and tell the agent to SHOW it before asking
        # — the old flow showed the schedule the moment a doctor was
        # named, and the agent used to ask "which day?" blind
        # (2026-11-07: the patient had to ask "kon kon din" first).
        from booking_service import get_next_available_days
        return {"available": False, "needs_day": True,
                "doctor": _doctor_public(doctor),
                "next_days": get_next_available_days(doctor["name"]),
                "message": ("No day was given. Show the patient the "
                            "doctor's schedule first — every chamber day "
                            "with hours and the next dates below — and "
                            "then ask which day suits them. Never ask "
                            "for the day without showing the schedule.")}

    day = date.strftime("%A")
    result = check_booking(doctor["name"], date, day,
                           datetime.now().date(), lang="en")
    # check_booking's invalid-date path returns {"valid": False}
    # instead of {"available": False} — normalise so the LLM
    # always sees one consistent flag.
    if "available" not in result:
        result["available"] = bool(result.get("valid", False))
    # When booking is NOT possible, attach the real next chamber
    # days (with exact dates) so the LLM never has to guess or
    # invent them — the 2026-11-07 Sufan bug was an empty day list
    # here, which the LLM filled in with invented Tue/Thu dates.
    if not result.get("available", False):
        from booking_service import get_next_available_days
        result["next_days"] = get_next_available_days(doctor["name"])
    result["doctor"] = doctor["name"]
    result["date"] = date.strftime("%Y-%m-%d")
    result["day"] = day
    return result


def _tool_create_booking(args, session):
    """Two-step booking — the summary is shown BEFORE anything is
    written, exactly like the old rule-based flow did:
      STEP 1 (no `confirmed`): validates everything and returns the
        booking PROPOSAL (doctor, date, serial, estimated time,
        arrival window) WITHOUT creating a booking. The agent shows
        this summary to the patient, asks them to confirm, and asks
        for their email address for the confirmation email (they
        may say "no" to skip it).
      STEP 2 (`confirmed` = true): creates the booking for real,
        fires the Sheet/admin notification, and — if an email was
        given — sends the patient confirmation email right away and
        reports whether it went (email_sent).
    A booking can NEVER be created by a step-1 call: the DB write
    lives only behind confirmed=true. The old flow's asking_email
    step is folded into step 2 (email in the confirmed call), and
    the separate send_confirmation_email tool stays as a fallback
    for an email given after the booking already exists.
    """
    from booking_service import create_booking

    doctor, all_names = _resolve_doctor(args.get("doctor_name"))
    if not doctor:
        return {"success": False,
                "message": f"Could not identify a doctor named '{args.get('doctor_name')}'.",
                "all_doctors": all_names}
    date = _resolve_date(args.get("day_or_date"))
    if date is None:
        return {"success": False,
                "message": "No booking day was given. Ask the patient which day."}
    patient_name = (args.get("patient_name") or "").strip()
    phone = (args.get("phone") or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(patient_name) < 2 or any(ch.isdigit() for ch in patient_name):
        return {"success": False,
                "message": "The patient name looks wrong or is missing. Ask for the name again."}
    if len(digits) < 10:
        return {"success": False,
                "message": "The phone number looks incomplete. Ask for the full phone number."}
    day = date.strftime("%A")

    email = (args.get("email") or "").strip()
    if email and not re.match(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                             email):
        return {"success": False,
                "message": "That email address does not look valid. Ask for the email again, or continue without it."}

    if not bool(args.get("confirmed")):
        # ---- STEP 1: proposal only. Verify the real availability
        # (cutoff + serial, same check create_booking runs) and
        # compute the serial/time — but write NOTHING. ----
        from booking_service import check_booking
        from availability import calculate_arrival_window
        chk = check_booking(doctor["name"], date, day,
                            datetime.now().date(), lang="en")
        if not chk.get("available", chk.get("valid", False)):
            return {"success": False, "proposal": False,
                    "message": chk.get("message",
                                       "That day is not available.")}
        serial = chk["next_serial"]
        window = calculate_arrival_window(doctor["name"], day, serial) or {}
        return {
            "success": False,
            "proposal": True,
            "message": ("Booking summary ready — show it to the patient, "
                        "ask them to confirm, and ask for their email "
                        "address for the confirmation email "
                        "(they can say 'no' to skip it)."),
            "proposal_details": {
                "doctor": doctor["name"],
                "department": doctor["department"],
                "date": date.strftime("%Y-%m-%d"),
                "day": day,
                "serial": serial,
                "estimated_time": window.get("estimated_time", ""),
                "arrival_window": (
                    f"{window.get('arrival_window_start', '')} - "
                    f"{window.get('arrival_window_end', '')}"
                ).strip(" -"),
                "patient_name": patient_name,
                "phone": phone,
            },
        }

    # ---- STEP 2: the patient said yes — create it for real. ----
    result = create_booking(doctor["name"], date, day, patient_name,
                            phone, datetime.now().date(), lang="en")
    if result.get("success"):
        appt = result["appointment"]
        session["last_appointment"] = appt
        # Google Sheet sync must not block the reply.
        try:
            from notifications import notify_new_booking
            threading.Thread(target=notify_new_booking, args=(appt,),
                             daemon=True).start()
        except Exception as e:
            print(f"Notification error: {e}")
        # Patient confirmation email — sent here (not left to a
        # separate tool call) so it cannot be skipped by accident.
        result["email_sent"] = False
        if email:
            try:
                from email_service import send_patient_confirmation
                result["email_sent"] = bool(
                    send_patient_confirmation(email, appt))
                result["email_to"] = email
            except Exception as e:
                print(f"Email error: {e}")
    return result


def _tool_cancel_booking(args, session):
    import database

    phone = (args.get("phone") or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        return {"success": False,
                "message": "I need the phone number the booking was made with."}
    today = datetime.now().date().strftime("%Y-%m-%d")
    doctor, _ = _resolve_doctor(args.get("doctor_name"))
    date = _resolve_date(args.get("day_or_date"))
    if doctor and date:
        ok = database.cancel_appointment_by_details(
            phone, doctor["name"], date.strftime("%Y-%m-%d"))
        if ok:
            return {"success": True, "cancelled": {
                "doctor": doctor["name"],
                "date": date.strftime("%Y-%m-%d")}}
        return {"success": False,
                "message": "No booking found for that phone, doctor and date."}
    upcoming = database.get_upcoming_appointments_for_phone(phone, today)
    if not upcoming:
        return {"success": False,
                "message": "No upcoming booking found for that phone number."}
    appt = upcoming[0]
    ok = database.cancel_appointment_by_details(
        phone, appt["doctor_name"], appt["date"])
    if ok:
        return {"success": True, "cancelled": {
            "doctor": appt["doctor_name"], "date": appt["date"],
            "serial": appt["serial_number"],
            "patient": appt["patient_name"]}}
    return {"success": False,
            "message": "Could not cancel the booking. Please try again."}


def _tool_get_clinic_info(args, session):
    from clinic_config import get_clinic_info
    from clinic_data import get_all_doctors, get_departments

    info = get_clinic_info()
    departments = {}
    for doctor in get_all_doctors():
        departments.setdefault(doctor["department"], []).append(doctor["name"])
    return {
        "name": info["name"],
        "address": info.get("address", ""),
        "phone": info.get("phone", ""),
        "email": info.get("email", ""),
        "departments": departments,
        "department_list": get_departments(),
    }


def _tool_send_confirmation_email(args, session):
    import re as _re

    email = (args.get("email") or "").strip()
    if not _re.match(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                     email):
        return {"success": False,
                "message": "That email address does not look valid."}
    appt = session.get("last_appointment")
    if not appt:
        return {"success": False,
                "message": "There is no confirmed booking in this chat to send."}
    try:
        from email_service import send_patient_confirmation
        ok = send_patient_confirmation(email, appt)
        return {"success": bool(ok),
                "message": "Confirmation email sent." if ok
                else "The email could not be sent. Please try again later."}
    except Exception as e:
        print(f"Email error: {e}")
        return {"success": False,
                "message": "The email could not be sent right now."}


_TOOLS = {
    "list_doctors": _tool_list_doctors,
    "get_doctor_schedule": _tool_get_doctor_schedule,
    "find_department_for_symptom": _tool_find_department_for_symptom,
    "check_availability": _tool_check_availability,
    "create_booking": _tool_create_booking,
    "cancel_booking": _tool_cancel_booking,
    "get_clinic_info": _tool_get_clinic_info,
    "send_confirmation_email": _tool_send_confirmation_email,
}

_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_doctors",
        "description": "List doctors, optionally in one department. Use when the patient asks who the doctors are, or which doctors a department has.",
        "parameters": {"type": "object", "properties": {
            "department": {"type": "string", "description": "Department name, e.g. Urology, ENT, Cardiology. Omit to list all doctors."}},
         "required": []}}},
    {"type": "function", "function": {
        "name": "get_doctor_schedule",
        "description": "Get one doctor's real chamber schedule (days + hours), department and specialization. Use when the patient asks about a specific doctor or their schedule.",
        "parameters": {"type": "object", "properties": {
            "doctor_name": {"type": "string", "description": "Doctor's name as the patient wrote it, e.g. 'dr ahamed hassan' or 'Ayesha Khan'."}},
         "required": ["doctor_name"]}}},
    {"type": "function", "function": {
        "name": "find_department_for_symptom",
        "description": "Find which department fits a symptom or health problem, and list its doctors. Use when the patient describes what is wrong with them.",
        "parameters": {"type": "object", "properties": {
            "symptom": {"type": "string", "description": "The patient's problem in their own words, e.g. 'amr jor' or 'I have a headache'."}},
         "required": ["symptom"]}}},
    {"type": "function", "function": {
        "name": "check_availability",
        "description": "Check whether a doctor is available on a day, and what the next serial number is. Respects the booking cutoff (a passed cutoff offers future days instead). Call this BEFORE telling the patient a doctor is available.",
        "parameters": {"type": "object", "properties": {
            "doctor_name": {"type": "string"},
            "day_or_date": {"type": "string", "description": "Weekday ('Friday'), a date ('2026-11-13'), 'today' or 'tomorrow'. Omit when the patient has not said a day yet."}},
         "required": ["doctor_name"]}}},
    {"type": "function", "function": {
        "name": "create_booking",
        "description": "Book an appointment — TWO steps, like a human receptionist. FIRST call WITHOUT confirmed: it only returns the booking summary (doctor, date, serial, estimated time) and creates NOTHING — show that summary to the patient, ask them to confirm, and ask for their email for the confirmation email. ONLY after the patient says yes, call AGAIN with confirmed=true (and the email if they gave one) — that second call is the one that creates the booking and sends the confirmation email. Never call with confirmed=true before showing the summary.",
        "parameters": {"type": "object", "properties": {
            "doctor_name": {"type": "string"},
            "day_or_date": {"type": "string"},
            "patient_name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string", "description": "Patient's email for the confirmation email. Omit it if they said no."},
            "confirmed": {"type": "boolean", "description": "true ONLY after the patient confirmed the summary. The first call must leave this out or false."}},
         "required": ["doctor_name", "day_or_date", "patient_name", "phone"]}}},
    {"type": "function", "function": {
        "name": "cancel_booking",
        "description": "Cancel a booking. The phone number the booking was made with is required. Doctor and day are optional — without them, the soonest upcoming booking for that phone is cancelled.",
        "parameters": {"type": "object", "properties": {
            "phone": {"type": "string"},
            "doctor_name": {"type": "string"},
            "day_or_date": {"type": "string"}},
         "required": ["phone"]}}},
    {"type": "function", "function": {
        "name": "get_clinic_info",
        "description": "Clinic name, address, phone, email, departments and all doctors. Use for general questions about the clinic.",
        "parameters": {"type": "object", "properties": {},
                       "required": []}}},
    {"type": "function", "function": {
        "name": "send_confirmation_email",
        "description": "Email the booking confirmation to the patient. Call only after a booking was confirmed in this chat and the patient gave an email address.",
        "parameters": {"type": "object", "properties": {
            "email": {"type": "string"}},
         "required": ["email"]}}},
]


# ------------------------------------------------------------------
# System prompt — built fresh per turn (doctor list never goes stale)
# ------------------------------------------------------------------
def _build_system_prompt():
    from clinic_data import get_all_doctors, get_departments

    today = datetime.now()
    by_dept = {}
    for doctor in get_all_doctors():
        by_dept.setdefault(doctor["department"], []).append(doctor["name"])
    dept_lines = "\n".join(
        f"  - {dept}: {', '.join(names)}"
        for dept, names in sorted(by_dept.items())
    )
    return f"""You are Jhon, a warm, friendly, professional AI receptionist at HealthCare Hospital in Dhaka, Bangladesh. You talk with patients and help them book appointments.

Today is {today.strftime('%A, %d %B %Y')}.

LANGUAGE — most important: reply in the SAME language the patient writes in.
- The patient writes Banglish (Bangla in Latin letters, e.g. "amr jor", "hea", "appointment nite cai") -> reply in Bangla (Bengali script), mixing in common English words (doctor, appointment, schedule, serial) the way a Bangladeshi receptionist texts.
- The patient writes English -> reply in English.
- The patient writes Bengali script -> reply in Bengali script.
- A short "yes"/"ok"/"hea" does NOT change the language — keep the language of the conversation so far.

HARD RULES — never break these:
1. NEVER state a fact that did not come from a tool result. Never invent a doctor's name, a schedule, a serial number, a date, a phone number, or a price. If you do not know something, call a tool or ask the patient — never guess.
2. You are NOT a doctor. Never diagnose, prescribe, or give medical advice. For symptoms, find the right department and offer to book.
3. BOOKING FLOW (two steps, never skip one): collect (a) the doctor, (b) the day, (c) the patient's name and phone number. Then call create_booking WITHOUT confirmed — it returns the booking summary (doctor, date, serial, estimated time) WITHOUT creating anything. Show that exact summary to the patient, ask them to confirm it, and ask for their email address for the confirmation email (they can say 'no' to skip it). ONLY after the patient says yes, call create_booking AGAIN with confirmed=true (and the email if they gave one) — that second call is the one that creates the booking and sends the confirmation email. After it succeeds, tell the patient the confirmed details (doctor, date, serial, estimated time, arrival window) and whether the confirmation email was sent. If the patient changes the doctor or the day in the middle, just use the new one; never start over, never ask for the name and phone again if you already have them.
4. If the patient says no, "thak", or wants to stop, thank them warmly and offer help later. Do not push.
5. If a booking cannot be made (doctor not available that day, serials full, cutoff passed), say so plainly and offer the next available days from the tool result — use the tool's next_days list with the exact dates it gives. If you previously mentioned a day and the tool now says it is unavailable, say sorry briefly and offer only the days the tool lists.
6. If the patient cancels and no booking exists for their phone, say clearly that no booking was found — never claim a booking was cancelled when it was not.
7. For emergencies (severe chest pain, cannot breathe, accident, heavy bleeding, unconsciousness), tell the patient to seek emergency care immediately and do not book a routine appointment.
8. Keep replies SHORT (2-4 sentences). Warm, human receptionist tone. No long paragraphs, no bullet-point essays unless the patient asked for a list.
9. DATES AND TIMES: copy them EXACTLY as the tool result gives them. Never invent a day, a date, or a time, and never add days to a list the tool gave you. If the tool lists only Saturday, offer only Saturday.
10. SCHEDULE ANSWERS: when the patient asks which days a doctor works, or names a doctor to book, list EVERY chamber day with its hours exactly as the tool gives them — never show only one day. If today's booking is closed, say so plainly (with the cutoff time from the tool) and offer the next available days. Never offer today as a booking day when the tool says today's booking is closed. Use only the doctor's real chamber days for examples too.
11. SCHEDULE FIRST: whenever the patient names a doctor — to book or just to ask — FIRST call get_doctor_schedule (or check_availability) and show the doctor's FULL schedule (every chamber day with exact hours and the next dates) BEFORE asking which day. Never ask 'which day?' without showing the schedule first — the patient cannot pick a day they cannot see.

DEPARTMENTS AND DOCTORS (real, from the database):
{dept_lines}

TOOLS: you have tools for listing doctors, schedules, symptom routing, availability, booking, cancelling, clinic info, and email confirmation. Use them — that is how you know the truth.
"""


# ------------------------------------------------------------------
# Agent loop
# ------------------------------------------------------------------
def _call_llm(messages, model):
    return _client.chat.completions.create(
        model=model,
        messages=messages,
        tools=_TOOL_SCHEMAS,
        tool_choice="auto",
        temperature=0.2,
        max_completion_tokens=600,
    )


def _run_agent(message, session):
    history = session.get("agent_history", [])
    messages = ([{"role": "system", "content": _build_system_prompt()}]
                + history
                + [{"role": "user", "content": message}])

    model = _MODEL_PRIMARY
    for round_no in range(_MAX_TOOL_ROUNDS):
        try:
            response = _call_llm(messages, model)
        except Exception as e:
            if model == _MODEL_PRIMARY:
                print(f"Agent primary model failed ({e}); "
                      f"falling back to {_MODEL_FALLBACK}")
                model = _MODEL_FALLBACK
                try:
                    response = _call_llm(messages, model)
                except Exception as e2:
                    print(f"Agent fallback model failed: {e2}")
                    raise
            else:
                raise
        assistant_msg = response.choices[0].message
        tool_calls = getattr(assistant_msg, "tool_calls", None)
        if not tool_calls:
            content = (assistant_msg.content or "").strip()
            if len(content) < 10:
                # Cut-off fragment — one retry, then the safe fallback.
                messages.append({"role": "assistant",
                                 "content": "That reply was cut off. Please answer again, completely."})
                continue
            return content
        messages.append(assistant_msg)
        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except Exception:
                args = {}
            fn = _TOOLS.get(name)
            if fn is None:
                result = {"error": f"Unknown tool: {name}"}
            else:
                try:
                    result = fn(args, session)
                except Exception as e:
                    print(f"Tool {name} error: {traceback.format_exc()}")
                    result = {"error": f"The {name} check failed: {e}"}
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
    # Too many tool rounds — say so honestly instead of looping.
    return None


def process_message(message, session):
    """Same signature as web_agent.process_message."""
    from messages import detect_lang

    # ---- Emergency — deterministic, never through the LLM ----
    _emergency_words = (
        "emergency", "accident", "durghotona", "unconscious", "oggan",
        "can't breathe", "cant breathe", "shash nite",
        "bleeding", "rokto ber", "heart attack", "stroke",
    )
    if any(w in message.lower() for w in _emergency_words):
        session["state"] = "idle"
        return ("This sounds like an emergency. Please go to the nearest "
                "emergency department or call emergency services now — "
                "do not wait for a routine appointment.")

    lang = detect_lang(message) or "en"
    try:
        reply = _run_agent(message, session)
    except Exception:
        print("Agent failed:")
        print(traceback.format_exc())
        reply = None
    if not reply:
        reply = (
            "দুঃখিত, একটু সমস্যা হয়েছে। আবার বলবেন কি?"
            if lang == "bn" else
            "Sorry, something went wrong on my side. Could you please say that again?"
        )

    # Keep a plain conversation history (no tool internals) so
    # follow-ups ("hea", "tar schedule bolo") stay understood.
    history = session.get("agent_history", [])
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    session["agent_history"] = history[-_MAX_HISTORY:]
    session["state"] = "idle"
    return reply
