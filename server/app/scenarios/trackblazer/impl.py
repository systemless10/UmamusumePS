"""TRACKBLAZER (scenario 4) -- "Start of the Climax". The mechanics.

The scenario's implementation, not its seam: shared career code NEVER imports
this module, it goes through the Trackblazer object in this package's
__init__.py. docs/URA_VS_TRACKBLAZER.md carries the full derivation and names
its sources; this docstring carries only what a reader of the code needs.

WIRE SHAPE (captures/bot/20260905_144604_icarus, a full 78-turn real run with
complete response bodies)
-------------------------------------------------------------------------
A Trackblazer response is the URA response we already build with exactly one
substitution: `ura_data_set` becomes `free_data_set`. Fifteen fields, all of
them observed populated:

    shop_id                       reset window 1-11 (single_mode_free_shop.id)
    sale_value                    discount state for the current lineup
    win_points / prev_win_points  GRADE POINTS now / before the last change
    coin_num / gained_coin_num    shop coin balance / this-response delta
    user_item_info_array          inventory [{item_id, num}]
    pick_up_item_info_array       the lineup, one entry per OFFER (see shop.py)
    item_effect_array             active buffs -- the master effect row verbatim
                                  with begin_turn/end_turn attached
    command_info_array            per-facility overlay, Unity Cup's shape
    rival_race_info_array         [{program_id, chara_id}] -- this turn's Rival Race
    twinkle_race_ranking          Climax standings position
    twinkle_race_npc_info_array   the Climax field, each NPC carrying its OWN win_points
    twinkle_race_npc_result_array per leg [{turn, program_id, race_result_array}]
    unchecked_event_achievement_id epithet unlocked on this response

THE TWO CURRENCIES, and neither exists in URA
---------------------------------------------
`single_mode_free_win_point` -- GRADE POINTS. Grade x placement: G1 100 at 1st,
G2 80, G3 60, OP 40, Pre-OP 20, Debut/maiden 10, times 100/60/40/20/20/10% by
placement. This is the objective counter, and it is CONSUMED: the turn-24, -48
and -72 objectives each reset it to zero (MANT: "ALL Grade Points are consumed
by each objective and do not carry over"), which is exactly why
prev_win_points exists as its own wire field.

`single_mode_free_coin_race` -- SHOP COINS. Grade-INDEPENDENT: 1st 100, 2nd-3rd
60, 4th-5th 30, 6th+ 0, for every grade. That grade-independence is what let
docs/TRACKBLAZER_SHOP.md reconstruct placement from coin deltas alone.

Climax legs pay win points and NO coins -- consistent with those rows living in
_win_point (race_group_id 40001/40002/40003) and not in _coin_race at all.

THE TWINKLE STAR CLIMAX
-----------------------
Three legs on turns 74/76/78, the same slots URA's Finals use, but the win
condition is cumulative Victory Points over a 16-runner field rather than
winning all three. The field is 15 NPCs + the trainee, and the NPCs carry their
own win_points, so the standings are real server-side state over a real field.

The 15 are single_mode_npc rows: twelve of the `<chara>900` rows plus the three
fixed `<chara>901` scenario girls (Happy Meek 2001, Bitter Glasse 2002, Little
Cocon 2003), which appear in every observed field and are the only 901 rows
master.mdb has. The VP ladder is master data too (single_mode_free_win_point,
race_group_id 40001-40003, grade 0): 1st 10, 2nd 8, 3rd 6, 4th 4, 5-6th 3,
7-9th 2, 10-13th 1, 14th+ 0 -- verified digit for digit against the capture's
own npc win_points.

Numbers tagged KNOB are reasoned defaults, not capture-derived ground truth.
"""

from __future__ import annotations

import logging
import random

from ... import epithets
from ... import master_data

log = logging.getLogger("uma-server")

SCENARIO_ID = 4
STATE_KEY = "trackblazer"

# The three Climax legs, and the master race_group each leg's VP table lives
# under. Same turns URA's finals run on -- see Scenario.has_ura_finals.
CLIMAX_TURNS = (74, 76, 78)
CLIMAX_RACE_GROUPS = {74: 40001, 76: 40002, 78: 40003}

# The turns whose objective is a Grade Point target, in objective order. The
# turn-12 objective is the Debut race instead (condition_type 8) and has no
# counter to reset.
OBJECTIVE_TURNS = (24, 48, 72)

# Aptitude codes: 1=G 2=F 3=E 4=D 5=C 6=B 7=A 8=S. Route selection wants B
# (single_mode_route_condition's condition_value_2 is 6 on every row); Rival
# Races want C (MANT).
APT_B, APT_C = 6, 5

STATS = ("speed", "stamina", "power", "guts", "wiz")

# speed, stamina, power, guts, wiz -- the capture's serve order, same as Grand
# Live's and Unity Cup's TRAINING_COMMAND_IDS.
TRAINING_COMMAND_IDS = (101, 105, 102, 103, 106)

