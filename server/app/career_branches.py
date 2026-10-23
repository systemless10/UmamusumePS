from __future__ import annotations

import functools
import json
import logging
import os
from dataclasses import dataclass

from . import master_data

log = logging.getLogger(__name__)

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "data", "events", "story_branches.json")

SHOW_CHANGE = 4
SHOW_DECIDE = 5
SHOW_ADD = 23
SHOW_CHANGE_ADD = 25
_UNCONDITIONAL_SHOWS = (SHOW_CHANGE, SHOW_ADD, SHOW_CHANGE_ADD)

DR_DEFAULT = 1
DR_ALTERNATE = 2
_ADDED_GOAL_DR = (3, 7)

LEAD_TURNS = 2

_FIXED_SUFFIX_TURN = {300: 40, 301: 64}


@dataclass(frozen=True)
class Arm:
    id: int
    sort_id: int
    turn: int
    program: int
    inst: int
    race_id: int
    dr: int
    flag: int
    gen: int


@dataclass(frozen=True)
class BranchStory:
    story_id: int
    choices: tuple
    by_select_index: bool
    sort_ids: frozenset
    first_turn: int


@functools.lru_cache(maxsize=1)
def _data() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        log.exception("story branch data unreadable (%s); no master-derived branches", _PATH)
        return {"roots": [], "conditions": []}


def _band(chara_id: int) -> tuple:
    base = 500000000 + chara_id * 1000
    return base, base + 999


@functools.lru_cache(maxsize=256)
def route_rows(race_set_id: int) -> tuple:
    rows = master_data.query(
        "SELECT rr.id AS id, rr.sort_id AS sort_id, rr.turn AS turn, "
        "       rr.condition_type AS ct, rr.target_type AS tt, rr.condition_id AS program, "
        "       rr.determine_race AS dr, rr.determine_race_flag AS flag, "
        "       rr.determine_race_for_generate AS gen, "
        "       p.race_instance_id AS inst, ri.race_id AS race_id "
        "FROM single_mode_route_race rr "
        "LEFT JOIN single_mode_program p ON rr.condition_type = 1 AND p.id = rr.condition_id "
        "LEFT JOIN race_instance ri ON ri.id = p.race_instance_id "
        "WHERE rr.race_set_id=? ORDER BY rr.id", (race_set_id,))
    return tuple(Arm(id=r["id"], sort_id=r["sort_id"], turn=r["turn"] or 0,
                     program=r["program"] or 0, inst=r["inst"] or 0,
                     race_id=r["race_id"] or 0, dr=r["dr"] or 0,
                     flag=r["flag"] or 0, gen=r["gen"] or 0)
                 for r in rows if r["tt"] in (1, 3))


def arms(race_set_id: int) -> tuple:
    return tuple(a for a in route_rows(race_set_id) if a.dr)


def _groups(race_set_id: int) -> dict:
    out: dict = {}
    for a in arms(race_set_id):
        out.setdefault(a.sort_id, []).append(a)
    return out


def _race_stories(chara_id: int, race_set_id: int, arm: Arm, prefix: str,
                  every_run: bool = False) -> frozenset:
    if not arm.program or not arm.race_id:
        return frozenset()
    from .handlers import single_mode_team as smt
    info = smt._race_info_for_program(arm.program)
    name = info and info.get("story_name")
    if not name:
        return frozenset()
    if every_run:
        found = set()
        for k in range(6):
            found.update(smt._find_race_story_ids(chara_id, prefix, name, k, 99))
        return frozenset(found)
    turns = sorted({a.turn for a in route_rows(race_set_id) if a.race_id == arm.race_id})
    occ = turns.index(arm.turn) if arm.turn in turns else 0
    return frozenset(smt._find_race_story_ids(chara_id, prefix, name, occ, len(turns)))


def _children(chara_id: int) -> dict:
    lo, hi = _band(chara_id)
    out: dict = {}
    for row in _data().get("conditions") or ():
        sid = row["story_id"]
        if not lo <= sid <= hi:
            continue
        for parent, gain, sel in row["parents"]:
            out.setdefault(parent, []).append((sid, gain or 0, sel or 0))
    return out


