from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .. import master_data, region

TABLES = ('skill_data', 'skill_level_value', 'race_proper_distance_rate',
          'race_proper_ground_rate', 'race_proper_runningstyle_rate',
          'race_motivation_rate', 'race_course_set_status', 'race_single_mode_team_status',
          'race_track', 'race_course_set', 'race', 'race_instance')
APTITUDES = tuple('proper_' + field for field in (
    'distance_short', 'distance_mile', 'distance_middle', 'distance_long',
    'running_style_nige', 'running_style_senko', 'running_style_sashi',
    'running_style_oikomi', 'ground_turf', 'ground_dirt'))


@functools.lru_cache(maxsize=1)
def _master_bundle():
    return {table: [dict(row) for row in master_data.query(f'SELECT * FROM {table}')]
            for table in TABLES}


def prepare_input(horses, course_id, ground, weather, season, seed, gates, single_mode, context):
    context = context or {}
    instance_id = int(context.get('race_instance_id') or 0)
    race = master_data.query_one('SELECT id FROM race WHERE course_set=? LIMIT 1', (course_id,))
    race_id = int(race['id']) if race else 0
    if instance_id:
        instance = master_data.query_one('SELECT race_id FROM race_instance WHERE id=?', (instance_id,))
        if instance:
            race_id = int(instance['race_id'])
    order = sorted(range(len(horses)), key=lambda i: gates[i])
    runners = []
    for rank, i in enumerate(order):
        h = dict(horses[i])
        h.update(frame_order=rank + 1, viewer_id=int(h.get('viewer_id') or 0),
                 chara_id=int(h.get('chara_id') or (h.get('card_id') or 0) // 100),
                 running_style=int(h.get('running_style') or 2),
                 motivation=int(h.get('motivation') or 3), skill_array=h.get('skill_array') or [])
        for key, alias in (('speed', 'speed'), ('stamina', 'stamina'), ('pow', 'power'),
                           ('guts', 'guts'), ('wiz', 'wisdom')):
            h[key] = max(1, int(h.get(key, h.get(alias, 1)) or 1))
        for key in APTITUDES:
            h[key] = int(h.get(key) or 1)
        runners.append(h)
    return dict(replay_sha='private-server-test', logged_seed=seed, seed=seed, seed_mode='logged',
                race_type=int(context.get('race_type') or (6 if single_mode else 9)),
                race_instance_id=instance_id, race_id=race_id, course_set=course_id,
                season=season, weather=weather, ground_condition=ground, horses=runners), order


def adapt_output(candidate, order, seed):
    n = len(order)
    if candidate.get('runner_count') != n or len(candidate.get('results', [])) != n or not candidate.get('frames'):
        raise ValueError('1:1 test engine returned an incomplete race')
    frames = []
    for frame in candidate['frames']:
        if len(frame['horses']) != n:
            raise ValueError('1:1 test engine returned an incomplete frame')
        runners = [None] * n
        for gate, h in enumerate(frame['horses']):
            block = h.get('block', -1)
            runners[order[gate]] = dict(pos=h['distance'], lane=h['lane'] / 10000,
                speed=h['speed'] / 100, hp=h['hp'], tempt=h.get('tempt', 0),
                block=order[block] if block >= 0 else -1)
        frames.append(dict(t=frame['t'], horses=runners))
    results = [None] * n
    for gate, r in enumerate(candidate['results']):
        results[order[gate]] = dict(finishOrder=r['order'], finishTime=r['time'],
            finishTimeRaw=r['raw_time'], finishTimeDisplay=r['time'], finishDiffDisplay=r['diff'],
            startDelayTime=r['delay'], gutsOrder=r['guts_order'], wizOrder=r['wiz_order'],
            lastSpurtStartDistance=r['last_spurt'], runningStyle=r['style'], defeat=r['defeat'])
    skills, events = [], []
    for e in candidate.get('events', []):
        params = e['params']
        if e['type'] == 3:
            skills.append(dict(t=e['t'], horseIndex=order[params[0]], skillId=params[1],
                               duration=params[2] / 10000 if params[2] >= 0 else -1))
        elif e['type'] in (4, 5, 6):
            events.append(dict(t=e['t'], horseIndex=order[params[0]], type=e['type']))
    return dict(seed=seed, frames=frames, results=results, skillEvents=skills, raceEvents=events,
                framesDownsampled=True, gateFrameNeeded=False,
                distanceDiffMax=candidate.get('distance_diff_max', 0),
                exactDiagnostics=candidate.get('diagnostics', {}), engine='exact_test')


def run(horses, course_id, ground, weather, season, seed, gates, single_mode=False, context=None):
    if region.PROCESS_REGION != 'global':
        raise ValueError('1:1 test engine supports Global only')
    if not 3 <= len(horses) <= 18:
        raise ValueError('1:1 test engine supports fields of 3–18 runners')
    server = Path(__file__).resolve().parents[2]
    exe = Path(os.environ.get('UMA_RACE_EXACT_RS_EXE') or server / 'race_exact_rs' / 'target' / 'release' /
               ('race_exact_rs.exe' if os.name == 'nt' else 'race_exact_rs'))
    assets = server / 'data' / 'race_param_define_global.json'
    payload, order = prepare_input(horses, course_id, ground, weather, season, seed, gates, single_mode, context)
    with tempfile.TemporaryDirectory(prefix='uma-exact-test-') as directory:
        root = Path(directory)
        input_path, master_path, output_path = (root / name for name in ('input.json', 'master.json', 'output.json'))
        input_path.write_text(json.dumps(payload), encoding='utf-8')
        master_path.write_text(json.dumps(_master_bundle()), encoding='utf-8')
        completed = subprocess.run([str(exe), 'simulate', '--input', str(input_path), '--master', str(master_path),
                                   '--assets', str(assets), '--output', str(output_path)],
                                  capture_output=True, text=True, timeout=30)
        if completed.returncode:
            raise RuntimeError(f'1:1 test engine failed: {completed.stderr[-2000:]}')
        return adapt_output(json.loads(output_path.read_text(encoding='utf-8')), order, seed)
