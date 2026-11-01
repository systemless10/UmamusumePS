from __future__ import annotations
import copy
import random
from ... import career_events as CE
from . import formula as F, master as M

SCENARIO_ID = 15
STATE_KEY = 'festa'
CAMP_PERIODS = ((13,16),(37,40),(61,64))


class Refused(ValueError):
    pass


def state(full_state):
    if not isinstance(full_state.get(STATE_KEY), dict):
        full_state[STATE_KEY] = {
            'acts': [{'act_type': i, 'act_gauge': 0, 'need_act_gauge': 5,
                      'act_training_state': 0, 'advice_id': 0, 'level': 1,
                      'friend_outing_gain_act_level': 0} for i in range(1,6)],
            'advices': [], 'roles': {}, 'results': [], 'preview': {},
            'hold_turn': 0, 'step': 1, 'phase': 0, 'flags': {}, 'gauge_due': None,
            'camp_extensions': {},
            'seed': random.randrange(1 << 63), 'link_steps': {}, 'fusion_preview': None,
            'auto': {'auto_call_preset_id': 0, 'preset_array': [
                {'preset_id': i, 'name': f'教えセット{i}', 'advice_info_array': [],
                 'auto_setting_attach_advice_info_array': []} for i in range(1,4)]}}
    return full_state[STATE_KEY]


def rng(st, *parts):
    return random.Random(':'.join(map(str, [st['seed'], *parts])))


def turn(ci):
    return int(ci.get('turn') or 1)


def shown_turn(st, ci):
    return st['hold_turn'] or turn(ci)


def camp_remaining(st, ci):
    t = shown_turn(st, ci)
    return next((end+st.get('camp_extensions',{}).get(str(start),0)-t+1
                 for start,end in CAMP_PERIODS if start <= t <= end+st.get('camp_extensions',{}).get(str(start),0)), 0)


def level_cap(ci):
    return 10 if 30323 in F.F.deck(ci).values() else 6


def roles(st, ci):
    out = []
    seen = set()
    for pos,sid in sorted(F.F.deck(ci).items()):
        out.append({'target_id': pos, 'chara_id': F.F.card_meta(sid)[3], 'act_type': st['roles'].get(str(pos),0)})
        seen.add(pos)
    for e in ci.get('evaluation_info_array') or []:
        tid = e['target_id']
        if tid not in seen and (tid == 8 or 102 <= tid <= 116):
            chara = 9000+tid-100 if 101 <= tid <= 108 else {109:9040,110:9043,111:9044,112:9047,113:9049,114:9050,115:1139,116:9052}.get(tid,tid)
            out.append({'target_id':tid,'chara_id':chara,'act_type':st['roles'].get(str(tid),0)})
            seen.add(tid)
    for tid,act in st['roles'].items():
        if int(tid) not in seen and int(tid) != 2001:
            out.append({'target_id':int(tid),'chara_id':int(tid),'act_type':act})
    return out


def seed_roles(st, ci):
    if st['roles']:
        return
    deck=F.F.deck(ci)
    for pos,sid in deck.items():
        act=assigned_act(ci,sid)
        if act:st['roles'][str(pos)]=act
    deck_charas = {F.F.card_meta(s)[3] for s in F.F.deck(ci).values()}
    for r in M.rows('single_mode_15_act_member'):
        if r['chara_id'] not in deck_charas and r['chara_id'] != int(ci.get('card_id',0))//100:
            st['roles'][str(r['chara_id'])] = r['act_type']
        else:
            add_member(st,ci,r['act_type'])
    for sid in deck.values():
        if F.F.card_meta(sid)[0]!=1:
            counts={a:sum(role==a for role in st['roles'].values()) for a in range(1,6)}
            add_member(st,ci,min(counts,key=lambda a:(counts[a],a)))


def assigned_act(ci,sid):
    kind,command,*_=F.F.card_meta(sid)
    if kind!=1 or command not in F.NORMAL:return 0
    normal=tuple(sorted(s for s in F.F.deck(ci).values() if F.F.card_meta(s)[0]==1))
    if normal==tuple(sorted((30145,30186,30304,30242,30260))) and sid==30242:return 4
    return F.NORMAL.index(command)+1


