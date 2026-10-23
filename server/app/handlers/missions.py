
from __future__ import annotations

import functools
import logging

from .. import master_data
from .. import patch
from .. import state as state_store
from . import bond, collection, login_bonus, registry, shop, stories, trained_chara
from .stories import _grant_reward
from ..result_codes import ResultCode, refuse

log = logging.getLogger("uma-server")

MISSION_STATE_KEY = "mission_state"
ACHIEVEMENT_FLAG_KEY = "mission_achievement_flags"


def mark_achieved(full_state: dict, flag: str) -> None:
    flags = full_state.setdefault(ACHIEVEMENT_FLAG_KEY, [])
    if flag not in flags:
        flags.append(flag)


_COND_SINGLE_MODE_RACE_WIN = 100003
_COND_COMPLETE_CAREER = 100004
_COND_G1_WINS = 100007
_COND_CAREER_WIN_THRESHOLD = 100012
_COND_CAREER_FAN_THRESHOLD = 100028
_COND_SUPPORT_CARD_LIMIT_BREAK = 300002
_COND_UNLOCK_STAR = 300003
_COND_POTENTIAL_LEVEL = 300004
_COND_UNLOCK_STAR_COUNT = 300005
_COND_TEAM_RANK = 300006
_COND_ADD_TRAINEES = 400001
_COND_SUPPORT_CARD_KIND_NUM = 400002
_COND_VIEW_CHARA_STORY = 500001
_COND_VIEW_MAIN_STORY = 500002
_COND_FAN_NUM = 600013
_COND_BOND_LEVEL_CHARA = 600014
_COND_BOND_LEVEL_ANY = 600020
_COND_FAN_THRESHOLD_COUNT = 600021
_COND_LOGIN_DAYS_TOTAL = 600001
_COND_LOGIN_TODAY = 600022
_COND_SKILL_COUNT_THRESHOLD = 100041
_COND_SPECIFIC_SKILL_ACQUIRED = 100038
_COND_EPITHET_AND_CAREER_COMPLETE = 100037
_COND_G1_TROPHY_COUNT = 100005
_COND_RAISE_SKILL_HINT = 300007
_COND_VIEW_DISTINCT_STORIES = 500003
_COND_BOND_LEVEL_THRESHOLD_COUNT = 600038
_COND_CHANGE_TITLE = 600009
_COND_CHANGE_HOME_COMPANIONS = 600010
_COND_CHANGE_PROFILE_CHARA = 600011
_COND_LINK_APP_DATA = 600012
_COND_EDIT_TRAINER_CARD = 600031
_COND_JUKEBOX_REQUEST = 600030
_COND_PRACTICE_RACE_PARTICIPATE = 200028
_COND_CLAW_MACHINE_PLAY_COUNT = 100022
_COND_CLAW_MACHINE_PLUSHIE_COUNT = 100051
_COND_CONCERT_SUCCESS_COUNT = 100074
_COND_CONCERT_ALL_GREAT = 100070
_COND_SPECIFIC_SONG_OBTAINED = 100077
_COND_SONG_COUNT_THRESHOLD = 100079
_COND_CLAW_MACHINE_PLUSHIE_SESSION = 100023
_COND_SONGS_BEFORE_FIRST_CONCERT = 100075
_COND_WATCH_SPECIFIC_SONG = 600006
_COND_WATCH_UNIQUE_CONCERTS = 600033
_COND_CLASSIC_TRIPLE_CROWN = 100010
_COND_BIG_EIGHT = 100020
_COND_URA_FINALE_WIN = 100019
_COND_ALL_TROPHIES_FOR_CHARA = 100056
_COND_FOLLOW_COUNT = 600002
_COND_FOLLOWER_COUNT = 600003
_COND_JOIN_CIRCLE = 600004
_COND_CIRCLE_SHARE_PARTNER = 600024
_COND_CIRCLE_CHECK = 600026
_COND_WIN_UNITY_CUP = 100033
_COND_DAILY_RACE_ENTRIES = 200008
_COND_CLEAR_MISSIONS = 900001

