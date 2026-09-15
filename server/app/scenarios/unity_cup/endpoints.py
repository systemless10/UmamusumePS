"""UNITY CUP (scenario 2) -- the four endpoints URA has no counterpart for.

    single_mode_team/opponent_list      pick who to race
    single_mode_team/team_edit          set the 5x3 lineup
    single_mode_team/team_race_analyze  scout the matchup
    single_mode_team/team_race_start / _end / _out   the 5-round race

Everything ELSE Unity Cup posts reuses the shared career handlers verbatim;
main.py aliases the whole single_mode/* family onto this scenario's prefix (see
the scenario's extra_endpoints()).

Shared helpers live in handlers/single_mode_team.py and are reached through
_smt() -- a deferred import, because that module imports the scenario registry
and a top-level import here would close the cycle.
"""

from __future__ import annotations

import copy
import logging
import random
from datetime import datetime, timezone

from ... import career_events as CE
from ... import master_data
from ...simulation import race_simulator
from . import impl as unity_cup

log = logging.getLogger("uma-server")

# The key live_theater.py and presents.py's reward_type 80 already write, so a
# song granted here shows up in the jukebox and collection like any other.
_MUSIC_LIST_KEY = "music_list_state"

# dump.cs SingleModeScenarioTeamRaceDefine.AoharuRaceResultState.
# Win = 1, Lose = 2, Draw = 3 -- NOT the order the field names suggest.
# Capture proof: the turn-48 race in run 1 of 20260905_183334_icarus has
# final_win_type 2 and team_race_history_array result_state 2 against set 606,
# and the ladder took that set's lose_down_rank (15), not its draw_rank (12).
_RESULT_WIN = 1
_RESULT_LOSE = 2     # the only result a retry is offered for
_RESULT_DRAW = 3


def _smt():
    """handlers.single_mode_team, imported on use. See the module docstring."""
    from ...handlers import single_mode_team
    return single_mode_team


def _refused(code: int = 205) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": code, "notifications": {}}, "data": {}}


def _career(payload: dict):
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return None, None, None
    return viewer_id, full_state, career


# What each team-management / team-race endpoint's `data` really carries,
# straight off the real captures (one full Unity Cup career: 40 responses per
# endpoint, all agreeing). team_data_set is NOT listed -- sync_chara_info
# attaches it afterwards on every response. The URA train_check_event seed
# these are built from carries the whole training envelope (home_info,
# race_condition_array, race_start_info, unchecked_event_array,
# not_up/not_down_parameter_info, event_effected_factor_array,
# race_running_style), and shipping all of it on a team-race screen was seven
# or eight fields per response the real server never sends.
#
# add_music on team_race_end is a KEY the client expects, whose value is null
# in every captured team_race_end (the Unity Cup song is granted by
# team_race_end_out instead -- see _grant_team_race_song), so it is served as
# a present-but-null field rather than left out.
# opponent_list's real `data` carries team_data_set and NOTHING else -- but it
# is listed here with chara_info, and single_mode_team._trim_opponent_envelope
# drops that again at the very end of the response pipeline. The detour is
# required: _attach_scenario_data_set identifies the career's scenario from
# data["chara_info"], so narrowing this to () stripped the one field that says
# "this is a Unity Cup run", the attach hook fell through to base.Scenario
# (whose attach is a no-op), and the response went out with no team_data_set at
# all. The client then NullRef'd in OnClickTeamRaceButton the instant the race
# button was pressed -- a hard softlock on the team-race screen, with the race
# unreachable for the rest of the run (user-reported 2026-09-08).
# /start solves the identical problem the identical way (_trim_start_envelope).
_ENDPOINT_FIELDS = {
    "opponent_list": ("chara_info",),
    "team_edit": ("chara_info",),
    "team_race_start": ("chara_info",),
    "team_race_end": ("chara_info", "add_music"),
    "team_race_out": ("chara_info", "unchecked_event_array"),
}


def _envelope(full_state: dict, career: dict, endpoint: str = "") -> dict:
    """A training-state response for a Unity Cup career: the player's live
    chara_info + home_info, no events. team_data_set is added afterwards by
    sync_chara_info, exactly as on every other response.

    `endpoint` (the bare endpoint name) narrows `data` to the fields that
    endpoint really serves -- see _ENDPOINT_FIELDS. Without it the full
    training envelope is kept, which is what the event-carrying endpoints
    want."""
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    chara_info = career["data"]["chara_info"]
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    keep = _ENDPOINT_FIELDS.get(endpoint)
    if keep is not None:
        response["data"] = {k: data.get(k) for k in keep}
    return response


# ============================================================ opponent_list ==

# Which team_race_set rank each of the five races draws its candidates from.
# Straight from the capture's own team_race_history_array: sets 305, 400, 607,
# 805, 902 -- i.e. ranks 3, 4, 6, 8 and finally 100 (Boss).
# The TOP ordinary tier of each of the five boards. Race 4 tops out at 7, not
# 8: rank 8 is the elite band (impl.ELITE_RANK -- the twelve Greek-god teams),
# and it is appended as a FOURTH offer rather than heading the ordinary three.
RACE_RANKS = (3, 4, 6, 7, unity_cup.BOSS_RANK)
OPPONENT_CHOICES = 3


