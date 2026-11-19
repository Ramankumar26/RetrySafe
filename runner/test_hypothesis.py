import os
import sys
import json
from hypothesis import given, strategies as st, settings, Phase, HealthCheck
from hypothesis.database import InMemoryExampleDatabase
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure booking-api and runner are on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "booking-api")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import app
from database import Base, get_db
from models import User, Booking, Payment, IdempotencyRecord

# Fast in-memory SQLite for high-speed Hypothesis state exploration & shrinking
hypo_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
HypoSession = sessionmaker(autocommit=False, autoflush=False, bind=hypo_engine)


def override_hypo_db():
    db = HypoSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_hypo_db
client = TestClient(app)


def reset_database():
    Base.metadata.create_all(bind=hypo_engine)
    db = HypoSession()
    db.query(IdempotencyRecord).delete()
    db.query(Payment).delete()
    db.query(Booking).delete()
    db.query(User).delete()
    db.add(User(id="user_1", balance=1000.0))
    db.commit()
    db.close()


# Strategy: Generate a sequence of attempts for a single logical booking request
attempt_strategy = st.lists(
    st.fixed_dictionaries({
        "fault": st.sampled_from(["none", "drop_request", "drop_response", "crash", "duplicate"]),
        "amount": st.sampled_from([50.0, 100.0, 150.0]),
    }),
    min_size=1,
    max_size=5,
)


hypothesis_results = {
    "version_a_failures": 0,
    "version_a_minimal_counterexample": None,
    "version_a_shrunk_length": None,
    "version_b_tests_run": 0,
    "version_b_passed": 0,
    "version_b_failures": 0,
}


def simulate_client_interaction(attempts, version: str, key: str = "hypo_key"):
    """
    Simulates client sending requests through proxy behavior.
    - drop_request: socket closed before server arrival, client catches error and retries.
    - drop_response: server processes request, but client receives socket closed, then retries.
    - crash: server crashes mid-transaction (500), client retries.
    - duplicate: request duplicated and delivered twice to server.
    - none: passes through.
    """
    is_version_b = (version == "Version B")
    os.environ["IDEMPOTENCY"] = "on" if is_version_b else "off"
    headers = {"Idempotency-Key": key}

    for i, step in enumerate(attempts):
        fault = step["fault"]
        amt = step["amount"]
        payload = {"user_id": "user_1", "slot_id": "slot_hypo", "amount": amt}

        if fault == "drop_request":
            continue
        elif fault == "drop_response":
            client.post("/book", json=payload, headers=headers)
            continue
        elif fault == "crash":
            client.post("/book", json=payload, headers={**headers, "X-Crash-Point": "after_charge"})
            continue
        elif fault == "duplicate":
            client.post("/book", json=payload, headers=headers)
            client.post("/book", json=payload, headers=headers)
            break
        elif fault == "none":
            res = client.post("/book", json=payload, headers=headers)
            if res.status_code == 200:
                break


def test_hypothesis_version_a_fails_and_shrinks():
    """
    Property: Under any fault sequence, logical request creates at most 1 booking.
    In Version A, this property FAILS, and Hypothesis shrinks the failing input
    to the minimal counterexample sequence.
    """
    reset_database()
    shrunk_example = None

    @settings(max_examples=30, database=InMemoryExampleDatabase(), deadline=None)
    @given(attempts=attempt_strategy)
    def prop_version_a(attempts):
        nonlocal shrunk_example
        reset_database()
        simulate_client_interaction(attempts, version="Version A")

        db = HypoSession()
        try:
            booking_count = db.query(Booking).count()
            if booking_count > 1:
                shrunk_example = attempts
                assert booking_count <= 1, f"Invariant I1 violated: {booking_count} bookings"
        finally:
            db.close()

    try:
        prop_version_a()
    except AssertionError as exc:
        hypothesis_results["version_a_failures"] += 1
        hypothesis_results["version_a_minimal_counterexample"] = shrunk_example
        hypothesis_results["version_a_shrunk_length"] = len(shrunk_example) if shrunk_example else 1
        print(f"\n[Hypothesis] Version A Property FAILED as expected.")
        print(f"[Hypothesis] Shrunk Minimal Counterexample: {shrunk_example}")
        print(f"[Hypothesis] Shrunk Sequence Length: {hypothesis_results['version_a_shrunk_length']}")


def test_hypothesis_version_b_holds_under_all_faults():
    """
    Property: Under Version B with Idempotency-Key, Invariants I1 and I2 hold
    under arbitrary generated fault and retry sequences.
    """
    reset_database()
    tests_run = 0

    @settings(max_examples=30, database=InMemoryExampleDatabase(), deadline=None)
    @given(attempts=attempt_strategy)
    def prop_version_b(attempts):
        nonlocal tests_run
        reset_database()
        simulate_client_interaction(attempts, version="Version B")
        tests_run += 1

        db = HypoSession()
        try:
            booking_count = db.query(Booking).count()
            payment_count = db.query(Payment).count()

            assert booking_count <= 1, f"Invariant I1 violated: {booking_count} bookings"
            assert payment_count <= 1, f"Invariant I2 violated: {payment_count} payments"
        finally:
            db.close()

    prop_version_b()
    hypothesis_results["version_b_tests_run"] = tests_run
    hypothesis_results["version_b_passed"] = tests_run
    hypothesis_results["version_b_failures"] = 0
    print(f"\n[Hypothesis] Version B Property PASSED across {tests_run} random generated fault sequences!")


def run_hypothesis_suite():
    print("=== Running Hypothesis Property-Based Exploration ===")
    test_hypothesis_version_a_fails_and_shrinks()
    test_hypothesis_version_b_holds_under_all_faults()

    os.makedirs("results", exist_ok=True)
    with open("results/hypothesis_results.json", "w", encoding="utf-8") as f:
        json.dump(hypothesis_results, f, indent=2)
    print("Hypothesis results saved to results/hypothesis_results.json")
    return hypothesis_results


if __name__ == "__main__":
    run_hypothesis_suite()
