
from __future__ import annotations

import functools
import random

from .. import master_data
from .. import patch
from .. import region
from .. import state as state_store
from . import missions, registry, shop
from .stories import _grant_reward
from ..result_codes import ResultCode, refuse

EVENT_STATE_KEY = "story_event_state"

_BOARD_SIZE = 9
_FREE_CENTER = 4
_BOARD_LINES: tuple = tuple(
    [tuple(r * 3 + c for c in range(3)) for r in range(3)] +
    [tuple(r * 3 + c for r in range(3)) for c in range(3)] +
    [tuple(i * 3 + i for i in range(3)),
     tuple(i * 3 + (2 - i) for i in range(3))])

_POSITION_TO_ORDER = {0: 5, 1: 2, 2: 6, 3: 3, 5: 1, 6: 4, 7: 8, 8: 7}
_LANDABLE_POSITIONS = tuple(_POSITION_TO_ORDER)

_ROULETTE_REWARD_TABLE = [
    {"item_category": 30, "item_id": 110, "item_num": 100},
    {"item_category": 103, "item_id": 98, "item_num": 50},
    {"item_category": 91, "item_id": 59, "item_num": 200},
    {"item_category": 30, "item_id": 110, "item_num": 50},
    {"item_category": 103, "item_id": 98, "item_num": 20},
    {"item_category": 91, "item_id": 59, "item_num": 100},
    {"item_category": 11, "item_id": 13, "item_num": 1},
    {"item_category": 11, "item_id": 7, "item_num": 1},
]


def _jp_wheel(shoe5: int, shoe6: int, shoe7: int) -> list:
    return [
        {"item_category": 103, "item_id": 98, "item_num": 100},
        {"item_category": 91, "item_id": 59, "item_num": 3000},
        {"item_category": 103, "item_id": 98, "item_num": 40},
        {"item_category": 91, "item_id": 59, "item_num": 1500},
        {"item_category": 11, "item_id": shoe5, "item_num": 1},
        {"item_category": 11, "item_id": shoe6, "item_num": 1},
        {"item_category": 11, "item_id": shoe7, "item_num": 1},
        {"item_category": 11, "item_id": 10, "item_num": 1},
    ]


_WHEEL_BY_EVENT = {
    "global": {},
    "jp": {1055: _jp_wheel(73, 78, 7), 1056: _jp_wheel(69, 74, 13),
           1057: _jp_wheel(73, 78, 7)},
}
_WHEEL_DEFAULT = {"global": _ROULETTE_REWARD_TABLE, "jp": _jp_wheel(73, 78, 7)}


def _wheel(story_event_id: int) -> list:
    name = region.CURRENT_REGION.get()
    return _WHEEL_BY_EVENT.get(name, {}).get(story_event_id) or _WHEEL_DEFAULT.get(
        name, _ROULETTE_REWARD_TABLE)

_CONTINUOUS_NONE = 0
_CONTINUOUS_UNTIL_1_LINE = 1
_CONTINUOUS_UNTIL_8_LINE = 2
_CONTINUOUS_UNTIL_10_PLAY = 3

_STOP_NONE = 0
_STOP_COIN_EMPTY = 1
_STOP_MAX_NUM = 2
_STOP_UNTIL_NONE = 3
_STOP_PLAY_COUNT_10 = 4
_STOP_ONE_LINE_BINGO = 5
_STOP_MAX_LINE = 6


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _all_events() -> tuple:
    return tuple(master_data.query("SELECT * FROM story_event_data"))


def _active_event_id(now_ts: int) -> int | None:
    for row in _all_events():
        if row["start_date"] <= now_ts <= row["end_date"]:
            return row["story_event_id"]
    return None


def _sheets(story_event_id: int) -> tuple:
    return tuple(master_data.query(
        "SELECT * FROM story_event_roulette_bingo WHERE story_event_id=? "
        "ORDER BY sheet_num", (story_event_id,)))


def _sheet_row(story_event_id: int, sheet_num: int):
    row = None
    for s in _sheets(story_event_id):
        if s["sheet_num"] <= sheet_num:
            row = s
    if row is None or (row["sheet_num"] != sheet_num and not row["can_loop"]):
        return None
    return row


