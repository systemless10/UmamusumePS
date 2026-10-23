
from __future__ import annotations

import copy
import dataclasses
import logging
import random

from . import event_engine

log = logging.getLogger("uma-server")

STATE_KEY = "career_events"

PRIO_FRONT = 0
PRIO_SCENARIO = 10
PRIO_UNLOCK = 20
PRIO_SPECIAL = 30
PRIO_NORMAL = 50
PRIO_TRAILING = 80


@dataclasses.dataclass
class Choice:
    effects: list = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {"effects": list(self.effects)}

    @classmethod
    def from_dict(cls, d: dict) -> "Choice":
        return cls(effects=list((d or {}).get("effects") or []))


@dataclasses.dataclass
class Event:
    event_id: int
    story_id: int
    play_timing: int = 1
    choices: list = dataclasses.field(default_factory=list)
    chara_id: int = 0
    support_card_id: int = 0
    once_key: str | None = None
    source: str = ""
    priority: int = PRIO_NORMAL
    resolver: str | None = None
    payload: dict = dataclasses.field(default_factory=dict)
    show_clear: int = 0
    show_clear_sort_id: int = 0
    minigame_result: dict | None = None
    raw: dict | None = None
    hold_until: str | None = None
    timing_from_endpoint: bool = False
    wire_event_id: int = 0

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["choices"] = [Choice(**c).to_dict() if isinstance(c, dict) else c.to_dict()
                        for c in self.choices]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d or {})
        d["choices"] = [Choice.from_dict(c) for c in (d.get("choices") or [])]
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def wire_entry(self) -> dict:
        if self.raw is not None:
            entry = copy.deepcopy(self.raw)
            if self.wire_event_id:
                entry["event_id"] = self.wire_event_id
            return entry
        return {
            "event_id": self.wire_event_id or self.event_id,
            "chara_id": self.chara_id,
            "story_id": self.story_id,
            "play_timing": self.play_timing,
            "event_contents_info": {
                "support_card_id": self.support_card_id,
                "show_clear": self.show_clear,
                "show_clear_sort_id": self.show_clear_sort_id,
                "choice_array": [
                    {"select_index": 1, "receive_item_id": 0, "target_race_id": 0,
                     "gain_select_id_index": i + 1, "select_icon": 0}
                    for i in range(len(self.choices))],
                "tips_training_partner_id": None,
            },
            "succession_event_info": None,
            "minigame_result": self.minigame_result,
        }

    def engine_event(self) -> dict:
        return {"choices": [c.to_dict() for c in self.choices]}


def _blank() -> dict:
    return {"queue": [], "fired": [], "active": None, "seq": 0}


CAREER_SALT_KEY = "salt"


def career_salt(full_state: dict) -> int:
    st = state(full_state)
    salt = st.get(CAREER_SALT_KEY)
    if not salt:
        salt = random.getrandbits(32)
        st[CAREER_SALT_KEY] = salt
    return int(salt)


def state(full_state: dict) -> dict:
    st = full_state.get(STATE_KEY)
    if not isinstance(st, dict):
        st = _blank()
        full_state[STATE_KEY] = st
    for k, v in _blank().items():
        st.setdefault(k, v)
    return st


def reset(full_state: dict) -> None:
    full_state[STATE_KEY] = _blank()


def already_fired(full_state: dict, once_key) -> bool:
    if not once_key:
        return False
    return str(once_key) in set(state(full_state).get("fired") or ())


def mark_fired(full_state: dict, once_key) -> None:
    if not once_key:
        return
    st = state(full_state)
    fired = set(st.get("fired") or ())
    fired.add(str(once_key))
    st["fired"] = sorted(fired)


def unmark_fired(full_state: dict, once_key) -> None:
    if not once_key:
        return
    st = state(full_state)
    fired = set(st.get("fired") or ())
    fired.discard(str(once_key))
    st["fired"] = sorted(fired)


_SYNTHETIC_ID_BASE = 990000


def wire_id(row) -> int:
    if not isinstance(row, dict):
        return 0
    return int(row.get("wire_event_id") or row.get("event_id") or 0)


def _disambiguate(st: dict, row: dict) -> None:
    taken = {wire_id(e) for e in (st.get("queue") or ())}
    if isinstance(st.get("active"), dict):
        taken.add(wire_id(st["active"]))
    if int(row.get("event_id") or 0) not in taken:
        return
    row["wire_event_id"] = _SYNTHETIC_ID_BASE + int(st.get("seq") or 0)
    log.info("event %s (story %s) collides with a queued event; serving it as "
             "%s so the acknowledgement is unambiguous",
             row.get("event_id"), row.get("story_id"), row["wire_event_id"])


