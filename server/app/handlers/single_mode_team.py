"""
Stateful handlers for the career/training mode (single_mode_team/*), the
part of the API where the server is authoritative over game logic (stat
gains, RNG events, race outcomes).

Strategy while master data (growth rates, support card effect tables, skill
conditions) isn't extracted yet:

1. exec_command / check_event / race_* etc. first look for a captured
   fixture whose *request* matches the live request on the fields that
   actually distinguish game states (command_id, current_turn, ...). If
   found, that exact real response is replayed -- this is not a guess, it's
   what the real server actually did for that exact situation.
2. If no exact match exists (the player deviated from the captured career
   -- different training choice, different turn), we fall back to a
   heuristic in `_simulate_exec_command`. That heuristic is clearly a rough
   approximation (flat stat bump, no support-card bonuses, no fail chance)
   and should be replaced once master data lets us implement the real
   formulas. It exists so the loop doesn't just error out the moment you
   go off-script.

All state is the full `data` blob from single_mode_team/start, persisted
per-viewer and mutated turn to turn.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import logging

from .. import career_events
from .. import concerts
from .. import epithets
from .. import config
from .. import event_engine
from .. import master_data
from .. import state as state_store
from .. import training_formula
from ..fixtures import store as fixtures
from ..simulation import race_simulator
from . import (bond, collection, conditions, missions, pal_cards,
               practice_race, presents, race_fatigue, secret_events,
               single_mode_events, stamina, trained_chara, transfer)
# Importing scenarios registers every scenario-owned event producer and resolver
# with career_events, by import side effect (see scenarios/__init__.py). Without
# it the producer registry is empty and no migrated family fires at all.
from .. import scenarios

log = logging.getLogger("uma-server")

STATE_KEY = "single_mode_team"
START_CHARA_STATE_KEY = "single_mode_team_start_chara"
FRIEND_SUPPORT_INJECT_KEY = "injected_friend_support_cards"  # see
# inject_friend_support_card() / handle_pre_single_mode_index's
# friend_support_card_data section

SCENARIO_NAMES = {1: "URA Finale", 2: "Unity Cup", 3: "Grand Live"}

# single_mode_chara_id is a server-assigned per-career instance id (not a master
# data id -- 601 in the capture, unrelated to the card). We mint our own in a
# high band that can't collide with roster trained_chara_ids (<=~2.9M) or the
# captured value, derived from card_id so a re-start of the same trainee is
# stable.
SM_CHARA_ID_BASE = 9_000_000

_STAT_TO_RARITY_COL = {
    "speed": "speed", "stamina": "stamina", "power": "pow",
    "guts": "guts", "wiz": "wiz",
}
_APTITUDE_FIELDS = (
    "proper_distance_short", "proper_distance_mile", "proper_distance_middle",
    "proper_distance_long", "proper_running_style_nige",
    "proper_running_style_senko", "proper_running_style_sashi",
    "proper_running_style_oikomi", "proper_ground_turf", "proper_ground_dirt",
)


def _card_rarity_row(card_id: int, star_level: int | None = None) -> dict | None:
    """The card_rarity_data row (base stats, max stats, aptitudes) for a trainee
    at its STAR LEVEL. Each star (rarity 3->4->5) adds ~+10 to every base stat, so
    a 5-star uma must use the rarity-5 row -- using default_rarity (usually 3)
    silently robbed higher-star umas of their stat bonus. Picks the row for
    `star_level` (clamped to the card's available rarities); falls back to
    default_rarity, then the highest row present."""
    rows = master_data.query(
        "SELECT * FROM card_rarity_data WHERE card_id=? ORDER BY rarity", (card_id,))
    if not rows:
        return None
    by_rarity = {r["rarity"]: dict(r) for r in rows}
    if star_level:
        target = max(min(by_rarity), min(max(by_rarity), int(star_level)))
        return by_rarity[target]
    dr_row = master_data.query_one("SELECT default_rarity FROM card_data WHERE id=?", (card_id,))
    dr = dr_row["default_rarity"] if dr_row else None
    return by_rarity.get(dr) or by_rarity[max(by_rarity)]


def _owned_support(viewer_id) -> dict:
    """support_card_id -> owned entry ({exp, limit_break_count, ...}) from the
    viewer's dynamic support_card_collection."""
    full = state_store.get_state(viewer_id) or {}
    return {s.get("support_card_id"): s for s in (full.get(collection.SUPPORT_CARD_KEY) or [])}


def _career_support_array(viewer_id, start_chara: dict) -> list[dict]:
    """The 6-slot deck for chara_info: the 5 chosen support cards + the friend
    card, each carrying the uncap/exp the viewer actually owns."""
    owned = _owned_support(viewer_id)
    out = []
    for sid in list(start_chara.get("support_card_ids") or [])[:5]:
        o = owned.get(sid, {})
        out.append({
            "position": len(out) + 1,
            "support_card_id": sid,
            "limit_break_count": o.get("limit_break_count", 0),
            "exp": o.get("exp", 0),
            "owner_viewer_id": 0,
        })
    friend = (start_chara.get("friend_support_card_info") or {}).get("support_card_id")
    if friend:
        o = owned.get(friend, {})
        out.append({
            "position": len(out) + 1,
            "support_card_id": friend,
            "limit_break_count": o.get("limit_break_count", 0),
            "exp": o.get("exp", 0),
            "owner_viewer_id": 0,
        })
    return out


def _owned_talent_level(viewer_id, card_id: int) -> int:
    full = state_store.get_state(viewer_id) or {}
    for c in (full.get(collection.CARD_LIST_KEY) or []):
        if c.get("card_id") == card_id:
            return c.get("talent_level", 0) or 0
    return 0


def _career_route(scenario_id, chara_id, chara_info=None):
    """(route_id, [route_race_id, ...]) for a career trainee, straight from
    master.mdb, so the client shows THIS uma's real goal races.

    single_mode_route maps (scenario, chara) -> race_set_id; single_mode_route_race
    lists that set's races (target_type 1 = goal, 3 = the scenario's finals). The
    generic per-uma historical route lives under scenario_id 0 and is what URA
    (1), Unity Cup (2), and Grand Concert (3) all use.

    A scenario whose routes are shaped differently answers for itself through
    Scenario.career_route -- Trackblazer's are chara_id 0 and selected by
    APTITUDE, seven shared rows rather than one per trainee, which is why that
    hook is handed the whole chara_info. Returns (None, []) when the chara has
    no route so the caller keeps the template's value."""
    own = scenarios.get(scenario_id).career_route(chara_info or {})
    if own is not None:
        return own
    rows = master_data.query(
        "SELECT id, race_set_id FROM single_mode_route "
        "WHERE scenario_id=0 AND chara_id=? ORDER BY priority DESC LIMIT 1", (chara_id,))
    row = rows[0] if rows else None
    if not row:
        return None, []
    races = master_data.query(
        "SELECT id FROM single_mode_route_race WHERE race_set_id=? ORDER BY id",
        (row["race_set_id"],))
    ids = tuple(r["id"] for r in races)
    # ONE arm per branch group. A career shows exactly ONE goal at a time
    # (user-confirmed 2026-09-04): the player never picks between goals, and an
    # event that swaps the objective REPLACES it. Sending master's full arm list
    # made the client render the alternative as a "Change Goal" offer beside the
    # live goal -- two goals on screen, which the real game never shows. Start on
    # master's own default (determine_race_for_generate == 0); switch_route_race
    # swaps the arm in place when a storyline branches.
    return row["id"], [i for i in ids if i not in _dropped_alternative_ids(ids)]


def _build_start_chara_info(viewer_id, start_chara: dict, template_ci: dict) -> dict:
    """A turn-1 chara_info for the player's ACTUAL selections -- chosen trainee
    (base stats/aptitudes from card_rarity_data), chosen 2 legacies, and the
    owned support deck -- built off the captured chara_info so every unrelated
    structural field keeps a valid shape. This is what un-hardcodes the recorded
    run's trainee (chara 1004) and legacy ids (176/220) that the client NullRefs
    resolving against a roster that no longer contains them."""
    ci = copy.deepcopy(template_ci)
    card_id = start_chara.get("card_id")
    # The trainee's star level (talent_level) selects the card_rarity_data row --
    # each star adds base stats. Prefer the value the client sent, else the owned
    # copy's rank.
    star_level = start_chara.get("talent_level") or _owned_talent_level(viewer_id, card_id)
    rd = _card_rarity_row(card_id, star_level) if card_id else None

    ci["card_id"] = card_id
    ci["single_mode_chara_id"] = SM_CHARA_ID_BASE + (card_id or 0) % 1_000_000
    ci["scenario_id"] = start_chara.get("scenario_id", ci.get("scenario_id"))
    ci["talent_level"] = star_level

    cap_bonus = scenarios.cap_bonus_for_scenario(ci.get("scenario_id") or 1)
    if rd:
        ci["rarity"] = rd.get("rarity", ci.get("rarity"))
        for stat, col in _STAT_TO_RARITY_COL.items():
            ci[stat] = rd.get(col, 0)
            base_cap = rd.get(f"max_{col}", ci.get(f"max_{stat}", 1200))
            # The career cap boost over the card's base 1200 is PER SCENARIO and
            # comes from single_mode_scenario. URA's row is a flat 200 across all
            # five stats -- which is exactly the constant this used to hardcode,
            # so URA careers are unchanged -- but Grand Live's is 400/100/100/
            # 300/100, i.e. the 1600 Speed / 1500 Guts ceiling the scenario is
            # built around. Hardcoding 200 gave a Grand Live trainee URA's caps
            # and silently threw away the whole point of the scenario.
            # Inheritance cap-sparks add a small variable amount on top
            # (see _apply_inheritance).
            ci[f"max_{stat}"] = base_cap + cap_bonus.get(stat, _CAREER_BASE_CAP_BONUS)
            ci[f"default_max_{stat}"] = base_cap
        for apt in _APTITUDE_FIELDS:
            if apt in rd:
                ci[apt] = rd[apt]

    # This uma's goal races (route_id + route_race_id_array) -- otherwise the
    # template's chara-1004 route leaks onto every trainee. Left as the
    # template's value for any uma without a route. AFTER the aptitude block
    # above, not before it: Trackblazer picks its route BY aptitude, so asking
    # while ci still carried the template trainee's aptitudes handed every
    # Trackblazer career chara 1004's route.
    route_id, route_races = _career_route(ci["scenario_id"], (card_id or 0) // 100, ci)
    if route_id is not None:
        ci["route_id"] = route_id
        ci["route_race_id_array"] = route_races

    ci["succession_trained_chara_id_1"] = start_chara.get("succession_trained_chara_id_1", 0) or 0
    ci["succession_trained_chara_id_2"] = start_chara.get("succession_trained_chara_id_2", 0) or 0
    ci["support_card_array"] = _career_support_array(viewer_id, start_chara)

    ci["turn"] = 1
    ci["playing_state"] = 1
    ci["state"] = 0
    ci["vital"] = ci.get("max_vital", 100)
    ci["motivation"] = 3
    ci["fans"] = 1
    ci["skill_point"] = 0
    # Start clean so nothing references a stale skill grid; the trainee's own
    # unique skill and her parents' green-spark hints are layered on below.
    # White (non-unique skill) sparks do NOT apply at career start at all --
    # user-confirmed 2026-08-27: only the two turn 31/55 Inspiration events
    # roll white, never the deterministic pre-turn-1 inheritance (the older
    # version of this comment implied a white-hint stage was still to come
    # here; that was simply wrong, not unfinished).
    ci["skill_array"] = []
    # BUG FIXED 2026-08-27 (user-reported: "This Dance Is for Vittoria!" and
    # "Resplendent Red Ace" showing up on EVERY new career regardless of
    # trainee or parents, "i dont have any parents for them"): skill_tips_
    # array was never reset here, unlike skill_array right above it. The
    # template_ci this whole chara_info is built from (a few lines up) is a
    # REAL captured single_mode/start response from the UmaDumpy dump set --
    # i.e. some OTHER real player's real career -- and its own real hint
    # list was silently surviving unchanged into every career this server
    # ever seeds. _apply_inheritance below only ever ADDS to an existing
    # skill_tips_array (tips.setdefault + per-group max), so it could never
    # have cleared this on its own -- those two specific skills are simply
    # whichever real trainee's captured hints happened to be in that fixture.
    ci["skill_tips_array"] = []
    # CARD-INNATE hints by potential/talent level (user-asked 2026-08-27:
    # "every uma has skill hints ... depending on their potential lvl, are we
    # giving those?" -- no, this was entirely unimplemented before now).
    # master.mdb's available_skill_set (card_data.available_skill_set_id ->
    # available_skill_set: {skill_id, need_rank}) is the SAME table this
    # module already uses for the during-training random hint-reveal pool
    # (see the "Skill pool is sourced from EACH card's own character" -- but
    # that pool ignores need_rank entirely, it's a different mechanic: which
    # skills a card CAN ever reveal, not which ones she already starts with
    # unlocked). need_rank is the talent tier that must be reached for that
    # specific hint to be pre-unlocked at career start -- 0 means always
    # unlocked regardless of talent. Level 1: no real capture pins the exact
    # starting hint level for these, and 1 (freshly unlocked, not yet
    # trained up) is the same conservative default this codebase already
    # uses elsewhere for a just-unlocked hint (see _apply_inheritance).
    # Keyed by (group_id, rarity), not group alone: available_skill_set is 202
    # GOLD skills deep, and a gold skill shares its group with its white
    # group-mate -- filing them all under rarity 1 collapsed each gold innate
    # hint onto the white skill of the pair (master_data.skill_tip_key).
    talent = ci.get("talent_level") or 1
    innate_groups: set[tuple[int, int]] = set()
    for row in master_data.query(
            "SELECT skill_id, need_rank FROM available_skill_set WHERE available_skill_set_id=?",
            (ci.get("card_id"),)):
        if row["need_rank"] <= talent:
            innate_groups.add(master_data.skill_tip_key(row["skill_id"]))
    for gid, rarity in sorted(innate_groups):
        ci["skill_tips_array"].append({"group_id": gid, "rarity": rarity, "level": 1})
    # ...except the trainee's own UNIQUE SKILL, which they own from turn 1 at a
    # level set by their star rating (see _unique_skill_for). A genuinely empty
    # skill_array meant nobody ever had their unique skill at all, so the three
    # level-up events had nothing to raise.
    _grant_unique_skill(ci)
    _init_bonds(ci)
    _apply_initial_stats(ci)                       # deck's åˆæœŸã‚¹ãƒ”ãƒ¼ãƒ‰/â€¦/åˆæœŸè³¢ã• bonuses
    _apply_inheritance(ci, viewer_id, start_chara)  # parents' blue-stat + green-hint sparks
    return ci


# The endpoint family the request in flight arrived on ("single_mode",
# "single_mode_live", ...). main.dispatch sets it before calling the handler.
# Almost nothing needs it -- every scenario posts the same request types to its
# own prefix and the career's own scenario_id answers the question -- but the
# ONE case with no career to ask is handle_load with nothing persisted, and
# there the prefix is the only evidence of which scenario the client is in.
CURRENT_ENDPOINT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "career_endpoint", default="")


def _endpoint_scenario_id():
    """The scenario whose endpoint family the current request came in on, or
    None off a career prefix (or outside a request)."""
    prefix = (CURRENT_ENDPOINT.get() or "").split("/", 1)[0]
    if not prefix:
        return None
    for scen in scenarios.all_scenarios():
        if scen.endpoint_prefix == prefix:
            return scen.scenario_id
    return None


def _force_seeded_scenario(viewer_id, career_state: dict, scenario_id) -> None:
    """Re-stamp a just-seeded career onto `scenario_id` (a no-op when it already
    matches, or when the caller could not work one out). Only the fallback seed
    in handle_load uses this -- a real /start carries the player's own pick."""
    if not scenario_id or not isinstance(career_state, dict):
        return
    data = career_state.get("data")
    if not isinstance(data, dict):
        return
    changed = False
    for holder in (data, data.get("single_mode_start_common"),
                   data.get("single_mode_load_common")):
        if isinstance(holder, dict) and isinstance(holder.get("chara_info"), dict):
            if holder["chara_info"].get("scenario_id") != scenario_id:
                holder["chara_info"]["scenario_id"] = scenario_id
                changed = True
    if not changed:
        return
    # The seed built its intro chain against the scenario it THOUGHT it was in
    # (see _seed_dynamic_career), so a re-stamp has to redo the head event too
    # -- otherwise a Trackblazer career recovered this way opens on 3000
    # "Introducing <trainee>!" instead of 4000 "Self-Introduction".
    intro = (list(scenarios.get(scenario_id).intro_chain)
             or single_mode_events.INTRO_CHAIN)
    if intro:
        ci = data.get("chara_info") or {}
        head = single_mode_events.event_entry(
            intro[0], (ci.get("card_id") or 0) // 100)
        for holder in (data, data.get("single_mode_start_common"),
                       data.get("single_mode_load_common")):
            if isinstance(holder, dict) and holder.get("unchecked_event_array"):
                holder["unchecked_event_array"] = [copy.deepcopy(head)]
    _install_own_data_set(data, data.get("chara_info") or {})
    full_state = state_store.get_state(viewer_id) or {}
    full_state[STATE_KEY] = career_state
    # ...and the queue the client resolves it against, or check_event would
    # answer the head it was just served with the other scenario's id.
    pending = full_state.get(single_mode_events.PENDING_EVENTS_KEY)
    if intro and isinstance(pending, list) and pending:
        full_state[single_mode_events.PENDING_EVENTS_KEY] = list(intro)
    state_store.save_state(viewer_id, full_state)


def _install_own_data_set(career_data: dict, chara_info: dict) -> None:
    """Give a freshly-seeded career the scenario envelope it is entitled to,
    and drop the template's.

    Every /start and /load template we hold is a scenario-2 Unity Cup capture,
    so the persisted career of a URA / Grand Live / Trackblazer run carried
    `team_data_set` and no envelope of its own. That is not only a wire-shape
    problem (see _scrub_foreign_data_sets): the shared code reads the PERSISTED
    copy. _sync_chara_info backfills a missing `ura_data_set` from
    career["data"]["ura_data_set"], handle_ura_check_event's race branch copies
    it over the seed's, and _apply_versus / the NPC-unlock reconcile both write
    into `data["ura_data_set"]` -- all no-ops while the key does not exist, so a
    URA career had no NPC naming table at all and Happy Meek's rows went
    nowhere.

    URA's comes from the real captured URA response in data/seeds/ura_race (the
    only genuine scenario-1 envelope in the corpus); _prune_roster relabels its
    deck rows onto this career's actual deck and drops the capture's un-unlocked
    NPC band, which is the same treatment the race seeds already get. The other
    scenarios build their own in Scenario.attach and need nothing seeded.
    """
    scen = scenarios.for_chara(chara_info)
    keep = scen.data_set_key
    for key in _SCENARIO_DATA_SET_KEYS:
        if key != keep and key in career_data:
            del career_data[key]
    if keep == "ura_data_set" and keep not in career_data:
        try:
            seed = _load_ura_race_seed("train_check_event")["data"]["ura_data_set"]
        except (OSError, KeyError):
            return
        career_data[keep] = copy.deepcopy(seed)


def _seed_fresh_career(viewer_id, start_chara: dict | None) -> dict:
    """Builds a fresh turn-1 career_state and persists it. Shared by
    handle_start and handle_load's no-state fallback -- see handle_load's
    docstring for why the fallback needs this too, not just plain start."""
    pair = fixtures.find("single_mode_team/start", **{"start_chara": start_chara})
    if pair is None:
        pair = fixtures.first("single_mode_team/start")
    if pair is None:
        raise LookupError("No captured single_mode_team/start fixture available")

    career_state = pair.response_copy()
    career_state.setdefault("data", {}).setdefault("race_history", [])
    _install_own_data_set(career_state["data"],
                          career_state["data"].get("chara_info") or {})

    full_state = state_store.get_state(viewer_id) or {}
    full_state[STATE_KEY] = career_state
    # start_chara (the request, not the response) carries the succession
    # parents chosen for this run -- chara_info never gets this itself, but
    # trained_chara.build_trained_chara_from_career needs it at finish time
    # to fill succession_trained_chara_id_1/2 on the new roster entry. Stored
    # as its own full_state key (like ROSTER_KEY/RACE_CTX_KEY), NOT on
    # career_state itself -- handle_load/handle_start return career_state
    # verbatim as the response envelope, so anything added there leaks
    # straight into the wire response as an unexpected field.
    full_state[START_CHARA_STATE_KEY] = start_chara or {}
    state_store.save_state(viewer_id, full_state)
    return career_state


def _seed_dynamic_career(viewer_id, start_chara: dict) -> dict:
    """Builds a fresh turn-1 career from the player's real start_chara
    selections (dynamic), persists it, and returns the /start response.

    Uses the captured /start as a STRUCTURAL template only -- every field that
    doesn't depend on the player's choices (home_info, scenario race/mission
    arrays, envelope shape) is kept as-is, while chara_info is rebuilt for the
    chosen trainee + legacies + deck. The scenario opening event
    (unchecked_event_array) is cleared for now so the run drops cleanly into
    turn 1; the scenario cutscene + first-inspiration event get reconstructed in
    a later stage. The client reads the NESTED single_mode_start_common copy in
    ApplySingleModeStartResponse, so chara_info/unchecked_event_array are written
    to BOTH the top-level and the nested copy."""
    # Prefer the REAL URA single_mode/start template (correct URA commands
    # 301/390/401/701/801 -- no team-only "Rest & Recreation" -- and a FRESH
    # ura_data_set so scenario NPCs aren't shown pre-unlocked). Fall back to the
    # scenario-2 team capture only if the URA one isn't indexed.
    pair = fixtures.first("single_mode/start") or fixtures.first("single_mode_team/start")
    if pair is None:
        raise LookupError("No captured single_mode start fixture available")

    career_state = pair.response_copy()
    data = career_state.get("data", {})

    template_ci = data.get("chara_info", {})
    chara_info = _build_start_chara_info(viewer_id, start_chara, template_ci)

    data["chara_info"] = chara_info
    _install_own_data_set(data, chara_info)
    # race_history (top-level, NOT nested in chara_info) starts genuinely empty
    # for a fresh career -- the /start template doesn't carry this key at all,
    # so without this, sync_chara_info's blanket key sync (which only touches
    # keys present in BOTH the response and the persisted career data) never
    # gets a chance to overwrite /load's own frozen fixture race_history (see
    # _append_race_history for where this gets filled in as real races finish).
    data.setdefault("race_history", [])
    # Career-start intro event chain: character intro -> Tazuna (+120 SP). The
    # client plays the head event then calls check_event, which resolves it and
    # chains to the next (see handle_ura_check_event). The remaining chain is
    # tracked on the career state.
    player_chara = (chara_info.get("card_id") or 0) // 100
    # ...but which beat opens it is the scenario's call: Trackblazer uses the
    # trainee's "Self-Introduction" (4000) where the rest use "Introducing
    # <trainee>!" (3000). An empty hook means "the shared default".
    intro = (list(scenarios.for_chara(chara_info).intro_chain)
             or single_mode_events.INTRO_CHAIN)
    data["unchecked_event_array"] = [single_mode_events.event_entry(intro[0], player_chara)] if intro else []
    # Compute the turn-1 training previews (command_info_array) from the player's
    # real deck so each facility shows the RIGHT stats for THIS run -- the
    # captured array is Maru's scenario-2 values (only its speed entry happens to
    # look right). exec_command applies straight from this array, so what's shown
    # is exactly what's gained.
    home = data.get("home_info")
    if isinstance(home, dict):
        # No training_bonus here by construction: this is turn 1 of a brand new
        # career, so Grand Live's Extra Stat Gain total is necessarily zero (the
        # first song can't arrive before turn 4's unlock event).
        _refresh_command_info(chara_info, home, turn=chara_info.get("turn", 1),
                              facility_levels=_facility_levels(data),
                              race_history=data.get("race_history", []),
                              support_card_levels=_support_card_levels(
                                  state_store.get_state(viewer_id) or {}),
                              full_state=state_store.get_state(viewer_id) or {})
        # No friendship_stacks here: turn 1 of a brand new career necessarily
        # has zero (FRIENDSHIP_STACK_KEY is wiped by clear_active_career).
    smsc = data.get("single_mode_start_common")
    if isinstance(smsc, dict):
        smsc["chara_info"] = copy.deepcopy(chara_info)
        # the nested copy is what ApplySingleModeStartResponse reads -- it must
        # carry the intro event too, or the client never plays it.
        smsc["unchecked_event_array"] = copy.deepcopy(data["unchecked_event_array"])
        if isinstance(smsc.get("home_info"), dict) and isinstance(home, dict):
            smsc["home_info"]["command_info_array"] = copy.deepcopy(home["command_info_array"])

    full_state = state_store.get_state(viewer_id) or {}
    full_state[STATE_KEY] = career_state
    full_state[START_CHARA_STATE_KEY] = start_chara or {}
    # remaining intro chain to resolve via check_event (full chain; the head is
    # already shown in unchecked_event_array). Stored on full_state (not the
    # response data) so it doesn't leak into the wire response.
    full_state[single_mode_events.PENDING_EVENTS_KEY] = list(intro)
    _remember_display(full_state, career_state)
    state_store.save_state(viewer_id, full_state)
    return career_state


def career_state_keys() -> tuple:
    """EVERY per-career state key, in one place. clear_active_career wipes
    them; admin's career-park/-resume moves them between save slots. A key
    added to a new career feature belongs in _CAREER_STATE_KEYS below, or it
    will leak across careers (the FIRED_EVENTS_KEY lesson) and be lost by
    park/resume. Resolved lazily -- many of the keys are defined further down
    the module."""
    return _CAREER_STATE_KEYS()


def clear_active_career(viewer_id) -> list[str]:
    """Wipe any in-progress career for this viewer (career state + the stored
    start_chara). Returns which keys were removed. Used to guarantee a clean
    slate on every start so a broken/half-finished run never blocks the next
    debug attempt, and callable directly for manual resets."""
    full_state = state_store.get_state(viewer_id) or {}
    keys = career_state_keys()
    removed = [k for k in keys if k in full_state]
    for k in removed:
        del full_state[k]
    if removed:
        state_store.save_state(viewer_id, full_state)
    return removed


def _CAREER_STATE_KEYS() -> tuple:
    return (STATE_KEY, START_CHARA_STATE_KEY, RACE_CTX_KEY, STYLE_CHOICE_KEY,
            RACE_REWARD_KEY, APPRAISAL_CTX_KEY, CAREER_CTX_QUEUE_KEY,
            PAL_STATE_KEY, PAL_EVENT_CTX_KEY, PURE_PASSION_KEY,
            GOAL_MARKED_KEY, GOAL_ANNOUNCED_KEY, GOAL_STORIES_FIRED_KEY,
            GOAL_EVENT_KEY, HOT_SPRING_KEY,
            SCENARIO_FIXED_FIRED_KEY,
            UNIQUE_LEVEL_FIRED_KEY, DIRECTOR_FAN_FIRED_KEY,
            EPITHET_RUN_FIRED_KEY,
            SCENARIO_TURNS_FIRED_KEY, SCENARIO_EVENTS_DONE_KEY,
            POST_RACE_FIRED_KEY, RAFFLE_CTX_KEY, INFIRMARY_CTX_KEY,
            CRANE_PLAYED_KEY, ENDING_PAYOUT_KEY, DISPLAY_EVENT_KEY,
            "career_factor_roll",  # or career #2's spark screen re-serves #1's roll
            INSPIRATION_CTX_KEY, CRANE_CTX_KEY,
            # FIRED_EVENTS_KEY was MISSING here -- it accumulated across every
            # career (354 ids by the time it was caught), so career #2+ started
            # with the deck's whole chain pool already 'fired' and the player
            # NEVER saw their own cards' events again. Same for leftover
            # queued/ctx entries below.
            event_engine.FIRED_EVENTS_KEY,
            event_engine.CHAIN_ENDED_KEY,
            event_engine.CAREER_EVENT_CTX_KEY,
            HINT_REVEAL_CTX_KEY, REST_CTX_KEY,
            single_mode_events.FAIL_CTX_KEY,
            CAREER_FAILED_FLAG_KEY,
            single_mode_events.EXTRA_EVENTS_KEY,
            single_mode_events.PENDING_EVENTS_KEY,
            single_mode_events.UNLOCKED_NPCS_KEY,
            single_mode_events.VERSUS_LEVEL_KEY,
            single_mode_events.DUEL_CTX_KEY,
            single_mode_events.DUEL_LAST_TURN_KEY,
            # EVERY scenario's own state -- collected from the registry, not
            # listed here, so a scenario added later cannot be forgotten. Cleared
            # for EVERY start regardless of which scenario is being played:
            # otherwise a Grand Live run's 19 songs would still be sitting there
            # when the next career begins.
            *scenarios.state_keys(),
            GRAND_LIVE_EVENTS_FIRED_KEY,
            # The unified pipeline's ONE key: queue + active + the single
            # fired-set. Replaces (and outlives) the per-family keys above it.
            career_events.STATE_KEY,
            # idle_single_mode's in-flight run marker (ready_at/start_time).
            # String literal, not an import, to avoid idle_single_mode.py <->
            # single_mode_team.py becoming a circular import -- keep this in
            # sync with idle_single_mode.IDLE_RUN_KEY.
            "idle_single_mode_run",
            # deck_num=0 of reserved_race_array -- THIS career's own race
            # schedule. Account-level until now, so the agenda set in one
            # career was still listed in the next one. The "Agenda N" presets
            # (RESERVED_DECKS_KEY) are deliberately NOT here: a saved agenda is
            # a reusable template and is meant to outlive the career.
            RESERVED_RACES_KEY,
            FRIENDSHIP_STACK_KEY, CRANE_SESSION_KEY)


def handle_pre_single_mode_index(payload: dict) -> dict:
    """pre_single_mode/index -- the succession/parent-picker + friend-support
    screen shown right before starting a new career. NEVER had a real
    handler: main.py fixture-replayed ONE frozen real capture verbatim for
    EVERY account (only patched to add default_running_style_array), and
    that capture's own succession_trained_chara_array happened to be EMPTY
    -- so every account, always, saw an empty parent-picker no matter what
    (user-reported 2026-08-18).

    CORRECTED the same day once a real, non-empty capture existed
    (captures/20260818_122351/0025_pre_single_mode_index.json, real
    server, real account): this session's FIRST fix injected the viewer's
    OWN roster into succession_trained_chara_array, which was a wrong
    guess -- the real capture's 70 succession_trained_chara_array entries
    and 70 summary_user_info_array entries belong ENTIRELY to OTHER real
    accounts (verified: zero of the 70 carry the requesting viewer_id;
    they're a public/circle "rental legacy" gallery of top players'
    horses, e.g. real names, real fan counts in the hundreds of millions).
    Same story for event_succession_trained_chara_data (30 entries, same
    pattern) and friend_support_card_data. On a private server with no
    other real players, the honest behavior is the same one user_profile.
    py's friend/index already established: serve these genuinely empty
    rather than fabricate other accounts, or (this session's actual bug)
    put the VIEWER'S OWN horses in a slot meant for borrowed ones.

    Your own roster for succession IS already served correctly elsewhere
    -- trained_chara/load (ENDPOINT_KEYS.md: "Veteran roster"), which the
    career-start flow's OWN "pick from my barn" UI reads from, confirmed
    already returning the real starter set before this session's `pre_
    single_mode/index` work even began.

    scenario_record_highest_score_array, unlike the above, genuinely IS
    real per-account data (real capture: one entry per scenario_id played,
    highest_rank_score per scenario) and IS honestly derivable from state
    this server already tracks truthfully: the max rank_score per
    scenario_id across the genuine (career-graduate) roster -- so it's
    computed for real below instead of left at the fixture's frozen value."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    # inject_friend_support_card does its OWN independent fetch/save (same
    # convention as presents.admin_send) -- re-fetch after it so this
    # function's own full_state isn't a stale pre-injection snapshot that
    # would clobber what it just wrote.
    _ensure_default_friend(viewer_id, full_state)
    full_state = state_store.get_state(viewer_id) or full_state
    pair = fixtures.first("pre_single_mode/index")
    base = pair.response_copy() if pair is not None else {
        "response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
        "data": {}}
    data = base.setdefault("data", {})
    data["default_running_style_array"] = []

    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    # EXPERIMENTAL 2026-08-19: also surfacing injected friends here, not
    # just in friend_support_card_data -- Borrow Card still showed nothing
    # even once friend_support_card_data was a byte-for-byte match (every
    # field, every type, every magnitude) to a PROVEN real working example,
    # which ran out every other explanation this session tried. Untested
    # hypothesis: the borrow pool might need the succession/rental arrays
    # non-empty too, not just friend_support_card_data. If this turns out
    # not to matter, revert to genuinely empty (still the honest default
    # per this project's "no real players to fabricate" rule elsewhere).
    for key in ("succession_trained_chara_data", "event_succession_trained_chara_data"):
        block = data.setdefault(key, {})
        block["succession_trained_chara_array"] = [
            {**trained_chara._load_starter_trained_chara(e["viewer_id"])[0]}
            for e in injected]
        block["summary_user_info_array"] = copy.deepcopy(injected)
    # Borrowable friend support cards are the SAME "no other real players"
    # gap as succession/event_succession above. UNLIKE those, this one is
    # no longer left genuinely empty by default: _ensure_default_friend
    # above seeds the whole _DEFAULT_FRIEND_CARDS roster (Light Hello, Fine
    # Motion, Riko Kashimoto, Kitasan Black) for every account on first
    # access, because a support-deck slot can legitimately REQUIRE a
    # borrowed card to start a career at all -- with no real players to
    # borrow from, an empty list here is a hard blocker, not a cosmetic
    # gap (user-corrected 2026-08-19, after a brand new account still had
    # nothing to borrow: "make sure that light hello friend is available
    # to every single account on the server, otherwise they cant start
    # careers"). admin.py's `add-friend-card` still works too, for adding
    # MORE lenders beyond the guaranteed defaults.
    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    fsc = data.setdefault("friend_support_card_data", {})
    fsc["summary_user_info_array"] = copy.deepcopy(injected)
    fsc["support_card_data_array"] = [copy.deepcopy(e["user_support_card"]) for e in injected]

    genuine = [c for c in trained_chara._get_or_seed_roster(viewer_id)
              if trained_chara.PLAYER_CAREER_ID_BASE
                 <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]
    best_by_scenario: dict = {}
    for c in genuine:
        sid = c.get("scenario_id")
        score = c.get("rank_score") or 0
        if sid and score > best_by_scenario.get(sid, 0):
            best_by_scenario[sid] = score
    data["scenario_record_highest_score_array"] = [
        {"scenario_id": sid, "highest_rank_score": score}
        for sid, score in sorted(best_by_scenario.items())]
    if _grant_scenario_record_rewards(full_state, best_by_scenario):
        state_store.save_state(viewer_id, full_state)
    return base


def handle_friend_support_card_reload(payload: dict) -> dict:
    """pre_single_mode/friend_support_card_reload {exclude_viewer_id_array} ->
    {summary_user_info_array, support_card_data_array}.

    The Borrow Card screen's REROLL: the player asks for a different set of
    lenders, naming the ones already on screen so they are not offered again.

    THE MOST-HIT UNHANDLED ENDPOINT ON THIS SERVER -- 33 no-op fallbacks in
    server.log before this existed. Both fields are arrays, so the no-op
    (`data: {}`) handed the career-setup screen two nulls on the one screen that
    can hard-block starting a career at all.

    Serves the same lender pool pre_single_mode/index does -- the
    FRIEND_SUPPORT_INJECT_KEY roster that _ensure_default_friend seeds for every
    account -- minus whoever the client says it is already showing.

    WHEN THE EXCLUSION EMPTIES THE POOL, the full pool is served again rather
    than an empty one. That is not a fudge: a reroll on a server with no other
    real players genuinely has nobody else to offer, and "the same lenders
    again" is the truthful answer, where an empty list is a career the player
    cannot start. It is the same call _ensure_default_friend already makes and
    for the same live-reported reason ("make sure that light hello friend is
    available to every single account on the server, otherwise they cant start
    careers")."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    _ensure_default_friend(viewer_id, full_state)
    full_state = state_store.get_state(viewer_id) or full_state

    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    excluded = {str(v) for v in (payload.get("exclude_viewer_id_array") or [])}
    remaining = [e for e in injected if str(e.get("viewer_id")) not in excluded]
    if not remaining and injected:
        log.info("friend_support_card_reload: every lender (%s) already shown -- "
                 "re-offering the whole pool rather than an empty Borrow screen",
                 len(injected))
        remaining = injected

    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {
                "summary_user_info_array": copy.deepcopy(remaining),
                "support_card_data_array": [copy.deepcopy(e["user_support_card"])
                                            for e in remaining
                                            if e.get("user_support_card")],
            }}


SCENARIO_RECORD_GRANTED_KEY = "scenario_record_granted"


def _grant_scenario_record_rewards(full_state: dict, best_by_scenario: dict) -> bool:
    """Mail every single_mode_scenario_record tier the account's best rank
    score has newly reached. Returns True if anything was mailed.

    ADDED 2026-09-03 (live-reported: scenario record rewards displayed as
    obtained but never sent). The client marks a tier obtained purely from
    scenario_record_highest_score_array -- which this server computes honestly
    -- but nothing ever paid the tier out. Tiers are cumulative and one-time,
    tracked by a per-scenario watermark of the highest need_record_min paid.

    NB presents.send, not admin_send: the caller owns full_state and saves it."""
    granted = full_state.setdefault(SCENARIO_RECORD_GRANTED_KEY, {})
    mailed = False
    for scenario_id, best in best_by_scenario.items():
        watermark = granted.get(str(scenario_id)) or 0
        rows = master_data.query(
            "SELECT need_record_min, reward_item_category, reward_item_id, reward_num "
            "FROM single_mode_scenario_record WHERE scenario_id=? AND need_record_min<=? "
            "AND need_record_min>? ORDER BY need_record_min",
            (scenario_id, best, watermark))
        for row in rows:
            if row["reward_num"]:
                presents.send(full_state, row["reward_item_category"],
                              row["reward_item_id"], row["reward_num"],
                              message=f"Scenario Record {row['need_record_min']} reward")
                mailed = True
            granted[str(scenario_id)] = row["need_record_min"]
    return mailed


# UserInfoAtFriend's real shape (dump.cs's own reflection is stale here --
# missing circle_info/circle_user/friend_state, all present in every real
# capture) confirmed against captures/20260818_122351/0025_pre_single_mode_
# index.json: one real lender entry there happened to ALREADY be offering
# support_card_id 30052 (SSR "Light Hello", rarity 3) at exp 118185/
# limit_break_count 4 -- exactly SSR-MLB-level-50, verified against master.
# mdb's support_card_level (rarity 3, level 50 -> total_exp 118185) and
# support_card_data (id 30052 -> rarity 3). That real entry is the literal
# template below for user_trained_chara's shape (anonymous gameplay stats,
# not identifying data) -- but NOT for the lending player's own identity
# (name/circle/etc, a real specific person's real username), which is
# replaced with neutral placeholders rather than reused.
_FRIEND_TEMPLATE_TRAINED_CHARA = {
    "card_id": 103201, "rank_score": 15351, "rank": 15,
    "proper_ground_turf": 7, "proper_ground_dirt": 2,
    "proper_running_style_nige": 3, "proper_running_style_senko": 7,
    "proper_running_style_sashi": 7, "proper_running_style_oikomi": 2,
    "proper_distance_short": 1, "proper_distance_mile": 4,
    "proper_distance_middle": 7, "proper_distance_long": 6,
    "rarity": 4, "talent_level": 5, "factor_info_array": [], "factor_extend_array": [],
    "skill_count": 23,
}
_FRIEND_SENTINEL_VIEWER_BASE = 900_000_500_000  # far outside any real/generated
                                                 # viewer_id range this server mints
_FRIEND_CIRCLE_ID = 900_000_500          # int32-safe (real circle_ids observed:
                                          # 379955687, 274238899, 114615922 --
                                          # all well under 2**31); do NOT reuse
                                          # the viewer_id sentinel's magnitude here

_DEFAULT_FRIEND_CARD_ID = 30052          # SSR "Light Hello" (pal)
_DEFAULT_FRIEND_LEVEL = 50
_DEFAULT_FRIEND_LIMIT_BREAK = 4          # MLB

# The full default lending roster: Light Hello (pal) plus MLB SSR trainer-
# and group-type cards, all real support_card_data rows (master.mdb-verified
# -- id/chara_id/rarity/command_type/support_card_type checked directly, not
# guessed from name): 30010 Fine Motion (wisdom/Wit), 30036 Riko Kashimoto
# (pal), 30028 Kitasan Black (speed), 30067 (GROUP card whose text_data name
# is "Heirs to the Throne"), 30081 (GROUP card whose text_data name is "Team
# Sirius" -- note pal_cards.PURE_PASSION keys these by id, not by which Pure
# Passion condition their own display name matches: 30067 -> condition 101,
# 30081 -> condition 102, capture-confirmed, do not "fix" by re-pairing on
# name). Same rarity (SSR) as Light Hello so _DEFAULT_FRIEND_LEVEL 50 is each
# one's real max level too.
_DEFAULT_FRIEND_CARDS = [
    (_DEFAULT_FRIEND_CARD_ID, "Light Hello"),
    (30010, "Fine Motion"),
    (30036, "Riko Kashimoto"),
    (30028, "Kitasan Black"),
    (30067, "Heirs to the Throne"),
    (30081, "Team Sirius"),
]


def _ensure_default_friend(viewer_id, full_state: dict) -> None:
    """Auto-seed the SAME Light Hello lender inject_friend_support_card's
    docstring describes, for EVERY account, not just ones an admin manually
    ran `add-friend-card` for. User-corrected 2026-08-19: a brand new
    account still had nothing to borrow -- "make sure that light hello
    friend is available to every single account on the server, otherwise
    they cant start careers". This overrides this module's own earlier
    "explicit opt-in, never fabricated automatically" stance on friend
    injection (see inject_friend_support_card's docstring) -- deliberately,
    per that direct instruction: a scenario whose deck REQUIRES a borrowed
    card, on a server with no other real players to borrow from, makes that
    system a hard blocker rather than a cosmetic gap once nobody has
    anything to lend.

    Extended 2026-09-08 (user request) to seed the whole _DEFAULT_FRIEND_CARDS
    roster instead of just Light Hello, so every account has an MLB card of
    every training type a deck slot might require, not only the pal type.
    Per-card presence check (by support_card_id), NOT "does this account have
    ANY injected friend yet" -- so accounts already seeded under the old
    single-card default (or ones an admin hand-picked cards for) still pick up
    exactly the missing defaults on their next call here, instead of being
    permanently skipped past for having a nonempty list already. Never
    duplicates: a default already present (self-seeded earlier, or an admin's
    own `add-friend-card` pick with the same id) is left alone."""
    injected = full_state.get(FRIEND_SUPPORT_INJECT_KEY) or []
    have = {e.get("support_card_id") for e in injected}
    for card_id, name in _DEFAULT_FRIEND_CARDS:
        if card_id in have:
            continue
        inject_friend_support_card(viewer_id, card_id,
                                   level=_DEFAULT_FRIEND_LEVEL,
                                   limit_break_count=_DEFAULT_FRIEND_LIMIT_BREAK,
                                   name=name)


def inject_friend_support_card(viewer_id, support_card_id: int, level: int = None,
                               limit_break_count: int = None, name: str = "Friend") -> dict:
    """admin.py's `add-friend-card` -- appends one synthetic, always-
    borrowable "friend" lending a specific support card, for the "Borrow
    Card" screen when a support-deck slot requires one and there are no
    real friends to borrow from (user-reported 2026-08-18: "There are no
    Support Cards to borrow" blocking career start entirely). Explicit
    opt-in only -- see handle_pre_single_mode_index's docstring for why
    this isn't done automatically.

    level defaults to that card's own max (support_card_level's highest row
    for its rarity); limit_break_count defaults to 4 (max break -- every
    rarity's real cap, confirmed against the SSR template above)."""
    row = master_data.query_one("SELECT rarity FROM support_card_data WHERE id=?",
                                (support_card_id,))
    if row is None:
        raise ValueError(f"no support_card_data row for id {support_card_id}")
    rarity = row["rarity"]
    if level is None:
        top = master_data.query_one(
            "SELECT MAX(level) AS lv FROM support_card_level WHERE rarity=?", (rarity,))
        level = (top["lv"] if top else None) or 1
    exp_row = master_data.query_one(
        "SELECT total_exp FROM support_card_level WHERE rarity=? AND level=?",
        (rarity, level))
    if exp_row is None:
        raise ValueError(f"no support_card_level row for rarity {rarity} level {level}")
    limit_break_count = 4 if limit_break_count is None else limit_break_count

    # 0-as-placeholder is NOT safe for chara_id/honor_id/trained_chara_id --
    # same lesson as the Veteran Roster bug (owner_viewer_id, then trained_
    # chara_id magnitude): a value that LOOKS like a harmless default but
    # doesn't resolve to a real master.mdb row (or, for trained_chara_id,
    # isn't in the small magnitude a real client has ever been handed --
    # see trained_chara.py's STARTER_ROSTER_SIZE) silently breaks
    # client-side rendering instead of erroring. User-reported 2026-08-18:
    # the first version of this (leader_chara_id/honor_id/partner_chara_id
    # all 0, trained_chara_id in the hundreds of billions from a viewer_id
    # modulo) still showed nothing. Fixed with values that all resolve for
    # real: honor_id 100101 (every real account's own default epithet, per
    # user_profile.py), leader/partner_chara_id derived from the SAME
    # template card's own chara_id (1032, self-consistent -- "friend" is
    # shown partnered with the character whose card they're lending),
    # leader_chara_dress_id a real dress_data row for that chara, and
    # trained_chara_id a small sequential id in the low thousands (matching
    # the magnitude real accounts actually use, not viewer_id math).
    _TEMPLATE_CHARA_ID = 1032
    _TEMPLATE_DRESS_ID = 103201
    _FRIEND_TRAINED_CHARA_ID_BASE = 9001

    full_state = state_store.get_state(viewer_id) or {}
    injected = full_state.setdefault(FRIEND_SUPPORT_INJECT_KEY, [])
    friend_viewer_id = _FRIEND_SENTINEL_VIEWER_BASE + len(injected)
    friend_trained_chara_id = _FRIEND_TRAINED_CHARA_ID_BASE + len(injected)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "viewer_id": friend_viewer_id, "name": name,
        "honor_id": 100101, "honor_data": {"honor_id": 100101},
        "last_login_time": now,
        "leader_chara_id": _TEMPLATE_CHARA_ID, "leader_chara_dress_id": _TEMPLATE_DRESS_ID,
        "support_card_id": support_card_id, "partner_chara_id": _TEMPLATE_CHARA_ID,
        "comment": "", "fan": 1, "directory_level": 1, "rank_score": 1,
        "team_stadium_win_count": 0, "single_mode_play_count": 1,
        "team_evaluation_point": 0, "best_team_evaluation_point": 0,
        # friend_state=1 (Follow, one-way) is CONFIRMED correct, not a
        # guess: captures/20260818_122351/0025_pre_single_mode_index.json's
        # summary_user_info_array entry for viewer_id 419118335502 is the
        # ACTUAL friend+card (support_card_id 30010) this exact account
        # then used to successfully start a real career (confirmed via
        # 0028_idle_single_mode_start.json's request.single_mode_start_
        # request_common.start_chara.friend_support_card_info) -- i.e. a
        # PROVEN-WORKING real example, not an inference.
        #
        # circle_info/circle_user POPULATED (not null) is the one real
        # content difference that example showed. directory_level/best_
        # team_evaluation_point/top-level "state" were REMOVED in an
        # earlier pass of this fix because that real capture's JSON didn't
        # show them -- WRONG conclusion: their absence from the JSON dump
        # is more likely the capture tool dropping zero/null fields for
        # compactness than genuine absence from the wire. Removing them
        # produced "Could not receive parameters from server" entering
        # scenario select (2026-08-19 live report) -- this project's own
        # documented signature for a response missing a field the client's
        # formatter declares (see main.py's _ADDED_RESPONSE_FIELDS
        # comment). Restored -- keep every field ever confirmed present
        # (by either a real capture OR dump.cs) rather than dropping one
        # on absence-of-evidence from a single JSON dump.
        "friend_state": 1, "state": 1,
        # circle_id is int32-shaped in every real example (379955687,
        # 274238899, 114615922 -- all comfortably under 2^31), NOT
        # viewer_id-shaped (12-digit, far past int32). Reusing the
        # viewer_id sentinel scheme for it here was the actual cause of
        # "Could not receive parameters from server" -- a value that
        # overflows the field's real (narrower) integer type on the
        # client side, the same class of bug as trained_chara_id's
        # magnitude in the Veteran Roster fix, just for a different field.
        "circle_info": {"circle_id": _FRIEND_CIRCLE_ID, "name": "Friends"},
        "circle_user": {"viewer_id": friend_viewer_id, "circle_id": _FRIEND_CIRCLE_ID,
                        "membership": 1, "join_time": now, "penalty_end_time": now,
                        "item_request_end_time": now, "last_check_post_id": 0,
                        "ranking_result_check_time": now},
        # viewer_id/favorite_flag/stock/possess_time/create_time: found
        # 2026-08-19 by diffing against the friend_support_card_data.
        # support_card_data_array SIBLING of summary_user_info_array in the
        # same proven-real capture (0025_pre_single_mode_index.json) -- a
        # field this project's earlier diffs never checked because they
        # only compared summary_user_info_array entries against each other.
        # Real entries there carry their OWN viewer_id (matching the owning
        # friend's), which the client almost certainly uses to JOIN the two
        # arrays; ours never had it (or the other 4 fields) at all, so the
        # client had no support card data to attach to the friend it did
        # show -- the actual cause of "Trainer Info works, Borrow Card still
        # empty" surviving every other fix this session.
        "user_support_card": {"viewer_id": friend_viewer_id, "support_card_id": support_card_id,
                              "exp": exp_row["total_exp"], "limit_break_count": limit_break_count,
                              "favorite_flag": 0, "stock": 0,
                              "possess_time": now, "create_time": now},
        "user_trained_chara": {**_FRIEND_TEMPLATE_TRAINED_CHARA,
                               "viewer_id": friend_viewer_id,
                               "trained_chara_id": friend_trained_chara_id,
                               "register_time": now},
    }
    injected.append(entry)
    state_store.save_state(viewer_id, full_state)
    return entry


def handle_start(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}

    # BUG FIXED 2026-08-20 (live-reported: "During a career, sometimes before
    # the debut race, it seems like my state completely changes as I gain and
    # lose a absurd amount of stuff, and my character changes, this then
    # softlocks me"). This used to unconditionally clear_active_career and
    # reseed a brand-new turn-1 career on EVERY call ("Always start from a
    # clean slate... you never resume stale state by accident") -- a debug
    # convenience from early iteration that never accounted for the real
    # client calling single_mode/start a SECOND time over an already-active
    # career (a network retry / double-submit is exactly the kind of thing
    # most likely near the pre-debut flow). Each such call nuked genuine
    # in-progress state and reseeded from scratch with a fresh random/
    # reselected trainee: precisely "gain and lose an absurd amount of
    # stuff" + "my character changes", then a softlock once the client's own
    # already-loaded view of the OLD career desyncs from this brand-new one.
    # Career-finish (delete/give-up/complete, below) already calls
    # clear_active_career explicitly, so by the time a player legitimately
    # wants a NEW career, nothing is left here to protect -- this guard only
    # ever intercepts a redundant start over a still-active one. Mirrors
    # handle_load's own already-established rule: seed fresh only if nothing
    # is persisted yet, otherwise hand back what's already there.
    #
    # REFINED 2026-08-20 (live-reported: an alt-form Smart Falcon (104602)
    # start landed the client straight on turn 12's debut race with a wall of
    # absurd stat/friendship deltas on the turn-1 "Introducing..." card, then
    # softlocked on "Before the Debut"). The blanket version above returns
    # whatever's persisted NO MATTER what the client just asked to start --
    # so a genuinely stale, never-cleared career (this account had a real
    # turn-12 Grand Live run sitting in state from an earlier session) gets
    # served back verbatim to a client that just sent a BRAND NEW start_chara
    # selection and is about to render ITS OWN turn-1 intro on top of it: the
    # "wild" numbers were turn 1..12's real accumulated totals, all attributed
    # to the intro card at once, and every event after that resolves against
    # the wrong turn. Only treat this as the redundant-retry case the guard
    # was built for when the incoming selection actually MATCHES what's
    # persisted (same trainee card at minimum -- the cheapest, least
    # ambiguous signal); a different card_id means the player started a real
    # new run and the stale one must be cleared first, same as delete/give-up
    # already does for a deliberately-finished career.
    start_chara = payload.get("start_chara")
    if STATE_KEY in full_state:
        persisted_start = full_state.get(START_CHARA_STATE_KEY) or {}
        same_selection = (not start_chara) or (
            start_chara.get("card_id") == persisted_start.get("card_id"))
        if same_selection:
            return full_state[STATE_KEY]
        log.info("single_mode/start: viewer %s sent a new start_chara (card %s) "
                 "over a stale persisted career (card %s, turn %s) -- clearing "
                 "and reseeding instead of serving the stale one back",
                 viewer_id, start_chara.get("card_id"),
                 persisted_start.get("card_id"),
                 full_state[STATE_KEY].get("data", {}).get("chara_info", {}).get("turn"))
        clear_active_career(viewer_id)
        full_state = state_store.get_state(viewer_id) or {}

    # A real start always carries the player's trainee/legacy/deck selection;
    # build the career from it. (The only caller without one is handle_load's
    # no-state fallback, which uses _seed_fresh_career instead.)
    if start_chara and start_chara.get("card_id"):
        # REJECT rather than silently proceed if a chosen parent doesn't
        # resolve in THIS viewer's own roster (user-confirmed 2026-08-27,
        # after the client's own local cache -- stale from an earlier real-
        # server session today -- submitted two real-account trained_chara
        # ids our private server has never seen). Silently continuing gave a
        # career with ZERO inheritance from that parent while the client's
        # own "Spark of Inspiration" text kept showing numbers computed
        # against data the server never received -- display and reality
        # permanently disagreeing for that whole career, not just one event.
        roster = _roster_by_trained_id(viewer_id)
        for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
            pid = start_chara.get(key)
            if pid and pid not in roster:
                log.warning("single_mode/start refused: viewer %s selected parent "
                           "%s=%s, not in this account's roster (stale client cache?)",
                           viewer_id, key, pid)
                return {"response_code": 1,
                       "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
        banned = _banned_deck_cards(start_chara)
        if banned:
            # DECK RULES ARE MASTER DATA (single_mode_restrict_support). The
            # client enforces them too, so reaching here means a stale client
            # cache or a hand-made request -- refuse rather than start a career
            # the real game would never allow.
            log.warning("single_mode/start refused: viewer %s brought %s into "
                        "scenario %s, barred by single_mode_restrict_support",
                        viewer_id, sorted(banned), start_chara.get("scenario_id"))
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}},
                    "data": {}}
        # TP IS CHARGED HERE, last, once the start is certain to go ahead --
        # after every refusal above, and before the career is seeded, so a
        # refused start never bills and a billed start never fails to build.
        #
        # Gated on the request actually CARRYING use_tp: every one of the 22
        # real start captures does, and the one internal caller that does not
        # is idle_single_mode/start, which reaches this function to build its
        # turn-1 trainee and does its own TP spend around that call. Without
        # the gate an auto-career would pay twice.
        #
        # The amount is recomputed server-side (campaign + boost multipliers)
        # rather than taken from the request -- see stamina.career_tp_cost.
        if "use_tp" in payload:
            full_state = state_store.get_state(viewer_id) or {}
            if not stamina.spend_career_tp(viewer_id, full_state, start_chara,
                                           payload.get("use_tp")):
                # The client checks TP before offering the button, so this is
                # a desynced/hand-made request. 205 leaves it on the setup
                # screen with a plain error instead of starting a free run.
                return {"response_code": 1,
                        "data_headers": {"result_code": 205, "notifications": {}},
                        "data": {}}
            state_store.save_state(viewer_id, full_state)
        return _seed_dynamic_career(viewer_id, start_chara)
    return _seed_fresh_career(viewer_id, start_chara)


def _banned_deck_cards(start_chara: dict) -> set:
    """The chosen deck's cards this scenario bars, if any.

    Covers the borrowed card as well as the player's own five: it occupies a
    deck slot like any other, and a rule that let a banned card in through the
    friend slot would not be a rule."""
    if not isinstance(start_chara, dict):
        return set()
    barred = scenarios.restricted_support_cards(start_chara.get("scenario_id"))
    if not barred:
        return set()
    chosen = {int(c) for c in (start_chara.get("support_card_ids") or ()) if c}
    friend = (start_chara.get("friend_support_card_info") or {}).get("support_card_id")
    if friend:
        chosen.add(int(friend))
    return chosen & set(barred)


def sync_chara_info(viewer_id, endpoint: str, response: dict) -> dict:
    """Dispatcher entry point (main.py applies this to EVERY response).

    Runs the sync below, then prunes the roster LAST. Order matters and cost a
    live round to learn: pruning first is undone, because the sync body then
    replaces data["chara_info"] wholesale from the persisted career -- roster
    rows and all. Doing it here also covers the endpoints the body returns
    early for (check_event, race_entry, race_start/end/out, minigame_end,
    continue), which are exactly the ones that leaked."""
    response = _sync_chara_info(viewer_id, endpoint, response)
    if isinstance(endpoint, str) and endpoint.startswith(_career_prefixes()):
        try:
            _prune_roster(viewer_id, response)
        except Exception:
            log.exception("roster prune failed; serving unpruned")
        # ...then put back the scenario's own STILL-LOCKED NPC placeholders,
        # which the prune has no way to keep (they are not unlocked, and not
        # deck slots) and which the client needs in order to SEE an unlock at
        # all. Serve-only and after the prune, deliberately -- see
        # _carry_locked_npc_rows.
        try:
            _carry_locked_npc_rows(response)
        except Exception:
            log.exception("locked NPC row carry failed; serving without them")
        # THE SCENARIO ENVELOPE. The one place a response is rewritten into the
        # scenario's own shape (Grand Live swaps URA's ura_data_set for its
        # live_data_set). Doing it HERE, after the sync, means no handler has to
        # know which scenarios exist -- and base.Scenario.attach is a no-op, so
        # a URA response leaves this function exactly as it did before.
        try:
            _attach_scenario_data_set(viewer_id, response, endpoint)
        except Exception:
            log.exception("scenario data_set attach failed; serving without it")
        _warn_foreign_chara(viewer_id, endpoint, response)
        # THE RESPONSE KIND OWNS THE TIMING. Last, so it also covers whatever
        # the scenario attach above added. See career_events for the corpus
        # measurement and for why only the three race endpoints are stamped.
        try:
            career_events.stamp_response_timing(
                (response or {}).get("data"), endpoint)
        except Exception:
            log.exception("response timing stamp failed; serving unstamped")
        # Same chokepoint, same reason: is_effected_multi_chara is decided by
        # the scenario family, not by whichever builder made the entry.
        try:
            career_events.stamp_multi_chara_flag(
                (response or {}).get("data"), endpoint)
        except Exception:
            log.exception("multi-chara flag stamp failed; serving unstamped")
        try:
            _trim_start_envelope(endpoint, response)
        except Exception:
            log.exception("start envelope trim failed; serving untrimmed")
        try:
            _trim_opponent_envelope(endpoint, response)
        except Exception:
            log.exception("opponent envelope trim failed; serving untrimmed")
    return response


def _carry_locked_npc_rows(response: dict) -> None:
    """Carry this scenario's yet-to-unlock NPCs as is_appear:0 placeholder rows
    on every served chara_info.

    THE CLIENT ANNOUNCES AN UNLOCK BY DIFFING evaluation_info_array BETWEEN
    RESPONSES. "X will now appear in training" is the 0 -> 1 transition of that
    NPC's is_appear -- so a row that only comes into existence ON the unlock
    response, already at 1, produces no transition and no message. The NPC is
    unlocked, is placed in training from then on, and the player is never told.

    That was Grand Live's Director Akikawa (event 202002, the same beat that
    reveals Light Hello) and its Reporter (101007, "A Quirky Correspondent?"),
    both user-reported as silently unlocking. Real Grand Live carries the whole
    101-106 band minus 105 from the first response of the run -- capture
    20260907_204921, rows [(101,0),(102,0),(103,0),(104,0),(106,0)] from 0004
    onward, 102 flipping at 0046 and 103 at 0084.

    SERVE-ONLY, AND AFTER _prune_roster. These rows are neither deck slots nor
    unlocked NPCs, so the prune would drop every one of them; and they are not
    persisted, because the persisted career gains the row at unlock time
    (_register_npc_unlocks / _ensure_npc_eval) and that row -- already at
    is_appear 1 -- is what this function then leaves alone. Existing rows are
    never touched, so an NPC already unlocked keeps her 1."""
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    chara_info = data.get("chara_info")
    if not isinstance(chara_info, dict):
        return
    rows = scenarios.for_chara(chara_info).locked_npc_rows
    if not rows:
        return
    arr = chara_info.setdefault("evaluation_info_array", [])
    present = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for target_id, _chara_id in rows:
        if target_id in present:
            continue
        arr.append({"target_id": target_id, "training_partner_id": target_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 0, "group_outing_info_array": []})


def _trim_opponent_envelope(endpoint: str, response: dict) -> None:
    """Drop chara_info from an opponent_list response, once the scenario
    attach above has finished with it.

    A real single_mode_team/opponent_list serves exactly one field --
    team_data_set (20/20 captures) -- but the attach step reads the career's
    scenario off data["chara_info"], so the field has to survive until then and
    can only be removed here, at the end of the pipeline. Same shape, and the
    same reason, as _trim_start_envelope. See unity_cup.endpoints._ENDPOINT_FIELDS."""
    if not isinstance(endpoint, str) or not endpoint.endswith("/opponent_list"):
        return
    data = (response or {}).get("data")
    if isinstance(data, dict) and "team_data_set" in data:
        # Only once the data_set actually made it on: if attach did not run,
        # serving a bare {} is what softlocked the race screen in the first
        # place, and chara_info alone is strictly better than nothing.
        data.pop("chara_info", None)


# Every top-level key a /start response used to duplicate. The real wire shape
# carries these ONCE, nested under single_mode_start_common -- confirmed across
# every real start capture in the corpus (single_mode_team/start,
# single_mode_free/start, single_mode_live/start): the top level holds exactly
# single_mode_start_common plus the scenario's own data_set, nothing else.
_START_NESTED_ONLY_FIELDS = (
    "chara_info", "home_info", "tp_info", "unchecked_event_array",
    "race_condition_array", "user_item_array", "add_trained_chara_array",
    "mission_list", "story_event_chara_bonus_list", "story_event_mission_list",
    "race_history", "race_random_program_array", "reserved_race_array",
    "facility_levels",
)


def _trim_start_envelope(endpoint: str, response: dict) -> None:
    """Fold a /start response's duplicated top-level fields into its nested
    single_mode_start_common and drop them.

    The object handle_start persists doubles as the wire response for a repeat
    start call, and the rest of this module reads its TOP-LEVEL keys as the
    career's own record -- so both shapes lived on one dict and the client got
    a second copy of every field. Trimming here, at the very end of the
    response pipeline, keeps the persisted record intact (this only ever
    touches the outgoing copy) while putting the wire back on the real shape.

    The nested copy is refreshed from the top-level values first: those are the
    ones _sync_chara_info has just patched to the live career state, and
    ApplySingleModeStartResponse reads only the nested ones."""
    if not isinstance(endpoint, str) or not endpoint.endswith("/start"):
        return
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    smsc = data.get("single_mode_start_common")
    if not isinstance(smsc, dict):
        return
    for key in _START_NESTED_ONLY_FIELDS:
        if key in data:
            if key in smsc:
                smsc[key] = data[key]
            data.pop(key, None)


def _warn_foreign_chara(viewer_id, endpoint: str, response: dict) -> None:
    """Tripwire: shout if a career response is about to serve a DIFFERENT
    trainee than the persisted run.

    The endpoints _sync_chara_info returns early for (check_event, the race
    flow, minigame_end, continue) each build their own chara_info off a
    captured Maruzensky seed, so a missed patch there ships her career to the
    client, which banks it -- stats, aptitudes and support bonds all replaced
    mid-run (live-reported 2026-09-05 at a debut). Nothing downstream notices,
    which is why it reached a player. Read-only and best-effort: it never
    changes a response, only names the endpoint that leaked."""
    try:
        card = ((response or {}).get("data") or {}).get("chara_info", {}).get("card_id")
        career = (state_store.get_state(viewer_id) or {}).get(STATE_KEY)
        if not card or not isinstance(career, dict):
            return
        mine = (career.get("data") or {}).get("chara_info", {}).get("card_id")
        if mine and card != mine:
            log.error("FOREIGN CAREER on %s: serving card_id %s but this viewer's "
                      "career is card_id %s -- unpatched capture leaking through",
                      endpoint, card, mine)
    except Exception:
        log.exception("foreign-chara tripwire failed")


# Every career endpoint family. A scenario posts to its own prefix but shares
# these handlers (see main.HANDLERS), so it must share the response
# post-processing too -- a family missing from here silently skips the roster
# prune AND the scenario data_set attach for every one of that scenario's
# responses. This used to be a hand-kept tuple, which is exactly the shape that
# forgets: it went a whole scenario without single_mode_live/ in it. Derived
# from the registry now, so adding a scenario adds nothing here.
# single_mode_team/ is not a scenario prefix -- it is the legacy name the URA
# handlers were captured under -- so it is named explicitly.
# LAZY, and it has to be: computing it at import time would make this module's
# own import pull the whole scenario registry in, and a scenario package that
# reaches back into handlers/ would close the cycle.
@functools.lru_cache(maxsize=1)
def _career_prefixes() -> tuple:
    return tuple(sorted({"single_mode/", "single_mode_team/"} | {
        f"{scen.endpoint_prefix}/" for scen in scenarios.all_scenarios()}))


# The four scenario envelopes. A career response carries exactly ONE of these --
# whichever its Scenario.data_set_key names -- never two.
_SCENARIO_DATA_SET_KEYS = ("ura_data_set", "team_data_set", "live_data_set",
                           "free_data_set")
# The nested copies the client actually reads on start/load (see the
# single_mode_load_common note in handle_load): a data_set scrubbed only at the
# top level survives here and is still what gets applied.
_NESTED_COMMON_KEYS = ("single_mode_start_common", "single_mode_load_common")


def _scrub_foreign_data_sets(data: dict, keep: str) -> None:
    """Drop every scenario envelope except this career's own, top level and
    nested. No-op when the response carries none."""
    for holder in (data, *(data.get(k) for k in _NESTED_COMMON_KEYS)):
        if not isinstance(holder, dict):
            continue
        for key in _SCENARIO_DATA_SET_KEYS:
            if key != keep and key in holder:
                del holder[key]


def _attach_scenario_data_set(viewer_id, response: dict, endpoint: str = "") -> None:
    """Rewrite a served career response into its scenario's envelope, with the
    per-facility preview matching the training screen the same response shows.

    Generic by construction: it asks the registry which scenario this career is
    and calls hooks. Every hook is a no-op on base.Scenario, so a scenario that
    serves URA's envelope unchanged costs nothing here and needs no branch."""
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return
    chara_info = data.get("chara_info")
    scen = scenarios.for_chara(chara_info)
    full_state = state_store.get_state(viewer_id) or {}
    # Prefer the response's own home_info, but fall back to the persisted
    # career's: Grand Live's live_start serves a reduced envelope carrying none,
    # and rolling a preview against an EMPTY facility layout would show (and
    # then bank) a supportless gain.
    home = data.get("home_info")
    if not isinstance(home, dict):
        home = ((full_state.get(STATE_KEY) or {}).get("data") or {}).get("home_info") or {}
    # DEFERRED PAY-OUTS first, so the envelope below is built from the settled
    # state rather than from the state that still owes it. See Scenario.settle.
    settled = scen.settle(full_state, chara_info, endpoint)
    command_info, rolled = scen.command_info(full_state, chara_info,
                                             home.get("command_info_array"))
    scen.attach(response, full_state, chara_info, command_info, endpoint=endpoint)
    # ...AND NOTHING ELSE'S. Every start/load/exec_command template we own is a
    # scenario-2 capture, so `team_data_set` rode along on URA, Grand Live and
    # Trackblazer responses too -- a scenario_id 1 career whose wire says "team
    # mode" renders the team facility grid instead of URA's Race Day screen.
    # handle_exec_command already deleted it by hand for exactly this reason;
    # doing it here covers /load and /start as well, and covers all four
    # scenarios instead of just the one field, because every Scenario declares
    # the only data_set key it is allowed to serve.
    _scrub_foreign_data_sets(data, scen.data_set_key)
    # THE HELD TURN. A scenario that keeps the client on a turn across a cutscene
    # chain reports it here (Grand Live holds the concert's turn from "Concert
    # Begins" until the chain empties -- capture: 24 on messages 97-100, 25 on
    # 101). Served, not persisted: the run's own turn keeps advancing as before,
    # so no turn-keyed schedule in any scenario changes.
    held = scen.held_turn(full_state)
    if not held:
        before = full_state.get(_TURN_HOLD_RELEASED_KEY)
        held = _turn_end_hold(data, full_state)
        if full_state.get(_TURN_HOLD_RELEASED_KEY) != before:
            state_store.save_state(viewer_id, full_state)
    if isinstance(data.get("chara_info"), dict):
        # ...AND WHATEVER THE HOLDS DECIDED, THE SERVED TURN NEVER GOES DOWN.
        # See _served_turn: a hold that starts after the client has already
        # been shown the next turn walks it backwards, and the client treats
        # every step as a turn step.
        ci_turn = data["chara_info"]
        served, moved = _served_turn(full_state, ci_turn,
                                     held or int(ci_turn.get("turn") or 0))
        ci_turn["turn"] = served
        if moved:
            state_store.save_state(viewer_id, full_state)
    if rolled or settled:
        # PERSIST the roll. exec_command banks whatever this preview promised,
        # so it has to survive to the next request -- recomputing it there would
        # re-roll against post-training bonds and could pay a different token
        # than the screen showed. A settled pay-out has to be persisted for the
        # obvious reason: it is paid exactly once.
        state_store.save_state(viewer_id, full_state)


# THE TURN-END CHAIN IS SERVED UNDER THE TURN THAT WAS JUST PLAYED ----------
#
# A training advances the run's turn immediately, but the beats that CONCLUDE
# the turn still belong to it and the real server serves them with the old
# number still on screen. It advances on the response that carries the NEXT
# turn's opening cutscene (play_timing 1) or, when there is none, on the empty
# tail of the chain. Capture, bot/20260905_183334 #0026-0029:
#
#     exec_command (turn 2)  [(201025, 6)]   chara_info.turn 2, playing_state 5
#     check_event            [(201114, 6)]   chara_info.turn 2
#     check_event            [(201103, 1)]   chara_info.turn 3
#     check_event            []              chara_info.turn 3
#
# We advanced on the exec_command instead, so every play_timing 6 beat in the
# game rendered under the NEXT turn's header -- "What's the Unity Cup?", "Team
# Support" and the Tutorial all looked like they had fired a turn early
# (user-reported 2026-09-07, turn 2's three-beat chain, the most visible case).
#
# THE TIMINGS THAT BELONG TO THE PLAYED TURN are 6 ("after a training") and 10.
# Scored over every exec_command/check_event response in the three bot
# captures, this rule reproduces 698 of 716 served turn numbers; all 18
# exceptions are turns 24 and 36, the team-race turns, which the scenario's own
# race hold already covers -- hence it is consulted only when that reports
# nothing.
#
# STATELESS ON PURPOSE. It is recomputed from the response's own event array
# every time, so unlike a persisted hold it cannot stick and freeze the served
# turn (see unity_cup.race_hold's brick guard for what that costs).
_TURN_END_TIMINGS = (6, 10)
# The advanced turn whose hold has already been given up. ONCE THE COUNTER HAS
# GONE UP IT MUST NOT COME BACK DOWN: our chain can serve a random/support
# event (20000, 10002) ahead of a scenario timing-6 beat, which the capture's
# never does, and holding purely on the head's timing then walked the client
# 18 -> 17 -> 18 inside one chain (observed live 2026-09-07 14:41). The hold is
# therefore released for good at the first beat that does not belong to the
# played turn, and re-armed by the next exec_command.
_TURN_HOLD_RELEASED_KEY = "turn_end_hold_released"


# THE SERVED TURN IS MONOTONIC. Holds decide to serve an EARLIER turn than the
# run's own, and each of them is right about when its own chain starts -- but
# none of them can see that a previous response in the same chain already served
# the later turn. Our chain can put a random/support event (20000, 10002) ahead
# of the scenario's timing-6 beat, so the turn ticks up on that first response
# and the hold then starts one response late, pulling it back down (observed
# live 2026-09-07: 18 -> 17, 19 -> 18 twice, 22 -> 21).
#
# The client reads every change as a TURN STEP (WorkSingleModeData.IsStepTurn),
# and a step is what makes it re-run the month-start production -- which is why
# the "N Turns to Reach Goal ... Start!" banner replayed at turns nowhere near a
# goal boundary (user-reported 2026-09-07; the 18 -> 17 step above put it on
# turn 17, and 31 - 17 + 1 = 15 is the number the banner showed).
#
# Keyed on the career's start_time so a NEW career starts from zero, and a drop
# of more than a hold's width is taken as a deliberate rewind (career.py
# set-turn) rather than clamped.
_TURN_SERVED_KEY = "turn_served_high"
_TURN_REWIND_SLACK = 2


def _served_turn(full_state: dict, chara_info: dict, served: int) -> tuple:
    """(turn to serve, whether full_state changed)."""
    served = int(served or 0)
    if served <= 0:
        return served, False
    career = str(chara_info.get("start_time") or "")
    mark = full_state.get(_TURN_SERVED_KEY)
    if not isinstance(mark, dict) or mark.get("career") != career:
        mark = {"career": career, "turn": 0}
    high = int(mark.get("turn") or 0)
    if served < high:
        if high - served <= _TURN_REWIND_SLACK:
            return high, False            # a hold that started late
        # Too big a drop to be a hold: the save itself was rewound.
    elif served == high:
        return served, False
    mark["turn"] = served
    full_state[_TURN_SERVED_KEY] = mark
    return served, True


def _turn_end_hold(data: dict, full_state: dict) -> int:
    """The turn to serve while this response carries a beat that concludes the
    turn just played, or 0 to serve the run's own turn.

    Returns 0 forever after within a chain once released -- see
    _TURN_HOLD_RELEASED_KEY."""
    chara_info = data.get("chara_info")
    if not isinstance(chara_info, dict):
        return 0
    turn = int(chara_info.get("turn") or 0)
    if turn <= 1:
        return 0
    events = data.get("unchecked_event_array") or []
    head = events[0] if events and isinstance(events[0], dict) else {}
    if int(head.get("play_timing") or 0) in _TURN_END_TIMINGS:
        if int(full_state.get(_TURN_HOLD_RELEASED_KEY) or 0) == turn:
            return 0                      # already advanced in this chain
        return turn - 1
    # Anything else -- including the empty tail -- ends the hold for this turn.
    if int(full_state.get(_TURN_HOLD_RELEASED_KEY) or 0) != turn:
        full_state[_TURN_HOLD_RELEASED_KEY] = turn
    return 0


# THE CLIENT'S TURN IS A CLAIM, NOT A FACT ----------------------------------
#
# Every career action carries the turn the CLIENT believes it is on, and several
# schedules here read it deliberately (the goal announcement's `acted_turn`,
# career_events.Ctx) because the run's own turn has already been advanced past
# it by the time they run. What it must never be is the SOURCE of the run's
# turn. The client caches its career locally and keeps sending that cache until
# it reloads, so a run whose turn moved underneath it -- admin.py set-turn, a
# restored save, a second client -- still reports the OLD number, and a handler
# that writes `current_turn + 1` back into chara_info silently rewinds the whole
# career to it. Live-reported 2026-09-07: set-turn 71 on turn 1, then train
# WITHOUT restarting the client, and the run lands on turn 2.
#
# The legitimate claims are exactly the turns we could still be showing:
#   * the run's own turn -- an action taken off a training screen we served;
#   * the run's turn - 1 -- the turn-end hold above, which serves the played
#     turn across the beats that conclude it;
#   * a scenario hold (Grand Live's concert, Unity Cup's team race), which pins
#     the served turn for the length of a cutscene chain.
# Anything else is a client whose cache disagrees with the run, and the only
# safe answer is to refuse the action: the persisted turn survives untouched and
# a reload hands the client the real one. Serving it is what corrupts the run.
def _client_turn_conflict(payload: dict, full_state: dict, chara_info: dict,
                          allow_played_turn: bool = True):
    """The client's claimed turn, if it cannot be one we are currently serving.

    None when the claim is legitimate (or absent -- plenty of requests carry no
    current_turn at all, and those are answered off the run's own turn).

    allow_played_turn admits the turn-end hold's `turn - 1`. Right for anything
    that READS the acted-on turn (that hold is exactly why those sites prefer
    the request's number), wrong for an action that ADVANCES the run: an
    exec_command arriving for a turn we have already advanced past is a replay,
    and honouring it would bank the same training twice and skip a turn.
    """
    claimed = payload.get("current_turn")
    if claimed is None or not isinstance(chara_info, dict):
        return None
    try:
        claimed = int(claimed)
        turn = int(chara_info.get("turn") or 0)
    except (TypeError, ValueError):
        return None
    if claimed == turn or (allow_played_turn and claimed == turn - 1):
        return None
    try:
        if claimed == int(scenarios.for_chara(chara_info).held_turn(full_state) or 0):
            return None
    except Exception:
        log.exception("held_turn check failed -- treating client turn %s as a "
                      "conflict against run turn %s", claimed, turn)
    return claimed


def _stale_turn_refusal(viewer_id, where: str, claimed, chara_info: dict) -> dict:
    """Refuse an action whose client turn disagrees with the run's."""
    log.warning("%s: viewer %s claims turn %s but the run is on turn %s -- "
                "REFUSING (stale client career cache; reload the career). "
                "Persisted turn left untouched.",
                where, viewer_id, claimed, (chara_info or {}).get("turn"))
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _friendship_bonus(full_state: dict, chara_info: dict) -> int:
    """Extra friendship-bonus percentage points from scenario mechanics.

    Grand Live's comes from the songs whose Live Bonus is active. 0 in every
    scenario that has no such mechanic -- and 0 in Grand Live too until a Live
    has actually activated something, which is the whole point of it.

    These three are thin wrappers rather than direct scenarios.for_chara() calls
    at each site because training_formula and _refresh_command_info take them as
    plain (full_state, chara_info) callables; the wrapper is the adapter."""
    return scenarios.for_chara(chara_info).friendship_bonus_pct(full_state)


def _specialty_bonus(full_state: dict, chara_info: dict) -> int:
    """Weight points added to a support card's OWN-TYPE facility when this
    turn's placement is rolled -- the same slot the card's own
    specialty_priority occupies. Grand Live's "Specialty Rate Up" Live Bonus
    feeds this; 0 in a scenario without one."""
    return scenarios.for_chara(chara_info).specialty_bonus(full_state)


def _training_bonus(full_state: dict, chara_info: dict):
    """A scenario's accumulated flat training bonus as {stat: +N} for the
    preview, or None. Grand Live's "Extra Stat Gain" songs feed this; None
    everywhere else, which is why other scenarios' previews are byte-identical
    to what they were before scenarios existed."""
    return scenarios.for_chara(chara_info).training_bonus(full_state)


def _sync_chara_info(viewer_id, endpoint: str, response: dict) -> dict:
    """Applied to every single_mode_team/* response (fixture-replayed or
    handler-produced) before it goes out: overwrites every top-level
    data.* field with our persisted state's copy of that same field, for
    any field both share.

    Why: single_mode_team/* fixtures were all captured from points in one
    recorded career (many clustered near its end, e.g. gain_skills at turn
    78/playing_state 5 "finished") and each embeds a full snapshot --
    chara_info, home_info, team_data_set, etc. -- from that moment. A fresh
    career (turn 1) hitting any of these via fixture replay looks like it
    jumped straight to the end. Originally this only patched chara_info
    (that's where the bug was first found, via single_mode_team/load), but
    the same staleness applies to every field these fixtures carry, so this
    syncs the whole family of fields rather than fixing each one as it's
    individually discovered.

    Fields present only in the fixture (not in our persisted state, e.g.
    single_mode_start_common) are left untouched -- we have nothing to sync
    them *to*. Endpoints that intentionally show a genuinely different
    snapshot for some field (reflecting an in-flight change the fixture
    captured) would be fighting this override -- none identified yet, but
    if one turns up, it needs its own handler that runs before this, not an
    exemption here.

    single_mode_load_common (single_mode_team/load only) is NOT just another
    top-level field -- real captures show it's a full second, NESTED copy of
    chara_info/home_info/etc. (SingleModeLoadCommon in dump.cs has its own
    chara_info field, separate from the top-level mirror), and the client's
    WorkSingleModeData.ApplySingleModeLoadResponse reads THIS nested copy,
    not the top-level one -- so leaving it untouched shows the captured
    career's real final turn (78, playing_state 5 "finished") instead of
    wherever the player actually left off, every time a career is resumed.
    Patching it blanket-style (every shared key, mirroring the top-level
    loop) was tried and caused a hard client crash (no catchable exception,
    Player.log just stops) -- almost certainly one of the nested array
    fields (mission_list, reserved_race_array, race_condition_array, ...)
    not actually sharing the same real invariants as its top-level
    /start-shaped namesake despite the matching key name, unlike chara_info
    (a flat scalar struct) and home_info (already exercised via the
    ura_data_set backfill + _apply_versus logic above on every response
    without incident). Only those two fields are mirrored into
    single_mode_load_common below, sourced from the ALREADY-patched
    top-level data (so Happy Meek/Director placement matches); the rest of
    single_mode_load_common stays untouched until each remaining field is
    verified safe individually against live testing."""
    if not endpoint.startswith(_career_prefixes()):
        return response

    # Race-flow endpoints carry RACE-state chara_info/home_info (playing_state=2
    # racing, race_program_id set, shortened_race_state, ...) that must NOT be
    # clobbered with the persisted training-state snapshot -- doing so wiped
    # playing_state back to 1 and the client never entered the race ("nothing
    # happens" on race entry). Those handlers build their own chara_info.
    if endpoint.rsplit("/", 1)[-1] in ("race_entry", "race_start", "race_end", "race_out",
                                       "check_event", "minigame_end", "continue",
                                       # Grand Live's two own endpoints likewise
                                       # build their own chara_info (a lesson's
                                       # stats / a Live's payout are applied
                                       # in-flight and must not be reverted to
                                       # the pre-call snapshot).
                                       "master_square", "live_start"):
        # minigame_end/continue build their own state (e.g. minigame_end's
        # playing_state=5 resume signal -- clobbering it back to the persisted
        # 1 left the client softlocked mid-crane-game).
        return response

    full_state = state_store.get_state(viewer_id)
    if not full_state or STATE_KEY not in full_state:
        return response

    persisted_data = full_state[STATE_KEY].get("data", {})
    data = response.get("data")
    if isinstance(data, dict):
        for key in data:
            # unchecked_event_array is the per-response EVENT QUEUE, owned by each
            # handler (start queues the intro; check_event advances/empties it).
            # The career state still holds the start's [3000]; syncing it would
            # re-serve the intro on every turn's response -> the intro replaying
            # each turn. Never sync it.
            if key == "unchecked_event_array":
                continue
            if key in persisted_data:
                data[key] = copy.deepcopy(persisted_data[key])

        # ura_data_set is REQUIRED for a URA training screen, but some fixtures
        # (the scenario-2 team exec_command) carry none -- and the sync loop only
        # overwrites keys the response ALREADY has, so it never adds one. A
        # training-screen response (has home_info) with chara_info referencing
        # Happy Meek but NO ura_data_set to register her in makes the client's
        # training init NullRef -> freeze on EVERY facility. Backfill it from the
        # persisted career (which was seeded from the URA start) so _apply_versus
        # has somewhere to put her versus membership.
        if "home_info" in data and "ura_data_set" not in data:
            persisted_uds = persisted_data.get("ura_data_set")
            if persisted_uds is not None:
                data["ura_data_set"] = copy.deepcopy(persisted_uds)

        # RETRY BUDGETS travel on home_info, and the client reads them to decide
        # whether to offer the free / alarm-clock buttons at all. They were only
        # ever recomputed inside handle_continue, so until the player used a
        # retry they were whatever the career-start fixture froze -- an
        # alarm-clock offer that ignored how many clocks the player owned, and a
        # free-retry count that never came back. Recomputed on every response
        # that carries home_info, from the live item stock and this race's
        # spend, so the buttons always match what can actually be paid for.
        if isinstance(data.get("home_info"), dict):
            counts = _continue_counts(full_state, full_state.get(RACE_CTX_KEY))
            data["home_info"].update(counts)
            if isinstance(persisted_data.get("home_info"), dict):
                persisted_data["home_info"].update(counts)

        # Happy Meek is injected per-response (she's NOT baked into the persisted
        # home_info the loop above just restored), so re-place her AFTER the sync
        # or the overwrite drops her partner slot + marker (Director survives only
        # because he IS baked into the persisted home_info). check_event/race
        # endpoints returned early above and handle their own placement.
        _apply_versus(data, full_state, (data.get("chara_info") or {}).get("turn"))

        # race_condition_array MUST be computed AFTER the restore loop above --
        # that loop just overwrote it with whatever static value the persisted
        # career's own /start snapshot happened to freeze (the captured
        # fixture's own race), same class of staleness as race_history before
        # it was made dynamic. Only ever REPLACES a field the endpoint's own
        # fixture already carries (never adds the key where it never existed).
        # Must run BEFORE the single_mode_load_common mirror below, which reads
        # this same freshly-computed value.
        if "race_condition_array" in data and "chara_info" in data:
            data["race_condition_array"] = _build_race_condition_array(
                data["chara_info"], data["chara_info"].get("turn"),
                persisted_data.get("race_history", []))

        # The in-career MISSION BOARD. Every response that carries the key
        # shipped it empty, so the board was blank for the whole career, on a
        # fresh start and on a resumed load alike. Built from the account's
        # real mission progress -- see missions.career_mission_list for the row
        # filter and for why it is the current campaign's rows only.
        if "mission_list" in data:
            data["mission_list"] = missions.career_mission_list(viewer_id, full_state)

        # story_event_mission_list is the STORY EVENT board, and in career it is
        # empty: 157 of 157 real career records across the three
        # captures/bot/20260905_*_icarus sessions carry []. It was reaching the
        # wire populated because the seed blob these responses are built from
        # carries the rows the capture's account happened to have, and nothing
        # cleared them -- so a career advertised missions for a story event that
        # is not running and that this server does not implement at all.
        if "story_event_mission_list" in data:
            data["story_event_mission_list"] = []

        # race_history goes on the WIRE, so it may only carry the eight fields
        # the client's own struct declares -- see _WIRE_RACE_HISTORY_FIELDS.
        # Must run BEFORE the single_mode_load_common mirror below, which deep
        # copies this value into the nested copy.
        if "race_history" in data:
            data["race_history"] = _wire_race_history(data["race_history"])

        # reserved_race_array's deck_num=0 ("Currently Scheduled Races") --
        # see _build_reserved_race_array / handle_multi_race_reserve.
        if "reserved_race_array" in data:
            data["reserved_race_array"] = _build_reserved_race_array(
                data["reserved_race_array"], full_state)

        # single_mode_load_common (single_mode_team/load only): the SECOND, nested
        # copy of chara_info/home_info that ApplySingleModeLoadResponse actually
        # reads to decide which screen to resume into -- not the top-level mirror
        # this function patches above. Left untouched, it kept showing the
        # CAPTURED career's own state (turn 78, playing_state 5 "finished"), which
        # is why resuming ANY career always dropped you at the very end (the
        # skill-select / end-of-career screen) regardless of your real turn.
        #
        # chara_info, home_info, race_history, and race_condition_array are
        # mirrored in here (as deep copies of the just-patched top-level values
        # above) -- a full blanket sync of every shared key (mission_list,
        # reserved_race_array, ...) was tried previously and caused a hard
        # client crash with no catchable exception, so the rest of
        # single_mode_load_common is deliberately left as the captured snapshot
        # until each remaining field is verified safe individually against live
        # testing. race_history/race_condition_array are safe to add here
        # (unlike those): they're already real, trusted, dynamically-built
        # values (see _append_race_history / _build_race_condition_array), not
        # raw fixture noise -- and without mirroring race_condition_array
        # specifically, the in-career Race List screen (which opens straight
        # into today's turn, reading THIS nested copy) kept showing "no races
        # to compete in" even after the top-level field was already correct --
        # confirmed live: manually browsing to a DIFFERENT calendar turn showed
        # real races (that path re-reads the top-level/per-turn field), only
        # the screen's own default landing turn read the stale nested copy.
        smlc = data.get("single_mode_load_common")
        if isinstance(smlc, dict):
            if "chara_info" in smlc and "chara_info" in data:
                smlc["chara_info"] = copy.deepcopy(data["chara_info"])
            if "home_info" in smlc and "home_info" in data:
                smlc["home_info"] = copy.deepcopy(data["home_info"])
            if "race_history" in smlc and "race_history" in data:
                smlc["race_history"] = copy.deepcopy(data["race_history"])
            if "race_condition_array" in smlc and "race_condition_array" in data:
                smlc["race_condition_array"] = copy.deepcopy(data["race_condition_array"])
            if "reserved_race_array" in smlc and "reserved_race_array" in data:
                smlc["reserved_race_array"] = copy.deepcopy(data["reserved_race_array"])
            if "mission_list" in smlc and "mission_list" in data:
                smlc["mission_list"] = copy.deepcopy(data["mission_list"])
            # Same reason as the top-level one above, and safe for the same
            # reason race_history is: an empty array is what real sends here in
            # every captured career record, so this cannot be the "raw fixture
            # noise" the blanket-sync warning is about -- it is the OPPOSITE,
            # clearing the captured account's rows that leaked through.
            if "story_event_mission_list" in smlc:
                smlc["story_event_mission_list"] = []

        # LAST, because it edits both copies patched above: put a career that
        # was quit mid-race back into its race instead of in front of it.
        _resume_race_on_load(endpoint, data, full_state)

    return response


# The playing_states that mean "this career is somewhere inside a race"
# (SingleModeDefine.PlayingState: 2 Race, 3 RaceInGame, 4 RaceResult).
_RACE_PLAYING_STATES = (2, 3, 4)


def _resume_race_on_load(endpoint: str, data: dict, full_state: dict) -> None:
    """Resume a career that was quit mid-race, on /load.

    RACE_CTX_KEY is the in-flight marker: _resolve_and_enter_race writes it as
    the race is entered and race_out pops it once the race is over. Nothing
    else about the race survived a quit -- the race chara_info (playing_state 2
    + race_program_id) was only ever built onto the WIRE, never persisted -- so
    /load re-served the ordinary training screen and the race was simply
    un-run: enter a race, close the game, come back, and you are standing in
    front of it again (user-reported).

    Everything has to go into single_mode_load_common: the client reads NOTHING
    else on a load (SingleModeLoadResponse.CommonResponse declares exactly
    single_mode_load_common + ura_data_set, and SingleModeLoadCommon is where
    race_start_info/race_scenario live). The top-level mirror is patched
    alongside it only to keep the two consistent, and only where the key
    already exists -- inventing a top-level field the response never had is
    what the single_mode_load_common blanket-sync warning above is about.

    The resume lands on playing_state 2 (Race), never 3 or 4, whatever stage
    the player quit at:

      * it is the one resume point that needs no reconstructed animation and no
        rebuilt reward card -- race_start_info is in the ctx verbatim,
      * the simulation was rolled at ENTRY and stashed in the same ctx, so
        re-running produces the identical result (same roster, same seed), and
      * handle_ura_race_end is idempotent per ctx (end_committed), so a race
        whose fans/history/pay-out were already banked cannot pay twice.

    The reverse case matters just as much. race_end PERSISTS playing_state 4,
    so a quit at the result screen used to load the client straight onto a race
    screen with no race_start_info behind it. A race playing_state with no ctx
    to resume into is unwound to TurnStart rather than served as-is."""
    if (endpoint or "").rsplit("/", 1)[-1] != "load":
        return
    ci = data.get("chara_info")
    if not isinstance(ci, dict):
        return
    ctx = full_state.get(RACE_CTX_KEY) or {}
    rsi = ctx.get("race_start_info")
    # A ctx stamped with a different turn than the run is on is a leftover, not
    # an in-flight race -- the same staleness guard race entry itself applies.
    resumable = bool(rsi) and ctx.get("turn") == ci.get("turn")
    stranded = int(ci.get("playing_state") or 0) in _RACE_PLAYING_STATES
    if not resumable and not stranded:
        return

    smlc = data.get("single_mode_load_common")
    holders = [h for h in (data, smlc) if isinstance(h, dict)]
    for holder in holders:
        hci = holder.get("chara_info")
        if not isinstance(hci, dict):
            continue
        if resumable:
            hci["playing_state"] = 2
            hci["state"] = 0
            hci["race_program_id"] = ctx.get("program_id")
        else:
            hci["playing_state"] = 1
            hci["race_program_id"] = 0
    if not resumable:
        log.info("load: career carried race playing_state with no race context "
                 "to resume (turn %s) -- reset to the training screen",
                 ci.get("turn"))
        return

    for holder in holders:
        if holder is smlc or "race_start_info" in holder:
            holder["race_start_info"] = copy.deepcopy(rsi)
        if ctx.get("race_scenario") and (holder is smlc or "race_scenario" in holder):
            holder["race_scenario"] = ctx["race_scenario"]
        # Whatever was remembered on screen belongs to the turn, not to the
        # race the client is about to re-enter -- and the pre-race cutscene it
        # would be has already played, before the quit.
        if "unchecked_event_array" in holder:
            holder["unchecked_event_array"] = []
    log.info("load: resuming in-flight race program %s (turn %s) instead of "
             "the training screen", ctx.get("program_id"), ctx.get("turn"))


def _prune_roster(viewer_id, response) -> None:
    """Drop roster rows for characters this career does not actually have.

    Done HERE, centrally, because doing it per-handler kept missing paths:
    `apply_versus_state` already rebuilds the roster correctly, but three
    responses never call it -- single_mode/load (pure fixture replay),
    race_entry, and the pre-race event's own resolution -- so each shipped the
    CAPTURE'S rows. That is the live "Tazuna Hayakawa / Trainer Kiryuin / Riko
    Kashimoto will now appear in training" trio at the debut, on a deck that
    contains none of them, and it survived a fix applied to only one path.
    Every response goes through sync_chara_info, so this cannot be bypassed.

    Kept: the live deck's slots, NPCs actually unlocked, and Happy Meek (2001)
    once she is. Everything else is fixture residue -- and residue is not
    merely cosmetic: rows present in ura_data_set but absent from
    chara_info.evaluation_info_array are exactly the desync apply_versus_state
    warns NullRefs the client's training init."""
    career = (state_store.get_state(viewer_id) or {}).get(STATE_KEY)
    if not isinstance(career, dict):
        return                      # no career -> nothing to validate against
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    chara_info = career.get("data", {}).get("chara_info") or {}
    # position -> the chara that slot's card actually features. Filtering by
    # target_id ALONE is not enough: the capture's own deck occupies slots 1-6
    # too, so its rows are positionally valid while naming the wrong six
    # characters. Those have to be CORRECTED, not dropped -- the client needs
    # a row per deck slot.
    deck = {}
    for c in chara_info.get("support_card_array") or ():
        pos, sid = c.get("position"), c.get("support_card_id")
        if pos:
            deck[pos] = event_engine.card_chara_id(sid) or 0
    allowed = set(deck)
    full_state = state_store.get_state(viewer_id) or {}
    for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []) or ():
        allowed.add(n[0] if isinstance(n, (list, tuple)) else n)
    # Grand Live's recruited-but-uncarded supporters hold rows keyed by their
    # OWN chara_id (see _ensure_supporter_eval_rows). They are as real to this
    # career as a deck slot -- without them here the prune deleted the very
    # rows the training screen had just placed, and the client drew a nameless
    # portrait (or nothing) where the supporter was standing.
    allowed.update(_npc_supporters(full_state, chara_info))
    if not allowed:
        return                      # an empty deck here means we can't tell; leave it

    def prune(container):
        arr = container.get("evaluation_info_array")
        if not isinstance(arr, list):
            return
        kept = []
        for e in arr:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            tid = e.get("target_id")
            if tid not in allowed:
                continue                      # not in this career at all
            if tid in deck and "chara_id" in e and e["chara_id"] != deck[tid]:
                e = dict(e, chara_id=deck[tid])   # right slot, wrong character
            kept.append(e)
        container["evaluation_info_array"] = kept

    for key in ("ura_data_set", "chara_info"):
        blob = data.get(key)
        if isinstance(blob, dict):
            prune(blob)
    smlc = data.get("single_mode_load_common")
    if isinstance(smlc, dict):
        for key in ("ura_data_set", "chara_info"):
            blob = smlc.get(key)
            if isinstance(blob, dict):
                prune(blob)


def handle_load(payload: dict) -> dict:
    """single_mode_team/load: re-displays the in-progress career.

    Turns out the real client calls this directly when entering career mode
    -- it does NOT call single_mode_team/start first if a career already
    exists in its own view of the world. That meant this endpoint's old
    fallback (plain fixture replay when we had no persisted state) was
    actually the COMMON path for the real client, not a rare edge case --
    and that fixture is frozen at turn 78 of the captured career, right
    near its real completion. Every "entering career mode dumps me at the
    end" report traced back to this: no single_mode_team/start call ever
    happened, so no state existed, so this returned the near-finished raw
    fixture every time.

    Fix: the fallback now seeds a fresh turn-1 career (same as start would)
    instead of returning the raw fixture, so simply visiting career mode
    always gets you a real, in-progress career -- whether or not the client
    happened to call start first.

    Second bug found later (real client testing): this used to return
    career_state directly -- but career_state is built from a /start
    capture, and /start and /load responses have genuinely different
    top-level shapes (/load has single_mode_load_common, race_history, etc.
    that /start never has; /start has single_mode_start_common, tp_info,
    etc. that /load never has -- confirmed by diffing real captures of
    each). Returning /start's shape here left single_mode_load_common
    missing from the wire response entirely, which the client deserializes
    as null and then NullReferenceExceptions on in
    WorkSingleModeData.ApplySingleModeLoadResponse. Fix: always return a
    real captured /load envelope (so every load-only field is present and
    well-formed, if stale) and let sync_chara_info -- which already runs on
    every single_mode_team/* response and overwrites any field present in
    both shapes -- patch in the persisted/fresh chara_info, home_info, etc."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id)
    if full_state is None or STATE_KEY not in full_state:
        # SEED IT FOR THE SCENARIO THE CLIENT IS ACTUALLY IN. The only start
        # template in the corpus is the scenario-2 Unity Cup capture, so this
        # fallback handed a URA client (single_mode/load, no start call) a
        # scenario_id 2 career -- team mode's screens, team mode's events, on a
        # URA endpoint family. Nothing else in the request identifies the
        # scenario, so the prefix decides it; the trainee is still the
        # template's, which is inherent to conjuring a career out of nothing and
        # is why this path exists only so that visiting career mode without
        # calling start does not hard-fail.
        career_state = _seed_fresh_career(viewer_id, None)
        _force_seeded_scenario(viewer_id, career_state, _endpoint_scenario_id())

    # Prefer the REAL URA single_mode/load capture (scenario_id 1, has
    # ura_data_set, no team_data_set) over single_mode_team/load -- that one is
    # a Unity Cup/TEAM capture (scenario_id 2, DOES carry team_data_set), and
    # was being used unconditionally for every career regardless of scenario.
    # A URA response structurally carrying team_data_set (a field that should
    # only exist for the team scenario) is exactly the kind of inconsistency
    # that could make the client treat the screen as team mode instead of URA
    # -- team mode has no reduced "Race Day" screen, it just shows the normal
    # facility grid with everything disabled, which is what mandatory-race
    # turns were incorrectly rendering as. Same fixture-preference pattern
    # already used for /start (see _seed_dynamic_career).
    pair = fixtures.first("single_mode/load") or fixtures.first("single_mode_team/load")
    if pair is None:
        raise LookupError("No captured single_mode/load fixture available")
    response = pair.response_copy()
    # The raw fixture's OWN unchecked_event_array is a leftover from whatever
    # moment IT was captured at (the URA one carries a stray post-race event for
    # its own recorded trainee/chara_id -- wrong for every other player), and the
    # persisted career's frozen career-start [3000] is equally wrong.
    #
    # But an EMPTY array isn't right either: whatever was genuinely ON SCREEN
    # when the player quit must come back, or closing the game skips it --
    # live-reported (#33) as being able to skip a race or an event's choice by
    # restarting. _remember_display records the last served array; re-serve it.
    pending_display = (state_store.get_state(viewer_id) or {}).get(DISPLAY_EVENT_KEY) or []
    response["data"]["unchecked_event_array"] = copy.deepcopy(pending_display)
    smlc = response["data"].get("single_mode_load_common")
    if isinstance(smlc, dict) and "unchecked_event_array" in smlc:
        smlc["unchecked_event_array"] = copy.deepcopy(pending_display)
    return response


DISPLAY_EVENT_KEY = "on_screen_event"   # the array the client is showing RIGHT NOW
# The just-cleared passive goal's 'Goal Achieved' event, waiting to be served.
# Its own slot rather than either queue, because _drain_pending serves the
# unified pipeline before legacy EXTRA and this has to lead BOTH -- see the
# parking site in handle_exec_command's passive-goal block.
GOAL_EVENT_KEY = "pending_goal_event"


def _served_branch(full_state: dict, event_id, choice_number):
    """The story branch we told the client to play for this choice, or None.

    Read straight back off the entry that is ON SCREEN right now (the one
    _remember_display recorded when it went out), so the reward a gamble pays
    cannot disagree with the text the client is already playing -- see
    event_engine._choice_branch_index. Reading the served entry rather than
    re-deriving the value is the point: a re-derivation would roll again, and
    two rolls is exactly the bug.

    The client sends back the chosen option's gain_select_id_index as
    choice_number (1..N; a one-choice event commits with 0), which is this
    array's own order."""
    for entry in full_state.get(DISPLAY_EVENT_KEY) or ():
        if not isinstance(entry, dict) or entry.get("event_id") != event_id:
            continue
        arr = (entry.get("event_contents_info") or {}).get("choice_array") or []
        i = max(1, int(choice_number or 1)) - 1
        if i >= len(arr):
            i = 0
        if i < len(arr) and isinstance(arr[i], dict):
            return arr[i].get("select_index")
    return None


def _remember_display(full_state: dict, response: dict) -> None:
    """Record what this response puts on screen so a reload can re-serve it.

    Without it, quitting mid-event dropped the event entirely and the player
    could skip its choice (or a whole race) by restarting the game (#33)."""
    try:
        arr = (response.get("data") or {}).get("unchecked_event_array")
    except AttributeError:
        return
    if arr:
        full_state[DISPLAY_EVENT_KEY] = copy.deepcopy(arr)
    else:
        full_state.pop(DISPLAY_EVENT_KEY, None)


# The single_mode_team/* race flow used to have its own handlers here: pick a
# captured race by program_id, then walk race_start/end/out by capture-order
# correlation, each returning pair.response_copy() with NO patching.
# Since the race endpoints are precisely the ones _sync_chara_info skips, that
# served the captured Maruzensky career verbatim to whoever was actually
# racing. Deleted rather than left as a fallback: both remaining call sites
# already fall back to the team capture as a structural TEMPLATE (patching the
# real trainee over it), so reaching these meant serving a foreign career, and
# keeping them around is what let main.py go on pointing at them for weeks
# after the single_mode/* twin was fixed. See handle_ura_race_entry /
# handle_ura_race_start / handle_ura_race_end / handle_ura_race_out.


# --- URA career race flow (single_mode/race_*) -------------------------------
# The real client's URA career race goes race_entry -> race_start -> race_end ->
# race_out. We have REAL captured single_mode/race_entry + race_start (from the
# 2026-07-20 UmaDumpy URA capture) but NOT race_end/race_out -- the capture tool
# crashes at race_start, so those two are SYNTHESIZED here (win the race, grant
# fans/rewards, advance the turn) using the old single_mode_team/race_end/out as
# structural skeletons minus the team-only team_data_set. Untested end-to-end
# against the live client -- flagged for validation.
RACE_CTX_KEY = "active_race_ctx"
STYLE_CHOICE_KEY = "career_style_choice"  # the strategy the PLAYER picked on the
                                          # race screen (single_mode/change_running_style)
                                          # -- per-career, cleared on career start


def _best_running_style(chara_info: dict) -> int:
    """1 Nige / 2 Senkou / 3 Sashi / 4 Oikomi -- the trainee's strongest style
    aptitude (ties go to the front-most style). Career state's own
    race_running_style is NOT trustworthy as a default: it's inherited verbatim
    from the captured start fixture (live-reported: an A-pace trainee served
    running sashi at aptitude D, 'everyone seems to run on late style')."""
    apts = {1: chara_info.get("proper_running_style_nige", 1),
            2: chara_info.get("proper_running_style_senko", 1),
            3: chara_info.get("proper_running_style_sashi", 1),
            4: chara_info.get("proper_running_style_oikomi", 1)}
    return max(apts, key=lambda k: (apts[k], -k))


def _player_running_style(full_state: dict, chara_info: dict) -> int:
    """The style the player's trainee races with: the player's own explicit
    pick (change_running_style) if they made one this career, else her best
    style aptitude."""
    choice = (full_state or {}).get(STYLE_CHOICE_KEY)
    return choice if choice in (1, 2, 3, 4) else _best_running_style(chara_info)


def _career_chara_info(viewer_id) -> dict | None:
    full = state_store.get_state(viewer_id) or {}
    career = full.get(STATE_KEY)
    return career.get("data", {}).get("chara_info") if isinstance(career, dict) else None


def _patch_player_horse(race_start_info: dict, chara_info: dict,
                        running_style: int | None = None) -> None:
    """The captured race_start_info describes the RECORDED trainee (chara 1011,
    single_mode_chara_id 603) as the player horse (entry 0). Overwrite it with
    THIS run's trainee so race_horse_data agrees with chara_info -- otherwise the
    client can't set up the race and 'nothing happens' at race_entry. (The
    encoded race_scenario still animates the recorded race; that's cosmetic.)"""
    if not chara_info:
        return
    rhd = (race_start_info or {}).get("race_horse_data")
    if not isinstance(rhd, list) or not rhd or not isinstance(rhd[0], dict):
        return
    h = rhd[0]
    card_id = chara_info.get("card_id")
    h["single_mode_chara_id"] = chara_info.get("single_mode_chara_id", h.get("single_mode_chara_id"))
    h["trained_chara_id"] = 0
    if card_id:
        h["card_id"] = card_id
        h["chara_id"] = card_id // 100
        h["race_dress_id"] = trained_chara.dress_for_card(card_id, card_id // 100)
    h["rarity"] = chara_info.get("rarity", h.get("rarity"))
    h["talent_level"] = chara_info.get("talent_level", h.get("talent_level"))
    h["skill_array"] = copy.deepcopy(chara_info.get("skill_array", []))
    h["speed"] = chara_info.get("speed", 0)
    h["stamina"] = chara_info.get("stamina", 0)
    h["pow"] = chara_info.get("power", 0)
    h["guts"] = chara_info.get("guts", 0)
    h["wiz"] = chara_info.get("wiz", 0)
    h["motivation"] = chara_info.get("motivation", h.get("motivation"))
    h["fan_count"] = chara_info.get("fans", h.get("fan_count"))
    h["running_style"] = running_style or _best_running_style(chara_info)
    for apt in (
        "proper_distance_short", "proper_distance_mile", "proper_distance_middle",
        "proper_distance_long", "proper_running_style_nige", "proper_running_style_senko",
        "proper_running_style_sashi", "proper_running_style_oikomi",
        "proper_ground_turf", "proper_ground_dirt",
    ):
        if apt in chara_info:
            h[apt] = chara_info[apt]


_DEBUT_GRADE = 900  # race.grade for every maiden/debut race; its story title says
                    # "Debut", not the race's real name ("Junior Make Debut" etc.)


def _race_info_for_program(program_id: int) -> dict | None:
    """Resolve a single_mode_program id -> race_instance -> race -> race name
    (text_data cat 28, keyed by race_instance_id). 'race_name' is the string
    used in a chara's own 'Before the X' / 'After the X' story titles --
    'Debut' for any grade-900 (maiden) race, the real race name otherwise.
    Shared by _race_for_turn (mandatory route races) and
    _voluntary_race_for_turn (optional races on any other turn)."""
    program = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (program_id,))
    if not program:
        return None
    instance_id = program["race_instance_id"]
    inst = master_data.query_one("SELECT race_id FROM race_instance WHERE id=?", (instance_id,))
    if not inst:
        return None
    race_id = inst["race_id"]
    race_row = master_data.query_one("SELECT grade FROM race WHERE id=?", (race_id,))
    if race_row and race_row["grade"] == _DEBUT_GRADE:
        story_name = "Debut"
    else:
        name_row = master_data.query_one(
            "SELECT text FROM text_data WHERE category=28 AND [index]=?", (instance_id,))
        story_name = name_row["text"] if name_row else None
    return {"program_id": program_id, "race_instance_id": instance_id,
            "race_id": race_id, "story_name": story_name}


_RACE_CATEGORIES = ("short", "mile", "middle", "long")


def _race_category(distance: int) -> str:
    """Distance category by the same thresholds that classify every URA-finals
    course variant correctly: <=1400 short, <=1800 mile, <=2400 middle."""
    if distance <= 1400:
        return "short"
    if distance <= 1800:
        return "mile"
    if distance <= 2400:
        return "middle"
    return "long"


@functools.lru_cache(maxsize=4096)
def _program_course(program_id) -> tuple | None:
    """(ground, category) for a program -- ground 1 turf / 2 dirt."""
    row = master_data.query_one(
        "SELECT cs.distance AS distance, cs.ground AS ground FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE p.id=?", (program_id,))
    return (row["ground"], _race_category(row["distance"])) if row else None


@functools.lru_cache(maxsize=64)
def _branch_arms(race_set_id: int) -> tuple:
    """Every branch ARM of a race set as ((id, sort_id, race_instance_id), ...)."""
    return tuple(
        {"id": r["id"], "sort_id": r["sort_id"], "inst": r["inst"]}
        for r in master_data.query(
            "SELECT rr.id AS id, rr.sort_id AS sort_id, p.race_instance_id AS inst "
            "FROM single_mode_route_race rr "
            "JOIN single_mode_program p ON p.id = rr.condition_id "
            "WHERE rr.race_set_id=? AND rr.determine_race != 0", (race_set_id,)))


def _race_set_of(route_race_id_array) -> int | None:
    ids = [int(i) for i in (route_race_id_array or ())]
    if not ids:
        return None
    row = master_data.query_one(
        f"SELECT race_set_id FROM single_mode_route_race "
        f"WHERE id IN ({','.join(map(str, ids))}) LIMIT 1")
    return row["race_set_id"] if row else None


def route_race_id_for_instance(chara_info: dict, race_instance_id: int) -> int:
    """The ROUTE_RACE row id of the branch arm that runs `race_instance_id`.

    `ChoiceArray.target_race_id` is a route_race id, NOT a race_instance id --
    the real server sends 296 (Agnes Tachyon's NHK Mile arm row) where we were
    sending 100701 (the NHK Mile race instance). Capture:
    `docs/tachyon switch`, seq 1192.
    """
    rs = _race_set_of(chara_info.get("route_race_id_array"))
    if rs is None or not race_instance_id:
        return 0
    arm = next((a for a in _branch_arms(rs) if a["inst"] == race_instance_id), None)
    return arm["id"] if arm else 0


def refresh_route_screen(full_state: dict | None, chara_info: dict) -> bool:
    """Rebuild the training screen after this turn's GOALS changed.

    A goal race turn is a FORCED turn: the client disables every training
    command and offers only the race. Cancelling that goal mid-turn (or
    switching a branch arm onto/off this turn) leaves the screen built for a
    race that is no longer scheduled -- live-reported 2026-09-04 on
    Matikanetannhauser's turn-70 cancellation: "im now locked out of doing any
    action apart from racing (should be other way around)". The lockout flag was
    right; the SCREEN was stale.

    Rebuilds the persisted career's home_info so the response copy taken after
    this carries it, and so a later /load serves the same thing.
    """
    if not isinstance(full_state, dict):
        return False
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return False
    data = career.get("data") or {}
    home = data.get("home_info")
    if not isinstance(home, dict):
        return False
    # The career's own chara_info is what every later response serves, so the
    # new route has to land on it, not only on the copy being mutated.
    ci = data.get("chara_info")
    if isinstance(ci, dict) and ci is not chara_info:
        ci["route_race_id_array"] = list(chara_info.get("route_race_id_array") or ())
        if chara_info.get(RACE_RESTRICT_UNTIL_KEY):
            ci[RACE_RESTRICT_UNTIL_KEY] = chara_info[RACE_RESTRICT_UNTIL_KEY]
    else:
        ci = chara_info
    _refresh_command_info(
        ci, home, turn=ci.get("turn", 1),
        unlocked_npcs=[n[0] for n in (full_state.get(
            single_mode_events.UNLOCKED_NPCS_KEY) or [])],
        facility_levels=_facility_levels(data),
        race_history=data.get("race_history", []),
        training_bonus=_training_bonus(full_state, ci),
        friendship_bonus=_friendship_bonus(full_state, ci),
        specialty_bonus=_specialty_bonus(full_state, ci),
        support_card_levels=_support_card_levels(full_state),
        friendship_stacks=_friendship_stacks(full_state),
        full_state=full_state)
    return True


def cancel_route_race(chara_info: dict, race_instance_id: int) -> bool:
    """CANCEL an objective race (GameTora's `ra`) -- drop it from this career's
    goals for good.

    The counterpart to switch_route_race: not a swap between arms, but a goal
    taken away. Matikanetannhauser's "There's Always Next Time" cancels her
    Japan Cup ("Objective race Japan Cup cancelled", alongside a 1-turn race
    lockout), and master marks that goal `determine_race = 4` -- the only row in
    the table with that value, i.e. the only goal in the game that can be
    removed rather than exchanged.
    """
    ids = list(chara_info.get("route_race_id_array") or ())
    race_set_id = _race_set_of(ids)
    if race_set_id is None or not race_instance_id:
        return False
    target = next((a for a in _branch_arms(race_set_id)
                   if a["inst"] == race_instance_id), None)
    if target is None or target["id"] not in ids:
        return False
    chara_info["route_race_id_array"] = [i for i in ids if i != target["id"]]
    log.info("route goal cancelled: sort_id %s -> route_race %s (race_instance %s)",
             target["sort_id"], target["id"], race_instance_id)
    return True


def restrict_racing(chara_info: dict, turns: int) -> bool:
    """"Cannot race for N turns" (GameTora's `rl`). Records the turn racing
    reopens on; _sync_command_info holds race_entry_restriction at 1 until then.

    master.mdb corroborates the one case we can check: single_mode_race_restrict
    _turn has (chara 1062, turn 70) -- Matikanetannhauser, the Japan Cup turn --
    with a gain_id encoding her story suffix 118, "There's Always Next Time".
    So the lockout and the cancelled goal are the same beat: she cannot race the
    turn her goal was on."""
    try:
        turns = int(turns)
    except (TypeError, ValueError):
        return False
    if turns <= 0:
        return False
    turn = chara_info.get("turn") or 0
    until = max(int(chara_info.get(RACE_RESTRICT_UNTIL_KEY) or 0), turn + turns)
    chara_info[RACE_RESTRICT_UNTIL_KEY] = until
    log.info("racing restricted for %s turn(s), reopens on turn %s", turns, until)
    return True


def switch_route_race(chara_info: dict, race_instance_id: int) -> bool:
    """Take a branching career storyline's OTHER arm: rewrite this career's
    `route_race_id_array` so the branch group containing `race_instance_id` is
    represented only by that arm.

    This is how the real server delivers a branch. `route_race_id_array` is a
    plain int[] on SingleModeCharaData, re-sent on every single_mode response,
    and dump.cs has no route-select endpoint -- the server simply sends a
    different array. Doing the same here means every existing reader (the goal
    banner, _race_for_turn, the forced-turn set, the epithet goal checks, the
    flag-gated rival lookup) follows the branch for free, and
    _dropped_alternative_ids then sees a one-row group and keeps it, instead of
    falling back to master's `determine_race_for_generate == 0` default.

    Returns whether anything changed -- False when the target race is not an
    arm of any branch group this trainee has (a `rc` effect on a trainee whose
    route does not branch, which master.mdb does contain).
    """
    ids = list(chara_info.get("route_race_id_array") or ())
    if not ids or not race_instance_id:
        return False
    # Look across the WHOLE race set, not just the ids currently served: the
    # array carries only the arm in force (see _career_route), so the arm being
    # switched TO is by definition not in it yet.
    race_set_id = _race_set_of(ids)
    if race_set_id is None:
        return False
    row = {"race_set_id": race_set_id}
    arms = _branch_arms(row["race_set_id"])
    target = next((r for r in arms if r["inst"] == race_instance_id), None)
    if target is None:
        log.warning("route branch: race_instance %s is not an arm of any branch "
                    "group on race set %s", race_instance_id, row["race_set_id"])
        return False
    if target["id"] in ids:
        return False                      # already on this arm
    # Drop whichever arm of the SAME group is in force, put the new one in its
    # place, and keep the array in master's id order so the goal list stays in
    # schedule order for every reader.
    siblings = {r["id"] for r in arms if r["sort_id"] == target["sort_id"]}
    ids = [i for i in ids if i not in siblings]
    ids.append(target["id"])
    chara_info["route_race_id_array"] = sorted(ids)
    log.info("route branch taken: sort_id %s -> route_race %s (race_instance %s)",
             target["sort_id"], target["id"], race_instance_id)
    return True


def _turn_slot(turn):
    """(month, half, period) for a career turn, or None -- the one place the
    single_mode_turn lookup lives now that non-race code (event_engine's season
    guard) needs the calendar too."""
    if turn is None:
        return None
    return master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))


@functools.lru_cache(maxsize=64)
def _route_goal_rows(route_race_id_array: tuple) -> tuple:
    """((turn, condition_id, kind), ...) for every REAL, determined race goal
    on this route -- the single source both _forced_race_turns and
    _race_for_turn resolve from.

    condition_type=1 ONLY: the 5 condition_type=2 rows (Oguri Cap, Agnes
    Digital, Tamamo Cross, Smart Falcon, Matikanetannhauser) are passive
    grade-TALLY goals ('top-3 in N G1s by this turn') whose condition_id is a
    race GRADE (100/300/700), not a program id -- treating them as races
    (the old condition_type IN (1,2)) wrongly forced those turns AND resolved
    calendar-impossible races via the accidental program-id collision
    (program 100 = Hakodate Kinen etc.). They need no forcing at all.

    condition_type=9 is Trackblazer's Twinkle Star Climax leg (turns 74/76/78,
    condition_id 40001/40002/40003). Mechanically it is the same thing as a
    ct=1 group goal -- a mandatory race resolved out of single_mode_race_group
    -- and only its CLEAR condition differs (cumulative Victory Point standings
    rather than a placing), which is settled in the Trackblazer scenario and
    not here. Omitting it left those three turns unforced and unresolvable, so
    the Climax could not be entered at all.

    kind='program': condition_id is a single_mode_program id (549 rows,
    all calendar-verified). kind='group': condition_id is a
    single_mode_race_group id -- the URA finals rounds (10001/10002/10003,
    41 distance/ground variants each, turns 74/76/78) and the venue-rotating
    JBC races (20002/20003, 4 venues, turn 69) -- resolved per-career by
    _group_race_for_turn.

    determine_race != 0 rows are ALTERNATIVE goals (same sort_id, different
    turns/races, e.g. race_set 1022's choice of Japan Cup/Mile CS/Champions
    Cup/Arima): the real game picks ONE per career; forcing all four would
    lock 4 extra turns. Exactly one alternative per sort_id survives -- master's
    own default, `determine_race_for_generate == 0` (see
    _dropped_alternative_ids). The real game lets the PLAYER choose, through an
    event nothing has captured yet; the default is the best-evidenced stand-in
    and replaced the old 'lowest row id' rule, which disagreed with master in
    5 of the 16 branching groups."""
    if not route_race_id_array:
        return ()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, turn, sort_id, condition_id, condition_value_1, determine_race "
        f"FROM single_mode_route_race "
        f"WHERE id IN ({ids}) AND condition_type IN (1, 9) AND target_type IN (1, 3) "
        f"ORDER BY id")
    dropped = _dropped_alternative_ids(route_race_id_array)
    out = []
    for r in rows:
        cid = r["condition_id"]
        if not cid:
            continue
        if r["id"] in dropped:
            continue        # a branching alternative this career doesn't run
        if master_data.query_one("SELECT id FROM single_mode_program WHERE id=?", (cid,)):
            kind = "program"
        elif master_data.query_one(
                "SELECT id FROM single_mode_race_group WHERE race_group_id=?", (cid,)):
            kind = "group"
        else:
            continue
        out.append((r["turn"], cid, kind, r["condition_value_1"]))
    return tuple(out)


def _best_ground_category(chara_info: dict, race_history) -> tuple:
    """The (ground, category) the trainee has raced MOST across the whole
    career, ties broken by higher aptitude -- the rule the real server's
    finals picks are consistent with (capture: mile 3 / middle 3 tie, mile
    aptitude S beats middle A -> finals ran turf mile). With no history,
    falls back to pure aptitude."""
    apt = {"short": chara_info.get("proper_distance_short", 1),
           "mile": chara_info.get("proper_distance_mile", 1),
           "middle": chara_info.get("proper_distance_middle", 1),
           "long": chara_info.get("proper_distance_long", 1)}
    ground_apt = {1: chara_info.get("proper_ground_turf", 1),
                  2: chara_info.get("proper_ground_dirt", 1)}
    tally = {}
    for h in race_history or ():
        gc = _program_course(h.get("program_id"))
        if gc:
            tally[gc] = tally.get(gc, 0) + 1
    if tally:
        return max(tally, key=lambda gc: (tally[gc], apt.get(gc[1], 0), ground_apt.get(gc[0], 0)))
    ground = 1 if ground_apt[1] >= ground_apt[2] else 2
    return (ground, max(_RACE_CATEGORIES, key=lambda c: apt[c]))


def _group_race_for_turn(chara_info: dict, group_id: int, turn, race_history=()) -> dict | None:
    """Resolve a single_mode_race_group goal to ONE member program. For the
    URA finals (41 course variants per round) the member matching the
    trainee's most-raced ground+distance category is chosen (aptitude
    tie-break; see _best_ground_category), relaxing to category-only then the
    whole group if that combination doesn't exist (e.g. dirt long). For the
    JBC groups (4 venues of the same race) any member is calendar-valid.
    Deterministic per turn+trainee so re-serving the turn repeats the pick."""
    members = master_data.query(
        "SELECT rg.race_program_id AS pid, cs.distance AS distance, cs.ground AS ground "
        "FROM single_mode_race_group rg JOIN single_mode_program p ON p.id=rg.race_program_id "
        "JOIN race_instance ri ON ri.id=p.race_instance_id JOIN race r ON r.id=ri.race_id "
        "JOIN race_course_set cs ON cs.id=r.course_set WHERE rg.race_group_id=?", (group_id,))
    if not members:
        return None
    pool = list(members)
    if len(pool) > 1:
        ground, category = _best_ground_category(chara_info, race_history)
        exact = [m for m in pool if m["ground"] == ground and _race_category(m["distance"]) == category]
        by_cat = exact or [m for m in pool if _race_category(m["distance"]) == category]
        pool = by_cat or pool
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = random.Random((int(turn or 0) << 16) ^ chara_id).choice(pool)
    return _race_info_for_program(pick["pid"])


def _race_for_turn(chara_info: dict, turn, race_history=()) -> dict | None:
    """The trainee's real, DETERMINED race scheduled for this turn -- a
    personal goal race (kind='program') or a group-resolved one (URA finals
    rounds / JBC, kind='group'); see _route_goal_rows for the classification.
    None if this turn isn't one of the trainee's own determined races -- see
    _voluntary_race_for_turn for every OTHER turn."""
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    if not route_ids or turn is None:
        return None
    for t, cid, kind, _cv1 in _route_goal_rows(route_ids):
        if t == turn:
            if kind == "program":
                return _race_info_for_program(cid)
            return _group_race_for_turn(chara_info, cid, turn, race_history)
    return None


@functools.lru_cache(maxsize=64)
def _dropped_alternative_ids(route_race_id_array: tuple) -> frozenset:
    """Row ids of the branching goal alternatives NOT taken this career.

    A `determine_race != 0` group is a CHOICE: several rows share a sort_id and
    the game runs exactly one of them (e.g. chara 1005's NHK Mile Cup on turn 33
    OR the Japanese Derby on turn 34; chara 1022 picks one of four). Every row
    but the chosen one must be dropped, or the turn gets force-locked for races
    that will never run and the goal banner counts one goal several times.

    WHICH one is master's own call: `determine_race_for_generate == 0` marks the
    default and the alternatives carry 99. Verified across all 16 branching
    groups -- 15 have exactly one such row, and the odd one out (chara 1062
    sort 9) has a single alternative anyway. The previous rule, 'keep the lowest
    row id', disagrees with master in **5 of 16 groups** -- most visibly chara
    1005, where it forced the NHK Mile Cup on turn 33 instead of the Derby on 34.

    This is still the DEFAULT, not the player's choice: the real game offers the
    pick through an event we have not reverse-engineered yet (no capture of one
    exists). Picking master's default is simply the best-evidenced stand-in."""
    if not route_race_id_array:
        return frozenset()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, sort_id, determine_race_for_generate AS gen "
        f"FROM single_mode_route_race WHERE id IN ({ids}) AND determine_race != 0")
    by_sort: dict = {}
    for r in rows:
        by_sort.setdefault(r["sort_id"], []).append(r)
    drop = set()
    for rs in by_sort.values():
        # This filters a LIVE array, so a group present here is a group the
        # career is on: pick its one arm and never empty the group. Excluding a
        # CONDITIONAL goal happens once, at route generation -- see
        # _conditional_goal_ids -- because once an event has granted it, it is
        # in the array precisely because it was earned.
        ordered = sorted(rs, key=lambda x: x["id"])
        chosen = next((r for r in ordered if r["gen"] == 0), ordered[0])
        drop |= {r["id"] for r in rs if r["id"] != chosen["id"]}
    return frozenset(drop)


@functools.lru_cache(maxsize=64)
def _route_all_goals(route_race_id_array: tuple) -> tuple:
    """Every target_type=1 goal row on this route, in schedule order:
    (turn, sort_id, condition_type, condition_id, cv1, cv2). Covers race
    goals (ct=1), grade tallies (ct=2) and fan thresholds (ct=3) -- the
    complete set the in-game goal banner steps through."""
    if not route_race_id_array:
        return ()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    rows = master_data.query(
        f"SELECT id, turn, sort_id, condition_type, condition_id, condition_value_1, "
        f"condition_value_2 FROM single_mode_route_race WHERE id IN ({ids}) "
        f"AND target_type=1 ORDER BY turn, sort_id")
    # Drop the branching alternatives not taken -- this list feeds the goal
    # BANNER, and counting every alternative made a 4-way choice read as four
    # separate goals (chara 1022's sort_id 10).
    dropped = _dropped_alternative_ids(route_race_id_array)
    return tuple((r["turn"], r["sort_id"], r["condition_type"], r["condition_id"],
                  r["condition_value_1"], r["condition_value_2"])
                 for r in rows if r["id"] not in dropped)


def _goal_window(goal, all_goals) -> tuple:
    """(after_turn, by_turn] -- the span a goal's qualifying race must fall in:
    strictly after the PREVIOUS goal's turn, up to and including its own.

    Without this, a repeated race satisfies the wrong year's goal: Curren
    Chan's route lists the Sprinter Stakes in BOTH classic and senior, and her
    classic win was counting for the senior goal too, so the run read as fully
    cleared (live-reported)."""
    turn = goal[0]
    earlier = [g[0] for g in all_goals if g[0] < turn]
    return (max(earlier) if earlier else 0), turn


def _grade_tally(goal, race_history, all_goals=()) -> int:
    """How many races count toward a ct=2 GRADE-TALLY goal ('finish cv1-th or
    better in cv2 races of grade condition_id').

    WINDOWED, exactly like a ct=1 race goal (see _goal_window): only races run
    strictly AFTER the previous goal's turn and no later than this goal's own
    deadline turn are counted. The client counts it that way -- its own header
    helper is
        SingleModeUtils.GetNextTargetRaceGradeWinNum(
            int preRouteTurn, RaceDefine.Grade grade, int rank, int nextRouteTurn)
    (dump.cs), taking BOTH ends of the span, with preRouteTurn coming from
    GetPreRequiredTargetRace(baseTurn) -- the previous target_type=1 row, which
    is what `all_goals` is here.

    Counting the WHOLE career instead made these goals self-clearing off the
    route's own earlier mandatory races and silently un-failable: Oguri Cap's
    turn-60 'top 3 in 2 G1 races' was already satisfied by her turn-46 and
    turn-48 goal races (both top-3 G1s), so the server announced Goal Achieved
    at turn 60 while the client's banner still read 0/2, and a player who never
    ran a senior G1 was never failed for it. Same for Agnes Digital (turn 60),
    Tamamo Cross (49), Smart Falcon (25) and Matikanetannhauser (24) -- the
    only five ct=2 rows in master.

    A history row with no turn recorded (pre-dating that field) is counted
    rather than dropped, the same tolerance the ct=1 window match applies."""
    _turn, _sort, _ctype, cid, cv1, _cv2 = goal
    after, by = _goal_window(goal, all_goals) if all_goals else (None, None)
    tally = 0
    for h in race_history or ():
        if after is not None:
            ht = h.get("turn")
            if ht is not None and not (after < ht <= by):
                continue      # right grade, wrong span -- another goal's race
        if (h.get("result_rank") or 99) > (cv1 or 99):
            continue
        row = master_data.query_one(
            "SELECT r.grade AS g FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id=p.race_instance_id "
            "JOIN race r ON r.id=ri.race_id WHERE p.id=?", (h.get("program_id"),))
        if row and row["g"] == cid:
            tally += 1
    return tally


def _goal_is_cleared(goal, chara_info: dict, race_history, all_goals=()) -> bool:
    """Whether a single goal row's requirement is satisfied right now.

    all_goals (the route's full goal list) enables WINDOW matching for race
    goals AND for grade tallies -- pass it wherever the answer must be
    year-correct."""
    _turn, _sort, ctype, cid, cv1, cv2 = goal
    if ctype == 3:                                   # fan threshold
        return (chara_info.get("fans", 0) or 0) >= (cv1 or 0)
    if ctype == 2:                                   # 'top-cv1 in cv2 races of grade cid'
        return _grade_tally(goal, race_history, all_goals) >= (cv2 or 1)
    # ct=1: the specific race, finished at or better than the required place
    # (cv1 == 0 on the debut means 'just run it') -- and, when the route's goal
    # list is available, run WITHIN this goal's own window (see _goal_window).
    after, by = _goal_window(goal, all_goals) if all_goals else (None, None)
    for h in race_history or ():
        if h.get("program_id") != cid:
            continue
        if after is not None:
            ht = h.get("turn")
            if ht is not None and not (after < ht <= by):
                continue      # right race, wrong YEAR -- a later goal owns it
        return not cv1 or (h.get("result_rank") or 99) <= cv1
    return False


def _goals_cleared_count(chara_info: dict, race_history, turn=None) -> int:
    """How many of this route's goals are satisfied -- the value the client's
    goal banner shows as 'cleared' (real capture: show_clear=1 right after
    clearing goal #1; =2 on a later event).

    A PASSIVE goal (fan threshold / grade tally) does NOT count until its
    DEADLINE turn arrives, even though its condition may be met long before.
    Without that, winning a race that pushes you past the fan threshold bumps
    this count immediately and the goal-cleared banner fires on that race's
    event -- live-reported as "the goal complete popup appeared 6 turns early on
    a race event instead of her designated event". The official capture is
    explicit: fans were already 3562 at turn 23 and the goal still announced at
    turn 24, its deadline. Race goals (ct=1) are unaffected -- you can only
    clear one by running it, which happens on its own turn anyway.

    `turn` is optional so callers that just want "is everything done" (e.g. the
    end-of-career check) keep the unfiltered count."""
    goals = _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
    if turn is None:
        turn = chara_info.get("turn")
    n = 0
    for g in goals:
        if not _goal_is_cleared(g, chara_info, race_history, goals):
            continue
        # g = (turn, sort_id, condition_type, condition_id, cv1, cv2)
        if g[2] in (2, 3) and turn is not None and g[0] > turn:
            continue        # passive goal met early -- not announced until due
        n += 1
    return n


GOAL_MARKED_KEY = "goal_clear_marked"   # legacy high-water count (kept for old saves)
GOAL_ANNOUNCED_KEY = "goals_announced"  # sort_ids whose clear has been announced
SCENARIO_TURNS_FIRED_KEY = "scenario_turns_fired"  # legacy key (cleared on career start)
SCENARIO_EVENTS_DONE_KEY = "scenario_events_done"  # scheduled event_ids RESOLVED


def _goal_announced(full_state: dict, sort_id) -> bool:
    return sort_id in set(full_state.get(GOAL_ANNOUNCED_KEY) or ())


def _record_goal_announced(full_state: dict, sort_id) -> None:
    done = set(full_state.get(GOAL_ANNOUNCED_KEY) or ())
    done.add(sort_id)
    full_state[GOAL_ANNOUNCED_KEY] = sorted(done)


def _mark_goal_progress(response: dict, chara_info: dict, race_history, sort_id=None,
                        full_state: dict | None = None) -> None:
    """Stamp the goal-banner fields onto the turn's first queued event, for ONE
    specific goal (`sort_id`).

    Tracked PER GOAL, not by a running count. The old high-water counter
    coupled every announcement together, and a race on a passive goal's own
    deadline turn consumed the passive goal's slot: the 3000-fan clear appeared
    on the 'Victory!' post-race event and its dedicated Goal Achieved event
    never played at all (live-reported). Each goal now announces exactly once,
    on whichever event legitimately owns it, and cannot steal another's.

    show_clear is a FLAG, not a count: 1 = cleared, 2 = failed/abandoned (the
    career-fail event carries 2 in the capture). Sending the cleared COUNT here
    made a third clear go out as '2+' and play the ABANDON text over a won goal
    (live-reported: 'Operation Oversight Completed' after winning)."""
    events = response.get("data", {}).get("unchecked_event_array") or []
    if not events or sort_id is None:
        return
    if full_state is not None:
        if _goal_announced(full_state, sort_id):
            return
        _record_goal_announced(full_state, sort_id)
        full_state[GOAL_MARKED_KEY] = _goals_cleared_count(chara_info, race_history)
    info = events[0].setdefault("event_contents_info", {})
    info["show_clear"] = 1
    info["show_clear_sort_id"] = sort_id


# A PASSIVE goal (fan threshold / grade tally) gets its OWN event, verbatim from
# the official capture of this very career (UmaDumpy 20260728_151855, tx 0085 --
# McQueen's turn-24 'earn 3000 fans' goal):
#
#   check_event -> {"event_id": 11105, "chara_id": 1013, "story_id": 501013415,
#                   "play_timing": 6, "event_contents_info": {
#                       "show_clear": 1, "show_clear_sort_id": 2,
#                       "choice_array": [<one acknowledge>], ...}}
#
# Three things that capture settles, all of which we had wrong:
#  1. It is a REAL EVENT, not a banner stamped on whatever else is on screen.
#     We were stamping show_clear onto the turn's existing event, which is
#     exactly the live report ("the 3000 fans event triggers inside the
#     Well-Rested event instead of its own event"). Officially the rest event
#     ('All Refreshed', tx 0084) plays FIRST and the goal event chains behind it.
#  2. TIMING is the goal's DEADLINE TURN, not the turn the threshold is crossed.
#     Fans were already 3562 at turn 23 in that capture; the goal announced at
#     turn 24, its deadline. Our existing turn gate was therefore right.
#  3. The story served is the row's **short_story_id** (the 4xx band), not its
#     story_id (3xx). Both name the same scene; the official server uses the
#     short cut everywhere in unchecked_event_array.
#
# play_timing 6 = the post-action chain (the same timing the rest event uses).
_GOAL_ACHIEVED_PLAY_TIMING = 6
# Clearing a goal PAYS, on the event's resolution. Capture-measured (tx 0085 ->
# 0086, turn 24 -> 25): +3 to all five stats and +24 SP. Attribution is solid --
# across all 74 single_mode responses in that session the ONLY all-five stat
# bumps are the two that follow a show_clear event. One sample, so unknown
# whether the amounts scale with anything; taken flat.
_GOAL_CLEAR_STATS = 3
_GOAL_CLEAR_SP = 24
GOAL_STORIES_FIRED_KEY = "goal_achieved_stories_fired"
# event_id: officially per-trainee and sequential by story row, but the base
# differs per chara (1001->10714, 1011->10864, 1013->10800, 1060->11047) and no
# formula fits all four, so the numbering is NOT solved. 10800 + suffix
# reproduces McQueen's real 11105 exactly and is merely plausible elsewhere;
# functionally it only has to be stable across the preview/commit round trip,
# which it is.
_GOAL_ACHIEVED_EVENT_BASE = 10800


@functools.lru_cache(maxsize=256)
def _goal_achieved_stories(chara_id: int) -> tuple:
    """((story_id, short_story_id), ...) of the trainee's goal-cleared scenes,
    in story order.

    Matched on the 'Goal ' prefix, NOT 'Goal Achieved' -- the title varies per
    trainee and an over-narrow match is a live bug: Silence Suzuka (chara 1002)
    has 'Goal Accomplished: A Secret Place' (501002304), so she resolved to
    nothing, fell through to the banner-stamp fallback, and her clear was
    announced on top of the New Year's event with no event of her own
    (live-reported). Both prefixes together give 30 stories across 23 trainees
    and cover ALL 23 trainees who have a passive goal -- i.e. the fallback
    should now never be needed for a fan goal."""
    base = 500000000 + chara_id * 1000
    return tuple((r["story_id"], r["short_story_id"] or r["story_id"])
                 for r in master_data.query(
                     "SELECT sd.story_id, sd.short_story_id FROM single_mode_story_data sd "
                     "JOIN text_data t ON t.category=181 AND t.[index]=sd.story_id "
                     "WHERE t.text LIKE 'Goal %' AND sd.story_id>=? "
                     "AND sd.story_id<? ORDER BY sd.story_id", (base, base + 1000)))


def _goal_achieved_event(full_state: dict, chara_info: dict, sort_id) -> dict | None:
    """The trainee's own 'Goal Achieved' event for a just-cleared passive goal,
    or None when this trainee has no such story (then the caller keeps stamping
    the banner onto the turn's event, which is all we can do). Each story fires
    at most once per career -- the two-goal trainees walk them in order."""
    chara_id = (chara_info.get("card_id") or 0) // 100
    stories = _goal_achieved_stories(chara_id) if chara_id else ()
    if not stories:
        return None
    fired = set(full_state.get(GOAL_STORIES_FIRED_KEY) or ())
    nxt = next(((sid, short) for sid, short in stories if sid not in fired), None)
    if not nxt:
        return None
    story_id, short_id = nxt
    full_state[GOAL_STORIES_FIRED_KEY] = sorted(fired | {story_id})
    return {
        "event_id": _GOAL_ACHIEVED_EVENT_BASE + story_id % 1000,
        "chara_id": chara_id, "story_id": short_id,
        "play_timing": _GOAL_ACHIEVED_PLAY_TIMING,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 1,
            "show_clear_sort_id": sort_id if sort_id is not None else 1,
            "choice_array": [{"select_index": 1, "receive_item_id": 0,
                              "target_race_id": 0, "gain_select_id_index": 1,
                              "select_icon": 0}],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def _newly_cleared_goals(full_state: dict, chara_info: dict, race_history) -> int:
    """How many goals are cleared, if that's MORE than we've announced (else
    0). Pure query -- does not consume the transition."""
    cleared = _goals_cleared_count(chara_info, race_history)
    return cleared if cleared > (full_state.get(GOAL_MARKED_KEY) or 0) else 0


# The route's PASSIVE deadline goals (ct=3 fan thresholds, ct=2 grade tallies)
# are just _route_all_goals filtered by condition_type -- there is deliberately
# no separate query for them. The one that used to live here selected straight
# from single_mode_route_race, so it carried neither the sort_id a window needs
# nor _dropped_alternative_ids' branch pruning, and _maybe_fail_goal's failure
# test drifted away from _goal_is_cleared's clear test as a result.


# The trainee's own reaction beat when a career goal is FAILED.
#
# BUG FIXED 2026-09-03 (live-reported: "my run ended due to not reaching req to
# enter race, it said well-rested event instead of loss event"). Both fail paths
# queued story suffix 701 with event id 7011 -- but this file's own captured
# table says 701/7011 IS "Well-Rested!", the rest event. The goal band carries
# the real pair: 600 "Ready for a Challenge" announces a goal, 601 "Not This
# Time!" is its failure sibling (the 7xx band is daily life -- sleep, meals,
# outings -- not goal outcomes). 601's real event_id was never captured, so it
# resolves through the ordinary shared-beat path and falls back to the generic
# id rather than borrowing the rest event's.
_CAREER_FAIL_REACTION_SUFFIX = 601


def _purge_queued_events(response: dict, full_state: dict) -> None:
    """Drop every event queued for this turn. Called the instant a career
    FAILS, before the fail chain is queued.

    BUG FIXED 2026-09-03 (live-reported: after the failure screen, "I got
    Curren Chan's Cute Posts event, and then Sweep Tosho's SSR chain event --
    I should have ended right after that failure event"). The career is over
    at state=2, but the turn's other events were already sitting in the
    display slot / EXTRA_EVENTS_KEY / PENDING_EVENTS_KEY, and _queue_turn_event
    politely chains BEHIND whatever is already there. So the fail chain played
    and then the turn carried on serving trainee and support-card events into a
    run that had already ended.

    Clearing first also means the fail reaction claims the display slot
    directly, which is what makes it the event the player actually sees."""
    response["data"]["unchecked_event_array"] = []
    full_state[CAREER_FAILED_FLAG_KEY] = True
    full_state.pop(SCENARIO_SLOT_RESERVED_KEY, None)
    for key in (single_mode_events.EXTRA_EVENTS_KEY,
                single_mode_events.PENDING_EVENTS_KEY,
                event_engine.CAREER_EVENT_CTX_KEY,
                single_mode_events.FAIL_CTX_KEY,
                HINT_REVEAL_CTX_KEY, REST_CTX_KEY,
                single_mode_events.DUEL_CTX_KEY):
        full_state.pop(key, None)


def _queue_fail_reaction(response, full_state, chara_info) -> None:
    """Queue the trainee's own 'goal failed' story, if she has one."""
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return
    story_id = 500000000 + chara * 1000 + _CAREER_FAIL_REACTION_SUFFIX
    if not master_data.query_one(
            "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)):
        return
    _queue_turn_event(response, full_state, _scenario_event_entry(
        _shared_beat_event_id(_CAREER_FAIL_REACTION_SUFFIX), story_id,
        play_timing=6, choices=[_ack_choice()], chara_id=chara), fail_chain=True)


# Career-failure wire shape, captured VERBATIM from a real failed career
# (UmaDumpy 20260721_143256: arrival at a goal turn with too few fans to
# enter -> chara_info.state=2 + this event; client acks it and the career is
# over -- no continue endpoint exists in any capture and the continue
# counters never change, so the alarm-clock retry is not modeled).
def _career_fail_event() -> dict:
    e = _scenario_event_entry(1, 400000090, play_timing=1, choices=[])
    e["event_contents_info"]["show_clear"] = 2
    return e


def _maybe_fail_goal(response: dict, full_state: dict, career: dict, new_turn) -> bool:
    """On ARRIVAL at a goal deadline turn, evaluate it exactly like the real
    server: (a) a program goal the trainee can't ENTER (fans below the
    race's need_fan_count -- the captured failure: needed 6000, had 2208);
    (b) ct=3 fan-threshold goals; (c) ct=2 grade-tally goals. On failure:
    chara_info.state=2 (the captured career-failed marker; playing_state
    stays 1) + queue the 400000090 ending event. Returns True if failed."""
    chara_info = career["data"]["chara_info"]
    if chara_info.get("state") == 2:
        return True
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    fans = chara_info.get("fans", 0)
    history = career["data"].get("race_history") or []
    # A goal the CLEAR logic already counts as done can never be a failure --
    # the two used to judge independently and disagree, which is how a met
    # goal still popped the 'incomplete' fail cutscene (live-reported).
    _all_goals = _route_all_goals(route_ids)
    cleared = {(g[0], g[2], g[3]) for g in _all_goals
               if _goal_is_cleared(g, chara_info, history, _all_goals)}
    failed = False
    for t, cid, kind, _cv1 in _route_goal_rows(route_ids):
        if t == new_turn and kind == "program" and (t, 1, cid) not in cleared:
            prog = master_data.query_one(
                "SELECT need_fan_count FROM single_mode_program WHERE id=?", (cid,))
            if prog and fans < prog["need_fan_count"]:
                failed = True
    for g in _all_goals:
        # PASSIVE deadlines (ct=3 fan threshold, ct=2 grade tally) are judged
        # when the trainee LEAVES the deadline turn, not on arrival at it: the
        # goal turn is itself a turn you can still race on to make the number.
        # Judging on arrival stole that last turn and failed goals the player
        # then went on to hit. (The captured arrival-time failure is the
        # PROGRAM branch above -- 'not enough fans to even ENTER this turn's
        # goal race' -- which is a different test.)
        #
        # The verdict is `cleared`'s and ONLY `cleared`'s. This used to re-test
        # the condition here with its own second copy of the tally, which is
        # how the two came to disagree once the tally was windowed: the copy
        # counted the whole career, so a grade goal the clear logic had NOT
        # met still read as met and never failed. One judge, one answer.
        if g[2] not in (2, 3) or g[0] + 1 != new_turn:
            continue
        if (g[0], g[2], g[3]) not in cleared:
            failed = True
    if failed:
        chara_info["state"] = 2
        # Real sequence (capture-corrected): the trainee's own fail-reaction
        # story (50<chara>701, event 7011, timing 6) plays FIRST, then the
        # 400000090 career-over event; the post-cutscene check_event gets an
        # empty queue and the client proceeds to factor_select -> finish.
        _purge_queued_events(response, full_state)
        _queue_fail_reaction(response, full_state, chara_info)
        _queue_turn_event(response, full_state, _career_fail_event(), fail_chain=True)
        log.info("career goal FAILED at turn %s (state=2, fail chain queued)", new_turn)
    return failed


# single_mode_scenario.turn_set_id for URA (scenario_id=1) -- maps a turn to its
# real-world (month, half) calendar slot via single_mode_turn. Hardcoded to 1
# rather than looked up per-call since URA is the only scenario wired up so far
# (see the scenario-progression note in _voluntary_race_for_turn).
_URA_TURN_SET_ID = 1


def _voluntary_race_for_turn(chara_info: dict, turn) -> dict | None:
    """An OPTIONAL race the trainee can voluntarily enter on a turn that isn't
    one of their own determined route races (once racing has unlocked, command
    401 stays enabled on every turn -- see _build_training_command_info). Real
    captures confirm this is a genuine mechanic: a turn-26 capture shows a race
    (program 196) that matches neither that trainee's single_mode_route_race
    rows NOR their single_mode_rival schedule -- a true voluntary entry. But
    every captured race_entry REQUEST is just {event_id, chara_id,
    choice_number, current_turn} -- no race/program identifier at all -- so the
    client never tells the server which race it picked. The eligible race has
    to be resolved server-side: single_mode_turn maps this turn to a (month,
    half) calendar slot; single_mode_program in that slot, excluding every id
    already claimed by a mandatory route race, is the candidate pool; narrowed
    to what the trainee can afford (need_fan_count) and is roughly the right
    class for (recommend_class_id vs chara_grade, widening the tolerance until
    something matches so a turn is never left with zero options -- confirmed
    real turns can have 250+ candidates in one slot, so this shouldn't starve).

    race_permission gates by CAREER YEAR, not by scenario as first thought
    (the IL2Cpp FilterPermission check filters against the current year's
    allowed set): 1=junior, 2=classic, 3=senior, 4=classic+senior
    (multi-year races like the Arima variants), 5=URA finale only. Evidence
    covering all cases: every junior-year enterable race in captures is
    perm=1 (incl. the turn-15 capture whose 7-of-19 narrowing matches
    perm=1 exactly); the capture-confirmed VOLUNTARY entry at classic turn
    26 (program 196, Crocus Stakes) is perm=2 -- impossible under a
    perm=1-only filter; all 12 JBC senior programs are perm=3; the 246
    finale programs (month 1!) are perm=5, which without this filter would
    flood every January turn's pool with URA Finale races.

    Turns 73-78 (single_mode_turn.period=3, the finals window) offer NO
    voluntary races at all -- real capture shows an empty race_condition_array
    at turn 73, and race_entry_type=0 disables entry on 73/75/77.

    Picked deterministically (seeded by turn+chara) so re-entering the same
    turn doesn't roll a different race each time."""
    if turn is None:
        return None
    trow = master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not trow or trow["period"] == 3:
        return None
    fans = chara_info.get("fans", 0)
    grade = chara_info.get("chara_grade", 1)
    rows = master_data.query(
        "SELECT id, race_instance_id, recommend_class_id FROM single_mode_program "
        f"WHERE month=? AND half=? AND need_fan_count<=? AND race_permission IN {_year_permissions(turn)} "
        "AND id NOT IN (SELECT DISTINCT condition_id FROM single_mode_route_race "
        "WHERE condition_type=1 AND condition_id IS NOT NULL)",
        (trow["month"], trow["half"], fans))
    if not rows:
        return None
    for tolerance in (0, 1, 2, 99):
        cands = [r for r in rows if abs(r["recommend_class_id"] - grade) <= tolerance]
        if cands:
            break
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = random.Random((int(turn) << 16) ^ chara_id).choice(cands)
    return _race_info_for_program(pick["id"])


def _year_permissions(turn) -> str:
    """SQL tuple literal of the race_permission values enterable on this turn.

    race_permission is the race's AGE eligibility, not the career year:
    1 = junior-only, 2 = classic-only, 3 = classic-and-senior ("3yo and up"),
    4 = senior-only, 5 = the URA Finale programs (never voluntary). So a
    junior may enter only 1; a classic may enter 2 and 3; a senior 3 and 4.

    Derived by fitting against every real captured race_condition_array
    (~1970 records, all four scenarios): the previous mapping put 4 in the
    classic year and left 2 out of it entirely, which both showed
    senior-only races to a classic trainee and hid the classic-only ones --
    e.g. real classic turn 25 lists ten perm-2 programs and no perm-4, and
    real senior turn 49 lists ten perm-4 and no perm-2."""
    year = (int(turn) - 1) // 24 + 1 if turn else 1
    if year <= 1:
        return "(1)"
    if year == 2:
        return "(2, 3)"
    return "(3, 4)"


def _race_condition_entry(program_id: int) -> dict:
    """weather/ground_condition aren't simulated (no real forecast data source)
    -- deterministic per program_id so re-serving the same turn doesn't change
    the displayed conditions."""
    seed = (program_id * 2654435761) & 0xffffffff
    return {"program_id": program_id, "weather": 1 + seed % 3, "ground_condition": 1 + (seed // 3) % 3}


# Grade 900 and grade 800 are DIFFERENT races, and conflating them broke a live
# career. Checked against master:
#   grade 900 = 'Junior Make Debut'                      25 programs
#   grade 800 = 'Junior Maiden Race' / 'Classic Maiden'  172 programs
# The debut is 900. The MAIDEN races you must win after losing it are 800.
_MAIDEN_GRADE = 800


@functools.lru_cache(maxsize=1)
def _debut_program_ids() -> frozenset:
    """The DEBUT programs (grade 900, 'Junior Make Debut') -- 25 of them, all in
    the same calendar slot as a route's own fixed debut turn."""
    rows = master_data.query(
        "SELECT sp.id FROM single_mode_program sp JOIN race_instance ri ON ri.id = sp.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE r.grade = 900")
    return frozenset(r["id"] for r in rows)


@functools.lru_cache(maxsize=1)
def _maiden_program_ids() -> frozenset:
    """The MAIDEN programs (grade 800) -- the retry pool after a lost debut."""
    rows = master_data.query(
        "SELECT sp.id FROM single_mode_program sp JOIN race_instance ri ON ri.id = sp.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE r.grade = ?", (_MAIDEN_GRADE,))
    return frozenset(r["id"] for r in rows)


def _debut_cleared(race_history) -> bool:
    """True once the trainee has WON (result_rank 1) the debut OR any MAIDEN
    race -- the real "clear your maiden" condition, and the thing that unlocks
    normal racing and training again.

    It used to count grade 900 ONLY, i.e. the debut itself. Live consequence:
    a career lost its debut, went and won a 'Junior Maiden Race' (grade 800) --
    exactly what the game asks for -- and stayed locked out of training anyway,
    because that win didn't match. Read from race_history rather than a
    persisted flag, since that is already the trusted record of every race run."""
    ids = _debut_program_ids() | _maiden_program_ids()
    return any(h.get("program_id") in ids and h.get("result_rank") == 1 for h in (race_history or []))


def _debut_retry_active(chara_info: dict, turn, race_history) -> bool:
    """True if training must stay locked into a forced re-run of the debut
    race -- the real mechanic when a trainee LOSES their debut: instead of
    advancing to the normal schedule, every subsequent turn offers another
    grade=900 maiden race (see _debut_program_ids) until one is actually won.
    Previously unhandled entirely: turn kept advancing normally after a lost
    debut, is_forced_turn/race_condition_array fell back to the ordinary
    schedule (which has nothing at that point -- the trainee's real route
    hasn't resumed yet), so both the Race Day lock and the Race List showed
    nothing until the trainee's stats eventually happened to line up with a
    normal voluntary race by sheer coincidence."""
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    forced_turns = _forced_race_turns(route_ids)
    if not forced_turns:
        return False
    debut_turn = min(forced_turns)
    if turn is None or turn <= debut_turn:
        return False
    return not _debut_cleared(race_history)


@functools.lru_cache(maxsize=128)
def _maiden_races_for_turn(turn) -> tuple:
    """Every MAIDEN (grade 800) program whose calendar slot is this turn -- the
    races a trainee who lost her debut may still enter."""
    pool = sorted(_maiden_program_ids())
    if not pool or turn is None:
        return ()
    slot = master_data.query_one(
        "SELECT month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not slot:
        return ()
    ids = ",".join(str(int(i)) for i in pool)
    return tuple(r["id"] for r in master_data.query(
        f"SELECT id FROM single_mode_program WHERE id IN ({ids}) "
        f"AND month=? AND half=? ORDER BY id", (slot["month"], slot["half"])))


def _debut_retry_race(chara_info: dict, turn) -> dict | None:
    """The MAIDEN race offered on a retry turn -- deterministic per turn+trainee
    so re-entering the same turn doesn't roll a different race.

    Drawn from grade 800 ('Junior Maiden Race'), NOT grade 900: 900 is the
    'Junior Make Debut' you just lost, and re-offering that is not what the game
    does. Restricted to maiden programs whose calendar slot is actually THIS
    turn, so the retry is a race that could really run today; falls back to the
    whole maiden pool if the calendar yields nothing."""
    pool = sorted(_maiden_program_ids())
    if not pool:
        return None
    slot = master_data.query_one(
        "SELECT month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if slot:
        ids = ",".join(str(int(i)) for i in pool)
        same_slot = [r["id"] for r in master_data.query(
            f"SELECT id FROM single_mode_program WHERE id IN ({ids}) "
            f"AND month=? AND half=?", (slot["month"], slot["half"]))]
        pool = same_slot or pool
    chara_id = (chara_info.get("card_id") or 0) // 100
    pick = pool[(int(turn or 0) + chara_id) % len(pool)]
    return _race_info_for_program(pick)


def _build_race_condition_array(chara_info: dict, turn, race_history=()) -> list:
    """The per-turn race SCHEDULE -- the client's own signal for whether/what
    races are open today. Distinct from command 401's is_enable (just whether
    the race button itself is clickable): this is the actual list the race
    screen renders from. Confirmed via a real capture: empty/absent before
    racing unlocks, exactly ONE entry on a forced/mandatory route-race turn
    (the day's only option, matching _race_for_turn), and a real multi-entry
    pool of eligible single_mode_program ids on every other turn once racing
    is open. That voluntary pool mirrors _voluntary_race_for_turn's own
    eligibility query (month/half calendar slot, fan-gated, excluding every id
    already claimed by a route race) but returns every match instead of
    picking one, since here the client itself needs the full list to render
    a selection screen. The narrowing the real game applies on top of the
    calendar slot -- long flagged here as unidentified -- is race_permission
    (the race's AGE eligibility, see _year_permissions) plus dropping
    program variants (base_program_id != 0); with those two the pool matches
    real captures exactly on 1966 of ~1970 records.

    While _debut_retry_active the list is restricted to MAIDEN races (grade
    800) -- every one whose calendar slot is this turn, not a single forced
    pick. A trainee who lost their debut still trains normally and still
    chooses a race; she simply cannot enter anything above a maiden until she
    wins one, which is exactly how a run can be lost (the later goals need the
    class those higher races grant)."""
    route_ids = tuple(chara_info.get("route_race_id_array") or ())
    forced_turns = _forced_race_turns(route_ids)
    if turn is None:
        return []
    # NO unlock-turn gate. This used to return [] for every turn before
    # min(forced_turns) -- the first MANDATORY route race -- which hid the
    # voluntary races the real server offers earlier: real captures show the
    # debut-window maiden (program 808) already listed at junior turn 9, while
    # the first forced route race is typically turn 11+. Racing opening at all
    # is not a rule that needs encoding: turns 1-8 are April/May of the junior
    # year, and master.mdb simply has no junior-permission program in those
    # calendar slots, so the ordinary eligibility query below already returns
    # [] there -- matching every real turn-1..8 capture.
    if turn in forced_turns:
        race = _race_for_turn(chara_info, turn, race_history)
        return [_race_condition_entry(race["program_id"])] if race else []
    if _debut_retry_active(chara_info, turn, race_history):
        maidens = _maiden_races_for_turn(turn)
        if maidens:
            return [_race_condition_entry(pid) for pid in maidens]
        race = _debut_retry_race(chara_info, turn)
        return [_race_condition_entry(race["program_id"])] if race else []

    trow = master_data.query_one(
        "SELECT month, half, period FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (_URA_TURN_SET_ID, turn))
    if not trow or trow["period"] == 3:
        # Finals window (turns 73-78): no voluntary races exist -- real capture
        # shows an empty array on turn 73; 74/76/78 are forced turns handled
        # above and 75/77 have race entry disabled client-side anyway.
        return []
    fans = chara_info.get("fans", 0)
    # base_program_id != 0 marks a VARIANT of another program (same race
    # instance, different rival field / grade rate) that the real server picks
    # between internally -- the schedule only ever lists the base entry, so
    # every variant is filtered out. Confirmed across the corpus: e.g. senior
    # turn 68 has four fan-eligible perm-4 programs (1080/1084/1098/1099), all
    # variants of program 76, and real lists 76 alone.
    #
    # The route-race exclusion is gone: real captures list goal-race programs
    # in the ordinary pool on non-forced turns (e.g. junior turn 13's
    # 298/946/951), and excluding every route-race id anywhere in master
    # dropped races this trainee never had as a goal at all.
    #
    # This reproduces 1966 of the ~1970 real multi-entry captures exactly. The
    # two known residuals, both left as-is: the November-H1 random_group_id
    # programs (1024-1026), where the real server substitutes one randomly
    # chosen variant group (1110-1118) per career, and a handful of very
    # high-fan G1s real listed to a trainee below their need_fan_count.
    rows = master_data.query(
        "SELECT id FROM single_mode_program WHERE month=? AND half=? AND need_fan_count<=? "
        f"AND race_permission IN {_year_permissions(turn)} AND base_program_id=0",
        (trow["month"], trow["half"], fans))
    return [_race_condition_entry(r["id"]) for r in rows]


RESERVED_RACES_KEY = "reserved_races"  # PER-CAREER deck_num=0 ("Currently Scheduled Races")
RESERVED_DECKS_KEY = "reserved_race_decks"  # ACCOUNT-LEVEL saved presets (deck_num >= 1)
# How many "Agenda N" preset slots to offer. The one real career capture carried
# 8; the live client's own preset picker offers 10, and a slot the server never
# sends cannot be saved into, so the two extra ones ship empty.
RESERVED_DECK_COUNT = 10

# The default preset agendas, lifted from the real career /start capture. They
# are NOT that account's own saves -- the same 8 agendas (near-identical races,
# in the same order) come back for two different real accounts, so they are the
# real server's own suggested schedules. Kept as the starting content of an
# untouched slot; a slot the player saves over is stored per-account and wins
# from then on.
_RESERVED_DECK_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "capture_06d26c"


@functools.lru_cache(maxsize=1)
def _default_reserved_decks() -> tuple:
    """Slots 1..RESERVED_DECK_COUNT as a brand-new account sees them."""
    found: dict = {}
    for path in sorted(_RESERVED_DECK_FIXTURE_DIR.glob("*single_mode_team_start_incoming.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a missing/broken fixture must not break the endpoint
            continue
        for deck in ((doc.get("data") or {}).get("data") or {}).get("reserved_race_array") or []:
            num = deck.get("deck_num")
            if isinstance(num, int) and num > 0 and num not in found:
                found[num] = [{"year": r.get("year"), "program_id": r.get("program_id")}
                              for r in deck.get("race_array") or []]
        if found:
            break
    if not found:
        log.warning("single_mode: no reserved-race deck fixture under %s -- "
                    "serving empty agenda presets", _RESERVED_DECK_FIXTURE_DIR)
    return tuple({"deck_num": n, "deck_name": f"Agenda {n}", "race_array": found.get(n, [])}
                 for n in range(1, RESERVED_DECK_COUNT + 1))


def _reserved_key(race: dict) -> tuple:
    """A reserved race's identity: (year, program_id), NOT program_id alone --
    a real capture showed the SAME program_id recurring across different years
    (an annual prize race entered more than once), so deduping on program_id
    alone would silently drop a legitimate repeat."""
    return (race.get("year"), race.get("program_id"))


def _apply_reserve_delta(races: list, add_array, cancel_array) -> list:
    """Cancels first, then adds -- the client sends both halves of one edit."""
    cancel = {_reserved_key(r) for r in (cancel_array or [])}
    out = [r for r in races if _reserved_key(r) not in cancel]
    have = {_reserved_key(r) for r in out}
    for r in (add_array or []):
        key = _reserved_key(r)
        if r.get("program_id") is not None and key not in have:
            out.append({"year": r.get("year"), "program_id": r.get("program_id")})
            have.add(key)
    return out


def _reserved_decks(full_state: dict) -> list:
    """Preset slots 1..N: the defaults above, overlaid with this account's own
    saves. Account-level ON PURPOSE -- a saved agenda is a reusable template
    that outlives the career it was written in (only deck 0 is per-career)."""
    saved = full_state.get(RESERVED_DECKS_KEY) or {}
    out = []
    for deck in _default_reserved_decks():
        row = saved.get(str(deck["deck_num"]))
        if not isinstance(row, dict):
            out.append(copy.deepcopy(deck))
            continue
        out.append({"deck_num": deck["deck_num"],
                    "deck_name": row.get("deck_name") or deck["deck_name"],
                    "race_array": copy.deepcopy(row.get("race_array") or [])})
    # A slot beyond the default count (a client offering more than we assume) is
    # still honoured once it has actually been saved into.
    known = {d["deck_num"] for d in out}
    for key in sorted(saved, key=lambda k: int(k) if str(k).isdigit() else 0):
        row = saved[key]
        num = int(key) if str(key).isdigit() else 0
        if num > 0 and num not in known and isinstance(row, dict):
            out.append({"deck_num": num, "deck_name": row.get("deck_name") or f"Agenda {num}",
                        "race_array": copy.deepcopy(row.get("race_array") or [])})
    return out


def _reserved_race_array(full_state: dict, deck0_name: str = "") -> list:
    """The whole wire array: deck 0 (this career's schedule) + the presets."""
    deck0 = {"deck_num": 0, "deck_name": deck0_name,
             "race_array": copy.deepcopy(list(full_state.get(RESERVED_RACES_KEY) or []))}
    return [deck0] + _reserved_decks(full_state)


def handle_multi_race_reserve(payload: dict) -> dict:
    """single_mode/multi_race_reserve -- the client PUSHES its own already-
    computed schedule of upcoming races here. Confirmed via real captures:
    {deck_num, deck_name, add_race_array, cancel_race_array} (flat on the
    wire, though SingleModeMultiRaceReserveRequestCommon nests it under
    multi_race_reserve_deck -- both shapes are accepted below).

    deck_num 0 is "Currently Scheduled Races", THIS career's own agenda;
    deck_num >= 1 are the saveable "Agenda N" presets. Both used to land in
    one account-level list, which is three bugs at once:

      * deck 0 was stored on the ACCOUNT, not the career, so the agenda set in
        one career was still sitting there in the next one (the key was not in
        career_state_keys(), so clear_active_career never wiped it). It is now,
        so a new career starts with an empty schedule.
      * deck_num was ignored entirely, so saving a preset to "Agenda 3" did not
        save a preset -- it dumped those races straight into the live career
        schedule, and the preset itself never persisted at all.
      * the response was an empty envelope, but
        SingleModeMultiRaceReserveResponse.CommonResponse declares
        reserved_race_array and the client's UpdateReservedData feeds on it.
        With nothing there the client kept whatever start/load last handed it
        -- and start/load are the ONLY two responses in the whole career that
        carry the field -- so a freshly-set agenda showed up nowhere until the
        career was exited and re-entered. The full array now rides back on
        every reserve call, which is what makes the edit visible immediately.
    """
    viewer_id = payload["viewer_id"]
    req = payload.get("multi_race_reserve_deck")
    if not isinstance(req, dict):
        req = payload
    try:
        deck_num = int(req.get("deck_num") or 0)
    except (TypeError, ValueError):
        deck_num = 0
    full_state = state_store.get_state(viewer_id) or {}

    if deck_num <= 0:
        full_state[RESERVED_RACES_KEY] = _apply_reserve_delta(
            list(full_state.get(RESERVED_RACES_KEY) or []),
            req.get("add_race_array"), req.get("cancel_race_array"))
    else:
        current = next((d for d in _reserved_decks(full_state) if d["deck_num"] == deck_num),
                       {"deck_name": f"Agenda {deck_num}", "race_array": []})
        # deck_name arrives on its own for a plain rename (RequestMultiRaceReserve
        # (deckNum, deckName, ...)), with empty add/cancel arrays -- so an absent
        # or blank name must keep the slot's existing one, not erase it.
        name = req.get("deck_name")
        saved = dict(full_state.get(RESERVED_DECKS_KEY) or {})
        saved[str(deck_num)] = {
            "deck_name": (name.strip() if isinstance(name, str) and name.strip()
                          else current["deck_name"]),
            "race_array": _apply_reserve_delta(current["race_array"],
                                               req.get("add_race_array"),
                                               req.get("cancel_race_array")),
        }
        full_state[RESERVED_DECKS_KEY] = saved

    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"reserved_race_array": _reserved_race_array(full_state)}}


def _build_reserved_race_array(template: list, full_state: dict) -> list:
    """reserved_race_array as start/load ship it: deck 0 is whatever the client
    has pushed for THIS career via multi_race_reserve, decks 1+ are the preset
    agendas (defaults, overlaid with this account's saves). The captured
    fixture's own decks are no longer replayed verbatim -- they are the seed
    content of _default_reserved_decks() instead, so a slot the player saves
    over actually stays saved.

    Only the deck-0 LABEL is taken from the template: the /load fixture leaves
    it "" while the client's own request calls it "Currently Scheduled Races",
    so whatever the fixture family uses is preserved rather than invented here.

    CAUTION: reserved_race_array is one of the fields flagged in
    sync_chara_info's docstring as a suspect in an earlier hard client crash
    (blanket-copying every nested single_mode_load_common field at once) --
    unlike race_history/race_condition_array (individually verified safe
    already), this one has NOT been individually verified against a live
    client yet."""
    if not template:
        return template
    deck0_name = next((d.get("deck_name", "") for d in template
                       if isinstance(d, dict) and d.get("deck_num") == 0), "")
    return _reserved_race_array(full_state, deck0_name)


_RACE_TITLE_FILLER = frozenset({"race"})


def _race_name_tokens(name: str | None) -> list:
    """Lowercase word tokens of a race name ('Asahi Hai F.S.' -> asahi hai f s)."""
    if not name:
        return []
    s = str(name).replace("\\n", " ").replace("\n", " ")
    return ["".join(ch for ch in w if ch.isalnum())
            for w in s.lower().replace(".", " ").replace("(", " ").replace(")", " ").split()
            if any(ch.isalnum() for ch in w)]


def _race_name_variants(name: str | None) -> list:
    """The forms a story title might use for this race: the full name, the
    parenthetical alone, and the part before it. master says 'Tokyo Yushun
    (Japanese Derby)' while the story says 'Before the Japanese Derby'."""
    if not name:
        return []
    s = str(name)
    out = [s]
    if "(" in s and ")" in s:
        inside = s[s.index("(") + 1:s.rindex(")")]
        outside = (s[:s.index("(")] + s[s.rindex(")") + 1:]).strip()
        # 'Tenno Sho (Spring)' is also written 'Spring Tenno Sho'.
        out += [inside, outside, f"{inside} {outside}"]
    return [v for v in out if v.strip()]


def _race_title_matches(story_part: str | None, race_name: str | None) -> bool:
    """Whether a story title's race portion names this race.

    Story titles ABBREVIATE by truncating trailing words: 'Aoi S.' for 'Aoi
    Stakes', 'Mile Ch.' for 'Mile Championship', 'Asahi Hai F.S.' for 'Asahi
    Hai Futurity Stakes', 'Saudi Arabia R.C.' for 'Saudi Arabia Royal Cup' --
    so each story token must be a PREFIX of the corresponding race token. An
    exact match found nothing for these, leaving those races with no story, no
    goal banner and no reward (live-reported on Curren Chan's Aoi Stakes; the
    audit found 57 goal races affected across the roster)."""
    st = _race_name_tokens(story_part)
    if not st:
        return False
    # A few trainees' titles append a generic word ('After the Debut RACE:').
    if len(st) > 1 and st[-1] in _RACE_TITLE_FILLER:
        st = st[:-1]
    for variant in _race_name_variants(race_name):
        rt = _race_name_tokens(variant)
        # EITHER side may be the abbreviated one: master writes 'M.C. Nambu
        # Hai' where the story spells out 'Mile Ch. Nambu Hai'.
        if len(st) == len(rt) and all(r.startswith(s) or s.startswith(r)
                                      for s, r in zip(st, rt)):
            return True
    return False


def _find_race_story_ids(chara_id: int, prefix: str, race_name: str) -> list:
    """Story ids in chara_id's own range (50<chara_id><suffix>) for '{prefix}
    {race_name}' -- e.g. prefix='Before the ', race_name='Kisaragi Sho'. Every
    such title is duplicated: once in a static reference block (Before +200s /
    After +300s) and once in the real chronological chain used during an actual
    run (+400s+), which ALWAYS has the higher story_id (the reference block
    comes first). A race with a win/lose branch has two chronological entries;
    the WIN-flavored one is always the lower-numbered of the pair (verified
    against every real branching race: 'Good News' before 'Lessons Learned',
    'Next Up...' before 'Keep Your Head Up!', etc.) -- so `ids[half:]`, kept in
    ascending order, is exactly [win, lose] or [the single entry]."""
    base = 500000000 + chara_id * 1000
    if not race_name:
        return []
    # Match on a NORMALIZED race name, not a raw LIKE: the story titles
    # abbreviate ('After the Aoi S.: ...' for the race master calls 'Aoi
    # Stakes'), so an exact prefix match silently found nothing and the race
    # played with no before/after story, no goal banner and no reward
    # (live-reported for Curren Chan's Aoi Stakes -- it affected EVERY race
    # ending in 'Stakes', i.e. most of a sprinter's card).
    rows = master_data.query(
        "SELECT [index], text FROM text_data WHERE category=181 "
        "AND [index]>=? AND [index]<? AND text LIKE ? ORDER BY [index]",
        (base, base + 1000, f"{prefix}%"))
    ids = [r["index"] for r in rows
           if _race_title_matches((r["text"] or "")[len(prefix):].split(":")[0], race_name)]
    # A title existing in text_data does NOT mean the story exists: the +400s
    # chronological block has a 'Before the Debut' entry for all 166 trainees
    # with NO single_mode_story_data row behind it. Serving one of those told
    # the client to play a story asset that isn't there -- the debut-race
    # softlock, live-reported. Drop dead ids BEFORE the win/lose split so the
    # split still lines up on real entries.
    ids = [i for i in ids if _story_exists(i)]
    half = len(ids) // 2
    return ids[half:] if half else ids


@functools.lru_cache(maxsize=4096)
def _story_exists(story_id: int) -> bool:
    """Whether master has an actual story row for this id. A title in
    text_data is not enough -- see _find_race_story_ids."""
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story_id,)))


@functools.lru_cache(maxsize=1024)
def _story_or_short_exists(story_id: int) -> bool:
    """Like _story_exists, but also accepts a SHORT story id.

    single_mode_story_data carries both a story_id (3xx band) and a
    short_story_id (4xx band), and the official server serves the SHORT one --
    every Grand Live scenario beat in the capture is a short id, so a plain
    _story_exists check calls all of them missing. Kept separate from
    _story_exists because that one deliberately filters the 5xx trainee band by
    story_id and must not start matching on the other column."""
    return bool(master_data.query_one(
        "SELECT story_id FROM single_mode_story_data "
        "WHERE story_id=? OR short_story_id=?", (story_id, story_id)))


def _retarget_race_prep_event(unchecked_event_array, chara_id: int, race_name: str | None = None) -> None:
    """Retarget a captured pre-race cutscene (e.g. 'Before the Debut') to a
    DIFFERENT trainee AND (when race_name is known) the trainee's ACTUAL
    scheduled race, not whichever race the fixture happened to be captured
    with. Without this, every race turn showed the captured fixture's own
    trainee (Grass Wonder) racing the captured fixture's own race (the debut),
    regardless of who's actually running or which turn it is."""
    if not unchecked_event_array or not chara_id:
        return
    ev = unchecked_event_array[0]
    old_story = ev.get("story_id")
    if not old_story:
        return
    if race_name:
        ids = _find_race_story_ids(chara_id, "Before the ", race_name)
        story_id = ids[0] if ids else None
    else:
        title_row = master_data.query_one(
            "SELECT text FROM text_data WHERE category=181 AND [index]=?", (old_story,))
        story_id = None
        if title_row:
            base = 500000000 + chara_id * 1000
            # Prefer a non-200 match (the chronological block) but only if the
            # story actually EXISTS -- the old query excluded suffix 200
            # unconditionally, which for 'Before the Debut' left only the dead
            # +400s id and softlocked the client. Fall back to suffix 200,
            # which is the real story for every trainee.
            rows = master_data.query(
                "SELECT [index] FROM text_data WHERE category=181 AND [index]>=? AND [index]<? "
                "AND text=? ORDER BY ([index] % 1000 = 200), [index]",
                (base, base + 1000, title_row["text"]))
            story_id = next((r["index"] for r in rows if _story_exists(r["index"])), None)
    if not story_id:
        # VOLUNTARY races have no per-chara 'Before the X' story (those only
        # exist for the trainee's own goal races), so the lookup fails and the
        # fixture's own trainee (Grass Wonder) leaked through -- live-reported.
        # Fall back to the trainee's own challenge-race story (suffix 600,
        # then 601: the chara's real generic race-flavor events).
        base = 500000000 + chara_id * 1000
        for suffix in (600, 601):
            if master_data.query_one(
                    "SELECT story_id FROM single_mode_story_data WHERE story_id=?",
                    (base + suffix,)):
                story_id = base + suffix
                break
    # Last line of defence: never point the client at a story that isn't
    # there. Leaving the captured story_id shows the wrong trainee's cutscene,
    # which is cosmetic; a missing asset softlocks the run.
    if story_id and _story_exists(story_id):
        ev["chara_id"] = chara_id
        # Serve the SHORT id. _find_race_story_ids and the title lookup both
        # return the `story_id` column -- the static reference -- but the real
        # server puts `short_story_id` on the wire for every one of these (see
        # event_engine.wire_story_id). Measured against the 20 real URA Finale
        # careers: real serves 501025408..416 here, we were serving the raw
        # 501025200..208 they are the long form of, in every career.
        ev["story_id"] = event_engine.wire_story_id(story_id)


def _post_race_event_entry(chara_info: dict, race_name: str, won: bool = True,
                           choices: int = 2) -> dict | None:
    """A dynamically-resolved 'After the X' post-race event for the trainee's
    JUST-COMPLETED race, matching the real captured shape (event_id 1101x,
    play_timing 3, single acknowledge choice). Picks the win-flavored branch
    when `won`, else the lose-flavored one when the race actually has a
    win/lose branch (_find_race_story_ids returns [win, lose] in that case --
    see its docstring); a non-branching race only ever has the one entry.
    None if no such story exists for this chara/race (falls back to showing
    nothing, same as before this feature existed, rather than a wrong guess)."""
    chara_id = (chara_info.get("card_id") or 0) // 100
    if not chara_id:
        return None
    ids = _find_race_story_ids(chara_id, "After the ", race_name)
    if not ids:
        return None
    if not won and len(ids) > 1:
        ids = ids[1:]
    # show_clear / show_clear_sort_id are the goal-banner fields; the caller
    # overwrites them with the REAL cleared-count and this goal's sort_id via
    # _mark_goal_progress (they were hardcoded 1/1, so every race claimed to
    # clear goal #1).
    # `choices` slots. A VOLUNTARY race's recovery event has TWO (both pay the
    # same stats and differ only in energy cost -- see _post_race_choices);
    # binding the reward to this event is what stopped it landing on an
    # unrelated later screen (#17). A GOAL race passes choices=1: its reward is
    # fixed, costs no energy and offers nothing to pick.
    return {
        # story_id: the short id, same rule as _retarget_race_prep_event above --
        # real serves 501025419..428 where we were serving 501025305..314.
        "event_id": 11168, "chara_id": chara_id,
        "story_id": event_engine.wire_story_id(ids[0]), "play_timing": 3,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [
                {"select_index": i, "receive_item_id": 0, "target_race_id": 0,
                 "gain_select_id_index": i, "select_icon": 0}
                for i in range(1, max(1, choices) + 1)],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


# --- POST-RACE REWARDS -- USER-SUPPLIED TABLE (2026-07-28) -------------------
# Two completely different payouts, and conflating them was the live bug ("the
# debut gave the same rewards as the non-debut thing, and it took away energy"):
#
#   GOAL races -- the trainee's own scheduled races, the debut (and its
#     maiden retries) and the URA finals rounds -- pay their OWN fixed reward
#     (_RACE_REWARD_BASE's all-five + SP), cost ZERO energy and offer NO
#     choice. User's words: "zero energy, no choice, like other career goal
#     rewards."
#   VOLUNTARY races pay the placement x grade table below, with a two-option
#     energy split -- and, 20% of the time, the Reporter's coverage INSTEAD.
#
# Everything here is ground truth from the user; do not re-guess it. Stats and
# SP are scaled by the deck's race bonus (as everywhere else).
#
# Grade columns: G1 / G2-G3 / (Pre-)OP-and-below.
_POST_RACE_GRADE_COL = {100: 0, 200: 1, 300: 1}   # anything else -> column 2
# placement bucket -> (ONE random stat by grade, SP by grade)
_POST_RACE_TABLE = {
    "win":    ((10, 8, 5), (45, 35, 30)),   # Victory!      (1st)
    "solid":  ((8, 5, 3),  (45, 35, 30)),   # Solid Showing (2nd-5th)
    "defeat": ((4, 3, 0),  (25, 20, 10)),   # Defeat        (6th or worse)
}
# placement bucket -> (top option's energy, bottom option's two possible ones).
# The bottom option is a coin-flip between the two (uniform -- see _roll_branch).
# (top option, (bottom option's two coin-flip branches)) per placement.
#
# CORPUS-MEASURED 2026-09-09 against the 1,974 real career logs in
# captures/bot_logs -- the vital delta across each post-race check_event, taken
# from the RES either side of it and dropping any race that bottomed out at 0
# (a floor-clipped delta is not the cost). n = 32,643:
#
#   Victory!       top -20  (98% of 23,789)
#   Solid Showing  top -25  (98% of  7,872)   bottom -30 / -10  (41/59% of 63)
#   Defeat         top -30  (93% of    834)
#
# So the bottom option is a coin flip between (top - 5) and a flat -10, and the
# tops run -20/-25/-30 by placement. USER-CONFIRMED for the Victory! row ("the
# top is -20, the bottom is -25 or -10"), which is the one whose bottom branch
# the bot never picked often enough to measure; Defeat's bottom follows the
# same rule and is the only cell neither source pins directly.
#
# Every top used to be 5 CHEAPER than this (-15/-20/-25) and Victory!'s bottom
# was (-5, -20) rather than (-25, -10), so racing was systematically less
# tiring than the real game -- which matters most in Trackblazer, where a
# career runs 37.9 races.
_POST_RACE_ENERGY = {
    "win":    (-20, (-25, -10)),
    "solid":  (-25, (-30, -10)),
    "defeat": (-30, (-35, -10)),
}
# The trainee's OWN story for each placement -- master titles them exactly as
# the user's table does ('Victory!' / 'Solid Showing' / 'Defeat'), and all 62
# trainees have all three (verified against single_mode_story_data, not just
# text_data -- a title alone is what softlocked the debut twice).
_POST_RACE_STORY_SUFFIX = {"win": 708, "solid": 709, "defeat": 710}
_POST_RACE_EVENT_ID = 11169        # voluntary-race result event
_ETSUKO_EVENT_ID = 11170           # ... replaced by the Reporter's coverage
_ETSUKO_COVERAGE_CHANCE = 0.20     # user-supplied: replaces the WHOLE event
_ETSUKO_STORY_WIN = 400000035      # "Etsuko's Elated Coverage"
_ETSUKO_STORY_LOSE = 400000036     # "Etsuko's Exhaustive Coverage"
_ETSUKO_TARGET, _ETSUKO_CHARA = 103, 9003   # the Reporter (see NPC_UNLOCKS 1016)
_ETSUKO_WIN_FANS = 500
_STAT_CHOICES = ("speed", "stamina", "power", "guts", "wisdom")


def _placement_bucket(rank) -> str:
    """The user's three result tiers, which are also the three story titles."""
    rank = int(rank or 1)
    if rank <= 1:
        return "win"
    return "solid" if rank <= 5 else "defeat"


def _post_race_amounts(chara_info: dict, program_id, bucket: str) -> tuple:
    """(one random stat's gain, SP gain) for a voluntary race, race-bonus scaled.
    Integer math throughout -- floats put 45 * 1.4 at 62.999..."""
    grade = _race_grade_of(program_id) if program_id else None
    col = _POST_RACE_GRADE_COL.get(grade, 2)
    stats, sps = _POST_RACE_TABLE[bucket]
    pct = int(_race_bonus_pct(chara_info))
    return (stats[col] * (100 + pct) // 100, sps[col] * (100 + pct) // 100)


_DISTANCE_TYPE = {"short": 1, "mile": 2, "middle": 3, "long": 4}


@functools.lru_cache(maxsize=64)
def _race_related_skills(ground: int, category: str) -> tuple:
    """General skills whose own activation condition names this race's distance
    type or ground -- the pool the post-race 'hint for a skill related to the
    race' draws from. skill_data.condition_N is an expression string like
    'distance_type==2&phase_random==1', so a substring match on the clause is
    exactly the relation we want."""
    dt = _DISTANCE_TYPE.get(category)
    if not dt:
        return ()
    d, g = f"%distance_type=={dt}%", f"%ground_type=={ground}%"
    return tuple(r["id"] for r in master_data.query(
        "SELECT id FROM skill_data WHERE is_general_skill=1 AND disable_singlemode=0 "
        "AND (condition_1 LIKE ? OR condition_2 LIKE ? "
        "     OR condition_1 LIKE ? OR condition_2 LIKE ?)", (d, d, g, g)))


# The race events never show their real numbers -- the client renders
# 'Stat gains based on race grade' and 'Chance to gain a random skill' instead
# (live-reported: we were leaking the exact stat/SP figures AND naming the
# skill, e.g. 'Power +15 / Skill Pts +67 / Eager hint lvl +1'). The values are
# still real and still applied; only the PREVIEW is generic. See
# event_engine._encode_effect's hidden/display_* overrides.
_RACE_HINT_CHANCE = 0.35   # "Chance to" -- the real odds are NOT known; flagged


# GOLD CITY's unique trait, and the only thing the `gold_city_race` secret-event
# note actually says: "From Senior Year Early May, the rewards for NON-OBJECTIVE
# races change." What changes is the SPREAD, not the amounts -- GameTora
# publishes her Victory! / Solid Showing / Defeat beats (the only trainee page
# in the cache that lists them at all, which is why they read as 'secret'), and
# its numbers match _POST_RACE_TABLE column for column while the stat lines do
# not: a win pays every stat instead of one, a placing pays two instead of one.
# User-confirmed 2026-09-06 ("this genuinely happens ... only for gold city").
#
# Senior, Early May = turn 49 + (5 - 1) * 2 = 57, by the calendar the whole
# scenario uses (24 turns a year, Early/Late per month).
_GOLD_CITY_CHARA = 1040
_GOLD_CITY_BUFF_TURN = 57
# placement bucket -> the stat line her buffed reward pays instead of one stat.
_GOLD_CITY_STAT_SPREAD = {
    "win":    ("all_stats", 1),        # +N to all five
    "solid":  ("random_stats", 2),     # +N to two random stats
    "defeat": ("random_stats", 2),
}


def _post_race_stat_effect(chara_info: dict, bucket: str, stat: int) -> dict | None:
    """The stat half of a voluntary race's payout: ONE random stat, or Gold
    City's widened spread once her trait is live."""
    if not stat:
        return None
    chara = (chara_info.get("card_id") or 0) // 100
    if (chara == _GOLD_CITY_CHARA
            and (chara_info.get("turn") or 0) >= _GOLD_CITY_BUFF_TURN):
        kind, count = _GOLD_CITY_STAT_SPREAD[bucket]
        eff = {"type": kind, "value": f"+{stat}"}
        if kind == "random_stats":
            eff["stat_count"] = count
        return eff
    return {"type": random.choice(_STAT_CHOICES), "value": f"+{stat}"}


def _race_reward_effects(stat_effect: dict | None, sp: int) -> list:
    """The stat + SP pair of a race reward, applied for real but collapsed into
    the single generic 'Stat gains based on race grade' preview row."""
    out = []
    if stat_effect:
        out.append(dict(stat_effect))
    if sp:
        out.append({"type": "skill_points", "value": f"+{sp}"})
    if out:
        # ONE generic row covers the whole payout; the rest apply silently, or
        # the player would see 'Stat gains based on race grade' followed by a
        # second line spelling the SP out.
        out[0]["display_id"] = event_engine.DISPLAY_RACE_GRADE_STATS
        for e in out[1:]:
            e["hidden"] = True
    return out


def _race_skill_hint(chara_info: dict, program_id) -> dict | None:
    """A CHANCE at a hint for a random race-related skill the trainee doesn't
    have yet -- every post-race option carries one. It is a chance, not a
    guarantee: the client's own label is 'Chance to gain a random skill', and
    the skill is deliberately not named in the preview. None when the race
    can't be resolved or the pool is exhausted."""
    course = _program_course(program_id) if program_id else None
    if not course:
        return None
    pool = _race_related_skills(*course)
    if not pool:
        return None
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or ()}
    candidates = [s for s in pool if s not in learned]
    if not candidates:
        return None
    return {"type": "skill_hint", "skill_id": random.choice(candidates),
            "value": 1, "chance": _RACE_HINT_CHANCE,
            "display_id": event_engine.DISPLAY_RANDOM_SKILL_CHANCE}


def _post_race_choices(chara_info: dict, program_id, rank) -> dict:
    """The VOLUNTARY race's 2-choice result event: identical stat/SP/hint on
    both options, different energy costs (the bottom one a coin-flip between
    two amounts). Returned in the engine's own event shape so the normal
    preview/commit path serves and applies it."""
    bucket = _placement_bucket(rank)
    stat, sp = _post_race_amounts(chara_info, program_id, bucket)
    # the (Pre-)OP defeat column really is +0 stat -- pay only the SP
    shared = _race_reward_effects(_post_race_stat_effect(chara_info, bucket, stat), sp)
    hint = _race_skill_hint(chara_info, program_id)
    if hint:
        shared.append(hint)
    top, bottom = _POST_RACE_ENERGY[bucket]
    return {"choices": [
        {"effects": shared + [{"type": "energy", "value": str(top)}]},
        {"outcomes": [shared + [{"type": "energy", "value": str(e)}] for e in bottom],
         "random_either": True},
    ]}


def _etsuko_bond(value: int) -> dict:
    return {"type": "npc_bond", "target_id": _ETSUKO_TARGET,
            "chara_id": _ETSUKO_CHARA, "value": f"{value:+d}"}


def _etsuko_coverage(chara_info: dict, program_id, rank) -> tuple:
    """(story_id, event) for the Reporter's post-race coverage. A WIN gets the
    single-outcome 'Elated' variant (and 500 fans); anything else gets
    'Exhaustive', which pays the DEFEAT stat/SP column throughout regardless of
    where you actually placed -- both user-supplied."""
    won = int(rank or 1) <= 1
    bucket = "win" if won else "defeat"
    stat, sp = _post_race_amounts(chara_info, program_id, bucket)
    # The Reporter REPLACES the post-race event, so Gold City's widened spread
    # has to hold here too -- otherwise her trait silently lapses on the 20% of
    # races Etsuko happens to cover.
    gains = _race_reward_effects(_post_race_stat_effect(chara_info, bucket, stat), sp)
    hint = _race_skill_hint(chara_info, program_id)
    if hint:
        gains.append(hint)
    if won:
        return (_ETSUKO_STORY_WIN, {"choices": [{"effects": gains + [
            {"type": "energy", "value": "-15"}, {"type": "mood", "value": "+1"},
            {"type": "fans", "value": f"+{_ETSUKO_WIN_FANS}"}, _etsuko_bond(15)]}]})
    return (_ETSUKO_STORY_LOSE, {"choices": [
        # top: a coin-flip between a bad interview and a good one
        {"outcomes": [
            gains + [{"type": "energy", "value": "-25"},
                     {"type": "mood", "value": "-1"}, _etsuko_bond(-10)],
            gains + [{"type": "energy", "value": "-15"},
                     {"type": "mood", "value": "+1"}, _etsuko_bond(15)]],
         "random_either": True},
        {"effects": gains + [{"type": "energy", "value": "-20"}, _etsuko_bond(10)]},
    ]})


def _goal_race_reward(chara_info: dict, program_id, finals_round, rank) -> dict:
    """A GOAL race's own reward: the fixed all-five + SP payout, ZERO energy,
    ONE acknowledge choice."""
    reward = _race_reward_for(chara_info, program_id, finals_round, int(rank or 1))
    effects = [{"type": "all_stats", "value": f"+{reward['stats']}"}]
    if reward.get("sp"):
        effects.append({"type": "skill_points", "value": f"+{reward['sp']}"})
    return {"choices": [{"effects": effects}]}


def _is_goal_race(chara_info: dict, program_id, turn, race_history=()) -> bool:
    """Whether this race is one of the trainee's OWN goals. Derived from the
    program rather than from which resolver produced it, because a
    client-supplied program_id bypasses those resolvers entirely."""
    if not program_id:
        return False
    if _finals_round_for_program(program_id):
        return True
    if _race_grade_of(program_id) == _DEBUT_GRADE:
        return True   # the debut, and every maiden retry of it, is goal #1
    goal = _race_for_turn(chara_info, turn, race_history) if chara_info else None
    return bool(goal and goal.get("program_id") == program_id)


def _voluntary_race_event(full_state: dict, chara_info: dict, program_id, rank):
    """(unchecked_event_array entry, event, title) for a VOLUNTARY race's
    result. Voluntary races used to produce nothing at all -- the old code
    keyed the post-race event on `race_story_name`, which only the trainee's
    own goal races have, so a voluntary race got no event AND (the reward being
    bound to the event since #17) no payout whatsoever.

    The 20% Reporter-coverage roll lives HERE, not inside _post_race_choices:
    it replaces the whole event, story and all. She has to be unlocked for it
    (she joins the turn after the debut is won -- see NPC_UNLOCKS 1016).

    entry is None when the story is missing, in which case the caller pays the
    reward directly rather than pointing the client at an absent asset."""
    chara = (chara_info.get("card_id") or 0) // 100
    unlocked = [list(x) for x in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
    if ([_ETSUKO_TARGET, _ETSUKO_CHARA] in unlocked
            and random.random() < _ETSUKO_COVERAGE_CHANCE):
        story_id, ev = _etsuko_coverage(chara_info, program_id, rank)
        if _story_exists(story_id):
            return (event_engine.career_event_entry(
                ev, _ETSUKO_EVENT_ID, story_id, chara_id=_ETSUKO_CHARA,
                support_card_id=0, play_timing=3),
                ev, event_engine.event_title(story_id))
    ev = _post_race_choices(chara_info, program_id, rank)
    suffix = _POST_RACE_STORY_SUFFIX[_placement_bucket(rank)]
    story_id = 500000000 + chara * 1000 + suffix
    if not chara or not _story_exists(story_id):
        return None, ev, None
    # All three placement reactions are pinned by the official captures now --
    # 'Victory!' 708 -> 7005, 'Solid Showing' 709 -> 7006, 'Defeat' 710 -> 7007
    # -- and the client keys its presentation off the event_id. _POST_RACE_EVENT_ID
    # stays as the fallback for a trainee whose suffix somehow isn't mapped.
    return (event_engine.career_event_entry(
        ev, _shared_beat_event_id(suffix) if suffix in _SHARED_BEAT_EVENT_IDS
        else _POST_RACE_EVENT_ID,
        story_id, chara_id=chara, support_card_id=0, play_timing=3),
        ev, event_engine.event_title(story_id))


# URA Finals scenario events: per round, the pre-race cutscene served in the
# race_entry response and the post-race one served in race_out.
#
# CORPUS-VERIFIED against the 2053 real career logs in captures/bot_logs. Every
# URA career that reached the finale acks exactly these six, in this order and
# on these turns -- 11000/11003 on turn 74, 11001/11004 on 76, 11002/11005 on
# 78 -- all with chara_id 0 and choice_number 0. (Only four such careers are in
# the corpus, but all four agree exactly.)
#
# Round 3 previously served 102002/102003. Those have ZERO acks anywhere in the
# corpus: they are master.mdb stories 400001045/400001046 "Before/After the URA
# Finale FINAL", which belong to the 102xxx Happy Meek duel group (102001
# "Happy Meek's Challenge!" 98 acks, 102005 "I'm Here to Challenge You" 16),
# not to the ordinary finale. The ordinary round 3 is 400001035/400001036
# "Before/After the URA Finale FINALS", continuing 400001031..400001034 exactly
# as the id pattern predicts; single_mode_story_data carries short_story_id 0
# for all six, so the wire value is the plain story_id.
#
# has_choice is False on all three pres now: choice_number is 0 for every one
# of the 23 real acks, and the "turn 78 uniquely acknowledges" claim came from
# the same bad source as the 102002 ids. Post sort ids 9/10/11 are unchanged.
_FINALS_EVENTS = {
    1: {"pre": (11000, 400001031, False), "post": (11003, 400001032, 9)},
    2: {"pre": (11001, 400001033, False), "post": (11004, 400001034, 10)},
    3: {"pre": (11002, 400001035, False), "post": (11005, 400001036, 11)},
}
# Career-ending chain after the turn-78 finals race_out (captured order/shapes):
# scenario epilogue -> URA wrap-up -> the trainee's own ending story
# (500000000 + chara*1000 + 111, confirmed 501011111 for chara 1011).
_ENDING_CHAIN = [
    (10, 400000091, 3, 0),   # (event_id, story_id, select_index, receive_item_id)
    (4, 400001412, 2, 2),
]
_ENDING_CHARA_EVENT_ID = 6

# --- END-OF-RUN PAYOUTS (#40-#43) --------------------------------------------
# Master has the STORIES for these but no reward table (same class as the race
# rewards): Etsuko has two coverage variants and the Director five 'A Present
# from Director Akikawa!' variants, both keyed by BOND.
#   400000035 "Etsuko's Elated Coverage"     high bond
#   400000036 "Etsuko's Exhaustive Coverage" low bond
#   400002031..400002035 Director's present, five bond tiers
# Amounts below are scaled off the bond thresholds the Appraisal series already
# uses (0/40/70/90). USER TO CONFIRM -- flagged unverified.
_REPORTER_ENDING = [(60, 400000035, 10, 40), (0, 400000036, 5, 20)]
_DIRECTOR_ENDING = [(90, 400002035, 12, 45), (70, 400002034, 10, 40),
                    (40, 400002033, 8, 30), (20, 400002032, 6, 25),
                    (0, 400002031, 5, 20)]
# The trainee's OWN final event -- USER-SUPPLIED exact values.
_TRAINEE_ENDING_STATS = 5
_TRAINEE_ENDING_SP = 20
ENDING_PAYOUT_KEY = "ending_payouts"   # {str(event_id): {"stats": n, "sp": n}}

# 'A Super Successful Event!' (story 400001030, served in _ENDING_CHAIN by its
# short id 400001412 under event_id 4). USER-SUPPLIED: all stats +15 and SP +50
# when the DIRECTOR's friendship gauge is maxed; "if you do not have max gauge
# with the director, only get +10 to all stats" -- i.e. no SP at all below max.
# It had no payout entry at all before, so it paid nothing either way.
_SUPER_SUCCESS_EVENT_ID = 4
_SUPER_SUCCESS_MAX = (15, 50)      # (all-stats, SP) at a maxed Director gauge
_SUPER_SUCCESS_BASE = (10, 0)      # ...and below it
_DIRECTOR_TARGET = 102
_DIRECTOR_MAX_BOND = 100           # the gauge is full at 100 (== _BOND_MAX, which
                                   # is defined much further down this module)


def _ending_payout_events(full_state: dict, chara_info: dict) -> list:
    """The bond-scaled end-of-run payout events (reporter, Director) plus the
    trainee's own final event, as (entry, stats, sp). Only NPCs the player
    actually unlocked contribute.

    The Director's sendoff ("A Present from Director Akikawa!") is URA
    exclusive -- user-supplied 2026-08-16, same call as _ENDING_CHAIN's Super
    Successful Event. The Reporter's is confirmed fine in both."""
    out = []
    unlocked = {n[0] for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])}
    has_director = scenarios.for_chara(chara_info).has_director_ending
    for target, table in ((103, _REPORTER_ENDING), (102, _DIRECTOR_ENDING)):
        if target not in unlocked or (not has_director and target == _DIRECTOR_TARGET):
            continue
        bond = _bond_of(chara_info, target)
        for need, story, stats, sp in table:
            if bond >= need:
                out.append((_scenario_event_entry(
                    story % 100000, story, play_timing=3,
                    choices=[_ack_choice()]), stats, sp))
                break
    chara_id = (chara_info.get("card_id") or 0) // 100
    if chara_id:
        # HOT SPRING GETAWAY -- only if the year-end raffle handed over the Hot
        # Spring Ticket (HOT_SPRING_KEY), and only immediately BEFORE her own
        # final event, which is where the player reported it belongs. Its
        # event_id follows the same story%100000 convention the two NPC
        # sendoffs above use, so it can never collide with
        # _ENDING_CHARA_EVENT_ID and gets its own ENDING_PAYOUT_KEY row.
        #
        # Story-only: no reward table exists for it and none was supplied, and
        # this module does not invent payouts (see _ending_payout_events' own
        # #40-#43 note and grand_live's _GL_STORY_ONLY).
        hot_spring = 500000000 + chara_id * 1000 + _HOT_SPRING_SUFFIX
        if full_state.get(HOT_SPRING_KEY) and _story_exists(hot_spring):
            out.append((_scenario_event_entry(
                hot_spring % 100000, hot_spring, play_timing=3,
                choices=[_ack_choice()], chara_id=chara_id), 0, 0))
        out.append((_scenario_event_entry(
            _ENDING_CHARA_EVENT_ID, 500000000 + chara_id * 1000 + 111,
            play_timing=3, choices=[_ack_choice()], chara_id=chara_id),
            _TRAINEE_ENDING_STATS, _TRAINEE_ENDING_SP))
    return out


def _ack_choice(select_index=1, receive_item_id=0):
    return {"select_index": select_index, "receive_item_id": receive_item_id,
            "target_race_id": 0, "gain_select_id_index": 1, "select_icon": 0}


def _scenario_event_entry(event_id, story_id, play_timing, choices, show_clear=0,
                          show_clear_sort_id=0, chara_id=0) -> dict:
    return {
        "event_id": event_id, "chara_id": chara_id,
        "story_id": event_engine.wire_story_id(story_id),
        "play_timing": play_timing,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": show_clear,
            "show_clear_sort_id": show_clear_sort_id, "choice_array": choices,
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }


def _finals_round_for_program(program_id) -> int | None:
    """1/2/3 when the program is a finale round member, else None.

    Two disjoint sets of 3x41 course variants, one per scenario family, read
    off single_mode_race_group:
      URA finals        2001-2041 / 2101-2141 / 2201-2241  (groups 10001-10003)
      Twinkle S. Climax 2301-2341 / 2401-2441 / 2501-2541  (groups 40001-40003)
    Trackblazer runs its three legs on the same turns as URA's finals but on
    its OWN programs; recognising only URA's range meant a Climax leg had no
    round number, so it got none of the finale cutscenes, none of the finale
    rewards, and never ended the career."""
    if program_id is None:
        return None
    if 2001 <= program_id <= 2041 or 2301 <= program_id <= 2341:
        return 1
    if 2101 <= program_id <= 2141 or 2401 <= program_id <= 2441:
        return 2
    if 2201 <= program_id <= 2241 or 2501 <= program_id <= 2541:
        return 3
    return None


def _finals_events_for(chara_info) -> dict:
    """The finale cutscene table for THIS career's scenario.

    The three finale races are shared; the stories wrapped around them are not.
    Scenario.finals_events is None for URA (and for anything that has not been
    captured yet), which keeps _FINALS_EVENTS as the default."""
    return scenarios.for_chara(chara_info).finals_events or _FINALS_EVENTS


def _finals_event_entry(round_no: int, which: str, chara_info=None) -> dict:
    """The round's captured pre-/post-race scenario event, exact wire shape."""
    table = _finals_events_for(chara_info)
    if which == "pre":
        event_id, story_id, has_choice = table[round_no]["pre"]
        return _scenario_event_entry(event_id, story_id, play_timing=2,
                                     choices=[_ack_choice()] if has_choice else [])
    # The post tuple may carry its own show_clear: URA and the 101xxx family
    # use 1, Trackblazer's 203102/203104/203106 use 5 in every capture.
    event_id, story_id, sort_id, *rest = table[round_no]["post"]
    return _scenario_event_entry(event_id, story_id, play_timing=3,
                                 choices=[_ack_choice()],
                                 show_clear=rest[0] if rest else 1,
                                 show_clear_sort_id=sort_id)


def _career_player_race_chara(chara_info: dict, viewer_id,
                              running_style: int | None = None) -> dict:
    """Adapt career chara_info into the shape practice_race/race_simulator
    expect from a trained_chara roster entry. Most stat/aptitude field names
    already match (power, wiz, proper_*). running_style is the caller's
    resolved choice (_player_running_style); without one, fall back to the
    trainee's best style aptitude -- NOT career state's race_running_style,
    which is fixture junk (see _best_running_style)."""
    chara = dict(chara_info)
    chara["running_style"] = running_style or _best_running_style(chara_info)
    chara.setdefault("viewer_id", viewer_id)
    chara.setdefault("trainer_name", "Trainer")
    return chara


_MOB_ID_BASE = 3_000_000  # synthetic career-race mob opponents (single_mode_npc-sourced)
                          # -- never persisted to any roster, own band so it can
                          # never collide with career/house/inject trained_chara ids.


def _npc_running_style(row: dict) -> int:
    """1 Nige / 2 Senkou / 3 Sashi / 4 Oikomi -- whichever style aptitude this
    mob row is strongest in, same rule trained_chara._gen_running_style uses
    for generated veterans."""
    apts = {1: row.get("proper_running_style_nige", 1), 2: row.get("proper_running_style_senko", 1),
            3: row.get("proper_running_style_sashi", 1), 4: row.get("proper_running_style_oikomi", 1)}
    return max(apts, key=apts.get)


_NAMED_OPPONENT_RATE = 0.15   # most of the field is mobs; some named runners


@functools.lru_cache(maxsize=None)
def _npc_skill_array(skill_set_id: int) -> tuple:
    """single_mode_npc.skill_set_id -> skill_set -> the [{skill_id, level}]
    shape a trained chara's skill_array has (same join daily_races.py's
    _skill_array_from_set and main_story_race.py's _skill_array_for already
    do for their own fields).

    BUG FIXED 2026-08-28 (user-reported): every career-race mob opponent was
    built with skill_array=[], so the whole field ran with no skills at all --
    no green stat boosts, no accelerations, no recoveries, no debuffs -- while
    the trainee ran with theirs. race_simulator.build_horse_spec feeds
    skill_array straight into the physics engine, so this wasn't just a
    cosmetic gap in the race UI: it silently removed every opponent's skill
    from the simulation itself. single_mode_npc.skill_set_id has carried the
    real answer all along (1551 of its rows have one).

    Returned as a tuple, and copied by callers, so the cache can never hand
    out a shared mutable list."""
    if not skill_set_id:
        return ()
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    if row is None:
        return ()
    out = []
    for n in range(1, 21):
        skill_id = row[f"skill_id{n}"]
        if skill_id:
            out.append({"skill_id": skill_id, "level": row[f"skill_level{n}"] or 1})
    return tuple(out)


def _npc_skills(row: dict) -> list:
    return [dict(sk) for sk in _npc_skill_array(row.get("skill_set_id") or 0)]


@functools.lru_cache(maxsize=1)
def _mob_id_pool() -> tuple:
    """Real mob ids from master. Used when an npc row has no usable named-uma
    presentation -- a mob is always renderable, an unresolvable character is
    not.

    Only mobs master itself RACES, i.e. ones a single_mode_npc row fields: 613
    of mob_data's 824, all in 8000-8612. The rest (10xxx/20xxx, use_live 0 /
    capture_type 2) are crowd and cutscene mobs, and borrowing one of those is
    the same blank-portrait bug this fallback exists to avoid."""
    return tuple(r["mob_id"] for r in master_data.query(
        "SELECT DISTINCT m.mob_id FROM mob_data m "
        "JOIN single_mode_npc n ON n.mob_id = m.mob_id ORDER BY m.mob_id"))


def _mob_shape_from_npc(row: dict, index: int, scale: float = 1.0) -> dict:
    """The MOB wire shape (capture-verified: card_id 0, chara_id 1,
    race_dress_id 1, mob_id 8xxx). Built from an npc row that has no mob_id of
    its own by borrowing a real mob id, so the field is never 0."""
    pool = _mob_id_pool()
    mob_id = row.get("mob_id") or (pool[(row.get("id", 0) + index) % len(pool)]
                                   if pool else 8000)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": row["id"], "single_mode_chara_id": row["id"],
        "nickname_id": 0, "card_id": 0, "mob_id": mob_id, "chara_id": 1,
        "race_dress_id": 1, "rarity": 1, "talent_level": 1,
        "skill_array": _npc_skills(row),
        "speed": max(1, round(row["speed"] * scale)),
        "stamina": max(1, round(row["stamina"] * scale)),
        "power": max(1, round(row["pow"] * scale)),
        "guts": max(1, round(row["guts"] * scale)),
        "wiz": max(1, round(row["wiz"] * scale)),
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 1),
        "proper_distance_mile": row.get("proper_distance_mile", 1),
        "proper_distance_middle": row.get("proper_distance_middle", 1),
        "proper_distance_long": row.get("proper_distance_long", 1),
        "proper_running_style_nige": row.get("proper_running_style_nige", 1),
        "proper_running_style_senko": row.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": row.get("proper_ground_turf", 1),
        "proper_ground_dirt": row.get("proper_ground_dirt", 1),
        "motivation": random.randint(row.get("motivation_min", 1) or 1,
                                     row.get("motivation_max", 4) or 4),
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


def _mob_opponent_from_npc(row: dict, uma: dict, index: int, scale: float = 1.0) -> dict:
    """A real single_mode_npc row adapted into the wire shape
    practice_race._build_race_horse_entry expects (same schema as a
    trained_chara roster entry). uma is only borrowed for cosmetic
    presentation (card_id/dress) -- every race-relevant field (stats,
    aptitudes, running_style) comes straight from the real npc row, not the
    borrowed character. `scale` multiplies the 5 core stats (see
    _mob_opponents -- single_mode_npc's own floor isn't reliably below a
    weak/early trainee's total, so strength is enforced by rescaling, not by
    filtering candidates alone)."""
    # MOB presentation, verbatim from real captures' race_horse_data:
    #   {viewer_id 0, card_id 0, mob_id 8xxx, chara_id 1, race_dress_id 1,
    #    single_mode_chara_id == trained_chara_id == the npc row id}
    # Borrowing a NAMED uma's card_id here is what made every opponent render
    # as one of the player's own former trainees (live-reported '#27: races
    # shouldn't be all of my former umas'). A named opponent (rival) keeps the
    # card_id path below.
    mob_id = row.get("mob_id") or 0
    if mob_id:
        return {
            "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
            "trained_chara_id": row["id"],
            "single_mode_chara_id": row["id"],
            "nickname_id": 0,
            "card_id": 0,
            "mob_id": mob_id,
            "chara_id": 1,
            "rarity": 1,
            "talent_level": 1,
            "skill_array": _npc_skills(row),
            "speed": max(1, round(row["speed"] * scale)),
            "stamina": max(1, round(row["stamina"] * scale)),
            "power": max(1, round(row["pow"] * scale)),
            "guts": max(1, round(row["guts"] * scale)),
            "wiz": max(1, round(row["wiz"] * scale)),
            "running_style": _npc_running_style(row),
            "race_dress_id": 1,
            "proper_distance_short": row.get("proper_distance_short", 1),
            "proper_distance_mile": row.get("proper_distance_mile", 1),
            "proper_distance_middle": row.get("proper_distance_middle", 1),
            "proper_distance_long": row.get("proper_distance_long", 1),
            "proper_running_style_nige": row.get("proper_running_style_nige", 1),
            "proper_running_style_senko": row.get("proper_running_style_senko", 1),
            "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
            "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
            "proper_ground_turf": row.get("proper_ground_turf", 1),
            "proper_ground_dirt": row.get("proper_ground_dirt", 1),
            "motivation": row.get("motivation_max", 3) or 3,
        }
    card_id = uma["cardId"]
    chara_id = uma.get("charaId", 0)
    # PRESENTATION MUST BE RENDERABLE. The paddock loads each horse's costume;
    # a race_dress_id of 0 (or a chara_id that resolves to nothing) NullRefs in
    # Gallop.PaddockViewControllerBase.RegisterTimeline, the view transition
    # never completes, and the run softlocks right after the pre-race story --
    # confirmed from the client's Player.log. The npc row carries its OWN
    # race_dress_id; prefer it, fall back to the card's, and if neither is
    # usable render this opponent as a MOB (always renderable) rather than a
    # named uma we have no assets for.
    dress = row.get("race_dress_id") or trained_chara.dress_for_card(card_id, chara_id)
    if not dress or not chara_id:
        return _mob_shape_from_npc(row, index, scale)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": _MOB_ID_BASE + row["id"] * 100 + index,
        "single_mode_chara_id": row.get("chara_id", 0),
        "nickname_id": 0,
        "card_id": card_id,
        "mob_id": 0,
        "rarity": 1,
        "talent_level": 1,
        "skill_array": _npc_skills(row),
        "speed": max(1, round(row["speed"] * scale)),
        "stamina": max(1, round(row["stamina"] * scale)),
        "power": max(1, round(row["pow"] * scale)),
        "guts": max(1, round(row["guts"] * scale)),
        "wiz": max(1, round(row["wiz"] * scale)),
        "running_style": _npc_running_style(row),
        "race_dress_id": dress,
        "proper_distance_short": row.get("proper_distance_short", 1),
        "proper_distance_mile": row.get("proper_distance_mile", 1),
        "proper_distance_middle": row.get("proper_distance_middle", 1),
        "proper_distance_long": row.get("proper_distance_long", 1),
        "proper_running_style_nige": row.get("proper_running_style_nige", 1),
        "proper_running_style_senko": row.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": row.get("proper_ground_turf", 1),
        "proper_ground_dirt": row.get("proper_ground_dirt", 1),
        "motivation": random.randint(row.get("motivation_min", 1) or 1, row.get("motivation_max", 4) or 4),
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


# --- HAPPY MEEK IN THE URA FINALS FINAL (user-supplied, 2026-07-28) ----------
# She runs in the round-3 race herself, and beating her swaps the post-race
# reward for a much bigger one (see _FINALS_MEEK_REWARD). master has her as
# single_mode_npc rows 2001900/901/902 (chara 2001) whose stats are exactly
# _HAPPY_MEEK_STATS levels 1 and 2, and -- crucially -- with her own
# race_dress_id 200101. She has NO card_data row, so she can only be rendered
# via that npc dress (see practice_race._build_race_horse_entry).
_HAPPY_MEEK_CHARA = 2001
_HAPPY_MEEK_NPC_ID = 2001902          # her strongest row; stats overridden below
_HAPPY_MEEK_MAX_LEVEL = 5             # user-supplied: she scales with duels WON,
                                      # capped at 5 (the chart's "max level")


def _meek_in_finals(full_state: dict, chara_info, finals_round) -> bool:
    """Whether Happy Meek runs in this URA Finals race.

    Grand Live DOES have the URA Finale (its capture fires 101001-101006 on
    turns 74/76/78 exactly like URA) -- but Happy Meek is not in that scenario
    at all, so she must not be in its round-3 field."""
    return finals_round == 3 and scenarios.for_chara(chara_info).has_versus_npc


def _happy_meek_finals_level(full_state: dict) -> int:
    """Her level for the URA final: it SCALES with duels won, capped at 5
    (user-supplied). VERSUS_LEVEL_KEY is exactly that -- it starts at 1 and now
    only advances on a duel WIN -- so a player who never duelled her meets her
    at level 1 and one who beat her four times meets her maxed."""
    return max(1, min(_HAPPY_MEEK_MAX_LEVEL,
                      int(full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1) or 1)))


def _happy_meek_opponent(level: int = _HAPPY_MEEK_MAX_LEVEL) -> dict | None:
    """Happy Meek as a race opponent at a given duel level. None if master has
    no row for her (then the finals field is just its usual mobs)."""
    raw = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?",
                                (_HAPPY_MEEK_NPC_ID,))
    if not raw:
        return None
    # sqlite3.Row has no .get(); the npc helpers below all expect a dict
    row = {k: raw[k] for k in raw.keys()}
    stats = single_mode_events.happy_meek_stats(level)
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": _HAPPY_MEEK_NPC_ID,
        # card_id 0 + single_mode_chara_id 2001 + her own dress is what makes
        # the client render Happy Meek rather than a mob.
        "single_mode_chara_id": _HAPPY_MEEK_CHARA,
        "nickname_id": 0, "card_id": 0, "mob_id": 0,
        "chara_id": _HAPPY_MEEK_CHARA,
        "race_dress_id": row["race_dress_id"] or 200101,
        "rarity": 5, "talent_level": 5, "skill_array": _npc_skills(row),
        "speed": stats["speed"], "stamina": stats["stamina"],
        "power": stats["power"], "guts": stats["guts"], "wiz": stats["wiz"],
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 7),
        "proper_distance_mile": row.get("proper_distance_mile", 7),
        "proper_distance_middle": row.get("proper_distance_middle", 7),
        "proper_distance_long": row.get("proper_distance_long", 7),
        "proper_running_style_nige": row.get("proper_running_style_nige", 2),
        "proper_running_style_senko": row.get("proper_running_style_senko", 7),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 7),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 2),
        "proper_ground_turf": row.get("proper_ground_turf", 7),
        "proper_ground_dirt": row.get("proper_ground_dirt", 7),
        "motivation": 5,
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


# --- A SCENARIO'S OWN FINAL CHALLENGERS (Scenario.finals_rival_npc_ids) -----
# The Unity Cup's Bitter Glasse and Little Cocon occupy the slot Happy Meek
# occupies for URA. The wire shape is hers exactly -- card_id 0, the chara's
# own single_mode_chara_id, and the npc row's race_dress_id -- because these
# characters likewise have no card_data row and can only be rendered through
# that dress (see _happy_meek_opponent).
#
# The one difference is the stats: Meek's are overridden per duel level, and
# these have no such ladder, so the master row is served as written.
def _scenario_finals_rival(npc_id: int) -> dict | None:
    """One scenario finals challenger, or None if master has no row for her."""
    raw = master_data.query_one("SELECT * FROM single_mode_npc WHERE id=?",
                                (int(npc_id),))
    if not raw:
        return None
    row = {k: raw[k] for k in raw.keys()}   # sqlite3.Row has no .get()
    chara_id = int(row.get("chara_id") or 0)
    # THIS BUILD HAS NO ART FOR THEM. Unlike Happy Meek -- whose textures ship
    # with URA, a scenario Global has -- Bitter Glasse and Little Cocon belong
    # to a scenario this build has not released, so the client draws them as
    # blank white planes in the finals field (user-reported 2026-09-07). Her
    # own dress is not enough on its own; see master_data.has_portrait_art.
    # Fall back to the mob shape, which is always renderable, keeping the npc
    # row's stats, skills and aptitudes so the final is exactly as hard as
    # before -- only the face is anonymous. If the art ever ships, this test
    # goes true on its own and they come back named.
    if not master_data.has_portrait_art(chara_id):
        mob = _mob_shape_from_npc(row, 0)
        mob["motivation"] = 5
        return mob
    return {
        "viewer_id": 0, "trainer_name": "", "owner_viewer_id": 0,
        "trained_chara_id": int(npc_id),
        "single_mode_chara_id": chara_id,
        "nickname_id": 0, "card_id": 0, "mob_id": 0,
        "chara_id": chara_id,
        "race_dress_id": row.get("race_dress_id") or 1,
        "rarity": 5, "talent_level": 5, "skill_array": _npc_skills(row),
        "speed": row.get("speed") or 100, "stamina": row.get("stamina") or 100,
        "power": row.get("pow") or 100, "guts": row.get("guts") or 100,
        "wiz": row.get("wiz") or 100,
        "running_style": _npc_running_style(row),
        "proper_distance_short": row.get("proper_distance_short", 7),
        "proper_distance_mile": row.get("proper_distance_mile", 7),
        "proper_distance_middle": row.get("proper_distance_middle", 7),
        "proper_distance_long": row.get("proper_distance_long", 7),
        "proper_running_style_nige": row.get("proper_running_style_nige", 2),
        "proper_running_style_senko": row.get("proper_running_style_senko", 7),
        "proper_running_style_sashi": row.get("proper_running_style_sashi", 7),
        "proper_running_style_oikomi": row.get("proper_running_style_oikomi", 2),
        "proper_ground_turf": row.get("proper_ground_turf", 7),
        "proper_ground_dirt": row.get("proper_ground_dirt", 7),
        "motivation": 5,
        "fans": 0, "wins": 0, "win_saddle_id_array": [],
    }


def _scenario_finals_rivals(chara_info, finals_round) -> list[dict]:
    """This scenario's own challengers for THIS finals race -- round 3 only,
    the same round Happy Meek runs in."""
    if finals_round != 3:
        return []
    ids = scenarios.for_chara(chara_info).finals_rival_npc_ids or ()
    return [o for o in (_scenario_finals_rival(i) for i in ids) if o]


def _mob_opponents(chara_info: dict, count: int, exclude_charas=()) -> list[dict]:
    """Real single_mode_npc mob stats, RESCALED to be clearly weaker than the
    trainee's OWN current total stat, in place of the account's house/legacy
    roster (_get_or_seed_roster) -- that pool is built for PRACTICE races to
    showcase a player's collection (including their own past FULL, maxed-out
    careers), which made a fresh debut race an unwinnable mismatch against
    veteran-strength opponents ("my old umas").

    Two earlier passes both still lost real debut races: sampling relative to
    the player's total via a multiplicative band (0.6x-1.3x, then even a
    weaker 0.35x-0.75x band) kept silently falling back to WIDER bands,
    because single_mode_npc's own natural floor (~450-500 total across its
    whole table) sits close to a genuinely early/weak trainee's own total
    (a turn-12 debut can be under 700) -- there just isn't enough real 'weak'
    data below that floor to filter down to, so the band fallback kept
    picking opponents in the same ballpark as the player regardless. Fixed by
    RESCALING each picked npc's 5 core stats directly to a target total (a
    random fraction, 0.45x-0.75x, of the player's own total) rather than
    relying on the raw pool containing weak enough rows -- this guarantees
    real weakness at any player stat level, low or high, while still keeping
    each mob's own aptitude/running-style shape (only the stat magnitude is
    rescaled).

    single_mode_rival/race_instance's own npc_group_id doesn't cleanly map
    onto single_mode_npc's tier groups (89 distinct ids on one side, 12 on the
    other, no shared id space found) -- rather than guess that join, this
    draws from the FULL single_mode_npc table (all groups combined) purely
    for aptitude/running-style variety; the actual strength comes from the
    rescale, not from which row was picked."""
    player_total = max(sum(chara_info.get(s, 0) for s in ("speed", "stamina", "power", "guts", "wiz")), 250)
    rows = master_data.query(
        "SELECT id, chara_id, mob_id, speed, stamina, pow, guts, wiz, "
        "proper_distance_short, proper_distance_mile, proper_distance_middle, proper_distance_long, "
        "proper_running_style_nige, proper_running_style_senko, proper_running_style_sashi, "
        "proper_running_style_oikomi, proper_ground_turf, proper_ground_dirt, "
        "skill_set_id, motivation_min, motivation_max FROM single_mode_npc")
    if not rows:
        return []

    def total(r):
        return r["speed"] + r["stamina"] + r["pow"] + r["guts"] + r["wiz"]

    # THE TRAINEE HERSELF IS NEVER IN HER OWN FIELD. The face is drawn from the
    # uma pool independently of the npc row (see below), so nothing stopped it
    # picking the player's own chara -- and it does not even have to match her
    # OUTFIT to read as her: a Wedding Mayano Top Gun career had the ordinary
    # Mayano Top Gun running against her (user-reported 2026-09-06), because
    # the face pool is keyed on the CHARA, not the card. Excluded here rather
    # than at each call site so every field this builds (career, Unity Cup
    # filler, practice race) inherits it.
    exclude_charas = set(exclude_charas or ()) | {
        (chara_info.get("card_id") or 0) // 100} - {0}
    rng = random.Random((chara_info.get("card_id", 0) << 8) ^ (chara_info.get("turn", 0) or 0))
    # Prefer REAL MOB rows (mob_id 8xxx, chara_id 1) -- the field is mobs, with
    # the occasional named runner (user: 'mob umas, and sometimes normal ones').
    mobs = [r for r in rows if (r["mob_id"] or 0) > 0]
    # A chara already running as a SCRIPTED rival must not also turn up in the
    # random filler -- that put King Halo in the same race twice.
    named = [r for r in rows
             if not (r["mob_id"] or 0) and r["chara_id"] not in set(exclude_charas or ())]
    picks = []
    for _ in range(min(count, len(rows))):
        pool = named if (named and rng.random() < _NAMED_OPPONENT_RATE) else mobs
        pool = pool or rows
        picks.append(rng.choice(pool))
    uma_pool = trained_chara._UMA_DATA
    # BUG FIXED 2026-08-29 (live-reported: an opponent -- "Follow the Sun" --
    # finished first by an absurd margin, no skill stacking involved, only
    # 4 skills). Root cause: `scale` is a single flat multiplier applied to
    # all 5 of the npc row's OWN stats, uniformly, to hit a TOTAL target --
    # but it says nothing about any ONE stat once the row's own distribution
    # is skewed. Verified against the current master data: the most
    # speed-skewed single_mode_npc rows, drawn at the high end of the
    # 0.45-0.75 band against a 9999-everywhere player, scale to 12000-14000+
    # speed -- HIGHER than the player's own stat, despite being "weaker"
    # by the crude 5-stat sum this formula actually targets. Clamp each
    # opponent stat to the player's own value in that stat afterward, so
    # "weaker" holds per-stat (what actually determines pace/spurt speed),
    # not just in the sum.
    player_stats = {s: max(chara_info.get(s, 1) or 1, 1) for s in ("speed", "stamina", "power", "guts", "wiz")}
    used_charas = set(exclude_charas or ())
    opponents = []
    for i, r in enumerate(picks):
        row = dict(r)
        target_total = player_total * rng.uniform(0.45, 0.75)
        scale = target_total / max(total(row), 1)
        # The FACE is drawn independently of the npc row, so a chara already
        # in the field as a scripted rival has to be excluded HERE, not just
        # from the row pool, or she turns up twice in the same race.
        faces = [u for u in uma_pool
                 if (u.get("charaId") or (u.get("cardId") or 0) // 100) not in used_charas]             or uma_pool
        face = rng.choice(faces)
        used_charas.add(face.get("charaId") or (face.get("cardId") or 0) // 100)
        opp = _mob_opponent_from_npc(row, face, i, scale=scale)
        for s in ("speed", "stamina", "power", "guts", "wiz"):
            opp[s] = min(opp[s], player_stats[s])
        opponents.append(opp)
    # trained_chara_id must stay unique across the field (it keys the entries).
    used = set()
    for i, o in enumerate(opponents):
        while o["trained_chara_id"] in used:
            o["trained_chara_id"] += 1000
        used.add(o["trained_chara_id"])
        o["single_mode_chara_id"] = o["trained_chara_id"] if o.get("mob_id") \
            else o["single_mode_chara_id"]
    return opponents


# When _simulate_career_race can't produce a real result (course/opponent
# resolution failed, or the simulator raised), the client still renders SOME
# finish from the static fallback fixture -- but we have no idea what it is.
# Every reward site downstream (race_end's fan lookup, race_out's placement
# bucket/goal-placement check) reads race_ctx["player_finish_order"] with an
# `... or 1` / `... if x else 1` fallback that exists to catch exactly this
# "no data" case -- but collapsing it to 1 means "we don't know" silently paid
# out a full win (live-reported 2026-09-03: lost a race outright -- 8th place
# -- and still got the win-tier post-race event and fans). Stashing this
# sentinel instead of None/0 makes every one of those `or 1` fallbacks a no-op
# (a real placement is never this bad) and lands each of them on its own
# already-existing "unknown result" behavior: _fan_gain_for_race's
# single_mode_fan_count lookup finds no row at this order and drops to its
# flat placeholder fan count instead of 1st's jackpot, _placement_bucket
# lands on "defeat", and a goal race's placement-required check correctly
# treats an unverifiable result as not having met the requirement rather than
# rubber-stamping it complete.
_UNKNOWN_RACE_RANK = 999


# Scripted career rivals -------------------------------------------------------
# master.mdb `single_mode_rival` names, per TRAINEE per RACE, which real umas
# turn out -- Special Week's Satsuki Sho gets Seiun Sky and King Halo; Yukino
# Bijin's Arima Kinen gets twelve named runners into a sixteen-gate field.
# 2,389 rows over 66 trainees, 94% joining to a single_mode_npc row carrying
# that rival's real stats, aptitudes, skill_set and race_dress_id.
#
# Previously ignored entirely: every career field was random mobs rescaled to
# the player. The old comment blamed an unmappable npc_group_id -- true, but the
# wrong column. single_mode_rival.single_mode_npc_id is a plain primary-key join
# onto single_mode_npc.id and works.
#
# STAT SCALING, measured from 9 real captured career races (docs/CAREER_RIVALS.md):
# within a race every rival shares ONE factor, and it tracks the race's calendar
# position, NOT the player -- two players 400 stat-points apart both got 0.68 at
# the Hopeful S. Least squares:
#     factor = 0.01804 * turn + 0.2747      (mean error 0.03, max 0.11)
# turn = (year-1)*24 + (month-1)*2 + half. Two late Grand Live races sat well
# above this line (1.89, 2.11) -- a scenario bonus probably exists; unmodelled.
_RIVAL_SCALE_SLOPE = 0.01804
_RIVAL_SCALE_INTERCEPT = 0.2747
_RIVAL_SCALE_MIN, _RIVAL_SCALE_MAX = 0.5, 2.5
# condition_type 1 is unconditional (2,251 of 2,389).
#
# condition_type 3 (2 rows) gates on a BRANCHING career storyline: its
# `rival_flag_id` is a `single_mode_route_race.determine_race_flag`, so the
# rival turns out only for the career that took that arm. Ines Fujin's row
# (flag 103101, her Arima Kinen arm) is served that way now -- see
# _active_branch_flags.
#
# The other condition_type 3 row is Kitasan Black's, flag 10681003, which
# matches NO determine_race_flag anywhere in master (she has no branch group at
# all; every one of her 14 route rows is determine_race 0). It looks like
# (chara 1068, rival chara 1003) in a different flag space -- the same space
# the 136 condition_type 2 rows appear to use, most of which have turn=0 and
# race_program_id=0 and so are career-long rival RELATIONSHIPS rather than race
# fields. Both stay unserved: nothing in the data can currently satisfy them,
# and serving them unconditionally would put rivals into races a career never
# reached.
_RIVAL_CONDITION_UNCONDITIONAL = 1
_RIVAL_CONDITION_BRANCH_FLAG = 3


@functools.lru_cache(maxsize=64)
def _active_branch_flags(route_race_id_array: tuple) -> frozenset:
    """The `determine_race_flag`s of the branch arms this career is actually
    on -- the arms present in `route_race_id_array` that survive
    _dropped_alternative_ids.

    Master lists every arm of a branch group, and until the career takes one
    they are all still in the array, so mere presence proves nothing: the
    default arm has to lose for a flag to count. That is exactly what
    _dropped_alternative_ids decides (master's own default until
    switch_route_race prunes the losers), which is why this is derived from it
    rather than tracked separately -- there is then no way for the goal race
    and the flag-gated rival field to disagree about which branch is live.
    """
    if not route_race_id_array:
        return frozenset()
    ids = ",".join(str(int(i)) for i in route_race_id_array)
    dropped = _dropped_alternative_ids(route_race_id_array)
    rows = master_data.query(
        f"SELECT id, determine_race_flag AS flag FROM single_mode_route_race "
        f"WHERE id IN ({ids}) AND determine_race_flag != 0")
    return frozenset(r["flag"] for r in rows if r["id"] not in dropped)


def _rival_scale(turn: int) -> float:
    return max(_RIVAL_SCALE_MIN,
               min(_RIVAL_SCALE_MAX, _RIVAL_SCALE_SLOPE * (turn or 0) + _RIVAL_SCALE_INTERCEPT))


def _uma_for_chara(uma_pool, npc):
    """The uma_data entry for this npc's OWN chara, so a scripted rival races as
    herself. uma_data uses camelCase (charaId/cardId) -- reading chara_id here
    silently matched nothing and handed every rival the pool's first face.
    None when the chara has no uma_data row (no renderable assets)."""
    chara_id = npc.get("chara_id")
    if not chara_id:
        return None
    for u in uma_pool:
        if (u.get("charaId") or (u.get("cardId") or 0) // 100) == chara_id:
            return u
    return None


def _rivals_for_turn(rows, turn: int) -> list:
    """One row per rival: the one written for THIS turn.

    single_mode_rival is keyed on the TURN as well as the trainee and the race,
    and we were ignoring that column -- which is why the same uma turned out
    twice in one field (user-reported 2026-09-06, two Narita Brians). Mayano's
    race program 81 is the plain case: Narita Brian is listed at turn 48 as npc
    1016001 and at turn 72 as npc 1016004, the same rival at two strengths for
    the two calendar slots that race occupies, and we served both bodies.

    It accounts for ALL of it -- 197 (trainee, race, rival) triples repeat in
    the table, and exactly 0 of them repeat once `turn` is part of the key -- so
    this is a read of the data, not a dedupe papering over one.

    Nearest turn wins rather than an exact match: a player can reach a race a
    turn off its scheduled slot (the Unity Cup hold did exactly that), and an
    exact-match filter would field NO rivals at all for that run. turn 0 is the
    table's wildcard (115 rows) and is only taken when nothing else fits."""
    best = {}
    for row in rows:
        chara = row["rival_chara_id"]
        row_turn = row["rival_turn"] or 0
        # (wildcard?, distance) -- a real turn always beats turn 0.
        rank = (0, abs(row_turn - int(turn or 0))) if row_turn else (1, 0)
        if chara not in best or rank < best[chara][0]:
            best[chara] = (rank, row)
    return [row for _rank, row in best.values()]


def _scripted_rivals(chara_info: dict, program_id, limit: int) -> list:
    """The real umas master.mdb says should run THIS trainee's THIS race, at
    stats scaled for the career turn. Empty when the race scripts none."""
    player_chara = (chara_info.get("card_id") or 0) // 100
    if not player_chara or not program_id or limit <= 0:
        return []
    rows = master_data.query(
        "SELECT r.rival_chara_id, r.frame_order, r.condition_type, r.rival_flag_id, "
        "r.turn AS rival_turn, n.* "
        "FROM single_mode_rival r JOIN single_mode_npc n ON n.id = r.single_mode_npc_id "
        "WHERE r.chara_id=? AND r.race_program_id=? AND r.condition_type IN (?, ?) "
        "ORDER BY r.id",
        (player_chara, program_id,
         _RIVAL_CONDITION_UNCONDITIONAL, _RIVAL_CONDITION_BRANCH_FLAG))
    if rows:
        flags = _active_branch_flags(tuple(chara_info.get("route_race_id_array") or ()))
        rows = [r for r in rows
                if r["condition_type"] != _RIVAL_CONDITION_BRANCH_FLAG
                or r["rival_flag_id"] in flags]
        # An active branch row REPLACES the unconditional row for the same
        # rival, it does not add a second copy of her: Ines Fujin's Arima lists
        # Oguri Cap twice, npc 1006024 unconditionally and npc 1006025 behind
        # flag 103101 (a different, slightly weaker stat line). Keeping both put
        # two Oguri Caps in one field.
        branched = {r["rival_chara_id"] for r in rows
                    if r["condition_type"] == _RIVAL_CONDITION_BRANCH_FLAG}
        if branched:
            rows = [r for r in rows
                    if r["condition_type"] == _RIVAL_CONDITION_BRANCH_FLAG
                    or r["rival_chara_id"] not in branched]
    rows = _rivals_for_turn(rows, chara_info.get("turn") or 0)
    if not rows:
        return []
    scale = _rival_scale(chara_info.get("turn") or 0)
    uma_pool = trained_chara._UMA_DATA
    out = []
    for i, row in enumerate(rows[:limit]):
        npc = dict(row)
        if not npc.get("rival_chara_id"):
            continue          # a mob slot inside the scripted list
        if npc["rival_chara_id"] == player_chara:
            continue          # never the trainee herself -- see _mob_opponents
        uma = _uma_for_chara(uma_pool, npc)
        if uma is None:
            continue          # no assets -- leave the slot to a mob
        out.append(_mob_opponent_from_npc(npc, uma, i, scale=scale))
    return out


def _dedupe_field_faces(horses) -> int:
    """ONE UMA, ONE BODY. Re-faces, in place, any horse repeating an identity
    already on the track; returns how many it changed.

    The invariant the player states plainly ("no uma should have 2 versions of
    them racing") is enforced HERE, once, rather than trusted to every caller
    that assembles a field -- because the two clones that reached a live race
    both came from a caller getting its own exclusion set subtly wrong: one
    never excluded the trainee, the other read `chara_id` off an opponent seat
    that carries an npc row id instead. Each was a different call site, and
    neither failed loudly.

    Only a NEUTRAL NAMED FILLER can be re-faced: its face is cosmetic and drawn
    independently of the npc row supplying its stats, so swapping it changes
    nothing about the race. Everyone else is structural -- the trainee, her
    teammates, the opposing team, a scripted rival -- and their identity IS the
    point, so a clash among them is logged rather than papered over. Mobs are
    faceless and can never clone.
    """
    pool = trained_chara._UMA_DATA

    def chara_of(h):
        cid = int(h.get("chara_id") or 0)
        return cid if cid > 1 else (int(h.get("card_id") or 0)) // 100

    def refaceable(h):
        return not h.get("viewer_id") and not h.get("mob_id") and h.get("card_id")

    # STRUCTURAL RUNNERS CLAIM THEIR IDENTITIES FIRST, in a pass of their own.
    # Walking the field in one pass instead lets whichever horse happens to
    # come first keep the chara -- and when that is the filler, the runner who
    # genuinely IS that uma is the one reported unfixable, which is backwards.
    seen, fixed = set(), 0
    for horse in horses:
        chara = chara_of(horse)
        if chara and not refaceable(horse):
            if chara in seen:
                log.error("duplicate uma in one field: chara %s appears twice "
                          "and neither one is a re-faceable filler", chara)
            seen.add(chara)
    for horse in horses:
        chara = chara_of(horse)
        if not chara or not refaceable(horse):
            continue                      # a mob is interchangeable by design
        if chara not in seen:
            seen.add(chara)
            continue
        for uma in pool:
            new_chara = uma.get("charaId") or (uma.get("cardId") or 0) // 100
            if not new_chara or new_chara in seen:
                continue
            dress = trained_chara.dress_for_card(uma["cardId"], new_chara)
            if not dress:
                continue
            horse["card_id"] = uma["cardId"]
            horse["race_dress_id"] = dress
            seen.add(new_chara)
            fixed += 1
            break
        else:
            log.error("duplicate uma in one field: chara %s repeats and the "
                      "face pool is exhausted", chara)
    return fixed


def _simulate_career_race(chara_info: dict, viewer_id, race_instance_id: int,
                          running_style: int | None = None,
                          with_happy_meek: bool = False,
                          happy_meek_level: int = _HAPPY_MEEK_MAX_LEVEL,
                          finals_rivals: list | None = None,
                          program_id=None) -> dict | None:
    """Run the REAL race physics simulator (race_simulator.py, already used for
    practice_race/race_start) for a career race, instead of replaying one
    static captured fixture (Grass Wonder's debut) for every race regardless of
    who's actually racing -- the animation and finish order now genuinely
    reflect the trainee's own stats/skills/aptitudes and the resolved course.

    Opponents are real single_mode_npc mob data scaled to the trainee's own
    strength (see _mob_opponents) -- previously sampled from the account's
    house/legacy roster, which could include the player's own maxed-out past
    careers and made an early debut effectively unwinnable.

    Returns {race_horse_data, random_seed, race_scenario, player_finish_order}
    or None (caller falls back to the old static fixture replay) if the course
    can't be resolved or no mob candidates are available."""
    try:
        player = _career_player_race_chara(chara_info, viewer_id, running_style)
        course_info = race_simulator.get_course_for_race_instance(race_instance_id)
        if course_info is None:
            return None
        _course_set_id, _course, entry_num = course_info
        # Scripted rivals first, then mobs to fill the gate. Scripted ones keep
        # their real master stats (scaled for the turn) rather than being
        # rescaled to the player -- that is the whole point of serving them.
        field_size = max(0, entry_num - 1)
        rivals = _scripted_rivals(chara_info, program_id, field_size)
        scripted_charas = {(o.get("card_id") or 0) // 100 for o in rivals if o.get("card_id")}
        opponents = rivals + _mob_opponents(
            chara_info, max(0, field_size - len(rivals)), exclude_charas=scripted_charas)
        if not opponents:
            return None
        # HAPPY MEEK runs in the URA Finals FINAL. She replaces one mob so the
        # field size (and therefore the frame count the client renders) is
        # unchanged; her index is tracked so race_out can tell whether the
        # player actually beat her.
        _dedupe_field_faces([player] + opponents)
        meek_index = None
        if with_happy_meek:
            meek = _happy_meek_opponent(happy_meek_level)
            if meek:
                opponents[0] = meek
                meek_index = 1          # horses[0] is the player
        # A SCENARIO'S OWN FINAL CHALLENGERS take the slots after Meek's, on
        # the same terms: each replaces a mob rather than joining the field, so
        # entry_num -- and the frame count the client renders from it -- is
        # untouched. Never more of them than there are mobs to give up.
        for offset, rival in enumerate(finals_rivals or ()):
            slot = (1 if meek_index else 0) + offset
            if slot >= len(opponents):
                break
            opponents[slot] = rival
        result = race_simulator.simulate_race(
            player, opponents, race_instance_id, ground_condition=1, weather=1, season=1)
        if result is None:
            return None

        horses = result["horses"]
        sim_results = result["sim_results"]

        def _rank_by(key):
            order = sorted(range(len(horses)), key=key)
            return {idx: rank + 1 for rank, idx in enumerate(order)}

        popularity_by_index = _rank_by(lambda i: -horses[i]["speed"])
        stamina_rank_by_index = _rank_by(lambda i: -horses[i]["stamina"])
        power_rank_by_index = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))

        # FRAME ORDER IS THE GATE, and it must be the SAME randomized gate
        # permutation build_race_scenario used for the in-race lane frames --
        # which simulate_race hands back as gate_assignment for exactly this.
        #
        # Using the plain array index (player always i=0 -> always "gate 1")
        # is the bug practice_race.py and main_story_race.py both fixed on
        # 2026-08-24; the CAREER path was missed and kept the broken form.
        # It is far worse here than a cosmetic lineup glitch: the client
        # identifies the player's horse by its gate/frame, so with the lineup
        # claiming gate 1 while the scenario runs her in some other gate, the
        # client follows THE WRONG HORSE for the whole race and reports that
        # horse's finish. The server meanwhile records its own (correct)
        # player_finish_order, so the two disagree completely --
        # live-reported 2026-09-03: "placed 10 in the Kikuka Sho, needed to
        # place 1st, but I still got the rewards and continued" against a
        # race_history that recorded result_rank 1, and (2026-09-03, earlier)
        # an 8th place that still paid win-tier fans and the Victory! story.
        # Both were read as reward bugs; the reward logic was right all along
        # and was being fed a placement for a different horse.
        gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
        race_horse_data = []
        for i, h in enumerate(horses):
            rank = chara_info.get("chara_grade", 1) if i == 0 else h.get("rank", 1)
            race_horse_data.append(practice_race._build_race_horse_entry(
                h, frame_order=gate_assignment[i] + 1, final_grade=rank,
                popularity=popularity_by_index[i],
                popularity_mark_rank_array=[popularity_by_index[i], stamina_rank_by_index[i],
                                            power_rank_by_index[i]],
            ))

        # horses[0]/sim_results[0] is always the player (simulate_race puts
        # player_chara first) -- its finishOrder is the real race result.
        # +1 EVERYWHERE below: race_runner.py's own finishOrder is explicitly
        # 0-based ("finishOrder": order_index[i], # 0-based), but every
        # consumer of player_finish_order (race_end's fan lookup, race_out's
        # _placement_bucket/placement_failed/goal reward) treats it as an
        # already-1-indexed placement. Left un-shifted, a true 2nd place (0-
        # indexed 1) read as rank 1 -> _placement_bucket's "win" bucket (the
        # Victory! story, win-tier fans, win-tier stat/SP reward) and a true
        # 6th read as rank 5 -> "solid" instead of "defeat" -- every placement
        # one bucket better than actually run, worst at the boundaries. A
        # genuine win (0-indexed 0) is FALSY, so it also fell into every
        # downstream `... or 1` / `... if x else 1` fallback that exists to
        # catch a MISSING simulation -- accidentally landing on the right
        # answer (rank 1) for a win only, which is exactly why this stayed
        # invisible until a non-win placement was live-reported paying out
        # as though it had won.
        player_order = sim_results[0]["finishOrder"] + 1
        meek_order = (sim_results[meek_index]["finishOrder"] + 1
                      if meek_index is not None and meek_index < len(sim_results)
                      else None)
        # WHO ELSE WAS IN THE FIELD, and where they finished. Secret events ask
        # ('beat Rival X in the Tenno Sho', 'both of you lost the Arima'), and
        # nothing else in the career record could answer it -- race_history knew
        # only the player's own placement. Mobs have no card_id and are left out;
        # only the scripted rivals a condition can name are recorded.
        rival_finish_orders = {}
        for i, h in enumerate(horses):
            if not i:
                continue
            chara = (h.get("card_id") or 0) // 100
            if chara and i < len(sim_results):
                rival_finish_orders.setdefault(chara, sim_results[i]["finishOrder"] + 1)
        return {
            "race_horse_data": race_horse_data,
            "random_seed": result["seed"],
            "race_scenario": result["race_scenario"],
            "player_finish_order": player_order,
            # The player's crossing time in MILLIseconds -- what the post-race
            # reward card's result_time shows (a real capture: 911307 for a
            # 91.1s race). finishTimeRaw is the unquantized value; finishTime
            # is snapped to the 1/15s frame grid.
            "player_finish_time": int(round(
                (sim_results[0].get("finishTimeRaw")
                 or sim_results[0].get("finishTime") or 0.0) * 1000)),
            "player_popularity": popularity_by_index[0],
            "rival_finish_orders": rival_finish_orders,
            "meek_finish_order": meek_order,
            # the whole point of her being here: did the player beat her?
            "beat_happy_meek": (meek_order is not None and player_order < meek_order),
            # WHAT HAPPENED TO HER IN THE RACE, for the two epithets that ask
            # about the running of it rather than the result. The player is
            # always horse index 0 (simulate_race puts player_chara first).
            # "kakari" is the Rushed sentinel the engine emits in place of a
            # skill id; everything else is a real skill firing, counted DISTINCT
            # because a skill that retriggers is still one skill used.
            "player_rushed_count": sum(
                1 for e in (result.get("skill_events") or ())
                if e.get("horseIndex") == 0 and e.get("skillId") == "kakari"),
            "player_skills_used": len({
                e.get("skillId") for e in (result.get("skill_events") or ())
                if e.get("horseIndex") == 0 and isinstance(e.get("skillId"), int)}),
            "player_overtakes": ((result.get("final_stretch_overtakes") or [0])[0]
                                 if result.get("final_stretch_overtakes") else 0),
        }
    except Exception:
        log.exception("career race simulation failed, falling back to static fixture replay")
        return None


def _resolve_and_enter_race(payload: dict) -> dict:
    """Resolve and simulate the race for this turn, and transition the trainee
    into racing state. Confirmed from a real capture: the client actually
    triggers race entry via single_mode/check_event with {program_id,
    current_turn} (NO event_id/choice_number -- a completely different shape
    from every story-event check_event) -- NOT via single_mode/race_entry's
    own request, which never carries a race identifier at all and is only
    called AFTERWARDS to acknowledge the pre-race cutscene this function
    queues. See the new branch (0) in handle_ura_check_event, which routes a
    program_id-bearing request here, and the new handle_ura_race_entry, which
    just re-serves what THIS function already set up.

    Previously all of this lived directly in handle_ura_race_entry, which the
    real client never called first -- so nothing ever set up race state, and
    the training screen (already correctly locked) just kept re-serving
    itself with nothing to advance it into the race. That is likely THE root
    cause of the long-standing 'Race Day screen stuck on all-locked' bug.

    race_horse_data[0] is patched to the player's trainee and chara_info.
    race_program_id/the pre-race cutscene patched to the trainee's ACTUAL
    scheduled race for this turn (see _race_for_turn) -- previously every
    race turn showed the captured fixture's own race (Grass Wonder's debut)
    regardless of who's racing or which turn it is. Falls back to the old
    chara-only retarget when this turn's race can't be resolved (Trackblazer,
    an off-route/optional race, ...). Also resolves an OPTIONAL race
    (_voluntary_race_for_turn) when this isn't one of the trainee's own
    determined turns -- once racing unlocks, the Race command stays enabled
    every turn, and real captures confirm players really can enter races
    outside their own route/rival schedule.

    Once a race is resolved, runs the REAL simulator (_simulate_career_race)
    for the roster + race_horse_data + race_scenario, so race_start's
    animation actually matches this race/trainee instead of always replaying
    Grass Wonder's captured debut. The simulated roster/seed/scenario/result
    are stashed in RACE_CTX_KEY so race_start/race_end reuse the EXACT same
    race rather than re-rolling a different one at each step.

    Stashes the race context (program_id/turn/race name) for the synthesized
    end/out and clears the queued event so nothing stalls."""
    viewer_id = payload["viewer_id"]
    # BUG FOUND 2026-08-20 (live-reported: a debut-race entry showed
    # Maruzensky's own stats/cutscene ("Before the Debut", story 501004408)
    # instead of the real trainee's, then softlocked -- reproduced on TWO
    # unrelated fresh accounts/trainees, ruling out stale client cache).
    # No genuine "single_mode/race_entry" capture exists in this project's
    # fixture set (only "single_mode_team/race_entry", a real Maruzensky
    # Team-scenario capture) -- fixtures.first("single_mode/race_entry")
    # always returned None, and THIS function used to bail straight into
    # handle_race_entry on that: a completely different, raw-fixture-replay
    # handler with NONE of this function's chara_info rebuild / real-race
    # resolution / _retarget_race_prep_event personalization below. Every
    # debut (and, by the same path, every OTHER race whose entry ever hit
    # this fallback) served Maruzensky's captured race_entry response
    # verbatim. Same "prefer URA, fall back to the team capture as a
    # STRUCTURAL TEMPLATE ONLY" pattern already used for exec_command
    # (see _apply_command_exec) -- reuse it here instead of skipping all
    # this function's personalization entirely.
    pair = fixtures.first("single_mode/race_entry") or fixtures.first("single_mode_team/race_entry")
    if pair is None:
        raise LookupError("No captured race_entry fixture available")
    response = pair.response_copy()
    data = response.get("data", {})
    rsi = data.get("race_start_info", {}) or {}
    program_id = payload.get("program_id") or rsi.get("program_id")

    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    chara_info = career["data"]["chara_info"] if isinstance(career, dict) else None
    career_home = career["data"].get("home_info") if isinstance(career, dict) else None
    if chara_info is None:
        # Every downstream personalization (player horse, race resolution,
        # simulation, home_info) silently degrades to raw-fixture replay when
        # the career state is missing -- which presents to the player as
        # "race entry does nothing". Don't fail (the fixture replay is still
        # the best available response) but make the cause diagnosable.
        log.warning("race entry for viewer %s with NO active career state -- "
                    "serving raw fixture replay (race will not match the run)", viewer_id)
    # Same stale-cache guard as exec_command: entering a race off a turn the run
    # is no longer on resolves the WRONG race (see _race_for_turn below) and
    # stores a race ctx stamped with it. Tolerant of the turn-end hold here --
    # this only reads the acted-on turn, it never advances the run.
    conflict = _client_turn_conflict(payload, full_state, chara_info)
    if conflict is not None:
        return _stale_turn_refusal(viewer_id, "single_mode/race_entry", conflict, chara_info)
    player_style = _player_running_style(full_state, chara_info or {})
    _patch_player_horse(rsi, chara_info, running_style=player_style)
    # race_entry's own request often carries no current_turn -- fall back to the
    # live career's turn so race resolution + the stored ctx turn are correct
    # (a ctx stamped with turn None can never be staleness-checked).
    current_turn = payload.get("current_turn")
    if current_turn is None and chara_info is not None:
        current_turn = chara_info.get("turn")
    # A client-provided program_id (the real, confirmed mechanism -- see above)
    # is authoritative; only fall back to server-side resolution/discovery
    # (mandatory route race, then optional-race eligibility) if it's absent,
    # e.g. a direct/legacy call that skipped the check_event(program_id) step.
    race_history = career["data"].get("race_history", []) if isinstance(career, dict) else []
    race_info = _race_info_for_program(payload["program_id"]) if payload.get("program_id") else None
    # MAIDEN LOCK: with the debut lost and no maiden won yet, only maiden (and
    # debut) races may be entered -- the client can still ask for a higher one
    # from a stale list, and we used to honour it. A live career got into a G3
    # and an OP race that way, which the real game would never allow.
    if (race_info and chara_info
            and _debut_retry_active(chara_info, current_turn, race_history)
            and _race_grade_of(race_info["program_id"]) not in
            (_MAIDEN_GRADE, _DEBUT_GRADE)):
        log.info("race %s refused: maiden not yet won (debut retry active)",
                 race_info["program_id"])
        race_info = None
    if not race_info and chara_info:
        race_info = _race_for_turn(chara_info, current_turn, race_history)
    # A lost debut forces a retry (a different grade=900 maiden race) every
    # turn until won -- see _debut_retry_active -- which takes priority over
    # the normal voluntary pool (the real schedule hasn't resumed yet).
    if not race_info and chara_info:
        if _debut_retry_active(chara_info, current_turn, race_history):
            race_info = _debut_retry_race(chara_info, current_turn)
    if not race_info and chara_info:
        race_info = _voluntary_race_for_turn(chara_info, current_turn)
    chara_id = (chara_info or {}).get("card_id", 0) // 100
    finals_round = _finals_round_for_program((race_info or {}).get("program_id"))
    if finals_round:
        # URA finals rounds have their own scenario cutscenes (exact captured
        # shapes), NOT a per-chara 'Before the X' story -- serve the real one.
        data["unchecked_event_array"] = [
            _finals_event_entry(finals_round, "pre", chara_info)]
    else:
        _retarget_race_prep_event(data.get("unchecked_event_array"), chara_id,
                                  race_name=(race_info or {}).get("story_name"))
    if race_info:
        program_id = race_info["program_id"]
        rsi["program_id"] = program_id

    sim = None
    if chara_info is not None and race_info and race_info.get("race_instance_id"):
        sim = _simulate_career_race(chara_info, viewer_id, race_info["race_instance_id"],
                                    running_style=player_style,
                                    program_id=race_info.get("program_id"),
                                    # Happy Meek runs in the FINAL round only,
                                    # at the level your duels left her on
                                    with_happy_meek=_meek_in_finals(
                                        full_state, chara_info, finals_round),
                                    happy_meek_level=_happy_meek_finals_level(full_state),
                                    finals_rivals=_scenario_finals_rivals(
                                        chara_info, finals_round))
        if sim:
            rsi["race_horse_data"] = sim["race_horse_data"]
            rsi["random_seed"] = sim["random_seed"]
        else:
            log.warning("race entry for viewer %s (program %s): simulation unavailable -- "
                        "falling back to static fixture replay, and the reward path below "
                        "won't know the real finish order", viewer_id, program_id)

    # Build the response chara_info = the player's live run put into RACE state
    # (sync_chara_info is skipped for race endpoints, so we own this). This is
    # what makes the client actually enter the race.
    if chara_info is not None:
        race_ci = copy.deepcopy(chara_info)
        race_ci["playing_state"] = 2       # racing
        race_ci["state"] = 0
        race_ci["race_program_id"] = program_id
        data["chara_info"] = race_ci

    # home_info: use the player's OWN, already-correctly-locked command_info_array
    # (built by _build_training_command_info's forced-turn logic when this
    # turn's training screen was last served) instead of leaving the captured
    # fixture's own stale home_info in place -- previously never overridden here.
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)

    full_state[RACE_CTX_KEY] = {
        "program_id": program_id,
        "turn": current_turn,
        "race_start_info": copy.deepcopy(rsi),  # reused by the race check_event
        "race_story_name": (race_info or {}).get("story_name"),  # for the post-race event
        # GOAL vs VOLUNTARY decides the whole post-race payout (see
        # _is_goal_race / handle_ura_race_out). Resolved here, while the race is
        # being entered and the turn is still known.
        "is_goal": _is_goal_race(chara_info or {}, program_id, current_turn, race_history),
        "race_scenario": (sim or {}).get("race_scenario"),  # reused by race_start
        # _UNKNOWN_RACE_RANK, not None, when sim failed -- see its own comment.
        "player_finish_order": (sim or {}).get("player_finish_order", _UNKNOWN_RACE_RANK),
        # Crossing time in ms, for the post-race reward card's result_time.
        "player_finish_time": (sim or {}).get("player_finish_time"),
        # For the secret-event conditions (see _secret_context). Absent when no
        # simulation ran, which those conditions read as 'cannot tell'.
        "player_popularity": (sim or {}).get("player_popularity"),
        "rival_finish_orders": (sim or {}).get("rival_finish_orders"),
        # How the race was run (see _simulate_career_race). None when no
        # simulation ran, which every reader treats as "cannot tell".
        "player_rushed_count": (sim or {}).get("player_rushed_count"),
        "player_skills_used": (sim or {}).get("player_skills_used"),
        "player_overtakes": (sim or {}).get("player_overtakes"),
        # round-3 finals only: did the player finish ahead of Happy Meek?
        "beat_happy_meek": (sim or {}).get("beat_happy_meek", False),
        "finals_round": finals_round,  # 1/2/3 for URA finals rounds, else None (race_out events)
        "running_style": player_style,  # what the trainee actually ran (race_history)
        "race_instance_id": (race_info or {}).get("race_instance_id"),  # for style re-sims
        # Energy going INTO the race, for the consecutive-race fatigue roll
        # (_maybe_fire_race_fatigue). Has to be captured here: by race_out the
        # race's own energy cost has already come off, and the table is keyed
        # on what she had when she entered, not what she has left.
        "vital_at_entry": (chara_info or {}).get("vital"),
    }
    # Fresh race => fresh retry budget, and the client needs the counts on
    # home_info to decide whether to offer the free/alarm-clock buttons at all.
    if isinstance(career_home, dict):
        career_home.update(_continue_counts(full_state, full_state[RACE_CTX_KEY]))
        data["home_info"] = copy.deepcopy(career_home)
    # REBUILD the roster from live state. This path was the ONE response that
    # skipped _apply_versus, so it shipped the capture's own
    # ura_data_set.evaluation_info_array verbatim -- 12 rows naming the
    # fixture's six support cards plus Tazuna/Kiryuin/Riko, to a player who has
    # none of them. That is the "3 people incorrectly unlock" at the debut and
    # the strangers turning up as practice partners (both live-reported).
    _apply_versus(data, full_state, current_turn)
    # KEEP the captured unchecked_event_array -- its race event (play_timing 2)
    # is what drives the client to call check_event, which we resolve straight
    # into the race (see handle_ura_check_event). Clearing it = "nothing happens".
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def handle_ura_race_entry(payload: dict) -> dict:
    """single_mode/race_entry -- called AFTER the race has already been
    resolved and entered via a program_id-bearing single_mode/check_event
    (see _resolve_and_enter_race and branch (0) in handle_ura_check_event).
    Confirmed from a real capture: the client calls
    check_event({program_id, current_turn}) FIRST -- that's what actually
    sets up RACE_CTX_KEY/race_start_info and queues the pre-race cutscene --
    and only THEN calls race_entry({event_id, chara_id, choice_number}) to
    acknowledge that cutscene. So by the time this runs, the race is already
    fully set up; just re-serve it with the event cleared. Falls back to
    resolving it directly if RACE_CTX_KEY isn't set (a flow variant that
    calls race_entry without that preceding check_event, or direct testing)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    chara_info = _career_chara_info(viewer_id)
    # STALENESS GUARD: only re-serve a context set up for THIS turn (and, if the
    # request names a race, THIS race). A context from an earlier turn is a
    # leftover from a race the player backed out of (or an interrupted flow) --
    # RACE_CTX_KEY is only cleared in race_out, so nothing else removes it --
    # and re-serving it locks the client solid (live-reported: turn-16 'Clover
    # Sho' setup re-served on turn 17 -> training locked, race button dead).
    current_turn = payload.get("current_turn") or (chara_info or {}).get("turn")
    stale = (ctx.get("turn") is not None and current_turn is not None
             and ctx.get("turn") != current_turn)
    mismatched = (payload.get("program_id") and ctx.get("program_id")
                  and payload.get("program_id") != ctx.get("program_id"))
    if not ctx.get("race_start_info") or chara_info is None or stale or mismatched:
        if stale or mismatched:
            log.warning("race_entry: discarding stale race ctx (ctx turn %s prog %s "
                        "vs current turn %s prog %s) -- resolving fresh",
                        ctx.get("turn"), ctx.get("program_id"),
                        current_turn, payload.get("program_id"))
            full_state.pop(RACE_CTX_KEY, None)
            state_store.save_state(viewer_id, full_state)
        return _resolve_and_enter_race(payload)

    pair = fixtures.first("single_mode/race_entry")
    response = pair.response_copy() if pair is not None else {
        "response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": {}}
    data = response.setdefault("data", {})
    race_ci = copy.deepcopy(chara_info)
    race_ci["playing_state"] = 2
    race_ci["state"] = 0
    race_ci["race_program_id"] = ctx.get("program_id")
    data["chara_info"] = race_ci
    data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
    career = full_state.get(STATE_KEY)
    career_home = career["data"].get("home_info") if isinstance(career, dict) else None
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def handle_change_running_style(payload: dict) -> dict:
    """single_mode/change_running_style -- the player picked a strategy on the
    race screen ({program_id, running_style, current_turn}). Previously
    unhandled (fell to the no-op fallback), so the pick changed NOTHING about
    the simulated race: everyone ran the fixture's default style regardless of
    aptitude (live-reported). Store the choice for the rest of the career and,
    if this turn's race is already set up (the normal flow: check_event
    (program_id) -> strategy picker -> change_running_style -> race_entry),
    RE-SIMULATE it with the new style so the pick actually shapes the result.

    The real server answers this with a bare data-less result_code 2502 (all 4
    captured calls; the client demonstrably carries on) -- we return our
    standard success envelope, which the client also accepts from the previous
    fallback behavior."""
    viewer_id = payload["viewer_id"]
    style = payload.get("running_style")
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if style in (1, 2, 3, 4) and isinstance(career, dict):
        full_state[STYLE_CHOICE_KEY] = style
        ci = career["data"]["chara_info"]
        ci["race_running_style"] = style
        ctx = full_state.get(RACE_CTX_KEY) or {}
        cur = payload.get("current_turn") or ci.get("turn")
        if ctx.get("race_start_info") and ctx.get("turn") == cur:
            rid = ctx.get("race_instance_id")
            if not rid and ctx.get("program_id"):
                rid = (_race_info_for_program(ctx["program_id"]) or {}).get("race_instance_id")
            sim = (_simulate_career_race(
                ci, viewer_id, rid, running_style=style,
                program_id=ctx.get("program_id"),
                # keep Happy Meek in the field across a running-style re-sim
                with_happy_meek=_meek_in_finals(full_state, ci,
                                                ctx.get("finals_round")),
                happy_meek_level=_happy_meek_finals_level(full_state),
                # ...and this scenario's own challengers, for the same reason
                finals_rivals=_scenario_finals_rivals(
                    ci, ctx.get("finals_round"))) if rid else None)
            if sim:
                rsi = ctx["race_start_info"]
                rsi["race_horse_data"] = sim["race_horse_data"]
                rsi["random_seed"] = sim["random_seed"]
                ctx["race_scenario"] = sim["race_scenario"]
                ctx["player_finish_order"] = sim["player_finish_order"]
                ctx["player_finish_time"] = sim.get("player_finish_time")
                ctx["player_popularity"] = sim.get("player_popularity")
                ctx["rival_finish_orders"] = sim.get("rival_finish_orders")
                ctx["beat_happy_meek"] = sim.get("beat_happy_meek", False)
            else:
                # No re-sim possible -- at least keep the displayed entry honest.
                rhd = (ctx.get("race_start_info") or {}).get("race_horse_data") or []
                if rhd and isinstance(rhd[0], dict):
                    rhd[0]["running_style"] = style
            ctx["running_style"] = style
            full_state[RACE_CTX_KEY] = ctx
        state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}


def _drain_pending(full_state: dict, resolved_event_id, player_chara: int) -> list:
    """Pop resolved_event_id off the scenario-event queue (if present) and return
    the unchecked_event_array entry for whatever's next, or [] if empty.

    Every check_event branch that resolves something OTHER than the generic
    scenario chain (duel, training failure, rest, dynamic career event) must
    funnel its response through this instead of hardcoding []. A scenario event
    (e.g. Director's turn-3 unlock) can get scheduled the same turn one of those
    interrupts the response -- e.g. training fails, so the failure event is shown
    instead and the scenario event never reaches the client. If those branches
    just clear unchecked_event_array, the scheduled event stays stuck in
    PENDING_EVENTS_KEY forever, only to resurface out of nowhere the next time
    ANY unrelated event happens to fall through the generic chain-resolution path
    (e.g. right after the debut race instead of back at turn 3).

    EXTRA_EVENTS_KEY is checked BEFORE the scenario queue on purpose: the real
    game plays the turn's own action (rest/duel/failure), then any recreation/
    support-card coincidence, and only THEN the scenario/meta cutscene (URA
    Finale announcements, Director/Happy Meek unlock, ...) -- so scenario
    events go LAST even though they're registered first (see handle_exec_command,
    which stores PENDING_EVENTS_KEY unconditionally but only claims the display
    slot if nothing else already has)."""
    pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
    if resolved_event_id in pending:
        pending.remove(resolved_event_id)
    full_state[single_mode_events.PENDING_EVENTS_KEY] = pending
    # NPC-UNLOCK cutscenes jump the queue: they gate who can appear in
    # training from that turn on, so they must not sit behind a backlog of
    # coincidence/hint events (which is what delayed the Director/Meek
    # unlocks by many turns). Every other scenario event keeps playing LAST.
    if pending and single_mode_events.npcs_unlocked_by(pending[0]):
        nxt = pending[0]
        full_state[single_mode_events.PENDING_EVENTS_KEY] = pending[1:]
        return [single_mode_events.event_entry(nxt, player_chara)]
    # THE UNIFIED PIPELINE (career_events). Migrated families queue here instead
    # of in EXTRA/PENDING.
    #
    # Drained BEFORE legacy EXTRA, and that order is not arbitrary: the families
    # migrated so far (scenario beats) were queued by _queue_turn_event ahead of
    # the turn's coincidence events, so they reached the client first. Draining
    # EXTRA first instead moved every one of them behind the support event -- 14
    # turns' worth of reordering in the baseline diff, with identical events and
    # identical payouts. Keeping this first makes the migration a pure no-op on
    # the wire, which is the only way to tell a real regression from a shuffle.
    #
    # (Worth noting for later: _drain_pending's own docstring argues scenario
    # cutscenes should play LAST. The shipped behaviour has them first. That
    # disagreement predates this refactor -- settle it against a capture, not by
    # letting a refactor silently pick a side.)
    #
    # If the CE pipeline already has an UNRESOLVED active event, it did not
    # come from resolved_event_id (that one lives in the legacy PENDING/EXTRA
    # queues, or this function would not have been reached for it) -- some
    # other family produced and served it earlier this turn, independent of
    # whatever just resolved. serve_next() pops the FRONT OF THE QUEUE into
    # active unconditionally, including setting active=None when the queue is
    # merely empty -- it has no notion of "something is already active,
    # unresolved". Calling it here would silently orphan that event instead of
    # advancing past it, with no error and no log (live-reported: "The Grand
    # Concert Begins!" was served, then a same-turn chara-story event resolved
    # through this path and wiped it before the player ever saw a choice,
    # leaving playing_state stuck at 1 and the concert permanently
    # unreachable). Re-serve the SAME active event instead of advancing.
    already_active = career_events.active(full_state)
    if already_active is not None:
        return [already_active.wire_entry()]
    # THE GOAL BANNER LEADS. Served ahead of both queues so a cleared passive
    # goal is the very next thing after the turn's own action -- see
    # GOAL_EVENT_KEY.
    goal_entry = full_state.pop(GOAL_EVENT_KEY, None)
    if goal_entry:
        return [goal_entry]
    # THE ENDPOINT MATTERS HERE TOO. serve_next stamps a timing_from_endpoint
    # event with the timing of the response kind carrying it, and records that
    # timing as the chain's for the check_events behind it. Both call sites in
    # this file used to pass nothing, so every beat served through the drain --
    # which is most of them, since the producer poll only serves when the
    # display slot is still free -- collapsed to timing 1 and never seeded the
    # chain. CURRENT_ENDPOINT is what the request in flight arrived on.
    wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
    if wire is not None:
        return [wire]
    extra = list(full_state.get(single_mode_events.EXTRA_EVENTS_KEY, []))
    if extra:
        nxt = extra.pop(0)
        full_state[single_mode_events.EXTRA_EVENTS_KEY] = extra
        return [nxt]
    if pending:
        return [single_mode_events.event_entry(pending[0], player_chara)]
    return []


# THE CONCERT'S DISPLAY SLOT, reserved.
#
# Set for exactly the stretch of handle_exec_command that queues the events of
# the turn the run is arriving AT (see _scenario_slot_reserved's call sites),
# and only when the turn that was just PLAYED is a Grand Live concert turn whose
# Live has not been performed yet.
#
# The problem it solves (live-reported, turn 24, and visible verbatim in
# debug_responses 0099-0104): our exec_command advances chara_info.turn as soon
# as the command runs, so on the concert turn the turn-25 families -- New Year's
# (_maybe_queue_chara_story), the unique-skill level-ups, the mid-run
# inspiration -- all queue during turn 24's own response. _queue_turn_event
# claims the response's display slot whenever it is free, and it IS free at that
# point: the Grand Live beats come from _poll_career_events, which deliberately
# runs LAST (see its call site's 2026-08-26 note). So New Year's went out first,
# the concert beats chained behind it, and the whole turn-24 chain read:
#
#     exec_command -> New Year's -> Training Level Up -> Concert Begins -> []
#
# ... after which the client, having already been told the turn was 25 and then
# snapped back to 24, played an entire GHOST turn before finally opening the
# backstage screen.
#
# While reserved, _queue_turn_event / _emit_turn_event never take the slot: they
# chain into EXTRA instead, so the slot is still free when _poll_career_events
# runs and the concert chain leads. Everything reserved out of the slot still
# plays -- after the Live, once the post-live beats drain (_drain_pending serves
# the unified pipeline before legacy EXTRA), which is the official order:
# concert, padlock, post-concert beats, THEN New Year's.
SCENARIO_SLOT_RESERVED_KEY = "_scenario_slot_reserved"


def _scenario_interstitial_pending(full_state: dict, career: dict,
                                   payload: dict, turn=None) -> bool:
    """Whether the turn just played owes a scenario set-piece that has not
    happened yet -- Grand Live's concert and Unity Cup's team race.

    Generic: the scenario answers, so a scenario without set-pieces (URA, and
    base.Scenario's default) returns False and every path below is unchanged.

    `turn` is explicit for the RACE path, whose payload carries no
    current_turn -- see handle_ura_race_out."""
    chara_info = career["data"]["chara_info"]
    if turn is None:
        turn = payload.get("current_turn")
    if turn is None:
        turn = (chara_info.get("turn") or 1) - 1
    return scenarios.for_chara(chara_info).interstitial_pending(
        full_state, chara_info, turn)


@contextlib.contextmanager
def _scenario_slot_reserved(full_state: dict, active: bool):
    """Hold the response's display slot open for a scenario's set-piece chain
    (no-op unless `active`). See SCENARIO_SLOT_RESERVED_KEY."""
    if not active:
        yield
        return
    full_state[SCENARIO_SLOT_RESERVED_KEY] = True
    try:
        yield
    finally:
        full_state.pop(SCENARIO_SLOT_RESERVED_KEY, None)


CAREER_FAILED_FLAG_KEY = "_career_failed_this_response"


def _career_has_failed(full_state: dict) -> bool:
    """Has this career ended in failure?

    Checks an explicit flag as well as the stored chara_info, because the
    placement-failure path works on a DEEPCOPY of chara_info that is not
    written back until later in the handler -- reading the stored state alone
    would miss every event queued in between, which is precisely the window
    the extra events were slipping through."""
    if (full_state or {}).get(CAREER_FAILED_FLAG_KEY):
        return True
    career = (full_state or {}).get(STATE_KEY)
    if not isinstance(career, dict):
        return False
    return (career.get("data", {}).get("chara_info") or {}).get("state") == 2


def _queue_turn_event(response: dict, full_state: dict, entry: dict,
                      front: bool = False, fail_chain: bool = False) -> None:
    """Show `entry` now if this response has nothing queued yet; otherwise
    chain it behind whatever IS queued (EXTRA_EVENTS_KEY) so it plays right
    after that resolves, instead of being silently dropped. See _drain_pending
    for the other half (popping the queue as each event resolves).

    front=True puts it at the HEAD instead: it shows now and everything already
    queued moves behind it. Needed by the goal-cleared announcement, which must
    precede the turn's other events rather than trail them -- for McQueen the
    turn-24 goal lands on the same beat as the turn-25 New Year's event, and
    appending buried it two events deep where it read as never playing at all
    (live-reported). The official order is goal first, then New Year's.

    NOT YET on the unified pipeline, and the attempt is worth recording.
    Wrapping `entry` in a career_events.Event (served verbatim via Event.raw)
    and emitting it here looks like it should move all 19 remaining families at
    once for free. It does not: the wire changes in two ways the baseline
    catches immediately.

      1. URA turn 4 reorders -- 1024 ends up behind 10002. The pipeline is
         served BEFORE legacy EXTRA but AFTER the PENDING npc-unlock jump, so
         events that used to interleave through one list now cross two with
         different precedence. Adding a FIFO sequence key inside each priority
         did NOT fix it, so the cause is that split precedence, not sort order.
      2. URA turn 54 loses an outing (6000/501013803) and its +20 Wit until
         turn 71. Making drop() remove one event instead of every id-match did
         not fix this either, so something else re-drops or skips it.

    Both fixes made along the way were real and were KEPT (see career_events'
    `seq` and `drop`). The funnel itself needs the check_event resolution order
    understood first -- converting queueing while resolution still runs through
    15 branches with their own precedence is what breaks. Do that migration and
    this function's body becomes a one-line emit."""
    # A FAILED career accepts nothing but its own ending chain. Every event
    # producer in the turn pipeline runs AFTER the goal-deadline check, so
    # without this they refill the queue that _maybe_fail_goal just purged --
    # live-reported 2026-09-03 as the failure screen being followed by the
    # trainee's chara story and a support-card chain event in a run that had
    # already ended. One choke point here covers every producer, including any
    # added later, which guarding the individual call sites would not.
    if not fail_chain and _career_has_failed(full_state):
        return
    shown = response["data"].get("unchecked_event_array") or []
    if not shown and not full_state.get(SCENARIO_SLOT_RESERVED_KEY):
        response["data"]["unchecked_event_array"] = [entry]
        return
    queue = full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, [])
    if not shown:
        # Slot RESERVED for the concert chain (SCENARIO_SLOT_RESERVED_KEY): chain in
        # rather than claiming it. `front` still means "ahead of everything
        # else already chained", it just no longer means "on screen now".
        if front:
            queue.insert(0, entry)
        else:
            queue.append(entry)
        return
    if front:
        # everything currently on screen (and already chained) falls in behind
        full_state[single_mode_events.EXTRA_EVENTS_KEY] = list(shown) + list(queue)
        response["data"]["unchecked_event_array"] = [entry]
    else:
        queue.append(entry)


def _note_not_up(data: dict, not_up) -> None:
    """Merge into this response's not_up_parameter_info, which is what makes the
    CLIENT print its "nothing happened" lines on the outcome screen (see
    event_engine.not_up_info for the full mapping): status_type_array codes 1-5
    are "<stat> is in superb form", 6 "Energy is full", 20 "Mood remains Great";
    evaluation_chara_id_array is "Friendship with {0} is maxed out".

    Takes either a bare list of status codes (what an event resolver returns) or
    the whole {field: ids} dict from not_up_info. Merging rather than assigning
    keeps two sources on one event from overwriting each other -- and keeps the
    seed response's other (empty) arrays intact."""
    if not not_up:
        return
    if not isinstance(not_up, dict):
        not_up = {"status_type_array": list(not_up)}
    info = data.setdefault("not_up_parameter_info", {})
    for field, ids in not_up.items():
        if ids:
            info[field] = sorted(set(info.get(field) or []) | set(ids))


def _emit_turn_event(response: dict, full_state: dict, event: career_events.Event) -> bool:
    """career_events.emit() + claim this response's display slot if nothing
    is shown yet -- the unified-pipeline drop-in for _queue_turn_event, used
    by every family migrated onto career_events EXCEPT the goal-cleared
    banner and the goal-failed chain: those keep calling _queue_turn_event
    directly with front=True, whose "bump whatever's already shown back to
    the head of the queue" behaviour operates on the response's shared
    display slot rather than on either queue's own internals, so it already
    works correctly no matter which mechanism produced what's currently
    shown -- migrating them would only add risk for no behavioural gain.

    Returns whether the event was newly accepted (False if it had already
    fired/was already queued, matching emit()'s own return)."""
    accepted = career_events.emit(full_state, event)
    if accepted:
        shown = response["data"].get("unchecked_event_array") or []
        if not shown and not full_state.get(SCENARIO_SLOT_RESERVED_KEY):
            wire, _ev = career_events.serve_next(full_state, CURRENT_ENDPOINT.get())
            if wire is not None:
                response["data"]["unchecked_event_array"] = [wire]
    return accepted


def handle_ura_check_event(payload: dict) -> dict:
    """single_mode/check_event -- resolves a queued event.

    Three cases: (0) RACE ENTRY -- a program_id-bearing request (no event_id)
    signals "enter this race now". Confirmed from a real capture:
    single_mode/check_event is called with exactly {program_id, current_turn}
    (never race_entry's own request, which carries no race identifier at
    all) to actually transition into a race -- observed for two different
    mandatory races in the same session, so this is the general mechanism,
    not a debut-only quirk. Previously unhandled entirely (fell through to
    case (2)'s generic branch, which just re-served the same, correctly
    locked, training screen with nothing to advance it into the race) --
    likely THE root cause of the long-standing "Race Day screen stuck on
    all-locked" bug. (1) RACE handoff -- once already in the race flow, the
    response hands back race_start_info (playing_state 2), the client's
    signal to proceed to race_start. (2) EVENT chain -- apply the resolved
    event's effect (see single_mode_events) to chara_info and queue the NEXT
    event in the chain (the career-start intro is chara-intro ->
    Tazuna(+120 SP); scenario events fire from the schedule). All three build
    off the real captured check_event envelope so every field is present and
    well-formed."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    career = full_state.get(STATE_KEY)
    chara_info = career["data"]["chara_info"] if isinstance(career, dict) else None

    # RACE STAT/SP REWARD held from race_out: the real server folds the just-run
    # race's gains into the FIRST check_event after race_out (the post-race
    # story's resolution) -- chara_info is byte-identical across race_end/
    # race_out in every capture, then jumps here. Applied before any branch so
    # every response path (and sync_chara_info's blanket key sync) already
    # carries the updated stats. Not consumed by a race-ENTRY request (branch
    # (0) below re-loads state on its own and would discard the pop).
    if not payload.get("program_id"):
        reward = full_state.pop(RACE_REWARD_KEY, None)
        if reward and chara_info is not None:
            # The codes ride on full_state rather than a local: `data` does not
            # exist yet here, and the branches below each build their own.
            owed = _apply_race_reward(chara_info, reward)
            if owed:
                full_state[RACE_REWARD_NOT_UP_KEY] = owed
            state_store.save_state(viewer_id, full_state)

    # (0) RACE ENTRY -- see docstring. Takes priority over everything else:
    # a program_id here means the client wants to race THIS turn, regardless
    # of what event/pending chain state happens to also be sitting around.
    if payload.get("program_id") and not payload.get("event_id"):
        return _resolve_and_enter_race(payload)

    # (1) RACE handoff -- race-state response (hands into race_start).
    if ctx.get("race_start_info") and chara_info is not None:
        response = _load_ura_race_seed("race_check_event")
        data = response["data"]
        race_ci = copy.deepcopy(chara_info)
        race_ci["playing_state"] = 2
        race_ci["state"] = 0
        race_ci["race_program_id"] = ctx.get("program_id")
        data["chara_info"] = race_ci
        data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
        # race_condition_array is a SEPARATE program_id from the captured seed
        # (race_check_event.json) -- left stale, it still says the captured
        # fixture's own race (685) while race_start_info/chara_info correctly
        # say the trainee's ACTUAL race. The client apparently cross-checks
        # these (a mismatch here softlocks race entry -- confirmed: every uma
        # whose real race isn't coincidentally 685 got stuck right after the
        # pre-race cutscene, never reaching the race).
        if data.get("race_condition_array"):
            data["race_condition_array"][0]["program_id"] = ctx.get("program_id")
        # home_info / ura_data_set are the seed's OWN (Maruzensky, turn 12 --
        # the debut, which is why this surfaced there first). chara_info is
        # replaced above but these two never were, and sync_chara_info's
        # blanket key sync deliberately skips check_event, so the client read
        # her facility previews and -- via ura_data_set.evaluation_info_array --
        # her SUPPORT BOND values on every race entry. _prune_roster only
        # relabels those rows onto the live deck's slots; the gauge values
        # riding on them are the capture's until the field itself is replaced.
        for key in ("home_info", "ura_data_set"):
            persisted = career["data"].get(key)
            if key in data and isinstance(persisted, dict):
                data[key] = copy.deepcopy(persisted)
        data["unchecked_event_array"] = []
        return response

    # (2) EVENT chain resolution -- TRAINING-state response: player's home_info
    # (facilities enabled) + fresh ura_data_set (NPCs not pre-unlocked). Using
    # the race seed here grayed out training and pre-unlocked scenario NPCs.
    if chara_info is None:
        return _no_career_refusal(viewer_id, "single_mode/check_event", full_state)
    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    data["unchecked_event_array"] = []

    updated = copy.deepcopy(chara_info)
    # PROTECTED PLAYING STATES. While the client is inside a flow that has not
    # finished -- an event chain still running (5, base), or Grand Live's
    # backstage Live screen (10) -- an ordinary check_event must NOT stomp
    # playing_state back to normal. The scenario owns the set; see
    # scenarios/base.py's playing_states (live-reported softlock, 2026-08-26:
    # a check_event arriving while backstage silently exited the Live screen
    # before live_start ever ran, leaving the concert permanently pending and
    # the career stuck on that turn).
    if updated.get("playing_state") not in scenarios.for_chara(updated).playing_states:
        updated["playing_state"] = 1
    career_home = career["data"].get("home_info")
    unlocked = full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])
    turn = payload.get("current_turn", updated.get("turn"))
    event_id = payload.get("event_id")
    # THE UNIFIED RESOLUTION. Runs before every branch, so an event's reward can
    # never be stranded behind an early `return` -- the failure mode that made a
    # hook placed further down unreachable for anything carrying a ctx, and that
    # silently ate payouts for beats served under their own event id.
    #
    # Returns None for ids the pipeline doesn't own, so everything not yet
    # migrated falls through to its existing branch untouched. Captured (not
    # discarded) so the generic fallback at the bottom of this function can
    # tell "already resolved by the pipeline" apart from "unknown to it" --
    # applying single_mode_events.apply_effect a second time for an id the
    # pipeline just paid would double every stat/energy/mood gain on it.
    ce_resolution = career_events.resolve(full_state, updated, event_id, payload.get("choice_number"),
                                          career=career, current_turn=turn)
    # A resolver hands back response-shaping needs it can't do itself (it only
    # gets full_state/chara_info -- not this response's `data`) via its extra
    # dict. Known keys, generic on purpose so a new resolver never has to
    # touch this dispatch: "not_up" -> not_up_parameter_info's status codes
    # (see the crane-game reward and the not_up_parameter_info fix earlier
    # this session for what these mean); "factors" -> event_effected_factor_
    # array, set whenever present (even []) -- the inspiration_main resolver
    # always returns it, matching the old branch's unconditional set.
    # The race pay-out's own capped-stat notices, claimed once. Applied a few
    # lines into this handler, long before `data` existed -- this is the first
    # point that can carry them, and it is the same response the pay-out
    # itself lands on (the post-race story's resolution).
    _note_not_up(data, full_state.pop(RACE_REWARD_NOT_UP_KEY, None))
    if ce_resolution is not None:
        _note_not_up(data, ce_resolution.extra.get("not_up"))
        # ... and the codes the reward itself produced: a stat this event
        # granted while it was already capped (apply_choice snapshots the caps
        # before applying, so only ALREADY-capped stats are named).
        _note_not_up(data, (ce_resolution.applied or {}).get("not_up"))
    if ce_resolution is not None and "factors" in ce_resolution.extra:
        data["event_effected_factor_array"] = ce_resolution.extra["factors"]
    # A resolver (training_failure granting Poor Practice, rest curing one,
    # ...) can change which conditions are active -- keep the infirmary lock
    # honest for THIS response rather than the stale value career_home still
    # holds from before this event resolved (see _sync_infirmary_lock).
    if isinstance(career_home, dict):
        _sync_infirmary_lock(updated, career_home, turn)
    # The post-live beats pay here rather than in perform_live, so each reward
    # lands on the card it belongs to -- see Scenario.on_event_resolved. Covers
    # Grand Live's inline "Concert Ends" beat too, which is pushed by live_start
    # rather than queued and so never reaches the resolver registry.
    scenarios.for_chara(updated).on_event_resolved(full_state, updated, event_id)
    # ...and the capped-stat notices that hook owes. Drained HERE rather than
    # with the race pay-out's above because the hook has only just run: a
    # scenario pay-out that stashed inside it (Grand Live's concert payout and
    # its mastery bonuses) would otherwise land on the NEXT event's card.
    _note_not_up(data, full_state.pop(event_engine.PENDING_NOT_UP_KEY, None))
    # Set once here (turn doesn't change across event resolution) so every
    # branch below (duel/failure/career-event/rest/hint/normal) inherits it --
    # see _build_race_condition_array; sync_chara_info can't reach this
    # endpoint (check_event is excluded from its restore loop), so check_event
    # needs its own call site.
    if "race_condition_array" in data:
        data["race_condition_array"] = _build_race_condition_array(
            updated, turn, career["data"].get("race_history", []))

    # DUEL COMMIT: migrated onto career_events -- see the "duel" resolver
    # (_resolve_duel) for the win/lose reward + versus_level bump, applied
    # above via the unified resolve() call (which also receives choice_number
    # directly, same as this branch used to via payload).

    # TRAINING FAILURE COMMIT: migrated onto career_events -- see the
    # "training_failure" resolver (_resolve_training_failure) for the
    # Top/Bottom outcome, applied above via the unified resolve() call.

    # DYNAMIC CAREER EVENT COMMIT (support chain 10002 / random 20000 / outing
    # 6000): apply the chosen choice's effects via the engine, then clear the
    # context. choice_number is the picked gain_select_id_index (1..N; a 1-choice
    # event commits with 0).
    cev_ctx = _career_ctx_for(full_state, event_id)
    # HAVING A CTX IS THE CONDITION -- not membership of CAREER_EVENT_IDS. A ctx
    # only exists for an event we queued ourselves, so this is precise, and both
    # PREVIEW paths already use exactly this rule; only the commit disagreed.
    # That mismatch silently costs the payout for any event served under its own
    # id: 'inline' post-race events needed a special case here, and the moment
    # the shared beats started using their real captured ids (Extra Training
    # 7017) they stopped paying out entirely while still previewing correctly.
    if cev_ctx:
        ev, _ = _career_event_from_ctx(full_state, event_id)
        # The gamble was already decided when this event was SERVED, and the
        # client is playing that arm's text right now -- read it back BEFORE
        # anything applies, and pay it rather than rolling a second time (see
        # event_engine._choice_branch_index).
        served_branch = _served_branch(full_state, event_id,
                                       payload.get("choice_number", 1))
        if ev:
            dcid = event_engine.source_default_chara(cev_ctx.get("source"), cev_ctx.get("source_id"))
            applied = event_engine.apply_choice(
                updated, ev, payload.get("choice_number", 1), dcid,
                source_card_id=_event_source_card(cev_ctx),
                full_state=full_state,
                chain_owner=event_engine.chain_owner_token(
                    cev_ctx.get("source"), cev_ctx.get("source_id")),
                branch=served_branch) or {}
            # "<stat> is in superb form" / "Energy is full." / "Friendship with
            # {0} is maxed out." for whatever this choice granted that was
            # already at its ceiling -- see _note_not_up.
            _note_not_up(data, applied.get("not_up"))
        # PAL/GROUP outing bookkeeping (unlock roll, outing step, Pure Passion)
        # for the same event, if this was one of theirs.
        try:
            _commit_pal_event(full_state, updated, payload.get("choice_number", 1),
                              ev, served_branch)
        except Exception:
            log.exception("pal/group outing commit failed; event effects kept")
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            # Same reasoning as the ce_resolution sync above: this choice's OWN
            # effects (event_engine.apply_choice, e.g. a story branch granting
            # Slow Metabolism / Night Owl / ...) can grant or cure a condition
            # too, after that first sync already ran.
            _sync_infirmary_lock(updated, career_home, turn)
            data["home_info"] = copy.deepcopy(career_home)
        _apply_versus(data, full_state, turn)
        player_chara = (updated.get("card_id") or 0) // 100
        data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
        career["data"]["chara_info"] = updated
        # TEMP INSTRUMENTATION (firing-rate investigation): how many turns a
        # support-card ctx sat queued before the client actually resolved it.
        # Remove alongside the pending_skip log once answered.
        if cev_ctx.get("queued_turn") is not None:
            log.info("SUPPORT_FIRE_RATE resolved event_id=%s queued_turn=%s "
                     "resolved_turn=%s pending_span=%s", event_id,
                     cev_ctx.get("queued_turn"), turn,
                     (turn - cev_ctx.get("queued_turn")) if turn is not None else None)
        _drop_career_ctx(full_state, cev_ctx)
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    # REST EVENT RESOLUTION: migrated onto career_events -- see the "rest"
    # resolver (_resolve_rest) for the energy apply / failure-rate refresh,
    # applied above via the unified resolve() call.

    # SKILL HINT REVEAL RESOLUTION: migrated onto career_events -- each
    # reveal is its own Event carrying its own choices/effects (see the
    # queue site, _hint_reveals handling in handle_exec_command), applied by
    # career_events.resolve()'s generic applier above; find()/drop() target
    # whichever instance is currently ACTIVE, so multiple same-turn reveals
    # still resolve one at a time in order without a separate pending list.

    # CRANE-GAME LAUNCH: migrated onto career_events (see the "crane_launch"
    # resolver), but the EMPTY-queue invariant still needs its own branch --
    # nothing else may be drained here, the client comes back via
    # single_mode/minigame_end, same reasoning as Grand Live's Concert Begins
    # below.
    if event_id == _CRANE_INTRO_EVENT and ce_resolution is not None:
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = []
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    # CRANE-GAME OUTCOME RESOLUTION: migrated onto career_events -- see the
    # "crane_outcome" resolver for the payout (vital/guts/mood/hint), applied
    # above via the unified resolve() call.

    # MID-RUN INSPIRATION COMMIT: migrated onto career_events -- see the
    # "inspiration_main" resolver for the stat/aptitude/cap gains and the
    # event_effected_factor_array dispatch above (the "factors" extra key).

    # NPC APPRAISAL RESOLUTION: migrated onto career_events -- see the
    # "appraisal" resolver (_resolve_appraisal) for the tiered reward + bond
    # deepening, applied above via the unified resolve() call.

    # END-OF-RUN PAYOUT: the reporter / Director sendoffs and the trainee's own
    # final event each pay all-five stats + SP when they resolve (#40-#43).
    payouts = full_state.get(ENDING_PAYOUT_KEY) or {}
    if event_id is not None and str(event_id) in payouts:
        pay = payouts.pop(str(event_id))
        # These pay by writing the stats directly rather than through a
        # choice, so nothing here builds the gains list not_up_info reads --
        # name the stats instead, BEFORE applying, or the final trainee event
        # / Director sendoff stays silent on a maxed trainee.
        owed = event_engine.capped_stat_codes(
            updated if pay.get("stats", 0) > 0 else None, _ALL_STATS)
        for stat in _ALL_STATS:
            event_engine.add_stat(updated, stat, pay.get("stats", 0))
        updated["skill_point"] = updated.get("skill_point", 0) + pay.get("sp", 0)
        full_state[ENDING_PAYOUT_KEY] = payouts
        log.info("ending payout on event %s: +%s all stats, +%s SP%s",
                 event_id, pay.get("stats"), pay.get("sp"),
                 " (at ceiling -> not_up_parameter_info %s)" % owed if owed else "")
        _note_not_up(data, owed)
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        player_chara = (updated.get("card_id") or 0) // 100
        data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    # INFIRMARY RESOLUTION: migrated onto career_events -- see the
    # "infirmary" resolver (_resolve_infirmary) for the +20 energy / cure-
    # chance logic and the not_up/command_info-refresh dispatch above.

    # RAFFLE RESOLUTION: migrated onto career_events -- its effects ride on
    # the Event itself (see the queue site, _CHARA_FIXED_STORY_EVENTS' suffix
    # 302 branch) and are already applied by career_events.resolve() above,
    # so nothing family-specific is left to do here; it falls through to the
    # generic tail like any other now-unified event.

    # GRAND LIVE CONCERT BEGINS: resolving this beat (already handled above by
    # career_events.resolve -> the "grand_live_beat" resolver, which flipped
    # playing_state to backstage and set concert_turn) must leave the response
    # with NOTHING else queued -- same requirement as the crane-game launch
    # above, and for the same reason: the client's cue to open a whole
    # separate screen (backstage here, the minigame there) is "nothing
    # follows", not a flag it reads off the event itself.
    #
    # BUG FIXED 2026-08-24 (live-reported: the concert never actually played
    # on turn 24; stuck there forever afterward). This event_id falls through
    # every named branch above to the generic one below, which unconditionally
    # calls _drain_pending -- and career_events.resolve() already dropped this
    # event from `active` (see its own drop() call) by the time we get here,
    # so _drain_pending's already-active guard (added for a near-identical
    # past incident, see its own docstring) does NOT protect this case: it
    # sees nothing active and happily serves whatever's queued next -- the
    # unified pipeline's own queue if anything is scenario-queued there, or
    # otherwise a completely ordinary per-turn character/support event sitting
    # in the legacy EXTRA_EVENTS_KEY queue -- in this SAME response. The
    # client then never gets the empty-chain signal it needs to open the
    # backstage screen, so it just keeps showing normal training on a turn
    # whose served number is now frozen at 24 (see the scenario's held_turn,
    # released only once the concert's own post-live chain resolves) -- which it
    # never can, since live_start was never reachable.
    # A SCENARIO CHAIN THE SCENARIO SERVES ITSELF, continued: Unity Cup's
    # post-race result beats do not go through the event queues (they are
    # withheld until the race has actually been run), so the response that
    # resolves one has to carry the next -- ALONE. Falling through to
    # _drain_pending instead serves the turn's own queued event and the client
    # drops the rest of the chain: on turn 60 the elite-win beat 201166 was
    # lost that way, and because the team-race turn hold waits on exactly that
    # beat the career was stuck on turn 60 (user-reported 2026-09-06).
    chain_next = scenarios.for_chara(updated).chain_entry_after(full_state, event_id)
    if chain_next:
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = [chain_next]
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    if scenarios.for_chara(updated).holds_chain_after(event_id):
        data["chara_info"] = updated
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
        data["unchecked_event_array"] = []
        career["data"]["chara_info"] = updated
        _remember_display(full_state, response)
        state_store.save_state(viewer_id, full_state)
        return response

    # Normal scenario-event chain resolution. Skipped when the unified
    # pipeline already resolved this id above (ce_resolution is not None) --
    # its own apply_choice already paid the effects; running the legacy
    # applier too would double them.
    if event_id and ce_resolution is None:
        _note_not_up(data, single_mode_events.apply_effect(updated, event_id))
        # A SCENARIO_SCHEDULE event is done once the client RESOLVES it --
        # only then may its turn refuse to re-queue it (see handle_exec_command;
        # marking at queue time lost undisplayed cutscenes to reloads).
        if any(event_id in evs for evs in
               single_mode_events.SCENARIO_SCHEDULE.values()):
            done = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
            if event_id not in done:
                full_state[SCENARIO_EVENTS_DONE_KEY] = sorted(done | {event_id})
    # NPC unlock registration happens at the cutscene's RESOLUTION -- THIS
    # response gains the NPC's evaluation row, which is the flip the client
    # announces as 'X will now appear in training'. Registering at queue time
    # (the old way) meant the row already existed before the cutscene resolved,
    # so nothing visibly changed and turns 3/4 never SHOWED the Director/Meek
    # joining (live-reported repeatedly).
    #
    # DELIBERATELY OUTSIDE the `ce_resolution is None` guard above. It used to
    # live inside it, which quietly made unlocks a URA-ONLY feature: every
    # scenario that serves its beats through the unified career_events pipeline
    # takes the `ce_resolution is not None` branch, so its unlock table entry
    # was never read. Grand Live's 202002 is exactly that case -- Light Hello
    # appeared only because she is usually an equipped SUPPORT CARD and the
    # deck-position branch below is reached through a different code path, so
    # the bug was invisible whenever the player brought her card. The Director
    # has no card, so she never appeared at all. Unlike the legacy applier this
    # block pays no effects and is idempotent (`newly_unlocked`), so running it
    # on both paths cannot double anything.
    if event_id:
        _register_npc_unlocks(full_state, updated, event_id)
    player_chara = (updated.get("card_id") or 0) // 100
    data["unchecked_event_array"] = _drain_pending(full_state, event_id, player_chara)
    data["chara_info"] = updated
    if isinstance(career_home, dict):  # facilities enabled + our previews
        data["home_info"] = copy.deepcopy(career_home)

    # Happy Meek's duel appearance: place her consistently (partner slot + marker
    # + evaluation) so the icon is on the facility she's actually in.
    _apply_versus(data, full_state, updated.get("turn", turn))
    # Re-assert a JUST-UNLOCKED NPC's announcement AFTER the versus reconcile:
    # _reconcile_npc_appearance resets is_appear to facility placement, which
    # stomped the flip on this very response (the Director isn't placed on turn
    # 3) -- the flip then leaked onto a later event, nameless. The capture's
    # 1014 response carries is_appear=1 unplaced, plus the ura_data_set
    # {target, chara, member_state 0} row the client needs to NAME her.
    for npc in single_mode_events.npcs_unlocked_by(event_id):
        # Same rule as the registration loop above: a character already
        # present as a deck card gets ONLY her deck row re-asserted, never
        # the separate NPC-band row/ura_data_set entry (that's the duplicate
        # "2 light hellos" the NPC-band path would otherwise reintroduce
        # right here even when the registration loop above skipped it).
        deck_position = _deck_position_for_chara(updated, npc[1])
        if deck_position is not None:
            for row in updated.get("evaluation_info_array") or []:
                if row.get("target_id") == deck_position:
                    row["is_appear"] = 1
            continue
        for row in updated.get("evaluation_info_array") or []:
            if row.get("target_id") == npc[0]:
                row["is_appear"] = 1
        uds = data.get("ura_data_set")
        if isinstance(uds, dict):
            uev = uds.setdefault("evaluation_info_array", [])
            if not any(e.get("target_id") == npc[0] for e in uev
                       if isinstance(e, dict)):
                uev.append({"target_id": npc[0], "chara_id": npc[1],
                            "member_state": 0})

    career["data"]["chara_info"] = updated
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def _apply_versus(data, full_state, turn):
    """Place Happy Meek consistently into a served training screen (home_info +
    ura_data_set + evaluation) using the stored versus_level / last-duel-turn.
    Returns her facility this turn (or None).

    HAPPY MEEK IS URA-ONLY. She has no evaluation row anywhere in the Grand Live
    capture and her unlock event (102005) never fires there, so a scenario-3
    career must never see her marker, her duels or her finals branch. This is
    the single gate for all of that -- every caller routes through here."""
    if not scenarios.for_chara((data or {}).get("chara_info")).has_versus_npc:
        return None
    return single_mode_events.apply_versus_state(
        data, turn,
        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []),
        full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1),
        full_state.get(single_mode_events.DUEL_LAST_TURN_KEY))


def _happy_meek_facility(turn, full_state, chara_info=None):
    """The facility Happy Meek is dueling in this turn, or None -- the same value
    used to place her marker, so training it triggers exactly her duel. Always
    None in Grand Live, where she doesn't exist."""
    if not scenarios.for_chara(chara_info).has_versus_npc:
        return None
    return single_mode_events.happy_meek_facility(
        turn,
        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, []),
        full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1),
        full_state.get(single_mode_events.DUEL_LAST_TURN_KEY))


def _duel_choice_to_stat(choice_number, options):
    """Map a duel commit's choice_number to the chosen stat (1-5). The client
    returns the picked option's select_index, which duel_event_entry sets equal
    to the stat -- so a valid 1-5 IS the stat. Fall back to treating it as a
    1-based ordinal into the offered options, else the first option."""
    opts = options or []
    if choice_number in (1, 2, 3, 4, 5):
        return choice_number
    if opts and isinstance(choice_number, int) and 1 <= choice_number <= len(opts):
        return opts[choice_number - 1]
    return opts[0] if opts else 1


@career_events.resolver("duel")
def _resolve_duel(full_state, chara_info, event, choice_number, current_turn=None, **kw):
    """Apply the chosen stat's win/lose reward and bump versus_level on a
    win -- moved verbatim from the old DUEL COMMIT branch. stat_options/
    versus_level ride on the Event's own payload instead of DUEL_CTX_KEY."""
    ctx = event.payload or {}
    vlevel = ctx.get("versus_level", full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1))
    stat_index = _duel_choice_to_stat(choice_number, ctx.get("stat_options"))
    duel_result = single_mode_events.resolve_duel(chara_info, stat_index, vlevel)
    # Her level rises only on a WIN -- "Winning the duel against Happy Meek
    # will provide a reward and raise Happy Meek's level, increasing the
    # difficulty of subsequent duels" (user-supplied). It used to increment
    # on every duel, so losing still made the next one harder.
    if duel_result.get("won"):
        full_state[single_mode_events.VERSUS_LEVEL_KEY] = vlevel + 1
    full_state[single_mode_events.DUEL_LAST_TURN_KEY] = current_turn if current_turn is not None else chara_info.get("turn")
    return {}


@career_events.resolver("training_failure")
def _resolve_training_failure(full_state, chara_info, event, choice_number, **kw):
    """Applies the chosen Top(1)/Bottom(2) infirmary outcome -- moved
    verbatim from the old TRAINING FAILURE COMMIT branch. FAIL_CTX_KEY is
    still cleared here even though the queue side keeps writing it (the
    preview endpoint reads it independently -- see that call site)."""
    payload = event.payload or {}
    single_mode_events.resolve_failure(
        chara_info, payload.get("worst", False), choice_number or 1,
        payload.get("stat") or "speed")
    full_state.pop(single_mode_events.FAIL_CTX_KEY, None)
    return {}


def handle_get_choice_reward(payload: dict) -> dict:
    """single_mode/get_choice_reward -- for the Happy Meek DUEL this is only the
    PREVIEW: the client calls it (with NO choice_number) to fill the 3 buttons
    with each option's win/lose rewards. It must NOT apply anything -- the actual
    commit is check_event(event_id=102001, choice_number=X) (see
    handle_ura_check_event). Applying here (defaulting the missing choice_number
    to 1) is what made every duel resolve to speed."""
    viewer_id = payload["viewer_id"]
    event_id = payload.get("event_id")
    full_state = state_store.get_state(viewer_id) or {}

    if event_id == single_mode_events.DUEL_EVENT_ID:
        vlevel = full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1)
        ctx = full_state.get(single_mode_events.DUEL_CTX_KEY) or {}
        options = ctx.get("stat_options") or single_mode_events.duel_stat_options(vlevel)
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": single_mode_events.duel_preview(options)}}

    # TRAINING FAILURE preview (7014 Normal / 7015 Worst): like the duel, this is
    # only the PREVIEW that fills the Top/Bottom buttons -- it applies NOTHING.
    # The commit is check_event(event_id, choice_number) (see handle_ura_check_event).
    ctx = full_state.get(single_mode_events.FAIL_CTX_KEY) or {}
    if event_id in (single_mode_events.NORMAL_FAIL_EVENT, single_mode_events.WORST_FAIL_EVENT):
        preview = single_mode_events.failure_preview(
            ctx.get("worst", event_id == single_mode_events.WORST_FAIL_EVENT),
            ctx.get("stat") or "speed")
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": preview}}

    # DYNAMIC CAREER EVENT preview (support chain 10002 / random 20000 / outing
    # 6000): each choice's gains, straight from the engine. Applies nothing.
    if event_id in event_engine.CAREER_EVENT_IDS or _career_ctx_for(full_state, event_id):
        ev, ctx = _career_event_from_ctx(full_state, event_id)
        if ev and ctx.get("event_id") == event_id:
            ci = (full_state.get(STATE_KEY) or {}).get("data", {}).get("chara_info")
            dcid = event_engine.source_default_chara(ctx.get("source"), ctx.get("source_id"))
            partner, chances = _pal_unlock_preview_args(full_state)
            return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, dcid, partner, chances,
                        source_card_id=_event_source_card(ctx))}}

    # ORDINARY STORY EVENT preview. No queued ctx names this event -- it was
    # served as a plain story entry (the cutscene path, which carries its
    # choices on the wire but keeps no context), or its ctx was lost. The
    # client still wants each button's gains, and returning [] left every one
    # of them blank, which is what the player actually sees on the large
    # majority of chara story events with 2-3 choices.
    #
    # The event the client is asking about is whatever is queued under this
    # event_id right now, so take its story_id from the queue and resolve the
    # same event data the commit path uses. Preview only -- applies nothing.
    story_id = _pending_event_story(full_state, event_id)
    if story_id:
        career = full_state.get(STATE_KEY) or {}
        ci = career.get("data", {}).get("chara_info") or {}
        card_id = ci.get("card_id") or 0
        try:
            ev = event_engine.resolve(story_id, event_engine.event_title(story_id),
                                      card_id)
        except Exception:
            log.exception("choice-reward preview resolve failed for story %s", story_id)
            ev = None
        if ev and ev.get("choices"):
            return {"response_code": 1,
                    "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, (card_id // 100) or None)}}

    data = single_mode_events.choice_reward(event_id, payload.get("choice_number", 0))
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _pending_event_story(full_state: dict, event_id):
    """story_id of the event currently queued under `event_id`, or None.

    Looks in both persisted queues an event can be waiting in -- the unified
    career_events pipeline (active first, then its queue) and the legacy
    EXTRA_EVENTS_KEY chain. The response's own display slot is gone by the
    time a separate get_choice_reward request arrives, so the queues are all
    there is to go on. (PENDING_EVENTS_KEY holds bare intro event ids, not
    entries, and those are cutscenes with nothing to preview.)"""
    if event_id is None:
        return None
    found = career_events.find(full_state, event_id)
    if found is not None:
        raw = getattr(found, "raw", None) or {}
        if raw.get("story_id"):
            return raw["story_id"]
        if getattr(found, "story_id", None):
            return found.story_id
    for entry in full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or ():
        if isinstance(entry, dict) and entry.get("event_id") == event_id:
            return entry.get("story_id")
    return None


# ======================================================== GRAND LIVE ONLY ===


# Hint-level SP discount on skill purchases (Lv1-5 -> 10/20/30/35/40%), the
# game's fixed ladder. Not found as a master.mdb table -- hardcoded like the
# client's own constants.
_HINT_SP_DISCOUNT = {1: 0.10, 2: 0.20, 3: 0.30, 4: 0.35, 5: 0.40}


_FAST_LEARNER_SP_DISCOUNT = 0.10   # the whole point of the condition


def _skill_purchase_cost(skill_id, tips_by_group: dict, conditions=()) -> int:
    """Base SP cost from master.mdb (single_mode_skill_need_point, keyed by the
    skill id itself), discounted by the trainee's hint level for that skill's
    group (skill_id // 10 -- same group derivation _apply_skill_hint uses),
    and again by Fast Learner if she currently has it -- that SP discount is
    what makes the condition valuable and it was previously not applied at
    all (the condition was cosmetic)."""
    row = master_data.query_one(
        "SELECT need_skill_point FROM single_mode_skill_need_point WHERE id=?", (skill_id,))
    base = row["need_skill_point"] if row else 0
    level = tips_by_group.get(int(skill_id) // 10, 0)
    cost = base * (1 - _HINT_SP_DISCOUNT.get(min(5, level), 0.0))
    if event_engine._FAST_LEARNER_CONDITION in (conditions or ()):
        cost *= (1 - _FAST_LEARNER_SP_DISCOUNT)
    return int(round(cost))


def handle_gain_skills(payload: dict) -> dict:
    """single_mode/gain_skills -- commits the Skills screen's purchases.

    The only 'capture' of this endpoint (UmaDumpy 20260721 txn 0092) has a
    MISLABELED request (its raw msgpack is an exec_command body -- the tool's
    known request-pairing bug) but a REAL response: a bare empty-data
    envelope. So, like multi_race_reserve and check_event(program_id), the
    client computes the purchase locally and just notifies the server; the
    server's job is to PERSIST it so every subsequent response (and the race
    simulator, which reads chara_info.skill_array for skill activation)
    reflects the new skills.

    The request's field name (gain_skill_info_array, [{skill_id, level}]) is
    now CONFIRMED from a live RAW dump of a real purchase against this
    server; the other candidate names are kept as fallbacks only.

    SP cost is computed server-side from master.mdb (base need_skill_point x
    hint discount, see _skill_purchase_cost) rather than trusting a
    client-sent total that may not even exist in the request. Re-buying a
    skill already owned at >= the requested level is FREE and a no-op --
    the real client never offers an owned skill for sale, so a repeat can
    only come from a client whose view is stale, and charging for it drains
    SP invisibly.

    RESPONSE SHAPE (live-bug lesson): the one captured response (empty
    data {}) came from the same transaction whose REQUEST was provably
    mislabeled -- and serving that empty envelope live made the client WIPE
    its skill screen: SP reverted, purchases showed unbought (rebuyable
    over and over), and the uma-details pane went blank until the next
    exec_command re-served real state. The client evidently rebuilds its
    in-career state from THIS response like any other state-changing
    endpoint, so it gets the full training-state envelope (updated
    chara_info + home_info + ura_data_set + fresh race_condition_array),
    not the untrustworthy captured emptiness."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}}, "data": {}}
    chara_info = career["data"]["chara_info"]

    purchased = None
    for field in ("gain_skill_info_array", "skill_info_array", "gain_skill_array",
                  "skill_array", "skill_id_array"):
        if payload.get(field):
            purchased = payload[field]
            break

    # Normalize: entries may be {skill_id, level} dicts or bare ids.
    entries = []
    for p in purchased or []:
        if isinstance(p, dict) and p.get("skill_id"):
            entries.append({"skill_id": int(p["skill_id"]), "level": int(p.get("level") or 1)})
        elif isinstance(p, int):
            entries.append({"skill_id": p, "level": 1})

    tips_by_group = {t.get("group_id"): t.get("level", 0)
                     for t in chara_info.get("skill_tips_array", [])}
    skills = chara_info.setdefault("skill_array", [])
    by_id = {s["skill_id"]: s for s in skills}
    total_cost = 0
    for e in entries:
        existing = by_id.get(e["skill_id"])
        if existing and existing.get("level", 1) >= e["level"]:
            continue  # already owned at this level -- free no-op, never re-charge
        total_cost += _skill_purchase_cost(e["skill_id"], tips_by_group,
                                           chara_info.get("chara_effect_id_array") or ())
        if existing:
            existing["level"] = e["level"]
        else:
            new = {"skill_id": e["skill_id"], "level": e["level"]}
            skills.append(new)
            by_id[e["skill_id"]] = new

    chara_info["skill_point"] = max(0, chara_info.get("skill_point", 0) - total_cost)
    state_store.save_state(viewer_id, full_state)
    if entries:
        log.info("gain_skills: +%d skills, -%d SP (now %d) for viewer %s",
                 len(entries), total_cost, chara_info["skill_point"], viewer_id)

    # Full training-state envelope (see docstring) -- the client rebuilds its
    # skill screen / uma details from this response.
    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    updated = copy.deepcopy(chara_info)
    updated["playing_state"] = 1
    data["chara_info"] = updated
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    turn = payload.get("current_turn", updated.get("turn"))
    _apply_versus(data, full_state, turn)
    # ...but only the two fields the real one carries. Every real gain_skills
    # capture across all four scenarios (28 of them) has exactly
    # {chara_info, home_info, <scenario>_data_set} -- the data_set arrives from
    # sync_chara_info afterwards, so those two are all that belongs here. The
    # rest of the train_check_event seed (race_condition_array, race_start_info,
    # unchecked_event_array, not_up/not_down_parameter_info, ...) is envelope
    # this endpoint never sends.
    keep = ["chara_info", "home_info"] + [k for k in data if k.endswith("_data_set")]
    response["data"] = {k: data[k] for k in keep if k in data}
    return response


def handle_ura_race_start(payload: dict) -> dict:
    """When race_entry successfully ran a REAL simulation (_simulate_career_race),
    reuse the captured single_mode/race_start envelope's shape but replace
    race_scenario/race_start_info wholesale with the EXACT roster/seed/scenario
    already stashed in RACE_CTX_KEY at entry time -- so this response is
    internally consistent with what entry already showed (same horses, same
    seed) and the animation genuinely matches the resolved race, instead of
    always replaying Grass Wonder's captured debut regardless of who's racing.
    Falls back to the old static replay (player horse patched) when no
    simulation is available (course couldn't be resolved, roster too small,
    node unavailable, ...)."""
    viewer_id = payload["viewer_id"]
    # SAME BUG as race_entry (see its docstring, 2026-08-20): no genuine
    # "single_mode/race_start" capture exists either, so this used to bail
    # into handle_race_start -- raw Maruzensky replay, animation and all --
    # instead of reaching the race_ctx overwrite below (live-reported: paddock
    # correctly showed the real trainee after the race_entry fix, but the
    # race itself still ran as swimsuit Maruzensky). The team capture is only
    # ever used here as an envelope SHAPE template; race_scenario/
    # race_start_info get replaced wholesale from RACE_CTX_KEY right below
    # when a real simulation ran, same as the URA-shaped fixture would be.
    pair = fixtures.first("single_mode/race_start") or fixtures.first("single_mode_team/race_start")
    if pair is None:
        raise LookupError("No captured race_start fixture available")
    response = pair.response_copy()
    data = response.get("data", {})

    full_state = state_store.get_state(viewer_id) or {}
    race_ctx = full_state.get(RACE_CTX_KEY) or {}
    if race_ctx.get("race_scenario") and race_ctx.get("race_start_info"):
        data["race_scenario"] = race_ctx["race_scenario"]
        data["race_start_info"] = copy.deepcopy(race_ctx["race_start_info"])
    else:
        _patch_player_horse(data.get("race_start_info", {}), _career_chara_info(viewer_id))
    return response


_URA_RACE_SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "seeds" / "ura_race"
_ura_race_seed_cache: dict = {}


def _no_career_refusal(viewer_id, where: str, full_state: dict) -> dict:
    """Refuse, rather than serve a raw URA seed, when the career state is gone.

    Every seed under data/seeds/ura_race is one real captured MARUZENSKY run
    (card 101101; race_check_event is frozen at turn 12 -- the debut, race_end/
    race_out at turn 23). Handlers here patch the player's own chara_info over
    that envelope, but each one used to fall through to serving it UNPATCHED
    when full_state[STATE_KEY] was missing -- handing the client Maru's stats,
    aptitudes and support bonds, which it then banks over the live run.
    Live-reported 2026-09-05 at a Mayano debut: every stat moved by ~100 in
    both directions, aptitudes and bonds changed, run softlocked.

    A refusal softlocks the same turn, but the persisted career survives it and
    the log says which endpoint saw no state -- neither is true of the leak."""
    log.error("%s: no career state for viewer %s -- refusing rather than serving "
              "the raw Maruzensky seed. full_state keys=%s",
              where, viewer_id, sorted(full_state or {}))
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


def _load_ura_race_seed(name: str) -> dict:
    """A real captured URA race_end/race_out response envelope (from the Icarus
    trace of a real-server career) -- has the URA-correct shape incl. ura_data_set
    that our old team skeleton lacked."""
    if name not in _ura_race_seed_cache:
        with open(_URA_RACE_SEED_DIR / f"{name}.json", encoding="utf-8") as f:
            _ura_race_seed_cache[name] = json.load(f)
    return copy.deepcopy(_ura_race_seed_cache[name])


def _career_win_streak(race_history, result_rank: int) -> int:
    """Consecutive wins ending with THIS race, clamped to master.mdb's 1-5
    `single_mode_reward_set.bonus` tiers.

    `bonus` is a server-side roll input the client never sends (see dump.cs's
    MasterSingleModeRewardSet.SingleModeRewardSet -- the client only ever reads
    the resulting RaceRewardData arrays), so its exact meaning is inferred:
    tiers 1-4 carry an identical drop table and only tier 5 adds the two rare
    entries (a 5% support-point drop and a 1% ticket), which reads as a
    streak/consistency reward. Every reward_type=2 row is also order 1-1, i.e.
    the bonus table only ever pays out on a win, which matches."""
    if result_rank != 1:
        return 0
    streak = 1
    for entry in reversed(list(race_history or ())):
        if entry.get("result_rank") == 1:
            streak += 1
        else:
            break
    return max(1, min(5, streak))


def _build_race_reward_info(program_id, result_rank: int, gained_fans: int,
                            race_history=None, rng=None) -> dict:
    """The post-race item reward card, computed from the race that was ACTUALLY
    run instead of replayed from the captured fixture.

    master.mdb: single_mode_program.reward_set_id -> single_mode_reward_set,
    whose rows are independent drops rolled at `odds`/1,000,000 and filtered by
    the finish place (order_min..order_max). reward_type 1 is the base
    `race_reward` list (the guaranteed support points plus, on a win, that
    race's trophy and its grade's cup, both item_category 11); reward_type 2 is
    the `race_reward_bonus` list, picked by the `bonus` tier -- see
    _career_win_streak. Row order within the response is master row id order,
    which is what real captures show (trophy, points, cup).

    `campaign_id_array` / `race_reward_plus_bonus` / `race_reward_bonus_win`
    stay empty: they are live-ops campaign multipliers (real captures show
    e.g. [223] only while a campaign was running) and this server runs none.

    Falls back to an empty reward list rather than the fixture's if the program
    or its reward set can't be resolved."""
    rng = rng or random
    info = {
        "result_rank": int(result_rank),
        "result_time": 0,
        "race_reward": [],
        "race_reward_bonus": [],
        "race_reward_plus_bonus": [],
        "race_reward_bonus_win": [],
        "gained_fans": int(gained_fans),
        "campaign_id_array": [],
    }
    if not program_id:
        return info
    program = master_data.query_one(
        "SELECT reward_set_id FROM single_mode_program WHERE id=?", (program_id,))
    if not program or not program["reward_set_id"]:
        return info
    rows = master_data.query(
        "SELECT reward_type, bonus, odds, item_category, item_id, item_num "
        "FROM single_mode_reward_set WHERE reward_set_id=? "
        "AND order_min<=? AND order_max>=? ORDER BY id",
        (program["reward_set_id"], result_rank, result_rank))
    tier = _career_win_streak(race_history, result_rank)
    for row in rows:
        if row["reward_type"] == 2 and row["bonus"] != tier:
            continue
        if rng.randrange(1000000) >= (row["odds"] or 0):
            continue
        item = {"item_type": row["item_category"], "item_id": row["item_id"],
                "item_num": row["item_num"]}
        key = "race_reward" if row["reward_type"] == 1 else "race_reward_bonus"
        info[key].append(item)
    return info


def _fan_gain_for_race(chara_info: dict, program_id, result_rank: int,
                       full_state: dict = None) -> int:
    """Real base fan gain for finishing `result_rank` in this race, from
    master.mdb: single_mode_program.fan_set_id -> single_mode_fan_count, the
    genuine per-finish-place fan table (e.g. a maiden race tops out near 700
    for 1st, a top G1 can be 13000+, and it falls off fast toward last place --
    previously a single flat 1000 regardless of the race's prestige or where
    the trainee actually placed). Multiplied by the trainee's support deck's
    total fan_bonus%, the same pattern as the skill-hint-rate formula (see
    _skill_hint_chance): sum each equipped card's fan_bonus effect (cards.json,
    GameTora-sourced, same field _card_effect/card_effect already reads for
    specialty_priority/initial_friendship_gauge elsewhere in this file).
    Falls back to a flat placeholder if the program/fan set can't be resolved
    (only expected for the static-fixture-replay fallback with no real
    program_id)."""
    base = 1000
    if program_id:
        program = master_data.query_one(
            "SELECT fan_set_id FROM single_mode_program WHERE id=?", (program_id,))
        if program:
            row = master_data.query_one(
                "SELECT fan_count FROM single_mode_fan_count WHERE fan_set_id=? AND [order]=?",
                (program["fan_set_id"], result_rank))
            if row:
                base = row["fan_count"]
    bonus_pct = sum(
        (training_formula.card_effect(sid, "fan_bonus", 0) or 0) for sid in _career_deck(chara_info))
    # ...and the scenario's own share on the same term -- Trackblazer's Glow
    # Sticks are +50% fans for the turn they are used on, and ride
    # item_effect_array as effect_type 14 with effect_value_1 40 (the fan
    # target) rather than 6 (the race-bonus one).
    if full_state is not None:
        bonus_pct += scenarios.for_chara(chara_info).fan_bonus_pct(full_state, chara_info)
    # Hot Topic ('builds rapport with fans') boosts the gain on top of the deck.
    return conditions.fan_gain(chara_info, int(round(base * (1 + bonus_pct / 100))))


# ---- race stat/SP rewards ----------------------------------------------------
# NOT in master.mdb (verified: no table maps grade x placement -> stat/SP; the
# only shaped tables, single_mode_free_win_point / _free_coin_race, feed free
# mode). Derived from TWO independent real URA career captures and corroborated
# by published JP tables (umamusumelabo / game8): every race pays
#     gain = floor(base * (1 + race_bonus%)),   per stat and for SP,
# where URA base = ALL five stats +3 for every pre-finals grade, SP 30 (OP and
# below) / 35 (G2/G3) / 45 (G1); the URA Finals rounds are their own richer
# tier. The popular EN guides' "G1 = +10 to a stat" is wrong for URA (that's
# Trackblazer): captures show +3 to ALL FIVE, scaling to +4 at >=34% bonus.
# GOAL RACES ONLY (see _goal_race_reward). The old reading of the captures --
# that goal and non-goal races pay identically, since an adjacent G2 goal and G1
# optional differed only by grade -- was wrong: the user's 07-28 table gives
# voluntary races a completely different placement x grade payout with an energy
# cost (_POST_RACE_TABLE). What this table describes is the goal-race result
# screen: ALL FIVE stats + SP, no energy, no choice.
# race.grade code -> (per-stat base, SP base) at 1st-3rd place.
_RACE_REWARD_BASE = {
    100: (3, 45),                 # G1
    200: (3, 35), 300: (3, 35),   # G2 / G3
    400: (3, 30), 500: (3, 30), 600: (3, 30), 700: (3, 30),  # OP / Pre-OP tiers
    800: (3, 30), 900: (3, 30),   # maiden / debut
}
# finals_round -> (per-stat base, SP base). SP 40/60/80 matches capture
# 20260717 exactly AND umamusumelabo's published table; the Icarus capture's
# richer round-3 delta (+28/SP 210) bundles the ending chain's own event
# effects into the same diff, so it's not used.
_FINALS_REWARD_BASE = {1: (10, 40), 2: (10, 60), 3: (10, 80)}
# BEATING HAPPY MEEK in the round-3 final replaces the round-3 reward outright
# (user-supplied chart: "All stats +10 / SP +80  OR  All stats +20 / SP +150 +
# 'Past My Limits' hint if the final race against max level Happy Meek is won").
# Its own story is served instead of the usual 'After the URA Finale Final'.
_FINALS_MEEK_REWARD = (20, 150)
_SKILL_PAST_MY_LIMITS = 210081        # 'Past My Limits' (text_data cat 47)
# NO separate story is known for the beat-Meek branch, so it plays the STANDARD
# 'After the URA Finale Final' and only the REWARD differs. Both ids that looked
# like candidates are already spoken for: 400001418 "Happy Meek's Challenge!" is
# the DUEL story (_DUEL_STORY_ID) and 400001422 "I'm Here to Challenge You" is
# her UNLOCK cutscene -- capture-confirmed as event 102005's story. Serving
# either would replay a scene the player has already had.
_FINALS_MEEK_STORY = None
_FINALS_MEEK_EVENT_ID = None
_DEFAULT_STAT_CAP = 1200  # default_max_*: gains above this halve (capture-verified
                          # at wiz 1204..1301: +4 -> +2, +14 -> +7)
# The trainee's post-race reflection stories (see _queue_post_race_choice).
_POST_RACE_SUFFIXES = (513, 514, 515)
POST_RACE_FIRED_KEY = "post_race_stories_fired"

# The "<stat> is in superb form" codes that pay-out owes, claimed by the first
# response that has a `data` to put them on -- see _apply_race_reward.
RACE_REWARD_NOT_UP_KEY = "pending_race_reward_not_up"
RACE_REWARD_KEY = "pending_race_reward"  # full_state: computed at race_end, applied
                                         # on the FIRST check_event after race_out
                                         # (the real server's observed timing --
                                         # chara_info is byte-identical across
                                         # race_end/race_out in every capture)


def _race_bonus_pct(chara_info: dict, full_state: dict = None) -> int:
    """The deck's summed Race Bonus % -- base (cards.json race_bonus,
    GameTora-sourced -- same channel _fan_gain_for_race uses for fan_bonus)
    PLUS each card's own EFFECT_RACE_BONUS (15) support_card_unique_effect
    bonus once its real level unlocks it (e.g. card 20003 +5% at level 25) --
    training_formula.py never named type 15 before 2026-08-19, so this used
    to silently miss the unique half entirely."""
    levels = _deck_support_card_levels(chara_info)
    total = 0
    for sid in _career_deck(chara_info):
        total += training_formula.card_effect(sid, "race_bonus", 0) or 0
        unique_lv = levels.get(sid)
        if unique_lv:
            total += training_formula.unique_effect(
                sid, training_formula.EFFECT_RACE_BONUS, level=unique_lv)
    # ...plus whatever the SCENARIO is adding right now. Trackblazer's Cleat
    # Hammers are a one-turn +20%/+35% race bonus, and the wire proves they ride
    # this same term: the item appears in item_effect_array as effect_type 14
    # with effect_value_1 6, which is the race-bonus target.
    #
    # full_state is optional because two callers genuinely do not have one --
    # the voluntary-race and goal-race event builders construct their reward
    # from chara_info alone. Those two miss the scenario share; every path that
    # settles a real race result passes it.
    if full_state is not None:
        total += scenarios.for_chara(chara_info).race_bonus_pct(full_state, chara_info)
    return total


def _race_grade_of(program_id) -> int | None:
    row = master_data.query_one(
        "SELECT r.grade FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE p.id=?", (program_id,))
    return row["grade"] if row else None


def _is_debut_race(program_id) -> bool:
    """Whether this program is the maiden/debut race (race.grade 900).

    The debut is a ROUTE GOAL like any other -- it has its own
    single_mode_route_race row -- but the real server does NOT give it the
    goal-race treatment on the way out. Three real captures, all at the debut
    (program 851, turn 12):

      152744  race_out -> 7005 'Victory!' (501068708), get_choice_reward, then
              check_event choice_number 2; vital 35 -> 10, i.e. -25 energy.
      144604  the same shape on its first race: 7006 'Solid Showing'
              (501068709), choice 1, vital 76 -> 51, -25 energy.

    That is the ORDINARY placement event -- two options, differing energy
    costs, the reward attached to the choice -- not the goal path's single
    acknowledge with a fixed all-five payout and no energy cost. Serving the
    goal path there handed the player free stats and cost them nothing, which
    is the '+4 to all stats' the debut should not be paying (user-reported).

    Goal BOOKKEEPING is untouched: the debut still counts as the route goal it
    is, still clears, and still cannot place-fail (its route row's cv1 is 0).
    Only the post-race event and its reward change.

    SCENARIO 4 ONLY -- see _debut_runs_voluntary. Both captures above are
    single_mode_free/* (Trackblazer), and the rule was wrongly generalised from
    them to every scenario."""
    return _race_grade_of(program_id) == _DEBUT_GRADE


def _debut_runs_voluntary(chara_info: dict, program_id) -> bool:
    """Whether THIS career's debut takes the ordinary placement path (two
    options, one of which costs energy) instead of the goal path.

    True only in Trackblazer, which has no career goals at all -- everywhere
    else the debut is goal #1 and pays like one: one acknowledge, the fixed
    all-five + SP, and no energy. User-reported: the debut was prompting for
    (and taking) energy in every scenario, and the Grand Live capture settles
    it -- turn 12's race_entry/race_end/race_out all carry vital 53, and so
    does turn 13. See scenarios/base.py's debut_is_goal_race."""
    if not _is_debut_race(program_id):
        return False
    return not scenarios.for_chara(chara_info).debut_is_goal_race


def _race_reward_for(chara_info: dict, program_id, finals_round, result_rank: int,
                     full_state: dict = None) -> dict:
    """{'stats': per-stat gain, 'sp': skill points} for this finish. Placement:
    1st-3rd pay the full base (kamigame: identical); 4th-5th and 6th+ are
    reduced -- the exact real scaling is undocumented, so: 4-5 halves the stat
    base and docks 5 SP base, 6+ collapses stats to base 1 and docks 10 SP base
    (the G1-6th capture point: stat 3 -> 1, SP 45 -> 35, reproduced exactly).

    TODO -- NEGATIVE SKILLS ARE NOT IMPLEMENTED. A bad finish can saddle the
    trainee with a debuff skill matching the race's own conditions (lose badly
    at Tokyo -> "Tokyo Racecourse ×" 200033, on dirt -> "Down in the Dirt ×"
    202303, in rain -> "Rainy Days ×" 200233). Nothing in this server ever
    grants one, so the whole downside of racing badly is currently missing --
    this function is where the finish is already known, so it is the natural
    place to wire it. User-reported 2026-09-05: "x variants are obtained by
    doing poorly in a race (i dont know the exact logic)."

    What master.mdb DOES settle (verified, not assumed):
      * The population is `skill_data.grade_value < 0`: 48 skills, ALL of them
        rarity 1 and disable_singlemode=1 (no career can buy one at the skill
        shop -- they are only ever granted). Do NOT identify them by the "×"
        in the name or by an id ending in 3: only 33 are named "×", and 42
        ids end in 3. The other 15 are plainly-named debuffs -- "Defeatist"
        200411, "Packphobia" 200401, "Reckless" 200421, "Running Idle" 200521,
        "Gatekept" 200433, "G1 Averseness" 200311, ...
      * They must go into skill_array DIRECTLY, never through a hint: 37 of
        the 48 collide with another skill on (group_id, rarity), which is the
        only key skill_tips_array has -- nothing on the wire could tell
        "Corner Adept ○" 200332 from "Corner Adept ×" 200333.
      * The rating penalty is free once granted: grade_value is -129/-174/-262
        (vs a white skill's +129) and rating_formula already sums grade_value.
      * skill_data.tag_id buckets them by trigger CONDITION -- 401 running
        style/season/handedness (13), 402 course + distance (21), 403 ground
        state (4), 404 weather, 405 course shape, 301/407 on the situational
        ones. That is the most likely basis for "which debuff fits this race",
        but no table maps a race RESULT to one.

    What is NOT known and must be captured before implementing: the trigger
    threshold (which placements, and whether aptitude or condition gates it),
    the per-race odds, and whether it lands at race end or via a following
    event. Do NOT guess these -- no event in the GameTora cache grants one
    (checked: 0 hits across all 810 files), so there is no second source to
    cross-check a guess against."""
    if finals_round:
        base_stat, base_sp = _FINALS_REWARD_BASE.get(finals_round, (10, 40))
    else:
        grade = _race_grade_of(program_id) if program_id else None
        base_stat, base_sp = _RACE_REWARD_BASE.get(grade, (3, 30))
    if result_rank >= 6:
        base_stat, base_sp = 1, max(10, base_sp - 10)
    elif result_rank >= 4:
        base_stat, base_sp = max(1, base_stat // 2), max(10, base_sp - 5)
    # integer math: floor(base * (1 + pct/100)) without float dust
    # (45 * 1.4 in floats is 62.999..., which int()s to the wrong 62)
    pct = int(_race_bonus_pct(chara_info, full_state))
    return {"stats": base_stat * (100 + pct) // 100,
            "sp": base_sp * (100 + pct) // 100}


def _apply_race_reward(chara_info: dict, reward: dict) -> list:
    """Apply a computed race reward to chara_info in place. Per-stat gain
    halves (floored) once that stat is over its default_max (1200) and always
    clamps to the hard max_*.

    Returns the not_up_parameter_info status codes it owes -- every stat it
    tried to raise that was already at its hard cap, i.e. "<stat> is in superb
    form". This pay-out writes chara_info directly instead of going through a
    choice, so event_engine.not_up_info never sees it and the post-race panel
    listed skill points alone with no explanation for the missing stats
    (user-reported 2026-09-07, screenshot of "After the URA Finale Qualifier"
    showing only "Skill Pts went up by 58")."""
    per_stat = reward.get("stats", 0)
    not_up = []
    for s in ("speed", "stamina", "power", "guts", "wiz"):
        cap_key = "max_wiz" if s == "wiz" else f"max_{s}"
        soft_key = "default_max_wiz" if s == "wiz" else f"default_max_{s}"
        soft = chara_info.get(soft_key) or _DEFAULT_STAT_CAP
        gain = per_stat // 2 if chara_info.get(s, 0) > soft else per_stat
        if gain > 0 and chara_info.get(s, 0) >= chara_info.get(cap_key, 9999):
            not_up.append(event_engine._STAT_KEY_IDX[s])
        chara_info[s] = min(chara_info.get(s, 0) + gain, chara_info.get(cap_key, 9999))
    chara_info["skill_point"] = chara_info.get("skill_point", 0) + reward.get("sp", 0)
    # Optional skill hint (only the beat-Happy-Meek finals reward carries one).
    hint = reward.get("skill_hint")
    if hint:
        event_engine._apply_skill_hint(chara_info, hint, 1)
    if not_up:
        log.info("race reward: already at ceiling -> not_up_parameter_info %s "
                 "(stats %s)", sorted(not_up),
                 [event_engine._STAT_IDX_KEY.get(c) for c in sorted(not_up)])
    return sorted(set(not_up))


def _queue_post_race_choice(response: dict, full_state: dict, career_state: dict) -> bool:
    """Queue the trainee's post-race reflection event -- 2 real choices from
    the engine (stats, sometimes energy or a condition).

    Capture-confirmed: right behind the 'After the Debut' story the real server
    played event 3031 / story 50<chara>514 'Bittersweet Sparkle'. The stories
    are suffix 513/514/515 and every trainee has all three.

    NOT suffix 516: that band is the FOOD event ('Putting It Away at the
    Cafeteria' / 'A Little Can't Hurt'), which fires on ordinary turns, not
    races. Pointing this hook at 516 is why most races still ended in silence
    (live-reported twice). Walks the three in order and stops when spent, so it
    never repeats within a career."""
    # DISABLED (live-corrected): 513/514/515 are NOT post-race stories -- for
    # McQueen they're 'Late-Night Fanservice Training' etc., outing/date
    # events, and serving them post-race ALSO made the deferred race reward
    # ride their result screen ('rewards from the completely wrong events').
    # The real post-race choice event's story band is still unidentified; until
    # a capture pins it, races end after their 'After the X' story only.
    return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    fired = set(full_state.get(POST_RACE_FIRED_KEY) or [])
    for suffix in _POST_RACE_SUFFIXES:
        sid = 500000000 + chara * 1000 + suffix
        if sid in fired:
            continue
        title = event_engine.event_title(sid)
        ev = event_engine.resolve(sid, title, card_id) if title else None
        if not ev or not ev.get("choices"):
            continue
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara,
            support_card_id=0, play_timing=3))
        _push_career_ctx(full_state, {
            "event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
            "source": "chara", "source_id": card_id, "title": title,
            "trainee_card_id": card_id})
        full_state[POST_RACE_FIRED_KEY] = sorted(fired | {sid})
        return True
    return False


def _append_race_history(career_data: dict, race_ctx: dict, chara_info: dict, result_rank: int) -> None:
    """Record this race in the persisted career's race_history (a TOP-LEVEL
    data field, NOT nested in chara_info) so it reflects the trainee's OWN
    actual results. Previously never touched at all -- every /load kept
    showing whatever the captured fixture's own frozen race_history was: a
    single 'turn 12, already won' entry from a completely different
    trainee's playthrough. Reported directly: seeing a race already marked
    as run in the career profile on a turn that hadn't happened yet for the
    live player. sync_chara_info's blanket key sync then carries this real,
    growing history into every subsequent response automatically (see
    _seed_dynamic_career, which seeds the key as [] so that sync can
    actually take effect)."""
    history = career_data.setdefault("race_history", [])
    rsi = race_ctx.get("race_start_info") or {}
    horses = rsi.get("race_horse_data") or []
    history.append({
        "turn": race_ctx.get("turn"),
        "program_id": race_ctx.get("program_id"),
        "weather": rsi.get("weather", 1),
        "ground_condition": rsi.get("ground_condition", 1),
        "running_style": race_ctx.get("running_style")
                         or chara_info.get("race_running_style", 2),
        "result_rank": result_rank,
        "frame_order": (horses[0].get("frame_order", 1) if horses else 1),
        "npc_count": max(0, len(horses) - 1),
        # The two facts secret-event conditions need and nothing else recorded:
        # where the trainee was seeded in the betting (`pop`) and where the
        # scripted rivals finished (`beat_rival` / `lose_to_rival` /
        # `rival_draw` / `rn_race_w`). Both stay None on the static-fixture
        # fallback path, where they are genuinely unknown.
        "popularity": race_ctx.get("player_popularity"),
        "rival_ranks": race_ctx.get("rival_finish_orders"),
        # How the race was RUN, from the simulation (None on the static-fixture
        # fallback, where it is genuinely unknown): how many times she went
        # Rushed, and how many distinct skills fired. Epithets 8/266 and 65.
        "rushed_count": race_ctx.get("player_rushed_count"),
        "skills_used": race_ctx.get("player_skills_used"),
        "overtakes": race_ctx.get("player_overtakes"),
        # The trainee's fan total AFTER this race. Recorded because the only
        # per-YEAR fan question anything asks ("obtain at least 100,000 fans in
        # classic year", epithet 223) cannot be answered from the final total --
        # fans only ever climb, so the high-water mark inside a year is the last
        # race of that year, and nothing else on the career is dated.
        "fans_after": int((chara_info or {}).get("fans") or 0),
    })


# The race_history fields that go ON THE WIRE, and the whole list of them:
# frame_order, ground_condition, npc_count, program_id, result_rank,
# running_style, turn, weather. Confirmed against every real career record in
# the three captures/bot/20260905_*_icarus sessions -- no record carries a
# ninth key.
#
# _append_race_history writes SIX more (popularity, rival_ranks, rushed_count,
# skills_used, overtakes, fans_after). Those are facts the epithet evaluator
# and the secret-event conditions read back later; they are career bookkeeping,
# not wire fields, and they were going out to the client because race_history
# is served straight from the persisted career. Same rule the note on
# FAN_PROMISES_DONE_KEY below states for chara_info, and the same fix
# live_result_array already got: project on the way out, keep the facts in
# state. They must stay in the PERSISTED copy -- epithets.build_facts and
# _rival_ranks read them at career finish.
_WIRE_RACE_HISTORY_FIELDS = (
    "turn", "program_id", "weather", "ground_condition", "running_style",
    "result_rank", "frame_order", "npc_count")


def _wire_race_history(history) -> list:
    if not isinstance(history, list):
        return history
    return [{k: e[k] for k in _WIRE_RACE_HISTORY_FIELDS if k in e}
            if isinstance(e, dict) else e
            for e in history]


# A career's fulfilled Fan Promises (condition ids). Career-scoped, not on
# chara_info: chara_info goes out on the wire and must keep the shape the
# client's own struct declares.
FAN_PROMISES_DONE_KEY = "fan_promises_fulfilled"


def _fulfil_fan_promise(career_data: dict, chara_info: dict, program_id) -> int | None:
    """A win at the promised racetrack FULFILS the promise: the condition
    clears and the career records it.

    master.mdb states the rule in the condition's own effect text -- 'Win a
    race at Kyoto or Hanshin to fulfill her promise' -- so the racetrack sets
    are read, not guessed (conditions.PROMISE_TRACKS). Fulfilling is what
    unlocks Smart Falcon's follow-up 'Connecting ☆ Inspiration'
    (secret condition `win_connect_live`).

    Deliberately evaluated at RACE time and only forwards: a Kyoto win from
    before the promise was made does not retroactively fulfil it."""
    track = (_race_facts_for_program(program_id) or {}).get("track_id")
    cid = conditions.promise_fulfilled_by(chara_info, track)
    if cid is None:
        return None
    conditions.remove(chara_info, cid)
    done = career_data.setdefault(FAN_PROMISES_DONE_KEY, [])
    if cid not in done:
        done.append(cid)
    log.info("fan promise %s fulfilled by a win at racetrack %s", cid, track)
    return cid


def _career_chara_grade(fans: int, runs: int, wins: int) -> int:
    """The trainee's current class from the REAL single_mode_chara_grade table
    -- the highest grade whose win_num/run_num/need_fan_count requirements are
    ALL met (grade 1 = never raced, 2 = raced but maiden not won, 3 = maiden
    won, 4+ = win + rising fan thresholds). Validated against the real-server
    capture: pre-debut (0 runs, 1 fan) -> 1; debut won (1 win, 1384 fans) ->
    3; second win at 15216 fans -> 4. NOT the same as trained_chara's
    _chara_grade_for, which prices FINISHED careers by fans alone and calls
    anything under 5000 fans a 3 -- wrong for an unraced in-career trainee."""
    grade = 1
    for r in master_data.query(
            "SELECT id, win_num, run_num, need_fan_count FROM single_mode_chara_grade ORDER BY id"):
        if wins >= r["win_num"] and runs >= r["run_num"] and fans >= r["need_fan_count"]:
            grade = r["id"]
    return grade


def handle_ura_race_end(payload: dict) -> dict:
    """Real captured race_end (ura_data_set, rewards) with chara_info set to the
    player's run: post-race state (playing_state 4), fans from the REAL
    per-race/per-placement fan table (_fan_gain_for_race) plus the trainee's
    support-deck fan bonus. result_rank reflects the REAL simulated finish
    (player_finish_order, stashed by _simulate_career_race at entry time) when
    available -- previously always hardcoded to a 1000-fan 1st-place win
    regardless of the (also-hardcoded) race shown. Falls back to reporting a
    win + flat fan gain when no simulation/program ran (static fixture
    replay). Turn advance happens in race_out.

    Also promotes chara_grade (the trainee's CLASS) from the real
    single_mode_chara_grade rule -- the real server does this in the same
    race_end response (capture: grade 1->3 on the debut win, 3->4 once past
    5000 fans). Never updated before, which left every trainee frozen in
    class 1 (Maiden) forever -- and since every non-maiden race requires a
    higher class, a trainee who'd won their maiden had NOTHING left they
    were eligible for: the likely true root cause of the Races screen's
    permanent "There are no races to compete in".

    Idempotent per race: a repeated race_end for the same RACE_CTX_KEY (client
    retry / re-entering the result screen) re-serves the same outcome without
    re-crediting fans or duplicating the race_history entry -- an earlier
    career save showed the debut recorded twice from exactly this."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    race_ctx = full_state.get(RACE_CTX_KEY) or {}

    if not career:
        return _no_career_refusal(viewer_id, "single_mode/race_end", full_state)

    response = _load_ura_race_seed("race_end")
    data = response["data"]
    finish_order = race_ctx.get("player_finish_order")
    result_rank = finish_order if finish_order else 1
    won = result_rank == 1
    gained_fans = 1000

    if career:
        chara_info = copy.deepcopy(career["data"]["chara_info"])
        gained_fans = _fan_gain_for_race(chara_info, race_ctx.get("program_id"),
                                         result_rank, full_state=full_state)
        already_committed = race_ctx.get("end_committed", False)
        if not already_committed:
            chara_info["fans"] = chara_info.get("fans", 0) + gained_fans
            _append_race_history(career["data"], race_ctx, chara_info, result_rank)
            if won:
                _fulfil_fan_promise(career["data"], chara_info,
                                    race_ctx.get("program_id"))
            # THE SCENARIO'S OWN PAY-OUT. Inside the end_committed guard, so a
            # client retry re-serves the same outcome without banking twice --
            # the same reason the fan credit and the history append live here.
            # No-op on base.Scenario; Trackblazer's Grade Points, shop coins,
            # Climax standings and post-race shop offer all land through it.
            try:
                scenarios.for_chara(chara_info).on_race_result(
                    full_state, chara_info, race_ctx, result_rank)
            except Exception:
                log.exception("scenario race pay-out failed; race still committed")
            # NOTE: the stat/SP reward is NOT computed here any more -- the
            # POST-RACE EVENT pays it (_post_race_choices: user-supplied
            # per-grade random stat + SP, scaled by race bonus). The old flat
            # all-five payout was our own reading of the captures; keeping both
            # double-paid (measured: +3 to all five AND +10 to one), and being
            # deferred it kept landing on unrelated later events (#17).
        history = career["data"].get("race_history") or []
        chara_info["chara_grade"] = _career_chara_grade(
            chara_info.get("fans", 0), len(history),
            sum(1 for h in history if h.get("result_rank") == 1))
        chara_info["playing_state"] = 4  # race-result state
        if won and race_ctx.get("finals_round") == 3:
            # Real capture: the turn-78 finals-win race_end response already
            # carries chara_info.state=3 = career COMPLETED.
            chara_info["state"] = 3
        data["chara_info"] = chara_info
        career["data"]["chara_info"] = chara_info
        race_ctx["won"] = won
        if not already_committed:
            race_ctx["gained_fans_committed"] = gained_fans
        race_ctx["end_committed"] = True
        full_state[RACE_CTX_KEY] = race_ctx
        state_store.save_state(viewer_id, full_state)

    # The item reward card is COMPUTED from the race actually run (see
    # _build_race_reward_info) -- it used to be whatever the captured fixture
    # happened to contain, patched only for the fan count and the finish place,
    # so every race in every career showed one specific captured race's drops.
    # Rolled once and cached on the race context so a retried race_end (the
    # same end_committed guard the fan/history credit uses) re-serves the same
    # card instead of re-rolling a different one.
    reward_info = race_ctx.get("reward_info")
    if not isinstance(reward_info, dict):
        reward_info = _build_race_reward_info(
            race_ctx.get("program_id"), result_rank, gained_fans,
            (career["data"].get("race_history") if career else None))
        race_ctx["reward_info"] = reward_info
        if career:
            full_state[RACE_CTX_KEY] = race_ctx
            state_store.save_state(viewer_id, full_state)
    reward_info = copy.deepcopy(reward_info)
    reward_info["gained_fans"] = gained_fans
    reward_info["result_rank"] = result_rank
    # result_time is the race's own clock; the fixture's belongs to the fixture.
    reward_info["result_time"] = int(race_ctx.get("player_finish_time") or 0)
    data["race_reward_info"] = reward_info
    # Real captures always show this empty in career mode (it is the live-ops
    # "additional race reward" campaign list), never the fixture's content.
    data["race_add_reward_info"] = []
    if isinstance(data.get("reward_summary_info"), dict):
        data["reward_summary_info"]["add_total_fan"] = gained_fans
        # The summary mirrors the card: same items, {item_id, number} shaped.
        data["reward_summary_info"]["add_item_list"] = [
            {"item_id": it["item_id"], "number": it["item_num"]}
            for it in reward_info["race_reward"] + reward_info["race_reward_bonus"]]
    return response


def handle_ura_race_out(payload: dict) -> dict:
    """Real captured race_out with chara_info set to the player's run. Our PS
    doesn't run the real post-race check_event/gain_skills/load chain that would
    normally advance the turn, so we advance it here and refresh training
    previews so the run drops straight back into the next turn.

    Also fires the trainee's real post-race 'After the X' story (Goal Complete)
    for whichever race race_entry resolved -- previously this was unconditionally
    cleared (data["unchecked_event_array"] = []), so it never played at all,
    regardless of the trainee or which race just ran.

    URA FINALS rounds (see _FINALS_EVENTS) get their captured scenario
    post-race events instead of a per-chara 'After the X' story; the FINAL
    round (turn 78) additionally queues the real captured career-ending
    chain (scenario epilogue -> URA wrap-up -> the trainee's own ending
    story) and does NOT advance the turn -- the real server keeps turn 78
    through the whole ending (the client then drives gain_skills -> load ->
    finish, all already handled, and finish at turn >= _CAREER_END_TURN
    builds the legacy)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    race_ctx = full_state.get(RACE_CTX_KEY) or {}

    if not career:
        return _no_career_refusal(viewer_id, "single_mode/race_out", full_state)

    response = _load_ura_race_seed("race_out")
    data = response["data"]
    data["unchecked_event_array"] = []

    if career:
        chara_info = copy.deepcopy(career["data"]["chara_info"])
        finals_round = race_ctx.get("finals_round")
        # URA FINALS LOSS (user-reported: "losing any race of the URA Finale
        # should end my run, but I still continue"). This whole finals_round
        # branch was written win-only -- it queues the round's reward and (on
        # round 3) the full success ending chain UNCONDITIONALLY, never
        # consulting race_ctx["won"] the way the non-finals branch below does
        # via won_flag/placement_failed. Every finals race is mandatory: a
        # loss at any of the three rounds ends the career on the spot, same
        # sequence as a missed goal-race placement (fail-reaction story, then
        # the career-over event) -- no round reward, no advancing to the next
        # round or the ending chain.
        if finals_round and not race_ctx.get("won", True):
            chara_info["state"] = 2
            chara = (chara_info.get("card_id") or 0) // 100
            if chara and master_data.query_one(
                    "SELECT story_id FROM single_mode_story_data WHERE story_id=?",
                    (500000000 + chara * 1000 + 701,)):
                _queue_turn_event(response, full_state, _scenario_event_entry(
                    7011, 500000000 + chara * 1000 + 701, play_timing=6,
                    choices=[_ack_choice()], chara_id=chara))
            _queue_turn_event(response, full_state, _career_fail_event())
            log.info("URA finals round %s LOST -- career failed", finals_round)
            data["chara_info"] = chara_info
            career["data"]["chara_info"] = chara_info
            full_state[RACE_CTX_KEY] = race_ctx
            # This early return used to skip _remember_display, so quitting on
            # the finals-loss reaction dropped the career-fail chain it had just
            # queued -- the one chain a player cannot get back by any other
            # route. Every other race_out exit remembers what it served.
            _remember_display(full_state, response)
            state_store.save_state(viewer_id, full_state)
            return response
        if finals_round:
            beat_meek = bool(race_ctx.get("beat_happy_meek")) and finals_round == 3
            # The beat-Meek branch changes the REWARD only -- no distinct story
            # for it is known (see _FINALS_MEEK_STORY).
            data["unchecked_event_array"] = [
                _finals_event_entry(finals_round, "post", chara_info)]
            # URA FINALS REWARD (live-reported missing): +10 to ALL FIVE stats
            # and SP 40/60/80 by round, scaled by the deck's race bonus --
            # _FINALS_REWARD_BASE, which nothing was reaching. The only caller
            # that handled finals_round was _post_race_choices, and that is
            # invoked exclusively from the non-finals `else` below, so the
            # finals branch fell through paying nothing at all.
            #
            # Paid via RACE_REWARD_KEY (applied on the first check_event after
            # race_out) rather than the ctx queue: these are SCENARIO events,
            # which resolve down the scenario/pending path and never consult the
            # career ctx, so a ctx pushed here would sit unclaimed forever.
            try:
                reward = _race_reward_for(
                    chara_info, race_ctx.get("program_id"), finals_round,
                    int(race_ctx.get("player_finish_order") or 1),
                    full_state=full_state)
                if beat_meek:
                    # REPLACES the round-3 reward (not added to it), and throws
                    # in the 'Past My Limits' hint.
                    pct = int(_race_bonus_pct(chara_info, full_state))
                    stats, sp = _FINALS_MEEK_REWARD
                    reward = {"stats": stats * (100 + pct) // 100,
                              "sp": sp * (100 + pct) // 100,
                              "skill_hint": _SKILL_PAST_MY_LIMITS}
                    log.info("URA final: beat Happy Meek -> %s", reward)
                full_state[RACE_REWARD_KEY] = reward
            except Exception:
                log.exception("finals reward failed; skipping")
            if finals_round == 3 and scenarios.for_chara(chara_info).has_ending_chain:
                # Queue the ending chain behind the post-race event -- drained
                # one per check_event by the established pending-events path.
                #
                # ...unless the scenario HAS AN ENDING OF ITS OWN. Trackblazer
                # does: its capture runs 203106 (the third Climax leg's
                # post-race beat) -> 203202 -> gain_skills/factor_select/finish,
                # with none of URA's wrap-up in between. See
                # Scenario.has_ending_chain, and note it is a strictly wider gate
                # than has_director_ending right below.
                #
                # _ENDING_CHAIN[1] (event 4, 400001412 "A Super Successful
                # Event!") is the DIRECTOR's own bond-scaled sendoff -- URA
                # exclusive (user-supplied 2026-08-16: "the event afterwards
                # is just the director version" -- it played in Grand Live and
                # shouldn't have). _ENDING_CHAIN[0] (400000091 "Twinkle
                # Monthly Special Issue", the Reporter's) is confirmed fine in
                # both scenarios and stays unconditional.
                scen = scenarios.for_chara(chara_info)
                has_director = scen.has_director_ending
                chain = [_scenario_event_entry(eid, sid, play_timing=3,
                                               choices=[_ack_choice(sel, item)])
                         for eid, sid, sel, item in _ENDING_CHAIN
                         if has_director or eid != _SUPER_SUCCESS_EVENT_ID]
                # END-OF-RUN PAYOUTS (#40-#43): the reporter's and Director's
                # bond-scaled sendoffs, then the trainee's own final event
                # (+5 all stats, +20 SP -- user-supplied). Each pays when it
                # RESOLVES, via ENDING_PAYOUT_KEY. _ending_payout_events itself
                # excludes the Director's tier for Grand Live (see there).
                payouts = {}
                for entry, stats, sp in _ending_payout_events(full_state, chara_info):
                    chain.append(entry)
                    payouts[str(entry["event_id"])] = {"stats": stats, "sp": sp}
                if has_director:
                    # 'A Super Successful Event!' is already IN _ENDING_CHAIN but
                    # had no payout entry, so it paid nothing. Director gauge
                    # maxed -> +15 all / +50 SP, else +10 all and no SP
                    # (user-supplied).
                    d_bond = _bond_of(chara_info, _DIRECTOR_TARGET)
                    s_stats, s_sp = (_SUPER_SUCCESS_MAX
                                     if d_bond >= _DIRECTOR_MAX_BOND
                                     else _SUPER_SUCCESS_BASE)
                    payouts[str(_SUPER_SUCCESS_EVENT_ID)] = {"stats": s_stats, "sp": s_sp}
                full_state[ENDING_PAYOUT_KEY] = payouts
                full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, []).extend(chain)
        else:
            # POST-RACE EVENT + REWARD. Which one depends entirely on whether
            # this was a GOAL race (see _is_goal_race):
            #   goal      -> the trainee's 'After the X' story, ONE acknowledge,
            #                the fixed all-five reward, no energy cost.
            #   voluntary -> the placement story ('Victory!' / 'Solid Showing' /
            #                'Defeat') with the 2-option table, or the
            #                Reporter's coverage 20% of the time.
            # Voluntary races previously keyed off `race_story_name`, which only
            # goal races have, so they fired NO event -- and since #17 bound the
            # reward to the event, they also paid NOTHING (live-reported).
            story_name = race_ctx.get("race_story_name")
            won_flag = race_ctx.get("won", True)
            rank = race_ctx.get("player_finish_order") or 1
            is_goal = race_ctx.get("is_goal")
            if is_goal is None:      # ctx from before this field existed
                is_goal = _is_goal_race(chara_info, race_ctx.get("program_id"),
                                        race_ctx.get("turn"),
                                        career["data"].get("race_history") or [])
            # GOAL PLACEMENT FAILURE, checked BEFORE deciding whether to pay
            # the goal-complete reward -- NOT after (moved from further below,
            # 2026-08-24; live-reported: a goal race finished worse than its
            # required placement still played the goal-COMPLETE celebration
            # and paid its reward, with the failure event only chained in
            # behind it once the old, later-placed check caught up). cv1==0
            # rows (the debut) never place-fail -- the maiden-retry system
            # owns that path instead.
            route_ids_now = tuple(chara_info.get("route_race_id_array") or ())
            placement_failed = is_goal and any(
                t == race_ctx.get("turn") and cv1 and rank > cv1
                for t, _cid, _kind, cv1 in _route_goal_rows(route_ids_now))
            try:
                if placement_failed:
                    # Mirrors _maybe_fail_goal's own sequence exactly: the
                    # trainee's fail-reaction story first, THEN the career-over
                    # event -- never the goal-complete celebration this race
                    # just missed.
                    chara_info["state"] = 2
                    _purge_queued_events(response, full_state)
                    _queue_fail_reaction(response, full_state, chara_info)
                    _queue_turn_event(response, full_state, _career_fail_event(),
                                      fail_chain=True)
                    log.info("goal race at turn %s lost (rank %s) -- career failed, "
                             "skipped goal-complete celebration/reward",
                             race_ctx.get("turn"), rank)
                elif is_goal and not _debut_runs_voluntary(chara_info,
                                                             race_ctx.get("program_id")):
                    ev = _goal_race_reward(chara_info, race_ctx.get("program_id"),
                                           race_ctx.get("finals_round"), rank)
                    entry = (_post_race_event_entry(chara_info, story_name,
                                                    won=won_flag, choices=1)
                             if story_name else None)
                    title = story_name
                    if entry:
                        data["unchecked_event_array"] = [entry]
                        _push_career_ctx(full_state, {
                            "event_id": entry["event_id"],
                            "story_id": entry["story_id"],
                            "source": "inline", "source_id": 0, "title": title,
                            "trainee_card_id": chara_info.get("card_id"),
                            "event": ev})
                    else:
                        # No story for this race (live-reported for Curren Chan's
                        # Aoi Stakes, #16) -- pay the reward straight away rather
                        # than silently dropping it.
                        event_engine.apply_choice(chara_info, ev, 1, full_state=full_state)
                else:
                    entry, ev, title = _voluntary_race_event(
                        full_state, chara_info, race_ctx.get("program_id"), rank)
                    if entry:
                        data["unchecked_event_array"] = [entry]
                        _push_career_ctx(full_state, {
                            "event_id": entry["event_id"],
                            "story_id": entry["story_id"],
                            "source": "inline", "source_id": 0, "title": title,
                            "trainee_card_id": chara_info.get("card_id"),
                            "event": ev})
                    else:
                        event_engine.apply_choice(chara_info, ev, 1, full_state=full_state)
            except Exception:
                log.exception("post-race reward event failed; skipping")
            # EPITHETS COMPLETED BY THIS RACE (Trackblazer only), chained
            # directly behind the post-race story and ahead of everything else
            # the turn produces -- user-reported: they "trigger as soon as you
            # complete the ephitets (right after race but before consequtive
            # race mood down event)". One event per epithet; see
            # _maybe_fire_epithet_events.
            try:
                _maybe_fire_epithet_events(full_state, career, response)
            except Exception:
                log.exception("epithet announcement failed; skipping")
            # A SECRET event whose condition THIS RACE just satisfied is due
            # now, chained behind the post-race story.
            #
            # The capture (`docs/tachyon switch`) has the real server serving
            # the Satsuki Sho's post-race story here on race_out (event 11426 /
            # 501032305) and Agnes Tachyon's branch event on the FIRST
            # check_event afterwards (11440 / 501032114) -- one step later, but
            # still inside the same post-race chain, before the player acts.
            # Queuing it here puts it in that chain one step early, which is
            # what the user asked for ("make it so that the event plays after
            # the Satsuki Sho"); the alternative, leaving it to the coincidence
            # roll on the next command, showed it a whole TURN later.
            try:
                _maybe_fire_secret_event(full_state, career, response)
            except Exception:
                log.exception("post-race secret event failed; skipping")
            # RACE FATIGUE, last in the race turn's chain -- which is where the
            # corpus has it, after the placement story (7005/7006/7007) and
            # after anything else this race triggered. Only the non-finals
            # branch: the finale's three races fall on turns 74/76/78, so no
            # streak can reach the minimum of 3 across them anyway, and the
            # rounds carry their own scripted cutscenes rather than the
            # trainee's own stories.
            try:
                _maybe_fire_race_fatigue(full_state, career, response, race_ctx)
            except Exception:
                log.exception("race fatigue roll failed; skipping")
            # REPORTER UNLOCK: Otonashi (target 103) joins the turn after the
            # debut race is WON (capture: debut turn 12 -> event 1016 in turn
            # 13's chain -> is_appear from 14). Keyed to the actual won maiden
            # race, not a fixed turn, so a delayed/retried debut still unlocks
            # her correctly. Queued via PENDING (unlock events jump the drain
            # queue), registered immediately so she can be placed next turn.
            try:
                unlocked = [list(x) for x in full_state.get(
                    single_mode_events.UNLOCKED_NPCS_KEY, [])]
                done = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
                if (race_ctx.get("won", True) and [103, 9003] not in unlocked
                        and 1016 not in done
                        and _race_grade_of(race_ctx.get("program_id")) == _DEBUT_GRADE):
                    # Queue ONLY -- registration (and the visible 'will now
                    # appear' flip) happens when 1016 RESOLVES, like every
                    # other unlock cutscene.
                    pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
                    if 1016 not in pending:
                        pending.append(1016)
                    full_state[single_mode_events.PENDING_EVENTS_KEY] = pending
            except Exception:
                log.exception("reporter unlock failed; skipping")
        if finals_round != 3:
            chara_info["turn"] = chara_info.get("turn", 1) + 1
        chara_info["playing_state"] = 1  # back to normal training
        # A race can be the action that advances INTO an inspiration turn
        # (e.g. a turn-30 goal race -> turn 31); chains behind the post-race
        # story. career["data"]["chara_info"] isn't reassigned yet, so point
        # the roll at the updated copy first.
        career["data"]["chara_info"] = chara_info
        try:
            _maybe_queue_inspiration(response, full_state, career, viewer_id,
                                     chara_info.get("turn"))
        except Exception:
            log.exception("inspiration roll failed post-race; skipping")
        # The new turn's own deadline evaluation (fan-threshold / grade-tally
        # passive goals). Goal PLACEMENT failure for the race that JUST ran is
        # now checked earlier, before the reward/celebration is even built
        # (see `placement_failed` above) -- checking it again here would only
        # re-detect the exact same loss and queue a second, duplicate
        # career-fail event behind the one already queued.
        try:
            if chara_info.get("state") != 2:
                _maybe_fail_goal(response, full_state, career, chara_info.get("turn"))
                _maybe_queue_chara_story(response, full_state, career, chara_info.get("turn"))
            # This race may have just cleared its goal -- stamp the banner with
            # the real cleared-count and THIS goal's sort_id.
            # ONLY the race goal this race actually is. A voluntary race, or a
            # race that merely happens to fall on a passive goal's deadline
            # turn, must NOT claim the banner -- that is what put the 3000-fan
            # clear on the 'Victory!' event and left its own Goal Achieved
            # event unplayed (live-reported). sort_id None => nothing to stamp.
            sort_id = next((s for t, s, ct, cid, _c1, _c2
                            in _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
                            if ct == 1 and cid == race_ctx.get("program_id")), None)
            if sort_id is not None:
                _mark_goal_progress(response, chara_info,
                                    career["data"].get("race_history", []), sort_id,
                                    full_state)
            # A PASSIVE goal due on the turn this race was run on -- the fan
            # threshold this race's fans just crossed, or the grade tally this
            # race just completed. Same call exec_command makes; a race turn
            # never reaches that one (see _announce_due_passive_goals).
            if chara_info.get("state") != 2:
                _announce_due_passive_goals(response, full_state, career,
                                            race_ctx.get("turn"))
        except Exception:
            log.exception("goal evaluation failed post-race; not failing the career")
        # THE TURN'S OWN SCENARIO SCHEDULE, which a RACE turn used to skip
        # entirely. _poll_career_events runs in exec_command -- and a turn the
        # player RACES never calls exec_command, it comes through here instead.
        # So every producer-driven beat scheduled for that turn was silently
        # lost, and the Unity Cup's team-race turns (24/36/48/60/72) are exactly
        # the turns a career is most likely to be racing on: 48 is Classic Late
        # December, the Arima Kinen slot.
        #
        # Live save, viewer 802445340143 (Unity Cup, turn 56): race_history has
        # a race on turn 48, and round 3's gate never fired there -- the round
        # only came back when unity_cup_missed_race re-offered it on a later
        # TRAINING turn, which is the "3rd Unity Cup event delayed to the end of
        # turn 50" the player saw. Round 2 (turn 36, also raced) went the same
        # way.
        #
        # The hold comes first, for the same reason it does in exec_command: the
        # set-piece owns the display slot and the served turn must stop
        # advancing, or the client draws the next turn's home screen and walks
        # past the race. The poll only CLAIMS that slot if the post-race story
        # above did not already take it -- otherwise the beat queues and drains
        # behind it, which is the right order (race result, then the Unity Cup).
        played_turn = race_ctx.get("turn")
        try:
            if played_turn is not None and _scenario_interstitial_pending(
                    full_state, career, payload, turn=played_turn):
                scenarios.for_chara(chara_info).begin_hold(full_state, int(played_turn))
        except Exception:
            log.exception("scenario turn-hold failed post-race; skipping")
        try:
            _poll_career_events(response, full_state, career, payload,
                                turn=played_turn)
        except Exception:
            log.exception("career event poll failed post-race; skipping")

        # RACE STAT/SP REWARD (computed at race_end): if any event is queued to
        # play, hold it for that event's check_event -- the real server's
        # observed timing, which lets the client animate the gains on the
        # result screen. With nothing queued (a voluntary race with no story
        # and no choice roll) there IS no later check_event, so apply now --
        # before the training previews rebuild, so they see the new stats.
        reward = race_ctx.get("pending_reward")
        if reward:
            if data.get("unchecked_event_array"):
                full_state[RACE_REWARD_KEY] = reward
            else:
                _apply_race_reward(chara_info, reward)
        career_home = career["data"].get("home_info")
        if isinstance(career_home, dict):
            unlocked = [n[0] for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
            _refresh_command_info(chara_info, career_home, turn=chara_info["turn"], unlocked_npcs=unlocked,
                                  facility_levels=_facility_levels(career["data"]),
                                  race_history=career["data"].get("race_history", []),
                                  training_bonus=_training_bonus(full_state, chara_info),
                              friendship_bonus=_friendship_bonus(full_state, chara_info),
                              specialty_bonus=_specialty_bonus(full_state, chara_info),
                              support_card_levels=_support_card_levels(full_state),
                              friendship_stacks=_friendship_stacks(full_state),
                              full_state=full_state)
            data["home_info"] = copy.deepcopy(career_home)
        data["chara_info"] = chara_info
        career["data"]["chara_info"] = chara_info
        full_state.pop(RACE_CTX_KEY, None)

    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


_TRAINING_COMMAND_IDS_SET = {101, 105, 102, 103, 106}
# Firing model (per the game's "coincidence" feel, user-tuned):
_SUPPORT_EVENT_CHANCE = 0.75   # a support event fires this training turn
_SUPPORT_FROM_DECK = 0.60      # of those, from an EQUIPPED card (else any card).
                               # Was briefly 0.90 chasing "no events from MY
                               # deck" -- the true culprit was FIRED_EVENTS_KEY
                               # never clearing between careers; with that fixed
                               # the user asked for 60/40 back.
_SUPPORT_CHAIN_VS_RANDOM = 0.80  # of those, a chain event (else 0.20 a random event) --
                               # was 0.70; corrected from a 34k-event bot-log
                               # corpus measurement (URA/Unity Cup/Trackblazer),
                               # which put the real split at ~79/21.
# MEASURED (bot_logs, Unity Cup + Grand Live, which still have commands on
# turn 72): support events (10002 chain / 20000 random) fire on about 25-30% of
# command turns through turn 71, and on 0 of 62 command turns at turn 72. So
# the last turn they can fire is 71, and both checks are "turn > 71".
_CHAIN_STOP_TURN = 71          # support CHAINS pause after this turn (and during
                               # summer camp) -- user rule
_SUPPORT_STOP_TURN = 71        # ...and past this turn NO support-card event of
                               # any kind fires -- chain or random (user rule,
                               # 2026-07-28). Stricter than _CHAIN_STOP_TURN,
                               # which only pauses CHAINS and still lets random
                               # support events through (that is what summer
                               # camp wants). The run's last turns are the
                               # finals; the deck's material is done by then.


def _career_deck(chara_info: dict) -> list:
    """Equipped support-card ids (the 6-slot deck)."""
    return [c.get("support_card_id") for c in chara_info.get("support_card_array") or []
            if c.get("support_card_id")]


def _deck_support_card_levels(chara_info: dict) -> dict:
    """{support_card_id: real level} sourced from chara_info's OWN support_
    card_array (each entry already carries its exp from deck-assembly time --
    same field trained_chara.py's support_card_list snapshot reads), not
    full_state's owned collection. Lets race-reward/placement code resolve a
    card's real support_card_unique_effect gate (EFFECT_RACE_BONUS/
    EFFECT_SPECIALTY_PRIORITY) from chara_info alone, without threading
    full_state through the whole _post_race_amounts/_race_reward_for/
    _roll_distribution call chain just for this."""
    return {c["support_card_id"]: training_formula.support_card_level_from_exp(
                c["support_card_id"], c.get("exp", 0))
           for c in chara_info.get("support_card_array") or []
           if c.get("support_card_id")}


def _pick_support_event(pool: list, fired: set, want_random: bool,
                        full_state: dict | None = None):
    """Find one fireable event of the requested kind across a shuffled card pool.
    Chain -> a card's next un-fired event in order; random -> any un-fired random
    event (each fires at most once). Returns (card, story_id, title, event,
    is_random) or None.

    A card whose chain was DEAD-ENDED this run (a `chain_end` outcome -- the
    client's "End this Support Card's chain event") is skipped for chain
    events and left alone for random ones, which is what that effect means."""
    cards = list(pool)
    random.shuffle(cards)
    for card in cards:
        chain, rand = event_engine.support_events(card)
        if want_random:
            avail = [(sid, t, ev) for sid, t, ev in rand if sid not in fired]
            if avail:
                sid, t, ev = random.choice(avail)
                return card, sid, t, ev, True
        elif not event_engine.chain_ended(full_state, f"support:{int(card)}"):
            for sid, t, ev in chain:
                if sid not in fired:
                    return card, sid, t, ev, False
    return None


def _maybe_fire_support_event(full_state: dict, career_state: dict, response: dict,
                              force: bool = False) -> bool:
    """Fire a support-card event with the tuned probability model: 75% a support
    event at all; of those 60% from the deck / 40% any card in the catalog; of
    those 70% a chain event / 30% a random one. Served from the engine, no
    capture. Chain events use event id 10002 (story 8<card>nnn); random events use
    20000 (story 80<chara>nnn). Chains behind anything already queued this turn
    (see _queue_turn_event) rather than requiring an empty slot. Returns whether
    it fired, so a caller trying outing-then-support-as-fallback knows whether
    the fallback is still needed. force=True skips the rate roll (but NOT the
    turn cutoff below, which is absolute)."""
    chara_info = career_state["data"]["chara_info"]
    # HARD STOP: no support-card event of any kind past turn 72 (user rule).
    # Checked before the rate roll so it can't be bypassed by force=True and so
    # it costs no randomness.
    turn_now = chara_info.get("turn")
    if turn_now is not None and turn_now > _SUPPORT_STOP_TURN:
        return False
    if not force and random.random() >= _SUPPORT_EVENT_CHANCE:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    # CHAIN events additionally pause during summer camp (user rule) -- random
    # support events still fire there. Past _SUPPORT_STOP_TURN neither does, but
    # that already returned above.
    chains_allowed = not (_is_camp_turn(turn_now)
                          or (turn_now is not None and turn_now > _CHAIN_STOP_TURN))

    # Prefer the deck 60% of the time, else go straight to the whole catalog. When
    # the deck is chosen but exhausted (all its events already fired -- inevitable
    # late in a run), fall back to the catalog so the fire rate holds. The catalog
    # already contains the deck cards, so it never needs a fallback.
    if random.random() < _SUPPORT_FROM_DECK:
        pools = [_career_deck(chara_info), event_engine.all_support_card_ids()]
    else:
        pools = [event_engine.all_support_card_ids()]
    # GRAND LIVE "Support Event Rate Up" (live_bonus_type 2, サポート連続イベント率)
    # raises the CHAIN share specifically -- the name is the chain-event rate, and
    # chains are the ones worth having. Percentage points on the 70/30 split, so
    # +10 makes it 80/20. Zero in every other scenario, and zero until a song
    # carrying the bonus has activated.
    chain_share = _SUPPORT_CHAIN_VS_RANDOM
    chain_share = min(1.0, chain_share
                      + scenarios.for_chara(chara_info)
                      .support_event_bonus_pct(full_state) / 100.0)
    want_random = random.random() >= chain_share
    if not chains_allowed:
        want_random = True
    pick = None
    for pool in pools:
        if not pool:
            continue
        pick = _pick_support_event(pool, fired, want_random, full_state)
        # The chain fallback is only allowed when chains are (camp / post-72
        # windows pause them entirely).
        if not pick and chains_allowed:
            pick = _pick_support_event(pool, fired, not want_random, full_state)
        if pick:
            break
    if not pick:
        return False
    card, sid, title, ev, is_random = pick

    # A random event's id depends on WHICH random event it is -- #3 has its own
    # 200xx (see event_engine.support_random_event_id); #1/#2 share 20000.
    event_id = (event_engine.support_random_event_id(sid) if is_random
                else event_engine.SUPPORT_EVENT_ID)
    # A support event of this SAME wire type (chain 10002 / random 20000) is
    # already queued and unresolved -- see _career_ctx_pending. Firing another
    # would collide under the same lookup key, so treat it as the rate roll
    # simply missing rather than stacking a second one behind it.
    if _career_ctx_pending(full_state, event_id):
        # TEMP INSTRUMENTATION (firing-rate investigation) -- the 0.75 roll
        # passed but a same-type ctx is still unresolved, so this counts as a
        # miss. Remove once the pending-backlog question is answered.
        log.info("SUPPORT_FIRE_RATE pending_skip event_id=%s turn=%s",
                 event_id, chara_info.get("turn"))
        return False
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=0, support_card_id=card, play_timing=1))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(
        full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_id, "story_id": sid, "source": "support",
        "source_id": card, "title": title,
        "queued_turn": chara_info.get("turn"),  # TEMP INSTRUMENTATION
        "trainee_card_id": chara_info.get("card_id")})
    return True


# The trainee's own FIXED-TURN story events, recovered from real captures
# (per-turn cross-career comparison): {turn: [(story_suffix, event_slot,
# play_timing)]}. Story = 500000000 + chara*1000 + suffix. Seasonal beats
# (New Year 101/102, camp 103-105 + ends 300/301, Valentine 107, Fan Fest
# 108, Holiday 109, raffle 302) + the 4-part chara intro chain 113-116.
# Served as acknowledge story events; choice effects for seasonals are not
# in the datamined caches (client shows its own choices; a wrong-effect
# guess is worse than narration-only -- refine when data exists). The shared
# URA scenario chain is deliberately NOT here (single_mode_events'
# SCENARIO_SCHEDULE owns shared events; duplicating would double-fire).
# --- URA SCENARIO FIXED EVENTS (user-supplied chart, 2026-07-28) -------------
# Turn numbers derive from the calendar the whole scenario uses: 24 turns a
# year, two per month (Early/Late), Junior 1-24, Classic 25-48, Senior 49-72.
# So turn = year_base + (month - 1) * 2 + (1 Early | 2 Late). Cross-checked
# against _CAMP_TURNS, which is July+August of Classic/Senior = 37-40 / 61-64.
#
#   Holding an Event!        Late March, Classic      24 + 6  = 30   (no gate)
#   A Three-Legged Race      Early November, Classic  24 + 21 = 45   50,000 fans
#   At the Carrot Farm       Late December, Classic   24 + 24 = 48  100,000 fans
#   Going to the URA Finale! Late December, Senior    48 + 24 = 72  240,000 fans
#
# Served by SHORT story id, which is what the official server puts in
# unchecked_event_array (see the Goal Achieved notes). The fan gate is a
# REQUIREMENT: miss it and the event simply does not fire.
# --- UNIQUE SKILL + ITS LEVEL-UP EVENTS (user-supplied, 2026-07-28) ----------
# Every trainee owns a unique skill from the start, and master carries TWO for
# each character (verified for 1010 and 1013, both named identically):
#   900000 + (chara-1000)*10 + 1   rarity 1  -- the lesser 1*/2* variant
#   100000 + (chara-1000)*10 + 1   rarity 5  -- the PRIMARY, from 3* up
# Starting level is by star rating: 3* -> 1, 4* -> 2, 5* -> 3 (so 3 at most).
# Each of the three scenario events below adds one more, so a 5* trainee taking
# all three tops out at 6 -- the stated maximum.
_UNIQUE_SKILL_MAX_LEVEL = 6
_GAUGE_GREEN = 60        # bond colours: 0-59 blue, 60-79 GREEN, 80-99 orange, 100 max


def _unique_skill_for(card_id, rarity) -> tuple:
    """(skill_id, starting level) for a trainee's unique skill, or (None, 0)."""
    chara = (card_id or 0) // 100
    if not chara:
        return None, 0
    offset = (chara - 1000) * 10 + 1
    if (rarity or 0) < 3:
        return 900000 + offset, 1          # the 1*/2* variant, always level 1
    return 100000 + offset, max(1, min(3, int(rarity) - 2))


def _grant_unique_skill(chara_info: dict) -> None:
    """Put the trainee's unique skill in skill_array at its starting level.
    Trainees were being created with an EMPTY skill_array, so they never owned
    their unique skill at all and the level-up events had nothing to act on."""
    skill_id, level = _unique_skill_for(chara_info.get("card_id"),
                                        chara_info.get("rarity"))
    if not skill_id:
        return
    arr = chara_info.setdefault("skill_array", [])
    for s in arr:
        if s.get("skill_id") == skill_id:
            return
    arr.append({"skill_id": skill_id, "level": level})


def _level_unique_skill(chara_info: dict, by: int = 1) -> bool:
    """Raise the trainee's unique skill by `by` levels, capped. Grants it first
    if somehow absent. Returns whether anything changed."""
    skill_id, start = _unique_skill_for(chara_info.get("card_id"),
                                        chara_info.get("rarity"))
    if not skill_id:
        return False
    arr = chara_info.setdefault("skill_array", [])
    entry = next((s for s in arr if s.get("skill_id") == skill_id), None)
    if entry is None:
        entry = {"skill_id": skill_id, "level": start}
        arr.append(entry)
    new = min(_UNIQUE_SKILL_MAX_LEVEL, (entry.get("level") or start) + by)
    if new == entry.get("level"):
        return False
    entry["level"] = new
    return True


# The three UNIQUE SKILL LEVEL-UP events, all in Senior year. Each is the
# trainee's own seasonal story (all 62 trainees have all three) and each grants
# +1 unique-skill level when its FAN requirement is met -- which differs for
# turf vs dirt trainees (whichever ground aptitude is higher).
#   turn 51  Early Feb  'Valentine's Day'   (107)  60,000 turf / 40,000 dirt
#   turn 55  Early Apr  'Fan Fest'          (108)  70,000 turf / 60,000 dirt
#                       ...and Akikawa's gauge GREEN (>=60). Mood +1 when the
#                       whole condition is met, Energy -5 when it is not.
#   turn 72  Late Dec   'Holiday Season'    (109) 120,000 turf / 80,000 dirt
# (Suffixes 107/108/109 have no short_story_id, so the full id is served.)
# Each has its OWN event_id, in the same 3xxx shared-beat family every other
# per-trainee calendar story uses (_CHARA_FIXED_STORY_EVENTS: suffix 101 -> 3001,
# 102 -> 3002, ...). They were going out under the generic outing id 6000, which
# costs the client its per-event presentation. Corpus, all four scenarios, every
# career that reaches the turn: 3007 on turn 51 (185 acks), 3008 on 55 (181),
# 3009 on 72 (171), and never on any other turn. The wire captures agree on the
# rest of the shape we already serve -- story 50<chara>107/108/109, one choice,
# play_timing 1, chara_id the trainee.
_UNIQUE_LEVEL_EVENTS = {
    51: (107, 3007, 60_000, 40_000, False),
    55: (108, 3008, 70_000, 60_000, True),
    72: (109, 3009, 120_000, 80_000, False),
}
UNIQUE_LEVEL_FIRED_KEY = "unique_skill_events_fired"


def _is_dirt_trainee(chara_info: dict) -> bool:
    return (chara_info.get("proper_ground_dirt") or 0) > (
        chara_info.get("proper_ground_turf") or 0)


# URA's fan-gated fixed beats and the Grand Live chain now live in
# career_producers, served through the unified pipeline with ONE fired-set.
# These two keys are LEGACY: nothing writes them any more, but they stay listed
# in clear_active_career so a career started before the migration still gets its
# stale bookkeeping wiped instead of carrying it forever.
SCENARIO_FIXED_FIRED_KEY = "scenario_fixed_fired"
GRAND_LIVE_EVENTS_FIRED_KEY = "grand_live_events_fired"


# --- DIRECTOR'S FAN randoms (user-supplied chart, 2026-07-28) ----------------
# Four extra random events that only become possible once Yayoi Akikawa's
# friendship gauge is MAXED (100 -- see the colour scale on _GAUGE_GREEN).
# All four are scenario stories with real rows, and each is served by its own
# id (they have no separate short_story_id).
_DIRECTOR_FAN_EVENTS = (
    (400001028, [{"type": "speed", "value": "+10"}]),   # Kiryuin's Day Off
    (400001029, [{"type": "power", "value": "+10"}]),   # Happy Shoe Shopping
    (400001026, [{"type": "wisdom", "value": "+10"}]),  # A Reporter's Duty
    (400001023, [{"type": "mood", "value": "+1"}]),     # To Action! Before Dawn!
)
_DIRECTOR_FAN_CHANCE = 0.10     # per eligible turn; rate is a knob, not ground truth
DIRECTOR_FAN_FIRED_KEY = "director_fan_fired"


def _maybe_fire_condition_event(full_state: dict, career: dict, response: dict) -> bool:
    """Fire a negative condition's OWN event when it procs this turn.

    USER-SUPPLIED: a proc costs -10 energy and SOMETIMES mood. It used to be
    applied silently inside the training result -- "night owl is supposed to
    have an event decreasing the mood, instead its bundled with the training".
    Unlike the one-shot story beats these can recur, so nothing is marked as
    fired."""
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    for cid in conditions.rolled_conditions(chara_info):
        suffix = conditions.CONDITION_STORY_SUFFIX.get(cid)
        if not suffix:
            continue
        story_id = 500000000 + chara * 1000 + suffix
        if not _story_exists(story_id):
            continue
        effects = [{"type": "energy",
                    "value": str(conditions.CONDITION_PROC_ENERGY)}]
        if random.random() < conditions.proc_mood_chance(cid):
            effects.append({"type": "mood", "value": "-1"})
        ev = {"choices": [{"effects": effects}]}
        entry = event_engine.career_event_entry(
            ev, _shared_beat_event_id(suffix, chara_info.get("turn")),
            story_id, chara_id=chara,
            support_card_id=0, play_timing=1)
        _queue_turn_event(response, full_state, entry)
        _push_career_ctx(full_state, {
            "event_id": entry["event_id"], "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": chara_info.get("card_id"), "event": ev})
        return True     # at most one condition event per turn
    return False


EPITHET_RUN_FIRED_KEY = "epithets_announced_in_run"


def _maybe_fire_epithet_events(full_state: dict, career: dict,
                               response: dict) -> int:
    """Announce every epithet the race that just finished completed.

    TRACKBLAZER ONLY. Scenario 4 does not hold epithets back until graduation:
    the moment a race satisfies one, the client is served an "Achievement!"
    cutscene from the 2038xx band and PAID for it -- two random stats, or a
    skill hint. See epithets.trackblazer_run_epithets for the band, the
    mapping and the evidence behind both.

    ONE EVENT PER EPITHET, chained (user-reported: "multiple epithets can be
    unlocked in one race, in that case each epithet unlock gets its own event
    and the event is just chained multiple times"). _queue_turn_event does the
    chaining, so emitting them in a loop is all that is needed.

    POSITION IN THE CHAIN: after the post-race result story, before the race
    fatigue / consecutive-race mood drop (user-reported: "right after race but
    before consequtive race mood down event"). It sits ahead of the secret
    event too, which is safe because that call and this one can never both fire
    -- this returns immediately for every scenario but Trackblazer.

    The fired set is per-career state (EPITHET_RUN_FIRED_KEY, listed in
    _CAREER_STATE_KEYS) rather than the account-level `epithets_owned`: the
    stats are paid once PER RUN, so a trainee who already owns the epithet from
    an earlier career still earns the reward again this one."""
    chara_info = career["data"]["chara_info"]
    if (chara_info.get("scenario_id") or 0) != scenarios.trackblazer.impl.SCENARIO_ID:
        return 0
    fired = [int(x) for x in (full_state.get(EPITHET_RUN_FIRED_KEY) or ())]
    new = epithets.newly_completed_in_run(career["data"], chara_info, fired)
    if not new:
        return 0
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    for nickname_id, event_id, story_id in new:
        # Seeded on the trainee and the epithet, not on the clock: a retried
        # race_out that rebuilds the chain must offer the same two stats.
        rng = random.Random("%s:epithet:%s" % (card_id, nickname_id))
        ev = {"choices": [{"effects":
                           epithets.trackblazer_reward_effects(event_id, rng)}]}
        entry = event_engine.career_event_entry(
            ev, event_id, story_id, chara_id=chara, support_card_id=0,
            play_timing=3)
        _queue_turn_event(response, full_state, entry)
        _push_career_ctx(full_state, {
            "event_id": entry["event_id"], "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": card_id, "event": ev})
        fired.append(nickname_id)
    full_state[EPITHET_RUN_FIRED_KEY] = fired
    log.info("trackblazer epithets completed: %s",
             [(n, e) for n, e, _s in new])
    return len(new)


def _maybe_fire_race_fatigue(full_state: dict, career: dict, response: dict,
                             race_ctx: dict) -> bool:
    """Fire 'Race Fatigue' / 'After Repeated Races...' for a race that has just
    finished, if the trainee has raced enough turns in a row to earn one.

    Chained behind the post-race result story via _queue_turn_event, which is
    where the real server puts it: in the corpus the fatigue event is always
    the LAST check_event of the race turn, after 7005/7006/7007. See
    race_fatigue for the model, the table and the corpus that backs it.

    Unlike the one-shot 5xx story beats this RECURS -- a career can take a
    dozen of them -- so nothing is written to FIRED_EVENTS_KEY, and both
    suffixes are excluded from the random chara-story pool that would
    otherwise burn them once and for all (see _chara_random_stories)."""
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    turn = race_ctx.get("turn")
    if turn in scenarios.for_chara(chara_info).race_fatigue_exempt_turns:
        return False
    streak = race_fatigue.consecutive_races(
        career["data"].get("race_history") or [], turn)
    # The energy she went INTO the race with, not what it left her with
    # (user-supplied: "the energy you have when you actually race"). Recorded
    # on the ctx at race_entry; a ctx from before that field existed reads as
    # unknown, which race_fatigue.roll treats as 'she had some'.
    outcome = race_fatigue.roll(streak, race_ctx.get("vital_at_entry"))
    if not outcome:
        return False
    story_id = 500000000 + chara * 1000 + outcome["suffix"]
    if not _story_exists(story_id):
        return False
    ev = {"choices": [{"effects": outcome["effects"]}]}
    entry = event_engine.career_event_entry(
        ev, _shared_beat_event_id(outcome["suffix"], turn), story_id,
        chara_id=chara, support_card_id=0, play_timing=3)
    _queue_turn_event(response, full_state, entry)
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    log.info("race fatigue: %s consecutive race turns at turn %s (energy %s) "
             "-> story %s%s", streak, turn, race_ctx.get("vital_at_entry"),
             story_id, " + Skin Outbreak" if outcome["skin_outbreak"] else "")
    return True


def _maybe_fire_director_fan(full_state: dict, career: dict, response: dict) -> bool:
    """Roll one of the Director's Fan events. Only possible with Akikawa's gauge
    MAXED; each fires at most once per career."""
    chara_info = career["data"]["chara_info"]
    if _bond_of(chara_info, _DIRECTOR_TARGET) < _DIRECTOR_MAX_BOND:
        return False
    fired = set(full_state.get(DIRECTOR_FAN_FIRED_KEY) or ())
    pool = [(sid, eff) for sid, eff in _DIRECTOR_FAN_EVENTS if sid not in fired]
    if not pool or random.random() >= _DIRECTOR_FAN_CHANCE:
        return False
    story_id, effects = random.choice(pool)
    if not _story_exists(story_id):
        return False
    ev = {"choices": [{"effects": effects}]}
    entry = event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, story_id,
        chara_id=(chara_info.get("card_id") or 0) // 100,
        support_card_id=0, play_timing=1)
    _queue_turn_event(response, full_state, entry)
    full_state[DIRECTOR_FAN_FIRED_KEY] = sorted(fired | {story_id})
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    return True


def _maybe_queue_unique_level(response: dict, full_state: dict, career: dict,
                              turn) -> bool:
    """Queue this turn's unique-skill level-up event. The event ALWAYS plays on
    its turn; whether it grants the level depends on the fan (and, on turn 55,
    bond) requirement -- turn 55 explicitly pays Energy -5 when unmet, which
    only makes sense if it plays regardless."""
    spec = _UNIQUE_LEVEL_EVENTS.get(turn)
    if not spec:
        return False
    suffix, event_id, need_turf, need_dirt, needs_green = spec
    chara_info = career["data"]["chara_info"]
    chara = (chara_info.get("card_id") or 0) // 100
    if not chara:
        return False
    story_id = 500000000 + chara * 1000 + suffix
    fired = set(full_state.get(UNIQUE_LEVEL_FIRED_KEY) or ())
    if story_id in fired or not _story_exists(story_id):
        return False
    need = need_dirt if _is_dirt_trainee(chara_info) else need_turf
    met = (chara_info.get("fans") or 0) >= need
    if met and needs_green:
        met = _bond_of(chara_info, _DIRECTOR_TARGET) >= _GAUGE_GREEN
    effects = []
    if met:
        effects.append({"type": "unique_skill_level", "value": "+1"})
        if needs_green:
            effects.append({"type": "mood", "value": "+1"})
    elif needs_green:
        effects.append({"type": "energy", "value": "-5"})
    ev = {"choices": [{"effects": effects}]}
    entry = event_engine.career_event_entry(
        ev, event_id, story_id, chara_id=chara,
        support_card_id=0, play_timing=1)
    _queue_turn_event(response, full_state, entry)
    full_state[UNIQUE_LEVEL_FIRED_KEY] = sorted(fired | {story_id})
    _push_career_ctx(full_state, {
        "event_id": entry["event_id"], "story_id": story_id,
        "source": "inline", "source_id": 0,
        "title": event_engine.event_title(story_id) or "",
        "trainee_card_id": chara_info.get("card_id"), "event": ev})
    return True


_CHARA_FIXED_STORY_EVENTS = {
    25: [(101, 3001, 1)],
    37: [(103, 3003, 1)],
    40: [(104, 3004, 1)],
    41: [(300, 3010, 6)],   # camp CLOSES on leaving (first post-camp turn) --
                            # live-tested: firing it on turn 40 was one turn early
    49: [(102, 3002, 1)],
    50: [(302, 6001, 6)],   # year-end raffle -- live-corrected: plays at the
                            # START of turn 50, not on 49 with the New Year beat
}

# Year-end raffle prizes: single_mode_event_item_detail event_category 177 --
# receive_item_id in the event's choice_array is what makes the client print
# the real name in 'You obtained: X' (it showed a literal '<itemname>' while
# we sent 0). Odds + effects are user-supplied ground truth (2026-07-26):
#   Tissues 10%   mood -1
#   Carrot 50%    +20 energy
#   Bushel 30%    +20 energy, mood +1, +5 all stats
#   Deluxe Carrot Hamburger Steak 5%  +30 energy, mood +1, +10 all stats
#   Hot Spring Ticket 5%  same as Deluxe, PLUS the end-of-career Hot Spring
#   Getaway scene -- see HOT_SPRING_KEY below
RAFFLE_CTX_KEY = "raffle_ctx"
# THE HOT SPRING TICKET's real payoff. Drawing it (item 1, 5%) pays the same
# stats as the Deluxe Carrot Hamburger Steak AND unlocks the trainee's "Hot
# Spring Getaway" scene at the very end of the career, immediately before her
# own final event (live-reported 2026-09-03: "it doesnt play here"). The
# comment above this table used to call that "a client-side story flag we don't
# model yet" -- it is a real per-trainee story, suffix 112, present for 66
# trainees (e.g. McQueen's 501013112).
HOT_SPRING_KEY = "hot_spring_ticket"
_HOT_SPRING_ITEM_ID = 1
_HOT_SPRING_SUFFIX = 112
_RAFFLE_PRIZES = (
    # (item_id, weight, effects)
    (5, 10, [{"type": "mood", "value": "-1"}]),
    (4, 50, [{"type": "energy", "value": "+20"}]),
    (3, 30, [{"type": "energy", "value": "+20"}, {"type": "mood", "value": "+1"},
             {"type": "all_stats", "value": "+5"}]),
    (2, 5, [{"type": "energy", "value": "+30"}, {"type": "mood", "value": "+1"},
            {"type": "all_stats", "value": "+10"}]),
    (1, 5, [{"type": "energy", "value": "+30"}, {"type": "mood", "value": "+1"},
            {"type": "all_stats", "value": "+10"}]),
)
_RAFFLE_EVENT_ID = 6001


def _roll_raffle() -> tuple:
    """(item_id, effects) drawn at the real odds."""
    total = sum(w for _i, w, _e in _RAFFLE_PRIZES)
    r = random.uniform(0, total)
    upto = 0.0
    for item_id, w, effects in _RAFFLE_PRIZES:
        upto += w
        if r <= upto:
            return item_id, effects
    return _RAFFLE_PRIZES[-1][0], _RAFFLE_PRIZES[-1][2]


_CHARA_FIXED_STORY_EVENTS.update({
    # Turns 51 / 55 / 72 are DELIBERATELY absent. They used to serve the
    # narration-only Valentine's Day (107) / Fan Fest (108) / Holiday Season
    # (109) beats, but those same three stories ARE the unique-skill level-up
    # events (see _UNIQUE_LEVEL_EVENTS), which serve them with their real
    # effects. Keeping both fired the story twice -- live-reported: "the
    # valentines event is served twice (the first one is the one we would
    # always serve that doesnt actually do anything, the second one is the one
    # with all the effects)".
    61: [(105, 3005, 1)],
    65: [(301, 3011, 6)],   # year-3 camp close, same one-turn-later rule
})

# The trainee's personal story chain (suffix 113-116, slots 11179-11182).
# These are NOT calendar events: each part narrates the trainee's ANTICIPATION
# of her next goal race, so they must be paced against HER OWN route, not the
# fixed turns of whichever career the schedule was recovered from -- doing the
# latter played McQueen's 'Hopes for the Tenno Sho' AFTER she'd already run
# the Tenno Sho (live-reported). Each part fires a couple of turns before one
# of her goal races.
_CHARA_CHAIN_SUFFIXES = [(113, 11179), (114, 11180), (115, 11181), (116, 11182)]
_CHAIN_LEAD_TURNS = 2


@functools.lru_cache(maxsize=64)
def _chara_chain_turns(route_race_id_array: tuple) -> tuple:
    """((turn, suffix, slot), ...) -- the personal-chain beats placed a couple
    of turns ahead of this trainee's own 2nd..5th goal races."""
    goals = [g for g in _route_all_goals(route_race_id_array) if g[2] == 1]
    out = []
    for i, (suffix, slot) in enumerate(_CHARA_CHAIN_SUFFIXES):
        if i + 1 < len(goals):
            turn = max(2, goals[i + 1][0] - _CHAIN_LEAD_TURNS)
            out.append((turn, suffix, slot, goals[i + 1][3]))
    return tuple(out)
CHARA_STORY_FIRED_KEY = "chara_story_turns_fired"
# chara_info field: the turn a story-imposed race lockout expires on.
RACE_RESTRICT_UNTIL_KEY = "race_restrict_until_turn"


@functools.lru_cache(maxsize=1)
def _race_restrict_stories() -> dict:
    """{(chara_id, turn): story_id} from single_mode_race_restrict_turn.

    THE TRIGGER TURN IS IN MASTER after all. This 3-row table pins exactly when
    each of these beats fires, and `gain_id` encodes which beat:

        gain_id = chara_id * 100000 + story_suffix * 100 + 11

    All three decode to a real, aptly-named story, which is what makes the
    formula trustworthy rather than a fit:

        1062 turn 70 -> 501062118 "There's Always Next Time"   (Japan Cup turn)
        1044 turn 36 -> 501044120 "Magic Training Week!"
        1078 turn 44 -> 501078118 "Eyes on the Promise"

    Matikanetannhauser's is turn 70 -- the very turn her Japan Cup goal sits on,
    which is why the beat can cancel it and lock her out of racing in one go.
    """
    out = {}
    for r in master_data.query(
            "SELECT chara_id, turn, gain_id FROM single_mode_race_restrict_turn"):
        gain = r["gain_id"] or 0
        chara, suffix = gain // 100000, (gain // 100) % 1000
        if chara != r["chara_id"] or not suffix:
            continue          # formula doesn't hold for this row -- skip it
        out[(r["chara_id"], r["turn"])] = 500000000 + chara * 1000 + suffix
    return out


def _maybe_fire_race_restrict_story(full_state: dict, career: dict, response: dict,
                                    new_turn) -> bool:
    """Fire the race-lockout beat scheduled for this trainee on this turn.

    Served through the dynamic event path, not as a cutscene: it carries real
    effects (Guts +5 / cannot race for 1 turn / objective race cancelled) and
    the plain _scenario_event_entry route would play the story and apply none
    of them."""
    chara_info = career["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    sid = _race_restrict_stories().get((chara, new_turn))
    if not sid:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if sid in fired:
        return False
    title = master_data.query_one(
        'SELECT text FROM text_data WHERE category=181 AND "index"=?', (sid,))
    ev = event_engine.resolve(sid, title["text"] if title else None, card_id)
    if not ev or not ev.get("choices"):
        return False
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara, support_card_id=0,
        play_timing=1, chara_info=chara_info))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
        "source": "chara", "source_id": card_id,
        "title": title["text"] if title else None, "trainee_card_id": card_id})
    return True


def _maybe_queue_chara_story(response: dict, full_state: dict, career: dict, new_turn) -> None:
    """Queue the trainee's own fixed-turn story beats on arrival at their
    turns (idempotent via a fired-marker; chains behind whatever the turn
    already shows, same as every other queued event)."""
    chara_info_ = career["data"]["chara_info"]
    try:
        _maybe_fire_race_restrict_story(full_state, career, response, new_turn)
    except Exception:
        log.exception("race-restrict story failed; skipping")
    entries = list(_CHARA_FIXED_STORY_EVENTS.get(new_turn) or ())
    # Chain beats narrate ANTICIPATION of a goal race -- never play one whose
    # race is already in the history ('Hopes for the Tenno Sho' popping up
    # after she'd run it, live-reported twice; happens when the beat lingered
    # in the backlog or the turn was revisited).
    ran = {h.get("program_id") for h in (career["data"].get("race_history") or [])}
    entries += [(suffix, slot, 1) for t, suffix, slot, goal_cid
                in _chara_chain_turns(tuple(chara_info_.get("route_race_id_array") or ()))
                if t == new_turn and goal_cid not in ran]
    if not entries:
        return
    fired = career["data"].setdefault(CHARA_STORY_FIRED_KEY, [])
    if new_turn in fired:
        return
    chara = (career["data"]["chara_info"].get("card_id") or 0) // 100
    if not chara:
        return
    fired.append(new_turn)
    card_id = career["data"]["chara_info"].get("card_id") or 0
    for suffix, slot, timing in entries:
        story = 500000000 + chara * 1000 + suffix
        if not master_data.query_one(
                "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (story,)):
            continue  # not every chara has every beat (e.g. shorter intro chains)
        # BOTH New Year beats are real 3-CHOICE events, not cutscenes -- route
        # them through the dynamic choice pipeline so the client offers the
        # options and the pick actually applies (live-reported: 'it just skips
        # through it'). 101 = Junior->Classic (turn 25), 102 = Classic->Senior
        # (turn 49); their real rewards differ, see event_engine.seasonal_event.
        kind = {101: "new_year", 102: "new_year_2", 104: "summer_camp"}.get(suffix)
        if kind:
            ev = event_engine.seasonal_event(kind, card_id)
            if ev:
                _queue_turn_event(response, full_state, event_engine.career_event_entry(
                    ev, event_engine.CHARA_EVENT_ID, story, chara_id=chara,
                    support_card_id=0, play_timing=timing))
                _push_career_ctx(full_state, {
                    "event_id": event_engine.CHARA_EVENT_ID, "story_id": story,
                    "source": "seasonal", "source_id": card_id, "title": kind,
                    "trainee_card_id": card_id})
                if kind == "new_year":
                    # ... and right behind it, each already-unlocked pal/group
                    # card's own New Year date (pal_cards.new_year_story, the
                    # GameTora 'ny' beat -- e.g. Light Hello's 'Bonding with
                    # Light Hello: Letting Loose'). It is part of the New Year
                    # chain, not a coincidence roll: live-reported 2026-09-03
                    # that nothing followed the shrine event.
                    #
                    # The recreation path (_queue_outing_event) KEEPS its own
                    # offer -- _new_year_outing_ready goes False once this
                    # fires, so a card unlocked BEFORE New Year gets the beat
                    # here and one unlocked after it still gets it on an
                    # outing. Both live reports hold.
                    _queue_pal_new_year(response, full_state, career)
                continue
        if suffix == 302:
            # Year-end RAFFLE: roll the prize NOW so the cutscene's choice
            # carries the real receive_item_id (the client prints its name in
            # 'You obtained: X'; 0 rendered a literal '<itemname>'). The
            # effects ride on the Event itself (career_events' own choice-
            # effects applier pays them on ack) -- no separate ctx needed.
            item_id, effects = _roll_raffle()
            if item_id == _HOT_SPRING_ITEM_ID:
                # Recorded at ROLL time, which is when the prize is decided and
                # baked into the entry's receive_item_id -- the player sees
                # "You obtained: Hot Spring Ticket" from this same draw.
                full_state[HOT_SPRING_KEY] = 1
                log.info("raffle: Hot Spring Ticket drawn -- the end-of-career "
                         "Hot Spring Getaway is now unlocked")
            entry = _scenario_event_entry(_RAFFLE_EVENT_ID, story,
                                          play_timing=timing,
                                          choices=[_ack_choice()], chara_id=chara)
            for c in entry["event_contents_info"]["choice_array"]:
                c["receive_item_id"] = item_id
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=_RAFFLE_EVENT_ID, story_id=story, raw=entry,
                choices=[career_events.Choice(effects=effects)],
                priority=career_events.PRIO_SPECIAL, source="raffle"))
            continue
        _queue_turn_event(response, full_state, _scenario_event_entry(
            slot, story, play_timing=timing, choices=[_ack_choice()], chara_id=chara))


# The trainee's own RANDOM events (story suffix 5xx) -- the mid-training
# "coincidence" beats specific to HER, with real choices/effects from the
# datamined data (13 of ~19 resolve per character). Distinct from support-card
# events (which fire from the deck) and from the outing chain (7xx).
# Kept LOW deliberately: a trainee has only ~13 of these for a whole career,
# so they must feel like rare personal beats, not a per-turn occurrence
# (live-reported at 0.28: "firing way too fast ... one every turn").
_CHARA_RANDOM_CHANCE = 0.07


@functools.lru_cache(maxsize=256)
def _chara_random_stories(chara: int, trainee_card_id: int = 0) -> tuple:
    """(story_id, title) for the trainee's own random events, for THIS OUTFIT.

    Two ranges hold them and both live in the shared chara band: 5<chara>5xx
    (events every outfit gets, plus the BASE card's costume events) and
    5<chara>8xx (an alt outfit's costume events). Scanning 5xx alone therefore
    did two wrong things at once -- it offered a swimsuit trainee the base
    outfit's costume events, and it never offered her own (live-reported:
    'outfit-variant trainee events wrong ... saw weird things on one').

    character_event_data is keyed by CARD and lists only that outfit's costume
    events, so it settles ownership: drop a story that belongs to a SIBLING
    outfit and not to this one. Events no card claims (the generic 'Fan Letter'
    /'Race Fatigue' set) belong to everyone and are always kept."""
    base = 500000000 + chara * 1000
    # THREE ranges. 5<chara>5xx and 5<chara>8xx are the costume/generic events
    # (see below); 5<chara>1xx from 113 up is the trainee's OWN story chain --
    # Matikanetannhauser's "There's Always Next Time", Agnes Tachyon's whole
    # "Report:" series -- and it was in neither pool nor the fixed-turn table,
    # so every one of those beats was dead content for every trainee.
    #
    # 113 is the boundary, NOT 111, and getting that wrong put the trainee's
    # own ENDING in the lottery (live-reported: McQueen's "Getting Boring",
    # story 501013111, played on turn 1). The 1xx band up to and INCLUDING 112
    # is reserved:
    #   ..110  the SHARED schedule -- intro, New Year, camps, Valentine's,
    #          Fan Fest, "Ending (Normal)" -- all fired by
    #          _maybe_queue_chara_story;
    #     111  the trainee's own final beat, served by the ending chain at
    #          turn 78 (see _ENDING_CHARA_EVENT_ID). Every chara has one:
    #          501001111 "I'm Home!", 501005111 "Finale: Light through Yonder
    #          Window", 501020111 "Happily Ever After?";
    #     112  the Hot Spring Getaway, gated on the year-end raffle ticket
    #          (_HOT_SPRING_SUFFIX).
    # None of the three belongs to a random roll, and none of the filters below
    # would have caught them: they are chain-served, so they are in neither
    # _race_restrict_stories nor _secret_story_ids.
    rows = master_data.query(
        "SELECT [index], text FROM text_data WHERE category=181 "
        "AND ([index] BETWEEN ? AND ? OR [index] BETWEEN ? AND ? "
        "     OR [index] BETWEEN ? AND ?) ORDER BY [index]",
        (base + 500, base + 599, base + 800, base + 899,
         base + 113, base + 199))
    # A beat with a scheduled turn (single_mode_race_restrict_turn) must not
    # also be reachable from the random roll, or it fires early and its lockout
    # lands on the wrong turn.
    scheduled = set(_race_restrict_stories().values())
    rows = [r for r in rows if r["index"] not in scheduled]
    # ...and NEITHER may a SECRET event. Their story ids sit in these very
    # bands -- Mayano Top Gun's 'The Fruits of My Labor' is 501024118, right
    # beside her personal chain -- so the random roll happily served one to a
    # player who had met none of its conditions (live-reported 2026-09-06:
    # the four-strategy-G1-wins event popping up out of nowhere). A secret
    # event has exactly one legitimate trigger, _maybe_fire_secret_event.
    secret = _secret_story_ids(trainee_card_id) if trainee_card_id else frozenset()
    rows = [r for r in rows if r["index"] not in secret]
    # ...and neither may 'Race Fatigue' (508) or 'After Repeated Races...'
    # (509). Same rule again: they have a real trigger now -- consecutive races,
    # see _maybe_fire_race_fatigue -- so the lottery must not serve them out of
    # nowhere, and must not burn them either. These two RECUR, and every other
    # story in this pool fires at most once per career, so leaving them in
    # capped a career at one 'Race Fatigue' total and spent it on a turn that
    # had nothing to do with racing.
    rows = [r for r in rows if r["index"] - base not in
            (race_fatigue.FATIGUE_SUFFIX, race_fatigue.REPEATED_SUFFIX)]
    mine = event_engine.card_event_title_keys(trainee_card_id) if trainee_card_id else None
    # No event data for this card (an outfit newer than character_event_data)
    # means no basis for deciding ownership. Filtering anyway would treat every
    # sibling's event as forbidden and leave the trainee with only the handful
    # of generic ones, so serve the whole band instead.
    if not mine:
        return tuple((r["index"], r["text"]) for r in rows)
    theirs = set()
    for sib in event_engine.sibling_card_ids(trainee_card_id):
        theirs |= event_engine.card_event_title_keys(sib)
    theirs -= mine
    return tuple((r["index"], r["text"]) for r in rows
                 if event_engine.title_key(r["text"]) not in theirs)


def _maybe_fire_chara_random_event(full_state: dict, career_state: dict, response: dict,
                                   force: bool = False) -> bool:
    """Occasionally fire one of the trainee's own random story events, served
    (choices + effects) by the event engine and committed through the same
    preview/commit path every dynamic event already uses. Each fires at most
    once per career. force=True skips the rate roll (used when the turn NEEDS
    an event to carry the goal-cleared banner)."""
    if not force and random.random() >= _CHARA_RANDOM_CHANCE:
        return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    candidates = [(sid, title) for sid, title in _chara_random_stories(chara, card_id)
                  if sid not in fired]
    random.shuffle(candidates)
    for sid, title in candidates:
        ev = event_engine.resolve(sid, title, card_id)
        synthesized = False
        if not ev or not ev.get("choices"):
            # SHARED beats (Dance Lesson, ...) sit in this band but exist in no
            # event cache, so resolve() finds nothing and they were skipped
            # every time -- they had never fired once. Synthesize the ones
            # whose effects we know from the trainee's own stat codes.
            ev = event_engine.shared_event(sid % 1000, card_id)
            synthesized = True
        if not ev or not ev.get("choices"):
            continue
        # Serve under the story's REAL event id where a capture pins it (e.g.
        # Master Trainer/Dance Lesson 10000, the food event 7019) -- the client
        # stages these differently from a plain outing.
        event_id = _shared_beat_event_id(sid % 1000, chara_info.get("turn"))
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_id, sid, chara_id=chara,
            support_card_id=0, play_timing=6))
        full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
        ctx = {"event_id": event_id, "story_id": sid,
               "source": "chara", "source_id": card_id, "title": title,
               "trainee_card_id": card_id}
        if synthesized:
            # nothing to re-resolve from at commit time -- carry the event
            ctx["event"] = ev
        _push_career_ctx(full_state, ctx)
        return True
    return False


@functools.lru_cache(maxsize=2048)
def _race_instance_for_program(program_id) -> int | None:
    """single_mode_program id -> race_instance id. race_history records the
    PROGRAM, while secret-event conditions name the RACE, so the two have to be
    joined before a condition can be checked."""
    if not program_id:
        return None
    row = master_data.query_one(
        "SELECT race_instance_id FROM single_mode_program WHERE id=?", (program_id,))
    return row["race_instance_id"] if row else None


@functools.lru_cache(maxsize=2048)
def _race_facts_for_program(program_id) -> dict | None:
    """Everything a secret condition can ask ABOUT a race, in one join:
    its race-instance id (what the conditions name), grade (100 = G1),
    distance, ground (1 turf / 2 dirt) and racetrack.

    Conditions like 'win a Long G1' or 'win 8 races at Hanshin' are pure
    functions of this row plus the placement race_history already records."""
    if not program_id:
        return None
    row = master_data.query_one(
        "SELECT ri.id AS race_id, r.grade AS grade, cs.distance AS distance, "
        "       cs.ground AS ground, cs.race_track_id AS track_id "
        "FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id "
        "LEFT JOIN race_course_set cs ON cs.id = r.course_set "
        "WHERE p.id=?", (program_id,))
    return dict(row) if row else None


# race_permission -> the career years that program is enterable in.
_RACE_PERMISSION_YEARS = {1: (1,), 2: (2,), 3: (3,), 4: (2, 3)}


@functools.lru_cache(maxsize=4096)
def _race_entry_turns(race_id) -> tuple:
    """((turn, year), ...) -- every career turn this race can be entered on.

    A race instance appears in several programs (the Tenno Sho (Spring) is
    enterable in both Classic and Senior, race_permission 4), and a NEGATIVE
    condition -- 'do NOT participate in the Tenno Sho' -- is only decidable
    once the LAST of those turns has gone by. Without that, 'you didn't run
    it' is true on turn 1 of every career and the event fires immediately,
    which is the same class of bug as serving it at random."""
    out = []
    for r in master_data.query(
            "SELECT month, half, race_permission FROM single_mode_program "
            "WHERE race_instance_id=?", (race_id,)):
        for year in _RACE_PERMISSION_YEARS.get(r["race_permission"], ()):
            out.append(((year - 1) * 24 + ((r["month"] or 1) - 1) * 2 + (r["half"] or 1),
                        year))
    return tuple(sorted(set(out)))


def _race_deadline(race_id, year=None):
    """The last turn `race_id` (in `year`, or in any year) can still be run."""
    turns = [t for t, y in _race_entry_turns(race_id) if year is None or y == year]
    return max(turns) if turns else None


def _secret_context(full_state: dict, career_data: dict) -> secret_events.Context:
    """The career as the secret conditions see it.

    Built here rather than in secret_events because every fact needs a
    master.mdb join or a career-state key: that module stays a pure evaluator
    of the condition language, which is what makes it testable.

    A race run WITHOUT a simulation (the static fixture fallback) carries no
    popularity and no rival placements, so those stay None -- and every
    condition that needs them then evaluates to 'unknown' and refuses to fire,
    rather than quietly reading a missing field as a loss."""
    chara_info = career_data.get("chara_info") or {}
    history = career_data.get("race_history") or []
    goals = _route_all_goals(tuple(chara_info.get("route_race_id_array") or ()))
    objective_programs = {g[3] for g in goals if g[2] == 1}
    races = []
    for h in history:
        facts = _race_facts_for_program(h.get("program_id")) or {}
        if not facts.get("race_id"):
            continue          # a race master no longer knows -- skip, not guess
        ranks = h.get("rival_ranks")
        races.append(secret_events.Race(
            program_id=h.get("program_id") or 0,
            race_id=facts["race_id"],
            turn=h.get("turn") or 0,
            rank=h.get("result_rank") or 99,
            running_style=h.get("running_style") or 0,
            grade=facts.get("grade") or 0,
            distance=facts.get("distance") or 0,
            ground=facts.get("ground") or 0,
            track_id=facts.get("track_id") or 0,
            objective=h.get("program_id") in objective_programs,
            popularity=h.get("popularity"),
            rival_ranks=({int(k): int(v) for k, v in ranks.items()}
                         if isinstance(ranks, dict) else None),
        ))
    return secret_events.Context(
        races=races,
        turn=chara_info.get("turn") or 0,
        fans=chara_info.get("fans") or 0,
        goals_cleared=_goals_cleared_count(chara_info, history),
        fired_story_ids=set(full_state.get(event_engine.FIRED_EVENTS_KEY) or ()),
        promises_fulfilled=set(career_data.get(FAN_PROMISES_DONE_KEY) or ()),
        race_deadline=_race_deadline,
    )


def _secret_events_for(card_id: int) -> list:
    """The trainee's secret events as (story_id, title, event), in story order.
    Their story ids are spread across the chara band (McQueen's 'Autumn
    Congratulations' is 501013314), so the whole band is searched by title
    rather than one fixed sub-range."""
    out = []
    chara = card_id // 100
    if not chara:
        return out
    base = 500000000 + chara * 1000
    titles = {}
    for r in master_data.query(
            'SELECT "index" i, text FROM text_data WHERE category=181 '
            'AND "index" BETWEEN ? AND ?', (base, base + 999)):
        if r["text"]:
            titles.setdefault(event_engine.title_key(r["text"]), r["i"])
    for ev in (event_engine.card_events_for_trainee(card_id) or {}).values():
        if not isinstance(ev, dict) or ev.get("section") != "secret":
            continue
        sid = titles.get(event_engine.title_key(ev.get("name") or ""))
        if sid and ev.get("choices"):
            out.append((sid, ev["name"], ev))
    out.sort(key=lambda t: t[0])
    return out


@functools.lru_cache(maxsize=256)
def _secret_story_ids(card_id: int) -> frozenset:
    """The story ids of this trainee's secret events -- the exclusion list the
    random-event pool subtracts (see _chara_random_stories)."""
    #
    # ...only the ones that actually HAVE a precondition. GameTora files Smart
    # Falcon's 'Coming to a City Near You ☆' under `secret`, but it carries no
    # conditions at all -- it just hands out a random Fan Promise -- so it is an
    # ordinary random beat and belongs in the pool. is_eligible() refuses to
    # fire a condition-less event anyway, so excluding it here would have made
    # it unreachable from BOTH paths.
    try:
        return frozenset(sid for sid, _title, ev in _secret_events_for(card_id)
                         if ev.get("conditions"))
    except Exception:                                        # noqa: BLE001
        log.exception("secret story id lookup failed for card %s", card_id)
        return frozenset()


# The REAL event_id the official server sends for a secret event, harvested from
# a capture -- same practice as the 7xx beat table below. Ours defaults to the
# generic CHARA_EVENT_ID envelope; where a capture pins the real id, use it.
#   501032114 'Report: A Clear Gaze' -> 11440   (`docs/tachyon switch`, seq 1192)
_SECRET_EVENT_IDS = {501032114: 11440}


def _maybe_fire_secret_event(full_state: dict, career_state: dict, response: dict) -> bool:
    """Fire a SECRET event whose precondition the career has now met (#21).

    Checked every turn rather than on a schedule: the whole point is that they
    unlock from what the player has DONE (won the Satsuki Sho in Classic, ...),
    so the turn a condition becomes true is the turn the event is due. Events
    whose conditions we cannot fully evaluate never fire -- see
    secret_events."""
    career_data = career_state["data"]
    chara_info = career_data["chara_info"]
    if not scenarios.for_chara(chara_info).has_secret_events:
        # MANT, on Trackblazer: "An Uma's Career Goals and Secret Events are
        # disabled". Gated here rather than inside secret_events, which is a
        # pure condition evaluator and has no business knowing about scenarios.
        return False
    card_id = chara_info.get("card_id") or 0
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    ctx = _secret_context(full_state, career_data)
    due = [(sid, title, ev) for sid, title, ev in _secret_events_for(card_id)
           if sid not in fired and secret_events.is_eligible(ev, ctx)]
    # GRADUATED win_g1 variants (Symboli Rudolf: 0-6 / 7 / 8 G1 wins) are one
    # event with three outcomes, and a `count` is 'at least', so once the top
    # one is due the weaker ones are due as well. Serve only the strongest --
    # otherwise the player collects both 'Seven Crowns Attained' and 'Current
    # of Success' on consecutive turns.
    thresholds = [t for t in (secret_events.win_g1_threshold(e) for _s, _t, e in due)
                  if t is not None]
    if thresholds:
        best = max(thresholds)
        due = [(sid, title, ev) for sid, title, ev in due
               if secret_events.win_g1_threshold(ev) in (None, best)]
    for sid, title, ev in due:
        ev = _resolve_reward_branch(ev, ctx)
        # The SAME id must go on the entry and on the context: the client
        # commits the choice under the event_id it was served, and
        # _career_event_from_ctx looks the event up by that id. Serving 11440
        # while registering 6000 lost the commit entirely -- the story played
        # and the branch animation ran, but no effect was ever applied, so the
        # objective never actually changed (live-reported 2026-09-04).
        event_id = _SECRET_EVENT_IDS.get(sid, event_engine.CHARA_EVENT_ID)
        _queue_turn_event(response, full_state, event_engine.career_event_entry(
            ev, event_id, sid, chara_id=card_id // 100, support_card_id=0,
            play_timing=1, chara_info=chara_info))
        full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
        event_ctx = {
            "event_id": event_id, "story_id": sid,
            "source": "chara", "source_id": card_id, "title": title,
            "trainee_card_id": card_id}
        if ev.get("_branch_resolved"):
            # Carry the RESOLVED event so the commit pays the same arm it was
            # served with. Re-resolving by title at commit time would go back
            # through resolve_segments, whose guard evaluator cannot see the
            # career's race history and so always lands on the first arm --
            # Rice Shower's list starts at '5+', so every player would be paid
            # the top branch. (Same mechanism the synthesized shared beats use.)
            event_ctx["event"] = ev
        _push_career_ctx(full_state, event_ctx)
        return True
    return False


def _resolve_reward_branch(ev: dict, ctx) -> dict:
    """Pick the reward arm a `*_wins_branch` event has earned.

    'The rewards will depend on the amount of Kyoto racetrack wins' is a real
    reward table -- 2 wins, 3-4, 5+ -- and which arm applies is a fact about
    the career, so it is settled HERE, where the race history is in hand,
    rather than by the generic segment guard evaluator (which only knows mood
    and season). Events without such a branch are returned untouched."""
    if secret_events.branch_count(ev, ctx) is None:
        return ev
    out = copy.deepcopy(ev)
    changed = False
    for choice in out.get("choices") or ():
        idx = secret_events.branch_segment_index(ev, ctx, choice)
        if idx is None:
            continue
        seg = (choice.get("segments") or [])[idx]
        for key in ("effects", "outcomes", "probs", "random_either"):
            choice.pop(key, None)
        choice.update({k: v for k, v in seg.items() if k != "when"})
        choice.pop("segments", None)
        changed = True
    if not changed:
        return ev
    out["_branch_resolved"] = True
    return out


_EXTRA_TRAINING_CHANCE = 0.06    # user-supplied: after a SUCCESSFUL training
# The acupuncturist ('Just an Acupuncturist, No Worries! ☆', the five-option
# gamble). User-supplied: "very rare event (lets say 1/400 per turn)". Briefly
# raised to 1/100 and then to 1.0 for live testing on 2026-07-28; restored to
# the intended rarity at the user's request ("shes really rare"). At 1/400 only
# ~16% of full careers ever meet her, which is the point.
_ACUPUNCTURE_CHANCE = 1 / 400
_EXTRA_TRAINING_SUFFIX = 715
_ACUPUNCTURE_SUFFIX = 720


# The REAL event_id the official server sends for each 7xx beat, harvested from
# the captures (story suffix -> event_id):
#   700 Sleep Deprived 7009 | 701 Well-Rested! 7011 | 718 All Refreshed 7010
#   713 Get Well Soon! 7014 | 714 Don't Overdo It! 7015 | 717 Infirmary 7016
#   715 Extra Training 7017 | 708 Victory! 7005
#   726 Claw Machine 6002 | 731 Claw Machine: Success! 6007
#   732/733 Inspiration 7044/7045
# These are server constants -- no master table maps them (checked every table
# for a story<->event pairing; single_mode_event_production is only item-reveal
# staging for the raffle/gift events, and event_category is 3 for all of these).
#
# It MATTERS: the client keys its special per-event presentation off event_id,
# which is why each of these has its own. Sending the generic outing id 6000
# gets a plain event with no staging -- live-reported as the acupuncturist not
# playing its win/lose animation.
#
# 720 (the acupuncturist) is NOT in any capture, so her real id is UNKNOWN and
# she still falls back to the generic id. That is the remaining gap.
# ONLY capture-confirmed ids belong in here (harvested from every
# (event_id, story_id) pair in all 23 UmaDumpy sessions + the 07-17 capture --
# 193 distinct pairs; see harvest_event_ids.py). Suffixes whose id VARIES per
# trainee are excluded: the 'Before the X' / 'After the X' / reflection bands
# (401, 408, 415, 416, 513, 514) each showed 2-4 different ids, so they are
# genuinely per-trainee and cannot be mapped by suffix.
#
# 709 'Solid Showing' and 710 'Defeat' are the obvious siblings of 708 and are
# PROBABLY adjacent (7006/7007) -- but nothing captured them, so they stay out.
# A wrong id costs only the special staging, which the generic fallback already
# loses, so inventing one buys nothing.
#
# 2026-09-08: the three real `captures/bot/20260905_*_icarus` sessions carry 77
# (story_id, event_id) pairs for a Kitasan Black (chara 1068) career, which adds
# 24 more suffixes -- and CONFIRMS the 7006/7007 guess above outright. Every one
# added below was cross-checked for being genuinely shared rather than that
# trainee's own id: each appears in the bot logs under 5 to 16 different
# (scenario, preset) pairs, i.e. under several different trainees in all four
# scenarios. The same check is what keeps the rest of chara 1068's pairs OUT --
# 3112 (suffixes 513/514/515) and the whole 12526-12558 block (suffixes
# 401-433, minus 409/410) appear under ZERO other presets, so they are
# per-trainee ids in the same family as chara 1006's 11051-11066, and they
# belong to the unsolved 11xxx/12xxx band, not here. That independently
# reproduces the earlier 23-session finding that 401/408/415/416/513/514 vary.
_SHARED_BEAT_EVENT_IDS = {
    # --- trainee "special event" family: all five captured instances use 10000
    500: 10000,   # Master Trainer
    506: 10000,   # Dance Lesson      <- we were sending 6000
    510: 10000,   # Errands Have Perks
    511: 10000,   # Beauteaful
    520: 10000,   # Tracen Karuta Queen
    519: 10012,   # Calm and Collected?
    517: 10012,   # Bad Timing
    518: 10012,   # Speak Not of One's Age
    524: 10000,   # Kitasan the Traveling Masseuse!
    516: 7019,    # Putting It Away at the Cafeteria (the FOOD event)
    523: 7000,    # Could I Be a Genius?!
    507: 10001,   # Fan Letter -- was INFERRED as 10000, captured as 10001
    # --- calendar / scenario beats
    101: 3001,    # New Year's Resolutions
    102: 3002,    # New Year's Shrine Visit
    104: 3004,    # At Summer Camp (Year 2)
    107: 3007,    # Valentine's Day
    108: 3008,    # Fan Fest
    109: 3009,    # Holiday Season
    111: 6,       # The Northern Sun
    300: 3010,    # Summer Camp (Year 2) Ends
    301: 3011,    # Summer Camp (Year 3) Ends
    302: 6001,    # Raffle Time!  <- the A8 raffle; see the turn caveat below
    100: 3000, 400: 3000,   # Introducing <trainee>! (both of its ids -- see below)
    # 2026-09-08 CORRECTION. These two were keyed 409/410 when they went in,
    # read straight off the Kitasan Black captures. That is a per-trainee
    # suffix, not a shared one: master carries each of these beats TWICE, once
    # at a fixed suffix (103/105) and once inside the trainee's own
    # chronological chain, and the chain position moves per trainee -- Kitasan
    # Black's camp beats are 409/410, Special Week's and El Condor Pasa's are
    # 406/407, Gold Ship's are 409 and 420. Keying on 409 would have given
    # Special Week's "Before the Kisaragi Sho" the camp id. This table is keyed
    # by the REFERENCE suffix, which is what every producer here computes, and
    # event_engine.wire_story_id turns it into the trainee's own short id on
    # the way out. Same reason 100 sits beside 400 above.
    103: 3003,    # Summer Camp (Year 2) Begins!  (short id 4xx, per trainee)
    105: 3005,    # Summer Camp (Year 3) Begins!  (short id 4xx, per trainee)
    600: 3016,    # Ready for a Challenge
    601: 3017,    # Not This Time!
    800: 3061,    # Swimsuit Power Unlocked!
    900: 4000,    # Self-Introduction
    # --- the 7xx band: each beat has its OWN id
    700: 7009,    # Sleep Deprived
    701: 7011,    # Well-Rested!
    705: None,    # (placeholder: not captured)
    708: 7005,    # Victory!
    709: 7006,    # Solid Showing   (the guess above, now captured)
    710: 7007,    # Defeat          (the guess above, now captured)
    713: 7014,    # Get Well Soon!
    714: 7015,    # Don't Overdo It!
    715: 7017,    # Extra Training
    717: 7016,    # At the Infirmary
    718: 7010,    # All Refreshed
    719: 7022,    # Singing Under the Bright, Blue Sky
    720: 7020,    # Just an Acupuncturist, No Worries!
    722: 7023,    # Migraine Blues
    726: 6002,    # Claw Machine!
    731: 6007,    # Claw Machine: Success!
    732: 7044, 733: 7045,   # Inspiration -- YEAR 2; year 3 has its own ids,
                            # see _SHARED_BEAT_EVENT_IDS_YEAR3
    508: 7003,    # Race Fatigue
    509: 7004,    # After Repeated Races...
    522: 7002,    # Skin Troubles
    # --- outings genuinely DO use the generic 6000 (702/703/704 all captured)
    702: 6000, 703: 6000, 704: 6000,
}
_SHARED_BEAT_EVENT_IDS = {k: v for k, v in _SHARED_BEAT_EVENT_IDS.items()
                          if v is not None}
# Nothing is inferred any more: Fan Letter (507) was the one guess in here --
# reasoned from the trainee "special event" family 500/506/510/511/520 all
# being captured as 10000 -- and the 20260905 captures show it is really
# 10001, so it moved into the factual table above. Kept as an empty hook so
# a future un-captured id has an honest place to live.
_SHARED_BEAT_EVENT_IDS_INFERRED: dict = {}


# A beat that can fire in BOTH the Classic and the Senior year does not reuse
# one id -- the second occurrence has its own. Captured for the 'Inspiration'
# pair in all three real 20260905 sessions, with no exceptions and no overlap:
# suffix 732 goes out as 7044 on turn 31 and 7046 on turn 55, suffix 733 as
# 7045 then 7047, always the same story_id. The bot logs agree independently --
# 7040/7044/7045 on turn 31 in 37 of 37 real Grand Live careers, 7041/7046/7047
# on turn 55 in 39 of 39. Turn 49 is the Senior-year boundary, so that is the
# split. (7040/7041 are the third member of the same trio; the suffix that
# produces them is not in any capture, so they are not mapped here.)
_SHARED_BEAT_EVENT_IDS_YEAR3 = {732: 7046, 733: 7047}
_YEAR3_FIRST_TURN = 49


def _shared_beat_event_id(suffix: int, turn=None) -> int:
    """The official event_id for a per-trainee story, or the generic
    chara-event id when nothing pins it down. The client keys its special
    presentation off event_id, so serving 6000 for everything is what makes
    these play as plain events with no staging.

    `turn` only matters for the handful of beats that fire once per year and
    change id on the second firing (_SHARED_BEAT_EVENT_IDS_YEAR3); callers that
    cannot see a turn get the year-2 id, which is what they served before.

    A beat outside the shared table gets the generic id: the trainee's own
    chronological-chain beats have real per-trainee ids of their own, but the
    formula behind them is not solved -- see the write-up above
    event_engine.wire_story_id."""
    if suffix in _SHARED_BEAT_EVENT_IDS:
        try:
            late = turn is not None and int(turn) >= _YEAR3_FIRST_TURN
        except (TypeError, ValueError):
            late = False
        if late and suffix in _SHARED_BEAT_EVENT_IDS_YEAR3:
            return _SHARED_BEAT_EVENT_IDS_YEAR3[suffix]
        return _SHARED_BEAT_EVENT_IDS[suffix]
    return _SHARED_BEAT_EVENT_IDS_INFERRED.get(suffix, event_engine.CHARA_EVENT_ID)


# MEASURED (bot_logs): Extra Training fires on 5.93% of successful non-camp
# trainings through turn 72 (2,648/44,625, which matches _EXTRA_TRAINING_CHANCE).
# It fires on 0 of 5,183 successful trainings in turns 73-78, and on 0 of
# 13,088 at camp.
_EXTRA_TRAINING_LAST_TURN = 72


def _maybe_fire_shared_beat(full_state: dict, career_state: dict, response: dict,
                            suffix: int, chance: float, context: dict | None = None,
                            once_per_career: bool = True) -> bool:
    """Roll for one of the SHARED beats that lives outside the random band
    (Extra Training 715, the acupuncturist 720 -- both 7xx, which
    _chara_random_stories does not scan) and queue it if it hits.

    By default each fires at most once per career, and is skipped when master
    has no story row for this trainee or when the engine has no values for it.
    once_per_career=False is for beats the real game repeats: Extra Training
    fires 0-6 times per real career (bot_logs, 1,806 full careers: 0:480
    1:588 2:416 3:219 4:71 5:24 6:8), which fits an uncapped per-training roll.
    The acupuncturist really is once per career (0:1641 1:165)."""
    if random.random() >= chance:
        return False
    chara_info = career_state["data"]["chara_info"]
    card_id = chara_info.get("card_id") or 0
    chara = card_id // 100
    if not chara:
        return False
    sid = 500000000 + chara * 1000 + suffix
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    if once_per_career and sid in fired:
        return False
    if not master_data.query_one(
            "SELECT story_id FROM single_mode_story_data WHERE story_id=?", (sid,)):
        return False
    # scenario_id -- Extra Training's top choice also carries a friendship
    # gain with the scenario's own companion NPC (real capture/screenshot:
    # "Friendship with Etsuko Otonashi +5", confirmed as the base scenario's
    # Reporter -- single_mode_events.NPC_UNLOCKS target 103/chara 9003). Only
    # scenario 1 (base/URA) is confirmed; other scenarios' equivalent
    # companion (if any) isn't verified, so seasonal_event only adds the bond
    # for scenario_id == 1 and flags the rest as unconfirmed rather than
    # guessing a name/chara_id.
    context = dict(context or {}, scenario_id=chara_info.get("scenario_id"))
    ev = event_engine.shared_event(suffix, card_id, context)
    if not ev or not ev.get("choices"):
        return False
    row = master_data.query_one(
        'SELECT text FROM text_data WHERE category=181 AND "index"=?', (sid,))
    event_id = _shared_beat_event_id(suffix)
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=chara,
        support_card_id=0, play_timing=1))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(fired) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_id, "story_id": sid,
        "source": "chara", "source_id": card_id,
        "title": (row["text"] if row else ""), "trainee_card_id": card_id,
        # CARRY the synthesized event: these beats are in no cache, so a
        # re-resolve at commit time finds nothing and pays nothing. Extra
        # Training can't be rebuilt without `context` either.
        "event": ev})
    return True


def _next_outing_event(full_state: dict, chara_info: dict):
    """The trainee's next un-fired outing/story event (story 50<chara>70x), in
    chain order. Returns (event, story_id, chara_id, title) or None.

    Dead-ended by a `chain_end` outcome exactly like a support card's chain --
    trainee chains carry `ee` too (105101/105102 'catchingtheeveningblooms')."""
    fired = set(full_state.get(event_engine.FIRED_EVENTS_KEY, []))
    card_id = chara_info.get("card_id") or 0
    if event_engine.chain_ended(full_state, f"chara:{int(card_id)}"):
        return None
    chara = card_id // 100
    for sid, title in event_engine.chara_outing_chain(card_id, chara):
        if sid not in fired:
            ev = event_engine.resolve(sid, title, card_id)
            if ev:
                return ev, sid, chara, title
    return None


def _maybe_fire_outing_event(full_state: dict, career_state: dict, response: dict) -> bool:
    """On recreation, sometimes queue the trainee's next outing/date event (6000),
    served from the event engine. Chains behind anything already queued this
    turn (see _queue_turn_event). Returns whether it fired."""
    if random.random() >= 0.5:
        return False
    chara_info = career_state["data"]["chara_info"]
    pick = _next_outing_event(full_state, chara_info)
    if not pick:
        return False
    ev, sid, chara, title = pick
    _queue_turn_event(response, full_state, event_engine.career_event_entry(
        ev, event_engine.CHARA_EVENT_ID, sid, chara_id=chara,
        support_card_id=0, play_timing=6))
    full_state[event_engine.FIRED_EVENTS_KEY] = list(
        full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    _push_career_ctx(full_state, {
        "event_id": event_engine.CHARA_EVENT_ID, "story_id": sid,
        "source": "chara", "source_id": chara_info.get("card_id"), "title": title,
        "trainee_card_id": chara_info.get("card_id")})
    return True


CAREER_CTX_QUEUE_KEY = "career_event_ctx_queue"


def _event_source_card(ctx: dict | None):
    """The SUPPORT CARD an event belongs to, or None. Only a card's own events
    get its event-recovery/event-effect bonuses (see event_engine.event_boost).
    Support-card chains, pal first-meetings and outings all carry
    source 'support' with the card id; 'chara'/'seasonal' source_ids are
    TRAINEE card ids and 'inline' has none, so those must return None rather
    than be looked up in the support tables."""
    if not ctx or ctx.get("source") != "support":
        return None
    return ctx.get("source_id")


def _push_career_ctx(full_state: dict, ctx: dict) -> None:
    """Register a dynamic event's context.

    A turn can queue SEVERAL of these (a seasonal beat AND a support
    coincidence AND a pal event...), but they used to share one slot, so
    whichever fired last silently replaced the others. The client then previewed
    and committed an event whose context no longer matched: every option showed
    'None' and awarded nothing (live-reported on New Year's Resolutions, but it
    hit any event unlucky enough to be queued before another one). They're now
    kept as a queue and looked up by the event_id the client actually asks
    about."""
    queue = full_state.setdefault(CAREER_CTX_QUEUE_KEY, [])
    queue.append(ctx)
    full_state[event_engine.CAREER_EVENT_CTX_KEY] = ctx   # newest, for compat


def _career_ctx_for(full_state: dict, event_id) -> dict:
    """The queued context for the event the client is asking about (oldest
    match wins -- events are played in the order they were queued)."""
    for ctx in full_state.get(CAREER_CTX_QUEUE_KEY) or ():
        if ctx.get("event_id") == event_id:
            return ctx
    legacy = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    return legacy if legacy.get("event_id") == event_id else {}


def _career_ctx_pending(full_state: dict, event_id) -> bool:
    """Whether a ctx is already queued under this SAME wire event_id.

    The wire event_id is a shared TYPE marker, not a per-event id (10002
    means "some support chain event", not "this one") -- so _career_ctx_for's
    oldest-match lookup only ever returns the RIGHT event if at most one of
    a given type is outstanding at a time. A producer firing a second one
    while the first is still unresolved (e.g. queued behind something else
    in EXTRA_EVENTS_KEY) doesn't queue two things the client can pick
    between: it makes the SECOND event's story get shown while the FIRST
    event's ctx -- the oldest match -- is what get_choice_reward/commit
    actually resolve, silently handing one card's rewards to a different
    card's event (live-reported: "Fortune Favors the Friends!"'s screen
    showing Super Creek's "Have a Second Helping!" payout). Producers that
    share a wire id across many real events must check this before firing
    and skip (as if the rate roll simply missed) rather than collide."""
    return any(c.get("event_id") == event_id
              for c in full_state.get(CAREER_CTX_QUEUE_KEY) or ())


def _drop_career_ctx(full_state: dict, ctx: dict) -> None:
    # Matched by (event_id, story_id), NOT `is`. `ctx` here almost always came
    # from _career_ctx_for, which reads it back out of full_state AFTER a
    # state_store save/load round-trip -- JSON deserializes every occurrence
    # into its OWN dict object, so the one in CAREER_CTX_QUEUE_KEY and the one
    # in CAREER_EVENT_CTX_KEY are never the same object even when they hold
    # identical content. An `is` compare therefore never matches the legacy
    # slot once state has been persisted even once, so it never gets cleared:
    # active_career_event stays pointed at a card that already resolved and
    # paid out, forever (live-reported: "Grand Finale" stuck as the active
    # event long after its payout had already landed).
    identity = (ctx.get("event_id"), ctx.get("story_id"))
    queue = [c for c in (full_state.get(CAREER_CTX_QUEUE_KEY) or ())
             if (c.get("event_id"), c.get("story_id")) != identity]
    if queue:
        full_state[CAREER_CTX_QUEUE_KEY] = queue
    else:
        full_state.pop(CAREER_CTX_QUEUE_KEY, None)
    legacy = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    if (legacy.get("event_id"), legacy.get("story_id")) == identity:
        full_state.pop(event_engine.CAREER_EVENT_CTX_KEY, None)


def _career_event_from_ctx(full_state: dict, event_id=None):
    """(event, ctx) for a queued dynamic career event, resolved fresh from the
    engine by its stored (source, source_id, title). With event_id given, picks
    the context for THAT event rather than whatever was queued last."""
    if event_id is not None:
        ctx = _career_ctx_for(full_state, event_id)
    else:
        ctx = full_state.get(event_engine.CAREER_EVENT_CTX_KEY) or {}
    if not ctx:
        return None, ctx
    # A ctx that CARRIES its own event wins outright. Two kinds need this:
    #   - 'inline' events (the post-race reward), whose numbers depend on the
    #     race just run and so can't be looked up by title;
    #   - SYNTHESIZED shared beats (Dance Lesson, Fan Letter, Extra Training,
    #     the acupuncturist). These exist in NO event cache -- that is the whole
    #     reason event_engine.shared_event() builds them -- so re-resolving them
    #     by (source, title) returns None and the event awarded NOTHING on
    #     commit while still rendering its choices (live-reported: "Dance Lesson
    #     awards nothing?"). Extra Training additionally can't be rebuilt at all
    #     without the facility context, which the ctx never carried.
    # Serving and committing the identical object also removes any chance of the
    # two drifting apart.
    if ctx.get("event"):
        return ctx["event"], ctx
    if ctx.get("source") == "inline":
        return ctx.get("event"), ctx
    if ctx.get("source"):
        return event_engine.resolve_source(ctx["source"], ctx.get("source_id"), ctx.get("title")), ctx
    sid = ctx.get("story_id")   # legacy ctx (pre-source)
    if not sid:
        return None, ctx
    return event_engine.resolve(sid, event_engine.event_title(sid), ctx.get("trainee_card_id")), ctx


def _announce_due_passive_goals(response: dict, full_state: dict, career_state: dict,
                                acted_turn=None) -> None:
    """Fire the trainee's own 'Goal Achieved' event for any PASSIVE goal (ct=3
    fan threshold / ct=2 grade tally) that is satisfied and due on the turn
    just acted on.

    Called from BOTH turn paths. It used to live only in handle_exec_command,
    but a turn spent RACING never calls exec_command (race_entry -> ... ->
    race_out advances the turn itself), and racing on the deadline turn is
    exactly how these goals tend to get cleared: a fan threshold is most often
    crossed by the race that pays the fans, and four of the five grade tallies
    have a qualifying race available on the deadline turn itself (Oguri Cap and
    Agnes Digital turn 60, Tamamo Cross 49, Matikanetannhauser 24). Clearing a
    goal that way announced nothing and paid neither the +3 stats nor the +24
    SP. `acted_turn` is the turn the player acted on -- exec_command's
    current_turn, or the race's own turn on the race path.

    A satisfied fan-threshold / grade-tally
    goal fires the trainee's own 'Goal Achieved' event, CHAINED BEHIND
    whatever this turn is already showing (the rest/training event plays
    first, then the goal event) -- see _goal_achieved_event for the verbatim
    captured wire shape.
   
    TIMING, and this is subtle: the goal announces at the END of its deadline
    turn -- after the player takes their action ON that turn -- NOT on
    arriving at it. Official capture 20260728_151855:
        tx 0083  check_event   arrive at turn 24        -> no event
        tx 0084  exec_command  current_turn=24 (rest)   -> the rest event
        tx 0085  check_event   still turn 24, ps 5      -> Goal Achieved
    so the gate is the turn being ACTED ON, which is the request's
    current_turn. Using chara_info['turn'] here fires a turn early, because
    our exec_command advances the turn within this same response while the
    official server advances it one step later in the chain (live-reported:
    "it plays 1 turn early"). Fans were already over the line at turn 23 in
    that capture and it still waited for 24, so crossing-turn is wrong too
    (#12 fired early, #23 fired four turns late).
    """
    try:
        ci_goal = career_state["data"]["chara_info"]
        history = career_state["data"].get("race_history", [])
        turn_acted = acted_turn
        if turn_acted is None:
            # Both paths advance the turn inside the same response, so the
            # turn just ACTED on is the one before the live one.
            turn_acted = (ci_goal.get("turn") or 1) - 1
        goals_all = _route_all_goals(tuple(ci_goal.get("route_race_id_array") or ()))
        due = [g for g in goals_all
               if g[2] in (2, 3) and g[0] == turn_acted
               and _goal_is_cleared(g, ci_goal, history, goals_all)
               and not _goal_announced(full_state, g[1])]
        if due:
            entry = _goal_achieved_event(full_state, ci_goal, due[-1][1])
            if entry:
                _record_goal_announced(full_state, due[-1][1])
                # Clearing a goal PAYS -- capture-measured across the whole
                # session: the only all-five stat bumps in it both follow a
                # show_clear event, and this one paid exactly +3 to all five
                # and +24 SP when the event resolved (turn 24 -> 25 in tx
                # 0085 -> 0086). Bound to the event via the ctx queue so it
                # lands on this screen and not a later one (the #17 rule).
                _push_career_ctx(full_state, {
                    "event_id": entry["event_id"], "story_id": entry["story_id"],
                    "source": "inline", "source_id": 0, "title": "Goal Achieved",
                    "trainee_card_id": ci_goal.get("card_id"),
                    "event": {"choices": [{"effects": [
                        {"type": "all_stats", "value": f"+{_GOAL_CLEAR_STATS}"},
                        {"type": "skill_points", "value": f"+{_GOAL_CLEAR_SP}"}]}]}})
                # FRONT, but only over a SCENARIO event: McQueen's turn-24 goal
                # shares a beat with the turn-25 New Year's event and must play
                # BEFORE it (user-confirmed, and the capture's order too);
                # appended, it sat two events deep and read as never playing.
                #
                # That capture is goal-vs-META-CUTSCENE. It is NOT evidence for
                # goal-vs-the-player's-OWN-CHOSEN-ACTION -- a recreation/support
                # coincidence the player just triggered (live-reported softlock,
                # 2026-08-26: an unconditional front=True demoted a just-queued
                # Light Hello recreation event behind the goal banner, and the
                # recreation was never seen again this turn). Front-jump only
                # when nothing is shown yet, or what IS shown is itself a
                # SCENARIO_SCHEDULE cutscene (the McQueen case) -- never over
                # the turn's own action.
                # NEXT, not last. Appending put it at the TAIL of legacy EXTRA,
                # which _drain_pending serves AFTER the unified pipeline -- so on
                # the Grand Live concert turn it came out behind the whole
                # concert chain AND behind New Year's, dead last (live-reported
                # 2026-09-03; this run's 0027, after New Year's at 0024). The
                # capture is unambiguous that it leads: "the rest event plays
                # first and the goal event chains behind it" (20260728_151855,
                # tx 0084 action -> tx 0085 Goal Achieved -> tx 0086 New
                # Year's). Park it in its own slot, which _drain_pending serves
                # ahead of both queues, so it is always the VERY NEXT event
                # after whatever the turn's own action is showing.
                shown_now = response["data"].get("unchecked_event_array") or []
                shown_id = shown_now[0].get("event_id") if shown_now else None
                is_scenario_shown = shown_id is not None and any(
                    shown_id in evs for evs in single_mode_events.SCENARIO_SCHEDULE.values())
                if not shown_now or is_scenario_shown:
                    _queue_turn_event(response, full_state, entry, front=True)
                else:
                    full_state[GOAL_EVENT_KEY] = entry
            elif response["data"].get("unchecked_event_array"):
                # No 'Goal Achieved' story for this trainee (only chara 1002's
                # fan goal is in that position) -- fall back to the banner stamp.
                _mark_goal_progress(response, ci_goal, history, due[-1][1], full_state)
    except Exception:
        log.exception("passive-goal announcement failed; skipping")


def handle_exec_command(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id)
    if full_state is None or STATE_KEY not in full_state:
        raise LookupError("No active career for this viewer -- call start first")

    career_state = full_state[STATE_KEY]

    # REST EVENT RESOLUTION via exec_command (event_id=7009/7010/7011, no
    # command_id): some clients resolve the rest event here rather than via
    # check_event. Delegate so the deferred energy is applied as the event outcome
    # -- NOT simulated as a training (which would wrongly drain vital).
    if payload.get("event_id") in _REST_EVENT_IDS and payload.get("command_id") is None:
        return handle_ura_check_event(payload)

    # DUEL preview: before the player picks, the client asks exec_command with the
    # duel event id (no command_id) for every choice's win/lose reward. Return the
    # choice_reward_array only (no chara_info change -- nothing is applied yet).
    if payload.get("event_id") == single_mode_events.DUEL_EVENT_ID:
        ctx = full_state.get(single_mode_events.DUEL_CTX_KEY) or {}
        options = ctx.get("stat_options") or single_mode_events.duel_stat_options(
            full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1))
        return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                "data": {"choice_reward_array": single_mode_events.duel_preview(options)}}

    # DYNAMIC CAREER EVENT preview: exec_command(event_id, no command_id) asks for
    # each choice's rewards -- serve them from the engine, apply nothing (the
    # commit is check_event(event_id, choice_number)).
    if (payload.get("event_id") in event_engine.CAREER_EVENT_IDS
            or _career_ctx_for(full_state, payload.get("event_id"))):
        ev, ctx = _career_event_from_ctx(full_state, payload.get("event_id"))
        if ev and ctx.get("event_id") == payload.get("event_id"):
            ci = career_state["data"].get("chara_info")
            dcid = event_engine.source_default_chara(ctx.get("source"), ctx.get("source_id"))
            partner, chances = _pal_unlock_preview_args(full_state)
            return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
                    "data": {"choice_reward_array": event_engine.choice_reward_array(
                        ev, ci, dcid, partner, chances,
                        source_card_id=_event_source_card(ctx))}}

    # STALE CLIENT CACHE. Everything past this point ACTS on the run -- it
    # banks stat gains, burns energy, fires this turn's schedule and advances
    # the turn. The preview branches above are read-only and answer whatever
    # turn they are asked about; this does not. See _client_turn_conflict.
    conflict = _client_turn_conflict(payload, full_state,
                                     career_state["data"].get("chara_info"),
                                     allow_played_turn=False)
    if conflict is not None:
        return _stale_turn_refusal(viewer_id, "single_mode/exec_command", conflict,
                                   career_state["data"].get("chara_info"))

    # This turn's scheduled scenario events (Director turn 3, Happy Meek turn 4).
    # If one unlocks an NPC, register it NOW (before building the next turn's
    # command_info) so the NPC appears in training from next turn.
    # ...but each scheduled EVENT exactly once per career. Two failure modes,
    # both live-reported repeatedly:
    #  * no dedupe at all -> a repeated turn (reload / backed-out race entry)
    #    re-queued the whole chain and Meek "unlocked" again at a random later
    #    moment;
    #  * marking the turn fired at QUEUE time -> a reload between queueing and
    #    DISPLAY lost the cutscene forever (the unlock then "happened" invisibly
    #    and the client announced 'X will now appear in training' off whatever
    #    event came next).
    # So: skip events already resolved (marked at check_event RESOLUTION) or
    # still sitting in the pending/extra queues; everything else may queue
    # again -- re-serving an undisplayed cutscene is exactly what a reload
    # needs.
    resolved = set(full_state.get(SCENARIO_EVENTS_DONE_KEY) or [])
    in_flight = set(full_state.get(single_mode_events.PENDING_EVENTS_KEY) or [])
    in_flight |= {e.get("event_id") for e in
                  (full_state.get(single_mode_events.EXTRA_EVENTS_KEY) or [])}
    cur_turn = payload.get("current_turn")
    # SCENARIO_SCHEDULE is URA's chain (1013 turn 2, Director 1014 turn 3,
    # 1015 + Happy Meek's unlock 102005 turn 4). NONE of it exists in Grand
    # Live -- verified in the capture: 1013/1014/1015/1016/102005/102001 never
    # fire and Happy Meek has no evaluation row at all. Serving it there put
    # her in a scenario she isn't in (live-reported). Grand Live's own chain is
    # _GRAND_LIVE_EVENTS, queued further down.
    if not scenarios.for_chara(career_state["data"].get("chara_info")).uses_fixed_schedule:
        scheduled = []
    else:
        scheduled = [e for e in single_mode_events.SCENARIO_SCHEDULE.get(cur_turn, [])
                     if e not in resolved and e not in in_flight]
    # Registration deliberately does NOT happen here anymore -- the unlock
    # event's RESOLUTION registers the NPC (see handle_ura_check_event), so the
    # cutscene's own response carries the new evaluation row and the client
    # announces the join. Placements below use only ALREADY-unlocked NPCs,
    # which matches the real wire (is_appear flips the turn after resolution).
    unlocked = [list(x) for x in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
    unlocked_target_ids = [n[0] for n in unlocked]

    # Always simulate against the PLAYER's live chara_info. (The old exact-match
    # fixture replay overwrote chara_info with the RECORDED run's chara -- e.g.
    # command 101/turn 1 IS captured -- so the player's stats jumped to Summer
    # Maru's recorded values ("way too much of all stats"), and the replayed
    # response carried the recorded unchecked_event_array/team_data the client
    # then hung trying to play. Simulating keeps stats correct + response clean.)
    chara_before = career_state["data"].get("chara_info") or {}
    response = _simulate_exec_command(
        career_state, payload, unlocked_npcs=unlocked_target_ids,
        training_bonus=_training_bonus(full_state, chara_before),
        friendship_bonus=_friendship_bonus(full_state, chara_before),
        specialty_bonus=_specialty_bonus(full_state, chara_before),
        full_state=full_state)
    full_state[single_mode_events.UNLOCKED_NPCS_KEY] = unlocked

    # GRAND LIVE: bank the performance tokens for the facility just trained.
    # Read back from the SAME preview the client rendered (the cached per-turn
    # roll in the scenario's command_info), so the tokens the player banks are the ones
    # the training screen promised -- the identical what-you-see-is-what-you-get
    # rule the stat gains already follow. Rest/outings/races pay nothing.
    # A FAILED training pays nothing either -- proven 4/4 on the real server
    # (Icarus Global captures); we were banking the full preview regardless.
    if not (response.get("data") or {}).get("_training_failed"):
        scenarios.for_chara(chara_before).award_training_gains(
            full_state, chara_before, payload)

    # DUEL: training is exec_command(command_id) for THIS client (not check_event).
    # If the player trained Happy Meek's facility on a duel turn, queue the
    # 3-choice duel event -- the client plays it, previews via exec_command(event),
    # then commits via get_choice_reward. (Still suppressed outright on an unlock
    # turn -- she shouldn't duel the same turn she's introduced.)
    cmd = payload.get("command_id")
    # Routed through the gated wrapper, NOT single_mode_events directly -- this
    # is the duel trigger, and calling past the gate is how Happy Meek could
    # still ambush a Grand Live career even with her marker suppressed.
    meek_fac = _happy_meek_facility(
        payload.get("current_turn"), full_state,
        career_state["data"].get("chara_info"))
    if not scheduled and cmd is not None and meek_fac is not None and cmd == meek_fac:
        vlevel = full_state.get(single_mode_events.VERSUS_LEVEL_KEY, 1)
        options = single_mode_events.duel_stat_options(vlevel)
        duel_ci = career_state["data"]["chara_info"]
        entry = single_mode_events.duel_event_entry(options, duel_ci, vlevel)
        # duel_stat_options(vlevel) is a pure function of vlevel (no
        # randomness), so the get_choice_reward preview branch can just
        # recompute it the same way -- no ctx needed there. Event_id/story
        # are fixed and a duel repeats across the career, so once_key must
        # vary per occurrence (turn).
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=single_mode_events.DUEL_EVENT_ID, story_id=entry.get("story_id"),
            raw=entry, once_key=f"duel:{duel_ci.get('turn')}",
            priority=career_events.PRIO_SPECIAL, resolver="duel", source="duel",
            payload={"stat_options": options, "versus_level": vlevel}))

    # TRAINING FAILURE (non-wit): queue the Top/Bottom infirmary event and stash
    # its context; the penalty is applied when the player commits via
    # get_choice_reward. Chains behind a scenario/duel event already showing
    # rather than replacing it.
    rdata = response.get("data", {})
    training_failed = bool(rdata.get("_training_failed"))
    if rdata.pop("_training_failed", False):
        worst = rdata.pop("_training_worst", False)
        fail_ci = career_state["data"]["chara_info"]
        player_chara = (fail_ci.get("card_id") or 0) // 100
        eid = single_mode_events.WORST_FAIL_EVENT if worst else single_mode_events.NORMAL_FAIL_EVENT
        stat = _COMMAND_TRAINED_STAT.get(cmd)
        # FAIL_CTX_KEY is kept (not just Event.payload) because the
        # get_choice_reward PREVIEW endpoint (a separate request, before the
        # commit) has no other way to recover which stat this failure is
        # for -- its own fallback defaults to "speed" unconditionally, which
        # would mis-preview every non-speed facility's failure.
        full_state[single_mode_events.FAIL_CTX_KEY] = {
            "event_id": eid, "worst": worst, "stat": stat}
        entry = single_mode_events.failure_event_entry(eid, player_chara, worst)
        # Only 2 possible event_ids, reused every failure -- once_key must
        # vary per occurrence (turn) or emit() would dedupe a later failure
        # of the same severity away as "already fired".
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=eid, story_id=entry.get("story_id"), raw=entry,
            once_key=f"training_failure:{fail_ci.get('turn')}:{eid}",
            priority=career_events.PRIO_SPECIAL, resolver="training_failure",
            source="training_failure", payload={"worst": worst, "stat": stat}))
    else:
        rdata.pop("_training_worst", None)

    # SLACKER no-show -> her 'Slacking Off' story, so the turn visibly goes to
    # the condition instead of the training just producing nothing.
    #
    # FIRST, and at the HEAD of the queue: the player clicked a facility and
    # got no training, so this is the response to that click and nothing may
    # play in front of it (user-supplied: "make it immediately play, no event
    # that could interrupt in between"). Hence _queue_turn_event(front=True)
    # rather than the career_events pipeline -- the pipeline is drained after
    # the PENDING npc-unlock jump, so an unlock cutscene queued this same turn
    # would have gone first -- and hence its position ABOVE every other
    # producer in this handler.
    #
    # Ack-only: the energy the wasted turn costs is already applied inline by
    # the training builder, and paying it again here would double-charge.
    if rdata.pop("_slacked", False):
        ci_slack = career_state["data"]["chara_info"]
        chara_slack = (ci_slack.get("card_id") or 0) // 100
        suffix = conditions.CONDITION_STORY_SUFFIX[conditions.SLACKER]
        story = 500000000 + chara_slack * 1000 + suffix
        # 66 of the 83 titles have a real story row behind them. The 17 that
        # do not (Shinko Windy, Twin Turbo, Tanino Gimlet, Katsuragi Ace, ...)
        # are all UNRELEASED trainees -- user-confirmed 2026-09-09 -- so this
        # guard never fires for anyone playable and there is nothing to chase.
        # It stays because a title without a story row is what softlocked the
        # debut twice: serving one tells the client to play a missing asset.
        if chara_slack and _story_exists(story):
            _queue_turn_event(response, full_state, _scenario_event_entry(
                _shared_beat_event_id(suffix, ci_slack.get("turn")), story,
                play_timing=6, choices=[_ack_choice()], chara_id=chara_slack),
                front=True)

    # REST flavor event: Rest plays a visible cutscene (Well-Rested! / Sleep
    # Deprived / All Refreshed). The energy is NOT applied yet -- it's the event's
    # OUTCOME, applied (and animated) when the event resolves. The ctx (and thus
    # the energy roll) must be stashed UNCONDITIONALLY -- previously this was
    # gated on "nothing else queued yet", so resting on a turn that also had a
    # scenario event scheduled silently discarded the rest roll entirely (not
    # just hid it -- REST_CTX_KEY was never set, so the energy gain vanished).
    # Now it always chains in, in order, behind whatever's already showing.
    rest_outcome = rdata.pop("_rest_outcome", None)
    if rest_outcome:
        rest_ci = career_state["data"]["chara_info"]
        player_chara = (rest_ci.get("card_id") or 0) // 100
        full_state[REST_CTX_KEY] = rest_outcome
        rest_eid = rest_outcome["event_id"]
        entry = _rest_event_entry(rest_eid, player_chara)
        # Only 3 possible event_ids (one per energy tier), reused every rest
        # -- once_key must vary per occurrence (turn) or emit() would dedupe
        # every later rest that rolls the same tier away as "already fired".
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=rest_eid, story_id=entry.get("story_id"), raw=entry,
            once_key=f"rest:{rest_ci.get('turn')}:{rest_eid}",
            priority=career_events.PRIO_SPECIAL, resolver="rest", source="rest"))

    # SKILL HINT REVEAL: one or more cards that showed the '!' icon
    # (tips_event_partner_array) this turn actually procced -- queue each
    # reveal in turn (_build_hint_reveals already computed which skill/level
    # per card, at exec_command PREVIEW time inside _simulate_exec_command).
    # Each reveal's effects are appended to HINT_REVEAL_CTX_KEY's pending
    # queue in the SAME order they're queued via _queue_turn_event, so
    # whichever one is currently on display always matches pending[0] when
    # the player commits via check_event (HINT_REVEAL_CTX_KEY branch), same
    # preview/commit split as every other interactive event here.
    hint_reveals = rdata.pop("_hint_reveals", None) or []
    if hint_reveals:
        # Every reveal this turn shares the same synthetic event_id AND real
        # story_id (both fixed per trainee), so each needs its OWN once_key
        # or career_events.emit() would dedupe the 2nd+ one away as "already
        # queued" -- turn+index is unique per occurrence without blocking a
        # LATER turn's reveal from the same card (a static per-card key would
        # wrongly do that). Resolution needs no custom resolver: find()/drop()
        # match on the currently-ACTIVE event (event_id alone, since only one
        # can be active at a time), and the effects ride on the Event's own
        # choices, applied by career_events.resolve()'s generic applier --
        # same mechanism as the Raffle migration.
        hr_turn = career_state["data"]["chara_info"].get("turn")
        for i, (hint_entry, hint_effects) in enumerate(hint_reveals):
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=hint_entry.get("event_id"),
                story_id=hint_entry.get("story_id"),
                raw=hint_entry, choices=[career_events.Choice(effects=hint_effects)],
                once_key=f"hint_reveal:{hr_turn}:{i}",
                priority=career_events.PRIO_SPECIAL, source="hint_reveal"))

    # FACILITY LEVEL-UP: the Director's 'Training Level Up!' event (400000005,
    # slot 1024 -- both verbatim from the user's own fresh real capture, txn
    # 0028), fired the training that completed the 4th successful session.
    # Pure story banner, no reward (not in EVENT_EFFECTS) -- no resolver
    # needed. Fixed event_id/story repeat across facilities/turns, so
    # once_key must vary per occurrence (turn) same as the other fixed-id
    # families above.
    leveled_command = rdata.pop("_facility_leveled", False)
    if leveled_command:
        lvl_event, lvl_story = scenarios.for_chara(
            career_state["data"]["chara_info"]).facility_levelup_event(leveled_command)
        entry = _scenario_event_entry(lvl_event, lvl_story, play_timing=6,
                                      choices=[_ack_choice()])
        lvlup_turn = career_state["data"]["chara_info"].get("turn")
        _emit_turn_event(response, full_state, career_events.Event(
            event_id=lvl_event, story_id=lvl_story, raw=entry,
            once_key=f"facility_levelup:{lvlup_turn}",
            priority=career_events.PRIO_SPECIAL, source="facility_levelup"))

    # NPC APPRAISAL: training a facility the Director (102) or the Reporter
    # (103) is standing in sometimes fires her Appraisal -- the reward tier
    # follows the bond gauge (single_mode_evaluation thresholds); the Director
    # pays skill points, the Reporter pays the trained facility's own stat.
    # Applied at check_event resolution (APPRAISAL_CTX_KEY branch).
    npc_partners = rdata.pop("_npc_partners", None) or []
    trained_cmd = rdata.pop("_trained_command", None)
    # Popped unconditionally (a recreation turn never reaches the training
    # branch below, and a leftover handoff key must not ship in the response).
    pal_partners = rdata.pop("_pal_partners", None) or []
    # INFIRMARY visit -> the trainee's 7016 event; energy/cure apply on its ack.
    if rdata.pop("_infirmary", False):
        ci_inf = career_state["data"]["chara_info"]
        chara_inf = (ci_inf.get("card_id") or 0) // 100
        if chara_inf:
            story = 500000000 + chara_inf * 1000 + _INFIRMARY_STORY_SUFFIX
            entry = _scenario_event_entry(
                _INFIRMARY_EVENT_ID, story, play_timing=6,
                choices=[_ack_choice()], chara_id=chara_inf)
            _emit_turn_event(response, full_state, career_events.Event(
                event_id=_INFIRMARY_EVENT_ID, story_id=story, raw=entry,
                priority=career_events.PRIO_SPECIAL, resolver="infirmary",
                source="infirmary"))
    if npc_partners and random.random() < _APPRAISAL_CHANCE:
        try:
            tid = random.choice(npc_partners)
            ci = career_state["data"]["chara_info"]
            bond = _bond_of(ci, tid)
            hit = single_mode_events.appraisal_for(
                tid, bond, scenarios.for_chara(ci).appraisal_events())
            if hit:
                a_event, amount = hit
                stat = None if tid == 102 else _COMMAND_TRAINED_STAT.get(trained_cmd, "speed")
                player_chara = (ci.get("card_id") or 0) // 100
                entry = single_mode_events.event_entry(a_event, player_chara)
                # a_event is one of a small fixed tier set, reused every time
                # this NPC's appraisal fires -- once_key must vary per
                # OCCURRENCE (turn) or emit() would dedupe every appraisal
                # after the run's first one away as "already fired".
                _emit_turn_event(response, full_state, career_events.Event(
                    event_id=a_event, story_id=entry.get("story_id"), raw=entry,
                    once_key=f"appraisal:{ci.get('turn')}:{a_event}",
                    priority=career_events.PRIO_SPECIAL, resolver="appraisal",
                    source="appraisal",
                    payload={"target_id": tid, "amount": amount, "stat": stat}))
        except Exception:
            log.exception("appraisal roll failed; skipping")

    # MID-RUN INSPIRATION: fires at the START of turns 31/55 -- i.e. queued on
    # the action that ADVANCES into them (the race-turn path has its own hook
    # in handle_ura_race_out). Guarded like the coincidence block below: a roll
    # failure must never break the training response itself.
    # THE CONCERT TURN. Everything from here to the end of the unique-skill
    # block queues the events of the turn being arrived AT -- which, on a Grand
    # Live concert turn, must not jump ahead of the concert. Reserve the display
    # slot for it (SCENARIO_SLOT_RESERVED_KEY) and hold the SERVED turn at the Live's
    # own turn for the whole chain.
    #
    # The hold used to start only when "Concert Begins" resolved, several
    # responses later -- so the client was told 24 -> 25 by this very response
    # and then snapped back to 24 mid-chain. It had already drawn turn 25's home
    # screen by then, and took a whole extra training turn (client-side, on a
    # turn we were reporting as 24) before honouring playing_state 10 and opening
    # the backstage screen: the reported "ghost turn" before the padlock. The
    # capture holds 24 across the concert turn's entire chain (messages 97-100),
    # which is what this reproduces. Served only -- the scenario's held_turn is
    # applied to the outgoing copy at the response chokepoint; the run's own
    # persisted turn keeps advancing.
    scenario_pending = False
    try:
        scenario_pending = _scenario_interstitial_pending(full_state, career_state,
                                                          payload)
        if scenario_pending:
            held = payload.get("current_turn")
            if held is None:
                held = (career_state["data"]["chara_info"].get("turn") or 1) - 1
            scenarios.for_chara(career_state["data"]["chara_info"]).begin_hold(
                full_state, int(held or 0))
    except Exception:
        log.exception("scenario turn-hold failed; skipping")

    with _scenario_slot_reserved(full_state, scenario_pending):
        try:
            _maybe_queue_inspiration(response, full_state, career_state, viewer_id,
                                     career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("inspiration roll failed; skipping this turn's event")

    # GOAL DEADLINE evaluation on the turn just arrived at (fan-entry /
    # fan-threshold / grade-tally) -- the real server fails the career HERE.
    career_failed = False
    try:
        career_failed = _maybe_fail_goal(
            response, full_state, career_state,
            career_state["data"]["chara_info"].get("turn"))
    except Exception:
        log.exception("goal evaluation failed; not failing the career")
    # career_failed is informational here: the actual suppression lives in
    # _queue_turn_event, which refuses every non-fail-chain event once
    # chara_info.state == 2. Every producer below runs AFTER this point, so
    # purging the queue inside _maybe_fail_goal is not enough on its own --
    # they would simply refill it.
    # New Year's lives here (turn 25 -- i.e. queued by the turn-24 action that
    # arrives at it, which is the concert turn). Reserved out of the display slot
    # so the concert leads; it plays once the post-live beats drain.
    with _scenario_slot_reserved(full_state, scenario_pending):
        try:
            _maybe_queue_chara_story(response, full_state, career_state,
                                     career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("chara story queue failed; skipping")
        # UNIQUE SKILL LEVEL-UP events (Valentine's Day / Fan Fest / Holiday Season)
        try:
            _maybe_queue_unique_level(response, full_state, career_state,
                                      career_state["data"]["chara_info"].get("turn"))
        except Exception:
            log.exception("unique skill level event queue failed; skipping")
    # DIRECTOR'S FAN randoms -- only once Akikawa's gauge is maxed
    try:
        _maybe_fire_director_fan(full_state, career_state, response)
    except Exception:
        log.exception("director's fan event failed; skipping")
    # NEGATIVE CONDITION procs (Night Owl etc.) get their own event
    try:
        _maybe_fire_condition_event(full_state, career_state, response)
    except Exception:
        log.exception("condition event failed; skipping")
    # Goal banner. A PASSIVE goal ('earn 3000 fans') clears on a turn that may
    # have queued nothing at all -- and the banner can only ride an event, so
    # with an empty queue there is nothing to animate and the goal silently
    # rolls over (live-reported twice). Force one of the trainee's own story
    # events out so the transition has a carrier, then stamp it.
    # PASSIVE-goal clear announcement: stamp show_clear=1 (a FLAG -- 2 means
    # abandoned!) onto an event that is ALREADY queued this turn. Never force
    # an event out for it (that popped the goal-summary screen early,
    # live-reported) -- if the turn queued nothing, the stamp simply waits for
    # a turn that did.
    # PASSIVE goals (fan thresholds / grade tallies) announce ONLY on their own
    # DEADLINE turn, never on whatever unrelated event happens to be queued
    # earlier or later. Both failure modes were live-reported: a fan goal
    # announced 6 turns early on a random Fukukitaru event (#12), and a race
    # goal's clear popping 4 turns LATE inside a Director event (#23). Race
    # goals announce on their OWN post-race story (the race_out path), never
    # here.
    # (the stamp itself runs LAST -- see _stamp_due_passive_goal below, after
    # this turn's coincidence event has been queued; stamping here would find
    # an empty array and silently skip.)

    # PAL/GROUP upkeep on the turn just arrived at: expire Pure Passion, mirror
    # unlock state onto evaluation_info_array (is_outing is what makes the
    # client offer the outing at all), and play a pending "outed with everyone"
    # finale.
    try:
        ci_now = career_state["data"]["chara_info"]
        # Scenario state that lives ON the persisted chara_info gets reasserted
        # first, before anything below reads it back (Scenario.persist_upkeep;
        # no-op for URA). A career saved while it was wrong heals here.
        scenarios.for_chara(ci_now).persist_upkeep(full_state, ci_now)
        passion_ended = _pure_passion_effects(full_state, ci_now, ci_now.get("turn"))
        _sync_pal_evaluation(full_state, ci_now)
        # PURE PASSION RAN OUT -> the group plays the story where they lose it
        # (pal_cards.pure_passion_end_story). The condition is already off
        # chara_effect_id_array by here, which is the right order: the buff
        # stops working on the turn it lapses and the cutscene explains why.
        for cond in passion_ended:
            end_card = pal_cards.pure_passion_card(cond)
            sid = pal_cards.pure_passion_end_story(end_card) if end_card else None
            if sid and _queue_pal_event(response, full_state, career_state,
                                        end_card, sid, "passion_end"):
                log.info("pure passion: condition %s expired, serving %s's "
                         "end story %s", cond, end_card, sid)
                break
        # Outing-unlock roll: every turn once met (Game.cpp reference:
        # handleFriendUnlock is rolled per turn while stage ==
        # beforeUnlockOutgoing, at a higher rate once bond >= 60).
        if not (response["data"].get("unchecked_event_array") or []):
            for pos, card_id in _deck_pal_cards(ci_now):
                entry = _pal_card_entry(full_state, card_id)
                if not entry.get("met") or entry.get("unlocked") \
                        or full_state.get(PAL_EVENT_CTX_KEY):
                    continue
                bond = _bond_of(ci_now, pos)
                chance = (_PAL_UNLOCK_CHANCE_HIGH if bond >= 60
                          else _PAL_UNLOCK_CHANCE_LOW)
                if random.random() < chance:
                    sid = pal_cards.unlock_story(card_id)
                    if sid and _queue_pal_event(response, full_state, career_state,
                                                card_id, sid, "unlock"):
                        break
        response["data"]["chara_info"] = copy.deepcopy(ci_now)
    except Exception:
        log.exception("pal/group upkeep failed; skipping")

    # DYNAMIC SUPPORT-CARD / OUTING EVENT ("coincidence"): the real game can fire
    # one of these after ANY action (training, rest, recreation, a duel...), not
    # just training -- and it chains behind whatever else this turn already
    # queued rather than requiring an empty slot. Guarded so a firing hiccup
    # can never break the whole request (worst case: no coincidence this turn --
    # never touches whatever's already queued).
    try:
        # SECRET events are checked on EVERY command, ahead of the coincidence
        # roll. They are earned (win the Satsuki Sho in Classic, take the Triple
        # Crown), so the turn the condition comes true is the turn they are due.
        #
        # They used to sit inside the training-only branch below, as one link in
        # an elif chain behind the acupuncture and extra-training rolls -- so a
        # secret event could only fire if the player happened to TRAIN that turn
        # and both rolls lost. Resting, recreation, an outing or a race skipped
        # the check entirely. Live-caught 2026-09-04 with Agnes Tachyon: her
        # branch event 'Report: A Clear Gaze' became eligible the moment the
        # Satsuki Sho entered race_history and then simply never fired, leaving
        # the career on the default Derby arm. A career-branching storyline is
        # not a coincidence and must not be rolled for.
        #
        # A secret event still TAKES the turn's slot -- the coincidence roll is
        # skipped when one fires, which is what the old elif chain expressed.
        if _maybe_fire_secret_event(full_state, career_state, response):
            pass
        elif payload.get("command_group_id") == _OUTING_COMMAND_GROUP:
            # PAL/GROUP OUTING: recreation spent WITH a specific partner
            # (capture: command_type 3, command_group_id 390, select_id =
            # the partner's chara id). Serves that partner's own outing story
            # instead of the normal recreation coincidence.
            sel = payload.get("select_id")
            card_id = _card_for_partner(full_state, career_state["data"]["chara_info"], sel)
            if card_id is None or not _queue_outing_event(
                    response, full_state, career_state, card_id, sel):
                _maybe_fire_support_event(full_state, career_state, response)
        elif payload.get("command_group_id") in _RECREATION_GROUPS or payload.get("command_type") == 3:
            # Recreation (the URA riverbank outing) is where the trainee's date
            # events AND support-card date events fire -- roll a mix so it isn't
            # always the same. Crane game first (real capture: it REPLACES the
            # date/support coincidence that turn); else a chara outing, else a
            # support-card event (both from the engine).
            # NOTE: summer camp's merged Rest & Recreation (group 304) is
            # deliberately NOT here -- the claw machine does not occur at camp
            # (user-confirmed).
            rec_turn = payload.get("current_turn")
            # _crane_available is checked HERE, not just inside _queue_crane_game:
            # once the career's crane is spent, a winning roll used to take this
            # branch, queue nothing, and leave the outing with no event at all.
            if (random.random() < _CRANE_CHANCE and not _is_camp_turn(rec_turn)
                    and (rec_turn or 0) >= _CRANE_FIRST_TURN
                    and _crane_available(full_state)):
                _queue_crane_game(response, full_state, career_state)
            else:
                fired = random.random() < 0.55 and _maybe_fire_outing_event(full_state, career_state, response)
                if not fired:
                    _maybe_fire_support_event(full_state, career_state, response)
        elif cmd is not None and cmd != _RACE_COMMAND_ID:
            # A training turn's coincidence is either a PAL/GROUP card in the
            # trained facility (its first-meeting event, an outing-unlock roll
            # or its repeatable event -- these take priority, they're the only
            # way that content is reachable), one of the TRAINEE's own random
            # story events, or an ordinary support-card event.
            # The acupuncturist (1/400) and Extra Training (6% after a
            # SUCCESSFUL training) live in the 7xx band, which the random pull
            # never scans, so they need their own roll. Extra Training's stat
            # is the facility just trained.
            trained_stat = _COMMAND_TRAINED_STAT.get(cmd)
            if _maybe_fire_shared_beat(full_state, career_state, response,
                                       _ACUPUNCTURE_SUFFIX, _ACUPUNCTURE_CHANCE):
                pass
            elif (trained_stat and not training_failed
                  and not _is_camp_turn(payload.get("current_turn"))
                  and (payload.get("current_turn") or 0) <= _EXTRA_TRAINING_LAST_TURN
                  and _maybe_fire_shared_beat(
                      full_state, career_state, response, _EXTRA_TRAINING_SUFFIX,
                      _EXTRA_TRAINING_CHANCE, {"stat": trained_stat},
                      once_per_career=False)):
                # User-confirmed 2026-08-24: Extra Training must NOT fire
                # during summer camp (same "no chain-style coincidences during
                # camp" rule the crane game and support chains already follow
                # -- see _is_camp_turn's other call sites), but it was: camp
                # facility ids (601-605) are covered by _COMMAND_TRAINED_STAT
                # same as the base ids, so trained_stat resolved fine and
                # nothing here previously excluded the roll.
                pass
            # (SECRET events are no longer part of this chain -- they are
            # checked for every command at the top of this block.)
            elif not _maybe_fire_pal_training_event(full_state, career_state, response,
                                                    pal_partners):
                if not _maybe_fire_chara_random_event(full_state, career_state, response):
                    _maybe_fire_support_event(full_state, career_state, response)
    except Exception:
        log.exception("dynamic event firing failed (skipped)")

    # PAL/GROUP FINALE ("you've been out with everyone"). Queued AFTER the
    # turn's own action so it chains behind it -- queueing it during upkeep let
    # it steal the display slot from the very outing that completed the set.
    try:
        _maybe_queue_pal_finale(response, full_state, career_state)
    except Exception:
        log.exception("pal/group finale queue failed; skipping")

    # SCENARIO_SCHEDULE events (Director turn 3, Happy Meek turn 4, URA Finale
    # announcements, ...) display LAST -- the real game resolves the turn's own
    # action (rest/duel/failure) and any recreation/support coincidence first,
    # and only then plays the meta scenario beat. The chain is stored
    # unconditionally (so it's never lost even when something else claims the
    # display slot); it only becomes the IMMEDIATE display if this turn had
    # nothing else to show. _drain_pending checks EXTRA_EVENTS_KEY before this
    # queue, so even when something else IS showing now, the scenario event(s)
    # correctly surface once that -- and any chained coincidence event -- finish.
    if scheduled:
        # MERGE, never overwrite: an earlier turn's cutscene that hasn't been
        # displayed yet (the slot was claimed by a coincidence/hint event) was
        # being DROPPED when the next scheduled turn arrived -- which is how
        # the Director (turn 3) and Happy Meek (turn 4) unlock cutscenes went
        # missing and surfaced much later (live-reported: 'after winning the
        # debut race, i get rewarded by unlocking the director').
        pending = list(full_state.get(single_mode_events.PENDING_EVENTS_KEY, []))
        pending += [e for e in scheduled if e not in pending]
        player_chara = (career_state["data"]["chara_info"].get("card_id") or 0) // 100
        shown = response["data"].get("unchecked_event_array")
        unlock = next((e for e in pending if single_mode_events.npcs_unlocked_by(e)), None)
        if unlock is not None:
            # An NPC-unlock cutscene must play ON ITS OWN TURN, in its own
            # right -- if it only claims the display slot when nothing else
            # wants it, it lingers in the queue and finally surfaces after
            # whatever the player does next, reading as that thing's reward
            # (live-reported: Happy Meek "unlocked" by finishing the claw
            # game, and the Director handed out after a goal race). Anything
            # already showing is pushed behind it instead of displacing it.
            #
            # The chain still plays in its recorded order -- turn 4 is
            # [1015 story, 102005 Meek unlock], so the story leads and
            # _drain_pending's unlock-jumps-the-queue rule pulls Meek up
            # right behind it. Only the FRONT of the chain is displayed here.
            lead = pending.pop(0)
            if shown:
                full_state.setdefault(single_mode_events.EXTRA_EVENTS_KEY, [])
                full_state[single_mode_events.EXTRA_EVENTS_KEY][:0] = shown
            response["data"]["unchecked_event_array"] = [
                single_mode_events.event_entry(lead, player_chara)]
        elif not shown:
            response["data"]["unchecked_event_array"] = [
                single_mode_events.event_entry(pending[0], player_chara)]
        full_state[single_mode_events.PENDING_EVENTS_KEY] = pending

    # A race ctx from a PREVIOUS turn is dead -- the player set a race up and
    # then backed out (nothing but race_out ever clears RACE_CTX_KEY). Left in
    # place, the next race_entry re-serves it and wedges the client.
    dead_ctx = full_state.get(RACE_CTX_KEY)
    if dead_ctx:
        now_turn = career_state["data"]["chara_info"].get("turn")
        if (dead_ctx.get("turn") is not None and now_turn is not None
                and dead_ctx["turn"] < now_turn):
            full_state.pop(RACE_CTX_KEY, None)

    # PASSIVE-GOAL ANNOUNCEMENT, last -- see _announce_due_passive_goals.
    _announce_due_passive_goals(response, full_state, career_state,
                                payload.get("current_turn"))

    # THE UNIFIED PIPELINE. One poll replaces the per-family
    # `_maybe_queue_*(response, full_state, career, turn)` calls: every registered
    # producer in career_producers is asked what fires this turn, dedupe and
    # ordering are the pipeline's job, and the reward rides on each Event.
    # Migrated so far: the Grand Live chain and URA's fan-gated fixed beats.
    #
    # MOVED HERE 2026-08-26 (live-reported softlock): this used to run BEFORE
    # the player's own recreation/support coincidence and the passive-goal
    # announcement above. Since a producer's event claims the display slot
    # the instant nothing else has it yet (see _poll_career_events's own
    # docstring: "the turn's own action first, meta cutscene last"), running
    # first made it ALWAYS win that race -- the opposite of its stated intent.
    # A turn where a Grand Live beat (e.g. Concert Begins) coincided with the
    # player's chosen recreation-with-a-support-card action served the beat
    # first and the recreation not at all this turn (deferred, then serving
    # out of order next turn) -- exactly the reported "recreation event
    # skipped, goal and concert scrambled" sequence.
    try:
        _poll_career_events(response, full_state, career_state, payload,
                            endpoint="exec_command")
    except Exception:
        log.exception("career event poll failed; skipping")

    # BACKLOG DRAIN: if this turn ends with nothing on display but events are
    # waiting (they chained behind a busy earlier turn and its resolution chain
    # ended before reaching them), surface the head NOW. Without this, a stale
    # beat sat until some later chain happened to drain it and then popped up
    # absurdly late ('Hopes for the Tenno Sho' several turns after the race --
    # live-reported; same staleness behind Meek's ghost re-unlocks).
    if not response["data"].get("unchecked_event_array"):
        backlog = list(full_state.get(single_mode_events.EXTRA_EVENTS_KEY, []))
        if backlog:
            response["data"]["unchecked_event_array"] = [backlog.pop(0)]
            full_state[single_mode_events.EXTRA_EVENTS_KEY] = backlog

    _remember_display(full_state, response)
    # campaign_walking's gauge_up_singlemode -- one training/turn action
    # fills the whole bar (see campaign_walking.py's module docstring).
    from . import campaign_walking
    campaign_walking.add_gauge(full_state, "singlemode")
    state_store.save_state(viewer_id, full_state)
    return response


# command_info_array params_inc_dec_info_array target_type -> chara_info field.
# 1-5 stats, 10 vital, 30 skill points; 20/101 (fan/max-up) not applied here.
_PREVIEW_TARGET_TO_STAT = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}


def _preview_gains_for(career_state: dict, command_id) -> list | None:
    """The params_inc_dec_info_array the client is CURRENTLY displaying for this
    command (from the career's home_info.command_info_array) -- i.e. the exact
    gains shown on the training button the player just pressed."""
    hi = career_state.get("data", {}).get("home_info", {})
    for c in hi.get("command_info_array", []) or []:
        if c.get("command_id") == command_id:
            return c.get("params_inc_dec_info_array") or []
    return None


def _merge_preview_params(base: list, extra: list) -> list:
    """base + extra, summing entries that share a target_type.

    Summed rather than appended so _apply_preview_gains' cap and not_up
    handling sees ONE entry per stat -- two separate entries for the same stat
    would each be clamped against the cap independently and could report
    "superb form" for a stat that did in fact go up."""
    merged = [dict(p) for p in base]
    by_type = {p.get("target_type"): p for p in merged}
    for p in extra or ():
        tt = p.get("target_type")
        if tt in by_type:
            by_type[tt]["value"] = by_type[tt].get("value", 0) + p.get("value", 0)
        else:
            entry = dict(p)
            merged.append(entry)
            by_type[tt] = entry
    return merged


def _note_capped_facility_stats(chara_info: dict, command_id, not_up: list) -> None:
    """Add the not_up_parameter_info codes for every stat this facility trains
    that is already at its hard cap. Idempotent against `not_up`, which
    _apply_preview_gains has already filled for the stats the preview DID
    carry."""
    if not_up is None:
        return
    try:
        stats = training_formula.facility_stats(
            chara_info.get("scenario_id") or 1, int(command_id or 0))
    except Exception:
        log.exception("facility stat profile lookup failed; "
                      "skipping the capped-stat notices")
        return
    for stat in stats:
        code = _STAT_TO_TARGET_TYPE.get(stat)
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}")
        if not code or code in not_up or not cap:
            continue
        if (chara_info.get(stat) or 0) >= cap:
            not_up.append(code)
            log.info("training reward: already at ceiling -> "
                     "not_up_parameter_info %s (stat %s, facility %s)",
                     code, stat, command_id)


def _apply_preview_gains(chara_info: dict, preview: list | None, payload: dict,
                         not_up: list | None = None) -> bool:
    """Apply a params_inc_dec_info_array to chara_info (stats clamped to their
    max, vital clamped to max_vital). Returns False if there's nothing to apply
    so the caller can fall back. Makes applied gains == displayed preview.

    `not_up`, when given, collects the status codes for gains that landed on a
    stat ALREADY at its cap, so the training result says "<stat> is in superb
    form" the same way an event outcome does (see _note_not_up). The preview's
    target_type IS the status code for the five stats (1-5).

    BUG FIXED 2026-08-28 (live-reported: "the training values i see and the
    actual stats i get are so so much different -- it says 9, but when i
    train i got 3"): training_formula.calculate_training_gain already halves
    the portion of a stat gain past the 1200 soft cap (via _apply_stat_cap)
    before it ever reaches the preview the client displays -- that's the
    number in `preview` here. Routing it through event_engine.add_stat, which
    halves-past-1200 AGAIN, silently halved an already-halved number a
    second time for every training that crossed 1200. The testing-knob
    branch below was already doing the right thing (a flat clamped add) for
    exactly the reason its own comment gives -- the same reasoning applies
    unconditionally now that the formula, not this function, owns the 1200
    rule."""
    if not preview:
        return False
    vital = payload.get("current_vital", chara_info.get("vital", 100))
    for p in preview:
        tt, val = p.get("target_type"), p.get("value", 0)
        stat = _PREVIEW_TARGET_TO_STAT.get(tt)
        if stat is not None:
            # Slow Metabolism: 'cannot gain Speed from training'.
            if conditions.blocks_stat(chara_info, stat):
                continue
            cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
            if not_up is not None and val > 0 and chara_info.get(stat, 0) >= cap:
                not_up.append(tt)
                # Same line event_engine.not_up_info logs, for the same reason:
                # a capped gain is invisible in the log otherwise, so a report
                # that the client "said nothing" cannot be told apart from the
                # server never having named the stat (user-reported 2026-09-07,
                # after the panel itself was confirmed working).
                log.info("training reward: already at ceiling -> "
                         "not_up_parameter_info %s (stat %s)", tt, stat)
            chara_info[stat] = max(0, min(cap, chara_info.get(stat, 0) + val))
        elif tt == 30:  # skill points
            chara_info["skill_point"] = chara_info.get("skill_point", 0) + val
        elif tt == 10:  # vital / energy (value already signed)
            vital = vital + val
    chara_info["vital"] = max(0, min(chara_info.get("max_vital", 100), vital))
    return True


# training command_id -> facility type index (matches CARD_TYPE_NAMES order in
# training_formula: speed/stamina/power/guts/wisdom).
TRAINING_COMMAND_IDS = [101, 105, 102, 103, 106]  # speed, stamina, power, guts, wiz
_COMMAND_TO_TYPE = {101: 0, 105: 1, 102: 2, 103: 3, 106: 4}
_STAT_TO_TARGET_TYPE = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}

# Support cards occupy evaluation_info_array target_id == their deck position;
# `evaluation` there IS the bond (friendship) gauge the client's card meters
# read. NPC training partners have target_id > deck size and stay at 0.
_BOND_GAIN_PER_TRAINING = 7
_BOND_MAX = 100
# Scenario NPCs build bond too, just slower: +2 per training in their facility
# (capture-measured for the Director and the Reporter; Meek shares the rule).
_NPC_TARGET_IDS = {102, 103, 2001}
_NPC_BOND_GAIN = 2
APPRAISAL_CTX_KEY = "appraisal_ctx"   # {'target_id', 'event_id', 'amount', 'stat'}
_APPRAISAL_CHANCE = 0.25              # per training with 102/103 in the facility


@career_events.resolver("appraisal")
def _resolve_appraisal(full_state, chara_info, event, choice_number, **kw):
    """Tiered reward (Director -> skill points, Reporter -> the trained
    facility's stat) plus the bond deepening -- moved verbatim from the old
    NPC APPRAISAL RESOLUTION branch. Inputs ride on the Event's own payload
    (set at queue time) rather than a separate ctx key."""
    payload = event.payload or {}
    amount = payload.get("amount") or 0
    stat = payload.get("stat")
    not_up = []
    if stat:
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        # "<stat> is in superb form" when the stat is ALREADY at its cap. The
        # reward is written straight onto chara_info rather than through a
        # choice, so it has to name its own capped stat the way
        # event_engine.not_up_info does for everything that goes through one.
        if amount > 0 and chara_info.get(stat, 0) >= cap:
            not_up.append(event_engine._STAT_KEY_IDX.get(stat))
        chara_info[stat] = min(chara_info.get(stat, 0) + amount, cap)
    else:
        chara_info["skill_point"] = chara_info.get("skill_point", 0) + amount
    tid = payload.get("target_id")
    if tid:
        _ensure_npc_eval(chara_info, tid)
        _set_bond(chara_info, tid, min(_BOND_MAX, _bond_of(chara_info, tid)
                                       + single_mode_events.APPRAISAL_BOND_GAIN))
    not_up = [c for c in not_up if c]
    return {"not_up": not_up} if not_up else {}

# chara_ids that are ALSO a scenario NPC gated behind a story-unlock event (see
# single_mode_events.NPC_UNLOCKS) -- currently just Light Hello (9008, Grand
# Live's own mascot, unlocked by event 202002). A handful of these NPCs (9001
# Tazuna, 9004, 9006, ...) are separately real ownable support cards, but only
# Light Hello's chara_id collides with one that's ALSO gated by NPC_UNLOCKS --
# computed from that dict rather than hardcoded so a future gated NPC with a
# real card is covered automatically.
# SUPERSEDED by Scenario.gated_npc_charas(). Deriving the set from every
# scenario's unlock table at once made the gate global: Light Hello's card was
# hidden in URA too, where 202002 never fires and nothing would ever have
# released her. Kept only as the union, for reference.
_GATED_NPC_CHARA_IDS = frozenset(
    chara_id for unlocks in single_mode_events.NPC_UNLOCKS.values()
    for _, chara_id in unlocks)


def _deck_position_for_chara(chara_info: dict, chara_id: int):
    """The deck position (1-6) whose equipped support card's character is
    chara_id, or None if she isn't in the deck at all."""
    for c in chara_info.get("support_card_array", []) or []:
        if master_data.support_card_chara(c.get("support_card_id") or 0) == chara_id:
            return c.get("position")
    return None


def _hide_ungated_support_card_rows(chara_info: dict) -> None:
    """A deck position whose card's character is one of _GATED_NPC_CHARA_IDS
    must not appear as a training partner before that character's own
    story-unlock event fires -- same rule as her NPC-band row.

    BUG FIXED 2026-08-23 (live-reported: "2 light hellos in my training
    screen... the support card light hello should appear on turn 4... instead
    it's unlocked on start, yet a not support card light hello gets unlocked
    at that time and she doesn't have a bond gauge" -- then, once this half
    was fixed, corrected further: "the NPC band does not appear if you got
    her support card, and either way she doesn't unlock till turn 4 no
    matter what"). A deck position simply inherits whatever is_appear the
    captured /start template happened to have for that target_id -- 1 from
    turn 1 for an ordinary card, which is right for every real support card,
    but wrong for one whose character is Grand Live's own unintroduced
    scenario NPC. And once event 202002 (turn 4) resolves, that same
    character must appear EXACTLY ONCE, not twice: if she's already a deck
    card, that IS her presence -- the separate NPC-band row/placement (see
    _sync_chara_info's unlock loops, which now skip it entirely whenever
    _deck_position_for_chara finds a match) must never also be registered,
    or she'd be placed as a training partner under both her card AND a
    duplicate NPC slot forever after turn 4."""
    gated = scenarios.for_chara(chara_info).gated_npc_charas()
    for c in chara_info.get("support_card_array", []) or []:
        if master_data.support_card_chara(c.get("support_card_id") or 0) not in gated:
            continue
        for row in chara_info.get("evaluation_info_array") or []:
            if row.get("target_id") == c.get("position"):
                row["is_appear"] = 0


def _register_npc_unlocks(full_state: dict, updated: dict, event_id) -> None:
    """Reveal every NPC this event introduces, on the response that resolves it.

    Called for BOTH resolution paths -- see the note at the call site. Pays no
    effects and is idempotent, so the unified pipeline running it too cannot
    double anything."""
    for npc in single_mode_events.npcs_unlocked_by(event_id):
        # If this character is ALSO equipped as a real support card
        # (Light Hello, see _hide_ungated_support_card_rows), her deck
        # position IS her presence -- reveal ONLY that, and skip the
        # separate NPC-band registration entirely (never add her to
        # UNLOCKED_NPCS_KEY, never give her a target_id==npc[0] row), or
        # she gets placed as a training partner under her card AND a
        # duplicate NPC slot forever after this event (user-corrected
        # 2026-08-23: "the NPC band does not appear if you got her
        # support card").
        deck_position = _deck_position_for_chara(updated, npc[1])
        if deck_position is not None:
            for row in updated.get("evaluation_info_array") or []:
                if row.get("target_id") == deck_position:
                    row["is_appear"] = 1
            continue
        unlocked_now = [list(x) for x in
                        full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
        newly_unlocked = list(npc) not in unlocked_now
        if newly_unlocked:
            unlocked_now.append(list(npc))
            full_state[single_mode_events.UNLOCKED_NPCS_KEY] = unlocked_now
        _ensure_npc_eval(updated, npc[0])
        # THE visual: the real resolution response flips is_appear to 1 on
        # the NPC's row (capture, check_event(1014): target 102 is_appear 1
        # while still-locked 103 stays 0). Without this exact flag the
        # client shows no 'will now appear in training' -- rows existing
        # with is_appear 0 was the whole 'no visual unlock' saga.
        #
        # ONLY on the first unlock. is_appear is persistent state that
        # rides chara_info from here on, so re-flipping it on a later event
        # that happens to map to the same NPC makes the client replay the
        # "will now appear in training" announcement -- live-reported
        # 2026-09-03: Happy Meek's message showed when expected and then
        # again a few events later, inside Tokai Teio's chain.
        if not newly_unlocked:
            continue
        for row in updated.get("evaluation_info_array") or []:
            if row.get("target_id") == npc[0]:
                row["is_appear"] = 1


def _ensure_npc_eval(chara_info: dict, target_id: int) -> None:
    """Make sure an NPC has an evaluation row so bond can accrue (the real wire
    carries rows for 102/103/2001 from turn 1; older careers lack them)."""
    arr = chara_info.setdefault("evaluation_info_array", [])
    if not any(e.get("target_id") == target_id for e in arr):
        arr.append({"target_id": target_id, "training_partner_id": target_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 0, "group_outing_info_array": []})
_AWAY_WEIGHT = 50          # weight of a card NOT appearing in any facility
_BASE_FACILITY_WEIGHT = 100

# Base chance per placed support card to show a skill hint, before the card's
# hint_frequency. MEASURED on 373k real card placements from the bot_logs
# training boards (server_command_board hints/partners), with each deck's
# hint_frequency read at its real level from master support_card_effect_table
# plus unique effects: the base implied by hf 0 / 20 / 30 / 50 is 6.97 / 6.98 /
# 7.05 / 7.08% (pooled fit 7.01%; the hf-0 interval is [6.83,7.12], which
# rules out 7.5%). So base * (1 + hf/100) is the right shape and 7% is the base.
# Bond has no effect: 8.30% below bond 80 vs 8.48% at 80+.
_HINT_BASE_CHANCE = 0.07


def _card_id_at_position(chara_info: dict, position) -> int | None:
    for c in chara_info.get("support_card_array", []) or []:
        if c.get("position") == position:
            return c.get("support_card_id")
    return None


def _skill_hint_chance(support_card_id) -> float:
    """A placed support card's chance to grant a skill hint this training: base
    7% (measured, see _HINT_BASE_CHANCE), scaled by the card's hint_frequency
    effect (cards.json, GameTora -- e.g. +60% at its shown tier -> 7% * 1.6 =
    11.2%, same 'base * (1 +
    pct/100)' shape as the race fan-bonus formula in _fan_gain_for_race). Pal
    (friend/group) cards have no hint_frequency effect and never proc -- they
    don't teach skills in the real game either.

    Also 0 if the card has no master.mdb-backed character (event_engine.
    card_chara_id -> support_card_data.chara_id) -- a handful of cards.json
    entries have no matching support_card_data row (confirmed: id 30296),
    and _build_hint_reveals needs that character to pick a skill pool and
    build the reveal event, so a card that can't resolve there must never
    show the '!' badge in the first place (showing it with no way to
    actually reveal anything would be worse than never showing it)."""
    card = training_formula.CARD_BY_ID.get(str(support_card_id))
    if not card or card.get("type") not in training_formula.CARD_TYPE_NAMES:
        return 0.0
    if not event_engine.card_chara_id(support_card_id):
        return 0.0
    hint_pct = training_formula.card_effect(support_card_id, "hint_frequency", 0) or 0
    return _HINT_BASE_CHANCE * (1 + hint_pct / 100)


def _hinting_partners(chara_info: dict, turn, partners) -> list:
    """Which of these placed support-card positions show the '!' hint icon
    THIS turn -- the client field is command_info_array[].tips_event_partner_array
    (previously hardcoded to [], which is exactly why no '!' ever appeared:
    the roll existed nowhere the client could see it). Rolled once per
    (trainee, turn, position) with a stable seed rather than plain
    random.random(), so it's identical whether it's being computed to DISPLAY
    the icon (_build_training_command_info, called on every /load or
    check_event refresh for the same turn) or to actually APPLY the hint once
    the player trains there (_build_hint_reveals) -- otherwise the hint
    granted could silently differ from the one the player saw and chose to
    train for."""
    card_id = int(chara_info.get("card_id") or 0)
    hinting = []
    for pos in partners:
        sid = _card_id_at_position(chara_info, pos)
        if sid is None:
            continue
        chance = _skill_hint_chance(sid)
        if chance <= 0:
            continue
        seed = (card_id << 20) ^ (int(turn or 0) << 8) ^ int(pos)
        if random.Random(seed).random() < chance:
            hinting.append(pos)
    return hinting


# (_HINT_REVEAL_EVENT_ID used to live here as the invented 90001. The reveal now
# carries the real 200xx id of its own 80<chara>003 story -- see
# event_engine.support_random_event_id.)
HINT_REVEAL_CTX_KEY = "active_hint_reveal"  # on full_state: {"pending": [effects, ...]}
# The base level of a support-card hint is ALWAYS 1; any spread comes only from
# the card's hint-level effect (type 17), added below. MEASURED (bot_logs): each
# "A Hint for Growth" reveal (event 20001+rank) was joined to the deck card it
# names, and the skill_tips level gained was read turn over turn:
#   card bonus 0 -> +1 in 948 cases, +2/+3 in only 4 of 1,262
#   bonus 1 -> +2 (249) | bonus 2 -> +3 (949) | bonus 3 -> +4 (88) | 4 -> +5
# (The few off-by-ones are the level-5 cap.) A 60/30/10 roll would make 40% of
# bonus-0 hints +2 or +3.
_HINT_LEVEL_WEIGHTS = ([1], [100])
_MAX_HINT_LEVEL = 5              # the ladder _HINT_SP_DISCOUNT tops out at
# A '!' proc isn't always a skill hint -- one hint_group per card is the STAT
# variant instead, and single_mode_hint_gain says which (see
# event_engine.card_hint_groups). The group is drawn uniformly out of the
# card's own pool, so the odds are the card's rather than a flat constant:
# Tokai Teio's SSR has nine skill groups and one stat group, i.e. 1-in-10.
#
# _HINT_STAT_VARIANT_CHANCE / _HINT_STAT_VARIANT below are the FALLBACK for a
# card with no rows at all (pals and group cards -- 30052 has none, and
# GameTora confirms cards without hints roll an aptitude-based one instead).
# The master rows agree with the user-supplied speed values exactly (+6 speed,
# +2 power) and disagree for the others -- a guts card pays +6 stamina / +2
# guts, a wit card +6 wit / +5 skill points -- so the per-card table wins.
_HINT_STAT_VARIANT_CHANCE = 0.25
_HINT_STAT_VARIANT = {
    "speed": [("speed", 6), ("power", 2)],
    "stamina": [("stamina", 6), ("guts", 2)],
    "power": [("power", 6), ("stamina", 2)],
    "guts": [("guts", 6), ("speed", 1), ("power", 1)],
    "wiz": [("wiz", 6), ("speed", 2)],
}
_HINT_BOND_GAIN = 5   # a hint also deepens the bond with the card that gave it


def _build_hint_reveals(chara_info: dict, career_state: dict, command_id, turn) -> list:
    """For each support-card position that showed the '!' icon this turn (see
    _hinting_partners -- re-derived with the SAME seed, so this always matches
    what was actually displayed to the player before they chose to train
    here), build ONE hint-reveal event granting ONE skill at a random level
    1-3 from THAT card's own character's learnable pool -- not by mutating
    chara_info directly. Every other interactive moment in this codebase
    (rest, duel, support/outing events) is a PREVIEW at exec_command time and
    a COMMIT at check_event time; a silent mutation here was exactly why the
    '!' badge showed up but tapping/training there did nothing visible --
    there was no event to tap.

    One hint per card, one skill per hint, leveled (not counted) 1-3: an
    earlier version granted 1-4 DIFFERENT skills per proc from a single
    combined reveal -- corrected per direct feedback ('its 1-3 skill hint
    LEVELS for ONE hint, not 3 different skills') after a live test showed 4
    unrelated skills from one card. The base level is 1, plus the card's hint-level
    effect. This is measured, not approximated -- see _HINT_LEVEL_WEIGHTS.

    Skill pool is sourced from EACH card's own character (available_skill_set,
    via the character's lowest-id trainee card -- master data keys this pool
    by trainee card_id, not support_card_id, and a support card has no
    card_data row of its own to query directly), matching the user's
    correction: 'a skill hint from that uma', not the trainee's, and NOT
    mixed with any other card's pool -- combining multiple cards' skills into
    one reveal was the other half of the same live-tested bug (a skill that
    didn't belong to the card the player actually saw the '!' on).

    Each reveal event is attributed to (and its story titled for) that same
    character -- 'A Hint for Growth' (master.mdb text_data cat 181, story id
    800000000 + chara_id*1000 + 3, confirmed present for every trainee
    character 1000-1077, in the exact same numbering family already used for
    the working random support-card events, SUPPORT_RANDOM_EVENT_ID/story
    80<chara>nnn).

    Returns a list of (event_entry, effects) tuples, in the order they should
    be queued/chained -- one per procced card -- or [] if nothing procced."""
    partners = _partners_in_command(career_state, command_id)
    hinting_positions = _hinting_partners(chara_info, turn, partners)
    if not hinting_positions:
        return []
    # AT MOST ONE hint per training, even when several cards show the '!'
    # (user-confirmed official behaviour: two cards in one facility do NOT both
    # proc -- one is picked). Kept as a list so the queue/commit plumbing and
    # its pending-effects queue stay unchanged.
    hinting_positions = [random.choice(list(hinting_positions))]
    learned = {s.get("skill_id") for s in chara_info.get("skill_array", []) or []}
    reveals = []
    for pos in hinting_positions:
        sid = _card_id_at_position(chara_info, pos)
        if sid is None:
            continue
        chara = event_engine.card_chara_id(sid)
        if not chara:
            continue
        # THE CARD'S OWN HINT POOL, not the character's learnable skill list.
        # single_mode_hint_gain is what the '!' actually draws from; the old
        # available_skill_set read handed out anything the character could ever
        # buy, which is how Tokai Teio's card gave a GOLD hint for a skill that
        # is not among her nine (all rarity 1) -- user-reported 2026-09-06.
        groups = event_engine.card_hint_groups(sid)
        candidates = [g for g in groups
                      if g[0] == "skill" and g[1] not in learned]
        # The FALLBACK stat, for a card with no rows at all. It follows the
        # CARD'S OWN TYPE, not the facility being trained (live-reported: a
        # speed card clicked on the guts facility still paid +6 speed / +2
        # power). Cards that DO have rows carry their own stat group, and it
        # says the same thing -- Teio's speed card pays speed +6 / power +2.
        card_type = training_formula.card_type_index(sid)
        trained_stat = (training_formula.STAT_NAMES[card_type]
                        if card_type is not None else None)
        # ONE GROUP OUT OF THE POOL, skill groups and the stat group together,
        # so a card whose whole pool is already learned still pays its stat
        # variant rather than the proc vanishing ('!' shown, nothing granted).
        stat_groups = [g for g in groups if g[0] == "stat"]
        pool = candidates + stat_groups
        pick = random.choice(pool) if pool else None
        if pick is not None and pick[0] == "stat":
            effects = [dict(e) for e in pick[1]]
        elif pick is None and trained_stat:
            # No rows at all (a pal or group card): the documented fallback is
            # the facility's own stat bump.
            effects = [{"type": stat, "value": amount}
                       for stat, amount in _HINT_STAT_VARIANT.get(
                           trained_stat, [(trained_stat, 6)])]
        elif pick is not None:
            skill_id = pick[1]
            level = random.choices(_HINT_LEVEL_WEIGHTS[0], weights=_HINT_LEVEL_WEIGHTS[1], k=1)[0]
            # HINT LEVEL UP (effect type 17, carried by 144 cards at +1..+4) --
            # read from master but never applied, so a card whose whole selling
            # point is better hints gave exactly the same hints as any other.
            level += int(round(training_formula.master_effect(
                sid, training_formula.EFFECT_HINT_LEVEL)))
            level = min(_MAX_HINT_LEVEL, level)
            effects = [{"type": "skill_hint", "skill_id": skill_id, "value": level}]
        else:
            continue
        # A hint also grants bond with the card that gave it (+5).
        effects = effects + [{"type": "bond", "value": f"+{_HINT_BOND_GAIN}",
                              "char_id": chara}]
        story_id = 800000000 + chara * 1000 + 3
        # 'A Hint for Growth' IS the support card's random event #3, so it takes
        # that story's own 200xx id -- not the invented 90001 this used to send,
        # which is not a real event id at all. play_timing 6 for the same
        # reason: the whole #3 family is served on the exec_command that
        # procced it (see the 20xxx band note in event_engine).
        entry = event_engine.career_event_entry(
            {"choices": [{"effects": effects}]},
            event_engine.support_random_event_id(story_id), story_id,
            chara_id=0, support_card_id=sid, play_timing=6)
        reveals.append((entry, effects))
    return reveals


def _support_card_levels(full_state: dict) -> dict:
    """{support_card_id: real persistent level}, from the OWNED collection's
    exp -- see training_formula.calculate_training_gain's support_card_levels
    param for why this is needed (support_card_unique_effect's real per-card
    bonuses, gated by real level, which cards.json never included)."""
    return {
        c["support_card_id"]: training_formula.support_card_level_from_exp(
            c["support_card_id"], c.get("exp") or 0)
        for c in full_state.get(collection.SUPPORT_CARD_KEY) or []
        if isinstance(c, dict) and c.get("support_card_id")
    }


def _friendship_stacks(full_state: dict) -> dict:
    """{support_card_id: stack_count} for EFFECT_FRIENDSHIP_STACKING (106) --
    see FRIENDSHIP_STACK_KEY. Keys come back from JSON as strings; converted
    to int here to match calculate_training_gain's friendship_stacks param
    (keyed the same way as support_card_levels/pos_to_sid values)."""
    raw = (full_state or {}).get(FRIENDSHIP_STACK_KEY) or {}
    return {int(k): v for k, v in raw.items()}


def _bond_of(chara_info: dict, position) -> int:
    for e in chara_info.get("evaluation_info_array", []) or []:
        if e.get("target_id") == position:
            return e.get("evaluation", 0) or 0
    return 0


def _set_bond(chara_info: dict, position, value: int) -> None:
    for e in chara_info.get("evaluation_info_array", []) or []:
        if e.get("target_id") == position:
            e["evaluation"] = value
            return


def _init_bonds(chara_info: dict) -> None:
    """Seed each support card's starting bond from its initial_friendship_gauge
    (0 for most). Runs at career start so bonds begin fresh, not at the captured
    run's values. Also seeds the scenario NPCs' evaluation rows (Director 102 /
    Reporter 103 / Meek 2001) -- the real wire carries them from turn 1."""
    for c in chara_info.get("support_card_array", []):
        init = training_formula.card_effect(c.get("support_card_id"), "initial_friendship_gauge", 0) or 0
        _set_bond(chara_info, c.get("position"), init)
    for tid in sorted(_NPC_TARGET_IDS):
        _ensure_npc_eval(chara_info, tid)
    # A deck card whose character hasn't story-unlocked yet (e.g. Light Hello,
    # see _hide_ungated_support_card_rows) starts hidden -- career start is
    # necessarily before any such unlock event has fired.
    _hide_ungated_support_card_rows(chara_info)


def _apply_initial_stats(chara_info: dict) -> dict:
    """Silently add the deck's support-card 'initial stat' bonuses (åˆæœŸã‚¹ãƒ”ãƒ¼ãƒ‰
    /â€¦/åˆæœŸè³¢ã•) to the starting stats. Summed across the deck, clamped to each
    stat's cap. Runs once at career start (on top of the trainee's base stats)."""
    totals: dict[str, int] = {}
    for c in chara_info.get("support_card_array", []):
        bonuses = training_formula.initial_stat_bonuses(
            c.get("support_card_id"), c.get("limit_break_count", 0))
        for stat, val in bonuses.items():
            totals[stat] = totals.get(stat, 0) + val
    # EFFECT_INITIAL_STAT_DECK_COMPOSITION (105, gametora-confirmed
    # 2026-08-19) -- a SEPARATE unique-effect bonus keyed off the whole
    # deck's card TYPES, not the flat per-card support_card_effect_table
    # (9-13) totals above. Needs the card's REAL level (support_card_
    # unique_effect.lv gates on it, limit_break_count alone isn't enough),
    # so this derives it from exp the same way support_card_levels does
    # elsewhere.
    deck_ids = [c.get("support_card_id") for c in chara_info.get("support_card_array", [])
               if c.get("support_card_id")]
    deck_levels = {c.get("support_card_id"): training_formula.support_card_level_from_exp(
                      c.get("support_card_id"), c.get("exp", 0))
                   for c in chara_info.get("support_card_array", []) if c.get("support_card_id")}
    for stat, val in training_formula.deck_composition_initial_stat_bonus(
            deck_ids, deck_levels).items():
        totals[stat] = totals.get(stat, 0) + val
    for stat, val in totals.items():
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        chara_info[stat] = min(chara_info.get(stat, 0) + val, cap)
    return totals


# Flat +200 career-start cap boost over the card's base (empirically constant).
_CAREER_BASE_CAP_BONUS = 200

# Blue (stat) spark: flat starting-stat bonus by star, from succession_initial_factor
# (factor_type 1): 1â˜…+5, 2â˜…+12, 3â˜…+21. Blue factor_group_id IS the stat index.
_INHERIT_BLUE = {1: 5, 2: 12, 3: 21}

# Flat stat-CAP bonus by a spark's own star, user-supplied 2026-08-27
# reference table -- applies at career start to every stat a spark's group
# targets (blue: its own stat; green: whichever stat(s) its group's effect
# rows target, e.g. Anchors Aweigh raises both Stamina and Power caps).
_CAP_INCREASE_BY_STAR = {1: 4, 2: 9, 3: 16}


def _pink_steps(total_stars: int) -> int:
    """Cumulative pink/red star total for ONE aptitude, across the whole
    lineage, -> grade steps at career start. User-supplied 2026-08-27
    reference table: 1â˜… total -> 1 step, 4â˜… -> 2, 7â˜… -> 3, 10â˜… -> 4 (the
    documented max). NOT a per-spark rule -- a single 1â˜… spark and three
    1â˜… sparks from three different lineage members both count toward this
    same running total for that aptitude."""
    if total_stars >= 10:
        return 4
    if total_stars >= 7:
        return 3
    if total_stars >= 4:
        return 2
    if total_stars >= 1:
        return 1
    return 0

# Red (aptitude) sparks: factor_group_id -> the chara_info aptitude field.
# master's own succession_factor_effect carries two tiers per group (value_1 =
# 1 and 2): +1 grade at 1-2â˜…, +2 grades at 3â˜… (the JP wikis' documented rule).
# Aptitude values run 1..8 = G..S; inheritance raises to at most A (7), never S.
_RED_GROUP_TO_APT = {
    11: "proper_ground_turf", 12: "proper_ground_dirt",
    21: "proper_running_style_nige", 22: "proper_running_style_senko",
    23: "proper_running_style_sashi", 24: "proper_running_style_oikomi",
    31: "proper_distance_short", 32: "proper_distance_mile",
    33: "proper_distance_middle", 34: "proper_distance_long",
}
_APT_INHERIT_CAP = 7      # A


def _red_rank_up(star: int) -> int:
    return 2 if star >= 3 else 1


def _raise_aptitude(chara_info: dict, field: str, ranks: int) -> None:
    cur = chara_info.get(field, 1) or 1
    if cur < _APT_INHERIT_CAP:
        chara_info[field] = min(_APT_INHERIT_CAP, cur + ranks)
_BLUE_GROUP_TO_STAT = {1: "speed", 2: "stamina", 3: "power", 4: "guts", 5: "wiz"}
# succession_factor_effect target_type -> the stat CAP it raises (61..65).
_CAP_TARGET_TO_STAT = {61: "speed", 62: "stamina", 63: "power", 64: "guts", 65: "wiz"}


def _spark_cap_bonuses(factor_group_id, star: int) -> dict:
    """{stat: cap_bonus} a spark contributes to stat caps (target_type 61-65),
    read at the effect step for its star. Small per-spark amounts that sum across
    the lineage into the variable cap boost the inspiration shows."""
    out: dict[str, int] = {}
    rows = master_data.query(
        "SELECT target_type, effect_id, value_1 FROM succession_factor_effect "
        "WHERE factor_group_id=? AND target_type IN (61,62,63,64,65)", (factor_group_id,))
    by_tt: dict = {}
    for r in rows:
        by_tt.setdefault(r["target_type"], []).append(r)
    for tt, rs in by_tt.items():
        stat = _CAP_TARGET_TO_STAT.get(tt)
        if not stat:
            continue
        rs.sort(key=lambda r: r["effect_id"])
        pick = None
        for r in rs:
            if r["effect_id"] <= star:
                pick = r
        pick = pick or rs[0]
        if pick["value_1"]:
            out[stat] = out.get(stat, 0) + pick["value_1"]
    return out


def _roster_by_trained_id(viewer_id) -> dict:
    """trained_chara_id -> uma entry (with factor_info_array), across every place
    a parent could come from. The client chooses succession parents from its FULL
    trained-chara collection (load_index.data.trained_chara, ids like 1319), NOT
    the house roster (legacy_roster, ids 2000001+) -- so search that first."""
    full = state_store.get_state(viewer_id) or {}
    out: dict = {}
    sources = []
    # load_index is a lazy key in state.py (see its module docstring), so it is
    # NOT in the dict get_state returned -- it has to be asked for explicitly,
    # or the trained-chara collection the client actually picks parents from
    # would silently look empty here.
    from .load import get_or_seed_blob
    li = get_or_seed_blob(full, viewer_id)
    if isinstance(li, dict):
        tc = (li.get("data") or {}).get("trained_chara")
        if isinstance(tc, list):
            sources.append(tc)
    for key in ("legacy_roster_pre_house", "legacy_roster"):
        if isinstance(full.get(key), list):
            sources.append(full[key])
    for lst in sources:
        for u in lst:
            if isinstance(u, dict):
                tid = u.get("trained_chara_id")
                if tid is not None and tid not in out:
                    out[tid] = u
    return out


def _apply_inheritance(chara_info: dict, viewer_id, start_chara: dict) -> dict:
    """The DETERMINISTIC first-inspiration inheritance from all 6 lineage
    members (2 parents + their 4 grandparents' factor_info_array).

    Blue (stat) sparks add a flat stat bonus by star (1â˜…+5/2â˜…+12/3â˜…+21) AND a
    flat stat-CAP bonus by star (1â˜…+4/2â˜…+9/3â˜…+16 -- user-supplied 2026-08-27
    reference table), both summed across every qualifying spark in the whole
    lineage -- this is a flat per-star table, not the master.mdb
    succession_factor_effect row value, which undershot real in-game numbers
    (a real "Spark of Inspiration" readout showed +17 speed cap/+8 power cap/
    +4 wit cap from a lineage master.mdb rows alone could not reach).

    RED (aptitude) sparks: NOT a max-per-spark or flat "= star" rule (an
    earlier version of this docstring cited Crazyfellow's Chapter 3, "Value
    of inheritance is the same as the â˜… of the gene", but that undersold the
    real mechanic). The real rule (user-supplied 2026-08-27 reference table)
    is a CUMULATIVE star total per aptitude, summed across the whole lineage,
    then converted to grade STEPS via fixed thresholds -- 1â˜… total -> 1 step,
    4â˜… -> 2 steps, 7â˜… -> 3 steps, 10â˜… -> 4 steps (the reference table's own
    documented max: "up to a maximum of 4 steps"). Capped at A -- inheritance
    never grants S; mid-run's red is a wholly DIFFERENT 1-5 random-value roll
    _roll_inspiration already models via _red_rank_up -- do not conflate the
    two, they are documented as separate mechanics.

    Applied silently at career start so the gains the inspiration cutscene
    shows are actually on chara_info from turn 1.

    SCOPE, user-corrected 2026-08-27 (real in-game evidence: a Wit cap/stat
    gain appeared despite NEITHER direct parent carrying any wisdom-related
    spark, only explainable by a grandparent's): blue/red/cap DO draw from
    the full 6-member lineage, same enumeration _roll_inspiration uses --
    this was previously parents-only, silently dropping every grandparent
    stat/aptitude contribution at start.

    GREEN remains narrower than the rest, confirmed separately: at career
    start you ONLY get the unique skill each DIRECT PARENT owns herself --
    never a green spark she is merely carrying from HER OWN ancestry (an
    inherited unique that isn't hers to begin with), and never a
    grandparent's at all. Those are only eligible at the turn 31/55
    Inspiration events (_roll_inspiration), which can raise a parent's own
    skill further OR introduce a genuinely new one from a grandparent."""
    roster = _roster_by_trained_id(viewer_id)
    blue: dict[str, int] = {}
    caps: dict[str, int] = {}       # stat -> cap bonus (summed)
    hints: dict[tuple[int, int], int] = {}   # (skill group_id, rarity) -> hint level
    red_stars: dict[str, int] = {}  # aptitude field -> cumulative star total (summed)
    parents = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        pid = start_chara.get(key)
        parent = roster.get(pid) if pid else None
        if parent:
            parents.append(parent)

    # (factor_list, is_direct_parent, parent_own_skill_ids) for all 6 members.
    # BUG FIXED 2026-08-27: grandparents come from each parent's own POSITION
    # 10 AND 20 entries in HER succession_chara_array -- that is her own two
    # parents, i.e. this trainee's grandparents. This is trained_chara.py's
    # OWN already-correct, already-documented convention (_real_ancestry:
    # "Grandparents come from each parent's own position-10/20 entries"),
    # which re-labels exactly those two entries onto positions 11/12 (or
    # 21/22) of the NEW trainee's OWN array once she's created. Filtering by
    # position_id 11/12/21/22 on a PARENT's array (the pre-existing
    # _roll_inspiration convention this was copied from) reads the WRONG
    # generation: those slots hold the PARENT's own grandparents -- this
    # trainee's GREAT-grandparents, one generation past what the game tracks
    # (6 lineage members total: 2 parents + 4 grandparents, never more,
    # confirmed in Crazyfellow's guide: "The effects of compatibility and
    # inheritance genes stop at the grandparent level").
    members = []
    for i, parent in enumerate(parents):
        parent_chara = (parent.get("card_id") or 0) // 100
        own_skill_ids = set()
        if parent_chara:
            offset = (parent_chara - 1000) * 10 + 1
            own_skill_ids = {900000 + offset, 100000 + offset}
        members.append((parent.get("factor_info_array") or [], True, own_skill_ids))
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        gps = [by_pos[p] for p in (10, 20) if p in by_pos]
        for a in gps:
            members.append((a.get("factor_info_array") or [], False, set()))

    for factor_list, is_parent, own_skill_ids in members:
        for f in factor_list:
            sf = master_data.query_one(
                "SELECT factor_type, factor_group_id, rarity FROM succession_factor WHERE factor_id=?",
                (f.get("factor_id"),))
            if not sf:
                continue
            star = sf["rarity"]
            if sf["factor_type"] == 3:                       # green unique -> skill hint
                if not is_parent:
                    continue                                 # grandparents never grant green at start
                eff = master_data.query_one(
                    "SELECT value_1 FROM succession_factor_effect "
                    "WHERE factor_group_id=? AND target_type=41 ORDER BY effect_id LIMIT 1",
                    (sf["factor_group_id"],))
                # Not this parent's OWN unique skill -> not eligible at start
                # (see docstring). Skip the WHOLE spark, including any cap
                # bonus it would otherwise contribute -- it isn't hers to pass
                # down yet, not just its skill component.
                if not eff or eff["value_1"] not in own_skill_ids:
                    continue
                key = master_data.skill_tip_key(eff["value_1"])
                hints[key] = max(hints.get(key, 0), min(3, star))
            # Stat-cap bonus is the FLAT per-star table (see docstring), and
            # BLUE-ONLY -- verified 2026-08-27 against this exact lineage's
            # real "Spark of Inspiration" readout: including green's own
            # succession_factor_effect cap-target rows (Anchors Aweigh
            # targets both Stamina and Power caps) overshot Power cap to +17
            # against a real +8; excluding green and using ONLY the two blue
            # Power sparks in this lineage (2 x star1 = 2x4) lands exactly on
            # +8. Speed and Wit caps (no green spark touches either) already
            # matched exactly either way, which is why the green-inclusive
            # version passed those two silently.
            if sf["factor_type"] == 1:                       # blue stat spark
                stat = _BLUE_GROUP_TO_STAT.get(sf["factor_group_id"])
                if stat:
                    blue[stat] = blue.get(stat, 0) + _INHERIT_BLUE.get(star, 0)
                    caps[stat] = caps.get(stat, 0) + _CAP_INCREASE_BY_STAR.get(star, 0)
            elif sf["factor_type"] == 2:                     # red aptitude spark
                field = _RED_GROUP_TO_APT.get(sf["factor_group_id"])
                if field:
                    # Cumulative star total per aptitude, summed across the
                    # whole lineage (see docstring) -- converted to grade
                    # steps by _pink_steps below, once every spark is in.
                    red_stars[field] = red_stars.get(field, 0) + star

    # Raise caps first (so the stat bonus can fill up to the new cap).
    for stat, cbonus in caps.items():
        key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[key] = chara_info.get(key, 1200) + cbonus
    for stat, val in blue.items():
        cap = chara_info.get("max_wiz" if stat == "wiz" else f"max_{stat}", 9999)
        chara_info[stat] = min(chara_info.get(stat, 0) + val, cap)
    red = {field: _pink_steps(total) for field, total in red_stars.items()}
    for field, ranks in red.items():
        if ranks:
            _raise_aptitude(chara_info, field, ranks)
    tips = chara_info.setdefault("skill_tips_array", [])
    for (gid, rarity), lvl in hints.items():
        existing = next((t for t in tips if t.get("group_id") == gid
                         and t.get("rarity") == rarity), None)
        if existing:
            existing["level"] = max(existing.get("level", 0), lvl)
        else:
            tips.append({"group_id": gid, "rarity": rarity, "level": lvl})
    return {"blue": blue, "caps": caps, "hints": hints, "red": red}


# --- Mid-run inspiration (inheritance events, turns 31 & 55) -----------------
# Real wire shapes from THREE independent captures (UmaDumpy 20260721_143256
# turn 31; Icarus full-career trace turns 31 AND 55; the old team-mode
# capture): turn 31 uses event_id 7040, turn 55 uses 7041, both story
# 400000040 (same cutscene), chara_id 0, play_timing 1, empty choice_array,
# and -- uniquely among all events -- a non-null succession_event_info
# {"effect_type": 1 or 2}. Each is WRAPPED by per-chara pre/post events
# (7044/7045 at 31, 7046/7047 at 55, stories 500000000+chara*1000+732/733).
# Serving 7040/7041 applies NOTHING; the response to the client's commit
# (check_event {event_id, choice_number: 0}) carries all gains at once plus
# event_effected_factor_array = the fired sparks by lineage position
# (10/20 parents, 11/12/21/22 grandparents) for the animation.
_INSPIRATION_EVENTS = {31: (7044, 7040, 7045), 55: (7046, 7041, 7047)}
_INSPIRATION_MAIN_IDS = (7040, 7041)
_INSPIRATION_STORY = 400000040
INSPIRATION_CTX_KEY = "active_inspiration"
INSPIRATION_FIRED_KEY = "inspiration_turns_fired"


@career_events.resolver("inspiration_main")
def _resolve_inspiration_main(full_state, chara_info, event, choice_number, **kw):
    """The silent bridging event (7040 turn 31 / 7041 turn 55) that actually
    carries the rolled gains -- moved verbatim from the old MID-RUN
    INSPIRATION COMMIT branch. Inputs ride on the Event's own payload
    (rolled at queue time in _maybe_queue_inspiration) instead of
    INSPIRATION_CTX_KEY. Always returns "factors" (even empty) because the
    old branch set event_effected_factor_array unconditionally."""
    payload = event.payload or {}
    event_engine.apply_choice(chara_info, {"choices": [{"effects": payload.get("effects") or []}]}, 1,
                              full_state=full_state)
    for field, ranks in (payload.get("aptitudes") or {}).items():
        _raise_aptitude(chara_info, field, ranks)
    # NOTE: apply_choice's stat effects (above) already ran against the OLD
    # caps, so a current-stat gain here can't overflow into a cap this same
    # proc just raised -- _apply_inheritance's turn-1 version raises caps
    # first for exactly that reason, but nothing captured confirms whether
    # the real server reorders this for the mid-run event too, so this is
    # left in the simpler existing commit order rather than restructuring
    # apply_choice's call around it.
    for stat, cbonus in (payload.get("caps") or {}).items():
        key = "max_wiz" if stat == "wiz" else f"max_{stat}"
        chara_info[key] = chara_info.get(key, 1200) + cbonus
    return {"factors": payload.get("factors") or []}

# Per-spark base proc rates by star at 0 compatibility (Crazyfellow guide's
# Polaris/Shoppo dataset, cross-checked against Cygames' 2021 patent):
# final chance = base * (1 + compatibility_score/100), each spark rolled
# INDEPENDENTLY using the score of the lineage member that owns it.
_INSPIRE_BASE_RATE = {
    1: (0.70, 0.80, 0.90),   # blue stat sparks
    2: (0.01, 0.03, 0.05),   # red aptitude sparks (guide's base 1/3/5%)
    3: (0.05, 0.10, 0.15),   # green unique-skill sparks
    4: (0.03, 0.06, 0.09),   # white skill sparks
}
_INSPIRE_BLUE_MAX = {1: 10, 2: 16, 3: 28}   # mid-run blue roll: 1..max by star


@functools.lru_cache(maxsize=512)
def _pair_compat_points(chara_a: int, chara_b: int) -> int:
    """Compatibility points between two characters -- sum of relation_point
    over every succession_relation both belong to (verified worked example:
    1001+1002=27, 1001+1003=25, 1002+1003=20 -> total 72 = rank 2 'circle')."""
    if not chara_a or not chara_b:
        return 0
    row = master_data.query_one(
        "SELECT COALESCE(SUM(r.relation_point), 0) AS pts FROM succession_relation r "
        "WHERE EXISTS (SELECT 1 FROM succession_relation_member m "
        "  WHERE m.relation_type=r.relation_type AND m.chara_id=?) "
        "AND EXISTS (SELECT 1 FROM succession_relation_member m "
        "  WHERE m.relation_type=r.relation_type AND m.chara_id=?)",
        (chara_a, chara_b))
    return row["pts"] if row else 0


def _roll_inspiration(chara_info: dict, viewer_id, start_chara: dict) -> dict | None:
    """Roll one mid-run inspiration: every spark of all 6 lineage members
    (2 parents + 4 grandparents) rolls independently at
    base_rate(star) * (1 + member_compatibility/100). The member score is the
    documented approximation of the guide's hidden per-member points:
    parent_i = pts(trainee, parent_i) + pts(parent_1, parent_2)/2;
    grandparents use half their parent's score (the guide's observed ~half
    proc rate for GPs). Procced blue sparks roll 1-10/1-16/1-28 stats by
    star; green/white sparks grant that member's skill hint at +1..+5 (a
    skill already LEARNED converts to a small 1-9 stat gain instead, per the
    guide). Returns {"effects", "factors", "gold"} -- gold means a 3-star
    spark procced (the guide's finding: the gold cutscene is an indicator,
    not a bonus) -- or None when no parents exist to inherit from."""
    roster = _roster_by_trained_id(viewer_id)
    trainee_chara = (chara_info.get("card_id") or 0) // 100
    learned = {s.get("skill_id") for s in chara_info.get("skill_array") or []}

    parents = []
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        pid = (start_chara or {}).get(key)
        parent = roster.get(pid) if pid else None
        if parent:
            parents.append(parent)
    if not parents:
        return None
    parent_charas = [(p.get("card_id") or 0) // 100 for p in parents]

    # (position, factor_info_array, member_score) for all 6 lineage members.
    members = []
    for i, parent in enumerate(parents):
        pos = 10 * (i + 1)                       # 10 / 20
        score = _pair_compat_points(trainee_chara, parent_charas[i])
        if len(parent_charas) == 2:
            score += _pair_compat_points(parent_charas[0], parent_charas[1]) // 2
        members.append((pos, parent.get("factor_info_array") or [], score))
        # BUG FIXED 2026-08-27: grandparents are each parent's own POSITION
        # 10 AND 20 entries (her own two parents), not 11/12/21/22 (her own
        # grandparents -- this trainee's great-grandparents, one generation
        # past what the game tracks). See _apply_inheritance's matching fix
        # and trained_chara._real_ancestry, which already documents this
        # convention correctly on the WRITE side.
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        for offset, src_pos in ((1, 10), (2, 20)):
            a = by_pos.get(src_pos)
            if a:
                members.append((pos + offset, a.get("factor_info_array") or [], score // 2))

    blue_totals: dict[str, int] = {}
    cap_totals: dict[str, int] = {}
    red_ups: dict[str, int] = {}
    effects: list = []
    factors: dict[int, list] = {}
    gold = False
    for pos, factor_list, score in members:
        for f in factor_list:
            sf = master_data.query_one(
                "SELECT factor_type, factor_group_id, rarity FROM succession_factor "
                "WHERE factor_id=?", (f.get("factor_id"),))
            if not sf or sf["factor_type"] not in _INSPIRE_BASE_RATE:
                continue
            star = max(1, min(3, sf["rarity"]))
            chance = _INSPIRE_BASE_RATE[sf["factor_type"]][star - 1] * (1 + score / 100)
            if random.random() >= min(1.0, chance):
                continue
            # STAT CAPS (user-reported 2026-08-26: "stat caps are wrong
            # completely"). BUG FIXED: every procced spark of ANY type can
            # carry a real succession_factor_effect target_type 61-65 row
            # (confirmed against the live master.mdb -- e.g. group 1's blue
            # Speed spark pairs a current-stat row with a MaxSpeed row at
            # every tier, and green/unique groups like 100701 "Anchors
            # Aweigh!" carry max-stat rows alongside their skill row too).
            # _apply_inheritance (the turn-1 START inheritance, a separate but
            # structurally identical roll) already reads this via
            # _spark_cap_bonuses -- this mid-run roll never called it at all,
            # so no spark here has EVER raised a cap, on top of the current
            # code's cap contributions being entirely absent rather than just
            # wrong. Best-per-stat, not summed, matching _apply_inheritance's
            # own established rule (summing exploded a cheat parent's speed
            # cap to 1501 against real captures' 1400-1420).
            for stat, cbonus in _spark_cap_bonuses(sf["factor_group_id"], star).items():
                cap_totals[stat] = max(cap_totals.get(stat, 0), cbonus)
            if sf["factor_type"] == 1:
                stat = _BLUE_GROUP_TO_STAT.get(sf["factor_group_id"])
                if not stat:
                    continue
                blue_totals[stat] = blue_totals.get(stat, 0) + \
                    random.randint(1, _INSPIRE_BLUE_MAX[star])
            elif sf["factor_type"] == 2:         # red -> aptitude grade up
                field = _RED_GROUP_TO_APT.get(sf["factor_group_id"])
                # Already at the inheritance ceiling: nothing to give, so don't
                # burn the proc (or report a factor that visibly did nothing).
                if not field or (chara_info.get(field, 1) or 1) >= _APT_INHERIT_CAP:
                    continue
                red_ups[field] = max(red_ups.get(field, 0), _red_rank_up(star))
            else:                                # green/white -> skill hint
                eff = master_data.query_one(
                    "SELECT value_1 FROM succession_factor_effect "
                    "WHERE factor_group_id=? AND target_type=41 ORDER BY effect_id LIMIT 1",
                    (sf["factor_group_id"],))
                if not eff or not eff["value_1"]:
                    continue
                if eff["value_1"] in learned:    # learned already: small stat instead
                    stat = random.choice(_ALL_STATS)
                    blue_totals[stat] = blue_totals.get(stat, 0) + random.randint(1, 9)
                else:
                    effects.append({"type": "skill_hint", "skill_id": eff["value_1"],
                                    "value": random.randint(1, 5)})
            gold = gold or star >= 3
            factors.setdefault(pos, []).append({"factor_id": f.get("factor_id"), "level": 0})

    effects = [{"type": s, "value": v} for s, v in blue_totals.items()] + effects
    return {
        "effects": effects,
        # Aptitude grades and stat caps aren't "effects" the engine can apply
        # (they're not stats or hints), so they ride separately and are
        # applied at commit -- same pattern as aptitudes.
        "aptitudes": red_ups,
        "caps": cap_totals,
        "factors": [{"position": p, "factor_info_array": fl} for p, fl in sorted(factors.items())],
        "gold": gold,
    }


def _maybe_queue_inspiration(response: dict, full_state: dict, career: dict,
                             viewer_id, new_turn) -> None:
    """If the career just ARRIVED at an inspiration turn (31/55) and hasn't
    fired it yet, roll it now, queue the real 3-event sequence (pre-wrapper ->
    inheritance -> post-wrapper, exact captured shapes) behind whatever this
    response already shows, and stash the rolled outcome for the commit (see
    handle_ura_check_event's inspiration branch). Fired-marker on the career
    data makes this idempotent across resyncs/re-serves."""
    if new_turn not in _INSPIRATION_EVENTS:
        return
    fired = career["data"].setdefault(INSPIRATION_FIRED_KEY, [])
    if new_turn in fired:
        return
    chara_info = career["data"]["chara_info"]
    roll = _roll_inspiration(chara_info, viewer_id,
                             full_state.get(START_CHARA_STATE_KEY) or {})
    if roll is None:
        return
    fired.append(new_turn)
    pre_id, main_id, post_id = _INSPIRATION_EVENTS[new_turn]
    chara = (chara_info.get("card_id") or 0) // 100
    pre = _scenario_event_entry(pre_id, 500000000 + chara * 1000 + 732,
                                play_timing=1, choices=[], chara_id=chara)
    main = _scenario_event_entry(main_id, _INSPIRATION_STORY, play_timing=1, choices=[])
    main["succession_event_info"] = {"effect_type": 2 if roll["gold"] else 1}
    post = _scenario_event_entry(post_id, 500000000 + chara * 1000 + 733,
                                 play_timing=1, choices=[], chara_id=chara)
    # PRIO_SCENARIO -- the SAME tier Grand Live's own scenario beats use (see
    # scenarios/grand_live/producers.py). Before this migration the two rode
    # separate queues with different drain precedence (the unified pipeline
    # always drained before this family's old EXTRA_EVENTS_KEY), so a beat
    # left over in the other queue could cut in front of this chain mid-way
    # (live-reported: "Whose Wish?" -- a Grand Live turn-30 beat -- surfaced
    # between Inspiration's pre-wrapper and "Spark of Inspiration"). Sharing
    # one priority-sorted queue removes the race instead of special-casing it.
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=pre_id, story_id=pre.get("story_id"), raw=pre,
        priority=career_events.PRIO_SCENARIO, source="inspiration"))
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=main_id, story_id=main.get("story_id"), raw=main,
        priority=career_events.PRIO_SCENARIO, resolver="inspiration_main",
        source="inspiration",
        payload={"effects": roll["effects"], "aptitudes": roll.get("aptitudes") or {},
                 "caps": roll.get("caps") or {}, "factors": roll["factors"]}))
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=post_id, story_id=post.get("story_id"), raw=post,
        priority=career_events.PRIO_SCENARIO, source="inspiration"))


def _roll_distribution(chara_info: dict, turn: int, specialty_bonus: int = 0,
                       salt: int = 0) -> dict:
    """Assign each support card to a facility (0-4) or 'away' for this turn ->
    {facility_index: [positions]}. Weighted like the real game: base 100 per
    facility + the card's specialty_priority on its own type, 50 for 'away'.
    Seeded by turn so a turn's placement is stable if rebuilt.

    `specialty_bonus` is Grand Live's "Specialty Rate Up" Live Bonus. It rides
    the SAME term as the card's own specialty_priority -- more weight on its own
    type -- which is how the mechanic reads in game: cards show up on their own
    facility more often, so rainbows (bond >= 80 AND own type) come up more.

    Also adds each card's own EFFECT_SPECIALTY_PRIORITY (19) support_card_
    unique_effect bonus once its real level unlocks it (e.g. Kitasan Black
    SSR 30028 +20 at level 30, on top of its own base 80 at max level --
    100 total, user-verified 2026-08-18) -- training_formula.py never named
    type 19 before 2026-08-19, so this used to silently miss the unique half."""
    rng = random.Random((int(chara_info.get("card_id", 0)) << 8) ^ int(turn)
                       ^ (int(salt or 0) << 24))
    dist = {0: [], 1: [], 2: [], 3: [], 4: []}
    levels = _deck_support_card_levels(chara_info)
    # BUG FIXED 2026-08-23 (live-reported: Light Hello -- see
    # _hide_ungated_support_card_rows -- still appeared as a training partner
    # before turn 4 even with that fix in place). This loop used to place
    # EVERY deck card regardless of is_appear, because is_appear was treated
    # as a purely cosmetic "have I met her" flag for NPCs only -- but for a
    # deck card whose character hasn't story-unlocked yet (a gated NPC's own
    # support card), is_appear IS the "can she physically show up training"
    # gate, and nothing was reading it. A card with no evaluation row at all
    # defaults to appearing (every ordinary real card has is_appear=1 from
    # turn 1; only the gated case ever sets it to 0).
    appear_by_position = {e.get("target_id"): e.get("is_appear", 1)
                          for e in chara_info.get("evaluation_info_array") or []}
    for c in chara_info.get("support_card_array", []):
        sid, pos = c.get("support_card_id"), c.get("position")
        if not appear_by_position.get(pos, 1):
            continue
        weights = [_BASE_FACILITY_WEIGHT] * 5 + [_AWAY_WEIGHT]
        t = training_formula.card_type_index(sid)
        if t is not None:
            weights[t] += training_formula.card_effect(sid, "specialty_priority", 0) or 0
            weights[t] += int(specialty_bonus or 0)
            unique_lv = levels.get(sid)
            if unique_lv:
                weights[t] += training_formula.unique_effect(
                    sid, training_formula.EFFECT_SPECIALTY_PRIORITY, level=unique_lv)
        pick = rng.choices(range(6), weights=weights, k=1)[0]
        if pick != 5:
            dist[pick].append(pos)
    return dist


def _place_unlocked_npcs(unlocked_npcs, turn: int) -> dict:
    """{facility command_id: [npc target_ids]} for this turn. Each unlocked
    scenario NPC (Director 102, Happy Meek 2001) rolls a facility or 'away'
    (weight 50), seeded by (turn, target_id) so it's stable within the turn.
    They ride in the SAME training_partner_array as support cards, which is how
    the client draws them in a facility (confirmed from the capture)."""
    # Happy Meek (VERSUS_NPCS) is placed separately at serve time (_apply_versus)
    # so her partner slot, duel marker, and duel trigger always agree; only the
    # plain partners (e.g. Director) roll a facility here.
    plain = [t for t in (unlocked_npcs or ()) if t not in single_mode_events.VERSUS_NPCS]
    out = {cmd: [] for cmd in TRAINING_COMMAND_IDS}
    for fac, tid in single_mode_events.npc_placements(turn, plain).items():
        if fac in out:
            out[fac].append(tid)
    return out


# At most FIVE bodies stand in one facility at a time -- support cards,
# scenario NPCs and Grand Live's recruited supporters all share the same five
# portrait slots the training screen draws. A deck can only put 6 cards in one
# facility on a freak roll and that case predates this; what the cap is here
# for is the supporters, who arrive by the dozen over a full run (13 seated by
# the last concert) and would otherwise pile a facility ten deep.
_MAX_FACILITY_PARTNERS = 5


def _npc_supporters(full_state: dict, chara_info: dict) -> list:
    """The scenario NPCs who have joined our cause and own NO equipped support
    card this run, in the order they joined.

    One who IS in the deck is already present as her card -- she must not also
    appear as a second, cardless copy of herself (the "2 Light Hellos" shape of
    bug, see _hide_ungated_support_card_rows). Empty in a scenario that recruits
    nobody, which is base.Scenario's default."""
    return scenarios.for_chara(chara_info).npc_supporters(full_state, chara_info)


def _npc_supporter_placements(full_state: dict, chara_info: dict, turn: int,
                              occupied: dict = None) -> dict:
    """{facility command_id: [chara_id, ...]} -- where this turn's uncarded
    Grand Live supporters are standing.

    User-confirmed 2026-08-24 and directed 2026-08-28: a recruited Umamusume
    who is not one of your support cards still SHOWS UP IN TRAINING, exactly
    like the Director/Reporter/Meek do in URA. She rolls a facility the same
    way a support card does -- 100 per facility against 50 for 'away' (the
    same _BASE_FACILITY_WEIGHT/_AWAY_WEIGHT the deck roll uses, with no
    specialty term: she has no card, so she has no specialty).

    She counts as a BODY and nothing more: she rides the facility's
    training_partner_array so the client draws her, and she feeds
    calculate_training_gain's npc_supporter_count (the +5%-per-body term and
    the "+N% per card in this facility" unique effect). She carries no bond,
    no friendship/rainbow, no hints, no stat bonuses -- see
    training_formula.calculate_training_gain's npc_supporter_count docs.

    `occupied` is {command_id: bodies already standing there} (support cards +
    scenario NPCs), so the 5-slot cap counts EVERYONE, not just supporters. A
    supporter who rolls a full facility simply doesn't train with us this turn
    rather than displacing a card.

    Deterministic by (turn, chara_id) -- the served screen and the exec_command
    that trains against it must agree, and this is rebuilt on both."""
    supporters = _npc_supporters(full_state or {}, chara_info)
    if not supporters:
        return {}
    taken = dict(occupied or {})
    weights = [_BASE_FACILITY_WEIGHT] * 5 + [_AWAY_WEIGHT]
    out: dict = {}
    for chara_id in supporters:
        pick = random.Random((int(turn) << 16) ^ int(chara_id)).choices(
            range(6), weights=weights, k=1)[0]
        if pick == 5:
            continue                          # away this turn
        fac = TRAINING_COMMAND_IDS[pick]
        if taken.get(fac, 0) >= _MAX_FACILITY_PARTNERS:
            continue                          # facility full -- she sits it out
        taken[fac] = taken.get(fac, 0) + 1
        out.setdefault(fac, []).append(chara_id)
    return out


def _ensure_supporter_eval_rows(chara_info: dict, supporter_ids) -> None:
    """Give each uncarded supporter her own chara_info.evaluation_info_array
    row, keyed target_id == chara_id -- the SAME id grand_live's _live_members
    already registers her under in live_data_set.evaluation_info_array, which
    is how the client resolves a training_partner_array entry to a name.
    Without the row the portrait has nothing to render from.

    is_appear is 1 from the moment she joins: joining IS her unlock, unlike the
    NPC band's separate "can now appear in training" flip. `evaluation` stays 0
    forever -- she owns no card, so she has no bond gauge to fill (the exact
    thing that read wrong when a cardless Light Hello was given one)."""
    if not supporter_ids:
        return
    arr = chara_info.setdefault("evaluation_info_array", [])
    have = {e.get("target_id") for e in arr if isinstance(e, dict)}
    for chara_id in supporter_ids:
        if chara_id in have:
            continue
        have.add(chara_id)
        arr.append({"target_id": chara_id, "training_partner_id": chara_id,
                    "evaluation": 0, "is_outing": 0, "story_step": 0,
                    "is_appear": 1, "group_outing_info_array": []})


# Summer camp: on these turns the 5 facilities become the level-5 camp facilities
# (command ids 601-605). Confirmed from the Icarus trace (Classic yr turns 37-40,
# Senior yr turns 61-64). The client sends 601-605 during camp; serving 101-106
# makes camp training unresponsive.
_CAMP_TURNS = frozenset({37, 38, 39, 40, 61, 62, 63, 64})
_CAMP_ID_BY_BASE = {101: 601, 105: 602, 102: 603, 103: 604, 106: 605}
_CAMP_BASE_BY_ID = {v: k for k, v in _CAMP_ID_BY_BASE.items()}
_CAMP_LEVEL = 5
# Summer camp's merged 'Rest & Recreation' command: NOT a 7xx rest id --
# it's outing command_group 304 (single_mode_outing_set id 2, the camp
# outing set, whose ONLY member is 304; single_mode_training row 304 has
# cutin 103040 + the camp-swimsuit dress, matching camp top_cloth_id=4).
# The camp turns themselves are flagged in single_mode_turn by rest_type=0 +
# health_room_type=0 (separate rest/infirmary hidden) with outing_set_id=2.
_CAMP_OUTING_GROUP = 304
_CAMP_REST_ENERGY = 40
_CAMP_REST_MOOD = 1

# Facility level-up: every _LEVEL_UP_TRAINS trains in a facility raises its level
# by 1 (cap 5). The level is served in command_info[].level (the client plays its
# own level-up flourish when it rises) and scales the base gain (via
# training_formula.FACILITY_LEVEL_MULT). Persisted on career_state["data"].
FACILITY_LEVELS_KEY = "facility_levels"   # {str(base_cmd): level}
FACILITY_TRAINS_KEY = "facility_trains"   # {str(base_cmd): count toward next level}
_LEVEL_UP_TRAINS = 4
_MAX_FACILITY_LEVEL = 5

# EFFECT_FRIENDSHIP_STACKING (106, see training_formula.py) -- how many times a
# card has proc'd friendship training this career. On full_state (NOT chara_info
# -- server-internal, never sent to the client), {str(support_card_id): count}.
# Incremented ONLY at genuine turn commit (_simulate_exec_command's bond-gain
# loop), never during a training-screen preview.
FRIENDSHIP_STACK_KEY = "friendship_stack_counts"


def _is_camp_turn(turn) -> bool:
    return turn in _CAMP_TURNS


def _facility_levels(career_data: dict) -> dict:
    """{str(base_cmd): level} for the 5 facilities, seeded to level 1."""
    lv = career_data.get(FACILITY_LEVELS_KEY)
    if not isinstance(lv, dict):
        lv = {str(cmd): 1 for cmd in TRAINING_COMMAND_IDS}
        career_data[FACILITY_LEVELS_KEY] = lv
    return lv


def _facility_level(career_data: dict, base_cmd: int) -> int:
    return _facility_levels(career_data).get(str(base_cmd), 1)


def _record_facility_train(career_data: dict, base_cmd: int,
                           chara_info: dict | None = None) -> bool:
    """Count a training in a facility; level it up every _LEVEL_UP_TRAINS trains
    (cap 5). Camp trains count toward the base facility too. Returns True when
    the facility just leveled. Also mirrors the new level into
    chara_info.training_level_info_array -- the field the CLIENT actually
    renders the facility level from (live-reported: internal levels rose and
    scaled gains, but the display stayed at the career-start snapshot's 1).

    Pass the chara_info that's about to be SERVED. Mirroring onto
    career_data["chara_info"] was useless: exec_command replaces that key with
    its own working copy a few lines later, so the new level was written to a
    dict that was immediately discarded -- the level-up event played and the
    facility kept showing its old level (#8)."""
    if base_cmd not in _COMMAND_TO_TYPE:
        return False
    levels = _facility_levels(career_data)
    trains = career_data.setdefault(FACILITY_TRAINS_KEY, {})
    key = str(base_cmd)
    if levels.get(key, 1) >= _MAX_FACILITY_LEVEL:
        return False
    n = trains.get(key, 0) + 1
    leveled = n >= _LEVEL_UP_TRAINS
    if leveled:
        n = 0
        levels[key] = min(_MAX_FACILITY_LEVEL, levels.get(key, 1) + 1)
    trains[key] = n
    if leveled:
        target = chara_info if isinstance(chara_info, dict) else career_data.get("chara_info")
        tla = (target if isinstance(target, dict) else {}).setdefault(
            "training_level_info_array", [])
        entry = next((e for e in tla if e.get("command_id") == base_cmd), None)
        if entry:
            entry["level"] = levels[key]
        else:
            tla.append({"command_id": base_cmd, "level": levels[key]})
    return leveled


# Race command (single_mode_program-determined career races -- Debut, Kikuka
# Sho, etc.) + the other commands a MANDATORY race turn locks out.
_RACE_COMMAND_ID = 401
_FORCEABLE_NON_TRAINING_IDS = {301, 390, 701, 304}  # outings, rest, camp rest&rec


@functools.lru_cache(maxsize=64)
def _forced_race_turns(route_race_id_array: tuple) -> frozenset:
    """{turn, ...} of this route's MANDATORY, determined races -- the ones that
    force race_entry (all training/outing/rest disabled, only the race command
    enabled), same as the real captured debut turn. Everything (classification,
    passive-goal exclusion, finals/JBC group resolution, determine_race
    alternative pruning) lives in _route_goal_rows -- this is just its turn
    set, and now INCLUDES the URA finals turns 74/76/78 (the old target_type=3
    TODO) and the JBC turn 69 for the two routes that have it."""
    return frozenset(row[0] for row in _route_goal_rows(route_race_id_array))


def _poll_career_events(response: dict, full_state: dict, career: dict,
                        payload: dict, turn=None, endpoint: str = "") -> list:
    """Ask every registered producer what fires this turn, then let the first
    accepted event claim the response's display slot if nothing else has it.

    The claim rule is the old _queue_turn_event behaviour preserved exactly: an
    event only becomes the IMMEDIATE display when the turn had nothing else to
    show; otherwise it waits in the queue and surfaces via _drain_pending once
    the current chain finishes. That ordering is what makes the client play the
    turn's own action first and the meta cutscene last."""
    chara_info = career["data"]["chara_info"]
    # An ALREADY-ACTIVE, unresolved CE event takes priority over producing
    # anything new, and must be RE-OFFERED here even when nothing fires this
    # call. poll() below only reports newly-PRODUCED events -- it is silent
    # about anything still sitting active from an earlier response -- so if
    # THAT response's unchecked_event_array never actually got acted on
    # client-side (the client kept training instead), the event would
    # otherwise sit in `active` forever: every later exec_command finds
    # nothing NEW to produce and returns early, and check_event only re-offers
    # it if the player happens to call check_event before another exec_command
    # (live-reported: "The Grand Concert Begins!" was served exactly once, on
    # the exec_command that produced it, then never seen again across two more
    # turns of training).
    # Before anything is offered: let the scenario re-stamp a queued event
    # whose play_timing this kind of response cannot carry. Withholding such an
    # event is only correct while some LATER response can still take it -- for
    # a beat that freezes the turn until it is played, "never offered" and
    # "dropped by the client" brick the career the same way.
    scenarios.for_chara(chara_info).retime_queued(full_state, endpoint)
    shown = response.get("data", {}).get("unchecked_event_array") or []
    if not shown:
        already_active = career_events.active(full_state)
        # ...but only onto a response this event's play_timing allows. Re-
        # offering a timing-3 event on exec_command is what turned "the client
        # ignored it once" into an unbreakable loop: it is dead on arrival
        # there, so it never resolves, so it is re-offered again next turn.
        if already_active is not None and not career_events.servable_on(
                already_active.to_dict(), endpoint):
            already_active = None
        if already_active is not None:
            response.setdefault("data", {})["unchecked_event_array"] = [already_active.wire_entry()]
            return [already_active]
    # `turn` is the turn the CLIENT was on when it sent this action; chara_info
    # has already been advanced past it by the time we get here, so passing that
    # instead fires every schedule a turn early (see career_events.Ctx).
    client_turn = turn if turn is not None else payload.get("current_turn")
    if client_turn is None:
        client_turn = max(1, (chara_info.get("turn") or 1) - 1)
    ctx = career_events.Ctx(
        viewer_id=payload.get("viewer_id"), full_state=full_state, career=career,
        chara_info=chara_info, turn=int(client_turn),
        advanced_turn=chara_info.get("turn") or (int(client_turn) + 1),
        payload=payload)
    # This response kind OPENS the chain, whether or not it ends up carrying an
    # event -- see career_events.note_endpoint.
    career_events.note_endpoint(full_state, endpoint)
    accepted = career_events.poll(ctx)
    if not accepted:
        return []
    shown = response.get("data", {}).get("unchecked_event_array") or []
    if not shown:
        wire, _ev = career_events.serve_next(full_state, endpoint)
        if wire is not None:
            response.setdefault("data", {})["unchecked_event_array"] = [wire]
    return accepted


def _forced_training_gain():
    """TESTING KNOB -- `force_training_gain` in client_config.json.

    Pins every facility's stat preview to a fixed number. This is genuinely
    SERVER-side: the client renders params_inc_dec_info_array verbatim and
    exec_command applies straight from the same array, so setting it here
    changes both the number shown AND the number gained. The trainee's stat
    caps still apply on the way in (see _apply_preview_gains), so a huge value
    fills to the cap rather than overflowing it.

    Null (the default) means the real formula decides -- the only setting that
    should ever be committed."""
    value = config.get("force_training_gain")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("client_config.json force_training_gain is not an integer: %r",
                    value)
        return None


def _build_training_command_info(chara_info: dict, template_command_info, distribution: dict,
                                 unlocked_npcs=(), turn: int = 1,
                                 facility_levels: dict = None, camp: bool = False,
                                 race_history=(), training_bonus: dict = None,
                                 friendship_bonus: int = 0,
                                 support_card_levels: dict = None,
                                 friendship_stacks: dict = None,
                                 full_state: dict = None) -> list:
    """command_info_array with per-facility training previews computed from the
    cards ACTUALLY placed in each facility this turn (distribution) and their
    live bonds. Unlocked scenario NPCs (Director/Happy Meek) are added to the
    facility they're placed in (training_partner_array). Non-training entries
    (races/rest/outing) are kept from the template. This IS the training screen
    display, and exec_command applies straight from it.

    support_card_levels: see training_formula.calculate_training_gain -- the
    owned cards' REAL levels (_support_card_levels(full_state)), for
    support_card_unique_effect gating. None just skips unique bonuses.

    friendship_stacks: see training_formula.calculate_training_gain -- EFFECT_
    FRIENDSHIP_STACKING's (106) running per-card counter (_friendship_stacks
    (full_state)). None just skips that bonus.

    full_state: only for _npc_supporter_facility_counts (Grand Live's
    recruited-but-uncarded supporters headcount, see there). None just skips
    that bonus, same convention as the other optional params above."""
    current = {s: chara_info.get(s, 0) for s in training_formula.STAT_NAMES}
    maxs = {s: chara_info.get("max_wiz" if s == "wiz" else f"max_{s}", 9999)
            for s in training_formula.STAT_NAMES}
    motivation = max(0, min(4, chara_info.get("motivation", 2)))
    scenario_id = chara_info.get("scenario_id", 1)
    pos_to_sid = {c["position"]: c["support_card_id"] for c in chara_info.get("support_card_array", [])}
    deck_card_ids = list(pos_to_sid.values())
    total_fan = chara_info.get("fans", 0) or 0
    current_vital = chara_info.get("vital", 0) or 0
    max_vital = chara_info.get("max_vital", 0) or 0
    deck_bond_sum = sum(_bond_of(chara_info, p) for p in pos_to_sid)
    # While a group card's Pure Passion is up it does friendship training in
    # whichever facility it is standing in -- the only way a friend/group card
    # ever reaches the friendship multiplier.
    pure_passion = _pure_passion_cards(chara_info)
    npc_placement = _place_unlocked_npcs(unlocked_npcs, turn)  # scenario NPCs by facility
    # Grand Live's recruited-but-uncarded supporters take whatever slots the
    # deck and the scenario NPCs left, up to five bodies per facility.
    occupied = {cmd: len(distribution.get(_COMMAND_TO_TYPE[cmd], []))
                     + len(npc_placement.get(cmd, []))
                for cmd in TRAINING_COMMAND_IDS}
    supporter_placement = (_npc_supporter_placements(full_state, chara_info, turn, occupied)
                           if full_state is not None else {})
    # The client needs a row per supporter before it can draw her portrait.
    _ensure_supporter_eval_rows(
        chara_info, [c for ids in supporter_placement.values() for c in ids])
    # Scenario TEAMMATES (Unity Cup) take what is left. Counted against the
    # same five slots, so they queue behind the deck, the NPCs and the
    # supporters rather than displacing them.
    for cmd, ids in supporter_placement.items():
        occupied[cmd] = occupied.get(cmd, 0) + len(ids)
    team_placement = (scenarios.for_chara(chara_info).partner_placements(
                          full_state, chara_info, turn, occupied)
                      if full_state is not None else {})

    training = []
    for cmd in TRAINING_COMMAND_IDS:              # base facility ids 101-106
        level = _CAMP_LEVEL if camp else (facility_levels or {}).get(str(cmd), 1)
        served_cmd = _CAMP_ID_BY_BASE[cmd] if camp else cmd   # 601-605 during camp
        partners = list(distribution.get(_COMMAND_TO_TYPE[cmd], []))
        in_facility = [(pos_to_sid.get(p), _bond_of(chara_info, p)) for p in partners]
        hinting = _hinting_partners(chara_info, turn, partners)
        partners += npc_placement.get(cmd, [])  # NPCs share the partner list
        # ... and so do the recruited supporters, who ride the same array under
        # their chara_id (see _npc_supporter_placements). They are appended
        # AFTER `in_facility`/`hinting` are taken so they can never be mistaken
        # for a deck position by anything that reads per-card state.
        supporters_here = supporter_placement.get(cmd, [])
        partners += supporters_here
        # Scenario teammates are BODIES ONLY here: their contribution is the
        # scenario's own bonus (team_data_set's params_inc_dec_info_array),
        # which the client adds on top, so they must not also feed
        # calculate_training_gain or the bonus would be counted twice.
        partners += team_placement.get(cmd, [])
        # Gains use the BASE command id (101-106) + the facility level multiplier;
        # the served command_id may be the camp id (601-605).
        gains = training_formula.calculate_training_gain(
            card_id=chara_info.get("card_id"), in_facility_cards=in_facility,
            command_id=cmd, scenario_id=scenario_id, facility_level=level,
            motivation=motivation, current_stats=current, max_stats=maxs,
            npc_supporter_count=len(supporters_here),
            extra_stat_bonus=training_bonus,
            scenario_friendship_bonus=friendship_bonus,
            support_card_levels=support_card_levels,
            deck_card_ids=deck_card_ids, total_fan=total_fan,
            current_vital=current_vital, max_vital=max_vital, deck_bond_sum=deck_bond_sum,
            friendship_stacks=friendship_stacks, pure_passion_cards=pure_passion)
        if not gains:
            continue
        # Slow Metabolism: 'cannot gain Speed from training' -- the formula
        # doesn't know about conditions, so zero the blocked stat here rather
        # than let the preview promise a gain _apply_preview_gains then
        # silently drops on commit (user-reported 2026-09-03: the button
        # still showed the raw speed number instead of the +0 the game
        # actually gives). Same "capped stat omitted, not sent as 0" handling
        # below then takes care of leaving it out of params entirely.
        for s in training_formula.STAT_NAMES:
            if gains.get(s) and conditions.blocks_stat(chara_info, s):
                gains[s] = 0
        forced = _forced_training_gain()
        if forced is None:
            # A capped stat (gains[s] == 0) is OMITTED, not sent as an
            # explicit 0 -- tried sending the explicit 0 (2026-08-16) as a
            # guess at what triggers the client's "X is in superb form"
            # result-log line when a stat is maxed; live-tested and it made
            # no difference (still shows nothing), so reverted rather than
            # carry unverified wire shape that doesn't even achieve its goal.
            # The real trigger is still unknown -- needs a capture of a
            # maxed-stat training result to solve properly.
            params = [{"target_type": _STAT_TO_TARGET_TYPE[s], "value": gains[s]}
                      for s in training_formula.STAT_NAMES if gains.get(s, 0)]
            if gains.get("skill_point"):
                params.append({"target_type": 30, "value": gains["skill_point"]})
        else:
            # TESTING KNOB: every stat AND skill points at a fixed value. All
            # five stats are listed, not just the ones this facility normally
            # trains, so the screen reads the same everywhere.
            params = [{"target_type": _STAT_TO_TARGET_TYPE[s], "value": forced}
                      for s in training_formula.STAT_NAMES]
            params.append({"target_type": 30, "value": forced})
        params.append({"target_type": 10, "value": gains.get("energy_cost", -20)})
        # THE SCENARIO'S LAST WORD on this facility's numbers. exec_command
        # applies exactly what the preview shows (_preview_gains_for), so a
        # scenario that scales the button here gets the award for free and the
        # two cannot disagree -- which is what lets both halves of a Trackblazer
        # ankle weight (+50% gain, +20% energy cost) be one edit.
        params = scenarios.for_chara(chara_info).scale_training_preview(
            full_state or {}, chara_info, cmd, params)
        fail = training_formula.failure_rate(
            chara_info.get("vital", 100), command_id=cmd, facility_level=level,
            conditions=chara_info.get("chara_effect_id_array"),
            deck=_career_deck(chara_info))
        # A scenario may override it outright -- Unity Cup's purple burst makes
        # its facility unfailable. Applied to the SERVED rate so the button the
        # player reads and the roll the server makes cannot disagree.
        override = scenarios.for_chara(chara_info).training_failure_override(
            full_state or {}, chara_info, cmd, partners)
        if override is not None:
            fail = override
        training.append({
            "command_type": 1, "command_id": served_cmd, "is_enable": 1,
            "training_partner_array": list(partners),
            "tips_event_partner_array": hinting, "params_inc_dec_info_array": params,
            "failure_rate": fail, "level": level,
        })
    # Non-training entries are built CANONICALLY per turn, not inherited from
    # the previous turn's array (the exact entry shape is capture-verified --
    # empty partner/params arrays, failure_rate 0, level 0). Inheriting was a
    # latent trap: filtering entries out during summer camp (which replaces
    # Rest 701 + outings 301/390 + infirmary 801 with the single merged
    # Rest & Recreation 304, per single_mode_turn rest_type=0/
    # health_room_type=0/outing_set_id=2) would then have permanently LOST
    # the filtered entries once camp ended, since the camp turn's array
    # becomes the next turn's template.
    def _non_training_entry(ctype, cid, enable=1):
        return {"command_type": ctype, "command_id": cid, "is_enable": enable,
                "training_partner_array": [], "tips_event_partner_array": [],
                "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0}

    if camp:
        non_training = [_non_training_entry(3, _CAMP_OUTING_GROUP),
                        _non_training_entry(4, _RACE_COMMAND_ID)]
    else:
        non_training = [_non_training_entry(3, 301), _non_training_entry(3, 390),
                        _non_training_entry(4, _RACE_COMMAND_ID),
                        _non_training_entry(7, 701),
                        _non_training_entry(8, 801, enable=0)]

    # Race command (401) + mandatory-race lockout. Verified from the real
    # capture (turn 12, the debut): on a turn with a real determined race the
    # client shows ONLY 401 enabled (everything else -- training, outing 301/390,
    # rest 701 -- disabled); every other turn from the FIRST such race onward, 401
    # stays enabled alongside everything else (this is what "unlocks racing" --
    # before that first race turn 401 is disabled, matching turn 1-11).
    #
    # is_enable alone is NOT what the client uses to decide "show the reduced
    # Race Day screen" vs "show the normal grid" -- verified by an exact,
    # byte-for-byte match of is_enable/race_entry_restriction/disable_command_id
    # against a real capture that still rendered wrong. Diffing a captured real
    # LOCKED response found the actual difference: training_partner_array is
    # EMPTY and failure_rate is 0 on every locked facility (no support
    # cards/NPCs standing in a facility nobody can train at, no failure % for a
    # roll that can't happen) -- ours kept the normal preview's partners/rate
    # alongside is_enable=0, which is what made the buttons look merely
    # "grayed" instead of genuinely empty/gone.
    forced_turns = _forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    unlock_turn = min(forced_turns) if forced_turns else None
    # A LOST DEBUT does NOT lock training (user-corrected): you keep training
    # normally, you are just restricted to MAIDEN races until you win one --
    # and failing to win one is how a run dies, because the later goals need
    # the higher classes. Only a genuine mandatory route race forces the turn.
    is_forced_turn = turn in forced_turns
    if is_forced_turn:
        for c in training:
            c["is_enable"] = 0
            c["training_partner_array"] = []
            c["tips_event_partner_array"] = []
            c["failure_rate"] = 0
    for c in non_training:
        cid = c.get("command_id")
        if cid == _RACE_COMMAND_ID:
            c["is_enable"] = 1 if (unlock_turn is not None and turn >= unlock_turn) else 0
        elif cid in _FORCEABLE_NON_TRAINING_IDS:
            c["is_enable"] = 0 if is_forced_turn else 1

    return training + non_training


def _refresh_failure_rates(chara_info: dict, home_info: dict,
                           full_state: dict = None,
                           facility_levels: dict = None) -> None:
    """Recompute just the per-facility failure_rate from the current vital, in
    place (no re-roll of the card distribution). Used after Rest applies its
    deferred energy at event-resolution time, so the training previews reflect the
    NEW energy -- otherwise the pre-rest failure rates stay on screen.

    Skips facilities the mandatory-race lock disabled (is_enable=0) -- those must
    stay at 0 (a locked facility has nothing to preview), which this function
    would otherwise blindly overwrite with a freshly-computed nonzero rate,
    undoing the "genuinely empty, not just grayed" clearing in
    _build_training_command_info."""
    conds = chara_info.get("chara_effect_id_array")
    vital = chara_info.get("vital", 100)
    scenario = scenarios.for_chara(chara_info)
    for c in home_info.get("command_info_array") or []:
        if c.get("command_type") == 1 and c.get("command_id") is not None and c.get("is_enable"):
            cmd = c["command_id"]
            base = _CAMP_BASE_BY_ID.get(cmd, cmd)
            # THE LEVEL THE FACILITY ACTUALLY IS. Recomputing at a flat level 1
            # quietly undid every level the career had earned, on the response
            # after a Rest -- the entry keeps its own `level`, so use that.
            level = int(c.get("level") or (facility_levels or {}).get(str(base), 1) or 1)
            rate = training_formula.failure_rate(
                vital, command_id=cmd, facility_level=level, conditions=conds,
                deck=_career_deck(chara_info))
            # ...AND THE SCENARIO STILL GETS ITS SAY. _build_training_command_info
            # asks training_failure_override and this did not, so a Unity Cup
            # purple burst's guaranteed 0 was overwritten with the ordinary rate
            # on the first response after a Rest: the button read a real failure
            # chance for a training that cannot fail (user-reported 2026-09-06).
            override = scenario.training_failure_override(
                full_state or {}, chara_info, base,
                c.get("training_partner_array") or [])
            c["failure_rate"] = rate if override is None else override


def _refresh_command_info(chara_info: dict, home_info: dict, turn: int, unlocked_npcs=(),
                          facility_levels: dict = None, race_history=(),
                          training_bonus: dict = None,
                          friendship_bonus: int = 0,
                          specialty_bonus: int = 0,
                          support_card_levels: dict = None,
                          friendship_stacks: dict = None,
                          full_state: dict = None) -> list:
    """Roll this turn's distribution and rebuild home_info.command_info_array
    from it (in place). unlocked_npcs (Director/Happy Meek target_ids) are placed
    into facilities so they appear on the training screen. facility_levels feeds
    per-facility level scaling; summer-camp turns serve the level-5 camp facilities
    (601-605). race_history feeds the debut-retry lock (see _debut_retry_active).
    support_card_levels/friendship_stacks/full_state: see _build_training_command_info.
    Returns the new array."""
    distribution = _roll_distribution(
        chara_info, turn, specialty_bonus,
        # Trackblazer's Reset Whistle re-rolls the placement; every other
        # scenario returns 0 and the roll is unchanged.
        salt=scenarios.for_chara(chara_info).placement_salt(full_state or {}))
    home_info["command_info_array"] = _build_training_command_info(
        chara_info, home_info.get("command_info_array"), distribution,
        unlocked_npcs=unlocked_npcs, turn=turn,
        facility_levels=facility_levels, camp=_is_camp_turn(turn),
        race_history=race_history, training_bonus=training_bonus,
        friendship_bonus=friendship_bonus, support_card_levels=support_card_levels,
        friendship_stacks=friendship_stacks, full_state=full_state)
    # race_entry_restriction (a home_info SIBLING of command_info_array, not a
    # per-command flag) was found stuck at its captured template's frozen value
    # (1, "restricted") forever, never following the same lock as the race
    # command -- every real capture checked (multiple turns before/after/during
    # a real race) shows it flip to 0 once racing unlocks and stay 0. Tied to the
    # exact same route data so it can never disagree with command_info_array.
    forced_turns = _forced_race_turns(tuple(chara_info.get("route_race_id_array") or ()))
    unlock_turn = min(forced_turns) if forced_turns else None
    restricted = (unlock_turn is None or turn is None or turn < unlock_turn)
    # ... and a story-imposed lockout ("Cannot race for N turns") outranks the
    # normal unlock: see restrict_racing.
    until = chara_info.get(RACE_RESTRICT_UNTIL_KEY) or 0
    if turn is not None and until and turn < until:
        restricted = True
    home_info["race_entry_restriction"] = 1 if restricted else 0

    _sync_infirmary_lock(chara_info, home_info, turn)
    return home_info["command_info_array"]


def _sync_infirmary_lock(chara_info: dict, home_info: dict, turn: int | None = None) -> None:
    """Keep the infirmary's is_enable in sync with whether a negative condition
    is currently active. The entry is ALWAYS in the list (outside camp) --
    what changes is is_enable, which is 1 exactly while a negative condition
    is active (capture-confirmed shape, sitting alongside Rest 701).

    Deliberately NOT part of a full command_info_array rebuild -- that rerolls
    this turn's training distribution (see _refresh_command_info), so it can
    only safely run once per turn. Conditions can be granted or cured mid-turn
    by an event's own effects (check_event resolving a story choice, the
    infirmary visit itself, ...), and that response must show the infirmary's
    lock state correctly WITHOUT re-rolling anything else (user-reported
    2026-09-03: the infirmary stayed locked in the very response that granted
    the negative condition, only catching up once the next real command_info
    refresh happened to run)."""
    cmds = home_info.get("command_info_array")
    if cmds is None:
        return
    treatable = conditions.any_negative(chara_info)
    found = next((c for c in cmds if c.get("command_id") == _INFIRMARY_COMMAND_ID), None)
    if found is not None:
        found["is_enable"] = 1 if treatable else 0
    elif turn is not None and not _is_camp_turn(turn):
        cmds.append({"command_type": _INFIRMARY_COMMAND_TYPE,
                     "command_id": _INFIRMARY_COMMAND_ID,
                     "is_enable": 1 if treatable else 0,
                     "training_partner_array": [], "tips_event_partner_array": [],
                     "params_inc_dec_info_array": [], "failure_rate": 0, "level": 0})


def _partners_in_command(career_state: dict, command_id) -> list:
    """The support-card positions currently placed in this facility (what the
    client is showing) -- the cards that gain bond when it's trained."""
    hi = career_state.get("data", {}).get("home_info", {})
    for c in hi.get("command_info_array", []) or []:
        if c.get("command_id") == command_id:
            return c.get("training_partner_array", []) or []
    return []


# command_id -> the stat that facility trains (the one a failure docks).
_COMMAND_TRAINED_STAT = {101: "speed", 105: "stamina", 102: "power", 103: "guts", 106: "wiz",
                         601: "speed", 602: "stamina", 603: "power", 604: "guts", 605: "wiz"}
_ALL_STATS = ["speed", "stamina", "power", "guts", "wiz"]

# PER-CAREER TRAINING TALLY, on the career under "training_tally":
# {"speed": n, ..., "failures": n}. Keyed by the stat the facility trains, so a
# camp id counts toward the same facility its base id does. Five epithets read
# it back (epithets.py's training_count_* / training_count_total /
# training_failures): 14 "Perform Stamina Training 35 times", 19 "Win a Make
# Debut without training a single time", 20/175 "never failing a Training", and
# 261's successful-train-at-30%-failure clause. None of them were earnable
# before, because nothing on the server had ever counted a training.
TRAINING_TALLY_KEY = "training_tally"


def _record_training_tally(career_data: dict, command_id, failed: bool) -> None:
    """Count one training command. FAILED trainings still count as performed --
    the epithet that cares about failures counts those separately, and "perform
    Stamina Training 35 times" is about taking the facility, not succeeding at
    it."""
    stat = _COMMAND_TRAINED_STAT.get(command_id)
    if stat is None:
        return                      # rest / outing / infirmary / race: not a training
    tally = career_data.setdefault(TRAINING_TALLY_KEY, {})
    tally[stat] = int(tally.get(stat) or 0) + 1
    if failed:
        tally["failures"] = int(tally.get("failures") or 0) + 1

_REST_COMMANDS = {701}                # ãŠä¼‘ã¿ Rest (recover energy)
REST_CTX_KEY = "active_rest"          # on full_state: the rolled-but-unapplied outcome
_REST_EVENT_IDS = frozenset({7009, 7010, 7011})
_REST_STORY_SUFFIX = {7011: 701, 7009: 700, 7010: 718}


@career_events.resolver("rest")
def _resolve_rest(full_state, chara_info, event, choice_number, career=None, **kw):
    """The rest command deferred its energy to here so the client animates
    the meter as the event outcome -- moved verbatim from the old REST EVENT
    RESOLUTION branch."""
    rest_ctx = full_state.get(REST_CTX_KEY)
    if not rest_ctx:
        return {}
    was_capped = (chara_info.get("vital", 0) or 0) >= chara_info.get("max_vital", 100)
    _apply_rest_outcome(chara_info, rest_ctx)
    career_data = (career or {}).get("data") if isinstance(career, dict) else None
    career_home = career_data.get("home_info") if isinstance(career_data, dict) else None
    if isinstance(career_home, dict):
        # The energy just changed -- recompute the training failure rates so
        # the previews aren't stuck on the pre-rest energy's rates.
        _refresh_failure_rates(chara_info, career_home, full_state,
                               _facility_levels(career_data))
    full_state.pop(REST_CTX_KEY, None)
    return {"not_up": [6]} if was_capped else {}
# The rest flavor event MUST match the energy recovered (the event describes how
# well you rested): Well-Rested! = +70, All Refreshed = +50, Sleep Deprived = +30.
# Firing the event independently of the energy is what made Sleep Deprived show
# +70, etc. The energy is applied when the event resolves (animated at the end),
# not at the Rest command.
_REST_EVENT_BY_ENERGY = {70: 7011, 50: 7010, 30: 7009}

# INFIRMARY -- capture-confirmed (UmaDumpy 20260727_190428, the user's own two
# visits): exec_command {command_type: 8, command_id: 801} responds with
# command_result {801, sub_id 1, result_state 2} plus the trainee's event 7016
# / story 50<chara>717 (timing 6, single acknowledge). chara_info is UNCHANGED
# in that response -- the effects land when 7016 RESOLVES:
#   visit 1: vital 5 -> 25, condition 6 still present  (+20 energy, no cure)
#   visit 2: vital 25 -> 45, conditions []             (+20 energy, CURED)
# The command sits in command_info_array with is_enable 1 while a negative
# condition is present.
_INFIRMARY_COMMAND_ID = 801
_INFIRMARY_COMMAND_TYPE = 8
_INFIRMARY_EVENT_ID = 7016
_INFIRMARY_STORY_SUFFIX = 717
_INFIRMARY_ENERGY = 20
# USER-SUPPLIED (2026-07-28): a 60% roll PER negative condition, not a single
# cure-one. Consistent with the capture above (one visit cured nothing, the next
# cured the one debuff present), which on its own could not distinguish the two
# models. The 60% itself is the user's estimate, flagged as approximate.
_INFIRMARY_CURE_CHANCE = 0.60
INFIRMARY_CTX_KEY = "infirmary_ctx"


@career_events.resolver("infirmary")
def _resolve_infirmary(full_state, chara_info, event, choice_number, career=None, **kw):
    """+20 energy always, and a chance to cure ONE negative condition per
    (capture: visit 1 energy-only, visit 2 cured) -- moved verbatim from the
    old INFIRMARY RESOLUTION branch in handle_ura_check_event."""
    was_capped = (chara_info.get("vital", 0) or 0) >= chara_info.get("max_vital", 100)
    chara_info["vital"] = min(chara_info.get("max_vital", 100),
                              (chara_info.get("vital", 0) or 0) + _INFIRMARY_ENERGY)
    cured = conditions.cure_negatives_by_chance(chara_info, _INFIRMARY_CURE_CHANCE)
    log.info("infirmary: +%s energy, cured=%s", _INFIRMARY_ENERGY, cured)
    career_data = (career or {}).get("data") if isinstance(career, dict) else None
    career_home = career_data.get("home_info") if isinstance(career_data, dict) else None
    if isinstance(career_home, dict) and isinstance(full_state, dict):
        _refresh_command_info(
            chara_info, career_home, turn=chara_info.get("turn"),
            unlocked_npcs=[n[0] for n in
                           full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])],
            facility_levels=_facility_levels(career_data),
            race_history=career_data.get("race_history", []),
            training_bonus=_training_bonus(full_state, chara_info),
            friendship_bonus=_friendship_bonus(full_state, chara_info),
            specialty_bonus=_specialty_bonus(full_state, chara_info),
            support_card_levels=_support_card_levels(full_state),
            friendship_stacks=_friendship_stacks(full_state),
            full_state=full_state)
    return {"not_up": [6]} if was_capped else {}


def _roll_rest_outcome() -> dict:
    """Roll a rest's outcome WITHOUT applying it. Energy: +70 (24.7%) / +50
    (60.5%) / +30 (12.3%) / +30 & Night Owl (2.5%); the +30 outcome carries a 10%
    Slow Metabolism. The flavor event is chosen to MATCH the energy. Applied later
    by _apply_rest_outcome when the event resolves.

    The energy tiers are MEASURED: 17,005 real Rest commands across all 1,974
    captures/bot_logs careers, tallied by flavor event (7011/7010/7009) ->
    +70 24.66% [24.02,25.31] / +50 60.55% [59.81,61.28] / +30 14.80%
    [14.27,15.34], flat across all four scenarios, turn phase and pre-rest
    energy. The old community split (25/62.5/12.5) is outside those intervals.
    The Night Owl (2.5%) and Slow Metabolism (10%) shares inside the +30 tier
    are NOT measured -- the flavor event doesn't carry the condition, and
    condition snapshots are too sparse -- so they keep the old values."""
    r = random.random()
    night_owl = False
    if r < 0.247:
        energy = 70
    elif r < 0.852:
        energy = 50
    elif r < 0.975:
        energy = 30
    else:
        energy = 30
        night_owl = True
    slow_metab = (energy == 30 and random.random() < 0.10)
    return {"event_id": _REST_EVENT_BY_ENERGY[energy], "energy": energy,
            "night_owl": night_owl, "slow_metab": slow_metab}


def _apply_rest_outcome(chara_info: dict, outcome: dict) -> None:
    """Apply a rolled rest outcome (energy + any conditions) to chara_info."""
    mv = chara_info.get("max_vital", 100)
    chara_info["vital"] = max(0, min(mv, chara_info.get("vital", 0) + outcome.get("energy", 0)))
    if outcome.get("night_owl"):
        single_mode_events.add_condition(chara_info, _COND_NIGHT_OWL)
    if outcome.get("slow_metab"):
        single_mode_events.add_condition(chara_info, _COND_SLOW_METABOLISM)


def _rest_event_entry(event_id: int, chara_id: int) -> dict:
    """A no-choice rest flavor event (Well-Rested! / Sleep Deprived / All
    Refreshed). Same envelope as the real capture."""
    return {
        "event_id": event_id, "chara_id": chara_id,
        "story_id": int(f"50{chara_id}{_REST_STORY_SUFFIX[event_id]:03d}"),
        "play_timing": 6,
        "event_contents_info": {
            "support_card_id": 0, "show_clear": 0, "show_clear_sort_id": 0,
            "choice_array": [{"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                              "gain_select_id_index": 1, "select_icon": 0}],
            "tips_training_partner_id": None,
        },
        "succession_event_info": None, "minigame_result": None,
    }
_RECREATION_GROUPS = {301, 390, 304}  # ãŠã§ã‹ã‘ Recreation command_group_id (mood + energy)
_MOTIVATION_MAX = 5
_COND_NIGHT_OWL = 1
_COND_SLOW_METABOLISM = 4


# Recreation outing types (command_group_id, cat 55): the server picks one and
# reports it back as command_result.command_id, which is what makes the client
# play that outing's animation. Confirmed from the capture (a cg=301 recreation
# request came back with command_result.command_id=302 = Karaoke).
_OUTING_RIVERBANK = 301
_OUTING_KARAOKE = 302
_OUTING_SHRINE = 303


# --- Pal / Group support-card outings -----------------------------------------
# See pal_cards.py for the decoded master data + story layout. State lives on
# full_state:
#   PAL_STATE_KEY = {str(card_id): {"met": 1, "unlocked": 1, "step": n,
#                                   "outed": [chara_id, ...], "finale": 1}}
# and mirrors onto chara_info.evaluation_info_array[pos] as is_outing/story_step
# (what the CLIENT reads to offer the outing).
PAL_STATE_KEY = "pal_card_state"
PAL_EVENT_CTX_KEY = "pal_event_ctx"
# Outing-unlock roll: EVERY TURN once the pal/group card has been met, gated
# on bond -- the reference sim (Game.cpp) rolls
# FriendUnlockOutgoingProbEveryTurn{Low,High}Friendship each turn with the
# threshold at friendship >= 60. The constants live in a header we don't have;
# these are the user's 25% (high) with a lower cold-start rate.
_PAL_UNLOCK_CHANCE_LOW = 0.10  # bond < 60
_PAL_UNLOCK_CHANCE_HIGH = 0.25  # bond >= 60
_PAL_RANDOM_CHANCE = 0.10      # its repeatable training event
_GROUP_BUFF_TRAIN_CHANCE = 0.07  # group card randomly grants Pure Passion
_OUTING_COMMAND_GROUP = 390    # single_mode_outing id 3 (condition 1)
_PAL_FIRST_EVENT_ID = 10002    # capture: pal/group card events use the support ids
_PAL_OUTING_EVENT_ID = 10002
# Every pal/group card's special New Year date (pal_cards.new_year_story)
# becomes available once the turn-25 New Year beat has passed (see
# event_engine.seasonal_event's 'new_year', queued exactly at turn 25) --
# live-reported 2026-09-03: recreation with an unlocked pal/group card right
# after that beat should offer THIS, ahead of the ordinary chain/finale.
_PAL_NEW_YEAR_TURN = 25
PURE_PASSION_KEY = "pure_passion_until"   # {str(condition_id): expires_after_turn}


def _pal_state(full_state: dict) -> dict:
    return full_state.setdefault(PAL_STATE_KEY, {})


def _pal_card_entry(full_state: dict, card_id: int) -> dict:
    return _pal_state(full_state).setdefault(str(card_id), {})


def _deck_pal_cards(chara_info: dict) -> list:
    """[(position, support_card_id)] of the pal/group cards in the deck."""
    return [(c.get("position"), c.get("support_card_id"))
            for c in (chara_info.get("support_card_array") or [])
            if pal_cards.is_pal_or_group(c.get("support_card_id"))]


def _sync_pal_evaluation(full_state: dict, chara_info: dict) -> None:
    """Mirror the pal/group unlock state onto evaluation_info_array -- is_outing
    is what makes the client offer the outing at all, story_step is how many
    outings it thinks you've had."""
    state = _pal_state(full_state)
    for pos, card_id in _deck_pal_cards(chara_info):
        entry = state.get(str(card_id)) or {}
        for e in chara_info.get("evaluation_info_array") or []:
            if e.get("target_id") == pos:
                e["is_outing"] = 1 if entry.get("unlocked") else 0
                e["story_step"] = entry.get("step", 0)
                break


def _pure_passion_effects(full_state: dict, chara_info: dict, turn) -> list:
    """Apply/expire the group cards' Pure Passion condition (friendship training
    with the group in ANY facility + Night Owl/Slacker immunity). Stored with an
    expiry turn; chara_effect_id_array is the wire field the client reads.

    Returns the condition ids that expired ON THIS CALL -- the caller owes each
    one its end-of-buff story (see the upkeep in handle_exec_command). Returning
    them rather than serving here keeps this callable from the response paths
    that have no event queue to put a story in."""
    active = full_state.get(PURE_PASSION_KEY) or {}
    if turn is None:
        return []
    live = {cid: until for cid, until in active.items() if int(until) >= int(turn)}
    expired = []
    if live != active:
        expired = [int(cid) for cid in active if cid not in live]
        full_state[PURE_PASSION_KEY] = live
    known = pal_cards.pure_passion_conditions()
    effects = [e for e in (chara_info.get("chara_effect_id_array") or [])
               if e not in known]
    effects += [int(cid) for cid in live]
    chara_info["chara_effect_id_array"] = effects
    return expired


def _grant_pure_passion(full_state: dict, chara_info: dict, card_id: int, turn) -> bool:
    """Hand this group's Pure Passion to the trainee for 3-5 turns.

    The length is rolled per grant, so the buff's own end date is decided the
    moment it lands rather than being a constant the player could count on."""
    cond = pal_cards.pure_passion_condition(card_id)
    if not cond or turn is None:
        return False
    turns = random.randint(pal_cards.PURE_PASSION_TURNS_MIN,
                           pal_cards.PURE_PASSION_TURNS_MAX)
    active = full_state.setdefault(PURE_PASSION_KEY, {})
    # LATER GRANT WINS, never shortens: re-granting mid-buff (the 7% training
    # roll can land while it is already up) extends to whichever end date is
    # further out.
    until = int(turn) + turns - 1
    active[str(cond)] = max(int(active.get(str(cond)) or 0), until)
    log.info("pure passion: card %s condition %s for %s turns (until turn %s)",
             card_id, cond, turns, active[str(cond)])
    _pure_passion_effects(full_state, chara_info, turn)
    return True


def _pure_passion_cards(chara_info: dict) -> frozenset:
    """The deck's group cards whose Pure Passion is LIVE right now, read off the
    served chara_effect_id_array so the training screen and the award can never
    disagree about whether the buff is up.

    These are the cards that do friendship training in ANY facility while the
    condition holds -- training_formula.calculate_training_gain skips every
    friend/group card otherwise."""
    live = set(chara_info.get("chara_effect_id_array") or ())
    if not live:
        return frozenset()
    return frozenset(
        card_id for _pos, card_id in _deck_pal_cards(chara_info)
        if pal_cards.is_group(card_id)
        and pal_cards.pure_passion_condition(card_id) in live)


def _pal_event_entry(card_id: int, sid: int, chara_info: dict, event_id: int):
    """Build the client entry for one of a pal/group card's events, using the
    engine's real GameTora choices/effects when we have them."""
    title, ev = event_engine.event_by_story_id(card_id, sid)
    if ev is None:
        ev = {"choices": [{"name": "Continue", "effects": []}]}
        title = master_data.query_one(
            "SELECT text FROM text_data WHERE category=181 AND [index]=?", (sid,))
        title = title["text"] if title else ""
    return title, ev, event_engine.career_event_entry(
        ev, event_id, sid, chara_id=0, support_card_id=card_id, play_timing=1)


def _queue_pal_event(response, full_state, career_state, card_id, sid, kind,
                     partner=None, front: bool = False) -> bool:
    """Queue a pal/group card event and stash the context so its commit
    (check_event / get_choice_reward) applies the right effects AND the right
    outing bookkeeping.

    `front` is for the ONE case that is a deliberate player choice rather than
    a passive coincidence roll -- see _queue_outing_event's call, the only one
    that passes True."""
    chara_info = career_state["data"]["chara_info"]
    title, ev, entry = _pal_event_entry(card_id, sid, chara_info, _PAL_FIRST_EVENT_ID)
    _queue_turn_event(response, full_state, entry, front=front)
    _push_career_ctx(full_state, {
        "event_id": _PAL_FIRST_EVENT_ID, "story_id": sid, "source": "support",
        "source_id": card_id, "title": title,
        "trainee_card_id": chara_info.get("card_id")})
    full_state[PAL_EVENT_CTX_KEY] = {"card_id": card_id, "story_id": sid,
                                     "kind": kind, "partner": partner}
    full_state[event_engine.FIRED_EVENTS_KEY] = list(
        full_state.get(event_engine.FIRED_EVENTS_KEY, [])) + [sid]
    return True


def _maybe_fire_pal_training_event(full_state, career_state, response, pal_partners) -> bool:
    """A pal/group card in the trained facility fires, in priority order: its
    first-meeting event (once), then -- once met -- a roll for the outing
    UNLOCK event, then a roll for its repeatable training event. Group cards
    also randomly re-grant Pure Passion while training with them.

    `pal_partners` is [(position, card_id)] captured at training time (see
    _simulate_exec_command) -- the placement the player actually trained."""
    chara_info = career_state["data"]["chara_info"]
    for pos, card_id in pal_partners or ():
        entry = _pal_card_entry(full_state, card_id)
        if not entry.get("met"):
            sid = pal_cards.first_meeting_story(card_id)
            entry["met"] = 1
            if sid:
                return _queue_pal_event(response, full_state, career_state, card_id,
                                        sid, "first")
            continue
        # (Unlock is NOT rolled here: per the reference sim (Game.cpp,
        # handleFriendUnlock caller), once met it rolls EVERY TURN, gated on
        # bond -- see the per-turn upkeep in handle_exec_command.)
        if (pal_cards.is_group(card_id)
                and random.random() < _GROUP_BUFF_TRAIN_CHANCE):
            _grant_pure_passion(full_state, chara_info, card_id, chara_info.get("turn"))
        if random.random() < _PAL_RANDOM_CHANCE:
            sid = pal_cards.random_story(card_id)
            if sid:
                return _queue_pal_event(response, full_state, career_state, card_id,
                                        sid, "random")
    return False


def _member_step(entry: dict, partner) -> int:
    return int((entry.get("steps") or {}).get(str(partner), 0))


def _new_year_outing_ready(full_state: dict, chara_info: dict, card_id: int) -> bool:
    """Whether this card's special New Year date (pal_cards.new_year_story) is
    available to offer: unlocked, past turn 25, and not already delivered."""
    entry = _pal_card_entry(full_state, card_id)
    if not entry.get("unlocked") or entry.get("new_year_fired"):
        return False
    if (chara_info.get("turn") or 0) < _PAL_NEW_YEAR_TURN:
        return False
    return pal_cards.new_year_story(card_id) is not None


def _queue_pal_new_year(response, full_state, career_state) -> bool:
    """Chain the first ready pal/group New Year date behind the trainee's own
    New Year beat. Returns whether one was queued.

    ONE per turn on purpose: _queue_pal_event parks its bookkeeping in the
    single PAL_EVENT_CTX_KEY slot, so a second card queued the same turn would
    overwrite the first one's context and resolve against the wrong card. A
    second unlocked card keeps its beat and is offered on an outing instead
    (_queue_outing_event), which is where it lived before this."""
    chara_info = career_state["data"]["chara_info"]
    cards = {c for _p, c in _deck_pal_cards(chara_info)}
    cards |= {int(cid) for cid, e in _pal_state(full_state).items()
              if e.get("unlocked") and pal_cards.is_pal_or_group(int(cid))}
    for card_id in sorted(cards):
        if not _new_year_outing_ready(full_state, chara_info, card_id):
            continue
        sid = pal_cards.new_year_story(card_id)
        if not sid:
            continue
        partner = next(iter(pal_cards.partners(card_id) or ()), None)
        if _queue_pal_event(response, full_state, career_state, card_id, sid,
                            "new_year", partner=partner):
            log.info("new year: pal/group card %s date %s queued behind the "
                     "trainee's own beat", card_id, sid)
            return True
    return False


def _outing_partners(full_state: dict, chara_info: dict) -> list:
    """[(chara_id, card_id)] the player may currently go out with: every
    unlocked pal/group card's partners who still have chain left. Each GROUP
    MEMBER walks their own chain (their card-band outing story then their
    personal sub-chain), so a member with a chain can be gone out with more
    than once while one without drops off after their single story.

    A partner whose chain IS exhausted stays selectable a while longer when
    their card's New Year date (see _new_year_outing_ready) hasn't fired yet
    -- otherwise a card finished before turn 25 could never reach it."""
    out = []
    cards = {c for _p, c in _deck_pal_cards(chara_info)}
    # A pal whose unlock was drawn from the general pull counts even when their
    # card isn't in the deck (that's the point of leaving it in the pull).
    cards |= {int(cid) for cid, e in _pal_state(full_state).items()
              if e.get("unlocked") and pal_cards.is_pal(int(cid))}
    for card_id in sorted(cards):
        entry = _pal_card_entry(full_state, card_id)
        if not entry.get("unlocked"):
            continue
        for chara in pal_cards.partners(card_id):
            step = (entry.get("step", 0) if pal_cards.is_pal(card_id)
                    else _member_step(entry, chara))
            exhausted = step >= pal_cards.outing_chain_len(
                card_id, None if pal_cards.is_pal(card_id) else chara)
            if exhausted and not _new_year_outing_ready(full_state, chara_info, card_id):
                continue
            out.append((chara, card_id))
    return out


def _card_for_partner(full_state: dict, chara_info: dict, select_id) -> int | None:
    for chara, card_id in _outing_partners(full_state, chara_info):
        if chara == select_id:
            return card_id
    return None


def _queue_outing_event(response, full_state, career_state, card_id, partner) -> bool:
    """The chosen partner's outing story (capture: exec_command 390/select_id ->
    that partner's 8xxxxx event).

    front=True (unlike every other pal-event call site): this is the ONE case
    where the player deliberately picked "recreation WITH <this partner>",
    not a passive coincidence roll -- user-reported 2026-08-24 that her
    outing event ("recreation with Light Hello") must play FIRST, immediately
    after being triggered, ahead of anything else queued the same turn (e.g.
    a scenario cutscene), where the general "outing events chain behind
    whatever else queued" rule (see the DYNAMIC SUPPORT-CARD/OUTING EVENT
    comment above _queue_outing_event's call site) is right for passive/
    random coincidences but wrong for a choice the player just explicitly
    made.

    New Year takes priority over the ordinary chain step whenever it's ready
    (see _new_year_outing_ready) -- it's a one-time bonus, not part of the
    numbered chain, so it must be offered as soon as it can be rather than
    waiting for the chain to run dry."""
    chara_info = career_state["data"]["chara_info"]
    if _new_year_outing_ready(full_state, chara_info, card_id):
        sid = pal_cards.new_year_story(card_id)
        if sid:
            return _queue_pal_event(response, full_state, career_state, card_id, sid,
                                    "new_year", partner=partner, front=True)
    entry = _pal_card_entry(full_state, card_id)
    step = (entry.get("step", 0) if pal_cards.is_pal(card_id)
            else _member_step(entry, partner))
    sid = pal_cards.outing_story(card_id, partner, step)
    if not sid:
        return False
    return _queue_pal_event(response, full_state, career_state, card_id, sid,
                            "outing", partner=partner, front=True)


def _pal_unlock_hit(card_id, choice_number, event, chara_info, branch) -> bool:
    """Did this choice unlock the card's outings?

    For a GAMBLE choice the answer is already decided: it is the story branch
    the event was served with, which is the arm whose text the client is
    playing right now (see event_engine._choice_branch_index). Rolling
    unlock_chance again here is a SECOND, independent coin -- the player could
    watch the outing open on screen and not get it, or the reverse. The branch
    carrying the richer gains is the unlocking one, exactly as the reward
    preview marks it (choice_reward_array / UNLOCK_OUTING_DISPLAY_ID).

    Falls back to the probability roll when there is no gamble to read: a
    single-outcome choice (where unlock_chance is a flat 1.0 or 0.0 anyway),
    an event we could not resolve, or a branch that doesn't fit the choice.
    """
    if event is not None:
        outcome, count = event_engine.served_outcome(
            event, choice_number, chara_info, branch)
        if count > 1 and outcome is not None:
            return outcome == event_engine.richest_outcome(
                event, choice_number, chara_info)
    return random.random() < pal_cards.unlock_chance(card_id, choice_number or 1)


def _commit_pal_event(full_state: dict, chara_info: dict, choice_number: int,
                      event: dict | None = None, branch=None) -> None:
    """Apply the outing bookkeeping for a pal/group event the player just
    resolved: unlock rolls (per-card, per-choice -- some are a gamble), outing
    step/member tracking, the group's Pure Passion grant, and the finale event
    once the whole chain is done. The event's stat/bond effects are applied by
    the normal career-event path; this is only the outing state."""
    ctx = full_state.get(PAL_EVENT_CTX_KEY)
    if not ctx:
        return
    card_id, kind = ctx.get("card_id"), ctx.get("kind")
    entry = _pal_card_entry(full_state, card_id)
    turn = chara_info.get("turn")
    if kind == "unlock":
        if _pal_unlock_hit(card_id, choice_number, event, chara_info, branch):
            entry["unlocked"] = 1
            # A group card's unlock is one of the three moments that grant
            # Pure Passion (unlock / all outings done / random while training).
            _grant_pure_passion(full_state, chara_info, card_id, turn)
    elif kind == "outing":
        entry["step"] = (entry.get("step") or 0) + 1
        partner = ctx.get("partner")
        if partner is not None:
            entry.setdefault("outed", [])
            if partner not in entry["outed"]:
                entry["outed"].append(partner)
            steps = entry.setdefault("steps", {})
            steps[str(partner)] = steps.get(str(partner), 0) + 1
        # The finale unlocks once you've been out with EVERYONE (group) / the
        # whole chain is walked (pal) -- not merely once some chain ran dry.
        if pal_cards.is_group(card_id):
            done = set(entry.get("outed") or [])
            complete = all(m in done for m in pal_cards.group_members(card_id))
        else:
            complete = entry["step"] >= pal_cards.outing_chain_len(card_id)
        if complete and not entry.get("finale"):
            entry["finale"] = 1
            entry["finale_pending"] = pal_cards.finale_story(card_id) or 0
            _grant_pure_passion(full_state, chara_info, card_id, turn)
    elif kind == "new_year":
        # A one-time bonus, not a chain step: no step/outed bookkeeping. Marked
        # fired so it's never re-offered (_new_year_outing_ready) or, for a PAL
        # card, re-served later as the chain-complete finale -- pal_cards.
        # new_year_story documents why that's the SAME story for pal cards
        # (suffix 11) but a genuinely different one for group cards.
        entry["new_year_fired"] = 1
        if pal_cards.is_pal(card_id):
            entry["finale"] = 1
    full_state.pop(PAL_EVENT_CTX_KEY, None)
    _sync_pal_evaluation(full_state, chara_info)


def _pal_unlock_preview_args(full_state: dict) -> tuple:
    """(unlock_partner, {select_index: chance}) when the event currently being
    previewed is a pal/group outing-UNLOCK, else (None, None). Feeds the
    display_id 11 marker so the player can SEE which choice unlocks recreation
    (and that a gamble choice only might)."""
    ctx = full_state.get(PAL_EVENT_CTX_KEY) or {}
    if ctx.get("kind") != "unlock":
        return None, None
    card_id = ctx.get("card_id")
    partners = pal_cards.partners(card_id)
    if not partners:
        return None, None
    return partners[0], pal_cards.unlock_chances(card_id)


def _maybe_queue_pal_finale(response, full_state, career_state) -> bool:
    """The "you've been out with everyone" event, queued the turn after the last
    outing resolves."""
    for card_id, entry in list(_pal_state(full_state).items()):
        sid = entry.get("finale_pending")
        if sid:
            entry["finale_pending"] = 0
            return _queue_pal_event(response, full_state, career_state, int(card_id),
                                    sid, "finale")
    return False


# --- Crane game (claw machine minigame after Recreation) ----------------------
# Full wire flow from the one real capture (UmaDumpy 20260721_143256 t32):
# recreation -> event 6002 (story 50<chara>726, timing 6) -> commit flips
# playing_state to 6 (client launches its native minigame) -> client POSTs
# single_mode/minigame_end {result:{result_state, result_value,
# result_detail_array:[{get_id, chara_id, dress_id, motion, face}]}} ->
# response serves the OUTCOME event (story 727-731 whose
# single_mode_story_data.mini_game_result == result_value; capture: value 2
# -> 731/event 6007) with minigame_result = the submitted result echoed back
# enriched with viewer/chara/turn/minigame_id -> normal commit resumes play.
# REWARDS apply at the ACK of the outcome event (the capture masked them --
# vital 100/100 and mood 5/5, hence not_up_parameter_info [6, 20] -- but the
# skill hint DID land there: skill_tips_array gained Straightaway Recovery
# group 20038 Lv1). Per-tier (JP wikis unanimous, game8 x2 for the numbers):
#   1 FAILURE:        vital  +5, guts +3
#   2 SUCCESS:        vital +10, mood +1, Straightaway Recovery hint +1
#   3 GREAT SUCCESS:  vital +15, mood +2, Straightaway Recovery hint +2
_CRANE_REWARDS = {1: (5, 0, 0), 2: (10, 1, 1), 3: (15, 2, 2)}  # (vital, mood, hint)
_CRANE_HINT_GROUP = 20038   # Straightaway Recovery (ç›´ç·šå›žå¾©)
# MEASURED over all 1,974 captures/bot_logs careers, plain recreation only
# (group 301; pal outings 390 and camp 304 never roll it):
#   - never before turn 25: 0 cranes in 1,836 year-one outings;
#   - at most once per career: 216 careers with one, zero with two;
#   - from turn 25 on (non-camp, not yet played): 216/920 = 23.48%
#     [20.85,26.32], and 24.82% on a career's FIRST eligible outing, so the
#     chance per outing is flat rather than rising. 0.15 (the old user-set
#     value) is outside that interval.
_CRANE_CHANCE = 0.235
_CRANE_FIRST_TURN = 25
CRANE_CTX_KEY = "active_crane_game"
CRANE_PLAYED_KEY = "crane_game_played"   # ONCE per career (user rule)
CRANE_LIFETIME_KEY = "crane_game_lifetime"  # {"play_count", "plushie_count"} --
                                            # NOT in _CAREER_STATE_KEYS, survives
                                            # career resets. missions.py's
                                            # ClawMachinePlayCount (100022) /
                                            # ClawMachinePlushieCount (100051).
CRANE_SESSION_KEY = "crane_session_plushies"  # THIS career's one session's
                                              # plushie result_value (the
                                              # minigame is capped at once per
                                              # career -- see CRANE_PLAYED_KEY),
                                              # read at career-finish time by
                                              # trained_chara.py (string
                                              # literal there, not an import,
                                              # same circular-import dodge as
                                              # "idle_single_mode_run" above)
                                              # for missions.py's
                                              # ClawMachinePlushieSession
                                              # (100023).
_CRANE_INTRO_EVENT = 6002


@career_events.resolver("crane_outcome")
def _resolve_crane_outcome(full_state, chara_info, event, choice_number, **kw):
    """The ack of the crane-game outcome story is where the real server pays
    out (capture: the Straightaway Recovery hint landed exactly here;
    vital/mood were capped and reported via not_up_parameter_info [6, 20]).
    Moved verbatim from the old CRANE-GAME OUTCOME RESOLUTION branch; tier
    rides on the Event's own payload instead of CRANE_CTX_KEY."""
    tier = (event.payload or {}).get("tier") or 1
    vital_add, mood_add, hint_add = _CRANE_REWARDS.get(tier, _CRANE_REWARDS[1])
    not_up = []
    max_vital = chara_info.get("max_vital", 100)
    if chara_info.get("vital", 0) >= max_vital:
        not_up.append(6)     # vital already capped (capture value)
    chara_info["vital"] = min(max_vital, chara_info.get("vital", 0) + vital_add)
    if tier == 1:
        cap = chara_info.get("max_guts", 9999)
        chara_info["guts"] = min(chara_info.get("guts", 0) + 3, cap)
    if mood_add:
        if chara_info.get("motivation", 3) >= 5:
            not_up.append(20)  # mood already capped (capture value)
        chara_info["motivation"] = min(5, chara_info.get("motivation", 3) + mood_add)
    if hint_add:
        tips = chara_info.setdefault("skill_tips_array", [])
        tip = next((t for t in tips if t.get("group_id") == _CRANE_HINT_GROUP
                    and t.get("rarity") == 1), None)
        if tip:
            tip["level"] = min(5, (tip.get("level") or 0) + hint_add)
        else:
            tips.append({"group_id": _CRANE_HINT_GROUP, "rarity": 1,
                         "level": hint_add})
    return {"not_up": not_up} if not_up else {}


@career_events.resolver("crane_launch")
def _resolve_crane_launch(full_state, chara_info, event, choice_number, **kw):
    """Flips playing_state to 6 (minigame active) -- the client's cue to run
    its native claw-machine minigame. The caller (handle_ura_check_event)
    still special-cases this event_id to force an EMPTY unchecked_event_array
    afterward, same as before migration: nothing may be drained here, the
    client comes back via single_mode/minigame_end."""
    chara_info["playing_state"] = 6
    return {}


def _crane_available(full_state: dict) -> bool:
    return not full_state.get(CRANE_PLAYED_KEY)


def _queue_crane_game(response: dict, full_state: dict, career_state: dict) -> None:
    chara = (career_state["data"]["chara_info"].get("card_id") or 0) // 100
    if not chara or not _crane_available(full_state):
        return
    entry = _scenario_event_entry(_CRANE_INTRO_EVENT, 500000000 + chara * 1000 + 726,
                                  play_timing=6, choices=[_ack_choice()], chara_id=chara)
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=_CRANE_INTRO_EVENT, story_id=entry.get("story_id"), raw=entry,
        priority=career_events.PRIO_SPECIAL, resolver="crane_launch", source="crane"))
    full_state[CRANE_PLAYED_KEY] = 1   # once per career, even if it's abandoned


def handle_minigame_end(payload: dict) -> dict:
    """single_mode/minigame_end -- the client reports its crane-game result;
    serve the matching outcome story (see the wire-flow comment above)."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.pop(CRANE_CTX_KEY, None) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/minigame_end", full_state)
    chara = ctx.get("chara") or ((career["data"]["chara_info"].get("card_id") or 0) // 100)
    result = payload.get("result") or {}
    lifetime = full_state.setdefault(CRANE_LIFETIME_KEY, {"play_count": 0, "plushie_count": 0})
    lifetime["play_count"] = (lifetime.get("play_count") or 0) + 1
    session_plushies = int(result.get("result_value") or 0)
    lifetime["plushie_count"] = (lifetime.get("plushie_count") or 0) + session_plushies
    full_state[CRANE_SESSION_KEY] = session_plushies
    # TIER is result_STATE (1 fail / 2 success / 3 great success); result_VALUE
    # is the PLUSHIE COUNT. Confirmed by four live requests of our own:
    #   state 1 / value 0 (no plushies)   -> failure
    #   state 2 / value 4, state 2 / value 5 -> success
    #   state 3 / value 6                 -> great success
    # which is exactly the documented 0 / 1-5 / 6+ threshold rule. We had these
    # two swapped, so `mini_game_result == value` compared a story tier (1-3)
    # against a plushie count (0,4,5,6), never matched, and fell through to the
    # hardcoded 731 'Success!' EVERY time -- a great success played the plain
    # success story (live-reported) and every outcome paid failure rewards
    # (result_value wasn't a key in _CRANE_REWARDS either).
    tier = result.get("result_state")
    tier = tier if tier in _CRANE_REWARDS else 1
    base = 500000000 + chara * 1000
    suffix = 731
    rows = master_data.query(
        "SELECT story_id, mini_game_result FROM single_mode_story_data "
        "WHERE story_id BETWEEN ? AND ?", (base + 727, base + 731))
    matches = [r["story_id"] - base for r in rows if r["mini_game_result"] == tier]
    if matches:
        suffix = max(matches)
    outcome_event = _CRANE_INTRO_EVENT + (suffix - 726)
    entry = _scenario_event_entry(outcome_event, base + suffix,
                                  play_timing=8, choices=[_ack_choice()], chara_id=chara)
    detail = [dict(d, viewer_id=payload.get("viewer_id"),
                   single_mode_chara_id=(career["data"]["chara_info"].get("single_mode_chara_id")
                                         if isinstance(career, dict) else 0),
                   turn=payload.get("current_turn"), minigame_id=1)
              for d in (result.get("result_detail_array") or [])]
    entry["minigame_result"] = {"result_state": result.get("result_state"),
                                "result_value": result.get("result_value"),
                                "result_detail_array": detail}

    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    if isinstance(career, dict):
        updated = copy.deepcopy(career["data"]["chara_info"])
        updated["playing_state"] = 5
        data["chara_info"] = updated
        career_home = career["data"].get("home_info")
        if isinstance(career_home, dict):
            data["home_info"] = copy.deepcopy(career_home)
    # Rewards apply at the ACK of this outcome event (real-server timing) --
    # the tier rides on the Event's own payload for that check_event (the
    # "crane_outcome" resolver) instead of a separate ctx key.
    _emit_turn_event(response, full_state, career_events.Event(
        event_id=outcome_event, story_id=base + suffix, raw=entry,
        priority=career_events.PRIO_SPECIAL, resolver="crane_outcome",
        source="crane", payload={"tier": tier}))
    _remember_display(full_state, response)
    state_store.save_state(viewer_id, full_state)
    return response


def _lineage_factor_groups(viewer_id, full_state: dict) -> dict:
    """{factor_group_id: count} across the 2 parents + their grandparents --
    feeds the white-spark lineage bonus (guide: appearance = base x 1.1^N
    where N = lineage members holding the SAME skill gene). Same member
    enumeration as _roll_inspiration."""
    counts: dict[int, int] = {}
    start_chara = full_state.get(START_CHARA_STATE_KEY) or {}
    roster = _roster_by_trained_id(viewer_id)
    for key in ("succession_trained_chara_id_1", "succession_trained_chara_id_2"):
        parent = roster.get(start_chara.get(key)) if start_chara.get(key) else None
        if not parent:
            continue
        # BUG FIXED 2026-08-27: grandparents are each parent's own POSITION
        # 10 AND 20 entries (her own two parents), not every entry in her
        # array -- 11/12/21/22 are HER OWN grandparents, this trainee's
        # great-grandparents, one generation past what the game tracks. See
        # _apply_inheritance's and _roll_inspiration's matching fix.
        by_pos = {a.get("position_id"): a for a in parent.get("succession_chara_array") or []
                  if isinstance(a, dict)}
        members = [parent] + [by_pos[p] for p in (10, 20) if p in by_pos]
        for m in members:
            for f in m.get("factor_info_array") or []:
                g = (f.get("factor_id") or 0) // 100
                counts[g] = counts.get(g, 0) + 1
    return counts


def _roll_career_factors(chara_info: dict, race_history, lineage_groups=None) -> list:
    """The REAL career-end spark roll, guide-verified odds (umamusustation /
    hakuraku datasets via Crazyfellow's guide) + master.mdb-verified id
    construction (factor_id = group*100 + star):

    BLUE: 1 of 5 stats uniform; 3-star needs the stat >=600, ~6% at 600-1099,
    ~11% at 1100+. RED: uniform among aptitudes at A(7)+; fixed 20/70/10.
    GREEN (own unique): guaranteed; star by rank-score band (<6500: 90/10/0,
    <17500: 50/45/5, else 20/70/10). WHITE: each LEARNED skill ~20% to
    appear, star by the same band. RACE: each G1 won in 1st ~20%, id =
    (9000+race_id)*100+star. SCENARIO: URA completed (state==3) -> group
    30001 at ~20%."""
    rolled = []
    stat_groups = {"speed": 1, "stamina": 2, "power": 3, "guts": 4, "wiz": 5}
    stat = random.choice(list(stat_groups))
    v = chara_info.get(stat, 0)
    if v < 600:
        star = random.choices((1, 2), (90, 10))[0]
    elif v < 1100:
        star = random.choices((1, 2, 3), (48, 46, 6))[0]
    else:
        star = random.choices((1, 2, 3), (21, 68, 11))[0]
    rolled.append(stat_groups[stat] * 100 + star)

    apt_groups = {"proper_ground_turf": 11, "proper_ground_dirt": 12,
                  "proper_running_style_nige": 21, "proper_running_style_senko": 22,
                  "proper_running_style_sashi": 23, "proper_running_style_oikomi": 24,
                  "proper_distance_short": 31, "proper_distance_mile": 32,
                  "proper_distance_middle": 33, "proper_distance_long": 34}
    eligible = [g for k, g in apt_groups.items() if (chara_info.get(k) or 1) >= 7]
    if eligible:
        rolled.append(random.choice(eligible) * 100
                      + random.choices((1, 2, 3), (20, 70, 10))[0])

    from .. import rating_formula
    score = rating_formula.get_career_score(chara_info)

    def band_star():
        if score < 6500:
            return random.choices((1, 2), (90, 10))[0]
        if score < 17500:
            return random.choices((1, 2, 3), (50, 45, 5))[0]
        return random.choices((1, 2, 3), (20, 70, 10))[0]

    card = chara_info.get("card_id") or 0
    if master_data.query_one(
            "SELECT factor_id FROM succession_factor WHERE factor_group_id=? AND factor_type=3 "
            "LIMIT 1", (card,)):
        rolled.append(card * 100 + band_star())

    lineage = lineage_groups or {}
    for s in chara_info.get("skill_array") or []:
        sid = s.get("skill_id")
        row = master_data.query_one(
            "SELECT sfe.factor_group_id AS g FROM succession_factor_effect sfe "
            "JOIN succession_factor sf ON sf.factor_group_id=sfe.factor_group_id "
            "AND sf.factor_type=4 WHERE sfe.target_type=41 AND sfe.value_1=? "
            "ORDER BY sfe.factor_group_id LIMIT 1", (sid,))
        g = row["g"] if row else None
        if g is None and master_data.query_one(
                "SELECT factor_id FROM succession_factor WHERE factor_group_id=? "
                "AND factor_type=4 LIMIT 1", (sid // 10,)):
            g = sid // 10
        if not g:
            continue
        # Guide-verified appearance model: base x 1.1^N (N = parents/GPs
        # carrying the SAME skill gene). Base 20% cap 35% for normal skills;
        # a learned GOLD version (skill_data.rarity=2) rolls at 40% cap 70%.
        rar = master_data.query_one("SELECT rarity FROM skill_data WHERE id=?", (sid,))
        base, cap = (0.40, 0.70) if (rar and rar["rarity"] == 2) else (0.20, 0.35)
        chance = min(cap, base * (1.1 ** lineage.get(g, 0)))
        if random.random() < chance:
            rolled.append(g * 100 + band_star())

    seen_races = set()
    for h in race_history or []:
        if h.get("result_rank") != 1:
            continue
        row = master_data.query_one(
            "SELECT ri.race_id AS rid, r.grade AS grade FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id=p.race_instance_id "
            "JOIN race r ON r.id=ri.race_id WHERE p.id=?", (h.get("program_id"),))
        if (row and row["grade"] == 100 and 1001 <= row["rid"] <= 1024
                and row["rid"] not in seen_races):
            seen_races.add(row["rid"])
            if random.random() < 0.20:
                rolled.append((9000 + row["rid"]) * 100 + band_star())

    if chara_info.get("state") == 3 and random.random() < 0.20:
        rolled.append(3000100 + band_star())
    return [{"factor_id": fid, "level": 0} for fid in rolled]


def handle_factor_select(payload: dict) -> dict:
    """single_mode/factor_select -- the post-career spark roll screen (fires
    after BOTH a failed and a completed career, before finish; captured live:
    request {is_force_delete, current_turn, factor_lottery_id} -> response
    single_mode_factor_select_common with the rolled 3-factor set + rank).
    Rolls via trained_chara's real factor generator and stashes the result so
    the flow is stable across re-serves. (The finish-built legacy currently
    rolls its own factors -- aligning the two is a noted follow-up.)"""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    stashed = _ensure_factor_roll(viewer_id, full_state)
    # The request's factor_lottery_id IS the player's pick once more than one
    # roll exists (single_mode/factor_lottery buys the extra rolls). Recording
    # it here is what makes handle_finish bank the set the player chose instead
    # of always the first one -- see _selected_factors.
    picked = payload.get("factor_lottery_id")
    if picked and any(r["lottery_id"] == int(picked) for r in stashed["rolls"]):
        stashed["selected_lottery_id"] = int(picked)
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_select_common": {
                "rank_score": stashed["rank_score"], "rank": stashed["rank"],
                "lottery_remain_num": _FACTOR_RELOTTERY_LIMIT - (len(stashed["rolls"]) - 1),
                "lottery_count": len(stashed["rolls"]),
                "factor_select_info_array": [
                    {"lottery_id": r["lottery_id"],
                     "factor_info_array": r["factors"]} for r in stashed["rolls"]],
                "factor_relottery_info_array": []}}}


# How many PAID re-rolls a career allows on top of the free first roll. No
# master.mdb table carries this (every factor_research_* table in this build is
# empty, and nothing else holds a relottery cost) and we have no capture of the
# screen, so it is a house number rather than a wire fact. The TP charge is NOT
# invented the same way: the client states what it means to spend in the
# request's own use_tp, and that is exactly what gets deducted.
_FACTOR_RELOTTERY_LIMIT = 2

# The career-scoped skip/fast-forward setting (single_mode/change_short_cut).
SHORT_CUT_KEY = "career_short_cut_state"


def _ensure_factor_roll(viewer_id, full_state: dict) -> dict:
    """The career's spark-roll stash, created on demand and forward-migrated.

    Shape: {factors, rank_score, rank, rolls: [{lottery_id, factors}],
    selected_lottery_id}. `factors` stays as the roll-1 mirror purely so a
    career stashed by the pre-re-roll code keeps working; new readers should go
    through _selected_factors instead."""
    stashed = full_state.get("career_factor_roll")
    career = full_state.get(STATE_KEY)
    ci = career["data"]["chara_info"] if isinstance(career, dict) else {}
    if not stashed:
        from .. import rating_formula
        history = career["data"].get("race_history") if isinstance(career, dict) else []
        factors = _roll_career_factors(ci, history,
                                       _lineage_factor_groups(viewer_id, full_state))
        score = rating_formula.get_career_score(ci)
        stashed = {"factors": factors, "rank_score": score,
                   "rank": trained_chara._rank_for_score(score)}
        full_state["career_factor_roll"] = stashed
    if not stashed.get("rolls"):
        stashed["rolls"] = [{"lottery_id": 1, "factors": stashed["factors"]}]
    stashed.setdefault("selected_lottery_id", stashed["rolls"][0]["lottery_id"])
    return stashed


def _selected_factors(stashed):
    """The factor set the player actually kept. Falls back to the roll-1 mirror
    for a career stashed before re-rolls existed."""
    if not isinstance(stashed, dict):
        return None
    want = stashed.get("selected_lottery_id")
    for roll in stashed.get("rolls") or ():
        if roll.get("lottery_id") == want:
            return roll.get("factors")
    return stashed.get("factors")


def handle_factor_lottery(payload: dict) -> dict:
    """single_mode/factor_lottery {lottery_count, tp_info, use_tp} -- pay TP to
    roll an ADDITIONAL spark set on the post-career screen, alongside the free
    one factor_select already rolled.

    Never captured; the shapes are read straight off dump.cs
    (SingleModeFactorLotteryRequest / SingleModeFactorLotteryCommon{tp_info,
    lottery_remain_num, lottery_count, select_lottery_id,
    factor_select_info_array}). It cannot be left on the no-op fallback: a
    rolled outcome the server does not persist desyncs the sparks the screen
    shows from the ones the legacy actually banks, which is exactly the failure
    mode already recorded for pre-rolled outcomes in this codebase.

    TP comes out of the real pool via stamina.TP.spend, charging exactly the
    use_tp the client asked to spend -- refused with 205 when the pool cannot
    cover it, or when the career has already used its re-rolls, rather than
    quietly handing back a free roll."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/factor_lottery", full_state)
    stashed = _ensure_factor_roll(viewer_id, full_state)
    if len(stashed["rolls"]) > _FACTOR_RELOTTERY_LIMIT:
        log.warning("factor_lottery refused: %s rolls already used (limit %s+1)",
                    len(stashed["rolls"]), _FACTOR_RELOTTERY_LIMIT)
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    use_tp = int(payload.get("use_tp") or 0)
    pool = stamina.TP.spend(full_state, use_tp)
    if pool is None:
        log.warning("factor_lottery refused: %s TP requested, pool has %s",
                    use_tp, stamina.tp_info(full_state).get("current_tp"))
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    ci = career["data"]["chara_info"]
    factors = _roll_career_factors(ci, career["data"].get("race_history") or [],
                                   _lineage_factor_groups(viewer_id, full_state))
    lottery_id = max(r["lottery_id"] for r in stashed["rolls"]) + 1
    stashed["rolls"].append({"lottery_id": lottery_id, "factors": factors})
    # The new roll is NOT auto-selected -- the player picks it via factor_select.
    log.info("factor_lottery: roll %s for %s TP (%s rolls now)",
             lottery_id, use_tp, len(stashed["rolls"]))
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"single_mode_factor_lottery_common": {
                "tp_info": stamina.tp_info(full_state),
                "lottery_remain_num": _FACTOR_RELOTTERY_LIMIT - (len(stashed["rolls"]) - 1),
                "lottery_count": len(stashed["rolls"]),
                "select_lottery_id": stashed["selected_lottery_id"],
                "factor_select_info_array": [
                    {"lottery_id": r["lottery_id"],
                     "factor_info_array": r["factors"]} for r in stashed["rolls"]]}}}


def handle_change_short_cut(payload: dict) -> dict:
    """single_mode/change_short_cut {short_cut_state, current_turn} -- the
    career skip / fast-forward setting (how much of each event the client plays
    out). Aliased onto every scenario prefix by _install_scenario_routes.

    It is a preference, but not a client-only one, so it cannot stay on the
    no-op fallback: SingleModeChangeShortCutResponse.CommonResponse declares
    {chara_info, unchecked_event_array, <scenario>_data_set}, and NOOP_SUCCESS's
    empty `data` deserializes all three to null in the middle of a live career
    -- the same shape behind present/index's blank tab and the familiar
    null-data_set softlocks. So: persist the flag, hand back the standard
    training-state envelope, and let the outbound sync_chara_info attach
    whichever data_set the running scenario owns.

    Never captured (no logged session has posted it), so the response shape is
    dump.cs's declaration rather than a wire fact -- but a well-formed career
    snapshot is the safe end of that uncertainty."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career = full_state.get(STATE_KEY)
    if not isinstance(career, dict):
        return _no_career_refusal(viewer_id, "single_mode/change_short_cut", full_state)

    full_state[SHORT_CUT_KEY] = int(payload.get("short_cut_state") or 0)
    chara_info = career["data"]["chara_info"]
    response = _load_ura_race_seed("train_check_event")
    data = response["data"]
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    # No events: this is a settings toggle, and re-emitting the turn's pending
    # chain here would play it a second time.
    data["unchecked_event_array"] = []
    # The seed is a race-turn capture; a settings toggle sends none of that.
    for key in ("race_condition_array", "race_start_info", "race_running_style",
                "event_effected_factor_array", "not_down_parameter_info"):
        data.pop(key, None)
    state_store.save_state(viewer_id, full_state)
    return response


_ALARM_CLOCK_ITEM = 95          # text_data category 23 index 95 = 'Alarm Clock'
_FREE_CONTINUE_ALLOWANCE = 1    # capture home_info: free_continue_num 1
_PAID_CONTINUE_ALLOWANCE = 3    # capture home_info: available_continue_num 3


def _alarm_clock_stock(full_state: dict) -> int:
    for it in full_state.get("item_list_state") or []:
        if it.get("item_id") == _ALARM_CLOCK_ITEM:
            return int(it.get("number") or 0)
    return 0


def _spend_alarm_clock(full_state: dict) -> bool:
    items = full_state.setdefault("item_list_state", [])
    for it in items:
        if it.get("item_id") == _ALARM_CLOCK_ITEM and int(it.get("number") or 0) > 0:
            it["number"] = int(it["number"]) - 1
            return True
    return False


def _continue_counts(full_state: dict, ctx: dict | None) -> dict:
    """The four retry fields a real career's home_info carries. Both budgets are
    scoped to the race currently on the table (they live on RACE_CTX_KEY, which
    race_out clears), and the alarm-clock count is capped by what the player
    actually owns -- offering a retry they can't pay for is how the button ends
    up doing nothing."""
    ctx = ctx or {}
    free_left = max(0, _FREE_CONTINUE_ALLOWANCE - int(ctx.get("continue_free_used") or 0))
    paid_left = max(0, _PAID_CONTINUE_ALLOWANCE - int(ctx.get("continue_paid_used") or 0))
    return {
        "available_free_continue_num": free_left,
        "available_continue_num": min(paid_left, _alarm_clock_stock(full_state)),
        "free_continue_num": _FREE_CONTINUE_ALLOWANCE,
    }


def handle_continue(payload: dict) -> dict:
    """single_mode/continue -- retry a lost goal race (real endpoint, contract
    from the Icarus client library: {current_turn, continue_type} where 1 =
    free daily retry, 2 = alarm clock; the client re-calls race_start on the
    SAME turn afterwards; result_code 205 = not retryable and the client
    accepts the result). Re-simulates the pending race so the retry is a
    genuinely fresh roll, not a replay of the same loss.

    continue_type 2 was live-reported as still broken after type 1 was fixed.
    It cost nothing and reported nothing: no Alarm Clock was consumed and
    `available_continue_num` -- the field the client actually reads to decide
    whether a paid retry is on offer -- was never sent, so it kept whatever the
    career-start fixture froze it at. Worse, the one line that did touch the
    counters HANDED BACK a free retry on every alarm-clock use
    (`available_free_continue_num = 0 if type == 1 else 1`). Both budgets are
    now tracked per race and spent for real."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY)
    chara_info = _career_chara_info(viewer_id)
    # A WON race can't be retried; a LOST one can -- even though race_end has
    # already committed by the time the player sees the result and presses
    # retry (it always has: the result screen IS race_end). The old guard
    # refused on end_committed, i.e. on every real retry, so the button never
    # worked (live-reported). Instead, roll the committed loss back.
    if not ctx or chara_info is None or ctx.get("won"):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
    # Pay for it before anything is rolled back -- a refused retry must leave
    # the lost race exactly as it stands.
    continue_type = payload.get("continue_type")
    counts = _continue_counts(full_state, ctx)
    if continue_type == 2:
        if counts["available_continue_num"] <= 0 or not _spend_alarm_clock(full_state):
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
        ctx["continue_paid_used"] = int(ctx.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}
        ctx["continue_free_used"] = int(ctx.get("continue_free_used") or 0) + 1
    career = full_state.get(STATE_KEY)
    if ctx.get("end_committed") and isinstance(career, dict):
        cd = career["data"]
        hist = cd.get("race_history") or []
        if hist and hist[-1].get("program_id") == ctx.get("program_id"):
            hist.pop()
        ci_live = cd["chara_info"]
        ci_live["fans"] = max(0, (ci_live.get("fans") or 0)
                              - (ctx.get("gained_fans_committed") or 0))
        ci_live["playing_state"] = 2   # back to the pre-race state
        ctx.pop("pending_reward", None)
        ctx.pop("gained_fans_committed", None)
        ctx["end_committed"] = False
        chara_info = copy.deepcopy(ci_live)
    race_info = _race_info_for_program(ctx.get("program_id")) or {}
    sim = None
    if race_info.get("race_instance_id"):
        sim = _simulate_career_race(
            chara_info, viewer_id, race_info["race_instance_id"],
            program_id=race_info.get("program_id"),
            # a CONTINUE re-runs the race -- Happy Meek must still be in it
            with_happy_meek=_meek_in_finals(full_state, chara_info,
                                            ctx.get("finals_round")),
            happy_meek_level=_happy_meek_finals_level(full_state),
            finals_rivals=_scenario_finals_rivals(
                chara_info, ctx.get("finals_round")))
    prev_finish = ctx.get("player_finish_order")
    if sim:
        rsi = ctx.get("race_start_info") or {}
        rsi["race_horse_data"] = sim["race_horse_data"]
        rsi["random_seed"] = sim["random_seed"]
        ctx.update(race_start_info=rsi, race_scenario=sim["race_scenario"],
                   player_finish_order=sim["player_finish_order"],
                   player_finish_time=sim.get("player_finish_time"),
                   player_popularity=sim.get("player_popularity"),
                   rival_finish_orders=sim.get("rival_finish_orders"),
                   beat_happy_meek=sim.get("beat_happy_meek", False))
    # LOUD ON PURPOSE. A retry that re-rolls nothing is invisible from the
    # outside -- the client just replays a race that looks identical -- so the
    # seed and the new finish order go in the log every time (user-reported
    # 2026-09-07: "the race is identical, the end times are identical". The
    # re-roll measured healthy here, so the next report has evidence either way).
    log.info("continue: type %s, race %s -> %s (seed %s, was finish %s)",
             continue_type, ctx.get("program_id"),
             "re-simulated -> finish %s" % ctx.get("player_finish_order") if sim
             else "NOT re-simulated (no simulation available)",
             (sim or {}).get("random_seed"), prev_finish)
    ctx["continue_used"] = ctx.get("continue_used", 0) + 1
    full_state[RACE_CTX_KEY] = ctx
    career = full_state.get(STATE_KEY)
    # The response must carry the SAME block race_start does. dump.cs's
    # SingleModeContinueResponse.CommonResponse (746087) is
    # {chara_info, home_info, race_start_info, unchecked_event_array, user_item,
    # ura_data_set} -- we were sending home_info and an `item_list` field that
    # does not exist on it. With no race_start_info the client accepted the
    # retry and then died rebuilding the pre-race screen:
    #
    #   NullReferenceException
    #     at Gallop.SingleModeChangeViewManager.<ChangePaddock>b__0
    #     at Gallop.RaceResultList+<RaceContinue>d__279.MoveNext
    #
    # (Player.log, 2026-09-04.) The paddock IS race_start_info, so the retry
    # crashed every time instead of re-running the race.
    data = {}
    race_ci = copy.deepcopy(chara_info)
    race_ci["playing_state"] = 2
    race_ci["state"] = 0
    race_ci["race_program_id"] = ctx.get("program_id")
    data["chara_info"] = race_ci
    if ctx.get("race_start_info"):
        data["race_start_info"] = copy.deepcopy(ctx["race_start_info"])
    data["unchecked_event_array"] = []
    if isinstance(career, dict) and isinstance(career["data"].get("home_info"), dict):
        # Persist the spent budgets on the career's own home_info, not just this
        # response: every later response serves that copy back, so a
        # response-only edit was undone by the next call.
        career["data"]["home_info"].update(_continue_counts(full_state, ctx))
        data["home_info"] = copy.deepcopy(career["data"]["home_info"])
        # home_info in the response makes the shared backfill attach
        # ura_data_set, the sixth field of the contract.
    if continue_type == 2:
        # `user_item` is ONE {item_id, number} -- the alarm clock's new count --
        # not a list, which is why the spend never showed up in the client.
        data["user_item"] = {"item_id": _ALARM_CLOCK_ITEM,
                             "number": _alarm_clock_stock(full_state)}
    state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": data}


def _apply_camp_rest(chara_info: dict, current_vital: int):
    """Summer camp's merged Rest & Recreation: fixed +40 energy, +1 mood (no
    30/50/70 rest roll, no separate outing roll). Returns 304 for
    command_result.command_id -- group 304's only member, which is what makes
    the client play the camp cutin (103040, swimsuit dress), same
    group->member echo the real server does for normal outings (captured
    cg=301 -> command_result 302)."""
    chara_info["vital"] = min(chara_info.get("max_vital", 100), current_vital + _CAMP_REST_ENERGY)
    chara_info["motivation"] = min(_MOTIVATION_MAX,
                                   chara_info.get("motivation", 3) + _CAMP_REST_MOOD)
    return _CAMP_OUTING_GROUP


def _apply_recreation(chara_info: dict, current_vital: int):
    """Apply a Recreation outing with the real outcome rates (Percentages-of-Events
    guide) and return (command_result.command_id, energy_capped) -- the CHOSEN
    outing group (301 Riverbank / 302 Karaoke / 303 Shrine), which is what makes
    the client play that outing's animation, and whether an energy gain the
    outing rolled was wasted on an already-full vital bar (Karaoke never rolls
    energy, so it's always False there). Karaoke +2 mood (35%); Riverbank
    stroll +1 mood +10 en (30%); Shrine +1 mood +10/+20/+30 en (20%/10%/5%).
    (Rest is handled separately via _roll_rest_outcome, whose energy is
    deferred to the event outcome.)"""
    mv = chara_info.get("max_vital", 100)
    capped = False

    def add_energy(n):
        nonlocal capped
        capped = current_vital >= mv
        chara_info["vital"] = min(mv, current_vital + n)

    def add_mood(n):
        chara_info["motivation"] = min(_MOTIVATION_MAX, chara_info.get("motivation", 3) + n)

    r = random.random()
    if r < 0.35:                 # Karaoke
        add_mood(2)
        return _OUTING_KARAOKE, capped
    if r < 0.65:                 # Riverbank stroll
        add_mood(1); add_energy(10)
        return _OUTING_RIVERBANK, capped
    if r < 0.85:                 # Shrine +10
        add_mood(1); add_energy(10)
    elif r < 0.95:               # Shrine +20
        add_mood(1); add_energy(20)
    else:                        # Shrine +30
        add_mood(1); add_energy(30)
    return _OUTING_SHRINE, capped


def _roll_training_failure(command_id, current_vital: int, conditions=None, deck=(),
                           in_facility_ids=(), support_card_levels: dict = None):
    """Roll whether a training FAILS (no apply). Returns (failed, is_worst).
    Facility-aware failure_rate; escalates to the 'Worst' (Don't Overdo It) event
    at >= 80%, else 'Normal' (Get Well Soon). The outcome is applied later when
    the player commits the Top/Bottom choice (see resolve_failure). Wit (106)
    can 'fail' too but has no infirmary event -- handled by the caller.

    in_facility_ids/support_card_levels: EFFECT_FAILURE_RATE_ZERO_CHANCE (112,
    gametora-confirmed) -- an unlocked card actually placed in this facility
    gets its own independent chance to zero the fail rate BEFORE the normal
    roll. See training_formula.training_failure_zero_proc."""
    stat = _COMMAND_TRAINED_STAT.get(command_id)
    if stat is None:
        return False, False
    if training_formula.training_failure_zero_proc(in_facility_ids, support_card_levels):
        return False, False
    fail_pct = training_formula.failure_rate(
        current_vital, command_id=command_id, conditions=conditions, deck=deck)
    if fail_pct <= 0 or random.random() * 100 >= fail_pct:
        return False, False
    return True, fail_pct >= 80


def _simulate_exec_command(career_state: dict, payload: dict, unlocked_npcs=(),
                           training_bonus: dict = None,
                           friendship_bonus: int = 0,
                           specialty_bonus: int = 0,
                           full_state: dict = None) -> dict:
    """Formula-based training simulation (see training_formula.py -- base gains
    from master.mdb, plus mood/support/growth multipliers and stat caps). The
    response is built off the captured exec_command as a STRUCTURAL template but
    the event/turn-advance payload is made clean: chara_info is the player's
    updated run, command_result matches the request, and unchecked_event_array
    is emptied (the training/turn events are Stage 2 -- leaving the recorded
    run's events in stalls the client's turn transition).

    Prefer the REAL URA single_mode/exec_command capture (scenario_id 1, has
    ura_data_set, no team_data_set) over single_mode_team/exec_command (a
    Unity Cup/TEAM capture, scenario_id 2, DOES carry team_data_set and has NO
    ura_data_set) -- same fixture-preference bug already root-caused for
    single_mode/load (see handle_load's docstring): every URA training-screen
    response was structurally shaped like a Team-scenario one, an
    inconsistency (team_data_set present on a scenario_id=1 career) the real
    client isn't expecting on ANY response, not just load. sync_chara_info's
    ura_data_set backfill already covers the missing-field half of this
    (see its docstring); the wrong-fixture template was still leaking the
    extra team_data_set field into every single training turn."""
    template_pair = fixtures.first("single_mode/exec_command") or fixtures.first("single_mode_team/exec_command")
    response = template_pair.response_copy()
    chara_info = copy.deepcopy(career_state["data"]["chara_info"])
    # BUG FOUND 2026-08-20 (user-prompted -- "do we still send static data if
    # it's close enough?"): the preferred single_mode/exec_command capture
    # doesn't exist in this machine's local fixture set (only the UmaDumpy
    # dumps dir, itself missing here, ever had it -- see fixtures.py), so
    # this ALWAYS falls back to single_mode_team/exec_command, a real
    # Maruzensky (card_id 100402) Team-scenario capture. chara_info/
    # ura_data_set get properly overwritten below and by sync_chara_info,
    # but team_data_set is a scenario-2-ONLY field this response should not
    # even carry for a URA/Grand Live run, and nothing scrubs it -- it rides
    # along on EVERY training turn with Maruzensky's own real teammates'
    # chara_ids hardcoded in its evaluation_info_array (1068/1041/1055/1032/
    # 1009/1022), unlike chara_info's own evaluation_info_array which
    # _prune_roster (main.py's sync_chara_info) already corrects. Drop it
    # outright for any non-Team scenario, same as it would never have been
    # in a real response for one.
    if chara_info.get("scenario_id") != 2 and "team_data_set" in response.get("data", {}):
        del response["data"]["team_data_set"]
    # Owned cards' real levels, for support_card_unique_effect gating (112's
    # failure-zero proc below, 106's stack counter further down, and the next
    # turn's preview refresh at the bottom) -- one fetch shared by all three
    # instead of each re-hitting state_store.
    support_card_levels = _support_card_levels(full_state) if full_state else {}

    command_id = payload.get("command_id")
    command_group_id = payload.get("command_group_id")
    command_type = payload.get("command_type")

    # Apply EXACTLY what the client is showing. The training screen renders
    # command_info_array[].params_inc_dec_info_array verbatim (target_type 1-5 =
    # speed/stamina/power/guts/wiz, 10 = vital, 30 = skill points) -- so reading
    # the gain back from the command_info the client currently displays makes
    # "what you see" == "what you get". (Recomputing with the formula instead
    # gave a different, larger number than the preview -> the +14 shown / +34
    # applied desync.) The formula still populates the preview values; matching
    # the preview to the player's real deck each turn is the next step.
    preview = _preview_gains_for(career_state, command_id)
    # A scenario may award more than home_info displays -- Unity Cup's teammate
    # bonus is a second array the client sums into the same number. Merged HERE
    # rather than in the hook that builds home_info, so the client is not shown
    # the bonus twice, and merged BEFORE _apply_preview_gains so the served
    # response already carries the full gain (and the cap / "superb form"
    # handling covers it too).
    if preview and full_state is not None and command_type == 1:
        bonus = scenarios.for_chara(
            career_state.get("data", {}).get("chara_info")).training_award_bonus(
                full_state, career_state.get("data", {}).get("chara_info"), command_id)
        if bonus:
            preview = _merge_preview_params(preview, bonus)
    current_vital = payload.get("current_vital", chara_info.get("vital", 100))

    # Roll a training failure FIRST (only for real facility training). On failure
    # the stat gains are forfeited; the mood/stat penalty is applied later when
    # the player picks the Top/Bottom infirmary choice (handle_exec_command
    # queues the 7014/7015 event + stores the context). EXCEPTION: wit (106) has
    # no infirmary event -- it just fails with its usual +5 energy, no penalty.
    # Camp facilities (601-605) share the base facility's failure behaviour.
    base_command = _CAMP_BASE_BY_ID.get(command_id, command_id)
    pos_to_sid = {c["position"]: c["support_card_id"] for c in chara_info.get("support_card_array", [])}
    in_facility_ids = [pos_to_sid[p] for p in _partners_in_command(career_state, command_id)
                       if p in pos_to_sid]
    failed, worst = _roll_training_failure(
        base_command, current_vital, conditions=chara_info.get("chara_effect_id_array"),
        deck=_career_deck(chara_info), in_facility_ids=in_facility_ids,
        support_card_levels=support_card_levels)

    outing_group = None   # recreation: the chosen outing (301/302/303) to report
    data_infirmary = False  # infirmary visit -> queue event 7016 (see caller)
    slacked = False       # Slacker no-show -> queue her 'Slacking Off' story
    rest_outcome = None   # rest: the rolled-but-unapplied outcome (energy is deferred)
    hint_reveals = []     # training: queued skill-hint reveals, one per card that procced
    npc_partners = []     # training: Appraisal-capable NPCs (102/103) in the trained facility
    pal_partners = []     # training: [(position, card_id)] pal/group cards in that facility
    # Rest / Recreation must be detected BEFORE the training-preview path: the Rest
    # button carries a fixed +energy preview, so going through _apply_preview_gains
    # would apply that and return before firing the flavor event -- exactly the
    # "sometimes just +50, no event" symptom.
    is_rest = command_id in _REST_COMMANDS or command_type == 7
    is_recreation = command_group_id in _RECREATION_GROUPS or command_type == 3
    # Camp's merged Rest & Recreation (cg=304, or any type-3 command sent
    # during a camp turn) -- fixed +40/+1, NOT the normal outing roll and NOT
    # the deferred-rest flavor events (those narrate 30/50/70 energy).
    is_camp_rest = (command_group_id == _CAMP_OUTING_GROUP or command_id == _CAMP_OUTING_GROUP
                    or (is_recreation and _is_camp_turn(payload.get("current_turn"))))
    # "nothing happened" notices for this training result: energy already full
    # (6, see the crane game) and any stat the gains land on that is already at
    # its cap (1-5, "<stat> is in superb form") -- see _note_not_up.
    not_up = []
    if failed and base_command == 106:
        if current_vital >= chara_info.get("max_vital", 100):
            not_up.append(6)
        chara_info["vital"] = min(chara_info.get("max_vital", 100), current_vital + 5)
    elif failed:
        pass  # no gains; the penalty comes with the Top/Bottom choice
    elif is_camp_rest:
        if current_vital >= chara_info.get("max_vital", 100):
            not_up.append(6)
        outing_group = _apply_camp_rest(chara_info, current_vital)
    elif is_rest:
        # Roll the outcome but DON'T apply the energy -- it's the rest EVENT's
        # outcome, applied when the event resolves so the client animates it.
        rest_outcome = _roll_rest_outcome()
        outing_group = 701
    elif is_recreation:
        outing_group, energy_capped = _apply_recreation(chara_info, current_vital)
        if energy_capped:
            not_up.append(6)
    elif command_id == _INFIRMARY_COMMAND_ID or command_type == _INFIRMARY_COMMAND_TYPE:
        # INFIRMARY: chara_info is served UNCHANGED here (capture) -- the
        # energy/cure land when event 7016 resolves. Flagged for the caller.
        data_infirmary = True
        outing_group = _INFIRMARY_COMMAND_ID
    elif conditions.skips_training(chara_info):
        # SLACKER: 'may not show up to training' -- the facility is clicked but
        # nothing is gained this turn (energy still goes, like a wasted turn).
        #
        # ...and it PLAYS HER 'Slacking Off' STORY (suffix 712) rather than
        # silently eating the turn (live-reported). Same correction the other
        # negative conditions already got: a proc the player cannot see reads
        # as the server dropping their input. Flagged for the caller to queue
        # (see the _slacked handoff in handle_exec_command) because event
        # queueing needs `response`/`full_state`, which this builder has not
        # got. Slacker is the one condition whose proc is NOT a
        # _maybe_fire_condition_event roll -- it is absent from
        # _CONDITION_PROC_CHANCE on purpose, since its trigger is "a facility
        # was clicked", not "a turn passed" -- so this is the only place its
        # story can come from.
        chara_info["vital"] = max(0, current_vital - 5)
        applied = False
        slacked = True
        log.info("slacker: trainee skipped training this turn")
    else:
        applied = _apply_preview_gains(chara_info, preview, payload, not_up)
        # ...and the stats this facility trains that are ALREADY at their hard
        # cap. Those gain 0, and a 0 gain is omitted from the preview
        # altogether (see _build_training_command_info), so _apply_preview_gains
        # never sees them -- which is precisely the case that owes the player
        # "<stat> is in superb form". Reported from the facility's own
        # master.mdb profile instead, which is the only place that still knows
        # the stat was involved (user-reported 2026-09-07: the line appeared on
        # some trainings and not others; the ones it missed were the fully
        # capped ones).
        if applied:
            _note_capped_facility_stats(chara_info, command_id, not_up)
        if applied:
            # Bond gain: every support card placed in the trained facility this
            # turn gains bond (cap 100). At bond >= 80 in its own-type facility it
            # starts triggering friendship (rainbow) training, which the next
            # preview picks up automatically (previews read live bonds).
            # Scenario NPCs sharing the facility (Director 102 / Reporter 103 /
            # Meek 2001) gain their own, slower +2 (capture-measured) -- their
            # evaluation rows are ensured on the fly so careers started before
            # the rows were seeded still accrue.
            fac_index = training_formula._facility_index_for(command_id)
            # Grand Live's recruited-but-uncarded supporters share this partner
            # list (keyed by chara_id, see _npc_supporter_placements) but own no
            # card and therefore no bond gauge -- training alongside one must not
            # mint an `evaluation` value for her.
            gl_supporters = set(_npc_supporters(full_state or {}, chara_info))
            for pos in _partners_in_command(career_state, command_id):
                if pos in gl_supporters:
                    continue
                if pos in _NPC_TARGET_IDS:
                    _ensure_npc_eval(chara_info, pos)
                    gain = _NPC_BOND_GAIN
                else:
                    gain = _BOND_GAIN_PER_TRAINING
                # Charming doubles friendship gained from training partners.
                gain = conditions.bond_gain(chara_info, gain)
                bond_before = _bond_of(chara_info, pos)
                _set_bond(chara_info, pos, min(_BOND_MAX, bond_before + gain))
                # EFFECT_FRIENDSHIP_STACKING (106, gametora-confirmed) -- a real
                # commit only (never a preview), using the SAME friendship
                # trigger condition calculate_training_gain applies (own card
                # type == this facility, bond >= 80 BEFORE this gain). Stored
                # on full_state (server-internal), never on chara_info.
                sid = pos_to_sid.get(pos)
                if (full_state is not None and sid and bond_before >= 80
                        and fac_index is not None):
                    card = training_formula.CARD_BY_ID.get(str(sid))
                    ctype = card.get("type") if card else None
                    card_type_num = (training_formula.CARD_TYPE_NAMES.index(ctype)
                                     if ctype in training_formula.CARD_TYPE_NAMES else -1)
                    unique_lv = support_card_levels.get(sid)
                    if (card_type_num == fac_index and unique_lv
                            and training_formula.unique_effect(
                                sid, training_formula.EFFECT_FRIENDSHIP_STACKING,
                                level=unique_lv)):
                        stacks = full_state.setdefault(FRIENDSHIP_STACK_KEY, {})
                        stacks[str(sid)] = stacks.get(str(sid), 0) + 1
            npc_partners = [p for p in _partners_in_command(career_state, command_id)
                            if p in single_mode_events.APPRAISAL_EVENTS]
            # Pal/group cards standing in the trained facility -- captured HERE,
            # before command_info is refreshed for the NEXT turn, so it reflects
            # the placement the player actually trained (reading it later saw
            # tomorrow's placement and the events almost never fired).
            trained_positions = set(_partners_in_command(career_state, command_id))
            pal_partners = [(p, c) for p, c in _deck_pal_cards(chara_info)
                            if p in trained_positions]
            # Preview only -- like every other interactive moment here (rest,
            # duel, support/outing events), the actual skill_tips_array update
            # happens at check_event commit time (see handle_exec_command,
            # which queues these via _queue_turn_event, and handle_ura_check_event's
            # HINT_REVEAL_CTX_KEY branch, which applies each in turn).
            hint_reveals = _build_hint_reveals(chara_info, career_state, command_id, payload.get("current_turn"))
        else:
            # Some other non-training command (race entry etc.) -- spend vital.
            chara_info["vital"] = max(0, current_vital - 20)

    # Per-turn condition risks are NOT applied here any more: they play as their
    # own event (see _maybe_fire_condition_event), so the cost shows on its own
    # screen instead of being folded into the training result.

    # ADVANCE OFF THE RUN, NOT OFF THE REQUEST. This used to be
    # `payload["current_turn"] + 1`, which made the client the authority on
    # where the career is: a client still holding turn 1 while the run sat on
    # turn 71 wrote 2 back over it (live-reported 2026-09-07). The guard in
    # handle_exec_command has already refused any request whose claim we could
    # not be serving, so the two agree by the time we get here -- and when they
    # somehow do not, the run wins.
    chara_info["turn"] = int(chara_info.get("turn") or 0) + 1

    data = response["data"]
    data["chara_info"] = chara_info
    data["unchecked_event_array"] = []
    _note_not_up(data, not_up)
    # result_state 2 = success, 1 = failure (the client plays the failure
    # cutscene). The infirmary event (non-wit) is queued by handle_exec_command.
    # Recreation's command_id is 0, so echo its command_group_id instead.
    data["command_result"] = {"command_id": outing_group or command_id or command_group_id or 0,
                              "sub_id": 1, "result_state": 1 if failed else 2}
    data["_training_failed"] = failed and base_command != 106  # handoff flag (stripped later)
    data["_training_worst"] = worst
    data["_rest_outcome"] = rest_outcome   # handoff: rest event + deferred energy
    data["_hint_reveals"] = hint_reveals   # handoff: [(event_entry, effects), ...]
    data["_npc_partners"] = npc_partners   # handoff: NPCs who may fire an Appraisal
    data["_trained_command"] = base_command if npc_partners else None
    data["_pal_partners"] = pal_partners   # handoff: pal/group cards trained with
    data["_infirmary"] = data_infirmary     # handoff: queue the infirmary event
    data["_slacked"] = slacked              # handoff: queue 'Slacking Off'
    career_data = career_state["data"]
    # Count this training for the career's own tally (see TRAINING_TALLY_KEY).
    # Before the level-up check below, and unconditional on the scenario: every
    # scenario trains the same five facilities even where only some of them
    # level up by training.
    _record_training_tally(career_data, base_command, failed)
    # Count a facility train toward its level-up (camp ids 601-605 map to their
    # base 101-106; FAILED trainings don't count -- live-reported rule is 4
    # SUCCESSFUL trains). The (possibly higher) level is reflected in the NEXT
    # turn's command_info refreshed just below; a level-up also flags the
    # Director's 'Training Level Up!' event (queued by handle_exec_command).
    if not failed and scenarios.for_chara(chara_info).levels_facilities_by_training():
        # Carries the BASE command id rather than a bare True: the level-up
        # banner has one event_id per facility in Grand Live (see
        # Scenario.facility_levelup_event), and this is the only place that
        # knows which facility levelled. Still falsy when nothing levelled.
        _lvl_base = _CAMP_BASE_BY_ID.get(command_id, command_id)
        data["_facility_leveled"] = (
            _lvl_base if _record_facility_train(career_data, _lvl_base, chara_info)
            else False)
    # Refresh the NEXT turn's training previews for the updated chara_info and
    # persist them on the career, so what the client shows next == what we'll
    # apply next (sync_chara_info serves the career's home_info back to it).
    career_home = career_data.get("home_info")
    if isinstance(career_home, dict):
        _refresh_command_info(chara_info, career_home, turn=chara_info["turn"],
                              unlocked_npcs=unlocked_npcs, facility_levels=_facility_levels(career_data),
                              race_history=career_data.get("race_history", []),
                              training_bonus=training_bonus,
                              friendship_bonus=friendship_bonus,
                              specialty_bonus=specialty_bonus,
                              support_card_levels=support_card_levels,
                              friendship_stacks=_friendship_stacks(full_state),
                              full_state=full_state)
        if isinstance(data.get("home_info"), dict):
            data["home_info"]["command_info_array"] = copy.deepcopy(career_home["command_info_array"])
    career_data["chara_info"] = chara_info
    return response


# URA career length (Junior/Classic/Senior + finals). A run that hasn't reached
# this is a give-up / "delete data" -- it just clears, no legacy is created.
_CAREER_END_TURN = 78

# A never-trained owned character's chara_list entry, copied from the real
# account seed's own shape (charas 1016/1045).
_CHARA_LIST_TEMPLATE = {"training_num": 0, "love_point": 0, "fan": 1, "max_grade": 0,
                        "dress_id": 2, "mini_dress_id": 2, "love_point_pool": 0}


def _upsert_chara_collection(full_state: dict, final_chara_info: dict) -> None:
    """Record a finished career against the OWNED-CHARACTER collection
    (chara_list) -- the container every chara-keyed screen reads, including
    the concert member picker. Nothing ever wrote it before, so it stayed
    frozen at the account's captured snapshot while careers/cards moved on
    (live-reported: 'when selecting umamusume for the songs, i see my old
    roster'). Adds the character if missing, else accumulates the run."""
    card_id = (final_chara_info or {}).get("card_id")
    if not card_id:
        return
    row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
    chara_id = row["chara_id"] if row else card_id // 100
    charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
    if not isinstance(charas, list):
        return
    fans = final_chara_info.get("fans", 0) or 0
    grade = final_chara_info.get("chara_grade", 0) or 0
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    if entry:
        entry["training_num"] = (entry.get("training_num", 0) or 0) + 1
        entry["fan"] = (entry.get("fan", 0) or 0) + fans
        entry["max_grade"] = max(entry.get("max_grade", 0) or 0, grade)
    else:
        charas.append(dict(_CHARA_LIST_TEMPLATE, chara_id=chara_id,
                           training_num=1, fan=max(1, fans), max_grade=grade))


def handle_finish(payload: dict) -> dict:
    """single_mode/finish: ends the active career. ALWAYS clears the career (so
    the in-game "delete data" / give-up button reliably resets it -- previously
    a partial career could throw in build_trained_chara_from_career before the
    clear, so the career never went away). A COMPLETED run (reached the final
    turn) becomes a legacy trained_chara on the roster; a give-up/delete does
    not. Never raises -- a finish with no active career still returns cleanly."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    career_state = full_state.get(STATE_KEY)

    roster = None
    new_record = None
    final_chara_info = None
    love_point_info = None
    new_chara_profile_array = []
    if isinstance(career_state, dict):
        final_chara_info = career_state.get("data", {}).get("chara_info")
        turn = (final_chara_info or {}).get("turn", 0) or 0
        # A COMPLETED run (reached the end) or a goal-FAILED run (state=2 --
        # the real game still hands you the trained uma after a failed
        # career, it just plays the Normal ending) both become legacies;
        # only a voluntary give-up (state 0, pre-end) does not.
        if turn >= _CAREER_END_TURN or (final_chara_info or {}).get("state") == 2:
            # completed run -> add a legacy. Guarded so a build failure never
            # blocks the clear below.
            try:
                start_chara = full_state.get(START_CHARA_STATE_KEY) or {}
                roster = trained_chara._get_or_seed_roster(viewer_id)
                full_state = state_store.get_state(viewer_id)
                career_state = full_state[STATE_KEY]
                stashed_roll = full_state.pop("career_factor_roll", None)
                new_record = trained_chara.build_trained_chara_from_career(
                    viewer_id, career_state, start_chara, roster,
                    factor_info_array=_selected_factors(stashed_roll),
                    full_state=full_state)
                roster = roster + [new_record]
                # Veteran box capacity (user-specified 260, configurable).
                # Overflow drops the OLDEST unlocked veteran, never the new
                # one -- silently losing the run just finished would be worse
                # than losing the stalest record.
                cap = int(config.get("veteran_capacity",
                                     trained_chara.VETERAN_CAPACITY) or trained_chara.VETERAN_CAPACITY)
                while len(roster) > cap:
                    victim = next((i for i, r in enumerate(roster)
                                   if not (r.get("is_lock") or r.get("lock_flag"))), None)
                    if victim is None:
                        break                     # everything locked -- keep all
                    dropped = roster.pop(victim)
                    log.info("veteran box over capacity (%s): dropped oldest "
                             "unlocked trained_chara %s", cap,
                             dropped.get("trained_chara_id"))
                full_state[trained_chara.ROSTER_KEY] = roster
                _upsert_chara_collection(full_state, final_chara_info)
                # BOND. A completed career pays the trainee permanent bond
                # points on the run's final RANK -- see bond.career_love_points
                # for the captured ladder (B->15, A->16, SS->18). Only a run
                # that became a legacy pays: every captured give-up leaves
                # love_point and love_point_pool untouched, which is why this
                # sits inside the completed-run branch.
                love_chara_id = bond.chara_id_for_card(
                    (final_chara_info or {}).get("card_id") or 0)
                rank_id = (new_record or {}).get("rank") or 0
                if love_chara_id and rank_id:
                    love_point_info, new_chara_profile_array = bond.grant(
                        full_state, love_chara_id, bond.career_love_points(rank_id))
                state_store.save_state(viewer_id, full_state)
            except Exception:
                # Logged, not swallowed: this used to fail silently, and its
                # ONLY visible symptom was the fixture leak the smfc block
                # below now closes -- the fallback hid its own trigger.
                log.exception("finish: building the legacy trained_chara failed; "
                              "no legacy added for viewer %s", viewer_id)
                roster = new_record = None

            # Epithets + songs earned by this run. Own try/except and its own
            # read-modify-save so a failure here can never cost the player the
            # legacy record written just above.
            try:
                full_state = state_store.get_state(viewer_id) or {}
                career_data = (career_state or {}).get("data") or {}
                facts = epithets.build_facts(career_data, final_chara_info, full_state)
                earned = epithets.award_for_career(
                    full_state, career_data, final_chara_info, facts=facts)
                songs = concerts.check_and_grant(full_state, facts)
                if earned or songs:
                    if earned and isinstance(new_record, dict):
                        # The client reads nickname_id_array off the
                        # trained_chara for the epithet display / profile badge.
                        existing = list(new_record.get("nickname_id_array") or ())
                        new_record["nickname_id_array"] = existing + [
                            i for i in earned if i not in existing]
                        roster_now = full_state.get(trained_chara.ROSTER_KEY)
                        if isinstance(roster_now, list):
                            for rec in roster_now:
                                if rec.get("trained_chara_id") == new_record.get("trained_chara_id"):
                                    rec["nickname_id_array"] = new_record["nickname_id_array"]
                                    break
                    state_store.save_state(viewer_id, full_state)
            except Exception:
                log.exception("finish: epithet/concert award failed for viewer %s", viewer_id)

    # ALWAYS clear the active career (delete/give-up/complete all end it).
    clear_active_career(viewer_id)

    template = fixtures.first("single_mode_team/finish")
    response = template.response_copy() if template is not None else {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {"single_mode_finish_common": {}},
    }
    # The captured template's data_headers.notifications is a frozen
    # snapshot of whatever the ORIGINAL capturing account genuinely had
    # unread at capture time (a completed limited-time event mission,
    # unread_information_exists, trophy_badge_flag, specific honor_ids) --
    # same "frozen seed masquerading as live state" bug class as
    # tool/start_session's unread_information_exists fix in main.py and
    # load.py's unread_announce_id_array fix. Never computed live here, so
    # replaying it verbatim resurfaces someone else's stale one-time
    # notifications (e.g. "Obtain 50k career rating score" mission_id
    # 1001254) on EVERY single career finish, forever. No notifications is
    # the safe default absent real computation.
    if isinstance(response.get("data_headers"), dict):
        response["data_headers"]["notifications"] = {}
    smfc = response.get("data", {}).get("single_mode_finish_common")
    if isinstance(smfc, dict):
        # ALWAYS this account's own roster. `roster` is populated ONLY when a
        # completed run just became a legacy, so on a give-up, a delete-data, a
        # finish with no active career, or a build failure above, it stays None
        # -- and the captured template's own trained_chara is the CAPTURE
        # ACCOUNT'S 237 real-server veterans. "Leave it alone when we have
        # nothing to say" therefore served someone else's entire barn verbatim.
        # The client CACHES that as the player's own and then offers it on the
        # next career-start parent picker; picking one is refused by
        # handle_start's roster guard, which is how this surfaced
        # (live-reported 2026-08-28: parent 1319, an id this server has never
        # issued, rejected at single_mode_live/start).
        smfc["trained_chara"] = copy.deepcopy(
            roster if roster is not None
            else trained_chara._get_or_seed_roster(viewer_id))
        # The same leak one field over: the template's 1871 is an id INSIDE
        # those 237 -- "your new veteran is <a horse you do not own>". 0 is the
        # only honest value when no legacy was created, since whatever id goes
        # here has to exist in the roster we just sent.
        smfc["trained_chara_id"] = (new_record["trained_chara_id"]
                                    if new_record is not None else 0)
        if final_chara_info is not None:
            smfc["chara_info"] = copy.deepcopy(final_chara_info)
        # The same frozen-fixture leak as trained_chara above, one field over:
        # the template's love_point_info/update_user_chara_info are the CAPTURE
        # account's bond numbers for the CAPTURE account's trainee (3,200-odd
        # points on a character this player may not even own), replayed on
        # every finish. Serve this account's own -- the real grant when a
        # legacy was just created, else the genuinely unchanged totals.
        saved = state_store.get_state(viewer_id) or {}
        smfc["love_point_info"] = love_point_info or _unchanged_love_point_info(
            saved, final_chara_info)
        # update_user_chara_info: the trainee's own chara_list row as it now
        # stands ("your uma's registered totals" on the results screen). Real
        # captures always carry an object here; ours went null whenever
        # love_point_info named no chara (a give-up, or a run whose bond grant
        # was skipped), so fall back to the career's own trainee before giving
        # up on the field.
        love_chara = smfc["love_point_info"].get("chara_id") or 0
        if not love_chara and final_chara_info is not None:
            love_chara = (final_chara_info.get("card_id") or 0) // 100
        smfc["update_user_chara_info"] = _love_chara_entry(saved, love_chara)
        smfc["new_chara_profile_array"] = new_chara_profile_array
        # The Transfer ("trade in a veteran") panel. Real finish responses
        # carry it whenever a Transfer event is running and null when none is
        # -- both states occur in the corpus. Ours was unconditionally null
        # because nothing ever built it here, so the panel never appeared even
        # while transfer/index was serving the very same live event.
        smfc["transfer_event_info"] = transfer.transfer_event_info(viewer_id)
        # story_event_info and training_challenge_result stay as the template
        # left them. Both are limited-time campaign panels: story_event_info
        # carries the career's point contribution to a running Story Event,
        # whose add_point_info total is a bonus-weighted score whose formula
        # no capture pins down (rank_score alone does not reproduce it in any
        # of the eight samples), and training_challenge_result belongs to the
        # Training Challenge exam mode, which this server does not implement
        # at all. Real captures show both null far more often than not, so
        # null is the honest value rather than an invented one.
    return response


def _love_chara_entry(full_state: dict, chara_id: int):
    """This account's chara_list row for chara_id, or None when she has none
    (never trained -- e.g. a give-up on a brand-new character). READ-ONLY: it
    must not mint a chara_list entry for a career the player just deleted."""
    charas = full_state.get(collection.CHARA_LIST_KEY)
    if not chara_id or not isinstance(charas, list):
        return None
    entry = next((c for c in charas if c.get("chara_id") == chara_id), None)
    return dict(entry) if entry else None


def _unchanged_love_point_info(full_state: dict, final_chara_info) -> dict:
    """love_point_info for a finish that granted nothing (give-up, delete, no
    active career): the character's current totals with before == after, which
    is exactly what the four captured force-delete finishes report."""
    chara_id = (bond.chara_id_for_card((final_chara_info or {}).get("card_id") or 0)
                if final_chara_info else 0)
    entry = _love_chara_entry(full_state, chara_id) or {}
    love = entry.get("love_point") or 0
    pool = entry.get("love_point_pool") or 0
    return {"chara_id": chara_id, "love_point_before": love, "love_point_after": love,
            "love_point_pool_before": pool, "love_point_pool_after": pool}



# ---------------------------------------------------------------- analyze --

def handle_race_analyze(payload: dict) -> dict:
    """single_mode/race_analyze {program_id, current_turn} -- the pre-race
    "analyze the field" screen.

    SHARED, not scenario-specific: dump.cs carries the string literals
    single_mode/race_analyze AND single_mode_live/race_analyze, so URA and
    Grand Live reach it too (Unity Cup gets its own alias for free, the same
    way it does every other shared verb).

    dump.cs SingleModeTeamRaceAnalyzeResponse.CommonResponse has exactly ONE
    field, race_horse_data_array -- no chara_info, no envelope. So this serves
    the field for the race already resolved onto RACE_CTX_KEY rather than
    simulating a second, different one: the whole point of the screen is to
    show the player the horses they are about to run against, and rolling a
    fresh field here would show a race that never happens.

    An unresolved or mismatched program is answered with an EMPTY array rather
    than 205 -- the client treats the screen as "no data yet" and moves on,
    where a refusal leaves it on a spinner."""
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    ctx = full_state.get(RACE_CTX_KEY) or {}
    program_id = payload.get("program_id")
    horses = []
    if ctx and (program_id in (None, 0) or ctx.get("program_id") == program_id):
        horses = copy.deepcopy((ctx.get("race_start_info") or {}).get("race_horse_data") or [])
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"race_horse_data_array": horses}}
