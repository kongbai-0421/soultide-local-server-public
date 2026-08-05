import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["SOULTIDE_DB_PATH"] = str(Path(TEST_DIR.name) / "module-rules-test.db")

import module_handlers
import module_rules
import protocol_codec
import storage
import login_server


class FakeSession:
    def __init__(self):
        self.messages = []
        self.uid = None
        self.account = None

    def send(self, message_id, body):
        self.messages.append((message_id, body))

    def _send_notify_start_fight(self, *args, **kwargs):
        self.messages.append((2903, args, kwargs))
        battle_type = int(kwargs.get("battle_type", args[0] if args else 4))
        map_id = int(kwargs.get("map_id", args[1] if len(args) > 1 else 0))
        monster_team_id = int(kwargs.get("monster_team_id", args[2] if len(args) > 2 else 0))
        battle_id = storage.create_battle_instance(
            self.uid, battle_type, map_id, monster_team_id,
            reward_pairs=kwargs.get("reward_pairs", []),
        )
        storage.set_battle_server_snapshot(
            self.uid,
            battle_id,
            {
                "RandomSeed": 7,
                "MaxRound": 1,
                "Attacker": {"ArrFightUnitPOD": [{"Power": 100}]},
                "Defender": {"ArrFightUnitPOD": [{"Power": 1}]},
            },
        )
        return True

    def _handle_local_battle_entry(self, request_id, result_id, body):
        try:
            values = protocol_codec.decode_method(request_id, body or b"")
        except (KeyError, TypeError, ValueError):
            return False
        if storage.get_active_battle(self.uid) is not None:
            return False
        numbers = [int(value) for value in values if isinstance(value, int) and not isinstance(value, bool) and value > 0]
        self._send_notify_start_fight(
            battle_type={5502: 2, 9402: 2, 9405: 2}.get(request_id, 2),
            map_id=numbers[0] if numbers else 0,
            monster_team_id=numbers[1] if len(numbers) > 1 else 0,
        )
        result_method = protocol_codec.METHODS[result_id]
        defaults = []
        for type_name in result_method["types"]:
            if type_name in ("int", "long"):
                defaults.append(0)
            elif type_name == "bool":
                defaults.append(False)
            elif type_name.startswith("list<"):
                defaults.append([])
            else:
                defaults.append({})
        self.send(result_id, protocol_codec.encode_method(result_id, *defaults))
        return True


