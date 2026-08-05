import json
import unittest
from pathlib import Path


class ProtocolRegistryTests(unittest.TestCase):
    def test_registry_has_no_guessed_ids(self):
        registry = json.loads(
            (Path(__file__).parent / "analysis" / "protocol_registry.json").read_text(encoding="utf-8")
        )
        self.assertTrue(registry["entries"])
        self.assertTrue(any(entry["status"] == "unmapped" for entry in registry["entries"]))
        self.assertEqual(
            {
                (entry["namespace"], entry["function"]): entry["message_id"]
                for entry in registry["entries"]
                if entry["status"] == "confirmed"
            },
            {
                ("net_dating", "datingResult"): 1503,
                ("net_dating", "notifyDatingEnd"): 1504,
                ("net_dating", "notifyDating"): 1505,
                ("net_girl", "giveGiftResult_delegate"): 1907,
                ("net_girl", "fondleResult"): 1909,
                ("net_girl", "notifyFondleNumRecovery"): 1910,
                ("net_girl", "connectiveResult"): 1913,
                ("net_girl", "getSoulOathResult"): 1914,
            },
        )


if __name__ == "__main__":
    unittest.main()
