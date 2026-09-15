"""Practice race handler.

Two generations of fix live here:

1. fixtures.py corrects for UmaDumpy's URL/body attribution bug (see its
   _load_umadumpy) so every pair filed under "practice_race/race_start" is a
   genuine captured race, keyed by its real request (race_instance_id +
   entry_chara_array). Kept as the fallback path.

2. Real simulation (app/simulation/race_simulator.py): runs the actual race
   physics for the uma/course the client selected -- covers every uma, not
   just the ~4 we happened to capture. This is now the primary path;
   fixture matching only kicks in if simulation can't run (course lookup
   fails, the selected uma isn't in the known roster, node isn't
   available, etc).
"""

from __future__ import annotations

import functools
import logging
import random

from .. import state as state_store
from ..fixtures import store as fixtures
from ..simulation import race_simulator
from . import trained_chara
from .load import STATE_KEY as LOAD_INDEX_STATE_KEY

log = logging.getLogger("uma-server")

ENDPOINT = "practice_race/race_start"

# A horse carrying an unrealistic number of skills (the maxed inject holds every
# skill in the game, 693) bloats the race: hundreds of skill-activation events in
# the scenario blob and a giant per-horse skill list. Cap to a realistic count
# for the race only (roster/display records keep the full list), applied to
# every horse so a normal race that draws the maxed uma as an opponent is safe.
RACE_SKILL_CAP = 40

_skill_meta_cache: dict | None = None


def _skill_meta() -> dict:
    """skill_id -> (rarity, grade_value). rarity: 1=white, 2=gold, 3/4/5=unique/
    special. grade_value ~ skill strength."""
    global _skill_meta_cache
    if _skill_meta_cache is None:
        from .. import master_data
        _skill_meta_cache = {
            r["id"]: (r["rarity"], r["grade_value"] or 0)
            for r in master_data.query("SELECT id, rarity, grade_value FROM skill_data")
        }
    return _skill_meta_cache


