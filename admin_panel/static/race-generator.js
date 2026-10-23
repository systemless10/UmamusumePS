
import { esc, prefs } from './core.js';
import { APT_FIELDS, STATS, modal } from './widgets.js';
import { MAX_RUNNERS, newRacer, statTotal } from './racer-editor.js';

const SIZES = [9, 12, 18, 30, 60, 120];
const DEFAULTS = { size: 18, mode: 'add', source: 'random', statMin: 900, statMax: 1300, skillMin: 3, skillMax: 8,
  style: 'card', mood: 'normal', suited: true, distinct: true, order: 'random' };

const rand = (lo, hi) => lo + Math.floor(Math.random() * (hi - lo + 1));
const pick = (list) => list[Math.floor(Math.random() * list.length)];
function shuffle(list) {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [out[i], out[j]] = [out[j], out[i]]; }
  return out;
}

const skillPool = (skills) => skills.filter((s) => s.id >= 200000 && s.id < 300000 && s.rarity <= 2 && !/×$/.test(s.name));

function generated(o, cards, skills, course, count) {
  const fits = (c) => !course || ((c.apt?.[course.surface === 2 ? 1 : 0] ?? 7) >= 6 && (c.apt?.[1 + (course.distanceType || 3)] ?? 7) >= 6);
  let pool = o.suited ? cards.filter(fits) : cards;
  if (!pool.length) pool = cards;
  const seen = new Set(), pool2 = skillPool(skills), out = [];
  for (let n = 0; n < count; n++) {
    const fresh = o.distinct ? pool.filter((c) => !seen.has(c.chara_id)) : pool;
    const card = pick(fresh.length ? fresh : pool);
    seen.add(card.chara_id);
    const h = newRacer(card);
    for (const [key] of STATS) h[key] = rand(Math.min(o.statMin, o.statMax), Math.max(o.statMin, o.statMax));
    h.pow = h.power;
    const styles = APT_FIELDS.slice(6).map((_, i) => [i + 1, card.apt?.[6 + i] ?? 1]);
    const best = Math.max(...styles.map(([, v]) => v));
    h.running_style = o.style === 'random' ? rand(1, 4) : o.style === 'best' ? pick(styles.filter(([, v]) => v === best))[0] : card.style || 2;
    h.motivation = o.mood === 'random' ? rand(1, 5) : o.mood === 'great' ? 5 : 3;
    const groups = new Set();
    h.skill_array = shuffle(pool2).filter((s) => !groups.has(s.group) && groups.add(s.group))
      .slice(0, rand(Math.min(o.skillMin, o.skillMax), Math.max(o.skillMin, o.skillMax))).map((s) => ({ skill_id: s.id, level: 1 }));
    out.push(h);
  }
  return out;
}

