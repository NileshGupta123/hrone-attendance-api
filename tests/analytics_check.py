"""Independent check of the analytics endpoints against a RUNNING server (real MongoDB). Standard library only.

    1. start the app:   uvicorn app.main:app --port 8000
    2. in another terminal, from the repository root:   python tests/analytics_check.py
       (another port:   python tests/analytics_check.py http://127.0.0.1:8001)

How it works: it builds an awkward dataset through the public API (employees with no logs, a mid-month joiner, an
employee who joins after the month, an overnight shift, weekend records, half days, open records, LEAVE and ABSENT
days, days with no record). It then reads every record back with GET /attendance and computes the expected analytics
with plain Python loops and exact Decimal arithmetic, a completely different way from the aggregation pipelines,
and compares the two. It uses its own random department names, so it can be re-run any number of times.
"""
import json
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
MONTH = "2033-02"
FIRST = date(2033, 2, 1)
LAST = date(2033, 2, 28)
failures = []


# ------------------------------------------------------------------ plumbing
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode()


def ms(dt):
    return int(dt.timestamp()) * 1000


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"\n        {detail}"))
    if not condition:
        failures.append(name)


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-9


def must(status_expected, response, what):
    status, body = response
    if status != status_expected:
        raise SystemExit(f"setup failed: {what} -> {status} {body}")
    return body


# ------------------------------------------------------------------ dataset
def create_employee(dept, shift_start, shift_end, joined_on):
    for _ in range(30):
        code = f"EMP{random.randint(100000, 999999)}"
        status, _ = call("POST", "/employees", {
            "emp_code": code, "name": f"AC {code}", "email": "ac@example.com", "department": dept,
            "shift_start": shift_start, "shift_end": shift_end, "joined_on": joined_on,
        })
        if status == 201:
            return {"code": code, "dept": dept, "start": shift_start, "end": shift_end, "joined": date.fromisoformat(joined_on)}
    raise SystemExit("could not create an employee")


def at(day, hhmm):
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=IST)


def populate(rng, emp, first_day, kinds):
    """Create one record per chosen day through the API."""
    created = 0
    overnight = emp["end"] <= emp["start"]
    day = first_day
    while day <= LAST:
        kind = rng.choice(kinds)
        if kind is None:
            day += timedelta(days=1)
            continue
        start = at(day, emp["start"])
        end = at(day + timedelta(days=1), emp["end"]) if overnight else at(day, emp["end"])
        late = kind in ("late_ot", "late_only")
        punch_in = start + timedelta(minutes=rng.randint(11, 70) if late else rng.randint(-25, 9), seconds=rng.randint(0, 59))
        status = {"wfh": "WFH", "on_duty": "ON_DUTY"}.get(kind, "PRESENT")
        must(201, call("POST", "/attendance/punch-in", {"emp_code": emp["code"], "punched_at": ms(punch_in), "status": status}), "punch-in")
        punch_out = None
        if kind in ("full", "wfh", "on_duty", "late_only"):
            punch_out = end + timedelta(minutes=rng.randint(-40, 25), seconds=rng.randint(0, 59))
        elif kind == "late_ot":
            punch_out = end + timedelta(minutes=rng.randint(30, 120), seconds=rng.randint(0, 59))
        elif kind == "half":
            punch_out = punch_in + timedelta(minutes=rng.randint(180, 266))
        if punch_out is not None:
            must(200, call("POST", "/attendance/punch-out", {"emp_code": emp["code"], "punched_at": ms(punch_out)}), "punch-out")
        if kind in ("leave", "absent"):
            must(200, call("PATCH", f"/attendance/{emp['code']}/{day.isoformat()}",
                           {"status": kind.upper(), "reason": "analytics check", "regularized_by": "check"}), "patch")
        created += 1
        day += timedelta(days=1)
    return created


def read_records(code):
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({"emp_code": code, "date_from": "2033-02-01", "date_to": "2033-02-28", "page": page, "page_size": 100})
        status, body = call("GET", "/attendance?" + q)
        must(200, (status, body), "list")
        out += body["items"]
        if len(out) >= body["total"]:
            return out
        page += 1