def handle_opponent_list(payload: dict) -> dict:
    """single_mode_team/opponent_list {current_turn} -- the three teams on
    offer for this turn's Unity Cup race.

    Each candidate ships the ladder outcomes with it (win_up_rank /
    lose_down_rank / draw_rank). That is not a convenience: team_rank is a
    LADDER POSITION, never a computed score, so these three numbers ARE the
    rule -- the race just picks which one to apply."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    turn = int(payload.get("current_turn") or career["data"]["chara_info"].get("turn") or 0)

    if not _cached_offers(st, turn):
        st["opponents"] = {"turn": turn,
                           "version": unity_cup.OPPONENT_ROLL_VERSION,
                           "list": _roll_opponents(
                               st, turn,
                               _our_charas(st, career["data"]["chara_info"]))}
    response = _envelope(full_state, career, "opponent_list")
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _cached_offers(st: dict, turn: int) -> list:
    """The offers already rolled for this turn, if they are still usable.

    Usable means the same turn AND the current roll version -- a roll cached by
    an older build outlives the fix that corrected it otherwise, because the
    career carries it in its own saved state and every response re-serves it
    from there."""
    cached = st.get("opponents")
    if not isinstance(cached, dict) or cached.get("turn") != turn:
        return []
    live = unity_cup.offers(st)
    if not live:
        log.info("unity cup: discarding a stale opponent roll (version %s)",
                 (cached or {}).get("version"))
    return live


def _our_charas(st: dict, chara_info=None) -> set:
    """Our own team's characters, trainee included.

    The other team must not field one of them: the same uma on both sides of
    the same round is a lineup the real game never serves, and it makes the
    race panel ambiguous about which one a result belongs to."""
    ours = {m["chara_id"] for m in unity_cup.active_members(st)}
    trainee = unity_cup.trainee_chara(chara_info)
    if trainee:
        ours.add(trainee)
    return ours


def _race_index(st: dict, turn: int) -> int:
    """Which of the five team races this turn is (0-based). Falls back to the
    number already run, so an off-schedule call still gets a sane tier."""
    if turn in unity_cup.TEAM_RACE_TURNS:
        return unity_cup.TEAM_RACE_TURNS.index(turn)
    return min(len(st.get("races") or ()), len(RACE_RANKS) - 1)


def _roll_opponents(st: dict, turn: int, exclude=()) -> list:
    """The board of teams to choose between.

    A BOARD IS THREE DIFFERENT TIERS, NOT THREE TEAMS OF ONE TIER. Every
    captured board descends: at rank 30 the offers are tiers 3/2/1, at 22 they
    are 4/3/2, at 14 they are 6/5/4. Rolling three sets of the same tier gave
    three cards with the same class badge and win rewards that read backwards
    -- the "Rank 23" card promising a worse outcome than the "Rank 25" one,
    because the reward ran by offer position while the displayed rank was
    rolled independently (user-reported 2026-09-05, with a screenshot of three
    E-class teams). RACE_RANKS is the TOP tier for each race; the board is that
    tier and the two below it, strongest first, which is also the order the
    win/lose/draw rules below assume."""
    index = _race_index(st, turn)
    rank = RACE_RANKS[index]
    rng = random.Random(f"{st.get('seed', 0)}:opponents:{turn}")
    current = int(st.get("team_rank") or 30)

    def one_set(tier: int):
        ids = list(unity_cup.team_race_sets_of_rank(tier))
        rng.shuffle(ids)
        return ids[0] if ids else None

    set_ids = []
    for tier in range(rank, 0, -1):
        if len(set_ids) >= OPPONENT_CHOICES:
            break
        chosen = one_set(tier)
        if chosen is not None:
            set_ids.append((tier, chosen))
    if not set_ids:
        set_ids = [(3, one_set(3))]

    # THE FINAL RACE MOVES NOTHING. It offers a single Boss team (team_power
    # 100/101, team_rank 1) and the capture serves win_up_rank == lose_down_rank
    # == draw_rank == the player's current rank, in all three runs that reached
    # it. The ladder is already settled by then; what race 5 does is flip
    # team_rank_state to 1 and hand over the graduation bonus.
    if rank >= unity_cup.BOSS_RANK:
        # ...and it is a STRENGTHENED Zenith (rank 101, set 1000) for a team
        # that beat an elite team on race 4.
        rank = unity_cup.finals_rank(st)
        boss = one_set(rank)
        return [{
            "team_race_set_id": boss,
            "team_power": rank,
            "team_rank": 1,
            "win_up_rank": current,
            "lose_down_rank": current,
            "draw_rank": current,
            "team_data_array": _opponent_lineup(boss, st, rng, exclude),
        }]

    # Per-offer ladder outcomes, read straight off the capture's own
    # opponent_list responses (the server ships all three with each offer, so
    # these numbers ARE the rule -- the race only picks which to apply).
    # Offers run strongest-first. Two bands, splitting at rank 20:
    #
    #   my_rank  win            lose            draw
    #   30       22 25 28       30 30 30        28 29 30
    #   22       14 17 20       22 23 23        20 21 22
    #   15        9 11 13       16 17 18        13 14 15
    #   14        8 10 12       15 16 17        12 13 14
    #
    # The rules below reproduce every one of those twelve triples exactly.
    # KNOWN GAP: one capture reached rank 8 and was offered FOUR teams, whose
    # numbers these rules do not reproduce (they give 5/6/7/8 for the draw where
    # the server sent 7/7/7/8). A four-offer board only appears near the top of
    # the ladder and has been seen once; the three-offer board covers races 1-4
    # in every observed run.
    high = current >= 20
    win_deltas = (-8, -5, -2) if high else (-6, -4, -2)
    lose_deltas = (0, 1, 1) if high else (1, 2, 3)

    out = []
    offers = set_ids[:OPPONENT_CHOICES]
    for offer, (tier, set_id) in enumerate(offers):
        last = len(offers) - 1
        out.append({
            "team_race_set_id": set_id,
            "team_power": tier,
            "team_rank": _opponent_rank(tier, rng),
            "win_up_rank": max(1, current + win_deltas[min(offer, 2)]),
            "lose_down_rank": min(30, current + lose_deltas[min(offer, 2)]),
            # Strongest offer gains the most on a draw; the weakest gains
            # nothing. Exact in all four observed three-offer boards.
            "draw_rank": max(1, current - (last - offer)),
            "team_data_array": _opponent_lineup(set_id, st, rng, exclude),
        })

    # THE FOURTH CARD. An elite team is offered on top of the ordinary three
    # when the run has earned it (impl.elite_available): league rank 10 or
    # better, team rank A or better, and at least one Extreme Spirit Burst.
    # Beating it pays extra and unlocks the strengthened Zenith in the finals.
    if unity_cup.elite_available(st, index):
        elite_ids = list(unity_cup.elite_set_ids())
        rng.shuffle(elite_ids)
        if elite_ids:
            out.insert(0, {
                "team_race_set_id": elite_ids[0],
                "team_power": unity_cup.ELITE_RANK,
                "team_rank": _opponent_rank(unity_cup.ELITE_RANK, rng),
                # It is the strongest card on the board, so it gains the most
                # and costs nothing extra to lose to -- the same shape the
                # top ordinary offer has.
                "win_up_rank": max(1, current + win_deltas[0] - 2),
                "lose_down_rank": min(30, current + lose_deltas[0]),
                "draw_rank": max(1, current - len(out)),
                "team_data_array": _opponent_lineup(elite_ids[0], st, rng,
                                                    exclude),
            })
    return out


def _opponent_rank(rank: int, rng: random.Random) -> int:
    """The opposing team's own ladder position, as shown on the offer card.

    It tracks THE SET'S tier, not the player's rank: a rank-3 team is placed
    23rd-25th whether the player is 30th or 15th. The band per tier is the
    corpus's own spread (impl.OPPONENT_RANK_BAND); we used to derive this from
    the player's rank, which put a tier-3 team at 29th."""
    lo, hi = unity_cup.OPPONENT_RANK_BAND.get(int(rank), (30, 30))
    return rng.randint(lo, hi)


