# RetrySafe

RetrySafe is an automated testing harness that verifies whether network unreliability corrupts user business intent in transactional workflows. It injects network faults between a client and a booking service, checks business invariants directly against PostgreSQL, and translates failures into human-readable counterexample stories.

---

## Architecture

```
Runner (Python)  ->  Fault Proxy (Node/Express, :8001)  ->  Booking API (FastAPI, :8000)  ->  PostgreSQL (:5432)
```

- **Booking API (`booking-api/`)**: FastAPI service backed by PostgreSQL. Supports Version A (unprotected) and Version B (`IDEMPOTENCY=on`).
- **Fault Proxy (`fault-proxy/`)**: Controllable reverse proxy on port 8001 that intercepts requests via `X-Fault` (`drop_request`, `drop_response`, `crash`, `duplicate`, `none`).
- **Runner (`runner/`)**: HTTP retry client, scenario runners, Hypothesis property testing, and rule-based story generator.
- **Results (`results/`)**: CSV summaries, JSON counterexamples, and comparative charts.

---

## Quick Start in 3 Commands

### 1. Start Database
```bash
docker compose up -d
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt && cd fault-proxy && npm install && cd ..
```

### 3. Run Experiments & Generate Reports
```bash
# Run multi-seed experiment suite (starts API & proxy automatically, generates results.csv & before_after.png)
python runner/run_experiments.py

# Run Hypothesis property-based testing & shrinking (generates counterexamples.json)
python runner/property_test.py
```

---

## Running Test Suites

```bash
# Run all Python unit, integration, and property tests
pytest -v

# Run Fault Proxy unit tests
cd fault-proxy && npm test && cd ..
```

---

## Manual Service Startup (Optional / Debugging)

To run each process manually in separate terminals:

```bash
# Terminal 1: Booking API
cd booking-api
uvicorn main:app --host 127.0.0.1 --port 8000

# Terminal 2: Fault Proxy
cd fault-proxy
node index.js

# Terminal 3: Run Fixed Scenarios or Matrix
python runner/scenarios.py
python runner/run_matrix.py
```

---

## Business Invariants Checked

- **Invariant I1 (Unique Booking)**: At most one confirmed booking per logical request.
- **Invariant I2 (Single Charge)**: Exactly one payment charge per confirmed booking; never charged twice.
- **Invariant I3 (Balance Consistency)**: User balance never negative and equals initial balance minus confirmed charges.

---

## Summary of Results

All experiment outputs are saved in `results/`:
- `results/results.csv`: Multi-seed scenario evaluation across Version A and Version B.
- `results/before_after.png`: Visual bar chart comparing violations before and after idempotency.
- `results/counterexamples.json`: Shrunk minimal counterexample sequences discovered by Hypothesis.
- `results/summary_table.csv`: 20-combination execution matrix with plain-language stories.
- `report.md`: Detailed technical project report.
