from decimal import Decimal
import unittest

from auto_trader.models import OrderRequest, OrderSide, OrderStatus, TickRequest, StrategyRequest
from auto_trader.paper_engine import EngineError, PaperEngine


class PaperEngineTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
