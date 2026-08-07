"""
Soul Tide TCP game server.

Wire format:
    [total length:u32 LE] [message id:u32 LE] [order:u32 LE] [body]

Known-good login and initialization response bodies are loaded from the saved
official capture instead of being approximated with an incompatible TLV shape.
"""

import hashlib
import json
import logging
import os
import random
import re
import socket
import struct
import threading
import time
from collections import defaultdict
from logging.handlers import RotatingFileHandler

import storage
import protocol_codec
import module_handlers
import module_rules


LOG_DIR = os.environ.get("SOULTIDE_LOG_DIR", os.path.dirname(os.path.abspath(__file__)))
OFFLINE_RESPONSE_FIXTURE_PATH = os.environ.get(
    "SOULTIDE_RESPONSE_FIXTURE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "tcp_offline_responses.json"),
)
TCP_PORT = int(os.environ.get("SOULTIDE_TCP_PORT", "51121"))
_BIND_HOST = os.environ.get(
    "SOULTIDE_BIND_HOST",
    "127.0.0.1" if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else "0.0.0.0",
).strip()
STORY_CONFIG_PATH = os.environ.get(
    "SOULTIDE_STORY_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "soul_new_story_config.json"),
)
COMPANION_RULES_PATH = os.environ.get(
    "SOULTIDE_COMPANION_RULES_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "companion_rules.json"),
)
LIBRARY_NEWS_REWARDS_PATH = os.environ.get(
    "SOULTIDE_LIBRARY_NEWS_REWARDS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "library_news_rewards.json"),
)
TASK_REWARDS_PATH = os.environ.get(
    "SOULTIDE_TASK_REWARDS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "task_rewards.json"),
)
LOTTERY_ACTIONS_PATH = os.environ.get(
    "SOULTIDE_LOTTERY_ACTIONS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "lottery_actions.json"),
)
LOTTERY_TIER_CONFIG_PATH = os.environ.get(
    "SOULTIDE_LOTTERY_TIER_CONFIG_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "analysis", "lottery_tier_config_5392.json",
    ),
)
LOTTERY_TIER_OVERRIDES_PATH = os.environ.get(
    "SOULTIDE_LOTTERY_TIER_OVERRIDES_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "analysis",
        "lottery_runtime_overrides_5392.json",
    ),
)
LOTTERY_DROP_CONFIG_PATH = os.environ.get(
    "SOULTIDE_LOTTERY_DROP_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "lottery_drop_config.json"),
)
BATTLE_CONFIG_PATH = os.environ.get(
    "SOULTIDE_BATTLE_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "battle_config.json"),
)
WORLD_BOSS_CONFIG_PATH = os.environ.get(
    "SOULTIDE_WORLD_BOSS_CONFIG_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "analysis",
        "decompiled_all",
        "textasset_00456_CfgWorldBossTable.lua.bi",
    ),
)
MODULE_CONFIG_PATH = os.environ.get(
    "SOULTIDE_MODULE_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "module_config.json"),
)
SOUL_GROWTH_CONFIG_PATH = os.environ.get(
    "SOULTIDE_SOUL_GROWTH_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "soul_growth_config.json"),
)
SOUL_BOOK_UNLOCKS_PATH = os.environ.get(
    "SOULTIDE_SOUL_BOOK_UNLOCKS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "soul_book_unlocks.json"),
)
LIBRARY_UNLOCK_CONFIG_PATH = os.environ.get(
    "SOULTIDE_LIBRARY_UNLOCK_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "library_unlock_config_5392.json"),
)
MAZE_CHALLENGE_BONUS_PATH = os.environ.get(
    "SOULTIDE_MAZE_CHALLENGE_BONUS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis", "maze_challenge_bonus.json"),
)
MAX_FRAME_SIZE = 2 * 1024 * 1024


def _load_player_config_ids(filename):
    """Read every cosmetic ID from the client table used by this server."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "analysis", "decompiled_all", filename
    )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ()
    return tuple(sorted({int(value) for value in re.findall(r"\bId\s*=\s*(\d+)", text)}))


# SettingModule.ReloadData checks these lists against every client-side config
# row.  Populate them from the same extracted tables instead of the small
# unlock lists present in the captured official login response.
ALL_HEAD_ICON_IDS = _load_player_config_ids("textasset_01637_CfgPlayerHeadIconTable.lua.bi")
ALL_AVATAR_FRAME_IDS = _load_player_config_ids("textasset_01645_CfgPlayerAvatarFrameTable.lua.bi")
ALL_EQUIP_SKIN_IDS = _load_player_config_ids("textasset_03117_CfgSoulPaintingSkinTable.lua.bi")


def _normalized_equip_skin_map(value):
    result = {}
    if isinstance(value, dict):
        for raw_cid, raw_state in value.items():
            try:
                result[int(raw_cid)] = int(raw_state)
            except (TypeError, ValueError):
                continue
    for skin_cid in ALL_EQUIP_SKIN_IDS:
        # State 0 means unlocked; state 1 means currently using the skin.
        result.setdefault(skin_cid, 0)
    return result

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TCP] %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(LOG_DIR, "tcp_server.log"),
            maxBytes=10 * 1024 * 1024,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tcp_server")


# net_user
VALIDATE_UUID = 3802
CHOOSE_ROLE = 3803
CREATE_ROLE = 3804
PING = 3805
LOGOUT = 3806
VALIDATE_UUID_RESULT = 3807
CHOOSE_ROLE_RESULT = 3808
CREATE_ROLE_RESULT = 3809
PANG = 3810
LOGOUT_RESULT = 3811
NOTIFY_SERVER_STATUS = 3812
HANDSEL_SOUL = 3814
HANDSEL_SOUL_RESULT = 3815
RECONNECT = 3816
RECONNECT_RESULT = 3817

# net_player / net_gameToCenter
LOAD_PLAYER = 3902
LOAD_PLAYER_RESULT = 3910
CHANGE_SHOW_SOUL = 3907
CHANGE_SHOW_SOUL_RESULT = 3915
NOTIFY_NUM_ATTR = 3924
HEARTBEAT = 106

# net_girl - companion screen
GET_GIRLS = 1902
EXIT_GIRLS = 1903
GET_GIRLS_RESULT = 1904
EXIT_GIRLS_RESULT = 1905
GIVE_GIFT = 1906
GIVE_GIFT_RESULT = 1907
GET_SOUL_OATH = 1912
GET_SOUL_OATH_RESULT = 1914
CONNECTIVE = 1911
CONNECTIVE_RESULT = 1913
FONDLE = 1908
FONDLE_RESULT = 1909
NOTIFY_FONDLE_RECOVERY = 1910
START_DATING = 1502
START_DATING_RESULT = 1503
NOTIFY_DATING_END = 1504
NOTIFY_DATING = 1505
UPDATE_SOUL = 4617
NOTIFY_REPETITION_UNLOCK_SOUL = 4618
NOTIFY_ITEM_CHANGE = 4102
GET_MAILS = 4502
READ_MAIL = 4503
PICK_UP_MAIL = 4504
DELETE_MAIL = 4505
GET_MAILS_RESULT = 4506
READ_MAIL_RESULT = 4507
PICK_UP_MAIL_RESULT = 4508
DELETE_MAIL_RESULT = 4509
NOTIFY_NEW_MAIL = 4510
OPEN_LIBRARY = 2602
VIEW_NEWS_BOOK = 2604
GET_NEWS_BOOK_REWARDS = 2606
OPEN_LIBRARY_RESULT = 2603
VIEW_NEWS_BOOK_RESULT = 2605
GET_NEWS_BOOK_REWARDS_RESULT = 2607
LOTTERY_DRAW = 2402
LOTTERY_DRAW_RESULT = 2403
GET_LOTTERY_HISTORY = 2404
GET_LOTTERY_HISTORY_RESULT = 2405

# net_dress
WEAR_DRESS = 1402
VIEW_DRESS = 1403
WEAR_DRESS_RESULT = 1404
VIEW_DRESS_RESULT = 1405

# net_soulNewStory
EXPERIENCE_STORY_CHAPTER = 3402
EXPERIENCE_STORY_CHAPTER_RESULT = 3403
NOTIFY_COMPLETE_STORY_CHAPTER = 3404
NOTIFY_START_FIGHT = 2903

# net_maze
ENTER_MAZE = 1302
ENTER_MAZE_RESULT = 1309
MAZE_SETTLEMENT = 1303
MAZE_SETTLEMENT_RESULT = 1310
SAVE_MAZE = 1304
SAVE_MAZE_RESULT = 1311
RESTORE_MAZE = 1305
RESTORE_MAZE_RESULT = 1312
REVIVE_MAZE = 1306
REVIVE_MAZE_RESULT = 1313
UPLOAD_MAZE_QUEST = 1307
UPLOAD_MAZE_QUEST_RESULT = 1314
UPLOAD_MAZE_ALIEN = 1308
UPLOAD_MAZE_ALIEN_RESULT = 1315
OPEN_HIDDEN_MAZE = 1319
OPEN_HIDDEN_MAZE_RESULT = 1322
BUY_MAZE_COUNT = 1320
BUY_MAZE_COUNT_RESULT = 1323
MOP_UP = 1321
MOP_UP_RESULT = 1324
ABANDON_MAZE = 1326
ABANDON_MAZE_RESULT = 1327
ENTER_ABYSS_MAZE = 1328
ENTER_ABYSS_MAZE_RESULT = 1329
ENTER_HIDDEN_MAZE = 1331
ENTER_HIDDEN_MAZE_RESULT = 1332
UPLOAD_MAZE_MONSTER_UNLOCK = 1333
UPLOAD_MAZE_MONSTER_UNLOCK_RESULT = 1334
ENTER_ILLUSION_MAZE = 1335
ENTER_ILLUSION_MAZE_RESULT = 1336
ENTER_TEST_MAZE = 1338
ENTER_TEST_MAZE_RESULT = 1339
ILLUSION_MOP_UP = 1340
ILLUSION_MOP_UP_RESULT = 1341
QUICK_CHALLENGE = 1343
QUICK_CHALLENGE_RESULT = 1344

# net_dialog
SELECT_DIALOG = 1602
SELECT_DIALOG_RESULT = 1603
NOTIFY_OPEN_DIALOG = 1604

# net_quest
COMMIT_QUEST = 4302
COMMIT_QUEST_RESULT = 4304
GIVE_UP_QUEST = 4303
GIVE_UP_QUEST_RESULT = 4305
NOTIFY_UPDATE_QUEST = 4306
NOTIFY_FINISH_QUEST_LIST = 4307
UNLOCK_CHAPTER_TASKS = 4310
UNLOCK_CHAPTER_TASKS_RESULT = 4311

# net_active
SIGN = 3704
SIGN_RESULT = 3705
LUCK_DRAW = 3707
LUCK_DRAW_RESULT = 3709
GET_LUCK_DRAW_HISTORY = 3708
GET_LUCK_DRAW_HISTORY_RESULT = 3710
GET_LV_REACH_REWARDS = 3711
GET_LV_REACH_REWARDS_RESULT = 3713
GET_LV_REACH_REWARD = 3712
GET_LV_REACH_REWARD_RESULT = 3714
GET_REFUNDS_GIFT_PACKS = 3715
GET_REFUNDS_GIFT_PACKS_RESULT = 3716

# net_player
SAVE_SETTING = 3940
SAVE_SETTING_RESULT = 3941
UPDATE_BASE_INFO = 3918
TRIGGER_GUIDE = 3908
TRIGGER_GUIDE_RESULT = 3916
REFRESH_READ_POINT = 3909
REFRESH_READ_POINT_RESULT = 3917
SAVE_SHOW_COLLECT_ITEMS = 3945
SAVE_SHOW_COLLECT_ITEMS_RESULT = 3946
USE_EQUIP_SKIN = 3954
USE_EQUIP_SKIN_RESULT = 3955
NOTIFY_EQUIP_SKIN_UPDATE = 3956
SAVE_PLAYER_SETTING = 3960
SAVE_PLAYER_SETTING_RESULT = 3961
DRESS_UP_ROTATE_SWITCH = 3962
DRESS_UP_ROTATE_SWITCH_RESULT = 3964
DRESS_UP_ROTATE_LIST = 3963
DRESS_UP_ROTATE_LIST_RESULT = 3965

# net_item
SELL_ITEM = 4002
SELL_ITEM_RESULT = 4005
USE_ITEM = 4003
USE_ITEM_RESULT = 4006
DESTROY_ITEM = 4004
DESTROY_ITEM_RESULT = 4007
EXCHANGE = 4008
EXCHANGE_RESULT = 4010
EXCHANGE_BATCH = 4009
EXCHANGE_BATCH_RESULT = 4011
LOCK_EQUIPMENT = 4012
LOCK_EQUIPMENT_RESULT = 4013
OPTIONAL_GIFT = 4014
OPTIONAL_GIFT_RESULT = 4015

# net_soul
UNLOCK_SOUL = 4602
UNLOCK_SOUL_RESULT = 4609
USE_SOUL_EXP_ITEM = 4603
USE_SOUL_EXP_ITEM_RESULT = 4610
EVOLUTION = 4604
EVOLUTION_RESULT = 4611
ACTIVE_TALENT = 4605
ACTIVE_TALENT_RESULT = 4612
ACTIVE_TALENT_GROUP = 4606
ACTIVE_TALENT_GROUP_RESULT = 4613
UNLOCK_SKILL_GROUP = 4607
UNLOCK_SKILL_GROUP_RESULT = 4614
ACTIVATION_SKILL_STRENGTHEN = 4608
ACTIVATION_SKILL_STRENGTHEN_RESULT = 4615
ACTIVE_SPECIAL_SPIRIT = 4619
ACTIVE_SPECIAL_SPIRIT_RESULT = 4620

# net_player remaining
DISBIND_ROLE = 3903
DISBIND_ROLE_RESULT = 3911
CHANGE_DATA = 3904
CHANGE_DATA_RESULT = 3912
GET_PLAYER_INFO = 3905
GET_PLAYER_INFO_RESULT = 3913
SEND_GIFT_CODE = 3906
SEND_GIFT_CODE_RESULT = 3914
BUY_ADVANCE_LEVEL_CHASE = 3947
BUY_ADVANCE_LEVEL_CHASE_RESULT = 3948

# net_shop
SHOP_BUY = 1202
SHOP_BUY_RESULT = 1204
SHOP_REFRESH = 1203
SHOP_REFRESH_RESULT = 1205

# net_centerFriend
REMOVE_FRIENDS = 100302
REMOVE_FRIENDS_RESULT = 100310
APPLY_FRIENDS = 100303
APPLY_FRIENDS_RESULT = 100311
DEAL_WITH_APPLY = 100304
DEAL_WITH_APPLY_RESULT = 100312
ADD_BLACKLIST = 100305
ADD_BLACKLIST_RESULT = 100313
REMOVE_BLACKLIST = 100306
REMOVE_BLACKLIST_RESULT = 100314
SEARCH_PLAYER = 100307
SEARCH_PLAYER_RESULT = 100315
SET_REMARK = 100308
SET_REMARK_RESULT = 100316
RECOMMEND_FRIENDS = 100309
RECOMMEND_FRIENDS_RESULT = 100317

# net_gameToCenter
REGISTER_SIMPLE_PLAYER = 102
REGISTER_SIMPLE_PLAYER_RESULT = 111
CHANGE_PLAYER_NAME = 103
CHANGE_PLAYER_NAME_RESULT = 112
LOAD_CENTER_PLAYER = 104
LOAD_CENTER_PLAYER_RESULT = 113
OFFLINE_NOTIFY = 105
UPLOAD_SIMPLE_PLAYER = 107
UPLOAD_RANK_SCORE = 108

# net_fishing
FISHING = 6702
FISHING_RESULT = 6705
FISHING_CONFIRM = 6703
FISHING_CONFIRM_RESULT = 6706
ILLEGAL_FISHING = 6704
ILLEGAL_FISHING_RESULT = 6707
EXCHANGE_FISH = 6709
EXCHANGE_FISH_RESULT = 6710
EXCHANGE_FISH_BY_TYPE = 6711
EXCHANGE_FISH_BY_TYPE_RESULT = 6712
AUTO_FISHING = 6713
AUTO_FISHING_RESULT = 6715
FISHING_DRAW_REWARDS = 6714
FISHING_DRAW_REWARDS_RESULT = 6716

# net_fishingActivity
ACTIVITY_FISHING = 9602
ACTIVITY_FISHING_RESULT = 9609
ACTIVITY_FISHING_CONFIRM = 9603
ACTIVITY_FISHING_CONFIRM_RESULT = 9610
ACTIVITY_GET_AUTO_REWARDS = 9604
ACTIVITY_GET_AUTO_REWARDS_RESULT = 9611
ACTIVITY_UP_ROLE = 9605
ACTIVITY_UP_ROLE_RESULT = 9612
ACTIVITY_UP_SKILL = 9606
ACTIVITY_UP_SKILL_RESULT = 9613
ACTIVITY_UP_ACTION = 9607
ACTIVITY_UP_ACTION_RESULT = 9614
ACTIVITY_GET_STORY_REWARDS = 9608
ACTIVITY_GET_STORY_REWARDS_RESULT = 9615

# net_lunaBattleLine
GARRISON = 5602
GARRISON_RESULT = 5606
GET_ASSISTS = 5603
GET_ASSISTS_RESULT = 5607
REFRESH_ASSIST = 5604
REFRESH_ASSIST_RESULT = 5608
GET_STRENGTHEN_SOUL_PREFAB = 5605
GET_STRENGTHEN_SOUL_PREFAB_RESULT = 5609
ENTER_FORT_MAZE = 5610
ENTER_FORT_MAZE_RESULT = 5613
ENTER_SEAL_MAZE = 5611
ENTER_SEAL_MAZE_RESULT = 5614
ENTER_STRENGTHEN_MAZE = 5612
ENTER_STRENGTHEN_MAZE_RESULT = 5615

# net_guild
REFRESH_RED_POINT = 7404
REFRESH_RED_POINT_RESULT = 7407

# net_guildChallenge
GET_GUILD_SCORE = 7505
GET_GUILD_SCORE_RESULT = 7509

# net_centerGuild
GET_GUILD_TRAINING = 100921
GET_GUILD_TRAINING_RESULT = 100941

CAPTURE_ROLE_ID = storage.DEFAULT_ROLE_ID
CAPTURE_ROLE_NAME = storage.DEFAULT_ROLE_NAME
CAPTURE_ROLE_LEVEL = storage.DEFAULT_ROLE_LEVEL


class TLV:
    @staticmethod
    def encode(tag, value):
        if isinstance(value, str):
            data = value.encode("utf-8")
        elif isinstance(value, int):
            data = str(value).encode("ascii")
        else:
            data = bytes(value)
        if len(data) > 255:
            return bytes(
                [tag, 0x82, (len(data) >> 8) & 0xFF, len(data) & 0xFF]
            ) + data
        return bytes([tag, len(data)]) + data


def encode_msg(msg_id, body=b"", order=0):
    body = bytes(body)
    return struct.pack("<III", 12 + len(body), msg_id, order) + body


def recv_exact(conn, size, timeout=30):
    conn.settimeout(timeout)
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return bytes(data) if data else None
        data.extend(chunk)
    return bytes(data)


def read_msg(conn, timeout=30):
    """Read one complete frame and return (message_id, order, body)."""
    header = recv_exact(conn, 12, timeout)
    if header is None:
        return None, None, None
    if len(header) != 12:
        raise ConnectionError(f"short frame header: {len(header)} bytes")

    total_length, msg_id, order = struct.unpack("<III", header)
    if total_length < 12 or total_length > MAX_FRAME_SIZE:
        raise ValueError(
            f"invalid frame length={total_length}, msg_id={msg_id}, "
            f"order={order}, header={header.hex()}"
        )

    body_length = total_length - 12
    body = recv_exact(conn, body_length, timeout) if body_length else b""
    if body is None or len(body) != body_length:
        raise ConnectionError(
            f"short frame body: expected={body_length}, "
            f"actual={0 if body is None else len(body)}"
        )
    return msg_id, order, body


def server_status_body():
    return b"\x5f" + struct.pack("<I", int(time.time())) + b"\x51\x08"


def pang_body():
    return b"\x5f" + struct.pack("<I", int(time.time()))


def extract_tlv_strings(body):
    values = []
    offset = 0
    while offset + 2 <= len(body):
        tag = body[offset]
        if tag != 0xA1:
            offset += 1
            continue
        length = body[offset + 1]
        header_length = 2
        if length == 0x82 and offset + 4 <= len(body):
            length = struct.unpack_from(">H", body, offset + 2)[0]
            header_length = 4
        start = offset + header_length
        end = start + length
        if end > len(body):
            break
        values.append(body[start:end].decode("utf-8", errors="replace"))
        offset = end
    return values


COMPACT_UINT_WIDTHS = {
    0x50: 0,
    0x51: 1,
    0x53: 2,
    0x57: 3,
    0x5F: 4,
}


def decode_compact_uint(body, offset=0):
    if offset >= len(body):
        raise ValueError("missing compact integer marker")
    marker = body[offset]
    width = COMPACT_UINT_WIDTHS.get(marker)
    if width is None:
        raise ValueError(f"unsupported compact integer marker 0x{marker:02x}")
    end = offset + 1 + width
    if end > len(body):
        raise ValueError("truncated compact integer")
    value = int.from_bytes(body[offset + 1 : end], "little") if width else 0
    return value, end


def encode_compact_uint(value):
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("compact integer must fit in uint32")
    if value == 0:
        return b"\x50"
    for limit, marker, width in (
        (0xFF, 0x51, 1),
        (0xFFFF, 0x53, 2),
        (0xFFFFFF, 0x57, 3),
        (0xFFFFFFFF, 0x5F, 4),
    ):
        if value <= limit:
            return bytes([marker]) + value.to_bytes(width, "little")
    raise AssertionError("unreachable compact integer width")


def encode_signed_protocol_int(value):
    if value == -1:
        return b"\x5f\xff\xff\xff\xff"
    return encode_compact_uint(value)


def decode_compact_uint_list(body, offset=0):
    if offset >= len(body):
        raise ValueError("missing list marker")
    marker = body[offset]
    if marker == 0xD0:
        return [], offset + 1
    width = COMPACT_UINT_WIDTHS.get(marker - 0x80)
    if width is None or width == 0:
        raise ValueError(f"unsupported list marker 0x{marker:02x}")
    end = offset + 1 + width
    if end > len(body):
        raise ValueError("truncated list count")
    count = int.from_bytes(body[offset + 1 : end], "little")
    values = []
    offset = end
    for _ in range(count):
        value, offset = decode_compact_uint(body, offset)
        values.append(value)
    return values, offset


def decode_select_dialog_request(body):
    select_index, offset = decode_compact_uint(body)
    skip_indexes, offset = decode_compact_uint_list(body, offset)
    if offset != len(body):
        raise ValueError("trailing select dialog request bytes")
    if select_index == 0:
        raise ValueError("dialog select index must be positive")
    return select_index, skip_indexes


def encode_story_completion_notify(
    story_cid, chapter_index, favor_level=37, is_all_complete=True
):
    # SoulNewStoryPOD fields: cid=1, unlockChapters=2, isAllComplete=3.
    story_pod = (
        b"\xc1\x03"
        + b"\x51\x01"
        + encode_compact_uint(story_cid)
        + b"\x51\x02\xc1\x01"
        + encode_compact_uint(chapter_index)
        + b"\x01"
        + b"\x51\x03"
        + (b"\x01" if is_all_complete else b"\x00")
    )
    empty_rewards = b"\xd0"
    favor = encode_compact_uint(favor_level)
    zero = encode_compact_uint(0)
    return story_pod + empty_rewards + favor + zero + favor + zero


try:
    with open(STORY_CONFIG_PATH, "r", encoding="utf-8") as file:
        STORY_CONFIG = json.load(file)
    log.info("Loaded %d new-story definitions", len(STORY_CONFIG))
except Exception as exc:
    STORY_CONFIG = {}
    log.warning("New-story config is unavailable: %s", exc)

try:
    with open(COMPANION_RULES_PATH, "r", encoding="utf-8") as file:
        COMPANION_RULES = json.load(file)["rules"]
    log.info(
        "Loaded companion rules: gifts=%d favor=%d",
        len(COMPANION_RULES["gifts"]),
        len(COMPANION_RULES["soul_favor"]),
    )
except Exception as exc:
    COMPANION_RULES = {}
    log.warning("Companion rules are unavailable: %s", exc)

try:
    with open(LIBRARY_NEWS_REWARDS_PATH, "r", encoding="utf-8") as file:
        LIBRARY_NEWS_REWARDS = json.load(file)["rewards"]
    log.info("Loaded %d library news rewards", len(LIBRARY_NEWS_REWARDS))
except Exception as exc:
    LIBRARY_NEWS_REWARDS = {}
    log.warning("Library news rewards are unavailable: %s", exc)

try:
    with open(TASK_REWARDS_PATH, "r", encoding="utf-8") as file:
        TASK_REWARDS = json.load(file)["tasks"]
    log.info("Loaded %d task rewards", len(TASK_REWARDS))
except Exception as exc:
    TASK_REWARDS = {}
    log.warning("Task rewards are unavailable: %s", exc)

try:
    with open(MAZE_CHALLENGE_BONUS_PATH, "r", encoding="utf-8") as file:
        MAZE_CHALLENGE_BONUS = json.load(file)["chapters"]
    log.info("Loaded %d maze challenge bonus chapters", len(MAZE_CHALLENGE_BONUS))
except Exception as exc:
    MAZE_CHALLENGE_BONUS = {}
    log.warning("Maze challenge bonus config is unavailable: %s", exc)

try:
    with open(LOTTERY_ACTIONS_PATH, "r", encoding="utf-8") as file:
        LOTTERY_ACTIONS = json.load(file)
    log.info("Loaded %d lottery actions", len(LOTTERY_ACTIONS))
except Exception as exc:
    LOTTERY_ACTIONS = {}
    log.warning("Lottery actions are unavailable: %s", exc)

try:
    LOTTERY_TIER_CONFIG = storage.load_lottery_tier_config(
        LOTTERY_TIER_CONFIG_PATH,
        LOTTERY_TIER_OVERRIDES_PATH,
    )
    log.info(
        "Loaded lottery tiers: shows=%d upGroups=%d",
        len(LOTTERY_TIER_CONFIG.get("shows", {})),
        len(LOTTERY_TIER_CONFIG.get("upGroups", {})),
    )
except Exception as exc:
    LOTTERY_TIER_CONFIG = dict(storage.EMPTY_LOTTERY_TIER_CONFIG)
    log.warning("Lottery tier config is unavailable: %s", exc)

try:
    with open(LOTTERY_DROP_CONFIG_PATH, "r", encoding="utf-8") as file:
        LOTTERY_DROP_CONFIG = json.load(file)["drops"]
    log.info("Loaded %d lottery drop configs", len(LOTTERY_DROP_CONFIG))
except Exception as exc:
    LOTTERY_DROP_CONFIG = {}
    log.warning("Lottery drop config is unavailable: %s", exc)


def _load_world_boss_config(path):
    """Read BossId -> MonsterTeam from the extracted official Lua table."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            source = file.read()
    except OSError as exc:
        log.warning("World Boss config is unavailable: %s", exc)
        return {}
    # Every keyed row starts with MonsterTeam, so matching the row prefix
    # avoids parsing nested reward arrays while retaining the official IDs.
    rows = re.findall(
        r"\[(\d+)\]\s*=\s*\{\s*MonsterTeam\s*=\s*(\d+)",
        source,
        flags=re.MULTILINE,
    )
    result = {str(int(boss_id)): {"MonsterTeam": int(team_id)} for boss_id, team_id in rows}
    log.info("Loaded World Boss config: %d bosses", len(result))
    return result


WORLD_BOSS_CONFIG = _load_world_boss_config(WORLD_BOSS_CONFIG_PATH)

try:
    with open(BATTLE_CONFIG_PATH, "r", encoding="utf-8") as file:
        BATTLE_CONFIG = json.load(file)
    log.info(
        "Loaded battle config: teams=%d monsters=%d souls=%d",
        len(BATTLE_CONFIG.get("teams", {})),
        len(BATTLE_CONFIG.get("monsters", {})),
        len(BATTLE_CONFIG.get("souls", {})),
    )
    storage.configure_battle_rewards(
        BATTLE_CONFIG.get("mazeInstances", {}),
        BATTLE_CONFIG.get("dropLibraries", {}),
    )
    storage.configure_battle_rules(
        BATTLE_CONFIG.get("skills", {}),
        BATTLE_CONFIG.get("skillDetails", {}),
        BATTLE_CONFIG.get("skillFunctions", {}),
        BATTLE_CONFIG.get("buffs", {}),
        BATTLE_CONFIG.get("searchTargets", {}),
        BATTLE_CONFIG.get("buffGroupRelations", {}),
    )
    storage.configure_fishing_activity(BATTLE_CONFIG.get("fishingActivity", {}))
except Exception as exc:
    BATTLE_CONFIG = {}
    log.warning("Battle config is unavailable: %s", exc)


def configured_mainline_maze_ids():
    """Return unpacked normal-story maze IDs (chapter type 2)."""
    return sorted(
        int(maze_id)
        for maze_id, data in BATTLE_CONFIG.get("mazeInstances", {}).items()
        if int(data.get("mazeType", 0) or 0) == 1
        and int(data.get("chapterType", 0) or 0) == 2
    )


VALID_OPERATION_EVENT_IDS = frozenset({
    211001001, 211001002, 211001003, 211001004, 211104001, 211202001,
    211001005, 211222001, 211222002, 211222003, 220127001, 220127002,
    220127003, 220127004, 220331001, 220331002, 220428001, 220630001,
    230100119, 230119001, 230223001, 250822001, 250822002,
})

try:
    with open(MODULE_CONFIG_PATH, "r", encoding="utf-8") as file:
        MODULE_CONFIG = json.load(file)
    storage.configure_sign_in(
        MODULE_CONFIG.get("sign_in", {}).get("SignInTable", {})
    )
    log.info(
        "Loaded sign-in config: %d rows",
        len(MODULE_CONFIG.get("sign_in", {}).get("SignInTable", {})),
    )
except Exception as exc:
    log.warning("Sign-in config is unavailable: %s", exc)

try:
    with open(SOUL_GROWTH_CONFIG_PATH, "r", encoding="utf-8") as file:
        SOUL_GROWTH_CONFIG = json.load(file)
    log.info(
        "Loaded soul growth config: quality=%d talent=%d skillGroup=%d",
        len(SOUL_GROWTH_CONFIG.get("quality", {})),
        len(SOUL_GROWTH_CONFIG.get("talent", {})),
        len(SOUL_GROWTH_CONFIG.get("skillGroup", {})),
    )
except Exception as exc:
    SOUL_GROWTH_CONFIG = {}
    log.warning("Soul growth config is unavailable: %s", exc)

try:
    with open(SOUL_BOOK_UNLOCKS_PATH, "r", encoding="utf-8") as file:
        SOUL_BOOK_UNLOCKS = json.load(file).get("souls", {})
    log.info("Loaded %d soul-book unlock groups", len(SOUL_BOOK_UNLOCKS))
except Exception as exc:
    SOUL_BOOK_UNLOCKS = {}
    log.warning("Soul-book unlock config is unavailable: %s", exc)

try:
    with open(LIBRARY_UNLOCK_CONFIG_PATH, "r", encoding="utf-8") as file:
        LIBRARY_UNLOCK_CONFIG = json.load(file)
    log.info(
        "Loaded 5392 library unlock config: dresses=%d souls=%d stories=%d cg=%d",
        len(LIBRARY_UNLOCK_CONFIG.get("dressCids", [])),
        len(LIBRARY_UNLOCK_CONFIG.get("souls", {})),
        len(LIBRARY_UNLOCK_CONFIG.get("townStory", [])),
        len(LIBRARY_UNLOCK_CONFIG.get("townStoryCG", [])),
    )
except Exception as exc:
    LIBRARY_UNLOCK_CONFIG = {}
    log.warning("Library unlock config is unavailable: %s", exc)


# Official CfgUpgradeBigBattleTable values extracted from the shipped game
# data.  The client uses the reward ID to render this table, while the server
# owns the level gate and the actual grant operation.
LEVEL_REACH_REWARDS = {
    1: {"target_level": 5, "rewards": [(10302, 80), (11402, 50), (1, 100000), (10711, 80), (10712, 80), (10713, 80), (10714, 80)]},
    2: {"target_level": 10, "rewards": [(10302, 180), (11402, 120), (1, 250000), (10501, 100), (20303, 80)]},
    3: {"target_level": 15, "rewards": [(10302, 280), (11402, 180), (1, 400000), (10711, 100), (10712, 100), (10713, 100), (10714, 100)]},
    4: {"target_level": 20, "rewards": [(10302, 400), (11402, 280), (1, 650000), (10501, 200), (20303, 100)]},
    5: {"target_level": 25, "rewards": [(10303, 180), (11403, 120), (1, 900000), (10711, 120), (10712, 120), (10713, 120), (10714, 120)]},
    6: {"target_level": 30, "rewards": [(10303, 220), (11403, 150), (1, 1100000), (10501, 400), (20303, 120)]},
    7: {"target_level": 35, "rewards": [(10303, 260), (11403, 180), (1, 1300000), (10711, 160), (10712, 160), (10713, 160), (10714, 160)]},
    8: {"target_level": 40, "rewards": [(10304, 90), (11404, 60), (1, 1500000), (10501, 600), (20303, 160)]},
    9: {"target_level": 45, "rewards": [(10304, 100), (11404, 70), (1, 1800000), (10711, 200), (10712, 200), (10713, 200), (10714, 200)]},
    10: {"target_level": 50, "rewards": [(10304, 120), (11404, 80), (1, 2000000), (10501, 800), (20303, 200)]},
}
LEVEL_REACH_REWARD_MIGRATION = "official_cfg_upgrade_big_battle_v1"