FLAG_TITLE_CHANGED = "title_changed"
FLAG_HOME_COMPANIONS_CHANGED = "home_companions_changed"
FLAG_TRAINER_CARD_EDITED = "trainer_card_edited"
FLAG_APP_DATA_LINKED = "app_data_linked"
FLAG_JUKEBOX_REQUESTED = "jukebox_song_requested"
FLAG_PRACTICE_RACE_RUN = "practice_race_participated"
FLAG_CIRCLE_JOINED = "circle_joined"
FLAG_CIRCLE_CHECKED = "circle_checked"
FLAG_CIRCLE_PARTNER_SHARED = "circle_partner_shared"
_G1_GRADE = 100

_STATUS_NOT_CLEAR = 0
_STATUS_CLEAR = 1
_STATUS_GOT_REWARD = 2


def _ok(data: dict) -> dict:
    return {"response_code": 1,
            "data_headers": {"result_code": 1, "notifications": {}}, "data": data}


def _refuse(code: int = ResultCode.PARAM_ERROR) -> dict:
    return refuse(code)


def _mission_claimed_set(full_state: dict) -> set:
    st = full_state.setdefault(MISSION_STATE_KEY, {})
    return set(st.setdefault("claimed", []))


def _save_claimed(full_state: dict, claimed: set) -> None:
    full_state.setdefault(MISSION_STATE_KEY, {})["claimed"] = sorted(claimed)


_DAILY_MISSION_TYPES = (1, 5)
_MISSION_TYPE_DAILY_TAB = 1


def _daily_mission_state(full_state: dict) -> dict:
    from . import daily_races
    today = daily_races._served_day()
    st = full_state.setdefault(MISSION_STATE_KEY, {})
    daily = st.setdefault("daily", {})
    if daily.get("day") != today:
        daily["day"] = today
        daily["baseline"] = {}
        daily["claimed"] = []
    return daily


def _daily_exec_count(viewer_id, full_state: dict, row, daily_state: dict) -> int:
    mid_key = str(row["id"])
    lifetime = _exec_count_for(viewer_id, full_state, row)
    baseline = daily_state.setdefault("baseline", {})
    if mid_key not in baseline:
        baseline[mid_key] = lifetime
    return max(0, lifetime - baseline[mid_key])


def _count_for(viewer_id, full_state: dict, row, daily_state: dict) -> int:
    if row["condition_type"] == _COND_CLEAR_MISSIONS:
        count = _cleared_today_count(viewer_id, full_state, row, daily_state)
    elif row["mission_type"] in _DAILY_MISSION_TYPES:
        count = _daily_exec_count(viewer_id, full_state, row, daily_state)
    else:
        count = _exec_count_for(viewer_id, full_state, row)
    return min(count, row["condition_num"])


def _cleared_today_count(viewer_id, full_state: dict, row, daily_state: dict) -> int:
    if (row["condition_value_1"] or 0) != _MISSION_TYPE_DAILY_TAB:
        return 0
    now_ts = patch._servertime()
    claimed_today = set(daily_state.get("claimed") or [])
    n = 0
    for other in _daily_tab_rows():
        if other["condition_type"] == _COND_CLEAR_MISSIONS or not _is_active(other, now_ts):
            continue
        if other["id"] in claimed_today or \
                _daily_exec_count(viewer_id, full_state, other, daily_state) >= other["condition_num"]:
            n += 1
    return n


def _daily_tab_rows() -> list:
    return master_data.query("SELECT * FROM mission_data WHERE mission_type=?",
                             (_MISSION_TYPE_DAILY_TAB,))


@functools.lru_cache(maxsize=8192)
def _parse_mdb_datetime(value) -> int | None:
    if not value:
        return None
    import datetime
    try:
        dt = datetime.datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=mdb_tz()).timestamp())


