
from __future__ import annotations

import functools

from .. import master_data

PAL_TYPE = 2
GROUP_TYPE = 3

PAL_FIRST, PAL_RANDOM, PAL_UNLOCK = 2, 3, 4
PAL_CHAIN = range(5, 9)
PAL_FINALE = 11
PAL_ENDING_COMPLETE, PAL_ENDING_INCOMPLETE = 9, 10
GROUP_FIRST, GROUP_UNLOCK, GROUP_RANDOM = 1, 2, 3
GROUP_CHAIN_BASE = 5
GROUP_FINALE = range(20, 40)

LINK_FIRST, LINK_RANDOM, LINK_UNLOCK = 101, 132, 104
LINK_FINALE = 121

LINK_WIRE = {30305: {LINK_FIRST: (21245, 6), LINK_UNLOCK: (21253, 1),
                     LINK_RANDOM: (21252, 6), LINK_FINALE: (21251, 1),
                     111: (21246, 6), 112: (21247, 6), 113: (21248, 6),
                     114: (21249, 6), 115: (21250, 6)},
             30276: {1: (21211, 6), 4: (21223, 1), 32: (21218, 6), 21: (21217, 1),
                     11: (21212, 6), 12: (21213, 6), 13: (21214, 6),
                     14: (21215, 6), 15: (21216, 6), 31: (20000, 1)},
             30290: {1: (21228, 6), 4: (21240, 1), 21: (21234, 1), 32: (21235, 6),
                     11: (21229, 6), 12: (21230, 6), 13: (21231, 6),
                     14: (21232, 6), 15: (21233, 6)},
             10141: {1: (21228, 6), 21: (21234, 1), 32: (21236, 6),
                     11: (21229, 6), 12: (21230, 6), 13: (21231, 6),
                     14: (21232, 6), 15: (21233, 6)},
             10138: {1: (21211, 6), 2: (21221, 1), 3: (21222, 1), 21: (21217, 1),
                     11: (21212, 6), 12: (21213, 6), 13: (21214, 6),
                     14: (21215, 6), 15: (21216, 6), 31: (20000, 1),
                     32: (21219, 6), 33: (21220, 6), 22: (38, 3)},
             30257: {1: (21162, 6), 21: (21168, 1), 32: (21169, 6),
                     11: (21163, 6), 12: (21164, 6), 13: (21165, 6),
                     14: (21166, 6), 15: (21167, 6)},
             10128: {1: (21162, 6), 2: (21172, 1), 3: (21173, 1), 21: (21168, 1),
                     32: (21170, 6), 31: (20000, 1),
                     11: (21163, 6), 12: (21164, 6), 13: (21165, 6),
                     14: (21166, 6), 15: (21167, 6)}}

LINK_KIT_OFFSET = {30276: 0, 30290: 0, 10141: 0, 10138: 0, 30257: 0, 10128: 0}
LINK_UNLOCK_SUFFIX = {10141: 2, 10138: 3, 10128: 3}
LINK_PRE_UNLOCK_SUFFIX = {10138: 2, 10128: 2}
_LINK_BASE = 100


def wire_ids(card_id: int, sid: int) -> tuple | None:
    kit = group_kit(card_id)
    if kit and sid == kit.get("passion_end"):
        return kit.get("passion_end_wire")
    if kit.get("wire"):
        return kit["wire"].get(sid - story_base(card_id))
    table = LINK_WIRE.get(card_id)
    return table.get(sid - story_base(card_id)) if table else None

PURE_PASSION = {30067: 101, 30081: 100, 30137: 102, 30241: 104}

PURE_PASSION_TURNS_MIN = 3
PURE_PASSION_TURNS_MAX = 5

GROUP_KITS = {
    30137: {
        "after_members": (20, 21), "after_partner": 9040,
        "random": None, "finale": None, "random_passion": False,
        "passion_turns": (2, 6), "passion_end": 800001001,
        "passion_end_wire": (21081, 1),
        "reserved": (1, 2, 3, 5, 6, 7, 20, 21, 30, 31, 32), "new_year": 32,
        "wire": {1: (21072, 6), 5: (21075, 6), 6: (21076, 6), 7: (21077, 6),
                 20: (21078, 6), 21: (21079, 6), 32: (21080, 1)},
    },
    30241: {
        "after_members": (20, 21), "after_partner": 9047,
        "member_story": {9046: 5, 9047: 6, 9048: 7},
        "passion_condition": 104,
        "random": None, "finale": None, "random_passion": False,
        "passion_turns": (2, 6), "passion_end": 800001001,
        "passion_end_wire": (21158, 1),
        "reserved": (1, 2, 3, 5, 6, 7, 20, 21, 30, 31, 32), "new_year": 32,
        "wire": {1: (21149, 6), 2: (10002, 1), 5: (21152, 6), 6: (21153, 6), 7: (21154, 6),
                 20: (21155, 6), 21: (21156, 6), 32: (21157, 1)},
    },
}


