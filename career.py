"""Master career tool for the private Umamusume server -- one place to move a
run to any turn, keep every turn-dependent piece of state consistent with it,
inspect/undo/redo career events, and edit any career property by path.

admin.py stays the general account cheat CLI (wallet, cards, roster, mail);
THIS tool owns the in-progress career: turn, goals, race history, the event
ledger and the training screen's unlock state.

Usage (run from anywhere; --viewer picks the account):

    python career.py show                 # everything about the active career
    python career.py goals                # route goals + cleared/pending
    python career.py races                # race history
    python career.py events               # active / queued / fired ledger
    python career.py props                # editable property aliases

  TURN
    python career.py set-turn 45          # move to turn 45, reconcile all state
    python career.py set-turn 45 --fill-races          # ...and count every goal
                                                       #    race before 45 as WON
    python career.py set-turn 45 --fill-races --with-rewards   # + pay their
                                                       #    stat/SP rewards
    python career.py set-turn 12 --reset-fired         # rewind AND wipe the
                                                       #    non-turn-keyed
                                                       #    fired sets
    python career.py set-turn 60 --keep-races          # don't prune history
    python career.py set-turn 60 --keep-events         # don't drop the queue

  PROPERTIES  (dotted paths into the career's own `data`, JSON values;
               `--full-state` addresses the whole per-viewer state instead)
    python career.py get chara_info.speed
    python career.py get                       # the whole career data tree
    python career.py set speed 900             # alias -> chara_info.speed
    python career.py set chara_info.speed 900  # ...the same thing
    python career.py set fans +50000           # relative (+/-) on numbers
    python career.py set chara_info.proper_ground_dirt 8      # aptitude S
    python career.py set chara_info.skill_array '[{"skill_id":200011,"level":1}]'
    python career.py del chara_info.foo
    python career.py set --full-state --create unlocked_versus_npcs '[[102,1],[103,1]]'
                                               # --create: the key isn't there yet

  EVENTS  (the career_events pipeline: one queue, one fired set)
    python career.py events --fired
    python career.py event-undo --turn 30      # un-fire everything from turn 30 on
    python career.py event-undo duel:31 rest:31:7009
    python career.py event-done gl:202002      # mark fired without playing it
    python career.py event-drop --all          # clear queue + active event
    python career.py event-queue 10002 --story 400000040 --choices 2

  RACES
    python career.py race-add 1005 --rank 1    # record program 1005 as won
    python career.py race-del --from-turn 40   # drop history from turn 40 on

  MAINTENANCE
    python career.py reconcile                 # recompute every derived piece
    python career.py unlock --all              # force-enable this turn's commands

WHAT A REWIND UNDOES: the turn-keyed event ledger, race history at/after the
target turn and the fans those races paid, the career-FAILED marker, and every
derived snapshot below. It does NOT roll back stats/SP/skills/bonds gained on
those turns -- nothing in the career gates on those the way fans and race
history gate goals, so subtracting them would be invention, not restoration.
Edit them directly (`career.py set speed 600`) if you want them rolled back.

WHY THE RECONCILE STEP EXISTS (admin.py learned it the hard way): turn is not
a scalar the client re-derives. home_info's command lock state, the goal
banner's cleared count, the class (chara_grade), the race-condition list and
the event ledger are all persisted snapshots that only ever get rebuilt as a
side effect of a real exec_command/race_out. Writing chara_info.turn alone
leaves every one of them describing the OLD turn -- a fully locked training
screen on a normal turn being the loudest symptom. Every mutating command here
ends in _reconcile(), which rebuilds all of them the same way the server's own
turn-advance path does.
"""

import argparse
import json
import os
import re
import sys

# The server package lives next to this file (same resolution as admin.py, so
# a Windows checkout at the old path still works).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(_HERE, "server"),
                   r"c:\Users\Systemless\Documents\private server\server"):
    if os.path.isdir(_candidate):
        sys.path.insert(0, _candidate)
        break

from app import career_events as CE                     # noqa: E402
from app import event_engine                            # noqa: E402
from app import master_data                             # noqa: E402
from app import state as state_store                    # noqa: E402
from app.handlers import career_producers as producers  # noqa: E402
from app.scenarios.grand_live import impl as GL          # noqa: E402
from app.handlers import single_mode_events as E        # noqa: E402
from app.handlers import single_mode_team as T          # noqa: E402

DEFAULT_VIEWER = "802445340143"
STATS = ("speed", "stamina", "power", "guts", "wiz")
CAREER_END_TURN = 78

# chara_info.state, from the server's own writers: 0 in progress, 2 career
# failed (missed goal / finals loss), 3 completed (turn-78 finals win).
STATE_NAMES = {0: "in progress", 2: "FAILED", 3: "completed"}


# ============================================================== plumbing ===

def _load(viewer):
    full_state = state_store.get_state(viewer)
    if not full_state:
        sys.exit(f"no state for viewer {viewer}")
    return full_state


def _career(full_state):
    career = full_state.get(T.STATE_KEY)
    if not isinstance(career, dict) or not isinstance(career.get("data"), dict):
        sys.exit("no active career -- start one in-game first "
                 "(or `python admin.py career-resume <slot>`)")
    return career


def _save(viewer, full_state):
    state_store.save_state(viewer, full_state)


# ================================================== derived-state rebuild ===

