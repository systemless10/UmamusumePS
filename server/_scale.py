import sys, time, random
sys.path.insert(0, '.')

def main():
    from app import state as S
    from app.handlers import team_stadium as T
    from app.simulation import race_simulator as rs
    v = '594943559313'
    st = S.get_state(v)['team_stadium_state']
    roster = T._roster_by_id(v)
    slots = [s for s in st['team_data_array'] if s.get('trained_chara_id')][:3]
    base = []
    for s in slots:
        c = dict(roster[s['trained_chara_id']]); c['running_style'] = s['running_style']
        base.append(c)
    cs, course, gc = rs.get_course_for_race_instance(T._pick_course(3, random.Random(1)))
    print('course distance %dm' % course['distance'])
    for nskill in (0, 10, 30, 80, 200, 664):
        horses = []
        for c in base + base:
            c = dict(c); c['skill_array'] = (c.get('skill_array') or [])[:nskill]
            horses.append(c)
        t = time.time()
        sim = rs.run_simulation(horses, cs, 1, 1, 1, 42, gate_count=gc or 12,
                                gate_assignment=list(range(6)))
        d = time.time() - t
        ticks = len(sim['frames'])
        print('%4d skills/horse -> %6.2fs   (%d frames)' % (nskill, d, ticks))

if __name__ == '__main__':
    main()
