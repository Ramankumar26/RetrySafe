import os
import sys
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure booking-api is on sys.path
sys.path.insert(0, os.path.dirname(__file__))

from database import Base, get_db
from main import app
from models import User, Booking, Payment, IdempotencyRecord

# Test engine for isolated test runs
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

test_engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_database():
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    db.query(IdempotencyRecord).delete()
    db.query(Payment).delete()
    db.query(Booking).delete()
    db.query(User).delete()
    db.add(User(id="user_1", balance=1000.0))
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=test_engine)
    app.dependency_overrides.clear()


def test_version_a_when_flag_off(monkeypatch):
    """
    When IDEMPOTENCY is 'off' (or not 'on'), sending duplicate requests with
    the same Idempotency-Key header creates multiple bookings and charges
    the user multiple times (preserving Version A unprotected behavior).
    """
    monkeypatch.setenv("IDEMPOTENCY", "off")

    payload = {"user_id": "user_1", "slot_id": "slot_V_A", "amount": 100.0}
    headers = {"Idempotency-Key": "key_version_a"}

    res1 = client.post("/book", json=payload, headers=headers)
    res2 = client.post("/book", json=payload, headers=headers)

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["booking_id"] != res2.json()["booking_id"]

    # Two distinct bookings created
    bookings = client.get("/bookings").json()
    assert len(bookings) == 2

    # Balance charged twice: 1000 - 200 = 800
    bal = client.get("/balance/user_1").json()
    assert bal["balance"] == 800.0


def test_version_b_sequential_duplicate(monkeypatch):
    """
    When IDEMPOTENCY is 'on', sending the exact same Idempotency-Key returns
    the original response and does NOT create a second booking or charge.
    """
    monkeypatch.setenv("IDEMPOTENCY", "on")

    payload = {"user_id": "user_1", "slot_id": "slot_SEQ", "amount": 150.0}
    headers = {"Idempotency-Key": "key_seq_1"}

    res1 = client.post("/book", json=payload, headers=headers)
    assert res1.status_code == 200
    data1 = res1.json()

    # Retry with identical key
    res2 = client.post("/book", json=payload, headers=headers)
    assert res2.status_code == 200
    data2 = res2.json()

    # Original result returned
    assert data1["booking_id"] == data2["booking_id"]
    assert data1["payment_id"] == data2["payment_id"]

    # Invariants: Exactly 1 booking created
    bookings = client.get("/bookings").json()
    assert len(bookings) == 1
    assert bookings[0]["id"] == data1["booking_id"]

    # Invariants: Charged only once: 1000 - 150 = 850
    bal = client.get("/balance/user_1").json()
    assert bal["balance"] == 850.0


def test_version_b_concurrent_duplicates(monkeypatch):
    """
    When IDEMPOTENCY is 'on', sending concurrent requests with the exact same
    Idempotency-Key produces exactly 1 booking and charge; all concurrent callers
    receive the same booking ID.
    """
    monkeypatch.setenv("IDEMPOTENCY", "on")

    payload = {"user_id": "user_1", "slot_id": "slot_CONC", "amount": 200.0}
    headers = {"Idempotency-Key": "key_concurrent_99"}

    def call_book():
        return client.post("/book", json=payload, headers=headers)

    # Dispatch 4 concurrent requests with same key
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: call_book(), range(4)))

    for r in responses:
        assert r.status_code == 200

    booking_ids = [r.json()["booking_id"] for r in responses]
    # All 4 responses must have the exact same booking_id
    assert len(set(booking_ids)) == 1

    # Invariants: Exactly 1 booking in database
    bookings = client.get("/bookings").json()
    assert len(bookings) == 1

    # Invariants: Charged only once: 1000 - 200 = 800
    bal = client.get("/balance/user_1").json()
    assert bal["balance"] == 800.0


def test_version_b_crash_rollback_and_retry(monkeypatch):
    """
    When a request crashes mid-transaction (X-Crash-Point: after_charge),
    the transaction rolls back atomically. A subsequent retry with the SAME
    Idempotency-Key must succeed, creating 1 booking and 1 charge.
    """
    monkeypatch.setenv("IDEMPOTENCY", "on")

    payload = {"user_id": "user_1", "slot_id": "slot_CRASH_V_B", "amount": 100.0}
    crash_headers = {
        "Idempotency-Key": "key_crash_retry",
        "X-Crash-Point": "after_charge"
    }

    # Attempt 1: Crashes
    res_crash = client.post("/book", json=payload, headers=crash_headers)
    assert res_crash.status_code == 500

    # Verify atomic rollback
    bookings = client.get("/bookings").json()
    assert len(bookings) == 0
    assert client.get("/balance/user_1").json()["balance"] == 1000.0

    # Attempt 2: Retry with SAME Idempotency-Key (no crash header)
    retry_headers = {"Idempotency-Key": "key_crash_retry"}
    res_retry = client.post("/book", json=payload, headers=retry_headers)
    assert res_retry.status_code == 200
    data = res_retry.json()
    assert data["booking_id"] == 1

    # Verify final state: exactly 1 booking, exactly 1 charge
    bookings_after = client.get("/bookings").json()
    assert len(bookings_after) == 1
    assert client.get("/balance/user_1").json()["balance"] == 900.0


def test_version_b_different_keys_create_different_bookings(monkeypatch):
    """
    When IDEMPOTENCY is 'on', different keys represent distinct requests
    and each creates its own booking and charge.
    """
    monkeypatch.setenv("IDEMPOTENCY", "on")

    resA = client.post("/book", json={"user_id": "user_1", "slot_id": "sA", "amount": 100.0}, headers={"Idempotency-Key": "key_A"})
    resB = client.post("/book", json={"user_id": "user_1", "slot_id": "sB", "amount": 100.0}, headers={"Idempotency-Key": "key_B"})

    assert resA.status_code == 200
    assert resB.status_code == 200
    assert resA.json()["booking_id"] != resB.json()["booking_id"]

    bookings = client.get("/bookings").json()
    assert len(bookings) == 2
    assert client.get("/balance/user_1").json()["balance"] == 800.0