def _opponent_lineup(set_id: int, st: dict, rng: random.Random,
                     exclude=()) -> list:
    """The opposing team, as up to five distance groups of up to three.

    Every npc_id here is a real single_mode_npc row -- see the comment over
    impl.mob_npc_pool for the crash that fabricating them causes. Three id
    spaces, picked by the set's own rank: the Boss tiers field their fixed
    fifteen, rank 8 fields an all-named themed team, and everything below
    fields a handful of named trainees leading a mob field.

    The named runners take the LOW member_ids: the corpus puts 169 of 243 of
    them at member_id 1 and only 24 at member_id 3, i.e. each group is led by
    its strongest runner. The team is not a full 5x3 -- group sizes vary per
    set (a rank-3 team is ten runners, a rank-8 themed team only eight), so the
    shape is rolled per set and stays put for as long as the offer stands."""
    row = unity_cup.team_race_set(set_id) or {}
    rank = int(row.get("rank") or 1)
    total, n_named = unity_cup.OPPONENT_TEAM_SHAPE.get(
        rank, unity_cup.OPPONENT_TEAM_SHAPE[1])

    if rank >= unity_cup.BOSS_RANK:
        picks = list(unity_cup.boss_npc_ids(rank))[:total]
    elif rank >= 8:
        # A themed team: the super chara leads, in her dress variant, and the
        # rest of the team wears one too.
        pool = [i for i in unity_cup.named_npc_pool(101)
                if i // 1000 not in exclude]
        super_chara = int(row.get("super_team_chara_id") or 0)
        lead = super_chara * 1000 + 101
        picks = [lead] if lead in pool else []
        picks += rng.sample([i for i in pool if i not in picks],
                            min(total - len(picks), max(0, len(pool) - len(picks))))
    else:
        named = [i for i in unity_cup.named_npc_pool(100)
                 if i // 1000 not in exclude]
        mobs = unity_cup.mob_npc_pool()
        picks = rng.sample(sorted(named), min(n_named, len(named)))
        picks += rng.sample(sorted(mobs), min(total - len(picks), len(mobs)))
    if not picks:
        return []

    # THE BOSS TEAM'S SEATING IS THE CAPTURE'S, POSITION FOR POSITION, and it
    # is not cosmetic. Capture 0469 (set 902) seats the fifteen Zenith rows as
    #     group g: member 1 = picks[2g], member 2 = picks[2g+1],
    #              member 3 = picks[10 + g]
    # which puts the team's only two NAMED runners (npc rows *04 and *06,
    # charas 2002/2003) at group 3 and group 4 -- i.e. OUT of the first six
    # seats. The generic leaders-first fill below scatters them instead, and
    # ours landed 3006101 sixth.
    #
    # That crashes the S+ upgrade cutscene. SingleModeScenarioTeamRaceOpponent-
    # SelectViewController.LoadBossPlusCutt3DCharacter builds its models by
    # mapping each of BOSS_TEAM_MEMBER_COUNT = 6 boss members through
    # single_mode_npc -> MOB DATA; a named runner has no mob row, so it drops
    # out of the list and the timeline asks for a sixth model that is not
    # there:
    #     ArgumentOutOfRangeException
    #       at SingleModeRushInCutInHelper.GetPreInstantiatedUserModelController
    #       at SingleModeScenarioTeamFirstPlusCutInController.Show
    #       at ...OpponentSelectViewController+<PlayChangeBossPlus>.MoveNext
    # (the player's own Player.log, 2026-09-06: the blue-flame animation
    # stopped partway through).
    if rank >= unity_cup.BOSS_RANK and len(picks) == 15:
        # The seat keeps its POSITION and its running style; only the id it is
        # drawn by changes, and only for the two the client cannot draw. See
        # impl.boss_art_substitute -- the panel resolves the portrait from this
        # id itself, so anonymising the race body alone left it blank.
        out = [{"distance_type": g + 1, "member_id": m + 1,
                "base_npc_id": unity_cup.boss_art_substitute(npc),
                "npc_id": unity_cup.boss_art_substitute(npc),
                "running_style": unity_cup.npc_running_style(npc)}
               for g in range(5)
               for m, npc in enumerate((picks[2 * g], picks[2 * g + 1],
                                        picks[10 + g]))]
        out.sort(key=lambda t: (t["distance_type"], t["member_id"]))
        return out

    # Group sizes: five groups, one to three each, summing to the team's size.
    sizes = [1] * 5
    shape = random.Random("%s:shape" % set_id)
    for _ in range(max(0, min(len(picks), 15) - 5)):
        candidates = [g for g in range(5) if sizes[g] < 3]
        sizes[shape.choice(candidates)] += 1

    out, i = [], 0
    for member_id in (1, 2, 3):                 # leaders first, then the rest
        for group in range(5):
            if sizes[group] < member_id or i >= len(picks):
                continue
            npc_id = picks[i]
            i += 1
            # Identity only (a no-op for every id outside the boss fifteen);
            # the running style still comes from the row actually being fielded.
            out.append({"distance_type": group + 1, "member_id": member_id,
                        "base_npc_id": unity_cup.boss_art_substitute(npc_id),
                        "npc_id": unity_cup.boss_art_substitute(npc_id),
                        "running_style": unity_cup.npc_running_style(npc_id)})
    out.sort(key=lambda t: (t["distance_type"], t["member_id"]))
    return out


# ================================================================ team_edit ==

def handle_team_edit(payload: dict) -> dict:
    """single_mode_team/team_edit {team_data_array, current_turn}.

    The client submits the WHOLE lineup, so it is stored wholesale rather than
    merged -- a merge would strand a member the player just moved out of a
    distance group."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    submitted = payload.get("team_data_array")
    if isinstance(submitted, list) and submitted:
        # THE TRAINEE IS A LEGAL SEAT. She is not in active_members -- she is
        # the player's own uma -- and filtering her out here silently deleted
        # her from the lineup the moment the player pressed save, which is
        # exactly the hole that NullRefs the victory cut-in (see
        # impl.lineup_of).
        roster = {m["chara_id"] for m in unity_cup.active_members(st)}
        trainee = unity_cup.trainee_chara(career["data"]["chara_info"])
        if trainee:
            roster.add(trainee)
        st["lineup"] = [
            {"distance_type": int(s.get("distance_type") or 1),
             "member_id": int(s.get("member_id") or 1),
             "chara_id": int(s.get("chara_id") or 0),
             "running_style": int(s.get("running_style") or 2)}
            for s in submitted if int(s.get("chara_id") or 0) in roster]
        # The player has a deck of their own now, so it goes on the wire from
        # here on whatever the turn -- see impl.lineup_served.
        st["lineup_saved"] = True
    response = _envelope(full_state, career, "team_edit")
    smt.state_store.save_state(viewer_id, full_state)
    return response


# ======================================================== team_race_analyze ==

def handle_team_race_analyze(payload: dict) -> dict:
    """single_mode_team/team_race_analyze {race_set_id, current_turn}.

    Returns ONLY analyze_mark_array -- the capture's whole response is 214
    bytes, no chara_info, no envelope. mark_type is the scouting verdict per
    distance group (1 = clearly ahead ... 4 = clearly behind, the four marks
    the client draws)."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    st = unity_cup.state(full_state)
    set_id = int(payload.get("race_set_id") or 0)
    marks = []
    for distance_type in (1, 2, 3, 4, 5):
        ours = _group_strength(st, distance_type)
        theirs = _opponent_strength(st, set_id, distance_type)
        ratio = ours / theirs if theirs else 1.0
        if ratio >= 1.25:
            mark = 1
        elif ratio >= 1.0:
            mark = 2
        elif ratio >= 0.8:
            mark = 3
        else:
            mark = 4
        marks.append({"distance_type": distance_type, "mark_type": mark})
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"analyze_mark_array": marks}}


def _group_strength(st: dict, distance_type: int) -> float:
    members = {m["chara_id"]: m for m in unity_cup.active_members(st)}
    total = 0
    for slot in unity_cup.lineup_of(st):
        if slot.get("distance_type") != distance_type:
            continue
        member = members.get(slot.get("chara_id"))
        if member:
            total += sum(member.get(s, 0) or 0 for s in unity_cup.STATS)
    return float(total or 1)


def _opponent_strength(st: dict, set_id: int, distance_type: int) -> float:
    row = unity_cup.team_race_set(set_id) or {}
    correction = int(row.get("status_correction") or 0)
    # status_correction is a whole-team offset in the same units the roster's
    # summed stats are, spread over the three runners in each distance group.
    return max(1.0, 3 * 600 + correction / 5.0)


# ========================================================== the team race ==

# The five distance groups, and a representative race_instance for each, taken
# from the chosen team_race_set's own race_instance_id_1..5 columns.
_SET_RACE_COLUMNS = ("race_instance_id_1", "race_instance_id_2", "race_instance_id_3",
                     "race_instance_id_4", "race_instance_id_5")

FIELD_SIZE = 12
TEAM_RUNNERS = 3


