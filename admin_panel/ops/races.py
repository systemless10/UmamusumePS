import base64
import copy
import functools
import gzip
import json
from dataclasses import asdict
from pathlib import Path

from app import master_data as M
from app.handlers import practice_race as P, practice_race_lobby as L
from app.simulation import race_simulator as S
from app.simulation.scenario_format import decode_scenario
from . import op
from .common import OpError, load, save, text, card_name, APTITUDES, int_arg
from .catalog import uma_catalog

KEY = 'admin_race_library'
MAX_RUNNERS = 255
MAIN_RUNNERS = 3


def definition(args):
    d = copy.deepcopy(args.get('definition'))
    if not isinstance(d, dict) or not isinstance(d.get('runners'), list):
        raise OpError('A race needs settings and a runner list.')
    if not 1 <= len(d['runners']) <= MAX_RUNNERS:
        raise OpError(f'Use 1–{MAX_RUNNERS} runners.')
    settings = d.setdefault('settings', {})
    rid = int_arg(settings, 'race_instance_id', lo=1)
    if S.get_course_for_race_instance(rid) is None:
        raise OpError('Unknown course/race instance for this region.')
    for k, hi in [('season', 5), ('weather', 4), ('ground_condition', 4), ('motivation', 5)]:
        settings[k] = int_arg(settings, k, 1 if k != 'motivation' else 3, 0, hi)
    if settings.get('seed') is not None:
        settings['seed'] = int_arg(settings, 'seed', lo=0, hi=2147483647)
    for i, h in enumerate(d['runners']):
        if not isinstance(h, dict):
            raise OpError('Each runner must be a record.')
        h['card_id'] = int_arg(h, 'card_id', 0, 0)
        if h.get('mob_id'):
            if not M.query_one('SELECT mob_id FROM mob_data WHERE mob_id=?', (int(h['mob_id']),)):
                raise OpError(f'Runner {i+1}: unknown mob.')
        elif not M.query_one('SELECT id FROM card_data WHERE id=?', (h['card_id'],)) \
                and not (h.get('chara_id') and h.get('race_dress_id') and
                         M.query_one('SELECT id FROM chara_data WHERE id=?', (h['chara_id'],))):
            raise OpError(f'Runner {i+1}: unknown card.')
        for k in ('speed', 'stamina', 'power', 'guts', 'wiz'):
            h[k] = int_arg(h, k, h.get('pow', 800) if k == 'power' else 800, 1, 32767)
        h['pow'] = h['power']
        h['running_style'] = int_arg(h, 'running_style', 2, 1, 4)
        h['motivation'] = int_arg(h, 'motivation', settings['motivation'] or 3, 1, 5)
        for k in APTITUDES:
            h[k] = int_arg(h, k, 7, 1, 10)
        h['trained_chara_id'] = 800000000 + i
        h.setdefault('rarity', 3)
        h.setdefault('talent_level', 5)
        skills = h.setdefault('skill_array', [])
        if not isinstance(skills, list):
            raise OpError('Skills must be an array of skill_id/level records.')
        for skill in skills:
            sid = int_arg(skill, 'skill_id', lo=1)
            if not M.query_one('SELECT id FROM skill_data WHERE id=?', (sid,)):
                raise OpError(f'Unknown skill {sid}.')
            skill['level'] = int_arg(skill, 'level', 1, 1, 6)
    gates = settings.get('gate_assignment')
    if gates is not None and (not isinstance(gates, list) or len(gates) != len(d['runners'])
            or any(type(g) is not int or g < 0 or g >= MAX_RUNNERS for g in gates)
            or len(set(gates)) != len(gates)):
        raise OpError(f'gate_assignment must contain one distinct gate index per runner (0–{MAX_RUNNERS - 1}).')
    for h in d['runners']:
        h['main'] = bool(h.get('main'))
        if not h['main']:
            del h['main']
    if sum(1 for h in d['runners'] if h.get('main')) > MAIN_RUNNERS:
        raise OpError(f'Pick at most {MAIN_RUNNERS} Main runners.')
    if not isinstance(settings.get('race_context', {}), dict):
        raise OpError('race_context must be an object.')
    return d


@op('races.list')
def listing(viewer, args):
    full = load(viewer)
    st = L.state(full)
    return {'templates': full.get(KEY, []),
            'saved': [dict(L._meta(e), name=e.get('name', 'Practice race')) for e in st['saved']],
            'pending': L._meta(st['pending']) if st.get('pending') else None,
            'queued': queue_meta(full.get('admin_practice_race_queue')),
            'slot_limit': L.saved_race_limit(),
            'roster': P._get_roster(viewer),
            'cards': uma_catalog()['cards'],
            'courses': [dict(r, name=text(28, r['id']) or f"Race {r['id']}")
                        for r in M.query('SELECT id, race_id FROM race_instance ORDER BY id')]}


def queue_meta(queued):
    if not queued:
        return None
    request = queued.get('request', {})
    return {'name': queued.get('name') or 'Queued race', 'race_instance_id': request.get('race_instance_id'),
            'entry_num': request.get('entry_num')}


