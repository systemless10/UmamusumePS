use race_exact_rs::modifiers::{Modifiers,Operation::*};
#[test]
fn sets_overwrite_earlier_adds_and_one_time_values_are_consumed_separately() {
    let mut m=Modifiers::default();
    m.add(6,2.0,Add,1,false);m.add(6,10.0,Set,2,false);m.add(6,3.0,Add,3,false);
    m.add(6,7.0,Add,4,true);
    assert_eq!(m.apply(6,20.0),13.0);
    assert_eq!(m.consume(6,0.0),7.0);assert_eq!(m.consume(6,0.0),0.0);
    m.stop(2);assert_eq!(m.apply(6,20.0),25.0);
}

#[test]
fn additional_modifier_uses_its_own_remaining_time_after_detail_stop() {
    let mut m=Modifiers::default();
    let dt=0.0666_f32;
    let mut detail_left=dt*2.0;
    m.add(6,0.25,Add,7,false);
    m.add_timed(6,0.025,Add,7,false,detail_left);
    detail_left-=dt;
    m.update_timed(dt);
    detail_left-=dt;
    assert!(detail_left<=0.0);
    m.stop(7);
    assert_eq!(m.apply(6,0.0),0.025);
    m.update_timed(dt);
    assert_eq!(m.apply(6,0.0),0.0);
}

#[test]
fn separately_added_modifiers_expire_from_copied_time_not_new_full_duration() {
    let mut m=Modifiers::default();
    m.add_timed(6,0.025,Add,7,false,0.2);
    m.update_timed(0.1);
    m.add_timed(6,0.025,Add,7,false,0.1);
    assert_eq!(m.apply(6,0.0),0.05);
    m.update_timed(0.1);
    assert_eq!(m.apply(6,0.0),0.0);
}
