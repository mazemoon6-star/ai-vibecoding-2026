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
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_FLOOR
from typing import Any
from uuid import uuid4

from .instruments import instrument_currency, instrument_name
from .paper_state import PaperStateStore, StateStoreError, dump_engine, restore_engine
from .models import (
    AUTO_STRATEGY_LONG_WINDOW,
    AUTO_STRATEGY_SHORT_WINDOW,
    AutoDiscoveryConfig,
    AutoDiscoveryRequest,
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
    entry_enabled: bool = True
    previous_relation: int | None = None
    investment_budget: Decimal | None = None


class PaperEngine:
    """Thread-safe in-memory paper account and moving-average strategy runner."""

    def __init__(
        self,
        initial_cash: Decimal = Decimal("10000000"),
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
        self.auto_discovery = AutoDiscoveryConfig()
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

    async def configure_untraded_initial_cash(self, initial_cash: Decimal) -> None:
        """Apply starting capital only while the saved account has never traded."""
        if not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError("initial_cash must be a finite positive decimal")
        async with self._mutation():
            if (
                self.orders or self.positions
                or self.cash != self.initial_cash
                or self.reserved_cash != 0
                or self.realized_pnl != 0
                or self.realized_pnl_before_fees != 0
            ):
                return
            self.initial_cash = initial_cash
            self.cash = initial_cash

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
                if not self.trading_enabled or self.kill_switch:
                    break
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
            strategy = self.strategies.get(order.strategy_id or "")
            if strategy is not None and strategy.investment_budget is not None:
                if order.symbol != strategy.symbol or not strategy.entry_enabled:
                    raise EngineError("invalid_budget_order", "현재 배분 대상 종목의 매수 주문만 가능합니다.", 409)
                if required > self._budget_remaining_unlocked(strategy):
                    raise EngineError("investment_budget_exceeded", "종목별 또는 총 투자금 한도를 초과합니다.", 422)
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
            if strategy.investment_budget is not None:
                target_quantity = self._budget_order_plan_unlocked(strategy)["quantity"]
            elif strategy.max_position is not None:
                target_quantity = min(target_quantity, strategy.max_position - position.quantity)
            if not strategy.entry_enabled:
                action = StrategyAction.HOLD
                reason = "not_selected_for_entry"
            elif target_quantity > 0 and not self._has_pending_strategy_order(strategy.strategy_id):
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
            elif target_quantity <= 0 and strategy.investment_budget is not None:
                action = StrategyAction.HOLD
                reason = "investment_budget_exhausted"
        elif previous is not None and previous >= 0 > relation:
            action = StrategyAction.SELL
            reason = "moving_average_cross_down"
            target_quantity = min(strategy.order_quantity, position.quantity)
            if strategy.investment_budget is not None:
                target_quantity = self.available_sell_quantity(strategy.symbol)
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
            self.auto_discovery.revision += 1
            self.trading_enabled = False
            return self.control_state("paper trading paused")

    async def resume(self) -> dict[str, Any]:
        async with self._mutation():
            self.auto_discovery.revision += 1
            self.kill_switch = False
            self.trading_enabled = True
            return self.control_state("paper trading resumed")

    async def kill(self) -> dict[str, Any]:
        async with self._mutation():
            self.auto_discovery.revision += 1
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
                current = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
                settings = settings_by_symbol.get(
                    symbol,
                    self.auto_strategy_settings.get(symbol, AutoStrategySettings()),
                )
                entry_enabled = current.entry_enabled if current is not None else True
                if symbol in self.auto_discovery.managed_symbols:
                    settings = self.auto_strategy_settings.get(symbol, settings)
                self._activate_auto_strategy_unlocked(symbol, settings, entry_enabled=entry_enabled)
            return [self._auto_watchlist_dict(symbol) for symbol in self.auto_watchlist]

    def _activate_auto_strategy_unlocked(
        self, symbol: str, settings: AutoStrategySettings, *, entry_enabled: bool = True
    ) -> None:
        self.auto_strategy_settings[symbol] = settings
        strategy = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
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
                entry_enabled=entry_enabled,
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
            strategy.max_position = None if strategy.investment_budget is not None else settings.order_quantity
            strategy.enabled = True
            strategy.entry_enabled = entry_enabled
            if parameters_changed:
                strategy.previous_relation = None

    def _position_investment_unlocked(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return Decimal("0")
        return self._money(position.quantity * position.average_price + position.remaining_buy_fees)

    def _committed_investment_unlocked(self, symbols: set[str]) -> Decimal:
        holdings = sum((self._position_investment_unlocked(symbol) for symbol in symbols), Decimal("0"))
        pending = sum((
            order.reserved_cash for order in self.orders.values()
            if order.symbol in symbols and order.side is OrderSide.BUY and order.status is OrderStatus.PENDING
        ), Decimal("0"))
        return holdings + pending

    def _budget_remaining_unlocked(self, strategy: Strategy) -> Decimal:
        if strategy.investment_budget is None or self.auto_discovery.total_investment is None:
            return Decimal("0")
        symbol_remaining = strategy.investment_budget - self._committed_investment_unlocked({strategy.symbol})
        managed = set(self.auto_discovery.managed_symbols) | {strategy.symbol}
        total_remaining = self.auto_discovery.total_investment - self._committed_investment_unlocked(managed)
        return max(Decimal("0"), min(symbol_remaining, total_remaining, self.available_cash))

    def _budget_order_plan_unlocked(
        self, strategy: Strategy, *, available_budget: Decimal | None = None
    ) -> dict[str, Decimal]:
        """Use the same price, fee rounding and cash limits as PAPER execution."""
        remaining = self._budget_remaining_unlocked(strategy)
        if available_budget is not None:
            remaining = min(remaining, max(Decimal("0"), available_budget))
        tick = self.ticks.get(strategy.symbol)
        if tick is None:
            return {"quantity": Decimal("0"), "price": Decimal("0"), "fee": Decimal("0"),
                    "estimated_total": Decimal("0"), "remaining": remaining}
        price = self._market_price(OrderSide.BUY, tick)
        if price <= 0:
            return {"quantity": Decimal("0"), "price": price, "fee": Decimal("0"),
                    "estimated_total": Decimal("0"), "remaining": remaining}
        quantity = (remaining / (price * (Decimal("1") + self.fee_rate))).to_integral_value(rounding=ROUND_FLOOR)
        if not strategy.entry_enabled or (tick.currency or instrument_currency(strategy.symbol)) != "KRW":
            quantity = Decimal("0")
        notional = self._money(price * quantity)
        fee = self._money(notional * self.fee_rate)
        if quantity > 0 and notional + fee > remaining:
            quantity -= 1
            notional = self._money(price * quantity)
            fee = self._money(notional * self.fee_rate)
        return {"quantity": quantity, "price": price, "fee": fee,
                "estimated_total": notional + fee, "remaining": remaining}

    async def auto_allocation_snapshot(self) -> dict[str, Any] | None:
        async with self.lock:
            total = self.auto_discovery.total_investment
            if total is None:
                return None
            managed = set(self.auto_discovery.managed_symbols)
            committed = self._committed_investment_unlocked(managed)
            preview_remaining = max(Decimal("0"), min(total - committed, self.available_cash))
            items = []
            retiring_cost = Decimal("0")
            for symbol in self.auto_discovery.managed_symbols:
                strategy = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
                if strategy is None or not strategy.entry_enabled:
                    retiring_cost += self._committed_investment_unlocked({symbol})
                    continue
                if strategy.investment_budget is None:
                    continue
                plan = self._budget_order_plan_unlocked(strategy, available_budget=preview_remaining)
                preview_remaining -= plan["estimated_total"]
                tick = self.ticks.get(symbol)
                items.append({
                    "symbol": symbol, "name": tick.name if tick else instrument_name(symbol),
                    "budget": strategy.investment_budget,
                    "committed": self._committed_investment_unlocked({symbol}),
                    "estimated_quantity": plan["quantity"], "estimated_price": plan["price"],
                    "estimated_fee": plan["fee"], "estimated_total": plan["estimated_total"],
                    "remaining": plan["remaining"],
                })
            return {
                "currency": "KRW", "total_investment": total, "committed": committed,
                "retiring_committed": retiring_cost,
                "available_for_investment": max(Decimal("0"), min(total - committed, self.available_cash)),
                "fee_rate": self.fee_rate, "slippage_rate": self.slippage_rate, "items": items,
            }

    async def apply_auto_discovery_selection(
        self,
        request: AutoDiscoveryRequest,
        symbols: list[str],
        *,
        expected_revision: int,
        start: bool = False,
    ) -> list[dict[str, Any]]:
        """Commit selection and strategy changes together after quotes are available."""
        selected = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        async with self._mutation():
            if self.auto_discovery.revision != expected_revision:
                raise EngineError("discovery_interrupted", "운영 설정이 변경되어 진행 중인 자동 발굴을 취소했습니다.", 409)
            if not start and (not self.auto_discovery.enabled or not self.trading_enabled):
                raise EngineError("discovery_interrupted", "자동 발굴이 정지되었습니다.", 409)
            if not selected or len(selected) > request.max_symbols:
                raise EngineError("invalid_selection", "자동 선정 종목 수가 올바르지 않습니다.", 422)
            if any(symbol not in self.ticks for symbol in selected):
                raise EngineError("price_unavailable", "선정 종목의 현재가를 확보하지 못했습니다.", 409)

            per_symbol_budget = None
            if request.total_investment is not None:
                managed = set(self.auto_discovery.managed_symbols) | set(selected)
                held = {symbol for symbol in managed if self.positions.get(symbol, Position(symbol)).quantity > 0}
                if any(
                    (self.ticks[symbol].currency or instrument_currency(symbol)) != "KRW"
                    for symbol in set(selected) | held if symbol in self.ticks
                ) or any(instrument_currency(symbol) != "KRW" for symbol in held if symbol not in self.ticks):
                    raise EngineError("budget_currency_mismatch", "원화 배분 대상에는 원화 종목만 포함할 수 있습니다.", 422)
                capacity = self.available_cash + sum((self._position_investment_unlocked(symbol) for symbol in held), Decimal("0"))
                if start and request.total_investment > capacity:
                    raise EngineError("insufficient_investment_cash", "총 투자금액은 가용현금과 기존 배분 대상 보유금액의 합 이하여야 합니다.", 422)
                retiring_cost = sum(
                    (self._position_investment_unlocked(symbol) for symbol in held - set(selected)), Decimal("0")
                )
                distributable = max(Decimal("0"), min(request.total_investment, capacity) - retiring_cost)
                per_symbol_budget = (distributable / len(selected)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

            retiring = []
            for symbol in self.auto_discovery.managed_symbols:
                if symbol in selected:
                    continue
                strategy = self.strategies.get(self.auto_strategy_ids.get(symbol, ""))
                position = self.positions.get(symbol)
                if position is not None and position.quantity > 0:
                    retiring.append(symbol)
                    self.auto_watchlist.setdefault(symbol, utc_now())
                    settings = self.auto_strategy_settings.get(symbol, AutoStrategySettings())
                    self._activate_auto_strategy_unlocked(symbol, settings, entry_enabled=False)
                else:
                    self.auto_watchlist.pop(symbol, None)
                    self.auto_strategy_settings.pop(symbol, None)
                    if strategy is not None:
                        strategy.enabled = False
                # An excluded symbol must never fill a previously queued entry.
                for order in self.orders.values():
                    if (order.symbol == symbol and order.side is OrderSide.BUY
                            and order.strategy_id == self.auto_strategy_ids.get(symbol)
                            and order.status is OrderStatus.PENDING):
                        self._release_reservation(order)
                        order.status = OrderStatus.CANCELED
                        order.updated_at = utc_now()
                        self.metrics["orders_canceled"] += 1

            for symbol in selected:
                self.auto_watchlist.setdefault(symbol, utc_now())
                self._activate_auto_strategy_unlocked(
                    symbol, AutoStrategySettings(order_quantity=request.order_quantity)
                )
                strategy = self.strategies[self.auto_strategy_ids[symbol]]
                strategy.investment_budget = per_symbol_budget
                strategy.max_position = None if per_symbol_budget is not None else request.order_quantity
            self.auto_discovery = AutoDiscoveryConfig(
                **request.model_dump(),
                enabled=True,
                revision=expected_revision + 1,
                managed_symbols=selected + retiring,
            )
            if start:
                self.kill_switch = False
                self.trading_enabled = True
            return [self._auto_watchlist_dict(symbol) for symbol in self.auto_discovery.managed_symbols]

    async def stop_auto_discovery(self) -> dict[str, Any]:
        async with self._mutation():
            self.auto_discovery.enabled = False
            self.auto_discovery.revision += 1
            self.trading_enabled = False
            return self.auto_discovery.model_dump(mode="json")

    async def auto_discovery_snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return self.auto_discovery.model_dump(mode="json")

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
            "status": (
                "일시 정지" if strategy is not None and strategy.enabled and not self.trading_enabled
                else
                "보유 포지션 정리 중"
                if strategy is not None and strategy.enabled and not strategy.entry_enabled
                else "시세 수집 중"
                if strategy is not None and strategy.enabled and len(self.price_history[symbol]) < strategy.long_window
                else "운영 중" if strategy is not None and strategy.enabled else "대기 중"
            ),
            "managed": symbol in self.auto_discovery.managed_symbols,
            "entry_enabled": strategy.entry_enabled if strategy else True,
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
            "investment_budget": strategy.investment_budget,
            "sizing_mode": "equal_budget" if strategy.investment_budget is not None else "fixed_quantity",
            "max_position": strategy.max_position,
            "enabled": strategy.enabled,
            "entry_enabled": strategy.entry_enabled,
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