def group_kit(card_id: int) -> dict:
    return GROUP_KITS.get(int(card_id or 0), {})


PURE_PASSION_TURN_WEIGHTS = {2: 0.32, 3: 0.30, 4: 0.23, 5: 0.11, 6: 0.05}


def roll_passion_turns(card_id: int) -> int:
    import random
    turns, weights = zip(*PURE_PASSION_TURN_WEIGHTS.items())
    return random.choices(turns, weights)[0]


def passion_turns(card_id: int) -> tuple:
    return group_kit(card_id).get("passion_turns") or (PURE_PASSION_TURNS_MIN,
                                                       PURE_PASSION_TURNS_MAX)


def random_passion(card_id: int) -> bool:
    return group_kit(card_id).get("random_passion", True)

GROUP_PASSION_END = 4

_DEFAULT_UNLOCK = {1: 1.0}
UNLOCK_CHANCE: dict[int, dict[int, float]] = {
    9001: {1: 1.0, 2: 0.0},
    9006: {1: 0.5, 2: 0.0, 3: 0.0},
}
_GAMBLE_CHANCE = 0.5


@functools.lru_cache(maxsize=64)
def pure_passion_condition(card_id: int) -> int:
    if group_kit(card_id).get("passion_condition"):
        return int(group_kit(card_id)["passion_condition"])
    from .. import event_engine
    sid = unlock_story(card_id) if is_group(card_id) else None
    if sid:
        _title, ev = event_engine.event_by_story_id(card_id, sid)
        for choice in (ev or {}).get("choices") or ():
            for eff in choice.get("effects") or ():
                if eff.get("type") == "condition" and eff.get("condition_id"):
                    return int(eff["condition_id"])
    return PURE_PASSION.get(card_id, 0)


def pure_passion_card(condition_id: int) -> int:
    for card_id in PURE_PASSION:
        if pure_passion_condition(card_id) == int(condition_id or 0):
            return card_id
    return 0


def pure_passion_conditions() -> frozenset:
    return frozenset(pure_passion_condition(c) for c in PURE_PASSION) - {0}


def pure_passion_end_story(card_id: int) -> int | None:
    if group_kit(card_id).get("passion_end"):
        return int(group_kit(card_id)["passion_end"])
    return story_id(card_id, GROUP_PASSION_END) if is_group(card_id) else None


@functools.lru_cache(maxsize=512)
def card_row(card_id: int) -> tuple:
    row = master_data.query_one(
        "SELECT support_card_type, chara_id FROM support_card_data WHERE id=?",
        (card_id,))
    if row is None:
        return (1, 0)
    return (row["support_card_type"] or 1, row["chara_id"] or 0)


def card_type(card_id: int) -> int:
    return card_row(card_id)[0]


def is_pal(card_id: int) -> bool:
    return card_type(card_id) == PAL_TYPE


def is_group(card_id: int) -> bool:
    return card_type(card_id) == GROUP_TYPE


def is_pal_or_group(card_id: int) -> bool:
    return card_type(card_id) in (PAL_TYPE, GROUP_TYPE)


@functools.lru_cache(maxsize=64)
def group_members(card_id: int) -> tuple:
    return tuple(r["chara_id"] for r in master_data.query(
        "SELECT chara_id FROM support_card_group WHERE support_card_id=? ORDER BY id",
        (card_id,)))


def story_base(card_id: int) -> int:
    ctype, chara = card_row(card_id)
    key = chara if ctype == PAL_TYPE else card_id
    return 800000000 + key * 1000


def story_owner(card_id: int) -> int:
    ctype, chara = card_row(card_id)
    return chara if ctype == PAL_TYPE else card_id


def partners(card_id: int) -> tuple:
    if is_pal(card_id):
        return (card_row(card_id)[1],)
    if is_group(card_id):
        return group_members(card_id)
    return ()