BATTLE_TROOP_ATTACK = 1
BATTLE_TROOP_DEFEND = 2

LOCAL_BATTLE_ENTRY_TYPES = {
    3002: 1, 3102: 2, 3202: 3, 4703: 4, 4704: 4,
    5202: 2, 5502: 2, 6403: 2, 6404: 3, 6502: 2, 6503: 3,
    6910: 2, 7103: 3, 7502: 2, 7802: 2, 7902: 2,
    8002: 2, 8003: 2, 8004: 2, 8005: 3, 9002: 2,
    9402: 2, 9405: 2, 9522: 2, 9707: 2,
}

GIFT_FAVOR_MULTIPLIERS = (100, 170, 200, 200, 200, 100, 100, 170, 170, 170, 140, 140)


def favor_level_for(soul_id, favor, oath_activated=False):
    level = 1
    threshold = 0
    for row in sorted(
        (
            row
            for row in COMPANION_RULES.get("soul_favor", {}).values()
            if row.get("SoulID") == soul_id
        ),
        key=lambda row: int(row.get("FavorDegree", 0) or 0),
    ):
        degree = int(row.get("FavorDegree", 0) or 0)
        if degree < 1 or degree > (50 if oath_activated else 40):
            continue
        value = row.get("FavorValue")
        if isinstance(value, int):
            threshold = value
        if threshold <= favor:
            level = degree
    return level


def fondle_action_for(soul_id, favor_level):
    rows = []
    for table_name in ("soul_action_groups_1", "soul_action_groups_2"):
        rows.extend(COMPANION_RULES.get(table_name, {}).values())
    for row in rows:
        level_range = row.get("FavorLevel") or []
        if (
            row.get("SoulId") == soul_id
            and row.get("Type") == 401
            and len(level_range) >= 2
            and level_range[0] <= favor_level <= level_range[1]
        ):
            actions = [value for value in row.get("MoodActionID", []) if value > 0]
            if actions:
                return actions[1] if len(actions) > 1 else actions[0]
    return 0


def _growth_pairs(value):
    if not isinstance(value, list):
        return []
    result = []
    index = 0
    while index + 1 < len(value):
        if isinstance(value[index], list):
            result.extend(_growth_pairs(value[index]))
            index += 1
            continue
        try:
            cid, quantity = int(value[index]), int(value[index + 1])
        except (TypeError, ValueError):
            index += 2
            continue
        if cid > 0 and quantity > 0:
            result.append((cid, quantity))
        index += 2
    return result


SOUL_EXP_VALUES = {
    10301: 30,
    10302: 100,
    10303: 300,
    10304: 1000,
}

# Older local clients sent the historical test material 10001. Keep that
# request decodable while the current client uses the four 103xx materials.
SOUL_EXP_VALUES[10001] = 100


def _growth_level_row(level):
    try:
        level = int(level)
    except (TypeError, ValueError):
        return None
    for row in SOUL_GROWTH_CONFIG.get("level", []):
        if isinstance(row, dict) and int(row.get("Id", 0) or 0) == level:
            return row
    return None


def _soul_level_after_exp(uid, current_level, current_exp, added_exp):
    """Apply the extracted level table and player-level gate to soul EXP."""
    player = storage.get_player(uid) or {}
    player_level = max(1, int(player.get("level", 1) or 1))
    level = max(1, int(current_level or 1))
    exp = max(0, int(current_exp or 0)) + max(0, int(added_exp or 0))
    while True:
        row = _growth_level_row(level)
        if not row:
            break
        next_exp = int(row.get("NextEXP", 0) or 0)
        next_level = level + 1
        need_player_level = int(row.get("NeedPlayerLv", next_level) or next_level)
        if next_exp <= 0 or exp < next_exp or player_level < need_player_level:
            break
        exp -= next_exp
        level = next_level
    return level, exp


def _growth_row(table_name, cid):
    table = SOUL_GROWTH_CONFIG.get(table_name, {})
    return table.get(str(int(cid))) if isinstance(table, dict) else None


def _growth_soul_row(soul_id):
    row = _growth_row("soul", soul_id)
    return row if isinstance(row, dict) else None


def _growth_quality_row(soul_id, quality_id=None):
    """Resolve a soul's quality row; quality IDs are global, not per-soul levels."""
    try:
        soul_id = int(soul_id)
        quality_id = int(quality_id or 0)
    except (TypeError, ValueError):
        return None
    direct = _growth_row("quality", quality_id) if quality_id > 0 else None
    if isinstance(direct, dict) and int(direct.get("SoulId", 0) or 0) == soul_id:
        return direct
    candidates = [
        row for row in SOUL_GROWTH_CONFIG.get("quality", {}).values()
        if isinstance(row, dict) and int(row.get("SoulId", 0) or 0) == soul_id
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda row: (int(row.get("Quality", 0) or 0), int(row.get("Id", 0) or 0)))
    return candidates[0]


