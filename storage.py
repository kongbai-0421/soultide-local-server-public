"""SQLite persistence shared by the local HTTP and TCP servers."""

import ast
import hashlib
import json
import os
import random
import re
import sqlite3
import time
import uuid as uuid_module
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(os.environ.get("SOULTIDE_ROOT", Path(__file__).resolve().parent)).resolve()
DB_PATH = Path(os.environ.get("SOULTIDE_DB_PATH", ROOT / "soultide.db"))
DEFAULT_ROLE_ID = "5056536390159161419"
DEFAULT_ROLE_NAME = "x空白x"
DEFAULT_ROLE_LEVEL = 70

# Public builds do not contain a real account mapping. Deployments may provide
# their own mapping through a private runtime patch or database provisioning
# step; no personal or captured account identity belongs in this repository.
DEFAULT_ACCOUNT_ALIASES = {}


# The client distinguishes numeric attributes (Type=6) from warehouse
# templates.  Older local data contains both rows for some CIDs, so checking
# which table happens to contain a row is not a reliable type decision.
_ITEM_KIND_BY_CID = {}
_ITEM_TYPE_BY_CID = {}
LOCAL_EQUIPMENT_ID_BASE = 6_000_000_000_000_000_000
try:
    _module_config_path = ROOT / "analysis" / "module_config.json"
    _module_config = json.loads(_module_config_path.read_text(encoding="utf-8"))
    for _table_name, _table in (_module_config.get("items") or {}).items():
        if not isinstance(_table, dict):
            continue
        for _raw_cid, _item in _table.items():
            if not isinstance(_item, dict):
                continue
            try:
                _cid = int(_raw_cid)
            except (TypeError, ValueError):
                continue
            _item_type = int(_item.get("Type", 0) or 0)
            _ITEM_TYPE_BY_CID[_cid] = _item_type
            _ITEM_KIND_BY_CID[_cid] = "attr" if _item_type == 6 else "item"
except (OSError, TypeError, ValueError):
    _ITEM_KIND_BY_CID = {}
    _ITEM_TYPE_BY_CID = {}

EMPTY_LOTTERY_TIER_CONFIG = {"weightScale": 1000, "shows": {}, "upGroups": {}, "poolShows": {}}
LOTTERY_TIER_CONFIG_PATH = Path(
    os.environ.get(
        "SOULTIDE_LOTTERY_TIER_CONFIG_PATH",
        ROOT / "analysis" / "lottery_tier_config_5392.json",
    )
)
LOTTERY_TIER_OVERRIDES_PATH = Path(
    os.environ.get(
        "SOULTIDE_LOTTERY_TIER_OVERRIDES_PATH",
        ROOT / "analysis" / "lottery_runtime_overrides_5392.json",
    )
)


def _positive_int_list(value, field_name):
    if not isinstance(value, list) or not value:
        raise ValueError(f"lottery override {field_name} must be a non-empty list")
    result = []
    for raw_value in value:
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f"lottery override {field_name} contains a non-integer")
        if parsed <= 0:
            raise ValueError(f"lottery override {field_name} contains a non-positive value")
        result.append(parsed)
    return result


def apply_lottery_runtime_overrides(config, overrides):
    """Apply local test UP tiers without altering the official snapshots.

    ``upList`` contains only the selected rewards for client display, while
    ``items``/``weights`` retain the official non-UP SSR tail when requested.
    """
    if not isinstance(config, dict) or not isinstance(overrides, dict):
        return config
    for raw_show_id, override in (overrides.get("showUpTiers") or {}).items():
        if not isinstance(override, dict):
            raise ValueError(f"lottery override for show {raw_show_id} is malformed")
        show_id = str(int(raw_show_id))
        show = (config.get("shows") or {}).get(show_id)
        if not isinstance(show, dict):
            raise ValueError(f"lottery override references unknown show {show_id}")
        items = _positive_int_list(override.get("items"), f"{show_id}.items")
        weights = _positive_int_list(override.get("weights"), f"{show_id}.weights")
        if len(items) != len(weights):
            raise ValueError(f"lottery override {show_id} items and weights differ")
        preserve_tail = override.get("preserveOfficialTail", False)
        if not isinstance(preserve_tail, bool):
            raise ValueError(f"lottery override {show_id}.preserveOfficialTail must be boolean")
        raw_group_ids = override.get("groups", show.get("upGroups") or [])
        if not isinstance(raw_group_ids, list) or not raw_group_ids:
            raise ValueError(f"lottery override {show_id}.groups must be a non-empty list")
        show_group_ids = {int(group_id) for group_id in show.get("upGroups") or []}
        for raw_group_id in raw_group_ids:
            group_id = str(int(raw_group_id))
            if int(group_id) not in show_group_ids:
                raise ValueError(f"lottery override group {group_id} is outside show {show_id}")
            group = (config.get("upGroups") or {}).get(group_id)
            if not isinstance(group, dict):
                raise ValueError(f"lottery override references unknown UP group {group_id}")
            rows = group.get("rows") or {}
            if not rows:
                raise ValueError(f"lottery UP group {group_id} has no rows")
            for row in rows.values():
                if not isinstance(row, dict):
                    raise ValueError(f"lottery UP group {group_id} has a malformed row")
                original_items = row.get("items", row.get("upList", []))
                original_weights = row.get("weights", [])
                if len(original_items) != len(original_weights):
                    raise ValueError(f"lottery UP group {group_id} has mismatched source weights")
                if preserve_tail:
                    selected = set(items)
                    tail = [
                        (int(cid), int(weight))
                        for index, (cid, weight) in enumerate(
                            zip(original_items, original_weights), start=1
                        )
                        if index > len(items) and int(cid) not in selected
                    ]
                    effective_items = list(items) + [cid for cid, _weight in tail]
                    effective_weights = list(weights) + [weight for _cid, weight in tail]
                else:
                    effective_items = list(items)
                    effective_weights = list(weights)
                # Keep UpList limited to the selected rewards while retaining
                # the official non-UP tail in the effective probability tier.
                row["upList"] = list(items)
                row["items"] = effective_items
                row["weights"] = effective_weights
    return config


def load_lottery_tier_config(config_path=None, overrides_path=None):
    """Load official lottery tiers plus an optional local test override file."""
    path = Path(config_path or LOTTERY_TIER_CONFIG_PATH)
    config = json.loads(path.read_text(encoding="utf-8"))
    override_path = Path(overrides_path or LOTTERY_TIER_OVERRIDES_PATH)
    if override_path.exists():
        overrides = json.loads(override_path.read_text(encoding="utf-8"))
        apply_lottery_runtime_overrides(config, overrides)
    return config


try:
    LOTTERY_TIER_CONFIG = load_lottery_tier_config()
except (OSError, TypeError, ValueError, json.JSONDecodeError):
    LOTTERY_TIER_CONFIG = dict(EMPTY_LOTTERY_TIER_CONFIG)


def _resource_kind(connection, uid, cid):
    """Return the authoritative storage kind for an item CID."""
    kind = _ITEM_KIND_BY_CID.get(int(cid))
    if kind in ("attr", "item"):
        return kind
    if connection.execute(
        "SELECT 1 FROM player_num_attrs WHERE uid=? AND cid=?", (uid, cid)
    ).fetchone() is not None:
        return "attr"
    return "item"


@contextmanager
def connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize():
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                channel_uid TEXT PRIMARY KEY,
                uid TEXT NOT NULL UNIQUE,
                uuid TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_http_login_at INTEGER NOT NULL,
                last_tcp_login_at INTEGER,
                last_seen_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                uid TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                role_name TEXT NOT NULL,
                level INTEGER NOT NULL,
                snapshot_mode TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                uuid TEXT NOT NULL,
                remote_addr TEXT NOT NULL,
                connected_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                disconnected_at INTEGER,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(uuid)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_aliases (
                alias_channel_uid TEXT PRIMARY KEY,
                target_uid TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(target_uid) REFERENCES accounts(uid)
            )
            """
        )
        now = int(time.time())
        for alias_channel_uid, target_uid in DEFAULT_ACCOUNT_ALIASES.items():
            if connection.execute(
                "SELECT 1 FROM accounts WHERE uid=?", (target_uid,)
            ).fetchone() is not None:
                connection.execute(
                    """
                    INSERT INTO account_aliases(alias_channel_uid,target_uid,created_at)
                    VALUES(?,?,?)
                    ON CONFLICT(alias_channel_uid) DO UPDATE SET
                        target_uid=excluded.target_uid
                    """,
                    (alias_channel_uid, target_uid, now),
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guilds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                level INTEGER NOT NULL DEFAULT 1,
                fund INTEGER NOT NULL DEFAULT 0,
                notice TEXT NOT NULL DEFAULT '',
                policy INTEGER NOT NULL DEFAULT 0,
                audit_type INTEGER NOT NULL DEFAULT 0,
                head_icon INTEGER NOT NULL DEFAULT 0,
                avatar_frame INTEGER NOT NULL DEFAULT 0,
                impeachment_time INTEGER NOT NULL DEFAULT 0,
                quest_progress_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_members (
                guild_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                active_num INTEGER NOT NULL DEFAULT 0,
                join_time INTEGER NOT NULL,
                last_login_time INTEGER NOT NULL,
                PRIMARY KEY(guild_id, uid),
                FOREIGN KEY(guild_id) REFERENCES guilds(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_applications (
                guild_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(guild_id, uid),
                FOREIGN KEY(guild_id) REFERENCES guilds(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_buildings (
                guild_id INTEGER NOT NULL,
                cid INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                buy_effect_exp_time INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(guild_id, cid),
                FOREIGN KEY(guild_id) REFERENCES guilds(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_members_uid ON guild_members(uid)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_guild_applications_guild ON guild_applications(guild_id)"
        )
        guild_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(guilds)").fetchall()
        }
        if "impeachment_time" not in guild_columns:
            connection.execute(
                "ALTER TABLE guilds ADD COLUMN impeachment_time INTEGER NOT NULL DEFAULT 0"
            )
        player_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(players)").fetchall()
        }
        if "current_dress_cid" not in player_columns:
            connection.execute(
                "ALTER TABLE players ADD COLUMN current_dress_cid INTEGER"
            )
        player_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(players)").fetchall()
        }
        for name, definition in (
            ("remainder_give_gift_num", "INTEGER"),
            ("give_gift_daily_max", "INTEGER"),
            ("companion_reset_day", "TEXT"),
            ("fondle_num", "INTEGER"),
            ("next_recovery_fondle_time", "INTEGER"),
            ("current_show_soul_cid", "INTEGER"),
        ):
            if name not in player_columns:
                connection.execute(f"ALTER TABLE players ADD COLUMN {name} {definition}")


def _guild_decode(row):
    if row is None:
        return None
    value = dict(row)
    try:
        value["quest_progress"] = json.loads(value.pop("quest_progress_json") or "{}")
    except (TypeError, ValueError):
        value["quest_progress"] = {}
    return value


def guild_get(guild_id):
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return None
    if guild_id <= 0:
        return None
    with connect() as connection:
        return _guild_decode(connection.execute(
            "SELECT * FROM guilds WHERE id=?", (guild_id,)
        ).fetchone())


def guild_for_uid(uid):
    if not uid:
        return None
    with connect() as connection:
        row = connection.execute(
            """
            SELECT g.*, m.position, m.active_num, m.join_time, m.last_login_time
            FROM guild_members m JOIN guilds g ON g.id=m.guild_id
            WHERE m.uid=?
            """, (str(uid),)
        ).fetchone()
        return _guild_decode(row)


def guild_create(uid, name):
    name = str(name or "").strip()
    if not uid or not 1 <= len(name) <= 32:
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone() is not None:
            return None
        try:
            connection.execute(
                "INSERT INTO guilds(name,created_at,updated_at) VALUES(?,?,?)",
                (name, now, now),
            )
        except sqlite3.IntegrityError:
            return None
        guild_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO guild_members(guild_id,uid,position,join_time,last_login_time) VALUES(?,?,?,?,?)",
            (guild_id, str(uid), 1, now, now),
        )
        row = connection.execute("SELECT * FROM guilds WHERE id=?", (guild_id,)).fetchone()
        return _guild_decode(row)


def guild_list(query="", limit=50):
    query = str(query or "").strip()
    limit = max(1, min(int(limit), 100))
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT g.*, COUNT(m.uid) AS member_num,
                   COALESCE(MAX(CASE WHEN m.position=1 THEN p.role_name END), '') AS leader_name
            FROM guilds g
            LEFT JOIN guild_members m ON m.guild_id=g.id
            LEFT JOIN players p ON p.uid=m.uid
            WHERE (?='' OR g.name LIKE ? COLLATE NOCASE)
            GROUP BY g.id
            ORDER BY g.id
            LIMIT ?
            """, (query, "%" + query + "%", limit),
        ).fetchall()
    return [dict(row) for row in rows]