# The per-response scratch full_state stashes an active item's bonus-only
# share of each facility's number into, between scale_training_preview
# (which computes it alongside the scaled total home_info actually shows) and
# command_info (which reads it back into the overlay). Never persisted: taken
# (popped) the same response it is written in, so it cannot go stale if a
# scenario method is ever skipped for one facility on some path.
BONUS_PREVIEW_KEY = "trackblazer_bonus_preview"


def stash_bonus_preview(full_state: dict, command_id, bonus: list) -> None:
    full_state.setdefault(BONUS_PREVIEW_KEY, {})[int(command_id)] = bonus


def take_bonus_preview(full_state: dict, command_id) -> list:
    return list((full_state.get(BONUS_PREVIEW_KEY) or {}).get(int(command_id)) or [])


def is_active(chara_info) -> bool:
    return int((chara_info or {}).get("scenario_id") or 0) == SCENARIO_ID


def state(full_state: dict) -> dict:
    """This scenario's slice of the career state, created on first touch."""
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = {}
        full_state[STATE_KEY] = st
    st.setdefault("coins", 0)
    st.setdefault("coins_total", 0)          # lifetime earned -- see add_coins
    st.setdefault("win_points", 0)
    st.setdefault("win_points_total", 0)     # lifetime earned -- see add_win_points
    st.setdefault("prev_win_points", 0)
    st.setdefault("items_bought", 0)         # lifetime purchases -- see shop.buy
    st.setdefault("gained_coin_num", 0)
    st.setdefault("items", {})
    st.setdefault("effects", [])
    st.setdefault("next_use_id", 1)
    st.setdefault("shop_id", 0)
    st.setdefault("offers", [])
    st.setdefault("next_shop_item_id", 1)
    st.setdefault("rival", {})
    st.setdefault("climax", {})
    st.setdefault("objectives", {})
    return st


def reset(full_state: dict) -> None:
    full_state.pop(STATE_KEY, None)


# ============================================================== the route ===
# Trackblazer's routes are chara_id = 0: seven SHARED rows selected by aptitude,
# not one route per trainee. That is master data's own form of MANT's "An Uma's
# Career Goals and Secret Events are disabled in Trackblazer".
#
# condition_type_1 1 = ground (value_1 1 turf / 2 dirt), 3 = distance (value_1
# 3 sprint / 4 mile / 5 middle / 6 long); condition_value_2 is the aptitude
# threshold, 6 (B) on every row. Highest matching priority wins -- the same
# ORDER BY priority DESC shape URA's per-chara lookup uses.
#
# Condition sets 10005 (dirt sprint) and 10008 (dirt long) exist with NO route
# row, so those umas fall through to the unconditioned route 10000 -- which
# points back at race_set 10006 and hands them the dirt requirements anyway.
# Nothing is missing; do not "fix" it by inventing routes for them.

_GROUND_APT = {1: "proper_ground_turf", 2: "proper_ground_dirt"}
_DISTANCE_APT = {3: "proper_distance_short", 4: "proper_distance_mile",
                 5: "proper_distance_middle", 6: "proper_distance_long"}


def _condition_met(cond, chara_info: dict) -> bool:
    kind = int(cond["condition_type_1"])
    field = (_GROUND_APT if kind == 1 else _DISTANCE_APT).get(
        int(cond["condition_value_1"]))
    if not field:
        return False
    return int(chara_info.get(field) or 0) >= int(cond["condition_value_2"] or APT_B)


def career_route(chara_info: dict):
    """(route_id, [route_race_id, ...]) for a Trackblazer career, or (None, [])
    if master data has no scenario-4 route at all."""
    rows = master_data.query(
        "SELECT id, race_set_id, condition_set_id FROM single_mode_route "
        "WHERE scenario_id=? AND chara_id=0 ORDER BY priority DESC", (SCENARIO_ID,))
    for row in rows:
        cset = int(row["condition_set_id"] or 0)
        if cset:
            conds = master_data.query(
                "SELECT condition_type_1, condition_value_1, condition_value_2 "
                "FROM single_mode_route_condition WHERE condition_set_id=?", (cset,))
            if not conds or not all(_condition_met(c, chara_info) for c in conds):
                continue
        races = master_data.query(
            "SELECT id, condition_type, condition_value_1 FROM single_mode_route_race "
            "WHERE race_set_id=? ORDER BY sort_id", (row["race_set_id"],))
        chara_id = int(chara_info.get("card_id") or 0) // 100
        out = []
        for r in races:
            if int(r["condition_type"] or 0) == _CT_PER_CHARA_RACE:
                sub = _per_chara_route_race(chara_id, int(r["condition_value_1"] or 0))
                if sub:
                    out.append(sub)
                    continue
                log.warning("trackblazer: no per-chara route race for chara %s "
                            "group %s -- keeping row %s unresolved",
                            chara_id, r["condition_value_1"], r["id"])
            out.append(int(r["id"]))
        return int(row["id"]), out
    return None, []