def emit(full_state: dict, event: Event) -> bool:
    if already_fired(full_state, event.once_key):
        return False
    st = state(full_state)

    def _identity(e: dict):
        return e.get("once_key") or ("id", e.get("event_id"), e.get("story_id"))

    mine = _identity(event.to_dict())
    known = {_identity(e) for e in st["queue"]}
    active_row = st.get("active")
    if active_row:
        known.add(_identity(active_row))
    if mine in known:
        return False
    st["seq"] = int(st.get("seq") or 0) + 1
    row = event.to_dict()
    row["_seq"] = st["seq"]
    _disambiguate(st, row)
    st["queue"].append(row)
    st["queue"].sort(key=lambda e: (e.get("priority", PRIO_NORMAL),
                                    e.get("_seq", 0)))
    mark_fired(full_state, event.once_key)
    log.debug("event queued: %s story=%s prio=%s src=%s",
              event.event_id, event.story_id, event.priority, event.source)
    return True


def pending(full_state: dict) -> list:
    return [Event.from_dict(e) for e in state(full_state).get("queue") or ()]


def peek(full_state: dict) -> Event | None:
    q = state(full_state).get("queue") or ()
    return Event.from_dict(q[0]) if q else None


TIMING_FORBIDDEN_ENDPOINTS = {
    3: ("exec_command",),
}


ENDPOINT_TIMING = {
    "race_entry": 2,
    "race_out": 3,
    "live_start": 12,
    "team_race_out": 9,
    "exec_command": 6,
    "start": 1,
    "load": 1,
}
CHAIN_TIMING_KEY = "chain_timing"

ENDPOINT_FORCED_TIMING = {
    "race_entry": 2,
    "race_out": 3,
    "team_race_out": 9,
    "load": 1,
}


def note_endpoint(full_state: dict, endpoint: str) -> int:
    return _endpoint_timing(full_state, endpoint)


def stamp_response_timing(data: dict, endpoint: str) -> None:
    timing = ENDPOINT_FORCED_TIMING.get((endpoint or "").rsplit("/", 1)[-1])
    if timing is None or not isinstance(data, dict):
        return
    for entry in _response_events(data):
        if entry.get("play_timing") != timing:
            entry["play_timing"] = timing


def _response_events(data: dict):
    for holder in (data, data.get("single_mode_load_common"),
                   data.get("single_mode_start_common")):
        if not isinstance(holder, dict):
            continue
        for entry in holder.get("unchecked_event_array") or ():
            if isinstance(entry, dict):
                yield entry


_MULTI_CHARA_FLAG_PREFIXES = ("single_mode_team",)


def stamp_multi_chara_flag(data: dict, endpoint: str) -> None:
    if not isinstance(data, dict):
        return
    wants = (endpoint or "").split("/")[0] in _MULTI_CHARA_FLAG_PREFIXES
    for entry in _response_events(data):
        info = entry.get("event_contents_info")
        if not isinstance(info, dict):
            continue
        if wants:
            info["is_effected_multi_chara"] = False
        else:
            info.pop("is_effected_multi_chara", None)


def _endpoint_timing(full_state: dict, endpoint: str) -> int:
    st = state(full_state)
    name = (endpoint or "").rsplit("/", 1)[-1]
    timing = ENDPOINT_TIMING.get(name)
    if timing is None:
        return int(st.get(CHAIN_TIMING_KEY) or 1)
    st[CHAIN_TIMING_KEY] = timing
    return timing


_HOLD_GATES = {}


def hold_gate(name: str):
    def wrap(fn):
        _HOLD_GATES[name] = fn
        return fn
    return wrap


def _held(row: dict, full_state: dict) -> bool:
    name = (row or {}).get("hold_until")
    if not name:
        return False
    fn = _HOLD_GATES.get(name)
    if fn is None:
        return False
    try:
        return not fn(full_state)
    except Exception:
        log.exception("hold gate %r failed; serving the event", name)
        return False


def servable_on(row: dict, endpoint: str) -> bool:
    if not endpoint or not isinstance(row, dict):
        return True
    if row.get("timing_from_endpoint"):
        return True
    blocked = TIMING_FORBIDDEN_ENDPOINTS.get(int(row.get("play_timing") or 0))
    return not (blocked and endpoint.rsplit("/", 1)[-1] in blocked)


def queued_rows(full_state: dict) -> list:
    st = state(full_state)
    rows = [r for r in (st.get("queue") or ()) if isinstance(r, dict)]
    if isinstance(st.get("active"), dict):
        rows.append(st["active"])
    return rows


SERVE_HOOKS: list = []


