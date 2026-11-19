from datetime import datetime
from pydantic import BaseModel, Field


class BookRequest(BaseModel):
    user_id: str = Field(..., description="Unique ID of the user")
    slot_id: str = Field(..., description="ID of the slot or seat being booked")
    amount: float = Field(..., gt=0, description="Amount to be charged")


class BookResponse(BaseModel):
    booking_id: int
    user_id: str
    slot_id: str
    amount: float
    status: str
    payment_id: int


class BookingOut(BaseModel):
    id: int
    user_id: str
    slot_id: str
    amount: float
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}



class BalanceResponse(BaseModel):
    user_id: str
    balance: float


class ResetResponse(BaseModel):
    status: str
    message: str
