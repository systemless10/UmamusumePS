
import { act, ago, bytes, call, dateTime, esc, icon, store } from "./core.js";
import { ask, dot, empty, opts, tabbed } from "./widgets.js";
import { champions } from "./champions.js";
import { runJob } from "./jobs.js";

const STAGE = { "0:0": "Before entry", "1:1": "Main stage", "1:2": "Main stage: results", "2:1": "Finals: entry",
  "2:2": "Finals: matching", "2:3": "Finals: race", "2:4": "Finals: results" };
const localInput = (ts) => {
  const d = new Date(ts * 1000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
};

function lohTab(body, win) {
  let d = null;
  const target = () => ({ region: store.region, viewer: store.viewer });
  async function load() {
    if (store.region !== "global") { body.innerHTML = empty("League of Heroes is served on Global only."); return; }
    try { d = await call("pvp.heroes", {}, target()); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
    render();
  }
  function render() {
    const a = d.active, cur = d.schedule;
    const row = cur && d.heroes.find((h) => h.id === cur.event_id);
    const short = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const stages = row ? row.stages.map((s) => {
      const shift = cur.start_date - row.start_date, now = d.now;
      const st = s.start + shift, en = s.end + shift;
      const name = esc(STAGE[`${s.stage}:${s.step}`] || `Stage ${s.stage}.${s.step}`);
      const cls = en < now ? "done" : st <= now ? "now" : "";
      return `<div class="pvp-step ${cls}" title="${dateTime(st)} → ${dateTime(en)}">${en < now ? icon("check") + " " : ""}${name}
        <small>${st <= now && now <= en ? "now · ends " + short(en) : en < now ? "done" : "from " + short(st)}</small></div>`;
    }).join("") : "";
    body.innerHTML = `<div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
      <div class="ed-sec">
        ${cur ? `<div class="pvp-head"><b class="grow">League of Heroes ${cur.event_id}</b>
            ${a ? dot(true, "live") : cur.end_date < d.now ? dot(false, "ended") : dot(null, "scheduled")}
            ${d.published?.event_id === cur.event_id && d.published.start_date === cur.start_date ? "" : `<span class="tag" style="--c:var(--warn)">not published</span>`}</div>
          <div class="pvp-steps">${stages}</div>
          <p class="faint" style="font-size:12px;margin:0">Stages follow the clock: ${dateTime(cur.start_date)} → ${dateTime(cur.end_date)}. To move them, serve it again with a different start time.</p>
          <div class="row">${store.viewer ? `<button class="btn sm" data-a="reset">${icon("undo")} Reset this trainer's run</button>` : ""}
            <span class="grow"></span><button class="btn sm" data-a="stop">${icon("trash")} Stop LoH</button></div>`
          : `<span class="faint">No League of Heroes is being served.</span>`}
      </div>
      <div class="ed-sec"><b>${cur ? "Serve again / another League of Heroes" : "Serve a League of Heroes"}</b>
        <div class="row" style="flex-wrap:wrap">
          <select class="grow" data-id>${opts(d.heroes.map((h) => [h.id, `LoH ${h.id}${h.ported ? " (ported)" : ""} · JP ${new Date(h.start_date * 1000).toLocaleDateString()} · ${Math.round(h.span / 86400)} days${h.generated ? "" : " · no generated opponents"}`]), cur?.event_id ?? d.heroes[0]?.id)}</select>
          <label class="faint">Starts <input type="datetime-local" data-start value="${localInput(d.now)}" title="Your local time"></label>
          <label class="faint" title="Blank = JP's original length">Length <input type="number" data-days min="2" max="60" placeholder="as JP" style="width:70px"> days</label>
        </div>
        <div class="row"><button class="btn primary sm" data-a="start" ${d.heroes.length ? "" : "disabled"}>${icon("play")} Serve</button></div>
        <p class="faint" style="font-size:12px">Uses JP's stage layout (entry, 6-day main stage, finals) moved to your start time; Length stretches the whole event.
          ${d.heroes.length ? "" : d.future_content === false ? "League of Heroes is not released on Global yet. Turn on “Enable Admin Panel support for future Global content” in the plugin settings to use it." : "No LoH is in this master or a ported umapack."}</p>
      </div>
      <p class="faint" style="font-size:12px">Pack ${esc(d.digest ? d.digest.slice(0, 12) : "none")} · ${d.enabled ? "private master on" : "private master off"}. Restart the game (server_events=true) after a change.</p>
    </div>`;
  }
  body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]");
    if (!b) return;
    const t = target();
    if (b.dataset.a === "start") {
      const start = Math.floor(new Date(body.querySelector("[data-start]").value).getTime() / 1000);
      const days = body.querySelector("[data-days]").value;
      await act(b, "League of Heroes", () => call("pvp.heroes_start", { heroes_id: +body.querySelector("[data-id]").value, start_date: start, days: days || null }, t),
        "Published. Restart the game to load it.");
    }
    if (b.dataset.a === "stop") {
      if (!await ask(win.parent, { title: "Stop League of Heroes?", text: "It disappears after the next game restart. Saved progress is kept.", ok: "Stop", danger: true })) return;
      await act(b, "League of Heroes", () => call("pvp.heroes_stop", {}, t), "Stopped. Restart the game to load it.");
    }
    if (b.dataset.a === "reset") {
      if (!await ask(win.parent, { title: "Reset this trainer's LoH run?", text: "Forgets league score, tickets and team for the trainer being edited.", ok: "Reset", danger: true })) return;
      await act(b, "League of Heroes", () => call("pvp.heroes_reset_viewer", {}, t), "Reset");
    }
    load();
  });
  win.refresh = load;
  const off = store.on((k) => { if (k === "target") load(); });
  body.innerHTML = empty("Loading…");
  load();
  return off;
}

