"""Contract tests for the official 0.49.10/5392 config snapshots."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))

import build_config_snapshots
import extract_lua_config

BUNDLE = extract_lua_config.DEFAULT_BUNDLE
HAS_BUNDLE = BUNDLE.is_dir() and any(BUNDLE.glob("textasset_*.lua.bin"))


def _probability_total(tiers):
    return sum(
        float(value.rstrip("%"))
        for tier in tiers
        for value in tier["probability"]
    )


@unittest.skipUnless(HAS_BUNDLE, "official 5392 LuaJIT bundle is not extracted")
class LuaConfigLoaderTests(unittest.TestCase):
    def test_rows_inherit_their_shared_metatable_defaults(self):
        table = extract_lua_config.load_config("CfgLotteryTable", BUNDLE)
        expected = {
            "AutoBuy", "FreeIntervalTime", "payPoint", "LotteryMode", "Type",
            "FreeType", "FirstDrop", "BaseDrop", "ItemCost", "PackId",
        }
        for key, row in table.items():
            self.assertLessEqual(expected, set(row), f"row {key} lost defaults")

    def test_sharded_tables_are_merged_without_loss(self):
        merged = extract_lua_config.load_config("CfgItemTable", BUNDLE)
        self.assertGreater(len(merged), 9000)
        self.assertEqual(merged["44149"]["Type"], 3)


@unittest.skipUnless(HAS_BUNDLE, "official 5392 LuaJIT bundle is not extracted")
class LotterySnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = build_config_snapshots.build_lottery(BUNDLE)

    def test_equipment_show_declares_the_pools_the_client_draws_from(self):
        show = self.snapshot["shows"]["20003"]
        self.assertEqual(show["LotteryType"], 2)
        self.assertEqual(show["Pool"], [2000301, 2000302])
        self.assertEqual(show["ChoiceNum"], 5)
        self.assertEqual(show["UpGroupRotation"], [110000])
        self.assertTrue(show["IsRotation"])
        self.assertEqual(show["InsureTimes"], [50])

        single = self.snapshot["pools"]["2000301"]
        ten = self.snapshot["pools"]["2000302"]
        self.assertEqual(single["ItemCost"], [10005, 1])
        self.assertEqual(ten["ItemCost"], [10005, 10])
        self.assertEqual(len(ten["packSlots"]), 10)

    def test_up_group_110000_maps_every_row_to_one_equipment_template(self):
        rows = self.snapshot["upGroups"]["110000"]["rows"]
        self.assertEqual(sorted(int(key) for key in rows), list(range(110001, 110151)))
        items = extract_lua_config.load_config("CfgItemTable", BUNDLE)
        for key, row in rows.items():
            self.assertEqual(len(row["UpList"]), 1, f"row {key} is not single-item")
            cid = row["UpList"][0]
            self.assertEqual(cid, int(key) - 110001 + 44001)
            self.assertEqual(items[str(cid)]["Type"], 3)

    def test_selection_observed_from_the_client_resolves(self):
        rows = self.snapshot["upGroups"]["110000"]["rows"]
        observed = [110149, 110150, 110147, 110146, 110145]
        self.assertEqual(
            [rows[str(key)]["UpList"][0] for key in observed],
            [44149, 44150, 44147, 44146, 44145],
        )

    def test_up_rows_only_differ_in_their_own_up_tier(self):
        group = self.snapshot["upGroups"]["110000"]
        self.assertTrue(all(row["sharedTiersMatch"] for row in group["rows"].values()))
        show_tiers = self.snapshot["shows"]["20003"]["tiers"]
        self.assertEqual(group["sharedTiers"], show_tiers[1:])

    def test_tier_snapshot_keeps_up_probability_items_separate_from_up_list(self):
        tiers = build_config_snapshots.build_lottery_tiers(BUNDLE)
        row = tiers["upGroups"]["1207"]["rows"]["1207"]
        self.assertNotEqual(row["upList"], row["items"])
        self.assertEqual(len(row["items"]), len(row["weights"]))
        self.assertEqual(row["items"][:2], [20010025, 20010034])

    def test_selected_up_tier_replaces_the_unselected_equipment_tier(self):
        group = self.snapshot["upGroups"]["110000"]
        selection = ["110145", "110146", "110147", "110149", "110150"]
        up_tier = {
            "order": 1,
            "items": [group["rows"][key]["upTier"]["items"][0] for key in selection],
            "probability": [
                group["rows"][key]["upTier"]["probability"][0] for key in selection
            ],
        }
        effective = [up_tier] + group["sharedTiers"]
        self.assertEqual(len(up_tier["items"]), 5)
        self.assertAlmostEqual(_probability_total(effective), 100.0, delta=0.5)
        self.assertAlmostEqual(
            _probability_total(self.snapshot["shows"]["20003"]["tiers"]),
            100.0,
            delta=0.5,
        )

    def test_up_packs_carry_no_static_weight(self):
        packs = self.snapshot["packs"]
        self.assertEqual(packs["100001"]["Weight"], 1250)
        self.assertEqual(packs["100002"]["Weight"], 2750)
        self.assertEqual(packs["100003"]["Weight"], 850)
        for pack_id in range(61001, 61151):
            self.assertEqual(packs[str(pack_id)]["Weight"], 0)


@unittest.skipUnless(HAS_BUNDLE, "official 5392 LuaJIT bundle is not extracted")
class HomelandSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = build_config_snapshots.build_homeland(BUNDLE)

    def test_dorm_rooms_are_separated_from_castle_function_rooms(self):
        rooms = self.snapshot["rooms"]
        function_rooms = [k for k, v in rooms.items() if v["CastleIndex"] == 1]
        dorms = [k for k, v in rooms.items() if v["CastleIndex"] == 2]
        self.assertEqual(sorted(int(k) for k in function_rooms), list(range(1, 20)))
        self.assertEqual(sorted(int(k) for k in dorms), list(range(101, 161)))

        room = rooms["103"]
        self.assertEqual(room["OpenCost"], [11911, 50])
        self.assertEqual(room["DefaultSuit"], 160000)
        self.assertEqual(room["ConditionId"], 26050223)
        self.assertEqual(room["ComfortNeed"], 100)

    def test_plant_grids_split_into_free_item_cost_and_pay_point_tiers(self):
        grids = self.snapshot["plantGrids"]
        self.assertEqual(sorted(int(k) for k in grids), list(range(36300001, 36300009)))
        for key in ("36300001", "36300002", "36300003", "36300004"):
            self.assertFalse(grids[key]["IsLock"])
            self.assertEqual(grids[key]["ConditionId"], 0)
        for key in ("36300005", "36300006"):
            self.assertTrue(grids[key]["IsLock"])
            self.assertEqual(grids[key]["ConditionId"], 26050230)
            self.assertEqual(grids[key]["OpenCost"], [2, 1000])
            self.assertEqual(grids[key]["OpenCostPayPoint"], 0)
        for key in ("36300007", "36300008"):
            self.assertTrue(grids[key]["IsLock"])
            self.assertEqual(grids[key]["ConditionId"], 26050230)
            self.assertEqual(grids[key]["OpenCost"], [])
            self.assertEqual(grids[key]["OpenCostPayPoint"], 5)

    def test_unlock_conditions_referenced_by_rooms_and_grids_are_captured(self):
        conditions = self.snapshot["referencedConditions"]
        self.assertEqual(conditions["26050223"]["Value"][0], 25020115)
        self.assertEqual(conditions["26050230"]["Value"][0], 25020205)
        # TYPE_PLAYER_ATT with either SUB_TYPE_PASS_LEVEL (against
        # PlayerInfo.quickChallenge) or SUB_TYPE_PLAYER_ATT_TOWN_STORY (against
        # PlayerInfo.unlockTownEvents); no other player state gates a room.
        town_story = set()
        for key, row in conditions.items():
            self.assertEqual(row["Type"][0], 1)
            self.assertIn(row["SubType"][0], (3, 19))
            self.assertEqual([t for t in row["Type"][1:] if t], [])
            if row["SubType"][0] == 19:
                town_story.add(key)
        self.assertEqual(town_story, {"26050203", "26050214"})
        self.assertEqual(conditions["26050203"]["Value"][0], 10000105)
        self.assertEqual(conditions["26050214"]["Value"][0], 10020107)

    def test_comfort_inputs_are_available_for_recomputation(self):
        decorates = self.snapshot["decorates"]
        self.assertGreater(len(decorates), 1000)
        self.assertTrue(all("Score" in row for row in decorates.values()))
        suits = self.snapshot["decorateSuits"]
        self.assertTrue(all("FurnitureIDList" in row for row in suits.values()))
        levels = self.snapshot["comfortLevels"]
        self.assertEqual(
            sorted(row["Level"] for row in levels.values()),
            sorted(set(row["Level"] for row in levels.values())),
        )

    def test_home_plot_dialog_cids_come_from_action_units(self):
        dialogs = self.snapshot["homePlotDialogCids"]
        self.assertGreater(len(dialogs), 100)
        self.assertEqual(dialogs, sorted(set(dialogs)))
        self.assertTrue(all(int(value) > 0 for value in dialogs))
        self.assertIn(12000000, dialogs)


@unittest.skipUnless(HAS_BUNDLE, "official 5392 LuaJIT bundle is not extracted")
class TownSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = build_config_snapshots.build_town(BUNDLE)

    def test_every_event_belongs_to_a_declared_area(self):
        areas = self.snapshot["areas"]
        events = self.snapshot["events"]
        self.assertEqual(len(areas), 13)
        self.assertGreater(len(events), 1000)
        area_ids = {int(key) for key in areas}
        for key, event in events.items():
            self.assertIn(event["AreaId"], area_ids, f"event {key} has unknown area")

    def test_area_entry_costs_and_patrol_rewards_are_present(self):
        area = self.snapshot["areas"]["10010"]
        self.assertEqual(area["WanderCost"], [101, 1])
        self.assertEqual(area["PatrolAward"], [11802, 11801, 10901])
        self.assertEqual(area["IsUnlocked"], 1)

    def test_event_conditions_are_included_for_server_side_gate_checks(self):
        conditions = self.snapshot["referencedConditions"]
        self.assertIn("26043001", conditions)
        self.assertEqual(conditions["26043001"]["SubType"][0], 3)
        self.assertEqual(conditions["26011926"]["SubType"][0], 1)


if __name__ == "__main__":
    unittest.main()
