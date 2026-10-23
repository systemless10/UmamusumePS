
from __future__ import annotations

import json
import sqlite3
import time

from app import accounts
from app import paid_features as PF
from app import region
from app import state as state_store

from .. import history
from . import op
from .catalog import rank_label
from app import default_viewer
from .common import OpError, card_name, load, region_name, save


def _q(sql, params=()):
    return history._state_db().execute(sql, params).fetchall()


def _per_key(key: str, expr: str) -> dict:
    return {r[0]: r[1] for r in _q(f"SELECT viewer_id, {expr} FROM viewer_state_kv WHERE key=?", (key,))}


def _account_rows() -> dict:
    path = region.data_dir() / "accounts.sqlite3"
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT viewer_id, created_at, last_seen, transition_password IS NOT NULL,"
                            " pending_deletion_at, udid FROM accounts").fetchall()
    finally:
        conn.close()
    return {r[0]: {"created_at": r[1], "last_seen": r[2], "has_password": bool(r[3]),
                   "pending_deletion_at": r[4], "udid": r[5]} for r in rows}


@op("users.list")
def users_list(viewer, args):
    present = [r[0] for r in _q("SELECT viewer_id FROM viewer_state_present")]
    sizes: dict = {}
    for vid, n, total in _q("SELECT viewer_id, COUNT(*), SUM(length(value)) FROM viewer_state_kv GROUP BY viewer_id"):
        sizes[vid] = (n, total)
    name = _per_key("load_index", "json_extract(value, '$.data.user_info.name')")
    social = _per_key("social_directory_card", "json_extract(value, '$.name')")
    leader = _per_key("load_index", "json_extract(value, '$.data.user_info.leader_chara_id')")
    level = _per_key("social_directory_card", "json_extract(value, '$.directory_level')")
    fcoin = _per_key("coin_info_state", "json_extract(value, '$.fcoin')")
    coin = _per_key("coin_info_state", "json_extract(value, '$.coin')")
    counts = {k: _per_key(k, "json_array_length(value)") for k in
              ("card_collection", "support_card_collection", "legacy_roster", "item_list_state")}
    career = {r[0]: r[1:] for r in _q(
        "SELECT viewer_id, json_extract(value, '$.data.chara_info.card_id'),"
        " json_extract(value, '$.data.chara_info.turn'), json_extract(value, '$.data.chara_info.scenario_id')"
        " FROM viewer_state_kv WHERE key='single_mode_team'")}
    acct = _account_rows()
    default = default_viewer.get()
    out = []
    for vid in present:
        a = acct.get(vid, {})
        c = career.get(vid)
        out.append({
            "id": vid, "name": name.get(vid) or social.get(vid) or "",
            "leader": leader.get(vid), "level": level.get(vid),
            "fcoin": fcoin.get(vid), "coin": coin.get(vid),
            "umas": counts["card_collection"].get(vid), "supports": counts["support_card_collection"].get(vid),
            "veterans": counts["legacy_roster"].get(vid), "items": counts["item_list_state"].get(vid),
            "career": {"card_id": c[0], "turn": c[1], "scenario": c[2]} if c and c[0] else None,
            "keys": sizes.get(vid, (0, 0))[0], "bytes": sizes.get(vid, (0, 0))[1] or 0,
            "created_at": a.get("created_at"), "last_seen": a.get("last_seen"),
            "has_password": a.get("has_password", False), "pending_deletion_at": a.get("pending_deletion_at"),
            "account": bool(a), "default": vid == default,
        })
    out.sort(key=lambda u: -(u["last_seen"] or 0))
    return {"region": region_name(), "default": default, "chosen": bool(default_viewer.chosen()), "users": out}


def _user_info(full_state) -> dict:
    blob = full_state.get("load_index")
    data = (blob or {}).get("data") if isinstance(blob, dict) else None
    return (data or {}).get("user_info") or {}


