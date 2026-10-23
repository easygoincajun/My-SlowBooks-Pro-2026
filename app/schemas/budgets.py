from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel
from app.schemas.common import StrictModel


class BudgetCreate(StrictModel):
    account_id: int
    year: int
    month: int
    amount: Decimal = Decimal("0")


class BudgetUpdate(StrictModel):
    amount: Optional[Decimal] = None


class BudgetResponse(BaseModel):
    id: int
    account_id: int
    year: int
    month: int
    amount: Decimal
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
