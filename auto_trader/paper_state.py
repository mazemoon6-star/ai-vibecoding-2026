"""Durable PAPER snapshots. Decimal values are stored as strings, never floats."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict, fields
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import get_args, get_type_hints

from .models import AutoStrategySettings, OrderSide, OrderStatus, utc_now


class StateStoreError(RuntimeError):
    pass


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported state value: {type(value).__name__}")


class PaperStateStore:
    """One process owns the database; each snapshot is an atomic transaction."""

    VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = None
        self.owner = None
        self._locked = False
        try:
            self.owner = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
            if self.owner.seek(0, 2) == 0:
                self.owner.write(b"0")
                self.owner.flush()
            self.owner.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            self.connection = sqlite3.connect(self.path, timeout=5)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS paper_state (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL, saved_at TEXT NOT NULL)")
            self.connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StateStoreError("Cannot open PAPER state storage (or another server owns it).") from exc

    def load(self):
        try:
            row = self.connection.execute("SELECT version, payload, checksum FROM paper_state WHERE id=1").fetchone()
            if row is None:
                return None
            version, payload, checksum = row
            if version != self.VERSION:
                raise StateStoreError("Unsupported PAPER state version; refusing to reset the account.")
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != checksum:
                raise StateStoreError("PAPER state checksum mismatch; refusing to reset the account.")
            return json.loads(payload)
        except (sqlite3.Error, ValueError) as exc:
            raise StateStoreError("Cannot read PAPER state; refusing to reset the account.") from exc

    def save(self, state):
        try:
            payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=json_value, allow_nan=False)
            checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            with self.connection:
                self.connection.execute(
                    "INSERT INTO paper_state VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET version=excluded.version, payload=excluded.payload, checksum=excluded.checksum, saved_at=excluded.saved_at",
                    (self.VERSION, payload, checksum, utc_now().isoformat()),
                )
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise StateStoreError("Could not commit PAPER state.") from exc

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.owner is not None:
            try:
                if self._locked:
                    self.owner.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.owner.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.owner.fileno(), fcntl.LOCK_UN)
            finally:
                self.owner.close()
                self.owner = None
                self._locked = False


SCALARS = (
    "initial_cash", "cash", "fee_rate", "slippage_rate", "history_size", "trading_enabled",
    "kill_switch", "created_at", "reserved_cash", "realized_pnl", "realized_pnl_before_fees",
)


def dump_engine(engine):
    state = {key: getattr(engine, key) for key in SCALARS}
    state["mode"] = engine.mode.value
    for key in ("ticks", "positions", "orders", "strategies"):
        state[key] = {identifier: asdict(item) for identifier, item in getattr(engine, key).items()}
    state["tick_history"] = {symbol: [asdict(tick) for tick in history] for symbol, history in engine.tick_history.items()}
    state["price_history"] = {symbol: list(history) for symbol, history in engine.price_history.items()}
    for key in ("client_orders", "auto_watchlist", "auto_strategy_ids", "reserved_sell_quantity", "metrics"):
        state[key] = dict(getattr(engine, key))
    state["auto_strategy_settings"] = {symbol: settings.model_dump(mode="json") for symbol, settings in engine.auto_strategy_settings.items()}
    return json.loads(json.dumps(state, default=json_value, allow_nan=False))


def restore_record(cls, values):
    types = get_type_hints(cls)
    converted = {}
    for field in fields(cls):
        if field.name not in values:
            continue
        value = values[field.name]
        kind = types[field.name]
        if value is not None:
            candidates = get_args(kind) or (kind,)
            if Decimal in candidates:
                value = Decimal(value)
                if not value.is_finite():
                    raise ValueError("Non-finite amount in PAPER state")
            elif datetime in candidates:
                value = datetime.fromisoformat(value)
            elif isinstance(kind, type) and issubclass(kind, Enum):
                value = kind(value)
        converted[field.name] = value
    return cls(**converted)


def restore_engine(engine, state):
    # Import here to keep engine/state dependencies acyclic.
    from .paper_engine import PaperOrder, Position, Strategy, Tick

    if state.get("mode") != "PAPER":
        raise ValueError("Only PAPER state can be restored")
    values = {key: state[key] for key in SCALARS}
    for key in ("initial_cash", "cash", "fee_rate", "slippage_rate", "reserved_cash", "realized_pnl", "realized_pnl_before_fees"):
        values[key] = Decimal(values[key])
        if not values[key].is_finite():
            raise ValueError("Non-finite account amount")
    if values["initial_cash"] <= 0 or any(values[key] < 0 for key in ("cash", "fee_rate", "slippage_rate", "reserved_cash")):
        raise ValueError("Invalid account amounts")
    if type(values["history_size"]) is not int or values["history_size"] < 1:
        raise ValueError("Invalid history size")
    if type(values["trading_enabled"]) is not bool or type(values["kill_switch"]) is not bool:
        raise ValueError("Invalid control flags")
    values["created_at"] = datetime.fromisoformat(values["created_at"])
    for key, cls in (("ticks", Tick), ("positions", Position), ("orders", PaperOrder), ("strategies", Strategy)):
        values[key] = {identifier: restore_record(cls, item) for identifier, item in state[key].items()}
    size = values["history_size"]
    values["tick_history"] = defaultdict(lambda: deque(maxlen=size), {
        symbol: deque((restore_record(Tick, tick) for tick in items), maxlen=size) for symbol, items in state["tick_history"].items()
    })
    values["price_history"] = defaultdict(lambda: deque(maxlen=size), {
        symbol: deque((Decimal(price) for price in items), maxlen=size) for symbol, items in state["price_history"].items()
    })
    values["client_orders"] = dict(state["client_orders"])
    values["auto_watchlist"] = {symbol: datetime.fromisoformat(added) for symbol, added in state["auto_watchlist"].items()}
    values["auto_strategy_ids"] = dict(state["auto_strategy_ids"])
    values["auto_strategy_settings"] = {symbol: AutoStrategySettings.model_validate(item) for symbol, item in state["auto_strategy_settings"].items()}
    values["reserved_sell_quantity"] = defaultdict(lambda: Decimal("0"), {symbol: Decimal(amount) for symbol, amount in state["reserved_sell_quantity"].items()})
    values["metrics"] = defaultdict(int, state["metrics"])
    pending = [order for order in values["orders"].values() if order.status is OrderStatus.PENDING]
    if sum((order.reserved_cash for order in pending), Decimal("0")) != values["reserved_cash"]:
        raise ValueError("Pending cash reservation mismatch")
    for symbol, position in values["positions"].items():
        reserved = sum((order.reserved_quantity for order in pending if order.symbol == symbol and order.side is OrderSide.SELL), Decimal("0"))
        if position.quantity < 0 or position.remaining_buy_fees < 0 or reserved != values["reserved_sell_quantity"].get(symbol, Decimal("0")) or reserved > position.quantity:
            raise ValueError("Position or sell reservation mismatch")
    if sum((p.realized_pnl for p in values["positions"].values()), Decimal("0")) != values["realized_pnl"]:
        raise ValueError("Realized PNL mismatch")
    for client_id, order_id in values["client_orders"].items():
        if values["orders"][order_id].client_order_id != client_id:
            raise ValueError("Idempotency index mismatch")
    # Install only after the entire snapshot passes decoding and validation.
    for key, value in values.items():
        setattr(engine, key, value)
