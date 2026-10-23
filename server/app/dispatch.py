
from __future__ import annotations

import copy
import json
import logging
import os
import time

from . import accounts
from . import concerts
from . import config
from . import event_gallery
from . import master_data
from . import tuning
from . import scenarios
from . import state as state_store
from .fixtures import store as fixtures
from .handlers import conditions
from .handlers import (collection, gacha, jukebox, live_theater, load, practice_race,
                       registry, shop, single_mode_team, tool, trained_chara)
from .patch import make_sid, patch_response
from .result_codes import ResultCode, describe, refuse
from . import session as session_store

log = logging.getLogger("uma-server")

registry.autoload()

import itertools as _itertools
_seq_counter = _itertools.count(1)
def _request_seq() -> str:
    return f"{next(_seq_counter):04d}"

_SESSION_ERROR = ResultCode.SESSION_ERROR

HANDLERS = {
    "single_mode/start": single_mode_team.handle_start,
    "single_mode/load": single_mode_team.handle_load,
    "single_mode/exec_command": single_mode_team.handle_exec_command,
    "single_mode/check_event": single_mode_team.handle_ura_check_event,
    "single_mode/get_choice_reward": single_mode_team.handle_get_choice_reward,
    "single_mode/race_entry": single_mode_team.handle_ura_race_entry,
    "single_mode/change_running_style": single_mode_team.handle_change_running_style,
    "single_mode/race_start": single_mode_team.handle_ura_race_start,
    "single_mode/race_end": single_mode_team.handle_ura_race_end,
    "single_mode/race_out": single_mode_team.handle_ura_race_out,
    "single_mode/race_analyze": single_mode_team.handle_race_analyze,
    "single_mode/finish": single_mode_team.handle_finish,
    "single_mode/multi_race_reserve": single_mode_team.handle_multi_race_reserve,
    "single_mode/gain_skills": single_mode_team.handle_gain_skills,
    "single_mode/minigame_end": single_mode_team.handle_minigame_end,
    "single_mode/continue": single_mode_team.handle_continue,
    "single_mode/factor_select": single_mode_team.handle_factor_select,
    "single_mode/select_succession": single_mode_team.handle_select_succession,
    "single_mode/factor_order_load": single_mode_team.handle_factor_order_load,
    "single_mode/factor_relottery_end": single_mode_team.handle_factor_relottery_end,
    "single_mode/factor_lottery": single_mode_team.handle_factor_lottery,
    "single_mode/change_short_cut": single_mode_team.handle_change_short_cut,
    "pre_single_mode/index": single_mode_team.handle_pre_single_mode_index,
    "pre_single_mode/friend_support_card_reload":
        single_mode_team.handle_friend_support_card_reload,
    "race/analyze": single_mode_team.handle_race_analyze,
    "live_theater/index": live_theater.handle_index,
    "live_theater/live_start": live_theater.handle_live_start,
    "gacha/index": gacha.handle_index,
    "gacha/exec": gacha.handle_exec,
    "gacha/limit_exchange": gacha.handle_limit_exchange,
    "item/show_exchange": shop.handle_show_exchange,
    "item/exchange": shop.handle_exchange,
    "item/exchange_multi": shop.handle_exchange_multi,
    "item/exchangeAddFrame": shop.handle_exchange_add_frame,
    "item/check_aniv_shop": shop.handle_check_aniv_shop,
    "jukebox/index": jukebox.handle_index,
    "single_mode_team/start": single_mode_team.handle_start,
    "single_mode_team/load": single_mode_team.handle_load,
    "single_mode_team/exec_command": single_mode_team.handle_exec_command,
    "single_mode_team/race_entry": single_mode_team.handle_ura_race_entry,
    "single_mode_team/race_start": single_mode_team.handle_ura_race_start,
    "single_mode_team/race_end": single_mode_team.handle_ura_race_end,
    "single_mode_team/race_out": single_mode_team.handle_ura_race_out,
    "single_mode_team/finish": single_mode_team.handle_finish,
    "support_card_deck/change_party": collection.handle_change_party,
    "tool/signup": tool.handle_signup,
    "load/index": load.handle_load_index,
    "practice_race/race_start": practice_race.handle_race_start,
    "practice_race/get_follow_user_data": practice_race.handle_get_follow_user_data,
    "trained_chara/load": trained_chara.handle_trained_chara_load,
    "trained_chara/change_nickname": trained_chara.handle_change_nickname,
    "trained_chara/change_lock_multi": trained_chara.handle_change_lock_multi,
    "trained_chara/change_memo": trained_chara.handle_change_memo,
    "trained_chara/remove": trained_chara.handle_remove,
    "trained_chara/get_succession_history_array":
        trained_chara.handle_get_succession_history_array,
}

