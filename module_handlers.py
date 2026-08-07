"""Module handler implementations for all remaining game modules.
Each module gets state persistence via player_state_json and real protocol responses."""

import hashlib
import json
import random
import time
from pathlib import Path
import storage
import protocol_codec
import module_rules

log = __import__("logging").getLogger("tcp_server")

# The extended handlers share the validated codec, numeric conversion and
# reward transaction helpers used by the configuration-backed rules module.
ROOT = Path(__file__).resolve().parent
_int = module_rules._int
_decode_or_reject = module_rules._decode_or_reject
_send = module_rules._send
_trade = module_rules._trade
_item_show = module_rules._item_show


def _load_official_capture_observations():
    path = ROOT / "analysis" / "official_capture_observations.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        log.exception("failed to load official capture observations from %s", path)
        return {}
    return value if isinstance(value, dict) else {}


OFFICIAL_CAPTURE_OBSERVATIONS = _load_official_capture_observations()


def _load_soul_memory_config():
    path = ROOT / "analysis" / "soul_memory_config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        log.exception("failed to load soul memory config from %s", path)
        return {"chapters": {}, "pieces": {}}
    return value if isinstance(value, dict) else {"chapters": {}, "pieces": {}}


SOUL_MEMORY_CONFIG = _load_soul_memory_config()