def add_member(st,ci,act_type):
    from ... import master_data
    excluded={int(ci.get('card_id') or 0)//100}|{F.F.card_meta(s)[3] for s in F.F.deck(ci).values()}
    excluded|={int(t) for t in st['roles'] if int(t)>=1000}
    excluded|={r['chara_id'] for r in M.rows('single_mode_15_act_member')}
    candidates=[int(r['chara_id']) for r in master_data.query(
        'SELECT DISTINCT chara_id FROM card_data WHERE chara_id BETWEEN 1000 AND 1999 ORDER BY chara_id')
        if int(r['chara_id']) not in excluded]
    if not candidates:return
    chara=rng(st,'member',act_type,len(st['roles'])).choice(candidates)
    st['roles'][str(chara)]=act_type
    evaluations=ci.setdefault('evaluation_info_array',[])
    if not any(e['target_id']==chara for e in evaluations):
        evaluations.append({'target_id':chara,'evaluation':0,'is_appear':1,'is_outing':0,'story_step':0})


def sync_training_levels(full_state, ci):
    old = {r['command_id']: r['level'] for r in ci.get('training_level_info_array') or []}
    levels = ([{'command_id': cid, 'level': old.get(cid, 1)} for cid in sorted(F.NORMAL)] +
              [{'command_id': cid, 'level': state(full_state)['step']} for cid in F.PRACTICE])
    changed = ci.get('training_level_info_array') != levels
    ci['training_level_info_array'] = levels
    return changed


def dataset(full_state, ci, full=True):
    sync_training_levels(full_state, ci)
    st = state(full_state)
    result = {'festa_info': {'summer_camp_remain_turn_num': camp_remaining(st,ci),
                             'max_act_level':level_cap(ci),'act_training_step':st['step']},
              'command_info_array': copy.deepcopy(list(st['preview'].values())) if full else None,
              'evaluation_info_array': roles(st,ci), 'advice_info_array':copy.deepcopy(st['advices']),
              'act_info_array':copy.deepcopy(st['acts']), 'check_point_result_array':copy.deepcopy(st['results']),
              'command_gain_act_gauge_array':None,
              'not_up_parameter_info':copy.deepcopy(st['flags'].get('not_up') or empty_not_up()),
              'auto_setting_info':copy.deepcopy(st['auto'])}
    for act in result['act_info_array']:
        act['act_training_state'] = 2 if camp_remaining(st,ci) else int(act['act_gauge'] >= 5 or turn(ci)>=73)
    if full:
        gains = nontraining_gauge(st,ci)
        result['command_gain_act_gauge_array'] = [{'command_type':ct,'predict_gain_act_gauge_array':gains if ct!=8 else []}
                                                  for ct in (3,4,7,8)]
    return result


def nontraining_gauge(st,ci,include_capped=False):
    if not st['advices']:
        return []
    chosen = rng(st,'nontraining',turn(ci)).choice(st['acts'])
    return [{'act_type':chosen['act_type'],'gain_act_gauge':1}] if include_capped or chosen['act_gauge'] < 5 else []


def refresh_preview(full_state,ci,commands,levels=None):
    st = state(full_state); ds = dataset(full_state,ci,False)
    preview = {}
    for cmd in commands or []:
        if cmd.get('command_type') != 1 or cmd['command_id'] not in (*F.NORMAL,*F.PRACTICE):
            continue
        cid = cmd['command_id']
        upper = False
        if st['advices'] and cmd.get('is_enable'):
            ctx = F.context(ci,ds,cmd,levels)
            rates = [r['effect_value_4' if ctx['practice'] else 'effect_value_2']
                     for _,a in ctx['actors'] for r in F.effect_rows(ctx,a) if r['effect_type']==8]
            upper = bool(rates) and rng(st,'upper',turn(ci),cid).randrange(100) < max(rates)
        preview[str(cid)] = {'command_type':1,'command_id':cid,
                            'params_inc_dec_info_array':F.bonus(ci,ds,cmd,levels) if st['advices'] and cmd.get('is_enable') else [],
                            'predict_gain_act_gauge_array':F.gauge(ci,ds,cmd) if st['advices'] and cmd.get('is_enable') else [],
                            'is_upper_training_event_occurred':upper}
    changed = st['preview'] != preview
    st['preview'] = preview
    return changed


def empty_not_up():
    return {'is_overflow_act_gauge_event_gain':False,'is_overflow_act_level_event_gain':False,'not_level_up_act_type_array':[]}


def add_gauge(st, gains, event=False):
    for gain in gains:
        act = next(a for a in st['acts'] if a['act_type']==gain['act_type'])
        if event and act['act_gauge']+gain['gain_act_gauge']>5:
            st['flags'].setdefault('not_up',empty_not_up())['is_overflow_act_gauge_event_gain']=True
        act['act_gauge'] = min(5,act['act_gauge']+gain['gain_act_gauge'])


def gain_level(st, ci, act):
    if act['level']>=level_cap(ci):
        ids=st['flags'].setdefault('not_up',empty_not_up())['not_level_up_act_type_array']
        if act['act_type'] not in ids:ids.append(act['act_type'])
    act['level'] = min(level_cap(ci),act['level']+1)


def validate_advice(st, assignments, advices, *, fusion=False):
    if not isinstance(assignments,list) or not isinstance(advices,list) or len(assignments)!=5:
        raise Refused('both complete teaching arrays are required')
    if {a.get('act_type') for a in assignments} != set(range(1,6)):
        raise Refused('each of five acts must be assigned exactly once')
    ids = [a.get('advice_id') for a in advices]
    if len(ids)!=5 or len(set(ids))!=5 or sorted(a.get('advice_id') for a in assignments)!=sorted(ids):
        raise Refused('five distinct owned teachings required')
    owned = {a['advice_id'] for a in st['advices']}
    donors = []
    for row in advices:
        aid, donor = row.get('advice_id'), row.get('fusion_advice_id',0)
        if aid not in owned:
            raise Refused('unoffered teaching')
        previous = next(a for a in st['advices'] if a['advice_id']==aid)
        if previous.get('fusion_advice_id') and donor != previous['fusion_advice_id']:
            raise Refused('a committed fusion cannot be changed')
        if donor:
            if aid<601 or donor not in (101,201,301,401,501) or donor in ids:
                raise Refused('invalid fusion donor')
            if not previous.get('fusion_advice_id') and donor not in owned:
                raise Refused('unowned fusion donor')
            donors.append(donor)
    if len(donors)!=len(set(donors)):
        raise Refused('fusion donor reused')
    if owned != set(ids)|(set(donors)&owned):
        raise Refused('offered teaching discarded')


def attach_advice(full_state,ci,assignments,advices):
    st=state(full_state)
    validate_advice(st,assignments,advices)
    st['advices']=copy.deepcopy(advices)
    for row in assignments:
        next(a for a in st['acts'] if a['act_type']==row['act_type'])['advice_id']=row['advice_id']
    st['fusion_preview']=None;st['hold_turn']=0
    ci['playing_state']=1
    refresh_home(full_state,ci)


def refresh_home(full_state,ci):
    from ..ramen.impl import refresh_home as common_refresh
    common_refresh(full_state,ci)