@master_data.context_cache(maxsize=64)
def _bingo_rewards(reward_set_id: int) -> tuple:
    return tuple(master_data.query(
        "SELECT * FROM story_event_bingo_reward WHERE reward_set_id=? "
        "ORDER BY line_num", (reward_set_id,)))


def _reward_preview(reward_set_id: int) -> list:
    return [{"reward_order": r["line_num"], "item_category": r["item_category"],
             "item_id": r["item_id"], "item_num": r["item_num"]}
            for r in _bingo_rewards(reward_set_id)]


def _roulette_reward_preview(story_event_id: int) -> list:
    table = _wheel(story_event_id)
    return [{"reward_order": i + 1, **table[i]} for i in range(len(table))]


def _fresh_board() -> list:
    board = [0] * _BOARD_SIZE
    board[_FREE_CENTER] = 1
    return board


def _event_state(full_state: dict, story_event_id: int) -> tuple[dict, bool]:
    store = full_state.setdefault(EVENT_STATE_KEY, {})
    key = str(story_event_id)
    first_access = key not in store
    st = store.setdefault(key, {
        "current_sheet": 1, "board": _fresh_board(),
        "lines_done": [], "roulette_exec_count": 0,
        "bingo_history": {},
    })
    return st, first_access


def _completed_lines(board: list) -> set:
    return {i for i, line in enumerate(_BOARD_LINES) if all(board[p] for p in line)}


def _order_numbers(board: list) -> list:
    return sorted(_POSITION_TO_ORDER[p] for p in _LANDABLE_POSITIONS if board[p])


def _event_user_info(full_state: dict, story_event_id: int, sheet_num: int) -> dict:
    return {"event_point": full_state.get("story_event_point", {}).get(str(story_event_id), 0),
            "roulette_coin_num": _coin_count(full_state, story_event_id, sheet_num),
            "roulette_sheet_num": sheet_num}


def _coin_count(full_state: dict, story_event_id: int, sheet_num: int) -> int:
    row = _sheet_row(story_event_id, sheet_num)
    if not row:
        return 0
    return shop._item_count(full_state, row["use_item_id"])


def _bingo_info_list(st: dict, row) -> list:
    cur = row["sheet_num"] if row is not None else None
    out = [{"sheet_num": int(k), "line_num": len(_BOARD_LINES)}
           for k in sorted(st["bingo_history"], key=int) if int(k) != cur]
    if cur is not None and (st["roulette_exec_count"] or st["lines_done"]):
        out.append({"sheet_num": cur, "line_num": len(st["lines_done"])})
    return out


def _advance_if_cleared(full_state, story_event_id: int, st: dict, row) -> tuple:
    if len(st["lines_done"]) < len(_BOARD_LINES):
        return None, []
    st["bingo_history"][str(row["sheet_num"])] = len(_BOARD_LINES)
    st["current_sheet"] += 1
    st["board"] = _fresh_board()
    st["lines_done"] = []
    st["roulette_exec_count"] = 0
    return st["current_sheet"], _roulette_reward_preview(story_event_id)


def overlay_login(data: dict, full_state: dict, viewer_id) -> None:
    if "story_event_id" not in data:
        return
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        data["story_event_id"] = 0
        data["story_event_first_flag"] = False
        data["story_event_mission_list"] = []
        data["story_event_roulette_coin_num"] = 0
        return
    store = full_state.get(EVENT_STATE_KEY) or {}
    sheet = (store.get(str(event_id)) or {}).get("current_sheet", 1)
    data["story_event_id"] = event_id
    data["story_event_first_flag"] = str(event_id) not in store
    data["story_event_mission_list"] = event_missions(viewer_id, full_state, event_id)
    data["story_event_roulette_coin_num"] = _coin_count(full_state, event_id, sheet)