@functools.lru_cache(maxsize=64)
def link_offset(card_id: int):
    if not is_pal(card_id):
        return None
    if card_id in LINK_KIT_OFFSET:
        return LINK_KIT_OFFSET[card_id]
    if master_data.query_one(
            "SELECT 1 FROM single_mode_story_data WHERE story_id=? AND support_card_id=?",
            (story_base(card_id) + LINK_FIRST, card_id)):
        return _LINK_BASE
    return None


@functools.lru_cache(maxsize=64)
def _link_chain(card_id: int) -> tuple:
    offset = link_offset(card_id)
    if offset is None:
        return ()
    base = story_base(card_id) + offset
    chara = card_row(card_id)[1]
    rows = master_data.query(
        "SELECT DISTINCT story_id, show_progress_1 FROM single_mode_story_data "
        "WHERE (support_card_id=? OR (support_card_id=0 AND support_chara_id=?)) "
        "AND show_progress_1>0 AND story_id BETWEEN ? AND ? "
        "ORDER BY show_progress_1", (card_id, chara, base + 10, base + 19))
    return tuple(int(r["story_id"]) for r in rows)


def _pal_suffix(card_id: int, old: int, link: int) -> int:
    offset = link_offset(card_id)
    return old if offset is None else link - _LINK_BASE + offset


@functools.lru_cache(maxsize=512)
def _existing_suffixes(card_id: int) -> frozenset:
    base = story_base(card_id)
    return frozenset(r["index"] - base for r in master_data.query(
        "SELECT [index] FROM text_data WHERE category=181 AND [index] BETWEEN ? AND ?",
        (base, base + 999)))


def story_id(card_id: int, suffix: int) -> int | None:
    return story_base(card_id) + suffix if suffix in _existing_suffixes(card_id) else None


def first_meeting_story(card_id: int) -> int | None:
    return story_id(card_id, _pal_suffix(card_id, PAL_FIRST, LINK_FIRST)
                    if is_pal(card_id) else GROUP_FIRST)


def unlock_story(card_id: int) -> int | None:
    if card_id in LINK_UNLOCK_SUFFIX:
        return story_id(card_id, LINK_UNLOCK_SUFFIX[card_id])
    return story_id(card_id, _pal_suffix(card_id, PAL_UNLOCK, LINK_UNLOCK)
                    if is_pal(card_id) else GROUP_UNLOCK)


def pre_unlock_story(card_id: int) -> int | None:
    suffix = LINK_PRE_UNLOCK_SUFFIX.get(card_id)
    return story_id(card_id, suffix) if suffix else None


def random_story(card_id: int) -> int | None:
    if "random" in group_kit(card_id):
        suffix = group_kit(card_id)["random"]
        return story_id(card_id, suffix) if suffix else None
    return story_id(card_id, _pal_suffix(card_id, PAL_RANDOM, LINK_RANDOM)
                    if is_pal(card_id) else GROUP_RANDOM)


@functools.lru_cache(maxsize=64)
def member_chain(card_id: int, member: int) -> tuple:
    from .. import event_engine
    members = group_members(card_id)
    if member not in members:
        return ()
    chain = []
    suffix = (group_kit(card_id).get("member_story") or {}).get(
        member, GROUP_CHAIN_BASE + members.index(member))
    head = story_id(card_id, suffix)
    if head:
        chain.append(head)
    kit = group_kit(card_id)
    if kit:
        if member == kit.get("after_partner"):
            chain += [s for s in (story_id(card_id, x) for x in kit.get("after_members") or ()) if s]
        return tuple(chain)
    gt = event_engine.gametora.load_cached("support", card_id) or {}
    base = 800000000 + member * 1000
    extra = set()
    for key, ev in gt.items():
        title = ev.get("name")
        if not title:
            continue
        owners = {e.get("char_id") for c in ev.get("choices") or []
                  for e in c.get("effects") or [] if e.get("char_id")}
        if owners and member not in owners:
            continue
        sid = event_engine._story_id_by_title(title, base) or next(
            (idx for idx, t in event_engine._titles_in_window(base, 1000)
             if "".join(ch for ch in t if ch.isalnum()) == key), None)
        if sid:
            extra.add(sid)
    return tuple(chain + sorted(extra))


PAL_EXTRA_OUTINGS = {30052: (830052001,)}


