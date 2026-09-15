"""Admin/cheat CLI for the private Umamusume server.

Edits the live per-viewer state (server/data/state.sqlite3) directly -- the
server re-reads state from SQLite on every request, so changes apply on the
client's NEXT action (train once / reopen career mode), no server restart.

Usage (run from anywhere):
    python admin.py show
    python admin.py set-turn 22
    python admin.py set-stat speed 800        # speed|stamina|power|guts|wiz
    python admin.py set-stat speed 1600 --max # the stat's CAP instead
    python admin.py set-sp 9999
    python admin.py set-fans 100000
    python admin.py set-vital 100
    python admin.py set-mood 5                # 1-5
    python admin.py set-grade 4               # class (1 maiden .. 10)
    python admin.py set-bond 3 80             # support position 1-6, bond 0-100
    python admin.py add-skill 200011 [--level 2]
    python admin.py items                     # list owned items
    python admin.py give-item 45 10           # ADD 10 of item 45
    python admin.py set-item 45 99            # SET item 45 count to 99
    python admin.py clear-mail                # delete every UNCLAIMED present in
                                              # the viewer's present box
                                              # (--all also drops the claimed
                                              # ledger rows)
    python admin.py set-fcoin 999999 | set-coin N | set-tp N | set-rp N
    python admin.py give-paid-carats 1500     # like set-fcoin but actually
                                              # spent last (free carats go
                                              # first) -- see shop.py
    python admin.py idle-career-complete      # instantly max + finish the
                                              # ACTIVE career (Independent
                                              # Career Training's cheat-
                                              # complete path from the CLI)
    python admin.py add-friend-card 30052     # add a borrowable "friend"
                                              # lending support card 30052
                                              # [--level N] [--limit-break N]
                                              # [--name NAME] (defaults: max
                                              # level, max limit break)
    python admin.py reset-password            # let this account be logged
                                              # into (via the real account-
                                              # link flow) with ANY password;
                                              # whatever password is used
                                              # becomes the new real one
    python admin.py give-all-cards            # add every uma card + support
                                              # card in master.mdb that isn't
                                              # already owned (new, not maxed
                                              # -- run max-all after for that)
    python admin.py max-support-cards         # max exp + limit break on every
                                              # OWNED support card (real
                                              # per-card/rarity caps from
                                              # master.mdb, not a guess)
    python admin.py max-cards                 # max every OWNED uma card
                                              # (card_collection): talent_level
                                              # -> 5, skill_data_array hint
                                              # levels -> 5 (existing entries
                                              # only, none added)
    python admin.py max-roster                # max every veteran in the
                                              # finished-career roster box
                                              # (stats/aptitudes/rank/skills,
                                              # NOT the active in-progress
                                              # career)
    python admin.py max-all                   # max-support-cards + max-cards
                                              # + max-roster
    python admin.py --viewer 12345 show       # different account (default: the
                                              # Steam client's viewer id)

IMPORTANT invariant (learned the hard way): whenever chara_info.turn or the
career's progress changes, the persisted home_info (the training screen's
command lock state) MUST be recomputed for the new turn -- it is only ever
rebuilt as a side effect of exec_command/race_out, so a raw turn edit leaves
a stale locked/unlocked screen (e.g. everything locked on a normal turn).
Every career-mutating command here ends with _refresh_career() which does
that recompute exactly like the server's own turn-advance path.

For anything beyond a raw turn write -- moving a run to a turn AND reconciling
the goal banner, class, race history, the fired-event ledger and the
in-flight contexts with it, plus editing any career property by path -- use
career.py (`python career.py show` / `set-turn 45 --fill-races` / `set speed
900`). set-turn here stays the minimal version it always was."""

import argparse
import os
import sys
from datetime import datetime

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"   # matches load.py's TIME_FORMAT / gacha.py's _TIME_FORMAT


def _now_str():
    return datetime.now().strftime(_TIME_FORMAT)

# The server package lives next to this file. This used to be hardcoded to a
# Windows checkout, which made admin.py unusable anywhere else (ModuleNotFound:
# app). Derive it from this file's own location instead, keeping the old path
# as a fallback so a Windows checkout still works.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (os.path.join(_HERE, "server"),
                   r"c:\Users\Systemless\Documents\private server\server"):
    if os.path.isdir(_candidate):
        sys.path.insert(0, _candidate)
        break

from app import state as state_store  # noqa: E402
from app.handlers import single_mode_team as T  # noqa: E402
from app.handlers import single_mode_events  # noqa: E402

