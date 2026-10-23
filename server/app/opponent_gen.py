from __future__ import annotations

import bisect
import calendar
import collections
import gzip
import hashlib
import json
import math
import random
import re
import time
from pathlib import Path

from . import champions_pool as POOL
from . import master_data as M
from . import rating_formula as R

STATS = ("speed", "stamina", "power", "guts", "wiz")
ROLES = ("ace", "debuffer", "runaway")
RUNAWAY_SKILL = 202051
DEBUFFER_MIN = 4
LAUNCH_STAMP = 1_700_000_000
GLOBAL_LAUNCH = 1_750_896_000
SOURCE_WEIGHT = {"deck": 4.0, "parents": 3.0, "awaken": 3.0, "general": 1.0, "other": 0.5}
_STYLE_APT = {1: "proper_running_style_nige", 2: "proper_running_style_senko",
              3: "proper_running_style_sashi", 4: "proper_running_style_oikomi"}
_DIST_APT = ("proper_distance_short", "proper_distance_mile", "proper_distance_middle", "proper_distance_long")
_LETTER = "GFEDCBAS"


class _Master:
    def __init__(self):
        self.skill = {r["id"]: dict(r) for r in M.query(
            "SELECT id, rarity, group_id, grade_value, ability_type_1_1, target_type_1_1, "
            "float_ability_value_1_1, precondition_1, condition_1 FROM skill_data")}
        self.card = {r["card_id"]: dict(r) for r in M.query(
            "SELECT c.* FROM card_rarity_data c WHERE rarity="
            "(SELECT MAX(rarity) FROM card_rarity_data WHERE card_id=c.card_id)")}
        self.unique = {}
        for cid, row in self.card.items():
            s = M.query_one("SELECT skill_id1 FROM skill_set WHERE id=?", (row["skill_set"],))
            self.unique[cid] = s["skill_id1"] if s else 0
        self.hints = collections.defaultdict(set)
        for r in M.query("SELECT support_card_id c, hint_value_1 s FROM single_mode_hint_gain WHERE hint_gain_type=0"):
            self.hints[r["c"]].add(r["s"])
        for r in M.query("SELECT d.id c, s.* FROM support_card_data d JOIN skill_set s ON s.id=d.skill_set_id"):
            self.hints[r["c"]].update(r[f"skill_id{i}"] for i in range(1, 11)
                                      if f"skill_id{i}" in r.keys() and r[f"skill_id{i}"])
        self.awaken = collections.defaultdict(set)
        for r in M.query("SELECT available_skill_set_id c, skill_id s FROM available_skill_set"):
            self.awaken[r["c"]].add(r["s"])
        self.factor_skill = collections.defaultdict(set)
        for r in M.query("SELECT f.factor_id, e.value_1 FROM succession_factor f JOIN succession_factor_effect e "
                         "ON e.factor_group_id=f.factor_group_id WHERE e.target_type=41"):
            self.factor_skill[r["factor_id"]].add(r["value_1"])
        self.factor_type = {r["factor_id"]: r["factor_type"] for r in M.query(
            "SELECT factor_id, factor_type FROM succession_factor")}
        self.src_by_group = collections.defaultdict(set)
        for c, ss in self.hints.items():
            for s in ss:
                self.src_by_group[self.group(s)].add(("s", c))
        for c, ss in self.awaken.items():
            for s in ss:
                self.src_by_group[self.group(s)].add(("c", c))
        rel = {(r["t"], r["c"]): r["s"] for r in M.query(
            "SELECT a.card_type t, a.card_id c, MIN(g.start_date) s FROM gacha_available a "
            "JOIN gacha_data g ON g.id=a.gacha_id GROUP BY a.card_type, a.card_id")}
        self.card_rel = {c: s for (t, c), s in rel.items() if t == 1}
        self.support = {r["id"]: dict(r) for r in M.query(
            "SELECT id, chara_id, rarity, command_id, support_card_type, start_date FROM support_card_data")}
        self.sup_rel = {}
        for sid, r in self.support.items():
            s = rel.get((2, sid), r["start_date"] or 0)
            self.sup_rel[sid] = 0 if s < LAUNCH_STAMP else s
        self.evo_date = {}
        if M.has_table("skill_upgrade_description"):
            for r in M.query("SELECT skill_id, MIN(start_date) s FROM skill_upgrade_description GROUP BY skill_id"):
                self.evo_date[r["skill_id"]] = r["s"]

    def group(self, sid):
        r = self.skill.get(sid)
        return r["group_id"] if r else -sid

    def debuff(self, sid) -> bool:
        r = self.skill.get(sid)
        return bool(r) and r["target_type_1_1"] != 1 and (r["float_ability_value_1_1"] or 0) < 0

    def func(self, sid) -> tuple:
        r = self.skill.get(sid) or {}
        tier = "inherit" if 900000 <= sid < 1000000 else "gold" if r.get("rarity") == 2 else "white"
        return tier, "debuff" if self.debuff(sid) else r.get("ability_type_1_1")

    def gv(self, sid) -> int:
        return int((self.skill.get(sid) or {}).get("grade_value") or 0)


