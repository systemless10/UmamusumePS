"""Independent re-check of U-001 (Rest outcomes) and U-020 (crane on recreation).

For every exec_command REQ with command_type 7 (rest) or 3 (outing), record:
scenario, turn, command_id, every check_event event_id served until the next
exec_command/race, and the vital delta (REQ current_vital -> exec RES chara.vital).
Aggregates are keyed so camp/scenario splits can be done afterwards.
"""
import json, glob, os, sys
from collections import Counter, defaultdict
from multiprocessing import Pool

LOGS = r"C:\Users\Systemless\Documents\UmaPS\captures\bot_logs"


def one(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return ("ERR", path, str(e))
    sc = d.get("scenario_id")
    for t in d.get("turns") or []:
        calls = t.get("api_calls") or []
        maxhp = (t.get("stats") or {}).get("max_hp")
        i = 0
        while i < len(calls):
            c = calls[i]
            ep = c.get("endpoint") or ""
            if c.get("direction") == "REQ" and ep.endswith("/exec_command"):
                data = c.get("data") or {}
                ct = data.get("command_type")
                if ct in (3, 7, 8):
                    rid = c.get("req_id")
                    before = data.get("current_vital")
                    after = None
                    evs = []
                    j = i + 1
                    while j < len(calls):
                        cj = calls[j]
                        epj = cj.get("endpoint") or ""
                        if cj.get("direction") == "RES" and cj.get("req_id") == rid:
                            after = ((cj.get("data") or {}).get("chara") or {}).get("vital")
                        elif cj.get("direction") == "REQ":
                            if epj.endswith("/exec_command") or "race" in epj:
                                break
                            if epj.endswith("/check_event"):
                                evs.append((cj.get("data") or {}).get("event_id"))
                        j += 1
                    out.append((sc, data.get("current_turn"), ct, data.get("command_id"),
                                tuple(evs), before, after, maxhp))
            i += 1
    return ("OK", path, out)


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(LOGS, "career_log_*.json")))
    rows, errs = [], 0
    with Pool(max(2, os.cpu_count() - 2)) as p:
        for status, path, payload in p.imap_unordered(one, files, chunksize=4):
            if status == "ERR":
                errs += 1
            else:
                rows.extend(payload)
    print("files", len(files), "errors", errs, "rows", len(rows))
    json.dump(rows, open(sys.argv[1], "w"))
