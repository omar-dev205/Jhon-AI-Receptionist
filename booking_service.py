"""
Booking validation 

Business rules:
  1. Cannot book a past date.
  2. Same-day booking STOPS 30 minutes before chamber START.
     (e.g., chamber starts 9 AM -> last booking at 8:30 AM)
     This gives the clinic time to print the serial sheet
     and hand it to the doctor BEFORE chamber begins.
  3. Serial-number limit (max_patients_per_session) is enforced
     separately in availability.py — that limit applies to ALL
     days (today and future dates alike).
  4. Cannot book more than 30 days in advance.
"""

from datetime import datetime, timedelta

from availability import (
    check_serial_availability,
    book_serial,
)

from clinic_config import (
    get_booking_mode,
    is_serial_mode,
)

from clinic_data import get_chamber_hours, find_doctor_by_name
from messages import t


ADVANCE_BOOKING_DAYS = 30
SAME_DAY_BOOKING = True

# Booking stops this many minutes BEFORE chamber start time.
# Applies ONLY to same-day bookings; future-date bookings are
# not affected by this cutoff.
CUTOFF_MINUTES_BEFORE_START = 30


def _booking_window_closed_today(doctor_name, day):
    """
    Returns a dict describing why today's booking is closed,
    or None if same-day booking is still open.

    Returns:
      None                                 -> booking still open
      {"reason": "before_start", ...}      -> within cutoff window
      {"reason": "after_start", ...}       -> chamber started/ended
    """
    chamber = get_chamber_hours(doctor_name, day)
    if not chamber:
        return None

    try:
        now = datetime.now().time()

        chamber_start = datetime.strptime(
            chamber["start"], "%I:%M %p"
        ).time()
        chamber_end = datetime.strptime(
            chamber["end"], "%I:%M %p"
        ).time()

        # Cutoff = chamber start minus CUTOFF_MINUTES_BEFORE_START
        start_dt = datetime.strptime(chamber["start"], "%I:%M %p")
        cutoff_dt = start_dt - timedelta(
            minutes=CUTOFF_MINUTES_BEFORE_START
        )
        cutoff_time = cutoff_dt.time()

        # Case A: inside the cutoff window (close to start)
        if cutoff_time <= now < chamber_start:
            return {
                "reason": "before_start",
                "chamber": chamber,
                "cutoff": cutoff_dt.strftime("%I:%M %p"),
            }

        # Case B: chamber already started or ended
        if now >= chamber_start:
            return {
                "reason": "after_start",
                "chamber": chamber,
            }

        # Otherwise booking still open
        return None

    except Exception:
        return None


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]


def get_next_available_days(doctor_name, today=None):
    """
    Next occurrence of EACH chamber day, strictly AFTER `today`,
    with the real calendar date and hours.

    Returns a list sorted by date:
      [{"day": "Saturday", "date": "2026-11-14",
        "date_display": "Saturday, 14 November 2026",
        "start": "12:40 PM", "end": "02:00 PM"}, ...]

    Why strictly after today: this is used when today's booking is
    already closed, so today's occurrence must NOT be offered again.
    A Saturday-only doctor asked on a Saturday therefore returns
    NEXT Saturday — never an empty list while the doctor still has
    chamber days. (The old code skipped today without rolling it
    forward to next week, so it returned an empty list for a
    single-day doctor — and the LLM filled the gap by inventing
    days. That is the 2026-11-07 Sufan bug.)
    """
    if today is None:
        today = datetime.now().date()
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return []
    out = []
    for day_name, hours in (doctor.get("schedule") or {}).items():
        if day_name not in _WEEKDAYS:
            continue
        target = _WEEKDAYS.index(day_name)
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7  # today is closed -> next week's occurrence
        d = today + timedelta(days=delta)
        out.append({
            "day": day_name,
            "date": d.strftime("%Y-%m-%d"),
            "date_display": d.strftime("%A, %d %B %Y"),
            "start": (hours or {}).get("start", ""),
            "end": (hours or {}).get("end", ""),
        })
    out.sort(key=lambda x: x["date"])
    return out