def _transient_keys() -> tuple:
    """Contexts describing an action IN FLIGHT (a race being run, an event on
    screen, a rolled-but-unapplied rest/hint/duel outcome). None of them
    survive a turn move: they name a turn or event that no longer applies, and
    a stale one hijacks the next check_event -- which is why admin.py's
    set-turn already had to drop RACE_CTX_KEY by hand."""
    # RESOLVED BY NAME, and a name that no longer exists is skipped rather than
    # raising. These constants live in modules that get refactored (the
    # scenario split moved several), and this tool is the thing you reach for
    # when a career is already wedged -- having it die on an AttributeError for
    # one stale name is the worst possible time to find out. T.RACE_FLOW_STATE_KEY
    # was exactly that: removed upstream, and it took set-turn down with it.
    names = [
        (T, "RACE_CTX_KEY"), (T, "RACE_FLOW_STATE_KEY"), (T, "RACE_REWARD_KEY"),
        (T, "DISPLAY_EVENT_KEY"), (T, "GOAL_EVENT_KEY"), (T, "APPRAISAL_CTX_KEY"),
        (T, "HINT_REVEAL_CTX_KEY"), (T, "REST_CTX_KEY"), (T, "INFIRMARY_CTX_KEY"),
        (T, "RAFFLE_CTX_KEY"), (T, "INSPIRATION_CTX_KEY"), (T, "CRANE_CTX_KEY"),
        (T, "PAL_EVENT_CTX_KEY"), (T, "CAREER_CTX_QUEUE_KEY"), (T, "STYLE_CHOICE_KEY"),
        (event_engine, "CAREER_EVENT_CTX_KEY"),
        (E, "FAIL_CTX_KEY"), (E, "DUEL_CTX_KEY"),
        (E, "PENDING_EVENTS_KEY"), (E, "EXTRA_EVENTS_KEY"),
    ]
    keys = [getattr(mod, name) for mod, name in names if hasattr(mod, name)]
    # Every registered scenario's own per-turn caches. Unity Cup's partner
    # preview is keyed to a turn and decides who trains with you and whose soul
    # gauge ticks -- carrying one across a turn move pays the OLD turn's roll.
    for scen in _scenario_transient_keys():
        keys.append(scen)
    return tuple(keys)


def _scenario_transient_keys() -> list:
    """The turn-scoped cache keys every registered scenario declares.

    Read off the registry so a new scenario needs no edit here: state_keys
    names the scenario's persistent state, and anything ending in the preview
    suffix is a per-turn roll rather than career-long state."""
    out = []
    try:
        from app import scenarios as _scen
        for scenario in getattr(_scen, "all_scenarios", lambda: [])():
            for key in getattr(scenario, "state_keys", ()) or ():
                if str(key).endswith("_preview"):
                    out.append(key)
    except Exception:
        pass
    return out


def _clear_transient(full_state: dict) -> list:
    gone = [k for k in _transient_keys() if k in full_state]
    for k in gone:
        del full_state[k]
    # Grand Live's "the client is backstage on turn N" hold has no release
    # condition of its own -- left set, every later response keeps reporting
    # that turn (see career_producers' GRAND_CONCERT_ENDS_EVENT note).
    gl = full_state.get(GL.STATE_KEY)
    if isinstance(gl, dict) and gl.get("concert_turn"):
        gl["concert_turn"] = 0
        gone.append("grand_live.concert_turn")
    return gone


def _refresh_home(full_state: dict, career: dict) -> bool:
    """Rebuild home_info.command_info_array (the training screen's per-command
    lock/preview state) for chara_info.turn, with the SAME arguments the
    server's own turn-advance passes -- Grand Live's live bonuses included, so
    a refreshed screen never disagrees with what the next exec_command
    applies."""
    data = career["data"]
    ci = data["chara_info"]
    home = data.get("home_info")
    if not isinstance(home, dict):
        return False
    unlocked = [n[0] for n in (full_state.get(E.UNLOCKED_NPCS_KEY) or [])]
    T._refresh_command_info(
        ci, home, turn=ci.get("turn", 1), unlocked_npcs=unlocked,
        facility_levels=T._facility_levels(data),
        race_history=data.get("race_history", []),
        training_bonus=T._training_bonus(full_state, ci),
        friendship_bonus=T._friendship_bonus(full_state, ci),
        specialty_bonus=T._specialty_bonus(full_state, ci),
        support_card_levels=T._support_card_levels(full_state),
        friendship_stacks=T._friendship_stacks(full_state),
        full_state=full_state)
    return True


def _reconcile(full_state: dict, career: dict, *, grade: bool = True) -> list:
    """Recompute every piece of career state DERIVED from turn / fans / race
    history, then rebuild the training screen. Safe to run at any time."""
    data = career["data"]
    ci = data["chara_info"]
    turn = ci.get("turn") or 1
    notes = []

    if ci.get("motivation") is not None:
        ci["motivation"] = max(1, min(5, int(ci["motivation"])))
    if ci.get("vital") is not None:
        ci["vital"] = max(0, min(int(ci.get("max_vital") or 100), int(ci["vital"])))

    history = data.get("race_history") or []
    if grade:
        wins = sum(1 for h in history if h.get("result_rank") == 1)
        new_grade = T._career_chara_grade(ci.get("fans", 0) or 0, len(history), wins)
        if new_grade != ci.get("chara_grade"):
            notes.append(f"class {ci.get('chara_grade')} -> {new_grade}")
        ci["chara_grade"] = new_grade

    # Goal banner: which goals are cleared AND due (a passive goal met early is
    # not announced until its deadline turn -- _goals_cleared_count's rule).
    goals = T._route_all_goals(tuple(ci.get("route_race_id_array") or ()))
    announced = sorted({g[1] for g in goals
                        if T._goal_is_cleared(g, ci, history, goals)
                        and not (g[2] in (2, 3) and g[0] > turn)})
    if announced != sorted(full_state.get(T.GOAL_ANNOUNCED_KEY) or []):
        notes.append(f"goals announced -> {announced}")
    full_state[T.GOAL_ANNOUNCED_KEY] = announced
    full_state[T.GOAL_MARKED_KEY] = T._goals_cleared_count(ci, history, turn)

    # Pure Passion's per-condition expiry is turn-keyed; a rewind must not
    # leave a condition immune past its window.
    pp = full_state.get(T.PURE_PASSION_KEY)
    if isinstance(pp, dict):
        kept = {k: v for k, v in pp.items() if int(v or 0) >= turn}
        if kept != pp:
            notes.append("dropped expired pure-passion entries")
        full_state[T.PURE_PASSION_KEY] = kept
    last_duel = full_state.get(E.DUEL_LAST_TURN_KEY)
    if isinstance(last_duel, int) and last_duel >= turn:
        full_state[E.DUEL_LAST_TURN_KEY] = max(0, turn - 1)

    # Rebuilt by _sync_chara_info on the next request too, but a stale list
    # here is what the very next /load would serve.
    if "race_condition_array" in data:
        data["race_condition_array"] = T._build_race_condition_array(
            ci, turn, history)

    if _refresh_home(full_state, career):
        notes.append("training screen recomputed")
    else:
        notes.append("no home_info on this career -- screen not rebuilt")
    return notes


