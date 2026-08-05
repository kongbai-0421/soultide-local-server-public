"""Configuration-backed handlers for the remaining local gameplay modules."""

from __future__ import annotations

import json
import hashlib
import random
import time
from pathlib import Path

import protocol_codec
import storage


ROOT = Path(__file__).resolve().parent
try:
    MODULE_CONFIG = json.loads((ROOT / "analysis" / "module_config.json").read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    MODULE_CONFIG = {}


def _now():
    return int(time.time())


def _table(group, name):
    value = MODULE_CONFIG.get(group, {}).get(name, {})
    return value if isinstance(value, dict) else {}


def _row(group, name, key):
    return _table(group, name).get(str(int(key)))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pairs(value):
    if not isinstance(value, list):
        return []
    result = []
    index = 0
    while index < len(value):
        if isinstance(value[index], list):
            result.extend(_pairs(value[index]))
            index += 1
            continue
        if index + 1 >= len(value):
            break
        cid, quantity = _int(value[index]), _int(value[index + 1])
        if cid > 0 and quantity > 0:
            result.append((cid, quantity))
        index += 2
    return result


def _state(uid, key, defaults=None):
    data = storage.get_player_state_json(uid, key)
    if not isinstance(data, dict):
        data = {}
    changed = False
    for name, value in (defaults or {}).items():
        if name not in data:
            data[name] = json.loads(json.dumps(value, ensure_ascii=False))
            changed = True
    if changed:
        storage.update_player_state_json(uid, key, data)
    return data


def _save(uid, key, data):
    storage.update_player_state_json(uid, key, data)


def _send(session, result_id, *values):
    session.send(result_id, protocol_codec.encode_method(result_id, *values))


def _grant(session, uid, pairs):
    applied = storage.grant_reward_pairs(uid, list(pairs))
    if applied is None:
        return None
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    return applied


def _trade(session, uid, costs, rewards):
    applied = storage.trade_reward_pairs(uid, list(costs), list(rewards))
    if applied is None:
        return None
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    return applied


def _item_show(pairs):
    return [{"cid": int(cid), "num": int(quantity), "tag": 0} for cid, quantity in pairs]


# ── Jewelry ────────────────────────────────────────────────────────────────

JEWELRY_DEFAULTS = {"items": [], "speed": {}, "next_id": 1}


def _jewelry_state(uid):
    data = _state(uid, "jewelry", JEWELRY_DEFAULTS)
    if data.get("items"):
        return data
    quality_rows = _table("jewelry", "JewelryQualityTable")
    template_ids = {_int(row.get("JewelryId")) for row in quality_rows.values() if isinstance(row, dict)}
    seeded = []
    for item in storage.get_equipment_instances(uid):
        if _int(item.get("template_id")) not in template_ids:
            continue
        seeded.append({
            "id": _int(item.get("id")), "template_id": _int(item.get("template_id")),
            "star": _int(item.get("star")), "speed": 0, "role_id": _int(item.get("equipped_to")),
            "slot": _int(item.get("equipped_slot")), "locked": bool(item.get("locked")),
        })
    if seeded:
        data["items"] = seeded
        data["next_id"] = max(item["id"] for item in seeded) + 1
        _save(uid, "jewelry", data)
    return data


def _jewel(data, item_id):
    return next((item for item in data.get("items", []) if _int(item.get("id")) == _int(item_id)), None)


def _jewel_quality(item, next_star=None):
    template_id = _int(item.get("template_id"))
    level = _int(item.get("star")) + 1 if next_star is None else _int(next_star)
    rows = [row for row in _table("jewelry", "JewelryQualityTable").values()
            if isinstance(row, dict) and _int(row.get("JewelryId")) == template_id]
    return next((row for row in rows if _int(row.get("QualityLevel")) == level), None)


def _jewel_wear(session, uid, body, request_id=7702, result_id=7713, new_style=False):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    if request_id == 7702:
        role_id, jewel_id, slot = values
    else:
        jewel_id, role_id, slot = values
    data, item = _jewelry_state(uid), None
    item = _jewel(data, jewel_id)
    if item is None or not 0 <= _int(slot) <= 5:
        return False
    if not any(_int(row.get("soul_id")) == _int(role_id) for row in storage.get_souls(uid)):
        return False
    for other in data["items"]:
        if _int(other.get("role_id")) == _int(role_id) and _int(other.get("slot")) == _int(slot):
            other["role_id"], other["slot"] = 0, 0
    item["role_id"], item["slot"] = _int(role_id), _int(slot)
    _save(uid, "jewelry", data)
    if new_style:
        _send(session, result_id, 0)
    else:
        _send(session, result_id, 0, _int(role_id), _int(jewel_id), _int(slot))
    return True


def _jewel_unwear(session, uid, body, request_id=7703, result_id=7714, new_style=False):
    try:
        (jewel_id,) = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    data, item = _jewelry_state(uid), None
    item = _jewel(data, jewel_id)
    if item is None:
        return False
    item["role_id"], item["slot"] = 0, 0
    _save(uid, "jewelry", data)
    _send(session, result_id, 0) if new_style else _send(session, result_id, 0, _int(jewel_id))
    return True


def _jewel_upstar(session, uid, body, request_id=7704, result_id=7715, new_style=False):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    jewel_id = values[0]
    data, item = _jewelry_state(uid), None
    item = _jewel(data, jewel_id)
    if item is None:
        return False
    row = _jewel_quality(item)
    if row is None:
        return False
    costs = _pairs(row.get("RisingStarCostJewelry")) + _pairs(row.get("RisingStarCostItem"))
    if costs and _trade(session, uid, costs, []) is None:
        return False
    item["star"] = _int(item.get("star")) + 1
    _save(uid, "jewelry", data)
    if new_style:
        _send(session, result_id, 0)
    elif result_id == 7715:
        _send(session, result_id, 0, _int(jewel_id), _int(item["star"]))
    else:
        _send(session, result_id, 0, _int(jewel_id))
    return True


def _jewel_set_speed(session, uid, body, request_id=7706, result_id=7717, new_style=False):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    jewel_id = values[0]
    speed_type = values[-1] if len(values) > 2 else 0
    speed_value = values[1] if len(values) == 2 else values[1]
    data, item = _jewelry_state(uid), None
    item = _jewel(data, jewel_id)
    if item is None:
        return False
    quality = _jewel_quality(item, _int(item.get("star")) + 1) or _jewel_quality(item, _int(item.get("star")))
    limit = _int((quality or {}).get("SpeedLimit"), 0)
    if _int(speed_value) < 0 or (limit and _int(speed_value) > limit):
        return False
    item["speed"] = _int(speed_value)
    data.setdefault("speed", {})[str(_int(jewel_id))] = _int(speed_value)
    _save(uid, "jewelry", data)
    _send(session, result_id, 0) if new_style else _send(session, result_id, 0, _int(jewel_id), _int(speed_type))
    return True


def _jewel_recycle(session, uid, body, request_id=7707, result_id=7718, new_style=False):
    try:
        values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError):
        return False
    jewel_id = values[0]
    data, item = _jewelry_state(uid), None
    item = _jewel(data, jewel_id)
    if item is None or item.get("locked") or item.get("role_id"):
        return False
    row = _jewel_quality(item, _int(item.get("star"))) or _jewel_quality(item)
    rewards = _pairs((row or {}).get("Recycling"))
    if _grant(session, uid, rewards) is None:
        return False
    data["items"] = [entry for entry in data["items"] if entry is not item]
    _save(uid, "jewelry", data)
    if new_style:
        _send(session, result_id, 0)
    else:
        _send(session, result_id, 0, _int(jewel_id), _item_show(rewards))
    return True


def handle_jewelry_wear(session, uid, body): return _jewel_wear(session, uid, body)
def handle_jewelry_unwear(session, uid, body): return _jewel_unwear(session, uid, body)
def handle_jewelry_upstar(session, uid, body): return _jewel_upstar(session, uid, body)
def handle_jewelry_upstar_bag(session, uid, body): return _jewel_upstar(session, uid, body, 7705, 7716)
def handle_jewelry_set_speed(session, uid, body): return _jewel_set_speed(session, uid, body)
def handle_jewelry_recycle(session, uid, body): return _jewel_recycle(session, uid, body)
def handle_jewelry_new_wear(session, uid, body): return _jewel_wear(session, uid, body, 7708, 7719, True)
def handle_jewelry_new_unwear(session, uid, body): return _jewel_unwear(session, uid, body, 7709, 7720, True)
def handle_jewelry_new_speed(session, uid, body): return _jewel_set_speed(session, uid, body, 7710, 7721, True)
def handle_jewelry_new_upstar(session, uid, body): return _jewel_upstar(session, uid, body, 7711, 7722, True)
def handle_jewelry_new_recycle(session, uid, body): return _jewel_recycle(session, uid, body, 7712, 7723, True)


# ── Place game ──────────────────────────────────────────────────────────────

PLACE_DEFAULTS = {"units": [], "formations": [], "level": 1, "tower_floor": 0, "equipment": [], "events": []}


def _place_state(uid):
    return _state(uid, "place_game", PLACE_DEFAULTS)


def _place_unit_pod(unit):
    return {"cid": _int(unit.get("cid")), "experience": _int(unit.get("experience")), "level": _int(unit.get("level"), 1)}


def _place_prefab(unit):
    return {
        "id": _int(unit.get("id")), "soulCid": _int(unit.get("cid")), "lv": _int(unit.get("level"), 1),
        "position": _int(unit.get("position")), "formationPos": _int(unit.get("position")),
        "power": max(1, _int(unit.get("power"), _int(unit.get("level"), 1) * 100)),
        "attr": list(unit.get("attr", [])), "equipments": {int(k): int(v) for k, v in unit.get("equipments", {}).items()},
    }


def _place_find(data, key):
    return next((unit for unit in data.get("units", []) if _int(unit.get("id")) == _int(key) or _int(unit.get("cid")) == _int(key)), None)


def handle_place_recruit(session, uid, body):
    try:
        (soul_cid,) = protocol_codec.decode_method(9202, body)
    except (ValueError, KeyError):
        return False
    data = _place_state(uid)
    row = _row("place", "PlaceGameSoulTable", soul_cid)
    if row is None:
        row = next(iter(_table("place", "PlaceGameSoulTable").values()), None)
    if row is None:
        return False
    control = next(iter(_table("place", "PlaceGameControlTable").values()), {})
    max_count = _int((control.get("SoulMax") or [3])[min(max(_int(data.get("tower_floor")), 0), 12)], 3)
    if len(data.get("units", [])) >= max_count:
        return False
    unit = {"id": _int(data.get("next_id"), 1), "cid": _int(row.get("Id")), "level": 1, "experience": 0, "position": 0, "power": 100}
    data["next_id"] = unit["id"] + 1
    data.setdefault("units", []).append(unit)
    _save(uid, "place_game", data)
    pod = _place_unit_pod(unit)
    _send(session, 9220, 0, pod, [_place_unit_pod(item) for item in data["units"]])
    return True


def handle_place_level_up(session, uid, body):
    try:
        unit_id, target_level = protocol_codec.decode_method(9203, body)
    except (ValueError, KeyError):
        return False
    data, unit = _place_state(uid), None
    unit = _place_find(data, unit_id)
    if unit is None or _int(target_level) != _int(unit.get("level"), 1) + 1:
        return False
    row = next((item for item in _table("place", "PlaceGameSoulLevelTable").values()
                if _int(item.get("Group")) == 1 and _int(item.get("Level")) == _int(target_level)), None)
    if row is None:
        return False
    exp_id = _int(next(iter(_table("place", "PlaceGameControlTable").values()), {}).get("ExpId"), 341)
    cost = _int(row.get("NeedExp"), 0)
    if cost and _trade(session, uid, [(exp_id, cost)], []) is None:
        return False
    unit["level"] = _int(target_level)
    unit["experience"] = 0
    unit["power"] = max(1, _int(unit.get("power"), 100) + _int(row.get("AttValue", [0])[0] if row.get("AttValue") else 0))
    _save(uid, "place_game", data)
    _send(session, 9221, 0, _place_unit_pod(unit))
    return True


def handle_place_dismissal(session, uid, body):
    try: (unit_id,) = protocol_codec.decode_method(9204, body)
    except (ValueError, KeyError): return False
    data, unit = _place_state(uid), None
    unit = _place_find(data, unit_id)
    row = _row("place", "PlaceGameSoulTable", unit.get("cid")) if unit else None
    if unit is None or row is None or not row.get("IsDismiss", False): return False
    data["units"] = [item for item in data["units"] if item is not unit]
    _save(uid, "place_game", data)
    _send(session, 9222, 0, _int(unit_id), [_place_unit_pod(item) for item in data["units"]])
    return True


def _place_form(data, form_id):
    return next((form for form in data.get("formations", []) if _int(form.get("id")) == _int(form_id)), None)


