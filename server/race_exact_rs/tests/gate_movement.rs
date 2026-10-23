use race_exact_rs::math::gate_movement;

#[test]
fn global_server_gate_exit_rounding_matches_replay_samples() {
    let dt = 0.0666f32;
    for (speed, delay, expected) in [
        (0x4093d3aa, 0x3d8543a7, 0x3be7895b),
        (0x4093b225, 0x3d81d9a7, 0x3c71b9fa),
        (0x4093cddc, 0x3d4ff3d5, 0x3d95bf28),
    ] {
        let movement = f32::from_bits(speed) * dt;
        let remainder = f32::from_bits(delay) - dt;
        assert_eq!(gate_movement(movement, remainder, dt).to_bits(), expected);
        assert_ne!((movement * (remainder.abs() / dt)).to_bits(), expected);
    }
}
