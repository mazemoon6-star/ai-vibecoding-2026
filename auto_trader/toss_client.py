"""Read-only Toss Securities market-data client.

This client deliberately exposes no account or order methods.  Phase 2 feeds
prices into :class:`PaperEngine`; live order APIs remain out of the process.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import TossConfig

try:  # Keep the PAPER-only MVP usable before optional dependencies are installed.
    import httpx
except ImportError:  # pragma: no cover - exercised only in an uninstalled environment
    httpx = None  # type: ignore[assignment]


class TossApiError(Exception):
    """A safe error returned by the read-only broker client."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


class TossClient:
    """Minimal OAuth client for Toss current-price requests."""

    def __init__(self, config: TossConfig) -> None:
        self.config = config
        self._access_token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.config.market_data_enabled and self.config.credentials_configured

    async def close(self) -> None:
        """Reserved for a future shared HTTP connection pool."""

    async def _issue_token(self) -> str:
        if httpx is None:
            raise TossApiError(
                "httpx_missing",
                "토스 시세 연동에는 httpx 설치가 필요합니다. requirements.txt를 설치하세요.",
                503,
            )

        try:
            # The app runs locally and talks directly to Toss. In some Windows
            # environments HTTP(S)_PROXY is set to a non-existent local proxy,
            # which makes token requests fail before reaching Toss.
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=False) as client:
                response = await client.post(
                    f"{self.config.base_url}/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                    },
                )
        except httpx.HTTPError as exc:
            raise TossApiError("toss_network_error", "토스 토큰 서버에 연결할 수 없습니다.", 503) from exc
        if response.status_code >= 400:
            raise self._response_error(response, "token_request_failed")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise TossApiError("token_missing", "토큰 응답에 access_token이 없습니다.", 502)
        expires_in = int(payload.get("expires_in", 3600))
        self._access_token = str(token)
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 30, 1))
        return self._access_token

    async def _token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = datetime.now(timezone.utc)
            if self._access_token and now < self._token_expires_at:
                return self._access_token
            return await self._issue_token()

    async def get_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Read current prices for 1–200 symbols from Toss."""

        normalized = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
        if not 1 <= len(normalized) <= 200:
            raise TossApiError("invalid_symbols", "symbols는 1~200개여야 합니다.", 422)
        if not self.config.market_data_enabled:
            raise TossApiError("market_data_disabled", "TOSS_MARKET_DATA_ENABLED=true가 필요합니다.", 409)
        if not self.config.credentials_configured:
            raise TossApiError("toss_not_configured", "토스 Client ID와 Secret을 .env에 설정하세요.", 503)

        payload = await self._get_json("/api/v1/prices", {"symbols": ",".join(normalized)})
        result = payload.get("result")
        if not isinstance(result, list):
            raise TossApiError("invalid_toss_response", "토스 현재가 응답 형식이 올바르지 않습니다.", 502)
        return [item for item in result if isinstance(item, dict)]

    async def get_exchange_rate(self, base_currency: str = "USD", quote_currency: str = "KRW") -> dict[str, Any]:
        """Return the current broker-provided currency conversion rate."""

        if base_currency not in {"KRW", "USD"} or quote_currency not in {"KRW", "USD"}:
            raise TossApiError("invalid_currency", "지원 통화는 KRW와 USD입니다.", 422)
        if base_currency == quote_currency:
            raise TossApiError("same_currency", "기준 통화와 표시 통화는 달라야 합니다.", 422)
        self._require_market_data()
        payload = await self._get_json(
            "/api/v1/exchange-rate",
            {"baseCurrency": base_currency, "quoteCurrency": quote_currency},
        )
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("rate") in (None, ""):
            raise TossApiError("invalid_toss_response", "토스 환율 응답 형식이 올바르지 않습니다.", 502)
        return result

    async def get_volume_rankings(self, market_country: str) -> dict[str, Any]:
        """Return Toss market-wide trading-volume rankings for KR or US."""

        if market_country not in {"KR", "US"}:
            raise TossApiError("invalid_market", "market은 KR 또는 US여야 합니다.", 422)
        self._require_market_data()
        payload = await self._get_json(
            "/api/v1/rankings",
            {
                "type": "MARKET_TRADING_VOLUME",
                "marketCountry": market_country,
                "duration": "realtime",
                "count": "100",
            },
        )
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("rankings"), list):
            raise TossApiError("invalid_toss_response", "토스 거래량 순위 응답 형식이 올바르지 않습니다.", 502)
        return result

    async def get_stocks(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Look up display names for a batch of Toss symbols."""

        normalized = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
        if not 1 <= len(normalized) <= 200:
            raise TossApiError("invalid_symbols", "symbols는 1~200개여야 합니다.", 422)
        self._require_market_data()
        payload = await self._get_json("/api/v1/stocks", {"symbols": ",".join(normalized)})
        result = payload.get("result")
        if not isinstance(result, list):
            raise TossApiError("invalid_toss_response", "토스 종목 정보 응답 형식이 올바르지 않습니다.", 502)
        return [item for item in result if isinstance(item, dict)]

    def _require_market_data(self) -> None:
        if not self.config.market_data_enabled:
            raise TossApiError("market_data_disabled", "TOSS_MARKET_DATA_ENABLED=true가 필요합니다.", 409)
        if not self.config.credentials_configured:
            raise TossApiError("toss_not_configured", "토스 Client ID와 Secret을 .env에 설정하세요.", 503)

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if httpx is None:
            raise TossApiError(
                "httpx_missing",
                "토스 시세 연동에는 httpx 설치가 필요합니다. requirements.txt를 설치하세요.",
                503,
            )
        token = await self._token()
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=False) as client:
                    response = await client.get(
                        f"{self.config.base_url}{path}",
                        params=params,
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                raise TossApiError("toss_network_error", "토스 시세 서버에 연결할 수 없습니다.", 503) from exc
            if response.status_code != 401 or attempt == 1:
                break
            self._access_token = None
            token = await self._token()
        if response.status_code >= 400:
            raise self._response_error(response, "market_request_failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TossApiError("invalid_toss_response", "토스 응답이 JSON이 아닙니다.", 502) from exc
        if not isinstance(payload, dict):
            raise TossApiError("invalid_toss_response", "토스 응답 형식이 올바르지 않습니다.", 502)
        return payload

    @staticmethod
    def _response_error(response: Any, fallback_code: str) -> TossApiError:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return TossApiError(
                str(error.get("code", fallback_code)),
                str(error.get("message", "토스 API 요청에 실패했습니다.")),
                response.status_code,
                error.get("data") if isinstance(error.get("data"), dict) else None,
            )
        return TossApiError(fallback_code, "토스 API 요청에 실패했습니다.", response.status_code)