def outing_story(card_id: int, partner_chara: int, step: int) -> int | None:
    if _link_chain(card_id):
        chain = list(_link_chain(card_id))
    elif is_pal(card_id):
        chain = [story_base(card_id) + s for s in PAL_CHAIN
                 if s in _existing_suffixes(card_id)] + list(PAL_EXTRA_OUTINGS.get(card_id, ()))
    else:
        chain = member_chain(card_id, partner_chara)
    return chain[step] if step < len(chain) else None


def outing_chain_len(card_id: int, partner_chara: int | None = None) -> int:
    if _link_chain(card_id):
        return len(_link_chain(card_id))
    if is_pal(card_id):
        return len([s for s in PAL_CHAIN if s in _existing_suffixes(card_id)]) + len(PAL_EXTRA_OUTINGS.get(card_id, ()))
    if partner_chara is not None:
        return len(member_chain(card_id, partner_chara))
    return sum(len(member_chain(card_id, m)) for m in group_members(card_id))


def finale_story(card_id: int) -> int | None:
    if is_pal(card_id):
        return story_id(card_id, _pal_suffix(card_id, PAL_FINALE, LINK_FINALE))
    if "finale" in group_kit(card_id):
        return story_id(card_id, group_kit(card_id)["finale"]) if group_kit(card_id)["finale"] else None
    have = sorted(s for s in _existing_suffixes(card_id) if s in GROUP_FINALE)
    return story_base(card_id) + have[0] if have else None


def ending_story(card_id: int, complete: bool) -> int | None:
    if not is_pal(card_id):
        return None
    if link_offset(card_id) is None:
        return story_id(card_id, PAL_ENDING_COMPLETE if complete else PAL_ENDING_INCOMPLETE)
    return story_id(card_id, link_offset(card_id) + (22 if complete else 23))


@functools.lru_cache(maxsize=64)
def new_year_story(card_id: int) -> int | None:
    if group_kit(card_id).get("new_year"):
        return story_id(card_id, group_kit(card_id)["new_year"])
    offset = link_offset(card_id)
    if offset is not None:
        return story_id(card_id, _pal_suffix(card_id, PAL_FINALE, LINK_FINALE))
    from .. import event_engine
    gt = event_engine.gametora.load_cached("support", card_id) or {}
    title = next((ev.get("name") for ev in gt.values()
                 if ev.get("event_type") == "ny"), None)
    if not title:
        return None
    return event_engine._story_id_by_title(title, story_base(card_id))


@functools.lru_cache(maxsize=64)
def unlock_chances(card_id: int) -> dict:
    if _link_chain(card_id):
        return {1: 1.0, 2: 1.0}
    override = UNLOCK_CHANCE.get(story_owner(card_id))
    if override:
        return dict(override)
    sid = unlock_story(card_id)
    if sid:
        from .. import event_engine
        _title, ev = event_engine.event_by_story_id(card_id, sid)
        choices = (ev or {}).get("choices") or []
        gambles = [i + 1 for i, c in enumerate(choices) if c.get("random_either")]
        if gambles:
            return {i: _GAMBLE_CHANCE for i in gambles}
        if choices:
            return {1: 1.0}
    return dict(_DEFAULT_UNLOCK)


def unlock_chance(card_id: int, select_index: int) -> float:
    return unlock_chances(card_id).get(select_index, 0.0)


@functools.lru_cache(maxsize=512)
def reserved_story_ids(card_id: int) -> frozenset:
    if not is_pal_or_group(card_id):
        return frozenset()
    base = story_base(card_id)
    have = _existing_suffixes(card_id)
    if is_pal(card_id):
        reserved = {base + s for s in have}
        card_base = 800000000 + card_id * 1000
        reserved |= {r["index"] for r in master_data.query(
            "SELECT [index] FROM text_data WHERE category=181 "
            "AND [index] BETWEEN ? AND ?", (card_base, card_base + 999))}
    else:
        if group_kit(card_id).get("reserved"):
            return frozenset(base + x for x in group_kit(card_id)["reserved"] if x in have)
        reserved = {base + s for s in have}
        for m in group_members(card_id):
            reserved |= set(member_chain(card_id, m))
    return frozenset(reserved)


def is_unlock_story(card_id: int, sid: int) -> bool:
    return bool(sid) and sid == unlock_story(card_id)


def is_reserved(card_id: int, sid: int) -> bool:
    return sid in reserved_story_ids(card_id)