def _descendants(children: dict, root: int, by_select: bool, value: int) -> set:
    seen: set = set()
    frontier = [c for c, g, s in children.get(root, ())
                if (s if by_select else g) == value]
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        frontier.extend(c for c, g, s in children.get(sid, ()) if not g and not s)
    return seen


def _pick_arms(chara_id: int, race_set_id: int, stories: set, after_turn: int = 0) -> tuple:
    chosen, ambiguous = {}, {}
    for sort_id, group in _groups(race_set_id).items():
        group = [a for a in group if a.turn > after_turn]
        hit = [a for a in group if _race_stories(chara_id, race_set_id, a, "Before the ") & stories]
        if not hit:
            hit = [a for a in group if _race_stories(
                chara_id, race_set_id, a, "Before the ", every_run=True) & stories]
        if len(hit) == 1:
            chosen[sort_id] = hit[0]
        elif hit:
            ambiguous[sort_id] = hit
    flags = {a.flag for a in chosen.values() if a.flag}
    for sort_id, hit in ambiguous.items():
        by_flag = [a for a in hit if a.flag in flags]
        if len(by_flag) == 1:
            chosen[sort_id] = by_flag[0]
    return tuple(a.id for a in sorted(chosen.values(), key=lambda a: a.turn))


def anchor_turn(chara_id: int, race_set_id: int, story_id: int) -> int:
    fixed = _FIXED_SUFFIX_TURN.get(story_id % 1000)
    if fixed:
        return fixed
    for row in route_rows(race_set_id):
        if story_id in _race_stories(chara_id, race_set_id, row, "After the "):
            return row.turn
    return 0


def _unconditional_arms(chara_id: int, race_set_id: int, story_id: int, show: int) -> tuple:
    anchor = anchor_turn(chara_id, race_set_id, story_id)
    groups = _groups(race_set_id)
    out = []
    if show in (SHOW_CHANGE, SHOW_CHANGE_ADD):
        alts = sorted((a for g in groups.values() if len(g) > 1 for a in g
                       if a.dr == DR_ALTERNATE and min(x.turn for x in g) > anchor),
                      key=lambda a: a.turn)
        if alts:
            out.append(alts[0])
    if show in (SHOW_ADD, SHOW_CHANGE_ADD):
        added = sorted((g[0] for g in groups.values()
                        if len(g) == 1 and g[0].dr in _ADDED_GOAL_DR and g[0].turn > anchor),
                       key=lambda a: a.turn)
        if added:
            out.append(added[0])
    return tuple(a.id for a in out)


@functools.lru_cache(maxsize=256)
def branch_stories(chara_id: int, race_set_id: int) -> dict:
    if not chara_id or not race_set_id or not arms(race_set_id):
        return {}
    lo, hi = _band(chara_id)
    children = _children(chara_id)
    by_id = {a.id: a for a in route_rows(race_set_id)}
    out: dict = {}

    def add(story_id, choices, by_select):
        choices = tuple(c for c in choices)
        ids = {i for c in choices for i in c}
        if not ids:
            return
        sorts = frozenset(by_id[i].sort_id for i in ids)
        first = min(a.turn for a in arms(race_set_id) if a.sort_id in sorts)
        out[story_id] = BranchStory(story_id, choices, by_select, sorts, first)

    roots = {r["story_id"] for r in _data().get("roots") or () if lo <= r["story_id"] <= hi}
    roots |= {p for p, kids in children.items() if lo <= p <= hi
              and any(g or s for _c, g, s in kids)}
    for root in sorted(roots):
        kids = children.get(root, ())
        by_select = not any(g for _c, g, _s in kids) and any(s for _c, _g, s in kids)
        values = sorted({(s if by_select else g) for _c, g, s in kids} - {0})
        anchor = anchor_turn(chara_id, race_set_id, root)
        choices = [_pick_arms(chara_id, race_set_id,
                              _descendants(children, root, by_select, v), anchor)
                   for v in values]
        if any(choices):
            add(root, choices, by_select)

    for r in master_data.query(
            "SELECT story_id, show_clear FROM single_mode_story_data "
            "WHERE story_id BETWEEN ? AND ? AND show_clear IN (?, ?, ?)",
            (lo, hi, *_UNCONDITIONAL_SHOWS)):
        if r["story_id"] in out:
            continue
        picked = _unconditional_arms(chara_id, race_set_id, r["story_id"], r["show_clear"])
        if picked:
            add(r["story_id"], (picked,), False)
    return out