def handle_team_race_start(payload: dict) -> dict:
    """single_mode_team/team_race_start {team_race_set_id, current_turn}.

    Runs all five rounds NOW and hands the client the whole block: five
    race_scenario animations, the gate assignments, and the aggregate result.
    team_race_end then just acknowledges it."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    chara_info = career["data"]["chara_info"]
    turn = int(payload.get("current_turn") or chara_info.get("turn") or 0)
    set_id = int(payload.get("team_race_set_id") or 0)
    if not set_id:
        rolled = unity_cup.offers(st)
        set_id = rolled[0]["team_race_set_id"] if rolled else 0
    race_set = unity_cup.team_race_set(set_id)
    if not race_set:
        log.warning("unity cup: unknown team_race_set_id %s", set_id)
        return _refused()

    # THE OPPOSING TEAM IS THE ONE THE PLAYER PICKED. Their runners are the
    # offer's own team_data_array, not a fresh roll -- the opponent-select
    # screen already showed the player exactly who they are, and the npc ids
    # have to survive into the race for the client to render them.
    offer = _offer_for(st, chara_info, set_id, turn)
    try:
        block = _run_team_race(st, chara_info, viewer_id, race_set, turn,
                               (offer or {}).get("team_data_array") or [])
    except Exception:
        log.exception("unity cup: team race simulation failed")
        return _refused()

    st["pending"] = block
    st["pending"]["set_id"] = set_id
    st["pending"]["turn"] = turn
    st["pending"]["version"] = unity_cup.PENDING_BLOCK_VERSION
    # Snapshot the ladder position BEFORE the result is applied, so
    # team_race_continue can roll a lost attempt back to it.
    st["rank_before_race"] = int(st.get("team_rank") or 30)
    chara_info["playing_state"] = unity_cup.PLAYING_STATE_TEAM_RACE_RUNNING
    response = _envelope(full_state, career, "team_race_start")
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _offer_for(st: dict, chara_info: dict, set_id: int, turn: int) -> dict:
    """The offer the player picked, and with it THE OPPOSING TEAM.

    Their runners are the offer's own team_data_array, not a fresh roll -- the
    opponent-select screen already showed the player exactly who they are, and
    the npc ids have to survive into the race for the client to render them.
    A retry faces that same team, so this is shared with
    handle_team_race_continue rather than duplicated there."""
    offer = next((o for o in unity_cup.offers(st)
                  if int(o.get("team_race_set_id") or 0) == set_id), None)
    if offer is not None:
        return offer
    # The board was rolled by an older build and thrown away, or the client
    # picked a set we never offered. Roll it now rather than racing nobody:
    # an empty team_data_array forfeits every round silently.
    st["opponents"] = {"turn": turn,
                       "version": unity_cup.OPPONENT_ROLL_VERSION,
                       "list": _roll_opponents(st, turn,
                                               _our_charas(st, chara_info))}
    offer = next((o for o in unity_cup.offers(st)
                  if int(o.get("team_race_set_id") or 0) == set_id), None)
    if offer is None:
        offer = {"team_race_set_id": set_id,
                 "team_data_array": _opponent_lineup(
                     set_id, st,
                     random.Random("%s:%s" % (st.get("seed"), turn)),
                     _our_charas(st, chara_info))}
    log.info("unity cup: re-rolled the opponent board for set %s", set_id)
    return offer


def _run_team_race(st: dict, chara_info: dict, viewer_id, race_set: dict,
                   turn: int, their_lineup: list) -> dict:
    smt = _smt()
    # The attempt number is IN the seed. Without it a retry re-rolls the exact
    # same five races and hands back the identical loss -- which is what
    # handle_continue's "genuinely fresh roll, not a replay of the same loss"
    # exists to avoid, and would make the retry button cost a budget for
    # nothing.
    attempt = int(st.get("continue_used") or 0)
    rng = random.Random(f"{st.get('seed', 0)}:teamrace:{turn}:{attempt}")
    members = {m["chara_id"]: m for m in unity_cup.active_members(st)}
    lineup = unity_cup.lineup_of(st, chara_info)
    trainee = unity_cup.trainee_chara(chara_info)
    correction = int(race_set.get("status_correction") or 0)
    opponent_team = unity_cup.opponent_team_name(int(race_set["id"]))
    # status_correction is the set's WHOLE-TEAM strength offset, so it spreads
    # over every stat of every runner the team fields -- not over one round's
    # three. Dividing by three instead put a rank-8 team at +1400 per stat,
    # around triple what the trainee could reach, and made the top of the
    # ladder unwinnable rather than hard. The Boss tiers correct by 0 and get
    # their wall from their own npc rows instead (speed 753 at rank 100, 985 at
    # rank 101), which is the same design read a different way.
    per_stat = correction / float(max(1, len(their_lineup)) * len(unity_cup.STATS))
    set_rank = int(race_set.get("rank") or 1)

    frame_order_info_array = []
    race_result_array = []
    wins = 0
    losses = 0

    for round_index, column in enumerate(_SET_RACE_COLUMNS, start=1):
        distance_type = round_index
        race_instance_id = int(race_set.get(column) or 0)
        if not race_instance_id:
            continue
        # THE LINEUP IS THE TEAM, TRAINEE INCLUDED. She holds a seat in
        # team_data_array like everyone else (see impl.lineup_of), so this
        # reads her group off the lineup rather than recomputing it -- the two
        # answers disagreeing is what put her on the track in a round the
        # client had not seated her in.
        seats = [s for s in lineup
                 if s.get("distance_type") == distance_type][:TEAM_RUNNERS]
        if not seats:
            continue
        styles = {s["chara_id"]: s.get("running_style", 2) for s in lineup}
        # PAIRED WITH THE SEAT, not just collected. Each runner has to carry
        # her own member_id onto the wire (see below), and the sort just
        # underneath reorders the runners but must not reorder their seats.
        paired = [(s, chara_info if s["chara_id"] == trainee
                   else members.get(s["chara_id"])) for s in seats]
        paired = [(s, m) for s, m in paired if m is not None]
        if not paired:
            continue
        # The trainee, when she is running, leads the sim: race_simulator takes
        # one "player" horse and the rest as extra_player_charas.
        if any(s["chara_id"] == trainee for s in seats):
            paired.sort(key=lambda sm: sm[1] is not chara_info)
        our_member_ids = [int(s.get("member_id") or 1) for s, _ in paired]
        ours = [m for _, m in paired]
        if ours[0] is chara_info:
            player = smt._career_player_race_chara(chara_info, viewer_id)
        else:
            player = _team_horse(
                ours[0], styles, viewer_id, chara_info,
                _entrant_motivation(st.get("seed", 0), turn,
                                    ours[0].get("chara_id")))
        extra = [_team_horse(
                     m, styles, viewer_id, chara_info,
                     _entrant_motivation(st.get("seed", 0), turn,
                                         m.get("chara_id")))
                 for m in ours[1:]]
        their_seats = [t for t in their_lineup
                       if int(t.get("distance_type") or 0) == distance_type
                       ][:TEAM_RUNNERS]
        theirs = [_opponent_horse(t, per_stat, opponent_team, set_rank,
                                  st.get("seed", 0), turn)
                  for t in their_seats]
        # NOBODY ALREADY ON THE TRACK GETS A SECOND BODY: the neutral filler
        # draws its faces from the whole uma pool, so without this a teammate
        # (or a named runner on the other team) turns up twice in one race.
        # The trainee herself is excluded by _mob_opponents.
        on_track = {int(s.get("chara_id") or 0) for s, _ in paired}
        # The other team's seats carry an NPC ROW ID, not a chara -- the chara
        # is on the row (see _opponent_horse). Reading .chara_id off the seat
        # got 0 every time, which quietly made this half of the exclusion a
        # no-op and left the neutral filler free to field a second copy of one
        # of their named runners.
        on_track |= {int((unity_cup.npc_row(int(t.get("npc_id") or 0)) or {})
                         .get("chara_id") or 0)
                     for t in their_seats}
        fillers = smt._mob_opponents(
            chara_info, max(0, FIELD_SIZE - 1 - len(extra) - len(theirs)),
            exclude_charas=on_track - {0, 1})
        opponents = theirs + fillers
        if not opponents:
            continue

        # ... and the invariant itself, over the assembled field: see
        # smt._dedupe_field_faces.
        smt._dedupe_field_faces([player] + extra + opponents)
        # THE SEED THE ATTEMPT COUNTER FEEDS. `rng` above was built from
        # seed:turn:attempt and then never used -- the sim was left to its own
        # unseeded default, so the attempt counter bought nothing and a
        # re-entered race (the client calls team_race_start again after every
        # continue) rolled a THIRD, different result from the one the retry
        # had just produced. Seeding here makes the block reproducible for one
        # attempt and genuinely different for the next, which is what the
        # continue was always documented to do.
        result = race_simulator.simulate_race(
            player, opponents, race_instance_id,
            ground_condition=1, weather=1, season=1,
            seed=rng.randrange(2 ** 31),
            extra_player_charas=extra or None)
        if result is None:
            continue

        horses = result["horses"]
        sim_results = result["sim_results"]
        gates = result.get("gate_assignment") or list(range(len(horses)))
        # horses[0] is ours, then extra_player_charas, then the opponents in the
        # order they were passed -- so team_id falls straight out of the index.
        our_count = 1 + len(extra)
        team_ids = [1] * our_count + [2] * len(theirs)
        team_ids += [0] * (len(horses) - len(team_ids))
        # OUR side has no npc id (chara_id identifies us instead); the other
        # team and the neutral field are both identified by their
        # single_mode_npc row id.
        npc_ids = [0] * our_count + [int(h.get("npc_id") or 0) for h in theirs]
        npc_ids += [int(h.get("single_mode_chara_id") or 0) if h.get("mob_id")
                    else 0 for h in fillers]
        npc_ids += [0] * (len(horses) - len(npc_ids))
        # MEMBER IDS RESTART AT 1 FOR EACH TEAM, and they are the SEAT's id,
        # not a running index over the field. The capture is explicit: round 5
        # lists ours as (1068,1) (1030,2) (1056,3) and theirs as (1024100,1)
        # (141,2) (142,3). Numbering straight through the horse array gave the
        # other team 4/5/6, and the race list draws each runner into the slot
        # its member_id names -- so its whole team landed in slots that do not
        # exist, leaving blank cards and an ACE badge floating over nothing
        # (user-reported 2026-09-05, with a screenshot).
        member_ids = list(our_member_ids)
        member_ids += [int(t.get("member_id") or 1) for t in their_seats]
        member_ids += [0] * (len(horses) - len(member_ids))

        popularity = _rank_map(horses, lambda h: -h.get("speed", 0))
        stamina_rank = _rank_map(horses, lambda h: -h.get("stamina", 0))
        power_rank = _rank_map(horses, lambda h: -h.get("pow", h.get("power", 0)))

        race_horse_data = []
        chara_result_array = []
        for i, horse in enumerate(horses):
            entry = smt.practice_race._build_race_horse_entry(
                horse, frame_order=gates[i] + 1,
                final_grade=horse.get("rank", 1),
                popularity=popularity[i],
                popularity_mark_rank_array=[popularity[i], stamina_rank[i],
                                            power_rank[i]])
            entry["team_id"] = team_ids[i]
            entry["team_member_id"] = member_ids[i] if team_ids[i] else 0
            entry["team_rank"] = int(st.get("team_rank") or 30) if team_ids[i] == 1 else 0
            race_horse_data.append(entry)
            chara_result_array.append({
                "frame_order": gates[i] + 1,
                "chara_id": entry.get("chara_id", 0),
                "npc_id": npc_ids[i],
                "team_id": team_ids[i],
                "finish_order": sim_results[i]["finishOrder"] + 1,
                "finish_time": int(sim_results[i].get("finishTimeRaw", 0) * 10000),
                "popularity": popularity[i],
            })

        # ROUND WINNER = THE TEAM OF THE HORSE THAT ACTUALLY WON THE RACE.
        # Not "whose best finisher placed higher": if a MOB takes the round,
        # neither team won it and it is a DRAW, however the two teams placed
        # behind her. The corpus settles it across 100 rounds -- team 1 first
        # -> win_type 1 (69), team 2 first -> 2 (27), a mob first -> 3 (4) --
        # and all four mob wins are rounds the best-finisher rule would have
        # awarded to somebody (e.g. ours 5th, theirs 4th, still a draw).
        first = min(chara_result_array, key=lambda c: c["finish_order"],
                    default=None)
        win_type = {1: 1, 2: 2}.get((first or {}).get("team_id"), 3)
        if win_type == 1:
            wins += 1
        elif win_type == 2:
            losses += 1

        frame_order_info_array.append({
            "distance_type": distance_type,
            "race_order": round_index,
            "random_info_array": [
                {"team_race_set_id": 0 if team_ids[i] == 1 else int(race_set["id"]),
                 "member_id": entry.get("team_member_id", 0),
                 "chara_id": entry.get("chara_id", 0) if team_ids[i] == 1 else 0,
                 "base_npc_id": npc_ids[i],
                 "npc_id": npc_ids[i],
                 "running_style": entry.get("running_style", 2),
                 "frame_order": entry.get("frame_order", i + 1),
                 "motivation": entry.get("motivation", 3),
                 # SingleModeTeamRandomInfo carries the five stats too, and the
                 # capture fills them in for the other team's runners (ours are
                 # looked up from the roster instead).
                 **({"speed": entry.get("speed", 0),
                     "stamina": entry.get("stamina", 0),
                     "pow": entry.get("pow", entry.get("power", 0)),
                     "guts": entry.get("guts", 0),
                     "wiz": entry.get("wiz", 0)} if team_ids[i] == 2 else {})}
                for i, entry in enumerate(race_horse_data) if team_ids[i]],
        })
        race_result_array.append({
            "distance_type": distance_type,
            "race_instance_id": race_instance_id,
            "season": 1, "weather": 1, "ground_condition": 1,
            "random_seed": result["seed"],
            "race_horse_data_array": race_horse_data,
            "race_scenario": result["race_scenario"],
            "round": round_index,
            "win_type": win_type,
            "chara_result_array": chara_result_array,
            "continue_num": 0,
        })

    if wins > losses:
        final = 1
    elif wins < losses:
        final = 2
    else:
        final = 3
    return {"frame_order_info_array": frame_order_info_array,
            "race_result_array": race_result_array,
            "final_win_type": final}


# distance_type -> the trainee's own aptitude column, so she is entered in the
# group she is actually built for rather than a fixed one.
_TRAINEE_APTITUDE = {1: "proper_distance_short", 2: "proper_distance_mile",
                     3: "proper_distance_middle", 4: "proper_distance_long",
                     5: "proper_distance_middle"}


def _trainee_distance_group(chara_info: dict) -> int:
    return max(_TRAINEE_APTITUDE,
               key=lambda d: chara_info.get(_TRAINEE_APTITUDE[d], 1))


def _rank_map(horses: list, key) -> dict:
    order = sorted(range(len(horses)), key=lambda i: key(horses[i]))
    return {idx: rank + 1 for rank, idx in enumerate(order)}


# TEAM-RACE MOTIVATION. Every runner used to go out at a flat 3, which is a
# value the real wire spreads across all five.
#
# RE-MEASURED over the FULL set of real team races in captures/bot/*_icarus --
# 20 races, 100 rounds, every one carrying a complete race_horse_data_array.
# The first fit used 82 and 65 runners because it only looked at the rounds one
# session happened to expose; the real sample is 277 and 237:
#
#   side          n     1    2    3    4    5
#   OUR team    277    32   35   31   95   84
#   THEIR team  237    25   24   58   66   64
#
# The old fit called OUR side "effectively uniform" and rolled it flat 1-5.
# It is not uniform: 4 and 5 together are 65% of real's runners, against the
# 40% a flat roll produces, so every one of our teams went to the line duller
# than real's. THEIR side keeps its 3-5 bias but is much less extreme than the
# n=65 sample suggested (1 and 2 are ~10% each, not 6% and 3%).
#
# Still NOT a master rule -- that was tried first and is disproved. Their
# runners are single_mode_npc rows, and _mob_opponents already rolls the
# neutral field from each row's own motivation_min/motivation_max, so the same
# rule was the obvious candidate here. It cannot be right: every Unity Cup
# opponent row sampled carries the range (1,4), which can never produce a 5,
# yet 64 of real's 237 opposing runners go out at 5. Rolling from the rows
# reproduced a flat 1-4 instead of the observed bias. So the weights below are
# measured off the wire, and are the honest reading until a rule is found that
# derives them. (The neutral field is a separate case and IS flat: real's 686
# mob runners come in at 141/133/128/148/136, which is what _mob_opponents
# already produces from the rows.)
#
# Seeded on (career seed, turn, chara) so it is stable for the whole race --
# all five rounds field the same runners and must not reshuffle between them --
# and different for the next race. A re-entered race_start reproduces it.
# Both halves of that are corpus-confirmed: 356 of 356 real runners hold ONE
# motivation across all five rounds of their race, and only 3 of 42 teammates
# hold the same value across a whole session, i.e. it is rolled per race.
_OUR_MOTIVATION_WEIGHTS = ((1, 32), (2, 35), (3, 31), (4, 95), (5, 84))
_OPPONENT_MOTIVATION_WEIGHTS = ((1, 25), (2, 24), (3, 58), (4, 66), (5, 64))


def _entrant_motivation(seed, turn, ident, opposing: bool = False) -> int:
    rng = random.Random("%s:motivation:%s:%s" % (seed, turn, ident))
    weights = (_OPPONENT_MOTIVATION_WEIGHTS if opposing
               else _OUR_MOTIVATION_WEIGHTS)
    total = sum(w for _v, w in weights)
    roll = rng.uniform(0, total)
    upto = 0.0
    for value, weight in weights:
        upto += weight
        if roll <= upto:
            return value
    return weights[-1][0]


def _team_horse(member: dict, styles: dict, viewer_id, chara_info: dict,
                motivation: int = 3) -> dict:
    """One of OUR teammates as a race entry.

    Field values are the capture's, verbatim (team race 1, round 2, the three
    team_id 1 entries):

        card_id       the teammate's SUPPORT CARD id -- Rice Shower raced as
                      10015, Matikanefukukitaru as 10044, which are exactly
                      their single_mode_scout_chara rows' support_card_id.
                      Independent confirmation that a teammate IS a scout row.
        race_dress_id 101 = PLAYER_TEAM_MEMBER_DEFAULT_DRESS_ID (dump.cs).
                      A synthetic card_id instead makes the entry unresolvable
                      and practice_race collapses it to a mob -- the teammate
                      then races as chara_id 1 in a placeholder costume.
        mob_id 0, rarity 3, talent_level 1, npc_type 11, and
        single_mode_chara_id = chara_id.
    """
    row = unity_cup.scout_pool().get(member.get("scout_id"), {})
    horse = {
        "chara_id": member["chara_id"],
        "card_id": int(row.get("support_card_id") or 0),
        "mob_id": 0,
        "race_dress_id": unity_cup.PLAYER_TEAM_MEMBER_DEFAULT_DRESS_ID,
        "single_mode_chara_id": member["chara_id"],
        "npc_type": 11,
        "speed": member["speed"], "stamina": member["stamina"],
        "pow": member["power"], "power": member["power"],
        "guts": member["guts"], "wiz": member["wiz"],
        "running_style": styles.get(member["chara_id"], 2),
        "skill_array": [{"skill_id": s, "level": 1}
                        for s in unity_cup.member_race_skills(member)],
        "viewer_id": viewer_id,
        "trainer_name": chara_info.get("trainer_name") or "Trainer",
        "motivation": motivation, "rarity": 3, "talent_level": 1,
    }
    for key in ("proper_distance_short", "proper_distance_mile",
                "proper_distance_middle", "proper_distance_long",
                "proper_running_style_nige", "proper_running_style_senko",
                "proper_running_style_sashi", "proper_running_style_oikomi",
                "proper_ground_turf", "proper_ground_dirt"):
        horse[key] = row.get(key, 4)
    return horse


def _opponent_horse(entry: dict, per_stat: float, team_name: str,
                    set_rank: int = 1, seed=0, turn=0) -> dict:
    """One member of the OPPOSING team, from the offer's own npc row.

    The wire shape is the capture's, verbatim. A MOB runs as chara_id 1 with
    the row's mob_id and race_dress_id 1; a NAMED runner keeps her own chara_id
    with mob_id 0, card_id null and the row's race_dress_id (101/102). Both
    carry single_mode_chara_id == trained_chara_id == THE NPC ROW ID, which is
    what the client looks the portrait up by -- inventing one (we shipped
    chara_id * 100) NullRefs the opponent panel.

    Unlike the neutral field, the other team's runners carry an opposing
    viewer_id and the team name, which is how the client tints them.

    per_stat is the set's status_correction already divided out over the whole
    opposing team (see _run_team_race). The capture ships RAW npc stats on the
    wire, so applying it at all is a deliberate difficulty choice, not a
    reading of the capture."""
    npc_id = int(entry.get("npc_id") or 0)
    row = unity_cup.npc_row(npc_id) or {}
    mob_id = int(row.get("mob_id") or 0)
    # A SUBSTITUTED BOSS SEAT KEEPS THE ORIGINAL'S STRENGTH. The two undrawable
    # boss rows are their team's best runners, so fielding the mob that stands
    # in for them at the mob's own ~255 speed would quietly gut the final; the
    # substitution is meant to change the face and nothing else. Identity
    # (chara/mob/dress) still comes from the row actually being fielded.
    stat_row = unity_cup.npc_row(
        unity_cup.boss_stat_source(npc_id, set_rank)) or row
    # A NAMED RUNNER THIS BUILD CANNOT DRAW RUNS AS A MOB. Only the Boss team's
    # two story girls reach this now (Bitter Glasse 2002 and Little Cocon 2003
    # -- the ordinary teams are drawn from a pool that is already filtered, see
    # impl.named_npc_pool): master has no card art for them here, so the client
    # draws a blank white plane where the portrait goes. Borrowing a real mob
    # keeps every race-relevant field -- her npc row still supplies the stats,
    # aptitudes and running style below -- and only anonymises the face.
    if not mob_id and not master_data.has_portrait_art(row.get("chara_id")):
        pool = _smt()._mob_id_pool()
        mob_id = pool[npc_id % len(pool)] if pool else 8000
    horse = {
        "viewer_id": unity_cup.OPPONENT_VIEWER_ID,
        "trainer_name": team_name,
        "owner_viewer_id": 0, "owner_trainer_name": "",
        "single_mode_chara_id": npc_id,
        "trained_chara_id": npc_id,
        "nickname_id": 0,
        "chara_id": 1 if mob_id else int(row.get("chara_id") or 1),
        "card_id": 0,
        "mob_id": mob_id,
        # A mob wears the mob dress; her own is part of the art we don't have.
        "race_dress_id": (int(row.get("race_dress_id") or 1)
                          if mob_id == int(row.get("mob_id") or 0) else 1),
        # NpcType 20 = the OTHER TEAM (ours are 11, the neutral field 0).
        # Capture-confirmed on all 237 team-2 rows across both runs.
        "npc_type": 20,
        "npc_id": npc_id,
        "running_style": int(entry.get("running_style")
                             or unity_cup.npc_running_style(npc_id)),
        # THE OTHER TEAM RUNS WITH ITS SKILLS. This was [] -- the exact bug
        # single_mode_team._npc_skill_array was written to fix for career-race
        # mobs in 2026-08-28, still unfixed on this side. race_simulator feeds
        # skill_array straight into the physics engine, so an empty one does
        # not just blank the race UI: it removes every green boost,
        # acceleration, recovery and debuff the runner owns from the
        # simulation. It also skewed the RESULT, because the neutral mobs kept
        # theirs -- measured over a full career, mobs went into each round with
        # 2.88 skills against 0.00 for both named teams, making them the only
        # skilled runners on the track, and a round is a DRAW when a mob
        # finishes first. Real is the other way round: 2.12 for mobs against
        # 4-5 for the two teams.
        #
        # Read off stat_row, not row, for the same reason the stats are: a
        # substituted boss seat keeps the original runner's strength.
        "skill_array": _smt()._npc_skills(stat_row),
        "motivation": _entrant_motivation(seed, turn, npc_id, opposing=True),
        "rarity": 1 if mob_id else 3, "talent_level": 1,
    }
    for stat, column in (("speed", "speed"), ("stamina", "stamina"),
                         ("pow", "pow"), ("guts", "guts"), ("wiz", "wiz")):
        horse[stat] = max(80, int((stat_row.get(column) or 120) + per_stat))
    horse["power"] = horse["pow"]
    for key in ("proper_distance_short", "proper_distance_mile",
                "proper_distance_middle", "proper_distance_long",
                "proper_running_style_nige", "proper_running_style_senko",
                "proper_running_style_sashi", "proper_running_style_oikomi",
                "proper_ground_turf", "proper_ground_dirt"):
        horse[key] = stat_row.get(key, 4)
    return horse


def handle_team_race_end(payload: dict) -> dict:
    """single_mode_team/team_race_end {current_turn} -- bank the result.

    The race payload is DROPPED here (the capture's team_race_end carries no
    race_result_array) and the history row appended instead. The ladder move
    comes straight from the opponent offer's own win_up/lose_down/draw_rank."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    _bank_pending_result(st)
    # 9: the result sequence. The rank, the teammates' stats and the reward
    # panel all stay where they were until the sequence is left.
    career["data"]["chara_info"]["playing_state"] = (
        unity_cup.PLAYING_STATE_TEAM_RACE_RESULT)
    response = _envelope(full_state, career, "team_race_end")
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _bank_pending_result(st: dict) -> bool:
    """Turn the block the client just watched into a history row. Idempotent:
    with nothing pending it does nothing, so it is safe on every path out of
    the race.

    CALLED FROM BOTH WAYS OUT, which is the whole point. It used to live only
    in team_race_end, and a client that leaves the result sequence through
    team_race_end_out instead never banked its round at all -- the request log
    for the 2026-09-06 finals is `team_race_start -> continue -> start ->
    continue -> start -> team_race_end_out`, with no team_race_end anywhere.
    The round stayed unrun, so the turn-72 hold never lifted and the career
    dropped straight back into re-racing the Arima Kinen, which is exactly
    what the player reported the moment they lost the Unity Cup."""
    pending = st.get("pending") or {}
    if pending:
        final = int(pending.get("final_win_type") or 3)
        set_id = int(pending.get("set_id") or 0)
        race_set = unity_cup.team_race_set(set_id) or {}
        offer = next((o for o in unity_cup.offers(st)
                      if o.get("team_race_set_id") == set_id), None)
        if offer:
            # opponent_list ships all three outcomes with each offer, so the
            # race only picks which one to apply -- team_rank is a ladder
            # POSITION, never a computed score. The mapping below was
            # previously 2->lose / 3->draw by name but 2 was commented as the
            # draw; a real draw would have taken the wrong field.
            #
            # BANKED, NOT APPLIED. The ladder moves on the request after
            # team_race_out, together with everything else the rank-up screen
            # shows -- see impl.bank_race_award.
            unity_cup.bank_race_award(
                st,
                new_rank=int(offer["win_up_rank"] if final == _RESULT_WIN else
                             offer["lose_down_rank"] if final == _RESULT_LOSE else
                             offer["draw_rank"]),
                set_rank=int(race_set.get("rank") or 0),
                won=final == _RESULT_WIN,
                final=len(st.get("races") or ()) + 1 >= len(unity_cup.TEAM_RACE_TURNS))
        # Beating an elite team is what earns the strengthened Zenith in the
        # finals, so it is remembered before the row is banked.
        unity_cup.note_elite_result(st, set_id, final == _RESULT_WIN)
        # THE SCHEDULED TURN, NOT THE TURN THE PLAYER RACED ON. Every history
        # row in every capture sits on one of the five scheduled turns
        # (24/36/48/60/72) exactly -- 62 distinct rows, no exceptions. Ours
        # banked payload.current_turn, and the player does not always race on
        # the scheduled turn: the live career raced round one on turn 25, so
        # the row went out at turn 25 and the Unity Cup standings screen had a
        # result belonging to no round, drawing round 1 as UNRANKED and hanging
        # there (user-reported 2026-09-06, twice).
        race_num = len(st.get("races") or ()) + 1
        st.setdefault("races", []).append({
            "race_num": race_num,
            "turn": unity_cup.scheduled_race_turn(race_num,
                                                  int(pending.get("turn") or 0)),
            "team_race_set_id": set_id,
            "result_state": final,
        })
        st["last_result"] = final
        st["pending"] = None
        st["opponents"] = None
        return True
    return False


