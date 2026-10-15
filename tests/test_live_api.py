"""
Live MongoDB Atlas Integration & API End-to-End Test Suite.
Uses isolated 'attendance_test_db' so the primary 'attendance_db' is never affected.
Tests all endpoints, business rules, schemas, concurrency, and explain plans.
"""
import concurrent.futures
from datetime import datetime, date, timedelta, timezone
import os
from pathlib import Path
import sys

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from bson import json_util
import dotenv
from fastapi.testclient import TestClient
import pymongo
import pytest

import app.main as main_module
from app.main import app

dotenv.load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
TEST_DB_NAME = "attendance_test_db"


@pytest.fixture(scope="session")
def mongo_client():
    client = pymongo.MongoClient(MONGO_URI, tz_aware=True)
    # verify connectivity
    client.admin.command("ping")
    return client


@pytest.fixture(scope="session")
def setup_test_db(mongo_client):
    test_db = mongo_client[TEST_DB_NAME]

    # Override db in app.main
    original_db = main_module.db
    main_module.db = test_db

    # Create indexes on test db
    test_db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique")
    test_db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined")
    test_db.employees.create_index([("joined_on", 1)], name="idx_employees_joined")

    test_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_attendance_emp_date_unique")
    test_db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_attendance_date_desc_emp_asc")
    test_db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_punch_in")
    test_db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_attendance_date_late")
    test_db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_attendance_emp_date_desc")
    test_db.attendance_logs.create_index([("date", 1), ("status", 1)], name="idx_attendance_date_status")

    yield test_db

    # Teardown: clean up test collections
    test_db.employees.delete_many({})
    test_db.attendance_logs.delete_many({})
    main_module.db = original_db


@pytest.fixture(autouse=True)
def seed_sample_data(setup_test_db):
    """Seed test_db with sample documents before each test."""
    test_db = setup_test_db
    here = Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)

    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(encoding="utf-8"), json_options=opts)
        test_db[name].delete_many({})
        test_db[name].insert_many(docs)


@pytest.fixture
def client(setup_test_db):
    return TestClient(app)


# --------------------------------------------------------------------------- #
# 1. System & Health Tests
# --------------------------------------------------------------------------- #
def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employee CRUD & Validation Tests
# --------------------------------------------------------------------------- #
def test_create_employee_success(client):
    payload = {
        "emp_code": "EMP9999",
        "name": "Ananya Sen",
        "email": "ananya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert data["name"] == "Ananya Sen"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate_conflict(client):
    payload = {
        "emp_code": "EMP0001",  # already in seed
        "name": "Duplicate Asha",
        "email": "asha2@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"]


def test_create_employee_validation_errors(client):
    # Invalid emp_code regex (requires EMP + 4-6 digits)
    res = client.post("/employees", json={
        "emp_code": "EMP1",
        "name": "Bad Code",
        "email": "bad@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    assert res.status_code == 422

    # Equal shift_start and shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP9001",
        "name": "Equal Shifts",
        "email": "equal@example.com",
        "department": "Engineering",
        "shift_start": "10:00",
        "shift_end": "10:00",
        "joined_on": "2026-01-05",
    })
    assert res.status_code == 422

    # Invalid calendar date (Feb 31)
    res = client.post("/employees", json={
        "emp_code": "EMP9002",
        "name": "Invalid Date",
        "email": "date@example.com",
        "department": "Engineering",
        "joined_on": "2026-02-31",
    })
    assert res.status_code == 422


def test_swagger_observed_valid_email_and_date(client):
    # Swagger fuzz test string matching openapi pattern '^[^@\s]+@[^@\s]+\.[^@\s]+$'
    fuzz_email = "BuC;,`&C:ZR.S+[1VDtbs7LWEJru{]gtd,@QG-Y)E*wa*HF*nJwgx}C|)e78Rh$jmY3+ex&tgM?NY[S{sQio?5mA%P\\.a&QiT?GwGt[c,$~X^EhK4T;RI[=%"
    payload = {
        "emp_code": "EMP8050",
        "name": "Fuzz Tester",
        "email": fuzz_email,
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "3812-03-05",  # Valid future calendar date
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    assert res.json()["joined_on"] == "3812-03-05"


def test_list_employees_pagination_and_sorting(client):
    res = client.get("/employees?page=1&page_size=3")
    assert res.status_code == 200
    data = res.json()
    assert data["page"] == 1
    assert data["page_size"] == 3
    assert data["total"] == 6
    assert len(data["items"]) == 3
    # Check sorted ascending by emp_code
    codes = [e["emp_code"] for e in data["items"]]
    assert codes == sorted(codes)

    # Department filtering
    res_dept = client.get("/employees?department=Engineering")
    assert res_dept.status_code == 200
    d_data = res_dept.json()
    assert d_data["total"] == 2
    assert len(d_data["items"]) == 2
    for item in d_data["items"]:
        assert item["department"] == "Engineering"


# --------------------------------------------------------------------------- #
# 3. Attendance Punch-in & Punch-out Tests
# --------------------------------------------------------------------------- #
def test_punch_in_nonexistent_employee(client):
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Employee not found"


def test_punch_in_success_and_duplicate(client):
    # Punch in on an unused date
    t_ms = int(datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_ms})
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-20"
    assert data["punch_in"] == t_ms
    assert data["punch_out"] is None
    assert data["late_minutes"] == 0
    assert "id" not in data

    # Duplicate punch-in on same date must return 409
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_ms})
    assert res_dup.status_code == 409
    assert "already punched in" in res_dup.json()["detail"]


