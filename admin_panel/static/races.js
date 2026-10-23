
import { act, ago, call, esc, gameIcon, icon, loadExtra, store, toast } from './core.js';
import { STATS, STYLES, ask, empty, modal, targetPicker } from './widgets.js';
import { MAX_RUNNERS, newRacer, openRacerEditor, statOf, statTotal } from './racer-editor.js';
import { openVeteranSelector } from './veteran-selector.js';
import { openAutoFill } from './race-generator.js';
import { GROUNDS, SEASONS, WEATHERS, courseFacts, courseStrip, mountBreakdown, skillTier } from './race-breakdown.js';
import { mountCourseMap, mountViewer } from './race-viewer.js';

const CONDITIONS = [['season', 'Season', SEASONS], ['weather', 'Weather', WEATHERS], ['ground_condition', 'Track', GROUNDS],
  ['motivation', 'Default mood', ['Random', 'Awful', 'Bad', 'Normal', 'Good', 'Great']]];
const MAIN_RUNNERS = 3;
const TABS = [['race', 'Race'], ['runners', 'Runners'], ['results', 'Results'], ['replay', 'Replay']];
const savedAgo = (t) => { const ms = Date.parse(String(t).replace(' ', 'T')); return Number.isNaN(ms) ? esc(t) : ago(ms / 1000); };
const blankSettings = (courses) => ({ race_instance_id: courses[0]?.id || 0, season: 1, weather: 1, ground_condition: 1, motivation: 3 });

