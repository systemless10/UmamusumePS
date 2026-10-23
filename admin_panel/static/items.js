
import { act, call, esc, fmt, gameIcon, icon, keepScroll, loadCatalog, store } from "./core.js";
import { ask, empty, opts, targetPicker } from "./widgets.js";

const GROUPS = ["Recovery", "Training", "Race", "Scouting", "Currency", "Other"];
const TABS = [["items", "Items"], ["wallet", "Wallet"], ["pieces", "Star pieces"], ["charas", "Characters"]];

export const itemsWin = {
  title: "Items", icon: "box", size: [640, 480], place: [0.2, 0.05, 0.7, 0.9], key: "2",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    win.state.tab ??= "items";
    win.state.f ??= { q: "", group: "", owned: false };
    const f = win.state.f;
    let cat = null, data = null, charas = null, pend = {}, pendPieces = {};
    body.classList.add("flush");

    async function load() {
      try {
        cat = await loadCatalog();
        data = await call("items.owned");
        if (win.state.tab === "pieces" || win.state.tab === "charas") charas = await call("charas.list");
      } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      pend = {}; pendPieces = {};
      render();
    }

    const tabs = () => `<div class="tabs" style="margin:0">${TABS.map(([k, t]) => `<button data-tab="${k}" class="${win.state.tab === k ? "on" : ""}">${t}</button>`).join("")}</div>`;
    function render() {
      if (!data) return;
      const t = win.state.tab;
      if (t === "items") return renderItems();
      if (t === "wallet") return renderWallet();
      if (!charas) { load(); return; }
      if (t === "pieces") return renderPieces();
      return renderCharas();
    }

    function renderItems() {
      const q = f.q.trim().toLowerCase();
      const list = cat.items.filter((i) => i.id !== 43 && (!f.group || i.group === f.group)
        && (!q || i.name.toLowerCase().includes(q) || String(i.id) === q) && (!f.owned || (data.items[i.id] || 0) > 0 || pend[i.id] !== undefined));
      const nPend = Object.keys(pend).length;
      keepScroll(body, () => {
        body.innerHTML = `<div class="pane-top">${tabs()}
            <div class="filters"><label class="search"><span>${icon("search")}</span><input type="text" data-q placeholder="Item name or id" value="${esc(f.q)}" spellcheck="false"></label>
              <label><input type="checkbox" data-owned ${f.owned ? "checked" : ""}> owned only</label></div>
            <div class="inv-groups" style="margin:0">${["", ...GROUPS].map((g) => `<button class="chipbtn ${f.group === g ? "on" : ""}" data-group="${g}">${g || "All"}</button>`).join("")}</div>
          </div>
          <div class="pane-scroll"><div class="inv-grid">${list.map((i) => {
            const n = pend[i.id] ?? data.items[i.id] ?? 0;
            return `<div class="inv-item ${n ? "" : "zero"} ${pend[i.id] !== undefined ? "dirty" : ""}" title="${esc(i.name)} · #${i.id}&#10;${esc(i.desc)}">
              ${gameIcon("item", i.id)}<div class="inv-name">${esc(i.name)}</div>
              <input type="number" class="num" data-item="${i.id}" value="${n}" min="0" style="width:100%;padding:3px 6px;font-size:12px;text-align:center"></div>`;
          }).join("") || empty("No items match.")}</div></div>
          <div class="pane-foot"><button class="btn primary sm" data-a="save" ${nPend ? "" : "disabled"}>${icon("save")} Save ${nPend || ""} change${nPend === 1 ? "" : "s"}</button>
            <button class="btn sm" data-a="discard" ${nPend ? "" : "disabled"}>Discard</button><span class="grow"></span>
            <span class="grp act"><span class="faint">${esc(f.group || "All groups")}:</span>
              <input type="number" class="num" data-bulkn value="999" min="0">
              <button class="btn sm" data-a="give" title="Raise every item shown in this group to at least this many">Give all</button>
              <button class="btn sm" data-a="owned" title="Set every owned item in this group to this many">Set owned</button>
              <button class="btn danger sm" data-a="clear">Clear</button></span></div>`;
      });
    }

    function renderWallet() {
      const w = data.wallet, tp = data.tp, rp = data.rp;
      const field = (k, label, v) => `<label>${label}<input type="number" class="num wide" data-w="${k}" value="${v ?? 0}" min="0"></label>`;
      body.innerHTML = `<div class="pane-top">${tabs()}</div><div class="pane-scroll"><div class="editor">
        <div class="ed-sec"><div class="section">Carats</div><div class="ed-row">${field("fcoin", "Free carats", w.fcoin)}${field("coin", "Paid carats", w.coin)}
          <button class="btn sm" data-quick="carats">Both 999,999,999</button></div>
          <div class="faint">Free carats are spent first. Total ${fmt((w.fcoin || 0) + (w.coin || 0))}.</div></div>
        <div class="ed-sec"><div class="section">Money</div><div class="ed-row">${field("money", "Monies (item 59)", data.items[59] || 0)}</div></div>
        <div class="ed-sec"><div class="section">Training & race points</div><div class="ed-row">
          ${field("tp", "TP", tp.current_tp)}${field("max_tp", "TP max", tp.max_tp)}${field("rp", "RP", rp.current_rp)}${field("max_rp", "RP max", rp.max_rp)}
          <button class="btn sm" data-quick="fill">Fill to max</button></div></div>
        <div class="row"><button class="btn primary" data-a="wallet">${icon("save")} Save wallet</button></div></div></div>`;
    }

    function renderPieces() {
      const q = f.q.trim().toLowerCase();
      const seen = new Set();
      const rows = cat.umas.cards.filter((c) => c.piece_id && !seen.has(c.piece_id) && seen.add(c.piece_id))
        .filter((c) => !q || `${c.name} ${c.piece_id}`.toLowerCase().includes(q));
      const n = Object.keys(pendPieces).length;
      keepScroll(body, () => {
        body.innerHTML = `<div class="pane-top">${tabs()}<div class="filters"><label class="search"><span>${icon("search")}</span>
          <input type="text" data-q placeholder="Uma name" value="${esc(f.q)}" spellcheck="false"></label></div></div>
          <div class="pane-scroll"><table class="grid left"><thead><tr><th>Uma</th><th>Piece id</th><th>Pieces</th></tr></thead><tbody>
          ${rows.map((c) => `<tr><td><span class="row">${gameIcon("chara", c.chara_id, "xs")}${esc(c.name)}</span></td><td class="faint">${c.piece_id}</td>
            <td><input type="number" class="num" data-piece="${c.piece_id}" value="${pendPieces[c.piece_id] ?? charas.pieces[c.piece_id] ?? 0}" min="0"></td></tr>`).join("")}
          </tbody></table></div>
          <div class="pane-foot"><button class="btn primary sm" data-a="pieces" ${n ? "" : "disabled"}>${icon("save")} Save ${n || ""}</button>
            <span class="grow"></span><input type="number" class="num" data-pall value="999"><button class="btn sm" data-a="pall">Set all shown</button></div>`;
      });
    }

    function renderCharas() {
      const rows = new Map(charas.charas.map((c) => [c.chara_id, c]));
      const ids = Object.keys(cat.misc.charas).map(Number).sort((a, b) => cat.misc.charas[a].localeCompare(cat.misc.charas[b]));
      const q = f.q.trim().toLowerCase();
      const shown = ids.filter((id) => !q || cat.misc.charas[id].toLowerCase().includes(q));
      win.charSel ||= new Set();
      const sel = win.charSel;
      keepScroll(body, () => {
        body.innerHTML = `<div class="pane-top">${tabs()}<div class="filters"><label class="search"><span>${icon("search")}</span>
          <input type="text" data-q placeholder="Character" value="${esc(f.q)}" spellcheck="false"></label>
          <span class="faint">${rows.size} in collection · ${sel.size} selected</span>
          <button class="linkish" data-csel="all">Select shown</button><button class="linkish" data-csel="none">Clear</button></div></div>
          <div class="pane-scroll"><table class="grid left"><thead><tr><th></th><th>Character</th><th>Bond</th><th>Fans</th><th>Careers</th><th>Best class</th></tr></thead><tbody>
          ${shown.map((id) => { const c = rows.get(id); return `<tr class="${sel.has(id) ? "on" : ""}"><td><input type="checkbox" data-cpick="${id}" ${sel.has(id) ? "checked" : ""}></td>
            <td><span class="row">${gameIcon("chara", id, "xs")}${esc(cat.misc.charas[id])}${c ? "" : ` <span class="faint">not in collection</span>`}</span></td>
            <td>${c ? fmt(c.love_point) : "–"}</td><td>${c ? fmt(c.fan) : "–"}</td><td>${c ? fmt(c.training_num) : "–"}</td><td>${c ? fmt(c.max_grade) : "–"}</td></tr>`; }).join("")}
          </tbody></table></div>
          <div class="pane-foot act"><span class="grp">Field <select data-cf>${opts([["love_point", "Bond (love point)"], ["fan", "Fans"], ["training_num", "Careers run"], ["max_grade", "Best class"]], win.state.cf || "love_point")}</select>
            <input type="number" class="num wide" data-cv value="${win.state.cv ?? 9999}"></span>
            <label class="check"><input type="checkbox" data-cadd checked> add missing</label>
            <button class="btn primary sm" data-a="charas" ${sel.size ? "" : "disabled"}>Set on ${sel.size}</button></div>`;
      });
    }

    async function onAction(a, btn) {
      if (a === "save") {
        if (await act(btn, "Items", () => call("items.set", { changes: pend }), "Items saved")) load();
      } else if (a === "discard") { pend = {}; render(); }
      else if (a === "give" || a === "owned" || a === "clear") {
        const count = +body.querySelector("[data-bulkn]").value;
        if (a === "clear" && !(await ask(win, { title: `Remove every owned item in ${f.group || "all groups"}?`, danger: true, ok: "Clear" }))) return;
        if (await act(btn, "Items", () => call("items.bulk", { action: a, group: f.group, count }), (r) => `${r.changed} items changed`)) load();
      } else if (a === "wallet") {
        const args = {};
        for (const el of body.querySelectorAll("[data-w]")) args[el.dataset.w] = el.value;
        if (await act(btn, "Wallet", () => call("wallet.set", args), "Wallet saved")) load();
      } else if (a === "pieces") {
        if (await act(btn, "Star pieces", () => call("pieces.set", { changes: pendPieces }), "Star pieces saved")) { charas = null; load(); }
      } else if (a === "pall") {
        const v = body.querySelector("[data-pall]").value;
        for (const el of body.querySelectorAll("[data-piece]")) { el.value = v; pendPieces[el.dataset.piece] = +v; }
        render();
      } else if (a === "charas") {
        const field = body.querySelector("[data-cf]").value, value = body.querySelector("[data-cv]").value;
        win.state.cf = field; win.state.cv = value; win.save();
        if (await act(btn, "Characters", () => call("charas.set", { ids: [...win.charSel], field, value, add_missing: body.querySelector("[data-cadd]").checked }),
          (r) => `${r.changed} characters changed`)) { charas = null; load(); }
      }
    }

    body.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.tab) { win.state.tab = b.dataset.tab; win.save(); if ((b.dataset.tab === "pieces" || b.dataset.tab === "charas") && !charas) load(); else render(); return; }
      if (b.dataset.group !== undefined) { f.group = b.dataset.group; win.save(); render(); return; }
      if (b.dataset.quick === "carats") { for (const k of ["fcoin", "coin"]) body.querySelector(`[data-w="${k}"]`).value = 999999999; return; }
      if (b.dataset.quick === "fill") {
        body.querySelector('[data-w="tp"]').value = body.querySelector('[data-w="max_tp"]').value;
        body.querySelector('[data-w="rp"]').value = body.querySelector('[data-w="max_rp"]').value;
        return;
      }
      if (b.dataset.csel) {
        const q = f.q.trim().toLowerCase();
        if (b.dataset.csel === "all") for (const id of Object.keys(cat.misc.charas)) { if (!q || cat.misc.charas[id].toLowerCase().includes(q)) win.charSel.add(+id); }
        else win.charSel.clear();
        render(); return;
      }
      if (b.dataset.a) onAction(b.dataset.a, b);
    });
    body.addEventListener("input", (e) => {
      const t = e.target;
      if (t.matches("[data-q]")) { f.q = t.value; win.save(); render(); }
      else if (t.matches("[data-owned]")) { f.owned = t.checked; win.save(); render(); }
      else if (t.dataset.item) {
        const id = +t.dataset.item, v = Math.max(0, +t.value || 0);
        if (v === (data.items[id] || 0)) delete pend[id]; else pend[id] = v;
        t.closest(".inv-item").classList.toggle("dirty", pend[id] !== undefined);
        const n = Object.keys(pend).length;
        const sb = body.querySelector('[data-a="save"]');
        sb.disabled = !n; sb.innerHTML = `${icon("save")} Save ${n || ""} change${n === 1 ? "" : "s"}`;
        body.querySelector('[data-a="discard"]').disabled = !n;
      } else if (t.dataset.piece) {
        pendPieces[t.dataset.piece] = +t.value || 0;
        const sb = body.querySelector('[data-a="pieces"]');
        sb.disabled = false; sb.innerHTML = `${icon("save")} Save ${Object.keys(pendPieces).length}`;
      } else if (t.dataset.cpick) {
        const id = +t.dataset.cpick;
        t.checked ? win.charSel.add(id) : win.charSel.delete(id);
        render();
      }
    });

    win.reveal = (o) => { if (o.tab) { win.state.tab = o.tab; render(); } };
    const off = store.on((kind) => {
      if (kind === "target") { data = null; charas = null; load(); }
    });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
