
import { REGION_NAME, act, call, esc, icon, store } from "./core.js";
import { empty, opts } from "./widgets.js";

const STAT_LABEL = { speed: "Speed", stamina: "Stamina", power: "Power", guts: "Guts", wiz: "Wit", skill_point: "Skill points" };

const normalOf = (k) => (k.kind === "mult" ? 1 : k.kind === "shift" || (k.kind === "int" && !k.unset) ? 0 : null);
const unsetV = (v) => v === null || v === undefined || v === "";
const isNormal = (k, v) => unsetV(v) || (Array.isArray(v) && !v.length)
  || (typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length)
  || (k.kind === "select" && v === k.options[0][0])
  || (normalOf(k) !== null && +v === normalOf(k));
const num = (x) => Math.round(x).toLocaleString();
const pct = (p) => `${+(p * 100).toFixed(1)}%`;

function luckP(p, L, good) {
  L = Math.max(-1, Math.min(1, L / 100));
  if (!good) L = -L;
  if (L >= 1) return p > 0 ? 1 : 0;
  if (L <= -1) return 0;
  if (p <= 0 || p >= 1) return p;
  const o = p / (1 - p) * 2 ** (4 * L);
  return o / (1 + o);
}
function failShift(r, s) { s = s / 100; return Math.round(s < 0 ? r * (1 + s) : r + (100 - r) * s); }

const EX = {
  training_gain_multiplier: (v) => `A training worth +20 Speed / +8 Power gives <b>+${num(20 * v)} / +${num(8 * v)}</b>`,
  failure_rate_shift: (v) => `Real 5% / 20% / 50% become <b>${[5, 20, 50].map((r) => failShift(r, v) + "%").join(" / ")}</b>`,
  energy_cost_multiplier: (v) => `A training that costs −20 energy costs <b>−${num(20 * v)}</b>`,
  bond_gain_multiplier: (v) => `+7 friendship per training becomes <b>+${num(7 * v)}</b>`,
  hint_rate_multiplier: (v) => `A card's 7% hint chance becomes <b>${pct(Math.min(1, 0.07 * v))}</b>`,
  specialty_priority_bonus: (v) => `A card's pull to its own type: ~50 → <b>${num(50 + v)}</b> (others stay ~20)`,
  skill_point_multiplier: (v) => `+15 SP becomes <b>+${num(15 * v)}</b>`,
  fan_gain_multiplier: (v) => `10,000 fans from a race become <b>${num(10000 * v)}</b>`,
  spark_proc_multiplier: (v) => `A spark with a 20% proc chance procs <b>${pct(Math.min(1, 0.2 * v))}</b> of the time (before Luck)`,
  legacy_spark_multiplier: (v) => `A learned skill becomes a spark <b>${pct(Math.min(1, 0.2 * v))}</b> of the time (real 20%)`,
  legacy_star_shift: (v) => {
    const L = v / 100, w = [20, 70, 10];
    let p = L >= 1 ? [0, 0, 1] : L <= -1 ? [1, 0, 0] : w.map((x, i) => x * 2 ** (4 * L * i));
    const t = p.reduce((a, b) => a + b);
    p = p.map((x) => Math.round(x / t * 100));
    return `A top-score career's 1★/2★/3★ odds (real 20/70/10%) become <b>${p.join(" / ")}%</b>`;
  },
  luck: (v) => `Hint 7% → <b>${pct(luckP(0.07, v, true))}</b> · Failure 20% → <b>${pct(luckP(0.2, v, false))}</b> · Bad condition 30% → <b>${pct(luckP(0.3, v, false))}</b>`,
  event_rate_multiplier: (v) => `A 7% event chance becomes <b>${pct(Math.min(1, 0.07 * v))}</b>`,
  condition_proc_multiplier: (v) => `A 30% Night Owl proc becomes <b>${pct(Math.min(1, 0.3 * v))}</b>`,
  rival_strength_multiplier: (v) => `A rival with 800 Speed has <b>${num(Math.min(2000, Math.max(1, 800 * v)))}</b>`,
  race_fatigue_multiplier: (v) => `−15 energy after a race becomes <b>−${num(15 * v)}</b>`,
};

function chip(k, v) {
  if (k.kind === "mult") return v === 0 ? "×0 (off)" : `×${+(+v).toFixed(2)}`;
  if (k.kind === "shift") return (k.names?.[v] ? k.names[v] + " " : "") + (v ? `${v > 0 ? "+" : ""}${v}` : "");
  return String(v);
}