@registry.endpoint("story_event/index")
def handle_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    now_ts = patch._servertime()
    event_id = _active_event_id(now_ts)
    if event_id is None:
        return _ok({"event_id": 0, "event_user_info": {}, "mission_list": [],
                    "story_data_list": [], "new_story_id_list": [], "first_access_flag": False})
    st, first = _event_state(full_state, event_id)
    data = {
        "event_id": event_id,
        "event_user_info": _event_user_info(full_state, event_id, st["current_sheet"]),
        "mission_list": event_missions(viewer_id, full_state, event_id),
        "story_data_list": _story_data(full_state, event_id),
        "new_story_id_list": [],
        "first_access_flag": first,
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(data)


@registry.endpoint("story_event/roulette")
def handle_roulette(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_IN_TERM)
    st, first_access = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_DATA)
    data = {
        "event_user_info": _event_user_info(full_state, event_id, st["current_sheet"]),
        "user_roulette_order_list": _order_numbers(st["board"]),
        "roulette_reward_list": _roulette_reward_preview(event_id),
        "bingo_info": _bingo_info_list(st, row),
        "first_access_flag": first_access,
        "roulette_exec_count": st["roulette_exec_count"],
    }
    state_store.save_state(viewer_id, full_state)
    return _ok(data)


_SAFETY_ROLL_CAP = 64
_CONTINUOUS_MAX_ROLLS = {_CONTINUOUS_NONE: 1, _CONTINUOUS_UNTIL_10_PLAY: 10}


@registry.endpoint("story_event/roulette_exec")
def handle_roulette_exec(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_IN_TERM)
    full_state = state_store.get_state(viewer_id) or {}
    st, _ = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_DATA)

    mode = int(payload.get("continuous_setting") or _CONTINUOUS_NONE)
    reset_line = len(_BOARD_LINES)
    cost_each = row["use_item_num"] or 1
    pity_threshold = row["roulette_max_num"] or 25
    max_rolls = _CONTINUOUS_MAX_ROLLS.get(mode, _SAFETY_ROLL_CAP)
    if shop._item_count(full_state, row["use_item_id"]) < cost_each:
        return _refuse()

    started_pre_pity = st["roulette_exec_count"] < pity_threshold

    summary = shop._empty_summary()
    order_list = []
    roulette_reward_list = []
    new_bingo_line_array = []
    bingo_reward_list = []
    rolled = 0
    stop_type = _STOP_NONE
    while rolled < max_rolls:
        unmarked = [i for i in _LANDABLE_POSITIONS if not st["board"][i]]
        if not unmarked:
            stop_type = _STOP_MAX_LINE
            break
        if shop._item_count(full_state, row["use_item_id"]) < cost_each:
            stop_type = _STOP_COIN_EMPTY
            break
        shop._add_item(full_state, row["use_item_id"], -cost_each)
        if st["roulette_exec_count"] >= pity_threshold:
            landed = random.choice(unmarked)
        else:
            landed = random.choice(_LANDABLE_POSITIONS)
        st["board"][landed] = 1
        st["roulette_exec_count"] += 1
        rolled += 1
        order_list.append(_POSITION_TO_ORDER[landed])

        prize = _wheel(event_id)[_POSITION_TO_ORDER[landed] - 1]
        _grant_reward(full_state, prize["item_category"], prize["item_id"],
                      prize["item_num"], summary, viewer_id)
        roulette_reward_list.append({"item_type": prize["item_category"],
                                     "item_id": prize["item_id"], "item_num": prize["item_num"]})

        completed_line_indexes = sorted(_completed_lines(st["board"]) - set(st["lines_done"]))
        completed_a_new_line = bool(completed_line_indexes)
        for line_idx in completed_line_indexes:
            if len(st["lines_done"]) >= reset_line:
                break
            st["lines_done"].append(line_idx)
            line_num = len(st["lines_done"])
            reward_row = next((r for r in _bingo_rewards(row["reward_set_id"])
                               if r["line_num"] == line_num), None)
            new_bingo_line_array.append(line_idx + 1)
            if reward_row is None:
                continue
            _grant_reward(
                full_state, reward_row["item_category"], reward_row["item_id"],
                reward_row["item_num"], summary, viewer_id)
            bingo_reward_list.append({"item_type": reward_row["item_category"],
                                      "item_id": reward_row["item_id"],
                                      "item_num": reward_row["item_num"]})

        if len(st["lines_done"]) >= reset_line:
            stop_type = _STOP_MAX_LINE
            break
        if mode == _CONTINUOUS_UNTIL_1_LINE and completed_a_new_line:
            stop_type = _STOP_ONE_LINE_BINGO
            break
        if started_pre_pity and st["roulette_exec_count"] >= pity_threshold:
            stop_type = _STOP_MAX_NUM
            break
    else:
        stop_type = {_CONTINUOUS_UNTIL_10_PLAY: _STOP_PLAY_COUNT_10,
                    _CONTINUOUS_NONE: _STOP_UNTIL_NONE}.get(mode, _STOP_NONE)

    change_sheet_num, change_reward_list = _advance_if_cleared(
        full_state, event_id, st, row)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "order_list": order_list,
        "reward_summary_info": summary,
        "roulette_reward_list": roulette_reward_list,
        "new_bingo_line_array": new_bingo_line_array,
        "bingo_reward_list": bingo_reward_list,
        "change_sheet_num": change_sheet_num,
        "change_roulette_reward_list": change_reward_list,
        "roulette_exec_count": st["roulette_exec_count"],
        "roulette_stop_type": stop_type,
    })


