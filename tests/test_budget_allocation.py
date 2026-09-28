from decimal import Decimal
import unittest

from pydantic import ValidationError

from auto_trader.models import AutoDiscoveryRequest, OrderRequest, TickRequest
from auto_trader.paper_engine import EngineError, PaperEngine
from auto_trader.paper_state import dump_engine, restore_engine


class BudgetAllocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0.01"))
        self.request = AutoDiscoveryRequest(keyword="냉각", max_symbols=3, total_investment=1000)
        for symbol, price in (("066570", 100), ("083450", 60), ("053080", 100), ("005930", 100)):
            await self.engine.update_tick(TickRequest(symbol=symbol, price=price, currency="KRW"))

    async def select(self, symbols, *, start=False, request=None):
        return await self.engine.apply_auto_discovery_selection(
            request or self.request, symbols,
            expected_revision=self.engine.auto_discovery.revision, start=start,
        )

    def strategy(self, symbol):
        return self.engine.strategies[self.engine.auto_strategy_ids[symbol]]

    def cross(self, symbol, *, up=True):
        strategy = self.strategy(symbol)
        strategy.short_window, strategy.long_window = 2, 3
        strategy.previous_relation = -1 if up else 1
        self.engine.price_history[symbol].clear()
        self.engine.price_history[symbol].extend(map(Decimal, ("10", "20", "30") if up else ("30", "20", "10")))

    async def test_equal_allocation_matches_actual_fees_ask_and_slippage(self):
        self.engine.slippage_rate = Decimal("0.01")
        await self.engine.update_tick(TickRequest(symbol="066570", price=99, ask=100, currency="KRW"))
        await self.select(["066570", "083450"], start=True)
        preview = await self.engine.auto_allocation_snapshot()
        a, b = preview["items"]
        self.assertEqual(a["budget"], Decimal("500"))
        self.assertEqual(a["estimated_quantity"], 4)
        self.assertEqual(a["estimated_price"], Decimal("101"))
        self.assertEqual(a["estimated_total"], Decimal("408.04"))
        self.assertEqual(b["estimated_quantity"], 8)
        self.assertEqual(b["estimated_total"], Decimal("489.648"))
        self.cross("066570")
        self.cross("083450")
        await self.engine.run_strategies()
        self.assertEqual(Decimal("10000") - self.engine.cash, a["estimated_total"] + b["estimated_total"])
        self.assertLessEqual((await self.engine.auto_allocation_snapshot())["committed"], Decimal("1000"))

    async def test_price_is_recalculated_at_buy_signal_and_sell_exits_entire_position(self):
        await self.select(["066570", "083450"], start=True)
        await self.engine.update_tick(TickRequest(symbol="066570", price=200, currency="KRW"))
        self.cross("066570")
        await self.engine.run_strategies()
        self.assertEqual(self.engine.positions["066570"].quantity, 2)
        self.assertEqual(self.engine.cash, Decimal("9596"))
        self.cross("066570", up=False)
        await self.engine.run_strategies()
        self.assertEqual(self.engine.positions["066570"].quantity, 0)
        self.assertEqual(list(self.engine.orders.values())[-1].quantity, 2)

    async def test_retiring_holdings_reduce_new_allocations_without_duplicate_budget(self):
        await self.select(["066570", "083450"], start=True)
        await self.engine.place_order(OrderRequest(symbol="066570", side="BUY", quantity=4))
        await self.engine.place_order(OrderRequest(symbol="083450", side="BUY", quantity=6))
        await self.select(["053080", "005930"])
        preview = await self.engine.auto_allocation_snapshot()
        self.assertEqual(preview["retiring_committed"], Decimal("767.6"))
        self.assertEqual([item["budget"] for item in preview["items"]], [Decimal("116.2")] * 2)
        self.cross("053080")
        self.cross("005930")
        await self.engine.run_strategies()
        self.assertEqual((await self.engine.auto_allocation_snapshot())["committed"], Decimal("969.6"))
        await self.select(["053080", "005930"])
        self.assertEqual([item["estimated_quantity"] for item in (await self.engine.auto_allocation_snapshot())["items"]], [0, 0])

    async def test_oversized_budget_is_rejected_without_changing_account(self):
        before = dump_engine(self.engine)
        with self.assertRaises(EngineError) as error:
            await self.select(["066570"], start=True, request=self.request.model_copy(update={"total_investment": Decimal("10001")}))
        self.assertEqual(error.exception.code, "insufficient_investment_cash")
        self.assertEqual(dump_engine(self.engine), before)

    async def test_unaffordable_share_remains_cash_and_no_order_is_created(self):
        request = self.request.model_copy(update={"total_investment": Decimal("100")})
        await self.select(["066570", "083450"], start=True, request=request)
        self.cross("066570")
        self.cross("083450")
        results = await self.engine.run_strategies()
        self.assertTrue(all(item["reason"] == "investment_budget_exhausted" for item in results))
        self.assertEqual(self.engine.orders, {})
        self.assertEqual(self.engine.cash, Decimal("10000"))

    async def test_existing_position_above_new_weight_still_respects_total_cap(self):
        await self.select(["066570"], start=True)
        await self.engine.place_order(OrderRequest(symbol="066570", side="BUY", quantity=8))
        await self.select(["066570", "083450"])
        # First holding exceeds its new 500 allocation; it must not be force-sold.
        self.assertEqual(self.engine.positions["066570"].quantity, 8)
        self.cross("066570")
        self.cross("083450")
        await self.engine.run_strategies()
        self.assertEqual(self.engine.positions["083450"].quantity, 3)
        self.assertEqual((await self.engine.auto_allocation_snapshot())["committed"], Decimal("989.8"))

    async def test_pending_order_and_reduced_cash_are_included_in_sizing(self):
        await self.select(["066570", "083450"], start=True)
        await self.engine.place_order(OrderRequest(symbol="066570", side="BUY", quantity=4, order_type="LIMIT", price=90))
        preview = await self.engine.auto_allocation_snapshot()
        self.assertEqual(preview["committed"], Decimal("363.6"))
        self.assertEqual(preview["items"][0]["estimated_quantity"], 1)
        # An unrelated order also reduces the account's available cash.
        await self.engine.place_order(OrderRequest(symbol="053080", side="BUY", quantity=97, order_type="LIMIT", price=98))
        preview = await self.engine.auto_allocation_snapshot()
        self.assertTrue(all(item["estimated_total"] <= self.engine.available_cash for item in preview["items"]))
        self.assertLessEqual(sum(item["estimated_total"] for item in preview["items"]), self.engine.available_cash)

    async def test_resume_and_restore_preserve_budget_and_existing_cash(self):
        await self.select(["066570", "083450"], start=True)
        self.cross("066570")
        await self.engine.run_strategies()
        await self.engine.pause()
        await self.engine.activate_auto_strategies()
        await self.engine.resume()
        self.assertEqual(self.strategy("066570").investment_budget, Decimal("500"))
        self.assertIsNone(self.strategy("066570").max_position)
        saved = dump_engine(self.engine)
        restored = PaperEngine()
        restore_engine(restored, saved)
        self.assertEqual(await restored.auto_allocation_snapshot(), await self.engine.auto_allocation_snapshot())
        self.assertEqual(dump_engine(restored), saved)

    async def test_domestic_budget_never_counts_us_holdings_as_won(self):
        with self.assertRaises(ValidationError):
            AutoDiscoveryRequest(keyword="AI", market="US", total_investment=1000)
        await self.engine.update_tick(TickRequest(symbol="AAPL", price=100, currency="USD"))
        before = dump_engine(self.engine)
        with self.assertRaises(EngineError) as error:
            await self.select(["AAPL"], start=True)
        self.assertEqual(error.exception.code, "budget_currency_mismatch")
        self.assertEqual(dump_engine(self.engine), before)


if __name__ == "__main__":
    unittest.main()
