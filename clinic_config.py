CLINIC = {
    "name": "HealthCare Hospital",
    "country": "Bangladesh",
    "address": "Dhaka, Bangladesh",
    "phone": "+880 1700-000000",
    "email": "support@healthcarehospital.com",
    "working_hours": {
        "Sunday": "09:00 AM - 08:00 PM",
        "Monday": "09:00 AM - 08:00 PM",
        "Tuesday": "09:00 AM - 08:00 PM",
        "Wednesday": "09:00 AM - 08:00 PM",
        "Thursday": "09:00 AM - 08:00 PM",
    },
    "default_booking_mode": "BD_SERIAL",
}


BOOKING_MODES = {
    "BD_SERIAL": {
        "name": "Bangladesh Serial Queue",
        "description": "Patients receive a serial number and an estimated consultation time."
    },
    "GLOBAL_SLOT": {
        "name": "Global Fixed Time Slot",
        "description": "Patients select a specific available appointment time."
    }
}


def get_clinic_info():
    return CLINIC


def get_booking_mode():
    return CLINIC["default_booking_mode"]


def set_booking_mode(mode):
    if mode not in BOOKING_MODES:
        raise ValueError(f"Invalid booking mode: {mode}")

    CLINIC["default_booking_mode"] = mode


def is_serial_mode():
    return get_booking_mode() == "BD_SERIAL"


def is_slot_mode():
    return get_booking_mode() == "GLOBAL_SLOT"


if __name__ == "__main__":
    print("HealthCare Hospital Configuration Test\n")

    print("Clinic:", get_clinic_info()["name"])
    print("Country:", get_clinic_info()["country"])
    print("Booking Mode:", get_booking_mode())
    print("Serial Mode:", is_serial_mode())
    print("Slot Mode:", is_slot_mode())