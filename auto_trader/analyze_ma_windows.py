"""Backtest fixed moving-average windows with Toss candle data.

This is an offline analysis helper. It never places an order and only uses the
read-only market-data client configured for the PAPER dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean
from typing import Any

from .config import TossConfig, load_local_env
from .toss_client import TossClient


@dataclass(frozen=True)
class BacktestResult:
    short_window: int
    long_window: int
    average_return: float
    symbol_returns: dict[str, float]
    trades: dict[str, int]


async def fetch_closes(client: TossClient, symbol: str, count: int) -> list[float]:
    """Fetch up to ``count`` one-minute closes, oldest first."""

    candles: dict[str, dict[str, Any]] = {}
    before: str | None = None
    while len(candles) < count:
        params = {"symbol": symbol, "interval": "1m", "count": str(min(200, count - len(candles)))}
        if before:
            params["before"] = before
        payload = await client._get_json("/api/v1/candles", params)
        result = payload.get("result")
        page = result.get("candles") if isinstance(result, dict) else None
        if not isinstance(page, list) or not page:
            break
        previous_size = len(candles)
        for candle in page:
            if isinstance(candle, dict) and candle.get("timestamp") and candle.get("closePrice") is not None:
                candles[str(candle["timestamp"])] = candle
        next_before = result.get("nextBefore")
        if len(candles) == previous_size or not isinstance(next_before, str) or next_before == before:
            break
        before = next_before
    ordered = sorted(candles.items())[-count:]
    return [float(candle["closePrice"]) for _, candle in ordered]


def backtest(prices: list[float], short_window: int, long_window: int, fee_rate: float) -> tuple[float, int]:
    """Return marked-to-market strategy return and completed order count."""

    cash = 1.0
    quantity = 0.0
    previous_relation: int | None = None
    trades = 0
    short_sum = sum(prices[:short_window])
    long_sum = sum(prices[:long_window])

    for index in range(long_window - 1, len(prices)):
        if index > long_window - 1:
            short_sum += prices[index] - prices[index - short_window]
            long_sum += prices[index] - prices[index - long_window]
        elif short_window < long_window:
            short_sum = sum(prices[index - short_window + 1 : index + 1])

        short_average = short_sum / short_window
        long_average = long_sum / long_window
        relation = (short_average > long_average) - (short_average < long_average)
        price = prices[index]
        if previous_relation is not None and previous_relation <= 0 < relation and quantity == 0:
            quantity = cash / (price * (1.0 + fee_rate))
            cash = 0.0
            trades += 1
        elif previous_relation is not None and previous_relation >= 0 > relation and quantity > 0:
            cash = quantity * price * (1.0 - fee_rate)
            quantity = 0.0
            trades += 1
        previous_relation = relation

    equity = cash if quantity == 0 else quantity * prices[-1] * (1.0 - fee_rate)
    return equity - 1.0, trades


def rank_windows(series: dict[str, list[float]], fee_rate: float) -> list[BacktestResult]:
    """Rank one common window pair by average return across all symbols."""

    ranked: list[BacktestResult] = []
    for short_window in range(2, 51):
        for long_window in range(max(short_window + 1, 5), 126):
            if any(len(prices) <= long_window for prices in series.values()):
                continue
            outcomes = {symbol: backtest(prices, short_window, long_window, fee_rate) for symbol, prices in series.items()}
            returns = {symbol: outcome[0] for symbol, outcome in outcomes.items()}
            ranked.append(BacktestResult(
                short_window=short_window,
                long_window=long_window,
                average_return=fmean(returns.values()),
                symbol_returns=returns,
                trades={symbol: outcome[1] for symbol, outcome in outcomes.items()},
            ))
    return sorted(ranked, key=lambda item: item.average_return, reverse=True)


async def analyze(symbols: list[str], candle_count: int, fee_rate: float, polls_per_candle: int) -> None:
    client = TossClient(TossConfig.from_env())
    try:
        series = {symbol: await fetch_closes(client, symbol, candle_count) for symbol in symbols}
    finally:
        await client.close()
    for symbol, prices in series.items():
        print(f"{symbol}: {len(prices)} one-minute candles")
    ranked = rank_windows(series, fee_rate)

    def print_results(title: str, results: list[BacktestResult]) -> None:
        print(title)
        print(
            "short_minutes,long_minutes,fixed_short_window,fixed_long_window,average_return,"
            + ",".join(f"{symbol}_return,{symbol}_trades" for symbol in symbols)
        )
        for result in results:
            details = ",".join(
                f"{result.symbol_returns[symbol]:.6%},{result.trades[symbol]}" for symbol in symbols
            )
            print(
                f"{result.short_window},{result.long_window},"
                f"{result.short_window * polls_per_candle},{result.long_window * polls_per_candle},"
                f"{result.average_return:.6%},{details}"
            )

    print_results("highest average return", ranked[:10])
    robust = sorted(
        ranked,
        key=lambda item: (min(item.symbol_returns.values()), item.average_return),
        reverse=True,
    )
    print_results("highest worst-symbol return", robust[:10])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Toss stock symbols to compare")
    parser.add_argument("--candles", type=int, default=2_000, help="one-minute candles per symbol")
    parser.add_argument("--fee-rate", type=Decimal, default=Decimal("0.00015"), help="fee charged on each side")
    parser.add_argument(
        "--polls-per-candle",
        type=int,
        default=4,
        help="engine polling samples represented by each one-minute candle (default: 4 for 15-second polling)",
    )
    args = parser.parse_args()
    if args.candles < 121:
        parser.error("--candles must be at least 121")
    if args.polls_per_candle < 1:
        parser.error("--polls-per-candle must be at least 1")
    load_local_env()
    asyncio.run(analyze(
        [symbol.strip().upper() for symbol in args.symbols],
        args.candles,
        float(args.fee_rate),
        args.polls_per_candle,
    ))


if __name__ == "__main__":
    main()