def guild_members(guild_id):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT m.*, p.role_name, p.level
            FROM guild_members m LEFT JOIN players p ON p.uid=m.uid
            WHERE m.guild_id=? ORDER BY m.position DESC, m.join_time, m.uid
            """, (int(guild_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def guild_applications(guild_id):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT a.uid, a.created_at, p.role_name, p.level
            FROM guild_applications a LEFT JOIN players p ON p.uid=a.uid
            WHERE a.guild_id=? ORDER BY a.created_at, a.uid
            """, (int(guild_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def guild_applications_for_uid(uid):
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT g.*, a.created_at AS application_created_at,
                   COUNT(m.uid) AS member_num,
                   COALESCE(MAX(CASE WHEN m.position=1 THEN p.role_name END), '') AS leader_name
            FROM guild_applications a
            JOIN guilds g ON g.id=a.guild_id
            LEFT JOIN guild_members m ON m.guild_id=g.id
            LEFT JOIN players p ON p.uid=m.uid
            WHERE a.uid=?
            GROUP BY g.id
            ORDER BY a.created_at
            """, (str(uid),),
        ).fetchall()
    return [dict(row) for row in rows]


def guild_apply(uid, guild_id):
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return False
    if not uid or guild_id <= 0:
        return False
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM guilds WHERE id=?", (guild_id,)).fetchone() is None:
            return False
        if connection.execute("SELECT 1 FROM guild_members WHERE uid=?", (str(uid),)).fetchone() is not None:
            return False
        count = connection.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id=?", (guild_id,)
        ).fetchone()[0]
        if int(count) >= 50:
            return False
        connection.execute(
            "INSERT OR IGNORE INTO guild_applications(guild_id,uid,created_at) VALUES(?,?,?)",
            (guild_id, str(uid), int(time.time())),
        )
    return True


def guild_cancel_apply(uid, guild_id):
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM guild_applications WHERE guild_id=? AND uid=?",
            (int(guild_id), str(uid)),
        )
    return cursor.rowcount > 0


def _guild_is_leader(connection, uid, guild_id):
    row = connection.execute(
        "SELECT position FROM guild_members WHERE guild_id=? AND uid=?",
        (int(guild_id), str(uid)),
    ).fetchone()
    return row is not None and int(row["position"]) == 1


def guild_accept(uid, member_uid):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return False
        guild_id = int(row["guild_id"])
        if connection.execute(
            "SELECT 1 FROM guild_applications WHERE guild_id=? AND uid=?",
            (guild_id, str(member_uid)),
        ).fetchone() is None:
            return False
        if connection.execute(
            "SELECT 1 FROM guild_members WHERE uid=?", (str(member_uid),)
        ).fetchone() is not None:
            connection.execute(
                "DELETE FROM guild_applications WHERE guild_id=? AND uid=?",
                (guild_id, str(member_uid)),
            )
            return False
        count = connection.execute(
            "SELECT COUNT(*) FROM guild_members WHERE guild_id=?", (guild_id,)
        ).fetchone()[0]
        if int(count) >= 50:
            return False
        now = int(time.time())
        connection.execute(
            "INSERT INTO guild_members(guild_id,uid,position,join_time,last_login_time) VALUES(?,?,?,?,?)",
            (guild_id, str(member_uid), 0, now, now),
        )
        connection.execute(
            "DELETE FROM guild_applications WHERE guild_id=? AND uid=?",
            (guild_id, str(member_uid)),
        )
    return True


def guild_refuse(uid, member_uids):
    values = [str(value) for value in (member_uids or [])]
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return False
        for member_uid in values:
            connection.execute(
                "DELETE FROM guild_applications WHERE guild_id=? AND uid=?",
                (int(row["guild_id"]), member_uid),
            )
    return True


def guild_remove(uid, member_uid):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return False
        if str(member_uid) == str(uid):
            return False
        cursor = connection.execute(
            "DELETE FROM guild_members WHERE guild_id=? AND uid=?",
            (int(row["guild_id"]), str(member_uid)),
        )
    return cursor.rowcount > 0


def guild_set_position(uid, member_uid, position):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return False
        guild_id = int(row["guild_id"])
        if connection.execute(
            "SELECT 1 FROM guild_members WHERE guild_id=? AND uid=?",
            (guild_id, str(member_uid)),
        ).fetchone() is None:
            return False
        position = max(0, min(2, int(position)))
        if position == 1 and str(member_uid) != str(uid):
            connection.execute(
                "UPDATE guild_members SET position=0 WHERE guild_id=? AND uid=?",
                (guild_id, str(uid)),
            )
            connection.execute(
                "UPDATE guild_members SET position=1 WHERE guild_id=? AND uid=?",
                (guild_id, str(member_uid)),
            )
        elif str(member_uid) != str(uid):
            connection.execute(
                "UPDATE guild_members SET position=? WHERE guild_id=? AND uid=?",
                (position, guild_id, str(member_uid)),
            )
    return True


def guild_leave(uid):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id,position FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None:
            return False
        guild_id = int(row["guild_id"])
        connection.execute(
            "DELETE FROM guild_members WHERE guild_id=? AND uid=?", (guild_id, str(uid))
        )
        if int(row["position"]) == 1:
            successor = connection.execute(
                "SELECT uid FROM guild_members WHERE guild_id=? ORDER BY join_time,uid LIMIT 1",
                (guild_id,),
            ).fetchone()
            if successor is None:
                connection.execute("DELETE FROM guilds WHERE id=?", (guild_id,))
            else:
                connection.execute(
                    "UPDATE guild_members SET position=1 WHERE guild_id=? AND uid=?",
                    (guild_id, successor["uid"]),
                )
    return True


def guild_impeachment(uid, cancel=False):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None:
            return None
        value = 0 if cancel else int(time.time())
        connection.execute(
            "UPDATE guilds SET impeachment_time=?,updated_at=? WHERE id=?",
            (value, int(time.time()), int(row["guild_id"])),
        )
    return value


def guild_update(uid, *, name=None, policy=None, audit_type=None, head_icon=None, avatar_frame=None, notice=None):
    allowed = {
        "name": name, "policy": policy, "audit_type": audit_type,
        "head_icon": head_icon, "avatar_frame": avatar_frame, "notice": notice,
    }
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return False
        guild_id = int(row["guild_id"])
        updates, values = [], []
        for column, value in allowed.items():
            if value is None:
                continue
            if column == "name":
                value = str(value).strip()
                if not 1 <= len(value) <= 32:
                    return False
            updates.append("%s=?" % column)
            values.append(value)
        if updates:
            updates.append("updated_at=?")
            values.extend([int(time.time()), guild_id])
            try:
                connection.execute(
                    "UPDATE guilds SET %s WHERE id=?" % ",".join(updates), values
                )
            except sqlite3.IntegrityError:
                return False
    return True


def guild_building_update(uid, building_id, effect=False):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None or not _guild_is_leader(connection, uid, row["guild_id"]):
            return None
        guild_id = int(row["guild_id"])
        current = connection.execute(
            "SELECT * FROM guild_buildings WHERE guild_id=? AND cid=?",
            (guild_id, int(building_id)),
        ).fetchone()
        now = int(time.time())
        level = int(current["level"]) if current else 1
        effect_time = int(current["buy_effect_exp_time"]) if current else 0
        if effect:
            effect_time = now + 86400
        else:
            level += 1
        connection.execute(
            """
            INSERT INTO guild_buildings(guild_id,cid,level,buy_effect_exp_time)
            VALUES(?,?,?,?)
            ON CONFLICT(guild_id,cid) DO UPDATE SET
                level=excluded.level, buy_effect_exp_time=excluded.buy_effect_exp_time
            """, (guild_id, int(building_id), level, effect_time),
        )
    return {"cid": int(building_id), "lv": level, "buyEffectExpTime": effect_time}


def guild_buildings(guild_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT cid,level,buy_effect_exp_time FROM guild_buildings WHERE guild_id=? ORDER BY cid",
            (int(guild_id),),
        ).fetchall()
    return [
        {"cid": int(row["cid"]), "lv": int(row["level"]), "buyEffectExpTime": int(row["buy_effect_exp_time"])}
        for row in rows
    ]


def guild_set_quest_progress(uid, progress):
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guild_id FROM guild_members WHERE uid=?", (str(uid),)
        ).fetchone()
        if row is None:
            return None
        guild_id = int(row["guild_id"])
        current = connection.execute(
            "SELECT quest_progress_json FROM guilds WHERE id=?", (guild_id,)
        ).fetchone()
        if current is None:
            return None
        try:
            old = json.loads(current["quest_progress_json"] or "{}")
        except (TypeError, ValueError):
            old = {}
        for key, value in (progress or {}).items():
            old[str(key)] = max(int(old.get(str(key), 0)), int(value))
        connection.execute(
            "UPDATE guilds SET quest_progress_json=?,updated_at=? WHERE id=?",
            (json.dumps(old, separators=(",", ":")), int(time.time()), guild_id),
        )
        return {int(key): int(value) for key, value in old.items()}


def _account_values(channel_uid):
    uid = hashlib.md5(channel_uid.encode("utf-8", errors="replace")).hexdigest()
    stable_uuid = str(
        uuid_module.uuid5(uuid_module.NAMESPACE_DNS, "soultide:" + channel_uid)
    )
    return uid, stable_uuid


def get_or_create_account(channel_uid, username="local_player", channel_id="46"):
    channel_uid = str(channel_uid)
    now = int(time.time())
    with connect() as connection:
        alias_row = connection.execute(
            """
            SELECT a.*
            FROM account_aliases aa
            JOIN accounts a ON a.uid=aa.target_uid
            WHERE aa.alias_channel_uid=?
            """,
            (channel_uid,),
        ).fetchone()
        if alias_row is None:
            configured_target_uid = DEFAULT_ACCOUNT_ALIASES.get(channel_uid)
            if configured_target_uid:
                alias_row = connection.execute(
                    "SELECT * FROM accounts WHERE uid=?", (configured_target_uid,)
                ).fetchone()
                if alias_row is not None:
                    connection.execute(
                        """
                        INSERT INTO account_aliases(alias_channel_uid,target_uid,created_at)
                        VALUES(?,?,?)
                        ON CONFLICT(alias_channel_uid) DO UPDATE SET
                            target_uid=excluded.target_uid
                        """,
                        (channel_uid, configured_target_uid, now),
                    )
        if alias_row is not None:
            uid = alias_row["uid"]
            connection.execute(
                """
                UPDATE accounts
                SET last_http_login_at=?,last_seen_at=?
                WHERE uid=?
                """,
                (now, now, uid),
            )
            row = connection.execute(
                "SELECT * FROM accounts WHERE uid=?", (uid,)
            ).fetchone()
        else:
            uid, stable_uuid = _account_values(channel_uid)
            connection.execute(
                """
                INSERT INTO accounts (
                    channel_uid, uid, uuid, username, channel_id,
                    created_at, last_http_login_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_uid) DO UPDATE SET
                    username = excluded.username,
                    channel_id = excluded.channel_id,
                    last_http_login_at = excluded.last_http_login_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    channel_uid,
                    uid,
                    stable_uuid,
                    username,
                    str(channel_id),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO players (
                    uid, role_id, role_name, level, snapshot_mode, updated_at
                ) VALUES (?, ?, ?, ?, 'local', ?)
                ON CONFLICT(uid) DO NOTHING
                """,
                (uid, DEFAULT_ROLE_ID, DEFAULT_ROLE_NAME, DEFAULT_ROLE_LEVEL, now),
            )
            row = connection.execute(
                "SELECT * FROM accounts WHERE channel_uid = ?", (channel_uid,)
            ).fetchone()
    ensure_default_souls(uid)
    ensure_default_dress(uid)
    ensure_default_tasks(uid)
    return dict(row)


def get_player(uid):
    if not uid:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM players WHERE uid = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def set_player_role(uid, role_id, role_name):
    """Persist the role identity returned by the local create-role flow."""
    if not uid or not role_id or not isinstance(role_name, str) or not role_name.strip():
        return False
    now = int(time.time())
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE players SET role_id=?,role_name=?,updated_at=? WHERE uid=?",
            (str(role_id), role_name.strip(), now, uid),
        )
    return cursor.rowcount == 1


def get_account_by_identity(identity):
    """Resolve a local account by stable UUID, player UID, role ID or channel alias."""
    if not identity:
        return None
    value = str(identity)
    with connect() as connection:
        # A preserved historical account may still own the raw channel_uid.
        # The explicit alias is the compatibility authority and must win.
        alias = connection.execute(
            """
            SELECT a.* FROM account_aliases aa
            JOIN accounts a ON a.uid=aa.target_uid
            WHERE aa.alias_channel_uid=? LIMIT 1
            """,
            (value,),
        ).fetchone()
        if alias is not None:
            return dict(alias)
        direct = connection.execute(
            "SELECT * FROM accounts WHERE uuid=? OR uid=? OR channel_uid=? LIMIT 1",
            (value, value, value),
        ).fetchone()
        if direct is not None:
            return dict(direct)
        # Role IDs are client-visible identities but older test/local accounts
        # may share defaults. Resolve them only when the match is unambiguous.
        role_rows = connection.execute(
            """
            SELECT a.* FROM players p
            JOIN accounts a ON a.uid=p.uid
            WHERE p.role_id=? LIMIT 2
            """,
            (value,),
        ).fetchall()
    return dict(role_rows[0]) if len(role_rows) == 1 else None


def get_configured_alias_account():
    """Return the one existing account targeted by configured channel aliases.

    Recharge requests from this client build do not always carry an account
    identity. Falling back is safe only when all configured aliases resolve to
    one existing UID; an ambiguous or missing target fails closed.
    """
    target_uids = {str(uid) for uid in DEFAULT_ACCOUNT_ALIASES.values() if uid}
    if len(target_uids) != 1:
        return None
    target_uid = next(iter(target_uids))
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM accounts WHERE uid=?",
            (target_uid,),
        ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def list_players():
    """Return every local player visible to the offline center service."""
    with connect() as connection:
        rows = connection.execute(
            "SELECT p.*, a.channel_uid FROM players p "
            "LEFT JOIN accounts a ON a.uid=p.uid ORDER BY p.uid"
        ).fetchall()
    return [dict(row) for row in rows]


def find_players(query):
    """Search the local account directory by uid, role id, or role name."""
    if query is None:
        return []
    needle = str(query).strip()
    if not needle:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT p.*, a.channel_uid FROM players p "
            "LEFT JOIN accounts a ON a.uid=p.uid "
            "WHERE p.uid=? OR p.role_id=? OR p.role_name LIKE ? OR a.channel_uid=? "
            "ORDER BY p.updated_at DESC LIMIT 20",
            (needle, needle, "%" + needle + "%", needle),
        ).fetchall()
    return [dict(row) for row in rows]


def set_snapshot_mode(uid, snapshot_mode):
    if not uid:
        return
    now = int(time.time())
    with connect() as connection:
        connection.execute(
            """
            UPDATE players
            SET snapshot_mode = ?, updated_at = ?
            WHERE uid = ?
            """,
            (snapshot_mode, now, uid),
        )


def set_current_dress(uid, dress_cid):
    if not uid or not 0 < dress_cid <= 0xFFFFFFFF:
        return False
    owned = get_player_state_json(uid, "dresses")
    if owned is None:
        ensure_default_dress(uid)
        owned = get_player_state_json(uid, "dresses")
    if not isinstance(owned, list) or not any(
        isinstance(item, dict) and int(item.get("dressCid", 0) or 0) == int(dress_cid)
        for item in owned
    ):
        return False
    now = int(time.time())
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE players
            SET current_dress_cid = ?, updated_at = ?
            WHERE uid = ?
            """,
            (dress_cid, now, uid),
        )
        return cursor.rowcount == 1


def ensure_default_dress(uid):
    """Seed the client-defined initial outfit for newly created accounts."""
    if not uid or get_player(uid) is None or get_player_state_json(uid, "dresses") is not None:
        return
    update_player_state_json(uid, "dresses", [
        {"dressCid": 33000110, "expireTime": 0, "isNew": True}
    ])


def ensure_owned_dresses(uid, dress_cids):
    """Merge permanent dress ownership without changing the worn dress."""
    if not uid:
        return 0
    current = get_player_state_json(uid, "dresses")
    if not isinstance(current, list):
        current = []
    owned = {
        int(row.get("dressCid", 0) or 0)
        for row in current
        if isinstance(row, dict)
    }
    added = 0
    for raw_cid in dress_cids or []:
        cid = int(raw_cid or 0)
        if cid <= 0 or cid in owned:
            continue
        current.append({"dressCid": cid, "expireTime": 0, "isNew": True})
        owned.add(cid)
        added += 1
    if added:
        update_player_state_json(uid, "dresses", current)
    return added


def _ensure_story_progress_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS soul_story_progress (
                uid TEXT NOT NULL,
                story_cid INTEGER NOT NULL,
                highest_chapter_index INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(uid, story_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        reward_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(companion_reward_log)")
        }
        if "result_json" not in reward_columns:
            connection.execute(
                "ALTER TABLE companion_reward_log ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"
            )


def record_story_chapter(uid, story_cid, chapter_index):
    if not uid or not 0 < story_cid <= 0xFFFFFFFF:
        return False
    if not 0 <= chapter_index <= 0xFFFFFFFF:
        return False
    now = int(time.time())
    with connect() as connection:
        player_exists = connection.execute(
            "SELECT 1 FROM players WHERE uid = ?", (uid,)
        ).fetchone()
        if player_exists is None:
            return False
        connection.execute(
            """
            INSERT INTO soul_story_progress (
                uid, story_cid, highest_chapter_index, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(uid, story_cid) DO UPDATE SET
                highest_chapter_index = MAX(
                    soul_story_progress.highest_chapter_index,
                    excluded.highest_chapter_index
                ),
                updated_at = excluded.updated_at
            """,
            (uid, story_cid, chapter_index, now),
        )
    return True


def get_story_progress(uid, story_cid=None):
    if not uid:
        return []
    query = (
        "SELECT story_cid, highest_chapter_index, updated_at "
        "FROM soul_story_progress WHERE uid = ?"
    )
    params = [uid]
    if story_cid is not None:
        query += " AND story_cid = ?"
        params.append(story_cid)
    query += " ORDER BY story_cid"
    with connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def record_tcp_login(stable_uuid, remote_addr):
    now = int(time.time())
    with connect() as connection:
        account = connection.execute(
            "SELECT * FROM accounts WHERE uuid = ?", (stable_uuid,)
        ).fetchone()
        uid = account["uid"] if account else None
        connection.execute(
            """
            INSERT INTO sessions (
                uid, uuid, remote_addr, connected_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (uid, stable_uuid, remote_addr, now, now),
        )
        session_id = connection.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        if uid:
            connection.execute(
                """
                UPDATE accounts
                SET last_tcp_login_at = ?, last_seen_at = ?
                WHERE uid = ?
                """,
                (now, now, uid),
            )
    if uid:
        social = get_player_state_json(uid, "social") or {}
        social["directoryVisible"] = True
        update_player_state_json(uid, "social", social)
    return (dict(account) if account else None), session_id


def touch_session(session_id, uid=None):
    if not session_id:
        return
    now = int(time.time())
    with connect() as connection:
        connection.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
            (now, session_id),
        )
        if uid:
            connection.execute(
                "UPDATE accounts SET last_seen_at = ? WHERE uid = ?",
                (now, uid),
            )


def close_session(session_id):
    if not session_id:
        return
    now = int(time.time())
    with connect() as connection:
        connection.execute(
            """
            UPDATE sessions
            SET last_seen_at = ?, disconnected_at = ?
            WHERE id = ?
            """,
            (now, now, session_id),
        )


# ── Currency persistence ──
# Field-ID mapping to known currencies in 3910 body.
# Key = protocol field ID (0xXX after 51), Value = display name + capture default.
# Only the first occurrence of each field in the base-info region is overlaid.

DEFAULT_GOLD = 1000
DEFAULT_SOULS = 0
DEFAULT_STAMINA = 120

CURRENCY_FIELDS = {
    0x09: ("gold", 1000),   # field ID → (name, pcap default for pattern matching)
    0x15: ("souls", 2912),
}


def _ensure_currencies_table():
    """Create the currencies table if it does not exist (called on import)."""
    with connect() as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS currencies (
                uid TEXT PRIMARY KEY,
                gold INTEGER NOT NULL DEFAULT {DEFAULT_GOLD},
                souls INTEGER NOT NULL DEFAULT {DEFAULT_SOULS},
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def get_currencies(uid):
    """Return {gold: N, souls: N} or defaults if no row exists."""
    if not uid:
        return {"gold": DEFAULT_GOLD, "souls": DEFAULT_SOULS}
    with connect() as connection:
        row = connection.execute(
            "SELECT gold, souls FROM currencies WHERE uid = ?", (uid,)
        ).fetchone()
        if row:
            return {"gold": row["gold"], "souls": row["souls"]}

        # Compatibility sessions have no account row and must remain read-only.
        now = int(time.time())
        connection.execute(
            """
            INSERT OR IGNORE INTO currencies (uid, gold, souls, updated_at)
            SELECT ?, ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM accounts WHERE uid = ?)
            """,
            (uid, DEFAULT_GOLD, DEFAULT_SOULS, now, uid),
        )
    return {"gold": DEFAULT_GOLD, "souls": DEFAULT_SOULS}


def set_currencies(uid, gold=None, souls=None):
    """Update one or both currency values. None = unchanged."""
    if not uid:
        return
    now = int(time.time())
    current = get_currencies(uid)
    new_gold = gold if gold is not None else current["gold"]
    new_souls = souls if souls is not None else current["souls"]
    with connect() as connection:
        connection.execute(
            "INSERT INTO currencies (uid, gold, souls, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET "
            "gold = excluded.gold, souls = excluded.souls, updated_at = excluded.updated_at",
            (uid, new_gold, new_souls, now),
        )
        # PlayerPOD.numAttrs is the wire-visible source used by the client;
        # keep the currency table and its money attribute in sync.
        connection.execute(
            "INSERT INTO player_num_attrs(uid,cid,quantity,snapshot_seeded,updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(uid,cid) DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at",
            (uid, 1, int(new_gold), 0, now),
        )


def _ensure_player_num_attrs_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS player_num_attrs (
                uid TEXT NOT NULL,
                cid INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                snapshot_seeded INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(uid, cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def seed_player_num_attrs(uid, num_attrs):
    if not uid or not isinstance(num_attrs, dict):
        return 0
    inserted = 0
    now = int(time.time())
    with connect() as connection:
        for cid, quantity in num_attrs.items():
            if not isinstance(cid, int) or not isinstance(quantity, int):
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO player_num_attrs(uid,cid,quantity,updated_at) VALUES(?,?,?,?)",
                (uid, cid, quantity, now),
            )
            inserted += cursor.rowcount
    return inserted


def get_player_num_attrs(uid):
    if not uid:
        return {}
    with connect() as connection:
        rows = connection.execute(
            "SELECT cid, quantity FROM player_num_attrs WHERE uid=? ORDER BY cid", (uid,)
        ).fetchall()
    return {row["cid"]: row["quantity"] for row in rows}


def _ensure_lottery_tables():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lottery_pool_state (
                uid TEXT NOT NULL,
                show_cid INTEGER NOT NULL,
                left_insure_time INTEGER NOT NULL,
                draw_count INTEGER NOT NULL,
                left_hidden_insure_time INTEGER NOT NULL,
                PRIMARY KEY(uid, show_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lottery_records (
                uid TEXT NOT NULL,
                lottery_cid INTEGER NOT NULL,
                last_draw_time INTEGER NOT NULL,
                PRIMARY KEY(uid, lottery_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lottery_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                lottery_cid INTEGER NOT NULL,
                item_cid INTEGER NOT NULL,
                item_num INTEGER NOT NULL,
                draw_time INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def seed_lottery_state(uid, lottery_shows, lottery_records):
    if not uid:
        return 0
    inserted = 0
    with connect() as connection:
        for pod in lottery_shows or []:
            show_cid = pod.get("showCid")
            if not isinstance(show_cid, int):
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO lottery_pool_state(uid,show_cid,left_insure_time,draw_count,left_hidden_insure_time) "
                "VALUES(?,?,?,?,?)",
                (
                    uid,
                    show_cid,
                    int(pod.get("leftInsureTime", -1)),
                    int(pod.get("drawCount", 0)),
                    int(pod.get("leftHiddenInsureTime", -1)),
                ),
            )
            inserted += cursor.rowcount
        for lottery_cid, last_draw_time in (lottery_records or {}).items():
            if isinstance(lottery_cid, int) and isinstance(last_draw_time, int):
                connection.execute(
                    "INSERT OR IGNORE INTO lottery_records(uid,lottery_cid,last_draw_time) VALUES(?,?,?)",
                    (uid, lottery_cid, last_draw_time),
                )
    return inserted


def get_lottery_state(uid):
    with connect() as connection:
        pools = connection.execute(
            "SELECT show_cid,left_insure_time,draw_count,left_hidden_insure_time "
            "FROM lottery_pool_state WHERE uid=? ORDER BY show_cid",
            (uid,),
        ).fetchall()
        records = connection.execute(
            "SELECT lottery_cid,last_draw_time FROM lottery_records WHERE uid=?",
            (uid,),
        ).fetchall()
    return {
        "lotteryShows": [
            {
                "showCid": row["show_cid"],
                "leftInsureTime": row["left_insure_time"],
                "drawCount": row["draw_count"],
                "leftHiddenInsureTime": row["left_hidden_insure_time"],
            }
            for row in pools
        ],
        "lotteryRecords": {
            row["lottery_cid"]: row["last_draw_time"] for row in records
        },
    }


class LotteryPoolError(ValueError):
    """The requested draw cannot be resolved against the official config."""


def _soul_number_from_template_cid(cid):
    """Return the playable soul number for a 200100xx lottery template."""
    try:
        soul_id = int(cid)
    except (TypeError, ValueError):
        return None
    if soul_id not in PLAYABLE_SOUL_IDS:
        return None
    return soul_id - 20010000


def _soul_reward_item_cid(soul_id):
    number = _soul_number_from_template_cid(soul_id)
    return 10100 + number if number is not None else None


def _soul_duplicate_item_cid(soul_id):
    number = _soul_number_from_template_cid(soul_id)
    return 10200 + number if number is not None else None


def _up_group_for_selection(show, selection, tier_config):
    for group_id in show.get("upGroups", []):
        group = tier_config.get("upGroups", {}).get(str(group_id))
        if group and all(str(row) in group["rows"] for row in selection):
            return group
    return None


def resolve_lottery_pool(show_cid, lottery_cid, up_cid_list, tier_config):
    """Resolve the effective draw distribution before anything is charged.

    The client-supplied ``upCidList`` is only a consistency hint: it must name
    CfgLotteryPackUpTable rows of a group this show actually rotates through.
    Selecting UP replaces the show's top tier with the selected rows' effective
    top-tier entries; the lower tiers are shared by every row in the group.
    ``topItems`` deliberately remains the complete effective SSR top tier. The
    configured 50-draw guarantee is therefore an any-SSR guarantee, not an
    UP-only guarantee; ``upItems`` is informational for the selected UP rows.
    """
    show = tier_config.get("shows", {}).get(str(int(show_cid)))
    if not show:
        raise LotteryPoolError(f"show {show_cid} is not configured")
    if int(lottery_cid) not in show["pools"]:
        raise LotteryPoolError(f"pool {lottery_cid} does not belong to show {show_cid}")

    try:
        selection = [int(row) for row in up_cid_list or []]
    except (TypeError, ValueError):
        raise LotteryPoolError("up selection is not a list of row ids")

    selected_up_items = frozenset()
    if selection:
        choice_num = show["choiceNum"]
        if choice_num <= 0:
            raise LotteryPoolError(f"show {show_cid} does not accept an up selection")
        if len(selection) != choice_num or len(set(selection)) != len(selection):
            raise LotteryPoolError(
                f"show {show_cid} expects {choice_num} distinct up rows"
            )
        group = _up_group_for_selection(show, selection, tier_config)
        if group is None:
            raise LotteryPoolError("up selection is outside this show's rotation")
        items = []
        weights = []
        for row_id in selection:
            row = group["rows"][str(row_id)]
            row_items = row.get("items", row.get("upList", []))
            if len(row_items) != len(row["weights"]):
                raise LotteryPoolError(f"up row {row_id} has mismatched weights")
            items.extend(int(cid) for cid in row_items)
            weights.extend(int(weight) for weight in row["weights"])
        selected_up_items = frozenset(
            int(cid) for row_id in selection
            for cid in group["rows"][str(row_id)].get("upList", [])
        )
        tiers = [{"order": 1, "items": items, "weights": weights}] + group["sharedTiers"]
    else:
        tiers = show["tiers"]

    if not tiers or not tiers[0]["items"]:
        raise LotteryPoolError(f"pool {lottery_cid} has no top tier")
    for tier in tiers:
        if len(tier["items"]) != len(tier["weights"]):
            raise LotteryPoolError(f"pool {lottery_cid} tier {tier['order']} is malformed")
        for cid in tier["items"]:
            # Character lotteries publish soul template CIDs (200100xx),
            # which are not rows in CfgItemTable. They are still valid draw
            # results and must not be rejected as unknown warehouse items.
            if int(cid) not in _ITEM_TYPE_BY_CID and not (
                20010001 <= int(cid) <= 20010055 and int(cid) != 20010026
            ):
                raise LotteryPoolError(f"pool {lottery_cid} references unknown item {cid}")
    if sum(weight for tier in tiers for weight in tier["weights"]) <= 0:
        raise LotteryPoolError(f"pool {lottery_cid} has no positive weight")

    insure_times = show.get("insureTimes") or [0]
    return {
        "tiers": tiers,
        "topItems": frozenset(int(cid) for cid in tiers[0]["items"]),
        "upItems": selected_up_items,
        "insure": max(0, int(insure_times[0])),
    }


def _draw_from_pool(pool, left_insure_time, draw_count, rng):
    """Draw ``draw_count`` template CIDs and return them with the new pity state."""
    flat = [
        (int(cid), int(weight))
        for tier in pool["tiers"]
        for cid, weight in zip(tier["items"], tier["weights"])
        if int(weight) > 0
    ]
    top = [
        (int(cid), int(weight))
        for cid, weight in zip(pool["tiers"][0]["items"], pool["tiers"][0]["weights"])
        if int(weight) > 0
    ]
    insure = pool["insure"]
    left = left_insure_time if 0 < left_insure_time <= insure else insure
    drawn = []
    for _ in range(draw_count):
        # Pity uses the full SSR top tier. Do not narrow this to upItems: the
        # local test pools preserve the official any-SSR guarantee semantics.
        candidates = top if insure and left == 1 and top else flat
        cid = rng.choices(
            [candidate for candidate, _ in candidates],
            weights=[weight for _, weight in candidates],
            k=1,
        )[0]
        if insure:
            left = insure if cid in pool["topItems"] else max(2, left) - 1
        drawn.append(cid)
    return drawn, left


def _legacy_lottery_draws(
    lottery_cid, draw_count, up_cid_list, action, pack_config, drop_config, rng,
):
    """Resolve the pre-5392 test/config shape without affecting production pools.

    Older local tests pass ``packIds`` plus a locally extracted DropId map.  The
    current client build does not use that representation for the main lottery,
    so this compatibility path is deliberately selected only when the caller
    supplies a config without the official ``shows`` table.
    """
    if not isinstance(action, dict) or not isinstance(pack_config, dict):
        raise LotteryPoolError("legacy lottery config is malformed")
    if isinstance(drop_config, dict):
        raw_drops = drop_config.get("drops", drop_config)
    else:
        raw_drops = {}
    if not isinstance(raw_drops, dict):
        raw_drops = {}

    # Historical tests used the old arithmetic mapping for a selected SSR row.
    # Keep it isolated here; the official resolver uses CfgLotteryPackUpTable's
    # explicit UpList and never derives an item CID from a row ID.
    selection = []
    try:
        selection = [int(row) for row in (up_cid_list or [])]
    except (TypeError, ValueError):
        raise LotteryPoolError("legacy up selection is malformed")
    if selection:
        mapped = []
        for row_id in selection:
            item_cid = 44000 + row_id - 110000
            if item_cid <= 0 or _ITEM_TYPE_BY_CID.get(item_cid) != 3:
                raise LotteryPoolError(f"legacy up row {row_id} is not an equipment template")
            mapped.append((item_cid, 1))
        return [list(mapped[index % len(mapped): index % len(mapped) + 1]) for index in range(draw_count)]

    groups = action.get("packIds") or []
    if not isinstance(groups, list) or not groups:
        raise LotteryPoolError(f"lottery {lottery_cid} has no legacy pack groups")
    draws = []
    for draw_index in range(draw_count):
        raw_group = groups[draw_index] if draw_index < len(groups) else groups[-1]
        values = raw_group if isinstance(raw_group, list) else [raw_group]
        candidates = []
        for raw_pack_id in values:
            try:
                pack_id = int(raw_pack_id)
            except (TypeError, ValueError):
                continue
            pack = pack_config.get(str(pack_id), pack_config.get(pack_id))
            if not isinstance(pack, dict):
                continue
            try:
                weight = max(0, int(pack.get("weight", 0) or 0))
                drop_id = int(pack.get("dropId", 0) or 0)
            except (TypeError, ValueError):
                continue
            if drop_id > 0:
                candidates.append((pack_id, weight, drop_id))
        if not candidates:
            raise LotteryPoolError(f"lottery {lottery_cid} has no legacy candidates")
        positive = [candidate for candidate in candidates if candidate[1] > 0]
        if positive:
            selected = rng.choices(
                positive,
                weights=[candidate[1] for candidate in positive],
                k=1,
            )[0]
        else:
            # A single zero-weight row was used by the old deterministic test
            # fixtures to stand for an explicitly selected pack.
            selected = candidates[0]
        raw_reward = raw_drops.get(str(selected[2]), raw_drops.get(selected[2]))
        if not isinstance(raw_reward, list) or not raw_reward:
            raise LotteryPoolError(f"legacy drop {selected[2]} is not configured")
        pairs = []
        for item in raw_reward:
            if not isinstance(item, dict):
                raise LotteryPoolError(f"legacy drop {selected[2]} is malformed")
            try:
                cid = int(item.get("cid", 0))
                quantity = int(item.get("num", 0))
            except (TypeError, ValueError):
                raise LotteryPoolError(f"legacy drop {selected[2]} is malformed")
            if cid <= 0 or quantity <= 0:
                raise LotteryPoolError(f"legacy drop {selected[2]} has an invalid item")
            pairs.append((cid, quantity))
        # The old 20003 fixture represents an equipment lottery. Do not let a
        # stale material DropId turn it into a successful non-equipment draw.
        if int(lottery_cid) == 2000302 and any(
            _ITEM_TYPE_BY_CID.get(cid) != 3 for cid, _quantity in pairs
        ):
            raise LotteryPoolError("legacy equipment lottery resolved a non-equipment item")
        draws.append(pairs)
    return draws


def _equipment_item_pod(row):
    equipped_to = row.get("equipped_to")
    equipped_slot = int(row.get("equipped_slot", 0) or 0)
    soul_prefab_ids = {}
    if equipped_to is not None and equipped_slot > 0:
        soul_prefab_ids[int(equipped_to)] = equipped_slot
    return {
        "id": int(row["id"]),
        "cid": int(row["template_id"]),
        "num": 1,
        "usedNum": 0,
        "createTime": int(row.get("created_at", 0) or 0),
        "equipmentData": {
            "lv": max(1, int(row.get("level", 1) or 1)),
            "exp": max(0, int(row.get("exp", 0) or 0)),
            "soulPrefabIds": soul_prefab_ids,
            "lock": bool(row.get("locked", 0)),
            "star": max(1, int(row.get("star", 1) or 1)),
            "upCostGold": 0,
        },
    }


def _next_equipment_instance_id_connection(connection):
    row = connection.execute(
        "SELECT MAX(id) AS max_id FROM equipment_instances WHERE id>=?",
        (LOCAL_EQUIPMENT_ID_BASE,),
    ).fetchone()
    return max(LOCAL_EQUIPMENT_ID_BASE, int(row["max_id"] or 0) + 1)


def _insert_equipment_instances_connection(connection, uid, reward_list, now):
    inserted = []
    next_id = _next_equipment_instance_id_connection(connection)
    for cid, quantity in reward_list:
        if _ITEM_TYPE_BY_CID.get(int(cid)) != 3:
            continue
        for _ in range(int(quantity)):
            instance_id = next_id
            next_id += 1
            connection.execute(
                "INSERT INTO equipment_instances(id,uid,template_id,level,star,created_at) VALUES(?,?,?,1,1,?)",
                (instance_id, uid, int(cid), now),
            )
            inserted.append(_equipment_item_pod({
                "id": instance_id,
                "template_id": int(cid),
                "level": 1,
                "star": 1,
                "exp": 0,
                "locked": 0,
                "equipped_to": None,
                "equipped_slot": 0,
                "created_at": now,
            }))
    return inserted


def perform_lottery_draw(
    uid, show_cid, lottery_cid, up_cid_list, lottery_actions, tier_config, drop_config, rng=random,
):
    """Execute one validated lottery transaction.

    The pool is resolved from the official per-tier probabilities before the
    cost is charged, so an unresolvable show, pool or UP selection is refused
    outright instead of silently falling back to a default pack. Costs, fixed
    rewards, drawn items, equipment instances, history and pity state are
    committed or rolled back together.
    """
    if not uid:
        return None
    action = lottery_actions.get(str(lottery_cid))
    if not action:
        return None
    draw_count = 10 if int(action.get("lotteryMode", 0)) == 2 else 1

    official_pool = isinstance(tier_config, dict) and "shows" in tier_config
    try:
        if official_pool:
            # Production requests must resolve against the current official
            # show/pool/UP tables. Never fall back to an old default pack.
            pool = resolve_lottery_pool(show_cid, lottery_cid, up_cid_list, tier_config)
            legacy_draws = None
        else:
            pool = None
            legacy_draws = _legacy_lottery_draws(
                lottery_cid, draw_count, up_cid_list, action,
                tier_config, drop_config, rng,
            )
    except (LotteryPoolError, TypeError, ValueError, KeyError):
        return None
    cost_cid = int(action.get("costCid", 0) or 0)
    cost_num = int(action.get("costNum", 0) or 0)
    base_drop_id = int(action.get("baseDrop", 0) or 0)
    base_items = []
    base_pairs = []
    if base_drop_id:
        configured_base_items = drop_config.get(str(base_drop_id), drop_config.get(base_drop_id))
        if not isinstance(configured_base_items, list) or not configured_base_items:
            return None
        for item in configured_base_items:
            try:
                cid = int(item.get("cid", 0))
                quantity = int(item.get("num", 0))
            except (TypeError, ValueError):
                return None
            if cid <= 0 or quantity <= 0:
                return None
            base_items.append({"cid": cid, "num": quantity, "tag": 0})
            base_pairs.append((cid, quantity))
        if len(base_items) > 1:
            return None
    now = int(time.time())

    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT uid FROM players WHERE uid=?", (uid,)).fetchone() is None:
                raise ValueError("lottery player does not exist")
            previous = connection.execute(
                "SELECT left_insure_time FROM lottery_pool_state WHERE uid=? AND show_cid=?",
                (uid, show_cid),
            ).fetchone()
            if official_pool:
                drawn, left_insure_time = _draw_from_pool(
                    pool, int(previous["left_insure_time"]) if previous else 0, draw_count, rng,
                )
                draw_pairs = [(cid, 1) for cid in drawn]
            else:
                left_insure_time = 0
                draw_pairs = [
                    (cid, quantity)
                    for pairs in legacy_draws
                    for cid, quantity in pairs
                ]
                drawn = [cid for cid, _quantity in draw_pairs]
            consumed = _consume_reward_pairs_connection(
                connection, uid, [(cost_cid, cost_num)] if cost_cid and cost_num else [], now,
            )
            if consumed is None:
                raise ValueError("lottery cost cannot be paid")

            show_items = []
            new_soul_ids = []
            duplicate_soul_ids = []
            duplicate_soul_pairs = []
            for cid, quantity in draw_pairs:
                soul_number = _soul_number_from_template_cid(cid)
                if soul_number is None:
                    show_items.append({"cid": int(cid), "num": int(quantity), "tag": 0})
                    continue

                soul_id = int(cid)
                reward_cid = _soul_reward_item_cid(soul_id)
                show_items.append({"cid": reward_cid, "num": int(quantity), "tag": 0})
                owned = connection.execute(
                    "SELECT 1 FROM souls WHERE uid=? AND soul_id=?",
                    (uid, soul_id),
                ).fetchone()
                for _ in range(int(quantity)):
                    if owned is None:
                        connection.execute(
                            "INSERT INTO souls(uid,soul_id,level,affection,created_at) VALUES(?,?,?,?,?)",
                            (uid, soul_id, 1, 0, now),
                        )
                        owned = True
                        new_soul_ids.append(soul_id)
                    else:
                        duplicate_soul_ids.append(soul_id)
                        duplicate_soul_pairs.append((_soul_duplicate_item_cid(soul_id), 20))

            normal_draw_pairs = [
                (cid, quantity) for cid, quantity in draw_pairs
                if _ITEM_TYPE_BY_CID.get(int(cid)) != 3
                and _soul_number_from_template_cid(cid) is None
            ]
            applied = _apply_reward_pairs_connection(
                connection, uid, base_pairs + normal_draw_pairs, now,
            )
            duplicate_applied = _apply_reward_pairs_connection(
                connection, uid, duplicate_soul_pairs, now,
            )
            equipment_instances = _insert_equipment_instances_connection(connection, uid, draw_pairs, now)

            connection.execute(
                "INSERT INTO lottery_pool_state(uid,show_cid,left_insure_time,draw_count,left_hidden_insure_time) "
                "VALUES(?,?,?,?,?) ON CONFLICT(uid,show_cid) DO UPDATE SET "
                "draw_count=draw_count+?, left_insure_time=?",
                (uid, show_cid, left_insure_time, draw_count, -1, draw_count, left_insure_time),
            )
            connection.execute(
                "INSERT INTO lottery_records(uid,lottery_cid,last_draw_time) VALUES(?,?,?) "
                "ON CONFLICT(uid,lottery_cid) DO UPDATE SET last_draw_time=?",
                (uid, lottery_cid, now, now),
            )
            for item in show_items:
                connection.execute(
                    "INSERT INTO lottery_history(uid,lottery_cid,item_cid,item_num,draw_time) VALUES(?,?,?,?,?)",
                    (uid, lottery_cid, item["cid"], item["num"], now),
                )
            pool_row = connection.execute(
                "SELECT show_cid,left_insure_time,draw_count,left_hidden_insure_time "
                "FROM lottery_pool_state WHERE uid=? AND show_cid=?", (uid, show_cid),
            ).fetchone()
            records_rows = connection.execute(
                "SELECT lottery_cid,last_draw_time FROM lottery_records WHERE uid=?", (uid,),
            ).fetchall()
    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        return None

    return {
        "lotteryShowPOD": {
            "showCid": pool_row["show_cid"],
            "leftInsureTime": pool_row["left_insure_time"],
            "drawCount": pool_row["draw_count"],
            "leftHiddenInsureTime": pool_row["left_hidden_insure_time"],
        },
        "lotteryRecords": {row["lottery_cid"]: row["last_draw_time"] for row in records_rows},
        "baseShowItems": base_items,
        "showItems": show_items,
        "lotteryCid": lottery_cid,
        "changed_attrs": {
            **consumed["changed_attrs"],
            **applied["changed_attrs"],
            **duplicate_applied["changed_attrs"],
        },
        "changed_items": (
            consumed["changed_items"]
            + applied["changed_items"]
            + duplicate_applied["changed_items"]
            + equipment_instances
        ),
        "equipmentInstances": equipment_instances,
        "newSoulIds": new_soul_ids,
        "duplicateSoulIds": duplicate_soul_ids,
    }


def record_lottery_history(uid, lottery_cid, item_cid, item_num, draw_time=None):
    timestamp = int(time.time()) if draw_time is None else int(draw_time)
    with connect() as connection:
        connection.execute(
            "INSERT INTO lottery_history(uid,lottery_cid,item_cid,item_num,draw_time) VALUES(?,?,?,?,?)",
            (uid, lottery_cid, item_cid, item_num, timestamp),
        )


def get_lottery_history(uid, limit=200):
    with connect() as connection:
        rows = connection.execute(
            "SELECT lottery_cid,item_cid,item_num,draw_time FROM lottery_history "
            "WHERE uid=? ORDER BY id DESC LIMIT ?",
            (uid, limit),
        ).fetchall()
    return [
        {
            "lotteryCid": row["lottery_cid"],
            "itemCid": row["item_cid"],
            "itemNum": row["item_num"],
            "time": row["draw_time"],
        }
        for row in rows
    ]


def _ensure_quest_state_tables():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quest_progress (
                uid TEXT NOT NULL,
                quest_cid INTEGER NOT NULL,
                fin_num INTEGER NOT NULL,
                tgt_num INTEGER NOT NULL,
                create_time INTEGER NOT NULL,
                PRIMARY KEY(uid, quest_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quest_lists (
                uid TEXT NOT NULL,
                list_name TEXT NOT NULL,
                quest_cid INTEGER NOT NULL,
                PRIMARY KEY(uid, list_name, quest_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quest_reward_log (
                uid TEXT NOT NULL,
                quest_cid INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(uid, quest_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def seed_quest_state(uid, quests, finish_ids, fail_ids, unlock_ids):
    if not uid:
        return 0
    inserted = 0
    with connect() as connection:
        completed_ids = {
            row["quest_cid"]
            for row in connection.execute(
                "SELECT quest_cid FROM quest_lists WHERE uid=? AND list_name='finish'",
                (uid,),
            ).fetchall()
        }
        completed_ids.update(
            row["quest_cid"]
            for row in connection.execute(
                "SELECT quest_cid FROM quest_reward_log WHERE uid=?", (uid,)
            ).fetchall()
        )
        for cid in completed_ids:
            connection.execute(
                "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                (uid, "finish", cid),
            )
        for quest in quests or []:
            cid = quest.get("cid")
            if not isinstance(cid, int) or cid in completed_ids:
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO quest_progress(uid,quest_cid,fin_num,tgt_num,create_time) VALUES(?,?,?,?,?)",
                (
                    uid,
                    cid,
                    int(quest.get("finNum", 0)),
                    int(quest.get("tgtNum", 0)),
                    int(quest.get("createTime", 0)),
                ),
            )
            inserted += cursor.rowcount
        for list_name, values in (
            ("finish", finish_ids),
            ("fail", fail_ids),
            ("unlock", unlock_ids),
        ):
            for cid in values or []:
                if not isinstance(cid, int):
                    continue
                # The captured PlayerPOD contains a stale completed sign task.
                # Sign-in state is reconstructed from the current-month bitmask
                # and must never be promoted by an old login fixture.
                if list_name == "finish" and cid == SIGN_IN_QUEST_ID:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                    (uid, list_name, cid),
                )
    return inserted


def get_quest_state(uid):
    with connect() as connection:
        quests = connection.execute(
            """
            SELECT progress.quest_cid,progress.fin_num,progress.tgt_num,progress.create_time
            FROM quest_progress AS progress
            WHERE progress.uid=?
              AND NOT EXISTS (
                  SELECT 1 FROM quest_lists AS completed
                  WHERE completed.uid=progress.uid
                    AND completed.list_name='finish'
                    AND completed.quest_cid=progress.quest_cid
              )
            ORDER BY progress.quest_cid
            """,
            (uid,),
        ).fetchall()
        lists = connection.execute(
            "SELECT list_name,quest_cid FROM quest_lists WHERE uid=? ORDER BY list_name,quest_cid",
            (uid,),
        ).fetchall()
    result = {
        "quests": [
            {
                "cid": row["quest_cid"],
                "finNum": row["fin_num"],
                "tgtNum": row["tgt_num"],
                "createTime": row["create_time"],
            }
            for row in quests
        ],
        "finishQuestList": [],
        "failQuestList": [],
        "unlockChapterTasks": [],
    }
    for row in lists:
        key = {
            "finish": "finishQuestList",
            "fail": "failQuestList",
            "unlock": "unlockChapterTasks",
        }.get(row["list_name"])
        if key:
            result[key].append(row["quest_cid"])
    return result


def commit_quests(uid, quest_ids, task_rewards):
    if not uid or not isinstance(quest_ids, list) or not quest_ids:
        return None
    ids = list(dict.fromkeys(quest_ids))
    if any(not isinstance(cid, int) for cid in ids):
        return None
    now = int(time.time())
    with connect() as connection:
        finish_ids = {
            row["quest_cid"]
            for row in connection.execute(
                "SELECT quest_cid FROM quest_lists WHERE uid=? AND list_name='finish'", (uid,)
            ).fetchall()
        }
        completed_quests = []
        for cid in ids:
            if cid in finish_ids:
                return None
            if connection.execute(
                "SELECT 1 FROM quest_reward_log WHERE uid=? AND quest_cid=?",
                (uid, cid),
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT quest_cid,fin_num,tgt_num,create_time FROM quest_progress "
                "WHERE uid=? AND quest_cid=?",
                (uid, cid),
            ).fetchone()
            if row is None or row["fin_num"] < row["tgt_num"]:
                return None
            completed_quests.append(
                {
                    "cid": row["quest_cid"],
                    "finNum": row["fin_num"],
                    "tgtNum": row["tgt_num"],
                    "createTime": row["create_time"],
                }
            )

        validated_rewards = {}
        for cid in ids:
            definition = task_rewards.get(str(cid))
            rewards = definition.get("rewards") if definition else None
            if not rewards:
                return None
            if any(
                not isinstance(reward, list)
                or len(reward) != 2
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in reward)
                or reward[1] <= 0
                for reward in rewards
            ):
                return None
            validated_rewards[cid] = rewards

        awards = []
        changed_attrs = {}
        changed_items = {}
        for cid in ids:
            task_awards = []
            for reward in validated_rewards[cid]:
                item_cid, quantity = reward
                task_awards.append({"cid": item_cid, "num": quantity, "tag": 0})
                attr = connection.execute(
                    "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?",
                    (uid, item_cid),
                ).fetchone()
                if attr is not None:
                    total = attr["quantity"] + quantity
                    connection.execute(
                        "UPDATE player_num_attrs SET quantity=?,updated_at=? WHERE uid=? AND cid=?",
                        (total, now, uid, item_cid),
                    )
                    changed_attrs[item_cid] = total
                    continue
                item = connection.execute(
                    "SELECT id,quantity,created_at FROM items WHERE uid=? AND template_id=?",
                    (uid, item_cid),
                ).fetchone()
                if item is None:
                    cursor = connection.execute(
                        "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                        (uid, item_cid, quantity, now),
                    )
                    item_id, total, created_at = cursor.lastrowid, quantity, now
                else:
                    total = item["quantity"] + quantity
                    connection.execute("UPDATE items SET quantity=? WHERE id=?", (total, item["id"]))
                    item_id, created_at = item["id"], item["created_at"]
                changed_items[item_cid] = {
                    "id": item_id,
                    "cid": item_cid,
                    "num": total,
                    "usedNum": 0,
                    "createTime": created_at,
                }
            awards.extend(task_awards)
            connection.execute("DELETE FROM quest_progress WHERE uid=? AND quest_cid=?", (uid, cid))
            connection.execute(
                "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                (uid, "finish", cid),
            )
            connection.execute(
                "INSERT INTO quest_reward_log(uid,quest_cid,result_json,created_at) VALUES(?,?,?,?)",
                (uid, cid, json.dumps(task_awards, separators=(",", ":")), now),
            )
    return {
        "cids": ids,
        "awards": awards,
        "changed_attrs": changed_attrs,
        "changed_items": list(changed_items.values()),
        "completed_quests": completed_quests,
    }


def unlock_chapter_tasks(uid, chapter_ids):
    if not uid or not isinstance(chapter_ids, list):
        return None
    ids = list(dict.fromkeys(chapter_ids))
    if any(not isinstance(cid, int) for cid in ids):
        return None
    with connect() as connection:
        for cid in ids:
            connection.execute(
                "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                (uid, "unlock", cid),
            )
    return ids


# ── Item persistence ──

def _ensure_items_table():
    """Create the items table if it does not exist."""
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                template_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                extra TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_uid ON items(uid)"
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
        if "snapshot_seeded" not in columns:
            connection.execute(
                "ALTER TABLE items ADD COLUMN snapshot_seeded INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            UPDATE items
            SET quantity = (
                SELECT SUM(duplicate.quantity)
                FROM items AS duplicate
                WHERE duplicate.uid = items.uid
                  AND duplicate.template_id = items.template_id
            ),
                snapshot_seeded = (
                    SELECT MAX(duplicate.snapshot_seeded)
                    FROM items AS duplicate
                    WHERE duplicate.uid = items.uid
                      AND duplicate.template_id = items.template_id
                )
            WHERE id = (
                SELECT MIN(duplicate.id)
                FROM items AS duplicate
                WHERE duplicate.uid = items.uid
                  AND duplicate.template_id = items.template_id
            )
            """
        )
        connection.execute(
            """
            DELETE FROM items
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM items
                GROUP BY uid, template_id
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uid_template "
            "ON items(uid, template_id)"
        )


def get_items(uid):
    """Return list of {template_id, quantity} for a player."""
    if not uid:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT template_id, quantity FROM items WHERE uid = ? ORDER BY template_id",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_item_pods(uid):
    if not uid:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT id,template_id,quantity,created_at FROM items WHERE uid=? ORDER BY id",
            (uid,),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "cid": int(row["template_id"]),
            "num": int(row["quantity"]),
            "usedNum": 0,
            "createTime": int(row["created_at"]),
        }
        for row in rows
        if _ITEM_TYPE_BY_CID.get(int(row["template_id"])) not in (3, 12)
    ]


def seed_items_from_snapshot(uid, item_pods):
    """Insert captured stackable inventory rows only when no local row exists."""
    if not uid:
        return 0
    now = int(time.time())
    totals = {}
    for pod in item_pods:
        cid = pod.get("cid")
        num = pod.get("num")
        if (
            isinstance(cid, int)
            and cid > 0
            and _ITEM_TYPE_BY_CID.get(cid) not in (3, 12)
            and isinstance(num, int)
            and num > 0
        ):
            totals[cid] = totals.get(cid, 0) + num
    inserted = 0
    with connect() as connection:
        for cid, quantity in totals.items():
            exists = connection.execute(
                "SELECT 1 FROM items WHERE uid=? AND template_id=?", (uid, cid)
            ).fetchone()
            if exists:
                continue
            connection.execute(
                "INSERT INTO items(uid,template_id,quantity,created_at,snapshot_seeded) VALUES(?,?,?,?,1)",
                (uid, cid, quantity, now),
            )
            inserted += 1
    return inserted


def add_item(uid, template_id, quantity=1):
    """Add quantity to an item. Inserts if not already owned."""
    if not uid or quantity <= 0:
        return
    now = int(time.time())
    with connect() as connection:
        existing = connection.execute(
            "SELECT id, quantity FROM items WHERE uid = ? AND template_id = ?",
            (uid, template_id),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE items SET quantity = ? WHERE id = ?",
                (existing["quantity"] + quantity, existing["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO items (uid, template_id, quantity, created_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, template_id, quantity, now),
            )


def remove_item(uid, template_id, quantity=1):
    """Remove quantity from an item. Deletes row if quantity reaches 0."""
    if not uid or quantity <= 0:
        return
    with connect() as connection:
        existing = connection.execute(
            "SELECT id, quantity FROM items WHERE uid = ? AND template_id = ?",
            (uid, template_id),
        ).fetchone()
        if existing:
            new_qty = existing["quantity"] - quantity
            if new_qty <= 0:
                connection.execute(
                    "DELETE FROM items WHERE id = ?", (existing["id"],)
                )
            else:
                connection.execute(
                    "UPDATE items SET quantity = ? WHERE id = ?",
                    (new_qty, existing["id"]),
                )


# ── Soul / character persistence ──

DEFAULT_SOULS_LIST = [
    (20010001, 70),  # 柯露雪儿
    (20010002, 70),  # 薇姬娜
    (20010003, 70),  # 尼柯莱特
]

# Player-facing roster verified from the decoded 3910 snapshot.  The broader
# battle config also contains NPC/placeholder soul records and must not make
# those records selectable in a player's formation.
PLAYABLE_SOUL_IDS = frozenset(
    20010000 + number for number in range(1, 56) if number != 26
)


def is_playable_soul_id(soul_id):
    try:
        return int(soul_id) in PLAYABLE_SOUL_IDS
    except (TypeError, ValueError):
        return False

def _ensure_souls_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS souls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                soul_id INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                affection INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid),
                UNIQUE(uid, soul_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_souls_uid ON souls(uid)"
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(souls)")}
        for name, definition in (
            ("favor", "INTEGER NOT NULL DEFAULT 0"),
            ("favor_level", "INTEGER NOT NULL DEFAULT 1"),
            ("daily_dislike", "INTEGER NOT NULL DEFAULT 0"),
            ("oath_activation", "INTEGER NOT NULL DEFAULT 0"),
            ("fondle_num", "INTEGER NOT NULL DEFAULT 0"),
            ("next_recovery_fondle_time", "INTEGER NOT NULL DEFAULT 0"),
            ("snapshot_seeded", "INTEGER NOT NULL DEFAULT 0"),
            ("oath_activated_at", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE souls ADD COLUMN {name} {definition}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dating_records (
                uid TEXT NOT NULL,
                soul_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                PRIMARY KEY(uid, soul_id, event_id),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_reward_log (
                uid TEXT NOT NULL,
                operation TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(uid, operation, operation_id),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def migrate_invalid_soul_lottery_items():
    """Convert old soul-template warehouse rows into duplicate fragments once."""
    if not DB_PATH.exists():
        return {"converted": 0, "backup": None}

    with connect() as connection:
        rows = connection.execute(
            "SELECT uid,template_id,SUM(quantity) AS quantity FROM items "
            "WHERE template_id BETWEEN 20010001 AND 20010055 "
            "AND template_id <> 20010026 GROUP BY uid,template_id"
        ).fetchall()
        if not rows:
            return {"converted": 0, "backup": None}

        backup_path = DB_PATH.with_name(
            f"{DB_PATH.stem}.pre-soul-lottery-migration-{int(time.time())}{DB_PATH.suffix}"
        )
        backup_connection = sqlite3.connect(str(backup_path))
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()

        converted = 0
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            duplicate_cid = _soul_duplicate_item_cid(row["template_id"])
            quantity = max(0, int(row["quantity"] or 0))
            if duplicate_cid is None or quantity <= 0:
                continue
            connection.execute(
                "INSERT INTO items(uid,template_id,quantity,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(uid,template_id) DO UPDATE SET "
                "quantity=items.quantity+excluded.quantity",
                (row["uid"], duplicate_cid, quantity * 20, int(time.time())),
            )
            connection.execute(
                "DELETE FROM items WHERE uid=? AND template_id=?",
                (row["uid"], row["template_id"]),
            )
            converted += 1
    return {"converted": converted, "backup": str(backup_path)}


def get_souls(uid):
    """Return list of souls owned by a player."""
    if not uid:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT soul_id, level, affection, favor, favor_level, daily_dislike, "
            "oath_activation, fondle_num, next_recovery_fondle_time "
            "FROM souls WHERE uid = ? ORDER BY soul_id",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_show_soul(uid, soul_id):
    if not uid or not isinstance(soul_id, int) or soul_id <= 0:
        return False
    with connect() as connection:
        owned = connection.execute(
            "SELECT 1 FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone()
        if owned is None:
            return False
        connection.execute(
            "UPDATE players SET current_show_soul_cid=?,updated_at=? WHERE uid=?",
            (soul_id, int(time.time()), uid),
        )
    return True


def get_companion(uid, soul_id):
    if not uid or not isinstance(soul_id, int) or soul_id <= 0:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM souls WHERE uid = ? AND soul_id = ?", (uid, soul_id)
        ).fetchone()
    return dict(row) if row else None


def seed_companions_from_snapshot(uid, soul_pods):
    """Seed untouched local soul rows from the captured compatibility snapshot."""
    if not uid:
        return 0
    now = int(time.time())
    seeded = 0
    with connect() as connection:
        for pod in soul_pods:
            soul_id = pod.get("cid")
            if not isinstance(soul_id, int) or soul_id <= 0:
                continue
            existing = connection.execute(
                "SELECT id, snapshot_seeded FROM souls WHERE uid=? AND soul_id=?",
                (uid, soul_id),
            ).fetchone()
            values = (
                int(pod.get("lv", 1)),
                int(pod.get("favor", 0)),
                int(pod.get("favorLv", 1)),
                int(bool(pod.get("dailyDislike", False))),
                int(bool(pod.get("oathActivation", False))),
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO souls(uid,soul_id,level,affection,created_at,favor,favor_level,daily_dislike,oath_activation,snapshot_seeded) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (uid, soul_id, values[0], values[1], now, values[1], values[2], values[3], values[4]),
                )
                seeded += 1
            elif not existing["snapshot_seeded"]:
                connection.execute(
                    "UPDATE souls SET level=?, affection=?, favor=?, favor_level=?, daily_dislike=?, oath_activation=?, snapshot_seeded=1 "
                    "WHERE id=?",
                    (values[0], values[1], values[1], values[2], values[3], values[4], existing["id"]),
                )
                seeded += 1
    return seeded


def seed_player_companion_state(uid, player_pod):
    if not uid:
        return False
    with connect() as connection:
        player = connection.execute(
            "SELECT remainder_give_gift_num, fondle_num, next_recovery_fondle_time FROM players WHERE uid=?",
            (uid,),
        ).fetchone()
        if player is None:
            return False
        connection.execute(
            "UPDATE players SET remainder_give_gift_num=COALESCE(remainder_give_gift_num,?), "
            "give_gift_daily_max=COALESCE(give_gift_daily_max,?), companion_reset_day=COALESCE(companion_reset_day,?), "
            "fondle_num=COALESCE(fondle_num,?), next_recovery_fondle_time=COALESCE(next_recovery_fondle_time,?) WHERE uid=?",
            (
                int(player_pod.get("remainderGiveGiftNum", 0)),
                int(player_pod.get("remainderGiveGiftNum", 0)),
                time.strftime("%Y-%m-%d", time.localtime()),
                int(player_pod.get("fondleNum", 0)),
                int(player_pod.get("nextRecoveryFondleTime", 0)),
                uid,
            ),
        )
    return True


def get_player_companion_state(uid):
    with connect() as connection:
        row = connection.execute(
            "SELECT remainder_give_gift_num, give_gift_daily_max, companion_reset_day, fondle_num, next_recovery_fondle_time FROM players WHERE uid=?",
            (uid,),
        ).fetchone()
        if row is None:
            return None
        state = dict(row)
        now = int(time.time())
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        updates = {}
        if state["companion_reset_day"] != today:
            updates["remainder_give_gift_num"] = int(state["give_gift_daily_max"] or 0)
            updates["companion_reset_day"] = today
        fondle_num = int(state["fondle_num"] or 0)
        next_time = int(state["next_recovery_fondle_time"] or 0)
        if fondle_num < 5 and next_time and now >= next_time:
            recovered = 1 + (now - next_time) // 10800
            fondle_num = min(5, fondle_num + recovered)
            updates["fondle_num"] = fondle_num
            updates["next_recovery_fondle_time"] = (
                0 if fondle_num >= 5 else next_time + recovered * 10800
            )
        if updates:
            assignments = ", ".join(f"{name}=?" for name in updates)
            connection.execute(
                f"UPDATE players SET {assignments} WHERE uid=?",
                (*updates.values(), uid),
            )
            state.update(updates)
    return state


def apply_gift(uid, soul_id, gift_cid, item_id, add_favor, new_favor_level, operation_id):
    if not uid or any(not isinstance(value, int) for value in (soul_id, gift_cid, item_id, add_favor, new_favor_level)):
        return None
    now = int(time.time())
    with connect() as connection:
        previous = connection.execute(
            "SELECT result_json FROM companion_reward_log WHERE uid=? AND operation='gift' AND operation_id=?",
            (uid, operation_id),
        ).fetchone()
        if previous:
            return {**json.loads(previous["result_json"]), "duplicate": True}
        soul = connection.execute(
            "SELECT favor FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone()
        player = connection.execute(
            "SELECT remainder_give_gift_num FROM players WHERE uid=?", (uid,)
        ).fetchone()
        item = connection.execute(
            "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
            (uid, item_id),
        ).fetchone()
        if soul is None or player is None or not player["remainder_give_gift_num"] or item is None or item["quantity"] < 1:
            return None
        item_quantity = item["quantity"] - 1
        if item_quantity:
            connection.execute("UPDATE items SET quantity=? WHERE id=?", (item_quantity, item["id"]))
        else:
            connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
        new_favor = soul["favor"] + add_favor
        remaining = player["remainder_give_gift_num"] - 1
        connection.execute(
            "UPDATE souls SET favor=?, affection=?, favor_level=? WHERE uid=? AND soul_id=?",
            (new_favor, new_favor, new_favor_level, uid, soul_id),
        )
        connection.execute(
            "UPDATE players SET remainder_give_gift_num=? WHERE uid=?", (remaining, uid)
        )
        result = {
            "soul_id": soul_id,
            "gift_cid": gift_cid,
            "item_id": item_id,
            "item_quantity": item_quantity,
            "add_favor": add_favor,
            "favor": new_favor,
            "favor_level": new_favor_level,
            "remainder_give_gift_num": remaining,
        }
        connection.execute(
            "INSERT INTO companion_reward_log(uid,operation,operation_id,created_at,result_json) VALUES(?,?,?,?,?)",
            (uid, "gift", operation_id, now, json.dumps(result, separators=(",", ":"))),
        )
    return {**result, "duplicate": False}


def apply_fondle(uid, soul_id, action_cid, add_favor, dislike, new_favor_level, operation_id):
    now = int(time.time())
    with connect() as connection:
        previous = connection.execute(
            "SELECT result_json FROM companion_reward_log WHERE uid=? AND operation='fondle' AND operation_id=?",
            (uid, operation_id),
        ).fetchone()
        if previous:
            return {**json.loads(previous["result_json"]), "duplicate": True}
        soul = connection.execute(
            "SELECT favor FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone()
        player = connection.execute(
            "SELECT fondle_num, next_recovery_fondle_time FROM players WHERE uid=?",
            (uid,),
        ).fetchone()
        if soul is None or player is None or not player["fondle_num"]:
            return None
        remaining = player["fondle_num"] - 1
        next_recovery = int(player["next_recovery_fondle_time"] or 0)
        if next_recovery <= now:
            next_recovery = now + 10800
        new_favor = soul["favor"] + add_favor
        connection.execute(
            "UPDATE souls SET favor=?, affection=?, favor_level=?, daily_dislike=? WHERE uid=? AND soul_id=?",
            (new_favor, new_favor, new_favor_level, int(dislike), uid, soul_id),
        )
        connection.execute(
            "UPDATE players SET fondle_num=?, next_recovery_fondle_time=? WHERE uid=?",
            (remaining, next_recovery, uid),
        )
        result = {
            "soul_id": soul_id,
            "action_cid": action_cid,
            "add_favor": add_favor,
            "dislike": bool(dislike),
            "fondle_num": remaining,
            "next_recovery_fondle_time": next_recovery,
            "favor": new_favor,
            "favor_level": new_favor_level,
        }
        connection.execute(
            "INSERT INTO companion_reward_log(uid,operation,operation_id,created_at,result_json) VALUES(?,?,?,?,?)",
            (uid, "fondle", operation_id, now, json.dumps(result, separators=(",", ":"))),
        )
    return {**result, "duplicate": False}


def apply_oath(uid, soul_id, marry_id, cost_item_id, cost_quantity, reward_item_id):
    now = int(time.time())
    operation_id = str(marry_id)
    with connect() as connection:
        previous = connection.execute(
            "SELECT result_json FROM companion_reward_log WHERE uid=? AND operation='oath' AND operation_id=?",
            (uid, operation_id),
        ).fetchone()
        if previous:
            return {**json.loads(previous["result_json"]), "duplicate": True}
        soul = connection.execute(
            "SELECT oath_activation FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone()
        item = connection.execute(
            "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
            (uid, cost_item_id),
        ).fetchone()
        if soul is None or soul["oath_activation"] or item is None or item["quantity"] < cost_quantity:
            return None
        remaining = item["quantity"] - cost_quantity
        if remaining:
            connection.execute("UPDATE items SET quantity=? WHERE id=?", (remaining, item["id"]))
        else:
            connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
        connection.execute(
            "UPDATE souls SET oath_activation=1, oath_activated_at=? WHERE uid=? AND soul_id=?",
            (now, uid, soul_id),
        )
        reward_applied = _apply_reward_pairs_connection(
            connection, uid, [(int(reward_item_id), 1)], now
        )
        if reward_applied is None:
            return None
        result = {
            "soul_id": soul_id,
            "marry_id": marry_id,
            "cost_item_id": cost_item_id,
            "cost_item_quantity": remaining,
            "reward_item_id": reward_item_id,
            "reward_changed_attrs": reward_applied.get("changed_attrs", {}),
            "reward_changed_items": reward_applied.get("changed_items", []),
            "activated_at": now,
        }
        connection.execute(
            "INSERT INTO companion_reward_log(uid,operation,operation_id,created_at,result_json) VALUES(?,?,?,?,?)",
            (uid, "oath", operation_id, now, json.dumps(result, separators=(",", ":"))),
        )
    return {**result, "duplicate": False}


def has_dating_record(uid, soul_id, event_id):
    with connect() as connection:
        return connection.execute(
            "SELECT 1 FROM dating_records WHERE uid=? AND soul_id=? AND event_id=?",
            (uid, soul_id, event_id),
        ).fetchone() is not None


def apply_companion_operation(
    uid, soul_id, event_id, cost, rewards, favor_delta=0, favor_level=None
):
    """Apply one local dating settlement atomically and idempotently.

    ``cost`` and ``rewards`` are (template_id, quantity) pairs. The operation
    key prevents a repeated client packet from duplicating rewards.
    """
    if not uid or not isinstance(soul_id, int) or not isinstance(event_id, int):
        return False
    now = int(time.time())
    with connect() as connection:
        soul = connection.execute(
            "SELECT favor FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone()
        if soul is None:
            return False
        duplicate = connection.execute(
            "SELECT 1 FROM dating_records WHERE uid=? AND soul_id=? AND event_id=?",
            (uid, soul_id, event_id),
        ).fetchone()
        if duplicate:
            return False
        for template_id, quantity in cost:
            row = connection.execute(
                "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
                (uid, template_id),
            ).fetchone()
            if row is None or row["quantity"] < quantity:
                return False
        for template_id, quantity in cost:
            row = connection.execute(
                "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
                (uid, template_id),
            ).fetchone()
            remaining = row["quantity"] - quantity
            if remaining:
                connection.execute("UPDATE items SET quantity=? WHERE id=?", (remaining, row["id"]))
            else:
                connection.execute("DELETE FROM items WHERE id=?", (row["id"],))
        for template_id, quantity in rewards:
            row = connection.execute(
                "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
                (uid, template_id),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE items SET quantity=? WHERE id=?",
                    (row["quantity"] + quantity, row["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO items(uid, template_id, quantity, created_at) VALUES(?,?,?,?)",
                    (uid, template_id, quantity, now),
                )
        if favor_level is None:
            connection.execute(
                "UPDATE souls SET favor=favor+?, affection=affection+? WHERE uid=? AND soul_id=?",
                (favor_delta, favor_delta, uid, soul_id),
            )
        else:
            connection.execute(
                "UPDATE souls SET favor=favor+?, affection=affection+?, favor_level=? WHERE uid=? AND soul_id=?",
                (favor_delta, favor_delta, favor_level, uid, soul_id),
            )
        connection.execute(
            "INSERT INTO dating_records(uid,soul_id,event_id,completed_at) VALUES(?,?,?,?)",
            (uid, soul_id, event_id, now),
        )
    return True


def get_dating_records(uid, soul_id):
    with connect() as connection:
        rows = connection.execute(
            "SELECT event_id, completed_at FROM dating_records WHERE uid=? AND soul_id=? ORDER BY event_id",
            (uid, soul_id),
        ).fetchall()
    return {row["event_id"]: 1 for row in rows}


def ensure_default_souls(uid):
    """Insert default soul roster for a new player."""
    if not uid:
        return
    now = int(time.time())
    with connect() as connection:
        existing = connection.execute(
            "SELECT COUNT(*) FROM souls WHERE uid = ?", (uid,)
        ).fetchone()[0]
        if existing > 0:
            return
        for soul_id, level in DEFAULT_SOULS_LIST:
            connection.execute(
                "INSERT OR IGNORE INTO souls (uid, soul_id, level, created_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, soul_id, level, now),
            )


# ── Task persistence ──

DEFAULT_TASKS = [
    (1001, "通关主线第一章"),
    (1002, "提升任意人偶等级"),
    (1003, "进行一次抽卡"),
]


def _ensure_tasks_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid),
                UNIQUE(uid, task_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_uid ON tasks(uid)"
        )


def get_tasks(uid):
    """Return list of tasks for a player."""
    if not uid:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT task_id, progress, completed FROM tasks WHERE uid = ? ORDER BY task_id",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def ensure_default_tasks(uid):
    """Insert starter tasks for a new player."""
    if not uid:
        return
    now = int(time.time())
    with connect() as connection:
        existing = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE uid = ?", (uid,)
        ).fetchone()[0]
        if existing > 0:
            return
        for task_id, _ in DEFAULT_TASKS:
            connection.execute(
                "INSERT OR IGNORE INTO tasks (uid, task_id, created_at) "
                "VALUES (?, ?, ?)",
                (uid, task_id, now),
            )


# -- Local mail persistence --

def _ensure_mails_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mails (
                uid TEXT NOT NULL,
                mail_id INTEGER NOT NULL,
                cid INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                sender TEXT NOT NULL,
                is_read INTEGER NOT NULL,
                is_has_item INTEGER NOT NULL,
                create_time INTEGER NOT NULL,
                expire_time INTEGER NOT NULL,
                item_list_json TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                snapshot_seeded INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(uid, mail_id),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_pickup_log (
                uid TEXT NOT NULL,
                mail_id INTEGER NOT NULL,
                claimed_at INTEGER NOT NULL,
                PRIMARY KEY(uid, mail_id),
                FOREIGN KEY(uid, mail_id) REFERENCES mails(uid, mail_id)
            )
            """
        )


def seed_mails_from_snapshot(uid, mail_pods):
    if not uid:
        return 0
    inserted = 0
    with connect() as connection:
        for pod in mail_pods:
            mail_id = pod.get("id")
            if not isinstance(mail_id, int):
                continue
            cursor = connection.execute(
                "INSERT OR IGNORE INTO mails(uid,mail_id,cid,title,content,sender,is_read,is_has_item,create_time,expire_time,item_list_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    mail_id,
                    int(pod.get("cid", 0)),
                    str(pod.get("title", "")),
                    str(pod.get("content", "")),
                    str(pod.get("sender", "")),
                    int(bool(pod.get("isRead", False))),
                    int(bool(pod.get("isHasItem", False))),
                    int(pod.get("createTime", 0)),
                    int(pod.get("expireTime", 0)),
                    json.dumps(pod.get("itemList", []), separators=(",", ":")),
                ),
            )
            inserted += cursor.rowcount
    return inserted