def _load_local_snapshot(filename, default):
    try:
        value = json.loads((ROOT / "analysis" / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        log.exception("failed to load local config snapshot %s", filename)
        return default
    return value if isinstance(value, dict) else default


HOME_CONFIG = _load_local_snapshot(
    "homeland_config_5392.json",
    {
        "rooms": {}, "plantGrids": {}, "decorates": {}, "decorateSuits": {},
        "homePlotDialogs": {}, "homePlotDialogCids": [], "referencedConditions": {},
    },
)
TOWN_CONFIG = _load_local_snapshot(
    "town_config_5392.json",
    {"areas": {}, "events": {}},
)
TOWN_DIALOG_CONFIG = _load_local_snapshot(
    "town_dialog_config_5392.json",
    {"roots": {}, "dialogs": {}, "executions": {}, "conditions": {}},
)

# CfgHomeLandDecorateThemeTable is a separate tiny config from the condensed
# homeland snapshot. These are the 5392 thresholds used by the client UI.
HOME_THEME_SCORES = {
    1: [(10, 500)], 2: [(10, 1500)], 3: [(10, 1500)],
    4: [(10, 2000)], 5: [(10, 1500)], 6: [(10, 2000)],
    7: [(10, 2000)], 8: [(10, 2000)], 9: [(10, 2000)],
    10: [(10, 2000)], 10001: [(5, 500)],
}

# This is the five-event selection present in the official 5392 TownPOD
# baseline. Keep it as a deterministic seed for a new local town; completed
# entries are replaced from the same config-backed candidate set.
TOWN_DEFAULT_SHOPPING_EVENT_IDS = (403528, 803512, 603354, 903523, 303524)
TOWN_SHOPPING_EVENT_COUNT = len(TOWN_DEFAULT_SHOPPING_EVENT_IDS)

# The client ships only the reward-group reference for most mining elements;
# the server-side group contents are not part of the APK. Keep a deterministic
# local catalog so every configured mine can be completed offline. Observed
# samples above always take precedence over these playable defaults.
LOCAL_MINING_REWARD_GROUPS = {
    11120101: [(46111, 1)],
    11120102: [(46106, 2), (46108, 1), (46109, 1)],
    11120103: [(46106, 1), (46108, 1)],
    11120104: [(316, 3)],
    11120105: [(316, 5)],
    11120106: [(316, 7)],
    11120107: [(316, 10)],
    11120108: [(316, 15)],
    11120109: [(320, 1)],
    11120110: [(320, 2)],
}


def _stamp():
    return int(time.time())


def _safe_positive_int_list(value):
    """Return a stable positive integer list without trusting local JSON."""
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for raw_value in value:
        value = _int(raw_value, 0)
        if value > 0 and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _read_state_json(uid, field_name):
    try:
        return storage.get_player_state_json(uid, field_name)
    except (TypeError, ValueError, json.JSONDecodeError):
        log.warning("ignoring malformed local state uid=%s field=%s", uid, field_name)
        return None


def _state(uid, key):
    """Load module state dict from player_state_json."""
    data = storage.get_player_state_json(uid, key)
    return data if isinstance(data, dict) else {}


def _save(uid, key, data):
    """Save module state and expose failures to stateful handlers."""
    try:
        saved = storage.update_player_state_json(uid, key, data)
    except Exception:
        log.exception("state save failed uid=%s field=%s", uid, key)
        return False
    if not saved:
        log.error("state save rejected uid=%s field=%s", uid, key)
        return False
    return True


def _notify_memory_chapter(session, chapter):
    session.send(3610, protocol_codec.encode_method(3610, chapter))


def _init_state(uid, key, defaults):
    """Initialize state with defaults if not exists."""
    data = _state(uid, key)
    changed = False
    for k, v in defaults.items():
        if k not in data:
            data[k] = json.loads(json.dumps(v, ensure_ascii=False))
            changed = True
    if changed:
        _save(uid, key, data)
    return data


def _send_rewards(session, rewards, attrs=None):
    """Send reward notifications (3924/4102)."""
    if attrs:
        for cid, qty in attrs.items():
            session.send(3924, protocol_codec.encode_method(3924, {cid: qty}))
    if rewards:
        session.send(4102, protocol_codec.encode_method(4102, rewards))


def _grant_rewards(session, uid, pairs):
    applied = storage.grant_reward_pairs(uid, list(pairs or []))
    if applied is None:
        return None
    for cid, quantity in applied.get("changed_attrs", {}).items():
        session.send(3924, protocol_codec.encode_method(3924, {int(cid): int(quantity)}))
    if applied.get("changed_items"):
        session.send(4102, protocol_codec.encode_method(4102, applied["changed_items"]))
    return applied


def _send_reward_changes(session, result):
    if not result:
        return
    for cid, quantity in result.get("changed_attrs", {}).items():
        session.send(3924, protocol_codec.encode_method(3924, {int(cid): int(quantity)}))
    if result.get("changed_items"):
        session.send(4102, protocol_codec.encode_method(4102, result["changed_items"]))


def _send_base_info_update(session, uid):
    """Push the current PlayerBaseInfoPOD so payPoint display stays in sync."""
    player = storage.get_player(uid) or {}
    profile = storage.get_player_state_json(uid, "player_profile") or {}
    attrs = storage.get_player_num_attrs(uid)
    wallet = storage.get_offline_wallet(uid)
    base_info = {
        "pid": str(profile.get("pid", player.get("role_id", uid))),
        "uid": str(uid),
        "pName": str(profile.get("name", player.get("role_name", "local"))),
        "pLv": int(player.get("level", 1)), "exp": 0, "power": 0,
        "guildId": 0, "leaderCid": 20010001, "showSoulCid": 20010001,
        "headIcon": 0, "avatarFrame": 0, "chatBackground": 0,
        "title": 0, "vip": 0, "vipexp": 0, "payPoint": int(attrs.get(5, 0)),
        "sumPay": int(wallet.get("sumPay", 0)),
        "guid": 0, "sceneID": 0, "areaId": "local", "serverId": "local",
        "channelNo": "local", "openId": "local", "intro": "", "sdkName": "local",
        "createTime": int(player.get("updated_at", 0)),
    }
    session.send(3918, protocol_codec.encode_method(3918, base_info))


# -- Extended local operation rules ---------------------------------------

EXTENDED_OPERATION_DEFAULTS = {
    "image_puzzle": {"unlocked": [], "unlock_claims": [], "reward_claims": []},
    "new_character": {"unlocked": [], "stories": [], "logs": []},
    "gacha": {"draws": {}, "refreshes": {}},
    "double_fight": {"reward_claims": [], "last_fight": {}},
    "space_treasure": {"explores": [], "reward_claims": []},
    "furniture_gacha": {"draws": 0, "reward_claims": []},
    "treasure_hunt": {"exchanges": [], "reward_claims": []},
    "survival": {"active": False, "unlimited": False, "level": 1, "last": {}, "claims": []},
}


def _extended_state(uid):
    return _init_state(uid, "remaining_operations", EXTENDED_OPERATION_DEFAULTS)


def _extended_data(event_id, data_id=None, **fields):
    data = {"eventCfgId": _int(event_id), "dataCfgId": _int(data_id if data_id is not None else event_id)}
    data.update(fields)
    return data


def _extended_claim(session, uid, state, claim_group, claim_key, pairs):
    applied = storage.claim_reward_once(uid, "extended_operation_claims", "%s:%s" % (claim_group, claim_key), pairs)
    if applied is None:
        return None
    _send_reward_changes(session, applied)
    return list(pairs) if applied.get("claimed") else []


_LOCAL_LOTTERY_CACHE = None


def _local_lottery_config():
    global _LOCAL_LOTTERY_CACHE
    if _LOCAL_LOTTERY_CACHE is not None:
        return _LOCAL_LOTTERY_CACHE
    try:
        def read(name):
            return json.loads((ROOT / "analysis" / name).read_text(encoding="utf-8"))
        _LOCAL_LOTTERY_CACHE = (
            read("lottery_actions.json"),
            read("lottery_packs.json"),
            read("lottery_drop_config.json").get("drops", {}),
            storage.load_lottery_tier_config(),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        _LOCAL_LOTTERY_CACHE = ({}, {}, {}, {})
    return _LOCAL_LOTTERY_CACHE


def _local_gacha_reward(pool_id, uid, draw_index):
    actions, packs, drops, official_tiers = _local_lottery_config()
    action = actions.get(str(_int(pool_id)))
    if not isinstance(action, dict):
        return None
    # The operation module reuses lottery pool IDs. Resolve those IDs from the
    # same official 5392 tier table as net_lottery instead of its old pack list.
    for show_id, show in (official_tiers.get("shows", {}) or {}).items():
        if _int(pool_id) not in [int(value) for value in show.get("pools", [])]:
            continue
        try:
            pool = storage.resolve_lottery_pool(int(show_id), pool_id, [], official_tiers)
            seed = hashlib.sha256(
                (str(uid) + ":" + str(draw_index) + ":gacha").encode()
            ).digest()
            rng = random.Random(int.from_bytes(seed[:8], "little"))
            drawn, _left = storage._draw_from_pool(pool, 0, 1, rng)
        except (storage.LotteryPoolError, TypeError, ValueError, KeyError):
            return None
        return action, [(int(drawn[0]), 1)] if drawn else None
    pack_groups = action.get("packIds") or []
    if not pack_groups:
        return None
    group = pack_groups[_int(draw_index) % len(pack_groups)]
    candidates = [_int(value) for value in (group if isinstance(group, list) else [group]) if _int(value) > 0]
    candidates = [value for value in candidates if isinstance(packs.get(str(value)), dict)]
    if not candidates:
        return None
    weighted = [(value, max(0, _int((packs.get(str(value)) or {}).get("weight")))) for value in candidates]
    total = sum(weight for _value, weight in weighted)
    if total <= 0:
        selected = candidates[int.from_bytes(hashlib.sha256((str(uid) + ":" + str(draw_index)).encode()).digest()[:4], "little") % len(candidates)]
    else:
        marker = int.from_bytes(hashlib.sha256((str(uid) + ":" + str(draw_index)).encode()).digest()[:8], "little") % total
        selected = weighted[-1][0]
        for value, weight in weighted:
            if marker < weight:
                selected = value
                break
            marker -= weight
    drop_id = _int((packs.get(str(selected)) or {}).get("dropId"))
    raw_reward = drops.get(str(drop_id), [])
    pairs = []
    for row in raw_reward if isinstance(raw_reward, list) else []:
        if isinstance(row, dict) and _int(row.get("cid")) > 0 and _int(row.get("num")) > 0:
            pairs.append((_int(row.get("cid")), _int(row.get("num"))))
    if not pairs:
        # A client-only pack may have no recoverable local drop definition.
        # Refuse the draw before charging its cost instead of creating an
        # apparently successful zero-reward transaction.
        return None
    return action, pairs


def handle_image_puzzle_unlock(session, uid, body):
    values = _decode_or_reject(5102, body)
    if values is None:
        return False
    (image_id,) = values
    if _int(image_id) <= 0:
        _send(session, 5105, 1, _extended_data(image_id))
        return True
    state = _extended_state(uid)
    unlocked = set(_int(value) for value in state["image_puzzle"].get("unlocked", []))
    already = _int(image_id) in unlocked
    unlocked.add(_int(image_id))
    state["image_puzzle"]["unlocked"] = sorted(unlocked)
    _save(uid, "remaining_operations", state)
    _send(session, 5105, 0 if not already else 1, _extended_data(image_id))
    return True


def _handle_image_puzzle_reward(session, uid, body, request_id, result_id):
    values = _decode_or_reject(request_id, body)
    if values is None:
        return False
    image_id, part_id = values
    if min(_int(image_id), _int(part_id)) <= 0:
        _send(session, result_id, 1, [], _extended_data(image_id))
        return True
    state = _extended_state(uid)
    group = "unlock" if request_id == 5103 else "reward"
    pairs = [(1, 100 if request_id == 5103 else 200)]
    reward = _extended_claim(session, uid, state, "image_%s" % group, "%d:%d" % (_int(image_id), _int(part_id)), pairs)
    if reward is None:
        return False
    claims = state["image_puzzle"].setdefault("%s_claims" % ("unlock" if request_id == 5103 else "reward"), [])
    key = "%d:%d" % (_int(image_id), _int(part_id))
    if key not in claims:
        claims.append(key)
        _save(uid, "remaining_operations", state)
    _send(session, result_id, 0, _item_show(reward), _extended_data(image_id))
    return True


def handle_new_character_unlock(session, uid, body):
    values = _decode_or_reject(5302, body)
    if values is None:
        return False
    event_id, character_id = values
    if min(_int(event_id), _int(character_id)) <= 0:
        _send(session, 5305, 1, [], [], [], [])
        return True
    state = _extended_state(uid)
    data = state["new_character"]
    key = [_int(event_id), _int(character_id)]
    if key not in data.setdefault("unlocked", []):
        data["unlocked"].append(key)
    _save(uid, "remaining_operations", state)
    _send(session, 5305, 0, [_int(character_id)], [_int(event_id)], [], [])
    return True


def handle_new_character_log(session, uid, body):
    values = _decode_or_reject(5303, body)
    if values is None:
        return False
    event_id, page = values
    state = _extended_state(uid)
    data = state["new_character"]
    data.setdefault("logs", []).append({"event": _int(event_id), "page": _int(page), "time": _stamp()})
    data["logs"] = data["logs"][-50:]
    _save(uid, "remaining_operations", state)
    _send(session, 5306, 0, [])
    return True


def handle_new_character_story(session, uid, body):
    values = _decode_or_reject(5304, body)
    if values is None:
        return False
    event_id, story_id = values
    if min(_int(event_id), _int(story_id)) <= 0:
        _send(session, 5307, 1)
        return True
    state = _extended_state(uid)
    state["new_character"].setdefault("stories", []).append([_int(event_id), _int(story_id)])
    _save(uid, "remaining_operations", state)
    _send(session, 5307, 0)
    return True


def handle_gacha_pool_draw(session, uid, body):
    values = _decode_or_reject(5402, body)
    if values is None:
        return False
    (pool_id,) = values
    state = _extended_state(uid)
    data = state["gacha"]
    draw_index = _int(data.setdefault("draws", {}).get(str(_int(pool_id))))
    selected = _local_gacha_reward(pool_id, uid, draw_index)
    if selected is None:
        _send(session, 5403, 1, 0, [], _extended_data(pool_id))
        return True
    action, rewards = selected
    draw_count = 10 if _int(action.get("lotteryMode")) == 2 else 1
    costs = []
    cost_cid, cost_num = _int(action.get("costCid")), _int(action.get("costNum"))
    if cost_cid > 0 and cost_num > 0:
        costs = [(cost_cid, cost_num * draw_count)]
    if _trade(session, uid, costs, rewards) is None:
        _send(session, 5403, 1, 0, [], _extended_data(pool_id))
        return True
    data["draws"][str(_int(pool_id))] = draw_index + draw_count
    _save(uid, "remaining_operations", state)
    _send(session, 5403, 0, draw_count, _item_show(rewards), _extended_data(pool_id))
    return True


def handle_gacha_pool_refresh(session, uid, body):
    values = _decode_or_reject(5404, body)
    if values is None:
        return False
    (pool_id,) = values
    if _local_gacha_reward(pool_id, uid, 0) is None:
        _send(session, 5405, 1, _extended_data(pool_id))
        return True
    state = _extended_state(uid)
    refreshes = state["gacha"].setdefault("refreshes", {})
    refreshes[str(_int(pool_id))] = _int(refreshes.get(str(_int(pool_id)))) + 1
    _save(uid, "remaining_operations", state)
    _send(session, 5405, 0, _extended_data(pool_id))
    return True


def _start_extended_battle(session, uid, request_id, result_id, body, module, key, battle_type=2):
    """Bind an activity entry to the exact server-owned battle it starts."""
    if not callable(getattr(session, "_handle_local_battle_entry", None)):
        return False
    if storage.get_active_battle(uid) is not None:
        _send(session, result_id, 1)
        return True
    if not session._handle_local_battle_entry(request_id, result_id, body or b""):
        return False
    active = storage.get_active_battle(uid, battle_type)
    if active is None:
        return False
    state = _extended_state(uid)
    data = state[module]
    data["pending_battle_id"] = str(active["id"])
    data["pending_key"] = _int(key)
    data["pending_battle_type"] = int(battle_type)
    _save(uid, "remaining_operations", state)
    return True


def handle_double_fight_start(session, uid, body):
    values = _decode_or_reject(5502, body)
    if values is None:
        return False
    if not values or _int(values[0]) <= 0 or _int(values[1]) <= 0:
        _send(session, 5504, 1)
        return True
    return _start_extended_battle(session, uid, 5502, 5504, body, "double_fight", values[1])


def handle_double_fight_rewards(session, uid, body):
    values = _decode_or_reject(5503, body)
    if values is None:
        return False
    event_id, fight_id, reward_ids = values
    if _int(event_id) <= 0 or _int(fight_id) <= 0 or not isinstance(reward_ids, list) or len(reward_ids) > 50:
        _send(session, 5505, 1, [], 0, 0, [])
        return True
    state = _extended_state(uid)
    data = state["double_fight"]
    pending_id = data.get("pending_battle_id")
    battle = storage.get_battle_instance(uid, pending_id) if pending_id else None
    if (
        battle is None
        or battle.get("battle_type") != 2
        or battle.get("status") != "won"
        or str(battle.get("id")) != str(pending_id)
        or _int(data.get("pending_key")) != _int(fight_id)
    ):
        _send(session, 5505, 1, [], 0, 0, [])
        return True
    key = "battle:%s" % battle["id"]
    reward = _extended_claim(session, uid, state, "double_fight", key, [(1, 100)])
    if reward is None:
        return False
    state["double_fight"]["last_fight"] = {"event": _int(event_id), "fight": _int(fight_id), "time": _stamp()}
    state["double_fight"].pop("pending_battle_id", None)
    state["double_fight"].pop("pending_key", None)
    _save(uid, "remaining_operations", state)
    _send(session, 5505, 0, _item_show(reward), _int(event_id), _int(fight_id), [_int(value) for value in reward_ids])
    return True


def handle_space_treasure_explore(session, uid, body):
    values = _decode_or_reject(5702, body)
    if values is None:
        return False
    event_id, area_id, node_id = values
    if min(_int(event_id), _int(area_id), _int(node_id)) <= 0:
        _send(session, 5703, 1, _int(event_id), _int(area_id), _int(node_id), _item_show([]))
        return True
    state = _extended_state(uid)
    key = "%d:%d:%d" % (_int(event_id), _int(area_id), _int(node_id))
    reward = _extended_claim(session, uid, state, "space_treasure", key, [(1, 50)])
    if reward is None:
        return False
    state["space_treasure"].setdefault("explores", []).append(key)
    _save(uid, "remaining_operations", state)
    _send(session, 5703, 0, _int(event_id), _int(area_id), _int(node_id), _item_show(reward))
    return True


def handle_furniture_gacha_draw(session, uid, body):
    values = _decode_or_reject(5802, body)
    if values is None:
        return False
    event_id, count = values
    count = _int(count)
    if _int(event_id) <= 0 or count <= 0 or count > 10:
        _send(session, 5803, 1, [])
        return True
    state = _extended_state(uid)
    key = "%d:%d:%d" % (_int(event_id), count, _int(state["furniture_gacha"].get("draws")))
    reward = _extended_claim(session, uid, state, "furniture_gacha", key, [(1, 100 * count)])
    if reward is None:
        return False
    state["furniture_gacha"]["draws"] = _int(state["furniture_gacha"].get("draws")) + count
    _save(uid, "remaining_operations", state)
    _send(session, 5803, 0, _item_show(reward))
    return True


def handle_treasure_hunt_exchange(session, uid, body):
    values = _decode_or_reject(6102, body)
    if values is None:
        return False
    event_id, gift_id = values
    if min(_int(event_id), _int(gift_id)) <= 0:
        _send(session, 6103, 1, [])
        return True
    state = _extended_state(uid)
    key = "%d:%d" % (_int(event_id), _int(gift_id))
    reward = _extended_claim(session, uid, state, "treasure_hunt", key, [(1, 100)])
    if reward is None:
        return False
    state["treasure_hunt"].setdefault("exchanges", []).append(key)
    _save(uid, "remaining_operations", state)
    _send(session, 6103, 0, _item_show(reward))
    return True


def _handle_survival_reward(session, uid, body, request_id, result_id):
    values = _decode_or_reject(request_id, body)
    if values is None:
        return False
    state = _extended_state(uid)
    data = state["survival"]
    pending_id = data.get("pending_battle_id")
    battle = storage.get_battle_instance(uid, pending_id) if pending_id else None
    expected_unlimited = request_id == 9406
    if (
        battle is None
        or battle.get("battle_type") != 2
        or battle.get("status") != "won"
        or bool(data.get("unlimited")) != expected_unlimited
    ):
        if request_id == 9403:
            _send(session, result_id, 1, [], 0, 0, 0, 0)
        else:
            _send(session, result_id, 1, 0, 0, 0, [])
        return True
    if any(_int(value) < 0 for value in values):
        return False
    score = _int(values[1] if request_id == 9403 and len(values) > 1 else values[0] if values else 0)
    key = "battle:%s:%d" % (battle["id"], request_id)
    reward = _extended_claim(session, uid, state, "survival", key, [(1, max(100, score))])
    if reward is None:
        return False
    data["active"] = False
    data["last"] = {"request": request_id, "values": values, "time": _stamp()}
    data.pop("pending_battle_id", None)
    data.setdefault("claims", []).append(key)
    _save(uid, "remaining_operations", state)
    if request_id == 9403:
        _send(session, result_id, 0, _item_show(reward), _int(values[1]), _int(values[2]), _int(values[3]), _int(values[0]))
    else:
        _send(session, result_id, 0, _int(values[0]), _int(values[1]), _item_show(reward))
    return True


def _handle_survival_start(session, uid, body, request_id, result_id, unlimited=False):
    values = _decode_or_reject(request_id, body)
    if values is None:
        return False
    level = _int(values[0]) if values else 1
    state = _extended_state(uid)
    data = state["survival"]
    pending_id = data.get("pending_battle_id")
    pending = storage.get_battle_instance(uid, pending_id) if pending_id else None
    if data.get("active") and pending is not None and pending.get("settled") and pending.get("status") != "won":
        data["active"] = False
        data.pop("pending_battle_id", None)
        _save(uid, "remaining_operations", state)
    if data.get("active") or level <= 0:
        _send(session, result_id, 1, 0) if request_id == 9402 else _send(session, result_id, 1)
        return True
    if not _start_extended_battle(session, uid, request_id, result_id, body, "survival", level):
        return False
    data["active"] = True
    data["unlimited"] = bool(unlimited)
    data["level"] = level
    _save(uid, "remaining_operations", state)
    return True


def handle_survival_start(session, uid, body):
    return _handle_survival_start(session, uid, body, 9402, 9407, False)


def handle_survival_start_unlimited(session, uid):
    return _handle_survival_start(session, uid, b"", 9405, 9410, True)


def handle_survival_level(session, uid):
    state = _extended_state(uid)
    _send(session, 9409, 0, _int(state["survival"].get("level"), 1))
    return True


def handle_survival_dialog(session, uid, body):
    values = _decode_or_reject(9412, body)
    if values is None:
        return False
    dialog, skip = values
    if _int(dialog) < 0 or not isinstance(skip, list) or len(skip) > 64 or not all(isinstance(value, int) and value >= 0 for value in skip):
        return False
    state = _extended_state(uid)
    state["survival"]["last_dialog"] = {"id": _int(dialog), "skip": list(skip), "time": _stamp()}
    _save(uid, "remaining_operations", state)
    _send(session, 9413, 0, -1)
    return True


def _home_pod(uid, data):
    player = storage.get_player(uid) or {}
    rooms = []
    for room in data.get("rooms", []):
        if not isinstance(room, dict):
            room = {
                "cid": int(room),
                "dbid": int(room),
                "name": "房间",
            }
        rooms.append(_home_room_pod(room))
    buildings = [
        _home_building_pod(building)
        for building in data.get("buildings", [])
        if isinstance(building, dict)
    ]
    return {
        "baseInfo": {
            "id": int(data.get("id") or player.get("role_id") or 0),
            "pname": str(player.get("role_name", "local")),
            "currentComfort": int(data.get("comfort", 0) or 0),
            "maxComfort": int(data.get("max_comfort", 0) or 0),
            "alreadyReward": list(data.get("already_reward", [])),
        },
        "rooms": rooms,
        "buildings": buildings,
        "roles": [
            _home_role_pod(role)
            for role in data.get("roles", [])
            if isinstance(role, dict)
        ],
        "activationDecorates": list(data.get("decorations", [])),
        "todayActions": list(data.get("today_actions", [])),
        "triggerDialogs": list(data.get("plots", [])),
        "unlockAIActions": list(data.get("unlock_ai_actions", [])),
        "unlockCookBook": list(data.get("unlock_cook_book", [])),
        "unlockManufactureItem": list(data.get("unlock_manufacture_items", [])),
        "unlockSuit": list(data.get("suits", [])),
        "visitTreasureChest": dict(data.get("visit_treasure_chest", {})),
    }


# ── Module: net_jewelry (灵装系统) ──

JEWELRY_DEFAULTS = {"items": [], "next_id": 1}

def handle_jewelry_wear(session, uid, body):
    try:
        role_id, jewel_id, slot = protocol_codec.decode_method(7702, body)
    except (ValueError, KeyError):
        return False
    state = _state(uid, "jewelry")
    for item in state.get("items", []):
        if item.get("id") == jewel_id:
            item["equipped_to"] = role_id
            item["slot"] = slot
            _save(uid, "jewelry", state)
            break
    session.send(7713, protocol_codec.encode_method(7713, 0, 0, 0, 0))
    return True

def handle_jewelry_unwear(session, uid, body):
    try:
        (jewel_id,) = protocol_codec.decode_method(7703, body)
    except (ValueError, KeyError):
        return False
    state = _state(uid, "jewelry")
    for item in state.get("items", []):
        if item.get("id") == jewel_id:
            item["equipped_to"] = None
            item["slot"] = 0
            _save(uid, "jewelry", state)
            break
    session.send(7714, protocol_codec.encode_method(7714, 0, 0))
    return True

def handle_jewelry_upstar(session, uid, body):
    try:
        (jewel_id,) = protocol_codec.decode_method(7704, body)
    except (ValueError, KeyError):
        return False
    state = _state(uid, "jewelry")
    for item in state.get("items", []):
        if item.get("id") == jewel_id:
            item["star"] = item.get("star", 0) + 1
            _save(uid, "jewelry", state)
            break
    session.send(7715, protocol_codec.encode_method(7715, 0, 0, 0))
    return True

def handle_jewelry_recycle(session, uid, body):
    try:
        jewel_id, count = protocol_codec.decode_method(7707, body)
    except (ValueError, KeyError):
        return False
    state = _state(uid, "jewelry")
    state["items"] = [i for i in state.get("items", []) if i.get("id") != jewel_id]
    _save(uid, "jewelry", state)
    session.send(7718, protocol_codec.encode_method(7718, 0, 0, []))
    return True

# ── Module: net_restaurant (餐厅经营) ──

try:
    RESTAURANT_CONFIG = json.loads(
        (Path(__file__).resolve().parent / "analysis" / "restaurant_config.json").read_text(
            encoding="utf-8"
        )
    )
except (OSError, ValueError, TypeError):
    RESTAURANT_CONFIG = {}

RESTAURANT_DEFAULTS = {
    "level": 1,
    "attrs": {"1": 3},
    "positions": [],
    "income": {},
    "plot": [],
    "dialog_id": 0,
    "events": [],
    "practice_count": 0,
    "returns": 0,
    "question": None,
    "question_number": 0,
    "answered_questions": [],
    "link_games": [],
    "memory_draws": [],
    "puzzle_score": 0,
}


def _restaurant_row(table, cid):
    row = RESTAURANT_CONFIG.get(table, {}).get(str(int(cid)))
    return row if isinstance(row, dict) else None


def _restaurant_control():
    return _restaurant_row("control", 1) or {}


def _restaurant_pair(value):
    if isinstance(value, list) and len(value) >= 2:
        try:
            cid, quantity = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
        if cid > 0 and quantity > 0:
            return cid, quantity
    return None


def _restaurant_init(session, uid):
    data = _init_state(uid, "restaurant", RESTAURANT_DEFAULTS)
    control = _restaurant_control()
    if not data.get("events"):
        data["events"] = [
            {"id": int(event_id), "status": False, "date": 0, "value": 0, "achievement": False}
            for event_id, row in RESTAURANT_CONFIG.get("events", {}).items()
            if int(row.get("Type", 0)) == 3
        ]
    if "props_initialized" not in data:
        initial = _restaurant_pair(control.get("InitialProps"))
        applied = storage.claim_reward_once(uid, "restaurant_claims", "initial_props", [initial] if initial else [])
        if applied is None:
            return None
        data["props_initialized"] = True
        _save(uid, "restaurant", data)
        if session is not None and applied.get("claimed"):
            _send_reward_changes(session, applied)
    return data


def _restaurant_position(data, player_id):
    return next(
        (row for row in data.get("positions", []) if int(row.get("id", 0)) == int(player_id)),
        None,
    )


def _restaurant_worker_config(player_id):
    row = _restaurant_row("players", player_id)
    return row if row and int(row.get("Type", 0)) == 1 else None


def _restaurant_post_list(data):
    level = int(data.get("level", 1))
    building = next(
        (row for row in RESTAURANT_CONFIG.get("buildings", {}).values() if int(row.get("Level", 0)) == level),
        None,
    )
    if not building:
        return []
    post_ids = building.get("PostId", [])
    post_nums = building.get("PostNum", [])
    result = []
    for index, post_id in enumerate(post_ids):
        control = _restaurant_row("posts", post_id) or {}
        max_num = int(post_nums[index]) if index < len(post_nums) else 0
        slots = {}
        value = 0
        for slot in range(1, max_num + 1):
            worker = next(
                (
                    row
                    for row in data.get("positions", [])
                    if int(row.get("postType", 0)) == int(post_id)
                    and int(row.get("location", 0)) == slot
                ),
                None,
            )
            if worker is None:
                pod = {"att": {}, "count": 0, "id": 0, "location": 0, "postType": int(post_id)}
            else:
                pod = {
                    "att": {int(k): int(v) for k, v in worker.get("att", {}).items()},
                    "count": int(worker.get("count", 1)),
                    "id": int(worker.get("id", 0)),
                    "location": slot,
                    "postType": int(post_id),
                }
                main = int(control.get("MainAtt", 0))
                value += int(worker.get("att", {}).get(str(main), 0))
            slots[slot] = pod
        result.append(
            {
                "number": max_num,
                "positionInformation": slots,
                "restaurantMarkUp": 0,
                "type": int(post_id),
                "value": value,
            }
        )
    return result


def _restaurant_attribute_pod(data):
    attrs = {int(k): int(v) for k, v in data.get("attrs", {}).items()}
    income = {int(k): float(v) for k, v in data.get("income", {}).items() if float(v) > 0}
    return {
        "allAtt": sum(attrs.values()),
        "dialogId": int(data.get("dialog_id", 0)),
        "income": income,
        "level": int(data.get("level", 1)),
        "plot": [int(value) for value in data.get("plot", [])],
        "positionInformation": [
            {
                "att": {int(k): int(v) for k, v in row.get("att", {}).items()},
                "count": int(row.get("count", 1)),
                "id": int(row.get("id", 0)),
                "location": int(row.get("location", 0)),
                "postType": int(row.get("postType", 0)),
            }
            for row in data.get("positions", [])
        ],
        "postInfo": _restaurant_post_list(data),
    }


def _restaurant_trade(session, uid, costs, rewards=()):
    result = storage.trade_reward_pairs(uid, list(costs or []), list(rewards or []))
    if result is None:
        return None
    _send_reward_changes(session, result)
    return result


def _restaurant_items(result):
    return list((result or {}).get("rewards", []))


def handle_restaurant_get_info(session, uid):
    data = _restaurant_init(session, uid)
    if data is None:
        return False
    session.send(9121, protocol_codec.encode_method(9121, 0, _restaurant_attribute_pod(data)))
    return True


def handle_restaurant_transact_documents(session, uid):
    data = _restaurant_init(session, uid)
    if data is None:
        return False
    control = _restaurant_control()
    building = next(
        (row for row in RESTAURANT_CONFIG.get("buildings", {}).values() if int(row.get("Level", 0)) == int(data.get("level", 1))),
        {},
    )
    if len(data.get("positions", [])) >= int(building.get("CreateMax", 0)):
        return False
    workers = sorted(
        int(cid) for cid, row in RESTAURANT_CONFIG.get("players", {}).items() if int(row.get("Type", 0)) == 1
    )
    player_id = next((cid for cid in workers if _restaurant_position(data, cid) is None), None)
    cost = _restaurant_pair(control.get("CreateCost"))
    if player_id is None or cost is None or _restaurant_trade(session, uid, [cost]) is None:
        return False
    data.setdefault("positions", []).append({"id": player_id, "att": {"1": 1}, "count": 1, "location": 0, "postType": 0})
    _save(uid, "restaurant", data)
    position = data["positions"][-1]
    pod = {"att": {1: 1}, "count": 1, "id": player_id, "location": 0, "postType": 0}
    session.send(9122, protocol_codec.encode_method(9122, 0, pod))
    return True


def handle_restaurant_practice(session, uid, body):
    try:
        (player_id,) = protocol_codec.decode_method(9104, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(session, uid)
    position = data and _restaurant_position(data, player_id)
    count = int(data.get("practice_count", 0)) if data else 0
    cost_row = _restaurant_row("modifyCosts", count + 1)
    cost = _restaurant_pair(cost_row.get("ModifyCost")) if cost_row else None
    if position is None or cost is None or _restaurant_trade(session, uid, [cost]) is None:
        return False
    before = {int(k): int(v) for k, v in position.get("att", {}).items()}
    increments = cost_row.get("ModifyAttNum", [1])
    increment = int(increments[count % len(increments)]) if increments else 1
    main_attr = min(before) if before else 1
    position.setdefault("att", {})[str(main_attr)] = before.get(main_attr, 0) + increment
    data["practice_count"] = count + 1
    _save(uid, "restaurant", data)
    pod = {"att": {int(k): int(v) for k, v in position["att"].items()}, "count": int(position.get("count", 1)), "id": int(player_id), "location": int(position.get("location", 0)), "postType": int(position.get("postType", 0))}
    session.send(9123, protocol_codec.encode_method(9123, 0, pod, before, _restaurant_attribute_pod(data)))
    return True


def handle_restaurant_level_up(session, uid):
    data = _restaurant_init(session, uid)
    if data is None:
        return False
    next_level = int(data.get("level", 1)) + 1
    building = next((row for row in RESTAURANT_CONFIG.get("buildings", {}).values() if int(row.get("Level", 0)) == next_level), None)
    if not building:
        return False
    attrs = data.get("attrs", {})
    required = building.get("LevelUpNeedAttType", [])
    needed = building.get("LevelUpNeedAttNum", [])
    if any(int(attrs.get(str(attr), 0)) < int(needed[index]) for index, attr in enumerate(required) if index < len(needed)):
        return False
    cost = _restaurant_pair(building.get("LevelUpCost"))
    if cost is None or _restaurant_trade(session, uid, [cost]) is None:
        return False
    for index, attr in enumerate(building.get("RestaurantAttType", [])):
        amount = int(building.get("RestaurantAttNum", [])[index]) if index < len(building.get("RestaurantAttNum", [])) else 0
        data.setdefault("attrs", {})[str(attr)] = max(int(data["attrs"].get(str(attr), 0)), amount)
    data["level"] = next_level
    _save(uid, "restaurant", data)
    session.send(9124, protocol_codec.encode_method(9124, 0, _restaurant_attribute_pod(data)))
    return True


def handle_restaurant_work(session, uid, body):
    try:
        player_id, post_id, location, state = protocol_codec.decode_method(9106, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(session, uid)
    position = data and _restaurant_position(data, player_id)
    post = _restaurant_row("posts", post_id)
    if position is None or post is None or int(location) <= 0 or int(state) not in (0, 1):
        return False
    posts = _restaurant_post_list(data)
    post_pod = next((row for row in posts if int(row["type"]) == int(post_id)), None)
    if post_pod is None or int(location) > int(post_pod["number"]):
        return False
    if state and int(data.get("returns", 0)) >= int(_restaurant_control().get("MaxTimes", 720)):
        return False
    occupied = next((row for row in data.get("positions", []) if int(row.get("postType", 0)) == int(post_id) and int(row.get("location", 0)) == int(location) and int(row.get("id", 0)) != int(player_id)), None)
    if occupied is not None:
        return False
    position["postType"] = int(post_id) if state else 0
    position["location"] = int(location) if state else 0
    if state:
        income = _restaurant_row("incomes", post.get("InComeID", 0)) or {}
        cid = int(income.get("IncomeItem", 0))
        main = str(post.get("MainAtt", 0))
        value = int(position.get("att", {}).get(main, 0))
        if cid > 0 and value > 0:
            data.setdefault("income", {})[str(cid)] = float(data.get("income", {}).get(str(cid), 0)) + value * float(income.get("Ratio", 0))
            data["returns"] = min(int(_restaurant_control().get("MaxTimes", 720)), int(data.get("returns", 0)) + 1)
    _save(uid, "restaurant", data)
    current = next(row for row in data["positions"] if int(row.get("id", 0)) == int(player_id))
    pod = {"att": {int(k): int(v) for k, v in current.get("att", {}).items()}, "count": int(current.get("count", 1)), "id": int(player_id), "location": int(current.get("location", 0)), "postType": int(current.get("postType", 0))}
    session.send(9125, protocol_codec.encode_method(9125, 0, _restaurant_post_list(data), pod, int(post_id), int(location)))
    return True


def handle_restaurant_receive_income(session, uid):
    data = _restaurant_init(session, uid)
    if data is None or not data.get("income"):
        return False
    rewards = []
    remainder = {}
    for cid, quantity in data.get("income", {}).items():
        whole = int(float(quantity))
        if whole > 0:
            rewards.append((int(cid), whole))
        fraction = float(quantity) - whole
        if fraction > 0:
            remainder[str(cid)] = fraction
    result = _restaurant_trade(session, uid, [], rewards)
    if result is None:
        return False
    data["income"] = remainder
    data["returns"] = 0
    _save(uid, "restaurant", data)
    session.send(9126, protocol_codec.encode_method(9126, 0, _restaurant_items(result), 0))
    return True


def handle_restaurant_read_burst(session, uid):
    data = _restaurant_init(session, uid)
    if data is None:
        return False
    for event in data.get("events", []):
        event["status"] = True
    _save(uid, "restaurant", data)
    session.send(9127, protocol_codec.encode_method(9127, 0))
    return True


def handle_restaurant_open_dialog(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(9109, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(session, uid)
    event = _restaurant_row("events", event_id)
    if data is None or event is None or int(event.get("Type", 0)) != 2:
        return False
    data["dialog_id"] = int(event.get("Parameter", 0))
    data["dialog_event"] = int(event_id)
    _save(uid, "restaurant", data)
    session.send(9128, protocol_codec.encode_method(9128, 0, data["dialog_id"]))
    return True


def handle_restaurant_select_dialog(session, uid, body):
    try:
        dialog_id, skips = protocol_codec.decode_method(9110, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    if data is None or int(data.get("dialog_id", 0)) != int(dialog_id):
        return False
    event_id = int(data.get("dialog_event", 0))
    if event_id and event_id not in data.setdefault("plot", []):
        data["plot"].append(event_id)
    data["dialog_id"] = 0
    data["dialog_event"] = 0
    _save(uid, "restaurant", data)
    session.send(9129, protocol_codec.encode_method(9129, 0, 0))
    return True


def _restaurant_question_pod(data):
    question = data.get("question") or {}
    return {
        "date": int(question.get("date", 0)),
        "id": int(question.get("id", 0)),
        "number": int(question.get("number", 0)),
        "rightNumber": int(question.get("rightNumber", 0)),
        "state": int(question.get("state", 0)),
    }


def handle_restaurant_get_problem(session, uid, body):
    try:
        (get_new,) = protocol_codec.decode_method(9111, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    if data is None:
        return False
    current = data.get("question")
    if get_new or not current or int(current.get("state", 0)) != 0:
        control = next(iter(RESTAURANT_CONFIG.get("answer", {}).values()), {})
        bank = control.get("QuestionBank", []) or [1]
        index = int(data.get("question_number", 0)) % len(bank)
        data["question_number"] = int(data.get("question_number", 0)) + 1
        data["question"] = {"id": int(bank[index]), "number": int(data["question_number"]), "rightNumber": 0, "state": 0, "date": _stamp()}
        _save(uid, "restaurant", data)
    session.send(9130, protocol_codec.encode_method(9130, 0, _restaurant_question_pod(data)))
    return True


def handle_restaurant_answer(session, uid, body):
    try:
        correct, question_id = protocol_codec.decode_method(9112, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    question = data and data.get("question")
    if question is None or int(question.get("id", 0)) != int(question_id) or int(question.get("state", 0)) != 0:
        return False
    question["state"] = 1 if correct else 2
    question["rightNumber"] = 1 if correct else 0
    rewards = []
    if correct and int(question_id) not in [int(value) for value in data.get("answered_questions", [])]:
        control = next(iter(RESTAURANT_CONFIG.get("answer", {}).values()), {})
        reward = _restaurant_pair(control.get("Reward"))
        if reward:
            result = _restaurant_trade(session, uid, [], [reward])
            if result is None:
                return False
            rewards = _restaurant_items(result)
        data.setdefault("answered_questions", []).append(int(question_id))
    _save(uid, "restaurant", data)
    session.send(9131, protocol_codec.encode_method(9131, 0, _restaurant_question_pod(data), rewards))
    return True


def _restaurant_links(data):
    return [{"id": int(row.get("id", 0)), "time": int(row.get("time", 0))} for row in data.get("link_games", [])]


def handle_restaurant_link_game_info(session, uid):
    data = _restaurant_init(uid=uid, session=session)
    if data is None:
        return False
    session.send(9132, protocol_codec.encode_method(9132, 0, _restaurant_links(data)))
    return True


def handle_restaurant_link_game(session, uid, body):
    try:
        level_id, duration = protocol_codec.decode_method(9114, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    config = _restaurant_row("fruitClean", level_id)
    if data is None or config is None or int(duration) <= 0 or int(duration) > int(config.get("Countdown", duration)):
        return False
    if any(int(row.get("id", 0)) == int(level_id) for row in data.get("link_games", [])):
        return False
    data.setdefault("link_games", []).append({"id": int(level_id), "time": int(duration)})
    data["link_games"] = sorted(data["link_games"], key=lambda row: int(row.get("id", 0)))
    _save(uid, "restaurant", data)
    session.send(9133, protocol_codec.encode_method(9133, 0, _restaurant_links(data)))
    return True


def handle_restaurant_puzzle_info(session, uid):
    data = _restaurant_init(uid=uid, session=session)
    if data is None:
        return False
    session.send(9134, protocol_codec.encode_method(9134, 0, int(data.get("puzzle_score", 0))))
    return True


def handle_restaurant_puzzle(session, uid, body):
    try:
        (score,) = protocol_codec.decode_method(9116, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    control = next(iter(RESTAURANT_CONFIG.get("puzzle", {}).values()), {})
    max_score = max([int(value) for value in control.get("Score", [])] or [0])
    if data is None or int(score) < 0 or int(score) > max_score:
        return False
    data["puzzle_score"] = max(int(data.get("puzzle_score", 0)), int(score))
    _save(uid, "restaurant", data)
    session.send(9135, protocol_codec.encode_method(9135, 0, int(data["puzzle_score"])))
    return True


def _restaurant_draws(data):
    return [{"cumulativeSteps": int(row.get("cumulativeSteps", 0)), "id": int(row.get("id", 0)), "time": int(row.get("time", 0))} for row in data.get("memory_draws", [])]


def handle_restaurant_memory_flop_info(session, uid):
    data = _restaurant_init(uid=uid, session=session)
    if data is None:
        return False
    session.send(9136, protocol_codec.encode_method(9136, 0, _restaurant_draws(data)))
    return True


def handle_restaurant_memory_flop(session, uid, body):
    try:
        level_id, steps, duration = protocol_codec.decode_method(9118, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    config = _restaurant_row("memoryCards", level_id)
    if data is None or config is None or int(steps) <= 0 or int(duration) <= 0 or int(duration) > int(config.get("Countdown", duration)):
        return False
    if any(int(row.get("id", 0)) == int(level_id) for row in data.get("memory_draws", [])):
        return False
    prior = sum(int(row.get("cumulativeSteps", 0)) for row in data.get("memory_draws", []))
    data.setdefault("memory_draws", []).append({"id": int(level_id), "cumulativeSteps": prior + int(steps), "time": int(duration)})
    data["memory_draws"] = sorted(data["memory_draws"], key=lambda row: int(row.get("id", 0)))
    _save(uid, "restaurant", data)
    session.send(9137, protocol_codec.encode_method(9137, 0, _restaurant_draws(data)))
    return True


def handle_restaurant_combat_training(session, uid, body):
    try:
        boss_id, formation_id = protocol_codec.decode_method(9119, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    monster = _restaurant_row("monsters", boss_id)
    if data is None or monster is None:
        return False
    reward = monster.get("Reward", [])
    reward_pairs = []
    if isinstance(reward, list) and len(reward) >= 2:
        try:
            reward_pairs = [(int(reward[0]), int(reward[1]))]
        except (TypeError, ValueError):
            reward_pairs = []
    if not module_rules._start_module_battle(
        session, uid, "restaurant", boss_id, int(boss_id),
        int(monster.get("MonsterTeam", 0)), reward_pairs, battle_type=4,
    ):
        return False
    session.send(9138, protocol_codec.encode_method(9138, 0))
    return True


def handle_restaurant_boss_training(session, uid, body):
    try:
        (formation_id,) = protocol_codec.decode_method(9120, body)
    except (ValueError, KeyError):
        return False
    data = _restaurant_init(uid=uid, session=session)
    control = _restaurant_control()
    if data is None or int(control.get("BossMapID", 0)) <= 0 or int(control.get("BossTeam", 0)) <= 0:
        return False
    if not module_rules._start_module_battle(
        session, uid, "restaurant_boss", 0, int(control["BossMapID"]),
        int(control["BossTeam"]), [], battle_type=4,
    ):
        return False
    session.send(9139, protocol_codec.encode_method(9139, 0))
    return True

# ── Module: net_amusementPark (游乐园) ──

AMUSEMENT_DEFAULTS = {"level": 1, "funds": 500, "attractions": [], "roles": []}

def handle_amusement_get_info(session, uid):
    data = _init_state(uid, "amusement_park", AMUSEMENT_DEFAULTS)
    pod = {
        "amusementParkAttPOD": {"attr": {1: int(data.get("funds", 500))}, "level": int(data.get("level", 1)), "number": 0},
        "amusementParkVoRoles": [], "amusementParkVoRolesHave": [], "boss": {},
        "dialogId": 0, "plot": [], "postList": [], "unitList": [],
    }
    session.send(9330, protocol_codec.encode_method(9330, 0, pod))
    return True

def handle_amusement_build(session, uid, body):
    try:
        land_id, building_id = protocol_codec.decode_method(9304, body)
    except (ValueError, KeyError):
        return False
    data = _state(uid, "amusement_park")
    if data.get("funds", 500) < 100:
        return False
    data["funds"] = data.get("funds", 500) - 100
    data.setdefault("attractions", []).append({"land": int(land_id), "building": int(building_id), "built_at": _stamp()})
    _save(uid, "amusement_park", data)
    session.send(9332, protocol_codec.encode_method(9332, 0, data["funds"]))
    return True

# ── Module: net_centerGuild (公会) ──

GUILD_DEFAULTS = {
    "guild_id": 0, "guild_name": "", "members": [], "level": 1, "fund": 0,
    "notice": "", "policy": 0, "applications": [], "audits": [],
    "buildings": {}, "quest_progress": {}, "training": [], "impeachment": 0,
}


def _guild_state(uid):
    return _init_state(uid, "guild", GUILD_DEFAULTS)


def _guild_base(data):
    if not data:
        return {
            "id": 0, "name": "", "leaderName": "", "level": 0,
            "memberNum": 0, "memberMaxNum": 50, "policy": 0,
            "auditType": 0, "headIcon": 0, "avatarFrame": 0,
        }
    return {
        "id": _int(data.get("id")), "name": str(data.get("name", "")),
        "leaderName": str(data.get("leader_name", "")), "level": _int(data.get("level"), 1),
        "memberNum": _int(data.get("member_num"), len(storage.guild_members(data.get("id", 0)))),
        "memberMaxNum": 50, "policy": _int(data.get("policy")),
        "auditType": _int(data.get("audit_type")),
        "headIcon": _int(data.get("head_icon")), "avatarFrame": _int(data.get("avatar_frame")),
    }


def _guild_player(uid, member=None):
    player = storage.get_player(uid) or {}
    member = member or {}
    return {
        "pid": uid, "pName": str(member.get("name", player.get("role_name", "local"))),
        "pLv": int(member.get("level", player.get("level", 1))),
        "guildId": _int(member.get("guild_id", member.get("guildId", 0))),
        "headIcon": 0, "avatarFrame": 0, "serverId": "local",
    }


def _guild_member_pod(uid, member):
    return {
        "player": _guild_player(uid, member), "position": int(member.get("position", 0)),
        "activeNum": _int(member.get("active_num", member.get("activeNum", 0))),
        "joinTime": _int(member.get("join_time", member.get("joinTime", _stamp()))),
        "lastLoginTime": _int(member.get("last_login_time", member.get("lastLoginTime", _stamp()))),
        "online": True,
    }


def _guild_pod(uid, data):
    if not data:
        return {
            "base": _guild_base(None), "fund": 0, "fundDailyGetRecord": 0,
            "impeachmentTime": 0, "notice": "", "banNotice": False,
            "newAuditCount": 0, "members": [], "buildings": [], "questProgress": {},
        }
    members = storage.guild_members(data["id"])
    audits = storage.guild_applications(data["id"])
    return {
        "base": _guild_base(data), "fund": _int(data.get("fund")),
        "fundDailyGetRecord": 0, "impeachmentTime": _int(data.get("impeachment_time")),
        "notice": str(data.get("notice", "")), "banNotice": False,
        "newAuditCount": len(audits),
        "members": [_guild_member_pod(member.get("uid", uid), {**member, "guild_id": data["id"]}) for member in members],
        "buildings": storage.guild_buildings(data["id"]),
        "questProgress": {int(cid): int(value) for cid, value in (data.get("quest_progress") or {}).items()},
    }

def handle_guild_create(session, uid, body):
    try:
        (name,) = protocol_codec.decode_method(100902, body)
    except (ValueError, KeyError):
        return False
    code = 0 if storage.guild_create(uid, name) else 1
    session.send(100922, protocol_codec.encode_method(100922, code))
    return True

def handle_guild_enter(session, uid):
    data = storage.guild_for_uid(uid)
    if data is None:
        session.send(100923, protocol_codec.encode_method(100923, 1, _guild_pod(uid, None)))
        return True
    pod = _guild_pod(uid, data)
    session.send(100923, protocol_codec.encode_method(100923, 0, pod))
    return True


def handle_guild_exit(session, uid):
    session.send(100924, protocol_codec.encode_method(100924, 0 if storage.guild_leave(uid) else 1))
    return True


def handle_guild_recommend(session, uid):
    bases = [_guild_base(row) for row in storage.guild_list("", 50)]
    session.send(100925, protocol_codec.encode_method(100925, 0, bases))
    return True


def handle_guild_apply(session, uid, body):
    try:
        (guild_id,) = protocol_codec.decode_method(100906, body)
    except (ValueError, KeyError):
        return False
    session.send(100926, protocol_codec.encode_method(100926, 0 if storage.guild_apply(uid, guild_id) else 1))
    return True


def handle_guild_my_apply(session, uid):
    bases = [_guild_base(row) for row in storage.guild_applications_for_uid(uid)]
    session.send(100927, protocol_codec.encode_method(100927, 0, bases))
    return True


def handle_guild_cancel_apply(session, uid, body):
    try:
        (guild_id,) = protocol_codec.decode_method(100908, body)
    except (ValueError, KeyError):
        return False
    ok = storage.guild_cancel_apply(uid, guild_id)
    session.send(100928, protocol_codec.encode_method(100928, 0 if ok else 1, guild_id))
    return True


def handle_guild_audit_list(session, uid):
    data = storage.guild_for_uid(uid)
    audits = [] if data is None else [
        {"player": _guild_player(item.get("uid", uid), {**item, "guild_id": data["id"]}), "online": True,
         "lastLoginTime": _int(item.get("created_at"), _stamp())}
        for item in storage.guild_applications(data["id"])
    ]
    session.send(100929, protocol_codec.encode_method(100929, 0, audits))
    return True


def handle_guild_refuse_apply(session, uid, body):
    try:
        (uids,) = protocol_codec.decode_method(100910, body)
    except (ValueError, KeyError):
        return False
    rejected = [str(value) for value in uids]
    ok = storage.guild_refuse(uid, rejected)
    session.send(100930, protocol_codec.encode_method(100930, 0 if ok else 1, rejected))
    return True


def handle_guild_accept_apply(session, uid, body):
    try:
        (member_uid,) = protocol_codec.decode_method(100911, body)
    except (ValueError, KeyError):
        return False
    ok = storage.guild_accept(uid, member_uid)
    session.send(100931, protocol_codec.encode_method(100931, 0 if ok else 1, member_uid))
    return True


def handle_guild_members(session, uid):
    data = storage.guild_for_uid(uid)
    members = [] if data is None else [
        _guild_member_pod(item.get("uid", uid), {**item, "guild_id": data["id"]})
        for item in storage.guild_members(data["id"])
    ]
    session.send(100932, protocol_codec.encode_method(100932, 0, members))
    return True


def handle_guild_appoint(session, uid, body):
    try:
        member_uid, position = protocol_codec.decode_method(100913, body)
    except (ValueError, KeyError):
        return False
    session.send(100933, protocol_codec.encode_method(100933, 0 if storage.guild_set_position(uid, member_uid, position) else 1))
    return True


def handle_guild_remove_member(session, uid, body):
    try:
        (member_uid,) = protocol_codec.decode_method(100914, body)
    except (ValueError, KeyError):
        return False
    session.send(100934, protocol_codec.encode_method(100934, 0 if storage.guild_remove(uid, member_uid) else 1))
    return True


def handle_guild_impeachment(session, uid, cancel=False):
    ok = storage.guild_impeachment(uid, cancel) is not None
    result_id = 100936 if cancel else 100935
    session.send(result_id, protocol_codec.encode_method(result_id, 0 if ok else 1))
    return True


def handle_guild_quit(session, uid):
    session.send(100937, protocol_codec.encode_method(100937, 0 if storage.guild_leave(uid) else 1))
    return True


def handle_guild_edit_info(session, uid, body):
    try:
        avatar, head, policy, audit = protocol_codec.decode_method(100918, body)
    except (ValueError, KeyError):
        return False
    ok = storage.guild_update(uid, avatar_frame=avatar, head_icon=head, policy=policy, audit_type=audit)
    session.send(100938, protocol_codec.encode_method(100938, 0 if ok else 1))
    return True


def handle_guild_change_name(session, uid, body):
    try:
        (name,) = protocol_codec.decode_method(100919, body)
    except (ValueError, KeyError):
        return False
    ok = storage.guild_update(uid, name=name)
    session.send(100939, protocol_codec.encode_method(100939, 0 if ok else 1))
    return True

def handle_guild_search(session, uid, body):
    try:
        (query,) = protocol_codec.decode_method(100920, body)
    except (ValueError, KeyError):
        return False
    bases = [_guild_base(row) for row in storage.guild_list(query, 50)]
    session.send(100940, protocol_codec.encode_method(100940, 0, bases))
    return True


def handle_guild_training(session, uid):
    data = storage.guild_for_uid(uid)
    rows = [] if data is None else [
        {"pid": str(item.get("uid", uid)), "name": str(item.get("role_name", "local")),
         "score": _int(item.get("active_num")), "createTime": int(item.get("join_time", _stamp()))}
        for item in storage.guild_members(data["id"])
    ]
    session.send(100941, protocol_codec.encode_method(100941, 0, rows))
    return True


def handle_guild_up_building(session, uid, body):
    try:
        (building_id,) = protocol_codec.decode_method(100947, body)
    except (ValueError, KeyError):
        return False
    pod = storage.guild_building_update(uid, building_id)
    session.send(100949, protocol_codec.encode_method(100949, 0 if pod else 1, pod or {}))
    return True


def handle_guild_buy_building_effect(session, uid, body):
    try:
        (building_id,) = protocol_codec.decode_method(100948, body)
    except (ValueError, KeyError):
        return False
    pod = storage.guild_building_update(uid, building_id, effect=True)
    session.send(100950, protocol_codec.encode_method(100950, 0 if pod else 1, pod or {}))
    return True


def handle_guild_quest_progress(session, uid):
    data = storage.guild_for_uid(uid)
    progress = {} if data is None else {int(cid): int(value) for cid, value in (data.get("quest_progress") or {}).items()}
    session.send(100952, protocol_codec.encode_method(100952, 0, progress))
    return True


def handle_guild_edit_notice(session, uid, body):
    try:
        (notice,) = protocol_codec.decode_method(100954, body)
    except (ValueError, KeyError):
        return False
    ok = storage.guild_update(uid, notice=notice)
    session.send(100955, protocol_codec.encode_method(100955, 0 if ok else 1, notice))
    return True

# ── Module: net_placeGame (布阵战棋) ──

PLACE_DEFAULTS = {"units": [], "formations": [], "level": 1}

def handle_place_get_info(session, uid):
    data = _init_state(uid, "place_game", PLACE_DEFAULTS)
    unit = data.get("units", [{}])[0] if data.get("units") else {"cid": 0, "level": int(data.get("level", 1)), "experience": 0}
    session.send(9221, protocol_codec.encode_method(9221, 0, unit))
    return True

def handle_place_recruit(session, uid, body):
    try:
        (recruit_type,) = protocol_codec.decode_method(9202, body)
    except (ValueError, KeyError):
        return False
    data = _state(uid, "place_game")
    new_unit = {"id": data.get("next_id", 1), "type": recruit_type, "level": 1}
    data["next_id"] = data.get("next_id", 1) + 1
    data.setdefault("units", []).append(new_unit)
    _save(uid, "place_game", data)
    soul_pod = {"cid": int(recruit_type), "level": 1, "experience": 0}
    session.send(9220, protocol_codec.encode_method(9220, 0, soul_pod, [soul_pod]))
    return True

# ── Module: net_miniGalGame (小游戏) ──

MINIGAL_DEFAULTS = {"saves": {}, "flags": {}}

def handle_minigal_start(session, uid):
    data = _init_state(uid, "mini_gal", MINIGAL_DEFAULTS)
    save_id = len(data.get("saves", {})) + 1
    data.setdefault("saves", {})[save_id] = {"chapter": 1, "flags": {}}
    _save(uid, "mini_gal", data)
    pod = {"saveId": save_id, "chapter": 1, "flags": {}}
    session.send(6812, protocol_codec.encode_method(6812, 0, pod))
    return True

def handle_minigal_select_dialog(session, uid, body):
    try:
        dialog_id, choices = protocol_codec.decode_method(6805, body)
    except (ValueError, KeyError):
        return False
    data = _state(uid, "mini_gal")
    data.setdefault("flags", {})[f"dialog_{dialog_id}"] = choices
    _save(uid, "mini_gal", data)
    session.send(6815, protocol_codec.encode_method(6815, 0, dialog_id))
    return True

# ── Module: net_evilErosion (心之裂痕) ──

def handle_evil_get_daily_supply(session, uid):
    day = time.strftime("%Y-%m-%d", time.localtime())
    state = _state(uid, "evil_erosion")
    if state.get("supply_day") == day:
        session.send(6921, protocol_codec.encode_method(6921, 1, []))
        return True
    applied = _grant_rewards(session, uid, [(1, 200)])
    if applied is None:
        return False
    state["supply_day"] = day
    _save(uid, "evil_erosion", state)
    session.send(6921, protocol_codec.encode_method(6921, 0, applied.get("rewards", [])))
    return True

# ── Module: net_horizontalRPG (横版RPG) ──

def handle_horizontal_quick_challenge(session, uid, body):
    try:
        maze_cid, count = protocol_codec.decode_method(9508, body)
    except (ValueError, KeyError):
        return False
    if maze_cid <= 0 or count <= 0 or count > 99:
        return False
    applied = _grant_rewards(session, uid, [(1, count * 100)])
    if applied is None:
        return False
    session.send(9515, protocol_codec.encode_method(9515, 0, applied.get("rewards", [])))
    return True

# ── Module: net_dualTeamExplore (双队探索) ──

DUAL_DEFAULTS = {"progress": {}, "items": []}

def handle_dual_enter(session, uid, body):
    try:
        maze_cid, formation_a, formation_b, flag = protocol_codec.decode_method(6509, body)
    except (ValueError, KeyError):
        return False
    empty_team = {"currNodeId": 0, "dead": False, "formationInfo": {}, "stop": False}
    pod = {
        "currDialog": 0, "currFightMonsterTeamId": 0, "currMazeCid": int(maze_cid),
        "currNumberInputId": 0, "currTeam": 1, "currTransportNodeId": 0,
        "easyMode": bool(flag), "levelCid": int(maze_cid), "nodes": [],
        "separate": False, "team1": empty_team, "team2": empty_team,
    }
    session.send(6511, protocol_codec.encode_method(6511, 0, pod))
    return True

def handle_dual_fight(session, uid, body):
    try:
        (fight_type,) = protocol_codec.decode_method(6523, body)
    except (ValueError, KeyError):
        return False
    applied = _grant_rewards(session, uid, [(1, 50)])
    if applied is None:
        return False
    session.send(6524, protocol_codec.encode_method(6524, 0))
    log.info("  dual explore fight uid=%s type=%d -> 6524", uid, fight_type)
    return True

# ── Module: net_home (家园系统) ──

HOME_DEFAULTS = {
    "level": 1, "comfort": 0, "max_comfort": 0, "rooms": [], "buildings": [],
    "lands": [], "suits": [], "decorations": [], "roles": [],
    "already_reward": [], "plots": [], "today_home_work_count": 0,
}

HOME_ROLE_LIMIT = 55
HOME_DEFAULT_BELONG_ROOM = 0

INITIAL_HOME_ROOMS = (
    (1, 10100), (2, 1007), (3, 20400), (4, 30400),
    (5, 1007), (6, 40400), (7, 1006), (8, 50400),
    (9, 60400), (10, 1006), (11, 70400), (12, 80400),
    (13, 1007), (14, 90400), (15, 110400), (16, 120400),
    (17, 1006), (18, 130400), (19, 100100),
)


def _initial_home_room(room_id, suit_id):
    return {
        "cid": int(room_id),
        "dbid": int(room_id),
        "comfort": 0,
        "decorates": [],
        "suitCid": int(suit_id),
        "name": "房间",
        "foreignShow": False,
        "receiveComfortAwards": False,
    }


def _home_config_row(table_name, key):
    snapshot_table = {
        "HomeLandRoomTable": "rooms",
        "HomeLandPlantGridTable": "plantGrids",
    }.get(table_name)
    if snapshot_table:
        row = (HOME_CONFIG.get(snapshot_table) or {}).get(str(int(key)))
        if isinstance(row, dict):
            return row
    row = module_rules._row("homeland", table_name, key)
    return row if isinstance(row, dict) else None


def _home_default_suit(room_id):
    """Return the official default theme for a room, if the room is known."""
    row = _home_config_row("HomeLandRoomTable", room_id)
    return _int(row.get("DefaultSuit"), 0) if isinstance(row, dict) else 0


def _home_is_dorm(room_id):
    row = _home_config_row("HomeLandRoomTable", room_id)
    return isinstance(row, dict) and _int(row.get("CastleIndex"), 0) == 2


def _home_default_layout(suit_id):
    """Build the official wall/floor/furniture layout for one dorm suit."""
    suit = (HOME_CONFIG.get("decorateSuits") or {}).get(str(int(suit_id)))
    if not isinstance(suit, dict):
        return []
    layout = []
    for key in ("FloorResUnlocked", "WallResUnlocked"):
        cid = _int(suit.get(key), 0)
        if cid > 0:
            layout.append({"cid": cid, "x": 0, "y": 0})
    furniture = suit.get("FurnitureIDList")
    if isinstance(furniture, list):
        for index in range(0, len(furniture) - 2, 3):
            cid = _int(furniture[index], 0)
            if cid <= 0:
                continue
            layout.append({
                "cid": cid,
                "x": _int(furniture[index + 1], 0),
                "y": _int(furniture[index + 2], 0),
            })
    return layout


def _home_apply_room_default_layout(room):
    """Seed a legacy/new dorm once, without replacing user-owned furniture."""
    if not isinstance(room, dict):
        return False
    room_id = _int(room.get("cid", room.get("id", 0)), 0)
    if room_id <= 0 or not _home_is_dorm(room_id):
        return False
    changed = False
    if not isinstance(room.get("decorates"), list):
        room["decorates"] = []
        changed = True
    if bool(room.get("default_layout_initialized")):
        return changed
    if room["decorates"]:
        room["default_layout_initialized"] = True
        return True
    default_suit = _home_default_suit(room_id)
    if default_suit > 0 and _int(room.get("suitCid"), 0) <= 0:
        room["suitCid"] = default_suit
        changed = True
    suit_id = _int(room.get("suitCid"), 0)
    layout = _home_default_layout(suit_id)
    if not layout:
        return changed
    room["decorates"] = layout
    room["default_layout_initialized"] = True
    return True


def _home_migrate_room_defaults(data):
    """Repair legacy room records without overwriting user-owned fields."""
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        return False
    changed = False
    normalized = []
    for value in data["rooms"]:
        if isinstance(value, dict):
            room = value
            room_id = _int(room.get("cid", room.get("id", 0)), 0)
        else:
            room_id = _int(value, 0)
            room = None
        if room_id <= 0:
            normalized.append(value)
            continue
        if room is None:
            room = _initial_home_room(room_id, _home_default_suit(room_id))
            changed = True
        if _int(room.get("cid"), 0) != room_id:
            room["cid"] = room_id
            changed = True
        if _int(room.get("dbid"), 0) <= 0:
            room["dbid"] = room_id
            changed = True
        default_suit = _home_default_suit(room_id)
        if default_suit > 0 and _int(room.get("suitCid"), 0) <= 0:
            room["suitCid"] = default_suit
            changed = True
        if not isinstance(room.get("decorates"), list):
            room["decorates"] = []
            changed = True
        if _home_apply_room_default_layout(room):
            changed = True
        normalized.append(room)
    placed_decorations = {
        _int(decorate.get("cid"), 0)
        for room in normalized
        if isinstance(room, dict)
        for decorate in room.get("decorates", [])
        if isinstance(decorate, dict) and _int(decorate.get("cid"), 0) > 0
    }
    active_decorations = set(_safe_positive_int_list(data.get("decorations", [])))
    merged_decorations = sorted(active_decorations | placed_decorations)
    if merged_decorations != data.get("decorations", []):
        data["decorations"] = merged_decorations
        changed = True
    if changed:
        data["rooms"] = normalized
    return changed


def _home_condition_satisfied(uid, condition_id):
    condition_id = int(condition_id or 0)
    if condition_id <= 0:
        return True
    row = (HOME_CONFIG.get("referencedConditions") or {}).get(str(condition_id))
    if not isinstance(row, dict):
        return False
    return _condition_satisfied(uid, row)


def _condition_compare(value, target, comparison, collection=False):
    comparison = str(comparison or "")
    if collection:
        values = set(_safe_positive_int_list(value))
        if comparison == "==":
            return int(target) in values
        if comparison == "!=":
            return int(target) not in values
        return False
    try:
        value = int(value)
        target = int(target)
    except (TypeError, ValueError):
        return False
    return {
        ">=": value >= target,
        "<=": value <= target,
        ">": value > target,
        "<": value < target,
        "==": value == target,
        "!=": value != target,
    }.get(comparison, False)


def _condition_clause_value(uid, condition_type, subtype, params, target):
    """Resolve the small set of player-backed condition fields we persist."""
    if condition_type != 1:
        if condition_type == 2 and subtype == 1:
            item_id = _int(params[0], 0) if params else 0
            if item_id <= 0:
                return None, False
            if storage._ITEM_TYPE_BY_CID.get(item_id) == 6:
                return storage.get_player_num_attrs(uid).get(item_id, 0), False
            quantity = next(
                (
                    _int(row.get("quantity"), 0)
                    for row in storage.get_items(uid)
                    if _int(row.get("template_id"), 0) == item_id
                ),
                0,
            )
            return quantity, False
        return None, False
    if subtype == 1:
        player = storage.get_player(uid) or {}
        return _int(player.get("level"), 0), False
    if subtype == 3:
        return _read_state_json(uid, "quickChallenge"), True
    if subtype == 19:
        return _read_state_json(uid, "unlockTownEvents"), True
    if subtype == 13:
        player_params = _read_state_json(uid, "playerParams")
        if not isinstance(player_params, dict) or not isinstance(params, list):
            return None, False
        total = 0
        for param_id in params:
            param_id = _int(param_id, 0)
            if param_id <= 0:
                return None, False
            raw_value = player_params.get(str(param_id), player_params.get(param_id))
            if raw_value is None:
                return None, False
            total += _int(raw_value, 0)
        return total, False
    return None, False


def _condition_satisfied(uid, row):
    """Evaluate persisted player conditions and fail closed on unknown data."""
    if not isinstance(row, dict):
        return False
    types = row.get("Type") if isinstance(row.get("Type"), list) else []
    subtypes = row.get("SubType") if isinstance(row.get("SubType"), list) else []
    params = row.get("Params") if isinstance(row.get("Params"), list) else []
    comparisons = row.get("ComparisonOP") if isinstance(row.get("ComparisonOP"), list) else []
    values = row.get("Value") if isinstance(row.get("Value"), list) else []
    logical = row.get("LogicalOP") if isinstance(row.get("LogicalOP"), list) else []
    results = []
    indexes = []
    for index, raw_type in enumerate(types):
        condition_type = _int(raw_type, 0)
        if condition_type <= 0:
            continue
        subtype = _int(subtypes[index] if index < len(subtypes) else 0, 0)
        clause_params = params[index] if index < len(params) else []
        target = _int(values[index] if index < len(values) else 0, 0)
        current, collection = _condition_clause_value(
            uid, condition_type, subtype, clause_params, target,
        )
        if current is None:
            return False
        comparison = comparisons[index] if index < len(comparisons) else ""
        results.append(_condition_compare(current, target, comparison, collection))
        indexes.append(index)
    if not results:
        return False
    result = results[0]
    for result_index in range(1, len(results)):
        logical_index = indexes[result_index - 1]
        operation = str(logical[logical_index] if logical_index < len(logical) else "").lower()
        if operation == "and":
            result = result and results[result_index]
        elif operation == "or":
            result = result or results[result_index]
        else:
            return False
    return result


def _home_recalculate_comfort(data):
    """Recompute room/current comfort from placed furniture and theme counts."""
    if not isinstance(data, dict):
        return False
    before = json.dumps(
        {
            "comfort": data.get("comfort"),
            "max_comfort": data.get("max_comfort"),
            "rooms": data.get("rooms"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    total = 0
    for room in data.get("rooms", []):
        if not isinstance(room, dict):
            continue
        base = 0
        theme_counts = {}
        for decorate in room.get("decorates", []):
            if not isinstance(decorate, dict):
                continue
            cid = int(decorate.get("cid", 0) or 0)
            if cid <= 0:
                continue
            config = (HOME_CONFIG.get("decorates") or {}).get(str(cid))
            if not isinstance(config, dict):
                continue
            base += int(config.get("Score", 0) or 0)
            theme_id = int(config.get("ThemeID", 0) or 0)
            if theme_id > 0:
                theme_counts[theme_id] = theme_counts.get(theme_id, 0) + 1
        theme_bonus = 0
        for theme_id, count in theme_counts.items():
            theme_score = 0
            for threshold, score in HOME_THEME_SCORES.get(theme_id, []):
                if count >= int(threshold):
                    theme_score = max(theme_score, int(score))
            theme_bonus += theme_score
        room["comfort"] = base + theme_bonus
        total += int(room["comfort"])
    data["comfort"] = int(total)
    data["max_comfort"] = max(int(data.get("max_comfort", 0) or 0), int(total))
    after = json.dumps(
        {
            "comfort": data.get("comfort"),
            "max_comfort": data.get("max_comfort"),
            "rooms": data.get("rooms"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return before != after


def _home_basic_info_pod(data, uid):
    player = storage.get_player(uid) or {}
    return {
        "id": int(data.get("id") or player.get("role_id") or 0),
        "pname": str(player.get("role_name", "local")),
        "currentComfort": int(data.get("comfort", 0) or 0),
        "maxComfort": int(data.get("max_comfort", 0) or 0),
        "alreadyReward": [int(value) for value in data.get("already_reward", [])],
    }


def _home_building_level_rule(building_id, level):
    for row in module_rules._table("homeland", "HomeLandBuildingLevelUpTable").values():
        if (
            isinstance(row, dict)
            and int(row.get("BuildingId", 0)) == int(building_id)
            and int(row.get("Level", 0)) == int(level)
        ):
            return row
    return None


def _apply_home_building_level_effect(building, rule):
    if not isinstance(building, dict) or not isinstance(rule, dict):
        return
    types = rule.get("Type", [])
    params = rule.get("Params", [])
    for index, effect_type in enumerate(types if isinstance(types, list) else []):
        values = params[index] if isinstance(params, list) and index < len(params) else []
        value = int(values[0]) if isinstance(values, list) and values else 0
        if int(effect_type) == 6 and isinstance(building.get("manufacture"), dict) and value > 0:
            building["manufacture"]["maxQueueCount"] = value
        elif int(effect_type) == 12 and isinstance(building.get("kitchenPOD"), dict) and value > 0:
            building["kitchenPOD"]["maxQueueCount"] = value
        elif int(effect_type) == 16 and isinstance(building.get("officePOD"), dict) and value > 0:
            office = building["officePOD"]
            affairs = office.setdefault("affairs", [])
            transaction_ids = sorted(
                int(key) for key, row in module_rules._table(
                    "homeland", "TransactionListTable",
                ).items() if isinstance(row, dict) and int(key) > 0
            )
            while len(affairs) < value and transaction_ids:
                affair_id = len(affairs) + 1
                affairs.append(_new_home_affair(
                    affair_id, transaction_ids[(affair_id - 1) % len(transaction_ids)],
                ))


def _home_affair_pod(affair):
    affair = affair or {}
    return {
        "id": int(affair.get("id", 0)),
        "cid": int(affair.get("cid", 0)),
        "status": int(affair.get("status", 0)),
        "finishTime": int(affair.get("finishTime", 0)),
        "soulCids": [int(value) for value in affair.get("soulCids", [])],
        "events": [
            {
                "cid": int(event.get("cid", 0)),
                "time": int(event.get("time", 0)),
                "soulCid": int(event.get("soulCid", 0)),
            }
            for event in affair.get("events", [])
            if isinstance(event, dict)
        ],
    }


def _new_home_affair(affair_id, transaction_cid):
    return {
        "id": int(affair_id),
        "cid": int(transaction_cid),
        "status": 0,
        "finishTime": 0,
        "soulCids": [],
        "events": [],
    }


def _initial_home_affairs():
    table = module_rules._table("homeland", "TransactionListTable")
    transaction_ids = sorted(
        int(key) for key, row in table.items()
        if isinstance(row, dict) and int(key) > 0
    )
    return [
        _new_home_affair(index, transaction_cid)
        for index, transaction_cid in enumerate(transaction_ids[:4], 1)
    ]


def _next_home_transaction_cid(building, office, affair_id, rng=random):
    """Draw a replacement transaction the way the official config describes it.

    ``CfgHomeLandBuildingLevelUpTable`` Type 15 publishes the star-tier
    distribution per office level and ``CfgTransactionListTable`` carries a
    per-row Weight, so a refreshed slot is a two-stage weighted draw. Rows
    already shown on the board are excluded so two slots never display the
    same job.
    """
    table = module_rules._table("homeland", "TransactionListTable")
    taken = {
        int(row.get("cid", 0))
        for row in (office or {}).get("affairs", [])
        if isinstance(row, dict) and int(row.get("cid", 0) or 0) > 0
    }
    candidates = [
        (int(key), row) for key, row in table.items()
        if isinstance(row, dict) and int(key) > 0 and int(key) not in taken
        and int(row.get("Weight", 0) or 0) > 0
    ]
    if not candidates:
        return 0
    star_weights = {}
    rule = _home_building_level_rule(
        building.get("id", 0), building.get("lv", 1),
    ) if isinstance(building, dict) else None
    if rule is not None:
        types = rule.get("Type", [])
        params = rule.get("Params", [])
        for index, effect_type in enumerate(types if isinstance(types, list) else []):
            if int(effect_type) != 15:
                continue
            values = params[index] if isinstance(params, list) and index < len(params) else []
            star_weights = {
                star: int(weight)
                for star, weight in enumerate(values if isinstance(values, list) else [], 1)
                if int(weight) > 0
            }
    stars = {
        star for star in star_weights
        if any(int(row.get("WorkStarLevel", 0) or 0) == star for _cid, row in candidates)
    }
    if stars:
        star = rng.choices(
            sorted(stars), weights=[star_weights[star] for star in sorted(stars)], k=1,
        )[0]
        candidates = [
            (cid, row) for cid, row in candidates
            if int(row.get("WorkStarLevel", 0) or 0) == star
        ]
    return rng.choices(
        [cid for cid, _row in candidates],
        weights=[int(row.get("Weight", 0) or 0) for _cid, row in candidates],
        k=1,
    )[0]


def _initial_home_buildings():
    now = _stamp()
    return [
        {
            "id": 36000001,
            "cid": 36000001,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
            "productionData": {
                "output": {},
                "itemAwards": {11911: 1},
                "storageLimit": 100,
                "nextProduceTime": now + 300,
                "oneProduceTime": 300,
            },
        },
        {
            "id": 36000002,
            "cid": 36000002,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
            "manufacture": {"maxQueueCount": 1, "makes": []},
        },
        {
            "id": 36000003,
            "cid": 36000003,
            "lv": 1,
            "helpLogs": [],
            "lands": [
                {
                    "cid": land_cid,
                    "currentSeedCid": 0,
                    "finishTime": 0,
                    "status": 1,
                }
                for land_cid in range(36300001, 36300005)
            ],
        },
        {
            "id": 36000005,
            "cid": 36000005,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
            "kitchenPOD": {"maxQueueCount": 1, "culinarys": []},
        },
        {
            "id": 36000006,
            "cid": 36000006,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
            "officePOD": {"affairs": _initial_home_affairs(), "freeRefreshTimes": 4},
        },
        {
            "id": 36000007,
            "cid": 36000007,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
        },
        {
            "id": 36000008,
            "cid": 36000008,
            "lv": 1,
            "helpLogs": [],
            "lands": [],
        },
    ]


def _home_initial_dress_cids(soul_id):
    soul_id = int(soul_id)
    if not 20010001 <= soul_id <= 20010055:
        return 0, 0
    suffix = soul_id - 20010000
    return 33010000 + suffix * 100 + 10, 33000000 + suffix * 100 + 10


def _home_role_pod(role):
    role = role or {}
    return {
        "roleCid": int(role.get("roleCid", 0)),
        "dress2DCid": int(role.get("dress2DCid", 0)),
        "dress3DCid": int(role.get("dress3DCid", 0)),
        "favorLv": max(1, int(role.get("favorLv", 1))),
        "belongRoom": int(role.get("belongRoom", HOME_DEFAULT_BELONG_ROOM)),
        "letters": [int(value) for value in role.get("letters", [])],
        "transactionCid": int(role.get("transactionCid", 0)),
        "newStoryId": int(role.get("newStoryId", 0)),
    }


def _initial_home_roles(uid):
    roles = []
    for soul in storage.get_souls(uid)[:HOME_ROLE_LIMIT]:
        soul_id = int(soul.get("soul_id", 0))
        dress_2d, dress_3d = _home_initial_dress_cids(soul_id)
        if dress_2d <= 0 or dress_3d <= 0:
            continue
        roles.append({
            "roleCid": soul_id,
            "dress2DCid": dress_2d,
            "dress3DCid": dress_3d,
            "favorLv": max(1, int(soul.get("favor_level", 1))),
            "belongRoom": HOME_DEFAULT_BELONG_ROOM,
            "letters": [],
            "transactionCid": 0,
            "newStoryId": 0,
        })
    return roles


def _seed_initial_home_state(uid, data):
    player = storage.get_player(uid) or {}
    changed = False
    role_id = int(player.get("role_id") or 0)
    if role_id > 0 and int(data.get("id") or 0) != role_id:
        data["id"] = role_id
        changed = True
    if "already_reward" not in data or not isinstance(data.get("already_reward"), list):
        data["already_reward"] = []
        changed = True
    if not data.get("rooms"):
        data["rooms"] = [
            _initial_home_room(room_id, suit_id)
            for room_id, suit_id in INITIAL_HOME_ROOMS
        ]
        changed = True
    if _home_migrate_room_defaults(data):
        changed = True
    if not data.get("buildings"):
        data["buildings"] = _initial_home_buildings()
        changed = True
    office_building = _home_building(data, 36000006)
    if office_building is not None:
        office = office_building.get("officePOD")
        if not isinstance(office, dict):
            office = {"affairs": [], "freeRefreshTimes": 4}
            office_building["officePOD"] = office
            changed = True
        if not isinstance(office.get("affairs"), list) or not office.get("affairs"):
            affairs = _initial_home_affairs()
            if affairs:
                office["affairs"] = affairs
                changed = True
        if "freeRefreshTimes" not in office:
            office["freeRefreshTimes"] = 4
            changed = True
    if _advance_home_state(data):
        changed = True
    if not data.get("suits"):
        data["suits"] = sorted({
            int(suit_id)
            for _room_id, suit_id in INITIAL_HOME_ROOMS
            if int(suit_id) > 0
        })
        changed = True
    if not data.get("roles"):
        data["roles"] = _initial_home_roles(uid)
        changed = True
    if _home_recalculate_comfort(data):
        changed = True
    return _save(uid, "home", data) if changed else True


def handle_home_enter(session, uid):
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if not _seed_initial_home_state(uid, data):
        return False
    pod = _home_pod(uid, data)
    session.send(1834, protocol_codec.encode_method(1834, 0, pod))
    return True

def _home_land_seed_cid(land):
    return int(land.get("currentSeedCid", land.get("seed", 0)) or 0)


def _home_land_finish_time(land):
    explicit = int(land.get("finishTime", 0) or 0)
    if explicit > 0:
        return explicit
    planted_at = int(land.get("planted_at", 0) or 0)
    grow_time = int(land.get("grow_time", 0) or 0)
    return planted_at + grow_time if planted_at > 0 and grow_time > 0 else 0


def _reset_home_land(land):
    land.update({
        "seed": 0,
        "currentSeedCid": 0,
        "planted_at": 0,
        "grow_time": 0,
        "finishTime": 0,
        "status": 1,
    })


HOME_FREE_REFRESH_TIMES = 4


def _home_day(now):
    # The shipped Constant.Player.DailyResetTimeHour is 04:00.
    return time.strftime("%Y-%m-%d", time.localtime(int(now) - 4 * 3600))


def _roll_home_day(data, now):
    """Reset the per-day work quota when the 04:00 game day turns over.

    The client only restores ``todayHomeWorkCount`` from the login PlayerPOD
    (``HLWorkModule.Reload``), so the server owns the rollover; without it the
    quota stays spent forever.
    """
    day = _home_day(now)
    if data.get("home_day") == day:
        return False
    data["home_day"] = day
    data["today_home_work_count"] = 0
    for building in data.get("buildings", []):
        office = _home_office(building)
        if office is not None:
            office["freeRefreshTimes"] = HOME_FREE_REFRESH_TIMES
    return True


def home_today_work_count(uid):
    """Today's spent work quota, or 0 once the 04:00 game day has turned."""
    data = _state(uid, "home")
    if data.get("home_day") != _home_day(_stamp()):
        return 0
    try:
        return max(0, int(data.get("today_home_work_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _advance_home_state(data, now=None):
    now = _stamp() if now is None else int(now)
    changed = _roll_home_day(data, now)
    for building in data.get("buildings", []):
        if not isinstance(building, dict):
            continue
        for land in building.get("lands", []):
            if not isinstance(land, dict) or _home_land_seed_cid(land) <= 0:
                continue
            finish_time = _home_land_finish_time(land)
            if finish_time > 0 and finish_time <= now and int(land.get("status", 1)) != 5:
                land["finishTime"] = finish_time
                land["status"] = 5
                changed = True
        kitchen = building.get("kitchenPOD")
        if isinstance(kitchen, dict):
            for culinary in kitchen.get("culinarys", []):
                if (
                    isinstance(culinary, dict)
                    and int(culinary.get("status", 0)) == 1
                    and 0 < int(culinary.get("finishTime", 0)) <= now
                ):
                    culinary["status"] = 2
                    changed = True
        office = building.get("officePOD")
        if not isinstance(office, dict):
            continue
        for affair in office.get("affairs", []):
            if (
                isinstance(affair, dict)
                and int(affair.get("status", 0)) == 1
                and 0 < int(affair.get("finishTime", 0)) <= now
            ):
                affair["status"] = 2
                changed = True
    return changed


def handle_home_plant(session, uid, body):
    try:
        building_id, land_id, seed_id = protocol_codec.decode_method(1806, body)
    except (ValueError, KeyError):
        return False
    if min(int(building_id), int(land_id), int(seed_id)) <= 0:
        return False
    plant_rule = _home_config_row("HomeLandPlantTable", seed_id)
    if plant_rule is None:
        return False
    cost_item = int(plant_rule.get("CostItem", 0) or 0)
    grow_time = int(plant_rule.get("CostTime", 0) or 0)
    if cost_item <= 0 or grow_time <= 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if not _seed_initial_home_state(uid, data):
        return False
    building = _home_building(data, building_id)
    if building is None:
        return False
    land = next(
        (row for row in building.setdefault("lands", [])
         if int(row.get("id", row.get("cid", 0))) == int(land_id)),
        None,
    )
    if (
        land is None
        or _home_land_seed_cid(land) > 0
        or int(land.get("status", 1)) != 1
    ):
        return False
    now = _stamp()
    land.update({
        "id": int(land_id),
        "cid": int(land_id),
        "seed": int(seed_id),
        "currentSeedCid": int(seed_id),
        "planted_at": now,
        "grow_time": grow_time,
        "finishTime": now + grow_time,
        "status": 3,
    })
    applied = storage.trade_reward_pairs_with_state(
        uid, [(cost_item, 1)], [], {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1838, protocol_codec.encode_method(
        1838, 0, int(building.get("cid", building_id)), _home_land_pod(land),
    ))
    return True


def handle_home_harvest_land(session, uid, body):
    try:
        building_id, land_id = protocol_codec.decode_method(1805, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    if building is None:
        return False
    advanced = _advance_home_state(data)
    lands = [row for row in building.setdefault("lands", []) if isinstance(row, dict)]
    if int(land_id) == -1:
        targets = [row for row in lands if int(row.get("status", 1)) == 5]
    else:
        targets = [
            row for row in lands
            if int(row.get("id", row.get("cid", 0))) == int(land_id)
            and int(row.get("status", 1)) == 5
        ]
    if not targets:
        if advanced:
            _save(uid, "home", data)
        return False
    reward_totals = {}
    for land in targets:
        plant_rule = _home_config_row("HomeLandPlantTable", _home_land_seed_cid(land))
        pairs = module_rules._pairs(plant_rule.get("DropItem", [])) if plant_rule else []
        if not pairs:
            return False
        for cid, quantity in pairs:
            reward_totals[int(cid)] = reward_totals.get(int(cid), 0) + int(quantity)
    returned_lands = []
    for land in targets:
        _reset_home_land(land)
        returned_lands.append(_home_land_pod(land))
    applied = storage.trade_reward_pairs_with_state(
        uid, [], sorted(reward_totals.items()), {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1837, protocol_codec.encode_method(
        1837, 0, int(building.get("cid", building_id)),
        returned_lands, applied.get("rewards", []),
    ))
    return True

def handle_home_cook(session, uid, body):
    try:
        building_id, queue_id, recipe_id, count = protocol_codec.decode_method(1812, body)
    except (ValueError, KeyError):
        return False
    if min(int(building_id), int(queue_id), int(recipe_id), int(count)) <= 0:
        return False
    recipe = _home_config_row("CookCombinationTable", recipe_id)
    if recipe is None:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if not _seed_initial_home_state(uid, data):
        return False
    building = _home_building(data, building_id)
    kitchen = building.get("kitchenPOD") if building else None
    if not isinstance(kitchen, dict):
        return False
    max_queue = max(1, int(kitchen.get("maxQueueCount", 1) or 1))
    if int(queue_id) > max_queue:
        return False
    culinarys = kitchen.setdefault("culinarys", [])
    if any(
        isinstance(row, dict) and int(row.get("id", 0)) == int(queue_id)
        for row in culinarys
    ):
        return False
    unlock_level = int(recipe.get("UnlockLevel", 1) or 1)
    unlocked = int(building.get("lv", 1)) >= unlock_level
    if bool(recipe.get("IsLock", False)):
        unlocked = int(recipe_id) in [int(value) for value in data.get("unlock_cook_book", [])]
    if not unlocked:
        return False
    ingredient_pairs = module_rules._pairs(recipe.get("NeedItem", []))
    single_cook_time = int(recipe.get("CookTimes", 0) or 0)
    if not ingredient_pairs or single_cook_time <= 0:
        return False
    costs = [(cid, quantity * int(count)) for cid, quantity in ingredient_pairs]
    now = _stamp()
    culinarys.append({
        "id": int(queue_id),
        "cid": int(recipe_id),
        "status": 1,
        "finishTime": now + single_cook_time * int(count),
        "idx": int(queue_id),
        "costs": [int(cid) for cid, _quantity in ingredient_pairs],
        "num": int(count),
        "receiveNum": 0,
        "singleCookTime": single_cook_time,
        "startTime": now,
    })
    culinarys.sort(key=lambda row: int(row.get("id", 0)) if isinstance(row, dict) else 0)
    applied = storage.trade_reward_pairs_with_state(uid, costs, [], {"home": data})
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1844, protocol_codec.encode_method(
        1844, 0, _home_building_pod(building),
    ))
    return True


def handle_home_reward_cook(session, uid, body):
    try:
        building_id, queue_id = protocol_codec.decode_method(1814, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    kitchen = building.get("kitchenPOD") if building else None
    culinarys = kitchen.get("culinarys", []) if isinstance(kitchen, dict) else []
    culinary = next(
        (row for row in culinarys if isinstance(row, dict) and int(row.get("id", 0)) == int(queue_id)),
        None,
    )
    if culinary is None:
        return False
    recipe = _home_config_row("CookCombinationTable", culinary.get("cid", 0))
    reward_pairs = module_rules._pairs(recipe.get("ItemId", [])) if recipe else []
    single_cook_time = int(culinary.get("singleCookTime", 0) or 0)
    total_count = int(culinary.get("num", 0) or 0)
    received_count = int(culinary.get("receiveNum", 0) or 0)
    start_time = int(culinary.get("startTime", 0) or 0)
    if not reward_pairs or single_cook_time <= 0 or total_count <= received_count:
        return False
    completed_count = min(total_count, max(0, (_stamp() - start_time) // single_cook_time))
    claim_count = completed_count - received_count
    if claim_count <= 0:
        return False
    rewards_to_grant = [(cid, quantity * claim_count) for cid, quantity in reward_pairs]
    culinary["receiveNum"] = received_count + claim_count
    if int(culinary["receiveNum"]) >= total_count:
        kitchen["culinarys"] = [row for row in culinarys if row is not culinary]
    else:
        culinary["status"] = 1
    applied = storage.trade_reward_pairs_with_state(
        uid, [], rewards_to_grant, {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1846, protocol_codec.encode_method(
        1846, 0, _home_building_pod(building), applied.get("rewards", []),
    ))
    return True

def handle_home_unlock_room(session, uid, body):
    try:
        (room_id,) = protocol_codec.decode_method(1807, body)
    except (ValueError, KeyError):
        return False
    room_id = int(room_id)
    if room_id <= 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room_config = _home_config_row("HomeLandRoomTable", room_id)
    if not isinstance(room_config, dict) or int(room_config.get("CastleIndex", 0)) != 2:
        return False
    if _home_room(data, room_id) is not None:
        return False
    default_suit = int(room_config.get("DefaultSuit", 0) or 0)
    cost_pairs = module_rules._pairs(room_config.get("OpenCost", []))
    if default_suit <= 0 or not cost_pairs:
        return False
    room = _initial_home_room(room_id, default_suit)
    _home_apply_room_default_layout(room)
    data.setdefault("rooms", []).append(room)
    unlocked_suits = set(_safe_positive_int_list(data.get("suits", [])))
    if not unlocked_suits:
        unlocked_suits.update(
            int(suit_id) for _room_id, suit_id in INITIAL_HOME_ROOMS
            if int(suit_id) > 0
        )
    data["suits"] = sorted(unlocked_suits | {default_suit})
    data["decorations"] = sorted(
        set(_safe_positive_int_list(data.get("decorations", [])))
        | {
            _int(decorate.get("cid"), 0)
            for decorate in room.get("decorates", [])
            if isinstance(decorate, dict) and _int(decorate.get("cid"), 0) > 0
        },
    )
    _home_recalculate_comfort(data)
    applied = storage.trade_reward_pairs_with_state(
        uid, cost_pairs, [], {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1871, protocol_codec.encode_method(1871, _home_room_pod(room)))
    session.send(1868, protocol_codec.encode_method(
        1868, _home_basic_info_pod(data, uid),
    ))
    session.send(1839, protocol_codec.encode_method(1839, 0, room_id))
    return True

def handle_home_change_suit(session, uid, body):
    try:
        room_id, suit_id = protocol_codec.decode_method(1808, body)
    except (ValueError, KeyError):
        return False
    room_id, suit_id = int(room_id), int(suit_id)
    if room_id <= 0 or suit_id <= 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room = _home_room(data, room_id)
    if room is None:
        return False
    unlocked_suits = {int(value) for value in data.get("suits", [])}
    if unlocked_suits and suit_id not in unlocked_suits:
        return False
    room["suitCid"] = suit_id
    room["default_layout_initialized"] = True
    if not _save(uid, "home", data):
        return False
    session.send(1840, protocol_codec.encode_method(1840, 0, _home_room_pod(room)))
    return True

def _home_office(building):
    office = building.get("officePOD") if isinstance(building, dict) else None
    return office if isinstance(office, dict) else None


def _home_affair(office, affair_id):
    if not isinstance(office, dict):
        return None
    return next(
        (
            affair for affair in office.get("affairs", [])
            if isinstance(affair, dict) and int(affair.get("id", 0)) == int(affair_id)
        ),
        None,
    )


def _known_home_soul_cids(uid):
    return {int(row.get("soul_id", 0)) for row in storage.get_souls(uid)}


def _home_busy_soul_cids(data):
    busy = set()
    for building in data.get("buildings", []):
        office = _home_office(building)
        if office is None:
            continue
        for affair in office.get("affairs", []):
            if not isinstance(affair, dict) or int(affair.get("status", 0)) == 0:
                continue
            busy.update(int(value) for value in affair.get("soulCids", []))
    return busy


def _home_work_limit(building):
    rule = _home_building_level_rule(
        building.get("id", 0), building.get("lv", 1),
    )
    if rule is None:
        return 0
    types = rule.get("Type", [])
    params = rule.get("Params", [])
    for index, effect_type in enumerate(types if isinstance(types, list) else []):
        if int(effect_type) != 16:
            continue
        values = params[index] if isinstance(params, list) and index < len(params) else []
        return int(values[0]) if isinstance(values, list) and values else 0
    return 0


def _home_affair_quick_priority(affair):
    rule = _home_config_row("TransactionListTable", affair.get("cid", 0)) or {}
    rewards = module_rules._pairs(rule.get("Reward", []))
    has_moon_essence = any(int(cid) == 2 for cid, _quantity in rewards)
    return (
        int(rule.get("WorkStarLevel", 0) or 0),
        int(has_moon_essence),
        int(rule.get("AutoExecutionSort", 0) or 0),
        -int(affair.get("id", 0)),
    )


def _home_quick_start_work(uid, data, building, office):
    known_souls = _known_home_soul_cids(uid)
    available_souls = sorted(known_souls - _home_busy_soul_cids(data))
    work_limit = _home_work_limit(building)
    completed_count = int(data.get("today_home_work_count", 0))
    remaining_count = max(0, work_limit - completed_count)
    if remaining_count <= 0:
        return 0

    affairs = sorted(
        (
            affair for affair in office.get("affairs", [])
            if isinstance(affair, dict) and int(affair.get("status", 0)) == 0
        ),
        key=_home_affair_quick_priority,
        reverse=True,
    )
    now = _stamp()
    started_count = 0
    for affair in affairs:
        if started_count >= remaining_count:
            break
        rule = _home_config_row("TransactionListTable", affair.get("cid", 0))
        soul_range = rule.get("SoulNumNeed", []) if rule else []
        if not isinstance(soul_range, list) or len(soul_range) < 2:
            continue
        maximum = int(soul_range[1])
        work_time = int(rule.get("WorkNeedTime", 0) or 0)
        if maximum <= 0 or work_time <= 0 or len(available_souls) < maximum:
            continue
        selected_souls = available_souls[:maximum]
        del available_souls[:maximum]
        affair.update({
            "status": 1,
            "finishTime": now + work_time,
            "soulCids": selected_souls,
            "events": [],
        })
        started_count += 1
    if started_count:
        data["today_home_work_count"] = completed_count + started_count
    return started_count


def handle_home_start_work(session, uid, body):
    try:
        building_id, affair_id, soul_ids = protocol_codec.decode_method(1821, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(soul_ids, list):
        return False
    normalized_souls = [int(value) for value in soul_ids]
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if not _seed_initial_home_state(uid, data):
        return False
    building = _home_building(data, building_id)
    office = _home_office(building)
    if building is None or office is None:
        return False
    if int(affair_id) == 0 and not normalized_souls:
        count = _home_quick_start_work(uid, data, building, office)
        if count and not _save(uid, "home", data):
            return False
        session.send(1853, protocol_codec.encode_method(
            1853, 0, _home_building_pod(building), count,
        ))
        return True
    if not normalized_souls or len(normalized_souls) != len(set(normalized_souls)):
        return False
    affair = _home_affair(office, affair_id)
    if affair is None or int(affair.get("status", 0)) != 0:
        return False
    rule = _home_config_row("TransactionListTable", affair.get("cid", 0))
    if rule is None:
        return False
    soul_range = rule.get("SoulNumNeed", [])
    if not isinstance(soul_range, list) or len(soul_range) < 2:
        return False
    minimum, maximum = int(soul_range[0]), int(soul_range[1])
    if not minimum <= len(normalized_souls) <= maximum:
        return False
    known_souls = _known_home_soul_cids(uid)
    if any(soul_cid not in known_souls for soul_cid in normalized_souls):
        return False
    if any(soul_cid in _home_busy_soul_cids(data) for soul_cid in normalized_souls):
        return False
    work_limit = _home_work_limit(building)
    if int(data.get("today_home_work_count", 0)) >= work_limit:
        return False
    work_time = int(rule.get("WorkNeedTime", 0) or 0)
    if work_time <= 0:
        return False
    now = _stamp()
    affair.update({
        "status": 1,
        "finishTime": now + work_time,
        "soulCids": normalized_souls,
        "events": [],
    })
    data["today_home_work_count"] = int(data.get("today_home_work_count", 0)) + 1
    if not _save(uid, "home", data):
        return False
    session.send(1853, protocol_codec.encode_method(
        1853, 0, _home_building_pod(building), 1,
    ))
    return True


def handle_home_reward_work(session, uid, body):
    try:
        building_id, affair_id = protocol_codec.decode_method(1822, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    office = _home_office(building)
    if building is None or office is None:
        return False
    _advance_home_state(data)
    # The overview action calls ReqGetReward(buildingCid, -1) for "claim
    # all".  The detailed work list uses an individual affair id, while the
    # older local test path used 0 for the same batch operation.
    if int(affair_id) in (0, -1):
        targets = [
            affair for affair in office.get("affairs", [])
            if isinstance(affair, dict) and int(affair.get("status", 0)) == 2
        ]
    else:
        affair = _home_affair(office, affair_id)
        targets = [affair] if affair is not None and int(affair.get("status", 0)) == 2 else []
    if not targets:
        # Keep a stale overview click request/response-complete and return the
        # current building POD so the client can hide the button immediately.
        session.send(1854, protocol_codec.encode_method(
            1854, 0, _home_building_pod(building), [],
        ))
        return True
    total_rewards = {}
    reward_results = []
    for affair in targets:
        rule = _home_config_row("TransactionListTable", affair.get("cid", 0))
        base_pairs = module_rules._pairs(rule.get("Reward", [])) if rule else []
        soul_count = len(affair.get("soulCids", []))
        if not base_pairs or soul_count <= 0:
            return False
        item_award = []
        for cid, quantity in base_pairs:
            total = int(quantity) * soul_count
            total_rewards[int(cid)] = total_rewards.get(int(cid), 0) + total
            item_award.append({"cid": int(cid), "num": total, "tag": 0})
        reward_results.append({
            "affairId": int(affair.get("id", 0)),
            "itemAward": item_award,
            "superSuc": False,
        })
        next_cid = _next_home_transaction_cid(
            building, office, affair.get("id", 0),
        )
        if next_cid <= 0:
            return False
        affair.update({
            "status": 0,
            "finishTime": 0,
            "soulCids": [],
            "events": [],
            "cid": next_cid,
        })
    applied = storage.trade_reward_pairs_with_state(
        uid, [], sorted(total_rewards.items()), {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    # The building push is sent before the result so the client cannot keep
    # rendering the completed affair after processing rewardWorkResult.
    session.send(1870, protocol_codec.encode_method(
        1870, _home_building_pod(building),
    ))
    session.send(1854, protocol_codec.encode_method(
        1854, 0, _home_building_pod(building), reward_results,
    ))
    return True


def _home_building(data, building_id, create=False):
    building = next(
        (row for row in data.setdefault("buildings", [])
         if int(row.get("id", 0)) == int(building_id)),
        None,
    )
    if building is None and create:
        building = {"id": int(building_id), "cid": 0, "lv": 1, "lands": []}
        data["buildings"].append(building)
    return building


def _home_building_pod(building):
    building = building or {}
    pod = {
        "id": int(building.get("id", 0)),
        "cid": int(building.get("cid", 0)),
        "lv": int(building.get("lv", 1)),
        "helpLogs": list(building.get("helpLogs", [])),
        "lands": [
            _home_land_pod(land)
            for land in building.get("lands", [])
            if isinstance(land, dict)
        ],
    }
    production = building.get("productionData")
    if isinstance(production, dict):
        pod["productionData"] = {
            "output": {
                int(cid): int(quantity)
                for cid, quantity in production.get("output", {}).items()
            },
            "itemAwards": {
                int(cid): int(quantity)
                for cid, quantity in production.get("itemAwards", {}).items()
            },
            "storageLimit": int(production.get("storageLimit", 0)),
            "nextProduceTime": int(production.get("nextProduceTime", 0)),
            "oneProduceTime": int(production.get("oneProduceTime", 0)),
        }
    kitchen = building.get("kitchenPOD")
    if isinstance(kitchen, dict):
        pod["kitchenPOD"] = {
            "maxQueueCount": int(kitchen.get("maxQueueCount", 0)),
            "culinarys": list(kitchen.get("culinarys", [])),
        }
    office = building.get("officePOD")
    if isinstance(office, dict):
        pod["officePOD"] = {
            "affairs": [
                _home_affair_pod(affair)
                for affair in office.get("affairs", [])
                if isinstance(affair, dict)
            ],
            "freeRefreshTimes": int(office.get("freeRefreshTimes", 0)),
        }
    manufacture = building.get("manufacture")
    if isinstance(manufacture, dict):
        pod["manufacture"] = {
            "maxQueueCount": int(manufacture.get("maxQueueCount", 0)),
            "makes": list(manufacture.get("makes", [])),
        }
    return pod


def _home_land_pod(land):
    land = land or {}
    status = int(land.get("status", 1))
    seed_cid = _home_land_seed_cid(land)
    finish_time = _home_land_finish_time(land)
    if seed_cid > 0 and finish_time > 0 and finish_time <= _stamp():
        status = 5
    return {
        "cid": int(land.get("id", land.get("cid", 0))),
        "currentSeedCid": seed_cid,
        "finishTime": finish_time,
        # HomelandPlantType: 1 idle, 2 init, 3 seedling, 4 growing, 5 mature.
        "status": status,
    }


def _home_room(data, room_id, create=False):
    rooms = data.setdefault("rooms", [])
    for index, value in enumerate(rooms):
        if isinstance(value, dict) and int(value.get("cid", value.get("id", 0))) == int(room_id):
            return value
        if not isinstance(value, dict) and int(value) == int(room_id):
            room = _initial_home_room(int(value), _home_default_suit(value))
            rooms[index] = room
            return room
    if create:
        room = _initial_home_room(int(room_id), _home_default_suit(room_id))
        rooms.append(room)
        return room
    return None


def _home_room_pod(room):
    room = room or {}
    return {
        "cid": int(room.get("cid", 0)), "dbid": int(room.get("dbid", room.get("cid", 0))),
        "name": str(room.get("name", "房间")), "comfort": int(room.get("comfort", 0)),
        "suitCid": int(room.get("suitCid", 0)), "decorates": list(room.get("decorates", [])),
        "foreignShow": bool(room.get("foreignShow", False)),
        "receiveComfortAwards": bool(room.get("receiveComfortAwards", False)),
    }


def _home_claim(session, uid, key, pairs, state_field="home_claims"):
    applied = storage.claim_reward_once(uid, state_field, str(key), list(pairs))
    if applied is None:
        return None
    _send_reward_changes(session, applied)
    return applied.get("rewards", [])


def handle_home_exit(session, uid):
    data = _init_state(uid, "home", HOME_DEFAULTS)
    data["last_exit"] = _stamp()
    _save(uid, "home", data)
    session.send(1835, protocol_codec.encode_method(1835, 0))
    return True


def handle_home_harvest_building(session, uid, body):
    try:
        (building_id,) = protocol_codec.decode_method(1804, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    if building is None or "working" not in building and "cooking" not in building and "making" not in building:
        return False
    rewards = _home_claim(session, uid, f"building:{int(building_id)}", [(1, 100)])
    if rewards is None:
        return False
    for key in ("working", "cooking", "making"):
        building.pop(key, None)
    _save(uid, "home", data)
    session.send(1836, protocol_codec.encode_method(1836, 0, _home_building_pod(building), rewards))
    return True


def handle_home_visit(session, uid, body):
    try:
        target_pid, target_name = protocol_codec.decode_method(1809, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(target_pid, str) or not isinstance(target_name, str) or len(target_pid) > 128 or len(target_name) > 80:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    session.send(1841, protocol_codec.encode_method(1841, 0, target_pid, _home_pod(uid, data)))
    return True


def handle_home_trigger_plot(session, uid, body):
    try:
        (action_id,) = protocol_codec.decode_method(1810, body)
    except (ValueError, KeyError):
        return False
    action_id = int(action_id)
    if action_id <= 0:
        return False
    plot_dialogs = {
        int(key): _int(value, 0)
        for key, value in (HOME_CONFIG.get("homePlotDialogs") or {}).items()
        if _int(key, 0) > 0 and _int(value, 0) > 0
    }
    dialog_id = plot_dialogs.get(action_id)
    if dialog_id is None:
        # Older local snapshots exposed only dialog IDs. Keep accepting that
        # legacy shape during rollout, while all current clients use action IDs.
        legacy_dialog_ids = {
        int(value) for value in HOME_CONFIG.get("homePlotDialogCids", [])
        if _int(value, 0) > 0
        }
        if action_id not in legacy_dialog_ids:
            return False
        dialog_id = action_id
    if dialog_id <= 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    plots = _safe_positive_int_list(data.get("plots"))
    data["plots"] = plots
    if action_id in set(plots):
        # Type=1 homeland bubbles are one-shot. Type=2 new-story bubbles do
        # not reach this handler and continue through the 3402 story chain.
        session.send(1842, protocol_codec.encode_method(1842, 1, action_id))
        return True
    if getattr(session, "active_story", None) is not None:
        return False
    plots.append(action_id)
    if not _save(uid, "home", data):
        return False
    session.active_story = {
        "kind": "home",
        "plot_id": action_id,
        "dialog_cid": dialog_id,
    }
    session.send(1842, protocol_codec.encode_method(1842, 0, action_id))
    session.send(1604, protocol_codec.encode_method(1604, dialog_id))
    log.info(
        "  home plot opened uid=%s actionId=%d dialogCid=%d -> 1842/1604",
        uid,
        action_id,
        dialog_id,
    )
    return True


def handle_home_unlock_suit(session, uid, body):
    try:
        (suit_id,) = protocol_codec.decode_method(1811, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if int(suit_id) <= 0:
        return False
    suits = data.setdefault("suits", [])
    if int(suit_id) not in [int(value) for value in suits]:
        suits.append(int(suit_id))
    _save(uid, "home", data)
    session.send(1843, protocol_codec.encode_method(1843, 0, int(suit_id)))
    return True


def handle_home_cancel_cook(session, uid, body):
    try:
        building_id, cook_id = protocol_codec.decode_method(1813, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    kitchen = building.get("kitchenPOD") if building else None
    culinarys = kitchen.get("culinarys", []) if isinstance(kitchen, dict) else []
    remaining = [
        row for row in culinarys
        if not isinstance(row, dict) or int(row.get("id", 0)) != int(cook_id)
    ]
    if len(remaining) == len(culinarys):
        return False
    kitchen["culinarys"] = remaining
    if not _save(uid, "home", data):
        return False
    session.send(1845, protocol_codec.encode_method(
        1845, 0, _home_building_pod(building),
    ))
    return True


def handle_home_record_daily_action(session, uid, body):
    try:
        (action_id,) = protocol_codec.decode_method(1815, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    today = time.strftime("%Y-%m-%d")
    if data.get("today_date") != today:
        data["today_date"], data["today_actions"] = today, []
    if int(action_id) not in [int(value) for value in data["today_actions"]]:
        data["today_actions"].append(int(action_id))
    _save(uid, "home", data)
    session.send(1847, protocol_codec.encode_method(1847, 0))
    return True


def handle_home_change_room_name(session, uid, body):
    try:
        room_id, name = protocol_codec.decode_method(1816, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room = _home_room(data, room_id, True)
    room["name"] = name.strip()
    _save(uid, "home", data)
    session.send(1848, protocol_codec.encode_method(1848, 0, _home_room_pod(room)))
    return True


def handle_home_enter_room(session, uid, body):
    try:
        room_id, room_type = protocol_codec.decode_method(1817, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if _home_room(data, room_id) is None:
        return False
    data["current_room"] = int(room_id)
    _save(uid, "home", data)
    session.send(1849, protocol_codec.encode_method(1849, 0))
    return True


def handle_home_switch_room_show(session, uid, body):
    try:
        (room_id,) = protocol_codec.decode_method(1818, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room = _home_room(data, room_id, True)
    room["foreignShow"] = not bool(room.get("foreignShow", False))
    _save(uid, "home", data)
    session.send(1850, protocol_codec.encode_method(1850, 0, _home_room_pod(room)))
    return True


def handle_home_receive_comfort_level(session, uid, body):
    try:
        (level,) = protocol_codec.decode_method(1819, body)
    except (ValueError, KeyError):
        return False
    rewards = _home_claim(session, uid, f"comfort-level:{int(level)}", [(1, max(1, int(level) * 10))])
    if rewards is None:
        return False
    session.send(1851, protocol_codec.encode_method(1851, 0, rewards))
    return True


def handle_home_complete_cook(session, uid, body):
    try:
        building_id, queue_id, complete_time = protocol_codec.decode_method(1820, body)
    except (ValueError, KeyError):
        return False
    if int(complete_time) <= 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    kitchen = building.get("kitchenPOD") if building else None
    culinarys = kitchen.get("culinarys", []) if isinstance(kitchen, dict) else []
    culinary = next(
        (row for row in culinarys if isinstance(row, dict) and int(row.get("id", 0)) == int(queue_id)),
        None,
    )
    if culinary is None or int(culinary.get("status", 0)) != 1:
        return False
    now = _stamp()
    remaining = max(0, int(culinary.get("finishTime", 0)) - now)
    if remaining <= 0:
        acceleration_cost = 0
    else:
        if int(complete_time) < remaining:
            return False
        acceleration_cost = (remaining + 299) // 300
    single_cook_time = int(culinary.get("singleCookTime", 0) or 0)
    total_count = int(culinary.get("num", 0) or 0)
    if single_cook_time <= 0 or total_count <= 0:
        return False
    culinary["startTime"] = now - single_cook_time * total_count
    culinary["finishTime"] = now
    culinary["status"] = 2
    costs = [(2, acceleration_cost)] if acceleration_cost > 0 else []
    applied = storage.trade_reward_pairs_with_state(uid, costs, [], {"home": data})
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1852, protocol_codec.encode_method(
        1852, 0, _home_building_pod(building),
    ))
    return True


def handle_home_complete_plant(session, uid, body):
    try:
        building_id, land_id, complete_time = protocol_codec.decode_method(1823, body)
    except (ValueError, KeyError):
        return False
    if int(complete_time) < 0:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    if building is None:
        return False
    land = next(
        (
            row for row in building.get("lands", [])
            if isinstance(row, dict)
            and int(row.get("id", row.get("cid", 0))) == int(land_id)
        ),
        None,
    )
    if land is None or _home_land_seed_cid(land) <= 0 or int(land.get("status", 1)) == 5:
        return False
    land["finishTime"] = _stamp()
    land["status"] = 5
    if not _save(uid, "home", data):
        return False
    session.send(1855, protocol_codec.encode_method(
        1855, 0, int(building.get("cid", building_id)), _home_land_pod(land),
    ))
    return True


def handle_home_cancel_plant(session, uid, body):
    try:
        building_id, land_id = protocol_codec.decode_method(1824, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    if building is None:
        return False
    land = next(
        (
            row for row in building.get("lands", [])
            if isinstance(row, dict)
            and int(row.get("id", row.get("cid", 0))) == int(land_id)
        ),
        None,
    )
    if land is None or _home_land_seed_cid(land) <= 0:
        return False
    _reset_home_land(land)
    if not _save(uid, "home", data):
        return False
    session.send(1856, protocol_codec.encode_method(
        1856, 0, int(building.get("cid", building_id)), _home_land_pod(land),
    ))
    return True


def _home_make_action(session, uid, body, request_id, result_id, mode):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    if mode in ("cancel", "reward"):
        building_id, item_id = values
        count = 1
    else:
        building_id, item_id, count = values
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id, mode == "start")
    if building is None:
        return False
    rewards = []
    if mode == "start":
        if "making" in building:
            return False
        building["making"] = {"item": int(item_id), "count": int(count), "finish_at": _stamp() + 60}
    elif mode == "cancel":
        if "making" not in building:
            return False
        building.pop("making", None)
    else:
        making = building.get("making")
        if making is None or _stamp() < int(making.get("finish_at", _stamp())):
            return False
        key = f"make:{int(building_id)}:{int(item_id)}"
        rewards = _home_claim(session, uid, key, [(int(item_id), max(1, int(count)))])
        if rewards is None:
            return False
        building.pop("making", None)
    _save(uid, "home", data)
    if mode == "reward":
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _home_building_pod(building), rewards))
    else:
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _home_building_pod(building)))
    return True


def handle_home_make(session, uid, body):
    return _home_make_action(session, uid, body, 1825, 1857, "start")


def handle_home_cancel_make(session, uid, body):
    return _home_make_action(session, uid, body, 1826, 1858, "cancel")


def handle_home_reward_make(session, uid, body):
    return _home_make_action(session, uid, body, 1827, 1859, "reward")


def handle_home_complete_make(session, uid, body):
    return _home_make_action(session, uid, body, 1828, 1860, "complete")


def handle_home_help(session, uid, body):
    try:
        target_uid, building_ids = protocol_codec.decode_method(1829, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(target_uid, int) or target_uid < 0 or not isinstance(building_ids, list):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    data.setdefault("help", []).append({"target": int(target_uid), "buildings": [int(value) for value in building_ids], "time": _stamp()})
    data["help"] = data["help"][-20:]
    _save(uid, "home", data)
    rewards = _home_claim(session, uid, f"help:{int(target_uid)}:{','.join(str(int(value)) for value in building_ids)}", [(1, 10)])
    if rewards is None:
        return False
    session.send(1861, protocol_codec.encode_method(1861, 0, int(target_uid), rewards))
    return True


def handle_home_open_treasure(session, uid, body):
    try:
        (chest_id,) = protocol_codec.decode_method(1830, body)
    except (ValueError, KeyError):
        return False
    rewards = _home_claim(session, uid, f"chest:{int(chest_id)}", [(1, 50)])
    if rewards is None:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    data.setdefault("visitTreasureChest", {})[str(int(chest_id))] = _stamp()
    _save(uid, "home", data)
    session.send(1862, protocol_codec.encode_method(1862, 0, int(chest_id), rewards))
    return True


def handle_home_receive_comfort(session, uid, body):
    try:
        (comfort_id,) = protocol_codec.decode_method(1831, body)
    except (ValueError, KeyError):
        return False
    rewards = _home_claim(session, uid, f"comfort:{int(comfort_id)}", [(1, 100)])
    if rewards is None:
        return False
    session.send(1863, protocol_codec.encode_method(1863, 0, int(comfort_id), rewards))
    return True


def handle_home_save_decorate(session, uid, body):
    try:
        room_id, decorations = protocol_codec.decode_method(1832, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(decorations, list) or len(decorations) > 500:
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room = _home_room(data, room_id)
    if room is None:
        return False
    normalized = []
    for decorate in decorations:
        if not isinstance(decorate, dict):
            return False
        try:
            cid = int(decorate.get("cid", 0))
            x = int(decorate.get("x", 0))
            y = int(decorate.get("y", 0))
        except (TypeError, ValueError):
            return False
        if cid <= 0:
            return False
        normalized.append({"cid": cid, "x": x, "y": y})
    room["decorates"] = normalized
    room["default_layout_initialized"] = True
    data["decorations"] = sorted({
        _int(decorate.get("cid"), 0)
        for candidate_room in data.get("rooms", [])
        if isinstance(candidate_room, dict)
        for decorate in candidate_room.get("decorates", [])
        if isinstance(decorate, dict) and _int(decorate.get("cid"), 0) > 0
    })
    _home_recalculate_comfort(data)
    applied = storage.trade_reward_pairs_with_state(uid, [], [], {"home": data})
    if applied is None:
        return False
    session.send(1864, protocol_codec.encode_method(1864, 0, int(room_id)))
    session.send(1871, protocol_codec.encode_method(1871, _home_room_pod(room)))
    session.send(1868, protocol_codec.encode_method(
        1868, _home_basic_info_pod(data, uid),
    ))
    return True


def handle_home_reset_affair(session, uid, body):
    try:
        affair_id, building_id, is_free = protocol_codec.decode_method(1833, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    office = _home_office(building)
    affair = _home_affair(office, affair_id)
    if affair is None or int(affair.get("status", 0)) != 0:
        return False
    costs = []
    if bool(is_free):
        free_times = int(office.get("freeRefreshTimes", 0))
        if free_times <= 0:
            return False
        office["freeRefreshTimes"] = free_times - 1
    else:
        costs = [(2, 10)]
    next_cid = _next_home_transaction_cid(
        building, office, affair.get("id", 0),
    )
    if next_cid <= 0:
        return False
    affair.update(_new_home_affair(affair.get("id", 0), next_cid))
    applied = storage.trade_reward_pairs_with_state(
        uid, costs, [], {"home": data},
    )
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1865, protocol_codec.encode_method(1865, 0))
    session.send(1870, protocol_codec.encode_method(
        1870, _home_building_pod(building),
    ))
    return True


def handle_home_receive_letter(session, uid, body):
    try:
        letter_id, count = protocol_codec.decode_method(1875, body)
    except (ValueError, KeyError):
        return False
    if int(letter_id) <= 0 or int(count) <= 0:
        return False
    rewards = _home_claim(session, uid, f"letter:{int(letter_id)}", [(1, int(count))])
    if rewards is None:
        return False
    session.send(1876, protocol_codec.encode_method(1876, 0, int(letter_id), int(count), rewards))
    return True


def handle_home_update_building_level(session, uid, body):
    try:
        (building_id,) = protocol_codec.decode_method(1877, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    if not _seed_initial_home_state(uid, data):
        return False
    building = _home_building(data, building_id)
    if building is None:
        return False
    current_level = int(building.get("lv", 1) or 1)
    next_level = current_level + 1
    next_rule = _home_building_level_rule(building_id, next_level)
    if next_rule is None:
        return False
    costs = module_rules._pairs(next_rule.get("Cost", []))
    if not costs:
        return False
    building["lv"] = next_level
    _apply_home_building_level_effect(building, next_rule)
    applied = storage.trade_reward_pairs_with_state(uid, costs, [], {"home": data})
    if applied is None:
        return False
    _send_reward_changes(session, applied)
    session.send(1879, protocol_codec.encode_method(1879, 0))
    # updateBuildingLvResult only carries the result code in this protocol
    # generation. The authoritative BuildingPOD arrives through the existing
    # notifyUpdateBuilding push so the client can refresh the displayed level.
    session.send(1870, protocol_codec.encode_method(
        1870, _home_building_pod(building),
    ))
    return True


def handle_home_undo_affair(session, uid, body):
    try:
        affair_id, building_id = protocol_codec.decode_method(1878, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    office = _home_office(building)
    affair = _home_affair(office, affair_id)
    if affair is None or int(affair.get("status", 0)) != 1:
        return False
    affair.update({
        "status": 0,
        "finishTime": 0,
        "soulCids": [],
        "events": [],
    })
    if not _save(uid, "home", data):
        return False
    session.send(1880, protocol_codec.encode_method(1880, 0))
    session.send(1870, protocol_codec.encode_method(
        1870, _home_building_pod(building),
    ))
    return True


def _home_exchange(session, uid, body, request_id, result_id, decompose=False):
    try:
        source, target, count = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    if min(int(source), int(target), int(count)) <= 0 or int(count) > 999:
        return False
    costs = [(int(source), int(count))]
    rewards = [(int(target), int(count))]
    result = storage.trade_reward_pairs(uid, costs, rewards)
    if result is None:
        return False
    _send_reward_changes(session, result)
    session.send(result_id, protocol_codec.encode_method(result_id, 0, result.get("rewards", [])))
    return True


def handle_home_compound(session, uid, body):
    return _home_exchange(session, uid, body, 1881, 1883)


def handle_home_decompose(session, uid, body):
    return _home_exchange(session, uid, body, 1882, 1884, True)


def handle_home_decompose_decorate(session, uid, body):
    try:
        room_id, decorate_id = protocol_codec.decode_method(1885, body)
    except (ValueError, KeyError):
        return False
    data = _init_state(uid, "home", HOME_DEFAULTS)
    room = _home_room(data, room_id)
    if room is None:
        return False
    room["decorates"] = [
        value for value in room.get("decorates", [])
        if not isinstance(value, dict) or int(value.get("cid", 0)) != int(decorate_id)
    ]
    room["default_layout_initialized"] = True
    _home_recalculate_comfort(data)
    if storage.trade_reward_pairs_with_state(uid, [], [], {"home": data}) is None:
        return False
    session.send(1887, protocol_codec.encode_method(1887, 0, int(room_id), int(decorate_id)))
    session.send(1871, protocol_codec.encode_method(1871, _home_room_pod(room)))
    session.send(1868, protocol_codec.encode_method(
        1868, _home_basic_info_pod(data, uid),
    ))
    return True


def _home_unlock_land_balance_snapshot(uid, cost_pairs):
    attrs = storage.get_player_num_attrs(uid) or {}
    attr_ids = {_int(cid, 0) for cid, _quantity in cost_pairs if _int(cid, 0) > 0}
    item_ids = {cid for cid in attr_ids if cid not in attrs}
    items = {}
    if item_ids:
        for row in storage.get_items(uid):
            cid = _int(row.get("template_id"), 0)
            if cid in item_ids:
                items[str(cid)] = _int(row.get("quantity"), 0)
    return {
        "numAttrs": {str(cid): _int(attrs.get(cid, 0), 0) for cid in sorted(attr_ids)},
        "items": items,
    }


HOME_UNLOCK_LAND_ERROR_CODE = 1


def _home_unlock_land_reject(session, uid, building_id, land_id, reason, **details):
    record = {
        "event": "home_unlock_land_rejected",
        "uid": str(uid),
        "buildingId": building_id,
        "landId": land_id,
        "reason": str(reason),
    }
    record.update(details)
    try:
        message = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        message = repr(record)
    log.warning("home_unlock_land_rejected %s", message)
    # The client keeps the unlock dialog open until 1888 arrives.  Returning
    # only from the server handler used to leave that dialog without a result.
    if session is not None:
        try:
            session.send(1888, protocol_codec.encode_method(
                1888,
                HOME_UNLOCK_LAND_ERROR_CODE,
                {"cid": 0, "currentSeedCid": 0, "finishTime": 0, "status": 1},
            ))
        except Exception:
            log.exception(
                "home_unlock_land_error_response_failed uid=%s land=%s",
                uid,
                land_id,
            )
    return False


def handle_home_unlock_land(session, uid, body):
    try:
        building_id, land_id = protocol_codec.decode_method(1886, body)
    except (ValueError, KeyError):
        return _home_unlock_land_reject(
            session, uid, None, None, "decode_failed", bodyLength=len(body or b""),
        )
    building_id, land_id = int(building_id), int(land_id)
    if building_id != 36000003 or land_id <= 0:
        return _home_unlock_land_reject(
            session, uid, building_id, land_id, "invalid_target",
        )
    data = _init_state(uid, "home", HOME_DEFAULTS)
    building = _home_building(data, building_id)
    if building is None:
        return _home_unlock_land_reject(
            session, uid, building_id, land_id, "building_not_found",
        )
    grid = (HOME_CONFIG.get("plantGrids") or {}).get(str(land_id))
    if not isinstance(grid, dict):
        return _home_unlock_land_reject(
            session, uid, building_id, land_id, "grid_not_configured",
        )
    lands = building.setdefault("lands", [])
    if any(
        isinstance(row, dict)
        and int(row.get("id", row.get("cid", 0))) == land_id
        for row in lands
    ):
        return _home_unlock_land_reject(
            session, uid, building_id, land_id, "already_unlocked",
        )
    if not _home_condition_satisfied(uid, grid.get("ConditionId", 0)):
        return _home_unlock_land_reject(
            session,
            uid,
            building_id,
            land_id,
            "condition_failed",
            conditionId=_int(grid.get("ConditionId"), 0),
            condition=HOME_CONFIG.get("referencedConditions", {}).get(
                str(_int(grid.get("ConditionId"), 0)),
            ),
            quickChallenge=_read_state_json(uid, "quickChallenge"),
        )
    cost_pairs = module_rules._pairs(grid.get("OpenCost", []))
    pay_point = int(grid.get("OpenCostPayPoint", 0) or 0)
    if pay_point > 0:
        if cost_pairs:
            return _home_unlock_land_reject(
                session, uid, building_id, land_id, "conflicting_cost_config",
                costPairs=cost_pairs, payPoint=pay_point,
            )
        cost_pairs = [(5, pay_point)]
    if not cost_pairs and (grid.get("ConditionId") or grid.get("OpenCost") or pay_point):
        return _home_unlock_land_reject(
            session, uid, building_id, land_id, "invalid_cost_config",
            costPairs=cost_pairs, payPoint=pay_point,
        )
    land = {
        "id": land_id,
        "cid": land_id,
        "status": 1,
        "seed": 0,
        "currentSeedCid": 0,
        "finishTime": 0,
    }
    lands.append(land)
    applied = storage.trade_reward_pairs_with_state(uid, cost_pairs, [], {"home": data})
    if applied is None:
        return _home_unlock_land_reject(
            session,
            uid,
            building_id,
            land_id,
            "cost_or_transaction_failed",
            costPairs=cost_pairs,
            balance=_home_unlock_land_balance_snapshot(uid, cost_pairs),
        )
    _send_reward_changes(session, applied)
    session.send(1869, protocol_codec.encode_method(
        1869, building_id,
        [_home_land_pod(row) for row in lands if isinstance(row, dict)],
    ))
    session.send(1888, protocol_codec.encode_method(1888, 0, _home_land_pod(land)))
    return True


# ── Module: net_formation (编队预制) ──

def _formation_state(uid):
    data = storage.get_player_state_json(uid, "formations")
    if not isinstance(data, list):
        data = []
    return data


def _formation_entry(formations, formation_id, create=False):
    entry = next((row for row in formations if isinstance(row, dict) and int(row.get("id", 0)) == int(formation_id)), None)
    if entry is None and create:
        entry = {"id": int(formation_id), "name": "编队", "prefabs": [], "updated_at": _stamp()}
        formations.append(entry)
    return entry


def handle_formation_change_name(session, uid, body):
    try:
        formation_id, name = protocol_codec.decode_method(4402, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        return False
    formations = _formation_state(uid)
    entry = _formation_entry(formations, formation_id, True)
    entry["name"] = name.strip()
    entry["updated_at"] = _stamp()
    _save(uid, "formations", formations)
    session.send(4404, protocol_codec.encode_method(4404, 0, name.strip()))
    return True


def handle_formation_exchange_prefab(session, uid, body):
    try:
        prefab_id, soul_id, position, skill_group_id, custom_skills, optional_skill = protocol_codec.decode_method(4403, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(custom_skills, list) or any(int(value) <= 0 for value in custom_skills):
        return False
    prefab = storage.update_soul_prefab(
        uid, int(prefab_id), int(soul_id), int(skill_group_id),
        [int(value) for value in custom_skills], int(optional_skill),
    )
    if prefab is None or not storage.update_soul_prefab_position(uid, int(prefab_id), int(position)):
        return False
    formations = _formation_state(uid)
    entry = _formation_entry(formations, prefab_id, True)
    entry["prefabs"] = [int(prefab_id)]
    entry["updated_at"] = _stamp()
    _save(uid, "formations", formations)
    session.send(4405, protocol_codec.encode_method(4405, 0))
    return True


def handle_formation_copy(session, uid, body):
    try:
        source_id, target_id = protocol_codec.decode_method(4407, body)
    except (ValueError, KeyError):
        return False
    formations = _formation_state(uid)
    source = _formation_entry(formations, source_id)
    if source is None or int(source_id) == int(target_id):
        return False
    target = _formation_entry(formations, target_id, True)
    target.update({"name": source.get("name", "编队"), "prefabs": list(source.get("prefabs", [])), "updated_at": _stamp()})
    _save(uid, "formations", formations)
    session.send(4408, protocol_codec.encode_method(4408, 0, int(source_id), int(target_id)))
    return True


# ── Module: net_town (城镇低频状态) ──

TOWN_DEFAULTS = {
    "shopping": [], "mainline": [], "areas": [], "last_area": 0,
    "current_event_id": 0, "executable_events": [], "shopping_event_ids": [],
    "completed_events": [], "completed_shopping": [], "town_day": "",
    "pending_story": None,
}

TOWN_PATROL_TICKET_CID = 101
TOWN_PATROL_TICKET_DAILY_MAX = 5


def _town_day(now=None):
    return _home_day(_stamp() if now is None else int(now))


def _town_event(event_id):
    row = (TOWN_CONFIG.get("events") or {}).get(str(int(event_id)))
    return row if isinstance(row, dict) else None


def _town_area(area_id):
    row = (TOWN_CONFIG.get("areas") or {}).get(str(int(area_id)))
    return row if isinstance(row, dict) else None


def _town_area_pod(row):
    if isinstance(row, dict):
        return {
            "cid": _int(row.get("cid", row.get("Id", 0)), 0),
            "isLock": bool(row.get("isLock", False)),
            "isNew": bool(row.get("isNew", False)),
        }
    return {"cid": _int(row, 0), "isLock": False, "isNew": False}


def _town_area_rows(data):
    rows = data.get("areas")
    if not isinstance(rows, list):
        rows = []
    normalized = []
    seen = set()
    for value in rows:
        pod = _town_area_pod(value)
        cid = _int(pod.get("cid"), 0)
        if cid <= 0 or cid in seen:
            continue
        seen.add(cid)
        normalized.append(pod)
    for raw_id, config in sorted(
        (TOWN_CONFIG.get("areas") or {}).items(), key=lambda item: int(item[0]),
    ):
        area_id = _int(raw_id, 0)
        if area_id <= 0 or not isinstance(config, dict):
            continue
        if area_id in seen:
            continue
        normalized.append({
            "cid": area_id,
            "isLock": not bool(_int(config.get("IsUnlocked", 0), 0)),
            "isNew": False,
        })
    data["areas"] = normalized
    return normalized


def _town_unlocked_area_ids(data):
    return {
        int(row["cid"])
        for row in _town_area_rows(data)
        if isinstance(row, dict) and not bool(row.get("isLock"))
    }


def _town_event_condition_satisfied(uid, event):
    condition_id = _int(event.get("Condition", 0), 0)
    if condition_id <= 0:
        return True
    row = (TOWN_CONFIG.get("referencedConditions") or {}).get(str(condition_id))
    if not isinstance(row, dict):
        row = (HOME_CONFIG.get("referencedConditions") or {}).get(str(condition_id))
    return _condition_satisfied(uid, row)


def _town_completed_ids(data, uid):
    completed = set(_safe_positive_int_list(data.get("completed_events")))
    unlocked = _read_state_json(uid, "unlockTownEvents")
    completed.update(_safe_positive_int_list(unlocked))
    return completed


def _town_refresh_executable(data, uid):
    unlocked_areas = _town_unlocked_area_ids(data)
    completed = _town_completed_ids(data, uid)
    previous = _safe_positive_int_list(data.get("executable_events"))
    current = {
        value for value in previous
        if value not in completed
        and _town_event(value)
        and _int(_town_event(value).get("EventType", 0), 0) in (1, 2)
    }
    for event_id, event in (TOWN_CONFIG.get("events") or {}).items():
        if not isinstance(event, dict):
            continue
        event_id = _int(event_id, 0)
        event_type = _int(event.get("EventType", 0), 0)
        if event_type not in (1, 2) or event_id in completed:
            continue
        if _int(event.get("AreaId", 0), 0) not in unlocked_areas:
            continue
        prerequisites = _safe_positive_int_list(event.get("PreEvent"))
        if any(value not in completed for value in prerequisites):
            continue
        if not _town_event_condition_satisfied(uid, event):
            continue
        current.add(event_id)
    data["executable_events"] = sorted(current)
    new_events = current.difference(previous)
    if new_events:
        new_areas = {
            _int(_town_event(value).get("AreaId", 0), 0)
            for value in new_events
            if _town_event(value) is not None
        }
        for row in _town_area_rows(data):
            if _int(row.get("cid"), 0) in new_areas:
                row["isNew"] = True
    mainline = [
        value for value in data["executable_events"]
        if _town_event(value) and _int(_town_event(value).get("EventType", 0), 0) == 1
    ]
    current_id = _int(data.get("current_event_id", 0), 0)
    if current_id in completed or (current_id and current_id not in data["executable_events"]):
        current_id = 0
    if current_id <= 0 and mainline:
        current_id = min(mainline)
    data["current_event_id"] = current_id


def _town_refresh_shopping(data, now=None):
    day = _town_day(now)
    if data.get("town_day") != day:
        data["town_day"] = day
        data["completed_shopping"] = []
        data["shopping_event_ids"] = []
    used = set(_safe_positive_int_list(data.get("completed_shopping")))

    def is_shopping(event_id):
        event = _town_event(event_id)
        if not isinstance(event, dict) or _int(event.get("EventType", 0), 0) != 0:
            return False
        event_type = event.get("Type")
        weight = event.get("Weight")
        return (
            isinstance(event_type, list) and event_type
            and _int(event_type[0], 0) == 2
            and isinstance(weight, list) and weight
            and _int(weight[0], 0) > 0
        )

    existing = []
    for event_id in _safe_positive_int_list(data.get("shopping_event_ids")):
        if event_id not in used and is_shopping(event_id):
            existing.append(event_id)
    # Migrate the earlier per-area implementation, which persisted nine IDs.
    if len(existing) > TOWN_SHOPPING_EVENT_COUNT:
        existing = []
    selected = list(existing)
    if not selected:
        selected = [
            event_id for event_id in TOWN_DEFAULT_SHOPPING_EVENT_IDS
            if event_id not in used and is_shopping(event_id)
        ]
    candidates = []
    for raw_event_id, event in (TOWN_CONFIG.get("events") or {}).items():
        event_id = _int(raw_event_id, 0)
        if event_id <= 0 or event_id in used or event_id in selected or not is_shopping(event_id):
            continue
        candidates.append((
            _int(event.get("AreaId", 0), 0),
            event_id,
        ))
    for _area_id, event_id in sorted(candidates):
        if len(selected) >= TOWN_SHOPPING_EVENT_COUNT:
            break
        selected.append(event_id)
    data["shopping_event_ids"] = selected[:TOWN_SHOPPING_EVENT_COUNT]


def _town_state(uid):
    data = _init_state(uid, "town", TOWN_DEFAULTS)
    previous_day = data.get("town_day")
    before = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _town_area_rows(data)
    _town_refresh_shopping(data)
    _town_refresh_executable(data, uid)
    after = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if after != before:
        # Patrol tickets are a daily allowance, not a cumulative reward. The
        # town-day marker is written in the same transaction as the refill so
        # reconnects cannot apply the same rollover twice.
        if previous_day != data.get("town_day"):
            attrs = storage.get_player_num_attrs(uid) or {}
            current = max(0, _int(attrs.get(TOWN_PATROL_TICKET_CID), 0))
            refill = max(0, TOWN_PATROL_TICKET_DAILY_MAX - current)
            applied = storage.trade_reward_pairs_with_state(
                uid,
                [],
                [[TOWN_PATROL_TICKET_CID, refill]] if refill else [],
                {"town": data},
            )
            if applied is None:
                log.error("town daily patrol ticket refresh failed uid=%s", uid)
        else:
            _save(uid, "town", data)
    return data


def _town_pod(uid, data=None):
    data = _town_state(uid) if data is None else data
    return {
        "areas": [_town_area_pod(row) for row in _town_area_rows(data)],
        "currentEventId": int(data.get("current_event_id", 0) or 0),
        "executableEvents": _safe_positive_int_list(data.get("executable_events")),
        "shoppingEventIds": _safe_positive_int_list(data.get("shopping_event_ids")),
    }


def _town_save_state(uid, data, unlock_town_events=None):
    updates = {"town": data}
    if unlock_town_events is not None:
        updates["unlockTownEvents"] = unlock_town_events
    return storage.trade_reward_pairs_with_state(uid, [], [], updates)


TOWN_DIALOG_CLIENT_EXECUTION_TYPES = frozenset((1001, 1002))
TOWN_DIALOG_MAX_SELECTIONS = 256


def _town_dialog(dialog_id):
    row = (TOWN_DIALOG_CONFIG.get("dialogs") or {}).get(str(_int(dialog_id, 0)))
    return row if isinstance(row, dict) else None


def _town_dialog_condition_satisfied(uid, condition_id):
    condition_id = _int(condition_id, 0)
    if condition_id <= 0:
        return True
    row = (TOWN_DIALOG_CONFIG.get("conditions") or {}).get(str(condition_id))
    if not isinstance(row, dict):
        row = (TOWN_CONFIG.get("referencedConditions") or {}).get(str(condition_id))
    return _condition_satisfied(uid, row)


def _town_dialog_transition(uid, row, selection_index):
    if not isinstance(row, dict):
        return None
    try:
        selection_index = int(selection_index)
    except (TypeError, ValueError):
        return None
    jumps = row.get("JumpID") if isinstance(row.get("JumpID"), list) else []
    if selection_index <= 0 or selection_index > len(jumps):
        return None
    position = selection_index - 1
    choose_conditions = row.get("ChooseCondition") or []
    if position < len(choose_conditions) and not _town_dialog_condition_satisfied(
        uid, choose_conditions[position]
    ):
        return None
    real_conditions = row.get("RealSelectionCondition") or []
    if position < len(real_conditions) and not _town_dialog_condition_satisfied(
        uid, real_conditions[position]
    ):
        return None
    next_id = _int(jumps[position], -1)
    if next_id == 0:
        return None
    services = row.get("JumService") or []
    services = services[position] if position < len(services) else []
    if not isinstance(services, list):
        services = []
    return next_id, [_int(service_id, 0) for service_id in services if _int(service_id, 0) > 0]


def _town_dialog_requires_server(services):
    for service_id in services or []:
        service = (TOWN_DIALOG_CONFIG.get("executions") or {}).get(str(_int(service_id, 0)))
        if not isinstance(service, dict):
            return True
        if _int(service.get("ExecutionType", -1), -1) not in TOWN_DIALOG_CLIENT_EXECUTION_TYPES:
            return True
    return False


def _town_dialog_reward_pairs(services):
    """Resolve only the town execution types that carry item/attribute rewards."""
    pairs = []
    for service_id in services or []:
        service = (TOWN_DIALOG_CONFIG.get("executions") or {}).get(str(_int(service_id, 0)))
        if not isinstance(service, dict):
            return None
        execution_type = _int(service.get("ExecutionType", -1), -1)
        params = service.get("Params") if isinstance(service.get("Params"), list) else []
        if execution_type in (101, 102, 109):
            if len(params) % 2:
                return None
            for index in range(0, len(params), 2):
                item_id = _int(params[index], 0)
                quantity = _int(params[index + 1], 0)
                if item_id <= 0 or quantity <= 0 or item_id not in storage._ITEM_TYPE_BY_CID:
                    return None
                pairs.append((item_id, quantity))
        elif execution_type in (1001, 1002, 103, 106):
            # 1001/1002 execute on the client; 103/106 are server-side
            # story bookkeeping services without a local reward payload.
            continue
        else:
            log.warning(
                "town dialog service has unsupported execution type: id=%s type=%s",
                service_id,
                execution_type,
            )
            return None
    totals = {}
    for item_id, quantity in pairs:
        totals[item_id] = totals.get(item_id, 0) + quantity
    return list(totals.items())


def _town_dialog_replay(uid, start_dialog_id, skip_indexes, select_index):
    try:
        skip_indexes = [int(value) for value in (skip_indexes or [])]
        select_index = int(select_index)
    except (TypeError, ValueError):
        return None
    if len(skip_indexes) > TOWN_DIALOG_MAX_SELECTIONS:
        return None
    selections = skip_indexes + [select_index]
    current_id = _int(start_dialog_id, 0)
    if current_id <= 0:
        return None
    for position, selection in enumerate(selections):
        row = _town_dialog(current_id)
        transition = _town_dialog_transition(uid, row, selection)
        if transition is None:
            return None
        next_id, services = transition
        is_last = position == len(selections) - 1
        if not is_last:
            # skipIndexes only contains locally executed transitions. A
            # server execution or terminal edge would have caused a request
            # at that point and reset the client's skip list.
            if next_id <= 0 or _town_dialog_requires_server(services):
                return None
            current_id = next_id
            continue
        if next_id > 0 and not _town_dialog_requires_server(services):
            # The client never sends 1602 for a purely local positive edge.
            return None
        return {
            "dialog_id": current_id,
            "next_dialog_id": next_id,
            "services": services,
        }
    return None


def _town_patrol_awards(area):
    if not isinstance(area, dict):
        return []
    return [
        (item_id, 1)
        for item_id in _safe_positive_int_list(area.get("PatrolAward"))
        if item_id in storage._ITEM_TYPE_BY_CID
    ]


def _town_active_story(session, pending):
    active = {
        "kind": "town",
        "town_kind": pending["town_kind"],
        "event_id": int(pending["event_id"]),
        "area_id": int(pending["area_id"]),
        "dialog_cid": int(pending["dialog_cid"]),
    }
    session.active_story = active
    return active


def _town_finish_pending_story(session, uid, data, pending, path):
    event_id = _int(pending.get("event_id"), 0)
    area_id = _int(pending.get("area_id"), 0)
    event = _town_event(event_id)
    if event is None or _int(event.get("AreaId"), 0) != area_id:
        return False
    service_rewards = _town_dialog_reward_pairs(path["services"])
    if service_rewards is None:
        return False
    reward_pairs = []
    for raw_pair in pending.get("reward_pairs") or []:
        if isinstance(raw_pair, (list, tuple)) and len(raw_pair) >= 2:
            reward_pairs.append((_int(raw_pair[0], 0), _int(raw_pair[1], 0)))
    reward_pairs.extend(service_rewards)
    reward_pairs = [
        (item_id, quantity)
        for item_id, quantity in reward_pairs
        if item_id > 0 and quantity > 0 and item_id in storage._ITEM_TYPE_BY_CID
    ]
    if pending.get("town_kind") == "shopping":
        completed_shopping = _safe_positive_int_list(data.get("completed_shopping"))
        if event_id not in completed_shopping:
            completed_shopping.append(event_id)
        data["completed_shopping"] = completed_shopping
        shopping = data.get("shopping")
        if not isinstance(shopping, list):
            shopping = []
        shopping.append({"id": event_id, "area": area_id, "time": _stamp()})
        data["shopping"] = shopping[-50:]
        _town_refresh_shopping(data)
        result_id = 2210
    else:
        mainline = data.get("mainline")
        if not isinstance(mainline, list):
            mainline = []
        mainline.append({"chapter": area_id, "event": event_id, "time": _stamp()})
        data["mainline"] = mainline[-100:]
        completed_events = _safe_positive_int_list(data.get("completed_events"))
        if event_id not in completed_events:
            completed_events.append(event_id)
        data["completed_events"] = completed_events
        data["executable_events"] = [
            value for value in _safe_positive_int_list(data.get("executable_events"))
            if value != event_id
        ]
        unlocked = _safe_positive_int_list(_read_state_json(uid, "unlockTownEvents"))
        if event_id not in unlocked:
            unlocked.append(event_id)
        _town_refresh_executable(data, uid)
        result_id = 2211

    data["pending_story"] = None
    updates = {"town": data}
    unlock_town_events = None
    if pending.get("town_kind") != "shopping":
        unlock_town_events = sorted(set(unlocked))
        updates["unlockTownEvents"] = unlock_town_events
    applied = storage.trade_reward_pairs_with_state(uid, [], reward_pairs, updates)
    if applied is None:
        return False

    # Refresh the town state while the dialog is still covering the map. The
    # incremental shopping notify can remove a stale function from the
    # client's live list while the reward panel is opening, which leaves the
    # town input lock active after the black transition. A full TownPOD
    # reload avoids that mutation race and carries all refreshed fields.
    if result_id == 2210:
        session.send(2213, protocol_codec.encode_method(2213, _town_pod(uid, data)))
    else:
        session.send(2209, protocol_codec.encode_method(2209, data["executable_events"]))
    session.active_story = None

    # Close the dialog before opening the reward panel so modal UI transitions
    # are processed in a stable order on the client.
    session.send(1603, protocol_codec.encode_method(1603, 0, -1))
    _send_reward_changes(session, applied)
    if result_id == 2210:
        session.send(2210, protocol_codec.encode_method(
            2210, area_id, applied.get("rewards", []),
        ))
    else:
        session.send(2211, protocol_codec.encode_method(2211, applied.get("rewards", [])))
    return True


def handle_town_dialog(session, uid, select_index, skip_indexes):
    """Advance a regular town story and settle it only at its terminal edge."""
    data = _town_state(uid)
    pending = data.get("pending_story")
    active = getattr(session, "active_story", None)
    if not isinstance(pending, dict) or pending.get("kind") != "town":
        return False
    if not isinstance(active, dict) or active.get("kind") != "town":
        return False
    if (
        _int(active.get("event_id"), 0) != _int(pending.get("event_id"), 0)
        or _int(active.get("dialog_cid"), 0) != _int(pending.get("dialog_cid"), 0)
    ):
        return False
    path = _town_dialog_replay(
        uid,
        pending.get("dialog_cid"),
        skip_indexes,
        select_index,
    )
    if path is None:
        return False
    service_rewards = _town_dialog_reward_pairs(path["services"])
    if service_rewards is None:
        return False
    if path["next_dialog_id"] > 0:
        pending["dialog_cid"] = path["next_dialog_id"]
        pending_rewards = []
        for raw_pair in pending.get("reward_pairs") or []:
            if isinstance(raw_pair, (list, tuple)) and len(raw_pair) >= 2:
                pending_rewards.append([_int(raw_pair[0], 0), _int(raw_pair[1], 0)])
        pending_rewards.extend([[item_id, quantity] for item_id, quantity in service_rewards])
        pending["reward_pairs"] = pending_rewards
        data["pending_story"] = pending
        if _town_save_state(uid, data) is None:
            return False
        active["dialog_cid"] = path["next_dialog_id"]
        session.send(1603, protocol_codec.encode_method(1603, 0, path["next_dialog_id"]))
        return True
    return _town_finish_pending_story(session, uid, data, pending, path)


def handle_town_shopping(session, uid, body):
    try:
        shop_id, = protocol_codec.decode_method(2202, body)
    except (ValueError, KeyError):
        return False
    shop_id = int(shop_id)
    if shop_id <= 0:
        return False
    data = _town_state(uid)
    if isinstance(data.get("pending_story"), dict) or getattr(session, "active_story", None) is not None:
        return False
    event = _town_event(shop_id)
    shopping_ids = set(_safe_positive_int_list(data.get("shopping_event_ids")))
    if event is not None and shop_id in shopping_ids:
        if _int(event.get("EventType"), 0) != 0:
            return False
        area_id = _int(event.get("AreaId", 0), 0)
    elif _town_area(shop_id) is not None:
        area_id = shop_id
        shop_id = next(
            (
                value for value in _safe_positive_int_list(data.get("shopping_event_ids"))
                if _town_event(value) and _int(_town_event(value).get("AreaId", 0), 0) == area_id
            ),
            0,
        )
        if shop_id <= 0:
            return False
        event = _town_event(shop_id)
    else:
        return False
    area = _town_area(area_id)
    if not isinstance(area, dict) or bool(next(
        (row.get("isLock") for row in _town_area_rows(data) if _int(row.get("cid", 0), 0) == area_id),
        False,
    )):
        return False
    cost = module_rules._pairs(area.get("WanderCost", []))
    awards = _town_patrol_awards(area)
    if not cost:
        return False
    dialog_id = _int(event.get("DialogId"), 0)
    if dialog_id <= 0 or _town_dialog(dialog_id) is None:
        return False
    pending = {
        "kind": "town",
        "town_kind": "shopping",
        "event_id": shop_id,
        "area_id": area_id,
        "dialog_cid": dialog_id,
        "reward_pairs": [[item_id, quantity] for item_id, quantity in awards],
        "started_at": _stamp(),
    }
    data["pending_story"] = pending
    applied = storage.trade_reward_pairs_with_state(uid, cost, [], {"town": data})
    if applied is None:
        return False
    _town_active_story(session, pending)
    _send_reward_changes(session, applied)
    session.send(2205, protocol_codec.encode_method(2205, 0))
    session.send(1604, protocol_codec.encode_method(1604, dialog_id))
    return True


def handle_town_mainline(session, uid, body):
    try:
        area_id, event_id = protocol_codec.decode_method(2203, body)
    except (ValueError, KeyError):
        return False
    area_id, event_id = int(area_id), int(event_id)
    if min(area_id, event_id) <= 0:
        return False
    data = _town_state(uid)
    if isinstance(data.get("pending_story"), dict) or getattr(session, "active_story", None) is not None:
        return False
    event = _town_event(event_id)
    completed = _town_completed_ids(data, uid)
    if event is not None:
        if int(event.get("AreaId", 0) or 0) != area_id:
            return False
        if int(event.get("EventType", 0) or 0) not in (1, 2):
            return False
        if event_id not in set(_safe_positive_int_list(data.get("executable_events"))):
            return False
        if any(value not in completed for value in _safe_positive_int_list(event.get("PreEvent"))):
            return False
        if not _town_event_condition_satisfied(uid, event):
            return False
    elif event_id in completed:
        return False
    else:
        # Preserve the small legacy fixture used by older tests while all real
        # client event IDs take the config-backed branch above.
        mainline = data.get("mainline")
        if not isinstance(mainline, list):
            mainline = []
        mainline.append({"chapter": area_id, "event": event_id, "time": _stamp()})
        data["mainline"] = mainline[-100:]
        if not _save(uid, "town", data):
            return False
        session.send(2206, protocol_codec.encode_method(2206, 0, area_id, event_id))
        return True
    dialog_id = _int(event.get("DialogId"), 0)
    if dialog_id <= 0 or _town_dialog(dialog_id) is None:
        return False
    pending = {
        "kind": "town",
        "town_kind": "mainline",
        "event_id": event_id,
        "area_id": area_id,
        "dialog_cid": dialog_id,
        "reward_pairs": [],
        "started_at": _stamp(),
    }
    data["pending_story"] = pending
    if _town_save_state(uid, data) is None:
        return False
    _town_active_story(session, pending)
    session.send(2206, protocol_codec.encode_method(2206, 0, area_id, event_id))
    session.send(1604, protocol_codec.encode_method(1604, dialog_id))
    return True


def handle_town_enter_area(session, uid, body):
    try:
        (area_id,) = protocol_codec.decode_method(2204, body)
    except (ValueError, KeyError):
        return False
    area_id = int(area_id)
    if area_id <= 0:
        return False
    data = _town_state(uid)
    config = _town_area(area_id)
    row = next((value for value in _town_area_rows(data) if int(value.get("cid", 0)) == area_id), None)
    if config is None and row is None:
        # Compatibility with the old low-frequency fixture; real town areas
        # are all present in the 5392 snapshot and use the strict branch.
        row = {"cid": area_id, "isLock": False, "isNew": False}
        data.setdefault("areas", []).append(row)
    if row is None or bool(row.get("isLock")):
        return False
    row["isNew"] = False
    data["last_area"] = area_id
    if not _save(uid, "town", data):
        return False
    session.send(2207, protocol_codec.encode_method(2207, 0, area_id))
    return True


# ── Module: net_soulMemory (记忆碎片) ──

def _memory_state(uid):
    return rebuild_memory_state(uid)


def rebuild_memory_state(uid, data=None, persist=True):
    """Rebuild every currently visible memory chapter from configuration."""
    if data is None:
        loaded = storage.get_player_state_json(uid, "soul_memory")
        data = loaded if isinstance(loaded, dict) else {}
    chapters = data.setdefault("chapters", {})
    if not isinstance(chapters, dict):
        chapters = {}
        data["chapters"] = chapters
    before = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    owned = {
        int(row["soul_id"]): int(row.get("favor_level", 0) or 0)
        for row in storage.get_souls(uid)
    }
    configs = sorted(
        (
            row for row in SOUL_MEMORY_CONFIG.get("chapters", {}).values()
            if isinstance(row, dict) and int(row.get("Id", 0) or 0) > 0
        ),
        key=lambda row: int(row["Id"]),
    )
    changed = True
    while changed:
        changed = False
        for config in configs:
            chapter_id = int(config["Id"])
            soul_id = int(config.get("SoulId", 0) or 0)
            if owned.get(soul_id, -1) < int(config.get("UnlockFavorDegreeLevel", 0) or 0):
                continue
            previous_id = int(config.get("PreMemoryChapter", 0) or 0)
            if previous_id > 0:
                previous = chapters.get(str(previous_id))
                if not isinstance(previous, dict) or not bool(previous.get("isGetReward", False)):
                    continue
            if str(chapter_id) not in chapters:
                _memory_chapter(data, chapter_id, create=True)
                changed = True

    # Normalize all retained chapters, including historical rows that were
    # already persisted before this rebuild logic existed.
    for key in list(chapters):
        if str(key).isdigit() and isinstance(chapters.get(key), dict):
            _memory_chapter(data, int(key))
    after = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if persist and after != before and not _save(uid, "soul_memory", data):
        raise RuntimeError("memory state rebuild could not be persisted")
    return data


def _memory_config(chapter_id):
    return SOUL_MEMORY_CONFIG.get("chapters", {}).get(str(int(chapter_id)))


def _memory_pairs(values):
    values = values if isinstance(values, list) else []
    return [
        (int(values[index]), int(values[index + 1]))
        for index in range(0, len(values) - 1, 2)
        if int(values[index]) > 0 and int(values[index + 1]) > 0
    ]


def _memory_chapter(data, chapter_id, create=False):
    chapters = data.setdefault("chapters", {})
    key = str(int(chapter_id))
    chapter = chapters.get(key)
    if chapter is None and create:
        chapter = {
            "cid": int(chapter_id), "isExperience": False, "isGetReward": False,
            "isNew": True, "unlockPieceCids": [],
        }
        chapters[key] = chapter
    if chapter is not None:
        chapter["cid"] = int(chapter_id)
        chapter["isExperience"] = bool(chapter.get("isExperience", False))
        chapter["isGetReward"] = bool(chapter.get("isGetReward", False))
        chapter["isNew"] = bool(chapter.get("isNew", True))
        chapter["unlockPieceCids"] = sorted({
            int(value) for value in chapter.get("unlockPieceCids", []) if int(value) > 0
        })
    return chapter


def _memory_unlocked_after(uid, data, chapter_id):
    before = set(data.setdefault("chapters", {}))
    rebuild_memory_state(uid, data, persist=False)
    created = [
        chapter for key, chapter in data["chapters"].items()
        if key not in before
        and isinstance(chapter, dict)
        and int((_memory_config(int(key)) or {}).get("PreMemoryChapter", 0) or 0) == int(chapter_id)
    ]
    return sorted(created, key=lambda chapter: int(chapter.get("cid", 0)))


def notify_memory_unlocks(session, uid):
    current = storage.get_player_state_json(uid, "soul_memory") or {}
    before = set((current.get("chapters") or {}).keys())
    rebuilt = rebuild_memory_state(uid, current, persist=True)
    created = [
        chapter for key, chapter in rebuilt.get("chapters", {}).items()
        if key not in before and isinstance(chapter, dict)
    ]
    for chapter in sorted(created, key=lambda row: int(row.get("cid", 0))):
        _notify_memory_chapter(session, chapter)
    return created


def handle_memory_activate_piece(session, uid, body):
    try:
        chapter_id, piece_id = protocol_codec.decode_method(3602, body)
    except (ValueError, KeyError):
        return False
    config = _memory_config(chapter_id)
    if config is None or int(piece_id) not in [int(value) for value in config.get("PieceIdList", [])]:
        return False
    data = _memory_state(uid)
    chapter = _memory_chapter(data, chapter_id, create=True)
    pieces = [int(value) for value in config.get("PieceIdList", [])]
    index = pieces.index(int(piece_id))
    unlocked = set(chapter["unlockPieceCids"])
    if int(piece_id) in unlocked:
        session.send(3606, protocol_codec.encode_method(3606, 0, int(chapter_id), int(piece_id), chapter))
        return True
    if any(previous not in unlocked for previous in pieces[:index]):
        return False
    piece_config = SOUL_MEMORY_CONFIG.get("pieces", {}).get(str(int(piece_id)), {})
    costs = _memory_pairs(piece_config.get("Cost", []))
    if storage.trade_reward_pairs(uid, costs, []) is None:
        return False
    chapter["unlockPieceCids"].append(int(piece_id))
    chapter["unlockPieceCids"] = sorted(set(chapter["unlockPieceCids"]), key=pieces.index)
    chapter["isNew"] = False
    data.setdefault("chapters", {})[str(int(chapter_id))] = chapter
    if not _save(uid, "soul_memory", data):
        log.error("memory piece save failed uid=%s chapter=%s piece=%s", uid, chapter_id, piece_id)
        return False
    session.send(3606, protocol_codec.encode_method(3606, 0, int(chapter_id), int(piece_id), chapter))
    _notify_memory_chapter(session, chapter)
    return True


def handle_memory_experience(session, uid, body):
    try:
        (chapter_id,) = protocol_codec.decode_method(3603, body)
    except (ValueError, KeyError):
        return False
    config = _memory_config(chapter_id)
    data = _memory_state(uid)
    chapter = _memory_chapter(data, chapter_id)
    if config is None or chapter is None:
        return False
    required = {int(value) for value in config.get("PieceIdList", [])}
    if not required or not required.issubset(set(chapter["unlockPieceCids"])):
        return False
    chapter["isExperience"] = True
    chapter["isNew"] = False
    if not _save(uid, "soul_memory", data):
        log.error("memory experience save failed uid=%s chapter=%s", uid, chapter_id)
        return False
    session.send(3607, protocol_codec.encode_method(3607, 0, int(chapter_id)))
    _notify_memory_chapter(session, chapter)
    return True


def handle_memory_reward(session, uid, body):
    try:
        (chapter_id,) = protocol_codec.decode_method(3604, body)
    except (ValueError, KeyError):
        return False
    config = _memory_config(chapter_id)
    data = _memory_state(uid)
    chapter = _memory_chapter(data, chapter_id)
    if config is None or chapter is None or not chapter.get("isExperience"):
        return False
    rewards = _home_claim(
        session, uid, f"soul-memory:{int(chapter_id)}",
        _memory_pairs(config.get("MemoryReward", [])), "soul_memory_claims",
    )
    if rewards is None:
        return False
    chapter["isGetReward"] = True
    new_chapters = _memory_unlocked_after(uid, data, chapter_id)
    new_chapter = new_chapters[0] if new_chapters else {}
    if not _save(uid, "soul_memory", data):
        log.error("memory reward save failed uid=%s chapter=%s", uid, chapter_id)
        return False
    # The shipped client dispatches MemoryGetRewardEvent while handling 3608.
    # Publish the persisted chapter PODs first so that event observes the
    # claimed current chapter and the newly unlocked next chapter immediately.
    _notify_memory_chapter(session, chapter)
    for unlocked in new_chapters:
        _notify_memory_chapter(session, unlocked)
    session.send(3608, protocol_codec.encode_method(
        3608, 0, int(chapter_id), rewards, chapter, new_chapter,
    ))
    return True


def handle_memory_view(session, uid, body):
    try:
        (chapter_id,) = protocol_codec.decode_method(3605, body)
    except (ValueError, KeyError):
        return False
    data = _memory_state(uid)
    chapter = _memory_chapter(data, chapter_id)
    if _memory_config(chapter_id) is None or chapter is None:
        return False
    chapter["isNew"] = False
    if not _save(uid, "soul_memory", data):
        log.error("memory view save failed uid=%s chapter=%s", uid, chapter_id)
        return False
    session.send(3609, protocol_codec.encode_method(3609, 0))
    _notify_memory_chapter(session, chapter)
    return True


# ── Module: net_evilErosion (心之裂痕编队/装备状态) ──

def _evil_state(uid):
    return _init_state(uid, "evil_erosion", {"prefabs": {}, "last_fight": 0})


def _evil_prefab(data, prefab_id, create=False, soul_id=0):
    prefabs = data.setdefault("prefabs", {})
    prefab = prefabs.get(str(int(prefab_id)))
    if prefab is None and create:
        prefab = {
            "id": int(prefab_id), "soulCid": int(soul_id), "lv": 1, "position": 1,
            "formationPos": 1, "power": 1, "qualityId": 0, "attr": [0.0] * 4,
            "allSkills": [], "allSkillStrengths": [], "customSkills": [], "equipments": {},
            "dress2DCid": 0, "dress3DCid": 0,
        }
        prefabs[str(int(prefab_id))] = prefab
    return prefab


def _evil_pod(prefab):
    prefab = prefab or {}
    return {
        "id": int(prefab.get("id", 0)), "soulCid": int(prefab.get("soulCid", 0)),
        "lv": int(prefab.get("lv", 1)), "position": int(prefab.get("position", 1)),
        "formationPos": int(prefab.get("formationPos", 1)), "power": int(prefab.get("power", 1)),
        "qualityId": int(prefab.get("qualityId", 0)), "attr": [float(value) for value in prefab.get("attr", [])],
        "allSkills": [int(value) for value in prefab.get("allSkills", [])],
        "allSkillStrengths": [int(value) for value in prefab.get("allSkillStrengths", [])],
        "customSkills": [int(value) for value in prefab.get("customSkills", [])],
        "equipments": {int(key): int(value) for key, value in prefab.get("equipments", {}).items()},
        "dress2DCid": int(prefab.get("dress2DCid", 0)), "dress3DCid": int(prefab.get("dress3DCid", 0)),
    }


def _evil_mutate(session, uid, body, request_id, result_id, operation):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    data = _evil_state(uid)
    prefab_id = int(values[0]) if values else 0
    default_soul = next((int(row.get("soul_id", 0)) for row in storage.get_souls(uid) if int(row.get("soul_id", 0)) > 0), 20010001)
    prefab = _evil_prefab(data, prefab_id, True, default_soul)
    if prefab is None or prefab_id <= 0:
        return False
    if operation == "wear":
        prefab.setdefault("equipments", {})[int(values[2])] = int(values[1])
    elif operation == "dump":
        prefab.setdefault("equipments", {}).pop(int(values[1]), None)
    elif operation == "exchange":
        equipment = prefab.setdefault("equipments", {})
        left, right = int(values[1]), int(values[2])
        equipment[left], equipment[right] = equipment.get(right, 0), equipment.get(left, 0)
    elif operation == "upstar":
        prefab["lv"] = int(prefab.get("lv", 1)) + 1
        prefab["power"] = int(prefab.get("power", 1)) + 1
    elif operation == "position":
        if int(values[1]) <= 0:
            return False
        prefab["formationPos"] = int(values[1])
    elif operation == "change_position":
        if int(values[1]) <= 0:
            return False
        prefab["position"] = int(values[1])
    elif operation == "skills":
        skills = values[1]
        if not isinstance(skills, list) or any(int(value) <= 0 for value in skills):
            return False
        prefab["customSkills"] = [int(value) for value in skills]
    _save(uid, "evil_erosion", data)
    session.send(result_id, protocol_codec.encode_method(result_id, 0))
    return True


def handle_evil_wear(session, uid, body): return _evil_mutate(session, uid, body, 6902, 6912, "wear")
def handle_evil_dump(session, uid, body): return _evil_mutate(session, uid, body, 6903, 6913, "dump")
def handle_evil_exchange(session, uid, body): return _evil_mutate(session, uid, body, 6904, 6914, "exchange")
def handle_evil_upstar(session, uid, body): return _evil_mutate(session, uid, body, 6905, 6915, "upstar")
def handle_evil_position(session, uid, body): return _evil_mutate(session, uid, body, 6907, 6917, "position")
def handle_evil_change_position(session, uid, body): return _evil_mutate(session, uid, body, 6908, 6918, "change_position")
def handle_evil_skills(session, uid, body): return _evil_mutate(session, uid, body, 6909, 6919, "skills")


def handle_evil_decompose(session, uid, body):
    try:
        (equipment_ids,) = protocol_codec.decode_method(6906, body)
    except (ValueError, KeyError):
        return False
    if not isinstance(equipment_ids, list) or len(equipment_ids) > 100:
        return False
    data = _evil_state(uid)
    wanted = {int(value) for value in equipment_ids}
    for prefab in data.get("prefabs", {}).values():
        prefab["equipments"] = {key: value for key, value in prefab.get("equipments", {}).items() if int(value) not in wanted}
    _save(uid, "evil_erosion", data)
    rewards = _grant_rewards(session, uid, [(1, len(wanted))]) if wanted else {"rewards": []}
    if rewards is None:
        return False
    session.send(6916, protocol_codec.encode_method(6916, 0, rewards.get("rewards", [])))
    return True


def handle_evil_fight(session, uid, body):
    try:
        (level_id,) = protocol_codec.decode_method(6910, body)
    except (ValueError, KeyError):
        return False
    if int(level_id) <= 0:
        return False
    if not module_rules._start_module_battle(
        session, uid, "evil_erosion", level_id, int(level_id), 0, [], battle_type=4,
    ):
        return False
    data = _evil_state(uid)
    data["last_fight"] = int(level_id)
    data["last_result"] = None
    _save(uid, "evil_erosion", data)
    session.send(6920, protocol_codec.encode_method(6920, 0))
    return True


# -- Module: remaining local gameplay ---------------------------------------

# These actions are not backed by a shipped server-side table in this client
# build.  They still need an explicit, persistent single-player state machine
# so retries do not become silent success or duplicate rewards.
REMAINING_BATTLE_REQUESTS = {
    3102, 3202, 4703, 4704, 5202, 5502, 6403, 6404, 7103, 7802,
    7902, 8005, 9402, 9405,
}

REMAINING_REWARD_REQUESTS = {
    5103: 100,
    5104: 200,
    5302: 50,
    5402: 10,
    5503: 100,
    5702: 10,
    5802: 10,
    6003: 25,
    6102: 50,
    7202: 10,
    8004: 100,
    9403: 100,
    9406: 100,
    9809: 100,
}


def _remaining_module_name(request_id):
    method = protocol_codec.METHODS.get(request_id, {})
    qualified_name = str(method.get("method", "net_local.action"))
    return qualified_name.split(".", 1)[0] or "net_local"


def _remaining_state(uid, module_name):
    state = storage.get_player_state_json(uid, "remaining_modules") or {}
    modules = state.setdefault("modules", {})
    module = modules.setdefault(module_name, {"actions": {}, "data": {}})
    module.setdefault("actions", {})
    module.setdefault("data", {})
    return state, module


def _remaining_item_show(reward_pairs):
    return [
        {"cid": int(cid), "num": int(quantity), "tag": 0}
        for cid, quantity in reward_pairs
        if int(cid) > 0 and int(quantity) > 0
    ]


def _remaining_operation_data(values, module_name):
    numbers = [
        int(value)
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]
    event_id = numbers[0] if numbers else 0
    data_id = numbers[1] if len(numbers) > 1 else event_id
    return {"eventCfgId": event_id, "dataCfgId": data_id}


def _remaining_dream_pod(data):
    map_id = int(data.get("mapId", 0))
    map_row = module_rules._row("dream_map", "DreamMapListTable", map_id) or {}
    raw_cells = map_row.get("DreamMapData") or []
    configured = {}
    for index in range(0, len(raw_cells) - 2, 3):
        try:
            x, y, data_id = (int(raw_cells[index]), int(raw_cells[index + 1]), int(raw_cells[index + 2]))
        except (TypeError, ValueError):
            continue
        if x < 0 or y < 0:
            continue
        configured[f"{x}:{y}"] = {"x": x, "y": y, "dataId": data_id}
    cells = []
    stored = data.setdefault("cells", {})
    if configured:
        for key, base in configured.items():
            cell = stored.setdefault(key, dict(base))
            cell.update({"x": base["x"], "y": base["y"], "dataId": base["dataId"]})
            cell.setdefault("elementId", 0)
            cell.setdefault("isOpen", False)
            cell.setdefault("markType", 0)
            cell.setdefault("showType", True)
            cells.append(dict(cell))
    else:
        for key, cell in stored.items():
            if isinstance(cell, dict):
                cells.append(dict(cell))
    cells.sort(key=lambda value: (int(value.get("x", 0)), int(value.get("y", 0))))
    return {
        "cells": cells,
        "combo": int(data.get("combo", 0)),
        "currDialog": int(data.get("currDialog", 0)),
        "mapId": int(data.get("mapId", 0)),
        "movePoint": int(data.get("movePoint", 10)),
        "resetCount": int(data.get("resetCount", 0)),
        "roleX": int(data.get("roleX", 0)),
        "roleY": int(data.get("roleY", 0)),
    }


def _dream_generated_cell_id(cell_type, seed, index, used):
    if _int(cell_type) <= 0:
        return 0
    table = module_rules._row("dream_map", "DreamMapCellTable", cell_type) or {}
    candidates = []
    for value in table.get("CellData") or []:
        row = module_rules._row("dream_map", "DreamMapCellDataTable", value)
        if row is not None:
            candidates.append((_int(value), row))
    if not candidates:
        return 0
    available = [item for item in candidates if _int(item[1].get("LimitNum")) <= 0 or used.get(item[0], 0) < _int(item[1].get("LimitNum"))]
    if not available:
        available = candidates
    # The server chooses a cell-data record during map generation.  Keep that
    # choice deterministic for offline replays while respecting weights and
    # the configured per-record limit.
    total = sum(max(1, _int(row.get("CellWeight"), 1)) for _value, row in available)
    pick = (int(hashlib.sha256((str(seed) + ":" + str(index)).encode()).hexdigest()[:12], 16) % total)
    for value, row in available:
        pick -= max(1, _int(row.get("CellWeight"), 1))
        if pick < 0:
            used[value] = used.get(value, 0) + 1
            return value
    return _int(available[-1][0])


def _remaining_magic_pod(data):
    return {
        "cells": list(data.get("cells", [])),
        "currDialog": int(data.get("currDialog", 0)),
        "mapId": int(data.get("mapId", 0)),
        "role": {
            "atk": int(data.get("atk", 10)),
            "def": int(data.get("def", 10)),
            "hp": int(data.get("hp", 100)),
            "cellId": int(data.get("cellId", 0)),
            "equipments": list(data.get("equipments", [])),
            "key1": int(data.get("key1", 0)),
            "key2": int(data.get("key2", 0)),
            "key3": int(data.get("key3", 0)),
        },
    }


def _remaining_mining_pod(data):
    floor = int(data.get("floor", 1))
    row = module_rules._row("mining", "MiningLayerTable", floor) or {}
    width = max(1, int(row.get("GridNumX", 20) or 20))
    height = max(1, int(row.get("GridNumY", 7) or 7))
    expected = width * height
    stored = data.get("grids")
    if not isinstance(stored, dict) or len(stored) != expected:
        _mining_layout(data, floor)
    elements = [
        int(value[0]) for value in (row.get("Element") or [])
        if isinstance(value, list) and value and int(value[0]) > 0
    ] or [1]
    skins = [int(value) for value in (row.get("LandSkinID") or []) if int(value) > 0] or [0]
    grids = {}
    stored = data.setdefault("grids", {})
    for index in range(1, width * height + 1):
        key = str(index)
        grid = data.setdefault("grids", {}).setdefault(
            key, {"id": index, "dataCid": elements[(index - 1) % len(elements)],
                  "skinId": skins[(index - 1) % len(skins)], "state": 0,
                  "x": (index - 1) % width, "y": (index - 1) // width},
        )
        grid.setdefault("dataCid", elements[(index - 1) % len(elements)])
        grid.setdefault("skinId", skins[(index - 1) % len(skins)])
        grid.setdefault("state", 0)
        grid["id"], grid["x"], grid["y"] = index, (index - 1) % width, (index - 1) // width
        grids[index] = dict(grid)
    return {"floor": int(data.get("floor", 1)), "grids": grids}


def _remaining_flight_pod(data):
    return {
        "id": int(data.get("mechaId", 1)),
        "firingSpeed": float(data.get("firingSpeed", 1.0)),
        "growthAttribute": {
            int(key): float(value)
            for key, value in (data.get("growthAttribute", {}) or {}).items()
        },
    }


def _record_remaining_action(module, request_id, values):
    action = module.setdefault("actions", {}).setdefault(str(request_id), {"count": 0})
    action["count"] = int(action.get("count", 0)) + 1
    action["lastValues"] = values
    action["lastTime"] = _stamp()
    module.setdefault("data", {})["lastRequest"] = request_id


def _dream_reset_state(data, map_id):
    row = module_rules._row("dream_map", "DreamMapListTable", map_id)
    raw_cells = (row or {}).get("DreamMapData") or []
    cells = {}
    first = None
    used_cell_data = {}
    seed = "%d:%d" % (int(map_id), int(data.get("resetCount", 0)))
    for index in range(0, len(raw_cells) - 2, 3):
        try:
            x, y, data_id = (int(raw_cells[index]), int(raw_cells[index + 1]), int(raw_cells[index + 2]))
        except (TypeError, ValueError):
            continue
        if x < 0 or y < 0:
            continue
        if first is None:
            first = (x, y)
        actual_data_id = _dream_generated_cell_id(data_id, seed, index // 3, used_cell_data)
        cells[f"{x}:{y}"] = {
            "x": x, "y": y, "dataId": actual_data_id, "elementId": 0,
            "isOpen": False, "markType": 0, "showType": True,
        }
    if not cells:
        return False
    start_x, start_y = first
    data.update({
        "active": True, "mapId": int(map_id), "cells": cells,
        "combo": 0, "currDialog": 0, "resetCount": 0,
        "roleX": start_x, "roleY": start_y,
        "movePoint": max(1, int((row or {}).get("FirstAP", 10) or 10)),
    })
    return True


def _dream_execution_items(session, uid, cell):
    cell_data = module_rules._row("dream_map", "DreamMapCellDataTable", cell.get("dataId")) or {}
    executions = []
    for service_id in cell_data.get("ServiceList") or []:
        service = module_rules._row("dream_map", "DreamMapServiceTable", service_id) or {}
        execution_type = _int(service.get("ExecutionType"))
        params = service.get("ExecutionParams") or []
        item_pairs = []
        special_id = 0
        if execution_type == 2 and len(params) >= 3:
            item_pairs = [(_int(params[1]), _int(params[2]))]
            applied = storage.grant_reward_pairs(uid, item_pairs)
            if applied is not None:
                _send_reward_changes(session, applied)
        elif execution_type == 3 and params:
            special_id = _int(params[0])
            reward = module_rules._row("dream_map", "DreamMapSPRewardDataTable", special_id) or {}
            if reward:
                pair = [(_int(reward.get("RewardID")), _int(reward.get("RewardNum")))]
                applied = storage.grant_reward_pairs(uid, pair)
                if applied is not None:
                    _send_reward_changes(session, applied)
        elif execution_type == 6 and params:
            cell["dialogId"] = _int(params[0])
        elif execution_type == 8 and params:
            cell["unlockMapId"] = _int(params[0])
        elif execution_type == 9 and params:
            cell["unlockTaskId"] = _int(params[0])
        if execution_type in (2, 3) or item_pairs or special_id:
            executions.append({
                "exectionId": _int(service_id), "getItems": _remaining_item_show(item_pairs),
                "getMovePoint": 0, "getSpAwardId": special_id,
            })
    return executions


def _handle_dream_map_action(session, uid, request_id, result_id, values):
    state, module = _remaining_state(uid, "net_dreamMap")
    data = module.setdefault("data", {})
    if request_id == 5902:
        (map_id,) = values
        if int(map_id) <= 0 or module_rules._row("dream_map", "DreamMapListTable", map_id) is None:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_dream_pod(data)))
            return True
        if data.get("active") and int(data.get("mapId", 0)) != int(map_id):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_dream_pod(data)))
            return True
        if not data.get("active") and not _dream_reset_state(data, int(map_id)):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_dream_pod(data)))
            return True
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _remaining_dream_pod(data)))
        return True

    if not data.get("active") or module_rules._row("dream_map", "DreamMapListTable", data.get("mapId")) is None:
        session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_dream_pod(data)))
        return True
    if request_id == 5903:
        if int(data.get("resetCount", 0)) >= 99 or not _dream_reset_state(data, int(data.get("mapId"))):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_dream_pod(data)))
            return True
        data["resetCount"] = int(data.get("resetCount", 0)) + 1
        row = module_rules._row("dream_map", "DreamMapListTable", data.get("mapId")) or {}
        data["movePoint"] = max(1, int(row.get("ResetAP", data.get("movePoint", 1)) or 1))
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _remaining_dream_pod(data)))
        return True
    if request_id == 5904:
        x, y = (int(values[0]), int(values[1]))
        key = f"{x}:{y}"
        cell = data.setdefault("cells", {}).get(key)
        if cell is None or bool(cell.get("isOpen")):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, [], int(data.get("combo", 0)), int(data.get("movePoint", 0)), x, y))
            return True
        distance = max(1, abs(x - int(data.get("roleX", 0))) + abs(y - int(data.get("roleY", 0))))
        if distance > int(data.get("movePoint", 0)):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, [], int(data.get("combo", 0)), int(data.get("movePoint", 0)), x, y))
            return True
        cell["isOpen"] = True
        data["roleX"], data["roleY"] = x, y
        data["movePoint"] = int(data.get("movePoint", 0)) - distance
        data["combo"] = int(data.get("combo", 0)) + 1
        executions = _dream_execution_items(session, uid, cell)
        if cell.get("dialogId"):
            data["currDialog"] = int(cell["dialogId"])
            session.send(1604, protocol_codec.encode_method(1604, int(cell["dialogId"])))
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, executions, int(data["combo"]), int(data["movePoint"]), x, y))
        return True
    if request_id == 5908:
        dialog, skip = values
        if int(dialog) < 0 or not isinstance(skip, list) or len(skip) > 64 or not all(isinstance(value, int) and value >= 0 for value in skip):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, -1))
            return True
        data["currDialog"] = 0
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, -1))
        return True
    if request_id == 5911:
        x, y, mark_type = (int(values[0]), int(values[1]), int(values[2]))
        cell = data.setdefault("cells", {}).get(f"{x}:{y}")
        if cell is None or mark_type < 0 or mark_type > 3:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, x, y, mark_type))
            return True
        cell["markType"] = mark_type
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, x, y, mark_type))
        return True
    return False


def _magic_choose_cell_data(cell_type, seed, index, used):
    """Resolve a map CellID to the weighted MagicTowerCellDataTable row."""
    table = module_rules._row("magic_tower", "MagicTowerMapCellTable", cell_type) or {}
    candidates = []
    for value in table.get("DataList") or []:
        row = module_rules._row("magic_tower", "MagicTowerCellDataTable", value)
        if row is not None:
            candidates.append((_int(value), row))
    if not candidates:
        return 0
    available = [
        item for item in candidates
        if _int(item[1].get("LimitNum")) <= 0
        or used.get(item[0], 0) < _int(item[1].get("LimitNum"))
    ]
    # Some shipped CellID groups contain more map cells than the sum of their
    # LimitNum values.  Preserve a valid node after the configured quota is
    # exhausted, while still applying the quota to the normal selection path.
    if not available:
        available = candidates
    total = sum(max(1, _int(row.get("CellWeight"), 1)) for _value, row in available)
    digest = hashlib.sha256((str(seed) + ":" + str(index)).encode("utf-8")).hexdigest()
    pick = int(digest[:16], 16) % total
    for value, row in available:
        pick -= max(1, _int(row.get("CellWeight"), 1))
        if pick < 0:
            used[value] = used.get(value, 0) + 1
            return value
    return _int(available[-1][0])


def _magic_reset_state(data, map_id):
    map_row = module_rules._row("magic_tower", "MagicTowerMapTable", map_id)
    if map_row is None:
        return False
    floors = [_int(value) for value in (map_row.get("MagicTowerFloorList") or []) if _int(value) > 0]
    if not floors:
        return False
    cells = []
    used = {}
    next_id = 1
    seed = "%d:%d" % (int(map_id), int(data.get("resetCount", 0)))
    for floor_id in floors:
        floor_row = module_rules._row("magic_tower", "MagicTowerFloorListTable", floor_id)
        if floor_row is None:
            return False
        map_data = floor_row.get("MapData") or []
        cell_refs = floor_row.get("CellID") or []
        for index, map_cell_type in enumerate(map_data):
            cell_ref = cell_refs[index] if index < len(cell_refs) else map_cell_type
            data_id = _magic_choose_cell_data(
                cell_ref, "%s:%d" % (seed, floor_id), index, used
            )
            if data_id <= 0:
                return False
            cells.append({
                "id": next_id,
                "dataId": data_id,
                "floor": floor_id,
                "x": index,
                "y": 0,
            })
            next_id += 1
    if not cells:
        return False
    data.update({
        "active": True,
        "mapId": int(map_id),
        "floor": floors[0],
        "cells": cells,
        "currDialog": 0,
        "completedCells": [],
        "pendingCell": 0,
        "pendingIndex": -1,
        "dialogPending": None,
        "settled": False,
        "role": {
            "atk": _int(map_row.get("InitialAtk"), 100),
            "def": _int(map_row.get("InitialDef"), 50),
            "hp": _int(map_row.get("InitialHP"), 2000),
            "cellId": int(cells[0]["id"]),
            "equipments": [0, 0],
            "key1": 0,
            "key2": 0,
            "key3": 0,
        },
    })
    return True


def _magic_cell(data, cell_id):
    return next(
        (cell for cell in data.get("cells", [])
         if isinstance(cell, dict) and _int(cell.get("id")) == _int(cell_id)),
        None,
    )


def _magic_index_xy(data, cell):
    floor = _int(cell.get("floor"))
    floor_cells = [
        value for value in data.get("cells", [])
        if isinstance(value, dict) and _int(value.get("floor")) == floor
    ]
    step_counts = {}
    for value in floor_cells:
        x = _int(value.get("x"))
        step_counts[x] = step_counts.get(x, 0) + 1
    max_step = max(step_counts.values() or [1])
    first_y = max_step // 2 + 1
    step_count = step_counts.get(_int(cell.get("x")), 1)
    index_x = _int(cell.get("x"))
    index_y = first_y - ((step_count - 1 + 1) // 2) - 1 + _int(cell.get("y"))
    return index_x, index_y


def _magic_hex_neighbors(data, cell):
    """Mirror GameSceneUtil.GetHexagonAroundXY and NodeSeqXYToIndexXY."""
    index_x, index_y = _magic_index_xy(data, cell)
    current_x, current_y = index_x - 1, index_y - 1
    if current_x % 2 == 0:
        around = (
            (current_x, current_y + 1),
            (current_x - 1, current_y),
            (current_x + 1, current_y),
            (current_x - 1, current_y - 1),
            (current_x, current_y - 1),
            (current_x + 1, current_y - 1),
        )
    else:
        around = (
            (current_x - 1, current_y + 1),
            (current_x, current_y + 1),
            (current_x + 1, current_y + 1),
            (current_x - 1, current_y),
            (current_x + 1, current_y),
            (current_x, current_y - 1),
        )
    return {(x + 1, y + 1) for x, y in around}


def _magic_nearby(data, current, target):
    if current is None or target is None or _int(current.get("floor")) != _int(target.get("floor")):
        return False
    if _int(target.get("x")) - _int(current.get("x")) != 1:
        return False
    return _magic_index_xy(data, target) in _magic_hex_neighbors(data, current)


def _magic_role_copy(role):
    return {
        "atk": _int(role.get("atk")),
        "def": _int(role.get("def")),
        "hp": _int(role.get("hp")),
        "cellId": _int(role.get("cellId")),
        "equipments": [_int(value) for value in role.get("equipments", [])],
        "key1": _int(role.get("key1")),
        "key2": _int(role.get("key2")),
        "key3": _int(role.get("key3")),
    }


def _magic_reward_pairs(params):
    values = [_int(value) for value in (params or [])]
    return [
        (values[index], values[index + 1])
        for index in range(0, len(values) - 1, 2)
        if values[index] > 0 and values[index + 1] > 0
    ]


def _magic_run_cell(session, uid, state, module, data, cell, start_index=0):
    cell_data = module_rules._row("magic_tower", "MagicTowerCellDataTable", cell.get("dataId")) or {}
    execution_types = cell_data.get("ExecutionType") or []
    execution_params = cell_data.get("ExecutionParams") or []
    role = data.setdefault("role", {})
    final_floor = False
    for index in range(max(0, int(start_index)), len(execution_types)):
        execution_type = _int(execution_types[index])
        params = execution_params[index] if index < len(execution_params) else []
        params = [str(value) for value in (params or [])]
        data["pendingCell"] = _int(cell.get("id"))
        data["pendingIndex"] = index
        before = _magic_role_copy(role)
        rewards = []
        monster_team = 0

        if execution_type == 1:
            monster_team = _int(params[0]) if params else _int(cell_data.get("MonsterID"))
        elif execution_type == 2:
            rewards = _magic_reward_pairs(params)
            applied = storage.grant_reward_pairs(uid, rewards) if rewards else {"changed_attrs": {}, "changed_items": []}
            if rewards and applied is None:
                return False
            _send_reward_changes(session, applied)
        elif execution_type == 3:
            values = [_int(value) for value in params[:6]]
            values.extend([0] * (6 - len(values)))
            costs = values[3:6]
            can_pay = all(_int(role.get("key%d" % (key + 1))) >= costs[key] for key in range(3))
            if can_pay:
                role["hp"] = max(0, _int(role.get("hp")) + values[0])
                role["atk"] = max(0, _int(role.get("atk")) + values[1])
                role["def"] = max(0, _int(role.get("def")) + values[2])
                for key in range(3):
                    role["key%d" % (key + 1)] = _int(role.get("key%d" % (key + 1))) - costs[key]
        elif execution_type == 4:
            dialog = _int(params[0]) if params else 0
            if dialog > 0:
                data["currDialog"] = dialog
                data["dialogPending"] = {"cellId": _int(cell.get("id")), "index": index}
        elif execution_type == 6:
            equip_id = _int(params[0]) if params else 0
            equip = module_rules._row("magic_tower", "MagicTowerEquipTable", equip_id) or {}
            equip_type = _int(equip.get("EquipType"))
            if equip_id > 0 and equip_type in (1, 2):
                equipments = [_int(value) for value in role.get("equipments", [])]
                while len(equipments) < 2:
                    equipments.append(0)
                slot = equip_type - 1
                old = module_rules._row("magic_tower", "MagicTowerEquipTable", equipments[slot]) or {}
                role["atk"] = _int(role.get("atk")) + _int(equip.get("Atk")) - _int(old.get("Atk"))
                role["def"] = _int(role.get("def")) + _int(equip.get("Def")) - _int(old.get("Def"))
                equipments[slot] = equip_id
                role["equipments"] = equipments
        elif execution_type == 7:
            floors = [_int(value) for value in (module_rules._row("magic_tower", "MagicTowerMapTable", data.get("mapId")) or {}).get("MagicTowerFloorList", [])]
            current_floor = _int(cell.get("floor"))
            next_floor = next((value for value in floors if value > current_floor), None)
            if next_floor is None:
                final_floor = True
            else:
                next_cell = next((value for value in data.get("cells", []) if _int(value.get("floor")) == next_floor), None)
                if next_cell is None:
                    return False
                role["cellId"] = _int(next_cell.get("id"))
                data["floor"] = next_floor
        elif execution_type == 8:
            values = [_int(value) for value in params[:3]]
            values.extend([0] * (3 - len(values)))
            for key in range(3):
                role["key%d" % (key + 1)] = _int(role.get("key%d" % (key + 1))) + values[key]

        item = {"executionType": execution_type, "params": params}
        if rewards:
            item["getItems"] = _remaining_item_show(rewards)
        if role != before:
            item["role"] = _magic_role_copy(role)
        session.send(6608, protocol_codec.encode_method(6608, item))

        if execution_type == 1:
            if not module_rules._start_module_battle(
                session, uid, "magic_tower", _int(cell.get("id")),
                _int(data.get("mapId")), monster_team, [], battle_type=4,
            ):
                data["active"] = False
                session.send(6609, protocol_codec.encode_method(6609, False))
                storage.update_player_state_json(uid, "remaining_modules", state)
                return False
            storage.update_player_state_json(uid, "remaining_modules", state)
            return True
        if execution_type == 4 and _int(data.get("currDialog")) > 0:
            session.send(6612, protocol_codec.encode_method(6612, _int(data["currDialog"])))
            storage.update_player_state_json(uid, "remaining_modules", state)
            return True

    completed = data.setdefault("completedCells", [])
    if _int(cell.get("id")) not in [_int(value) for value in completed]:
        completed.append(_int(cell.get("id")))
    data["pendingCell"], data["pendingIndex"] = 0, -1
    data["dialogPending"] = None
    if final_floor:
        data["active"], data["settled"] = False, True
        session.send(6609, protocol_codec.encode_method(6609, True))
    storage.update_player_state_json(uid, "remaining_modules", state)
    return True


def magic_tower_battle_complete(session, uid, win):
    """Resume or settle a Magic Tower execution after the common fight flow."""
    state, module = _remaining_state(uid, "net_magicTower")
    data = module.setdefault("data", {})
    if not data.get("active") or _int(data.get("pendingCell")) <= 0:
        return False
    cell = _magic_cell(data, data.get("pendingCell"))
    if cell is None:
        return False
    if not win:
        data["active"], data["settled"] = False, True
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(6609, protocol_codec.encode_method(6609, False))
        return True
    return _magic_run_cell(
        session, uid, state, module, data, cell, _int(data.get("pendingIndex")) + 1
    )


def _handle_magic_tower_action(session, uid, request_id, result_id, values):
    state, module = _remaining_state(uid, "net_magicTower")
    data = module.setdefault("data", {})
    if request_id == 6602:
        (map_id,) = values
        if int(map_id) <= 0 or module_rules._row("magic_tower", "MagicTowerMapTable", map_id) is None:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_magic_pod(data)))
            return True
        if data.get("active") and int(data.get("mapId", 0)) != int(map_id):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_magic_pod(data)))
            return True
        if not data.get("active"):
            day = time.strftime("%Y-%m-%d", time.localtime())
            if data.get("day") != day:
                data["day"], data["enterCount"] = day, 0
            if int(data.get("enterCount", 0)) >= 3 or not _magic_reset_state(data, int(map_id)):
                session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_magic_pod(data)))
                return True
            data["enterCount"] = int(data.get("enterCount", 0)) + 1
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _remaining_magic_pod(data)))
        return True
    if request_id == 6603:
        if not data.get("active"):
            session.send(result_id, protocol_codec.encode_method(result_id, 1))
            return True
        data["active"], data["mapId"] = False, 0
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0))
        return True
    if not data.get("active"):
        session.send(result_id, protocol_codec.encode_method(result_id, 1, 0) if request_id == 6604 else protocol_codec.encode_method(result_id, 1, -1))
        return True
    if request_id == 6604:
        (cell_id,) = values
        current = _magic_cell(data, data.get("role", {}).get("cellId", 0))
        target = _magic_cell(data, cell_id)
        completed = {_int(value) for value in data.get("completedCells", [])}
        if (
            current is None
            or target is None
            or _int(target.get("id")) == _int(current.get("id"))
            or _int(target.get("id")) in completed
            or not _magic_nearby(data, current, target)
        ):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, int(cell_id)))
            return True
        data.setdefault("role", {})["cellId"] = _int(cell_id)
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, int(cell_id)))
        if not _magic_run_cell(session, uid, state, module, data, target, 0):
            return True
        return True
    if request_id == 6610:
        dialog, skip = values
        pending = data.get("dialogPending") or {}
        if (
            _int(dialog) <= 0
            or _int(data.get("currDialog")) != _int(dialog)
            or not isinstance(skip, list)
            or len(skip) > 64
            or not all(isinstance(value, int) and value >= 0 for value in skip)
        ):
            return False
        data["currDialog"] = 0
        data["dialogPending"] = None
        _record_remaining_action(module, request_id, values)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, 0))
        cell = _magic_cell(data, pending.get("cellId"))
        if cell is not None:
            _magic_run_cell(session, uid, state, module, data, cell, _int(pending.get("index")) + 1)
        else:
            storage.update_player_state_json(uid, "remaining_modules", state)
        return True
    return False


def _mining_new_state(data, floor):
    row = module_rules._row("mining", "MiningLayerTable", floor)
    if row is None:
        return False
    generation = _int(data.get("generation")) + 1
    data.clear()
    data.update({
        "active": True,
        "floor": int(floor),
        "generation": generation,
        "grids": {},
        "currDialog": 0,
        "dialogGrid": 0,
        "pendingBattleGrid": 0,
    })
    _mining_layout(data, int(floor))
    return True


def _mining_layout(data, floor):
    row = module_rules._row("mining", "MiningLayerTable", floor) or {}
    width = max(1, _int(row.get("GridNumX"), 20))
    height = max(1, _int(row.get("GridNumY"), 7))
    capacity = width * height
    entries = []
    for entry in row.get("Element") or []:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        element_id, minimum, maximum = (_int(entry[0]), _int(entry[1]), _int(entry[2]))
        if element_id <= 0 or minimum < 0 or maximum < minimum:
            continue
        digest = hashlib.sha256(
            ("%s:%s:%s" % (floor, data.get("generation", 0), element_id)).encode("utf-8")
        ).hexdigest()
        count = minimum + (int(digest[:12], 16) % (maximum - minimum + 1))
        entries.append((element_id, minimum, count))

    cells = []
    for element_id, minimum, count in entries:
        for _ in range(minimum):
            cells.append(element_id)
        for _ in range(max(0, count - minimum)):
            if len(cells) >= capacity:
                break
            cells.append(element_id)
        if len(cells) >= capacity:
            break
    seed = int(hashlib.sha256(
        ("%s:%s" % (floor, data.get("generation", 0))).encode("utf-8")
    ).hexdigest()[:16], 16)
    rng = random.Random(seed)
    rng.shuffle(cells)
    positions = list(range(1, capacity + 1))
    rng.shuffle(positions)
    positions = sorted(positions[:len(cells)])
    skins = [_int(value) for value in (row.get("LandSkinID") or []) if _int(value) > 0] or [0]
    data["grids"] = {
        str(position): {
            "id": position,
            "dataCid": int(element_id),
            "skinId": int(skins[(position - 1) % len(skins)]),
            "state": 0,
            "x": (position - 1) // height,
            "y": (position - 1) % height,
        }
        for position, element_id in zip(positions, cells)
    }


def _mining_element(grid):
    return module_rules._row("mining", "MiningElementTable", grid.get("dataCid")) or {}


def _mining_reward_pairs(element):
    # Reward is a server-side reward-group ID in this client package.  The
    # group ID must never be inserted as if it were an item template.
    show = module_rules._pairs(element.get("RewardShow"))
    if show:
        return _known_mining_items(show)

    # Captured outcomes are an empirical fallback, not a claim that the
    # server-side reward distribution is complete. Repeated samples remain in
    # the pool so later captures can approximate observed weights.
    reward_group = str(_int(element.get("Reward")))
    group = (OFFICIAL_CAPTURE_OBSERVATIONS.get("mining_reward_groups") or {}).get(reward_group) or {}
    outcomes = []
    for sample in group.get("samples") or []:
        raw_items = sample.get("items") if isinstance(sample, dict) else None
        if isinstance(raw_items, list) and raw_items and all(isinstance(item, list) for item in raw_items):
            pairs = [
                (_int(item[0]), _int(item[1]))
                for item in raw_items
                if len(item) >= 2 and _int(item[0]) > 0 and _int(item[1]) > 0
            ]
        else:
            pairs = module_rules._pairs(raw_items)
        if pairs:
            outcomes.append(pairs)
    if outcomes:
        return _known_mining_items(random.choice(outcomes))
    return _known_mining_items(LOCAL_MINING_REWARD_GROUPS.get(_int(element.get("Reward")), []))


def _known_mining_items(pairs):
    """Drop malformed or unknown item CIDs before ItemShowPOD encoding.

    ``analysis/module_config.json`` is generated from the client item tables.
    Older installations may not have that optional catalog yet, so an absent
    catalog keeps the existing local behavior while a present catalog is a
    strict guard against reward-group IDs leaking to the client.
    """
    catalog = module_rules.MODULE_CONFIG.get("items")
    if not isinstance(catalog, dict) or not catalog:
        return [(_int(cid), _int(quantity)) for cid, quantity in pairs
                if _int(cid) > 0 and _int(quantity) > 0]
    known = set()
    for table in catalog.values():
        if isinstance(table, dict):
            known.update(_int(cid) for cid in table if _int(cid) > 0)
    return [(_int(cid), _int(quantity)) for cid, quantity in pairs
            if _int(cid) in known and _int(quantity) > 0]


def _mining_cost(element, layer=False):
    values = (module_rules._row("mining", "MiningLayerTable", element) or {}).get("Cost") if layer else element.get("Cost")
    return module_rules._pairs(values)


def _mining_apply_cost_reward(session, uid, cost, rewards):
    result = storage.trade_reward_pairs(uid, list(cost or []), list(rewards or []))
    if result is not None:
        _send_reward_changes(session, result)
    return result


def _mining_complete_grid(data, grid_id):
    grid = data.setdefault("grids", {}).get(str(_int(grid_id)))
    if grid is not None:
        grid["state"] = 2
    return grid


def mining_battle_complete(session, uid, grid_id, win, item_shows):
    state, module = _remaining_state(uid, "net_mining")
    data = module.setdefault("data", {})
    grid = data.setdefault("grids", {}).get(str(_int(grid_id)))
    if grid is None or _int(grid.get("state")) != 1:
        return False
    if win:
        _mining_complete_grid(data, grid_id)
    data["pendingBattleGrid"] = 0
    storage.update_player_state_json(uid, "remaining_modules", state)
    session.send(7615, protocol_codec.encode_method(
        7615, bool(win), {}, list(item_shows or [])
    ))
    if win and item_shows:
        session.send(7616, protocol_codec.encode_method(7616, list(item_shows)))
    return True


def _handle_mining_action(session, uid, request_id, result_id, values):
    state, module = _remaining_state(uid, "net_mining")
    data = module.setdefault("data", {})
    if request_id == 7602:
        if data.get("active"):
            _record_remaining_action(module, request_id, values)
            storage.update_player_state_json(uid, "remaining_modules", state)
            session.send(result_id, protocol_codec.encode_method(result_id, 0, _remaining_mining_pod(data)))
            return True
        floor = int(data.get("floor", 1) or 1)
        row = module_rules._row("mining", "MiningLayerTable", floor)
        if row is None or not _mining_new_state(data, floor):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, _remaining_mining_pod(data)))
            return True
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, _remaining_mining_pod(data)))
        return True
    if not data.get("active"):
        if request_id == 7603:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, 0))
        elif request_id == 7604:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, []))
        else:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, int(values[0]) if values else 0))
        return True
    grids = _remaining_mining_pod(data)["grids"]
    if request_id == 7603:
        (grid_id,) = values
        grid = grids.get(int(grid_id))
        if grid is None or int(grid.get("state", 0)) != 0:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, int(grid_id)))
            return True
        layer_cost = _mining_cost(int(data.get("floor", 1)), layer=True)
        applied = _mining_apply_cost_reward(session, uid, layer_cost, [])
        if applied is None:
            session.send(result_id, protocol_codec.encode_method(result_id, 1, int(grid_id)))
            return True
        element = _mining_element(grid)
        if _int(element.get("Type")) == 9:
            _mining_complete_grid(data, grid_id)
        else:
            data.setdefault("grids", {}).setdefault(str(int(grid_id)), {})["state"] = 1
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, int(grid_id)))
        if _int(element.get("Type")) == 9:
            session.send(7611, protocol_codec.encode_method(7611, int(grid_id)))
        return True
    if request_id == 7604:
        count, element_type = (int(values[0]), int(values[1]))
        if count <= 0 or count > len(grids) or element_type not in (0, 1):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, []))
            return True
        selected = []
        completed = []
        collected = []
        for grid_id, grid in grids.items():
            state_value = _int(grid.get("state"))
            element = _mining_element(grid)
            element_kind = _int(element.get("Type"))
            eligible = state_value == 0 or (
                element_type == 1 and state_value == 1 and 2 <= element_kind <= 5
            )
            if not eligible:
                continue
            cost = _mining_cost(int(data.get("floor", 1)), layer=True) if state_value == 0 else _mining_cost(element)
            rewards = _mining_reward_pairs(element) if state_value == 1 else []
            applied = _mining_apply_cost_reward(session, uid, cost, rewards)
            if applied is None:
                break
            selected.append(int(grid_id))
            if state_value == 1:
                _mining_complete_grid(data, grid_id)
                completed.append(int(grid_id))
                collected.extend(_remaining_item_show(rewards))
            elif element_kind == 9:
                _mining_complete_grid(data, grid_id)
                completed.append(int(grid_id))
            else:
                data.setdefault("grids", {}).setdefault(str(int(grid_id)), {})["state"] = 1
            if len(selected) >= count:
                break
        if len(selected) != count:
            if not selected:
                session.send(result_id, protocol_codec.encode_method(result_id, 1, []))
                return True
            _record_remaining_action(module, request_id, values)
            storage.update_player_state_json(uid, "remaining_modules", state)
            session.send(result_id, protocol_codec.encode_method(result_id, 0, selected))
            for grid_id in completed:
                session.send(7611, protocol_codec.encode_method(7611, grid_id))
            if collected:
                session.send(7616, protocol_codec.encode_method(7616, collected))
            return True
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, selected))
        for grid_id in completed:
            session.send(7611, protocol_codec.encode_method(7611, grid_id))
        if collected:
            session.send(7616, protocol_codec.encode_method(7616, collected))
        return True
    if request_id == 7605:
        grid_id, formation_id = (int(values[0]), int(values[1]))
        grid = grids.get(grid_id)
        element = _mining_element(grid or {})
        element_type = _int(element.get("Type"))
        if grid is None or _int(grid.get("state")) != 1 or (element_type == 10 and formation_id <= 0):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, grid_id))
            return True
        if element_type == 10:
            params = element.get("Parameter") or []
            monster_team = _int(params[0]) if params else 0
            rewards = _mining_reward_pairs(element)
            if not module_rules._start_module_battle(
                session, uid, "mining", grid_id, int(data.get("floor", 1)),
                monster_team, rewards, battle_type=4,
            ):
                session.send(result_id, protocol_codec.encode_method(result_id, 1, grid_id))
                return True
            data["pendingBattleGrid"] = grid_id
        else:
            cost = _mining_cost(element)
            rewards = _mining_reward_pairs(element)
            applied = _mining_apply_cost_reward(session, uid, cost, rewards)
            if applied is None:
                session.send(result_id, protocol_codec.encode_method(result_id, 1, grid_id))
                return True
            _mining_complete_grid(data, grid_id)
            if rewards:
                session.send(7616, protocol_codec.encode_method(7616, _remaining_item_show(rewards)))
            if element_type == 6 and int(data.get("floor", 1)) < 5:
                _mining_new_state(data, int(data.get("floor", 1)) + 1)
                session.send(7613, protocol_codec.encode_method(7613, _remaining_mining_pod(data)))
            elif element_type == 7 and int(data.get("floor", 1)) > 1:
                _mining_new_state(data, int(data.get("floor", 1)) - 1)
                session.send(7613, protocol_codec.encode_method(7613, _remaining_mining_pod(data)))
            elif element_type == 11:
                params = element.get("Parameter") or []
                dialog = _int(params[0]) if params else 0
                if dialog > 0:
                    data["currDialog"], data["dialogGrid"] = dialog, grid_id
                    session.send(7614, protocol_codec.encode_method(7614, dialog))
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, grid_id))
        return True
    if request_id == 7606:
        dialog, skip = values
        if (
            _int(dialog) <= 0
            or _int(data.get("currDialog")) != _int(dialog)
            or not isinstance(skip, list)
            or len(skip) > 64
            or not all(isinstance(value, int) and value >= 0 for value in skip)
        ):
            session.send(result_id, protocol_codec.encode_method(result_id, 1, 0))
            return True
        data["currDialog"] = 0
        grid_id = _int(data.get("dialogGrid"))
        data["dialogGrid"] = 0
        if grid_id > 0:
            _mining_complete_grid(data, grid_id)
        _record_remaining_action(module, request_id, values)
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, 0))
        if grid_id > 0:
            session.send(7611, protocol_codec.encode_method(7611, grid_id))
        return True
    return False