from .handlers import factor_research as _factor_research
HANDLERS.update(_factor_research.COMMON_HANDLERS)


NOOP_SUCCESS = {
    "response_code": 1,
    "data_headers": {"result_code": 1, "notifications": {}},
    "data": {},
}


_UNSUPPORTED_STARTS = ("single_mode_team/start_team",
                       "single_mode_venus/start",
                       "single_mode_pioneer/start", "single_mode_arc/start",
                       "single_mode_grand_masters/start",
                       "single_mode_project_l/start")
_REFUSE = refuse()


_FEATURE_MAINTENANCE = {
    "gacha": (ResultCode.MAINTENANCE_TASK_GACHA, ("gacha/",)),
    "present": (ResultCode.MAINTENANCE_TASK_PRESENT, ("present/",)),
    "payment": (ResultCode.MAINTENANCE_TASK_PAYMENT_ALL, ("payment/",)),
    "shop": (ResultCode.MAINTENANCE_TASK_SHOP_ITEM_EXCHANGE, ("item/",)),
    "single_mode": (ResultCode.MAINTENANCE_TASK_SINGLE_MODE,
                    ("single_mode", "pre_single_mode/")),
    "circle": (ResultCode.MAINTENANCE_TASK_CIRCLE,
               ("circle/", "circle_user/", "circle_chat/", "circle_item_request/")),
    "team_stadium": (ResultCode.MAINTENANCE_TASK_TEAM_STADIUM, ("team_stadium/",)),
    "daily_race": (ResultCode.MAINTENANCE_TASK_DAILY_RACE, ("daily_race/", "daily_race_skip/")),
    "legend_race": (ResultCode.MAINTENANCE_TASK_LEGEND_RACE, ("legend_race/",)),
    "daily_legend_race": (ResultCode.MAINTENANCE_TASK_DAILY_LEGEND_RACE,
                          ("daily_legend_race/",)),
    "main_story": (ResultCode.MAINTENANCE_TASK_MAIN_STORY, ("main_story/", "main_story_race/")),
    "character_story": (ResultCode.MAINTENANCE_TASK_CHARACTER_STORY, ("character_story/",)),
    "extra_story": (ResultCode.MAINTENANCE_TASK_STORY_EXTRA, ("extra_story/",)),
    "story_event": (ResultCode.MAINTENANCE_TASK_STORY_EVENT, ("story_event/",)),
    "mission": (ResultCode.MAINTENANCE_TASK_MISSION, ("mission/",)),
    "practice_race": (ResultCode.MAINTENANCE_TASK_PRACTICE_RACE, ("practice_race/",)),
    "room_match": (ResultCode.MAINTENANCE_TASK_ROOM_MATCH, ("room_match/",)),
    "jukebox": (ResultCode.MAINTENANCE_TASK_JUKEBOX, ("jukebox/",)),
    "campaign_walking": (ResultCode.MAINTENANCE_TASK_CAMPAIGN_WALKING, ("campaign_walking/",)),
    "card_talent_strengthen": (ResultCode.MAINTENANCE_TASK_CARD_TALENT_STRENGTHEN,
                               ("card/talent_strengthen",)),
    "card_rarity_upgrade": (ResultCode.MAINTENANCE_TASK_CARD_RARITY_UPGRADE,
                            ("card/rarity_upgrade",)),
    "card_unlock": (ResultCode.MAINTENANCE_TASK_CARD_UNLOCK, ("card/unlock",)),
    "support_card_strengthen": (ResultCode.MAINTENANCE_TASK_SUPPORT_CARD_STRENGTHEN,
                                ("support_card/strengthen",)),
    "support_card_limit_break": (ResultCode.MAINTENANCE_TASK_SUPPORT_CARD_LIMIT_BREAK,
                                 ("support_card/limit_break",)),
    "support_card_sell": (ResultCode.MAINTENANCE_TASK_SUPPORT_CARD_SELL,
                          ("support_card/sell",)),
}