def handle_place_change_name(session, uid, body):
    try: form_id, name = protocol_codec.decode_method(9205, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None:
        form = {"id": _int(form_id), "index": len(data["formations"]), "name": "编队", "formation": {}}
        data["formations"].append(form)
    if not isinstance(name, str) or not name.strip() or len(name) > 20: return False
    form["name"] = name.strip(); _save(uid, "place_game", data)
    _send(session, 9223, 0, _int(form_id), name.strip()); return True


def handle_place_go_battle(session, uid, body):
    try: form_id, index, position = protocol_codec.decode_method(9206, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None: return False
    form["index"] = _int(index); form["formation"] = {int(k): int(v) for k, v in form.get("formation", {}).items()}
    _save(uid, "place_game", data)
    _send(session, 9224, 0, {"id": _int(form_id), "index": _int(index), "name": str(form.get("name", "编队")), "formation": form["formation"]})
    return True


def handle_place_modify_soul(session, uid, body):
    try: form_id, soul_id = protocol_codec.decode_method(9207, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); unit = _place_find(data, soul_id)
    form = _place_form(data, form_id)
    if unit is None or form is None: return False
    form.setdefault("formation", {})[_int(unit["id"])] = _int(unit["position"])
    _save(uid, "place_game", data); _send(session, 9225, 0, _place_prefab(unit)); return True


def handle_place_position(session, uid, body):
    try: form_id, position = protocol_codec.decode_method(9208, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None or not 0 <= _int(position) <= 9: return False
    form["position"] = _int(position); _save(uid, "place_game", data); _send(session, 9226, 0, _int(position)); return True


def handle_place_weapon(session, uid, body):
    try: form_id, equip_id, enabled = protocol_codec.decode_method(9209, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None: return False
    equips = form.setdefault("equipments", {})
    if enabled: equips[_int(equip_id)] = _int(equip_id)
    else: equips.pop(str(_int(equip_id)), None); equips.pop(_int(equip_id), None)
    _save(uid, "place_game", data); _send(session, 9227, 0, []); return True


def handle_place_tower(session, uid, body):
    try: (tower_id,) = protocol_codec.decode_method(9210, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); row = _row("place", "PlaceGameTowerTable", tower_id)
    if row is None or _int(row.get("Floor")) != _int(data.get("tower_floor"), 0) + 1: return False
    rewards = _pairs(row.get("ClearReward"))
    if not _start_module_battle(
        session, uid, "place_game", tower_id, _int(row.get("BattleAreaId")),
        _int(row.get("MonsterTeam")), rewards, battle_type=6,
    ):
        return False
    data["pending_tower"] = _int(tower_id); _save(uid, "place_game", data)
    _send(session, 9228, 0); return True


def _place_open_box(session, uid, box_id, result_id):
    data = _place_state(uid); row = _row("place", "PlaceGameBoxExchangeTable", box_id)
    if row is None: return False
    need_tower = _int(row.get("NeedTower"), 0)
    if _int(data.get("tower_floor"), 0) < need_tower: return False
    cost, reward = _pairs(row.get("Cost")), _int(row.get("Reward"))
    if _trade(session, uid, cost, [(reward, 1)]) is None: return False
    _send(session, result_id, 0, [{"id": 0, "cid": reward, "num": 1, "usedNum": 0, "createTime": _now()}])
    return True


def handle_place_open_box(session, uid, body):
    try: body_values = protocol_codec.decode_method(9211, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); available = sorted(_table("place", "PlaceGameBoxExchangeTable"), key=int)
    box_id = _int(data.get("selected_box"), _int(available[0]) if available else 0)
    return _place_open_box(session, uid, box_id, 9229)


def handle_place_buy_box(session, uid, body):
    try: box_id, count = protocol_codec.decode_method(9212, body)
    except (ValueError, KeyError): return False
    if _int(count) <= 0 or _int(count) > 20: return False
    data = _place_state(uid); row = _row("place", "PlaceGameBoxExchangeTable", box_id)
    if row is None: return False
    rewards = [(_int(row.get("Reward")), _int(count))]
    if _trade(session, uid, [(cid, quantity * _int(count)) for cid, quantity in _pairs(row.get("Cost"))], rewards) is None: return False
    _send(session, 9230, 0, [{"id": 0, "cid": rewards[0][0], "num": rewards[0][1], "usedNum": 0, "createTime": _now()}]); return True


def handle_place_unload_all_soul(session, uid, body):
    try: (form_id,) = protocol_codec.decode_method(9213, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None: return False
    form["formation"] = {}; _save(uid, "place_game", data); _send(session, 9231, 0, []); return True


def handle_place_unload_all_equip(session, uid, body):
    try: (form_id,) = protocol_codec.decode_method(9214, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); form = _place_form(data, form_id)
    if form is None: return False
    form["equipments"] = {}; _save(uid, "place_game", data); _send(session, 9232, 0, []); return True


def handle_place_unload_equip(session, uid, body):
    try: (equip_id,) = protocol_codec.decode_method(9215, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid)
    for form in data.get("formations", []):
        form.get("equipments", {}).pop(str(_int(equip_id)), None); form.get("equipments", {}).pop(_int(equip_id), None)
    _save(uid, "place_game", data); _send(session, 9233, 0, {}); return True


def handle_place_open_dialog(session, uid, body):
    try: (event_id,) = protocol_codec.decode_method(9216, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); event = _row("place", "PlaceGameEventTable", event_id)
    if event is None or _int(data.get("tower_floor"), 0) < _int(event.get("UnlockTower"), 0): return False
    data["dialog_event"] = _int(event_id); _save(uid, "place_game", data); _send(session, 9234, 0, _int(event_id)); return True


def handle_place_select_dialog(session, uid, body):
    try: event_id, choices = protocol_codec.decode_method(9217, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid)
    if _int(data.get("dialog_event")) != _int(event_id): return False
    event = _row("place", "PlaceGameEventTable", event_id); rewards = _pairs((event or {}).get("DialogReward"))
    key = f"event:{int(event_id)}"
    applied = storage.claim_reward_once(uid, "place_game_claims", key, rewards)
    if applied is None: return False
    data["events"].append(_int(event_id)); data["dialog_event"] = 0; _save(uid, "place_game", data)
    _send(session, 9235, 0, _int(event_id), _item_show(rewards) if applied.get("claimed") else []); return True


def handle_place_lock(session, uid, body):
    try: equip_id, locked = protocol_codec.decode_method(9218, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid)
    entry = next((item for item in data["equipment"] if _int(item.get("id")) == _int(equip_id)), None)
    if entry is None: entry = {"id": _int(equip_id), "locked": False}; data["equipment"].append(entry)
    entry["locked"] = bool(locked); _save(uid, "place_game", data); _send(session, 9236, 0); return True


def handle_place_resolve(session, uid, body):
    try: (equip_ids,) = protocol_codec.decode_method(9219, body)
    except (ValueError, KeyError): return False
    data = _place_state(uid); ids = {_int(value) for value in equip_ids}
    if any(item.get("locked") and _int(item.get("id")) in ids for item in data["equipment"]): return False
    data["equipment"] = [item for item in data["equipment"] if _int(item.get("id")) not in ids]
    rewards = [(341, len(ids))] if ids else []
    applied = _grant(session, uid, rewards)
    if applied is None: return False
    _save(uid, "place_game", data); _send(session, 9237, 0, _item_show(rewards)); return True


PLACE_DISPATCH = {
    9202: (handle_place_recruit, True), 9203: (handle_place_level_up, True), 9204: (handle_place_dismissal, True),
    9205: (handle_place_change_name, True), 9206: (handle_place_go_battle, True), 9207: (handle_place_modify_soul, True),
    9208: (handle_place_position, True), 9209: (handle_place_weapon, True), 9210: (handle_place_tower, True),
    9211: (handle_place_open_box, True), 9212: (handle_place_buy_box, True), 9213: (handle_place_unload_all_soul, True),
    9214: (handle_place_unload_all_equip, True), 9215: (handle_place_unload_equip, True), 9216: (handle_place_open_dialog, True),
    9217: (handle_place_select_dialog, True), 9218: (handle_place_lock, True), 9219: (handle_place_resolve, True),
}


JEWELRY_DISPATCH = {
    7702: handle_jewelry_wear, 7703: handle_jewelry_unwear, 7704: handle_jewelry_upstar, 7705: handle_jewelry_upstar_bag,
    7706: handle_jewelry_set_speed, 7707: handle_jewelry_recycle, 7708: handle_jewelry_new_wear, 7709: handle_jewelry_new_unwear,
    7710: handle_jewelry_new_speed, 7711: handle_jewelry_new_upstar, 7712: handle_jewelry_new_recycle,
}


# ── Amusement park ──────────────────────────────────────────────────────────

AMUSEMENT_DEFAULTS = {
    "level": 1, "funds": 0, "score": 0, "buildings": [], "roles": [],
    "lands": [], "events": {}, "games": {}, "dialog": 0, "last_income": 0,
}


def _amusement_state(uid):
    data = _state(uid, "amusement_park", AMUSEMENT_DEFAULTS)
    control = next(iter(_table("amusement", "AmusementParkControlTable").values()), {})
    if not data.get("initialized"):
        initial = _pairs(control.get("InitialProps"))
        data["funds"] = initial[0][1] if initial else _int(data.get("funds"), 0)
        data["initialized"] = True
        _save(uid, "amusement_park", data)
    return data


def _amusement_att(data):
    return {"attr": {1: _int(data.get("funds")), 2: _int(data.get("score"))}, "level": _int(data.get("level"), 1), "number": len(data.get("buildings", []))}


def _amusement_role(role):
    return {"id": _int(role.get("id")), "level": _int(role.get("level"), 1), "status": bool(role.get("deployed")), "attr": {int(k): _int(v) for k, v in role.get("attr", {}).items()}}


def _amusement_land(land):
    return {"unitID": _int(land.get("unit_id")), "level": _int(land.get("level"), 1), "post": _int(land.get("post")), "property": {int(k): _int(v) for k, v in land.get("property", {}).items()}, "roleAdd": {}, "doll": []}


def handle_amusement_get_info(session, uid):
    data = _amusement_state(uid)
    pod = {"amusementParkAttPOD": _amusement_att(data), "amusementParkVoRoles": [_amusement_role(r) for r in data["roles"]], "amusementParkVoRolesHave": [_amusement_role(r) for r in data["roles"]], "boss": {}, "dialogId": _int(data.get("dialog")), "plot": list(data.get("events", {}).keys()), "postList": [], "unitList": [_amusement_land(land) for land in data["lands"]]}
    _send(session, 9330, 0, pod)
    return True


def handle_amusement_temporary(session, uid):
    data = _amusement_state(uid)
    _send(session, 9331, 0, [_amusement_land(land) for land in data["lands"]])
    return True


def _amusement_build_cost(building_id, level=1):
    rows = [row for row in _table("amusement", "AmusementParkBuildingLevelTable").values() if _int(row.get("BuildId")) == _int(building_id) and _int(row.get("Level"), 1) == _int(level)]
    return _pairs((rows[0] if rows else {}).get("LevelUpCost"))


def handle_amusement_build(session, uid, body):
    try: land_id, building_id = protocol_codec.decode_method(9304, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid)
    if _row("amusement", "AmusementParkBuildingTable", building_id) is None: return False
    if any(_int(land.get("unit_id")) == _int(land_id) for land in data["lands"]): return False
    costs = _amusement_build_cost(building_id)
    if costs and _trade(session, uid, costs, []) is None: return False
    data["buildings"].append({"id": _int(building_id), "level": 1, "land": _int(land_id)})
    data["lands"].append({"unit_id": _int(land_id), "level": 1, "post": 0, "property": {}})
    _save(uid, "amusement_park", data); _send(session, 9332, 0, _int(data.get("funds"))); return True


def handle_amusement_layout(session, uid, body):
    try: lands = protocol_codec.decode_method(9305, body)[0]
    except (ValueError, KeyError): return False
    data = _amusement_state(uid)
    if not isinstance(lands, list): return False
    for land in lands:
        if not isinstance(land, dict) or _int(land.get("unitID")) <= 0: return False
    data["layout"] = lands; _save(uid, "amusement_park", data); _send(session, 9333, 0, _amusement_att(data)); return True


def handle_amusement_confirm(session, uid, body):
    try: lands = protocol_codec.decode_method(9306, body)[0]
    except (ValueError, KeyError): return False
    data = _amusement_state(uid)
    if not isinstance(lands, list): return False
    data["layout"] = lands; _save(uid, "amusement_park", data)
    _send(session, 9334, 0, _amusement_att(data), [_amusement_land(land) for land in data["lands"]]); return True


def handle_amusement_level_up(session, uid, body):
    try: (target,) = protocol_codec.decode_method(9307, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid)
    if _int(target) != _int(data.get("level"), 1) + 1: return False
    cost = [(_int(next(iter(_table("amusement", "AmusementParkControlTable").values()), {}).get("MoneyId"), 351), _int(target) * 100)]
    if _trade(session, uid, cost, []) is None: return False
    data["level"] = _int(target); _save(uid, "amusement_park", data); _send(session, 9335, 0, _int(target)); return True


def handle_amusement_random_role(session, uid):
    data = _amusement_state(uid); rows = list(_table("amusement", "AmusementParkRoleTable").values())
    if not rows: return False
    row = rows[_int(data.get("level"), 1) % len(rows)]
    role = {"id": _int(row.get("Id")), "level": 1, "deployed": False, "attr": {}}
    data["roles"].append(role); _save(uid, "amusement_park", data); _send(session, 9336, 0, [_amusement_role(role)]); return True


def handle_amusement_recruit(session, uid, body):
    try: (role_id,) = protocol_codec.decode_method(9309, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid); row = _row("amusement", "AmusementParkRoleTable", role_id)
    if row is None: return False
    limit = dict(_pairs(next(iter(_table("amusement", "AmusementParkControlTable").values()), {}).get("RecruitLimit")))
    level = _int(data.get("level"), 1)
    if len(data["roles"]) >= _int(limit.get(level), 999): return False
    cost = _pairs(next(iter(_table("amusement", "AmusementParkControlTable").values()), {}).get("RecruitCost"))
    if cost and _trade(session, uid, cost, []) is None: return False
    role = {"id": _int(row.get("Id")), "level": 1, "deployed": False, "attr": {}}
    data["roles"].append(role); _save(uid, "amusement_park", data); _send(session, 9337, 0, [_amusement_role(role)], [_amusement_role(role)]); return True


def handle_amusement_role_level(session, uid, body):
    try: role_id, target = protocol_codec.decode_method(9310, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid); role = next((r for r in data["roles"] if _int(r.get("id")) == _int(role_id)), None)
    if role is None or _int(target) != _int(role.get("level"), 1) + 1: return False
    rows = [row for row in _table("amusement", "AmusementParkRoleLevelTable").values() if _int(row.get("Level")) == _int(target)]
    cost = _pairs((rows[0] if rows else {}).get("Cost"))
    if cost and _trade(session, uid, cost, []) is None: return False
    role["level"] = _int(target); _save(uid, "amusement_park", data); _send(session, 9338, 0, _amusement_role(role)); return True


def _amusement_deploy(session, uid, body, deploy):
    try: role_id, post = protocol_codec.decode_method(9311 if deploy else 9312, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid); role = next((r for r in data["roles"] if _int(r.get("id")) == _int(role_id)), None)
    if role is None: return False
    role["deployed"], role["post"] = bool(deploy), _int(post)
    _save(uid, "amusement_park", data); _send(session, 9339 if deploy else 9340, 0, _amusement_role(role)); return True


def handle_amusement_open_dialog(session, uid, body):
    try: (event_id,) = protocol_codec.decode_method(9313, body)
    except (ValueError, KeyError): return False
    if _row("amusement", "AmusementParkEventControlTable", event_id) is None: return False
    data = _amusement_state(uid); data["dialog"] = _int(event_id); _save(uid, "amusement_park", data); _send(session, 9341, 0, _int(event_id)); return True


def handle_amusement_select_dialog(session, uid, body):
    try: event_id, choices = protocol_codec.decode_method(9314, body)
    except (ValueError, KeyError): return False
    data = _amusement_state(uid)
    if _int(data.get("dialog")) != _int(event_id): return False
    data["events"][str(_int(event_id))] = {"choices": choices, "time": _now()}; data["dialog"] = 0; _save(uid, "amusement_park", data); _send(session, 9342, 0, _int(event_id)); return True


def handle_amusement_income(session, uid):
    data = _amusement_state(uid); now = _now()
    interval = _int(next(iter(_table("amusement", "AmusementParkControlTable").values()), {}).get("TimeInterval"), 60)
    elapsed = max(0, (now - _int(data.get("last_income"))) // max(1, interval))
    if elapsed <= 0: return False
    income = next(iter(_table("amusement", "AmusementParkIncomeTable").values()), {})
    pairs = _pairs(income.get("Reward")) or [(351, elapsed * max(1, len(data.get("roles", []))))]
    pairs = [(cid, quantity * elapsed) for cid, quantity in pairs]
    applied = _grant(session, uid, pairs)
    if applied is None: return False
    data["last_income"] = now; _save(uid, "amusement_park", data); _send(session, 9343, 0, _item_show(pairs), _int(data.get("score"))); return True


def _amusement_game(session, uid, request_id, result_id, body=None):
    try: values = protocol_codec.decode_method(request_id, body or b"") if body is not None else []
    except (ValueError, KeyError): return False
    data = _amusement_state(uid); game_id = _int(values[0]) if values else 0
    score = _int(values[-1]) if values else 0
    data.setdefault("games", {})[str(request_id)] = {"id": game_id, "score": score, "time": _now()}
    _save(uid, "amusement_park", data)
    types = protocol_codec.METHODS[result_id]["types"]
    if types == ["int"]: values_out = [0]
    elif types == ["int", "int"]: values_out = [0, score]
    elif types == ["int", "int", "int"]: values_out = [0, score, _int(data.get("level"), 1)]
    else: values_out = [0, score, _item_show([(353, max(0, score // 100))])]
    _send(session, result_id, *values_out); return True


def handle_amusement_combat(session, uid, body):
    try: boss_id, formation_id = protocol_codec.decode_method(9316, body)
    except (ValueError, KeyError): return False
    control = next(iter(_table("amusement", "AmusementParkControlTable").values()), {})
    if _int(boss_id) <= 0: return False
    if not _start_module_battle(
        session, uid, "amusement", boss_id, _int(control.get("BossAreaID")),
        _int(control.get("BossTeam")), [], battle_type=6,
    ):
        return False
    _send(session, 9344, 0)
    return True


def handle_amusement_boss(session, uid, body):
    try: (boss_id,) = protocol_codec.decode_method(9317, body)
    except (ValueError, KeyError): return False
    control = next(iter(_table("amusement", "AmusementParkControlTable").values()), {})
    if _int(boss_id) <= 0: return False
    if not _start_module_battle(
        session, uid, "amusement_boss", boss_id, _int(control.get("BossAreaID")),
        _int(control.get("BossTeam")), [], battle_type=6,
    ):
        return False
    _send(session, 9345, 0)
    return True


def handle_amusement_read_burst(session, uid):
    data = _amusement_state(uid); data["burst_read"] = True; _save(uid, "amusement_park", data); _send(session, 9346, 0); return True


AMUSEMENT_DISPATCH = {
    9302: (handle_amusement_get_info, False), 9303: (handle_amusement_temporary, False), 9304: (handle_amusement_build, True),
    9305: (handle_amusement_layout, True), 9306: (handle_amusement_confirm, True), 9307: (handle_amusement_level_up, True),
    9308: (handle_amusement_random_role, False), 9309: (handle_amusement_recruit, True), 9310: (handle_amusement_role_level, True),
    9311: (lambda s, u, b: _amusement_deploy(s, u, b, True), True), 9312: (lambda s, u, b: _amusement_deploy(s, u, b, False), True),
    9313: (handle_amusement_open_dialog, True), 9314: (handle_amusement_select_dialog, True), 9315: (handle_amusement_income, False),
    9316: (handle_amusement_combat, True), 9317: (handle_amusement_boss, True), 9318: (handle_amusement_read_burst, False),
    9319: (lambda s, u: _amusement_game(s, u, 9319, 9347), False), 9320: (lambda s, u, b: _amusement_game(s, u, 9320, 9348, b), True),
    9321: (lambda s, u: _amusement_game(s, u, 9321, 9349), False), 9322: (lambda s, u, b: _amusement_game(s, u, 9322, 9350, b), True),
    9323: (lambda s, u: _amusement_game(s, u, 9323, 9351), False), 9324: (lambda s, u, b: _amusement_game(s, u, 9324, 9352, b), True),
    9325: (lambda s, u: _amusement_game(s, u, 9325, 9353), False), 9326: (lambda s, u, b: _amusement_game(s, u, 9326, 9354, b), True),
    9327: (lambda s, u: _amusement_game(s, u, 9327, 9355), False), 9328: (lambda s, u, b: _amusement_game(s, u, 9328, 9356, b), True),
    9329: (lambda s, u: _amusement_game(s, u, 9329, 9357), False),
}


# ── Dual-team exploration ──────────────────────────────────────────────────

DUAL_DEFAULTS = {"level": 0, "maze": 0, "team": 1, "team1": {"node": 0, "dead": False}, "team2": {"node": 0, "dead": False}, "nodes": [], "items": [], "dialog": 0}


def _dual_state(uid): return _state(uid, "dual_team_explore", DUAL_DEFAULTS)


def _dual_team(data, number):
    value = data.get("team1" if number == 1 else "team2", {})
    return {"currNodeId": _int(value.get("node")), "dead": bool(value.get("dead")), "formationInfo": {"index": number, "name": f"队伍{number}", "soulPrefabs": {}, "userData": ""}, "stop": False}


def _dual_pod(data):
    return {"currDialog": _int(data.get("dialog")), "currFightMonsterTeamId": _int(data.get("fight_team")), "currMazeCid": _int(data.get("maze")), "currNumberInputId": _int(data.get("number_input")), "currTeam": _int(data.get("team"), 1), "currTransportNodeId": 0, "easyMode": False, "levelCid": _int(data.get("level")), "nodes": data.get("nodes", []), "separate": False, "team1": _dual_team(data, 1), "team2": _dual_team(data, 2)}


def handle_dual_enter(session, uid, body):
    try: level, form_a, form_b, easy = protocol_codec.decode_method(6509, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data.update({"level": _int(level), "maze": _int(level), "team": 1, "team1": {"node": 0, "dead": False, "formation": _int(form_a)}, "team2": {"node": 0, "dead": False, "formation": _int(form_b)}})
    _save(uid, "dual_team_explore", data); _send(session, 6511, 0, _dual_pod(data)); return True


def handle_dual_move(session, uid, body):
    try: team, node = protocol_codec.decode_method(6510, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); key = "team1" if _int(team) == 1 else "team2"; data[key]["node"] = _int(node); data["team"] = _int(team); _save(uid, "dual_team_explore", data); _send(session, 6512, 0, _int(team), _int(node), False); return True


def handle_dual_dialog(session, uid, body):
    try: dialog, choices = protocol_codec.decode_method(6520, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data["dialog"] = 0; data.setdefault("dialogs", {})[str(_int(dialog))] = choices; _save(uid, "dual_team_explore", data); _send(session, 6521, 0, _int(dialog)); return True


def handle_dual_fight(session, uid, body):
    try: (fight_type,) = protocol_codec.decode_method(6523, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid)
    if _int(fight_type) <= 0 or not _start_module_battle(
        session, uid, "dual_team", fight_type, _int(data.get("maze")),
        _int(fight_type), [], battle_type=6,
    ):
        return False
    _send(session, 6524, 0)
    return True


def handle_dual_boss(session, uid, body, ex=False):
    try: values = protocol_codec.decode_method(6503 if ex else 6502, body)
    except (ValueError, KeyError): return False
    map_id = _int(values[0])
    team = _int(values[1] if len(values) > 1 else values[0])
    if map_id <= 0 or team <= 0 or not _start_module_battle(
        session, uid, "dual_boss_ex" if ex else "dual_boss", map_id,
        map_id, team, [], battle_type=6,
    ):
        return False
    _send(session, 6505 if ex else 6504, 0)
    return True


def handle_dual_enter_maze(session, uid, body):
    try: (maze,) = protocol_codec.decode_method(6526, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data["maze"] = _int(maze); _save(uid, "dual_team_explore", data); _send(session, 6527, 0, {"id": 0, "mazeCid": _int(maze), "isLocal": True, "randomSeed": _int(maze), "saveData": "", "saveVersion": 1, "carryItems": [], "mazePlayer": {}}); return True


def handle_dual_plot(session, uid, body):
    try: plot, finished = protocol_codec.decode_method(6528, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data.setdefault("plots", {})[str(_int(plot))] = bool(finished); _save(uid, "dual_team_explore", data); _send(session, 6529, 0); return True


def handle_dual_giveup(session, uid):
    data = _dual_state(uid); data["level"] = 0; data["maze"] = 0; _save(uid, "dual_team_explore", data); _send(session, 6531, 0); return True


def handle_dual_number(session, uid, body):
    try: (value,) = protocol_codec.decode_method(6532, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data["number_input"] = _int(value); _save(uid, "dual_team_explore", data); _send(session, 6533, 0, _int(value)); return True


def handle_dual_revive(session, uid):
    data = _dual_state(uid); team = _int(data.get("team"), 1); data["team%d" % team]["dead"] = False; _save(uid, "dual_team_explore", data); _send(session, 6535, 0, team, 0, _dual_team(data, team)); return True


def handle_dual_item(session, uid, body):
    try: (item,) = protocol_codec.decode_method(6536, body)
    except (ValueError, KeyError): return False
    data = _dual_state(uid); data["items"] = [value for value in data.get("items", []) if _int(value) != _int(item)]; _save(uid, "dual_team_explore", data); _send(session, 6537, 0); return True


DUAL_DISPATCH = {
    6502: (lambda s, u, b: handle_dual_boss(s, u, b, False), True), 6503: (lambda s, u, b: handle_dual_boss(s, u, b, True), True),
    6509: (handle_dual_enter, True), 6510: (handle_dual_move, True), 6520: (handle_dual_dialog, True), 6523: (handle_dual_fight, True),
    6526: (handle_dual_enter_maze, True), 6528: (handle_dual_plot, True), 6530: (handle_dual_giveup, False), 6532: (handle_dual_number, True),
    6534: (handle_dual_revive, False), 6536: (handle_dual_item, True),
}


# ── Horizontal RPG ──────────────────────────────────────────────────────────

HORIZONTAL_DEFAULTS = {"level": 0, "map": 0, "weather": 0, "finished": [], "dialogs": {}, "elements": {}, "tickets": 1, "bosses": []}


def _horizontal_state(uid): return _state(uid, "horizontal_rpg", HORIZONTAL_DEFAULTS)


def handle_horizontal_element(session, uid, body):
    try: level, element_id, option, value = protocol_codec.decode_method(9502, body)
    except (ValueError, KeyError): return False
    row = _row("horizontal", "HorizontalRPGElementTable", element_id)
    if row is None: return False
    data = _horizontal_state(uid); data.setdefault("elements", {})[str(_int(element_id))] = {"level": _int(level), "option": _int(option), "value": _int(value)}; _save(uid, "horizontal_rpg", data); _send(session, 9509, 0); return True


def handle_horizontal_element_else(session, uid, body):
    try: values = protocol_codec.decode_method(9503, body)[0]
    except (ValueError, KeyError): return False
    data = _horizontal_state(uid); data["last_elements"] = values; _save(uid, "horizontal_rpg", data); _send(session, 9510, 0); return True


def _horizontal_battle(session, uid, body, request_id, result_id, boss=False):
    try: values = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError): return False
    data = _horizontal_state(uid)
    control = next(iter(_table("horizontal", "HorizontalRPGControlTable").values()), {})
    map_id = _int(values[0] if values else data.get("map"), _int(control.get("EXBossMapID")))
    team = _int(control.get("EXBossTeam")) if boss else _int(values[0] if values else 0)
    if map_id <= 0 or team <= 0 or not _start_module_battle(
        session, uid, "horizontal_boss" if boss else "horizontal", map_id,
        map_id, team, [], battle_type=6,
    ):
        return False
    _send(session, result_id, 0)
    return True


def handle_horizontal_combat(session, uid, body): return _horizontal_battle(session, uid, body, 9504, 9511)
def handle_horizontal_boss(session, uid, body): return _horizontal_battle(session, uid, body, 9505, 9512, True)


def handle_horizontal_dialog(session, uid, body, request_id=9506, result_id=9513):
    try: dialog, choices = protocol_codec.decode_method(request_id, body)
    except (ValueError, KeyError): return False
    data = _horizontal_state(uid); data.setdefault("dialogs", {})[str(_int(dialog))] = choices; _save(uid, "horizontal_rpg", data); _send(session, result_id, 0, _int(dialog)); return True


def handle_horizontal_weather(session, uid, body):
    try: (weather,) = protocol_codec.decode_method(9507, body)
    except (ValueError, KeyError): return False
    data = _horizontal_state(uid)
    if _int(weather) < 0 or _int(weather) > 10: return False
    data["weather"] = _int(weather); _save(uid, "horizontal_rpg", data); _send(session, 9514, 0, _int(weather)); return True


def handle_horizontal_quick(session, uid, body):
    try: maze, count = protocol_codec.decode_method(9508, body)
    except (ValueError, KeyError): return False
    if _int(maze) <= 0 or not 1 <= _int(count) <= 99: return False
    data = _horizontal_state(uid)
    if _int(maze) not in {_int(value) for value in data.get("finished", [])} and data.get("finished"):
        return False
    pairs = [(1, _int(count) * 100)]
    applied = _grant(session, uid, pairs)
    if applied is None: return False
    _send(session, 9515, 0, _item_show(pairs)); return True


def handle_horizontal_challenge(session, uid, body):
    try: level, maze = protocol_codec.decode_method(9522, body)
    except (ValueError, KeyError): return False
    row = _row("horizontal", "HorizontalRPGListTable", level) or _row("horizontal", "HorizontalRPGMapTable", level)
    if row is None: return False
    data = _horizontal_state(uid); data["level"], data["map"] = _int(level), _int(maze); _save(uid, "horizontal_rpg", data)
    map_pod = {"id": _int(row.get("Id")), "born": _int(row.get("BornPoint")), "region": _int(row.get("AreaId")), "currLevelCid": _int(level), "element": []}
    maze_pod = {"id": 0, "mazeCid": _int(maze), "isLocal": True, "randomSeed": _int(maze), "saveData": "", "saveVersion": 1, "carryItems": [], "mazePlayer": {}}
    _send(session, 9523, 0, _int(level), maze_pod, map_pod, 0); return True


def handle_horizontal_level_dialog(session, uid, body):
    try: level, choices = protocol_codec.decode_method(9529, body)
    except (ValueError, KeyError): return False
    data = _horizontal_state(uid); data.setdefault("level_dialogs", {})[str(_int(level))] = choices; _save(uid, "horizontal_rpg", data); _send(session, 9530, 0, _int(level)); return True


HORIZONTAL_DISPATCH = {
    9502: (handle_horizontal_element, True), 9503: (handle_horizontal_element_else, True), 9504: (handle_horizontal_combat, True),
    9505: (handle_horizontal_boss, True), 9506: (handle_horizontal_dialog, True), 9507: (handle_horizontal_weather, True),
    9508: (handle_horizontal_quick, True), 9522: (handle_horizontal_challenge, True), 9529: (handle_horizontal_level_dialog, True),
}


# ── Mini Galgame ────────────────────────────────────────────────────────────

MINIGAL_DEFAULTS = {"saves": {}, "active": 0, "flags": {}, "items": {}, "shop": {}, "tasks": [], "tower": []}


def _minigal_state(uid): return _state(uid, "mini_gal", MINIGAL_DEFAULTS)


def _minigal_pod(save):
    return {
        "basePOD": {"areaList": list(save.get("areas", [])), "baseProps": {int(k): _int(v) for k, v in save.get("props", {}).items()}, "day": _int(save.get("day"), 1), "dayOfPhase": _int(save.get("day_phase"), 1)},
        "dialogId": _int(save.get("dialog")), "girls": [], "itemUsedCount": {}, "items": {int(k): _int(v) for k, v in save.get("items", {}).items()},
        "localAchievementData": {}, "messageGroupCount": {}, "playthrough": _int(save.get("playthrough"), 1), "scheduleList": [], "shopRecord": {int(k): _int(v) for k, v in save.get("shop", {}).items()}, "taskList": list(save.get("tasks", [])), "towerRecord": list(save.get("tower", [])), "triggeredCount": {},
    }


def _minigal_active(data, save_id=None):
    key = _int(save_id if save_id is not None else data.get("active"))
    return data.get("saves", {}).get(str(key)), key


def handle_minigal_start(session, uid):
    data = _minigal_state(uid); key = max([_int(value) for value in data.get("saves", {}).keys()] or [0]) + 1
    save = {"day": 1, "day_phase": 1, "areas": [1], "props": {}, "dialog": 0, "items": {}, "shop": {}, "tasks": [], "tower": [], "playthrough": 1}
    data["saves"][str(key)] = save; data["active"] = key; _save(uid, "mini_gal", data); _send(session, 6812, 0, _minigal_pod(save)); return True


def handle_minigal_load(session, uid, body):
    try: (save_id,) = protocol_codec.decode_method(6803, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, key = _minigal_active(data, save_id)
    if save is None: return False
    data["active"] = key; _save(uid, "mini_gal", data); _send(session, 6813, 0, _minigal_pod(save)); return True


def handle_minigal_save(session, uid, body):
    try: (save_id,) = protocol_codec.decode_method(6804, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data, save_id)
    if save is None: return False
    save["saved_at"] = _now(); _save(uid, "mini_gal", data); _send(session, 6814, 0); return True


def handle_minigal_dialog(session, uid, body):
    try: dialog, choices = protocol_codec.decode_method(6805, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None: return False
    save["dialog"], save.setdefault("flags", {})[str(_int(dialog))] = _int(dialog), choices; _save(uid, "mini_gal", data); _send(session, 6815, 0, _int(dialog)); return True


def _minigal_cost_reward(item_id):
    row = _row("minigal", "GalgameMonsterShopTable", item_id)
    return _pairs((row or {}).get("Cost")), _pairs((row or {}).get("Reward"))


def handle_minigal_shop(session, uid, body):
    try: item_id, count = protocol_codec.decode_method(6806, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None or _int(count) <= 0 or _int(count) > 99: return False
    costs, rewards = _minigal_cost_reward(item_id)
    if not rewards: rewards = [(item_id, 1)]
    if _trade(session, uid, [(cid, qty * _int(count)) for cid, qty in costs], [(cid, qty * _int(count)) for cid, qty in rewards]) is None: return False
    save.setdefault("shop", {})[str(_int(item_id))] = _int(save.setdefault("shop", {}).get(str(_int(item_id)), 0)) + _int(count); _save(uid, "mini_gal", data); _send(session, 6816, 0); return True


def handle_minigal_item(session, uid, body):
    try: item_id, count = protocol_codec.decode_method(6807, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None or _int(save.get("items", {}).get(str(_int(item_id)), 0)) < _int(count) or _int(count) <= 0: return False
    save["items"][str(_int(item_id))] -= _int(count); _save(uid, "mini_gal", data); _send(session, 6817, 0); return True


def handle_minigal_game_over(session, uid, body):
    try: game_id, score, result = protocol_codec.decode_method(6808, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None or _int(score) < 0: return False
    reward = [(1, max(1, _int(score) // 10))]
    if _grant(session, uid, reward) is None: return False
    save.setdefault("games", {})[str(_int(game_id))] = {"score": _int(score), "result": _int(result)}; _save(uid, "mini_gal", data); _send(session, 6818, 0, []); return True


def handle_minigal_event(session, uid, body):
    try: (event_id,) = protocol_codec.decode_method(6809, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None: return False
    save.setdefault("events", []).append(_int(event_id)); _save(uid, "mini_gal", data); _send(session, 6819, 0); return True


def handle_minigal_tower(session, uid, body):
    try: (tower_id,) = protocol_codec.decode_method(6810, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None: return False
    if _int(tower_id) in save.get("tower", []): return False
    save.setdefault("tower", []).append(_int(tower_id)); reward = [(1, 100 + _int(tower_id))]
    if _grant(session, uid, reward) is None: return False
    _save(uid, "mini_gal", data); _send(session, 6820, 0, _int(tower_id), 1, [{"itemId": 1, "itemNum": 100 + _int(tower_id)}]); return True


def handle_minigal_call(session, uid, body):
    try: girl_id, call_id = protocol_codec.decode_method(6811, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None: return False
    save.setdefault("calls", []).append([_int(girl_id), _int(call_id)]); _save(uid, "mini_gal", data); _send(session, 6821, 0); return True


def handle_minigal_message(session, uid, body):
    try: group_id, message_id = protocol_codec.decode_method(6838, body)
    except (ValueError, KeyError): return False
    data = _minigal_state(uid); save, _ = _minigal_active(data)
    if save is None: return False
    save.setdefault("messages", []).append([_int(group_id), _int(message_id)]); _save(uid, "mini_gal", data); _send(session, 6839, 0, _int(group_id), _int(message_id)); return True


MINIGAL_DISPATCH = {
    6802: (handle_minigal_start, False), 6803: (handle_minigal_load, True), 6804: (handle_minigal_save, True), 6805: (handle_minigal_dialog, True),
    6806: (handle_minigal_shop, True), 6807: (handle_minigal_item, True), 6808: (handle_minigal_game_over, True), 6809: (handle_minigal_event, True),
    6810: (handle_minigal_tower, True), 6811: (handle_minigal_call, True), 6838: (handle_minigal_message, True),
}


# ── Card activity, chat, ranking and legacy guild actions ──────────────────

CARD_DEFAULTS = {"deck": {}, "equip": {}, "consume": {}, "story": [], "score": 0}


def _card_state(uid): return _state(uid, "card_activity", CARD_DEFAULTS)


def handle_card_fight(session, uid, body):
    try: activity_id, won, rounds = protocol_codec.decode_method(9702, body)
    except (ValueError, KeyError): return False
    data = _card_state(uid)
    level = _row("card", "CardActiveLevelTable", activity_id)
    if level is None:
        return False
    data["last_fight"] = {"activity": _int(activity_id), "won": bool(won), "rounds": _int(rounds)}
    rewards = [(_int(card), 1) for card in (level.get("RewardCard") or []) if _int(card) > 0] if won else []
    if won:
        data["score"] = _int(data.get("score")) + max(1, _int(rounds))
        if _grant(session, uid, rewards) is None:
            return False
    _save(uid, "card_activity", data); _send(session, 9708, 0, _int(data.get("score")), _int(rounds)); return True


def handle_card_deck(session, uid, body):
    try: deck_id, cards = protocol_codec.decode_method(9703, body)
    except (ValueError, KeyError): return False
    if not isinstance(cards, list) or len(cards) > 30 or len(set(cards)) != len(cards): return False
    data = _card_state(uid); data["deck"][str(_int(deck_id))] = [_int(card) for card in cards]; _save(uid, "card_activity", data); _send(session, 9709, 0); return True


def handle_card_equip(session, uid, body):
    try: values = protocol_codec.decode_method(9704, body)[0]
    except (ValueError, KeyError): return False
    data = _card_state(uid); data["equip"] = {str(_int(k)): _int(v) for k, v in values.items()}; _save(uid, "card_activity", data); _send(session, 9710, 0); return True


def handle_card_consume(session, uid, body):
    try: values = protocol_codec.decode_method(9705, body)[0]
    except (ValueError, KeyError): return False
    data = _card_state(uid); data["consume"] = {str(_int(k)): _int(v) for k, v in values.items()}; _save(uid, "card_activity", data); _send(session, 9711, 0); return True


def handle_card_story(session, uid, body):
    try: (story_id,) = protocol_codec.decode_method(9706, body)
    except (ValueError, KeyError): return False
    event = _row("card", "CardActiveEventControlTable", story_id)
    if event is None:
        return False
    rewards = _pairs(event.get("DialogReward"))
    key = f"story:{_int(story_id)}"; data = _card_state(uid)
    applied = storage.claim_reward_once(uid, "card_activity_claims", key, rewards)
    if applied is None: return False
    if applied.get("claimed"): data["story"].append(_int(story_id)); _save(uid, "card_activity", data)
    _send(session, 9712, 0, _item_show(rewards) if applied.get("claimed") else []); return True


def handle_card_boss(session, uid, body):
    try: (boss_id,) = protocol_codec.decode_method(9707, body)
    except (ValueError, KeyError): return False
    data = _card_state(uid); data["boss"] = _int(boss_id); _save(uid, "card_activity", data); _send(session, 9713, 0); return True


CHAT_DEFAULTS = {"room": 1, "messages": [], "reports": []}


def _chat_state(uid): return _state(uid, "chat", CHAT_DEFAULTS)


def handle_chat_send(session, uid, body):
    try: message = protocol_codec.decode_method(100102, body)[0]
    except (ValueError, KeyError): return False
    if not isinstance(message, dict) or not isinstance(message.get("content", ""), str) or len(message.get("content", "")) > 200: return False
    # The localized build has no public chat; acknowledge valid sends without
    # persisting or broadcasting the submitted message.
    data = _chat_state(uid)
    if data.get("messages"):
        data["messages"] = []
        _save(uid, "chat", data)
    _send(session, 100104, 0)
    return True


def handle_chat_room(session, uid, body):
    try: (room,) = protocol_codec.decode_method(100103, body)
    except (ValueError, KeyError): return False
    if not 1 <= _int(room) <= 100: return False
    data = _chat_state(uid)
    data["room"] = _int(room)
    data["messages"] = []
    _save(uid, "chat", data)
    _send(session, 100105, 0, {"roomNumber": _int(room), "onlineCount": 1, "msg": []})
    return True


def handle_chat_report(session, uid, body):
    try: values = protocol_codec.decode_method(100108, body)
    except (ValueError, KeyError): return False
    data = _chat_state(uid); data["reports"].append(values); _save(uid, "chat", data); _send(session, 100109, 0); return True


def _rank_value(uid, rank_id):
    scores = storage.get_player_state_json(uid, "rank_scores") or {}
    if str(rank_id) in scores:
        return max(0, _int(scores[str(rank_id)]))
    player = storage.get_player(uid) or {}
    if _int(rank_id) in (1, 1001):
        return max(0, _int(player.get("level"), 1))
    if _int(rank_id) in (2, 1002):
        attrs = storage.get_player_num_attrs(uid)
        return max(0, sum(max(0, _int(value)) for value in attrs.values()))
    remaining = storage.get_player_state_json(uid, "remaining_modules") or {}
    modules = remaining.get("modules", {}) if isinstance(remaining, dict) else {}
    dream = modules.get("net_dreamMap", {}).get("data", {}) if isinstance(modules, dict) else {}
    tower = modules.get("net_magicTower", {}).get("data", {}) if isinstance(modules, dict) else {}
    if _int(rank_id) in (3, 1003):
        return max(0, _int(dream.get("combo")))
    if _int(rank_id) in (4, 1004):
        return max(0, _int(tower.get("floor")))
    return max(0, _int(player.get("level"), 1))


def _ranking(uid, rank_id=0):
    player = storage.get_player(uid) or {}
    value = _rank_value(uid, rank_id)
    return {
        "pid": uid, "name": str(player.get("role_name", "local")),
        "pLv": _int(player.get("level"), 1), "value": value,
        "serverId": "offline-local", "avatarFrame": 0, "headIcon": 0,
        "guid": 0, "title": 0, "vip": 0, "updateTime": _int(player.get("updated_at"), _now()),
        "customData": "", "userData": "",
    }


def _rank_entries(uid, rank_id, self_only):
    players = storage.list_players()
    entries = [_ranking(player["uid"], rank_id) for player in players]
    entries.sort(key=lambda item: (-_int(item.get("value")), str(item.get("pid"))))
    if self_only:
        entries = [entry for entry in entries if str(entry.get("pid")) == str(uid)]
    return entries


def handle_rank(session, uid, body):
    try: rank_id, page, self_only = protocol_codec.decode_method(100202, body)
    except (ValueError, KeyError): return False
    entries = _rank_entries(uid, _int(rank_id), bool(self_only))
    page_size = 20
    page_number = max(1, _int(page, 1))
    start = (page_number - 1) * page_size
    page_entries = entries[start:start + page_size]
    _send(session, 100203, 0, _int(rank_id), bool(self_only), page_entries, page_entries,
          len(entries), _now(), "offline-local", page_number)
    return True


def handle_rank_user(session, uid, body):
    try: rank_id, self_only, page = protocol_codec.decode_method(100204, body)
    except (ValueError, KeyError): return False
    entries = _rank_entries(uid, _int(rank_id), bool(self_only))
    _send(session, 100205, 0, json.dumps({
        "rankId": _int(rank_id), "page": max(1, _int(page, 1)),
        "serverId": "offline-local", "entries": entries,
    }, ensure_ascii=False, separators=(",", ":")))
    return True


def handle_rank_goalie(session, uid, body):
    try: rank_id, page, goal = protocol_codec.decode_method(100206, body)
    except (ValueError, KeyError): return False
    entries = _rank_entries(uid, _int(rank_id), False)
    selected = next((entry for entry in entries if _int(entry.get("value")) >= _int(goal)), _ranking(uid, _int(rank_id)))
    _send(session, 100207, 0, selected, max(1, _int(page, 1)), _int(goal)); return True


def handle_guild_sign(session, uid):
    key = time.strftime("sign:%Y-%m-%d")
    applied = storage.claim_reward_once(uid, "guild_claims", key, [(1, 100)])
    if applied is None: return False
    _send(session, 7405, 0, _item_show([(1, 100)]) if applied.get("claimed") else []); return True


def handle_guild_quest_rewards(session, uid, body):
    try: quest_ids = protocol_codec.decode_method(7403, body)[0]
    except (ValueError, KeyError): return False
    if not isinstance(quest_ids, list): return False
    rewards = [(1, 100 * len(quest_ids))] if quest_ids else []
    applied = _grant(session, uid, rewards)
    if applied is None: return False
    _send(session, 7406, 0, [_int(value) for value in quest_ids], _item_show(rewards)); return True


def handle_guild_redpoint(session, uid):
    _send(session, 7407, 0, False); return True


def handle_guild_challenge_attack(session, uid, body):
    try: values = protocol_codec.decode_method(7502, body); challenge_id = values[-1]
    except (ValueError, KeyError): return False
    row = _row("guild", "GuildChallengeLayerTable", challenge_id)
    if row is None: return False
    costs = _pairs(row.get("Cost"))
    if costs and _trade(session, uid, costs, []) is None: return False
    data = _state(uid, "guild_challenge", {"score": 0, "cleared": []}); data["last"] = _int(challenge_id); _save(uid, "guild_challenge", data)
    _send(session, 7506, 0, _int(row.get("RecPower"))); return True


def handle_guild_challenge_rewards(session, uid, body):
    try: challenge_ids = protocol_codec.decode_method(7503, body)[0]
    except (ValueError, KeyError): return False
    rewards = []
    for challenge_id in challenge_ids:
        row = _row("guild", "GuildChallengeLayerTable", challenge_id)
        if row is not None:
            rewards.extend(_pairs(row.get("RewardShow")))
    if not rewards:
        return False
    applied = _grant(session, uid, rewards)
    if applied is None: return False
    _send(session, 7507, 0, [_int(value) for value in challenge_ids], _item_show(rewards)); return True


def handle_guild_challenge_mopup(session, uid, body):
    try: challenge_id, count = protocol_codec.decode_method(7504, body)
    except (ValueError, KeyError): return False
    if _int(count) <= 0 or _int(count) > 99: return False
    row = _row("guild", "GuildChallengeLayerTable", challenge_id)
    if row is None: return False
    cost = [(cid, qty * _int(count)) for cid, qty in _pairs(row.get("Cost"))]
    rewards = [(cid, qty * _int(count)) for cid, qty in _pairs(row.get("RewardShow"))]
    applied = _trade(session, uid, cost, rewards)
    if applied is None: return False
    _send(session, 7508, 0, _int(challenge_id), _item_show(rewards), _int(count)); return True


def handle_guild_challenge_score(session, uid, body):
    try: (challenge_id,) = protocol_codec.decode_method(7505, body)
    except (ValueError, KeyError): return False
    data = _state(uid, "guild_challenge", {"score": 0, "cleared": []})
    _send(session, 7509, 0, _int(data.get("score"))); return True


def handle_guild_training(session, uid, body):
    try: layer_id, formation_id = protocol_codec.decode_method(9002, body)
    except (ValueError, KeyError): return False
    row = _row("guild", "GuildTrainingLayerTable", layer_id)
    if row is None: return False
    data = _state(uid, "guild_training", {"points": 0, "attacks": []}); data["attacks"].append({"layer": _int(layer_id), "formation": _int(formation_id), "time": _now()}); data["points"] = _int(data.get("points")) + _int(row.get("PerPointDamage"), 1); _save(uid, "guild_training", data)
    _send(session, 9003, 0); return True


# ── Operation activities (turntable, group purchase, voting, welcome) ─────

OPERATION_DEFAULTS = {
    "turntables": {},
    "group_buys": {},
    "votes": {},
    "cup_votes": {},
    "welcome": {},
    "turntable_records": [],
    "followers": [],
}


def _operation_state(uid):
    return _state(uid, "operation_activities", OPERATION_DEFAULTS)


def _operation_control(event_id):
    return _row("operations", "OperateEventsControlTable", event_id) or {}


def _operation_cfg(event_id, table_name, default_id=1):
    """Resolve an operation event to its data configuration ID.

    Operation event IDs and data IDs are intentionally separate in the client
    POD.  The extracted control table is the source of truth; the fallback is
    only for older event IDs whose control row was not shipped in this build.
    """
    direct = _row("operations", table_name, event_id)
    if direct is not None:
        return _int(event_id), direct
    control = _operation_control(event_id)
    candidates = []
    if control:
        event_type = _int(control.get("Type"))
        type_defaults = {1: 1, 7: 1, 12: 1, 18: 220630, 21: 1}
        candidates.append(type_defaults.get(event_type, default_id))
    value = _int(event_id)
    if value >= 1000:
        candidates.extend((value // 1000, value // 10000))
    candidates.append(default_id)
    for candidate in candidates:
        row = _row("operations", table_name, candidate)
        if row is not None:
            return candidate, row
    return 0, None


def _operation_pod(event_id, data_cfg_id, **data_fields):
    pod = {
        "eventCfgId": _int(event_id),
        "dataCfgId": _int(data_cfg_id),
        "eventUid": 0,
        "status": 1,
        "startTime": 0,
        "endTime": 0,
        "closeTime": 0,
    }
    pod.update(data_fields)
    return pod


def _operation_reward_list(pairs):
    return _item_show([(cid, quantity) for cid, quantity in pairs if cid > 0 and quantity > 0])


def _turntable_reward(data_cfg_id, draw_index, uid, event_id):
    global_cfg = _row("operations", "ActiveTurntableGlobalTable", data_cfg_id)
    if not global_cfg:
        return None
    reward_ids = [_int(value) for value in (global_cfg.get("Rewards") or []) if _int(value) > 0]
    reward_rows = [
        _row("operations", "ActiveTurntableTable", reward_id)
        for reward_id in reward_ids
    ]
    reward_rows = [row for row in reward_rows if row and _pairs(row.get("Reward"))]
    if not reward_rows:
        return None
    seed_bytes = f"{uid}:{event_id}:{draw_index}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "little")
    choice = random.Random(seed).choice(reward_rows)
    pairs = _pairs(choice.get("Reward"))
    return pairs, _int(choice.get("Id")), _int(choice.get("Rare"))


def handle_turntable_draw(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(7002, body)
    except (ValueError, KeyError):
        return False
    data_cfg_id, config = _operation_cfg(event_id, "ActiveTurntableGlobalTable", 220630)
    if config is None:
        return False
    state = _operation_state(uid)
    event_key = str(_int(event_id))
    progress = state.setdefault("turntables", {}).setdefault(event_key, {"count": 0, "freeDay": "", "records": []})
    today = time.strftime("%Y-%m-%d")
    free_available = progress.get("freeDay") != today and _int(config.get("DailyFreeChance")) > 0
    draw_index = _int(progress.get("count"))
    reward = _turntable_reward(data_cfg_id, draw_index, uid, event_id)
    if reward is None:
        return False
    pairs, turntable_id, rare = reward
    costs = []
    if free_available:
        progress["freeDay"] = today
    else:
        costs = [(_int(config.get("CostItem")), _int(config.get("CostNum")))]
    applied = _trade(session, uid, costs, pairs)
    if applied is None:
        return False
    progress["count"] = draw_index + 1
    record = {
        "eventId": _int(event_id),
        "turntableId": turntable_id,
        "uuid": hashlib.sha1(f"{uid}:{event_id}:{draw_index}".encode()).hexdigest(),
        "goods": {str(cid): quantity for cid, quantity in pairs},
        "rare": rare,
        "time": _now(),
        "claimed": False,
    }
    progress.setdefault("records", []).append(record)
    state.setdefault("turntable_records", []).append(record)
    _save(uid, "operation_activities", state)
    _send(session, 7003, 0, turntable_id, record["uuid"])
    return True


def handle_turntable_log(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(100402, body)
    except (ValueError, KeyError):
        return False
    records = []
    for record in _operation_state(uid).get("turntable_records", []):
        if _int(record.get("eventId")) != _int(event_id):
            continue
        records.append({
            "goods": {_int(cid): _int(quantity) for cid, quantity in (record.get("goods") or {}).items()},
            "pid": uid,
            "pname": "local",
            "rare": _int(record.get("rare")),
            "time": _int(record.get("time")),
        })
    _send(session, 100404, 0, records[-50:])
    return True


def handle_turntable_receive(session, uid, body):
    try:
        event_id, uuid, name, phone, address = protocol_codec.decode_method(100403, body)
    except (ValueError, KeyError):
        return False
    if not uuid or len(name) > 80 or len(phone) > 40 or len(address) > 200:
        return False
    state = _operation_state(uid)
    found = next((record for record in state.get("turntable_records", []) if record.get("uuid") == uuid and _int(record.get("eventId")) == _int(event_id)), None)
    if found is None:
        return False
    found.update({"name": name, "phone": phone, "address": address, "claimed": True})
    _save(uid, "operation_activities", state)
    _send(session, 100405, 0)
    return True


def _group_buy_data(event_id):
    data_cfg_id, global_cfg = _operation_cfg(event_id, "GroupBuyGlobalTable", 1)
    if global_cfg is None:
        return None, None, None
    pack_ids = [_int(value) for value in (global_cfg.get("Items") or []) if _int(value) > 0]
    return data_cfg_id, global_cfg, pack_ids


def _group_buy_pod(event_id, data_cfg_id, counts):
    return _operation_pod(event_id, data_cfg_id, gpData={"buyCount": {int(k): int(v) for k, v in counts.items()}})


def handle_group_buy_info(session, uid, body):
    try:
        (data_cfg_id,) = protocol_codec.decode_method(100502, body)
    except (ValueError, KeyError):
        return False
    if _row("operations", "GroupBuyGlobalTable", data_cfg_id) is None and _row("operations", "GroupBuyGlobalTable", 1) is None:
        return False
    state = _operation_state(uid)
    counts = state.get("group_totals", {})
    _send(session, 100503, 0, {"totalCount": {int(k): _int(v) for k, v in counts.items()}})
    return True


def handle_group_buy(session, uid, body):
    try:
        event_id, pack_id, count = protocol_codec.decode_method(5002, body)
    except (ValueError, KeyError):
        return False
    if _int(count) <= 0 or _int(count) > 99:
        return False
    data_cfg_id, global_cfg, pack_ids = _group_buy_data(event_id)
    if global_cfg is None or _int(pack_id) not in pack_ids:
        return False
    pack = _row("operations", "GroupBuyPackTable", pack_id)
    if pack is None:
        return False
    state = _operation_state(uid)
    bought = _int(state.setdefault("group_buys", {}).get(str(_int(event_id)), {}).get(str(_int(pack_id))))
    limit = _int(pack.get("TimesLimit"))
    if limit and bought + _int(count) > limit:
        return False
    if _int(pack.get("payPoint")) > 0:
        costs = [(5, _int(pack.get("payPoint")) * _int(count))]
    else:
        price = _pairs(pack.get("Price"))
        costs = [(cid, quantity * _int(count)) for cid, quantity in price]
    rewards = [(cid, quantity * _int(count)) for cid, quantity in _pairs(pack.get("Reward"))]
    applied = _trade(session, uid, costs, rewards)
    if applied is None:
        return False
    event_counts = state["group_buys"].setdefault(str(_int(event_id)), {})
    event_counts[str(_int(pack_id))] = bought + _int(count)
    totals = state.setdefault("group_totals", {})
    totals[str(_int(pack_id))] = _int(totals.get(str(_int(pack_id)))) + _int(count)
    _save(uid, "operation_activities", state)
    pod = _group_buy_pod(event_id, data_cfg_id, event_counts)
    _send(session, 5003, 0, _operation_reward_list(rewards), pod)
    return True


def handle_vote_info(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(100602, body)
    except (ValueError, KeyError):
        return False
    if _operation_cfg(event_id, "VoteGlobalTable", 1)[1] is None:
        return False
    state = _operation_state(uid)
    totals = state.get("vote_totals", {})
    _send(session, 100603, 0, {"votes": {int(k): _int(v) for k, v in totals.items()}})
    return True


def handle_vote(session, uid, body):
    try:
        event_id, vote_id = protocol_codec.decode_method(6202, body)
    except (ValueError, KeyError):
        return False
    _, config = _operation_cfg(event_id, "VoteGlobalTable", 1)
    if config is None or _int(vote_id) <= 0:
        return False
    need = _pairs(config.get("VoteNeeds"))
    state = _operation_state(uid)
    own = state.setdefault("votes", {}).setdefault(str(_int(event_id)), {})
    if _int(own.get(str(_int(vote_id)))) > 0:
        return False
    if _trade(session, uid, need, []) is None:
        return False
    own[str(_int(vote_id))] = 1
    totals = state.setdefault("vote_totals", {})
    totals[str(_int(vote_id))] = _int(totals.get(str(_int(vote_id)))) + 1
    _save(uid, "operation_activities", state)
    _send(session, 6203, 0, _int(event_id), _int(vote_id), [])
    return True


def _simple_player(uid):
    player = storage.get_player(uid) or {}
    return {
        "avatarFrame": _int(player.get("avatar_frame")),
        "chatBackground": 0,
        "guid": _int(player.get("guid")),
        "guildId": _int(player.get("guild_id")),
        "headIcon": _int(player.get("head_icon")),
        "leaderCid": _int(player.get("leader_cid"), 20010001),
        "pLv": _int(player.get("level"), 1),
        "pName": str(player.get("role_name") or "local"),
        "pid": uid,
        "serverId": "local",
        "showGirlDressId": 0,
        "title": 0,
        "vip": 0,
    }


def handle_newbies_followers(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(100702, body)
    except (ValueError, KeyError):
        return False
    if _operation_cfg(event_id, "WelcomeNewcomersGlobalTable", 1)[1] is None:
        return False
    followers = []
    for item in _operation_state(uid).get("followers", []):
        if not isinstance(item, dict):
            continue
        followers.append({"lastMazeId": _int(item.get("lastMazeId")), "pod": item.get("pod") or _simple_player(uid)})
    _send(session, 100703, 0, followers)
    return True


def handle_newbies_submit(session, uid, body):
    try:
        event_id, invite_code = protocol_codec.decode_method(6302, body)
    except (ValueError, KeyError):
        return False
    if not invite_code or len(invite_code) > 32 or _operation_cfg(event_id, "WelcomeNewcomersGlobalTable", 1)[1] is None:
        return False
    state = _operation_state(uid)
    welcome = state.setdefault("welcome", {})
    if welcome.get("usedInviteCode"):
        return False
    welcome.update({"usedInviteCode": _int(invite_code) if invite_code.isdigit() else 0, "inviteCode": invite_code, "useCode": True})
    _save(uid, "operation_activities", state)
    _send(session, 6304, 0)
    return True


def handle_newbies_task(session, uid, body):
    try:
        event_id, task_id = protocol_codec.decode_method(6303, body)
    except (ValueError, KeyError):
        return False
    _, config = _operation_cfg(event_id, "WelcomeNewcomersGlobalTable", 1)
    task = _row("operations", "WelcomeTaskListTable", task_id)
    if config is None or task is None:
        return False
    state = _operation_state(uid)
    welcome = state.setdefault("welcome", {"eventTask": {}, "finishedTask": []})
    complete = _int(welcome.setdefault("eventTask", {}).get(str(_int(task_id))))
    if complete < _int(task.get("NeedNum")) or _int(task_id) in [_int(value) for value in welcome.setdefault("finishedTask", [])]:
        return False
    rewards = _pairs(task.get("Reward"))
    applied = _grant(session, uid, rewards)
    if applied is None:
        return False
    welcome["finishedTask"].append(_int(task_id))
    _save(uid, "operation_activities", state)
    _send(session, 6305, 0, _operation_reward_list(rewards))
    return True


def handle_cup_vote_info(session, uid, body):
    try:
        (event_id,) = protocol_codec.decode_method(100802, body)
    except (ValueError, KeyError):
        return False
    if _operation_cfg(event_id, "CupMatchVoteGlobalTable", 1)[1] is None:
        return False
    state = _operation_state(uid)
    totals = state.get("cup_totals", {})
    _send(session, 100803, 0, {
        "stage": 1,
        "votes": {int(k): _int(v) for k, v in totals.items()},
        "group": {},
        "knockoutTime": 0,
        "finalTime": 0,
        "finishTime": 0,
        "lastVoteTime": {},
    })
    return True


def handle_cup_vote(session, uid, body):
    try:
        event_id, vote_id = protocol_codec.decode_method(7302, body)
    except (ValueError, KeyError):
        return False
    _, config = _operation_cfg(event_id, "CupMatchVoteGlobalTable", 1)
    group = _row("operations", "CupMatchVoteGroupTable", vote_id)
    if config is None or group is None:
        return False
    state = _operation_state(uid)
    cup = state.setdefault("cup_votes", {}).setdefault(str(_int(event_id)), {"tickets": 3, "votes": {}})
    if _int(cup.get("tickets")) <= 0:
        return False
    if _int(cup.setdefault("votes", {}).get(str(_int(vote_id)))) > 0:
        return False
    cup["tickets"] = _int(cup.get("tickets")) - 1
    cup["votes"][str(_int(vote_id))] = 1
    totals = state.setdefault("cup_totals", {})
    totals[str(_int(vote_id))] = _int(totals.get(str(_int(vote_id)))) + 1
    _save(uid, "operation_activities", state)
    _send(session, 7303, 0, _int(event_id), _int(vote_id), [])
    return True


# ── Recovered challenge and activity modules ─────────────────────────────

def _decode_or_reject(request_id, body):
    try:
        return protocol_codec.decode_method(request_id, body or b"")
    except (KeyError, TypeError, ValueError):
        return None


def _send_code(session, result_id, code=0):
    _send(session, result_id, int(code))


def _module_state(uid, field, defaults=None):
    data = _state(uid, field, defaults or {})
    return data


def _day_key():
    return time.strftime("%Y-%m-%d", time.localtime())


def _start_module_battle(
    session,
    uid,
    module,
    key,
    map_id,
    monster_team_id,
    rewards,
    battle_type=4,
):
    """Create the same server-owned battle used by maze fights.

    The pending context is deliberately separate from the client request.  It
    is consumed by handle_battle_completion only after tcp_server validates and
    settles fightOver.
    """
    if hasattr(session, "account") and not getattr(session, "account"):
        return False
    starter = getattr(session, "_send_notify_start_fight", None)
    if not callable(starter):
        return False
    battle_type = _int(battle_type)
    if battle_type <= 0 or _int(map_id) < 0 or _int(monster_team_id) < 0:
        return False
    # A module request must never abandon an unrelated maze/activity battle or
    # replace a pending module context.  The client can retry after reconnect;
    # it must settle or explicitly abandon the existing server-owned instance.
    # A player has one server-owned fight slot. Checking only the requested
    # type would allow a module fight to overlap a maze or activity fight.
    active = storage.get_active_battle(uid)
    if active is not None:
        pending = _module_state(uid, "module_battles", {})
        pending_active = pending.get("active")
        if isinstance(pending_active, dict) and str(pending_active.get("battleId")) == str(active.get("id")):
            return False
        return False
    if not starter(
        battle_type=battle_type,
        map_id=_int(map_id),
        monster_team_id=_int(monster_team_id),
        reward_pairs=list(rewards or []),
    ):
        return False
    active = storage.get_active_battle(uid, battle_type)
    if not active:
        # Protocol-only test doubles may model the notify call without a
        # database-backed Session.  Production sessions always expose
        # ``account`` and therefore cannot pass this boundary silently.
        return not hasattr(session, "account")
    pending = _module_state(uid, "module_battles", {})
    pending["active"] = {
        "battleId": str(active["id"]),
        "module": str(module),
        "key": _int(key),
        "mapId": _int(map_id),
        "monsterTeamId": _int(monster_team_id),
        "battleType": battle_type,
        "createdAt": _now(),
    }
    _save(uid, "module_battles", pending)
    return True


def _module_battle_slot_available(uid, battle_type=4):
    return storage.get_active_battle(uid, _int(battle_type)) is None


def _plot_reward(row, first=False):
    return _pairs(row.get("FirstChallengeReward" if first else "ChallengeReward"))


def handle_daily_dup_buy_count(session, uid, body):
    values = _decode_or_reject(2802, body)
    if values is None:
        return False
    (dup_id,) = values
    row = _row("daily_dup", "DailyDupTable", dup_id)
    if row is None or _int(dup_id) <= 0:
        _send_code(session, 2803, 1)
        return True
    state = _module_state(uid, "daily_dup", {"day": _day_key(), "buyCount": {}})
    if state.get("day") != _day_key():
        state["day"] = _day_key()
        state["buyCount"] = {}
    counts = state.setdefault("buyCount", {})
    key = str(_int(dup_id))
    current = _int(counts.get(key))
    # Empty BuyTimesCost is the official data for this client version.  The
    # server still enforces the configured daily limit when one exists.
    costs = row.get("BuyTimesCost") or []
    if costs and current >= len(costs):
        _send_code(session, 2803, 1)
        return True
    limit = _int(row.get("Times"), 0)
    if limit > 0 and current >= limit:
        _send_code(session, 2803, 1)
        return True
    if costs:
        cost = costs[current]
        cost_pairs = _pairs(cost if isinstance(cost, list) else [])
        if _trade(session, uid, cost_pairs, []) is None:
            _send_code(session, 2803, 1)
            return True
    counts[key] = current + 1
    _save(uid, "daily_dup", state)
    _send_code(session, 2803, 0)
    return True


def _battle_pass_state(uid):
    state = _module_state(uid, "battle_pass", {
        "season": 1,
        "level": 0,
        "exp": 0,
        "advanced": False,
        "claimedFree": [],
        "claimedPay": [],
        "lastSeasonClaimed": False,
    })
    state.setdefault("claimedFree", [])
    state.setdefault("claimedPay", [])
    return state


def handle_battle_pass_rewards(session, uid, body):
    values = _decode_or_reject(4802, body)
    if values is None:
        return False
    (reward_ids,) = values
    if not reward_ids or len(reward_ids) > 100:
        _send_code(session, 4804, 1)
        return True
    state = _battle_pass_state(uid)
    season = _int(state.get("season"), 1)
    level = max(_int(state.get("level")), _int(state.get("exp")) // 200)
    rows = []
    for reward_id in reward_ids:
        row = _row("battle_pass", "BattlePassRewardTable", reward_id)
        if row is None or _int(row.get("BattlePassSeason")) != season:
            _send_code(session, 4804, 1)
            return True
        if _int(row.get("SeasonLv")) > level:
            _send_code(session, 4804, 1)
            return True
        rows.append(row)
    claimed_free = {str(_int(value)) for value in state.get("claimedFree", [])}
    claimed_pay = {str(_int(value)) for value in state.get("claimedPay", [])}
    for reward_id, row in zip(reward_ids, rows):
        key = str(_int(reward_id))
        if key not in claimed_free:
            applied = storage.claim_reward_once(
                uid, "battle_pass_claims", "free:" + key, _pairs(row.get("FreeReward"))
            )
            if applied is None:
                _send_code(session, 4804, 1)
                return True
            for cid, quantity in applied.get("changed_attrs", {}).items():
                _send(session, 3924, {int(cid): int(quantity)})
            if applied.get("changed_items"):
                _send(session, 4102, applied["changed_items"])
            claimed_free.add(key)
        if bool(state.get("advanced")) and key not in claimed_pay:
            applied = storage.claim_reward_once(
                uid, "battle_pass_claims", "pay:" + key, _pairs(row.get("PayReward"))
            )
            if applied is None:
                _send_code(session, 4804, 1)
                return True
            for cid, quantity in applied.get("changed_attrs", {}).items():
                _send(session, 3924, {int(cid): int(quantity)})
            if applied.get("changed_items"):
                _send(session, 4102, applied["changed_items"])
            claimed_pay.add(key)
    state["claimedFree"] = sorted(int(value) for value in claimed_free)
    state["claimedPay"] = sorted(int(value) for value in claimed_pay)
    _save(uid, "battle_pass", state)
    _send_code(session, 4804, 0)
    return True


def handle_battle_pass_last_season(session, uid):
    state = _battle_pass_state(uid)
    if state.get("lastSeasonClaimed"):
        _send_code(session, 4805, 0)
        return True
    state["lastSeasonClaimed"] = True
    _save(uid, "battle_pass", state)
    _send_code(session, 4805, 0)
    return True


def _panda_state(uid):
    cfg = _row("panda", "PandaActivityGlobalTable", 1) or {}
    max_favor = _int((cfg.get("ExtraRewardNeeds") or [10000, 0])[0], 10000)
    data = _module_state(uid, "panda", {
        "favorNum": 0,
        "maxFavorNum": max_favor,
        "getGifts": [],
        "exploreCount": 0,
        "forest": False,
        "events": [],
        "completedEvents": [],
        "dialog": 0,
    })
    data["maxFavorNum"] = max_favor
    data["favorNum"] = min(max(0, _int(data.get("favorNum"))), max_favor)
    return data


def handle_panda_action(session, uid, body):
    values = _decode_or_reject(6002, body)
    if values is None:
        return False
    (action_type,) = values
    cfg = _row("panda", "PandaActivityGlobalTable", 1) or {}
    action_cfg = {
        1: (cfg.get("FeedingNeeds"), _int(cfg.get("FeedingFavor"))),
        2: (cfg.get("BathingNeeds"), _int(cfg.get("BathingFavor"))),
        3: (cfg.get("PlayNeeds"), _int(cfg.get("PlayFavor"))),
    }.get(_int(action_type))
    if action_cfg is None:
        _send(session, 6007, 1, _int(action_type), 0, 0, 0, [])
        return True
    cost = _pairs(action_cfg[0])
    if _trade(session, uid, cost, []) is None:
        _send(session, 6007, 1, _int(action_type), 0, 0, 0, [])
        return True
    data = _panda_state(uid)
    old = _int(data.get("favorNum"))
    maximum = max(0, _int(data.get("maxFavorNum"), 10000))
    new = min(maximum, old + max(0, _int(action_cfg[1])))
    data["favorNum"] = new
    _save(uid, "panda", data)
    _send(session, 6007, 0, _int(action_type), new - old, new, maximum, [])
    return True


def handle_panda_get_gift(session, uid, body):
    values = _decode_or_reject(6003, body)
    if values is None:
        return False
    (gift_id,) = values
    row = _row("panda", "PandaGiftListTable", gift_id)
    data = _panda_state(uid)
    if row is None or _int(row.get("Team")) != 1:
        _send(session, 6008, 1, _int(gift_id), [])
        return True
    if _int(row.get("FavorNum")) > _int(data.get("favorNum")) or _int(gift_id) in data.get("getGifts", []):
        _send(session, 6008, 1, _int(gift_id), [])
        return True
    applied = storage.claim_reward_once(uid, "panda_claims", str(_int(gift_id)), _pairs(row.get("Reward")))
    if applied is None:
        _send(session, 6008, 1, _int(gift_id), [])
        return True
    data.setdefault("getGifts", []).append(_int(gift_id))
    data["getGifts"] = sorted(set(_int(value) for value in data["getGifts"]))
    _save(uid, "panda", data)
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    _send(session, 6008, 0, _int(gift_id), _item_show(_pairs(row.get("Reward"))))
    return True


def handle_panda_enter(session, uid):
    data = _panda_state(uid)
    data["forest"] = True
    event_ids = sorted(
        _int(row.get("Id")) for row in _table("panda", "PandaEventListTable").values()
        if isinstance(row, dict) and _int(row.get("Group"), 1) == 1
    )
    data["events"] = event_ids[:6]
    _save(uid, "panda", data)
    _send(session, 6009, 0, event_ids[:6], 0)
    return True


def handle_panda_explore(session, uid):
    data = _panda_state(uid)
    if not data.get("forest"):
        _send(session, 6010, 1, [])
        return True
    data["exploreCount"] = _int(data.get("exploreCount")) + 1
    seed = int(hashlib.sha256((str(uid) + str(data["exploreCount"])).encode()).hexdigest()[:8], 16)
    rows = [row for row in _table("panda", "PandaEventListTable").values() if isinstance(row, dict)]
    rng = random.Random(seed)
    rng.shuffle(rows)
    events = [_int(row.get("Id")) for row in rows[: min(6, len(rows))]]
    data["events"] = events
    _save(uid, "panda", data)
    _send(session, 6010, 0, events)
    return True


def handle_panda_event(session, uid, body):
    values = _decode_or_reject(6006, body)
    if values is None:
        return False
    event_id, params = values
    row = _row("panda", "PandaEventListTable", event_id)
    data = _panda_state(uid)
    if row is None or not data.get("forest") or _int(event_id) not in data.get("events", []):
        _send(session, 6011, 1, _int(event_id), list(params or []))
        return True
    if not isinstance(params, list) or len(params) > 32 or not all(isinstance(value, str) for value in params):
        return False
    completed = set(_int(value) for value in data.get("completedEvents", []))
    if _int(event_id) in completed:
        _send(session, 6011, 1, _int(event_id), list(params))
        return True
    event_type = _int(row.get("Type"))
    if event_type == 1:
        data["dialog"] = _int(row.get("Parameter"))
        completed.add(_int(event_id))
        _save(uid, "panda", data)
    elif event_type == 2:
        monster_type = _int(row.get("Parameter"))
        monsters = [
            item for item in _table("panda", "PandaMonsterListTable").values()
            if isinstance(item, dict) and _int(item.get("MonsterType")) == monster_type
        ]
        if not monsters:
            _send(session, 6011, 1, _int(event_id), list(params))
            return True
        # The event carries the monster type; difficulty is selected by the
        # local event sequence, while every candidate remains config-backed.
        monsters.sort(key=lambda item: (_int(item.get("Difficulty")), _int(item.get("Id"))))
        selected = monsters[_int(data.get("exploreCount")) % len(monsters)]
        rewards = _pairs(selected.get("RewardShow"))
        if not _start_module_battle(
            session, uid, "panda", event_id, 0, _int(selected.get("MonsterTeam")), rewards
        ):
            return False
        data["pendingEvent"] = _int(event_id)
    elif event_type == 3:
        reward = [(_int(row.get("Reward")), 1)] if _int(row.get("Reward")) > 0 else []
        if _int(event_id) not in completed and reward:
            applied = storage.claim_reward_once(uid, "panda_event_claims", str(_int(event_id)), reward)
            if applied is None:
                return False
            completed.add(_int(event_id))
            for cid, quantity in applied.get("changed_attrs", {}).items():
                _send(session, 3924, {int(cid): int(quantity)})
            if applied.get("changed_items"):
                _send(session, 4102, applied["changed_items"])
            _send(session, 6012, _int(event_id), _item_show(reward))
    else:
        _send(session, 6011, 1, _int(event_id), list(params))
        return True
    data["completedEvents"] = sorted(completed)
    _save(uid, "panda", data)
    _send(session, 6011, 0, _int(event_id), list(params))
    if event_type == 1 and _int(data.get("dialog")) > 0:
        _send(session, 6016, _int(data["dialog"]))
    return True


def handle_panda_select_dialog(session, uid, body):
    values = _decode_or_reject(6014, body)
    if values is None:
        return False
    select_index, skip = values
    if _int(select_index) < 0 or not isinstance(skip, list) or len(skip) > 64 or not all(isinstance(value, int) and value >= 0 for value in skip):
        return False
    data = _panda_state(uid)
    data["dialog"] = 0
    _save(uid, "panda", data)
    _send(session, 6015, 0, -1)
    return True


def _tale_state(uid):
    return _module_state(uid, "tale_challenge", {
        "passedNode": [],
        "unlockedBoss": False,
        "unlockedDifficulty": [],
        "dialogId": 0,
        "drawPools": {},
    })


def _tale_global(data_cfg_id=1):
    rows = _table("tale_challenge", "PlotChallengeActivityGlobalTable")
    row = rows.get(str(_int(data_cfg_id)))
    if row is None:
        row = next(iter(rows.values()), None)
    return row if isinstance(row, dict) else {}


def _tale_node_unlocked(data, row):
    """Require every earlier node in the same difficulty to be cleared."""
    difficulty = _int(row.get("Difficulty"), 1)
    order = _int(row.get("Order"))
    if order <= 0:
        return False
    passed = {_int(value) for value in data.get("passedNode", [])}
    for prior in _table("tale_challenge", "PlotChallengeActivityTable").values():
        if not isinstance(prior, dict) or _int(prior.get("Difficulty"), 1) != difficulty:
            continue
        if 0 < _int(prior.get("Order")) < order and _int(prior.get("Id")) not in passed:
            return False
    return True


def handle_tale_story(session, uid, body):
    values = _decode_or_reject(6402, body)
    if values is None:
        return False
    (node_id,) = values
    row = _row("tale_challenge", "PlotChallengeActivityTable", node_id)
    if row is None or _int(row.get("Type")) != 1:
        _send_code(session, 6405, 1)
        return True
    data = _tale_state(uid)
    if not _tale_node_unlocked(data, row):
        _send_code(session, 6405, 1)
        return True
    passed = set(_int(value) for value in data.get("passedNode", []))
    first = _int(node_id) not in passed
    rewards = _plot_reward(row, first=first)
    if first and rewards:
        applied = storage.claim_reward_once(uid, "tale_story_claims", str(_int(node_id)), rewards)
        if applied is None:
            _send_code(session, 6405, 1)
            return True
        for cid, quantity in applied.get("changed_attrs", {}).items():
            _send(session, 3924, {int(cid): int(quantity)})
        if applied.get("changed_items"):
            _send(session, 4102, applied["changed_items"])
    passed.add(_int(node_id))
    data["passedNode"] = sorted(passed)
    _save(uid, "tale_challenge", data)
    _send_code(session, 6405, 0)
    _send(session, 6408, _int(node_id), _item_show(rewards))
    parameter = _int(row.get("Parameter"))
    if parameter > 0:
        _send(session, 6413, parameter)
    return True


def handle_tale_fight(session, uid, body):
    values = _decode_or_reject(6403, body)
    if values is None:
        return False
    node_id, formation_id = values
    row = _row("tale_challenge", "PlotChallengeActivityTable", node_id)
    if row is None or _int(row.get("Type")) != 2 or _int(formation_id) <= 0:
        _send_code(session, 6406, 1)
        return True
    data = _tale_state(uid)
    if not _tale_node_unlocked(data, row):
        _send_code(session, 6406, 1)
        return True
    if not _module_battle_slot_available(uid):
        _send_code(session, 6406, 1)
        return True
    passed = set(_int(value) for value in data.get("passedNode", []))
    rewards = _plot_reward(row, first=_int(node_id) not in passed)
    need = _pairs(row.get("ChallengeNeed"))
    if need and _trade(session, uid, need, []) is None:
        _send_code(session, 6406, 1)
        return True
    if not _start_module_battle(
        session, uid, "tale", node_id, _int(row.get("BattleMapID")), _int(row.get("Parameter")), rewards
    ):
        return False
    data["pendingFormation"] = _int(formation_id)
    _save(uid, "tale_challenge", data)
    _send_code(session, 6406, 0)
    return True


def handle_tale_boss(session, uid, body):
    values = _decode_or_reject(6404, body)
    if values is None:
        return False
    (formation_id,) = values
    cfg = _tale_global(1)
    data = _tale_state(uid)
    unlock_need = _int(cfg.get("UnLockBossNeed")) if cfg else 0
    passed = {_int(value) for value in data.get("passedNode", [])}
    if _int(formation_id) <= 0 or not cfg or (unlock_need > 0 and unlock_need not in passed):
        _send_code(session, 6407, 1)
        return True
    if not _module_battle_slot_available(uid):
        _send_code(session, 6407, 1)
        return True
    need = _pairs(cfg.get("ChallengeBossNeed"))
    if need and _trade(session, uid, need, []) is None:
        _send_code(session, 6407, 1)
        return True
    if not _start_module_battle(
        session, uid, "tale_boss", 0, _int(cfg.get("BossMapID")), _int(cfg.get("BossTeam")), []
    ):
        return False
    data["pendingFormation"] = _int(formation_id)
    _save(uid, "tale_challenge", data)
    _send_code(session, 6407, 0)
    return True


def handle_tale_select_dialog(session, uid, body):
    values = _decode_or_reject(6411, body)
    if values is None:
        return False
    select_index, skip = values
    if not isinstance(skip, list) or len(skip) > 64 or not all(isinstance(value, int) and value >= 0 for value in skip):
        return False
    data = _tale_state(uid)
    data["dialogId"] = 0
    _save(uid, "tale_challenge", data)
    _send(session, 6412, 0, -1)
    return True


def handle_tale_draw(session, uid, body):
    values = _decode_or_reject(6414, body)
    if values is None:
        return False
    pool_id, count = values
    pool = _row("tale_challenge", "PlotChallengeActivityRewardPoolTable", pool_id)
    count = _int(count)
    if pool is None or count <= 0 or count > 10:
        _send(session, 6415, 1, _int(pool_id), count, [], {})
        return True
    data = _tale_state(uid)
    drawn = data.setdefault("drawPools", {}).setdefault(str(_int(pool_id)), {})
    item_ids = pool.get("ItemID") or []
    item_nums = pool.get("ItemNum") or []
    item_times = pool.get("ItemTime") or []
    item_weights = pool.get("ItemWeight") or []
    available = []
    for index, item_id in enumerate(item_ids):
        total = _int(item_times[index]) if index < len(item_times) else 0
        used = _int(drawn.get(str(index)))
        if _int(item_id) > 0 and total > used:
            available.append((index, max(1, _int(item_weights[index]) if index < len(item_weights) else 1)))
    if sum(1 for _ in available) == 0 or count > sum(max(0, _int(item_times[index]) - _int(drawn.get(str(index)))) for index, _ in available):
        _send(session, 6415, 1, _int(pool_id), count, [], {})
        return True
    global_cfg = _tale_global(1)
    rng = random.Random(int(uid.encode().hex()[:8], 16) ^ _int(pool_id) ^ _int(drawn.get("total")))
    rewards = []
    draw_info = {}
    for _ in range(count):
        available = [
            (index, weight) for index, weight in available
            if _int(item_times[index]) > _int(drawn.get(str(index)))
        ]
        total_weight = sum(weight for _, weight in available)
        pick = rng.uniform(0, total_weight)
        selected = available[-1][0]
        for index, weight in available:
            pick -= weight
            if pick <= 0:
                selected = index
                break
        drawn[str(selected)] = _int(drawn.get(str(selected))) + 1
        drawn["total"] = _int(drawn.get("total")) + 1
        draw_info[selected] = _int(draw_info.get(selected)) + 1
        cid = _int(item_ids[selected])
        quantity = _int(item_nums[selected]) if selected < len(item_nums) else 1
        if cid <= 0 or quantity <= 0:
            return False
        rewards.append((cid, quantity))
    ticket = _pairs([_int(global_cfg.get("Ticket")), count])
    applied = _trade(session, uid, ticket, rewards)
    if applied is None:
        _send(session, 6415, 1, _int(pool_id), count, [], {})
        return False
    data["drawPools"][str(_int(pool_id))] = drawn
    _save(uid, "tale_challenge", data)
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    _send(session, 6415, 0, _int(pool_id), count, _item_show(rewards), {int(k): int(v) for k, v in draw_info.items()})
    return True


def _turntable_state(uid):
    return _module_state(uid, "limited_turntable", {"drawCount": 0, "rewards": {}, "history": []})


def _turntable_pick(data, count, rng, group_id=None):
    rows = []
    for row in _table("limited_turntable", "LimitedTurntableGroupTable").values():
        if not isinstance(row, dict):
            continue
        if group_id is not None and _int(row.get("Group")) != _int(group_id):
            continue
        limit = _int(row.get("TotalLimit"), -1)
        used = _int(data.setdefault("rewards", {}).get(str(_int(row.get("Id")))))
        if limit == -1 or used < limit:
            rows.append((row, max(1, _int(row.get("Odds")))))
    if not rows:
        return None
    picked = []
    for _ in range(count):
        current = [(row, weight) for row, weight in rows if _int(row.get("TotalLimit"), -1) == -1 or _int(data["rewards"].get(str(_int(row.get("Id"))))) < _int(row.get("TotalLimit"))]
        if not current:
            return None
        total = sum(weight for _row_data, weight in current)
        marker = rng.uniform(0, total)
        chosen = current[-1][0]
        for row, weight in current:
            marker -= weight
            if marker <= 0:
                chosen = row
                break
        key = str(_int(chosen.get("Id")))
        data["rewards"][key] = _int(data["rewards"].get(key)) + 1
        picked.append(chosen)
    return picked


def handle_limited_turntable_draw(session, uid, body):
    values = _decode_or_reject(7202, body)
    if values is None:
        return False
    data_cfg_id, count = values
    cfg = _row("limited_turntable", "LimitedTurntableGlobalTable", data_cfg_id)
    count = _int(count)
    state = _turntable_state(uid)
    if cfg is None or count <= 0 or count > 10 or _int(state.get("drawCount")) + count > _int(cfg.get("MaxTimes")):
        _send(session, 7204, 1, [], [], {"eventCfgId": _int(data_cfg_id), "dataCfgId": _int(data_cfg_id)})
        return True
    candidate = json.loads(json.dumps(state, ensure_ascii=False))
    picked = _turntable_pick(
        candidate, count,
        random.Random(int(uid.encode().hex()[:8], 16) ^ _int(state.get("drawCount"))),
        _int(cfg.get("RewardsGroup")),
    )
    if not picked:
        _send(session, 7204, 1, [], [], {"eventCfgId": _int(data_cfg_id), "dataCfgId": _int(data_cfg_id)})
        return True
    rewards = [_pairs(row.get("Reward"))[0] for row in picked if _pairs(row.get("Reward"))]
    if len(rewards) != count:
        _send(session, 7204, 1, [], [], {"eventCfgId": _int(data_cfg_id), "dataCfgId": _int(data_cfg_id)})
        return True
    cost = [_int(cfg.get("CostItem")), _int(cfg.get("CostNum")) * count]
    applied = _trade(session, uid, [cost], rewards)
    if applied is None:
        _send(session, 7204, 1, [], [], {"eventCfgId": _int(data_cfg_id), "dataCfgId": _int(data_cfg_id)})
        return True
    state = candidate
    state["drawCount"] = _int(state.get("drawCount")) + count
    for row in picked:
        state.setdefault("history", []).append({"rewardId": _int(row.get("Id")), "time": _now()})
    state["history"] = state.get("history", [])[-100:]
    _save(uid, "limited_turntable", state)
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    event_data = {
        "eventCfgId": _int(data_cfg_id),
        "dataCfgId": _int(data_cfg_id),
        "limitTurnTableDataPOD": {"getAwards": {int(k): int(v) for k, v in state.get("rewards", {}).items()}, "insureTimes": 0},
    }
    _send(session, 7204, 0, [_int(row.get("Id")) for row in picked], _item_show(rewards), event_data)
    return True


def handle_limited_turntable_history(session, uid, body):
    values = _decode_or_reject(7203, body)
    if values is None:
        return False
    (data_cfg_id,) = values
    if _row("limited_turntable", "LimitedTurntableGlobalTable", data_cfg_id) is None:
        _send(session, 7205, 1, [])
        return True
    state = _turntable_state(uid)
    history = [{"rewardId": _int(row.get("rewardId")), "time": _int(row.get("time"))} for row in state.get("history", [])]
    _send(session, 7205, 0, history)
    return True


def _tower_state(uid):
    return _module_state(uid, "single_weak_tower", {"floors": {}, "maxFloor": {}})


def handle_single_weak_tower(session, uid, body):
    values = _decode_or_reject(7802, body)
    if values is None:
        return False
    floor_id, formation_id = values
    row = _row("single_weak_tower", "SingleWeakTowerFloorTable", floor_id)
    if row is None or _int(formation_id) <= 0:
        _send_code(session, 7803, 1)
        return True
    state = _tower_state(uid)
    floor_type = _int(row.get("Type"))
    floor = _int(row.get("Floor"))
    if floor_type <= 0 or floor <= 0 or _int(row.get("BattleAreaId")) <= 0 or _int(row.get("MonsterTeam")) <= 0:
        _send_code(session, 7803, 1)
        return True
    max_floor = _int(state.setdefault("maxFloor", {}).get(str(floor_type)))
    if floor > max_floor + 1:
        _send_code(session, 7803, 1)
        return True
    first_clear = str(_int(floor_id)) not in state.setdefault("floors", {})
    rewards = _pairs(row.get("ClearReward")) if first_clear else []
    if not _start_module_battle(
        session, uid, "single_weak_tower", floor_id, _int(row.get("BattleAreaId")), _int(row.get("MonsterTeam")), rewards
    ):
        return False
    state["pendingFormation"] = _int(formation_id)
    state["pendingType"] = floor_type
    _save(uid, "single_weak_tower", state)
    _send_code(session, 7803, 0)
    return True


def _command_state(uid):
    return _module_state(uid, "command_challenge", {"passed": [], "opened": False})


def handle_command_challenge(session, uid, body):
    values = _decode_or_reject(7902, body)
    if values is None:
        return False
    (layer_id,) = values
    row = _row("command_challenge", "CommandChallengeLayerTable", layer_id)
    if row is None or _int(row.get("Sort")) <= 0 or _int(row.get("MonserTeamId")) <= 0:
        _send_code(session, 7903, 1)
        return True
    state = _command_state(uid)
    passed = set(_int(value) for value in state.get("passed", []))
    sort = _int(row.get("Sort"))
    prior = [item for item in _table("command_challenge", "CommandChallengeLayerTable").values() if _int(item.get("Sort")) < sort]
    if any(_int(item.get("Id")) not in passed for item in prior):
        _send_code(session, 7903, 1)
        return True
    first_clear = _int(layer_id) not in passed
    rewards = _pairs(row.get("Reward")) if first_clear else []
    if not _start_module_battle(session, uid, "command_challenge", layer_id, 0, _int(row.get("MonserTeamId")), rewards):
        return False
    state["opened"] = True
    _save(uid, "command_challenge", state)
    _send_code(session, 7903, 0)
    return True


def _flight_state(uid):
    return _module_state(uid, "flight_challenge", {
        "started": False,
        "record": 0,
        "score": 0,
        "mechas": {},
        "bossWins": 0,
        "distanceRewardClaimed": False,
    })


def _flight_player(row_id):
    return _row("flight_challenge", "FlightChallengePlayerTable", row_id)


def _flight_mecha_pod(player_id, data):
    cfg = _flight_player(player_id) or {}
    attrs = cfg.get("AttType") or []
    values = cfg.get("AttValue") or []
    levels = data.get("levels", {}) if isinstance(data, dict) else {}
    growth = {}
    for index, attr_id in enumerate(attrs):
        base = float(values[index]) if index < len(values) else 0.0
        element_id, element = 0, None
        for candidate in _table("flight_challenge", "FlightChallengeElementTable").values():
            if _int(candidate.get("AttPlayer")) == _int(player_id) and _int(candidate.get("AttType")) == _int(attr_id):
                element_id, element = _int(candidate.get("Id")), candidate
                break
        add = float(element.get("AttValue", 0) if element else 0)
        growth[_int(attr_id)] = base + add * _int(levels.get(str(element_id)))
    return {"id": _int(player_id), "growthAttribute": growth, "firingSpeed": 0.0}


def handle_flight_start(session, uid):
    state = _flight_state(uid)
    if state.get("started"):
        _send_code(session, 8006, 1)
        return True
    state["started"] = True
    _save(uid, "flight_challenge", state)
    _send_code(session, 8006, 0)
    return True


def handle_flight_level_up(session, uid, body):
    values = _decode_or_reject(8003, body)
    if values is None:
        return False
    (element_id,) = values
    element = _row("flight_challenge", "FlightChallengeElementTable", element_id)
    state = _flight_state(uid)
    if element is None or not state.get("started") or _int(element.get("ItemID")) <= 0 or _int(element.get("AttPlayer")) <= 0:
        _send(session, 8007, 1, {"id": _int(element.get("AttPlayer")) if element else 0})
        return True
    item_id = _int(element.get("ItemID"))
    player_id = _int(element.get("AttPlayer"))
    mecha = state.setdefault("mechas", {}).setdefault(str(player_id), {"levels": {}})
    if _int(mecha.setdefault("levels", {}).get(str(_int(element_id)))) >= 99:
        _send(session, 8007, 1, _flight_mecha_pod(player_id, mecha))
        return True
    if _trade(session, uid, [[item_id, 1]], []) is None:
        _send(session, 8007, 1, _flight_mecha_pod(player_id, mecha))
        return True
    key = str(_int(element_id))
    mecha.setdefault("levels", {})[key] = _int(mecha.setdefault("levels", {}).get(key)) + 1
    _save(uid, "flight_challenge", state)
    _send(session, 8007, 0, _flight_mecha_pod(player_id, mecha))
    return True


def handle_flight_end(session, uid, body):
    values = _decode_or_reject(8004, body)
    if values is None:
        return False
    distance, score = values
    state = _flight_state(uid)
    cfg = _row("flight_challenge", "FlightChallengeControlTable", 1) or {}
    distance, score = _int(distance), _int(score)
    if not state.get("started") or distance < 0 or score < 0 or distance > _int(cfg.get("MaxDistance"), 0) or score > _int(cfg.get("MaxScore"), 0):
        _send(session, 8008, 1, 0, [], 0)
        return True
    state["started"] = False
    state["record"] = max(_int(state.get("record")), distance)
    state["score"] = max(_int(state.get("score")), score)
    rewards = []
    threshold = cfg.get("DistanceReward") or []
    threshold_met = len(threshold) >= 2 and distance >= _int(threshold[0])
    if threshold_met and not bool(state.get("distanceRewardClaimed")):
        rewards.append((_int(threshold[1]), 1))
    if threshold_met:
        state["distanceRewardClaimed"] = True
    applied = storage.grant_reward_pairs(uid, rewards)
    if applied is None:
        return False
    _save(uid, "flight_challenge", state)
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    _send(session, 8008, 0, distance, _item_show(rewards), score)
    return True


def handle_flight_boss(session, uid, body):
    values = _decode_or_reject(8005, body)
    if values is None:
        return False
    (formation_id,) = values
    cfg = _row("flight_challenge", "FlightChallengeControlTable", 1) or {}
    state = _flight_state(uid)
    if _int(formation_id) <= 0 or not state.get("started") or not cfg or _int(cfg.get("BossMapID")) <= 0 or _int(cfg.get("BossTeam")) <= 0:
        _send_code(session, 8009, 1)
        return True
    boss_limit = _int(cfg.get("BossAddLimit"))
    if boss_limit > 0 and _int(state.get("bossWins")) >= boss_limit:
        _send_code(session, 8009, 1)
        return True
    if not _module_battle_slot_available(uid):
        _send_code(session, 8009, 1)
        return True
    cost = _pairs(cfg.get("Cost"))
    if cost and _trade(session, uid, cost, []) is None:
        _send_code(session, 8009, 1)
        return True
    if not _start_module_battle(session, uid, "flight_boss", 0, _int(cfg.get("BossMapID")), _int(cfg.get("BossTeam")), []):
        return False
    state["pendingFormation"] = _int(formation_id)
    _save(uid, "flight_challenge", state)
    _send_code(session, 8009, 0)
    return True


def _puzzle_state(uid):
    return _module_state(uid, "puzzle_adv", {
        "active": 0,
        "finished": [],
        "clues": [],
        "claimedClues": [],
        "dialog": 0,
    })


def handle_puzzle_start(session, uid, body):
    values = _decode_or_reject(9802, body)
    if values is None:
        return False
    (puzzle_id,) = values
    row = _row("puzzle_adv", "PuzzleAdvTable", puzzle_id)
    if row is None:
        _send(session, 9803, 1, _int(puzzle_id))
        return True
    data = _puzzle_state(uid)
    if _int(data.get("active")) not in (0, _int(puzzle_id)):
        _send(session, 9803, 1, _int(puzzle_id))
        return True
    data["active"] = _int(puzzle_id)
    data["clues"] = sorted(set(_int(value) for value in (row.get("DefaultClue") or []) + (row.get("BringInClues") or [])))
    _save(uid, "puzzle_adv", data)
    _send(session, 9803, 0, _int(puzzle_id))
    _send(session, 9805, _int(puzzle_id))
    if _int(row.get("BeginStory")):
        _send(session, 9808, _int(row.get("BeginStory")))
    return True


def handle_puzzle_select_dialog(session, uid, body):
    values = _decode_or_reject(9806, body)
    if values is None:
        return False
    select_index, skip = values
    if _int(select_index) < 0 or not isinstance(skip, list) or len(skip) > 64 or not all(isinstance(value, int) and value >= 0 for value in skip):
        return False
    data = _puzzle_state(uid)
    if _int(data.get("active")) <= 0:
        return False
    data["dialog"] = 0
    _save(uid, "puzzle_adv", data)
    _send(session, 9807, 0, -1)
    return True


def handle_puzzle_end(session, uid, body):
    values = _decode_or_reject(9809, body)
    if values is None:
        return False
    puzzle_id, win, clue_items = values
    row = _row("puzzle_adv", "PuzzleAdvTable", puzzle_id)
    data = _puzzle_state(uid)
    if row is None or _int(data.get("active")) != _int(puzzle_id) or not isinstance(clue_items, list):
        _send(session, 9810, 1, [], [])
        return True
    if len(clue_items) > 100 or any(_int(value) <= 0 for value in clue_items):
        return False
    allowed = set()
    for clue in _table("puzzle_adv", "PuzzleAdvClueTable").values():
        if _int(puzzle_id) in [_int(value) for value in clue.get("PluzzleAdvEvent", [])]:
            allowed.add(_int(clue.get("Id")))
    known = {_int(value) for value in data.get("clues", [])}
    if any(_int(value) not in allowed or _int(value) not in known for value in clue_items):
        _send(session, 9810, 1, [], [])
        return True
    clue_items = sorted(set(_int(value) for value in clue_items))
    finished = set(_int(value) for value in data.get("finished", []))
    rewards = _pairs(row.get("Reward")) if bool(win) and _int(puzzle_id) not in finished else []
    claimed_clues = set(_int(value) for value in data.get("claimedClues", []))
    clue_rows = {
        _int(clue.get("Id")): clue
        for clue in _table("puzzle_adv", "PuzzleAdvClueTable").values()
        if isinstance(clue, dict)
    }
    clue_pairs = []
    for clue_id in clue_items:
        if clue_id in claimed_clues:
            continue
        item_id = _int((clue_rows.get(clue_id) or {}).get("Item"))
        if item_id > 0:
            clue_pairs.append((item_id, 1))
        claimed_clues.add(clue_id)
    applied = storage.grant_reward_pairs(uid, rewards + clue_pairs)
    if applied is None:
        return False
    data["active"] = 0
    data["clues"] = sorted(set(_int(value) for value in data.get("clues", [])) | set(_int(value) for value in clue_items))
    data["claimedClues"] = sorted(claimed_clues)
    if bool(win):
        finished.add(_int(puzzle_id))
        data["finished"] = sorted(finished)
    _save(uid, "puzzle_adv", data)
    for cid, quantity in applied.get("changed_attrs", {}).items():
        _send(session, 3924, {int(cid): int(quantity)})
    if applied.get("changed_items"):
        _send(session, 4102, applied["changed_items"])
    _send(session, 9810, 0, _item_show(rewards), _item_show(clue_pairs))
    if bool(win):
        _send(session, 9804, _int(puzzle_id))
    return True


def handle_battle_completion(session, uid, battle_id, win, settlement):
    """Apply module progress after the common battle transaction succeeds."""
    data = _module_state(uid, "module_battles", {})
    active = data.get("active")
    if not isinstance(active, dict) or str(active.get("battleId")) != str(battle_id):
        return False
    module = str(active.get("module"))
    key = _int(active.get("key"))
    show = _item_show((settlement or {}).get("rewards", []))
    if module == "tale" and win:
        state = _tale_state(uid)
        passed = set(_int(value) for value in state.get("passedNode", []))
        passed.add(key)
        state["passedNode"] = sorted(passed)
        _save(uid, "tale_challenge", state)
        session.send(6409, protocol_codec.encode_method(6409, key, True, show))
    elif module == "single_weak_tower" and win:
        state = _tower_state(uid)
        row = _row("single_weak_tower", "SingleWeakTowerFloorTable", key) or {}
        floor_type = _int(row.get("Type"))
        floor = _int(row.get("Floor"))
        state.setdefault("floors", {})[str(key)] = 1
        state.setdefault("maxFloor", {})[str(floor_type)] = max(_int(state.setdefault("maxFloor", {}).get(str(floor_type))), floor)
        _save(uid, "single_weak_tower", state)
        session.send(7804, protocol_codec.encode_method(7804, True, key, floor_type, {}, show, 0))
    elif module == "command_challenge" and win:
        state = _command_state(uid)
        passed = set(_int(value) for value in state.get("passed", []))
        passed.add(key)
        state["passed"] = sorted(passed)
        state["opened"] = False
        _save(uid, "command_challenge", state)
        session.send(7904, protocol_codec.encode_method(7904, True, key, {}, show))
    elif module == "panda":
        state = _panda_state(uid)
        if win:
            completed = set(_int(value) for value in state.get("completedEvents", []))
            completed.add(key)
            state["completedEvents"] = sorted(completed)
            state["dialog"] = 0
            state.pop("pendingEvent", None)
            _save(uid, "panda", state)
        session.send(6013, protocol_codec.encode_method(6013, key, bool(win), show))
    elif module == "flight_boss":
        state = _flight_state(uid)
        if win:
            state["bossWins"] = _int(state.get("bossWins")) + 1
        _save(uid, "flight_challenge", state)
        session.send(8010, protocol_codec.encode_method(8010, bool(win), key, {}))
    elif module == "place_game":
        state = _place_state(uid)
        if win:
            row = _row("place", "PlaceGameTowerTable", key) or {}
            state["tower_floor"] = max(
                _int(state.get("tower_floor")), _int(row.get("Floor"))
            )
        state.pop("pending_tower", None)
        _save(uid, "place_game", state)
    elif module in ("dual_team", "dual_boss", "dual_boss_ex"):
        state = _dual_state(uid)
        state["lastFight"] = {"key": key, "win": bool(win), "time": _now()}
        if win:
            team = _int(state.get("team"), 1)
            if team in (1, 2):
                state.setdefault("team%d" % team, {})["dead"] = False
        _save(uid, "dual_team_explore", state)
    elif module in ("horizontal", "horizontal_boss"):
        state = _horizontal_state(uid)
        state["lastFight"] = {"key": key, "win": bool(win), "time": _now()}
        if win:
            finished = set(_int(value) for value in state.get("finished", []))
            finished.add(key)
            state["finished"] = sorted(finished)
        _save(uid, "horizontal_rpg", state)
    elif module in ("amusement", "amusement_boss"):
        state = _amusement_state(uid)
        state.setdefault("combat", {})[str(key)] = {
            "win": bool(win), "time": _now(), "count": _int(state.setdefault("combat", {}).get(str(key))) + 1,
        }
        _save(uid, "amusement_park", state)
    elif module in ("restaurant", "restaurant_boss"):
        state = storage.get_player_state_json(uid, "restaurant") or {}
        combat = state.setdefault("combatTraining", {})
        record = combat.setdefault(str(key), {"win": False, "count": 0})
        record["win"] = bool(win)
        record["count"] = _int(record.get("count")) + 1
        if module == "restaurant_boss" and win:
            state["exBossLock"] = False
        storage.update_player_state_json(uid, "restaurant", state)
    elif module == "magic_tower":
        # The tower sends its execution item before the common fight flow.
        # Resume the same cell only after the server-owned battle transaction
        # has settled, so a retry cannot duplicate an execution or reward.
        from module_handlers import magic_tower_battle_complete
        magic_tower_battle_complete(session, uid, bool(win))
    elif module == "mining":
        from module_handlers import mining_battle_complete
        mining_battle_complete(session, uid, _int(key), bool(win), show)
    elif module == "evil_erosion":
        state = storage.get_player_state_json(uid, "evil_erosion") or {}
        state["last_result"] = {"level": key, "win": bool(win), "time": _now()}
        storage.update_player_state_json(uid, "evil_erosion", state)
    data.pop("active", None)
    _save(uid, "module_battles", data)
    return True


OPERATION_DISPATCH = {
    5002: (handle_group_buy, True),
    6202: (handle_vote, True),
    6302: (handle_newbies_submit, True),
    6303: (handle_newbies_task, True),
    7002: (handle_turntable_draw, True),
    7302: (handle_cup_vote, True),
    100402: (handle_turntable_log, True),
    100403: (handle_turntable_receive, True),
    100502: (handle_group_buy_info, True),
    100602: (handle_vote_info, True),
    100702: (handle_newbies_followers, True),
    100802: (handle_cup_vote_info, True),
}


CARD_DISPATCH = {9702: (handle_card_fight, True), 9703: (handle_card_deck, True), 9704: (handle_card_equip, True), 9705: (handle_card_consume, True), 9706: (handle_card_story, True), 9707: (handle_card_boss, True)}
CHAT_DISPATCH = {100102: (handle_chat_send, True), 100103: (handle_chat_room, True), 100108: (handle_chat_report, True)}
RANK_DISPATCH = {100202: (handle_rank, True), 100204: (handle_rank_user, True), 100206: (handle_rank_goalie, True)}
GUILD_LEGACY_DISPATCH = {7402: (handle_guild_sign, False), 7403: (handle_guild_quest_rewards, True), 7404: (handle_guild_redpoint, False), 7502: (handle_guild_challenge_attack, True), 7503: (handle_guild_challenge_rewards, True), 7504: (handle_guild_challenge_mopup, True), 7505: (handle_guild_challenge_score, True), 9002: (handle_guild_training, True)}
