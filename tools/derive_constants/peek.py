import json, glob, sys

fp = sys.argv[1] if len(sys.argv) > 1 else None
if not fp:
    files = sorted(glob.glob('captures/bot_logs/career_log_*.json'))
    fp = files[0]

with open(fp, encoding='utf-8') as f:
    data = json.load(f)

print("FILE", fp)
print("scenario_id", data.get('scenario_id'), "final_turn", data.get('final_turn'), "outcome", data.get('outcome'))
turns = data['turns']
print("num turns", len(turns))
# find a training turn with exec_command REQ command_type 1
for t in turns[:6]:
    print("---TURN", t.get('turn'), "cmd_type", (t.get('current_command') or {}).get('command_type') if isinstance(t.get('current_command'),dict) else None)
    print("stats", t.get('stats'))
    print("event", json.dumps(t.get('event'))[:300])
    for ac in t.get('api_calls', [])[:8]:
        print(" ", ac.get('direction'), ac.get('endpoint'), json.dumps(ac.get('data'))[:400])
