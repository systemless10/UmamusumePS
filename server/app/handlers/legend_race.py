"""Periodic Legend Races (legend_race/*) -- the limited-time EVENT one.

NOT the same feature as daily_legend_race/* (daily_races.py), despite the
near-identical master schema. The two are separate systems with separate
currencies, and conflating them is the easy mistake here:

  daily_legend_race   PERMANENT. Always open. You pick an opponent from a
                      LIST, one ticket a day, modest rewards.
                      Ticket = item 168 "Daily Legend Race Ticket".
  legend_race (here)  PERIODIC. Runs in short event windows, ONE boss to race
                      per window, much larger rewards, and its own mission
                      list on top.
                      Ticket = item 97 "Legend Race Ticket".

(Both item names are master.mdb text_data category 23, indexes 97 and 168 --
the client itself names them apart, which is the cleanest proof they are two
systems and not one.)

Event shape, straight out of master.mdb `legend_race` (44 rows): each row is
one boss, windowed by start_date/end_date, and they run in back-to-back
batches -- e.g. id 53 from 2026-09-03 22:00 to 09-06 14:59, then id 54 from
09-06 15:00 to 09-09 14:59. Earlier batches ran three. So "how many are
active" is not a constant; it falls out of the window test, and normally
comes to exactly one.

Wire truth. Only legend_race/index was ever captured against the real server
(captures/20260903_204408 tx 7 and 20260904_232507 tx 17, the same event id
53 before and after being cleared). The other eight endpoints are dump.cs
field sets -- `LegendRace*Response.CommonResponse` -- and are marked
SYNTHESIZED where the body is not otherwise pinned. The one race_entry
capture we have (20260903_204408 tx 8) is a 217 error with NO data body, so
it confirms the REQUEST shape {legend_race_id, trained_chara_id} and nothing
about the response.

Rewards: the master row carries first_clear_item_*_1..3 and pick_up_item_*_1..3
(real data -- e.g. id 53 pays 10x the boss's card piece + 150 carats + 10,000
Monies on first clear, and 1 piece as the per-run drop). It ALSO carries
drop_reward_odds_id / victory_reward_odds_id, and those are the gap: there is
no odds table anywhere in Global's master.mdb for them. A mechanical scan of
every table x every integer column for the ids 10001..10012 came back with
nothing that is a reward table, so the odds live server-side on the real
server and cannot be read here. What is synthesized is called out at
_award().

Simulation, field building and the boss profile are all reused from
daily_races.py rather than reimplemented -- the schemas are identical, so
_legend_opponents/_boss_chara/_run_race already do the right thing for these
rows.
"""

from __future__ import annotations

import copy
import functools
import logging
import random

from .. import config, master_data, patch
from .. import state as state_store
from . import daily_races, limited_shop, registry

log = logging.getLogger("uma-server")

LEGEND_KEY = "legend_race_state"
TICKET_ITEM_ID = 97           # "Legend Race Ticket" (text_data cat 23 idx 97)
_TICKET_PER_PURCHASE = 1
_VICTORY_MONEY = 20000        # synthesized; see _award()


@functools.lru_cache(maxsize=1)
def _billing() -> tuple:
    """(frequency, pay_cost) from master.mdb `legend_race_billing`.

    BUG FIXED 2026-09-11 (live-reported broken start UI). The first cut of
    handle_recovery_ticket invented a 5-per-day cap at 50 carats. The real
    numbers are in master and say something completely different:

        legend_race_billing   frequency 0, pay_cost 0
        daily_race_billing    frequency 1, pay_cost 100

    frequency 0 means extra entries CANNOT be bought for a periodic legend
    race at all -- which is the master-data form of the user's own report,
    "there's only 1 entry a day so there's no multiple clear option". Serving
    a purchasable ticket, and a purchase_num that could climb above a cap of
    zero, hands the client a negative "buys remaining" for its entry UI to
    render.
    """
    row = master_data.query_one(
        "SELECT frequency, pay_cost FROM legend_race_billing ORDER BY id LIMIT 1")
    if row is None:
        return (0, 0)
    return ((row["frequency"] or 0), (row["pay_cost"] or 0))


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse() -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}


