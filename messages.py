"""
Message templates for the clinic bot — English + Bangla.

Usage:
    from messages import t, detect_lang
    t("doctor_not_found")
    t("serial_available", "bn", doctor="Dr. X", serial=3)

Language: a patient who writes Banglish (romanised Bangla) gets
Bangla replies (Bengali script, common English words mixed in —
the way a Bangladeshi receptionist texts); a patient who writes
English gets English replies. detect_lang() figures out which
one the message is; it returns None when the message carries no
language signal (e.g. a bare "yes"/"ok"), so the caller keeps
the session's current language.
"""

import re

# ---------------------------------------------------------
# LANGUAGE DETECTION
# ---------------------------------------------------------
# Bengali script anywhere in the message -> Bangla.
_BN_SCRIPT = re.compile(r"[\u0980-\u09FF]")

# Romanised-Bangla marker words. These are Bangla words written
# in Latin letters — they never appear in a normal English
# sentence. Shared words such as "doctor", "appointment",
# "schedule", "serial" are deliberately NOT here: patients use
# them in both languages, so they carry no signal.
_BN_ROMAN_WORDS = frozenset((
    "amr", "amar", "ami", "tumi", "apni", "apnar", "kemon", "achen",
    "acho", "asen", "aso", "joor", "jwor", "jor", "thanda", "betha",
    "byatha", "matha", "pete", "sorir", "shorir", "ga", "cai", "chai",
    "cay", "nite", "nitey", "nibo", "niben", "korbo", "korben", "kore",
    "korte", "koro", "korun", "asche", "ache", "achhe", "nai", "nei",
    "thak", "thakbo", "haa", "hea", "heya", "ha", "hya", "ji", "bolen",
    "bolo", "bolte", "bolben", "bolbo", "jante", "janaben", "koyta",
    "kokhon", "kothay", "kothai", "koto", "ajke", "aj", "kalke", "kal",
    "sonibar", "shonibar", "robbar", "robibar", "shukrobar", "sukrobar",
    "sombar", "shombar", "mongolbar", "budhbar", "budbar",
    "brihospotibar", "brihoshpotibar", "kache", "kachey", "kivabe",
    "kibhabe", "lagbe", "dorkar", "dao", "den", "diben", "dibo",
    "dekhabe", "dekhaw", "dekhben", "pari", "parbo", "parben", "jabo",
    "jaben", "asbo", "asben", "boshe", "boshen", "tor", "tar", "onar",
    "unar", "unake", "eder", "oder", "shei", "ei", "oi", "ekta",
    "duita", "koyjon", "kotogulo", "keno", "ken", "ki", "kiser",
    "kisher", "kobe", "kemne", "kamne", "bhalo", "valo", "kharap",
    "onek", "kom", "beshi", "ekhon", "ekhono", "pore", "age", "porey",
    "ajkei", "buke", "gaye", "chokh", "kan", "nak", "gola", "daat",
    "dat", "haat", "pa", "komor", "buk", "pet", "mathay", "kashi",
    "sordi", "gorom", "ghum", "khida", "khabar", "oshustho", "oshuk",
    "rog", "rogi", "daktar", "oshudh", "osudh", "dawa",
))

# Clear English words. Checked only AFTER the Bangla markers.
# Shared words patients use in BOTH languages ("doctor",
# "appointment", "schedule", "hospital", "patient", "phone",
# "name", "email") are in NEITHER list — a bare "doctor rana"
# carries no language signal, so it must not flip the session
# language mid-conversation.
_EN_WORDS = frozenset((
    "what", "when", "where", "which", "who", "how", "why", "please",
    "want", "need", "would", "could", "should", "book", "booking",
    "cancel", "hello", "hi", "hey", "thanks", "thank", "good",
    "morning", "afternoon", "evening", "today", "tomorrow",
    "yesterday", "available", "fever", "cold", "headache", "pain", "stomach", "urine",
    "problem", "symptoms", "symptom", "help", "know", "tell", "me",
    "my", "your", "is", "are", "am", "have", "has", "can", "will",
    "the", "and", "with", "like", "see", "check", "give", "take",
    "come", "speak", "english", "bangla", "yes", "no", "ok", "okay",
    "sure", "confirm",
))

# Bare confirmations carry no language signal on their own —
# "yes" / "ok" after a Banglish conversation must NOT flip the
# reply language to English. If the English words in a message
# are only these, detect_lang returns None (keep session lang).
_CONFIRM_EN = frozenset((
    "yes", "yeah", "yep", "ok", "okay", "sure", "no", "nah", "nope",
    "confirm", "please",
))


def detect_lang(message):
    """'bn' for Bangla (script or romanised), 'en' for English,
    None when the message carries no language signal."""
    if not message:
        return None
    if _BN_SCRIPT.search(message):
        return "bn"
    words = set(re.findall(r"[a-z']+", message.lower()))
    if words & _BN_ROMAN_WORDS:
        return "bn"
    en_words = words & _EN_WORDS
    if en_words and not en_words <= _CONFIRM_EN:
        return "en"
    return None


