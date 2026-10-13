# Candidate kit
# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0 or newer). All code is in `app/main.py`.

## Run
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    # create a .env file with MONGO_URI and MONGO_DB
    uvicorn app.main:app --port 8000

Indexes are created automatically at startup.

## Status
Implemented: health, employees (create, list), attendance (punch-in, punch-out,
regularize, list), employee monthly summary, department summary, late leaderboard,
department trend. REVIEW.md lists the defects found in the starter.

**Not implemented:** `GET /admin/explain/{endpoint}`.