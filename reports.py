"""
Report builder for HealthCare Hospital.

Two flavours:
  1. write_daily_sheets(date_str)
       — writes per-(department, doctor) tabs in Google Sheets.
  2. build_doctor_report(date_str, doctor_name)
       — returns (summary_text, csv_path) for ONE doctor,
         used by the cutoff-report email.
"""

import os
import traceback
from datetime import datetime
from collections import defaultdict

from dotenv import load_dotenv

import database
from notifications import (
    get_spreadsheet,
    send_cutoff_report_email,
)

load_dotenv()


# ---------------------------------------------------------
# GOOGLE SHEETS (per-doctor tabs)
# ---------------------------------------------------------
SHEET_HEADER = [
    "Serial", "Patient Name", "Phone",
    "Estimated Time", "Arrival Window", "Booked At",
]


def _tab_name(department, doctor_name):
    raw = f"{department or 'General'} - {doctor_name}"
    cleaned = raw.replace("/", "-").replace("\\", "-").replace(":", "-")
    return cleaned[:100]


def write_daily_sheets(date_str):
    """Writes every doctor's patient list into their own tab."""
    spreadsheet = get_spreadsheet()
    rows = database.get_appointments_for_date(date_str)

    if not rows:
        return {
            "sheets_written": 0,
            "rows_written": 0,
            "breakdown": {},
        }

    grouped = defaultdict(lambda: defaultdict(list))
    for row in rows:
        department = row.get("department") or "General"
        doctor = row["doctor_name"]
        grouped[department][doctor].append(row)

    sheets_written = 0
    rows_written = 0

    if spreadsheet is not None:
        for department, doctors in grouped.items():
            for doctor, appointments in doctors.items():
                tab = _tab_name(department, doctor)
                try:
                    worksheet = _get_or_create_worksheet(spreadsheet, tab)
                    _overwrite_worksheet(worksheet, appointments)
                    sheets_written += 1
                    rows_written += len(appointments)
                except Exception:
                    print(f"Sheet write error for tab '{tab}':")
                    print(traceback.format_exc())

    breakdown = {
        dept: {doc: len(appts) for doc, appts in docs.items()}
        for dept, docs in grouped.items()
    }

    return {
        "sheets_written": sheets_written,
        "rows_written": rows_written,
        "breakdown": breakdown,
    }


def _get_or_create_worksheet(spreadsheet, tab_name):
    try:
        return spreadsheet.worksheet(tab_name)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=tab_name, rows=1000, cols=len(SHEET_HEADER)
        )
        worksheet.append_row(SHEET_HEADER)
        return worksheet


def _overwrite_worksheet(worksheet, appointments):
    worksheet.clear()
    worksheet.append_row(SHEET_HEADER)
    for row in sorted(appointments, key=lambda r: r["serial_number"]):
        worksheet.append_row([
            row["serial_number"],
            row["patient_name"],
            row["phone"],
            row["estimated_time"],
            row["arrival_window"],
            row.get("created_at", ""),
        ])


