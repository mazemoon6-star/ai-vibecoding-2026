"""Durable PAPER snapshots. Decimal values are stored as strings, never floats."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import get_args, get_type_hints

from .instruments import instrument_name
from .models import AutoDiscoveryConfig, AutoStrategySettings, OrderSide, OrderStatus, utc_now


KST = timezone(timedelta(hours=9))


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
            self.connection.execute("""CREATE TABLE IF NOT EXISTS trade_history (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                quantity TEXT NOT NULL,
                average_filled_price TEXT NOT NULL,
                fee TEXT NOT NULL,
                gross_amount TEXT NOT NULL,
                filled_at TEXT NOT NULL,
                trade_date_kst TEXT NOT NULL,
                strategy_id TEXT,
                client_order_id TEXT NOT NULL
            )""")
            self.connection.execute("CREATE INDEX IF NOT EXISTS trade_history_by_date ON trade_history (trade_date_kst, side, filled_at)")
            self.connection.commit()
            self._recorded_order_ids = {
                row[0] for row in self.connection.execute("SELECT order_id FROM trade_history")
            }
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
            rows = self._new_trade_rows(state)
            with self.connection:
                self.connection.execute(
                    "INSERT INTO paper_state VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET version=excluded.version, payload=excluded.payload, checksum=excluded.checksum, saved_at=excluded.saved_at",
                    (self.VERSION, payload, checksum, utc_now().isoformat()),
                )
                self._insert_trade_rows(rows)
            self._recorded_order_ids.update(row[0] for row in rows)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise StateStoreError("Could not commit PAPER state.") from exc

    def backfill_trade_history(self, state) -> None:
        """Import filled orders from older one-row snapshots exactly once."""
        try:
            rows = self._new_trade_rows(state)
            if rows:
                with self.connection:
                    self._insert_trade_rows(rows)
                self._recorded_order_ids.update(row[0] for row in rows)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            raise StateStoreError("Could not backfill PAPER trade history.") from exc

    def _new_trade_rows(self, state) -> list[tuple]:
        rows = []
        for order in state["orders"].values():
            if order["status"] != OrderStatus.FILLED or order["order_id"] in self._recorded_order_ids:
                continue
            filled_at = datetime.fromisoformat(order["filled_at"])
            if filled_at.tzinfo is None:
                raise ValueError("A filled order must have a timezone-aware fill time")
            filled_at = filled_at.astimezone(timezone.utc)
            quantity = Decimal(str(order["filled_quantity"]))
            price = Decimal(str(order["average_filled_price"]))
            fee = Decimal(str(order["fee"]))
            if not all(value.is_finite() for value in (quantity, price, fee)) or quantity <= 0 or price <= 0 or fee < 0:
                raise ValueError("Invalid filled order in PAPER state")
            symbol = order["symbol"]
            name = state.get("ticks", {}).get(symbol, {}).get("name") or instrument_name(symbol)
            rows.append((
                order["order_id"], symbol, name, str(order["side"]), str(quantity), str(price), str(fee),
                str((quantity * price).quantize(Decimal("0.00000001"))), filled_at.isoformat(),
                filled_at.astimezone(KST).date().isoformat(), order.get("strategy_id"),
                order["client_order_id"],
            ))
        return rows

    def _insert_trade_rows(self, rows: list[tuple]) -> None:
        self.connection.executemany(
            """INSERT OR IGNORE INTO trade_history
            (order_id, symbol, name, side, quantity, average_filled_price, fee,
             gross_amount, filled_at, trade_date_kst, strategy_id, client_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def trade_history(
        self, trade_date: date | None = None, side: OrderSide | None = None,
        *, limit: int = 100, offset: int = 0,
    ) -> dict:
        clauses = []
        values: list[object] = []
        if trade_date is not None:
            clauses.append("trade_date_kst = ?")
            values.append(trade_date.isoformat())
        if side is not None:
            clauses.append("side = ?")
            values.append(side.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        columns = (
            "order_id", "symbol", "name", "side", "quantity", "average_filled_price", "fee",
            "gross_amount", "filled_at", "trade_date_kst", "strategy_id", "client_order_id",
        )
        try:
            total = self.connection.execute("SELECT COUNT(*) FROM trade_history" + where, values).fetchone()[0]
            rows = self.connection.execute(
                "SELECT " + ", ".join(columns) + " FROM trade_history" + where
                + " ORDER BY filled_at DESC, order_id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError("Could not read PAPER trade history.") from exc
        return {
            "items": [dict(zip(columns, row)) for row in rows], "total": total,
            "date": trade_date.isoformat() if trade_date is not None else None,
            "side": side.value if side is not None else None,
            "limit": limit, "offset": offset, "timezone": "Asia/Seoul",
            "paper_only": True,
        }

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
    state["auto_discovery"] = engine.auto_discovery.model_dump(mode="json")
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
    values["auto_discovery"] = AutoDiscoveryConfig.model_validate(state.get("auto_discovery", {}))
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