def mdb_tz():
    import datetime
    from .. import region
    hours = 9 if region.PROCESS_REGION == "jp" else 0
    return datetime.timezone(datetime.timedelta(hours=hours))


def _is_active(row, now_ts: int) -> bool:
    if row["condition_type"] == 200012:
        from .. import champions_meeting
        cm = champions_meeting.ticket_mission_active(row, now_ts)
        if cm is not None:
            return cm
    if not row["date_check_flg"]:
        return True
    start = _parse_mdb_datetime(row["start_date"])
    end = _parse_mdb_datetime(row["end_date"])
    if start and now_ts < start:
        return False
    if end and now_ts > end:
        return False
    return True


def _genuine_roster(viewer_id, full_state: dict | None = None) -> list:
    roster = None
    if full_state is not None and (
            full_state.get(trained_chara.ROSTER_VERSION_KEY)
            == trained_chara.ROSTER_VERSION):
        roster = full_state.get(trained_chara.ROSTER_KEY)
    if roster is None:
        roster = trained_chara._get_or_seed_roster(viewer_id) or []
    return [c for c in roster
           if trained_chara.PLAYER_CAREER_ID_BASE
              <= (c.get("trained_chara_id") or 0) < trained_chara.INJECT_ID_BASE]


def _love_rank_level(love_point: int) -> int:
    return bond.love_rank(love_point)


_program_race_cache: dict | None = None


def _program_race_row(program_id) -> dict | None:
    global _program_race_cache
    if _program_race_cache is None:
        rows = master_data.query(
            "SELECT p.id AS program_id, ri.id AS inst, rc.id AS race_id, rc.grade AS grade "
            "FROM single_mode_program p "
            "JOIN race_instance ri ON ri.id = p.race_instance_id "
            "JOIN race rc ON rc.id = ri.race_id")
        _program_race_cache = {r["program_id"]: {"inst": r["inst"], "race_id": r["race_id"],
                                                  "grade": r["grade"]} for r in rows}
    return _program_race_cache.get(program_id)


_race_ids_by_name_cache: dict = {}


def _race_ids_by_name(name: str) -> frozenset:
    if name not in _race_ids_by_name_cache:
        rows = master_data.query(
            'SELECT "index" FROM text_data WHERE category=33 AND text=?', (name,))
        _race_ids_by_name_cache[name] = frozenset(r["index"] for r in rows)
    return _race_ids_by_name_cache[name]


def _won_race_ids(roster_entry) -> set:
    out = set()
    for pid in roster_entry.get("_won_program_ids") or ():
        row = _program_race_row(pid)
        if row:
            out.add(row["race_id"])
    return out


_trophy_instance_cache: frozenset | None = None


def _trophy_instance_ids() -> frozenset:
    global _trophy_instance_cache
    if _trophy_instance_cache is None:
        rows = master_data.query("SELECT race_instance_id FROM race_trophy")
        _trophy_instance_cache = frozenset(r["race_instance_id"] for r in rows)
    return _trophy_instance_cache


_finale_program_cache: frozenset | None = None


def _finale_program_ids() -> frozenset:
    global _finale_program_cache
    if _finale_program_cache is None:
        rows = master_data.query(
            "SELECT race_program_id FROM single_mode_race_group WHERE race_group_id=10003")
        _finale_program_cache = frozenset(r["race_program_id"] for r in rows)
    return _finale_program_cache


CONDITION_HOOKS: dict = {}


_chara_id_cache: dict = {}


def _chara_id_for_card(card_id) -> int | None:
    if card_id not in _chara_id_cache:
        row = master_data.query_one("SELECT chara_id FROM card_data WHERE id=?", (card_id,))
        _chara_id_cache[card_id] = row["chara_id"] if row else None
    return _chara_id_cache[card_id]