def _remaining_protocol_value(type_name, request_values, module, reward_pairs):
    if type_name in ("int", "long"):
        return 0
    if type_name == "bool":
        return False
    if type_name == "string":
        return ""
    generic = protocol_codec.split_generic(type_name)
    if generic:
        outer, inner = generic
        if outer == "list":
            if inner == "ItemShowPOD":
                return _remaining_item_show(reward_pairs)
            if inner == "int" and isinstance(module.get("data", {}).get("items"), list):
                return list(module["data"]["items"])
            return []
        if outer == "map":
            return {}
    if type_name == "OperationEventDataPOD":
        return _remaining_operation_data(request_values, module.get("name", ""))
    if type_name == "DreamMapDataPOD":
        return _remaining_dream_pod(module.setdefault("data", {}))
    if type_name == "MagicTowerMapDataPOD":
        return _remaining_magic_pod(module.setdefault("data", {}))
    if type_name == "MiningLayerPOD":
        return _remaining_mining_pod(module.setdefault("data", {}))
    if type_name == "FlightChallengeMechaPOD":
        return _remaining_flight_pod(module.setdefault("data", {}))
    if type_name in protocol_codec.POD_TYPES:
        return {}
    raise ValueError("unsupported remaining module type: %s" % type_name)


def _remaining_key(request_id, values):
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "%d:%s" % (request_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16])


