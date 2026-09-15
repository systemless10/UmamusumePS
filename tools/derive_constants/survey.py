import json, glob, random, sys, collections

random.seed(42)
files = sorted(glob.glob('captures/bot_logs/career_log_*.json'))
print(f"total files: {len(files)}", file=sys.stderr)

by_scenario = collections.defaultdict(list)

# first pass: peek scenario_id only (read whole file, they're not THAT big individually, but stream carefully)
sample = random.sample(files, min(220, len(files)))

scenario_counts = collections.Counter()
endpoint_counts = collections.Counter()
req_key_counts = collections.defaultdict(collections.Counter)
res_key_counts = collections.defaultdict(collections.Counter)
event_id_locations = collections.Counter()
turn_key_counts = collections.Counter()
top_key_counts = collections.Counter()
command_type_counts = collections.Counter()
sample_events = []
n_by_scenario = collections.Counter()
picked_per_scenario = collections.defaultdict(list)

for fp in sample:
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERR {fp}: {e}", file=sys.stderr)
        continue
    sid = data.get('scenario_id')
    n_by_scenario[sid] += 1
    if len(picked_per_scenario[sid]) < 15:
        picked_per_scenario[sid].append(fp)
    for k in data.keys():
        top_key_counts[k]+=1
    turns = data.get('turns', [])
    for t in turns:
        for k in t.keys():
            turn_key_counts[k]+=1
        cc = t.get('current_command')
        if isinstance(cc, dict):
            ct = cc.get('command_type')
            command_type_counts[ct]+=1
        ev = t.get('event')
        if ev:
            event_id_locations['turn.event present']+=1
        acs = t.get('api_calls', [])
        for ac in acs:
            ep = ac.get('endpoint')
            direction = ac.get('direction')
            endpoint_counts[(direction, ep)] += 1
            d = ac.get('data')
            if isinstance(d, dict):
                if direction == 'REQ':
                    req_key_counts[ep].update(d.keys())
                else:
                    res_key_counts[ep].update(d.keys())

print("=== scenario counts (sample) ===")
print(n_by_scenario)
print("=== top-level keys ===")
print(top_key_counts)
print("=== turn keys ===")
print(turn_key_counts)
print("=== command_type counts ===")
print(command_type_counts)
print("=== endpoint counts (top 40) ===")
for k,v in endpoint_counts.most_common(40):
    print(k, v)
print("=== REQ keys per endpoint (top 20 endpoints) ===")
for ep, ctr in sorted(req_key_counts.items(), key=lambda x: -sum(x[1].values()))[:20]:
    print(ep, dict(ctr.most_common(15)))
print("=== RES keys per endpoint (top 20 endpoints) ===")
for ep, ctr in sorted(res_key_counts.items(), key=lambda x: -sum(x[1].values()))[:20]:
    print(ep, dict(ctr.most_common(15)))

with open('tools/derive_constants/sample_files_by_scenario.json','w') as f:
    json.dump(picked_per_scenario, f, indent=2)
