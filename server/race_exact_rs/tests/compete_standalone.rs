#[path = "../src/compete.rs"]
mod compete;

#[test]
fn prototype_constants_match_serialized_global_assets() {
    let assets: serde_json::Value = serde_json::from_str(include_str!(
        "../../data/race_param_define_global.json")).unwrap();
    let fields=&assets["race_param_define"]["objects"][0]["fields"];
    let p=compete::Params::default();
    for (group,key,value) in [
        ("CompeteTop","CheckStartDistance",p.start_distance),
        ("CompeteTop","DistanceGap1",p.top_distance),
        ("CompeteTop","LaneGap1",p.top_lane),
        ("CompeteTop","DistanceGap2",p.top_exit_distance),
        ("CompeteTop","LaneGap2",p.top_exit_lane),
        ("CompeteFight","DistanceGap",p.fight_distance),
        ("CompeteFight","LaneGap",p.fight_lane),
        ("CompeteFight","TargetContinueTime",p.fight_time),
        ("CompeteFight","SpeedGap",p.fight_speed_gap),
        ("CompeteFight","TargetContinueDistance",p.fight_exit_distance),
    ] {
        let actual=fields[group][key]["decimal"].as_f64().unwrap() as f32;
        assert_eq!(actual.to_bits(),value.to_bits(),"{group}.{key}");
    }
    for (group,prefix,values) in [
        ("CompeteTop","Time",p.top_time),
        ("CompeteTop","AddParam1",p.top_speed),
        ("CompeteFight","AddParam1",p.fight_speed),
        ("CompeteFight","AddParam2",p.fight_accel),
    ] {
        for (i,value) in values.into_iter().enumerate() {
            let key=format!("{prefix}Coef{}",i+1);
            assert_eq!((fields[group][&key]["decimal"].as_f64().unwrap() as f32).to_bits(),value.to_bits(),"{group}.{key}");
        }
    }
}
