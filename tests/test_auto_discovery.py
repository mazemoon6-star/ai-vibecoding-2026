import asyncio
from decimal import Decimal
import unittest
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from auto_trader import app as dashboard
from auto_trader.models import AutoDiscoveryRequest, AutoStrategySymbolRequest, OrderRequest, TickRequest
from auto_trader.paper_engine import EngineError, PaperEngine
from auto_trader.paper_state import dump_engine, restore_engine
from auto_trader.investment_planner import InvestmentPlan
from auto_trader.trading_assistant import TradingAssistantError
from auto_trader.toss_client import TossApiError


class AutoDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = PaperEngine(initial_cash=Decimal("10000"), fee_rate=Decimal("0"))
        self.request = AutoDiscoveryRequest(keyword="냉각", market="KR", max_symbols=2, order_quantity=2)
        self.client = SimpleNamespace(
            configured=True,
            get_market_rankings=AsyncMock(return_value={"rankings": [
                {"symbol": "053080", "rank": 3},
                {"symbol": "066570", "rank": 1},
                {"symbol": "083450", "rank": 2},
                {"symbol": "UNRELATED", "rank": 0},
            ]}),
            get_prices=AsyncMock(side_effect=lambda symbols: [
                {"symbol": symbol, "lastPrice": "100"} for symbol in symbols
            ]),
        )
        for attribute, replacement in (
            ("engine", self.engine), ("toss_client", self.client),
            ("auto_discovery_lock", asyncio.Lock()), ("auto_discovery_last_attempt", None),
            ("auto_discovery_status", {"selected_symbols": [], "error": None, "last_scan_at": None}),
        ):
            patcher = patch.object(dashboard, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Bounded catalog fixtures isolate engine/scanner behavior from keyword scoring.
        self.candidates = [
            {"symbol": symbol, "name": symbol, "relevance_score": 80}
            for symbol in ("066570", "083450", "053080")
        ]
        for attribute, replacement in (
            ("search_direct_stocks", lambda *args, **kwargs: []),
            ("search_related_stocks", lambda *args, **kwargs: self.candidates),
        ):
            patcher = patch.object(dashboard, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_start_fetches_quotes_selects_ranked_sector_stocks_and_resumes(self):
        await self.engine.pause()
        await self.engine.update_tick(TickRequest(symbol="MANUAL", price=100))
        await self.engine.add_auto_symbol(AutoStrategySymbolRequest(symbol="MANUAL"))

        result = await dashboard.start_auto_discovery(self.request)

        self.assertEqual(result["status"]["selected_symbols"], ["066570", "083450"])
        self.client.get_prices.assert_awaited_once_with(["066570", "083450"])
        self.assertTrue(self.engine.trading_enabled)
        self.assertEqual(await self.engine.auto_strategy_symbols(), ["066570", "083450"])
        self.assertEqual(len(self.engine.orders), 0)
        for strategy in self.engine.strategies.values():
            self.assertEqual(strategy.max_position, Decimal("2"))
        self.assertEqual({item["status"] for item in result["selected"]}, {"시세 수집 중"})

    async def test_new_sector_profiles_can_start_budget_auto_discovery(self):
        from auto_trader.related_stock_search import search_related_stocks

        for keyword, symbols in (
            ("의약", ["207940", "068270"]),
            ("우주", ["012450", "099320"]),
            ("로봇", ["454910", "277810"]),
        ):
            with self.subTest(keyword=keyword), patch.object(dashboard, "search_related_stocks", search_related_stocks):
                self.client.get_market_rankings.return_value = {"rankings": [
                    {"symbol": "UNRELATED", "rank": 0},
                    *[{"symbol": symbol, "rank": rank} for rank, symbol in enumerate(symbols, 1)]
                ]}
                result = await dashboard.start_auto_discovery(AutoDiscoveryRequest(
                    keyword=keyword, market="KR", max_symbols=5, total_investment=Decimal("6000")
                ))
                self.assertEqual(result["config"]["keyword"], keyword)
                self.assertEqual(result["status"]["selected_symbols"], symbols)
                self.assertEqual([item["budget"] for item in result["allocation"]["items"]], [Decimal("3000")] * 2)
                self.assertTrue(self.engine.trading_enabled)
                self.assertEqual(len(self.engine.orders), 0)

    async def test_rotation_blocks_old_entries_preserves_exits_and_removes_flat_stocks(self):
        await dashboard.start_auto_discovery(self.request)
        await self.engine.place_order(OrderRequest(symbol="066570", side="BUY", quantity=2))
        old_strategy = self.engine.strategies[self.engine.auto_strategy_ids["066570"]]
        flat_strategy = self.engine.strategies[self.engine.auto_strategy_ids["083450"]]
        self.client.get_market_rankings.return_value = {"rankings": [{"symbol": "053080", "rank": 1}]}
        await dashboard._scan_auto_discovery()

        self.assertFalse(old_strategy.entry_enabled)
        self.assertTrue(old_strategy.enabled)
        self.assertFalse(flat_strategy.enabled)
        self.assertNotIn("083450", self.engine.auto_watchlist)
        await self.engine.pause()
        await self.engine.activate_auto_strategies()
        await self.engine.resume()
        self.assertFalse(old_strategy.entry_enabled)

        # Exercise the actual MA crossing logic with an exit-only strategy.
        old_strategy.short_window, old_strategy.long_window = 2, 3
        self.engine.price_history["066570"].clear()
        self.engine.price_history["066570"].extend(map(Decimal, ("100", "100", "120")))
        old_strategy.previous_relation = -1
        results = await self.engine.run_strategies()
        old_result = next(item for item in results if item["symbol"] == "066570")
        self.assertEqual(old_result["reason"], "not_selected_for_entry")
        self.assertEqual(len(self.engine.orders), 1)
        await self.engine.update_tick(TickRequest(symbol="066570", price=60))
        await self.engine.run_strategies()
        self.assertEqual(self.engine.positions["066570"].quantity, Decimal("0"))
        await dashboard._scan_auto_discovery()
        self.assertNotIn("066570", self.engine.auto_watchlist)
        self.assertFalse(old_strategy.enabled)

    async def test_stop_during_network_request_prevents_late_start(self):
        async def stop_then_quote(symbols):
            await dashboard.stop_auto_discovery()
            return [{"symbol": symbol, "lastPrice": "100"} for symbol in symbols]
        self.client.get_prices.side_effect = stop_then_quote
        with self.assertRaises(EngineError) as error:
            await dashboard.start_auto_discovery(self.request)
        self.assertEqual(error.exception.code, "discovery_interrupted")
        self.assertFalse(self.engine.trading_enabled)
        self.assertFalse(self.engine.auto_discovery.enabled)
        self.assertEqual(self.engine.auto_watchlist, {})

    async def test_failed_refresh_keeps_existing_selection_and_reports_error(self):
        await dashboard.start_auto_discovery(self.request)
        self.client.get_market_rankings.side_effect = TossApiError("offline", "시세 연결 실패", 503)
        before = dump_engine(self.engine)
        with self.assertRaises(TossApiError):
            await dashboard._scan_auto_discovery()
        self.assertEqual(dump_engine(self.engine), before)
        status = await dashboard.get_auto_discovery()
        self.assertEqual(status["selected_symbols"], ["066570", "083450"])
        self.assertEqual(status["error"], "시세 연결 실패")
        self.assertFalse(status["scanning"])

    async def test_partial_quotes_do_not_change_existing_configuration(self):
        await dashboard.start_auto_discovery(self.request)
        before = dump_engine(self.engine)
        self.client.get_prices.side_effect = None
        self.client.get_prices.return_value = [{"symbol": "066570", "lastPrice": "100"}]
        with self.assertRaises(EngineError):
            await dashboard.start_auto_discovery(self.request.model_copy(update={"order_quantity": Decimal("5")}))
        self.assertEqual(dump_engine(self.engine), before)

    async def test_http_endpoints_serialize_configuration_and_reject_blank_keyword(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.app), base_url="http://test"
        ) as client:
            invalid = await client.post("/api/v1/auto-discovery/start", json={"keyword": "   "})
            self.assertEqual(invalid.status_code, 422)
            started = await client.post(
                "/api/v1/auto-discovery/start", json=self.request.model_dump(mode="json")
            )
            self.assertEqual(started.status_code, 200)
            self.assertEqual(started.json()["config"]["order_quantity"], "2")
            status = (await client.get("/api/v1/auto-discovery")).json()
            self.assertTrue(status["enabled"])
            self.assertEqual(status["selected_symbols"], ["066570", "083450"])
            stopped = (await client.post("/api/v1/auto-discovery/stop")).json()
            self.assertFalse(stopped["trading_enabled"])
            self.assertFalse(stopped["config"]["enabled"])
            self.assertEqual(len(self.engine.auto_watchlist), 2)

    async def test_discovery_state_restores_and_legacy_state_defaults_to_disabled(self):
        await dashboard.start_auto_discovery(self.request)
        saved = dump_engine(self.engine)
        restored = PaperEngine()
        restore_engine(restored, saved)
        self.assertEqual(dump_engine(restored), saved)
        saved.pop("auto_discovery")
        for strategy in saved["strategies"].values():
            strategy.pop("entry_enabled")
        restore_engine(restored, saved)
        self.assertFalse(restored.auto_discovery.enabled)
        self.assertEqual(restored.auto_discovery.managed_symbols, [])

    async def test_budget_http_start_and_periodic_scan_keep_allocation_settings(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.app), base_url="http://test"
        ) as client:
            response = await client.post("/api/v1/auto-discovery/start", json={
                "keyword": "냉각", "market": "KR", "max_symbols": 2, "total_investment": "6000"
            })
            self.assertEqual(response.status_code, 200)
            allocation = response.json()["allocation"]
            self.assertEqual([Decimal(item["budget"]) for item in allocation["items"]], [Decimal("3000")] * 2)
            self.assertEqual([Decimal(item["estimated_quantity"]) for item in allocation["items"]], [Decimal("30")] * 2)
            await dashboard._scan_auto_discovery()
            status = (await client.get("/api/v1/auto-discovery")).json()
            self.assertEqual(status["total_investment"], "6000")
            self.assertEqual(status["allocation"], allocation)

    async def test_paused_engine_collects_quotes_without_filling_pending_orders(self):
        await self.engine.update_tick(TickRequest(symbol="TEST", price=100))
        pending = await self.engine.place_order(OrderRequest(
            symbol="TEST", side="BUY", quantity=1, order_type="LIMIT", price=90
        ))
        await dashboard.stop_auto_discovery()
        await self.engine.update_tick(TickRequest(symbol="TEST", price=80))
        self.assertEqual(self.engine.orders[pending["order_id"]].status, "PENDING")
        self.assertNotIn("TEST", self.engine.positions)

    async def test_searching_other_sector_preserves_active_selection_and_periodic_scan_keyword(self):
        cooling_candidates = self.candidates
        semiconductor_candidates = [{"symbol": "083450", "name": "GST", "relevance_score": 80}]
        search = Mock(side_effect=lambda keyword, *args, **kwargs:
            cooling_candidates if keyword == "냉각" else semiconductor_candidates)
        with patch.object(dashboard, "search_related_stocks", search):
            await dashboard.start_auto_discovery(self.request.model_copy(update={
                "total_investment": Decimal("6000")
            }))
            before = dump_engine(self.engine)
            allocation = await self.engine.auto_allocation_snapshot()
            self.client.get_market_rankings.reset_mock()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=dashboard.app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/market/related-stocks", params={
                    "keyword": "반도체", "market": "KR"
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["symbol"] for item in response.json()["items"]], ["083450"])
                self.assertEqual(dump_engine(self.engine), before)
                self.assertEqual(await self.engine.auto_allocation_snapshot(), allocation)
                self.client.get_market_rankings.assert_not_awaited()
                await dashboard._scan_auto_discovery()
                self.assertEqual(search.call_args.args[0], "냉각")
                status = (await client.get("/api/v1/auto-discovery")).json()
                self.assertEqual(status["keyword"], "냉각")
                self.assertEqual(status["selected_symbols"], ["066570", "083450"])
                self.assertEqual(await self.engine.auto_allocation_snapshot(), allocation)

    async def test_cash_ratio_http_uses_ai_count_and_weights_and_keeps_budget_on_rescan(self):
        symbols = ["066570", "083450", "053080", "000100"]
        self.candidates.append({"symbol": "000100", "name": "유한양행", "relevance_score": 80})
        self.client.get_market_rankings.return_value = {"rankings": [
            {"symbol": symbol, "rank": rank} for rank, symbol in enumerate(symbols, 1)
        ]}
        plan = InvestmentPlan(items=[{"symbol": symbol, "weight_percent": 25} for symbol in symbols], reason="4종목 분산")
        planner = SimpleNamespace(configured=True, model="gpt-test", plan=AsyncMock(return_value=plan),
            status=lambda: {"configured": True, "model": "gpt-test"})
        with patch.object(dashboard, "investment_planner", planner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard.app), base_url="http://test") as client:
                started = await client.post("/api/v1/auto-discovery/start", json={
                    "keyword": "냉각", "cash_percentage": 30, "max_symbols": 1
                })
                self.assertEqual(started.status_code, 200)
                self.assertEqual(started.json()["status"]["selected_symbols"], symbols)
                self.assertEqual(started.json()["config"]["total_investment"], "3000.00")
                self.assertEqual(planner.plan.call_args.args[0]["new_investment_budget"], Decimal("3000"))
                await self.engine.place_order(OrderRequest(symbol="066570", side="BUY", quantity=5))
                await dashboard._scan_auto_discovery()
                self.assertEqual(planner.plan.call_args.args[0]["new_investment_budget"], Decimal("2500"))
                status = (await client.get("/api/v1/auto-discovery")).json()
                self.assertEqual(status["cash_percentage"], 30)
                self.assertEqual(Decimal(status["total_investment"]), Decimal("3000"))
                self.assertEqual(Decimal(status["cash_base"]), Decimal("10000"))
                self.assertEqual(status["planner_model"], "gpt-test")

    async def test_missing_ai_key_or_interrupted_ai_plan_cannot_replace_selection(self):
        await dashboard.start_auto_discovery(self.request)
        before = dump_engine(self.engine)
        with patch.object(dashboard, "investment_planner", SimpleNamespace(configured=False)):
            with self.assertRaises(TradingAssistantError):
                await dashboard.start_auto_discovery(AutoDiscoveryRequest(keyword="의약", cash_percentage=30))
        self.assertEqual(dump_engine(self.engine), before)

        async def stop_then_plan(context):
            await dashboard.stop_auto_discovery()
            return InvestmentPlan(items=[{"symbol": "066570", "weight_percent": 100}], reason="의약")

        planner = SimpleNamespace(configured=True, model="gpt-test", plan=stop_then_plan)
        with patch.object(dashboard, "investment_planner", planner), self.assertRaises(EngineError):
            await dashboard.start_auto_discovery(AutoDiscoveryRequest(keyword="의약", cash_percentage=30))
        self.assertFalse(self.engine.auto_discovery.enabled)
        self.assertFalse(self.engine.trading_enabled)
        self.assertEqual(self.engine.auto_discovery.keyword, "냉각")

    async def test_ai_refresh_failure_does_not_skip_existing_strategy_quote_polling(self):
        await dashboard.start_auto_discovery(self.request)
        self.client.get_prices.reset_mock()
        run_strategies = AsyncMock()
        with patch.object(dashboard, "auto_discovery_last_attempt", None), patch.object(
            dashboard, "_scan_auto_discovery", AsyncMock(side_effect=TradingAssistantError(
                "planner_network_error", "AI 연결 실패", 503
            ))
        ), patch.object(self.engine, "run_strategies", run_strategies), patch.object(
            dashboard.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
        ), self.assertLogs(dashboard.logger, level="WARNING"), self.assertRaises(asyncio.CancelledError):
            await dashboard._market_data_monitor()
        self.client.get_prices.assert_awaited_once_with(["066570", "083450"])
        run_strategies.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
