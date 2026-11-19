import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

try:
    from .database import Base, get_db
    from .main import app
    from .models import User, Booking, Payment
except ImportError:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from database import Base, get_db
    from main import app
    from models import User, Booking, Payment


# In-memory SQLite database for self-contained, high-speed test execution
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
    # Reset state and seed default test user
    db.query(Payment).delete()
    db.query(Booking).delete()
    db.query(User).delete()
    db.add(User(id="user_1", balance=1000.0))
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=test_engine)
    app.dependency_overrides.clear()



def test_reset_endpoint():
    # Make a booking first
    client.post("/book", json={"user_id": "user_1", "slot_id": "slot_100", "amount": 100.0})

    # Reset
    response = client.post("/reset")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "reset"

    # Confirm tables are cleared and user_1 restored to 1000.0
    bookings_res = client.get("/bookings")
    assert bookings_res.status_code == 200
    assert len(bookings_res.json()) == 0

    balance_res = client.get("/balance/user_1")
    assert balance_res.status_code == 200
    assert balance_res.json()["balance"] == 1000.0


def test_book_happy_path():
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_101",
        "amount": 150.0
    }
    response = client.post("/book", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["booking_id"] == 1
    assert data["payment_id"] == 1
    assert data["user_id"] == "user_1"
    assert data["slot_id"] == "slot_101"
    assert data["amount"] == 150.0
    assert data["status"] == "CONFIRMED"


def test_get_bookings():
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_A",
        "amount": 75.0
    }
    client.post("/book", json=payload)

    response = client.get("/bookings")
    assert response.status_code == 200
    bookings = response.json()
    assert len(bookings) == 1
    assert bookings[0]["user_id"] == "user_1"
    assert bookings[0]["slot_id"] == "slot_A"
    assert bookings[0]["amount"] == 75.0
    assert bookings[0]["status"] == "CONFIRMED"


def test_get_balance_after_booking():
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_B",
        "amount": 250.0
    }
    client.post("/book", json=payload)

    response = client.get("/balance/user_1")
    assert response.status_code == 200
    assert response.json()["balance"] == 750.0


def test_version_a_duplicate_booking_vulnerability():
    """
    In Version A, retrying the exact same request creates multiple bookings
    and charges the user multiple times (no idempotency protection).
    """
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_DUP",
        "amount": 100.0
    }
    res1 = client.post("/book", json=payload)
    res2 = client.post("/book", json=payload)

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["booking_id"] != res2.json()["booking_id"]

    bookings_res = client.get("/bookings")
    assert len(bookings_res.json()) == 2

    # Initial balance 1000 - (2 * 100) = 800
    balance_res = client.get("/balance/user_1")
    assert balance_res.json()["balance"] == 800.0


def test_crash_point_after_charge():
    """
    When X-Crash-Point: after_charge is passed, an exception is raised after
    writing the charge but before committing the booking. The single transaction
    must roll back, leaving no booking, no charge, and user balance unchanged.
    """
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_CRASH",
        "amount": 200.0
    }
    headers = {"X-Crash-Point": "after_charge"}
    response = client.post("/book", json=payload, headers=headers)
    assert response.status_code == 500
    assert "Simulated crash" in response.json()["detail"]

    # Invariants: No booking committed
    bookings_res = client.get("/bookings")
    assert len(bookings_res.json()) == 0

    # Invariants: Balance intact
    balance_res = client.get("/balance/user_1")
    assert balance_res.json()["balance"] == 1000.0


def test_insufficient_balance():
    payload = {
        "user_id": "user_1",
        "slot_id": "slot_EXPENSIVE",
        "amount": 1500.0
    }
    response = client.post("/book", json=payload)
    assert response.status_code == 400
    assert "Insufficient balance" in response.json()["detail"]


def test_unknown_user_balance_404():
    response = client.get("/balance/unknown_user_999")
    assert response.status_code == 404
