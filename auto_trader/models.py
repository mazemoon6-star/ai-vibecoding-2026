"""API and domain models for the paper trading MVP."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class ExecutionMode(StrEnum):
    PAPER = "PAPER"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class StrategyAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class TickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    price: Decimal = Field(gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    volume: Decimal | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be blank")
        return value

    @model_validator(mode="after")
    def validate_book(self) -> "TickRequest":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid must be less than or equal to ask")
        return self


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = Field(default=None, gt=0)
    strategy_id: str | None = Field(default=None, max_length=64)
    client_order_id: str | None = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be blank")
        return value

    @model_validator(mode="after")
    def validate_price(self) -> "OrderRequest":
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("price is required for LIMIT orders")
        if self.order_type is OrderType.MARKET and self.price is not None:
            raise ValueError("price is not allowed for MARKET orders")
        return self


class StrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    short_window: int = Field(default=3, ge=2, le=200)
    long_window: int = Field(default=8, ge=3, le=500)
    order_quantity: Decimal = Field(gt=0)
    max_position: Decimal | None = Field(default=None, gt=0)
    enabled: bool = True

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("symbol must not be blank")
        return value

    @model_validator(mode="after")
    def validate_windows(self) -> "StrategyRequest":
        if self.short_window >= self.long_window:
            raise ValueError("short_window must be less than long_window")
        return self


class ControlResponse(BaseModel):
    mode: ExecutionMode
    trading_enabled: bool
    message: str


class ApiError(BaseModel):
    code: str
    message: str
    data: dict[str, Any] | None = None
