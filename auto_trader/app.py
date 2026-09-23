"""FastAPI application for the paper-only trader MVP."""

from __future__ import annotations

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import TossConfig, load_local_env
from .instruments import instrument_name
from .models import AutoStrategyResumeRequest, AutoStrategySymbolRequest, MarketSyncRequest, OrderRequest, OrderStatus, StrategyRequest, TickRequest
from .paper_engine import EngineError, PaperEngine
from .related_stock_search import expand_keyword, search_direct_stocks, search_related_stocks
from .toss_client import TossApiError, TossClient


load_local_env()


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a decimal") from exc
    return value


engine = PaperEngine(
    initial_cash=_decimal_env("PAPER_INITIAL_CASH", "1000000"),
    fee_rate=_decimal_env("PAPER_FEE_RATE", "0.00015"),
    slippage_rate=_decimal_env("PAPER_SLIPPAGE_RATE", "0"),
)
toss_config = TossConfig.from_env()
toss_client = TossClient(toss_config)
STATIC_DIR = Path(__file__).parent / "static"
PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.getenv("PAPER_STATE_PATH", "data/paper_state.sqlite3"))
if not STATE_PATH.is_absolute():
    STATE_PATH = PROJECT_DIR / STATE_PATH
logger = logging.getLogger(__name__)


async def _market_data_monitor() -> None:
    """Poll held and activated auto-strategy symbols into the PAPER engine."""

    while True:
        try:
            positions = [position for position in await engine.positions_snapshot() if position["quantity"] > 0]
            symbols = list(dict.fromkeys(
                [str(position["symbol"]) for position in positions]
                + await engine.auto_strategy_symbols()
            ))[:200]
            if symbols and toss_client.configured:
                for item in await toss_client.get_prices(symbols):
                    symbol = str(item.get("symbol", "")).strip().upper()
                    raw_price = item.get("lastPrice")
                    if not symbol or raw_price in (None, ""):
                        continue
                    try:
                        price = Decimal(str(raw_price))
                    except (InvalidOperation, ValueError):
                        continue
                    timestamp = datetime.now(timezone.utc)
                    raw_timestamp = item.get("timestamp")
                    if isinstance(raw_timestamp, str):
                        try:
                            timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    await engine.update_tick(
                        TickRequest(symbol=symbol, name=instrument_name(symbol), price=price, timestamp=timestamp)
                    )
                if engine.trading_enabled:
                    await engine.run_strategies()
        except TossApiError as exc:
            logger.warning("PAPER auto-trading quote polling failed: %s", exc.code)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PAPER market-data monitor failed")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Restore the account before polling; every state mutation is committed."""
    engine.enable_persistence(STATE_PATH)
    monitor_task = asyncio.create_task(_market_data_monitor()) if toss_client.configured else None
    try:
        yield
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        try:
            await toss_client.close()
        finally:
            engine.close_persistence()


app = FastAPI(
    title="Automatic Trader",
    version="0.2.0",
    description="Phase 2 paper trader with read-only Toss market data and dashboard.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(EngineError)
async def engine_error_handler(_: Request, exc: EngineError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "data": exc.data,
            }
        },
    )


@app.exception_handler(TossApiError)
async def toss_error_handler(_: Request, exc: TossApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "data": exc.data,
            }
        },
    )


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "mode": engine.mode.value}


@app.get("/health/ready")
async def ready() -> dict[str, object]:
    return {
        "status": "ready",
        "mode": engine.mode.value,
        "trading_enabled": engine.trading_enabled,
        "market_data_configured": toss_client.configured,
        "paper_state_persisted": engine.state_store is not None,
    }


@app.get("/api/v1/broker/status")
async def broker_status() -> dict[str, object]:
    """Expose connection capability without exposing credentials or tokens."""

    return {
        "provider": "toss",
        "mode": engine.mode,
        "market_data": {
            "enabled": toss_config.market_data_enabled,
            "credentials_configured": toss_config.credentials_configured,
            "read_only": True,
        },
        "orders": {"enabled": False, "reason": "phase_2_paper_only"},
    }


@app.get("/api/v1/account")
async def account() -> dict[str, object]:
    return await engine.account()