def _exec_count_for(viewer_id, full_state: dict, row) -> int:
    ctype = row["condition_type"]
    cval1 = row["condition_value_1"] or 0

    if ctype == 200012:
        from .champions import ENTRY_COUNT_KEY
        return int((full_state.get(ENTRY_COUNT_KEY) or {}).get(str(cval1), 0))

    if ctype == 200024:
        from .challenge_match import runs_in
        start = _parse_mdb_datetime(row["start_date"])
        end = _parse_mdb_datetime(row["end_date"])
        return runs_in({cval1, row["condition_value_2"] or 0} - {0}, start, end, full_state)

    if ctype in (200031, 200032):
        from .team_building import counts_in
        start = _parse_mdb_datetime(row["start_date"])
        end = _parse_mdb_datetime(row["end_date"])
        return counts_in(full_state, "runs" if ctype == 200031 else "scouts", start, end)

    if ctype == 200035:
        from .team_building import KEY as TB_KEY
        return len((full_state.get(TB_KEY) or {}).get("groups") or [])

    if ctype == 700001:
        from .training_challenge import STATE_KEY as EXAMS_KEY
        exams = (full_state.get(EXAMS_KEY) or {}).get(str(cval1), {}).get("exams", {})
        return sum(e.get("result_type", 1) >= (row["condition_value_2"] or 2) for e in exams.values())

    if ctype == _COND_FAN_NUM:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        if cval1:
            return sum(c.get("fan") or 0 for c in charas if c.get("chara_id") == cval1)
        return sum(c.get("fan") or 0 for c in charas)

    if ctype == _COND_SINGLE_MODE_RACE_WIN:
        genuine = _genuine_roster(viewer_id, full_state)
        if cval1:
            return sum(c.get("wins") or 0 for c in genuine if c.get("card_id") == cval1)
        return sum(c.get("wins") or 0 for c in genuine)

    if ctype == _COND_COMPLETE_CAREER:
        if cval1 <= 1:
            return len(_genuine_roster(viewer_id, full_state))
        return sum(1 for c in _genuine_roster(viewer_id, full_state) if (c.get("rank") or 0) >= cval1)

    if ctype == _COND_ADD_TRAINEES:
        return len(_genuine_roster(viewer_id, full_state))

    if ctype == _COND_G1_WINS:
        return sum(trained_chara.g1_win_count(c) for c in _genuine_roster(viewer_id, full_state))

    if ctype == _COND_CAREER_WIN_THRESHOLD:
        return sum(1 for c in _genuine_roster(viewer_id, full_state) if (c.get("wins") or 0) >= cval1)

    if ctype == _COND_CAREER_FAN_THRESHOLD:
        threshold = row["condition_value_2"] or 0
        roster = _genuine_roster(viewer_id, full_state)
        if cval1:
            ids = {r["id"] for r in master_data.query(
                "SELECT id FROM card_data WHERE chara_id=?", (cval1,))}
            roster = [c for c in roster if c.get("card_id") in ids]
        return sum(1 for c in roster if (c.get("fans") or 0) >= threshold)

    if ctype == _COND_UNLOCK_STAR_COUNT:
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return sum(1 for c in cards if (c.get("talent_level") or 1) >= cval1)

    if ctype == _COND_VIEW_CHARA_STORY:
        st = stories._story_state(full_state)
        row2 = master_data.query_one(
            "SELECT id FROM chara_story_data WHERE story_id=?", (cval1,))
        if not row2:
            return 0
        return 1 if row2["id"] in (st.get("chara_cleared") or []) else 0

    if ctype == _COND_VIEW_MAIN_STORY:
        st = stories._story_state(full_state)
        return 1 if cval1 in (st.get("main_cleared") or []) else 0

    if ctype == _COND_LOGIN_DAYS_TOTAL:
        return login_bonus.total_login_days(full_state)

    if ctype == _COND_LOGIN_TODAY:
        return 1 if login_bonus.logged_in_today(full_state) else 0

    if ctype == _COND_SKILL_COUNT_THRESHOLD:
        threshold = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(len(c.get("skill_array") or []) >= threshold for c in genuine) else 0

    if ctype == _COND_SPECIFIC_SKILL_ACQUIRED:
        target_skill = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(
            any((s.get("skill_id") == target_skill) for s in (c.get("skill_array") or []))
            for c in genuine
        ) else 0

    if ctype == _COND_EPITHET_AND_CAREER_COMPLETE:
        from . import user_profile
        honors = user_profile._honor_state(full_state, viewer_id).get("honors") or {}
        has_epithet = str(cval1) in honors
        return 1 if has_epithet and _genuine_roster(viewer_id, full_state) else 0

    if ctype == _COND_G1_TROPHY_COUNT:
        genuine = _genuine_roster(viewer_id, full_state)
        return max((trained_chara.g1_win_count(c) for c in genuine), default=0)

    if ctype == _COND_RAISE_SKILL_HINT:
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return 1 if any(
            (s.get("level") or 0) > 0
            for c in cards for s in (c.get("skill_data_array") or [])
        ) else 0

    if ctype == _COND_VIEW_DISTINCT_STORIES:
        st = stories._story_state(full_state)
        return (len(st.get("chara_cleared") or [])
                + len(st.get("main_cleared") or [])
                + len(st.get("story_event_cleared") or []))

    if ctype == _COND_BOND_LEVEL_THRESHOLD_COUNT:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        return sum(1 for c in charas
                  if _love_rank_level(c.get("love_point") or 0) >= cval1)

    flags = full_state.get(ACHIEVEMENT_FLAG_KEY) or []
    if ctype == _COND_CHANGE_TITLE:
        return 1 if FLAG_TITLE_CHANGED in flags else 0
    if ctype in (_COND_CHANGE_HOME_COMPANIONS, _COND_CHANGE_PROFILE_CHARA):
        return 1 if FLAG_HOME_COMPANIONS_CHANGED in flags else 0
    if ctype == _COND_LINK_APP_DATA:
        return 1 if FLAG_APP_DATA_LINKED in flags else 0
    if ctype == _COND_EDIT_TRAINER_CARD:
        return 1 if FLAG_TRAINER_CARD_EDITED in flags else 0
    if ctype == _COND_JUKEBOX_REQUEST:
        return 1 if FLAG_JUKEBOX_REQUESTED in flags else 0
    if ctype == _COND_PRACTICE_RACE_PARTICIPATE:
        return 1 if FLAG_PRACTICE_RACE_RUN in flags else 0
    if ctype == _COND_JOIN_CIRCLE:
        if FLAG_CIRCLE_JOINED in flags:
            return 1
        from .. import social
        try:
            if social.circle_of(viewer_id) is not None:
                mark_achieved(full_state, FLAG_CIRCLE_JOINED)
                return 1
        except Exception:
            log.exception("missions: circle lookup failed for viewer %s", viewer_id)
        return 0
    if ctype == _COND_CIRCLE_CHECK:
        return 1 if FLAG_CIRCLE_CHECKED in flags else 0
    if ctype == _COND_CIRCLE_SHARE_PARTNER:
        return 1 if FLAG_CIRCLE_PARTNER_SHARED in flags else 0

    if ctype in (_COND_FOLLOW_COUNT, _COND_FOLLOWER_COUNT):
        from .. import social
        try:
            return (social.follow_num(viewer_id) if ctype == _COND_FOLLOW_COUNT
                    else social.follower_num(viewer_id))
        except Exception:
            log.exception("missions: social count failed for viewer %s", viewer_id)
            return 0

    if ctype == _COND_WIN_UNITY_CUP:
        return sum(1 for c in _genuine_roster(viewer_id, full_state)
                   if ((c.get("_scenario_facts") or {}).get("unity_cup_won")))

    if ctype == _COND_CLAW_MACHINE_PLAY_COUNT:
        from . import single_mode_team
        lifetime = full_state.get(single_mode_team.CRANE_LIFETIME_KEY) or {}
        return lifetime.get("play_count") or 0

    if ctype == _COND_CLAW_MACHINE_PLUSHIE_COUNT:
        from . import single_mode_team
        lifetime = full_state.get(single_mode_team.CRANE_LIFETIME_KEY) or {}
        return lifetime.get("plushie_count") or 0

    if ctype == _COND_CLAW_MACHINE_PLUSHIE_SESSION:
        genuine = _genuine_roster(viewer_id, full_state)
        return sum(1 for c in genuine if (c.get("_crane_session_plushies") or 0) >= cval1)

    if ctype == _COND_CONCERT_SUCCESS_COUNT:
        genuine = _genuine_roster(viewer_id, full_state)
        all_lives = [l for c in genuine for l in (c.get("grand_live_lives_done") or [])]
        if cval1 == 1:
            return len(all_lives)
        if cval1 == 2 and row["condition_value_2"] == 1:
            return sum(1 for l in all_lives if l.get("result_state") == 2)
        return 0

    if ctype == _COND_CONCERT_ALL_GREAT:
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            lives = c.get("grand_live_lives_done") or []
            if lives and all(l.get("result_state") == 2 for l in lives):
                return 1
        return 0

    if ctype == _COND_SPECIFIC_SONG_OBTAINED:
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(cval1 in (c.get("grand_live_songs") or []) for c in genuine) else 0

    if ctype == _COND_SONG_COUNT_THRESHOLD:
        genuine = _genuine_roster(viewer_id, full_state)
        return 1 if any(len(c.get("grand_live_songs") or []) >= cval1 for c in genuine) else 0

    if ctype == _COND_SONGS_BEFORE_FIRST_CONCERT:
        threshold = row["condition_value_2"] or 0
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            lives = c.get("grand_live_lives_done") or []
            if lives and (lives[0].get("songs_owned") or 0) >= threshold:
                return 1
        return 0

    if ctype == _COND_WATCH_SPECIFIC_SONG:
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            for l in (c.get("grand_live_lives_done") or []):
                if cval1 in (l.get("song_ids") or ()):
                    return 1
        return 0

    if ctype == _COND_WATCH_UNIQUE_CONCERTS:
        genuine = _genuine_roster(viewer_id, full_state)
        seen = set()
        for c in genuine:
            for l in (c.get("grand_live_lives_done") or []):
                seen.update(l.get("song_ids") or ())
        return len(seen)

    if ctype == _COND_CLASSIC_TRIPLE_CROWN:
        if cval1 != 1:
            return 0
        targets = [_race_ids_by_name(n) for n in
                  ("Satsuki Sho", "Tokyo Yushun (Japanese Derby)", "Kikuka Sho")]
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            won = _won_race_ids(c)
            if all(won & t for t in targets):
                return 1
        return 0

    if ctype == _COND_BIG_EIGHT:
        targets = [_race_ids_by_name(n) for n in (
            "Oka Sho", "Satsuki Sho", "Tenno Sho (Spring)", "Japanese Oaks",
            "Tokyo Yushun (Japanese Derby)", "Kikuka Sho", "Tenno Sho (Autumn)",
            "Arima Kinen")]
        genuine = _genuine_roster(viewer_id, full_state)
        best = 0
        for c in genuine:
            won = _won_race_ids(c)
            best = max(best, sum(1 for t in targets if won & t))
        return best

    if ctype == _COND_URA_FINALE_WIN:
        finale_ids = _finale_program_ids()
        genuine = _genuine_roster(viewer_id, full_state)
        for c in genuine:
            if finale_ids & set(c.get("_won_program_ids") or ()):
                return 1
        return 0

    if ctype == _COND_ALL_TROPHIES_FOR_CHARA:
        target = _trophy_instance_ids()
        if not target:
            return 0
        genuine = _genuine_roster(viewer_id, full_state)
        won_instances = set()
        for c in genuine:
            if _chara_id_for_card(c.get("card_id")) != cval1:
                continue
            for pid in c.get("_won_program_ids") or ():
                info = _program_race_row(pid)
                if info and info["grade"] in (100, 200, 300):
                    won_instances.add(info["inst"])
        return 1 if target <= won_instances else 0

    if ctype == _COND_SUPPORT_CARD_KIND_NUM:
        return len(full_state.get(collection.SUPPORT_CARD_KEY) or [])

    if ctype == _COND_SUPPORT_CARD_LIMIT_BREAK:
        cards = full_state.get(collection.SUPPORT_CARD_KEY) or []
        return sum(c.get("limit_break_count") or 0 for c in cards)

    if ctype in (_COND_UNLOCK_STAR, _COND_POTENTIAL_LEVEL):
        cards = full_state.get(collection.CARD_LIST_KEY) or []
        return sum(max(0, (c.get("talent_level") or 1) - 1) for c in cards)

    if ctype == _COND_TEAM_RANK:
        from . import team_stadium
        rank_row = master_data.query_one(
            "SELECT team_min_value FROM team_stadium_rank WHERE id=?", (cval1,))
        if not rank_row:
            return 0
        ts_state = full_state.get(team_stadium.TEAM_STADIUM_STATE_KEY) or {}
        best_point = ts_state.get("best_point") or 0
        return 1 if best_point >= rank_row["team_min_value"] else 0

    if ctype == _COND_BOND_LEVEL_CHARA:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        chara = next((c for c in charas if c.get("chara_id") == cval1), None)
        return _love_rank_level(chara.get("love_point") or 0) if chara else 0

    if ctype == _COND_BOND_LEVEL_ANY:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        return sum(_love_rank_level(c.get("love_point") or 0) for c in charas)

    if ctype == _COND_FAN_THRESHOLD_COUNT:
        charas = full_state.get(collection.CHARA_LIST_KEY) or []
        threshold = cval1 or 1
        return sum(1 for c in charas if (c.get("fan") or 0) >= threshold)

    if ctype == _COND_DAILY_RACE_ENTRIES:
        from . import daily_races
        return (full_state.get(daily_races.STATE_KEY) or {}).get("entries_total") or 0

    hook = CONDITION_HOOKS.get(ctype)
    if hook is not None:
        return hook(viewer_id, full_state, row)
    return 0


