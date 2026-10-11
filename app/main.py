"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, in this order:
  1. configuration and database connection
  2. indexes (created idempotently at startup)
  3. time helpers          (epoch ms <-> datetime, IST)
  4. rule helpers          (R1 - R5 and R8 rounding)
  5. serialisers           (Mongo document -> API JSON)
  6. request models        (validation)
  7. endpoints
"""
import logging
import os
import re
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and connection
# --------------------------------------------------------------------------- #
# load_dotenv() does NOT override variables that are already set, so real
# environment variables always win over the local .env file.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment variables or a local .env file)")

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones,
# so we never subtract aware from naive values or mistake local time for UTC.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")

GRACE_SECONDS = 10 * 60        # R2
MIN_OVERTIME_MINUTES = 30      # R3
HALF_DAY_HOURS = 4.5           # R5

# Valid range for an API instant, from the contract (EpochMillis).
MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# 2. Indexes
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """Create every index the app needs. create_index is idempotent for an identical spec."""
    global _indexes_ready
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    # attendance_logs: one record per (emp_code, date) - this is what makes punch-in race-safe
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # GET /attendance: default order is date desc, emp_code asc (also serves date-range filters)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    # GET /attendance with a status filter keeps the same order without a blocking sort
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    # punch-out: "this employee's most recent record whose punch_in <= punched_at"
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # DB not reachable yet: /health will answer 503 and retry
        log.warning("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 3. Time helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Always work with aware datetimes. A naive value coming from Mongo is UTC by definition."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Aware/naive datetime -> epoch milliseconds, using integer maths (no float rounding)."""
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def opt_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def now_ms() -> int:
    return _time.time_ns() // 1_000_000


def now_dt_ms_precision() -> datetime:
    """Current time as an aware datetime truncated to milliseconds (BSON dates have ms precision)."""
    return EPOCH + timedelta(milliseconds=now_ms())


def parse_hhmm(value: str) -> time:
    return time(int(value[0:2]), int(value[3:5]))


_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def parse_iso_date(value: str) -> date:
    """Strict YYYY-MM-DD. Raises ValueError for anything else (including 2026-02-30)."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


# --------------------------------------------------------------------------- #
# 4. Rule helpers (R1 - R5, R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, places: int) -> float:
    """R8: half-up rounding. Python's round() is banker's rounding on binary floats, which is wrong here."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Exact shift start and end instants (IST) for an attendance date. The end of an overnight shift
    falls on the next calendar day."""
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(shift_end), tzinfo=IST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    delta = int((as_utc(punch_in) - as_utc(shift_start_dt)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up, computed with Decimal (exact)."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes from shift end to punch-out, counted only if at least 30."""
    delta = int((as_utc(punch_out) - as_utc(shift_end_dt)).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: on the already-rounded work_hours."""
    return work_hours is not None and work_hours < HALF_DAY_HOURS


def derive_fields(
    emp: dict, status: str, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> dict:
    """All four derived fields in one place, used by punch-in, punch-out and PATCH, so they can never disagree."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start_dt, end_dt = shift_bounds(date_str, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(punch_in, start_dt)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, end_dt),
        "half_day": compute_half_day(work_hours),
    }


