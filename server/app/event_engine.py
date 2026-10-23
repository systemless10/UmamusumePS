
from __future__ import annotations

import functools
import json
import logging
import os
import random
import re

from . import gametora
from . import master_data
from .handlers import conditions
from .handlers import single_mode_events

log = logging.getLogger("uma-server")

_GOOD_CONDITIONS = frozenset({7, 8, 9, 10, 11, 13})

SUPPORT_EVENT_ID = 10002
SUPPORT_RANDOM_EVENT_ID = 20000
CHARA_EVENT_ID = 6000
CAREER_EVENT_IDS = frozenset({SUPPORT_EVENT_ID, SUPPORT_RANDOM_EVENT_ID, CHARA_EVENT_ID})


@master_data.context_cache(maxsize=8192)
def wire_story_id(story_id) -> int:
    try:
        sid = int(story_id or 0)
    except (TypeError, ValueError):
        return story_id
    if sid <= 0:
        return story_id
    row = master_data.query_one(
        "SELECT short_story_id FROM single_mode_story_data WHERE story_id=?", (sid,))
    short = (row["short_story_id"] if row else 0) or 0
    return short or sid


_RANDOM_EVENT_IDS: dict | None = None


def support_random_event_id(story_id) -> int:
    global _RANDOM_EVENT_IDS
    if _RANDOM_EVENT_IDS is None:
        _RANDOM_EVENT_IDS = {
            int(r["story_id"]): SUPPORT_RANDOM_EVENT_ID + 1 + i
            for i, r in enumerate(master_data.query(
                "SELECT story_id FROM single_mode_story_data "
                "WHERE story_id BETWEEN 800000000 AND 808999999 "
                "AND story_id % 1000 = 3 ORDER BY id"))}
    try:
        return _RANDOM_EVENT_IDS.get(int(story_id), SUPPORT_RANDOM_EVENT_ID)
    except (TypeError, ValueError):
        return SUPPORT_RANDOM_EVENT_ID

CAREER_EVENT_CTX_KEY = "active_career_event"
FIRED_EVENTS_KEY = "fired_career_events"
CHAIN_ENDED_KEY = "ended_support_chains"

_STAT_TYPE_KEY = {"speed": "speed", "stamina": "stamina", "power": "power",
                  "guts": "guts", "wisdom": "wiz", "wiz": "wiz"}
_FAST_LEARNER_CONDITION = 7
_FAST_LEARNER_CHANCE = 0.10
_STAT_KEY_IDX = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events")

_SUPPORT_INDEX: dict[str, dict[str, dict]] | None = None
_CHARA_INDEX: dict[str, dict[str, dict]] | None = None


def _norm(s: str | None) -> str:
    s = re.sub(r"\\+[nrt]", " ", s or "")
    return re.sub(r"[^\w]", "", s.lower(), flags=re.UNICODE)


def _iter_events(card: dict):
    secs = card.get("sections")
    if isinstance(secs, dict):
        for evs in secs.values():
            for e in evs or ():
                yield e
    for key in ("chain_events", "events", "random_events", "special_events",
                "date_events", "costume_events"):
        for e in card.get(key) or ():
            yield e


