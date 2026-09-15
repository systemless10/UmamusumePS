"""Stream all career_log_*.json files once and tally the handful of
shortlist constants that have a clean, unambiguous event_id (or command_type)
signature in the REQ/RES digest. See docs/UNPROVEN_CONSTANTS_DERIVED.md for
the write-up; this script is the only source of the numbers quoted there.

Run: server/.venv/Scripts/python tools/derive_constants/analyze.py
"""
import json, glob, sys, math, collections

REST_EVENTS = {7009: 30, 7010: 50, 7011: 70}
INFIRMARY_EVENT = 7016
EXTRA_TRAINING_EVENT = 7017
ACUPUNCTURE_EVENT = 7020
CRANE_EVENT = 6002
SKIN_OUTBREAK_EVENT = 7002
MIGRAINE_EVENT = 7023
ETSUKO_WIN_EVENT_A = 11169
ETSUKO_WIN_EVENT_B = 11170

SLACKER_COND = 2
SKIN_OUTBREAK_COND = 3
MIGRAINE_COND = 5

def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = (z * math.sqrt((p*(1-p) + z*z/(4*n)) / n)) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


class Tallies:
    def __init__(self):
        self.rest_events = collections.Counter()          # event_id -> count (command_type==7 turns)
        self.rest_turns_total = 0
        self.rest_uncapped_delta = collections.Counter()   # event_id -> list handled as sum/count
        self.rest_uncapped_n = collections.Counter()
        self.rest_uncapped_sum = collections.Counter()
        self.rest_capped_skipped = 0

        self.infirmary_turns = 0
        self.infirmary_fired = 0

        self.extra_training_num = 0
        self.acupuncture_num = 0
        self.training_turns = 0

        self.crane_num = 0
        self.recreation_turns = 0

        self.etsuko_a = 0
        self.etsuko_b = 0

        # condition proc rate: numerator = turns where proc event fired AND a
        # chara_effect_id_array snapshot within +/-1 turn confirms the
        # condition was held; denominator = turns where a snapshot confirms
        # the condition held (any such turn, regardless of proc).
        self.cond_holds = collections.Counter()      # cond_id -> turns confirmed held (snapshot)
        self.cond_procs_in_held_turn = collections.Counter()

        # slacker no-show: training turns where Slacker confirmed held via a
        # same-turn snapshot, split by whether the command produced 0 stat gain.
        self.slacker_train_turns = 0
        self.slacker_zero_gain = 0

        self.files_ok = 0
        self.files_err = 0
        self.scenario_counts = collections.Counter()


def chara_effect_ids(turn):
    cc = turn.get('current_command')
    if not isinstance(cc, dict):
        return None
    ci = cc.get('chara_info')
    if not isinstance(ci, dict):
        return None
    arr = ci.get('chara_effect_id_array')
    if arr is None:
        return None
    try:
        return set(int(x) for x in arr)
    except Exception:
        return None


