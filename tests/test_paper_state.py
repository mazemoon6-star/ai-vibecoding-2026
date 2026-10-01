import json
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

from auto_trader import app as dashboard
from auto_trader.migrate_paper_state import migrate_snapshot
from auto_trader.models import AutoDiscoveryRequest, AutoStrategySettings, AutoStrategySymbolRequest, OrderRequest, OrderSide, TickRequest
from auto_trader.paper_engine import EngineError, PaperEngine
from auto_trader.paper_state import StateStoreError, dump_engine, json_value, restore_engine


class PaperStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "paper.sqlite3"

    def persistent_engine(self):
        engine = PaperEngine()
        engine.enable_persistence(self.path)
        self.addCleanup(engine.close_persistence)
        return engine

    async def test_restart_preserves_every_field_and_pending_order_idempotency(self):
        engine = self.persistent_engine()
        for price in (100, 102, 101, 103):
            await engine.update_tick(TickRequest(symbol="TEST", price=price))
        await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=4))
        await engine.add_auto_symbol(AutoStrategySymbolRequest(symbol="TEST"))
        await engine.activate_auto_strategies({"TEST": AutoStrategySettings(short_window=2, long_window=4, order_quantity=3)})
        await engine.run_strategies()
        request = OrderRequest(symbol="TEST", side="BUY", quantity=2, order_type="LIMIT", price=90, client_order_id="persist-once")
        pending = await engine.place_order(request)
        await engine.place_order(OrderRequest(symbol="TEST", side="SELL", quantity=1, order_type="LIMIT", price=120))
        await engine.pause()
        expected = dump_engine(engine)
        engine.close_persistence()

        restored = self.persistent_engine()
        self.assertEqual(dump_engine(restored), expected)
        self.assertFalse(restored.trading_enabled)
        self.assertEqual(len(restored.price_history["TEST"]), 4)
        await restored.resume()
        duplicate = await restored.place_order(request)
        self.assertEqual(duplicate["order_id"], pending["order_id"])
        self.assertEqual(len(restored.orders), len(engine.orders))
        await restored.cancel_order(pending["order_id"])
        self.assertEqual(restored.reserved_cash, Decimal("0"))
        await restored.update_tick(TickRequest(symbol="TEST", price=120))
        self.assertEqual(restored.positions["TEST"].quantity, Decimal("3"))
        expected_pnl = restored.realized_pnl
        restored.close_persistence()
        again = self.persistent_engine()
        self.assertEqual(again.realized_pnl, expected_pnl)
        self.assertGreater(again.realized_pnl_before_fees, expected_pnl)
        self.assertEqual(again.reserved_sell_quantity["TEST"], Decimal("0"))

    async def test_filled_buy_and_sell_are_recorded_once_and_pending_is_excluded(self):
        engine = self.persistent_engine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        bought = await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2))
        sold = await engine.place_order(OrderRequest(symbol="TEST", side="SELL", quantity=1))
        await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=1, order_type="LIMIT", price=50))
        history = await engine.trade_history_snapshot()
        self.assertEqual(history["total"], 2)
        self.assertEqual({item["order_id"] for item in history["items"]}, {bought["order_id"], sold["order_id"]})
        buys = await engine.trade_history_snapshot(side=OrderSide.BUY)
        self.assertEqual(buys["total"], 1)
        self.assertEqual(buys["items"][0]["gross_amount"], "200.00000000")
        self.assertEqual(buys["items"][0]["quantity"], "2")
        self.assertEqual(buys["items"][0]["average_filled_price"], "100.00000000")
        self.assertEqual(buys["items"][0]["trade_date_kst"],
                         bought["filled_at"].astimezone(timezone(timedelta(hours=9))).date().isoformat())
        self.assertEqual((await engine.trade_history_snapshot(limit=1))["total"], 2)
        self.assertEqual(len((await engine.trade_history_snapshot(limit=1))["items"]), 1)
        engine.close_persistence()
        restored = self.persistent_engine()
        self.assertEqual((await restored.trade_history_snapshot())["total"], 2)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0], 2)

    async def test_history_uses_korean_calendar_day_across_utc_midnight(self):
        engine = PaperEngine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        first = await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2))
        second = await engine.place_order(OrderRequest(symbol="TEST", side="SELL", quantity=1))
        engine.orders[first["order_id"]].filled_at = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
        engine.orders[second["order_id"]].filled_at = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
        engine.enable_persistence(self.path)
        self.addCleanup(engine.close_persistence)
        first_day = await engine.trade_history_snapshot(date(2026, 10, 1))
        second_day = await engine.trade_history_snapshot(date(2026, 10, 2))
        self.assertEqual([item["order_id"] for item in first_day["items"]], [first["order_id"]])
        self.assertEqual([item["order_id"] for item in second_day["items"]], [second["order_id"]])

    async def test_existing_single_row_database_backfills_filled_orders(self):
        old = PaperEngine()
        await old.update_tick(TickRequest(symbol="TEST", price=100))
        bought = await old.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=1))
        state = dump_engine(old)
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE paper_state (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL, saved_at TEXT NOT NULL)")
            connection.execute("INSERT INTO paper_state VALUES (1, 1, ?, ?, ?)", (
                payload, hashlib.sha256(payload.encode("utf-8")).hexdigest(), datetime.now(timezone.utc).isoformat(),
            ))
            connection.commit()
        restored = self.persistent_engine()
        history = await restored.trade_history_snapshot()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["order_id"], bought["order_id"])
        restored.close_persistence()
        self.assertEqual((await self.persistent_engine().trade_history_snapshot())["total"], 1)

    async def test_history_insert_failure_rolls_back_account_and_snapshot(self):
        engine = self.persistent_engine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        before = dump_engine(engine)
        with patch.object(engine.state_store, "_insert_trade_rows", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(EngineError) as error:
                await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2))
        self.assertEqual(error.exception.code, "state_storage_error")
        self.assertEqual(dump_engine(engine), before)
        self.assertEqual((await engine.trade_history_snapshot())["total"], 0)
        engine.close_persistence()
        self.assertEqual(dump_engine(self.persistent_engine()), before)

    async def test_history_http_filters_filled_buys_by_korean_date(self):
        engine = self.persistent_engine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        bought = await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=1))
        day = bought["filled_at"].astimezone(timezone(timedelta(hours=9))).date().isoformat()
        with patch.object(dashboard, "engine", engine):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app), base_url="http://test") as client:
                response = await client.get("/api/v1/trade-history", params={"date": day, "side": "BUY"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["total"], 1)
                self.assertEqual(response.json()["items"][0]["order_id"], bought["order_id"])
                self.assertEqual(response.json()["timezone"], "Asia/Seoul")
                invalid = await client.get("/api/v1/trade-history", params={"date": "not-a-date"})
                self.assertEqual(invalid.status_code, 422)

    async def test_failed_commit_rolls_back_cash_position_and_order(self):
        engine = self.persistent_engine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        before = dump_engine(engine)
        with patch.object(engine.state_store, "save", side_effect=StateStoreError("disk full")):
            with self.assertRaises(EngineError) as error:
                await engine.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2))
        self.assertEqual(error.exception.code, "state_storage_error")
        self.assertEqual(dump_engine(engine), before)
        engine.close_persistence()
        self.assertEqual(dump_engine(self.persistent_engine()), before)

    async def test_starting_cash_updates_untraded_saved_account_but_never_resets_trades(self):
        original = PaperEngine(initial_cash=Decimal("1000000"))
        original.enable_persistence(self.path)
        original.close_persistence()
        engine = self.persistent_engine()
        await engine.configure_untraded_initial_cash(Decimal("10000000"))
        self.assertEqual((await engine.account())["available_cash"], Decimal("10000000"))
        engine.close_persistence()
        restored = self.persistent_engine()
        self.assertEqual(restored.initial_cash, Decimal("10000000"))
        self.assertEqual(restored.cash, Decimal("10000000"))
        await restored.update_tick(TickRequest(symbol="TEST", price=100))
        await restored.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2))
        before = dump_engine(restored)
        await restored.configure_untraded_initial_cash(Decimal("20000000"))
        self.assertEqual(dump_engine(restored), before)
        restored.close_persistence()
        self.assertEqual(dump_engine(self.persistent_engine()), before)

    async def test_corrupt_state_is_not_replaced_by_a_new_account(self):
        engine = self.persistent_engine()
        await engine.update_tick(TickRequest(symbol="TEST", price=100))
        engine.close_persistence()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE paper_state SET payload='{}' WHERE id=1")
            connection.commit()
        with self.assertRaises(StateStoreError):
            PaperEngine().enable_persistence(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT payload FROM paper_state").fetchone()[0], "{}")

    async def test_discovery_selection_commit_failure_restores_watchlist_and_entry_flags(self):
        engine = self.persistent_engine()
        for symbol in ("OLD", "NEW"):
            await engine.update_tick(TickRequest(symbol=symbol, price=100))
        request = AutoDiscoveryRequest(keyword="냉각", max_symbols=1, order_quantity=2)
        await engine.apply_auto_discovery_selection(request, ["OLD"], expected_revision=0, start=True)
        await engine.place_order(OrderRequest(symbol="OLD", side="BUY", quantity=2))
        before = dump_engine(engine)
        with patch.object(engine.state_store, "save", side_effect=StateStoreError("disk full")):
            with self.assertRaises(EngineError):
                await engine.apply_auto_discovery_selection(
                    request, ["NEW"], expected_revision=engine.auto_discovery.revision
                )
        self.assertEqual(dump_engine(engine), before)
        engine.close_persistence()
        self.assertEqual(dump_engine(self.persistent_engine()), before)

    async def test_second_server_cannot_overwrite_the_same_account(self):
        self.persistent_engine()
        with self.assertRaises(StateStoreError):
            PaperEngine().enable_persistence(self.path)

    async def test_legacy_migration_reconciles_recorded_fees_and_preserves_ids(self):
        old = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0.01"))
        await old.update_tick(TickRequest(symbol="TEST", price=100))
        await old.place_order(OrderRequest(symbol="TEST", side="BUY", quantity=2, client_order_id="original-buy"))
        await old.add_auto_symbol(AutoStrategySymbolRequest(symbol="TEST"))
        await old.activate_auto_strategies({"TEST": AutoStrategySettings(short_window=2, long_window=5, order_quantity=2)})
        await old.update_tick(TickRequest(symbol="TEST", price=110))
        await old.place_order(OrderRequest(symbol="TEST", side="SELL", quantity=1))
        await old.pause()
        snapshot = {
            "account": await old.account(), "orders": {"items": await old.orders_snapshot()},
            "positions": {"items": await old.positions_snapshot()}, "ticks": {"items": await old.ticks_snapshot()},
            "strategies": {"items": await old.list_strategies()}, "auto_symbols": {"items": await old.auto_watchlist_snapshot()},
            "metrics": await old.metrics_snapshot(),
        }
        snapshot["account"]["realized_pnl"] = Decimal("8.9")  # legacy subtracted only sell fee
        snapshot = json.loads(json.dumps(snapshot, default=json_value))
        state = migrate_snapshot(snapshot, fee_rate=Decimal("0.00015"))  # current rate differs from actual fees
        new = PaperEngine()
        restore_engine(new, state)
        self.assertEqual(new.cash, old.cash)
        self.assertEqual(new.realized_pnl_before_fees, Decimal("10"))
        self.assertEqual(new.realized_pnl, Decimal("7.9"))
        self.assertEqual(new.positions["TEST"].remaining_buy_fees, Decimal("1"))
        self.assertEqual(new.positions["TEST"].quantity, Decimal("1"))
        self.assertEqual(list(new.orders), list(old.orders))
        self.assertEqual(new.client_orders, old.client_orders)
        self.assertEqual(new.auto_strategy_settings["TEST"].long_window, 5)
        self.assertFalse(new.trading_enabled)
        self.assertEqual(len(new.price_history["TEST"]), 0)
        new.enable_persistence(self.path)
        new.close_persistence()
        restored = self.persistent_engine()
        self.assertEqual(restored.realized_pnl, Decimal("7.9"))
        snapshot["account"]["cash"] = "9999"
        with self.assertRaisesRegex(ValueError, "cash"):
            migrate_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
