"""GRAND LIVE (scenario 3) -- the two endpoints that have no URA counterpart.

single_mode_live/master_square (take a lesson) and single_mode_live/live_start
(perform a Live). Every OTHER endpoint Grand Live posts to reuses the shared
career handlers verbatim; main.py aliases them onto this scenario's
endpoint_prefix, so only these two are declared here (see the scenario's
extra_endpoints()).

They lived in handlers/single_mode_team.py, which meant an 11k-line file that
serves every scenario also carried one scenario's private flows. The shared
helpers they need are still there and are reached through _smt() -- a deferred
import, because single_mode_team imports the scenario registry and a top-level
import here would close that cycle.
"""

from __future__ import annotations

import copy
import logging

from ... import career_events
from ... import event_engine
from . import impl as grand_live
from . import producers

log = logging.getLogger("uma-server")


def _smt():
    """handlers.single_mode_team, imported on use. See the module docstring."""
    from ...handlers import single_mode_team
    return single_mode_team


# The two endpoints with no URA counterpart. Everything else Grand Live sends
# reuses the handlers above verbatim (see main.HANDLERS).

# Fields the captured master_square / live_start responses do NOT carry. Our
# training-state seed is a check_event capture, which does -- serving the extra
# race fields on a lesson would be inventing wire the real server never sends.
_LIVE_ONLY_DROP = ("race_condition_array", "race_start_info", "race_running_style",
                   "event_effected_factor_array", "not_down_parameter_info")


def _grand_live_envelope(full_state: dict, career: dict, drop=()) -> dict:
    """A training-state response for a Grand Live career: the player's live
    chara_info + home_info, no events. live_data_set is added later by
    sync_chara_info, the same as on every other response."""
    smt = _smt()
    response = smt._load_ura_race_seed("train_check_event")
    data = response["data"]
    chara_info = career["data"]["chara_info"]
    # Preserve the two Grand Live states. Forcing playing_state back to 1 here
    # would close the Live screen the moment the player took a backstage lesson
    # (master_square answers through this same envelope), and would undo the
    # "chain still running" 5 that live_start sets for the concert's result
    # screen.
    if chara_info.get("playing_state") not in (grand_live.PLAYING_STATE_BACKSTAGE,
                                               grand_live.PLAYING_STATE_EVENT_CHAIN):
        chara_info["playing_state"] = 1
    data["chara_info"] = copy.deepcopy(chara_info)
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        data["home_info"] = copy.deepcopy(career_home)
    data["unchecked_event_array"] = []
    for key in drop:
        data.pop(key, None)
    return response


