import unittest

from fleetpulse_server.sources import budget_in_units


class BudgetInUnitsTest(unittest.TestCase):
    def test_probes_become_minutes_of_downtime(self):
        # 99.5% over 30 days allows 216 min; 1.15% of 5.6 h of probes failed = about 3.9 min down.
        allowed, used = budget_in_units(0.995, 0.0115, 5.6, "minutes", None)
        self.assertAlmostEqual(allowed, 216)
        self.assertAlmostEqual(used, 3.864, places=3)

    def test_requests_are_judged_against_the_whole_period(self):
        # 10,000 requests in 72 h -> about 100,000 in 30 days; 2% may fail = 2,000. 0.5% failed so far = 50.
        allowed, used = budget_in_units(0.98, 0.005, 72, "requests", 10_000)
        self.assertAlmostEqual(allowed, 2000)
        self.assertAlmostEqual(used, 50)

    def test_a_full_period_is_not_projected(self):
        allowed, _ = budget_in_units(0.98, 0.005, 720, "requests", 100_000)
        self.assertAlmostEqual(allowed, 2000)

    def test_unknown_stays_unknown(self):
        self.assertEqual(budget_in_units(0.995, None, 5.6, "minutes", None), (None, None))
        self.assertEqual(budget_in_units(0.995, 0.01, None, "minutes", None), (None, None))
        self.assertEqual(budget_in_units(0.98, 0.01, 10, "requests", None), (None, None))


if __name__ == "__main__":
    unittest.main()