# ============================================= the turn-keyed event ledger ===

# once_keys that carry their turn in the key itself (career_events' dedupe
# identities). Everything else needs a schedule lookup -- see _once_key_turn.
_TURN_IN_KEY = re.compile(
    r"^(duel|rest|hint_reveal|facility_levelup|appraisal|training_failure):(\d+)")


def _gl_beat_turn(event_id: int):
    for turn, beats in producers.GRAND_LIVE_BEATS.items():
        for beat in beats:
            if beat[0] == event_id:
                return turn
    return None


def _ura_fixed_turn(story_id: int):
    for turn, entry in producers.URA_FIXED_BEATS.items():
        if entry[0] == story_id:
            return turn
    return None


def _once_key_turn(key: str):
    """The turn a fired once_key belongs to, or None when it isn't turn-keyed."""
    m = _TURN_IN_KEY.match(key or "")
    if m:
        return int(m.group(2))
    if (key or "").startswith("gl:"):
        try:
            return _gl_beat_turn(int(key.split(":", 1)[1]))
        except ValueError:
            return None
    if (key or "").startswith("ura_fixed:"):
        try:
            return _ura_fixed_turn(int(key.split(":", 1)[1]))
        except ValueError:
            return None
    return None


def _unfire_from(full_state: dict, career: dict, turn: int) -> list:
    """Mark every turn-keyed event at or after `turn` as NOT fired, so a rewind
    lets those turns play out again. Keys whose turn can't be derived are left
    alone (--reset-fired is the blunt instrument for those)."""
    st = CE.state(full_state)
    fired = list(st.get("fired") or ())
    kept, dropped = [], []
    for key in fired:
        t = _once_key_turn(key)
        if t is not None and t >= turn:
            dropped.append(key)
        else:
            kept.append(key)
    st["fired"] = kept

    data = career["data"]
    for key in (T.CHARA_STORY_FIRED_KEY, T.INSPIRATION_FIRED_KEY):
        turns = data.get(key)
        if isinstance(turns, list):
            still = [t for t in turns if int(t) < turn]
            if still != turns:
                dropped += [f"{key}:{t}" for t in turns if int(t) >= turn]
            data[key] = still
    return dropped


def _mark_past_fired(full_state: dict, career: dict, turn: int) -> list:
    """Bookkeeping for a FORWARD jump: record the scheduled beats of the turns
    being skipped as fired.

    This changes no behaviour -- every producer keys on an exact turn, so a
    skipped beat could never fire later anyway -- it only makes `events`
    report a ledger that matches the run's actual history."""
    marked = []
    ci = career["data"]["chara_info"]
    gl_active = GL.is_active(ci)
    for beat_turn, beats in producers.GRAND_LIVE_BEATS.items():
        if beat_turn >= turn or not gl_active:
            continue
        for beat in beats:
            key = f"gl:{beat[0]}"
            if not CE.already_fired(full_state, key):
                CE.mark_fired(full_state, key)
                marked.append(key)
    for beat_turn, entry in producers.URA_FIXED_BEATS.items():
        if beat_turn >= turn or gl_active:
            continue
        key = f"ura_fixed:{entry[0]}"
        if not CE.already_fired(full_state, key):
            CE.mark_fired(full_state, key)
            marked.append(key)
    data = career["data"]
    fired = data.setdefault(T.CHARA_STORY_FIRED_KEY, [])
    for t in sorted(T._CHARA_FIXED_STORY_EVENTS):
        if t < turn and t not in fired:
            fired.append(t)
            marked.append(f"{T.CHARA_STORY_FIRED_KEY}:{t}")
    fired.sort()
    return marked


def _legacy_fired_keys() -> tuple:
    """The fired sets that are NOT turn-derivable (story ids, event ids, race
    outcomes). --reset-fired wipes them; nothing else here touches them."""
    return (T.GOAL_STORIES_FIRED_KEY, T.UNIQUE_LEVEL_FIRED_KEY,
            T.DIRECTOR_FAN_FIRED_KEY, T.POST_RACE_FIRED_KEY,
            T.SCENARIO_EVENTS_DONE_KEY, T.SCENARIO_FIXED_FIRED_KEY,
            T.GRAND_LIVE_EVENTS_FIRED_KEY, T.SCENARIO_TURNS_FIRED_KEY,
            event_engine.FIRED_EVENTS_KEY)


# ================================================================= races ===

def _race_name(program_id) -> str:
    info = T._race_info_for_program(program_id) or {}
    return info.get("story_name") or f"program {program_id}"


def _goal_schedule(ci: dict) -> list:
    """[(turn, program_id, name)] for every DETERMINED goal race on the route,
    group goals (URA finals / JBC) resolved to the member this career runs."""
    out = []
    for t, cid, kind, _cv1 in T._route_goal_rows(
            tuple(ci.get("route_race_id_array") or ())):
        info = (T._race_info_for_program(cid) if kind == "program"
                else T._group_race_for_turn(ci, cid, t))
        if info:
            out.append((t, info["program_id"], info.get("story_name") or ""))
    return sorted(out)


