# Global Rust race simulator port

The expanded 247,851-race Global corpus is **not fully exact**. The current
distributed holdout reaches **1,027/1,038 exact replays**; the original regression
set remains **673/673 exact**. All 247,851 inputs pass the documented integrity
checks, which do not establish physics parity. A current full replay audit is
in progress. See [the October 29 report](../../docs/RACE_PARITY_2026-10-29.md) and
[the earlier expanded-corpus work](../../docs/RACE_EXPANDED_CORPUS_2026-10-28.md).
The first 9,568 full-audit rows are 9,464 exact. A subsequent homogeneous-field
pacemaker correction makes one of their 104 failures exact; a later counterexample
narrows the correction to the opening section selection. Both counterexample races
are exact, and a 208-race focused rerun remains 105/208 exact with no changed exact
identities or moved first divergences. The full audit predates that correction and
remains incomplete.
Earlier complete-parity claims below refer only to their named capture sets.

JP parity work has resumed using the same crate with explicitly labeled JP
inputs and assets. See [the JP restart report](../../docs/RACE_JP_RESTART_2026-10-09.md)
for the 34,480-race capture inventory, current baseline, rejected opening probes
and reproducible commands. JP has no complete replay parity in the new baseline.

This crate generates independent race candidates from prepared inputs, Global
assets and the master bundle. It has a runnable tick loop and finish handling.
Global now uses this engine for live race simulation. General parity beyond
the named verified capture sets remains unverified. Every output reports
`diagnostics.parity_verified: false` and lists unsupported behavior;
`--require-exact` fails without writing a candidate.

The verified Global build matches **2,379/2,379 complete real replays** across
six disjoint captured sets: **673/673** original, **400/400** batch 1,
**300/300** lane, **400/400** holdout 1, **400/400** holdout 2 and
**206/206** crowded races. The last two failures came from stopping additional
skill-speed modifiers when their parent detail expired. Each added modifier now
copies the detail's remaining time and expires on its own receiver countdown.
This repairs both entire replays, with zero losses or earlier divergences.
See [the additional-effect expiry report](../../docs/RACE_ADDITIONAL_EXPIRY_2026-10-09.md)
for native support, regression tests and the full six-set audit.

The current build also includes the Global compete-bonus order correction and
the pacemaker correction for fields without front-runners.
`RX_GLOBAL_ADDITIONAL_TIME=0` restores parent-detail expiry;
`RX_CE_AFTER_SLOPE=0` restores adding the compete bonus before slopes;
`RX_PM_FORWARD_NIGE=0` restores the previous early pacemaker handover.
These corrections are Global-only. The shared release executable has been rebuilt.
The adjusted-stat field store correction also repairs both remaining career
races (`1fd80833` and `3f359e91`). Course/aptitude multiplication and the ground
and career additions now stay in double until the adjusted field is stored;
the motivation-adjusted base remains float. The complete audit matches
**143/143 career replays** and preserves all **2,379/2,379** earlier exact replays.
Six career inputs explicitly select their previously recovered seeds; these are
not new simulator repairs, and `gl_prepare` still defaults to the logged seed.
`RX_GLOBAL_ADJUSTED_STAT=0` restores the previous adjustment rounding; JP keeps
its prior float arithmetic. All **44 Rust test executions pass** with this change.
See [the career stat report](../../docs/RACE_CAREER_STAT_PARITY_2026-10-09.md).

The [duel continuation report](../../docs/RACE_DUEL_CONTINUATION_2026-10-09.md),
[cap-decision investigation](../../docs/RACE_CAP_DECISION_2026-10-09.md) and
[arithmetic follow-up](../../docs/RACE_ARITHMETIC_FOLLOWUP_2026-10-09.md)
retain their historical audits and rejected probes. The cap investigation's
remaining two original failures are now repaired by modifier expiry timing.

