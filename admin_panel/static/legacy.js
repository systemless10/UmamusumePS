
import { act, call, esc, gameIcon, icon, keepScroll, loadCatalog, loadExtra, reportError, store } from "./core.js";
import { APT_FIELDS, APT_LABELS, STATS, STYLES, aptRow, ask, empty, factorRow, factorSearch, gradeOptions, modal, openBrowser, opts,
  picker, skillRow, skillSearch, targetPicker, umaRow, umaSearch } from "./widgets.js";
import { openUmaMoe } from "./umamoe.js";

const SORTS = [["score", "Rating"], ["new", "Newest"], ["old", "Oldest"], ["name", "Name"], ["stats", "Stat total"],
               ["sparks", "Spark stars"], ["skills", "Skills"]];
const blank = () => ({ q: "", rank: "", scenario: "", style: "", kind: "", spark: "", sparkMin: 1, parents: false,
                       apt: Array(10).fill(""), locked: "", band: "", sort: "score" });
const KINDS = [["", "Any spark"], ["blue", "Blue"], ["pink", "Pink"], ["green", "Green"], ["white", "White"], ["race", "Race"], ["scenario", "Scenario"]];
const POSITIONS = [[10, "Parent 1"], [11, "Parent 1 · grandparent A"], [12, "Parent 1 · grandparent B"],
                   [20, "Parent 2"], [21, "Parent 2 · grandparent A"], [22, "Parent 2 · grandparent B"]];

