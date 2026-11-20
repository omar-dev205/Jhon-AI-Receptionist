"""
Persistent storage for booked appointments using SQLite.

Why SQLite: it's built into Python (no extra install needed, works
the same on Windows/Linux), stores everything in a single file
(clinic_bookings.db) next to your code, and survives bot restarts —
unlike the old in-memory Python list which reset to empty every
time the bot process restarted.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "clinic_bookings.db",
)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
def init_db():
    with get_connection() as conn:
        # Existing appointments table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_name TEXT NOT NULL,
                department TEXT,
                date TEXT NOT NULL,
                day TEXT NOT NULL,
                serial_number INTEGER NOT NULL,
                patient_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                estimated_time TEXT,
                arrival_window TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # NEW: doctors table — dynamic, admin-editable
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                department TEXT NOT NULL,
                specialization TEXT,
                description TEXT,
                average_patient_minutes INTEGER DEFAULT 10,
                max_patients_per_session INTEGER DEFAULT 20,
                schedule TEXT,  -- JSON: {"Monday": {"start": "09:00 AM", "end": "01:00 PM"}}
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # NEW: departments table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                symptoms TEXT,  -- JSON array of keywords
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Existing indexes
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_doctor_date
            ON appointments (doctor_name, date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_department_date
            ON appointments (department, date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_doctors_active
            ON doctors (active)
            """
        )

        # Migration for old databases (missing department column)
        try:
            conn.execute("ALTER TABLE appointments ADD COLUMN department TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        conn.commit()


def get_next_serial_number(doctor_name, date, day):
    """
    Count existing bookings for this doctor on this exact date.
    Because `date` is a specific calendar date (e.g. "2026-09-15"),
    not just a weekday name, every occurrence of "Friday" already
    gets its own fresh count automatically — no separate reset
    logic is needed for that part.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) AS count FROM appointments "
            "WHERE doctor_name = ? AND date = ? AND day = ?",
            (doctor_name, date, day),
        )
        return cursor.fetchone()["count"] + 1


def save_appointment(appointment):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO appointments
                (doctor_name, department, date, day, serial_number,
                 patient_name, phone, estimated_time, arrival_window)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                appointment["doctor_name"],
                appointment.get("department", ""),
                appointment["date"],
                appointment["day"],
                appointment["serial_number"],
                appointment["patient_name"],
                appointment["phone"],
                appointment["estimated_time"],
                appointment["arrival_window"],
            ),
        )
        conn.commit()


def _digits_only(phone):
    return "".join(ch for ch in str(phone) if ch.isdigit())


def get_upcoming_appointments_for_phone(phone, today_str):
    """Upcoming appointments for a phone number.

    Numbers are compared by their last 10 digits, so +880 / 0 /
    spaces / dashes all match the same patient.
    """
    if not phone:
        return []
    wanted = _digits_only(phone)[-10:]
    if not wanted:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE date >= ? "
            "ORDER BY date, serial_number",
            (today_str,),
        ).fetchall()
    out = []
    for row in rows:
        if _digits_only(row["phone"])[-10:] == wanted:
            out.append(dict(row))
    return out


def cancel_appointment_by_details(phone, doctor_name, date):
    """Delete the appointment for (phone, doctor, date).

    Returns True when one was deleted, False otherwise.
    """
    if not phone or not doctor_name or not date:
        return False
    wanted = _digits_only(phone)[-10:]
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, phone FROM appointments "
            "WHERE doctor_name = ? AND date = ?",
            (doctor_name, str(date)),
        ).fetchall()
        for row in rows:
            if _digits_only(row["phone"])[-10:] == wanted:
                conn.execute(
                    "DELETE FROM appointments WHERE id = ?", (row["id"],)
                )
                conn.commit()
                return True
    return False


def get_all_appointments():
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM appointments ORDER BY date, doctor_name, serial_number"
        )
        return [dict(row) for row in cursor.fetchall()]


def get_appointments_for_date(date_str, doctor_name=None, department=None):
    """
    Returns the booking list ("sheet") for a given date, optionally
    narrowed to one doctor or one department — this is what powers
    the printable per-doctor / per-department report.
    """
    query = "SELECT * FROM appointments WHERE date = ?"
    params = [date_str]

    if doctor_name:
        query += " AND doctor_name = ?"
        params.append(doctor_name)

    if department:
        query += " AND department = ?"
        params.append(department)

    query += " ORDER BY department, doctor_name, serial_number"

    with get_connection() as conn:
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_doctors_with_bookings(date_str, department=None):
    """
    Returns the distinct doctor names that have at least one booking
    on this date (optionally scoped to one department). Used to
    split a department/date report into one sheet per doctor.
    """
    query = "SELECT DISTINCT doctor_name FROM appointments WHERE date = ?"
    params = [date_str]

    if department:
        query += " AND department = ?"
        params.append(department)

    query += " ORDER BY doctor_name"

    with get_connection() as conn:
        cursor = conn.execute(query, params)
        return [row["doctor_name"] for row in cursor.fetchall()]


def export_appointments_to_csv(date_str, filepath, doctor_name=None, department=None):
    """
    Writes the appointment sheet for `date_str` (optionally filtered
    by doctor/department) to a CSV file at `filepath`. CSV opens
    directly in Excel/Google Sheets, so the clinic can print it like
    a normal daily register sheet.

    Returns the number of rows written.
    """
    import csv

    rows = get_appointments_for_date(date_str, doctor_name=doctor_name, department=department)

    with open(filepath, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([
            "Date", "Department", "Doctor", "Serial",
            "Patient Name", "Phone", "Estimated Time", "Arrival Window",
        ])
        for row in rows:
            writer.writerow([
                row["date"], row["department"], row["doctor_name"],
                row["serial_number"], row["patient_name"], row["phone"],
                row["estimated_time"], row["arrival_window"],
            ])

    return len(rows)


def cleanup_past_appointments(today_str):
    """
    Deletes appointments whose date has already passed.

    This is the "schedule shesh hole purfigures remove" part you
    asked about: once a booked date is in the past, there's no
    reason to keep it taking up space, so it's deleted. (Serial
    numbers for *future* dates were never affected by old dates in
    the first place, since counting is scoped to the exact date —
    this cleanup is purely about not letting the database grow
    forever.)

    Returns how many rows were deleted (useful for a log line).
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM appointments WHERE date < ?",
            (today_str,),
        )
        conn.commit()
        return cursor.rowcount


init_db()