def test_punch_in_timestamp_validation(client):
    # Reject second-looking timestamps
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res.status_code == 422

    # Reject string timestamps
    res_str = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "1783312500000"})
    assert res_str.status_code == 422

    # Reject unwhitelisted statuses
    res_stat = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert res_stat.status_code == 422


def test_punch_out_success(client):
    # EMP0006 in seed has open record on 2026-07-07: punch_in 04:05 UTC (09:35 IST)
    # Punch out at 13:35 UTC (19:05 IST) -> 9.5 hours, overtime 35 min
    out_ms = int(datetime(2026, 7, 7, 13, 35, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": out_ms})
    assert res.status_code == 200
    data = res.json()
    assert data["punch_out"] == out_ms
    assert data["work_hours"] == 9.5
    assert data["overtime_minutes"] == 35
    assert data["half_day"] is False

    # Second punch out for the same record must return 409
    res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": out_ms + 1000})
    assert res_dup.status_code == 409


def test_punch_out_before_punch_in_error(client):
    # EMP0006 punched in at 04:05 UTC; punching out earlier must be 422
    early_ms = int(datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": early_ms})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 4. Attendance Listing & Regularization Tests
# --------------------------------------------------------------------------- #
def test_list_attendance(client):
    res = client.get("/attendance?page=1&page_size=5")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 5

    # Test date range filter
    res_range = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-07")
    assert res_range.status_code == 200
    # Dates 06 and 07 have 6 records in sample seed (3 on 07-06, 3 on 07-07)
    assert res_range.json()["total"] == 6

    # Test invalid date range (from > to)
    res_inv = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_inv.status_code == 422


def test_regularize_attendance_success(client):
    # Regularize EMP0001 on 2026-07-06: correct punch_in from 03:58 to 04:00
    new_in = int(datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    payload = {
        "punch_in": new_in,
        "reason": "Correct biometric clock drift",
        "regularized_by": "hr_manager",
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "hr_manager"
    assert hist["reason"] == "Correct biometric clock drift"
    assert "punch_in" in hist["changes"]
    assert "work_hours" in hist["changes"]


def test_regularize_attendance_unchanged_error(client):
    # Submitting regularization that changes nothing must return 422
    payload = {
        "status": "PRESENT",  # already PRESENT
        "reason": "Attempting no change",
        "regularized_by": "hr_manager",
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 422


def test_regularize_attendance_absent_with_times_error(client):
    # Setting status ABSENT with punch times must return 422
    payload = {
        "status": "ABSENT",
        "punch_in": 1783312500000,
        "reason": "Invalid combination",
        "regularized_by": "hr_manager",
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 5. Analytics & Explain Endpoints
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics(client):
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["attendance_pct"] == 8.7


def test_department_summary(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Engineering"]["headcount"] == 2
    assert depts["Engineering"]["present_days"] == 3.0


def test_late_leaderboard(client):
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    top = data["items"][0]
    assert top["rank"] == 1
    assert top["emp_code"] == "EMP0003"
    assert top["total_late_minutes"] == 35


def test_department_trend(client):
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-14")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 9  # exactly 9 calendar days
    for day in data["items"]:
        assert "date" in day
        assert "is_working_day" in day
        assert "headcount" in day
        assert "attendance_rate" in day
        assert "moving_avg_7d" in day


def test_admin_explain_endpoints(client):
    endpoints = [
        "/admin/explain/attendance_list?emp_code=EMP0001",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-14",
    ]
    for url in endpoints:
        res = client.get(url)
        assert res.status_code == 200
        data = res.json()
        assert "endpoint" in data
        assert "collection" in data
        assert "explain" in data


# --------------------------------------------------------------------------- #
# 6. Concurrency Stress Simulation Tests
# --------------------------------------------------------------------------- #
def test_concurrent_punch_in_race(client):
    # Two identical punch-ins at the same instant for an unused date
    t_ms = int(datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch():
        return client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t_ms}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_punch)
        f2 = executor.submit(do_punch)
        statuses = sorted([f1.result(), f2.result()])

    # Exactly one 201 and one 409
    assert statuses == [201, 409]


def test_concurrent_punch_out_race(client):
    # Create an open record first
    t_in = int(datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t_in})
    assert res_in.status_code == 201

    t_out = int(datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_out():
        return client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": t_out}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_punch_out)
        f2 = executor.submit(do_punch_out)
        statuses = sorted([f1.result(), f2.result()])

    # Exactly one 200 and one 409
    assert statuses == [200, 409]


# --------------------------------------------------------------------------- #
# 7. Regression & High-Priority Scenario Verification (Tasks 1 - 3)
# --------------------------------------------------------------------------- #
def test_punch_out_selection_with_future_punch_in(client, setup_test_db):
    """
    Task 1: Verify punch-out selects the most recent record whose punch_in <= punched_at,
    even when a future-dated punch-in exists. Also verify 404 when no punch-in <= punched_at.
    """
    test_db = setup_test_db
    # Record 1 (target): 2026-07-20 04:00 UTC (09:30 IST)
    t_in_target = int(datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in_target})
    assert res_in.status_code == 201

    # Record 2 (future): 2026-07-25 04:00 UTC (09:30 IST)
    t_in_future = int(datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_future = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in_future})
    assert res_future.status_code == 201

    # Punch out for target date: 2026-07-20 13:00 UTC (18:30 IST)
    t_out_target = int(datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out_target})
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["date"] == "2026-07-20"
    assert out_data["punch_in"] == t_in_target
    assert out_data["punch_out"] == t_out_target
    assert out_data["work_hours"] == 9.0

    # Ensure the future record was NOT touched
    future_doc = test_db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-25"})
    assert future_doc is not None
    assert future_doc.get("punch_out") is None

    # Employee EMP0003 has punch-in records but only in the future relative to punched_at
    # Contract: punched_at before any existing punch_in must return 422 (not 404)
    t_early = int(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_none = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_early})
    assert res_none.status_code == 422
    assert "punch_in" in res_none.json()["detail"]


def test_late_leaderboard_ties_preserve_competition_rank(client, setup_test_db):
    """
    Task 2.1: Competition ranking must be preserved, and all employees whose rank <= limit
    must be returned, even when ties produce more rows than limit.
    """
    test_db = setup_test_db
    # Clear logs for 2026-05
    test_db.attendance_logs.delete_many({"date": {"$regex": "^2026-05"}})

    # Insert late attendance logs for 4 employees in Engineering
    # EMP0001: 60 late min (rank 1)
    # EMP0002: 40 late min (rank 2)
    # EMP0006: 40 late min (rank 2)
    # EMP0003 (Operations): 20 late min (rank 4)
    records = [
        {"emp_code": "EMP0001", "date": "2026-05-04", "status": "PRESENT", "late_minutes": 60, "overtime_minutes": 0, "work_hours": 8.0, "punch_in": datetime(2026, 5, 4, 5, 0, tzinfo=timezone.utc)},
        {"emp_code": "EMP0002", "date": "2026-05-04", "status": "PRESENT", "late_minutes": 40, "overtime_minutes": 0, "work_hours": 8.0, "punch_in": datetime(2026, 5, 4, 4, 40, tzinfo=timezone.utc)},
        {"emp_code": "EMP0006", "date": "2026-05-04", "status": "PRESENT", "late_minutes": 40, "overtime_minutes": 0, "work_hours": 8.0, "punch_in": datetime(2026, 5, 4, 4, 40, tzinfo=timezone.utc)},
        {"emp_code": "EMP0003", "date": "2026-05-04", "status": "PRESENT", "late_minutes": 20, "overtime_minutes": 0, "work_hours": 8.0, "punch_in": datetime(2026, 5, 4, 4, 20, tzinfo=timezone.utc)},
    ]
    test_db.attendance_logs.insert_many(records)

    # Query with limit=2: Ranks are 1, 2, 2 (EMP0003 has rank 4 > 2).
    # All rows with rank <= 2 must be returned (total 3 rows > limit).
    res = client.get("/analytics/leaderboard/late?month=2026-05&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 60

    assert items[1]["rank"] == 2
    assert items[1]["total_late_minutes"] == 40
    assert items[2]["rank"] == 2
    assert items[2]["total_late_minutes"] == 40

    returned_codes = {items[1]["emp_code"], items[2]["emp_code"]}
    assert returned_codes == {"EMP0002", "EMP0006"}


def test_mid_month_joiner_working_days(client, setup_test_db):
    """
    Task 2.2: Working days are counted starting from the employee's joining date.
    """
    test_db = setup_test_db
    # Employee joins on 2026-03-16 (Monday). March 2026 ends on March 31 (Tuesday).
    # Weekdays between 2026-03-16 and 2026-03-31 inclusive is exactly 12 working days.
    test_db.employees.insert_one({
        "emp_code": "EMP8888",
        "name": "Mid Month Joiner",
        "email": "mid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-03-16",
        "created_at": datetime.now(timezone.utc),
    })

    res = client.get("/analytics/employees/EMP8888/monthly?month=2026-03")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 12


def test_weekend_attendance_analytics(client, setup_test_db):
    """
    Task 2.3: Weekend attendance records must not increase present_days,
    but late and overtime values must still contribute to applicable totals.
    """
    test_db = setup_test_db
    # 2026-08-01 is a Saturday
    test_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-08-01",
        "status": "PRESENT",
        "late_minutes": 25,
        "overtime_minutes": 55,
        "work_hours": 8.0,
        "punch_in": datetime(2026, 8, 1, 4, 25, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 8, 1, 12, 25, tzinfo=timezone.utc),
    })

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-08")
    assert res.status_code == 200
    data = res.json()
    # Weekend record: present_days must remain 0.0
    assert data["present_days"] == 0.0
    # Late and overtime values must still contribute
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 25
    assert data["total_overtime_minutes"] == 55


def test_department_summary_headcount_without_attendance(client, setup_test_db):
    """
    Task 2.4: Department summary headcount must count employees qualifying by joining date,
    even if they have no attendance logs.
    """
    test_db = setup_test_db
    # Insert new employee in "Security" department with joined_on before month_end
    test_db.employees.insert_one({
        "emp_code": "EMP7001",
        "name": "Security Guard",
        "email": "guard@example.com",
        "department": "Security",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-04-01",
        "created_at": datetime.now(timezone.utc),
    })

    # No attendance logs for Security in 2026-04
    res = client.get("/analytics/departments/summary?month=2026-04&department=Security")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    sec = data["items"][0]
    assert sec["department"] == "Security"
    assert sec["headcount"] == 1
    assert sec["present_days"] == 0.0
    assert sec["avg_work_hours"] is None
    assert sec["late_count"] == 0
    assert sec["total_late_minutes"] == 0
    assert sec["leave_count"] == 0
    assert sec["on_duty_count"] == 0


def test_concurrent_regularization_conflict(client):
    """
    Task 2.5: Concurrent regularizations on the same record must not overwrite history;
    one succeeds (200) and the conflicting one returns 409.
    """
    # Use existing record EMP0001 on 2026-07-06
    payload1 = {
        "status": "WFH",
        "reason": "Concurrent update test 1",
        "regularized_by": "Manager1",
    }
    payload2 = {
        "status": "ON_DUTY",
        "reason": "Concurrent update test 2",
        "regularized_by": "Manager2",
    }

    def do_regularize(payload):
        return client.patch("/attendance/EMP0001/2026-07-06", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_regularize, payload1)
        f2 = executor.submit(do_regularize, payload2)
        statuses = sorted([f1.result(), f2.result()])

    # Exactly one 200 and one 409
    assert statuses == [200, 409]

    # Verify history has exactly 1 entry added
    res = client.get("/attendance?emp_code=EMP0001&date_from=2026-07-06&date_to=2026-07-06")
    rec = res.json()["items"][0]
    assert len(rec["history"]) == 1


def test_regularization_404_cases(client):
    """
    Task 2.6: Regularization 404 cases: nonexistent employee and nonexistent attendance record.
    """
    # Nonexistent employee
    res_emp = client.patch(
        "/attendance/EMP9999/2026-07-06",
        json={"status": "WFH", "reason": "Nonexistent employee test", "regularized_by": "Admin"},
    )
    assert res_emp.status_code == 404
    assert res_emp.json()["detail"] == "Employee not found"

    # Nonexistent attendance record for valid employee
    res_rec = client.patch(
        "/attendance/EMP0001/2020-01-01",
        json={"status": "WFH", "reason": "Nonexistent record test", "regularized_by": "Admin"},
    )
    assert res_rec.status_code == 404
    assert res_rec.json()["detail"] == "Record not found"


def test_regularize_present_to_absent_clears_fields(client):
    """
    Task 2.7: Regularization from PRESENT to ABSENT or LEAVE clears punch times
    and resets derived fields (work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False).
    """
    res = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={"status": "ABSENT", "reason": "Employee confirmed absent", "regularized_by": "HR01"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert len(data["history"]) == 1
    assert "status" in data["history"][0]["changes"]
    assert "punch_in" in data["history"][0]["changes"]


def test_punch_out_more_than_24h_after_punch_in(client):
    """
    Task 2.8: Punch-out more than 24 hours after punch-in returns 422.
    """
    # Punch in on 2026-07-22 04:00 UTC
    t_in = int(datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    assert res_in.status_code == 201

    # Punch out at 2026-07-23 04:00:01 UTC (24h + 1 sec = 86401 seconds later)
    t_out_excess = t_in + (86401 * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out_excess})
    assert res_out.status_code == 422
    assert "within 24 hours" in res_out.json()["detail"]


def test_department_trend_validations_and_weekdays(client):
    """
    Task 2.9: Department trend: invalid date range, range over 92 days, unknown department,
    and correct weekday flags.
    """
    # 1. Invalid date range (from > to)
    res_inv = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
    assert res_inv.status_code == 422
    assert "'to' date must be >= 'from' date" in res_inv.json()["detail"]

    # 2. Range over 92 days (100 days)
    res_long = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-15")
    assert res_long.status_code == 422
    assert "must not exceed 92 days" in res_long.json()["detail"]

    # 3. Unknown department
    res_dept = client.get("/analytics/departments/UnknownDept/trend?from=2026-07-06&to=2026-07-10")
    assert res_dept.status_code == 404
    assert res_dept.json()["detail"] == "Department not found"

    # 4. Correct weekday flags (2026-07-10 is Friday, 11 is Sat, 12 is Sun, 13 is Mon)
    res_week = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-13")
    assert res_week.status_code == 200
    days = {item["date"]: item["is_working_day"] for item in res_week.json()["items"]}
    assert days["2026-07-10"] is True   # Friday
    assert days["2026-07-11"] is False  # Saturday
    assert days["2026-07-12"] is False  # Sunday
    assert days["2026-07-13"] is True   # Monday


def test_attendance_listing_sort_order(client):
    """
    Task 2.10: Attendance listing sort order: date descending, then emp_code ascending.
    """
    res = client.get("/attendance?page=1&page_size=50")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) > 1

    for i in range(len(items) - 1):
        d1, d2 = items[i]["date"], items[i + 1]["date"]
        e1, e2 = items[i]["emp_code"], items[i + 1]["emp_code"]
        # Date must be descending
        assert d1 >= d2
        # If dates are equal, emp_code must be ascending
        if d1 == d2:
            assert e1 <= e2


def test_trend_aggregation_missing_half_day_legacy(client, setup_test_db):
    """
    Task 3: Trend aggregation safely treats missing legacy half_day as false (1.0 full day).
    """
    test_db = setup_test_db
    # Clear logs on 2026-07-18
    test_db.attendance_logs.delete_many({"date": "2026-07-18"})

    # Insert doc without half_day field for an Engineering employee
    test_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-18",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
    })

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-18&to=2026-07-18")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    # Missing half_day is treated as false -> counts as 1.0 full day present
    assert items[0]["present_count"] == 1.0