def _mall_time(value):
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(str(value), "%Y/%m/%d %H:%M:%S")))
    except (TypeError, ValueError, OverflowError):
        return None


def _mall_in_right_time(row, now=None):
    now = int(time.time()) if now is None else int(now)
    if _int(row.get("TimeLimitType")) == 0:
        return True
    opened = _mall_time(row.get("TimeLimitOpen"))
    ended = _mall_time(row.get("TimeLimitEnd"))
    return opened is not None and ended is not None and opened <= now <= ended


def _mall_period(row, now=None):
    now = time.localtime(int(time.time()) if now is None else int(now))
    limit_type = _int(row.get("LimitType"))
    if limit_type == 1:
        return time.strftime("day:%Y-%m-%d", now)
    if limit_type == 3:
        return time.strftime("week:%G-%V", now)
    if limit_type == 4:
        return time.strftime("month:%Y-%m", now)
    return "life"


def _mall_state(uid):
    state = storage.get_player_state_json(uid, "mall") or {}
    state.setdefault("purchases", {})
    return state


def _mall_purchased(state, mall_id, period):
    row = state.setdefault("purchases", {}).get(str(mall_id), {})
    if not isinstance(row, dict) or row.get("period") != period:
        return 0
    return max(0, _int(row.get("count")))