@app.get("/api/v1/positions")
async def positions() -> dict[str, object]:
    return {"items": await engine.positions_snapshot()}


@app.get("/api/v1/market/ticks")
async def ticks() -> dict[str, object]:
    return {"items": await engine.ticks_snapshot()}


@app.get("/api/v1/market/related-stocks")
@app.get("/api/v1/market/volume-search", include_in_schema=False)
async def related_stocks(
    keyword: Annotated[str, Query(min_length=1, max_length=80)],
    market: Annotated[str, Query(pattern="^(KR|US)$")] = "KR",
) -> dict[str, object]:
    """Search curated issuer business profiles using expanded industry concepts."""

    keyword = keyword.strip()
    if not keyword:
        raise TossApiError("invalid_keyword", "검색어를 입력하세요.", 422)
    try:
        direct_matches = search_direct_stocks(keyword, market, limit=5)
        matches = direct_matches or search_related_stocks(keyword, market, limit=5)
    except ValueError as exc:
        raise TossApiError("invalid_market", "market은 KR 또는 US여야 합니다.", 422) from exc

    price_source: str | None = None
    if direct_matches:
        symbols = [str(item["symbol"]) for item in matches]
        try:
            stock_info = await toss_client.get_stocks(symbols)
            names = {
                str(stock.get("symbol", "")).upper(): str(stock.get("name", "")).strip()
                for stock in stock_info
                if stock.get("symbol") and stock.get("name")
            }
            for item in matches:
                if names.get(str(item["symbol"]).upper()):
                    item["name"] = names[str(item["symbol"]).upper()]
        except TossApiError:
            # Alias-based results remain useful if optional broker metadata is unavailable.
            pass

    # Search results should always try the live quote API, including semantic
    # industry matches. A query needs at most five symbols, within Toss limits.
    symbols = [str(item["symbol"]) for item in matches]
    if symbols:
        try:
            prices = await toss_client.get_prices(symbols)
            live_prices = {
                str(price.get("symbol", "")).upper(): price
                for price in prices
                if price.get("symbol") and price.get("lastPrice") not in (None, "")
            }
            for item in matches:
                quote = live_prices.get(str(item["symbol"]).upper())
                if quote:
                    item["price"] = quote["lastPrice"]
                    item["price_updated_at"] = quote.get("timestamp")
                    item["price_source"] = "toss"
            if live_prices:
                price_source = "toss"
        except TossApiError as exc:
            if any(item.get("verify_with_broker") for item in matches) and exc.status_code == 404:
                matches = []

    ticks_by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in await engine.ticks_snapshot()
    }
    for item in matches:
        if item.get("price") is None:
            quote = ticks_by_symbol.get(str(item["symbol"]).upper())
            item["price"] = quote.get("price") if quote else None
            item["price_updated_at"] = quote.get("timestamp") if quote else None
            if quote:
                item["price_source"] = "paper"
                price_source = price_source or "paper_ticks"

    expansion = expand_keyword(keyword)
    return {
        "market": market,
        "search_type": "direct" if direct_matches else "related",
        "keyword": keyword,
        "expanded_keywords": expansion["terms"],
        "profile_count": len(matches),
        "price_source": price_source if any(item.get("price") is not None for item in matches) else None,
        "items": matches,
    }


@app.get("/api/v1/market/history/{symbol}")
async def market_history(symbol: str) -> dict[str, object]:
    return await engine.chart_snapshot(symbol)


@app.get("/api/v1/orders")
async def orders(
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
) -> dict[str, object]:
    return {"items": await engine.orders_snapshot(order_status)}


@app.post("/api/v1/market/ticks", status_code=status.HTTP_200_OK)
async def ingest_tick(request: TickRequest) -> dict[str, object]:
    """Ingest a market tick and immediately evaluate enabled strategies."""

    tick_result = await engine.update_tick(request)
    strategy_results = await engine.run_strategies()
    return {**tick_result, "strategy_results": strategy_results}