def handle_team_race_out(payload: dict) -> dict:
    """single_mode_team/team_race_out {current_turn} -- leave the race screen.

    THIS is where the play_timing 9 result beats reach the client. The capture
    is unambiguous: on turn 24, 201030 arrives on team_race_out and 201146 on
    the check_event straight after, and team_race_end_out is never called at
    all in that run. producers.py withholds those beats from the ordinary
    check_event chain precisely so they land here, after the race has been run.

    tmp_team_rank is added by impl.attach (it keys off the endpoint)."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    turn = int(payload.get("current_turn") or career["data"]["chara_info"].get("turn") or 0)
    # Out of the race screen and back into the ordinary event chain. The award
    # is settled on the NEXT request, not this one -- this response is still
    # the pre-race rank, with the new one alongside as tmp_team_rank.
    _bank_pending_result(st)
    career["data"]["chara_info"]["playing_state"] = unity_cup.PLAYING_STATE_EVENT_CHAIN
    response = _envelope(full_state, career, "team_race_out")
    events = _take_post_race_events(st, turn, full_state)
    if events:
        response["data"]["unchecked_event_array"] = events
    smt.state_store.save_state(viewer_id, full_state)
    return response


# ====================================================== save_team_edit_flag ==

def handle_save_team_edit_flag(payload: dict) -> dict:
    """single_mode_team/save_team_edit_flag {team_edit_flag, current_turn}.

    The team screen's auto-edit toggle. dump.cs
    SingleModeTeamSaveTeamEditFlagResponse.CommonResponse has exactly ONE field
    -- team_data_set -- so this answers with the envelope alone, no home_info.
    attach() supplies the envelope at the shared chokepoint, so all this has to
    do is persist the flag.

    TeamEditFlag (dump.cs): Invalid=0, On=1, Off=2. Anything else is ignored
    rather than stored: a junk value would sit in team_info forever and the
    client re-reads it every turn."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    flag = payload.get("team_edit_flag")
    if flag in (unity_cup.TEAM_EDIT_ON, unity_cup.TEAM_EDIT_OFF):
        st["team_edit_flag"] = int(flag)
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {"chara_info": copy.deepcopy(career["data"]["chara_info"])}}


