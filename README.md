# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne attendance assignment. The API supports employee creation, atomic
punch-in and punch-out, regularization with an append-only audit trail, and the requested MongoDB-backed analytics.

## Local setup

Python 3.11+, MongoDB 6.0+, and Git are required.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
set MONGO_URI=mongodb://localhost:27017
set MONGO_DB=attendance_db
python sample_seed.py
uvicorn app.main:app --port 8000
```

The service reads `MONGO_URI` and `MONGO_DB`, defaulting to a local MongoDB instance and `attendance_db`. It creates
its indexes during startup. OpenAPI documentation is available at `/docs`.

`sample_data/` contains MongoDB Extended JSON fixtures, and `sample_seed.py` loads them. The fixtures demonstrate
document shape only; analytics are implemented for larger datasets and records with no matching logs.
