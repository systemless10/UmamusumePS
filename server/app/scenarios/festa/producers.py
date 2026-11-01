from __future__ import annotations
from ... import career_events as CE, event_engine
from . import impl, master as M

STARTS={13:(215016,400015004),37:(215017,400015014),61:(215018,400015021)}
ENDS={16:(215045,400015006),40:(215046,400015016),64:(215047,400015023)}
STORIES={15:(215031,400015005),20:(215032,400015007),25:(215033,400015011),32:(215034,400015012),
         34:(215035,400015013),39:(215036,400015015),46:(215037,400015017),59:(215038,400015020),62:(215039,400015022)}
OPENS={24:(215007,400015008),48:(215008,400015018),68:(215009,400015025)}
TRAINER_STORIES={1009:zip((34,55,62,63),range(215049,215053),range(400015300,400015304)),
                 1024:zip((17,30,54,66),range(215057,215061),range(400015308,400015312))}
TRAINER_STORIES={k:list(v) for k,v in TRAINER_STORIES.items()}


def effect(kind,value,**kw):
    return {'type':kind,'value':f'{value:+d}',**kw}


def beat(eid,sid,n=1,*,timing=1,resolver=None,payload=None,choices=None,priority=CE.PRIO_SCENARIO,once=None):
    return CE.Event(event_id=eid,story_id=sid,choices=[CE.Choice() for _ in range(n)] if choices is None else choices,
                    play_timing=timing,resolver=resolver,payload=payload or {},priority=priority,
                    once_key=once or f'festa:{eid}',source='festa')


def offer(st,phase):
    ids={1:(101,201,301,401,501),2:(601,701),3:(801,901,1001)}.get(phase,())
    owned={a['advice_id'] for a in st['advices']}
    st['advices'] += [{'advice_id':a,'fusion_advice_id':0} for a in ids if a not in owned]
    st['phase']=max(st['phase'],phase)


@CE.resolver('festa_assign')
def assign(full_state,ci,event,choice_number,**kw):
    st=impl.state(full_state)
    offer(st,event.payload.get('phase',1))
    impl.seed_roles(st,ci)
    ci['playing_state']=51
    st['hold_turn']=event.payload.get('hold',0)
    return {}


@CE.resolver('festa_checkpoint_open')
def checkpoint_open(full_state,ci,event,choice_number,**kw):
    ci['playing_state']=49
    impl.state(full_state)['hold_turn']=event.payload['turn']
    return {}


@CE.resolver('festa_checkpoint_result')
def checkpoint_result(full_state,ci,event,choice_number,**kw):
    st=impl.state(full_state);cp=event.payload['schedule_id']
    if cp==1:
        st['step']=2
        CE.emit(full_state,beat(215044,400015010,timing=25,resolver='festa_assign',payload={'phase':2,'hold':24}))
    elif cp==2:
        for act in range(1,6):impl.add_member(st,ci,act)
        offer(st,3);ci['playing_state']=51
    else:
        ci['playing_state']=50
    return {}


@CE.resolver('festa_camp_end')
def camp_end(full_state,ci,event,choice_number,**kw):
    st=impl.state(full_state)
    capped=all(a['level']>=impl.level_cap(ci) for a in st['acts'])
    for act in st['acts']:impl.gain_level(st,ci,act)
    if capped:st['flags'].setdefault('not_up',impl.empty_not_up())['is_overflow_act_level_event_gain']=True
    return {}


@CE.resolver('festa_gauge')
def gauge(full_state,ci,event,choice_number,**kw):
    impl.add_gauge(impl.state(full_state),event.payload['gains'],event=True)
    return {}


