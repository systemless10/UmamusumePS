import { call, esc, gameIcon, icon, loadCatalog, loadExtra, store, toast, reportError, refreshCatalog } from "./core.js";
import { ask, opts, targetPicker } from "./widgets.js";

const TYPES = [["speed", "Speed"], ["stamina", "Stamina"], ["power", "Power"], ["guts", "Guts"], ["wiz", "Wisdom"]];
const TABS = [["identity", "Identity", "uma"], ["effects", "Bonuses", "bolt"], ["hints", "Hints", "sparkle"], ["events", "Events", "flag"], ["art", "Artwork", "cards"], ["review", "Review", "check"]];
const clone = (x) => structuredClone(x);
const rarity = (n) => ["", "R", "SR", "SSR"][n];
const levelValue = (values, level) => {
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i], lv = i * 5;
    if (v < 0) continue;
    if (lv === level) return v;
    if (lv > level) return prev ? Math.floor(prev[1] + (v - prev[1]) * (level - prev[0]) / (lv - prev[0])) : 0;
    prev = [lv, v];
  }
  return prev?.[1] ?? 0;
};

export const customSupportWin = {
  title: "Custom support cards", icon: "sparkle", size: [840, 580], place: [0.05, 0.025, 0.92, 0.94], key: "c",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    body.classList.add("flush", "csc-studio");
    let data = null, cat = null, skills = [], draft = null, original = null, tab = "identity", dirty = false;
    let selectedEffect = 0, search = "", skillQuery = "", message = "", busy = false, generation = 0;
    const target = () => ({ region: store.region, viewer: store.viewer });
    const published = (id) => data?.manifest?.cards.find((c) => c.id === id);
    const CROPS = {
      card_focus: { aspect: [3, 4], label: "CARD CROP", help: "Drag the frame over the part of the artwork the deck and list card should show." },
      icon_focus: { aspect: [1, 1], label: "ICON CROP", help: "Drag the square over the part of the artwork the small round icon should show." },
    };
    let artDims = null;
    const artSrc = (d) => `data:image/png;base64,${d.art.replace(/^data:image\/png;base64,/, "")}`;
    const dims = () => {
      if (!draft?.art) return null;
      if (artDims?.art === draft.art) return artDims;
      const img = new Image(), key = draft.art;
      img.onload = () => { artDims = { art: key, w: img.naturalWidth, h: img.naturalHeight }; if (draft?.art === key && tab === "art") render(); };
      img.src = artSrc(draft);
      return null;
    };
    const fullCrop = (key) => {
      const [aw, ah] = CROPS[key].aspect, { w, h } = artDims;
      const fw = Math.min(w, Math.floor(h * aw / ah));
      return [fw, Math.floor(fw * ah / aw)];
    };
    const cropSize = (key, f) => fullCrop(key).map((v) => v * f.size / 1000);
    const focusOf = (key) => {
      if (draft[key]) return draft[key];
      const { w, h } = artDims, [fw, fh] = fullCrop(key);
      if (key === "icon_focus") return { x: Math.floor((w - fw * 0.6) / 2 * 1000 / w), y: 50, size: 600 };
      return { x: Math.floor((w - fw) / 2 * 1000 / w), y: 0, size: 1000 };
    };
    const focusStyle = (key, f) => {
      const { w, h } = artDims, [cw, ch] = cropSize(key, f);
      return `left:${f.x / 10}%;top:${f.y / 10}%;width:${cw / w * 100}%;height:${ch / h * 100}%`;
    };
    const clampFocus = (key, f) => {
      const { w, h } = artDims, size = Math.max(100, Math.min(1000, Math.round(f.size)));
      const [cw, ch] = cropSize(key, { size });
      return { x: Math.round(Math.max(0, Math.min(1000 - cw / w * 1000, f.x))), y: Math.round(Math.max(0, Math.min(1000 - ch / h * 1000, f.y))), size };
    };
    const focusPreview = (key, f) => {
      const { w, h } = artDims, [cw, ch] = cropSize(key, f);
      const px = cw >= w ? 0 : f.x / 10 / (1 - cw / w), py = ch >= h ? 0 : f.y / 10 / (1 - ch / h);
      return `<div style="background-image:url('${esc(artSrc(draft))}');background-size:${w / cw * 100}% ${h / ch * 100}%;background-position:${px}% ${py}%"></div>`;
    };
    const focusEditor = (key) => {
      if (!draft.art) return "";
      const c = CROPS[key];
      if (!dims()) return `<div class="csc-focus"><span class="csc-eyebrow">${c.label}</span><p>Loading artwork…</p></div>`;
      const f = focusOf(key);
      return `<div class="csc-focus"><span class="csc-eyebrow">${c.label}</span>
        <div class="csc-focus-row"><div class="csc-focus-stage" data-focus-stage="${key}"><img src="${esc(artSrc(draft))}" alt="" draggable="false"><div class="csc-focus-box" data-focus-box="${key}" style="${focusStyle(key, f)}"></div></div>
        <div class="csc-focus-side"><div class="csc-focus-preview" style="aspect-ratio:${c.aspect[0]}/${c.aspect[1]}" data-focus-preview="${key}">${focusPreview(key, f)}</div>
        <label class="csc-field">Zoom<input type="range" min="100" max="1000" step="10" value="${1100 - f.size}" data-focus-zoom="${key}"></label>
        <button class="btn sm" data-action="reset-focus" data-crop="${key}">Reset</button></div></div>
        <p class="csc-focus-help">${c.help} The rarity frame${key === "icon_focus" ? " and badge are" : " is"} added when the pack is published.</p></div>`;
    };
    const setFocus = (key, f) => {
      draft[key] = clampFocus(key, f); dirtyMark();
      const box = body.querySelector(`[data-focus-box="${key}"]`); if (box) box.style.cssText = focusStyle(key, draft[key]);
      const pv = body.querySelector(`[data-focus-preview="${key}"]`); if (pv) pv.innerHTML = focusPreview(key, draft[key]);
    };
    body.addEventListener("pointerdown", (e) => {
      const stage = e.target.closest("[data-focus-stage]"); if (!stage || !draft?.art || !artDims) return;
      e.preventDefault();
      const key = stage.dataset.focusStage, rect = stage.getBoundingClientRect(), start = focusOf(key);
      const box = stage.querySelector("[data-focus-box]").getBoundingClientRect();
      const inside = e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
      const off = inside ? [e.clientX - box.left, e.clientY - box.top] : [box.width / 2, box.height / 2];
      const move = (ev) => setFocus(key, { ...start, x: (ev.clientX - off[0] - rect.left) / rect.width * 1000, y: (ev.clientY - off[1] - rect.top) / rect.height * 1000 });
      move(e);
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    });
    const art = (d, cls = "") => d?.art
      ? `<img class="${cls}" src="data:image/png;base64,${esc(d.art.replace(/^data:image\/png;base64,/, ""))}" alt="Custom card artwork">`
      : gameIcon("supportart", d?.art_donor || d?.template, cls);
    const dirtyMark = () => {
      dirty = true; message = "";
      const el = body.querySelector("[data-dirty]");
      if (el) el.textContent = "Unsaved draft";
      updatePreview();
    };
    async function run(fn) {
      if (busy) return;
      busy = true; body.classList.add("csc-busy");
      try { await fn(target()); } catch (e) { message = e.message; reportError("Custom support studio", e); render(); }
      finally { busy = false; body.classList.remove("csc-busy"); }
    }
    async function load(keep = false) {
      const request = ++generation, t = target();
      try {
        const [d, c, s] = await Promise.all([call("custom_support.list", {}, t), loadCatalog(t.region), loadExtra("skills", t.region)]);
        if (request !== generation) return;
        data = d; cat = c; skills = s;
        if (!keep) { draft = null; original = null; dirty = false; tab = "identity"; }
        render();
      } catch (e) { if (request === generation) body.innerHTML = `<div class="csc-empty"><span>${icon("alert")}</span><h2>Studio unavailable</h2><p>${esc(e.message)}</p><button class="btn" data-action="reload">Try again</button></div>`; }
    }
    async function leave() {
      return !dirty || await ask(win, { title: "Discard this draft's unsaved changes?", text: "Your last saved version will remain in the library.", ok: "Discard changes", danger: true });
    }
    win.beforeClose = leave;
    function choose(d) {
      draft = clone(d); original = clone(d); dirty = false; selectedEffect = 0; message = ""; tab = "identity"; render();
    }
    function label(id) { const c = cat?.supportById.get(+id); return c ? `${c.name} · ${c.title} · ${id}` : `Card ${id}`; }
    function selectCards(value, attr, filter = () => true) {
      const cards = (cat?.supports.cards || []).filter((c) => !c.custom && filter(c));
      return `<select ${attr}>${opts(cards.map((c) => [c.id, label(c.id)]), value)}</select>`;
    }
    function preview() {
      if (!draft) return "";
      const cap = [0, 40, 45, 50][draft.rarity];
      const top = draft.effects.filter((e) => levelValue(e.values, cap) > 0).slice(0, 4);
      return `<div class="csc-preview-art">${art(draft, "csc-full-art")}<span class="csc-rarity">${rarity(draft.rarity)}</span><span class="csc-art-type t-${esc(draft.type)}">${esc(TYPES.find(([k]) => k === draft.type)?.[1])}</span></div>
        <div class="csc-preview-copy"><span class="csc-eyebrow">LIVE PREVIEW · LV ${cap}</span><h2>${esc(draft.name || "Untitled card")}</h2><p>${esc(draft.title || "Give your card a title")}</p>
        <div class="csc-preview-stats">${top.map((e) => `<span>${esc(data.effects[e.type])}<b>+${levelValue(e.values, cap)}</b></span>`).join("")}</div>
        <div class="csc-preview-id">${icon("lock")} ${draft.id ? `Custom ID ${draft.id}` : "New ID allocated on save"}</div></div>`;
    }
    function updatePreview() { const p = body.querySelector("[data-preview]"); if (p) p.innerHTML = preview(); }
    function content() {
      if (!draft) return `<div class="csc-empty"><span>${icon("sparkle")}</span><h2>Custom support cards</h2><p>Start from an existing card, give it its own identity,<br>and shape its bonuses and hints.</p><button class="btn primary" data-action="new">${icon("plus")} Create a card</button><div class="csc-empty-note">Completely optional. Custom cards stay hidden<br>until the matching client plugin is ready.</div></div>`;
      if (tab === "identity") return `<div class="csc-section-head"><h2>Identity</h2><p>Keep an existing character and give this support a new name and title.</p></div>
        <div class="csc-form-grid"><label>Name<input maxlength="100" data-field="name" value="${esc(draft.name)}" placeholder="Featured character"></label><label>Card title<input maxlength="150" data-field="title" value="${esc(draft.title)}" placeholder="A little spark of inspiration"></label>
        <label>Rarity<select data-field="rarity" ${draft.id ? "disabled" : ""}>${opts([[1, "R"], [2, "SR"], [3, "SSR"]], draft.rarity)}</select></label><label>Training type<select data-field="type">${opts(TYPES, draft.type)}</select></label>
        <label class="csc-span">Description<textarea data-field="description" maxlength="2000" rows="4" placeholder="The story behind this support…">${esc(draft.description)}</textarea></label></div>
        <div class="csc-note">${icon("uma")}<div><b>Character #${draft.chara_id}</b><p>Based on ${esc(label(draft.template))}. Character identity is inherited so existing models and events remain compatible.</p></div></div>`;
      if (tab === "effects") {
        const effect = draft.effects[selectedEffect];
        const cap = [0, 40, 45, 50][draft.rarity];
        return `<div class="csc-section-head"><h2>Bonuses</h2><p>Edit the values at each level. A blank breakpoint inherits interpolation from its neighbours.</p></div>
          <div class="csc-effects-layout"><div class="csc-effect-list">${draft.effects.map((e, i) => `<button class="${i === selectedEffect ? "on" : ""}" data-effect="${i}"><span>${esc(data.effects[e.type])}</span><b>+${levelValue(e.values, cap)}</b></button>`).join("")}
          <select aria-label="Add an effect" data-add-effect><option value="">+ Add a bonus</option>${Object.entries(data.effects).filter(([k]) => !draft.effects.some((e) => e.type === +k)).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></div>
          <div class="csc-effect-detail">${effect ? `<div class="csc-effect-heading"><h3>${esc(data.effects[effect.type])}</h3><button class="btn sm" data-action="remove-effect" aria-label="Remove effect">${icon("trash")}</button></div><div data-curve>${curve(effect)}</div>
          <div class="csc-breakpoints">${effect.values.map((v, i) => `<label>${i ? `Lv ${i * 5}` : "Initial"}<input type="number" min="0" max="10000" aria-label="${esc(data.effects[effect.type])} at level ${i * 5}" data-breakpoint="${i}" value="${v < 0 ? "" : v}" placeholder="—"></label>`).join("")}</div>
          <div class="csc-cap-strip">${[0, 1, 2, 3, 4].map((lb) => { const lv = [0, 20, 25, 30][draft.rarity] + lb * 5; return `<span>${lb}/4 uncap · Lv${lv}<b>+${levelValue(effect.values, lv)}</b></span>`; }).join("")}</div>` : `<p>Select or add a bonus to edit its curve.</p>`}</div></div>
          <div class="csc-note">${icon("sparkle")}<div><b>Unique effect</b><p>${draft.unique ? `Inherited from the template, unlocking at level ${draft.unique.lv}. Its existing behavior is preserved.` : "No unique effect inherited from this template."}</p>${draft.unique ? '<button class="linkish" data-action="remove-unique">Remove inherited unique effect</button>' : ""}</div></div>`;
      }
      if (tab === "hints") {
        const selected = new Set(draft.hints.filter((h) => !h.hint_gain_type).map((h) => h.hint_value_1));
        const results = skills.filter((s) => !selected.has(s.id) && `${s.name} ${s.id}`.toLowerCase().includes(skillQuery.toLowerCase())).slice(0, 40);
        return `<div class="csc-section-head"><h2>Hints</h2><p>Choose existing skills available in ${store.region === "jp" ? "JP" : "Global"}. These become the displayed and actual hint pool.</p></div>
          <div class="csc-hint-head"><h3>Skill hints</h3><span class="csc-badge">${selected.size} / 20</span></div><div class="csc-hint-chips">${[...selected].map((id) => `<button data-remove-hint="${id}" title="Remove hint">${esc(skills.byId?.get(id)?.name || id)} ${icon("close")}</button>`).join("") || '<p class="faint">No skill hints yet. Add one below.</p>'}</div>
          <label class="csc-skill-search">${icon("search")}<input data-skill-search value="${esc(skillQuery)}" placeholder="Find a skill by name or ID" aria-label="Search skills"></label><div class="csc-skill-results" data-skill-results>${skillResults(results, selected.size)}</div>
          <div class="csc-hint-head"><h3>Stat hints</h3><button class="btn sm" data-action="add-stat">${icon("plus")} Add group</button></div>
          <div class="csc-stat-hints">${draft.hints.map((h, i) => h.hint_gain_type ? `<div><span>Group ${h.hint_group}</span><select data-stat-target="${i}">${opts([[1, "Speed"], [2, "Stamina"], [3, "Power"], [4, "Guts"], [5, "Wisdom"], [30, "Skill points"]], h.hint_value_1)}</select><input type="number" min="0" max="1000" data-stat-value="${i}" value="${h.hint_value_2}" aria-label="Stat hint amount"><button class="btn sm" data-remove-stat="${i}" aria-label="Remove stat hint">${icon("close")}</button></div>` : "").join("") || '<p class="faint">No stat-hint groups.</p>'}</div>`;
      }
      if (tab === "events") return `<div class="csc-section-head"><h2>Events</h2><p>Reuse a compatible card's existing events, choices, and rewards. This card keeps its own bond and chain progress.</p></div><label class="csc-field">Event donor${selectCards(draft.event_donor, 'data-field="event_donor"', (c) => c.chara_id === draft.chara_id && !["friend", "group"].includes(c.type))}</label>
          <div class="csc-donor-preview">${gameIcon("supportart", draft.event_donor, "csc-donor-art")}<div><span class="csc-eyebrow">EXISTING EVENT ASSETS</span><h3>${esc(label(draft.event_donor))}</h3><p>Original story IDs and character models. Separate custom-card ownership.</p></div></div><div class="csc-note">${icon("flag")}<div><b>Same character, independent card</b><p>Donors are limited to this character to keep dialogue, bond rewards, and story playback consistent. Friend and group outing chains are outside this release.</p></div></div>`;
      if (tab === "art") return `<div class="csc-section-head"><h2>Artwork</h2><p>Borrow existing artwork or upload your own PNG. Your original file is preserved.</p></div>
        <label class="csc-upload"><input type="file" accept="image/png" data-art-file>${icon("upload")}<b>Choose custom artwork</b><span>PNG · up to 8 MB · 32–4096 pixels per side</span></label>
        ${focusEditor("card_focus")}${focusEditor("icon_focus")}
        <div class="csc-art-previews"><div>${art(draft, "csc-art-large")}<span>Full card</span></div><div>${art(draft, "csc-art-small")}<span>Collection icon</span></div><div>${art(draft, "csc-art-thumb")}<span>Training thumbnail</span></div></div>
        ${draft.art ? '<button class="btn sm" data-action="clear-art">Use borrowed art instead</button>' : `<label class="csc-field">Artwork donor${selectCards(draft.art_donor, 'data-field="art_donor"')}</label>`}
        <div class="csc-note">${icon("cards")}<div><b>Best results: portrait artwork</b><p>A 3:4 image suits the full card. Small views crop the image to fit. The plugin supplies local textures without changing the donor card's artwork.</p></div></div>`;
      const changes = original ? ["name", "title", "type", "rarity", "description", "effects", "hints", "event_donor", "art_donor", "art", "icon_focus", "card_focus", "unique"].filter((k) => JSON.stringify(draft[k]) !== JSON.stringify(original[k])) : [];
      return `<div class="csc-section-head"><h2>Review</h2><p>Save your draft, validate its references, then publish a matching client and server pack.</p></div><div class="csc-review-grid"><div><span>Card identity</span><b>${draft.id || "Allocated on save"}</b><small>${rarity(draft.rarity)} · ${esc(draft.type)}</small></div><div><span>Training bonuses</span><b>${draft.effects.length}</b><small>${draft.unique ? "With inherited unique effect" : "No unique effect"}</small></div><div><span>Hint skills</span><b>${draft.hints.filter((h) => !h.hint_gain_type).length}</b><small>Existing regional skills</small></div></div>
        <div class="csc-review-change"><h3>${changes.length ? "Unsaved changes" : "Draft status"}</h3>${changes.length ? `<div class="csc-hint-chips">${changes.map((k) => `<span>${esc(k.replaceAll("_", " "))}</span>`).join("")}</div>` : '<p class="faint">No changes since the last saved version.</p>'}</div><div class="csc-note">${icon("lock")}<div><b>Plugin readiness required</b><p>Publishing includes all non-retired saved drafts. The feature remains off until you enable it. Every client must load the exact pack before receiving custom cards.</p></div></div><button class="btn" data-action="validate">${icon("check")} Validate this draft</button>
        <div class="csc-publish-panel"><div><h3>Publish the saved collection</h3><p>${data.cards.filter((d) => !d.retired).length} saved cards · ${store.region === "jp" ? "JP" : "Global"} pack</p></div><button class="btn primary" data-action="publish" ${dirty ? "disabled" : ""}>${icon("upload")} Publish pack</button></div>`;
    }
    function curve(e) {
      const max = Math.max(1, ...e.values), points = Array.from({ length: 51 }, (_, lv) => `${20 + lv * 6.7},${120 - levelValue(e.values, lv) / max * 90}`).join(" ");
      return `<svg class="csc-curve" viewBox="0 0 380 150" role="img" aria-label="Bonus across card levels"><path d="M20 30H355M20 75H355M20 120H355" class="csc-curve-grid"/><polygon points="20,120 ${points} 355,120"/><polyline points="${points}"/><text x="20" y="142">Lv 0</text><text x="326" y="142">Lv 50</text><text x="20" y="18">${max}</text></svg>`;
    }
    function skillResults(results, count) {
      return results.map((s) => `<button data-add-hint="${s.id}" ${count >= 20 ? "disabled" : ""}><span><b>${esc(s.name)}</b><small>${s.id} · ${s.rarity > 1 ? "Rare skill" : "Skill"}</small></span>${icon("plus")}</button>`).join("") || '<p class="faint">No matching skills.</p>';
    }
    function render() {
      if (!data || !cat) return;
      const list = data.cards.filter((c) => `${c.name} ${c.title} ${c.id}`.toLowerCase().includes(search.toLowerCase()));
      body.innerHTML = `<div class="csc-topbar"><div><h1>Custom Support Cards</h1></div><button class="csc-switch ${data.enabled ? "on" : ""}" data-action="toggle" aria-pressed="${data.enabled}"><span></span>${data.enabled ? "Feature enabled" : "Feature off"}</button></div>
        <div class="csc-layout"><aside class="csc-library"><div class="csc-library-top"><div><h3>Library</h3><span>${data.cards.length} custom cards</span></div><button class="btn sm primary" data-action="new" aria-label="Create custom card">${icon("plus")}</button></div><label class="csc-library-search">${icon("search")}<input data-search value="${esc(search)}" placeholder="Find a card…" aria-label="Search custom cards"></label>
        <div class="csc-library-list">${list.map((c) => `<button class="csc-library-card ${draft?.id === c.id ? "on" : ""}" data-select="${c.id}">${c.art ? `<img src="data:image/png;base64,${esc(c.art.replace(/^data:image\/png;base64,/, ""))}" alt="">` : gameIcon("support", c.art_donor)}<span><b>${esc(c.name)}</b><small>${esc(c.title)}</small><em>${c.id} · ${c.retired ? "Retired" : published(c.id) ? "Published · draft v" + c.version : "Draft v" + c.version}</em></span></button>`).join("") || '<div class="csc-library-empty">Your custom cards<br>will appear here.</div>'}</div><div class="csc-library-foot"><span class="csc-eyebrow">CLIENT PACK</span><b>${data.digest ? data.digest.slice(0, 12) : "Not published"}</b><span>${data.sessions.length} ready plugin session${data.sessions.length === 1 ? "" : "s"}</span>${data.revisions?.length > 1 ? `<label class="csc-revision-label">Published revision<select data-revision>${opts(data.revisions.map((r) => [r.digest, `${r.digest.slice(0, 8)} · ${r.cards} cards`]), data.digest)}</select></label><button class="linkish" data-action="activate">Use selected revision</button>` : ""}<button class="linkish" data-action="export">${icon("download")} Export definitions</button><button class="linkish" data-action="reload">${icon("refresh")} Refresh status</button></div></aside>
        <main class="csc-main">${draft ? `<div class="csc-editor-heading"><div><span class="csc-badge">${draft.id ? '#' + draft.id : "NEW CARD"}</span><span data-dirty>${dirty ? "Unsaved draft" : `Saved draft · v${draft.version || 0}`}</span></div><div><button class="btn sm" data-action="duplicate" title="Create a separate copy">${icon("copy")}</button>${draft.id ? `<button class="btn sm" data-action="retire" title="Retire this ID">${icon("trash")}</button>` : ""}<button class="btn primary sm" data-action="save">${icon("save")} Save draft</button></div></div><nav class="csc-tabs" aria-label="Card editor sections">${TABS.map(([k, name, glyph]) => `<button data-tab="${k}" class="${tab === k ? "on" : ""}" aria-current="${tab === k ? "page" : "false"}">${icon(glyph)}${name}</button>`).join("")}</nav>` : ""}
        ${message ? `<div class="csc-message" role="status">${icon("alert")}${esc(message)}</div>` : ""}<div class="csc-workspace"><div class="csc-content">${content()}</div>${draft ? `<aside class="csc-preview" data-preview>${preview()}</aside>` : ""}</div>
        ${draft ? `<footer class="csc-footer"><span>${icon("lock")} Drafts never reach the game until published.</span><button class="btn sm" data-action="validate">${icon("check")} Validate</button></footer>` : ""}</main></div>`;
    }
    body.addEventListener("input", (e) => {
      const el = e.target;
      if (el.matches("[data-search]")) { search = el.value; const p = el.selectionStart; render(); const n = body.querySelector("[data-search]"); n.focus(); n.setSelectionRange(p, p); return; }
      if (el.matches("[data-skill-search]")) { skillQuery = el.value; const ids = new Set(draft.hints.filter((h) => !h.hint_gain_type).map((h) => h.hint_value_1)); body.querySelector("[data-skill-results]").innerHTML = skillResults(skills.filter((s) => !ids.has(s.id) && `${s.name} ${s.id}`.toLowerCase().includes(skillQuery.toLowerCase())).slice(0, 40), ids.size); return; }
      if (!draft) return;
      if (el.matches("[data-focus-zoom]") && artDims) {
        const key = el.dataset.focusZoom, f = focusOf(key), { w, h } = artDims, size = 1100 - +el.value;
        const [cw, ch] = cropSize(key, f), [nw, nh] = cropSize(key, { size });
        setFocus(key, { x: f.x + (cw - nw) / w * 500, y: f.y + (ch - nh) / h * 500, size }); return;
      }
      if (el.dataset.field) { const key = el.dataset.field; draft[key] = ["rarity", "event_donor", "art_donor"].includes(key) ? +el.value : el.value; dirtyMark(); }
      if (el.dataset.breakpoint !== undefined) { draft.effects[selectedEffect].values[+el.dataset.breakpoint] = el.value === "" ? -1 : +el.value; dirtyMark(); body.querySelector("[data-curve]").innerHTML = curve(draft.effects[selectedEffect]); }
      if (el.dataset.statTarget !== undefined) { draft.hints[+el.dataset.statTarget].hint_value_1 = +el.value; dirtyMark(); }
      if (el.dataset.statValue !== undefined) { draft.hints[+el.dataset.statValue].hint_value_2 = +el.value; dirtyMark(); }
    });
    body.addEventListener("change", (e) => {
      if (e.target.matches("[data-add-effect]") && e.target.value) { draft.effects.push({ type: +e.target.value, values: [0, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0] }); selectedEffect = draft.effects.length - 1; dirtyMark(); render(); }
      if (e.target.matches("[data-field=event_donor],[data-field=art_donor],[data-field=rarity],[data-field=type]")) render();
      if (e.target.matches("[data-art-file]")) run(async (operationTarget) => {
        const f = e.target.files[0]; if (!f) return;
        if (f.type !== "image/png" || f.size > 8 * 1024 * 1024) throw new Error("Choose a PNG smaller than 8 MB.");
        const file = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(new Error("Could not read the image.")); r.readAsDataURL(f); });
        const candidate = { ...draft, art: file.split(",")[1] };
        await call("custom_support.validate", { definition: candidate }, operationTarget); if (operationTarget.region !== store.region) return; draft = candidate; dirtyMark(); render();
      });
    });
    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button"); if (!b || b.disabled || busy) return;
      if (b.dataset.tab) { tab = b.dataset.tab; render(); return; }
      if (b.dataset.select) { if (await leave()) choose(data.cards.find((c) => c.id === +b.dataset.select)); return; }
      if (b.dataset.effect !== undefined) { selectedEffect = +b.dataset.effect; render(); return; }
      if (b.dataset.addHint) { if (draft.hints.filter((h) => !h.hint_gain_type).length >= 20) return; draft.hints.push({ hint_group: Math.max(0, ...draft.hints.map((h) => h.hint_group)) + 1, hint_gain_type: 0, hint_value_1: +b.dataset.addHint, hint_value_2: 0 }); dirtyMark(); render(); return; }
      if (b.dataset.removeHint) { draft.hints = draft.hints.filter((h) => h.hint_gain_type || h.hint_value_1 !== +b.dataset.removeHint); dirtyMark(); render(); return; }
      if (b.dataset.removeStat !== undefined) { draft.hints.splice(+b.dataset.removeStat, 1); dirtyMark(); render(); return; }
      const action = b.dataset.action;
      if (action === "new") {
        if (!await leave()) return;
        const donor = cat.supports.cards.find((c) => !c.custom && c.rarity === 3 && c.type === "speed") || cat.supports.cards.find((c) => !c.custom && TYPES.some(([t]) => c.type === t));
        if (!donor) { message = "No normal support cards are available in this region."; render(); return; }
        body.querySelector(".csc-content").innerHTML = `<div class="csc-section-head"><h2>New card</h2><p>Copy a normal support card's effects, hints, character and events. It will receive an entirely new ID.</p></div><label class="csc-field">Template card${selectCards(donor.id, 'data-template-picker', (c) => TYPES.some(([t]) => c.type === t))}</label><div class="csc-start-preview" data-template-preview>${gameIcon("supportart", donor.id, "csc-donor-art")}<p>${esc(label(donor.id))}</p></div><button class="btn primary" data-action="start-template">${icon("plus")} Create draft</button>`;
        const picker = body.querySelector("[data-template-picker]"); picker.onchange = () => { body.querySelector("[data-template-preview]").innerHTML = `${gameIcon("supportart", +picker.value, "csc-donor-art")}<p>${esc(label(picker.value))}</p>`; };
        return;
      }
      if (action === "duplicate") { const d = clone(draft); d.id = 0; d.version = 0; d.retired = false; d.title += " (Copy)"; choose(d); dirtyMark(); return; }
      if (action === "remove-effect") { draft.effects.splice(selectedEffect, 1); selectedEffect = Math.max(0, selectedEffect - 1); dirtyMark(); render(); return; }
      if (action === "remove-unique") { draft.unique = null; dirtyMark(); render(); return; }
      if (action === "add-stat") { draft.hints.push({ hint_group: Math.max(0, ...draft.hints.map((h) => h.hint_group)) + 1, hint_gain_type: 1, hint_value_1: 1, hint_value_2: 5 }); dirtyMark(); render(); return; }
      if (action === "reset-focus") { delete draft[b.dataset.crop]; dirtyMark(); render(); return; }
      if (action === "clear-art") { delete draft.icon_focus; delete draft.card_focus; draft.art = ""; dirtyMark(); render(); return; }
      await run(async (operationTarget) => {
        const scopedCall = (op, args = {}) => call(op, args, operationTarget);
        if (action === "start-template") { const d = await scopedCall("custom_support.template", { id: +body.querySelector("[data-template-picker]").value }); if (operationTarget.region !== store.region) return; choose(d); dirtyMark(); }
        if (action === "save") { const saved = await scopedCall("custom_support.save", { definition: draft, version: draft.version || 0 }); if (operationTarget.region !== store.region) return; const currentTab = tab; choose(saved); tab = currentTab; await load(true); toast("Draft saved. Game data is unchanged."); }
        if (action === "validate") { const v = await scopedCall("custom_support.validate", { definition: draft }); message = `Ready to save: ${v.effects} bonuses and ${v.skills} hint skills validated against this region's master.`; render(); }
        if (action === "publish") {
          if (!await ask(win, { title: "Publish the saved collection?", text: "Creates an immutable pack from every non-retired saved draft. Existing custom careers keep their current revision. Clients load the new pack after restarting.", ok: "Publish pack" })) return;
          const m = await scopedCall("custom_support.publish"); await refreshCatalog(); await load(true); message = `Pack ${m.digest.slice(0, 12)} published. ${data.enabled ? "Restart the client to load it." : "Enable the feature when you are ready."}`; render();
        }
        if (action === "activate") {
          const digest = body.querySelector("[data-revision]").value;
          if (digest === data.digest || !await ask(win, { title: "Switch the published revision?", text: "New plugin sessions will load this pack after restarting. Saved drafts and clients already playing keep their current versions.", ok: "Use revision" })) return;
          await scopedCall("custom_support.activate", { digest }); await refreshCatalog(); await load(true);
        }
        if (action === "toggle") { await scopedCall("custom_support.enabled", { enabled: !data.enabled }); await load(true); }
        if (action === "retire") { if (!await ask(win, { title: "Retire this card ID?", text: "Saved ownership and published packs remain available. This ID cannot be reused. Publish again to exclude it from new packs.", ok: "Retire card", danger: true })) return; await scopedCall("custom_support.retire", { id: draft.id }); await load(); }
        if (action === "reload") { if (await leave()) await load(); }
        if (action === "export") { const d = await scopedCall("custom_support.export"); const url = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: "application/json" })); const a = document.createElement("a"); a.href = url; a.download = `custom-support-${store.region}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
      });
    });
    let editorRegion = store.region;
    const unsaved = new Map();
    const off = store.on((kind) => {
      if (kind !== "target" || editorRegion === store.region) return;
      if (draft) unsaved.set(editorRegion, { draft, original, dirty, tab });
      editorRegion = store.region;
      ++generation;
      const restore = unsaved.get(editorRegion);
      const loadingRegion = editorRegion;
      load().then(() => {
        if (editorRegion !== loadingRegion) return;
        if (restore) { ({ draft, original, dirty, tab } = restore); render(); }
      });
    });
    const unload = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", unload);
    load();
    return () => { ++generation; off(); window.removeEventListener("beforeunload", unload); };
  },
};
