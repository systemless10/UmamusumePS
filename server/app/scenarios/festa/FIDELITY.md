# Scenario 15 implementation fidelity

The scenario is registered as `single_mode_festa` and uses `festa_data_set`.
It runs through the shared JP transport/response pipeline, rather than replaying
another account's career. All verification careers use scratch account data.

Implemented and exercised:

- Start/load and ordinary career actions, five performance gauges and levels,
  camp practices, camp-end level awards and goal-race camp extensions.
- Assignment screen 51, both required assignment arrays, donor-consuming fusion
  previews and two transformation stages. Attachment succeeds with an empty
  response; fusion returns `festa_data_set_fusion`. Refusals are atomic.
- Presentation screen 49 at turns 24/48/68, ordinary/Great results, reward
  events, festival-live screen 50, finals and normal factor/veteran saving.
- Captured normal/practice home bases, separate bonus lines, native card levels,
  deck-type conditions, effect stacking, energy reductions, caps and stat
  halving. Input replays check the production formula, not a research fitter.
- Performer assignment by normal support-card training type; replacement NPCs
  for nonperforming slots, five linked roles, second-presentation recruits,
  guaranteed act members in practices, Cooperation/camp card copies and finals
  extra partners. Random character/copy selection currently uses stable draws.
- NPC Azuma appraisal tiers, SSR cap 10 (R remains cap 6 per master text), SSR
  native invitation/outings/repeatable/New Year/sendoff data and live event IDs.
  Outings raise one of the lowest-level performances, with display 61 previews.
- Growth-hint event routing, enhanced extra practice, captured trainee-specific
  narratives for Daiwa/Mayano, pre-festival link hints, both scenario endings,
  and explicit evolution requirements for all-Great versus final-Great results.
- Injury story routing, preview/context preservation and common failure reward
  handling. These identity/probability rules are still provisional below.
- Captured Festa response schemas are merged into the JP completion layer;
  shape audits compare actual isolated JP responses with real captures.

## Remaining limits

Playable does not mean every retail probability or content branch is solved.

- Injury IDs 215078/215079/215081/215082/215083 are live captured; 215077 has
  not been observed. The severity draw and exact identity boundaries are not
  recovered. The implementation uses the existing common severity roll, an
  observed stage-specific story family, and an inferred first-stage low-energy
  split. In particular, the existing >=80% severe-roll rule is not exact for
  Festa: a real severe injury also occurred at 63%. Do not count this as full
  failure-event parity. Reward outcome probabilities also retain shared estimates.
- Enhanced extra practice uses one maximum eligible teaching rate. Additive
  per-performer probability is disproved; this particular remaining model is
  provisional. The rare Practice Good branch is not reconstructed.
- Nontraining gauge target distributions, the number of goal-race targets,
  replacement/recruit NPC selection weights, support-copy placement weights,
  and tie-breaking draws are not recovered. Stable local draws avoid rerolling
  rewards on reload; they do not reproduce the retail random stream.
- Initial actor assignments normally follow card type in the captures. The
  four-type controls instead assign speed card 30242 to guts; that exact
  observed deck has a scoped override. The general allocation rule for other
  duplicate-type decks has not been recovered, so their type-based fallback
  is provisional.
- Native hint roll/pool selection and the fused extra-hint detail need further
  live checks. Training stat/SP lines are audited separately from hint rewards.
- Eight No Reason/Yukino scenario narratives have no measured schedule and are
  deliberately not scheduled on guessed turns. R Azuma's exclusive event rewards,
  the incomplete Azuma sendoff and shared cheer trigger remain unvalidated.
- Auto-setting storage follows the dumped request fields; its three endpoints
  have not been observed live. Their native refusal rules remain unverified.
- No graphical-client rendering test has been performed. The full JP API flow,
  wire types/required fields, saved veteran and formula replays are verified.

## Reproduce

```
server/.venv/bin/python -m unittest discover -s server/tests -p test_festa.py
python tools/research/shuntaisai_implementation_parity.py
SMOKE_DUMP=/tmp/festa-smoke.jsonl SMOKE_LOAD=1 server/.venv/bin/python tools/jp_career_smoke.py --region jp --scenario 15 --card 100201 --deck 30145,30186,30304,20001,20004 --finish --no-fail
python tools/research/shuntaisai_wire_audit.py /tmp/festa-smoke.jsonl
```

`--no-fail` bypasses career-goal failure in scratch tests; it is not a
production change. Forced-failure tests use a scratch config override. Neither
the smoke harness nor formula replay writes a real account's save.

Current metrics and provenance are under `docs/shuntaisai_evidence/`:
`implementation_parity.json`, `implementation_wire_audit.json`,
`implementation_smoke_summary.json`, and `injury_audit.json`.