def _mail_pod(row):
    return {
        "id": row["mail_id"],
        "cid": row["cid"],
        "title": row["title"],
        "content": row["content"],
        "sender": row["sender"],
        "isRead": bool(row["is_read"]),
        "isHasItem": bool(row["is_has_item"]),
        "createTime": row["create_time"],
        "expireTime": row["expire_time"],
        "itemList": json.loads(row["item_list_json"]),
    }


def get_mails(uid, mail_type=0, mail_types=None):
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM mails WHERE uid=? AND deleted=0 ORDER BY create_time DESC",
            (uid,),
        ).fetchall()
    pods = [_mail_pod(row) for row in rows]
    if mail_type and mail_types is not None:
        pods = [pod for pod in pods if int(mail_types.get(str(pod["cid"]), 0) or 0) == mail_type]
    return pods


def unread_mail_count(uid):
    with connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM mails WHERE uid=? AND deleted=0 AND is_read=0",
            (uid,),
        ).fetchone()[0]


def mark_mail_read(uid, mail_id):
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE mails SET is_read=1 WHERE uid=? AND mail_id=? AND deleted=0",
            (uid, mail_id),
        )
    return cursor.rowcount == 1


def delete_mails(uid, mail_ids):
    deleted = []
    with connect() as connection:
        for mail_id in mail_ids:
            cursor = connection.execute(
                "UPDATE mails SET deleted=1 WHERE uid=? AND mail_id=? AND deleted=0 AND is_has_item=0",
                (uid, mail_id),
            )
            if cursor.rowcount:
                deleted.append(mail_id)
    return deleted


def pick_up_mail_attachments(uid, mail_ids):
    """Claim mail attachments atomically and return protocol-ready changes."""
    if not uid or not isinstance(mail_ids, list) or not mail_ids:
        return None
    unique_ids = list(dict.fromkeys(mail_ids))
    if any(not isinstance(mail_id, int) for mail_id in unique_ids):
        return None

    now = int(time.time())
    with connect() as connection:
        rows = []
        for mail_id in unique_ids:
            row = connection.execute(
                "SELECT * FROM mails WHERE uid=? AND mail_id=? AND deleted=0",
                (uid, mail_id),
            ).fetchone()
            claimed = connection.execute(
                "SELECT 1 FROM mail_pickup_log WHERE uid=? AND mail_id=?",
                (uid, mail_id),
            ).fetchone()
            if row is None or (not row["is_has_item"] and claimed is None):
                return None
            rows.append((row, claimed is not None))

        rewards = []
        changed_attrs = {}
        changed_items = {}
        mails = []
        for row, duplicate in rows:
            item_list = json.loads(row["item_list_json"])
            rewards.extend(item_list)
            if not duplicate:
                for reward in item_list:
                    cid = reward.get("cid")
                    quantity = reward.get("num")
                    if not isinstance(cid, int) or not isinstance(quantity, int) or quantity <= 0:
                        raise ValueError(f"invalid mail reward: {reward!r}")
                    attr = connection.execute(
                        "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?",
                        (uid, cid),
                    ).fetchone()
                    if attr is not None:
                        total = attr["quantity"] + quantity
                        connection.execute(
                            "UPDATE player_num_attrs SET quantity=?,updated_at=? WHERE uid=? AND cid=?",
                            (total, now, uid, cid),
                        )
                        changed_attrs[cid] = total
                        continue

                    item = connection.execute(
                        "SELECT id,quantity,created_at FROM items WHERE uid=? AND template_id=?",
                        (uid, cid),
                    ).fetchone()
                    if item is None:
                        cursor = connection.execute(
                            "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                            (uid, cid, quantity, now),
                        )
                        item_id, total, created_at = cursor.lastrowid, quantity, now
                    else:
                        total = item["quantity"] + quantity
                        connection.execute(
                            "UPDATE items SET quantity=? WHERE id=?", (total, item["id"])
                        )
                        item_id, created_at = item["id"], item["created_at"]
                    changed_items[cid] = {
                        "id": item_id,
                        "cid": cid,
                        "num": total,
                        "usedNum": 0,
                        "createTime": created_at,
                    }
                connection.execute(
                    "INSERT INTO mail_pickup_log(uid,mail_id,claimed_at) VALUES(?,?,?)",
                    (uid, row["mail_id"], now),
                )
                connection.execute(
                    "UPDATE mails SET is_read=1,is_has_item=0 WHERE uid=? AND mail_id=?",
                    (uid, row["mail_id"]),
                )
            mail = _mail_pod(row)
            mail["isRead"] = True
            mail["isHasItem"] = False
            mails.append(mail)

        unread_count = connection.execute(
            "SELECT COUNT(*) FROM mails WHERE uid=? AND deleted=0 AND is_read=0", (uid,)
        ).fetchone()[0]
    return {
        "mails": mails,
        "rewards": rewards,
        "changed_attrs": changed_attrs,
        "changed_items": list(changed_items.values()),
        "unread_count": unread_count,
    }


# -- Local library persistence --

def _ensure_library_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS library_state (
                uid TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def seed_library_state(uid, library_pod):
    with connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO library_state(uid,state_json,updated_at) VALUES(?,?,?)",
            (uid, json.dumps(library_pod, ensure_ascii=False, separators=(",", ":")), int(time.time())),
        )
    return cursor.rowcount == 1


def get_library_state(uid):
    with connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM library_state WHERE uid=?", (uid,)
        ).fetchone()
    return _restore_numeric_keys(json.loads(row["state_json"])) if row else None


def _restore_numeric_keys(value):
    if isinstance(value, list):
        return [_restore_numeric_keys(item) for item in value]
    if isinstance(value, dict):
        restored = {}
        for key, item in value.items():
            restored_key = int(key) if isinstance(key, str) and key.lstrip("-").isdigit() else key
            restored[restored_key] = _restore_numeric_keys(item)
        return restored
    return value


def mark_news_book_viewed(uid, news_id):
    with connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM library_state WHERE uid=?", (uid,)
        ).fetchone()
        if row is None:
            return False
        state = json.loads(row["state_json"])
        state.setdefault("newsBook", {})[str(news_id)] = True
        connection.execute(
            "UPDATE library_state SET state_json=?, updated_at=? WHERE uid=?",
            (json.dumps(state, ensure_ascii=False, separators=(",", ":")), int(time.time()), uid),
        )
    return True