# ------------------------------------------------------------------ expected values (plain Python)
def is_weekday(date_str):
    return date.fromisoformat(date_str).weekday() < 5


def half_up(value, places):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def expected_employee(emp, recs):
    counted_from = max(FIRST, emp["joined"])
    working = sum(1 for i in range((LAST - counted_from).days + 1) if (counted_from + timedelta(days=i)).weekday() < 5) if counted_from <= LAST else 0
    present = sum(Decimal("0.5") if r["half_day"] else Decimal(1) for r in recs if r["status"] in PRESENCE and is_weekday(r["date"]))
    return {
        "emp_code": emp["code"], "month": MONTH, "working_days": working, "present_days": float(present),
        "leave_days": sum(1 for r in recs if r["status"] == "LEAVE"),
        "late_count": sum(1 for r in recs if r["late_minutes"] > 0),
        "total_late_minutes": sum(r["late_minutes"] for r in recs),
        "total_overtime_minutes": sum(r["overtime_minutes"] for r in recs),
        "attendance_pct": None if working == 0 else half_up(present * 100 / Decimal(working), 2),
    }


def expected_department(dept, employees, records):
    members = [e for e in employees if e["dept"] == dept and e["joined"] <= LAST]
    if not members:
        return None
    recs = [r for e in members for r in records[e["code"]]]
    hours = [Decimal(str(r["work_hours"])) for r in recs if r["status"] in PRESENCE and r["work_hours"] is not None]
    return {
        "department": dept, "headcount": len(members),
        "present_days": float(sum(Decimal("0.5") if r["half_day"] else Decimal(1) for r in recs if r["status"] in PRESENCE and is_weekday(r["date"]))),
        "avg_work_hours": half_up(sum(hours) / len(hours), 2) if hours else None,
        "late_count": sum(1 for r in recs if r["late_minutes"] > 0),
        "total_late_minutes": sum(r["late_minutes"] for r in recs),
        "leave_count": sum(1 for r in recs if r["status"] == "LEAVE"),
        "on_duty_count": sum(1 for r in recs if r["status"] == "ON_DUTY"),
    }


def compare(name, actual, expected):
    ok = actual is not None and expected is not None and set(actual) == set(expected) and all(
        same(actual[k], expected[k]) if isinstance(expected[k], float) or expected[k] is None else actual[k] == expected[k]
        for k in expected
    )
    check(name, ok, f"\n        expected {expected}\n        actual   {actual}")