# condition_type 8: "this goal is the trainee's OWN race, named indirectly".
# condition_value_1 is a single_mode_change_chara_route.route_race_group_id, and
# that table maps (group, chara_id) -> a route_race id on the chara's URA route
# -- for group 40000, every one of its 66 rows is that trainee's sort_id 1,
# turn 12, condition_type 1 Junior Make Debut.
_CT_PER_CHARA_RACE = 8
_URA_SCENARIO_ID = 1


def _per_chara_route_race(chara_id: int, group_id: int) -> int:
    """The route_race id a condition_type 8 row stands in for, or 0.

    THIS SUBSTITUTION IS WHAT THE REAL SERVER SENDS. Capture 0003 of
    20260905_144604_icarus serves chara 1068 the array
    [605, 40022, 40023, 40024, 40025, 40026, 40027]: Trackblazer's own rows
    40022-40027 (sort_id 2-7) with row 40021 -- the condition_type 8 debut --
    REPLACED by 605, which is chara 1068's URA route_race for the Junior Make
    Debut, exactly what change_chara_route(40000, 1068) names.

    It matters for far more than the goal banner. Every downstream rule in
    single_mode_team keys off condition_type 1 rows: _forced_race_turns (which
    turns lock into a race), _build_race_condition_array (which races the Race
    Day screen offers), and the race command's own is_enable, whose unlock turn
    is min(forced_turns). Left unresolved, a Trackblazer career has NO
    condition_type 1 rows at all -- so the debut is skipped, racing never
    unlocks for the whole run, and the banner falls through to the turn-24
    Grade Point objective (all three live-reported)."""
    if not chara_id or not group_id:
        return 0
    row = master_data.query_one(
        "SELECT route_race_id FROM single_mode_change_chara_route "
        "WHERE route_race_group_id=? AND chara_id=?", (int(group_id), int(chara_id)))
    if row:
        return int(row["route_race_id"] or 0)
    # The table covers 66 trainees. For anyone else, take the same thing it
    # would have named: the first goal on that trainee's own URA route.
    row = master_data.query_one(
        "SELECT rr.id FROM single_mode_route r "
        "JOIN single_mode_route_race rr ON rr.race_set_id = r.race_set_id "
        "WHERE r.scenario_id=? AND r.chara_id=? AND rr.condition_type=1 "
        "ORDER BY rr.sort_id LIMIT 1", (_URA_SCENARIO_ID, int(chara_id)))
    return int(row["id"]) if row else 0


def objective_target(chara_info: dict, turn: int) -> int:
    """The Grade Point target this career's route sets for `turn`, or 0 if that
    turn carries no Grade Point objective."""
    _route_id, race_ids = career_route(chara_info)
    if not race_ids:
        return 0
    marks = ",".join("?" * len(race_ids))
    row = master_data.query_one(
        f"SELECT condition_value_1 FROM single_mode_route_race WHERE id IN ({marks}) "
        "AND turn=? AND condition_type=7", (*race_ids, int(turn)))
    return int(row["condition_value_1"]) if row else 0


# ========================================================= race pay-outs ====

def grade_of(program_id):
    row = master_data.query_one(
        "SELECT r.grade FROM single_mode_program p "
        "JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id WHERE p.id=?", (program_id,))
    return int(row["grade"]) if row else None


def win_points_for(grade, result_rank: int, race_group_id: int = 0) -> int:
    """single_mode_free_win_point, straight off master. race_group_id 0 is the
    ordinary career table (keyed by grade); 40001-40003 are the Climax legs'
    own VP ladders, which are grade-independent (grade = 0)."""
    row = master_data.query_one(
        "SELECT point_num FROM single_mode_free_win_point "
        "WHERE race_group_id=? AND grade=? AND order_min<=? AND order_max>=?",
        (int(race_group_id), int(grade or 0), int(result_rank), int(result_rank)))
    return int(row["point_num"]) if row else 0


def coins_for(grade, result_rank: int) -> int:
    """single_mode_free_coin_race. Identical for every grade -- see above."""
    row = master_data.query_one(
        "SELECT coin_num FROM single_mode_free_coin_race "
        "WHERE grade=? AND order_min<=? AND order_max>=?",
        (int(grade or 0), int(result_rank), int(result_rank)))
    return int(row["coin_num"]) if row else 0


def add_coins(st: dict, amount: int) -> None:
    amount = int(amount or 0)
    if amount <= 0:
        return
    st["coins"] = int(st.get("coins") or 0) + amount
    st["gained_coin_num"] = int(st.get("gained_coin_num") or 0) + amount
    # LIFETIME EARNED, which is a different number from both of the above:
    # `coins` is a balance the Pro Shop spends down and `gained_coin_num` is a
    # per-response delta. Epithet 184 asks what was EARNED across the run
    # ("having earned at least 1,000 Pro Shop coins"), so a player who earned
    # 1,200 and spent 900 still qualifies.
    st["coins_total"] = int(st.get("coins_total") or 0) + amount