def claim_news_book_reward(uid, news_id, rewards):
    if not uid or not isinstance(news_id, int) or not isinstance(rewards, list):
        return None
    now = int(time.time())
    with connect() as connection:
        row = connection.execute(
            "SELECT state_json FROM library_state WHERE uid=?", (uid,)
        ).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        if str(news_id) not in state.get("newsBook", {}):
            return None
        claimed = state.setdefault("getNewsBook", [])
        duplicate = news_id in claimed
        changed_attrs = {}
        changed_items = {}
        item_shows = []
        for reward in rewards:
            if (
                not isinstance(reward, list)
                or len(reward) != 2
                or not all(isinstance(value, int) for value in reward)
                or reward[1] <= 0
            ):
                raise ValueError(f"invalid news-book reward: {reward!r}")
            cid, quantity = reward
            item_shows.append({"cid": cid, "num": quantity, "tag": 0})
            if duplicate:
                continue
            attr = connection.execute(
                "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?", (uid, cid)
            ).fetchone()
            if attr is not None:
                total = attr["quantity"] + quantity
                connection.execute(
                    "UPDATE player_num_attrs SET quantity=?,updated_at=? WHERE uid=? AND cid=?",
                    (total, now, uid, cid),
                )
                changed_attrs[cid] = total
                continue
            item = connection.execute(
                "SELECT id,quantity,created_at FROM items WHERE uid=? AND template_id=?",
                (uid, cid),
            ).fetchone()
            if item is None:
                cursor = connection.execute(
                    "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                    (uid, cid, quantity, now),
                )
                item_id, total, created_at = cursor.lastrowid, quantity, now
            else:
                total = item["quantity"] + quantity
                connection.execute("UPDATE items SET quantity=? WHERE id=?", (total, item["id"]))
                item_id, created_at = item["id"], item["created_at"]
            changed_items[cid] = {
                "id": item_id,
                "cid": cid,
                "num": total,
                "usedNum": 0,
                "createTime": created_at,
            }
        if not duplicate:
            claimed.append(news_id)
            connection.execute(
                "UPDATE library_state SET state_json=?,updated_at=? WHERE uid=?",
                (json.dumps(state, ensure_ascii=False, separators=(",", ":")), now, uid),
            )
    return {
        "item_shows": item_shows,
        "changed_attrs": changed_attrs,
        "changed_items": list(changed_items.values()),
        "duplicate": duplicate,
    }


# ── Player settings persistence ──

def _ensure_player_settings_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS player_settings (
                uid TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(uid, key),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def save_player_setting(uid, key, value):
    if not uid or not isinstance(key, str) or value is None:
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute(
            "INSERT INTO player_settings(uid,key,value,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (uid, key, str(value), now),
        )
    return True


def get_player_setting(uid, key):
    if not uid or not isinstance(key, str):
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT value FROM player_settings WHERE uid=? AND key=?", (uid, key)
        ).fetchone()
    return row[0] if row else None


# ── Active / sign-in persistence ──

def _ensure_active_sign_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS active_sign_in (
                uid TEXT PRIMARY KEY,
                sign_date TEXT NOT NULL,
                sign_count INTEGER NOT NULL,
                sign_info INTEGER NOT NULL DEFAULT 0,
                sign_month TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(active_sign_in)")}
        if "sign_info" not in columns:
            connection.execute(
                "ALTER TABLE active_sign_in ADD COLUMN sign_info INTEGER NOT NULL DEFAULT 0"
            )
        if "sign_month" not in columns:
            connection.execute(
                "ALTER TABLE active_sign_in ADD COLUMN sign_month TEXT NOT NULL DEFAULT ''"
            )


SIGN_IN_RULES = {"1": {"Id": 1, "Reward": [2, 20]}}
SIGN_IN_QUEST_ID = 10190101
SIGN_IN_QUEST_TARGET = 7


def configure_sign_in(sign_in_rules):
    """Install extracted CfgSignInTable rows used by the daily sign-in flow."""
    global SIGN_IN_RULES
    SIGN_IN_RULES = {
        str(row_id): value
        for row_id, value in (sign_in_rules or {}).items()
        if isinstance(value, dict)
    }


def _sign_in_reward(today):
    for value in SIGN_IN_RULES.values():
        date_value = str(value.get("Date", "") or "").replace("-", "/")
        if date_value:
            parts = date_value.split("/")
            if len(parts) == 3 and "/".join(str(int(part)) for part in parts) == today.replace("-", "/"):
                reward = value.get("Reward")
                if isinstance(reward, list) and len(reward) >= 2:
                    return [(int(reward[0]), int(reward[1]))]
    default = SIGN_IN_RULES.get("1", {})
    reward = default.get("Reward") if isinstance(default, dict) else None
    if isinstance(reward, list) and len(reward) >= 2:
        try:
            return [(int(reward[0]), int(reward[1]))]
        except (TypeError, ValueError):
            pass
    return []


def _rebuild_sign_in_quest_connection(connection, uid, sign_count, now):
    """Rebuild the seven-day sign quest from the authoritative monthly bitmask.

    Unlike normal quest progress, this trusted derived value may move backward
    when repairing a stale snapshot. The quest becomes a finish-list entry only
    after the seventh signed day.
    """
    progress = min(SIGN_IN_QUEST_TARGET, max(0, int(sign_count)))
    connection.execute(
        "INSERT INTO quest_progress(uid,quest_cid,fin_num,tgt_num,create_time) VALUES(?,?,?,?,?) "
        "ON CONFLICT(uid,quest_cid) DO UPDATE SET fin_num=excluded.fin_num,tgt_num=excluded.tgt_num",
        (uid, SIGN_IN_QUEST_ID, progress, SIGN_IN_QUEST_TARGET, now),
    )
    completed = progress >= SIGN_IN_QUEST_TARGET
    was_finished = connection.execute(
        "SELECT 1 FROM quest_lists WHERE uid=? AND list_name='finish' AND quest_cid=?",
        (uid, SIGN_IN_QUEST_ID),
    ).fetchone() is not None
    if completed:
        connection.execute(
            "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
            (uid, "finish", SIGN_IN_QUEST_ID),
        )
    else:
        connection.execute(
            "DELETE FROM quest_lists WHERE uid=? AND list_name='finish' AND quest_cid=?",
            (uid, SIGN_IN_QUEST_ID),
        )
    return {
        "cid": SIGN_IN_QUEST_ID,
        "finNum": progress,
        "tgtNum": SIGN_IN_QUEST_TARGET,
        "createTime": now,
        "completed": completed,
        "finishListChanged": completed != was_finished,
    }


def reconcile_sign_in_quest(uid):
    """Repair the seven-day quest from the current-month persisted bitmask."""
    if not uid:
        return None
    now = int(time.time())
    month = time.strftime("%Y-%m", time.localtime(now))
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT sign_info,sign_month FROM active_sign_in WHERE uid=?", (uid,)
        ).fetchone()
        if row is not None and row["sign_month"] == month:
            sign_info = int(row["sign_info"] or 0)
        else:
            sign_info = 0
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "signInfo", json.dumps(sign_info), now),
        )
        return _rebuild_sign_in_quest_connection(
            connection, uid, sign_info.bit_count(), now
        )


def record_sign_in(uid):
    """Sign today and update calendar, reward, JSON and seven-day quest atomically."""
    if not uid:
        return None
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    month = today[:7]
    day = int(today[8:10])
    day_bit = 1 << (day - 1)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT sign_date, sign_count, sign_info, sign_month FROM active_sign_in WHERE uid=?", (uid,)
        ).fetchone()
        if row is not None:
            old_info = int(row["sign_info"] or 0)
        else:
            state_row = connection.execute(
                "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='signInfo'",
                (uid,),
            ).fetchone()
            try:
                old_info = int(json.loads(state_row["value_json"])) if state_row is not None else 0
            except (TypeError, ValueError, json.JSONDecodeError):
                old_info = 0
        if row is None or row["sign_month"] != month:
            old_info = 0
        already_signed = bool(old_info & day_bit)
        new_info = old_info if already_signed else old_info | day_bit
        new_count = new_info.bit_count()
        reward_result = None
        if not already_signed:
            reward_result = _apply_reward_pairs_connection(
                connection, uid, _sign_in_reward(today), now
            )
        if row is None:
            connection.execute(
                "INSERT INTO active_sign_in(uid,sign_date,sign_count,sign_info,sign_month,updated_at) VALUES(?,?,?,?,?,?)",
                (uid, today if not already_signed else "", new_count, new_info, month, now),
            )
        else:
            connection.execute(
                "UPDATE active_sign_in SET sign_date=?, sign_count=?, sign_info=?, sign_month=?, updated_at=? WHERE uid=?",
                (today if not already_signed else row["sign_date"], new_count, new_info, month, now, uid),
            )
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "signInfo", json.dumps(new_info), now),
        )
        quest = _rebuild_sign_in_quest_connection(connection, uid, new_count, now)
    return {
        "sign_info": new_info,
        "sign_count": new_count,
        "rewards": reward_result["rewards"] if reward_result else [],
        "already_signed": already_signed,
        "quest": quest,
    }


# ── Maze instance persistence ──

def _ensure_maze_instances_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS maze_instances (
                uid TEXT NOT NULL,
                maze_cid INTEGER NOT NULL,
                formation_id INTEGER NOT NULL,
                random_seed INTEGER NOT NULL,
                save_data TEXT NOT NULL DEFAULT '',
                save_version INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(uid, maze_cid),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def create_maze_instance(uid, maze_cid, formation_id):
    if not uid:
        return None
    import random as _random
    seed = _random.randint(1, 0x7FFFFFFF)
    now = int(time.time())
    with connect() as connection:
        player = connection.execute(
            "SELECT 1 FROM players WHERE uid=?", (uid,)
        ).fetchone()
        if player is None:
            return None
        connection.execute(
            "INSERT OR REPLACE INTO maze_instances(uid,maze_cid,formation_id,random_seed,save_data,save_version,active,created_at) "
            "VALUES(?,?,?,?,?,?,1,?)",
            (uid, maze_cid, formation_id, seed, "", 0, now),
        )
    return {
        "id": maze_cid,
        "mazeCid": maze_cid,
        "randomSeed": seed,
        "isLocal": False,
        "carryItems": [],
        "saveData": "",
        "saveVersion": 0,
        "mazePlayer": {
            "first": True,
            "baseInfo": {
                "pid": "",
                "pName": "local",
                "pLv": 1,
                "exp": 0,
                "headIcon": 0,
                "leaderCid": 0,
                "vip": 0,
                "vipexp": 0,
                "serverId": "",
                "avatarFrame": 0,
            },
            "dolls": [],
            "completePathNodes": [],
            "finishMazes": [],
            "mainQuests": {},
            "openPathNodes": [],
            "items": {},
            "events": {},
            "alienEvents": [],
            "itemDropGetCnts": {},
            "finishQuests": [],
            "first": True,
            "fishSpecimens": [],
            "mazeRuneList": [],
            "currMazeBuffCids": [],
            "maxRuneLevel": 0,
            "activityPOD": {},
        },
    }


def save_maze_data(uid, maze_cid, save_data, save_version=0):
    if not uid:
        return False
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE maze_instances SET save_data=?, save_version=? WHERE uid=? AND maze_cid=? AND active=1",
            (save_data, save_version, uid, maze_cid),
        )
    return cursor.rowcount > 0


def delete_maze_instance(uid, maze_cid):
    if not uid:
        return False
    with connect() as connection:
        cursor = connection.execute(
            "DELETE FROM maze_instances WHERE uid=? AND maze_cid=?", (uid, maze_cid)
        )
    return cursor.rowcount > 0


# ── Player state JSON persistence ──
# Stores arbitrary PlayerPOD sub-fields that have no dedicated table.
# Key = field_name, value = JSON-encoded POD/map/list.

FIELD_BLACKLIST = {
    "baseInfo", "souls", "warehouse", "numAttrs",
    "lotteryShows", "lotteryRecords", "lotteryCnts",
    "quests", "finishQuestList", "failQuestList", "unlockChapterTasks",
    "remainderGiveGiftNum", "fondleNum", "nextRecoveryFondleTime",
    "newMailCount", "mails", "showCollectItems",
    "soulNewStorys",  # handled by soul_story_progress
    "dailyDups",  # fishingActivityPOD is overlaid into this typed list
    "fishing_activity",  # mapped into dailyDups[].fishingActivityPOD
    "soul_memory",  # internal state; explicitly mapped to soulMemoryChapters
}


def _ensure_player_state_json_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS player_state_json (
                uid TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(uid, field_name),
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )


def seed_player_state_json(uid, player_pod):
    """Seed all unhandled PlayerPOD fields from snapshot on first login."""
    if not uid or not isinstance(player_pod, dict):
        return 0
    # Only seed simple scalar fields (int, bool, list<int>, map<int|int>, string).
    # Complex PODs (formations, dresses, shops, etc.) require dedicated tables.
    # Seed all fields not in the blacklist. Complex PODs are stored as JSON
    # and survive roundtrip through _restore_numeric_keys + protocol_codec encoding.
    seedable = {
        name: value
        for name, value in player_pod.items()
        if name not in FIELD_BLACKLIST and value is not None
    }
    if not seedable:
        return 0
    now = int(time.time())
    inserted = 0
    with connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM accounts WHERE uid=?", (uid,)
        ).fetchone()
        if not exists:
            return 0
        for name, value in seedable.items():
            cursor = connection.execute(
                "INSERT OR IGNORE INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?)",
                (uid, name, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now),
            )
            inserted += cursor.rowcount
    return inserted


def get_player_state_json(uid, field_name):
    """Get a single JSON field for a player. Returns None if not found."""
    if not uid:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM player_state_json WHERE uid=? AND field_name=?",
            (uid, field_name),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def get_all_player_state_json(uid):
    """Get ALL JSON fields for a player as a dict with numeric keys restored."""
    if not uid:
        return {}
    with connect() as connection:
        rows = connection.execute(
            "SELECT field_name, value_json FROM player_state_json WHERE uid=? ORDER BY field_name",
            (uid,),
        ).fetchall()
    result = {}
    for row in rows:
        try:
            result[row["field_name"]] = _restore_numeric_keys(json.loads(row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return result


def update_player_state_json(uid, field_name, value):
    """Upsert a single JSON field."""
    if not uid:
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (uid, field_name, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now),
        )
    return True


def mark_mazes_complete(uid, maze_ids):
    """Mark configured maze instances complete without discarding existing progress."""
    normalized = sorted({int(value) for value in maze_ids if int(value) > 0})
    if not uid or not normalized or get_player(uid) is None:
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        finished = _state_json_connection(connection, uid, "finishMazes")
        if not isinstance(finished, list):
            row = connection.execute(
                "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='finishMazes'",
                (uid,),
            ).fetchone()
            try:
                finished = json.loads(row["value_json"]) if row else []
            except (TypeError, ValueError, json.JSONDecodeError):
                finished = []
        finished_set = {int(value) for value in finished if isinstance(value, int)}

        maze_info = _state_json_connection(connection, uid, "mazeInfoPOD")
        if not isinstance(maze_info, dict):
            maze_info = {}
        added = 0
        for maze_id in normalized:
            if maze_id not in finished_set:
                finished_set.add(maze_id)
                added += 1
            key = str(maze_id)
            existing = maze_info.get(key, maze_info.get(maze_id, {}))
            if not isinstance(existing, dict):
                existing = {}
            maze_info[key] = {
                **existing,
                "cid": maze_id,
                "star": max(3, int(existing.get("star", 0) or 0)),
                "score": max(100, int(existing.get("score", 0) or 0)),
                "starConditions": [True, True, True],
                "enterCount": max(0, int(existing.get("enterCount", 0) or 0)),
                "buyCount": max(0, int(existing.get("buyCount", 0) or 0)),
                "winCount": max(1, int(existing.get("winCount", 0) or 0)),
            }
            if maze_id in maze_info and maze_id != key:
                del maze_info[maze_id]

        for field_name, value in (
            ("finishMazes", sorted(finished_set)),
            ("mazeInfoPOD", maze_info),
        ):
            connection.execute(
                "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (
                    uid,
                    field_name,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
    return {"requested": len(normalized), "added": added, "total": len(finished_set)}


def _state_json_connection(connection, uid, field_name):
    row = connection.execute(
        "SELECT value_json FROM player_state_json WHERE uid=? AND field_name=?",
        (uid, field_name),
    ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _put_state_json_connection(connection, uid, field_name, value, now):
    connection.execute(
        "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
        (uid, field_name, json.dumps(value, ensure_ascii=False, separators=(",", ":")), now),
    )


def get_offline_wallet(uid):
    value = get_player_state_json(uid, "offline_wallet") or {}
    return {
        "balance": max(0, int(value.get("balance", 0) or 0)),
        "sumPay": max(0, int(value.get("sumPay", 0) or 0)),
        "orders": value.get("orders", {}) if isinstance(value.get("orders", {}), dict) else {},
    }


def credit_offline_wallet(uid, amount):
    """Explicitly top up the local virtual wallet; never contacts a payment channel."""
    if not uid or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        wallet = _state_json_connection(connection, uid, "offline_wallet")
        wallet["balance"] = max(0, int(wallet.get("balance", 0) or 0)) + amount
        wallet["sumPay"] = max(0, int(wallet.get("sumPay", 0) or 0))
        wallet.setdefault("orders", {})
        _put_state_json_connection(connection, uid, "offline_wallet", wallet, now)
    return get_offline_wallet(uid)


def trade_offline_payment(
    uid, amount, reward_list, order_key, *, mall_id=None, period=None, count=0,
    pending_notification=None,
):
    """Record a local-only purchase and grant it atomically.

    Offline mode has no payment channel and must not depend on a wallet top-up.
    ``amount`` is retained as the configured local price for accounting, while
    the order key makes client retries idempotent.  The virtual wallet balance
    is deliberately unchanged; this prevents a local purchase from pretending
    that an external charge took place.
    """
    if (
        not uid
        or not isinstance(amount, int)
        or isinstance(amount, bool)
        or amount <= 0
        or not isinstance(reward_list, list)
        or not isinstance(order_key, str)
        or not order_key
    ):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        wallet = _state_json_connection(connection, uid, "offline_wallet")
        wallet["balance"] = max(0, int(wallet.get("balance", 0) or 0))
        wallet["sumPay"] = max(0, int(wallet.get("sumPay", 0) or 0))
        orders = wallet.setdefault("orders", {})
        if order_key in orders:
            return {"duplicate": True, "claimed": False, "changed_attrs": {}, "changed_items": [], "rewards": []}
        applied = _apply_reward_pairs_connection(connection, uid, reward_list, now)
        wallet["sumPay"] += amount
        orders[order_key] = {"amount": amount, "time": now}
        _put_state_json_connection(connection, uid, "offline_wallet", wallet, now)
        if isinstance(pending_notification, dict):
            pending = _state_json_connection(connection, uid, "pending_tcp_notifications")
            if not isinstance(pending, dict):
                pending = {}
            notification = dict(pending_notification)
            notification["changedAttrs"] = {
                str(cid): int(quantity)
                for cid, quantity in applied.get("changed_attrs", {}).items()
            }
            notification["changedItems"] = list(applied.get("changed_items", []))
            notifications = pending.setdefault("notifications", [])
            notifications.append(notification)
            pending["notifications"] = notifications[-50:]
            _put_state_json_connection(connection, uid, "pending_tcp_notifications", pending, now)
        if mall_id is not None and period and int(count) > 0:
            mall = _state_json_connection(connection, uid, "mall")
            purchases = mall.setdefault("purchases", {})
            key = str(int(mall_id))
            current = purchases.get(key, {})
            previous = int(current.get("count", 0) or 0) if current.get("period") == period else 0
            purchases[key] = {
                "period": str(period),
                "count": previous + int(count),
                "updatedAt": now,
            }
            _put_state_json_connection(connection, uid, "mall", mall, now)
    applied["duplicate"] = False
    applied["claimed"] = True
    applied["paidAmount"] = amount
    return applied


def pop_pending_tcp_notifications(uid):
    """Atomically take HTTP-originated notifications for an online TCP session."""
    if not uid:
        return []
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = _state_json_connection(connection, uid, "pending_tcp_notifications")
        notifications = pending.get("notifications", []) if isinstance(pending, dict) else []
        notifications = [row for row in notifications if isinstance(row, dict)]
        if notifications:
            _put_state_json_connection(
                connection, uid, "pending_tcp_notifications", {"notifications": []}, now
            )
    return notifications


def _social_defaults():
    return {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}


def apply_friend_request(from_uid, target_uid):
    """Write a friend request to the target account in one local transaction."""
    if not from_uid or not target_uid or str(from_uid) == str(target_uid):
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sender = connection.execute("SELECT * FROM players WHERE uid=?", (from_uid,)).fetchone()
        target = connection.execute("SELECT * FROM players WHERE uid=?", (target_uid,)).fetchone()
        if sender is None or target is None:
            return False
        source_state = _state_json_connection(connection, from_uid, "social")
        target_state = _state_json_connection(connection, target_uid, "social")
        for state in (source_state, target_state):
            for key, default in _social_defaults().items():
                state.setdefault(key, default.copy())
        if str(target_uid) in source_state["blacklist"] or str(from_uid) in target_state["blacklist"]:
            return False
        if str(target_uid) in source_state["friends"]:
            return True
        target_state["pending"][str(from_uid)] = {
            "pId": str(from_uid), "pName": str(sender["role_name"]), "remark": "",
            "pLv": int(sender["level"]), "serverId": "offline-local", "time": now,
        }
        _put_state_json_connection(connection, target_uid, "social", target_state, now)
    return True


def handle_friend_application(target_uid, requester_uid, accepted):
    """Accept or reject a request and synchronize both local accounts."""
    if not target_uid or not requester_uid or str(target_uid) == str(requester_uid):
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (target_uid,)).fetchone() is None:
            return False
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (requester_uid,)).fetchone() is None:
            return False
        target_state = _state_json_connection(connection, target_uid, "social")
        requester_state = _state_json_connection(connection, requester_uid, "social")
        pending = target_state.setdefault("pending", {}).pop(str(requester_uid), None)
        if pending is None:
            return False
        if accepted:
            requester = connection.execute("SELECT role_name,level FROM players WHERE uid=?", (requester_uid,)).fetchone()
            target = connection.execute("SELECT role_name,level FROM players WHERE uid=?", (target_uid,)).fetchone()
            target_state.setdefault("friends", {})[str(requester_uid)] = {
                "pId": str(requester_uid), "pName": str(requester["role_name"]),
                "pLv": int(requester["level"]), "serverId": "offline-local", "remark": "",
            }
            requester_state.setdefault("friends", {})[str(target_uid)] = {
                "pId": str(target_uid), "pName": str(target["role_name"]),
                "pLv": int(target["level"]), "serverId": "offline-local", "remark": "",
            }
        _put_state_json_connection(connection, target_uid, "social", target_state, now)
        _put_state_json_connection(connection, requester_uid, "social", requester_state, now)
    return True


def remove_friend_pair(uid, friend_uid):
    if not uid or not friend_uid:
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return False
        state = _state_json_connection(connection, uid, "social")
        state.setdefault("friends", {}).pop(str(friend_uid), None)
        state.setdefault("remarks", {}).pop(str(friend_uid), None)
        _put_state_json_connection(connection, uid, "social", state, now)
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (friend_uid,)).fetchone() is not None:
            other = _state_json_connection(connection, friend_uid, "social")
            other.setdefault("friends", {}).pop(str(uid), None)
            other.setdefault("remarks", {}).pop(str(uid), None)
            _put_state_json_connection(connection, friend_uid, "social", other, now)
    return True


def _state_list(uid, field_name):
    value = get_player_state_json(uid, field_name)
    return value if isinstance(value, list) else []


def _state_id(value, *keys):
    if not isinstance(value, dict):
        return 0
    for key in keys:
        try:
            result = int(value.get(key, 0) or 0)
        except (TypeError, ValueError):
            result = 0
        if result:
            return result
    return 0


def _find_state_prefab(prefabs, prefab_id):
    return next(
        (item for item in prefabs if _state_id(item, "id", "prefabId") == prefab_id),
        None,
    )