MESSAGES = {
    'doctor_not_found': 'Doctor not found.',
    'serial_booking_disabled': 'Serial booking is not enabled for this clinic.',
    'not_available_on_day': '{doctor} is not available on {day}. Available days: {days}.',
    'no_serials_available': 'No serials are available for {doctor} on {day}.',
    'serial_available': 'Serial {serial} is available for {doctor}.',
    'booking_confirmed_message': 'Appointment confirmed. Your serial number is {serial}. Estimated consultation time: {time}. Please arrive between {start} and {end}.',
    'past_date': 'You cannot book an appointment for a past date.',
    'same_day_unavailable': 'Same-day booking is not available.',
    'advance_limit': 'Appointments can only be booked up to {days} days in advance.',
    'booking_mode_not_implemented': "Booking mode '{mode}' is not implemented yet.",
    'ask_which_day': 'Which day would you like to book?',
    'ask_day_examples': 'Which day would you like the appointment?\n\nExample: Friday, Wednesday, or 2026-09-10',
    'doctor_selected_schedule': 'Sure. You selected {doctor}.\n\nSchedule:\n{schedule}\n\nWhich day would you like to book?',
    'doctor_not_identified': "I couldn't identify the doctor. Please provide the doctor's name.",
    'slot_available_ask_name': '{doctor} is available on {day}, {date}.\n\nCurrent next serial: {serial}\n\nTo continue booking, please give your name and phone number together.\nExample: Karim Rahman, 01700000000',
    'ask_missing_name': 'Got the phone number. Please also give your name.',
    'ask_missing_phone': 'Got your name. Please also give your phone number.\nExample: 01700000000',
    'invalid_phone': 'Please give a valid phone number.\nExample: 01700000000',
    'booking_details_confirm': 'Booking details:\n\nDoctor: {doctor}\nDate: {date}\nPatient: {name}\nPhone: {phone}\n\nIs everything correct?\nPlease type Yes / No.',
    'booking_success': 'Appointment confirmed successfully! ✅\n\nDoctor: {doctor}\nDate: {date}\nDay: {day}\nSerial Number: {serial}\nEstimated Consultation: {time}\nArrival Window: {window}\n\nPatient: {name}\nPhone: {phone}\n\nPlease arrive within your arrival window.',
    'booking_failed_generic': 'Sorry, booking could not be completed.',
    'booking_cancelled': 'Okay. Booking has been cancelled. You can start again anytime.',
    'ask_yes_no': 'Please type Yes or No.',
    'symptom_department_suggestion': 'The {department} department may be suitable for your problem.\n\nAvailable doctors:\n{doctor_list}\n\nWhich doctor would you like to see?',
    'symptom_single_doctor_suggestion': 'The {department} department may be suitable for your problem.\n\nAvailable doctors:\n{doctor_list}\n\nWould you like to book an appointment with {doctor_name}?',
    'resolved_symptom': "I'm glad to hear that your problem has improved. If you have another health concern, feel free to tell me.",
    'emergency_warning': 'Your symptoms may be an emergency. Please contact the nearest emergency department or emergency medical service without delay. Do not wait for a routine appointment.',
    'generic_error': 'Sorry, something went wrong. Please try again.',
    'fallback_unclear': "Sorry, I couldn't understand that. You can tell me your health problem or ask about a doctor.",
    'voice_transcription_failed': "Sorry, I couldn't understand that voice message. Could you try typing it, or send the voice message again?",
    'ask_symptom_for_booking': 'Sure, I can help you book an appointment. Could you briefly tell me your main health problem? That way I can point you to the right department and doctor.',
    'booking_thank_you': 'Thank you for choosing {clinic_name}! We look forward to seeing you.',
    'clinic_overview': "{clinic_name} is located at {address}. Contact: {phone}.\n\nDepartments & Doctors:\n{departments}\n\nLet me know if you'd like to book an appointment with any of them.",
}