The preceding [section-length report](../../docs/RACE_SECTION_LENGTH_PARITY_2026-10-09.md)
and [lane-ratio report](../../docs/RACE_LANE_RATIO_PARITY_2026-10-09.md)
record the earlier corrections. The [lane follow-up](../../docs/RACE_LANE_CONTINUATION_2026-10-09.md)
records the previous rounding investigation.
`RX_CF_JOIN_KEEP=0` restores the separate duel exit check;
`RX_SECTION_LENGTH_LOCAL=0` restores the previous double section length;
`RX_LANE_RATIO_LOCAL=0` restores the previous lane expression;
`RX_HP_DEC_LOCAL=0` and `RX_STRAIGHT_CLR=0` restore the preceding HP and
straight-start behavior. These corrections are Global-only.

Implemented components include the shared .NET RNG, ordered modifiers,
pre-start skill conditions and lotteries, stat adjustment, Global course events
and lane geometry, client position-keep strategies, speed/acceleration/HP
updates, temptation, downhill acceleration, last-spurt search, basic skills,
frame recording and finish-time adjustment. The runtime never loads reference
frames, events or results. It needs no native client execution.

## Server integration

Global's shared simulation entry point uses `race_exact_rs` by default for all
race modes. JP uses the same worker in experimental JP mode;
`UMA_JP_LEGACY_RACE=1` selects its legacy engine. The Python adapter exports the live
master tables and sends raw entrants, seed, conditions, race instance and race
type to a persistent `serve` worker. Playback preserves the worker's quantized
frame values, result fields and event order directly; it bypasses legacy race
post-processing. Rushing durations are also exposed for team scoring.

Both launch scripts build the release worker when missing or stale, for Global
and experimental JP. Rust/Cargo must be on PATH when a build is needed.
Builds use `--locked` and can download dependencies on a fresh checkout;
current binaries are reused. Bash selects JP with `--region jp`, PowerShell
with `-Region jp`. JP legacy mode skips the exact worker build.
`UMA_RACE_EXACT_RS_EXE` overrides its location; `UMA_RACE_EXACT_ASSETS` overrides
the Global race parameters. Live workers discard `RX_*` research overrides.
Worker failures propagate from the simulation entry point instead of selecting
the approximate engine; existing handler error handling still applies.

See [the runtime integration report](../../docs/GLOBAL_EXACT_ENGINE_RUNTIME_2026-10-09.md)
for routing and validation details. `serve --master FILE --assets FILE` reads
one race input JSON per stdin line and returns one snapshot JSON per stdout
line, loading master data and assets once.

## Build and run

```sh
cargo test --manifest-path server/race_exact_rs/Cargo.toml --offline
cargo build --release --manifest-path server/race_exact_rs/Cargo.toml --offline

server/race_exact_rs/target/release/race_exact_rs simulate \
  --input ~/tools/gl_inputs/1d3d564e6f29aae8.json \
  --master ~/tools/gl_master.json \
  --assets server/data/race_param_define_global.json \
  --output /tmp/global-candidate.json
```

Use `init` instead of `simulate` to inspect initialization. `--trace` adds tagged
RNG draws and latent per-frame state. `--max-ticks N` bounds the simulation;
failure to finish is an error. `--input-dir DIR --output DIR` processes a batch,
loading the master and asset bundle once. Batch simulation uses one worker by
default; `--jobs N` explicitly requests additional workers, bounded by CPU
availability and input count. Inputs are produced by
`tools/race_exact/gl_prepare.py`; see `docs/RACE_GLOBAL_EXACT.md` for preparation.
Only an input explicitly marked `seed_mode: recovered` may use an alternate seed.

## Verification

The unit tests cover RNG vectors, native math vectors, .NET sort ties, post-number
tables, modifier ordering and position-keep state transitions. The separate
native oracle audit checked 100,000 `log10f` and 100,000 `powf` inputs with zero
bit mismatches in the tested positive finite normal domain. This validates the
Global FMA-dispatch kernels in that domain, not all CRT special/error cases.

```sh
python tools/race_exact/gl_rust_compare.py \
  --input-dir ~/tools/gl_inputs --master ~/tools/gl_master.json \
  --reference-dir ~/tools/gl_refs --output-dir /tmp/global-rust-audit

python tools/race_exact/gl_rust_math_audit.py \
  --image ~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby/hachimi/codedump/GameAssembly.mem \
  --output /tmp/global-rust-math-audit.json

# Requires pefile (available in ~/.venvs/raceassets), clang and rustc.
~/.venvs/raceassets/bin/python tools/race_exact/gl_rust_quaternion_audit.py \
  --image ~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby/UnityPlayer.dll \
  --output /tmp/global-rust-quaternion-audit.json
```

