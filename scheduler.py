"""
Per-doctor cutoff scheduler.

For every doctor's chamber on every day:
  - Compute the cutoff time (chamber start - 30 min)
  - At that time (or within the last hour of it), send the
    doctor's booking report via reports.send_doctor_cutoff_report.

Runs as a background thread inside the Flask process.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from clinic_data import CLINIC_DOCTORS
from clinic_config import is_serial_mode
from reports import send_doctor_cutoff_report


# How many minutes before chamber start to fire the report
CUTOFF_MINUTES_BEFORE_START = 30

# How many seconds after the cutoff time we still fire the report
# (so a server restart shortly after cutoff doesn't miss it).
FIRE_WINDOW_SECONDS = 3600  # 1 hour

# Persist fired cutoff reports to disk so a server restart inside the
# 1-hour fire window does NOT resend the same report. The old code
# kept this only in memory, so every restart re-sent the email.
_FIRED_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cutoff_fired.json"
)


def _load_fired():
    """Load already-sent report keys; keep only today's keys."""
    try:
        with open(_FIRED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            today = datetime.now().strftime("%Y-%m-%d")
            return {
                str(k): True
                for k in data
                if str(k).startswith(today + "|")
            }
    except Exception:
        pass
    return {}


def _save_fired():
    """Save the fired-report keys to disk (best-effort)."""
    try:
        with open(_FIRED_FILE, "w", encoding="utf-8") as f:
            json.dump(_last_fired, f)
    except Exception as e:
        print(f"[scheduler] Could not save fired reports: {e}")


_scheduler_started = False
_last_fired = _load_fired()  # {key: True} - prevents double-firing
_last_cleanup_date = None  # the date we last auto-cleaned past bookings


def _cutoff_datetime_for(doctor, day_name, ref_dt):
    """
    Returns the datetime at which this doctor's cutoff fires
    for the given day_name (on ref_dt's date).
    Returns None if this doctor does not work on that day.
    """
    schedule = doctor["schedule"].get(day_name)
    if not schedule:
        return None

    try:
        start_dt = datetime.strptime(schedule["start"], "%I:%M %p")
    except Exception:
        return None

    cutoff_dt = start_dt - timedelta(minutes=CUTOFF_MINUTES_BEFORE_START)

    return ref_dt.replace(
        hour=cutoff_dt.hour,
        minute=cutoff_dt.minute,
        second=0,
        microsecond=0,
    )


def _fire_due_reports():
    """
    Check all doctors for today. If any cutoff time has just
    passed (within FIRE_WINDOW_SECONDS) and we haven't fired it
    yet for this (date, doctor, cutoff) tuple, fire it now.
    """
    now = datetime.now()
    today = now.date()
    day_name = now.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")

    for doctor in CLINIC_DOCTORS:
        cutoff_dt = _cutoff_datetime_for(doctor, day_name, now)
        if cutoff_dt is None:
            continue

        # How many seconds since cutoff time?
        delta = (now - cutoff_dt).total_seconds()

        # Fire only if we're at or past cutoff AND within window
        if not (0 <= delta <= FIRE_WINDOW_SECONDS):
            continue

        # Unique key: prevent firing twice for the same day + doctor + time
        # (string key so it can be saved to cutoff_fired.json)
        fire_key = "|".join(
            [date_str, doctor["name"], cutoff_dt.strftime("%H:%M")]
        )
        if fire_key in _last_fired:
            continue

        _last_fired[fire_key] = True
        _save_fired()

        try:
            print(
                f"[scheduler] Firing cutoff report for "
                f"{doctor['name']} ({date_str})"
            )
            sent = send_doctor_cutoff_report(date_str, doctor["name"])
            if sent:
                # The CSV is in the admin's email — tell them on
                # Telegram too, with the doctor's name + sheet link,
                # so they check the email right away.
                try:
                    _notify_admin_cutoff(date_str, doctor)
                except Exception as e:
                    print(f"[scheduler] Telegram notify failed: {e}")
        except Exception as e:
            print(
                f"[scheduler] Report failed for "
                f"{doctor['name']}: {e}"
            )


def _notify_admin_cutoff(date_str, doctor):
    """Telegram message to the admin right after the cutoff email
    goes out: doctor's name, booking count, 'check your email',
    plus the Google Sheet link. Never breaks the scheduler —
    the caller already guards it, and this is best-effort.
    """
    import json
    import urllib.request

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_ids = [
        x.strip()
        for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
        if x.strip()
    ]
    if not token or not admin_ids:
        return

    try:
        import database

        rows = database.get_appointments_for_date(
            date_str, doctor_name=doctor["name"]
        )
    except Exception:
        rows = []

    # Admin message in English (the clinic's working language for
    # reports): doctor's name, booking count, 'check your email',
    # plus the Google Sheet link.
    lines = [
        f"\U0001f4cb Booking report for {doctor['name']} has been emailed.",
        f"\U0001f4c5 {date_str} \u2022 Total bookings: {len(rows)}",
        "\U0001f4e7 Please check your email \u2014 the CSV is attached.",
    ]
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if sheet_id:
        lines.append(
            "\U0001f4ca Sheet: "
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        )
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": admin_ids[0], "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)


def _maybe_cleanup_past_bookings(today_str=None):
    """Auto-clean: delete past bookings once a day, right after
    midnight, with no restart and no booking needed.

    Example: Dr. Rana's Friday 13 Nov chamber — on 14 Nov the
    13th's bookings are gone from the database. The durable
    record lives in the emailed CSV + Google Sheet, so nothing
    is lost. Idempotent: runs at most once per date.
    """
    global _last_cleanup_date
    if today_str is None:
        today_str = datetime.now().date().strftime("%Y-%m-%d")
    if _last_cleanup_date == today_str:
        return 0
    _last_cleanup_date = today_str
    try:
        import database

        removed = database.cleanup_past_appointments(today_str)
        if removed:
            print(
                f"[scheduler] Auto-cleaned {removed} past "
                f"appointment(s) (before {today_str})"
            )
        return removed
    except Exception as e:
        print(f"[scheduler] Auto-cleanup failed: {e}")
        return 0


def _loop():
    while True:
        try:
            _maybe_cleanup_past_bookings()
            _fire_due_reports()
        except Exception as e:
            print(f"[scheduler] loop error: {e}")
        # Check every 30 seconds — precise enough
        time.sleep(30)


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    if not is_serial_mode():
        print("[scheduler] Not in serial mode — skipping")
        return

    # Clean once right now too (a restart cleans immediately,
    # same as the Telegram bot does at startup).
    _maybe_cleanup_past_bookings()

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    print(
        f"[scheduler] Per-doctor cutoff scheduler started "
        f"(fires at chamber_start - {CUTOFF_MINUTES_BEFORE_START} min)"
    )