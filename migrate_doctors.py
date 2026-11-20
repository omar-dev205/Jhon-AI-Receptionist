"""
Manual doctor sync — file -> database.

Normally you do NOT need this command any more: the server and the
Telegram bot watch clinic_data.py and sync it into the database
automatically within a few seconds (and once at startup), and
Telegram changes write back into the file too.

Keep this command for a manual check / first-time setup:

    python migrate_doctors.py

Rules (file is the master list):
- Doctor in file but not in DB  -> INSERT.
- Doctor already in DB          -> UPDATE from the file (department,
  specialization, minutes, max patients, name spelling, schedule).
  The file WINS completely: what you write in clinic_data.py is
  what the database (and the website) will show.
- Doctor in DB but not in file  -> set inactive (a file remove
  removes them from the website; old bookings are kept).
- Matching is case-insensitive, so fixing "Dr. mina" -> "Dr. Mina"
  in the file renames the DB row instead of creating a duplicate.

Safe to run many times — a second run reports "already up to date".
"""

import database
from sync import sync_db_from_file


def migrate_doctors():
    result = sync_db_from_file()

    print(f"Inserted (new): {result['inserted']}")
    print(f"Updated: {result['updated']}")
    print(f"Already up to date: {result['up_to_date']}")
    print(f"Deactivated (removed from file): {result['deactivated']}")
    print(f"Errors: {result['errors']}")
    print()

    # Verify — list all doctors in DB
    with database.get_connection() as conn:
        cur = conn.execute(
            "SELECT name, department FROM doctors WHERE active = 1 "
            "ORDER BY department, name"
        )
        rows = cur.fetchall()

    print(f"Total doctors in database: {len(rows)}")
    print("─" * 50)
    current_dept = None
    for row in rows:
        if row["department"] != current_dept:
            current_dept = row["department"]
            print(f"\n{current_dept}:")
        print(f"  - {row['name']}")


if __name__ == "__main__":
    migrate_doctors()
