import os
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

try:
    from .database import engine, Base, get_db
    from .models import User, Booking, Payment, IdempotencyRecord
    from .schemas import BookRequest, BookResponse, BookingOut, BalanceResponse, ResetResponse
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from database import engine, Base, get_db
    from models import User, Booking, Payment, IdempotencyRecord
    from schemas import BookRequest, BookResponse, BookingOut, BalanceResponse, ResetResponse


logger = logging.getLogger("booking-api")
logging.basicConfig(level=logging.INFO)

# Process-level lock registry for concurrent idempotency requests
_key_locks: dict = {}
_key_locks_guard = threading.Lock()


def get_key_lock(key: str) -> threading.Lock:
    """Retrieve or create a re-entrant lock for a specific idempotency key."""
    with _key_locks_guard:
        if key not in _key_locks:
            _key_locks[key] = threading.Lock()
        return _key_locks[key]


def is_idempotency_enabled() -> bool:
    """Check if Version B idempotency protection is enabled via env flag."""
    val = os.getenv("IDEMPOTENCY", "off").lower().strip()
    return val in ("on", "true", "1", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.warning(f"Database table initialization failed on startup: {exc}")
    yield


app = FastAPI(
    title="RetrySafe Booking API",
    description="Booking API supporting Version A (unprotected) and Version B (Idempotent)",
    version="1.0.0",
    lifespan=lifespan
)


@app.post("/config/idempotency")
def set_idempotency(enabled: bool):
    """Dynamically toggle idempotency flag (for test runner matrix execution)."""
    os.environ["IDEMPOTENCY"] = "on" if enabled else "off"
    return {"idempotency": os.environ["IDEMPOTENCY"]}



@app.post("/reset", response_model=ResetResponse)
def reset_database(db: Session = Depends(get_db)):
    """
    Truncate all tables (test mode only) and seed default test user.
    Clears idempotency records, payments, bookings, and users.
    """
    try:
        dialect = db.bind.dialect.name
        db.execute(text("DELETE FROM idempotency_records;"))
        db.execute(text("DELETE FROM payments;"))
        db.execute(text("DELETE FROM bookings;"))
        db.execute(text("DELETE FROM users;"))

        if dialect == "postgresql":
            try:
                db.execute(text("ALTER SEQUENCE bookings_id_seq RESTART WITH 1;"))
                db.execute(text("ALTER SEQUENCE payments_id_seq RESTART WITH 1;"))
            except Exception:
                pass

        # Clear in-memory locks
        with _key_locks_guard:
            _key_locks.clear()

        # Seed default test user
        default_user = User(id="user_1", balance=1000.0)
        db.add(default_user)
        db.commit()

        return ResetResponse(
            status="reset",
            message="All tables truncated and default user_1 seeded with 1000.0 balance"
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reset failed: {str(exc)}"
        )


def _process_booking(
    req: BookRequest,
    x_crash_point: Optional[str],
    idempotency_key: Optional[str],
    use_idempotency: bool,
    db: Session,
) -> BookResponse:
    try:
        # Find user or auto-seed with default balance 1000.0
        user = db.query(User).filter(User.id == req.user_id).first()
        if not user:
            user = User(id=req.user_id, balance=1000.0)
            db.add(user)
            db.flush()

        if user.balance < req.amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient balance. Current: {user.balance}, required: {req.amount}"
            )

        # 1. Deduct balance from user
        user.balance -= req.amount

        # 2. Record payment charge
        payment = Payment(
            user_id=user.id,
            amount=req.amount,
            status="SUCCESS"
        )
        db.add(payment)
        db.flush()  # Charge written within this active transaction

        # Crash point simulation
        if x_crash_point == "after_charge":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Simulated crash: server failed after charge before booking commit"
            )

        # 3. Create booking
        booking = Booking(
            user_id=user.id,
            slot_id=req.slot_id,
            amount=req.amount,
            status="CONFIRMED"
        )
        db.add(booking)
        db.flush()

        # Link payment to booking
        payment.booking_id = booking.id

        # 4. Commit logic
        if use_idempotency:
            response_payload = {
                "booking_id": booking.id,
                "user_id": booking.user_id,
                "slot_id": booking.slot_id,
                "amount": booking.amount,
                "status": booking.status,
                "payment_id": payment.id,
            }
            rec = IdempotencyRecord(
                key=idempotency_key,
                user_id=user.id,
                booking_id=booking.id,
                payment_id=payment.id,
                response_status=200,
                response_body=json.dumps(response_payload),
            )
            db.add(rec)
            try:
                db.commit()
                return BookResponse(**response_payload)
            except IntegrityError:
                db.rollback()
                committed = db.query(IdempotencyRecord).filter(IdempotencyRecord.key == idempotency_key).first()
                if committed:
                    return BookResponse(**json.loads(committed.response_body))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Concurrent idempotency race conflict"
                )
        else:
            db.commit()
            return BookResponse(
                booking_id=booking.id,
                user_id=booking.user_id,
                slot_id=booking.slot_id,
                amount=booking.amount,
                status=booking.status,
                payment_id=payment.id
            )

    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Transaction failed: {str(exc)}"
        )


@app.post("/book", response_model=BookResponse, status_code=status.HTTP_200_OK)
def book_slot(
    req: BookRequest,
    x_crash_point: Optional[str] = Header(None, alias="X-Crash-Point"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    """
    Book a slot and record a payment charge.
    - Version A (IDEMPOTENCY != 'on'): Every call creates a new booking and charge.
    - Version B (IDEMPOTENCY == 'on' and Idempotency-Key header provided):
      Uses unique constraint and transaction to ensure exactly one booking and charge.
      Duplicate requests return the original result.
    - Crash point: If X-Crash-Point is 'after_charge', raises exception after charge
      is written but before booking commit, rolling back the transaction.
    """
    use_idempotency = is_idempotency_enabled() and bool(idempotency_key)

    if use_idempotency:
        lock = get_key_lock(idempotency_key)
        with lock:
            # Fast path under lock: return cached response if key already committed
            existing = db.query(IdempotencyRecord).filter(IdempotencyRecord.key == idempotency_key).first()
            if existing:
                cached_data = json.loads(existing.response_body)
                return BookResponse(**cached_data)

            return _process_booking(req, x_crash_point, idempotency_key, True, db)
    else:
        return _process_booking(req, x_crash_point, idempotency_key, False, db)


@app.get("/bookings", response_model=List[BookingOut])
def get_bookings(db: Session = Depends(get_db)):
    """
    Returns all bookings for inspection.
    """
    bookings = db.query(Booking).order_by(Booking.id.asc()).all()
    return bookings


@app.get("/balance/{user_id}", response_model=BalanceResponse)
def get_balance(user_id: str, db: Session = Depends(get_db)):
    """
    Returns the balance for the specified user.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found"
        )
    return BalanceResponse(user_id=user.id, balance=user.balance)
