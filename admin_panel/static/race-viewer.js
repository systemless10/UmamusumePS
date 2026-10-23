
import { act, call, esc, gameIcon, icon, ICON_VERSION } from './core.js';
import { STYLES } from './widgets.js';
import { PHASES, courseStrip, landmarks, skillTier } from './race-breakdown.js';

const COURSE_WIDTH = 11.25;
const LENGTH = 2.5;
const RUNOUT = 80;
const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];
const POPUP = 2.2;
const EVENT_NAMES = { 4: 'Spot struggle', 5: 'Dueling', 6: 'Conserve power' };
const MOODS = ['', 'Awful', 'Bad', 'Normal', 'Good', 'Great'];
const TIER = { white: '#e8e4da', gold: '#f0cf7a', unique: '#c6b4f0', '': '#cfc8d8' };
const PHASE_FILL = { early: 'rgba(143,201,164,0.55)', mid: 'rgba(127,163,184,0.5)', late: 'rgba(227,181,107,0.6)', spurt: 'rgba(224,138,122,0.7)' };
const SURFACE_FILL = { 1: '#2c4f37', 2: '#5c4632' };
const apt = (v) => 'GFEDCBAS'[Math.max(1, Math.min(8, v || 1)) - 1];
const m = (v) => `${Math.round(v).toLocaleString()} m`;
const clock = (t) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`;
const runnerColor = (i) => `hsl(${(i * 137.508 + 20) % 360} 62% 64%)`;

const tracks = new Map();
export function loadTrack(id) {
  if (!tracks.has(id)) tracks.set(id, call('races.track', { id }).catch((ex) => { tracks.delete(id); throw ex; }));
  return tracks.get(id);
}
const images = new Map();
function image(kind, id) {
  if (!id) return null;
  const key = kind + id;
  if (!images.has(key)) {
    const img = new Image();
    img.src = `/icons/${kind}/${id}?v=${ICON_VERSION}`;
    images.set(key, img);
  }
  const img = images.get(key);
  return img.complete && img.naturalWidth ? img : null;
}

const pairs = (flat) => { const out = []; for (let i = 0; i + 1 < flat.length; i += 2) out.push([flat[i], -flat[i + 1]]); return out; };

function schematic(c) {
  const n = 1001, step = c.distance / (n - 1), sign = c.turn === 2 ? -1 : 1, out = [];
  let x = 0, y = 0, a = 0;
  for (let k = 0; k < n; k++) {
    out.push([x, y]);
    const s = k * step, corner = (c.corners || []).find((q) => s >= q.start && s < q.start + q.length);
    if (corner) a += sign * (Math.PI / 2) * step / corner.length;
    x += Math.cos(a) * step; y += Math.sin(a) * step;
  }
  return out;
}

export function geometry(track, course) {
  const dist = course.distance, laneMax = (course.laneMax || 13000) / 10000;
  const exact = !!track?.route;
  const route = exact ? pairs(track.route) : schematic(course);
  const outline = exact ? track.outline.map(pairs) : [];
  const all = [...route, ...outline.flat()];
  const mx = all.reduce((s, p) => s + p[0], 0) / all.length, my = all.reduce((s, p) => s + p[1], 0) / all.length;
  let sxx = 0, syy = 0, sxy = 0;
  for (const [x, y] of all) { sxx += (x - mx) ** 2; syy += (y - my) ** 2; sxy += (x - mx) * (y - my); }
  const th = 0.5 * Math.atan2(2 * sxy, sxx - syy);
  let c = Math.cos(th), s = Math.sin(th);
  const goal = route.at(-1);
  if (-s * (goal[0] - mx) + c * (goal[1] - my) < 0) { c = -c; s = -s; }
  const rot = (p) => { const dx = p[0] - mx, dy = p[1] - my; p[0] = c * dx + s * dy; p[1] = -s * dx + c * dy; };
  route.forEach(rot); outline.forEach((o) => o.forEach(rot));

  const n = route.length, T = [], N = [];
  for (let i = 0; i < n; i++) {
    const a = route[Math.max(0, i - 3)], b = route[Math.min(n - 1, i + 3)];
    const dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1;
    T.push([dx / l, dy / l]);
  }
  let turn = 0;
  for (let i = 0; i + 1 < n; i++) turn += T[i][0] * T[i + 1][1] - T[i][1] * T[i + 1][0];
  const side = Math.abs(turn) > 0.5 ? -Math.sign(turn) : -1;
  for (const [tx, ty] of T) N.push([-ty * side, tx * side]);

  const at = (d, off = 0) => {
    if (d <= 0 || d >= dist) {
      const k = d <= 0 ? 0 : n - 1, e = d <= 0 ? d : d - dist;
      return [route[k][0] + T[k][0] * e + N[k][0] * off, route[k][1] + T[k][1] * e + N[k][1] * off];
    }
    const u = d / dist * (n - 1), i = Math.min(n - 2, Math.floor(u)), f = u - i;
    const p = route[i], q = route[i + 1], a = N[i], b = N[i + 1];
    return [p[0] + (q[0] - p[0]) * f + (a[0] + (b[0] - a[0]) * f) * off,
            p[1] + (q[1] - p[1]) * f + (a[1] + (b[1] - a[1]) * f) * off];
  };
  const normal = (d) => N[Math.max(0, Math.min(n - 1, Math.round(d / dist * (n - 1))))];
  const band = (a, b, o0, o1, path = new Path2D()) => {
    const steps = Math.max(2, Math.ceil((b - a) / dist * (n - 1)));
    const inner = [], outer = [];
    for (let k = 0; k <= steps; k++) { const d = a + (b - a) * k / steps; inner.push(at(d, o0)); outer.push(at(d, o1)); }
    path.moveTo(...inner[0]);
    for (const p of inner) path.lineTo(...p);
    for (const p of outer.reverse()) path.lineTo(...p);
    path.closePath();
    return path;
  };
  const across = (d, o0, o1, path = new Path2D()) => { path.moveTo(...at(d, o0)); path.lineTo(...at(d, o1)); return path; };
  const polyline = (pts, path = new Path2D()) => { pts.forEach((p, i) => i ? path.lineTo(...p) : path.moveTo(...p)); return path; };
  const along = (a, b, off) => { const steps = Math.max(2, Math.ceil((b - a) / dist * (n - 1))); return Array.from({ length: steps + 1 }, (_, k) => at(a + (b - a) * k / steps, off)); };

  const inner = -0.6, outer = laneMax * COURSE_WIDTH + 0.6;
  const corners = new Path2D();
  for (const k of course.corners || []) band(k.start, k.start + k.length, inner, outer, corners);
  const marks = new Path2D();
  for (const [d] of landmarks(course)) across(d, inner, outer, marks);

  const xs = all.length ? [...route, ...outline.flat()] : route;
  const bounds = [Math.min(...xs.map((p) => p[0])), Math.min(...xs.map((p) => p[1])), Math.max(...xs.map((p) => p[0])), Math.max(...xs.map((p) => p[1]))];

  const stripCache = new Map();
  function strips(scale) {
    const key = Math.round(Math.log2(scale) * 4);
    if (!stripCache.has(key)) {
      const z = 2 ** (key / 4), gap = Math.max(0.6, 2 / z), w = Math.max(2.5, 6 / z);
      const up = new Path2D(), down = new Path2D(), ticks = new Path2D();
      for (const sl of course.slopes || []) band(sl.start, sl.start + sl.length, outer + gap, outer + gap + w, sl.slope > 0 ? up : down);
      const phases = PHASES.map(([a, b, , k]) => [k, band(a * dist, b * dist, inner - gap - w, inner - gap)]);
      for (let i = 1; i < 24; i++) across(dist * i / 24, inner - gap - w, inner - gap, ticks);
      stripCache.set(key, { up, down, phases, ticks, outside: outer + gap + w, inside: inner - gap - w });
    }
    return stripCache.get(key);
  }

  const cornerNo = (i, all) => 4 - ((all.length - 1 - i) % 4);
  const labels = [
    { d: 0, side: 1, text: 'Start', kind: 'key' }, { d: dist, side: 1, text: 'Goal', kind: 'key' },
    ...landmarks(course).map(([d, l]) => ({ d, side: 1, text: l, kind: 'mark' })),
    ...PHASES.slice(1).map(([a, , l]) => ({ d: a * dist, side: -1, text: l, kind: 'phase' })),
    ...(course.corners || []).map((k, i, all) => ({ d: k.start + k.length / 2, side: -1, text: `Corner ${cornerNo(i, all)}${i === all.length - 1 ? ' (final)' : ''}`, kind: 'corner' })),
    ...(course.slopes || []).map((sl) => ({ d: sl.start + sl.length / 2, side: 1, text: `${sl.slope > 0 ? '▲ Uphill' : '▼ Downhill'} ${Math.abs(sl.slope / 10000).toFixed(1)}%`, kind: sl.slope > 0 ? 'up' : 'down' })),
  ];

  function describe(d) {
    const ph = PHASES.find(([a, b]) => d / dist >= a && d / dist < b);
    const corner = (course.corners || []).findIndex((k) => d >= k.start && d < k.start + k.length);
    const slope = (course.slopes || []).find((sl) => d >= sl.start && d < sl.start + sl.length);
    return [m(d), `Section ${Math.min(24, Math.floor(d / dist * 24) + 1)}/24`, ph ? ph[2] : 'Past the goal',
      corner >= 0 ? `Corner ${cornerNo(corner, course.corners)}` : (course.straights || []).some((st) => d >= st.start && d < st.end) ? 'Straight' : '',
      slope ? `${slope.slope > 0 ? 'Uphill' : 'Downhill'} ${Math.abs(slope.slope / 10000).toFixed(1)}%` : 'Flat'].filter(Boolean);
  }
  function locate(x, y) {
    let best = Infinity, k = -1;
    for (let i = 0; i < n; i++) { const e = (route[i][0] - x) ** 2 + (route[i][1] - y) ** 2; if (e < best) { best = e; k = i; } }
    const [nx, ny] = N[k], off = (x - route[k][0]) * nx + (y - route[k][1]) * ny;
    return off > inner - 8 && off < outer + 8 ? k / (n - 1) * dist : null;
  }

  return {
    exact, dist, laneMax, inner, outer, bounds, at, normal, strips, labels, describe, locate, surface: course.surface,
    base: band(0, dist, inner, outer), runout: band(dist, dist + RUNOUT, inner, outer), corners, marks,
    rails: [polyline(along(0, dist + RUNOUT, inner)), polyline(along(0, dist + RUNOUT, outer))],
    outline: outline.map((o) => polyline(o)),
    start: across(0, inner, outer), goal: across(dist, inner, outer),
  };
}

function createMap(host, geo) {
  const canvas = document.createElement('canvas');
  host.prepend(canvas);
  const g = canvas.getContext('2d');
  let W = 1, H = 1, dpr = 1;
  const cam = { x: 0, y: 0, s: 1 };
  const view = (box, pad = 24) => {
    const [x0, y0, x1, y1] = box;
    return { x: (x0 + x1) / 2, y: (y0 + y1) / 2, s: Math.min((W - pad * 2) / Math.max(1, x1 - x0), (H - pad * 2) / Math.max(1, y1 - y0)) };
  };
  const toScreen = ([x, y], c = cam) => [(x - c.x) * c.s + W / 2, (y - c.y) * c.s + H / 2];
  const toWorld = (sx, sy) => [(sx - W / 2) / cam.s + cam.x, (sy - H / 2) / cam.s + cam.y];
  function resize() {
    const r = host.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    W = Math.max(1, r.width); H = Math.max(1, r.height);
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  }

  function course(c, minimal = false, ox = 0, oy = 0) {
    const s = c.s;
    g.setTransform(dpr * s, 0, 0, dpr * s, dpr * (W / 2 - c.x * s + ox), dpr * (H / 2 - c.y * s + oy));
    g.lineJoin = 'round'; g.lineCap = 'round';
    g.strokeStyle = 'rgba(255,255,255,0.055)'; g.lineWidth = geo.outer * 1.05;
    for (const p of geo.outline) g.stroke(p);
    g.fillStyle = SURFACE_FILL[geo.surface] || SURFACE_FILL[1];
    g.fill(geo.base);
    g.globalAlpha = 0.45; g.fill(geo.runout); g.globalAlpha = 1;
    if (minimal) return;
    g.fillStyle = 'rgba(216,185,138,0.16)'; g.fill(geo.corners);
    g.strokeStyle = 'rgba(255,255,255,0.55)'; g.lineWidth = 1.4 / s; g.stroke(geo.rails[0]);
    g.strokeStyle = 'rgba(255,255,255,0.25)'; g.stroke(geo.rails[1]);
    const st = geo.strips(s);
    g.fillStyle = 'rgba(224,138,122,0.85)'; g.fill(st.up);
    g.fillStyle = 'rgba(111,178,232,0.85)'; g.fill(st.down);
    for (const [k, p] of st.phases) { g.fillStyle = PHASE_FILL[k]; g.fill(p); }
    g.strokeStyle = 'rgba(20,16,26,0.9)'; g.lineWidth = 1.2 / s; g.stroke(st.ticks);
    g.setLineDash([4 / s, 4 / s]); g.strokeStyle = 'rgba(236,231,220,0.5)'; g.lineWidth = 1 / s; g.stroke(geo.marks); g.setLineDash([]);
    g.strokeStyle = '#ece7dc'; g.lineWidth = 2.5 / s; g.stroke(geo.start);
    g.strokeStyle = '#fff'; g.lineWidth = 3 / s; g.stroke(geo.goal);
    g.setLineDash([3 / s, 3 / s]); g.strokeStyle = '#111'; g.stroke(geo.goal); g.setLineDash([]);
  }

  function labels(taken) {
    const st = geo.strips(cam.s);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.font = '700 11px Nunito, system-ui, sans-serif';
    for (const l of geo.labels) {
      const [nx, ny] = geo.normal(l.d), dir = [nx * l.side, ny * l.side];
      const [px, py] = toScreen(geo.at(l.d, l.side > 0 ? st.outside : st.inside));
      const w = g.measureText(l.text).width + 10, h = 18;
      const x = px + dir[0] * (8 + w / 2) - w / 2, y = py + dir[1] * (8 + h / 2) - h / 2;
      if (x < 4 || y < 4 || x + w > W - 4 || y + h > H - 4 || taken.some((r) => x < r[2] && x + w > r[0] && y < r[3] && y + h > r[1])) continue;
      taken.push([x, y, x + w, y + h]);
      g.fillStyle = l.kind === 'key' ? 'rgba(236,231,220,0.92)' : 'rgba(24,19,31,0.82)';
      roundRect(g, x, y, w, h, 6); g.fill();
      g.fillStyle = { key: '#1f1a27', up: '#f0a698', down: '#8cc4f0', phase: '#cfc8d8', corner: '#e6cfa6', mark: '#ece7dc' }[l.kind];
      g.textBaseline = 'middle'; g.textAlign = 'center';
      g.fillText(l.text, x + w / 2, y + h / 2 + 0.5);
    }
  }

  return { canvas, cam, view, toScreen, toWorld, resize, course, labels, g, size: () => [W, H], dpr: () => dpr };
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

export function mountCourseMap(host, raceInstanceId, course) {
  let alive = true, map = null, geo = null, hover = null;
  host.innerHTML = `<div class="rv-stage rv-static"><div class="rv-tip" hidden></div><div class="rv-msg faint">Loading course map…</div></div>`;
  const stage = host.firstElementChild, tip = stage.querySelector('.rv-tip');
  const draw = () => {
    if (!map) return;
    map.resize();
    Object.assign(map.cam, map.view(geo.bounds, 40));
    const [W, H] = map.size();
    map.g.setTransform(map.dpr(), 0, 0, map.dpr(), 0, 0); map.g.clearRect(0, 0, W, H);
    map.course(map.cam);
    if (hover !== null) {
      const [x, y] = map.toScreen(geo.at(hover, geo.outer / 2));
      map.g.setTransform(map.dpr(), 0, 0, map.dpr(), 0, 0);
      map.g.fillStyle = '#fff'; map.g.beginPath(); map.g.arc(x, y, 4, 0, Math.PI * 2); map.g.fill();
    }
    map.labels([]);
  };
  const ro = new ResizeObserver(draw);
  loadTrack(raceInstanceId).then((t) => {
    if (!alive) return;
    geo = geometry(t, course || t.course);
    map = createMap(stage, geo);
    const msg = stage.querySelector('.rv-msg'); msg.textContent = geo.exact ? '' : 'Schematic layout: no client path for this course.'; msg.classList.add('note');
    ro.observe(stage);
    stage.onmousemove = (e) => {
      const r = stage.getBoundingClientRect();
      const d = geo.locate(...map.toWorld(e.clientX - r.left, e.clientY - r.top));
      hover = d === null || d > geo.dist ? null : d;
      tip.hidden = hover === null;
      if (hover !== null) { tip.innerHTML = geo.describe(hover).map(esc).join(' · '); tip.style.left = e.clientX - r.left + 14 + 'px'; tip.style.top = e.clientY - r.top + 14 + 'px'; }
      draw();
    };
    stage.onmouseleave = () => { hover = null; tip.hidden = true; draw(); };
  }).catch((ex) => { if (alive) stage.querySelector('.rv-msg').textContent = ex.message; });
  return () => { alive = false; ro.disconnect(); };
}

export function mountViewer(root, ctx) {
  const { replay } = ctx;
  const course = replay.course || {}, horses = replay.horses, results = replay.results, frames = replay.frames;
  const info = replay.entry.payload.race_result_info;
  const dist = course.distance || 1;
  const skillById = new Map(ctx.skills.map((s) => [s.id, s]));
  const skillName = (id) => skillById.get(id)?.name || `Skill ${id}`;
  const tierOf = (id) => skillTier(skillById.get(id));
  const name = (i) => ctx.runnerName(horses[i]);
  const END = frames.at(-1)?.t || 0;
  const events = [...replay.events].sort((a, b) => a.t - b.t).map((e) => ({ ...e, i: e.params[0],
    skill: e.type === 3 ? e.params[1] : 0, dur: e.type === 3 ? e.params[2] / 10000 : 0 }));
  const shownEvents = events.filter((e) => e.type !== 3 || e.dur >= 0);
  const byRunner = horses.map((_, i) => events.filter((e) => e.i === i && e.type === 3));

  let alive = true, raf = 0, last = 0, t = 0, playing = false, rate = 1, sel = results.findIndex((r) => r.finish_order === 0);
  let mode = 'pack', popups = 'all', showLabels = true, dirty = true, hoverRunner = -1, hoverD = null, uiAt = 0, feedKey = '';
  let geo = null, map = null, snap = null, drag = null;
  if (sel < 0) sel = 0;

  function frameAt(time) {
    let lo = 0, hi = frames.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (frames[mid].t <= time) lo = mid; else hi = mid - 1; }
    return lo;
  }
  function snapshot(time) {
    const k = frameAt(time), a = frames[k], b = frames[Math.min(k + 1, frames.length - 1)];
    const h = b.t - a.t, s = h > 0 ? Math.min(1, (time - a.t) / h) : 0;
    const s2 = s * s, s3 = s2 * s;
    const rows = horses.map((_, i) => {
      const p = a.horses[i], q = b.horses[i];
      let d = h > 0 ? (2 * s3 - 3 * s2 + 1) * p.distance + (s3 - 2 * s2 + s) * h * p.speed + (-2 * s3 + 3 * s2) * q.distance + (s3 - s2) * h * q.speed : p.distance;
      d = Math.max(Math.min(p.distance, q.distance) - 0.5, Math.min(Math.max(p.distance, q.distance) + 0.5, d));
      const r = results[i];
      return { i, d, lane: p.lane_position + (q.lane_position - p.lane_position) * s, v: p.speed + (q.speed - p.speed) * s,
        hp: p.hp + (q.hp - p.hp) * s, rushed: !!p.temptation_mode, block: p.block_front_horse_index,
        done: time >= r.finish_time_raw, spurt: r.last_spurt_start_distance >= 0 && d >= r.last_spurt_start_distance };
    });
    const order = [...rows].sort((x, y) => (y.done ? 1e9 - results[y.i].finish_order : y.d) - (x.done ? 1e9 - results[x.i].finish_order : x.d));
    order.forEach((r, k) => { r.rank = k + 1; });
    return { t: time, rows, order, lead: order[0] };
  }
  const active = (i, time) => byRunner[i].filter((e) => e.dur >= 0 && e.t <= time && time <= e.t + Math.max(e.dur, 0.8));
  function leaderTime(d) {
    for (let k = 1; k < frames.length; k++) {
      const hi = Math.max(...frames[k].horses.map((h) => h.distance));
      if (hi >= d) {
        const lo = Math.max(...frames[k - 1].horses.map((h) => h.distance));
        return frames[k - 1].t + (frames[k].t - frames[k - 1].t) * Math.max(0, Math.min(1, (d - lo) / Math.max(1e-6, hi - lo)));
      }
    }
    return END;
  }

  const phaseTimes = PHASES.map(([a, b, l, k]) => [leaderTime(a * dist), leaderTime(b * dist), l, k]);
  const winner = results.findIndex((r) => r.finish_order === 0);
  root.innerHTML = `<div class="rv" tabindex="0">
      <div class="rv-main">
        <div class="rv-stage" data-rv-stage>
          <div class="rv-hud tl" data-rv-hud></div>
          <div class="rv-hud tr">
            <div class="seg">${[['track', 'Whole track'], ['pack', 'Follow pack'], ['runner', 'Follow runner']].map(([k, l]) => `<button data-rv-cam="${k}" class="${mode === k ? 'on' : ''}">${l}</button>`).join('')}</div>
            <div class="seg">${[['all', 'All call-outs'], ['sel', 'Selected only'], ['off', 'None']].map(([k, l]) => `<button data-rv-pop="${k}" class="${popups === k ? 'on' : ''}" title="Skill call-outs on the map">${l}</button>`).join('')}</div>
            <button class="btn sm ${showLabels ? 'on' : ''}" data-rv-labels>Labels</button>
          </div>
          <div class="rv-legend">
            <span><i style="background:rgba(224,138,122,.85)"></i>Uphill</span><span><i style="background:rgba(111,178,232,.85)"></i>Downhill</span>
            <span><i style="background:rgba(216,185,138,.4)"></i>Corner</span>
            ${PHASES.map(([, , l, k]) => `<span><i style="background:${PHASE_FILL[k]}"></i>${l}</span>`).join('')}
          </div>
          <div class="rv-tip" hidden></div>
          <div class="rv-msg faint">Loading course map…</div>
        </div>
        <div class="rv-transport">
          <button class="btn icon-btn" data-rv="restart" title="Restart (Home)">${icon('restart')}</button>
          <button class="btn primary icon-btn" data-rv="play" title="Play / pause (Space)"></button>
          <span class="rv-time mono" data-rv-time></span>
          <div class="rv-scrub" data-rv-scrub title="Drag to scrub · ←/→ step 1 s">
            <div class="rv-scrub-phases">${phaseTimes.map(([a, b, l, k]) => `<span class="ph-${k}" style="left:${a / END * 100}%;width:${Math.max(0, b - a) / END * 100}%" title="${l}">${l}</span>`).join('')}</div>
            <div class="rv-scrub-ticks" data-rv-ticks></div>
            ${winner >= 0 ? `<i class="rv-scrub-goal" style="left:${results[winner].finish_time_raw / END * 100}%" title="Winner reaches the goal"></i>` : ''}
            <b class="rv-scrub-cursor" data-rv-cursor></b>
          </div>
          <div class="seg rv-rate">${SPEEDS.map((v) => `<button data-rv-rate="${v}" class="${v === rate ? 'on' : ''}">${v}×</button>`).join('')}</div>
        </div>
        <div class="rv-strip">${courseStrip(course, `<div class="cs-row"><div class="cs-label">Field</div><div class="cs-track rv-field" data-rv-field>${horses.map((_, i) =>
          `<i data-rv-dot="${i}" style="--c:${runnerColor(i)}" title="${esc(name(i))}"></i>`).join('')}</div></div>`)}</div>
      </div>
      <aside class="rv-side"><div class="rv-side-head"><div class="section">Standings</div><span class="faint" data-rv-lead></span></div>
        <div class="rv-standings" data-rv-standings></div></aside>
    </div>
    <div class="rv-lower">
      <section class="rw-card rv-runner" data-rv-runner></section>
      <section class="rw-card rv-feed"><div class="rw-card-head"><div class="section">Live feed</div>
        <label class="check"><input type="checkbox" data-rv-feed-sel> Selected runner only</label></div>
        <div class="rv-feed-list" data-rv-feed></div></section>
    </div>`;
  const $ = (q) => root.querySelector(q);
  const stage = $('[data-rv-stage]'), tip = stage.querySelector('.rv-tip');
  const dots = [...root.querySelectorAll('[data-rv-dot]')];

  function renderTicks() {
    $('[data-rv-ticks]').innerHTML = shownEvents.map((e) => {
      const mine = e.i === sel, cls = e.type === 3 ? 'sk ' + tierOf(e.skill) : 'ev';
      return `<i class="${cls} ${mine ? 'mine' : ''}" style="left:${e.t / END * 100}%;--c:${runnerColor(e.i)}" title="${esc(clock(e.t))} · ${esc(name(e.i))} · ${esc(e.type === 3 ? skillName(e.skill) : EVENT_NAMES[e.type] || 'Event')}"></i>`;
    }).join('');
  }
  function renderRunner() {
    const h = horses[sel], r = results[sel];
    const skills = [...new Set([...(h.skill_array || []).map((s) => s.skill_id), ...byRunner[sel].map((e) => e.skill)])];
    const stat = (k, l) => `<span><b>${(h[k] ?? (k === 'pow' ? h.power : 0)).toLocaleString()}</b>${l}</span>`;
    const dists = [['proper_distance_short', 'Sprint'], ['proper_distance_mile', 'Mile'], ['proper_distance_middle', 'Medium'], ['proper_distance_long', 'Long']];
    const styleApt = [null, 'proper_running_style_nige', 'proper_running_style_senko', 'proper_running_style_sashi', 'proper_running_style_oikomi'][r.running_style || h.running_style];
    $('[data-rv-runner]').innerHTML = `<div class="rv-rh">${gameIcon('card', h.card_id, 'lg')}
        <div class="grow"><div class="row"><i class="rv-dot" style="--c:${runnerColor(sel)}">${sel + 1}</i><b class="rv-rh-name ellipsis">${esc(name(sel))}</b></div>
          <div class="faint">${STYLES[r.running_style || h.running_style] || ''} · ${MOODS[h.motivation] || ''} mood · Gate ${sel + 1}
            · Finished ${r.finish_order + 1}${['st', 'nd', 'rd'][r.finish_order] || 'th'} in ${r.finish_time.toFixed(3)} s</div></div></div>
      <div class="rv-stats">${stat('speed', 'Speed')}${stat('stamina', 'Stamina')}${stat('pow', 'Power')}${stat('guts', 'Guts')}${stat('wiz', 'Wit')}</div>
      <div class="rv-apts"><span>Surface <b>${apt(course.surface === 2 ? h.proper_ground_dirt : h.proper_ground_turf)}</b></span>
        <span>Distance <b>${apt(h[dists[(course.distanceType || 1) - 1][0]])}</b></span>${styleApt ? `<span>Style <b>${apt(h[styleApt])}</b></span>` : ''}
        <span>Start delay <b>${r.start_delay_time.toFixed(3)} s</b></span><span>Spurt from <b>${r.last_spurt_start_distance >= 0 ? m(r.last_spurt_start_distance) : '—'}</b></span></div>
      <div class="rv-live" data-rv-live></div>
      <div class="rv-skills">${skills.map((id) => {
        const fired = byRunner[sel].filter((e) => e.skill === id);
        return `<div class="rv-sk skill-${tierOf(id)}" data-rv-sk="${id}">${gameIcon('skill', skillById.get(id)?.icon, 'sm')}
          <div class="grow"><b class="ellipsis">${esc(skillName(id))}</b><span class="faint">${fired.length ? fired.map((e) => e.dur < 0 ? 'Passive from the start'
            : `<button class="linkish" data-rv-seek="${e.t}">${clock(e.t)} · ${m(distAt(sel, e.t))}${e.dur > 0 ? ` · ${e.dur.toFixed(1)} s` : ''}</button>`).join(' ') : 'Did not activate'}</span></div>
          <span class="rv-sk-state" data-rv-skst></span><i class="rv-sk-bar" data-rv-skbar></i></div>`;
      }).join('') || '<p class="faint">No skills.</p>'}</div>`;
  }
  const seriesCache = new Map();
  function distAt(i, time) {
    if (!seriesCache.has(i)) seriesCache.set(i, frames.map((f) => [f.t, f.horses[i].distance]));
    const pts = seriesCache.get(i), p = pts.find((x) => x[0] >= time) || pts.at(-1);
    return Math.min(p[1], dist);
  }
  function updatePanels() {
    const { rows, order, lead } = snap;
    $('[data-rv-lead]').textContent = `Leader ${m(Math.min(lead.d, dist))} / ${m(dist)}`;
    $('[data-rv-standings]').innerHTML = order.map((r) => {
      const fin = r.done, gap = lead.d - r.d, sk = active(r.i, t).slice(-2);
      return `<div class="rv-st ${r.i === sel ? 'on' : ''}" data-rv-pick="${r.i}"><b class="rv-pos">${r.rank}</b><i class="rv-dot" style="--c:${runnerColor(r.i)}">${r.i + 1}</i>
        <span class="rv-st-name ellipsis">${esc(name(r.i))}</span>
        <span class="rv-st-skills">${sk.map((e) => `<span class="skill-${tierOf(e.skill)}" title="${esc(skillName(e.skill))}">${gameIcon('skill', skillById.get(e.skill)?.icon, 'xs')}</span>`).join('')}${r.rushed ? '<em class="rv-flag" title="Rushed">!</em>' : ''}</span>
        <span class="rv-st-gap mono">${fin ? results[r.i].finish_time.toFixed(2) + ' s' : r.rank === 1 ? `${r.v.toFixed(1)} m/s` : `+${(gap / LENGTH).toFixed(1)} L`}</span>
        <span class="rv-hpbar" title="HP ${Math.round(r.hp)}"><i style="width:${Math.max(0, Math.min(100, r.hp / (frames[0].horses[r.i].hp || 1) * 100))}%"></i></span></div>`;
    }).join('');
    const r = rows[sel], hp0 = frames[0].horses[sel].hp || 1;
    const phase = PHASES.find(([a, b]) => r.d / dist >= a && r.d / dist < b)?.[2] || 'Finished';
    const state = [r.done ? 'Finished' : '', r.spurt && !r.done ? 'Last spurt' : '', r.rushed ? 'Rushed' : '', r.block >= 0 && !r.done ? `Blocked by ${r.block + 1}` : ''].filter(Boolean);
    $('[data-rv-live]').innerHTML = `<span><b>${r.rank}${['st', 'nd', 'rd'][r.rank - 1] || 'th'}</b>Position</span><span><b>${m(Math.min(r.d, dist))}</b>Distance</span>
      <span><b>${r.v.toFixed(2)}</b>m/s</span><span><b>${(r.lane * 18).toFixed(1)}</b>Lane</span>
      <span class="rv-live-hp"><b>${Math.round(r.hp)} <small>/ ${hp0}</small></b><i class="rv-hpbar"><i style="width:${Math.max(0, r.hp / hp0 * 100)}%"></i></i></span>
      <span><b>${r.rank === 1 ? '—' : `+${((lead.d - r.d) / LENGTH).toFixed(1)} L`}</b>Behind leader</span><span><b>${esc(state.join(' · ') || phase)}</b>State</span>`;
    for (const row of root.querySelectorAll('[data-rv-sk]')) {
      const id = +row.dataset.rvSk, list = byRunner[sel].filter((e) => e.skill === id);
      const on = list.find((e) => e.dur >= 0 && e.t <= t && t <= e.t + Math.max(e.dur, 0.6));
      const passive = list.some((e) => e.dur < 0), fired = list.some((e) => e.t <= t);
      row.classList.toggle('active', !!on); row.classList.toggle('fired', fired && !on);
      row.querySelector('[data-rv-skst]').textContent = on ? (on.dur > 0 ? `Active · ${(on.t + on.dur - t).toFixed(1)} s` : 'Activated') : passive ? 'Passive' : fired ? 'Used' : list.length ? 'Waiting' : '—';
      row.querySelector('[data-rv-skbar]').style.width = on && on.dur > 0 ? `${(1 - (t - on.t) / on.dur) * 100}%` : '0';
    }
    const only = $('[data-rv-feed-sel]').checked;
    const past = shownEvents.filter((e) => e.t <= t && (!only || e.i === sel));
    const key = `${past.length}:${only}:${sel}`;
    if (key !== feedKey) {
      feedKey = key;
      $('[data-rv-feed]').innerHTML = past.slice(-80).reverse().map((e) => `<div class="rv-ev ${e.i === sel ? 'mine' : ''}" data-rv-seek="${e.t}" data-rv-pick="${e.i}">
          <span class="mono">${clock(e.t)}</span><span class="mono faint">${m(distAt(e.i, e.t))}</span>
          <span class="rv-ev-who"><i class="rv-dot sm" style="--c:${runnerColor(e.i)}">${e.i + 1}</i><span class="ellipsis">${esc(name(e.i))}</span></span>
          <span class="rv-ev-what">${e.type === 3 ? `${gameIcon('skill', skillById.get(e.skill)?.icon, 'xs')}<span class="skill-${tierOf(e.skill)} ellipsis">${esc(skillName(e.skill))}</span>${e.dur > 0 ? `<span class="faint">${e.dur.toFixed(1)} s</span>` : ''}`
            : `<span class="rv-ev-tag">${esc(EVENT_NAMES[e.type] || 'Event ' + e.type)}</span>`}</span></div>`).join('')
        || '<p class="faint">Skill activations and race events appear here as the race plays.</p>';
    }
  }
  function updateTransport() {
    $('[data-rv="play"]').innerHTML = icon(playing ? 'pause' : 'play');
    $('[data-rv-time]').textContent = `${clock(t)} / ${clock(END)}`;
    $('[data-rv-cursor]').style.left = `${t / END * 100}%`;
    const lead = snap.lead, ph = PHASES.find(([a, b]) => lead.d / dist >= a && lead.d / dist < b);
    $('[data-rv-hud]').innerHTML = `<b>${esc(ctx.courseName(info.race_instance_id))}</b><span>${ph ? ph[2] : 'Finished'} · Section ${Math.min(24, Math.floor(Math.min(lead.d, dist - 0.01) / dist * 24) + 1)}/24</span>
      <span class="faint">${m(Math.max(0, dist - lead.d))} to go</span>`;
    for (let i = 0; i < dots.length; i++) {
      dots[i].style.left = `${Math.max(0, Math.min(1, snap.rows[i].d / dist)) * 100}%`;
      dots[i].classList.toggle('on', i === sel);
    }
  }

  function target() {
    const [W, H] = map.size();
    if (mode === 'track') return map.view(geo.bounds, 30);
    if (mode === 'runner') { const [x, y] = geo.at(snap.rows[sel].d, snap.rows[sel].lane * COURSE_WIDTH); return { x, y, s: Math.max(W, H) / 160 }; }
    const running = snap.rows.filter((r) => !r.done);
    const pts = (running.length ? running : snap.rows).map((r) => geo.at(r.d, r.lane * COURSE_WIDTH));
    let [x0, y0, x1, y1] = [Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1]))];
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, hw = Math.max(45, (x1 - x0) / 2 + 25), hh = Math.max(30, (y1 - y0) / 2 + 25);
    const v = map.view([cx - hw, cy - hh, cx + hw, cy + hh], 20);
    return { ...v, s: Math.min(v.s, Math.max(W, H) / 70) };
  }
  function draw(dt) {
    const [W, H] = map.size(), g = map.g, dpr = map.dpr(), cam = map.cam;
    if (mode !== 'free') {
      const goal = target(), k = dt === null ? 1 : 1 - Math.exp(-dt * 5);
      cam.x += (goal.x - cam.x) * k; cam.y += (goal.y - cam.y) * k; cam.s = Math.exp(Math.log(cam.s) + (Math.log(goal.s) - Math.log(cam.s)) * k);
      if (Math.abs(goal.x - cam.x) * cam.s > 0.3 || Math.abs(goal.y - cam.y) * cam.s > 0.3 || Math.abs(goal.s / cam.s - 1) > 0.002) dirty = true;
    }
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    map.course(cam);
    const trail = [];
    for (let s = Math.max(0, t - 4); s <= t; s += 0.1) { const p = snapshotOne(sel, s); trail.push(geo.at(p.d, p.lane * COURSE_WIDTH)); }
    g.strokeStyle = runnerColor(sel); g.globalAlpha = 0.5; g.lineWidth = 3 / cam.s;
    g.beginPath(); trail.forEach((p, i) => i ? g.lineTo(...p) : g.moveTo(...p)); g.stroke(); g.globalAlpha = 1;

    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const taken = [];
    const r = Math.max(5, Math.min(15, 0.95 * cam.s));
    const pos = snap.rows.map((row) => map.toScreen(geo.at(row.d, row.lane * COURSE_WIDTH + 0.5)));
    const me = snap.rows[sel];
    if (me.block >= 0 && !me.done && pos[me.block]) {
      g.strokeStyle = 'rgba(224,138,122,0.8)'; g.lineWidth = 1.5; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(...pos[sel]); g.lineTo(...pos[me.block]); g.stroke(); g.setLineDash([]);
    }
    const drawOrder = [...snap.rows].sort((a, b) => a.d - b.d).filter((x) => x.i !== sel).concat([me]);
    for (const row of drawOrder) {
      const [x, y] = pos[row.i], i = row.i, color = runnerColor(i);
      if (x < -40 || y < -40 || x > W + 40 || y > H + 40) continue;
      const on = active(i, t);
      for (const e of on) {
        const age = t - e.t;
        if (e.dur === 0 || age < 0.6) {
          const p = Math.min(1, age / 0.8);
          g.strokeStyle = TIER[tierOf(e.skill)]; g.globalAlpha = 1 - p; g.lineWidth = 2.5;
          g.beginPath(); g.arc(x, y, r + 3 + p * 16, 0, Math.PI * 2); g.stroke(); g.globalAlpha = 1;
        }
      }
      const lasting = on.filter((e) => e.dur > 0);
      if (lasting.length) {
        g.shadowColor = TIER[tierOf(lasting.at(-1).skill)]; g.shadowBlur = 12;
        g.strokeStyle = TIER[tierOf(lasting.at(-1).skill)]; g.lineWidth = 2.5;
        g.beginPath(); g.arc(x, y, r + 2.5, 0, Math.PI * 2); g.stroke(); g.shadowBlur = 0;
      }
      const img = r >= 10 && horses[i].card_id ? image('card', horses[i].card_id) : null;
      g.fillStyle = color; g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
      if (img) {
        g.save(); g.beginPath(); g.arc(x, y, r - 2, 0, Math.PI * 2); g.clip();
        g.fillStyle = '#2a2433'; g.fill(); g.drawImage(img, x - r + 2, y - r + 2, (r - 2) * 2, (r - 2) * 2); g.restore();
      } else if (r >= 6) {
        g.fillStyle = '#18131f'; g.font = `800 ${Math.round(r * 1.05)}px Nunito, system-ui, sans-serif`;
        g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(String(i + 1), x, y + 0.5);
      }
      if (row.rushed) { g.fillStyle = '#e08a7a'; g.beginPath(); g.arc(x + r * 0.8, y - r * 0.8, 3.5, 0, Math.PI * 2); g.fill(); }
      if (i === sel || i === hoverRunner) {
        g.strokeStyle = '#fff'; g.lineWidth = 2; g.beginPath(); g.arc(x, y, r + (lasting.length ? 5.5 : 2), 0, Math.PI * 2); g.stroke();
        tag(g, `${row.rank}. ${name(i)}`, x, y + r + 14, taken, i === sel ? 'rgba(236,231,220,0.95)' : 'rgba(24,19,31,0.85)', i === sel ? '#1f1a27' : '#ece7dc');
      }
    }
    if (popups !== 'off') {
      const placed = [];
      for (let k = shownEvents.length - 1; k >= 0; k--) {
        const e = shownEvents[k], age = t - e.t;
        if (age < 0 || age > POPUP || (popups === 'sel' && e.i !== sel)) continue;
        const [x, y] = pos[e.i];
        g.globalAlpha = Math.min(1, (POPUP - age) / 0.4, age / 0.12);
        callout(g, e, x, y - r - 12, placed);
        g.globalAlpha = 1;
      }
      taken.push(...placed);
    }
    if (showLabels) map.labels(taken);
    if (mode !== 'track') minimap(W, H);
  }
  function snapshotOne(i, time) {
    const k = frameAt(time), a = frames[k].horses[i], b = frames[Math.min(k + 1, frames.length - 1)].horses[i];
    const h = frames[Math.min(k + 1, frames.length - 1)].t - frames[k].t, s = h > 0 ? (time - frames[k].t) / h : 0;
    return { d: a.distance + (b.distance - a.distance) * s, lane: a.lane_position + (b.lane_position - a.lane_position) * s };
  }
  function callout(g, e, x, y, placed) {
    const text = e.type === 3 ? skillName(e.skill) : EVENT_NAMES[e.type] || 'Event';
    const tier = e.type === 3 ? tierOf(e.skill) : '';
    g.font = '700 11px Nunito, system-ui, sans-serif';
    const label = text.length > 26 ? text.slice(0, 25) + '…' : text;
    const img = e.type === 3 ? image('skill', skillById.get(e.skill)?.icon) : null;
    const w = g.measureText(label).width + (img ? 30 : 14), h = 22, bx = x - w / 2;
    let by = y - h;
    while (by > 0 && placed.some((q) => bx < q[2] && bx + w > q[0] && by < q[3] && by + h > q[1])) by -= h + 3;
    placed.push([bx, by, bx + w, by + h]);
    if (by < y - h - 1) { g.strokeStyle = 'rgba(236,231,220,0.35)'; g.lineWidth = 1; g.beginPath(); g.moveTo(x, y + 6); g.lineTo(x, by + h); g.stroke(); }
    g.fillStyle = 'rgba(24,19,31,0.92)'; roundRect(g, bx, by, w, h, 11); g.fill();
    g.strokeStyle = e.type === 3 ? TIER[tier] : '#e3b56b'; g.lineWidth = 1.5; g.stroke();
    if (img) g.drawImage(img, bx + 4, by + 3, 16, 16);
    g.fillStyle = e.type === 3 ? TIER[tier] : '#e3b56b'; g.textAlign = 'left'; g.textBaseline = 'middle';
    g.fillText(label, bx + (img ? 24 : 7), by + h / 2 + 0.5);
  }
  function tag(g, text, x, y, taken, bg, fg) {
    g.font = '700 11px Nunito, system-ui, sans-serif';
    const w = g.measureText(text).width + 12, h = 18, bx = x - w / 2, by = y - h / 2;
    taken.push([bx, by, bx + w, by + h]);
    g.fillStyle = bg; roundRect(g, bx, by, w, h, 9); g.fill();
    g.fillStyle = fg; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(text, x, y + 0.5);
  }
  function minimap(W, H) {
    const g = map.g, dpr = map.dpr(), mw = Math.min(200, W * 0.32), mh = mw * 0.62, x0 = W - mw - 12, y0 = H - mh - 12;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = 'rgba(20,16,26,0.86)'; roundRect(g, x0, y0, mw, mh, 10); g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.12)'; g.lineWidth = 1; g.stroke();
    const [bx0, by0, bx1, by1] = geo.bounds;
    const s = Math.min((mw - 16) / (bx1 - bx0), (mh - 16) / (by1 - by0));
    const c = { x: (bx0 + bx1) / 2, y: (by0 + by1) / 2, s };
    const off = [x0 + mw / 2 - W / 2, y0 + mh / 2 - H / 2];
    g.save(); roundRect(g, x0, y0, mw, mh, 10); g.clip();
    map.course(c, true, ...off);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const row of snap.rows) {
      const [x, y] = map.toScreen(geo.at(row.d, row.lane * COURSE_WIDTH), c);
      g.fillStyle = runnerColor(row.i); g.beginPath(); g.arc(x + off[0], y + off[1], row.i === sel ? 3.5 : 2.2, 0, Math.PI * 2); g.fill();
    }
    const cam = map.cam, [vx, vy] = map.toScreen([cam.x - W / 2 / cam.s, cam.y - H / 2 / cam.s], c);
    g.strokeStyle = 'rgba(236,231,220,0.7)'; g.lineWidth = 1;
    g.strokeRect(vx + off[0], vy + off[1], W / cam.s * s, H / cam.s * s);
    g.restore();
  }

  function seek(time) { t = Math.max(0, Math.min(END, time)); invalidate(); }
  function invalidate() { dirty = true; }
  function loop(now) {
    if (!alive || !root.isConnected) { alive = false; return; }
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
    last = now;
    if (playing) { t += dt * rate; if (t >= END) { t = END; playing = false; } dirty = true; }
    if (dirty && map) {
      dirty = false;
      snap = snapshot(t);
      draw(dt || null);
      updateTransport();
      if (!playing || now - uiAt > 120) { uiAt = now; updatePanels(); }
    }
    raf = requestAnimationFrame(loop);
  }
  function setPlaying(on) {
    if (on && t >= END) t = 0;
    playing = on; invalidate();
  }
  function select(i) {
    if (i < 0 || i >= horses.length || i === sel) return;
    sel = i; feedKey = ''; renderRunner(); renderTicks(); invalidate();
  }

  const scrub = $('[data-rv-scrub]');
  scrub.onpointerdown = (e) => {
    scrub.setPointerCapture(e.pointerId);
    const move = (ev) => { const r = scrub.getBoundingClientRect(); seek((ev.clientX - r.left) / r.width * END); };
    move(e);
    scrub.onpointermove = move;
    scrub.onpointerup = () => { scrub.onpointermove = null; };
  };
  root.addEventListener('pointerdown', (e) => {
    const p = e.target.closest('[data-rv-pick], [data-rv-seek]');
    if (!p) return;
    if (p.dataset.rvSeek) seek(+p.dataset.rvSeek - 0.5);
    if (p.dataset.rvPick !== undefined) select(+p.dataset.rvPick);
  });
  root.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const d = b.dataset;
    if (d.rv === 'play') setPlaying(!playing);
    else if (d.rv === 'restart') { seek(0); setPlaying(true); }
    else if (d.rvRate) { rate = +d.rvRate; for (const x of root.querySelectorAll('[data-rv-rate]')) x.classList.toggle('on', x === b); }
    else if (d.rvCam) { mode = d.rvCam; for (const x of root.querySelectorAll('[data-rv-cam]')) x.classList.toggle('on', x === b); invalidate(); }
    else if (d.rvPop) { popups = d.rvPop; for (const x of root.querySelectorAll('[data-rv-pop]')) x.classList.toggle('on', x === b); invalidate(); }
    else if (d.rvLabels !== undefined) { showLabels = !showLabels; b.classList.toggle('on', showLabels); invalidate(); }
  });
  root.addEventListener('change', (e) => { if (e.target.matches('[data-rv-feed-sel]')) { feedKey = ''; invalidate(); } });
  const shell = $('.rv');
  shell.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea')) return;
    const step = e.shiftKey ? 5 : 1;
    if (e.key === ' ') setPlaying(!playing);
    else if (e.key === 'ArrowRight') seek(t + step);
    else if (e.key === 'ArrowLeft') seek(t - step);
    else if (e.key === 'Home') seek(0);
    else if (e.key === 'End') seek(END);
    else return;
    e.preventDefault();
  });

  function runnerUnder(sx, sy) {
    let best = -1, bd = 14 * 14;
    snap?.rows.forEach((row) => {
      const [x, y] = map.toScreen(geo.at(row.d, row.lane * COURSE_WIDTH + 0.5));
      const e = (x - sx) ** 2 + (y - sy) ** 2;
      if (e < bd) { bd = e; best = row.i; }
    });
    return best;
  }
  function freeCam() { mode = 'free'; for (const x of root.querySelectorAll('[data-rv-cam]')) x.classList.remove('on'); }
  stage.addEventListener('wheel', (e) => {
    if (!map) return;
    e.preventDefault();
    const r = stage.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top;
    const [wx, wy] = map.toWorld(sx, sy), f = Math.exp(-e.deltaY * 0.0015);
    freeCam();
    map.cam.s = Math.max(0.05, Math.min(40, map.cam.s * f));
    const [nx, ny] = map.toWorld(sx, sy);
    map.cam.x += wx - nx; map.cam.y += wy - ny; invalidate();
  }, { passive: false });
  stage.addEventListener('pointerdown', (e) => {
    if (!map || e.target.closest('button, .rv-hud')) return;
    const r = stage.getBoundingClientRect(), hit = runnerUnder(e.clientX - r.left, e.clientY - r.top);
    if (hit >= 0) { select(hit); return; }
    drag = { x: e.clientX, y: e.clientY, cx: map.cam.x, cy: map.cam.y, moved: false };
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', (e) => {
    if (!map) return;
    const r = stage.getBoundingClientRect(), sx = e.clientX - r.left, sy = e.clientY - r.top;
    if (drag) {
      if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 3) { drag.moved = true; freeCam(); }
      if (drag.moved) { map.cam.x = drag.cx - (e.clientX - drag.x) / map.cam.s; map.cam.y = drag.cy - (e.clientY - drag.y) / map.cam.s; invalidate(); }
      return;
    }
    const hit = runnerUnder(sx, sy);
    if (hit !== hoverRunner) { hoverRunner = hit; invalidate(); }
    stage.style.cursor = hit >= 0 ? 'pointer' : 'grab';
    let html = '';
    if (hit >= 0) {
      const row = snap.rows[hit];
      html = `<b>${row.rank}. ${esc(name(hit))}</b> · ${m(Math.min(row.d, dist))} · ${row.v.toFixed(2)} m/s · HP ${Math.round(row.hp)}`;
    } else {
      hoverD = geo.locate(...map.toWorld(sx, sy));
      if (hoverD !== null && hoverD <= dist) html = geo.describe(hoverD).map(esc).join(' · ');
    }
    tip.hidden = !html;
    if (html) { tip.innerHTML = html; tip.style.left = Math.min(sx + 14, r.width - 260) + 'px'; tip.style.top = sy + 16 + 'px'; }
  });
  stage.addEventListener('pointerup', () => { drag = null; });
  stage.addEventListener('pointerleave', () => { tip.hidden = true; if (hoverRunner >= 0) { hoverRunner = -1; invalidate(); } });
  stage.addEventListener('dblclick', () => { mode = 'pack'; root.querySelector('[data-rv-cam="pack"]').classList.add('on'); invalidate(); });

  const ro = new ResizeObserver(() => { if (map) { map.resize(); dirty = true; } });
  renderRunner(); renderTicks();
  snap = snapshot(0);
  act(null, 'Load course map', async () => {
    const track = await loadTrack(info.race_instance_id);
    if (!alive) return;
    geo = geometry(track, course);
    map = createMap(stage, geo);
    map.resize();
    const msg = stage.querySelector('.rv-msg'); msg.textContent = geo.exact ? '' : 'Schematic layout: no client path for this course.'; msg.classList.add('note');
    ro.observe(stage);
    Object.assign(map.cam, map.view(geo.bounds, 30));
    dirty = true;
  });
  updateTransport(); updatePanels();
  raf = requestAnimationFrame(loop);
  shell.focus({ preventScroll: true });
  return () => { alive = false; cancelAnimationFrame(raf); ro.disconnect(); };
}