def _mall_record(state, mall_id, period, count):
    state.setdefault("purchases", {})[str(mall_id)] = {
        "period": period, "count": _mall_purchased(state, mall_id, period) + int(count),
        "updatedAt": _stamp(),
    }


def _mall_pairs(row):
    items = row.get("Item") or []
    quantities = row.get("ItemNum") or []
    return [(_int(cid), _int(quantities[index])) for index, cid in enumerate(items)
            if index < len(quantities) and _int(cid) > 0 and _int(quantities[index]) > 0]


LOCAL_MALL_ALIASES = {
    # This client build sends a compatibility ID for Philodoxy's outfit token
    # exchange. Resolve it to the matching SellType=1 row. The RMB product that
    # shares the same dress is handled separately through PayTable[40098].
    1010910103: 1010910101,
}


def _mall_row(mall_cid):
    requested_id = _int(mall_cid)
    row = module_rules._row("mall", "MallTable", requested_id)
    if row is not None:
        return row
    source_id = LOCAL_MALL_ALIASES.get(requested_id)
    if source_id is None:
        return None
    selected = module_rules._row("mall", "MallTable", source_id)
    if selected is None:
        return None
    # The client itself is the availability signal for a compatibility ID.
    # Preserve the requested ID for limits/order keys while using the real
    # source product's reward and price configuration.
    resolved = dict(selected)
    resolved.update({
        "Id": requested_id,
        "SourceMallId": int(source_id),
        "TimeLimitType": 0,
        "ConditionId": 0,
        "ShowConditionId": 0,
    })
    log.info("mall config alias requested=%s selected=%s", requested_id, source_id)
    return resolved