def _load():
    global _SUPPORT_INDEX, _CHARA_INDEX
    if _SUPPORT_INDEX is not None:
        return
    _SUPPORT_INDEX, _CHARA_INDEX = {}, {}

    def add(index, cid, event):
        index.setdefault(str(cid), {})[_norm(event.get("name"))] = event

    for fname in ("jp_events.json", "events.json"):
        path = os.path.join(_DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            db = json.load(fh)
        for cid, card in db.items():
            for e in _iter_events(card):
                add(_SUPPORT_INDEX, cid, e)

    path = os.path.join(_DATA_DIR, "character_event_data.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            db = json.load(fh)
        for cid, card in db.items():
            for e in _iter_events(card):
                add(_CHARA_INDEX, cid, e)


def _support_card_of(story_id: int):
    if 800000000 <= story_id < 900000000:
        return (story_id - 800000000) // 1000
    return None


def _chara_of(story_id: int):
    if 500000000 <= story_id < 600000000:
        return (story_id - 500000000) // 1000
    return None


def resolve(story_id: int, title: str | None, trainee_card_id=None) -> dict | None:
    _load()
    key = _norm(title)
    if not key:
        return None

    card = _support_card_of(story_id)
    if card is not None:
        gt = gametora.load_cached("support", card)
        ja = _ported_aliases()[1].get(key)
        for k in (key, ja) if ja else (key,):
            if gt and k in gt:
                return gt[k]
            ev = (_SUPPORT_INDEX.get(str(card)) or {}).get(k)
            if ev:
                return ev
        return None

    chara = _chara_of(story_id)
    if chara is not None:
        if trainee_card_id:
            gt = gametora.load_cached("chara", trainee_card_id)
            if gt and key in gt:
                return gt[key]
            ev = (_CHARA_INDEX.get(str(trainee_card_id)) or {}).get(key)
            if ev:
                return ev
        for cid, evs in _CHARA_INDEX.items():
            if cid.startswith(str(chara)) and key in evs:
                return evs[key]
    return None


def title_key(text: str) -> str:
    return _norm(text)


def card_event_title_keys(trainee_card_id: int) -> frozenset:
    _load()
    keys = set(_CHARA_INDEX.get(str(trainee_card_id)) or ())
    keys |= set(gametora.load_cached("chara", trainee_card_id) or ())
    return frozenset(k for k in keys if k and k not in _RESERVED_EVENT_KEYS)


_RESERVED_EVENT_KEYS = frozenset({"__meta__", gametora._SPECIAL_KEY})


def card_events_for_trainee(trainee_card_id: int) -> dict:
    _load()
    merged = dict(_CHARA_INDEX.get(str(trainee_card_id)) or {})
    merged.update(gametora.load_cached("chara", trainee_card_id) or {})
    return {k: v for k, v in merged.items() if k not in _RESERVED_EVENT_KEYS}


def special_event_stats(trainee_card_id: int) -> dict:
    page = gametora.load_cached("chara", trainee_card_id) or {}
    val = page.get(gametora._SPECIAL_KEY)
    return val if isinstance(val, dict) else {}


SPECIAL_STAT_NAMES = {"sp": "speed", "st": "stamina", "po": "power",
                      "gu": "guts", "in": "wisdom", "pt": "skill_points"}


def sibling_card_ids(trainee_card_id: int) -> tuple:
    _load()
    chara = trainee_card_id // 100
    ids = set(_CHARA_INDEX) | set(_cached_chara_ids())
    return tuple(sorted(int(c) for c in ids
                        if str(c).isdigit() and int(c) // 100 == chara
                        and int(c) != trainee_card_id))


_cached_chara_ids_cache: tuple | None = None


def _cached_chara_ids() -> tuple:
    global _cached_chara_ids_cache
    if _cached_chara_ids_cache is None:
        try:
            names = os.listdir(gametora._DATA_DIR)
        except OSError:
            names = []
        _cached_chara_ids_cache = tuple(
            n[len("chara_"):-len(".json")] for n in names
            if n.startswith("chara_") and n.endswith(".json")
            and n[len("chara_"):-len(".json")].isdigit())
    return _cached_chara_ids_cache


def _parse_value(v, default: int = 0) -> int:
    if v is None:
        return default
    m = re.search(r"-?\d+", str(v))
    return int(m.group()) if m else default


_RANGE_RE = re.compile(r"^\s*([+-]?\d+)\s*/\s*([+-]?\d+)\s*$")


def _split_range_effects(effects):
    if not effects:
        return None
    if not any(_RANGE_RE.match(str(e.get("value") or "")) for e in effects):
        return None
    low, high = [], []
    for e in effects:
        m = _RANGE_RE.match(str(e.get("value") or ""))
        if m:
            low.append(dict(e, value=m.group(1)))
            high.append(dict(e, value=m.group(2)))
        else:
            low.append(e)
            high.append(e)
    return low, high


def _G(did, e0, e1=0, e2=0):
    return {"display_id": did, "effect_value_0": int(e0),
            "effect_value_1": int(e1), "effect_value_2": int(e2)}


DISPLAY_FANS = 3
DISPLAY_RACE_GRADE_STATS = 21
DISPLAY_RANDOM_SKILL_CHANCE = 8
DISPLAY_CHAIN_END = 12

_STAT_IDX_KEY = {idx: key for key, idx in _STAT_KEY_IDX.items()}

_MOOD_MAX = 5
_BOND_MAX = 100

_NPC_BOND_CHARAS = {target_id: chara_id
                    for pairs in single_mode_events.NPC_UNLOCKS.values()
                    for target_id, chara_id in pairs}


def capped_stat_indexes(chara_info: dict | None) -> frozenset:
    if not chara_info:
        return frozenset()
    capped = set()
    for key, idx in _STAT_KEY_IDX.items():
        cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}")
        if cap and (chara_info.get(key) or 0) >= cap:
            capped.add(idx)
    return frozenset(capped)


def capped_stat_codes(before: dict | None, stats) -> list:
    if not before:
        return []
    capped = capped_stat_indexes(before)
    return sorted({_STAT_KEY_IDX[s] for s in stats or ()
                   if s in _STAT_KEY_IDX and _STAT_KEY_IDX[s] in capped})


PENDING_NOT_UP_KEY = "pending_not_up"


def stash_not_up(full_state: dict | None, codes) -> None:
    if not isinstance(full_state, dict) or not codes:
        return
    merged = set(full_state.get(PENDING_NOT_UP_KEY) or ()) | set(codes)
    full_state[PENDING_NOT_UP_KEY] = sorted(merged)
    log.info("pay-out: already at ceiling -> not_up_parameter_info %s (stats %s)",
             sorted(codes), [_STAT_IDX_KEY.get(c) for c in sorted(codes)])


def not_up_snapshot(chara_info: dict | None) -> dict | None:
    if not chara_info:
        return None
    snap = dict(chara_info)
    snap["evaluation_info_array"] = [dict(e) if isinstance(e, dict) else e
                                     for e in chara_info.get("evaluation_info_array") or ()]
    return snap


def bond_with_chara(chara_info: dict, char_id) -> int | None:
    if not char_id:
        return None
    targets = {char_id} if char_id > 100 else set()
    pos = deck_position_for_chara(chara_info, char_id)
    if pos is not None:
        targets.add(pos)
    for target_id, npc_chara in _NPC_BOND_CHARAS.items():
        if npc_chara == char_id:
            targets.add(target_id)
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") in targets:
            return e.get("evaluation", 0)
    return None


def not_up_info(gains: list, before: dict | None) -> dict:
    if not before:
        return {}
    capped = capped_stat_indexes(before)
    energy_full = (before.get("vital") or 0) >= before.get("max_vital", 100)
    mood_max = (before.get("motivation") or 0) >= _MOOD_MAX
    status: set = set()
    charas: set = set()
    for g in gains or ():
        did, target = g.get("display_id"), g.get("effect_value_0")
        if did == 1:
            if target in capped:
                status.add(target)
            elif target == 10 and energy_full:
                status.add(6)
            elif target == 20 and mood_max:
                status.add(20)
        elif did == 13:
            status.update(capped)
        elif did == 4 and (bond_with_chara(before, target) or 0) >= _BOND_MAX:
            charas.add(target)
    info = {}
    if status:
        info["status_type_array"] = sorted(status)
    if charas:
        info["evaluation_chara_id_array"] = sorted(charas)
    if info:
        log.info("event reward: already at ceiling -> not_up_parameter_info %s "
                 "(stats %s)", info,
                 [_STAT_IDX_KEY.get(t) for t in sorted(status & set(_STAT_IDX_KEY))])
    return info


def source_default_chara(source, source_id):
    if source == "support":
        return card_chara_id(source_id)
    return None


def _encode_gains(effects, chara_info=None, default_char_id=None) -> list:
    out = []
    for e in effects or ():
        g = _encode_effect(e, chara_info, default_char_id)
        if g is None:
            continue
        out.extend(g if isinstance(g, list) else [g])
    return out


def _encode_effect(eff: dict, chara_info: dict | None = None, default_char_id=None):
    if eff.get("hidden"):
        return None
    if eff.get("display_id") is not None:
        return _G(eff["display_id"], eff.get("display_target", 0),
                  eff.get("display_value", 0))
    t = eff.get("type")
    if t in _STAT_TYPE_KEY:
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, _STAT_KEY_IDX[_STAT_TYPE_KEY[t]], abs(v))
    if t == "energy":
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, 10, abs(v))
    if t == "mood":
        v = _parse_value(eff.get("value"))
        guard = conditions.mood_guard_preview(chara_info) if v < 0 else None
        if guard:
            return _G(38, 20, abs(v), guard)
        return _G(1 if v >= 0 else 2, 20, abs(v))
    if t in ("skill_points", "skill_point"):
        v = _parse_value(eff.get("value"))
        return _G(1 if v >= 0 else 2, 30, abs(v))
    if t == "all_stats":
        v = _parse_value(eff.get("value"), 1)
        if v >= 0:
            return _G(13, v)
        return [_G(2, idx, -v) for idx in (1, 2, 3, 4, 5)]
    if t == "max_energy":
        return _G(1, 11, abs(_parse_value(eff.get("value"))))
    if t == "full_energy":
        return _G(1, 10, 100)
    if t == "bond":
        char_id = eff.get("char_id") or default_char_id or 0
        v = _parse_value(eff.get("value"), 5)
        if chara_info is not None and char_id and deck_position_for_chara(chara_info, char_id) is None:
            return _G(5, char_id, abs(v))
        return _G(4 if v >= 0 else 35, char_id, abs(v))
    if t == "performance":
        target = {1: 77, 2: 79, 3: 78, 4: 80, 5: 81}.get(int(eff.get("perf") or 0), 0)
        return _G(1, target, abs(_parse_value(eff.get("value"), 20)))
    if t == "npc_bond":
        v = _parse_value(eff.get("value"), 5)
        return _G(4 if v >= 0 else 35, eff.get("chara_id") or 0, abs(v))
    if t == "ken_ing":
        return _G(59, abs(_parse_value(eff.get("value"))))
    if t == "fans":
        return _G(DISPLAY_FANS, abs(_parse_value(eff.get("value"))))
    if t in ("skill_hint", "obtain_skill"):
        return _G(6, eff.get("skill_id") or 0, abs(_parse_value(eff.get("value"), 1)))
    if t == "skill_hint_or":
        return [_G(6, o.get("skill_id") or 0, abs(_parse_value(o.get("value"), 1)))
                for o in (eff.get("options") or []) if isinstance(o, dict)]
    if t == "chain_end":
        return _G(DISPLAY_CHAIN_END, 0, 0)
    if t == "condition":
        cid = eff.get("condition_id") or 0
        guard = conditions.bad_guard_preview(chara_info, cid) if cid else None
        if guard:
            return _G(40, cid, guard)
        return _G(9 if cid in _GOOD_CONDITIONS else 37, cid, 0)
    if t in ("highest_stat", "lowest_stat", "last_stat", "random_stats"):
        v = _parse_value(eff.get("value"))
        return _G(14 if v >= 0 else 34, int(eff.get("stat_count") or 1), abs(v))
    if t == "heal_status":
        return _G(16, 1)
    return None


