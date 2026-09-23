"""In-memory paper trading engine used by the FastAPI MVP.

The engine deliberately has no broker client.  It accepts market ticks and
simulates orders, which keeps version 0.1 safe to run while the strategy and
operational controls are being validated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .instruments import instrument_currency, instrument_name
from .paper_state import PaperStateStore, StateStoreError, dump_engine, restore_engine
from .models import (
    AUTO_STRATEGY_LONG_WINDOW,
    AUTO_STRATEGY_SHORT_WINDOW,
    AutoStrategySettings,
    AutoStrategySymbolRequest,
    ExecutionMode,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    StrategyAction,
    StrategyRequest,
    TickRequest,
    utc_now,
)


class EngineError(Exception):
    """A safe, user-facing business error."""

    def __init__(self, code: str, message: str, status_code: int = 400, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


@dataclass
class Tick:
    symbol: str
    name: str
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    volume: Decimal | None
    timestamp: datetime
    currency: str | None = None
    received_at: datetime = field(default_factory=utc_now)


@dataclass
class Position:
    symbol: str
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    remaining_buy_fees: Decimal = Decimal("0")


@dataclass
class PaperOrder:
    order_id: str
    client_order_id: str
    request_fingerprint: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    price: Decimal | None
    strategy_id: str | None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: Decimal = Decimal("0")
    average_filled_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    rejection_reason: str | None = None
    reserved_cash: Decimal = Decimal("0")
    reserved_quantity: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    filled_at: datetime | None = None


@dataclass
class Strategy:
    strategy_id: str
    name: str
    symbol: str
    short_window: int
    long_window: int
    order_quantity: Decimal
    max_position: Decimal | None
    enabled: bool
    previous_relation: int | None = None


class PaperEngine:
    """Thread-safe in-memory paper account and moving-average strategy runner."""

    def __init__(
        self,
        initial_cash: Decimal = Decimal("1000000"),
        fee_rate: Decimal = Decimal("0.00015"),
        slippage_rate: Decimal = Decimal("0"),
        history_size: int = 2_000,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be greater than zero")
        if fee_rate < 0 or slippage_rate < 0:
            raise ValueError("fee_rate and slippage_rate must not be negative")

        self.mode = ExecutionMode.PAPER
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate
        self.history_size = history_size
        self.trading_enabled = True
        self.kill_switch = False
        self.created_at = utc_now()

        self.ticks: dict[str, Tick] = {}
        self.tick_history: dict[str, deque[Tick]] = defaultdict(
            lambda: deque(maxlen=self.history_size)
        )
        self.price_history: dict[str, deque[Decimal]] = defaultdict(
            lambda: deque(maxlen=self.history_size)
        )
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.client_orders: dict[str, str] = {}
        self.strategies: dict[str, Strategy] = {}
        self.auto_watchlist: dict[str, datetime] = {}
        self.auto_strategy_ids: dict[str, str] = {}
        self.auto_strategy_settings: dict[str, AutoStrategySettings] = {}
        self.reserved_cash = Decimal("0")
        self.reserved_sell_quantity: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self.realized_pnl = Decimal("0")
        self.realized_pnl_before_fees = Decimal("0")
        self.metrics: dict[str, int] = defaultdict(int)
        self.lock = asyncio.Lock()
        self.state_store: PaperStateStore | None = None

    def enable_persistence(self, path) -> None:
        """Load before starting workers; never silently discard an unreadable account."""
        if self.state_store is not None:
            raise RuntimeError("Persistence is already enabled")
        store = PaperStateStore(path)
        try:
            snapshot = store.load()
            if snapshot is None:
                store.save(dump_engine(self))
            else:
                restore_engine(self, snapshot)
            self.state_store = store
        except BaseException:
            store.close()
            raise

    async def enforce_fixed_auto_windows(self) -> None:
        """Migrate persisted automatic strategies to the analyzed fixed windows."""

        async with self._mutation():
            for symbol in self.auto_watchlist:
                strategy = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
                settings = self.auto_strategy_settings.get(symbol)
                order_quantity = (
                    strategy.order_quantity if strategy is not None
                    else settings.order_quantity if settings is not None
                    else Decimal("1")
                )
                self.auto_strategy_settings[symbol] = AutoStrategySettings(order_quantity=order_quantity)
                if strategy is not None:
                    changed = (
                        strategy.short_window != AUTO_STRATEGY_SHORT_WINDOW
                        or strategy.long_window != AUTO_STRATEGY_LONG_WINDOW
                    )
                    strategy.short_window = AUTO_STRATEGY_SHORT_WINDOW
                    strategy.long_window = AUTO_STRATEGY_LONG_WINDOW
                    if changed:
                        strategy.previous_relation = None

    @asynccontextmanager
    async def _mutation(self):
        async with self.lock:
            before = dump_engine(self) if self.state_store is not None else None
            try:
                yield
                if self.state_store is not None:
                    self.state_store.save(dump_engine(self))
            except BaseException as exc:
                if before is not None:
                    restore_engine(self, before)
                if isinstance(exc, StateStoreError):
                    raise EngineError("state_storage_error", "가상계좌 저장에 실패해 변경을 취소했습니다. 저장 공간을 확인하세요.", 503) from exc
                raise

    def close_persistence(self) -> None:
        if self.state_store is not None:
            self.state_store.close()
            self.state_store = None

    async def update_tick(self, request: TickRequest) -> dict[str, Any]:
        async with self._mutation():
            tick = Tick(
                symbol=request.symbol,
                name=request.name or instrument_name(request.symbol),
                price=request.price,
                bid=request.bid,
                ask=request.ask,
                volume=request.volume,
                timestamp=request.timestamp,
                currency=request.currency or instrument_currency(request.symbol),
            )
            self.ticks[tick.symbol] = tick
            self.tick_history[tick.symbol].append(tick)
            self.price_history[tick.symbol].append(tick.price)
            self.metrics["ticks_received"] += 1

            filled_orders: list[dict[str, Any]] = []
            for order in list(self.orders.values()):
                if order.symbol != tick.symbol or order.status is not OrderStatus.PENDING:
                    continue
                if self._can_fill(order, tick):
                    self._fill_order(order, self._fill_price(order, tick))
                    filled_orders.append(self._order_dict(order))

            return {
                "tick": self._tick_dict(tick),
                "filled_orders": filled_orders,
            }

    async def place_order(self, request: OrderRequest) -> dict[str, Any]:
        async with self._mutation():
            return self._place_order_unlocked(request)

    def _place_order_unlocked(self, request: OrderRequest) -> dict[str, Any]:
        if not self.trading_enabled:
            raise EngineError("trading_paused", "paper trading is paused", 409)
        if request.symbol not in self.ticks:
            raise EngineError("price_unavailable", "submit a tick before placing an order", 409)

        client_order_id = request.client_order_id or f"paper-{uuid4().hex}"
        fingerprint = self._fingerprint(request)
        existing_id = self.client_orders.get(client_order_id)
        if existing_id is not None:
            existing = self.orders[existing_id]
            if existing.request_fingerprint != fingerprint:
                raise EngineError(
                    "idempotency_conflict",
                    "client_order_id was already used with different order data",
                    409,
                )
            return self._order_dict(existing)

        order = PaperOrder(
            order_id=f"paper-{uuid4().hex}",
            client_order_id=client_order_id,
            request_fingerprint=fingerprint,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            price=request.price,
            strategy_id=request.strategy_id,
        )
        self._reserve(order)
        self.orders[order.order_id] = order
        self.client_orders[client_order_id] = order.order_id
        self.metrics["orders_created"] += 1

        tick = self.ticks[request.symbol]
        if self._can_fill(order, tick):
            self._fill_order(order, self._fill_price(order, tick))
        return self._order_dict(order)

    def _reserve(self, order: PaperOrder) -> None:
        if order.side is OrderSide.BUY:
            if order.order_type is OrderType.MARKET:
                estimate = self._market_price(order.side, self.ticks[order.symbol])
            else:
                estimate = order.price
            assert estimate is not None
            required = self._money(estimate * order.quantity * (Decimal("1") + self.fee_rate))
            if required > self.available_cash:
                raise EngineError("insufficient_cash", "not enough available paper cash", 422)
            order.reserved_cash = required
            self.reserved_cash += required
        else:
            available = self.available_sell_quantity(order.symbol)
            if order.quantity > available:
                raise EngineError("insufficient_quantity", "not enough sellable paper quantity", 422)
            order.reserved_quantity = order.quantity
            self.reserved_sell_quantity[order.symbol] += order.quantity

    def _release_reservation(self, order: PaperOrder) -> None:
        if order.reserved_cash:
            self.reserved_cash -= order.reserved_cash
            order.reserved_cash = Decimal("0")
        if order.reserved_quantity:
            self.reserved_sell_quantity[order.symbol] -= order.reserved_quantity
            order.reserved_quantity = Decimal("0")

    def _can_fill(self, order: PaperOrder, tick: Tick) -> bool:
        if order.order_type is OrderType.MARKET:
            return True
        assert order.price is not None
        if order.side is OrderSide.BUY:
            compare_price = tick.ask or tick.price
            return compare_price <= order.price
        compare_price = tick.bid or tick.price
        return compare_price >= order.price

    def _market_price(self, side: OrderSide, tick: Tick) -> Decimal:
        base = (tick.ask if side is OrderSide.BUY else tick.bid) or tick.price
        if side is OrderSide.BUY:
            return self._money(base * (Decimal("1") + self.slippage_rate))
        return self._money(base * (Decimal("1") - self.slippage_rate))

    def _fill_price(self, order: PaperOrder, tick: Tick) -> Decimal:
        if order.order_type is OrderType.MARKET:
            return self._market_price(order.side, tick)
        assert order.price is not None
        return order.price

    def _fill_order(self, order: PaperOrder, price: Decimal, *, recorded_fee: Decimal | None = None) -> None:
        self._release_reservation(order)
        notional = self._money(price * order.quantity)
        fee = recorded_fee if recorded_fee is not None else self._money(notional * self.fee_rate)
        position = self.positions.setdefault(order.symbol, Position(order.symbol))

        if order.side is OrderSide.BUY:
            total = notional + fee
            if total > self.cash:
                self._reject(order, "available cash changed before fill")
                return
            new_quantity = position.quantity + order.quantity
            position.average_price = self._money(
                ((position.quantity * position.average_price) + notional) / new_quantity
            )
            position.quantity = new_quantity
            position.remaining_buy_fees += fee
            self.cash -= total
        else:
            if order.quantity > position.quantity:
                self._reject(order, "sellable quantity changed before fill")
                return
            # Recognize acquisition fees only for the quantity being sold.
            # The final sale consumes the full residual to avoid rounding drift.
            buy_fee = position.remaining_buy_fees if order.quantity == position.quantity else self._money(
                position.remaining_buy_fees * order.quantity / position.quantity
            )
            gross_pnl = self._money((price - position.average_price) * order.quantity)
            pnl = self._money(gross_pnl - buy_fee - fee)
            position.remaining_buy_fees -= buy_fee
            position.quantity -= order.quantity
            position.realized_pnl += pnl
            self.realized_pnl += pnl
            self.realized_pnl_before_fees += gross_pnl
            self.cash += notional - fee
            if position.quantity == 0:
                position.average_price = Decimal("0")

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_filled_price = price
        order.fee = fee
        order.filled_at = utc_now()
        order.updated_at = order.filled_at
        self.metrics["orders_filled"] += 1

    def _reject(self, order: PaperOrder, reason: str) -> None:
        self._release_reservation(order)
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        order.updated_at = utc_now()
        self.metrics["orders_rejected"] += 1

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        async with self._mutation():
            order = self.orders.get(order_id)
            if order is None:
                raise EngineError("order_not_found", "order was not found", 404)
            if order.status is not OrderStatus.PENDING:
                raise EngineError("order_not_pending", "only pending orders can be canceled", 409)
            self._release_reservation(order)
            order.status = OrderStatus.CANCELED
            order.updated_at = utc_now()
            self.metrics["orders_canceled"] += 1
            return self._order_dict(order)

    async def create_strategy(self, request: StrategyRequest) -> dict[str, Any]:
        async with self._mutation():
            strategy = Strategy(
                strategy_id=f"strategy-{uuid4().hex[:12]}",
                name=request.name,
                symbol=request.symbol,
                short_window=request.short_window,
                long_window=request.long_window,
                order_quantity=request.order_quantity,
                max_position=request.max_position,
                enabled=request.enabled,
            )
            self.strategies[strategy.strategy_id] = strategy
            return self._strategy_dict(strategy)

    async def list_strategies(self) -> list[dict[str, Any]]:
        async with self.lock:
            return [self._strategy_dict(strategy) for strategy in self.strategies.values()]

    async def run_strategies(self) -> list[dict[str, Any]]:
        async with self._mutation():
            results: list[dict[str, Any]] = []
            for strategy in self.strategies.values():
                results.append(self._run_strategy_unlocked(strategy))
            return results

    def _run_strategy_unlocked(self, strategy: Strategy) -> dict[str, Any]:
        if not strategy.enabled:
            return {
                "strategy_id": strategy.strategy_id,
                "symbol": strategy.symbol,
                "action": StrategyAction.HOLD,
                "reason": "disabled",
            }
        history = self.price_history[strategy.symbol]
        if len(history) < strategy.long_window:
            return {
                "strategy_id": strategy.strategy_id,
                "symbol": strategy.symbol,
                "action": StrategyAction.HOLD,
                "reason": "warming_up",
                "samples": len(history),
                "required_samples": strategy.long_window,
            }
        short_avg = sum(list(history)[-strategy.short_window :], Decimal("0")) / strategy.short_window
        long_avg = sum(list(history)[-strategy.long_window :], Decimal("0")) / strategy.long_window
        relation = 1 if short_avg > long_avg else -1 if short_avg < long_avg else 0
        previous = strategy.previous_relation
        strategy.previous_relation = relation

        action = StrategyAction.HOLD
        reason = "no_crossing"
        order: dict[str, Any] | None = None
        position = self.positions.get(strategy.symbol, Position(strategy.symbol))
        if previous is not None and previous <= 0 < relation:
            action = StrategyAction.BUY
            reason = "moving_average_cross_up"
            target_quantity = strategy.order_quantity
            if strategy.max_position is not None:
                target_quantity = min(target_quantity, strategy.max_position - position.quantity)
            if target_quantity > 0 and not self._has_pending_strategy_order(strategy.strategy_id):
                try:
                    order = self._place_order_unlocked(
                        OrderRequest(
                            symbol=strategy.symbol,
                            side=OrderSide.BUY,
                            quantity=target_quantity,
                            order_type=OrderType.MARKET,
                            strategy_id=strategy.strategy_id,
                        )
                    )
                except EngineError as exc:
                    reason = f"order_rejected:{exc.code}"
        elif previous is not None and previous >= 0 > relation:
            action = StrategyAction.SELL
            reason = "moving_average_cross_down"
            target_quantity = min(strategy.order_quantity, position.quantity)
            if target_quantity > 0 and not self._has_pending_strategy_order(strategy.strategy_id):
                try:
                    order = self._place_order_unlocked(
                        OrderRequest(
                            symbol=strategy.symbol,
                            side=OrderSide.SELL,
                            quantity=target_quantity,
                            order_type=OrderType.MARKET,
                            strategy_id=strategy.strategy_id,
                        )
                    )
                except EngineError as exc:
                    reason = f"order_rejected:{exc.code}"

        return {
            "strategy_id": strategy.strategy_id,
            "symbol": strategy.symbol,
            "action": action,
            "reason": reason,
            "short_average": self._money(short_avg),
            "long_average": self._money(long_avg),
            "order": order,
        }

    def _has_pending_strategy_order(self, strategy_id: str) -> bool:
        return any(
            order.strategy_id == strategy_id and order.status is OrderStatus.PENDING
            for order in self.orders.values()
        )

    async def pause(self) -> dict[str, Any]:
        async with self._mutation():
            self.trading_enabled = False
            return self.control_state("paper trading paused")

    async def resume(self) -> dict[str, Any]:
        async with self._mutation():
            self.kill_switch = False
            self.trading_enabled = True
            return self.control_state("paper trading resumed")

    async def kill(self) -> dict[str, Any]:
        async with self._mutation():
            self.kill_switch = True
            self.trading_enabled = False
            return self.control_state("kill switch enabled")

    def control_state(self, message: str) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "trading_enabled": self.trading_enabled,
            "kill_switch": self.kill_switch,
            "message": message,
        }

    async def account(self) -> dict[str, Any]:
        async with self.lock:
            market_value = sum(
                (
                    position.quantity * self.ticks[position.symbol].price
                    for position in self.positions.values()
                    if position.symbol in self.ticks
                ),
                Decimal("0"),
            )
            return {
                "mode": self.mode,
                "initial_cash": self.initial_cash,
                "cash": self.cash,
                "reserved_cash": self.reserved_cash,
                "available_cash": self.available_cash,
                "market_value": self._money(market_value),
                "equity": self._money(self.cash + market_value),
                "realized_pnl": self.realized_pnl,
                "realized_pnl_before_fees": self.realized_pnl_before_fees,
                "realized_fees": self.realized_pnl_before_fees - self.realized_pnl,
                "trading_enabled": self.trading_enabled,
                "kill_switch": self.kill_switch,
                "as_of": utc_now(),
            }

    async def positions_snapshot(self) -> list[dict[str, Any]]:
        async with self.lock:
            return [self._position_dict(position) for position in self.positions.values()]

    async def ticks_snapshot(self) -> list[dict[str, Any]]:
        """Return up to ten symbols with the newest ticks."""

        async with self.lock:
            latest = sorted(
                self.ticks.values(),
                key=lambda tick: (tick.timestamp, tick.received_at),
                reverse=True,
            )[:10]
            return [self._tick_dict(tick) for tick in latest]

    async def chart_snapshot(self, symbol: str) -> dict[str, Any]:
        """Return one-minute close bars and filled buy markers for a symbol."""

        normalized = symbol.strip().upper()
        async with self.lock:
            tick = self.ticks.get(normalized)
            items = self.tick_history.get(normalized, ())
            markers = [
                {
                    "price": order.average_filled_price,
                    "timestamp": order.filled_at,
                }
                for order in self.orders.values()
                if order.symbol == normalized
                and order.side is OrderSide.BUY
                and order.status is OrderStatus.FILLED
                and order.average_filled_price is not None
                and order.filled_at is not None
            ]
            bars: dict[int, dict[str, Any]] = {}
            for item in sorted(items, key=lambda value: value.timestamp):
                bucket = int(item.timestamp.timestamp()) // 60 * 60
                bar = bars.get(bucket)
                if bar is None:
                    bars[bucket] = {
                        "timestamp": datetime.fromtimestamp(bucket, tz=timezone.utc),
                        "open": item.price,
                        "high": item.price,
                        "low": item.price,
                        "close": item.price,
                        "price": item.price,
                        "volume": item.volume or Decimal("0"),
                    }
                else:
                    bar["high"] = max(bar["high"], item.price)
                    bar["low"] = min(bar["low"], item.price)
                    bar["close"] = item.price
                    bar["price"] = item.price
                    bar["volume"] += item.volume or Decimal("0")
            return {
                "symbol": normalized,
                "name": tick.name if tick else instrument_name(normalized),
                "interval": "1m",
                "items": [
                    bar for bar in bars.values()
                ],
                "buy_markers": markers,
            }

    async def add_auto_symbol(self, request: AutoStrategySymbolRequest) -> dict[str, Any]:
        async with self._mutation():
            tick = self.ticks.get(request.symbol)
            if tick is None:
                raise EngineError("price_unavailable", "최근 시세에 있는 종목만 자동매매 목록에 담을 수 있습니다.", 409)
            self.auto_watchlist.setdefault(request.symbol, utc_now())
            self.auto_strategy_settings.setdefault(request.symbol, AutoStrategySettings())
            return self._auto_watchlist_dict(request.symbol)

    async def auto_watchlist_snapshot(self) -> list[dict[str, Any]]:
        async with self.lock:
            return [self._auto_watchlist_dict(symbol) for symbol in self.auto_watchlist]

    async def remove_auto_symbol(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.strip().upper()
        async with self._mutation():
            if normalized not in self.auto_watchlist:
                raise EngineError("auto_symbol_not_found", "자동매매 목록에서 종목을 찾을 수 없습니다.", 404)
            self.auto_watchlist.pop(normalized)
            self.auto_strategy_settings.pop(normalized, None)
            strategy_id = self.auto_strategy_ids.get(normalized)
            if strategy_id in self.strategies:
                self.strategies[strategy_id].enabled = False
            return {"symbol": normalized, "removed": True}

    async def activate_auto_strategies(
        self,
        settings_by_symbol: dict[str, AutoStrategySettings] | None = None,
    ) -> list[dict[str, Any]]:
        """Create or update each queued symbol using its own PAPER settings."""

        settings_by_symbol = settings_by_symbol or {}
        async with self._mutation():
            for symbol in self.auto_watchlist:
                settings = settings_by_symbol.get(
                    symbol,
                    self.auto_strategy_settings.get(symbol, AutoStrategySettings()),
                )
                self.auto_strategy_settings[symbol] = settings
                strategy_id = self.auto_strategy_ids.get(symbol)
                strategy = self.strategies.get(strategy_id or "")
                if strategy is None:
                    strategy = Strategy(
                        strategy_id=f"auto-{uuid4().hex[:12]}",
                        name=f"auto-{symbol}",
                        symbol=symbol,
                        short_window=settings.short_window,
                        long_window=settings.long_window,
                        order_quantity=settings.order_quantity,
                        max_position=settings.order_quantity,
                        enabled=True,
                    )
                    self.strategies[strategy.strategy_id] = strategy
                    self.auto_strategy_ids[symbol] = strategy.strategy_id
                else:
                    parameters_changed = (
                        strategy.short_window != settings.short_window
                        or strategy.long_window != settings.long_window
                        or strategy.order_quantity != settings.order_quantity
                    )
                    strategy.short_window = settings.short_window
                    strategy.long_window = settings.long_window
                    strategy.order_quantity = settings.order_quantity
                    strategy.max_position = settings.order_quantity
                    strategy.enabled = True
                    if parameters_changed:
                        strategy.previous_relation = None
            return [self._auto_watchlist_dict(symbol) for symbol in self.auto_watchlist]

    async def auto_strategy_symbols(self) -> list[str]:
        async with self.lock:
            return [
                symbol for symbol in self.auto_watchlist
                if (strategy := self.strategies.get(self.auto_strategy_ids.get(symbol, ""))) is not None
                and strategy.enabled
            ]

    def _auto_watchlist_dict(self, symbol: str) -> dict[str, Any]:
        tick = self.ticks.get(symbol)
        strategy = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
        settings = self.auto_strategy_settings.get(symbol)
        short_window = strategy.short_window if strategy is not None else settings.short_window if settings else None
        long_window = strategy.long_window if strategy is not None else settings.long_window if settings else None
        order_quantity = strategy.order_quantity if strategy is not None else settings.order_quantity if settings else None
        return {
            "symbol": symbol,
            "name": tick.name if tick else instrument_name(symbol),
            "added_at": self.auto_watchlist[symbol],
            "status": "운영 중" if strategy is not None and strategy.enabled else "대기 중",
            "strategy_id": strategy.strategy_id if strategy is not None else None,
            "short_window": short_window,
            "long_window": long_window,
            "order_quantity": order_quantity,
        }

    async def orders_snapshot(self, status: OrderStatus | None = None) -> list[dict[str, Any]]:
        async with self.lock:
            orders = self.orders.values()
            if status is not None:
                orders = (order for order in orders if order.status is status)
            return [self._order_dict(order) for order in orders]

    async def metrics_snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "mode": self.mode,
                "trading_enabled": self.trading_enabled,
                "ticks_received": self.metrics["ticks_received"],
                "orders_created": self.metrics["orders_created"],
                "orders_filled": self.metrics["orders_filled"],
                "orders_rejected": self.metrics["orders_rejected"],
                "orders_canceled": self.metrics["orders_canceled"],
                "symbols": sorted(self.ticks),
                "strategies": len(self.strategies),
            }

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    def available_sell_quantity(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        quantity = position.quantity if position else Decimal("0")
        return quantity - self.reserved_sell_quantity[symbol]

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.00000001"))

    @staticmethod
    def _fingerprint(request: OrderRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"client_order_id"})
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _tick_dict(tick: Tick) -> dict[str, Any]:
        return {
            "symbol": tick.symbol,
            "name": tick.name,
            "price": tick.price,
            "bid": tick.bid,
            "ask": tick.ask,
            "volume": tick.volume,
            "currency": tick.currency or instrument_currency(tick.symbol),
            "timestamp": tick.timestamp,
            "received_at": tick.received_at,
        }

    def _position_dict(self, position: Position) -> dict[str, Any]:
        tick = self.ticks.get(position.symbol)
        market_price = tick.price if tick else None
        market_value = position.quantity * market_price if market_price is not None else None
        cost_basis = position.quantity * position.average_price
        unrealized = (
            (market_price - position.average_price) * position.quantity
            if market_price is not None
            else None
        )
        unrealized_return = unrealized / cost_basis if unrealized is not None and cost_basis > 0 else None
        return {
            "symbol": position.symbol,
            "name": tick.name if tick else instrument_name(position.symbol),
            "currency": (tick.currency if tick else None) or instrument_currency(position.symbol),
            "quantity": position.quantity,
            "average_price": position.average_price,
            "market_price": market_price,
            "price_updated_at": tick.timestamp if tick else None,
            "cost_basis": self._money(cost_basis),
            "market_value": self._money(market_value) if market_value is not None else None,
            "unrealized_pnl": self._money(unrealized) if unrealized is not None else None,
            "unrealized_return": self._money(unrealized_return) if unrealized_return is not None else None,
            "realized_pnl": position.realized_pnl,
            "reserved_quantity": self.reserved_sell_quantity[position.symbol],
        }

    @staticmethod
    def _strategy_dict(strategy: Strategy) -> dict[str, Any]:
        return {
            "strategy_id": strategy.strategy_id,
            "name": strategy.name,
            "symbol": strategy.symbol,
            "short_window": strategy.short_window,
            "long_window": strategy.long_window,
            "order_quantity": strategy.order_quantity,
            "max_position": strategy.max_position,
            "enabled": strategy.enabled,
            "previous_relation": strategy.previous_relation,
        }

    @staticmethod
    def _order_dict(order: PaperOrder) -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "name": instrument_name(order.symbol),
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "price": order.price,
            "strategy_id": order.strategy_id,
            "status": order.status,
            "filled_quantity": order.filled_quantity,
            "average_filled_price": order.average_filled_price,
            "fee": order.fee,
            "rejection_reason": order.rejection_reason,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "filled_at": order.filled_at,
        }
