from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from auto_trader.models import AutoStrategySettings, AutoStrategySymbolRequest, OrderRequest, OrderSide, OrderStatus, TickRequest, StrategyRequest
from auto_trader.paper_engine import EngineError, PaperEngine


class PaperEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_snapshot_works_without_positions(self) -> None:
        engine = PaperEngine(initial_cash=Decimal("1000000"))

        account = await engine.account()

        self.assertEqual(account["market_value"], Decimal("0E-8"))
        self.assertEqual(account["equity"], Decimal("1000000.00000000"))
        self.assertEqual(account["realized_pnl"], Decimal("0"))
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("0"))
        self.assertEqual(account["realized_fees"], Decimal("0"))

    async def test_tick_snapshot_includes_display_name(self) -> None:
        await self.engine.update_tick(TickRequest(symbol="005930", price=Decimal("100")))

        ticks = await self.engine.ticks_snapshot()

        self.assertEqual(ticks[0]["symbol"], "005930")
        self.assertEqual(ticks[0]["name"], "삼성전자")

    async def asyncSetUp(self) -> None:
        self.engine = PaperEngine(
            initial_cash=Decimal("10000"),
            fee_rate=Decimal("0"),
        )

    async def test_market_buy_and_sell_update_cash_and_position(self) -> None:
        await self.engine.update_tick(
            TickRequest(symbol="TEST", price=Decimal("100"), bid=Decimal("99"), ask=Decimal("101"))
        )
        buy = await self.engine.place_order(
            OrderRequest(symbol="TEST", side=OrderSide.BUY, quantity=Decimal("2"))
        )
        self.assertEqual(buy["status"], OrderStatus.FILLED)
        self.assertEqual(buy["average_filled_price"], Decimal("101"))

        await self.engine.update_tick(
            TickRequest(symbol="TEST", price=Decimal("110"), bid=Decimal("109"), ask=Decimal("111"))
        )
        sell = await self.engine.place_order(
            OrderRequest(symbol="TEST", side=OrderSide.SELL, quantity=Decimal("1"))
        )
        self.assertEqual(sell["status"], OrderStatus.FILLED)

        positions = await self.engine.positions_snapshot()
        self.assertEqual(positions[0]["quantity"], Decimal("1"))
        account = await self.engine.account()
        self.assertEqual(account["realized_pnl"], Decimal("8.00000000"))

    async def test_realized_pnl_allocates_buy_fees_on_partial_sales(self) -> None:
        engine = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0.01"))

        async def trade(side, quantity, price):
            await engine.update_tick(TickRequest(symbol="TEST", price=Decimal(price)))
            return await engine.place_order(OrderRequest(symbol="TEST", side=side, quantity=Decimal(quantity)))

        await trade(OrderSide.BUY, "10", "100")  # purchase fee 10, not yet realized
        account = await engine.account()
        self.assertEqual(account["realized_pnl"], Decimal("0"))
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("0"))

        await trade(OrderSide.SELL, "4", "120")  # gross 80, allocated buy fee 4, sell fee 4.8
        account = await engine.account()
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("80"))
        self.assertEqual(account["realized_pnl"], Decimal("71.2"))
        self.assertEqual(account["realized_fees"], Decimal("8.8"))
        self.assertEqual(engine.positions["TEST"].remaining_buy_fees, Decimal("6"))

        await trade(OrderSide.BUY, "4", "150")  # average 120, open buy fees 12
        await trade(OrderSide.SELL, "10", "110")  # gross -100, fees 12 + 11
        account = await engine.account()
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("-20"))
        self.assertEqual(account["realized_pnl"], Decimal("-51.8"))
        self.assertEqual(account["realized_fees"], Decimal("31.8"))
        self.assertEqual(account["equity"] - account["initial_cash"], account["realized_pnl"])
        self.assertEqual(engine.positions["TEST"].remaining_buy_fees, Decimal("0"))
        self.assertEqual(engine.positions["TEST"].realized_pnl, account["realized_pnl"])

        await trade(OrderSide.BUY, "1", "100")
        await trade(OrderSide.SELL, "1", "100")
        account = await engine.account()
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("-20"))
        self.assertEqual(account["realized_pnl"], Decimal("-53.8"))

    async def test_duplicate_and_canceled_orders_do_not_double_count_fees(self) -> None:
        engine = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0.00015"))
        await engine.update_tick(TickRequest(symbol="TEST", price=Decimal("100")))
        buy = OrderRequest(symbol="TEST", side=OrderSide.BUY, quantity=Decimal("3"), client_order_id="fee-once")
        await engine.place_order(buy)
        await engine.place_order(buy)
        pending = await engine.place_order(OrderRequest(symbol="TEST", side=OrderSide.BUY,
            quantity=Decimal("1"), order_type="LIMIT", price=Decimal("90")))
        await engine.cancel_order(pending["order_id"])
        await engine.update_tick(TickRequest(symbol="TEST", price=Decimal("110")))
        for quantity in ("1", "2"):
            await engine.place_order(OrderRequest(symbol="TEST", side=OrderSide.SELL, quantity=Decimal(quantity)))
        account = await engine.account()
        self.assertEqual(account["realized_pnl_before_fees"], Decimal("30"))
        self.assertEqual(account["realized_pnl"], Decimal("29.9055"))
        self.assertEqual(account["realized_fees"], Decimal("0.0945"))
        self.assertEqual(engine.positions["TEST"].remaining_buy_fees, Decimal("0"))

    async def test_limit_order_fills_on_later_tick_and_is_idempotent(self) -> None:
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("100")))
        request = OrderRequest(
            symbol="TEST",
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type="LIMIT",
            price=Decimal("95"),
            client_order_id="one-order",
        )
        pending = await self.engine.place_order(request)
        duplicate = await self.engine.place_order(request)
        self.assertEqual(pending["order_id"], duplicate["order_id"])
        self.assertEqual(pending["status"], OrderStatus.PENDING)

        result = await self.engine.update_tick(
            TickRequest(symbol="TEST", price=Decimal("94"), ask=Decimal("94"))
        )
        self.assertEqual(result["filled_orders"][0]["status"], OrderStatus.FILLED)

    async def test_sell_requires_position_and_pause_blocks_orders(self) -> None:
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("100")))
        with self.assertRaises(EngineError) as error:
            await self.engine.place_order(
                OrderRequest(symbol="TEST", side=OrderSide.SELL, quantity=Decimal("1"))
            )
        self.assertEqual(error.exception.code, "insufficient_quantity")

        await self.engine.pause()
        with self.assertRaises(EngineError) as error:
            await self.engine.place_order(
                OrderRequest(symbol="TEST", side=OrderSide.BUY, quantity=Decimal("1"))
            )
        self.assertEqual(error.exception.code, "trading_paused")

    async def test_moving_average_strategy_generates_paper_orders(self) -> None:
        strategy = await self.engine.create_strategy(
            StrategyRequest(
                name="cross",
                symbol="TEST",
                short_window=2,
                long_window=3,
                order_quantity=Decimal("1"),
            )
        )
        prices = ["10", "9", "8", "9", "11"]
        results = []
        for price in prices:
            await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal(price)))
            results.extend(await self.engine.run_strategies())

        buy_results = [result for result in results if result["action"] == "BUY"]
        self.assertEqual(len(buy_results), 1)
        self.assertIsNotNone(buy_results[0]["order"])
        self.assertEqual(strategy["symbol"], "TEST")

    async def test_staged_auto_symbol_gets_strategy_only_when_activated(self) -> None:
        for price in ("10", "9", "8"):
            await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal(price)))

        queued = await self.engine.add_auto_symbol(AutoStrategySymbolRequest(symbol="test"))
        self.assertEqual(queued["status"], "대기 중")
        self.assertEqual(await self.engine.list_strategies(), [])

        activated = await self.engine.activate_auto_strategies({
            "TEST": AutoStrategySettings(short_window=2, long_window=3, order_quantity=Decimal("1"))
        })
        self.assertEqual(activated[0]["status"], "운영 중")
        self.assertEqual(activated[0]["short_window"], 2)
        self.assertEqual(activated[0]["long_window"], 3)
        await self.engine.run_strategies()  # establish the initial moving-average relation
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("9")))
        await self.engine.run_strategies()
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("11")))
        result = await self.engine.run_strategies()

        self.assertEqual(result[0]["action"], "BUY")
        self.assertEqual(result[0]["order"]["quantity"], Decimal("1"))

    async def test_auto_symbol_settings_are_independent_on_resume(self) -> None:
        for symbol in ("AAA", "BBB"):
            await self.engine.update_tick(TickRequest(symbol=symbol, price=Decimal("100")))
            await self.engine.add_auto_symbol(AutoStrategySymbolRequest(symbol=symbol))

        first = await self.engine.activate_auto_strategies({
            "AAA": AutoStrategySettings(short_window=2, long_window=4, order_quantity=Decimal("2")),
            "BBB": AutoStrategySettings(short_window=3, long_window=9, order_quantity=Decimal("5")),
        })
        self.assertEqual([(item["short_window"], item["long_window"], item["order_quantity"]) for item in first], [
            (2, 4, Decimal("2")), (3, 9, Decimal("5")),
        ])

        second = await self.engine.activate_auto_strategies({
            "AAA": AutoStrategySettings(short_window=2, long_window=5, order_quantity=Decimal("3")),
        })
        self.assertEqual([(item["short_window"], item["long_window"], item["order_quantity"]) for item in second], [
            (2, 5, Decimal("3")), (3, 9, Decimal("5")),
        ])

    async def test_chart_snapshot_returns_one_minute_close_bars(self) -> None:
        start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("100"), timestamp=start))
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("103"), timestamp=start + timedelta(seconds=30)))
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("102"), timestamp=start + timedelta(minutes=2)))
        await self.engine.update_tick(TickRequest(symbol="TEST", price=Decimal("98"), timestamp=start + timedelta(minutes=5)))

        snapshot = await self.engine.chart_snapshot("TEST")

        self.assertEqual(snapshot["interval"], "1m")
        self.assertEqual(len(snapshot["items"]), 3)
        self.assertEqual(snapshot["items"][0]["open"], Decimal("100"))
        self.assertEqual(snapshot["items"][0]["high"], Decimal("103"))
        self.assertEqual(snapshot["items"][0]["close"], Decimal("103"))
        self.assertEqual(snapshot["items"][1]["close"], Decimal("102"))
        self.assertEqual(snapshot["items"][2]["close"], Decimal("98"))


if __name__ == "__main__":
    unittest.main()