@registry.endpoint("story_event/roulette_change_sheet")
def handle_roulette_change_sheet(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_IN_TERM)
    full_state = state_store.get_state(viewer_id) or {}
    st, _ = _event_state(full_state, event_id)
    row = _sheet_row(event_id, st["current_sheet"])
    if row is None or not row["can_loop"]:
        return _refuse()
    st["board"] = _fresh_board()
    st["lines_done"] = []
    st["roulette_exec_count"] = 0
    state_store.save_state(viewer_id, full_state)
    return _ok({
        "change_sheet_num": st["current_sheet"],
        "change_roulette_reward_list": _roulette_reward_preview(event_id),
    })


@functools.lru_cache(maxsize=None)
def _max_point_threshold(story_event_id: int) -> int:
    row = master_data.query_one(
        "SELECT MAX(point) AS p FROM story_event_point_reward WHERE story_event_id=?",
        (story_event_id,))
    return (row["p"] if row and row["p"] else 0)


def _all_point_rewards_reached(story_event_id: int, event_point: int) -> bool:
    top = _max_point_threshold(story_event_id)
    return bool(top) and event_point >= top


def _point_rewards(full_state, event_id, viewer_id, summary):
    points = full_state.get("story_event_point", {}).get(str(event_id), 0)
    claimed = full_state.setdefault("story_event_point_claims", {}).setdefault(str(event_id), [])
    rewards, ids = [], []
    for row in master_data.query("SELECT * FROM story_event_point_reward WHERE story_event_id=? AND point<=? ORDER BY point,id", (event_id, points)):
        if row["id"] in claimed:
            continue
        rewards += _grant_reward(full_state, row["item_category"], row["item_id"], row["item_num"], summary, viewer_id)
        claimed.append(row["id"])
        ids.append(row["id"])
    return rewards, ids


def _story_data(full_state, event_id):
    from .stories import STORY_STATE_KEY
    cleared = (full_state.get(STORY_STATE_KEY) or {}).get("story_event_cleared", [])
    return [{"episode_id": r["id"], "state": int(r["id"] in cleared)}
            for r in master_data.query("SELECT * FROM story_event_story_data WHERE story_event_id=? ORDER BY episode_index_id", (event_id,))]


def _unlocked_stories(full_state, event_id):
    points = full_state.get("story_event_point", {}).get(str(event_id), 0)
    now = patch._servertime()
    return {r["id"] for r in master_data.query("SELECT * FROM story_event_story_data WHERE story_event_id=?", (event_id,))
            if r["need_point"] <= points and r["start_date"] <= now}