const STYLE_SHORT = { 1: "Front", 2: "Pace", 3: "Late", 4: "End", 5: "Runaway" };
const KIND = { real: "Real teams", generated: "Generated", loh_sheet: "Generated (LoH)", list: "Team list" };
export function poolLabel(key) {
  const [kind, name] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
  const cm = (n) => n.replace(/^cm(\d+)(.*)$/, (_, num, rest) => `CM${num}${rest ? " " + rest.replace(/^-/, "") : ""}`);
  if (kind === "real") return name === "auto" ? "Real teams · this CM's own" : `Real teams · ${cm(name)}`;
  if (kind === "loh") return name === "auto" ? "Generated · this LoH's own" : `Generated · LoH ${name}`;
  if (kind === "gen") return `Generated · ${name}`;
  if (kind === "list") return `Team list · ${name}`;
  return key;
}
const TIERS_EXAMPLE = [
  { pct: [0, 25], templates: [0, 20], stats: { speed: [800, 1050], stamina: [600, 850], power: [600, 850], guts: [300, 500], wiz: [500, 800] }, skills: [8, 12], golds: [0, 2] },
  { pct: [25, 90] },
  { pct: [90, 100], templates: [95, 100], stats: { ace: { speed: [1650, 1800], power: [1300, 1450] } }, golds: [6, 8], unique_level: [5, 6] },
];