class ModuleRuleTests(unittest.TestCase):
    def setUp(self):
        account = storage.get_or_create_account("module-rule-" + self._testMethodName)
        self.uid = account["uid"]
        storage.seed_player_num_attrs(self.uid, {1: 100000, 2: 100000, 121: 100000, 122: 100000, 124: 100000, 125: 100000, 315: 100000, 341: 100000, 342: 100000, 351: 100000, 46102: 100000})
        self.session = FakeSession()
        self.session.uid = self.uid
        self.session.account = account

    def _item_quantity(self, item_id):
        return next(
            (
                int(row["quantity"])
                for row in storage.get_items(self.uid)
                if int(row.get("template_id", 0)) == int(item_id)
            ),
            0,
        )

    def _town_dialog_request_from(self, start_dialog_id, choice_selection=1):
        """Build one client-style 1602 request from the active town dialog."""
        current_id = int(start_dialog_id)
        skip_indexes = []
        choice_count = 0
        for _ in range(module_handlers.TOWN_DIALOG_MAX_SELECTIONS):
            row = module_handlers._town_dialog(current_id)
            self.assertIsNotNone(row)
            if len(row.get("JumpID") or []) > 1:
                choice_count += 1
            selection = choice_selection if len(row.get("JumpID") or []) > 1 else 1
            transition = module_handlers._town_dialog_transition(self.uid, row, selection)
            self.assertIsNotNone(transition)
            next_id, services = transition
            if next_id > 0 and not module_handlers._town_dialog_requires_server(services):
                skip_indexes.append(selection)
                current_id = next_id
                continue
            return selection, skip_indexes, choice_count
        self.fail("town dialog did not reach a server or terminal edge")

    def _town_dialog_request(self, choice_selection=1):
        pending = storage.get_player_state_json(self.uid, "town")["pending_story"]
        return self._town_dialog_request_from(
            pending["dialog_cid"], choice_selection,
        )

    def _finish_town_story(self, choice_selection=1):
        choice_count = 0
        for _ in range(16):
            if getattr(self.session, "active_story", None) is None:
                return choice_count
            select_index, skip_indexes, choices = self._town_dialog_request(choice_selection)
            choice_count += choices
            self.assertTrue(
                module_handlers.handle_town_dialog(
                    self.session, self.uid, select_index, skip_indexes,
                )
            )
        self.fail("town story did not finish")

    def test_place_recruit_level_cost_and_persistence(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9202, protocol_codec.encode_method(9202, 1001)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9203, protocol_codec.encode_method(9203, 1, 2)))
        state = storage.get_player_state_json(self.uid, "place_game")
        self.assertEqual(state["units"][0]["level"], 2)
        self.assertEqual(storage.get_player_num_attrs(self.uid)[341], 92600)
        _, pod = protocol_codec.decode_method(9221, self.session.messages[-1][1])
        self.assertEqual(pod["level"], 2)

    def test_place_tower_uses_configured_battle_and_box_cost(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9210, protocol_codec.encode_method(9210, 1001)))
        self.assertEqual(self.session.messages[-2][0], 2903)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9212, protocol_codec.encode_method(9212, 10001, 1)))
        self.assertTrue(any(message[0] == 4102 for message in self.session.messages))

    def test_jewelry_cost_speed_and_recycle(self):
        storage.grant_reward_pairs(self.uid, [(315, 100000), (46102, 100000)])
        storage.update_player_state_json(self.uid, "jewelry", {
            "items": [{
                "id": 1, "template_id": 46602, "star": 1, "speed": 0,
                "role_id": 0, "slot": 0, "locked": False,
            }],
            "speed": {},
            "next_id": 2,
        })
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7704, protocol_codec.encode_method(7704, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7706, protocol_codec.encode_method(7706, 1, 4)))
        self.assertFalse(module_handlers.dispatch(self.session, self.uid, 7706, protocol_codec.encode_method(7706, 1, 99)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7707, protocol_codec.encode_method(7707, 1, 1)))
        self.assertTrue(any(item["template_id"] == 46602 for item in storage.get_items(self.uid)))

    def test_dispatch_replaces_generated_fallbacks(self):
        for message_id in (6502, 6503, 6510, 6520, 6526, 6528, 6530, 6532, 6534, 6536, 6803, 6804, 6806, 6807, 6808, 6809, 6810, 6811, 6838, 7705, 7708, 7709, 7710, 7711, 7712, 9204, 9205, 9206, 9207, 9208, 9209, 9213, 9214, 9215, 9218, 9219, 9303, 9305, 9306, 9307, 9308, 9309, 9310, 9311, 9312, 9313, 9314, 9315, 9316, 9317, 9318, 9319, 9320, 9321, 9322, 9323, 9324, 9325, 9326, 9327, 9328, 9329, 9502, 9503, 9504, 9505, 9506, 9507, 9522, 9529, 9702, 9703, 9704, 9705, 9706, 9707, 100102, 100103, 100108, 100202, 100204, 100206, 7402, 7403, 7404, 7502, 7503, 7504, 7505, 9002):
            self.assertIn(message_id, module_handlers.MODULE_DISPATCH)

    def test_amusement_dual_horizontal_and_minigal_state(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9302, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9308, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6509, protocol_codec.encode_method(6509, 1, 1, 2, False)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6510, protocol_codec.encode_method(6510, 1, 2)))
        element_id = int(next(iter(module_rules._table("horizontal", "HorizontalRPGElementTable"))))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9502, protocol_codec.encode_method(9502, 1, element_id, 1, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9507, protocol_codec.encode_method(9507, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6802, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6803, protocol_codec.encode_method(6803, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6805, protocol_codec.encode_method(6805, 1, [])))
        self.assertEqual(storage.get_player_state_json(self.uid, "dual_team_explore")["team1"]["node"], 2)
        self.assertEqual(storage.get_player_state_json(self.uid, "horizontal_rpg")["weather"], 1)

    def test_card_chat_rank_and_guild_rules(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9702, protocol_codec.encode_method(9702, 1001, True, 3)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9706, protocol_codec.encode_method(9706, 1001)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9706, protocol_codec.encode_method(9706, 1001)))
        self.assertEqual(storage.get_player_state_json(self.uid, "card_activity")["story"].count(1001), 1)
        chat = {"channel": 1, "content": "local", "target": "", "type": 1}
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100102, protocol_codec.encode_method(100102, chat)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100103, protocol_codec.encode_method(100103, 1)))
        self.assertEqual(storage.get_player_state_json(self.uid, "chat")["messages"], [])
        _, chat_room = protocol_codec.decode_method(100105, self.session.messages[-1][1])
        self.assertEqual(chat_room["msg"], [])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100202, protocol_codec.encode_method(100202, 1, 1, False)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7502, protocol_codec.encode_method(7502, 0, 101)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7504, protocol_codec.encode_method(7504, 101, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 9002, protocol_codec.encode_method(9002, 100, 1)))

    def test_operation_activities_use_extracted_rules_and_persist(self):
        storage.seed_player_num_attrs(self.uid, {5: 10000, 405: 10})
        storage.grant_reward_pairs(self.uid, [(405, 10)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7002, protocol_codec.encode_method(7002, 220630001)))
        turntable_result = protocol_codec.decode_method(7003, self.session.messages[-1][1])
        self.assertEqual(turntable_result[0], 0)
        self.assertTrue(turntable_result[2])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100402, protocol_codec.encode_method(100402, 220630001)))
        _, records = protocol_codec.decode_method(100404, self.session.messages[-1][1])
        self.assertEqual(len(records), 1)
        uuid = turntable_result[2]
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100403, protocol_codec.encode_method(100403, 220630001, uuid, "local", "", "")))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100602, protocol_codec.encode_method(100602, 220127002)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6202, protocol_codec.encode_method(6202, 220127002, 10001)))
        self.assertFalse(module_handlers.dispatch(self.session, self.uid, 6202, protocol_codec.encode_method(6202, 220127002, 10001)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7302, protocol_codec.encode_method(7302, 230223001, 10001)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 100502, protocol_codec.encode_method(100502, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5002, protocol_codec.encode_method(5002, 211001001, 1, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6302, protocol_codec.encode_method(6302, 211001005, "12345")))
        state = storage.get_player_state_json(self.uid, "operation_activities")
        self.assertEqual(state["group_buys"]["211001001"]["1"], 1)
        self.assertEqual(state["votes"]["220127002"]["10001"], 1)
        self.assertEqual(state["cup_votes"]["230223001"]["tickets"], 2)
        self.assertTrue(state["welcome"]["useCode"])

    def test_home_enter_seeds_complete_idempotent_pod(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertEqual(self.session.messages[-1][0], 1834)
        code, home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        self.assertEqual(code, 0)
        player = storage.get_player(self.uid)
        self.assertEqual(home["baseInfo"]["id"], int(player["role_id"]))
        self.assertEqual(home["baseInfo"]["alreadyReward"], [])
        self.assertEqual([room["cid"] for room in home["rooms"]], list(range(1, 20)))
        self.assertEqual(len(home["rooms"]), 19)
        self.assertEqual(len(home["buildings"]), 7)
        self.assertEqual(len(home["roles"]), len(storage.get_souls(self.uid)))
        self.assertTrue(all(role["belongRoom"] == 0 for role in home["roles"]))
        self.assertTrue(all(role["letters"] == [] for role in home["roles"]))
        for role in home["roles"]:
            suffix = role["roleCid"] - 20010000
            self.assertGreaterEqual(suffix, 1)
            self.assertLessEqual(suffix, 55)
            self.assertEqual(role["dress3DCid"], 33000000 + suffix * 100 + 10)
            self.assertEqual(role["dress2DCid"], 33010000 + suffix * 100 + 10)
            self.assertGreaterEqual(role["favorLv"], 1)
            self.assertEqual(role["transactionCid"], 0)
            self.assertEqual(role["newStoryId"], 0)
        building_by_cid = {row["cid"]: row for row in home["buildings"]}
        self.assertEqual(set(building_by_cid), {
            36000001, 36000002, 36000003, 36000005,
            36000006, 36000007, 36000008,
        })
        for building in home["buildings"]:
            self.assertIn("lands", building)
            self.assertIn("helpLogs", building)
        production = building_by_cid[36000001]["productionData"]
        self.assertEqual(production["storageLimit"], 100)
        self.assertEqual(production["oneProduceTime"], 300)
        self.assertEqual(production["itemAwards"], {11911: 1})
        self.assertEqual(building_by_cid[36000002]["manufacture"], {
            "maxQueueCount": 1, "makes": [],
        })
        self.assertEqual(building_by_cid[36000005]["kitchenPOD"], {
            "maxQueueCount": 1, "culinarys": [],
        })
        office = building_by_cid[36000006]["officePOD"]
        self.assertEqual(office["freeRefreshTimes"], 4)
        self.assertEqual(len(office["affairs"]), 4)
        self.assertEqual([row["id"] for row in office["affairs"]], [1, 2, 3, 4])
        self.assertEqual([row["cid"] for row in office["affairs"]], [1, 2, 3, 4])
        self.assertTrue(all(row["status"] == 0 for row in office["affairs"]))
        self.assertEqual(
            [land["cid"] for land in building_by_cid[36000003]["lands"]],
            [36300001, 36300002, 36300003, 36300004],
        )
        self.assertTrue(all(
            land["status"] == 1
            for land in building_by_cid[36000003]["lands"]
        ))
        first_state = storage.get_player_state_json(self.uid, "home")
        first_next_time = building_by_cid[36000001]["productionData"]["nextProduceTime"]
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        _, second_home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        self.assertEqual(len(second_home["rooms"]), 19)
        self.assertEqual(len(second_home["buildings"]), 7)
        self.assertEqual(
            {row["cid"]: row for row in second_home["buildings"]}[36000001]["productionData"]["nextProduceTime"],
            first_next_time,
        )
        self.assertEqual(storage.get_player_state_json(self.uid, "home"), first_state)

    def test_home_enter_migrates_existing_state_without_overwriting_user_data(self):
        storage.update_player_state_json(self.uid, "home", {
            "level": 9,
            "comfort": 321,
            "rooms": [{
                "cid": 7, "dbid": 7007, "name": "保留房间", "comfort": 88,
                "decorates": [{"cid": 1001, "x": 2, "y": 3}], "suitCid": 1006,
                "foreignShow": True, "receiveComfortAwards": True,
            }],
            "buildings": [{
                "id": 36000005, "cid": 36000005, "lv": 4, "helpLogs": [],
                "lands": [], "kitchenPOD": {"maxQueueCount": 3, "culinarys": []},
            }],
            "suits": [1006],
            "decorations": [1001],
        })
        original_player = storage.get_player(self.uid)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        _, home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        migrated = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(migrated["id"], int(original_player["role_id"]))
        self.assertEqual(migrated["already_reward"], [])
        self.assertEqual(migrated["rooms"][0]["name"], "保留房间")
        self.assertEqual(migrated["rooms"][0]["decorates"], [{"cid": 1001, "x": 2, "y": 3}])
        self.assertEqual(migrated["buildings"][0]["lv"], 4)
        self.assertEqual(migrated["buildings"][0]["kitchenPOD"]["maxQueueCount"], 3)
        self.assertEqual(migrated["suits"], [1006])
        self.assertEqual(migrated["decorations"], [1001])
        self.assertEqual(len(migrated["roles"]), len(storage.get_souls(self.uid)))
        self.assertEqual(home["baseInfo"]["id"], int(original_player["role_id"]))
        self.assertEqual(home["baseInfo"]["alreadyReward"], [])
        first_migrated = migrated
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertEqual(storage.get_player_state_json(self.uid, "home"), first_migrated)

    def test_home_comfort_sums_each_theme_and_recomputes_after_delete(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        theme_one = [
            int(cid) for cid, row in module_handlers.HOME_CONFIG["decorates"].items()
            if int(row.get("ThemeID", 0) or 0) == 1
        ][:10]
        theme_two = [
            int(cid) for cid, row in module_handlers.HOME_CONFIG["decorates"].items()
            if int(row.get("ThemeID", 0) or 0) == 2
        ][:10]
        self.assertEqual((len(theme_one), len(theme_two)), (10, 10))
        state = storage.get_player_state_json(self.uid, "home")
        room = next(row for row in state["rooms"] if int(row["cid"]) == 1)
        room["decorates"] = [{"cid": cid} for cid in theme_one + theme_two]
        storage.update_player_state_json(self.uid, "home", state)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state = storage.get_player_state_json(self.uid, "home")
        expected_all = sum(
            int(module_handlers.HOME_CONFIG["decorates"][str(cid)]["Score"])
            for cid in theme_one + theme_two
        ) + 500 + 1500
        self.assertEqual(state["comfort"], expected_all)

        room = next(row for row in state["rooms"] if int(row["cid"]) == 1)
        room["decorates"] = [{"cid": cid} for cid in theme_one]
        storage.update_player_state_json(self.uid, "home", state)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        persisted = storage.get_player_state_json(self.uid, "home")
        expected_one = sum(
            int(module_handlers.HOME_CONFIG["decorates"][str(cid)]["Score"])
            for cid in theme_one
        ) + 500
        self.assertEqual(persisted["comfort"], expected_one)
        self.assertEqual(
            next(row for row in persisted["rooms"] if int(row["cid"]) == 1)["comfort"],
            expected_one,
        )

    def test_home_enter_migrates_legacy_dorm_suits_and_comfort(self):
        """旧 101/102 为空时恢复官方布局，并重算所有宿舍舒适度。"""
        decorates = [
            {"cid": 1601021, "x": 0, "y": 0},
            {"cid": 1601011, "x": 0, "y": 0},
            {"cid": 1601031, "x": 22, "y": 9},
            {"cid": 1601037, "x": 16, "y": 29},
            {"cid": 1601037, "x": 44, "y": 29},
            {"cid": 1801096, "x": 37, "y": 6},
            {"cid": 1801112, "x": 32, "y": 29},
        ]
        storage.update_player_state_json(self.uid, "home", {
            "comfort": 0,
            "rooms": [
                {"cid": 101, "dbid": 101, "name": "旧房间", "comfort": 0,
                 "suitCid": 0, "decorates": [], "foreignShow": False,
                 "receiveComfortAwards": False},
                {"cid": 102, "dbid": 102, "name": "旧房间", "comfort": 0,
                 "suitCid": 0, "decorates": [], "foreignShow": False,
                 "receiveComfortAwards": False},
                {"cid": 103, "dbid": 103, "name": "旧房间", "comfort": 0,
                 "suitCid": 160000, "decorates": decorates,
                 "foreignShow": False, "receiveComfortAwards": False},
            ],
        })
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        _, home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        rooms = {int(row["cid"]): row for row in home["rooms"]}
        self.assertEqual(rooms[101]["suitCid"], 150000)
        self.assertEqual(rooms[102]["suitCid"], 160000)
        self.assertEqual(
            rooms[101]["decorates"], module_handlers._home_default_layout(150000),
        )
        self.assertEqual(
            rooms[102]["decorates"], module_handlers._home_default_layout(160000),
        )
        expected = sum(
            int(module_handlers.HOME_CONFIG["decorates"][str(row["cid"])] ["Score"])
            for row in decorates
        )
        self.assertEqual(rooms[103]["comfort"], expected)
        expected_total = expected + 400 + 500
        self.assertEqual(home["baseInfo"]["currentComfort"], expected_total)
        self.assertEqual(home["baseInfo"]["maxComfort"], expected_total)
        persisted = storage.get_player_state_json(self.uid, "home")
        persisted_rooms = {int(row["cid"]): row for row in persisted["rooms"]}
        self.assertEqual(persisted_rooms[101]["suitCid"], 150000)
        self.assertEqual(persisted_rooms[102]["suitCid"], 160000)
        self.assertEqual(
            persisted_rooms[101]["decorates"], module_handlers._home_default_layout(150000),
        )
        self.assertEqual(
            persisted_rooms[102]["decorates"], module_handlers._home_default_layout(160000),
        )
        self.assertEqual(persisted_rooms[103]["comfort"], expected)
        self.assertEqual(persisted["comfort"], expected_total)

    def test_home_default_dorm_layout_does_not_fill_function_rooms_or_restore_cleared_room(self):
        storage.update_player_state_json(self.uid, "home", {
            "rooms": [
                module_handlers._initial_home_room(1, 10100),
                module_handlers._initial_home_room(101, 150000),
            ],
        })
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state = storage.get_player_state_json(self.uid, "home")
        rooms = {int(row["cid"]): row for row in state["rooms"]}
        self.assertEqual(rooms[1]["decorates"], [])
        self.assertEqual(rooms[101]["decorates"], module_handlers._home_default_layout(150000))
        self.assertEqual(rooms[101]["comfort"], 400)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1832,
            protocol_codec.encode_method(1832, 101, []),
        ))
        state = storage.get_player_state_json(self.uid, "home")
        cleared = next(row for row in state["rooms"] if int(row["cid"]) == 101)
        self.assertEqual(cleared["decorates"], [])
        self.assertTrue(cleared["default_layout_initialized"])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state = storage.get_player_state_json(self.uid, "home")
        cleared = next(row for row in state["rooms"] if int(row["cid"]) == 101)
        self.assertEqual(cleared["decorates"], [])
        self.assertEqual(cleared["comfort"], 0)

    def test_home_migration_preserves_custom_dorm_furniture(self):
        custom = [{"cid": 1801096, "x": 37, "y": 6}]
        storage.update_player_state_json(self.uid, "home", {
            "rooms": [{
                "cid": 101, "dbid": 101, "suitCid": 150000,
                "decorates": custom,
            }],
        })
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state = storage.get_player_state_json(self.uid, "home")
        room = next(row for row in state["rooms"] if int(row["cid"]) == 101)
        self.assertEqual(room["decorates"], custom)
        self.assertTrue(room["default_layout_initialized"])

    def test_home_unlock_room_rejects_seeded_room(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 1807,
            protocol_codec.encode_method(1807, 7),
        ))
        state = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(sum(
            1 for room in state["rooms"]
            if int(room.get("cid", 0)) == 7
        ), 1)

    def test_home_unlock_dorm_103_sends_1871_and_deducts_cost(self):
        """解锁 CastleIndex=2 宿舍 103：扣 11911×50、发 1871 RoomPOD、发 1839。"""
        storage.grant_reward_pairs(self.uid, [(11911, 50)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        items_before = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1807,
            protocol_codec.encode_method(1807, 103),
        ))
        messages = {mid: body for mid, body in self.session.messages[-10:]}
        self.assertIn(1871, messages, "缺少 1871 notifyUpdateRoom")
        room = protocol_codec.decode_method(1871, messages[1871])[0]
        self.assertEqual(room["cid"], 103)
        self.assertEqual(room["suitCid"], 160000)
        self.assertIn(1839, messages, "缺少 1839 unlockRoomResult")
        code, cid = protocol_codec.decode_method(1839, messages[1839])
        self.assertEqual((code, cid), (0, 103))
        self.assertIn(4102, messages, "缺少 4102 物品变化通知")
        items_after = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        self.assertEqual(
            items_after.get(11911, 0),
            items_before.get(11911, 0) - 50,
            "11911 未正确扣减",
        )
        state = storage.get_player_state_json(self.uid, "home")
        dorm = next((r for r in state["rooms"] if int(r.get("cid", 0)) == 103), None)
        self.assertIsNotNone(dorm, "103 号宿舍未持久化")
        self.assertEqual(dorm["suitCid"], 160000)
        self.assertEqual(
            dorm["decorates"], module_handlers._home_default_layout(160000),
        )
        self.assertEqual(dorm["comfort"], 500)
        self.assertEqual(state["comfort"], 500)
        self.assertEqual(state["max_comfort"], 500)

    def test_home_unlock_dorm_duplicate_no_recharge(self):
        """重复解锁同一宿舍不二次扣费、不重复新增房间条目。"""
        storage.grant_reward_pairs(self.uid, [(11911, 100)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1807,
            protocol_codec.encode_method(1807, 103),
        ))
        items_after_first = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        state_first = storage.get_player_state_json(self.uid, "home")
        count_first = sum(
            1 for r in state_first["rooms"] if int(r.get("cid", 0)) == 103
        )
        self.session.messages.clear()
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 1807,
            protocol_codec.encode_method(1807, 103),
        ), "重复解锁 103 应返回失败")
        items_after_second = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        self.assertEqual(
            items_after_second.get(11911, 0),
            items_after_first.get(11911, 0),
            "重复解锁不应再次扣费",
        )
        state_second = storage.get_player_state_json(self.uid, "home")
        count_second = sum(
            1 for r in state_second["rooms"] if int(r.get("cid", 0)) == 103
        )
        self.assertEqual(count_second, count_first, "房间条目不应重复")

    def test_home_unlock_land_checks_condition_cost_and_duplicate(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        before = storage.get_player_num_attrs(self.uid)[2]
        request = protocol_codec.encode_method(1886, 36000003, 36300005)
        with self.assertLogs("tcp_server", level="WARNING") as logs:
            self.assertFalse(module_handlers.dispatch(self.session, self.uid, 1886, request))
        self.assertEqual(self.session.messages[-1][0], 1888)
        error_code, error_land = protocol_codec.decode_method(
            1888, self.session.messages[-1][1],
        )
        self.assertEqual(error_code, module_handlers.HOME_UNLOCK_LAND_ERROR_CODE)
        self.assertEqual(error_land["cid"], 0)
        self.assertTrue(any(
            "home_unlock_land_rejected" in message and "condition_failed" in message
            for message in logs.output
        ))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[2], before)

        storage.update_player_state_json(self.uid, "quickChallenge", [25020205])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1886, request))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[2], before - 1000)
        state = storage.get_player_state_json(self.uid, "home")
        building = next(row for row in state["buildings"] if row["id"] == 36000003)
        land = next(
            row for row in building["lands"]
            if int(row.get("id", row.get("cid", 0))) == 36300005
        )
        self.assertEqual(land["status"], 1)
        self.assertEqual(land["currentSeedCid"], 0)
        self.assertFalse(module_handlers.dispatch(self.session, self.uid, 1886, request))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[2], before - 1000)

        storage.seed_player_num_attrs(self.uid, {5: 100000})
        pay_point_before = storage.get_player_num_attrs(self.uid)[5]
        pay_point_request = protocol_codec.encode_method(1886, 36000003, 36300007)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1886, pay_point_request,
        ))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[5], pay_point_before - 5)

    def test_home_trigger_plot_accepts_official_dialog_and_is_idempotent(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        plot_id = min(module_handlers.HOME_CONFIG["homePlotDialogCids"])
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 1810,
            protocol_codec.encode_method(1810, 99999999),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1810,
            protocol_codec.encode_method(1810, plot_id),
        ))
        code, returned_id = protocol_codec.decode_method(1842, self.session.messages[-1][1])
        self.assertEqual((code, returned_id), (0, plot_id))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1810,
            protocol_codec.encode_method(1810, plot_id),
        ))
        code, returned_id = protocol_codec.decode_method(1842, self.session.messages[-1][1])
        self.assertEqual((code, returned_id), (1, plot_id))
        self.assertEqual(
            storage.get_player_state_json(self.uid, "home")["plots"],
            [plot_id],
        )

    def test_home_unlock_dorm_rejected_for_castle_index_one(self):
        """CastleIndex=1 功能房（如 7 号）不允许通过新增宿舍入口重复解锁。"""
        storage.grant_reward_pairs(self.uid, [(11911, 200)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        for room_id in (7, 19):
            with self.subTest(room_id=room_id):
                self.assertFalse(module_handlers.dispatch(
                    self.session, self.uid, 1807,
                    protocol_codec.encode_method(1807, room_id),
                ), "CastleIndex=1 功能房应被拒绝")

    def test_home_unlock_dorm_insufficient_balance_atomic_rollback(self):
        """11911 不足时拒绝解锁，状态和库存均不做任何修改。"""
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state_before = storage.get_player_state_json(self.uid, "home")
        items_before = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        result = module_handlers.dispatch(
            self.session, self.uid, 1807,
            protocol_codec.encode_method(1807, 105),
        )
        self.assertFalse(result, "余额不足应拒绝")
        state_after = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(
            state_before, state_after,
            "余额不足时 home 状态不应改变",
        )
        items_after = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        }
        self.assertEqual(
            items_after.get(11911, 0),
            items_before.get(11911, 0),
            "余额不足时库存不应改变",
        )

    def test_home_enter_nonexistent_room_is_rejected(self):
        """进入不存在的房间必须返回失败，不能隐式创建。"""
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state_before = storage.get_player_state_json(self.uid, "home")
        count_before = len(state_before["rooms"])
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 1817,
            protocol_codec.encode_method(1817, 199, 1),
        ), "不存在的房间 199 应被拒绝")
        state_after = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(
            len(state_after["rooms"]), count_before,
            "不存在房间不应被隐式创建",
        )

    def test_home_enter_unlocked_dorm_succeeds(self):
        """进入已解锁宿舍 101 应返回成功 1849。"""
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        data = storage.get_player_state_json(self.uid, "home")
        data["rooms"].append(module_handlers._initial_home_room(101, 160000))
        self.assertTrue(storage.update_player_state_json(self.uid, "home", data))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1817,
            protocol_codec.encode_method(1817, 101, 1),
        ))
        code, = protocol_codec.decode_method(1849, self.session.messages[-1][1])
        self.assertEqual(code, 0)

    def test_home_plant_complete_cancel_and_harvest_use_building_lands(self):
        storage.grant_reward_pairs(self.uid, [(11201, 3)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        seed_before = next(
            row["quantity"] for row in storage.get_items(self.uid)
            if row["template_id"] == 11201
        )
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1806,
            protocol_codec.encode_method(1806, 36000003, 36300001, 1),
        ))
        code, building_cid, land = protocol_codec.decode_method(
            1838, self.session.messages[-1][1],
        )
        self.assertEqual((code, building_cid), (0, 36000003))
        self.assertEqual(land["cid"], 36300001)
        self.assertEqual(land["currentSeedCid"], 1)
        self.assertEqual(land["status"], 3)
        state = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(state.get("lands", []), [])
        building = next(row for row in state["buildings"] if row["id"] == 36000003)
        planted = next(row for row in building["lands"] if row["cid"] == 36300001)
        self.assertEqual(planted["grow_time"], 14400)
        self.assertEqual(planted["finishTime"], planted["planted_at"] + 14400)
        seed_after = next(
            row["quantity"] for row in storage.get_items(self.uid)
            if row["template_id"] == 11201
        )
        self.assertEqual(seed_after, seed_before - 1)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1823,
            protocol_codec.encode_method(1823, 36000003, 36300001, 1),
        ))
        code, building_cid, mature = protocol_codec.decode_method(
            1855, self.session.messages[-1][1],
        )
        self.assertEqual((code, building_cid, mature["status"]), (0, 36000003, 5))
        reward_before = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(11811, 0)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1805,
            protocol_codec.encode_method(1805, 36000003, 36300001),
        ))
        result = next(
            protocol_codec.decode_method(1837, body)
            for message_id, body in reversed(self.session.messages)
            if message_id == 1837
        )
        self.assertEqual(result[0:2], [0, 36000003])
        self.assertEqual(result[2][0]["status"], 1)
        self.assertEqual(result[2][0]["currentSeedCid"], 0)
        self.assertEqual(result[3], [{"cid": 11811, "num": 5, "tag": 0}])
        reward_after = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(11811, 0)
        self.assertEqual(reward_after, reward_before + 5)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1806,
            protocol_codec.encode_method(1806, 36000003, 36300002, 1),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1824,
            protocol_codec.encode_method(1824, 36000003, 36300002),
        ))
        code, building_cid, cancelled = protocol_codec.decode_method(
            1856, self.session.messages[-1][1],
        )
        self.assertEqual((code, building_cid), (0, 36000003))
        self.assertEqual(cancelled["status"], 1)
        self.assertEqual(cancelled["currentSeedCid"], 0)
        state = storage.get_player_state_json(self.uid, "home")
        building = next(row for row in state["buildings"] if row["id"] == 36000003)
        self.assertEqual(len(building["lands"]), 4)

    def test_home_harvest_all_and_enter_advance_mature_status(self):
        storage.grant_reward_pairs(self.uid, [(11201, 2)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        for land_id in (36300001, 36300002):
            self.assertTrue(module_handlers.dispatch(
                self.session, self.uid, 1806,
                protocol_codec.encode_method(1806, 36000003, land_id, 1),
            ))
        state = storage.get_player_state_json(self.uid, "home")
        building = next(row for row in state["buildings"] if row["id"] == 36000003)
        for land in building["lands"][:2]:
            land["finishTime"] = 1
            land["planted_at"] = 1
        storage.update_player_state_json(self.uid, "home", state)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        _, home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        building_pod = next(row for row in home["buildings"] if row["id"] == 36000003)
        self.assertEqual([row["status"] for row in building_pod["lands"][:2]], [5, 5])
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1805,
            protocol_codec.encode_method(1805, 36000003, -1),
        ))
        result = next(
            protocol_codec.decode_method(1837, body)
            for message_id, body in reversed(self.session.messages)
            if message_id == 1837
        )
        self.assertEqual(len(result[2]), 2)
        self.assertEqual(result[3], [{"cid": 11811, "num": 10, "tag": 0}])

    def test_home_affair_refresh_start_finish_reward_and_undo(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        _, home = protocol_codec.decode_method(1834, self.session.messages[-1][1])
        office_building = next(row for row in home["buildings"] if row["id"] == 36000006)
        affair = office_building["officePOD"]["affairs"][0]
        original_cid = affair["cid"]
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1833,
            protocol_codec.encode_method(1833, affair["id"], 36000006, True),
        ))
        self.assertEqual(self.session.messages[-2][0], 1865)
        self.assertEqual(self.session.messages[-1][0], 1870)
        refreshed = protocol_codec.decode_method(1870, self.session.messages[-1][1])[0]
        affair = refreshed["officePOD"]["affairs"][0]
        self.assertNotEqual(affair["cid"], original_cid)
        self.assertEqual(refreshed["officePOD"]["freeRefreshTimes"], 3)

        soul_cid = storage.get_souls(self.uid)[0]["soul_id"]
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1821,
            protocol_codec.encode_method(1821, 36000006, affair["id"], [soul_cid]),
        ))
        code, started, count = protocol_codec.decode_method(1853, self.session.messages[-1][1])
        self.assertEqual((code, count), (0, 1))
        working = started["officePOD"]["affairs"][0]
        self.assertEqual(working["status"], 1)
        self.assertEqual(working["soulCids"], [soul_cid])

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1878,
            protocol_codec.encode_method(1878, affair["id"], 36000006),
        ))
        self.assertEqual(self.session.messages[-2][0], 1880)
        self.assertEqual(self.session.messages[-1][0], 1870)
        undone = protocol_codec.decode_method(1870, self.session.messages[-1][1])[0]
        self.assertEqual(undone["officePOD"]["affairs"][0]["status"], 0)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1821,
            protocol_codec.encode_method(1821, 36000006, affair["id"], [soul_cid]),
        ))
        state = storage.get_player_state_json(self.uid, "home")
        building = next(row for row in state["buildings"] if row["id"] == 36000006)
        persisted = building["officePOD"]["affairs"][0]
        persisted["finishTime"] = 1
        storage.update_player_state_json(self.uid, "home", state)
        rule = module_rules._row("homeland", "TransactionListTable", persisted["cid"])
        expected = module_rules._pairs(rule["Reward"])
        before_attrs = storage.get_player_num_attrs(self.uid)
        before_items = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1822,
            # The overview button uses -1 to request all completed affairs.
            protocol_codec.encode_method(1822, 36000006, -1),
        ))
        code, rewarded, reward_results = protocol_codec.decode_method(
            1854, self.session.messages[-1][1],
        )
        self.assertEqual(code, 0)
        self.assertEqual(reward_results[0]["affairId"], affair["id"])
        self.assertEqual(
            [(row["cid"], row["num"]) for row in reward_results[0]["itemAward"]],
            expected,
        )
        self.assertEqual(rewarded["officePOD"]["affairs"][0]["status"], 0)
        for cid, quantity in expected:
            if cid in before_attrs:
                self.assertEqual(storage.get_player_num_attrs(self.uid)[cid], before_attrs[cid] + quantity)
            else:
                after_items = {
                    row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
                }
                self.assertEqual(after_items.get(cid, 0), before_items.get(cid, 0) + quantity)

    def test_home_affair_quick_start_assigns_maximum_unique_idle_souls(self):
        storage.seed_companions_from_snapshot(self.uid, [
            {"cid": 20010000 + index, "lv": 70, "favor": 100, "favorLv": 15}
            for index in range(1, 13)
        ])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1821,
            protocol_codec.encode_method(1821, 36000006, 0, []),
        ))
        code, building, count = protocol_codec.decode_method(
            1853, self.session.messages[-1][1],
        )
        self.assertEqual(code, 0)
        self.assertEqual(count, 4)
        working = [
            affair for affair in building["officePOD"]["affairs"]
            if affair["status"] == 1
        ]
        self.assertEqual(len(working), count)
        self.assertEqual([affair["cid"] for affair in working], [1, 2, 3, 4])
        self.assertTrue(all(len(affair["soulCids"]) == 3 for affair in working))
        assigned = [soul for affair in working for soul in affair["soulCids"]]
        self.assertEqual(len(assigned), len(set(assigned)))
        persisted = storage.get_player_state_json(self.uid, "home")
        self.assertEqual(persisted["today_home_work_count"], count)
        persisted_office = next(
            row["officePOD"] for row in persisted["buildings"]
            if row["id"] == 36000006
        )
        self.assertEqual(
            sum(affair["status"] == 1 for affair in persisted_office["affairs"]),
            count,
        )

    def test_home_affair_quick_start_returns_zero_when_idle_souls_below_three(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        with mock.patch.object(
            module_handlers, "_known_home_soul_cids", return_value={20010001, 20010002},
        ):
            self.assertTrue(module_handlers.dispatch(
                self.session, self.uid, 1821,
                protocol_codec.encode_method(1821, 36000006, 0, []),
            ))
        code, building, count = protocol_codec.decode_method(
            1853, self.session.messages[-1][1],
        )
        self.assertEqual((code, count), (0, 0))
        self.assertTrue(all(
            affair["status"] == 0
            for affair in building["officePOD"]["affairs"]
        ))

    def test_home_affair_quick_start_does_not_reuse_busy_souls(self):
        storage.seed_companions_from_snapshot(self.uid, [
            {"cid": 20010000 + index, "lv": 70, "favor": 100, "favorLv": 15}
            for index in range(1, 13)
        ])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        state = storage.get_player_state_json(self.uid, "home")
        building = next(row for row in state["buildings"] if row["id"] == 36000006)
        busy_souls = [20010001, 20010002, 20010003]
        building["officePOD"]["affairs"][0].update({
            "status": 1,
            "finishTime": 4000000000,
            "soulCids": busy_souls,
        })
        storage.update_player_state_json(self.uid, "home", state)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1821,
            protocol_codec.encode_method(1821, 36000006, 0, []),
        ))
        _code, updated, count = protocol_codec.decode_method(
            1853, self.session.messages[-1][1],
        )
        self.assertEqual(count, 3)
        newly_assigned = [
            soul
            for affair in updated["officePOD"]["affairs"]
            if affair["status"] == 1 and affair["id"] != 1
            for soul in affair["soulCids"]
        ]
        self.assertTrue(set(busy_souls).isdisjoint(newly_assigned))

    def test_home_cook_queue_cancel_complete_reward_and_building_update(self):
        recipe = module_rules._row("homeland", "CookCombinationTable", 1)
        ingredients = module_rules._pairs(recipe["NeedItem"])
        for cid, quantity in ingredients:
            storage.grant_reward_pairs(self.uid, [(cid, quantity * 2)])
        before_ingredients = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1812,
            protocol_codec.encode_method(1812, 36000005, 1, 1, 1),
        ))
        code, building = protocol_codec.decode_method(1844, self.session.messages[-1][1])
        self.assertEqual(code, 0)
        self.assertEqual(len(building["kitchenPOD"]["culinarys"]), 1)
        culinary = building["kitchenPOD"]["culinarys"][0]
        self.assertEqual(culinary["id"], 1)
        self.assertEqual(culinary["cid"], 1)
        self.assertEqual(culinary["singleCookTime"], 600)
        self.assertEqual(culinary["finishTime"], culinary["startTime"] + 600)
        after_ingredients = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }
        for cid, quantity in ingredients:
            self.assertEqual(after_ingredients[cid], before_ingredients[cid] - quantity)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1813,
            protocol_codec.encode_method(1813, 36000005, 1),
        ))
        _, cancelled = protocol_codec.decode_method(1845, self.session.messages[-1][1])
        self.assertEqual(cancelled["kitchenPOD"]["culinarys"], [])

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1812,
            protocol_codec.encode_method(1812, 36000005, 1, 1, 1),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1820,
            protocol_codec.encode_method(1820, 36000005, 1, 601),
        ))
        _, completed = protocol_codec.decode_method(1852, self.session.messages[-1][1])
        self.assertEqual(completed["kitchenPOD"]["culinarys"][0]["status"], 2)
        reward_cid, reward_quantity = module_rules._pairs(recipe["ItemId"])[0]
        reward_before = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(reward_cid, 0)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1814,
            protocol_codec.encode_method(1814, 36000005, 1),
        ))
        code, rewarded, rewards = protocol_codec.decode_method(1846, self.session.messages[-1][1])
        self.assertEqual(code, 0)
        self.assertEqual(rewarded["kitchenPOD"]["culinarys"], [])
        self.assertEqual([(row["cid"], row["num"]) for row in rewards], [(reward_cid, reward_quantity)])
        reward_after = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(reward_cid, 0)
        self.assertEqual(reward_after, reward_before + reward_quantity)

    def test_home_building_upgrade_consumes_next_level_cost_and_pushes_pod(self):
        storage.grant_reward_pairs(self.uid, [(11911, 60)])
        before = next(
            row["quantity"] for row in storage.get_items(self.uid)
            if row["template_id"] == 11911
        )
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1877,
            protocol_codec.encode_method(1877, 36000005),
        ))
        self.assertEqual(self.session.messages[-2][0], 1879)
        self.assertEqual(protocol_codec.decode_method(1879, self.session.messages[-2][1]), [0])
        self.assertEqual(self.session.messages[-1][0], 1870)
        building = protocol_codec.decode_method(1870, self.session.messages[-1][1])[0]
        self.assertEqual(building["id"], 36000005)
        self.assertEqual(building["lv"], 2)
        self.assertEqual(building["kitchenPOD"]["maxQueueCount"], 2)
        after = {
            row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)
        }.get(11911, 0)
        self.assertEqual(after, before - 60)
        state = storage.get_player_state_json(self.uid, "home")
        persisted = next(row for row in state["buildings"] if row["id"] == 36000005)
        self.assertEqual(persisted["lv"], 2)
        self.assertEqual(persisted["kitchenPOD"]["maxQueueCount"], 2)
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 1877,
            protocol_codec.encode_method(1877, 36000005),
        ))
        self.assertEqual(storage.get_player_state_json(self.uid, "home")["buildings"][3]["lv"], 2)

    def test_home_remaining_actions_persist_and_claim_once(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1802, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1816, protocol_codec.encode_method(1816, 7, "厨房")))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1817, protocol_codec.encode_method(1817, 7, 1)))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1832,
            protocol_codec.encode_method(1832, 7, [{"cid": 1001, "x": 2, "y": 3}]),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1815, protocol_codec.encode_method(1815, 9),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1825, protocol_codec.encode_method(1825, 1, 999, 1),
        ))
        state = storage.get_player_state_json(self.uid, "home")
        making_building = next(
            row for row in state["buildings"]
            if int(row.get("id", 0)) == 1
        )
        making_building["making"]["finish_at"] = 0
        storage.update_player_state_json(self.uid, "home", state)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1827, protocol_codec.encode_method(1827, 1, 999),
        ))
        quantity_after_first = storage.get_items(self.uid)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1830, protocol_codec.encode_method(1830, 44),
        ))
        quantity_with_chest = {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}.get(1, 0)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1830, protocol_codec.encode_method(1830, 44),
        ))
        self.assertEqual(
            quantity_with_chest,
            {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}.get(1, 0),
        )
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1803, b""))
        state = storage.get_player_state_json(self.uid, "home")
        room = next(
            row for row in state["rooms"]
            if int(row.get("cid", 0)) == 7
        )
        self.assertEqual(room["name"], "厨房")
        self.assertEqual(room["decorates"][0]["cid"], 1001)

    def test_formation_name_prefab_and_copy_persist(self):
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 4402, protocol_codec.encode_method(4402, 1, "主队"),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 4403,
            protocol_codec.encode_method(4403, 1, 20010001, 1, 1, [1], 0),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 4407, protocol_codec.encode_method(4407, 1, 2),
        ))
        formations = storage.get_player_state_json(self.uid, "formations")
        self.assertEqual(formations[0]["name"], "主队")
        self.assertEqual(formations[1]["prefabs"], [1])

    def test_town_and_soul_memory_stateful_results(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2204, protocol_codec.encode_method(2204, 12)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2203, protocol_codec.encode_method(2203, 1, 2)))
        storage.seed_companions_from_snapshot(self.uid, [{"cid": 20010003, "lv": 70, "favor": 0, "favorLv": 3}])
        storage.grant_reward_pairs(self.uid, [(10711, 4), (10712, 4), (10713, 4), (10714, 4)])
        for piece_id in (300011, 300012, 300013, 300014):
            self.assertTrue(module_handlers.dispatch(
                self.session, self.uid, 3602,
                protocol_codec.encode_method(3602, 300010, piece_id),
            ))
        self.assertEqual(
            [message_id for message_id, _ in self.session.messages[-8:]],
            [3606, 3610, 3606, 3610, 3606, 3610, 3606, 3610],
        )
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 3603, protocol_codec.encode_method(3603, 300010)))
        self.assertEqual([message_id for message_id, _ in self.session.messages[-2:]], [3607, 3610])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 3604, protocol_codec.encode_method(3604, 300010)))
        self.assertEqual([message_id for message_id, _ in self.session.messages[-3:]], [3610, 3610, 3608])
        notified_current = protocol_codec.decode_method(3610, self.session.messages[-3][1])[0]
        notified_next = protocol_codec.decode_method(3610, self.session.messages[-2][1])[0]
        self.assertEqual((notified_current["cid"], notified_next["cid"]), (300010, 300020))
        state = storage.get_player_state_json(self.uid, "soul_memory")
        chapter = state["chapters"]["300010"]
        self.assertEqual(chapter["unlockPieceCids"], [300011, 300012, 300013, 300014])
        self.assertTrue(chapter["isExperience"])
        self.assertTrue(chapter["isGetReward"])
        reward_body = next(
            body for message_id, body in reversed(self.session.messages) if message_id == 3608
        )
        code, chapter_id, rewards, current, new_chapter = protocol_codec.decode_method(3608, reward_body)
        self.assertEqual((code, chapter_id), (0, 300010))
        self.assertEqual(rewards, [{"cid": 2, "num": 30, "tag": 0}, {"cid": 5003101, "num": 1, "tag": 0}])
        self.assertTrue(current["isGetReward"])
        self.assertEqual(new_chapter["cid"], 300020)
        self.assertEqual(storage.get_player_state_json(self.uid, "town")["last_area"], 12)

    def test_town_mainline_completion_refreshes_next_event(self):
        storage.update_player_state_json(
            self.uid, "quickChallenge", [25020102, 25020103],
        )
        initial = module_handlers._town_state(self.uid)
        self.assertIn(10020101, initial["executable_events"])
        self.session.messages.clear()
        dialog_id = int(module_handlers._town_event(10020101)["DialogId"])
        select_index, skip_indexes, _ = self._town_dialog_request_from(dialog_id)
        expected_path = module_handlers._town_dialog_replay(
            self.uid, dialog_id, skip_indexes, select_index,
        )
        self.assertIsNotNone(expected_path)
        expected_rewards = dict(
            module_handlers._town_dialog_reward_pairs(expected_path["services"])
        )
        self.assertTrue(expected_rewards)
        before_rewards = {
            item_id: self._item_quantity(item_id)
            for item_id in expected_rewards
        }
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 2203,
            protocol_codec.encode_method(2203, 10070, 10020101),
        ))
        self.assertEqual(
            [message_id for message_id, _ in self.session.messages[-2:]],
            [2206, 1604],
        )
        self.assertIsNotNone(
            storage.get_player_state_json(self.uid, "town")["pending_story"]
        )
        self.assertEqual(
            before_rewards,
            {item_id: self._item_quantity(item_id) for item_id in expected_rewards},
        )
        self.assertEqual(self._finish_town_story(), 0)
        self.assertEqual(
            [
                message_id for message_id, _ in self.session.messages
                if message_id in (2209, 1603, 2211)
            ][-3:],
            [2209, 1603, 2211],
        )
        self.assertIsNone(
            storage.get_player_state_json(self.uid, "town")["pending_story"]
        )
        self.assertIsNone(getattr(self.session, "active_story", None))
        self.assertEqual(
            {
                item_id: self._item_quantity(item_id)
                for item_id in expected_rewards
            },
            {
                item_id: before_rewards[item_id] + quantity
                for item_id, quantity in expected_rewards.items()
            },
        )
        refreshed_body = next(
            body for message_id, body in reversed(self.session.messages)
            if message_id == 2209
        )
        refreshed = protocol_codec.decode_method(2209, refreshed_body)[0]
        self.assertIn(10000101, refreshed)
        saved = storage.get_player_state_json(self.uid, "town")
        self.assertIn(10000101, saved["executable_events"])
        self.assertIn(10020101, storage.get_player_state_json(self.uid, "unlockTownEvents"))
        reward_snapshot = {
            item_id: self._item_quantity(item_id) for item_id in expected_rewards
        }
        self.assertFalse(module_handlers.handle_town_dialog(self.session, self.uid, 1, []))
        self.assertEqual(
            reward_snapshot,
            {item_id: self._item_quantity(item_id) for item_id in expected_rewards},
        )

    def test_town_shopping_uses_five_event_baseline_and_refreshes_after_completion(self):
        storage.seed_player_num_attrs(self.uid, {101: 2})
        initial = module_handlers._town_state(self.uid)
        self.assertEqual(
            initial["shopping_event_ids"],
            list(module_handlers.TOWN_DEFAULT_SHOPPING_EVENT_IDS),
        )
        area = module_handlers._town_area(10040)
        reward_ids = module_handlers._town_patrol_awards(area)
        before_rewards = {item_id: self._item_quantity(item_id) for item_id, _ in reward_ids}
        before_cost = storage.get_player_num_attrs(self.uid)[101]
        self.session.messages.clear()
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 2202,
            protocol_codec.encode_method(2202, 403528),
        ))
        self.assertEqual(
            [message_id for message_id, _ in self.session.messages[-2:]],
            [2205, 1604],
        )
        self.assertEqual(storage.get_player_num_attrs(self.uid)[101], before_cost - 1)
        self.assertEqual(
            before_rewards,
            {item_id: self._item_quantity(item_id) for item_id in before_rewards},
        )
        self.assertGreater(self._finish_town_story(choice_selection=2), 0)
        self.assertEqual(
            [
                message_id for message_id, _ in self.session.messages
                if message_id in (2213, 1603, 2210)
            ][-3:],
            [2213, 1603, 2210],
        )
        refresh_body = next(
            body for message_id, body in reversed(self.session.messages)
            if message_id == 2213
        )
        refreshed = protocol_codec.decode_method(2213, refresh_body)[0]
        self.assertEqual(len(refreshed["shoppingEventIds"]), 5)
        self.assertNotIn(403528, refreshed["shoppingEventIds"])
        self.assertEqual(
            storage.get_player_state_json(self.uid, "town")["shopping_event_ids"],
            refreshed["shoppingEventIds"],
        )
        self.assertEqual(
            {
                item_id: self._item_quantity(item_id)
                for item_id in before_rewards
            },
            {
                item_id: before_rewards[item_id] + quantity
                for item_id, quantity in reward_ids
            },
        )

    def test_town_refreshes_at_four_am_once_and_keeps_completed_before_boundary(self):
        local_now = time.localtime()
        four_am = int(time.mktime((
            local_now.tm_year, local_now.tm_mon, local_now.tm_mday,
            4, 0, 0, 0, 0, -1,
        )))
        before = four_am - 1
        after = four_am

        data = {
            "town_day": module_handlers._town_day(before),
            "completed_shopping": [403528],
            "shopping_event_ids": [403528],
        }
        module_handlers._town_refresh_shopping(data, before)
        self.assertEqual(data["town_day"], module_handlers._town_day(before))
        self.assertEqual(data["completed_shopping"], [403528])

        module_handlers._town_refresh_shopping(data, after)
        first_after_refresh = list(data["shopping_event_ids"])
        self.assertEqual(data["town_day"], module_handlers._town_day(after))
        self.assertEqual(data["completed_shopping"], [])
        self.assertEqual(
            len(first_after_refresh), module_handlers.TOWN_SHOPPING_EVENT_COUNT,
        )

        module_handlers._town_refresh_shopping(data, after)
        self.assertEqual(data["shopping_event_ids"], first_after_refresh)
        self.assertEqual(data["completed_shopping"], [])

    def test_town_patrol_tickets_refill_to_five_after_four_am_once(self):
        storage.seed_player_num_attrs(self.uid, {101: 1})
        local_now = time.localtime()
        four_am = int(time.mktime((
            local_now.tm_year, local_now.tm_mon, local_now.tm_mday,
            4, 0, 0, 0, 0, -1,
        )))
        before = four_am - 1
        after = four_am
        storage.update_player_state_json(
            self.uid, "town", {"town_day": module_handlers._town_day(before)},
        )
        with mock.patch.object(module_handlers, "_stamp", return_value=before):
            module_handlers._town_state(self.uid)
        self.assertEqual(storage.get_player_num_attrs(self.uid).get(101), 1)

        with mock.patch.object(module_handlers, "_stamp", return_value=after):
            module_handlers._town_state(self.uid)
        self.assertEqual(storage.get_player_num_attrs(self.uid).get(101), 5)

        with mock.patch.object(module_handlers, "_stamp", return_value=after):
            module_handlers._town_state(self.uid)
        self.assertEqual(storage.get_player_num_attrs(self.uid).get(101), 5)

    def test_soul_memory_rebuilds_all_owned_root_chapters(self):
        soul_ids = sorted({
            int(row["SoulId"])
            for row in module_handlers.SOUL_MEMORY_CONFIG["chapters"].values()
        })
        storage.seed_companions_from_snapshot(self.uid, [
            {"cid": soul_id, "lv": 70, "favor": 0, "favorLv": 1}
            for soul_id in soul_ids
        ])
        state = module_handlers.rebuild_memory_state(self.uid)
        roots = {
            int(row["Id"])
            for row in module_handlers.SOUL_MEMORY_CONFIG["chapters"].values()
            if int(row.get("PreMemoryChapter", 0)) == 0
        }
        self.assertEqual(len(roots), 55)
        self.assertTrue(roots.issubset({int(key) for key in state["chapters"]}))

    def test_soul_memory_rebuild_preserves_progress_and_requires_rewarded_predecessor(self):
        storage.seed_companions_from_snapshot(
            self.uid, [{"cid": 20010006, "lv": 70, "favor": 0, "favorLv": 50}],
        )
        storage.update_player_state_json(self.uid, "soul_memory", {"chapters": {
            "600010": {
                "cid": 600010, "isExperience": True, "isGetReward": False,
                "isNew": False, "unlockPieceCids": [600011, 600012, 600013, 600014],
            }
        }})
        state = module_handlers.rebuild_memory_state(self.uid)
        self.assertNotIn("600020", state["chapters"])
        state["chapters"]["600010"]["isGetReward"] = True
        storage.update_player_state_json(self.uid, "soul_memory", state)
        rebuilt = module_handlers.rebuild_memory_state(self.uid)
        self.assertIn("600020", rebuilt["chapters"])
        self.assertNotIn("600030", rebuilt["chapters"])
        self.assertEqual(
            rebuilt["chapters"]["600010"]["unlockPieceCids"],
            [600011, 600012, 600013, 600014],
        )
        self.assertTrue(rebuilt["chapters"]["600010"]["isExperience"])
        self.assertTrue(rebuilt["chapters"]["600010"]["isGetReward"])

    def test_soul_memory_rejects_out_of_order_and_requires_all_pieces(self):
        storage.grant_reward_pairs(self.uid, [(10711, 4), (10712, 4), (10713, 4), (10714, 4)])
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 3602,
            protocol_codec.encode_method(3602, 300010, 300012),
        ))
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 3602,
            protocol_codec.encode_method(3602, 300010, 999999),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 3602,
            protocol_codec.encode_method(3602, 300010, 300011),
        ))
        self.assertFalse(module_handlers.dispatch(
            self.session, self.uid, 3603,
            protocol_codec.encode_method(3603, 300010),
        ))

    def test_evil_erosion_prefab_actions_persist(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6905, protocol_codec.encode_method(6905, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6902, protocol_codec.encode_method(6902, 1, 1001, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6907, protocol_codec.encode_method(6907, 1, 2)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6909, protocol_codec.encode_method(6909, 1, [11, 12])))
        state = storage.get_player_state_json(self.uid, "evil_erosion")
        self.assertEqual(state["prefabs"]["1"]["formationPos"], 2)
        self.assertEqual(state["prefabs"]["1"]["customSkills"], [11, 12])

    def test_remaining_maps_are_stateful_and_typed(self):
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5902, protocol_codec.encode_method(5902, 7)))
        _, dream = protocol_codec.decode_method(5905, self.session.messages[-1][1])
        self.assertEqual(dream["mapId"], 7)
        self.assertEqual(len(dream["cells"]), 54)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5904, protocol_codec.encode_method(5904, 1, 2)))
        storage.grant_reward_pairs(self.uid, [(311, 3)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7602, b""))
        _, mining = protocol_codec.decode_method(7607, self.session.messages[-1][1])
        self.assertEqual(mining["floor"], 1)
        self.assertEqual(len(mining["grids"]), 140)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6602, protocol_codec.encode_method(6602, 1)))
        _, tower = protocol_codec.decode_method(6605, self.session.messages[-1][1])
        self.assertEqual(tower["mapId"], 1)

        state = storage.get_player_state_json(self.uid, "remaining_modules")
        self.assertEqual(state["modules"]["net_dreamMap"]["data"]["roleX"], 1)
        self.assertEqual(state["modules"]["net_dreamMap"]["data"]["roleY"], 2)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1702, protocol_codec.encode_method(1702, 2)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 1705, protocol_codec.encode_method(1705, 1)))
        state = storage.get_player_state_json(self.uid, "remaining_modules")
        self.assertEqual(state["modules"]["net_miniGame"]["actions"]["1702"]["lastValues"], [2])

    def test_low_frequency_handlers_echo_and_persist_official_fields(self):
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1002,
            protocol_codec.encode_method(1002, 77, "maze-order"),
        ))
        self.assertEqual(protocol_codec.decode_method(1005, self.session.messages[-1][1]), [0])
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1003,
            protocol_codec.encode_method(1003, "battle-1", "battle-order"),
        ))
        self.assertEqual(protocol_codec.decode_method(1007, self.session.messages[-1][1]), [0])

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1702, protocol_codec.encode_method(1702, 12),
        ))
        self.assertEqual(protocol_codec.decode_method(1703, self.session.messages[-1][1]), [0, 12])
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 1705, protocol_codec.encode_method(1705, 9),
        ))
        self.assertEqual(protocol_codec.decode_method(1706, self.session.messages[-1][1]), [0, 9])

        state = storage.get_player_state_json(self.uid, "remaining_modules")["modules"]
        self.assertEqual(state["net_remoteLogic"]["data"]["lastMazeOrder"]["mazeId"], 77)
        self.assertEqual(state["net_miniGame"]["data"]["lastCardCfgIndex"], 9)

    def test_daily_supply_enforces_window_cap_and_once_per_reset_day(self):
        storage.seed_player_num_attrs(self.uid, {104: 400})
        noon = int(__import__("time").mktime((2026, 7, 27, 13, 0, 0, 0, 0, -1)))
        body = protocol_codec.encode_method(3702, 1, True)
        with mock.patch.object(module_handlers.time, "time", return_value=noon):
            self.assertTrue(module_handlers.dispatch(self.session, self.uid, 3702, body))
            result_body = next(body for mid, body in reversed(self.session.messages) if mid == 3703)
            self.assertEqual(protocol_codec.decode_method(3703, result_body), [0, 1])
            self.assertEqual(storage.get_player_num_attrs(self.uid)[104], 450)
            self.assertTrue(module_handlers.dispatch(self.session, self.uid, 3702, body))
            result_body = next(body for mid, body in reversed(self.session.messages) if mid == 3703)
            self.assertEqual(protocol_codec.decode_method(3703, result_body), [1, 1])
        self.assertEqual(storage.get_player_state_json(self.uid, "dailySupplyList"), [1])

    def test_abyss_runes_are_unique_and_persisted(self):
        body = protocol_codec.encode_method(7107, [101, 202])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7107, body))
        self.assertEqual(protocol_codec.decode_method(7108, self.session.messages[-1][1]), [0])
        self.assertEqual(storage.get_player_state_json(self.uid, "abyss_plus")["usedRunes"], [101, 202])
        duplicate = protocol_codec.encode_method(7107, [101, 101])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7107, duplicate))
        self.assertEqual(protocol_codec.decode_method(7108, self.session.messages[-1][1]), [1])

    def test_remaining_rewards_are_persisted_and_idempotent(self):
        storage.seed_player_num_attrs(self.uid, {1: 1000})
        body = protocol_codec.encode_method(5103, 100, 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5103, body))
        first = storage.get_player_num_attrs(self.uid)[1]
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5103, body))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[1], first)
        _, rewards, _ = protocol_codec.decode_method(5106, self.session.messages[-1][1])
        self.assertEqual(rewards, [])
        state = storage.get_player_state_json(self.uid, "remaining_operations")
        self.assertIn("100:1", state["image_puzzle"]["unlock_claims"])

    def test_extended_rewards_and_battle_claims_are_bound_to_their_context(self):
        # Gacha uses the extracted action/pack data and the local drop table;
        # the draw counter advances only after the cost transaction succeeds.
        storage.grant_reward_pairs(self.uid, [(10005, 1)])
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 5402, protocol_codec.encode_method(5402, 999901),
        ))
        self.assertEqual(storage.get_player_state_json(self.uid, "remaining_operations")["gacha"]["draws"]["999901"], 1)

        # A reward request cannot attach itself to an unrelated type-2 battle.
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 5503, protocol_codec.encode_method(5503, 1, 1, []),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 5502, protocol_codec.encode_method(5502, 1, 1, 1, 1),
        ))
        double_battle = storage.get_active_battle(self.uid, 2)
        self.assertIsNotNone(double_battle)
        storage.settle_battle(self.uid, double_battle["id"], 1, rounds=1)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 5503, protocol_codec.encode_method(5503, 1, 1, [7]),
        ))
        after_double = storage.get_player_num_attrs(self.uid)[1]
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 5503, protocol_codec.encode_method(5503, 1, 1, [8]),
        ))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[1], after_double)

        # Survival settlement is tied to its pending battle ID, not merely to
        # whichever type-2 battle happens to be latest.
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9402, protocol_codec.encode_method(9402, 3),
        ))
        survival_battle = storage.get_active_battle(self.uid, 2)
        self.assertIsNotNone(survival_battle)
        storage.settle_battle(self.uid, survival_battle["id"], 1, rounds=1)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9403, protocol_codec.encode_method(9403, 3, 100, 2, 1),
        ))
        before_retry = storage.get_player_num_attrs(self.uid)[1]
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9403, protocol_codec.encode_method(9403, 3, 999, 9, 9),
        ))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[1], before_retry)

    def test_recovered_activity_claims_and_unlock_boundaries(self):
        # Battle pass rewards are level-gated and each free/pay lane is
        # independently idempotent.
        storage.update_player_state_json(self.uid, "battle_pass", {
            "season": 1, "level": 1, "exp": 0, "advanced": True,
            "claimedFree": [], "claimedPay": [], "lastSeasonClaimed": False,
        })
        reward_body = protocol_codec.encode_method(4802, [1001])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 4802, reward_body))
        before = storage.get_player_num_attrs(self.uid)[1]
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 4802, reward_body))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[1], before)

        # A panda event must belong to the current six-event exploration set;
        # replaying a completed event cannot grant a second reward.
        storage.grant_reward_pairs(self.uid, [(403, 2)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6004, b""))
        panda = storage.get_player_state_json(self.uid, "panda")
        event_id = panda["events"][0]
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6006,
            protocol_codec.encode_method(6006, 99999999, []),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6006,
            protocol_codec.encode_method(6006, event_id, []),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6006,
            protocol_codec.encode_method(6006, event_id, []),
        ))
        event_result = next(body for message_id, body in reversed(self.session.messages) if message_id == 6011)
        self.assertEqual(protocol_codec.decode_method(6011, event_result)[0], 1)

        # Plot challenge nodes are sequential and the boss stays locked until
        # the configured unlock node is actually passed.
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6402,
            protocol_codec.encode_method(6402, 1003),
        ))
        self.assertEqual(protocol_codec.decode_method(6405, self.session.messages[-1][1])[0], 1)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6402,
            protocol_codec.encode_method(6402, 1001),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 6404,
            protocol_codec.encode_method(6404, 1),
        ))
        self.assertEqual(protocol_codec.decode_method(6407, self.session.messages[-1][1])[0], 1)

    def test_turntable_flight_and_puzzle_rewards_are_idempotent(self):
        storage.grant_reward_pairs(self.uid, [(426, 3)])
        draw = protocol_codec.encode_method(7202, 1, 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7202, draw))
        first_cost = storage.get_player_num_attrs(self.uid).get(426, 0)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7202, draw))
        second_cost = storage.get_player_num_attrs(self.uid).get(426, 0)
        self.assertLess(second_cost, first_cost)

        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 8002, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 8002, b""))
        self.assertEqual(protocol_codec.decode_method(8006, self.session.messages[-1][1])[0], 1)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 8004, protocol_codec.encode_method(8004, 1500, 10),
        ))
        reward_cid = 11125001
        first_reward = next((row["quantity"] for row in storage.get_items(self.uid) if row["template_id"] == reward_cid), 0)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 8002, b""))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 8004, protocol_codec.encode_method(8004, 1500, 10),
        ))
        second_reward = next((row["quantity"] for row in storage.get_items(self.uid) if row["template_id"] == reward_cid), 0)
        self.assertEqual(second_reward, first_reward)

        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9802, protocol_codec.encode_method(9802, 1001),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9809, protocol_codec.encode_method(9809, 1001, False, [1]),
        ))
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 9809, protocol_codec.encode_method(9809, 1001, True, [1]),
        ))

    def test_real_map_boundaries_and_resource_costs(self):
        # Dream map uses configured coordinates and rejects duplicate opening.
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5902, protocol_codec.encode_method(5902, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5904, protocol_codec.encode_method(5904, 1, 0)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5904, protocol_codec.encode_method(5904, 1, 0)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5911, protocol_codec.encode_method(5911, 1, 0, 2)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 5911, protocol_codec.encode_method(5911, 99, 99, 2)))

        # Magic tower has one local map and rejects non-adjacent or post-giveup moves.
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6602, protocol_codec.encode_method(6602, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6604, protocol_codec.encode_method(6604, 3)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6604, protocol_codec.encode_method(6604, 2)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6603, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 6604, protocol_codec.encode_method(6604, 3)))

        # Mining entry atomically consumes the configured layer cost and every
        # grid can only transition from untouched to excavated once.
        storage.grant_reward_pairs(self.uid, [(311, 3)])
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7602, b""))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7603, protocol_codec.encode_method(7603, 1)))
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 7603, protocol_codec.encode_method(7603, 1)))

    def test_mall_uses_local_currency_transaction(self):
        storage.seed_player_num_attrs(self.uid, {1: 1000})
        module_rules.MODULE_CONFIG["mall"]["MallTable"]["991233"] = {
            "Id": 991233, "SellType": 1, "Price": [1, 10],
            "Item": [12345], "ItemNum": [1], "TimeLimitType": 0,
        }
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2502, protocol_codec.encode_method(2502, 991233, 2)))
        self.assertEqual(storage.get_player_num_attrs(self.uid)[1], 99980)
        self.assertEqual(storage.get_items(self.uid)[0]["template_id"], 12345)
        self.assertFalse(module_handlers.dispatch(self.session, self.uid, 2502, protocol_codec.encode_method(2502, 991233, 0)))

    def test_unknown_mall_id_fails_closed(self):
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 2502,
            protocol_codec.encode_method(2502, 999999999, 1),
        ))
        self.assertEqual(storage.get_items(self.uid), [])

    def test_mall_compatibility_alias_resolves_to_outfit_item_exchange(self):
        row = module_handlers._mall_row(1010910103)
        source = module_handlers._mall_row(1010910101)
        self.assertEqual(row["Id"], 1010910103)
        self.assertEqual(row["SourceMallId"], 1010910101)
        self.assertEqual(row["SellType"], 1)
        self.assertEqual(row["Price"], [10032, 1])
        self.assertEqual(row.get("Item"), source.get("Item"))
        self.assertEqual(row.get("ItemNum"), source.get("ItemNum"))
        self.assertIsNone(module_handlers._mall_row(1010910102))

    def test_real_outfit_exchange_deducts_token_grants_rewards_and_limits_retry(self):
        storage.grant_reward_pairs(self.uid, [(10032, 2)])
        before = {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}
        body = protocol_codec.encode_method(2502, 1010910103, 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2502, body))
        code, mall_id, count, rewards = protocol_codec.decode_method(2503, self.session.messages[-1][1])
        self.assertEqual((code, mall_id, count), (0, 1010910103, 1))
        self.assertEqual(
            [(row["cid"], row["num"]) for row in rewards],
            [(61206, 1), (71265, 1), (34002, 1), (11140, 3), (11141, 5)],
        )
        after = {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}
        self.assertEqual(after[10032], before[10032] - 1)
        for cid, quantity in ((61206, 1), (71265, 1), (34002, 1), (11140, 3), (11141, 5)):
            self.assertEqual(after.get(cid, 0), before.get(cid, 0) + quantity)
        purchases = storage.get_player_state_json(self.uid, "mall")["purchases"]
        self.assertEqual(purchases["1010910101"]["count"], 1)
        self.assertNotIn("1010910103", purchases)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2502, body))
        retry_code = protocol_codec.decode_method(2503, self.session.messages[-1][1])[0]
        self.assertEqual(retry_code, 1)
        self.assertEqual(
            {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}, after,
        )

    def test_outfit_alias_ignores_legacy_alias_count_but_honors_source_limit(self):
        storage.grant_reward_pairs(self.uid, [(10032, 2)])
        storage.update_player_state_json(self.uid, "mall", {
            "purchases": {
                "1010910103": {
                    "period": "life", "count": 4, "updatedAt": 1,
                },
            },
        })
        body = protocol_codec.encode_method(2502, 1010910103, 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2502, body))
        code = protocol_codec.decode_method(2503, self.session.messages[-1][1])[0]
        self.assertEqual(code, 0)
        purchases = storage.get_player_state_json(self.uid, "mall")["purchases"]
        self.assertEqual(purchases["1010910103"]["count"], 4)
        self.assertEqual(purchases["1010910101"]["count"], 1)
        self.assertTrue(module_handlers.dispatch(self.session, self.uid, 2502, body))
        retry_code = protocol_codec.decode_method(2503, self.session.messages[-1][1])[0]
        self.assertEqual(retry_code, 1)

    def test_recharge_payload_resolves_paytable_goods_id_to_paid_product(self):
        payload = login_server._decode_request_payload(
            ("data=%%7B%%22uid%%22%%3A%%22%s%%22%%2C%%22goodsId%%22%%3A40098%%2C"
             "%%22orderId%%22%%3A%%22retry-1%%22%%7D" % self.uid).encode()
        )
        self.assertEqual(login_server._recharge_account(payload, "127.0.0.1")["uid"], self.uid)
        mall_id, period, plan = login_server._recharge_plan(payload)
        self.assertEqual(mall_id, 1010304114)
        self.assertEqual(period, "life")
        self.assertEqual(plan["payMoney"], 40098)
        self.assertEqual(plan["amount"], 98)
        self.assertTrue(plan["rewards"])

    def test_recharge_without_identity_falls_back_only_to_configured_alias_target(self):
        payload = login_server._decode_request_payload(
            b"data=%7B%22goodsId%22%3A40098%2C%22orderId%22%3A%22no-identity%22%7D"
        )
        login_server._RECENT_HTTP_ACCOUNTS.clear()
        with mock.patch.dict(
            storage.DEFAULT_ACCOUNT_ALIASES,
            {"real-channel": self.uid},
            clear=True,
        ):
            account = login_server._recharge_account(payload, "192.168.1.136")
        self.assertIsNotNone(account)
        self.assertEqual(account["uid"], self.uid)
        shape = login_server._payload_shape(payload)
        self.assertIn("data", shape)
        self.assertNotIn("no-identity", json.dumps(shape, ensure_ascii=False))

    def test_account_identity_resolves_unique_role_and_channel_alias(self):
        unique_role_id = 987654321000000000 + len(self._testMethodName)
        self.assertTrue(storage.set_player_role(self.uid, unique_role_id, "identity-test"))
        self.assertEqual(
            storage.get_account_by_identity(unique_role_id)["uid"], self.uid,
        )
        conflicting = storage.get_or_create_account("identity-channel-alias")
        self.assertNotEqual(conflicting["uid"], self.uid)
        with storage.connect() as connection:
            connection.execute(
                "INSERT INTO account_aliases(alias_channel_uid,target_uid,created_at) "
                "VALUES(?,?,1)",
                ("identity-channel-alias", self.uid),
            )
        self.assertEqual(
            storage.get_account_by_identity("identity-channel-alias")["uid"], self.uid,
        )

    def test_offline_payment_grants_without_wallet_and_is_idempotent(self):
        # Install a configuration-only test row in memory.  This exercises the
        # same 2502 dispatch used by the client without relying on an expired
        # live event window.
        module_rules.MODULE_CONFIG["mall"]["MallTable"]["991234"] = {
            "Id": 991234,
            "SellType": 3,
            "TimeLimitType": 0,
            "PayMoney": 101,
            "SingleBuyLimits": 1,
            "LimitTimes": 1,
            "Item": [10013],
            "ItemNum": [1],
        }
        before = storage.get_offline_wallet(self.uid)
        before_item_count = sum(item["quantity"] for item in storage.get_items(self.uid) if item["template_id"] == 10013)
        self.assertEqual(before["balance"], 0)
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 2502, protocol_codec.encode_method(2502, 991234, 1),
        ))
        wallet = storage.get_offline_wallet(self.uid)
        self.assertEqual(wallet["balance"], 0)
        self.assertEqual(wallet["sumPay"], 30)
        self.assertEqual(
            storage.get_player_state_json(self.uid, "mall")["purchases"]["991234"],
            {"period": "life", "count": 1, "updatedAt": mock.ANY},
        )
        after_item_count = sum(item["quantity"] for item in storage.get_items(self.uid) if item["template_id"] == 10013)
        self.assertEqual(after_item_count, before_item_count + 1)

        # The mall limit rejects a second purchase, while the storage-level
        # order key remains safe if the client retries the same transaction.
        self.assertTrue(module_handlers.dispatch(
            self.session, self.uid, 2502, protocol_codec.encode_method(2502, 991234, 1),
        ))
        duplicate = storage.trade_offline_payment(self.uid, 30, [(10013, 1)], "mall:991234:life:1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(sum(item["quantity"] for item in storage.get_items(self.uid) if item["template_id"] == 10013), after_item_count)


if __name__ == "__main__":
    unittest.main()