def result_event(full_state,ci,schedule,result):
    cp=schedule['id'];great=result==2
    eid,sid = ((215010 if great else 215011,400015009) if cp==1 else
               (215012,400015019) if cp==2 else (215014,400015027) if great else (215015,400015028))
    stats,sp,fans = {(1,True):(5,200,1000),(1,False):(3,100,500),
                     (2,True):(10,300,20000),(2,False):(5,150,2000),
                     (3,True):(15,400,45000),(3,False):(10,200,5000)}[(cp,great)]
    effects=[effect('all_stats',stats),effect('skill_point',sp),effect('fans',fans)]
    if great and cp<3:effects.append(effect('npc_bond',15,target_id=102))
    if cp==2 and great:effects.append(effect('skill_hint',1,skill_id=200511))
    if cp==3:
        effects += [effect('energy',30),effect('motivation',1),effect('skill_hint',3 if great else 1,skill_id=210402)]
        if great:effects += [effect('skill_hint',2,skill_id=200361),effect('skill_hint',2,skill_id=202801)]
        if not great:effects.append(effect('skill_hint',1,skill_id=202802))
    return beat(eid,sid,timing=25,choices=[CE.Choice(effects)],resolver='festa_checkpoint_result',payload={'schedule_id':cp})


LINK_HINTS=((1009,200532,200531),(1024,204072,204071),(1096,204052,204051),
            (1128,203742,203741),(1029,201591,201592),(9052,204812,204811))


def link_choices(ci):
    from .formula import F
    linked={int(ci.get('card_id',0))//100}|{F.card_meta(s)[3] for s in F.deck(ci).values()}
    return [CE.Choice([effect('skill_hint',2,skill_id=gold if chara in linked else white)])
            for chara,white,gold in LINK_HINTS]


@CE.producer('festa_calendar',priority=CE.PRIO_SCENARIO,scenario=15)
def calendar(ctx):
    st=impl.state(ctx.full_state);played,arrived=int(ctx.turn),int(ctx.advanced_turn)
    out=[]
    if arrived==2:out.append(beat(215030,400015000))
    if arrived==3:
        out += [beat(215001,400015001),beat(215073,400015003,resolver='festa_assign',payload={'phase':1})]
    if arrived in (11,35,59):out.append(beat({11:215024,35:215025,59:215026}[arrived],400015104,n=0))
    if played in (12,36,60):
        out.append(beat({12:215002,36:215003,60:215004}[played],400015105,timing=6,
                        resolver='festa_assign',payload={'phase':st['phase'] or 1,'hold':played}))
    if arrived in STARTS:out.append(beat(*STARTS[arrived]))
    if played in ENDS:out.append(beat(*ENDS[played],timing=6,resolver='festa_camp_end'))
    if played in ENDS and st.get('camp_extensions',{}).get(str({16:13,40:37,64:61}[played]),0):
        out.append(beat(215074,400015108,timing=6,once=f'festa:camp-extension:{played}'))
    if played in STORIES:out.append(beat(*STORIES[played],n=0,timing=6))
    for t,eid,sid in TRAINER_STORIES.get(ctx.player_chara,[]):
        if played==t:out.append(beat(eid,sid,timing=6))
    if played==67:out.append(beat(215029,400015024,timing=3,choices=link_choices(ctx.chara_info)))
    if played in OPENS and not any(r['schedule_id']==M.schedule(played)['id'] for r in st['results']):
        out.append(beat(*OPENS[played],timing=6,resolver='festa_checkpoint_open',payload={'turn':played},priority=CE.PRIO_TRAILING))
    due=st.get('gauge_due')
    if due:
        st['gauge_due']=None
        eid={'rest':215020,'outing':215021,'race':215022}[due['kind']]
        out.append(beat(eid,400015107,timing=3 if due['kind']=='race' else 6,resolver='festa_gauge',
                        payload={'gains':due['gains']},once=f'festa:gauge:{played}:{due["kind"]}'))
    return out


def after_live():
    return beat(215041,400015029,n=0,priority=CE.PRIO_TRAILING)


def ending(full_state,ci):
    st=impl.state(full_state);great=bool(st['results']) and st['results'][-1]['result_type']==2
    eid,sid=(215027,400015030) if great else (215028,400015031)
    effects=[effect('all_stats',30 if great else 15),effect('skill_point',350 if great else 150),
             effect('skill_hint',2 if great else 3,skill_id=210401 if great else 210402)]
    return beat(eid,sid,timing=3,choices=[CE.Choice(effects)],priority=CE.PRIO_FRONT)
