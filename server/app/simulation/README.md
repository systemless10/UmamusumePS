# Race simulation and replay encoding

`race_simulator.py` runs the native Rust race engine with a Python fallback.
Both engines accept the same course, horse, condition, gate and seed inputs.
The server supplies completed outcomes and gzip/base64 replays to the client.

`scenario_format.py` encodes the captured `RaceSimulateData` framing: timestamped
horse frames, result records, and length-prefixed skill/race events. Events refer
to the same gate-ordered horse indices as the frames and results. Races without
events carry a reserved field and zero event count; they never borrow events from
another race.

The layout was reconstructed from client declarations and captured blobs.
Decoding a replay validates its framing and indexing, not complete physics or
client-rendering parity. See [Team Trials implementation](../../../docs/TEAM_TRIALS_IMPLEMENTATION.md)
for regional checks and remaining research boundaries.

## Global 1:1 test simulator

In the Global admin panel, open **Career tuning → Race simulator** and choose
**Existing simulator** or **1:1 test (unfinished)**, then Save. The setting is
server-wide and applies to newly simulated races immediately. Existing is the
default; Reset all restores it. The selector is absent in JP.

Build the experimental binary before selecting it:

```sh
cargo build --release --manifest-path server/race_exact_rs/Cargo.toml --offline
```

The test adapter uses the current Global master database and
`server/data/race_param_define_global.json`, with no captured replay outcomes.
`UMA_RACE_EXACT_RS_EXE` can override the binary path. The saved config key is
`race_simulator` (`existing` or `exact_test`). Missing binaries, timeouts,
unsupported fields (outside 3–18 runners), and engine failures log a warning and
fall back to the existing Rust/Python simulator. JP always uses the existing
simulator, even if the experimental value is hand-edited into its config.

This engine is unfinished and does not establish 1:1 parity. Its native display
times and ranks are preserved, with limitations in `exactDiagnostics`; existing
server scoring and replay encoding remain in use. A generated race without an
instance uses a course-matching master race for grade conditions. Room matches, Team Trials, main story, daily races and Unity Cup supply their
race types; other callers default to career (6) or practice (9) unless overridden
in race context.