_TERM = re.compile(r"^([a-z_]+)(==|!=|>=|<=|>|<)(-?\d+)$")
_OPS = {"==": lambda x, y: x == y, "!=": lambda x, y: x != y, ">=": lambda x, y: x >= y,
        "<=": lambda x, y: x <= y, ">": lambda x, y: x > y, "<": lambda x, y: x < y}


def _cond_ok(cond, env) -> bool:
    if not cond:
        return True
    for alt in cond.split("@"):
        good = True
        for term in alt.split("&"):
            m = _TERM.match(term.strip())
            if not m or m.group(1) not in env:
                continue
            have = env[m.group(1)]
            have = have if isinstance(have, (set, frozenset)) else {have}
            if not any(_OPS[m.group(2)](h, int(m.group(3))) for h in have):
                good = False
                break
        if good:
            return True
    return False


def course_env(race_instance_id: int, season=None, weather=None, ground_condition=None) -> dict:
    row = M.query_one("SELECT rcs.distance, rcs.ground, rcs.turn, rcs.race_track_id FROM race_instance ri "
                      "JOIN race r ON r.id=ri.race_id JOIN race_course_set rcs ON rcs.id=r.course_set "
                      "WHERE ri.id=?", (race_instance_id,))
    if not row:
        raise ValueError(f"race instance {race_instance_id} not in master")
    d = row["distance"]
    track = M.query_one("SELECT text FROM text_data WHERE category=35 AND [index]=?", (row["race_track_id"],))
    return {"distance_type": 1 if d <= 1400 else 2 if d <= 1800 else 3 if d <= 2400 else 4,
            "course_distance": d, "is_basis_distance": int(d % 400 == 0), "ground_type": row["ground"],
            "rotation": row["turn"], "track_id": row["race_track_id"],
            "season": {season} if season else {1, 2, 3, 4, 5},
            "weather": {weather} if weather else {1, 2},
            "ground_condition": {ground_condition} if ground_condition else {1},
            "_ri": race_instance_id,
            "_label": f"{track['text'] if track else row['race_track_id']} {d:,} m "
                      f"{'Dirt' if row['ground'] == 2 else 'Turf'}"}


def target_course(target: dict) -> tuple[dict, int | None]:
    if "champions_id" in target:
        cm = int(target["champions_id"])
        r = M.query_one("SELECT race_instance_id, race_condition_id FROM champions_race_condition "
                        "WHERE champions_id=? ORDER BY round_id LIMIT 1", (cm,))
        if not r:
            raise ValueError(f"CM {cm} has no champions_race_condition (ported CM? run inside its pack)")
        c = M.query_one("SELECT season, weather, ground FROM race_condition WHERE id=?", (r["race_condition_id"],))
        env = course_env(r["race_instance_id"], *(c["season"], c["weather"], c["ground"]) if c else ())
        env["_label"] = f"CM{cm} {env['_label']}"
        return env, _cm_start(cm)
    if "heroes_id" in target:
        hid = int(target["heroes_id"])
        r = M.query_one("SELECT race_instance_id, season FROM heroes_race_condition WHERE heroes_id=? AND stage=1",
                        (hid,)) if M.has_table("heroes_race_condition") else None
        if not r:
            raise ValueError(f"LoH {hid} has no heroes_race_condition (run inside the pack)")
        env = course_env(r["race_instance_id"], r["season"])
        env["_label"] = f"LoH{hid} {env['_label']}"
        s = M.query_one("SELECT start_date FROM heroes_data WHERE heroes_id=?", (hid,)) \
            if M.has_table("heroes_data") else None
        return env, (s["start_date"] if s else None)
    return course_env(int(target["race_instance_id"])), None


