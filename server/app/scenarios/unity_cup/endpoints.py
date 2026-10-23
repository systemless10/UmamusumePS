
from __future__ import annotations

import copy
import logging
import random

from ... import career_events as CE
from ... import master_data
from ...handlers import race_mobs
from ...simulation import race_simulator
from . import impl as unity_cup
from ...result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")


_RESULT_WIN = 1
_RESULT_LOSE = 2
_RESULT_DRAW = 3


def _smt():
    from ...handlers import single_mode_team
    return single_mode_team


def _refused(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _career(payload: dict):
    smt = _smt()
    viewer_id = payload["viewer_id"]
    full_state = smt.state_store.get_state(viewer_id) or {}
    career = full_state.get(smt.STATE_KEY)
    if not isinstance(career, dict):
        return None, None, None
    return viewer_id, full_state, career


_ENDPOINT_FIELDS = {
    "opponent_list": ("chara_info",),
    "team_edit": ("chara_info",),
    "team_race_start": ("chara_info",),
    "team_race_end": ("chara_info", "add_music"),
    "team_race_out": ("chara_info", "unchecked_event_array"),
}


def _envelope(full_state: dict, career: dict, endpoint: str = "") -> dict:
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


RACE_RANKS = (3, 4, 6, 7, unity_cup.BOSS_RANK)
OPPONENT_CHOICES = 3


def handle_opponent_list(payload: dict) -> dict:
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
    cached = st.get("opponents")
    if not isinstance(cached, dict) or cached.get("turn") != turn:
        return []
    live = unity_cup.offers(st)
    if not live:
        log.info("unity cup: discarding a stale opponent roll (version %s)",
                 (cached or {}).get("version"))
    return live


def _our_charas(st: dict, chara_info=None) -> set:
    ours = {m["chara_id"] for m in unity_cup.active_members(st)}
    trainee = unity_cup.trainee_chara(chara_info)
    if trainee:
        ours.add(trainee)
    return ours


def _race_index(st: dict, turn: int) -> int:
    if turn in unity_cup.TEAM_RACE_TURNS:
        return unity_cup.TEAM_RACE_TURNS.index(turn)
    return min(len(st.get("races") or ()), len(RACE_RANKS) - 1)


def _roll_opponents(st: dict, turn: int, exclude=()) -> list:
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

    if rank >= unity_cup.BOSS_RANK:
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
            "draw_rank": max(1, current - (last - offer)),
            "team_data_array": _opponent_lineup(set_id, st, rng, exclude),
        })

    if unity_cup.elite_available(st, index):
        elite_ids = list(unity_cup.elite_set_ids())
        rng.shuffle(elite_ids)
        if elite_ids:
            out.insert(0, {
                "team_race_set_id": elite_ids[0],
                "team_power": unity_cup.ELITE_RANK,
                "team_rank": _opponent_rank(unity_cup.ELITE_RANK, rng),
                "win_up_rank": max(1, current + win_deltas[0] - 2),
                "lose_down_rank": min(30, current + lose_deltas[0]),
                "draw_rank": max(1, current - len(out)),
                "team_data_array": _opponent_lineup(elite_ids[0], st, rng,
                                                    exclude),
            })
    return out


def _opponent_rank(rank: int, rng: random.Random) -> int:
    lo, hi = unity_cup.OPPONENT_RANK_BAND.get(int(rank), (30, 30))
    return rng.randint(lo, hi)


def _opponent_lineup(set_id: int, st: dict, rng: random.Random,
                     exclude=()) -> list:
    row = unity_cup.team_race_set(set_id) or {}
    rank = int(row.get("rank") or 1)
    total, n_named = unity_cup.OPPONENT_TEAM_SHAPE.get(
        rank, unity_cup.OPPONENT_TEAM_SHAPE[1])

    if rank >= unity_cup.BOSS_RANK:
        picks = list(unity_cup.boss_npc_ids(rank))[:total]
    elif rank >= 8:
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

    if rank >= unity_cup.BOSS_RANK and len(picks) == 15:
        out = [{"distance_type": g + 1, "member_id": m + 1,
                "base_npc_id": unity_cup.boss_art_substitute(npc),
                "npc_id": unity_cup.boss_art_substitute(npc),
                "running_style": unity_cup.npc_running_style(npc)}
               for g in range(5)
               for m, npc in enumerate((picks[2 * g], picks[2 * g + 1],
                                        picks[10 + g]))]
        out.sort(key=lambda t: (t["distance_type"], t["member_id"]))
        return out

    sizes = [1] * 5
    shape = random.Random("%s:shape" % set_id)
    for _ in range(max(0, min(len(picks), 15) - 5)):
        candidates = [g for g in range(5) if sizes[g] < 3]
        sizes[shape.choice(candidates)] += 1

    out, i = [], 0
    for member_id in (1, 2, 3):
        for group in range(5):
            if sizes[group] < member_id or i >= len(picks):
                continue
            npc_id = picks[i]
            i += 1
            out.append({"distance_type": group + 1, "member_id": member_id,
                        "base_npc_id": unity_cup.boss_art_substitute(npc_id),
                        "npc_id": unity_cup.boss_art_substitute(npc_id),
                        "running_style": unity_cup.npc_running_style(npc_id)})
    out.sort(key=lambda t: (t["distance_type"], t["member_id"]))
    return out