# --------------------------------------------------------------------------- #
# 5. Serialisers (Mongo -> API). _id never leaves the app; instants become epoch ms.
# --------------------------------------------------------------------------- #
def _ms_if_datetime(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": opt_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {
        field: {"from": _ms_if_datetime(change.get("from")), "to": _ms_if_datetime(change.get("to"))}
        for field, change in (entry.get("changes") or {}).items()
    }
    return {"at": opt_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """Legacy documents may omit late/overtime/half_day/history: missing means 0 / false / empty."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": opt_ms(doc.get("punch_in")),
        "punch_out": opt_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 6. Request models
# --------------------------------------------------------------------------- #
# An API instant: a strict integer (floats, strings and bools are rejected) inside the contract's range.
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]

HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, value: str) -> str:
        parse_iso_date(value)
        return value

    @model_validator(mode="after")
    def _shift_times_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]  # the only statuses punch-in accepts


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None  # epoch ms; defaults to now
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None  # epoch ms; defaults to now


class RegularizeIn(BaseModel):
    """Only these fields can be corrected. Derived fields sent by a client are ignored (extra fields are dropped)."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str) -> HTTPException:
    """A 422 with the same body shape FastAPI itself produces for validation errors."""
    return HTTPException(status_code=422, detail=[{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


def parse_date_param(name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        parse_iso_date(value)
    except ValueError:
        raise unprocessable(["query", name], f"{name} must be a valid date in YYYY-MM-DD format")
    return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is not reachable")
    if not _indexes_ready:  # the database was down at startup: finish the job now
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt_ms_precision()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items: list = []
    if skip < total:
        cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms())  # whole seconds (R1)
    date_str = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])  # R1, incl. overnight shifts
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derive_fields(emp, body.status, date_str, punched_at, None),
        "history": [],
    }
    try:
        # No "find first, then insert": the unique (emp_code, date) index is the referee, so of N
        # simultaneous requests exactly one insert succeeds and the rest raise DuplicateKeyError.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out_dt = ms_to_dt(body.punched_at if body.punched_at is not None else now_ms())
    # The employee's most recent record whose punch_in is at or before the punch-out. It is found by
    # punch_in (not by calendar date), so an overnight shift that crosses midnight works.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    in_dt = as_utc(record["punch_in"])
    if out_dt <= in_dt:
        raise unprocessable(["body", "punched_at"], "punched_at must be after the punch-in")
    if out_dt - in_dt > timedelta(hours=24):
        raise unprocessable(["body", "punched_at"], "punched_at is more than 24 hours after the punch-in")
    derived = derive_fields(emp, record["status"], record["date"], in_dt, out_dt)
    # Atomic compare-and-set: only the request that still sees punch_out == null can win. A second
    # simultaneous punch-out matches nothing and gets 409. late_minutes is left as stored at punch-in.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_attendance(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    """The filter for GET /attendance. Kept separate so /admin/explain can build the identical query."""
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = parse_date_param("date_from", date_from)
    date_to = parse_date_param("date_to", date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    items: list = []
    if skip < total:  # sort/skip/limit run inside MongoDB; nothing is loaded into Python except one page
        cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # NB: the path parameter is called `date` (it must match the URL template), so inside this function the
    # name `date` is the string, not the datetime.date class.
    try:
        parse_iso_date(date)
    except ValueError:
        raise unprocessable(["path", "date"], "date must be a valid date in YYYY-MM-DD format")
    # The contract's fields are not nullable: an explicit null is a client error, an omitted field means "unchanged".
    for name in ("status", "punch_in", "punch_out"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise unprocessable(["body", name], f"{name} must not be null")

    emp = get_employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    old_status = record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_status = body.status if body.status is not None else old_status
    in_supplied = body.punch_in is not None
    out_supplied = body.punch_out is not None

    if new_status in ("ABSENT", "LEAVE"):
        # ABSENT / LEAVE have no punch times: they are cleared, and supplying any is a contradiction.
        if in_supplied or out_supplied:
            raise unprocessable(["body"], f"status {new_status} has no punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if in_supplied else old_in  # whole seconds (R1)
        new_out = ms_to_dt(body.punch_out) if out_supplied else old_out
        if new_in is None:
            raise unprocessable(["body", "punch_in"], "a presence status requires a punch_in")
        new_in = as_utc(new_in)
        new_out = None if new_out is None else as_utc(new_out)
        if in_supplied and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != record["date"]:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if (in_supplied or out_supplied) and new_out is not None:
            if new_out <= new_in:
                raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise unprocessable(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    # "Changes nothing" is judged on what the client can change; the derived fields follow from those.
    if new_status == old_status and _same(new_in, old_in) and _same(new_out, old_out):
        raise unprocessable(["body"], "the request changes nothing")

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(emp, new_status, record["date"], new_in, new_out),  # recomputed from the final times
    }
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    changes = {
        f: {"from": old_values[f], "to": new_values[f]} for f in TRACKED_FIELDS if not _same(old_values[f], new_values[f])
    }
    entry = {"at": now_dt_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency (compare-and-set). The update only applies if the record still looks exactly like
    # the one we read: same status and punch times, and the history still has exactly n entries. If another
    # correction got in first, nothing matches and this request gets 409, so no history entry is ever lost.
    n = len(record.get("history") or [])
    condition: dict = {
        "_id": record["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        f"history.{n}": {"$exists": False},
    }
    if n > 0:
        condition[f"history.{n - 1}"] = {"$exists": True}
    set_fields = dict(new_values)
    update: dict = {"$set": set_fields}
    if n == 0:
        set_fields["history"] = [entry]  # also covers legacy documents that have no history field
    else:
        update["$push"] = {"history": entry}  # append, never overwrite
    updated = db.attendance_logs.find_one_and_update(condition, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(status_code=409, detail="the record was changed by another request; reload and retry")
    return serialize_attendance(updated)