import json, glob, os
from multiprocessing import Pool
LOGS = r"C:\Users\Systemless\Documents\UmaPS\captures\bot_logs"
def one(p):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    idn = d.get("identity") or {}
    return os.path.basename(p), {"sc": d.get("scenario_id"), "deck": idn.get("support_deck"),
                                 "identity_keys": list(idn.keys())}
if __name__ == "__main__":
    out = {}
    with Pool(max(2, os.cpu_count() - 2)) as pool:
        for r in pool.imap_unordered(one, sorted(glob.glob(os.path.join(LOGS, "career_log_*.json"))), chunksize=4):
            if r: out[r[0]] = r[1]
    json.dump(out, open("decks.json", "w"))
    have = [v for v in out.values() if v["deck"]]
    print("careers", len(out), "with deck", len(have))
    from collections import Counter
    print("identity keys", Counter(k for v in out.values() for k in v["identity_keys"]).most_common())
    print("distinct cards", len({c["support_card_id"] for v in have for c in v["deck"]}))
