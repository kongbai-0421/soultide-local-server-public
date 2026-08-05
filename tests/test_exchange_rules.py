import gc
import json
import os
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["SOULTIDE_DB_PATH"] = str(Path(TEST_DIR.name) / "exchange-test.db")

import module_rules
import storage


class ExchangeRuleTests(unittest.TestCase):
    def row(self, exchange_id):
        return module_rules._row("economy", "ExchangeTable", exchange_id)

    def account(self, suffix):
        return storage.get_or_create_account("exchange-" + suffix)

    def test_progressive_cost_and_limit_are_atomic(self):
        account = self.account("progressive")
        uid = account["uid"]
        storage.seed_player_num_attrs(uid, {2: 1200})

        result = storage.apply_exchange(uid, 3001, 5, self.row(3001))

        self.assertIsNotNone(result)
        self.assertEqual(result["rewards"], [{"cid": 104, "num": 500, "tag": 0}])
        self.assertEqual(storage.get_player_num_attrs(uid).get(104, 0), 500)
        self.assertEqual(storage.get_player_state_json(uid, "economy_exchange")["records"]["3001"]["count"], 5)
        self.assertIsNone(storage.apply_exchange(uid, 3001, 1, self.row(3001)))
        self.assertEqual(storage.get_player_num_attrs(uid).get(104, 0), 500)

    def test_critical_multiples_are_weighted_and_persisted(self):
        account = self.account("critical")
        uid = account["uid"]
        storage.seed_player_num_attrs(uid, {2: 100})

        result = storage.apply_exchange(uid, 2001, 2, self.row(2001))

        self.assertIsNotNone(result)
        multiples = result["critMultiples"][2001]
        self.assertEqual(len(multiples), 2)
        self.assertTrue(all(value in (1, 2, 3) for value in multiples))
        expected = sum(100000 * value for value in multiples)
        self.assertEqual(storage.get_player_num_attrs(uid).get(1, 0), expected)

    def test_unknown_or_malformed_rows_fail_closed_without_consuming(self):
        account = self.account("unknown")
        uid = account["uid"]
        storage.add_item(uid, 10201, 1)
        before = storage.get_items(uid)

        self.assertIsNone(storage.apply_exchange(uid, 999999, 1, None))
        self.assertEqual(storage.get_items(uid), before)
        self.assertIsNone(storage.apply_exchange(uid, 50001, 1, {"CostItems": [[10201, 1]], "GetItems": [[]]}))
        self.assertEqual(storage.get_items(uid), before)

    def test_batch_rolls_back_when_any_row_is_unknown(self):
        account = self.account("batch")
        uid = account["uid"]
        storage.add_item(uid, 2, 50)
        before = storage.get_items(uid)

        result = storage.apply_exchange_batch(
            uid,
            {2001: 1, 999999: 1},
            {2001: self.row(2001), 999999: None},
        )

        self.assertIsNone(result)
        self.assertEqual(storage.get_items(uid), before)
        self.assertIsNone(storage.get_player_state_json(uid, "economy_exchange"))


if __name__ == "__main__":
    unittest.main()


def tearDownModule():
    gc.collect()
    TEST_DIR.cleanup()