@op('races.course')
def course_profile(viewer, args):
    found = S.get_course_for_race_instance(int_arg(args, 'id', lo=1))
    if found is None:
        raise OpError('Unknown course/race instance for this region.')
    _, course, gates = found
    return dict(course, gates=gates)


COURSE_PATHS = Path(__file__).resolve().parents[2] / 'server' / 'data' / 'course_paths.json'


@functools.cache
def course_paths():
    return json.loads(COURSE_PATHS.read_text(encoding='utf-8')) if COURSE_PATHS.is_file() else {}


@op('races.track')
def track_layout(viewer, args):
    found = S.get_course_for_race_instance(int_arg(args, 'id', lo=1))
    if found is None:
        raise OpError('Unknown course/race instance for this region.')
    set_id, course, gates = found
    paths = course_paths()
    route = paths.get(str(set_id))
    ground = route['ground'] if route else course.get('surface')
    track = route['track'] if route else course.get('raceTrackId')
    outline = [[v for i in range(0, len(p['points']), 8) for v in p['points'][i:i + 2]] + p['points'][-2:]
               for k, p in paths.items() if k != str(set_id) and p['track'] == track and p['ground'] == ground]
    return {'course_set': set_id, 'course': dict(course, gates=gates),
            'route': route['points'] if route else None, 'outline': outline}


@op('races.delete_template', label='Delete race template')
def delete_template(viewer, args):
    full = load(viewer)
    library = full.get(KEY, [])
    tid = int_arg(args, 'id')
    if not any(e['id'] == tid for e in library):
        raise OpError('Template no longer exists.')
    full[KEY] = [e for e in library if e['id'] != tid]
    save(viewer, full)
    return {'id': tid}


@op('races.delete_saved', label='Delete saved practice race')
def delete_saved(viewer, args):
    full = load(viewer)
    st = L.state(full)
    rid = int_arg(args, 'id')
    if not any(e['practice_race_id'] == rid for e in st['saved']):
        raise OpError('Race no longer exists. Refresh the list.')
    st['saved'] = [e for e in st['saved'] if e['practice_race_id'] != rid]
    save(viewer, full)
    return {'id': rid}


@op('races.unqueue', label='Cancel queued practice race')
def unqueue(viewer, args):
    full = load(viewer)
    full.pop('admin_practice_race_queue', None)
    save(viewer, full)
    return {}


@op('races.save', label='Save race template')
def save_template(viewer, args):
    d = definition(args)
    full = load(viewer)
    library = full.setdefault(KEY, [])
    tid = args.get('id')
    old = next((e for e in library if e['id'] == tid), None)
    if tid is not None and old is None:
        raise OpError('Template no longer exists.')
    row = {'id': tid or max([e['id'] for e in library] or [0]) + 1,
           'name': str(args.get('name') or 'Custom race')[:120], 'definition': d}
    if old:
        library[library.index(old)] = row
    else:
        library.append(row)
    save(viewer, full)
    return row


def entry(full, args):
    st = L.state(full)
    pool = st['saved'] + ([st['pending']] if st.get('pending') else [])
    e = next((e for e in pool if e['practice_race_id'] == int_arg(args, 'id')), None)
    if e is None:
        raise OpError('Race no longer exists. Refresh the list.')
    return e


def by_gate(e):
    rows = e['payload']['race_result_info']['race_horse_data_array']
    gates = e.get('gate_assignment')
    if gates and len(gates) == len(rows):
        return [rows[i] for i in sorted(range(len(rows)), key=gates.__getitem__)]
    return sorted(rows, key=lambda h: h['frame_order'])


def inspect(e):
    info = e['payload']['race_result_info']
    decoded = decode_scenario(gzip.decompress(base64.b64decode(info['race_scenario'])))
    horses = by_gate(e)
    course = S.get_course_for_race_instance(info['race_instance_id'])
    return {'entry': e, 'horses': horses, 'results': [asdict(r) for r in decoded['results']],
            'frames': [{'t': t, 'horses': [asdict(h) for h in hs]} for t, hs in decoded['frames']],
            'events': decoded['events'], 'course': S._course_data()[str(course[0])] if course else {},
            'definition': copy.deepcopy(e.get('definition')) or {'settings': dict(race_instance_id=info['race_instance_id'],
                            season=info['season'], weather=info['weather'],
                            ground_condition=info['ground_condition'], seed=info['random_seed']),
                           'runners': copy.deepcopy(info['race_horse_data_array'])}}


