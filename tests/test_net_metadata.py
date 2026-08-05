import json
import unittest
from pathlib import Path


class NetMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(
            (Path(__file__).parent / "analysis" / "net_metadata.json").read_text(encoding="utf-8")
        )
        cls.methods = {entry["message_id"]: entry for entry in cls.data["methods"]}

    def test_companion_message_ids_and_types(self):
        self.assertEqual(self.methods[1502]["method"], "net_dating.dating")
        self.assertEqual(self.methods[1504]["types"], ["int", "int", "list<ItemShowPOD>", "list<int>"])
        self.assertEqual(self.methods[1907]["types"], ["int", "int", "int", "bool", "int"])
        self.assertEqual(self.methods[1914]["types"], ["int", "int", "SoulOathPOD"])

    def test_required_pod_shapes(self):
        self.assertEqual(
            self.data["pod_types"]["SoulOathPOD"],
            {"activation": "bool", "countData": "map<int|int>", "dateData": "map<int|long>"},
        )
        self.assertEqual(
            self.data["pod_types"]["ItemShowPOD"],
            {"cid": "int", "num": "int", "tag": "int"},
        )


if __name__ == "__main__":
    unittest.main()
