import unittest

from fleetpulse.analysis import judge

A = {"min_canary_requests": 100, "max_error_ratio": 0.01, "max_error_ratio_vs_stable": 2.0,
     "max_p95_vs_stable": 1.5, "p95_slack_s": 0.05, "early_fail_min_requests": 20, "early_fail_error_ratio": 0.5}


def stats(requests, error_ratio=0.0, p95=0.1):
    return {"requests": requests, "errors": round(requests * error_ratio), "error_ratio": error_ratio, "p95_s": p95}


class JudgeTest(unittest.TestCase):
    def test_waits_until_enough_requests(self):
        self.assertEqual(judge(stats(10), stats(500), A, final=False)[0], "wait")

    def test_too_few_requests_at_the_end_fails(self):
        self.assertEqual(judge(stats(10), stats(500), A, final=True)[0], "fail")

    def test_errors_above_ceiling_fail_early(self):
        self.assertEqual(judge(stats(200, 0.05), stats(2000), A, final=False)[0], "fail")

    def test_shared_outage_is_not_blamed_on_canary(self):
        # Stable also fails 4%: the canary at 5% is within 2x, so it is not the new version's fault.
        self.assertEqual(judge(stats(200, 0.05), stats(2000, 0.04), A, final=True)[0], "pass")

    def test_slow_canary_fails(self):
        self.assertEqual(judge(stats(200, 0, 0.9), stats(2000, 0, 0.2), A, final=False)[0], "fail")

    def test_small_latency_noise_passes(self):
        # 0.1s stable -> limit 0.2s; 0.18s is noise on a fast endpoint.
        self.assertEqual(judge(stats(200, 0, 0.18), stats(2000, 0, 0.1), A, final=True)[0], "pass")

    def test_missing_latency_fails_the_final_check(self):
        # A vanished histogram must not switch the latency gate off.
        self.assertEqual(judge(stats(200, 0, None), stats(2000, 0, 0.1), A, final=True)[0], "fail")

    def test_missing_latency_is_fine_while_waiting(self):
        self.assertEqual(judge(stats(200, 0, None), stats(2000, 0, 0.1), A, final=False)[0], "wait")

    def test_canary_failing_most_requests_leaves_before_the_full_sample(self):
        self.assertEqual(judge(stats(25, 0.8), stats(300), A, final=False)[0], "fail")


if __name__ == "__main__":
    unittest.main()