# ------------------------------------------------------------------ main
def main():
    rng = random.Random(20261011)
    tag = f"{random.randint(0, 10**6):06d}"
    dept_a, dept_b, dept_c, dept_empty = f"ACA{tag}", f"ACB{tag}", f"ACC{tag}", f"ACZ{tag}"
    mix = ["full"] * 8 + ["late_ot"] * 4 + ["late_only"] * 2 + ["half"] * 3 + ["open"] * 2 + ["wfh"] * 2 + ["on_duty"] * 3 + ["leave"] * 2 + ["absent"] * 2 + [None] * 5

    employees = [
        create_employee(dept_a, "09:30", "18:30", "2026-01-05"),   # regular, weekend records included
        create_employee(dept_a, "09:30", "18:30", "2033-02-15"),   # joins mid-month
        create_employee(dept_a, "09:30", "18:30", "2026-01-05"),   # no logs at all
        create_employee(dept_a, "09:30", "18:30", "2033-03-01"),   # joins after the month: not in headcount
        create_employee(dept_b, "22:00", "06:00", "2026-01-05"),   # overnight shift
        create_employee(dept_b, "09:00", "17:00", "2033-02-01"),   # joins on the first day
        create_employee(dept_c, "09:30", "18:30", "2026-01-05"),   # only LEAVE / ABSENT -> avg_work_hours null
        create_employee(dept_empty, "09:30", "18:30", "2033-03-10"),  # department whose headcount is 0 in Feb
    ]
    a1, a2, a3, a4, b1, b2, c1, z1 = employees
    print("building the dataset through the API ...")
    n = populate(rng, a1, FIRST, mix)
    n += populate(rng, a2, date(2033, 2, 15), mix)
    n += populate(rng, b1, FIRST, mix)
    n += populate(rng, b2, FIRST, mix)
    n += populate(rng, c1, FIRST, ["leave", "absent", None])
    print(f"{n} attendance records created\n")
    records = {e["code"]: read_records(e["code"]) for e in employees}

    # ---------------- employee monthly
    for e in employees[:7]:
        s, body = call("GET", f"/analytics/employees/{e['code']}/monthly?month={MONTH}")
        compare(f"monthly {e['code']} ({e['dept'][:3]}, joined {e['joined']}, {len(records[e['code']])} records)",
                body if s == 200 else None, expected_employee(e, records[e["code"]]))
    s, body = call("GET", f"/analytics/employees/{a1['code']}/monthly?month=2033-04")
    check("monthly for a month without logs: zeros, 21 working days, 0.0 %",
          s == 200 and body["working_days"] == 21 and body["present_days"] == 0 and body["attendance_pct"] == 0.0, body)
    s, body = call("GET", f"/analytics/employees/{a4['code']}/monthly?month={MONTH}")
    check("monthly for someone who has not joined yet: working_days 0, attendance_pct null",
          s == 200 and body["working_days"] == 0 and body["attendance_pct"] is None, body)
    check("monthly unknown employee -> 404", call("GET", f"/analytics/employees/EMP000000/monthly?month={MONTH}")[0] == 404)
    for bad in ("2033-13", "2033-2", "2033-00", "abcd", "2033-02-01", ""):
        check(f"monthly rejects month={bad!r} -> 422", call("GET", f"/analytics/employees/{a1['code']}/monthly?month={bad}")[0] == 422)
    check("monthly without month -> 422", call("GET", f"/analytics/employees/{a1['code']}/monthly")[0] == 422)

    # ---------------- department summary
    s, body = call("GET", f"/analytics/departments/summary?month={MONTH}")
    check("summary 200 with month echoed", s == 200 and body["month"] == MONTH, (s, body))
    items = body["items"] if s == 200 else []
    names = [i["department"] for i in items]
    check("summary sorted by department ascending", names == sorted(names), names)
    by_name = {i["department"]: i for i in items}
    for dept in (dept_a, dept_b, dept_c):
        compare(f"summary {dept}", by_name.get(dept), expected_department(dept, employees, records))
    check("department with headcount 0 is omitted", dept_empty not in by_name, names)
    check("A: headcount counts the no-log employee and the mid-month joiner but not the future joiner",
          by_name.get(dept_a, {}).get("headcount") == 3, by_name.get(dept_a))
    check("C: avg_work_hours is null when no record has work hours", by_name.get(dept_c, {}).get("avg_work_hours") is None, by_name.get(dept_c))
    s, body = call("GET", f"/analytics/departments/summary?month={MONTH}&department={dept_a}")
    check("summary filtered by department returns only it", s == 200 and [i["department"] for i in body["items"]] == [dept_a], body)
    if s == 200 and body["items"]:
        compare("filtered summary equals the unfiltered row", body["items"][0], expected_department(dept_a, employees, records))
    s, body = call("GET", f"/analytics/departments/summary?month={MONTH}&department=NoSuchDept{tag}")
    check("summary for an unknown department -> 200 with no items", s == 200 and body["items"] == [], (s, body))
    s, body = call("GET", f"/analytics/departments/summary?month=2033-03&department={dept_empty}")
    check("summary in the month the last employee joins -> headcount 1", s == 200 and len(body["items"]) == 1
          and body["items"][0]["headcount"] == 1 and body["items"][0]["avg_work_hours"] is None, (s, body))
    for bad in ("2033-13", "abc"):
        check(f"summary rejects month={bad!r} -> 422", call("GET", f"/analytics/departments/summary?month={bad}")[0] == 422)
    check("summary without month -> 422", call("GET", "/analytics/departments/summary")[0] == 422)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print("  -", name)
        sys.exit(1)
    print("ALL ANALYTICS CHECKS PASSED")


if __name__ == "__main__":
    main()