export function openAutoFill(win, ctx) {
  const o = { ...DEFAULTS, ...prefs.get('raceAutoFill', {}) };
  o.size = Math.max(1, Math.min(MAX_RUNNERS, ctx.course?.gates && !prefs.get('raceAutoFill', null) ? ctx.course.gates : o.size));
  if (!ctx.roster.length && o.source === 'roster') o.source = 'random';
  const m = modal(win, `<div class="af" data-af></div>`, { wide: true });
  const root = m.box.querySelector('[data-af]');
  const seg = (key, pairs) => `<div class="seg wrap">${pairs.map(([v, l, dis]) => `<button type="button" class="${o[key] === v ? 'on' : ''}" data-k="${key}" data-v="${v}" ${dis ? 'disabled' : ''}>${esc(l)}</button>`).join('')}</div>`;
  const num = (key, lo, hi) => `<input type="number" class="num" min="${lo}" max="${hi}" data-n="${key}" value="${o[key]}">`;
  const adds = () => o.mode === 'replace' ? o.size : Math.max(0, o.size - ctx.runners.length);

  function render() {
    const source = ctx.runners[ctx.selected];
    root.innerHTML = `<header class="row"><div class="grow"><h3>Auto-fill field</h3>
        <p>Generate runners in bulk. Every runner stays editable afterwards.</p></div></header>
      <div class="af-grid">
        <div class="re-field"><span>Field size</span><div class="row wrap">${num('size', 1, MAX_RUNNERS)}
          <span class="chips">${SIZES.map((n) => `<button type="button" class="chipbtn sm ${o.size === n ? 'on' : ''}" data-size="${n}">${n}</button>`).join('')}</span>
          ${ctx.course?.gates ? `<span class="faint">This course has ${ctx.course.gates} gates; bigger fields need in-game checking.</span>` : ''}</div></div>
        <div class="re-field"><span>Existing runners</span>${seg('mode', [['add', `Keep ${ctx.runners.length}, add the rest`], ['replace', 'Replace the whole field']])}</div>
        <div class="re-field"><span>New runners from</span>${seg('source', [['random', 'Random umas'], ['roster', `Roster veterans (${ctx.roster.length})`, !ctx.roster.length],
          ['copy', source ? `Copies of runner ${ctx.selected + 1}` : 'Copies of a runner', !source]])}</div>
        ${o.source === 'random' ? `
          <div class="re-field"><span>Stats</span><div class="row">${num('statMin', 1, 32767)}<span class="faint">to</span>${num('statMax', 1, 32767)}<span class="faint">per stat, rolled independently</span></div></div>
          <div class="re-field"><span>Skills</span><div class="row">${num('skillMin', 0, 40)}<span class="faint">to</span>${num('skillMax', 0, 40)}<span class="faint">random white/gold skills each</span></div></div>
          <div class="re-field"><span>Running style</span>${seg('style', [['card', 'Uma default'], ['best', 'Best aptitude'], ['random', 'Random']])}</div>
          <div class="re-field"><span>Mood</span>${seg('mood', [['normal', 'Normal'], ['great', 'Great'], ['random', 'Random']])}</div>
          <div class="re-field"><span>Options</span><div class="checks">
            <label class="check"><input type="checkbox" data-c="suited" ${o.suited ? 'checked' : ''}> Only umas with B+ on this surface and distance</label>
            <label class="check"><input type="checkbox" data-c="distinct" ${o.distinct ? 'checked' : ''}> Avoid repeating characters</label></div></div>` : ''}
        ${o.source === 'roster' ? `<div class="re-field"><span>Pick</span>${seg('order', [['random', 'Random'], ['rating', 'Highest rating first'], ['stats', 'Highest stats first']])}</div>
          <p class="faint">${ctx.roster.length < adds() ? `The roster has ${ctx.roster.length} veterans, so some will appear more than once.` : 'Each veteran is used at most once.'}</p>` : ''}
      </div>
      <div class="modal-foot"><span class="grow faint">${adds() ? `Adds ${adds()} runner${adds() === 1 ? '' : 's'} · field becomes ${o.size}` : `Field already has ${ctx.runners.length} runners; ${o.mode === 'add' && ctx.runners.length > o.size ? `extra runners after ${o.size} are removed` : 'nothing to add'}`}</span>
        <button type="button" class="btn" data-x>Cancel</button><button type="button" class="btn primary" data-go>Fill field</button></div>`;
  }

  function build() {
    const keep = o.mode === 'replace' ? [] : ctx.runners.slice(0, o.size);
    const need = o.size - keep.length;
    let extra = [];
    if (o.source === 'copy') extra = Array.from({ length: need }, () => structuredClone(ctx.runners[ctx.selected]));
    else if (o.source === 'roster') {
      const sorted = o.order === 'rating' ? [...ctx.roster].sort((a, b) => Number(b.rank_score || 0) - Number(a.rank_score || 0))
        : o.order === 'stats' ? [...ctx.roster].sort((a, b) => statTotal(b) - statTotal(a)) : shuffle(ctx.roster);
      extra = Array.from({ length: need }, (_, i) => structuredClone(sorted[i % sorted.length]));
    } else extra = generated(o, ctx.cards, ctx.skills, ctx.course, need);
    return [...keep, ...extra];
  }

  root.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    e.stopPropagation();
    if (b.dataset.k) { o[b.dataset.k] = b.dataset.v; render(); }
    else if (b.dataset.size) { o.size = +b.dataset.size; render(); }
    else if (b.hasAttribute('data-x')) m.close();
    else if (b.hasAttribute('data-go')) {
      prefs.set('raceAutoFill', o);
      ctx.apply(build());
      m.close();
    }
  });
  root.addEventListener('change', (e) => {
    const t = e.target;
    e.stopPropagation();
    if (t.dataset.n) {
      if (!t.checkValidity()) { t.reportValidity(); return; }
      o[t.dataset.n] = +t.value;
      render();
    } else if (t.dataset.c) o[t.dataset.c] = t.checked;
  });
  render();
}
