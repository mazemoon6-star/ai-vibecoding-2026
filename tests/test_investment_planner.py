import json
import unittest

import httpx

from auto_trader.investment_planner import InvestmentPlannerClient, InvestmentPlannerError


class InvestmentPlannerTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {"keyword": "의약", "cash_percentage": 30, "available_cash": "10000",
            "candidates": [{"symbol": "207940"}, {"symbol": "068270"}]}

    def client(self, plan, *, status="completed"):
        def handler(request):
            self.payload = json.loads(request.content)
            return httpx.Response(200, json={"status": status, "output": [{"type": "message",
                "content": [{"type": "output_text", "text": json.dumps(plan)}]}]})
        return InvestmentPlannerClient(api_key="sk-test", model="gpt-test", transport=httpx.MockTransport(handler))

    async def test_structured_plan_uses_only_candidates_and_weights_sum_to_100(self):
        client = self.client({"items": [{"symbol": "207940", "weight_percent": 60},
            {"symbol": "068270", "weight_percent": 40}], "reason": "의약 섹터 분산"})
        plan = await client.plan(self.context())
        self.assertEqual([item.weight_percent for item in plan.items], [60, 40])
        self.assertEqual(self.payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(self.payload["text"]["format"]["strict"])
        self.assertFalse(self.payload["store"])
        self.assertEqual(self.payload["model"], "gpt-test")

    async def test_invalid_or_incomplete_ai_plans_are_not_accepted(self):
        for items in (
            [{"symbol": "OUTSIDE", "weight_percent": 100}],
            [{"symbol": "207940", "weight_percent": 60}],
            [{"symbol": "207940", "weight_percent": 50}, {"symbol": "207940", "weight_percent": 50}],
            [{"symbol": "207940", "weight_percent": 100.0}],
            [],
        ):
            with self.subTest(items=items), self.assertRaises(InvestmentPlannerError):
                await self.client({"items": items, "reason": "분산"}).plan(self.context())
        with self.assertRaises(InvestmentPlannerError):
            await self.client({"items": [{"symbol": "207940", "weight_percent": 100}],
                "reason": "분산"}, status="incomplete").plan(self.context())

    async def test_no_key_fails_without_a_network_call(self):
        client = InvestmentPlannerClient(api_key="")
        with self.assertRaises(InvestmentPlannerError) as error:
            await client.plan(self.context())
        self.assertEqual(error.exception.code, "openai_not_configured")

    async def test_blank_model_disables_planner_before_network_call(self):
        client = InvestmentPlannerClient(api_key="sk-test", model="")
        status = await client.status()
        self.assertFalse(status["configured"])
        self.assertIn("OPENAI_MODEL", status["message"])
        with self.assertRaises(InvestmentPlannerError) as error:
            await client.plan(self.context())
        self.assertEqual(error.exception.code, "openai_not_configured")

    async def test_malformed_response_is_rejected_safely(self):
        client = InvestmentPlannerClient(
            api_key="sk-test",
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"status": "completed", "output": [{"type": "message", "content": None}]}
            )),
        )
        with self.assertRaises(InvestmentPlannerError) as error:
            await client.plan(self.context())
        self.assertEqual(error.exception.code, "invalid_investment_plan")
