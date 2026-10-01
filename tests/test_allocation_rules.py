import unittest
from decimal import Decimal

from auto_trader.allocation_rules import liquidity_price_weights


class AllocationRuleTests(unittest.TestCase):
    def test_cash_ratio_budget_is_split_by_liquidity_after_one_share_reserve(self):
        weights = liquidity_price_weights(
            {"066570": Decimal("200000"), "083450": Decimal("50000")},
            {"066570": 1, "083450": 80},
            Decimal("4000000"),
            fee_rate=Decimal("0.00015"), slippage_rate=Decimal("0"),
        )
        self.assertEqual(sum(weights.values()), Decimal("100"))
        self.assertGreater(weights["066570"], weights["083450"])
        self.assertGreater(Decimal("4000000") * weights["066570"] / 100, Decimal("200030"))
        self.assertGreater(Decimal("4000000") * weights["083450"] / 100, Decimal("50008"))

    def test_unaffordable_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "1주씩"):
            liquidity_price_weights(
                {"066570": Decimal("3000000"), "083450": Decimal("2000000")},
                {}, Decimal("4000000"),
                fee_rate=Decimal("0.00015"), slippage_rate=Decimal("0"),
            )


if __name__ == "__main__":
    unittest.main()
