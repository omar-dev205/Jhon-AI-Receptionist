import re
from difflib import SequenceMatcher

from clinic_data import (
    find_doctor_by_name,
    find_doctors_by_department,
)
from ai_classifier import ai_detect_department


FUZZY_MATCH_THRESHOLD = 0.8


SYMPTOM_DEPARTMENT_MAP = {
    "Cardiology": [
        "chest pain", "heart pain", "heart problem", "palpitation",
        "palpitations", "blood pressure", "high blood pressure",
        "chest discomfort", "buk betha", "buke betha", "buk dhorfor",
        "buke dhorfor", "বুক ব্যথা", "বুকে ব্যথা", "হার্ট", "হৃদরোগ",
        "বুক ধড়ফড়", "প্রেসার",
    ],
    "Neurology": [
       
        "headache", "migraine", "dizziness", "vertigo", "nerve problem",
        "nerve pain", "memory problem", "matha betha", "mathay betha",
        "মাথা ব্যথা", "মাথায় ব্যথা", "মাইগ্রেন", "মাথা ঘোরা", "নার্ভ",
        # English phrasings for head pain
        "pain in my head", "pain in the head", "head pain",
        "head hurts", "my head hurts", "head is paining",
        "head ache", "headache problem", "severe headache",
        # Banglish variations
        "matha batha", "matha byatha", "matha betha",
        "matha betha korche", "matha betha kortese",
        # Bangla variations
        "মাথা ব্যথা করছে", "মাথা ধরছে", "মাথা যন্ত্রণা",
    ],
    "Ophthalmology": [
        "eye pain", "eye problem", "eye infection", "blurred vision",
        "vision problem", "poor vision", "cataract", "glaucoma",
        "dry eyes", "chokh betha", "chokhe betha", "chokher problem",
        "chokhe ghola", "ghola dekhi", "chokher daktar", "chokher doctor",
        "চোখে ব্যথা", "চোখের সমস্যা", "চোখ জ্বালা", "ঝাপসা", "দেখতে সমস্যা",
    ],
    "Dermatology": [
        "skin problem", "skin disease", "skin allergy", "skin infection",
        "acne", "rash", "itching", "allergy", "eczema", "hair loss",
        "scalp problem", "chulkani", "chamra problem", "ত্বকের সমস্যা",
        "চামড়া", "চুলকানি", "ব্রণ", "অ্যালার্জি", "চুল পড়া", "র‍্যাশ",
    ],
    "Orthopedics": [
        "bone pain", "joint pain", "joint problem", "back pain",
        "knee pain", "knee problem", "leg pain", "fracture",
        "sports injury", "muscle pain", "shoulder pain", "haddi betha",
        "joint betha", "komor betha", "hatu betha", "paye betha",
        "হাড়ে ব্যথা", "জয়েন্ট", "কোমর ব্যথা", "হাঁটু ব্যথা", "পায়ে ব্যথা",
        "হাড় ভাঙা",
    ],
    "General Medicine": [
        "fever", "cold", "cough", "weakness", "diabetes",
        "general problem", "body pain", "jor", "joor", "jor hoise",
        "sordi", "kashi", "durbolota", "shorir kharap", "জ্বর", "সর্দি",
        "কাশি", "দুর্বলতা", "ডায়াবেটিস", "শরীর খারাপ", "শরীর ব্যথা",
    ],
    "ENT": [
        "ear pain", "ear problem", "ear infection", "hearing problem",
        "hearing loss", "nose problem", "nose infection", "sinus",
        "sinus problem", "sore throat", "throat pain", "throat problem",
        "kan betha", "kane betha", "nak bondho", "gola betha",
        "কানে ব্যথা", "কান ব্যথা", "নাক বন্ধ", "সাইনাস", "গলা ব্যথা",
        "গলার সমস্যা",
    ],
    "Gastroenterology": [
        "stomach pain", "stomach problem", "gastric", "gastric problem",
        "acid reflux", "acidity", "digestive problem", "digestion problem",
        "abdominal pain", "intestinal problem", "pet betha", "pete betha",
        "gas", "গ্যাস্ট্রিক", "গ্যাস", "অ্যাসিডিটি", "পেট ব্যথা",
        "পেটের ব্যথা", "হজমের সমস্যা", "বদহজম",
    ],
    "Gynecology": [
        "gynecology", "pregnancy", "pregnant", "period problem",
        "period pain", "menstrual problem", "menstrual pain",
        "women health", "reproductive health", "meyeder problem",
        "মহিলাদের সমস্যা", "নারীদের সমস্যা", "গর্ভাবস্থা", "গর্ভবতী",
        "মাসিক", "মাসিকের সমস্যা", "মাসিকের ব্যথা",
    ],
    "Pediatrics": [
        "child", "children", "baby", "infant", "kid", "kids",
        "pediatric", "child problem", "child fever", "baby fever",
        "baccha", "bachcha", "bacha", "bacchar jor", "শিশু", "বাচ্চা",
        "বাচ্চার", "বাচ্চার জ্বর", "বাচ্চার কাশি", "শিশুর সমস্যা",
    ],
    "Urology": [
        "urine problem", "urinary problem", "urine infection",
        "kidney problem", "kidney stone", "kidney pain",
        "bladder problem", "bladder pain", "prostate problem",
        "pain while urinating", "frequent urination", "blood in urine",
        "urology", "urologist", "urine", "urinary", "bladder", "prostate",
        "proshab problem", "proshabe betha", "proshab jala",
        "prossab", "prosrab", "prashab", "porosab", "poroshab", "kidni",
        "kidney betha", "প্রস্রাব", "প্রস্রাবে ব্যথা", "কিডনি", "মূত্র",
    ],
    "Dentistry": [
        "dentist", "dentistry", "dental", "tooth", "teeth", "tooth pain",
        "toothache", "teeth pain", "gum pain", "gum problem",
        "gum swelling", "cavity", "dental pain", "dental infection",
        "oral problem", "oral pain", "dat betha", "dant betha",
        "dante betha", "dater betha", "dater mari betha",
        "dant er mari betha", "dater marir betha", "mari betha",
        "dat er mari", "দাঁত ব্যথা", "দাঁতের ব্যথা", "দাঁতে ব্যথা",
        "দাঁতের মাড়ি", "দাঁতের মারি", "মাড়ি ব্যথা", "দাঁতের মাড়িতে ব্যথা",
        "দাঁতের সমস্যা",
    ],
}