def _maintenance_code(endpoint: str) -> int:
    if config.get("maintenance", False):
        return ResultCode.MAINTENANCE
    for name in config.get("feature_maintenance", []) or []:
        code, prefixes = _FEATURE_MAINTENANCE.get(name, (0, ()))
        if code and endpoint.startswith(prefixes):
            return code
    return 0


_REPLAY_NEWEST = {e.strip() for e in os.environ.get("UMA_REPLAY_NEWEST", "").split(",") if e.strip()}

_REPLAY_FILE: dict[str, str] = {}
for _entry in os.environ.get("UMA_REPLAY_FILE", "").split(","):
    _entry = _entry.strip()
    if "=" in _entry:
        _ep, _path = _entry.split("=", 1)
        _REPLAY_FILE[_ep.strip()] = _path.strip()


def _file_capture(endpoint: str):
    path = _REPLAY_FILE.get(endpoint)
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("response")


def _newest_capture(endpoint: str):
    pairs = fixtures.all_for(endpoint)
    return max(pairs, key=lambda p: p.ts).response if pairs else None


_UNHANDLED_SEEN: set[str] = set()
_UNHANDLED_DUMPED: set[str] = set()


def _fallback_response(endpoint: str) -> dict:
    if endpoint in _REPLAY_FILE:
        resp = _file_capture(endpoint)
        if resp is not None:
            log.warning("UMA_REPLAY_FILE: serving literal capture file for %s", endpoint)
            return copy.deepcopy(resp)
    if endpoint in _REPLAY_NEWEST:
        resp = _newest_capture(endpoint)
        if resp is not None:
            log.warning("UMA_REPLAY_NEWEST: serving newest raw capture for %s", endpoint)
            return copy.deepcopy(resp)
    if any(endpoint.endswith(u) for u in _UNSUPPORTED_STARTS):
        log.warning("REFUSING unsupported scenario start %s (result_code 205)", endpoint)
        return _REFUSE
    pair = fixtures.first(endpoint)
    if pair is not None:
        log.info("fixture replay for %s (no handler / no exact match)", endpoint)
        return pair.response
    log.warning("no handler/fixture for %s -- returning no-op success", endpoint)
    _UNHANDLED_SEEN.add(endpoint)
    return NOOP_SUCCESS


GAME_RESOURCE_VERSION = "10008400"

def _resource_version() -> str:
    return str(config.get("resource_version", GAME_RESOURCE_VERSION))

_ADDED_RESPONSE_FIELDS = {
    "pre_single_mode/index": {"default_running_style_array": []},
    "load/index": {
        "idle_single_mode_load_info": None,
        "option_info_array": [{"type": 3, "value": 0}],
    },
}


def _install_scenario_routes() -> None:
    shared = {ep.split("/", 1)[1]: fn for ep, fn in HANDLERS.items()
              if ep.startswith("single_mode/")}
    for scen in scenarios.all_scenarios():
        prefix = scen.endpoint_prefix
        if prefix == "single_mode":
            continue
        routes = dict(shared)
        routes.update(scen.extra_endpoints())
        for suffix, fn in routes.items():
            HANDLERS.setdefault(f"{prefix}/{suffix}", fn)


_install_scenario_routes()


_scenario_ids_cache: dict = {}


def _scenario_ids() -> list:
    from . import custom_support
    hidden = custom_support.hidden_scenarios()
    override = config.get("scenario_ids")
    if isinstance(override, list) and override:
        try:
            ids = [int(x) for x in override if int(x) not in hidden]
            implemented = set(scenarios.implemented_ids())
            return ids + sorted(s for s in custom_support.pack_scenarios() if s not in ids and s in implemented)
        except (TypeError, ValueError):
            log.warning("client_config.json scenario_ids not a list of ints: %r", override)
    implemented = set(scenarios.implemented_ids())
    return [sid for sid in _available_scenario_ids() if sid in implemented and sid not in hidden]


def _available_scenario_ids() -> list:
    from . import custom_support
    pack = custom_support.CURRENT_PACK.get()
    if pack not in _scenario_ids_cache:
        now = int(time.time())
        try:
            rows = master_data.query(
                "SELECT id FROM single_mode_scenario "
                "WHERE start_date <= ? AND end_date > ? ORDER BY id", (now, now))
            _scenario_ids_cache[pack] = [r["id"] for r in rows]
        except Exception:
            log.exception("could not read scenario list from master.mdb")
            _scenario_ids_cache[pack] = []
    return _scenario_ids_cache[pack]


