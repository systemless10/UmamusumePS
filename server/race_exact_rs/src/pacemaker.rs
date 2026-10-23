
#[derive(Clone, Copy)]
pub struct Runner {
    pub style: i32,
    pub order: usize,
    pub distance: f32,
    pub finished: bool,
}

#[derive(Clone, Copy)]
pub struct Rules {
    pub early_forward: bool,
    pub reset_on_forward: bool,
    pub reset_on_recalc: bool,
    pub order_first: bool,
    pub forward_nige_only: bool,
}

impl Default for Rules {
    fn default() -> Self {
        Self { early_forward: false, reset_on_forward: true, reset_on_recalc: false, order_first: false, forward_nige_only: false }
    }
}

pub fn recalculate(runners: &[Runner], count: &mut i32, threshold: i32,
                   forward_range: f32) -> Option<usize> {
    recalculate_with(runners, count, threshold, forward_range, Rules::default())
}

pub fn recalculate_with(runners: &[Runner], count: &mut i32, threshold: i32,
                   forward_range: f32, rules: Rules) -> Option<usize> {
    let forward = runners.iter().filter(|h| !h.finished).map(|h| h.style).min()?;
    let leader = runners.iter().enumerate().filter(|(_, h)| !h.finished)
        .min_by_key(|(i, h)| (h.order, *i)).map(|(i, _)| i)?;
    if runners[leader].style == forward {
        if rules.reset_on_forward { *count = 0; }
        return None;
    }
    *count = count.saturating_add(1);
    let below = *count < threshold;
    if below && !rules.early_forward { return None; }
    if !below && rules.reset_on_recalc { *count = 0; }
    let pick = |i: usize| if below && (runners[i].style != forward || rules.forward_nige_only && forward > 1) { None } else { Some(i) };
    if runners[leader].style < 3 { return pick(leader); }
    let lead = runners[leader];
    let by_order = rules.order_first || std::env::var("RX_PMLONE").map_or(true, |v| v != "0")
        && runners.iter().filter(|h| !h.finished && h.style == forward).count() == 1;
    Some(runners.iter().enumerate().filter(|(_, h)| !h.finished && h.style < lead.style
        && lead.distance - h.distance <= forward_range)
        .min_by_key(|(i, h)| if by_order { (0, h.order, *i) } else { (h.style, h.order, *i) }).map_or(leader, |(i, _)| i)).and_then(pick)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runner(style: i32, order: usize, distance: f32) -> Runner {
        Runner { style, order, distance, finished: false }
    }

    #[test]
    fn senko_promotion_survives_later_section_checks_and_resets_when_nige_leads() {
        let mut runners = [runner(1, 1, 99.0), runner(2, 0, 100.0)];
        let mut count = 0;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), None);
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(1));
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(1));
        runners[0].order = 0;
        runners[1].order = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), None);
        assert_eq!(count, 0);
    }

    #[test]
    fn cumulative_count_survives_forward_led_checks() {
        let global = Rules { reset_on_forward: false, ..Rules::default() };
        let mut runners = [runner(1, 1, 99.0), runner(2, 0, 100.0)];
        let mut count = 0;
        assert_eq!(recalculate_with(&runners, &mut count, 2, 10.0, global), None);
        runners[0].order = 0; runners[1].order = 1;
        assert_eq!(recalculate_with(&runners, &mut count, 2, 10.0, global), None);
        assert_eq!(count, 1);
        runners[0].order = 1; runners[1].order = 0;
        assert_eq!(recalculate_with(&runners, &mut count, 2, 10.0, global), Some(1));
    }

    #[test]
    fn late_leader_prefers_most_forward_style_within_inclusive_range() {
        let runners = [runner(3, 0, 100.0), runner(2, 1, 99.0), runner(1, 2, 90.0), runner(1, 3, 50.0)];
        let mut count = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(2));
        let mut outside = runners;
        outside[2].distance = f32::from_bits(90.0f32.to_bits() - 1);
        assert_eq!(recalculate(&outside, &mut count, 2, 10.0), Some(1));
    }

    #[test]
    fn lone_most_forward_style_yields_to_frontmost_eligible_runner() {
        let runners = [runner(3, 0, 100.0), runner(2, 1, 99.6), runner(1, 2, 99.2)];
        let mut count = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(1));
    }

    #[test]
    fn no_front_lobby_preserves_nearby_senko_for_a_late_leader() {
        let runners = [runner(3, 0, 100.0), runner(2, 1, 98.0)];
        let mut count = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(1));
    }

    #[test]
    fn distant_forward_styles_allow_end_closer_to_be_pacemaker() {
        let runners = [runner(4, 0, 100.0), runner(1, 1, 80.0), runner(2, 2, 79.0)];
        let mut count = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(0));
    }

    #[test]
    fn finished_runners_are_excluded_and_oonige_has_priority() {
        let mut runners = [runner(3, 0, 100.0), runner(1, 1, 98.0), runner(0, 2, 97.0), runner(0, 3, 50.0)];
        let mut count = 1;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(2));
        runners[2].finished = true;
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), Some(1));
        for h in &mut runners { h.finished = true; }
        assert_eq!(recalculate(&runners, &mut count, 2, 10.0), None);
    }

    #[test]
    fn global_rules_reset_on_recalc_and_pick_frontmost_by_order() {
        let rules = Rules { reset_on_forward: false, reset_on_recalc: true, order_first: true, ..Rules::default() };
        let runners = [runner(4, 0, 351.2), runner(3, 1, 346.2), runner(2, 2, 345.0), runner(2, 5, 342.0)];
        let mut count = 1;
        assert_eq!(recalculate_with(&runners, &mut count, 2, 10.0, rules), Some(1));
        assert_eq!(count, 0);
        assert_eq!(recalculate_with(&runners, &mut count, 2, 10.0, rules), None);
    }
}