export const tuningWin = {
  title: "Career tuning", icon: "bolt", size: [640, 600], place: [0.26, 0.05, 0.54, 0.9], key: "0",
  mount(body, win) {
    let d = null, edits = {};
    body.classList.add("flush");

    const val = (k) => (k.key in edits ? edits[k.key] : k.value);
    const cur = (k) => (unsetV(val(k)) ? normalOf(k) : +val(k));
    const luckOn = () => { const l = d.knobs.find((k) => k.key === "luck"); return l && !isNormal(l, val(l)); };
    const locked = (k) => k.luck_locked && luckOn();
    const P = (k, x) => ((Math.max(k.range[0], Math.min(k.range[1], x)) - k.range[0]) / (k.range[1] - k.range[0])) * 100;

    async function load() {
      try { d = await call("tuning.get"); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      edits = {};
      render();
    }

    function slider(k) {
      const v = cur(k), dis = locked(k) ? "disabled" : "";
      const n = P(k, normalOf(k)), p = P(k, v);
      const ticks = k.ticks.map((t) => `<span style="left:${P(k, t)}%" class="${t === normalOf(k) ? "n" : ""}">${
        t === normalOf(k) ? "Normal" : k.kind === "mult" ? "×" + t : k.names?.[t] ?? (t > 0 ? "+" + t : t)}</span>`).join("");
      return `<div class="tune-slide">
        <input type="range" class="tune-range" data-slide="${k.key}" min="${k.range[0]}" max="${k.range[1]}" step="${k.step}" value="${v}"
          style="--lo:${Math.min(n, p)}%;--hi:${Math.max(n, p)}%;--n:${n}%" ${dis}>
        <div class="tune-scale">${ticks}</div></div>`;
    }

    function perstat(k) {
      const v = val(k) || {}, base = cur(d.knobs.find((x) => x.key === "training_gain_multiplier")) ?? 1;
      return `<div class="tune-stats">${d.stats.map((s) => {
        const r = v[s] || {}, mode = r.add ? "add" : (r.x ?? 1) !== 1 ? "mult" : "off";
        const amt = mode === "add" ? r.add : mode === "mult" ? r.x : "";
        const after = mode === "add" ? Math.max(0, Math.round(20 * base) + r.add) : Math.round(20 * base * (r.x ?? 1));
        return `<span class="${mode !== "off" ? "on" : ""}">${STAT_LABEL[s]}</span>
          <select data-psmode="${s}">${opts([["off", "Normal"], ["mult", "Multiply ×"], ["add", "Add +"]], mode)}</select>
          ${mode === "off" ? "<span></span>" : `<input type="number" class="num" data-psamt="${s}" value="${amt}"
            ${mode === "mult" ? 'min="0" max="100" step="0.05"' : 'min="-1000" max="10000" step="1"'}>`}
          <span class="faint">${mode === "off" ? "real formula" : `+20 → <b class="on">+${after}</b>${mode === "add" ? " (only when the training gives it)" : ""}`}</span>`;
      }).join("")}</div>`;
    }

    function row(k, groupWhen) {
      const v = val(k), changed = !isNormal(k, v), lock = locked(k);
      let ctl = "", head = "";
      if (k.kind === "select") ctl = `<select data-k="${k.key}">${opts(k.options, v ?? k.options[0][0])}</select>`;
      else if (k.kind === "stats") {
        const on = new Set(v || []);
        ctl = `<div class="row" style="flex-wrap:wrap;gap:4px 12px">${d.stats.map((s) =>
          `<label><input type="checkbox" data-stat="${s}" ${on.has(s) ? "checked" : ""}> ${STAT_LABEL[s]}</label>`).join("")}</div>`;
      } else if (k.kind === "perstat") ctl = perstat(k);
      else if (k.range) {
        ctl = slider(k);
        head = `<input type="number" class="tune-chip ${changed ? "on" : ""}" data-exact="${k.key}" value="${cur(k)}" min="${k.min}" max="${k.max}" step="${k.step}" ${lock ? "disabled" : ""}
          title="Type an exact value (${k.min} to ${k.max})"><span class="faint tune-name">${esc(chip(k, cur(k)).replace(/^×[\d.]+$|^[+-]?\d+$/, ""))}</span>`;
      } else {
        ctl = `<input type="number" class="num" data-exact="${k.key}" value="${v ?? ""}" min="${k.min}" max="${k.max}" placeholder="${k.unset || "normal"}" style="width:110px">`;
      }
      const ex = EX[k.key] && k.range ? `<div class="tune-ex">${EX[k.key](cur(k))}</div>` : "";
      return `<div class="tune-row ${lock ? "locked" : ""}" data-row="${k.key}">
        <div class="tune-head"><b>${esc(k.label)}</b>${head}<span class="grow"></span>
          ${lock ? `<span class="tag" title="Luck already moves these rolls">set by Luck</span>` : ""}
          ${changed && !lock ? `<button class="btn sm tune-reset" data-normal="${k.key}" title="Back to Normal">${icon("undo")} Normal</button>` : ""}</div>
        ${ctl}${ex}
        <div class="tune-help">${esc(k.help)}${k.when !== groupWhen ? ` <span class="tune-when">${icon("clock")} ${esc(k.when)}</span>` : ""}</div>
      </div>`;
    }

    function render() {
      const y = body.querySelector(".pane-scroll")?.scrollTop || 0;
      const groups = [...new Set(d.knobs.map((k) => k.group))];
      const active = d.knobs.filter((k) => !isNormal(k, val(k)) && !locked(k));
      const dirty = Object.keys(edits).length;
      body.innerHTML = `<div class="pane-top"><div class="faint">${REGION_NAME[store.region]} · every account in this region · Normal = the real game
          <br><span class="mono" style="font-size:11px">${esc(d.path)}</span></div>
          <div style="margin-top:6px;font-size:12px">${active.length
            ? `${icon("bolt")} ${active.length} change${active.length > 1 ? "s" : ""} from the real game: ${active.map((k) => `<b>${esc(k.label)}</b>`).join(", ")}`
            : `<span class="faint">Everything is Normal: careers play exactly like the real game.</span>`}</div></div>
        <div class="pane-scroll"><div class="tune-groups">${groups.map((g) => {
          const ks = d.knobs.filter((k) => k.group === g);
          const whens = ks.map((k) => k.when), gw = whens.sort((a, b) => whens.filter((x) => x === b).length - whens.filter((x) => x === a).length)[0];
          return `<section class="tune-group"><div class="tune-ghead"><b>${esc(g)}</b>
              <span class="faint">${icon("clock")} takes effect: ${esc(gw)}</span></div>
            ${g.startsWith("Exact") ? `<div class="tune-help">For testing: a pin replaces the formula outright.</div>` : ""}
            ${ks.map((k) => row(k, gw)).join("")}</section>`;
        }).join("")}</div></div>
        <div class="pane-foot row"><button class="btn sm" data-a="normal-all">${icon("undo")} Everything back to Normal</button><span class="grow"></span>
          ${dirty ? `<span class="faint" style="font-size:12px">${dirty} unsaved</span><button class="btn sm" data-a="discard">Discard</button>` : ""}
          <button class="btn primary sm" data-a="save" ${dirty ? "" : "disabled"}>${icon("save")} Save</button></div>`;
      body.querySelector(".pane-scroll").scrollTop = y;
    }

    const set = (key, v) => { edits[key] = v; render(); };
    body.addEventListener("input", (e) => {
      const el = e.target;
      if (!el.dataset.slide) return;
      const k = d.knobs.find((x) => x.key === el.dataset.slide), v = +el.value;
      edits[k.key] = v;
      const n = P(k, normalOf(k)), p = P(k, v);
      el.style.setProperty("--lo", Math.min(n, p) + "%");
      el.style.setProperty("--hi", Math.max(n, p) + "%");
      const r = el.closest(".tune-row");
      const c = r.querySelector(".tune-chip");
      c.value = v; c.classList.toggle("on", !isNormal(k, v));
      r.querySelector(".tune-name").textContent = chip(k, v).replace(/^×[\d.]+$|^[+-]?\d+$/, "");
      const ex = r.querySelector(".tune-ex");
      if (ex) ex.innerHTML = EX[k.key](v);
    });
    body.addEventListener("change", (e) => {
      const el = e.target;
      if (el.dataset.slide) render();
      else if (el.dataset.exact) set(el.dataset.exact, el.value === "" ? null : +el.value);
      else if (el.dataset.k) set(el.dataset.k, el.value);
      else if (el.dataset.psmode || el.dataset.psamt) {
        const k = d.knobs.find((x) => x.kind === "perstat"), st = el.dataset.psmode || el.dataset.psamt;
        const t = JSON.parse(JSON.stringify(val(k) || {}));
        if (el.dataset.psmode) {
          if (el.value === "off") delete t[st];
          else t[st] = el.value === "mult" ? { x: 2, add: 0 } : { x: 1, add: 5 };
        } else {
          const n = el.value === "" ? null : +el.value, add = !!t[st]?.add;
          if (n === null || (add ? n === 0 : n === 1)) delete t[st];
          else t[st] = add ? { x: 1, add: n } : { x: n, add: 0 };
        }
        set(k.key, t);
      } else if (el.dataset.stat) {
        const k = d.knobs.find((x) => x.kind === "stats");
        const on = new Set(val(k) || []);
        el.checked ? on.add(el.dataset.stat) : on.delete(el.dataset.stat);
        set(k.key, d.stats.filter((s) => on.has(s)));
      }
    });
    body.addEventListener("click", async (e) => {
      const n = e.target.closest("[data-normal]");
      if (n) return set(n.dataset.normal, null);
      const b = e.target.closest("[data-a]");
      if (!b) return;
      if (b.dataset.a === "discard") { edits = {}; render(); return; }
      if (b.dataset.a === "normal-all") {
        for (const k of d.knobs) if (!isNormal(k, val(k))) edits[k.key] = null;
        render();
        return;
      }
      const values = Object.fromEntries(Object.entries(edits).map(([k, v]) => [k, v ?? null]));
      const r = await act(b, "Career tuning", () => call("tuning.set", { values }),
        "Saved. Each change reaches the game at the time shown on its group.");
      if (r) { d = r; edits = {}; render(); }
    });

    win.refresh = load;
    const off = store.on((kind) => { if (kind === "target") load(); });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
