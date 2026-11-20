"""
Auto-sync engine: clinic_data.py <-> database.

One system, three directions — no restarts, no manual migrate:

  1. File -> DB: a background watcher notices every clinic_data.py
     edit (a few seconds later) and syncs it into the database.
     Server startup does the same sync once, so a fresh deploy
     (VPS / cloud) also lands the file's data automatically.
  2. DB -> file: every Telegram admin change (add / slot / remove)
     rewrites clinic_data.py's doctor list from the database,
     so the file always mirrors the live data.
  3. Startup adopt: active doctors that exist only in the DB
     (e.g. added from Telegram before this version) are written
     into the file once, so nothing is ever lost.

Rules:
- A doctor present in the file is always active=1 in the DB.
- An active DB doctor missing from the file is set active=0
  (a file remove removes them from the website too — their
  old bookings are kept, only future bookings stop).
- The file is parsed with ast (never imported), so edits are
  picked up without restarting Python.
"""

import ast
import hashlib
import json
import os
import pprint
import threading
import time

import database

CLINIC_DATA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "clinic_data.py"
)


# ---------------------------------------------------------
# File <-> list (ast: no import caching, no side effects)
# ---------------------------------------------------------
def _read_file_doctors():
    """Parse CLINIC_DOCTORS out of clinic_data.py. [] on any error."""
    try:
        with open(CLINIC_DATA_PATH, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CLINIC_DOCTORS":
                        value = ast.literal_eval(node.value)
                        if isinstance(value, list):
                            return value
    except Exception as e:
        print(f"[sync] Could not read {CLINIC_DATA_PATH}: {e}")
    return []


def _write_file_doctors(doctors):
    """Replace ONLY the CLINIC_DOCTORS block in clinic_data.py.

    Everything else in the file (functions, imports) is untouched.
    Written atomically (temp file + rename) so a reader never
    sees a half-written file.
    """
    with open(CLINIC_DATA_PATH, encoding="utf-8") as f:
        text = f.read()

    start = text.find("CLINIC_DOCTORS = [")
    if start < 0:
        raise ValueError("CLINIC_DOCTORS block not found in clinic_data.py")

    # Find the matching closing bracket by counting depth.
    depth = 0
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise ValueError("CLINIC_DOCTORS block has no closing bracket")

    ordered = sorted(doctors, key=lambda d: d.get("name", ""))
    block = "CLINIC_DOCTORS = " + pprint.pformat(
        ordered, width=100, sort_dicts=False
    )
    new_text = text[:start] + block + text[end:]

    tmp = CLINIC_DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, CLINIC_DATA_PATH)


def _doctors_hash(doctors):
    """Stable fingerprint of a doctor list (for change detection)."""
    norm = [
        {
            "name": d.get("name", ""),
            "department": d.get("department", ""),
            "specialization": d.get("specialization", ""),
            "description": d.get("description", ""),
            "average_patient_minutes": d.get("average_patient_minutes", 10),
            "max_patients_per_session": d.get("max_patients_per_session", 20),
            "schedule": d.get("schedule", {}) or {},
        }
        for d in doctors
    ]
    norm.sort(key=lambda d: d["name"].lower())
    return hashlib.sha256(
        json.dumps(norm, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _db_doctors():
    """Active doctors from the database, as CLINIC_DOCTORS-style dicts."""
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM doctors WHERE active = 1 ORDER BY name"
        ).fetchall()
    out = []
    for row in rows:
        try:
            sched = json.loads(row["schedule"]) if row["schedule"] else {}
        except (json.JSONDecodeError, TypeError):
            sched = {}
        out.append(
            {
                "name": row["name"],
                "department": row["department"],
                "specialization": row["specialization"] or "",
                "description": row["description"] or "",
                "average_patient_minutes": row["average_patient_minutes"] or 10,
                "max_patients_per_session": row["max_patients_per_session"] or 20,
                "schedule": sched,
            }
        )
    return out


# ---------------------------------------------------------
# File -> DB (the old migrate logic, now importable)
# ---------------------------------------------------------
def sync_db_from_file(doctors=None):
    """UPSERT the file's doctors into the DB; deactivate active DB
    doctors that are no longer in the file. Returns a result dict.

    File wins for every doctor it lists (name spelling, department,
    schedule, limits). Safe to run many times.
    """
    if doctors is None:
        doctors = _read_file_doctors()

    inserted = updated = up_to_date = deactivated = errors = 0
    file_names = {d.get("name", "").lower() for d in doctors}

    with database.get_connection() as conn:
        for doctor in doctors:
            try:
                name = doctor["name"]
                cur = conn.execute(
                    "SELECT * FROM doctors WHERE LOWER(name) = LOWER(?)",
                    (name,),
                )
                row = cur.fetchone()
                file_schedule = doctor.get("schedule", {}) or {}

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO doctors
                            (name, department, specialization, description,
                             average_patient_minutes, max_patients_per_session,
                             schedule, active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            name,
                            doctor["department"],
                            doctor.get("specialization", ""),
                            doctor.get("description", ""),
                            doctor.get("average_patient_minutes", 10),
                            doctor.get("max_patients_per_session", 20),
                            json.dumps(file_schedule),
                        ),
                    )
                    inserted += 1
                    continue

                new_values = (
                    name,
                    doctor["department"],
                    doctor.get("specialization", "") or "",
                    doctor.get("description", "") or "",
                    doctor.get("average_patient_minutes", 10),
                    doctor.get("max_patients_per_session", 20),
                    json.dumps(file_schedule, sort_keys=True),
                    1,
                )
                old_values = (
                    row["name"],
                    row["department"],
                    row["specialization"] or "",
                    row["description"] or "",
                    row["average_patient_minutes"] or 10,
                    row["max_patients_per_session"] or 20,
                    json.dumps(
                        json.loads(row["schedule"]) if row["schedule"] else {},
                        sort_keys=True,
                    ),
                    row["active"],
                )
                if new_values == old_values:
                    up_to_date += 1
                    continue

                conn.execute(
                    """
                    UPDATE doctors SET
                        name = ?, department = ?, specialization = ?,
                        description = ?, average_patient_minutes = ?,
                        max_patients_per_session = ?, schedule = ?,
                        active = ?
                    WHERE id = ?
                    """,
                    (*new_values, row["id"]),
                )
                updated += 1
            except Exception as e:
                errors += 1
                print(f"[sync] Error syncing {doctor.get('name')}: {e}")

        # File remove: deactivate active DB doctors not in the file.
        # (Bookings are kept — only future bookings stop.)
        rows = conn.execute(
            "SELECT id, name FROM doctors WHERE active = 1"
        ).fetchall()
        for row in rows:
            if row["name"].lower() not in file_names:
                conn.execute(
                    "UPDATE doctors SET active = 0 WHERE id = ?",
                    (row["id"],),
                )
                deactivated += 1

        conn.commit()

    return {
        "inserted": inserted,
        "updated": updated,
        "up_to_date": up_to_date,
        "deactivated": deactivated,
        "errors": errors,
    }


