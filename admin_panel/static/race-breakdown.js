
import { act, call, esc, gameIcon, icon } from './core.js';
import { STYLES } from './widgets.js';

export const SURFACES = { 1: 'Turf', 2: 'Dirt' };
export const TURNS = { 1: 'Right', 2: 'Left', 4: 'Straight' };
export const DISTANCES = { 1: 'Sprint', 2: 'Mile', 3: 'Medium', 4: 'Long' };
export const SEASONS = ['Random', 'Spring', 'Summer', 'Autumn', 'Winter', 'Cherry blossom'];
export const WEATHERS = ['Random', 'Sunny', 'Cloudy', 'Rainy', 'Snowy'];
export const GROUNDS = ['Random', 'Firm', 'Yielding', 'Soft', 'Heavy'];
const EVENT_NAMES = { 4: 'Spot struggle', 5: 'Dueling', 6: 'Conserve power released' };
export const PHASES = [[0, 1 / 6, 'Start', 'early'], [1 / 6, 2 / 3, 'Mid-race', 'mid'], [2 / 3, 5 / 6, 'Late-race', 'late'], [5 / 6, 1, 'Last spurt', 'spurt']];
const SERIES = { speed: 'Speed', hp: 'HP', lane: 'Lane' };

export const skillTier = (s) => !s ? '' : s.rarity >= 3 ? 'unique' : s.rarity === 2 ? 'gold' : 'white';
export const courseFacts = (c) => c ? [`${c.distance} m`, SURFACES[c.surface], DISTANCES[c.distanceType], TURNS[c.turn], c.gates ? `${c.gates} gates` : '']
  .filter(Boolean) : [];

const span = (a, b, dist, cls, label = '', title = label) =>
  `<div class="cs-seg ${cls}" style="left:${a / dist * 100}%;width:${Math.max(0.15, (b - a) / dist * 100)}%" title="${esc(title)}"><span>${esc(label)}</span></div>`;
const m = (v) => `${Math.round(v)}m`;

export function landmarks(c) {
  const d = c.distance, out = [[d / 2, 'Halfway']];
  const corner = c.corners?.at(-1), straight = c.straights?.at(-1);
  if (corner) out.push([corner.start, 'Final corner']);
  if (straight && straight.start > 0) out.push([straight.start, 'Final straight']);
  if (d > 1000) out.push([d - 777, '777 m left']);
  if (d > 400) out.push([d - 200, '200 m left']);
  return out.sort((a, b) => a[0] - b[0]);
}

function staggered(marks, d) {
  let prev = -1, row = 0;
  return marks.map(([x, l]) => { row = prev >= 0 && (x - prev) / d < 0.12 ? 1 - row : 0; prev = x; return [x, l, row]; });
}