# ---------------------------------------------------------------------------
# which races are open

@functools.lru_cache(maxsize=1)
def _all_races() -> tuple:
    return tuple(master_data.query("SELECT * FROM legend_race ORDER BY start_date, id"))


@functools.lru_cache(maxsize=1)
def _ticket_category() -> int:
    """The ticket's real item_category from master.mdb (140), read rather
    than hardcoded. daily_races._grant only branches on 90 (carats) and 102
    (pieces) and routes everything else to the item list, so any plain
    category lands the ticket correctly -- but reporting the item's OWN
    category is what the reward summary should carry."""
    row = master_data.query_one("SELECT item_category FROM item_data WHERE id=?",
                                (TICKET_ITEM_ID,))
    return (row["item_category"] if row else 0) or 0


def _row(legend_race_id):
    if not isinstance(legend_race_id, int) or isinstance(legend_race_id, bool):
        return None
    return next((r for r in _all_races() if r["id"] == legend_race_id), None)


def active_races() -> list:
    """The legend races open right now.

    Window test against patch._servertime(), the same clock story_event.py's
    _active_event_id uses -- so a servertime override moves this feature's
    schedule with everything else rather than out of step with it.

    OVERRIDE: client_config.json "legend_race_id" pins one race open
    regardless of its dates. This exists because the master schedule is
    Global's real one and every window in it has already closed (the last,
    id 54, ended 2026-09-09), so on a private server the feature would
    otherwise be permanently dark and untestable. Unset by default, in which
    case the real dates decide and an empty list is the honest answer.
    """
    forced = config.get("legend_race_id")
    if forced is not None:
        row = _row(int(forced))
        return [row] if row is not None else []
    now = patch._servertime()
    return [r for r in _all_races() if (r["start_date"] or 0) <= now <= (r["end_date"] or 0)]


# ---------------------------------------------------------------------------
# per-viewer state

def _state(full_state: dict) -> dict:
    """The viewer's legend-race block.

    `cleared` and `played` are LIFETIME ledgers keyed by race id, not daily:
    an event race is cleared once and stays cleared, which is exactly what
    the two captures show (is_cleared/is_played 0 -> 1 across the same id 53
    on consecutive days). Only purchase_num rolls, on the shared
    daily_races._served_day() reset clock.
    """
    st = full_state.setdefault(LEGEND_KEY, {})
    st.setdefault("cleared", [])
    st.setdefault("played", [])
    day = daily_races._served_day()
    if st.get("day") != day:
        st["day"] = day
        st["purchase_num"] = 0
        st["entries_today"] = 0
    st.setdefault("purchase_num", 0)
    st.setdefault("entries_today", 0)
    return st


# One entry per day, user-reported 2026-09-11 ("there's only 1 entry a day so
# there's no multiple clear option at a time"). Corroborated by master.mdb:
# legend_race_billing is frequency 0, i.e. the entry cannot be bought back
# either -- see _billing. So a periodic legend race is one run per reset, on
# the same daily_races._served_day() clock everything else here rolls on.
_ENTRIES_PER_DAY = 1


def _index_boss(row) -> dict:
    """The boss as legend_race/index shows it: a standalone profile card, not
    a race entrant.

    Capture-validated. daily_races._boss_chara + _race_horse_array already
    produce 36 of this row's 47 fields byte-identical to the real capture
    (every stat, every aptitude, the skill array, card_id, rarity,
    race_dress_id). The 11 that differ are all RACE context the index has
    none of, and are overridden below: no gate, no running style, no
    popularity or motivation, and the two chara-id fields carry the BOSS NPC
    id rather than the chara id.

    KNOWN one-field discrepancy: final_grade. The only boss we have ground
    truth for (npc 197) reports 12 on the real server and 11 from
    _rank_for_stats' rating->rank derivation. One sample is not enough to
    re-derive the mapping for the other 48 bosses, so the derived value is
    kept -- it is a cosmetic grade label on the boss card, not a race input.

    The projection itself lives in daily_races.index_boss_data because BOTH
    index screens need it: dump.cs declares boss_data as a RaceHorseData in
    DailyLegendRaceData and LegendRaceData alike, and the daily one was
    serving a two-field stub until 2026-09-11.
    """
    return daily_races.index_boss_data(row["legend_race_boss_npc_id"])


