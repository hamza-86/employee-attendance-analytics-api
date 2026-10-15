"""
Employee Attendance & Analytics API - HROne Engineering Assignment
Contract version: openapi.yaml v2.0.0

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment; real environment variables win)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

import json
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined")
        db.employees.create_index([("joined_on", 1)], name="idx_employees_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_attendance_emp_date_unique")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_attendance_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_punch_in")
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_attendance_date_late")
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_attendance_emp_date_desc")
        db.attendance_logs.create_index([("date", 1), ("status", 1)], name="idx_attendance_date_status")
    except PyMongoError:
        # If MongoDB is currently unavailable at import/startup, indexes will retry on ping
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(value: float | int | Decimal | None, decimals: int) -> float | None:
    """R8: Decimal round half-up (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def truncate_ms_to_seconds(ms: int) -> int:
    """R1: Whole-second truncation for instants."""
    return (ms // 1000) * 1000


def ms_to_utc_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    trunc_ms = truncate_ms_to_seconds(ms)
    return datetime.fromtimestamp(trunc_ms / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt: datetime | int | float | None) -> int | None:
    """Convert a BSON/Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return truncate_ms_to_seconds(int(dt))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.astimezone(timezone.utc).timestamp() * 1000)
    return truncate_ms_to_seconds(ms)


def parse_time_str(t_str: str) -> tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start implies an overnight shift."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Attendance date in IST. Overnight shifts prior to shift_end belong to previous day."""
    punch_in_ist = punch_in_dt.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = parse_time_str(shift_end)
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Grace period 10:00. Strictly > 10 min late counts full floor minutes from shift_start."""
    punch_in_ist = punch_in_dt.astimezone(IST)
    h, m = parse_time_str(shift_start)
    att_d = date.fromisoformat(attendance_date)
    start_ist = datetime(att_d.year, att_d.month, att_d.day, h, m, 0, tzinfo=IST)
    diff_sec = int((punch_in_ist - start_ist).total_seconds())
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """R3: Whole minutes between shift_end and punch_out, only if >= 30, else 0."""
    punch_out_ist = punch_out_dt.astimezone(IST)
    end_h, end_m = parse_time_str(shift_end)
    att_d = date.fromisoformat(attendance_date)
    if is_overnight_shift(shift_start, shift_end):
        end_d = att_d + timedelta(days=1)
    else:
        end_d = att_d
    end_ist = datetime(end_d.year, end_d.month, end_d.day, end_h, end_m, 0, tzinfo=IST)
    diff_sec = int((punch_out_ist - end_ist).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float | None) -> bool:
    """R5: half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history(history: list[dict]) -> list[dict]:
    out = []
    for h in history or []:
        entry = {
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {},
        }
        for k, v in h.get("changes", {}).items():
            entry["changes"][k] = {
                "from": dt_to_epoch_ms(v["from"]) if k in ("punch_in", "punch_out") else v["from"],
                "to": dt_to_epoch_ms(v["to"]) if k in ("punch_in", "punch_out") else v["to"],
            }
        out.append(entry)
    return out


def format_attendance_record(doc: dict) -> dict:
    """Format attendance document for HTTP response (no _id, epoch ms instants)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": serialize_history(doc.get("history") or []),
    }


def format_employee_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AllStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]

    @model_validator(mode="after")
    def validate_employee(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[AllStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        init_indexes()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_record(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_ms = truncate_ms_to_seconds(body.punched_at)
        punch_in_dt = datetime.fromtimestamp(punch_in_ms / 1000, tz=timezone.utc)
    else:
        now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
        punch_in_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_ms = truncate_ms_to_seconds(body.punched_at)
        punch_out_dt = datetime.fromtimestamp(punch_out_ms / 1000, tz=timezone.utc)
    else:
        now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
        punch_out_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    # Find the employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt, "$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        # Check if the employee has any punch-in (e.g. in the future relative to punched_at)
        any_punch_in = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", 1)],
        )
        if any_punch_in:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = record["punch_in"]
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]] = None,
    date_to: Optional[Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]] = None,
    status: Optional[AllStatus] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")],
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            punch_in_ms = truncate_ms_to_seconds(body.punch_in)
            final_punch_in = datetime.fromtimestamp(punch_in_ms / 1000, tz=timezone.utc)
        else:
            final_punch_in = rec.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        calc_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            punch_out_ms = truncate_ms_to_seconds(body.punch_out)
            final_punch_out = datetime.fromtimestamp(punch_out_ms / 1000, tz=timezone.utc)
        else:
            final_punch_out = rec.get("punch_out")

        new_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)

        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime_minutes(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check which fields actually changed
    old_status = rec["status"]
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)

    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if final_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if final_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    new_history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = rec.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current_history)]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": new_history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_record(updated)


def build_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$day_of_week", 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate working days (Mon-Fri) from max(month_start, joined_on) to month_end
    start_date = max(date.fromisoformat(month_start), date.fromisoformat(emp["joined_on"]))
    end_date = date.fromisoformat(month_end)
    working_days = 0
    if start_date <= end_date:
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list[dict]:
    emp_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": ["$$l.day_of_week", 5]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": "$$l.late_minutes",
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": round_half_up(r["avg_work_hours"], 2) if r["avg_work_hours"] is not None else None,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> list[dict]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mon)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append(
            {
                "rank": int(r["rank"]),
                "emp_code": r["emp_code"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": int(r["total_late_minutes"]),
                "late_count": int(r["late_count"]),
            }
        )

    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str,
    emp_docs: list[dict],
) -> list[dict]:
    emp_codes = [e["emp_code"] for e in emp_docs]
    from_dt = datetime.fromisoformat(from_date_str).replace(tzinfo=timezone.utc)
    to_dt_plus_one = (datetime.fromisoformat(to_date_str) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    dept_emps_array = [{"emp_code": e["emp_code"], "joined_on": e["joined_on"]} for e in emp_docs]

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date_str, "$lte": to_date_str},
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {"$documents": [{"date": from_date_str, "_seed": True}]},
                ]
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$_seed", True]},
                            0.0,
                            {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$_seed", True]},
                            0,
                            {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                        ]
                    }
                },
            }
        },
        {"$project": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
                "dept_emps": dept_emps_array,
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")],
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date, dept_emps)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None,
                "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None,
            }
        )

    return {"department": department, "items": items}


ExplainEndpoint = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Annotated[
        ExplainEndpoint,
        Path(description="Endpoint name to explain"),
    ],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[Optional[str], Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")] = None,
    date_to: Annotated[Optional[str], Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")] = None,
    status: Optional[AllStatus] = None,
    from_date: Annotated[Optional[str], Query(alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")] = None,
    to_date: Annotated[Optional[str], Query(alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        try:
            explain_result = db.command("explain", command, verbosity="executionStats")
        except PyMongoError as err:
            raise HTTPException(status_code=503, detail=str(err))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain_result))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        pipeline = build_monthly_pipeline(emp_code, f"{month}-01", f"{month}-{num_days:02d}")
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        try:
            explain_result = db.command("explain", command, verbosity="executionStats")
        except PyMongoError as err:
            raise HTTPException(status_code=503, detail=str(err))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain_result))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        pipeline = build_department_summary_pipeline(f"{month}-01", f"{month}-{num_days:02d}", department)
        command = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        try:
            explain_result = db.command("explain", command, verbosity="executionStats")
        except PyMongoError as err:
            raise HTTPException(status_code=503, detail=str(err))
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(explain_result))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, mon = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mon)[1]
        pipeline = build_late_leaderboard_pipeline(f"{month}-01", f"{month}-{num_days:02d}", limit, department)
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        try:
            explain_result = db.command("explain", command, verbosity="executionStats")
        except PyMongoError as err:
            raise HTTPException(status_code=503, detail=str(err))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain_result))}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
        pipeline = build_department_trend_pipeline(department, from_date, to_date, dept_emps)
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        try:
            explain_result = db.command("explain", command, verbosity="executionStats")
        except PyMongoError as err:
            raise HTTPException(status_code=503, detail=str(err))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(explain_result))}
