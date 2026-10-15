import os
import sys
from pathlib import Path

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

import dotenv
import pymongo
from app.main import (
    build_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

dotenv.load_dotenv()
db_name = os.getenv("MONGO_DB_NAME", "attendance_db")
client = pymongo.MongoClient(os.getenv("MONGO_URI"), tz_aware=True)
tdb = client[db_name]


def extract_winning_stages(explain_doc):
    stages = []

    def walk(node):
        if isinstance(node, dict):
            if "stage" in node:
                stages.append(node["stage"])
            for k, v in node.items():
                if k != "rejectedPlans":
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if "queryPlanner" in explain_doc:
        walk(explain_doc["queryPlanner"].get("winningPlan", {}))
    if "stages" in explain_doc:
        for s in explain_doc["stages"]:
            for k, v in s.items():
                if isinstance(v, dict) and "queryPlanner" in v:
                    walk(v["queryPlanner"].get("winningPlan", {}))
    return stages


def verify_all():
    print("Verifying explain plans on live MongoDB Atlas...")

    # 1. attendance_list
    exp1 = tdb.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": {"emp_code": "EMP0001"},
            "sort": {"date": -1, "emp_code": 1},
            "skip": 0,
            "limit": 20,
        },
        verbosity="executionStats",
    )
    st1 = extract_winning_stages(exp1)
    print("1. attendance_list:", st1, "IXSCAN:", "IXSCAN" in st1, "COLLSCAN:", "COLLSCAN" in st1)

    # 2. employee_monthly
    exp2 = tdb.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": build_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31"),
            "cursor": {},
        },
        verbosity="executionStats",
    )
    st2 = extract_winning_stages(exp2)
    print("2. employee_monthly:", st2, "IXSCAN:", "IXSCAN" in st2, "COLLSCAN:", "COLLSCAN" in st2)

    # 3. department_summary
    exp3 = tdb.command(
        "explain",
        {
            "aggregate": "employees",
            "pipeline": build_department_summary_pipeline("2026-07-01", "2026-07-31", "Engineering"),
            "cursor": {},
        },
        verbosity="executionStats",
    )
    st3 = extract_winning_stages(exp3)
    print("3. department_summary:", st3, "IXSCAN:", "IXSCAN" in st3, "COLLSCAN:", "COLLSCAN" in st3)

    # 4. late_leaderboard
    exp4 = tdb.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": build_late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10, "Sales"),
            "cursor": {},
        },
        verbosity="executionStats",
    )
    st4 = extract_winning_stages(exp4)
    print("4. late_leaderboard:", st4, "IXSCAN:", "IXSCAN" in st4, "COLLSCAN:", "COLLSCAN" in st4)

    # 5. department_trend
    dept_emps = list(tdb.employees.find({"department": "Engineering"}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        dept_emps = [{"emp_code": "EMP0001", "joined_on": "2026-01-01"}]
    exp5 = tdb.command(
        "explain",
        {
            "aggregate": "attendance_logs",
            "pipeline": build_department_trend_pipeline("Engineering", "2026-07-06", "2026-07-14", dept_emps),
            "cursor": {},
        },
        verbosity="executionStats",
    )
    st5 = extract_winning_stages(exp5)
    print("5. department_trend:", st5, "IXSCAN:", "IXSCAN" in st5, "COLLSCAN:", "COLLSCAN" in st5)


if __name__ == "__main__":
    verify_all()