def update_soul_prefab(uid, prefab_id, soul_id, skill_group_id, custom_skills, optional_skill):
    if not uid or not all(isinstance(value, int) for value in (prefab_id, soul_id, skill_group_id, optional_skill)):
        return None
    if prefab_id <= 0 or soul_id <= 0 or skill_group_id < 0 or optional_skill < 0:
        return None
    if not isinstance(custom_skills, list) or any(not isinstance(value, int) or value <= 0 for value in custom_skills):
        return None
    with connect() as connection:
        if connection.execute("SELECT 1 FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)).fetchone() is None:
            return None
    prefabs = _state_list(uid, "soulPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        prefab = {"id": prefab_id}
        prefabs.append(prefab)
    prefab.update({
        "id": prefab_id,
        "soulCid": soul_id,
        "skillGroupId": skill_group_id,
        "customSkills": list(custom_skills),
        "optionalSkill": optional_skill,
    })
    update_player_state_json(uid, "soulPrefabs", prefabs)
    return prefab


def update_soul_prefab_position(uid, prefab_id, position):
    if not uid or not isinstance(prefab_id, int) or not isinstance(position, int) or prefab_id <= 0 or position <= 0:
        return False
    prefabs = _state_list(uid, "soulPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        return False
    prefab["position"] = position
    formations = _state_list(uid, "formations")
    for formation in formations:
        mapping = formation.get("formation") if isinstance(formation, dict) else None
        if isinstance(mapping, dict) and prefab_id in mapping:
            mapping[prefab_id] = position
    update_player_state_json(uid, "soulPrefabs", prefabs)
    if formations:
        update_player_state_json(uid, "formations", formations)
    return True


def exchange_soul_prefab_equipment(uid, prefab_id, slot_a, slot_b):
    if not uid or not all(isinstance(value, int) for value in (prefab_id, slot_a, slot_b)) or slot_a <= 0 or slot_b <= 0:
        return False
    prefabs = _state_list(uid, "soulPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        return False
    equipment = prefab.setdefault("equipments", {})
    value_a = equipment.get(slot_a, equipment.get(str(slot_a), 0))
    value_b = equipment.get(slot_b, equipment.get(str(slot_b), 0))
    equipment.pop(str(slot_a), None)
    equipment.pop(str(slot_b), None)
    equipment[slot_a], equipment[slot_b] = value_b, value_a
    update_player_state_json(uid, "soulPrefabs", prefabs)
    return True


def _normalize_equipment_map(equipment_map):
    if not isinstance(equipment_map, dict):
        return None
    normalized = {}
    for slot, equip_id in equipment_map.items():
        try:
            slot, equip_id = int(slot), int(equip_id)
        except (TypeError, ValueError):
            return None
        if slot <= 0 or equip_id < 0:
            return None
        if equip_id:
            normalized[slot] = equip_id
    return normalized


def save_equipment_prefab(uid, prefab_id, equipment_map):
    if not uid or not isinstance(prefab_id, int) or prefab_id <= 0:
        return False
    normalized = _normalize_equipment_map(equipment_map)
    if normalized is None:
        return False
    equipment_ids = list(normalized.values())
    with connect() as connection:
        for equip_id in equipment_ids:
            if connection.execute(
                "SELECT 1 FROM equipment_instances WHERE uid=? AND id=?", (uid, equip_id)
            ).fetchone() is None:
                return False
    prefabs = _state_list(uid, "equipmentPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        prefab = {"id": prefab_id, "name": ""}
        prefabs.append(prefab)
    prefab["id"] = prefab_id
    prefab["equipmentMap"] = normalized
    update_player_state_json(uid, "equipmentPrefabs", prefabs)
    return True


def wear_equipment_prefab(uid, soul_prefab_id, equipment_prefab_id):
    if not uid or not isinstance(soul_prefab_id, int) or not isinstance(equipment_prefab_id, int):
        return False
    prefabs = _state_list(uid, "soulPrefabs")
    soul_prefab = _find_state_prefab(prefabs, soul_prefab_id)
    equipment_prefabs = _state_list(uid, "equipmentPrefabs")
    equipment_prefab = _find_state_prefab(equipment_prefabs, equipment_prefab_id)
    if soul_prefab is None or equipment_prefab is None:
        return False
    soul_prefab["equipments"] = dict(equipment_prefab.get("equipmentMap") or {})
    update_player_state_json(uid, "soulPrefabs", prefabs)
    return True


def cover_soul_prefab_equipment(uid, soul_prefab_id, equipment_map):
    if not uid or not isinstance(soul_prefab_id, int):
        return False
    normalized = _normalize_equipment_map(equipment_map)
    if normalized is None:
        return False
    prefabs = _state_list(uid, "soulPrefabs")
    prefab = _find_state_prefab(prefabs, soul_prefab_id)
    if prefab is None:
        return False
    with connect() as connection:
        for equip_id in normalized.values():
            if connection.execute("SELECT 1 FROM equipment_instances WHERE uid=? AND id=?", (uid, equip_id)).fetchone() is None:
                return False
    prefab["equipments"] = normalized
    update_player_state_json(uid, "soulPrefabs", prefabs)
    return True


def rename_equipment_prefab(uid, prefab_id, name):
    if not uid or not isinstance(prefab_id, int) or prefab_id <= 0 or not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) > 32:
        return False
    prefabs = _state_list(uid, "equipmentPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        return False
    prefab["name"] = name
    update_player_state_json(uid, "equipmentPrefabs", prefabs)
    return True


def set_soul_prefab_jewelry_speed(uid, prefab_id, jewelry_cid, speed):
    if not uid or not all(isinstance(value, int) for value in (prefab_id, jewelry_cid, speed)):
        return False
    if prefab_id <= 0 or jewelry_cid <= 0 or speed < 0:
        return False
    prefabs = _state_list(uid, "soulPrefabs")
    prefab = _find_state_prefab(prefabs, prefab_id)
    if prefab is None:
        return False
    prefab.setdefault("jewelrySpeeds", {})[jewelry_cid] = speed
    update_player_state_json(uid, "soulPrefabs", prefabs)
    return True


def _fishing_activity_defaults():
    return {
        "roleLevel": 1,
        "skillLevel": {},
        "actionLevel": {},
        "book": {},
        "maxWeight": {},
        "getStoryList": [],
        "autoFishingRewardsTime": 0,
        "totalWeight": 0,
        "pending": None,
    }


FISHING_ACTIVITY_CONFIG = {}


def configure_fishing_activity(config):
    """Install extracted fishing activity upgrade tables."""
    global FISHING_ACTIVITY_CONFIG
    FISHING_ACTIVITY_CONFIG = config if isinstance(config, dict) else {}


def _fishing_activity_state_connection(connection, uid):
    row = connection.execute(
        "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='fishing_activity'",
        (uid,),
    ).fetchone()
    state = {}
    if row is not None:
        try:
            stored = json.loads(row["value_json"])
            if isinstance(stored, dict):
                state = stored
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
    defaults = _fishing_activity_defaults()
    for key, value in defaults.items():
        if key not in state:
            state[key] = value.copy() if isinstance(value, (dict, list)) else value
    return state


def get_fishing_activity_state(uid):
    if not uid:
        return _fishing_activity_defaults()
    with connect() as connection:
        return _fishing_activity_state_connection(connection, uid)


def update_fishing_activity_state(uid, state):
    return update_player_state_json(uid, "fishing_activity", state)


def prepare_fishing_catch(uid, fish_pod):
    if not uid or not isinstance(fish_pod, dict):
        return False
    with connect() as connection:
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return False
        state = _fishing_activity_state_connection(connection, uid)
        state["pending"] = dict(fish_pod)
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "fishing_activity", json.dumps(state, ensure_ascii=False, separators=(",", ":")), int(time.time())),
        )
    return True


def confirm_fishing_catch(uid, caught):
    if not uid or not isinstance(caught, bool):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        state = _fishing_activity_state_connection(connection, uid)
        pending = state.get("pending")
        if not isinstance(pending, dict):
            return None
        state["pending"] = None
        if caught:
            fish_id = int(pending.get("fishId") or 0)
            num = max(1, int(pending.get("num") or 1))
            weight = max(0, int(pending.get("weight") or 0))
            book = state.setdefault("book", {})
            entry = book.setdefault(str(fish_id), {"fishId": fish_id, "num": 0, "weight": 0})
            entry["num"] = int(entry.get("num") or 0) + num
            entry["weight"] = max(int(entry.get("weight") or 0), weight)
            max_weight = state.setdefault("maxWeight", {})
            max_weight[str(fish_id)] = max(int(max_weight.get(str(fish_id)) or 0), weight)
            state["totalWeight"] = int(state.get("totalWeight") or 0) + weight
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "fishing_activity", json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    return state


def claim_fishing_story(uid, story_id, reward_pairs, unlock_parameter=0):
    if not uid or not isinstance(story_id, int) or story_id <= 0:
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        state = _fishing_activity_state_connection(connection, uid)
        stories = state.setdefault("getStoryList", [])
        if story_id in stories:
            return {
                "claimed": False,
                "rewards": [],
                "changed_attrs": {},
                "changed_items": [],
                "state": state,
            }
        if int(state.get("totalWeight") or 0) < max(0, int(unlock_parameter)):
            return None
        applied = _apply_reward_pairs_connection(connection, uid, reward_pairs or [], now)
        stories.append(story_id)
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "fishing_activity", json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    applied.update({"claimed": True, "state": state})
    return applied


def claim_fishing_auto(uid, now, interval, fish_count, fish_pods):
    if not uid or not isinstance(now, int) or not isinstance(interval, int) or interval <= 0:
        return None
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        state = _fishing_activity_state_connection(connection, uid)
        end_time = int(state.get("autoFishingRewardsTime") or 0)
        if end_time > now:
            return {"claimed": False, "rewards": [], "next_time": end_time, "total_weight": int(state.get("totalWeight") or 0)}
        state["autoFishingRewardsTime"] = now + interval
        book = state.setdefault("book", {})
        total_weight = 0
        rewards = []
        for pod in fish_pods or []:
            fish_id = int(pod.get("fishId") or 0)
            num = max(1, int(pod.get("num") or 1))
            weight = max(0, int(pod.get("weight") or 0))
            if fish_id <= 0:
                continue
            entry = book.setdefault(str(fish_id), {"fishId": fish_id, "num": 0, "weight": 0})
            entry["num"] = int(entry.get("num") or 0) + num
            entry["weight"] = max(int(entry.get("weight") or 0), weight)
            max_weight = state.setdefault("maxWeight", {})
            max_weight[str(fish_id)] = max(int(max_weight.get(str(fish_id)) or 0), weight)
            total_weight += weight
            rewards.append({"fishId": fish_id, "num": num, "weight": weight})
        state["totalWeight"] = int(state.get("totalWeight") or 0) + total_weight
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "fishing_activity", json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    return {"claimed": True, "rewards": rewards, "next_time": now + interval, "total_weight": state["totalWeight"]}


def upgrade_fishing_activity(uid, upgrade_type, target_id=None):
    """Upgrade one fishing activity value and spend its configured currency atomically.

    ``upgrade_type`` is one of ``role``, ``skill`` or ``action``.  The client
    sends skill/action root IDs; the level tables are resolved here so a
    malformed or stale client request cannot choose its own cost or level.
    """
    if not uid or upgrade_type not in {"role", "skill", "action"}:
        return None
    if upgrade_type != "role" and (not isinstance(target_id, int) or isinstance(target_id, bool) or target_id <= 0):
        return None

    config = FISHING_ACTIVITY_CONFIG
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None

        state = _fishing_activity_state_connection(connection, uid)
        cost_row = None
        current_level = 1
        next_level = 2
        state_key = None

        if upgrade_type == "role":
            state_key = "roleLevel"
            current_level = max(1, int(state.get(state_key) or 1))
            role_levels = config.get("roleLevels", {})
            control = config.get("control", {})
            max_level = int(control.get("maxLevel") or 0)
            rows = list(role_levels.values()) if isinstance(role_levels, dict) else []
            if max_level <= 0:
                max_level = max((int(row.get("level", 0)) for row in rows if isinstance(row, dict)), default=0)
            if max_level <= 0 or current_level >= max_level:
                return None
            cost_row = next(
                (row for row in rows if isinstance(row, dict) and int(row.get("level", 0)) == current_level),
                None,
            )
        elif upgrade_type == "skill":
            state_key = "skillLevel"
            roots = config.get("skills", {})
            root = roots.get(str(target_id)) if isinstance(roots, dict) else None
            if not isinstance(root, dict):
                return None
            levels = state.setdefault(state_key, {})
            current_level = max(1, int(levels.get(str(target_id), 1) or 1))
            group = root.get("skillGroup", [])
            max_level = int(root.get("levelMax") or len(group))
            if current_level >= max_level or current_level >= len(group):
                return None
            level_id = group[current_level - 1]
            cost_row = config.get("skillLevels", {}).get(str(level_id))
        else:
            state_key = "actionLevel"
            roots = config.get("actions", {})
            root = roots.get(str(target_id)) if isinstance(roots, dict) else None
            if not isinstance(root, dict):
                return None
            levels = state.setdefault(state_key, {})
            is_locked_root = bool(root.get("isUnlock"))
            default_level = 0 if is_locked_root else 1
            current_level = max(0, int(levels.get(str(target_id), default_level) or default_level))
            group = root.get("skillActionGroup", [])
            max_level = int(root.get("levelMax") or len(group))
            if current_level >= max_level or current_level >= len(group):
                return None
            if root.get("isUnlock"):
                requirements = root.get("needSkillId", [])
                for index in range(0, len(requirements) - 1, 2):
                    required_id = int(requirements[index])
                    required_level = int(requirements[index + 1])
                    if int(levels.get(str(required_id), 1) or 1) < required_level:
                        return None
            level_id = group[current_level if is_locked_root else current_level - 1]
            cost_row = config.get("actionLevels", {}).get(str(level_id))

        if not isinstance(cost_row, dict):
            return None
        next_level = current_level + 1
        costs = cost_row.get("cost", [])
        if not isinstance(costs, list) or not costs or not isinstance(costs[0], list) or len(costs[0]) < 2:
            return None
        cost_cid, cost_num = int(costs[0][0]), int(costs[0][1])
        if cost_cid <= 0 or cost_num <= 0:
            return None
        currency = connection.execute(
            "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?",
            (uid, cost_cid),
        ).fetchone()
        if currency is None or int(currency["quantity"]) < cost_num:
            return None

        remaining = int(currency["quantity"]) - cost_num
        connection.execute(
            "UPDATE player_num_attrs SET quantity=?, updated_at=? WHERE uid=? AND cid=?",
            (remaining, now, uid, cost_cid),
        )
        if upgrade_type == "role":
            state[state_key] = next_level
        else:
            state[state_key][str(target_id)] = next_level
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "fishing_activity", json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    return {
        "type": upgrade_type,
        "id": target_id,
        "level": next_level,
        "cost_cid": cost_cid,
        "cost_num": cost_num,
        "remaining": remaining,
        "changed_attrs": {cost_cid: remaining},
        "state": state,
    }


# ── Battle instance persistence ──

BATTLE_REWARDS = {
    # Default rewards by battle type (BattleType key)
    # Each entry: [(item_cid, quantity), ...]
    0: [(1, 100), (10006, 1)],     # normal/main story
    1: [(1, 50), (10006, 1)],      # daily dungeon
    2: [(1, 200), (2, 10)],        # event
    3: [(1, 300), (10006, 2)],     # boss
    4: [(1, 150), (10303, 1)],     # maze
    5: [(1, 80)],                   # material
    6: [(1, 500), (2, 30), (10006, 3)],  # challenge
}

BATTLE_MAZE_REWARDS = {}
BATTLE_DROP_LIBRARIES = {}
BATTLE_RULES = {
    "skills": {},
    "skillDetails": {},
    "skillFunctions": {},
    "buffs": {},
    "searchTargets": {},
    "buffGroupRelations": {},
}

# Names are taken from BuffConstant in global-metadata.dat.  Keeping the
# complete table here makes an unsupported effect observable without treating
# it as an arbitrary battle mutation.
BATTLE_EFFECT_NAMES = {
    101: "AddBuff",
    102: "AddSubBuff",
    103: "DispelBuff",
    104: "RandomAddBuff",
    105: "RandomAddSubBuff",
    106: "BuffResistance",
    107: "ChangeTime",
    108: "ChangeStack",
    109: "ChangeMaxStack",
    110: "BuffTime",
    111: "Service",
    201: "Drop",
    202: "ExDrop",
    203: "ChangeAttributes",
    204: "ItemGetNumAdd",
    205: "ItemGetNumPercentAdd",
    206: "MazeRevive",
    207: "AddItem",
    208: "ConvertItem",
    209: "DurableCostChange",
    210: "GatherNumChange",
    211: "GatherProbabilityChange",
    212: "GatherCritChange",
    213: "RuneStrengthCostChange",
    214: "AbyssScoreChange",
    215: "ChangeHpRate",
    216: "ForceFightMode",
    217: "ChooseRuneNumChange",
    218: "RuneConvertToScore",
    219: "AddDollBattleBuff",
    220: "AddMonsterBattleBuff",
    221: "DollRevive",
    222: "AddSelfTroopBattleBuff",
    223: "AddEnemyTroopBattleBuff",
    224: "RuneShopPriceChange",
    225: "AddAbyssScore",
    226: "ChangeEnergyRate",
    227: "ChangeEnergyMax",
    228: "ChangeQTEButtonCount",
    229: "ChangeQTERetryCount",
    230: "ChangeQTETime",
    231: "ChangeElementStatusTime",
    232: "ChangeHPChangeMaxLimit",
    233: "ChangeDollHP",
    234: "ChangeClockRetryCount",
    235: "ChangeClockSpeed",
    236: "DeleteClockItem",
    237: "ChangeClockItemArea",
    238: "AddRandomDollBattleBuff",
    239: "AddRandomMonsterBattleBuff",
    240: "NoTriggerElementType",
    241: "ChangeRuneRemakePrice",
    242: "ChangeRuneShopRefreshPrice",
    301: "ChangeBattleAttributes",
    302: "ChangeBattleStatus",
    303: "BattleChangeHP",
    304: "BattleChangeSkillEnergy",
    305: "BattleCastSkill",
    306: "Shield",
    307: "ImmediatelyDead",
    308: "ImmuneStatus",
    309: "BattleAccumulateChangeHP",
    310: "AddBuffByFunction",
    311: "AddSubBuffByFunction",
    312: "AddTmpSubSkill",
    313: "ChangeSkillCD",
    314: "ChangeWeakType",
    315: "ChangeWeakMaxNum",
    316: "AbsorbDmg",
    317: "ChangeWeakNum",
    318: "ShareDmg",
    319: "ChangeWeakStatus",
    320: "ChangeSkillEnergyCost",
    321: "ChangeSkillEnergyMax",
    322: "ChangeSkillRatio",
    323: "ChangeSkillRatioPercent",
    324: "ChangeSkillElement",
    325: "ReplaceSkill",
    326: "Plot",
    327: "ChangeSkillRatioAddition",
    # These values occur in the shipped CfgBuff tables but have no named
    # BuffConstant entry in this client metadata generation.  Preserve them
    # as explicit legacy records instead of silently labeling them unknown.
    245: "LegacyEffectType245",
    246: "LegacyEffectType246",
    248: "LegacyEffectType248",
    249: "LegacyEffectType249",
    251: "LegacyEffectType251",
    270: "LegacyEffectType270",
    271: "LegacyEffectType271",
    272: "LegacyEffectType272",
}


def configure_battle_rewards(maze_instances, drop_libraries=None):
    """Install extracted per-maze reward data without coupling storage to tcp_server."""
    global BATTLE_MAZE_REWARDS, BATTLE_DROP_LIBRARIES
    if not isinstance(maze_instances, dict):
        BATTLE_MAZE_REWARDS = {}
    else:
        BATTLE_MAZE_REWARDS = {
            str(maze_id): config
            for maze_id, config in maze_instances.items()
            if isinstance(config, dict)
        }
    BATTLE_DROP_LIBRARIES = {
        str(drop_id): config
        for drop_id, config in (drop_libraries or {}).items()
        if isinstance(config, dict)
    }


def configure_battle_rules(
    skills=None,
    skill_details=None,
    skill_functions=None,
    buffs=None,
    search_targets=None,
    buff_group_relations=None,
):
    """Install decompiled skill/effect data used by the local battle evaluator."""
    global BATTLE_RULES
    values = {
        "skills": skills,
        "skillDetails": skill_details,
        "skillFunctions": skill_functions,
        "buffs": buffs,
        "searchTargets": search_targets,
        "buffGroupRelations": buff_group_relations,
    }
    BATTLE_RULES = {
        key: {
            str(item_id): value
            for item_id, value in (value or {}).items()
            if isinstance(value, dict)
        }
        for key, value in values.items()
    }


def _maze_finished_in_connection(connection, uid, maze_id):
    row = connection.execute(
        "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='finishMazes'",
        (uid,),
    ).fetchone()
    if row is None:
        return False
    try:
        finished = json.loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(finished, list) and maze_id in finished


def _config_number(config, field):
    values = config.get(field, []) if isinstance(config, dict) else []
    if not isinstance(values, list):
        return 0
    return next((int(value) for value in values if isinstance(value, (int, float)) and value > 0), 0)


def _expand_drop_library(drop_id, rng, _ancestors=None, _multiplier=1, _depth=0):
    """Resolve a DropLibMaze row to final item CIDs only."""
    try:
        drop_id = int(drop_id)
        multiplier = int(_multiplier)
    except (TypeError, ValueError):
        return []
    if drop_id <= 0 or multiplier <= 0 or _depth >= 32:
        return []
    ancestors = frozenset(_ancestors or ())
    if drop_id in ancestors:
        return []
    config = BATTLE_DROP_LIBRARIES.get(str(drop_id))
    if not isinstance(config, dict):
        return []

    ids = config.get("randomIds", [])
    types = config.get("randomTypes", [])
    weights = config.get("weights", [])
    counts = config.get("randomCounts", [])
    if not isinstance(ids, list) or not ids:
        return []
    valid = []
    for index, raw_id in enumerate(ids):
        try:
            candidate_id = int(raw_id)
            candidate_type = int(types[index]) if index < len(types) else 0
            weight = int(weights[index]) if index < len(weights) else 1
            quantity = int(counts[index]) if index < len(counts) else 1
        except (TypeError, ValueError):
            continue
        if candidate_id > 0 and candidate_type in (1, 2) and weight > 0 and quantity > 0:
            valid.append((index, candidate_id, candidate_type, weight, quantity))
    if not valid:
        return []

    selected = []
    next_ancestors = ancestors | {drop_id}
    try:
        loop_count = max(0, int(config.get("loopCount", 1)))
    except (TypeError, ValueError):
        loop_count = 0
    for _ in range(loop_count):
        total = sum(row[3] for row in valid)
        pick = rng.randint(1, total)
        chosen = valid[-1]
        cursor = 0
        for row in valid:
            cursor += row[3]
            if pick <= cursor:
                chosen = row
                break
        _index, chosen_id, chosen_type, _weight, quantity = chosen
        final_quantity = multiplier * quantity
        if chosen_type == 1:
            selected.extend(_expand_drop_library(
                chosen_id, rng, next_ancestors, final_quantity, _depth + 1,
            ))
        elif chosen_type == 2:
            selected.append((chosen_id, final_quantity))

    aggregated = {}
    for item_id, quantity in selected:
        aggregated[item_id] = aggregated.get(item_id, 0) + quantity
    return sorted(aggregated.items())


def _maze_reward_pairs(config, seed=0, count=1, include_first=False):
    if not isinstance(config, dict) or count <= 0:
        return []
    import random as _random
    rng = _random.Random(int(seed) if seed else 1)
    result = []
    fixed = config.get("rewardShow", [])
    random_show = [int(value) for value in config.get("randomRewardShow", []) if isinstance(value, int) and value > 0]
    first = config.get("firstRewards", []) if include_first else []
    for _ in range(count):
        for pair in fixed:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and int(pair[0]) > 0 and int(pair[1]) > 0:
                result.append((int(pair[0]), int(pair[1])))
        if random_show:
            selected = random_show[rng.randrange(len(random_show))]
            result.extend(_expand_drop_library(selected, rng))
        for pair in first:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and int(pair[0]) > 0 and int(pair[1]) > 0:
                result.append((int(pair[0]), int(pair[1])))
    return result


def _battle_reward_list(connection, battle, result=1):
    if not battle or result != 1:
        return []
    try:
        explicit = json.loads(battle.get("reward_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        explicit = []
    if isinstance(explicit, list) and explicit:
        return [
            (int(pair[0]), int(pair[1]))
            for pair in explicit
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
            and isinstance(pair[0], int) and isinstance(pair[1], int)
            and pair[0] > 0 and pair[1] > 0
        ]
    maze_id = int(battle.get("map_id") or 0)
    maze_config = BATTLE_MAZE_REWARDS.get(str(maze_id))
    if not maze_config:
        return list(BATTLE_REWARDS.get(battle.get("battle_type"), BATTLE_REWARDS[0]))

    include_first = not _maze_finished_in_connection(connection, battle.get("uid"), maze_id)
    return _maze_reward_pairs(maze_config, battle.get("random_seed", 0), 1, include_first)


def _apply_reward_pairs_connection(connection, uid, reward_list, now):
    changed_attrs = {}
    changed_items = {}
    item_shows = []
    for cid, quantity in reward_list:
        if not isinstance(cid, int) or not isinstance(quantity, int) or cid <= 0 or quantity <= 0:
            continue
        item_shows.append({"cid": cid, "num": quantity, "tag": 0})
        if _resource_kind(connection, uid, cid) == "attr":
            attr = connection.execute(
                "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?", (uid, cid)
            ).fetchone()
            total = int(attr["quantity"]) + quantity if attr is not None else quantity
            connection.execute(
                "INSERT INTO player_num_attrs(uid,cid,quantity,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(uid,cid) DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at",
                (uid, cid, total, now),
            )
            changed_attrs[cid] = total
            continue
        item = connection.execute(
            "SELECT id,quantity,created_at FROM items WHERE uid=? AND template_id=?",
            (uid, cid),
        ).fetchone()
        if item is None:
            cursor = connection.execute(
                "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                (uid, cid, quantity, now),
            )
            item_id, total, created_at = cursor.lastrowid, quantity, now
        else:
            total = item["quantity"] + quantity
            connection.execute("UPDATE items SET quantity=? WHERE id=?", (total, item["id"]))
            item_id, created_at = item["id"], item["created_at"]
        changed_items[cid] = {
            "id": item_id, "cid": cid, "num": total,
            "usedNum": 0, "createTime": created_at,
        }
    return {
        "rewards": item_shows,
        "changed_attrs": changed_attrs,
        "changed_items": list(changed_items.values()),
    }


def _consume_item_pairs_connection(connection, uid, cost_pairs, now):
    """Consume a validated list of item/num-attribute pairs atomically."""
    totals = {}
    for cid, quantity in cost_pairs:
        if not isinstance(cid, int) or not isinstance(quantity, int) or cid <= 0 or quantity <= 0:
            return False
        totals[cid] = totals.get(cid, 0) + quantity
    available = {}
    for cid, quantity in totals.items():
        if _resource_kind(connection, uid, cid) == "attr":
            attr = connection.execute(
                "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?", (uid, cid)
            ).fetchone()
            if attr is None:
                return False
            available[cid] = ("attr", None, attr["quantity"])
            continue
        item = connection.execute(
            "SELECT id, quantity FROM items WHERE uid=? AND template_id=?", (uid, cid)
        ).fetchone()
        if item is None:
            return False
        available[cid] = ("item", item["id"], item["quantity"])
    if any(available[cid][2] < quantity for cid, quantity in totals.items()):
        return False
    for cid, quantity in totals.items():
        kind, row_id, current = available[cid]
        if kind == "attr":
            connection.execute(
                "UPDATE player_num_attrs SET quantity=?,updated_at=? WHERE uid=? AND cid=?",
                (current - quantity, now, uid, cid),
            )
        elif current == quantity:
            connection.execute("DELETE FROM items WHERE id=?", (row_id,))
        else:
            connection.execute("UPDATE items SET quantity=? WHERE id=?", (current - quantity, row_id))
    return True


def grant_reward_pairs(uid, reward_list):
    """Apply item/currency rewards atomically for a registered player."""
    if not uid or not isinstance(reward_list, list):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        return _apply_reward_pairs_connection(connection, uid, reward_list, now)


def claim_reward_once(uid, state_field, claim_key, reward_list):
    """Atomically apply a reward and record its claim key in player JSON state."""
    if not uid or not isinstance(state_field, str) or not state_field:
        return None
    if not isinstance(claim_key, str) or not claim_key or not isinstance(reward_list, list):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        row = connection.execute(
            "SELECT value_json FROM player_state_json WHERE uid=? AND field_name=?",
            (uid, state_field),
        ).fetchone()
        state = {}
        if row is not None:
            try:
                stored = json.loads(row["value_json"])
                if isinstance(stored, dict):
                    state = stored
            except (TypeError, ValueError, json.JSONDecodeError):
                state = {}
        claimed = state.setdefault("claimed", [])
        if claim_key in claimed:
            return {"claimed": False, "rewards": [], "changed_attrs": {}, "changed_items": []}
        applied = _apply_reward_pairs_connection(connection, uid, reward_list, now)
        claimed.append(claim_key)
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, state_field, json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    applied["claimed"] = True
    return applied


def migrate_claimed_reward_state(uid, state_field, migration_key, reward_map):
    """Backfill rewards for claims recorded by an older reward implementation.

    The migration is transactionally marked in the same state document as the
    original claims, so reconnects cannot apply the compensation twice.
    """
    if not uid or not isinstance(state_field, str) or not state_field:
        return None
    if not isinstance(migration_key, str) or not migration_key:
        return None
    if not isinstance(reward_map, dict):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        row = connection.execute(
            "SELECT value_json FROM player_state_json WHERE uid=? AND field_name=?",
            (uid, state_field),
        ).fetchone()
        state = {}
        if row is not None:
            try:
                stored = json.loads(row["value_json"])
                if isinstance(stored, dict):
                    state = stored
            except (TypeError, ValueError, json.JSONDecodeError):
                state = {}

        migrations = state.get("migrations", [])
        if not isinstance(migrations, list):
            migrations = []
        if migration_key in migrations:
            return {"migrated": False, "migrated_claims": [], "rewards": [], "changed_attrs": {}, "changed_items": []}

        claimed = state.get("claimed", [])
        if not isinstance(claimed, list):
            claimed = []
        claimed_keys = {str(value) for value in claimed}
        migrated_claims = []
        reward_list = []
        for reward_id in sorted(reward_map):
            if str(reward_id) not in claimed_keys:
                continue
            rewards = reward_map[reward_id]
            if not isinstance(rewards, list):
                continue
            migrated_claims.append(int(reward_id))
            reward_list.extend(rewards)

        applied = _apply_reward_pairs_connection(connection, uid, reward_list, now)
        migrations.append(migration_key)
        state["migrations"] = migrations
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, state_field, json.dumps(state, ensure_ascii=False, separators=(",", ":")), now),
        )
    applied["migrated"] = bool(migrated_claims)
    applied["migrated_claims"] = migrated_claims
    return applied


def _consume_reward_pairs_connection(connection, uid, cost_list, now):
    """Consume attributes/items in one transaction and report changed PODs."""
    totals = {}
    for cid, quantity in cost_list or []:
        if not isinstance(cid, int) or not isinstance(quantity, int) or cid <= 0 or quantity <= 0:
            return None
        totals[cid] = totals.get(cid, 0) + quantity

    changed_attrs = {}
    changed_items = {}
    for cid, quantity in totals.items():
        if _resource_kind(connection, uid, cid) == "attr":
            attr = connection.execute(
                "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?", (uid, cid)
            ).fetchone()
            if attr is None:
                return None
            remaining = int(attr["quantity"]) - quantity
            if remaining < 0:
                return None
            connection.execute(
                "UPDATE player_num_attrs SET quantity=?,updated_at=? WHERE uid=? AND cid=?",
                (remaining, now, uid, cid),
            )
            changed_attrs[cid] = remaining
            continue

        item = connection.execute(
            "SELECT id,quantity,created_at FROM items WHERE uid=? AND template_id=?",
            (uid, cid),
        ).fetchone()
        if item is None or int(item["quantity"]) < quantity:
            return None
        remaining = int(item["quantity"]) - quantity
        if remaining:
            connection.execute("UPDATE items SET quantity=? WHERE id=?", (remaining, item["id"]))
        else:
            connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
        changed_items[cid] = {
            "id": int(item["id"]),
            "cid": cid,
            "num": remaining,
            "usedNum": 0,
            "createTime": int(item["created_at"]),
        }
    return {"changed_attrs": changed_attrs, "changed_items": list(changed_items.values())}


def trade_reward_pairs(uid, cost_list, reward_list):
    """Atomically consume costs and grant rewards for a local gameplay action."""
    if not uid or not isinstance(cost_list, list) or not isinstance(reward_list, list):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        consumed = _consume_reward_pairs_connection(connection, uid, cost_list, now)
        if consumed is None:
            connection.rollback()
            return None
        granted = _apply_reward_pairs_connection(connection, uid, reward_list, now)
        consumed["changed_attrs"].update(granted["changed_attrs"])
        consumed["changed_items"].extend(granted["changed_items"])
        consumed["rewards"] = granted["rewards"]
        return consumed


def trade_reward_pairs_with_state(uid, cost_list, reward_list, state_updates):
    """Apply inventory changes and JSON state updates in one SQLite transaction."""
    if (
        not uid
        or not isinstance(cost_list, list)
        or not isinstance(reward_list, list)
        or not isinstance(state_updates, dict)
        or not state_updates
    ):
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            connection.rollback()
            return None
        consumed = _consume_reward_pairs_connection(connection, uid, cost_list, now)
        if consumed is None:
            connection.rollback()
            return None
        granted = _apply_reward_pairs_connection(connection, uid, reward_list, now)
        for field_name, value in state_updates.items():
            if not isinstance(field_name, str) or not field_name:
                connection.rollback()
                return None
            _put_state_json_connection(connection, uid, field_name, value, now)
        consumed["changed_attrs"].update(granted["changed_attrs"])
        consumed["changed_items"].extend(granted["changed_items"])
        consumed["rewards"] = granted["rewards"]
        return consumed


def commit_soul_growth(uid, soul_id, progress_state, cost_list, level=None):
    """Atomically consume growth costs and persist soul progress.

    Soul growth has two independent persistence targets: the JSON progress
    document and the normalized soul row.  Keep both changes in the same
    transaction so a failed state write cannot leave the player short of
    materials.
    """
    if (
        not uid
        or not isinstance(soul_id, int)
        or soul_id <= 0
        or not isinstance(progress_state, dict)
        or not isinstance(cost_list, list)
    ):
        return None
    normalized_level = None
    if level is not None:
        try:
            normalized_level = max(1, int(level))
        except (TypeError, ValueError):
            return None
    try:
        serialized_state = json.dumps(
            progress_state, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return None

    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM players WHERE uid=?", (uid,)
        ).fetchone() is None:
            connection.rollback()
            return None
        if connection.execute(
            "SELECT 1 FROM souls WHERE uid=? AND soul_id=?", (uid, soul_id)
        ).fetchone() is None:
            connection.rollback()
            return None
        consumed = _consume_reward_pairs_connection(connection, uid, cost_list, now)
        if consumed is None:
            connection.rollback()
            return None
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(uid,field_name) DO UPDATE SET "
            "value_json=excluded.value_json,updated_at=excluded.updated_at",
            (uid, "soul_progress", serialized_state, now),
        )
        if normalized_level is not None:
            updated = connection.execute(
                "UPDATE souls SET level=? WHERE uid=? AND soul_id=?",
                (normalized_level, uid, soul_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
        return consumed


def _exchange_pairs(value):
    """Decode one extracted exchange item list into (cid, quantity) pairs."""
    if not isinstance(value, list):
        return []
    pairs = []
    if any(isinstance(item, list) for item in value):
        for item in value:
            pairs.extend(_exchange_pairs(item))
        return pairs
    for index in range(0, len(value) - 1, 2):
        try:
            cid, quantity = int(value[index]), int(value[index + 1])
        except (TypeError, ValueError):
            continue
        if cid > 0 and quantity > 0:
            pairs.append((cid, quantity))
    return pairs


def _exchange_slots(value):
    """Return the per-use rows in CostItems/GetItems from the local table."""
    if not isinstance(value, list) or not value:
        return []
    if not any(isinstance(item, list) for item in value):
        return [_exchange_pairs(value)]
    return [_exchange_pairs(item) for item in value]


def _exchange_period(reset_type, now):
    """Map the extracted reset type to the game's local-day key.

    The shipped tables only expose reset type 1.  The client uses the same
    04:00 boundary as the other daily systems, so a request just before and
    after reset cannot share a limit bucket.
    """
    try:
        reset_type = int(reset_type or 0)
    except (TypeError, ValueError):
        return None
    if reset_type == 0:
        return "lifetime"
    if reset_type == 1:
        local_seconds = int(now) - 4 * 60 * 60
        return time.strftime("day:%Y-%m-%d", time.localtime(local_seconds))
    return None


def _exchange_multiple(row, uid, exchange_id, period, use_index):
    weights = row.get("CritWeights")
    multiples = row.get("CritMultiples")
    if weights in (None, []) and multiples in (None, []):
        return 1
    if not isinstance(weights, list) or not isinstance(multiples, list):
        return None
    if len(weights) != len(multiples) or not weights:
        return None
    choices = []
    for weight, multiple in zip(weights, multiples):
        try:
            weight, multiple = int(weight), int(multiple)
        except (TypeError, ValueError):
            return None
        if weight < 0 or multiple <= 0:
            return None
        choices.append((weight, multiple))
    total_weight = sum(weight for weight, _multiple in choices)
    if total_weight <= 0:
        return None
    seed_material = "%s:%s:%s:%s" % (uid, exchange_id, period, use_index)
    seed = int.from_bytes(hashlib.sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    roll = random.Random(seed).randrange(total_weight)
    for weight, multiple in choices:
        if roll < weight:
            return multiple
        roll -= weight
    return choices[-1][1]


def _exchange_plan(state, uid, exchange_id, count, row, now):
    """Validate one exchange and build its costs/rewards without mutating DB."""
    try:
        exchange_id = int(exchange_id)
        count = int(count)
    except (TypeError, ValueError):
        return None
    if exchange_id <= 0 or count <= 0 or count > 99 or not isinstance(row, dict):
        return None
    period = _exchange_period(row.get("ResetType"), now)
    if period is None:
        return None
    records = state.setdefault("records", {})
    record = records.get(str(exchange_id), {})
    if not isinstance(record, dict) or record.get("period") != period:
        used = 0
    else:
        try:
            used = max(0, int(record.get("count", 0)))
        except (TypeError, ValueError):
            used = 0
    try:
        limit = max(0, int(row.get("Limit", 0) or 0))
    except (TypeError, ValueError):
        return None
    if limit and used + count > limit:
        return None

    cost_slots = _exchange_slots(row.get("CostItems"))
    reward_slots = _exchange_slots(row.get("GetItems"))
    if not cost_slots or not reward_slots:
        return None
    bulk = bool(row.get("Bulk")) or len(cost_slots) == 1
    costs, rewards, crit_multiples = [], [], []
    for offset in range(count):
        use_index = used + offset
        slot = 0 if bulk else use_index
        if slot >= len(cost_slots) or slot >= len(reward_slots):
            return None
        slot_costs, slot_rewards = cost_slots[slot], reward_slots[slot]
        if not slot_costs or not slot_rewards:
            return None
        multiple = _exchange_multiple(row, uid, exchange_id, period, use_index)
        if multiple is None:
            return None
        costs.extend(slot_costs)
        rewards.extend((cid, quantity * multiple) for cid, quantity in slot_rewards)
        crit_multiples.append(multiple)
    next_record = {"period": period, "count": used + count, "updatedAt": int(now)}
    return {
        "costs": costs,
        "rewards": rewards,
        "record": next_record,
        "critMultiples": crit_multiples,
        "count": count,
    }


def apply_exchange(uid, exchange_id, count, row):
    """Apply an extracted ExchangeTable row atomically and enforce its limit."""
    return apply_exchange_batch(uid, {int(exchange_id): int(count)}, {int(exchange_id): row})


def apply_exchange_batch(uid, exchange_map, rows):
    """Apply several ExchangeTable rows as one transaction.

    Costs, rewards and the per-reset usage counters commit together.  This is
    important for the batch packet: a later invalid row must not leave earlier
    rows consumed.
    """
    if not uid or not isinstance(exchange_map, dict) or not isinstance(rows, dict):
        return None
    normalized = {}
    for exchange_id, count in exchange_map.items():
        try:
            exchange_id, count = int(exchange_id), int(count)
        except (TypeError, ValueError):
            return None
        if exchange_id <= 0 or count <= 0 or count > 99:
            return None
        normalized[exchange_id] = count
    if not normalized:
        return None

    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        state = _state_json_connection(connection, uid, "economy_exchange")
        state.setdefault("records", {})
        costs, rewards, success, crit_multiples = [], [], {}, {}
        updates = {}
        for exchange_id, count in normalized.items():
            plan = _exchange_plan(state, uid, exchange_id, count, rows.get(exchange_id), now)
            if plan is None:
                return None
            costs.extend(plan["costs"])
            rewards.extend(plan["rewards"])
            success[exchange_id] = count
            crit_multiples[exchange_id] = plan["critMultiples"]
            updates[str(exchange_id)] = plan["record"]
        consumed = _consume_reward_pairs_connection(connection, uid, costs, now)
        if consumed is None:
            return None
        aggregated_rewards = {}
        for cid, quantity in rewards:
            aggregated_rewards[cid] = aggregated_rewards.get(cid, 0) + quantity
        granted = _apply_reward_pairs_connection(
            connection,
            uid,
            list(aggregated_rewards.items()),
            now,
        )
        consumed.update(granted)
        state["records"].update(updates)
        _put_state_json_connection(connection, uid, "economy_exchange", state, now)
        consumed["success"] = success
        consumed["critMultiples"] = crit_multiples
        return consumed


def give_up_quest(uid, quest_id):
    """Move a quest to the failed list without creating a reward record."""
    if not uid or not isinstance(quest_id, int) or quest_id <= 0:
        return False
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT 1 FROM quest_lists WHERE uid=? AND list_name='active' AND quest_cid=?",
            (uid, quest_id),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT 1 FROM quest_progress WHERE uid=? AND quest_cid=?",
                (uid, quest_id),
            ).fetchone()
        if row is None:
            # A client may retry after the server already moved the quest.
            return connection.execute(
                "SELECT 1 FROM quest_lists WHERE uid=? AND list_name='fail' AND quest_cid=?",
                (uid, quest_id),
            ).fetchone() is not None
        connection.execute(
            "DELETE FROM quest_lists WHERE uid=? AND list_name='active' AND quest_cid=?",
            (uid, quest_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
            (uid, "fail", quest_id),
        )
    return True


def update_quest_progress(uid, quest_id, progress):
    """Persist monotonic quest progress and promote completed quests atomically."""
    if (
        not uid
        or not isinstance(quest_id, int)
        or quest_id <= 0
        or not isinstance(progress, int)
        or progress < 0
    ):
        return None
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT fin_num,tgt_num FROM quest_progress WHERE uid=? AND quest_cid=?",
            (uid, quest_id),
        ).fetchone()
        if row is None:
            return None
        target = max(0, int(row["tgt_num"]))
        updated = min(target, max(int(row["fin_num"]), int(progress)))
        connection.execute(
            "UPDATE quest_progress SET fin_num=? WHERE uid=? AND quest_cid=?",
            (updated, uid, quest_id),
        )
        completed = updated >= target and target > 0
        if completed:
            connection.execute(
                "DELETE FROM quest_lists WHERE uid=? AND list_name='active' AND quest_cid=?",
                (uid, quest_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO quest_lists(uid,list_name,quest_cid) VALUES(?,?,?)",
                (uid, "finish", quest_id),
            )
    return {"quest_id": quest_id, "fin_num": updated, "tgt_num": target, "completed": completed}


def settle_maze_mop_up(uid, maze_cid, count=1, seed=0):
    """Sweep a previously completed maze and grant its configured rewards."""
    if not uid or not isinstance(maze_cid, int) or maze_cid <= 0 or not isinstance(count, int) or not 1 <= count <= 99:
        return None
    config = BATTLE_MAZE_REWARDS.get(str(maze_cid))
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone() is None:
            return None
        if not _maze_finished_in_connection(connection, uid, maze_cid):
            return None
        if config:
            run_pairs = [
                _maze_reward_pairs(config, (seed or maze_cid) + index, 1, False)
                for index in range(count)
            ]
            pairs = [pair for run in run_pairs for pair in run]
            money_per_run = _config_number(config, "money")
            exp_per_run = _config_number(config, "playerExp")
            money = money_per_run * count
            player_exp = exp_per_run * count
        else:
            base_pairs = list(BATTLE_REWARDS.get(4, BATTLE_REWARDS[0]))
            run_pairs = [base_pairs for _ in range(count)]
            pairs = [pair for run in run_pairs for pair in run]
            money_per_run = sum(quantity for cid, quantity in pairs if cid == 1) // count
            exp_per_run = 50
            money = money_per_run * count
            player_exp = exp_per_run * count
        applied = _apply_reward_pairs_connection(connection, uid, pairs, now)
    applied.update({
        "money": money,
        "player_exp": player_exp,
        "count": count,
        "money_per_run": money_per_run,
        "player_exp_per_run": exp_per_run,
        "run_rewards": [
            [{"cid": cid, "num": quantity, "tag": 0} for cid, quantity in run]
            for run in run_pairs
        ],
    })
    return applied


def _ensure_battle_instances_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS battle_instances (
                id TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                battle_type INTEGER NOT NULL,
                map_id INTEGER NOT NULL,
                monster_team_id INTEGER NOT NULL,
                result INTEGER,
                settled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                settled_at INTEGER,
                random_seed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                rounds INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL DEFAULT '{}',
                server_snapshot_json TEXT NOT NULL DEFAULT '{}',
                reward_json TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(battle_instances)")
        }
        for name, definition in (
            ("random_seed", "INTEGER NOT NULL DEFAULT 0"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("rounds", "INTEGER NOT NULL DEFAULT 0"),
            ("report_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("server_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("reward_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE battle_instances ADD COLUMN {name} {definition}"
                )
        connection.execute(
            "UPDATE battle_instances SET random_seed=abs(random()) % 2147483646 + 1 "
            "WHERE random_seed=0"
        )
        connection.execute(
            "UPDATE battle_instances SET status=CASE "
            "WHEN settled=0 THEN 'active' WHEN result=1 THEN 'won' "
            "WHEN result IS NULL THEN 'abandoned' ELSE 'lost' END "
            "WHERE status IS NULL OR status='' OR (status='active' AND settled=1)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_battle_instances_active "
            "ON battle_instances(uid,settled,created_at DESC)"
        )


def create_battle_instance(
    uid, battle_type, map_id=0, monster_team_id=0, reuse_active=False, reward_pairs=None
):
    """Create a battle instance, optionally resuming the same active encounter."""
    if not uid:
        return None
    import uuid as _uuid
    import random as _random
    battle_id = str(_uuid.uuid4())
    now = int(time.time())
    random_seed = _random.randint(1, 0x7FFFFFFF)
    normalized_rewards = []
    for pair in reward_pairs or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            try:
                cid, quantity = int(pair[0]), int(pair[1])
            except (TypeError, ValueError):
                continue
            if cid > 0 and quantity > 0:
                normalized_rewards.append([cid, quantity])
    with connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM accounts WHERE uid=?", (uid,)
        ).fetchone()
        if not exists:
            return None
        if reuse_active:
            active = connection.execute(
                "SELECT id FROM battle_instances WHERE uid=? AND battle_type=? "
                "AND map_id=? AND monster_team_id=? AND settled=0 "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (uid, battle_type, map_id, monster_team_id),
            ).fetchone()
            if active is not None:
                return active["id"]
            connection.execute(
                "UPDATE battle_instances SET settled=1,status='abandoned',settled_at=?,updated_at=? "
                "WHERE uid=? AND settled=0",
                (now, now, uid),
            )
        connection.execute(
            "INSERT INTO battle_instances("
            "id,uid,battle_type,map_id,monster_team_id,created_at,random_seed,status,updated_at,reward_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                battle_id, uid, battle_type, map_id, monster_team_id, now,
                random_seed, "active", now,
                json.dumps(normalized_rewards, separators=(",", ":")),
            ),
        )
    return battle_id


def set_battle_server_snapshot(uid, battle_id, snapshot):
    """Persist the server-generated battle snapshot used for result validation."""
    if not uid or not battle_id or not isinstance(snapshot, dict):
        return False
    now = int(time.time())
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE battle_instances SET server_snapshot_json=?,updated_at=? "
            "WHERE id=? AND uid=? AND settled=0",
            (encoded, now, battle_id, uid),
        )
    return cursor.rowcount == 1


def evaluate_battle_instance(uid, battle_id):
    """Return a deterministic server-side outcome from the generated FightPOD.

    The evaluator intentionally consumes the decompiled data tables rather than
    trusting the client's damage report.  Unknown effect types remain visible in
    the trace and do not silently become arbitrary damage.
    """
    if not uid or not battle_id:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT server_snapshot_json FROM battle_instances WHERE id=? AND uid=?",
            (battle_id, uid),
        ).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["server_snapshot_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None

    def side_power(side):
        units = side.get("ArrFightUnitPOD", []) if isinstance(side, dict) else []
        if not isinstance(units, list):
            return 0, 0
        total = 0
        valid = 0
        for unit in units:
            if not isinstance(unit, dict):
                continue
            try:
                power = max(0, int(unit.get("Power", 0)))
            except (TypeError, ValueError):
                power = 0
            total += power
            valid += 1
        return total, valid

    max_round = max(1, int(snapshot.get("MaxRound", 30) or 30))
    attacker = snapshot.get("Attacker", {})
    defender = snapshot.get("Defender", {})
    attacker_power, attacker_count = side_power(attacker)
    defender_power, defender_count = side_power(defender)
    event_trace = []
    combatants = []

    # Empty sides are retained for legacy/fixture battles.  When both sides
    # contain only synthetic Power fields, retain the old deterministic power
    # rule because those snapshots have no unit attributes to simulate.
    if defender_count == 0:
        simulated_result = 1
        simulated_rounds = 1
        turn_count = 0
        trace = []
    elif attacker_count == 0:
        simulated_result = 0
        simulated_rounds = 1
        turn_count = 0
        trace = []
    else:
        all_units = []
        for side_name, side in (("attacker", attacker), ("defender", defender)):
            for index, unit in enumerate(side.get("ArrFightUnitPOD", []) or []):
                if not isinstance(unit, dict):
                    continue
                attrs = unit.get("Attributes")
                all_units.append((side_name, index, unit, attrs if isinstance(attrs, list) else []))
        has_real_attributes = any(
            len(attrs) >= 4 and any(float(value or 0) > 0 for value in attrs[:4])
            for _side, _index, _unit, attrs in all_units
        )
        if not has_real_attributes:
            if attacker_power > defender_power:
                simulated_result = 1
            elif attacker_power < defender_power:
                simulated_result = 0
            else:
                seed = int(snapshot.get("RandomSeed", 0) or 0)
                simulated_result = 1 if seed % 2 else 0
            simulated_rounds = min(max_round, max(1, (defender_power // max(attacker_power, 1)) + 1))
            turn_count = 0
            trace = []
        else:
            seed = int(snapshot.get("RandomSeed", 0) or 0)
            rng = __import__("random").Random(seed)
            rules = BATTLE_RULES
            attribute_ids = {
                "hp": 9,
                "attack": 7,
                "defense": 11,
                "speed": 10,
                "crit": 24,
                "critDamage": 26,
                "energy": 14,
            }

            def number(value, fallback=0.0):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return fallback
                return value

            def numeric(value, fallback=0.0):
                """Convert a configured scalar without discarding zero/negative values."""
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return fallback

            def attr(unit, attr_id, fallback=0.0):
                attrs = []
                types = []
                if isinstance(unit, dict):
                    attrs = unit.get("Attributes") or unit.get("attributes") or []
                    types = unit.get("AttributeTypes") or unit.get("attributeTypes") or []
                if isinstance(attrs, list) and isinstance(types, list) and attr_id in types:
                    index = types.index(attr_id)
                    return numeric(attrs[index], fallback) if index < len(attrs) else fallback
                # Legacy snapshots did not preserve AttType. Keep their old
                # positional interpretation only for compatibility fixtures.
                legacy = {9: 0, 7: 1, 11: 2, 10: 3, 24: 6, 26: 7, 14: 4}
                index = legacy.get(attr_id)
                if index is not None and isinstance(attrs, list) and index < len(attrs):
                    return numeric(attrs[index], fallback)
                return fallback

            def value_list(value):
                if isinstance(value, list):
                    return value
                return []

            def initial_buff_ids(unit):
                values = value_list(unit.get("InitBuff"))
                # InitialBuff is a flat [buff CID, parameter] array.
                return [int(values[index]) for index in range(0, len(values), 2)
                        if isinstance(values[index], (int, float)) and int(values[index]) > 0
                        and int(values[index]) != 999]

            def make_state(side_name, index, unit, attrs):
                power = max(1, int(unit.get("Power", 0) or 1))
                hp = max(1, int(attr(unit, attribute_ids["hp"], power)))
                skills = [int(value) for value in value_list(unit.get("Skills"))
                          if isinstance(value, (int, float)) and int(value) > 0]
                buffs = []
                for buff_id in initial_buff_ids(unit):
                    config = rules["buffs"].get(str(buff_id), {})
                    buffs.append({
                        "id": buff_id,
                        "stack": 1,
                        "remaining": int(config.get("buffTime", -1) or -1),
                        "skillParams": [],
                        "source": None,
                        "triggerCount": 0,
                        "stackTriggerCount": 0,
                        "triggerCooldown": 0,
                        "lastAppliedRound": None,
                        "maxStack": max(1, int(config.get("stackMax", 1) or 1)),
                    })
                cooldowns = {}
                for skill_id in skills:
                    detail_id = rules["skills"].get(str(skill_id), {}).get("detail", skill_id)
                    detail = rules["skillDetails"].get(str(detail_id), {})
                    cooldowns[str(skill_id)] = max(0, int(detail.get("initCd", 0) or 0))
                weak = max(0, int(unit.get("WeakNum", 0) or 0))
                weak_max = max(weak, int(unit.get("WeakMaxNum", 999999) or 999999))
                return {
                    "side": side_name,
                    "index": index,
                    "position": max(1, int(unit.get("BattlePos", index + 1) or index + 1)),
                    "attributes": list(attrs),
                    "attributeTypes": value_list(unit.get("AttributeTypes")),
                    "hp": hp,
                    "max_hp": hp,
                    "attack": max(1, int(attr(unit, attribute_ids["attack"], power))),
                    "defense": max(0, int(attr(unit, attribute_ids["defense"], power // 4))),
                    "speed": max(1, int(attr(unit, attribute_ids["speed"], 1))),
                    "crit": max(0.0, min(1.0, attr(unit, attribute_ids["crit"], 0.0))),
                    "critDamage": max(1.0, attr(unit, attribute_ids["critDamage"], 1.5)),
                    "skills": skills,
                    "sp": max(0, int(attr(unit, attribute_ids["energy"], 0))),
                    "max_sp": 100,
                    "cooldowns": cooldowns,
                    "actions": 0,
                    "buffs": buffs,
                    "statuses": set(),
                    "spStatuses": {
                        int(value) for value in value_list(unit.get("SPStatus"))
                        if isinstance(value, (int, float)) and int(value) > 0
                    },
                    "attributeMods": {},
                    "skillRatioMods": {},
                    "skillRatioPercentMods": {},
                    "skillRatioAddMods": {},
                    "skillElementMods": {},
                    "skillCostMods": {},
                    "skillReplacements": {},
                    "buffResistance": set(),
                    "absorb": 0,
                    "shareDamage": 0.0,
                    "shield": 0,
                    "weak": weak,
                    "weakMax": weak_max,
                    "weakTypes": [int(value) for value in value_list(unit.get("WeakTypes"))
                                  if isinstance(value, (int, float))],
                    "revived": False,
                    "services": [],
                    "contentEffects": [],
                    "noTriggerElements": set(),
                    "noTriggerAll": False,
                    "energyRate": 1.0,
                }

            combatants = [make_state(side_name, index, unit, attrs) for side_name, index, unit, attrs in all_units]

            def living(side, include_dead=False):
                return [unit for unit in combatants
                        if unit["side"] == side and (include_dead or unit["hp"] > 0)]

            def target_list(actor, target_type, allow_dead=False):
                config = rules["searchTargets"].get(str(target_type), {})
                camp = int(config.get("selectCamp", 1) or 1)
                select_self = bool(config.get("selectSelf"))
                same_side = camp == 2 or camp in (8, 9, 10, 11, 12)
                if camp == -1:
                    candidates = [unit for unit in combatants
                                  if allow_dead or unit["hp"] > 0]
                elif camp in (3, 4):
                    candidates = [actor]
                else:
                    side = actor["side"] if same_side else ("defender" if actor["side"] == "attacker" else "attacker")
                    candidates = living(side, allow_dead or bool(config.get("selectDeath")))
                if not candidates:
                    return []
                position_type = int(config.get("positionType", 0) or 0)
                if position_type == 1:
                    candidates = [unit for unit in candidates if unit["position"] <= 5] or candidates
                elif position_type == 2:
                    candidates = [unit for unit in candidates if unit["position"] > 5] or candidates
                if config.get("alivePriority"):
                    candidates.sort(key=lambda unit: (unit["hp"] <= 0, unit["position"], unit["index"]))
                else:
                    candidates.sort(key=lambda unit: (unit["position"], unit["index"]))
                if bool(config.get("isGroup")):
                    if not select_self:
                        candidates = [unit for unit in candidates if unit is not actor]
                    return candidates
                if not select_self:
                    candidates = [unit for unit in candidates if unit is not actor]
                return candidates[: max(1, int(config.get("selectNum", 1) or 1))]

            trigger_names = {
                101: "TimeTrigger",
                102: "BeRemoved",
                103: "AddBuff",
                104: "StackBuff",
                301: "BattleRoundStart",
                302: "BattleRoundEnd",
                303: "BattleActionStart",
                304: "BattleActionEnd",
                305: "BattleCastSkillStart",
                306: "BattleCastSkillEndResult",
                307: "BattleCastSkillEndTargetStatus",
                308: "BattleCastSkillEndTargetSPStatus",
                309: "BattleBeAtkResult",
                310: "BattleSelfAddStatus",
                311: "BattleUnitAddSPStatus",
                312: "BattleStart",
                313: "BattleUnitBeHurt",
                314: "BattleCastSkillEnd",
                315: "BattleUnitAttrChange",
                316: "BattleUnitBeHeal",
                317: "BattlePreAtkTargetBuff",
                318: "BattlePreAtkHit",
                319: "BattleUnitBeforeDead",
                320: "BattleUnitDotEffect",
                321: "BattleUnitBuffImmune",
                322: "BePreAtkHit",
                323: "BattleAfterAtkHit",
                324: "BattlePreBeAtkBuff",
                325: "BattleUnitAfterDead",
                326: "BattleInitComplete",
                327: "BattleUnitWeakBeBreak",
                328: "BattleUnitWeakRecover",
                329: "BattleAbsorbDmg",
                330: "BattleAfterChooseSkill",
                331: "BattleCastSkillGroupEnd",
                332: "BattleUnitBeforeDotEffect",
                333: "BattleUnitShareDmg",
                334: "BattleBeforeHPChange",
                335: "BattleAfterHPChange",
                336: "BattlePreCastSkillEnd",
            }
            event_trace = []
            event_depth = 0

            def int_values(value):
                values = value_list(value)
                result = []
                for item in values:
                    try:
                        result.append(int(float(item)))
                    except (TypeError, ValueError):
                        continue
                return result

            def apply_buff_tags(target, config, add):
                for tag in int_values(config.get("buffTag")):
                    if tag <= 0:
                        continue
                    if add:
                        target["statuses"].add(tag)
                    else:
                        still_present = any(
                            tag in int_values(rules["buffs"].get(str(item["id"]), {}).get("buffTag"))
                            for item in target["buffs"]
                        )
                        if not still_present:
                            target["statuses"].discard(tag)

            def buff_state(target, buff_id):
                return next((item for item in target["buffs"] if item["id"] == buff_id), None)

            def clear_buff_modifiers(item):
                for modifier in item.pop("modifiers", []):
                    modifier_target = modifier.get("target")
                    if not isinstance(modifier_target, dict):
                        continue
                    field = modifier.get("field")
                    key = modifier.get("key")
                    value = modifier.get("value", 0)
                    values = modifier_target.get(field)
                    if modifier.get("mode") == "set_add" and isinstance(values, set):
                        values.discard(value)
                        continue
                    if modifier.get("mode") == "set_flag":
                        if modifier_target.get(field) == value:
                            modifier_target[field] = False
                        continue
                    if modifier.get("mode") == "restore":
                        modifier_target[field] = value
                        continue
                    if not isinstance(values, dict):
                        continue
                    if modifier.get("mode") == "add":
                        values[key] = values.get(key, 0) - value
                        if values[key] == 0:
                            values.pop(key, None)
                    elif values.get(key) == value:
                        values.pop(key, None)

            def buff_group_relation(existing_config, new_config):
                """Return the official group relation value for a new Buff."""
                old_group = int(existing_config.get("groupId", 0) or 0)
                new_group = int(new_config.get("groupId", 0) or 0)
                if not old_group or not new_group:
                    return 0
                relation = rules["buffGroupRelations"].get(str(new_group), {})
                groups = value_list(relation.get("group"))
                target_relation = rules["buffGroupRelations"].get(str(old_group), {})
                old_index = int(target_relation.get("index", 0) or 0)
                if old_index > 0 and old_index <= len(groups):
                    return int(numeric(groups[old_index - 1], 0))
                return 0

            def selected_effect_targets(owner, target_type, fallback=None, allow_dead=True):
                try:
                    target_type = int(target_type or 0)
                except (TypeError, ValueError):
                    target_type = 0
                if target_type:
                    selected = target_list(owner, target_type, allow_dead=allow_dead)
                    if selected:
                        return selected
                return [fallback or owner]

            def remove_buff(target, buff_id, trigger=True, dispel=False):
                removed = [item for item in target["buffs"] if item["id"] == buff_id]
                if not removed:
                    return False
                config = rules["buffs"].get(str(buff_id), {})
                if dispel and 1 in int_values(config.get("properties")):
                    event_trace.append({
                        "event": "Dispel",
                        "effectType": 103,
                        "effectName": BATTLE_EFFECT_NAMES[103],
                        "buff": buff_id,
                        "status": "immune",
                    })
                    return False
                for item in removed:
                    clear_buff_modifiers(item)
                target["buffs"] = [item for item in target["buffs"] if item["id"] != buff_id]
                apply_buff_tags(target, config, False)
                if trigger:
                    for item in removed:
                        trigger_buff(item, target, 102, target, None, {
                            "reason": "remove",
                            "buffId": item.get("id", 0),
                            "buffConfig": config,
                            "removedBuff": item,
                        })
                return True

            def add_buff(
                target, buff_id, stack=1, duration=0, skill_params=None,
                source=None, round_number=None, event_context=None,
            ):
                config = rules["buffs"].get(str(buff_id), {})
                if not config:
                    return False
                if buff_id in target.get("buffResistance", set()):
                    event_trace.append({
                        "event": "BuffResistance",
                        "effectType": 106,
                        "effectName": BATTLE_EFFECT_NAMES[106],
                        "buff": buff_id,
                        "status": "immune",
                    })
                    return False
                maximum = max(1, int(config.get("stackMax", 1) or 1))
                existing = next((item for item in target["buffs"] if item["id"] == buff_id), None)
                if existing is None:
                    item = {
                        "id": buff_id,
                        "stack": min(maximum, max(1, int(stack or 1))),
                        "remaining": int(duration or config.get("buffTime", -1) or -1),
                        "skillParams": list(skill_params or []),
                        "source": source,
                        "triggerCount": 0,
                        "stackTriggerCount": 0,
                        "triggerCooldown": 0,
                        "lastAppliedRound": round_number,
                        "maxStack": maximum,
                    }
                    for old in list(target["buffs"]):
                        old_config = rules["buffs"].get(str(old["id"]), {})
                        relation = buff_group_relation(old_config, config)
                        if relation == 1:
                            remove_buff(target, old["id"])
                        elif relation == 2:
                            return False
                    target["buffs"].append(item)
                    apply_buff_tags(target, config, True)
                    trigger_context = {
                        "reason": "add",
                        "buffId": buff_id,
                        "buffConfig": config,
                        "addedBuff": item,
                    }
                    if isinstance(event_context, dict):
                        trigger_context.update(event_context)
                    trigger_buff(item, target, 103, target, source, trigger_context)
                else:
                    old_stack = existing["stack"]
                    stack_type = int(config.get("stackType", 0) or 0)
                    maximum = max(1, int(existing.get("maxStack", maximum) or maximum))
                    if stack_type == 3:
                        existing["stack"] = min(maximum, existing["stack"] + max(1, int(stack or 1)))
                    elif stack_type == 5 and (
                        round_number is None or existing.get("lastAppliedRound") == round_number
                    ):
                        # ROUND_OVERRIDE replaces repeated applications in a
                        # single round, while a later round can accumulate a
                        # fresh stack up to StackMaxNumber.
                        existing["stack"] = min(maximum, max(1, int(stack or 1)))
                    elif stack_type == 5:
                        existing["stack"] = min(maximum, existing["stack"] + max(1, int(stack or 1)))
                    else:
                        # The client enum values are Effect=3, Override=4 and
                        # RoundOverride=5 in the serialized tables.  Both
                        # override modes replace the visible stack; the round
                        # mode also refreshes the configured duration below.
                        existing["stack"] = min(maximum, max(1, int(stack or 1)))
                    if duration or int(config.get("buffTime", -1) or -1) < 0:
                        existing["remaining"] = int(duration or config.get("buffTime", -1) or -1)
                    if skill_params is not None:
                        existing["skillParams"] = list(skill_params)
                    if source is not None:
                        existing["source"] = source
                    if round_number is not None:
                        existing["lastAppliedRound"] = round_number
                    if existing["stack"] != old_stack:
                        for modifier in existing.pop("modifiers", []):
                            modifier_target = modifier.get("target")
                            values = modifier_target.get(modifier.get("field"), {}) if isinstance(modifier_target, dict) else {}
                            key = modifier.get("key")
                            if modifier.get("mode") == "add" and isinstance(values, dict):
                                values[key] = values.get(key, 0) - modifier.get("value", 0)
                                if values[key] == 0:
                                    values.pop(key, None)
                            elif modifier.get("mode") == "set_add" and isinstance(values, set):
                                values.discard(modifier.get("value"))
                            elif modifier.get("mode") == "set_flag" and modifier_target.get(modifier.get("field")) == modifier.get("value"):
                                modifier_target[modifier.get("field")] = False
                            elif isinstance(values, dict) and values.get(key) == modifier.get("value"):
                                values.pop(key, None)
                        trigger_buff(existing, target, 104, target, source, {
                            "reason": "stack",
                            "buffId": buff_id,
                            "buffConfig": config,
                            "stack": existing["stack"],
                            "stackDelta": existing["stack"] - old_stack,
                            "addedBuff": existing,
                        })
                return True

            def expire_buffs():
                for unit in combatants:
                    kept = []
                    for item in unit["buffs"]:
                        if item["remaining"] > 0:
                            item["remaining"] -= 1
                        if item["remaining"] != 0:
                            kept.append(item)
                        else:
                            config = rules["buffs"].get(str(item["id"]), {})
                            clear_buff_modifiers(item)
                            apply_buff_tags(unit, config, False)
                            trigger_buff(item, unit, 102, unit, item.get("source"), {
                                "reason": "timeout",
                                "buffId": item.get("id", 0),
                                "buffConfig": config,
                                "removedBuff": item,
                            })
                    unit["buffs"] = kept

            def dynamic_value(arg_type, arg, actor, target, detail, buff=None, arg_index=0):
                values = value_list(arg)
                try:
                    kind = int(arg_type or 0)
                except (TypeError, ValueError):
                    kind = 0

                def selected_unit(selector):
                    return actor if int(numeric(selector, 1)) in (1, 3) else target

                def read_attribute(values, include_mods=True):
                    if len(values) < 2:
                        return 0.0
                    selector = int(numeric(values[0], 1))
                    attribute_id = int(numeric(values[1], 0))
                    unit = selected_unit(selector)
                    value = attr(unit, attribute_id, 0.0)
                    if include_mods:
                        value += unit.get("attributeMods", {}).get(attribute_id, 0.0)
                    return value

                def read_buff_metric(metric):
                    if not values:
                        return 0.0
                    unit = selected_unit(values[0]) if len(values) > 1 else target
                    try:
                        buff_id = int(numeric(values[-1], 0))
                    except (TypeError, ValueError):
                        return 0.0
                    state = buff_state(unit, buff_id)
                    if state is None:
                        return 0.0
                    if metric == "time":
                        return float(state.get("remaining", 0))
                    if metric == "stack":
                        return float(state.get("stack", 0))
                    if metric == "trigger":
                        return float(state.get("triggerCount", 0))
                    return 0.0

                def current_skill_id(context):
                    candidates = [context]
                    if isinstance(context, dict) and isinstance(context.get("detail"), dict):
                        candidates.append(context["detail"])
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue
                        for key in ("skill", "skillId", "currentSkill", "eventSkill"):
                            if key not in candidate:
                                continue
                            try:
                                return float(candidate[key] or 0)
                            except (TypeError, ValueError):
                                return 0.0
                    return 0.0

                if kind in (303, 320):
                    return read_attribute(values, include_mods=True)
                if kind in (102,):
                    return read_buff_metric("time")
                if kind in (103, 310, 312):
                    return read_buff_metric("stack")
                if kind == 204:
                    # The extracted tables pass one Buff CID and compare the
                    # result with 0/1/>1.  With one parameter the owner is the
                    # official implicit unit, so the value is its stack count.
                    return read_buff_metric("stack")
                if kind == 205:
                    return read_buff_metric("trigger")
                if kind == 104:
                    try:
                        index = max(0, int(numeric(values[0], 1)) - 1) if values else 0
                    except (TypeError, ValueError):
                        index = 0
                    payload = detail.get("detail") if isinstance(detail, dict) else None
                    payload = payload if isinstance(payload, dict) else detail
                    params = value_list(payload.get("parameter")) if isinstance(payload, dict) else []
                    if buff is not None and not params:
                        params = value_list(buff.get("skillParams"))
                    if index < len(params):
                        return numeric(params[index], 0.0)
                    return 0.0
                if kind == 308:
                    unit = selected_unit(values[0]) if values else actor
                    status_id = int(numeric(values[-1], 0)) if values else 0
                    return 1.0 if status_id in unit.get("statuses", set()) else 0.0
                if kind == 306:
                    unit = selected_unit(values[0]) if len(values) > 1 else actor
                    status_id = int(numeric(values[-1], 0)) if values else 0
                    return 1.0 if status_id in unit.get("statuses", set()) else 0.0
                if kind == 307:
                    unit = selected_unit(values[0]) if len(values) > 1 else actor
                    status_id = int(numeric(values[-1], 0)) if values else 0
                    return 1.0 if status_id in unit.get("spStatuses", set()) else 0.0
                if kind == 309:
                    unit = selected_unit(values[0]) if values else actor
                    maximum = max(1.0, numeric(unit.get("max_hp"), 1.0))
                    return numeric(unit.get("hp"), 0.0) / maximum
                if kind == 311:
                    unit = selected_unit(values[0]) if values else actor
                    return numeric(unit.get("sp"), 0.0)
                if kind == 313:
                    unit = selected_unit(values[0]) if values else actor
                    return float(len(unit.get("buffs", [])))
                if kind == 314:
                    return numeric(detail.get("element", 0), 0.0) if isinstance(detail, dict) else 0.0
                if kind == 317:
                    unit = selected_unit(values[0]) if values else actor
                    return numeric(unit.get("weak", 0), 0.0)
                if kind == 318:
                    return numeric(detail.get("damage", 0), 0.0) if isinstance(detail, dict) else 0.0
                if kind == 316:
                    return current_skill_id(detail)
                if kind == 319:
                    unit = selected_unit(values[0]) if len(values) > 1 else actor
                    status_id = int(numeric(values[-1], 0)) if values else 0
                    return 1.0 if status_id in unit.get("statuses", set()) else 0.0
                if kind == 321:
                    unit = selected_unit(values[0]) if values else actor
                    return numeric(unit.get("max_sp", 0), 0.0)
                if kind in (322, 323, 324, 325, 327):
                    skill_id = int(numeric(values[-1], 0)) if values else 0
                    if kind == 322:
                        return numeric(actor.get("skillRatioMods", {}).get(str(skill_id), 0.0), 0.0)
                    if kind == 323:
                        return numeric(actor.get("skillRatioPercentMods", {}).get(str(skill_id), 0.0), 0.0)
                    if kind == 327:
                        return numeric(actor.get("skillRatioAddMods", {}).get(str(skill_id), 0.0), 0.0)
                    if kind == 324:
                        return numeric(actor.get("skillElementMods", {}).get(str(skill_id), 0.0), 0.0)
                    return numeric(actor.get("skillReplacements", {}).get(str(skill_id), 0.0), 0.0)
                if kind == 326:
                    context = detail if isinstance(detail, dict) else {}
                    trigger_args = value_list(context.get("triggerArgs"))
                    if not trigger_args and isinstance(context.get("detail"), dict):
                        trigger_args = value_list(context["detail"].get("parameter"))
                    if not trigger_args and buff is not None:
                        trigger_args = value_list(buff.get("skillParams"))
                    if 0 <= arg_index < len(trigger_args):
                        return numeric(trigger_args[arg_index], 0.0)
                    return 0.0
                if kind == 328:
                    # 1444114 is the only extracted use and passes [unit, buff].
                    # It gates on the presence/stack of Buff 10022.
                    return read_buff_metric("stack")
                event_trace.append({
                    "event": "DynamicFormula",
                    "dynamicType": kind,
                    "dynamicParams": values,
                    "status": "unsupported",
                })
                return 0.0

            def safe_formula(expression, env):
                expression = str(expression or "").strip()
                if not expression:
                    return None
                expression = expression.replace("&&", " and ").replace("||", " or ")
                expression = re.sub(r"!(?!=)", " not ", expression).replace("^", "**")
                if not re.fullmatch(r"[A-Za-z0-9_+*/().%\- <>=!&|]+", expression):
                    return None
                try:
                    tree = ast.parse(expression, mode="eval")
                except SyntaxError:
                    return None
                allowed = (
                    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp,
                    ast.Compare, ast.Name, ast.Load, ast.Constant,
                    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
                    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
                    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                )
                if any(not isinstance(node, allowed) for node in ast.walk(tree)):
                    return None
                try:
                    return float(eval(compile(tree, "<battle-rpn>", "eval"), {"__builtins__": {}}, env))
                except (ArithmeticError, SyntaxError, ValueError, TypeError, OverflowError):
                    return None

            def evaluate_formula(function, base, actor, target, detail, buff=None):
                expression = function.get("dynamicRpn") or ""
                env = {"K": float(base), "k": float(base)}
                payload = detail.get("detail") if isinstance(detail, dict) else None
                payload = payload if isinstance(payload, dict) else detail
                payload = payload if isinstance(payload, dict) else {}
                params = value_list(payload.get("parameter"))
                for index in range(1, 9):
                    value = params[index - 1] if index <= len(params) else None
                    env["A%d" % index] = numeric(value, 0.0)
                dynamic_types = value_list(function.get("dynamicArgType"))
                dynamic_params = value_list(function.get("dynamicArgParams"))
                for index, arg_type in enumerate(dynamic_types[:8], start=1):
                    if int(numeric(arg_type, 0)) == 0:
                        continue
                    arg = dynamic_params[index - 1] if index <= len(dynamic_params) else []
                    env["A%d" % index] = dynamic_value(
                        arg_type, arg, actor, target, detail, buff, index - 1,
                    )
                value = safe_formula(expression, env)
                if value is None:
                    return float(base)
                return max(0.0, value)

            def effect_amount(params, actor, target, detail):
                values = value_list(params)
                numeric_values = [numeric(value, 0.0) for value in values]
                function_id = next(
                    (
                        int(value) for value in numeric_values
                        if value.is_integer() and str(int(value)) in rules["skillFunctions"]
                    ),
                    0,
                )
                if function_id:
                    function = rules["skillFunctions"].get(str(function_id), {})
                    return evaluate_formula(function, max(1.0, actor["attack"]), actor, target, detail)
                return numeric_values[-1] if numeric_values else 0.0

            def apply_hp_change(target, amount, source=None, is_damage=False):
                amount = int(max(0.0, amount))
                if not amount:
                    return {"damage": 0, "heal": 0, "absorbed": 0}
                before = target["hp"]
                if is_damage:
                    absorbed = min(target["shield"], amount)
                    target["shield"] -= absorbed
                    remaining = amount - absorbed
                    target["hp"] = max(0, target["hp"] - remaining)
                    return {"damage": before - target["hp"], "heal": 0, "absorbed": absorbed}
                healed = min(target["max_hp"] - target["hp"], amount)
                target["hp"] += healed
                return {"damage": 0, "heal": healed, "absorbed": 0}

            def execute_effect(owner, effect_type, params, actor, target, detail, source_item, event):
                effect_type = int(effect_type or 0)
                if effect_type == 0:
                    return
                values = value_list(params)
                # AddBuff, AddSubBuff, random variants, and the two function
                # variants share the five-column target/buff/time/stack shape.
                if effect_type in (101, 102, 104, 105, 310, 311):
                    for start in range(0, len(values), 5):
                        part = values[start : start + 5]
                        if len(part) < 3:
                            continue
                        probability = number(part[0], 1.0)
                        if effect_type in (104, 105) and rng.random() > max(0.0, min(1.0, probability)):
                            continue
                        try:
                            target_type = int(float(part[1]))
                            buff_id = int(float(part[2]))
                        except (TypeError, ValueError):
                            continue
                        duration = int(float(part[3])) if len(part) > 3 else 0
                        stack = int(float(part[4])) if len(part) > 4 else 1
                        if effect_type in (310, 311) and len(part) > 4:
                            function_id = int(numeric(part[4], 0))
                            function = rules["skillFunctions"].get(str(function_id))
                            if function is not None:
                                stack = max(1, int(effect_amount([function_id], actor, target, detail)))
                        for buff_target in selected_effect_targets(owner, target_type, target):
                            add_buff(
                                buff_target, buff_id, stack, duration,
                                skill_params=values,
                                source=owner,
                                round_number=(detail.get("round") if isinstance(detail, dict) else None),
                            )
                    return
                if effect_type == 106:
                    # BuffResistance carries the same target/buff columns as
                    # AddBuff, but records the resisted Buff IDs instead of
                    # materializing them.
                    for start in range(0, len(values), 5):
                        part = values[start : start + 5]
                        if len(part) < 3:
                            continue
                        try:
                            target_type = int(float(part[1]))
                            buff_id = int(float(part[2]))
                        except (TypeError, ValueError):
                            continue
                        for buff_target in selected_effect_targets(owner, target_type, target):
                            buff_target.setdefault("buffResistance", set()).add(buff_id)
                    return
                if effect_type == 103:
                    try:
                        target_type = int(float(values[0]))
                        buff_id = int(float(values[2])) if len(values) > 2 else int(float(values[0]))
                    except (TypeError, ValueError, IndexError):
                        return
                    for buff_target in selected_effect_targets(owner, target_type, target):
                        remove_buff(buff_target, buff_id, dispel=True)
                    return
                if effect_type in (107, 110):
                    if len(values) >= 2:
                        try:
                            buff_id = int(float(values[0]))
                            delta = int(float(values[1]))
                        except (TypeError, ValueError):
                            return
                        state = buff_state(owner, buff_id)
                        if state is not None:
                            state["remaining"] = delta if effect_type == 110 else max(0, state["remaining"] + delta)
                    return
                if effect_type in (108, 109):
                    if len(values) >= 3:
                        try:
                            target_type, buff_id, change = map(lambda value: int(float(value)), values[:3])
                        except (TypeError, ValueError):
                            return
                        for buff_target in selected_effect_targets(owner, target_type, target):
                            state = buff_state(buff_target, buff_id)
                            if state is not None:
                                config = rules["buffs"].get(str(buff_id), {})
                                if effect_type == 108:
                                    maximum = max(1, int(state.get("maxStack", config.get("stackMax", 1)) or 1))
                                    state["stack"] = max(0, min(maximum, change))
                                    if state["stack"] == 0:
                                        remove_buff(buff_target, buff_id)
                                else:
                                    state["maxStack"] = max(1, change)
                                    state["stack"] = min(state["stack"], state["maxStack"])
                    return
                if effect_type == 111:
                    owner.setdefault("services", []).append({
                        "id": int(source_item.get("id", 0)) if source_item else 0,
                        "params": values,
                    })
                    event_trace.append({
                        "event": event,
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "status": "service_recorded",
                        "params": values,
                    })
                    return
                if effect_type in (219, 220, 222, 223, 238, 239, 221, 240):
                    # These are the battle-specific members of the otherwise
                    # non-battle 201-242 range.  Their payloads vary by table
                    # generation, so resolve the first configured Buff CID and
                    # treat following values as duration/stack when present.
                    if effect_type in (219, 220, 222, 223, 238, 239):
                        buff_index = next(
                            (index for index, value in enumerate(int_values(values))
                             if str(value) in rules["buffs"]),
                            None,
                        )
                        if buff_index is not None:
                            int_params = int_values(values)
                            buff_id = int_params[buff_index]
                            tail = int_params[buff_index + 1:]
                            duration = tail[0] if tail else 0
                            stack = tail[1] if len(tail) > 1 else 1
                            own_side = owner["side"]
                            enemy_side = "defender" if own_side == "attacker" else "attacker"
                            if effect_type in (219, 222, 238):
                                candidates = living(own_side)
                            else:
                                candidates = living(enemy_side)
                            if effect_type in (238, 239):
                                candidates = [rng.choice(candidates)] if candidates else []
                            for buff_target in candidates:
                                add_buff(
                                    buff_target, buff_id, stack, duration,
                                    skill_params=values, source=owner,
                                    round_number=(detail.get("round") if isinstance(detail, dict) else None),
                                )
                            event_trace.append({
                                "event": event,
                                "effectType": effect_type,
                                "effectName": BATTLE_EFFECT_NAMES[effect_type],
                                "status": "applied",
                                "buff": buff_id,
                                "targets": len(candidates),
                            })
                        return
                    if effect_type == 221:
                        candidates = [owner] if owner["hp"] <= 0 else [
                            unit for unit in living(owner["side"], include_dead=True)
                            if unit["hp"] <= 0
                        ]
                        if not candidates:
                            return
                        rate = numeric(values[0], 30.0) if values else 30.0
                        if rate <= 1.0:
                            rate *= 100.0
                        rate = max(1.0, min(100.0, rate))
                        for revived in candidates:
                            revived["hp"] = max(1, int(revived["max_hp"] * rate / 100.0))
                            revived["shield"] = 0
                            revived["revived"] = True
                        event_trace.append({
                            "event": event,
                            "effectType": effect_type,
                            "effectName": BATTLE_EFFECT_NAMES[effect_type],
                            "status": "revived",
                            "rate": rate,
                            "targets": len(candidates),
                        })
                        return
                    # A zero-only payload is the client sentinel for all
                    # element triggers.  Positive values restrict the mask.
                    elements = {value for value in int_values(values) if value > 0}
                    owner["noTriggerAll"] = not elements
                    owner["noTriggerElements"].update(elements)
                    if source_item is not None:
                        if elements:
                            for element in elements:
                                source_item.setdefault("modifiers", []).append({
                                    "target": owner, "field": "noTriggerElements",
                                    "value": element, "mode": "set_add",
                                })
                        else:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "noTriggerAll",
                                "value": True, "mode": "set_flag",
                            })
                    event_trace.append({
                        "event": event,
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "status": "applied",
                        "elements": sorted(elements),
                        "all": not elements,
                    })
                    return
                if effect_type == 226:
                    # ChangeEnergyRate is serialized as either a fraction or
                    # a percentage depending on the source table.
                    if values:
                        old_rate = float(owner.get("energyRate", 1.0))
                        rate = numeric(values[-1], old_rate)
                        if rate > 1.0:
                            rate /= 100.0
                        owner["energyRate"] = max(0.0, rate)
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "energyRate",
                                "value": old_rate, "mode": "restore",
                            })
                    return
                if effect_type == 227:
                    if values:
                        old_max = int(owner.get("max_sp", 100))
                        new_max = max(0, int(numeric(values[-1], old_max)))
                        owner["max_sp"] = new_max
                        owner["sp"] = min(owner.get("sp", 0), new_max)
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "max_sp",
                                "value": old_max, "mode": "restore",
                            })
                    return
                if 201 <= effect_type <= 242 or effect_type in (245, 246, 248, 249, 251, 270, 271, 272):
                    owner.setdefault("contentEffects", []).append({
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "params": values,
                    })
                    event_trace.append({
                        "event": event,
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "status": "non_battle_recorded" if effect_type <= 242 else "legacy_recorded",
                        "params": values,
                    })
                    return
                if effect_type == 301:
                    if len(values) >= 3:
                        try:
                            attribute_id, mode = int(float(values[0])), int(float(values[1]))
                        except (TypeError, ValueError):
                            return
                        amount = effect_amount(values[2:], actor, target, detail)
                        sign = -1.0 if mode in (2, 4) else 1.0
                        owner["attributeMods"][attribute_id] = owner["attributeMods"].get(attribute_id, 0.0) + sign * amount
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "attributeMods", "key": attribute_id,
                                "value": sign * amount, "mode": "add",
                            })
                    return
                if effect_type == 302:
                    for status in int_values(values):
                        owner["statuses"].add(status)
                    return
                if effect_type in (303, 309):
                    amount = effect_amount(values, actor, target, detail)
                    for hp_target in selected_effect_targets(owner, values[0] if values else 0, target):
                        apply_hp_change(hp_target, amount, owner, is_damage=effect_type == 303)
                    return
                if effect_type == 304:
                    amount = int(effect_amount(values, actor, target, detail))
                    owner["sp"] = max(0, min(owner["max_sp"], owner["sp"] + amount))
                    return
                if effect_type == 305:
                    # Native effect is an immediate cast; the selected skill is
                    # kept in state so the next action can consume it safely.
                    skill_id = next((value for value in int_values(values) if str(value) in rules["skills"]), 0)
                    if skill_id:
                        owner.setdefault("pendingSkills", []).append(skill_id)
                    return
                if effect_type == 306:
                    owner["shield"] += int(max(0.0, effect_amount(values, actor, target, detail)))
                    return
                if effect_type == 307:
                    before = owner["hp"]
                    owner["hp"] = 0
                    event_trace.append({
                        "event": event,
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "status": "applied",
                        "hpBefore": before,
                        "hpAfter": owner["hp"],
                    })
                    return
                if effect_type == 308:
                    owner["statuses"].update(int_values(values))
                    return
                if effect_type == 312:
                    values = int_values(values)
                    if values:
                        owner.setdefault("skills", []).append(values[0])
                    return
                if effect_type == 313:
                    values = int_values(values)
                    if len(values) >= 2:
                        owner["cooldowns"][str(values[0])] = max(0, owner["cooldowns"].get(str(values[0]), 0) + values[1])
                    return
                if effect_type == 314:
                    old_types = list(owner.get("weakTypes", []))
                    owner["weakTypes"] = int_values(values)
                    if source_item is not None:
                        source_item.setdefault("modifiers", []).append({
                            "target": owner, "field": "weakTypes",
                            "value": old_types, "mode": "restore",
                        })
                    return
                if effect_type == 315:
                    if values:
                        old_max = int(owner.get("weakMax", owner.get("weak", 0)))
                        owner["weakMax"] = max(0, int(float(values[-1])))
                        owner["weak"] = min(owner.get("weak", 0), owner["weakMax"])
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "weakMax",
                                "value": old_max, "mode": "restore",
                            })
                    return
                if effect_type == 316:
                    owner["absorb"] = max(0, int(effect_amount(values, actor, target, detail)))
                    return
                if effect_type == 317:
                    if values:
                        old_weak = int(owner.get("weak", 0))
                        owner["weak"] = max(0, min(owner.get("weakMax", owner["weak"] + 999999), owner["weak"] + int(float(values[-1]))))
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "weak",
                                "value": old_weak, "mode": "restore",
                            })
                    return
                if effect_type == 318:
                    old_share = float(owner.get("shareDamage", 0.0))
                    share_amount = 1.0 if not values else numeric(values[-1], 0.0)
                    share_rate = share_amount if 0.0 < share_amount <= 1.0 else share_amount / 100.0
                    owner["shareDamage"] = max(0.0, min(1.0, share_rate))
                    if source_item is not None:
                        source_item.setdefault("modifiers", []).append({
                            "target": owner, "field": "shareDamage",
                            "value": old_share, "mode": "restore",
                        })
                    return
                if effect_type == 319:
                    for status in int_values(values):
                        owner["statuses"].add(status)
                    return
                if effect_type == 320:
                    values = int_values(values)
                    if len(values) >= 2:
                        owner.setdefault("skillCostMods", {})[str(values[0])] = values[1]
                    return
                if effect_type == 321:
                    if values:
                        old_max = int(owner.get("max_sp", 100))
                        owner["max_sp"] = max(0, int(float(values[-1])))
                        owner["sp"] = min(owner["sp"], owner["max_sp"])
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "max_sp",
                                "value": old_max, "mode": "restore",
                            })
                    return
                if effect_type in (322, 323, 327):
                    values = int_values(values)
                    if len(values) >= 2:
                        if effect_type == 322:
                            owner["skillRatioMods"][str(values[0])] = owner["skillRatioMods"].get(str(values[0]), 0.0) + values[1]
                            field, value = "skillRatioMods", values[1]
                        elif effect_type == 323:
                            owner["skillRatioPercentMods"][str(values[0])] = owner.get("skillRatioPercentMods", {}).get(str(values[0]), 0.0) + values[1] / 100.0
                            field, value = "skillRatioPercentMods", values[1] / 100.0
                        else:
                            owner["skillRatioAddMods"][str(values[0])] = owner["skillRatioAddMods"].get(str(values[0]), 0.0) + values[1]
                            field, value = "skillRatioAddMods", values[1]
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": field, "key": str(values[0]),
                                "value": value, "mode": "add",
                            })
                    return
                if effect_type == 324:
                    values = int_values(values)
                    if len(values) >= 2:
                        owner["skillElementMods"][str(values[0])] = values[1]
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "skillElementMods", "key": str(values[0]),
                                "value": values[1], "mode": "set",
                            })
                    return
                if effect_type == 325:
                    values = int_values(values)
                    if len(values) >= 2:
                        owner.setdefault("skillReplacements", {})[str(values[0])] = values[1]
                        if source_item is not None:
                            source_item.setdefault("modifiers", []).append({
                                "target": owner, "field": "skillReplacements", "key": str(values[0]),
                                "value": values[1], "mode": "set",
                            })
                    return
                if effect_type == 326:
                    owner.setdefault("plotEffects", []).append(values)
                    event_trace.append({
                        "event": event,
                        "effectType": effect_type,
                        "effectName": BATTLE_EFFECT_NAMES[effect_type],
                        "status": "plot_recorded",
                        "params": values,
                    })
                    return
                event_trace.append({
                    "event": event,
                    "effectType": effect_type,
                    "effectName": BATTLE_EFFECT_NAMES.get(effect_type, "UnknownEffectType"),
                    "status": "unsupported",
                    "buff": source_item.get("id") if source_item else 0,
                })

            def buff_condition(item, owner, actor, target, detail):
                config = rules["buffs"].get(str(item["id"]), {})
                expression = config.get("dynamicRpn") or ""
                if not expression:
                    return True
                function = {
                    "dynamicRpn": expression,
                    "dynamicArgType": config.get("dynamicArgType", []),
                    "dynamicArgParams": config.get("dynamicArgParams", []),
                }
                return evaluate_formula(function, 1.0, actor or owner, target or owner, detail or {}, item) > 0

            def trigger_params_match(config, event_code, context):
                """Apply the serialized TriggerParams filters before rolling a trigger."""
                params = int_values(config.get("triggerParams"))
                if not params:
                    return True
                context = context if isinstance(context, dict) else {}
                if event_code in (102, 103, 104):
                    # BuffTriggerType uses PARAM_TYPE_* followed by the value.
                    buff_config = context.get("buffConfig")
                    buff_config = buff_config if isinstance(buff_config, dict) else config
                    actual = {
                        1: int(numeric(context.get("buffId", 0), 0)),
                        2: int(numeric(buff_config.get("groupId", 0), 0)),
                        3: int(numeric(buff_config.get("buffType", 0), 0)),
                        4: int(numeric(buff_config.get("debuffType", 0), 0)),
                        5: set(int_values(buff_config.get("buffTag"))),
                    }
                    if len(params) < 2:
                        return True
                    param_type, expected = params[0], params[1]
                    if expected == -1:
                        return True
                    value = actual.get(param_type, 0)
                    if param_type == 5:
                        return expected in value
                    return value == expected

                # Battle event filters are positional.  Zero and 999 are the
                # values used by the client for a wildcard in these tables.
                match_values = context.get("triggerMatchValues")
                if not isinstance(match_values, list):
                    return True
                for expected, actual in zip(params, match_values):
                    if expected in (0, 999, -1):
                        continue
                    if int(numeric(actual, 0)) != expected:
                        return False
                return True

            def trigger_buff(item, owner, event_code, event_target=None, source=None, context=None):
                nonlocal event_depth
                if event_depth >= 32:
                    return
                config = rules["buffs"].get(str(item.get("id")), {})
                if not config or int(config.get("triggerType", 0) or 0) != int(event_code or 0):
                    return
                if int(config.get("triggerMax", -1) or -1) >= 0 and item.get("triggerCount", 0) >= int(config.get("triggerMax")):
                    return
                if item.get("triggerCooldown", 0) > 0:
                    return
                if int(event_code or 0) == 104:
                    max_stack_triggers = int(config.get("triggerMaxStack", -1) or -1)
                    if max_stack_triggers >= 0 and item.get("stackTriggerCount", 0) >= max_stack_triggers:
                        return
                actor = source if isinstance(source, dict) else owner
                context = dict(context) if isinstance(context, dict) else {}
                if "triggerArgs" not in context:
                    skill_detail = context.get("detail")
                    if isinstance(skill_detail, dict):
                        context["triggerArgs"] = list(value_list(skill_detail.get("parameter")))
                    elif item.get("skillParams"):
                        context["triggerArgs"] = list(value_list(item.get("skillParams")))
                target = context.get("eventTarget") if isinstance(context.get("eventTarget"), dict) else event_target
                target = target if isinstance(target, dict) else owner
                if not trigger_params_match(config, int(event_code or 0), context):
                    return
                if not buff_condition(item, owner, actor, target, context):
                    return
                configured_probability = config.get("triggerProbability", 1.0)
                if int(event_code or 0) == 104 and float(config.get("triggerProbabilityStack", 0.0) or 0.0) > 0:
                    configured_probability = config.get("triggerProbabilityStack")
                probability = max(0.0, min(1.0, float(configured_probability or 0.0)))
                if rng.random() > probability:
                    return
                event_depth += 1
                try:
                    item["triggerCount"] = int(item.get("triggerCount", 0)) + 1
                    if int(event_code or 0) == 104:
                        item["stackTriggerCount"] = int(item.get("stackTriggerCount", 0)) + 1
                    item["triggerCooldown"] = max(0, int(config.get("triggerCooldown", 0) or 0))
                    effect_types = value_list(config.get("effectTypes"))
                    effect_params = value_list(config.get("effectParams"))
                    for index, effect_type in enumerate(effect_types):
                        if not effect_type:
                            continue
                        params = effect_params[index] if index < len(effect_params) else []
                        execute_effect(owner, effect_type, params, actor, target, context, item, trigger_names.get(event_code, str(event_code)))
                    event_trace.append({
                        "event": trigger_names.get(event_code, str(event_code)),
                        "buff": int(item.get("id", 0)),
                        "owner": owner["position"],
                        "count": item["triggerCount"],
                    })
                finally:
                    event_depth -= 1
                max_count = int(config.get("triggerMax", -1) or -1)
                if max_count >= 0 and item.get("triggerCount", 0) >= max_count and buff_state(owner, item["id"]) is not None:
                    remove_buff(owner, item["id"], trigger=False)

            def trigger_event(event_code, event_target=None, source=None, detail=None):
                event_code = int(event_code or 0)
                context = dict(detail) if isinstance(detail, dict) else {}
                context.setdefault("eventTarget", event_target)
                context.setdefault("source", source)
                skill_detail = context.get("detail")
                if isinstance(skill_detail, dict):
                    context.setdefault("triggerArgs", list(value_list(skill_detail.get("parameter"))))
                    context.setdefault("element", int(skill_detail.get("element", 0) or 0))
                    skill_id = int(context.get("skill", 0) or 0)
                    skill_meta = rules["skills"].get(str(skill_id), {})
                    context.setdefault("skillType", int(skill_meta.get("type", 0) or 0))
                    context.setdefault("targetType", int(skill_detail.get("targetType", 0) or 0))
                    function = rules["skillFunctions"].get(str(skill_detail.get("functionId", 0)), {})
                    context.setdefault("damageType", int(function.get("damageType", 0) or 0))
                if "round" in context:
                    context["triggerMatchValues"] = [int(context.get("round", 0) or 0)]
                elif event_code in (305, 306, 307, 308, 314, 323, 330, 331, 336):
                    context["triggerMatchValues"] = [
                        context.get("skillType", 0),
                        context.get("skill", 0),
                        context.get("skillGroup", 0),
                        context.get("targetType", 0),
                        context.get("element", 0),
                    ]
                else:
                    context["triggerMatchValues"] = [
                        context.get("damageType", 0),
                        context.get("skill", 0),
                        context.get("element", 0),
                        context.get("targetType", 0),
                        context.get("isDamage", 0),
                        context.get("hpChange", 0),
                    ]
                candidates = []
                for owner in combatants:
                    if event_target is not None and owner is not event_target:
                        continue
                    for item in list(owner["buffs"]):
                        config = rules["buffs"].get(str(item["id"]), {})
                        if int(config.get("triggerType", 0) or 0) == event_code:
                            candidates.append((int(config.get("triggerPriority", 0) or 0), owner, item))
                candidates.sort(key=lambda row: -row[0])
                for _priority, owner, item in candidates:
                    if event_code == 314 and (
                        owner.get("noTriggerAll") or
                        int(numeric(context.get("element", 0), 0)) in owner.get("noTriggerElements", set())
                    ):
                        continue
                    if buff_state(owner, item["id"]) is not None:
                        trigger_buff(item, owner, event_code, event_target or owner, source or owner, context)
                    config = rules["buffs"].get(str(item["id"]), {})
                    if event_code in int_values(config.get("removeTrigger")) and buff_state(owner, item["id"]) is not None:
                        remove_buff(owner, item["id"])

            def apply_skill(actor, target, skill_id, round_number):
                skill_meta = rules["skills"].get(str(skill_id), {})
                detail_id = skill_meta.get("detail", skill_id)
                detail = rules["skillDetails"].get(str(detail_id), {})
                function = rules["skillFunctions"].get(str(detail.get("functionId", 0)), {})
                ratio_values = value_list(detail.get("ratio"))
                ratio = number(ratio_values[0], 1.0) if ratio_values else 1.0
                ratio += actor.get("skillRatioMods", {}).get(str(skill_id), 0.0)
                ratio *= 1.0 + actor.get("skillRatioPercentMods", {}).get(str(skill_id), 0.0)
                ratio += actor.get("skillRatioAddMods", {}).get(str(skill_id), 0.0)
                actor_attack = actor["attack"] + actor.get("attributeMods", {}).get(attribute_ids["attack"], 0.0)
                base = max(0.0, actor_attack * ratio)
                # Function tables express the common attack-minus-defense rule
                # explicitly through TargetAtt=11 / TargetAttVal=-1.
                target_att = value_list(function.get("targetAtt"))
                target_att_val = value_list(function.get("targetAttVal"))
                for index, attribute_id in enumerate(target_att):
                    if not attribute_id or index >= len(target_att_val):
                        continue
                    values = value_list(target_att_val[index])
                    try:
                        coefficient = float(values[0]) if values else 0.0
                    except (TypeError, ValueError):
                        coefficient = 0.0
                    base += (attr(target, int(attribute_id), 0.0) + target.get("attributeMods", {}).get(int(attribute_id), 0.0)) * coefficient
                self_att = value_list(function.get("selfAtt"))
                self_att_val = value_list(function.get("selfAttVal"))
                for index, attribute_id in enumerate(self_att):
                    if not attribute_id or index >= len(self_att_val):
                        continue
                    values = value_list(self_att_val[index])
                    try:
                        coefficient = float(values[0]) if values else 0.0
                    except (TypeError, ValueError):
                        coefficient = 0.0
                    # SkillRatio already supplies the ordinary attack term;
                    # avoid adding the common coefficient 1 twice.
                    if int(attribute_id) == attribute_ids["attack"] and coefficient == 1.0:
                        continue
                    base += (attr(actor, int(attribute_id), 0.0) + actor.get("attributeMods", {}).get(int(attribute_id), 0.0)) * coefficient
                amount = evaluate_formula(function, max(0.0, base), actor, target, detail)
                min_function = int(function.get("minDamage", 0) or 0)
                max_function = int(function.get("maxDamage", 0) or 0)
                max_rpn_function = int(function.get("maxFunctionRpn", 0) or 0)
                if min_function and str(min_function) in rules["skillFunctions"]:
                    amount = max(amount, evaluate_formula(rules["skillFunctions"][str(min_function)], base, actor, target, detail))
                upper_id = max_function or max_rpn_function
                if upper_id and str(upper_id) in rules["skillFunctions"]:
                    amount = min(amount, evaluate_formula(rules["skillFunctions"][str(upper_id)], base, actor, target, detail))
                damage_type = int(function.get("damageType", 0) or 0)
                same_side = actor["side"] == target["side"]
                if damage_type == 9 or (damage_type == 0 and same_side):
                    healed = min(target["max_hp"] - target["hp"], int(max(0.0, amount)))
                    if healed > 0:
                        target["hp"] += healed
                    return {"heal": healed, "shield": 0, "damage": 0, "critical": False, "weak": False}
                if damage_type == 11 or (damage_type == 0 and same_side and not target["hp"] < target["max_hp"]):
                    shield = int(max(0.0, amount))
                    target["shield"] += shield
                    return {"heal": 0, "shield": shield, "damage": 0, "critical": False, "weak": False}
                if damage_type == 16:
                    target["sp"] = min(target["max_sp"], target["sp"] + int(max(0.0, amount)))
                    return {"heal": 0, "shield": 0, "damage": 0, "energy": int(max(0.0, amount)), "critical": False, "weak": False}
                if 19 in target["statuses"] or 7 == damage_type:
                    return {"damage": 0, "absorbed": 0, "heal": 0, "shield": 0, "miss": True, "critical": False, "weak": False}
                element = int(actor.get("skillElementMods", {}).get(str(skill_id), detail.get("element", 0)) or 0)
                immunity_status = {1: 21, 2: 22, 3: 23, 4: 24, 5: 25, 6: 26}.get(element)
                if immunity_status in target["statuses"] or damage_type in (8, 14, 15):
                    return {"damage": 0, "absorbed": 0, "heal": 0, "shield": 0, "immune": True, "critical": False, "weak": False}
                weak_hit = bool(element and element in target.get("weakTypes", []))
                if weak_hit and target["weak"] > 0:
                    target["weak"] -= 1
                    if not detail.get("notBreakWeak"):
                        amount *= 1.2
                crit_rate = actor["crit"]
                if 16 in actor["statuses"]:
                    crit_rate = 1.0
                if 20 in actor["statuses"]:
                    crit_rate = 0.0
                critical = not function.get("isDamagePlus") and rng.random() < crit_rate
                if critical:
                    crit_damage = actor["critDamage"] / 100.0 if actor["critDamage"] > 5 else actor["critDamage"]
                    amount *= crit_damage
                damage = max(1, int(amount))
                absorb_limit = target.get("absorb", 0)
                absorbed = min(target["shield"] + absorb_limit, damage) if not function.get("ignoreShield") and not function.get("ignoreAbsorb") else 0
                target["shield"] -= absorbed
                if target["shield"] < 0:
                    target["absorb"] = max(0, target.get("absorb", 0) + target["shield"])
                    target["shield"] = 0
                damage -= absorbed
                target["hp"] = max(0, target["hp"] - damage)
                if damage_type == 10:
                    actor["hp"] = min(actor["max_hp"], actor["hp"] + damage)
                shared_damage = 0
                # ShareDmg is a post-mitigation split: the protected unit
                # keeps the direct hit and a living ally receives the
                # configured share.  Apply it directly so the secondary hit
                # cannot recursively trigger another share chain.
                share_rate = max(0.0, min(1.0, float(target.get("shareDamage", 0.0))))
                if damage > 0 and share_rate > 0:
                    allies = [
                        unit for unit in living(target["side"])
                        if unit is not target
                    ]
                    allies.sort(key=lambda unit: (unit["position"], unit["index"]))
                    if allies:
                        shared_target = allies[0]
                        shared_amount = max(1, int(damage * share_rate))
                        shared_absorbed = min(shared_target["shield"], shared_amount)
                        shared_target["shield"] -= shared_absorbed
                        shared_damage = shared_amount - shared_absorbed
                        shared_target["hp"] = max(0, shared_target["hp"] - shared_damage)
                return {
                    "damage": damage,
                    "absorbed": absorbed,
                    "heal": 0,
                    "shield": 0,
                    "critical": critical,
                    "weak": weak_hit,
                    "sharedDamage": shared_damage,
                }

            # Initial buffs are materialized by the same AddBuff trigger used
            # for skill-applied buffs.  This is important for passive chains and
            # for BuffTag-derived battle statuses.
            for unit in combatants:
                for item in list(unit["buffs"]):
                    apply_buff_tags(unit, rules["buffs"].get(str(item["id"]), {}), True)
                    trigger_buff(item, unit, 103, unit, unit, {})
            trigger_event(326)
            trigger_event(312)

            rounds = 0
            turn_count = 0
            trace = []
            while rounds < max_round:
                rounds += 1
                for unit in combatants:
                    for item in unit["buffs"]:
                        item["triggerCooldown"] = max(0, int(item.get("triggerCooldown", 0) or 0) - 1)
                expire_buffs()
                trigger_event(301, detail={"round": rounds})
                trigger_event(101, detail={"round": rounds})
                for unit in combatants:
                    for skill_id in list(unit["cooldowns"]):
                        if 10 not in unit["statuses"]:
                            unit["cooldowns"][skill_id] = max(0, unit["cooldowns"][skill_id] - 1)
                order = [unit for unit in combatants if unit["hp"] > 0]
                order.sort(key=lambda unit: (-unit["speed"], unit["position"], unit["side"], unit["index"]))
                for actor in order:
                    if actor["hp"] <= 0:
                        continue
                    opponents = living("defender" if actor["side"] == "attacker" else "attacker")
                    if not opponents:
                        break
                    actor["actions"] += 1
                    actor["sp"] = min(
                        actor["max_sp"],
                        actor["sp"] + max(0, int(round(8 * actor.get("energyRate", 1.0)))),
                    )
                    trigger_event(303, actor, actor, {"eventTarget": actor})
                    chosen_skill = 0
                    skill_candidates = list(actor.pop("pendingSkills", [])) + list(actor["skills"])
                    def effective_skill_id(skill_id):
                        current = int(skill_id or 0)
                        visited = set()
                        while str(current) in actor.get("skillReplacements", {}) and current not in visited:
                            visited.add(current)
                            current = int(actor["skillReplacements"][str(current)])
                        return current

                    for raw_skill_id in skill_candidates:
                        skill_id = effective_skill_id(raw_skill_id)
                        skill_meta = rules["skills"].get(str(skill_id), {})
                        detail_id = skill_meta.get("detail", skill_id)
                        detail = rules["skillDetails"].get(str(detail_id), {})
                        cost = int(detail.get("costEnergy", 0) or 0)
                        cost += int(actor.get("skillCostMods", {}).get(str(skill_id), 0) or 0)
                        cost = max(0, cost)
                        if actor["sp"] >= cost and actor["cooldowns"].get(str(skill_id), 0) == 0:
                            chosen_skill = skill_id
                            actor["sp"] -= cost
                            actor["cooldowns"][str(skill_id)] = max(0, int(detail.get("cooldown", 0) or 0))
                            break
                    trigger_event(330, actor, actor, {"eventTarget": actor, "skill": chosen_skill})
                    target_type = 0
                    detail = {}
                    if chosen_skill:
                        skill_meta = rules["skills"].get(str(chosen_skill), {})
                        detail = rules["skillDetails"].get(str(skill_meta.get("detail", chosen_skill)), {})
                        target_type = int(detail.get("targetType", 0) or 0)
                    targets = target_list(actor, target_type, allow_dead=False) if chosen_skill else opponents[:1]
                    if not targets:
                        targets = opponents[:1]
                    trigger_event(305, actor, actor, {"eventTarget": actor, "skill": chosen_skill, "detail": detail})
                    for target in targets:
                        if target["hp"] <= 0:
                            continue
                        event_context = {
                            "eventTarget": target,
                            "skill": chosen_skill,
                            "detail": detail,
                            "beforeHp": target["hp"],
                            "damageType": 0,
                        }
                        trigger_event(324, target, actor, event_context)
                        trigger_event(317, actor, actor, event_context)
                        trigger_event(322, target, actor, event_context)
                        trigger_event(318, actor, actor, event_context)
                        trigger_event(334, target, actor, event_context)
                        result = apply_skill(actor, target, chosen_skill, rounds) if chosen_skill else apply_skill(
                            actor, target, 0, rounds
                        )
                        event_context.update({
                            "afterHp": target["hp"],
                            "hpChange": int(event_context.get("beforeHp", target["hp"]) - target["hp"]),
                            "damage": int(result.get("damage", 0) or 0),
                            "isDamage": 1 if result.get("damage", 0) else 0,
                        })
                        trigger_event(335, target, actor, event_context)
                        if result.get("damage", 0) > 0:
                            trigger_event(313, target, actor, event_context)
                            trigger_event(309, target, actor, event_context)
                        if result.get("heal", 0) > 0:
                            trigger_event(316, target, actor, event_context)
                        if result.get("energy", 0) > 0:
                            trigger_event(311, target, actor, event_context)
                        if result.get("absorbed", 0) > 0:
                            trigger_event(329, target, actor, event_context)
                        function_id = int(detail.get("functionId", 0) or 0) if chosen_skill else 0
                        function = rules["skillFunctions"].get(str(function_id), {})
                        if result.get("weak") and target["weak"] <= 0:
                            trigger_event(327, target, actor, event_context)
                        if int(function.get("damageType", 0) or 0) == 4:
                            trigger_event(332, target, actor, event_context)
                            trigger_event(320, target, actor, event_context)
                        if chosen_skill:
                            for effect in detail.get("buffs", []):
                                probability = max(0.0, min(1.0, float(effect.get("probability", 1.0) or 0.0)))
                                if rng.random() > probability:
                                    continue
                                buff_targets = target_list(actor, int(effect.get("target", 0) or 0), allow_dead=False)
                                for buff_target in buff_targets or [target]:
                                    add_buff(
                                        buff_target,
                                        int(effect.get("id", 0) or 0),
                                        int(effect.get("stack", 1) or 1),
                                        int(effect.get("time", 0) or 0),
                                        source=actor,
                                        round_number=rounds,
                                        event_context=event_context,
                                    )
                        trigger_event(307, target, actor, event_context)
                        trigger_event(308, target, actor, event_context)
                        trigger_event(323, actor, actor, event_context)
                        trigger_event(306, actor, actor, event_context)
                        trigger_event(314, actor, actor, event_context)
                        turn_count += 1
                        if len(trace) < 128:
                            trace.append({
                                "round": rounds,
                                "side": actor["side"],
                                "position": actor["position"],
                                "target": target["position"],
                                "skill": int(chosen_skill or 0),
                                "targetHp": target["hp"],
                                "targetShield": target["shield"],
                                **result,
                            })
                        if target["hp"] <= 0:
                            trigger_event(319, target, actor, event_context)
                            for dead_buff in list(target["buffs"]):
                                if not rules["buffs"].get(str(dead_buff["id"]), {}).get("deathEffective"):
                                    remove_buff(target, dead_buff["id"])
                            trigger_event(325, target, actor, event_context)
                        if not living(target["side"]):
                            break
                    trigger_event(331, actor, actor, {"eventTarget": actor, "skill": chosen_skill, "detail": detail})
                    trigger_event(304, actor, actor, {"eventTarget": actor, "skill": chosen_skill, "detail": detail})
                    attacker_alive = bool(living("attacker"))
                    defender_alive = bool(living("defender"))
                    if not attacker_alive or not defender_alive:
                        break
                attacker_alive = bool(living("attacker"))
                defender_alive = bool(living("defender"))
                if not attacker_alive or not defender_alive:
                    break
            attacker_alive = bool(living("attacker"))
            defender_alive = bool(living("defender"))
            if attacker_alive and not defender_alive:
                simulated_result = 1
            elif defender_alive and not attacker_alive:
                simulated_result = 0
            else:
                attacker_remaining = sum(unit["hp"] for unit in combatants if unit["side"] == "attacker")
                defender_remaining = sum(unit["hp"] for unit in combatants if unit["side"] == "defender")
                simulated_result = 1 if attacker_remaining > defender_remaining else 0
            simulated_rounds = rounds
    return {
        "result": simulated_result,
        "rounds": simulated_rounds,
        "attackerPower": attacker_power,
        "defenderPower": defender_power,
        "attackerCount": attacker_count,
        "defenderCount": defender_count,
        "turnCount": turn_count,
        "trace": trace,
        "events": event_trace,
        "states": [
            {
                "side": unit["side"],
                "position": unit["position"],
                "hp": unit["hp"],
                "maxHp": unit["max_hp"],
                "sp": unit["sp"],
                "shield": unit["shield"],
                "weak": unit["weak"],
                "weakMax": unit.get("weakMax", unit["weak"]),
                "maxSp": unit.get("max_sp", 100),
                "energyRate": unit.get("energyRate", 1.0),
                "shareDamage": unit.get("shareDamage", 0.0),
                "revived": bool(unit.get("revived")),
                "buffs": [
                    {"id": item["id"], "stack": item["stack"], "remaining": item["remaining"]}
                    for item in unit["buffs"]
                ],
                "statuses": sorted(unit["statuses"]),
                "spStatuses": sorted(unit["spStatuses"]),
            }
            for unit in combatants
        ],
    }


