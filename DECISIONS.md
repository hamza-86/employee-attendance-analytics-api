# DECISIONS.md

1. **Indexes.**
On `employees`, unique `emp_code` ensures O(1) lookups and race safety; compound `(department, joined_on)` speeds headcount and department filters. On `attendance_logs`, unique `(emp_code, date)` strictly enforces one log per day; `(date, emp_code)` supports paginated listings; `(emp_code, punch_in)` accelerates punch-out lookups; and `(date, late_minutes)` supports leaderboard scans. We rejected indexing `history.at` because audit entries are append-only and never queried.

2. **Punch-in race.**
Both requests determine the same attendance date and attempt an atomic `insert_one`. The unique compound index on `(emp_code, date)` allows only the first insert to succeed (HTTP 201). The duplicate insert immediately triggers `DuplicateKeyError`, which our exception handler catches and translates into HTTP 409 Conflict.

3. **Ties.**
We use MongoDB's `$rank` window operator for standard competition ranking (1, 2, 2, 4). The `limit` parameter filters `rank <= limit`. If two employees tie at the cutoff rank, both satisfy the predicate and are returned. Tied rows are deterministically ordered by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation pipeline originates on the `employees` collection matching `joined_on <= month_end`, grouping by department with `$sum: 1`. This counts every active employee regardless of attendance logs. Logs are joined via `$lookup`; employees with zero logs simply join an empty array.

5. **One thing you would change for 100x data.**
At 100x scale (~10 million logs), we would pre-aggregate daily and monthly statistics asynchronously upon punch-out and regularization into a dedicated `daily_summaries` collection, avoiding expensive multi-collection group-and-lookup operations across raw logs during read requests.
