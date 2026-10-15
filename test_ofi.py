"""Unit tests for ofi.py. Run with: python -m unittest -v test_ofi.py"""

import math
import unittest

from engine.signal_layer.momentum.ofi import compute_ofi


class ComputeOfiTests(unittest.TestCase):
    def test_balanced_positive_depth_is_valid_zero(self):
        result = compute_ofi(1000, 1000)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["ofi"], 0.0)
        self.assertEqual(result["interpretation"], "balanced order flow")

    def test_buy_pressure(self):
        result = compute_ofi(1500, 500)
        self.assertEqual(result["ofi"], 0.5)
        self.assertEqual(result["interpretation"], "buy pressure dominant")

    def test_sell_pressure(self):
        result = compute_ofi(500, 1500)
        self.assertEqual(result["ofi"], -0.5)
        self.assertEqual(result["interpretation"], "sell pressure dominant")

    def test_all_buy_or_all_sell(self):
        self.assertEqual(compute_ofi(1000, 0)["ofi"], 1.0)
        self.assertEqual(compute_ofi(0, 1000)["ofi"], -1.0)

    def test_zero_depth_is_not_balanced(self):
        result = compute_ofi(0, 0)
        self.assertEqual(result["status"], "no_depth")
        self.assertIsNone(result["ofi"])
        self.assertIsNone(result["interpretation"])

    def test_missing_quantity(self):
        for buy, sell in ((None, 100), (100, None), (None, None)):
            with self.subTest(buy=buy, sell=sell):
                result = compute_ofi(buy, sell)
                self.assertEqual(result["status"], "missing")
                self.assertIsNone(result["ofi"])

    def test_negative_quantity_rejected(self):
        result = compute_ofi(-1, 100)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["ofi"])

    def test_malformed_quantity_rejected(self):
        for value in ("100", object(), True):
            with self.subTest(value=repr(value)):
                result = compute_ofi(value, 100)
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["ofi"])

    def test_non_finite_quantity_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                result = compute_ofi(value, 100)
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["ofi"])

    def test_normalized_result_is_in_range(self):
        for buy, sell in ((1, 999), (999, 1), (25, 75), (75, 25)):
            with self.subTest(buy=buy, sell=sell):
                result = compute_ofi(buy, sell)
                self.assertGreaterEqual(result["ofi"], -1.0)
                self.assertLessEqual(result["ofi"], 1.0)


if __name__ == "__main__":
    unittest.main()