def added_goal_ids(chara_id: int, race_set_id: int) -> frozenset:
    lone = {g[0].id for g in _groups(race_set_id).values()
            if len(g) == 1 and g[0].dr in _ADDED_GOAL_DR}
    targeted = {i for bs in branch_stories(chara_id, race_set_id).values()
                for c in bs.choices for i in c}
    return frozenset(lone & targeted)


def start_route(chara_id: int, race_set_id: int, ids) -> list:
    ids = [int(i) for i in ids]
    drop = set(added_goal_ids(chara_id, race_set_id))
    for group in _groups(race_set_id).values():
        if len(group) < 2:
            continue
        pick = (next((a for a in group if a.dr == DR_DEFAULT), None)
                or next((a for a in group if a.gen == 0), None)
                or min(group, key=lambda a: a.id))
        drop |= {a.id for a in group if a.id != pick.id}
    return [i for i in ids if i not in drop]


def fire_turn(race_set_id: int, bs: BranchStory) -> int:
    before = [a.turn for a in route_rows(race_set_id)
              if a.turn < bs.first_turn and a.sort_id not in bs.sort_ids]
    floor = (max(before) + 1) if before else 2
    return max(2, floor, bs.first_turn - LEAD_TURNS)


def effects_for_choice(race_set_id: int, bs: BranchStory, index: int) -> list:
    if not 0 <= index < len(bs.choices):
        return []
    by_id = {a.id: a for a in route_rows(race_set_id)}
    return [{"type": "race_change", "race_instance_id": by_id[i].inst, "route_race_id": i}
            for i in bs.choices[index] if i in by_id]


GATE_ALL = 1
GATE_ANY = 2


@functools.lru_cache(maxsize=512)
def chapter_stories(chara_id: int) -> tuple:
    lo, hi = _band(chara_id)
    rows = sorted((r["order"], r["story_id"], r["turn"])
                  for r in _data().get("chapters") or () if lo <= r["story_id"] <= hi)
    slots: dict = {}
    for order, sid, _turn in rows:
        slots.setdefault(order, []).append(sid)
    gates = _gates()
    undecidable = {sid for sids in slots.values() if len(sids) > 1
                   and not any(s in gates for s in sids) for sid in sids}
    return tuple((sid, turn) for _order, sid, turn in rows if sid not in undecidable)


@functools.lru_cache(maxsize=512)
def chapter_band_ids(chara_id: int) -> frozenset:
    lo, hi = _band(chara_id)
    return frozenset(r["story_id"] for r in _data().get("chapters") or ()
                     if lo <= r["story_id"] <= hi)


@functools.lru_cache(maxsize=1)
def _gates() -> dict:
    return {r["story_id"]: (r.get("type", GATE_ALL),
                            tuple((p, g or 0, s or 0) for p, g, s in r["parents"]))
            for r in _data().get("conditions") or () if r.get("parents")}


@functools.lru_cache(maxsize=1)
def gate_parents() -> frozenset:
    return frozenset(p for _t, parents in _gates().values() for p, _g, _s in parents)


def gate_met(story_id: int, played, choices) -> bool:
    gate = _gates().get(story_id)
    if gate is None:
        return True
    kind, parents = gate

    def ok(parent, gain, sel):
        if parent not in played or sel:
            return False
        return not gain or choices.get(parent) == gain

    hits = [ok(*p) for p in parents]
    return any(hits) if kind == GATE_ANY else all(hits)