def get_battle_instance(uid, battle_id):
    if not uid or not battle_id:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM battle_instances WHERE id=? AND uid=?", (battle_id, uid)
        ).fetchone()
    return dict(row) if row else None


def get_active_battle(uid, battle_type=None):
    if not uid:
        return None
    query = "SELECT * FROM battle_instances WHERE uid=? AND settled=0"
    params = [uid]
    if battle_type is not None:
        query += " AND battle_type=?"
        params.append(battle_type)
    query += " ORDER BY created_at DESC,id DESC LIMIT 1"
    with connect() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def get_latest_battle(uid, map_id=None):
    if not uid:
        return None
    query = "SELECT * FROM battle_instances WHERE uid=?"
    params = [uid]
    if map_id is not None:
        query += " AND map_id=?"
        params.append(map_id)
    query += " ORDER BY created_at DESC,id DESC LIMIT 1"
    with connect() as connection:
        row = connection.execute(query, params).fetchone()
    return dict(row) if row else None


def abandon_active_battles(uid, map_id=None):
    if not uid:
        return 0
    now = int(time.time())
    query = (
        "UPDATE battle_instances SET settled=1,status='abandoned',settled_at=?,updated_at=? "
        "WHERE uid=? AND settled=0"
    )
    params = [now, now, uid]
    if map_id is not None:
        query += " AND map_id=?"
        params.append(map_id)
    with connect() as connection:
        cursor = connection.execute(query, params)
    return cursor.rowcount


