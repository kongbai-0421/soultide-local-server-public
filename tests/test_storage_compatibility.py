import gc
import json
import os
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
TEST_DB = Path(TEST_DIR.name) / "soultide-test.db"
os.environ["SOULTIDE_DB_PATH"] = str(TEST_DB)

import storage


class CurrencyCompatibilityTests(unittest.TestCase):
    def test_configured_channel_alias_reuses_preserved_account(self):
        alias_channel_uid = next(iter(storage.DEFAULT_ACCOUNT_ALIASES))
        target_uid = storage.DEFAULT_ACCOUNT_ALIASES[alias_channel_uid]
        target_channel_uid = "preserved-test-account"
        target_uuid = "00000000-0000-5000-8000-000000000001"
        wrong_uid, wrong_uuid = storage._account_values(alias_channel_uid)
        now = 1
        with storage.connect() as connection:
            connection.executemany(
                """
                INSERT INTO accounts(
                    channel_uid,uid,uuid,username,channel_id,created_at,
                    last_http_login_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    (
                        target_channel_uid,
                        target_uid,
                        target_uuid,
                        "preserved",
                        "20",
                        now,
                        now,
                        now,
                    ),
                    (
                        alias_channel_uid,
                        wrong_uid,
                        wrong_uuid,
                        "mistaken",
                        "46",
                        now,
                        now,
                        now,
                    ),
                ),
            )
            connection.executemany(
                """
                INSERT INTO players(uid,role_id,role_name,level,snapshot_mode,updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    (target_uid, "role-preserved", "正式账号", 70, "local", now),
                    (wrong_uid, "role-mistaken", "误建账号", 70, "local", now),
                ),
            )

        account = storage.get_or_create_account(
            alias_channel_uid, "channel-user", "46"
        )

        self.assertEqual(account["uid"], target_uid)
        self.assertEqual(account["uuid"], target_uuid)
        self.assertEqual(account["channel_uid"], target_channel_uid)
        self.assertEqual(storage.get_player(target_uid)["role_name"], "正式账号")
        self.assertEqual(storage.get_player(wrong_uid)["role_name"], "误建账号")
        with storage.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT uid FROM accounts WHERE channel_uid=?",
                    (alias_channel_uid,),
                ).fetchone()[0],
                wrong_uid,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT target_uid FROM account_aliases WHERE alias_channel_uid=?",
                    (alias_channel_uid,),
                ).fetchone()[0],
                target_uid,
            )

    @staticmethod
    def seed_test_equipment(uid, count):
        pods = [
            {
                "cid": 42001,
                "num": 1,
                "createTime": 1,
                "equipmentData": {
                    "lv": 1,
                    "exp": 0,
                    "soulPrefabIds": {},
                    "lock": False,
                    "star": 1,
                    "upCostGold": 0,
                },
            }
            for _ in range(count)
        ]
        return storage.seed_equipment_from_snapshot(uid, pods)

    def test_unknown_uid_returns_defaults_without_creating_currency_row(self):
        unknown_uid = "unknown-compatibility-session"

        self.assertEqual(
            storage.get_currencies(unknown_uid),
            {"gold": storage.DEFAULT_GOLD, "souls": storage.DEFAULT_SOULS},
        )

        connection = sqlite3.connect(TEST_DB)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM currencies WHERE uid = ?", (unknown_uid,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_registered_uid_gets_default_currency_row(self):
        account = storage.get_or_create_account("currency-test-account")

        self.assertEqual(
            storage.get_currencies(account["uid"]),
            {"gold": storage.DEFAULT_GOLD, "souls": storage.DEFAULT_SOULS},
        )

        connection = sqlite3.connect(TEST_DB)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM currencies WHERE uid = ?", (account["uid"],)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_current_dress_is_persisted_for_registered_player(self):
        account = storage.get_or_create_account("dress-test-account")

        self.assertTrue(storage.set_current_dress(account["uid"], 33000110))

        player = storage.get_player(account["uid"])
        self.assertEqual(player["current_dress_cid"], 33000110)

    def test_current_dress_rejects_unowned_known_dress(self):
        account = storage.get_or_create_account("unowned-dress-test-account")

        self.assertFalse(storage.set_current_dress(account["uid"], 33011740))

    def test_current_dress_rejects_unknown_uid(self):
        self.assertFalse(storage.set_current_dress("unknown-player", 33011740))

    def test_player_role_identity_is_persisted(self):
        account = storage.get_or_create_account("role-identity-account")
        self.assertTrue(
            storage.set_player_role(
                account["uid"], "local-role-123", "\u4eba\u5076\u5e08"
            )
        )
        player = storage.get_player(account["uid"])
        self.assertEqual(player["role_id"], "local-role-123")
        self.assertEqual(player["role_name"], "\u4eba\u5076\u5e08")

    def test_mark_mazes_complete_merges_existing_progress(self):
        account = storage.get_or_create_account("mainline-complete-account")
        uid = account["uid"]
        storage.update_player_state_json(uid, "finishMazes", [1001])
        storage.update_player_state_json(
            uid,
            "mazeInfoPOD",
            {1001: {"cid": 1001, "star": 1, "score": 20, "winCount": 2}},
        )

        result = storage.mark_mazes_complete(uid, [1001, 1002])

        self.assertEqual(result, {"requested": 2, "added": 1, "total": 2})
        self.assertEqual(storage.get_player_state_json(uid, "finishMazes"), [1001, 1002])
        info = storage.get_all_player_state_json(uid)["mazeInfoPOD"]
        self.assertEqual(info[1001]["star"], 3)
        self.assertEqual(info[1001]["winCount"], 2)
        self.assertEqual(info[1002]["starConditions"], [True, True, True])
        self.assertEqual(info[1002]["winCount"], 1)

    def test_story_progress_is_persisted_without_regressing(self):
        account = storage.get_or_create_account("story-test-account")
        uid = account["uid"]

        self.assertTrue(storage.record_story_chapter(uid, 1901013, 2))
        self.assertTrue(storage.record_story_chapter(uid, 1901013, 1))

        progress = storage.get_story_progress(uid, 1901013)
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["highest_chapter_index"], 2)

    def test_story_progress_rejects_unknown_uid(self):
        self.assertFalse(storage.record_story_chapter("unknown-player", 1901013, 1))

    def test_companion_settlement_is_atomic_and_idempotent(self):
        account = storage.get_or_create_account("companion-settlement-account")
        uid = account["uid"]
        storage.add_item(uid, 10901, 3)

        self.assertTrue(
            storage.apply_companion_operation(
                uid, 20010001, 1001001, [(10901, 2)], [(10601, 100)], 30
            )
        )
        self.assertFalse(
            storage.apply_companion_operation(
                uid, 20010001, 1001001, [(10901, 2)], [(10601, 100)], 30
            )
        )
        items = {row["template_id"]: row["quantity"] for row in storage.get_items(uid)}
        self.assertEqual(items[10901], 1)
        self.assertEqual(items[10601], 100)
        self.assertEqual(storage.get_companion(uid, 20010001)["favor"], 30)
        self.assertTrue(storage.has_dating_record(uid, 20010001, 1001001))

    def test_companion_settlement_rolls_back_when_cost_is_missing(self):
        account = storage.get_or_create_account("companion-rollback-account")
        uid = account["uid"]
        self.assertFalse(
            storage.apply_companion_operation(
                uid, 20010001, 1001002, [(10901, 2)], [(10601, 100)], 30
            )
        )
        self.assertFalse(storage.has_dating_record(uid, 20010001, 1001002))
        self.assertEqual(storage.get_companion(uid, 20010001)["favor"], 0)

    def test_snapshot_seed_only_updates_untouched_rows(self):
        account = storage.get_or_create_account("snapshot-seed-account")
        uid = account["uid"]
        pods = [
            {"cid": 20010001, "lv": 70, "favor": 1234, "favorLv": 12, "dailyDislike": False, "oathActivation": True},
            {"cid": 20010004, "lv": 60, "favor": 567, "favorLv": 6, "dailyDislike": True, "oathActivation": False},
        ]
        self.assertEqual(storage.seed_companions_from_snapshot(uid, pods), 2)
        self.assertEqual(storage.get_companion(uid, 20010001)["favor"], 1234)
        self.assertTrue(storage.get_companion(uid, 20010001)["oath_activation"])
        self.assertEqual(storage.seed_companions_from_snapshot(uid, [{**pods[0], "favor": 9999}]), 0)
        self.assertEqual(storage.get_companion(uid, 20010001)["favor"], 1234)

    def test_snapshot_inventory_seed_preserves_existing_local_quantity(self):
        account = storage.get_or_create_account("inventory-seed-account")
        uid = account["uid"]
        storage.add_item(uid, 10901, 99)
        inserted = storage.seed_items_from_snapshot(
            uid,
            [
                {"cid": 10901, "num": 2},
                {"cid": 20201, "num": 3},
                {"cid": 20201, "num": 4},
            ],
        )
        self.assertEqual(inserted, 1)
        items = {row["template_id"]: row["quantity"] for row in storage.get_items(uid)}
        self.assertEqual(items[10901], 99)
        self.assertEqual(items[20201], 7)

    def test_companion_counters_recover_lazily(self):
        account = storage.get_or_create_account("counter-recovery-account")
        uid = account["uid"]
        storage.seed_player_companion_state(
            uid, {"remainderGiveGiftNum": 10, "fondleNum": 5, "nextRecoveryFondleTime": 0}
        )
        with storage.connect() as connection:
            connection.execute(
                "UPDATE players SET remainder_give_gift_num=2, companion_reset_day='2000-01-01', "
                "fondle_num=3, next_recovery_fondle_time=? WHERE uid=?",
                (int(__import__('time').time()) - 10801, uid),
            )
        state = storage.get_player_companion_state(uid)
        self.assertEqual(state["remainder_give_gift_num"], 10)
        self.assertEqual(state["fondle_num"], 5)
        self.assertEqual(state["next_recovery_fondle_time"], 0)


    def test_player_state_json_seeds_and_overlays_simple_fields(self):
        account = storage.get_or_create_account("json-state-account")
        uid = account["uid"]
        sample = {
            "abyssCid": 25030003,
            "equipSkins": {44050001: 0, 44007001: 1},
            "functionTypes": [1, 2, 3],
            "finishMazes": [1001, 1002],
        }
        n = storage.seed_player_state_json(uid, sample)
        self.assertGreater(n, 0)
        all_fields = storage.get_all_player_state_json(uid)
        self.assertEqual(all_fields.get("abyssCid"), 25030003)
        self.assertEqual(all_fields.get("equipSkins"), {44050001: 0, 44007001: 1})
        self.assertEqual(all_fields.get("functionTypes"), [1, 2, 3])
        self.assertIn(1002, all_fields.get("finishMazes", []))
        self.assertNotIn("souls", all_fields)
        self.assertNotIn("warehouse", all_fields)
        self.assertTrue(storage.update_player_state_json(uid, "finishMazes", [1001, 1002, 1003]))
        updated = storage.get_all_player_state_json(uid)
        self.assertEqual(len(updated["finishMazes"]), 3)

    def test_maze_quest_progress_is_monotonic_and_promotes_completion(self):
        account = storage.get_or_create_account("maze-quest-progress-test")
        uid = account["uid"]
        storage.seed_quest_state(
            uid,
            [{"cid": 901001, "finNum": 0, "tgtNum": 3, "createTime": 1}],
            [],
            [],
            [],
        )

        self.assertEqual(
            storage.update_quest_progress(uid, 901001, 2),
            {"quest_id": 901001, "fin_num": 2, "tgt_num": 3, "completed": False},
        )
        self.assertEqual(
            storage.update_quest_progress(uid, 901001, 1)["fin_num"],
            2,
        )
        completed = storage.update_quest_progress(uid, 901001, 99)
        self.assertEqual(completed["fin_num"], 3)
        self.assertTrue(completed["completed"])
        self.assertIn(901001, storage.get_quest_state(uid)["finishQuestList"])

    def test_maze_quest_progress_rejects_unknown_or_negative_updates(self):
        account = storage.get_or_create_account("maze-quest-invalid-test")
        uid = account["uid"]
        self.assertIsNone(storage.update_quest_progress(uid, 901002, 1))
        self.assertIsNone(storage.update_quest_progress(uid, 901002, -1))

    def test_battle_instance_rejects_unknown_uid(self):
        result = storage.create_battle_instance("nonexistent-uid", 0)
        self.assertIsNone(result)

    def test_battle_instance_duplicate_settle(self):
        account = storage.get_or_create_account("battle-dup-test")
        uid = account["uid"]
        bid = storage.create_battle_instance(uid, 4)
        self.assertIsNotNone(bid)
        r1 = storage.settle_battle(uid, bid, 1)
        self.assertIsNotNone(r1)
        r2 = storage.settle_battle(uid, bid, 1)
        self.assertIsNone(r2)

    def test_battle_rewards_by_type(self):
        account = storage.get_or_create_account("battle-reward-test")
        uid = account["uid"]
        for btype in [0, 1, 4, 6]:
            bid = storage.create_battle_instance(uid, btype)
            self.assertIsNotNone(bid)
            result = storage.settle_battle(uid, bid, 1)
            self.assertIsNotNone(result, f"type={btype}")
            self.assertGreater(len(result["rewards"]), 0)

    def test_battle_loss_persists_report_without_rewards(self):
        account = storage.get_or_create_account("battle-loss-test")
        uid = account["uid"]
        before_attrs = storage.get_player_num_attrs(uid)
        before_items = storage.get_items(uid)
        bid = storage.create_battle_instance(uid, 4, 25020100, 10101101)
        result = storage.settle_battle(
            uid, bid, 0, rounds=9, report={"userData": "local-loss"}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["rewards"], [])
        self.assertEqual(storage.get_player_num_attrs(uid), before_attrs)
        self.assertEqual(storage.get_items(uid), before_items)
        battle = storage.get_battle_instance(uid, bid)
        self.assertEqual(battle["status"], "lost")
        self.assertEqual(battle["rounds"], 9)
        self.assertIn("local-loss", battle["report_json"])

    def test_battle_start_resume_and_abandon_lifecycle(self):
        account = storage.get_or_create_account("battle-lifecycle-test")
        uid = account["uid"]
        first = storage.create_battle_instance(
            uid, 4, 25020100, 10101101, reuse_active=True
        )
        resumed = storage.create_battle_instance(
            uid, 4, 25020100, 10101101, reuse_active=True
        )
        self.assertEqual(resumed, first)
        active = storage.get_active_battle(uid)
        self.assertEqual(active["id"], first)
        self.assertGreater(active["random_seed"], 0)
        self.assertEqual(storage.abandon_active_battles(uid, 25020100), 1)
        self.assertIsNone(storage.get_active_battle(uid))
        self.assertEqual(storage.get_battle_instance(uid, first)["status"], "abandoned")

    def test_equipment_wear_dump_cycle(self):
        account = storage.get_or_create_account("equip-test")
        uid = account["uid"]
        self.seed_test_equipment(uid, 3)
        items = storage.get_equipment_instances(uid)
        self.assertGreater(len(items), 0)
        eid = items[0]["id"]
        self.assertTrue(storage.wear_equipment(uid, eid, 20010001, 1))
        equipped = storage.get_equipment_instances(uid)
        equipped_item = next(i for i in equipped if i["id"] == eid)
        self.assertEqual(equipped_item["equipped_to"], 20010001)
        count = storage.dump_equipment(uid, [eid])
        self.assertEqual(count, 1)
        dumped = storage.get_equipment_instances(uid)
        dumped_item = next(i for i in dumped if i["id"] == eid)
        self.assertIsNone(dumped_item["equipped_to"])

    def test_equipment_upgrade_and_upstar(self):
        account = storage.get_or_create_account("equip-upgrade")
        uid = account["uid"]
        self.seed_test_equipment(uid, 5)
        storage.add_item(uid, 10006, 5)
        items = storage.get_equipment_instances(uid)
        target = items[0]
        result = storage.upgrade_equipment(uid, target["id"], {10006: 5})
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 1)
        fodders = [i["id"] for i in items[1:4]]
        self.assertTrue(storage.upstar_equipment(uid, target["id"], fodders))

    def test_equipment_decompose(self):
        account = storage.get_or_create_account("equip-decomp")
        uid = account["uid"]
        self.seed_test_equipment(uid, 2)
        items = storage.get_equipment_instances(uid)
        eids = [i["id"] for i in items]
        result = storage.decp_equipment(uid, eids)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 0)
        remaining = storage.get_equipment_instances(uid)
        self.assertEqual(len(remaining), 0)

    def test_sign_in_grants_configured_reward_and_is_idempotent(self):
        account = storage.get_or_create_account("sign-reward")
        first = storage.record_sign_in(account["uid"])
        self.assertEqual(first["sign_info"].bit_count(), 1)
        self.assertEqual(first["rewards"], [{"cid": 2, "num": 20, "tag": 0}])
        self.assertEqual(first["quest"]["finNum"], 1)
        self.assertEqual(storage.get_items(account["uid"]), [])
        self.assertEqual(storage.get_player_num_attrs(account["uid"])[2], 20)
        second = storage.record_sign_in(account["uid"])
        self.assertTrue(second["already_signed"])
        self.assertEqual(second["rewards"], [])
        self.assertEqual(second["quest"]["finNum"], 1)

    def test_stale_snapshot_cannot_reinsert_completed_sign_task(self):
        account = storage.get_or_create_account("sign-snapshot-repair")
        uid = account["uid"]
        storage.record_sign_in(uid)
        storage.seed_quest_state(uid, [], [storage.SIGN_IN_QUEST_ID], [], [])
        storage.reconcile_sign_in_quest(uid)
        state = storage.get_quest_state(uid)
        self.assertNotIn(storage.SIGN_IN_QUEST_ID, state["finishQuestList"])
        quest = next(row for row in state["quests"] if row["cid"] == storage.SIGN_IN_QUEST_ID)
        self.assertEqual((quest["finNum"], quest["tgtNum"]), (1, 7))

    def test_sign_in_repairs_stale_seven_day_quest_from_bitmask(self):
        account = storage.get_or_create_account("sign-quest-repair")
        uid = account["uid"]
        first = storage.record_sign_in(uid)
        with sqlite3.connect(TEST_DB) as connection:
            connection.execute(
                "UPDATE quest_progress SET fin_num=5 WHERE uid=? AND quest_cid=?",
                (uid, storage.SIGN_IN_QUEST_ID),
            )
            connection.execute(
                "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                (uid, "finish", storage.SIGN_IN_QUEST_ID),
            )
        repaired = storage.record_sign_in(uid)
        self.assertTrue(repaired["already_signed"])
        self.assertEqual(repaired["quest"]["finNum"], first["sign_info"].bit_count())
        self.assertFalse(repaired["quest"]["completed"])
        state = storage.get_quest_state(uid)
        quest = next(row for row in state["quests"] if row["cid"] == storage.SIGN_IN_QUEST_ID)
        self.assertEqual(quest["finNum"], 1)
        self.assertNotIn(storage.SIGN_IN_QUEST_ID, state["finishQuestList"])
        self.assertEqual(storage.get_player_state_json(uid, "signInfo"), first["sign_info"])

    def test_equipment_guards_reject_locked_or_equipped_consumables(self):
        account = storage.get_or_create_account("equip-guards")
        uid = account["uid"]
        self.seed_test_equipment(uid, 3)
        items = storage.get_equipment_instances(uid)
        target, fodder, equipped = items
        self.assertTrue(storage.set_equipment_locked(uid, fodder["id"]))
        self.assertFalse(storage.upstar_equipment(uid, target["id"], [fodder["id"]]))
        self.assertTrue(storage.wear_equipment(uid, equipped["id"], 20010001, 1))
        self.assertIsNone(storage.decp_equipment(uid, [equipped["id"]]))

    def test_soul_and_equipment_prefab_state_is_persisted(self):
        account = storage.get_or_create_account("prefab-state")
        uid = account["uid"]
        self.seed_test_equipment(uid, 2)
        equipment = storage.get_equipment_instances(uid)
        self.assertIsNotNone(storage.update_soul_prefab(uid, 7, 20010001, 1, [11], 0))
        self.assertTrue(storage.save_equipment_prefab(uid, 3, {1: equipment[0]["id"]}))
        self.assertTrue(storage.wear_equipment_prefab(uid, 7, 3))
        self.assertTrue(storage.update_soul_prefab_position(uid, 7, 4))
        self.assertTrue(storage.set_soul_prefab_jewelry_speed(uid, 7, 46601, 2))
        self.assertTrue(storage.rename_equipment_prefab(uid, 3, "farm"))
        prefab = storage.get_player_state_json(uid, "soulPrefabs")[0]
        self.assertEqual(prefab["position"], 4)
        self.assertEqual(prefab["equipments"]["1"], equipment[0]["id"])
        self.assertEqual(storage.get_player_state_json(uid, "equipmentPrefabs")[0]["name"], "farm")

    def test_seed_player_state_skips_unknown_uid(self):
        n = storage.seed_player_state_json("nonexistent-uid", {"testField": 1})
        self.assertEqual(n, 0)

    def test_lottery_missing_server_drop_rolls_back_instead_of_fabricating_fragments(self):
        account = storage.get_or_create_account("lottery-missing-drop-rollback")
        uid = account["uid"]
        storage.add_item(uid, 10006, 1)
        result = storage.perform_lottery_draw(
            uid,
            9997,
            999701,
            [],
            {"999701": {
                "lotteryMode": 1,
                "costCid": 10006,
                "costNum": 1,
                "packIds": [[21001]],
            }},
            {"21001": {"weight": 1, "dropId": 11220001}},
            {},
        )
        self.assertIsNone(result)
        items = {
            row["template_id"]: row["quantity"]
            for row in storage.get_items(uid)
        }
        self.assertEqual(items[10006], 1)
        self.assertNotIn(10201, items)
        self.assertEqual(storage.get_lottery_history(uid), [])

    def test_lottery_unknown_server_drop_fails_instead_of_granting_currency(self):
        account = storage.get_or_create_account("lottery-unknown-drop")
        uid = account["uid"]
        result = storage.perform_lottery_draw(
            uid,
            100,
            101,
            [],
            {"101": {"lotteryMode": 1, "costCid": 0, "costNum": 0, "packIds": [[1]]}},
            {"1": {"weight": 1, "dropId": 99999999}},
            {},
        )
        self.assertIsNone(result)

    def test_equipment_ten_draw_excludes_zero_weight_packs_and_creates_instances(self):
        account = storage.get_or_create_account("equipment-ten-draw")
        uid = account["uid"]
        storage.add_item(uid, 10005, 10)
        actions = {
            "2000302": {
                "lotteryMode": 2, "baseDrop": 11100006,
                "costCid": 10005, "costNum": 10,
                "packIds": [[100001, 100002, 100003, 61001] for _ in range(10)],
            }
        }
        packs = {
            "100001": {"weight": 1250, "dropId": 11300001},
            "100002": {"weight": 2750, "dropId": 11300002},
            "100003": {"weight": 850, "dropId": 11300003},
            "61001": {"weight": 0, "dropId": 11310001},
        }
        drops = {
            "11100006": [{"cid": 116, "num": 10}],
            "11300001": [{"cid": 44001, "num": 1}],
            "11300002": [{"cid": 43001, "num": 1}],
            "11300003": [{"cid": 42001, "num": 1}],
            "11310001": [{"cid": 10201, "num": 1}],
        }
        result = storage.perform_lottery_draw(
            uid, 20003, 2000302, [], actions, packs, drops,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["baseShowItems"], [{"cid": 116, "num": 10, "tag": 0}])
        self.assertEqual(len(result["showItems"]), 10)
        self.assertTrue(all(storage._ITEM_TYPE_BY_CID[item["cid"]] == 3 for item in result["showItems"]))
        self.assertNotIn(10201, [item["cid"] for item in result["showItems"]])
        instances = storage.get_equipment_instances(uid)
        self.assertEqual(len(instances), 10)
        self.assertTrue(all(instance["id"] >= storage.LOCAL_EQUIPMENT_ID_BASE for instance in instances))
        self.assertTrue(all(instance["star"] == 1 for instance in instances))
        self.assertFalse(any(item["template_id"] in (42001, 43001, 44001) for item in storage.get_items(uid)))
        self.assertFalse(any(item["template_id"] == 10005 for item in storage.get_items(uid)))
        self.assertEqual(storage.get_player_num_attrs(uid)[116], 10)
        equipment_changes = [item for item in result["changed_items"] if item.get("equipmentData")]
        self.assertEqual(len(equipment_changes), 10)
        self.assertEqual(len({item["id"] for item in equipment_changes}), 10)

    def test_equipment_draw_rolls_back_cost_when_drop_is_invalid(self):
        account = storage.get_or_create_account("equipment-draw-rollback")
        uid = account["uid"]
        storage.add_item(uid, 10005, 10)
        actions = {"2000302": {
            "lotteryMode": 2, "costCid": 10005, "costNum": 10,
            "packIds": [[61001] for _ in range(10)],
        }}
        result = storage.perform_lottery_draw(
            uid, 20003, 2000302, [], actions,
            {"61001": {"weight": 0, "dropId": 11310001}},
            {"11310001": [{"cid": 10201, "num": 1}]},
        )
        self.assertIsNone(result)
        self.assertEqual(next(item["quantity"] for item in storage.get_items(uid) if item["template_id"] == 10005), 10)
        self.assertEqual(storage.get_equipment_instances(uid), [])
        self.assertEqual(storage.get_lottery_history(uid), [])

    def test_equipment_pickup_id_resolves_to_configured_ssr_template(self):
        account = storage.get_or_create_account("equipment-pickup")
        uid = account["uid"]
        result = storage.perform_lottery_draw(
            uid, 20003, 2000301, [110001],
            {"2000301": {"lotteryMode": 1, "baseDrop": 11100005, "costCid": 0, "costNum": 0, "packIds": [[100001]]}},
            {"100001": {"weight": 0, "dropId": 11300001}},
            {
                "11100005": [{"cid": 116, "num": 1}],
                "11300001": [{"cid": 44002, "num": 1}],
            },
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["baseShowItems"], [{"cid": 116, "num": 1, "tag": 0}])
        self.assertEqual(result["showItems"], [{"cid": 44001, "num": 1, "tag": 0}])
        self.assertEqual(storage.get_equipment_instances(uid)[0]["template_id"], 44001)

    def test_character_lottery_pools_accept_soul_template_cids(self):
        for show_cid, lottery_cid in ((20000, 999899), (9998, 999801)):
            with self.subTest(show=show_cid, pool=lottery_cid):
                pool = storage.resolve_lottery_pool(
                    show_cid, lottery_cid, [], storage.LOTTERY_TIER_CONFIG
                )
                self.assertTrue(pool["topItems"])
                self.assertTrue(all(
                    20010001 <= cid <= 20010055 and cid != 20010026
                    for cid in pool["topItems"]
                ))

    def test_local_lottery_up_overrides_select_requested_test_rewards(self):
        expected = (
            (9997, 999701, [1207], {20010019, 20010002}),
            (9996, 999905, [2210], {44001, 44002}),
        )
        for show_cid, lottery_cid, selection, top_items in expected:
            with self.subTest(show=show_cid, pool=lottery_cid):
                pool = storage.resolve_lottery_pool(
                    show_cid, lottery_cid, selection, storage.LOTTERY_TIER_CONFIG
                )
                self.assertTrue(top_items.issubset(pool["topItems"]))
                self.assertEqual(pool["upItems"], frozenset(top_items))
                self.assertEqual(
                    len(pool["tiers"][0]["items"]),
                    len(set(pool["tiers"][0]["items"])),
                )

    def test_local_lottery_up_overrides_keep_probability_scale_and_tail(self):
        expected = (
            (9997, 1207, 20010019, 20010002, 99820, 1360),
            (9996, 2210, 44001, 44002, 100020, 1510),
        )
        for show_cid, group_id, first, second, expected_weight, expected_up_weight in expected:
            with self.subTest(show=show_cid, group=group_id):
                show = storage.LOTTERY_TIER_CONFIG["shows"][str(show_cid)]
                pool = storage.resolve_lottery_pool(
                    show_cid,
                    show["pools"][0],
                    [group_id],
                    storage.LOTTERY_TIER_CONFIG,
                )
                top = pool["tiers"][0]
                self.assertEqual(top["items"][:2], [first, second])
                total_weight = sum(
                    sum(tier["weights"]) for tier in pool["tiers"]
                )
                self.assertEqual(total_weight, expected_weight)
                weights_by_item = dict(zip(top["items"], top["weights"]))
                self.assertEqual(weights_by_item[first], expected_up_weight)
                self.assertEqual(weights_by_item[second], expected_up_weight)
                self.assertEqual(
                    Fraction(weights_by_item[first], total_weight),
                    Fraction(expected_up_weight, expected_weight),
                )

    def test_local_lottery_pity_uses_any_top_tier_ssr_not_only_up(self):
        expected = (
            (9997, 1207),
            (9996, 2210),
        )
        for show_cid, group_id in expected:
            with self.subTest(show=show_cid, group=group_id):
                show = storage.LOTTERY_TIER_CONFIG["shows"][str(show_cid)]
                pool = storage.resolve_lottery_pool(
                    show_cid,
                    show["pools"][0],
                    [group_id],
                    storage.LOTTERY_TIER_CONFIG,
                )
                non_up_top = next(
                    cid for cid in pool["topItems"] if cid not in pool["upItems"]
                )
                lower_tier_item = pool["tiers"][1]["items"][0]

                class PityChoice:
                    calls = 0

                    def choices(self, population, weights, k):
                        self.calls += 1
                        if self.calls == pool["insure"]:
                            self.assert_candidate(population, non_up_top)
                            return [non_up_top]
                        self.assert_candidate(population, lower_tier_item)
                        return [lower_tier_item]

                    @staticmethod
                    def assert_candidate(population, candidate):
                        if candidate not in population:
                            raise AssertionError(f"candidate {candidate} missing from draw pool")

                chooser = PityChoice()
                drawn, left = storage._draw_from_pool(
                    pool,
                    pool["insure"],
                    pool["insure"],
                    chooser,
                )
                self.assertEqual(chooser.calls, pool["insure"])
                self.assertEqual(drawn[-1], non_up_top)
                self.assertTrue(all(cid == lower_tier_item for cid in drawn[:-1]))
                self.assertEqual(left, pool["insure"])

    def test_character_lottery_draws_charge_and_commit(self):
        account = storage.get_or_create_account("character-lottery-draw")
        uid = account["uid"]
        storage.add_item(uid, 10029, 1)
        storage.add_item(uid, 10006, 1)
        actions = json.loads(
            (storage.ROOT / "analysis" / "lottery_actions.json").read_text(encoding="utf-8")
        )
        drops = json.loads(
            (storage.ROOT / "analysis" / "lottery_drop_config.json").read_text(encoding="utf-8")
        ).get("drops", {})

        class FirstChoice:
            @staticmethod
            def choices(population, weights, k):
                return [population[0]] * k

        for show_cid, lottery_cid in ((20000, 999899), (9998, 999801)):
            result = storage.perform_lottery_draw(
                uid,
                show_cid,
                lottery_cid,
                [],
                actions,
                storage.LOTTERY_TIER_CONFIG,
                drops,
                FirstChoice(),
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["showItems"][0]["cid"], 10101)
            self.assertFalse(any(
                row["template_id"] in (10101, 20010001)
                for row in storage.get_items(uid)
            ))

    def test_character_lottery_unlocks_soul_and_converts_duplicate(self):
        account = storage.get_or_create_account("character-lottery-soul-state")
        uid = account["uid"]
        actions = {"1": {"lotteryMode": 1, "costCid": 0, "costNum": 0}}
        tiers = {
            "shows": {
                "1": {
                    "choiceNum": 0,
                    "insureTimes": [1],
                    "pools": [1],
                    "tiers": [{"order": 1, "items": [20010017], "weights": [1]}],
                    "upGroups": [],
                }
            },
            "upGroups": {},
        }

        class FirstChoice:
            @staticmethod
            def choices(population, weights, k):
                return [population[0]] * k

        first = storage.perform_lottery_draw(
            uid, 1, 1, [], actions, tiers, {}, FirstChoice()
        )
        self.assertIsNotNone(first)
        self.assertEqual(first["showItems"], [{"cid": 10117, "num": 1, "tag": 0}])
        self.assertEqual(first["newSoulIds"], [20010017])
        self.assertEqual(first["duplicateSoulIds"], [])
        self.assertTrue(any(row["soul_id"] == 20010017 for row in storage.get_souls(uid)))
        self.assertFalse(any(
            row["template_id"] in (10117, 20010017, 10217)
            for row in storage.get_items(uid)
        ))

        second = storage.perform_lottery_draw(
            uid, 1, 1, [], actions, tiers, {}, FirstChoice()
        )
        self.assertIsNotNone(second)
        self.assertEqual(second["showItems"], [{"cid": 10117, "num": 1, "tag": 0}])
        self.assertEqual(second["newSoulIds"], [])
        self.assertEqual(second["duplicateSoulIds"], [20010017])
        self.assertEqual(
            next(row["quantity"] for row in storage.get_items(uid) if row["template_id"] == 10217),
            20,
        )

    def test_invalid_historical_soul_template_items_migrate_to_fragments(self):
        account = storage.get_or_create_account("historical-soul-template-item")
        uid = account["uid"]
        storage.add_item(uid, 20010017, 2)

        migration = storage.migrate_invalid_soul_lottery_items()

        self.assertEqual(migration["converted"], 1)
        self.assertTrue(migration["backup"])
        items = {row["template_id"]: row["quantity"] for row in storage.get_items(uid)}
        self.assertNotIn(20010017, items)
        self.assertEqual(items[10217], 40)


    def test_official_up_selection_grants_selected_soul_template(self):
        account = storage.get_or_create_account("official-up-selection")
        uid = account["uid"]
        storage.add_item(uid, 10005, 1)
        actions = json.loads(
            (storage.ROOT / "analysis" / "lottery_actions.json").read_text(encoding="utf-8")
        )
        drops = json.loads(
            (storage.ROOT / "analysis" / "lottery_drop_config.json").read_text(encoding="utf-8")
        ).get("drops", {})

        class FirstChoice:
            @staticmethod
            def choices(population, weights, k):
                return [population[0]]

        result = storage.perform_lottery_draw(
            uid, 20003, 2000301,
            [110149, 110150, 110147, 110146, 110145],
            actions, storage.LOTTERY_TIER_CONFIG, drops, FirstChoice(),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["showItems"], [{"cid": 44149, "num": 1, "tag": 0}])
        self.assertEqual(storage.get_equipment_instances(uid)[0]["template_id"], 44149)
        self.assertEqual(storage.get_items(uid), [])


def tearDownModule():
    gc.collect()
    TEST_DIR.cleanup()


if __name__ == "__main__":
    unittest.main()