def process_file(fp, T: Tallies):
    with open(fp, encoding='utf-8') as f:
        data = json.load(f)
    T.scenario_counts[data.get('scenario_id')] += 1
    turns = data.get('turns') or []

    # pre-index: turn_number -> effect id snapshot (if any) for this file
    snap_by_turn = {}
    for t in turns:
        eids = chara_effect_ids(t)
        if eids is not None:
            tn = t.get('turn')
            if tn is not None:
                snap_by_turn[tn] = eids

    for i, t in enumerate(turns):
        tn = t.get('turn')
        cc = t.get('current_command')
        ctype = cc.get('command_type') if isinstance(cc, dict) else None
        acs = t.get('api_calls') or []

        event_ids_this_turn = []
        exec_req_vital = None
        exec_res_vital = None
        for ac in acs:
            ep = ac.get('endpoint', '')
            d = ac.get('data')
            if not isinstance(d, dict):
                continue
            if ac.get('direction') == 'REQ' and ep.endswith('check_event'):
                eid = d.get('event_id')
                if eid is not None:
                    event_ids_this_turn.append(eid)
            if ep.endswith('exec_command'):
                if ac.get('direction') == 'REQ':
                    exec_req_vital = d.get('current_vital')
                elif ac.get('direction') == 'RES':
                    chara = d.get('chara')
                    if isinstance(chara, dict):
                        exec_res_vital = chara.get('vital')

        # --- U-001 Rest outcome ---
        if ctype == 7:
            T.rest_turns_total += 1
            fired = [e for e in event_ids_this_turn if e in REST_EVENTS]
            if fired:
                eid = fired[0]
                T.rest_events[eid] += 1
                # find the vital snapshot after this event resolves: scan
                # forward through RES chara.vital in this turn's remaining
                # api_calls after the REQ for eid.
                after_vital = None
                seen_req = False
                for ac in acs:
                    d = ac.get('data')
                    if not isinstance(d, dict):
                        continue
                    if ac.get('direction') == 'REQ' and d.get('event_id') == eid:
                        seen_req = True
                        continue
                    if seen_req and ac.get('direction') == 'RES':
                        chara = d.get('chara')
                        if isinstance(chara, dict) and chara.get('vital') is not None:
                            after_vital = chara.get('vital')
                            break
                before_vital = exec_req_vital
                if before_vital is not None and after_vital is not None:
                    expect = REST_EVENTS[eid]
                    if before_vital + expect < 100:
                        delta = after_vital - before_vital
                        T.rest_uncapped_n[eid] += 1
                        T.rest_uncapped_sum[eid] += delta
                    else:
                        T.rest_capped_skipped += 1

        # --- U-005 Infirmary ---
        if ctype == 8:
            T.infirmary_turns += 1
            if INFIRMARY_EVENT in event_ids_this_turn:
                T.infirmary_fired += 1

        # --- U-013 Extra training / acupuncture (rate per training turn) ---
        if ctype == 1:
            T.training_turns += 1
            if EXTRA_TRAINING_EVENT in event_ids_this_turn:
                T.extra_training_num += 1
            if ACUPUNCTURE_EVENT in event_ids_this_turn:
                T.acupuncture_num += 1

        # --- U-020 Crane machine on recreation turns ---
        if ctype == 3:
            T.recreation_turns += 1
            if CRANE_EVENT in event_ids_this_turn:
                T.crane_num += 1

        # --- U-016 Etsuko coverage variant ---
        if ETSUKO_WIN_EVENT_A in event_ids_this_turn:
            T.etsuko_a += 1
        if ETSUKO_WIN_EVENT_B in event_ids_this_turn:
            T.etsuko_b += 1

        # --- U-006 condition proc rate (Skin Outbreak / Migraine) ---
        # denominator: this turn (or +/-1) has a snapshot confirming the
        # condition held.
        eids_snap = snap_by_turn.get(tn)
        if eids_snap is None and tn is not None:
            eids_snap = snap_by_turn.get(tn - 1)
        if eids_snap is not None:
            if SKIN_OUTBREAK_COND in eids_snap:
                T.cond_holds[SKIN_OUTBREAK_COND] += 1
                if SKIN_OUTBREAK_EVENT in event_ids_this_turn:
                    T.cond_procs_in_held_turn[SKIN_OUTBREAK_COND] += 1
            if MIGRAINE_COND in eids_snap:
                T.cond_holds[MIGRAINE_COND] += 1
                if MIGRAINE_EVENT in event_ids_this_turn:
                    T.cond_procs_in_held_turn[MIGRAINE_COND] += 1

            # --- U-030 Slacker no-show ---
            if SLACKER_COND in eids_snap and ctype == 1:
                T.slacker_train_turns += 1
                if exec_req_vital is not None and exec_res_vital is not None:
                    # Slacker no-show means the command produced no vital
                    # drain and (per the code) no stat gain; vital unchanged
                    # is our proxy since stat deltas aren't in the digest
                    # chara block (speed/power/etc ARE present -- use those).
                    pass
                # use stat deltas from exec_command RES vs turn-start stats
                for ac in acs:
                    if ac.get('endpoint', '').endswith('exec_command') and ac.get('direction') == 'RES':
                        d = ac.get('data')
                        chara = d.get('chara') if isinstance(d, dict) else None
                        if isinstance(chara, dict):
                            start = t.get('stats') or {}
                            gained = False
                            for k_after, k_before in (('speed','speed'),('stamina','stamina'),
                                                        ('power','power'),('guts','guts'),('wiz','wit')):
                                av = chara.get(k_after)
                                bv = start.get(k_before)
                                if av is not None and bv is not None and av > bv:
                                    gained = True
                                    break
                            if not gained:
                                T.slacker_zero_gain += 1
                        break