def _cm_start(cm: int) -> int | None:
    from . import custom_support as C
    r = M.get_connection().execute("SELECT start_date FROM champions_schedule WHERE id=?", (cm,)).fetchone()
    if r and r["start_date"]:
        return r["start_date"]
    for b in C.unreleased_bundles():
        for row in (b.get("tables") or {}).get("champions_schedule") or []:
            if row.get("id") == cm and row.get("start_date"):
                return row["start_date"] if row["start_date"] >= GLOBAL_LAUNCH else int(time.time())
    r = M.query_one("SELECT start_date FROM champions_schedule WHERE id=?", (cm,))
    return r["start_date"] if r and r["start_date"] else None


def compatible(mst: _Master, sid: int, style: int, env: dict) -> bool:
    r = mst.skill.get(sid)
    if not r:
        return False
    if 100000 <= sid < 200000:
        return True
    e = dict(env, running_style=1 if style == 5 else style)
    return _cond_ok(r["precondition_1"], e) and _cond_ok(r["condition_1"], e)


def _ref_index() -> dict:
    try:
        rows = json.loads((POOL.corpus_root() / "index.json").read_text())
    except (OSError, ValueError):
        rows = []
    return {e["snapshot"]: e for e in rows}


def role_of(mst: _Master, m: dict) -> str:
    skills = [s for s, _ in m.get("skills") or []]
    if int(m.get("racingStyle") or 0) == 5 or RUNAWAY_SKILL in skills:
        return "runaway"
    label = int(m.get("style") or 0)
    if label == 6:
        return "debuffer"
    if 1 <= label <= 5:
        return "ace"
    if sum(mst.debuff(s) for s in skills) >= DEBUFFER_MIN:
        return "debuffer"
    return "ace"


def _similarity(a: dict, b: dict) -> float:
    w = math.exp(-abs(a["course_distance"] - b["course_distance"]) / 300)
    if a["distance_type"] == b["distance_type"]:
        w *= 2
    if a["ground_type"] != b["ground_type"]:
        w *= 0.25
    if a["track_id"] == b["track_id"]:
        w *= 1.25
    return w


def _snapshot_date(name: str, entry: dict) -> int | None:
    m = re.match(r"cm(\d+)", name)
    if m:
        r = M.get_connection().execute("SELECT start_date FROM champions_schedule WHERE id=?",
                                       (int(m.group(1)),)).fetchone()
        if r and r["start_date"] and r["start_date"] >= GLOBAL_LAUNCH:
            return r["start_date"]
    d = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", name)
    return calendar.timegm(time.strptime("".join(d.groups()), "%Y%m%d")) + 86399 if d else None


