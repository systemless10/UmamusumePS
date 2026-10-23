
import { esc, gameIcon, icon } from './core.js';
import { APT_FIELDS, APT_LABELS, GRADE, STATS, STYLES, MOODS, modal } from './widgets.js';
import { skillTier } from './race-breakdown.js';

export const MAX_RUNNERS = 255;
const SKILL_TYPES = [['', 'All'], ['1', 'White'], ['2', 'Gold'], ['3', 'Unique']];
const STAT_PRESETS = [600, 900, 1200, 1600];
const APT_GROUPS = [['Surface', [0, 1]], ['Distance', [2, 3, 4, 5]], ['Running style', [6, 7, 8, 9]]];

export function newRacer(card) {
  return {card_id: card.id, chara_id: card.chara_id, rarity: 3, talent_level: 5,
    speed: 800, stamina: 800, power: 800, pow: 800, guts: 800, wiz: 800,
    running_style: card.style || 2, motivation: 3, skill_array: [],
    ...Object.fromEntries(APT_FIELDS.map((key, i) => [key, card.apt?.[i] || 7]))};
}

export const statOf = (h, key) => Number(h[key] ?? (key === 'power' ? h.pow : null) ?? 800);
export const statTotal = (h) => STATS.reduce((sum, [key]) => sum + statOf(h, key), 0);

