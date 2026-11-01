from __future__ import annotations
import copy
from ... import career_events as CE
from ...result_codes import refuse
from . import impl, master as M, producers as P


def smt():
    from ...handlers import single_mode_team
    return single_mode_team


def career(payload):
    viewer=payload['viewer_id'];full=smt().state_store.get_state(viewer) or {}
    full=copy.deepcopy(full);saved=full.get(smt().STATE_KEY)
    if not isinstance(saved,dict):return None
    ci=saved['data'].get('chara_info') or {}
    if ci.get('scenario_id')!=15:return None
    return viewer,full,saved,ci


def valid_turn(payload,st,ci):
    return isinstance(payload.get('current_turn'),int) and payload['current_turn']==impl.shown_turn(st,ci)


def success(data=None):
    return {'response_code':1,'data_headers':{'result_code':1},'data':{} if data is None else data}


def envelope(saved,ci,events=()):
    return success({'chara_info':copy.deepcopy(ci),'home_info':copy.deepcopy(saved['data'].get('home_info') or {}),
                    'unchecked_event_array':list(events)})


def save(viewer,full,response):
    smt()._remember_display(full,response)
    smt().state_store.save_state(viewer,full)
    return response


def handle_attach_advice(payload):
    got=career(payload)
    if got is None:return refuse()
    viewer,full,saved,ci=got;st=impl.state(full)
    if ci.get('playing_state')!=51 or not valid_turn(payload,st,ci):return refuse()
    if 'advice_info_array' not in payload:
        return refuse(102)
    try:impl.attach_advice(full,ci,payload.get('attach_advice_info_array'),payload.get('advice_info_array'))
    except (impl.Refused,KeyError,TypeError,ValueError):return refuse()
    wire,_=CE.serve_next(full,'attach_advice')
    response=envelope(saved,ci,[wire] if wire else [])
    response['data']['race_condition_array']=smt()._build_race_condition_array(ci,ci.get('turn'),saved['data'].get('race_history',[]))
    return save(viewer,full,response)


def handle_fusion_advice(payload):
    got=career(payload)
    if got is None:return refuse()
    _viewer,full,_saved,ci=got;st=impl.state(full)
    if ci.get('playing_state')!=51 or not valid_turn(payload,st,ci):return refuse()
    advices=payload.get('advice_info_array')
    try:
        ids={a['advice_id'] for a in advices}
        replacement={a.get('fusion_advice_id'):a['advice_id'] for a in advices if a.get('fusion_advice_id')}
        acts=copy.deepcopy(st['acts'])
        for a in acts:
            if a['advice_id'] not in ids:a['advice_id']=replacement.get(a['advice_id'],0)
        assignments=[{'act_type':a['act_type'],'advice_id':a['advice_id']} for a in acts]
        impl.validate_advice(st,assignments,advices,fusion=True)
    except (impl.Refused,KeyError,TypeError,ValueError):return refuse()
    return success({'festa_data_set_fusion':{'advice_info_array':copy.deepcopy(advices),'act_info_array':acts}})


def handle_check_point(payload):
    got=career(payload)
    if got is None:return refuse()
    viewer,full,saved,ci=got;st=impl.state(full)
    schedule=M.schedule(impl.shown_turn(st,ci))
    if ci.get('playing_state')!=49 or not valid_turn(payload,st,ci) or not schedule:return refuse()
    if any(r['schedule_id']==schedule['id'] for r in st['results']):return refuse()
    result=2 if sum(a['level']>=schedule['need_act_level'] for a in st['acts'])>=schedule['need_act_num'] else 1
    st['results'].append({'schedule_id':schedule['id'],'result_type':result})
    if schedule['id']==3:CE.emit(full,P.beat(215023,400015026,timing=25))
    CE.emit(full,P.result_event(full,ci,schedule,result))
    ci['playing_state']=5
    wire,_=CE.serve_next(full,'check_point')
    response=envelope(saved,ci,[wire] if wire else [])
    response['data']['race_condition_array']=smt()._build_race_condition_array(ci,ci.get('turn'),saved['data'].get('race_history',[]))
    return save(viewer,full,response)


def handle_festa_live(payload):
    got=career(payload)
    if got is None:return refuse()
    viewer,full,saved,ci=got;st=impl.state(full)
    if ci.get('playing_state')!=50 or not valid_turn(payload,st,ci):return refuse()
    st['hold_turn']=0;ci['playing_state']=1
    impl.refresh_home(full,ci)
    CE.emit(full,P.after_live())
    events=smt()._drain_pending(full,None,int(ci.get('card_id') or 0)//100)
    response=envelope(saved,ci,events)
    from ... import concerts
    music=concerts.grant(full,1195)
    seed_cloths=(((full.get('load_index') or {}).get('data') or {}).get('cloth_list') or [])
    cloths=full.setdefault('cloth_list_state',copy.deepcopy(seed_cloths))
    added=[]
    if not any(c.get('cloth_id')==63 for c in cloths):
        cloths.append({'cloth_id':63});added=[{'cloth_id':63}]
    response['data']['live_reward']={'add_music':music,'add_cloth_array':added}
    return save(viewer,full,response)


def handle_auto(payload,kind):
    got=career(payload)
    if got is None:return refuse()
    viewer,full,_saved,_ci=got;auto=impl.state(full)['auto']
    if kind=='call':
        pid=payload.get('auto_call_preset_id')
        if not isinstance(pid,int) or pid not in range(4):return refuse()
        auto['auto_call_preset_id']=pid
    else:
        values=payload.get('preset') if kind=='save' else payload
        if not isinstance(values,dict):return refuse()
        pid=values.get('preset_id');preset=next((p for p in auto['preset_array'] if p['preset_id']==pid),None)
        if preset is None:return refuse()
        if kind=='name':
            if not isinstance(payload.get('name'),str):return refuse()
            preset['name']=payload['name']
        else:
            for key in ('advice_info_array','auto_setting_attach_advice_info_array'):
                if key in values:preset[key]=copy.deepcopy(values[key])
    return save(viewer,full,success())


HANDLERS={'attach_advice':handle_attach_advice,'fusion_advice':handle_fusion_advice,
          'check_point':handle_check_point,'festa_live':handle_festa_live,
          'change_auto_setting_auto_call':lambda p:handle_auto(p,'call'),
          'change_auto_setting_preset_name':lambda p:handle_auto(p,'name'),
          'save_auto_setting_preset':lambda p:handle_auto(p,'save')}