export const legacyWin = {
  title: "Legacy umas", icon: "medal", size: [760, 520], place: [0.08, 0.03, 0.9, 0.94], key: "5",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    win.state.f = { ...blank(), ...(win.state.f || {}) };
    const f = win.state.f;
    if (!Array.isArray(f.apt) || f.apt.length !== 10) f.apt = Array(10).fill("");
    const sel = new Set();
    let cat = null, factors = null, skills = null, data = null, order = [];
    body.classList.add("flush");
    body.innerHTML = `<div class="pane-top" data-filters></div><div class="pane-scroll"><div class="vt-list" data-list></div></div><div class="pane-foot" data-foot></div>`;
    const list = body.querySelector("[data-list]"), top = body.querySelector("[data-filters]"), foot = body.querySelector("[data-foot]");
    const fac = (id) => factors?.byId.get(id) || { name: `#${id}`, kind: "other", stars: id % 10 };

    async function load() {
      try {
        [cat, factors, skills] = await Promise.all([loadCatalog(), loadExtra("factors"), loadExtra("skills")]);
        data = await call("legacy.list");
      } catch (ex) { list.innerHTML = empty(esc(ex.message)); return; }
      const ids = new Set(data.veterans.map((v) => v.id));
      for (const id of [...sel]) if (!ids.has(id)) sel.delete(id);
      renderFilters();
      render();
    }

    function renderFilters() {
      const ranks = cat.misc.ranks;
      top.innerHTML = `<div class="filters">
          <label class="search"><span>${icon("search")}</span><input type="text" data-f="q" placeholder="Name, outfit or id" value="${esc(f.q)}" spellcheck="false"></label>
          <label>Sort <select data-f="sort">${opts(SORTS, f.sort)}</select></label></div>
        <div class="filters">
          <label>Rank ≥ <select data-f="rank">${opts([["", "Any"], ...ranks.filter((r, i) => i % 2 === 0 || r.id > 18).map((r) => [r.id, r.label])], f.rank)}</select></label>
          <label>Scenario <select data-f="scenario">${opts([["", "Any"], ...Object.entries(cat.misc.scenarios)], f.scenario)}</select></label>
          <label>Style <select data-f="style">${opts([["", "Any"], ...Object.entries(STYLES)], f.style)}</select></label>
          <label>Box <select data-f="band">${opts([["", "All"], ["career", "Finished careers"], ["inject", "Added"], ["house", "House (re-seeded)"], ["starter", "Starters"]], f.band)}</select></label>
          <label>Lock <select data-f="locked">${opts([["", "Any"], ["1", "Locked"], ["0", "Unlocked"]], f.locked)}</select></label></div>
        <div class="filters">
          <label><select data-f="kind">${opts(KINDS, f.kind)}</select></label>
          <input type="text" class="sm" data-f="spark" placeholder="spark name (e.g. Speed, Turf, a skill)" value="${esc(f.spark)}" style="width:220px">
          <label>stars ≥ <select data-f="sparkMin">${opts([[1, "1"], [2, "2"], [3, "3"], [4, "4"], [6, "6"], [9, "9"]], f.sparkMin)}</select></label>
          <label><input type="checkbox" data-f="parents" ${f.parents ? "checked" : ""}> count parents' sparks</label>
          <button class="linkish" data-reset>Reset filters</button></div>
        <div class="apt-filter">${APT_LABELS.map((l, i) => `<label>${l}<select data-apt="${i}" class="${f.apt[i] ? "set" : ""}">${gradeOptions(f.apt[i], true)}</select></label>`).join("")}</div>
        <div class="count-line" data-count></div>`;
    }

    const sparkIds = (v) => f.parents ? [...v.factors, ...Object.values(v.parents).flatMap((p) => p.factors)] : v.factors;
    const sparkStars = (v) => sparkIds(v).reduce((n, id) => {
      const x = fac(id);
      return n + ((!f.kind || x.kind === f.kind) && (!f.spark || x.name.toLowerCase().includes(f.spark.toLowerCase())) ? x.stars : 0);
    }, 0);
    function shown() {
      const q = f.q.trim().toLowerCase();
      const out = data.veterans.filter((v) => {
        if (q && !(`${v.name} ${v.id} ${v.card_id}`.toLowerCase().includes(q))) return false;
        if (f.rank && v.rank < +f.rank) return false;
        if (f.scenario && v.scenario !== +f.scenario) return false;
        if (f.style && v.style !== +f.style) return false;
        if (f.band && v.band !== f.band) return false;
        if (f.locked !== "" && v.locked !== (f.locked === "1")) return false;
        for (let i = 0; i < 10; i++) if (f.apt[i] && v.apt[i] < +f.apt[i]) return false;
        if ((f.kind || f.spark) && sparkStars(v) < +f.sparkMin) return false;
        return true;
      });
      const total = (v) => v.stats.reduce((a, b) => a + b, 0);
      const allStars = (v) => v.factors.reduce((n, id) => n + fac(id).stars, 0);
      const by = {
        score: (a, b) => b.score - a.score, new: (a, b) => String(b.created).localeCompare(String(a.created)) || b.id - a.id,
        old: (a, b) => String(a.created).localeCompare(String(b.created)) || a.id - b.id, name: (a, b) => a.name.localeCompare(b.name),
        stats: (a, b) => total(b) - total(a), sparks: (a, b) => allStars(b) - allStars(a), skills: (a, b) => b.skills - a.skills,
      }[f.sort];
      return out.sort(by);
    }

    const TILE_SPARKS = 6;
    const sparkChips = (ids) => {
      const chip = (id) => { const x = fac(id); return `<span class="spark kind-${x.kind}" title="${esc(x.kind)}">${esc(x.name)} <b>${"★".repeat(x.stars)}</b></span>`; };
      const rest = ids.slice(TILE_SPARKS);
      return `<div class="sparks">${ids.slice(0, TILE_SPARKS).map(chip).join("")}${rest.length
        ? `<span class="spark more" title="${esc(rest.map((id) => fac(id).name).join(", "))}">+${rest.length}</span>` : ""}</div>`;
    };

    const STAT_ABBR = ["Spd", "Sta", "Pow", "Gut", "Wit"];
    const short = (n) => n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.floor(n / 1e3)}k` : String(n);
    const splitName = (n) => { const m = /^\[(.*?)\]\s*(.*)$/.exec(n || ""); return m ? [m[1], m[2]] : ["", n || ""]; };
    const whiteCount = (v) => {
      const w = v.factors.filter((id) => !["blue", "pink", "green"].includes(fac(id).kind));
      return w.length ? `<span class="faint">+${w.length} white (${w.reduce((n, id) => n + fac(id).stars, 0)}★) · ${v.skills} skills</span>` : `<span class="faint">${v.skills} skills</span>`;
    };
    function render() {
      if (!data) return;
      const vs = shown();
      order = vs.map((v) => v.id);
      keepScroll(list, () => {
        list.innerHTML = vs.map((v) => `<div class="vrow vtile ${sel.has(v.id) ? "on" : ""}" data-id="${v.id}">
          <div class="vt-head"><input type="checkbox" data-pick ${sel.has(v.id) ? "checked" : ""}>
            ${gameIcon("card", v.card_id)}
            <div class="vn"><b>${esc(splitName(v.name)[1])}</b><span class="faint vt">${esc(splitName(v.name)[0]) || "&nbsp;"}</span></div>
            <div class="vs" title="#${v.id} · score ${v.score.toLocaleString()}"><span class="rank-badge big">${esc(v.rank_label)}</span>
              <span class="faint">${v.score.toLocaleString()}</span></div></div>
          <div class="faint vm">${v.locked ? `${icon("lock")} ` : ""}<span class="band ${v.band}">${v.band}</span> ${esc(cat.misc.scenarios[v.scenario] || `scenario ${v.scenario}`)} · ${esc(String(v.created || "").slice(0, 10))}</div>
          <div class="vst">${v.stats.map((n, i) => `<span><i>${STAT_ABBR[i]}</i>${short(n)}</span>`).join("")}</div>
          ${aptRow(v.apt)}
          <div class="vk">${sparkChips(v.factors.filter((id) => ["blue", "pink", "green"].includes(fac(id).kind)))}</div>
          <div class="vt-foot">${whiteCount(v)}<span class="grow"></span><button class="btn icon-btn" data-edit title="Edit">${icon("edit")}</button>
            <button class="btn icon-btn" data-clone title="Clone">${icon("copy")}</button></div></div>`).join("") || empty("No veterans match.");
      });
      top.querySelector("[data-count]").innerHTML = `<span><b>${vs.length}</b> shown of ${data.veterans.length} / ${data.capacity}</span>
        <span>· <b>${sel.size}</b> selected</span><button class="linkish" data-sel="all">Select shown</button>
        <button class="linkish" data-sel="none">Clear</button>${data.version_ok ? "" : `<span class="warn-t">· roster will be re-seeded on next login</span>`}`;
      renderFoot();
    }

    function syncSel() {
      for (const r of list.querySelectorAll(".vrow")) {
        const on = sel.has(+r.dataset.id);
        r.classList.toggle("on", on);
        r.querySelector("[data-pick]").checked = on;
      }
      const c = top.querySelector("[data-count] span:nth-child(2) b");
      if (c) c.textContent = sel.size;
      renderFoot();
    }

    function renderFoot() {
      const n = sel.size;
      foot.innerHTML = `<div class="act">
        <button class="btn primary sm" data-do="max" ${n ? "" : "disabled"} title="9999 stats, S aptitudes, max rank, every skill">${icon("bolt")} Max</button>
        <button class="btn sm" data-do="lock" ${n ? "" : "disabled"}>${icon("lock")} Lock</button>
        <button class="btn sm" data-do="unlock" ${n ? "" : "disabled"}>Unlock</button>
        <span class="grp">Stats <input type="number" class="num" data-v="stats" value="1200" min="0" max="9999"><button class="btn sm" data-do="stats" ${n ? "" : "disabled"}>Set</button></span>
        <span class="grp">Aptitudes <select data-v="aptitudes">${gradeOptions(8)}</select><button class="btn sm" data-do="aptitudes" ${n ? "" : "disabled"}>Set</button></span>
        <button class="btn sm" data-do="rerank" ${n ? "" : "disabled"} title="Rank from rating score">Re-rank</button>
        <button class="btn danger sm" data-do="delete" ${n ? "" : "disabled"}>${icon("trash")} Delete</button>
        <span class="grow"></span>
        <button class="btn sm" data-umamoe title="Fetch a trainer's umas from uma.moe">${icon("download")} uma.moe</button>
        <span class="grp">New <span data-addpick></span><select data-v="mode">${opts([["maxed", "maxed"], ["generated", "generated"]], "maxed")}</select></span></div>`;
      const p = picker({ placeholder: "Add a veteran: search uma", width: 190, search: umaSearch(cat), row: umaRow,
        pick: async (c) => {
          const mode = foot.querySelector('[data-v="mode"]').value;
          const r = await act(null, "Add veteran", () => call("legacy.add", { card_id: c.id, mode }), `Added ${c.name}`);
          if (r) { await load(); editOne(r.id); }
        } });
      foot.querySelector("[data-addpick]").replaceWith(p);
    }

    async function run(action, btn) {
      const ids = [...sel];
      if (action === "delete" && !(await ask(win, { title: `Delete ${ids.length} veteran(s)?`, danger: true, ok: "Delete", text: "Undo is in History." }))) return;
      const value = action === "stats" || action === "aptitudes" ? foot.querySelector(`[data-v="${action}"]`).value : undefined;
      if (await act(btn, "Veterans", () => call("legacy.apply", { ids, action, value }), (r) => `${action}: ${r.changed}`)) {
        if (action === "delete") sel.clear();
        load();
      }
    }

    async function editOne(id, onSaved) {
      const m = modal(win, empty("Loading…"), { wide: true });
      let rec, band;
      try { ({ record: rec, band } = await call("legacy.get", { id })); } catch (ex) { m.box.innerHTML = empty(esc(ex.message)); return; }
      rec = structuredClone(rec);
      rec.skill_array ||= []; rec.factor_info_array ||= []; rec.succession_chara_array ||= [];
      let autoRank = false;
      const nick = cat.misc.nicknames;
      const GROUPS = [["blue", "Stats"], ["pink", "Aptitudes"], ["green", "Unique"], ["race", "Race"], ["scenario", "Scenario"], ["white", "Skills"], ["other", "Other"]];
      const closed = new Set();
      const filt = {};
      const poolHead = (key, count, extra) => `<div class="pool-head"><label class="search sm">${icon("search")}<input type="text" data-pq="${key}" placeholder="Filter ${count}…" value="${esc(filt[key] || "")}" spellcheck="false"></label>${extra}</div>`;
      const poolGroup = (key, gk, label, rows, cls = "") => {
        if (!rows.length) return "";
        const id = `${key}/${gk}`;
        return `<div class="pool-group ${closed.has(id) ? "closed" : ""}"><button type="button" class="pool-gh ${cls}" data-pg="${id}">${icon("chevron")}<b>${esc(label)}</b><span class="faint">${rows.length}</span></button>
          <div class="pool-rows">${rows.join("")}</div></div>`;
      };
      const sparkEditor = (arr, where) => {
        const groups = new Map(GROUPS.map(([k]) => [k, []]));
        const seen = new Map();
        arr.forEach((fi) => { const g = fac(fi.factor_id).group; seen.set(g, (seen.get(g) || 0) + 1); });
        const dupes = [...seen.values()].some((n) => n > 1);
        arr.forEach((fi, i) => {
          const x = fac(fi.factor_id);
          const avail = factors.filter((y) => y.group === x.group).map((y) => y.stars).sort((a, b) => a - b);
          const stars = (avail.length ? avail : [x.stars]).map((s) => `<button type="button" class="pstar ${s <= x.stars ? "on" : ""}" data-fset="${where}:${i}:${s}" title="${s}★">★</button>`).join("");
          (groups.get(x.kind) || groups.get("other")).push(`<div class="pool-row ${seen.get(x.group) > 1 ? "dupe" : ""}" data-name="${esc(x.name.toLowerCase())}">
            <span class="ellipsis spark-name kind-${x.kind}" title="${esc(x.name)} · ${fi.factor_id}${seen.get(x.group) > 1 ? " · duplicate" : ""}">${esc(x.name)}</span>
            <span class="pstars">${stars}</span><button class="wbtn" data-frm="${where}:${i}" title="Remove">${icon("close")}</button></div>`);
        });
        const total = arr.reduce((n, fi) => n + fac(fi.factor_id).stars, 0);
        return `<div class="pool">${poolHead(`f${where}`, `${arr.length} sparks`, `<span class="faint">${arr.length} sparks · ${total}★</span><span class="grow"></span>
            <span data-fpick="${where}"></span><button class="btn sm" data-fbrowse="${where}">${icon("search")} Browse…</button>
            ${dupes ? `<button class="btn sm" data-fdedupe="${where}" title="Keep the highest-star copy of each spark">Remove duplicates</button>` : ""}
            ${arr.length ? `<button class="btn sm" data-fclear="${where}">Clear</button>` : ""}`)}
          <div class="pool-body" data-pb="f${where}">${GROUPS.map(([k, l]) => poolGroup(`f${where}`, k, l, groups.get(k), `kind-${k}`)).join("") || empty("No sparks.")}</div></div>`;
      };
      const sparkArr = () => rec.factor_info_array;

      const skillPool = () => {
        const tiers = { unique: [], gold: [], white: [] };
        rec.skill_array.forEach((sk, i) => {
          const x = skills.byId.get(sk.skill_id);
          tiers[x ? tier(x) : "white"].push(`<div class="pool-row" data-name="${esc((x?.name || String(sk.skill_id)).toLowerCase())}">
            ${gameIcon("skill", x?.icon, "xs")}<span class="ellipsis ${x?.rarity >= 2 ? "r2" : ""}" title="${esc(x?.name || "")} · ${sk.skill_id}">${esc(x?.name || sk.skill_id)}</span>
            <input type="number" class="num xs" data-slv="${i}" value="${sk.level ?? 1}" min="1" max="10" title="Level">
            <button class="wbtn" data-srm="${i}" title="Remove">${icon("close")}</button></div>`);
        });
        return `<div class="pool">${poolHead("sk", `${rec.skill_array.length} skills`, `<span class="faint">${rec.skill_array.length} skills</span><span class="grow"></span><span data-skillpick></span>
            <button class="btn sm" data-skbrowse>${icon("search")} Browse…</button>${rec.skill_array.length ? `<button class="btn sm" data-clearskills>Clear</button>` : ""}`)}
          <div class="pool-body" data-pb="sk">${[["unique", "Unique"], ["gold", "Gold"], ["white", "White"]].map(([k, l]) => poolGroup("sk", k, l, tiers[k], k === "gold" ? "r2" : "")).join("") || empty("No skills.")}</div></div>`;
      };
      const lineage = () => {
        const vById = new Map(data.veterans.map((v) => [v.id, v]));
        const snap = (pos) => rec.succession_chara_array.find((p) => p.position_id === pos);
        const card = (cls, label, v, s, k) => {
          const cid = v ? v.card_id : s?.card_id;
          const u = cat.umaById.get(cid);
          const ids = v ? v.factors : (s?.factor_info_array || []).map((fi) => fi.factor_id);
          const tools = k ? `<span data-vpick="${k}"></span>${v ? `<button class="btn sm" data-punlink="${k}">Unlink</button>` : ""}` : "";
          return `<div class="parent ${cls}"><div class="row">${cid ? gameIcon("card", cid, "sm") : ""}<div class="grow"><b>${label}</b>
              <div class="faint">${cid ? `${esc(u?.name || cid)} · ${esc(u?.title || "")}` : "none"}${v ? ` · #${v.id} <span class="rank-badge">${esc(v.rank_label)}</span>`
                : s ? ` · <span class="warn-t">not a roster veteran (generated)</span>` : ""}</div></div>
              ${tools}${v ? `<button class="btn sm" data-pedit="${v.id}">${icon("edit")} Edit</button>` : ""}</div>
            ${ids.length ? sparkChips(ids) : ""}</div>`;
        };
        return `<div class="ed-sec"><div class="section">Lineage</div><div class="parent-grid">${[1, 2].map((k) => {
          const pid = rec[`succession_trained_chara_id_${k}`] || 0, v = vById.get(pid), pos = k * 10;
          const gps = [1, 2].map((g) => {
            const gv = v && vById.get(v[`p${g}`]);
            const gs = v ? (v.parents[String(g * 10)] && { card_id: v.parents[String(g * 10)].card_id, factor_info_array: v.parents[String(g * 10)].factors.map((id) => ({ factor_id: id })) }) : snap(pos + g);
            return gv || gs ? card(`gp gp${g}`, `Grandparent ${g}`, gv, gs) : "";
          }).join("");
          return card("root", `Parent ${k}`, v, v ? null : snap(pos), k) + gps;
        }).join("")}</div></div>`;
      };
      const applyFilters = () => {
        for (const body of m.box.querySelectorAll("[data-pb]")) {
          const q = (filt[body.dataset.pb] || "").trim().toLowerCase();
          for (const r of body.querySelectorAll(".pool-row")) r.hidden = !!q && !r.dataset.name.includes(q);
          for (const g of body.querySelectorAll(".pool-group")) g.classList.toggle("filtered", !!q && !g.querySelector(".pool-row:not([hidden])"));
        }
      };
      const draw = () => {
        const chara = Math.floor(rec.card_id / 100);
        const nicks = nick.filter((n) => !n.chara || n.chara === chara);
        const skillById = skills.byId;
        const keep = [m.box, ...m.box.querySelectorAll("[data-pb]")].map((el) => el.scrollTop);
        m.box.innerHTML = `<div class="editor">
          <div class="ed-head">${gameIcon("card", rec.card_id)}<div class="grow"><h3>${esc(cat.umaById.get(rec.card_id)?.name || rec.card_id)}
            <span class="faint">${esc(cat.umaById.get(rec.card_id)?.title || "")}</span></h3>
            <div class="faint">#${rec.trained_chara_id} <span class="band ${band}">${band}</span> · card ${rec.card_id} · created ${esc(rec.create_time || "–")}
            ${band === "house" ? ` · <span class="warn-t">house veterans are regenerated when the roster version changes</span>` : ""}</div></div>
            <span data-cardpick></span></div>

          <div class="ed-sec"><div class="section">Rating</div><div class="ed-row">
            <label>Rating score<input type="number" class="num wide" data-n="rank_score" value="${rec.rank_score ?? 0}"></label>
            <label>Rank<select class="sm" data-n="rank" ${autoRank ? "disabled" : ""}>${opts(cat.misc.ranks.map((r) => [r.id, `${r.label} (${r.min}+)`]), rec.rank)}</select></label>
            <label class="check"><input type="checkbox" data-auto ${autoRank ? "checked" : ""}> rank from score</label>
            <label>Fans<input type="number" class="num wide" data-n="fans" value="${rec.fans ?? 0}"></label>
            <label>Wins<input type="number" class="num" data-n="wins" value="${rec.wins ?? 0}"></label>
            <label>Class<input type="number" class="num" data-n="chara_grade" value="${rec.chara_grade ?? 0}"></label></div>
            <div class="ed-row">
            <label>Stars<input type="number" class="num xs" data-n="rarity" min="1" max="5" value="${rec.rarity ?? 3}"></label>
            <label>Potential<input type="number" class="num xs" data-n="talent_level" min="1" max="${cat.umas.max_talent}" value="${rec.talent_level ?? 1}"></label>
            <label>Style<select class="sm" data-n="running_style">${opts(Object.entries(STYLES), rec.running_style)}</select></label>
            <label>Scenario<select class="sm" data-n="scenario_id">${opts(Object.entries(cat.misc.scenarios), rec.scenario_id)}</select></label>
            <label>Epithet<select class="sm" data-n="nickname_id" style="max-width:220px">${opts([[0, "–"], ...nicks.map((n) => [n.id, n.name])], rec.nickname_id)}</select></label>
            <label class="check"><input type="checkbox" data-b="is_locked" ${rec.is_locked ? "checked" : ""}> locked</label>
            <label class="check"><input type="checkbox" data-b="is_saved" ${rec.is_saved ? "checked" : ""}> saved</label></div></div>

          <div class="ed-sec"><div class="row section-row"><div class="section">Stats</div>
            <div class="row"><button class="btn sm" data-allstat="1200">All 1200</button><button class="btn sm" data-allstat="9999">All 9999</button></div></div>
            <div class="stat-edit">${STATS.map(([k, t]) => `<label>${t}<input type="number" class="num" data-n="${k}" min="0" max="9999" value="${rec[k] ?? 0}"></label>`).join("")}</div></div>

          <div class="ed-sec"><div class="row section-row"><div class="section">Aptitudes</div>
            <div class="row"><button class="btn sm" data-allapt="8">All S</button><button class="btn sm" data-allapt="7">All A</button></div></div>
            <div class="apt-grid">${APT_FIELDS.map((k, i) => `<label>${APT_LABELS[i]}<select data-n="${k}">${gradeOptions(rec[k])}</select></label>`).join("")}</div></div>

          <div class="ed-sec"><div class="row section-row"><div class="section">Skills · ${rec.skill_array.length}</div>
            </div>
            ${skillPool()}</div>

          <div class="ed-sec"><div class="section">Sparks</div>${sparkEditor(rec.factor_info_array, "own")}</div>

          ${lineage()}

          <details class="ed-sec"><summary class="section" style="cursor:pointer">Raw record (everything else: race results, saddles, support cards…)</summary>
            <textarea class="code" data-raw spellcheck="false">${esc(JSON.stringify(rec, null, 2))}</textarea>
            <div class="row"><button class="btn sm" data-applyraw>Use this JSON</button><span class="faint">replaces the editor's copy; Save still needed</span></div></details>

          <div class="modal-foot"><button class="btn danger sm" data-x="rm">${icon("trash")} Delete</button><button class="btn sm" data-x="clone">${icon("copy")} Clone</button>
            <span class="grow"></span><button class="btn sm" data-x="close">Cancel</button>
            <button class="btn primary sm" data-x="save">${icon("save")} Save</button></div></div>`;

        m.box.querySelector("[data-cardpick]").replaceWith(picker({ placeholder: "Change card…", width: 170, search: umaSearch(cat), row: umaRow,
          pick: (c) => { rec.card_id = c.id; draw(); } }));
        m.box.querySelector("[data-skillpick]").replaceWith(picker({ placeholder: "Add skill…", width: 200, search: skillSearch(skills), row: skillRow,
          pick: (s) => { if (!rec.skill_array.some((x) => x.skill_id === s.id)) rec.skill_array.push({ skill_id: s.id, level: s.rarity >= 3 ? 6 : 1 }); draw(); } }));
        for (const ph of m.box.querySelectorAll("[data-fpick]")) {
          const where = ph.dataset.fpick;
          ph.replaceWith(picker({ placeholder: "Add spark…", width: 160, search: factorSearch(factors), row: factorRow,
            pick: (x) => {
              const best = factors.filter((y) => y.group === x.group).sort((a, b) => b.stars - a.stars)[0] || x;
              sparkArr(where).push({ factor_id: best.id, level: 0 });
              draw();
            } }));
        }
        for (const ph of m.box.querySelectorAll("[data-vpick]")) {
          const k = ph.dataset.vpick;
          ph.replaceWith(picker({ placeholder: "Pick veteran…", width: 170,
            search: (q) => data.veterans.filter((v) => v.id !== rec.trained_chara_id && (v.name.toLowerCase().includes(q) || String(v.id).includes(q))),
            row: (v) => `${gameIcon("card", v.card_id, "xs")}<span>${esc(v.name)}</span><span class="faint">${esc(v.rank_label)} · #${v.id}</span>`,
            pick: (v) => { rec[`succession_trained_chara_id_${k}`] = v.id; draw(); } }));
        }
        applyFilters();
        [m.box, ...m.box.querySelectorAll("[data-pb]")].forEach((el, i) => { if (keep[i]) el.scrollTop = keep[i]; });
      };

      const tier = (s) => s.rarity >= 3 ? "unique" : s.rarity === 2 ? "gold" : "white";
      const browseSkills = () => openBrowser(win, { title: "Skills", items: skills,
        filters: [["", "All", null], ["white", "White", (s) => tier(s) === "white"], ["gold", "Gold", (s) => tier(s) === "gold"], ["unique", "Unique", (s) => tier(s) === "unique"]],
        tile: (s) => `${gameIcon("skill", s.icon, "xs")}<span class="ellipsis ${s.rarity >= 2 ? "r2" : ""}">${esc(s.name)}</span>`,
        isOn: (s) => rec.skill_array.some((x) => x.skill_id === s.id),
        toggle: (s, on) => {
          if (on) rec.skill_array.push({ skill_id: s.id, level: s.rarity >= 3 ? 6 : 1 });
          else rec.skill_array = rec.skill_array.filter((x) => x.skill_id !== s.id);
        }, onDone: draw });
      const browseSparks = (where) => {
        const best = new Map();
        for (const x of factors) if (!best.has(x.group) || x.stars > best.get(x.group).stars) best.set(x.group, x);
        const inArr = (x) => sparkArr(where).findIndex((fi) => fac(fi.factor_id)?.group === x.group);
        openBrowser(win, { title: where === "own" ? "Sparks" : `Sparks · ${POSITIONS.find(([p]) => p === +where)?.[1]}`, items: [...best.values()],
          filters: KINDS.map(([v, l]) => [v, v ? l : "All", v ? (x) => x.kind === v : null]),
          tile: (x) => `<span class="spark kind-${x.kind}">${esc(x.name)}</span><span class="faint">${"★".repeat(x.stars)}</span>`,
          isOn: (x) => inArr(x) >= 0,
          toggle: (x, on) => { const arr = sparkArr(where); if (on) arr.push({ factor_id: x.id, level: 0 }); else arr.splice(inArr(x), 1); },
          onDone: draw });
      };
      const num = (v) => (v === "" || v === null ? 0 : Number(v));
      m.box.addEventListener("input", (e) => {
        const t = e.target;
        if (t.dataset.pq) { filt[t.dataset.pq] = t.value; applyFilters(); }
        else if (t.dataset.n) rec[t.dataset.n] = num(t.value);
        else if (t.dataset.b) rec[t.dataset.b] = t.checked ? 1 : 0;
        else if (t.dataset.slv) rec.skill_array[+t.dataset.slv].level = num(t.value);
        else if (t.dataset.fstar) {
          const [where, i] = t.dataset.fstar.split(":");
          const arr = sparkArr(where);
          const x = fac(arr[+i].factor_id);
          const to = factors.find((y) => y.group === x.group && y.stars === +t.value);
          if (to) arr[+i].factor_id = to.id;
        } else if (t.matches("[data-auto]")) { autoRank = t.checked; draw(); }
      });
      m.box.addEventListener("click", async (e) => {
        const t = e.target.closest("button");
        if (!t) return;
        const d = t.dataset;
        if (d.srm !== undefined) { rec.skill_array.splice(+d.srm, 1); draw(); }
        else if (d.pg) { closed.has(d.pg) ? closed.delete(d.pg) : closed.add(d.pg); t.parentElement.classList.toggle("closed"); }
        else if (d.fset) {
          const [where, i, st] = d.fset.split(":");
          const arr = sparkArr(where);
          const to = factors.find((y) => y.group === fac(arr[+i].factor_id).group && y.stars === +st);
          if (to) { arr[+i].factor_id = to.id; draw(); }
        } else if (d.fclear) { sparkArr(d.fclear).length = 0; draw(); }
        else if (d.fdedupe) {
          const arr = sparkArr(d.fdedupe), best = new Map();
          for (const fi of arr) { const x = fac(fi.factor_id); if (!best.has(x.group) || x.stars > fac(best.get(x.group).factor_id).stars) best.set(x.group, fi); }
          arr.splice(0, arr.length, ...arr.filter((fi) => best.get(fac(fi.factor_id).group) === fi));
          draw();
        } else if (d.frm) { const [where, i] = d.frm.split(":"); sparkArr(where).splice(+i, 1); draw(); }
        else if (d.allstat) { for (const [k] of STATS) rec[k] = +d.allstat; draw(); }
        else if (d.allapt) { for (const k of APT_FIELDS) rec[k] = +d.allapt; draw(); }
        else if (d.skbrowse !== undefined) browseSkills();
        else if (d.fbrowse) browseSparks(d.fbrowse);
        else if (d.clearskills !== undefined) { rec.skill_array = []; draw(); }
        else if (d.punlink) { rec[`succession_trained_chara_id_${d.punlink}`] = 0; draw(); }
        else if (d.pedit) editOne(+d.pedit, async () => { await load(); draw(); });
        else if (d.applyraw !== undefined) {
          try { rec = JSON.parse(m.box.querySelector("[data-raw]").value); rec.skill_array ||= []; rec.factor_info_array ||= []; rec.succession_chara_array ||= []; draw(); }
          catch (ex) { reportError("Not valid JSON", ex); }
        } else if (d.x === "close") m.close();
        else if (d.x === "save") {
          if (await act(t, "Save veteran", () => call("legacy.save", { id, record: rec, auto_rank: autoRank }), (r) => `Saved · rank ${r.rank_label}`)) { m.close(); onSaved ? onSaved() : load(); }
        } else if (d.x === "clone") {
          const r = await act(t, "Clone veteran", () => call("legacy.clone", { id }), (r) => `Cloned as #${r.id}`);
          if (r) { m.close(); await load(); editOne(r.id); }
        } else if (d.x === "rm") {
          if (!(await ask(win, { title: "Delete this veteran?", danger: true, ok: "Delete" }))) return;
          if (await act(t, "Delete veteran", () => call("legacy.apply", { ids: [id], action: "delete" }), "Deleted")) { m.close(); load(); }
        }
      });
      draw();
    }

    body.addEventListener("click", async (e) => {
      if (e.target.closest(".modal")) return;
      const row = e.target.closest(".vrow");
      if (row) {
        const id = +row.dataset.id;
        if (e.target.closest("[data-edit]")) return editOne(id);
        if (e.target.closest("[data-clone]")) {
          const r = await act(null, "Clone veteran", () => call("legacy.clone", { id }), (r) => `Cloned as #${r.id}`);
          if (r) load();
          return;
        }
        if (e.shiftKey && sel.size) {
          const last = [...sel].pop(), a = order.indexOf(last), b = order.indexOf(id);
          for (let i = Math.min(a, b); i <= Math.max(a, b); i++) sel.add(order[i]);
        } else sel.has(id) ? sel.delete(id) : sel.add(id);
        syncSel();
        return;
      }
      const s = e.target.closest("[data-sel]");
      if (s) { s.dataset.sel === "all" ? order.forEach((id) => sel.add(id)) : sel.clear(); syncSel(); return; }
      if (e.target.closest("[data-reset]")) { Object.assign(f, blank()); win.save(); renderFilters(); render(); return; }
      if (e.target.closest("[data-umamoe]")) return openUmaMoe(win, load);
      const d = e.target.closest("[data-do]");
      if (d) run(d.dataset.do, d);
    });
    body.addEventListener("dblclick", (e) => {
      const row = e.target.closest(".vrow");
      if (row && !e.target.closest("button, input, .modal")) editOne(+row.dataset.id);
    });
    top.addEventListener("input", (e) => {
      if (e.target.dataset.apt !== undefined) {
        f.apt[+e.target.dataset.apt] = e.target.value;
        e.target.classList.toggle("set", !!e.target.value);
        win.save(); render(); return;
      }
      const k = e.target.dataset.f;
      if (!k) return;
      f[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      win.save(); render();
    });

    const off = store.on((kind) => {
      if (kind === "target") { data = null; sel.clear(); load(); }
      if (kind === "history") load();
    });
    load();
    return off;
  },
};

