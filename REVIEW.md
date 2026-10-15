# REVIEW.md

Comprehensive defect analysis of the starter `app/main.py` (helpers, endpoints, models, and architecture).

### Defect Log (Granular Findings & Resolutions)

| # | Where (function / line in starter) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (lines 33-35) | Naive time replacement `punch_in.replace(...)` assumes local IST and same calendar day; fails on overnight shifts where punch-in crosses midnight. | Punching in at 00:15 for a 22:00 overnight shift compares against 22:00 of the same day (negative difference). | Evaluated against attendance date taking overnight shift rules into account; converted to IST before extracting time. |
| 2 | `compute_late_minutes` (lines 33-36) | Missing whole-second truncation on punch-in instant before calculation (R1). | 09:40:00.900 is treated with fractional seconds rather than whole-second floor (09:40:00). | Truncated epoch milliseconds to whole seconds (`ms // 1000 * 1000`) before any calculation or storage. |
| 3 | `compute_late_minutes` (line 37) | Incorrect grace period comparison: `minutes > 10` after integer division drops boundary seconds. | Punching in at 09:40:01 IST produces `diff = 601s`, integer division gave 10, but `10 > 10` evaluated false (0 late min). Expected 10 min late (R2). | Measured total elapsed seconds strictly `> 600` (10:00 grace), and calculated floor minutes since shift start (`diff_sec // 60`). |
| 4 | `compute_work_hours` (line 41) | Uses Python built-in `round(..., 2)`, which implements banker's rounding (round-half-to-even) instead of decimal half-up. | Value 4.125 or 9.125 rounds to 9.12 instead of 9.13 (R4 requires half-up rounding). | Used `decimal.Decimal` division quantized to `Decimal("0.01")` with `ROUND_HALF_UP`. |
| 5 | `compute_work_hours` (lines 40-41) | Uses IEEE 754 floating-point arithmetic for time conversion, susceptible to binary float precision artifacts. | Inexact decimal representations in floats causing subtle discrepancies on hour calculations. | Converted elapsed seconds to `Decimal(str(diff_sec)) / Decimal("3600")`. |
| 6 | `compute_overtime` (line 47) | Awards overtime for any positive minutes (`max(0, ...)`); misses the mandatory 30-minute minimum threshold (R3). | Punching out 5 minutes after shift end awards 5 minutes overtime instead of 0. | Evaluated `diff_sec >= 1800` (30 minutes). Returned `diff_sec // 60` if >= 30, else 0. |
| 7 | `compute_overtime` (lines 45-46) | Assumes `shift_end` is on `date_str`, causing overnight shift end times to be calculated on the shift start date rather than next day. | Overnight shift (22:00-06:00) punching out at 06:40 next morning calculated difference against 06:00 of previous day (~24h overtime). | For overnight shifts (`shift_end <= shift_start`), shift end instant is computed on `attendance_date + 1 day`. |
| 8 | `EmployeeIn` (line 54) | Missing regex pattern validation on `emp_code` (`^EMP\d{4,6}$`). | Non-conforming employee codes (e.g., "EMP1", "ABC") are accepted with 201 instead of 422. | Added `pattern=r"^EMP\d{4,6}$"` in Pydantic schema `EmployeeCreate`. |
| 9 | `EmployeeIn` (lines 58-59) | Allows `shift_start == shift_end`, violating contract requirement that shift start and end must differ. | Creating employee with identical start/end times (`09:00`/`09:00`) succeeds instead of returning 422. | Added model validator rejecting payloads where `shift_start == shift_end`. |
| 10 | `EmployeeIn` (lines 54-61) | Missing string length limits, email regex validation, and date format validation on employee inputs. | Oversized names, malformed emails, or invalid dates pass validation. | Added length constraints, email regex (`^[^@\s]+@[^@\s]+\.[^@\s]+$`), and date format regex. |
| 11 | `/health` (lines 72-74) | Returns hardcoded `{"status": "ok"}` unconditionally without verifying database connectivity. | Endpoint returns 200 OK even when MongoDB is down or unreachable (contract requires 503). | Added `client.admin.command("ping")` and raised HTTP 503 on connection failure. |
| 12 | `create_employee` (lines 79-83) | Check-then-insert race condition (TOCTOU): uses `find_one` followed by `insert_one` without unique index enforcement. | Parallel create requests with the same `emp_code` both pass `find_one` and create duplicate records. | Created unique index on `emp_code` and caught `DuplicateKeyError` to return HTTP 409. |
| 13 | `create_employee` (line 82) | Sets `created_at = datetime.now()`, which is a local naive datetime without timezone or second truncation. | Stored dates reflect server local clock without UTC normalization or whole-second alignment. | Stored UTC datetime truncated to whole seconds (`ms_to_utc_datetime`). |
| 14 | `create_employee` (lines 84-85) | Returns raw document without serializing `created_at` instant to epoch milliseconds. | Client receives ISO string or BSON date instead of required integer epoch milliseconds. | Serialized `created_at` to integer epoch milliseconds via `format_employee_record`. |
| 15 | `list_employees` (line 93) | Off-by-one pagination error: `skip = page * page_size` skips page 1 completely (for `page=1`, skips 20 items). | Requesting `GET /employees?page=1` returns items 21-40 rather than 1-20. | Corrected skip formula to `(page - 1) * page_size`. |
| 16 | `list_employees` (line 94) | `total` counts entire collection (`count_documents({})`), ignoring the `department` query filter. | Filtering by department returns the department's items but reports the company-wide total count. | Passed filter query `q` to `count_documents(q)` so total reflects the applied filter (R10). |
| 17 | `list_employees` (line 95) | Missing sort order: results returned in arbitrary MongoDB natural insertion order. | Response items are not sorted deterministically by `emp_code` ascending. | Added `.sort("emp_code", 1)` to MongoDB cursor query. |
| 18 | `punch_in` (lines 101-102) | Unchecked employee lookup: accessing `emp["shift_start"]` raises unhandled `TypeError` (HTTP 500) if employee does not exist. | Punching in for a non-existent employee returns 500 Internal Server Error instead of 404. | Checked `if not emp: raise HTTPException(404, "Employee not found")`. |
| 19 | `punch_in` (lines 104-118) | Check-then-insert race condition: `find_one` allows concurrent punch-ins for the same day to both succeed. | Concurrent punch-ins for the same employee on the same day both receive 201. | Created unique compound index on `(emp_code, date)` and caught `DuplicateKeyError` to return 409. |
| 20 | `punch_in` (line 66) | Accepts any arbitrary string as `status` instead of restricting to presence statuses (`PRESENT`, `WFH`, `ON_DUTY`). | Submitting `status: "INVALID"` or `"ABSENT"` via punch-in endpoint is accepted. | Used `PresenceStatus` literal enum restricting punch-in statuses to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 21 | `punch_in` (line 65) | Accepts unvalidated timestamps, including second-like timestamps (< 100000000000), floats, and strings. | Sending `1783312500` (seconds) is accepted instead of returning 422. | Added strict `EpochMillis` Pydantic type with bounds `[100000000000, 4102444800000]`. |
| 22 | `punch_in` & `list_attendance` (lines 119, 149) | Injects non-contract `"id"` field into responses (`doc["id"] = str(res.inserted_id)`). | Attendance record responses contain prohibited `id` field (contract specifies natural key `(emp_code, date)`). | Omitted MongoDB `_id` and removed `"id"` field from `format_attendance_record`. |
| 23 | `list_attendance` (lines 144-147) | In-memory sorting and pagination (`list(db.attendance_logs.find(q))` loads all records into RAM). | Severe memory exhaustion and high latency when querying a ~100k-record collection. | Pushed sorting (`[("date", -1), ("emp_code", 1)]`), `skip`, and `limit` directly to MongoDB cursor. |
| 24 | `list_attendance` (lines 125-132) | Missing date range validation: does not validate `date_from <= date_to`. | Passing `date_from=2026-07-10&date_to=2026-07-01` returns 200 with empty list instead of 422. | Added check: `if date_from and date_to and date_from > date_to: raise HTTPException(422)`. |
| 25 | `punch_out` record query | Filtering by `punch_in <= punched_at` masked attempts to punch out before punch-in, returning 404 instead of contract-required 422. | Submitting punch-out with timestamp earlier than punch-in returned 404 ("No punch-in found") instead of 422. | Retrieved most recent record with punch-in, verified `punched_at > punch_in` and within 24h before returning 422. |
| 26 | `explain_endpoint` query aliases | Wrapping `Annotated` inside `Optional` caused FastAPI to fail binding query parameter alias `from`. | GET `/admin/explain/department_trend?from=...` returned 422 ("from is required"). | Reordered typing to canonical `Annotated[Optional[str], Query(alias="from", ...)]`. |
| 27 | `explain_endpoint` BSON Timestamps | Raw explain output from MongoDB contains BSON `Timestamp` objects unencodable by FastAPI's `jsonable_encoder`. | Calling `/admin/explain/...` raised unhandled `ValueError` in `jsonable_encoder`. | Sanitized explain documents using `json.loads(json_util.dumps(explain_result))`. |
| 28 | `EmployeeCreate` calendar dates | Regex `\d{4}-\d{2}-\d{2}` accepted invalid calendar dates (e.g., Feb 31). | Submitting `2026-02-31` was accepted instead of returning 422. | Added `date.fromisoformat(self.joined_on)` in model validator, rejecting invalid dates while accepting valid ISO full-dates. |