def _mission_entry(viewer_id, full_state: dict, row, claimed: set, daily_state: dict) -> dict:
    mid = row["id"]
    is_daily = row["mission_type"] in _DAILY_MISSION_TYPES
    if mid in ((daily_state.get("claimed") or []) if is_daily else claimed):
        return {"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                "exec_count": row["condition_num"],
                "mission_type": row["mission_type"], "event_id": row["event_id"]}
    count = _count_for(viewer_id, full_state, row, daily_state)
    status = _STATUS_CLEAR if count >= row["condition_num"] else _STATUS_NOT_CLEAR
    return {"mission_id": mid, "mission_status": status, "exec_count": count,
            "mission_type": row["mission_type"], "event_id": row["event_id"]}


def build_mission_list(viewer_id, full_state: dict) -> list:
    now_ts = patch._servertime()
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    rows = master_data.query("SELECT * FROM mission_data")
    return [_mission_entry(viewer_id, full_state, row, claimed, daily_state)
            for row in rows if _is_active(row, now_ts)]


_CAREER_MISSION_TYPE = 4
_CAREER_MISSION_CONDITIONS = (100003, 100025)
_EVERGREEN_END_YEAR = 2049


def _is_campaign_row(row) -> bool:
    end = (row["end_date"] or "").strip()
    return bool(row["date_check_flg"]) and end[:4].isdigit() \
        and int(end[:4]) < _EVERGREEN_END_YEAR


