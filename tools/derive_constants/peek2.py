import json, glob, sys

def find_scenario_file(sid, n=1):
    files = sorted(glob.glob('captures/bot_logs/career_log_*.json'))
    out = []
    for fp in files:
        try:
            with open(fp, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if data.get('scenario_id') == sid:
            out.append((fp, data))
            if len(out) >= n:
                break
    return out

for sid in (2, 3):
    res = find_scenario_file(sid, 1)
    if not res:
        continue
    fp, data = res[0]
    print("====", sid, fp)
    turns = data['turns']
    for t in turns:
        if t.get('grand_live') is not None:
            print("grand_live sample:", json.dumps(t.get('grand_live'))[:800])
            break
    for t in turns:
        if t.get('bond') is not None:
            print("bond sample:", json.dumps(t.get('bond'))[:400])
            print("stat_caps sample:", json.dumps(t.get('stat_caps'))[:400])
            break
    # look for team/roster/soul related keys anywhere
    for t in turns[:3]:
        print("turn keys:", list(t.keys()))
