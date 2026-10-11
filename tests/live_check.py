"""End-to-end check against a RUNNING server (real MongoDB). Standard library only.

    1. start the app:   uvicorn app.main:app --port 8000
    2. in another terminal, from the repository root:   python tests/live_check.py
       (another port:   python tests/live_check.py http://127.0.0.1:8001)

It creates its own random employees, so it can be re-run any number of times.
Exit code 0 = every check passed.
"""
import json
import random
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))
failures = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, raw.decode()


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"   -> {detail}"))
    if not condition:
        failures.append(name)


def new_employee(shift_start="09:30", shift_end="18:30", department="LiveCheck"):
    for _ in range(20):
        code = f"EMP{random.randint(100000, 999999)}"
        status, body = call("POST", "/employees", {
            "emp_code": code, "name": "Live Check", "email": "live@example.com",
            "department": department, "shift_start": shift_start, "shift_end": shift_end, "joined_on": "2026-01-05",
        })
        if status == 201:
            return code
    raise SystemExit("could not create an employee")


def main():
    status, _ = call("GET", "/health")
    check("health is 200", status == 200, status)

    # ---------------- punch-in ----------------
    a = new_employee()
    s, r = call("POST", "/attendance/punch-in", {"emp_code": a, "punched_at": ms(2031, 3, 3, 9, 40, 1)})
    check("punch-in 201", s == 201, (s, r))
    check("punch-in date/late (09:40:01 -> late 10)", s == 201 and r["date"] == "2031-03-03" and r["late_minutes"] == 10, r)
    check("punch-in shape (null punch_out/work_hours, no id)",
          s == 201 and r["punch_out"] is None and r["work_hours"] is None and r["half_day"] is False
          and r["history"] == [] and "id" not in r and "_id" not in r, r)
    check("punch-in stores whole seconds", s == 201 and r["punch_in"] % 1000 == 0, r)
    s, _ = call("POST", "/attendance/punch-in", {"emp_code": a, "punched_at": ms(2031, 3, 3, 11, 0)})
    check("second punch-in same date -> 409", s == 409, s)
    s, r = call("POST", "/attendance/punch-in", {"emp_code": a, "punched_at": ms(2031, 3, 3, 9, 40, 0) + 900})
    check("sub-second duplicate still 409", s == 409, s)
    check("unknown employee -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP000000"})[0] == 404)
    for bad in ({"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "nope"}, {"punched_at": 1_783_312_500},
                {"punched_at": 1.5}, {"punched_at": "1783312500000"}, {"punched_at": True}, {"punched_at": -1}):
        s, _ = call("POST", "/attendance/punch-in", {"emp_code": a, **bad})
        check(f"punch-in rejects {bad} -> 422", s == 422, s)
    s, r = call("POST", "/attendance/punch-in", {"emp_code": new_employee()})
    check("punch-in without punched_at uses now -> 201", s == 201, (s, r))

    # ---------------- punch-out ----------------
    s, r = call("POST", "/attendance/punch-out", {"emp_code": a, "punched_at": ms(2031, 3, 3, 19, 10)})
    check("punch-out 200", s == 200, (s, r))
    check("punch-out values (9.50 h, overtime 40, not half day, late kept)",
          s == 200 and r["work_hours"] == 9.5 and r["overtime_minutes"] == 40 and r["half_day"] is False
          and r["late_minutes"] == 10, r)
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": a, "punched_at": ms(2031, 3, 3, 19, 30)})
    check("second punch-out -> 409", s == 409, s)
    check("punch-out unknown employee -> 404", call("POST", "/attendance/punch-out", {"emp_code": "EMP000000"})[0] == 404)
    check("punch-out with no punch-in -> 404", call("POST", "/attendance/punch-out", {"emp_code": new_employee()})[0] == 404)

    h = new_employee()
    call("POST", "/attendance/punch-in", {"emp_code": h, "punched_at": ms(2031, 3, 4, 9, 30)})
    s, r = call("POST", "/attendance/punch-out", {"emp_code": h, "punched_at": ms(2031, 3, 4, 13, 0)})
    check("half day (3.5 h) flagged", s == 200 and r["work_hours"] == 3.5 and r["half_day"] is True, (s, r))

    b = new_employee("22:00", "06:00")  # overnight shift
    s, r = call("POST", "/attendance/punch-in", {"emp_code": b, "punched_at": ms(2031, 3, 3, 21, 55)})
    check("overnight punch-in belongs to the start day", s == 201 and r["date"] == "2031-03-03", (s, r))
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": b, "punched_at": ms(2031, 3, 3, 21, 55)})
    check("punch-out == punch-in -> 422", s == 422, s)
    s, _ = call("POST", "/attendance/punch-out", {"emp_code": b, "punched_at": ms(2031, 3, 4, 22, 56)})
    check("punch-out more than 24 h later -> 422", s == 422, s)
    s, r = call("POST", "/attendance/punch-out", {"emp_code": b, "punched_at": ms(2031, 3, 4, 6, 40)})
    check("overnight punch-out across midnight (8.75 h, overtime 40)",
          s == 200 and r["date"] == "2031-03-03" and r["work_hours"] == 8.75 and r["overtime_minutes"] == 40, (s, r))
    c = new_employee("22:00", "06:00")
    s, r = call("POST", "/attendance/punch-in", {"emp_code": c, "punched_at": ms(2031, 3, 4, 1, 0)})
    check("overnight early-morning punch-in -> previous day, late 180", s == 201 and r["date"] == "2031-03-03"
          and r["late_minutes"] == 180, (s, r))

    # ---------------- list ----------------
    for day in (4, 5, 6):
        call("POST", "/attendance/punch-in", {"emp_code": a, "punched_at": ms(2031, 3, day, 9, 30)})
    s, r = call("GET", f"/attendance?emp_code={a}")
    check("list filters by employee, total 4", s == 200 and r["total"] == 4, (s, r))
    dates = [i["date"] for i in r["items"]] if s == 200 else []
    check("list sorted by date descending", dates == sorted(dates, reverse=True) and len(dates) == 4, dates)
    s, r = call("GET", f"/attendance?emp_code={a}&page=2&page_size=3")
    check("pagination: page 2 of size 3 has the 1 remaining row, total 4",
          s == 200 and len(r["items"]) == 1 and r["total"] == 4 and r["page"] == 2, (s, r))
    s, r = call("GET", f"/attendance?emp_code={a}&date_from=2031-03-04&date_to=2031-03-05")
    check("date range is inclusive", s == 200 and r["total"] == 2, (s, r))
    s, r = call("GET", f"/attendance?emp_code={a}&status=WFH")
    check("status filter", s == 200 and r["total"] == 0, (s, r))
    s, r = call("GET", "/attendance?date_from=2031-03-03&date_to=2031-03-03&page_size=100")
    both = [i["emp_code"] for i in r["items"]] if s == 200 else []
    check("same date ordered by emp_code ascending", both == sorted(both) and len(both) >= 2, both)
    for q in ("date_from=2031-03-05&date_to=2031-03-04", "date_from=2031-3-5", "date_to=2031-02-30", "status=BAD",
              "page=0", "page_size=0", "page_size=101"):
        check(f"list rejects ?{q} -> 422", call("GET", "/attendance?" + q)[0] == 422)

    # ---------------- PATCH (regularize) ----------------
    p = new_employee()
    call("POST", "/attendance/punch-in", {"emp_code": p, "punched_at": ms(2031, 5, 5, 10, 5)})
    call("POST", "/attendance/punch-out", {"emp_code": p, "punched_at": ms(2031, 5, 5, 19, 10)})
    url = f"/attendance/{p}/2031-05-05"
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}

    s, r = call("PATCH", url, {**base, "punch_in": ms(2031, 5, 5, 9, 28), "work_hours": 99, "late_minutes": 99})
    check("PATCH punch_in -> 200, derived recomputed (late 0, 9.70 h), client-sent derived ignored",
          s == 200 and r["late_minutes"] == 0 and r["work_hours"] == 9.7 and r["overtime_minutes"] == 40, (s, r))
    h1 = r["history"][0] if s == 200 and r["history"] else {}
    check("history entry: only fields that actually changed",
          set(h1.get("changes", {})) == {"punch_in", "work_hours", "late_minutes"}, h1)
    check("history entry values (epoch ms for punch_in, late 35 -> 0, by/reason)",
          h1.get("changes", {}).get("punch_in") == {"from": ms(2031, 5, 5, 10, 5), "to": ms(2031, 5, 5, 9, 28)}
          and h1["changes"]["late_minutes"] == {"from": 35, "to": 0} and h1.get("by") == "hr.admin"
          and h1.get("reason") == "biometric glitch" and isinstance(h1.get("at"), int), h1)
    check("PATCH identical values again -> 422 (changes nothing)",
          call("PATCH", url, {**base, "punch_in": ms(2031, 5, 5, 9, 28)})[0] == 422)
    check("PATCH with no changeable field -> 422", call("PATCH", url, base)[0] == 422)
    check("PATCH punch_in on another date -> 422", call("PATCH", url, {**base, "punch_in": ms(2031, 5, 6, 9, 30)})[0] == 422)
    check("PATCH punch_out before punch_in -> 422", call("PATCH", url, {**base, "punch_out": ms(2031, 5, 5, 9, 0)})[0] == 422)
    check("PATCH punch_out > 24 h after punch_in -> 422", call("PATCH", url, {**base, "punch_out": ms(2031, 5, 6, 10, 0)})[0] == 422)
    check("PATCH ABSENT together with punch_in -> 422",
          call("PATCH", url, {**base, "status": "ABSENT", "punch_in": ms(2031, 5, 5, 9, 30)})[0] == 422)
    check("PATCH explicit null punch_in -> 422", call("PATCH", url, {**base, "punch_in": None})[0] == 422)
    check("PATCH reason too short -> 422", call("PATCH", url, {"reason": "abc", "regularized_by": "x", "status": "WFH"})[0] == 422)
    check("PATCH missing regularized_by -> 422", call("PATCH", url, {"reason": "long enough", "status": "WFH"})[0] == 422)
    check("PATCH seconds-looking punch_in -> 422", call("PATCH", url, {**base, "punch_in": 1_783_312_500})[0] == 422)
    check("PATCH bad date in path -> 422", call("PATCH", f"/attendance/{p}/2031-02-30", {**base, "status": "WFH"})[0] == 422)
    check("PATCH unknown date -> 404", call("PATCH", f"/attendance/{p}/2031-05-06", {**base, "status": "WFH"})[0] == 404)
    check("PATCH unknown employee -> 404", call("PATCH", "/attendance/EMP000000/2031-05-05", {**base, "status": "WFH"})[0] == 404)

    s, r = call("PATCH", url, {**base, "status": "ABSENT"})
    check("PATCH status ABSENT clears punch times and derived fields",
          s == 200 and r["punch_in"] is None and r["punch_out"] is None and r["work_hours"] is None
          and r["overtime_minutes"] == 0 and r["late_minutes"] == 0 and r["half_day"] is False and len(r["history"]) == 2, (s, r))
    check("ABSENT history lists status/punch_in/punch_out/work_hours/overtime_minutes",
          s == 200 and set(r["history"][1]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "overtime_minutes"},
          r["history"][1] if s == 200 else r)
    check("PATCH back to PRESENT without a punch_in -> 422", call("PATCH", url, {**base, "status": "PRESENT"})[0] == 422)
    s, r = call("PATCH", url, {**base, "status": "PRESENT", "punch_in": ms(2031, 5, 5, 9, 30)})
    check("PATCH ABSENT -> PRESENT with punch_in -> 200, open record", s == 200 and r["punch_out"] is None
          and r["work_hours"] is None and len(r["history"]) == 3, (s, r))
    s, r = call("PATCH", url, {**base, "punch_out": ms(2031, 5, 5, 13, 30)})
    check("PATCH punch_out gives 4.00 h and half_day", s == 200 and r["work_hours"] == 4.0 and r["half_day"] is True
          and set(r["history"][3]["changes"]) == {"punch_out", "work_hours", "half_day"}, (s, r))
    s, r = call("GET", f"/attendance?emp_code={p}")
    check("history persisted: 4 entries via GET /attendance", s == 200 and len(r["items"][0]["history"]) == 4, (s, r))

    o = new_employee("22:00", "06:00")
    call("POST", "/attendance/punch-in", {"emp_code": o, "punched_at": ms(2031, 5, 5, 22, 0)})
    s, r = call("PATCH", f"/attendance/{o}/2031-05-05", {**base, "punch_in": ms(2031, 5, 6, 0, 30)})
    check("overnight PATCH: 00:30 next morning is still the same attendance date (late 150)",
          s == 200 and r["date"] == "2031-05-05" and r["late_minutes"] == 150, (s, r))
    s, _ = call("PATCH", f"/attendance/{o}/2031-05-05", {**base, "punch_in": ms(2031, 5, 6, 7, 0)})
    check("overnight PATCH: 07:00 belongs to the next day -> 422", s == 422, s)

    q = new_employee()
    call("POST", "/attendance/punch-in", {"emp_code": q, "punched_at": ms(2031, 6, 2, 9, 30)})
    call("POST", "/attendance/punch-out", {"emp_code": q, "punched_at": ms(2031, 6, 2, 18, 30)})
    qurl = f"/attendance/{q}/2031-06-02"
    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(lambda i: call("PATCH", qurl, {**base, "punch_in": ms(2031, 6, 2, 9, 31 + i)})[0], range(10)))
    wins = codes.count(200)
    check("10 concurrent PATCHes: only 200 or 409, at least one 200", set(codes) <= {200, 409} and wins >= 1, sorted(codes))
    s, r = call("GET", f"/attendance?emp_code={q}")
    hist = r["items"][0]["history"] if s == 200 else []
    check(f"no lost history: {wins} successes -> {wins} history entries", len(hist) == wins, (wins, len(hist)))
    chain_ok = all(hist[i]["changes"]["punch_in"]["from"] == hist[i - 1]["changes"]["punch_in"]["to"] for i in range(1, len(hist)))
    check("history forms an unbroken chain (each 'from' is the previous 'to')", chain_ok, hist)
    check("final punch_in equals the last history 'to'", bool(hist) and r["items"][0]["punch_in"] == hist[-1]["changes"]["punch_in"]["to"])

    # ---------------- concurrency ----------------
    d = new_employee()
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(lambda _: call("POST", "/attendance/punch-in",
                                             {"emp_code": d, "punched_at": ms(2031, 4, 7, 9, 30)})[0], range(20)))
    check("20 simultaneous punch-ins -> exactly one 201, rest 409",
          codes.count(201) == 1 and codes.count(409) == 19, sorted(codes))
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(lambda _: call("POST", "/attendance/punch-out",
                                             {"emp_code": d, "punched_at": ms(2031, 4, 7, 18, 30)})[0], range(20)))
    check("20 simultaneous punch-outs -> exactly one 200, rest 409",
          codes.count(200) == 1 and codes.count(409) == 19, sorted(codes))
    code = f"EMP{random.randint(100000, 999999)}"
    payload = {"emp_code": code, "name": "Race", "email": "r@example.com", "department": "LiveCheck", "joined_on": "2026-01-05"}
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(lambda _: call("POST", "/employees", payload)[0], range(20)))
    check("20 simultaneous employee creates -> exactly one 201, rest 409",
          codes.count(201) == 1 and codes.count(409) == 19, sorted(codes))

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print("  -", name)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()