# ---------------------------------------------------------
# PER-DOCTOR REPORT (used by cutoff email)
# ---------------------------------------------------------
def build_doctor_report(date_str, doctor_name):
    """
    Returns (summary_text, csv_path) for ONE doctor on ONE date.
    Returns (None, None) if this doctor has no bookings that day.
    """
    rows = database.get_appointments_for_date(
        date_str, doctor_name=doctor_name
    )
    if not rows:
        return None, None

    # Sort by serial
    rows = sorted(rows, key=lambda r: r["serial_number"])

    department = rows[0].get("department", "")

    # Build human-readable summary
    lines = []
    lines.append(f"Doctor       : {doctor_name}")
    lines.append(f"Department   : {department}")
    lines.append(f"Date         : {date_str}")
    lines.append(f"Total booked : {len(rows)} patient(s)")
    lines.append("")
    lines.append(
        f"{'Serial':<7}{'Patient':<22}{'Phone':<16}{'Time':<12}"
    )
    lines.append("-" * 60)
    for r in rows:
        lines.append(
            f"{r['serial_number']:<7}"
            f"{r['patient_name'][:20]:<22}"
            f"{r['phone']:<16}"
            f"{r['estimated_time']:<12}"
        )

    summary_text = "\n".join(lines)

    # Write CSV for attachment
    export_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "exports"
    )
    os.makedirs(export_dir, exist_ok=True)

    safe_name = doctor_name.replace(" ", "_").replace(".", "")
    csv_path = os.path.join(
        export_dir,
        f"report_{date_str}_{safe_name}.csv",
    )

    import csv
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Serial", "Patient Name", "Phone",
            "Estimated Time", "Arrival Window",
        ])
        for r in rows:
            writer.writerow([
                r["serial_number"],
                r["patient_name"],
                r["phone"],
                r["estimated_time"],
                r["arrival_window"],
            ])

    return summary_text, csv_path


def send_doctor_cutoff_report(date_str, doctor_name):
    """
    Builds + emails the cutoff report for ONE doctor.
    Skips silently if that doctor has no bookings today.

    Returns True if sent, False otherwise.
    """
    summary_text, csv_path = build_doctor_report(date_str, doctor_name)
    if summary_text is None:
        print(f"[report] No bookings for {doctor_name} — skipping email")
        return False

    # Also refresh this doctor's Google Sheet tab
    try:
        spreadsheet = get_spreadsheet()
        if spreadsheet is not None:
            rows = database.get_appointments_for_date(
                date_str, doctor_name=doctor_name
            )
            if rows:
                department = rows[0].get("department", "General")
                tab = _tab_name(department, doctor_name)
                worksheet = _get_or_create_worksheet(spreadsheet, tab)
                _overwrite_worksheet(worksheet, rows)
    except Exception:
        print("Sheet refresh error (non-fatal):")
        print(traceback.format_exc())

    return send_cutoff_report_email(
        doctor_name=doctor_name,
        date_str=date_str,
        summary_text=summary_text,
        csv_path=csv_path,
    )


# ---------------------------------------------------------
# OLD DAILY REPORT (kept for /report command)
# ---------------------------------------------------------
def build_summary(date_str, breakdown):
    lines = []
    lines.append("HealthCare Hospital — Daily Appointment Report")
    lines.append(f"Date: {date_str}")
    lines.append("")

    total = 0
    if not breakdown:
        lines.append("No bookings for this date.")
        return "\n".join(lines)

    for department in sorted(breakdown.keys()):
        lines.append(department.upper())
        doctor_map = breakdown[department]
        dept_total = 0
        for doctor in sorted(doctor_map.keys()):
            count = doctor_map[doctor]
            dept_total += count
            lines.append(f"  {doctor:<25} : {count} patient(s)")
        lines.append(f"  {'— Department total':<25} : {dept_total}")
        lines.append("")
        total += dept_total

    lines.append(f"TOTAL BOOKINGS: {total}")
    return "\n".join(lines)


def run_daily_report(date_str=None, send_email=False):
    """
    Bulk version (used by /report command).
    Writes all sheets. Returns summary + stats.

    send_email defaults to False now — email is handled per-doctor
    by the cutoff scheduler, not by this function.
    """
    if date_str is None:
        date_str = datetime.now().date().strftime("%Y-%m-%d")

    result = write_daily_sheets(date_str)
    summary = build_summary(date_str, result["breakdown"])

    return {
        "date": date_str,
        "summary": summary,
        "sheets_written": result["sheets_written"],
        "rows_written": result["rows_written"],
        "breakdown": result["breakdown"],
    }


if __name__ == "__main__":
    report = run_daily_report()
    print(report["summary"])
    print()
    print(f"Sheets written: {report['sheets_written']}")
    print(f"Rows written:   {report['rows_written']}")