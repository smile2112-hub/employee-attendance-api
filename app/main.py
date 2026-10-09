import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH = 100_000_000_000
MAX_EPOCH = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
EMP_RE = re.compile(r"^EMP\d{4,6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def _indexes() -> None:
    db.employees.create_index("emp_code", unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index("joined_on", name="employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_key_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING), ("punch_out", ASCENDING)],
        name="open_punch_lookup",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    _indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("shift_end")
    @classmethod
    def distinct_shift_end(cls, value: str, info):
        if "shift_start" in info.data and value == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        return _check_epoch(value)

    @field_validator("status")
    @classmethod
    def valid_presence(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be a presence status")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        return _check_epoch(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, value: Optional[int]) -> Optional[int]:
        return _check_epoch(value)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value


def _check_epoch(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or value < MIN_EPOCH or value > MAX_EPOCH:
        raise ValueError("must be epoch milliseconds")
    return value


def _month(value: str) -> tuple[date, date]:
    if not MONTH_RE.fullmatch(value):
        raise ValueError("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _day(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _epoch_dt(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local(value: datetime) -> datetime:
    return _as_utc(value).astimezone(IST)


def _ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(_as_utc(value).timestamp() * 1000)


def _clock(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def _overnight(emp: dict[str, Any]) -> bool:
    return _clock(emp["shift_end"]) <= _clock(emp["shift_start"])


def _attendance_date(ts: datetime, emp: dict[str, Any]) -> date:
    local = _local(ts)
    result = local.date()
    if _overnight(emp) and local.time().replace(tzinfo=None) < _clock(emp["shift_end"]):
        result -= timedelta(days=1)
    return result


def _shift_point(day: date, value: str, overnight_end: bool = False) -> datetime:
    result = datetime.combine(day, _clock(value), IST)
    if overnight_end:
        result += timedelta(days=1)
    return result


def _late_minutes(punch_in: datetime, emp: dict[str, Any], attendance_day: date) -> int:
    start = _shift_point(attendance_day, emp["shift_start"])
    elapsed_seconds = int((_local(punch_in) - start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 10 * 60 else 0


def _derived(punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict[str, Any], day: date) -> dict:
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = _late_minutes(punch_in, emp, day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((_as_utc(punch_out) - _as_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    end = _shift_point(day, emp["shift_end"], _overnight(emp))
    extra = int((_local(punch_out) - end).total_seconds() // 60)
    return {
        "work_hours": float(hours),
        "late_minutes": late,
        "overtime_minutes": extra if extra >= 30 else 0,
        "half_day": hours < Decimal("4.50"),
    }


def _record(doc: dict) -> dict:
    out = {key: value for key, value in doc.items() if key != "_id"}
    out["punch_in"] = _ms(out.get("punch_in"))
    out["punch_out"] = _ms(out.get("punch_out"))
    out["created_at"] = _ms(out.get("created_at")) if "created_at" in out else None
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("history", [])
    for entry in out["history"]:
        entry["at"] = _ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            if isinstance(change.get("from"), datetime):
                change["from"] = _ms(change["from"])
            if isinstance(change.get("to"), datetime):
                change["to"] = _ms(change["to"])
    out.pop("created_at", None)
    return out


def _employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def _page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid pagination")


def _query_dates(date_from: Optional[str], date_to: Optional[str]) -> dict:
    if date_from is None and date_to is None:
        return {}
    query: dict[str, Any] = {}
    if date_from:
        _day(date_from)
        query["$gte"] = date_from
    if date_to:
        _day(date_to)
        query["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")
    return {"date": query}


def _present_expr() -> dict:
    return {"$in": ["$status", list(PRESENCE)]}


def _working_days(start: date, end: date) -> int:
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return {key: value for key, value in doc.items() if key != "_id" and key != "created_at"} | {
        "created_at": _ms(doc["created_at"])
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = []
    for doc in docs:
        doc["created_at"] = _ms(doc.get("created_at"))
        items.append(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = _employee(body.emp_code)
    ts = _epoch_dt(body.punched_at)
    day = _attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **_derived(ts, None, emp, day),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = _employee(body.emp_code)
    ts = _epoch_dt(body.punched_at)
    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if current is None:
        closed = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
            sort=[("punch_in", DESCENDING)],
        )
        if closed is not None and closed.get("punch_out") is not None:
            raise HTTPException(409, "attendance record is already punched out")
        raise HTTPException(404, "no open punch-in found")
    punch_in_dt = _as_utc(current["punch_in"])
    if ts <= punch_in_dt or ts - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    day = _day(current["date"])
    vals = _derived(punch_in_dt, ts, emp, day)
    result = db.attendance_logs.update_one(
        {"_id": current["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record is already punched out")
    current.update({"punch_out": ts, **vals})
    return _record(current)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = _query_dates(date_from, date_to)
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(
        (page - 1) * page_size
    ).limit(page_size)
    return {"items": [_record(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = _employee(emp_code)
    day = _day(date)
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set
    status = body.status if "status" in fields else current["status"]
    pin = _epoch_dt(body.punch_in) if "punch_in" in fields else current.get("punch_in")
    pout = _epoch_dt(body.punch_out) if "punch_out" in fields else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in fields or "punch_out" in fields:
            raise HTTPException(422, "non-presence status cannot have punch times")
        pin = None
        pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and _attendance_date(pin, emp) != day:
        raise HTTPException(422, "punch_in does not belong to the attendance date")
    if pout is not None:
        if pin is None or _as_utc(pout) <= _as_utc(pin) or _as_utc(pout) - _as_utc(pin) > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    vals = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE:
        vals = _derived(pin, pout, emp, day)
    proposed = {"status": status, "punch_in": pin, "punch_out": pout, **vals}
    changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old = current.get(key, False if key == "half_day" else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        if old != proposed[key]:
            changes[key] = {"from": old, "to": proposed[key]}
    if not changes:
        raise HTTPException(422, "correction changes nothing")
    history = list(current.get("history", []))
    history.append({"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes})
    update = {**proposed, "history": history}
    result = db.attendance_logs.update_one(
        {"_id": current["_id"], "history": current.get("history", [])},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently")
    current.update(update)
    return _record(current)


def _employee_month_pipeline(emp_code: str, start: date, end: date) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}}, 6]},
            ]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    emp = _employee(emp_code)
    try:
        start, end = _month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = next(iter(db.attendance_logs.aggregate(_employee_month_pipeline(emp_code, start, end))), {})
    joined = max(start, _day(emp["joined_on"]))
    working = _working_days(joined, end) if joined <= end else 0
    present = float(row.get("present_days", 0))
    pct = None if working == 0 else float(
        (Decimal(str(present)) * Decimal("100") / Decimal(working)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    try:
        start, end = _month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    match = {"$match": {"joined_on": {"$lte": end.isoformat()}}}
    if department:
        match["$match"]["department"] = department
    pipeline = [
        match,
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start.isoformat()]},
                {"$lte": ["$date", end.isoformat()]},
            ]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "logs": {"$push": "$logs"}}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$_id", "headcount": {"$first": "$headcount"},
            "present_days": {"$sum": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]},
                {"$cond": [{"$ne": ["$logs.date", None]}, {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "timezone": "+05:30"}}}, 6]}, False]},
            ]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
            ]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    items = []
    for row in db.employees.aggregate(pipeline):
        avg = row.get("avg_work_hours")
        items.append({
            "department": row["_id"], "headcount": row["headcount"], "present_days": row["present_days"],
            "avg_work_hours": None if avg is None else float(Decimal(str(avg)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "late_count": row["late_count"], "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"], "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    try:
        start, end = _month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    lookup_match: dict[str, Any] = {"$expr": {"$eq": ["$emp_code", "$$code"]}}
    if department:
        lookup_match["department"] = department
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "let": {"code": "$emp_code"}, "pipeline": [{"$match": lookup_match}], "as": "emp"}},
        {"$unwind": "$emp"},
        {"$group": {"_id": "$emp_code", "name": {"$first": "$emp.name"}, "department": {"$first": "$emp.department"}, "total": {"$sum": "$late_minutes"}, "count": {"$sum": 1}}},
        {"$setWindowFields": {"partitionBy": None, "sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total": -1, "_id": 1}},
    ]
    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        items.append({"rank": row["rank"], "emp_code": row["_id"], "name": row["name"], "department": row["department"], "total_late_minutes": row["total"], "late_count": row["count"]})
    return {"month": month, "items": items}


def _trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    start_dt = datetime.combine(start, time(), IST).astimezone(UTC)
    end_dt = datetime.combine(end, time(), IST).astimezone(UTC)
    return [
        {"$documents": [{"date": start_dt}, {"date": end_dt}]},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
            {"$lookup": {"from": "employees", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$department", department]},
            ]}}}], "as": "emp"}},
            {"$match": {"$expr": {"$gt": [{"$size": "$emp"}, 0]}}},
            {"$project": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "+05:30"}}, "present": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0,
            ]}, "late": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}},
        ]}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": "$present"}, "late_count": {"$sum": "$late"}}},
        {"$set": {"date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "+05:30"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [{"$match": {"$expr": {"$and": [
            {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]},
        ]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "working": {"$lt": [{"$isoDayOfWeek": "$_id"}, 6]}}},
        {"$set": {"rate_raw": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$rate_raw", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$rate_raw", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": "$working", "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$ne": ["$moving_raw", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_raw", 10000]}, 0.5]}}, 10000]}, None]}}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    try:
        start, end = _day(from_), _day(to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if end < start:
        raise HTTPException(422, "to must not precede from")
    if (end - start).days > 91:
        raise HTTPException(422, "range must not exceed 92 days")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.attendance_logs.aggregate(_trend_pipeline(department, start, end)))}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        query = _query_dates(date_from, date_to)
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        op = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": op.explain("executionStats")}
    if month is None:
        raise HTTPException(422, "month is required")
    try:
        start, end = _month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if endpoint == "employee_monthly":
        if emp_code is None:
            raise HTTPException(422, "emp_code is required")
        pipeline = _employee_month_pipeline(emp_code, start, end)
        coll = db.attendance_logs
    elif endpoint == "department_summary":
        pipeline = [{"$match": {"joined_on": {"$lte": end.isoformat()}}}]
        coll = db.employees
    elif endpoint == "late_leaderboard":
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}}]
        coll = db.attendance_logs
    else:
        if department is None or from_ is None or to is None:
            raise HTTPException(422, "department, from and to are required")
        pipeline = _trend_pipeline(department, _day(from_), _day(to))
        command = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        raw = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}
    command = {"aggregate": coll.name, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": coll.name, "explain": db.command("explain", command, verbosity="executionStats")}