def test_punch_in_omitted_punched_at(client, setup_test_db):
    """
    Task 3: Punch-in with omitted punched_at defaults to current time and status PRESENT.
    """
    test_db = setup_test_db
    # Insert an employee with joined_on in the past
    test_db.employees.insert_one({
        "emp_code": "EMP6666",
        "name": "Omitted Punch Employee",
        "email": "omit@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP6666"})
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP6666"
    assert data["status"] == "PRESENT"
    assert data["punch_in"] is not None
    # punch_in is within the last 60 seconds
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    assert abs(data["punch_in"] - now_ms) < 60_000


def test_legacy_records_missing_fields_formatting_and_regularization(client, setup_test_db):
    """
    Task 3: Legacy records missing history, half_day, late_minutes, or overtime_minutes
    are formatted safely and can be regularized without error.
    """
    test_db = setup_test_db
    # Insert a bare legacy document
    test_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-31",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc),
    })

    # Test GET /attendance
    res = client.get("/attendance?emp_code=EMP0001&date_from=2026-07-31&date_to=2026-07-31")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    rec = items[0]
    assert rec["history"] == []
    assert rec["half_day"] is False
    assert rec["late_minutes"] == 0
    assert rec["overtime_minutes"] == 0

    # Test PATCH regularization on this legacy record
    res_reg = client.patch(
        "/attendance/EMP0001/2026-07-31",
        json={"status": "LEAVE", "reason": "Regularizing legacy entry", "regularized_by": "HR_ADMIN"},
    )
    assert res_reg.status_code == 200
    reg_data = res_reg.json()
    assert reg_data["status"] == "LEAVE"
    assert len(reg_data["history"]) == 1
    assert reg_data["history"][0]["by"] == "HR_ADMIN"


def test_health_check_database_unavailable(client, monkeypatch):
    """
    Task 3: Health check returns 503 'Database unavailable' when MongoDB ping fails.

    Note: MongoClient.admin is a property that returns a new Database object
    on every access, so patching main_module.client.admin.command patches a
    transient object the endpoint never sees. Instead we replace the entire
    client.admin attribute with a stable mock whose command() raises.
    """
    from unittest.mock import MagicMock

    fake_admin = MagicMock()
    fake_admin.command.side_effect = pymongo.errors.PyMongoError("Simulated database failure")
    monkeypatch.setattr(main_module.client, "admin", fake_admin)
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json()["detail"] == "Database unavailable"