# ======================================================== team_race_end_out ==

def handle_team_race_end_out(payload: dict) -> dict:
    """single_mode_team/team_race_end_out {current_turn} -- leave the team-race
    RESULT sequence.

    Distinct from team_race_out, and its response carries two things that one
    does not (dump.cs SingleModeTeamTeamRaceEndOutResponse.CommonResponse):
    `add_music`, which is how the Unity Cup song is granted, and the
    play_timing 9 result beats.

    The song is SINGLE_MODE_TEAM_RACE_LIVE_ID (1035, dump.cs), granted ONCE per
    account: add_music is served only on the run that actually unlocks it,
    because the client reads a non-null add_music as "new song!" and would
    re-announce it after every team race otherwise."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    turn = int(payload.get("current_turn") or career["data"]["chara_info"].get("turn") or 0)
    # BANK FIRST, then read the round back off the history -- this is the exit
    # a client that never calls team_race_end takes, and _take_post_race_events
    # keys on races[-1]. See _bank_pending_result.
    _bank_pending_result(st)
    career["data"]["chara_info"]["playing_state"] = unity_cup.PLAYING_STATE_EVENT_CHAIN
    data = {"chara_info": copy.deepcopy(career["data"]["chara_info"]),
            "tmp_team_rank": unity_cup.pending_rank(st),
            # Only if team_race_out has not already served them: the capture's
            # client takes the team_race_out path and never calls this one, but
            # dump.cs gives this response the field too, so it is the fallback
            # for a client that leaves the result sequence the other way.
            "unchecked_event_array": _take_post_race_events(st, turn, full_state),
            "add_music": _grant_team_race_song(full_state)}
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _grant_team_race_song(full_state: dict):
    """{music_id, acquisition_time} the first time, None afterwards.

    Written to music_list_state -- the same key live_theater.py and presents.py
    reward_type 80 already use -- so the song appears in the jukebox and the
    collection like any other."""
    music_id = unity_cup.TEAM_RACE_LIVE_ID
    musics = full_state.setdefault(_MUSIC_LIST_KEY, [])
    if any(m.get("music_id") == music_id for m in musics):
        return None
    entry = {"music_id": music_id,
             "acquisition_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    musics.append(entry)
    return copy.deepcopy(entry)


def _take_post_race_events(st: dict, turn: int, full_state: dict | None = None) -> list:
    """The play_timing 9 beats for the team race that just finished, CLAIMED
    once.

    producers.py deliberately withholds these from the ordinary check_event
    chain -- they are the result cutscene and must not play before the race has
    been run. Whichever of team_race_out / team_race_end_out the client calls
    first gets them; the other then gets an empty list rather than replaying
    the result cutscene.

    KEYED ON THE ROUND AND ITS RESULT, not on `turn`. The player does not
    always race on the scheduled turn -- the live career raced round one on
    turn 25, not 24 -- and looking the beats up by current_turn found nothing
    at all, which skipped the whole result sequence AND (because the settle
    hook rides on the check_event that sequence provokes) pushed the race
    pay-out onto the next training. `turn` is still taken so the two callers'
    signatures are unchanged; the round is read off the history row
    handle_team_race_end has already appended."""
    from . import producers
    races = st.get("races") or []
    if not races:
        return []
    last = races[-1]
    round_no = int(last.get("race_num") or len(races))
    served = st.setdefault("post_race_served", [])
    if round_no in served:
        return []
    served.append(round_no)
    out = []
    for (event_id, story_id, chara_id, timing, choice_count,
         show_clear) in producers.post_race_beats_for_round(
             round_no, last.get("result_state"),
             beat_elite=unity_cup.beat_elite(st)):
        # Same choice-array rule the producer follows: a choice-less cutscene
        # ships [], or the client waits forever for a resolution it will never
        # send. All five post-race beats DO carry one choice in the capture.
        choices = [{"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                    "gain_select_id_index": 1, "select_icon": 0}] if choice_count else []
        out.append({
            "event_id": event_id, "story_id": story_id, "chara_id": chara_id,
            "play_timing": timing,
            "event_contents_info": {
                "support_card_id": 0, "show_clear": show_clear,
                "show_clear_sort_id": 1,
                "choice_array": choices,
                "is_effected_multi_chara": False, "tips_training_partner_id": None},
            "succession_event_info": None, "minigame_result": None,
        })
    # The turn stays frozen until the LAST of these is acknowledged -- see
    # unity_cup.race_hold.
    st[unity_cup.POST_RACE_LAST_KEY] = out[-1]["event_id"] if out else None
    # ... and they go out ONE PER RESPONSE, the head now and the rest on the
    # check_event that each resolution provokes (unity_cup.next_post_race_beat).
    # The capture serves them that way and the client only ever plays the head
    # of an array: shipping all of them lost every beat but the first.
    st[unity_cup.POST_RACE_CHAIN_KEY] = copy.deepcopy(out)
    # The fifth round's chain ends with URA's invitation beat, which
    # unity_cup_ura_invite would otherwise ALSO offer a turn later. Mark the
    # producer's key spent so the chain is the only thing that serves it; the
    # producer stays as the safety net for a chain that never got built.
    if full_state is not None and any(
            e["event_id"] == producers.URA_INVITE_EVENT for e in out):
        CE.mark_fired(full_state, producers.URA_INVITE_KEY)
    return out[:1]


# ======================================================= team_race_continue ==

def handle_team_race_continue(payload: dict) -> dict:
    """single_mode_team/team_race_continue {continue_type, current_turn} --
    retry a LOST team race.

    Mirrors handle_continue's contract, which was itself derived from a live
    client: continue_type 1 = the free daily retry, 2 = an Alarm Clock, and
    result_code 205 means "not retryable" (the client accepts that and keeps
    the result). The budgets are the career's own, so a team-race retry and a
    goal-race retry draw on the same pool -- the client shows one counter.

    A DRAW IS RETRYABLE. Only a win is not. The client offers the retry on a
    drawn match and we answered 205, so it kept the draw (user-reported
    2026-09-06). The old rule -- lose only -- was an assumption: no captured
    run ever drew a match (2540 wins and 218 losses in the history rows, no
    draws at all), so nothing ever tested it.

    IT ARRIVES FROM THE RESULT SCREEN, WHILE THE BLOCK IS STILL PENDING. The
    retry comes between team_race_start and team_race_end -- the live request
    log has start at 12:48:10 and continue at 12:48:34 with no end between --
    so `races` is still empty and the result to judge is
    st["pending"]["final_win_type"]. Reading races[-1] refused every retry that
    has ever been offered, whatever its result. A block already banked into
    history is still honoured, so a client that ends first can retry too.

    The whole 5-round race is re-simulated rather than replayed, against THE
    SAME OPPOSING TEAM, so the retry is a genuinely fresh roll of the match the
    player just watched. A banked attempt's ladder move is rolled back BEFORE
    the new race is run, otherwise a retried loss would leave the team a rank
    it never actually finished at."""
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    races = st.get("races") or []
    pending = st.get("pending") or {}
    if pending.get("final_win_type") is not None:
        result, banked = int(pending.get("final_win_type") or 0), None
    elif races:
        result, banked = int(races[-1].get("result_state") or 0), races[-1]
    else:
        return _refused()
    if result not in (_RESULT_LOSE, _RESULT_DRAW):
        return _refused()

    race_ctx = full_state.get(smt.RACE_CTX_KEY) or {}
    counts = smt._continue_counts(full_state, race_ctx)
    continue_type = payload.get("continue_type")
    if continue_type == 2:
        if counts["available_continue_num"] <= 0 or not smt._spend_alarm_clock(full_state):
            return _refused()
        race_ctx["continue_paid_used"] = int(race_ctx.get("continue_paid_used") or 0) + 1
    else:
        if counts["available_free_continue_num"] <= 0:
            return _refused()
        race_ctx["continue_free_used"] = int(race_ctx.get("continue_free_used") or 0) + 1
    full_state[smt.RACE_CTX_KEY] = race_ctx

    # Roll the attempt back BEFORE re-running: the history row and the ladder
    # drop both belong to a race that is about to be replaced. A block still
    # pending has neither yet -- it is simply discarded.
    attempt = banked if banked is not None else pending
    if banked is not None:
        races.pop()
    st["team_rank"] = int(st.get("rank_before_race") or st.get("team_rank") or 30)
    st["rank_state"] = 0
    st["last_result"] = None
    # The attempt's pay-out is discarded WITH the attempt. It was only ever
    # banked (see impl.bank_race_award), so nothing has to be undone -- but
    # leaving it queued would settle the retried race at the old rank.
    st.pop("pending_award", None)

    chara_info = career["data"]["chara_info"]
    turn = int(attempt.get("turn") or payload.get("current_turn") or 0)
    # Bump the attempt counter BEFORE re-simulating: it feeds the race seed, so
    # incrementing afterwards would roll the retry on the losing seed again.
    st["continue_used"] = int(st.get("continue_used") or 0) + 1
    set_id = int(attempt.get("team_race_set_id") or attempt.get("set_id") or 0)
    race_set = unity_cup.team_race_set(set_id)
    if race_set:
        # THE SAME OPPONENTS, and with the right number of arguments. This call
        # had gone stale when the opposing team became an argument, so even a
        # genuine loss raised TypeError here, was swallowed by the except
        # below, and came back as the same 205 the gate above was producing.
        offer = _offer_for(st, chara_info, set_id, turn)
        try:
            block = _run_team_race(st, chara_info, viewer_id, race_set, turn,
                                   (offer or {}).get("team_data_array") or [])
        except Exception:
            log.exception("unity cup: team race retry simulation failed")
            return _refused()
        block["set_id"] = set_id
        block["turn"] = turn
        block["version"] = unity_cup.PENDING_BLOCK_VERSION
        st["pending"] = block
    data = {"chara_info": copy.deepcopy(chara_info)}
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        career_home.update(smt._continue_counts(full_state, race_ctx))
        data["home_info"] = copy.deepcopy(career_home)
    if continue_type == 2:
        data["user_item"] = {"item_id": smt._ALARM_CLOCK_ITEM,
                             "number": smt._alarm_clock_stock(full_state)}
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}