def _record(row, st: dict) -> dict:
    """One LegendRaceData: {legend_race_id, is_cleared, is_played, boss_data}."""
    rid = row["id"]
    return {"legend_race_id": rid,
            "is_cleared": 1 if rid in st["cleared"] else 0,
            "is_played": 1 if rid in st["played"] else 0,
            "boss_data": _index_boss(row)}


@registry.endpoint("legend_race/index")
def handle_index(payload: dict) -> dict:
    """legend_race/index -- the event's top screen. Request carries nothing.

    Capture-exact shape: {legend_race_record_array, purchase_num,
    update_item_array, legend_race_mission_num}.

    legend_race_mission_num is the claimable-mission badge. The real server
    moved it 0 -> 4 once the race was cleared, but Global's master.mdb has NO
    mission_data rows for legend races at all (the mission ids the client
    names, e.g. 100540 "Win 1 Legend Race", resolve in text_data and are
    absent from mission_data), so there is nothing here to count. Served as
    0 rather than inventing a badge that opens an empty list.

    update_item_array was [] in both captures; it reports item balances the
    index itself changed, and opening a screen changes none.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    races = active_races()
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "legend_race_record_array": [_record(r, st) for r in races],
        "purchase_num": st["purchase_num"],
        "update_item_array": [],
        "legend_race_mission_num": 0,
    })


def _preview_rewards(row) -> list:
    out = []
    for prefix in ("first_clear", "pick_up"):
        for i in (1, 2, 3):
            cat = row[f"{prefix}_item_category_{i}"] or 0
            iid = row[f"{prefix}_item_id_{i}"] or 0
            num = row[f"{prefix}_item_num_{i}"] or 0
            if not iid or num <= 0:
                continue
            if cat == daily_races._ITEM_TYPE_PIECE:
                iid = daily_races._piece_id_for_card(iid)
            out.append({"item_type": cat, "item_id": iid, "item_num": num})
    return out


@registry.endpoint("legend_race/get_reward_list")
def handle_get_reward_list(payload: dict) -> dict:
    """legend_race/get_reward_list: {legend_race_id} -> {reward_array}.

    SYNTHESIZED-UNVERIFIED body (dump.cs gives only `RaceRewardData[]
    reward_array`). Read-only reward PREVIEW -- the rewards panel opened from
    the race card -- so it must grant nothing. Lists the master row's real
    first_clear and pick_up items; the odds-driven drop/victory extras cannot
    be listed because their tables are not in Global's master.mdb.
    """
    row = _row(payload.get("legend_race_id"))
    if row is None:
        return _refuse()
    return _ok({"reward_array": _preview_rewards(row)})


# ---------------------------------------------------------------------------
# the race itself

@registry.endpoint("legend_race/race_entry")
def handle_race_entry(payload: dict) -> dict:
    """legend_race/race_entry: {legend_race_id, trained_chara_id} -> the
    paddock lineup.

    Request shape is capture-confirmed (20260903_204408 tx 8, a 217). The
    response mirrors daily_races' race_entry field-for-field because dump.cs
    gives the two the same CommonResponse field set -- including the
    race_instance_id the paddock background loader null-refs without (see
    daily_races._do_race_entry's own note).

    Refuses a race that is not currently open, so a stale client cannot enter
    a closed event.
    """
    viewer_id = payload["viewer_id"]
    race_id = payload.get("legend_race_id")
    row = _row(race_id)
    if row is None or not any(r["id"] == race_id for r in active_races()):
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    # One run per daily reset (see _ENTRIES_PER_DAY). A run already in flight
    # is not a second entry -- re-entering the one you are in is how the
    # client recovers from a drop, and refusing it would strand the player.
    if not st.get("pending") and st["entries_today"] >= _ENTRIES_PER_DAY:
        return _refuse()
    player = daily_races._find_veteran(viewer_id, payload.get("trained_chara_id"))
    if player is None:
        return _refuse()
    opponents = daily_races._legend_opponents(row, viewer_id)
    if not opponents:
        return _refuse()

    horses = [copy.deepcopy(player)] + opponents
    season = (row["season"] or 1)
    weather = (row["weather"] or 1)
    ground = (row["ground"] or 1)
    course = daily_races.race_simulator.get_course_for_race_instance(row["race_instance_id"])
    entry_num = course[2] if course else len(horses)
    gates = random.sample(range(max(entry_num, len(horses))), len(horses))
    wire_horses = daily_races._race_horse_array(horses, gates)
    seed = random.randint(0, 2**31 - 1)
    st["pending"] = {
        "race_id": race_id,
        "trained_chara_id": payload.get("trained_chara_id"),
        "item_id_array": [],
        "result_rank": None,
        "season": season, "weather": weather, "ground": ground,
        "gate_assignment": gates,
        # Kept so resume can hand back the SAME paddock it served here rather
        # than rebuilding a different one -- see handle_resume.
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "race_horse_data_array": copy.deepcopy(wire_horses),
        "race_scenario": None,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_horse_data_array": wire_horses,
        "season": season,
        "weather": weather,
        "ground_condition": ground,
        "random_seed": seed,
        "race_instance_id": row["race_instance_id"],
        "state": 1,
        "trained_chara_id": payload.get("trained_chara_id"),
    })


@registry.endpoint("legend_race/reflect_item_effect")
def handle_reflect_item_effect(payload: dict) -> dict:
    """legend_race/reflect_item_effect: {item_id_array} -> the paddock after
    the pre-race items are applied, AND where the entry ticket is spent.

    The ticket-spend pairing is not a guess: the daily family's real captures
    show item_info_array reporting the ticket balance decrementing across
    exactly this call (5 -> 4 -> 3) with an empty item_id_array, and the
    client's own paddock method is named StartItemSelectAndUseTicket(). Same
    method, same place, different ticket -- item 97 here, cost_num from the
    master row.

    Refuses when the ticket cannot be paid, rather than running a free race.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    row = _row(pending["race_id"])
    if row is None:
        return _refuse()

    cost = row["cost_num"] or 1
    if daily_races._item_count(full_state, TICKET_ITEM_ID) < cost:
        return _refuse()
    daily_races._add_item(full_state, TICKET_ITEM_ID, -cost)
    # The day's entry is counted HERE, where the ticket is actually spent --
    # not at race_entry. Entering the paddock and backing out (reset) costs
    # neither the ticket nor the day; committing to the race costs both.
    if not pending.get("counted"):
        pending["counted"] = True
        st["entries_today"] = st.get("entries_today", 0) + 1
    pending["item_id_array"] = [i for i in (payload.get("item_id_array") or [])
                                if isinstance(i, int) and not isinstance(i, bool)]
    state_store.save_state(viewer_id, full_state)
    # weather/ground_condition/race_horse_data_array stayed null in every real
    # capture of the daily twin -- the paddock already has them from
    # race_entry, and re-sending would let them disagree.
    return _ok({
        "weather": None,
        "ground_condition": None,
        "race_horse_data_array": None,
        "item_info_array": [{"item_id": TICKET_ITEM_ID,
                             "number": daily_races._item_count(full_state, TICKET_ITEM_ID)}],
        "state": 2,
    })


@registry.endpoint("legend_race/race_start")
def handle_race_start(payload: dict) -> dict:
    """legend_race/race_start: {running_style, is_short} ->
    {race_scenario, state, running_style}, exactly those three and nothing
    nested (dump.cs LegendRaceRaceStartResponse.CommonResponse, and the daily
    twin's real capture proved the same three there).
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending:
        return _refuse()
    row = _row(pending["race_id"])
    player = daily_races._find_veteran(viewer_id, pending["trained_chara_id"])
    if row is None or player is None:
        return _refuse()

    player = dict(player)
    if payload.get("running_style"):
        player["running_style"] = payload["running_style"]
    outcome = daily_races._run_race(
        player, daily_races._legend_opponents(row, viewer_id),
        row["race_instance_id"], pending["season"], pending["weather"],
        pending["ground"], gate_assignment=pending.get("gate_assignment"))
    pending["result_rank"] = outcome["rank"]
    pending["race_scenario"] = outcome["race_scenario"]   # replayed by resume
    state_store.save_state(viewer_id, full_state)
    log.info("legend_race/race_start: race=%s player=%s rank=%d simulated=%s",
             pending["race_id"], pending["trained_chara_id"], outcome["rank"],
             outcome["sim_results"] is not None)
    return _ok({
        "race_scenario": outcome["race_scenario"],
        "state": 4,
        "running_style": payload.get("running_style") or player.get("running_style") or 1,
    })


def _merge_summary(summary: dict) -> None:
    """Collapse repeated ids in reward_summary_info, in place.

    A win pays from two arrays at once -- first_clear and victory -- and both
    can name the same thing (id 53's first clear pays Monies, and so does the
    victory bonus), which left the summary carrying two separate item-59
    lines. The real server aggregates per id in first-seen order instead:
    capture-proven on the present box, where five separate 50,000-Monies
    presents came back as one {item_id: 110, number: 250000}. The running
    total the client ends up with is the same either way, but the reward
    popup reads off these entries, so two lines for one item shows the player
    the same reward twice.
    """
    for key, id_field, num_field in (("add_item_list", "item_id", "number"),
                                     ("add_piece_list", "piece_id", "piece_num")):
        merged = []
        for entry in summary.get(key) or []:
            prev = next((e for e in merged if e[id_field] == entry[id_field]), None)
            if prev is None:
                merged.append(entry)
            else:
                prev[num_field] += entry[num_field]
        summary[key] = merged


def _award(full_state: dict, st: dict, row, rank: int) -> dict:
    """Pay out one finished legend race.

    Split across the three arrays dump.cs declares for THIS family -- note it
    is not the daily family's shape: LegendRaceReplayCheckResponse has
    first_clear_reward_array / drop_reward_array / victory_reward_array,
    where the daily one has normal/rare/bonus. Mapping them onto the master
    row:

      first_clear -> first_clear_item_*, once ever, on a win. Real data.
      drop        -> pick_up_item_*, every run, win or lose. Real data, and
                     "drop" is what a per-run pick-up is.
      victory     -> a win-only bonus. SYNTHESIZED: this is the one the
                     absent victory_reward_odds table would have filled, so
                     it pays flat Monies rather than a fabricated odds roll.

    Paying the drop on a loss is deliberate and follows the feature's own
    design -- a periodic legend race rewards the attempt, not only the win.
    """
    summary = daily_races._empty_summary()
    rid = row["id"]
    first_clear, victory = [], []

    drop = daily_races._grant_row_items(full_state, row, "pick_up", summary)
    if rank == 1:
        if rid not in st["cleared"]:
            first_clear = daily_races._grant_row_items(
                full_state, row, "first_clear", summary)
            st["cleared"].append(rid)
        daily_races._grant(full_state, daily_races._ITEM_TYPE_MONEY, 59,
                           _VICTORY_MONEY, summary)
        victory = [{"item_type": daily_races._ITEM_TYPE_MONEY, "item_id": 59,
                    "item_num": _VICTORY_MONEY}]
    if rid not in st["played"]:
        st["played"].append(rid)
    _merge_summary(summary)
    return {"first_clear_reward_array": first_clear,
            "drop_reward_array": drop,
            "victory_reward_array": victory,
            "reward_summary_info": summary}


@registry.endpoint("legend_race/replay_check")
def handle_replay_check(payload: dict) -> dict:
    """legend_race/replay_check: {race_result_array} -> the results screen.

    add_trophy_info / trophy_reward_info are served as null: dump.cs declares
    them, but no trophy data for legend races exists in Global's master.mdb,
    and a fabricated trophy would show the player an award that no other
    screen can ever resolve. Null is the honest "no trophy this run".
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending")
    if not pending or pending.get("result_rank") is None:
        return _refuse()
    row = _row(pending["race_id"])
    if row is None:
        return _refuse()

    rank = pending["result_rank"]
    reward = _award(full_state, st, row, rank)
    st.pop("pending", None)
    shop_info = limited_shop.roll(full_state, "legend_race")
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "rank": rank,
        "first_clear_reward_array": reward["first_clear_reward_array"],
        "drop_reward_array": reward["drop_reward_array"],
        "victory_reward_array": reward["victory_reward_array"],
        "reward_summary_info": reward["reward_summary_info"],
        "state": 0,
        "add_trophy_info": None,
        "trophy_reward_info": None,
        "limited_shop_info": shop_info,
    })


