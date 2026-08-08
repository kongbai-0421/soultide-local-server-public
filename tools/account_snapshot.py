"""Export, inspect, verify, and import one local account snapshot.

The snapshot is an account-state archive, not an authentication backup.  It
deliberately omits channel credentials, UUIDs, aliases, sessions, and guild
relations.  Import creates a new local identity and maps all account-scoped
rows to it inside one SQLite transaction.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "soultide.db"
SNAPSHOT_KIND = "soultide-account-snapshot"
SNAPSHOT_VERSION = 1
ACCOUNT_ARTIFACT_KIND = "soultide-account-file"
ACCOUNT_ARTIFACT_VERSION = 1
UID_TOKEN = "__SOURCE_UID__"
WHISPER_LIST_FIELD = "unlockSoulWhispers"
WHISPER_STATE_FIELD = "soulWhisperUnlocks"

# Keep this list explicit.  A generic "every table containing uid" export
# would accidentally include sessions, aliases, or cross-account guild state.
ACCOUNT_TABLES = (
    "players",
    "currencies",
    "player_num_attrs",
    "items",
    "souls",
    "equipment_instances",
    "mails",
    "mail_pickup_log",
    "tasks",
    "quest_progress",
    "quest_lists",
    "quest_reward_log",
    "library_state",
    "soul_story_progress",
    "dating_records",
    "active_sign_in",
    "lottery_pool_state",
    "lottery_records",
    "lottery_history",
    "maze_instances",
    "battle_instances",
    "companion_reward_log",
    "player_settings",
    "player_state_json",
)

OMITTED_TABLES = {
    "accounts": "recreated with a new local identity",
    "account_aliases": "authentication/account routing metadata",
    "sessions": "runtime connection state",
    "guilds": "cross-account relation",
    "guild_members": "cross-account relation",
    "guild_applications": "cross-account relation",
    "guild_buildings": "cross-account relation",
}

SENSITIVE_FIELD_RE = re.compile(
    r"(?:token|password|passwd|cookie|secret|credential|authorization|session)",
    re.IGNORECASE,
)


def database_path(value: str | None) -> Path:
    return Path(value or os.environ.get("SOULTIDE_DB_PATH", DEFAULT_DB)).resolve()


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def open_write(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def require_tables(connection: sqlite3.Connection, names: tuple[str, ...]) -> None:
    missing = sorted(set(names) - table_names(connection))
    if missing:
        raise ValueError("database is missing required tables: " + ", ".join(missing))


def resolve_account(connection: sqlite3.Connection, identity: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT a.* FROM account_aliases aa
        JOIN accounts a ON a.uid=aa.target_uid
        WHERE aa.alias_channel_uid=?
        LIMIT 1
        """,
        (identity,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT * FROM accounts
            WHERE uid=? OR uuid=? OR channel_uid=?
            LIMIT 1
            """,
            (identity, identity, identity),
        ).fetchone()
    if row is None:
        raise ValueError(f"account identity was not found: {identity}")
    return row


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def json_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def sanitize_value(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, replacement in replacements.items():
            value = value.replace(source, replacement)
        return value
    if isinstance(value, list):
        return [sanitize_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_value(key, replacements): sanitize_value(item, replacements)
            for key, item in value.items()
        }
    return value


def sanitize_row(row: dict[str, Any], replacements: dict[str, str]) -> dict[str, Any]:
    return {key: sanitize_value(value, replacements) for key, value in row.items()}


def whisper_ids(value: Any) -> set[int]:
    """Read both official PlayerPOD and local whisper-state formats."""
    if isinstance(value, dict):
        value = value.get("whisperIds", [])
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for item in value:
        try:
            item = int(item)
        except (TypeError, ValueError):
            continue
        if item > 0:
            result.add(item)
    return result


def normalize_whisper_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep legacy and canonical whisper unlock fields synchronized.

    Older captures only contained ``unlockSoulWhispers`` from PlayerPOD.  The
    local mall handler reads ``soulWhisperUnlocks.whisperIds`` instead, so a
    snapshot with only the former lost private whispers after import.
    """
    normalized = [dict(row) for row in rows]
    by_name = {
        str(row.get("field_name")): row
        for row in normalized
        if isinstance(row, dict) and row.get("field_name")
    }
    legacy = by_name.get(WHISPER_LIST_FIELD)
    canonical = by_name.get(WHISPER_STATE_FIELD)
    if legacy is None and canonical is None:
        return normalized

    all_ids = set()
    if legacy is not None:
        try:
            all_ids |= whisper_ids(json.loads(str(legacy.get("value_json") or "[]")))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if canonical is not None:
        try:
            all_ids |= whisper_ids(json.loads(str(canonical.get("value_json") or "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if not all_ids:
        return normalized

    values = sorted(all_ids)
    if legacy is not None:
        legacy["value_json"] = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if canonical is None:
        template = dict(legacy or {})
        template["field_name"] = WHISPER_STATE_FIELD
        template["value_json"] = json.dumps(
            {"whisperIds": values}, ensure_ascii=False, separators=(",", ":")
        )
        normalized.append(template)
    else:
        canonical["value_json"] = json.dumps(
            {"whisperIds": values}, ensure_ascii=False, separators=(",", ":")
        )
    return normalized


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def collect_table_rows(
    connection: sqlite3.Connection,
    table: str,
    uid: str,
    replacements: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    if "uid" not in columns:
        raise ValueError(f"account table has no uid column: {table}")
    rows = connection.execute(
        f'SELECT * FROM "{table}" WHERE uid=? ORDER BY rowid', (uid,)
    ).fetchall()
    sensitive_fields: list[str] = []
    result: list[dict[str, Any]] = []
    for row in rows:
        data = sanitize_row(row_dict(row), replacements)
        for field in list(data):
            if table == "player_state_json" and field == "field_name":
                field_name = str(data[field])
                if SENSITIVE_FIELD_RE.search(field_name):
                    sensitive_fields.append(field_name)
                    data = None
                    break
        if data is not None:
            # UID is always normalized even if the row was unusual or the
            # source UID appeared in a nested value.
            data["uid"] = UID_TOKEN
            result.append(data)
    return result, sorted(set(sensitive_fields))


def account_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    tables = snapshot.get("tables") or {}
    return {
        "kind": snapshot.get("kind"),
        "version": snapshot.get("version"),
        "sourceUidSha256": (snapshot.get("source") or {}).get("uidSha256"),
        "role": (snapshot.get("source") or {}).get("role"),
        "tables": {name: len(rows) for name, rows in tables.items()},
        "omitted": snapshot.get("omitted", {}),
        "sensitiveFieldsOmitted": snapshot.get("sensitiveFieldsOmitted", []),
    }


def export_snapshot(db: Path, identity: str, output: Path) -> dict[str, Any]:
    with closing(open_readonly(db)) as connection:
        require_tables(connection, ("accounts",) + ACCOUNT_TABLES)
        account = resolve_account(connection, identity)
        uid = str(account["uid"])
        player = connection.execute(
            "SELECT * FROM players WHERE uid=? LIMIT 1", (uid,)
        ).fetchone()
        if player is None:
            raise ValueError(f"account has no player row: {uid}")

        replacements = {
            uid: UID_TOKEN,
            str(account["uuid"]): "__SOURCE_UUID__",
            str(account["channel_uid"]): "__SOURCE_CHANNEL_UID__",
        }
        tables: dict[str, list[dict[str, Any]]] = {}
        sensitive_fields: list[str] = []
        for table in ACCOUNT_TABLES:
            rows, omitted = collect_table_rows(connection, table, uid, replacements)
            tables[table] = rows
            sensitive_fields.extend(f"{table}.{field}" for field in omitted)
        tables["player_state_json"] = normalize_whisper_rows(
            tables["player_state_json"]
        )

        player_data = row_dict(player)
        snapshot = {
            "kind": SNAPSHOT_KIND,
            "version": SNAPSHOT_VERSION,
            "createdAt": int(time.time()),
            "source": {
                "uidSha256": stable_digest(uid),
                "channelUidSha256": stable_digest(str(account["channel_uid"])),
                "role": {
                    "roleId": player_data.get("role_id", ""),
                    "roleName": player_data.get("role_name", ""),
                    "level": player_data.get("level", 0),
                },
                "username": str(account["username"]),
                "channelId": str(account["channel_id"]),
            },
            "tables": tables,
            "omitted": OMITTED_TABLES,
            "sensitiveFieldsOmitted": sorted(set(sensitive_fields)),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.part-{os.getpid()}")
    if output.suffix.lower() == ".soulaccount":
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        value = {
            "kind": ACCOUNT_ARTIFACT_KIND,
            "version": ACCOUNT_ARTIFACT_VERSION,
            "payloadSha256": hashlib.sha256(payload).hexdigest(),
            "payload": snapshot,
        }
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    else:
        encoded = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, output)
    return snapshot


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid snapshot JSON: {path}: {exc}") from exc
    if isinstance(snapshot, dict) and snapshot.get("kind") == ACCOUNT_ARTIFACT_KIND:
        if snapshot.get("version") != ACCOUNT_ARTIFACT_VERSION:
            raise ValueError(f"unsupported account file version: {snapshot.get('version')}")
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("account file payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != str(snapshot.get("payloadSha256")):
            raise ValueError("account file checksum mismatch")
        snapshot = payload
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != SNAPSHOT_KIND:
        raise ValueError("not a Soul Tide account snapshot")
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported snapshot version: {snapshot.get('version')}")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("snapshot tables must be an object")
    missing = sorted(set(ACCOUNT_TABLES) - set(tables))
    if missing:
        raise ValueError("snapshot is missing table sections: " + ", ".join(missing))
    for table in ACCOUNT_TABLES:
        if not isinstance(tables[table], list):
            raise ValueError(f"snapshot table is not a list: {table}")
        for index, row in enumerate(tables[table]):
            if not isinstance(row, dict):
                raise ValueError(f"snapshot row is not an object: {table}[{index}]")
            if row.get("uid") != UID_TOKEN:
                raise ValueError(f"snapshot row has invalid UID marker: {table}[{index}]")


def target_identity(channel_uid: str) -> tuple[str, str]:
    uid = hashlib.md5(channel_uid.encode("utf-8")).hexdigest()
    stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "soultide:" + channel_uid))
    return uid, stable_uuid


def destination_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def remap_nested_ids(value: Any, replacements: dict[str, str]) -> Any:
    """Rewrite exact IDs inside JSON while leaving ordinary text untouched."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            mapped_key = replacements.get(str(key), key)
            result[mapped_key] = remap_nested_ids(item, replacements)
        return result
    if isinstance(value, list):
        return [remap_nested_ids(item, replacements) for item in value]
    if isinstance(value, int):
        return int(replacements.get(str(value), value))
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def remap_serialized_fields(row: dict[str, Any], replacements: dict[str, str]) -> dict[str, Any]:
    """Remap IDs in JSON-bearing fields without changing non-JSON strings."""
    result = dict(row)
    for field, raw_value in list(result.items()):
        if not isinstance(raw_value, str):
            continue
        if field != "value_json" and not field.endswith("_json") and field not in {"extra", "save_data"}:
            continue
        try:
            decoded = json.loads(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            result[field] = replacements.get(raw_value, raw_value)
            continue
        result[field] = json.dumps(
            remap_nested_ids(decoded, replacements),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return result


def allocate_equipment_ids(
    connection: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, str]:
    if not rows:
        return {}
    current = connection.execute(
        "SELECT COALESCE(MAX(id),0) FROM equipment_instances"
    ).fetchone()[0]
    # Match the reserved long-ID range used by storage.py for local equipment.
    next_id = max(int(current or 0) + 1, 6_000_000_000_000_000_001)
    replacements: dict[str, str] = {}
    for row in rows:
        try:
            source_id = int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("equipment snapshot contains an invalid instance ID") from exc
        if str(source_id) in replacements:
            raise ValueError(f"equipment snapshot contains duplicate instance ID: {source_id}")
        replacements[str(source_id)] = str(next_id)
        next_id += 1
    return replacements


def allocate_battle_ids(rows: list[dict[str, Any]]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for row in rows:
        source_id = str(row.get("id", ""))
        if not source_id:
            raise ValueError("battle snapshot contains an empty instance ID")
        if source_id in replacements:
            raise ValueError(f"battle snapshot contains duplicate instance ID: {source_id}")
        replacements[source_id] = str(uuid.uuid4())
    return replacements


def prepare_import_rows(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    tables = dict(snapshot["tables"])
    tables["player_state_json"] = normalize_whisper_rows(
        tables["player_state_json"]
    )
    equipment_replacements = allocate_equipment_ids(
        connection, tables["equipment_instances"]
    )
    battle_replacements = allocate_battle_ids(tables["battle_instances"])
    replacements = {**equipment_replacements, **battle_replacements}
    prepared: dict[str, list[dict[str, Any]]] = {}
    for table in ACCOUNT_TABLES:
        rows = [remap_serialized_fields(dict(row), replacements) for row in tables[table]]
        if table == "equipment_instances":
            for row in rows:
                row["id"] = int(equipment_replacements[str(int(row["id"]))])
        elif table == "battle_instances":
            for row in rows:
                row["id"] = battle_replacements[str(row["id"])]
        prepared[table] = rows
    return prepared


def insert_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: list[dict[str, Any]],
    target_uid: str,
    omit_columns: set[str] | None = None,
) -> int:
    omit_columns = omit_columns or set()
    columns = destination_columns(connection, table)
    if not columns:
        raise ValueError(f"destination table does not exist: {table}")
    if "uid" not in columns:
        raise ValueError(f"destination account table has no uid column: {table}")
    unknown = sorted(
        {key for row in rows for key in row}
        - set(columns)
        - omit_columns
    )
    if unknown:
        raise ValueError(f"snapshot columns are not supported by {table}: {', '.join(unknown)}")
    insert_columns = [column for column in columns if column not in omit_columns]
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in insert_columns)
    statement = f'INSERT INTO "{table}" ({",".join(insert_columns)}) VALUES ({placeholders})'
    count = 0
    for row in rows:
        values = [target_uid if row.get(column) == UID_TOKEN else row.get(column) for column in insert_columns]
        connection.execute(statement, values)
        count += 1
    return count


def import_snapshot(
    db: Path,
    snapshot_path: Path,
    target_channel_uid: str,
    target_username: str | None,
    target_channel_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    snapshot = load_snapshot(snapshot_path)
    target_channel_uid = target_channel_uid.strip()
    if not target_channel_uid or target_channel_uid.startswith("__"):
        raise ValueError("target channel UID must be a new non-empty local identifier")
    target_uid, target_uuid = target_identity(target_channel_uid)
    with closing(open_write(db)) as connection:
        require_tables(connection, ("accounts",) + ACCOUNT_TABLES)
        if connection.execute(
            "SELECT 1 FROM accounts WHERE uid=? OR uuid=? OR channel_uid=? LIMIT 1",
            (target_uid, target_uuid, target_channel_uid),
        ).fetchone():
            raise ValueError("target identity already exists; choose a new --target-channel-uid")
        if connection.execute(
            "SELECT 1 FROM account_aliases WHERE alias_channel_uid=? LIMIT 1",
            (target_channel_uid,),
        ).fetchone():
            raise ValueError("target channel UID is already configured as an account alias")
        for table in ACCOUNT_TABLES:
            if connection.execute(
                f'SELECT 1 FROM "{table}" WHERE uid=? LIMIT 1', (target_uid,)
            ).fetchone():
                raise ValueError(f"target already has rows in {table}: {target_uid}")

        counts: dict[str, int] = {}
        if dry_run:
            prepared = prepare_import_rows(connection, snapshot)
            for table, rows in snapshot["tables"].items():
                destination_columns(connection, table)
                counts[table] = len(prepared[table])
            return {"targetUid": target_uid, "targetUuid": target_uuid, "counts": counts, "dryRun": True}

        source = snapshot.get("source") or {}
        now = int(time.time())
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO accounts(
                channel_uid,uid,uuid,username,channel_id,created_at,
                last_http_login_at,last_tcp_login_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                target_channel_uid,
                target_uid,
                target_uuid,
                target_username or str(source.get("username") or "local_player"),
                target_channel_id or str(source.get("channelId") or "46"),
                now,
                now,
                None,
                now,
            ),
        )

        prepared = prepare_import_rows(connection, snapshot)
        omit_id_tables = {"items", "souls", "tasks", "lottery_history"}
        for table in ACCOUNT_TABLES:
            omit = {"id"} if table in omit_id_tables else set()
            counts[table] = insert_rows(
                connection, table, prepared[table], target_uid, omit
            )
        connection.commit()
    return {"targetUid": target_uid, "targetUuid": target_uuid, "counts": counts, "dryRun": False}


def restore_snapshot(
    db: Path,
    snapshot_path: Path,
    target_channel_uid: str,
) -> dict[str, Any]:
    """Replace the fixed local account with an imported snapshot.

    Mobile builds already route login to one local channel identity.  The
    regular import command intentionally creates a new identity, which is
    unsuitable here because the client would continue logging into the old
    one.  Keep the identity stable and replace only its account-scoped rows
    in one transaction.
    """
    snapshot = load_snapshot(snapshot_path)
    target_channel_uid = target_channel_uid.strip()
    if not target_channel_uid or target_channel_uid.startswith("__"):
        raise ValueError("target channel UID must be a fixed non-empty local identifier")
    target_uid, target_uuid = target_identity(target_channel_uid)
    source = snapshot.get("source") or {}
    now = int(time.time())
    with closing(open_write(db)) as connection:
        require_tables(connection, ("accounts",) + ACCOUNT_TABLES)
        connection.execute("BEGIN IMMEDIATE")
        account = connection.execute(
            "SELECT uid FROM accounts WHERE channel_uid=? OR uid=? OR uuid=? LIMIT 1",
            (target_channel_uid, target_uid, target_uuid),
        ).fetchone()
        if account is None:
            connection.execute(
                """
                INSERT INTO accounts(
                    channel_uid,uid,uuid,username,channel_id,created_at,
                    last_http_login_at,last_tcp_login_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    target_channel_uid,
                    target_uid,
                    target_uuid,
                    str(source.get("username") or "local_player"),
                    str(source.get("channelId") or "46"),
                    now,
                    now,
                    None,
                    now,
                ),
            )
        elif str(account["uid"]) != target_uid:
            raise ValueError("target local identity has inconsistent uid/uuid mapping")
        else:
            connection.execute(
                """
                UPDATE accounts
                SET username=?, channel_id=?, last_http_login_at=?, last_seen_at=?
                WHERE uid=?
                """,
                (
                    str(source.get("username") or "local_player"),
                    str(source.get("channelId") or "46"),
                    now,
                    now,
                    target_uid,
                ),
            )

        for table in reversed(ACCOUNT_TABLES):
            connection.execute(f'DELETE FROM "{table}" WHERE uid=?', (target_uid,))
        prepared = prepare_import_rows(connection, snapshot)
        omit_id_tables = {"items", "souls", "tasks", "lottery_history"}
        counts: dict[str, int] = {}
        for table in ACCOUNT_TABLES:
            omit = {"id"} if table in omit_id_tables else set()
            counts[table] = insert_rows(
                connection, table, prepared[table], target_uid, omit
            )
        connection.commit()
    return {"targetUid": target_uid, "counts": counts, "replaced": True}


def write_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def pack_account_file(snapshot_path: Path, output: Path) -> dict[str, Any]:
    """Validate a redacted snapshot and wrap it for the mobile service APK."""
    snapshot = load_snapshot(snapshot_path)
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    artifact = {
        "kind": ACCOUNT_ARTIFACT_KIND,
        "version": ACCOUNT_ARTIFACT_VERSION,
        "payloadSha256": hashlib.sha256(payload).hexdigest(),
        "payload": snapshot,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.part-{os.getpid()}")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, output)
    return account_summary(snapshot)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export one account as a redacted snapshot")
    export.add_argument("--identity", required=True, help="uid, uuid, channel uid, or configured alias")
    export.add_argument("--output", type=Path, required=True)

    for name in ("inspect", "verify"):
        command = sub.add_parser(name, help=f"{name} a snapshot without changing the database")
        command.add_argument("snapshot", type=Path)

    pack = sub.add_parser("pack", help="wrap a verified snapshot as a mobile-service .soulaccount file")
    pack.add_argument("snapshot", type=Path)
    pack.add_argument("--output", type=Path, required=True)

    import_command = sub.add_parser("import", help="import into a new local identity")
    import_command.add_argument("snapshot", type=Path)
    import_command.add_argument("--target-channel-uid", required=True)
    import_command.add_argument("--target-username")
    import_command.add_argument("--target-channel-id")
    import_command.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            snapshot = export_snapshot(database_path(str(args.db) if args.db else None), args.identity, args.output)
            write_json(account_summary(snapshot))
        elif args.command in ("inspect", "verify"):
            snapshot = load_snapshot(args.snapshot)
            write_json(account_summary(snapshot))
        elif args.command == "pack":
            write_json(pack_account_file(args.snapshot, args.output))
        else:
            result = import_snapshot(
                database_path(str(args.db) if args.db else None),
                args.snapshot,
                args.target_channel_uid,
                args.target_username,
                args.target_channel_id,
                args.dry_run,
            )
            write_json(result)
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"account snapshot failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