DEFAULT_VIEWER = "802445340143"
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
    """Recompute home_info lock/preview state for the (possibly new) turn and
    persist -- the same recompute the server's own turn-advance does."""
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
    full_state.pop(T.RACE_CTX_KEY, None)  # a stale race ctx would hijack check_event
    _refresh_career(viewer, full_state, career)
    print(f"turn -> {ci['turn']} (training screen recomputed)")
    # The running client is still holding its OWN cached career at the old
    # turn, and the server now REFUSES any action stamped with it rather
    # than rewinding the run to match (single_mode_team._client_turn_conflict).
    # So say it out loud: without a reload the next training just errors.
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
    """Grant carats to the PAID bucket specifically (coin_info_state.coin) --
    a genuinely separate wire field the client displays on its own (real
    screenshot, 2026-08-21: "PAID 0 / FREE N" as two distinct numbers), not
    a combined total. set-fcoin/set-coin already do this directly; this just
    matches the give-item-style "add" convention instead of set-coin's "set"."""
    from app.handlers import shop as S
    full_state = _load(viewer)
    wallet = S.grant_carats(full_state, int(args.value), paid=True)
    state_store.save_state(viewer, full_state)
    print(f"coin (paid) -> {wallet['coin']}  (fcoin/free unchanged at {wallet['fcoin']})")


CAREER_SLOTS_KEY = "career_save_slots"


def cmd_career_park(viewer, args):
    """Move the ACTIVE career into a named save slot (career leaves the
    account; the client sees 'no career running'). All 42 per-career state
    keys travel together -- career_state_keys() is the single source."""
    full_state = _load(viewer)
    _career(full_state)                       # refuses when nothing to park
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
    """Restore a parked career into the active position. Refuses when a career
    is already active (park it first) so two runs can never merge."""
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


_CARAT_ITEM_ID = 43  # category 90 (carats) ALWAYS uses item_id 43 in every real
                     # capture (captures/20260818_081331/0010_present_index.json)
                     # -- reward_id is ignored server-side for this category
                     # (presents.py's _grant credits fcoin regardless), but the
                     # CLIENT apparently needs a real item_data id to render the
                     # present's icon/label; item_id 0 silently failed to show
                     # in-game (found live 2026-08-18).


def cmd_send_mail(viewer, args):
    """Compose a present-box mail: item + amount (+ message id). The present
    lands unclaimed; the client's mailbox shows and redeems it. The present
    handlers own the shape -- this only appends to their inbox key."""
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
    """Empty the present box: drop every UNCLAIMED present (what the client's
    mailbox actually shows), or with --all every row including the claimed
    ledger entries. Claimed rows are kept by default because presents.py's
    _receive uses them as the already-redeemed ledger -- deleting one only
    reclaims list space, it can never un-grant the reward."""
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
    """CLI shortcut for idle_single_mode's cheat-complete path (see server/
    app/handlers/idle_single_mode.py): maxes every stat to 9999, skill_point
    to 999999, and gives a max-level hint for every skill the active
    trainee can learn, then marks the run COMPLETE (turn 78, state 2) --
    reusing the SAME _max_out_career/_apply_hints/_persist_card_hints
    functions idle_single_mode/end runs, so this and "start Independent
    Career Training and wait ~10s in-game" produce identical results.
    Does NOT itself call single_mode_live/finish -- the client still needs
    to make that call (or you resume the client) to bank it as a roster
    trained_chara; this only prepares the career for that."""
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
    """Max every OWNED support card (collection.SUPPORT_CARD_KEY): limit_break
    to the real max uncap (cards.py's _MAX_LIMIT_BREAK, 4) and exp to that
    card's real cumulative-exp cap AT that uncap (cards.py's
    _support_level_cap, sourced from master.mdb support_card_limit x
    support_card_level -- the exact same lookup support_card/strengthen
    itself enforces as a ceiling), not a guessed number. Does not touch
    stock/favorite_flag/possess_time."""
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
            continue  # card_id not in master data -- leave alone rather than guess
        e["limit_break_count"] = C._MAX_LIMIT_BREAK
        e["exp"] = cap[1]
        changed += 1
    state_store.save_state(viewer, full_state)
    print(f"maxed {changed}/{len(entries)} support cards "
          f"(limit_break -> {C._MAX_LIMIT_BREAK}, exp -> each card's own rarity/uncap cap)")


