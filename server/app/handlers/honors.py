
from __future__ import annotations

import json
from pathlib import Path

from .. import master_data
from . import collection, missions, single_mode_team, stories, team_stadium

_CATALOGUE_PATH = (Path(__file__).resolve().parents[3]
                   / "data" / "training_ref" / "honor_data.json")

_catalogue_cache: list | None = None
_mission_reward_honor_ids_cache: frozenset | None = None


def _mission_reward_honor_ids() -> frozenset:
    global _mission_reward_honor_ids_cache
    if _mission_reward_honor_ids_cache is None:
        rows = master_data.query(
            "SELECT DISTINCT item_id FROM mission_data WHERE item_category=55")
        _mission_reward_honor_ids_cache = frozenset(r["item_id"] for r in rows)
    return _mission_reward_honor_ids_cache


def _catalogue() -> list:
    global _catalogue_cache
    if _catalogue_cache is None:
        try:
            with open(_CATALOGUE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = []
        claim_gated = _mission_reward_honor_ids()
        _catalogue_cache = [d for d in data if d["condition"]["kind"] in _EVALUATORS
                            and d["honor_id"] not in claim_gated]
    return _catalogue_cache


_G13_GRADES = (100, 200, 300)


def _grade_trophy_instance_ids(grade: int) -> frozenset:
    rows = master_data.query(
        "SELECT rt.race_instance_id AS inst FROM race_trophy rt "
        "JOIN race_instance ri ON ri.id = rt.race_instance_id "
        "JOIN race rc ON rc.id = ri.race_id WHERE rc.grade=?", (grade,))
    return frozenset(r["inst"] for r in rows)


_BIG_EIGHT_NAMES = ("Oka Sho", "Satsuki Sho", "Tenno Sho (Spring)", "Japanese Oaks",
                   "Tokyo Yushun (Japanese Derby)", "Kikuka Sho", "Tenno Sho (Autumn)",
                   "Arima Kinen")


class Context:

    def __init__(self, viewer_id, full_state: dict, owned_honor_count: int = 0):
        self.genuine = missions._genuine_roster(viewer_id)
        self.charas = full_state.get(collection.CHARA_LIST_KEY) or []
        self.cards = full_state.get(collection.CARD_LIST_KEY) or []
        self.support_cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
        self.story_state = stories._story_state(full_state)
        crane_lifetime = full_state.get(single_mode_team.CRANE_LIFETIME_KEY) or {}
        self.crane_lifetime_plushies = crane_lifetime.get("plushie_count") or 0
        ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
        self.team_best_point = ts_state.get("best_point") or 0
        self.owned_honor_count = owned_honor_count

    def won_race_grade_count(self, grade: int) -> int:
        total = 0
        for c in self.genuine:
            for pid in c.get("_won_program_ids") or ():
                row = missions._program_race_row(pid)
                if row and row["grade"] == grade:
                    total += 1
        return total

    def won_race_ids_at(self, race_ids) -> int:
        target = frozenset(race_ids)
        total = 0
        for c in self.genuine:
            for pid in c.get("_won_program_ids") or ():
                row = missions._program_race_row(pid)
                if row and row["race_id"] in target:
                    total += 1
        return total

    def distinct_songs_performed(self) -> set:
        out = set()
        for c in self.genuine:
            for l in c.get("grand_live_lives_done") or ():
                out.update(l.get("song_ids") or ())
        return out


_EVALUATORS: dict = {}


def _evaluator(kind: str):
    def deco(fn):
        _EVALUATORS[kind] = fn
        return fn
    return deco


@_evaluator("chara_fan_threshold")
def _e_chara_fan_threshold(ctx: Context, cond: dict) -> bool:
    total = sum(c.get("fan") or 0 for c in ctx.charas if c.get("chara_id") == cond["chara_id"])
    return total >= cond["threshold"]


@_evaluator("chara_story_episode")
def _e_chara_story_episode(ctx: Context, cond: dict) -> bool:
    return cond["chara_story_data_id"] in (ctx.story_state.get("chara_cleared") or [])


@_evaluator("grade_win_count")
def _e_grade_win_count(ctx: Context, cond: dict) -> bool:
    return ctx.won_race_grade_count(cond["grade"]) >= cond["threshold"]


@_evaluator("all_trophies_by_grade")
def _e_all_trophies_by_grade(ctx: Context, cond: dict) -> bool:
    target = _grade_trophy_instance_ids(cond["grade"])
    if not target:
        return False
    won = set()
    for c in ctx.genuine:
        for pid in c.get("_won_program_ids") or ():
            row = missions._program_race_row(pid)
            if row and row["grade"] == cond["grade"]:
                won.add(row["inst"])
    return target <= won


@_evaluator("rank_achieved_count")
def _e_rank_achieved_count(ctx: Context, cond: dict) -> bool:
    n = sum(1 for c in ctx.genuine if (c.get("rank") or 0) >= cond["rank_id"])
    return n >= cond["threshold"]


@_evaluator("team_rank")
def _e_team_rank(ctx: Context, cond: dict) -> bool:
    return ctx.team_best_point >= cond["team_min_value"]


@_evaluator("venue_win_count")
def _e_venue_win_count(ctx: Context, cond: dict) -> bool:
    return ctx.won_race_ids_at(cond["race_ids"]) >= cond["threshold"]


@_evaluator("crane_session_plushies")
def _e_crane_session_plushies(ctx: Context, cond: dict) -> bool:
    return any((c.get("_crane_session_plushies") or 0) >= cond["threshold"] for c in ctx.genuine)


@_evaluator("crane_lifetime_plushies")
def _e_crane_lifetime_plushies(ctx: Context, cond: dict) -> bool:
    return ctx.crane_lifetime_plushies >= cond["threshold"]


@_evaluator("career_fan_threshold")
def _e_career_fan_threshold(ctx: Context, cond: dict) -> bool:
    return any((c.get("fans") or 0) >= cond["threshold"] for c in ctx.genuine)


@_evaluator("ura_finale_win")
def _e_ura_finale_win(ctx: Context, cond: dict) -> bool:
    finale_ids = missions._finale_program_ids()
    return any(finale_ids & set(c.get("_won_program_ids") or ()) for c in ctx.genuine)


@_evaluator("big_eight")
def _e_big_eight(ctx: Context, cond: dict) -> bool:
    targets = [missions._race_ids_by_name(n) for n in _BIG_EIGHT_NAMES]
    for c in ctx.genuine:
        won = missions._won_race_ids(c)
        if all(won & t for t in targets):
            return True
    return False


@_evaluator("add_trainees")
def _e_add_trainees(ctx: Context, cond: dict) -> bool:
    return len(ctx.genuine) >= cond["threshold"]


@_evaluator("support_card_kind_num")
def _e_support_card_kind_num(ctx: Context, cond: dict) -> bool:
    return len(ctx.support_cards) >= cond["threshold"]


@_evaluator("watch_unique_concerts")
def _e_watch_unique_concerts(ctx: Context, cond: dict) -> bool:
    return len(ctx.distinct_songs_performed()) >= cond["threshold"]


@_evaluator("unlock_star_count")
def _e_unlock_star_count(ctx: Context, cond: dict) -> bool:
    n = sum(1 for c in ctx.cards if (c.get("talent_level") or 1) >= cond["star"])
    return n >= cond["threshold"]


@_evaluator("all_trophies_for_chara")
def _e_all_trophies_for_chara(ctx: Context, cond: dict) -> bool:
    target = missions._trophy_instance_ids()
    if not target:
        return False
    won = set()
    for c in ctx.genuine:
        if missions._chara_id_for_card(c.get("card_id")) != cond["chara_id"]:
            continue
        for pid in c.get("_won_program_ids") or ():
            row = missions._program_race_row(pid)
            if row and row["grade"] in _G13_GRADES:
                won.add(row["inst"])
    return target <= won


@_evaluator("bond_level_threshold_count")
def _e_bond_level_threshold_count(ctx: Context, cond: dict) -> bool:
    n = sum(1 for c in ctx.charas
           if missions._love_rank_level(c.get("love_point") or 0) >= cond["bond_level"])
    return n >= cond["chara_count"]


@_evaluator("title_collector")
def _e_title_collector(ctx: Context, cond: dict) -> bool:
    return ctx.owned_honor_count >= cond["threshold"]


@_evaluator("view_distinct_stories")
def _e_view_distinct_stories(ctx: Context, cond: dict) -> bool:
    st = ctx.story_state
    total = (len(st.get("chara_cleared") or [])
            + len(st.get("main_cleared") or [])
            + len(st.get("story_event_cleared") or []))
    return total >= cond["threshold"]


@_evaluator("watch_song")
def _e_watch_song(ctx: Context, cond: dict) -> bool:
    live_id = cond["live_id"]
    for c in ctx.genuine:
        for l in c.get("grand_live_lives_done") or ():
            if live_id in (l.get("song_ids") or ()):
                return True
    return False


@_evaluator("skill_combo_owned")
def _e_skill_combo_owned(ctx: Context, cond: dict) -> bool:
    target = cond["skill_ids"]
    for c in ctx.genuine:
        owned = {s.get("skill_id") for s in c.get("skill_array") or ()}
        if all(sid in owned for sid in target):
            return True
    return False


@_evaluator("concert_all_great")
def _e_concert_all_great(ctx: Context, cond: dict) -> bool:
    for c in ctx.genuine:
        lives = c.get("grand_live_lives_done") or []
        if lives and all(l.get("result_state") == 2 for l in lives):
            return True
    return False


def newly_earned_honor_ids(viewer_id, full_state: dict, already_earned: set,
                           owned_honor_count: int) -> list:
    ctx = Context(viewer_id, full_state, owned_honor_count)
    out = []
    for row in _catalogue():
        hid = row["honor_id"]
        if str(hid) in already_earned:
            continue
        fn = _EVALUATORS.get(row["condition"]["kind"])
        if fn and fn(ctx, row["condition"]):
            out.append(hid)
    return out