def main():
    files = sorted(glob.glob('captures/bot_logs/career_log_*.json'))
    T = Tallies()
    n = len(files)
    for i, fp in enumerate(files):
        try:
            process_file(fp, T)
            T.files_ok += 1
        except Exception as e:
            T.files_err += 1
            print(f"ERR {fp}: {e}", file=sys.stderr)
        if (i+1) % 200 == 0:
            print(f"...{i+1}/{n}", file=sys.stderr)

    out = {}
    out['files_ok'] = T.files_ok
    out['files_err'] = T.files_err
    out['scenario_counts'] = dict(T.scenario_counts)

    # U-001
    rest_total_fired = sum(T.rest_events.values())
    out['U001_rest_turns_total'] = T.rest_turns_total
    out['U001_rest_fired_total'] = rest_total_fired
    out['U001_rest_dist'] = {}
    for eid, energy in REST_EVENTS.items():
        k = T.rest_events[eid]
        p, lo, hi = wilson_ci(k, rest_total_fired)
        out['U001_rest_dist'][energy] = {'n': k, 'rate': p, 'ci_lo': lo, 'ci_hi': hi}
    out['U001_rest_uncapped_mean_delta'] = {
        energy: (T.rest_uncapped_sum[eid] / T.rest_uncapped_n[eid] if T.rest_uncapped_n[eid] else None,
                 T.rest_uncapped_n[eid])
        for eid, energy in REST_EVENTS.items()
    }
    out['U001_rest_capped_skipped'] = T.rest_capped_skipped

    # U-005
    p, lo, hi = wilson_ci(T.infirmary_fired, T.infirmary_turns)
    out['U005_infirmary'] = {'fired': T.infirmary_fired, 'turns': T.infirmary_turns,
                              'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    # U-013
    p, lo, hi = wilson_ci(T.extra_training_num, T.training_turns)
    out['U013_extra_training'] = {'n': T.extra_training_num, 'denom': T.training_turns,
                                   'rate': p, 'ci_lo': lo, 'ci_hi': hi}
    p, lo, hi = wilson_ci(T.acupuncture_num, T.training_turns)
    out['U013_acupuncture'] = {'n': T.acupuncture_num, 'denom': T.training_turns,
                                'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    # U-020
    p, lo, hi = wilson_ci(T.crane_num, T.recreation_turns)
    out['U020_crane'] = {'n': T.crane_num, 'denom': T.recreation_turns,
                          'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    # U-016
    tot = T.etsuko_a + T.etsuko_b
    p, lo, hi = wilson_ci(T.etsuko_b, tot)
    out['U016_etsuko'] = {'coverage_b': T.etsuko_b, 'normal_a': T.etsuko_a, 'total': tot,
                           'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    # U-006
    out['U006_cond_proc'] = {}
    for cond, name in ((SKIN_OUTBREAK_COND, 'skin_outbreak'), (MIGRAINE_COND, 'migraine')):
        k = T.cond_procs_in_held_turn[cond]
        n_ = T.cond_holds[cond]
        p, lo, hi = wilson_ci(k, n_)
        out['U006_cond_proc'][name] = {'n': k, 'denom': n_, 'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    # U-030
    p, lo, hi = wilson_ci(T.slacker_zero_gain, T.slacker_train_turns)
    out['U030_slacker'] = {'zero_gain': T.slacker_zero_gain, 'denom': T.slacker_train_turns,
                            'rate': p, 'ci_lo': lo, 'ci_hi': hi}

    with open('tools/derive_constants/results.json', 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))


if __name__ == '__main__':
    main()
