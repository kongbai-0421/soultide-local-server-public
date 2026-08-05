import os
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["SOULTIDE_DB_PATH"] = str(Path(TEST_DIR.name) / "restaurant-test.db")

import module_handlers
import protocol_codec
import storage


class FakeSession:
    def __init__(self):
        self.messages = []

    def send(self, message_id, body):
        self.messages.append((message_id, body))

    def _send_notify_start_fight(self, *args, **kwargs):
        self.messages.append((2903, args, kwargs))
        return True


class RestaurantRuleTests(unittest.TestCase):
    def setUp(self):
        self.account = storage.get_or_create_account("restaurant-rule-account-" + self._testMethodName)
        self.uid = self.account["uid"]
        storage.seed_player_num_attrs(self.uid, {331: 0})
        self.session = FakeSession()

    def _last(self, message_id):
        return next(body for current, body in reversed(self.session.messages) if current == message_id)

    def test_initial_state_and_recruitment_use_configured_currency(self):
        self.assertTrue(module_handlers.handle_restaurant_get_info(self.session, self.uid))
        code, pod = protocol_codec.decode_method(9121, self._last(9121))
        self.assertEqual(code, 0)
        self.assertEqual(pod["level"], 1)
        self.assertEqual(pod["allAtt"], 3)
        self.assertEqual(storage.get_player_num_attrs(self.uid)[331], 500)

        self.assertTrue(module_handlers.handle_restaurant_transact_documents(self.session, self.uid))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[331], 400)
        _, position = protocol_codec.decode_method(9122, self._last(9122))
        self.assertEqual(position["id"], 1001)

    def test_work_income_and_receive_are_idempotent(self):
        module_handlers.handle_restaurant_get_info(self.session, self.uid)
        module_handlers.handle_restaurant_transact_documents(self.session, self.uid)
        body = protocol_codec.encode_method(9106, 1001, 1001, 1, 1)
        self.assertTrue(module_handlers.handle_restaurant_work(self.session, self.uid, body))
        self.assertTrue(module_handlers.handle_restaurant_receive_income(self.session, self.uid))
        self.assertFalse(module_handlers.handle_restaurant_receive_income(self.session, self.uid))
        self.assertEqual(storage.get_player_num_attrs(self.uid).get(334, 0), 1)

    def test_practice_uses_modify_cost_and_updates_position(self):
        module_handlers.handle_restaurant_get_info(self.session, self.uid)
        module_handlers.handle_restaurant_transact_documents(self.session, self.uid)
        self.assertTrue(
            module_handlers.handle_restaurant_practice(
                self.session, self.uid, protocol_codec.encode_method(9104, 1001)
            )
        )
        self.assertEqual(storage.get_player_num_attrs(self.uid)[331], 200)
        _, _, before, pod = protocol_codec.decode_method(9123, self._last(9123))
        self.assertEqual(before, {1: 1})
        self.assertEqual(pod["positionInformation"][0]["att"][1], 2)

    def test_answer_puzzle_and_minigames_persist_and_reject_duplicates(self):
        module_handlers.handle_restaurant_get_info(self.session, self.uid)
        self.assertTrue(module_handlers.handle_restaurant_get_problem(self.session, self.uid, protocol_codec.encode_method(9111, True)))
        _, question = protocol_codec.decode_method(9130, self._last(9130))
        self.assertTrue(module_handlers.handle_restaurant_answer(self.session, self.uid, protocol_codec.encode_method(9112, True, question["id"])))
        self.assertFalse(module_handlers.handle_restaurant_answer(self.session, self.uid, protocol_codec.encode_method(9112, True, question["id"])))
        self.assertTrue(module_handlers.handle_restaurant_puzzle(self.session, self.uid, protocol_codec.encode_method(9116, 1000)))
        self.assertTrue(module_handlers.handle_restaurant_link_game(self.session, self.uid, protocol_codec.encode_method(9114, 1, 6)))
        self.assertFalse(module_handlers.handle_restaurant_link_game(self.session, self.uid, protocol_codec.encode_method(9114, 1, 6)))
        self.assertTrue(module_handlers.handle_restaurant_memory_flop(self.session, self.uid, protocol_codec.encode_method(9118, 1, 5, 40)))
        self.assertFalse(module_handlers.handle_restaurant_memory_flop(self.session, self.uid, protocol_codec.encode_method(9118, 1, 5, 40)))
        state = storage.get_player_state_json(self.uid, "restaurant")
        self.assertEqual(state["puzzle_score"], 1000)
        self.assertEqual(state["link_games"][0]["id"], 1)
        self.assertEqual(state["memory_draws"][0]["cumulativeSteps"], 5)

    def test_challenge_reward_is_bound_to_the_battle_instance(self):
        battle_id = storage.create_battle_instance(
            self.uid, 4, 10101, 42710101, reward_pairs=[(11501, 100)]
        )
        result = storage.settle_battle(self.uid, battle_id, 1, rounds=1, report={})
        self.assertEqual(result["rewards"], [{"cid": 11501, "num": 100, "tag": 0}])
        self.assertEqual(storage.get_items(self.uid)[0]["template_id"], 11501)

    def test_remaining_actions_have_rule_driven_responses(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9102, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9108, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9109, protocol_codec.encode_method(9109, 1007)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9110, protocol_codec.encode_method(9110, 50000100, [])))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9113, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9115, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9117, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9119, protocol_codec.encode_method(9119, 10101, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9120, protocol_codec.encode_method(9120, 1)))


if __name__ == "__main__":
    unittest.main()