def _select_race_skills(chara: dict) -> dict:
    """Trim an over-stuffed skill list to RACE_SKILL_CAP for the race. NOT a
    plain truncation: skill_array is ordered by id, and the lowest ids are all
    uniques (rarity 3/4/5, ids ~10071-100xxx) while white (rarity 1) / gold
    (rarity 2) start at id 200011 -- so `skills[:cap]` yields only uniques.
    Reserve explicit slots per type (white / gold / unique), each filled by
    strength (grade_value), so the race shows a real mix of all three. (Sorting
    white+gold together doesn't work: gold always outranks white by grade_value,
    so white never makes the cut.)"""
    skills = chara.get("skill_array") or []
    if len(skills) <= RACE_SKILL_CAP:
        return chara
    meta = _skill_meta()

    def grade(s):
        return meta.get(s["skill_id"], (1, 0))[1]

    def rarity(s):
        return meta.get(s["skill_id"], (1, 0))[0]

    white = sorted((s for s in skills if rarity(s) == 1), key=grade, reverse=True)
    gold = sorted((s for s in skills if rarity(s) == 2), key=grade, reverse=True)
    unique = sorted((s for s in skills if rarity(s) in (3, 4, 5)), key=grade, reverse=True)

    # target split of the cap: ~20% unique, remainder even between white & gold
    n_unique = min(len(unique), max(4, RACE_SKILL_CAP // 5))
    rest = RACE_SKILL_CAP - n_unique
    n_gold = min(len(gold), rest // 2)
    n_white = min(len(white), rest - n_gold)
    selected = white[:n_white] + gold[:n_gold] + unique[:n_unique]

    # if a bucket ran short, top up from whatever's left so we still fill the cap
    if len(selected) < RACE_SKILL_CAP:
        chosen = {id(s) for s in selected}
        leftover = [s for s in white[n_white:] + gold[n_gold:] + unique[n_unique:] if id(s) not in chosen]
        selected += leftover[: RACE_SKILL_CAP - len(selected)]

    capped = dict(chara)
    capped["skill_array"] = selected
    return capped


def _frozen_servertime(viewer_id) -> int:
    """The rest of the session runs on the frozen timeline baked into the
    load/index seed (see patch_response's servertime handling) -- every
    other handler either inherits a fixture's own frozen servertime or,
    like load/index itself, never touches the field, so patch_response
    never overwrites it. This handler builds a response from scratch with
    no fixture behind it, so without this it'd fall through to
    patch_response's "totally missing" fallback (real current time), which
    races far ahead of that frozen timeline and trips the client's "new
    day" flow. Match it explicitly instead."""
    full_state = state_store.get_state(viewer_id) or {}
    load_index = full_state.get(LOAD_INDEX_STATE_KEY, {})
    servertime = load_index.get("data_headers", {}).get("servertime")
    return servertime if servertime is not None else 1784325282


def _trained_chara_id(payload: dict) -> object:
    entries = payload.get("entry_chara_array") or []
    return entries[0].get("trained_chara_id") if entries else None


def _running_style(payload: dict) -> int | None:
    entries = payload.get("entry_chara_array") or []
    return entries[0].get("running_style") if entries else None


def _entry_chara_specs(payload: dict) -> list[dict]:
    """Every entry in entry_chara_array, not just the first. BUG FIXED
    2026-08-30 (live-reported: entering more than one of the player's own
    umas in a practice race only ever showed one of them). Confirmed via a
    real proxy-captured practice_race/race_start request against the
    genuine Cygames server: entry_chara_array legitimately carries multiple
    entries (5 in the verified capture), each with its OWN entry_id,
    trained_chara_id, and running_style -- and the real response's
    trained_chara_array/entry_info_array/race_horse_data_array all carry
    one row per entry, each at its own independently-randomized gate. This
    server only ever read entries[0] (_trained_chara_id/_running_style
    above), so every additional entry was silently dropped."""
    entries = payload.get("entry_chara_array") or []
    return [
        {"entry_id": e.get("entry_id", i), "trained_chara_id": e.get("trained_chara_id"),
         "running_style": e.get("running_style")}
        for i, e in enumerate(entries)
        if e.get("trained_chara_id") is not None
    ]


def _get_roster(viewer_id) -> list[dict]:
    """The live per-viewer house roster (the same source trained_chara/load
    serves), NOT the raw captured fixture -- so the player's selectable umas
    and their practice-race opponents are the curated house veterans (plus any
    careers finished on this server) and stay in sync with what the succession/
    roster screens show. Reading the fixture here was why a roster entry that
    existed in live state but not the capture (e.g. an injected or freshly
    finished uma) could never be found as the player or drawn as an opponent."""
    return trained_chara._get_or_seed_roster(viewer_id)


def _is_real_chara(chara_id) -> bool:
    """A renderable character id: 1 is the mob placeholder, real trainees are
    1001+. An npc ROW id (243, 323, ...) is neither and has no assets."""
    return bool(chara_id) and (chara_id == 1 or chara_id >= 1000)


@functools.lru_cache(maxsize=1)
def _mob_ids() -> tuple:
    from .. import master_data
    return tuple(r["mob_id"] for r in master_data.query(
        "SELECT mob_id FROM mob_data ORDER BY mob_id"))


def _fallback_mob_id(seed: int) -> int:
    """A real mob id from master, chosen deterministically so the same opponent
    keeps the same face across responses for a race."""
    pool = _mob_ids()
    return pool[abs(int(seed or 0)) % len(pool)] if pool else 8000


def _build_race_horse_entry(chara: dict, frame_order: int, final_grade: int, popularity: int, popularity_mark_rank_array: list[int]) -> dict:
    running_style = chara.get("running_style", 2)
    card_id = chara.get("card_id", 0)

    # MOB PRESENTATION. Every entry must be renderable: the paddock loads each
    # horse's costume, and a race_dress_id of 0 NullRefs in
    # Gallop.PaddockViewControllerBase.RegisterTimeline -- the view transition
    # never finishes and the race softlocks (confirmed from the client's
    # Player.log). This used to hardcode mob_id 0 and keep whatever
    # dress_for_card returned, so NPC opponents went out as named umas with
    # dress 0 and, via the chara_id fallback above, an npc ROW id (e.g. 243)
    # sitting in chara_id.
    #
    # BUG FIXED 2026-08-19 (live-reported -- a daily race's paddock hung
    # forever on this same null-ref). This branch used to run AFTER
    # resolving race_dress_id via card_data/dress_for_card, so a mob (whose
    # card_id is always 0) still got a REAL character's costume fallback
    # (dress_for_card(0, 1) == 101, a real dress_data row -- not empty, so
    # 'or 1' never even triggered) before being collapsed to the bare
    # placeholder 1 regardless. Checked FIRST now, before any card/dress_for
    # _card lookup runs at all: a real daily_race capture (captures/
    # 20260819_173442) shows most mob rows carry a genuine small dress id of
    # their own (8/11/12/70/80/100/150, confirmed against master.mdb's
    # daily_race_npc across 3 real races) -- 1 is only the honest fallback
    # for mobs that truly have nothing better (difficulty-1's own
    # placeholder-only NPC pool, or any caller that never set
    # chara["race_dress_id"] at all).
    mob_id = chara.get("mob_id") or 0
    if mob_id:
        chara_id, card_id = 1, 0
        race_dress_id = chara.get("race_dress_id") or 1
    else:
        from .. import master_data
        row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        # AN EXPLICIT chara_id WINS over single_mode_chara_id. A Unity Cup team
        # race fields NAMED opponents out of single_mode_npc, where the row id
        # (1071100) and the character it draws (1071) are different numbers --
        # and single_mode_chara_id has to stay the ROW id, because that is what
        # the client looks the opponent panel up by. Falling through to it put
        # an npc row id in chara_id, which has no assets, so the other team
        # raced as blank portraits (user-reported 2026-09-06).
        chara_id = (row["chara_id"] if row
                    else chara.get("chara_id")
                    or chara.get("single_mode_chara_id", 0))
        # resolve the real race dress (alt/summer cards -> their actual costume,
        # e.g. Summer Maru card 100402 -> dress 100430), NOT the base outfit
        # fallback. An EXPLICIT race_dress_id on the entry wins: scenario NPCs
        # like Happy Meek (chara 2001) have no card_data row at all, so dress_
        # for_card can't find their costume -- single_mode_npc carries it
        # instead (hers is 200101).
        race_dress_id = (chara.get("race_dress_id")
                         or trained_chara.dress_for_card(card_id, chara_id))
        if not race_dress_id or not _is_real_chara(chara_id):
            mob_id = _fallback_mob_id(chara.get("single_mode_chara_id")
                                      or chara.get("trained_chara_id") or 0)
            chara_id, card_id, race_dress_id = 1, 0, 1

    return {
        "frame_order": frame_order,
        "viewer_id": chara.get("viewer_id", 0),
        "trainer_name": chara.get("trainer_name", "Trainer"),
        "owner_viewer_id": chara.get("owner_viewer_id", 0),
        "owner_trainer_name": chara.get("owner_trainer_name", ""),
        "single_mode_chara_id": chara.get("single_mode_chara_id", 0),
        "trained_chara_id": chara.get("trained_chara_id", 0),
        "nickname_id": chara.get("nickname_id", 0),
        "chara_id": chara_id,
        "card_id": card_id,
        "mob_id": mob_id,
        "rarity": chara.get("rarity", 1),
        "talent_level": chara.get("talent_level", 1),
        "skill_array": chara.get("skill_array", []),
        "stamina": chara["stamina"],
        "speed": chara["speed"],
        "pow": chara.get("pow", chara.get("power", 1)),
        "guts": chara["guts"],
        "wiz": chara.get("wiz", chara.get("wisdom", 1)),
        "running_style": running_style,
        "race_dress_id": race_dress_id,
        "chara_color_type": chara.get("chara_color_type", 0),
        # NpcType, and the client tints and routes the portrait by it: 11 for
        # the player's own team-race entries, 20 for the OPPOSING team, 0 for
        # the neutral field. Hardcoding 0 threw away what _team_horse and
        # _opponent_horse had already set (capture: 277 team-1 rows all 11, 237
        # team-2 rows all 20, 686 field rows all 0 -- no exceptions).
        "npc_type": chara.get("npc_type", 0),
        "final_grade": final_grade,
        "popularity": popularity,
        "popularity_mark_rank_array": popularity_mark_rank_array,
        "proper_distance_short": chara.get("proper_distance_short", 1),
        "proper_distance_mile": chara.get("proper_distance_mile", 1),
        "proper_distance_middle": chara.get("proper_distance_middle", 1),
        "proper_distance_long": chara.get("proper_distance_long", 1),
        "proper_running_style_nige": chara.get("proper_running_style_nige", 1),
        "proper_running_style_senko": chara.get("proper_running_style_senko", 1),
        "proper_running_style_sashi": chara.get("proper_running_style_sashi", 1),
        "proper_running_style_oikomi": chara.get("proper_running_style_oikomi", 1),
        "proper_ground_turf": chara.get("proper_ground_turf", 1),
        "proper_ground_dirt": chara.get("proper_ground_dirt", 1),
        "motivation": chara.get("motivation", 3),
        "win_saddle_id_array": chara.get("win_saddle_id_array", []),
        "race_result_array": [],
        "team_id": chara.get("team_id", 0),
        "team_member_id": chara.get("team_member_id", 0),
        "team_rank": chara.get("team_rank", 0),
        "single_mode_win_count": chara.get("wins", 0),
        "fan_count": chara.get("fans", 0),
        "item_id_array": [],
        "motivation_change_flag": 0,
        "frame_order_change_flag": 0,
    }


def _simulate(payload: dict) -> dict | None:
    try:
        return _simulate_inner(payload)
    except Exception:
        # Any failure anywhere in simulation or envelope-building must fall
        # back to fixture replay rather than propagate -- an uncaught
        # exception here becomes an HTTP 500, which the real client shows
        # as nothing happening at all (no error popup), which is a much
        # worse failure mode than replaying a captured race.
        log.exception("race simulation failed, falling back to fixture replay")
        return None


def _simulate_inner(payload: dict) -> dict | None:
    # BUG FIXED 2026-08-30 (live-reported: entering more than one of the
    # player's own umas in a practice race only shows one of them; the
    # others silently don't appear). Confirmed real via a proxy-captured
    # request/response against the genuine Cygames server: entry_chara_array
    # legitimately carries multiple entries, and the real response gives
    # each its own trained_chara_array row, its own entry_info_array
    # (entry_id -> frame_order) row, and its own row in
    # race_horse_data_array at its own independently-randomized gate. Every
    # entry beyond entries[0] is now carried through the same way.
    entry_specs = _entry_chara_specs(payload)
    race_instance_id = payload.get("race_instance_id")
    if not entry_specs or race_instance_id is None:
        return None

    roster = _get_roster(payload.get("viewer_id"))
    if not roster:
        return None

    roster_by_id = {c.get("trained_chara_id"): c for c in roster}
    players = []
    player_entry_ids = []
    for spec in entry_specs:
        base = roster_by_id.get(spec["trained_chara_id"])
        if base is None:
            return None
        chara = dict(base)
        if spec.get("running_style"):
            chara["running_style"] = spec["running_style"]
        players.append(chara)
        player_entry_ids.append(spec["entry_id"])

    # BUG FIXED 2026-08-20 (live-reported: uma-selection screen shows, but
    # clicking race softlocks). Opponents used to be sampled from the SAME
    # house/legacy roster _get_roster draws the player's own pick from --
    # random.sample(pool, 17) needs 17 OTHER veterans in the box, which no
    # real account has this early (the confirmed real account has 9 total;
    # a fresh/rebuilding one has far fewer), so this returned None on every
    # attempt and fell through to fixture replay -- 0 fixtures exist for
    # this endpoint without the (missing on this machine) UmaDumpy dumps
    # dir, so the client got a bare {"data": {}} and null-ref'd on
    # RaceResultInfo.race_horse_data_array (Player.log confirms:
    # WorkPracticeRaceData+RaceResultData.Setup). It also isn't correct even
    # when the roster IS big enough: a practice-race field of the player's
    # OWN former maxed-out umas is exactly the mismatch
    # single_mode_team._mob_opponents was already written to avoid for
    # mandatory career races (see its docstring) -- practice race just never
    # got the same fix. Reuse it here: real single_mode_npc mob data, scaled
    # to the picked uma's own current stat total, same as every other
    # single_mode race on this server.
    #
    # Multi-entry scaling basis: real captures show a full 18-horse field
    # regardless of how many of those are the player's own umas (5 player +
    # 13 mobs = 18 in the verified capture) -- opponent count is 18 minus
    # however many player entries there are. Scaled against the FIRST
    # entry's stat total (unverified against multiple real stat levels;
    # matches this server's pre-existing single-entry behaviour exactly
    # when there's only one).
    from . import single_mode_team
    opponents_needed = max(0, 18 - len(players))
    # Every uma the PLAYER entered is already on the track -- keep the filler
    # from handing one of them a second body in the same field.
    entered = {(p.get("card_id") or 0) // 100 for p in players} - {0}
    opponents = single_mode_team._mob_opponents(players[0], max(opponents_needed, 17),
                                                exclude_charas=entered)
    if len(opponents) < opponents_needed:
        return None

    # Intentionally NO skill cap: a horse races with every skill it holds, so
    # every skill that can activate does (the maxed uma procs ~281 of her 693).
    # The earlier cap came from a wrong hypothesis that skill-event volume
    # crashed the client -- the real crash was the invalid dress (now fixed).
    # The client already renders a 693-skill trained_chara in the roster without
    # issue, and the scenario's event_count is an int32. If a genuine per-race
    # skill/event limit ever surfaces (Player.log would show a
    # Gallop.RaceSimulateEventData error), reinstate _select_race_skills here.

    result = race_simulator.simulate_race(
        players[0], opponents, race_instance_id,
        ground_condition=payload.get("ground_condition") or 1,
        weather=payload.get("weather") or 1,
        season=payload.get("season") or 1,
        entry_num=payload.get("entry_num"),  # the player's chosen field size
        extra_player_charas=players[1:],
    )
    if result is None:
        return None

    horses = result["horses"]
    sim_results = result["sim_results"]

    def _rank_by(key):
        order = sorted(range(len(horses)), key=key)
        return {idx: rank + 1 for rank, idx in enumerate(order)}

    # popularity + its 3-category mark array: real captures always carry
    # exactly 3 rank values per horse (see server logs -- the client's
    # Gallop.HorseData.InitPopularity() indexes this array unconditionally
    # at [0]/[1]/[2] and throws IndexOutOfRangeException on an empty one,
    # which is what was actually causing "nothing happens" on every
    # simulated race). Real semantics (presumably win/place/show odds
    # ranks) aren't reverse-engineered; three independent stat-based
    # rankings are structurally valid and non-crashing, which is what
    # matters here.
    popularity_by_index = _rank_by(lambda i: -horses[i]["speed"])
    stamina_rank_by_index = _rank_by(lambda i: -horses[i]["stamina"])
    power_rank_by_index = _rank_by(lambda i: -horses[i].get("pow", horses[i].get("power", 0)))

    # BUG FIXED 2026-08-24 (live-reported: "at the start of the race, before
    # gates open, my uma is always in gate 1, and then she randomly teleports
    # to her actual gate"). frame_order (dump.cs RaceHorseData.frame_order) is
    # what the client uses for the PRE-race gate lineup -- a completely
    # separate field from the in-race animation's lane_position, which
    # simulate_race/run_simulation already randomizes correctly. Using the
    # plain array index here (player always index 0) put her in "gate 1"
    # for the lineup every time regardless of the real randomized gate the
    # animation then jumps to once the race actually starts.
    gate_assignment = result.get("gate_assignment") or list(range(len(horses)))
    race_horse_data_array = []
    for i, (chara, sim) in enumerate(zip(horses, sim_results)):
        race_horse_data_array.append(_build_race_horse_entry(
            chara,
            # final_grade is the character's evaluation RANK (single_mode_rank
            # id, 1=G .. 98=US9), NOT the finish position -- the client renders
            # this as the rank badge. Setting it to finish order made every
            # uma's rank track where it placed (1st->G, 18th->SS+).
            frame_order=gate_assignment[i] + 1,
            final_grade=chara.get("rank", 1),
            popularity=popularity_by_index[i],
            popularity_mark_rank_array=[popularity_by_index[i], stamina_rank_by_index[i], power_rank_by_index[i]],
        ))

    envelope = {
        "response_code": 1,
        "data_headers": {
            "result_code": 1,
            "notifications": {},
            "servertime": _frozen_servertime(payload.get("viewer_id")),
        },
        "data": {
            "trained_chara_array": players,
            # entry_info_array tells the client which race_horse_data_array
            # row IS each of the player's own entries, by frame_order -- it
            # was left hardcoded to a single {entry_id:0, frame_order:1}
            # when the gate_assignment fix above (2026-08-24) stopped
            # frame_order from always being 1. Whenever the real gate wasn't
            # 1 (the ~17/18 common case), this pointed at some opponent
            # mob's row instead; the client's UpdateCurrentRaceEntryCharaData
            # tries to link that row to trained_chara_array (which only ever
            # held the player's own TrainedChara), fails, and null-refs --
            # softlocking race_start almost every time. Now one row per
            # player entry (see _entry_chara_specs), each matching that
            # entry's own actual assigned gate -- horses[0:len(players)] are
            # the player entries in submission order (simulate_race puts
            # extra_player_charas right after player_chara, before any
            # opponent).
            "entry_info_array": [
                {"entry_id": eid, "frame_order": gate_assignment[i] + 1}
                for i, eid in enumerate(player_entry_ids)
            ],
            "practice_partner_owner_info_array": [],
            # Filled in by handle_race_start once the envelope is built -- this
            # used to be random.randint(1, 2**31-1), a throwaway the server
            # never stored, so nothing the client did with the id afterwards
            # (save it, replay it, overwrite it) could possibly resolve.
            "practice_race_id": 0,
            "state": 1,
            "race_result_info": {
                "race_instance_id": race_instance_id,
                "race_horse_data_array": race_horse_data_array,
                "season": payload.get("season") or 1,
                "weather": payload.get("weather") or 1,
                "ground_condition": payload.get("ground_condition") or 1,
                "random_seed": result["seed"],
                "race_scenario": result["race_scenario"],
            },
        },
    }
    log.info(
        "practice_race/race_start: simulated race_instance_id=%s players=%s scenario_b64_len=%d "
        "frame_count=%d (raw=%d) duration=%.1fs",
        race_instance_id, [p.get("trained_chara_id") for p in players], len(result["race_scenario"]),
        result["frame_count"], result["raw_frame_count"],
        max((s["finishTime"] for s in sim_results), default=0),
    )
    return envelope


def _mark_practice_race_participated(payload: dict) -> None:
    """missions.py's 200028 ('Participate in a practice race') -- a real
    call reaching this handler at all (real simulation OR fixture fallback,
    both are a genuine practice race attempt from the client's own POV) is
    the only signal needed; condition_num is 1 in every real row."""
    viewer_id = payload.get("viewer_id")
    if not viewer_id:
        return
    from . import missions
    from .. import state as state_store
    full_state = state_store.get_state(viewer_id) or {}
    missions.mark_achieved(full_state, missions.FLAG_PRACTICE_RACE_RUN)
    state_store.save_state(viewer_id, full_state)


def handle_get_follow_user_data(payload: dict) -> dict:
    """practice_race/get_follow_user_data -- called right after picking a
    track, before race_start. BUG FIXED 2026-08-19 (live-reported: practice
    race could softlock the instant a track was picked, or hang forever on
    the actual race start -- both from THIS endpoint, not race_start itself).

    This had no handler and no fixture, so it fell through to main.py's
    bare NOOP_SUCCESS ({"data": {}}) -- a real client capture, Player.log
    fatal in-hand: Gallop.WorkPracticeRaceData.Update null-refs on
    PracticeRaceGetFollowUserDataResponse.CommonResponse (dump.cs) missing
    its two array fields. MessagePack C# leaves an absent field at its
    default (null for arrays, not empty), and the client iterates both
    unconditionally with no null guard -- an empty map answers the request
    but crashes the very next frame either at track-select (this response)
    or, if that particular UI path tolerates the null a little longer,
    visibly hangs once race_start's OWN response arrives and something else
    finally touches the still-null array.

    follow_user_chara_array / practice_partner_owner_info_array: "trainers
    you follow who are also running a practice race right now" -- real
    social data this project has no way to populate (same "no other real
    players" reasoning as every other follow/friend feature here). Empty
    arrays are the honest answer, not null."""
    return {
        "response_code": 1,
        "data_headers": {"result_code": 1, "notifications": {}},
        "data": {
            "follow_user_chara_array": [],
            "practice_partner_owner_info_array": [],
        },
    }


def handle_race_start(payload: dict) -> dict:
    import time
    t0 = time.time()
    log.info("practice_race/race_start: handling request")
    _mark_practice_race_participated(payload)
    simulated = _simulate(payload)
    log.info("practice_race/race_start: _simulate took %.2fs, result=%s", time.time() - t0, "ok" if simulated else "None")
    if simulated is not None:
        log.info("practice_race/race_start: real simulation")
        # Hand the finished race to the lobby as the PENDING one and report the
        # id it filed under. practice_race/race_end is what decides whether it
        # reaches the saved-race shelf; race_replay reads back these exact
        # bytes, because re-simulating would show a different race under the
        # same name.
        try:
            from . import practice_race_lobby
            simulated["data"]["practice_race_id"] = practice_race_lobby.remember_race(
                payload.get("viewer_id"), payload, simulated["data"])
        except Exception:
            log.exception("practice_race: could not file the race for replay; "
                          "serving it unsaved")
        return simulated

    log.info("practice_race/race_start: falling back to fixture replay")
    pairs = fixtures.all_for(ENDPOINT)
    if not pairs:
        return {
            "response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}},
            "data": {},
        }

    wanted_chara = _trained_chara_id(payload)
    wanted_course = payload.get("race_instance_id")

    def score(pair):
        chara_match = wanted_chara is not None and _trained_chara_id(pair.request) == wanted_chara
        course_match = wanted_course is not None and pair.request.get("race_instance_id") == wanted_course
        return (chara_match, course_match)

    best = max(pairs, key=score)
    return best.response