def spend_coins(st: dict, amount: int) -> bool:
    amount = int(amount or 0)
    if amount > int(st.get("coins") or 0):
        return False
    st["coins"] = int(st.get("coins") or 0) - amount
    return True


def add_win_points(st: dict, amount: int) -> None:
    amount = int(amount or 0)
    st["prev_win_points"] = int(st.get("win_points") or 0)
    st["win_points"] = st["prev_win_points"] + amount
    # LIFETIME EARNED. The visible counter is CONSUMED -- each year-end
    # objective resets it to zero (see consume_win_points) -- so at graduation
    # it holds only what was banked since the turn-72 objective, and epithet
    # 180 ("at least 1,000 Result Pts") would be unreachable read off it.
    if amount > 0:
        st["win_points_total"] = int(st.get("win_points_total") or 0) + amount


def consume_win_points(st: dict) -> int:
    """Spend the whole counter on an objective and report what it was. The
    objectives do not subtract a cost -- ALL Grade Points are consumed."""
    had = int(st.get("win_points") or 0)
    st["prev_win_points"] = had
    st["win_points"] = 0
    return had


def objective_met(full_state: dict, chara_info: dict, turn: int) -> bool:
    """Whether the year-end objective on `turn` was cleared, recorded so the
    story beat and every later reader agree. Idempotent: the first call decides,
    which matters because it is also what CONSUMES the counter."""
    st = state(full_state)
    key = str(int(turn))
    done = st.get("objectives") or {}
    if key in done:
        return bool(done[key])
    target = objective_target(chara_info, turn)
    got = consume_win_points(st)
    met = got >= target if target else True
    done[key] = met
    st["objectives"] = done
    return met


# ====================================================== the Climax field ====
# KNOB. The capture's field carries stats ~1.83x their single_mode_npc row
# (1001900: master 260/274/253/218/235, served 476/499/467/400/425 -- the ratio
# is 1.81-1.85 across all five). One capture, one talent_level, so the factor is
# fitted rather than derived. It is cosmetic plus an input to our own placement
# roll below; it never reaches the race simulator.
_NPC_STAT_SCALE = 1.83

_FIXED_CLIMAX_NPCS = (2001901, 2002901, 2003901)
_CLIMAX_FIELD_SIZE = 15


def _skill_set(skill_set_id) -> list:
    """skill_set is WIDE -- skill_id1..skill_id20 across one row, not a row per
    skill. Zeroes pad the tail."""
    row = master_data.query_one("SELECT * FROM skill_set WHERE id=?", (skill_set_id,))
    if row is None:
        return []
    out = []
    for i in range(1, 21):
        sid = row[f"skill_id{i}"]
        if sid:
            out.append({"skill_id": int(sid), "level": int(row[f"skill_level{i}"] or 1)})
    return out


def _npc_wire(row) -> dict:
    scale = _NPC_STAT_SCALE
    return {
        "npc_id": int(row["id"]),
        "chara_id": int(row["chara_id"]),
        "dress_id": int(row["race_dress_id"] or 0),
        "talent_level": 1,
        "win_points": 0,
        "speed": int(row["speed"] * scale),
        "stamina": int(row["stamina"] * scale),
        "power": int(row["pow"] * scale),
        "guts": int(row["guts"] * scale),
        "wiz": int(row["wiz"] * scale),
        "proper_distance_short": row["proper_distance_short"],
        "proper_distance_mile": row["proper_distance_mile"],
        "proper_distance_middle": row["proper_distance_middle"],
        "proper_distance_long": row["proper_distance_long"],
        "proper_running_style_nige": row["proper_running_style_nige"],
        "proper_running_style_senko": row["proper_running_style_senko"],
        "proper_running_style_sashi": row["proper_running_style_sashi"],
        "proper_running_style_oikomi": row["proper_running_style_oikomi"],
        "proper_ground_turf": row["proper_ground_turf"],
        "proper_ground_dirt": row["proper_ground_dirt"],
        "skill_array": _skill_set(row["skill_set_id"]),
    }