def _apply_version_compat(endpoint: str, response: dict) -> dict:
    data = (response or {}).get("data")
    if not isinstance(data, dict):
        return response
    if "resource_version" in data:
        data["resource_version"] = _resource_version()
    if "single_mode_scenario_id_array" in data:
        ids = _scenario_ids()
        if ids:
            data["single_mode_scenario_id_array"] = list(ids)
    for field, default in _ADDED_RESPONSE_FIELDS.get(endpoint, {}).items():
        data.setdefault(field, copy.deepcopy(default))
    return response


REQUEST_GUARDS: list = []


def handle_decoded(endpoint_path: str, payload: dict, presented_sid: bytes | None) -> dict:
    endpoint = endpoint_path.strip("/")
    if endpoint.startswith("umamusume/"):
        endpoint = endpoint[len("umamusume/") :]
    viewer_id = payload.get("viewer_id", "unknown")

    if log.isEnabledFor(logging.DEBUG):
        _boilerplate = {
            "device", "device_id", "device_name", "graphics_device_name",
            "ip_address", "platform_os_version", "carrier", "keychain",
            "locale", "button_info", "dmm_viewer_id", "dmm_onetime_token",
            "steam_id", "steam_session_ticket",
        }
        interesting = {k: v for k, v in payload.items() if k not in _boilerplate}
        log.debug("REQUEST %s payload=%s", endpoint, interesting)
    else:
        log.info("REQUEST %s", endpoint)

    handler = HANDLERS.get(endpoint) or registry.get(endpoint)
    if endpoint in _REPLAY_NEWEST or endpoint in _REPLAY_FILE:
        handler = None
    with accounts.viewer_lock(viewer_id):
        from . import custom_support
        custom_ok = custom_support.preflight(endpoint, payload)
        if endpoint == "tool/start_session" and accounts.deletion_days_remaining(viewer_id) == 0:
            accounts.delete_account(viewer_id)
            state_store.delete_state(viewer_id)
        sid = (session_store.start_session(viewer_id) if endpoint == "tool/start_session"
               else session_store.get_or_create_sid(viewer_id))
        session_ok = True
        maintenance_code = _maintenance_code(endpoint)
        if endpoint == "tool/start_session":
            accounts.touch(viewer_id, sid)
        else:
            session_ok = accounts.check_session(
                viewer_id, presented_sid, enforce=bool(config.get("auth_enforce", False)))
            if session_ok:
                accounts.touch(viewer_id, sid)
            else:
                log.warning("kicking stale session for viewer %s from %s "
                            "(logged in from another client)", viewer_id, endpoint)
                sid = make_sid()

        if not session_ok:
            response = refuse(_SESSION_ERROR)
        elif maintenance_code:
            response = refuse(maintenance_code)
        elif not custom_ok:
            response = refuse()
        elif (guarded := next((r for g in REQUEST_GUARDS
                               if (r := g(endpoint, payload)) is not None), None)) is not None:
            response = guarded
        else:
            single_mode_team.CURRENT_ENDPOINT.set(endpoint)
            conditions.begin_request()
            sp_before = (tuning.career_sp(viewer_id)
                         if endpoint.startswith("single_mode") and tuning.mult("skill_point_multiplier") != 1.0
                         else None)
            tuning.TRAINING_SP.set(0)
            response = handler(payload) if handler is not None else _fallback_response(endpoint)
            if sp_before is not None:
                try:
                    tuning.settle_skill_points(viewer_id, sp_before, response)
                except Exception:
                    log.exception("skill_point_multiplier settle failed; serving unscaled")
            custom_support.pin_career(endpoint, payload, response)
            guards = conditions.guard_report()
            data = response.get("data") if isinstance(response, dict) else None
            if guards and isinstance(data, dict):
                data["not_down_parameter_info"] = guards
            response = _apply_version_compat(endpoint, response)
            response = single_mode_team.sync_chara_info(viewer_id, endpoint, response)
            if endpoint.startswith("single_mode"):
                response = concerts.attach_story_songs(viewer_id, endpoint, response)
                event_gallery.record_served(viewer_id, response)
            if endpoint == "tool/start_session":
                notifs = (response.get("data_headers") or {}).get("notifications")
                if isinstance(notifs, dict) and notifs.get("unread_information_exists"):
                    full_state = state_store.get_state(viewer_id) or {}
                    if full_state.get("seen_unread_information_flag"):
                        notifs["unread_information_exists"] = 0
                    else:
                        full_state["seen_unread_information_flag"] = True
                        state_store.save_state(viewer_id, full_state)

    result_code = ((response or {}).get("data_headers") or {}).get("result_code")
    if result_code not in (None, ResultCode.RESULT_CODE_OK):
        log.info("RESULT %s -> %s %s", endpoint, result_code, describe(result_code))

    if endpoint.startswith("single_mode") and ("check_event" in endpoint or "exec_command" in endpoint or "get_choice_reward" in endpoint or "load" in endpoint):
        try:
            import json as _json, os as _os
            _dbg = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "debug_responses")
            _os.makedirs(_dbg, exist_ok=True)
            _d = (response or {}).get("data", {})
            _ci = _d.get("chara_info", {}) if isinstance(_d, dict) else {}
            _hi = _d.get("home_info", {}) if isinstance(_d, dict) else {}
            _smlc = _d.get("single_mode_load_common", {}) if isinstance(_d, dict) else {}
            _boiler = {"device", "device_id", "device_name", "graphics_device_name",
                       "ip_address", "platform_os_version", "carrier", "keychain", "locale",
                       "button_info", "dmm_viewer_id", "dmm_onetime_token", "steam_id",
                       "steam_session_ticket", "viewer_id"}
            _snap = {
                "endpoint": endpoint,
                "req_full": {k: v for k, v in payload.items() if k not in _boiler},
                "req": {k: payload.get(k) for k in ("current_turn", "command_id", "event_id", "choice_number")},
                "turn": _ci.get("turn"),
                "playing_state": _ci.get("playing_state"),
                "state": _ci.get("state"),
                "race_program_id": _ci.get("race_program_id"),
                "stats": {s: _ci.get(s) for s in ("speed", "stamina", "power", "guts", "wiz", "skill_point")} if _ci else None,
                "data_top_level_keys": sorted(_d.keys()) if isinstance(_d, dict) else None,
                "has_team_data_set": "team_data_set" in _d if isinstance(_d, dict) else None,
                "chara_info_full": _ci,
                "home_info_full": _hi,
                "load_common_present": bool(_smlc),
                "load_common_turn": (_smlc.get("chara_info") or {}).get("turn") if isinstance(_smlc, dict) else None,
                "load_common_has_team_data_set": "team_data_set" in _smlc if isinstance(_smlc, dict) else None,
                "resp_choice_reward_array": _d.get("choice_reward_array") if isinstance(_d, dict) else None,
                "unchecked_event_array": _d.get("unchecked_event_array"),
            }
            _fn = _os.path.join(_dbg, f"{_request_seq()}_{endpoint.replace('/', '_')}.json")
            with open(_fn, "w", encoding="utf-8") as _fh:
                _json.dump(_snap, _fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    if ("multi_race_reserve" in endpoint or "gain_skills" in endpoint
            or "minigame_end" in endpoint or "gacha/exec" in endpoint
            or "gacha/limit_exchange" in endpoint
            or "jukebox/" in endpoint
            or "item/exchange" in endpoint
            or "present/" in endpoint
            or "legend_race/" in endpoint
            or "training_challenge/" in endpoint
            or (endpoint in _UNHANDLED_SEEN and endpoint not in _UNHANDLED_DUMPED)):
        _UNHANDLED_DUMPED.add(endpoint)
        try:
            import json as _json, os as _os
            _dbg = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "debug_responses")
            _os.makedirs(_dbg, exist_ok=True)
            _boiler = {"device", "device_id", "device_name", "graphics_device_name",
                       "ip_address", "platform_os_version", "carrier", "keychain", "locale",
                       "button_info", "dmm_viewer_id", "dmm_onetime_token", "steam_id",
                       "steam_session_ticket", "viewer_id"}
            _snap = {
                "endpoint": endpoint,
                "request": {k: v for k, v in payload.items() if k not in _boiler},
                "response": response,
            }
            _fn = _os.path.join(_dbg, f"{_request_seq()}_RAW_{endpoint.replace('/', '_')}.json")
            with open(_fn, "w", encoding="utf-8") as _fh:
                _json.dump(_snap, _fh, ensure_ascii=False, indent=1)
        except Exception:
            pass

    patched = patch_response(response, payload, sid)
    if endpoint == "tool/signup":
        new_viewer_id = (patched.get("data") or {}).get("viewer_id")
        if new_viewer_id and isinstance(patched.get("data_headers"), dict):
            patched["data_headers"]["viewer_id"] = new_viewer_id
    return patched
