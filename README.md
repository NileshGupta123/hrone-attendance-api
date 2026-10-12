# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
Needs Python 3.11+ and MongoDB 6.0+ (developed on a local MongoDB; the grader uses 7.0).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows PowerShell; on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Set two environment variables, either in the shell or in a local `.env` file (not committed):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Both are required; the app refuses to start without them. Then:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created at startup and `/health` answers 200 as soon as MongoDB responds to a ping (503 otherwise). Interactive
docs are at `http://127.0.0.1:8000/docs`.

## What is implemented
Everything in the contract: employees, punch-in, punch-out, attendance list, PATCH regularization (with history),
employee monthly analytics, department summary, late leaderboard, department trend, and `/admin/explain/{endpoint}`.
Nothing is missing.

## Choices where the contract left room
- **Instants** are epoch milliseconds in the API (strict integers), BSON dates in MongoDB, truncated to whole seconds.
- **Punch-out errors** are checked in this order: unknown employee 404, no punch-in 404, already punched out 409,
  invalid time 422.
- **PATCH "changes nothing"** (422) is judged on `status`, `punch_in` and `punch_out`; derived fields follow from those.
  Of several concurrent PATCHes on one record, the losers get 409 (compare-and-set), so no history entry is lost.
- **Trend range limit:** `to - from + 1` days must be at most 92 (inclusive); more is 422.
- **Leaderboard** `limit` applies to the rank, so ties at the cutoff can return more than `limit` rows.
- **Index hints** are passed on the main queries so that the plan is always an index scan; `/admin/explain` uses the
  same hints and the same builders as the real endpoints.

## Tests (optional, not needed to run the app)
They talk to a running server (except the first), so start the app first.

```bash
pip install pytest
python -m pytest tests -q            # business rules, no database needed
python tests/live_check.py           # endpoints + concurrency
python tests/analytics_check.py      # analytics against brute-force results
python tests/seed_100k.py            # ~100k records in a separate database, attendance_big
python tests/explain_check.py        # run the app with MONGO_DB=attendance_big first
```

`REVIEW.md` lists the defects found in the starter, `DECISIONS.md` has the design answers.