def cmd_max_cards(viewer, args):
    """Max every OWNED uma card (collection.CARD_LIST_KEY, state key
    "card_collection" -- the trainable cards, NOT the finished-career
    veteran roster max-roster handles): talent_level -> 5 (trained_chara.
    py's own established max-veteran ceiling, build_maxed_veteran's
    rec["talent_level"] = 5), rarity -> 5 (same convention --
    build_maxed_veteran's rec["rarity"] = 5 -- and confirmed every real
    card_id's card_rarity_data progression tops out at rarity 5, none lower,
    so this is never overshooting a card's real ceiling), and every EXISTING
    skill_data_array[].level -> 5 (single_mode_team.py's _MAX_HINT_LEVEL, the
    real per-career hint ladder's ceiling -- corroborated by real level-5
    entries in captured finished careers). Mirrors idle_single_mode.py's
    _persist_card_hints mutation shape ({skill_id, level} dicts) but only
    raises levels on entries already present -- never adds new skill hints
    that weren't already there."""
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
    """Max every veteran in the finished-career roster box (trained_chara.
    ROSTER_KEY -- NOT the active in-progress career): reuses trained_chara.
    _max_out_veteran_fields, the SAME "fully maxed" convention
    build_maxed_veteran already established for its single synthetic cheat
    veteran (9999/stat, S=8 every aptitude, rank/chara_grade/fans/rank_score
    pinned to this codebase's real ceilings, every non-negative skill in the
    game at its real max level) -- applied in place to every REAL roster
    entry instead of building a new synthetic one."""
    from app.handlers import trained_chara as TC
    TC._get_or_seed_roster(viewer)  # ensure seeded/up-to-date before we mutate it
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
    """Add every uma card (master.mdb card_data) and every support card
    (master.mdb support_card_data) that isn't already owned, using the exact
    same real entry shape gacha.py's _grant builds for a freshly-drawn new
    card: uma cards land at talent_level 1 with no skill hints yet, support
    cards at exp 0 / limit_break_count 0 -- not maxed, just owned. Also
    seeds a chara_collection entry for any character newly unlocked by an
    added uma card (same as a real gacha pull), so new cards show up without
    a client restart. Existing owned cards are left untouched. Run max-all
    afterward if you want everything this adds fully maxed too."""
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
            # default_rarity 0 (live-confirmed: ids 9100101 / 9101101, both
            # all-zero stats/talent_group_id/get_piece_id) are unplayable
            # system placeholders, not real ownable cards -- the client's own
            # WorkCardData.CalcStatus NullRef's trying to resolve a talent
            # group for one (Footer.UpdateStatus -> HasTalentUpgradeEnableCard
            # / HasHintLvUpEnableCard, right on the post-login home screen,
            # aborting every in-flight request and surfacing as a bare
            # "connection error" -- confirmed live via Player.log after
            # give-all-cards put one of these in a card_collection).
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
    """max-support-cards + max-cards + max-roster in one call."""
    cmd_max_support_cards(viewer, args)
    cmd_max_cards(viewer, args)
    cmd_max_roster(viewer, args)


def cmd_reset_password(viewer, args):
    """Puts an account into the 'accept any password' state for the real
    account-linking flow (account/publish_transition_code -> .../get_by_ or
    chain_by_transition_code -- the game's own 'log back into my account'
    mechanism). For a player who forgot their transition password (or never
    set one): from ANY other account (even a fresh throwaway signup), call
    account/chain_by_transition_code with {input_viewer_id: this viewer,
    password: <anything non-empty>} and it succeeds -- and whatever password
    was actually typed there is automatically saved as this account's new
    real password (accounts.claim_reset_password), so the window closes
    after that first successful login and only that password works from
    then on."""
    from app import accounts
    accounts.reset_transition_password(viewer)
    print(f"viewer {viewer}: transition password reset -- next successful "
          f"account/chain_by_transition_code login (any password) becomes "
          f"the new real password")


def cmd_add_friend_card(viewer, args):
    """CLI for single_mode_team.inject_friend_support_card -- adds one
    always-borrowable synthetic "friend" lending a specific support card,
    for the pre-career "Borrow Card" screen when there are no real friends
    to borrow from (user-reported 2026-08-18: a required support-deck slot
    blocked career start entirely, "There are no Support Cards to
    borrow"). --level defaults to that card's own max; --limit-break
    defaults to 4 (max break)."""
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

    args = p.parse_args()
    # Viewer ids are pure digits, but a trainer id copy-pasted from the game's
    # own UI (which displays it grouped, e.g. "802 445 340 143") carries
    # spaces along -- strip ALL whitespace so that still resolves to the real
    # account instead of silently minting a brand-new bogus row keyed on the
    # space-containing string (live-hit 2026-08-20: reset-password --viewer
    # "802 445 340 143" created a phantom account instead of touching the
    # real 802445340143, so the reset never took effect where it was needed).
    viewer = "".join(str(args.viewer).split())
    args.fn(viewer, args)


if __name__ == "__main__":
    main()
