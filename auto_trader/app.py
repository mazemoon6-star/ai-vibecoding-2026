"""FastAPI application for the paper-only trader MVP."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import JSONResponse

from .models import OrderRequest, OrderStatus, StrategyRequest, TickRequest
from .paper_engine import EngineError, PaperEngine


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize and close application resources.

    Version 0.1 keeps the paper engine in memory.  The lifespan hook is kept
    explicit so a database, broker client, and worker supervisor can be added
    without changing the API surface in a later version.
    """

    yield


app = FastAPI(
    title="Automatic Trader",
    version="0.1.0",
    description="Paper-only automatic trading MVP. No broker order is sent.",
    lifespan=lifespan,
)


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


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "mode": engine.mode.value}


@app.get("/health/ready")
async def ready() -> dict[str, object]:
    return {
        "status": "ready",
        "mode": engine.mode.value,
        "trading_enabled": engine.trading_enabled,
    }


@app.get("/api/v1/account")
async def account() -> dict[str, object]:
    return await engine.account()


@app.get("/api/v1/positions")
async def positions() -> dict[str, object]:
    return {"items": await engine.positions_snapshot()}


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


@app.post("/api/v1/orders", status_code=status.HTTP_201_CREATED)
async def create_order(request: OrderRequest) -> dict[str, object]:
    return {"order": await engine.place_order(request)}


@app.post("/api/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: str) -> dict[str, object]:
    return {"order": await engine.cancel_order(order_id)}


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
async def resume() -> dict[str, object]:
    return await engine.resume()


@app.post("/api/v1/controls/kill-switch")
async def kill_switch() -> dict[str, object]:
    return await engine.kill()


@app.get("/api/v1/metrics")
async def metrics() -> dict[str, object]:
    return await engine.metrics_snapshot()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("auto_trader.app:app", host="127.0.0.1", port=8000, reload=False)