export const racesWin = {
  title: 'Race workshop', icon: 'flag', size: [760, 560], place: [0.08, 0.04, 0.88, 0.92],
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    body.classList.add('flush');
    let catalog = null, skills = [], cardById = new Map(), courseById = new Map(), epoch = 0;
    let view = 'library', tab = 'race';
    let draft = null, name = '', templateId = null, baseJSON = '', replay = null, dirty = false;
    let selected = 0, runnerQuery = '', courseQuery = '';
    let stopPane = null;
    const courses = new Map();

    const runnerName = (h) => cardById.get(h.card_id)?.name || (h.mob_id ? 'Mob ' + h.mob_id : 'Card ' + h.card_id);
    const courseName = (id) => courseById.get(id)?.name || `Race ${id}`;
    const snapshot = () => JSON.stringify({ name, draft });
    const unsaved = () => draft && snapshot() !== baseJSON;
    const isSaved = () => templateId !== null && !unsaved();

    async function refresh() {
      const token = ++epoch;
      let result, skillList;
      try {
        [result, skillList] = await Promise.all([call('races.list'), loadExtra('skills')]);
      } catch (ex) {
        if (token === epoch && !catalog) {
          body.innerHTML = `<div class="pane-scroll">${empty(esc(ex.message), `<button class="btn" data-a="refresh">${icon('refresh')} Retry</button>`)}</div>`;
        }
        throw ex;
      }
      if (token !== epoch) return;
      catalog = result; skills = skillList;
      cardById = new Map(catalog.cards.map((c) => [c.id, c]));
      courseById = new Map(catalog.courses.map((c) => [c.id, c]));
      render();
    }
    function course(id) {
      const c = courses.get(id);
      if (c && !(c instanceof Promise)) return c;
      if (!c) {
        const token = epoch;
        courses.set(id, call('races.course', { id }).then((p) => {
          courses.set(id, p);
          if (token === epoch && draft?.settings.race_instance_id === id) {
            body.querySelector('[data-course-profile]')?.replaceChildren(...profileNodes(p));
          }
        }).catch((ex) => {
          courses.delete(id);
          const el = body.querySelector('[data-course-profile]');
          if (el) el.innerHTML = `<p class="warn-t">${esc(ex.message)}</p>`;
        }));
      }
      return null;
    }
    function profileNodes(p) {
      const tmp = document.createElement('div');
      tmp.innerHTML = profileHTML(p);
      return [...tmp.childNodes];
    }

    function stopPaneView() { stopPane?.(); stopPane = null; }
    function render() {
      stopPaneView();
      if (!catalog) { body.innerHTML = `<div class="pane-scroll">${empty('Loading races…')}</div>`; return; }
      if (view === 'library') return renderLibrary();
      body.innerHTML = `<div class="rw-top pane-top">
          <div class="row rw-titlebar"><button class="btn sm" data-a="library">‹ Library</button>
            <input type="text" class="rw-name" data-name value="${esc(name)}" placeholder="Race name" spellcheck="false" aria-label="Race name">
            <span class="rw-status" data-status></span></div>
          <nav class="rw-steps">${TABS.map(([k, l], i) => `<button class="${tab === k ? 'on' : ''}" data-tab="${k}"><b>${i + 1}</b>${l}<span data-tab-note="${k}"></span></button>`).join('')}</nav>
        </div>
        <div class="pane-scroll rw-pane" data-pane></div>
        <div class="pane-foot rw-foot"><span class="grow faint" data-foot-note></span>
          <button class="btn" data-a="save">${icon('save')} Save to library</button>
          <button class="btn" data-a="run">${icon('bolt')} Simulate</button>
          <button class="btn primary" data-a="send">${icon('upload')} Send to game</button></div>`;
      renderPane();
    }
    function renderChrome() {
      const status = body.querySelector('[data-status]');
      if (!status) return;
      status.className = 'rw-status ' + (isSaved() ? 'ok' : 'warn');
      status.textContent = isSaved() ? 'Saved' : templateId === null ? 'Not in library' : 'Unsaved changes';
      body.querySelector('[data-tab-note="race"]').textContent = courseName(draft.settings.race_instance_id);
      body.querySelector('[data-tab-note="runners"]').textContent = `${draft.runners.length} / ${MAX_RUNNERS}`;
      body.querySelector('[data-tab-note="results"]').textContent = replay ? (dirty ? 'Out of date' : `Race #${replay.entry.practice_race_id}`) : 'Not simulated';
      body.querySelector('[data-tab-note="replay"]').textContent = replay ? 'Top-down race playback' : 'Not simulated';
      body.querySelector('[data-foot-note]').textContent = !draft.runners.length ? 'Add at least one runner to simulate.'
        : replay && !dirty ? 'Sending uses the exact simulated race shown in Results.' : 'Sending simulates the race first, so the game replays exactly what you can inspect.';
      for (const b of body.querySelectorAll('[data-a="run"], [data-a="send"]')) b.disabled = !draft.runners.length;
    }
    function renderPane() {
      const pane = body.querySelector('[data-pane]');
      pane.onclick = pane.onchange = null;
      stopPaneView();
      for (const b of body.querySelectorAll('[data-tab]')) b.classList.toggle('on', b.dataset.tab === tab);
      if (tab === 'race') { pane.innerHTML = raceTab(); mountMap(); }
      else if (tab === 'runners') pane.innerHTML = runnersTab();
      else if (!replay) pane.innerHTML = `<div class="rw-card">${empty('No simulation yet.', `<button class="btn primary" data-a="run" ${draft.runners.length ? '' : 'disabled'}>${icon('bolt')} Simulate this race</button>`)}</div>`;
      else if (tab === 'replay') {
        pane.innerHTML = `${dirty ? `<div class="rw-banner warn"><span class="grow">The race setup changed after this simulation. This replay still shows the last simulated race.</span><button class="btn sm primary" data-a="run">Re-simulate</button></div>` : ''}<div data-viewer></div>`;
        stopPane = mountViewer(pane.querySelector('[data-viewer]'), { replay, skills, courseName, runnerName });
      }
      else mountBreakdown(pane, { replay, skills, cards: catalog.cards, courseName, runnerName, dirty,
        onResimulate: (b) => act(b, 'Simulate race', () => simulate(b)),
        onWatch: () => { tab = 'replay'; renderPane(); } });
      renderChrome();
    }
    function changed({ pane = false } = {}) {
      if (replay) dirty = true;
      if (pane) renderPane(); else renderChrome();
    }

    function renderLibrary() {
      const shelf = catalog.saved, q = catalog.queued;
      const avatars = (runners) => `<span class="rw-stack">${runners.slice(0, 6).map((h) => gameIcon('card', h.card_id, 'sm')).join('')}${runners.length > 6 ? `<b>+${runners.length - 6}</b>` : ''}</span>`;
      body.innerHTML = `<div class="pane-top rw-lib-top"><div class="row"><div class="grow"><h2 class="rw-h">Race workshop</h2>
          <div class="faint">Build a race, simulate it, inspect every runner, then load it into the game.</div></div>
          <button class="btn icon-btn" data-a="refresh" title="Refresh">${icon('refresh')}</button>
          <button class="btn primary" data-a="new">${icon('plus')} New race</button></div></div>
        <div class="pane-scroll rw-lib">
          <section><div class="section-row row"><div class="section">Your races · ${catalog.templates.length}</div></div>
            ${catalog.templates.length ? `<div class="rw-lib-grid">${catalog.templates.map((t) => `<article class="rw-tpl" data-open-template="${t.id}" tabindex="0">
                <div class="rw-tpl-main"><b class="ellipsis">${esc(t.name)}</b><span class="faint ellipsis">${esc(courseName(t.definition.settings?.race_instance_id))}</span></div>
                <div class="row">${avatars(t.definition.runners)}<span class="faint">${t.definition.runners.length} runner${t.definition.runners.length === 1 ? '' : 's'}</span></div>
                <button class="wbtn rw-tpl-del" data-delete-template="${t.id}" title="Delete">${icon('trash')}</button></article>`).join('')}</div>`
              : `<div class="rw-empty"><b>No races yet</b><span class="faint">Create one to configure the course, conditions and runners.</span><button class="btn primary" data-a="new">${icon('plus')} New race</button></div>`}
          </section>
          <section><div class="section-row row"><div class="section">In game</div></div>
            <div class="rw-ingame">
              <div class="rw-slot-card ${q ? 'on' : ''}"><div class="rw-slot-icon">${icon('clock')}</div><div class="grow"><b>Next practice race</b>
                <div class="faint">${q ? `${esc(q.name)} · ${esc(courseName(q.race_instance_id))} · ${q.entry_num} runners — plays the next time this account starts a practice race.` : 'Nothing scheduled. Send a race here to play it on the next practice start.'}</div></div>
                ${q ? `<button class="btn sm" data-a="unqueue">Cancel</button>` : ''}</div>
              ${catalog.pending ? `<div class="rw-slot-card"><div class="rw-slot-icon">${icon('bolt')}</div><div class="grow"><b>Last run · #${catalog.pending.practice_race_id}</b>
                <div class="faint">${esc(courseName(catalog.pending.race_instance_id))} · ${catalog.pending.entry_num} runners · not saved to a slot</div></div>
                <button class="btn sm" data-inspect="${catalog.pending.practice_race_id}">Inspect</button></div>` : ''}
            </div>
            <div class="section-row row rw-slots-head"><div class="section">Practice race slots · ${shelf.length} / ${catalog.slot_limit || 5}</div></div>
            ${shelf.length ? `<div class="rw-slots">${shelf.map((r) => `<div class="rw-slot" data-inspect="${r.practice_race_id}">
                <span class="rw-slot-id">#${r.practice_race_id}</span>${r.is_favorite ? '<span class="stars" title="Favourite">★</span>' : ''}
                <div class="grow"><b class="ellipsis">${esc(r.name)}</b><div class="faint ellipsis">${esc(courseName(r.race_instance_id))} · ${r.entry_num} runners · ${savedAgo(r.save_time)}</div></div>
                <button class="btn sm" data-inspect="${r.practice_race_id}">Inspect</button>
                <button class="wbtn" data-delete-saved="${r.practice_race_id}" title="Delete from game">${icon('trash')}</button></div>`).join('')}</div>`
              : '<p class="faint">No saved practice races. Send a race to a slot, or save one in game.</p>'}
          </section>
        </div>`;
    }
    function openDraft(d, n, tid, rep = null) {
      draft = structuredClone(d);
      draft.settings ||= blankSettings(catalog.courses);
      draft.runners ||= [];
      name = n; templateId = tid; replay = rep; dirty = false; selected = 0; runnerQuery = ''; courseQuery = '';
      baseJSON = snapshot();
      tab = rep ? 'results' : 'race';
      view = 'editor';
      render();
    }

    function profileHTML(p) {
      if (!p) return '<p class="faint">Loading course…</p>';
      return `<div class="rw-chips">${courseFacts(p).map((f) => `<span class="chip">${esc(f)}</span>`).join('')}</div>${courseStrip(p)}
        ${draft.runners.length > p.gates ? `<p class="warn-t rw-note">${draft.runners.length} runners exceed this course's ${p.gates} gates. The simulation adds gates; check the race in game.</p>` : ''}`;
    }
    function mountMap() {
      stopPaneView();
      const el = body.querySelector('[data-course-map]');
      if (el) stopPane = mountCourseMap(el, draft.settings.race_instance_id);
    }
    function courseOptions() {
      const q = courseQuery.trim().toLowerCase(), cur = draft.settings.race_instance_id;
      const list = catalog.courses.filter((c) => c.id === cur || !q || c.name.toLowerCase().includes(q) || String(c.id).includes(q));
      return list.slice(0, 400).map((c) => `<option value="${c.id}" ${c.id === cur ? 'selected' : ''}>${esc(c.name)} · ${c.id}</option>`).join('');
    }
    function raceTab() {
      const s = draft.settings;
      return `<section class="rw-card"><div class="rw-card-head"><div class="section">Course</div></div>
          <div class="rw-course-pick"><label class="search">${icon('search')}<input type="text" data-course-q placeholder="Filter races by name or ID" value="${esc(courseQuery)}" spellcheck="false"></label>
            <select data-course>${courseOptions()}</select></div>
          <div data-course-profile>${profileHTML(course(s.race_instance_id))}</div>
          <div class="rw-card-head rv-map-head"><div class="section">Course map</div><span class="faint">Hover the track for distance, section, phase and slope</span></div>
          <div class="rv-map" data-course-map></div></section>
        <section class="rw-card"><div class="rw-card-head"><div class="section">Conditions</div></div>
          <div class="rw-conds">${CONDITIONS.map(([key, label, names]) => `<div class="re-field"><span>${label}</span><div class="seg wrap">${names.map((n, v) =>
            `<button class="${v === (s[key] ?? (key === 'motivation' ? 3 : 1)) ? 'on' : ''}" data-setting="${key}" data-v="${v}">${n}</button>`).join('')}</div></div>`).join('')}
            <div class="re-field"><span>Seed</span><div class="row"><input type="number" class="num wide" min="0" max="2147483647" step="1" data-seed value="${s.seed ?? ''}" placeholder="Random">
              <button class="btn sm" data-a="seed-roll">Roll</button>${s.seed != null ? '<button class="btn sm" data-a="seed-clear">Random each run</button>' : ''}
              <span class="faint">A fixed seed reproduces the same race for the same setup.</span></div></div></div>
          <details class="rw-adv"><summary>All settings (JSON)</summary><p class="faint">Also supports gate_assignment, single_mode and race_context. Conditions 0 = random.</p>
            <textarea class="code" data-settings-json rows="8" spellcheck="false">${esc(JSON.stringify(s, null, 2))}</textarea>
            <div class="row"><button class="btn sm" data-a="apply-settings">Apply JSON</button></div></details></section>
        <div class="rw-next"><button class="btn primary" data-tab="runners">Next: runners ›</button></div>`;
    }

    function runnerCard(h, i) {
      const card = cardById.get(h.card_id), equipped = h.skill_array || [];
      const bars = STATS.map(([key, label]) => `<i class="st-${key}" style="--v:${Math.min(100, statOf(h, key) / 20)}%" title="${label} ${statOf(h, key)}"></i>`).join('');
      return `<article class="rw-runner ${i === selected ? 'on' : ''}${h.main ? ' main' : ''}" data-edit="${i}" tabindex="0">
        <span class="rw-gate">${i + 1}</span>${gameIcon('card', h.card_id)}
        <div class="rw-runner-main"><b class="ellipsis">${esc(runnerName(h))}</b><span class="faint ellipsis">${esc(card?.title || '')}</span>
          <div class="rw-runner-meta"><span class="tag-s">${STYLES[h.running_style] || 'Pace'}</span><span class="faint">${statTotal(h).toLocaleString()} total</span></div>
          <div class="rw-bars">${bars}</div>
          <div class="rw-runner-skills">${equipped.slice(0, 7).map((s) => { const sk = skills.byId?.get(s.skill_id); return sk ? `<span class="skill-${skillTier(sk)}" title="${esc(sk.name)}">${gameIcon('skill', sk.icon, 'xs')}</span>` : ''; }).join('')}
            ${equipped.length > 7 ? `<span class="faint">+${equipped.length - 7}</span>` : equipped.length ? '' : '<span class="faint">No skills</span>'}</div></div>
        <div class="rw-runner-acts"><button class="wbtn rw-main ${h.main ? 'on' : ''}" data-main="${i}" title="${h.main ? 'Main runner (in-game Main slot) — click to unset' : 'Set as one of the 3 Main runners'}">${h.main ? '★' : '☆'}</button><button class="wbtn" data-dup="${i}" title="Duplicate">${icon('copy')}</button><button class="wbtn close" data-del="${i}" title="Remove">${icon('trash')}</button></div>
      </article>`;
    }
    function runnersTab() {
      const q = runnerQuery.trim().toLowerCase();
      const shown = draft.runners.map((h, i) => [h, i]).filter(([h]) => !q || runnerName(h).toLowerCase().includes(q) || (cardById.get(h.card_id)?.title || '').toLowerCase().includes(q));
      const full = draft.runners.length >= MAX_RUNNERS;
      return `<div class="rw-toolbar">
          <button class="btn primary" data-a="create" ${full || !catalog.cards.length ? 'disabled' : ''}>${icon('plus')} New runner</button>
          <button class="btn" data-a="import" ${full || !catalog.roster.length ? 'disabled' : ''}>${icon('uma')} Import veteran</button>
          <button class="btn" data-a="autofill">${icon('sparkle')} Auto-fill…</button>
          <span class="grow"></span>
          ${draft.runners.length > 8 ? `<label class="search rw-filter">${icon('search')}<input type="text" data-runner-q placeholder="Filter runners" value="${esc(runnerQuery)}" spellcheck="false"></label>` : ''}
          ${draft.runners.length ? `<button class="btn" data-a="clear">${icon('trash')} Clear</button>` : ''}</div>
        <div class="count-line"><span><b>${draft.runners.length}</b> / ${MAX_RUNNERS} runners</span>${q ? `<span>· ${shown.length} shown</span>` : ''}<span class="faint">Click a runner to edit stats, aptitudes and skills. ★ marks up to ${MAIN_RUNNERS} Main runners${draft.runners.some((h) => h.main) ? '' : ' (none set: the first ' + MAIN_RUNNERS + ' are Main)'}.</span></div>
        ${draft.runners.length ? `<div class="rw-runners">${shown.map(([h, i]) => runnerCard(h, i)).join('')}</div>`
          : `<div class="rw-empty"><b>No runners yet</b><span class="faint">Create a racer from any uma, import a trained veteran, or auto-fill a whole field.</span>
              <div class="row wrap"><button class="btn primary" data-a="create">${icon('plus')} New runner</button><button class="btn" data-a="import" ${catalog.roster.length ? '' : 'disabled'}>Import veteran</button>
              <button class="btn" data-a="autofill">${icon('sparkle')} Auto-fill…</button></div></div>`}
        ${draft.runners.length ? `<div class="rw-next"><button class="btn primary" data-a="run">${icon('bolt')} Simulate & view results</button></div>` : ''}`;
    }
    function refreshRunners() {
      if (tab !== 'runners') return renderChrome();
      const pane = body.querySelector('[data-pane]'), top = pane.scrollTop;
      const focus = document.activeElement?.matches?.('[data-runner-q]');
      pane.innerHTML = runnersTab();
      pane.scrollTop = top;
      if (focus) { const el = pane.querySelector('[data-runner-q]'); el?.focus(); el?.setSelectionRange(el.value.length, el.value.length); }
      renderChrome();
    }
    function addRunners(list) {
      const room = MAX_RUNNERS - draft.runners.length;
      if (room <= 0) throw Error(`A race holds at most ${MAX_RUNNERS} runners.`);
      draft.runners.push(...list.slice(0, room));
      selected = draft.runners.length - 1;
      changed();
      refreshRunners();
    }
    function editRunner(i) {
      selected = i;
      openRacerEditor(win, {
        runners: draft.runners, index: i, cards: catalog.cards, skills,
        changed: () => { changed(); refreshRunners(); },
        duplicate: (j) => { if (draft.runners.length >= MAX_RUNNERS) return j; draft.runners.splice(j + 1, 0, structuredClone(draft.runners[j])); changed(); refreshRunners(); return j + 1; },
        remove: (j) => { draft.runners.splice(j, 1); changed(); refreshRunners(); return draft.runners.length ? Math.min(j, draft.runners.length - 1) : -1; },
      });
    }

    async function simulate(btn) {
      if (!draft.runners.length) throw Error('Add at least one runner first.');
      const label = btn?.innerHTML;
      if (btn) btn.textContent = 'Simulating…';
      try {
        replay = await call('races.run', { name, definition: draft });
      } finally { if (btn?.isConnected) btn.innerHTML = label; }
      dirty = false; tab = 'results';
      refreshCatalogQuietly();
      render();
    }
    function refreshCatalogQuietly() {
      call('races.list').then((r) => { catalog = r; if (view === 'library') render(); }).catch(() => {});
    }
    async function saveTemplate() {
      const t = await call('races.save', { id: templateId, name: name || 'Custom race', definition: draft });
      templateId = t.id; draft = structuredClone(t.definition); baseJSON = snapshot();
      catalog.templates = [...catalog.templates.filter((x) => x.id !== t.id), t];
      renderChrome();
      toast(`Saved “${t.name}” to the library`);
    }
    function sendDialog() {
      const shelf = catalog.saved, limit = catalog.slot_limit || 5, q = catalog.queued;
      let dest = 'practice';
      const m = modal(win, `<h3>Send to game</h3><p>Load <b>${esc(name || 'this race')}</b> (${draft.runners.length} runners) into this account's practice races.</p>
        <div class="rw-dest">
          <button type="button" class="rw-dest-opt on" data-dest="practice">${icon('clock')}<div><b>Next practice race</b>
            <span>Plays automatically the next time a practice race starts.${q ? ` Replaces the scheduled “${esc(q.name)}”.` : ''}</span></div></button>
          <button type="button" class="rw-dest-opt" data-dest="saved">${icon('save')}<div><b>Practice race slot</b>
            <span>Appears in the saved races list to replay any time. ${shelf.length} / ${limit} used${shelf.length >= limit ? ' — the oldest non-favourite is dropped' : ''}.</span></div></button>
        </div>
        <div class="field" data-slot hidden><label>Slot</label><select data-overwrite><option value="0">New slot</option>${shelf.map((r) =>
          `<option value="${r.practice_race_id}">Replace #${r.practice_race_id} · ${esc(r.name)} · ${esc(courseName(r.race_instance_id))}</option>`).join('')}</select></div>
        ${replay && !dirty ? `<p class="faint">Uses simulated race #${replay.entry.practice_race_id}, exactly as shown in Results.</p>` : '<p class="faint">The race is simulated first; the game then replays that exact result.</p>'}
        <div class="modal-foot"><button class="btn" data-x>Cancel</button><button class="btn primary" data-go>${icon('upload')} Send</button></div>`);
      m.box.addEventListener('click', (e) => {
        const b = e.target.closest('button');
        if (!b) return;
        e.stopPropagation();
        if (b.dataset.dest) {
          dest = b.dataset.dest;
          for (const o of m.box.querySelectorAll('[data-dest]')) o.classList.toggle('on', o === b);
          m.box.querySelector('[data-slot]').hidden = dest !== 'saved';
        } else if (b.hasAttribute('data-x')) m.close();
        else if (b.hasAttribute('data-go')) act(b, 'Send race to game', async () => {
          const overwrite = +m.box.querySelector('[data-overwrite]').value;
          if (!replay || dirty) { replay = await call('races.run', { name, definition: draft }); dirty = false; }
          await call('races.inject', { id: replay.entry.practice_race_id, destination: dest, overwrite_id: dest === 'saved' ? overwrite : 0 });
          m.close();
          toast(dest === 'practice' ? 'Scheduled for the next practice race' : overwrite ? `Replaced practice race #${overwrite}` : 'Saved to a practice race slot');
          catalog = await call('races.list');
          if (tab === 'results') renderPane(); else renderChrome();
        });
      });
    }

    body.addEventListener('input', (e) => {
      const t = e.target;
      if (t.closest('.modal')) return;
      if (t.matches('[data-name]')) { name = t.value; renderChrome(); }
      else if (t.matches('[data-course-q]')) { courseQuery = t.value; body.querySelector('[data-course]').innerHTML = courseOptions(); }
      else if (t.matches('[data-runner-q]')) { runnerQuery = t.value; refreshRunners(); }
    });
    body.addEventListener('change', (e) => {
      const t = e.target;
      if (t.closest('.modal')) return;
      if (t.matches('[data-course]')) {
        draft.settings.race_instance_id = +t.value; changed();
        body.querySelector('[data-course-profile]').innerHTML = profileHTML(course(+t.value));
        mountMap();
        syncSettingsJSON();
      } else if (t.matches('[data-seed]')) {
        if (!t.checkValidity()) { t.reportValidity(); return; }
        draft.settings.seed = t.value === '' ? null : +t.value; changed({ pane: true });
      }
    });
    function syncSettingsJSON() {
      const ta = body.querySelector('[data-settings-json]');
      if (ta) ta.value = JSON.stringify(draft.settings, null, 2);
    }
    body.addEventListener('click', (e) => {
      const b = e.target.closest('button, [data-open-template], [data-inspect], [data-edit]');
      if (!b || b.closest('.modal') || b.closest('[data-rb-analysis], .rb-table-wrap, .rb-events, [data-viewer], [data-course-map]')) return;
      act(b.matches('button') ? b : null, 'Race workshop', async () => {
        const d = b.dataset;
        if (d.deleteTemplate) {
          e.stopPropagation();
          const t = catalog.templates.find((x) => x.id === +d.deleteTemplate);
          if (!await ask(win, { title: 'Delete race?', text: `“${esc(t?.name)}” is removed from your library. Races already sent to the game stay.`, ok: 'Delete', danger: true })) return;
          await call('races.delete_template', { id: +d.deleteTemplate });
          return refresh();
        }
        if (d.deleteSaved) {
          if (!await ask(win, { title: `Delete practice race #${d.deleteSaved}?`, text: 'It is removed from the in-game saved races list.', ok: 'Delete', danger: true })) return;
          await call('races.delete_saved', { id: +d.deleteSaved });
          return refresh();
        }
        if (d.openTemplate) {
          const t = catalog.templates.find((x) => x.id === +d.openTemplate);
          return openDraft(t.definition, t.name, t.id);
        }
        if (d.inspect) {
          const rep = await call('races.inspect', { id: +d.inspect });
          const entry = catalog.saved.find((r) => r.practice_race_id === +d.inspect);
          return openDraft(rep.definition, entry?.name || rep.entry.name || `Practice race #${d.inspect}`, null, rep);
        }
        if (d.edit !== undefined) return editRunner(+d.edit);
        if (d.main !== undefined) {
          e.stopPropagation();
          const h = draft.runners[+d.main];
          if (!h.main && draft.runners.filter((r) => r.main).length >= MAIN_RUNNERS) return toast(`Only ${MAIN_RUNNERS} Main runners — unset one first.`);
          if (h.main) delete h.main; else h.main = true;
          changed(); return refreshRunners();
        }
        if (d.dup !== undefined) { e.stopPropagation(); const c = structuredClone(draft.runners[+d.dup]); delete c.main; return addRunners([c]); }
        if (d.del !== undefined) { e.stopPropagation(); draft.runners.splice(+d.del, 1); changed(); return refreshRunners(); }
        if (d.tab) { tab = d.tab; return renderPane(); }
        if (d.setting) { draft.settings[d.setting] = +d.v; changed({ pane: true }); return; }
        switch (d.a) {
          case 'refresh': return refresh();
          case 'new': return openDraft({ settings: blankSettings(catalog.courses), runners: [] }, 'New race', null);
          case 'library':
            if (unsaved() && !await ask(win, { title: 'Leave without saving?', text: 'Changes to this race are not in your library yet.', ok: 'Discard changes', danger: true })) return;
            view = 'library'; draft = null; replay = null; render(); return refresh();
          case 'unqueue': await call('races.unqueue'); return refresh();
          case 'create': {
            const card = cardById.get(draft.runners[selected]?.card_id) || catalog.cards[0];
            addRunners([newRacer(card)]);
            return editRunner(draft.runners.length - 1);
          }
          case 'import': return openVeteranSelector(win, catalog.roster, catalog.cards, skills,
            (h) => act(null, 'Import veteran', async () => addRunners([h])));
          case 'autofill': {
            course(draft.settings.race_instance_id);
            const c = courses.get(draft.settings.race_instance_id);
            return openAutoFill(win, { runners: draft.runners, selected, cards: catalog.cards, skills, roster: catalog.roster,
              course: c instanceof Promise ? null : c, apply: (list) => { draft.runners = list; selected = 0; changed(); refreshRunners(); } });
          }
          case 'clear':
            if (!await ask(win, { title: 'Remove every runner?', text: `All ${draft.runners.length} runners are removed from this race.`, ok: 'Remove all', danger: true })) return;
            draft.runners = []; changed(); return refreshRunners();
          case 'seed-roll': draft.settings.seed = Math.floor(Math.random() * 2147483647); changed({ pane: true }); return;
          case 'seed-clear': draft.settings.seed = null; changed({ pane: true }); return;
          case 'apply-settings': {
            const value = JSON.parse(body.querySelector('[data-settings-json]').value);
            if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('Settings must be a JSON object.');
            draft.settings = value; changed({ pane: true }); return;
          }
          case 'save': return saveTemplate();
          case 'run': return simulate(b);
          case 'send': return sendDialog();
        }
      });
    });
    body.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[data-open-template], [data-edit]')) { e.preventDefault(); e.target.click(); }
    });

    const off = store.on((k) => {
      if (k === 'target') { epoch++; catalog = null; replay = null; draft = null; view = 'library'; courses.clear(); render(); act(null, 'Load races', refresh); }
    });
    render();
    act(null, 'Load races', refresh);
    return () => { epoch++; off(); stopPaneView(); };
  },
};
