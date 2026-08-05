import os
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["SOULTIDE_DB_PATH"] = str(Path(TEST_DIR.name) / "remaining-modules-test.db")

import module_handlers
import module_rules
import protocol_codec
import storage


class FakeSession:
    def __init__(self, uid):
        self.uid = uid
        self.account = storage.get_player(uid)
        self.messages = []

    def send(self, message_id, body):
        self.messages.append((message_id, body))

    def _send_notify_start_fight(self, **kwargs):
        battle_id = storage.create_battle_instance(
            self.uid,
            int(kwargs.get("battle_type", 4)),
            int(kwargs.get("map_id", 0)),
            int(kwargs.get("monster_team_id", 0)),
            reward_pairs=kwargs.get("reward_pairs", []),
        )
        if battle_id is None:
            return False
        storage.set_battle_server_snapshot(
            self.uid,
            battle_id,
            {"RandomSeed": 7, "Attacker": {}, "Defender": {}},
        )
        self.send(2903, b"")
        return True


class RemainingModuleTests(unittest.TestCase):
    def setUp(self):
        account = storage.get_or_create_account("remaining-" + self._testMethodName)
        self.uid = account["uid"]
        storage.seed_player_num_attrs(self.uid, {311: 100000, 1: 100000, 2: 100000})
        self.session = FakeSession(self.uid)

    def test_magic_tower_uses_real_cells_hex_neighbors_and_dialog_result(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6602, protocol_codec.encode_method(6602, 1)))
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_magicTower"]["data"]
        self.assertEqual(len(data["cells"]), 90)
        self.assertEqual(data["cells"][0]["id"], 1)
        for cell in data["cells"]:
            ref = module_rules._row("magic_tower", "MagicTowerFloorListTable", cell["floor"])
            self.assertIn(cell["dataId"], module_rules._row("magic_tower", "MagicTowerMapCellTable", ref["CellID"][cell["x"]])["DataList"])

        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6604, protocol_codec.encode_method(6604, 2)))
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_magicTower"]["data"]
        self.assertEqual(data["role"]["cellId"], 2)
        self.assertTrue(any(message[0] == 6608 for message in self.session.messages))

        state = storage.get_player_state_json(self.uid, "remaining_modules")
        data = state["modules"]["net_magicTower"]["data"]
        data["currDialog"] = 46000000
        data["dialogPending"] = {"cellId": 2, "index": 0}
        storage.update_player_state_json(self.uid, "remaining_modules", state)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6610, protocol_codec.encode_method(6610, 46000000, [])))
        result = next(body for message_id, body in reversed(self.session.messages) if message_id == 6611)
        code, next_dialog = protocol_codec.decode_method(6611, result)
        self.assertEqual((code, next_dialog), (0, 0))

    def test_magic_tower_final_floor_settles(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6602, protocol_codec.encode_method(6602, 1)))
        state = storage.get_player_state_json(self.uid, "remaining_modules")
        module = state["modules"]["net_magicTower"]
        data = module["data"]
        final = next(cell for cell in data["cells"] if cell["floor"] == 6 and cell["x"] == 14)
        data["role"]["cellId"] = final["id"]
        self.assertTrue(module_handlers._magic_run_cell(self.session, self.uid, state, module, data, final))
        self.assertFalse(data["active"])
        self.assertTrue(any(message[0] == 6609 for message in self.session.messages))

    def test_mining_layout_costs_empty_and_ore_interact(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7602, b""))
        state = storage.get_player_state_json(self.uid, "remaining_modules")
        data = state["modules"]["net_mining"]["data"]
        layer = module_rules._row("mining", "MiningLayerTable", 1)
        minimum = sum(int(entry[1]) for entry in layer["Element"] if isinstance(entry, list) and len(entry) >= 3)
        maximum = sum(int(entry[2]) for entry in layer["Element"] if isinstance(entry, list) and len(entry) >= 3)
        self.assertGreaterEqual(len(data["grids"]), minimum)
        self.assertLessEqual(len(data["grids"]), maximum)
        self.assertLess(len(data["grids"]), 140)
        for key, grid in data["grids"].items():
            self.assertEqual(int(key), grid["x"] * 7 + grid["y"] + 1)
        empty_id = next(int(key) for key, grid in data["grids"].items() if module_rules._row("mining", "MiningElementTable", grid["dataCid"]).get("Type") == 9)
        before = storage.get_player_num_attrs(self.uid)[311]
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7603, protocol_codec.encode_method(7603, empty_id)))
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_mining"]["data"]
        self.assertEqual(data["grids"][str(empty_id)]["state"], 2)
        self.assertEqual(storage.get_player_num_attrs(self.uid)[311], before - 3)

        ore_id = next(int(key) for key, grid in data["grids"].items() if module_rules._row("mining", "MiningElementTable", grid["dataCid"]).get("Type") == 2 and grid["state"] == 0)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7603, protocol_codec.encode_method(7603, ore_id)))
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_mining"]["data"]
        self.assertEqual(data["grids"][str(ore_id)]["state"], 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7605, protocol_codec.encode_method(7605, ore_id, 0)))
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_mining"]["data"]
        self.assertEqual(data["grids"][str(ore_id)]["state"], 2)

    def test_mining_auto_modes_persist_selected_grid_states(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7602, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7604, protocol_codec.encode_method(7604, 2, 0)))
        result = next(body for message_id, body in reversed(self.session.messages) if message_id == 7609)
        code, grid_ids = protocol_codec.decode_method(7609, result)
        self.assertEqual(code, 0)
        self.assertEqual(len(grid_ids), 2)
        data = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]["net_mining"]["data"]
        self.assertTrue(all(data["grids"][str(grid_id)]["state"] in (1, 2) for grid_id in grid_ids))

    def test_mining_uses_captured_reward_group_sample(self):
        self.assertEqual(
            module_handlers._mining_reward_pairs({"Reward": 11120102}),
            [(46106, 2), (46108, 1), (46109, 1)],
        )
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7602, b""))
        state = storage.get_player_state_json(self.uid, "remaining_modules")
        data = state["modules"]["net_mining"]["data"]
        chest_id = next(
            int(key) for key, grid in data["grids"].items()
            if int(grid["dataCid"]) == 201
        )
        before = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(46111, 0)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 7603, protocol_codec.encode_method(7603, chest_id)
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 7605, protocol_codec.encode_method(7605, chest_id, 0)
        ))

        after = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(46111, 0)
        self.assertEqual(after, before + 1)
        reward_body = next(
            body for message_id, body in reversed(self.session.messages) if message_id == 7616
        )
        self.assertEqual(
            protocol_codec.decode_method(7616, reward_body),
            [[{"cid": 46111, "num": 1, "tag": 0}]],
        )

    def test_mining_unobserved_reward_groups_use_playable_local_defaults(self):
        self.assertEqual(
            module_handlers._mining_reward_pairs({"Reward": 11120104}),
            [(316, 3)],
        )
        self.assertEqual(
            module_handlers._mining_reward_pairs({"Reward": 11120106}),
            [(316, 7)],
        )
        self.assertEqual(
            module_handlers._mining_reward_pairs({"Reward": 11120110}),
            [(320, 2)],
        )

    def test_mining_reward_show_and_unknown_cids_are_normalized(self):
        self.assertEqual(
            module_handlers._mining_reward_pairs({"RewardShow": [[46106, 2], [46108, 1]]}),
            [(46106, 2), (46108, 1)],
        )
        self.assertEqual(
            module_handlers._known_mining_items([(46106, 1), (999999999, 1), (0, 2)]),
            [(46106, 1)],
        )

    def test_guild_is_shared_across_accounts(self):
        owner = storage.get_or_create_account("guild-owner")
        applicant = storage.get_or_create_account("guild-applicant")
        owner_session = FakeSession(owner["uid"])
        applicant_session = FakeSession(applicant["uid"])
        self.assertTrue(module_handlers.dispatch(owner_session, owner["uid"], 100902, protocol_codec.encode_method(100902, "Shared Guild")))
        guild_id = storage.guild_for_uid(owner["uid"])["id"]
        self.assertEqual(len(storage.guild_list("Shared", 10)), 1)
        self.assertTrue(module_handlers.dispatch(applicant_session, applicant["uid"], 100906, protocol_codec.encode_method(100906, guild_id)))
        self.assertTrue(module_handlers.dispatch(owner_session, owner["uid"], 100911, protocol_codec.encode_method(100911, applicant["uid"])))
        members = storage.guild_members(guild_id)
        self.assertEqual({member["uid"] for member in members}, {owner["uid"], applicant["uid"]})
        self.assertTrue(module_handlers.dispatch(applicant_session, applicant["uid"], 100917, b""))
        self.assertIsNone(storage.guild_for_uid(applicant["uid"]))
        self.assertIsNotNone(storage.guild_for_uid(owner["uid"]))


if __name__ == "__main__":
    unittest.main()