def handle_master_square(payload: dict) -> dict:
    """single_mode_live/master_square {square_id, current_turn} -- take a lesson.

    Spends the performance tokens, applies the square's Practice Bonus (stats /
    energy / skill hint / Extra Stat Gain) or learns its song, then REFRESHES
    the three offers -- the list refreshes on completion, which is what lets a
    player take several lessons in one turn.

    An unaffordable or unknown square is refused with result_code 205 rather
    than silently granted: the client re-reads live_data_set from the response
    either way, so a refusal simply leaves the board as it was."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    chara_info = career["data"]["chara_info"]
    st = grand_live.state(full_state)
    square_id = payload.get("square_id")

    # No square_id at all = "reserve nothing / just show me the board" (the
    # client sends a bare {current_turn} after a Live). Serve current state.
    if square_id:
        try:
            result = grand_live.apply_square(st, chara_info, square_id)
            log.info("grand live lesson %s -> %s", square_id, result)
        except ValueError as exc:
            log.warning("grand live lesson %s refused: %s", square_id, exc)
            return {"response_code": 1,
                    "data_headers": {"result_code": 205, "notifications": {}},
                    "data": {}}

    career["data"]["chara_info"] = chara_info
    # An Extra Stat Gain song changes every future training preview, so the
    # command_info the client is holding is now stale -- rebuild it here rather
    # than making the player wait a turn for the bonus to show up.
    career_home = career["data"].get("home_info")
    if isinstance(career_home, dict):
        smt._refresh_command_info(
            chara_info, career_home, turn=chara_info.get("turn", 1),
            unlocked_npcs=[n[0] for n in
                           full_state.get(smt.single_mode_events.UNLOCKED_NPCS_KEY, [])],
            facility_levels=smt._facility_levels(career["data"]),
            race_history=career["data"].get("race_history", []),
            training_bonus=smt._training_bonus(full_state, chara_info),
            friendship_bonus=smt._friendship_bonus(full_state, chara_info),
            specialty_bonus=smt._specialty_bonus(full_state, chara_info),
            support_card_levels=smt._support_card_levels(full_state),
            friendship_stacks=smt._friendship_stacks(full_state),
            full_state=full_state)
    response = _grand_live_envelope(full_state, career, drop=_LIVE_ONLY_DROP)
    smt.state_store.save_state(viewer_id, full_state)
    return response


# The event each Live hands back when it finishes -- capture-confirmed, one per
# live_type. The client's flow is: check_event plays "The Nth Concert Begins!"
# (see _GRAND_LIVE_EVENTS) -> the Live is performed -> live_start returns the
# matching "Concert Ends!" beat for the client to resolve.
_LIVE_END_EVENTS = {
    1:  (202006, 400003441),   # The First Concert Ends!
    2:  (202012, 400003443),   # The Second Concert Ends!
    3:  (202020, 400003446),   # The Third Concert Ends!
    4:  (202027, 400003447),   # The Fourth Concert Ends!
    # Grand Finale. The finale is the ONE Live whose ends-beat varies: across
    # 38 real careers that played it, the ack is 202041 x32, 202046 x3, and
    # 202044/202045/202049 once each. What selects the variant is NOT known --
    # it tracks neither the trainee (1041 and 1026 each appear under two
    # different variants) nor the career outcome. 202046 came from the single
    # capture this table was read off and is the rare one; serve the modal id.
    # producers.GRAND_CONCERT_ENDS_EVENTS accepts every variant either way, so
    # whichever arrives still releases the held turn.
    #
    # master.mdb single_mode_story_data (whose `id` IS the event id for these
    # scenario beats) shows the finale beats are TWO groups of five sharing a
    # short_story_id -- 202041-202045 -> 400003417 and 202046-202050 ->
    # 400003418 -- so the story has to move with the id: 34 of the 38 acks are
    # in the 417 group, 32 of those on 202041.
    10: (202041, 400003417),
}

# live_start answers with a REDUCED envelope -- only these four keys, verified
# identical across all five Lives in the capture (no home_info, no race fields,
# no not_up_parameter_info).
_LIVE_START_KEYS = ("chara_info", "unchecked_event_array", "live_data_set", "add_music")


def handle_live_start(payload: dict) -> dict:
    """single_mode_live/live_start {current_turn} -- perform this turn's Live.

    Pays out (25 SP per song + 5 per technique taken this segment; all stats
    +10 for three or more songs, +3 below that), brings every dormant Live
    Bonus online, raises the token caps, resets the lesson pattern, and hands
    back the "Concert Ends!" event for the client to play.

    The request carries ONLY current_turn -- all five Lives, including the Grand
    Live (the capture's five live_start requests are byte-identical in shape).
    Idempotent per turn: paying twice would double every Live in the run."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    chara_info = career["data"]["chara_info"]
    turn = payload.get("current_turn") or chara_info.get("turn")
    # Tolerates our exec_command having already advanced the turn past the Live
    # turn -- see grand_live.pending_live_for.
    live_type = grand_live.pending_live_for(full_state, turn)

    if live_type is not None:
        live_turn = grand_live.live_turn_of(live_type) or turn
        result = grand_live.perform_live(full_state, chara_info, live_turn)
        log.info("grand live: LIVE %s on turn %s -> %s", live_type, turn, result)
        # The Grand Live itself awards Girls' Legend U, which is why it is
        # excluded from the 18-song count the event before it gates on.
        if live_type == grand_live.GRAND_LIVE_TYPE:
            grand_live.grant_free_song(full_state, grand_live.GIRLS_LEGEND_U_LIVE_ID,
                                       chara_info=chara_info)
        career["data"]["chara_info"] = chara_info
    # Leave the backstage state -- but into 5 ("chain still running"), NOT 1.
    # The capture's live_start response carries 5, because the Concert Ends beat
    # and the after-the-live beats have yet to play. Answering 1 declares the
    # turn finished while events are still pending, and the client then skips
    # the concert's own result screen.
    if chara_info.get("playing_state") == grand_live.PLAYING_STATE_BACKSTAGE:
        chara_info["playing_state"] = grand_live.PLAYING_STATE_EVENT_CHAIN
        career["data"]["chara_info"] = chara_info

    response = _grand_live_envelope(full_state, career)
    data = response["data"]
    for key in [k for k in data if k not in _LIVE_START_KEYS]:
        del data[key]
    data["add_music"] = None      # null when no jukebox track is awarded

    # REPORT THE LIVE'S OWN TURN. The official server holds chara_info.turn at
    # the turn being played until that turn's event chain finishes, so its
    # live_start response says 24; ours already advanced to 25 in exec_command.
    # The served value is corrected here, on this response only -- the persisted
    # turn is left alone, because moving the whole turn-advance to end-of-chain
    # is a core-loop change that touches every turn-keyed schedule in both
    # scenarios and deserves its own pass against the baseline.
    if live_type is not None:
        live_turn = grand_live.live_turn_of(live_type)
        if live_turn:
            data["chara_info"]["turn"] = live_turn

    end = _LIVE_END_EVENTS.get(live_type)
    if end:
        event_id, story_id = end
        ev = {"choices": [{"effects": []}]}
        entry = event_engine.career_event_entry(
            ev, event_id, story_id,
            # chara_id 0 -- a scenario cutscene, not a character event (capture).
            chara_id=0,
            support_card_id=0, play_timing=producers.POST_LIVE_TIMING)
        data["unchecked_event_array"] = [entry]
        smt._push_career_ctx(full_state, {
            "event_id": event_id, "story_id": story_id,
            "source": "inline", "source_id": 0,
            "title": event_engine.event_title(story_id) or "",
            "trainee_card_id": chara_info.get("card_id"), "event": ev})

        # The turn's AFTER-THE-LIVE beats chain behind Concert Ends. They are
        # deliberately withheld from the turn's normal chain, because resolving
        # "Concert Begins" has to leave the chain EMPTY -- that gap is what makes
        # the client open the backstage screen (lessons + the concert button).
        # Keyed on the LIVE's own turn, not the reported one -- the reported turn
        # may already have advanced past it (see pending_live_for), which would
        # find no beats and leave the player with no post-concert events at all.
        beats_turn = grand_live.live_turn_of(live_type) or turn
        for beat_id, beat_story, n_choices, timing in \
                producers.post_live_beats(beats_turn):
            career_events.emit(full_state, career_events.Event(
                event_id=beat_id, story_id=beat_story, play_timing=timing,
                choices=[career_events.Choice(effects=[])
                         for _ in range(n_choices)],
                chara_id=0,
                once_key=f"gl:{beat_id}", source="grand_live_post_live",
                priority=career_events.PRIO_SCENARIO))

    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_reserve_square(payload: dict) -> dict:
    """single_mode_live/reserve_square {square_id, current_turn} -- pin one of
    the three offers as the 予約 (reserved) lesson, so it survives the board
    re-roll that every completed lesson triggers.

    `reserve_square_id` was already modelled and already published in
    live_data_set (impl.attach) and already cleared when the reserved lesson is
    taken (apply_square) -- the ONE missing piece was the endpoint that sets it,
    so the reserve button did nothing. Live-hit 6x in server.log against the
    no-op fallback before this existed.

    SingleModeLiveReserveSquareResponse.CommonResponse declares NO fields, so a
    data-less envelope is the structurally correct answer here -- unlike
    lottery_square below, this one does not re-publish live_data_set.

    square_id 0 (or an id not on the board) clears the reservation, which is how
    the client un-reserves."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    st = grand_live.state(full_state)
    square_id = int(payload.get("square_id") or 0)
    board = [int(s) for s in (st.get("board") or [])]
    st["reserve_square_id"] = square_id if square_id in board else 0
    log.info("grand live reserve square %s -> %s (board %s)",
             square_id, st["reserve_square_id"], board)
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1, "data_headers": {"result_code": 1, "notifications": {}},
            "data": {}}


def handle_lottery_square(payload: dict) -> dict:
    """single_mode_live/lottery_square {square_id, current_turn} -- re-roll the
    lesson board, keeping the reserved offer.

    INFERRED, not capture-backed. What IS known from dump.cs: the client sends
    one square_id (SendSingleModeLiveLotterySquareRequest(int squareId)) and the
    response carries live_data_set and nothing else -- i.e. the call's whole
    point is that the BOARD changes. That plus `reserve_square_id` existing at
    all (a reservation is only meaningful if something can otherwise sweep the
    board away) reads as a re-roll, with the named square the one being swapped
    out. It has never appeared in a capture or in server.log, so the mechanic
    is a strong guess and the paid/free question is unanswered -- nothing is
    charged here.

    Deliberately conservative: this only touches `board`, which apply_square
    already re-rolls wholesale after every single lesson, so a wrong guess about
    the trigger cannot corrupt tokens, songs or stats. The reserved square is
    carried across the re-roll, which is the one behaviour a reservation has to
    have for it to mean anything."""
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return {"response_code": 1,
                "data_headers": {"result_code": 205, "notifications": {}}, "data": {}}

    chara_info = career["data"]["chara_info"]
    st = grand_live.state(full_state)
    reserved = int(st.get("reserve_square_id") or 0)
    st["board"] = grand_live.reroll_board(st, chara_info, keep=reserved)
    log.info("grand live lottery square %s -> board %s (kept %s)",
             payload.get("square_id"), st["board"], reserved)
    response = _grand_live_envelope(full_state, career, drop=_LIVE_ONLY_DROP)
    smt.state_store.save_state(viewer_id, full_state)
    return response