def _guard_holds(guard: dict, chara_info: dict) -> bool | None:
    kind = guard.get("kind")
    if kind == "otherwise":
        return None
    if kind in ("mood_min", "mood_max"):
        mood = chara_info.get("motivation")
        if mood is None:
            return None
        want = guard.get("value")
        if not isinstance(want, int):
            return None
        return mood >= want if kind == "mood_min" else mood <= want
    if kind == "season":
        season = _season_of(chara_info)
        return None if season is None else season == guard.get("value")
    return None


_SEASONS = {1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall",
            11: "fall", 12: "winter"}


def _season_of(chara_info: dict):
    from .handlers import single_mode_team
    row = single_mode_team._turn_slot(chara_info.get("turn"))
    return _SEASONS.get(row["month"]) if row else None


def resolve_segments(choice: dict, chara_info: dict | None) -> dict:
    segments = choice.get("segments")
    if not segments:
        return choice
    chosen = segments[_segment_index(segments, chara_info)]
    out = {k: v for k, v in choice.items() if k not in ("segments", "effects",
                                                        "outcomes", "probs",
                                                        "random_either")}
    out.update({k: v for k, v in chosen.items() if k != "when"})
    return out


def chain_owner_token(source, source_id) -> str | None:
    if source not in ("support", "chara") or not source_id:
        return None
    return f"{source}:{int(source_id)}"


def end_chain(full_state: dict | None, owner) -> None:
    if full_state is None or not owner:
        return
    ended = full_state.setdefault(CHAIN_ENDED_KEY, [])
    if owner not in ended:
        ended.append(owner)


def chain_ended(full_state: dict | None, owner) -> bool:
    if full_state is None or not owner:
        return False
    return owner in (full_state.get(CHAIN_ENDED_KEY) or ())


def _pick_or_option(eff: dict) -> dict | None:
    opts = [o for o in (eff.get("options") or []) if isinstance(o, dict)]
    if not opts:
        return None
    return opts[_roll_branch(None, len(opts), lucky=False)]


def resolve_or_effects(choice: dict) -> dict:
    def resolve(effects):
        if not any(e.get("type") == "skill_hint_or" for e in effects or ()):
            return effects
        out = []
        for e in effects:
            if e.get("type") != "skill_hint_or":
                out.append(e)
                continue
            opt = _pick_or_option(e)
            if opt:
                out.append({"type": "skill_hint", "skill_id": opt.get("skill_id"),
                            "value": opt.get("value")})
        return out

    if not isinstance(choice, dict):
        return choice
    out = dict(choice)
    if isinstance(out.get("outcomes"), list):
        out["outcomes"] = [resolve(g) for g in out["outcomes"]]
    if out.get("effects"):
        out["effects"] = resolve(out["effects"])
    return out


def _choice_effect_groups(choice: dict):
    if choice.get("random_either") and isinstance(choice.get("outcomes"), list):
        return [g or [] for g in choice["outcomes"]]
    effects = choice.get("effects") or []
    pair = _split_range_effects(effects)
    if pair:
        return [pair[0], pair[1]]
    return [effects]


def choice_array(event: dict, allow_empty: bool = False, chara_info: dict | None = None) -> list:
    choices = event.get("choices") or []
    n = len(choices)
    if not (allow_empty and isinstance(event.get("choices"), list)):
        n = n or 1
    return [
        {"select_index": (_choice_branch_index(choices[i], chara_info)
                          if i < len(choices) else 1),
         "receive_item_id": 0,
         "target_race_id": (_choice_target_race(choices[i], chara_info)
                            if i < len(choices) else 0),
         "gain_select_id_index": i + 1, "select_icon": 0}
        for i in range(n)
    ]


def _choice_branch_index(choice: dict, chara_info: dict | None) -> int:
    resolved = resolve_segments(choice, chara_info)
    groups = _choice_effect_groups(resolved)
    branch = (_roll_branch(resolved.get("probs"), len(groups))
              if len(groups) > 1 else 0)
    return _branch_offset(choice, chara_info) + branch + 1


def _branch_offset(choice: dict, chara_info: dict | None) -> int:
    segments = choice.get("segments")
    if not segments:
        return 0
    i = _segment_index(segments, chara_info)
    return sum(len(_choice_effect_groups(seg)) for seg in segments[:i])


def _segment_index(segments: list, chara_info: dict | None) -> int:
    if chara_info is not None:
        for i, seg in enumerate(segments):
            guards = seg.get("when") or []
            if guards and all(_guard_holds(g, chara_info) for g in guards):
                return i
    for i, seg in enumerate(segments):
        if any(g.get("kind") == "otherwise" for g in seg.get("when") or []):
            return i
    for i, seg in enumerate(segments):
        if not seg.get("when"):
            return i
    return 0


