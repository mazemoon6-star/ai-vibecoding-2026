import unittest

from auto_trader.related_stock_search import expand_keyword, search_direct_stocks, search_related_stocks


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


if __name__ == "__main__":
    unittest.main()
