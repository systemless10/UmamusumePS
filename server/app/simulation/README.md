# Real race simulation

Produces genuine `practice_race/race_start` results for *any* uma (not just
the 4 we have real captures for), using:

- `uma-tools` (`C:\Users\Systemless\Documents\uma-tools-master\uma-skill-tools`,
  a community-verified TypeScript race physics engine) via a Node.js
  subprocess, for the actual position/speed/HP/skill simulation.
- Our own encoder for `race_scenario`, the proprietary gzip+base64 binary
  blob the real client expects for race animation. Cracked empirically by
  cross-referencing the IL2CPP dump's field layout (`RaceSimulateData`,
  `RaceSimulateHorseFrameData`, `RaceSimulateHorseResultData` in
  `Career Dump Plugin/il2cppdumper/dump.cs`) against real captured blobs, and
  validated against a known-good partial decoder already sitting in
  `Icarus-Dev-Build-Private-main/career_bot/dailies.py`
  (`parse_race_result_array`). See `scenario_format.py` for the full layout
  and how it was verified (finish-order permutation check, monotonic
  distance-over-time check, physics-constant match on start-dash speed/accel).

Known gap: skill-activation event markers (the ~1.4KB trailing section in
real blobs) are not yet decoded/encoded, so skill procs won't show their
visual burst effect. The race itself (positions, overtakes, finish order)
should render correctly without it.
