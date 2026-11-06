import unittest

from tradeyi.pnl import position_pnl, weighted_average_cost


class PositionPnlTest(unittest.TestCase):
    def test_long_gain(self):
        self.assertAlmostEqual(position_pnl(100.0, 110.0, 5), 50.0)

    def test_short_gain_when_price_falls(self):
        self.assertAlmostEqual(position_pnl(100.0, 90.0, 5, side="short"), 50.0)

    def test_short_loss_when_price_rises(self):
        self.assertAlmostEqual(position_pnl(100.0, 110.0, 5, side="short"), -50.0)

    def test_short_pnl_is_negated_long_pnl(self):
        for entry, exit_price in ((100.0, 90.0), (100.0, 110.0)):
            self.assertAlmostEqual(
                position_pnl(entry, exit_price, 7, side="short"),
                -position_pnl(entry, exit_price, 7),
            )


class WeightedAverageCostTest(unittest.TestCase):
    def test_weighted_by_quantity(self):
        self.assertAlmostEqual(weighted_average_cost([(100.0, 10), (200.0, 30)]), 175.0)

    def test_single_fill(self):
        self.assertAlmostEqual(weighted_average_cost([(42.5, 3)]), 42.5)

    def test_quantity_weighting_with_three_fills(self):
        self.assertAlmostEqual(
            weighted_average_cost([(10.0, 1), (20.0, 1), (30.0, 4)]), 25.0
        )


if __name__ == "__main__":
    unittest.main()