def _format_closed_message(doctor_name, day, closed_info):
    next_days = get_next_available_days(doctor_name)
    if next_days:
        available_text = "\n".join(
            f"- {d['date_display']}: {d['start']} - {d['end']}"
            for d in next_days
        )
        days_tail = (
            f"Dr. {doctor_name.split()[-1]}'s available days:\n"
            f"{available_text}\n\n"
            f"Which day would you like to book instead?"
        )
    else:
        days_tail = "Which day would you like to book instead?"

    if closed_info["reason"] == "before_start":
        chamber = closed_info["chamber"]
        return (
            f"Sorry, today's online booking for {doctor_name} "
            f"has already closed.\n\n"
            f"Chamber starts at {chamber['start']} — online booking "
            f"stops {CUTOFF_MINUTES_BEFORE_START} minutes before start "
            f"(last booking was at {closed_info['cutoff']}).\n\n"
            f"{days_tail}"
        )

    if closed_info["reason"] == "after_start":
        chamber = closed_info["chamber"]
        return (
            f"Sorry, {doctor_name}'s chamber for today has already "
            f"started ({chamber['start']}) or ended "
            f"({chamber['end']}).\n\n"
            f"{days_tail}"
        )

    return "Booking is not available today. Please try another day."


def validate_booking_date(date, today, lang="en",
                          doctor_name=None, day=None):
    """Validates a booking date."""

    # 1. Past date
    if date < today:
        return {
            "valid": False,
            "message": t("past_date", lang),
        }

    # 2. Same-day: is the booking window still open?
    #    NOTE: this check runs ONLY when date == today.
    #    Future dates are not affected by the cutoff.
    if date == today and doctor_name and day:
        closed = _booking_window_closed_today(doctor_name, day)
        if closed:
            return {
                "valid": False,
                "message": _format_closed_message(
                    doctor_name, day, closed
                ),
            }

    # 3. Same-day booking disabled entirely (config toggle)
    if date == today and not SAME_DAY_BOOKING:
        return {
            "valid": False,
            "message": t("same_day_unavailable", lang),
        }

    # 4. Advance limit
    days_ahead = (date - today).days
    if days_ahead > ADVANCE_BOOKING_DAYS:
        return {
            "valid": False,
            "message": t(
                "advance_limit", lang, days=ADVANCE_BOOKING_DAYS
            ),
        }

    return {
        "valid": True,
        "message": "Booking date is valid.",
    }


def check_booking(doctor_name, date, day, today, lang="en"):
    """
    Check if a booking is possible for the given doctor/date/day.
    The serial-number limit is enforced inside
    check_serial_availability() (availability.py).
    """
    date_validation = validate_booking_date(
        date, today,
        lang=lang,
        doctor_name=doctor_name,
        day=day,
    )
    if not date_validation["valid"]:
        return date_validation

    if is_serial_mode():
        return check_serial_availability(
            doctor_name,
            date.strftime("%Y-%m-%d"),
            day,
            lang=lang,
        )

    return {
        "available": False,
        "message": t(
            "booking_mode_not_implemented",
            lang,
            mode=get_booking_mode(),
        ),
    }


def create_booking(doctor_name, date, day, patient_name, phone,
                   today, lang="en"):
    """Create a booking after validating the date."""
    date_validation = validate_booking_date(
        date, today,
        lang=lang,
        doctor_name=doctor_name,
        day=day,
    )
    if not date_validation["valid"]:
        return {
            "success": False,
            "message": date_validation["message"],
        }

    if is_serial_mode():
        return book_serial(
            doctor_name=doctor_name,
            date=date.strftime("%Y-%m-%d"),
            day=day,
            patient_name=patient_name,
            phone=phone,
            lang=lang,
        )

    return {
        "success": False,
        "message": t(
            "booking_mode_not_implemented",
            lang,
            mode=get_booking_mode(),
        ),
    }