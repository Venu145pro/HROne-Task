"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Idempotently create required indexes on startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", pymongo.ASCENDING)], unique=True)
        db.employees.create_index([
            ("department", pymongo.ASCENDING),
            ("joined_on", pymongo.ASCENDING),
            ("emp_code", pymongo.ASCENDING)
        ])
        
        # attendance_logs indexes
        db.attendance_logs.create_index([
            ("emp_code", pymongo.ASCENDING),
            ("date", pymongo.ASCENDING)
        ], unique=True)
        db.attendance_logs.create_index([
            ("date", pymongo.DESCENDING),
            ("emp_code", pymongo.ASCENDING)
        ])
        db.attendance_logs.create_index([
            ("emp_code", pymongo.ASCENDING),
            ("punch_in", pymongo.ASCENDING),
            ("punch_out", pymongo.ASCENDING)
        ])
        db.attendance_logs.create_index([
            ("date", pymongo.ASCENDING),
            ("emp_code", pymongo.ASCENDING),
            ("status", pymongo.ASCENDING)
        ])
    except Exception as e:
        print(f"Error creating indexes: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def half_up_round(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """Round a float half-up to specified decimals (R4, R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime (UTC or aware) to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_epoch_ms(ms: int) -> datetime:
    """Validate and convert integer epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise HTTPException(422, "Timestamp must be an integer count of epoch milliseconds")
    if ms < 100000000000 or ms > 4102444800000:
        raise HTTPException(422, "Epoch milliseconds out of allowed range")
    # Truncate to whole seconds (R1)
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000.0, tz=timezone.utc)


def parse_time_str(t_str: str) -> tuple[int, int]:
    h, m = map(int, t_str.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    """
    Determine the IST calendar attendance date for a punch-in instant.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = parse_time_str(shift_start_str)
    eh, em = parse_time_str(shift_end_str)
    
    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        if punch_in_ist.time() < time(eh, em):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_shift_instants(attendance_date_str: str, shift_start_str: str, shift_end_str: str) -> tuple[datetime, datetime]:
    """Return timezone-aware IST datetime objects for shift start and shift end."""
    d_obj = date.fromisoformat(attendance_date_str)
    sh, sm = parse_time_str(shift_start_str)
    eh, em = parse_time_str(shift_end_str)
    
    start_ist = datetime(d_obj.year, d_obj.month, d_obj.day, sh, sm, 0, tzinfo=IST)
    if (eh, em) <= (sh, sm): # Overnight shift ends on the next calendar day
        end_date = d_obj + timedelta(days=1)
        end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    else:
        end_ist = datetime(d_obj.year, d_obj.month, d_obj.day, eh, em, 0, tzinfo=IST)
        
    return start_ist, end_ist


def compute_late_minutes(punch_in_utc: datetime, shift_start_ist: datetime) -> int:
    """
    Rule R2: Grace period 10 minutes. Late only if punch_in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600: # strictly more than 10 minutes (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """Rule R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = diff_sec / 3600.0
    return half_up_round(raw_hours, 2)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end_ist: datetime) -> int:
    """Rule R3: Whole minutes from shift_end to punch_out, counted only if >= 30; otherwise 0."""
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800: # at least 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """Rule R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee_doc(doc: dict) -> dict:
    """Format MongoDB employee document for HTTP response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at"))
    }


def format_history_entry(entry: dict) -> dict:
    """Format history entry for HTTP response."""
    formatted_changes = {}
    changes = entry.get("changes", {})
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            formatted_changes[k] = {
                "from": to_epoch_ms(v.get("from")) if v.get("from") is not None else None,
                "to": to_epoch_ms(v.get("to")) if v.get("to") is not None else None
            }
        else:
            formatted_changes[k] = {
                "from": v.get("from"),
                "to": v.get("to")
            }
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": formatted_changes
    }


def format_attendance_doc(doc: dict) -> dict:
    """Format MongoDB attendance log document for HTTP response."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])]
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Punch-in status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status value")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))

    return {
        "items": [format_employee_doc(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_in_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    shift_start_ist, _ = compute_shift_instants(attendance_date, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, shift_start_ist)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is <= punched_at
    open_rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_utc},
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )

    if not open_rec:
        # Check why it wasn't found
        # 1. Check if punch_out <= punch_in
        future_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punch_out_utc}},
            sort=[("punch_in", 1)]
        )
        if future_rec:
            raise HTTPException(422, "punched_at must be strictly after punch_in")

        # 2. Check if already punched out
        already_out_rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}, "punch_out": {"$ne": None}},
            sort=[("punch_in", -1)]
        )
        if already_out_rec:
            raise HTTPException(409, "record is already punched out")

        raise HTTPException(404, "no punch-in record found")

    punch_in_utc = open_rec["punch_in"]

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    _, shift_end_ist = compute_shift_instants(open_rec["date"], emp["shift_start"], emp["shift_end"])
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_mins = compute_overtime_minutes(punch_out_utc, shift_end_ist)
    half_day_flag = compute_half_day(work_hrs)

    # Atomic update
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": open_rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_mins,
                "half_day": half_day_flag
            }
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "record is already punched out")

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [format_attendance_doc(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeIn = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(404, "Attendance record not found")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch times for ABSENT or LEAVE status")
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = parse_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_punch_out = parse_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

        if new_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in instant")

        # Verify punch_in stays on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(422, f"punch_in instant belongs to attendance date {calc_date}, not {date_str}")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    shift_start_ist, shift_end_ist = compute_shift_instants(date_str, emp["shift_start"], emp["shift_end"])

    if target_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(new_punch_in, shift_start_ist)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(new_punch_out, shift_end_ist)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Compute actual changes
    changes = {}
    field_pairs = [
        ("status", rec.get("status"), target_status),
        ("punch_in", rec.get("punch_in"), new_punch_in),
        ("punch_out", rec.get("punch_out"), new_punch_out),
        ("work_hours", rec.get("work_hours"), new_work_hours),
        ("late_minutes", rec.get("late_minutes", 0), new_late),
        ("overtime_minutes", rec.get("overtime_minutes", 0), new_overtime),
        ("half_day", rec.get("half_day", False), new_half_day)
    ]

    for fname, old_v, new_v in field_pairs:
        if old_v != new_v:
            changes[fname] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(422, "Request resulted in no field changes")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Atomic update preserving history concurrent edits
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": rec.get("history", [])},
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day
            },
            "$push": {"history": history_entry}
        },
        return_document=pymongo.ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(409, "Concurrent modification of attendance record")

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    month_start_date = date(year, month_num, 1)
    month_end_date = date(year, month_num, num_days)
    joined_date = date.fromisoformat(emp["joined_on"])

    if joined_date > month_end_date:
        working_days = 0
    else:
        start_calc = max(month_start_date, joined_date)
        curr = start_calc
        working_days = 0
        while curr <= month_end_date:
            if curr.weekday() < 5: # Monday - Friday
                working_days += 1
            curr += timedelta(days=1)

    # MongoDB Aggregation Pipeline for log stats
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$project": {
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]}, # Sun=1, Mon=2, ..., Fri=6, Sat=7
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        res = agg_res[0]
        present_days = half_up_round(res["present_days"], 2)
        leave_days = res["leave_days"]
        late_count = res["late_count"]
        total_late = res["total_late_minutes"]
        total_ot = res["total_overtime_minutes"]
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = half_up_round((present_days / working_days) * 100.0, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct
    }


