#!/usr/bin/env python3
import argparse
import csv
import gzip
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHEET = "18vWIpjfQxN9Mw0DXyHPNuY4RJc2wGi9RkyiuE9SqoYo"
GIDS = {"1": 995692999, "2": 251168694}
RAW = ROOT / "captures" / "loh_sheet"
OUT = ROOT / "server" / "data" / "heroes_opponents"

FRONT, PACE, LATE, END = 1, 2, 3, 4
STYLE_NAMES = {"front": FRONT, "pace": PACE, "late": LATE, "end": END, "runaway": FRONT}
RUNAWAY_SKILL = 202051
DIST_TYPE = {"1": 3, "2": 1}

ALIASES = {
    "ksb white": 106802, "vbourb": 102602, "v.ines": 103102, "neo uni": 110501, "festive tama": 102102,
    "tokai teio": 100301, "xmas vodka": 100802, "xoguri": 100602, "ines fujin": 103101, "mr.cb": 105701,
    "camp dober": 105902, "h.doto": 105802, "red maru": 100401, "ny tm opera o": 101502, "ny dia": 106702,
    "steampunk tickezo": 103502, "h.rice": 103002, "blue tannhauser": 106202, "silence suzuka": 100201,
    "mejiro ardan": 107101, "seiun sky": 102001, "eishin flash": 103701, "daisca": 100901,
    "eishin flash (valentines)": 103702, "tamamo cross": 102101, "sakura laurel": 107601,
    "narita brian": 101601, "symboli rudolf": 101701, "bird mcqueen": 101302, "fine motion": 102201,
    "wedding maya": 102402, "agnes tachyon": 103201, "mihono bourbon": 102601, "daitaku helios": 106501,
    "nice nature (cheerleader)": 106002, "ball sei": 102002, "air shakur": 103601, "copano rickey": 109801,
    "biwa hayahide (christmas)": 102302,
    "oguri cap": 100601, "aston machan": 108701, "seeking the pearl": 104201, "taki shuttle": 101001,
    "taiki shuttle": 101001, "nishino flower": 105101, "wedding curren": 103802, "camp taiki": 101002,
    "yamanin zephyr": 107801, "air groove (wedding)": 101802, "red bakushin": 104102,
    "summer bamboo memory": 105302, "hishi akebono": 102801, "wedding nishino flower": 105102,
    "rice shower": 103001, "mejiro ardan (ballroom)": 107102,
}
SUPPORT_CMD = {"speed": 101, "stamina": 105, "power": 102, "guts": 103, "wit": 106}
CHARA_ALIASES = {"matikanefukukitaru": "Matikanefukukitaru", "k.s.miracle": "K.S.Miracle", "mr.c.b": "Mr. C.B."}


def fetch():
    RAW.mkdir(parents=True, exist_ok=True)
    for n, gid in GIDS.items():
        url = f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv&gid={gid}"
        (RAW / f"loh_{n}.csv").write_bytes(urllib.request.urlopen(url, timeout=60).read())


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def pct(cell):
    m = re.match(r"(.*?)\s*\(([\d.]+)%\)\s*$", norm(cell))
    return (norm(m.group(1)), float(m.group(2))) if m else (None, 0.0)


