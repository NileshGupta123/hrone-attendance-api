"""Fills a SEPARATE database with about 100,000 attendance records, so you can look at the query plans and the
response times on a realistic amount of data. Standard library + pymongo + python-dotenv only.

    python tests/seed_100k.py                  # writes into the database  attendance_big  (never your normal one)
    python tests/seed_100k.py --force          # replaces that database if it already has data

Then start the API against that database (a real environment variable beats the local .env file):

    PowerShell:   $env:MONGO_DB = "attendance_big"; uvicorn app.main:app --port 8000
    bash:         MONGO_DB=attendance_big uvicorn app.main:app --port 8000

and run   python tests/explain_check.py   in another terminal. Afterwards close that server and start the normal one.
The data is random but fixed (seeded), 650 employees in 8 departments, every weekday 2026-03-02 .. 2026-09-30.
"""
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = "attendance_big"
IST = timezone(timedelta(hours=5, minutes=30))
DEPARTMENTS = ["Engineering", "Sales", "Support", "HR", "Finance", "Ops", "Design", "Legal"]
FIRST, LAST = date(2026, 3, 2), date(2026, 9, 30)


def main() -> None:
    client = MongoClient(URI, tz_aware=True)
    db = client[DB_NAME]
    if db.attendance_logs.estimated_document_count() and "--force" not in sys.argv:
        raise SystemExit(f"{DB_NAME} already has data. Use --force to replace it.")
    client.drop_database(DB_NAME)
    rng = random.Random(7)

    employees = []
    for i in range(650):
        employees.append({
            "emp_code": f"EMP{100000 + i}", "name": f"Employee {i}", "email": f"e{i}@example.com",
            "department": DEPARTMENTS[i % len(DEPARTMENTS)], "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": (date(2025, 1, 1) + timedelta(days=rng.randint(0, 600))).isoformat(),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
    db.employees.insert_many(employees)

    batch, total = [], 0
    for emp in employees:
        joined = date.fromisoformat(emp["joined_on"])
        day = max(FIRST, joined)
        while day <= LAST:
            if day.weekday() < 5 or rng.random() < 0.02:  # a few weekend records as well
                roll = rng.random()
                status = "PRESENT" if roll < 0.84 else "WFH" if roll < 0.90 else "ON_DUTY" if roll < 0.93 else "LEAVE" if roll < 0.97 else "ABSENT"
                doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
                       "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                       "half_day": False, "history": []}
                if status in ("PRESENT", "WFH", "ON_DUTY"):
                    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST)
                    pin = start + timedelta(minutes=rng.randint(-20, 75), seconds=rng.randint(0, 59))
                    pout = datetime(day.year, day.month, day.day, 18, 30, tzinfo=IST) + timedelta(minutes=rng.randint(-200, 120))
                    secs = int((pin - start).total_seconds())
                    hours = round((pout - pin).total_seconds() / 3600, 2)
                    doc.update({"punch_in": pin.astimezone(timezone.utc), "punch_out": pout.astimezone(timezone.utc),
                                "work_hours": hours, "late_minutes": secs // 60 if secs > 600 else 0,
                                "overtime_minutes": max(0, int((pout - datetime(day.year, day.month, day.day, 18, 30, tzinfo=IST)).total_seconds() // 60)) if (pout - datetime(day.year, day.month, day.day, 18, 30, tzinfo=IST)).total_seconds() >= 1800 else 0,
                                "half_day": hours < 4.5})
                batch.append(doc)
                if len(batch) == 5000:
                    db.attendance_logs.insert_many(batch, ordered=False)
                    total += len(batch)
                    batch = []
            day += timedelta(days=1)
    if batch:
        db.attendance_logs.insert_many(batch, ordered=False)
        total += len(batch)
    print(f"{DB_NAME}: {len(employees)} employees, {total} attendance records. Indexes are created when the API starts.")


if __name__ == "__main__":
    main()