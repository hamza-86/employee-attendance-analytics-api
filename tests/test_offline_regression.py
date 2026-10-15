"""
Offline Regression Test Suite for Employee Attendance & Analytics API
Tests all business rules (R1-R10), helper functions, Pydantic schemas,
and route metadata against openapi.yaml without requiring a live MongoDB instance.
"""
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal
import os
import unittest
from pydantic import ValidationError
import yaml

from app.main import (
    round_half_up,
    truncate_ms_to_seconds,
    ms_to_utc_datetime,
    dt_to_epoch_ms,
    is_overnight_shift,
    get_attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    format_employee_record,
    format_attendance_record,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
    app,
    IST,
)


class TestHelpersAndRules(unittest.TestCase):
    def test_r1_whole_second_truncation(self):
        # 1783312500999 ms -> truncated to 1783312500000 ms
        ms = 1783312500999
        self.assertEqual(truncate_ms_to_seconds(ms), 1783312500000)
        dt = ms_to_utc_datetime(ms)
        self.assertEqual(dt.microsecond, 0)
        self.assertEqual(dt_to_epoch_ms(dt), 1783312500000)

    def test_r1_overnight_shift_and_attendance_date(self):
        # Normal shift: 09:30 to 18:30
        self.assertFalse(is_overnight_shift("09:30", "18:30"))
        dt_norm = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
        self.assertEqual(get_attendance_date(dt_norm, "09:30", "18:30"), "2026-07-06")

        # Overnight shift: 22:00 to 06:00
        self.assertTrue(is_overnight_shift("22:00", "06:00"))
        # Punch-in at 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
        dt_evening = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_evening, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 05:30 IST on 2026-07-07 (before 06:00 shift end) -> belongs to 2026-07-06
        dt_early = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_early, "22:00", "06:00"), "2026-07-06")

        # Punch-in at 06:05 IST on 2026-07-07 (after shift end) -> belongs to 2026-07-07
        dt_after = datetime(2026, 7, 7, 0, 35, tzinfo=timezone.utc)
        self.assertEqual(get_attendance_date(dt_after, "22:00", "06:00"), "2026-07-07")

    def test_r2_late_minutes_boundary(self):
        # Shift 09:30 IST
        # 09:40:00 (exactly 10 min) -> 0
        t1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t1, "09:30", "2026-07-06"), 0)

        # 09:40:01 (10 min 1 sec) -> 10
        t2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t2, "09:30", "2026-07-06"), 10)

        # 10:15:59 (45 min 59 sec) -> 45
        t3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t3, "09:30", "2026-07-06"), 45)

        # Overnight shift (22:00-06:00) on 2026-07-06: punch-in at 00:15:00 next morning
        t_overnight = datetime(2026, 7, 7, 0, 15, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t_overnight, "22:00", "2026-07-06"), 135)

    def test_r3_overtime_threshold(self):
        # Shift 09:30 to 18:30
        # Punch out 18:59:59 (29 min 59 sec) -> 0
        t1 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
        self.assertEqual(compute_overtime_minutes(t1, "09:30", "18:30", "2026-07-06"), 0)

        # Punch out 19:00:00 (30 min) -> 30
        t2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
        self.assertEqual(compute_overtime_minutes(t2, "09:30", "18:30", "2026-07-06"), 30)

        # Overnight shift: 22:00 to 06:00 next day (Sample 7: punch out 06:40 IST)
        t_ot = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
        self.assertEqual(compute_overtime_minutes(t_ot, "22:00", "06:00", "2026-07-06"), 40)

    def test_r4_work_hours_half_up(self):
        # Sample 1: 09:28 to 18:35 -> 9.12
        pi1 = datetime.fromisoformat("2026-07-06T03:58:00Z")
        po1 = datetime.fromisoformat("2026-07-06T13:05:00Z")
        self.assertEqual(compute_work_hours(pi1, po1), 9.12)

        # Sample 2: 10:05 to 19:10 -> 9.08
        pi2 = datetime.fromisoformat("2026-07-06T04:35:00Z")
        po2 = datetime.fromisoformat("2026-07-06T13:40:00Z")
        self.assertEqual(compute_work_hours(pi2, po2), 9.08)

        # Sample 7: 21:55 to 06:40 -> 8.75
        pi7 = datetime.fromisoformat("2026-07-06T16:25:00Z")
        po7 = datetime.fromisoformat("2026-07-07T01:10:00Z")
        self.assertEqual(compute_work_hours(pi7, po7), 8.75)

        # Test exact rounding boundary (ROUND_HALF_UP)
        # 14850 seconds / 3600 = 4.125 hours -> rounds to 4.13 (not 4.12 as in banker's rounding)
        pi = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
        po = datetime(2026, 7, 6, 4, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(compute_work_hours(pi, po), 4.13)

    def test_r5_half_day(self):
        self.assertTrue(compute_half_day(3.50))
        self.assertTrue(compute_half_day(4.49))
        self.assertFalse(compute_half_day(4.50))
        self.assertFalse(compute_half_day(8.00))
        self.assertFalse(compute_half_day(None))

    def test_r8_rates_rounding(self):
        self.assertEqual(round_half_up(0.12345, 4), 0.1235)
        self.assertEqual(round_half_up(0.12344, 4), 0.1234)
        self.assertEqual(round_half_up(9.115, 2), 9.12)
        self.assertEqual(round_half_up(9.114, 2), 9.11)
        self.assertIsNone(round_half_up(None, 2))


class TestPydanticValidation(unittest.TestCase):
    def test_employee_create_valid(self):
        e = EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-01-05",
        )
        self.assertEqual(e.emp_code, "EMP0001")

    def test_employee_create_invalid_emp_code(self):
        with self.assertRaises(ValidationError):
            EmployeeCreate(
                emp_code="EMP1",  # requires 4-6 digits
                name="Asha",
                email="asha@example.com",
                department="Engineering",
                joined_on="2026-01-05",
            )

    def test_employee_create_equal_shifts(self):
        with self.assertRaises(ValidationError):
            EmployeeCreate(
                emp_code="EMP0001",
                name="Asha",
                email="asha@example.com",
                department="Engineering",
                shift_start="09:00",
                shift_end="09:00",
                joined_on="2026-01-05",
            )

    def test_punch_in_request_timestamp_validation(self):
        # Seconds rejected (< 100000000000)
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", punched_at=1783312500)

        # String rejected
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", punched_at="1783312500000")

        # Valid epoch ms
        p = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000)
        self.assertEqual(p.punched_at, 1783312500000)

    def test_punch_in_status_enum(self):
        # Only PRESENT, WFH, ON_DUTY allowed
        self.assertEqual(PunchInRequest(emp_code="EMP0001", status="WFH").status, "WFH")
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", status="ABSENT")
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", status="LEAVE")

    def test_regularize_request_validation(self):
        # Reason min length 5
        with self.assertRaises(ValidationError):
            RegularizeRequest(reason="bad", regularized_by="admin")

        r = RegularizeRequest(reason="biometric glitch", regularized_by="admin")
        self.assertEqual(r.reason, "biometric glitch")


class TestOpenApiRouteCompliance(unittest.TestCase):
    def test_all_routes_match_openapi(self):
        with open("openapi.yaml", "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)

        app_routes = {}
        for route in app.routes:
            if hasattr(route, "methods"):
                for method in route.methods:
                    if method in ("GET", "POST", "PATCH", "DELETE", "PUT"):
                        app_routes[(method, route.path)] = route

        for path, path_item in spec["paths"].items():
            for method in ("get", "post", "patch", "delete", "put"):
                if method in path_item:
                    key = (method.upper(), path)
                    self.assertIn(
                        key,
                        app_routes,
                        f"Route {method.upper()} {path} defined in openapi.yaml missing in app/main.py",
                    )


if __name__ == "__main__":
    unittest.main()
