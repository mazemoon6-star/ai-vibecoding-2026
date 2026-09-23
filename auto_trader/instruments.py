"""Small local symbol catalog used to make PAPER data readable.

The Toss current-price endpoint returns the symbol and price used by the
engine, but the MVP does not have a security-master database yet.  Keeping a
small catalog here gives the dashboard a useful display name while unknown
symbols still fall back to their symbol.
"""

from __future__ import annotations


INSTRUMENT_NAMES: dict[str, str] = {
    # Frequently used Korean symbols.
    "000660": "SK하이닉스",
    "000990": "DB하이텍",
    "005930": "삼성전자",
    "010120": "LS ELECTRIC",
    "010950": "S-OIL",
    "032640": "LG유플러스",
    "036200": "유니셈",
    "053080": "케이엔솔",
    "066570": "LG전자",
    "011930": "신성이엔지",
    "018880": "한온시스템",
    "083450": "GST",
    # Cooling and data-center watchlist symbols.
    "CARR": "Carrier Global",
    "MOD": "Modine Manufacturing",
    "NVT": "nVent Electric",
    "SMCI": "Super Micro Computer",
    "TT": "Trane Technologies",
    "VRT": "Vertiv Holdings",
    # Paper-only sample symbol.
    "TEST": "테스트 종목",
}


def instrument_name(symbol: str) -> str:
    """Return a display name, falling back to the normalized symbol."""

    normalized = symbol.strip().upper()
    return INSTRUMENT_NAMES.get(normalized, normalized)
