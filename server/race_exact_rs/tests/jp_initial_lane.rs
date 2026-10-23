use race_exact_rs::simulator::initial_lane;

#[test]
fn niigata_outside_lanes_match_real_team_replay() {
    let expected=[16499,17055,17611,18500,19055,19611,20166,20722,21277,21833,22388,22944];
    let lane_max=23500.0f32*0.0001;
    let actual:Vec<i32>=(0..12).map(|gate|(initial_lane(gate,1,false,lane_max,12,true)*10000.0) as i32).collect();
    assert_eq!(actual,expected);
}