WHISPER_OATH_CONDITION_SOULS = {
    # CfgConditionTable 26075001..26075049: Type=1 SubType=27 (soul oath),
    # Params=[soulId] Value=1. Purchasable once the matching soul is sworn.
    "26075001": 20010001, "26075002": 20010002, "26075003": 20010003,
    "26075004": 20010004, "26075005": 20010005, "26075006": 20010006,
    "26075007": 20010007, "26075008": 20010008, "26075009": 20010009,
    "26075010": 20010010, "26075011": 20010011, "26075012": 20010012,
    "26075013": 20010013, "26075014": 20010014, "26075015": 20010015,
    "26075016": 20010016, "26075017": 20010017, "26075018": 20010018,
    "26075019": 20010019, "26075020": 20010020, "26075021": 20010021,
    "26075022": 20010022, "26075023": 20010023, "26075024": 20010024,
    "26075025": 20010025, "26075026": 20010026, "26075027": 20010027,
    "26075028": 20010028, "26075029": 20010029, "26075030": 20010030,
    "26075031": 20010031, "26075032": 20010032, "26075033": 20010033,
    "26075034": 20010034, "26075035": 20010035, "26075036": 20010036,
    "26075037": 20010037, "26075038": 20010038, "26075039": 20010039,
    "26075040": 20010041, "26075041": 20010042, "26075042": 20010040,
    "26075043": 20010043, "26075044": 20010044, "26075045": 20010045,
    "26075046": 20010046, "26075047": 20010047, "26075048": 20010048,
    "26075049": 20010049,
}