# ---------------------------------------------------------------------------
# tickets and run control

@registry.endpoint("legend_race/recovery_ticket")
def handle_recovery_ticket(payload: dict) -> dict:
    """legend_race/recovery_ticket: {client_coin_num} -> buy an entry ticket
    for carats. {coin_info, reward_summary_info, purchase_num}.

    client_coin_num is the client's OWN idea of its balance and is never
    trusted -- the charge is validated against stored state, like every other
    spend on this server.

    Both the cap and the price come from master.mdb `legend_race_billing`
    (see _billing). In this build that row is frequency 0 / pay_cost 0, so
    every call refuses: a periodic legend race sells no extra entries at all.
    The row is still read rather than the refusal hardcoded, so a build that
    does open purchases needs no edit here.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    frequency, cost = _billing()
    if frequency <= 0 or st["purchase_num"] >= frequency:
        return _refuse()

    from . import shop
    if cost and shop.spend_carats(full_state, cost) is None:
        return _refuse()
    summary = daily_races._empty_summary()
    daily_races._grant(full_state, _ticket_category(), TICKET_ITEM_ID,
                       _TICKET_PER_PURCHASE, summary)
    st["purchase_num"] += 1
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "coin_info": copy.deepcopy(full_state.get("coin_info_state")),
        "reward_summary_info": summary,
        "purchase_num": st["purchase_num"],
    })


@registry.endpoint("legend_race/reset")
def handle_reset(payload: dict) -> dict:
    """legend_race/reset -> {state}. Request carries nothing.

    Abandons an in-flight race: the client calls this to get out of a run it
    entered but did not finish. The ticket is NOT refunded -- it was spent at
    reflect_item_effect, and refunding it here would make a free retry loop
    out of entering and backing out.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    st.pop("pending", None)
    state_store.save_state(viewer_id, full_state)
    return _ok({"state": 0})