def _add_race(career: dict, program_id: int, turn: int, rank: int,
              with_rewards: bool) -> dict:
    """Append one race to race_history in the same shape _append_race_history
    writes, credit its real fan gain, and optionally pay its stat/SP reward."""
    data = career["data"]
    ci = data["chara_info"]
    history = data.setdefault("race_history", [])
    history.append({
        "turn": turn, "program_id": program_id,
        "weather": 1, "ground_condition": 1,
        "running_style": ci.get("race_running_style", 2),
        "result_rank": rank, "frame_order": 1, "npc_count": 17,
    })
    history.sort(key=lambda h: (h.get("turn") or 0))
    fans = T._fan_gain_for_race(ci, program_id, rank)
    ci["fans"] = (ci.get("fans") or 0) + fans
    reward = None
    if with_rewards:
        reward = T._race_reward_for(ci, program_id,
                                    T._finals_round_for_program(program_id), rank)
        T._apply_race_reward(ci, reward)
    return {"program_id": program_id, "turn": turn, "rank": rank,
            "fans": fans, "reward": reward}


def _tally_fill_races(career: dict, up_to_turn: int, taken_turns: set,
                     rank: int) -> list:
    """[(turn, program_id, rank)] to record so every ct=2 GRADE-TALLY goal whose
    deadline is behind `up_to_turn` reads as met.

    --fill-races' job is "count every goal", and a tally goal is not a race on
    the route: it is N races of a grade, run inside the goal's own window (see
    T._goal_window / T._grade_tally). Filling only the ct=1 rows left Oguri
    Cap's turn-60 'top 3 in 2 G1s' unmet on a jumped-to turn, and the live
    server then failed the career on the next turn. Picks real programs whose
    calendar slot falls in the window, on turns nothing else is using."""
    ci = career["data"]["chara_info"]
    history = career["data"].get("race_history") or []
    goals = T._route_all_goals(tuple(ci.get("route_race_id_array") or ()))
    out = []
    for g in goals:
        if g[2] != 2 or g[0] >= up_to_turn:
            continue
        after, by = T._goal_window(g, goals)
        # What the route's own goal races already contributed counts -- only
        # the shortfall gets invented.
        need = (g[5] or 1) - T._grade_tally(g, history, goals)
        # A tally that asks for a WIN (Smart Falcon's cv1=1) is not satisfied
        # by --rank 3, so the fill never places worse than the goal requires.
        place = min(rank, g[4] or rank)
        used = set(taken_turns)
        for turn in range(after + 1, by + 1):
            if need <= 0:
                break
            if turn in used:
                continue
            row = master_data.query_one(
                "SELECT month, half, period FROM single_mode_turn "
                "WHERE turn_set_id=1 AND turn=?", (turn,))
            if not row or row["period"] == 3:
                continue
            prog = master_data.query_one(
                "SELECT p.id FROM single_mode_program p "
                "JOIN race_instance ri ON ri.id=p.race_instance_id "
                "JOIN race r ON r.id=ri.race_id "
                "WHERE p.month=? AND p.half=? AND r.grade=? AND p.base_program_id=0 "
                f"AND p.race_permission IN {T._year_permissions(turn)} LIMIT 1",
                (row["month"], row["half"], g[3]))
            if not prog:
                continue
            out.append((turn, prog["id"], place))
            used.add(turn)
            need -= 1
    return out


def _refund_fans(ci: dict, races) -> int:
    """Take back what these races paid. Un-running a race has to un-pay it, or
    the class/goal reconcile reads a trainee who never ran them yet somehow
    carries their following."""
    total = 0
    for h in races:
        gain = T._fan_gain_for_race(ci, h.get("program_id"), h.get("result_rank") or 1)
        ci["fans"] = max(0, (ci.get("fans") or 0) - gain)
        total += gain
    return total


# ================================================================= paths ===

# Short aliases for the properties that actually get edited, so the common case
# is `career.py set speed 900` rather than a path nobody remembers.
ALIASES = {
    "turn": "chara_info.turn",
    "state": "chara_info.state",
    "playing-state": "chara_info.playing_state",
    "speed": "chara_info.speed", "stamina": "chara_info.stamina",
    "power": "chara_info.power", "guts": "chara_info.guts",
    "wiz": "chara_info.wiz",
    "max-speed": "chara_info.max_speed", "max-stamina": "chara_info.max_stamina",
    "max-power": "chara_info.max_power", "max-guts": "chara_info.max_guts",
    "max-wiz": "chara_info.max_wiz",
    "sp": "chara_info.skill_point", "fans": "chara_info.fans",
    "vital": "chara_info.vital", "max-vital": "chara_info.max_vital",
    "mood": "chara_info.motivation", "grade": "chara_info.chara_grade",
    "scenario": "chara_info.scenario_id", "card": "chara_info.card_id",
    "style": "chara_info.race_running_style",
    "skills": "chara_info.skill_array", "hints": "chara_info.skill_tips_array",
    "conditions": "chara_info.chara_effect_id_array",
    "route": "chara_info.route_race_id_array",
    "history": "race_history",
    "facility-levels": T.FACILITY_LEVELS_KEY,
    "facility-trains": T.FACILITY_TRAINS_KEY,
}
for _f in T._APTITUDE_FIELDS:                 # turf/dirt/mile/nige/... aptitudes
    ALIASES[_f.replace("proper_", "").replace("_", "-")] = f"chara_info.{_f}"
    # ...and the bare name ("dirt", "mile", "nige"), which is what anyone
    # actually types. A typo must NOT silently create a junk key, so `set`
    # refuses to invent a path unless --create says to.
    ALIASES.setdefault(_f.rsplit("_", 1)[-1], f"chara_info.{_f}")