# ---------------------------------------------------------
# DB -> file
# ---------------------------------------------------------
def sync_file_from_db():
    """Rewrite clinic_data.py's doctor list to exactly the DB's
    active doctors. Used after every Telegram admin change, so
    the file always mirrors the live data. Returns the count."""
    doctors = _db_doctors()
    _write_file_doctors(doctors)
    return len(doctors)


def adopt_db_doctors_into_file():
    """One-time adopt: active DB doctors missing from the file are
    appended to the file (e.g. added from Telegram before this
    version). Removed doctors are never resurrected — a removed
    doctor is inactive in the DB, so it is not adopted.
    Returns how many were adopted."""
    file_doctors = _read_file_doctors()
    file_names = {d.get("name", "").lower() for d in file_doctors}
    adopted = 0
    for doc in _db_doctors():
        if doc["name"].lower() not in file_names:
            file_doctors.append(doc)
            adopted += 1
    if adopted:
        _write_file_doctors(file_doctors)
    return adopted


# ---------------------------------------------------------
# Startup + watcher
# ---------------------------------------------------------
def converge_on_startup():
    """Run once at server/bot start: adopt any DB-only doctors
    into the file, then sync the file into the DB. Returns the
    sync result dict."""
    try:
        adopted = adopt_db_doctors_into_file()
        if adopted:
            print(f"[sync] Adopted {adopted} doctor(s) from DB into clinic_data.py")
        result = sync_db_from_file()
        if result["inserted"] or result["updated"] or result["deactivated"]:
            print(
                "[sync] Startup sync: "
                f"inserted={result['inserted']} updated={result['updated']} "
                f"deactivated={result['deactivated']}"
            )
        return result
    except Exception as e:
        print(f"[sync] Startup sync failed (continuing anyway): {e}")
        return {"inserted": 0, "updated": 0, "up_to_date": 0,
                "deactivated": 0, "errors": 1}


def start_file_watcher(interval_seconds=5):
    """Background thread: whenever clinic_data.py changes, sync
    it into the DB within a few seconds — no restart needed.
    Daemon thread: it never blocks shutdown, and any error is
    logged, never raised."""

    def _watch():
        last_mtime = None
        last_hash = None
        while True:
            try:
                try:
                    mtime = os.path.getmtime(CLINIC_DATA_PATH)
                except OSError:
                    time.sleep(interval_seconds)
                    continue
                if mtime != last_mtime:
                    last_mtime = mtime
                    doctors = _read_file_doctors()
                    h = _doctors_hash(doctors)
                    if h != last_hash:
                        last_hash = h
                        result = sync_db_from_file(doctors)
                        if result["inserted"] or result["updated"] \
                                or result["deactivated"]:
                            print(
                                "[sync] clinic_data.py changed -> DB updated: "
                                f"inserted={result['inserted']} "
                                f"updated={result['updated']} "
                                f"deactivated={result['deactivated']}"
                            )
                        # Refresh the in-memory list too (best effort).
                        try:
                            import importlib
                            import clinic_data
                            importlib.reload(clinic_data)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[sync] Watcher error (will retry): {e}")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_watch, daemon=True, name="clinic-sync-watcher")
    t.start()
    return t
