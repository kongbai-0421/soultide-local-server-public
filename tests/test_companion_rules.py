import json
import unittest
from pathlib import Path


class CompanionRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(
            (Path(__file__).parent / "analysis" / "companion_rules.json").read_text(encoding="utf-8")
        )

    def test_required_tables_are_extracted(self):
        counts = self.data["counts"]
        self.assertGreaterEqual(counts["dating_events"], 500)
        self.assertGreaterEqual(counts["dating_choices"], 200)
        self.assertGreaterEqual(counts["gifts"], 50)
        self.assertGreaterEqual(counts["soul_marry"], 10)
        self.assertGreaterEqual(counts["soul_favor"], 1000)
        self.assertGreaterEqual(
            counts["soul_action_groups_1"] + counts["soul_action_groups_2"], 1000
        )

    def test_first_dating_event_has_real_cost_reward_and_dialog(self):
        event = self.data["rules"]["dating_events"]["1001001"]
        self.assertEqual(event["Dialog"], 1110200000)
        self.assertEqual(event["Cost"], [10901, 2])
        self.assertEqual(event["Reward"], [10601, 100, 20201, 6])


if __name__ == "__main__":
    unittest.main()