@registry.endpoint("legend_race/resume")
def handle_resume(payload: dict) -> dict:
    """legend_race/resume -> the whole in-flight race. Request carries nothing.

    TEN fields, not one:
      {race_horse_data_array, season, weather, ground_condition, random_seed,
       race_instance_id, race_scenario, state, purchase_num, is_cleared}

    BUG FIXED 2026-09-11 (live-reported: "the UI of starting the legend race
    is extremely broken, buttons everywhere, and the text malformed"). This
    used to answer {state} alone, by analogy with reset -- whose
    CommonResponse really is just {state}. Resume's is not. The other nine
    fields deserialized to null/0, and a null race_horse_data_array on the
    screen that is rebuilding the race is exactly the sort of thing that
    leaves pooled prefab buttons stranded and text unformatted, the same
    missing-key-reads-as-null failure as card/get_release_card_array.

    The paddock is REPLAYED from what race_entry already served, not rebuilt:
    _legend_opponents rerolls its field off a per-run seed, so rebuilding
    here would resume the player into a different set of horses than the one
    they were looking at.

    An empty array is served when nothing is pending -- never null, for the
    same reason.
    """
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    st = _state(full_state)
    pending = st.get("pending") or {}
    if not pending:
        state = 0
    elif pending.get("result_rank") is not None:
        state = 4
    else:
        state = 2
    race_id = pending.get("race_id")
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "race_horse_data_array": copy.deepcopy(
            pending.get("race_horse_data_array") or []),
        "season": pending.get("season") or 0,
        "weather": pending.get("weather") or 0,
        "ground_condition": pending.get("ground") or 0,
        "random_seed": pending.get("random_seed") or 0,
        "race_instance_id": pending.get("race_instance_id") or 0,
        "race_scenario": pending.get("race_scenario"),
        "state": state,
        "purchase_num": st["purchase_num"],
        "is_cleared": 1 if race_id in st["cleared"] else 0,
    })