export function courseStrip(c, extraRows = '') {
  if (!c?.distance) return '<p class="faint">No course geometry for this race.</p>';
  const d = c.distance;
  let h = 0, pts = [[0, 0]], last = 0;
  for (const s of [...(c.slopes || [])].sort((a, b) => a.start - b.start)) {
    pts.push([s.start, h]);
    h += s.length * s.slope / 1e6;
    pts.push([s.start + s.length, h]);
    last = s.start + s.length;
  }
  if (last < d) pts.push([d, h]);
  const lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
  const range = Math.max(1, hi - lo);
  const poly = pts.map(([x, y]) => `${(x / d * 1000).toFixed(1)},${(36 - (y - lo) / range * 30).toFixed(1)}`).join(' ');
  const corners = (c.corners || []).map((k, i, all) => span(k.start, k.start + k.length, d, 'corner',
    i === all.length - 1 ? 'Final corner' : 'Corner', `Corner ${m(k.start)}–${m(k.start + k.length)}`));
  const straights = (c.straights || []).map((s, i, all) => span(s.start, s.end, d, 'straight',
    i === all.length - 1 && s.start > 0 ? 'Final straight' : 'Straight', `Straight ${m(s.start)}–${m(s.end)}`));
  const slopes = (c.slopes || []).map((s) => span(s.start, s.start + s.length, d, s.slope > 0 ? 'up' : 'down',
    s.slope > 0 ? 'Uphill' : 'Downhill', `${s.slope > 0 ? 'Uphill' : 'Downhill'} ${(s.slope / 10000).toFixed(1)}% · ${m(s.start)}–${m(s.start + s.length)}`));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span style="left:${f * 100}%">${Math.round(d * f)}</span>`).join('');
  return `<div class="cs">
    <div class="cs-row"><div class="cs-label">Elevation <span class="faint">${range > 1 || hi > 0 ? `${(hi - lo).toFixed(1)} m` : 'flat'}</span></div>
      <div class="cs-track cs-elev"><svg viewBox="0 0 1000 40" preserveAspectRatio="none"><polygon points="0,40 ${poly} 1000,40"/><polyline points="${poly}"/></svg></div></div>
    <div class="cs-row"><div class="cs-label">Slope</div><div class="cs-track">${slopes.join('') || '<span class="cs-empty">Flat course</span>'}</div></div>
    <div class="cs-row"><div class="cs-label">Course</div><div class="cs-track">${straights.join('')}${corners.join('')}</div></div>
    <div class="cs-row"><div class="cs-label">Race phase</div><div class="cs-track">${PHASES.map(([a, b, l, k]) => span(a * d, b * d, d, 'ph-' + k, l, `${l} ${m(a * d)}–${m(b * d)}`)).join('')}</div></div>
    <div class="cs-row"><div class="cs-label">Section</div><div class="cs-track cs-sections">${Array.from({ length: 24 }, (_, i) => `<span>${i + 1}</span>`).join('')}</div></div>
    ${extraRows}
    <div class="cs-row"><div class="cs-label"></div><div class="cs-track cs-ticks">${ticks}</div></div>
    <div class="cs-marks"><div></div><div class="cs-marks-track">${staggered(landmarks(c), d).map(([x, l, r]) => `<i class="r${r}" style="left:${x / d * 100}%"><b>${esc(l)}</b></i>`).join('')}</div></div>
  </div>`;
}

export function mountBreakdown(root, ctx) {
  const { replay } = ctx;
  const skillById = new Map(ctx.skills.map((s) => [s.id, s]));
  const info = replay.entry.payload.race_result_info;
  const course = replay.course || {};
  const dist = course.distance || Math.max(...replay.frames.at(-1)?.horses.map((h) => h.distance) || [1]);
  const horses = replay.horses, results = replay.results;
  const order = horses.map((_, i) => i).sort((a, b) => results[a].finish_order - results[b].finish_order);
  const style = (i) => STYLES[results[i]?.running_style || horses[i].running_style] || '';
  const skillEvents = replay.events.filter((e) => e.type === 3);
  const eventsOf = (i) => skillEvents.filter((e) => e.params[0] === i);
  let sel = order[0] ?? 0, compare = -1, shown = { speed: true, hp: true, lane: false }, pinned = null, possible = new Map(), showPossible = false;

  const cache = new Map();
  function series(i) {
    if (!cache.has(i)) {
      cache.set(i, replay.frames.map((f) => {
        const h = f.horses[i];
        return { t: f.t, d: Math.min(h.distance, dist), v: h.speed, hp: h.hp, lane: h.lane_position, rushed: !!h.temptation_mode };
      }));
    }
    return cache.get(i);
  }
  const at = (pts, d) => {
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (pts[mid].d <= d) lo = mid; else hi = mid - 1; }
    return lo;
  };
  const distAt = (pts, t) => { const p = pts.find((p) => p.t >= t) || pts.at(-1); return p.d; };
  function stats(i) {
    const pts = series(i), r = results[i];
    const run = pts.filter((p) => p.d < dist);
    return {
      place: r.finish_order + 1, time: r.finish_time, diff: r.finish_diff_time, delay: r.start_delay_time,
      spurt: r.last_spurt_start_distance, top: Math.max(...pts.map((p) => p.v)),
      hpEnd: run.at(-1)?.hp ?? pts.at(-1)?.hp, hpMax: pts[0]?.hp || 1,
      rushed: run.length ? run.filter((p) => p.rushed).length / run.length : 0,
      skills: eventsOf(i).length,
    };
  }

  const skillIcon = (id, cls = 'xs') => { const s = skillById.get(id); return s ? gameIcon('skill', s.icon, cls) : '<span class="gicon xs missing"></span>'; };
  const skillName = (id) => skillById.get(id)?.name || `Skill ${id}`;
  const runnerLabel = (i) => `${esc(ctx.runnerName(horses[i]))}`;
  const placeBadge = (p) => `<span class="rb-place p${Math.min(p, 4)}">${p}</span>`;

  function header() {
    const w = order[0], ws = stats(w);
    const cond = [SEASONS[info.season], WEATHERS[info.weather], GROUNDS[info.ground_condition]].filter(Boolean);
    return `<div class="rb-head">
      <div class="rb-title"><div class="section">Race #${replay.entry.practice_race_id}${replay.entry.name ? ' · ' + esc(replay.entry.name) : ''}</div>
        <h2>${esc(ctx.courseName(info.race_instance_id))}</h2>
        <div class="rw-chips">${courseFacts(course).map((f) => `<span class="chip">${esc(f)}</span>`).join('')}
          ${cond.map((f) => `<span class="chip">${esc(f)}</span>`).join('')}<span class="chip mono" title="Random seed">Seed ${esc(info.random_seed)}</span>
          <span class="chip">${horses.length} runners</span></div></div>
      ${w !== undefined ? `<div class="rb-winner">${gameIcon('card', horses[w].card_id, 'lg')}<div><div class="section">Winner</div><b>${runnerLabel(w)}</b>
        <div class="faint">${ws.time.toFixed(3)} s · ${style(w)}</div></div></div>` : ''}
      ${ctx.onWatch ? `<button class="btn primary" data-rb="watch">${icon('play')} Watch replay</button>` : ''}
    </div>
    ${ctx.dirty ? `<div class="rw-banner warn"><span class="grow">The race setup changed after this simulation. The breakdown and anything sent to the game still use this replay.</span><button class="btn sm primary" data-rb="resim">Re-simulate</button></div>` : ''}`;
  }

  function table() {
    return `<div class="rb-table-wrap"><table class="rb-table"><thead><tr><th>Place</th><th>Runner</th><th>Gate</th><th>Style</th><th>Time</th><th>Margin</th><th>Start</th><th>Spurt at</th><th>HP left</th><th>Skills fired</th></tr></thead>
      <tbody>${order.map((i) => {
        const s = stats(i), ev = eventsOf(i);
        return `<tr class="${i === sel ? 'on' : ''}" data-rb-runner="${i}"><td>${placeBadge(s.place)}</td>
          <td><span class="rb-who">${gameIcon('card', horses[i].card_id, 'sm')}<span class="ellipsis">${runnerLabel(i)}</span></span></td>
          <td>${horses[i].frame_order}</td><td>${style(i)}</td><td class="num">${s.time.toFixed(3)}</td>
          <td class="num">${s.place === 1 ? '—' : '+' + s.diff.toFixed(3)}</td><td class="num">${s.delay.toFixed(3)}</td>
          <td class="num">${s.spurt >= 0 ? m(s.spurt) : '—'}</td>
          <td class="num"><span class="rb-hp"><i style="width:${Math.max(0, s.hpEnd / s.hpMax * 100)}%"></i></span>${Math.round(s.hpEnd)}</td>
          <td><span class="rb-fired">${ev.slice(0, 8).map((e) => `<span title="${esc(skillName(e.params[1]))}">${skillIcon(e.params[1])}</span>`).join('')}${ev.length > 8 ? `<span class="faint">+${ev.length - 8}</span>` : ''}</span></td></tr>`;
      }).join('')}</tbody></table></div>`;
  }

  function runnerOptions(cur, none) {
    return (none ? `<option value="-1">${none}</option>` : '') + order.map((i) =>
      `<option value="${i}" ${i === cur ? 'selected' : ''}>${results[i].finish_order + 1}. ${runnerLabel(i)} · gate ${horses[i].frame_order}</option>`).join('');
  }

  function analysis() {
    const s = stats(sel);
    return `<div class="rb-runner-head">${gameIcon('card', horses[sel].card_id, 'lg')}
        <div class="grow"><div class="row wrap"><select class="compact" data-rb-sel>${runnerOptions(sel)}</select>
          <span class="faint">vs</span><select class="compact" data-rb-compare>${runnerOptions(compare, 'No comparison')}</select></div>
          <div class="rb-kpis">
            <span><b>${placeBadge(s.place)}</b>Place</span><span><b>${s.time.toFixed(2)} s</b>Time</span><span><b>${s.top.toFixed(2)}</b>Top speed m/s</span>
            <span><b>${Math.round(s.hpEnd)}</b>HP at finish</span><span><b>${(s.rushed * 100).toFixed(0)}%</b>Rushed</span>
            <span><b>${s.spurt >= 0 ? m(s.spurt) : '—'}</b>Spurt start</span><span><b>${s.skills}</b>Skills fired</span></div></div></div>
      ${courseStrip(course)}
      <div class="rb-lanes-head"><div class="section">Skills</div>
        <label class="check"><input type="checkbox" data-rb-possible ${showPossible ? 'checked' : ''}> Show where each skill can activate</label></div>
      <div class="rb-lanes" data-rb-lanes>${lanes()}</div>
      <div class="rb-chart-head"><div class="seg">${Object.entries(SERIES).map(([k, l]) => `<button class="${shown[k] ? 'on' : ''}" data-rb-series="${k}"><i class="sw sw-${k}"></i>${l}</button>`).join('')}</div>
        <span class="faint">Hover to inspect · click to pin</span></div>
      <div class="rb-readout" data-rb-readout>${readout(null)}</div>
      <div class="rb-chart" data-rb-chart>${chart()}</div>`;
  }

  function lanes() {
    const equipped = (horses[sel].skill_array || []).map((s) => s.skill_id);
    const fired = eventsOf(sel).map((e) => e.params[1]);
    const ids = [...new Set([...equipped, ...fired])];
    if (!ids.length) return '<p class="faint">This runner had no skills.</p>';
    const pts = series(sel), win = possible.get(sel);
    return ids.map((id) => {
      const w = win?.find((x) => x.id === id);
      const obs = eventsOf(sel).filter((e) => e.params[1] === id).map((e) => {
        const a = distAt(pts, e.t), dur = e.params[2] / 10000;
        if (dur < 0) return `<i class="obs passive" style="left:0;width:100%" title="Passive from the start"></i>`;
        const b = dur > 0 ? distAt(pts, e.t + dur) : a;
        return `<i class="obs ${dur === 0 ? 'instant' : ''}" style="left:${a / dist * 100}%;width:${Math.max(0.6, (b - a) / dist * 100)}%"
          title="${esc(skillName(id))} · ${e.t.toFixed(2)} s · ${m(a)}${dur > 0 ? `–${m(b)} · ${dur.toFixed(1)} s` : ''}"></i>`;
      }).join('');
      const regions = showPossible && w ? w.regions.map(([a, b]) => `<i class="can" style="left:${a / dist * 100}%;width:${Math.max(0.4, (b - a) / dist * 100)}%" title="Can activate ${m(a)}–${m(b)}"></i>`).join('') : '';
      const note = showPossible && w ? (w.error ? `<em class="faint" title="${esc(w.error)}">unsupported</em>` : w.passive ? '<em class="faint">passive</em>' : !w.regions.length ? '<em class="faint">no window</em>' : '') : '';
      return `<div class="cs-row rb-lane ${fired.includes(id) ? '' : 'idle'}"><div class="cs-label skill-${skillTier(skillById.get(id))}">${skillIcon(id)}<span class="ellipsis" title="${esc(skillName(id))}">${esc(skillName(id))}</span>${note}</div>
        <div class="cs-track">${regions}${obs}</div></div>`;
    }).join('') + (showPossible && !win ? '<p class="faint">Loading activation windows…</p>' : '');
  }

  function chart() {
    const W = 1000, H = 240, runners = [sel, ...(compare >= 0 && compare !== sel ? [compare] : [])];
    const all = runners.map(series);
    const vs = all.flat().map((p) => p.v), lanesV = all.flat().map((p) => p.lane);
    const cruising = all.flat().filter((p) => p.d > dist * 0.05).map((p) => p.v);
    const vMin = Math.floor(Math.min(...(cruising.length ? cruising : vs)) - 1), vMax = Math.ceil(Math.max(...vs) + 0.5);
    const hpMax = Math.max(...all.map((pts) => pts[0]?.hp || 1)), laneMax = Math.max(0.1, ...lanesV);
    const x = (d) => (d / dist * W).toFixed(1);
    const y = { speed: (p) => H - (Math.max(p.v, vMin) - vMin) / (vMax - vMin) * (H - 10) - 4,
                hp: (p) => H - Math.max(0, p.hp) / hpMax * (H - 10) - 4, lane: (p) => H - p.lane / laneMax * (H - 10) - 4 };
    const path = (pts, k) => {
      let out = '', lastX = -1;
      for (const p of pts) { const px = x(p.d); if (px === lastX) continue; lastX = px; out += (out ? 'L' : 'M') + px + ',' + y[k](p).toFixed(1); }
      return out;
    };
    const lines = runners.flatMap((r, ri) => Object.keys(SERIES).filter((k) => shown[k]).map((k) =>
      `<path class="ln ln-${k} ${ri ? 'cmp' : ''}" d="${path(all[ri], k)}"/>`)).join('');
    const phases = PHASES.map(([a, b, , k]) => `<rect class="ph ph-${k}" x="${a * W}" y="0" width="${(b - a) * W}" height="${H}"/>`).join('');
    const marks = landmarks(course).map(([d]) => `<line class="mk" x1="${x(d)}" x2="${x(d)}" y1="0" y2="${H}"/>`).join('');
    const rush = all[0].reduce((acc, p, i, pts) => {
      if (p.rushed && (!i || !pts[i - 1].rushed)) acc.push([p.d, p.d]);
      if (p.rushed && acc.length) acc.at(-1)[1] = p.d;
      return acc;
    }, []).map(([a, b]) => `<rect class="rush" x="${x(a)}" y="${H - 4}" width="${Math.max(2, (b - a) / dist * W)}" height="4"><title>Rushed ${m(a)}–${m(b)}</title></rect>`).join('');
    const fired = eventsOf(sel).filter((e) => e.params[2] >= 0).map((e) => {
      const d = distAt(all[0], e.t);
      return `<line class="sk" x1="${x(d)}" x2="${x(d)}" y1="0" y2="${H}"/>`;
    }).join('');
    const axisL = shown.speed ? [0, 0.5, 1].map((f) => `<span style="top:${(1 - f) * 100}%">${(vMin + (vMax - vMin) * f).toFixed(1)}</span>`).join('') : '';
    const axisR = shown.hp ? [0, 0.5, 1].map((f) => `<span style="top:${(1 - f) * 100}%">${Math.round(hpMax * f)}</span>`).join('') : '';
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span style="left:${f * 100}%">${Math.round(dist * f)} m</span>`).join('');
    const band = `<div></div><div class="rb-band">
        <div class="rb-band-phases">${PHASES.map(([a, b, l, k]) => `<span class="ph-${k}" style="left:${a * 100}%;width:${(b - a) * 100}%" title="${l} ${m(a * dist)}–${m(b * dist)}">${l}</span>`).join('')}</div>
        <div class="rb-band-marks">${staggered(landmarks(course), dist).map(([d, l, r]) => `<b class="r${r}" style="left:${d / dist * 100}%" title="${m(d)}">${esc(l)}</b>`).join('')}</div>
      </div><div></div>`;
    return `${band}<div class="rb-axis l">${axisL}</div><div class="rb-plot">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${phases}${marks}${fired}${rush}${lines}</svg>
        <div class="rb-cross" data-rb-cross hidden></div><div class="rb-x">${ticks}</div></div><div class="rb-axis r">${axisR}</div>`;
  }

  function readout(d) {
    const rows = [sel, ...(compare >= 0 && compare !== sel ? [compare] : [])];
    const cells = rows.map((i, ri) => {
      if (d === null) return `<div class="rb-ro-row"><span class="rb-ro-who"><i class="sw ${ri ? 'cmp' : ''}"></i>${runnerLabel(i)}</span><span>—</span><span>—</span><span>—</span><span>—</span><span>—</span><span class="grow">—</span></div>`;
      const pts = series(i), p = pts[at(pts, d)];
      const frame = replay.frames[at(pts, d)];
      const place = 1 + frame.horses.filter((h) => h.distance > frame.horses[i].distance).length;
      const active = eventsOf(i).filter((e) => e.t <= p.t && (e.params[2] < 0 || e.t + e.params[2] / 10000 >= p.t)).map((e) => e.params[1]);
      const phase = PHASES.find(([a, b]) => p.d / dist >= a && p.d / dist < b)?.[2] || 'Finished';
      const state = [p.rushed ? 'Rushed' : '', results[i].last_spurt_start_distance >= 0 && p.d >= results[i].last_spurt_start_distance ? 'Spurting' : ''].filter(Boolean).join(' · ') || phase;
      return `<div class="rb-ro-row"><span class="rb-ro-who"><i class="sw ${ri ? 'cmp' : ''}"></i>${runnerLabel(i)}</span><span><b>${p.v.toFixed(2)}</b> m/s</span>
        <span><b>${p.t.toFixed(2)}</b> s</span><span><b>${m(p.d)}</b> · ${place}${['st', 'nd', 'rd'][place - 1] || 'th'}</span><span><b>${Math.round(p.hp)}</b> HP</span>
        <span>${esc(state)}</span><span class="grow rb-ro-skills">${active.map((id) => `<span title="${esc(skillName(id))}">${skillIcon(id)}${esc(skillName(id))}</span>`).join('') || '—'}</span></div>`;
    }).join('');
    return `<div class="rb-ro-row head"><span>Runner</span><span>Speed</span><span>Time</span><span>Position</span><span>HP</span><span>State</span><span class="grow">Active skills</span></div>${cells}`;
  }

  function events() {
    const rows = [...replay.events].sort((a, b) => a.t - b.t);
    return `<details class="rb-events"><summary>Race events · ${rows.length}</summary><div class="rb-ev-list">${rows.map((e) => {
      const i = e.params[0], pts = horses[i] ? series(i) : null;
      const what = e.type === 3 ? `${skillIcon(e.params[1])}<span>${esc(skillName(e.params[1]))}</span>${e.params[2] > 0 ? `<span class="faint">${(e.params[2] / 10000).toFixed(1)} s</span>` : e.params[2] < 0 ? '<span class="faint">passive</span>' : ''}`
        : `<span>${esc(EVENT_NAMES[e.type] || 'Event ' + e.type)}</span>`;
      return `<div class="rb-ev" data-rb-runner="${i}"><span class="mono">${e.t.toFixed(2)} s</span><span class="mono">${pts ? m(distAt(pts, e.t)) : ''}</span>
        <span class="rb-who">${horses[i] ? gameIcon('card', horses[i].card_id, 'xs') + runnerLabel(i) : 'Runner ' + (i + 1)}</span><span class="rb-what">${what}</span></div>`;
    }).join('') || '<p class="faint">No events recorded.</p>'}</div></details>`;
  }

  function render() {
    root.innerHTML = `${header()}
      <section class="rw-card"><div class="rw-card-head"><div class="section">Results</div><span class="faint">Click a runner to analyse it</span></div>${table()}</section>
      <section class="rw-card" data-rb-analysis>${analysis()}</section>
      <section class="rw-card">${events()}</section>`;
    bindChart();
  }
  function renderAnalysis() {
    root.querySelector('[data-rb-analysis]').innerHTML = analysis();
    for (const tr of root.querySelectorAll('tr[data-rb-runner]')) tr.classList.toggle('on', +tr.dataset.rbRunner === sel);
    bindChart();
  }
  function bindChart() {
    const plot = root.querySelector('.rb-plot');
    if (!plot) return;
    const cross = plot.querySelector('[data-rb-cross]');
    const show = (ev) => {
      const r = plot.getBoundingClientRect();
      const f = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
      cross.hidden = false; cross.style.left = f * 100 + '%';
      root.querySelector('[data-rb-readout]').innerHTML = readout(f * dist);
    };
    plot.onmousemove = (ev) => { if (!pinned) show(ev); };
    plot.onmouseleave = () => { if (!pinned) { cross.hidden = true; root.querySelector('[data-rb-readout]').innerHTML = readout(null); } };
    plot.onclick = (ev) => { pinned = !pinned; cross.classList.toggle('pinned', !!pinned); show(ev); };
  }
  async function loadPossible() {
    if (!showPossible || possible.has(sel)) return;
    const idx = sel;
    await act(null, 'Skill activation windows', async () => {
      possible.set(idx, await call('races.possible', { id: replay.entry.practice_race_id, runner: idx }));
      if (idx === sel) root.querySelector('[data-rb-lanes]').innerHTML = lanes();
    });
  }

  root.onclick = (e) => {
    const t = e.target.closest('[data-rb-runner], [data-rb-series], [data-rb]');
    if (!t) return;
    if (t.dataset.rbRunner !== undefined) { sel = +t.dataset.rbRunner; pinned = null; renderAnalysis(); loadPossible();
      if (t.matches('.rb-ev')) {
        const box = root.querySelector('[data-rb-analysis]');
        root.scrollTo({ top: root.scrollTop + box.getBoundingClientRect().top - root.getBoundingClientRect().top - 8, behavior: 'smooth' });
      } }
    else if (t.dataset.rbSeries) { shown[t.dataset.rbSeries] = !shown[t.dataset.rbSeries]; renderAnalysis(); }
    else if (t.dataset.rb === 'resim') ctx.onResimulate?.(t);
    else if (t.dataset.rb === 'watch') ctx.onWatch?.();
  };
  root.onchange = (e) => {
    const t = e.target;
    if (t.matches('[data-rb-sel]')) { sel = +t.value; pinned = null; renderAnalysis(); loadPossible(); }
    else if (t.matches('[data-rb-compare]')) { compare = +t.value; renderAnalysis(); }
    else if (t.matches('[data-rb-possible]')) { showPossible = t.checked; root.querySelector('[data-rb-lanes]').innerHTML = lanes(); loadPossible(); }
  };
  render();
}
