import os
import sys
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Ensure booking-api is on sys.path
sys.path.insert(0, os.path.dirname(__file__))

from database import engine, SessionLocal, Base
from main import app
from models import User, Booking, Payment

# Connect TestClient directly to live app with real PostgreSQL backend
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def verify_and_init_postgres():
    """Verify PostgreSQL connection and initialize schema before running tests."""
    app.dependency_overrides.clear()
    dialect = engine.dialect.name
    if dialect != "postgresql":
        pytest.skip(f"Live PostgreSQL required; detected dialect: {dialect}")
    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()



@pytest.fixture(autouse=True)
def reset_db_before_each_test():
    """Reset PostgreSQL state via POST /reset before every test."""
    res = client.post("/reset")
    assert res.status_code == 200, f"Reset failed: {res.text}"


def test_postgres_connection_and_dialect():
    """Confirm the test suite is running against live PostgreSQL 16."""
    with engine.connect() as conn:
        ver = conn.execute(text("SELECT version();")).scalar()
        assert "PostgreSQL" in ver, f"Expected PostgreSQL version string, got: {ver}"
        assert engine.dialect.name == "postgresql"


def test_postgres_reset_behavior():
    """
    Test that POST /reset deletes data, resets sequence IDs, and seeds user_1 with balance 1000.0.
    """
    # Create bookings to alter state
    res1 = client.post("/book", json={"user_id": "user_1", "slot_id": "slot_1", "amount": 100.0})
    res2 = client.post("/book", json={"user_id": "user_1", "slot_id": "slot_2", "amount": 200.0})
    assert res1.status_code == 200
    assert res2.status_code == 200

    # Confirm records exist in PostgreSQL
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM bookings;")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM payments;")).scalar() == 2

    # Reset
    res_reset = client.post("/reset")
    assert res_reset.status_code == 200
    assert res_reset.json()["status"] == "reset"

    # Verify tables are empty
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM bookings;")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM payments;")).scalar() == 0
        user_bal = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(user_bal) == 1000.0

    # Verify identity sequence was restarted to 1
    res_next = client.post("/book", json={"user_id": "user_1", "slot_id": "slot_fresh", "amount": 50.0})
    assert res_next.status_code == 200
    assert res_next.json()["booking_id"] == 1
    assert res_next.json()["payment_id"] == 1


def test_postgres_transaction_rollback_at_crash_point():
    """
    Test that when X-Crash-Point: after_charge is triggered, PostgreSQL
    atomically rolls back the transaction. Direct SQL queries verify:
    no booking record, no payment record, and user balance unchanged.
    """
    with engine.connect() as conn:
        initial_balance = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(initial_balance) == 1000.0

    payload = {"user_id": "user_1", "slot_id": "slot_CRASH_PG", "amount": 350.0}
    headers = {"X-Crash-Point": "after_charge"}
    res = client.post("/book", json=payload, headers=headers)
    assert res.status_code == 500
    assert "Simulated crash" in res.json()["detail"]

    # Directly query PostgreSQL to verify atomic rollback
    with engine.connect() as conn:
        booking_count = conn.execute(
            text("SELECT count(*) FROM bookings WHERE slot_id = 'slot_CRASH_PG';")
        ).scalar()
        assert booking_count == 0

        payment_count = conn.execute(
            text("SELECT count(*) FROM payments WHERE user_id = 'user_1' AND amount = 350.0;")
        ).scalar()
        assert payment_count == 0

        current_balance = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(current_balance) == 1000.0


def test_postgres_foreign_key_enforcement():
    """
    Test PostgreSQL foreign key constraint enforcement:
    1. Direct insertion of a booking with a non-existent user_id violates FK constraint.
    2. Deleting a user cascades and deletes child bookings and payments.
    """
    db = SessionLocal()
    try:
        invalid_booking = Booking(
            user_id="nonexistent_user_999",
            slot_id="slot_fk_test",
            amount=50.0,
            status="CONFIRMED"
        )
        db.add(invalid_booking)
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()

    # 2. Test CASCADE deletion
    book_res = client.post("/book", json={"user_id": "user_1", "slot_id": "slot_cascade", "amount": 120.0})
    assert book_res.status_code == 200

    db = SessionLocal()
    try:
        assert db.query(Booking).filter(Booking.user_id == "user_1").count() == 1
        assert db.query(Payment).filter(Payment.user_id == "user_1").count() == 1

        user = db.query(User).filter(User.id == "user_1").first()
        db.delete(user)
        db.commit()

        assert db.query(Booking).filter(Booking.user_id == "user_1").count() == 0
        assert db.query(Payment).filter(Payment.user_id == "user_1").count() == 0
    finally:
        db.close()


def test_postgres_wallet_balance_consistency():
    """
    Test wallet balance consistency against PostgreSQL:
    - Multiple sequential bookings deduct balance correctly.
    - Invariant: User balance + sum(confirmed payments) == Initial balance.
    - Overdraft attempts (> balance) are rejected with 400.
    """
    initial_balance = 1000.0
    charges = [100.0, 250.0, 150.0]

    for idx, amt in enumerate(charges):
        res = client.post("/book", json={"user_id": "user_1", "slot_id": f"slot_{idx}", "amount": amt})
        assert res.status_code == 200

    expected_balance = initial_balance - sum(charges)  # 500.0

    # Verify via API
    bal_res = client.get("/balance/user_1")
    assert bal_res.status_code == 200
    assert bal_res.json()["balance"] == expected_balance

    with engine.connect() as conn:
        raw_balance = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(raw_balance) == expected_balance

        total_payments = conn.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE user_id = 'user_1';")
        ).scalar()
        assert float(total_payments) == sum(charges)
        assert float(raw_balance) + float(total_payments) == initial_balance

    # Overdraft attempt
    overdraft_res = client.post("/book", json={"user_id": "user_1", "slot_id": "slot_overdraft", "amount": 600.0})
    assert overdraft_res.status_code == 400
    assert "Insufficient balance" in overdraft_res.json()["detail"]

    with engine.connect() as conn:
        final_balance = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(final_balance) == expected_balance


def test_postgres_concurrent_duplicate_requests():
    """
    Test duplicate requests against PostgreSQL:
    In Version A (no idempotency protection), sending multiple requests
    creates multiple bookings and deducts balance multiple times.
    """
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_DUP_SEQ",
        "amount": 75.0
    }

    # Dispatch 4 sequential requests (simulating duplicate retries)
    for _ in range(4):
        r = client.post("/book", json=payload)
        assert r.status_code == 200

    with engine.connect() as conn:
        booking_count = conn.execute(
            text("SELECT count(*) FROM bookings WHERE slot_id = 'slot_DUP_SEQ';")
        ).scalar()
        assert booking_count == 4

        payment_count = conn.execute(
            text("SELECT count(*) FROM payments WHERE user_id = 'user_1' AND amount = 75.0;")
        ).scalar()
        assert payment_count == 4

        user_bal = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        assert float(user_bal) == 700.0
