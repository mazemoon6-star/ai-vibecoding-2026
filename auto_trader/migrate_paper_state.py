"""Import a paused legacy API export into SQLite without submitting any orders.

Usage: python -m auto_trader.migrate_paper_state BACKUP.json --output data/paper_state.sqlite3
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .config import load_local_env
from .models import AutoStrategySettings, OrderRequest, OrderSide, OrderStatus, utc_now
from .paper_engine import PaperEngine, PaperOrder, Position, Strategy, Tick
from .paper_state import PaperStateStore, dump_engine, restore_engine, restore_record


def migrate_snapshot(snapshot, *, fee_rate=Decimal("0.00015"), slippage_rate=Decimal("0")):
    account = snapshot["account"]
    orders = snapshot["orders"]["items"]
    if account["mode"] != "PAPER" or account["trading_enabled"]:
        raise ValueError("Export a paused PAPER account before migration")
    if any(order["status"] == "PENDING" for order in orders):
        raise ValueError("Legacy pending orders cannot be migrated without their private reservations")
    if Decimal(account["reserved_cash"]) != 0:
        raise ValueError("Unexpected legacy cash reservation")
    engine = PaperEngine(initial_cash=Decimal(account["initial_cash"]), fee_rate=fee_rate, slippage_rate=slippage_rate)
    converted = {}
    for item in orders:
        request = OrderRequest(symbol=item["symbol"], side=item["side"], quantity=item["quantity"],
            order_type=item["order_type"], price=item["price"], strategy_id=item["strategy_id"], client_order_id=item["client_order_id"])
        raw = dict(item, request_fingerprint=engine._fingerprint(request))
        order = restore_record(PaperOrder, raw)
        if order.order_id in converted or order.client_order_id in engine.client_orders:
            raise ValueError("Duplicate legacy order identity")
        if order.status is OrderStatus.FILLED and (order.filled_quantity != order.quantity or order.average_filled_price is None or order.filled_at is None):
            raise ValueError("Unsupported legacy partial fill or missing execution data")
        if order.fee < 0:
            raise ValueError("Invalid execution fee")
        converted[order.order_id] = order
        engine.client_orders[order.client_order_id] = order.order_id

    # Replay into a detached engine, using actual recorded fees even if the rate changed.
    filled = sorted((o for o in converted.values() if o.status is OrderStatus.FILLED), key=lambda o: (o.filled_at, o.created_at))
    for order in filled:
        replay = restore_record(PaperOrder, next(dict(item, request_fingerprint=order.request_fingerprint) for item in orders if item["order_id"] == order.order_id))
        engine._fill_order(replay, order.average_filled_price, recorded_fee=order.fee)
        if replay.status is not OrderStatus.FILLED:
            raise ValueError("Execution history cannot reconstruct the legacy account")
    engine.orders = converted
    if engine.cash != Decimal(account["cash"]):
        raise ValueError("Replayed cash differs from live cash; retain the old server")
    old_positions = {item["symbol"]: item for item in snapshot["positions"]["items"]}
    for symbol in set(old_positions) | set(engine.positions):
        old = old_positions.get(symbol)
        position = engine.positions.get(symbol, Position(symbol))
        if old is None or position.quantity != Decimal(old["quantity"]) or position.average_price != Decimal(old["average_price"]) or Decimal(old["reserved_quantity"]) != 0:
            raise ValueError(f"Position mismatch: {symbol}; retain the old server")
    sell_fees = sum((o.fee for o in filled if o.side is OrderSide.SELL), Decimal("0"))
    if engine.realized_pnl_before_fees - sell_fees != Decimal(account["realized_pnl"]):
        raise ValueError("Legacy realized PNL does not reconcile to recorded sells")

    for item in snapshot["ticks"]["items"]:
        tick = restore_record(Tick, item)
        engine.ticks[tick.symbol] = tick
        engine.tick_history[tick.symbol].append(tick)
    # The old API exposes only aggregated minute bars, not raw MA samples.
    # Start sample warm-up afresh instead of treating candles as original ticks.
    for item in snapshot["strategies"]["items"]:
        strategy = restore_record(Strategy, dict(item, previous_relation=None))
        engine.strategies[strategy.strategy_id] = strategy
        if strategy.name == f"auto-{strategy.symbol}" and strategy.strategy_id.startswith("auto-"):
            engine.auto_strategy_ids[strategy.symbol] = strategy.strategy_id
    for item in snapshot["auto_symbols"]["items"]:
        symbol = item["symbol"]
        engine.auto_watchlist[symbol] = datetime.fromisoformat(item["added_at"])
        if item["strategy_id"]:
            if item["strategy_id"] not in engine.strategies:
                raise ValueError("Missing watchlist strategy")
            engine.auto_strategy_ids[symbol] = item["strategy_id"]
        engine.auto_strategy_settings[symbol] = AutoStrategySettings(
            short_window=item.get("short_window") or 3,
            long_window=item.get("long_window") or 8,
            order_quantity=item.get("order_quantity") or "1",
        )
    for symbol, old in old_positions.items():
        if symbol not in engine.ticks and old["market_price"] is not None:
            engine.ticks[symbol] = Tick(symbol=symbol, name=symbol, price=Decimal(old["market_price"]),
                bid=None, ask=None, volume=None, timestamp=datetime.fromisoformat(account["as_of"]))
    for key in ("ticks_received", "orders_created", "orders_filled", "orders_rejected", "orders_canceled"):
        engine.metrics[key] = snapshot["metrics"][key]
    engine.trading_enabled = account["trading_enabled"]
    engine.kill_switch = account["kill_switch"]
    engine.created_at = min((o.created_at for o in converted.values()), default=utc_now())
    state = dump_engine(engine)
    restore_engine(PaperEngine(), state)  # Validate before writing anything.
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Destination already exists; refusing to overwrite a PAPER account")
    load_local_env()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    state = migrate_snapshot(snapshot, fee_rate=Decimal(os.getenv("PAPER_FEE_RATE", "0.00015")),
        slippage_rate=Decimal(os.getenv("PAPER_SLIPPAGE_RATE", "0")))
    store = PaperStateStore(args.output)
    try:
        if store.load() is not None:
            raise ValueError("Destination already contains PAPER state")
        store.save(state)
    finally:
        store.close()
    print(json.dumps({"saved_to": str(args.output), "cash": state["cash"], "orders": len(state["orders"]),
        "positions": len(state["positions"]), "realized_pnl": state["realized_pnl"],
        "realized_pnl_before_fees": state["realized_pnl_before_fees"]}))


if __name__ == "__main__":
    main()