The replay comparison exits 1 when parity fails. References go only to the
Python comparator, never to the Rust process. Saved reports are in
`docs/race_exact_evidence/global_rust_*_audit.json`.

## Remaining work

The named original capture sets are exact; the expanded 247,851-race corpus
still has replay divergences and has not completed a current full physics audit.
Input-file integrity is separate from replay parity; see
[the October 29 audit](../../docs/RACE_PARITY_2026-10-29.md).
Remaining implementation and validation work includes overtaking and lane target
selection, advanced skill value/time usage and additional activation, score
events, defeat results and full post-goal behavior. Position keep uses the first
gate among the most forward strategy as an inferred initial pacemaker. Later
server pacemaker changes, spot struggle, dueling and conserve-power activation
remain unresolved. Server overcap conversion is applied before mood adjustment:
the corrected initial HP matches all 187 races. The first movement sample matches
all 2,733 horses in the 181 races with matching logged seeds. These opening
checks do not establish complete replay parity. Early HP matches all 21,864
tested samples; the native quaternion Lerp audit matches 100,000 pairs. Source search and hypothesis
audits are documented in `docs/RACE_GLOBAL_SOURCE_SEARCH.md`.

The candidate's ordinary movement, skills and finish behavior also require
first-divergence validation. Ported does not imply proven bit-exact. Do not use
candidate results as an authoritative implementation of the Global server.

Global position-keep speed-up does not end merely because its runner is last
and therefore has no runner behind. `RX_GLOBAL_PK_NO_BEHIND_FALSE=0` restores
the previous vacuous-distance-end behavior; JP is unchanged.

Global CompeteTop separation is evaluated per runner; the last active cohort
member is not forced out when its peers separate. `RX_CT_CASCADE=1` restores
the earlier prototype cascade.

Global CompeteTop includes the 3.75 m entry boundary and retains a competitor
at the 5 m exit boundary (`RX_CT_ENTRY_INCLUSIVE=0` / `RX_CT_STRICT_EXIT=0`
restore the older comparisons). Global full-spurt HP feasibility evaluates
distance/speed*drain in double before storing the float (`RX_SPURT_NEED_D=0`
reverts). ConservePower acceleration defaults to `RX_CPORD=3`, with the old
expression available as `RX_CPORD=0`. Skill-window lotteries round the draw
to float and scale in double; `RX_LOT_FLOATDRAW=0` restores full-double draws.
These changes preserve JP behavior. Validated together: 203/208 focused
races and 512/512 independent frozen-audit exact controls. This does not
establish full-corpus parity; see the parity research document for failures.

Global recalculation also permits a nearby more-forward fallback below its
counter threshold while the current handed-over pacemaker is Oikomi;
`RX_PM_EARLY_OIKOMI=0` restores the old guard. This raises the focused set to
204/208 exact while preserving all 512 additional exact controls.

Global `is_finalcorner_laterhalf` also requires current corner membership;
it stops matching on the final straight (`RX_FINALCORNER_HALF_INSIDE=0`
reverts). This resolves 31 additional full races in a 256-failure discovery
set while preserving the focused and 512 exact-control corpora.

Global post-finish front blocking now uses the same double-evaluated float
rate local as the in-race block cap (`RX_GOAL_BLOCK_RATE_D=0` reverts).
This fixes four full races with no exact losses or earlier divergences in
the 1,354-race failure set, preserving all 3,942 controls and 673 originals.
The 257-race early set is unchanged. See Adoption 16 in
`docs/RACE_PARITY_RESUME_2026-11-01.md`; full-corpus parity remains unproven.

Global pacemaker recalculation also respects the existing position-keep end
section, like ordinary selection (`RX_PM_RECALC_ENDSEC=0` reverts). This fixes
37 additional full races with no exact losses or earlier divergences, preserves
all 3,942 controls and 673 originals, and makes the new set 17/17 exact.
See Adoption 17 in the resume notes; this remains partial corpus evidence.
