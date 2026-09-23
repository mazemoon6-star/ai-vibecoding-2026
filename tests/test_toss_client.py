import unittest
from unittest.mock import AsyncMock, patch

from auto_trader.config import TossConfig
from auto_trader.toss_client import TossApiError, TossClient


class TossClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = TossClient(TossConfig(
            base_url="https://example.invalid",
            ws_url="wss://example.invalid",
            client_id="client",
            client_secret="secret",
            account_seq="",
            market_data_enabled=True,
            timeout_seconds=1,
        ))

    async def test_exchange_rate_uses_required_currency_parameters(self) -> None:
        response = {"result": {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1366.3"}}
        with patch.object(self.client, "_get_json", AsyncMock(return_value=response)) as request:
            result = await self.client.get_exchange_rate()

        self.assertEqual(result["rate"], "1366.3")
        request.assert_awaited_once_with(
            "/api/v1/exchange-rate",
            {"baseCurrency": "USD", "quoteCurrency": "KRW"},
        )

    async def test_exchange_rate_rejects_invalid_response(self) -> None:
        with patch.object(self.client, "_get_json", AsyncMock(return_value={"result": {}})):
            with self.assertRaises(TossApiError) as error:
                await self.client.get_exchange_rate()

        self.assertEqual(error.exception.code, "invalid_toss_response")


if __name__ == "__main__":
    unittest.main()
