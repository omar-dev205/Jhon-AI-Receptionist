"""
Patient-facing confirmation email.

Separate from notifications.py (which emails the clinic admin)
— this one sends a booking confirmation TO THE PATIENT when they
provide their email during the chat/voice booking flow.
"""

import os
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import smtplib

from dotenv import load_dotenv

load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")

EMAIL_ENABLED = bool(EMAIL_ADDRESS and EMAIL_APP_PASSWORD)


def send_patient_confirmation(patient_email, appointment):
    """
    Sends a booking confirmation to the patient's email.

    Returns True on success, False on any failure.
    """
    if not EMAIL_ENABLED or not appointment:
        return False

    patient_email = patient_email.strip()
    if "@" not in patient_email:
        return False

    try:
        subject = (
            f"Appointment Confirmed — {appointment['doctor_name']} "
            f"on {appointment['date']}"
        )

        body = (
            f"Dear {appointment['patient_name']},\n\n"
            f"Your appointment at HealthCare Hospital has been confirmed.\n\n"
            f"{'=' * 45}\n"
            f"Doctor          : {appointment['doctor_name']}\n"
            f"Department      : {appointment.get('department', '')}\n"
            f"Date            : {appointment['date']}\n"
            f"Day             : {appointment['day']}\n"
            f"Serial Number   : {appointment['serial_number']}\n"
            f"Estimated Time  : {appointment['estimated_time']}\n"
            f"Arrival Window  : {appointment['arrival_window']}\n"
            f"{'=' * 45}\n\n"
            f"Please arrive within your arrival window.\n\n"
            f"For any changes, contact us at:\n"
            f"Phone: +880 1700-000000\n"
            f"Email: support@healthcarehospital.com\n\n"
            f"Thank you for choosing HealthCare Hospital!\n\n"
            f"—\n"
            f"This is an automated message. Please do not reply.\n"
        )

        message = MIMEMultipart()
        message["From"] = EMAIL_ADDRESS
        message["To"] = patient_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(message)

        print(f"[email_service] Confirmation sent to {patient_email}")
        return True

    except Exception:
        print("Patient confirmation email error:")
        print(traceback.format_exc())
        return False