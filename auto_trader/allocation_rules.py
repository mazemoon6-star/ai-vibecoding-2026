"""Deterministic PAPER allocation for user-selected sector stocks."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Mapping


def liquidity_price_weights(
    prices: Mapping[str, Decimal],
    ranks: Mapping[str, int],
    budget: Decimal,
    *,
    fee_rate: Decimal,
    slippage_rate: Decimal,
) -> dict[str, Decimal]:
    """Reserve one affordable share per stock, then distribute the rest by liquidity rank.

    Rank 1 receives score 2.0, rank 100 receives 1.01, and an unranked
    user-selected stock receives 1.0. This caps the liquidity preference near 2:1.
    The PAPER engine still determines actual quantities at the later buy signal.
    """
    if not prices or budget <= 0:
        raise ValueError("투자 가능한 금액과 종목이 필요합니다.")
    required: dict[str, Decimal] = {}
    for symbol, price in prices.items():
        if price <= 0 or not price.is_finite():
            raise ValueError("선택 종목의 현재가가 올바르지 않습니다.")
        # Leave a small rounding margin for PAPER's fee/quantity calculation.
        required[symbol] = (price * (1 + slippage_rate) * (1 + fee_rate)).quantize(
            Decimal("1"), rounding=ROUND_UP
        ) + 1
    minimum_total = sum(required.values(), Decimal("0"))
    if minimum_total > budget:
        raise ValueError("선택한 모든 종목을 1주씩 살 자금이 부족합니다. 투자 비율을 높이거나 종목 수를 줄이세요.")

    scores = {
        symbol: Decimal("1") + Decimal(101 - min(101, max(1, ranks.get(symbol, 101)))) / 100
        for symbol in prices
    }
    score_total = sum(scores.values(), Decimal("0"))
    remainder = budget - minimum_total
    symbols = list(prices)
    weights = {
        symbol: ((required[symbol] + remainder * scores[symbol] / score_total) * 100 / budget)
        .quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        for symbol in symbols[:-1]
    }
    weights[symbols[-1]] = Decimal("100") - sum(weights.values(), Decimal("0"))
    return weights
