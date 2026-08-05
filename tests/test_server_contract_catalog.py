import json
import unittest
from pathlib import Path


class ServerContractCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(
            (Path(__file__).parent / "analysis" / "server_contract_catalog.json").read_text(encoding="utf-8")
        )
        cls.methods = {row["message_id"]: row for row in data["methods"]}

    def test_known_dynamic_and_replay_requests(self):
        self.assertEqual(self.methods[1502]["implementation"], "dynamic")
        self.assertEqual(self.methods[1906]["implementation"], "dynamic")
        self.assertEqual(self.methods[2602]["implementation"], "dynamic")
        self.assertEqual(self.methods[3907]["implementation"], "dynamic")
        self.assertEqual(self.methods[2606]["implementation"], "dynamic")
        self.assertEqual(self.methods[2402]["implementation"], "dynamic")
        self.assertEqual(self.methods[4302]["implementation"], "dynamic")
        self.assertEqual(self.methods[4310]["implementation"], "dynamic")
        self.assertEqual(self.methods[4502]["implementation"], "dynamic")
        self.assertEqual(self.methods[4504]["implementation"], "dynamic")
        self.assertEqual(self.methods[2402]["implementation"], "dynamic")
        self.assertEqual(self.methods[3704]["implementation"], "dynamic")
        self.assertEqual(self.methods[3707]["implementation"], "dynamic")
        self.assertEqual(self.methods[3708]["implementation"], "dynamic")
        self.assertEqual(self.methods[3711]["implementation"], "dynamic")
        self.assertEqual(self.methods[3712]["implementation"], "dynamic")
        self.assertEqual(self.methods[3715]["implementation"], "dynamic")
        self.assertEqual(self.methods[3908]["implementation"], "dynamic")
        self.assertEqual(self.methods[3909]["implementation"], "dynamic")
        self.assertEqual(self.methods[3940]["implementation"], "dynamic")
        self.assertEqual(self.methods[3945]["implementation"], "dynamic")
        self.assertEqual(self.methods[3954]["implementation"], "dynamic")
        self.assertEqual(self.methods[3960]["implementation"], "dynamic")
        self.assertEqual(self.methods[3962]["implementation"], "dynamic")
        self.assertEqual(self.methods[3963]["implementation"], "dynamic")
        self.assertEqual(self.methods[4008]["implementation"], "dynamic")
        self.assertEqual(self.methods[7404]["implementation"], "dynamic")
        self.assertEqual(self.methods[7505]["implementation"], "dynamic")
        self.assertEqual(self.methods[100921]["implementation"], "dynamic")

    def test_request_result_pair_uses_metadata(self):
        self.assertEqual(self.methods[1911]["result_message_id"], 1913)
        self.assertEqual(
            self.methods[1911]["result_types"],
            ["int", "int", "int", "list<ItemShowPOD>"],
        )


if __name__ == "__main__":
    unittest.main()