def normalize_text(text):
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def detect_departments(message):
    text = normalize_text(message)
    departments = []

    for department, keywords in SYMPTOM_DEPARTMENT_MAP.items():
        for keyword in keywords:
            keyword = normalize_text(keyword)
            if keyword in text:
                departments.append(department)
                break

    return departments


def detect_department(message):
    departments = detect_departments(message)
    if not departments:
        return None
    return departments[0]


def detect_departments_fuzzy(message):
    """
    Catches common typos/spelling variants that exact substring
    matching in detect_departments() misses — e.g. "joor" instead of
    "jor", "fiver" instead of "fever". Compares each word in the
    message against each word in the keyword list using the same
    fuzzy-ratio approach already used for doctor-name matching, so a
    single-letter typo still routes correctly instead of silently
    falling through to a full AI call (or worse, to the open-ended
    general chat).

    Both sides of the comparison require length >= 4. Short 3-letter
    words (e.g. keyword "kan" for ear, or a message word like "for")
    are excluded — fuzzy-matching very short strings is unstable and
    collision-prone: "khan" (a common surname) scores 0.857 against
    "kan", well above the 0.8 threshold, which was wrongly routing
    any patient or doctor named Khan into the ENT department. Exact
    substring matching (detect_departments, which runs before this)
    already catches short keywords like "kan" and "gas" when they
    genuinely appear in the message, so nothing meaningful is lost.
    """
    text = normalize_text(message)
    words = [w for w in re.findall(r"[a-zA-Z\u0980-\u09FF]+", text) if len(w) >= 4]

    departments = []
    for department, keywords in SYMPTOM_DEPARTMENT_MAP.items():
        matched = False
        for keyword in keywords:
            keyword_words = [
                kw for kw in normalize_text(keyword).split() if len(kw) >= 4
            ]
            for keyword_word in keyword_words:
                for word in words:
                    score = SequenceMatcher(None, word, keyword_word).ratio()
                    if score >= FUZZY_MATCH_THRESHOLD:
                        matched = True
                        break
                if matched:
                    break
            if matched:
                break
        if matched:
            departments.append(department)

    return departments
def find_doctors_for_problem(message):
    """
    Detect the best department using the LLM.

    Returns:
      {"found": True, "department": ..., "doctors": [...]}
      or {"found": False} if no symptom / vague message.
    """
    from ai_classifier import ai_detect_department

    departments = list(SYMPTOM_DEPARTMENT_MAP.keys())

    # Single LLM call — decides everything
    ai_department = ai_detect_department(message, departments)

    if not ai_department:
        return {
            "found": False,
            "needs_clarification": False,
            "department": None,
            "departments": [],
            "doctors": [],
        }

    # Special marker: if AI returns this, the message was vague
    if ai_department == "NEEDS_CLARIFICATION":
        return {
            "found": False,
            "needs_clarification": True,
            "department": None,
            "departments": [],
            "doctors": [],
        }

    doctors = find_doctors_by_department(ai_department)

    return {
        "found": True,
        "needs_clarification": False,
        "department": ai_department,
        "departments": [ai_department],
        "doctors": doctors,
    }

def get_doctor_details(doctor_name):
    doctor = find_doctor_by_name(doctor_name)
    if not doctor:
        return None

    return {
        "name": doctor["name"],
        "department": doctor["department"],
        "specialization": doctor["specialization"],
        "description": doctor["description"],
        "average_patient_minutes": doctor["average_patient_minutes"],
        "max_patients_per_session": doctor["max_patients_per_session"],
        "schedule": doctor["schedule"],
    }
