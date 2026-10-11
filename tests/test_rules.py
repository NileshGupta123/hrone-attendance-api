"""Pure-Python tests for the business rules (no MongoDB needed).

Run from the repository root:   pip install pytest   then   python -m pytest tests -q
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test_unused")

from app.main import (  # noqa: E402
    IST,
    EmployeeIn,
    EpochMs,
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    derive_fields,
    dt_to_ms,
    ms_to_dt,
    round_half_up,
    shift_bounds,
)


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# ---------------- R2: late ----------------
@pytest.mark.parametrize(
    "h,mi,s,expected",
    [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 30, 0, 0), (9, 0, 0, 0), (9, 39, 59, 0), (9, 41, 0, 11)],
)
def test_late_minutes_shift_0930(h, mi, s, expected):
    start, _ = shift_bounds("2026-07-06", "09:30", "18:30")
    assert compute_late_minutes(ist(2026, 7, 6, h, mi, s), start) == expected


def test_late_counts_from_shift_start_not_from_grace_end():
    start, _ = shift_bounds("2026-07-06", "09:30", "18:30")
    assert compute_late_minutes(ist(2026, 7, 6, 10, 5), start) == 35


# ---------------- R3: overtime ----------------
@pytest.mark.parametrize("h,mi,s,expected", [(18, 30, 0, 0), (18, 59, 59, 0), (19, 0, 0, 30), (19, 10, 0, 40), (18, 35, 0, 0)])
def test_overtime_minimum_30(h, mi, s, expected):
    _, end = shift_bounds("2026-07-06", "09:30", "18:30")
    assert compute_overtime(ist(2026, 7, 6, h, mi, s), end) == expected


# ---------------- R4: half-up work hours ----------------
def test_work_hours_half_up_not_bankers():
    t = ist(2026, 7, 6, 9, 0)
    out = t + timedelta(seconds=32850)  # exactly 9.125 h
    assert round(32850 / 3600, 2) == 9.12  # what Python's round() would give
    assert compute_work_hours(t, out) == 9.13


def test_work_hours_samples():
    t = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)
    assert compute_work_hours(t, datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc)) == 9.12  # 9h07m
    t = datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc)
    assert compute_work_hours(t, datetime(2026, 7, 6, 13, 40, tzinfo=timezone.utc)) == 9.08


# ---------------- R1: attendance date / overnight ----------------
def test_normal_shift_date_is_ist_date_not_utc_date():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th: the attendance date must be the 7th.
    assert attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"


@pytest.mark.parametrize(
    "when,expected",
    [
        (ist(2026, 7, 6, 21, 55), "2026-07-06"),
        (ist(2026, 7, 7, 1, 0), "2026-07-06"),
        (ist(2026, 7, 7, 5, 59, 59), "2026-07-06"),
        (ist(2026, 7, 7, 6, 0, 0), "2026-07-07"),  # not *earlier than* shift_end
        (ist(2026, 7, 7, 22, 0), "2026-07-07"),
    ],
)
def test_overnight_attendance_date(when, expected):
    assert attendance_date(when, "22:00", "06:00") == expected


def test_overnight_sample_record():
    emp = {"shift_start": "22:00", "shift_end": "06:00"}
    pin = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)   # 21:55 IST
    pout = datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc)   # 06:40 IST next day
    d = attendance_date(pin, emp["shift_start"], emp["shift_end"])
    assert d == "2026-07-06"
    assert derive_fields(emp, "PRESENT", d, pin, pout) == {
        "work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False,
    }


def test_overnight_late_minutes():
    start, _ = shift_bounds("2026-07-06", "22:00", "06:00")
    assert compute_late_minutes(ist(2026, 7, 6, 22, 11), start) == 11


# ---------------- R5 and derive_fields ----------------
def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    pin = ist(2026, 7, 7, 9, 30)
    assert derive_fields(emp, "WFH", "2026-07-07", pin, pin + timedelta(seconds=16182))["half_day"] is False  # 4.495 -> 4.50
    assert derive_fields(emp, "WFH", "2026-07-07", pin, pin + timedelta(seconds=16164))["half_day"] is True   # 4.49
    assert derive_fields(emp, "WFH", "2026-07-07", pin, pin + timedelta(hours=3, minutes=30))["half_day"] is True


def test_derive_fields_absent_leave_and_open_record():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    assert derive_fields(emp, "ABSENT", "2026-07-08", None, None) == zero
    assert derive_fields(emp, "LEAVE", "2026-07-08", None, None) == zero
    pin = ist(2026, 7, 6, 10, 5)
    assert derive_fields(emp, "PRESENT", "2026-07-06", pin, None) == {
        "work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False,
    }


# ---------------- R8 rounding ----------------
def test_round_half_up():
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(1.005, 2) == 1.01


# ---------------- time conversion ----------------
def test_ms_roundtrip_and_truncation():
    ms = 1_783_312_500_900  # .900 s
    dt = ms_to_dt(ms)
    assert dt.microsecond == 0 and dt.tzinfo is not None
    assert dt_to_ms(dt) == 1_783_312_500_000
    assert dt_to_ms(datetime(2026, 7, 6, 3, 58)) == dt_to_ms(datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc))  # naive = UTC


# ---------------- request validation ----------------
@pytest.mark.parametrize("bad", [1.0, 1_783_312_500.0, "1783312500000", True, 1_783_312_500, -5, 4_102_444_800_001])
def test_epoch_ms_rejects(bad):
    with pytest.raises(ValidationError):
        TypeAdapter(EpochMs).validate_python(bad)


def test_epoch_ms_accepts_valid():
    assert TypeAdapter(EpochMs).validate_python(1_783_312_500_000) == 1_783_312_500_000


GOOD = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-05"}


def test_employee_defaults_and_ignored_extras():
    e = EmployeeIn(**GOOD, created_at=5, _id="x")
    assert (e.shift_start, e.shift_end) == ("09:30", "18:30")
    assert "created_at" not in e.model_dump()


@pytest.mark.parametrize(
    "patch",
    [
        {"emp_code": "emp0001"}, {"emp_code": "EMP001"}, {"emp_code": "EMP1234567"}, {"emp_code": "EMP\u0660\u0660\u0660\u0661"},
        {"emp_code": "EMP0001\n"}, {"name": ""}, {"email": "nope"}, {"department": ""},
        {"shift_start": "9:30"}, {"shift_start": "24:00"}, {"shift_start": "09:30", "shift_end": "09:30"},
        {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"joined_on": "05-01-2026"},
        {"name": 5},
    ],
)
def test_employee_rejects(patch):
    with pytest.raises(ValidationError):
        EmployeeIn(**{**GOOD, **patch})