---

### Reconciliation: 22 Granular Findings vs. 11 Functional Categories
In the initial inspection, 22–24 specific implementation bugs were noted across low-level statements. In our consolidated summary, these were grouped into 11 functional defect categories:
1. **Helper: `compute_late_minutes`** (Findings #1, #2, #3: overnight shift, truncation, boundary grace period).
2. **Helper: `compute_work_hours`** (Findings #4, #5: banker's rounding, float precision).
3. **Helper: `compute_overtime`** (Findings #6, #7: 30-minute threshold, overnight shift dates).
4. **Validation: `EmployeeIn` / Models** (Findings #8, #9, #10: regexes, shift time inequality, field bounds).
5. **Endpoint: `/health`** (Finding #11: hardcoded status without DB ping).
6. **Endpoint: `create_employee`** (Findings #12, #13, #14: TOCTOU race, naive timestamps, serialization).
7. **Endpoint: `list_employees`** (Findings #15, #16, #17: skip off-by-one, total count filter, missing sort).
8. **Endpoint: `punch_in` error handling** (Finding #18: unhandled 500 on missing employee).
9. **Endpoint: `punch_in` concurrency** (Finding #19: check-then-insert race on same day).
10. **Validation & Response Shape** (Findings #20, #21, #22: status whitelist, epoch ms validation, extraneous `id`).
11. **Endpoint: `list_attendance` scalability** (Findings #23, #24: in-memory loading, invalid date range check).

---

### Items Examined and Confirmed NOT to Be Defects
1. **String Storage for Dates and Shift Times (`YYYY-MM-DD`, `HH:MM`):** We examined storing calendar dates and shift times as plain strings rather than BSON dates. This is an explicit architectural design specified in `DATA_MODEL.md` (section 1) to prevent unwanted timezone shifts across calendar boundaries.
2. **Default Pagination (`page=1`, `page_size=20`):** Query defaults match the openapi parameter specification.
3. **No ID on Attendance Records:** We confirmed that attendance records are addressed exclusively by the natural compound key `(emp_code, date)` and must not expose MongoDB's internal `_id` or an `id` field.
