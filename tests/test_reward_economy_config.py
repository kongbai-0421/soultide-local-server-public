import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class RewardEconomyConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "analysis" / "module_config.json").read_text(encoding="utf-8"))

    def test_reward_tables_are_extracted_with_official_rows(self):
        rewards = self.config["rewards"]
        expected = {
            "DropLibMazeTable": 1752,
            "DualTeamExploreEXBossRewardTable": 45,
            "GuildChallengeChestRewardTable": 10,
            "RPGMazeExBossRewardTable": 15,
            "WorldBossRewardTable": 15,
        }
        for table, minimum in expected.items():
            self.assertGreaterEqual(len(rewards[table]), minimum)
        self.assertEqual(rewards["DropLibMazeFilterTable"], {})

    def test_economy_tables_include_purchase_and_exchange_records(self):
        economy = self.config["economy"]
        self.assertGreaterEqual(len(economy["GoodsTable"]), 100)
        self.assertGreaterEqual(len(economy["ExchangeTable"]), 50)
        self.assertGreaterEqual(len(economy["GiftTable"]), 100)
        self.assertGreaterEqual(len(self.config["mall"]["PayTable"]), 500)
        self.assertGreaterEqual(len(self.config["mall"]["MallTable"]), 3000)


if __name__ == "__main__":
    unittest.main()