def serve_next(full_state: dict, endpoint: str = ""):
    st = state(full_state)
    if not st["queue"]:
        st["active"] = None
        return None, None
    index = 0
    while index < len(st["queue"]) and _held(st["queue"][index], full_state):
        index += 1
    if index >= len(st["queue"]):
        return None, None
    if not servable_on(st["queue"][index], endpoint):
        return None, None
    ev = Event.from_dict(st["queue"].pop(index))
    if ev.timing_from_endpoint:
        ev.play_timing = _endpoint_timing(full_state, endpoint)
    elif endpoint:
        _endpoint_timing(full_state, endpoint)
    st["active"] = ev.to_dict()
    for hook in SERVE_HOOKS:
        hook(full_state, ev)
    return ev.wire_entry(), ev


def active(full_state: dict) -> Event | None:
    a = state(full_state).get("active")
    return Event.from_dict(a) if a else None


def find(full_state: dict, event_id) -> Event | None:
    st = state(full_state)
    a = st.get("active")
    if a and _matches(a, event_id):
        return Event.from_dict(a)
    for e in st.get("queue") or ():
        if _matches(e, event_id):
            return Event.from_dict(e)
    return None


def _matches(row: dict, event_id) -> bool:
    try:
        return int(event_id) == wire_id(row)
    except (TypeError, ValueError):
        return False


def drop(full_state: dict, event_id) -> None:
    st = state(full_state)
    a = st.get("active")
    if a and _matches(a, event_id):
        st["active"] = None
        return
    queue = list(st.get("queue") or ())
    for i, e in enumerate(queue):
        if _matches(e, event_id):
            del queue[i]
            break
    st["queue"] = queue


RESOLVERS: dict = {}


def resolver(name: str):
    def deco(fn):
        RESOLVERS[name] = fn
        return fn
    return deco


@dataclasses.dataclass
class Resolution:
    event: Event
    choice_number: int
    applied: dict = dataclasses.field(default_factory=dict)
    extra: dict = dataclasses.field(default_factory=dict)
    handled: bool = True


def resolve(full_state: dict, chara_info: dict, event_id, choice_number=0,
            **kwargs) -> Resolution | None:
    ev = find(full_state, event_id)
    if ev is None:
        return None

    res = Resolution(event=ev, choice_number=choice_number or 0)

    if ev.choices:
        index = max(1, int(choice_number or 1))
        index = min(index, len(ev.choices))
        try:
            res.applied = event_engine.apply_choice(
                chara_info, ev.engine_event(), index, full_state=full_state) or {}
        except Exception:
            log.exception("event %s: reward application failed", event_id)

    fn = RESOLVERS.get(ev.resolver) if ev.resolver else None
    if fn is not None:
        try:
            res.extra = fn(full_state, chara_info, ev, choice_number, **kwargs) or {}
        except Exception:
            log.exception("event %s: resolver %r failed", event_id, ev.resolver)

    drop(full_state, event_id)
    log.info("event %s resolved (choice %s, src=%s) applied=%s%s",
             event_id, choice_number, ev.source, res.applied,
             f" extra={res.extra}" if res.extra else "")
    return res


PRODUCERS: list = []


@dataclasses.dataclass
class Ctx:
    viewer_id: object
    full_state: dict
    career: dict
    chara_info: dict
    turn: int
    advanced_turn: int | None = None
    payload: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        if self.advanced_turn is None:
            self.advanced_turn = (self.turn or 0) + 1

    @property
    def career_data(self) -> dict:
        return self.career.get("data", {}) if isinstance(self.career, dict) else {}

    @property
    def race_history(self) -> list:
        return self.career_data.get("race_history") or []

    @property
    def player_chara(self) -> int:
        return (self.chara_info.get("card_id") or 0) // 100

    @property
    def scenario_id(self) -> int:
        return int(self.chara_info.get("scenario_id") or 1)


def producer(name: str, priority: int = PRIO_NORMAL, scenario: int | None = None):
    def deco(fn):
        PRODUCERS.append((name, priority, fn, scenario))
        return fn
    return deco


def poll(ctx: Ctx) -> list:
    accepted = []
    for name, prio, fn, scenario in PRODUCERS:
        if scenario is not None and ctx.scenario_id != scenario:
            continue
        try:
            produced = fn(ctx) or ()
        except Exception:
            log.exception("producer %r failed; skipping it this turn", name)
            continue
        for ev in produced:
            if ev is None:
                continue
            if not ev.source:
                ev.source = name
            if ev.priority == PRIO_NORMAL and prio != PRIO_NORMAL:
                ev.priority = prio
            if emit(ctx.full_state, ev):
                accepted.append(ev)
    accepted.sort(key=lambda e: e.priority)
    return accepted