def career_mission_list(viewer_id, full_state: dict) -> list:
    now_ts = patch._servertime()
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    rows = master_data.query(
        "SELECT * FROM mission_data WHERE mission_type=? AND condition_type IN (%s)"
        % ",".join("?" * len(_CAREER_MISSION_CONDITIONS)),
        (_CAREER_MISSION_TYPE,) + _CAREER_MISSION_CONDITIONS)
    return [_mission_entry(viewer_id, full_state, row, claimed, daily_state)
            for row in rows if _is_campaign_row(row) and _is_active(row, now_ts)]


EVENT_MISSION_TYPES = {
    "legend_mission_num": (5, 8),
    "training_challenge_mission_num": (9, 12),
    "challenge_match_mission_num": (10, 11),
    "team_building_mission_num": (14, 15),
    "collect_raid_mission_flag": (16, 17),
}
_CHAMPIONS_MISSION_TYPE = 7


def badge_counts(viewer_id, full_state: dict) -> dict:
    clear = [m for m in build_mission_list(viewer_id, full_state) if m["mission_status"] == _STATUS_CLEAR]
    event_types = {t for ts in EVENT_MISSION_TYPES.values() for t in ts} | {_CHAMPIONS_MISSION_TYPE}
    out = {"mission_num": sum(1 for m in clear if m["mission_type"] not in event_types)}
    for key, types in EVENT_MISSION_TYPES.items():
        n = sum(1 for m in clear if m["mission_type"] in types)
        out[key] = min(n, 1) if key.endswith("_flag") else n
    out["champions_mission_array"] = [
        {"mission_id": m["mission_id"], "exec_count": m["exec_count"], "mission_status": m["mission_status"]}
        for m in clear if m["mission_type"] == _CHAMPIONS_MISSION_TYPE]
    return out


