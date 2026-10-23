
from __future__ import annotations

from functools import lru_cache

import numpy as np

from .params import _raw

F = np.float32
LANE_TO_WORLD = F(11.25)


def _arr(lst):
    return np.array([float(v['decimal']) for v in lst], dtype=np.float32)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by + ay * bw + az * bx - ax * bz,
            aw * bz + az * bw + ax * by - ay * bx,
            aw * bw - ax * bx - ay * by - az * bz)


def _qlerp(a, b, t):
    t = F(min(max(t, F(0)), F(1)))
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    if dot < 0:
        q = tuple(a[i] + t * (-b[i] - a[i]) for i in range(4))
    else:
        q = tuple(a[i] + t * (b[i] - a[i]) for i in range(4))
    n = F(np.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]))
    return tuple(c / n for c in q)


def _euler_y_quat(deg: float):
    h = F(F(deg) * F(0.01745329)) * F(0.5)
    return (F(0), F(np.sin(h)), F(0), F(np.cos(h)))


class LaneAnim:
    def __init__(self, fields: dict, distance: float):
        k = fields['key']
        self.x = _arr(k['valueX'])
        self.y = _arr(k['valueY']) if k.get('valueY') else None
        self.z = _arr(k['valueZ'])
        self.rot = [tuple(F(float(r[c]['decimal'])) for c in 'xyzw') for r in k['rotation']]
        self.distance = F(distance)

    def sample(self, t):
        t = F(t)
        i = int(t)
        n = len(self.x)
        if 0 <= i < n - 1:
            fr = F(t - F(i))
            fr = F(min(max(fr, F(0)), F(1)))
            y0 = self.y[i] if self.y is not None else F(0)
            y1 = self.y[i + 1] if self.y is not None else F(0)
            pos = ((self.x[i + 1] - self.x[i]) * fr + self.x[i],
                   (y1 - y0) * fr + y0,
                   (self.z[i + 1] - self.z[i]) * fr + self.z[i])
        else:
            j = n - 1
            pos = (self.x[j], self.y[j] if self.y is not None else F(0), self.z[j])
        nr = len(self.rot)
        if i < nr - 1:
            rot = _qlerp(self.rot[i], self.rot[i + 1], F(t - F(i)))
        else:
            rot = self.rot[nr - 1]
        return pos, rot


class CourseLane:
    def __init__(self, anim: LaneAnim, right_fix: bool):
        self.anim = anim
        self.fix = _euler_y_quat(180.0 if right_fix else 0.0)
        d = anim.distance
        p1, _ = self.lane_transform(F(d - F(10)))
        self.goal_pos, self.goal_rot = self.lane_transform(d)
        self.goal_dir = tuple(F(F(self.goal_pos[i] - p1[i]) * F(0.1)) for i in range(3))

    def lane_transform(self, d):
        d = F(d)
        a = self.anim
        if d <= a.distance:
            pos, rot = a.sample(F(F(d / a.distance) * F(1000)))
            return pos, _qmul(rot, self.fix)
        e = F(d - a.distance)
        return tuple(self.goal_dir[i] * e + self.goal_pos[i] for i in range(3)), self.goal_rot

    def world(self, d, lane):
        o, (x, y, z, w) = self.lane_transform(d)
        L = F(F(lane) * LANE_TO_WORLD)
        x2, y2, z2 = x + x, y + y, z + z
        xx, yy, zz = x2 * x, y2 * y, z2 * z
        return (F(1) - (yy + zz)) * L + o[0], (x2 * y + z2 * w) * L + o[1], (x2 * z - y2 * w) * L + o[2]

    @property
    def distance2world(self):
        a = self.world(F(0), F(0))
        b = self.world(F(1), F(0))
        dx, dy, dz = a[0] - b[0], a[1] - b[1], a[2] - b[2]
        return F(np.sqrt(np.float64(dy * dy + dx * dx + dz * dz)))


@lru_cache(maxsize=None)
def course_lane(course_id: int, right_fix: bool | None = None) -> CourseLane | None:
    cs = _raw()['course_sets'][str(course_id)]
    name = cs.get('normal_lane_asset')
    if not name:
        return None
    fields = _raw()['lane_assets'][name]['objects'][0]['fields']
    if right_fix is None:
        right_fix = cs['master']['turn'] == 1
    return CourseLane(LaneAnim(fields, cs['master']['distance']), right_fix)