class Corpus:

    def __init__(self, mst: _Master, env: dict, refs="auto"):
        self.mst, self.env = mst, env
        idx = _ref_index()
        self.snaps = {}
        named = set(refs) if refs != "auto" else set()
        cms = set()
        for s in POOL.snapshots():
            if s["cm"] in cms and s["snapshot"] not in named:
                continue
            cms.add(s["cm"])
            ri = (idx.get(s["snapshot"]) or {}).get("raceInstanceId")
            try:
                senv = course_env(int(ri)) if ri else None
            except ValueError:
                senv = None
            if senv:
                self.snaps[s["snapshot"]] = {"env": senv, "w": _similarity(env, senv),
                                             "teams": POOL.teams(s["snapshot"])}
        if not self.snaps:
            raise RuntimeError("no CM team snapshots (captures/cm_teams)")
        if refs == "auto":
            top = max(v["w"] for v in self.snaps.values())
            self.refs = {k: v["w"] for k, v in self.snaps.items() if v["w"] >= 0.2 * top}
        else:
            self.refs = {k: self.snaps[k]["w"] for k in refs if k in self.snaps}
            if not self.refs:
                raise ValueError(f"none of {refs} is a usable snapshot")
        self.skill_use = collections.defaultdict(collections.Counter)
        self.card_use = collections.defaultdict(collections.Counter)
        self.deck_use = collections.Counter()
        self.parent_use = collections.Counter()
        self.seen = {}
        self.fielded = set()

        def see(kind, i, when):
            k = (kind, int(i))
            if when and (k not in self.seen or when < self.seen[k]):
                self.seen[k] = when

        for name, v in self.snaps.items():
            when = _snapshot_date(name, idx.get(name) or {})
            for t in v["teams"]:
                for m in t["members"]:
                    key = (int(m.get("racingStyle") or 2), role_of(mst, m))
                    self.card_use[key][int(m["card"])] += v["w"]
                    self.fielded.add((int(m["card"]), v["env"]["distance_type"], v["env"]["ground_type"]))
                    see("c", m["card"], when)
                    for s, _ in m.get("skills") or []:
                        self.skill_use[key][int(s)] += v["w"]
                        see("k", s, when)
                    for d in m.get("deck") or []:
                        self.deck_use[int(d["id"])] += v["w"]
                        see("s", d["id"], when)
                    for p in m.get("parents") or []:
                        self.parent_use[int(p.get("cardId") or 0)] += v["w"]
                        see("c", p.get("cardId") or 0, when)
        self._quant = {}

    def same_course(self, snap) -> bool:
        e = self.snaps[snap]["env"]
        return (e["distance_type"], e["ground_type"]) == (self.env["distance_type"], self.env["ground_type"])

    def quantile(self, snap, band, style, role, key, value) -> float:
        k = (snap, tuple(band), style, role, key)
        if k not in self._quant:
            vals = []
            for t in self.snaps[snap]["teams"]:
                if band[0] <= t["_percentile"] <= band[1]:
                    for m in t["members"]:
                        if int(m.get("racingStyle") or 2) == style and role_of(self.mst, m) == role:
                            vals.append(self.measure(m, key))
            self._quant[k] = sorted(vals)
        vals = self._quant[k]
        if len(vals) < 2:
            return 0.5
        lo, hi = bisect.bisect_left(vals, value), bisect.bisect_right(vals, value)
        return min(1.0, max(0.0, ((lo + hi) / 2) / len(vals)))

    def measure(self, m, key):
        if key == "skills":
            return len(m.get("skills") or [])
        if key == "golds":
            return sum(self.mst.func(int(s))[0] == "gold" for s, _ in m.get("skills") or [])
        return m["stats"][key]


def _tier(spec, p) -> dict:
    for t in spec.get("tiers") or []:
        lo, hi = t["pct"]
        if lo <= p <= hi:
            return t
    return {"pct": [0, 100]}


def _ranges(tier, role) -> dict:
    s = tier.get("stats") or {}
    if any(k in s for k in ROLES) or "all" in s:
        return s.get(role) or s.get("all") or {}
    return s


def _as_of(spec, target_start) -> int | None:
    v = spec.get("as_of", "auto")
    if v in (None, "", "none"):
        return None
    if v == "auto":
        return target_start
    if isinstance(v, (int, float)):
        return int(v)
    return calendar.timegm(time.strptime(str(v)[:10], "%Y-%m-%d")) + 86399


