# Employee Attendance & Analytics API

FastAPI and MongoDB backend implementation for employee attendance tracking, regularization, and multi-collection analytics following `openapi.yaml` (v2.0.0) and `DATA_MODEL.md`.

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas M0 cluster)

## Installation & Setup

1. **Create & activate virtual environment:**
   - **Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .\.venv\Scripts\Activate.ps1
     pip install -r requirements.txt
     ```
   - **Linux / macOS:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     pip install -r requirements.txt
     ```

2. **Configure Environment Variables:**
   Create a `.env` file in the project root (automatically loaded via `python-dotenv`) or export them directly in your shell:
   ```env
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```
   *(For MongoDB Atlas, set `MONGO_URI` to your `mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?appName=<appName>` connection string).*

   - **Windows (PowerShell alternative):**
     ```powershell
     $env:MONGO_URI = "mongodb://localhost:27017"
     $env:MONGO_DB = "attendance_db"
     ```

3. **Seed Sample Data (Optional):**
   ```powershell
   python sample_seed.py
   ```

4. **Run the Application:**
   ```powershell
   uvicorn app.main:app --port 8000
   ```

Interactive Swagger documentation will be available at `http://127.0.0.1:8000/docs`.

## Running Tests

Install test dependencies (if not already installed):
```powershell
pip install pytest httpx httpx2
```

Run the complete test suite (unit, regression, integration, and explain tests):
```powershell
python -m pytest -v
```
*(Or target offline regression suite without live database: `python -m pytest tests/test_offline_regression.py -v`)*

Verify explain execution plans directly on live database:
```powershell
python tests/verify_explain_plans.py
```

## Key Implementation Highlights

- **All Application Code:** Self-contained in `app/main.py`.
- **Database Indexes:** Created idempotently on application startup via FastAPI lifespan.
- **Race Condition Prevention:** Unique database constraints on `employees.emp_code` and `attendance_logs(emp_code, date)` with atomic operations (`insert_one`, `find_one_and_update`).
- **Precision:** Decimal `ROUND_HALF_UP` calculations for work hours and analytics rates per R4 and R8.
- **Overnight Shifts:** Robust IST conversion handling shifts crossing midnight per R1-R3.
- **Aggregation Pipelines:** MongoDB 6.0+ aggregation pipelines with `$rank`, `$lookup`, `$densify`, and `$setWindowFields` for analytics and explain plans.
- **Review & Design:** See `REVIEW.md` and `DECISIONS.md`.