export function openRacerEditor(win, ctx) {
  const cardById = new Map(ctx.cards.map((c) => [c.id, c]));
  const skillById = new Map(ctx.skills.map((s) => [s.id, s]));
  let index = ctx.index, picking = false, skillQuery = '', skillType = '', umaQuery = '';
  const m = modal(win, `<div class="re" data-re></div>`, { wide: true });
  m.box.classList.add('xl');
  const root = m.box.querySelector('[data-re]');
  const h = () => ctx.runners[index];
  const match = (q, ...values) => values.some((v) => String(v ?? '').toLowerCase().includes(q));

  function seg(field, pairs, value) {
    return `<div class="seg wrap">${pairs.map(([v, l]) => `<button type="button" class="${+v === +value ? 'on' : ''}" data-set="${field}" data-v="${v}">${esc(l)}</button>`).join('')}</div>`;
  }
  function aptSelect(i) {
    const key = APT_FIELDS[i], v = h()[key] ?? 7;
    const grades = [8, 7, 6, 5, 4, 3, 2, 1].map((g) => `<option value="${g}" ${g === +v ? 'selected' : ''}>${GRADE[g]}</option>`).join('')
      + ([9, 10].includes(+v) ? `<option value="${v}" selected>S+ (custom)</option>` : '');
    return `<label class="re-apt"><span>${APT_LABELS[i]}</span><select class="apt-sel g${Math.min(8, v)}" data-field="${key}">${grades}</select></label>`;
  }

  function render() {
    const r = h(), card = cardById.get(r.card_id), equipped = r.skill_array || [];
    root.innerHTML = `<header class="re-head">
        <button type="button" class="re-portrait" data-a="pick" title="Change uma or outfit">${gameIcon('card', r.card_id, 'lg')}<span>${icon('edit')}</span></button>
        <div class="grow"><div class="section">Runner ${index + 1} of ${ctx.runners.length}</div>
          <h3>${esc(card?.name || (r.mob_id ? 'Mob ' + r.mob_id : 'Custom racer'))}</h3><div class="faint">${esc(card?.title || '')}</div></div>
        <div class="row"><button type="button" class="btn icon-btn" data-a="prev" title="Previous runner" ${index ? '' : 'disabled'}>‹</button>
          <button type="button" class="btn icon-btn" data-a="next" title="Next runner" ${index < ctx.runners.length - 1 ? '' : 'disabled'}>›</button>
          <button type="button" class="wbtn close" data-a="close" title="Close">${icon('close')}</button></div>
      </header>
      ${picking ? `<section class="re-pick"><div class="row"><label class="search grow">${icon('search')}<input type="text" data-q="uma" placeholder="Search character, outfit or card ID" value="${esc(umaQuery)}" spellcheck="false"></label>
        <button type="button" class="btn" data-a="pick">Done</button></div><div class="re-gallery" data-gallery></div><p class="faint" data-gallery-count></p></section>` : ''}
      <div class="re-body">
        <div class="re-col">
          <section class="re-sec"><div class="section-row row"><div class="section">Stats</div><span class="faint">Total ${statTotal(r).toLocaleString()}</span></div>
            <div class="re-stats">${STATS.map(([key, label]) => `<label class="re-stat st-${key}"><span>${label}</span>
              <input type="number" min="1" max="32767" step="1" data-field="${key}" value="${statOf(r, key)}">
              <input type="range" min="1" max="2000" step="1" data-field="${key}" value="${Math.min(2000, statOf(r, key))}"></label>`).join('')}</div>
            <div class="row wrap re-presets"><span class="faint">Set all</span>${STAT_PRESETS.map((v) => `<button type="button" class="chipbtn sm" data-a="preset" data-v="${v}">${v}</button>`).join('')}</div></section>
          <section class="re-sec"><div class="section">Race setup</div>
            <div class="re-field"><span>Running style</span>${seg('running_style', Object.entries(STYLES), r.running_style ?? 2)}</div>
            <div class="re-field"><span>Mood</span>${seg('motivation', Object.entries(MOODS), r.motivation ?? 3)}</div>
            <div class="re-field"><span>Stars</span>${seg('rarity', [1, 2, 3, 4, 5].map((v) => [v, '★'.repeat(v)]), r.rarity ?? 3)}</div></section>
          <section class="re-sec"><div class="section">Aptitudes</div>
            ${APT_GROUPS.map(([label, idx]) => `<div class="re-field"><span>${label}</span><div class="re-apts">${idx.map(aptSelect).join('')}</div></div>`).join('')}</section>
        </div>
        <div class="re-col">
          <section class="re-sec"><div class="section-row row"><div class="section">Equipped skills · ${equipped.length}</div>
            ${equipped.length ? '<button type="button" class="linkish" data-a="clear-skills">Remove all</button>' : ''}</div>
            <div class="re-equipped">${equipped.map((s, i) => {
              const skill = skillById.get(s.skill_id), levels = skill && skill.rarity >= 3 ? [1, 2, 3, 4, 5, 6] : [1];
              if (!levels.includes(s.level ?? 1)) levels.push(s.level);
              return `<div class="re-skill skill-${skillTier(skill)}">${skill ? gameIcon('skill', skill.icon, 'sm') : '<span class="gicon sm missing"></span>'}
                <span class="grow ellipsis" title="${esc(skill?.name || '')}">${esc(skill?.name || `Skill ${s.skill_id}`)}</span>
                ${levels.length > 1 ? `<select class="compact" data-level="${i}">${levels.map((v) => `<option value="${v}" ${v === (s.level ?? 1) ? 'selected' : ''}>Lv ${v}</option>`).join('')}</select>` : ''}
                <button type="button" class="wbtn" data-remove-skill="${i}" title="Remove">${icon('close')}</button></div>`;
            }).join('') || '<p class="faint">No skills yet. Add some from the list below.</p>'}</div></section>
          <section class="re-sec re-browser"><div class="section">Add skills</div>
            <div class="row wrap"><label class="search grow">${icon('search')}<input type="text" data-q="skill" placeholder="Search skills" value="${esc(skillQuery)}" spellcheck="false"></label>
              <div class="seg">${SKILL_TYPES.map(([v, l]) => `<button type="button" class="${v === skillType ? 'on' : ''}" data-skill-type="${v}">${l}</button>`).join('')}</div></div>
            <div class="re-results" data-results></div><p class="faint" data-results-count></p></section>
        </div>
      </div>
      <details class="re-json"><summary>Advanced runner data (JSON)</summary><textarea class="code" data-json rows="10" spellcheck="false">${esc(JSON.stringify(r, null, 2))}</textarea>
        <div class="row"><button type="button" class="btn sm" data-a="apply-json">Apply JSON</button><span class="faint" data-json-err></span></div></details>
      <footer class="re-foot"><button type="button" class="btn" data-a="duplicate" ${ctx.runners.length >= MAX_RUNNERS ? 'disabled' : ''}>${icon('copy')} Duplicate</button>
        <button type="button" class="btn danger" data-a="remove" ${ctx.runners.length > 1 ? '' : 'disabled'}>${icon('trash')} Remove</button>
        <span class="grow"></span><button type="button" class="btn primary" data-a="close">Done</button></footer>`;
    results();
    if (picking) gallery();
  }

  function results() {
    const q = skillQuery.trim().toLowerCase(), owned = new Set((h().skill_array || []).map((s) => s.skill_id));
    const found = ctx.skills.filter((s) => !owned.has(s.id) && (!q || match(q, s.name, s.id))
      && (!skillType || (skillType === '3' ? s.rarity >= 3 : s.rarity === +skillType)));
    root.querySelector('[data-results]').innerHTML = found.slice(0, 120).map((s) =>
      `<button type="button" class="re-skill add skill-${skillTier(s)}" data-add-skill="${s.id}" title="${esc(s.name)} · ${s.id}">${gameIcon('skill', s.icon, 'sm')}<span class="ellipsis">${esc(s.name)}</span>${icon('plus')}</button>`).join('')
      || '<p class="faint">No matching skills.</p>';
    root.querySelector('[data-results-count]').textContent = found.length > 120 ? `Showing 120 of ${found.length} · search to narrow down` : `${found.length} skills`;
  }
  function gallery() {
    const q = umaQuery.trim().toLowerCase();
    const found = ctx.cards.filter((c) => !q || match(q, c.name, c.title, c.id));
    root.querySelector('[data-gallery]').innerHTML = found.slice(0, 90).map((c) =>
      `<button type="button" class="re-uma ${c.id === h().card_id ? 'on' : ''}" data-uma="${c.id}">${gameIcon('card', c.id)}<b class="ellipsis">${esc(c.name)}</b><span class="ellipsis">${esc(c.title)}</span></button>`).join('')
      || '<p class="faint">No matching umas.</p>';
    root.querySelector('[data-gallery-count]').textContent = found.length > 90 ? `Showing 90 of ${found.length} outfits · search to narrow down` : `${found.length} outfits`;
  }

  function update() {
    const scroll = m.box.scrollTop, focus = document.activeElement?.dataset?.q;
    render();
    m.box.scrollTop = scroll;
    if (focus) { const el = root.querySelector(`[data-q="${focus}"]`); el?.focus(); el?.setSelectionRange(el.value.length, el.value.length); }
  }
  const changed = () => { ctx.changed(); update(); };

  root.addEventListener('input', (e) => {
    const t = e.target;
    if (t.dataset.q === 'skill') { skillQuery = t.value; results(); }
    else if (t.dataset.q === 'uma') { umaQuery = t.value; gallery(); }
    else if (t.type === 'range') {
      const box = root.querySelector(`input[type=number][data-field="${t.dataset.field}"]`);
      box.value = t.value;
    }
  });
  root.addEventListener('change', (e) => {
    const t = e.target, r = h();
    if (t.dataset.field) {
      if (!t.checkValidity()) { t.reportValidity(); return; }
      r[t.dataset.field] = +t.value;
      if (t.dataset.field === 'power') r.pow = +t.value;
      changed();
    } else if (t.dataset.level !== undefined) { r.skill_array[+t.dataset.level].level = +t.value; changed(); }
  });
  root.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    e.stopPropagation();
    const r = h();
    if (b.dataset.set) { r[b.dataset.set] = +b.dataset.v; return changed(); }
    if (b.dataset.skillType !== undefined) { skillType = b.dataset.skillType; return update(); }
    if (b.dataset.addSkill) {
      r.skill_array ||= [];
      if (!r.skill_array.some((s) => s.skill_id === +b.dataset.addSkill)) r.skill_array.push({ skill_id: +b.dataset.addSkill, level: 1 });
      return changed();
    }
    if (b.dataset.removeSkill !== undefined) { r.skill_array.splice(+b.dataset.removeSkill, 1); return changed(); }
    if (b.dataset.uma) {
      const c = cardById.get(+b.dataset.uma);
      r.card_id = c.id; r.chara_id = c.chara_id;
      for (const key of ['mob_id', 'race_dress_id', 'race_cloth_id', 'single_mode_chara_id']) delete r[key];
      picking = false;
      return changed();
    }
    switch (b.dataset.a) {
      case 'close': return m.close();
      case 'pick': picking = !picking; update(); if (picking) root.querySelector('[data-q="uma"]')?.focus(); return;
      case 'prev': index = Math.max(0, index - 1); picking = false; return update();
      case 'next': index = Math.min(ctx.runners.length - 1, index + 1); picking = false; return update();
      case 'preset': for (const [key] of STATS) r[key] = +b.dataset.v; r.pow = r.power; return changed();
      case 'clear-skills': r.skill_array = []; return changed();
      case 'duplicate': index = ctx.duplicate(index); return update();
      case 'remove': { const next = ctx.remove(index); if (next < 0) return m.close(); index = next; return update(); }
      case 'apply-json':
        try {
          const value = JSON.parse(root.querySelector('[data-json]').value);
          if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('A runner must be a JSON object.');
          ctx.runners[index] = value;
          return changed();
        } catch (ex) { root.querySelector('[data-json-err]').textContent = ex.message; }
    }
  });
  render();
  return m;
}