def _growth_skill_belongs_to_soul(soul_id, skill_id):
    try:
        soul_id, skill_id = int(soul_id), int(skill_id)
    except (TypeError, ValueError):
        return False
    # Change skills (for example 113510) are not listed in GroupSkills.
    # Their leading skill prefix still identifies the owning soul.
    if any(
        isinstance(row, dict)
        and int(row.get("Soul", 0) or 0) == soul_id
        and skill_id in {int(value) for value in row.get("GroupSkills", []) if isinstance(value, int)}
        for row in SOUL_GROWTH_CONFIG.get("skillGroup", {}).values()
    ):
        return True
    soul_prefix = _growth_soul_prefix(soul_id)
    return bool(soul_prefix and skill_id // 1000 == soul_prefix)


def _growth_soul_prefix(soul_id):
    """Return the numeric talent prefix used by the extracted soul tables."""
    try:
        value = int(soul_id) - 20010000
    except (TypeError, ValueError):
        return 0
    return 100 + value if value > 0 else 0


def _normalized_skill_strengthens(progress):
    """Migrate the old skill->level cache to the real strengthen config IDs."""
    if not isinstance(progress, dict):
        return []
    result = []
    raw_ids = progress.get("activationSkillStrengthen")
    if isinstance(raw_ids, list):
        for raw_id in raw_ids:
            try:
                config_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if _growth_row("skillStrengthen", config_id) and config_id not in result:
                result.append(config_id)

    legacy = progress.get("skillStrengthens")
    if isinstance(legacy, dict):
        for raw_skill, raw_level in legacy.items():
            try:
                skill_id, level = int(raw_skill), max(1, int(raw_level))
            except (TypeError, ValueError):
                continue
            direct = _growth_row("skillStrengthen", skill_id)
            if direct:
                if skill_id not in result:
                    result.append(skill_id)
                continue
            candidates = []
            for key, row in SOUL_GROWTH_CONFIG.get("skillStrengthen", {}).items():
                if not isinstance(row, dict) or int(row.get("INSkill", 0) or 0) != skill_id:
                    continue
                candidates.append((int(row.get("Order", 0) or 0), int(key), row))
            candidates.sort(key=lambda item: (item[0], item[1]))
            for _order, config_id, _row in candidates[:level]:
                if config_id not in result:
                    result.append(config_id)
    return result


SOUL_ATTRIBUTE_COUNT = 91
SOUL_GROWTH_ATTRIBUTE_FIELDS = (
    (7, "LevelAtkUP"),
    (8, "LevelMAtkUP"),
    (9, "LevelMaxHpUP"),
    (10, "LevelSpeedUP"),
    (11, "LevelDefUP"),
    (12, "LevelMDefUP"),
)


def _attribute_map_from_row(row):
    """Convert an extracted AttType/AttValue row to an ID keyed map."""
    if not isinstance(row, dict):
        return {}
    result = {}
    for raw_type, raw_value in zip(row.get("AttType", []), row.get("AttValue", [])):
        try:
            attr_type = int(raw_type or 0)
            attr_value = float(raw_value or 0)
        except (TypeError, ValueError):
            continue
        if attr_type:
            result[attr_type] = result.get(attr_type, 0.0) + attr_value
    return result


def _attribute_map_from_ids(table_name, values):
    result = {}
    if not isinstance(values, list):
        return result
    table = SOUL_GROWTH_CONFIG.get(table_name, {})
    for raw_id in values:
        try:
            row = table.get(str(int(raw_id))) if isinstance(table, dict) else None
        except (TypeError, ValueError):
            continue
        for attr_type, attr_value in _attribute_map_from_row(row).items():
            result[attr_type] = result.get(attr_type, 0.0) + attr_value
    return result


def _soul_growth_rate_map(row):
    if not isinstance(row, dict):
        return {}
    result = {}
    for attr_type, field_name in SOUL_GROWTH_ATTRIBUTE_FIELDS:
        try:
            value = float(row.get(field_name, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            result[attr_type] = value
    return result


def _soul_attr_with_local_growth(
    soul_id,
    reference_pod,
    current_pod,
    current_level,
    current_quality_id,
):
    """Apply local growth deltas to the captured official soul attributes.

    The captured SoulPOD already contains talent, equipment, and other server
    effects. Rebuilding it from the small extracted config would discard those
    effects, so only the local level/quality/talent deltas are applied.
    """
    if not isinstance(reference_pod, dict) or not isinstance(current_pod, dict):
        return None
    reference_attrs = reference_pod.get("soulAttr")
    if not isinstance(reference_attrs, list):
        return None
    try:
        reference_level = max(1, int(reference_pod.get("lv", current_level) or 1))
        current_level = max(1, int(current_level or 1))
        reference_quality_id = max(1, int(reference_pod.get("qualityId", current_quality_id) or 1))
        current_quality_id = max(1, int(current_quality_id or 1))
        soul_id = int(soul_id)
    except (TypeError, ValueError):
        return None

    values = []
    for value in reference_attrs:
        try:
            values.append(float(value or 0))
        except (TypeError, ValueError):
            values.append(0.0)
    if len(values) < SOUL_ATTRIBUTE_COUNT:
        values.extend([0.0] * (SOUL_ATTRIBUTE_COUNT - len(values)))

    delta = {}
    reference_quality = _growth_quality_row(soul_id, reference_quality_id)
    current_quality = _growth_quality_row(soul_id, current_quality_id)
    reference_quality_values = _attribute_map_from_row(reference_quality)
    current_quality_values = _attribute_map_from_row(current_quality)
    for attr_type in set(reference_quality_values) | set(current_quality_values):
        delta[attr_type] = (
            current_quality_values.get(attr_type, 0.0)
            - reference_quality_values.get(attr_type, 0.0)
        )

    reference_rates = _soul_growth_rate_map(reference_quality)
    current_rates = _soul_growth_rate_map(current_quality)
    for attr_type in set(reference_rates) | set(current_rates):
        level_delta = (current_level - reference_level) * current_rates.get(attr_type, 0.0)
        quality_delta = (
            current_rates.get(attr_type, 0.0) - reference_rates.get(attr_type, 0.0)
        ) * reference_level
        delta[attr_type] = delta.get(attr_type, 0.0) + level_delta + quality_delta

    reference_talents = reference_pod.get("talentCids", [])
    current_talents = current_pod.get("talentCids", [])
    reference_talent_values = _attribute_map_from_ids("talent", reference_talents)
    current_talent_values = _attribute_map_from_ids("talent", current_talents)
    for attr_type in set(reference_talent_values) | set(current_talent_values):
        delta[attr_type] = delta.get(attr_type, 0.0) + (
            current_talent_values.get(attr_type, 0.0)
            - reference_talent_values.get(attr_type, 0.0)
        )

    reference_groups = reference_pod.get("talentGroupCids", [])
    current_groups = current_pod.get("talentGroupCids", [])
    reference_group_values = _attribute_map_from_ids("talentGroup", reference_groups)
    current_group_values = _attribute_map_from_ids("talentGroup", current_groups)
    for attr_type in set(reference_group_values) | set(current_group_values):
        delta[attr_type] = delta.get(attr_type, 0.0) + (
            current_group_values.get(attr_type, 0.0)
            - reference_group_values.get(attr_type, 0.0)
        )

    for attr_type, amount in delta.items():
        if not 1 <= attr_type <= len(values) or not amount:
            continue
        values[attr_type - 1] += amount
    return values


def local_soul_pod_for(uid, soul_id, base=None, companion=None):
    """Merge local soul state into a complete client SoulPOD."""
    companion = companion or storage.get_companion(uid, int(soul_id)) or {}
    state = storage.get_player_state_json(uid, "soul_progress") or {}
    progress = state.get(str(soul_id), {})
    reference_pod = dict(base or {})
    pod = dict(reference_pod)
    list_defaults = {
        "activationSkillStrengthen": "activationSkillStrengthen",
        "talentCids": "talents",
        "talentGroupCids": "talentGroups",
        "unlockSkillGroups": "skillGroups",
        "specialSpirit": "specialSpirit",
        "soulMemoryPieces": "soulMemoryPieces",
    }
    for pod_name, state_name in list_defaults.items():
        if state_name in progress and isinstance(progress[state_name], list):
            pod[pod_name] = list(progress[state_name])
        elif pod_name not in pod:
            pod[pod_name] = []
    if "activationSkillStrengthen" in progress or "skillStrengthens" in progress:
        pod["activationSkillStrengthen"] = _normalized_skill_strengthens(progress)
    elif "activationSkillStrengthen" not in pod:
        pod["activationSkillStrengthen"] = []
    quality_id = int(progress.get("qualityId", pod.get("qualityId", 0)) or 0)
    direct_quality_row = _growth_row("quality", quality_id) if quality_id > 0 else None
    quality_row = _growth_quality_row(soul_id, quality_id)
    if quality_row is not None and (
        direct_quality_row is None
        or int(direct_quality_row.get("SoulId", 0) or 0) != int(soul_id)
    ):
        quality_id = int(quality_row.get("Id", quality_id) or quality_id)
    oath_activated = bool(companion.get("oath_activation", 0))
    favor_level = int(companion.get("favor_level", 1))
    if not oath_activated and favor_level > 40:
        favor_level = 40
    pod.update({
        "cid": int(soul_id),
        "lv": int(companion.get("level", 1)),
        "exp": int(progress.get("exp", pod.get("exp", 0)) or 0),
        "favor": int(companion.get("favor", 0)),
        "favorLv": favor_level,
        "favorMaxLv": 50 if oath_activated else 40,
        "qualityId": max(1, quality_id),
        "dailyDislike": bool(companion.get("daily_dislike", 0)),
        "oathActivation": oath_activated,
        "mood": int(pod.get("mood", 150) or 0),
        "moodTimeInterval": int(pod.get("moodTimeInterval", 0) or 0),
        "workStatus": int(pod.get("workStatus", 0) or 0),
    })
    soul_attrs = _soul_attr_with_local_growth(
        soul_id,
        reference_pod,
        pod,
        pod["lv"],
        pod["qualityId"],
    )
    if soul_attrs is not None:
        pod["soulAttr"] = soul_attrs
    return pod


def legacy_fishing_state_for(uid):
    state = storage.get_player_state_json(uid, "legacy_fishing") or {}
    state.setdefault("pending", None)
    state.setdefault("book", {})
    state.setdefault("autoPending", [])
    state.setdefault("autoNextTime", 0)
    return state


def save_legacy_fishing_state(uid, state):
    return storage.update_player_state_json(uid, "legacy_fishing", state)


def fish_show(fish_id, count=1):
    return {"cid": int(fish_id), "num": int(count), "tag": 0}


def assist_prefab_for(formation_id):
    return {
        "id": int(formation_id), "soulCid": 20010001, "lv": 1, "exp": 0,
        "favorLv": 1, "qualityId": 1, "position": 1, "power": 0,
        "activeTalentCids": [], "activeTalentGroupCids": [], "allSkillStrengths": [],
        "allSkills": [], "customSkills": [], "pAblityIds": [], "soulMemoryPieces": [],
        "specialSpirit": [], "unlockSkillGroups": [], "equipments": {},
    }


def player_base_info_for(uid, pid=None, name=None):
    player = storage.get_player(uid) or {}
    profile = storage.get_player_state_json(uid, "player_profile") or {}
    attrs = storage.get_player_num_attrs(uid)
    wallet = storage.get_offline_wallet(uid)
    return {
        "pid": str(pid if pid is not None else uid),
        "uid": str(uid),
        "pName": str(name if name is not None else profile.get("name", player.get("role_name", "local"))),
        "pLv": int(player.get("level", 1)), "exp": 0, "power": 0,
        "guildId": 0, "leaderCid": 20010001, "showSoulCid": 20010001,
        "headIcon": 0, "avatarFrame": 0, "chatBackground": 0,
        "title": 0, "vip": 0, "vipexp": 0, "payPoint": int(attrs.get(5, 0)), "sumPay": int(wallet.get("sumPay", 0)),
        "guid": 0, "sceneID": 0, "areaId": "local", "serverId": "local",
        "channelNo": "local", "openId": "local", "intro": "", "sdkName": "local",
        "createTime": int(player.get("updated_at", 0)),
    }


def local_player_by_ref(value):
    """Resolve a local social reference by uid, role id, channel id, or name."""
    text = str(value or "").strip()
    if not text:
        return None
    rows = storage.find_players(text)
    for row in rows:
        if text in {str(row.get("uid", "")), str(row.get("role_id", "")), str(row.get("channel_uid", ""))}:
            return row
    return rows[0] if rows else None


def local_friend_pod(player, remark=""):
    if not player:
        return {}
    return {
        "id": int(player.get("role_id", 0) or 0), "pId": str(player.get("uid", "")),
        "pName": str(player.get("role_name", "local")), "remark": str(remark or ""),
        "pLv": int(player.get("level", 1) or 1), "online": True,
        "serverId": "offline-local", "createTime": int(player.get("updated_at", 0) or 0),
        "lastLoginTime": int(player.get("updated_at", 0) or 0), "type": 0,
        "guid": 0, "headIcon": 0, "avatarFrame": 0, "title": 0, "vip": 0,
    }


def seed_local_mails(uid):
    body = REPLAY.body(GET_MAILS_RESULT) if REPLAY else None
    if body is None:
        return 0
    code, mail_type, mails = protocol_codec.decode_method(GET_MAILS_RESULT, body)
    if code != 0 or mail_type != 0:
        raise ValueError("captured mail seed is not a successful all-mail response")
    return storage.seed_mails_from_snapshot(uid, mails)


def seed_local_player_attrs(uid):
    body = REPLAY.body(LOAD_PLAYER_RESULT) if REPLAY else None
    if body is None:
        return 0
    code, player = protocol_codec.decode_method(LOAD_PLAYER_RESULT, body)
    if code != 0:
        raise ValueError("captured player seed is not successful")
    return storage.seed_player_num_attrs(uid, player.get("numAttrs", {}))


def seed_local_quest_state(uid):
    body = REPLAY.body(LOAD_PLAYER_RESULT) if REPLAY else None
    if body is None:
        return 0
    code, player = protocol_codec.decode_method(LOAD_PLAYER_RESULT, body)
    if code != 0:
        raise ValueError("captured player seed is not successful")
    return storage.seed_quest_state(
        uid,
        player.get("quests", []),
        player.get("finishQuestList", []),
        player.get("failQuestList", []),
        player.get("unlockChapterTasks", []),
    )


def local_mail_types():
    return {
        cid: int(row.get("MailType") or 0)
        for cid, row in COMPANION_RULES.get("mail_templates", {}).items()
    }


def seed_local_library(uid):
    body = REPLAY.body(OPEN_LIBRARY_RESULT) if REPLAY else None
    if body is None:
        return False
    code, library = protocol_codec.decode_method(OPEN_LIBRARY_RESULT, body)
    if code != 0:
        raise ValueError("captured library seed is not successful")
    return storage.seed_library_state(uid, library)


def complete_library_state(library):
    """Project every archive entry declared by the shipped 5392 client."""
    config = LIBRARY_UNLOCK_CONFIG
    if not isinstance(library, dict) or not config:
        return library
    for field in ("newsBook", "alienEvent", "townStory", "townStoryCG"):
        values = library.setdefault(field, {})
        for cid in config.get(field, []):
            values[int(cid)] = True
    equip_star = library.setdefault("equipStar", {})
    for cid in config.get("equipStar", []):
        equip_star[int(cid)] = 5
    monster = library.setdefault("monster", {})
    for cid in config.get("monster", []):
        pod = monster.setdefault(int(cid), {"count": 1, "rewards": {}})
        if not isinstance(pod, dict):
            pod = {"count": 1, "rewards": {}}
            monster[int(cid)] = pod
        pod["count"] = max(1, int(pod.get("count", 0) or 0))
        pod.setdefault("rewards", {})
    npc = library.setdefault("npc", {})
    for raw_cid, entries in config.get("npc", {}).items():
        cid = int(raw_cid)
        pod = npc.setdefault(cid, {"entrys": [], "rewards": {}})
        pod["entrys"] = sorted(set(pod.get("entrys", [])) | {int(value) for value in entries})
        pod.setdefault("rewards", {})
    soul_by_cid = {
        int(row.get("soulCid", 0) or 0): row
        for row in library.setdefault("souls", [])
        if isinstance(row, dict)
    }
    for raw_cid, unlocks in config.get("souls", {}).items():
        cid = int(raw_cid)
        soul = soul_by_cid.get(cid)
        if soul is None:
            soul = {"soulCid": cid, "unlockPlate": {}, "newStroys": [], "datings": []}
            library["souls"].append(soul)
            soul_by_cid[cid] = soul
        plate = soul.setdefault("unlockPlate", {})
        for value in unlocks.get("unlockPlate", []):
            plate[int(value)] = True
        for field in ("newStroys", "datings"):
            soul[field] = sorted(
                set(int(value) for value in soul.get(field, []))
                | set(int(value) for value in unlocks.get(field, []))
            )
    return library


def decode_story_chapter_request(body):
    story_cid, offset = decode_compact_uint(body)
    chapter_index, offset = decode_compact_uint(body, offset)
    if offset != len(body):
        raise ValueError("trailing story chapter request bytes")
    if story_cid == 0:
        raise ValueError("storyCid must be positive")
    return story_cid, chapter_index


def overlay_player_snapshot(body, player):
    """Overlay fixed-width player fields without rebuilding the captured object."""
    if not player:
        return body, ()

    patched = bytes(body)
    changed = []

    role_id = str(player["role_id"])
    capture_role_id = CAPTURE_ROLE_ID.encode("ascii")
    role_id_bytes = role_id.encode("ascii")
    if len(role_id_bytes) != len(capture_role_id):
        log.warning(
            "  role_id overlay skipped: expected %d ASCII bytes, got %d",
            len(capture_role_id),
            len(role_id_bytes),
        )
    else:
        occurrences = patched.count(capture_role_id)
        if occurrences:
            patched = patched.replace(capture_role_id, role_id_bytes)
            changed.append(f"role_id({occurrences})")
        elif role_id_bytes != capture_role_id:
            log.warning("  role_id overlay skipped: capture value not found")

    role_name_bytes = str(player["role_name"]).encode("utf-8")
    capture_role_name = CAPTURE_ROLE_NAME.encode("utf-8")
    if len(role_name_bytes) != len(capture_role_name):
        log.warning(
            "  role_name overlay skipped: expected %d UTF-8 bytes, got %d",
            len(capture_role_name),
            len(role_name_bytes),
        )
    else:
        occurrences = patched.count(capture_role_name)
        if occurrences:
            patched = patched.replace(capture_role_name, role_name_bytes, 1)
            changed.append("role_name")
        elif role_name_bytes != capture_role_name:
            log.warning("  role_name overlay skipped: capture value not found")

    level = int(player["level"])
    level_prefix = b"\x51\x0c\x51"
    capture_level = level_prefix + bytes([CAPTURE_ROLE_LEVEL])
    if not 0 <= level <= 255:
        log.warning("  level overlay skipped: value %d is outside uint8", level)
    else:
        level_offset = patched.find(capture_level, 0, 256)
        if level_offset >= 0:
            replacement = level_prefix + bytes([level])
            patched = (
                patched[:level_offset]
                + replacement
                + patched[level_offset + len(capture_level) :]
            )
            changed.append("level")
        elif level != CAPTURE_ROLE_LEVEL:
            log.warning("  level overlay skipped: capture field not found")

    return patched, tuple(changed)


def overlay_currencies(body, uid):
    """Patch currency amounts (53-marker uint16 LE fields) from SQLite."""
    if not uid:
        return body, ()

    patched = bytes(body)
    changed = []
    currencies = storage.get_currencies(uid)

    # Map field ID → (capture default, new value, name)
    # We only overlay the first occurrence (base info section, within first 1024 bytes)
    currency_map = {
        0x09: (1000, currencies.get("gold", 1000), "gold"),
        0x15: (2912, currencies.get("souls", 0), "souls"),
    }

    for field_id, (capture_default, new_val, name) in currency_map.items():
        # Build capture pattern: 51 XX 53 [LE16 of capture_default]
        pattern = b"\x51" + bytes([field_id]) + b"\x53" + struct.pack(
            "<H", capture_default
        )
        if capture_default == new_val:
            continue  # nothing to change
        pos = patched.find(pattern, 0, 1024)
        if pos < 0:
            log.warning(
                "  currency %s overlay skipped: pattern 51 %02x 53 %04x not found",
                name,
                field_id,
                capture_default,
            )
            continue
        # The captured player POD stores these display fields as uint16. Keep
        # the database value at its configured cap, but saturate the wire value
        # instead of allowing a large local balance to crash snapshot encoding.
        wire_value = max(0, min(0xFFFF, int(new_val)))
        if wire_value != int(new_val):
            log.info("  currency %s display value saturated %d -> %d", name, new_val, wire_value)
        new_le = struct.pack("<H", wire_value)
        if new_le == patched[pos + 3 : pos + 5]:
            continue  # value already matches
        patched = patched[: pos + 3] + new_le + patched[pos + 5 :]
        changed.append(f"{name}({capture_default}->{new_val})")

    return patched, tuple(changed)


def _fishing_activity_pod(state):
    """Convert local fishing state to the typed FishingActivityPOD shape."""
    book = {}
    for key, value in (state.get("book", {}) or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            fish_id = int(value.get("fishId") or key)
            num = max(0, int(value.get("num") or 0))
            weight = max(0, int(value.get("weight") or 0))
        except (TypeError, ValueError):
            continue
        if fish_id > 0 and num > 0:
            book[fish_id] = {"fishId": fish_id, "num": num, "weight": weight}

    def integer_map(value):
        result = {}
        for key, item in (value or {}).items():
            try:
                result[int(key)] = int(item or 0)
            except (TypeError, ValueError):
                continue
        return result

    return {
        "roleLevel": max(1, int(state.get("roleLevel") or 1)),
        "skillLevel": integer_map(state.get("skillLevel")),
        "actionLevel": integer_map(state.get("actionLevel")),
        "book": book,
        "maxWeight": integer_map(state.get("maxWeight")),
        "getStoryList": [int(story_id) for story_id in state.get("getStoryList", []) if int(story_id) > 0],
        "autoFishingRewardsTime": max(0, int(state.get("autoFishingRewardsTime") or 0)),
        "totalWeight": max(0, int(state.get("totalWeight") or 0)),
    }


def overlay_fishing_activity_snapshot(player_pod, uid):
    """Place local fishing state in the DailyDupPOD used by the client module."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    state = storage.get_fishing_activity_state(uid)
    pod = _fishing_activity_pod(state)
    daily_dups = player_pod.setdefault("dailyDups", [])
    target = next(
        (
            daily_dup
            for daily_dup in daily_dups
            if isinstance(daily_dup, dict)
            and isinstance(daily_dup.get("common"), dict)
            and int(daily_dup["common"].get("cid") or 0) == 28
        ),
        None,
    )
    if target is None:
        target = {
            "common": {
                "cid": 28,
                "status": 1,
                "openCount": 1,
                "openDate": int(time.time()),
            },
            "buyCount": 0,
            "enterCount": 0,
        }
        daily_dups.append(target)
    common = target.setdefault("common", {})
    if int(common.get("status") or 0) == 0:
        common["status"] = 1
        common["openDate"] = int(time.time())
    common["openCount"] = max(1, int(common.get("openCount") or 1))
    target["fishingActivityPOD"] = pod
    return True


def overlay_home_work_snapshot(player_pod, uid):
    """Restore the daily homeland affair count used by HLWorkModule.Reload."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    player_pod["todayHomeWorkCount"] = module_handlers.home_today_work_count(uid)
    return True


def overlay_local_progress_snapshot(player_pod, uid):
    """Restore the local progress lists used by homeland and town conditions."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    for field_name in ("quickChallenge", "unlockTownEvents"):
        try:
            value = storage.get_player_state_json(uid, field_name)
        except (TypeError, ValueError, json.JSONDecodeError):
            log.warning("ignoring malformed local progress uid=%s field=%s", uid, field_name)
            continue
        if isinstance(value, list):
            normalized = []
            seen = set()
            for item in value:
                try:
                    item = int(item)
                except (TypeError, ValueError):
                    continue
                if item > 0 and item not in seen:
                    seen.add(item)
                    normalized.append(item)
            player_pod[field_name] = normalized
    return True


def overlay_mall_snapshot(player_pod, uid):
    """Project local mall purchases into both client purchase-count maps."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    state = storage.get_player_state_json(uid, "mall") or {}
    purchases = state.get("purchases") if isinstance(state, dict) else None
    if not isinstance(purchases, dict) or not purchases:
        return True

    def integer_map(value):
        result = {}
        for key, count in (value or {}).items():
            try:
                key = int(key)
                count = max(0, int(count or 0))
            except (TypeError, ValueError):
                continue
            if key > 0:
                result[key] = count
        return result

    records = integer_map(player_pod.get("mallBuyCountRecords"))
    history = integer_map(player_pod.get("mallBuyCountHistory"))
    aliases_by_source = {}
    for alias_id, source_id in module_handlers.LOCAL_MALL_ALIASES.items():
        try:
            aliases_by_source.setdefault(int(source_id), []).append(int(alias_id))
        except (TypeError, ValueError):
            continue

    for raw_id, purchase in purchases.items():
        if not isinstance(purchase, dict):
            continue
        try:
            mall_id = int(raw_id)
            count = max(0, int(purchase.get("count", 0) or 0))
        except (TypeError, ValueError):
            continue
        if mall_id <= 0:
            continue
        records[mall_id] = count
        history[mall_id] = count
        for alias_id in aliases_by_source.get(mall_id, ()):
            records[alias_id] = count
            history[alias_id] = count

    player_pod["mallBuyCountRecords"] = records
    player_pod["mallBuyCountHistory"] = history
    return True


def overlay_town_snapshot(player_pod, uid):
    """Project the authoritative TownPOD rebuilt from the local town state."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    state = module_handlers._town_state(uid)
    player_pod["townInfo"] = module_handlers._town_pod(uid, state)
    return True


def overlay_low_frequency_snapshot(player_pod, uid):
    """Overlay persistent local supply and Abyss Plus selections."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    try:
        supply = storage.get_player_state_json(uid, "daily_supply") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        supply = {}
    supply_day = module_handlers._supply_day(int(time.time()))
    player_pod["dailySupplyList"] = (
        [int(value) for value in supply.get("claimed", [])
         if isinstance(value, (int, str)) and str(value).isdigit() and int(value) in (1, 2)]
        if supply.get("day") == supply_day else []
    )

    try:
        abyss = storage.get_player_state_json(uid, "abyss_plus") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        abyss = {}
    used_runes = []
    for value in abyss.get("usedRunes", []) if isinstance(abyss, dict) else []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in used_runes:
            used_runes.append(value)
    if abyss or used_runes:
        daily_dups = player_pod.setdefault("dailyDups", [])
        target = next(
            (row for row in daily_dups if isinstance(row, dict)
             and isinstance(row.get("abyssPlusPOD"), dict)),
            None,
        )
        if target is None:
            target = {
                "common": {"cid": 16, "status": 1, "openCount": 1,
                           "openDate": int(time.time())},
                "buyCount": 0,
                "enterCount": 0,
                "abyssPlusPOD": {"levelScore": {}, "runes": [], "usedRunes": []},
            }
            daily_dups.append(target)
        target.setdefault("abyssPlusPOD", {})["usedRunes"] = used_runes
    return True


def overlay_operation_activity_snapshot(player_pod, uid):
    """Expose the locally implemented operation activities through 3910."""
    if not isinstance(player_pod, dict) or not uid:
        return False
    def op_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    now = int(time.time())
    state = storage.get_player_state_json(uid, "operation_activities") or {}
    today = time.strftime("%Y-%m-%d")
    events = (
        (211001001, 1, {"gpData": {"buyCount": {op_int(k): op_int(v) for k, v in (state.get("group_totals") or {}).items()}}}),
        # FurnitureGashaponModule.Reload dereferences this POD as soon as the
        # corresponding operation event is open; an empty event row is invalid.
        (211222002, 1, {"furnitureGachaDataPOD": {"gachaCount": 0, "gachaIdList": []}}),
        (220127002, 1, {"voteDataPOD": {"myVotes": {op_int(k): op_int(v) for k, v in (state.get("votes", {}).get("220127002") or {}).items()}}}),
        (220630001, 220630, {"turntableDataPOD": {"dailyFreeDrawCount": 1 if (state.get("turntables", {}).get("220630001", {}).get("freeDay") == today) else 0}}),
        (230223001, 1, {"cupMatchVoteDataPOD": {"tickets": op_int(state.get("cup_votes", {}).get("230223001", {}).get("tickets", 3)), "myVotes": {op_int(k): op_int(v) for k, v in (state.get("cup_votes", {}).get("230223001", {}).get("votes") or {}).items()}}}),
        (211001005, 1, {"helpNewbiesDataPOD": {"type": 2, "rookie": {"name": "", "reachedLevel": False, "useCode": bool(state.get("welcome", {}).get("useCode")), "usedInviteCode": op_int(state.get("welcome", {}).get("usedInviteCode", 0) or 0)}, "senior": {"eventTask": {op_int(k): op_int(v) for k, v in (state.get("welcome", {}).get("eventTask") or {}).items()}, "finishedTask": [op_int(v) for v in state.get("welcome", {}).get("finishedTask", [])], "inviteCode": 0}}}),
    )
    # Daily activity IDs are not entries in CfgOperateEventsControlTable.
    # Sending them as operation events makes ActiveOperationEventModule index
    # a missing config and abort PlayerModule.Load on the client.
    implemented_ids = {event_id for event_id, _data_cfg_id, _fields in events}
    # A status without its matching data POD makes ActivityUI.GetViewPath()
    # dereference nil after opening the fullscreen mask.  The captured account
    # contains old status rows for all 23 historical events but only two data
    # rows, so expose only activities that this local server can fully build.
    status = [
        row for row in (player_pod.get("opEventsStatus") or [])
        if isinstance(row, dict) and op_int(row.get("eventCfgId")) in implemented_ids
    ]
    data = [
        row for row in (player_pod.get("opEventsDatas") or [])
        if isinstance(row, dict) and op_int(row.get("eventCfgId")) in implemented_ids
    ]
    known_status = {op_int(row.get("eventCfgId")) for row in status if isinstance(row, dict)}
    known_data = {op_int(row.get("eventCfgId")) for row in data if isinstance(row, dict)}
    for event_id, data_cfg_id, fields in events:
        if event_id not in known_status:
            status.append({
                "eventCfgId": event_id,
                "dataCfgId": data_cfg_id,
                "eventUid": f"local-{event_id}",
                "status": 1,
                "startTime": now - 86400,
                "endTime": now + 30 * 86400,
                "closeTime": now + 31 * 86400,
                "extJsonData": "",
            })
        else:
            for row in status:
                if isinstance(row, dict) and op_int(row.get("eventCfgId")) == event_id:
                    row.update({
                        "eventCfgId": event_id,
                        "dataCfgId": data_cfg_id,
                        "eventUid": f"local-{event_id}",
                        "status": 1,
                        "startTime": now - 86400,
                        "endTime": now + 30 * 86400,
                        "closeTime": now + 31 * 86400,
                        "extJsonData": "",
                    })
                    break
        operation_data = {
            "eventCfgId": event_id,
            "dataCfgId": data_cfg_id,
            **fields,
        }
        if event_id not in known_data:
            data.append(operation_data)
        else:
            for row in data:
                if isinstance(row, dict) and op_int(row.get("eventCfgId")) == event_id:
                    row.update(operation_data)
    player_pod["opEventsStatus"] = status
    player_pod["opEventsDatas"] = data
    return True


def overlay_companion_snapshot(body, uid):
    """Overlay complete SoulPODs while preserving all captured non-local fields."""
    if not uid:
        return body, (), None
    code, player_pod = protocol_codec.decode_method(LOAD_PLAYER_RESULT, body)
    soul_pods = player_pod.get("souls", [])
    seeded = storage.seed_companions_from_snapshot(uid, soul_pods)
    warehouse_snapshot = player_pod.get("warehouse", [])
    storage.seed_items_from_snapshot(uid, warehouse_snapshot)
    storage.seed_equipment_from_snapshot(uid, warehouse_snapshot)
    ssr_result = storage.ensure_ssr_spirits_five_star(uid)
    if ssr_result["inserted"] or ssr_result["upgraded"]:
        log.info("  SSR spirits uid=%s: inserted=%d upgraded=%d total=%d", uid, ssr_result["inserted"], ssr_result["upgraded"], ssr_result["total"])
    storage.seed_player_num_attrs(uid, player_pod.get("numAttrs", {}))
    storage.seed_lottery_state(
        uid, player_pod.get("lotteryShows", []), player_pod.get("lotteryRecords", {})
    )
    storage.seed_quest_state(
        uid,
        player_pod.get("quests", []),
        player_pod.get("finishQuestList", []),
        player_pod.get("failQuestList", []),
        player_pod.get("unlockChapterTasks", []),
    )
    storage.reconcile_sign_in_quest(uid)
    storage.seed_player_companion_state(uid, player_pod)
    storage.seed_player_state_json(uid, player_pod)
    added_dresses = storage.ensure_owned_dresses(uid, LIBRARY_UNLOCK_CONFIG.get("dressCids", []))
    if added_dresses:
        log.info("  archive dresses uid=%s: added=%d", uid, added_dresses)
    finish_mazes = storage.get_player_state_json(uid, "finishMazes") or []
    complete_mazes = sorted(
        set(int(value) for value in finish_mazes)
        | set(int(value) for value in LIBRARY_UNLOCK_CONFIG.get("finishMazes", []))
    )
    if complete_mazes != finish_mazes:
        storage.update_player_state_json(uid, "finishMazes", complete_mazes)
    seed_local_mails(uid)
    player_state = storage.get_player_companion_state(uid)
    if player_state:
        player_pod["remainderGiveGiftNum"] = int(player_state["remainder_give_gift_num"] or 0)
        player_pod["fondleNum"] = int(player_state["fondle_num"] or 0)
        player_pod["nextRecoveryFondleTime"] = int(player_state["next_recovery_fondle_time"] or 0)
    player_pod["newMailCount"] = storage.unread_mail_count(uid)
    local_num_attrs = storage.get_player_num_attrs(uid)
    if local_num_attrs:
        player_pod["numAttrs"] = local_num_attrs
    local_player = storage.get_player(uid)
    local_show_soul = local_player.get("current_show_soul_cid") if local_player else None
    if local_player and player_pod.get("baseInfo"):
        local_attrs = storage.get_player_num_attrs(uid)
        player_pod["baseInfo"].update(
            {
                "pid": str(local_player["role_id"]),
                "pName": str(local_player["role_name"]),
                "pLv": int(local_player["level"]),
                "payPoint": int(local_attrs.get(5, 0)),
            }
        )
        if local_show_soul:
            player_pod["baseInfo"]["showSoulCid"] = int(local_show_soul)
    lottery_state = storage.get_lottery_state(uid)
    if lottery_state["lotteryShows"]:
        player_pod.update(lottery_state)
    quest_state = storage.get_quest_state(uid)
    if quest_state["quests"] or any(
        quest_state[key] for key in ("finishQuestList", "failQuestList", "unlockChapterTasks")
    ):
        player_pod.update(quest_state)
    overlay_fishing_activity_snapshot(player_pod, uid)
    local_souls = {row["soul_id"]: row for row in storage.get_souls(uid)}
    changed = []
    for pod in soul_pods:
        local = local_souls.get(pod.get("cid"))
        if local is None:
            continue
        updates = local_soul_pod_for(uid, int(pod["cid"]), pod, local)
        if any(pod.get(name) != value for name, value in updates.items()):
            pod.update(updates)
            changed.append(str(pod["cid"]))
    persisted_pods = storage.get_item_pods(uid) + storage.get_equipment_item_pods(uid)
    persisted_ids = {int(pod["id"]) for pod in persisted_pods}
    captured_special = [
        pod for pod in warehouse_snapshot
        if isinstance(pod, dict)
        and int(pod.get("id", 0) or 0) not in persisted_ids
        and (
            pod.get("newJewelryEquipmentVoPOD") is not None
            or pod.get("placeGameEquipPOD") is not None
        )
    ]
    player_pod["warehouse"] = persisted_pods + captured_special
    # Rebuild configuration-driven memories after the captured souls have been
    # seeded, so the login snapshot always exposes every currently visible
    # chapter instead of relying on a previous reward handler to create one.
    module_handlers.rebuild_memory_state(uid)
    # Overlay JSON state fields
    local_json = storage.get_all_player_state_json(uid)
    if local_json:
        for name, value in local_json.items():
            if name not in storage.FIELD_BLACKLIST and value is not None:
                player_pod[name] = value
        memory_state = local_json.get("soul_memory")
        if isinstance(memory_state, dict):
            chapters = memory_state.get("chapters", {})
            if isinstance(chapters, dict):
                player_pod["soulMemoryChapters"] = [
                    chapter for _, chapter in sorted(
                        (
                            (int(key), value)
                            for key, value in chapters.items()
                            if isinstance(value, dict) and str(key).isdigit()
                        ),
                        key=lambda item: item[0],
                    )
                ]
    overlay_mall_snapshot(player_pod, uid)
    whisper_ids = module_handlers._unlock_soul_whispers(uid)
    if whisper_ids:
        captured = player_pod.get("unlockSoulWhispers")
        merged = list(whisper_ids)
        if isinstance(captured, list):
            merged = sorted(set(merged) | {int(value) for value in captured if str(value).isdigit()})
        player_pod["unlockSoulWhispers"] = merged
    overlay_home_work_snapshot(player_pod, uid)
    overlay_local_progress_snapshot(player_pod, uid)
    overlay_town_snapshot(player_pod, uid)
    overlay_low_frequency_snapshot(player_pod, uid)
    # Operation data is generated from the current local activity state and
    # must be applied after the generic JSON overlay, which may contain the
    # original captured opEventsDatas list.
    overlay_operation_activity_snapshot(player_pod, uid)
    # Unlock every row known to the client and suppress captured public-chat
    # text, including messages that may have been persisted in old state.
    if ALL_HEAD_ICON_IDS:
        player_pod["unlockHeadIcons"] = list(ALL_HEAD_ICON_IDS)
    if ALL_AVATAR_FRAME_IDS:
        player_pod["unlockAvatarFrames"] = list(ALL_AVATAR_FRAME_IDS)
    player_pod["equipSkins"] = _normalized_equip_skin_map(player_pod.get("equipSkins"))
    player_pod["chatRoom"] = {"roomNumber": 1, "onlineCount": 1, "msg": []}
    player_pod["guildChatCaches"] = []
    rebuilt = protocol_codec.encode_method(LOAD_PLAYER_RESULT, code, player_pod)
    return rebuilt, tuple(changed), player_pod


class CaptureReplay:
    """Extract server response bodies from the saved official pcap."""

    def __init__(self, path, server_port):
        self.path = path
        self.server_port = server_port
        self.messages = defaultdict(list)
        self._load()

    @staticmethod
    def _iter_pcap_packets(data):
        if data[:4] == b"\xd4\xc3\xb2\xa1":
            endian = "<"
        elif data[:4] == b"\xa1\xb2\xc3\xd4":
            endian = ">"
        else:
            raise ValueError("unsupported pcap magic")

        offset = 24
        while offset + 16 <= len(data):
            _, _, captured_len, _ = struct.unpack_from(
                f"{endian}IIII", data, offset
            )
            offset += 16
            packet = data[offset : offset + captured_len]
            offset += captured_len
            yield packet

    @staticmethod
    def _parse_linux_sll_tcp(packet):
        if len(packet) < 16 or packet[14:16] != b"\x08\x00":
            return None
        ip = packet[16:]
        if len(ip) < 20 or ip[9] != socket.IPPROTO_TCP:
            return None
        ip_header_len = (ip[0] & 0x0F) * 4
        total_len = struct.unpack_from(">H", ip, 2)[0]
        tcp = ip[ip_header_len:total_len]
        if len(tcp) < 20:
            return None
        src_port, dst_port, seq = struct.unpack_from(">HHI", tcp, 0)
        tcp_header_len = ((tcp[12] >> 4) & 0x0F) * 4
        payload = tcp[tcp_header_len:]
        if not payload:
            return None
        return src_port, dst_port, seq, payload

    @staticmethod
    def _reassemble(segments):
        segments.sort(key=lambda item: item[0])
        data = bytearray()
        next_seq = None
        for seq, payload in segments:
            if next_seq is None:
                next_seq = seq
            if seq > next_seq:
                raise ValueError(f"gap in captured TCP stream: {seq - next_seq} bytes")
            overlap = max(next_seq - seq, 0)
            if overlap < len(payload):
                data.extend(payload[overlap:])
                next_seq = seq + len(payload)
        return bytes(data)

    def _load(self):
        with open(self.path, "rb") as file:
            capture = file.read()
        segments = []
        for packet in self._iter_pcap_packets(capture):
            parsed = self._parse_linux_sll_tcp(packet)
            if parsed is None:
                continue
            src_port, _, seq, payload = parsed
            if src_port == self.server_port:
                segments.append((seq, payload))

        stream = self._reassemble(segments)
        offset = 0
        while offset + 12 <= len(stream):
            total_length, msg_id, order = struct.unpack_from("<III", stream, offset)
            if total_length < 12 or offset + total_length > len(stream):
                raise ValueError(
                    f"bad captured frame at offset={offset}, length={total_length}"
                )
            body = stream[offset + 12 : offset + total_length]
            self.messages[msg_id].append((order, body))
            offset += total_length
        if offset != len(stream):
            raise ValueError(f"trailing captured bytes: {len(stream) - offset}")

    def body(self, msg_id, occurrence=0):
        matches = self.messages.get(msg_id)
        if not matches:
            return None
        return matches[min(occurrence, len(matches) - 1)][1]


class LocalResponseFixture:
    """Load static local protocol bodies generated from offline evidence."""

    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid local response fixture version")
        responses = payload.get("responses")
        if not isinstance(responses, dict):
            raise ValueError("local response fixture has no responses")
        self.responses = {}
        for message_id, body_hex in responses.items():
            message_id = int(message_id)
            if message_id <= 0 or not isinstance(body_hex, str):
                raise ValueError("invalid local response fixture entry")
            self.responses[message_id] = bytes.fromhex(body_hex)

    def body(self, msg_id, occurrence=0):
        return self.responses.get(int(msg_id))


try:
    REPLAY = LocalResponseFixture(OFFLINE_RESPONSE_FIXTURE_PATH)
    log.info(
        "Loaded %d local response fixtures from %s",
        len(REPLAY.responses),
        OFFLINE_RESPONSE_FIXTURE_PATH,
    )
except Exception as exc:
    log.exception("Local response fixture is unavailable: %s", exc)
    raise RuntimeError("TCP server cannot start without its local response fixture") from exc


# Requests made after loadPlayer and their captured responses.
# Comments indicate likely purpose based on module context.
CAPTURE_RESPONSE_MAP = {
    # All remaining entries are for functionality not yet locally implemented.
    # Each should be migrated to a dynamic handler before the final pcap removal.
}

class Session:
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self.uid = hashlib.md5(
            (str(time.time()) + str(os.urandom(8))).encode()
        ).hexdigest()
        self.uuid = ""
        self.account = None
        self.session_id = None
        self.active_story = None
        self.player_snapshot = None
        self.running = True
        log.info("New connection from %s", addr)

    def send(self, msg_id, body=b"", order=0):
        self.conn.sendall(encode_msg(msg_id, body, order))
        preview = body.hex() if body and len(body) <= 80 else ""
        log.info(
            "  [S->C] MsgID=%d order=%d body=%db%s",
            msg_id,
            order,
            len(body),
            f" {preview}" if preview else "",
        )

    def _flush_pending_tcp_notifications(self):
        if not self.account:
            return 0
        notifications = storage.pop_pending_tcp_notifications(self.uid)
        for notification in notifications:
            for cid, quantity in notification.get("changedAttrs", {}).items():
                self.send(3924, protocol_codec.encode_method(3924, {int(cid): int(quantity)}))
            changed_items = notification.get("changedItems", [])
            if changed_items:
                self.send(4102, protocol_codec.encode_method(4102, changed_items))
            if notification.get("kind") == "offline_recharge":
                rewards = [
                    {"cid": int(cid), "num": int(quantity), "tag": 0}
                    for cid, quantity in notification.get("rewards", [])
                    if int(cid) != 5
                ]
                pay_point = sum(
                    int(quantity) for cid, quantity in notification.get("rewards", [])
                    if int(cid) == 5
                )
                player = storage.get_player(self.uid) or {}
                order_id = "offline-" + hashlib.sha256(
                    str(notification.get("orderKey", "")).encode("utf-8")
                ).hexdigest()[:24]
                pay_id = int(notification.get("payMoney", 0))
                self.send(3936, protocol_codec.encode_method(
                    3936,
                    pay_point,
                    rewards,
                    str(self.uid),
                    order_id,
                    order_id,
                    pay_id,
                    3,
                    float(notification.get("amount", 0)),
                    "CNY",
                    int(notification.get("createdAt", int(time.time()))),
                    str(player.get("role_name", "local")),
                ))
                if pay_id > 0:
                    self.send(3937, protocol_codec.encode_method(
                        3937, {pay_id: 1}, {pay_id: False}
                    ))
        if notifications:
            log.info("  flushed %d pending TCP notifications uid=%s", len(notifications), self.uid)
        return len(notifications)

    def send_local_fixture(self, msg_id, required=False):
        body = REPLAY.body(msg_id) if REPLAY else None
        if body is None:
            message = f"local response fixture {msg_id} is unavailable"
            if required:
                raise RuntimeError(message)
            log.warning("  %s", message)
            return False
        self.send(msg_id, body)
        return True

    def _role_info(self):
        if not self.account:
            return None
        player = storage.get_player(self.uid)
        if player is None:
            return None
        created_at = max(0, min(0xFFFFFFFF, int(self.account.get("created_at", 0) or 0)))
        guid = int(hashlib.md5(self.uid.encode("utf-8")).hexdigest()[:8], 16)
        profile = storage.get_player_state_json(self.uid, "player_role") or {}
        return {
            "pid": str(player["role_id"]),
            "pname": str(player["role_name"]),
            "leaderCid": int(profile.get("leaderCid", 1000) or 1000),
            "lv": int(player["level"]),
            "guid": guid,
            "createTime": created_at,
        }

    def handle_validate_uuid(self, body):
        try:
            identity, _server_id, _account_server_id = protocol_codec.decode_method(
                VALIDATE_UUID, body
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("  invalid validateUUID body: %s", exc)
            return False
        account = storage.get_account_by_identity(identity)
        if self.session_id is None:
            account, self.session_id = storage.record_tcp_login(
                account["uuid"] if account else identity,
                f"{self.addr[0]}:{self.addr[1]}",
            )
        if account:
            self.account = account
            self.uid = account["uid"]
            self.uuid = account["uuid"]
            self._restore_active_battle_context()
        else:
            self.account = None
            self.uuid = identity
        role = self._role_info()
        response = protocol_codec.encode_method(
            VALIDATE_UUID_RESULT,
            0,
            [role] if role else [],
            0,
            self.uid if self.account else "",
            self.uuid,
            False,
        )
        self.send(VALIDATE_UUID_RESULT, response)
        log.info(
            "  validate UUID resolved uid=%s roles=%d -> %d",
            self.uid if self.account else "none",
            1 if role else 0,
            VALIDATE_UUID_RESULT,
        )
        return True

    def handle_choose_role(self, body):
        try:
            (requested_pid,) = protocol_codec.decode_method(CHOOSE_ROLE, body)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("  invalid chooseRole body: %s", exc)
            return False
        role = self._role_info()
        if role is None:
            self.send(
                CHOOSE_ROLE_RESULT,
                protocol_codec.encode_method(CHOOSE_ROLE_RESULT, 11001, 0, ""),
            )
            return True
        self.send(
            CHOOSE_ROLE_RESULT,
            protocol_codec.encode_method(CHOOSE_ROLE_RESULT, 0, 1, ""),
        )
        log.info(
            "  chose local role uid=%s requestedPid=%s localPid=%s -> %d",
            self.uid,
            requested_pid,
            role["pid"],
            CHOOSE_ROLE_RESULT,
        )
        return True

    def handle_create_role(self, body):
        try:
            leader_cid, role_name = protocol_codec.decode_method(CREATE_ROLE, body)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("  invalid createRole body: %s", exc)
            return False
        if not self.account or not role_name.strip():
            self.send(
                CREATE_ROLE_RESULT,
                protocol_codec.encode_method(CREATE_ROLE_RESULT, 11002, {}),
            )
            return True
        player = storage.get_player(self.uid)
        role_id = player["role_id"] if player else str(
            int(hashlib.sha256(self.uid.encode("utf-8")).hexdigest()[:15], 16)
        )
        if not storage.set_player_role(self.uid, role_id, role_name):
            self.send(
                CREATE_ROLE_RESULT,
                protocol_codec.encode_method(CREATE_ROLE_RESULT, 11002, {}),
            )
            return True
        storage.update_player_state_json(
            self.uid, "player_role", {"leaderCid": int(leader_cid)}
        )
        role = self._role_info()
        self.send(
            CREATE_ROLE_RESULT,
            protocol_codec.encode_method(CREATE_ROLE_RESULT, 0, role),
        )
        log.info(
            "  created local role uid=%s pid=%s name=%s -> %d",
            self.uid,
            role["pid"],
            role["pname"],
            CREATE_ROLE_RESULT,
        )
        return True

    def send_player_snapshot(self):
        body = REPLAY.body(LOAD_PLAYER_RESULT) if REPLAY else None
        if body is None:
            raise RuntimeError(
                f"captured response {LOAD_PLAYER_RESULT} is unavailable"
            )
        player = storage.get_player(self.uid) if self.account else None
        body, player_changed = overlay_player_snapshot(body, player)
        # Currency values are part of the structured PlayerPOD.  The old
        # fixed-byte patch could match an unrelated uint16 field such as
        # baseInfo.leaderCid and turn a valid role config ID into 65535.
        currency_changed = ()
        companion_changed = ()
        if self.account:
            try:
                body, companion_changed, self.player_snapshot = overlay_companion_snapshot(
                    body, self.uid
                )
            except Exception:
                log.exception("  structured companion overlay failed; sending scalar overlays")
        self.send(LOAD_PLAYER_RESULT, body)
        all_changed = list(player_changed) + list(currency_changed)
        if companion_changed:
            all_changed.append(f"companions({len(companion_changed)})")
        if player:
            storage.set_snapshot_mode(self.uid, "local_fixture_overlay")
            log.info(
                "  sent player snapshot uid=%s overlays=%s",
                self.uid,
                ",".join(all_changed) if all_changed else "none",
            )
        else:
            log.info("  sent captured player snapshot in compatibility mode")

    def receive_handshake_request(self):
        while True:
            msg_id, order, body = read_msg(self.conn, timeout=30)
            if msg_id == PING:
                self.send(PANG, pang_body())
                continue
            if msg_id == HEARTBEAT:
                continue
            return msg_id, order, body

    def _restore_active_battle_context(self):
        if not self.account:
            return None
        battle = storage.get_active_battle(self.uid)
        if battle is None:
            return None
        if battle["map_id"]:
            self.active_story = {
                "kind": "maze",
                "maze_cid": battle["map_id"],
                "battle_id": battle["id"],
            }
        else:
            self.active_story = {"kind": "battle", "battle_id": battle["id"]}
        return battle

    def handle_reconnect(self, body, request_order, resolve_identity=False):
        try:
            reconnect_token, read_msg_length, requested_uid = (
                protocol_codec.decode_method(RECONNECT, body)
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("  invalid reconnect body: %s", exc)
            return False

        identity = None
        if resolve_identity or not self.account:
            for candidate in (reconnect_token, requested_uid):
                account = storage.get_account_by_identity(candidate)
                if account is None:
                    continue
                self.account = account
                self.uid = account["uid"]
                self.uuid = account["uuid"]
                if self.session_id is None:
                    self.session_id = storage.record_tcp_login(
                        self.uuid, f"{self.addr[0]}:{self.addr[1]}"
                    )[1]
                self._restore_active_battle_context()
                identity = candidate
                break
            if identity is None:
                log.warning(
                    "  reconnect identity was not found token=%s uid=%s",
                    reconnect_token,
                    requested_uid,
                )
                return False
        elif requested_uid and requested_uid != self.uid:
            log.warning(
                "  rejected reconnect UID mismatch current=%s requested=%s",
                self.uid,
                requested_uid,
            )
            return False

        # Native Net.SendAllFailMsgs resends only cached requests whose
        # PackageHeader.order is greater than lastMsgOrder. Returning zero made
        # the client replay its entire cache (including ping and reconnect)
        # forever. This request has already been accepted, so its order is the
        # highest client order this compatibility server can acknowledge.
        last_msg_order = max(0, int(request_order or 0))
        response = protocol_codec.encode_method(
            RECONNECT_RESULT,
            0,
            last_msg_order,
            reconnect_token,
        )
        self.send(RECONNECT_RESULT, response)
        log.info(
            "  accepted reconnect uid=%s identity=%s readBytes=%d "
            "lastMsgOrder=%d token=%s",
            self.uid,
            identity or "session",
            read_msg_length,
            last_msg_order,
            reconnect_token,
        )
        return True

    def handshake(self):
        self.send(NOTIFY_SERVER_STATUS, server_status_body())
        msg_id, order, body = self.receive_handshake_request()
        if msg_id is None:
            return False
        log.info(
            "  [C->S] MsgID=%d order=%d body=%db %s",
            msg_id,
            order,
            len(body),
            body[:80].hex(),
        )

        if msg_id == RECONNECT:
            return self.handle_reconnect(body, order, resolve_identity=True)
        if msg_id != VALIDATE_UUID:
            log.warning("  expected validateUUID(%d), got %d", VALIDATE_UUID, msg_id)
            return False

        if not self.handle_validate_uuid(body):
            return False

        msg_id, order, body = self.receive_handshake_request()
        if msg_id is None:
            return False
        log.info(
            "  [C->S] MsgID=%d order=%d body=%db %s",
            msg_id,
            order,
            len(body),
            body[:80].hex(),
        )
        if msg_id == CHOOSE_ROLE:
            self.handle_choose_role(body)
            return True
        if msg_id == CREATE_ROLE:
            self.handle_create_role(body)
            return True
        log.warning("  expected chooseRole(%d), got %d", CHOOSE_ROLE, msg_id)
        return False

    def handle_captured_response(self, request_id):
        if request_id == 2602 and self.account:
            item_count = len(storage.get_items(self.uid))
            if item_count:
                log.warning(
                    "  SQLite has %d item rows, but dynamic 2603 encoding is "
                    "disabled until its wire format is verified; replaying capture",
                    item_count,
                )

        response_ids = CAPTURE_RESPONSE_MAP.get(request_id)
        if response_ids is None:
            return False
        for response_id in response_ids:
            self.send_local_fixture(response_id)
        return True

    def handle_wear_dress(self, body):
        if len(body) != 5 or body[0] != 0x5F:
            log.warning("  invalid wear dress body: %s", body.hex())
            return False
        if not self.account:
            log.warning("  wear dress rejected for compatibility session")
            return False

        dress_cid = struct.unpack_from("<I", body, 1)[0]
        if not storage.set_current_dress(self.uid, dress_cid):
            log.warning("  wear dress rejected: uid=%s dressCid=%d", self.uid, dress_cid)
            return False

        self.send(WEAR_DRESS_RESULT, b"\x50")
        log.info(
            "  wear dress persisted uid=%s dressCid=%d -> %d",
            self.uid,
            dress_cid,
            WEAR_DRESS_RESULT,
        )
        return True

    def handle_view_dress(self, body):
        try:
            (dress_cid,) = protocol_codec.decode_method(VIEW_DRESS, body)
        except ValueError as exc:
            log.warning("  invalid view dress body %s: %s", body.hex(), exc)
            return False
        if not self.account or dress_cid <= 0:
            return False
        self.send(
            VIEW_DRESS_RESULT,
            protocol_codec.encode_method(VIEW_DRESS_RESULT, 0),
        )
        log.info("  view dress uid=%s dressCid=%d -> %d", self.uid, dress_cid, VIEW_DRESS_RESULT)
        return True

    def handle_get_mails(self, body):
        try:
            (mail_type,) = protocol_codec.decode_method(GET_MAILS, body)
        except ValueError as exc:
            log.warning("  invalid get mails body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        seed_local_mails(self.uid)
        mails = storage.get_mails(self.uid, mail_type, local_mail_types())
        self.send(
            GET_MAILS_RESULT,
            protocol_codec.encode_method(GET_MAILS_RESULT, 0, mail_type, mails),
        )
        log.info(
            "  local mails uid=%s type=%d count=%d -> %d",
            self.uid,
            mail_type,
            len(mails),
            GET_MAILS_RESULT,
        )
        return True

    def handle_read_mail(self, body):
        try:
            (mail_id,) = protocol_codec.decode_method(READ_MAIL, body)
        except ValueError as exc:
            log.warning("  invalid read mail body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.mark_mail_read(self.uid, mail_id):
            return False
        self.send(
            READ_MAIL_RESULT,
            protocol_codec.encode_method(READ_MAIL_RESULT, 0, mail_id),
        )
        log.info("  mail read uid=%s mailId=%d -> %d", self.uid, mail_id, READ_MAIL_RESULT)
        return True

    def handle_delete_mail(self, body):
        try:
            (mail_ids,) = protocol_codec.decode_method(DELETE_MAIL, body)
        except ValueError as exc:
            log.warning("  invalid delete mail body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        deleted = storage.delete_mails(self.uid, mail_ids)
        self.send(
            DELETE_MAIL_RESULT,
            protocol_codec.encode_method(DELETE_MAIL_RESULT, 0, deleted),
        )
        log.info(
            "  mails deleted uid=%s requested=%d deleted=%d -> %d",
            self.uid,
            len(mail_ids),
            len(deleted),
            DELETE_MAIL_RESULT,
        )
        return True

    def handle_pick_up_mail(self, body):
        try:
            (mail_ids,) = protocol_codec.decode_method(PICK_UP_MAIL, body)
        except ValueError as exc:
            log.warning("  invalid mail pickup body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        seed_local_mails(self.uid)
        seed_local_player_attrs(self.uid)
        result = storage.pick_up_mail_attachments(self.uid, mail_ids)
        if result is None:
            log.warning("  mail pickup rejected uid=%s ids=%s", self.uid, mail_ids)
            return False

        for cid, quantity in result["changed_attrs"].items():
            self.send(
                NOTIFY_NUM_ATTR,
                protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}),
            )
        if result["changed_items"]:
            self.send(
                NOTIFY_ITEM_CHANGE,
                protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]),
            )
        self.send(
            NOTIFY_NEW_MAIL,
            protocol_codec.encode_method(NOTIFY_NEW_MAIL, result["unread_count"]),
        )
        self.send(
            PICK_UP_MAIL_RESULT,
            protocol_codec.encode_method(
                PICK_UP_MAIL_RESULT, 0, result["mails"], result["rewards"]
            ),
        )
        log.info(
            "  mail pickup uid=%s requested=%d attrs=%d items=%d -> %d/%d/%d/%d",
            self.uid,
            len(mail_ids),
            len(result["changed_attrs"]),
            len(result["changed_items"]),
            NOTIFY_NUM_ATTR,
            NOTIFY_ITEM_CHANGE,
            NOTIFY_NEW_MAIL,
            PICK_UP_MAIL_RESULT,
        )
        return True

    def handle_open_library(self, body):
        try:
            values = protocol_codec.decode_method(OPEN_LIBRARY, body)
        except ValueError as exc:
            log.warning("  invalid open library body %s: %s", body.hex(), exc)
            return False
        if values or not self.account:
            return False
        seed_local_library(self.uid)
        library = storage.get_library_state(self.uid)
        if library is None:
            return False
        complete_library_state(library)
        self.send(
            OPEN_LIBRARY_RESULT,
            protocol_codec.encode_method(OPEN_LIBRARY_RESULT, 0, library),
        )
        log.info("  local library uid=%s -> %d", self.uid, OPEN_LIBRARY_RESULT)
        return True

    def handle_view_news_book(self, body):
        try:
            (news_id,) = protocol_codec.decode_method(VIEW_NEWS_BOOK, body)
        except ValueError as exc:
            log.warning("  invalid view news body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        seed_local_library(self.uid)
        if not storage.mark_news_book_viewed(self.uid, news_id):
            return False
        self.send(
            VIEW_NEWS_BOOK_RESULT,
            protocol_codec.encode_method(VIEW_NEWS_BOOK_RESULT, 0),
        )
        log.info("  news viewed uid=%s newsId=%d -> %d", self.uid, news_id, VIEW_NEWS_BOOK_RESULT)
        return True

    def handle_get_news_book_rewards(self, body):
        try:
            (news_id,) = protocol_codec.decode_method(GET_NEWS_BOOK_REWARDS, body)
        except ValueError as exc:
            log.warning("  invalid news reward body %s: %s", body.hex(), exc)
            return False
        rewards = LIBRARY_NEWS_REWARDS.get(str(news_id))
        if not self.account or rewards is None:
            return False
        seed_local_library(self.uid)
        seed_local_player_attrs(self.uid)
        result = storage.claim_news_book_reward(self.uid, news_id, rewards)
        if result is None:
            log.warning("  news reward rejected uid=%s newsId=%d", self.uid, news_id)
            return False
        for cid, quantity in result["changed_attrs"].items():
            self.send(
                NOTIFY_NUM_ATTR,
                protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}),
            )
        if result["changed_items"]:
            self.send(
                NOTIFY_ITEM_CHANGE,
                protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]),
            )
        self.send(
            GET_NEWS_BOOK_REWARDS_RESULT,
            protocol_codec.encode_method(
                GET_NEWS_BOOK_REWARDS_RESULT, 0, news_id, result["item_shows"]
            ),
        )
        log.info(
            "  news reward uid=%s newsId=%d duplicate=%s attrs=%d items=%d -> %d",
            self.uid,
            news_id,
            result["duplicate"],
            len(result["changed_attrs"]),
            len(result["changed_items"]),
            GET_NEWS_BOOK_REWARDS_RESULT,
        )
        return True

    def handle_lottery_history(self, body):
        try:
            values = protocol_codec.decode_method(GET_LOTTERY_HISTORY, body)
        except ValueError as exc:
            log.warning("  invalid lottery history body %s: %s", body.hex(), exc)
            return False
        if values or not self.account:
            return False
        history = storage.get_lottery_history(self.uid)
        self.send(
            GET_LOTTERY_HISTORY_RESULT,
            protocol_codec.encode_method(GET_LOTTERY_HISTORY_RESULT, 0, history),
        )
        log.info(
            "  local lottery history uid=%s count=%d -> %d",
            self.uid,
            len(history),
            GET_LOTTERY_HISTORY_RESULT,
        )
        return True

    def handle_lottery_draw(self, body):
        try:
            show_id, lottery_cid, up_cid_list = protocol_codec.decode_method(LOTTERY_DRAW, body)
        except ValueError as exc:
            log.warning("  invalid lottery draw body %s: %s", body.hex(), exc)
            self.send(LOTTERY_DRAW_RESULT, protocol_codec.encode_method(LOTTERY_DRAW_RESULT, 1, {}, 0, {}, [], []))
            return False
        if not self.account:
            self.send(LOTTERY_DRAW_RESULT, protocol_codec.encode_method(LOTTERY_DRAW_RESULT, 1, {}, int(lottery_cid), {}, [], []))
            return False
        try:
            result = storage.perform_lottery_draw(
                self.uid, show_id, lottery_cid, up_cid_list,
                LOTTERY_ACTIONS, LOTTERY_TIER_CONFIG, LOTTERY_DROP_CONFIG,
            )
        except storage.LotteryPoolError as exc:
            log.error(
                "  lottery pool unresolvable uid=%s showId=%d lotteryCid=%d upCids=%s: %s",
                self.uid, show_id, lottery_cid, list(up_cid_list or []), exc,
            )
            self.send(LOTTERY_DRAW_RESULT, protocol_codec.encode_method(LOTTERY_DRAW_RESULT, 1, {}, int(lottery_cid), {}, [], []))
            return False
        if result is None:
            log.warning("  lottery draw rejected uid=%s showId=%d lotteryCid=%d", self.uid, show_id, lottery_cid)
            self.send(LOTTERY_DRAW_RESULT, protocol_codec.encode_method(LOTTERY_DRAW_RESULT, 1, {}, int(lottery_cid), {}, [], []))
            return False
        Session._send_reward_changes(self, result)
        for soul_id in result.get("newSoulIds", []):
            soul_pod = self._local_soul_pod(soul_id)
            snapshot_soul = next(
                (pod for pod in self.player_snapshot.get("souls", [])
                 if isinstance(pod, dict) and int(pod.get("cid", 0) or 0) == int(soul_id)),
                None,
            )
            if snapshot_soul is None:
                self.player_snapshot.setdefault("souls", []).append(soul_pod)
            else:
                snapshot_soul.update(soul_pod)
            self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, soul_pod))
        for soul_id in result.get("duplicateSoulIds", []):
            self.send(
                NOTIFY_REPETITION_UNLOCK_SOUL,
                protocol_codec.encode_method(NOTIFY_REPETITION_UNLOCK_SOUL, int(soul_id)),
            )
        self.send(
            LOTTERY_DRAW_RESULT,
            protocol_codec.encode_method(
                LOTTERY_DRAW_RESULT,
                0,
                result["lotteryShowPOD"],
                result["lotteryCid"],
                result["lotteryRecords"],
                result["baseShowItems"],
                result["showItems"],
            ),
        )
        log.info(
            "  lottery draw committed uid=%s showId=%d lotteryCid=%d count=%d fixed=%d results=%d -> %d",
            self.uid,
            show_id,
            lottery_cid,
            10 if LOTTERY_ACTIONS.get(str(lottery_cid), {}).get("lotteryMode") == 2 else 1,
            len(result["baseShowItems"]),
            len(result["showItems"]),
            LOTTERY_DRAW_RESULT,
        )
        return True

    def handle_change_show_soul(self, body):
        try:
            (soul_id,) = protocol_codec.decode_method(CHANGE_SHOW_SOUL, body)
        except ValueError as exc:
            log.warning("  invalid show soul body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.set_show_soul(self.uid, soul_id):
            log.warning("  show soul rejected uid=%s soulCid=%d", self.uid, soul_id)
            return False
        self.send(
            CHANGE_SHOW_SOUL_RESULT,
            protocol_codec.encode_method(CHANGE_SHOW_SOUL_RESULT, 0),
        )
        log.info("  show soul changed uid=%s soulCid=%d -> %d", self.uid, soul_id, CHANGE_SHOW_SOUL_RESULT)
        return True

    def handle_commit_quest(self, body):
        try:
            (quest_ids,) = protocol_codec.decode_method(COMMIT_QUEST, body)
        except ValueError as exc:
            log.warning("  invalid quest commit body %s: %s", body.hex(), exc)
            self.send(COMMIT_QUEST_RESULT, protocol_codec.encode_method(COMMIT_QUEST_RESULT, 1, [], []))
            return True
        if not self.account:
            self.send(COMMIT_QUEST_RESULT, protocol_codec.encode_method(COMMIT_QUEST_RESULT, 1, [], []))
            return True
        seed_local_player_attrs(self.uid)
        result = storage.commit_quests(self.uid, quest_ids, TASK_REWARDS)
        if result is None:
            log.warning("  quest commit rejected uid=%s ids=%s", self.uid, quest_ids)
            self.send(COMMIT_QUEST_RESULT, protocol_codec.encode_method(COMMIT_QUEST_RESULT, 1, [], []))
            return True
        for cid, quantity in result["changed_attrs"].items():
            self.send(
                NOTIFY_NUM_ATTR,
                protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}),
            )
        if result["changed_items"]:
            self.send(
                NOTIFY_ITEM_CHANGE,
                protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]),
            )
        for quest in result["completed_quests"]:
            self.send(
                NOTIFY_UPDATE_QUEST,
                protocol_codec.encode_method(NOTIFY_UPDATE_QUEST, quest, True),
            )
            self.send(
                NOTIFY_FINISH_QUEST_LIST,
                protocol_codec.encode_method(NOTIFY_FINISH_QUEST_LIST, quest["cid"], False),
            )
        self.send(
            COMMIT_QUEST_RESULT,
            protocol_codec.encode_method(COMMIT_QUEST_RESULT, 0, result["cids"], result["awards"]),
        )
        log.info(
            "  quest committed uid=%s requested=%d attrs=%d items=%d -> %d",
            self.uid,
            len(quest_ids),
            len(result["changed_attrs"]),
            len(result["changed_items"]),
            COMMIT_QUEST_RESULT,
        )
        return True

    def handle_unlock_chapter_tasks(self, body):
        try:
            (chapter_ids,) = protocol_codec.decode_method(UNLOCK_CHAPTER_TASKS, body)
        except ValueError as exc:
            log.warning("  invalid chapter unlock body %s: %s", body.hex(), exc)
            return False
        if not self.account or any(str(cid) not in MAZE_CHALLENGE_BONUS for cid in chapter_ids):
            return False
        seed_local_quest_state(self.uid)
        result = storage.unlock_chapter_tasks(self.uid, chapter_ids)
        if result is None:
            return False
        self.send(
            UNLOCK_CHAPTER_TASKS_RESULT,
            protocol_codec.encode_method(UNLOCK_CHAPTER_TASKS_RESULT, 0, result),
        )
        log.info(
            "  chapter tasks unlocked uid=%s requested=%d -> %d",
            self.uid,
            len(chapter_ids),
            UNLOCK_CHAPTER_TASKS_RESULT,
        )
        return True

    def handle_give_gift(self, body, order):
        try:
            soul_id, gift_cid = protocol_codec.decode_method(GIVE_GIFT, body)
        except ValueError as exc:
            log.warning("  invalid give gift body %s: %s", body.hex(), exc)
            return False
        if not self.account or self.player_snapshot is None:
            log.warning("  give gift rejected without a loaded local player snapshot")
            return False
        gift = COMPANION_RULES.get("gifts", {}).get(str(gift_cid))
        if not gift:
            log.warning("  give gift rejected: unknown giftCid=%d", gift_cid)
            return False
        try:
            index = gift["SoulId"].index(soul_id)
            inclination = gift["Inclination"][index]
            multiplier = GIFT_FAVOR_MULTIPLIERS[inclination - 1]
        except (ValueError, IndexError, TypeError):
            log.warning("  give gift rejected: giftCid=%d has no rule for soulCid=%d", gift_cid, soul_id)
            return False
        add_favor = int(gift["Favor"] * multiplier / 100)
        companion = storage.get_companion(self.uid, soul_id)
        if companion is None:
            return False
        new_level = favor_level_for(
            soul_id,
            companion["favor"] + add_favor,
            bool(companion["oath_activation"]),
        )
        result = storage.apply_gift(
            self.uid,
            soul_id,
            gift_cid,
            int(gift["ItemId"]),
            add_favor,
            new_level,
            f"{self.session_id}:{order}",
        )
        if result is None:
            log.warning("  give gift rejected by local transaction uid=%s giftCid=%d", self.uid, gift_cid)
            return False

        warehouse = self.player_snapshot.get("warehouse", [])
        item_pod = next((item for item in warehouse if item.get("cid") == result["item_id"]), None)
        if item_pod is None:
            log.warning("  give gift committed but ItemPOD template is missing for cid=%d", result["item_id"])
            return False
        item_pod["num"] = result["item_quantity"]
        soul_pod = next((pod for pod in self.player_snapshot.get("souls", []) if pod.get("cid") == soul_id), None)
        if soul_pod is None:
            log.warning("  give gift committed but SoulPOD template is missing for cid=%d", soul_id)
            return False
        local = storage.get_companion(self.uid, soul_id)
        soul_pod.update(
            {
                "favor": local["favor"],
                "favorLv": local["favor_level"],
                "dailyDislike": bool(local["daily_dislike"]),
                "oathActivation": bool(local["oath_activation"]),
            }
        )
        self.player_snapshot["remainderGiveGiftNum"] = result["remainder_give_gift_num"]
        self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, [item_pod]))
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, soul_pod))
        self.send(
            GIVE_GIFT_RESULT,
            protocol_codec.encode_method(
                GIVE_GIFT_RESULT,
                0,
                soul_id,
                gift_cid,
                multiplier > 100,
                add_favor,
            ),
        )
        module_handlers.notify_memory_unlocks(self, self.uid)
        log.info(
            "  give gift committed uid=%s soulCid=%d giftCid=%d itemCid=%d addFavor=%d duplicate=%s -> %d/%d/%d",
            self.uid,
            soul_id,
            gift_cid,
            result["item_id"],
            add_favor,
            result["duplicate"],
            NOTIFY_ITEM_CHANGE,
            UPDATE_SOUL,
            GIVE_GIFT_RESULT,
        )
        return True

    def handle_get_soul_oath(self, body):
        try:
            (soul_id,) = protocol_codec.decode_method(GET_SOUL_OATH, body)
        except ValueError as exc:
            log.warning("  invalid get soul oath body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        companion = storage.get_companion(self.uid, soul_id)
        if companion is None:
            log.warning("  get soul oath rejected: unknown soulCid=%d", soul_id)
            return False
        date_data = {state: 0 for state in range(1, 6)}
        if companion.get("oath_activated_at"):
            date_data[5] = int(companion["oath_activated_at"]) * 1000
        pod = {
            "activation": bool(companion["oath_activation"]),
            "countData": {2: 0},
            "dateData": date_data,
        }
        self.send(
            GET_SOUL_OATH_RESULT,
            protocol_codec.encode_method(GET_SOUL_OATH_RESULT, 0, soul_id, pod),
        )
        log.info(
            "  get soul oath uid=%s soulCid=%d activation=%s -> %d",
            self.uid,
            soul_id,
            pod["activation"],
            GET_SOUL_OATH_RESULT,
        )
        return True

    def handle_connective(self, body):
        try:
            (marry_id,) = protocol_codec.decode_method(CONNECTIVE, body)
        except ValueError as exc:
            log.warning("  invalid connective body %s: %s", body.hex(), exc)
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, 0, 0, []))
            return True
        if not self.account or self.player_snapshot is None:
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, marry_id, 0, []))
            return True
        config = COMPANION_RULES.get("soul_marry", {}).get(str(marry_id))
        if not config:
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, marry_id, 0, []))
            return True
        soul_id = int(config["SoulId"])
        companion = storage.get_companion(self.uid, soul_id)
        if companion is None or companion["favor_level"] < int(config["MarryFavorLv"]):
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, marry_id, soul_id, []))
            return True
        cost = config.get("CostItem") or []
        reward = config.get("Reward") or []
        if len(cost) < 2 or len(reward) < 2:
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, marry_id, soul_id, []))
            return True
        result = storage.apply_oath(
            self.uid, soul_id, marry_id, int(cost[0]), int(cost[1]), int(reward[0])
        )
        if result is None:
            log.warning("  connective rejected by local transaction uid=%s marryId=%d", self.uid, marry_id)
            self.send(CONNECTIVE_RESULT, protocol_codec.encode_method(CONNECTIVE_RESULT, 1, marry_id, soul_id, []))
            return True
        cost_pod = next(
            (item for item in self.player_snapshot.get("warehouse", []) if item.get("cid") == result["cost_item_id"]),
            None,
        )
        changed_items = {
            int(item.get("cid", 0)): dict(item)
            for item in result.get("reward_changed_items", [])
            if isinstance(item, dict) and int(item.get("cid", 0) or 0) > 0
        }
        if cost_pod:
            cost_pod["num"] = result["cost_item_quantity"]
            changed_items[int(cost_pod["cid"])] = dict(cost_pod)
        Session._send_reward_changes(self, {
            "changed_attrs": result.get("reward_changed_attrs", {}),
            "changed_items": list(changed_items.values()),
        })
        soul_pod = next(
            pod for pod in self.player_snapshot["souls"] if pod.get("cid") == soul_id
        )
        soul_pod["oathActivation"] = True
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, soul_pod))
        all_collect = self.player_snapshot.setdefault("allCollectItems", [])
        if result["reward_item_id"] not in all_collect:
            all_collect.append(result["reward_item_id"])
        shows = [{"cid": result["reward_item_id"], "num": 1, "tag": 0}]
        self.send(
            CONNECTIVE_RESULT,
            protocol_codec.encode_method(
                CONNECTIVE_RESULT, 0, marry_id, soul_id, shows
            ),
        )
        log.info(
            "  connective committed uid=%s soulCid=%d marryId=%d rewardCid=%d duplicate=%s -> %d/%d/%d",
            self.uid,
            soul_id,
            marry_id,
            result["reward_item_id"],
            result["duplicate"],
            NOTIFY_ITEM_CHANGE,
            UPDATE_SOUL,
            CONNECTIVE_RESULT,
        )
        return True

    def handle_fondle(self, body, order):
        try:
            (soul_id,) = protocol_codec.decode_method(FONDLE, body)
        except ValueError as exc:
            log.warning("  invalid fondle body %s: %s", body.hex(), exc)
            return False
        if not self.account or self.player_snapshot is None:
            return False
        companion = storage.get_companion(self.uid, soul_id)
        if companion is None:
            return False
        action_cid = fondle_action_for(soul_id, companion["favor_level"])
        if action_cid == 0:
            log.warning("  fondle rejected: no action rule soulCid=%d favorLv=%d", soul_id, companion["favor_level"])
            return False
        add_favor = 100
        new_level = favor_level_for(
            soul_id,
            companion["favor"] + add_favor,
            bool(companion["oath_activation"]),
        )
        result = storage.apply_fondle(
            self.uid,
            soul_id,
            action_cid,
            add_favor,
            False,
            new_level,
            f"{self.session_id}:{order}",
        )
        if result is None:
            log.warning("  fondle rejected by local transaction uid=%s soulCid=%d", self.uid, soul_id)
            return False
        soul_pod = next(
            pod for pod in self.player_snapshot["souls"] if pod.get("cid") == soul_id
        )
        soul_pod.update(
            {
                "favor": result["favor"],
                "favorLv": result["favor_level"],
                "dailyDislike": result["dislike"],
            }
        )
        self.player_snapshot["fondleNum"] = result["fondle_num"]
        self.player_snapshot["nextRecoveryFondleTime"] = result["next_recovery_fondle_time"]
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, soul_pod))
        self.send(
            FONDLE_RESULT,
            protocol_codec.encode_method(
                FONDLE_RESULT,
                0,
                soul_id,
                action_cid,
                add_favor,
                result["dislike"],
                result["fondle_num"],
            ),
        )
        self.send(
            NOTIFY_FONDLE_RECOVERY,
            protocol_codec.encode_method(
                NOTIFY_FONDLE_RECOVERY,
                result["fondle_num"],
                result["next_recovery_fondle_time"],
            ),
        )
        module_handlers.notify_memory_unlocks(self, self.uid)
        log.info(
            "  fondle committed uid=%s soulCid=%d actionCid=%d addFavor=%d remaining=%d duplicate=%s -> %d/%d/%d",
            self.uid,
            soul_id,
            action_cid,
            add_favor,
            result["fondle_num"],
            result["duplicate"],
            UPDATE_SOUL,
            FONDLE_RESULT,
            NOTIFY_FONDLE_RECOVERY,
        )
        return True

    def handle_save_setting(self, body):
        try:
            key, value = protocol_codec.decode_method(SAVE_SETTING, body)
        except ValueError as exc:
            log.warning("  invalid save setting body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        storage.save_player_setting(self.uid, key, value)
        self.send(SAVE_SETTING_RESULT, protocol_codec.encode_method(SAVE_SETTING_RESULT, 0))
        log.info("  setting saved uid=%s key=%s -> %d", self.uid, key, SAVE_SETTING_RESULT)
        return True

    def handle_sign(self, body):
        if body or not self.account:
            return False
        result = storage.record_sign_in(self.uid)
        if result is None:
            return False
        quest = result.get("quest")
        if isinstance(quest, dict):
            quest_pod = {
                "cid": int(quest["cid"]),
                "finNum": int(quest["finNum"]),
                "tgtNum": int(quest["tgtNum"]),
                "createTime": int(quest["createTime"]),
            }
            is_remove = bool(quest.get("completed"))
            self.send(
                NOTIFY_UPDATE_QUEST,
                protocol_codec.encode_method(NOTIFY_UPDATE_QUEST, quest_pod, is_remove),
            )
            if quest.get("finishListChanged"):
                self.send(
                    NOTIFY_FINISH_QUEST_LIST,
                    protocol_codec.encode_method(
                        NOTIFY_FINISH_QUEST_LIST,
                        int(quest["cid"]),
                        False if quest["completed"] else True,
                    ),
                )
            if isinstance(self.player_snapshot, dict):
                quest_state = storage.get_quest_state(self.uid)
                self.player_snapshot.update(quest_state)
        self.send(
            SIGN_RESULT,
            protocol_codec.encode_method(SIGN_RESULT, 0, result["sign_info"], result["rewards"]),
        )
        log.info(
            "  daily sign uid=%s count=%d already=%s quest=%d/%d completed=%s -> %d",
            self.uid,
            result["sign_count"],
            result["already_signed"],
            quest["finNum"] if quest else -1,
            quest["tgtNum"] if quest else -1,
            quest["completed"] if quest else False,
            SIGN_RESULT,
        )
        return True

    def handle_get_lv_reach_rewards(self, body):
        if body or not self.account:
            return False
        seed_local_player_attrs(self.uid)
        migration = storage.migrate_claimed_reward_state(
            self.uid,
            "level_rewards",
            LEVEL_REACH_REWARD_MIGRATION,
            {reward_id: definition["rewards"] for reward_id, definition in LEVEL_REACH_REWARDS.items()},
        )
        if migration is None:
            return False
        if migration.get("migrated"):
            Session._send_reward_changes(self, migration)
        player = storage.get_player(self.uid) or {}
        state = storage.get_player_state_json(self.uid, "level_rewards") or {}
        claimed = {int(value) for value in state.get("claimed", []) if str(value).isdigit()}
        available = [reward_id for reward_id in sorted(LEVEL_REACH_REWARDS) if reward_id not in claimed]
        self.send(
            GET_LV_REACH_REWARDS_RESULT,
            protocol_codec.encode_method(GET_LV_REACH_REWARDS_RESULT, 0, available),
        )
        level = max(1, int(player.get("level", 1)))
        log.info("  lv reach reward list uid=%s level=%d available=%d -> %d", self.uid, level, len(available), GET_LV_REACH_REWARDS_RESULT)
        return True

    def handle_trigger_guide(self, body):
        try:
            guide_id, step, action = protocol_codec.decode_method(TRIGGER_GUIDE, body)
        except ValueError as exc:
            log.warning("  invalid trigger guide body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(TRIGGER_GUIDE_RESULT, protocol_codec.encode_method(TRIGGER_GUIDE_RESULT, 0, guide_id, step, action))
        log.info("  guide triggered uid=%s guideId=%d step=%d -> %d", self.uid, guide_id, step, TRIGGER_GUIDE_RESULT)
        return True

    def handle_refresh_read_point(self, body):
        try:
            (point_id,) = protocol_codec.decode_method(REFRESH_READ_POINT, body)
        except ValueError as exc:
            log.warning("  invalid refresh read point body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(REFRESH_READ_POINT_RESULT, protocol_codec.encode_method(REFRESH_READ_POINT_RESULT, 0))
        log.info("  read point refreshed uid=%s pointId=%d -> %d", self.uid, point_id, REFRESH_READ_POINT_RESULT)
        return True

    def handle_save_show_collect_items(self, body):
        try:
            (collect_map,) = protocol_codec.decode_method(SAVE_SHOW_COLLECT_ITEMS, body)
        except ValueError as exc:
            log.warning("  invalid save show collect body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        # Persist to settings for durability
        import json as _json
        storage.save_player_setting(self.uid, "show_collect", _json.dumps(collect_map, separators=(",", ":")))
        self.send(
            SAVE_SHOW_COLLECT_ITEMS_RESULT,
            protocol_codec.encode_method(SAVE_SHOW_COLLECT_ITEMS_RESULT, 0, collect_map),
        )
        log.info("  show collect saved uid=%s items=%d -> %d", self.uid, len(collect_map), SAVE_SHOW_COLLECT_ITEMS_RESULT)
        return True

    def handle_use_equip_skin(self, body):
        try:
            skin_id, status = protocol_codec.decode_method(USE_EQUIP_SKIN, body)
        except ValueError as exc:
            log.warning("  invalid use equip skin body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if skin_id not in ALL_EQUIP_SKIN_IDS or status not in (0, 1):
            log.warning("  rejected use equip skin uid=%s skinId=%s status=%s", self.uid, skin_id, status)
            return False
        equip_skins = _normalized_equip_skin_map(storage.get_player_state_json(self.uid, "equipSkins"))
        equip_skins[skin_id] = status
        storage.update_player_state_json(
            self.uid,
            "equipSkins",
            {str(cid): int(state) for cid, state in equip_skins.items()},
        )
        self.send(USE_EQUIP_SKIN_RESULT, protocol_codec.encode_method(USE_EQUIP_SKIN_RESULT, 0, skin_id, status))
        # The result callback is intentionally empty in the client. The notify
        # message is what updates PlayerModule.PlayerInfo.equipSkins in-place.
        self.send(NOTIFY_EQUIP_SKIN_UPDATE, protocol_codec.encode_method(NOTIFY_EQUIP_SKIN_UPDATE, skin_id, status))
        log.info("  equip skin used uid=%s skinId=%d status=%d -> %d/%d", self.uid, skin_id, status, USE_EQUIP_SKIN_RESULT, NOTIFY_EQUIP_SKIN_UPDATE)
        return True

    def handle_save_player_setting(self, body):
        try:
            key, value = protocol_codec.decode_method(SAVE_PLAYER_SETTING, body)
        except ValueError as exc:
            log.warning("  invalid save player setting body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        storage.save_player_setting(self.uid, f"ps_{key}", str(value))
        self.send(SAVE_PLAYER_SETTING_RESULT, protocol_codec.encode_method(SAVE_PLAYER_SETTING_RESULT, 0, key, value))
        log.info("  player setting saved uid=%s key=%d value=%d -> %d", self.uid, key, value, SAVE_PLAYER_SETTING_RESULT)
        return True

    def handle_dress_up_rotate_switch(self, body):
        try:
            (rotate_id,) = protocol_codec.decode_method(DRESS_UP_ROTATE_SWITCH, body)
        except ValueError as exc:
            log.warning("  invalid dress rotate switch body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(DRESS_UP_ROTATE_SWITCH_RESULT, protocol_codec.encode_method(DRESS_UP_ROTATE_SWITCH_RESULT, 0, rotate_id))
        log.info("  dress rotate switch uid=%s rotateId=%d -> %d", self.uid, rotate_id, DRESS_UP_ROTATE_SWITCH_RESULT)
        return True

    def handle_dress_up_rotate_list(self, body):
        try:
            (rotate_list,) = protocol_codec.decode_method(DRESS_UP_ROTATE_LIST, body)
        except ValueError as exc:
            log.warning("  invalid dress rotate list body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(
            DRESS_UP_ROTATE_LIST_RESULT,
            protocol_codec.encode_method(DRESS_UP_ROTATE_LIST_RESULT, 0, rotate_list),
        )
        log.info("  dress rotate list uid=%s count=%d -> %d", self.uid, len(rotate_list), DRESS_UP_ROTATE_LIST_RESULT)
        return True

    def handle_luck_draw(self, body):
        try:
            (draw_id,) = protocol_codec.decode_method(LUCK_DRAW, body)
        except ValueError as exc:
            log.warning("  invalid luck draw body %s: %s", body.hex(), exc)
            return False
        if not self.account or draw_id <= 0:
            return False
        day = time.strftime("%Y-%m-%d", time.localtime())
        state = storage.get_player_state_json(self.uid, "luck_draw") or {}
        if state.get("day") == day:
            self.send(LUCK_DRAW_RESULT, protocol_codec.encode_method(LUCK_DRAW_RESULT, 1, []))
            return True
        applied = storage.grant_reward_pairs(self.uid, [(10001, 1)])
        if applied is None:
            return False
        storage.update_player_state_json(
            self.uid,
            "luck_draw",
            {"day": day, "drawId": draw_id, "history": state.get("history", []) + [10001]},
        )
        Session._send_reward_changes(self, applied)
        self.send(LUCK_DRAW_RESULT, protocol_codec.encode_method(LUCK_DRAW_RESULT, 0, applied["rewards"]))
        log.info("  luck draw committed uid=%s drawId=%d -> %d", self.uid, draw_id, LUCK_DRAW_RESULT)
        return True

    def handle_get_girls(self, body):
        if body or not self.account:
            return False
        girls = []
        for soul in storage.get_souls(self.uid):
            soul_id = int(soul["soul_id"])
            girls.append({
                "soulCid": soul_id,
                "activation": bool(soul.get("oath_activation", False)),
                "datingRecord": storage.get_dating_records(self.uid, soul_id),
            })
        self.send(GET_GIRLS_RESULT, protocol_codec.encode_method(GET_GIRLS_RESULT, 0, girls))
        log.info("  companion state loaded uid=%s count=%d -> %d", self.uid, len(girls), GET_GIRLS_RESULT)
        return True

    def handle_give_up_quest(self, body):
        try:
            (quest_id,) = protocol_codec.decode_method(GIVE_UP_QUEST, body)
        except ValueError as exc:
            log.warning("  invalid quest give-up body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.give_up_quest(self.uid, quest_id):
            return False
        self.send(
            GIVE_UP_QUEST_RESULT,
            protocol_codec.encode_method(GIVE_UP_QUEST_RESULT, 0, quest_id),
        )
        log.info("  quest given up uid=%s quest=%d -> %d", self.uid, quest_id, GIVE_UP_QUEST_RESULT)
        return True

    def handle_get_luck_draw_history(self, body):
        if body or not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "luck_draw") or {}
        history = [
            {"itemCid": int(cid), "itemNum": 1, "time": 0}
            for cid in state.get("history", [])
            if isinstance(cid, int) and cid > 0
        ]
        self.send(GET_LUCK_DRAW_HISTORY_RESULT, protocol_codec.encode_method(GET_LUCK_DRAW_HISTORY_RESULT, 0, history))
        log.info("  luck draw history uid=%s count=%d -> %d", self.uid, len(history), GET_LUCK_DRAW_HISTORY_RESULT)
        return True

    def handle_get_lv_reach_reward(self, body):
        try:
            (reward_id,) = protocol_codec.decode_method(GET_LV_REACH_REWARD, body)
        except ValueError as exc:
            log.warning("  invalid lv reach reward body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        player = storage.get_player(self.uid) or {}
        level = max(1, int(player.get("level", 1)))
        definition = LEVEL_REACH_REWARDS.get(reward_id)
        if definition is None or level < definition["target_level"]:
            return False
        seed_local_player_attrs(self.uid)
        applied = storage.claim_reward_once(
            self.uid,
            "level_rewards",
            str(reward_id),
            definition["rewards"],
        )
        if applied is None:
            return False
        Session._send_reward_changes(self, applied)
        self.send(GET_LV_REACH_REWARD_RESULT, protocol_codec.encode_method(GET_LV_REACH_REWARD_RESULT, 0, applied["rewards"]))
        log.info("  lv reach reward uid=%s rewardId=%d claimed=%s -> %d", self.uid, reward_id, applied["claimed"], GET_LV_REACH_REWARD_RESULT)
        return True

    def handle_get_refunds_gift_packs(self, body):
        try:
            (pack_ids,) = protocol_codec.decode_method(GET_REFUNDS_GIFT_PACKS, body)
        except ValueError as exc:
            log.warning("  invalid refunds gift packs body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if any(not isinstance(pack_id, int) or pack_id <= 0 for pack_id in pack_ids):
            return False
        rewards = []
        for pack_id in pack_ids:
            applied = storage.claim_reward_once(
                self.uid,
                "refund_gift_packs",
                str(pack_id),
                [(1, 1000), (10006, 1)],
            )
            if applied is None:
                return False
            Session._send_reward_changes(self, applied)
            rewards.extend(applied["rewards"])
        self.send(GET_REFUNDS_GIFT_PACKS_RESULT, protocol_codec.encode_method(GET_REFUNDS_GIFT_PACKS_RESULT, 0, rewards))
        log.info("  refunds gift packs uid=%s packs=%d rewards=%d -> %d", self.uid, len(pack_ids), len(rewards), GET_REFUNDS_GIFT_PACKS_RESULT)
        return True

    # ── net_item handlers ──

    def handle_sell_item(self, body):
        try:
            item_id, count = protocol_codec.decode_method(SELL_ITEM, body)
        except ValueError as exc:
            log.warning("  invalid sell item body %s: %s", body.hex(), exc)
            return False
        if not self.account or item_id <= 0 or count <= 0:
            return False
        seed_local_player_attrs(self.uid)
        # Item sell prices are defined by the local item table when available;
        # the deterministic fallback keeps unpriced event items usable offline.
        result = storage.trade_reward_pairs(self.uid, [(int(item_id), count)], [(1, count * 10)])
        if result is None:
            return False
        Session._send_reward_changes(self, result)
        self.send(SELL_ITEM_RESULT, protocol_codec.encode_method(SELL_ITEM_RESULT, 0))
        log.info("  sell item committed uid=%s itemId=%d count=%d -> %d", self.uid, item_id, count, SELL_ITEM_RESULT)
        return True

    def handle_use_item(self, body):
        try:
            item_id, count = protocol_codec.decode_method(USE_ITEM, body)
        except ValueError as exc:
            log.warning("  invalid use item body %s: %s", body.hex(), exc)
            return False
        if not self.account or item_id <= 0 or count <= 0:
            return False
        seed_local_player_attrs(self.uid)
        result = storage.trade_reward_pairs(self.uid, [(int(item_id), count)], [])
        if result is None:
            return False
        records = [{"itemCid": int(item_id), "useTime": int(time.time())}]
        Session._send_reward_changes(self, result)
        self.send(USE_ITEM_RESULT, protocol_codec.encode_method(USE_ITEM_RESULT, 0, [], records))
        log.info("  use item committed uid=%s itemId=%d count=%d -> %d", self.uid, item_id, count, USE_ITEM_RESULT)
        return True

    def handle_destroy_item(self, body):
        try:
            (item_id,) = protocol_codec.decode_method(DESTROY_ITEM, body)
        except ValueError as exc:
            log.warning("  invalid destroy item body %s: %s", body.hex(), exc)
            return False
        if not self.account or item_id <= 0:
            return False
        seed_local_player_attrs(self.uid)
        result = storage.trade_reward_pairs(self.uid, [(int(item_id), 1)], [])
        if result is None:
            return False
        Session._send_reward_changes(self, result)
        self.send(DESTROY_ITEM_RESULT, protocol_codec.encode_method(DESTROY_ITEM_RESULT, 0))
        log.info("  destroy item committed uid=%s itemId=%d -> %d", self.uid, item_id, DESTROY_ITEM_RESULT)
        return True

    def handle_exchange_batch(self, body):
        try:
            (exchange_map,) = protocol_codec.decode_method(EXCHANGE_BATCH, body)
        except ValueError as exc:
            log.warning("  invalid exchange batch body %s: %s", body.hex(), exc)
            return False
        if not self.account or any(not isinstance(cid, int) or not isinstance(num, int) or cid <= 0 or num <= 0 for cid, num in exchange_map.items()):
            return False
        seed_local_player_attrs(self.uid)
        rows = {
            int(exchange_id): module_rules._row("economy", "ExchangeTable", exchange_id)
            for exchange_id in exchange_map
        }
        result = storage.apply_exchange_batch(self.uid, exchange_map, rows)
        if result is None:
            self.send(EXCHANGE_BATCH_RESULT, protocol_codec.encode_method(EXCHANGE_BATCH_RESULT, 1, {}, [], {}))
            return True
        Session._send_reward_changes(self, result)
        success = result["success"]
        self.send(EXCHANGE_BATCH_RESULT, protocol_codec.encode_method(EXCHANGE_BATCH_RESULT, 0, success, result["rewards"], success))
        log.info("  exchange batch committed uid=%s count=%d -> %d", self.uid, len(exchange_map), EXCHANGE_BATCH_RESULT)
        return True

    def handle_lock_equipment(self, body):
        try:
            (equip_id,) = protocol_codec.decode_method(LOCK_EQUIPMENT, body)
        except ValueError as exc:
            log.warning("  invalid lock equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        locked = storage.set_equipment_locked(self.uid, equip_id)
        if locked is None:
            return False
        self.send(LOCK_EQUIPMENT_RESULT, protocol_codec.encode_method(LOCK_EQUIPMENT_RESULT, 0, equip_id, locked))
        log.info("  equipment lock toggle uid=%s equipId=%d -> %d", self.uid, equip_id, LOCK_EQUIPMENT_RESULT)
        return True

    def handle_optional_gift(self, body):
        try:
            gift_id, options, count = protocol_codec.decode_method(OPTIONAL_GIFT, body)
        except ValueError as exc:
            log.warning("  invalid optional gift body %s: %s", body.hex(), exc)
            return False
        if not self.account or gift_id <= 0 or count <= 0 or not options:
            return False
        option = next((int(cid) for cid in options if isinstance(cid, int) and cid > 0), 0)
        if not option:
            return False
        seed_local_player_attrs(self.uid)
        result = storage.trade_reward_pairs(self.uid, [(int(gift_id), int(count))], [(option, int(count))])
        if result is None:
            self.send(OPTIONAL_GIFT_RESULT, protocol_codec.encode_method(OPTIONAL_GIFT_RESULT, 1, []))
            return True
        Session._send_reward_changes(self, result)
        self.send(OPTIONAL_GIFT_RESULT, protocol_codec.encode_method(OPTIONAL_GIFT_RESULT, 0, result["rewards"]))
        log.info("  optional gift committed uid=%s giftId=%d option=%d count=%d -> %d", self.uid, gift_id, option, count, OPTIONAL_GIFT_RESULT)
        return True

    # ── net_soul handlers ──

    def handle_unlock_soul(self, body):
        try:
            (soul_id,) = protocol_codec.decode_method(UNLOCK_SOUL, body)
        except ValueError as exc:
            log.warning("  invalid unlock soul body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.get_companion(self.uid, soul_id):
            return False
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        state.setdefault(str(soul_id), {})["unlocked"] = True
        storage.update_player_state_json(self.uid, "soul_progress", state)
        self.send(UNLOCK_SOUL_RESULT, protocol_codec.encode_method(UNLOCK_SOUL_RESULT, 0))
        log.info("  soul unlocked uid=%s soulId=%d -> %d", self.uid, soul_id, UNLOCK_SOUL_RESULT)
        return True

    def handle_use_soul_exp_item(self, body):
        try:
            soul_id, exp_map = protocol_codec.decode_method(USE_SOUL_EXP_ITEM, body)
        except ValueError as exc:
            log.warning("  invalid use soul exp body %s: %s", body.hex(), exc)
            self.send(
                USE_SOUL_EXP_ITEM_RESULT,
                protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 1),
            )
            return True
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        if companion is None:
            log.warning("  soul exp rejected: unknown soul uid=%s soulId=%s", self.uid, soul_id)
            self.send(
                USE_SOUL_EXP_ITEM_RESULT,
                protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 1),
            )
            return True
        if (
            not isinstance(exp_map, dict)
            or any(
                not isinstance(cid, int)
                or cid <= 0
                or not isinstance(num, int)
                or num <= 0
                for cid, num in exp_map.items()
            )
        ):
            log.warning("  soul exp rejected: malformed items uid=%s soulId=%d", self.uid, soul_id)
            self.send(
                USE_SOUL_EXP_ITEM_RESULT,
                protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 1),
            )
            return True
        if any(cid not in SOUL_EXP_VALUES for cid in exp_map):
            log.warning("  unsupported soul exp item uid=%s soulId=%d items=%s", self.uid, soul_id, list(exp_map))
            self.send(USE_SOUL_EXP_ITEM_RESULT, protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 1))
            return True
        local_pod_method = getattr(self, "_local_soul_pod", None)
        current_pod = (
            local_pod_method(soul_id)
            if callable(local_pod_method)
            else local_soul_pod_for(self.uid, soul_id)
        )
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        stored_progress = state.get(str(soul_id), {})
        progress = dict(stored_progress) if isinstance(stored_progress, dict) else {}
        current_exp = progress.get("exp", current_pod.get("exp", 0))
        added_exp = sum(SOUL_EXP_VALUES[cid] * int(num) for cid, num in exp_map.items())
        level, remaining_exp = _soul_level_after_exp(
            self.uid, companion.get("level", 1), current_exp, added_exp
        )
        progress["exp"] = remaining_exp
        state[str(soul_id)] = progress
        result = storage.commit_soul_growth(
            self.uid, soul_id, state, list(exp_map.items()), level=level
        )
        if result is None:
            self.send(USE_SOUL_EXP_ITEM_RESULT, protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 1))
            return True
        Session._send_reward_changes(self, result)
        self.send(USE_SOUL_EXP_ITEM_RESULT, protocol_codec.encode_method(USE_SOUL_EXP_ITEM_RESULT, 0))
        updated_pod = (
            local_pod_method(soul_id)
            if callable(local_pod_method)
            else local_soul_pod_for(self.uid, soul_id)
        )
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, updated_pod))
        log.info(
            "  soul exp committed uid=%s soulId=%d level=%d exp=%d -> %d",
            self.uid, soul_id, level, remaining_exp, USE_SOUL_EXP_ITEM_RESULT,
        )
        return True

    def _send_soul_growth_failure(self, result_id, soul_id=0, spirit_id=0):
        """Always finish a growth request so the client cannot wait forever."""
        if result_id in (EVOLUTION_RESULT, ACTIVE_TALENT_RESULT, ACTIVE_TALENT_GROUP_RESULT):
            pod = Session._local_soul_pod(self, soul_id) if storage.get_companion(self.uid, int(soul_id or 0)) else {}
            self.send(result_id, protocol_codec.encode_method(result_id, 1, pod))
        elif result_id in (UNLOCK_SKILL_GROUP_RESULT, ACTIVATION_SKILL_STRENGTHEN_RESULT):
            self.send(result_id, protocol_codec.encode_method(result_id, 1))
        else:
            self.send(result_id, protocol_codec.encode_method(result_id, 1, int(soul_id or 0), int(spirit_id or 0)))
        return True

    def _growth_commit(self, soul_id, progress, costs, level=None):
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        state[str(soul_id)] = progress
        result = storage.commit_soul_growth(
            self.uid, soul_id, state, costs, level=level
        )
        if result is None:
            return None
        Session._send_reward_changes(self, result)
        return progress

    def handle_evolution(self, body):
        try:
            (soul_id,) = protocol_codec.decode_method(EVOLUTION, body)
        except ValueError as exc:
            log.warning("  invalid evolution body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, EVOLUTION_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        current = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(current.get(str(soul_id), {}) or {})
        current_pod = Session._local_soul_pod(self, soul_id)
        quality_id = int(
            progress.get("qualityId", 0) or current_pod.get("qualityId", 0) or 0
        )
        row = _growth_quality_row(soul_id, quality_id)
        next_quality_id = int(row.get("NextLevel", 0) or 0) if row else 0
        next_row = _growth_row("quality", next_quality_id) if next_quality_id > 0 else None
        if (
            companion is None
            or row is None
            or next_row is None
            or int(next_row.get("SoulId", 0) or 0) != int(soul_id)
            or int(companion.get("level", 1) or 1) < int(row.get("NeedSoulLevel", 1) or 1)
        ):
            return Session._send_soul_growth_failure(self, EVOLUTION_RESULT, soul_id)
        costs = _growth_pairs(row.get("Cost"))
        if not costs:
            return Session._send_soul_growth_failure(self, EVOLUTION_RESULT, soul_id)
        progress["qualityId"] = next_quality_id
        if Session._growth_commit(self, soul_id, progress, costs) is None:
            return Session._send_soul_growth_failure(self, EVOLUTION_RESULT, soul_id)
        self.send(EVOLUTION_RESULT, protocol_codec.encode_method(EVOLUTION_RESULT, 0, Session._local_soul_pod(self, soul_id)))
        log.info("  evolution committed uid=%s soulId=%d quality=%d -> %d", self.uid, soul_id, progress["qualityId"], EVOLUTION_RESULT)
        return True

    def handle_active_talent(self, body):
        try:
            soul_id, talent_id = protocol_codec.decode_method(ACTIVE_TALENT, body)
        except ValueError as exc:
            log.warning("  invalid active talent body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(state.get(str(soul_id), {}) or {})
        row = _growth_row("talent", talent_id)
        expected_prefix = _growth_soul_prefix(soul_id)
        active = {int(value) for value in progress.get("talents", []) if isinstance(value, int)}
        pre_talents = row.get("PreTalent", []) if isinstance(row, dict) else []
        if isinstance(pre_talents, int):
            pre_talents = [pre_talents]
        valid = (
            companion is not None
            and row is not None
            and int(row.get("GroupId", 0) or 0) // 100 == expected_prefix
            and talent_id not in active
            and int(companion.get("level", 1) or 1) >= int(row.get("ActivationLv", 1) or 1)
            and all(int(value) in active for value in pre_talents if int(value) > 0)
        )
        if not valid:
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_RESULT, soul_id)
        costs = _growth_pairs(row.get("Cost"))
        if not costs:
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_RESULT, soul_id)
        progress["talents"] = sorted(active | {int(talent_id)})
        if Session._growth_commit(self, soul_id, progress, costs) is None:
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_RESULT, soul_id)
        self.send(ACTIVE_TALENT_RESULT, protocol_codec.encode_method(ACTIVE_TALENT_RESULT, 0, Session._local_soul_pod(self, soul_id)))
        log.info("  talent activated uid=%s soulId=%d talentId=%d -> %d", self.uid, soul_id, talent_id, ACTIVE_TALENT_RESULT)
        return True

    def handle_active_talent_group(self, body):
        try:
            soul_id, group_id = protocol_codec.decode_method(ACTIVE_TALENT_GROUP, body)
        except ValueError as exc:
            log.warning("  invalid active talent group body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_GROUP_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(state.get(str(soul_id), {}) or {})
        row = _growth_row("talentGroup", group_id)
        active = {int(value) for value in progress.get("talentGroups", []) if isinstance(value, int)}
        pre_group = int(row.get("PreGroup", 0) or 0) if isinstance(row, dict) else 0
        valid = (
            companion is not None
            and row is not None
            and int(row.get("SoulId", soul_id) or soul_id) == int(soul_id)
            and group_id not in active
            and int(companion.get("level", 1) or 1) >= int(row.get("UnlockLv", 1) or 1)
            and (pre_group <= 0 or pre_group in active)
        )
        if not valid:
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_GROUP_RESULT, soul_id)
        progress["talentGroups"] = sorted(active | {int(group_id)})
        if Session._growth_commit(self, soul_id, progress, []) is None:
            return Session._send_soul_growth_failure(self, ACTIVE_TALENT_GROUP_RESULT, soul_id)
        self.send(ACTIVE_TALENT_GROUP_RESULT, protocol_codec.encode_method(ACTIVE_TALENT_GROUP_RESULT, 0, Session._local_soul_pod(self, soul_id)))
        log.info("  talent group activated uid=%s soulId=%d groupId=%d -> %d", self.uid, soul_id, group_id, ACTIVE_TALENT_GROUP_RESULT)
        return True

    def handle_unlock_skill_group(self, body):
        try:
            soul_id, group_id = protocol_codec.decode_method(UNLOCK_SKILL_GROUP, body)
        except ValueError as exc:
            log.warning("  invalid unlock skill group body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, UNLOCK_SKILL_GROUP_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(state.get(str(soul_id), {}) or {})
        row = _growth_row("skillGroup", group_id)
        active = {int(value) for value in progress.get("skillGroups", []) if isinstance(value, int)}
        valid = (
            companion is not None
            and row is not None
            and int(row.get("Soul", soul_id) or soul_id) == int(soul_id)
            and group_id not in active
            and int(companion.get("level", 1) or 1) >= int(row.get("UnlockLv", 1) or 1)
        )
        if not valid:
            return Session._send_soul_growth_failure(self, UNLOCK_SKILL_GROUP_RESULT, soul_id)
        progress["skillGroups"] = sorted(active | {int(group_id)})
        if Session._growth_commit(self, soul_id, progress, []) is None:
            return Session._send_soul_growth_failure(self, UNLOCK_SKILL_GROUP_RESULT, soul_id)
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, Session._local_soul_pod(self, soul_id)))
        self.send(UNLOCK_SKILL_GROUP_RESULT, protocol_codec.encode_method(UNLOCK_SKILL_GROUP_RESULT, 0))
        log.info("  skill group unlocked uid=%s soulId=%d groupId=%d -> %d", self.uid, soul_id, group_id, UNLOCK_SKILL_GROUP_RESULT)
        return True

    def handle_activation_skill_strengthen(self, body):
        try:
            soul_id, skill_id, strengthen_cid = protocol_codec.decode_method(ACTIVATION_SKILL_STRENGTHEN, body)
        except ValueError as exc:
            log.warning("  invalid activation skill strengthen body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, ACTIVATION_SKILL_STRENGTHEN_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(state.get(str(soul_id), {}) or {})
        row = _growth_row("skillStrengthen", strengthen_cid)
        active = _normalized_skill_strengthens(progress)
        valid = (
            companion is not None
            and row is not None
            and int(row.get("INSkill", 0) or 0) == int(skill_id)
            and _growth_skill_belongs_to_soul(soul_id, skill_id)
            and strengthen_cid not in active
            and int(companion.get("level", 1) or 1) >= int(row.get("UnLockLv", row.get("UnlockLv", 1)) or 1)
        )
        if not valid:
            return Session._send_soul_growth_failure(self, ACTIVATION_SKILL_STRENGTHEN_RESULT, soul_id)
        costs = _growth_pairs(row.get("UnLockCost"))
        if costs is None:
            return Session._send_soul_growth_failure(self, ACTIVATION_SKILL_STRENGTHEN_RESULT, soul_id)
        progress["activationSkillStrengthen"] = sorted(set(active + [int(strengthen_cid)]))
        progress.pop("skillStrengthens", None)
        if Session._growth_commit(self, soul_id, progress, costs) is None:
            return Session._send_soul_growth_failure(self, ACTIVATION_SKILL_STRENGTHEN_RESULT, soul_id)
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, Session._local_soul_pod(self, soul_id)))
        self.send(ACTIVATION_SKILL_STRENGTHEN_RESULT, protocol_codec.encode_method(ACTIVATION_SKILL_STRENGTHEN_RESULT, 0))
        log.info("  skill strengthen committed uid=%s soulId=%d skillId=%d strengthenCid=%d -> %d", self.uid, soul_id, skill_id, strengthen_cid, ACTIVATION_SKILL_STRENGTHEN_RESULT)
        return True

    def handle_active_special_spirit(self, body):
        try:
            soul_id, spirit_id = protocol_codec.decode_method(ACTIVE_SPECIAL_SPIRIT, body)
        except ValueError as exc:
            log.warning("  invalid active special spirit body %s: %s", body.hex(), exc)
            return Session._send_soul_growth_failure(self, ACTIVE_SPECIAL_SPIRIT_RESULT)
        companion = storage.get_companion(self.uid, soul_id) if self.account else None
        state = storage.get_player_state_json(self.uid, "soul_progress") or {}
        progress = dict(state.get(str(soul_id), {}) or {})
        row = _growth_row("specialSpirit", spirit_id)
        active = {int(value) for value in progress.get("specialSpirit", []) if isinstance(value, int)}
        pre = row.get("PreSpirit", []) if isinstance(row, dict) else []
        pre_group = int(pre[0]) if isinstance(pre, list) and len(pre) >= 2 else 0
        pre_level = int(pre[1]) if isinstance(pre, list) and len(pre) >= 2 else 0
        previous_ok = True
        if pre_group > 0:
            previous_ok = any(
                isinstance(candidate, dict)
                and int(candidate.get("Group", 0) or 0) == pre_group
                and int(candidate.get("Level", 0) or 0) >= pre_level
                for candidate in (
                    _growth_row("specialSpirit", active_id) for active_id in active
                )
            )
        valid = (
            companion is not None
            and row is not None
            and int(row.get("SoulId", soul_id) or soul_id) == int(soul_id)
            and spirit_id not in active
            and int(companion.get("level", 1) or 1) >= int(row.get("NeedSoulLevel", 1) or 1)
            and previous_ok
        )
        if not valid:
            return Session._send_soul_growth_failure(self, ACTIVE_SPECIAL_SPIRIT_RESULT, soul_id, spirit_id)
        costs = _growth_pairs(row.get("Cost"))
        # Skill-linked spirit nodes intentionally omit Cost in the extracted
        # config; they are unlocked by the skill/pre-spirit requirements only.
        progress["specialSpirit"] = sorted(active | {int(spirit_id)})
        if Session._growth_commit(self, soul_id, progress, costs) is None:
            return Session._send_soul_growth_failure(self, ACTIVE_SPECIAL_SPIRIT_RESULT, soul_id, spirit_id)
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, Session._local_soul_pod(self, soul_id)))
        self.send(ACTIVE_SPECIAL_SPIRIT_RESULT, protocol_codec.encode_method(ACTIVE_SPECIAL_SPIRIT_RESULT, 0, soul_id, spirit_id))
        log.info("  special spirit activated uid=%s soulId=%d spiritId=%d -> %d", self.uid, soul_id, spirit_id, ACTIVE_SPECIAL_SPIRIT_RESULT)
        return True

    # ── net_player remaining handlers ──

    def handle_disbind_role(self, body):
        if not self.account:
            return False
        if body:
            try:
                (role_id,) = protocol_codec.decode_method(DISBIND_ROLE, body)
            except ValueError as exc:
                log.warning("  invalid disbind role body %s: %s", body.hex(), exc)
                return False
        else:
            role_id = ""
        storage.update_player_state_json(self.uid, "disbound_role", {"roleId": role_id, "time": int(time.time())})
        self.send(DISBIND_ROLE_RESULT, protocol_codec.encode_method(DISBIND_ROLE_RESULT, 0))
        log.info("  disbind role committed uid=%s roleId=%s -> %d", self.uid, role_id, DISBIND_ROLE_RESULT)
        return True

    def handle_change_data(self, body):
        try:
            data_type, data_value = protocol_codec.decode_method(CHANGE_DATA, body)
        except ValueError as exc:
            log.warning("  invalid change data body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        profile = storage.get_player_state_json(self.uid, "player_profile") or {}
        if data_type == 1:
            if not data_value:
                return False
            profile["name"] = data_value
        else:
            profile[str(data_type)] = data_value
        storage.update_player_state_json(self.uid, "player_profile", profile)
        self.send(CHANGE_DATA_RESULT, protocol_codec.encode_method(CHANGE_DATA_RESULT, 0, data_type, player_base_info_for(self.uid)))
        log.info("  change data committed uid=%s type=%d -> %d", self.uid, data_type, CHANGE_DATA_RESULT)
        return True

    def handle_get_player_info(self, body):
        try:
            pid, name = protocol_codec.decode_method(GET_PLAYER_INFO, body)
        except ValueError as exc:
            log.warning("  invalid get player info body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        player_info = {"baseInfo": player_base_info_for(self.uid, pid, name), "showCollectItems": {}, "allCollectItems": [], "finishMaze": [], "soulCount": len(storage.get_souls(self.uid)), "guildId": 0, "guildName": ""}
        self.send(GET_PLAYER_INFO_RESULT, protocol_codec.encode_method(GET_PLAYER_INFO_RESULT, 0, player_info))
        log.info("  get player info uid=%s targetPid=%s -> %d", self.uid, pid, GET_PLAYER_INFO_RESULT)
        return True

    def handle_send_gift_code(self, body):
        try:
            (code,) = protocol_codec.decode_method(SEND_GIFT_CODE, body)
        except ValueError as exc:
            log.warning("  invalid send gift code body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not code:
            return False
        applied = storage.claim_reward_once(self.uid, "gift_codes", str(code), [(1, 500), (10006, 1)])
        if applied is None:
            return False
        Session._send_reward_changes(self, applied)
        self.send(SEND_GIFT_CODE_RESULT, protocol_codec.encode_method(SEND_GIFT_CODE_RESULT, 0, applied["rewards"]))
        log.info("  gift code uid=%s code=%s claimed=%s -> %d", self.uid, code, applied["claimed"], SEND_GIFT_CODE_RESULT)
        return True

    def handle_buy_advance_level_chase(self, body):
        if body or not self.account:
            return False
        applied = storage.claim_reward_once(self.uid, "advance_level_chase", "purchase", [(1, 1000), (10006, 1)])
        if applied is None:
            return False
        Session._send_reward_changes(self, applied)
        self.send(BUY_ADVANCE_LEVEL_CHASE_RESULT, protocol_codec.encode_method(BUY_ADVANCE_LEVEL_CHASE_RESULT, 0))
        log.info("  buy advance level chase uid=%s claimed=%s -> %d", self.uid, applied["claimed"], BUY_ADVANCE_LEVEL_CHASE_RESULT)
        return True

    def handle_shop_buy(self, body):
        try:
            shop_id, goods_id = protocol_codec.decode_method(SHOP_BUY, body)
        except ValueError as exc:
            log.warning("  invalid shop buy body %s: %s", body.hex(), exc)
            return False
        if not self.account or shop_id <= 0 or goods_id <= 0:
            return False
        state = storage.get_player_state_json(self.uid, "shops") or {}
        shop = state.setdefault(str(shop_id), {"bought": [], "refresh": 0})
        if goods_id in shop.get("bought", []):
            self.send(SHOP_BUY_RESULT, protocol_codec.encode_method(SHOP_BUY_RESULT, 1, shop_id, goods_id))
            return True
        seed_local_player_attrs(self.uid)
        # Local shop goods use the extracted goods CID as the item CID. The
        # common shop currency is CID 2; stock is one purchase per refresh.
        result = storage.trade_reward_pairs(self.uid, [(2, 1)], [(goods_id, 1)])
        if result is None:
            self.send(SHOP_BUY_RESULT, protocol_codec.encode_method(SHOP_BUY_RESULT, 1, shop_id, goods_id))
            return True
        shop.setdefault("bought", []).append(goods_id)
        storage.update_player_state_json(self.uid, "shops", state)
        Session._send_reward_changes(self, result)
        self.send(SHOP_BUY_RESULT, protocol_codec.encode_method(SHOP_BUY_RESULT, 0, shop_id, goods_id))
        log.info("  shop buy committed uid=%s shopId=%d goodsId=%d -> %d", self.uid, shop_id, goods_id, SHOP_BUY_RESULT)
        return True

    def handle_shop_refresh(self, body):
        try:
            shop_id, refresh_type = protocol_codec.decode_method(SHOP_REFRESH, body)
        except ValueError as exc:
            log.warning("  invalid shop refresh body %s: %s", body.hex(), exc)
            return False
        if not self.account or shop_id <= 0 or refresh_type < 0:
            return False
        state = storage.get_player_state_json(self.uid, "shops") or {}
        shop = state.setdefault(str(shop_id), {"bought": [], "refresh": 0})
        shop["refresh"] = int(shop.get("refresh", 0)) + 1
        shop["bought"] = []
        storage.update_player_state_json(self.uid, "shops", state)
        self.send(SHOP_REFRESH_RESULT, protocol_codec.encode_method(SHOP_REFRESH_RESULT, 0, shop_id, shop["refresh"]))
        log.info("  shop refreshed uid=%s shopId=%d type=%d -> %d", self.uid, shop_id, refresh_type, SHOP_REFRESH_RESULT)
        return True

    def handle_remove_friends(self, body):
        try:
            (friend_ids,) = protocol_codec.decode_method(REMOVE_FRIENDS, body)
        except ValueError as exc:
            log.warning("  invalid remove friends body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
        for friend_id in friend_ids:
            target = local_player_by_ref(friend_id)
            if target:
                storage.remove_friend_pair(self.uid, target["uid"])
            else:
                state.setdefault("friends", {}).pop(str(friend_id), None)
                state.setdefault("remarks", {}).pop(str(friend_id), None)
        storage.update_player_state_json(self.uid, "social", state)
        self.send(REMOVE_FRIENDS_RESULT, protocol_codec.encode_method(REMOVE_FRIENDS_RESULT, 0))
        log.info("  friends removed uid=%s count=%d -> %d", self.uid, len(friend_ids), REMOVE_FRIENDS_RESULT)
        return True

    def handle_apply_friends(self, body):
        try:
            (player_ids,) = protocol_codec.decode_method(APPLY_FRIENDS, body)
        except ValueError as exc:
            log.warning("  invalid apply friends body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        for player_id in player_ids:
            target = local_player_by_ref(player_id)
            if target and target["uid"] != self.uid:
                storage.apply_friend_request(self.uid, target["uid"])
        self.send(APPLY_FRIENDS_RESULT, protocol_codec.encode_method(APPLY_FRIENDS_RESULT, 0))
        log.info("  friend applications saved uid=%s count=%d -> %d", self.uid, len(player_ids), APPLY_FRIENDS_RESULT)
        return True

    def handle_deal_with_apply(self, body):
        try:
            player_ids, accepted = protocol_codec.decode_method(DEAL_WITH_APPLY, body)
        except ValueError as exc:
            log.warning("  invalid deal with apply body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        for player_id in player_ids:
            target = local_player_by_ref(player_id)
            if target:
                storage.handle_friend_application(self.uid, target["uid"], bool(accepted))
            else:
                state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
                key = str(player_id)
                pending = state.setdefault("pending", {}).pop(key, None)
                if accepted and pending:
                    state.setdefault("friends", {})[key] = pending
                storage.update_player_state_json(self.uid, "social", state)
        self.send(DEAL_WITH_APPLY_RESULT, protocol_codec.encode_method(DEAL_WITH_APPLY_RESULT, 0))
        log.info("  friend applications handled uid=%s accepted=%s -> %d", self.uid, accepted, DEAL_WITH_APPLY_RESULT)
        return True

    def handle_add_blacklist(self, body):
        try:
            (player_id,) = protocol_codec.decode_method(ADD_BLACKLIST, body)
        except ValueError as exc:
            log.warning("  invalid add blacklist body %s: %s", body.hex(), exc)
            return False
        if not self.account or not player_id:
            return False
        state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
        target = local_player_by_ref(player_id)
        key = str(target["uid"] if target else player_id)
        state.setdefault("blacklist", {})[key] = True
        state.setdefault("friends", {}).pop(key, None)
        storage.update_player_state_json(self.uid, "social", state)
        if target:
            storage.remove_friend_pair(self.uid, target["uid"])
        self.send(ADD_BLACKLIST_RESULT, protocol_codec.encode_method(ADD_BLACKLIST_RESULT, 0))
        log.info("  blacklist saved uid=%s pid=%s -> %d", self.uid, player_id, ADD_BLACKLIST_RESULT)
        return True

    def handle_remove_blacklist(self, body):
        try:
            (player_ids,) = protocol_codec.decode_method(REMOVE_BLACKLIST, body)
        except ValueError as exc:
            log.warning("  invalid remove blacklist body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
        for player_id in player_ids:
            state.setdefault("blacklist", {}).pop(str(player_id), None)
        storage.update_player_state_json(self.uid, "social", state)
        self.send(REMOVE_BLACKLIST_RESULT, protocol_codec.encode_method(REMOVE_BLACKLIST_RESULT, 0))
        log.info("  blacklist entries removed uid=%s count=%d -> %d", self.uid, len(player_ids), REMOVE_BLACKLIST_RESULT)
        return True

    def handle_search_player(self, body):
        try:
            (query,) = protocol_codec.decode_method(SEARCH_PLAYER, body)
        except ValueError as exc:
            log.warning("  invalid search player body %s: %s", body.hex(), exc)
            return False
        if not self.account or not query:
            return False
        target = local_player_by_ref(query)
        if target is None:
            self.send(SEARCH_PLAYER_RESULT, protocol_codec.encode_method(SEARCH_PLAYER_RESULT, 1, {}))
            return True
        pod = local_friend_pod(target)
        self.send(SEARCH_PLAYER_RESULT, protocol_codec.encode_method(SEARCH_PLAYER_RESULT, 0, pod))
        log.info("  player searched uid=%s query=%s -> %d", self.uid, query, SEARCH_PLAYER_RESULT)
        return True

    def handle_set_remark(self, body):
        try:
            friend_id, remark = protocol_codec.decode_method(SET_REMARK, body)
        except ValueError as exc:
            log.warning("  invalid set remark body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
        target = local_player_by_ref(friend_id)
        key = str(target["uid"] if target else friend_id)
        state.setdefault("remarks", {})[key] = str(remark)
        if target:
            if key in state.setdefault("friends", {}):
                state["friends"][key]["remark"] = str(remark)
        storage.update_player_state_json(self.uid, "social", state)
        friend_pod = local_friend_pod(target, remark) if target else {"id": int(friend_id), "pId": key, "pName": key, "remark": str(remark), "pLv": 1, "online": False, "serverId": "offline-local", "type": 0}
        self.send(SET_REMARK_RESULT, protocol_codec.encode_method(SET_REMARK_RESULT, 0, friend_pod))
        log.info("  friend remark saved uid=%s friendId=%d -> %d", self.uid, friend_id, SET_REMARK_RESULT)
        return True

    def handle_recommend_friends(self, body):
        if body or not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "social") or {"friends": {}, "pending": {}, "blacklist": {}, "remarks": {}}
        friend_keys = set(state.get("friends", {}).keys())
        blocked = set(state.get("blacklist", {}).keys())
        rows = []
        for player in storage.list_players():
            if player["uid"] == self.uid or player["uid"] in friend_keys or player["uid"] in blocked:
                continue
            directory = storage.get_player_state_json(player["uid"], "social") or {}
            if not directory.get("directoryVisible"):
                continue
            rows.append(local_friend_pod(player))
            if len(rows) >= 20:
                break
        self.send(RECOMMEND_FRIENDS_RESULT, protocol_codec.encode_method(RECOMMEND_FRIENDS_RESULT, 0, rows))
        log.info("  friend recommendations uid=%s count=%d -> %d", self.uid, len(rows), RECOMMEND_FRIENDS_RESULT)
        return True

    # ── net_gameToCenter handlers ──

    def handle_register_simple_player(self, body):
        if not self.account:
            return False
        social = storage.get_player_state_json(self.uid, "social") or {}
        social["directoryVisible"] = True
        storage.update_player_state_json(self.uid, "social", social)
        self.send(REGISTER_SIMPLE_PLAYER_RESULT, protocol_codec.encode_method(REGISTER_SIMPLE_PLAYER_RESULT, 0, 0, {"pid": str(self.uid), "pname": "local", "lv": 1}))
        log.info("  register simple player uid=%s -> %d", self.uid, REGISTER_SIMPLE_PLAYER_RESULT)
        return True

    def handle_change_player_name(self, body):
        try:
            pid, new_name = protocol_codec.decode_method(CHANGE_PLAYER_NAME, body)
        except ValueError as exc:
            log.warning("  invalid change player name body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(CHANGE_PLAYER_NAME_RESULT, protocol_codec.encode_method(CHANGE_PLAYER_NAME_RESULT, 0, True, new_name, {"pid": str(pid), "pname": new_name, "lv": 1}))
        log.info("  change player name uid=%s newName=%s -> %d", self.uid, new_name, CHANGE_PLAYER_NAME_RESULT)
        return True

    def handle_load_center_player(self, body):
        try:
            pid, platform = protocol_codec.decode_method(LOAD_CENTER_PLAYER, body)
        except ValueError as exc:
            log.warning("  invalid load center player body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        pod = {"simplePlayerPOD": {"pid": str(pid), "pname": "local", "lv": 1, "uid": str(self.uid), "isOnline": True, "isRobot": False, "serverId": "local", "createTime": 0, "lastLoginTime": int(time.time()), "lastLogoutTime": 0, "avatarFrame": 0, "chatBackground": 0, "guid": 0, "headIcon": 0, "leaderCid": 20010001, "showGirlDressId": 0, "teamPower": 0, "title": 0, "totalCharge": 0.0, "vip": 0, "banSpeakEndTime": 0, "registerIp": "local"}, "friends": [], "guildId": 0, "guildName": "", "guildChatCaches": [], "chatRoom": {}, "eoDataPOD": {}}
        self.send(LOAD_CENTER_PLAYER_RESULT, protocol_codec.encode_method(LOAD_CENTER_PLAYER_RESULT, 0, str(pid), pod))
        log.info("  load center player uid=%s pid=%s -> %d", self.uid, pid, LOAD_CENTER_PLAYER_RESULT)
        return True

    def handle_center_offline(self, body):
        """Persist the center-server offline notice; the official method has no reply."""
        try:
            (reason,) = protocol_codec.decode_method(OFFLINE_NOTIFY, body)
        except (ValueError, KeyError) as exc:
            log.warning("  invalid center offline body %s: %s", body.hex(), exc)
            return False
        if not self.account or not isinstance(reason, str):
            return False
        storage.update_player_state_json(
            self.uid,
            "center_presence",
            {"online": False, "reason": reason[:128], "updatedAt": int(time.time())},
        )
        log.info("  center offline uid=%s reason=%s", self.uid, reason[:64])
        return True

    def handle_upload_simple_player(self, body):
        """Keep the latest local center directory POD for one-way synchronization."""
        try:
            (simple_player,) = protocol_codec.decode_method(UPLOAD_SIMPLE_PLAYER, body)
        except (ValueError, KeyError) as exc:
            log.warning("  invalid upload simple player body %s: %s", body.hex(), exc)
            return False
        if not self.account or not isinstance(simple_player, dict):
            return False
        storage.update_player_state_json(
            self.uid,
            "center_directory",
            {"simplePlayer": simple_player, "updatedAt": int(time.time())},
        )
        log.info("  upload simple player uid=%s", self.uid)
        return True

    def handle_upload_rank_score(self, body):
        """Persist locally uploaded ranking scores without contacting a center server."""
        try:
            rank_id, server_id, score, rank_data, custom_data = protocol_codec.decode_method(
                UPLOAD_RANK_SCORE, body
            )
        except (ValueError, KeyError) as exc:
            log.warning("  invalid upload rank score body %s: %s", body.hex(), exc)
            return False
        if (
            not self.account
            or not isinstance(rank_id, int)
            or rank_id <= 0
            or not isinstance(server_id, str)
            or not isinstance(score, int)
            or isinstance(score, bool)
            or score < 0
            or not isinstance(rank_data, str)
            or not isinstance(custom_data, str)
        ):
            return False
        state = storage.get_player_state_json(self.uid, "rank_scores") or {}
        state[str(rank_id)] = score
        storage.update_player_state_json(self.uid, "rank_scores", state)
        uploads = storage.get_player_state_json(self.uid, "rank_score_uploads") or {}
        uploads[str(rank_id)] = {
            "serverId": server_id[:64],
            "score": score,
            "rankData": rank_data[:512],
            "customData": custom_data[:512],
            "updatedAt": int(time.time()),
        }
        storage.update_player_state_json(self.uid, "rank_score_uploads", uploads)
        log.info("  upload rank score uid=%s rankId=%d score=%d", self.uid, rank_id, score)
        return True

    def handle_open_home_box(self, body):
        """Record a cross-game home-box event; its one-way protocol has no reward payload."""
        try:
            home_id, box_id, box_type = protocol_codec.decode_method(202, body)
        except (ValueError, KeyError) as exc:
            log.warning("  invalid open home box body %s: %s", body.hex(), exc)
            return False
        if (
            not self.account
            or not isinstance(home_id, str)
            or not home_id
            or not isinstance(box_id, str)
            or not box_id
            or not isinstance(box_type, int)
        ):
            return False
        state = storage.get_player_state_json(self.uid, "home_cross_server") or {}
        events = state.setdefault("openBoxes", [])
        events.append({"homeId": home_id, "boxId": box_id, "boxType": box_type, "time": int(time.time())})
        del events[:-20]
        storage.update_player_state_json(self.uid, "home_cross_server", state)
        log.info("  open home box uid=%s homeId=%s boxId=%s", self.uid, home_id, box_id)
        return True

    def handle_help_home(self, body):
        """Record a cross-game home-help event without inventing an item grant."""
        try:
            home_id, target_id, building_ids = protocol_codec.decode_method(203, body)
        except (ValueError, KeyError) as exc:
            log.warning("  invalid help home body %s: %s", body.hex(), exc)
            return False
        if (
            not self.account
            or not isinstance(home_id, str)
            or not home_id
            or not isinstance(target_id, str)
            or not target_id
            or not isinstance(building_ids, list)
            or len(building_ids) > 100
            or any(not isinstance(value, int) or isinstance(value, bool) for value in building_ids)
        ):
            return False
        state = storage.get_player_state_json(self.uid, "home_cross_server") or {}
        events = state.setdefault("helpHome", [])
        events.append(
            {
                "homeId": home_id,
                "targetId": target_id,
                "buildingIds": building_ids,
                "time": int(time.time()),
            }
        )
        del events[:-20]
        storage.update_player_state_json(self.uid, "home_cross_server", state)
        log.info("  help home uid=%s homeId=%s targetId=%s", self.uid, home_id, target_id)
        return True

    # ── net_maze handlers ──

    def _make_maze_pod(self, maze_cid, formation_id):
        maze = storage.create_maze_instance(self.uid, maze_cid, formation_id)
        if maze is None:
            return None
        return maze

    def _set_maze_context(self, maze_cid):
        self.active_story = {"kind": "maze", "maze_cid": maze_cid}

    def handle_enter_maze(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ENTER_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, formation_id)
        if maze_pod is None:
            return False
        self._set_maze_context(maze_cid)
        self.send(ENTER_MAZE_RESULT, protocol_codec.encode_method(ENTER_MAZE_RESULT, 0, maze_pod))
        log.info("  enter maze uid=%s mazeCid=%d formation=%d -> %d", self.uid, maze_cid, formation_id, ENTER_MAZE_RESULT)
        return True

    def handle_maze_settlement(self, body):
        if not self.account:
            return False
        maze_cid = (
            self.active_story.get("maze_cid", 0)
            if isinstance(self.active_story, dict)
            else 0
        )
        if not maze_cid:
            log.warning("  maze settlement rejected: no active maze uid=%s", self.uid)
            return False
        active_battle = storage.get_active_battle(self.uid)
        if active_battle and active_battle["map_id"] == maze_cid:
            log.warning(
                "  maze settlement rejected: battle still active uid=%s mazeCid=%d",
                self.uid, maze_cid,
            )
            return False
        battle = storage.get_latest_battle(self.uid, maze_cid)
        success = battle is None or battle["status"] == "won"
        rewards = storage.get_battle_reward_shows(battle)
        if success and not storage.complete_maze_instance(self.uid, maze_cid):
            return False
        money = sum(item["num"] for item in rewards if item["cid"] == 1)
        settlement_pod = {
            "success": success,
            "score": 1000 if success else 0,
            "starConditions": [success, success, success],
            "firstRewards": [],
            "rewards": rewards,
            "addSoulExps": {},
            "playerExp": 50 if success else 0,
            "money": money,
            "rewardsBoxes": [],
        }
        self.send(MAZE_SETTLEMENT_RESULT, protocol_codec.encode_method(MAZE_SETTLEMENT_RESULT, 0, settlement_pod))
        if success:
            self.active_story = None
        log.info(
            "  maze settlement uid=%s mazeCid=%d success=%s rewards=%d -> %d",
            self.uid, maze_cid, success, len(rewards), MAZE_SETTLEMENT_RESULT,
        )
        return True

    def handle_save_maze(self, body):
        try:
            save_data, is_quit, version = protocol_codec.decode_method(SAVE_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid save maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_cid = self.active_story.get("maze_cid", 0) if self.active_story and isinstance(self.active_story, dict) else 0
        if maze_cid:
            storage.save_maze_data(self.uid, maze_cid, save_data, version)
        self.send(SAVE_MAZE_RESULT, protocol_codec.encode_method(SAVE_MAZE_RESULT, 0, is_quit))
        log.info("  save maze uid=%s isQuit=%s -> %d", self.uid, is_quit, SAVE_MAZE_RESULT)
        return True

    def handle_restore_maze(self, body):
        try:
            (maze_cid,) = protocol_codec.decode_method(RESTORE_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid restore maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, 0)
        if maze_pod is None:
            return False
        self._set_maze_context(maze_cid)
        self.send(RESTORE_MAZE_RESULT, protocol_codec.encode_method(RESTORE_MAZE_RESULT, 0, maze_pod))
        log.info("  restore maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, RESTORE_MAZE_RESULT)
        return True

    def handle_revive_maze(self, body):
        if body or not self.account:
            return False
        self.send(REVIVE_MAZE_RESULT, protocol_codec.encode_method(REVIVE_MAZE_RESULT, 0))
        log.info("  maze revive uid=%s -> %d", self.uid, REVIVE_MAZE_RESULT)
        return True

    def handle_upload_maze_quest(self, body):
        try:
            quest_id, progress = protocol_codec.decode_method(UPLOAD_MAZE_QUEST, body)
        except ValueError as exc:
            log.warning("  invalid upload maze quest body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        seed_local_quest_state(self.uid)
        state = storage.update_quest_progress(self.uid, quest_id, progress)
        if state is None:
            log.warning(
                "  upload maze quest rejected uid=%s questId=%d progress=%d",
                self.uid,
                quest_id,
                progress,
            )
            return False
        self.send(
            UPLOAD_MAZE_QUEST_RESULT,
            protocol_codec.encode_method(UPLOAD_MAZE_QUEST_RESULT, 0),
        )
        log.info(
            "  upload maze quest uid=%s questId=%d progress=%d/%d completed=%s -> %d",
            self.uid,
            quest_id,
            state["fin_num"],
            state["tgt_num"],
            state["completed"],
            UPLOAD_MAZE_QUEST_RESULT,
        )
        return True

    def handle_upload_maze_alien(self, body):
        try:
            cid, element_cid = protocol_codec.decode_method(UPLOAD_MAZE_ALIEN, body)
        except ValueError as exc:
            log.warning("  invalid upload maze alien body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        # First send the upload result
        self.send(UPLOAD_MAZE_ALIEN_RESULT, protocol_codec.encode_method(UPLOAD_MAZE_ALIEN_RESULT, 0, cid, element_cid))
        log.info("  upload maze alien uid=%s cid=%d element=%d -> %d", self.uid, cid, element_cid, UPLOAD_MAZE_ALIEN_RESULT)
        # Then trigger a fight for alien events (maze nodes with enemies)
        self._send_notify_start_fight(battle_type=4, map_id=cid, monster_team_id=element_cid)
        return True

    def handle_open_hidden_maze(self, body):
        try:
            (maze_cid,) = protocol_codec.decode_method(OPEN_HIDDEN_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid open hidden maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(OPEN_HIDDEN_MAZE_RESULT, protocol_codec.encode_method(OPEN_HIDDEN_MAZE_RESULT, 0))
        log.info("  open hidden maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, OPEN_HIDDEN_MAZE_RESULT)
        return True

    def handle_buy_maze_count(self, body):
        try:
            (maze_cid,) = protocol_codec.decode_method(BUY_MAZE_COUNT, body)
        except ValueError as exc:
            log.warning("  invalid buy maze count body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(BUY_MAZE_COUNT_RESULT, protocol_codec.encode_method(BUY_MAZE_COUNT_RESULT, 0))
        log.info("  buy maze count uid=%s mazeCid=%d -> %d", self.uid, maze_cid, BUY_MAZE_COUNT_RESULT)
        return True

    def handle_mop_up(self, body):
        try:
            maze_cid, count, formation_id = protocol_codec.decode_method(MOP_UP, body)
        except ValueError as exc:
            log.warning("  invalid mop up body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not isinstance(count, int) or count < 1 or count > 99:
            return False
        config = BATTLE_CONFIG.get("mazeInstances", {}).get(str(maze_cid))
        if config:
            finished = storage.get_player_state_json(self.uid, "finishMazes") or []
            if maze_cid not in finished:
                log.warning("  mop up rejected: maze not completed uid=%s mazeCid=%d", self.uid, maze_cid)
                return False
            result = storage.settle_maze_mop_up(self.uid, maze_cid, count=count)
            if result is None:
                return False
            rewards = [{
                "money": result["money_per_run"],
                "playerExp": result["player_exp_per_run"],
                "rewards": run_rewards,
                "addSoulExps": {},
            } for run_rewards in result["run_rewards"]]
            for cid, quantity in result["changed_attrs"].items():
                self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}))
            if result["changed_items"]:
                self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]))
        else:
            # Older event-only clients send a map that has no extracted instance table.
            rewards = [{"money": 1000, "playerExp": 50, "rewards": [{"cid": 1, "num": 200, "tag": 0}], "addSoulExps": {}} for _ in range(count)]
        self.send(MOP_UP_RESULT, protocol_codec.encode_method(MOP_UP_RESULT, 0, rewards))
        log.info("  mop up uid=%s mazeCid=%d count=%d -> %d", self.uid, maze_cid, count, MOP_UP_RESULT)
        return True

    def handle_abandon_maze(self, body):
        try:
            (maze_cid,) = protocol_codec.decode_method(ABANDON_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid abandon maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        storage.abandon_active_battles(self.uid, maze_cid)
        storage.delete_maze_instance(self.uid, maze_cid)
        if (
            isinstance(self.active_story, dict)
            and self.active_story.get("maze_cid") == maze_cid
        ):
            self.active_story = None
        self.send(ABANDON_MAZE_RESULT, protocol_codec.encode_method(ABANDON_MAZE_RESULT, 0, maze_cid))
        log.info("  abandon maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ABANDON_MAZE_RESULT)
        return True

    def handle_enter_abyss_maze(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ENTER_ABYSS_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter abyss maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, formation_id)
        if maze_pod is None:
            return False
        self._set_maze_context(maze_cid)
        self.send(ENTER_ABYSS_MAZE_RESULT, protocol_codec.encode_method(ENTER_ABYSS_MAZE_RESULT, 0, maze_pod))
        log.info("  enter abyss maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ENTER_ABYSS_MAZE_RESULT)
        return True

    def handle_enter_hidden_maze(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ENTER_HIDDEN_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter hidden maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, formation_id)
        if maze_pod is None:
            return False
        self._set_maze_context(maze_cid)
        self.send(ENTER_HIDDEN_MAZE_RESULT, protocol_codec.encode_method(ENTER_HIDDEN_MAZE_RESULT, 0, maze_pod))
        log.info("  enter hidden maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ENTER_HIDDEN_MAZE_RESULT)
        return True

    def handle_upload_maze_monster_unlock(self, body):
        try:
            (monster_cid,) = protocol_codec.decode_method(UPLOAD_MAZE_MONSTER_UNLOCK, body)
        except ValueError as exc:
            log.warning("  invalid upload maze monster body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        self.send(UPLOAD_MAZE_MONSTER_UNLOCK_RESULT, protocol_codec.encode_method(UPLOAD_MAZE_MONSTER_UNLOCK_RESULT, 0))
        log.info("  upload maze monster unlock uid=%s monsterCid=%d -> %d", self.uid, monster_cid, UPLOAD_MAZE_MONSTER_UNLOCK_RESULT)
        return True

    def handle_enter_illusion_maze(self, body):
        try:
            maze_cid, soul_id = protocol_codec.decode_method(ENTER_ILLUSION_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter illusion maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, soul_id)
        if maze_pod is None:
            return False
        self.send(ENTER_ILLUSION_MAZE_RESULT, protocol_codec.encode_method(ENTER_ILLUSION_MAZE_RESULT, 0, maze_pod))
        log.info("  enter illusion maze uid=%s mazeCid=%d soulId=%d -> %d", self.uid, maze_cid, soul_id, ENTER_ILLUSION_MAZE_RESULT)
        return True

    def handle_enter_test_maze(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ENTER_TEST_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter test maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_maze_pod(maze_cid, formation_id)
        if maze_pod is None:
            return False
        self.send(ENTER_TEST_MAZE_RESULT, protocol_codec.encode_method(ENTER_TEST_MAZE_RESULT, 0, maze_pod))
        log.info("  enter test maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ENTER_TEST_MAZE_RESULT)
        return True

    def handle_illusion_mop_up(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ILLUSION_MOP_UP, body)
        except ValueError as exc:
            log.warning("  invalid illusion mop up body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        config = BATTLE_CONFIG.get("mazeInstances", {}).get(str(maze_cid))
        if config:
            finished = storage.get_player_state_json(self.uid, "finishMazes") or []
            if maze_cid not in finished:
                return False
            result = storage.settle_maze_mop_up(self.uid, maze_cid, count=1)
            if result is None:
                return False
            rewards = [{"money": result["money"], "playerExp": result["player_exp"], "rewards": result["rewards"], "addSoulExps": {}}]
            for cid, quantity in result["changed_attrs"].items():
                self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}))
            if result["changed_items"]:
                self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]))
        else:
            rewards = [{"money": 500, "playerExp": 30, "rewards": [{"cid": 1, "num": 100, "tag": 0}], "addSoulExps": {}}]
        self.send(ILLUSION_MOP_UP_RESULT, protocol_codec.encode_method(ILLUSION_MOP_UP_RESULT, 0, rewards))
        log.info("  illusion mop up uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ILLUSION_MOP_UP_RESULT)
        return True

    def handle_quick_challenge(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(QUICK_CHALLENGE, body)
        except ValueError as exc:
            log.warning("  invalid quick challenge body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        config = BATTLE_CONFIG.get("mazeInstances", {}).get(str(maze_cid))
        if config:
            finished = storage.get_player_state_json(self.uid, "finishMazes") or []
            if maze_cid not in finished:
                return False
            result = storage.settle_maze_mop_up(self.uid, maze_cid, count=1)
            if result is None:
                return False
            for cid, quantity in result["changed_attrs"].items():
                self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}))
            if result["changed_items"]:
                self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]))
        self.send(QUICK_CHALLENGE_RESULT, protocol_codec.encode_method(QUICK_CHALLENGE_RESULT, 0))
        log.info("  quick challenge uid=%s mazeCid=%d -> %d", self.uid, maze_cid, QUICK_CHALLENGE_RESULT)
        return True

    # ── net_soulPrefab handlers ──

    def handle_wear_equipment(self, body):
        try:
            equip_id, soul_cid, slot = protocol_codec.decode_method(2702, body)
        except ValueError as exc:
            log.warning("  invalid wear equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.wear_equipment(self.uid, equip_id, soul_cid, slot):
            return False
        self.send(2710, protocol_codec.encode_method(2710, 0))
        log.info("  equip wear uid=%s equipId=%d soulCid=%d slot=%d -> 2710", self.uid, equip_id, soul_cid, slot)
        return True

    def handle_dump_equipment(self, body):
        try:
            equip_ids, target_id = protocol_codec.decode_method(2703, body)
        except ValueError as exc:
            log.warning("  invalid dump equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        count = storage.dump_equipment(self.uid, equip_ids)
        self.send(2711, protocol_codec.encode_method(2711, 0))
        log.info("  equip dump uid=%s count=%d -> 2711", self.uid, count)
        return True

    def handle_upgrade_equipment(self, body):
        try:
            equip_id, material_map = protocol_codec.decode_method(2704, body)
        except ValueError as exc:
            log.warning("  invalid upgrade equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if storage.upgrade_equipment(self.uid, equip_id, material_map) is None:
            self.send(2712, protocol_codec.encode_method(2712, 1))
            return True
        self.send(2712, protocol_codec.encode_method(2712, 0))
        log.info("  equip upgrade uid=%s equipId=%d -> 2712", self.uid, equip_id)
        return True

    def handle_upstar_equipment(self, body):
        try:
            equip_id, fodder_ids = protocol_codec.decode_method(2705, body)
        except ValueError as exc:
            log.warning("  invalid upstar equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.upstar_equipment(self.uid, equip_id, fodder_ids):
            return False
        self.send(2713, protocol_codec.encode_method(2713, 0))
        log.info("  equip upstar uid=%s equipId=%d fodder=%d -> 2713", self.uid, equip_id, len(fodder_ids))
        return True

    def handle_decp_equipment(self, body):
        try:
            equip_ids, keep_high = protocol_codec.decode_method(2706, body)
        except ValueError as exc:
            log.warning("  invalid decp equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        results = storage.decp_equipment(self.uid, equip_ids, keep_high)
        if results is None:
            return False
        self.send(2714, protocol_codec.encode_method(2714, 0, results))
        log.info("  equip decp uid=%s count=%d rewards=%d -> 2714", self.uid, len(equip_ids), len(results))
        return True

    def handle_change_soul_prefab(self, body):
        try:
            prefab_id, soul_cid, skill_group_id, custom_skills, optional_skill = protocol_codec.decode_method(2707, body)
        except ValueError as exc:
            log.warning("  invalid change soul prefab body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if storage.update_soul_prefab(self.uid, prefab_id, soul_cid, skill_group_id, custom_skills, optional_skill) is None:
            return False
        self.send(2715, protocol_codec.encode_method(2715, 0))
        log.info("  change soul prefab uid=%s prefab=%d soul=%d -> 2715", self.uid, prefab_id, soul_cid)
        return True

    def handle_change_formation_pos(self, body):
        try:
            prefab_id, new_pos = protocol_codec.decode_method(2708, body)
        except ValueError as exc:
            log.warning("  invalid change formation pos body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.update_soul_prefab_position(self.uid, prefab_id, new_pos):
            return False
        self.send(2716, protocol_codec.encode_method(2716, 0))
        log.info("  change formation pos uid=%s prefab=%d pos=%d -> 2716", self.uid, prefab_id, new_pos)
        return True

    def handle_exchange_equipment(self, body):
        try:
            prefab_id, pos_a, pos_b = protocol_codec.decode_method(2709, body)
        except ValueError as exc:
            log.warning("  invalid exchange equipment body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.exchange_soul_prefab_equipment(self.uid, prefab_id, pos_a, pos_b):
            return False
        self.send(2717, protocol_codec.encode_method(2717, 0))
        log.info("  exchange equipment uid=%s prefab=%d posA=%d posB=%d -> 2717", self.uid, prefab_id, pos_a, pos_b)
        return True

    def handle_wear_equipment_prefab(self, body):
        try:
            soul_prefab_id, equipment_prefab_id = protocol_codec.decode_method(2719, body)
        except ValueError as exc:
            log.warning("  invalid wear equipment prefab body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.wear_equipment_prefab(self.uid, soul_prefab_id, equipment_prefab_id):
            return False
        self.send(2721, protocol_codec.encode_method(2721, 0))
        log.info("  wear equip prefab uid=%s soulPrefab=%d equipmentPrefab=%d -> 2721", self.uid, soul_prefab_id, equipment_prefab_id)
        return True

    def handle_save_equipment_prefab(self, body):
        try:
            equip_map, prefab_id = protocol_codec.decode_method(2720, body)
        except ValueError as exc:
            log.warning("  invalid save equipment prefab body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.save_equipment_prefab(self.uid, prefab_id, equip_map):
            return False
        self.send(2722, protocol_codec.encode_method(2722, 0))
        log.info("  save equip prefab uid=%s prefab=%d slots=%d -> 2722", self.uid, prefab_id, len(equip_map))
        return True

    def handle_cover_equipments(self, body):
        try:
            soul_prefab_id, equip_map = protocol_codec.decode_method(2724, body)
        except ValueError as exc:
            log.warning("  invalid cover equipments body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.cover_soul_prefab_equipment(self.uid, soul_prefab_id, equip_map):
            return False
        self.send(2725, protocol_codec.encode_method(2725, 0))
        log.info("  cover equipments uid=%s prefab=%d slots=%d -> 2725", self.uid, soul_prefab_id, len(equip_map))
        return True

    def handle_chang_equipment_prefab_name(self, body):
        try:
            prefab_id, new_name = protocol_codec.decode_method(2726, body)
        except ValueError as exc:
            log.warning("  invalid change equip prefab name body %s: %s", body.hex(), exc)
            return False
        if not self.account or not storage.rename_equipment_prefab(self.uid, prefab_id, new_name):
            return False
        self.send(2727, protocol_codec.encode_method(2727, 0))
        log.info("  change equip prefab name uid=%s prefab=%d -> 2727", self.uid, prefab_id)
        return True

    def handle_set_jewelry_speed(self, body):
        try:
            prefab_id, jewelry_cid, speed_value = protocol_codec.decode_method(2728, body)
        except ValueError as exc:
            log.warning("  invalid set jewelry speed body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if not storage.set_soul_prefab_jewelry_speed(self.uid, prefab_id, jewelry_cid, speed_value):
            return False
        self.send(2729, protocol_codec.encode_method(2729, 0))
        log.info("  set jewelry speed uid=%s prefab=%d jewelry=%d value=%d -> 2729", self.uid, prefab_id, jewelry_cid, speed_value)
        return True

    # ── net_fishing handlers ──

    def _legacy_fishing_state(self):
        state = storage.get_player_state_json(self.uid, "legacy_fishing") or {}
        state.setdefault("pending", None)
        state.setdefault("book", {})
        state.setdefault("autoPending", [])
        state.setdefault("autoNextTime", 0)
        return state

    def _save_legacy_fishing_state(self, state):
        storage.update_player_state_json(self.uid, "legacy_fishing", state)

    @staticmethod
    def _fish_show(fish_id, count=1):
        return {"cid": int(fish_id), "num": int(count), "tag": 0}

    def handle_fishing(self, body):
        try:
            rod_id, bait_id = protocol_codec.decode_method(FISHING, body)
        except ValueError as exc:
            log.warning("  invalid fishing body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if rod_id <= 0 or bait_id <= 0:
            return False
        fish_id = max(1, int(rod_id) * 1000 + int(bait_id))
        state = legacy_fishing_state_for(self.uid)
        state["pending"] = {"fishId": fish_id, "num": 1, "weight": 100}
        save_legacy_fishing_state(self.uid, state)
        self.send(FISHING_RESULT, protocol_codec.encode_method(FISHING_RESULT, 0, fish_id, 100, 0))
        log.info("  fishing uid=%s rod=%d bait=%d fish=%d -> %d", self.uid, rod_id, bait_id, fish_id, FISHING_RESULT)
        return True

    def handle_fishing_confirm(self, body):
        try:
            (caught,) = protocol_codec.decode_method(FISHING_CONFIRM, body)
        except ValueError as exc:
            log.warning("  invalid fishing confirm body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = legacy_fishing_state_for(self.uid)
        pending = state.get("pending")
        if not isinstance(pending, dict):
            return False
        state["pending"] = None
        reward = fish_show(pending.get("fishId", 1), pending.get("num", 1)) if caught else fish_show(1, 0)
        if caught:
            fish_id = int(pending.get("fishId", 1))
            entry = state["book"].setdefault(str(fish_id), {"fishId": fish_id, "num": 0, "weight": 0})
            entry["num"] = int(entry.get("num", 0)) + int(pending.get("num", 1))
            entry["weight"] = max(int(entry.get("weight", 0)), int(pending.get("weight", 0)))
            applied = storage.grant_reward_pairs(self.uid, [(fish_id, int(pending.get("num", 1)))])
            if applied is None:
                return False
        save_legacy_fishing_state(self.uid, state)
        self.send(FISHING_CONFIRM_RESULT, protocol_codec.encode_method(FISHING_CONFIRM_RESULT, 0, reward))
        log.info("  fishing confirm uid=%s caught=%s -> %d", self.uid, caught, FISHING_CONFIRM_RESULT)
        return True

    def handle_illegal_fishing(self, body):
        try:
            fish_type, count = protocol_codec.decode_method(ILLEGAL_FISHING, body)
        except ValueError as exc:
            log.warning("  invalid illegal fishing body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if fish_type <= 0 or count <= 0:
            return False
        applied = storage.grant_reward_pairs(self.uid, [(fish_type, count)])
        if applied is None:
            return False
        self.send(ILLEGAL_FISHING_RESULT, protocol_codec.encode_method(ILLEGAL_FISHING_RESULT, 0, applied["rewards"]))
        log.info("  illegal fishing uid=%s type=%d count=%d -> %d", self.uid, fish_type, count, ILLEGAL_FISHING_RESULT)
        return True

    def handle_exchange_fish(self, body):
        try:
            fish_id, count = protocol_codec.decode_method(EXCHANGE_FISH, body)
        except ValueError as exc:
            log.warning("  invalid exchange fish body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if fish_id <= 0 or count <= 0:
            return False
        seed_local_player_attrs(self.uid)
        result = storage.trade_reward_pairs(self.uid, [(fish_id, count)], [(1, count * 10)])
        if result is None:
            return False
        self._send_reward_changes(result)
        self.send(EXCHANGE_FISH_RESULT, protocol_codec.encode_method(EXCHANGE_FISH_RESULT, 0, result["rewards"]))
        log.info("  exchange fish committed uid=%s fishId=%d count=%d -> %d", self.uid, fish_id, count, EXCHANGE_FISH_RESULT)
        return True

    def handle_exchange_fish_by_type(self, body):
        try:
            (fish_type,) = protocol_codec.decode_method(EXCHANGE_FISH_BY_TYPE, body)
        except ValueError as exc:
            log.warning("  invalid exchange fish by type body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if fish_type <= 0:
            return False
        state = legacy_fishing_state_for(self.uid)
        entries = [entry for entry in state["book"].values() if int(entry.get("fishId", 0)) // 1000 == fish_type]
        if not entries:
            return False
        costs = [(int(entry["fishId"]), int(entry.get("num", 0))) for entry in entries if int(entry.get("num", 0)) > 0]
        total = sum(quantity for _, quantity in costs)
        seed_local_player_attrs(self.uid)
        result = storage.trade_reward_pairs(self.uid, costs, [(1, total * 10)])
        if result is None:
            return False
        for entry in entries:
            entry["num"] = 0
        save_legacy_fishing_state(self.uid, state)
        self._send_reward_changes(result)
        self.send(EXCHANGE_FISH_BY_TYPE_RESULT, protocol_codec.encode_method(EXCHANGE_FISH_BY_TYPE_RESULT, 0, result["rewards"]))
        log.info("  exchange fish by type committed uid=%s type=%d count=%d -> %d", self.uid, fish_type, total, EXCHANGE_FISH_BY_TYPE_RESULT)
        return True

    def handle_auto_fishing(self, body):
        try:
            rod_id, count = protocol_codec.decode_method(AUTO_FISHING, body)
        except ValueError as exc:
            log.warning("  invalid auto fishing body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        if rod_id <= 0 or count <= 0:
            return False
        now = int(time.time())
        state = legacy_fishing_state_for(self.uid)
        fish_id = max(1, int(rod_id) * 1000 + 1)
        state["autoPending"] = [{"fishId": fish_id, "num": int(count), "weight": 100}]
        state["autoNextTime"] = now + 3600
        save_legacy_fishing_state(self.uid, state)
        self.send(AUTO_FISHING_RESULT, protocol_codec.encode_method(AUTO_FISHING_RESULT, 0, state["autoNextTime"]))
        log.info("  auto fishing uid=%s rod=%d count=%d next=%d -> %d", self.uid, rod_id, count, state["autoNextTime"], AUTO_FISHING_RESULT)
        return True

    def handle_fishing_draw_rewards(self, body):
        if body or not self.account:
            return False
        state = legacy_fishing_state_for(self.uid)
        pending = list(state.get("autoPending") or [])
        if not pending:
            self.send(FISHING_DRAW_REWARDS_RESULT, protocol_codec.encode_method(FISHING_DRAW_REWARDS_RESULT, 0, []))
            return True
        rewards = []
        for pod in pending:
            fish_id = int(pod.get("fishId", 0))
            count = int(pod.get("num", 0))
            if fish_id <= 0 or count <= 0:
                continue
            rewards.append(fish_show(fish_id, count))
            entry = state["book"].setdefault(str(fish_id), {"fishId": fish_id, "num": 0, "weight": 0})
            entry["num"] = int(entry.get("num", 0)) + count
            entry["weight"] = max(int(entry.get("weight", 0)), int(pod.get("weight", 0)))
            applied = storage.grant_reward_pairs(self.uid, [(fish_id, count)])
            if applied is None:
                return False
        state["autoPending"] = []
        save_legacy_fishing_state(self.uid, state)
        self.send(FISHING_DRAW_REWARDS_RESULT, protocol_codec.encode_method(FISHING_DRAW_REWARDS_RESULT, 0, rewards))
        log.info("  fishing draw rewards uid=%s count=%d -> %d", self.uid, len(rewards), FISHING_DRAW_REWARDS_RESULT)
        return True

    # ── net_fishingActivity handlers ──

    def handle_activity_fishing(self, body):
        if body or not self.account:
            self.send(ACTIVITY_FISHING_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_RESULT, 1, {"fishId": 0, "num": 0, "weight": 0}))
            return False
        fish_config = BATTLE_CONFIG.get("fishingActivity", {}).get("fish", {})
        if not fish_config:
            self.send(ACTIVITY_FISHING_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_RESULT, 1, {"fishId": 0, "num": 0, "weight": 0}))
            return True
        fish_id = int(random.choice(sorted(fish_config, key=int)))
        fish = fish_config[str(fish_id)]
        weight_range = fish.get("weightRange", [1, 1])
        weight = random.randint(int(weight_range[0]), int(weight_range[1]))
        pod = {"fishId": fish_id, "num": 1, "weight": weight}
        if not storage.prepare_fishing_catch(self.uid, pod):
            self.send(ACTIVITY_FISHING_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_RESULT, 1, {"fishId": 0, "num": 0, "weight": 0}))
            return True
        self.send(ACTIVITY_FISHING_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_RESULT, 0, pod))
        log.info("  activity fishing uid=%s -> %d", self.uid, ACTIVITY_FISHING_RESULT)
        return True

    def handle_activity_fishing_confirm(self, body):
        try:
            (caught,) = protocol_codec.decode_method(ACTIVITY_FISHING_CONFIRM, body)
        except ValueError as exc:
            log.warning("  invalid activity fishing confirm body %s: %s", body.hex(), exc)
            self.send(ACTIVITY_FISHING_CONFIRM_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_CONFIRM_RESULT, 1))
            return False
        if not self.account:
            self.send(ACTIVITY_FISHING_CONFIRM_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_CONFIRM_RESULT, 1))
            return False
        state = storage.confirm_fishing_catch(self.uid, caught)
        if state is None:
            self.send(ACTIVITY_FISHING_CONFIRM_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_CONFIRM_RESULT, 1))
            return True
        self.send(ACTIVITY_FISHING_CONFIRM_RESULT, protocol_codec.encode_method(ACTIVITY_FISHING_CONFIRM_RESULT, 0))
        log.info("  activity fishing confirm uid=%s caught=%s -> %d", self.uid, caught, ACTIVITY_FISHING_CONFIRM_RESULT)
        return True

    def handle_activity_get_auto_rewards(self, body):
        if body or not self.account:
            self.send(ACTIVITY_GET_AUTO_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_AUTO_REWARDS_RESULT, 1, [], 0))
            return False
        activity = BATTLE_CONFIG.get("fishingActivity", {})
        control = activity.get("control", {})
        interval = max(1, int(control.get("timeInterval", 28800)))
        fish_config = activity.get("fish", {})
        if not fish_config:
            self.send(ACTIVITY_GET_AUTO_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_AUTO_REWARDS_RESULT, 1, [], 0))
            return True
        fish_count_range = control.get("fishNum", [1, 1])
        fish_count = random.randint(int(fish_count_range[0]), int(fish_count_range[-1])) if fish_count_range else 1
        fish_pods = []
        for _ in range(fish_count):
            fish_id = int(random.choice(sorted(fish_config, key=int)))
            weight_range = fish_config[str(fish_id)].get("weightRange", [1, 1])
            fish_pods.append({"fishId": fish_id, "num": 1, "weight": random.randint(int(weight_range[0]), int(weight_range[1]))})
        now = int(time.time())
        result = storage.claim_fishing_auto(self.uid, now, interval, fish_count, fish_pods)
        if result is None:
            self.send(ACTIVITY_GET_AUTO_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_AUTO_REWARDS_RESULT, 1, [], 0))
            return True
        self.send(ACTIVITY_GET_AUTO_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_AUTO_REWARDS_RESULT, 0, result["rewards"], result["next_time"]))
        log.info("  activity auto rewards uid=%s fish=%d claimed=%s -> %d", self.uid, len(result["rewards"]), result["claimed"], ACTIVITY_GET_AUTO_REWARDS_RESULT)
        return True

    def handle_activity_up_role(self, body):
        if body or not self.account:
            self.send(ACTIVITY_UP_ROLE_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ROLE_RESULT, 1, 0))
            return False
        result = storage.upgrade_fishing_activity(self.uid, "role")
        if result is None:
            self.send(ACTIVITY_UP_ROLE_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ROLE_RESULT, 1, 0))
            return True
        self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, result["changed_attrs"]))
        self.send(ACTIVITY_UP_ROLE_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ROLE_RESULT, 0, result["level"]))
        log.info("  activity up role uid=%s level=%d cost=%d -> %d", self.uid, result["level"], result["cost_num"], ACTIVITY_UP_ROLE_RESULT)
        return True

    def handle_activity_up_skill(self, body):
        try:
            (skill_id,) = protocol_codec.decode_method(ACTIVITY_UP_SKILL, body)
        except ValueError as exc:
            log.warning("  invalid activity up skill body %s: %s", body.hex(), exc)
            self.send(ACTIVITY_UP_SKILL_RESULT, protocol_codec.encode_method(ACTIVITY_UP_SKILL_RESULT, 1, 0, 0))
            return False
        if not self.account:
            self.send(ACTIVITY_UP_SKILL_RESULT, protocol_codec.encode_method(ACTIVITY_UP_SKILL_RESULT, 1, 0, 0))
            return False
        result = storage.upgrade_fishing_activity(self.uid, "skill", skill_id)
        if result is None:
            self.send(ACTIVITY_UP_SKILL_RESULT, protocol_codec.encode_method(ACTIVITY_UP_SKILL_RESULT, 1, skill_id, 0))
            return True
        self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, result["changed_attrs"]))
        self.send(ACTIVITY_UP_SKILL_RESULT, protocol_codec.encode_method(ACTIVITY_UP_SKILL_RESULT, 0, skill_id, result["level"]))
        log.info("  activity up skill uid=%s skillId=%d level=%d cost=%d -> %d", self.uid, skill_id, result["level"], result["cost_num"], ACTIVITY_UP_SKILL_RESULT)
        return True

    def handle_activity_up_action(self, body):
        try:
            (action_id,) = protocol_codec.decode_method(ACTIVITY_UP_ACTION, body)
        except ValueError as exc:
            log.warning("  invalid activity up action body %s: %s", body.hex(), exc)
            self.send(ACTIVITY_UP_ACTION_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ACTION_RESULT, 1, 0, 0))
            return False
        if not self.account:
            self.send(ACTIVITY_UP_ACTION_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ACTION_RESULT, 1, 0, 0))
            return False
        result = storage.upgrade_fishing_activity(self.uid, "action", action_id)
        if result is None:
            self.send(ACTIVITY_UP_ACTION_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ACTION_RESULT, 1, action_id, 0))
            return True
        self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, result["changed_attrs"]))
        self.send(ACTIVITY_UP_ACTION_RESULT, protocol_codec.encode_method(ACTIVITY_UP_ACTION_RESULT, 0, action_id, result["level"]))
        log.info("  activity up action uid=%s actionId=%d level=%d cost=%d -> %d", self.uid, action_id, result["level"], result["cost_num"], ACTIVITY_UP_ACTION_RESULT)
        return True

    def handle_activity_get_story_rewards(self, body):
        try:
            (story_id,) = protocol_codec.decode_method(ACTIVITY_GET_STORY_REWARDS, body)
        except ValueError as exc:
            log.warning("  invalid activity get story rewards body %s: %s", body.hex(), exc)
            self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
            return False
        if not self.account:
            self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
            return False
        event = BATTLE_CONFIG.get("fishingActivity", {}).get("events", {}).get(str(story_id))
        if not event:
            self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
            return True
        state = storage.get_fishing_activity_state(self.uid)
        stories = state.get("getStoryList", [])
        if int(event.get("preStory", 0) or 0) > 0 and int(event["preStory"]) not in stories:
            self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
            return True
        unlock_parameter = int(event.get("unlockParameter", 0) or 0)
        if int(event.get("type", 0) or 0) == 1:
            if int(state.get("roleLevel", 1) or 1) < unlock_parameter:
                self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
                return True
        elif int(event.get("type", 0) or 0) == 2:
            fish = state.get("book", {}).get(str(unlock_parameter), {})
            if not isinstance(fish, dict) or int(fish.get("num", 0) or 0) <= 0:
                self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
                return True
        reward_pairs = []
        for pair in event.get("dialogReward", []):
            if isinstance(pair, list) and len(pair) >= 2:
                reward_pairs.append((int(pair[0]), int(pair[1])))
        result = storage.claim_fishing_story(
            self.uid, story_id, reward_pairs, 0
        )
        if result is None:
            self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 1, []))
            return True
        for cid, quantity in result["changed_attrs"].items():
            self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}))
        if result["changed_items"]:
            self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, result["changed_items"]))
        self.send(ACTIVITY_GET_STORY_REWARDS_RESULT, protocol_codec.encode_method(ACTIVITY_GET_STORY_REWARDS_RESULT, 0, result["rewards"]))
        log.info("  activity story rewards uid=%s storyId=%d claimed=%s -> %d", self.uid, story_id, result["claimed"], ACTIVITY_GET_STORY_REWARDS_RESULT)
        return True

    # ── net_lunaBattleLine handlers ──

    def handle_garrison(self, body):
        try:
            slot_id, formation_id = protocol_codec.decode_method(GARRISON, body)
        except ValueError as exc:
            log.warning("  invalid garrison body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "luna_battle_line") or {"garrisons": {}, "prefabs": {}}
        prefab = assist_prefab_for(formation_id)
        state.setdefault("garrisons", {})[str(slot_id)] = int(formation_id)
        state.setdefault("prefabs", {})[str(formation_id)] = prefab
        storage.update_player_state_json(self.uid, "luna_battle_line", state)
        self.send(GARRISON_RESULT, protocol_codec.encode_method(GARRISON_RESULT, 0, 0, formation_id, prefab))
        log.info("  garrison uid=%s slot=%d formation=%d -> %d", self.uid, slot_id, formation_id, GARRISON_RESULT)
        return True

    def handle_get_assists(self, body):
        try:
            (slot_id,) = protocol_codec.decode_method(GET_ASSISTS, body)
        except ValueError as exc:
            log.warning("  invalid get assists body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "luna_battle_line") or {"garrisons": {}, "prefabs": {}}
        formation_id = int(state.setdefault("garrisons", {}).get(str(slot_id), 0))
        if formation_id <= 0:
            self.send(GET_ASSISTS_RESULT, protocol_codec.encode_method(GET_ASSISTS_RESULT, 0, 0, []))
            return True
        prefab = state.setdefault("prefabs", {}).get(str(formation_id)) or assist_prefab_for(formation_id)
        player = storage.get_player(self.uid) or {}
        assist = {"player": {"pid": self.uid, "pName": player.get("role_name", "local"), "pLv": int(player.get("level", 1)), "guildId": 0}, "soulPrefab": prefab}
        self.send(GET_ASSISTS_RESULT, protocol_codec.encode_method(GET_ASSISTS_RESULT, 0, 0, [assist]))
        log.info("  get assists uid=%s slot=%d formation=%d -> %d", self.uid, slot_id, formation_id, GET_ASSISTS_RESULT)
        return True

    def handle_refresh_assist(self, body):
        try:
            (slot_id,) = protocol_codec.decode_method(REFRESH_ASSIST, body)
        except ValueError as exc:
            log.warning("  invalid refresh assist body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "luna_battle_line") or {"garrisons": {}, "prefabs": {}}
        formation_id = int(state.setdefault("garrisons", {}).get(str(slot_id), 0))
        assists = []
        if formation_id > 0:
            player = storage.get_player(self.uid) or {}
            assists.append({"player": {"pid": self.uid, "pName": player.get("role_name", "local"), "pLv": int(player.get("level", 1)), "guildId": 0}, "soulPrefab": assist_prefab_for(formation_id)})
        self.send(REFRESH_ASSIST_RESULT, protocol_codec.encode_method(REFRESH_ASSIST_RESULT, 0, 0, assists))
        log.info("  refresh assist uid=%s slot=%d -> %d", self.uid, slot_id, REFRESH_ASSIST_RESULT)
        return True

    def handle_get_strengthen_soul_prefab(self, body):
        try:
            (slot_id,) = protocol_codec.decode_method(GET_STRENGTHEN_SOUL_PREFAB, body)
        except ValueError as exc:
            log.warning("  invalid get strengthen soul prefab body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        state = storage.get_player_state_json(self.uid, "luna_battle_line") or {"garrisons": {}, "prefabs": {}}
        formation_id = int(state.setdefault("garrisons", {}).get(str(slot_id), 0))
        prefabs = []
        if formation_id > 0:
            prefabs.append(state.setdefault("prefabs", {}).get(str(formation_id)) or assist_prefab_for(formation_id))
        self.send(GET_STRENGTHEN_SOUL_PREFAB_RESULT, protocol_codec.encode_method(GET_STRENGTHEN_SOUL_PREFAB_RESULT, 0, 0, prefabs))
        log.info("  get strengthen soul prefab uid=%s slot=%d count=%d -> %d", self.uid, slot_id, len(prefabs), GET_STRENGTHEN_SOUL_PREFAB_RESULT)
        return True

    @staticmethod
    def _make_assist_prefab(formation_id):
        return {
            "id": int(formation_id), "soulCid": 20010001, "lv": 1, "exp": 0,
            "favorLv": 1, "qualityId": 1, "position": 1, "power": 0,
            "activeTalentCids": [], "activeTalentGroupCids": [], "allSkillStrengths": [],
            "allSkills": [], "customSkills": [], "pAblityIds": [], "soulMemoryPieces": [],
            "specialSpirit": [], "unlockSkillGroups": [], "equipments": {},
        }

    def _make_luna_maze_pod(self, maze_cid):
        return {
            "id": maze_cid,
            "mazeCid": maze_cid,
            "randomSeed": 12345,
            "isLocal": False,
            "carryItems": [],
            "saveData": "",
            "saveVersion": 0,
            "mazePlayer": {"first": True, "baseInfo": {"pid": "", "pName": "local", "pLv": 1, "exp": 0}, "dolls": [], "completePathNodes": [], "finishMazes": [], "mainQuests": {}, "openPathNodes": [], "items": {}, "events": {}, "alienEvents": [], "itemDropGetCnts": {}, "finishQuests": [], "first": True, "fishSpecimens": [], "mazeRuneList": [], "currMazeBuffCids": [], "maxRuneLevel": 0, "activityPOD": {}},
        }

    def handle_enter_fort_maze(self, body):
        try:
            maze_cid, formation_id = protocol_codec.decode_method(ENTER_FORT_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter fort maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_luna_maze_pod(maze_cid)
        self.send(ENTER_FORT_MAZE_RESULT, protocol_codec.encode_method(ENTER_FORT_MAZE_RESULT, 0, 0, maze_pod))
        log.info("  enter fort maze uid=%s mazeCid=%d formation=%d -> %d", self.uid, maze_cid, formation_id, ENTER_FORT_MAZE_RESULT)
        return True

    def handle_enter_seal_maze(self, body):
        try:
            maze_cid, formation_id, extra = protocol_codec.decode_method(ENTER_SEAL_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter seal maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_luna_maze_pod(maze_cid)
        self.send(ENTER_SEAL_MAZE_RESULT, protocol_codec.encode_method(ENTER_SEAL_MAZE_RESULT, 0, 0, maze_pod))
        log.info("  enter seal maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ENTER_SEAL_MAZE_RESULT)
        return True

    def handle_enter_strengthen_maze(self, body):
        try:
            maze_cid, prefab_list = protocol_codec.decode_method(ENTER_STRENGTHEN_MAZE, body)
        except ValueError as exc:
            log.warning("  invalid enter strengthen maze body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            return False
        maze_pod = self._make_luna_maze_pod(maze_cid)
        self.send(ENTER_STRENGTHEN_MAZE_RESULT, protocol_codec.encode_method(ENTER_STRENGTHEN_MAZE_RESULT, 0, 0, maze_pod))
        log.info("  enter strengthen maze uid=%s mazeCid=%d -> %d", self.uid, maze_cid, ENTER_STRENGTHEN_MAZE_RESULT)
        return True

    def _handle_local_battle_entry(self, request_id, result_id, body):
        """Start a persisted FightPOD for recovered non-maze battle entrances."""
        if request_id == 3003:
            try:
                maze_cid, count = protocol_codec.decode_method(request_id, body or b"")
            except (KeyError, TypeError, ValueError):
                return False
            result = storage.settle_maze_mop_up(self.uid, maze_cid, count, seed=maze_cid)
            if result is None:
                log.warning("  daily sweep rejected uid=%s mazeCid=%s count=%s", self.uid, maze_cid, count)
                return False
            Session._send_reward_changes(self, result)
            self.send(
                result_id,
                protocol_codec.encode_method(
                    result_id, 0, result.get("rewards", []), int(result.get("player_exp", 0))
                ),
            )
            log.info("  daily sweep uid=%s mazeCid=%d count=%d -> %d", self.uid, maze_cid, count, result_id)
            return True
        battle_type = LOCAL_BATTLE_ENTRY_TYPES.get(request_id)
        if battle_type is None or not self.account:
            return False
        try:
            values = protocol_codec.decode_method(request_id, body or b"")
            result_method = protocol_codec.METHODS[result_id]
            result_values = [Session._default_protocol_value(type_name) for type_name in result_method["types"]]
        except (KeyError, TypeError, ValueError):
            log.warning("  local battle entry rejected: invalid body uid=%s msgId=%s", self.uid, request_id)
            return False

        if request_id == 3202:
            # net_worldBoss.attack(dupCid, formationId, clearCD, bossCid)
            # has no monster-team ID in its request.  The previous generic
            # parser treated the message ID/dup fields as battle data and
            # created a FightPOD with an empty defender.
            try:
                dup_cid, _formation_id, _clear_cd, boss_cid = values
                world_boss = WORLD_BOSS_CONFIG.get(str(int(boss_cid)))
                monster_team_id = int(world_boss["MonsterTeam"]) if world_boss else 0
                map_id = int(dup_cid)
            except (TypeError, ValueError, KeyError, IndexError):
                log.warning("  world boss entry rejected: malformed values uid=%s values=%r", self.uid, values)
                return False
            if not monster_team_id:
                log.warning(
                    "  world boss entry rejected: unknown bossCid uid=%s bossCid=%s",
                    self.uid,
                    boss_cid,
                )
                return False
        else:
            numbers = []
            for value in values:
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and value > 0:
                    numbers.append(value)
            map_id = numbers[0] if numbers else 0
            configured_teams = BATTLE_CONFIG.get("teams", {})
            monster_team_id = next(
                (value for value in numbers[1:] if str(value) in configured_teams),
                0,
            )
        if not self._send_notify_start_fight(
            battle_type=battle_type, map_id=map_id, monster_team_id=monster_team_id
        ):
            log.warning(
                "  local battle entry could not create instance uid=%s msgId=%s",
                self.uid,
                request_id,
            )
            return False
        # The entry result is part of the same logical transaction as the
        # persisted battle instance.  Do not acknowledge an entry that could
        # not create the server-side encounter.
        self.send(result_id, protocol_codec.encode_method(result_id, *result_values))
        log.info(
            "  local battle entry uid=%s msgId=%s type=%d map=%d team=%d -> %d/2903",
            self.uid,
            request_id,
            battle_type,
            map_id,
            monster_team_id,
            result_id,
        )
        return True

    def handle_challenge_dup(self, body):
        """Start the persisted local encounter for net_challenge.challengeDup."""
        return self._handle_local_battle_entry(3002, 3004, body)

    def handle_fight_over(self, body):
        """Handle net_fight.fightOver from client after a battle completes."""
        if not self.account:
            return False
        try:
            battle_type, fight_result, dmg_records, attacker, defender, json_order, user_data, rounds, heal_records, hurt_records = (
                protocol_codec.decode_method(2902, body)
            )
        except (ValueError, KeyError) as exc:
            log.warning("  invalid fightOver body %s: %s", body.hex(), exc)
            return False

        if fight_result not in (0, 1, 2, 3):
            log.warning(
                "  fightOver rejected: invalid result uid=%s result=%d",
                self.uid, fight_result,
            )
            return False
        if not Session._valid_fight_troop_report(attacker) or not Session._valid_fight_troop_report(defender):
            log.warning("  fightOver rejected: invalid troop report uid=%s", self.uid)
            return False

        # A result is valid only for a battle previously created by the server.
        battle_id = None
        if self.active_story and isinstance(self.active_story, dict):
            battle_id = self.active_story.get("battle_id")
        if not battle_id:
            active = storage.get_active_battle(self.uid, battle_type)
            battle_id = active["id"] if active else None
        if not battle_id:
            log.warning("  fightOver rejected: no active battle uid=%s", self.uid)
            return False

        battle = storage.get_battle_instance(self.uid, battle_id)
        if battle is None:
            log.warning("  fightOver rejected: unknown battle uid=%s battleId=%s", self.uid, battle_id)
            return False
        if battle["battle_type"] != battle_type:
            log.warning(
                "  fightOver rejected: type mismatch uid=%s battleId=%s expected=%d got=%d",
                self.uid, battle_id, battle["battle_type"], battle_type,
            )
            return False
        if battle["settled"]:
            log.info("  duplicate fightOver uid=%s battleId=%s", self.uid, battle_id)
            # If the process stopped after the SQLite settlement but before
            # module progress was written, a retry must finish that second
            # half without replaying currency/item notifications.
            module_rules.handle_battle_completion(
                self,
                self.uid,
                battle_id,
                battle.get("result") == 1,
                {"rewards": storage.get_battle_reward_shows(battle)},
            )
            return True

        if fight_result == 1:
            simulation = storage.evaluate_battle_instance(self.uid, battle_id)
            if simulation is not None and simulation["result"] != 1:
                log.warning(
                    "  fightOver rejected: server simulation lost uid=%s battleId=%s "
                    "attackerPower=%d defenderPower=%d",
                    self.uid,
                    battle_id,
                    simulation["attackerPower"],
                    simulation["defenderPower"],
                )
                return False
            if rounds <= 0:
                log.warning("  fightOver rejected: winning battle has no rounds uid=%s", self.uid)
                return False

        report = {
            "damage": dmg_records,
            "heal": heal_records,
            "hurt": hurt_records,
            "attacker": attacker,
            "defender": defender,
            "jsonOrder": json_order,
            "userData": user_data,
        }
        simulation = storage.evaluate_battle_instance(self.uid, battle_id)
        if simulation is not None:
            report["serverSimulation"] = simulation
        result = storage.settle_battle(
            self.uid, battle_id, fight_result, rounds=rounds, report=report
        )
        if result is None:
            current = storage.get_battle_instance(self.uid, battle_id)
            return bool(current and current["settled"])

        # Module-specific progress is committed only after the common battle
        # transaction has accepted the server simulation and client report.
        module_rules.handle_battle_completion(
            self, self.uid, battle_id, fight_result == 1, result
        )

        # Send reward notifications (similar to mail pickup / quest commit pattern)
        for cid, quantity in result["changed_attrs"].items():
            self.send(
                NOTIFY_NUM_ATTR,
                protocol_codec.encode_method(NOTIFY_NUM_ATTR, {cid: quantity}),
            )
        if result["changed_items"]:
            self.send(
                NOTIFY_ITEM_CHANGE,
                protocol_codec.encode_method(
                    NOTIFY_ITEM_CHANGE, result["changed_items"]
                ),
            )

        if battle["map_id"]:
            self.active_story = {"kind": "maze", "maze_cid": battle["map_id"]}
        else:
            self.active_story = None

        log.info(
            "  fight settled uid=%s battleType=%d result=%d rounds=%d rewards=%d -> %d/%d",
            self.uid, battle_type, fight_result, rounds,
            len(result["rewards"]),
            NOTIFY_NUM_ATTR if result["changed_attrs"] else 0,
            NOTIFY_ITEM_CHANGE if result["changed_items"] else 0,
        )
        return True

    def _send_notify_start_fight(self, battle_type=4, map_id=0, monster_team_id=0, reward_pairs=None):
        """Send 2903 notifyStartFight to the client to start a battle."""
        battle_id = storage.create_battle_instance(
            self.uid, battle_type, map_id, monster_team_id, reuse_active=True,
            reward_pairs=reward_pairs,
        )
        if battle_id is None:
            return False
        battle = storage.get_battle_instance(self.uid, battle_id)
        if battle is None:
            return False
        fight_pod = self._make_fight_pod(
            battle_id, battle_type, map_id, monster_team_id, battle["random_seed"]
        )
        defender = fight_pod.get("Defender", {}).get("ArrFightUnitPOD", [])
        if battle_type == 3 and not defender:
            storage.abandon_active_battles(self.uid, map_id)
            log.warning(
                "  notifyStartFight rejected: empty defender uid=%s battleType=%d mapId=%d team=%d",
                self.uid,
                battle_type,
                map_id,
                monster_team_id,
            )
            return False
        if not storage.set_battle_server_snapshot(self.uid, battle_id, fight_pod):
            log.warning("  notifyStartFight rejected: unable to persist server snapshot uid=%s battleId=%s", self.uid, battle_id)
            return False
        self._set_maze_context(map_id or 0)
        if self.active_story and isinstance(self.active_story, dict):
            self.active_story["battle_id"] = battle_id
        # notifyStartFight(isLocalFight=true, fightPOD, userData="")
        body = protocol_codec.encode_method(NOTIFY_START_FIGHT, True, fight_pod, "")
        self.send(NOTIFY_START_FIGHT, body)
        log.info(
            "  notifyStartFight uid=%s battleType=%d mapId=%d battleId=%s -> %d",
            self.uid, battle_type, map_id, battle_id, NOTIFY_START_FIGHT,
        )
        return True

    @staticmethod
    def _valid_fight_troop_report(troop):
        """Validate optional client unit state without requiring a full battle simulator."""
        if not isinstance(troop, dict):
            return False
        units = troop.get("ArrFightUnitPOD", [])
        if not isinstance(units, list):
            return False
        for unit in units:
            if not isinstance(unit, dict):
                return False
            troop_type = unit.get("TroopType")
            if troop_type is not None and troop_type not in (1, 2):
                return False
            battle_pos = unit.get("BattlePos")
            if battle_pos is not None and (not isinstance(battle_pos, int) or not 1 <= battle_pos <= 10):
                return False
            power = unit.get("Power")
            if power is not None and (not isinstance(power, int) or power < 0):
                return False
            for field in ("Attributes", "Skills", "SkillStrengthens", "InitBuff", "SPStatus"):
                value = unit.get(field)
                if value is not None and not isinstance(value, list):
                    return False
        return True

    @staticmethod
    def _battle_int_list(values):
        if not isinstance(values, (list, tuple)):
            return []
        result = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _battle_power(attributes, explicit_power=0):
        try:
            power = int(explicit_power or 0)
        except (TypeError, ValueError):
            power = 0
        if power > 0:
            return power
        try:
            return max(1, int(sum(float(value) for value in attributes[:4])))
        except (TypeError, ValueError):
            return 1

    def _make_fight_unit(
        self,
        *,
        cfg_id,
        position,
        troop_type,
        attributes,
        skills,
        initial_buffs=None,
        level=1,
        power=0,
        pid="",
        is_helper=False,
        attribute_types=None,
        weak_num=0,
        weak_types=None,
    ):
        skills = self._battle_int_list(skills)
        attributes = list(attributes or [])
        return {
            "Attributes": attributes,
            # These metadata-only fields are ignored by FightUnitPOD encoding,
            # but are retained in the persisted snapshot for local simulation.
            "AttributeTypes": self._battle_int_list(attribute_types),
            "BattlePos": max(1, int(position)),
            "InitBuff": self._battle_int_list(initial_buffs),
            "IsHelper": bool(is_helper),
            "Level": max(1, int(level or 1)),
            "MonsterCfgId": int(cfg_id),
            "Pid": str(pid or ""),
            "Power": self._battle_power(attributes, power),
            "SPStatus": [False] * len(skills),
            "SkillStrengthens": [0] * len(skills),
            "Skills": skills,
            "TroopType": int(troop_type),
            "WeakNum": max(0, int(weak_num or 0)),
            "WeakTypes": self._battle_int_list(weak_types),
        }

    def _make_defender_units(self, monster_team_id):
        team = BATTLE_CONFIG.get("teams", {}).get(str(monster_team_id), {})
        monsters = BATTLE_CONFIG.get("monsters", {})
        units = []
        for position, monster_id in enumerate(team.get("teamUnit", []), start=1):
            if not monster_id:
                continue
            config = monsters.get(str(monster_id))
            if not config:
                continue
            units.append(
                self._make_fight_unit(
                    cfg_id=monster_id,
                    position=position,
                    troop_type=BATTLE_TROOP_DEFEND,
                    attributes=config.get("attributes", []),
                    skills=config.get("skills", []),
                    initial_buffs=config.get("initialBuff", []),
                    attribute_types=config.get("attributeTypes", []),
                    weak_num=config.get("weakNum", 0),
                    weak_types=config.get("weakTypes", []),
                    level=config.get("grade", 1),
                    power=config.get("power", 0),
                )
            )
        return units

    def _make_attacker_units(self):
        state_prefabs = storage.get_player_state_json(self.uid, "soulPrefabs")
        state_formations = storage.get_player_state_json(self.uid, "formations")
        prefabs = state_prefabs if isinstance(state_prefabs, list) else []
        formations = state_formations if isinstance(state_formations, list) else []
        if not prefabs and isinstance(self.player_snapshot, dict):
            prefabs = self.player_snapshot.get("soulPrefabs", [])
        if not formations and isinstance(self.player_snapshot, dict):
            formations = self.player_snapshot.get("formations", [])

        position_by_prefab = {}
        valid_formations = [row for row in formations if isinstance(row, dict)]
        valid_formations.sort(key=lambda row: (int(row.get("index") or 0), int(row.get("id") or 0)))
        for formation in valid_formations:
            mapping = formation.get("formation")
            if not isinstance(mapping, dict):
                continue
            for prefab_id, position in mapping.items():
                try:
                    position_by_prefab[int(prefab_id)] = int(position)
                except (TypeError, ValueError):
                    continue
            if position_by_prefab:
                break

        soul_rows = {row["soul_id"]: row for row in storage.get_souls(self.uid)}
        if not prefabs:
            prefabs = [
                {
                    "id": soul_id,
                    "soulCid": soul_id,
                    "position": index,
                    "power": 0,
                }
                for index, (soul_id, _level) in enumerate(
                    getattr(storage, "DEFAULT_SOULS_LIST", []), start=1
                )
            ]

        soul_configs = BATTLE_CONFIG.get("souls", {})
        skill_groups = BATTLE_CONFIG.get("skillGroups", {})
        monster_by_soul = BATTLE_CONFIG.get("monsterBySoul", {})
        units = []
        for fallback_position, prefab in enumerate(prefabs, start=1):
            if not isinstance(prefab, dict):
                continue
            try:
                soul_cid = int(prefab.get("soulCid") or prefab.get("cid") or 0)
                prefab_id = int(prefab.get("id") or 0)
            except (TypeError, ValueError):
                continue
            # Battle config contains NPC and future placeholder soul records.
            # A formation may only use a real player soul owned by this UID.
            if not storage.is_playable_soul_id(soul_cid) or soul_cid not in soul_rows:
                log.warning(
                    "  ignored invalid formation soul uid=%s soulCid=%s",
                    self.uid,
                    soul_cid,
                )
                continue
            soul_config = soul_configs.get(str(soul_cid), {})
            monster_ids = monster_by_soul.get(str(soul_cid), [])
            if not soul_cid or not monster_ids:
                continue
            try:
                position = position_by_prefab.get(
                    prefab_id, int(prefab.get("position") or fallback_position)
                )
            except (TypeError, ValueError):
                position = fallback_position
            skills = prefab.get("allSkills") or prefab.get("skills")
            if not skills:
                skills = skill_groups.get(str(prefab.get("skillGroupId") or 0))
            if not skills:
                skills = soul_config.get("skills", [])
            soul_row = soul_rows.get(soul_cid, {})
            base_soul_pod = None
            if isinstance(self.player_snapshot, dict):
                base_soul_pod = next(
                    (
                        row
                        for row in self.player_snapshot.get("souls", [])
                        if isinstance(row, dict)
                        and int(row.get("cid", 0) or 0) == soul_cid
                    ),
                    None,
                )
            local_soul_pod = local_soul_pod_for(
                self.uid,
                soul_cid,
                base_soul_pod,
                soul_row,
            )
            attributes = (
                local_soul_pod.get("soulAttr")
                or prefab.get("attr")
                or soul_config.get("attributes", [])
            )
            units.append(
                self._make_fight_unit(
                    cfg_id=monster_ids[0],
                    position=position,
                    troop_type=BATTLE_TROOP_ATTACK,
                    attributes=attributes,
                    attribute_types=prefab.get("attributeTypes") or soul_config.get("attributeTypes", []),
                    skills=skills,
                    level=local_soul_pod.get("lv") or prefab.get("lv") or soul_row.get("level", 1),
                    power=prefab.get("power", 0),
                    pid=self.uid,
                    is_helper=False,
                )
            )
        return units

    def _make_fight_pod(
        self, battle_id, battle_type, map_id=0, monster_team_id=0, random_seed=None
    ):
        """Build a FightPOD from extracted monster tables and player formations."""
        return {
            "ID": battle_id,
            "BattleType": battle_type,
            "MapID": map_id,
            "MonsterTeamID": monster_team_id,
            "RandomSeed": random_seed or int(time.time()) % 0x7FFFFFFF,
            "MaxRound": 30,
            "Attacker": {"ArrFightUnitPOD": self._make_attacker_units(), "Buffs": []},
            "Defender": {"ArrFightUnitPOD": self._make_defender_units(monster_team_id), "Buffs": []},
            "Players": [str(self.uid)],
            "BattleParams": {},
        }

    @staticmethod
    def _result_id_for_request(request_id):
        request = protocol_codec.METHODS.get(request_id)
        if not request:
            return None
        result_name = str(request.get("method", "")) + "Result"
        for candidate_id, candidate in protocol_codec.METHODS.items():
            if candidate.get("method") == result_name:
                return candidate_id
        return None

    def _record_low_frequency_request(self, request_id, request_body):
        state = storage.get_player_state_json(self.uid, "low_frequency") or {}
        key = str(request_id)
        entry = state.setdefault(key, {"count": 0})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["lastBody"] = bytes(request_body or b"").hex()
        try:
            entry["lastValues"] = protocol_codec.decode_method(request_id, bytes(request_body or b""))
        except (ValueError, KeyError):
            entry["lastValues"] = None
        storage.update_player_state_json(self.uid, "low_frequency", state)

    def _handle_low_frequency_message(self, request_id, request_body, forced_result_id=None):
        """Record an uncovered request and answer only from extracted metadata."""
        if not self.account or request_id not in protocol_codec.METHODS:
            return False
        request_body = bytes(request_body or b"")
        Session._record_low_frequency_request(self, request_id, request_body)
        result_id = forced_result_id
        if result_id is None:
            result_id = self._result_id_for_request(request_id)
        if result_id is not None and Session._handle_local_battle_entry(
            self, request_id, result_id, request_body
        ):
            return True
        if result_id is not None and module_handlers.handle_local_content_fallback(
            self, self.uid, request_id, request_body, result_id
        ):
            return True
        if result_id is None:
            log.info("  low-frequency request recorded uid=%s msgId=%s", self.uid, request_id)
            return True
        method = protocol_codec.METHODS.get(result_id)
        if not method:
            log.warning("  low-frequency response has no metadata msgId=%s resultId=%s", request_id, result_id)
            return False
        try:
            values = [Session._default_protocol_value(type_name) for type_name in method["types"]]
            body_bytes = protocol_codec.encode_method(result_id, *values)
        except (KeyError, TypeError, ValueError):
            log.warning("  low-frequency response schema failed msgId=%s resultId=%s", request_id, result_id)
            return False
        self.send(result_id, body_bytes)
        log.info("  low-frequency request handled uid=%s msgId=%s resultId=%s", self.uid, request_id, result_id)
        return True

    def _stub(self, res_id=None):
        """Compatibility entry for old callers; the message loop uses metadata dispatch."""
        return Session._handle_low_frequency_message(
            self,
            getattr(self, "_current_message_id", None),
            getattr(self, "_current_message_body", b""),
            res_id,
        )

    @staticmethod
    def _default_protocol_value(type_name):
        if type_name in ("int", "long"):
            return 0
        if type_name == "bool":
            return False
        if type_name == "string":
            return ""
        generic = protocol_codec.split_generic(type_name)
        if generic:
            outer, _inner = generic
            if outer == "list":
                return []
            if outer == "map":
                return {}
        if type_name in protocol_codec.POD_TYPES:
            return {}
        raise ValueError("unsupported default protocol type: %s" % type_name)

    def _send_reward_changes(self, result):
        """Emit the same incremental notifications used by battle/mail rewards."""
        for cid, quantity in (result or {}).get("changed_attrs", {}).items():
            self.send(NOTIFY_NUM_ATTR, protocol_codec.encode_method(NOTIFY_NUM_ATTR, {int(cid): int(quantity)}))
        changed_items = (result or {}).get("changed_items", [])
        if changed_items:
            self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, changed_items))

    def _local_soul_pod(self, soul_id):
        base = None
        player_snapshot = getattr(self, "player_snapshot", None)
        if isinstance(player_snapshot, dict):
            base = next(
                (pod for pod in player_snapshot.get("souls", [])
                 if isinstance(pod, dict) and int(pod.get("cid", 0) or 0) == int(soul_id)),
                None,
            )
        local = local_soul_pod_for(self.uid, soul_id, base)
        if isinstance(player_snapshot, dict):
            snapshot_soul = next(
                (
                    pod
                    for pod in player_snapshot.get("souls", [])
                    if isinstance(pod, dict)
                    and int(pod.get("cid", 0) or 0) == int(soul_id)
                ),
                None,
            )
            if snapshot_soul is not None:
                snapshot_soul.update(local)
        return local

    # ── Generic module handler ──

    def _handle_module_entry_or_action(self, msg_id, body):
        """Dispatch to module_handlers for real implementations."""
        if not self.account:
            return False
        return module_handlers.dispatch(self, self.uid, msg_id, body)


        """Handle an action request: validate, optionally persist, return success."""
        try:
            values = protocol_codec.decode_method(msg_id, body)
        except (ValueError, KeyError) as exc:
            log.warning("  invalid %s body %s: %s", msg_id, body.hex(), exc)
            return False
        if not self.account:
            return False
        if save_fn:
            save_fn(self.uid, values)
        if field_name:
            storage.update_player_state_json(self.uid, field_name, {"action": msg_id, "time": int(time.time())})
        self.send(res_id, protocol_codec.encode_method(res_id, 0))
        log.info("  module action %s uid=%s -> %d",
                 protocol_codec.METHODS.get(msg_id, {}).get("method", f"msg{msg_id}"),
                 self.uid, res_id)
        return True

    def handle_refresh_red_point(self, body):
        if body or not self.account:
            return False
        self.send(
            REFRESH_RED_POINT_RESULT,
            protocol_codec.encode_method(REFRESH_RED_POINT_RESULT, 0, False),
        )
        log.info("  guild red point refreshed uid=%s -> %d", self.uid, REFRESH_RED_POINT_RESULT)
        return True

    def handle_get_guild_score(self, body):
        if body or not self.account:
            return False
        self.send(
            GET_GUILD_SCORE_RESULT,
            protocol_codec.encode_method(GET_GUILD_SCORE_RESULT, 0, 0),
        )
        log.info("  guild challenge score uid=%s -> %d", self.uid, GET_GUILD_SCORE_RESULT)
        return True

    def handle_get_guild_training(self, body):
        if body or not self.account:
            return False
        self.send(
            GET_GUILD_TRAINING_RESULT,
            protocol_codec.encode_method(GET_GUILD_TRAINING_RESULT, 0, []),
        )
        log.info("  guild training integral uid=%s -> %d", self.uid, GET_GUILD_TRAINING_RESULT)
        return True

    def handle_exchange(self, body):
        try:
            cid, count = protocol_codec.decode_method(EXCHANGE, body)
        except ValueError as exc:
            log.warning("  invalid exchange body %s: %s", body.hex(), exc)
            return False
        if not self.account or cid <= 0 or count <= 0 or count > 99:
            return False
        seed_local_player_attrs(self.uid)
        result = storage.apply_exchange(
            self.uid,
            cid,
            count,
            module_rules._row("economy", "ExchangeTable", cid),
        )
        if result is None:
            self.send(EXCHANGE_RESULT, protocol_codec.encode_method(EXCHANGE_RESULT, 1, False, [], {}, 1.0))
            return True
        Session._send_reward_changes(self, result)
        multiples = result.get("critMultiples", {}).get(cid, [1])
        multiple = float(multiples[0]) if count == 1 and multiples else 1.0
        self.send(
            EXCHANGE_RESULT,
            protocol_codec.encode_method(EXCHANGE_RESULT, 0, True, result["rewards"], {cid: count}, multiple),
        )
        log.info("  exchange committed uid=%s cid=%d count=%d -> %d", self.uid, cid, count, EXCHANGE_RESULT)
        return True

    def handle_start_dating(self, body):
        try:
            soul_id, event_id = protocol_codec.decode_method(START_DATING, body)
        except ValueError as exc:
            log.warning("  invalid dating body %s: %s", body.hex(), exc)
            return False
        if not self.account or self.player_snapshot is None or self.active_story is not None:
            return False
        event = COMPANION_RULES.get("dating_events", {}).get(str(event_id))
        companion = storage.get_companion(self.uid, soul_id)
        if not event or not companion or event.get("SoulId") != soul_id:
            return False
        if companion["favor_level"] < int(event.get("UnlockLevel") or 1):
            return False
        predecessor = event.get("NeedDownDatingId")
        if predecessor and not storage.has_dating_record(self.uid, soul_id, predecessor):
            return False
        costs = list(zip(event.get("Cost", [])[::2], event.get("Cost", [])[1::2]))
        inventory = {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}
        if any(inventory.get(cid, 0) < quantity for cid, quantity in costs):
            return False
        self.active_story = {
            "kind": "dating",
            "soul_id": soul_id,
            "event_id": event_id,
            "dialog_cid": int(event["Dialog"]),
            "event": event,
            "begin_favor": int(companion["favor"]),
            "begin_favor_level": int(companion["favor_level"]),
        }
        self.send(
            START_DATING_RESULT,
            protocol_codec.encode_method(START_DATING_RESULT, 0, soul_id, event_id),
        )
        self.send(NOTIFY_OPEN_DIALOG, protocol_codec.encode_method(NOTIFY_OPEN_DIALOG, int(event["Dialog"])))
        log.info(
            "  dating opened uid=%s soulCid=%d eventCid=%d dialogCid=%d -> %d/%d",
            self.uid,
            soul_id,
            event_id,
            event["Dialog"],
            START_DATING_RESULT,
            NOTIFY_OPEN_DIALOG,
        )
        return True

    def finish_dating(self, select_index, skip_indexes):
        dating = self.active_story
        event = dating["event"]
        reward_values = event.get("Reward", [])
        rewards = list(zip(reward_values[::2], reward_values[1::2]))
        favor_delta = sum(quantity for cid, quantity in rewards if 10600 < cid < 10700)
        item_rewards = [(cid, quantity) for cid, quantity in rewards if not 10600 < cid < 10700]
        cost_values = event.get("Cost", [])
        costs = list(zip(cost_values[::2], cost_values[1::2]))
        end_favor = dating["begin_favor"] + favor_delta
        local_companion = storage.get_companion(self.uid, dating["soul_id"])
        end_level = favor_level_for(
            dating["soul_id"],
            end_favor,
            bool(local_companion["oath_activation"]),
        )
        if not storage.apply_companion_operation(
            self.uid,
            dating["soul_id"],
            dating["event_id"],
            costs,
            item_rewards,
            favor_delta,
            end_level,
        ):
            log.warning("  dating settlement transaction rejected uid=%s eventCid=%d", self.uid, dating["event_id"])
            return False

        self.send(SELECT_DIALOG_RESULT, protocol_codec.encode_method(SELECT_DIALOG_RESULT, 0, -1))
        inventory = {row["template_id"]: row["quantity"] for row in storage.get_items(self.uid)}
        touched = {cid for cid, _ in costs + item_rewards}
        changed_items = []
        for item_pod in self.player_snapshot.get("warehouse", []):
            cid = item_pod.get("cid")
            if cid in touched:
                item_pod["num"] = inventory.get(cid, 0)
                changed_items.append(item_pod)
        if changed_items:
            self.send(NOTIFY_ITEM_CHANGE, protocol_codec.encode_method(NOTIFY_ITEM_CHANGE, changed_items))
        soul_pod = next(
            pod for pod in self.player_snapshot["souls"] if pod.get("cid") == dating["soul_id"]
        )
        local = storage.get_companion(self.uid, dating["soul_id"])
        soul_pod.update({"favor": local["favor"], "favorLv": local["favor_level"]})
        self.send(UPDATE_SOUL, protocol_codec.encode_method(UPDATE_SOUL, soul_pod))
        records = storage.get_dating_records(self.uid, dating["soul_id"])
        self.send(NOTIFY_DATING, protocol_codec.encode_method(NOTIFY_DATING, dating["soul_id"], records))
        show_items = [{"cid": cid, "num": quantity, "tag": 0} for cid, quantity in item_rewards]
        favor_data = [dating["begin_favor_level"], dating["begin_favor"], end_level, end_favor]
        self.send(
            NOTIFY_DATING_END,
            protocol_codec.encode_method(
                NOTIFY_DATING_END,
                dating["soul_id"],
                dating["event_id"],
                show_items,
                favor_data,
            ),
        )
        log.info(
            "  dating completed uid=%s soulCid=%d eventCid=%d addFavor=%d selectIndex=%d skipped=%s -> %d/%d/%d/%d/%d",
            self.uid,
            dating["soul_id"],
            dating["event_id"],
            favor_delta,
            select_index,
            skip_indexes,
            SELECT_DIALOG_RESULT,
            NOTIFY_ITEM_CHANGE,
            UPDATE_SOUL,
            NOTIFY_DATING,
            NOTIFY_DATING_END,
        )
        self.active_story = None
        return True

    def handle_experience_story_chapter(self, body):
        try:
            story_cid, chapter_index = decode_story_chapter_request(body)
        except ValueError as exc:
            log.warning("  invalid story chapter body %s: %s", body.hex(), exc)
            return False
        if not self.account:
            log.warning("  story chapter rejected for compatibility session")
            return False
        story_config = STORY_CONFIG.get(str(story_cid))
        chapter_config = (
            story_config.get("chapters", {}).get(str(chapter_index))
            if story_config
            else None
        )
        if chapter_config is None:
            log.warning(
                "  story chapter rejected: no config for storyCid=%d chapterIndex=%d",
                story_cid,
                chapter_index,
            )
            return False
        if not storage.record_story_chapter(self.uid, story_cid, chapter_index):
            log.warning(
                "  story chapter rejected: uid=%s storyCid=%d chapterIndex=%d",
                self.uid,
                story_cid,
                chapter_index,
            )
            return False

        # Result fields are code, storyCid and chapterIndex in the same scalar encoding.
        self.send(EXPERIENCE_STORY_CHAPTER_RESULT, b"\x50" + body)
        dialog_cid = int(chapter_config["dialog_cid"])
        self.send(NOTIFY_OPEN_DIALOG, encode_compact_uint(dialog_cid))
        self.active_story = {
            "kind": "story",
            "story_cid": story_cid,
            "chapter_index": chapter_index,
            "dialog_cid": dialog_cid,
            "favor_level": int(story_config.get("unlock_favor_level", 1)),
        }
        log.info(
            "  story chapter opened uid=%s storyCid=%d chapterIndex=%d "
            "dialogCid=%d -> %d/%d",
            self.uid,
            story_cid,
            chapter_index,
            dialog_cid,
            EXPERIENCE_STORY_CHAPTER_RESULT,
            NOTIFY_OPEN_DIALOG,
        )
        return True

    def handle_select_dialog(self, body):
        try:
            select_index, skip_indexes = decode_select_dialog_request(body)
        except ValueError as exc:
            log.warning("  invalid select dialog body %s: %s", body.hex(), exc)
            return False
        if not self.account or self.active_story is None:
            log.warning("  select dialog rejected without an active story")
            return False

        if self.active_story.get("kind") == "dating":
            return self.finish_dating(select_index, skip_indexes)

        if self.active_story.get("kind") == "town":
            return module_handlers.handle_town_dialog(
                self,
                self.uid,
                select_index,
                skip_indexes,
            )

        if self.active_story.get("kind") == "home":
            story = self.active_story
            # Homeland plot dialogs are one-shot, non-reward dialogs. The
            # action was persisted when it opened; this packet only closes
            # the client dialog and releases the session interaction lock.
            self.send(SELECT_DIALOG_RESULT, protocol_codec.encode_method(1603, 0, -1))
            log.info(
                "  home plot completed uid=%s actionId=%d dialogCid=%d "
                "selectIndex=%d skipped=%s -> %d",
                self.uid,
                int(story.get("plot_id", 0)),
                int(story.get("dialog_cid", 0)),
                select_index,
                skip_indexes,
                SELECT_DIALOG_RESULT,
            )
            self.active_story = None
            return True

        story = self.active_story
        result_body = b"\x50" + encode_signed_protocol_int(-1)
        self.send(SELECT_DIALOG_RESULT, result_body)
        completion_body = encode_story_completion_notify(
            story["story_cid"],
            story["chapter_index"],
            favor_level=story["favor_level"],
            is_all_complete=True,
        )
        self.send(NOTIFY_COMPLETE_STORY_CHAPTER, completion_body)
        log.info(
            "  story chapter completed uid=%s storyCid=%d chapterIndex=%d "
            "dialogCid=%d selectIndex=%d skipped=%s -> %d/%d",
            self.uid,
            story["story_cid"],
            story["chapter_index"],
            story["dialog_cid"],
            select_index,
            skip_indexes,
            SELECT_DIALOG_RESULT,
            NOTIFY_COMPLETE_STORY_CHAPTER,
        )
        self.active_story = None
        return True

    def message_loop(self):
        while self.running:
            try:
                msg_id, order, body = read_msg(self.conn, timeout=60)
            except socket.timeout:
                log.info("  connection idle; continuing to wait")
                continue
            if msg_id is None:
                log.info("  peer closed the connection")
                break

            self._current_message_id = msg_id
            self._current_message_body = body

            log.info(
                "  [C->S] MsgID=%d order=%d body=%db %s",
                msg_id,
                order,
                len(body),
                body[:80].hex() if body else "(empty)",
            )
            if msg_id == VALIDATE_UUID:
                self.handle_validate_uuid(body)
            elif msg_id == CHOOSE_ROLE:
                self.handle_choose_role(body)
            elif msg_id == CREATE_ROLE:
                self.handle_create_role(body)
            elif msg_id == PING:
                storage.touch_session(self.session_id, self.uid if self.account else None)
                self._flush_pending_tcp_notifications()
                self.send(PANG, pang_body())
            elif msg_id == HEARTBEAT:
                self._flush_pending_tcp_notifications()
                continue
            elif msg_id == LOAD_PLAYER:
                self.send_player_snapshot()
            elif msg_id == HANDSEL_SOUL:
                self.send(HANDSEL_SOUL_RESULT, b"\x50")
            elif msg_id == LOGOUT:
                self.send(LOGOUT_RESULT, b"\x50")
                break
            elif msg_id == RECONNECT:
                self.handle_reconnect(body, order)
            elif msg_id == GET_GIRLS:
                self.handle_get_girls(body)
            elif msg_id == EXIT_GIRLS:
                self.send(EXIT_GIRLS_RESULT, b"\x50")
                log.info("  companion exitGirls -> %d", EXIT_GIRLS_RESULT)
            elif msg_id == WEAR_DRESS:
                self.handle_wear_dress(body)
            elif msg_id == VIEW_DRESS:
                self.handle_view_dress(body)
            elif msg_id == GET_MAILS:
                self.handle_get_mails(body)
            elif msg_id == READ_MAIL:
                self.handle_read_mail(body)
            elif msg_id == PICK_UP_MAIL:
                self.handle_pick_up_mail(body)
            elif msg_id == DELETE_MAIL:
                self.handle_delete_mail(body)
            elif msg_id == OPEN_LIBRARY:
                self.handle_open_library(body)
            elif msg_id == VIEW_NEWS_BOOK:
                self.handle_view_news_book(body)
            elif msg_id == GET_NEWS_BOOK_REWARDS:
                self.handle_get_news_book_rewards(body)
            elif msg_id == LOTTERY_DRAW:
                self.handle_lottery_draw(body)
            elif msg_id == GET_LOTTERY_HISTORY:
                self.handle_lottery_history(body)
            elif msg_id == CHANGE_SHOW_SOUL:
                self.handle_change_show_soul(body)
            elif msg_id == COMMIT_QUEST:
                self.handle_commit_quest(body)
            elif msg_id == GIVE_UP_QUEST:
                self.handle_give_up_quest(body)
            elif msg_id == UNLOCK_CHAPTER_TASKS:
                self.handle_unlock_chapter_tasks(body)
            elif msg_id == GIVE_GIFT:
                self.handle_give_gift(body, order)
            elif msg_id == FONDLE:
                self.handle_fondle(body, order)
            elif msg_id == GET_SOUL_OATH:
                self.handle_get_soul_oath(body)
            elif msg_id == CONNECTIVE:
                self.handle_connective(body)
            elif msg_id == SAVE_SETTING:
                self.handle_save_setting(body)
            elif msg_id == SIGN:
                self.handle_sign(body)
            elif msg_id == GET_LV_REACH_REWARDS:
                self.handle_get_lv_reach_rewards(body)
            elif msg_id == TRIGGER_GUIDE:
                self.handle_trigger_guide(body)
            elif msg_id == REFRESH_READ_POINT:
                self.handle_refresh_read_point(body)
            elif msg_id == SAVE_SHOW_COLLECT_ITEMS:
                self.handle_save_show_collect_items(body)
            elif msg_id == USE_EQUIP_SKIN:
                self.handle_use_equip_skin(body)
            elif msg_id == SAVE_PLAYER_SETTING:
                self.handle_save_player_setting(body)
            elif msg_id == DRESS_UP_ROTATE_SWITCH:
                self.handle_dress_up_rotate_switch(body)
            elif msg_id == DRESS_UP_ROTATE_LIST:
                self.handle_dress_up_rotate_list(body)
            elif msg_id == LUCK_DRAW:
                self.handle_luck_draw(body)
            elif msg_id == GET_LUCK_DRAW_HISTORY:
                self.handle_get_luck_draw_history(body)
            elif msg_id == GET_LV_REACH_REWARD:
                self.handle_get_lv_reach_reward(body)
            elif msg_id == GET_REFUNDS_GIFT_PACKS:
                self.handle_get_refunds_gift_packs(body)
            elif msg_id == REFRESH_RED_POINT:
                self.handle_refresh_red_point(body)
            elif msg_id == GET_GUILD_SCORE:
                self.handle_get_guild_score(body)
            elif msg_id == GET_GUILD_TRAINING:
                self.handle_get_guild_training(body)
            elif msg_id == SELL_ITEM:
                self.handle_sell_item(body)
            elif msg_id == USE_ITEM:
                self.handle_use_item(body)
            elif msg_id == DESTROY_ITEM:
                self.handle_destroy_item(body)
            elif msg_id == EXCHANGE_BATCH:
                self.handle_exchange_batch(body)
            elif msg_id == LOCK_EQUIPMENT:
                self.handle_lock_equipment(body)
            elif msg_id == OPTIONAL_GIFT:
                self.handle_optional_gift(body)
            elif msg_id == UNLOCK_SOUL:
                self.handle_unlock_soul(body)
            elif msg_id == USE_SOUL_EXP_ITEM:
                self.handle_use_soul_exp_item(body)
            elif msg_id == EVOLUTION:
                self.handle_evolution(body)
            elif msg_id == ACTIVE_TALENT:
                self.handle_active_talent(body)
            elif msg_id == ACTIVE_TALENT_GROUP:
                self.handle_active_talent_group(body)
            elif msg_id == UNLOCK_SKILL_GROUP:
                self.handle_unlock_skill_group(body)
            elif msg_id == ACTIVATION_SKILL_STRENGTHEN:
                self.handle_activation_skill_strengthen(body)
            elif msg_id == ACTIVE_SPECIAL_SPIRIT:
                self.handle_active_special_spirit(body)
            elif msg_id == DISBIND_ROLE:
                self.handle_disbind_role(body)
            elif msg_id == CHANGE_DATA:
                self.handle_change_data(body)
            elif msg_id == GET_PLAYER_INFO:
                self.handle_get_player_info(body)
            elif msg_id == SEND_GIFT_CODE:
                self.handle_send_gift_code(body)
            elif msg_id == BUY_ADVANCE_LEVEL_CHASE:
                self.handle_buy_advance_level_chase(body)
            elif msg_id == SHOP_BUY:
                self.handle_shop_buy(body)
            elif msg_id == SHOP_REFRESH:
                self.handle_shop_refresh(body)
            elif msg_id == REMOVE_FRIENDS:
                self.handle_remove_friends(body)
            elif msg_id == APPLY_FRIENDS:
                self.handle_apply_friends(body)
            elif msg_id == DEAL_WITH_APPLY:
                self.handle_deal_with_apply(body)
            elif msg_id == ADD_BLACKLIST:
                self.handle_add_blacklist(body)
            elif msg_id == REMOVE_BLACKLIST:
                self.handle_remove_blacklist(body)
            elif msg_id == SEARCH_PLAYER:
                self.handle_search_player(body)
            elif msg_id == SET_REMARK:
                self.handle_set_remark(body)
            elif msg_id == RECOMMEND_FRIENDS:
                self.handle_recommend_friends(body)
            elif msg_id == REGISTER_SIMPLE_PLAYER:
                self.handle_register_simple_player(body)
            elif msg_id == CHANGE_PLAYER_NAME:
                self.handle_change_player_name(body)
            elif msg_id == LOAD_CENTER_PLAYER:
                self.handle_load_center_player(body)
            elif msg_id == OFFLINE_NOTIFY:
                self.handle_center_offline(body)
            elif msg_id == UPLOAD_SIMPLE_PLAYER:
                self.handle_upload_simple_player(body)
            elif msg_id == UPLOAD_RANK_SCORE:
                self.handle_upload_rank_score(body)
            elif msg_id == 202:
                self.handle_open_home_box(body)
            elif msg_id == 203:
                self.handle_help_home(body)
            elif msg_id == 2702: self.handle_wear_equipment(body)
            elif msg_id == 2703: self.handle_dump_equipment(body)
            elif msg_id == 2704: self.handle_upgrade_equipment(body)
            elif msg_id == 2705: self.handle_upstar_equipment(body)
            elif msg_id == 2706: self.handle_decp_equipment(body)
            elif msg_id == 2707: self.handle_change_soul_prefab(body)
            elif msg_id == 2708: self.handle_change_formation_pos(body)
            elif msg_id == 2709: self.handle_exchange_equipment(body)
            elif msg_id == 2719: self.handle_wear_equipment_prefab(body)
            elif msg_id == 2720: self.handle_save_equipment_prefab(body)
            elif msg_id == 2724: self.handle_cover_equipments(body)
            elif msg_id == 2726: self.handle_chang_equipment_prefab_name(body)
            elif msg_id == 2728: self.handle_set_jewelry_speed(body)
            elif msg_id == QUICK_CHALLENGE: self.handle_quick_challenge(body)
            elif self._handle_module_entry_or_action(msg_id, body):
                pass
            elif msg_id == 2902:
                self.handle_fight_over(body)
            elif msg_id == 3002:
                self.handle_challenge_dup(body)
            elif msg_id == EXCHANGE:
                self.handle_exchange(body)
            elif msg_id == START_DATING:
                self.handle_start_dating(body)
            elif msg_id == EXPERIENCE_STORY_CHAPTER:
                self.handle_experience_story_chapter(body)
            elif msg_id == ENTER_MAZE:
                self.handle_enter_maze(body)
            elif msg_id == MAZE_SETTLEMENT:
                self.handle_maze_settlement(body)
            elif msg_id == SAVE_MAZE:
                self.handle_save_maze(body)
            elif msg_id == RESTORE_MAZE:
                self.handle_restore_maze(body)
            elif msg_id == REVIVE_MAZE:
                self.handle_revive_maze(body)
            elif msg_id == UPLOAD_MAZE_QUEST:
                self.handle_upload_maze_quest(body)
            elif msg_id == UPLOAD_MAZE_ALIEN:
                self.handle_upload_maze_alien(body)
            elif msg_id == OPEN_HIDDEN_MAZE:
                self.handle_open_hidden_maze(body)
            elif msg_id == BUY_MAZE_COUNT:
                self.handle_buy_maze_count(body)
            elif msg_id == MOP_UP:
                self.handle_mop_up(body)
            elif msg_id == ABANDON_MAZE:
                self.handle_abandon_maze(body)
            elif msg_id == ENTER_ABYSS_MAZE:
                self.handle_enter_abyss_maze(body)
            elif msg_id == ENTER_HIDDEN_MAZE:
                self.handle_enter_hidden_maze(body)
            elif msg_id == UPLOAD_MAZE_MONSTER_UNLOCK:
                self.handle_upload_maze_monster_unlock(body)
            elif msg_id == ENTER_ILLUSION_MAZE:
                self.handle_enter_illusion_maze(body)
            elif msg_id == ENTER_TEST_MAZE:
                self.handle_enter_test_maze(body)
            elif msg_id == ILLUSION_MOP_UP:
                self.handle_illusion_mop_up(body)
            elif msg_id == FISHING:
                self.handle_fishing(body)
            elif msg_id == FISHING_CONFIRM:
                self.handle_fishing_confirm(body)
            elif msg_id == ILLEGAL_FISHING:
                self.handle_illegal_fishing(body)
            elif msg_id == EXCHANGE_FISH:
                self.handle_exchange_fish(body)
            elif msg_id == EXCHANGE_FISH_BY_TYPE:
                self.handle_exchange_fish_by_type(body)
            elif msg_id == AUTO_FISHING:
                self.handle_auto_fishing(body)
            elif msg_id == FISHING_DRAW_REWARDS:
                self.handle_fishing_draw_rewards(body)
            elif msg_id == ACTIVITY_FISHING:
                self.handle_activity_fishing(body)
            elif msg_id == ACTIVITY_FISHING_CONFIRM:
                self.handle_activity_fishing_confirm(body)
            elif msg_id == ACTIVITY_GET_AUTO_REWARDS:
                self.handle_activity_get_auto_rewards(body)
            elif msg_id == ACTIVITY_UP_ROLE:
                self.handle_activity_up_role(body)
            elif msg_id == ACTIVITY_UP_SKILL:
                self.handle_activity_up_skill(body)
            elif msg_id == ACTIVITY_UP_ACTION:
                self.handle_activity_up_action(body)
            elif msg_id == ACTIVITY_GET_STORY_REWARDS:
                self.handle_activity_get_story_rewards(body)
            elif msg_id == GARRISON:
                self.handle_garrison(body)
            elif msg_id == GET_ASSISTS:
                self.handle_get_assists(body)
            elif msg_id == REFRESH_ASSIST:
                self.handle_refresh_assist(body)
            elif msg_id == GET_STRENGTHEN_SOUL_PREFAB:
                self.handle_get_strengthen_soul_prefab(body)
            elif msg_id == ENTER_FORT_MAZE:
                self.handle_enter_fort_maze(body)
            elif msg_id == ENTER_SEAL_MAZE:
                self.handle_enter_seal_maze(body)
            elif msg_id == ENTER_STRENGTHEN_MAZE:
                self.handle_enter_strengthen_maze(body)
            elif msg_id == QUICK_CHALLENGE:
                self.handle_quick_challenge(body)
            elif msg_id == SELECT_DIALOG:
                self.handle_select_dialog(body)
            elif self._handle_low_frequency_message(msg_id, body):
                pass
            elif not self.handle_captured_response(msg_id):
                log.info("  no response mapping for MsgID=%d", msg_id)
        log.info("  message loop ended")

    def run(self):
        try:
            if self.handshake():
                self.message_loop()
        except socket.timeout:
            log.info("  handshake timeout")
        except (ConnectionError, ConnectionResetError) as exc:
            log.info("  connection ended: %s", exc)
        except Exception:
            log.exception("  session error")
        finally:
            storage.close_session(self.session_id)
            self.conn.close()
            log.info("  connection closed")


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((_BIND_HOST, TCP_PORT))
    server.listen(10)
    log.info("=" * 50)
    log.info("Soul Tide TCP Game Server v4")
    log.info("Listening on :%d", TCP_PORT)
    log.info("=" * 50)
    while True:
        conn, addr = server.accept()
        session = Session(conn, addr)
        threading.Thread(target=session.run, daemon=True).start()


if __name__ == "__main__":
    main()