function opponentsTab(body, win) {
  const T = "Opponents";
  let d = null, browse = null, edits = {}, gen = null;
  const tgt = { region: "global", viewer: null };
  const poolOpts = (sel) => opts(d.pools.map((p) => [p.pool, `${poolLabel(p.pool)}${p.teams ? ` · ${p.teams} teams` : ""}`]), sel);

  async function load() {
    if (store.region !== "global") { body.innerHTML = empty("PvP opponents are Global only."); return; }
    try { d = await call("pvp.opp_state", {}, tgt); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
    edits = { cm: structuredClone(d.mix.cm), loh: structuredClone(d.mix.loh) };
    gen ??= { name: "", kind: "cm", id: d.cms[0]?.id ?? 1, teams: 3000, as_of: "auto", variety: 0.2,
              tiers: JSON.stringify(TIERS_EXAMPLE, null, 1) };
    render();
  }

  function mixSec(mode) {
    const m = edits[mode], total = m.sources.reduce((a, s) => a + (+s.weight || 0), 0) || 1;
    const band = (s, i) => {
      const v = Array.isArray(s.band) ? "custom" : s.band || "round";
      return `<select data-band="${mode}:${i}">${opts([["round", "Round band"], ["any", "Any strength"], ["custom", "Custom %"]], v)}</select>
        ${v === "custom" ? `<input class="num" data-blo="${mode}:${i}" value="${s.band[0]}" style="width:52px">–<input class="num" data-bhi="${mode}:${i}" value="${s.band[1]}" style="width:52px">` : ""}`;
    };
    return `<div class="ed-sec"><b>${mode === "cm" ? "Champions Meeting" : "League of Heroes"} mix</b>
      <span class="faint" style="font-size:12px">each slot draws one whole team: a source by weight, then a team in its band. Weight 0 = fallback only.</span>
      <table class="grid compact left"><thead><tr><th>Pool</th><th>Weight</th><th>Share</th><th>Strength</th><th></th></tr></thead><tbody>
      ${m.sources.map((s, i) => `<tr><td><select data-pool="${mode}:${i}">${poolOpts(s.pool)}${["real:auto", "loh:auto"].map((k) => `<option value="${k}" ${s.pool === k ? "selected" : ""}>${poolLabel(k)}</option>`).join("")}</select></td>
        <td><input type="number" class="num" step="0.05" min="0" data-w="${mode}:${i}" value="${s.weight}" style="width:70px"></td>
        <td class="faint">${s.weight > 0 ? Math.round(100 * s.weight / total) + "%" : "fallback"}</td><td>${band(s, i)}</td>
        <td><button class="btn sm" data-a="rm-src" data-k="${mode}:${i}" title="Remove">${icon("close")}</button></td></tr>`).join("")}
      </tbody></table>
      ${m.pinned?.length ? `<div class="faint" style="font-size:12px;margin-top:6px">Pinned (served first when inside the round band):</div>
        ${m.pinned.map((r, i) => `<span class="pill">${esc(poolLabel(r.pool))} / <span class="mono">${esc(r.id.slice(0, 10))}</span>
          <button class="wbtn" data-a="unpin" data-k="${mode}:${i}">${icon("close")}</button></span>`).join(" ")}` : ""}
      <div class="ed-row"><button class="btn sm" data-a="add-src" data-k="${mode}">${icon("plus")} Add source</button><span class="grow"></span>
        <button class="btn sm" data-a="reset-mix" data-k="${mode}">Default</button>
        <button class="btn primary sm" data-a="save-mix" data-k="${mode}">${icon("save")} Save ${mode.toUpperCase()} mix</button></div></div>`;
  }

  function poolsSec() {
    return `<div class="ed-sec"><b>Pools</b> <span class="faint" style="font-size:12px">real = scraped Global teams as they were; generated = real plans re-fit to a course/era/strength (house rules)</span>
      <table class="grid compact left"><thead><tr><th>Pool</th><th>Kind</th><th>Teams</th><th>Course</th><th></th></tr></thead><tbody>
      ${d.pools.map((p) => `<tr><td>${esc(poolLabel(p.pool))}</td><td>${KIND[p.kind] || p.kind}</td><td>${p.teams ?? "–"}</td><td class="wrap">${esc(p.course || "")}</td>
        <td class="row" style="gap:4px"><button class="btn sm" data-a="browse" data-k="${esc(p.pool)}">Browse</button>
        ${p.kind === "generated" ? `<button class="btn sm" data-a="spec" data-k="${esc(p.pool.slice(4))}">Edit spec</button>
          <button class="btn sm" data-a="del-pool" data-k="${esc(p.pool.slice(4))}" title="Delete">${icon("trash")}</button>` : ""}
        ${p.kind === "list" ? `<button class="btn sm" data-a="del-list" data-k="${esc(p.pool.slice(5))}" title="Delete list">${icon("trash")}</button>` : ""}</td></tr>`).join("")}
      </tbody></table></div>`;
  }

  function browseSec() {
    if (!browse) return "";
    const lists = Object.keys(d.lists);
    return `<div class="ed-sec"><b>Teams in ${esc(poolLabel(browse.pool))}</b> <span class="faint">${browse.count} in [${browse.min}, ${browse.max}]</span>
      <div class="ed-row"><label>Percentile<input class="num" data-bmin value="${browse.min}" style="width:60px">–<input class="num" data-bmax value="${browse.max}" style="width:60px"></label>
        <button class="btn sm" data-a="rebrowse">Show</button><span class="grow"></span>
        <label>List<input data-listname list="opp-lists" value="${esc(lists[0] || "picked")}" style="width:110px"><datalist id="opp-lists">${lists.map((l) => `<option value="${esc(l)}">`).join("")}</datalist></label></div>
      <table class="grid compact left"><thead><tr><th>%</th><th>Team</th><th></th></tr></thead><tbody>
      ${browse.teams.map((t, i) => `<tr><td>${(+t.percentile).toFixed(1)}</td><td class="wrap">${t.members.map((m) =>
        `<div>${esc(m.name)} <span class="faint">${STYLE_SHORT[m.style] || "?"}${m.role && m.role !== "ace" ? " · " + m.role : ""} · ${m.stats.join("/")} · ${m.skills} skills${m.aptitudes ? " · " + m.aptitudes.join("") : ""}</span></div>`).join("")}</td>
        <td class="row" style="gap:4px;flex-wrap:wrap">
        ${browse.pool.startsWith("list:") ? `<button class="btn sm" data-a="unlist" data-i="${i}">Remove</button>` :
          `<button class="btn sm" data-a="add-list" data-i="${i}">+ List</button>`}
          <button class="btn sm" data-a="pin" data-i="${i}" data-k="cm">Pin CM</button><button class="btn sm" data-a="pin" data-i="${i}" data-k="loh">Pin LoH</button></td></tr>`).join("")}
      </tbody></table></div>`;
  }

  function genSec() {
    const g = gen;
    const ids = g.kind === "cm" ? d.cms.map((c) => [c.id, `CM ${c.id} · ${c.name}`]) : g.kind === "loh" ? d.heroes.map((h) => [h, `LoH ${h}`]) : null;
    return `<div class="ed-sec"><b>Generate a pool</b> <span class="faint" style="font-size:12px">every team copies one real team's plan (comp, roles, decks, parents, skills); only stats are re-ranged and course/era misfits swapped</span>
      <div class="ed-row"><label>Name<input data-g="name" value="${esc(g.name)}" placeholder="cm1-early" style="width:130px"></label>
        <label>For<select data-g="kind">${opts([["cm", "Champions Meeting"], ["loh", "League of Heroes"], ["ri", "Race instance"]], g.kind)}</select></label>
        ${ids ? `<select data-g="id">${opts(ids, g.id)}</select>` : `<input class="num" data-g="id" value="${esc(g.id)}" style="width:90px">`}</div>
      <div class="ed-row"><label>Teams<input type="number" class="num" data-g="teams" value="${g.teams}" min="50" max="20000" style="width:80px"></label>
        <label title="auto = the event's Global start: later trainees, cards and skills are swapped for ones out then">Era<input data-g="as_of" value="${esc(g.as_of)}" style="width:100px" placeholder="auto / 2025-08-17 / none"></label>
        <label title="share of whites swapped for role-equivalents so teams from one plan differ">Variety<input type="number" class="num" step="0.05" min="0" max="1" data-g="variety" value="${g.variety}" style="width:60px"></label></div>
      <label class="faint" style="font-size:12px">Tiers (JSON): pct = share of this pool; templates = which real band's plans to copy (weak tiers copy bottom plans, whales top);
        stats per stat or per role (ace/debuffer/runaway); skills / golds counts; unique_level. A stat with no range keeps the real value.</label>
      <textarea data-g="tiers" class="mono" rows="9" style="width:100%">${esc(g.tiers)}</textarea>
      <div class="ed-row"><span class="grow"></span><button class="btn primary sm" data-a="gen">${icon("refresh")} Generate</button></div></div>`;
  }

  function render() {
    const y = body.querySelector(".pane-scroll")?.scrollTop || 0;
    body.innerHTML = `<div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
      ${mixSec("cm")}${mixSec("loh")}${poolsSec()}${browseSec()}${genSec()}</div>`;
    body.querySelector(".pane-scroll").scrollTop = y;
  }

  const key = (k) => { const [mode, i] = k.split(":"); return [mode, +i]; };
  async function showTeams(pool, min = 0, max = 100) {
    const r = await act(null, T, () => call("pvp.opp_teams", { pool, min, max, limit: 80 }, tgt));
    if (r) { browse = { ...r, min, max }; render(); }
  }
  async function saveList(name, refs) {
    if (await act(null, T, () => call("pvp.opp_list", { name, refs }, tgt), `List ${name} saved.`)) await load();
  }

  body.addEventListener("change", (e) => {
    const el = e.target;
    if (el.dataset.pool) { const [m, i] = key(el.dataset.pool); edits[m].sources[i].pool = el.value; render(); }
    else if (el.dataset.w) { const [m, i] = key(el.dataset.w); edits[m].sources[i].weight = +el.value; render(); }
    else if (el.dataset.band) { const [m, i] = key(el.dataset.band); edits[m].sources[i].band = el.value === "custom" ? [0, 100] : el.value; render(); }
    else if (el.dataset.blo || el.dataset.bhi) {
      const [m, i] = key(el.dataset.blo || el.dataset.bhi), b = edits[m].sources[i].band;
      b[el.dataset.blo ? 0 : 1] = +el.value;
    } else if (el.dataset.g) {
      gen[el.dataset.g] = el.type === "number" ? +el.value : el.value;
      if (el.dataset.g === "kind") { gen.id = el.value === "cm" ? d.cms[0]?.id : el.value === "loh" ? d.heroes[0] : ""; render(); }
    }
  });
  body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]");
    if (!b) return;
    const a = b.dataset.a, k = b.dataset.k;
    if (a === "add-src") { edits[k].sources.push({ pool: d.pools[0]?.pool || "real:auto", weight: 1, band: "round" }); render(); }
    else if (a === "rm-src") { const [m, i] = key(k); edits[m].sources.splice(i, 1); render(); }
    else if (a === "unpin") { const [m, i] = key(k); edits[m].pinned.splice(i, 1); render(); }
    else if (a === "save-mix") { if (await act(b, T, () => call("pvp.opp_mix", { mode: k, mix: edits[k] }, tgt), `${k.toUpperCase()} mix saved: next race uses it.`)) await load(); }
    else if (a === "reset-mix") { if (await act(b, T, () => call("pvp.opp_mix", { mode: k, reset: true }, tgt), "Back to the default mix.")) await load(); }
    else if (a === "browse") await showTeams(k);
    else if (a === "rebrowse") await showTeams(browse.pool, +body.querySelector("[data-bmin]").value, +body.querySelector("[data-bmax]").value);
    else if (a === "pin") {
      const t = browse.teams[+b.dataset.i];
      edits[k].pinned = [...(edits[k].pinned || []), { pool: t.pool, id: t.id }];
      if (await act(b, T, () => call("pvp.opp_mix", { mode: k, mix: edits[k] }, tgt), `Pinned to the ${k.toUpperCase()} mix.`)) await load();
    } else if (a === "add-list") {
      const t = browse.teams[+b.dataset.i], name = body.querySelector("[data-listname]").value.trim();
      if (!name) return;
      await saveList(name, [...(d.lists[name] || []), { pool: t.pool, id: t.id }]);
    } else if (a === "unlist") {
      const name = browse.pool.slice(5), t = browse.teams[+b.dataset.i];
      await saveList(name, (d.lists[name] || []).filter((r) => !(r.pool === t.pool && r.id === t.id)));
      await showTeams(browse.pool, browse.min, browse.max);
    } else if (a === "del-list") {
      if (!await ask(win, { title: `Delete list ${k}?`, text: "Mixes that use it must drop it first.", ok: "Delete", danger: true })) return;
      if (await act(b, T, () => call("pvp.opp_list", { name: k, delete: true }, tgt))) await load();
    } else if (a === "del-pool") {
      if (!await ask(win, { title: `Delete gen:${k}?`, text: "The generated teams are removed from disk.", ok: "Delete", danger: true })) return;
      if (await act(b, T, () => call("pvp.opp_pool_delete", { name: k }, tgt))) await load();
    } else if (a === "spec") {
      const m = await act(b, T, () => call("pvp.opp_pool_spec", { name: k }, tgt));
      if (!m) return;
      const sp = m.spec || {}, t = sp.target || {};
      gen = { name: sp.name, kind: "champions_id" in t ? "cm" : "heroes_id" in t ? "loh" : "ri",
              id: t.champions_id ?? t.heroes_id ?? t.race_instance_id, teams: sp.teams || 3000,
              as_of: sp.as_of === null ? "none" : sp.as_of ?? "auto", variety: sp.variety ?? 0.2,
              tiers: JSON.stringify(sp.tiers || [], null, 1) };
      render();
      body.querySelector("[data-g=name]")?.scrollIntoView({ block: "center" });
    } else if (a === "gen") {
      let tiers;
      try { tiers = JSON.parse(gen.tiers || "[]"); } catch (ex) { return reportBad(`Tiers are not valid JSON: ${ex.message}`); }
      if (!/^[A-Za-z0-9_.-]{1,64}$/.test(gen.name || "")) return reportBad("Name: letters, digits, _ . - only.");
      const target = gen.kind === "cm" ? { champions_id: +gen.id } : gen.kind === "loh" ? { heroes_id: +gen.id } : { race_instance_id: +gen.id };
      const as_of = gen.as_of === "none" ? null : gen.as_of || "auto";
      const spec = { name: gen.name, target, teams: gen.teams, seed: 1, as_of, variety: gen.variety, tiers };
      runJob(b, "pvp_opponents", { spec }, { region: "global", onDone: load });
    }
  });
  const reportBad = (msg) => ask(win, { title: "Can't generate", text: msg, ok: "OK" });
  win.refresh = load;
  const off = store.on((k) => { if (k === "target") load(); });
  body.innerHTML = empty("Loading…");
  load();
  return off;
}