def ensure_climax_field(full_state: dict, chara_info: dict) -> list:
    """Seed the 16-runner Climax field once, and hand back the NPC half.

    ONE UMA, ONE BODY: the trainee's own character is excluded from the draw --
    she is already in the race as herself. Undrawable characters are excluded
    too, because a chara with no card_data row renders as a blank white plane on
    the standings screen with no error anywhere (master_data.has_portrait_art).
    The three 901 girls are exempt from that test for the same reason Unity
    Cup's finals rivals are: they have no card at all and the client draws them
    from their own race_dress_id."""
    st = state(full_state)
    climax = st.get("climax") or {}
    if climax.get("npcs"):
        return climax["npcs"]
    player = int(chara_info.get("card_id") or 0) // 100
    pool = [int(r["id"]) for r in master_data.query(
        "SELECT id, chara_id FROM single_mode_npc WHERE id % 1000 = 900 ORDER BY id")
        if int(r["chara_id"]) != player and master_data.has_portrait_art(r["chara_id"])]
    rng = random.Random((player << 8) ^ 0x7B7B)
    want = _CLIMAX_FIELD_SIZE - len(_FIXED_CLIMAX_NPCS)
    draw = rng.sample(pool, min(want, len(pool)))
    npcs = []
    for npc_id in list(draw) + list(_FIXED_CLIMAX_NPCS):
        row = master_data.query_one(
            "SELECT * FROM single_mode_npc WHERE id=?", (npc_id,))
        if row is not None:
            npcs.append(_npc_wire(row))
    climax["npcs"] = npcs
    climax.setdefault("results", [])
    climax.setdefault("ranking", 0)
    st["climax"] = climax
    return npcs


def _npc_strength(npc: dict) -> float:
    return sum(int(npc.get(s) or 0) for s in STATS)


def record_climax_leg(full_state: dict, chara_info: dict, turn: int,
                      program_id, player_rank: int) -> None:
    """Run the NPC half of one Climax leg, pay everyone their Victory Points and
    re-rank the standings.

    The trainee's own finish comes from the real simulated race; the NPCs are
    slotted around it by a strength-weighted roll, which is what makes their
    result ranks a permutation of the REMAINING places rather than a second,
    contradictory running of a race the client has already shown."""
    st = state(full_state)
    npcs = ensure_climax_field(full_state, chara_info)
    climax = st["climax"]
    if any(int(r.get("turn") or 0) == int(turn) for r in climax.get("results") or []):
        return                                    # this leg is already banked
    rng = random.Random((int(turn) << 12) ^ int(chara_info.get("card_id") or 0))
    ordered = sorted(npcs, key=lambda n: -(_npc_strength(n) + rng.uniform(-400, 400)))
    places = [p for p in range(1, len(npcs) + 2) if p != int(player_rank)]
    group = CLIMAX_RACE_GROUPS.get(int(turn), 0)
    result_array = []
    for npc, place in zip(ordered, places):
        npc["win_points"] = int(npc.get("win_points") or 0) + win_points_for(
            0, place, race_group_id=group)
        result_array.append({"npc_id": npc["npc_id"], "result_rank": place})
    add_win_points(st, win_points_for(0, int(player_rank), race_group_id=group))
    result_array.sort(key=lambda r: r["npc_id"])
    climax.setdefault("results", []).append(
        {"turn": int(turn), "program_id": int(program_id or 0),
         # The player's OWN finish in this leg. Server-internal (the wire shape
         # is the npc result array alone), recorded because epithet 182 asks
         # whether all three Climax legs were WON -- which the standings rank
         # cannot answer: first in the cumulative table is reachable without
         # winning every leg, and the NPC array only lists the places the
         # player did not take.
         "player_rank": int(player_rank),
         "race_result_array": result_array})
    # THE STANDINGS, which is what the turn-78 objective reads: condition rank 1
    # on the final leg means first in the cumulative table, not first past the
    # post. Capture 20260905_144604 settles it -- win_points 10 -> 20 -> 24 (two
    # leg wins, then a 4th), twinkle_race_ranking 1, career completed.
    better = sum(1 for n in npcs
                 if int(n.get("win_points") or 0) > int(st.get("win_points") or 0))
    # ...but it is PUBLISHED one response late. The race pair (race_end and the
    # race_out that carries "After the Nth Climax Race") still shows the standing
    # from BEFORE this leg -- capture 20260905_144604 has ranking 0 on both of
    # leg 1's responses and 1 only on the check_event after them, while the
    # npc win_points and the result array on those same responses are already
    # post-race. That gap IS the standings animation: the client counts the
    # published rank up to the one it derives from the new points, so handing it
    # the settled rank straight away leaves nothing to animate and the whole
    # screen is skipped (live-reported "we don't see that"). See publish_ranking.
    climax["pending_ranking"] = better + 1
    # THE SERVED TURN must lag the same one response, or the softlock below.
    # See held_turn.
    climax["pending_ranking_turn"] = int(turn)


# The two responses that must keep showing the pre-leg standing (above).
_RANKING_HELD_ENDPOINTS = ("race_end", "race_out")


