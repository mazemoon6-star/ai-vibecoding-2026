import unittest

from auto_trader.related_stock_search import STOCK_PROFILES, expand_keyword, search_direct_stocks, search_related_stocks


class RelatedStockSearchTests(unittest.TestCase):
    def test_cooling_expands_into_industry_and_technology_terms(self) -> None:
        expanded = expand_keyword("냉각")
        normalized_terms = {term.casefold() for term in expanded["terms"]}

        self.assertIn("냉각", normalized_terms)
        self.assertIn("액침 냉각", normalized_terms)
        self.assertIn("thermal management", normalized_terms)
        self.assertIn("데이터센터 냉각", normalized_terms)
        self.assertIn("서버 냉각", normalized_terms)
        self.assertIn("산업용 냉각", normalized_terms)
        self.assertIn("chiller", normalized_terms)
        self.assertIn("열교환기", normalized_terms)

    def test_cooling_search_uses_company_business_profiles(self) -> None:
        results = search_related_stocks("냉각", "KR")
        symbols = {item["symbol"] for item in results}

        self.assertTrue({"066570", "083450", "011930"}.issubset(symbols))
        self.assertFalse({"018880", "053080", "000990"}.intersection(symbols))
        self.assertTrue(all(item["reason"] and item["related_industries"] for item in results))
        self.assertTrue(all(len(item["related_industries"]) <= 2 for item in results))
        scores = [item["relevance_score"] for item in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        by_symbol = {item["symbol"]: item for item in results}
        self.assertIn("AI 데이터센터용", by_symbol["066570"]["reason"])

    def test_vehicle_cooling_is_returned_for_specific_vehicle_query(self) -> None:
        results = search_related_stocks("자동차 냉각", "KR")

        self.assertIn("018880", {item["symbol"] for item in results})

    def test_liquid_cooling_matches_us_products_and_returns_reason(self) -> None:
        results = search_related_stocks("액침 냉각", "US")
        by_symbol = {item["symbol"]: item for item in results}

        self.assertTrue({"VRT", "SMCI", "NVT"}.issubset(by_symbol))
        self.assertIn("냉각", by_symbol["VRT"]["reason"])
        self.assertTrue(by_symbol["SMCI"]["source_url"].startswith("https://"))

    def test_unknown_query_returns_no_matches(self) -> None:
        self.assertEqual(search_related_stocks("qzxv-unlisted-topic", "KR"), [])

    def test_direct_company_search_finds_kb_financial_by_name_or_ticker(self) -> None:
        by_name = search_direct_stocks("KB금융", "KR")
        by_ticker = search_direct_stocks("105560", "KR")

        self.assertEqual(by_name[0]["symbol"], "105560")
        self.assertEqual(by_name[0]["name"], "KB금융")
        self.assertEqual(by_ticker[0]["name"], "KB금융")
        self.assertEqual(by_name[0]["match_type"], "direct")

    def test_direct_company_search_finds_apple_even_if_market_dropdown_is_kr(self) -> None:
        results = search_direct_stocks("애플", "KR")

        self.assertEqual(results[0]["symbol"], "AAPL")
        self.assertEqual(results[0]["market"], "US")

    def test_unknown_ticker_is_returned_for_broker_verification(self) -> None:
        results = search_direct_stocks("999999", "KR")

        self.assertEqual(results[0]["symbol"], "999999")
        self.assertTrue(results[0]["verify_with_broker"])

    def test_added_domestic_sectors_include_supported_auto_discovery_candidates(self) -> None:
        sectors = {
            "의약": {"207940", "068270", "000100", "128940"},
            "우주": {"012450", "047810", "272210", "099320"},
            "방산": {"012450", "047810", "272210"},
            "로봇": {"454910", "277810"},
            "반도체": {"005930", "000660", "000990", "083450"},
            "AI": {"035420", "035720", "005930", "000660"},
            "전력": {"267260", "010120", "298040"},
            "원전": {"034020"},
            "2차전지": {"373220", "006400"},
            "신재생": {"009830"},
            "조선": {"042660", "010140"},
        }
        for keyword, expected in sectors.items():
            with self.subTest(keyword=keyword):
                results = search_related_stocks(keyword, "KR", limit=100)
                admitted = {item["symbol"] for item in results if item["relevance_score"] >= 25}
                self.assertTrue(expected.issubset(admitted), (keyword, admitted))
                self.assertTrue(all(item["market"] == "KR" and item["source_url"] for item in results))
                self.assertEqual(search_direct_stocks(keyword, "KR"), [])

    def test_sector_synonyms_and_us_candidates_are_searchable(self) -> None:
        for keyword, market, expected in (
            ("바이오", "KR", "068270"), ("제약", "US", "LLY"),
            ("biotech", "US", "AMGN"), ("우주항공", "KR", "047810"),
            ("위성", "US", "ASTS"), ("space", "US", "RKLB"),
            ("robotics", "US", "TER"), ("AI", "US", "NVDA"),
            ("HBM", "KR", "000660"), ("SMR", "KR", "034020"),
            ("SMR", "US", "SMR"), ("ESS", "KR", "006400"),
            ("태양광", "US", "FSLR"), ("전력", "US", "ETN"),
        ):
            with self.subTest(keyword=keyword, market=market):
                self.assertIn(expected, {item["symbol"] for item in search_related_stocks(keyword, market)})
                self.assertEqual(search_direct_stocks(keyword, market), [])

    def test_cooling_does_not_expand_into_unrelated_new_sectors(self) -> None:
        concepts = set(expand_keyword("냉각")["concepts"])
        self.assertFalse(concepts & {"pharma", "space", "ai", "robotics", "nuclear"})
        self.assertEqual({item["symbol"] for item in search_related_stocks("냉각", "KR")},
                         {"066570", "083450", "011930"})

    def test_exact_sectors_and_short_acronyms_do_not_leak_into_adjacent_industries(self) -> None:
        self.assertEqual(expand_keyword("반도체")["concepts"], ["semiconductor"])
        self.assertEqual(expand_keyword("AI")["concepts"], ["ai"])
        self.assertNotIn("ai", expand_keyword("air cooling")["concepts"])
        self.assertFalse({"035420", "035720"} & {
            item["symbol"] for item in search_related_stocks("반도체", "KR")
        })
        self.assertNotIn("053080", {item["symbol"] for item in search_related_stocks("바이오", "KR")})

    def test_catalog_coverage_does_not_dilute_existing_relevance_scores(self) -> None:
        from unittest.mock import patch
        from auto_trader import related_stock_search as search

        before = search_related_stocks("의약", "KR")
        with patch.dict(search.CONCEPTS, {"unrelated_test": ("qzxv-topic",)}):
            after = search_related_stocks("의약", "KR")
        self.assertEqual(before, after)
        self.assertEqual(len({(item.market, item.symbol) for item in STOCK_PROFILES}), len(STOCK_PROFILES))


if __name__ == "__main__":
    unittest.main()
