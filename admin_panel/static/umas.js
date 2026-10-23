
import { act, call, esc, gameIcon, icon, keepScroll, loadCatalog, store } from "./core.js";
import { APT_LABELS, STATS, STYLES, aptRow, ask, empty, gradeOptions, modal, opts, selection, selectionBar, stars, targetPicker }
  from "./widgets.js";

const SORTS = [["new", "Newest"], ["old", "Oldest"], ["name", "Name"], ["stars", "Stars"], ["pot", "Potential"], ["apt", "Aptitude total"]];
const blank = () => ({ q: "", own: "all", base: [], stars: "", pot: "", hints: "", styles: [], apt: Array(10).fill(""), bonus: [], sort: "new" });

export const umasWin = {
  title: "Umas", icon: "uma", size: [720, 520], place: [0.1, 0.03, 0.88, 0.94], key: "4",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    win.state.f = { ...blank(), ...(win.state.f || {}) };
    win.state.bulk ??= { stars: "max", pot: "max", hints: "max", all: false, give: true };
    const f = win.state.f, bulk = win.state.bulk;
    if (!Array.isArray(f.apt) || f.apt.length !== 10) f.apt = Array(10).fill("");
    const sel = selection();
    let cat = null, owned = new Map(), charas = {}, pieces = {}, order = [];
    body.classList.add("flush");
    body.innerHTML = `<div class="pane-top" data-filters></div><div class="pane-scroll"><div class="cgrid"></div></div><div class="pane-foot" data-foot></div>`;
    const grid = body.querySelector(".cgrid"), top = body.querySelector("[data-filters]"), foot = body.querySelector("[data-foot]");
    const maxT = () => cat.umas.max_talent;

    async function load() {
      try {
        cat = await loadCatalog();
        const r = await call("umas.owned");
        owned = new Map(r.cards.map((x) => [x.id, x]));
        charas = r.charas; pieces = r.pieces;
      } catch (ex) { grid.innerHTML = empty(esc(ex.message)); return; }
      renderFilters();
      render();
    }

    function renderFilters() {
      const chip = (group, v, label, on) => `<button class="chipbtn sm ${on ? "on" : ""}" data-chip="${group}" data-v="${v}">${label}</button>`;
      top.innerHTML = `<div class="filters">
          <label class="search"><span>${icon("search")}</span><input type="text" data-f="q" placeholder="Name, outfit or id" value="${esc(f.q)}" spellcheck="false"></label>
          <label>Sort <select data-f="sort">${opts(SORTS, f.sort)}</select></label>
        </div>
        <div class="filters">
          <span class="chips">${[["all", "All"], ["owned", "Owned"], ["missing", "Missing"]].map(([v, t]) => chip("own", v, t, f.own === v)).join("")}</span>
          <span class="chips" title="Base rarity">${[1, 2, 3].map((r) => chip("base", r, `${r}★ base`, f.base.includes(r))).join("")}</span>
          <span class="chips" title="Preferred running style">${Object.entries(STYLES).map(([k, t]) => chip("styles", +k, t, f.styles.includes(+k))).join("")}</span>
          <span class="chips" title="Stat growth bonus">${STATS.map(([k, t], i) => chip("bonus", i, `${t}+`, f.bonus.includes(i))).join("")}</span>
        </div>
        <div class="filters">
          <label>Stars <select data-f="stars">${opts([["", "Any"], ...[1, 2, 3, 4, 5].map((n) => [n, `${n}★`]), ["notmax", "Not max"]], f.stars)}</select></label>
          <label>Potential <select data-f="pot">${opts([["", "Any"], ...Array.from({ length: maxT() }, (_, i) => [i + 1, `Lv${i + 1}`]), ["notmax", "Not max"]], f.pot)}</select></label>
          <label>Hints <select data-f="hints">${opts([["", "Any"], ["none", "None"], ["some", "Some"], ["notmax", "Below cap"], ["max", "At cap"]], f.hints)}</select></label>
          <button class="linkish" data-reset>Reset filters</button>
        </div>
        <div class="apt-filter" title="Minimum aptitude">${APT_LABELS.map((l, i) =>
          `<label>${l}<select data-apt="${i}" class="${f.apt[i] ? "set" : ""}">${gradeOptions(f.apt[i], true)}</select></label>`).join("")}</div>
        <div class="count-line" data-count></div>`;
    }

    const hintCap = (talent) => cat.umas.hint_caps[talent] ?? 3;
    function shown() {
      const q = f.q.trim().toLowerCase();
      const list = cat.umas.cards.filter((c) => {
        const o = owned.get(c.id);
        if (q && !(`${c.name} ${c.title} ${c.id}`.toLowerCase().includes(q))) return false;
        if (f.own === "owned" && !o) return false;
        if (f.own === "missing" && o) return false;
        if (f.base.length && !f.base.includes(c.default_rarity)) return false;
        if (f.styles.length && !f.styles.includes(c.style)) return false;
        if (f.bonus.length && !f.bonus.every((i) => c.talent[i] > 0)) return false;
        for (let i = 0; i < 10; i++) if (f.apt[i] && c.apt[i] < +f.apt[i]) return false;
        if (f.stars && (!o || (f.stars === "notmax" ? o.rarity >= c.max_rarity : o.rarity !== +f.stars))) return false;
        if (f.pot && (!o || (f.pot === "notmax" ? o.talent >= (c.max_talent || maxT()) : o.talent !== +f.pot))) return false;
        if (f.hints) {
          if (!o) return false;
          const cap = hintCap(o.talent);
          if (f.hints === "none" && o.hints) return false;
          if (f.hints === "some" && !o.hints) return false;
          if (f.hints === "max" && o.hint_max < cap) return false;
          if (f.hints === "notmax" && o.hints && o.hint_max >= cap) return false;
        }
        return true;
      });
      const aptSum = (c) => c.apt.reduce((a, b) => a + b, 0);
      const by = {
        new: (a, b) => b.id - a.id, old: (a, b) => a.id - b.id, name: (a, b) => a.name.localeCompare(b.name) || a.id - b.id,
        stars: (a, b) => (owned.get(b.id)?.rarity ?? 0) - (owned.get(a.id)?.rarity ?? 0) || b.default_rarity - a.default_rarity,
        pot: (a, b) => (owned.get(b.id)?.talent ?? 0) - (owned.get(a.id)?.talent ?? 0), apt: (a, b) => aptSum(b) - aptSum(a),
      }[f.sort];
      return list.sort(by);
    }

    function render() {
      if (!cat) return;
      const list = shown();
      order = list.map((c) => c.id);
      keepScroll(grid, () => {
        grid.innerHTML = list.map((c) => {
          const o = owned.get(c.id);
          return `<div class="ctile ${o ? "" : "missing"} ${sel.set.has(c.id) ? "on" : ""}" data-id="${c.id}"
              title="${esc(c.name)} [${esc(c.title)}] · ${c.id}&#10;${APT_LABELS.map((l, i) => `${l} ${"_GFEDCBAS"[c.apt[i]]}`).join(" · ")}">
            <span class="ct-badge">${c.default_rarity}★</span>${o ? `<span class="ct-own" title="Potential">Lv${o.talent}</span>` : ""}
            <span class="ct-check"></span>
            ${gameIcon("card", c.id)}
            <span class="ct-name">${esc(c.name)}</span>
            <span class="ct-sub">${esc(c.title)}</span>
            ${o ? `${stars(o.rarity, c.max_rarity)}${o.hints ? `<span class="faint">${o.hints} hint${o.hints > 1 ? "s" : ""} · max ${o.hint_max}</span>` : ""}` : `<span class="faint">not owned</span>`}
          </div>`;
        }).join("") || empty("No umas match.");
      });
      top.querySelector("[data-count]").innerHTML = selectionBar(sel.set.size, list.length, cat.umas.cards.length) + `<span class="faint">· ${owned.size} owned</span>`;
      renderFoot();
    }

    function syncSel() {
      for (const t of grid.querySelectorAll(".ctile")) t.classList.toggle("on", sel.set.has(+t.dataset.id));
      top.querySelector("[data-count]").innerHTML = selectionBar(sel.set.size, order.length, cat.umas.cards.length) + `<span class="faint">· ${owned.size} owned</span>`;
      renderFoot();
    }

    function renderFoot() {
      const n = sel.set.size;
      const t = maxT();
      foot.innerHTML = `<div class="act">
        <button class="btn primary sm" data-do="give" ${n ? "" : "disabled"} title="Add the selected umas you don't own, with the settings on the right">${icon("plus")} Give</button>
        <span class="grp">Stars <select data-b="stars">${opts([["", "keep"], ["max", "max"], ...[1, 2, 3, 4, 5].map((x) => [x, `${x}★`])], bulk.stars)}</select></span>
        <span class="grp">Potential <select data-b="pot">${opts([["", "keep"], ["max", "max"], ...Array.from({ length: t }, (_, i) => [i + 1, `Lv${i + 1}`])], bulk.pot)}</select></span>
        <span class="grp">Hints <select data-b="hints">${opts([["", "keep"], ["max", "max"], ...[0, 1, 2, 3, 4, 5].map((x) => [x, `Lv${x}`])], bulk.hints)}</select>
          <label class="check" title="Also skills not unlocked at the card's potential yet"><input type="checkbox" data-b="all" ${bulk.all ? "checked" : ""}> locked too</label></span>
        <label class="check"><input type="checkbox" data-b="give" ${bulk.give ? "checked" : ""}> also give missing</label>
        <button class="btn sm" data-do="set" ${n ? "" : "disabled"}>${icon("check")} Apply to ${n}</button>
        <span class="grow"></span>
        <button class="btn sm" data-do="edit" ${n === 1 ? "" : "disabled"}>${icon("edit")} Edit</button>
        <button class="btn danger sm" data-do="remove" ${n ? "" : "disabled"}>${icon("trash")} Remove</button></div>`;
    }

    async function run(action, btn) {
      const ids = [...sel.set];
      if (action === "edit") return editOne(ids[0]);
      if (action === "remove" && !(await ask(win, { title: `Remove ${ids.length} uma(s)?`, danger: true, ok: "Remove", text: "Undo is in History." }))) return;
      const r = await act(btn, "Umas", () => call("umas.apply", { ids, action, rarity: bulk.stars, talent: bulk.pot, hints: bulk.hints,
        all_skills: bulk.all, give_missing: bulk.give }),
        (r) => action === "remove" ? `Removed ${r.removed}` : `${r.added} added · ${r.changed} changed`);
      if (r) load();
    }

    async function editOne(id) {
      const m = modal(win, empty("Loading…"), { wide: true });
      let d;
      try { d = await call("umas.detail", { card_id: id }); } catch (ex) { m.box.innerHTML = empty(esc(ex.message)); return; }
      const c = d.card, e = d.entry || { rarity: c.default_rarity, talent_level: 1 };
      const st = { rarity: e.rarity, talent: e.talent_level, levels: Object.fromEntries(d.skills.map((s) => [s.skill_id, s.level])),
                   dropExtra: false };
      const ch = d.chara || {};
      const draw = () => {
        const cap = hintCap(st.talent);
        m.box.innerHTML = `<div class="editor">
          <div class="ed-head">${gameIcon("card", c.id)}<div class="grow"><h3>${esc(c.name)}</h3>
            <div class="faint">${esc(c.title)} · ${c.id} · ${d.owned ? "owned" : "not owned yet (saving gives it)"}</div>
            <div class="row wrap" style="margin-top:6px">${aptRow(c.apt)}<span class="pill">Prefers ${STYLES[c.style] || "?"}</span>
              <span class="faint">${STATS.map(([, t], i) => c.talent[i] ? `${t} +${c.talent[i]}%` : "").filter(Boolean).join(" · ")}</span></div></div></div>
          <div class="ed-sec"><div class="section">Card</div><div class="ed-row">
            <label>Stars<span class="starpick">${[1, 2, 3, 4, 5].map((n) => `<button data-star="${n}" class="${n <= st.rarity ? "on" : ""}"
              ${n < c.default_rarity || n > c.max_rarity ? "disabled" : ""}>★</button>`).join("")}</span></label>
            <label>Potential (awakening)<span class="seg">${Array.from({ length: c.max_talent || maxT() }, (_, i) => i + 1).map((n) =>
              `<button data-pot="${n}" class="${n === st.talent ? "on" : ""}">${n}</button>`).join("")}</span></label>
            <label>Created<input type="text" class="sm" data-ct value="${esc(e.create_time || "")}" style="width:170px"></label>
          </div></div>
          <div class="ed-sec"><div class="row section-row"><div class="section">Hint levels · cap ${cap} at potential ${st.talent}</div>
            <div class="row"><button class="btn sm" data-hint-all="max">All max</button><button class="btn sm" data-hint-all="0">All 0</button></div></div>
            <div class="hint-table">${d.skills.map((s) => {
              const locked = s.need_rank > st.talent;
              const lv = Math.min(st.levels[s.skill_id] || 0, cap);
              return `<div class="hint-row ${locked ? "locked" : ""}" title="${locked ? `Unlocks at potential ${s.need_rank}` : ""}">
                <span class="faint">${s.need_rank ? `L${s.need_rank}` : "–"}</span><span class="${s.rarity >= 2 ? "r2" : ""} ellipsis">${esc(s.name)}</span>
                <span class="faint">${s.rarity >= 2 ? "gold" : "white"}</span>
                <span class="seg">${Array.from({ length: cap + 1 }, (_, i) => i).map((n) => `<button data-skill="${s.skill_id}" data-lv="${n}" class="${n === lv ? "on" : ""}">${n}</button>`).join("")}</span></div>`;
            }).join("") || `<div class="faint">This card has no awakening skills in master.</div>`}</div>
            ${d.extra_hints.length ? `<label class="check"><input type="checkbox" data-drop ${st.dropExtra ? "checked" : ""}>
              Drop ${d.extra_hints.length} hint entr${d.extra_hints.length > 1 ? "ies" : "y"} for skills that aren't this card's awakening skills (left by older cheats)</label>` : ""}
          </div>
          <div class="ed-sec"><div class="section">Character · ${esc(c.name)}</div><div class="ed-row">
            <label>Bond (love point)<input type="number" class="num wide" data-ch="love_point" value="${ch.love_point ?? 0}"></label>
            <label>Fans<input type="number" class="num wide" data-ch="fan" value="${ch.fan ?? 1}"></label>
            <label>Careers run<input type="number" class="num" data-ch="training_num" value="${ch.training_num ?? 0}"></label>
            <label>Best class<input type="number" class="num" data-ch="max_grade" value="${ch.max_grade ?? 0}"></label>
            <label>Star pieces<input type="number" class="num wide" data-pc value="${d.pieces}"></label>
          </div></div>
          <div class="modal-foot">${d.owned ? `<button class="btn danger sm" data-x="rm">${icon("trash")} Remove card</button><span class="grow"></span>` : ""}
            <button class="btn sm" data-x="close">Cancel</button><button class="btn primary sm" data-x="save">${icon("save")} Save</button></div></div>`;
      };
      m.box.addEventListener("input", (ev) => {
        const t = ev.target;
        if (t.dataset.ch) ch[t.dataset.ch] = t.value;
        else if (t.matches("[data-pc]")) d.pieces = t.value;
        else if (t.matches("[data-ct]")) e.create_time = t.value;
      });
      m.box.addEventListener("click", async (ev) => {
        const t = ev.target.closest("button, input");
        if (!t) return;
        if (t.dataset.star) { st.rarity = +t.dataset.star; draw(); }
        else if (t.dataset.pot) { st.talent = +t.dataset.pot; draw(); }
        else if (t.dataset.skill) { st.levels[t.dataset.skill] = +t.dataset.lv; draw(); }
        else if (t.dataset.hintAll) {
          const cap = hintCap(st.talent);
          for (const s of d.skills) if (s.need_rank <= st.talent) st.levels[s.skill_id] = t.dataset.hintAll === "max" ? cap : 0;
          draw();
        } else if (t.matches("[data-drop]")) { st.dropExtra = t.checked; }
        else if (t.dataset.x === "close") m.close();
        else if (t.dataset.x === "rm") {
          if (await act(t, "Remove uma", () => call("umas.apply", { ids: [id], action: "remove" }), "Removed")) { m.close(); load(); }
        } else if (t.dataset.x === "save") {
          const cap = hintCap(st.talent);
          const skills = d.skills.map((s) => ({ skill_id: s.skill_id, level: Math.min(st.levels[s.skill_id] || 0, cap) })).filter((s) => s.level > 0);
          if (!st.dropExtra) skills.push(...d.extra_hints.map((s) => ({ skill_id: s.skill_id, level: Math.min(s.level, cap) })));
          const chara = {};
          for (const el of m.box.querySelectorAll("[data-ch]")) chara[el.dataset.ch] = el.value;
          const r = await act(t, "Save uma", () => call("umas.save", { card_id: id, rarity: st.rarity, talent_level: st.talent, skills, chara,
            pieces: m.box.querySelector("[data-pc]").value, create_time: m.box.querySelector("[data-ct]").value }), "Saved");
          if (r) { m.close(); load(); }
        }
      });
      draw();
    }

    body.addEventListener("click", (e) => {
      if (e.target.closest(".modal")) return;
      const tile = e.target.closest(".ctile");
      if (tile) { sel.click(+tile.dataset.id, order, e); syncSel(); return; }
      const ch = e.target.closest("[data-chip]");
      if (ch) {
        const g = ch.dataset.chip, v = ["base", "styles", "bonus"].includes(g) ? +ch.dataset.v : ch.dataset.v;
        if (g === "own") f.own = v;
        else f[g] = f[g].includes(v) ? f[g].filter((x) => x !== v) : [...f[g], v];
        win.save(); renderFilters(); render(); return;
      }
      const s = e.target.closest("[data-sel]");
      if (s) { sel[s.dataset.sel](order); syncSel(); return; }
      if (e.target.closest("[data-reset]")) { Object.assign(f, blank()); win.save(); renderFilters(); render(); return; }
      const d = e.target.closest("[data-do]");
      if (d) run(d.dataset.do, d);
    });
    body.addEventListener("dblclick", (e) => {
      if (e.target.closest(".modal")) return;
      const tile = e.target.closest(".ctile");
      if (tile) { sel.only(+tile.dataset.id); syncSel(); editOne(+tile.dataset.id); }
    });
    top.addEventListener("input", (e) => {
      if (e.target.dataset.apt !== undefined) {
        f.apt[+e.target.dataset.apt] = e.target.value;
        e.target.classList.toggle("set", !!e.target.value);
        win.save(); render(); return;
      }
      const k = e.target.dataset.f;
      if (!k) return;
      f[k] = e.target.value;
      win.save(); render();
    });
    foot.addEventListener("change", (e) => {
      const k = e.target.dataset.b;
      if (!k) return;
      bulk[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      win.save(); renderFoot();
    });

    win.reveal = (o) => { if (o.card_id) editOne(o.card_id); };
    const off = store.on((kind) => {
      if (kind === "target") { cat = null; sel.none(); load(); }
      if (kind === "history") load();
    });
    load();
    return off;
  },
};