@registry.endpoint("mission/index")
def handle_mission_index(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    full_state = state_store.get_state(viewer_id) or {}
    mission_list = build_mission_list(viewer_id, full_state)
    state_store.save_state(viewer_id, full_state)
    return _ok({"mission_list": mission_list})


def claim_missions(viewer_id, full_state: dict, ids, summary: dict) -> list | None:
    claimed = _mission_claimed_set(full_state)
    daily_state = _daily_mission_state(full_state)
    updated = []
    for mid in ids:
        row = master_data.query_one("SELECT * FROM mission_data WHERE id=?", (mid,))
        if row is None:
            return None
        is_daily = row["mission_type"] in _DAILY_MISSION_TYPES
        already = (mid in (daily_state.get("claimed") or [])) if is_daily else (mid in claimed)
        if already:
            updated.append({"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                            "exec_count": row["condition_num"],
                            "condition_type": row["condition_type"]})
            continue
        if _count_for(viewer_id, full_state, row, daily_state) < row["condition_num"]:
            return None
        _grant_reward(full_state, row["item_category"], row["item_id"],
                      row["item_num"], summary, viewer_id)
        if is_daily:
            daily_state.setdefault("claimed", []).append(mid)
        else:
            claimed.add(mid)
        updated.append({"mission_id": mid, "mission_status": _STATUS_GOT_REWARD,
                        "exec_count": row["condition_num"],
                        "condition_type": row["condition_type"]})

    _save_claimed(full_state, claimed)
    return updated


@registry.endpoint("mission/receive")
def handle_mission_receive(payload: dict) -> dict:
    viewer_id = payload["viewer_id"]
    ids = payload.get("mission_id_array")
    if not isinstance(ids, list) or not ids:
        return _refuse()

    full_state = state_store.get_state(viewer_id) or {}
    summary = shop._empty_summary()
    updated = claim_missions(viewer_id, full_state, ids, summary)
    if updated is None:
        return _refuse()
    state_store.save_state(viewer_id, full_state)
    return _ok({"reward_summary_info": summary, "updated_mission_array": updated})