def _resolve_path(path: str) -> list:
    path = ALIASES.get(path, path)
    parts = []
    for seg in str(path).split("."):
        if seg == "":
            continue
        parts.append(int(seg) if re.fullmatch(r"-?\d+", seg) else seg)
    if not parts:
        sys.exit("empty path")
    return parts


def _dig(root, parts, create=False):
    """(container, last_key) for a dotted path, so the caller can read, write
    or delete the leaf."""
    node = root
    for i, key in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(key, int):
            if not isinstance(node, list) or not (-len(node) <= key < len(node)):
                sys.exit(f"path {'.'.join(map(str, parts))}: index {key} out of range")
            node = node[key]
            continue
        if not isinstance(node, dict):
            sys.exit(f"path {'.'.join(map(str, parts))}: {key} is not under a dict")
        if key not in node:
            if not create:
                sys.exit(f"path {'.'.join(map(str, parts))}: no key {key!r}")
            node[key] = [] if isinstance(nxt, int) else {}
        node = node[key]
    key = parts[-1]
    if isinstance(key, int):
        if not isinstance(node, list) or not (-len(node) <= key < len(node)):
            sys.exit(f"path {'.'.join(map(str, parts))}: index {key} out of range")
    elif not isinstance(node, dict):
        sys.exit(f"path {'.'.join(map(str, parts))}: {key} is not under a dict")
    return node, key


def _parse_value(raw: str, current):
    """JSON, with two conveniences: a bare word stays a string, and +N/-N is a
    relative change when the current value is a number."""
    if isinstance(current, (int, float)) and not isinstance(current, bool) \
            and re.fullmatch(r"[+-]\d+(\.\d+)?", raw or ""):
        return current + (float(raw) if "." in raw else int(raw))
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _target(full_state, career, args):
    return full_state if getattr(args, "full_state", False) else career["data"]


def _turn_label(turn) -> str:
    row = master_data.query_one(
        "SELECT month, half FROM single_mode_turn WHERE turn_set_id=? AND turn=?",
        (T._URA_TURN_SET_ID, turn))
    if not row:
        return f"turn {turn}"
    year = ("Junior" if turn <= 24 else "Classic" if turn <= 48
            else "Senior" if turn <= 72 else "Finals")
    return (f"turn {turn} ({year} Year, "
            f"{'Early' if row['half'] == 1 else 'Late'} month {row['month']})")


# ============================================================== commands ===