class Generator:
    def __init__(self, spec: dict):
        self.spec = spec
        self.mst = _Master()
        self.env, start = target_course(spec.get("target") or {})
        self.as_of = _as_of(spec, start)
        self.corpus = Corpus(self.mst, self.env, spec.get("references", "auto"))
        self.rng = random.Random(f"{spec.get('name')}:{spec.get('seed', 1)}")
        self.used = collections.Counter()
        self.stats = collections.Counter()

    def card_ok(self, c) -> bool:
        if c not in self.mst.card:
            return False
        if self.as_of is None:
            return True
        rel = self.mst.card_rel.get(c)
        return (rel is not None and rel <= self.as_of) or self._seen("c", c)

    def sup_ok(self, s) -> bool:
        if s not in self.mst.support:
            return False
        return self.as_of is None or self.mst.sup_rel[s] <= self.as_of or self._seen("s", s)

    def _seen(self, kind, i) -> bool:
        when = self.corpus.seen.get((kind, int(i)))
        return when is not None and when <= self.as_of

    def skill_ok(self, sid) -> bool:
        if sid not in self.mst.skill:
            return False
        if self.as_of is None:
            return True
        if self._seen("k", sid):
            return True
        evo = self.mst.evo_date.get(sid)
        if evo is not None:
            return evo <= self.as_of
        if 900000 <= sid < 1000000:
            return any(self.card_ok(c) for c, u in self.mst.unique.items() if u == sid - 800000)
        srcs = self.mst.src_by_group.get(self.mst.group(sid))
        if not srcs:
            return True
        return any(self.sup_ok(i) if k == "s" else self.card_ok(i) for k, i in srcs)

    def _template(self, p) -> tuple[str, dict]:
        refs = self.corpus.refs
        snap = self.rng.choices(list(refs), weights=list(refs.values()))[0]
        rows = self.corpus.snaps[snap]["teams"]
        widen = 1.0
        while True:
            cands = [t for t in rows if abs(t["_percentile"] - p) <= widen]
            if cands or widen > 100:
                break
            widen *= 2
        least = min(self.used[(snap, t["id"])] for t in cands)
        t = self.rng.choice([t for t in cands if self.used[(snap, t["id"])] == least])
        self.used[(snap, t["id"])] += 1
        return snap, t

    def _deck(self, deck) -> list[dict]:
        mst, out, charas = self.mst, [], set()
        keep = [d for d in deck if self.sup_ok(int(d["id"]))]
        for d in keep:
            charas.add(mst.support[int(d["id"])]["chara_id"])
        for d in deck:
            sid = int(d["id"])
            if self.sup_ok(sid) and d in keep:
                out.append(dict(d))
                continue
            row = mst.support.get(sid) or {}
            kind = (row.get("support_card_type"), row.get("command_id"))
            have = {int(x["id"]) for x in out} | {int(x["id"]) for x in keep}
            cands = [c for c, r in mst.support.items()
                     if (r["support_card_type"], r["command_id"]) == kind and self.sup_ok(c)
                     and c not in have and r["chara_id"] not in charas]
            if not cands:
                continue
            ssr = [c for c in cands if mst.support[c]["rarity"] == 3] or cands
            w = [self.corpus.deck_use.get(c, 0) + 0.05 for c in ssr]
            c = self.rng.choices(ssr, weights=w)[0]
            charas.add(mst.support[c]["chara_id"])
            out.append(dict(d, id=c))
            self.stats["deck_swaps"] += 1
        for i, d in enumerate(out):
            d["position"] = i + 1
        return out

    def _parents(self, parents) -> list[dict]:
        mst, out = self.mst, []
        for p in parents:
            c = int(p.get("cardId") or 0)
            p = dict(p, factors=[dict(f) for f in p.get("factors") or []])
            if not self.card_ok(c):
                cands = [x for x in mst.card if self.card_ok(x)]
                if not cands:
                    continue
                n = self.rng.choices(cands, weights=[self.corpus.parent_use.get(x, 0) + 0.2 for x in cands])[0]
                for f in p["factors"]:
                    if mst.factor_type.get(f["id"]) == 3:
                        f["id"] = n * 100 + int(f["id"]) % 100
                p["cardId"] = n
                self.stats["parent_swaps"] += 1
            p["factors"] = [f for f in p["factors"] if f["id"] in mst.factor_type
                            and all(self.skill_ok(s) for s in mst.factor_skill.get(f["id"], ()))]
            out.append(p)
        return out

    def _sources(self, card, deck, parents) -> dict:
        mst = self.mst
        g = mst.group
        return {"deck": {g(s) for d in deck for s in mst.hints.get(int(d["id"]), ())},
                "awaken": {g(s) for s in mst.awaken.get(card, ())},
                "parents": {g(s) for p in parents for f in p["factors"] for s in mst.factor_skill.get(f["id"], ())},
                "parent_skills": {s for p in parents for f in p["factors"] for s in mst.factor_skill.get(f["id"], ())},
                "deck_skills": {s for d in deck for s in mst.hints.get(int(d["id"]), ())},
                "awaken_skills": set(mst.awaken.get(card, ()))}

    def _src_weight(self, sid, src) -> float:
        if not self.skill_ok(sid):
            return 0.0
        g = self.mst.group(sid)
        for k in ("deck", "parents", "awaken"):
            if g in src[k]:
                return SOURCE_WEIGHT[k]
        return SOURCE_WEIGHT["general"] if not self.mst.src_by_group.get(g) else SOURCE_WEIGHT["other"]

    def _explained(self, sid, src) -> bool:
        g = self.mst.group(sid)
        return g in src["deck"] or g in src["parents"] or g in src["awaken"]

    def _candidates(self, style, role, src, taken, have, ok) -> list[tuple[int, float]]:
        mst = self.mst
        use = self.corpus.skill_use[(style, role)]
        pool = set(use) | src["deck_skills"] | src["awaken_skills"] | src["parent_skills"]
        out = []
        for s in pool:
            if s in have or s not in mst.skill or 100000 <= s < 200000 or mst.group(s) in taken:
                continue
            if 900000 <= s < 1000000 and s not in src["parent_skills"]:
                continue
            if not ok(s) or not compatible(mst, s, style, self.env):
                continue
            w = (use.get(s, 0) + 0.5) * self._src_weight(s, src)
            if w > 0:
                out.append((s, w))
        return out

    def _choose(self, cands):
        return self.rng.choices([s for s, _ in cands], weights=[w for _, w in cands])[0] if cands else None

    def _swap(self, sid, style, role, src, taken, have):
        want = self.mst.func(sid)
        c = self._candidates(style, role, src, taken, have, lambda s: self.mst.func(s) == want) or \
            self._candidates(style, role, src, taken, have, lambda s: self.mst.func(s)[0] == want[0])
        return self._choose(c)

    def _member(self, snap, m, tier, team_charas) -> dict:
        mst, env, rng = self.mst, self.env, self.rng
        style = int(m.get("racingStyle") or 2)
        role = role_of(mst, m)
        band = tier.get("templates", tier.get("pct", [0, 100]))
        card = int(m["card"])
        fits = self.corpus.same_course(snap) or _fits(mst, card, env, self.corpus)
        if not (self.card_ok(card) and fits) or card // 100 in team_charas:
            use = self.corpus.card_use[(style, role)]
            cands = [(c, w) for c, w in use.items() if c // 100 not in team_charas and self.card_ok(c)
                     and _fits(mst, c, env, self.corpus) and mst.card[c][_STYLE_APT[1 if style == 5 else style]] >= 6]
            if cands:
                card = self._choose(cands)
                self.stats["card_swaps"] += card != int(m["card"])
        swapped_card = card != int(m["card"])
        old_unique, new_unique = mst.unique.get(int(m["card"]), 0), mst.unique.get(card, 0)

        deck = self._deck(m.get("deck") or [])
        parents = self._parents(m.get("parents") or [])
        src = self._sources(card, deck, parents)
        tsrc = self._sources(int(m["card"]), m.get("deck") or [], m.get("parents") or [])

        same = self.corpus.same_course(snap)
        out, taken = [], set()
        variety = float(self.spec.get("variety", 0.2))
        for sid, lv in m.get("skills") or []:
            sid = int(sid)
            if sid == old_unique or 100000 <= sid < 200000:
                sid = new_unique or sid
            else:
                lost = self._explained(sid, tsrc) and not self._explained(sid, src)
                dead = not same and not compatible(mst, sid, style, env)
                bad = sid not in mst.skill or not self.skill_ok(sid) or dead \
                    or (900000 <= sid < 1000000 and sid not in src["parent_skills"])
                roll = mst.func(sid)[0] == "white" and rng.random() < variety * 0.3
                if bad or lost or roll:
                    have = {s for s, _ in out} | {sid}
                    new = self._swap(sid, style, role, src, taken, have)
                    self.stats["skill_swaps" if (bad or lost) else "variety_swaps"] += 1
                    if new is None:
                        if bad or lost:
                            continue
                    else:
                        sid = new
            g = mst.group(sid)
            if g in taken:
                continue
            taken.add(g)
            out.append([sid, int(lv)])

        def fill(kind, want):
            while sum(mst.func(s)[0] == kind for s, _ in out) < want:
                s = self._choose(self._candidates(style, role, src, taken, {x for x, _ in out},
                                                  lambda x: mst.func(x)[0] == kind))
                if s is None:
                    return
                taken.add(mst.group(s))
                out.append([s, 1])

        def trim(kinds, want_total_of_kind, kind_count):
            use = self.corpus.skill_use[(style, role)]
            while kind_count() > want_total_of_kind:
                cands = [i for i, (s, _) in enumerate(out) if mst.func(s)[0] in kinds and not 100000 <= s < 200000]
                if not cands:
                    return
                i = min(cands, key=lambda i: (use.get(out[i][0], 0) + 0.5) * self._src_weight(out[i][0], src))
                taken.discard(mst.group(out[i][0]))
                out.pop(i)

        if "golds" in tier:
            lo, hi = tier["golds"]
            q = self.corpus.quantile(snap, band, style, role, "golds", self.corpus.measure(m, "golds"))
            want = round(lo + q * (hi - lo))
            trim(("gold",), want, lambda: sum(mst.func(s)[0] == "gold" for s, _ in out))
            fill("gold", want)
        if "skills" in tier:
            lo, hi = tier["skills"]
            q = self.corpus.quantile(snap, band, style, role, "skills", len(m.get("skills") or []))
            want = max(1, round(lo + q * (hi - lo)))
            trim(("white",), want, lambda: len(out))
            trim(("inherit",), want, lambda: len(out))
            while len(out) < want:
                n = len(out)
                fill("white", sum(mst.func(s)[0] == "white" for s, _ in out) + 1)
                if len(out) == n:
                    break
        if "unique_level" in tier:
            lo, hi = tier["unique_level"]
            out = [[s, rng.randint(int(lo), int(hi))] if 100000 <= s < 200000 else [s, lv] for s, lv in out]

        stats = [int(x) for x in m["stats"]]
        ranges = _ranges(tier, role)
        for i, name in enumerate(STATS):
            r = ranges.get(name)
            if r:
                q = self.corpus.quantile(snap, band, style, role, i, m["stats"][i])
                q = min(1.0, max(0.0, q + rng.uniform(-0.03, 0.03)))
                stats[i] = round(r[0] + q * (r[1] - r[0]))
        cap = self.spec.get("stat_cap")
        if cap:
            stats = [min(int(cap), v) for v in stats]

        tmpl_apt = list(m.get("aptitudes") or [])
        if not swapped_card and self.corpus.same_course(snap) and len(tmpl_apt) == 3:
            apt = tmpl_apt
        else:
            row = mst.card.get(card) or {}
            keys = ("proper_ground_dirt" if env["ground_type"] == 2 else "proper_ground_turf",
                    _DIST_APT[env["distance_type"] - 1], _STYLE_APT[1 if style == 5 else style])
            apt = []
            for j, k in enumerate(keys):
                v = int(row.get(k) or 5)
                if v < 7:
                    v = 7 if rng.random() < 0.9 else v
                if v == 7 and len(tmpl_apt) == 3 and tmpl_apt[j] == "S":
                    v = 8
                apt.append(_LETTER[max(1, min(8, v)) - 1])

        old_stats = [m["stats"][0], m["stats"][1], m["stats"][2], m["stats"][4], m["stats"][3]]
        new_stats = [stats[0], stats[1], stats[2], stats[4], stats[3]]
        score = int(m.get("score") or 0) + R.get_rating(new_stats) - R.get_rating(old_stats) + round(
            1.128 * (sum(mst.gv(s) for s, _ in out) - sum(mst.gv(int(s)) for s, _ in m.get("skills") or [])))
        mid = hashlib.sha1(f"{self.spec['name']}:{m['id']}:{rng.random()}".encode()).hexdigest()
        return {"id": mid, "card": card, "chara": card // 100, "deck": deck, "score": max(score, 0),
                "stats": stats, "style": m.get("style"), "racingStyle": style, "aptitudes": apt,
                "skills": out, "parents": parents, "_role": role,
                "_template": {"snapshot": snap, "member": m["id"], "card": int(m["card"])}}

    def team(self, i, n) -> dict:
        p = 100.0 * (i + 0.5) / n
        tier = _tier(self.spec, p)
        lo, hi = tier.get("pct", [0, 100])
        tlo, thi = tier.get("templates", [lo, hi])
        f = (p - lo) / (hi - lo) if hi > lo else 0.5
        snap, tmpl = self._template(tlo + f * (thi - tlo))
        charas, members = set(), []
        for m in tmpl["members"]:
            g = self._member(snap, m, tier, charas)
            charas.add(g["card"] // 100)
            members.append(g)
        tid = hashlib.sha1(f"{self.spec['name']}:{self.spec.get('seed', 1)}:{i}".encode()).hexdigest()
        ground = "proper_ground_dirt" if self.env["ground_type"] == 2 else "proper_ground_turf"
        return {"snapshot": f"gen:{self.spec['name']}", "id": tid,
                "owner": {"id": f"{self.spec['name']}-{i}", "names": [f"Trainer {tid[:4].upper()}"]},
                "members": members, "_percentile": round(p, 2),
                "course_keys": [ground, _DIST_APT[self.env["distance_type"] - 1]],
                "_template": {"snapshot": snap, "team": tmpl["id"], "percentile": tmpl["_percentile"]}}


def _fits(mst, card, env, corpus=None) -> bool:
    row = mst.card.get(card)
    if not row:
        return False
    if corpus and (card, env["distance_type"], env["ground_type"]) in corpus.fielded:
        return True
    return row[_DIST_APT[env["distance_type"] - 1]] >= 6 and \
        row["proper_ground_dirt" if env["ground_type"] == 2 else "proper_ground_turf"] >= 6


def generate(spec: dict, write: bool = True) -> dict:
    name = str(spec.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("spec.name: 1-64 of A-Z a-z 0-9 _ . -")
    for t in spec.get("tiers") or []:
        lo, hi = t["pct"]
        if not 0 <= lo <= hi <= 100:
            raise ValueError(f"bad tier pct {t['pct']}")
    n = int(spec.get("teams") or 3000)
    g = Generator(spec)
    teams = [g.team(i, n) for i in range(n)]
    roles = collections.Counter(m["_role"] for t in teams for m in t["members"])
    meta = {"name": name, "spec": spec, "teams": n, "built": int(time.time()),
            "as_of": time.strftime("%Y-%m-%d", time.gmtime(g.as_of)) if g.as_of else None,
            "course": {"label": g.env["_label"], "race_instance_id": g.env["_ri"],
                       "distance": g.env["course_distance"], "ground": g.env["ground_type"]},
            "references": {k: round(v, 3) for k, v in g.corpus.refs.items()},
            "edits": dict(g.stats), "roles": dict(roles), "distinct_templates": len(g.used)}
    if write:
        from .opponent_sources import gen_dir
        OUT = gen_dir()
        OUT.mkdir(parents=True, exist_ok=True)
        tmp = OUT / f"{name}.jsonl.gz.tmp"
        with gzip.open(tmp, "wt") as fh:
            for t in teams:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        tmp.replace(OUT / f"{name}.jsonl.gz")
        (OUT / f"{name}.json").write_text(json.dumps(meta, indent=1))
    meta["_teams"] = teams
    return meta