@op("users.detail")
def users_detail(viewer, args):
    full_state = load(viewer)
    info = _user_info(full_state)
    acct = accounts.get_account(viewer) or {}
    keys = [{"key": k, "size": n} for k, n in
            _q("SELECT key, length(value) FROM viewer_state_kv WHERE viewer_id=? ORDER BY key", (viewer,))]
    career = full_state.get("single_mode_team")
    ci = (career or {}).get("data", {}).get("chara_info") if isinstance(career, dict) else None
    box = (full_state.get("present_box_state") or {}).get("presents") or []
    social = full_state.get("social_directory_card") or {}
    roster = full_state.get("legacy_roster") or []
    best = max(roster, key=lambda r: r.get("rank_score") or 0, default=None)
    return {
        "id": viewer, "region": region_name(),
        "profile": {k: info.get(k) for k in ("name", "comment", "sex", "honor_id", "leader_chara_id",
                                             "leader_chara_dress_id", "support_card_id", "fan", "register_time",
                                             "create_time", "birth_day", "tutorial_step")},
        "social": {k: social.get(k) for k in ("directory_level", "team_evaluation_point", "single_mode_play_count",
                                              "last_login_time", "comment")},
        "account": {
            "exists": bool(acct), "created_at": acct.get("created_at"), "last_seen": acct.get("last_seen"),
            "has_password": bool(acct.get("transition_password")),
            "password_reset": acct.get("transition_password") == accounts._RESET_SENTINEL,
            "pending_deletion_at": acct.get("pending_deletion_at"), "udid": acct.get("udid"),
            "has_auth_key": bool(acct.get("auth_key")),
        },
        "wallet": full_state.get("coin_info_state") or {},
        "tp": full_state.get("tp_info_state") or {}, "rp": full_state.get("rp_info_state") or {},
        "money": next((i.get("number") for i in full_state.get("item_list_state") or [] if i.get("item_id") == 59), 0),
        "counts": {
            "umas": len(full_state.get("card_collection") or []),
            "supports": len(full_state.get("support_card_collection") or []),
            "characters": len(full_state.get("chara_collection") or []),
            "veterans": len(roster), "items": len(full_state.get("item_list_state") or []),
            "pieces": len(full_state.get("piece_list_state") or []),
            "music": len(full_state.get("music_list_state") or []),
            "mail_unclaimed": sum(1 for p in box if not p.get("state")), "mail_total": len(box),
        },
        "best_veteran": {"card": card_name(best.get("card_id")), "card_id": best.get("card_id"),
                         "score": best.get("rank_score"), "rank": rank_label(best.get("rank") or 1)} if best else None,
        "career": {"card_id": ci.get("card_id"), "card": card_name(ci.get("card_id")), "turn": ci.get("turn"),
                   "scenario": ci.get("scenario_id"), "state": ci.get("state")} if ci else None,
        "parked": sorted((full_state.get("career_save_slots") or {}).keys()),
        "paid": {name: PF.end_time(full_state, name) for name in PF.FEATURES},
        "keys": keys,
    }


@op("users.profile", label="Edit profile")
def users_profile(viewer, args):
    full_state = load(viewer)
    from app.handlers import load as L
    info = L.get_or_seed_data(full_state, viewer).setdefault("user_info", {})
    social = full_state.get("social_directory_card")
    changed = []
    for field, cast in (("name", str), ("comment", str), ("sex", int), ("honor_id", int),
                        ("leader_chara_id", int), ("leader_chara_dress_id", int), ("birth_day", str)):
        if field not in args or args[field] is None:
            continue
        value = cast(args[field])
        if field == "name" and not value.strip():
            raise OpError("The name can't be blank.")
        info[field] = value
        if field == "honor_id":
            info["honor_data"] = {"honor_id": value}
        if isinstance(social, dict) and field in social:
            social[field] = value
            if field == "honor_id":
                social["honor_data"] = {"honor_id": value}
        changed.append(field)
    save(viewer, full_state)
    return {"changed": changed}


@op("users.set_password")
def users_set_password(viewer, args):
    pw = str(args.get("password") or "")
    if not pw:
        raise OpError("Give a password.")
    accounts.set_transition_password(viewer, pw)
    return {"ok": True}


@op("users.reset_password")
def users_reset_password(viewer, args):
    accounts.reset_transition_password(viewer)
    return {"ok": True}


@op("users.cancel_deletion")
def users_cancel_deletion(viewer, args):
    accounts.cancel_deletion(viewer)
    return {"ok": True}


@op("users.set_default")
def users_set_default(viewer, args):
    default_viewer.set(viewer if args.get("on", True) else None)
    return {"default": default_viewer.get(), "chosen": bool(default_viewer.chosen())}


@op("users.delete", label="Delete account")
def users_delete(viewer, args):
    if str(args.get("confirm")) != viewer:
        raise OpError("Type the viewer id to confirm.")
    state_store.delete_state(viewer)
    if args.get("account_row", True):
        accounts.delete_account(viewer)
    return {"deleted": viewer}


@op("users.copy", label="Copy save from {source}")
def users_copy(viewer, args):
    source = "".join(str(args.get("source") or "").split())
    if not source or source == viewer:
        raise OpError("Pick a different source viewer.")
    src = state_store.get_state(source)
    if src is None:
        raise OpError(f"No save for viewer {source}.")
    data = json.loads(json.dumps(src.copy()))
    target = state_store.get_state(viewer)
    if target is not None and not args.get("overwrite"):
        raise OpError("This viewer already has a save; tick overwrite to replace it.")
    _retarget(data, viewer)
    fresh = target if target is not None else {}
    for k in list(fresh.keys()):
        del fresh[k]
    fresh.update(data)
    save(viewer, fresh)
    accounts.touch(viewer)
    return {"keys": len(data)}


def _retarget(data: dict, viewer: str) -> None:
    try:
        vid_int = int(viewer)
    except ValueError:
        return
    card = data.get("social_directory_card")
    if isinstance(card, dict):
        card["viewer_id"] = vid_int
        usc = card.get("user_support_card")
        if isinstance(usc, dict):
            usc["viewer_id"] = vid_int
    for rec in data.get("legacy_roster") or []:
        if isinstance(rec, dict) and "viewer_id" in rec:
            rec["viewer_id"] = vid_int


@op("users.new", label="Create save")
def users_new(viewer, args):
    if state_store.get_state(viewer) is not None:
        raise OpError("That viewer already has a save.")
    save(viewer, {})
    accounts.touch(viewer)
    return {"id": viewer, "created": time.time()}