def handle_team_edit(payload: dict) -> dict:
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    submitted = payload.get("team_data_array")
    if isinstance(submitted, list) and submitted:
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
        st["lineup_saved"] = True
    response = _envelope(full_state, career, "team_edit")
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_team_race_analyze(payload: dict) -> dict:
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
    return max(1.0, 3 * 600 + correction / 5.0)


_SET_RACE_COLUMNS = ("race_instance_id_1", "race_instance_id_2", "race_instance_id_3",
                     "race_instance_id_4", "race_instance_id_5")

FIELD_SIZE = 12
TEAM_RUNNERS = 3


def handle_team_race_start(payload: dict) -> dict:
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
    st["rank_before_race"] = int(st.get("team_rank") or 30)
    chara_info["playing_state"] = unity_cup.PLAYING_STATE_TEAM_RACE_RUNNING
    response = _envelope(full_state, career, "team_race_start")
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _offer_for(st: dict, chara_info: dict, set_id: int, turn: int) -> dict:
    offer = next((o for o in unity_cup.offers(st)
                  if int(o.get("team_race_set_id") or 0) == set_id), None)
    if offer is not None:
        return offer
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
    attempt = int(st.get("continue_used") or 0)
    rng = random.Random(f"{st.get('seed', 0)}:teamrace:{turn}:{attempt}")
    members = {m["chara_id"]: m for m in unity_cup.active_members(st)}
    lineup = unity_cup.lineup_of(st, chara_info)
    trainee = unity_cup.trainee_chara(chara_info)
    correction = int(race_set.get("status_correction") or 0)
    opponent_team = unity_cup.opponent_team_name(int(race_set["id"]))
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
        seats = [s for s in lineup
                 if s.get("distance_type") == distance_type][:TEAM_RUNNERS]
        if not seats:
            continue
        styles = {s["chara_id"]: s.get("running_style", 2) for s in lineup}
        paired = [(s, chara_info if s["chara_id"] == trainee
                   else members.get(s["chara_id"])) for s in seats]
        paired = [(s, m) for s, m in paired if m is not None]
        if not paired:
            continue
        if any(s["chara_id"] == trainee for s in seats):
            paired.sort(key=lambda sm: sm[1] is not chara_info)
        our_member_ids = [int(s.get("member_id") or 1) for s, _ in paired]
        ours = [m for _, m in paired]
        if ours[0] is chara_info:
            player = smt._career_player_race_chara(chara_info, viewer_id)
            smt.conditions.apply_race_buffs(player, None)
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
        on_track = {int(s.get("chara_id") or 0) for s, _ in paired}
        on_track |= {int((unity_cup.npc_row(int(t.get("npc_id") or 0)) or {})
                         .get("chara_id") or 0)
                     for t in their_seats}
        fillers = smt._mob_opponents(
            chara_info, max(0, FIELD_SIZE - 1 - len(extra) - len(theirs)),
            exclude_charas=on_track - {0, 1})
        opponents = theirs + fillers
        if not opponents:
            continue

        smt._dedupe_field_faces([player] + extra + opponents)
        for h in [player] + extra:
            h["team_id"] = 1
        for h in theirs:
            h["team_id"] = 2
        program_id = smt.master_data.query_one(
            "SELECT id FROM single_mode_program WHERE race_instance_id=? LIMIT 1", (race_instance_id,))
        cond = smt._career_race_conditions(program_id["id"], chara_info) if program_id else {"ground_condition": 1, "weather": 1, "season": 1}
        from ... import tuning
        tuning.scale_rivals(opponents)
        result = race_simulator.simulate_race(
            player, opponents, race_instance_id,
            ground_condition=cond["ground_condition"], weather=cond["weather"], season=cond["season"],
            seed=rng.randrange(2 ** 31),
            extra_player_charas=extra or None,
            single_mode=True, race_context={"race_type": 7})
        if result is None:
            continue

        horses = result["horses"]
        sim_results = result["sim_results"]
        gates = result.get("gate_assignment") or list(range(len(horses)))
        our_count = 1 + len(extra)
        team_ids = [1] * our_count + [2] * len(theirs)
        team_ids += [0] * (len(horses) - len(team_ids))
        npc_ids = [0] * our_count + [int(h.get("npc_id") or 0) for h in theirs]
        npc_ids += [int(h.get("single_mode_chara_id") or 0) if h.get("mob_id")
                    else 0 for h in fillers]
        npc_ids += [0] * (len(horses) - len(npc_ids))
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
                "finish_time": int(round(sim_results[i].get("finishTimeDisplay", sim_results[i].get("finishTimeRaw", 0)) * 10000)),
                "popularity": popularity[i],
            })

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
            "season": cond["season"], "weather": cond["weather"], "ground_condition": cond["ground_condition"],
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


_TRAINEE_APTITUDE = {1: "proper_distance_short", 2: "proper_distance_mile",
                     3: "proper_distance_middle", 4: "proper_distance_long",
                     5: "proper_distance_middle"}