def complete_maze_instance(uid, maze_cid):
    """Close a maze run and add its CID to the persistent finished-maze list."""
    if not uid or not isinstance(maze_cid, int) or maze_cid <= 0:
        return False
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        maze = connection.execute(
            "SELECT 1 FROM maze_instances WHERE uid=? AND maze_cid=?",
            (uid, maze_cid),
        ).fetchone()
        if maze is None:
            return False
        connection.execute(
            "UPDATE maze_instances SET active=0 WHERE uid=? AND maze_cid=?",
            (uid, maze_cid),
        )
        row = connection.execute(
            "SELECT value_json FROM player_state_json WHERE uid=? AND field_name='finishMazes'",
            (uid,),
        ).fetchone()
        try:
            finished = json.loads(row["value_json"]) if row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            finished = []
        if not isinstance(finished, list):
            finished = []
        if maze_cid not in finished:
            finished.append(maze_cid)
        connection.execute(
            "INSERT INTO player_state_json(uid,field_name,value_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid,field_name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (
                uid, "finishMazes",
                json.dumps(finished, ensure_ascii=False, separators=(",", ":")), now,
            ),
        )
    return True


def get_battle_reward_shows(battle):
    if not battle or battle.get("status") != "won":
        return []
    with connect() as connection:
        rewards = _battle_reward_list(connection, battle, result=1)
    return [
        {"cid": cid, "num": quantity, "tag": 0}
        for cid, quantity in rewards
        if isinstance(cid, int) and isinstance(quantity, int) and quantity > 0
    ]