@app.post("/api/v1/market/sync")
async def sync_market(request: MarketSyncRequest) -> dict[str, object]:
    """Fetch Toss prices, feed them to PAPER, then evaluate strategies."""

    prices = await toss_client.get_prices(request.symbols)
    symbols_needing_names = list(dict.fromkeys(
        str(item.get("symbol", "")).strip().upper()
        for item in prices
        if item.get("symbol")
        and (
            not str(item.get("name", "")).strip()
            or str(item.get("name", "")).strip().upper() == str(item.get("symbol", "")).strip().upper()
        )
    ))
    stock_names: dict[str, str] = {}
    if symbols_needing_names:
        try:
            stocks = await toss_client.get_stocks(symbols_needing_names)
            stock_names = {
                str(stock.get("symbol", "")).strip().upper(): str(stock.get("name", "")).strip()
                for stock in stocks
                if stock.get("symbol") and stock.get("name")
            }
        except TossApiError:
            # Quote sync should still work if optional display-name lookup fails.
            pass

    ingested: list[dict[str, object]] = []
    for item in prices:
        symbol = str(item.get("symbol", "")).strip().upper()
        raw_price = item.get("lastPrice")
        if not symbol or raw_price in (None, ""):
            continue
        try:
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError) as exc:
            raise TossApiError("invalid_price", "토스 응답의 현재가를 해석할 수 없습니다.", 502) from exc
        raw_timestamp = item.get("timestamp")
        timestamp = datetime.now(timezone.utc)
        if isinstance(raw_timestamp, str):
            try:
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError:
                pass
        tick_result = await engine.update_tick(
            TickRequest(
                symbol=symbol,
                name=(
                    str(item.get("name")).strip()
                    if item.get("name") and str(item.get("name")).strip().upper() != symbol
                    else stock_names.get(symbol) or instrument_name(symbol)
                ),
                price=price,
                timestamp=timestamp,
            )
        )
        ingested.append(tick_result["tick"])
    strategy_results = await engine.run_strategies()
    return {
        "source": "toss",
        "requested_symbols": request.symbols,
        "ingested": ingested,
        "strategy_results": strategy_results,
    }


@app.post("/api/v1/orders", status_code=status.HTTP_201_CREATED)
async def create_order(request: OrderRequest) -> dict[str, object]:
    return {"order": await engine.place_order(request)}


@app.post("/api/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: str) -> dict[str, object]:
    return {"order": await engine.cancel_order(order_id)}


@app.get("/api/v1/auto-trade-symbols")
async def auto_trade_symbols() -> dict[str, object]:
    return {"items": await engine.auto_watchlist_snapshot()}


@app.post("/api/v1/auto-trade-symbols", status_code=status.HTTP_201_CREATED)
async def add_auto_trade_symbol(request: AutoStrategySymbolRequest) -> dict[str, object]:
    return {"item": await engine.add_auto_symbol(request)}


@app.delete("/api/v1/auto-trade-symbols/{symbol}")
async def remove_auto_trade_symbol(symbol: str) -> dict[str, object]:
    return await engine.remove_auto_symbol(symbol)


@app.post("/api/v1/strategies", status_code=status.HTTP_201_CREATED)
async def create_strategy(request: StrategyRequest) -> dict[str, object]:
    return {"strategy": await engine.create_strategy(request)}


@app.get("/api/v1/strategies")
async def list_strategies() -> dict[str, object]:
    return {"items": await engine.list_strategies()}


@app.post("/api/v1/engine/step")
async def run_engine_step() -> dict[str, object]:
    return {"results": await engine.run_strategies()}


@app.post("/api/v1/controls/pause")
async def pause() -> dict[str, object]:
    return await engine.pause()


@app.post("/api/v1/controls/resume")
async def resume(request: AutoStrategyResumeRequest = Body(default=AutoStrategyResumeRequest())) -> dict[str, object]:
    auto_strategies = await engine.activate_auto_strategies(request.settings)
    result = await engine.resume()
    result["auto_strategies"] = auto_strategies
    return result


@app.post("/api/v1/controls/kill-switch")
async def kill_switch() -> dict[str, object]:
    return await engine.kill()


@app.get("/api/v1/metrics")
async def metrics() -> dict[str, object]:
    return await engine.metrics_snapshot()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("auto_trader.app:app", host="127.0.0.1", port=8000, reload=False)