def settle_career(full_state, record, viewer_id, start_chara=None):
    import math
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return None
    event = master_data.query_one("SELECT * FROM story_event_data WHERE story_event_id=?", (event_id,))
    if "ending_date" in event.keys() and patch._servertime() > event["ending_date"]:
        return None
    settled = full_state.setdefault("story_event_career_claims", {}).setdefault(str(event_id), {})
    key = str(record["trained_chara_id"])
    if key in settled:
        return settled[key]
    now = patch._servertime()
    card_bonus = 0
    for row in master_data.query("SELECT * FROM story_event_bonus_card WHERE story_event_id=? AND start_date<=? AND end_date>=?", (event_id, now, now)):
        card = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (record["card_id"],))
        if (row["card_id"] and row["card_id"] == record["card_id"]) or (not row["card_id"] and card and row["chara_id"] == card[0]):
            card_bonus += row[f'rarity_{max(1, min(5, int(record.get("rarity") or 1)))}']
    support_bonus = 0
    for support in record.get("support_card_list", []):
        data = master_data.query_one("SELECT chara_id,rarity FROM support_card_data WHERE id=?", (support["support_card_id"],))
        if data is None:
            continue
        for table in ("story_event_bonus_support_card", "story_event_bonus_group_support_card"):
            for row in master_data.query(f"SELECT * FROM {table} WHERE story_event_id=? AND start_date<=? AND end_date>=?", (event_id, now, now)):
                if row["support_card_id"]:
                    matches = row["support_card_id"] == support["support_card_id"]
                else:
                    matches = row["chara_id"] == data[0] and ("rarity" not in row.keys() or row["rarity"] == data[1])
                if matches:
                    support_bonus += row[f'limit_{max(0, min(4, int(support.get("limit_break_count") or 0)))}']
    nickname = master_data.query_one("SELECT rank FROM nickname WHERE id=?", (record.get("nickname_id", 0),))
    bonus = master_data.query_one("SELECT bonus_point FROM story_event_nickname_bonus WHERE story_event_id=? AND nickname_rank=?", (event_id, nickname[0])) if nickname else None
    nickname_bonus = int(bonus[0]) if bonus else 0
    wins = max(0, int(record.get("wins") or 0))
    win_bonus = min(wins, 10) * 50 + min(max(wins - 10, 0), 10) * 30 + min(max(wins - 20, 0), 10) * 20
    total = math.ceil((record["rank_score"] + nickname_bonus + win_bonus) * (100 + card_bonus + support_bonus) / 100)
    boosted = (start_chara or {}).get("boost_story_event_id") == event_id
    if boosted:
        total = total * event["bonus_reward_ratio"] // 10000
    before = _unlocked_stories(full_state, event_id)
    full_state.setdefault("story_event_point", {}).setdefault(str(event_id), 0)
    full_state["story_event_point"][str(event_id)] += total
    summary = shop._empty_summary()
    rewards, ids = _point_rewards(full_state, event_id, viewer_id, summary)
    after = _story_data(full_state, event_id)
    st, _ = _event_state(full_state, event_id)
    result = {"event_id": event_id, "event_user_info": _event_user_info(full_state, event_id, st["current_sheet"]),
              "add_point_info": {"total": total, "rank_score": record["rank_score"], "nickname_bonus": nickname_bonus,
                                 "card_bonus": card_bonus, "support_card_bonus": support_bonus, "win_num_bonus": win_bonus},
              "reward_summary_info": summary, "reward_list": rewards, "point_reward_id_list": ids,
              "get_all_point_reward": _all_point_rewards_reached(event_id, full_state["story_event_point"][str(event_id)]),
              "training_reward_list": [], "additional_training_reward_list": [],
              "new_story_id_list": sorted(_unlocked_stories(full_state, event_id) - before),
              "story_data_list": after, "is_boost": int(boosted)}
    settled[key] = result
    return result


def _mission_board(full_state, event_id):
    from .daily_races import _served_day
    board = full_state.setdefault("story_event_missions", {}).setdefault(str(event_id),
        {"claimed": [], "baseline": {}})
    today = _served_day()
    if board.get("day") != today:
        board["day"] = today
        board["daily"] = {"claimed": [], "baseline": {}}
    return board


def _event_mission_rows(event_id):
    rows = master_data.query("SELECT * FROM story_event_mission WHERE story_event_id=? ORDER BY disp_order,id", (event_id,))
    now = patch._servertime()
    return [dict(r) for r in rows if not r["start_date"] or
            (missions._parse_mdb_datetime(r["start_date"]) or 0) <= now]


