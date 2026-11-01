from __future__ import annotations
import copy
from ..base import Scenario
from ..registry import register
from ... import career_events as CE, event_engine
from . import endpoints, formula as F, impl, master as M, producers as P


class Festa(Scenario):
    scenario_id=15
    name='Shuntaisai'
    endpoint_prefix='single_mode_festa'
    data_set_key='festa_data_set'
    data_set_on_race_start=True
    state_keys=(impl.STATE_KEY,)
    uses_fixed_schedule=False
    has_versus_npc=False
    has_director_ending=False
    debut_reporter_event=0
    camp_lists_rest=True
    playing_states=frozenset({5,49,50,51})
    turn_end_timings=(3,6,10,25)
    sp_appraisers=frozenset({102,116})
    LEVELUP_EVENTS={101:215801,102:215802,103:215803,105:215804,106:215805}

    def facility_levelup_event(self,command_id):
        cid=int(command_id or 0)
        return (self.LEVELUP_EVENTS.get(F.PRACTICE.get(cid,cid),215801),400000005)
    finals_events={
        1:{'pre':(101001,400000101,False),'post':(101002,400000104,12)},
        2:{'pre':(101003,400000102,False),'post':(101004,400000105,13)},
        3:{'pre':(101005,400000103,False),'post':(101006,400000106,14)}}

    def extra_endpoints(self):return dict(endpoints.HANDLERS)
    def base_command(self,cid):
        return F.PRACTICE.get(int(cid or 0),super().base_command(cid))
    def serves_camp_rest(self,turn):
        return any(a<=int(turn or 0)<=b for a,b in impl.CAMP_PERIODS)
    def held_turn(self,full_state):return impl.state(full_state)['hold_turn']
    def begin_hold(self,full_state,turn):impl.state(full_state)['hold_turn']=int(turn)
    def interstitial_pending(self,full_state,ci,turn):
        st=impl.state(full_state);schedule=M.schedule(turn)
        return bool(schedule) and not any(r['schedule_id']==schedule['id'] for r in st['results'])
    def holds_chain_after(self,event_id):
        return int(event_id or 0) in (215073,215002,215003,215004,215007,215008,215009,215012,215014,215015,215044)

    def home_commands(self,full_state,ci,turn,commands):
        from ...handlers import single_mode_team as smt
        st=impl.state(full_state);ds=impl.dataset(full_state,ci,False)
        camp=bool(impl.camp_remaining(st,ci));levels=smt._support_card_levels(full_state)
        facilities={r['command_id']:r['level'] for r in ci.get('training_level_info_array') or []}
        career_data=((full_state.get(smt.STATE_KEY) or {}).get('data') or {})
        for cid,lv in (career_data.get(smt.FACILITY_LEVELS_KEY) or {}).items():
            facilities.setdefault(int(cid),int(lv))
        out=[];extra=[]
        for command in commands:
            c=copy.deepcopy(command)
            if c.get('command_type')!=1:
                out+=extra;extra=[]
                if camp and c['command_id'] in (301,390):
                    if not any(r['command_id']==304 for r in out):
                        c['command_id']=304;out.append(c)
                    continue
                if camp and c['command_id'] in (701,801):c['is_enable']=0
                out.append(c);continue
            base=self.base_command(c['command_id'])
            if base not in F.NORMAL:out.append(c);continue
            act=st['acts'][F.NORMAL.index(base)]
            practice=bool(st['advices']) and (camp or turn>=73)
            if not practice and c['command_id'] in F.PRACTICE:
                if any(x.get('command_id')==base for x in commands):continue
            if bool(st['advices']) and not practice and act['act_gauge']>=5:
                extra.append(self._home_command(ci,ds,copy.deepcopy(c),5101+F.NORMAL.index(base),st['step'],levels))
            cid=5101+F.NORMAL.index(base) if practice else base
            lv=st['step'] if practice else facilities.get(base,c.get('level',1))
            out.append(self._home_command(ci,ds,c,cid,lv,levels))
        return out+extra

    @staticmethod
    def _home_command(ci,ds,c,cid,lv,levels):
        c['command_id']=cid;c['level']=lv
        gains=F.home_gains(ci,cid,lv,c.get('training_partner_array') or [],levels)
        c['params_inc_dec_info_array']=[{'target_type':i,'value':gains[s]} for i,s in enumerate(F.F.STATS,1) if gains.get(s)]
        c['params_inc_dec_info_array'] += [{'target_type':30,'value':gains.get('skill_point',0)},
                                         {'target_type':10,'value':gains['energy_cost']}]
        c['failure_rate']=F.failure(ci,ds,c,levels) if c.get('is_enable') else 0
        return c

    def training_gains(self,full_state,ci,cid,level,camp,partners):
        base=self.base_command(cid)
        return F.home_gains(ci,base,min(5,level),partners) if base in F.NORMAL else None

    def training_failure_override(self,full_state,ci,cid,partners):
        if cid not in F.PRACTICE:return None
        st=impl.state(full_state)
        return F.failure(ci,impl.dataset(full_state,ci,False),{'command_id':cid,'level':st['step'],'training_partner_array':partners})

    def command_info(self,full_state,ci,commands):
        from ...handlers import single_mode_team as smt
        changed=False
        if isinstance(commands,list) and ci.get('playing_state')==1:
            current=self.home_commands(full_state,ci,impl.turn(ci),commands)
            changed=current!=commands
            if changed:
                commands[:]=current
                saved=full_state.get(smt.STATE_KEY)
                if isinstance(saved,dict):
                    saved['data'].setdefault('home_info',{})['command_info_array']=copy.deepcopy(current)
        rolled=impl.refresh_preview(full_state,ci,commands,smt._support_card_levels(full_state))
        return None,changed or rolled or bool(impl.state(full_state)['flags'])

    def attach(self,response,full_state,ci,command_info_array=None,endpoint=''):
        data=(response or {}).get('data')
        if not isinstance(data,dict):return response
        call=endpoint.rsplit('/',1)[-1]
        if call in ('fusion_advice','get_choice_reward','finish','factor_select','factor_order_load','factor_lottery','factor_reload',
                    'change_auto_setting_auto_call','change_auto_setting_preset_name','save_auto_setting_preset','save_skill_filter_set'):
            return response
        full=call in ('start','load','gain_skills') or (call in ('exec_command','check_event','race_out','attach_advice')
             and not data.get('unchecked_event_array') and ci.get('playing_state')==1)
        data['festa_data_set']=impl.dataset(full_state,ci,full)
        for holder in (data,data.get('single_mode_load_common'),data.get('single_mode_start_common')):
            nested=(holder or {}).get('chara_info')
            if isinstance(nested,dict):
                nested['training_level_info_array']=copy.deepcopy(ci['training_level_info_array'])
        impl.state(full_state)['flags'].pop('not_up',None)
        if call=='load':
            hold=impl.state(full_state)['hold_turn']
            nested=(data.get('single_mode_load_common') or {}).get('chara_info')
            if hold and isinstance(nested,dict):
                nested['turn']=hold
                nested['playing_state']=ci.get('playing_state',nested.get('playing_state',1))
            nested_home=(data.get('single_mode_load_common') or {}).get('home_info')
            board=command_info_array or (data.get('home_info') or {}).get('command_info_array')
            if isinstance(nested_home,dict) and board:
                nested_home['command_info_array']=copy.deepcopy(board)
        if call=='start' and not data['festa_data_set']['command_info_array']:
            data['festa_data_set']['command_info_array']=[{'command_type':1,'command_id':c,'params_inc_dec_info_array':[],
                'predict_gain_act_gauge_array':[],'is_upper_training_event_occurred':False} for c in F.NORMAL]
        return response

    def training_award_bonus(self,full_state,ci,cid):
        return copy.deepcopy((impl.state(full_state)['preview'].get(str(cid)) or {}).get('params_inc_dec_info_array') or [])

    def award_training_gains(self,full_state,ci,payload):
        st=impl.state(full_state);cid=payload.get('command_id');t=payload.get('current_turn',impl.turn(ci))
        if payload.get('command_type')==1:
            preview=st['preview'].get(str(cid)) or {}
            if cid in F.PRACTICE:
                act=st['acts'][list(F.PRACTICE).index(cid)]
                if not impl.camp_remaining(st,dict(ci,turn=t)):
                    act['act_gauge']=0;impl.gain_level(st,ci,act)
            else:impl.add_gauge(st,preview.get('predict_gain_act_gauge_array') or [])
            if preview.get('is_upper_training_event_occurred'):
                base=self.base_command(cid);stat=F.F.STATS[F.NORMAL.index(base)]
                effects=[P.effect('energy',-5),P.effect(stat,20),P.effect('skill_point',20),P.effect('npc_bond',5,target_id=102)]
                ev=P.beat(215019,400015106,timing=6,choices=[CE.Choice(effects),CE.Choice([P.effect('energy',5),P.effect(stat,5)])],
                          once=f'festa:extra:{t}',resolver='festa_extra',payload={'act_type':F.NORMAL.index(base)+1})
                CE.emit(full_state,ev)
        elif st['advices'] and payload.get('command_type') in (3,7):
            st['gauge_due']={'kind':'rest' if payload['command_type']==7 else 'outing','gains':impl.nontraining_gauge(st,ci,include_capped=True)}

    def goal_race_reward(self,full_state,ci,reward,finals_round):
        if finals_round:return reward
        stats,sp=reward['stats']*2,reward['sp']*18//10
        cp3=next((r for r in impl.state(full_state)['results'] if r.get('schedule_id')==3),None)
        if cp3:
            bonus=100 if cp3.get('result_type')==2 else 30
            stats,sp=stats*(100+bonus)//100,sp*(100+bonus)//100
        return {**reward,'stats':stats,'sp':sp}

    def on_race_result(self,full_state,ci,race_ctx,result_rank):
        st=impl.state(full_state)
        t=int(race_ctx.get('turn') or impl.turn(ci))
        if race_ctx.get('is_goal'):
            for start,end in impl.CAMP_PERIODS:
                if start<=t<=end:
                    extensions=st.setdefault('camp_extensions',{})
                    extensions[str(start)]=extensions.get(str(start),0)+1
        if st['advices'] and not race_ctx.get('finals_round'):
            gains=impl.nontraining_gauge(st,ci)
            if race_ctx.get('is_goal'):
                for g in gains:g['gain_act_gauge']=min(2,5-next(a['act_gauge'] for a in st['acts'] if a['act_type']==g['act_type']))
            st['gauge_due']={'kind':'race','gains':gains}

    def appraisal_events(self):
        from ...handlers import single_mode_events
        return {**single_mode_events.APPRAISAL_EVENTS,
                116:[(90,215072,5),(70,215071,4),(40,215070,3),(0,215069,2)]}
    def npc_training_partners(self,ci):return frozenset({116})
    def npc_supporters(self,full_state,ci):
        return [int(t) for t in impl.state(full_state)['roles'] if int(t)>999]

    def hint_partners(self,full_state,ci,cid,partners,hinting):
        base=self.base_command(cid)
        if base not in F.NORMAL:return hinting
        st=impl.state(full_state);act=st['acts'][F.NORMAL.index(base)]
        practice=cid in F.PRACTICE or impl.camp_remaining(st,ci) or act['act_gauge']>=5 or impl.turn(ci)>=73
        if not practice or act['level']<4 or F.F.deck_type_count(ci)<5:return hinting
        deck=F.F.deck(ci)
        return sorted(set(hinting)|{p for p in partners if p in deck and F.F.card_meta(deck[p])[0]==1})

    def partner_placements(self,full_state,ci,turn,occupied=None,standing=None):
        st=impl.state(full_state);deck=F.F.deck(ci);standing=standing or {};out={}
        additions=[]
        for act in st['acts']:
            members=[p for p,sid in deck.items() if st['roles'].get(str(p))==act['act_type'] and F.F.card_meta(sid)[0]==1]
            if act['advice_id']==801:additions.extend(members)
            advice=next((a for a in st['advices'] if a['advice_id']==act['advice_id']),{})
            if impl.camp_remaining(st,ci) and advice.get('fusion_advice_id')==101 and members:
                additions.append(impl.rng(st,'fusion-copy',turn,act['act_type']).choice(members))
        for index,partner in enumerate(additions):
            choices=[c for c in F.NORMAL if partner not in standing.get(c,[]) and partner not in out.get(c,[])
                     and (occupied or {}).get(c,0)+len(out.get(c,[]))<5]
            if choices:
                cid=impl.rng(st,'cooperation-copy',turn,partner,index).choice(choices)
                out.setdefault(cid,[]).append(partner)
        return out

    def hint_reveals(self,full_state,ci,cid,reveals):
        st=impl.state(full_state)
        if cid not in F.PRACTICE or st['acts'][list(F.PRACTICE).index(cid)]['level']<4 or F.F.deck_type_count(ci)<5:
            return reveals
        if not reveals:return reveals
        effects=[effect for _entry,items in reveals for effect in items]
        return [(P.beat(215043,400015110,timing=6).wire_entry(),effects)]

    def final_partners(self,full_state,ci,cid,turn,partners):
        st=impl.state(full_state);base=self.base_command(cid)
        if base not in F.NORMAL or not st['advices']:return partners
        act=st['acts'][F.NORMAL.index(base)]
        practice=bool(impl.camp_remaining(st,ci)) or act['act_gauge']>=5 or turn>=73
        if practice:
            matching=[int(tid) for tid,role in st['roles'].items() if role==act['act_type']]
            partners=list(dict.fromkeys(matching+list(partners)))[:5]
        if turn<73:return partners
        candidates=[p for p,sid in F.F.deck(ci).items() if p not in partners and F.F.card_meta(sid)[0]==1]
        if not candidates:return partners
        extra=impl.rng(impl.state(full_state),'final-partner',turn,cid).choice(candidates)
        return (list(partners)+[extra])[-5:]

    def specialty_bonus(self,full_state):
        results=impl.state(full_state)['results']
        if not results:return 0
        r=results[-1]
        return sum(e['effect_value_4'] for e in M.rows('single_mode_15_check_point_effect')
                   if e['schedule_id']==r['schedule_id'] and e['result_state']==r['result_type'] and e['effect_type']==2)

    def choice_reward_preview(self,full_state,ci,event_id):
        from ...handlers import single_mode_team as smt
        ctx=smt._career_ctx_for(full_state,event_id) or {}
        if ctx.get('source_id') in (30323,10149):
            step=int(ctx.get('story_id') or 0)-809052010
            if any(r['support_card_id']==ctx['source_id'] and r['story_step']==step
                   for r in M.rows('single_mode_15_outing_effect')):
                native,_=smt._career_event_from_ctx(full_state,event_id)
                if native:
                    result=event_engine.choice_reward_array(native,ci,default_char_id=9052,source_card_id=ctx['source_id'])
                    for row in result:
                        row['gain_param_array'].append({'display_id':61,'effect_value_0':0,'effect_value_1':0,'effect_value_2':0})
                    return result
        ev=CE.find(full_state,event_id)
        return event_engine.choice_reward_array(ev.engine_event(),ci) if ev and ev.source=='festa' and ev.choices else None

    def on_event_resolved(self,full_state,ci,event_id):
        if int(event_id or 0) in (215030,215031):
            from ...handlers import single_mode_team as smt
            deck_charas={F.F.card_meta(s)[3] for s in F.F.deck(ci).values()}
            npcs=((102,9002),(116,9052)) if int(event_id)==215030 else ((103,9003),)
            unlocked=full_state.setdefault(smt.single_mode_events.UNLOCKED_NPCS_KEY,[])
            for tid,chara in npcs:
                if chara not in deck_charas and [tid,chara] not in unlocked:
                    unlocked.append([tid,chara]);smt._ensure_npc_eval(ci,tid)
                    next(e for e in ci['evaluation_info_array'] if e['target_id']==tid)['is_appear']=1
            impl.refresh_home(full_state,ci)
        if int(event_id or 0) in (2,10) and impl.turn(ci)>=78:
            CE.emit(full_state,P.ending(full_state,ci))
        return {}

    def training_failure_event(self,full_state,ci,cid,worst):
        if cid not in F.PRACTICE:return None
        first=impl.state(full_state)['step']==1
        eid=(215083 if first else 215079) if worst else (215082 if first else 215078)
        if not worst and ci.get('vital',100)>=40:
            eid=215081 if first else 215077
        return eid,400015109,1

    def settle(self,full_state,ci,endpoint=''):
        st=impl.state(full_state);changed=False;deck=F.F.deck(ci)
        for row in ci.get('evaluation_info_array') or []:
            sid=deck.get(row['target_id']);step=int(row.get('story_step') or 0)
            if sid not in (10149,30323):continue
            seen=st['link_steps'].get(str(sid),0)
            for n in range(seen+1,step+1):
                if any(r['support_card_id']==sid and r['story_step']==n for r in M.rows('single_mode_15_outing_effect')):
                    minimum=min(a['level'] for a in st['acts'])
                    act=impl.rng(st,'outing',sid,n).choice([a for a in st['acts'] if a['level']==minimum])
                    impl.gain_level(st,ci,act);changed=True
            st['link_steps'][str(sid)]=step
        return changed

    def snapshot(self,full_state):return {'festa_results':copy.deepcopy(impl.state(full_state)['results'])}


@CE.resolver('festa_extra')
def _extra(full_state,ci,event,choice_number,**kw):
    if choice_number==1:impl.add_gauge(impl.state(full_state),[{'act_type':event.payload['act_type'],'gain_act_gauge':1}],event=True)
    return {}


register(Festa())