@op('races.possible')
def possible(viewer, args):
    from app.simulation.race_engine import race_runner, race_solver_builder as B
    from app.simulation.race_engine.region import Region, RegionList
    from app.simulation.race_engine.race_solver import Perspective
    e = entry(load(viewer), args)
    info = e['payload']['race_result_info']
    horses = by_gate(e)
    gates = sorted(e['gate_assignment']) if e.get('gate_assignment') else [h['frame_order']-1 for h in horses]
    idx = int_arg(args, 'runner', 0, 0, len(horses)-1)
    course = S.get_course_for_race_instance(info['race_instance_id'])
    spec = S.build_spec(horses, course[0], info['ground_condition'], info['weather'],
                        info['season'], info['random_seed'], gates)
    b = race_runner._new_builder(spec, spec['horses'][idx], idx)
    horse = B.build_base_stats(b._horse, b._race_params.mood)
    windows = []
    for skill in horses[idx].get('skill_array', []):
        sid = str(skill['skill_id'])
        try:
            data = B.build_skill_data(horse, b._race_params, b._course,
                    RegionList([Region(0, b._course.distance)]), b._parser,
                    sid, Perspective.SELF, skill.get('level', 1), rng=b._rng,
                    ctx=b._context)
            windows.append({'id': int(sid), 'name': text(47, int(sid)) or sid,
                'passive': any(sd.green for sd in data),
                'regions': [[r.start, r.end] for sd in data for r in sd.regions
                            if r.start <= b._course.distance]})
        except (ValueError, KeyError, NotImplementedError) as ex:
            windows.append({'id': int(sid), 'name': text(47, int(sid)) or sid,
                            'regions': [], 'error': str(ex)})
    return windows


@op('races.inspect')
def inspect_saved(viewer, args):
    return inspect(entry(load(viewer), args))


@op('races.run', label='Simulate custom race')
def run(viewer, args):
    d = definition(args)
    settings = d['settings']
    order = sorted(range(len(d['runners'])), key=lambda i: not d['runners'][i].get('main'))
    horses = [{k: v for k, v in d['runners'][i].items() if k != 'main'} for i in order]
    if settings.get('gate_assignment'):
        settings = dict(settings, gate_assignment=[settings['gate_assignment'][i] for i in order])
    course_id, _, gates = S.get_course_for_race_instance(settings['race_instance_id'])
    import random
    season, weather, ground = P.resolve_conditions(settings, random.Random(settings.get('seed')))
    stalls, gates = gates, max(gates, len(horses))
    sim = S.run_simulation(horses, course_id, ground, weather, season, settings.get('seed'),
                           gate_count=stalls, gate_assignment=settings.get('gate_assignment'),
                           single_mode=bool(settings.get('single_mode')),
                           race_context=settings.get('race_context'))
    blob, _, _ = S.build_race_scenario(sim, len(horses), gates)
    rows, assignments = P.race_horse_rows({'horses': horses, 'sim_results': sim['results'],
                                           'gate_assignment': sim['gate_assignment'],
                                           'gate_count': stalls})
    request = dict(settings, viewer_id=viewer, entry_num=len(horses),
                   entry_chara_array=[{'entry_id': i, 'trained_chara_id': h['trained_chara_id']} for i, h in enumerate(horses)])
    data = {'trained_chara_array': horses,
            'entry_info_array': [{'entry_id': i, 'frame_order': assignments[i]+1} for i in range(len(horses))],
            'practice_partner_owner_info_array': [],
            'race_result_info': {'race_instance_id': settings['race_instance_id'],
                'season': season, 'weather': weather, 'ground_condition': ground,
                'random_seed': sim['seed'], 'race_scenario': blob, 'race_horse_data_array': rows}}
    rid = L.remember_race(viewer, request, data)
    full = load(viewer)
    full[L.STATE_KEY]['pending']['name'] = str(args.get('name') or 'Custom race')[:120]
    full[L.STATE_KEY]['pending']['definition'] = d
    full[L.STATE_KEY]['pending']['gate_assignment'] = sim['gate_assignment']
    save(viewer, full)
    return inspect(full[L.STATE_KEY]['pending'])


@op('races.inject', label='Inject race into practice: {destination}')
def inject(viewer, args):
    full = load(viewer)
    e = copy.deepcopy(entry(full, args))
    destination = args.get('destination')
    if destination == 'practice':
        full['admin_practice_race_queue'] = {'name': e.get('name'), 'payload': e['payload'], 'request': {
            'race_instance_id': e['race_instance_id'], 'entry_num': e['entry_num'],
            'entry_chara_array': [{'trained_chara_id': h['trained_chara_id']} for h in e['payload']['trained_chara_array']],
            **{k: e.get('before_'+k, 0) for k in ('season', 'weather', 'ground_condition', 'motivation')}}}
        save(viewer, full)
    elif destination == 'saved':
        st = L.state(full)
        overwrite = int(args.get('overwrite_id') or 0)
        if overwrite and not any(r['practice_race_id'] == overwrite for r in st['saved']):
            raise OpError('Choose an existing saved race to replace.')
        e['practice_race_id'] = st['next_race_id']
        st['next_race_id'] += 1
        previous_pending = st['pending']
        st['pending'] = e
        save(viewer, full)
        L.handle_race_end({'viewer_id': viewer, 'is_save': 1, 'overwrite_race_id': overwrite})
        full = load(viewer)
        L.state(full)['pending'] = previous_pending
        save(viewer, full)
    else:
        raise OpError('Destination must be practice or saved.')
    return {'id': e['practice_race_id'], 'destination': destination}