@app.get("/analytics/departments/summary")
def department_summary_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None)
):
    year, month_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    month_end = f"{month}-{num_days:02d}"

    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$eq": [{"$substr": ["$date", 0, 7]}, month]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "log_status": "$logs.status",
                "log_half_day": "$logs.half_day",
                "log_work_hours": "$logs.work_hours",
                "log_late_minutes": "$logs.late_minutes",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}
                            }
                        },
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$log_half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log_work_hours", None]}
                                ]
                            },
                            "$log_work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log_work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$log_late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$log_late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$log_status", "ON_DUTY"]}, 1, 0]}
                }
            }
        },
        {"$sort": {"_id": 1}}
    ]

    agg_res = list(db.employees.aggregate(pipeline))

    items = []
    for r in agg_res:
        headcount = len(r["employees"])
        if headcount == 0:
            continue
        wh_count = r["work_hours_count"]
        avg_wh = half_up_round(r["total_work_hours"] / wh_count, 2) if wh_count > 0 else None
        
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": half_up_round(r["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard_analytics(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"$expr": {"$lte": ["$rank", limit]}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ])

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in agg_res:
        items.append({
            "rank": r["rank"],
            "emp_code": r["_id"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"]
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend_analytics(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
):
    d_start = date.fromisoformat(from_date)
    d_end = date.fromisoformat(to_date)

    if d_end < d_start:
        raise HTTPException(422, "to date must be >= from date")

    if (d_end - d_start).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    # Generate full date range
    date_list = []
    curr = d_start
    while curr <= d_end:
        date_list.append(curr.isoformat())
        curr += timedelta(days=1)

    # Use pipeline to compute daily counts
    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "log_date": "$logs.date",
                "log_status": "$logs.status",
                "log_half_day": "$logs.half_day",
                "log_late_minutes": "$logs.late_minutes"
            }
        }
    ]

    docs = list(db.employees.aggregate(pipeline))

    # Pre-index data by date
    daily_stats: Dict[str, Dict[str, Any]] = {d: {"present_count": 0.0, "late_count": 0} for d in date_list}
    emp_joined: Dict[str, str] = {}

    for doc in docs:
        emp_joined[doc["emp_code"]] = doc["joined_on"]
        l_date = doc.get("log_date")
        if l_date in daily_stats:
            st = doc.get("log_status")
            if st in ("PRESENT", "WFH", "ON_DUTY"):
                hd = doc.get("log_half_day", False)
                daily_stats[l_date]["present_count"] += (0.5 if hd else 1.0)
            if doc.get("log_late_minutes", 0) > 0:
                daily_stats[l_date]["late_count"] += 1

    daily_items = []
    for d_str in date_list:
        d_obj = date.fromisoformat(d_str)
        is_wk = d_obj.weekday() < 5
        headcount = sum(1 for j_on in emp_joined.values() if j_on <= d_str)

        pres_cnt = half_up_round(daily_stats[d_str]["present_count"], 2)
        late_cnt = daily_stats[d_str]["late_count"]

        if is_wk and headcount > 0:
            att_rate = half_up_round(pres_cnt / headcount, 4)
        else:
            att_rate = None

        daily_items.append({
            "date": d_str,
            "is_working_day": is_wk,
            "headcount": headcount,
            "present_count": pres_cnt,
            "late_count": late_cnt,
            "attendance_rate": att_rate
        })

    # Compute 7-day moving average over preceding rows in range
    for i, item in enumerate(daily_items):
        window = daily_items[max(0, i - 6): i + 1]
        valid_rates = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if valid_rates:
            avg_val = sum(valid_rates) / len(valid_rates)
            item["moving_avg_7d"] = half_up_round(avg_val, 4)
        else:
            item["moving_avg_7d"] = None

    return {"department": department, "items": daily_items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
        explain_output = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly explain")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {
                "$group": {
                    "_id": None,
                    "total_late": {"$sum": "$late_minutes"}
                }
            }
        ]
        explain_output = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary explain")
        pipeline = [
            {"$match": {"joined_on": {"$lte": f"{month}-31"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs"
                }
            }
        ]
        explain_output = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_output}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard explain")
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}}
        ]
        explain_output = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required for department_trend explain")
        pipeline = [
            {"$match": {"department": department}}
        ]
        explain_output = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_output}

    else:
        raise HTTPException(422, f"Unknown endpoint: {endpoint}")
