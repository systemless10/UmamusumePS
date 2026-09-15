"""Reduce every career log to what the derivations need; one .pkl.gz per career.

career = {file, sc, final_turn, outcome, turns: [turn]}
turn   = {t, stats, bond, caps, board, tips, cond, calls}
calls  = [(dir, ep_tail, payload)]   REQ payload = request data dict (small)
                                     RES payload = (chara digest, playing_state)
"""
import json, glob, os, gzip, pickle, sys
from multiprocessing import Pool

LOGS = r"C:\Users\Systemless\Documents\UmaPS\captures\bot_logs"
OUT = sys.argv[1]


def board(b):
    if not isinstance(b, list):
        return None
    return [(x.get("command_type"), x.get("command_id"), x.get("is_enable"),
             x.get("failure_rate"), x.get("level"), tuple(x.get("partners") or ()),
             tuple(x.get("hints") or ()), x.get("gains")) for x in b if isinstance(x, dict)]


def one(path):
    name = os.path.basename(path)
    dst = os.path.join(OUT, name + ".pkl.gz")
    if os.path.exists(dst):
        return name, "skip"
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return name, f"ERR {e}"
    turns = []
    for t in d.get("turns") or []:
        cc = t.get("current_command") or {}
        ci = cc.get("chara_info") if isinstance(cc, dict) else None
        cond = ci.get("chara_effect_id_array") if isinstance(ci, dict) else None
        calls = []
        for c in t.get("api_calls") or []:
            ep = (c.get("endpoint") or "").split("/")[-1]
            data = c.get("data") or {}
            if c.get("direction") == "REQ":
                calls.append(("REQ", ep, {k: v for k, v in data.items()
                                          if not isinstance(v, (list, dict))}))
            else:
                calls.append(("RES", ep, (data.get("chara"), data.get("playing_state"))))
        tips = t.get("server_skill_tips_raw")
        turns.append({
            "t": t.get("turn"), "stats": t.get("stats"), "bond": t.get("bond"),
            "caps": t.get("stat_caps"), "board": board(t.get("server_command_board")),
            "tips": [(x.get("group_id"), x.get("rarity"), x.get("level")) for x in tips]
                    if isinstance(tips, list) else None,
            "cond": cond, "calls": calls,
            "skill_point": t.get("skill_point"), "gl": t.get("grand_live"),
        })
    car = {"file": name, "sc": d.get("scenario_id"), "final_turn": d.get("final_turn"),
           "outcome": d.get("outcome"), "turns": turns}
    with gzip.open(dst, "wb", compresslevel=3) as f:
        pickle.dump(car, f, protocol=pickle.HIGHEST_PROTOCOL)
    return name, "ok"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    files = sorted(glob.glob(os.path.join(LOGS, "career_log_*.json")))
    bad = 0
    with Pool(max(2, os.cpu_count() - 2)) as p:
        for name, st in p.imap_unordered(one, files, chunksize=4):
            if st.startswith("ERR"):
                bad += 1
    print("files", len(files), "errors", bad)