WHISPER_UNLOCK_ITEM_WHISPER_IDS = {
    # CfgItemTable 4101001..4101048: Type=5 EffectTypeID=64,
    # EffectTypeParam=[[whisperId]]. Purchasing the corresponding mall
    # product unlocks that whisper; the token item itself is a receipt.
    "4101001": 40102, "4101002": 130102, "4101003": 190102,
    "4101004": 110102, "4101005": 70102, "4101006": 20102,
    "4101007": 60102, "4101008": 90102, "4101009": 80102,
    "4101010": 180102, "4101011": 10102, "4101012": 160102,
    "4101013": 30102, "4101014": 240102, "4101015": 50102,
    "4101016": 200102, "4101017": 120102, "4101018": 140102,
    "4101019": 170102, "4101020": 220102, "4101021": 150102,
    "4101022": 230102, "4101023": 210102, "4101024": 270102,
    "4101025": 250102, "4101026": 280102, "4101027": 100102,
    "4101028": 290102, "4101029": 300102, "4101030": 320102,
    "4101031": 330102, "4101032": 310102, "4101033": 340102,
    "4101034": 350102, "4101035": 360102, "4101036": 370102,
    "4101037": 380102, "4101038": 390102, "4101039": 410102,
    "4101040": 420102, "4101041": 400102, "4101042": 430102,
    "4101043": 440102, "4101044": 450102, "4101045": 460102,
    "4101046": 470102, "4101047": 480102, "4101048": 490102,
}


def _unlock_soul_whispers(uid):
    """List of whisper ids unlocked for the player."""
    data = storage.get_player_state_json(uid, "soulWhisperUnlocks") or {}
    values = data.get("whisperIds") if isinstance(data, dict) else None
    if not isinstance(values, list):
        values = []
    return [int(value) for value in values if str(value).isdigit()]


def _grant_soul_whisper_unlock(uid, whisper_id):
    """Persist a whisper unlock and return True when it is newly granted."""
    whisper_id = _int(whisper_id)
    if whisper_id <= 0:
        return False
    data = storage.get_player_state_json(uid, "soulWhisperUnlocks") or {}
    if not isinstance(data, dict):
        data = {}
    values = data.get("whisperIds")
    if not isinstance(values, list):
        values = []
    ids = [int(value) for value in values if str(value).isdigit()]
    if whisper_id in ids:
        return False
    ids.append(whisper_id)
    ids.sort()
    data["whisperIds"] = ids
    return _save(uid, "soulWhisperUnlocks", data)


def _mall_condition_satisfied(uid, row):
    condition_id = _int(row.get("ConditionId"))
    if condition_id <= 0:
        return True
    conditions = storage.get_player_state_json(uid, "conditions") or {}
    unlocked = conditions.get("unlocked", []) if isinstance(conditions, dict) else []
    if bool(conditions.get(str(condition_id), False)) or condition_id in unlocked:
        return True
    soul_id = WHISPER_OATH_CONDITION_SOULS.get(str(condition_id))
    if soul_id is not None:
        companion = storage.get_companion(uid, soul_id)
        return bool(companion and companion.get("oath_activation"))
    return False


def _mall_state_id(row):
    """Use the authoritative source row for limits while preserving client IDs."""
    return _int(row.get("SourceMallId")) or _int(row.get("Id"))


def _mall_purchase_plan(uid, row, count, period):
    sell_type = _int(row.get("SellType"))
    mall_id = _int(row.get("Id"))
    state_id = _mall_state_id(row)
    if sell_type not in (0, 1, 2, 3):
        log.warning("mall plan rejected uid=%s mall=%s reason=sell_type value=%s", uid, mall_id, sell_type)
        return None
    if not _mall_in_right_time(row):
        log.warning("mall plan rejected uid=%s mall=%s reason=time", uid, mall_id)
        return None
    if not _mall_condition_satisfied(uid, row):
        log.warning("mall plan rejected uid=%s mall=%s reason=condition", uid, mall_id)
        return None
    single_limit = _int(row.get("SingleBuyLimits"))
    if single_limit > 0 and count > single_limit:
        log.warning(
            "mall plan rejected uid=%s mall=%s reason=single_limit count=%s limit=%s",
            uid, mall_id, count, single_limit,
        )
        return None
    purchased = _mall_purchased(_mall_state(uid), state_id, period)
    limits = [_int(row.get("LimitTimes")), _int(row.get("BuyLimitShow"))]
    limits = [value for value in limits if value > 0]
    if limits and purchased + count > min(limits):
        log.warning(
            "mall plan rejected uid=%s mall=%s stateId=%s reason=total_limit "
            "purchased=%s count=%s limit=%s period=%s",
            uid, mall_id, state_id, purchased, count, min(limits), period,
        )
        return None

    if sell_type == 0:
        return {"kind": "free", "costs": [], "rewards": _mall_pairs(row)}
    if sell_type == 1:
        price = row.get("Price") or []
        if len(price) < 2 or _int(price[0]) <= 0 or _int(price[1]) <= 0:
            return None
        return {"kind": "item", "costs": [(_int(price[0]), _int(price[1]) * count)],
                "rewards": [(cid, quantity * count) for cid, quantity in _mall_pairs(row)]}
    if sell_type == 2:
        pay_point = _int(row.get("PayPoint"))
        if pay_point <= 0:
            return None
        return {"kind": "paypoint", "costs": [(5, pay_point * count)],
                "rewards": [(cid, quantity * count) for cid, quantity in _mall_pairs(row)]}

    pay_id = _int(row.get("PayMoney"))
    pay_row = module_rules._row("mall", "PayTable", pay_id)
    if pay_row is None:
        return None
    amount = _int(pay_row.get("Amount"))
    if amount <= 0:
        return None
    rewards = module_rules._pairs(pay_row.get("GetItems"))
    bonus_paypoint = _int(pay_row.get("GetPaypoint"))
    if bonus_paypoint > 0:
        rewards.insert(0, (5, bonus_paypoint))
    if not rewards:
        rewards = [(cid, quantity * count) for cid, quantity in _mall_pairs(row)]
    else:
        rewards = [(cid, quantity * count) for cid, quantity in rewards]
    return {"kind": "offline_payment", "amount": amount * count, "payMoney": pay_id, "rewards": rewards}


def _handle_mall_buy(session, uid, body):
    try:
        mall_cid, count = protocol_codec.decode_method(2502, body or b"")
    except (KeyError, TypeError, ValueError):
        return False
    if mall_cid <= 0 or count <= 0 or count > 99:
        return False
    row = _mall_row(mall_cid)
    if row is None:
        session.send(2503, protocol_codec.encode_method(2503, 1, mall_cid, 0, []))
        return True
    period = _mall_period(row)
    plan = _mall_purchase_plan(uid, row, int(count), period)
    if plan is None:
        session.send(2503, protocol_codec.encode_method(2503, 1, mall_cid, 0, []))
        return True
    state = _mall_state(uid)
    state_id = _mall_state_id(row)
    if plan["kind"] == "offline_payment":
        next_count = _mall_purchased(state, state_id, period) + int(count)
        order_key = "mall:%d:%s:%d" % (state_id, period, next_count)
        result = storage.trade_offline_payment(
            uid, plan["amount"], plan["rewards"], order_key,
            mall_id=state_id, period=period, count=int(count),
        )
    else:
        result = storage.trade_reward_pairs(uid,
            [(cid, quantity) for cid, quantity in plan["costs"]],
            [(cid, quantity) for cid, quantity in plan["rewards"]])
    if result is None:
        log.warning(
            "mall transaction rejected uid=%s mall=%s stateId=%s kind=%s "
            "costs=%s rewards=%d",
            uid, mall_cid, state_id, plan.get("kind"), plan.get("costs", []),
            len(plan.get("rewards", [])),
        )
        session.send(2503, protocol_codec.encode_method(2503, 1, mall_cid, 0, []))
        return True
    _send_reward_changes(session, result)
    if plan["kind"] != "offline_payment":
        _mall_record(state, state_id, period, count)
        storage.update_player_state_json(uid, "mall", state)
    for item_cid, _quantity in plan["rewards"]:
        whisper_id = WHISPER_UNLOCK_ITEM_WHISPER_IDS.get(str(item_cid))
        if whisper_id is None:
            continue
        if _grant_soul_whisper_unlock(uid, whisper_id):
            session.send(3950, protocol_codec.encode_method(3950, int(whisper_id)))
            log.info(
                "mall whisper unlock uid=%s mall=%s item=%s whisper=%s",
                uid, mall_cid, item_cid, whisper_id,
            )
    if any(int(cid) == 5 for cid, _quantity in plan.get("costs", []) or []):
        _send_base_info_update(session, uid)
    rewards = plan["rewards"] if result.get("claimed", True) else []
    session.send(2503, protocol_codec.encode_method(
        2503, 0, int(mall_cid), int(count), _remaining_item_show(rewards)
    ))
    return True


def _record_explicit_action(uid, request_id, values, extra=None):
    module_name = _remaining_module_name(request_id)
    state, module = _remaining_state(uid, module_name)
    module["name"] = module_name
    _record_remaining_action(module, request_id, values)
    if isinstance(extra, dict):
        module.setdefault("data", {}).update(extra)
    storage.update_player_state_json(uid, "remaining_modules", state)


def handle_remote_maze_order(session, uid, body):
    values = _decode_or_reject(1002, body)
    if values is None:
        return False
    maze_id, order = values
    if _int(maze_id) <= 0 or not isinstance(order, str) or not order or len(order) > 65535:
        _send(session, 1005, 1)
        return True
    _record_explicit_action(uid, 1002, values, {"lastMazeOrder": {"mazeId": _int(maze_id), "order": order}})
    _send(session, 1005, 0)
    return True