def parse(n):
    rows = list(csv.reader(open(RAW / f"loh_{n}.csv", encoding="utf-8")))
    grid = lambda r, c: norm(rows[r][c]) if r < len(rows) and c < len(rows[r]) else ""
    out = {"usage": {}, "comps": {}, "cards": {}, "uniques": {}, "builds": [], "final_scores": []}
    for r in range(2, len(rows)):
        for c in range(0, 12):
            m = re.match(r"^(\d+)/(\d+)$", grid(r, c))
            name = grid(r, c + 1).lower()
            if m and name and not re.match(r"^\d", name):
                out["usage"][name] = out["usage"].get(name, 0) + int(m.group(1)) / int(m.group(2))
    for r in range(len(rows)):
        for c in range(0, 12):
            lab = grid(r, c).lower()
            if re.match(r"^(\d (runaway|front|pace|late|end) ?)+$", lab):
                cnt = next((grid(r, k) for k in range(c + 1, c + 4) if grid(r, k).isdigit()), "")
                if cnt:
                    out["comps"][lab] = out["comps"].get(lab, 0) + int(cnt)
    for r0 in range(len(rows)):
        for c in range(len(rows[r0])):
            h = grid(r0, c)
            m = re.match(r"^(?:(Front|Pace|Late|End|Runaway) )?Card Usage Rate", h)
            u = re.match(r"^Inherited uniques(?: \((Front|Pace|Late|End|Runaway)\))?$", h)
            if not (m or u):
                continue
            style = ((m or u).group(1) or "all").lower()
            key = "cards" if m else "uniques"
            if "T300" in h and style in out[key]:
                continue
            lst = out[key].setdefault(style, {})
            for r in range(r0 + 2, min(r0 + 16, len(rows))):
                for cc in (c, c + 1):
                    name, p = pct(grid(r, cc))
                    if name:
                        lst[name] = max(lst.get(name, 0), p)
                        break
    for r in range(len(rows)):
        for c in range(len(rows[r])):
            if re.match(r"^\d{3,4}(/\d{3,4}){4}$", grid(r, c)):
                spark = " ".join(grid(r, k) for k in range(c + 1, c + 3))
                skills = next((grid(r, k) for k in range(c + 2, c + 5) if "," in grid(r, k)), "")
                st = re.search(r"\d+\* ?(Front|Pace|Late|End|Runaway)", spark)
                out["builds"].append({"stats": [int(x) for x in grid(r, c).split("/")],
                                      "style": st.group(1).lower() if st else None, "spark": spark,
                                      "skills": [norm(s) for s in skills.split(",") if norm(s)]})
    for r in range(len(rows)):
        for c in range(len(rows[r]) - 1):
            if grid(r, c) == "Rank" and grid(r, c + 1) == "Points":
                k = r + 1
                while grid(k, c).isdigit() and grid(k, c + 1).isdigit():
                    out["final_scores"].append(int(grid(k, c + 1)))
                    k += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="re-download the sheet CSVs")
    ap.add_argument("--teams", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=20261017)
    ap.add_argument("--jp", default=str(Path.home() / ".local/share/Steam/steamapps/common/UmamusumePrettyDerby_Jpn/"
                                        "UmamusumePrettyDerby_Jpn_Data/Persistent/master/master.mdb"),
                    help="JP master.mdb (which card evolves which skill)")
    ap.add_argument("--pack", default="", help="pack digest to read (default: the active one); must hold skills_evolved")
    a = ap.parse_args()
    if a.fetch or not (RAW / "loh_1.csv").exists():
        fetch()
    os.environ.setdefault("UMA_REGION", "global")
    sys.path.insert(0, str(ROOT / "server"))
    from app import custom_support as C, master_data as M, rating_formula as R

    digest = a.pack or (C.manifest() or {}).get("digest", "")
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"_source": f"https://docs.google.com/spreadsheets/d/{SHEET} (Melon_egg LoH archive; JP data via "
                       "shiny_broom942 / bilibili wiki)", "heroes": {}}
    with C.use_pack(digest):
        names = {}
        for r in M.query("SELECT [index] i, text FROM text_data WHERE category=47"):
            names.setdefault(norm(r["text"]).lower(), []).append(r["i"])
        chara_names = {norm(r["text"]).lower(): r["i"] for r in M.query(
            "SELECT [index] i, text FROM text_data WHERE category=6")}
        cards = {r["card_id"]: dict(r) for r in M.query(
            "SELECT c.* FROM card_rarity_data c WHERE rarity=(SELECT MAX(rarity) FROM card_rarity_data WHERE card_id=c.card_id)")}
        skill_rows = {r["id"]: dict(r) for r in M.query(
            "SELECT id, rarity, group_id, grade_value, ability_type_1_1, target_type_1_1, float_ability_value_1_1, "
            "precondition_1, condition_1, condition_2 "
            "FROM skill_data")}

        def unique_of(card):
            row = cards.get(card)
            s = M.query_one("SELECT skill_id1 FROM skill_set WHERE id=?", (row["skill_set"],)) if row else None
            return s["skill_id1"] if s else 0

        def skill_id(name, inherited=False):
            ids = names.get(norm(name).lower(), [])
            if inherited:
                ids = [i for i in ids if 900000 <= i < 1000000] or [i + 800000 for i in ids if 100000 <= i < 200000]
            else:
                ids = [i for i in ids if 200000 <= i < 900000] or [i for i in ids if i >= 100000000 and i in skill_rows]
            return min(ids) if ids else 0

        def support_id(name):
            m = re.match(r"^(.*?) (Speed|Stamina|Power|Guts|Wit)$", name)
            if name.lower().startswith("ancestors and guides"):
                r = M.query_one("SELECT [index] i FROM text_data WHERE category=75 AND text LIKE '%Ancestors%' ORDER BY i")
                return r["i"] if r else 0
            if not m:
                return 0
            cn = CHARA_ALIASES.get(m.group(1).lower(), m.group(1)).lower()
            cid = chara_names.get(cn) or next((v for k, v in chara_names.items() if k.replace(" ", "") == cn.replace(" ", "")), 0)
            r = M.query_one("SELECT id FROM support_card_data WHERE chara_id=? AND rarity=3 AND command_id=? ORDER BY id",
                            (cid, SUPPORT_CMD[m.group(2).lower()]))
            return r["id"] if r else 0

        def course_of(hid):
            row = M.query_one(
                "SELECT rcs.distance, rcs.ground, rcs.turn, rcs.race_track_id, hrc.season FROM heroes_race_condition hrc "
                "JOIN race_instance ri ON ri.id=hrc.race_instance_id JOIN race r ON r.id=ri.race_id "
                "JOIN race_course_set rcs ON rcs.id=r.course_set WHERE hrc.heroes_id=? AND hrc.stage=1", (hid,))
            d = row["distance"]
            return {"distance_type": 1 if d <= 1400 else 2 if d <= 1800 else 3 if d <= 2400 else 4,
                    "course_distance": d, "is_basis_distance": int(d % 400 == 0), "ground_type": row["ground"],
                    "rotation": row["turn"], "track_id": row["race_track_id"], "season": row["season"],
                    "weather": {1, 2}, "ground_condition": {1}}

        term_re = re.compile(r"^([a-z_]+)(==|!=|>=|<=|>|<)(-?\d+)$")
        ops = {"==": lambda x, y: x == y, "!=": lambda x, y: x != y, ">=": lambda x, y: x >= y,
               "<=": lambda x, y: x <= y, ">": lambda x, y: x > y, "<": lambda x, y: x < y}

        def cond_ok(cond, env):
            if not cond:
                return True
            for alt in cond.split("@"):
                good = True
                for term in alt.split("&"):
                    m = term_re.match(term.strip())
                    if not m or m.group(1) not in env:
                        continue
                    have, want = env[m.group(1)], int(m.group(3))
                    have = have if isinstance(have, set) else {have}
                    if not any(ops[m.group(2)](h, want) for h in have):
                        good = False
                        break
                if good:
                    return True
            return False

        def compatible(sid, style, course):
            row = skill_rows.get(sid)
            if not row or (row["grade_value"] or 0) < 0:
                return False
            env = dict(course, running_style=style)
            return cond_ok(row["precondition_1"], env) and cond_ok(row["condition_1"], env)

        jp = sqlite3.connect(Path(a.jp).as_uri() + "?mode=ro", uri=True)
        evos = {}
        for card, rank, sid in jp.execute("SELECT card_id, rank, skill_id FROM skill_upgrade_description WHERE rank IN (3, 5)"):
            if sid in skill_rows:
                evos.setdefault(card, {}).setdefault(rank, []).append(sid)
        awaken = {}
        for r in M.query("SELECT a.available_skill_set_id c, a.need_rank n, a.skill_id s FROM available_skill_set a"):
            awaken.setdefault(r["c"], []).append((r["n"], r["s"]))
        hints = {}
        for r in M.query("SELECT support_card_id c, hint_value_1 s FROM single_mode_hint_gain WHERE hint_gain_type=0"):
            hints.setdefault(r["c"], set()).add(r["s"])
        for r in M.query("SELECT d.id c, s.* FROM support_card_data d JOIN skill_set s ON s.id=d.skill_set_id"):
            hints.setdefault(r["c"], set()).update(r[f"skill_id{i}"] for i in range(1, 11)
                                                   if f"skill_id{i}" in r.keys() and r[f"skill_id{i}"])
        general = [i for i, s in skill_rows.items() if 200000 <= i < 300000 and s["rarity"] in (1, 2)
                   and (s["grade_value"] or 0) > 0]

        for n in GIDS:
            data = parse(n)
            hid = int(n)
            course = course_of(hid)
            dist = course["distance_type"]
            rng = random.Random(f"{a.seed}:{hid}")
            usage, missing = {}, []
            for nick, w in data["usage"].items():
                cid = ALIASES.get(nick)
                if cid and cid in cards:
                    usage[cid] = usage.get(cid, 0) + w
                else:
                    missing.append(nick)
            style_key = {FRONT: "proper_running_style_nige", PACE: "proper_running_style_senko",
                         LATE: "proper_running_style_sashi", END: "proper_running_style_oikomi"}
            by_style = {s: {c: w for c, w in usage.items() if cards[c][k] >= 7} for s, k in style_key.items()}
            comps = []
            for lab, cnt in data["comps"].items():
                slots = []
                for num, sty in re.findall(r"(\d) (runaway|front|pace|late|end)", lab):
                    slots += [sty] * int(num)
                if len(slots) == 3:
                    comps.append((slots, cnt))
            stat_base = {}
            for b in data["builds"]:
                stat_base.setdefault(b["style"] or "all", []).append(b["stats"])
            allb = [b["stats"] for b in data["builds"]]
            avg = lambda xs: [round(sum(v[i] for v in xs) / len(xs)) for i in range(5)]
            base_all = avg(allb)
            t1 = {skill_id(s) for b in data["builds"] for s in b["skills"]} - {0}
            deck_w = {sty: [(support_id(k), v) for k, v in lst.items()] for sty, lst in data["cards"].items()}
            uniq_w = {sty: [(skill_id(k, True), v) for k, v in lst.items()] for sty, lst in data["uniques"].items()}

            def value(sid, style, q):
                s = skill_rows[sid]
                v = float(s["grade_value"] or 0)
                kind = s["ability_type_1_1"]
                if s["target_type_1_1"] != 1 and (s["float_ability_value_1_1"] or 0) < 0:
                    v *= 0.35
                elif kind in (27, 31):
                    v *= 1.4
                elif kind == 9:
                    v *= 1.3 if dist >= 3 else 0.35
                elif kind in (1, 2, 3, 4, 5):
                    v *= 0.8
                if sid in t1:
                    v *= 1 + 2.5 * q
                return max(v, 1.0) ** (1 + 2.5 * q)

            def pick_by_value(pool, k, style, q, taken_groups):
                out = []
                pool = [s for s in pool if skill_rows[s]["group_id"] not in taken_groups]
                while pool and len(out) < k:
                    s = rng.choices(pool, weights=[value(x, style, q) for x in pool])[0]
                    out.append(s)
                    taken_groups.add(skill_rows[s]["group_id"])
                    pool = [x for x in pool if skill_rows[x]["group_id"] != skill_rows[s]["group_id"]]
                return out

            def build_member(card, sty, p, members_i, tseed):
                style = STYLE_NAMES[sty]
                q = p / 100
                ok = lambda sid: compatible(sid, style, course)
                base = avg(stat_base[sty]) if sty in stat_base else base_all
                floors = (0.80, 0.70 if dist >= 3 else 0.80, 0.66, 0.55, 0.64)
                curve = q ** 0.85
                stats = [max(100, min(2000, round(v * (fl + (1 - fl) * curve) * rng.uniform(0.96, 1.04))))
                         for v, fl in zip(base, floors)]
                deck = []
                dw = [(s, w) for s, w in (deck_w.get(sty) or deck_w.get("all") or []) if s]
                while dw and len(deck) < 6:
                    s = rng.choices([x for x, _ in dw], weights=[w for _, w in dw])[0]
                    lb = 4 if rng.random() < 0.35 + 0.65 * q else rng.choice((2, 3))
                    deck.append({"id": s, "lb": lb, "exp": 0, "position": len(deck) + 1})
                    dw = [x for x in dw if x[0] != s]
                own_pool = {s for _, s in awaken.get(card, ())}
                deck_pool = set().union(*(hints.get(d["id"], set()) for d in deck)) if deck else set()
                groups = set()
                out = []
                own = unique_of(card)
                if own:
                    out.append([own, 3 + round(3 * q * rng.uniform(0.8, 1.0))])
                if sty == "runaway":
                    out.append([RUNAWAY_SKILL, 1])
                    groups.add(skill_rows[RUNAWAY_SKILL]["group_id"])
                golds_pool = [s for s in (own_pool | deck_pool | set(general))
                              if s in skill_rows and skill_rows[s]["rarity"] == 2 and ok(s)]
                n_gold = max(0, min(len(golds_pool), round(1 + 5 * q + rng.uniform(-1, 1))))
                golds = pick_by_value(golds_pool, n_gold, style, q, groups)
                for rank in (3, 5):
                    base_gold = next((s for nr, s in awaken.get(card, ()) if nr == rank and skill_rows.get(s, {}).get("rarity") == 2), 0)
                    cands = [e for e in evos.get(card, {}).get(rank, []) if ok(e)]
                    if cands and rng.random() < 0.15 + 0.85 * q:
                        if base_gold in golds:
                            golds.remove(base_gold)
                        else:
                            plain = [g for g in golds if skill_rows[g]["rarity"] == 2]
                            if len(golds) >= n_gold and plain:
                                golds.remove(min(plain, key=lambda g: value(g, style, q)))
                        golds.append(max(cands, key=lambda e: value(e, style, q)) if q > 0.6 else rng.choice(cands))
                out += [[s, 1] for s in golds]
                inh = []
                for sid, rate in uniq_w.get(sty) or uniq_w.get("all") or []:
                    if sid and sid in skill_rows and sid != own + 800000 and ok(sid) \
                            and rng.random() < rate / 100 * (0.3 + 0.7 * q) and len(inh) < 4:
                        inh.append([sid, 1])
                out += inh
                n_white = round(9 + 7 * q + rng.uniform(-1.5, 1.5))
                white_pool = [s for s in (own_pool | deck_pool | set(general))
                              if s in skill_rows and skill_rows[s]["rarity"] == 1 and ok(s)]
                out += [[s, 1] for s in pick_by_value(white_pool, n_white, style, q, groups)]
                prop = cards[card]
                dist_key = ("proper_distance_short", "proper_distance_mile", "proper_distance_middle",
                            "proper_distance_long")[dist - 1]
                gk = "proper_ground_dirt" if course["ground_type"] == 2 else "proper_ground_turf"
                letter = lambda v: "GFEDCBAS"[max(1, min(8, v)) - 1]
                apt = []
                for key in (gk, dist_key, style_key[style]):
                    v = prop[key]
                    if key != style_key[style] and v < 7:
                        v = min(7, v + 2 + round(2 * q + rng.uniform(0, 1)))
                    if v == 7 and rng.random() < 0.55 * q:
                        v = 8
                    apt.append(letter(v))
                score = R.get_rating([stats[0], stats[1], stats[2], stats[4], stats[3]]) + \
                    round(1.128 * R.get_skill_score([s for s, _ in out]))
                mid = hashlib.sha1(f"loh{hid}:{tseed}:{members_i}".encode()).hexdigest()
                return {"id": mid, "card": card, "chara": card // 100, "deck": deck, "score": score,
                        "stats": stats, "style": style, "racingStyle": style, "aptitudes": apt,
                        "skills": out, "parents": []}

            teams = []
            for i in range(a.teams):
                p = 100.0 * (i + 0.5) / a.teams
                slots = rng.choices([c for c, _ in comps], weights=[w for _, w in comps])[0]
                used, members = set(), []
                for sty in slots:
                    style = STYLE_NAMES[sty]
                    pool = {c: w for c, w in (by_style.get(style) or usage).items() if c // 100 not in used} or \
                        {c: w for c, w in usage.items() if c // 100 not in used}
                    card = rng.choices(list(pool), weights=list(pool.values()))[0]
                    used.add(card // 100)
                    members.append(build_member(card, sty, p, len(members), i))
                tid = hashlib.sha1(f"loh{hid}:{a.seed}:{i}".encode()).hexdigest()
                teams.append({"snapshot": f"loh{hid}", "id": tid, "owner": {"id": f"loh{hid}-{i}",
                              "names": [f"Trainer {tid[:4].upper()}"]}, "members": members, "_percentile": round(p, 2)})
            with gzip.open(OUT / f"heroes_{hid}.jsonl.gz", "wt") as fh:
                for t in teams:
                    fh.write(json.dumps(t, ensure_ascii=False) + "\n")
            meta["heroes"][str(hid)] = {"usage": {str(k): round(v, 4) for k, v in usage.items()},
                                        "unmapped_umas": missing, "comps": data["comps"],
                                        "builds": data["builds"], "final_scores": data["final_scores"],
                                        "teams": len(teams)}
            print(f"LoH {hid}: {len(teams)} teams, {len(usage)} cards, comps {len(comps)}, builds "
                  f"{len(data['builds'])}, final scores {len(data['final_scores'])}, unmapped {missing}")
    (OUT / "heroes_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
