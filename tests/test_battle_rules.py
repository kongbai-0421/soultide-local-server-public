import os
import copy
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
TEST_DB = Path(TEST_DIR.name) / "battle-rules.db"
os.environ["SOULTIDE_DB_PATH"] = str(TEST_DB)

import storage
from tools.extract_battle_config import extract_skill_functions


class FixedDropRandom:
    def __init__(self, picks):
        self.picks = iter(picks)

    def randint(self, _start, _end):
        return next(self.picks)


class BattleRuleTests(unittest.TestCase):
    def setUp(self):
        storage.configure_battle_rules(
            skills={"1": {"detail": 1}},
            skill_details={
                "1": {
                    "functionId": 1,
                    "targetType": 101,
                    "ratio": [1.0],
                    "costEnergy": 20,
                    "cooldown": 2,
                    "initCd": 0,
                    "parameter": [],
                    "element": 0,
                    "buffs": [],
                },
                "2": {
                    "functionId": 2,
                    "targetType": 206,
                    "ratio": [1.0],
                    "costEnergy": 20,
                    "cooldown": 0,
                    "initCd": 0,
                    "parameter": [],
                    "element": 0,
                    "buffs": [],
                },
                "3": {
                    "functionId": 2,
                    "targetType": 100,
                    "ratio": [1.0],
                    "costEnergy": 20,
                    "cooldown": 0,
                    "initCd": 0,
                    "parameter": [],
                    "element": 0,
                    "buffs": [],
                },
                "4": {
                    "functionId": 4,
                    "targetType": 101,
                    "ratio": [1.0],
                    "costEnergy": 20,
                    "cooldown": 0,
                    "initCd": 0,
                    "parameter": [],
                    "element": 0,
                    "buffs": [],
                },
                "5": {
                    "functionId": 5,
                    "targetType": 101,
                    "ratio": [1.0],
                    "costEnergy": 20,
                    "cooldown": 0,
                    "initCd": 0,
                    "parameter": [1.0, 2.0],
                    "element": 0,
                    "buffs": [],
                },
            },
            skill_functions={
                "1": {
                    "dynamicRpn": "",
                    "selfAtt": [7, 0],
                    "selfAttVal": [[1], []],
                    "targetAtt": [11, 0],
                    "targetAttVal": [[-1], []],
                    "ignoreShield": False,
                },
                "2": {
                    "dynamicRpn": "",
                    "selfAtt": [7, 0],
                    "selfAttVal": [[1], []],
                    "targetAtt": [],
                    "targetAttVal": [],
                    "ignoreShield": False,
                },
                "3": {
                    "dynamicRpn": "",
                    "selfAtt": [7, 0],
                    "selfAttVal": [[1], []],
                    "targetAtt": [],
                    "targetAttVal": [],
                    "ignoreShield": False,
                },
                "4": {
                    "dynamicRpn": "K*(1+0.01*A1)",
                    "dynamicArgType": [303],
                    "dynamicArgParams": [["1", "7"]],
                    "selfAtt": [7, 0],
                    "selfAttVal": [[1], []],
                    "targetAtt": [11, 0],
                    "targetAttVal": [[-1], []],
                    "damageType": 1,
                    "ignoreShield": False,
                },
                "5": {
                    "dynamicRpn": "A1==1&&A2>=2",
                    "dynamicArgType": [104, 104],
                    "dynamicArgParams": [["1"], ["2"]],
                    "selfAtt": [7, 0],
                    "selfAttVal": [[1], []],
                    "targetAtt": [],
                    "targetAttVal": [],
                    "damageType": 1,
                    "ignoreShield": False,
                },
            },
            buffs={},
            search_targets={
                "101": {
                    "selectCamp": 1,
                    "positionType": 0,
                    "isGroup": False,
                    "selectNum": 1,
                    "selectSelf": False,
                    "selectDeath": False,
                    "alivePriority": True,
                },
                "206": {
                    "selectCamp": 2,
                    "positionType": 0,
                    "isGroup": True,
                    "selectNum": 5,
                    "selectSelf": True,
                    "selectDeath": False,
                    "alivePriority": True,
                },
                "100": {
                    "selectCamp": 4,
                    "positionType": 0,
                    "isGroup": False,
                    "selectNum": 1,
                    "selectSelf": True,
                    "selectDeath": False,
                    "alivePriority": True,
                },
            },
        )

    @staticmethod
    def unit(side, position, hp, attack, defense, speed, energy=0, skills=None):
        attributes = [hp, attack, defense, speed, energy]
        return {
            "Attributes": attributes,
            "AttributeTypes": [9, 7, 11, 10, 14],
            "BattlePos": position,
            "Power": max(1, attack + defense),
            "Skills": skills or [],
            "SkillStrengthens": [],
            "SPStatus": [],
            "InitBuff": [],
            "TroopType": 1 if side == "attacker" else 2,
            "WeakNum": 0,
            "WeakTypes": [],
        }

    def snapshot(self, attacker, defenders, max_round=1):
        return {
            "RandomSeed": 7,
            "MaxRound": max_round,
            "Attacker": {"ArrFightUnitPOD": attacker},
            "Defender": {"ArrFightUnitPOD": defenders},
        }

    def test_attribute_mapping_and_signed_defense_formula(self):
        account = storage.get_or_create_account("battle-rule-formula")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        storage.set_battle_server_snapshot(
            account["uid"],
            battle_id,
            self.snapshot(
                [self.unit("attacker", 1, 100, 20, 5, 10, 100, [1])],
                [self.unit("defender", 1, 100, 1, 7, 1)],
            ),
        )

        result = storage.evaluate_battle_instance(account["uid"], battle_id)

        self.assertEqual(result["result"], 1)
        first_hit = next(item for item in result["trace"] if item["skill"] == 1)
        self.assertEqual(first_hit["damage"], 13)

    def test_buff_stack_status_and_skill_context_dynamic_values(self):
        rules = copy.deepcopy(storage.BATTLE_RULES)
        rules["skills"] = {"11": {"detail": 11}}
        rules["skillDetails"] = {
            "11": {
                "functionId": 11,
                "targetType": 101,
                "ratio": [1.0],
                "costEnergy": 0,
                "cooldown": 0,
                "initCd": 0,
                "parameter": [],
                "element": 0,
                "buffs": [{"id": 9002, "probability": 1.0, "target": 101, "time": 1, "stack": 1}],
            }
        }
        rules["skillFunctions"] = {
            "11": {
                "dynamicRpn": "",
                "selfAtt": [7, 0],
                "selfAttVal": [[1], []],
                "targetAtt": [11, 0],
                "targetAttVal": [[-1], []],
                "ignoreShield": False,
            }
        }
        rules["buffs"] = {
            "9001": {
                "buffType": 0, "debuffType": 0, "stackMax": 3, "stackType": 3,
                "buffTime": -1, "triggerType": 0, "effectTypes": [], "effectParams": [],
                "buffTag": [1], "dynamicRpn": "", "dynamicArgType": [], "dynamicArgParams": [],
            },
            "9002": {
                "buffType": 0, "debuffType": 0, "stackMax": 1, "stackType": 0,
                "buffTime": 1, "triggerType": 103, "effectTypes": [101],
                "effectParams": [["1", "201", "9003", "0", "1"]],
                "buffTag": [], "dynamicRpn": "A1==1&&A2==11",
                "dynamicArgType": [319, 316], "dynamicArgParams": [["2", "1"], ["2"]],
            },
            "9003": {
                "buffType": 0, "debuffType": 0, "stackMax": 1, "stackType": 0,
                "buffTime": 1, "triggerType": 0, "effectTypes": [], "effectParams": [],
                "buffTag": [], "dynamicRpn": "", "dynamicArgType": [], "dynamicArgParams": [],
            },
        }
        storage.configure_battle_rules(
            rules["skills"], rules["skillDetails"], rules["skillFunctions"],
            buffs=rules["buffs"], search_targets=storage.BATTLE_RULES["searchTargets"],
            buff_group_relations=storage.BATTLE_RULES["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-dynamic-context")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 100, 20, 5, 10, 0, [11])
        attacker["InitBuff"] = [9001, 0, 9002, 0]
        defender = self.unit("defender", 1, 10, 1, 0, 1)
        defender["InitBuff"] = [9001, 0]
        storage.set_battle_server_snapshot(account["uid"], battle_id, self.snapshot([attacker], [defender]))

        result = storage.evaluate_battle_instance(account["uid"], battle_id)

        self.assertEqual(result["result"], 1)
        self.assertTrue(
            any(item["id"] == 9003 for state in result["states"] for item in state["buffs"])
        )

    def test_dynamic_205_306_307_and_326_use_official_contexts(self):
        storage.configure_battle_rules(
            skills={"11": {"detail": 11, "type": 1}},
            skill_details={
                "11": {
                    "functionId": 11, "targetType": 101, "ratio": [1.0],
                    "costEnergy": 0, "cooldown": 0, "initCd": 0,
                    "parameter": [0, 0, 0, 77], "element": 0,
                    "buffs": [{"id": 9001, "probability": 1.0, "target": 101,
                               "time": 1, "stack": 1}],
                },
            },
            skill_functions={"11": {"dynamicRpn": "", "selfAtt": [7],
                                      "selfAttVal": [[1]], "targetAtt": [],
                                      "targetAttVal": [], "ignoreShield": False}},
            buffs={
                "9000": {"triggerType": 103, "triggerMax": -1,
                         "triggerProbability": 1.0, "stackMax": 1,
                         "stackType": 5, "buffTime": -1, "effectTypes": []},
                "9002": {"triggerType": 0, "triggerMax": -1,
                         "triggerProbability": 1.0, "stackMax": 1,
                         "stackType": 5, "buffTime": -1, "buffTag": [42],
                         "effectTypes": []},
                "9001": {"triggerType": 103, "triggerMax": -1,
                         "triggerProbability": 1.0, "stackMax": 1,
                         "stackType": 5, "buffTime": 1,
                         "dynamicRpn": "A1>=1&&A2==1&&A3==1&&A4==77",
                         "dynamicArgType": [205, 306, 307, 326],
                         "dynamicArgParams": [[9000], [2, 42], [2, 28]],
                         "effectTypes": [302], "effectParams": [[99]]},
            },
            search_targets=storage.BATTLE_RULES["searchTargets"],
        )
        account = storage.get_or_create_account("battle-rule-dynamic-205-326")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 100, 20, 0, 10, 0, [11])
        defender = self.unit("defender", 1, 100, 1, 0, 1)
        defender["InitBuff"] = [9000, 0, 9002, 0]
        defender["SPStatus"] = [28]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id, self.snapshot([attacker], [defender]),
        )

        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        state = next(item for item in result["states"] if item["side"] == "defender")
        self.assertIn(99, state["statuses"])
        self.assertIn(28, state["spStatuses"])

    def test_drop_libraries_expand_nested_groups_and_final_items(self):
        previous = storage.BATTLE_DROP_LIBRARIES
        try:
            storage.BATTLE_DROP_LIBRARIES = {
                "10": {"loopCount": 2, "randomIds": [20], "randomTypes": [1],
                       "randomCounts": [3], "weights": [1]},
                "20": {"loopCount": 1, "randomIds": [101, 102], "randomTypes": [2, 2],
                       "randomCounts": [2, 4], "weights": [1, 3]},
            }
            rewards = storage._expand_drop_library(10, FixedDropRandom([1, 1, 1, 4]))
            self.assertEqual(rewards, [(101, 6), (102, 12)])
            self.assertNotIn(10, dict(rewards))
            self.assertNotIn(20, dict(rewards))
        finally:
            storage.BATTLE_DROP_LIBRARIES = previous

    def test_drop_libraries_reject_cycles_unknown_types_and_missing_groups(self):
        previous = storage.BATTLE_DROP_LIBRARIES
        try:
            storage.BATTLE_DROP_LIBRARIES = {
                "1": {"loopCount": 1, "randomIds": [2], "randomTypes": [1],
                      "randomCounts": [1], "weights": [1]},
                "2": {"loopCount": 1, "randomIds": [1], "randomTypes": [1],
                      "randomCounts": [1], "weights": [1]},
                "3": {"loopCount": 1, "randomIds": [999], "randomTypes": [9],
                      "randomCounts": [1], "weights": [1]},
            }
            self.assertEqual(storage._expand_drop_library(1, FixedDropRandom([1, 1])), [])
            self.assertEqual(storage._expand_drop_library(3, FixedDropRandom([])), [])
            self.assertEqual(storage._expand_drop_library(404, FixedDropRandom([])), [])
        finally:
            storage.BATTLE_DROP_LIBRARIES = previous

    def test_sp_cost_and_cooldown_are_applied(self):
        account = storage.get_or_create_account("battle-rule-cooldown")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        storage.set_battle_server_snapshot(
            account["uid"],
            battle_id,
            self.snapshot(
                [self.unit("attacker", 1, 1000, 20, 5, 10, 20, [1])],
                [self.unit("defender", 1, 1000, 1, 0, 1)],
                max_round=2,
            ),
        )

        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        skill_hits = [item for item in result["trace"] if item["skill"] == 1]

        self.assertEqual(len(skill_hits), 1)
        self.assertGreater(result["turnCount"], len(skill_hits))

    def test_group_self_target_includes_all_living_allies(self):
        account = storage.get_or_create_account("battle-rule-target")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        snapshot = self.snapshot(
            [
                self.unit("attacker", 1, 100, 20, 5, 10),
                self.unit("attacker", 2, 100, 20, 5, 9),
            ],
            [self.unit("defender", 1, 1000, 1, 0, 1)],
        )
        snapshot["Attacker"]["ArrFightUnitPOD"][0]["Skills"] = [2]
        snapshot["Attacker"]["ArrFightUnitPOD"][0]["Attributes"][-1] = 20
        storage.set_battle_server_snapshot(account["uid"], battle_id, snapshot)

        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        targets = {
            item["target"] for item in result["trace"] if item["skill"] == 2
        }
        self.assertEqual(targets, {1, 2})

    def test_group_target_excludes_caster_when_select_self_is_false(self):
        rules = storage.BATTLE_RULES
        targets = copy.deepcopy(rules["searchTargets"])
        targets["206"] = dict(targets["206"], selectSelf=False)
        storage.configure_battle_rules(
            rules["skills"], rules["skillDetails"], rules["skillFunctions"],
            buffs=rules["buffs"], search_targets=targets,
            buff_group_relations=rules["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-target-exclude-self")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        first = self.unit("attacker", 1, 50, 20, 5, 10, 20, [2])
        second = self.unit("attacker", 2, 50, 20, 5, 9)
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([first, second], [self.unit("defender", 1, 1000, 1, 0, 1)]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        targets_seen = {item["target"] for item in result["trace"] if item["skill"] == 2}
        self.assertEqual(targets_seen, {2})

    def test_share_damage_and_energy_modifiers_are_applied_and_removed(self):
        rules = storage.BATTLE_RULES
        storage.configure_battle_rules(
            skills=rules["skills"], skill_details=rules["skillDetails"],
            skill_functions=rules["skillFunctions"], search_targets=rules["searchTargets"],
            buff_group_relations=rules["buffGroupRelations"],
            buffs={
                "950": {
                    "triggerType": 103, "effectTypes": [318, 226, 227],
                    "effectParams": [[0.5], [0.5], [40]],
                    "buffTime": 2, "stackMax": 1, "stackType": 5,
                },
            },
        )
        account = storage.get_or_create_account("battle-rule-share-energy")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        protected = self.unit("attacker", 1, 100, 1, 0, 1)
        protected["InitBuff"] = [950, 0]
        ally = self.unit("attacker", 2, 100, 1, 0, 2)
        enemy = self.unit("defender", 1, 100, 20, 0, 10)
        storage.set_battle_server_snapshot(
            account["uid"], battle_id, self.snapshot([protected, ally], [enemy], max_round=2)
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        hit = next(item for item in result["trace"] if item.get("sharedDamage", 0) > 0)
        self.assertGreater(hit["sharedDamage"], 0)
        protected_state = next(item for item in result["states"] if item["position"] == 1 and item["side"] == "attacker")
        ally_state = next(item for item in result["states"] if item["position"] == 2 and item["side"] == "attacker")
        self.assertEqual(protected_state["maxSp"], 100)
        self.assertEqual(protected_state["energyRate"], 1.0)
        self.assertEqual(protected_state["shareDamage"], 0.0)
        self.assertLess(ally_state["hp"], ally_state["maxHp"])

    def test_self_target_does_not_switch_to_enemy(self):
        account = storage.get_or_create_account("battle-rule-self-target")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        snapshot = self.snapshot(
            [self.unit("attacker", 1, 100, 20, 5, 10, 20, [3])],
            [self.unit("defender", 1, 1000, 1, 0, 1)],
        )
        storage.set_battle_server_snapshot(account["uid"], battle_id, snapshot)

        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        targets = {
            item["target"] for item in result["trace"] if item["skill"] == 3
        }
        self.assertEqual(targets, {1})

    def test_extractor_preserves_dynamic_function_arguments(self):
        source = Path("analysis/decompiled_all")
        functions = extract_skill_functions(source)
        self.assertEqual(functions["103"]["dynamicArgType"][:2], [103, 102])
        self.assertEqual(
            functions["103"]["dynamicArgParams"][:2],
            [["2", "3", "11061"], ["2", "1", "1120"]],
        )

    def test_team_buff_and_random_team_target_are_applied(self):
        rules = storage.BATTLE_RULES
        storage.configure_battle_rules(
            rules["skills"], rules["skillDetails"], rules["skillFunctions"],
            buffs={
                "9001": {"triggerType": 103, "effectTypes": [219], "effectParams": [[9002, 2, 1]], "buffTime": -1},
                "9002": {"triggerType": 0, "effectTypes": [], "effectParams": [], "buffTime": -1},
            },
            search_targets=rules["searchTargets"],
            buff_group_relations=rules["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-team-buff")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        first = self.unit("attacker", 1, 100, 20, 5, 10)
        first["InitBuff"] = [9001, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([first, self.unit("attacker", 2, 100, 20, 5, 9)], [self.unit("defender", 1, 1000, 1, 0, 1)]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        attacker_states = [state for state in result["states"] if state["side"] == "attacker"]
        self.assertTrue(all(any(buff["id"] == 9002 for buff in state["buffs"]) for state in attacker_states))
        self.assertTrue(any(event["effectType"] == 219 and event["status"] == "applied" for event in result["events"]))

    def test_doll_revive_runs_after_death_and_preserves_state(self):
        rules = storage.BATTLE_RULES
        storage.configure_battle_rules(
            rules["skills"], rules["skillDetails"], rules["skillFunctions"],
            buffs={
                "9010": {"triggerType": 319, "effectTypes": [221], "effectParams": [[50]], "buffTime": -1, "deathEffective": True},
            },
            search_targets=rules["searchTargets"],
            buff_group_relations=rules["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-revive")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        defender = self.unit("defender", 1, 10, 1, 0, 1)
        defender["InitBuff"] = [9010, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([self.unit("attacker", 1, 100, 100, 0, 10, 0, [1])], [defender]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        defender_state = next(state for state in result["states"] if state["side"] == "defender")
        self.assertTrue(defender_state["revived"])
        self.assertEqual(defender_state["hp"], 5)
        self.assertTrue(any(event["effectType"] == 221 for event in result["events"]))

    def test_dynamic_attribute_formula_and_lua_boolean_formula(self):
        account = storage.get_or_create_account("battle-rule-dynamic")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        snapshot = self.snapshot(
            [self.unit("attacker", 1, 1000, 20, 5, 10, 100, [4, 5])],
            [self.unit("defender", 1, 1000, 1, 2, 1)],
            max_round=1,
        )
        storage.set_battle_server_snapshot(account["uid"], battle_id, snapshot)
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        hits = [item for item in result["trace"] if item["skill"] in (4, 5)]
        self.assertEqual(hits[0]["damage"], 21)

    def test_buff_add_trigger_tag_and_trigger_limit(self):
        storage.configure_battle_rules(
            skills={},
            skill_details={},
            skill_functions={},
            buffs={
                "10": {
                    "triggerType": 103,
                    "triggerMax": -1,
                    "triggerProbability": 1.0,
                    "stackMax": 1,
                    "stackType": 5,
                    "buffTime": -1,
                    "buffTag": [16],
                    "effectTypes": [101],
                    "effectParams": [["1", "201", "11", "0", "1"]],
                },
                "11": {
                    "triggerType": 301,
                    "triggerMax": 1,
                    "triggerProbability": 1.0,
                    "stackMax": 1,
                    "stackType": 5,
                    "buffTime": -1,
                    "effectTypes": [302],
                    "effectParams": [["42"]],
                },
            },
            search_targets={
                "201": {
                    "selectCamp": 3,
                    "positionType": 0,
                    "isGroup": False,
                    "selectNum": 1,
                    "selectSelf": True,
                    "selectDeath": False,
                    "alivePriority": True,
                },
            },
        )
        account = storage.get_or_create_account("battle-rule-buff")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 1, 0, 10, 0, [])
        attacker["InitBuff"] = [10, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id, self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)])
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        self.assertTrue(any(event.get("buff") == 11 and event.get("event") == "BattleRoundStart" for event in result["events"]))
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertIn(16, state["statuses"])
        self.assertNotIn(11, {buff["id"] for buff in state["buffs"]})

    def trigger_rules(self, buffs):
        storage.configure_battle_rules(
            skills={},
            skill_details={},
            skill_functions={},
            buffs=buffs,
            search_targets={
                "201": {
                    "selectCamp": 3,
                    "positionType": 0,
                    "isGroup": False,
                    "selectNum": 1,
                    "selectSelf": True,
                    "selectDeath": False,
                    "alivePriority": True,
                },
            },
        )

    def run_trigger_buffs(self, buffs, init_buffs, max_round=1):
        self.trigger_rules(buffs)
        account = storage.get_or_create_account("battle-rule-trigger-%s" % len(init_buffs))
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 1, 0, 10, 0, [])
        attacker["InitBuff"] = init_buffs
        storage.set_battle_server_snapshot(
            account["uid"],
            battle_id,
            self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)], max_round),
        )
        return storage.evaluate_battle_instance(account["uid"], battle_id)

    def test_trigger_params_filter_add_remove_and_stack(self):
        buffs = {
            "10": {
                "triggerType": 103,
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [101],
                "effectParams": [[
                    "1", "201", "11", "0", "1",
                    "1", "201", "12", "0", "1",
                ]],
            },
            "11": {
                "triggerType": 103,
                "triggerParams": [1, 12],
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [302],
                "effectParams": [[41]],
            },
            "12": {
                "triggerType": 103,
                "triggerParams": [1, 12],
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [302],
                "effectParams": [[42]],
            },
        }
        result = self.run_trigger_buffs(buffs, [10, 0])
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertIn(42, state["statuses"])
        self.assertNotIn(41, state["statuses"])
        self.assertTrue(any(event.get("buff") == 12 for event in result["events"]))

    def test_trigger_params_filter_be_removed_and_stack(self):
        buffs = {
            "13": {
                "triggerType": 102,
                "triggerParams": [1, 13, -1],
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": 1,
                "effectTypes": [302],
                "effectParams": [[43]],
            },
            "15": {
                "triggerType": 104,
                "triggerParams": [1, 15],
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 2,
                "stackType": 3,
                "buffTime": -1,
                "effectTypes": [302],
                "effectParams": [[44]],
            },
            "16": {
                "triggerType": 103,
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [101],
                "effectParams": [["1", "201", "15", "0", "1"]],
            },
        }
        result = self.run_trigger_buffs(buffs, [13, 0], max_round=1)
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertIn(43, state["statuses"])
        self.assertNotIn(13, {buff["id"] for buff in state["buffs"]})

        result = self.run_trigger_buffs(buffs, [15, 0, 16, 0], max_round=1)
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertIn(44, state["statuses"])
        self.assertEqual(next(buff["stack"] for buff in state["buffs"] if buff["id"] == 15), 2)

    def test_buff_resistance_records_official_effect_and_blocks_buff(self):
        buffs = {
            "10": {
                "triggerType": 103,
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [106],
                "effectParams": [["1", "201", "20", "0", "1"]],
            },
            "16": {
                "triggerType": 103,
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
                "effectTypes": [101],
                "effectParams": [["1", "201", "20", "0", "1"]],
            },
            "20": {
                "triggerType": 103,
                "triggerMax": -1,
                "triggerProbability": 1.0,
                "stackMax": 1,
                "stackType": 5,
                "buffTime": -1,
            },
        }
        result = self.run_trigger_buffs(buffs, [10, 0, 16, 0])
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertNotIn(20, {buff["id"] for buff in state["buffs"]})
        self.assertTrue(any(event.get("status") == "immune" for event in result["events"]))

    def test_lowercase_k_formula_is_evaluated(self):
        storage.configure_battle_rules(
            skills={"6": {"detail": 6, "type": 1}},
            skill_details={
                "6": {
                    "functionId": 6,
                    "targetType": 101,
                    "ratio": [1.0],
                    "costEnergy": 1,
                    "cooldown": 0,
                    "initCd": 0,
                    "parameter": [],
                    "element": 0,
                    "buffs": [],
                },
            },
            skill_functions={
                "6": {
                    "dynamicRpn": "k*0.1",
                    "dynamicArgType": [],
                    "dynamicArgParams": [],
                    "selfAtt": [7],
                    "selfAttVal": [[1]],
                    "targetAtt": [],
                    "targetAttVal": [],
                    "damageType": 1,
                    "ignoreShield": False,
                },
            },
            buffs={},
            search_targets={
                "101": {
                    "selectCamp": 1,
                    "positionType": 0,
                    "isGroup": False,
                    "selectNum": 1,
                    "selectSelf": False,
                    "selectDeath": False,
                    "alivePriority": True,
                },
            },
        )
        account = storage.get_or_create_account("battle-rule-k")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 20, [6])
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        self.assertEqual(next(item for item in result["trace"] if item["skill"] == 6)["damage"], 2)

    def test_zero_cost_skill_is_selected(self):
        storage.BATTLE_RULES["skillDetails"]["1"]["costEnergy"] = 0
        storage.BATTLE_RULES["skillDetails"]["1"]["cooldown"] = 0
        account = storage.get_or_create_account("battle-rule-zero-cost")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot(
                [self.unit("attacker", 1, 1000, 20, 0, 10, 0, [1])],
                [self.unit("defender", 1, 1000, 1, 0, 1)],
            ),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        self.assertTrue(any(item["skill"] == 1 for item in result["trace"]))

    def test_replace_skill_changes_cast_skill(self):
        storage.BATTLE_RULES["skills"]["2"] = {"detail": 2}
        storage.BATTLE_RULES["skillDetails"]["2"] = dict(storage.BATTLE_RULES["skillDetails"]["1"], costEnergy=0)
        storage.BATTLE_RULES["buffs"]["99"] = {
            "triggerType": 103,
            "triggerMax": -1,
            "triggerProbability": 1.0,
            "stackMax": 1,
            "stackType": 5,
            "buffTime": -1,
            "effectTypes": [325],
            "effectParams": [[1, 2]],
        }
        account = storage.get_or_create_account("battle-rule-replace-skill")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 0, [1])
        attacker["InitBuff"] = [99, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        self.assertTrue(any(item["skill"] == 2 for item in result["trace"]))

    def test_attribute_modifier_is_removed_with_buff(self):
        storage.BATTLE_RULES["skillDetails"]["1"]["costEnergy"] = 0
        storage.BATTLE_RULES["skillDetails"]["1"]["cooldown"] = 0
        storage.BATTLE_RULES["buffs"]["98"] = {
            "triggerType": 103,
            "triggerMax": -1,
            "triggerProbability": 1.0,
            "stackMax": 1,
            "stackType": 5,
            "buffTime": 2,
            "effectTypes": [301],
            "effectParams": [[7, 1, 42]],
        }
        account = storage.get_or_create_account("battle-rule-buff-lifecycle")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 0, [1])
        attacker["InitBuff"] = [98, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)], max_round=2),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        hits = [item["damage"] for item in result["trace"] if item["skill"] == 1]
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[:2], [62, 20])

    def test_change_skill_element_affects_weakness_and_immunity(self):
        rules = storage.BATTLE_RULES
        buffs = {
            "910": {
                "triggerType": 103, "triggerMax": -1, "triggerProbability": 1.0,
                "stackMax": 1, "stackType": 5, "buffTime": -1,
                "effectTypes": [324], "effectParams": [[1, 2]],
            },
            "911": {
                "triggerType": 103, "triggerMax": -1, "triggerProbability": 1.0,
                "stackMax": 1, "stackType": 5, "buffTime": -1,
                "effectTypes": [302], "effectParams": [[22]],
            },
        }
        storage.configure_battle_rules(
            rules["skills"], rules["skillDetails"], rules["skillFunctions"],
            buffs=buffs, search_targets=rules["searchTargets"],
            buff_group_relations=rules["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-element-boundary")

        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 20, [1])
        attacker["InitBuff"] = [910, 0]
        defender = self.unit("defender", 1, 1000, 1, 0, 1)
        defender["WeakNum"], defender["WeakTypes"] = 1, [2]
        storage.set_battle_server_snapshot(account["uid"], battle_id, self.snapshot([attacker], [defender]))
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        hit = next(item for item in result["trace"] if item["skill"] == 1)
        self.assertTrue(hit["weak"])

        battle_id = storage.create_battle_instance(account["uid"], 4)
        immune_defender = self.unit("defender", 1, 1000, 1, 0, 1)
        immune_defender["InitBuff"] = [911, 0]
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 20, [1])
        attacker["InitBuff"] = [910, 0]
        storage.set_battle_server_snapshot(account["uid"], battle_id, self.snapshot([attacker], [immune_defender]))
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        hit = next(item for item in result["trace"] if item["skill"] == 1)
        self.assertTrue(hit["immune"])
        self.assertEqual(hit["damage"], 0)

    def test_no_trigger_element_only_blocks_matching_element(self):
        rules = storage.BATTLE_RULES
        skill_details = copy.deepcopy(rules["skillDetails"])
        skill_details["1"] = dict(skill_details["1"], element=2)
        storage.configure_battle_rules(
            rules["skills"], skill_details, rules["skillFunctions"],
            buffs={
                "920": {
                    "triggerType": 103, "triggerMax": -1, "triggerProbability": 1.0,
                    "stackMax": 1, "stackType": 5, "buffTime": -1,
                    "effectTypes": [240], "effectParams": [[2]],
                },
                "921": {
                    "triggerType": 314, "triggerMax": -1, "triggerProbability": 1.0,
                    "stackMax": 1, "stackType": 5, "buffTime": -1,
                    "effectTypes": [302], "effectParams": [[55]],
                },
            },
            search_targets=rules["searchTargets"],
            buff_group_relations=rules["buffGroupRelations"],
        )
        account = storage.get_or_create_account("battle-rule-no-trigger-element")
        battle_id = storage.create_battle_instance(account["uid"], 4)
        attacker = self.unit("attacker", 1, 1000, 20, 0, 10, 20, [1])
        attacker["InitBuff"] = [920, 0, 921, 0]
        storage.set_battle_server_snapshot(
            account["uid"], battle_id,
            self.snapshot([attacker], [self.unit("defender", 1, 1000, 1, 0, 1)]),
        )
        result = storage.evaluate_battle_instance(account["uid"], battle_id)
        state = next(item for item in result["states"] if item["side"] == "attacker")
        self.assertNotIn(55, state["statuses"])


if __name__ == "__main__":
    unittest.main()