def _choice_target_race(choice: dict, chara_info: dict | None = None) -> int:
    groups = list(choice.get("segments") or [])
    if groups and chara_info is not None:
        groups = [groups[_segment_index(groups, chara_info)]]
    for group in ([choice] + groups):
        lists = [group.get("effects") or []] + list(group.get("outcomes") or [])
        for effects in lists:
            for eff in effects:
                if eff.get("type") == "race_change" and eff.get("race_instance_id"):
                    if chara_info is None:
                        return 0
                    if eff.get("route_race_id"):
                        return int(eff["route_race_id"])
                    from .handlers import single_mode_team
                    return single_mode_team.route_race_id_for_instance(
                        chara_info, int(eff["race_instance_id"]))
    return 0


def career_event_entry(event: dict, event_id: int, story_id: int,
                       chara_id: int = 0, support_card_id: int = 0,
                       play_timing: int = 1, allow_empty_choices: bool = False,
                       chara_info: dict | None = None) -> dict:
    return {
        "event_id": event_id, "chara_id": chara_id,
        "story_id": wire_story_id(story_id),
        "play_timing": play_timing,
        "event_contents_info": {
            "support_card_id": support_card_id, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": choice_array(event, allow_empty_choices, chara_info),
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


UNLOCK_OUTING_DISPLAY_ID = 11


def choice_reward_array(event: dict, chara_info: dict | None = None,
                        default_char_id=None, unlock_partner=None,
                        unlock_chances=None, source_card_id=None) -> list:
    out = []
    chances = unlock_chances or {}
    for i, choice in enumerate(event.get("choices") or []):
        idx = i + 1
        chance = chances.get(idx, 0.0) if unlock_partner else 0.0
        marker = _G(UNLOCK_OUTING_DISPLAY_ID, unlock_partner) if chance > 0 else None
        entries = []
        for group in _choice_effect_groups(choice):
            gains = _encode_gains(boost_effects(group, source_card_id, chara_info),
                                  chara_info, default_char_id)
            entries.append({"select_index": idx, "gain_param_array": gains})
        if marker is not None:
            if chance >= 1.0:
                for e in entries:
                    e["gain_param_array"] = e["gain_param_array"] + [dict(marker)]
            elif len(entries) > 1:
                target = max(entries, key=lambda e: len(e["gain_param_array"]))
                target["gain_param_array"] = target["gain_param_array"] + [dict(marker)]
            else:
                entries.append({"select_index": idx,
                                "gain_param_array": list(entries[0]["gain_param_array"])
                                + [dict(marker)]})
        out.extend(entries)
    return out


def served_outcome(event: dict, choice_number, chara_info: dict | None,
                   branch) -> tuple:
    choices = event.get("choices") or []
    if not choices:
        return None, 0
    idx = max(1, int(choice_number or 1)) - 1
    if idx >= len(choices):
        idx = 0
    groups = _choice_effect_groups(resolve_segments(choices[idx], chara_info))
    if not branch:
        return None, len(groups)
    i = int(branch) - 1 - _branch_offset(choices[idx], chara_info)
    return (i if 0 <= i < len(groups) else None), len(groups)


def richest_outcome(event: dict, choice_number, chara_info: dict | None = None) -> int:
    choices = event.get("choices") or []
    if not choices:
        return 0
    idx = max(1, int(choice_number or 1)) - 1
    if idx >= len(choices):
        idx = 0
    groups = _choice_effect_groups(resolve_segments(choices[idx], chara_info))
    if not groups:
        return 0
    sizes = [len(_encode_gains(g, chara_info)) for g in groups]
    return max(range(len(groups)), key=lambda i: sizes[i])


SOFT_CAP = 1200


def add_stat(chara_info: dict, key: str, amount: int) -> int:
    cap = chara_info.get("max_wiz" if key == "wiz" else f"max_{key}", 9999)
    cur = chara_info.get(key, 0) or 0
    if amount <= 0:
        new = max(0, min(cap, cur + amount))
        chara_info[key] = new
        return new - cur
    below = max(0, min(amount, SOFT_CAP - cur))
    above = amount - below
    new = min(cap, cur + below + above // 2)
    chara_info[key] = new
    return new - cur


_EVENT_BOOST_CACHE: dict = {}


def event_boost(source_card_id, level: int | None = None) -> tuple:
    if not source_card_id:
        return (0, 0)
    from . import custom_support
    cache_key = (master_data.region.CURRENT_REGION.get(), custom_support.CURRENT_PACK.get(),
                 source_card_id, level)
    if cache_key not in _EVENT_BOOST_CACHE:
        from . import training_formula
        _EVENT_BOOST_CACHE[cache_key] = (
            int(training_formula.master_effect(
                source_card_id, training_formula.EFFECT_EVENT_RECOVERY, level)),
            int(training_formula.master_effect(
                source_card_id, training_formula.EFFECT_EVENT_EFFECT, level)),
        )
    return _EVENT_BOOST_CACHE[cache_key]


def deck_card_level(chara_info: dict | None, support_card_id) -> int | None:
    for s in (chara_info or {}).get("support_card_array") or ():
        if s.get("support_card_id") == support_card_id:
            from . import training_formula
            return training_formula.card_level(support_card_id, s.get("exp") or 0,
                                               s.get("limit_break_count"))
    return None


_BOOSTED_ENERGY = ("energy", "max_energy")
_BOOSTED_EFFECT = frozenset(("all_stats", "skill_points", "skill_point",
                             "highest_stat", "lowest_stat", "last_stat",
                             "random_stats"))


def boost_effects(effects: list, source_card_id, chara_info: dict | None = None) -> list:
    energy_pct, effect_pct = event_boost(source_card_id,
                                         deck_card_level(chara_info, source_card_id))
    if not energy_pct and not effect_pct:
        return effects
    out = []
    for eff in effects or ():
        t = eff.get("type")
        pct = (energy_pct if t in _BOOSTED_ENERGY else
               effect_pct if (t in _STAT_TYPE_KEY or t in _BOOSTED_EFFECT) else 0)
        v = _parse_value(eff.get("value"), 0)
        if not pct or v <= 0:
            out.append(eff)
            continue
        out.append(dict(eff, value=f"+{v * (100 + pct) // 100}"))
    return out


def _apply_effects(chara_info: dict, effects: list, default_char_id=None,
                   full_state: dict | None = None, source_card_id=None,
                   chain_owner=None) -> list:
    extra_gains: list = []
    for eff in effects or ():
        t = eff.get("type")
        if t in _STAT_TYPE_KEY:
            add_stat(chara_info, _STAT_TYPE_KEY[t], _parse_value(eff.get("value")))
        elif t == "all_stats":
            v = _parse_value(eff.get("value"))
            for key in ("speed", "stamina", "power", "guts", "wiz"):
                add_stat(chara_info, key, v)
        elif t == "energy":
            mv = max(chara_info.get("max_vital", 100), chara_info.get("vital", 0))
            chara_info["vital"] = max(0, min(mv, chara_info.get("vital", 0) + _parse_value(eff.get("value"))))
        elif t == "max_energy":
            chara_info["max_vital"] = chara_info.get("max_vital", 100) + _parse_value(eff.get("value"))
        elif t == "full_energy":
            chara_info["vital"] = chara_info.get("max_vital", 100)
        elif t == "mood":
            v = _parse_value(eff.get("value"))
            if full_state is not None:
                from . import scenarios
                v = scenarios.for_chara(chara_info).mood_change(full_state, chara_info, v)
            if v < 0:
                conditions.drop_mood(chara_info, -v)
            else:
                chara_info["motivation"] = max(1, min(5, chara_info.get("motivation", 3) + v))
        elif t == "condition_end":
            conditions.remove(chara_info, int(eff.get("condition_id") or 0))
        elif t in ("skill_points", "skill_point"):
            chara_info["skill_point"] = max(0, chara_info.get("skill_point", 0) + _parse_value(eff.get("value")))
        elif t == "bond":
            _apply_bond(chara_info, eff.get("char_id") or default_char_id, _parse_value(eff.get("value"), 5), source_card_id)
        elif t == "npc_bond":
            _apply_npc_bond(chara_info, eff.get("target_id"), _parse_value(eff.get("value"), 5))
        elif t == "race_change":
            from .handlers import single_mode_team
            if single_mode_team.switch_route_race(chara_info, eff.get("race_instance_id"),
                                                  eff.get("route_race_id")):
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "race_cancel":
            from .handlers import single_mode_team
            if single_mode_team.cancel_route_race(chara_info, eff.get("race_instance_id")):
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "race_restrict":
            from .handlers import single_mode_team
            if single_mode_team.restrict_racing(chara_info, eff.get("turns")):
                single_mode_team.refresh_route_screen(full_state, chara_info)
        elif t == "branch_flag":
            flags = chara_info.setdefault("branch_flags", [])
            if eff.get("flag") and eff["flag"] not in flags:
                flags.append(eff["flag"])
        elif t == "unique_skill_level":
            from .handlers import single_mode_team
            single_mode_team._level_unique_skill(
                chara_info, _parse_value(eff.get("value"), 1))
        elif t == "fans":
            chara_info["fans"] = max(0, chara_info.get("fans", 0)
                                     + _parse_value(eff.get("value")))
        elif t == "negative_race_skill":
            from . import negative_race_skills
            negative_race_skills.apply(chara_info, eff.get("context") or {})
        elif t in ("skill_hint", "obtain_skill"):
            chance = eff.get("chance")
            if chance is not None and random.random() >= _lucky(float(chance)):
                continue
            _apply_skill_hint(chara_info, eff.get("skill_id"), _parse_value(eff.get("value"), 1))
        elif t == "skill_hint_or":
            opt = _pick_or_option(eff)
            if opt:
                _apply_skill_hint(chara_info, opt.get("skill_id"),
                                  _parse_value(opt.get("value"), 1))
        elif t == "chain_end":
            end_chain(full_state, chain_owner)
        elif t == "condition":
            cond = eff.get("condition_id")
            if cond == _FAST_LEARNER_CONDITION and random.random() >= _FAST_LEARNER_CHANCE:
                continue
            chance = eff.get("chance")
            if cond == _COND_PRACTICE_PERFECT and chance is not None \
                    and random.random() >= _lucky(float(chance)):
                continue
            if cond:
                single_mode_events.add_condition(chara_info, cond)
        elif t == "heal_status":
            chance = eff.get("chance")
            if chance is not None and random.random() >= _lucky(float(chance)):
                continue
            conditions.cure_one_negative(chara_info)
        elif t in ("highest_stat", "lowest_stat", "last_stat", "random_stats"):
            _apply_dynamic_stat(chara_info, t, _parse_value(eff.get("value")),
                                 int(eff.get("stat_count") or 1),
                                 (full_state or {}).get(LAST_TRAINED_KEY))
        elif t == "performance":
            if full_state is not None:
                from .scenarios.grand_live import impl as _gl
                _gl.grant_performance(full_state, chara_info, int(eff.get("perf") or 0),
                                      _parse_value(eff.get("value"), 20))
        elif t in ("mt", "performance_tokens"):
            if full_state is not None:
                from . import scenarios
                amount = _parse_value(eff.get("value"), 20)
                gain = scenarios.for_chara(chara_info).apply_currency_gain(
                    full_state, chara_info, amount)
                if gain is not None:
                    wire_target, gained = gain
                    extra_gains.append(_G(1, wire_target, gained))
    return extra_gains


def deck_position_for_chara(chara_info: dict, char_id) -> int | None:
    if not char_id:
        return None
    for c in chara_info.get("support_card_array") or ():
        if card_chara_id(c.get("support_card_id")) == char_id:
            return c.get("position")
    return None


def _apply_bond(chara_info: dict, char_id, amount: int, source_card_id=None) -> None:
    pos = deck_position_for_chara(chara_info, char_id)
    from . import custom_support
    if source_card_id and custom_support.card(source_card_id) and card_chara_id(source_card_id) == char_id:
        pos = next((c.get("position") for c in chara_info.get("support_card_array", [])
                    if c.get("support_card_id") == source_card_id), pos)
    if pos is None:
        return
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") == pos:
            e["evaluation"] = max(0, min(100, e.get("evaluation", 0) + amount))
            return


def _apply_npc_bond(chara_info: dict, target_id, amount: int) -> None:
    if not target_id:
        return
    for e in chara_info.get("evaluation_info_array") or ():
        if isinstance(e, dict) and e.get("target_id") == target_id:
            e["evaluation"] = max(0, min(100, e.get("evaluation", 0) + amount))
            return


_STAT_KEYS_ALL = ("speed", "stamina", "power", "guts", "wiz")
LAST_TRAINED_KEY = "_last_trained_stat"


def _apply_dynamic_stat(chara_info: dict, kind: str, value: int, count: int,
                        last_trained: str | None = None) -> None:
    def cur(k):
        return chara_info.get(k, 0)

    def bump(k):
        add_stat(chara_info, k, value)

    keys = list(_STAT_KEYS_ALL)
    if kind == "random_stats":
        targets = random.sample(keys, min(max(1, count), len(keys)))
    elif kind == "lowest_stat":
        targets = [min(keys, key=cur)]
    elif kind == "last_stat" and last_trained in keys:
        targets = [last_trained]
    else:
        targets = [max(keys, key=cur)]
    for k in targets:
        bump(k)


HINT_STAT_TARGETS = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz",
                     30: "skill_point"}


@master_data.context_cache(maxsize=None)
def card_hint_groups(support_card_id) -> tuple:
    if not support_card_id:
        return ()
    rows = master_data.query(
        "SELECT hint_group, hint_gain_type, hint_value_1, hint_value_2 "
        "FROM single_mode_hint_gain WHERE support_card_id=? "
        "ORDER BY hint_group, id", (int(support_card_id),))
    groups, order = {}, []
    for row in rows:
        key = int(row["hint_group"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    out = []
    for key in order:
        rows = groups[key]
        if int(rows[0]["hint_gain_type"] or 0) == 0:
            out.append(("skill", int(rows[0]["hint_value_1"])))
            continue
        effects = []
        for row in rows:
            field = HINT_STAT_TARGETS.get(int(row["hint_value_1"] or 0))
            if field:
                effects.append({"type": field,
                                "value": "+%d" % int(row["hint_value_2"] or 0)})
        if effects:
            out.append(("stat", tuple(effects)))
    return tuple(out)


def card_hint_skills(support_card_id) -> tuple:
    return tuple(v for kind, v in card_hint_groups(support_card_id)
                 if kind == "skill")


MAX_SKILL_HINT_LEVEL = 5


def skill_hint_level(chara_info: dict, skill_id) -> int:
    group_id, rarity = master_data.skill_tip_key(skill_id)
    for t in (chara_info or {}).get("skill_tips_array") or ():
        if t.get("group_id") == group_id and t.get("rarity") == rarity:
            return int(t.get("level") or 0)
    return 0


def card_hint_open(chara_info: dict, skill_id, learned=()) -> bool:
    return (skill_id not in learned
            and skill_hint_level(chara_info, skill_id) < MAX_SKILL_HINT_LEVEL)


def _apply_skill_hint(chara_info: dict, skill_id, level: int) -> None:
    if not skill_id:
        return
    group_id, rarity = master_data.skill_tip_key(skill_id)
    tips = chara_info.setdefault("skill_tips_array", [])
    for t in tips:
        if t.get("group_id") == group_id and t.get("rarity") == rarity:
            t["level"] = min(5, t.get("level", 1) + level)
            return
    tips.append({"group_id": group_id, "rarity": rarity, "level": min(5, max(1, level))})


def apply_choice(chara_info: dict, event: dict, choice_number: int,
                 default_char_id=None, source_card_id=None,
                 full_state: dict | None = None, chain_owner=None,
                 branch=None) -> dict:
    choices = event.get("choices") or []
    idx = max(1, choice_number) - 1
    if idx >= len(choices):
        idx = 0
    if not choices:
        return {"choice_reward_array": []}
    choice = resolve_or_effects(resolve_segments(choices[idx], chara_info))
    groups = _choice_effect_groups(choice)
    if len(groups) > 1:
        picked = None
        if branch:
            picked = int(branch) - 1 - _branch_offset(choices[idx], chara_info)
            if not 0 <= picked < len(groups):
                log.warning("event choice %s: served branch %s out of range for "
                            "%s outcomes; re-rolling", choice_number, branch, len(groups))
                picked = None
        if picked is None:
            picked = _roll_branch(choice.get("probs"), len(groups))
        effects = groups[picked]
    else:
        effects = groups[0]
    effects = boost_effects(effects, source_card_id, chara_info)
    before = not_up_snapshot(chara_info)
    extra_gains = _apply_effects(chara_info, effects, default_char_id, full_state,
                                 source_card_id, chain_owner)
    gains = _encode_gains(effects, chara_info, default_char_id) + extra_gains
    return {"choice_reward_array": [{"select_index": idx + 1, "gain_param_array": gains}],
            "not_up": not_up_info(gains, before)}


_SEASONAL_KINDS = ("new_year", "new_year_2", "summer_camp", "dance_lesson",
                   "fan_letter", "extra_training", "acupuncture")

SHARED_EVENT_KINDS = {506: "dance_lesson", 507: "fan_letter",
                      715: "extra_training", 720: "acupuncture"}

_SKILL_CORNER_RECOVERY = 200352
_SKILL_STRAIGHT_RECOVERY = 200382


def shared_event(story_suffix: int, card_id, context: dict | None = None) -> dict | None:
    kind = SHARED_EVENT_KINDS.get(story_suffix)
    return seasonal_event(kind, card_id, context) if kind else None


def seasonal_event(kind: str, card_id, context: dict | None = None) -> dict | None:
    if kind not in _SEASONAL_KINDS:
        return None
    if kind == "summer_camp":
        return {"choices": [
            {"effects": [{"type": "power", "value": "+10"}]},
            {"effects": [{"type": "guts", "value": "+10"}]},
        ]}
    if kind == "dance_lesson":
        codes = (special_event_stats(card_id) or {}).get("dance") or ()
        stats = [SPECIAL_STAT_NAMES.get(c) for c in codes]
        stats = [s for s in stats if s]
        if len(stats) < 2:
            return None
        return {"choices": [{"effects": [{"type": s, "value": "+10"}]}
                            for s in stats[:2]]}
    if kind == "fan_letter":
        return {"choices": [{"effects": [{"type": "mood", "value": "+1"},
                                         {"type": "skill_points", "value": "+30"}]}]}
    if kind == "extra_training":
        stat = (context or {}).get("stat")
        if not stat:
            return None
        base_effects = [{"type": stat, "value": "+5",
                         "display_id": 19, "display_target": 5},
                        {"type": "energy", "value": "-5"}]
        if (context or {}).get("scenario_id") == 1:
            base_effects = [{"type": "npc_bond", "target_id": 103, "chara_id": 9003,
                             "value": "+5"}] + base_effects
        no_cure = list(base_effects)
        with_cure = base_effects + [{"type": "heal_status"}]
        return {"choices": [
            {"outcomes": [no_cure, with_cure],
             "probs": [round(100 * (1 - _EXTRA_TRAINING_CURE_CHANCE)),
                       round(100 * _EXTRA_TRAINING_CURE_CHANCE)],
             "random_either": True},
            {"effects": [{"type": "energy", "value": "+5"}]},
        ]}
    if kind == "acupuncture":
        return _acupuncture_event()
    if kind == "new_year_2":
        return {"choices": [
            {"effects": [{"type": "energy", "value": "+30"}]},
            {"effects": [{"type": "all_stats", "value": "+8"}]},
            {"effects": [{"type": "skill_points", "value": "+35"}]},
        ]}
    _load()
    stat = SPECIAL_STAT_NAMES.get((special_event_stats(card_id) or {}).get("nyear"))
    if not stat:
        meta = ((_CHARA_INDEX.get(str(card_id)) or {}).get("__meta__")
                or _chara_meta(card_id) or {})
        stat = meta.get("new_years_resolution_stat") or "speed"
    return {"choices": [
        {"effects": [{"type": stat, "value": "+25"}]},
        {"effects": [{"type": "energy", "value": "+20"}]},
        {"effects": [{"type": "skill_points", "value": "+20"}]},
    ]}


_EXTRA_TRAINING_CURE_CHANCE = 0.20


def _acupuncture_event() -> dict:
    def gamble(good, good_pct, bad):
        return {"outcomes": [good, bad], "probs": [good_pct, 100 - good_pct],
                "random_either": True}

    return {"choices": [
        gamble([{"type": "all_stats", "value": "+20"}], 30,
               [{"type": "mood", "value": "-2"},
                {"type": "all_stats", "value": "-15"},
                {"type": "condition", "condition_id": _COND_NIGHT_OWL}]),
        gamble([{"type": "obtain_skill", "skill_id": _SKILL_CORNER_RECOVERY, "value": 1},
                {"type": "obtain_skill", "skill_id": _SKILL_STRAIGHT_RECOVERY, "value": 1}], 45,
               [{"type": "energy", "value": "-20"}, {"type": "mood", "value": "-2"}]),
        gamble([{"type": "max_energy", "value": "+12"},
                {"type": "energy", "value": "+40"},
                {"type": "heal_status"}], 70,
               [{"type": "energy", "value": "-20"}, {"type": "mood", "value": "-2"},
                {"type": "condition", "condition_id": _COND_PRACTICE_POOR}]),
        gamble([{"type": "energy", "value": "+20"}, {"type": "mood", "value": "+1"},
                {"type": "condition", "condition_id": _COND_CHARMING}], 85,
               [{"type": "energy", "value": "-10"}, {"type": "mood", "value": "-1"},
                {"type": "condition", "condition_id": _COND_PRACTICE_POOR}]),
        {"effects": [{"type": "energy", "value": "+10"}]},
    ]}


_COND_NIGHT_OWL = 1
_COND_PRACTICE_POOR = 6
_COND_CHARMING = 8
_COND_PRACTICE_PERFECT = 10


def _chara_meta(card_id) -> dict | None:
    _load()
    path = os.path.join(_DATA_DIR, "character_event_data.json")
    global _CHARA_META
    if _CHARA_META is None:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            _CHARA_META = {k: (v or {}).get("special_event_meta") or {}
                           for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            _CHARA_META = {}
    meta = _CHARA_META.get(str(card_id))
    if meta:
        return meta
    prefix = str((int(card_id) // 100))
    for cid, m in _CHARA_META.items():
        if cid.startswith(prefix) and m:
            return m
    return None


_CHARA_META: dict | None = None


def resolve_source(source: str, source_id, title: str | None) -> dict | None:
    _load()
    if source == "seasonal":
        return seasonal_event(title, source_id)
    key = _norm(title)
    if not key:
        return None
    if source == "support":
        gt = gametora.load_cached("support", source_id)
        if gt and key in gt:
            return gt[key]
        return (_SUPPORT_INDEX.get(str(source_id)) or {}).get(key)
    gt = gametora.load_cached("chara", source_id)
    if gt and key in gt:
        return gt[key]
    ev = (_CHARA_INDEX.get(str(source_id)) or {}).get(key)
    if ev:
        return ev
    for cid, evs in _CHARA_INDEX.items():
        if key in evs:
            return evs[key]
    return None


_card_chara_cache: dict = {}


def card_chara_id(card_id: int) -> int | None:
    from . import custom_support, region
    key = (region.CURRENT_REGION.get(), custom_support.CURRENT_PACK.get(), card_id)
    if key not in _card_chara_cache:
        rows = master_data.query("SELECT chara_id FROM support_card_data WHERE id=?", (card_id,))
        chara = next((r["chara_id"] for r in rows), None)
        if chara is None and card_id not in custom_support.known_ids():
            chara = _chara_id_from_events(card_id)
        _card_chara_cache[key] = chara
    return _card_chara_cache[key]


def _chara_id_from_events(card_id: int) -> int | None:
    counts: dict = {}
    for ev in (_card_events(card_id) or {}).values():
        for choice in ev.get("choices") or []:
            for eff in choice.get("effects") or []:
                cid = eff.get("char_id")
                if cid:
                    counts[cid] = counts.get(cid, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _title_key(text: str) -> str:
    for a, b in (("\\n", " "), ("\\r", " "), ("\r", " "), ("\n", " "),
                 ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), ("♪", ""), ("☆", ""), ("★", ""),
                 ("！", "!"), ("？", "?"), ("：", ":"), ("；", ";"),
                 ("（", "("), ("）", ")"), ("～", "~"), ("〜", "~"),
                 ("　", " "), ("・", " ")):
        text = text.replace(a, b)
    return " ".join(text.split()).casefold()


@master_data.context_cache(maxsize=4096)
def _titles_in_window(base: int, span: int) -> tuple:
    return tuple((r["idx"], _title_key(r["text"] or "")) for r in master_data.query(
        "SELECT [index] AS idx, text FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<?", (base, base + span)))


_TITLE_ALIASES = {
    "ちゃっかりリサーチ♪": "Some Crafty Research ♪",
    "イベント企画のライトハロー": "Light Hello, the Event Planner",
    "お疲れ様です……！": "Another Day's Hard Work!",
    "想いの力、受け止めて……！": "Embrace Those Emotions!",
    "嵐の大洋で、まどろみ": "Repose in the Lunar Mare",
    "ティコの輝き": "Tycho's Radiance",
    "レゴリスで隠して": "Hidden Beneath the Regolith",
    "ホイヘンス山、越えし君": "Scaling Mons Huygens",
    "有明を共に歩む": "Dawn Under the Waning Moon",
    "満ちる時まで": "Until It Shines in Full",
    "ハローとの絆・きょうはぶれいこー": "Bonding with Light Hello: Letting Loose",
}
_ALIAS_KEYS: dict | None = None


def _alias_key(key: str) -> str | None:
    global _ALIAS_KEYS
    if _ALIAS_KEYS is None:
        _ALIAS_KEYS = {_title_key(jp): _title_key(en)
                       for jp, en in _TITLE_ALIASES.items()}
    return _ALIAS_KEYS.get(key) or _ported_aliases()[0].get(key)


_PORTED_ALIASES: dict = {}


def _ported_aliases() -> tuple:
    from . import custom_support
    bundles = custom_support.unreleased_bundles()
    hit = _PORTED_ALIASES.get(id(bundles))
    if hit is None:
        pairs = [(ja, en) for b in bundles for ja, en in (b.get("title_aliases") or {}).items()]
        hit = ({_title_key(ja): _title_key(en) for ja, en in pairs},
               {_norm(en): _norm(ja) for ja, en in pairs})
        _PORTED_ALIASES.clear()
        _PORTED_ALIASES[id(bundles)] = hit
    return hit


def _untranslated_ids(base: int, span: int = 1000) -> tuple:
    return tuple(r["idx"] for r in master_data.query(
        "SELECT [index] AS idx FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<? AND TRIM(COALESCE(text,''))='' "
        "ORDER BY [index]", (base, base + span)))


def _pair_untranslated(card_id: int, chara: int | None, leftovers: list) -> dict:
    leftovers = [ev for ev in leftovers if ev.get("id") is not None]
    if not leftovers:
        return {}
    chain_ids = _untranslated_ids(800000000 + card_id * 1000)
    rand_ids = _untranslated_ids(800000000 + chara * 1000) if chara else ()
    if len(leftovers) != len(chain_ids) + len(rand_ids) or not (chain_ids or rand_ids):
        return {}
    ordered = sorted(leftovers, key=lambda e: e["id"])
    paired = {}
    for ev, sid in zip(ordered[len(ordered) - len(chain_ids):], chain_ids):
        paired[id(ev)] = sid
    for ev, sid in zip(ordered[:len(rand_ids)], rand_ids):
        paired[id(ev)] = sid
    return paired


@master_data.context_cache(maxsize=8192)
def _story_row_exists(story_id: int) -> bool:
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)))


def _story_id_by_title(title: str, base: int, span: int = 1000) -> int | None:
    key = _title_key(title or "")
    if not key:
        return None
    hit = next((idx for idx, t in _titles_in_window(base, span) if t == key), None)
    if hit is None:
        alias = _alias_key(key)
        if alias:
            hit = next((idx for idx, t in _titles_in_window(base, span) if t == alias), None)
    return hit


_support_events_cache: dict = {}


def _card_events(card_id: int) -> dict:
    _load()
    merged = dict(_SUPPORT_INDEX.get(str(card_id)) or {})
    for k, v in (gametora.load_cached("support", card_id) or {}).items():
        merged[k or f"__cache_{len(merged)}"] = v
    return merged


def support_events(card_id: int):
    from . import custom_support
    donor = custom_support.event_donor(card_id)
    if donor != card_id:
        return support_events(donor)
    cache_key = (custom_support.CURRENT_PACK.get(), card_id)
    if cache_key in _support_events_cache:
        return _support_events_cache[cache_key]
    from .handlers import pal_cards
    gt = _card_events(card_id)
    chara = card_chara_id(card_id)
    bands = [chara] if chara else []
    bands += [m for m in pal_cards.group_members(card_id) if m not in bands]
    seen_titles, uniq = set(), []
    for ev in gt.values():
        key = _title_key(ev.get("name") or "")
        if not key or key in seen_titles:
            continue
        seen_titles.add(key)
        uniq.append(ev)

    chain, rand, unplaced = [], [], []
    for ev in uniq:
        title = ev["name"]
        sid = _story_id_by_title(title, 800000000 + card_id * 1000)
        if sid:
            if not pal_cards.is_reserved(card_id, sid):
                chain.append((sid, title, ev))
            continue
        sid = next((x for x in (_story_id_by_title(title, 800000000 + b * 1000) for b in bands) if x), None)
        if sid:
            if not pal_cards.is_reserved(card_id, sid):
                rand.append((sid, title, ev))
            continue
        unplaced.append(ev)

    placed = _pair_untranslated(card_id, chara, unplaced)
    own_band = (800000000 + card_id * 1000) // 1000
    for ev in unplaced:
        sid = placed.get(id(ev))
        if not sid or pal_cards.is_reserved(card_id, sid):
            continue
        (chain if sid // 1000 == own_band else rand).append((sid, ev["name"], ev))

    chain = [t for t in chain if _story_row_exists(t[0])]
    rand = [t for t in rand if _story_row_exists(t[0])]
    chain.sort(key=lambda t: t[0])
    rand.sort(key=lambda t: t[0])
    _support_events_cache[cache_key] = (chain, rand)
    return chain, rand


def event_by_story_id(card_id: int, sid: int):
    from .handlers import pal_cards
    gt = _card_events(card_id)
    for ev in gt.values():
        title = ev.get("name")
        if not title:
            continue
        for base in (800000000 + card_id * 1000, 800000000 + (card_chara_id(card_id) or 0) * 1000,
                     *(800000000 + m * 1000 for m in pal_cards.group_members(card_id))):
            if _story_id_by_title(title, base) == sid:
                return title, ev
    sid_key = next((t for idx, t in _titles_in_window(sid, 1) if idx == sid), None)
    if sid_key:
        for ev in gt.values():
            if ev.get("name") and _title_key(ev["name"]) == sid_key:
                return ev["name"], ev
    return None, None


_all_support_ids_cache: list | None = None


def all_support_card_ids() -> list:
    global _all_support_ids_cache
    if _all_support_ids_cache is None:
        ids = []
        d = os.path.join(gametora._DATA_DIR)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.startswith("support_") and fn.endswith(".json"):
                    try:
                        ids.append(int(fn[len("support_"):-len(".json")]))
                    except ValueError:
                        pass
        with_row = {r["id"] for r in master_data.query(
            "SELECT id FROM support_card_data WHERE id IN ({})".format(
                ",".join("?" * len(ids))), tuple(ids))} if ids else set()
        _all_support_ids_cache = [cid for cid in ids if cid in with_row]
    return _all_support_ids_cache


def event_title(story_id: int) -> str | None:
    rows = master_data.query(
        "SELECT text FROM text_data WHERE category=181 AND [index]=?", (story_id,))
    for r in rows:
        return r["text"]
    return None


def support_event_chain(card_id: int, max_events: int = 25) -> list:
    chain = []
    for idx in range(1, max_events + 1):
        sid = 800000000 + card_id * 1000 + idx
        title = event_title(sid)
        if not title:
            continue
        if resolve(sid, title):
            chain.append((sid, title))
    return chain


_OUTING_SUFFIXES = range(702, 708)


def chara_outing_chain(trainee_card_id: int, chara_id: int, max_events: int = 40) -> list:
    chain = []
    for idx in list(_OUTING_SUFFIXES):
        sid = 500000000 + chara_id * 1000 + idx
        title = event_title(sid)
        if not title:
            continue
        if resolve(sid, title, trainee_card_id):
            chain.append((sid, title))
    return chain


def _lucky(p: float) -> float:
    from . import tuning
    return tuning.good(p)


def _roll_branch(probs, n: int, lucky: bool = True) -> int:
    weights = list(probs) if probs and len(probs) == n else [None] * n
    known = sum(w for w in weights if isinstance(w, (int, float)))
    unknown = [i for i, w in enumerate(weights) if not isinstance(w, (int, float))]
    if unknown:
        share = max(0.0, 1.0 - known) / len(unknown)
        for i in unknown:
            weights[i] = share
    if lucky:
        from . import tuning
        weights = tuning.branch_weights(weights)
    total = sum(weights) or 1.0
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return n - 1


def choice_count(event: dict) -> int:
    return len(event.get("choices") or []) or 1
