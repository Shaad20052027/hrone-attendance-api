"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
#1st change made in project
import json
from bson import json_util
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.decimal128 import Decimal128

load_dotenv()

#2nd change made in project 
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.employees.create_index("joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#3rd change made in project
def to_utc(dt: datetime) -> datetime:
    """Make any datetime timezone-aware UTC and chop off microseconds (R1)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def parse_hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start          # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST date this punch-in belongs to."""
    ist = to_utc(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        h, m = parse_hm(shift_end)
        if ist.time() < time(h, m):          # early-morning punch = previous day's shift
            d = d - timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = parse_hm(shift_start)
    return datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_hm(shift_end)
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2"""
    diff = int((to_utc(punch_in) - shift_start_dt(date_str, shift_start)).total_seconds())
    return diff // 60 if diff > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3"""
    end = shift_end_dt(date_str, shift_start, shift_end)
    mins = int((to_utc(punch_out) - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: half-up rounding using Decimal, not float round()."""
    seconds = int((to_utc(punch_out) - to_utc(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5"""
    return work_hours < 4.50

def resolve_instant(ms: Optional[int]) -> datetime:
    """Epoch ms (or now if None) -> UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return to_utc(datetime.now(timezone.utc))
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)

def to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds (exact integer math)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(out["created_at"])
    return out


def ms_or_none(v):
    return to_ms(v) if isinstance(v, datetime) else None


def history_out(h: dict) -> dict:
    changes = {}
    for key, val in h.get("changes", {}).items():
        if key in ("punch_in", "punch_out"):
            val = {"from": ms_or_none(val.get("from")), "to": ms_or_none(val.get("to"))}
        changes[key] = val
    return {"at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")),
        "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_out(h) for h in doc.get("history", [])],
    }


def check_date_param(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a real date in YYYY-MM-DD format")
    return value 

def parse_month(month: str) -> tuple:
    """'2026-07' -> (2026, 7). Rejects year 0000."""
    y, m = int(month[:4]), int(month[5:7])
    if y < 1:
        raise HTTPException(422, "month must be YYYY-MM")
    return y, m


def working_days_in_month(month: str, joined_on: str) -> int:
    """R7: Mon-Fri days in the month, counted from joined_on if that is later than the 1st."""
    y, m = parse_month(month)
    start = max(date(y, m, 1), date.fromisoformat(joined_on))
    end = date(y, m, calendar.monthrange(y, m)[1])
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:           # Monday=0 ... Friday=4
            n += 1
        d += timedelta(days=1)
    return n


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding (Mongo's $round is banker's rounding, so we round in Python)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))  

def as_decimal(v) -> Decimal:
    return v.to_decimal() if isinstance(v, Decimal128) else Decimal(str(v))    

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_real_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a real date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")      # derived fields (work_hours etc.) cannot be sent -> 422
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)    
# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        ts = to_utc(datetime.now(timezone.utc))
    else:
        ts = datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc)   # whole seconds (R1)

    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = resolve_instant(body.punched_at)

    # most recent record whose punch_in is at or before ts (ABSENT/LEAVE have null punch_in, so they never match)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_ts = to_utc(rec["punch_in"])
    if ts <= punch_in_ts or ts - punch_in_ts > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    work_hours = compute_work_hours(punch_in_ts, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},      # only succeeds if still open
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(ts, rec["date"], emp["shift_start"], emp["shift_end"]),
            "half_day": compute_half_day(work_hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:                               # someone else closed it first
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: str, body: RegularizeIn):
    check_date_param(att_date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]

    # ---- build the final state ----
    if new_status in PRESENCE:
        new_in = resolve_instant(body.punch_in) if body.punch_in is not None else (to_utc(old_in) if old_in else None)
        new_out = resolve_instant(body.punch_out) if body.punch_out is not None else (to_utc(old_out) if old_out else None)
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        late = compute_late_minutes(new_in, att_date, emp["shift_start"])
        if new_out is not None:
            work_hours = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, att_date, emp["shift_start"], emp["shift_end"])
            half_day = compute_half_day(work_hours)
        else:
            work_hours, overtime, half_day = None, 0, False
    else:                                              # ABSENT / LEAVE clear the punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
        late, work_hours, overtime, half_day = 0, None, 0, False

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}
    old = {"status": rec["status"],
           "punch_in": to_utc(old_in) if old_in else None,
           "punch_out": to_utc(old_out) if old_out else None,
           "work_hours": rec.get("work_hours"),
           "late_minutes": rec.get("late_minutes", 0),
           "overtime_minutes": rec.get("overtime_minutes", 0),
           "half_day": rec.get("half_day", False)}

    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": to_utc(datetime.now(timezone.utc)), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}

    # ---- write only if the record is still exactly as we read it ----
    flt = {"_id": rec["_id"], "status": rec["status"], "punch_in": old_in, "punch_out": old_out}
    hist = rec.get("history")
    flt["history"] = {"$size": len(hist)} if isinstance(hist, list) else {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return attendance_out(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = check_date_param(date_from, "date_from")
    date_to = check_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    pipeline = [
        {"$match": {"emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
        # $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Monday-Friday = 2..6
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]},
                          {"$in": ["$dow", [2, 3, 4, 5, 6]]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    agg = rows[0] if rows else {}

    working_days = working_days_in_month(month, emp["joined_on"])
    present_days = agg.get("present_days", 0)
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 2)
        if working_days > 0 else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    y, m = parse_month(month)
    first, last = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"

    is_present = {"$in": ["$logs.status", list(PRESENCE)]}
    has_hours = {"$and": [is_present, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}

    emp_match = {"joined_on": {"$lte": last}}                # R9: joined on or before month end
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$lookup": {                                         # start from employees, so zero-log people stay
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, {"$in": ["$logs.dow", [2, 3, 4, 5, 6]]}]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0]}},
            "hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$logs.work_hours"}, 0]}},
            "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$emps"},
                      "present_days": 1, "hours_sum": 1, "hours_n": 1, "late_count": 1,
                      "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]

    items = []
    for r in db.employees.aggregate(pipeline):
        n = r["hours_n"]
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(as_decimal(r["hours_sum"]) / n, 2) if n else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    y, m = parse_month(month)
    first, last = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"

    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                       # no preserve: logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # filter BEFORE ranking
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},      # 1, 2, 2, 4 style ranking
        {"$match": {"rank": {"$lte": limit}}},                          # limit AFTER ranking
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    check_date_param(date_from, "from")
    check_date_param(date_to, "to")
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if d1 < d0:
        raise HTTPException(422, "to must not be before from")
    if (d1 - d0).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")

    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not codes:
        raise HTTPException(404, "unknown department")

    start = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc)
    end_excl = datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc) + timedelta(days=1)
    half = Decimal128("0.5")

    # half-up rounding to 4 decimals, done exactly in Decimal128: floor(x * 10000 + 0.5) / 10000
    rate_expr = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        {"$divide": [
            {"$floor": {"$add": [
                {"$multiply": [{"$divide": [{"$toDecimal": "$present_count"},
                                            {"$toDecimal": "$headcount"}]}, 10000]},
                half]}},
            10000]},
        None,
    ]}

    pipeline = [
        # one seed row (the department exists, so this always yields 1 row) ...
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": start}}},
        # ... and $densify fills in every other calendar day of the range
        {"$densify": {"field": "day",
                      "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                        "dow": {"$dayOfWeek": "$day"}}},
        # this day's attendance stats for the department's employees
        {"$lookup": {
            "from": "attendance_logs", "localField": "date", "foreignField": "date",
            "pipeline": [
                {"$match": {"emp_code": {"$in": codes}}},
                {"$group": {
                    "_id": None,
                    "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                                                   {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                    "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "agg"}},
        # headcount on this day (R9)
        {"$lookup": {
            "from": "employees", "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc"}},
        {"$addFields": {
            "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]},
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$agg.present", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$agg.late", 0]}, 0]},
        }},
        {"$addFields": {"attendance_rate": rate_expr}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]

    items = []
    for r in db.employees.aggregate(pipeline):
        rate, mv = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": float(as_decimal(rate)) if rate is not None else None,
            "moving_avg_7d": round_half_up(as_decimal(mv), 4) if mv is not None else None,
        })
    return {"department": department, "items": items}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