def cmd_show(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    data = career["data"]
    ci = data["chara_info"]
    turn = ci.get("turn")
    history = data.get("race_history") or []
    print(f"card {ci.get('card_id')}  scenario {ci.get('scenario_id')} "
          f"({T.SCENARIO_NAMES.get(ci.get('scenario_id'), '?')})")
    print(f"{_turn_label(turn)}  state {ci.get('state')} "
          f"({STATE_NAMES.get(ci.get('state'), '?')})  "
          f"playing_state {ci.get('playing_state')}  class {ci.get('chara_grade')}")
    print("stats:", {s: ci.get(s) for s in STATS})
    print(" caps :", {s: ci.get('max_wiz' if s == 'wiz' else f'max_{s}') for s in STATS})
    print(f"SP {ci.get('skill_point')}  fans {ci.get('fans')}  "
          f"vital {ci.get('vital')}/{ci.get('max_vital')}  mood {ci.get('motivation')}")
    print(f"skills {len(ci.get('skill_array') or [])}  "
          f"hints {len(ci.get('skill_tips_array') or [])}  "
          f"conditions {ci.get('chara_effect_id_array') or []}")
    print(f"facility levels: {T._facility_levels(data)}  "
          f"camp turn: {'yes' if T._is_camp_turn(turn) else 'no'}")
    goals = T._route_all_goals(tuple(ci.get("route_race_id_array") or ()))
    print(f"goals: {T._goals_cleared_count(ci, history, turn)}/{len(goals)} cleared "
          f"(announced {full_state.get(T.GOAL_ANNOUNCED_KEY) or []})")
    forced = sorted(T._forced_race_turns(tuple(ci.get("route_race_id_array") or ())))
    upcoming = [t for t in forced if t >= (turn or 0)][:3]
    print(f"races run: {len(history)}  next mandatory race turns: {upcoming or 'none'}")
    home = data.get("home_info") or {}
    cmds = home.get("command_info_array") or []
    enabled = [c.get("command_id") for c in cmds if c.get("is_enable")]
    print(f"training screen: {len(cmds)} commands, enabled {enabled} "
          f"(race_entry_restriction {home.get('race_entry_restriction')})")
    st = CE.state(full_state)
    active = st.get("active")
    print(f"events: active {active.get('event_id') if active else 'none'}  "
          f"queued {len(st.get('queue') or [])}  fired {len(st.get('fired') or [])}")
    in_flight = [k for k in _transient_keys() if k in full_state]
    if in_flight:
        print(f"in flight: {in_flight}")


def cmd_goals(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    history = career["data"].get("race_history") or []
    goals = T._route_all_goals(tuple(ci.get("route_race_id_array") or ()))
    if not goals:
        print("no route goals (route_race_id_array empty)")
        return
    kinds = {1: "race", 2: "grade tally", 3: "fan threshold"}
    for g in goals:
        turn, sort_id, ctype, cid, cv1, cv2 = g
        done = T._goal_is_cleared(g, ci, history, goals)
        due = "due" if turn <= (ci.get("turn") or 0) else "future"
        if ctype == 1:
            what = _race_name(cid)
        elif ctype == 2:
            # Progress AND the window it is counted over -- a tally only counts
            # races run after the previous goal (T._grade_tally), so "which
            # turns count" is the first thing to check when one reads pending.
            after, by = T._goal_window(g, goals)
            what = (f"{T._grade_tally(g, history, goals)}/{cv2} top-{cv1} in "
                    f"grade {cid}, turns {after + 1}-{by}")
        else:
            what = f"{cv1} fans"
        print(f"  turn {turn:>2}  sort {sort_id:>3}  {kinds.get(ctype, ctype):<14} "
              f"{'CLEARED' if done else 'pending':<8} {due:<6} {what}")


def cmd_races(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    history = career["data"].get("race_history") or []
    if args.schedule:
        ran = {h.get("program_id") for h in history}
        for turn, program_id, name in _goal_schedule(ci):
            print(f"  turn {turn:>2}  program {program_id:<6} "
                  f"{'RUN' if program_id in ran else '---'}  {name}")
        return
    if not history:
        print("no races run")
        return
    for h in history:
        print(f"  turn {h.get('turn'):>2}  program {h.get('program_id'):<6} "
              f"rank {h.get('result_rank'):<3} {_race_name(h.get('program_id'))}")


def _career_data_get(full_state, key):
    career = full_state.get(T.STATE_KEY) or {}
    return (career.get("data") or {}).get(key)


def cmd_events(viewer, args):
    full_state = _load(viewer)
    _career(full_state)
    st = CE.state(full_state)
    active = st.get("active")
    print(f"ACTIVE: {active.get('event_id')} story {active.get('story_id')} "
          f"src {active.get('source')}" if active else "ACTIVE: none")
    queue = st.get("queue") or []
    print(f"QUEUED ({len(queue)}):")
    for e in queue:
        print(f"  prio {e.get('priority'):>3}  event {e.get('event_id'):<8} "
              f"story {e.get('story_id'):<12} {e.get('once_key') or ''} "
              f"[{e.get('source')}]")
    fired = st.get("fired") or []
    print(f"FIRED ({len(fired)})" + (":" if args.fired else " -- pass --fired to list"))
    if args.fired:
        for key in fired:
            t = _once_key_turn(key)
            print(f"  {key}" + (f"   (turn {t})" if t is not None else ""))
    for key in _legacy_fired_keys():
        value = full_state.get(key)
        if value:
            print(f"  {key}: {len(value) if isinstance(value, list) else value}")
    for key in (T.CHARA_STORY_FIRED_KEY, T.INSPIRATION_FIRED_KEY):
        value = _career_data_get(full_state, key)
        if value:
            print(f"  {key} (turns): {value}")


def cmd_event_undo(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    if args.all:
        st = CE.state(full_state)
        dropped = list(st.get("fired") or ())
        st["fired"] = []
        for key in (T.CHARA_STORY_FIRED_KEY, T.INSPIRATION_FIRED_KEY):
            career["data"][key] = []
    elif args.turn is not None:
        dropped = _unfire_from(full_state, career, int(args.turn))
    elif args.keys:
        st = CE.state(full_state)
        want = set(args.keys)
        dropped = [k for k in (st.get("fired") or ()) if k in want]
        st["fired"] = [k for k in (st.get("fired") or ()) if k not in want]
        missing = want - set(dropped)
        if missing:
            print(f"not in the fired set: {sorted(missing)}")
    else:
        sys.exit("give once_keys, --turn N, or --all")
    _save(viewer, full_state)
    print(f"un-fired {len(dropped)} event(s)"
          + (f": {dropped}" if dropped and len(dropped) <= 20 else ""))


def cmd_event_done(viewer, args):
    full_state = _load(viewer)
    _career(full_state)
    for key in args.keys:
        CE.mark_fired(full_state, key)
    _save(viewer, full_state)
    print(f"marked fired: {args.keys}")


def cmd_event_drop(viewer, args):
    full_state = _load(viewer)
    _career(full_state)
    st = CE.state(full_state)
    if args.all:
        n = len(st.get("queue") or []) + (1 if st.get("active") else 0)
        st["queue"] = []
        st["active"] = None
        print("cleared:", _clear_transient(full_state) or "nothing in flight")
        _save(viewer, full_state)
        print(f"dropped {n} event(s)")
        return
    if args.event_id is None:
        sys.exit("give an event_id or --all")
    CE.drop(full_state, int(args.event_id))
    _save(viewer, full_state)
    print(f"dropped event {args.event_id}")


def cmd_event_queue(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    chara = int(args.chara) if args.chara is not None else (ci.get("card_id") or 0) // 100
    event = CE.Event(
        event_id=int(args.event_id), story_id=int(args.story),
        play_timing=int(args.timing),
        choices=[CE.Choice(effects=[]) for _ in range(int(args.choices))],
        chara_id=chara, once_key=args.once_key, source="career.py",
        priority=int(args.priority))
    ok = CE.emit(full_state, event)
    _save(viewer, full_state)
    print(f"queued event {args.event_id} story {args.story} "
          f"({args.choices} choice(s))" if ok
          else "rejected -- already fired or already queued")


def cmd_set_turn(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    new = int(args.value)
    if not 1 <= new <= CAREER_END_TURN:
        sys.exit(f"turn must be 1..{CAREER_END_TURN}")
    old = ci.get("turn") or 1

    if not args.keep_events:
        st = CE.state(full_state)
        dropped = len(st.get("queue") or []) + (1 if st.get("active") else 0)
        st["queue"], st["active"] = [], None
        if dropped:
            print(f"dropped {dropped} in-flight event(s)")
    print("cleared:", _clear_transient(full_state) or "nothing in flight")

    if new < old:
        if not args.keep_races:
            history = career["data"].get("race_history") or []
            kept = [h for h in history if (h.get("turn") or 0) < new]
            undone = [h for h in history if h not in kept]
            if undone:
                lost = _refund_fans(ci, undone)
                print(f"pruned {len(undone)} race(s) from turn {new} on "
                      f"(-{lost} fans)")
            career["data"]["race_history"] = kept
        unfired = _unfire_from(full_state, career, new)
        print(f"un-fired {len(unfired)} turn-keyed event(s) from turn {new} on")
        if ci.get("state") == 2:
            ci["state"] = 0
            print("cleared the career-FAILED marker (state 2 -> 0)")
    elif new > old and args.mark_past:
        marked = _mark_past_fired(full_state, career, new)
        print(f"marked {len(marked)} skipped beat(s) as fired (bookkeeping only)")

    if args.reset_fired:
        wiped = [key for key in _legacy_fired_keys()
                 if full_state.pop(key, None) is not None]
        CE.state(full_state)["fired"] = []
        print(f"reset fired sets: career_events + {wiped}")

    ci["turn"] = new
    ci["playing_state"] = int(args.playing_state)

    if args.fill_races:
        ran = {h.get("program_id") for h in (career["data"].get("race_history") or [])}
        filled = []
        for t, program_id, _name in _goal_schedule(ci):
            if t >= new or program_id in ran:
                continue
            filled.append(_add_race(career, program_id, t, int(args.rank),
                                    args.with_rewards))
        # ...and the ct=2 grade tallies, which are not route races at all.
        taken = {h.get("turn") for h in (career["data"].get("race_history") or [])}
        taken |= {f["turn"] for f in filled}
        for t, program_id, place in _tally_fill_races(career, new, taken,
                                                      int(args.rank)):
            filled.append(_add_race(career, program_id, t, place,
                                    args.with_rewards))
            taken.add(t)
        filled.sort(key=lambda f: f["turn"])
        for f in filled:
            print(f"  ran turn {f['turn']:>2} {_race_name(f['program_id'])} "
                  f"rank {f['rank']} (+{f['fans']} fans"
                  + (f", {f['reward']}" if f.get("reward") else "") + ")")
        print(f"filled {len(filled)} goal race(s); fans now {ci.get('fans')}")

    for note in _reconcile(full_state, career):
        print(f"  {note}")
    _save(viewer, full_state)
    print(f"turn {old} -> {new}  [{_turn_label(new)}]")
    # A client already sitting in this career still holds the OLD turn, and the
    # server refuses actions stamped with it rather than rewinding the run to
    # match (single_mode_team._client_turn_conflict) -- reload before playing.
    print("   reload the career in the client (title -> Continue) before acting")


def cmd_reconcile(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    if args.clear_flight:
        print("cleared:", _clear_transient(full_state) or "nothing in flight")
    for note in _reconcile(full_state, career, grade=not args.no_grade):
        print(f"  {note}")
    _save(viewer, full_state)
    print("reconciled")


def cmd_get(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    root = _target(full_state, career, args)
    if not args.path:
        print(json.dumps(root, indent=2, ensure_ascii=False)[:args.limit])
        return
    parts = _resolve_path(args.path)
    node, key = _dig(root, parts)
    try:
        value = node[key]
    except (KeyError, IndexError):
        sys.exit(f"no key {'.'.join(map(str, parts))}")
    print(json.dumps(value, indent=2, ensure_ascii=False)[:args.limit])


def cmd_set(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    root = _target(full_state, career, args)
    parts = _resolve_path(args.path)
    node, key = _dig(root, parts, create=args.create)
    exists = isinstance(key, int) or key in node
    if not exists and not args.create:
        # A mistyped path used to land as a brand-new top-level key that
        # nothing reads -- the edit silently did nothing at all.
        sys.exit(f"no existing key {'.'.join(map(str, parts))} "
                 f"-- pass --create to add it (see `career.py props`)")
    current = node[key] if exists else None
    value = _parse_value(args.value, current)
    node[key] = value
    # A property edit can change what the rest of the state must say (fans ->
    # goals, a condition or facility level -> the training screen), so the same
    # reconcile set-turn runs happens here. chara_grade is left alone: `set
    # grade N` has to survive its own command.
    for note in _reconcile(full_state, career, grade=False):
        print(f"  {note}")
    _save(viewer, full_state)
    print(f"{'.'.join(map(str, parts))}: {current!r} -> {value!r}")


def cmd_del(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    root = _target(full_state, career, args)
    parts = _resolve_path(args.path)
    node, key = _dig(root, parts)
    if not isinstance(key, int) and key not in node:
        sys.exit(f"no key {key!r}")
    gone = node.pop(key)
    for note in _reconcile(full_state, career, grade=False):
        print(f"  {note}")
    _save(viewer, full_state)
    print(f"deleted {'.'.join(map(str, parts))} (was {gone!r})")


def cmd_props(viewer, args):
    full_state = state_store.get_state(viewer) or {}
    career = full_state.get(T.STATE_KEY)
    data = (career or {}).get("data") or {}
    print(f"{'alias':<18} {'path':<35} current")
    for alias in sorted(ALIASES):
        path = ALIASES[alias]
        try:
            node, key = _dig(data, _resolve_path(path))
            value = json.dumps(node[key], ensure_ascii=False)
        except (SystemExit, KeyError, IndexError, TypeError):
            value = "-"          # not on this career (or no career at all)
        print(f"  {alias:<18} {path:<35} {value[:44]}")
    print("\nany other dotted path under the career's `data` works too; "
          "--full-state addresses the whole viewer state.")


def cmd_race_add(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    turn = int(args.turn) if args.turn is not None else (ci.get("turn") or 1)
    res = _add_race(career, int(args.program_id), turn, int(args.rank),
                    args.with_rewards)
    for note in _reconcile(full_state, career):
        print(f"  {note}")
    _save(viewer, full_state)
    print(f"recorded {_race_name(res['program_id'])} on turn {res['turn']} "
          f"rank {res['rank']} (+{res['fans']} fans)")


def cmd_race_del(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    data = career["data"]
    history = data.get("race_history") or []
    if args.all:
        kept = []
    elif args.from_turn is not None:
        kept = [h for h in history if (h.get("turn") or 0) < int(args.from_turn)]
    elif args.turn is not None:
        kept = [h for h in history if (h.get("turn") or 0) != int(args.turn)]
    else:
        sys.exit("give --turn N, --from-turn N or --all")
    removed = [h for h in history if h not in kept]
    if args.refund_fans:
        print(f"refunded {_refund_fans(data['chara_info'], removed)} fans")
    data["race_history"] = kept
    for note in _reconcile(full_state, career):
        print(f"  {note}")
    _save(viewer, full_state)
    print(f"removed {len(removed)} race(s)")


def cmd_unlock(viewer, args):
    """Force command_info_array entries enabled for the CURRENT turn.

    Deliberately a one-shot override, not a persistent flag: the screen is
    rebuilt from the route on every turn advance (and by every other command
    here), so this survives exactly until the next action -- which is what you
    want for poking at a locked mandatory-race turn."""
    full_state = _load(viewer)
    career = _career(full_state)
    home = career["data"].get("home_info") or {}
    cmds = home.get("command_info_array") or []
    if not cmds:
        sys.exit("no command_info_array on this career -- run `reconcile` first")
    want = None if (args.all or not args.command) else {int(c) for c in args.command}
    value = 0 if args.lock else 1
    touched = []
    for c in cmds:
        if want is None or c.get("command_id") in want:
            c["is_enable"] = value
            touched.append(c.get("command_id"))
    if want is None and not args.lock:
        home["race_entry_restriction"] = 0
    _save(viewer, full_state)
    print(f"{'locked' if args.lock else 'unlocked'} {touched} "
          f"(until the next turn advance rebuilds the screen)")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--viewer", default=DEFAULT_VIEWER)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *fields):
        sp = sub.add_parser(name)
        for f in fields:
            sp.add_argument(f)
        sp.set_defaults(fn=fn)
        return sp

    add("show", cmd_show)
    add("goals", cmd_goals)
    add("props", cmd_props)

    sp = add("races", cmd_races)
    sp.add_argument("--schedule", action="store_true",
                    help="the route's goal races instead of the history")

    sp = add("set-turn", cmd_set_turn, "value")
    sp.add_argument("--fill-races", action="store_true",
                    help="record every goal race before the target turn as run")
    sp.add_argument("--rank", default=1, help="placement for --fill-races (default 1)")
    sp.add_argument("--with-rewards", action="store_true",
                    help="--fill-races also pays each race's stat/SP reward")
    sp.add_argument("--keep-races", action="store_true",
                    help="rewinding: keep race_history at/after the target turn")
    sp.add_argument("--keep-events", action="store_true",
                    help="keep the queued/active events instead of dropping them")
    sp.add_argument("--reset-fired", action="store_true",
                    help="also wipe the fired sets that aren't turn-derivable")
    sp.add_argument("--mark-past", action="store_true",
                    help="jumping forward: record skipped beats as fired")
    sp.add_argument("--playing-state", default=1)

    sp = add("reconcile", cmd_reconcile)
    sp.add_argument("--clear-flight", action="store_true",
                    help="also drop in-flight race/event contexts")
    sp.add_argument("--no-grade", action="store_true", help="leave chara_grade alone")

    sp = add("events", cmd_events)
    sp.add_argument("--fired", action="store_true", help="list the fired once_keys")

    sp = add("event-undo", cmd_event_undo)
    sp.add_argument("keys", nargs="*")
    sp.add_argument("--turn", default=None)
    sp.add_argument("--all", action="store_true")

    add("event-done", cmd_event_done).add_argument("keys", nargs="+")

    sp = add("event-drop", cmd_event_drop)
    sp.add_argument("event_id", nargs="?", default=None)
    sp.add_argument("--all", action="store_true")

    sp = add("event-queue", cmd_event_queue, "event_id")
    sp.add_argument("--story", required=True)
    sp.add_argument("--choices", default=1)
    sp.add_argument("--timing", default=1)
    sp.add_argument("--chara", default=None)
    sp.add_argument("--priority", default=CE.PRIO_NORMAL)
    sp.add_argument("--once-key", default=None)

    sp = sub.add_parser("get"); sp.add_argument("path", nargs="?")
    sp.add_argument("--limit", type=int, default=20000)
    sp.add_argument("--full-state", action="store_true")
    sp.set_defaults(fn=cmd_get)
    sp = sub.add_parser("set"); sp.add_argument("path"); sp.add_argument("value")
    sp.add_argument("--create", action="store_true",
                    help="allow adding a key that doesn't exist yet")
    sp.add_argument("--full-state", action="store_true")
    sp.set_defaults(fn=cmd_set)
    sp = sub.add_parser("del"); sp.add_argument("path")
    sp.add_argument("--full-state", action="store_true")
    sp.set_defaults(fn=cmd_del)

    sp = add("race-add", cmd_race_add, "program_id")
    sp.add_argument("--turn", default=None)
    sp.add_argument("--rank", default=1)
    sp.add_argument("--with-rewards", action="store_true")

    sp = add("race-del", cmd_race_del)
    sp.add_argument("--turn", default=None)
    sp.add_argument("--from-turn", default=None)
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--refund-fans", action="store_true")

    sp = add("unlock", cmd_unlock)
    sp.add_argument("command", nargs="*", help="command_ids (default: all)")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--lock", action="store_true", help="disable them instead")

    args = p.parse_args()
    # Same whitespace strip as admin.py: a trainer id pasted from the game's UI
    # arrives grouped ("802 445 340 143") and would otherwise mint a phantom
    # account row instead of touching the real one.
    args.fn("".join(str(args.viewer).split()), args)


if __name__ == "__main__":
    main()
