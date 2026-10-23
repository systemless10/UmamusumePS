
import { act, call, esc, gameIcon, icon, keepScroll, loadCatalog, store } from "./core.js";
import { RARITY, SUPPORT_TYPES, empty, lbDiamonds, modal, ask, opts, selection, selectionBar, targetPicker } from "./widgets.js";

const SORTS = [["new", "Newest"], ["old", "Oldest"], ["name", "Name"], ["rarity", "Rarity"], ["type", "Type"], ["lb", "Uncap"], ["level", "Level"]];
const blank = () => ({ q: "", types: [], rar: [], own: "all", lb: "", lvl: "", fav: false, sort: "new" });
const TYPE_ORDER = Object.fromEntries(SUPPORT_TYPES.map(([k], i) => [k, i]));

export const supportsWin = {
  title: "Support cards", icon: "cards", size: [720, 520], place: [0.12, 0.03, 0.86, 0.94], key: "6",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    win.state.f = { ...blank(), ...(win.state.f || {}) };
    win.state.bulk ??= { lb: "4", level: "max", stock: "", fav: "", give: true };
    const f = win.state.f, bulk = win.state.bulk;
    const sel = selection();
    let cat = null, owned = new Map(), order = [];
    body.classList.add("flush");
    body.innerHTML = `<div class="pane-top" data-filters></div><div class="pane-scroll"><div class="cgrid"></div></div><div class="pane-foot" data-foot></div>`;
    const grid = body.querySelector(".cgrid"), top = body.querySelector("[data-filters]"), foot = body.querySelector("[data-foot]");

    const capOf = (c, lb) => cat.supports.limits[c.rarity]?.[lb] ?? 50;
    async function load() {
      try {
        cat = await loadCatalog();
        const r = await call("supports.owned");
        owned = new Map(r.cards.map((x) => [x.id, x]));
      } catch (ex) { grid.innerHTML = empty(esc(ex.message)); return; }
      renderFilters();
      render();
    }

    function renderFilters() {
      const chip = (group, v, label, on) => `<button class="chipbtn sm ${on ? "on" : ""}" data-chip="${group}" data-v="${v}">${label}</button>`;
      top.innerHTML = `<div class="filters">
          <label class="search"><span>${icon("search")}</span><input type="text" data-f="q" placeholder="Name, title or id" value="${esc(f.q)}" spellcheck="false"></label>
          <label>Sort <select data-f="sort">${opts(SORTS, f.sort)}</select></label>
        </div>
        <div class="filters">
          <button class="btn sm" data-open="more" data-open-arg='{"tab":"custom"}'>${icon("sparkle")} Custom studio</button>
          <span class="chips">${SUPPORT_TYPES.map(([k, t]) => chip("types", k, `<span class="type-dot t-${k}"></span>${t}`, f.types.includes(k))).join("")}</span>
          <span class="chips">${[1, 2, 3].map((r) => chip("rar", r, RARITY[r], f.rar.includes(r))).join("")}</span>
        </div>
        <div class="filters">
          <span class="chips">${[["all", "All"], ["owned", "Owned"], ["missing", "Missing"]].map(([v, t]) => chip("own", v, t, f.own === v)).join("")}</span>
          <label>Uncap <select data-f="lb">${opts([["", "Any"], ...[0, 1, 2, 3, 4].map((n) => [n, `${n}/4`]), ["notmax", "Not max"]], f.lb)}</select></label>
          <label>Level <select data-f="lvl">${opts([["", "Any"], ["max", "At cap"], ["notmax", "Below cap"]], f.lvl)}</select></label>
          <label><input type="checkbox" data-f="fav" ${f.fav ? "checked" : ""}> Favourites</label>
          <button class="linkish" data-reset>Reset filters</button>
        </div>
        <div class="count-line" data-count></div>`;
    }

    function shown() {
      const q = f.q.trim().toLowerCase();
      const list = cat.supports.cards.filter((c) => {
        const o = owned.get(c.id);
        if (q && !(`${c.name} ${c.title} ${c.id}`.toLowerCase().includes(q))) return false;
        if (f.types.length && !f.types.includes(c.type)) return false;
        if (f.rar.length && !f.rar.includes(c.rarity)) return false;
        if (f.own === "owned" && !o) return false;
        if (f.own === "missing" && o) return false;
        if (f.lb !== "" && (!o || (f.lb === "notmax" ? o.lb >= 4 : o.lb !== +f.lb))) return false;
        if (f.lvl && (!o || (f.lvl === "max") !== (o.level >= capOf(c, o.lb)))) return false;
        if (f.fav && !o?.favorite) return false;
        return true;
      });
      const by = {
        new: (a, b) => b.start - a.start || b.id - a.id, old: (a, b) => a.start - b.start || a.id - b.id,
        name: (a, b) => a.name.localeCompare(b.name) || a.id - b.id, rarity: (a, b) => b.rarity - a.rarity || b.id - a.id,
        type: (a, b) => TYPE_ORDER[a.type] - TYPE_ORDER[b.type] || b.rarity - a.rarity,
        lb: (a, b) => (owned.get(b.id)?.lb ?? -1) - (owned.get(a.id)?.lb ?? -1),
        level: (a, b) => (owned.get(b.id)?.level ?? 0) - (owned.get(a.id)?.level ?? 0),
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
          return `<div class="ctile ${o ? "" : "missing"} ${sel.set.has(c.id) ? "on" : ""}" data-id="${c.id}" title="${esc(c.name)} [${esc(c.title)}] · ${c.id}">
            ${o?.stock ? `<span class="ct-own" title="Spare copies">+${o.stock}</span>` : ""}
            <span class="ct-check"></span>
            ${gameIcon("support", c.id)}
            <span class="ct-name"><span class="type-dot t-${c.type}"></span>${esc(c.name)}</span>
            <span class="ct-sub">${esc(c.title)}</span>
            ${o ? `<span>${lbDiamonds(o.lb)} <span class="faint">Lv${o.level}${o.favorite ? " ♥" : ""}</span></span>` : `<span class="faint">not owned</span>`}
          </div>`;
        }).join("") || empty("No cards match.");
      });
      top.querySelector("[data-count]").innerHTML = selectionBar(sel.set.size, list.length, cat.supports.cards.length)
        + `<span class="faint">· ${owned.size} owned</span>`;
      renderFoot();
    }

    function syncSel() {
      for (const t of grid.querySelectorAll(".ctile")) t.classList.toggle("on", sel.set.has(+t.dataset.id));
      top.querySelector("[data-count]").innerHTML = selectionBar(sel.set.size, order.length, cat.supports.cards.length) + `<span class="faint">· ${owned.size} owned</span>`;
      renderFoot();
    }

    function renderFoot() {
      const n = sel.set.size;
      foot.innerHTML = `<div class="act">
        <button class="btn primary sm" data-do="give" ${n ? "" : "disabled"} title="Add the selected cards you don't own, with the settings on the right">${icon("plus")} Give</button>
        <span class="grp">Uncap <select data-b="lb">${opts([["", "keep"], ...[0, 1, 2, 3, 4].map((x) => [x, x])], bulk.lb)}</select></span>
        <span class="grp">Level <select data-b="level">${opts([["", "keep"], ["max", "max"], ["n", "number…"]], ["", "max"].includes(bulk.level) ? bulk.level : "n")}</select>
          ${!["", "max"].includes(bulk.level) ? `<input type="number" class="num" data-b="levelN" min="1" max="50" value="${esc(bulk.level || 1)}">` : ""}</span>
        <span class="grp">Copies <input type="number" class="num" data-b="stock" min="0" max="999" placeholder="keep" value="${esc(bulk.stock)}"></span>
        <span class="grp">Fav <select data-b="fav">${opts([["", "keep"], ["1", "yes"], ["0", "no"]], bulk.fav)}</select></span>
        <label class="check"><input type="checkbox" data-b="give" ${bulk.give ? "checked" : ""}> also give missing</label>
        <button class="btn sm" data-do="set" ${n ? "" : "disabled"}>${icon("check")} Apply to ${n}</button>
        <span class="grow"></span>
        <button class="btn danger sm" data-do="remove" ${n ? "" : "disabled"}>${icon("trash")} Remove</button></div>`;
    }

    const bulkArgs = () => ({ lb: bulk.lb, level: bulk.level, stock: bulk.stock, favorite: bulk.fav, give_missing: bulk.give });
    async function run(action, btn) {
      const ids = [...sel.set];
      if (action === "remove" && !(await ask(win, { title: `Remove ${ids.length} support card(s)?`, danger: true, ok: "Remove",
        text: "They're also taken out of any deck. Undo is in History." }))) return;
      const r = await act(btn, "Support cards", () => call("supports.apply", { ids, action, ...bulkArgs() }),
        (r) => action === "remove" ? `Removed ${r.removed}` : `${r.added} added · ${r.changed} changed`);
      if (r) load();
    }

    function editOne(id) {
      const c = cat.supportById.get(id), o = owned.get(id);
      let lb = o?.lb ?? 4, level = o?.level ?? capOf(c, 4), stock = o?.stock ?? 0, fav = o?.favorite ?? 0;
      const m = modal(win, "");
      const draw = () => {
        const cap = capOf(c, lb);
        level = Math.min(level, cap);
        m.box.innerHTML = `<div class="ed-head">${gameIcon("supportart", id, "card-art")}<div class="grow">
            <h3>${esc(c.name)}</h3><div class="faint">${esc(c.title)} · ${c.id}</div>
            <div class="row" style="margin-top:6px"><span class="rar rar-${c.rarity}">${RARITY[c.rarity]}</span><span><span class="type-dot t-${c.type}"></span>${esc(c.type)}</span>
              <span class="faint">${o ? "owned" : "not owned"}</span></div></div></div>
          <div class="field"><label>Uncap</label><div class="seg">${[0, 1, 2, 3, 4].map((n) => `<button data-lb="${n}" class="${lb === n ? "on" : ""}">${n}</button>`).join("")}</div></div>
          <div class="field"><label>Level <span class="accent">${level}</span> / ${cap}</label>
            <input type="range" class="single" min="1" max="${cap}" value="${level}" data-lv style="--p:${(level - 1) / Math.max(1, cap - 1) * 100}%"></div>
          <div class="ed-row"><label>Spare copies<input type="number" class="num" data-st min="0" max="999" value="${stock}"></label>
            <label class="check"><input type="checkbox" data-fv ${fav ? "checked" : ""}> Favourite</label></div>
          <div class="modal-foot">${o ? `<button class="btn danger sm" data-x="rm">${icon("trash")} Remove</button><span class="grow"></span>` : ""}
            <button class="btn sm" data-x="close">Cancel</button><button class="btn primary sm" data-x="save">${icon("save")} ${o ? "Save" : "Give"}</button></div>`;
      };
      m.box.addEventListener("click", async (e) => {
        const b = e.target.closest("[data-lb]");
        if (b) { lb = +b.dataset.lb; draw(); return; }
        const x = e.target.closest("[data-x]")?.dataset.x;
        if (x === "close") m.close();
        if (x === "save") {
          const r = await act(e.target, "Support card", () => call("supports.apply", { ids: [id], action: "set", give_missing: true,
            lb, level, stock: m.box.querySelector("[data-st]").value, favorite: m.box.querySelector("[data-fv]").checked ? 1 : 0 }), "Saved");
          if (r) { m.close(); load(); }
        }
        if (x === "rm") {
          const r = await act(e.target, "Support card", () => call("supports.apply", { ids: [id], action: "remove" }), "Removed");
          if (r) { m.close(); load(); }
        }
      });
      m.box.addEventListener("input", (e) => {
        if (e.target.matches("[data-lv]")) {
          level = +e.target.value;
          const cap = capOf(c, lb);
          e.target.style.setProperty("--p", `${(level - 1) / Math.max(1, cap - 1) * 100}%`);
          m.box.querySelector(".field label .accent").textContent = level;
        }
      });
      draw();
    }

    body.addEventListener("click", (e) => {
      const tile = e.target.closest(".ctile");
      if (tile) { sel.click(+tile.dataset.id, order, e); syncSel(); return; }
      const ch = e.target.closest("[data-chip]");
      if (ch) {
        const g = ch.dataset.chip, v = g === "rar" ? +ch.dataset.v : ch.dataset.v;
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
      const tile = e.target.closest(".ctile");
      if (tile) { sel.only(+tile.dataset.id); syncSel(); editOne(+tile.dataset.id); }
    });
    top.addEventListener("input", (e) => {
      const k = e.target.dataset.f;
      if (!k) return;
      f[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      win.save(); render();
    });
    foot.addEventListener("change", (e) => {
      const k = e.target.dataset.b;
      if (!k) return;
      if (k === "give") bulk.give = e.target.checked;
      else if (k === "level") bulk.level = e.target.value === "n" ? "1" : e.target.value;
      else if (k === "levelN") bulk.level = e.target.value;
      else bulk[k] = e.target.value;
      win.save(); renderFoot();
    });

    const off = store.on((kind) => {
      if (kind === "target") { cat = null; sel.none(); load(); }
      if (kind === "history") load();
    });
    load();
    return off;
  },
};