def _trainee_distance_group(chara_info: dict) -> int:
    return max(_TRAINEE_APTITUDE,
               key=lambda d: chara_info.get(_TRAINEE_APTITUDE[d], 1))


def _rank_map(horses: list, key) -> dict:
    order = sorted(range(len(horses)), key=lambda i: key(horses[i]))
    return {idx: rank + 1 for rank, idx in enumerate(order)}


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
    npc_id = int(entry.get("npc_id") or 0)
    row = unity_cup.npc_row(npc_id) or {}
    mob_id = int(row.get("mob_id") or 0)
    stat_row = unity_cup.npc_row(
        unity_cup.boss_stat_source(npc_id, set_rank)) or row
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
        "race_dress_id": (race_mobs.mob_dress(mob_id) if mob_id
                          else int(row.get("race_dress_id") or 1)),
        "npc_type": 20,
        "npc_id": npc_id,
        "running_style": int(entry.get("running_style")
                             or unity_cup.npc_running_style(npc_id)),
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
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    _bank_pending_result(st)
    career["data"]["chara_info"]["playing_state"] = (
        unity_cup.PLAYING_STATE_TEAM_RACE_RESULT)
    response = _envelope(full_state, career, "team_race_end")
    response["data"]["add_music"] = _grant_team_race_song(full_state, st)
    smt.state_store.save_state(viewer_id, full_state)
    return response


def _bank_pending_result(st: dict) -> bool:
    pending = st.get("pending") or {}
    if pending:
        final = int(pending.get("final_win_type") or 3)
        set_id = int(pending.get("set_id") or 0)
        race_set = unity_cup.team_race_set(set_id) or {}
        offer = next((o for o in unity_cup.offers(st)
                      if o.get("team_race_set_id") == set_id), None)
        if offer:
            unity_cup.bank_race_award(
                st,
                new_rank=int(offer["win_up_rank"] if final == _RESULT_WIN else
                             offer["lose_down_rank"] if final == _RESULT_LOSE else
                             offer["draw_rank"]),
                set_rank=int(race_set.get("rank") or 0),
                won=final == _RESULT_WIN,
                final=len(st.get("races") or ()) + 1 >= len(unity_cup.TEAM_RACE_TURNS),
                result=final)
        unity_cup.note_elite_result(st, set_id, final == _RESULT_WIN)
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
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    turn = int(payload.get("current_turn") or career["data"]["chara_info"].get("turn") or 0)
    _bank_pending_result(st)
    career["data"]["chara_info"]["playing_state"] = unity_cup.PLAYING_STATE_EVENT_CHAIN
    response = _envelope(full_state, career, "team_race_out")
    events = _take_post_race_events(st, turn, full_state)
    if events:
        response["data"]["unchecked_event_array"] = events
    smt.state_store.save_state(viewer_id, full_state)
    return response


def handle_save_team_edit_flag(payload: dict) -> dict:
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


def handle_team_race_end_out(payload: dict) -> dict:
    viewer_id, full_state, career = _career(payload)
    if career is None:
        return _refused()
    smt = _smt()
    st = unity_cup.state(full_state)
    turn = int(payload.get("current_turn") or career["data"]["chara_info"].get("turn") or 0)
    _bank_pending_result(st)
    career["data"]["chara_info"]["playing_state"] = unity_cup.PLAYING_STATE_EVENT_CHAIN
    data = {"chara_info": copy.deepcopy(career["data"]["chara_info"]),
            "tmp_team_rank": unity_cup.pending_rank(st),
            "unchecked_event_array": _take_post_race_events(st, turn, full_state),
            "add_music": _grant_team_race_song(full_state, st)}
    smt.state_store.save_state(viewer_id, full_state)
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _grant_team_race_song(full_state: dict, st: dict):
    if not unity_cup.won_final(st):
        return None
    from ... import concerts
    return concerts.on_scenario(full_state, "uc_finals")


def _take_post_race_events(st: dict, turn: int, full_state: dict | None = None) -> list:
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
    st[unity_cup.POST_RACE_LAST_KEY] = out[-1]["event_id"] if out else None
    st[unity_cup.POST_RACE_CHAIN_KEY] = copy.deepcopy(out)
    if full_state is not None and any(
            e["event_id"] == producers.URA_INVITE_EVENT for e in out):
        CE.mark_fired(full_state, producers.URA_INVITE_KEY)
    return out[:1]


def handle_team_race_continue(payload: dict) -> dict:
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

    attempt = banked if banked is not None else pending
    if banked is not None:
        races.pop()
    st["team_rank"] = int(st.get("rank_before_race") or st.get("team_rank") or 30)
    st["rank_state"] = 0
    st["last_result"] = None
    st.pop("pending_award", None)

    chara_info = career["data"]["chara_info"]
    turn = int(attempt.get("turn") or payload.get("current_turn") or 0)
    st["continue_used"] = int(st.get("continue_used") or 0) + 1
    set_id = int(attempt.get("team_race_set_id") or attempt.get("set_id") or 0)
    race_set = unity_cup.team_race_set(set_id)
    if race_set:
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
