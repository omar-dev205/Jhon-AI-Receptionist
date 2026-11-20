import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import json

import database


def _row_to_doctor_dict(row):
    """
    Convert a database row to a doctor dict (same format as
    the old CLINIC_DOCTORS list entries).
    """
    schedule_raw = row["schedule"]
    if schedule_raw:
        try:
            schedule = json.loads(schedule_raw)
        except (json.JSONDecodeError, TypeError):
            schedule = {}
    else:
        schedule = {}

    return {
        "name": row["name"],
        "department": row["department"],
        "specialization": row["specialization"] or "",
        "description": row["description"] or "",
        "average_patient_minutes": row["average_patient_minutes"] or 10,
        "max_patients_per_session": row["max_patients_per_session"] or 20,
        "schedule": schedule,
    }


CLINIC_DOCTORS = [
    {
        "name": "Dr. Sarah Ahmed",
        "department": "Cardiology",
        "specialization": "Interventional Cardiology",
        "description": (
            "Specializes in heart disease, chest pain, hypertension, "
            "cardiac rehabilitation, and preventive cardiac care."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 30,
        "schedule": {
            "Sunday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Monday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Friday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
    {
        "name": "Dr. Michael Carter",
        "department": "Cardiology",
        "specialization": "Cardiology",
        "description": (
            "Provides consultation for heart conditions, blood pressure, "
            "palpitations, and cardiovascular health."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 30,
        "schedule": {
            "Tuesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Thursday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
    {
        "name": "Dr. Karim Hossain",
        "department": "Ophthalmology",
        "specialization": "Ophthalmology",
        "description": (
            "Specializes in cataract treatment, glaucoma management, "
            "vision problems, and routine eye examinations."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 30,
        "schedule": {
            "Tuesday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Thursday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Saturday": {"start": "10:00 AM", "end": "01:00 PM"},
        },
    },
    {
        "name": "Dr. Emily Wilson",
        "department": "Ophthalmology",
        "specialization": "Ophthalmology",
        "description": (
            "Treats vision problems, eye infections, dry eyes, "
            "cataracts, and common eye conditions."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 25,
        "schedule": {
            "Monday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Wednesday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
    {
        "name": "Dr. Nadia Rahman",
        "department": "Dermatology",
        "specialization": "Dermatology",
        "description": (
            "Treats acne, skin allergies, eczema, pigmentation, "
            "hair problems, and common skin conditions."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 30,
        "schedule": {
            "Monday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Tuesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Thursday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
    {
        "name": "Dr. Robert Smith",
        "department": "Dermatology",
        "specialization": "Clinical Dermatology",
        "description": (
            "Specializes in skin diseases, allergies, acne, "
            "eczema, and hair and scalp conditions."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 25,
        "schedule": {
            "Wednesday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Saturday": {"start": "03:00 PM", "end": "07:00 PM"},
        },
    },
    {
        "name": "Dr. James Peterson",
        "department": "Orthopedics",
        "specialization": "Orthopedic Surgery",
        "description": (
            "Treats bone and joint problems, fractures, back pain, "
            "sports injuries, and joint-related conditions."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 30,
        "schedule": {
            "Wednesday": {"start": "05:00 PM", "end": "08:00 PM"},
            "Friday": {"start": "05:00 PM", "end": "08:00 PM"},
            "Saturday": {"start": "03:00 PM", "end": "07:00 PM"},
        },
    },
    {
        "name": "Dr. Daniel Brown",
        "department": "Orthopedics",
        "specialization": "Joint and Sports Medicine",
        "description": (
            "Treats knee pain, joint injuries, sports injuries, "
            "back pain, and musculoskeletal conditions."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 25,
        "schedule": {
            "Monday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Thursday": {"start": "05:00 PM", "end": "09:00 PM"},
        },
    },
    {
        "name": "Dr. Ayesha Khan",
        "department": "General Medicine",
        "specialization": "Internal Medicine",
        "description": (
            "Provides primary care for fever, diabetes, hypertension, "
            "general illnesses, and routine health consultations."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 35,
        "schedule": {
            "Monday": {"start": "11:00 AM", "end": "01:00 PM"},
            "Tuesday": {"start": "09:00 AM", "end": "01:00 PM"},
            "Wednesday": {"start": "09:00 AM", "end": "01:00 PM"},
            "Thursday": {"start": "09:00 AM", "end": "01:00 PM"},
            "Friday": {"start": "09:00 AM", "end": "01:00 PM"},
            "Saturday": {"start": "08:50 AM", "end": "01:00 PM"},
        },
    },
    {
        "name": "Dr. David Wilson",
        "department": "Neurology",
        "specialization": "Neurology",
        "description": (
            "Evaluates headaches, migraines, dizziness, nerve problems, "
            "and other neurological conditions."
        ),
        "average_patient_minutes": 10,
        "max_patients_per_session": 20,
        "schedule": {
            "Monday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Thursday": {"start": "05:00 PM", "end": "09:00 PM"},
        },
    },
    {
        "name": "Dr. Sophia Ahmed",
        "department": "Neurology",
        "specialization": "Clinical Neurology",
        "description": (
            "Specializes in migraine, neurological disorders, "
            "memory problems, and nerve-related conditions."
        ),
        "average_patient_minutes": 10,
        "max_patients_per_session": 20,
        "schedule": {
            "Tuesday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Saturday": {"start": "10:00 AM", "end": "02:00 PM"},
        },
    },
    {
        "name": "Dr. Ahmed Hassan",
        "department": "ENT",
        "specialization": "Ear, Nose and Throat",
        "description": (
            "Treats ear infections, sinus problems, sore throat, "
            "hearing problems, and other ENT conditions."
        ),
        "average_patient_minutes": 7,
        "max_patients_per_session": 30,
        "schedule": {
            "Monday": {"start": "05:00 PM", "end": "09:00 PM"},
            "Wednesday": {"start": "05:00 PM", "end": "09:00 PM"},
        },
    },
    {
        "name": "Dr. Olivia Martin",
        "department": "Gastroenterology",
        "specialization": "Gastroenterology",
        "description": (
            "Treats stomach pain, acid reflux, digestive problems, "
            "gastric conditions, and intestinal disorders."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 25,
        "schedule": {
            "Tuesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Friday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
    {
        "name": "Dr. Maria Khan",
        "department": "Gynecology",
        "specialization": "Obstetrics and Gynecology",
        "description": (
            "Provides women's health consultations, reproductive care, "
            "pregnancy-related care, and gynecological services."
        ),
        "average_patient_minutes": 10,
        "max_patients_per_session": 20,
        "schedule": {
            "Monday": {"start": "03:00 PM", "end": "07:00 PM"},
            "Wednesday": {"start": "03:00 PM", "end": "07:00 PM"},
            "Saturday": {"start": "10:00 AM", "end": "02:00 PM"},
        },
    },
    {
        "name": "Dr. William Taylor",
        "department": "Pediatrics",
        "specialization": "Pediatrics",
        "description": (
            "Provides medical care for infants, children, and teenagers, "
            "including common childhood illnesses."
        ),
        "average_patient_minutes": 8,
        "max_patients_per_session": 30,
        "schedule": {
            "Tuesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Thursday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Saturday": {"start": "09:00 AM", "end": "01:00 PM"},
        },
    },

    # Dental
    {
        "name": "Dr. Ryan Miller",
        "department": "Dentistry",
        "specialization": "General Dentistry",
        "description": (
            "Treats tooth pain, gum problems, cavities, dental infections, "
            "and provides routine dental care."
        ),
        "average_patient_minutes": 10,
        "max_patients_per_session": 24,
        "schedule": {
            "Monday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Wednesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Saturday": {"start": "10:00 AM", "end": "02:00 PM"},
        },
    },
    {
        "name": "Dr. Jessica Brown",
        "department": "Dentistry",
        "specialization": "Dental Surgery",
        "description": (
            "Provides treatment for gum disease, tooth infection, "
            "dental pain, and minor oral surgery."
        ),
        "average_patient_minutes": 12,
        "max_patients_per_session": 20,
        "schedule": {
            "Tuesday": {"start": "04:00 PM", "end": "08:00 PM"},
            "Thursday": {"start": "04:00 PM", "end": "08:00 PM"},
        },
    },
        # Urology
    {
        "name": "Dr. Rana",
        "department": "Urology",
        "specialization": "Urology",
        "description": (
            "Treats urinary tract problems, kidney stones, "
            "bladder issues, and prostate conditions."
        ),
        "average_patient_minutes": 10,
        "max_patients_per_session": 20,
        "schedule": {
            "Friday": {"start": "05:45 PM", "end": "09:00 PM"},
        },
    },
    {
            "name": "Dr. Mina",
            "department": "Urology",
            "specialization": "Urology",
            "description": (
                "Treats urinary tract problems, kidney stones, "
                "bladder issues, and prostate conditions."
            ),
            "average_patient_minutes": 10,
            "max_patients_per_session": 20,
            "schedule": {
                "Monday": {"start": "05:45 PM", "end": "09:00 PM"},
            },
        },
]

def get_all_doctors():
    """
    Returns ALL active doctors from database.
    Use this instead of CLINIC_DOCTORS where possible.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM doctors WHERE active = 1 ORDER BY name"
            )
            rows = cursor.fetchall()
    except Exception:
        rows = []

    if not rows:
        # Database not seeded yet (run python migrate_doctors.py) —
        # use the built-in list so the agent still works.
        return list(CLINIC_DOCTORS)

    return [_row_to_doctor_dict(row) for row in rows]


def _normalize_name(s):
    """Lowercase, drop titles/punctuation/spaces: 'Dr. Ayesha Khan'
    -> 'ayeshakhan'. Lets patients write 'ayshakhan' (no space,
    missing letter) and still match."""
    s = s.lower().strip()
    s = re.sub(r"^(dr\.?|doctor)\s+", "", s)  # leading title, with or without dot
    return re.sub(r"[^a-z0-9]", "", s)


def find_doctor_by_name(name):
    """
    Find a doctor by name (fuzzy match).
    Reads from database; falls back to the built-in list.
    Space-insensitive: 'ayshakhan' matches 'Ayesha Khan'.
    """
    query = name.lower().strip()
    query_norm = _normalize_name(query)

    try:
        with database.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM doctors WHERE active = 1"
            )
            rows = cursor.fetchall()
        candidates = [_row_to_doctor_dict(r) for r in rows]
    except Exception:
        candidates = []

    if not candidates:
        candidates = list(CLINIC_DOCTORS)

    # 1) exact / substring on the plain name
    for doctor in candidates:
        if query in doctor["name"].lower():
            return doctor
    # 2) space-insensitive: 'ayshakhan' vs 'ayeshakhan'
    for doctor in candidates:
        if query_norm and query_norm in _normalize_name(doctor["name"]):
            return doctor
    # 3) fuzzy on the space-free names (handles a missing letter)
    best, best_score = None, 0.0
    for doctor in candidates:
        score = SequenceMatcher(
            None, query_norm, _normalize_name(doctor["name"])
        ).ratio()
        if score > best_score:
            best, best_score = doctor, score
    if best_score >= 0.85:
        return best
    return None


def find_doctors_by_department(department):
    """
    Find active doctors in a department.
    Reads from database.
    """
    query = department.lower().strip()

    with database.get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM doctors WHERE active = 1 AND LOWER(department) = ?",
            (query,),
        )
        rows = cursor.fetchall()

    return [_row_to_doctor_dict(row) for row in rows]
def get_departments():
    """
    Return list of all active departments.
    Reads from database: departments created by the admin (even
    with no doctor yet) UNION the departments doctors belong to,
    so a brand-new department shows up on the website at once.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.execute(
                "SELECT name AS department FROM departments WHERE active = 1 "
                "UNION "
                "SELECT DISTINCT department FROM doctors WHERE active = 1 "
                "ORDER BY 1"
            )
            return [row["department"] for row in cursor.fetchall()]
    except Exception:
        # Old database without the departments table — fall back
        # to the departments doctors belong to.
        with database.get_connection() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT department FROM doctors WHERE active = 1 "
                "ORDER BY department"
            )
            return [row["department"] for row in cursor.fetchall()]


def get_doctor_schedule(doctor_name):
    doctor = find_doctor_by_name(doctor_name)

    if not doctor:
        return None

    return {
        "name": doctor["name"],
        "department": doctor["department"],
        "specialization": doctor["specialization"],
        "appointment_duration": doctor["average_patient_minutes"],
        "max_patients_per_session": doctor["max_patients_per_session"],
        "schedule": doctor["schedule"],
    }

def works_on_day(doctor_name, day):
    """
    Check if doctor works on a specific day.
    Reads from database.
    """
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return False

    day_name = day.strip().title()
    return day_name in doctor.get("schedule", {})


def get_chamber_hours(doctor_name, day):
    """
    Get chamber hours for a doctor on a specific day.
    Reads from database.
    """
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return None

    day_name = day.strip().title()
    return doctor.get("schedule", {}).get(day_name)


def generate_time_slots(start_time, end_time, duration):
    start = datetime.strptime(start_time, "%I:%M %p")
    end = datetime.strptime(end_time, "%I:%M %p")

    slots = []
    current = start

    while current + timedelta(minutes=duration) <= end:
        slots.append(current.strftime("%I:%M %p"))
        current += timedelta(minutes=duration)

    return slots


def get_available_slots(doctor_name, day=None):
    doctor = find_doctor_by_name(doctor_name)

    if not doctor:
        return None

    if day:
        day_name = day.strip().title()
        schedule = doctor["schedule"].get(day_name)

        if not schedule:
            return []

        return generate_time_slots(
            schedule["start"],
            schedule["end"],
            doctor["average_patient_minutes"],
        )

    all_slots = {}

    for day_name, schedule in doctor["schedule"].items():
        all_slots[day_name] = generate_time_slots(
            schedule["start"],
            schedule["end"],
            doctor["average_patient_minutes"],
        )

    return all_slots