def promote_ranking(full_state: dict, endpoint: str = "") -> bool:
    """Publish a leg's new standing, once the race pair that animates it is
    past. True if state changed.

    Runs from Scenario.settle, NOT from the envelope builder. The builder is
    the read-only half of the response chokepoint -- its mutations are only
    persisted when something ELSE on that response reports a change -- so
    promoting there left the new rank unsaved on the very response that
    published it. The next response would promote again to the same value, so
    the wire looked right, but climax["ranking"] stayed 0 in storage: leg 2's
    race_end then animated from 0 instead of from leg 1's standing. settle()
    exists for exactly this (its docstring describes Unity Cup's identical
    rank-up hand-off) and its return value is one of the two things that make
    the chokepoint save."""
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" not in climax:
        return False
    if (endpoint or "").rsplit("/", 1)[-1] in _RANKING_HELD_ENDPOINTS:
        return False                                # animate from the old rank
    climax["ranking"] = int(climax.pop("pending_ranking") or 0)
    climax.pop("pending_ranking_turn", None)
    return True


def held_turn(full_state: dict) -> int:
    """The turn to SERVE while a Climax leg's standing is still animating, or
    0 to serve the run's own.

    handle_ura_race_out advances chara_info["turn"] and resets playing_state
    to 1 on the SAME response that queues the leg's "After the Nth Climax
    Race" cutscene (204102/104/106) -- every other race's post-race event
    tolerates that because nothing there reads the turn number, but the
    standings screen (see record_climax_leg/promote_ranking) does: served a
    turn that has already moved on and a playing_state that already says
    "back to normal" while the rank it is supposed to animate is still frozen,
    the client cannot reconcile the two and hangs on the ranking screen
    (live-reported 2026-09-09, "full on softlock ... right when it's supposed
    to play the ranking animation"). Freezing the served turn at the leg's own
    number for exactly the one response promote_ranking also holds fixes both
    at once: this and Scenario.settle share the same `climax["pending_*"]`
    lifetime, so the two release together."""
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" not in climax:
        return 0
    return int(climax.get("pending_ranking_turn") or 0)


def publish_ranking(st: dict, endpoint: str = "") -> int:
    """The twinkle_race_ranking THIS response carries. A pure read -- whether a
    pending standing has been published is promote_ranking's decision, already
    made by the time the envelope is built."""
    return int((st.get("climax") or {}).get("ranking") or 0)


def climax_ranking(full_state: dict) -> int:
    """The SETTLED standing -- what the objective and the veteran record read,
    which must not lag behind the way the published field deliberately does."""
    climax = (state(full_state).get("climax") or {})
    return int(climax.get("pending_ranking") or climax.get("ranking") or 0)


# ========================================================== Rival Races =====
# MANT: from Early August of the Junior year (turn 15) onward, a G1-G3 race can
# be flagged as a Rival Race when the trainee has BOTH the distance and the
# surface aptitude at C or better. Winning gives a skill hint chosen from the
# race's distance and the running style used -- explicitly NOT aptitude-based,
# which is how Unity Cup's Spirit Bursts work -- plus +5 to two random stats,
# and raises the odds of new shop items appearing (see shop.offer_chance).
RIVAL_FIRST_TURN = 15
# MEASURED, no longer a knob. Over every real capture that carries a
# free_data_set alongside a chara_info, 58 of the 72 distinct turns at or past
# turn 16 advertise a Rival Race -- 0.806 -- and the flagged program is one of
# THAT TURN'S OWN entries in race_condition_array in 58 of 58. Rival turns run
# consecutively (16, 17, 19, 20, 21, 22, ...), so this is a near-every-turn
# flag, not the occasional event the old 0.35 modelled.
#
# The old value was also applied to a single deterministic pick
# (_voluntary_race_for_turn) rather than to the turn's whole race list, so the
# effective rate was 0.35 x P(that one pick happens to be a graded race the
# trainee fits) -- far below 0.8. The corroboration is 203025 "Rival Bested!":
# real careers ack it ~19.6 times each, which needs a rival on most turns.
RIVAL_CHANCE = 0.80
RIVAL_WIN_STAT_BONUS = 5
RIVAL_WIN_STAT_COUNT = 2

_GRADED = (100, 200, 300)


def _program_course(program_id):
    return master_data.query_one(
        "SELECT cs.distance AS distance, cs.ground AS ground "
        "FROM single_mode_program p JOIN race_instance ri ON ri.id = p.race_instance_id "
        "JOIN race r ON r.id = ri.race_id "
        "JOIN race_course_set cs ON cs.id = r.course_set WHERE p.id=?", (program_id,))


def distance_field(metres) -> str:
    metres = int(metres or 0)
    if metres <= 1400:
        return "proper_distance_short"
    if metres <= 1800:
        return "proper_distance_mile"
    if metres <= 2400:
        return "proper_distance_middle"
    return "proper_distance_long"


def _drawable_chara_pool(player: int) -> list:
    return [int(r["chara_id"]) for r in master_data.query(
        "SELECT DISTINCT chara_id FROM single_mode_npc WHERE id % 1000 = 900 "
        "ORDER BY chara_id")
        if int(r["chara_id"]) != player and master_data.has_portrait_art(r["chara_id"])]