def _event_mission_count(viewer_id, full_state, row, board, rows):
    bucket = board["daily"] if row["mission_type"] == 1 else board
    if row["id"] in bucket["claimed"]:
        return row["condition_num"]
    if row["condition_type"] == 900001:
        return min(row["condition_num"], sum(
            _event_mission_count(viewer_id, full_state, other, board, rows) >= other["condition_num"]
            for other in rows if other["mission_type"] == row["condition_value_1"]
            and other["condition_type"] != 900001))
    if row["condition_type"] == 600022:
        return min(row["condition_num"], missions._exec_count_for(viewer_id, full_state, row))
    lifetime = missions._exec_count_for(viewer_id, full_state, row)
    baseline = bucket["baseline"].setdefault(str(row["id"]), lifetime)
    return min(row["condition_num"], max(0, lifetime - baseline))


def event_missions(viewer_id, full_state, event_id):
    board, rows = _mission_board(full_state, event_id), _event_mission_rows(event_id)
    result = []
    for row in rows:
        bucket = board["daily"] if row["mission_type"] == 1 else board
        count = _event_mission_count(viewer_id, full_state, row, board, rows)
        status = 2 if row["id"] in bucket["claimed"] else int(count >= row["condition_num"])
        result.append({"mission_id": row["id"], "exec_count": count,
                       "mission_status": status, "mission_type": row["mission_type"], "event_id": 0})
    return result


def _claim_event_missions(viewer_id, full_state, event_id, ids, summary):
    board, rows = _mission_board(full_state, event_id), _event_mission_rows(event_id)
    by_id = {r["id"]: r for r in rows}
    if any(type(mid) is not int or mid not in by_id for mid in ids):
        return None
    if any(_event_mission_count(viewer_id, full_state, by_id[mid], board, rows) <
           by_id[mid]["condition_num"] for mid in ids):
        return None
    updated = []
    for mid in dict.fromkeys(ids):
        row = by_id[mid]
        bucket = board["daily"] if row["mission_type"] == 1 else board
        if mid not in bucket["claimed"]:
            if row["item_category"] == 100:
                full_state.setdefault("story_event_point", {}).setdefault(str(event_id), 0)
                full_state["story_event_point"][str(event_id)] += row["item_num"]
            else:
                _grant_reward(full_state, row["item_category"], row["item_id"], row["item_num"], summary, viewer_id)
            bucket["claimed"].append(mid)
        updated.append({"mission_id": mid, "exec_count": row["condition_num"],
                        "mission_status": 2, "condition_type": row["condition_type"]})
    return updated


@registry.endpoint("story_event/announce")
def handle_announce(payload: dict) -> dict:
    if _active_event_id(patch._servertime()) is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_IN_TERM)
    return _ok({"content": ""})


@registry.endpoint("story_event/receive_mission")
def handle_receive_mission(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    ids = payload.get("mission_id_array")
    if not isinstance(ids, list) or not ids:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    event_id = _active_event_id(patch._servertime())
    if event_id is None:
        return _refuse(ResultCode.STORY_EVENT_NOT_IN_TERM)
    st, _ = _event_state(full_state, event_id)
    sheet_num = st["current_sheet"]

    points_before = (full_state.get("story_event_point") or {}).get(str(event_id), 0)
    coins_before = _coin_count(full_state, event_id, sheet_num)
    unlocked_before = _unlocked_stories(full_state, event_id)

    summary = shop._empty_summary()
    updated = _claim_event_missions(viewer_id, full_state, event_id, ids, summary)
    if updated is None:
        return _refuse()

    _point_rewards(full_state, event_id, viewer_id, summary)

    points_after = (full_state.get("story_event_point") or {}).get(str(event_id), 0)
    coins_after = _coin_count(full_state, event_id, sheet_num)

    state_store.save_state(viewer_id, full_state)
    return _ok({
        "reward_summary_info": summary,
        "updated_mission_array": updated,
        "add_event_point": points_after - points_before,
        "add_roulette_coin_num": coins_after - coins_before,
        "new_story_id_list": sorted(_unlocked_stories(full_state, event_id) - unlocked_before),
        "get_all_point_reward": _all_point_rewards_reached(event_id, points_after),
    })