def settle_battle(uid, battle_id, result, rounds=0, report=None):
    """Settle a battle instance and return rewards.
    Returns None if battle not found or already settled.
    On success returns {
        'rewards': [(cid, num, tag), ...],
        'player_exp': int,
        'money': int,
    }
    """
    if not uid or not battle_id:
        return None
    if not isinstance(result, int) or result < 0 or result > 3:
        return None
    if not isinstance(rounds, int) or rounds < 0 or rounds > 10000:
        return None
    now = int(time.time())
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM battle_instances WHERE id=? AND uid=?", (battle_id, uid)
        ).fetchone()
        if row is None or row["settled"]:
            return None

        battle_type = row["battle_type"]
        reward_list = _battle_reward_list(connection, dict(row), result=result)

        changed_attrs = {}
        changed_items = {}
        item_shows = []

        for cid, quantity in reward_list:
            if not isinstance(cid, int) or not isinstance(quantity, int) or quantity <= 0:
                continue
            item_shows.append({"cid": cid, "num": quantity, "tag": 0})
            # Check if it's a num_attr (player currency like gold/souls)
            attr = connection.execute(
                "SELECT quantity FROM player_num_attrs WHERE uid=? AND cid=?",
                (uid, cid),
            ).fetchone()
            if attr is not None:
                total = attr["quantity"] + quantity
                connection.execute(
                    "UPDATE player_num_attrs SET quantity=?, updated_at=? WHERE uid=? AND cid=?",
                    (total, now, uid, cid),
                )
                changed_attrs[cid] = total
                continue
            # Otherwise treat as item
            item = connection.execute(
                "SELECT id, quantity, created_at FROM items WHERE uid=? AND template_id=?",
                (uid, cid),
            ).fetchone()
            if item is None:
                cursor = connection.execute(
                    "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                    (uid, cid, quantity, now),
                )
                item_id, created_at = cursor.lastrowid, now
            else:
                total = item["quantity"] + quantity
                connection.execute("UPDATE items SET quantity=? WHERE id=?", (total, item["id"]))
                item_id, created_at = item["id"], item["created_at"]
            changed_items[cid] = {
                "id": item_id, "cid": cid, "num": quantity,
                "usedNum": 0, "createTime": created_at,
            }

        status = "won" if result == 1 else "lost"
        report_json = json.dumps(
            report if isinstance(report, dict) else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE battle_instances SET result=?,settled=1,settled_at=?,status=?,"
            "rounds=?,report_json=?,updated_at=? WHERE id=? AND settled=0",
            (result, now, status, rounds, report_json, now, battle_id),
        )

    player_exp = result * 50 if result > 0 else 10
    money = sum(
        qty for cid, qty in reward_list
        if cid == 1 and isinstance(qty, int)
    )
    return {
        "rewards": item_shows,
        "changed_attrs": changed_attrs,
        "changed_items": list(changed_items.values()),
        "player_exp": player_exp,
        "money": money,
    }


# ── Equipment instance persistence ──
# Tracks individual equipment items with unique IDs, level, stars, and equipped state.

def _ensure_equipment_instances_table():
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS equipment_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                template_id INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                star INTEGER NOT NULL DEFAULT 0,
                exp INTEGER NOT NULL DEFAULT 0,
                locked INTEGER NOT NULL DEFAULT 0,
                equipped_to INTEGER,
                equipped_slot INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(uid) REFERENCES accounts(uid)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_equip_uid ON equipment_instances(uid)"
        )
        # Early local builds used a second AUTOINCREMENT namespace, so ordinary
        # items and equipment could share the same client-visible ItemPOD.id.
        # Move only those legacy small IDs into the reserved local long-ID range;
        # official captured IDs are already around 5e18 and remain unchanged.
        connection.execute(
            "UPDATE equipment_instances SET id=?+id WHERE id>0 AND id<1000000000000",
            (LOCAL_EQUIPMENT_ID_BASE,),
        )
        connection.execute("UPDATE equipment_instances SET star=1 WHERE star<1")
        equipment_cids = [cid for cid, item_type in _ITEM_TYPE_BY_CID.items() if item_type == 3]
        if equipment_cids:
            placeholders = ",".join("?" for _ in equipment_cids)
            connection.execute(
                f"DELETE FROM items WHERE template_id IN ({placeholders})",
                equipment_cids,
            )


def seed_equipment_from_snapshot(uid, item_pods):
    """Seed Type=3 instances from 3910 warehouse without collapsing duplicate CIDs."""
    if not uid:
        return 0
    inserted = 0
    now = int(time.time())
    with connect() as connection:
        next_id = _next_equipment_instance_id_connection(connection)
        for pod in item_pods or []:
            cid = pod.get("cid")
            num = pod.get("num")
            equipment_data = pod.get("equipmentData")
            if (
                not isinstance(cid, int)
                or _ITEM_TYPE_BY_CID.get(cid) != 3
                or not isinstance(num, int)
                or num <= 0
                or not isinstance(equipment_data, dict)
            ):
                continue
            raw_id = pod.get("id")
            instance_id = int(raw_id) if isinstance(raw_id, int) and raw_id > 0 else next_id
            existing = connection.execute(
                "SELECT uid FROM equipment_instances WHERE id=?", (instance_id,)
            ).fetchone()
            if existing is not None:
                if existing["uid"] == uid:
                    continue
                # Official capture IDs are globally unique on the real server,
                # but several offline accounts may be seeded from the same
                # captured PlayerPOD. Preserve the ID for its first owner and
                # allocate a collision-free local ID for subsequent accounts.
                instance_id = next_id
                next_id += 1
            elif instance_id == next_id:
                next_id += 1
            soul_prefab_ids = equipment_data.get("soulPrefabIds") or {}
            equipped_to = None
            equipped_slot = 0
            if isinstance(soul_prefab_ids, dict) and soul_prefab_ids:
                raw_prefab_id, raw_slot = next(iter(soul_prefab_ids.items()))
                try:
                    equipped_to = int(raw_prefab_id)
                    equipped_slot = int(raw_slot)
                except (TypeError, ValueError):
                    equipped_to = None
                    equipped_slot = 0
            connection.execute(
                "INSERT INTO equipment_instances("
                "id,uid,template_id,level,star,exp,locked,equipped_to,equipped_slot,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    uid,
                    cid,
                    max(1, int(equipment_data.get("lv", 1) or 1)),
                    max(1, int(equipment_data.get("star", 1) or 1)),
                    max(0, int(equipment_data.get("exp", 0) or 0)),
                    1 if equipment_data.get("lock") else 0,
                    equipped_to,
                    equipped_slot,
                    int(pod.get("createTime", now) or now),
                ),
            )
            inserted += 1
    return inserted


def ensure_ssr_spirits_five_star(uid):
    """Ensure one five-star instance of every visible SSR spirit exists.

    The current client data defines the SSR spirit set as 44001-44150.  This
    migration is intentionally idempotent and upgrades an existing instance
    in place so equipped items and duplicate copies are preserved.
    """
    if not uid:
        return {"inserted": 0, "upgraded": 0, "total": 0}
    ssr_cids = sorted(
        cid for cid, item in (_module_config.get("items", {}).get("ItemTable_2", {}) or {}).items()
        if str(cid).isdigit()
        and 44001 <= int(cid) <= 44150
        and isinstance(item, dict)
        and int(item.get("Type", 0) or 0) == 3
        and int(item.get("Quality", 0) or 0) == 5
    )
    if not ssr_cids:
        ssr_cids = list(range(44001, 44151))
    inserted = upgraded = 0
    now = int(time.time())
    with connect() as connection:
        next_id = _next_equipment_instance_id_connection(connection)
        for cid in ssr_cids:
            row = connection.execute(
                "SELECT id,star FROM equipment_instances WHERE uid=? AND template_id=? "
                "ORDER BY CASE WHEN star>=5 THEN 0 ELSE 1 END,id LIMIT 1",
                (uid, int(cid)),
            ).fetchone()
            if row is not None:
                if int(row["star"] or 0) < 5:
                    connection.execute(
                        "UPDATE equipment_instances SET star=5 WHERE uid=? AND id=?",
                        (uid, int(row["id"])),
                    )
                    upgraded += 1
                continue
            connection.execute(
                "INSERT INTO equipment_instances(id,uid,template_id,level,star,created_at) "
                "VALUES(?,?,?,1,5,?)",
                (next_id, uid, int(cid), now),
            )
            next_id += 1
            inserted += 1
    return {"inserted": inserted, "upgraded": upgraded, "total": len(ssr_cids)}


def get_equipment_instances(uid):
    """Return all persisted equipment instances for a player."""
    if not uid:
        return []
    with connect() as connection:
        rows = connection.execute(
            "SELECT id,template_id,level,star,exp,locked,equipped_to,equipped_slot,created_at "
            "FROM equipment_instances WHERE uid=? ORDER BY id",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_equipment_item_pods(uid):
    return [_equipment_item_pod(row) for row in get_equipment_instances(uid)]


def wear_equipment(uid, equip_id, soul_cid, slot):
    """Equip an item to a soul. Returns True if successful."""
    if not uid or not isinstance(equip_id, int) or not isinstance(soul_cid, int) or not isinstance(slot, int) or slot <= 0:
        return False
    with connect() as connection:
        equip = connection.execute(
            "SELECT equipped_to FROM equipment_instances WHERE id=? AND uid=?", (equip_id, uid)
        ).fetchone()
        if equip is None:
            return False
        soul = connection.execute(
            "SELECT 1 FROM souls WHERE uid=? AND soul_id=?", (uid, soul_cid)
        ).fetchone()
        if soul is None:
            return False
        connection.execute(
            "UPDATE equipment_instances SET equipped_to=NULL,equipped_slot=0 "
            "WHERE uid=? AND equipped_to=? AND equipped_slot=? AND id<>?",
            (uid, soul_cid, slot, equip_id),
        )
        connection.execute(
            "UPDATE equipment_instances SET equipped_to=?, equipped_slot=? WHERE uid=? AND id=?",
            (soul_cid, slot, uid, equip_id),
        )
    return True


def set_equipment_locked(uid, equip_id, locked=None):
    if not uid or not isinstance(equip_id, int):
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT locked FROM equipment_instances WHERE uid=? AND id=?", (uid, equip_id)
        ).fetchone()
        if row is None:
            return None
        new_value = (not bool(row["locked"])) if locked is None else bool(locked)
        cursor = connection.execute(
            "UPDATE equipment_instances SET locked=? WHERE uid=? AND id=?",
            (1 if new_value else 0, uid, equip_id),
        )
    return bool(new_value) if cursor.rowcount == 1 else None


def dump_equipment(uid, equip_ids):
    """Remove equipment from souls. Returns count of items affected."""
    if not uid or not equip_ids:
        return 0
    ids = [eid for eid in equip_ids if isinstance(eid, int)]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with connect() as connection:
        cursor = connection.execute(
            f"UPDATE equipment_instances SET equipped_to=NULL,equipped_slot=0 WHERE uid=? AND id IN ({placeholders}) AND equipped_to IS NOT NULL",
            (uid, *ids),
        )
    return cursor.rowcount


def upgrade_equipment(uid, equip_id, material_map):
    """Upgrade equipment level using materials. Returns (new_level, materials_used)."""
    if not uid:
        return None
    if not isinstance(material_map, dict):
        return None
    normalized = {}
    for cid, quantity in material_map.items():
        if not isinstance(cid, int) or not isinstance(quantity, int) or cid <= 0 or quantity <= 0:
            return None
        normalized[cid] = quantity
    total_materials = sum(normalized.values())
    if total_materials <= 0:
        return None
    now = int(time.time())
    with connect() as connection:
        equip = connection.execute(
            "SELECT level FROM equipment_instances WHERE id=? AND uid=?", (equip_id, uid)
        ).fetchone()
        if equip is None:
            return None
        gain = min(total_materials, max(0, 50 - int(equip["level"])))
        if gain <= 0:
            return None
        costs = []
        remaining = gain
        for cid, quantity in normalized.items():
            spent = min(quantity, remaining)
            if spent:
                costs.append((cid, spent))
                remaining -= spent
            if remaining == 0:
                break
        if not _consume_item_pairs_connection(connection, uid, costs, now):
            return None
        new_level = int(equip["level"]) + gain
        connection.execute(
            "UPDATE equipment_instances SET level=? WHERE uid=? AND id=?", (new_level, uid, equip_id)
        )
    return new_level, gain


def upstar_equipment(uid, equip_id, fodder_ids):
    """Increase equipment star level."""
    if not uid:
        return False
    if not isinstance(fodder_ids, list) or not fodder_ids:
        return False
    if any(not isinstance(fid, int) for fid in fodder_ids):
        return False
    if len(set(fodder_ids)) != len(fodder_ids) or equip_id in fodder_ids:
        return False
    with connect() as connection:
        equip = connection.execute(
            "SELECT star FROM equipment_instances WHERE id=? AND uid=?", (equip_id, uid)
        ).fetchone()
        if equip is None or equip["star"] >= 10:
            return False
        # Verify all fodder exists
        for fid in fodder_ids:
            f = connection.execute(
                "SELECT locked,equipped_to FROM equipment_instances WHERE id=? AND uid=?", (fid, uid)
            ).fetchone()
            if f is None or f["locked"] or f["equipped_to"] is not None:
                return False
        # Delete fodder and upgrade
        for fid in fodder_ids:
            connection.execute(
                "DELETE FROM equipment_instances WHERE uid=? AND id=?", (uid, fid)
            )
        connection.execute(
            "UPDATE equipment_instances SET star=star+1 WHERE uid=? AND id=?", (uid, equip_id)
        )
    return True


def decp_equipment(uid, equip_ids, keep_high_rarity=False):
    """Decompose equipment into materials. Returns list of ItemShowPOD."""
    if not uid or not equip_ids:
        return None
    if not isinstance(equip_ids, list) or any(not isinstance(eid, int) for eid in equip_ids):
        return None
    ids = list(dict.fromkeys(equip_ids))
    if len(ids) != len(equip_ids):
        return None
    now = int(time.time())
    results = []
    with connect() as connection:
        equips = []
        for eid in ids:
            equip = connection.execute(
                "SELECT template_id,level,star,locked,equipped_to FROM equipment_instances WHERE id=? AND uid=?",
                (eid, uid),
            ).fetchone()
            if equip is None or equip["locked"] or equip["equipped_to"] is not None:
                return None
            equips.append(equip)
        for eid, equip in zip(ids, equips):
            # Simple formula: recycle 1 material per star+1
            recycle_cid = 10006  # generic material
            recycle_qty = 1 + equip["star"]
            connection.execute(
                "DELETE FROM equipment_instances WHERE uid=? AND id=?", (uid, eid)
            )
            results.append({"cid": recycle_cid, "num": recycle_qty, "tag": 0})
            # Add to inventory
            item = connection.execute(
                "SELECT id, quantity FROM items WHERE uid=? AND template_id=?",
                (uid, recycle_cid),
            ).fetchone()
            if item:
                connection.execute(
                    "UPDATE items SET quantity=quantity+? WHERE id=?", (recycle_qty, item["id"])
                )
            else:
                connection.execute(
                    "INSERT INTO items(uid,template_id,quantity,created_at) VALUES(?,?,?,?)",
                    (uid, recycle_cid, recycle_qty, now),
                )
    return results if results else None


def database_summary():
    with connect() as connection:
        accounts = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        players = connection.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        cur = connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0]
        item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        soul_count = connection.execute("SELECT COUNT(*) FROM souls").fetchone()[0]
        task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    return {"accounts": accounts, "players": players, "sessions": sessions, "currencies": cur, "items": item_count, "souls": soul_count, "tasks": task_count}
    with connect() as connection:
        accounts = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        players = connection.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        cur = connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0]
    return {"accounts": accounts, "players": players, "sessions": sessions, "currencies": cur}


initialize()
_ensure_currencies_table()
_ensure_player_num_attrs_table()
_ensure_lottery_tables()
_ensure_quest_state_tables()
_ensure_items_table()
_ensure_souls_table()
migrate_invalid_soul_lottery_items()
_ensure_mails_table()
_ensure_library_table()
_ensure_tasks_table()
_ensure_story_progress_table()
_ensure_player_settings_table()
_ensure_active_sign_table()
_ensure_player_state_json_table()
_ensure_battle_instances_table()
_ensure_equipment_instances_table()
_ensure_maze_instances_table()
