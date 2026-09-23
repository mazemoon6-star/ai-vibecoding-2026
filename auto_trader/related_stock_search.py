"""Offline, profile-based related-stock search.

The catalog is intentionally separate from HTTP/API code so its keyword
expansion and scoring can later be replaced or supplemented with embeddings.
Profiles are curated summaries of issuer products and business lines, not
investment recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class StockProfile:
    symbol: str
    name: str
    market: str
    business: str
    industries: tuple[str, ...]
    products: tuple[str, ...]
    technologies: tuple[str, ...]
    themes: tuple[str, ...]
    reasons: tuple[tuple[str, str], ...]
    source_url: str


# These concept groups expand a user's wording into related terms. Company
# name is just one searchable field; business, industry, products, technology,
# and themes carry most of the relevance score.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "data_center": (
        "데이터센터", "데이터 센터", "데이터센터 냉각", "데이터 센터 냉각",
        "데이터센터 온도관리", "데이터 센터 온도 관리", "IDC", "AIDC", "AI 데이터센터",
        "서버룸", "서버 룸", "서버 냉각", "서버 열관리", "server room", "server cooling",
        "data center", "data centre", "hyperscale", "HPC",
    ),
    "liquid_cooling": (
        "액침 냉각", "액침냉각", "침지 냉각", "침지냉각", "immersion cooling",
        "수랭식 냉각", "수랭 냉각", "수냉식", "수냉", "liquid cooling",
        "direct-to-chip", "direct to chip", "DLC", "냉각수", "coolant",
        "coolant distribution unit", "냉각수 분배 장치", "CDU", "콜드플레이트",
        "cold plate", "rear door heat exchanger", "후면도어 열교환기", "RDHx",
    ),
    "thermal_management": (
        "열관리", "열 관리", "열에너지 관리", "thermal management",
        "방열", "heat dissipation", "열 배출", "열교환기", "열 교환기",
        "heat exchanger", "라디에이터", "radiator", "온도 관리", "temperature control",
    ),
    "hvac": (
        "공조", "냉난방공조", "HVAC", "air conditioning", "공기조화",
        "항온항습", "CRAC", "CRAH", "공랭", "공랭식", "air cooling",
        "냉동", "냉매", "refrigeration", "refrigerant", "칠러", "chiller",
    ),
    "industrial_cooling": (
        "산업용 냉각", "산업 냉각", "공정 냉각", "process cooling",
        "공정 칠러", "process chiller", "반도체 칠러", "반도체 냉각",
        "클린룸", "cleanroom", "공장 공조", "산업용 냉동기", "산업용 냉각",
        "산업용 냉각 시스템", "산업용 냉각 장비", "냉각 시스템", "냉각 장비",
        "냉각 설비", "cooling system", "cooling equipment",
    ),
    "vehicle_thermal": (
        "자동차 열관리", "차량 열관리", "자동차 냉각", "파워트레인 냉각",
        "powertrain cooling", "차량 공조", "전기차 열관리", "EV thermal management",
    ),
}

COOLING_QUERY_ALIASES = {
    "냉각", "냉각 시스템", "냉각 장비", "냉각 설비", "산업용 냉각",
    "데이터센터 냉각", "데이터 센터 냉각", "서버 냉각", "액침 냉각",
    "액침냉각", "수랭식 냉각", "열관리", "thermal management", "방열",
    "냉동", "공조", "hvac", "칠러", "chiller", "냉매", "열교환기",
    "데이터센터 온도관리", "데이터 센터 온도 관리", "liquid cooling",
    "immersion cooling", "냉각기", "쿨링", "cooling",
}

# A bare cooling query is intended to find companies that sell or develop
# data-center/compute cooling products. Broader HVAC and vehicle-thermal names
# are returned only for a more specific query.
CORE_COOLING_QUERIES = {
    "냉각", "냉각 시스템", "냉각 장비", "냉각 설비", "데이터센터 냉각",
    "데이터 센터 냉각", "서버 냉각", "컴퓨터 냉각", "액침 냉각",
    "액침냉각", "수랭식 냉각", "liquid cooling", "immersion cooling", "cooling",
}


STOCK_PROFILES: tuple[StockProfile, ...] = (
    StockProfile(
        "066570", "LG전자", "KR",
        "가전과 B2B 솔루션을 영위하며 데이터센터용 HVAC와 열관리 솔루션을 확대하는 전자기업",
        ("냉난방공조", "데이터센터 인프라", "전자·가전"),
        ("수랭식·공랭식 칠러", "데이터센터 HVAC", "프리쿨링", "액체·액침 냉각 솔루션"),
        ("냉매 프리쿨링", "열관리", "에너지 최적화"),
        ("AI 데이터센터", "산업용 냉각", "HVAC", "칠러"),
        (("data_center", "AI 데이터센터용 HVAC·칠러와 냉각 솔루션 사업을 확대 중"),
         ("hvac", "데이터센터용 냉난방공조와 수랭·공랭 칠러 제품군"),
         ("liquid_cooling", "AI 데이터센터용 액체·액침 냉각 솔루션을 공개"),
         ("thermal_management", "데이터센터 열관리와 에너지 최적화 솔루션")),
        "https://www.lge.co.kr/story/newsroom/236187",
    ),
    StockProfile(
        "083450", "GST", "KR",
        "반도체·디스플레이 공정 장비를 공급하며 스크러버와 공정 온도 제어 칠러를 제조하는 기업",
        ("반도체 장비", "산업용 공정 냉각"),
        ("반도체 공정 칠러", "온도 제어 장비", "액체 냉각 기술"),
        ("공정 온도 안정화", "친환경 냉매", "액체 냉각"),
        ("산업용 냉각", "반도체 공정", "칠러", "열관리"),
        (("industrial_cooling", "반도체 공정 온도를 제어하는 칠러 제조 사업"),
         ("thermal_management", "반도체 공정의 안정적 온도 제어와 열관리 장비"),
         ("liquid_cooling", "회사가 공개한 액체 냉각 기술 연구개발")),
        "https://www.gst-in.com/en/m31.php",
    ),
    StockProfile(
        "053080", "케이엔솔", "KR",
        "반도체·디스플레이·바이오 클린룸, 배터리 드라이룸과 HVAC 시스템을 설계·시공하는 기업",
        ("산업용 클린룸", "드라이룸", "HVAC·공조"),
        ("클린룸 공조", "항온·습도 환경 제어", "FFU", "드라이룸"),
        ("기류·온습도 제어", "스마트 공조 제어"),
        ("산업용 냉각", "반도체 제조 환경", "공조", "HVAC"),
        (("industrial_cooling", "반도체·바이오 클린룸과 배터리 드라이룸의 공조·환경 제어 사업"),
         ("hvac", "산업시설용 HVAC와 온습도 제어 시스템"),
         ("thermal_management", "클린룸의 온도·습도·기류를 제어하는 환경 설비")),
        "https://www.nvhkorea.com/Down/2024_%EC%82%AC%EC%97%85%EB%B3%B4%EA%B3%A0%EC%84%9C.pdf",
    ),
    StockProfile(
        "018880", "한온시스템", "KR",
        "자동차용 HVAC, 파워트레인 냉각, 냉매 순환과 열에너지 관리 시스템을 공급하는 기업",
        ("자동차 부품", "자동차 열관리"),
        ("차량 공조", "파워트레인 냉각", "컴프레서", "냉매·유체 운송 시스템"),
        ("전기차 열관리", "열에너지 관리", "냉각수 제어"),
        ("자동차 냉각", "전기차", "HVAC", "열관리"),
        (("vehicle_thermal", "자동차·전기차 HVAC와 파워트레인 냉각 시스템 공급"),
         ("thermal_management", "차량용 열에너지 관리와 냉각수·냉매 회로 제어"),
         ("hvac", "차량 실내 공조와 냉난방 시스템")),
        "https://www.hanonsystems.com/KR",
    ),
    StockProfile(
        "011930", "신성이엔지", "KR",
        "반도체 클린룸 설비를 바탕으로 데이터센터 공간 냉각과 액침 냉각 시스템도 공급하는 기업",
        ("데이터센터 냉각", "클린룸", "산업용 공조"),
        ("데이터센터 공간 냉각", "액침 냉각 탱크·컨테이너", "Fan Wall Unit", "항온기"),
        ("데이터센터 온·습도 제어", "액침 냉각 시스템"),
        ("AI 데이터센터", "액침 냉각", "산업용 냉각", "HVAC"),
        (("data_center", "데이터센터·전산실용 공간 냉각과 에너지 절감 공조 시스템 공급"),
         ("liquid_cooling", "12U·25U·50U 탱크 및 컨테이너형 액침 냉각 솔루션 제공"),
         ("industrial_cooling", "반도체 클린룸과 산업용 냉동·공조 설비 공급"),
         ("hvac", "공조기·냉동기·항온항습기와 제습기 제품군"),
         ("thermal_management", "클린룸의 온도·습도 등 제조 환경 제어")),
        "https://shinsung.co.kr/m26.php?tab=2",
    ),
    StockProfile(
        "VRT", "Vertiv Holdings", "US",
        "데이터센터·통신 인프라의 전력과 공랭·액체 냉각 시스템을 공급하는 기업",
        ("데이터센터 인프라", "열관리", "critical facilities"),
        ("랙·룸 냉각", "인로우 냉각", "CDU", "프리쿨링 칠러", "액침 냉각"),
        ("직접 칩 액체 냉각", "열 회수", "열 제어·모니터링"),
        ("AI 데이터센터", "서버 냉각", "HVAC", "액침 냉각", "열관리"),
        (("data_center", "데이터센터의 랙·룸·시설 냉각 제품군을 보유"),
         ("liquid_cooling", "직접 칩 액체 냉각과 CDU·액침 냉각 제품을 공급"),
         ("thermal_management", "칩에서 열 회수까지 데이터센터 열관리 솔루션 제공"),
         ("hvac", "데이터센터 프리쿨링 칠러와 공랭·공조 설비")),
        "https://www.vertiv.com/en-emea/products/thermal-management/cooling/",
    ),
    StockProfile(
        "MOD", "Modine Manufacturing", "US",
        "Airedale 브랜드 등을 통해 데이터센터용 critical cooling과 산업용 열교환 솔루션을 공급하는 기업",
        ("데이터센터 냉각", "산업용 열관리", "HVAC"),
        ("데이터센터 칠러", "CDU", "액체-액체 열교환기", "공랭·액랭 시스템"),
        ("프리쿨링", "액체 냉각", "열교환"),
        ("AI 데이터센터", "산업용 냉각", "칠러", "열교환기"),
        (("data_center", "Airedale by Modine이 데이터센터용 critical cooling 시스템 공급"),
         ("liquid_cooling", "데이터센터 액체 냉각용 CDU와 하이브리드 냉각 제품"),
         ("industrial_cooling", "산업용 열교환·공랭 및 액랭 시스템"),
         ("hvac", "데이터센터용 칠러와 프리쿨링 공조 제품")),
        "https://investors.modine.com/news/news-details/2024/Airedale-by-Modine-Launches-1MW-Cooling-Distribution-Unit/default.aspx",
    ),
    StockProfile(
        "NVT", "nVent Electric", "US",
        "전기 인프라와 데이터센터 랙·액체 냉각·열관리 장비를 공급하는 기업",
        ("데이터센터 인프라", "전기·랙 시스템", "열관리"),
        ("CDU", "랙 칠러", "후면도어 열교환기", "액체-공기 열 배출 장치"),
        ("직접 칩 액체 냉각", "랙 단위 열관리", "냉각수 분배"),
        ("AI 데이터센터", "서버 냉각", "액체 냉각", "열교환기"),
        (("data_center", "고밀도·하이퍼스케일 데이터센터 냉각 인프라 제공"),
         ("liquid_cooling", "CDU·직접 칩 액체 냉각·후면도어 열교환기 제품군"),
         ("thermal_management", "랙과 서버 환경의 열관리 및 냉각수 분배 솔루션")),
        "https://www.nvent.com/en-us/data-solutions/liquid-cooling",
    ),
    StockProfile(
        "SMCI", "Super Micro Computer", "US",
        "AI·HPC 서버와 랙 시스템, 데이터센터 단위의 액체 냉각 인프라를 설계·공급하는 기업",
        ("서버·스토리지", "AI 데이터센터", "HPC"),
        ("직접 칩 냉각", "액침 냉각", "냉각수 분배 장치", "냉각 타워", "후면도어 열교환기"),
        ("랙 단위 직접 액체 냉각", "AI 서버 열관리", "폐쇄형 냉각 루프"),
        ("서버 냉각", "AI 데이터센터", "액침 냉각", "수랭식 냉각"),
        (("data_center", "AI/HPC 데이터센터용 서버와 랙 단위 냉각 시스템을 함께 공급"),
         ("liquid_cooling", "직접 칩·액침 냉각과 CDU 등 액체 냉각 솔루션 제공"),
         ("thermal_management", "서버·랙의 고밀도 열을 처리하는 통합 열관리")),
        "https://www.supermicro.com/en/solutions/liquid-cooling",
    ),
    StockProfile(
        "CARR", "Carrier Global", "US",
        "상업용 HVAC·냉동 솔루션과 데이터센터 열관리 시스템을 공급하는 기업",
        ("HVAC", "데이터센터 인프라", "냉동·공조"),
        ("데이터센터 칠러", "CDU", "공기조화기", "직접 칩 액체 냉각"),
        ("열관리 통합 제어", "에너지 최적화", "냉각수 분배"),
        ("데이터센터 냉각", "산업용 냉각", "칠러", "HVAC"),
        (("data_center", "데이터센터 전용 HVAC·열관리 포트폴리오를 운영"),
         ("liquid_cooling", "고밀도 데이터센터용 CDU와 직접 칩 액체 냉각 솔루션"),
         ("hvac", "공랭·수랭 칠러와 공기조화 설비를 공급"),
         ("thermal_management", "칩에서 칠러까지 데이터센터 열관리 시스템 제공")),
        "https://www.carrier.com/us/en/commercial/data-centers/",
    ),
    StockProfile(
        "TT", "Trane Technologies", "US",
        "상업용 HVAC와 데이터센터 등 mission-critical 시설의 냉각·열관리 솔루션을 공급하는 기업",
        ("HVAC", "데이터센터 냉각", "상업용 공조"),
        ("칠러", "공조 시스템", "열관리 제어"),
        ("고효율 냉동", "시설 열관리", "공기 처리"),
        ("데이터센터", "HVAC", "산업용 냉각", "칠러"),
        (("data_center", "데이터센터와 mission-critical 시설의 냉각 솔루션 공급"),
         ("hvac", "상업용 HVAC·칠러·공조 시스템 사업"),
         ("thermal_management", "시설의 열환경 제어 및 에너지 효율 솔루션")),
        "https://www.trane.com/commercial/north-america/us/en/industries/data-centers.html",
    ),
    StockProfile(
        "JCI", "Johnson Controls", "US",
        "건물 기술·HVAC·빌딩 관리 시스템과 AI 데이터센터 냉각 인프라를 공급하는 기업",
        ("데이터센터 인프라", "HVAC", "빌딩 자동화"),
        ("공랭·수랭 칠러", "흡수식 냉동기", "직접 칩 액체 냉각 설계"),
        ("건물 관리 시스템", "고효율 냉각 제어", "열관리"),
        ("AI 데이터센터", "칠러", "HVAC", "열관리"),
        (("data_center", "AI 데이터센터용 냉각 시스템과 통합 인프라 솔루션 제공"),
         ("hvac", "데이터센터용 공랭·수랭 칠러와 흡수식 냉동 기술"),
         ("thermal_management", "건물 제어와 데이터센터 열관리 시스템 통합")),
        "https://www.johnsoncontrols.com/industries/data-centers/reference-designs",
    ),
)

CONCEPT_LABELS = {
    "data_center": "데이터센터 냉각",
    "liquid_cooling": "액체·액침 냉각",
    "thermal_management": "열관리·방열",
    "hvac": "냉동·공조(HVAC)",
    "industrial_cooling": "산업용·공정 냉각",
    "vehicle_thermal": "자동차 열관리",
}

CONCEPT_IMPORTANCE = {
    "data_center": 1.0,
    "liquid_cooling": 1.0,
    "thermal_management": 0.85,
    "hvac": 0.9,
    "industrial_cooling": 0.95,
    # Vehicle thermal systems are genuinely cooling-related, but are an
    # adjacent market for a general data-center/industrial cooling query.
    "vehicle_thermal": 0.45,
}


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", unicodedata.normalize("NFKC", value).casefold())


def expand_keyword(keyword: str) -> dict[str, object]:
    """Expand a query into the original input and related industry concepts."""

    original = unicodedata.normalize("NFKC", keyword).strip()
    normalized = _normalize(original)
    terms: dict[str, str] = {}
    concepts: set[str] = set()

    for concept, aliases in CONCEPTS.items():
        if any(_normalize(alias) in normalized or normalized in _normalize(alias) for alias in aliases):
            concepts.add(concept)
            for alias in aliases:
                terms.setdefault(_normalize(alias), alias)

    if normalized in {_normalize(alias) for alias in COOLING_QUERY_ALIASES}:
        concepts.update(concept for concept in CONCEPTS if concept != "vehicle_thermal")
        for aliases in CONCEPTS.values():
            for alias in aliases:
                terms.setdefault(_normalize(alias), alias)

    if normalized:
        terms.setdefault(normalized, original)
    ordered_terms = [terms[key] for key in sorted(terms, key=lambda key: (-len(key), key))]
    return {"original": original, "terms": ordered_terms, "concepts": sorted(concepts)}


def search_related_stocks(keyword: str, market: str = "KR", limit: int = 20) -> list[dict[str, object]]:
    """Rank listed-company profiles by matches across business and product data."""

    if market not in {"KR", "US"}:
        raise ValueError("market은 KR 또는 US여야 합니다.")
    if not keyword.strip():
        return []

    expansion = expand_keyword(keyword)
    terms = [(term, _normalize(term)) for term in expansion["terms"] if _normalize(term)]
    query = _normalize(keyword)
    core_cooling_query = query in {_normalize(term) for term in CORE_COOLING_QUERIES}
    results: list[dict[str, object]] = []

    for profile in STOCK_PROFILES:
        if profile.market != market:
            continue
        profile_reason_concepts = {concept for concept, _ in profile.reasons}
        if core_cooling_query and not profile_reason_concepts.intersection({"data_center", "liquid_cooling"}):
            continue
        fields = {
            "name": (profile.name, 2),
            "business": (profile.business, 5),
            "industries": (" ".join(profile.industries), 4),
            "products": (" ".join(profile.products), 6),
            "technologies": (" ".join(profile.technologies), 5),
            "themes": (" ".join(profile.themes), 2),
        }
        normalized_fields = {name: (_normalize(value), weight) for name, (value, weight) in fields.items()}
        matched_terms: list[str] = []
        matched_concepts: set[str] = set()
        concept_scores: dict[str, int] = {}
        direct_score = 0

        for term, normalized_term in terms:
            found_weight = max(
                (weight for field_text, weight in normalized_fields.values() if normalized_term in field_text),
                default=0,
            )
            if not found_weight:
                continue
            matched_terms.append(term)
            term_concepts: set[str] = set()
            for concept, aliases in CONCEPTS.items():
                if any(_normalize(alias) == normalized_term for alias in aliases):
                    term_concepts.add(concept)
            if term_concepts:
                matched_concepts.update(term_concepts)
                for concept in term_concepts:
                    weighted_score = round(found_weight * CONCEPT_IMPORTANCE[concept])
                    concept_scores[concept] = max(concept_scores.get(concept, 0), weighted_score)
            else:
                direct_score = max(direct_score, found_weight)

        # Direct mentions of the input carry extra weight, but a name-only
        # match cannot outrank a well-supported product/business match.
        if query and query in normalized_fields["name"][0]:
            direct_score = max(direct_score, 4)
        score = sum(concept_scores.values()) + direct_score
        if score == 0:
            continue

        reason_by_concept = dict(profile.reasons)
        reason_candidates = [reason_by_concept[key] for key in sorted(matched_concepts) if key in reason_by_concept]
        reason = " · ".join(reason_candidates[:2]) or f"회사 사업·제품 정보에서 '{keyword}' 연관 용어 확인"
        related_industries = [
            CONCEPT_LABELS[concept]
            for concept in sorted(matched_concepts, key=lambda value: (-concept_scores.get(value, 0), value))
            if concept in CONCEPT_LABELS
        ][:2]
        if not related_industries:
            related_industries = list(profile.industries[:2])

        relevance_score = min(100, round(score / (len(CONCEPTS) * 6 + 8) * 100))
        relevance_label = "높음" if relevance_score >= 55 else "보통" if relevance_score >= 25 else "참고"
        results.append({
            "symbol": profile.symbol,
            "name": profile.name,
            "market": profile.market,
            "relevance_score": relevance_score,
            "relevance_label": relevance_label,
            "related_industries": related_industries,
            "matched_terms": matched_terms[:8],
            "reason": reason,
            "business_summary": profile.business,
            "source_url": profile.source_url,
        })

    results.sort(key=lambda item: (int(item["relevance_score"]), len(item["matched_terms"])), reverse=True)
    return results[:max(1, limit)]


# The Toss stock-info API looks up stocks by ticker, not by a company-name
# query. Keep a small, easy-to-extend alias index for direct company searches.
# Profiles above are also searched by name and symbol.
DIRECT_STOCKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("105560", "KB금융", "KR", ("KB금융", "KB 금융", "KB Financial", "KBFG")),
    ("005930", "삼성전자", "KR", ("삼성전자", "Samsung Electronics")),
    ("000660", "SK하이닉스", "KR", ("SK하이닉스", "하이닉스", "SK Hynix")),
    ("000270", "기아", "KR", ("기아", "기아자동차", "Kia")),
    ("005380", "현대차", "KR", ("현대차", "현대자동차", "Hyundai Motor")),
    ("035420", "NAVER", "KR", ("네이버", "NAVER")),
    ("035720", "카카오", "KR", ("카카오", "Kakao")),
    ("068270", "셀트리온", "KR", ("셀트리온", "Celltrion")),
    ("000990", "DB하이텍", "KR", ("DB하이텍", "DB Hitek")),
    ("066570", "LG전자", "KR", ("LG전자", "LG Electronics")),
    ("AAPL", "Apple", "US", ("애플", "Apple", "Apple Inc.")),
    ("MSFT", "Microsoft", "US", ("마이크로소프트", "Microsoft")),
    ("NVDA", "NVIDIA", "US", ("엔비디아", "NVIDIA")),
    ("AMZN", "Amazon", "US", ("아마존", "Amazon")),
    ("GOOGL", "Alphabet", "US", ("알파벳", "구글", "Google", "Alphabet")),
    ("META", "Meta Platforms", "US", ("메타", "Meta", "Facebook")),
    ("TSLA", "Tesla", "US", ("테슬라", "Tesla")),
)


def search_direct_stocks(query: str, market: str = "KR", limit: int = 5) -> list[dict[str, object]]:
    """Find a company by its ticker or known company-name aliases."""

    if market not in {"KR", "US"}:
        raise ValueError("market은 KR 또는 US여야 합니다.")
    normalized_query = _normalize(query)
    if not normalized_query:
        return []

    candidates: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for symbol, name, stock_market, aliases in DIRECT_STOCKS:
        candidates[symbol] = (name, stock_market, aliases)
    for profile in STOCK_PROFILES:
        candidates.setdefault(profile.symbol, (profile.name, profile.market, (profile.name, profile.symbol)))

    matches: list[dict[str, object]] = []
    for symbol, (name, stock_market, aliases) in candidates.items():
        searchable = {_normalize(symbol), _normalize(name), *(_normalize(alias) for alias in aliases)}
        exact = normalized_query in searchable
        partial = any(
            len(normalized_query) >= 2 and (normalized_query in value or value in normalized_query)
            for value in searchable if value
        )
        # Allow any valid ticker entered directly, even when it is not in the
        # small name-alias index. The caller verifies it against Toss.
        ticker_query = query.strip().upper()
        direct_ticker = ticker_query == symbol
        if exact or partial or direct_ticker:
            matches.append({
                "symbol": symbol,
                "name": name,
                "market": stock_market,
                "relevance_score": 100 if exact or direct_ticker else 85,
                "relevance_label": "종목 일치" if exact or direct_ticker else "이름 일치",
                "related_industries": ["개별 종목"],
                "matched_terms": [query.strip()],
                "reason": f"회사명 또는 종목코드가 검색어 '{query.strip()}'와 일치합니다.",
                "business_summary": "회사명 또는 티커로 직접 검색한 종목입니다.",
                "source_url": None,
                "match_type": "direct",
            })

    # A six-digit KRX code or a US ticker can be checked by Toss even when it
    # is not in the local aliases. Company-name search uses the curated aliases.
    ticker_query = query.strip().upper()
    if not matches and (
        (market == "KR" and re.fullmatch(r"\d{6}", ticker_query))
        or (market == "US" and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker_query))
    ):
        matches.append({
            "symbol": ticker_query,
            "name": ticker_query,
            "market": market,
            "relevance_score": 100,
            "relevance_label": "종목코드 검색",
            "related_industries": ["개별 종목"],
            "matched_terms": [query.strip()],
            "reason": f"종목코드 '{ticker_query}'를 조회합니다.",
            "business_summary": "티커 조회 결과입니다.",
            "source_url": None,
            "match_type": "direct",
            "verify_with_broker": True,
        })

    matches.sort(key=lambda item: int(item["relevance_score"]), reverse=True)
    return matches[:max(1, limit)]
