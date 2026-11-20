"""
Notification system for HealthCare Hospital.

"""

import os
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import smtplib

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------
# EMAIL CONFIG
# ---------------------------------------------------------
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")

NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")
RECEPTIONIST_EMAIL = os.getenv("RECEPTIONIST_EMAIL")

EMAIL_ENABLED = bool(EMAIL_ADDRESS and EMAIL_APP_PASSWORD and NOTIFY_EMAIL_TO)


def _recipient_list():
    """Returns a list of emails to send reports to."""
    recipients = []
    if NOTIFY_EMAIL_TO:
        recipients.append(NOTIFY_EMAIL_TO.strip())
    if RECEPTIONIST_EMAIL and RECEPTIONIST_EMAIL.strip() not in recipients:
        recipients.append(RECEPTIONIST_EMAIL.strip())
    return recipients


# ---------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

GOOGLE_SHEETS_ENABLED = bool(
    GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_ID
)

_spreadsheet = None

SHEET_HEADER = [
    "Date", "Day", "Serial", "Patient Name", "Phone",
    "Estimated Time", "Arrival Window", "Booked At",
]


def _get_spreadsheet():
    global _spreadsheet

    if not GOOGLE_SHEETS_ENABLED:
        return None

    if _spreadsheet is not None:
        return _spreadsheet

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
        )
        client = gspread.authorize(creds)
        _spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        return _spreadsheet
    except Exception:
        print("Google Sheets connection error:")
        print(traceback.format_exc())
        return None


def get_spreadsheet():
    """Public wrapper for reports.py."""
    return _get_spreadsheet()


def sync_to_google_sheets(appointment):
    """
    Appends one booking to its per-doctor tab. Called from
    web_agent / telegram on every confirmed booking.
    """
    spreadsheet = _get_spreadsheet()
    if spreadsheet is None:
        return False

    tab_name = (
        f"{appointment.get('department', 'General')} - "
        f"{appointment['doctor_name']}"
    )
    tab_name = tab_name[:100]

    try:
        try:
            worksheet = spreadsheet.worksheet(tab_name)
        except Exception:
            worksheet = spreadsheet.add_worksheet(
                title=tab_name, rows=1000, cols=len(SHEET_HEADER)
            )
            worksheet.append_row(SHEET_HEADER)

        worksheet.append_row([
            appointment["date"],
            appointment["day"],
            appointment["serial_number"],
            appointment["patient_name"],
            appointment["phone"],
            appointment["estimated_time"],
            appointment["arrival_window"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ])
        return True
    except Exception:
        print("Google Sheets sync error:")
        print(traceback.format_exc())
        return False


# ---------------------------------------------------------
# CUTOFF REPORT EMAIL (the main one now)
# ---------------------------------------------------------
def send_cutoff_report_email(
    doctor_name,
    date_str,
    summary_text,
    csv_path=None,
):
    """
    Sends ONE email per doctor at their chamber-cutoff time,
    containing:
      - a human-readable patient list (summary_text)
      - optional CSV attachment (for printing)

    Recipients: NOTIFY_EMAIL_TO + RECEPTIONIST_EMAIL.

    Returns True on success, False on failure.
    """
    if not EMAIL_ENABLED:
        return False

    recipients = _recipient_list()
    if not recipients:
        return False

    subject = (
        f"Today's Booking Report — {doctor_name} — {date_str}"
    )

    # Build email body
    body = (
        f"HealthCare Hospital — Daily Booking Report\n"
        f"Doctor: {doctor_name}\n"
        f"Date: {date_str}\n"
        f"Report generated at chamber-cutoff time.\n"
        f"{'=' * 55}\n\n"
        f"{summary_text}\n\n"
        f"{'=' * 55}\n"
        f"CSV attached (if bookings exist) for printing.\n"
        f"Google Sheet is updated live.\n\n"
        f"—\n"
        f"Automated message. Please do not reply.\n"
    )

    message = MIMEMultipart()
    message["From"] = EMAIL_ADDRESS
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    # Attach CSV if provided
    if csv_path and os.path.exists(csv_path):
        try:
            with open(csv_path, "rb") as f:
                csv_data = f.read()
            attachment = MIMEApplication(csv_data, _subtype="csv")
            filename = os.path.basename(csv_path)
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=filename,
            )
            message.attach(attachment)
        except Exception:
            print("CSV attach error:")
            print(traceback.format_exc())

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(message, to_addrs=recipients)
        print(
            f"[cutoff-report] Sent {doctor_name} report to "
            f"{recipients}"
        )
        return True
    except Exception:
        print("Cutoff report email error:")
        print(traceback.format_exc())
        return False


# ---------------------------------------------------------
# PATIENT CONFIRMATION EMAIL (unchanged)
# ---------------------------------------------------------
def send_patient_confirmation_email(patient_email, appointment):
    """
    Confirmation email sent to a PATIENT (not admin).
    Currently called by email_service.py — this is a thin wrapper
    so the logic lives in one place if needed.
    """
    from email_service import send_patient_confirmation
    return send_patient_confirmation(patient_email, appointment)


# ---------------------------------------------------------
# PUBLIC HOOKS (used by web_agent / main)
# ---------------------------------------------------------
def notify_new_booking(appointment):
    """
    Called on every confirmed booking.
    Now does ONLY the Google Sheet sync — no per-booking email.
    The cutoff report (sent later by the scheduler) is what the
    admin actually reads.
    """
    sync_to_google_sheets(appointment)