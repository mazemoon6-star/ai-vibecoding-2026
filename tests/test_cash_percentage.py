from decimal import Decimal
import unittest

from pydantic import ValidationError

from auto_trader.models import AutoDiscoveryRequest, OrderRequest, TickRequest
from auto_trader.paper_engine import EngineError, PaperEngine
from auto_trader.paper_state import dump_engine, restore_engine


class CashPercentageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0.01"))
        self.request = AutoDiscoveryRequest(keyword="의약", cash_percentage=30, max_symbols=1)
        for symbol in ("207940", "068270", "005930"):
            await self.engine.update_tick(TickRequest(symbol=symbol, price=100, currency="KRW"))
        self.weights = {"207940": Decimal("60"), "068270": Decimal("40")}

    async def select(self, *, start=False, weights=None, request=None):
        return await self.engine.apply_auto_discovery_selection(
            request or self.request, ["207940", "068270"], start=start,
            expected_revision=self.engine.auto_discovery.revision,
            allocation_weights=self.weights if weights is None else weights,
            planner_model="gpt-test", planner_reason="섹터 후보 분산")

    async def buy_signals(self):
        for strategy in self.engine.strategies.values():
            if not strategy.entry_enabled:
                continue
            strategy.short_window, strategy.long_window, strategy.previous_relation = 2, 3, -1
            self.engine.price_history[strategy.symbol].clear()
            self.engine.price_history[strategy.symbol].extend(map(Decimal, ("10", "20", "30")))
        return await self.engine.run_strategies()

    def test_percentage_accepts_only_ten_percent_steps_and_kr(self):
        for percentage in (0, 5, 15, 101, 30.5):
            with self.subTest(percentage=percentage), self.assertRaises(ValidationError):
                AutoDiscoveryRequest(keyword="의약", cash_percentage=percentage)
        with self.assertRaises(ValidationError):
            AutoDiscoveryRequest(keyword="의약", market="US", cash_percentage=30)

    async def test_server_uses_available_cash_weights_and_fees_ignoring_legacy_count(self):
        await self.select(start=True)
        config = await self.engine.auto_discovery_snapshot()
        self.assertEqual(Decimal(config["total_investment"]), Decimal("3000"))
        self.assertEqual(Decimal(config["cash_base"]), Decimal("10000"))
        preview = await self.engine.auto_allocation_snapshot()
        self.assertEqual([item["budget"] for item in preview["items"]], [Decimal("1800"), Decimal("1200")])
        await self.buy_signals()
        self.assertEqual(self.engine.positions["207940"].quantity, 17)
        self.assertEqual(self.engine.positions["068270"].quantity, 11)
        self.assertEqual(len(self.engine.orders), 2)
        self.assertLessEqual(Decimal("10000") - self.engine.cash, Decimal("3000"))

    async def test_periodic_refresh_and_restore_do_not_reapply_percentage_to_cash(self):
        await self.select(start=True)
        await self.buy_signals()
        await self.select()
        self.assertEqual(self.engine.auto_discovery.total_investment, Decimal("3000"))
        self.assertEqual(self.engine.auto_discovery.cash_base, Decimal("10000"))
        self.assertLessEqual(sum(item["estimated_total"] for item in
            (await self.engine.auto_allocation_snapshot())["items"]), Decimal("3000") - Decimal("2828"))
        saved = dump_engine(self.engine)
        restored = PaperEngine()
        restore_engine(restored, saved)
        self.assertEqual(dump_engine(restored), saved)

    async def test_reserved_cash_is_excluded_and_client_amount_cannot_override_ratio(self):
        await self.engine.place_order(OrderRequest(symbol="005930", side="BUY", quantity=10,
            order_type="LIMIT", price=90))
        before_cash = self.engine.available_cash
        await self.select(start=True, request=self.request.model_copy(update={"total_investment": Decimal("99999999")}))
        self.assertEqual(self.engine.auto_discovery.cash_base, before_cash)
        self.assertEqual(self.engine.auto_discovery.total_investment,
            (before_cash * Decimal("0.30")).quantize(Decimal("0.01")))

    async def test_existing_positions_are_preserved_and_new_cash_is_allocated_separately(self):
        await self.engine.place_order(OrderRequest(symbol="207940", side="BUY", quantity=2))
        existing = Decimal("202")
        fresh_budget = self.engine.available_cash * Decimal("0.30")
        await self.select(start=True)
        preview = await self.engine.auto_allocation_snapshot()
        self.assertEqual(preview["total_investment"], existing + fresh_budget)
        self.assertEqual(preview["items"][0]["budget"], existing + fresh_budget * Decimal("0.60"))
        self.assertEqual(self.engine.positions["207940"].quantity, 2)

    async def test_invalid_weights_and_empty_cash_do_not_mutate_configuration(self):
        before = dump_engine(self.engine)
        with self.assertRaises(EngineError):
            await self.select(start=True, weights={"207940": Decimal("80"), "068270": Decimal("30")})
        self.assertEqual(dump_engine(self.engine), before)
        self.engine.cash = Decimal("0")
        with self.assertRaises(EngineError):
            await self.select(start=True)
        self.assertFalse(self.engine.auto_discovery.enabled)

    async def test_fractional_fees_keep_saved_limit_valid_without_exceeding_percentage(self):
        self.engine.fee_rate = Decimal("0.00015")
        await self.engine.update_tick(TickRequest(symbol="207940", price=101, currency="KRW"))
        await self.engine.place_order(OrderRequest(symbol="207940", side="BUY", quantity=1))
        committed = Decimal("101.01515")
        fresh_limit = self.engine.available_cash * Decimal("0.30")
        await self.select(start=True)
        self.assertLessEqual(self.engine.auto_discovery.total_investment - committed, fresh_limit)
        self.assertEqual(self.engine.auto_discovery.total_investment.as_tuple().exponent, -2)
        saved = dump_engine(self.engine)
        restored = PaperEngine()
        restore_engine(restored, saved)
        self.assertEqual(dump_engine(restored), saved)