def tick(full_state: dict, chara_info: dict) -> bool:
    """Everything this scenario has to do ONCE per served response, and whether
    it changed persistent state.

    It runs from command_info(), whose `rolled` return is the existing "a fresh
    random roll was made, persist it" signal -- and the shop lineup and the
    Rival Race flag are exactly that. Doing it in attach() instead would re-roll
    the lineup on every single response and never save it, because the response
    chokepoint only persists when command_info or settle says to."""
    if not is_active(chara_info):
        return False
    from . import shop
    st = state(full_state)
    turn = int(chara_info.get("turn") or 1)
    changed = shop.refresh(full_state, turn)
    rival = st.get("rival") or {}
    if int(rival.get("turn") or 0) != turn:
        rival_for_turn(full_state, chara_info, turn,
                       _turn_race_program(chara_info, turn))
        changed = True
    # gained_coin_num is a DELTA that belongs to ONE response; build_free_data_set
    # zeroes it, and that zero has to survive or the "+N coins" reads twice.
    if int(st.get("gained_coin_num") or 0):
        changed = True
    return changed


def _turn_race_programs(chara_info: dict, turn) -> list:
    """Every program the trainee can enter this turn -- the same list the client
    renders on the race screen. Deferred import for the same cycle reason as
    _turn_race_program."""
    try:
        from ...handlers import single_mode_team as smt
        rows = smt._build_race_condition_array(chara_info, turn) or []
    except Exception:                                      # noqa: BLE001
        return []
    return [r.get("program_id") for r in rows if isinstance(r, dict) and r.get("program_id")]


