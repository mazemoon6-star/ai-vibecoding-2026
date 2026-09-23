import json
import unittest

import httpx
from pydantic import ValidationError

from auto_trader.models import AssistantChatRequest
from auto_trader.trading_assistant import TradingAssistantClient, TradingAssistantError


class TradingAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_client_fails_without_network_request(self) -> None:
        client = TradingAssistantClient(api_key="")

        with self.assertRaises(TradingAssistantError) as raised:
            await client.respond(AssistantChatRequest(message="계좌를 요약해 주세요"), {})

        self.assertEqual(raised.exception.code, "openai_not_configured")
        self.assertFalse(client.status()["configured"])

    async def test_response_request_is_read_only_and_extracts_output_text(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "현재 PAPER 계좌는 정상입니다."}],
                }]
            })

        client = TradingAssistantClient(
            api_key="sk-test",
            model="gpt-test",
            transport=httpx.MockTransport(handler),
        )
        reply = await client.respond(
            AssistantChatRequest(message="상태를 알려 주세요"),
            {"account": {"mode": "PAPER"}},
        )

        self.assertEqual(reply, "현재 PAPER 계좌는 정상입니다.")
        self.assertEqual(captured["model"], "gpt-test")
        self.assertFalse(captured["store"])
        self.assertIn("읽기 전용", captured["instructions"])
        self.assertIn('"mode":"PAPER"', captured["instructions"])

    def test_blank_message_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AssistantChatRequest(message="   ")


if __name__ == "__main__":
    unittest.main()
