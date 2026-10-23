
import { REGION_NAME, act, call, esc, gameIcon, icon, keepScroll, store } from "./core.js";
import { empty, modal } from "./widgets.js";

const RAR = { 1: "R", 2: "SR", 3: "SSR" };
const STAR = { 1: "★", 2: "★★", 3: "★★★" };
const day = (ts) => ts ? new Date(ts * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "";
const span = (b) => b.end ? `${day(b.start)} – ${day(b.end)}` : b.start ? `From ${day(b.start)}` : "Permanent";
const rarLabel = (b, r) => (b.kind === "uma" ? STAR : RAR)[r];

function headline(b) {
  const seen = [...new Set(b.pickups.map((p) => p.name))];
  if (!seen.length) return b.title;
  return seen.length > 3 ? `${seen.slice(0, 3).join(" · ")} +${seen.length - 3}` : seen.join(" · ");
}

const art = (b, cls = "") => `<div class="bn-art ${cls}"><img src="/icons/${store.region === "jp" ? "bannerjp" : "banner"}/${b.id}?v=2" loading="lazy" alt=""
  onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'bn-noart',textContent:${esc(JSON.stringify(b.title))}}))"></div>`;

function rateInputs(rates, real, kind) {
  return `<div class="bn-rates">${["3", "2", "1"].map((k) => `<label><span>${kind === "uma" ? STAR[k] : RAR[k]}</span>
    <input type="number" class="num xs" min="0" max="100" step="0.1" data-rate="${k}" value="${rates?.[k] ?? real[k]}">%</label>`).join("")}
    <button class="btn sm" data-rates-real>Real rates</button></div>`;
}
const readRates = (box) => Object.fromEntries([...box.querySelectorAll("[data-rate]")].map((i) => [i.dataset.rate, i.value]));

export const bannersWin = {
  title: "Banners", icon: "sparkle", size: [620, 460], place: [0.2, 0.05, 0.6, 0.9], key: "b",
  mount(body, win) {
    let d = null, q = "", show = win.state.show || "all";
    body.classList.add("flush");

    async function load() {
      try { d = await call("banners.list"); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      render();
    }

    const visible = () => d.banners.filter((b) => {
      if (show === "listed" && !b.listed) return false;
      if (show === "toggled" && b.state === "auto" && !b.rates && !Object.keys(b.boosts).length) return false;
      if (show === "uma" && b.kind !== "uma") return false;
      if (show === "support" && b.kind !== "support") return false;
      if (!q) return true;
      const hay = `${b.title} ${b.pickups.map((p) => `${p.name} ${p.title}`).join(" ")}`.toLowerCase();
      return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
    });

    function tile(b) {
      const tuned = b.rates || Object.keys(b.boosts).length || b.advanced;
      return `<div class="bn-tile ${b.listed ? "" : "off"}" data-id="${b.id}">
        ${art(b)}
        <div class="bn-info">
          <div class="bn-name ellipsis" title="${esc(headline(b))}">${esc(headline(b))}</div>
          <div class="bn-sub ellipsis">${esc(b.title)}</div>
          <div class="bn-meta"><span class="faint">${span(b)}</span>
            ${b.ported ? `<span class="chip">Ported</span>` : ""}${tuned ? `<span class="chip on">Custom rates</span>` : ""}</div>
        </div>
        <div class="bn-side">
          <label class="bn-switch" title="${b.listed ? "Hide this banner" : "List this banner"}">
            <input type="checkbox" data-toggle="${b.id}" ${b.listed ? "checked" : ""}><i></i></label>
          <span class="bn-why ${b.state !== "auto" ? "set" : ""}">${esc(b.why)}</span>
          ${b.state !== "auto" ? `<button class="linkbtn" data-auto="${b.id}" title="Drop the toggle and follow the listing rule again">Reset</button>` : ""}
        </div></div>`;
    }

    function render() {
      const list = visible();
      keepScroll(body, () => {
        body.innerHTML = `<div class="pane-top bn-top">
            <div class="row wrap">
              <div class="seg" title="Which banners the game lists when you don't toggle them">
                <button data-mode="all" class="${d.mode === "all" ? "on" : ""}">Every banner</button>
                <button data-mode="schedule" class="${d.mode === "schedule" ? "on" : ""}">Running only</button></div>
              <span class="faint grow">${d.mode === "all" ? "All past banners are listed; switch one off to hide it."
                : `Only banners running on the server date (${day(d.now)}) plus the ones you switch on.`}</span>
              <button class="btn sm" data-all title="List every banner and undo every switch-off">${icon("sparkle")} Enable all</button>
              <button class="btn sm" data-real title="Hide ported and switched-on banners; list only real banners running now">${icon("check")} Real lineup only</button>
              <button class="btn sm" data-global>${icon("settings")} Default rates${d.rates ? " •" : ""}</button>
            </div>
            <div class="filters">
              <label class="search">${icon("search")}<input type="text" data-q placeholder="Search a character or card" value="${esc(q)}" spellcheck="false"></label>
              <div class="chips">${[["all", "All"], ["listed", "Listed"], ["toggled", "Changed"], ["uma", "Trainees"], ["support", "Support cards"]].map(([k, t]) =>
                `<button class="chipbtn sm ${show === k ? "on" : ""}" data-show="${k}">${t}</button>`).join("")}</div>
              <span class="faint">${REGION_NAME[store.region]} · ${list.length} banner${list.length === 1 ? "" : "s"} · ${d.banners.filter((b) => b.listed).length} listed</span>
            </div></div>
          <div class="pane-scroll"><div class="bn-grid">${list.map(tile).join("") || empty("No banners match.")}</div></div>`;
      });
    }

    async function openBanner(id) {
      let b;
      try { b = await call("banners.cards", { id }); } catch (ex) { return act(null, "Banner", () => { throw ex; }); }
      let boosts = { ...b.boosts };
      const m = modal(win, "", { wide: true });
      const chance = (rates) => {
        const tier = {};
        for (const c of b.cards) tier[c.rarity] = (tier[c.rarity] || 0) + c.pct * (boosts[c.id] ?? 1);
        const rt = Object.fromEntries(Object.keys(tier).map((r) => [r, +(rates[r] ?? d.real_rates[r]) || 0]));
        const sum = Object.values(rt).reduce((a, x) => a + x, 0) || 1;
        return (c) => tier[c.rarity] ? rt[c.rarity] / sum * (c.pct * (boosts[c.id] ?? 1)) / tier[c.rarity] * 100 : 0;
      };
      const fmtPct = (p) => p === 0 ? "0%" : `${p.toFixed(p < 0.01 ? 4 : p < 0.1 ? 3 : 2)}%`;
      const refreshPct = () => {
        const f = chance(readRates(m.box));
        for (const el of m.box.querySelectorAll("[data-pct]")) el.textContent = fmtPct(f(b.cards[+el.dataset.pct]));
      };
      const draw = () => {
        m.box.innerHTML = `<div class="bn-head"><div class="bn-head-info">
            <h3>${esc(headline(b))}</h3>
            <div class="faint">${esc(b.title)}</div>
            <div class="faint">${span(b)}</div>
            <div class="bn-meta"><span class="bn-why ${b.state !== "auto" ? "set" : ""}">${esc(b.why)}</span>
              ${b.ported ? `<span class="chip">Ported</span>` : ""}</div></div>
            ${art(b, "big")}</div>
          <div class="bn-sec"><b>Rarity rates</b> <span class="faint">The real game uses ${d.real_rates[3]}% / ${d.real_rates[2]}% / ${d.real_rates[1]}%.</span>
            ${rateInputs(b.rates || d.rates, d.real_rates, b.kind)}</div>
          <div class="bn-sec"><b>Cards</b> <span class="faint">Boost a card to make it more likely within its rarity. ×1 is the real chance.</span>
            ${b.advanced ? `<div class="warn-t">${b.advanced} hand-written rule${b.advanced === 1 ? "" : "s"} in the config also apply and are kept.</div>` : ""}
            <div class="bn-cards">${b.cards.map((c) => {
              const x = boosts[c.id] ?? 1;
              return `<div class="bn-card ${c.pickup ? "pick" : ""} ${x !== 1 ? "boosted" : ""}">
                ${gameIcon(b.kind === "uma" ? "card" : "support", c.id)}
                <span class="ellipsis"><b>${esc(c.name)}</b> <span class="faint">${esc(c.title)}</span></span>
                <span class="chip">${rarLabel(b, c.rarity)}</span>
                <span class="faint bn-pct" data-pct="${b.cards.indexOf(c)}" title="Chance per pull"></span>
                <label class="bn-x">×<input type="number" class="num xs" min="0" max="1000" step="0.5" data-boost="${c.id}" value="${x}"></label></div>`;
            }).join("")}</div></div>
          <div class="modal-foot"><button class="btn" data-x>Close</button><button class="btn" data-clear>Real odds</button>
            <button class="btn primary" data-save>${icon("save")} Save</button></div>`;
      };
      draw();
      refreshPct();
      m.box.addEventListener("input", (e) => {
        const bi = e.target.closest("[data-boost]");
        if (bi) {
          boosts[bi.dataset.boost] = bi.value === "" ? 1 : +bi.value;
          bi.closest(".bn-card").classList.toggle("boosted", boosts[bi.dataset.boost] !== 1);
        }
        if (bi || e.target.closest("[data-rate]")) refreshPct();
      });
      m.box.addEventListener("click", async (e) => {
        if (e.target.closest("[data-x]")) return m.close();
        if (e.target.closest("[data-rates-real]")) {
          m.box.querySelectorAll("[data-rate]").forEach((i) => { i.value = d.real_rates[i.dataset.rate]; });
          return refreshPct();
        }
        if (e.target.closest("[data-clear]")) {
          boosts = {};
          draw();
          m.box.querySelectorAll("[data-rate]").forEach((i) => { i.value = d.real_rates[i.dataset.rate]; });
          return refreshPct();
        }
        const s = e.target.closest("[data-save]");
        if (s) {
          const r = await act(s, "Save banner", () => call("banners.set", { id: b.id, rates: readRates(m.box), boosts }), "Banner saved");
          if (r) { m.close(); load(); }
        }
      });
    }

    async function realLineup(btn) {
      const ported = d.banners.filter((b) => b.ported).length;
      const m = modal(win, `<h3>Real lineup only</h3>
        <p>Lists only the real banners running on the server date (${day(d.now)}).</p>
        <ul class="bn-list"><li>Switches to <b>Running only</b></li>
          <li>Drops every banner you switched on</li>
          ${ported ? `<li>Hides all ${ported} ported banners</li>` : ""}</ul>
        <label><input type="checkbox" data-rr> Also put every banner back on the real rates</label>
        <div class="modal-foot"><button class="btn" data-x>Cancel</button><button class="btn primary" data-ok>Apply</button></div>`);
      m.box.addEventListener("click", async (e) => {
        if (e.target.closest("[data-x]")) return m.close();
        const ok = e.target.closest("[data-ok]");
        if (ok) {
          const r = await act(ok, "Real lineup", () => call("banners.real", { rates: m.box.querySelector("[data-rr]").checked }),
            (r) => `${r.banners.filter((b) => b.listed).length} real banners listed`);
          if (r) { d = r; m.close(); render(); }
        }
      });
    }

    async function globalRates() {
      const m = modal(win, `<h3>Default rates</h3>
        <p class="faint">Rarity rates for every banner without its own. The real game uses ${d.real_rates[3]}% / ${d.real_rates[2]}% / ${d.real_rates[1]}%.</p>
        ${rateInputs(d.rates, d.real_rates, "support")}
        <div class="modal-foot"><button class="btn" data-x>Cancel</button><button class="btn primary" data-ok>Save</button></div>`);
      m.box.addEventListener("click", async (e) => {
        if (e.target.closest("[data-x]")) return m.close();
        if (e.target.closest("[data-rates-real]")) { m.box.querySelectorAll("[data-rate]").forEach((i) => { i.value = d.real_rates[i.dataset.rate]; }); return; }
        const ok = e.target.closest("[data-ok]");
        if (ok) {
          const r = await act(ok, "Default rates", () => call("banners.mode", { mode: d.mode, rates: readRates(m.box) }), "Default rates saved");
          if (r) { d = r; m.close(); render(); }
        }
      });
    }

    body.addEventListener("input", (e) => {
      if (e.target.matches("[data-q]")) { q = e.target.value; render(); }
    });
    body.addEventListener("change", async (e) => {
      const t = e.target.closest("[data-toggle]");
      if (!t) return;
      const id = +t.dataset.toggle;
      const r = await act(t, "Banner switch", () => call("banners.set", { id, state: t.checked ? "on" : "off" }));
      if (r) Object.assign(d.banners.find((b) => b.id === id), r, { cards: null });
      render();
    });
    body.addEventListener("click", async (e) => {
      const m = e.target.closest("[data-mode]");
      if (m && m.dataset.mode !== d.mode) {
        const r = await act(m, "Banner listing", () => call("banners.mode", { mode: m.dataset.mode }));
        if (r) { d = r; render(); }
        return;
      }
      const s = e.target.closest("[data-show]");
      if (s) { show = win.state.show = s.dataset.show; render(); return; }
      if (e.target.closest("[data-global]")) return globalRates();
      const al = e.target.closest("[data-all]");
      if (al) {
        const r = await act(al, "Enable all", () => call("banners.all"), (r) => `${r.banners.filter((b) => b.listed).length} banners listed`);
        if (r) { d = r; render(); }
        return;
      }
      const rl = e.target.closest("[data-real]");
      if (rl) return realLineup(rl);
      const a = e.target.closest("[data-auto]");
      if (a) {
        const id = +a.dataset.auto;
        const r = await act(a, "Banner switch", () => call("banners.set", { id, state: "auto" }));
        if (r) Object.assign(d.banners.find((b) => b.id === id), r, { cards: null });
        render();
        return;
      }
      if (e.target.closest(".bn-switch, button, input")) return;
      const tileEl = e.target.closest(".bn-tile");
      if (tileEl) openBanner(+tileEl.dataset.id);
    });

    load();
  },
};