# Bangla versions of every key above. Bangla script with common
# English words mixed in (doctor, appointment, schedule, serial)
# — the way a Bangladeshi receptionist texts a patient.
MESSAGES_BN = {
    'doctor_not_found': 'Doctor পাওয়া যায়নি।',
    'serial_booking_disabled': 'এই clinic-এ serial booking চালু নেই।',
    'not_available_on_day': '{doctor} {day}-এ available নেই। Available দিন: {days}।',
    'no_serials_available': '{doctor}-এর {day}-এর কোনো serial available নেই।',
    'serial_available': '{doctor}-এর Serial {serial} available আছে।',
    'booking_confirmed_message': 'Appointment confirm হয়েছে। আপনার serial number {serial}। আনুমানিক consultation সময়: {time}। {start} থেকে {end}-এর মধ্যে আসবেন।',
    'past_date': 'আপনি past date-এর appointment নিতে পারবেন না।',
    'same_day_unavailable': 'আজকের দিনের booking available নেই।',
    'advance_limit': 'Appointment শুধু {days} দিন আগে পর্যন্ত নেওয়া যাবে।',
    'booking_mode_not_implemented': "Booking mode '{mode}' এখনো implement করা হয়নি।",
    'ask_which_day': 'আপনি কোন দিন booking করতে চান?',
    'ask_day_examples': 'কোন দিন appointment চান?\n\nউদাহরণ: Friday, Wednesday, বা 2026-09-10',
    'doctor_selected_schedule': 'ঠিক আছে, আপনি {doctor} কে select করেছেন।\n\nSchedule:\n{schedule}\n\nআপনি কোন দিন booking করতে চান?',
    'doctor_not_identified': 'Doctor-কে চিনতে পারিনি। Doctor-এর নাম বলুন।',
    'slot_available_ask_name': '{doctor} {day}, {date}-এ available আছেন।\n\nবর্তমান serial: {serial}\n\nBooking চালিয়ে যেতে আপনার নাম আর phone number একসাথে দিন।\nউদাহরণ: Karim Rahman, 01700000000',
    'ask_missing_name': 'Phone number পেয়েছি। আপনার নামটাও দিন।',
    'ask_missing_phone': 'নাম পেয়েছি। আপনার phone number-টাও দিন।\nউদাহরণ: 01700000000',
    'invalid_phone': 'সঠিক phone number দিন।\nউদাহরণ: 01700000000',
    'booking_details_confirm': 'Booking-এর details:\n\nDoctor: {doctor}\nDate: {date}\nPatient: {name}\nPhone: {phone}\n\nসব ঠিক আছে তো?\nYes / No লিখুন।',
    'booking_success': 'Appointment confirm হয়েছে! ✅\n\nDoctor: {doctor}\nDate: {date}\nDay: {day}\nSerial Number: {serial}\nআনুমানিক Consultation: {time}\nআসার সময়: {window}\n\nPatient: {name}\nPhone: {phone}\n\nআপনার আসার সময়ের মধ্যে আসবেন।',
    'booking_failed_generic': 'দুঃখিত, booking complete করা যায়নি।',
    'booking_cancelled': 'ঠিক আছে, booking cancel করা হয়েছে। যেকোনো সময় আবার শুরু করতে পারেন।',
    'ask_yes_no': 'Yes বা No লিখুন।',
    'symptom_department_suggestion': 'আপনার সমস্যার জন্য {department} বিভাগ উপযুক্ত হতে পারে।\n\nডাক্তার:\n{doctor_list}\n\nআপনি কোন doctor-কে দেখতে চান?',
    'symptom_single_doctor_suggestion': 'আপনার সমস্যার জন্য {department} বিভাগ উপযুক্ত হতে পারে।\n\nডাক্তার:\n{doctor_list}\n\nআপনি কি {doctor_name}-এর appointment নিতে চান?',
    'resolved_symptom': 'শুনে ভালো লাগলো যে আপনার সমস্যা কমেছে। আর কোনো health সমস্যা থাকলে বলতে পারেন।',
    'emergency_warning': 'আপনার symptom emergency হতে পারে। দেরি না করে কাছের emergency বিভাগ বা emergency medical service-এ যোগাযোগ করুন। Routine appointment-এর জন্য অপেক্ষা করবেন না।',
    'generic_error': 'দুঃখিত, কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।',
    'fallback_unclear': 'দুঃখিত, বুঝতে পারিনি। আপনার health সমস্যা বলুন বা কোনো doctor সম্পর্কে জিজ্ঞেস করুন।',
    'voice_transcription_failed': 'দুঃখিত, voice message-টা বুঝতে পারিনি। Type করে বলবেন, বা voice message-টা আবার পাঠাবেন?',
    'ask_symptom_for_booking': 'ঠিক আছে, আমি appointment book করতে সাহায্য করতে পারি। আপনার মূল health সমস্যাটা সংক্ষেপে বলবেন? তাহলে সঠিক department আর doctor-এর কাছে পাঠাতে পারব।',
    'booking_thank_you': '{clinic_name} বেছে নেওয়ার জন্য ধন্যবাদ! আপনার সাথে দেখা হবে।',
    'clinic_overview': "{clinic_name} {address}-এ অবস্থিত। Contact: {phone}।\n\nDepartments & Doctors:\n{departments}\n\nকোনো doctor-এর appointment নিতে চাইলে বলুন।",
}


def t(key, lang="en", **kwargs):
    if lang == "bn":
        template = MESSAGES_BN.get(key) or MESSAGES.get(key, "")
    else:
        template = MESSAGES.get(key, "")
    if kwargs:
        return template.format(**kwargs)
    return template
