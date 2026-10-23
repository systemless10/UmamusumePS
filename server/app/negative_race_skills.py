import random

from . import master_data, race_conditions

GRANT_CHANCE = 0.08
_COURSES = dict(zip(
    (10006, 10005, 10009, 10008, 10007, 10001, 10002, 10004, 10003, 10010,
     10101, 10103, 10104, 10105, 10201, 10202, 10203),
    (200033, 200043, 200053, 200063, 200073, 200083, 200093, 200103, 200113,
     200123, 200953, 202203, 202213, 202223, 202733, 204234, 204244)))


def effect(chara_info, program_id, *, hidden=False, full_state=None):
    row = master_data.query_one(
        'SELECT cs.race_track_id, cs.distance, cs.turn, r.grade, ri.time, r.is_dirtgrade '
        'FROM single_mode_program p JOIN race_instance ri ON ri.id=p.race_instance_id '
        'JOIN race r ON r.id=ri.race_id JOIN race_course_set cs ON cs.id=r.course_set '
        'WHERE p.id=?', (program_id,))
    if not row:
        return None
    context = dict(row)
    ctx = (full_state or {}).get('active_race_ctx') or {}
    if ctx.get('program_id') == program_id:
        info = ctx.get('race_start_info') or {}
        for key in ('season', 'weather', 'ground_condition'):
            if info.get(key) is not None:
                context[key] = info[key]
        horses = info.get('race_horse_data') or []
        player = horses[0] if horses else {}
        for key in ('post_number', 'popularity'):
            if player.get(key) is not None:
                context[key] = player[key]
        if ctx.get('player_popularity') is not None:
            context['popularity'] = ctx['player_popularity']
        style = player.get('running_style')
        if style and horses and all(h.get('running_style') for h in horses):
            context['running_style_count_same_rate'] = (
                100 * sum(h['running_style'] == style for h in horses) / len(horses))
    elif full_state is None:
        context.update(race_conditions.career_conditions(chara_info, program_id))
    return {'type': 'negative_race_skill', 'context': context,
            'display_id': 8, 'hidden': hidden}


def candidates(chara_info, context):
    ids = [_COURSES.get(context.get('race_track_id'))]
    if context.get('distance'):
        ids.append(200133 if context['distance'] % 400 == 0 else 200143)
    direction = context.get('turn')
    if direction in (1, 2):
        ids.append(200013 if direction == 1 else 200023)
    season = context.get('season')
    if season in (1, 2, 3, 4, 5):
        ids.append({1: 200173, 2: 200183, 3: 200193, 4: 200203, 5: 200173}[season])
    ground = context.get('ground_condition')
    if ground in (1, 2, 3, 4):
        ids.append(200153 if ground == 1 else 200163)
    if context.get('grade') == 100:
        ids.append(200311)
    if context.get('weather') == 3:
        ids.append(200233)
    if context.get('popularity') == 1:
        ids.append(200321)
    post = context.get('post_number')
    if post is not None and 1 <= post <= 3:
        ids.append(200253)
    elif post is not None and post >= 6:
        ids.append(200263)
    if context.get('running_style_count_same_rate', -1) >= 40:
        ids.append(200283)
    if context.get('time') == 4:
        ids.append(202233)
    if context.get('race_track_id') in (10001, 10002, 10004, 10010, 10103, 10104):
        ids.append(202243)
    if context.get('is_dirtgrade') == 1:
        ids.append(202253)
    owned = {s.get('skill_id') for s in chara_info.get('skill_array') or ()}
    return [sid for sid in ids if sid and sid not in owned and _negative_group(sid) is not None]


@master_data.context_cache(maxsize=128)
def _negative_group(skill_id):
    row = master_data.query_one('SELECT group_id, grade_value FROM skill_data WHERE id=?', (skill_id,))
    return int(row['group_id']) if row and row['grade_value'] < 0 else None


def apply(chara_info, context):
    pool = candidates(chara_info, context)
    from . import tuning
    if not pool or random.random() >= tuning.bad(GRANT_CHANCE):
        return None
    skill_id = random.choice(pool)
    group = _negative_group(skill_id)
    skills = chara_info.setdefault('skill_array', [])
    skills[:] = [s for s in skills if master_data.skill_tip_key(s.get('skill_id'))[0] != group]
    skills.append({'skill_id': skill_id, 'level': 1})
    return skill_id