def rival_for_turn(full_state: dict, chara_info: dict, turn, program_id=None) -> list:
    """[{program_id, chara_id}] for this turn's race, or [] -- decided ONCE per
    turn and remembered, so re-entering the turn cannot re-roll it.

    THE CANDIDATE IS ANY OF THIS TURN'S RACES, not one deterministic pick. Real
    flags one of the turn's own race_condition_array entries (58 of 58 captured
    rivals), so the roll is made over that whole list; `program_id` survives
    only as a fallback for a caller that has a race in hand and no schedule."""
    st = state(full_state)
    turn = int(turn or 0)
    if turn < RIVAL_FIRST_TURN:
        return []
    rival = st.get("rival") or {}
    if int(rival.get("turn") or 0) == turn:
        return list(rival.get("array") or [])
    programs = _turn_race_programs(chara_info, turn)
    if not programs and program_id:
        programs = [program_id]
    candidates = []
    for pid in programs:
        course = _program_course(pid)
        if grade_of(pid) not in _GRADED or course is None:
            continue
        ground_field = ("proper_ground_turf" if int(course["ground"]) == 1
                        else "proper_ground_dirt")
        if (int(chara_info.get(ground_field) or 0) >= APT_C
                and int(chara_info.get(distance_field(course["distance"])) or 0) >= APT_C):
            candidates.append(int(pid))
    array = []
    if candidates:
        # Seeded on the CAREER and the turn: stable across every response of
        # this turn (the flag is re-derived on each one), different between
        # careers -- see career_events.career_salt.
        from ... import career_events as _ce
        rng = random.Random("tbrival:%d:%d" % (_ce.career_salt(full_state), turn))
        if rng.random() < RIVAL_CHANCE:
            pool = _drawable_chara_pool(int(chara_info.get("card_id") or 0) // 100)
            if pool:
                array = [{"program_id": rng.choice(candidates),
                          "chara_id": rng.choice(pool)}]
    st["rival"] = {"turn": turn, "array": array}
    return list(array)


def is_rival_race(full_state: dict, program_id, turn) -> bool:
    rival = state(full_state).get("rival") or {}
    if int(rival.get("turn") or 0) != int(turn or 0):
        return False
    return any(int(e.get("program_id") or 0) == int(program_id or 0)
               for e in rival.get("array") or [])


def rival_win_reward(full_state: dict, chara_info: dict, program_id) -> dict:
    """+5 to two random stats and a skill hint drawn from the race's DISTANCE
    and the running style used. Returns {'stats': [...], 'skill_id': int|None}
    so the caller can apply it wherever it applies its other race rewards."""
    rng = random.Random((int(program_id or 0) << 4) ^ 0x5A5A)
    stats = rng.sample(list(STATS), RIVAL_WIN_STAT_COUNT)
    course = _program_course(program_id)
    skill_id = None
    if course is not None:
        # skill_data.tag_id 402 is the course+distance bucket; the running-style
        # half sits in 401. Both are "which condition does this skill trigger
        # on", which is what MANT describes the hint as being picked from.
        rows = master_data.query(
            "SELECT id FROM skill_data WHERE tag_id IN (401, 402) AND rarity=1 "
            "AND grade_value > 0 AND disable_singlemode=0 ORDER BY id")
        if rows:
            skill_id = int(rng.choice(rows)["id"])
    return {"stats": stats, "skill_id": skill_id}


# ============================================================== the wire ====

def _turn_race_program(chara_info: dict, turn):
    """The program the trainee can enter on this turn, for the Rival Race flag.

    Deferred import: handlers.single_mode_team imports the scenario registry, so
    a top-level import here would close the cycle. The pick is deterministic
    (seeded by turn + chara), which is what lets the flag be decided against it
    without pinning a second, disagreeing choice of race."""
    try:
        from ...handlers import single_mode_team as smt
        info = smt._voluntary_race_for_turn(chara_info, turn)
    except Exception:                                      # noqa: BLE001
        return None
    return (info or {}).get("program_id")


def build_free_data_set(full_state: dict, chara_info: dict,
                        command_info_array=None, endpoint: str = "") -> dict:
    from . import shop
    st = state(full_state)
    turn = int(chara_info.get("turn") or 1)
    shop.refresh(full_state, turn)
    rival_for_turn(full_state, chara_info, turn, _turn_race_program(chara_info, turn))
    gained = int(st.get("gained_coin_num") or 0)
    st["gained_coin_num"] = 0            # a DELTA: it belongs to one response
    climax = st.get("climax") or {}
    items = [{"item_id": int(k), "num": int(v)}
             for k, v in sorted((st.get("items") or {}).items(),
                                key=lambda kv: int(kv[0])) if int(v) > 0]
    return {
        "shop_id": int(st.get("shop_id") or 0),
        "sale_value": 0,
        "win_points": int(st.get("win_points") or 0),
        "prev_win_points": int(st.get("prev_win_points") or 0),
        "gained_coin_num": gained,
        "coin_num": int(st.get("coins") or 0),
        "twinkle_race_ranking": publish_ranking(st, endpoint),
        "user_item_info_array": items or None,
        "pick_up_item_info_array": list(st.get("offers") or []) or None,
        "twinkle_race_npc_info_array": list(climax.get("npcs") or []),
        "item_effect_array": shop.active_effects(st, turn) or None,
        "twinkle_race_npc_result_array": list(climax.get("results") or []),
        "command_info_array": _command_info(command_info_array),
        "rival_race_info_array": list((st.get("rival") or {}).get("array") or []),
        "unchecked_event_achievement_id": None,   # set by attach()
    }


def _stamp_achievement_id(data: dict) -> None:
    """Tell the client WHICH epithet the "Achievement!" cutscene on this
    response is about.

    The three 2038xx stories are shared templates: their text carries
    <achievement_name> and <achievement_info> tags, which the client fills from
    MasterString categories 247/249 indexed by this id
    (TextUtil.ReplaceAchievementTag). Without it the player literally reads
    "Mejiro McQueen has earned <achievement_name>." -- reported live.

    Read off the event the response is SHOWING rather than stashed at fire
    time, because the field is a single int while several epithets can complete
    on one race and chain. Each response carries the id of its own head event,
    which is exactly what "epithet unlocked on this response" means."""
    fds = data.get("free_data_set")
    if not isinstance(fds, dict):
        return
    head = (data.get("unchecked_event_array") or [None])[0]
    if not isinstance(head, dict):
        return
    ach = epithets.trackblazer_achievement_id(head.get("event_id"))
    if ach:
        fds["unchecked_event_achievement_id"] = ach


def _command_info(command_info_array) -> list:
    """The per-facility overlay -- Trackblazer.command_info's own bonus-only
    breakdown (see impl.take_bonus_preview), passed through as built. Every
    capture with no item active shows five entries with an EMPTY params array,
    which is what an idle facility still gets there; but the array itself is
    not optional even then: the client reads it to lay the training screen
    out, and even /start's own response already carries all five. Falls back
    to that all-empty shape if the caller could not build one at all (no
    scenario hook ran, or it ran before home_info existed)."""
    if command_info_array:
        return [{"command_type": 1, "command_id": c.get("command_id"),
                 "params_inc_dec_info_array":
                     list(c.get("params_inc_dec_info_array") or [])}
                for c in command_info_array if c.get("command_type") == 1]
    return [{"command_type": 1, "command_id": cid, "params_inc_dec_info_array": []}
            for cid in TRAINING_COMMAND_IDS]


def attach(response: dict, full_state: dict, chara_info: dict,
           command_info_array=None, endpoint: str = "") -> dict:
    """Swap URA's ura_data_set for Trackblazer's free_data_set."""
    if not is_active(chara_info):
        return response
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    data.pop("ura_data_set", None)
    data.pop("team_data_set", None)
    data.pop("live_data_set", None)
    data["free_data_set"] = build_free_data_set(full_state, chara_info,
                                                command_info_array, endpoint)
    _stamp_achievement_id(data)
    # held_turn freezes the SERVED turn number for this same response; the
    # served playing_state has to hold too, or the client is told "back to
    # normal training" on a turn it hasn't reached yet -- see held_turn.
    climax = state(full_state).get("climax") or {}
    if "pending_ranking" in climax:
        chara_info["playing_state"] = 5
        data["chara_info"] = chara_info
    return response
