
import argparse
import os
import sys
from datetime import datetime

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now_str():
    return datetime.now().strftime(_TIME_FORMAT)

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(_HERE, "server"),
                   r"c:\Users\Trainer\Documents\private server\server"):
    if os.path.isdir(_candidate):
        sys.path.insert(0, _candidate)
        break

for _i, _arg in enumerate(sys.argv):
    if _arg == "--region" and _i + 1 < len(sys.argv):
        os.environ["UMA_REGION"] = sys.argv[_i + 1]
    elif _arg.startswith("--region="):
        os.environ["UMA_REGION"] = _arg.split("=", 1)[1]

from app import region
from app import state as state_store
from app.handlers import single_mode_team as T
from app.handlers import single_mode_events

from app import default_viewer as _default_viewer
DEFAULT_VIEWER = _default_viewer.get()
STATS = ("speed", "stamina", "power", "guts", "wiz")


def _load(viewer):
    full_state = state_store.get_state(viewer)
    if not full_state:
        sys.exit(f"no state for viewer {viewer}")
    return full_state


def _career(full_state):
    career = full_state.get(T.STATE_KEY)
    if not isinstance(career, dict):
        sys.exit("no active career -- start one in-game first")
    return career


def _refresh_career(viewer, full_state, career):
    ci = career["data"]["chara_info"]
    home = career["data"].get("home_info")
    if isinstance(home, dict):
        unlocked = [n[0] for n in full_state.get(single_mode_events.UNLOCKED_NPCS_KEY, [])]
        T._refresh_command_info(
            ci, home, turn=ci.get("turn", 1), unlocked_npcs=unlocked,
            facility_levels=T._facility_levels(career["data"]),
            race_history=career["data"].get("race_history", []),
            support_card_levels=T._support_card_levels(full_state))
    state_store.save_state(viewer, full_state)


def cmd_show(viewer, args):
    full_state = _load(viewer)
    career = full_state.get(T.STATE_KEY)
    if not isinstance(career, dict):
        print("no active career")
        return
    ci = career["data"]["chara_info"]
    print(f"card {ci.get('card_id')}  turn {ci.get('turn')}  class {ci.get('chara_grade')}  "
          f"playing_state {ci.get('playing_state')}")
    print("stats:", {s: ci.get(s) for s in STATS},
          " caps:", {s: ci.get('max_wiz' if s == 'wiz' else f'max_{s}') for s in STATS})
    print(f"SP {ci.get('skill_point')}  fans {ci.get('fans')}  vital {ci.get('vital')}/"
          f"{ci.get('max_vital')}  mood {ci.get('motivation')}")
    print(f"skills: {len(ci.get('skill_array') or [])}  hints: {len(ci.get('skill_tips_array') or [])}  "
          f"races run: {len(career['data'].get('race_history') or [])}")


