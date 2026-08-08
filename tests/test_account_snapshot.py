import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import account_snapshot  # noqa: E402


def integer_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from integer_values(key)
            yield from integer_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from integer_values(item)
    elif isinstance(value, int):
        yield value


@unittest.skipUnless(
    (ROOT / "soultide.db").is_file(),
    "requires a private local database fixture",
)
class AccountSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="soultide-account-snapshot-")
        self.temp_root = Path(self.temp_dir.name)
        self.database = self.temp_root / "soultide.db"
        shutil.copy2(ROOT / "soultide.db", self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT uid FROM players WHERE snapshot_mode='local_fixture_overlay' LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.source_uid = row[0]
        self.snapshot_path = self.temp_root / "account.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_redacts_account_identifiers(self):
        snapshot = account_snapshot.export_snapshot(
            self.database, self.source_uid, self.snapshot_path
        )
        encoded = self.snapshot_path.read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection:
            source = connection.execute(
                "SELECT channel_uid,uuid FROM accounts WHERE uid=?", (self.source_uid,)
            ).fetchone()
        self.assertNotIn(self.source_uid, encoded)
        self.assertNotIn(source[0], encoded)
        self.assertNotIn(source[1], encoded)
        self.assertEqual(len(snapshot["tables"]["player_state_json"]), 92)
        self.assertIn("__SOURCE_UID__", encoded)

    def test_round_trip_import_remaps_global_ids(self):
        account_snapshot.export_snapshot(
            self.database, self.source_uid, self.snapshot_path
        )
        result = account_snapshot.import_snapshot(
            self.database,
            self.snapshot_path,
            "snapshot-test-automated",
            "snapshot-test",
            "46",
            False,
        )
        target_uid = result["targetUid"]
        with closing(sqlite3.connect(self.database)) as connection:
            counts = {
                table: connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE uid=?', (target_uid,)
                ).fetchone()[0]
                for table in account_snapshot.ACCOUNT_TABLES
            }
            source_equipment = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM equipment_instances WHERE uid=?", (self.source_uid,)
                )
            }
            target_equipment = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM equipment_instances WHERE uid=?", (target_uid,)
                )
            }
            state_json = [
                row[0]
                for row in connection.execute(
                    "SELECT value_json FROM player_state_json WHERE uid=?", (target_uid,)
                )
            ]
        self.assertEqual(counts["player_state_json"], 92)
        self.assertEqual(counts["equipment_instances"], 846)
        self.assertTrue(source_equipment.isdisjoint(target_equipment))
        target_values = set()
        for value in state_json:
            target_values.update(integer_values(json.loads(value)))
        self.assertTrue(target_equipment.intersection(target_values))
        self.assertFalse(source_equipment.intersection(target_values))

    def test_soulaccount_artifact_preserves_snapshot_contract(self):
        artifact = self.temp_root / "player.soulaccount"
        exported = account_snapshot.export_snapshot(
            self.database, self.source_uid, artifact
        )
        envelope = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(envelope["kind"], account_snapshot.ACCOUNT_ARTIFACT_KIND)
        self.assertEqual(account_snapshot.load_snapshot(artifact), exported)
        envelope["payload"]["tables"]["players"][0]["uid"] = "tampered"
        artifact.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(ValueError):
            account_snapshot.load_snapshot(artifact)

    def test_legacy_whisper_list_is_promoted_on_import(self):
        account_snapshot.export_snapshot(
            self.database, self.source_uid, self.snapshot_path
        )
        snapshot = account_snapshot.load_snapshot(self.snapshot_path)
        state_rows = snapshot["tables"]["player_state_json"]
        state_rows[:] = [
            row for row in state_rows if row["field_name"] != "soulWhisperUnlocks"
        ]
        legacy = next(
            row for row in state_rows if row["field_name"] == "unlockSoulWhispers"
        )
        expected = set(json.loads(legacy["value_json"]))
        self.snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )
        result = account_snapshot.import_snapshot(
            self.database,
            self.snapshot_path,
            "snapshot-test-legacy-whisper",
            None,
            None,
            False,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT value_json FROM player_state_json WHERE uid=? AND field_name=?",
                (result["targetUid"], "soulWhisperUnlocks"),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(set(json.loads(row[0])["whisperIds"]), expected)

    def test_second_import_fails_without_partial_target_rows(self):
        account_snapshot.export_snapshot(
            self.database, self.source_uid, self.snapshot_path
        )
        account_snapshot.import_snapshot(
            self.database,
            self.snapshot_path,
            "snapshot-test-duplicate",
            None,
            None,
            False,
        )
        with self.assertRaises(ValueError):
            account_snapshot.import_snapshot(
                self.database,
                self.snapshot_path,
                "snapshot-test-duplicate",
                None,
                None,
                False,
            )
        with closing(sqlite3.connect(self.database)) as connection:
            target_uid = connection.execute(
                "SELECT uid FROM accounts WHERE channel_uid=?", ("snapshot-test-duplicate",)
            ).fetchone()[0]
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM player_state_json WHERE uid=?", (target_uid,)
                ).fetchone()[0],
                92,
            )


if __name__ == "__main__":
    unittest.main()
