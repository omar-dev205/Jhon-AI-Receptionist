from datetime import datetime, timedelta

from clinic_config import (
    get_booking_mode,
    is_serial_mode,
    is_slot_mode,
)

from clinic_data import (
    find_doctor_by_name,
    get_chamber_hours,
    works_on_day,
)

from messages import t
import database


def get_booking_system():
    return {
        "mode": get_booking_mode(),
        "serial_mode": is_serial_mode(),
        "slot_mode": is_slot_mode(),
    }


def get_next_serial_number(doctor_name, date, day):
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return None

    day_name = day.strip().title()
    return database.get_next_serial_number(doctor["name"], date, day_name)


def calculate_estimated_arrival_time(doctor_name, day, serial_number):
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return None

    chamber_hours = get_chamber_hours(doctor_name, day)
    if not chamber_hours:
        return None

    start_time = datetime.strptime(chamber_hours["start"], "%I:%M %p")
    estimated_time = start_time + timedelta(
        minutes=((serial_number - 1) * doctor["average_patient_minutes"])
    )

    return estimated_time.strftime("%I:%M %p")


def calculate_arrival_window(doctor_name, day, serial_number):
    estimated_time = calculate_estimated_arrival_time(doctor_name, day, serial_number)
    if not estimated_time:
        return None

    estimated_datetime = datetime.strptime(estimated_time, "%I:%M %p")
    window_start = estimated_datetime - timedelta(minutes=15)
    window_end = estimated_datetime + timedelta(minutes=15)

    return {
        "estimated_time": estimated_time,
        "arrival_window_start": window_start.strftime("%I:%M %p"),
        "arrival_window_end": window_end.strftime("%I:%M %p"),
    }


def check_serial_availability(doctor_name, date, day, lang="en"):
    # Opportunistic cleanup: any appointment dated before TODAY has
    # already happened, so drop it. Cheap (single DELETE query) and
    # keeps the database from growing forever without needing a
    # separate scheduled job. Uses the real current date, not the
    # date being checked (which may itself be in the future).
    database.cleanup_past_appointments(datetime.now().date().strftime("%Y-%m-%d"))

    if not is_serial_mode():
        return {
            "available": False,
            "message": t("serial_booking_disabled", lang),
        }

    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return {
            "available": False,
            "message": t("doctor_not_found", lang),
        }

    day_name = day.strip().title()

    if not works_on_day(doctor_name, day_name):
        available_days = ", ".join(doctor["schedule"].keys())
        return {
            "available": False,
            "message": t(
                "not_available_on_day",
                lang,
                doctor=doctor["name"],
                day=day_name,
                days=available_days,
            ),
        }

    next_serial = get_next_serial_number(doctor_name, date, day_name)

    if next_serial > doctor["max_patients_per_session"]:
        return {
            "available": False,
            "message": t(
                "no_serials_available",
                lang,
                doctor=doctor["name"],
                day=day_name,
            ),
        }

    return {
        "available": True,
        "next_serial": next_serial,
        "message": t(
            "serial_available",
            lang,
            doctor=doctor["name"],
            serial=next_serial,
        ),
    }


def book_serial(doctor_name, date, day, patient_name, phone, lang="en"):
    if not is_serial_mode():
        return {
            "success": False,
            "message": t("serial_booking_disabled", lang),
        }

    result = check_serial_availability(doctor_name, date, day, lang=lang)
    if not result["available"]:
        return {
            "success": False,
            "message": result["message"],
        }

    doctor = find_doctor_by_name(doctor_name)
    day_name = day.strip().title()
    serial_number = result["next_serial"]

    arrival_info = calculate_arrival_window(doctor_name, day_name, serial_number)

    appointment = {
        "doctor_name": doctor["name"],
        "department": doctor["department"],
        "date": date,
        "day": day_name,
        "serial_number": serial_number,
        "patient_name": patient_name,
        "phone": phone,
        "estimated_time": arrival_info["estimated_time"],
        "arrival_window": (
            f"{arrival_info['arrival_window_start']} - "
            f"{arrival_info['arrival_window_end']}"
        ),
    }

    database.save_appointment(appointment)

    return {
        "success": True,
        "appointment": appointment,
        "message": t(
            "booking_confirmed_message",
            lang,
            serial=serial_number,
            time=arrival_info["estimated_time"],
            start=arrival_info["arrival_window_start"],
            end=arrival_info["arrival_window_end"],
        ),
    }


def get_booked_appointments():
    return database.get_all_appointments()


if __name__ == "__main__":
    print("Jhon Booking System Test\n")

    system = get_booking_system()
    print(f"Booking Mode: {system['mode']}")
    print(f"Serial Mode: {system['serial_mode']}")
    print(f"Slot Mode: {system['slot_mode']}\n")

    for number in range(1, 4):
        booking = book_serial(
            doctor_name="Karim",
            date="2026-09-01",
            day="Tuesday",
            patient_name=f"Patient {number}",
            phone=f"017000000{number:02d}",
        )
        print(booking["message"])