def cmd_set_turn(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    ci["turn"] = int(args.value)
    ci["playing_state"] = 1
    full_state.pop(T.RACE_CTX_KEY, None)
    _refresh_career(viewer, full_state, career)
    print(f"turn -> {ci['turn']} (training screen recomputed)")
    print("   reload the career in the client (title -> Continue) before "
          "acting -- its cached turn is now stale and will be refused")


def cmd_set_stat(viewer, args):
    stat = args.name.lower()
    if stat not in STATS:
        sys.exit(f"stat must be one of {STATS}")
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    key = ("max_wiz" if stat == "wiz" else f"max_{stat}") if args.max else stat
    ci[key] = int(args.value)
    _refresh_career(viewer, full_state, career)
    print(f"{key} -> {ci[key]}")


def _simple_career_setter(field, clamp=None):
    def run(viewer, args):
        full_state = _load(viewer)
        career = _career(full_state)
        ci = career["data"]["chara_info"]
        value = int(args.value)
        if clamp:
            value = max(clamp[0], min(clamp[1], value))
        ci[field] = value
        _refresh_career(viewer, full_state, career)
        print(f"{field} -> {value}")
    return run


def cmd_set_bond(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    T._set_bond(ci, int(args.name), max(0, min(100, int(args.value))))
    _refresh_career(viewer, full_state, career)
    print(f"bond @position {args.name} -> {args.value}")


def cmd_add_skill(viewer, args):
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    skills = ci.setdefault("skill_array", [])
    sid, level = int(args.value), int(args.level)
    for s in skills:
        if s["skill_id"] == sid:
            s["level"] = level
            break
    else:
        skills.append({"skill_id": sid, "level": level})
    _refresh_career(viewer, full_state, career)
    print(f"skill {sid} -> level {level}")


def cmd_items(viewer, args):
    full_state = _load(viewer)
    for it in full_state.get("item_list_state") or []:
        print(f"item {it.get('item_id'):>5}  x{it.get('number')}")


def _item_setter(add):
    def run(viewer, args):
        full_state = _load(viewer)
        items = full_state.setdefault("item_list_state", [])
        iid, num = int(args.name), int(args.value)
        for it in items:
            if it.get("item_id") == iid:
                it["number"] = (it.get("number", 0) + num) if add else num
                new = it["number"]
                break
        else:
            new = num
            items.append({"item_id": iid, "number": num})
        state_store.save_state(viewer, full_state)
        print(f"item {iid} -> x{new}")
    return run


def _wallet_setter(state_key, field):
    def run(viewer, args):
        full_state = _load(viewer)
        wallet = full_state.setdefault(state_key, {})
        wallet[field] = int(args.value)
        state_store.save_state(viewer, full_state)
        print(f"{field} -> {wallet[field]}")
    return run


def cmd_give_paid_carats(viewer, args):
    from app.handlers import shop as S
    full_state = _load(viewer)
    wallet = S.grant_carats(full_state, int(args.value), paid=True)
    state_store.save_state(viewer, full_state)
    print(f"coin (paid) -> {wallet['coin']}  (fcoin/free unchanged at {wallet['fcoin']})")


def _paid_names(name):
    from app import paid_features as PF
    if name == "all":
        return PF.FEATURES
    if name not in PF.FEATURES:
        sys.exit(f"unknown paid feature {name!r}: one of {', '.join(PF.FEATURES)} or all")
    return (name,)


def cmd_paid_features(viewer, args):
    from app import paid_features as PF
    full_state = _load(viewer)
    for name in PF.FEATURES:
        end = PF.end_time(full_state, name)
        status = (f"until {datetime.fromtimestamp(end).strftime(_TIME_FORMAT)}"
                  if PF.active(full_state, name) else "not held")
        print(f"{name:<12} {status}")
    for iid, label in ((PF.REROLL_TICKET_ITEM, "re-roll tickets"),
                       (PF.ORDER_TICKET_ITEM, "spark order tickets")):
        print(f"{label:<20} x{PF.item_count(full_state, iid)}")


def cmd_give_paid_feature(viewer, args):
    from app import paid_features as PF
    full_state = _load(viewer)
    for name in _paid_names(args.name):
        end = PF.grant(full_state, name, int(args.days))
        print(f"{name} -> until {datetime.fromtimestamp(end).strftime(_TIME_FORMAT)}")
    state_store.save_state(viewer, full_state)


def cmd_revoke_paid_feature(viewer, args):
    from app import paid_features as PF
    full_state = _load(viewer)
    for name in _paid_names(args.name):
        PF.revoke(full_state, name)
        print(f"{name} -> revoked")
    state_store.save_state(viewer, full_state)


CAREER_SLOTS_KEY = "career_save_slots"


def cmd_career_park(viewer, args):
    full_state = _load(viewer)
    _career(full_state)
    slot = str(args.name)
    slots = full_state.setdefault(CAREER_SLOTS_KEY, {})
    if slot in slots and not getattr(args, "force", False):
        sys.exit(f"slot '{slot}' already holds a career -- use --force to overwrite")
    moved = {}
    for key in T.career_state_keys():
        if key in full_state:
            moved[key] = full_state.pop(key)
    slots[slot] = moved
    state_store.save_state(viewer, full_state)
    turn = ((moved.get(T.STATE_KEY) or {}).get("data", {}).get("chara_info", {}) or {}).get("turn")
    print(f"parked active career (turn {turn}) into slot '{slot}' ({len(moved)} keys)")


def cmd_career_resume(viewer, args):
    full_state = _load(viewer)
    if isinstance(full_state.get(T.STATE_KEY), dict):
        sys.exit("a career is already active -- park it first")
    slots = full_state.get(CAREER_SLOTS_KEY) or {}
    slot = str(args.name)
    if slot not in slots:
        sys.exit(f"no slot '{slot}' -- have: {sorted(slots) or '(none)'}")
    for key, value in slots.pop(slot).items():
        full_state[key] = value
    state_store.save_state(viewer, full_state)
    career = full_state.get(T.STATE_KEY) or {}
    _refresh_career(viewer, full_state, career) if isinstance(career, dict) and career else None
    turn = (career.get("data", {}).get("chara_info", {}) or {}).get("turn")
    print(f"resumed slot '{slot}' (turn {turn})")


def cmd_career_slots(viewer, args):
    full_state = _load(viewer)
    slots = full_state.get(CAREER_SLOTS_KEY) or {}
    if not slots:
        print("no parked careers"); return
    for name, blob in sorted(slots.items()):
        ci = ((blob.get(T.STATE_KEY) or {}).get("data", {}) or {}).get("chara_info", {}) or {}
        print(f"  {name}: card {ci.get('card_id')} turn {ci.get('turn')} "
              f"({len(blob)} keys)")


_CARAT_ITEM_ID = 43


def cmd_send_mail(viewer, args):
    from app.handlers import presents as P
    category = int(args.category)
    item = int(args.item)
    if category == 90 and item != _CARAT_ITEM_ID:
        print(f"note: category 90 (carats) always uses item_id {_CARAT_ITEM_ID} "
              f"in every real capture -- overriding item {item}")
        item = _CARAT_ITEM_ID
    entry = P.admin_send(viewer, item_category=category, item_id=item,
                         item_num=int(args.num), message=getattr(args, "message", "") or "")
    print(f"queued present {entry.get('present_id')}: "
          f"cat {category} item {item} x{args.num}")


def cmd_clear_mail(viewer, args):
    from app.handlers import presents as P
    full_state = _load(viewer)
    box = full_state.get(P.PRESENT_BOX_KEY) or {}
    presents = box.get("presents", [])
    if not presents:
        print("present box already empty"); return
    unclaimed = [p for p in presents if (p.get("state") or 0) == 0]
    if args.all:
        kept, removed = [], len(presents)
    else:
        kept = [p for p in presents if (p.get("state") or 0) != 0]
        removed = len(presents) - len(kept)
    if not removed:
        print(f"nothing to clear ({len(kept)} claimed row(s) left -- use --all)")
        return
    box["presents"] = kept
    full_state[P.PRESENT_BOX_KEY] = box
    state_store.save_state(viewer, full_state)
    print(f"cleared {removed} present(s) for viewer {viewer} "
          f"({len(unclaimed)} were unclaimed, {len(kept)} row(s) kept)")


def cmd_idle_career_complete(viewer, args):
    from app.handlers import idle_single_mode as I
    full_state = _load(viewer)
    career = _career(full_state)
    ci = career["data"]["chara_info"]
    I._max_out_career(ci)
    skill_ids = I._apply_hints(ci)
    I._persist_card_hints(full_state, ci.get("card_id"), skill_ids)
    _refresh_career(viewer, full_state, career)
    print(f"career maxed: stats->{I._MAX_STAT} SP->{I._MAX_SP} "
          f"hints on {len(skill_ids)} skills (turn {ci['turn']}, state {ci['state']}) "
          f"-- call single_mode_live/finish in-game (or resume the client) to bank it")


def cmd_max_support_cards(viewer, args):
    from app.handlers import cards as C
    from app.handlers import collection
    full_state = _load(viewer)
    entries = full_state.get(collection.SUPPORT_CARD_KEY) or []
    if not entries:
        print("no owned support cards"); return
    changed = 0
    for e in entries:
        cap = C._support_level_cap(e.get("support_card_id"), C._MAX_LIMIT_BREAK)
        if cap is None:
            continue
        e["limit_break_count"] = C._MAX_LIMIT_BREAK
        e["exp"] = cap[1]
        changed += 1
    state_store.save_state(viewer, full_state)
    print(f"maxed {changed}/{len(entries)} support cards "
          f"(limit_break -> {C._MAX_LIMIT_BREAK}, exp -> each card's own rarity/uncap cap)")


def cmd_max_cards(viewer, args):
    from app.handlers import collection
    from app.handlers import single_mode_team as ST
    full_state = _load(viewer)
    cards = full_state.get(collection.CARD_LIST_KEY) or []
    if not cards:
        print("no owned uma cards"); return
    changed = 0
    for c in cards:
        c["talent_level"] = 5
        c["rarity"] = 5
        skills = c.get("skill_data_array") or []
        for s in skills:
            if isinstance(s, dict):
                s["level"] = ST._MAX_HINT_LEVEL
        changed += 1
    state_store.save_state(viewer, full_state)
    print(f"maxed {changed}/{len(cards)} uma cards "
          f"(talent_level -> 5, rarity -> 5, skill_data_array levels -> {ST._MAX_HINT_LEVEL})")


def cmd_max_roster(viewer, args):
    from app.handlers import trained_chara as TC
    TC._get_or_seed_roster(viewer)
    full_state = _load(viewer)
    roster = full_state.get(TC.ROSTER_KEY) or []
    if not roster:
        print("no roster entries"); return
    for rec in roster:
        TC._max_out_veteran_fields(rec)
    state_store.save_state(viewer, full_state)
    print(f"maxed {len(roster)} roster umas "
          f"(stats -> 9999, aptitudes -> S, rank -> {TC._max_rank_id()}, "
          f"talent_level -> 5, rarity -> 5, every non-negative skill at max level)")


def cmd_give_all_cards(viewer, args):
    from app.handlers import collection
    from app import master_data
    full_state = _load(viewer)

    cards = full_state.setdefault(collection.CARD_LIST_KEY, [])
    owned_card_ids = {c.get("card_id") for c in cards}
    charas = full_state.setdefault(collection.CHARA_LIST_KEY, [])
    owned_chara_ids = {c.get("chara_id") for c in charas}
    added_cards = 0
    for row in master_data.query("SELECT id, chara_id, default_rarity FROM card_data ORDER BY id"):
        if row["id"] in owned_card_ids:
            continue
        if not row["default_rarity"]:
            continue
        cards.append({"card_id": row["id"], "rarity": row["default_rarity"],
                      "talent_level": 1, "create_time": _now_str(),
                      "skill_data_array": []})
        added_cards += 1
        if row["chara_id"] not in owned_chara_ids:
            charas.append({"chara_id": row["chara_id"], "training_num": 0, "love_point": 0,
                           "fan": 1, "max_grade": 0, "dress_id": 2, "mini_dress_id": 2,
                           "love_point_pool": 0})
            owned_chara_ids.add(row["chara_id"])

    support_cards = full_state.setdefault(collection.SUPPORT_CARD_KEY, [])
    owned_support_ids = {c.get("support_card_id") for c in support_cards}
    added_support = 0
    for row in master_data.query("SELECT id FROM support_card_data ORDER BY id"):
        if row["id"] in owned_support_ids:
            continue
        support_cards.append({"viewer_id": "<redacted>", "support_card_id": row["id"],
                              "exp": 0, "limit_break_count": 0, "favorite_flag": 0, "stock": 0})
        added_support += 1

    state_store.save_state(viewer, full_state)
    print(f"added {added_cards} new uma cards, {added_support} new support cards "
          f"({len(cards)} uma cards / {len(support_cards)} support cards total owned) "
          f"-- run max-all to fully max them")


def cmd_max_all(viewer, args):
    cmd_max_support_cards(viewer, args)
    cmd_max_cards(viewer, args)
    cmd_max_roster(viewer, args)


def cmd_reset_password(viewer, args):
    from app import accounts
    accounts.reset_transition_password(viewer)
    print(f"viewer {viewer}: transition password reset -- next successful "
          f"account/chain_by_transition_code login (any password) becomes "
          f"the new real password")


def cmd_add_friend_card(viewer, args):
    level = int(args.level) if args.level is not None else None
    limit_break = int(args.limit_break) if args.limit_break is not None else None
    entry = T.inject_friend_support_card(
        viewer, int(args.support_card_id), level=level,
        limit_break_count=limit_break, name=args.name)
    usc = entry["user_support_card"]
    print(f"added friend '{entry['name']}' lending support_card {usc['support_card_id']} "
          f"(exp {usc['exp']}, limit_break {usc['limit_break_count']})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--viewer", default=DEFAULT_VIEWER)
    p.add_argument("--region", default=region.PROCESS_REGION, choices=region.REGIONS,
                   help="which server's data to edit (default viewer follows it)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *fields, **kw):
        sp = sub.add_parser(name)
        for f in fields:
            sp.add_argument(f)
        for k, v in kw.items():
            sp.add_argument(k, **v)
        sp.set_defaults(fn=fn)

    add("show", cmd_show)
    add("set-turn", cmd_set_turn, "value")
    sp = sub.add_parser("set-stat"); sp.add_argument("name"); sp.add_argument("value")
    sp.add_argument("--max", action="store_true"); sp.set_defaults(fn=cmd_set_stat)
    add("set-sp", _simple_career_setter("skill_point"), "value")
    add("set-fans", _simple_career_setter("fans"), "value")
    add("set-vital", _simple_career_setter("vital"), "value")
    add("set-mood", _simple_career_setter("motivation", clamp=(1, 5)), "value")
    add("set-grade", _simple_career_setter("chara_grade", clamp=(1, 10)), "value")
    add("set-bond", cmd_set_bond, "name", "value")
    sp = sub.add_parser("add-skill"); sp.add_argument("value")
    sp.add_argument("--level", default=1); sp.set_defaults(fn=cmd_add_skill)
    add("items", cmd_items)
    add("give-item", _item_setter(add=True), "name", "value")
    add("set-item", _item_setter(add=False), "name", "value")
    sp = sub.add_parser("career-park"); sp.add_argument("name")
    sp.add_argument("--force", action="store_true"); sp.set_defaults(fn=cmd_career_park)
    add("career-resume", cmd_career_resume, "name")
    add("career-slots", cmd_career_slots)
    sp = sub.add_parser("send-mail"); sp.add_argument("category"); sp.add_argument("item")
    sp.add_argument("num"); sp.add_argument("--message", default="")
    sp.set_defaults(fn=cmd_send_mail)
    sp = sub.add_parser("clear-mail")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_clear_mail)
    add("set-fcoin", _wallet_setter("coin_info_state", "fcoin"), "value")
    add("set-coin", _wallet_setter("coin_info_state", "coin"), "value")
    add("give-paid-carats", cmd_give_paid_carats, "value")
    add("set-tp", _wallet_setter("tp_info_state", "current_tp"), "value")
    add("set-rp", _wallet_setter("rp_info_state", "current_rp"), "value")
    add("idle-career-complete", cmd_idle_career_complete)
    sp = sub.add_parser("add-friend-card"); sp.add_argument("support_card_id")
    sp.add_argument("--level", default=None)
    sp.add_argument("--limit-break", default=None)
    sp.add_argument("--name", default="Friend")
    sp.set_defaults(fn=cmd_add_friend_card)
    add("give-all-cards", cmd_give_all_cards)
    add("reset-password", cmd_reset_password)
    add("max-support-cards", cmd_max_support_cards)
    add("max-cards", cmd_max_cards)
    add("max-roster", cmd_max_roster)
    add("max-all", cmd_max_all)
    add("paid-features", cmd_paid_features)
    sp = sub.add_parser("give-paid-feature"); sp.add_argument("name")
    sp.add_argument("--days", default=30); sp.set_defaults(fn=cmd_give_paid_feature)
    add("revoke-paid-feature", cmd_revoke_paid_feature, "name")

    args = p.parse_args()
    viewer = "".join(str(args.viewer).split())
    args.fn(viewer, args)


if __name__ == "__main__":
    main()
