from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator


class PortfolioCreate(BaseModel):
    portfolio_name: str = Field("My Portfolio", min_length=1, max_length=120)


class HoldingCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    quantity: Decimal = Field(..., gt=0)
    avg_buy_price: Decimal = Field(..., gt=0)
    buy_date: date

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class HoldingUpdate(BaseModel):
    """Edits buy_date / confirmation on any holding. Also allows correcting
    quantity/avg_buy_price, but ONLY when the holding still has exactly one
    BUY transaction and no SELL transactions -- i.e. it hasn't been touched
    by Buy More or Sell Position yet. Once a holding has real transaction
    history, quantity/avg_buy_price are derived from the ledger and must be
    corrected via that ledger (a follow-up buy/sell), not overwritten directly."""
    quantity: Decimal | None = Field(None, gt=0)
    avg_buy_price: Decimal | None = Field(None, gt=0)
    buy_date: date | None = None
    requires_confirmation: bool | None = None
    notes: str | None = Field(None, max_length=2000)


class HoldingBuyMore(BaseModel):
    """An additional purchase of a symbol already (or previously) held.
    Recomputes the weighted-average buy price across all open quantity."""
    quantity: Decimal = Field(..., gt=0)
    price: Decimal = Field(..., gt=0)
    buy_date: date
    charges: Decimal = Field(Decimal("0"), ge=0)
    notes: str | None = Field(None, max_length=280)


class HoldingSell(BaseModel):
    """Sell Position: sells only `quantity` shares out of the current
    holding. The remaining quantity stays ACTIVE/PARTIAL at the original,
    unchanged average buy price."""
    quantity: Decimal = Field(..., gt=0)
    sell_price: Decimal = Field(..., gt=0)
    sell_date: date
    charges: Decimal = Field(Decimal("0"), ge=0)
    notes: str | None = Field(None, max_length=280)