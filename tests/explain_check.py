"""Checks /admin/explain/* and the response time of the analytics endpoints on a RUNNING server. Standard library only.

    1. python tests/seed_100k.py                                   (about 100,000 records in the database attendance_big)
    2. start the API on that database:
         PowerShell:  $env:MONGO_DB = "attendance_big"; uvicorn app.main:app --port 8000
         bash:        MONGO_DB=attendance_big uvicorn app.main:app --port 8000
    3. python tests/explain_check.py            (another port: python tests/explain_check.py http://127.0.0.1:8001)

For every explain call it asserts: HTTP 200, the body is {endpoint, collection, explain}, the plan contains IXSCAN and
the whole explain text contains no COLLSCAN. It prints how many index keys / documents each plan examined and what the
real endpoint's response time was. It also runs on a small database (then it only proves the shapes and the plans).
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
failures = []


def get(path):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null"), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode()
        return err.code, body, (time.perf_counter() - started) * 1000


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        {detail}"))
    if not ok:
        failures.append(name)


def find_all(node, key, out):
    """Collect every value stored under `key` anywhere in a nested explain document."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                out.append(v)
            find_all(v, key, out)
    elif isinstance(node, list):
        for v in node:
            find_all(v, key, out)
    return out


def stats(explain):
    keys = sum(x for x in find_all(explain, "totalKeysExamined", []) if isinstance(x, int))
    docs = sum(x for x in find_all(explain, "totalDocsExamined", []) if isinstance(x, int))
    millis = [x for x in find_all(explain, "executionTimeMillis", []) if isinstance(x, int)]
    return keys, docs, (max(millis) if millis else None)


def explain_case(label, endpoint, query, collection):
    status, body, _ = get(f"/admin/explain/{endpoint}?{urllib.parse.urlencode(query)}")
    if status != 200:
        check(f"explain {label}: HTTP 200", False, (status, body))
        return
    text = json.dumps(body)
    shape = set(body) == {"endpoint", "collection", "explain"} and body["endpoint"] == endpoint and body["collection"] == collection
    keys, docs, millis = stats(body["explain"])
    check(f"explain {label}: shape ok, collection {collection}", shape, list(body))
    check(f"explain {label}: plan contains IXSCAN", "IXSCAN" in text)
    check(f"explain {label}: no COLLSCAN anywhere", "COLLSCAN" not in text)
    print(f"        keys examined {keys}, docs examined {docs}, server time {millis} ms")


def timed(label, path, limit_ms=3000):
    status, body, ms = get(path)
    check(f"{label}: 200 in {ms:.0f} ms (limit {limit_ms} ms)", status == 200 and ms < limit_ms, (status, str(body)[:200], ms))
    return body


def main():
    status, body, _ = get("/employees?page_size=1")
    if status != 200:
        raise SystemExit(f"server not reachable or unhealthy: {status} {body}")
    total_logs = get("/attendance?page_size=1")[1]["total"]
    first = get("/employees?page_size=1")[1]["items"]
    print(f"database has {body['total']} employees and {total_logs} attendance records\n")
    emp = first[0]["emp_code"] if first else "EMP100000"
    dept = first[0]["department"] if first else "Engineering"
    month = "2026-05"

    print("--- explain: every endpoint, several parameter shapes")
    explain_case("attendance_list (no filter)", "attendance_list", {}, "attendance_logs")
    explain_case("attendance_list (page 3, size 50)", "attendance_list", {"page": 3, "page_size": 50}, "attendance_logs")
    explain_case("attendance_list (emp_code)", "attendance_list", {"emp_code": emp}, "attendance_logs")
    explain_case("attendance_list (date range)", "attendance_list", {"date_from": "2026-05-01", "date_to": "2026-05-31"}, "attendance_logs")
    explain_case("attendance_list (status)", "attendance_list", {"status": "LEAVE"}, "attendance_logs")
    explain_case("attendance_list (status + date range)", "attendance_list", {"status": "PRESENT", "date_from": "2026-05-01", "date_to": "2026-05-31"}, "attendance_logs")
    explain_case("attendance_list (emp_code + status + range)", "attendance_list", {"emp_code": emp, "status": "PRESENT", "date_from": "2026-05-01", "date_to": "2026-05-31"}, "attendance_logs")
    explain_case("employee_monthly", "employee_monthly", {"emp_code": emp, "month": month}, "attendance_logs")
    explain_case("department_summary (all departments)", "department_summary", {"month": month}, "employees")
    explain_case("department_summary (one department)", "department_summary", {"month": month, "department": dept}, "employees")
    explain_case("late_leaderboard", "late_leaderboard", {"month": month}, "attendance_logs")
    explain_case("late_leaderboard (department, limit 5)", "late_leaderboard", {"month": month, "department": dept, "limit": 5}, "attendance_logs")
    explain_case("department_trend", "department_trend", {"department": dept, "from": "2026-05-01", "to": "2026-07-30"}, "attendance_logs")

    print("\n--- explain: errors")
    for label, endpoint, query in (
        ("attendance_list with a bad date", "attendance_list", {"date_from": "2026-13-01"}),
        ("employee_monthly without emp_code", "employee_monthly", {"month": month}),
        ("employee_monthly without month", "employee_monthly", {"emp_code": emp}),
        ("department_summary without month", "department_summary", {}),
        ("late_leaderboard without month", "late_leaderboard", {}),
        ("late_leaderboard with limit 0", "late_leaderboard", {"month": month, "limit": 0}),
        ("department_trend without department", "department_trend", {"from": "2026-05-01", "to": "2026-05-10"}),
        ("department_trend without from", "department_trend", {"department": dept, "to": "2026-05-10"}),
        ("department_trend without to", "department_trend", {"department": dept, "from": "2026-05-01"}),
        ("an unknown endpoint name", "nonsense", {}),
    ):
        check(f"explain {label} -> 422", get(f"/admin/explain/{endpoint}?{urllib.parse.urlencode(query)}")[0] == 422)

    print("\n--- the real endpoints on this amount of data")
    timed("GET /attendance (no filter)", "/attendance")
    timed("GET /attendance (last page)", f"/attendance?page={max(1, total_logs // 100)}&page_size=100")
    timed("monthly", f"/analytics/employees/{emp}/monthly?month={month}")
    summary = timed("department summary", f"/analytics/departments/summary?month={month}")
    timed("late leaderboard", f"/analytics/leaderboard/late?month={month}")
    trend = timed("department trend (92 days)", f"/analytics/departments/{dept}/trend?from=2026-04-01&to=2026-06-30")
    if isinstance(trend, dict):
        check("trend returns 91 rows for 2026-04-01..2026-06-30", len(trend["items"]) == 91, len(trend["items"]))
    if isinstance(summary, dict):
        check("summary returns one row per department with employees", len(summary["items"]) >= 1)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print("  -", name)
        sys.exit(1)
    print("ALL EXPLAIN CHECKS PASSED")


if __name__ == "__main__":
    main()