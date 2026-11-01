from __future__ import annotations
import collections
import math
from ..ramen import formula as F
from ... import training_formula as TF
from . import master as M
NORMAL = (101, 105, 102, 103, 106)
PRACTICE = {5101+i: cid for i,cid in enumerate(NORMAL)}
MASTER = M.TABLES


def displayed(ci, target, gain):
    if target > 5:
        return gain
    stat = F.STATS[target - 1]
    current = ci.get(stat, 0)
    whole = min(gain, max(0, 1200 - current))
    tail = gain - whole
    value = whole + (max(1, tail // 2) if tail > 0 else 0)
    return max(0, min(value, ci.get('max_' + stat, 9999) - current))

BASE = {
    101: {lv: {1: 11 + lv, 3: (2 if lv <= 3 else 3)} for lv in range(1, 6)},
    105: {lv: {2: 9 + lv, 4: (3, 4, 4, 5, 6)[lv - 1]} for lv in range(1, 6)},
    102: {lv: {2: (6, 6, 6, 7, 8)[lv - 1], 3: 8 + lv} for lv in range(1, 6)},
    103: {lv: {1: 2 if lv <= 3 else 3, 3: 2 if lv <= 4 else 3, 4: 11 + lv} for lv in range(1, 6)},
    106: {lv: {1: 2 if lv <= 3 else 3, 5: 7 + lv} for lv in range(1, 6)},
    5101: {1: {1: 16, 3: 3}, 2: {1: 21, 3: 3}},
    5102: {1: {2: 14, 4: 6}, 2: {2: 20, 4: 8}},
    5103: {1: {2: 6, 3: 14}, 2: {2: 8, 3: 20}},
    5104: {1: {1: 3, 3: 2, 4: 15}, 2: {1: 3, 3: 2, 4: 23}},
    5105: {1: {1: 3, 5: 12}, 2: {1: 4, 5: 14}},
}
SP = {101: 7, 105: 7, 102: 7, 103: 7, 106: 6,
      5101: {1: 9, 2: 10}, 5102: {1: 10, 2: 12}, 5103: {1: 10, 2: 12},
      5104: {1: 10, 2: 12}, 5105: {1: 8, 2: 9}}


def context(ci, ds, cmd, levels=None):
    cid, lv = cmd['command_id'], cmd['level']
    practice = cid in PRACTICE
    base_cid = PRACTICE.get(cid, cid)
    act_type = list(NORMAL).index(base_cid) + 1
    by_act = {a['act_type']: a for a in ds.get('act_info_array') or []}
    roles = {e['target_id']: e['act_type'] for e in ds.get('evaluation_info_array') or []}
    deck, bonds = F.deck(ci), F.bonds(ci)
    def rainbow(partner):
        kind, typ, *_ = F.card_meta(deck.get(partner, 0))
        return kind == 1 and typ == base_cid and bonds.get(partner, 0) >= 80
    partners = cmd.get('training_partner_array') or []
    any_rainbow = any(rainbow(p) for p in partners)
    levels = levels or {c['support_card_id']: TF.card_level(c['support_card_id'], c.get('exp', 0), c.get('limit_break_count'))
              for c in ci.get('support_card_array') or []}
    sp = SP[cid][lv] if practice else SP[cid]
    raw = F.home_raw(ci, base_cid, lv, partners, levels,
                     table={base_cid: BASE[cid]}, sp=sp)
    return {'ci': ci, 'ds': ds, 'cmd': cmd, 'practice': practice,
            'raw': raw, 'home_pre': {tt: F.capped(v) for tt, v in raw.items()},
            'main': act_type, 'actors': [(p, by_act[roles[p]]) for p in partners if roles.get(p) in by_act],
            'rainbow': rainbow, 'any_rainbow': any_rainbow,
            'act_level': (by_act.get(act_type) or {}).get('level', 1),
            'deck_types': F.deck_type_count(ci)}


def percentages(ctx):
    p = collections.Counter()
    practice = ctx['practice']
    for partner, act in ctx['actors']:
        for r in effect_rows(ctx, act):
            typ = r['effect_type']
            if typ not in (13, 203, 202, 21, 206):
                continue
            if r['condition_type'] == 3 and ctx['any_rainbow']:
                continue
            if typ == 202 and not ctx['any_rainbow']:
                continue
            p[typ] += r['effect_value_4' if practice else 'effect_value_2']
    if 47 in (ctx['ci'].get('chara_effect_id_array') or []):
        p[203] += 5
    if ctx['any_rainbow']:
        results = ctx['ds'].get('check_point_result_array') or []
        if results:
            result = max(results, key=lambda r:r['schedule_id'])
            p[202] += sum(r['effect_value_4'] for r in MASTER['single_mode_15_check_point_effect']
                          if r['schedule_id']==result['schedule_id'] and r['result_state']==result['result_type']
                          and r['effect_type']==202)
        if practice:
            p[202] += sum(r['effect_value_4'] for r in MASTER['single_mode_15_act_effect']
                          if r['level']==ctx['act_level'] and r['effect_type']==202)
    if practice and ctx['deck_types'] >= 5:
        for r in MASTER['single_mode_15_act_effect']:
            if r['level']==ctx['act_level'] and r['effect_type'] in (201,109,206):
                p[1006 if r['effect_type']==206 else r['effect_type']] += r['effect_value_4']
    return p


def predict(ctx):
    p = percentages(ctx)
    result = {}
    for tt, home in ctx['home_pre'].items():
        if tt == ctx['main']:
            multiplier = (1+p[203]/100)*(1+p[202]/100)*(1+p[13]/100)
        else:
            modifier = p[21] if tt<=5 else p[201]
            multiplier = (1+(p[203]+modifier)/100)*(1+p[202]/100)
        delta = math.floor(home*(multiplier-1)+1e-9)
        if ctx['practice'] and tt<=5:
            delta += 10
        limit = p[109] if tt==30 else p[1006]+(p[206] if tt==ctx['main'] else 0)
        delta = min(delta,100+limit)
        if delta:
            result[tt] = F._shown_bonus(ctx['ci'],tt,home,delta)
    if ctx['practice']:
        for tt in range(1,6):
            result.setdefault(tt,F._shown_bonus(ctx['ci'],tt,0,10))
    return result


def home_gains(ci, cid, level, partners, levels=None):
    cmd = {'command_id': cid, 'level': level, 'training_partner_array': partners}
    ctx = context(ci, {}, cmd, levels)
    out = {('skill_point' if tt == 30 else F.STATS[tt - 1]): displayed(ci, tt, value)
           for tt, value in ctx['home_pre'].items()}
    cards = [(sid, bond, None) for sid, bond in F.facility_cards(ci, partners)]
    cost = M.energy(cid, level)
    base = PRACTICE.get(cid, cid)
    out['energy_cost'] = (F.facility_energy(cost, base, cards, levels) if cost < 0
                          else TF.training_energy(cost, base, cards))
    return out


def effect_rows(ctx, act):
    rows = [r for r in MASTER['single_mode_15_advice_effect']
            if r['advice_id'] == act['advice_id'] and r['level'] == act['level']]
    advice = next((r for r in ctx['ds'].get('advice_info_array') or []
                   if r['advice_id'] == act['advice_id']), {})
    return rows + [r for r in MASTER['single_mode_15_fusion_effect']
                   if r['advice_id'] == advice.get('fusion_advice_id')]


def bonus(ci, ds, cmd, levels=None):
    ctx = context(ci, ds, cmd, levels)
    result = predict(ctx)
    pct = sum(r['effect_value_4' if ctx['practice'] else 'effect_value_2']
              for _, act in ctx['actors'] for r in effect_rows(ctx, act) if r['effect_type'] == 3)
    energy = sum(p['value'] for p in cmd.get('params_inc_dec_info_array') or [] if p['target_type'] == 10)
    delta = int(energy * max(0, 1 + pct / 100) - 1e-9) - energy if energy < 0 else 0
    if ctx['practice'] and ci.get('turn', 1) >= 73:
        delta += 30
        result[20] = 1
    if delta:
        result[10] = delta
    return [{'target_type': tt, 'value': value} for tt, value in sorted(result.items())]


def failure(ci, ds, cmd, levels=None):
    ctx = context(ci, ds, cmd, levels)
    down = 100 if ctx['practice'] and ci.get('turn', 1) >= 73 else 0
    if any(r['effect_type'] == 205 and ci.get('vital', 100) >= r['condition_value']
           for _, act in ctx['actors'] for r in effect_rows(ctx, act)):
        down = 100
    own_guard = ctx['practice'] and any(a['act_type'] == ctx['main'] and
        any(r['effect_type'] == 205 and ci.get('vital',100) >= r['condition_value'] for r in effect_rows(ctx,a))
        for _,a in ctx['actors'])
    if ctx['practice'] and ci.get('turn',1) >= 73 or own_guard:
        override = TF.forced_failure_rate()
        return 0 if override is None else override
    levels = levels or {c['support_card_id']: TF.card_level(c['support_card_id'], c.get('exp', 0), c.get('limit_break_count'))
                        for c in ci.get('support_card_array') or []}
    cards = [(sid, levels.get(sid, F.max_level(sid))) for sid, _ in F.facility_cards(ci, cmd.get('training_partner_array'))]
    return TF.failure_rate(ci.get('vital', 100), command_id=cmd['command_id'], facility_level=cmd['level'],
                           conditions=ci.get('chara_effect_id_array'), extra_down_pct=down, facility_cards=cards)


def gauge(ci, ds, cmd):
    if cmd['command_id'] in PRACTICE:
        return []
    ctx = context(ci, ds, cmd)
    gains = collections.Counter()
    for partner, act in ctx['actors']:
        gains[act['act_type']] += 2 if ctx['rainbow'](partner) else 1
    by_act = {a['act_type']: a for a in ds['act_info_array']}
    return [{'act_type': a, 'gain_act_gauge': min(n, max(0, 5 - by_act[a]['act_gauge']))}
            for a, n in sorted(gains.items()) if by_act[a]['act_gauge'] < 5]