def handle_remote_battle_order(session, uid, body):
    values = _decode_or_reject(1003, body)
    if values is None:
        return False
    battle_id, order = values
    if (not isinstance(battle_id, str) or not battle_id or len(battle_id) > 1024
            or not isinstance(order, str) or not order or len(order) > 65535):
        _send(session, 1007, 1)
        return True
    _record_explicit_action(uid, 1003, values, {"lastBattleOrder": {"battleId": battle_id, "order": order}})
    _send(session, 1007, 0)
    return True


def _handle_minigame_index(session, uid, body, request_id, result_id):
    values = _decode_or_reject(request_id, body)
    if values is None:
        return False
    card_index = _int(values[0])
    if card_index < 0:
        _send(session, result_id, 1, card_index)
        return True
    _record_explicit_action(uid, request_id, values, {"lastCardCfgIndex": card_index})
    _send(session, result_id, 0, card_index)
    return True


def handle_minigame_choose_card(session, uid, body):
    return _handle_minigame_index(session, uid, body, 1702, 1703)


def handle_minigame_turntable_end(session, uid, body):
    return _handle_minigame_index(session, uid, body, 1705, 1706)


def _supply_day(now):
    # The shipped Constant.Player.DailyResetTimeHour is 04:00.
    return time.strftime("%Y-%m-%d", time.localtime(int(now) - 4 * 3600))


def handle_daily_supply(session, uid, body):
    values = _decode_or_reject(3702, body)
    if values is None:
        return False
    cid, free = _int(values[0]), bool(values[1])
    supply = {1: (12, 50), 2: (18, 50)}.get(cid)
    now = int(time.time())
    local = time.localtime(now)
    free_window = bool(supply) and local.tm_hour >= supply[0]
    paid_window = bool(supply) and local.tm_hour < 4
    state = _state(uid, "daily_supply")
    day = _supply_day(now)
    claimed = [
        _int(value) for value in state.get("claimed", [])
        if _int(value) in (1, 2)
    ] if state.get("day") == day else []
    attrs = storage.get_player_num_attrs(uid)
    current_energy = _int(attrs.get(104))
    valid = (
        supply is not None and cid not in claimed and current_energy + supply[1] <= 500
        and ((free and free_window) or (not free and paid_window))
    )
    if not valid:
        _send(session, 3703, 1, cid)
        return True
    applied = storage.claim_reward_once(
        uid, "daily_supply_claims", "%s:%d" % (day, cid), [(104, supply[1])],
    )
    if applied is None or not applied.get("claimed"):
        _send(session, 3703, 1, cid)
        return True
    _send_reward_changes(session, applied)
    claimed.append(cid)
    state.update({"day": day, "claimed": sorted(set(claimed))})
    _save(uid, "daily_supply", state)
    storage.update_player_state_json(uid, "dailySupplyList", state["claimed"])
    _record_explicit_action(uid, 3702, values)
    _send(session, 3703, 0, cid)
    return True


def handle_abyss_plus_use_rune(session, uid, body):
    values = _decode_or_reject(7107, body)
    if values is None:
        return False
    runes = values[0]
    if (not isinstance(runes, list) or len(runes) > 64
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in runes)
            or len(set(runes)) != len(runes)):
        _send(session, 7108, 1)
        return True
    state = _state(uid, "abyss_plus")
    state["usedRunes"] = list(runes)
    _save(uid, "abyss_plus", state)
    _record_explicit_action(uid, 7107, values, {"usedRunes": list(runes)})
    _send(session, 7108, 0)
    return True


def _handle_remaining_local(session, uid, request_id, result_id, body):
    if not uid:
        return False
    request_method = protocol_codec.METHODS.get(request_id)
    result_method = protocol_codec.METHODS.get(result_id)
    if request_method is None or result_method is None:
        return False
    try:
        values = protocol_codec.decode_method(request_id, body or b"")
    except (KeyError, TypeError, ValueError):
        return False

    if request_id == 2502:
        return _handle_mall_buy(session, uid, body)
    if request_id in {5902, 5903, 5904, 5908, 5911}:
        return _handle_dream_map_action(session, uid, request_id, result_id, values)
    if request_id in {6602, 6603, 6604, 6610}:
        return _handle_magic_tower_action(session, uid, request_id, result_id, values)
    if request_id in {7602, 7603, 7604, 7605, 7606}:
        return _handle_mining_action(session, uid, request_id, result_id, values)
    if request_id in REMAINING_BATTLE_REQUESTS and hasattr(session, "_handle_local_battle_entry"):
        return bool(session._handle_local_battle_entry(request_id, result_id, body or b""))

    module_name = _remaining_module_name(request_id)
    state, module = _remaining_state(uid, module_name)
    module["name"] = module_name
    action = module["actions"].setdefault(str(request_id), {"count": 0})
    action["count"] = int(action.get("count", 0)) + 1
    action["lastValues"] = values
    action["lastTime"] = _stamp()
    module["data"]["lastRequest"] = request_id

    if request_id in (4709, 7102) and hasattr(session, "_make_maze_pod"):
        formation_id = int(values[1]) if request_id == 7102 and len(values) > 1 else 0
        maze_id = int(values[0]) if values else 1
        maze_pod = session._make_maze_pod(maze_id, formation_id)
        if maze_pod is None:
            return False
        module["data"]["mazeId"] = maze_id
        storage.update_player_state_json(uid, "remaining_modules", state)
        session.send(result_id, protocol_codec.encode_method(result_id, 0, maze_pod))
        return True
    if request_id in (5902, 5903):
        if request_id == 5902:
            map_id = int(values[0]) if values else 1
            module["data"].setdefault("mapId", map_id)
        else:
            module["data"]["resetCount"] = int(module["data"].get("resetCount", 0)) + 1
            module["data"].pop("cells", None)
        module["data"].setdefault("movePoint", 10)
    elif request_id == 5904 and len(values) >= 2:
        x, y = int(values[0]), int(values[1])
        cell = module["data"].setdefault("cells", {}).setdefault(
            "%d:%d" % (x, y), {"x": x, "y": y, "dataId": 0, "elementId": 0,
                               "isOpen": False, "markType": 0, "showType": True},
        )
        cell["isOpen"] = True
        module["data"]["roleX"], module["data"]["roleY"] = x, y
        module["data"]["movePoint"] = max(0, int(module["data"].get("movePoint", 10)) - 1)
    elif request_id == 5908 and values:
        module["data"]["currDialog"] = int(values[0])
    elif request_id == 5911 and len(values) >= 3:
        module["data"].setdefault("marks", {})[str(values[0])] = int(values[2])
    elif request_id == 6602:
        module["data"].setdefault("mapId", int(values[0]) if values else 1)
    elif request_id == 6603:
        module["data"]["ended"] = True
    elif request_id == 6604 and values:
        module["data"]["cellId"] = int(values[0])
    elif request_id == 6610 and values:
        module["data"]["currDialog"] = int(values[0])
    elif request_id == 7602:
        module["data"].setdefault("floor", 1)
    elif request_id == 7603 and values:
        grid_id = int(values[0])
        module["data"].setdefault("grids", {}).setdefault(str(grid_id), {"id": grid_id})["state"] = 1
    elif request_id == 7604 and len(values) >= 2:
        module["data"]["autoExcavate"] = int(values[1])
    elif request_id == 7605 and values:
        module["data"]["lastInteract"] = int(values[0])
    elif request_id in (7606, 9412) and values:
        module["data"]["currDialog"] = int(values[0])
    elif request_id == 8002:
        module["data"].setdefault("mechaId", 1)
    elif request_id == 8003 and values:
        module["data"]["mechaId"] = int(values[0])
    elif request_id == 8004 and len(values) >= 2:
        module["data"]["level"] = int(values[0])
        module["data"]["score"] = int(values[1])
    elif request_id in (9402, 9405):
        module["data"]["active"] = True
        module["data"]["level"] = int(values[0]) if values else int(module["data"].get("level", 1))
    elif request_id in (9403, 9406):
        module["data"]["active"] = False
    elif request_id in (9802, 9806):
        module["data"]["story"] = int(values[0]) if values else int(module["data"].get("story", 0))

    reward_pairs = []
    reward_amount = REMAINING_REWARD_REQUESTS.get(request_id, 0)
    if reward_amount:
        claim_key = _remaining_key(request_id, values)
        applied = storage.claim_reward_once(
            uid, "remaining_claims", claim_key, [(1, reward_amount)]
        )
        if applied is None:
            return False
        reward_pairs = [(1, reward_amount)] if applied.get("claimed") else []
        for cid, quantity in applied.get("changed_attrs", {}).items():
            session.send(3924, protocol_codec.encode_method(3924, {int(cid): int(quantity)}))
        if applied.get("changed_items"):
            session.send(4102, protocol_codec.encode_method(4102, applied["changed_items"]))

    storage.update_player_state_json(uid, "remaining_modules", state)
    response_values = [
        _remaining_protocol_value(type_name, values, module, reward_pairs)
        for type_name in result_method["types"]
    ]
    session.send(result_id, protocol_codec.encode_method(result_id, *response_values))
    return True


def _remaining_handler(request_id, result_id, needs_body):
    if needs_body:
        return lambda session, uid, body: _handle_remaining_local(session, uid, request_id, result_id, body)
    return lambda session, uid: _handle_remaining_local(session, uid, request_id, result_id, b"")


# ── Module: local content fallback ──

def _local_default_value(type_name):
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
    raise ValueError("unsupported local content type: %s" % type_name)


def _local_content_values(result_id):
    method = protocol_codec.METHODS.get(result_id)
    if method is None:
        return None
    return [_local_default_value(type_name) for type_name in method["types"]]


def handle_local_content_fallback(session, uid, request_id, body, result_id):
    """Persist and acknowledge an otherwise unimplemented local content action."""
    if not uid or not isinstance(request_id, int) or result_id is None:
        return False
    request_method = protocol_codec.METHODS.get(request_id)
    result_method = protocol_codec.METHODS.get(result_id)
    if request_method is None or result_method is None:
        return False
    try:
        values = protocol_codec.decode_method(request_id, body or b"")
        response_values = _local_content_values(result_id)
        response_body = protocol_codec.encode_method(result_id, *response_values)
    except (KeyError, TypeError, ValueError):
        return False

    method_name = str(request_method.get("method", "local"))
    module_name, _, action_name = method_name.partition(".")
    state = storage.get_player_state_json(uid, "local_content") or {}
    modules = state.setdefault("modules", {})
    module = modules.setdefault(module_name, {"actions": {}, "history": []})
    actions = module.setdefault("actions", {})
    action = actions.setdefault(action_name or str(request_id), {"count": 0})
    action["count"] = int(action.get("count", 0)) + 1
    action["lastValues"] = values
    action["lastTime"] = _stamp()
    history = module.setdefault("history", [])
    history.append({"requestId": request_id, "resultId": result_id, "values": values, "time": action["lastTime"]})
    del history[:-20]
    storage.update_player_state_json(uid, "local_content", state)
    session.send(result_id, response_body)
    log.info("  local content action uid=%s method=%s count=%d -> %d", uid, method_name, action["count"], result_id)
    return True


# ── Module dispatch map: msg_id → (handler_fn, res_id, needs_body) ──

MODULE_DISPATCH = {
    # net_home
    1802: (handle_home_enter, 1834, False),
    1803: (handle_home_exit, 1835, False),
    1804: (handle_home_harvest_building, 1836, True),
    1805: (handle_home_harvest_land, 1837, True),
    1806: (handle_home_plant, 1838, True),
    1807: (handle_home_unlock_room, 1839, True),
    1808: (handle_home_change_suit, 1840, True),
    1809: (handle_home_visit, 1841, True),
    1810: (handle_home_trigger_plot, 1842, True),
    1811: (handle_home_unlock_suit, 1843, True),
    1812: (handle_home_cook, 1844, True),
    1813: (handle_home_cancel_cook, 1845, True),
    1814: (handle_home_reward_cook, 1846, True),
    1815: (handle_home_record_daily_action, 1847, True),
    1816: (handle_home_change_room_name, 1848, True),
    1817: (handle_home_enter_room, 1849, True),
    1818: (handle_home_switch_room_show, 1850, True),
    1819: (handle_home_receive_comfort_level, 1851, True),
    1820: (handle_home_complete_cook, 1852, True),
    1821: (handle_home_start_work, 1853, True),
    1822: (handle_home_reward_work, 1854, True),
    1823: (handle_home_complete_plant, 1855, True),
    1824: (handle_home_cancel_plant, 1856, True),
    1825: (handle_home_make, 1857, True),
    1826: (handle_home_cancel_make, 1858, True),
    1827: (handle_home_reward_make, 1859, True),
    1828: (handle_home_complete_make, 1860, True),
    1829: (handle_home_help, 1861, True),
    1830: (handle_home_open_treasure, 1862, True),
    1831: (handle_home_receive_comfort, 1863, True),
    1832: (handle_home_save_decorate, 1864, True),
    1833: (handle_home_reset_affair, 1865, True),
    1875: (handle_home_receive_letter, 1876, True),
    1877: (handle_home_update_building_level, 1879, True),
    1878: (handle_home_undo_affair, 1880, True),
    1881: (handle_home_compound, 1883, True),
    1882: (handle_home_decompose, 1884, True),
    1885: (handle_home_decompose_decorate, 1887, True),
    1886: (handle_home_unlock_land, 1888, True),
    # net_formation
    4402: (handle_formation_change_name, 4404, True),
    4403: (handle_formation_exchange_prefab, 4405, True),
    4407: (handle_formation_copy, 4408, True),
    # net_town
    2202: (handle_town_shopping, 2205, True),
    2203: (handle_town_mainline, 2206, True),
    2204: (handle_town_enter_area, 2207, True),
    # net_soulMemory
    3602: (handle_memory_activate_piece, 3606, True),
    3603: (handle_memory_experience, 3607, True),
    3604: (handle_memory_reward, 3608, True),
    3605: (handle_memory_view, 3609, True),
    # net_evilErosion
    6902: (handle_evil_wear, 6912, True),
    6903: (handle_evil_dump, 6913, True),
    6904: (handle_evil_exchange, 6914, True),
    6905: (handle_evil_upstar, 6915, True),
    6906: (handle_evil_decompose, 6916, True),
    6907: (handle_evil_position, 6917, True),
    6908: (handle_evil_change_position, 6918, True),
    6909: (handle_evil_skills, 6919, True),
    6910: (handle_evil_fight, 6920, True),
    # net_jewelry
    7702: (module_rules.handle_jewelry_wear, 7713, True),
    7703: (module_rules.handle_jewelry_unwear, 7714, True),
    7704: (module_rules.handle_jewelry_upstar, 7715, True),
    7705: (module_rules.handle_jewelry_upstar_bag, 7716, True),
    7706: (module_rules.handle_jewelry_set_speed, 7717, True),
    7707: (module_rules.handle_jewelry_recycle, 7718, True),
    7708: (module_rules.handle_jewelry_new_wear, 7719, True),
    7709: (module_rules.handle_jewelry_new_unwear, 7720, True),
    7710: (module_rules.handle_jewelry_new_speed, 7721, True),
    7711: (module_rules.handle_jewelry_new_upstar, 7722, True),
    7712: (module_rules.handle_jewelry_new_recycle, 7723, True),
    # net_restaurant
    9102: (handle_restaurant_get_info, 9121, False),
    9103: (handle_restaurant_transact_documents, 9122, False),
    9104: (handle_restaurant_practice, 9123, True),
    9105: (handle_restaurant_level_up, 9124, False),
    9106: (handle_restaurant_work, 9125, True),
    9107: (handle_restaurant_receive_income, 9126, False),
    9108: (handle_restaurant_read_burst, 9127, False),
    9109: (handle_restaurant_open_dialog, 9128, True),
    9110: (handle_restaurant_select_dialog, 9129, True),
    9111: (handle_restaurant_get_problem, 9130, True),
    9112: (handle_restaurant_answer, 9131, True),
    9113: (handle_restaurant_link_game_info, 9132, False),
    9114: (handle_restaurant_link_game, 9133, True),
    9115: (handle_restaurant_puzzle_info, 9134, False),
    9116: (handle_restaurant_puzzle, 9135, True),
    9117: (handle_restaurant_memory_flop_info, 9136, False),
    9118: (handle_restaurant_memory_flop, 9137, True),
    9119: (handle_restaurant_combat_training, 9138, True),
    9120: (handle_restaurant_boss_training, 9139, True),
    # net_amusementPark
    9302: (module_rules.handle_amusement_get_info, 9330, False),
    9303: (module_rules.handle_amusement_temporary, 9331, False),
    9304: (module_rules.handle_amusement_build, 9332, True),
    9305: (module_rules.handle_amusement_layout, 9333, True),
    9306: (module_rules.handle_amusement_confirm, 9334, True),
    9307: (module_rules.handle_amusement_level_up, 9335, True),
    9308: (module_rules.handle_amusement_random_role, 9336, False),
    9309: (module_rules.handle_amusement_recruit, 9337, True),
    9310: (module_rules.handle_amusement_role_level, 9338, True),
    9311: (lambda s, u, b: module_rules._amusement_deploy(s, u, b, True), 9339, True),
    9312: (lambda s, u, b: module_rules._amusement_deploy(s, u, b, False), 9340, True),
    9313: (module_rules.handle_amusement_open_dialog, 9341, True),
    9314: (module_rules.handle_amusement_select_dialog, 9342, True),
    9315: (module_rules.handle_amusement_income, 9343, False),
    9316: (module_rules.handle_amusement_combat, 9344, True),
    9317: (module_rules.handle_amusement_boss, 9345, True),
    9318: (module_rules.handle_amusement_read_burst, 9346, False),
    9319: (lambda s, u: module_rules._amusement_game(s, u, 9319, 9347), 9347, False),
    9320: (lambda s, u, b: module_rules._amusement_game(s, u, 9320, 9348, b), 9348, True),
    9321: (lambda s, u: module_rules._amusement_game(s, u, 9321, 9349), 9349, False),
    9322: (lambda s, u, b: module_rules._amusement_game(s, u, 9322, 9350, b), 9350, True),
    9323: (lambda s, u: module_rules._amusement_game(s, u, 9323, 9351), 9351, False),
    9324: (lambda s, u, b: module_rules._amusement_game(s, u, 9324, 9352, b), 9352, True),
    9325: (lambda s, u: module_rules._amusement_game(s, u, 9325, 9353), 9353, False),
    9326: (lambda s, u, b: module_rules._amusement_game(s, u, 9326, 9354, b), 9354, True),
    9327: (lambda s, u: module_rules._amusement_game(s, u, 9327, 9355), 9355, False),
    9328: (lambda s, u, b: module_rules._amusement_game(s, u, 9328, 9356, b), 9356, True),
    9329: (lambda s, u: module_rules._amusement_game(s, u, 9329, 9357), 9357, False),
    # net_centerGuild
    100902: (handle_guild_create, 100922, True),
    100903: (handle_guild_enter, 100923, False),
    100904: (handle_guild_exit, 100924, False),
    100905: (handle_guild_recommend, 100925, False),
    100906: (handle_guild_apply, 100926, True),
    100907: (handle_guild_my_apply, 100927, False),
    100908: (handle_guild_cancel_apply, 100928, True),
    100909: (handle_guild_audit_list, 100929, False),
    100910: (handle_guild_refuse_apply, 100930, True),
    100911: (handle_guild_accept_apply, 100931, True),
    100912: (handle_guild_members, 100932, False),
    100913: (handle_guild_appoint, 100933, True),
    100914: (handle_guild_remove_member, 100934, True),
    100915: (handle_guild_impeachment, 100935, False),
    100916: (lambda session, uid: handle_guild_impeachment(session, uid, True), 100936, False),
    100917: (handle_guild_quit, 100937, False),
    100918: (handle_guild_edit_info, 100938, True),
    100919: (handle_guild_change_name, 100939, True),
    100920: (handle_guild_search, 100940, True),
    100921: (handle_guild_training, 100941, False),
    100947: (handle_guild_up_building, 100949, True),
    100948: (handle_guild_buy_building_effect, 100950, True),
    100951: (handle_guild_quest_progress, 100952, False),
    100954: (handle_guild_edit_notice, 100955, True),
    # net_placeGame
    9202: (module_rules.handle_place_recruit, 9220, True),
    9203: (module_rules.handle_place_level_up, 9221, True),
    9204: (module_rules.handle_place_dismissal, 9222, True),
    9205: (module_rules.handle_place_change_name, 9223, True),
    9206: (module_rules.handle_place_go_battle, 9224, True),
    9207: (module_rules.handle_place_modify_soul, 9225, True),
    9208: (module_rules.handle_place_position, 9226, True),
    9209: (module_rules.handle_place_weapon, 9227, True),
    9210: (module_rules.handle_place_tower, 9228, True),
    9211: (module_rules.handle_place_open_box, 9229, True),
    9212: (module_rules.handle_place_buy_box, 9230, True),
    9213: (module_rules.handle_place_unload_all_soul, 9231, True),
    9214: (module_rules.handle_place_unload_all_equip, 9232, True),
    9215: (module_rules.handle_place_unload_equip, 9233, True),
    9216: (module_rules.handle_place_open_dialog, 9234, True),
    9217: (module_rules.handle_place_select_dialog, 9235, True),
    9218: (module_rules.handle_place_lock, 9236, True),
    9219: (module_rules.handle_place_resolve, 9237, True),
    # net_miniGalGame
    6802: (module_rules.handle_minigal_start, 6812, False),
    6803: (module_rules.handle_minigal_load, 6813, True),
    6804: (module_rules.handle_minigal_save, 6814, True),
    6805: (module_rules.handle_minigal_dialog, 6815, True),
    6806: (module_rules.handle_minigal_shop, 6816, True),
    6807: (module_rules.handle_minigal_item, 6817, True),
    6808: (module_rules.handle_minigal_game_over, 6818, True),
    6809: (module_rules.handle_minigal_event, 6819, True),
    6810: (module_rules.handle_minigal_tower, 6820, True),
    6811: (module_rules.handle_minigal_call, 6821, True),
    6838: (module_rules.handle_minigal_message, 6839, True),
    # net_evilErosion
    6911: (handle_evil_get_daily_supply, 6921, False),
    # net_horizontalRPG
    9502: (module_rules.handle_horizontal_element, 9509, True),
    9503: (module_rules.handle_horizontal_element_else, 9510, True),
    9504: (module_rules.handle_horizontal_combat, 9511, True),
    9505: (module_rules.handle_horizontal_boss, 9512, True),
    9506: (module_rules.handle_horizontal_dialog, 9513, True),
    9507: (module_rules.handle_horizontal_weather, 9514, True),
    9508: (module_rules.handle_horizontal_quick, 9515, True),
    9522: (module_rules.handle_horizontal_challenge, 9523, True),
    9529: (module_rules.handle_horizontal_level_dialog, 9530, True),
    # net_dualTeamExplore
    6502: (lambda s, u, b: module_rules.handle_dual_boss(s, u, b, False), 6504, True),
    6503: (lambda s, u, b: module_rules.handle_dual_boss(s, u, b, True), 6505, True),
    6509: (module_rules.handle_dual_enter, 6511, True),
    6510: (module_rules.handle_dual_move, 6512, True),
    6520: (module_rules.handle_dual_dialog, 6521, True),
    6523: (module_rules.handle_dual_fight, 6524, True),
    6526: (module_rules.handle_dual_enter_maze, 6527, True),
    6528: (module_rules.handle_dual_plot, 6529, True),
    6530: (module_rules.handle_dual_giveup, 6531, False),
    6532: (module_rules.handle_dual_number, 6533, True),
    6534: (module_rules.handle_dual_revive, 6535, False),
    6536: (module_rules.handle_dual_item, 6537, True),
    # net_cardActivity
    9702: (module_rules.handle_card_fight, 9708, True),
    9703: (module_rules.handle_card_deck, 9709, True),
    9704: (module_rules.handle_card_equip, 9710, True),
    9705: (module_rules.handle_card_consume, 9711, True),
    9706: (module_rules.handle_card_story, 9712, True),
    9707: (module_rules.handle_card_boss, 9713, True),
    # net_centerChat / net_centerRank
    100102: (module_rules.handle_chat_send, 100104, True),
    100103: (module_rules.handle_chat_room, 100105, True),
    100108: (module_rules.handle_chat_report, 100109, True),
    100202: (module_rules.handle_rank, 100203, True),
    100204: (module_rules.handle_rank_user, 100205, True),
    100206: (module_rules.handle_rank_goalie, 100207, True),
    # legacy guild activity/challenge
    7402: (module_rules.handle_guild_sign, 7405, False),
    7403: (module_rules.handle_guild_quest_rewards, 7406, True),
    7404: (module_rules.handle_guild_redpoint, 7407, False),
    7502: (module_rules.handle_guild_challenge_attack, 7506, True),
    7503: (module_rules.handle_guild_challenge_rewards, 7507, True),
    7504: (module_rules.handle_guild_challenge_mopup, 7508, True),
    7505: (module_rules.handle_guild_challenge_score, 7509, True),
    9002: (module_rules.handle_guild_training, 9003, True),
    # Operation activities: server-authoritative local state and rewards.
    5002: (module_rules.handle_group_buy, 5003, True),
    6202: (module_rules.handle_vote, 6203, True),
    6302: (module_rules.handle_newbies_submit, 6304, True),
    6303: (module_rules.handle_newbies_task, 6305, True),
    7002: (module_rules.handle_turntable_draw, 7003, True),
    7302: (module_rules.handle_cup_vote, 7303, True),
    100402: (module_rules.handle_turntable_log, 100404, True),
    100403: (module_rules.handle_turntable_receive, 100405, True),
    100502: (module_rules.handle_group_buy_info, 100503, True),
    100602: (module_rules.handle_vote_info, 100603, True),
    100702: (module_rules.handle_newbies_followers, 100703, True),
    100802: (module_rules.handle_cup_vote_info, 100803, True),
}


# Explicitly recovered modules.  These entries must stay ahead of the generic
# fallback table below so an official request can never receive a zero-value
# acknowledgement after its rule has been implemented.
MODULE_DISPATCH.update({
    2802: (module_rules.handle_daily_dup_buy_count, 2803, True),
    4802: (module_rules.handle_battle_pass_rewards, 4804, True),
    4803: (module_rules.handle_battle_pass_last_season, 4805, False),
    6002: (module_rules.handle_panda_action, 6007, True),
    6003: (module_rules.handle_panda_get_gift, 6008, True),
    6004: (module_rules.handle_panda_enter, 6009, False),
    6005: (module_rules.handle_panda_explore, 6010, False),
    6006: (module_rules.handle_panda_event, 6011, True),
    6014: (module_rules.handle_panda_select_dialog, 6015, True),
    6402: (module_rules.handle_tale_story, 6405, True),
    6403: (module_rules.handle_tale_fight, 6406, True),
    6404: (module_rules.handle_tale_boss, 6407, True),
    6411: (module_rules.handle_tale_select_dialog, 6412, True),
    6414: (module_rules.handle_tale_draw, 6415, True),
    7202: (module_rules.handle_limited_turntable_draw, 7204, True),
    7203: (module_rules.handle_limited_turntable_history, 7205, True),
    7802: (module_rules.handle_single_weak_tower, 7803, True),
    7902: (module_rules.handle_command_challenge, 7903, True),
    8002: (module_rules.handle_flight_start, 8006, False),
    8003: (module_rules.handle_flight_level_up, 8007, True),
    8004: (module_rules.handle_flight_end, 8008, True),
    8005: (module_rules.handle_flight_boss, 8009, True),
    9802: (module_rules.handle_puzzle_start, 9803, True),
    9806: (module_rules.handle_puzzle_select_dialog, 9807, True),
    9809: (module_rules.handle_puzzle_end, 9810, True),
})


# Extended handlers above are intentionally registered after the recovered
# module table so the generic typed acknowledgement cannot shadow real rules.
MODULE_DISPATCH.update({
    1002: (handle_remote_maze_order, 1005, True),
    1003: (handle_remote_battle_order, 1007, True),
    1702: (handle_minigame_choose_card, 1703, True),
    1705: (handle_minigame_turntable_end, 1706, True),
    3702: (handle_daily_supply, 3703, True),
    7107: (handle_abyss_plus_use_rune, 7108, True),
    5102: (handle_image_puzzle_unlock, 5105, True),
    5103: (lambda s, u, b: _handle_image_puzzle_reward(s, u, b, 5103, 5106), 5106, True),
    5104: (lambda s, u, b: _handle_image_puzzle_reward(s, u, b, 5104, 5107), 5107, True),
    5302: (handle_new_character_unlock, 5305, True),
    5303: (handle_new_character_log, 5306, True),
    5304: (handle_new_character_story, 5307, True),
    5402: (handle_gacha_pool_draw, 5403, True),
    5404: (handle_gacha_pool_refresh, 5405, True),
    5502: (handle_double_fight_start, 5504, True),
    5503: (handle_double_fight_rewards, 5505, True),
    5702: (handle_space_treasure_explore, 5703, True),
    5802: (handle_furniture_gacha_draw, 5803, True),
    6102: (handle_treasure_hunt_exchange, 6103, True),
    9402: (handle_survival_start, 9407, True),
    9403: (lambda s, u, b: _handle_survival_reward(s, u, b, 9403, 9408), 9408, True),
    9404: (handle_survival_level, 9409, False),
    9405: (handle_survival_start_unlimited, 9410, False),
    9406: (lambda s, u, b: _handle_survival_reward(s, u, b, 9406, 9411), 9411, True),
    9412: (handle_survival_dialog, 9413, True),
})


# Configuration-backed low-frequency modules use dedicated state machines in
# _handle_remaining_local.  Register each request explicitly so none of them
# can fall through to the generated compatibility table.
MODULE_DISPATCH.update({
    2502: (_handle_mall_buy, 2503, True),
    3102: (_remaining_handler(3102, 3103, True), 3103, True),
    3202: (_remaining_handler(3202, 3203, True), 3203, True),
    4703: (_remaining_handler(4703, 4705, True), 4705, True),
    4704: (_remaining_handler(4704, 4706, True), 4706, True),
    4709: (_remaining_handler(4709, 4710, True), 4710, True),
    5202: (_remaining_handler(5202, 5203, True), 5203, True),
    5902: (_remaining_handler(5902, 5905, True), 5905, True),
    5903: (_remaining_handler(5903, 5906, False), 5906, False),
    5904: (_remaining_handler(5904, 5907, True), 5907, True),
    5908: (_remaining_handler(5908, 5909, True), 5909, True),
    5911: (_remaining_handler(5911, 5912, True), 5912, True),
    6602: (_remaining_handler(6602, 6605, True), 6605, True),
    6603: (_remaining_handler(6603, 6606, False), 6606, False),
    6604: (_remaining_handler(6604, 6607, True), 6607, True),
    6610: (_remaining_handler(6610, 6611, True), 6611, True),
    7102: (_remaining_handler(7102, 7104, True), 7104, True),
    7103: (_remaining_handler(7103, 7105, True), 7105, True),
    7602: (_remaining_handler(7602, 7607, False), 7607, False),
    7603: (_remaining_handler(7603, 7608, True), 7608, True),
    7604: (_remaining_handler(7604, 7609, True), 7609, True),
    7605: (_remaining_handler(7605, 7610, True), 7610, True),
    7606: (_remaining_handler(7606, 7612, True), 7612, True),
})


# Explicitly route the remaining generated requests through the local state
# machine above.  Keeping this table next to MODULE_DISPATCH makes coverage
# auditable and prevents a later fallback branch from silently regressing.
_REMAINING_ACTIONS = {
}
for _request_id, (_result_id, _needs_body) in _REMAINING_ACTIONS.items():
    MODULE_DISPATCH.setdefault(
        _request_id,
        (_remaining_handler(_request_id, _result_id, _needs_body), _result_id, _needs_body),
    )


def dispatch(session, uid, msg_id, body):
    """Dispatch a module action to its handler. Returns True if handled."""
    entry = MODULE_DISPATCH.get(msg_id)
    if entry is None:
        return False
    handler_fn, res_id, needs_body = entry
    if needs_body:
        return handler_fn(session, uid, body)
    return handler_fn(session, uid)
