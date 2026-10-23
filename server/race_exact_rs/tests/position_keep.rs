use race_exact_rs::{data::Tree, position_keep::{Keep, Runner}, rng::RaceRandom};
use serde_json::json;

fn parameters() -> serde_json::Value {
    json!({"PositionKeepStartSection":1,"PositionKeepEndSection":10,
        "PositionKeepCoolDownSec":1,"PositionKeepCheckIntervalSec":2,
        "PositionKeepContinueSection":1,"PositionKeepContinueSectionExArray":[3],
        "PositionKeepPaseBaseDistance":1000,"PositionKeepPaseBaseDistanceCoef":0.0008,
        "PositionKeepPaseUpDownDistanceDiffMinSenko":3,"PositionKeepPaseUpDownDistanceDiffMaxSenko":5,
        "PositionKeepPaseUpDownDistanceDiffMaxCoef":0.5,
        "PositionKeepPaseDownBaseTargetSpeedMultiply":0.915,
        "PositionKeepPaseDownBaseTargetSpeedMultiplyPhaseMiddle":0.945,
        "PositionKeepSpeedUpEndDistanceDiff":4.5,
        "PositionKeepSpeedUpEndDistanceDiffOne":12.5,
        "PositionKeepSpeedUpStartPerVal1":20,
        "PositionKeepSpeedUpBaseTargetSpeedMultiply":1.04})
}

#[test]
fn pace_down_draws_once_and_ends_on_target_gap() {
    let value = parameters();
    let p = Tree(&value);
    let horse = Runner {distance:0.0,order:0,style:2,ex:false,phase:0,wiz:500.0,temptation:0,speed_skill:false};
    let mut runners = [horse, Runner {order:1,..horse}];
    let mut state = Keep::default();
    let mut random = RaceRandom::new(3,false);
    state.update(&p,1200,24,&runners,1,0,&mut random,0.0666,true);
    assert_eq!(state.mode,4);
    assert_eq!(state.count,1);
    assert_eq!(random.calls(),1);
    state.update(&p,1200,24,&runners,1,0,&mut random,0.0666,true);
    assert_eq!(random.calls(),1);
    runners[0].distance = 6.0;
    state.update(&p,1200,24,&runners,1,0,&mut random,0.0666,true);
    assert_eq!(state.mode,0);
    state.update(&p,1200,24,&runners,1,0,&mut random,0.0666,true);
    assert_eq!(random.calls(),1);
}

#[test]
fn section_boundaries_use_native_integer_division() {
    let value = parameters();
    let p = Tree(&value);
    assert!(Keep::in_section(&p,1250,24,519.9));
    assert!(!Keep::in_section(&p,1250,24,520.0));
}

#[test]
fn lone_front_runner_threshold_counts_its_strategy_only() {
    let value = parameters();
    let p = Tree(&value);
    let first = Runner {distance:8.0,order:0,style:1,ex:false,phase:0,
        wiz:1_000_000.0,temptation:0,speed_skill:false};
    let mut runners = [first, Runner {distance:0.0,order:1,style:2,..first}];
    let mut state = Keep::default();
    let mut random = RaceRandom::new(3,false);
    state.update(&p,1200,24,&runners,0,0,&mut random,0.0666,true);
    assert_eq!(state.mode,1);
    runners[1].style = 1;
    let mut state = Keep::default();
    state.update(&p,1200,24,&runners,0,0,&mut random,0.0666,true);
    assert_eq!(state.mode,0);
}