const TT_TERM = { 1: "Racing week", 2: "Interval", 3: "Tallying results" };
function ttTab(body, win) {
  const T = "Team Trials";
  let d = null;
  const target = () => ({ region: store.region, viewer: store.viewer });
  async function load() {
    try { d = await call("pvp.tt", {}, target()); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
    render();
  }
  const field = (k) => k.kind === "bool"
    ? `<select data-k="${k.key}">${opts([["", "Default"], ["true", "On"], ["false", "Off"]], k.value === null || k.value === undefined ? "" : String(k.value))}</select>`
    : `<input data-k="${k.key}" ${k.kind === "str" ? "" : `type="number" class="num" step="${k.kind === "int" ? 1 : 0.05}" min="${k.min}" max="${k.max}"`}
        value="${k.value ?? ""}" placeholder="default" style="width:${k.kind === "str" ? 130 : 90}px">`;
  function render() {
    const v = d.viewer;
    body.innerHTML = `<div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
      <div class="ed-sec"><div class="pvp-head"><b class="grow">This week</b><span class="faint">term ${d.term_id} · ${TT_TERM[d.term_state] || d.term_state}</span></div>
        ${v ? `<div class="faint" style="font-size:12px">Class ${v.team_class} (best ${v.best_team_class}) · best ${v.best_point} pts · ${v.members}/15 members · RP ${v.rp}/${v.max_rp}${v.in_match ? " · <b>match in progress</b>" : ""}</div>
          <div class="row" style="flex-wrap:wrap">
            <label class="faint">Class <select data-class>${opts([1, 2, 3, 4, 5, 6].map((c) => [c, `Class ${c}`]), v.team_class)}</select></label>
            <button class="btn sm" data-a="class">Set class</button>
            <button class="btn sm" data-a="refill">Refill RP</button>
            <button class="btn sm" data-a="clear_match" ${v.in_match ? "" : "disabled"}>Clear stuck match</button>
            <span class="grow"></span><button class="btn sm" data-a="reset_week">${icon("undo")} Reset week score</button></div>`
          : `<span class="faint">Pick a trainer to edit their class, RP or week.</span>`}</div>
      <div class="ed-sec"><b>Settings</b> <span class="faint" style="font-size:12px">blank = server default · applies on the next request, no restart</span>
        <table class="grid compact left"><tbody>${d.knobs.map((k) => `<tr><td title="${esc(k.help)}">${esc(k.label)}</td><td>${field(k)}</td><td class="faint wrap" style="font-size:12px">${esc(k.help)}</td></tr>`).join("")}</tbody></table>
        <div class="ed-row"><span class="grow"></span><button class="btn primary sm" data-a="save">${icon("save")} Save settings</button></div></div>
    </div>`;
  }
  body.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]");
    if (!b) return;
    const a = b.dataset.a, t = target();
    if (a === "save") {
      const values = {};
      for (const el of body.querySelectorAll("[data-k]")) values[el.dataset.k] = el.tagName === "SELECT" ? (el.value === "" ? null : el.value === "true") : el.value;
      const r = await act(b, T, () => call("pvp.tt_set", { values }, t), "Saved.");
      if (r) { d = r; render(); }
      return;
    }
    if (a === "reset_week" && !await ask(win.parent, { title: "Reset this week's score?", text: "Clears this trainer's best points for the week and any match in progress.", ok: "Reset", danger: true })) return;
    const r = await act(b, T, () => call("pvp.tt_trainer", { action: a, team_class: +(body.querySelector("[data-class]")?.value || 0) }, t), "Done.");
    if (r) { d = r; render(); }
  });
  win.refresh = load;
  const off = store.on((k) => { if (k === "target") load(); });
  body.innerHTML = empty("Loading…");
  load();
  return off;
}

export const pvpWin = {
  title: "PvP", icon: "swords", size: [620, 460], place: [0.18, 0.05, 0.62, 0.9], key: "p", group: "PvP",
  mount(body, win) {
    let off = null, dead = false;
    body.innerHTML = empty("Loading…");
    call("system.future", {}, { region: store.region, viewer: store.viewer }).catch(() => ({ future: false })).then(({ future }) => {
      if (dead) return;
      off = tabbed(body, win, [
        { id: "cm", label: "Champions Meeting", icon: "medal", mount: (b, w) => { b.style.padding = "14px 16px"; return champions.mount(b, w); } },
        { id: "tt", label: "Team Trials", icon: "users", mount: ttTab },
        ...(future ? [{ id: "loh", label: "League of Heroes", icon: "flag", mount: lohTab }] : []),
        { id: "opponents", label: "Opponents", icon: "users", mount: opponentsTab },
      ]);
    });
    return () => { dead = true; if (typeof off === "function") off(); };
  },
};
