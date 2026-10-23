
import { act, call, callFull, esc, gameIcon, icon, keepScroll, loadCatalog, loadExtra, store, toast, ui } from "./core.js";
import { APT_FIELDS, APT_LABELS, MOODS, SKILL_FILTERS, STATS, STYLES, ask, empty, gradeOptions, openBrowser, opts, picker, skillTile, skillRow, skillSearch, targetPicker }
  from "./widgets.js";

export const careerWin = {
  title: "Career", icon: "flag", size: [760, 540], place: [0.06, 0.02, 0.92, 0.96], key: "7",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    let rawKey = null, rawQ = "", rawText = "", dirty = false, liveNote = "";
    let s = null, cat = null, skills = null, programs = null, lastLog = "", edits = {};
    win.state.turnOpts ??= { fill_races: true, with_rewards: false, rank: 1, keep_races: false, keep_events: false, reset_fired: false, mark_past: true };
    const to = win.state.turnOpts;

    async function load() {
      try {
        [cat, skills, programs] = await Promise.all([loadCatalog(), loadExtra("skills"), loadExtra("programs")]);
        s = await call("career.state");
      } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      edits = {}; dirty = false; liveNote = ""; rawText = "";
      if (s.active && ![s.state_key, ...s.scenario_keys].includes(rawKey)) rawKey = s.state_key;
      render();
    }

    async function cmd(name, opts = {}, btn, okText) {
      const r = await act(btn, `Career: ${name}`, () => callFull("career.cmd", { cmd: name, opts }));
      if (r) { lastLog = r.log.trim(); if (okText) toast(okText); load(); }
    }
    async function patch(fields, btn, extra = {}) {
      const r = await act(btn, "Career edit", () => call("career.patch", { fields, ...extra }), "Career updated");
      if (r) { lastLog = (r.notes || []).join("\n"); load(); }
    }

    const CMD = { 101: "Speed training", 105: "Stamina training", 102: "Power training", 103: "Guts training", 106: "Wit training",
      601: "Speed training (camp)", 602: "Power training (camp)", 603: "Guts training (camp)", 605: "Stamina training (camp)", 606: "Wit training (camp)",
      301: "Outing", 304: "Summer outing", 390: "Scenario outing", 401: "Race", 701: "Rest", 801: "Infirmary" };
    const cmdName = (id) => CMD[id] || (id >= 100 && id < 200 ? `Training ${id}` : `Command ${id}`);
    const summaryHtml = (ci, st) => `<div class="c-summary" data-summary title="Live — updates as the game plays">
          ${STATS.map(([k, t]) => `<div><span class="faint">${t}</span><b>${ci[k]}</b></div>`).join("")}
          <div><span class="faint">Energy</span><b>${ci.vital}/${ci.max_vital}</b></div>
          <div><span class="faint">Mood</span><b>${esc(MOODS[ci.motivation] || ci.motivation)}</b></div>
          <div><span class="faint">SP</span><b>${ci.skill_point}</b></div>
          <div><span class="faint">Turn</span><b>${ci.turn}</b></div>
          <div><span class="faint">Goals</span><b>${st.goals_cleared}/${st.goals.length}</b></div>
          <span class="c-livedot" title="Live">●</span></div>`;
    const skSection = (title, kind, rows, none, pick, browse, help = "") => `<div class="sk-sec">
        <div class="sk-head"><div class="section">${title} · ${rows.length}</div><span class="grow"></span><span data-${pick}></span>
          <button class="btn sm" data-browse="${browse}">${icon("search")} Browse…</button></div>
        ${help ? `<div class="faint sk-help">${help}</div>` : ""}
        <div class="sk-grid">${rows.map((r, i) => `<div class="sk-tile ${r.sk?.rarity >= 2 ? "r2" : ""} ${r.sk?.rarity >= 3 ? "r3" : ""}">
          ${r.sk ? gameIcon("skill", r.sk.icon, "sm") : `<span class="gicon sm"></span>`}
          <div class="sk-body"><b class="ellipsis" title="${esc(r.sk?.name || r.id)}">${esc(r.sk?.name || r.id)}</b>
            <span class="faint sk-desc">${esc(r.sk?.desc || "")}</span></div>
          <div class="sk-step" title="${kind === "h" ? "Hint level" : "Skill level"}"><button class="wbtn" data-lv="${kind}:${i}:-1">−</button><b>${r.level}</b><button class="wbtn" data-lv="${kind}:${i}:1">+</button></div>
          <button class="wbtn" data-${kind === "h" ? "hrm" : "skrm"}="${i}" title="Remove">${icon("close")}</button></div>`).join("") || `<div class="faint">${none}</div>`}</div></div>`;
    const card = (title, inner, cls = "") => `<div class="c-card ${cls}"><div class="section">${title}</div>${inner}</div>`;
    const parkedHtml = () => s.parked.length ? `<div class="list-scroll">${s.parked.map((p) => `<div class="row">
        ${gameIcon("card", p.card_id, "sm")}<div class="grow"><b>${esc(p.name)}</b><div class="faint">${esc(p.card)} · turn ${p.turn}</div></div>
        <button class="btn sm" data-resume="${esc(p.name)}" ${s.active ? "disabled title='Park the active career first'" : ""}>Resume</button>
        <button class="btn icon-btn danger" data-dropslot="${esc(p.name)}" title="Delete slot">${icon("trash")}</button></div>`).join("")}</div>`
      : `<div class="faint">No parked careers.</div>`;

    function render() {
      if (!s) return;
      if (!s.active) {
        body.innerHTML = `<div class="c-grid">${empty("No career in progress on this account.", "<div class='faint'>Start one in the game, or resume a parked one.</div>")}
          ${card("Parked careers", parkedHtml())}${lastLog ? `<pre class="log-out">${esc(lastLog)}</pre>` : ""}</div>`;
        return;
      }
      const ci = s.chara_info;
      const skillByGroup = new Map();
      for (const x of skills) skillByGroup.set(`${x.group}:${x.rarity}`, skillByGroup.get(`${x.group}:${x.rarity}`) || x);
      const condName = new Map(cat.misc.conditions.map((c) => [c.id, c.name]));
      const capKey = (k) => (k === "wiz" ? "max_wiz" : `max_${k}`);
      const nextGoal = s.goals.find((g) => !g.cleared);
      const tab = win.state.ctab || "overview";
      const pane = (id, inner) => `<div class="cgrid2 c-pane" data-pane="${id}" ${id === tab ? "" : "hidden"}>${inner}</div>`;
      const TABS = [["overview", "Overview"], ["trainee", "Stats & aptitudes"], ["skills", `Skills · ${(ci.skill_array || []).length}`],
        ["supports", "Supports & training"], ["events", "Events"], ["advanced", "Advanced"]];
      const pct = Math.round((ci.turn - 1) / Math.max(1, s.end_turn - 1) * 100);

      keepScroll(body, () => {
        body.innerHTML = `<div class="c-grid">
        <div class="c-status">${gameIcon("card", ci.card_id)}<div class="grow"><b>${esc(s.card)}</b>
            <div class="faint">${esc(s.scenario_name)} · ${esc(s.turn_label)} · <span class="${ci.state === 2 ? "neg" : ""}">${esc(s.state_name)}</span></div>
            <div class="c-progress" title="Turn ${ci.turn} of ${s.end_turn}"><span style="width:${pct}%"></span></div></div>
          <div class="row wrap"><button class="btn sm" data-c="reconcile" title="Recompute class, goals and the training screen">${icon("refresh")} Reconcile</button>
            <button class="btn sm" data-c="park" title="Move this career into a save slot">Park…</button>
            <button class="btn sm" data-c="idle_complete" title="Max everything and mark the run complete (finish it in the game)">${icon("bolt")} Complete</button>
            <button class="btn danger sm" data-c="abandon">${icon("trash")} Abandon</button></div></div>
        ${summaryHtml(ci, s)}
        <div class="c-live" data-livenote ${liveNote ? "" : "hidden"}>${icon("refresh")}<span>${esc(liveNote)}</span><button class="btn primary sm" data-livereload>Reload</button></div>
        <div class="faint c-note">${icon("refresh")} The game caches the career — after saving a change here, reload it in the game (title screen → Continue) before acting.</div>
        ${lastLog ? `<pre class="log-out">${esc(lastLog)}</pre>` : ""}
        <div class="tabs" style="margin:0">${TABS.map(([k, t]) => `<button data-ctab="${k}" class="${k === tab ? "on" : ""}">${t}</button>`).join("")}</div>

        ${pane("overview", `
        ${card("Jump to turn", `<div class="ed-row"><label>Turn<input type="number" class="num" data-turn min="1" max="${s.end_turn}" value="${ci.turn}"></label>
            <input type="range" class="single" data-turnr min="1" max="${s.end_turn}" value="${ci.turn}" style="flex:1;--p:${(ci.turn - 1) / (s.end_turn - 1) * 100}%">
            <span class="faint">/ ${s.end_turn}</span></div>
          <div class="faint">Next mandatory races: ${s.forced_turns.slice(0, 6).map((t) => `T${t}`).join(", ") || "none"}</div>
          <details class="c-opts"><summary class="faint">Options</summary><div class="checks">
            <div class="faint c-sub">Moving forward</div>
            <label class="check"><input type="checkbox" data-to="fill_races" ${to.fill_races ? "checked" : ""}> count skipped goal races as run</label>
            <label class="check" style="padding-left:18px"><input type="checkbox" data-to="with_rewards" ${to.with_rewards ? "checked" : ""}> and pay their rewards, finishing
              <input type="number" class="num xs" data-to="rank" value="${to.rank}" min="1" max="18"> place</label>
            <label class="check"><input type="checkbox" data-to="mark_past" ${to.mark_past ? "checked" : ""}> mark skipped story events as seen</label>
            <div class="faint c-sub">Moving back</div>
            <label class="check"><input type="checkbox" data-to="keep_races" ${to.keep_races ? "checked" : ""}> keep races after the new turn</label>
            <div class="faint c-sub">Events</div>
            <label class="check"><input type="checkbox" data-to="keep_events" ${to.keep_events ? "checked" : ""}> keep queued events</label>
            <label class="check"><input type="checkbox" data-to="reset_fired" ${to.reset_fired ? "checked" : ""}> also reset non-turn fired events</label></div></details>
          <div class="row"><button class="btn primary sm" data-c="set_turn">Set turn</button></div>`)}

        ${card(`Goals · ${s.goals_cleared}/${s.goals.length}`, `<div class="list-scroll">${s.goals.map((g) => `<div class="goal ${g.cleared ? "done" : ""} ${g === nextGoal ? "next" : ""}">
            <span class="faint">T${g.turn}</span><span>${esc(g.what)}</span><span>${g.cleared ? `<span class="ok-t">${icon("check")}</span>` : g.due ? `<span class="warn-t">due</span>` : g === nextGoal ? `<span class="faint">next</span>` : ""}</span></div>`).join("") || `<div class="faint">No route goals.</div>`}</div>`)}

        ${card(`Race history · ${s.races.length}`, `<div class="list-scroll">${s.races.map((r) => `<div class="race-row"><span class="faint">T${r.turn}</span>
            <span class="ellipsis">${esc(r.name)}</span><span>${r.result_rank === 1 ? `<span class="badge is-win sm">1st</span>` : `#${r.result_rank}`}</span>
            <button class="wbtn" data-racedel="${r.turn}" title="Remove (refund fans)">${icon("close")}</button></div>`).join("") || `<div class="faint">No races yet.</div>`}</div>
          <div class="section" style="margin-top:6px">Record a race</div>
          <div class="ed-row"><span data-progpick></span><label>Turn<input type="number" class="num xs" data-rt value="${ci.turn}"></label>
            <label>Place<input type="number" class="num xs" data-rr value="1" min="1"></label>
            <label class="check"><input type="checkbox" data-rw> pay rewards</label></div>
          <div class="faint" data-progsel>Search for a race above.</div>`)}`)}

        ${pane("trainee", `
        ${card("Stats", `<div class="stat-edit">${STATS.map(([k, t]) => `<label>${t}<input type="number" class="num" data-f="${k}" value="${ci[k]}"></label>`).join("")}</div>
          <div class="stat-edit">${STATS.map(([k, t]) => `<label class="faint">${t} cap<input type="number" class="num" data-f="${capKey(k)}" value="${ci[capKey(k)]}"></label>`).join("")}</div>
          <div class="ed-row"><label>Skill points<input type="number" class="num wide" data-f="skill_point" value="${ci.skill_point}"></label>
            <label>Fans<input type="number" class="num wide" data-f="fans" value="${ci.fans}"></label>
            <label>Energy<input type="number" class="num xs" data-f="vital" value="${ci.vital}"></label>
            <label>Max energy<input type="number" class="num xs" data-f="max_vital" value="${ci.max_vital}"></label>
            <label>Class<input type="number" class="num xs" data-f="chara_grade" value="${ci.chara_grade}"></label></div>
          <div class="ed-row"><label>Mood<span class="seg mood-seg">${Object.entries(MOODS).map(([v, t]) => `<button data-mood="${v}" class="${+v === ci.motivation ? "on" : ""}">${t}</button>`).join("")}</span></label>
            <label>Race style<span class="seg mood-seg">${Object.entries(STYLES).map(([v, t]) => `<button data-style="${v}" class="${+v === ci.race_running_style ? "on" : ""}">${t}</button>`).join("")}</span></label></div>
          <div class="row"><button class="btn primary sm" data-save="stats">${icon("save")} Save stats</button>
            <button class="btn sm" data-quick="max" title="Fills the fields; press Save to apply">Fill max</button><label class="check"><input type="checkbox" data-regrade> recompute class on save</label></div>`)}

        ${card("Aptitudes", `<div class="apt-grid">${APT_FIELDS.map((k, i) => `<label>${APT_LABELS[i]}<select data-f="${k}">${gradeOptions(ci[k])}</select></label>`).join("")}</div>
          <div class="row"><button class="btn primary sm" data-save="apt">${icon("save")} Save aptitudes</button><button class="btn sm" data-quick="apt" title="Fills the fields; press Save to apply">Fill all S</button></div>`)}

        ${card("Conditions", `<div class="skill-chips">${(ci.chara_effect_id_array || []).map((id, i) =>
            `<span class="skill-chip">${esc(condName.get(id) || `#${id}`)}<button class="wbtn" data-cdrm="${i}">${icon("close")}</button></span>`).join("") || `<span class="faint">No conditions.</span>`}</div>
          <div class="row wrap"><select class="sm" data-cdadd style="flex:1;min-width:0;max-width:100%">${opts([["", "Add a condition…"], ...cat.misc.conditions.map((c) => [c.id, `${c.name} · ${c.id}`])], "")}</select>
            <button class="btn primary sm" data-save="conditions">${icon("save")} Save conditions</button></div>`)}`)}

        ${pane("skills", `<div class="c-card wide sk-card">
          ${skSection("Learned skills", "sk", (ci.skill_array || []).map((x) => ({ sk: skills.byId.get(x.skill_id), id: x.skill_id, level: x.level })), "No skills learned yet.", "skpick", "skills")}
          ${skSection("Hints", "h", (ci.skill_tips_array || []).map((h) => ({ sk: skillByGroup.get(`${h.group_id}:${h.rarity}`), id: `group ${h.group_id}`, level: h.level })), "No hints.", "hpick", "hints",
            "Discounted skills; hint level 1–5 sets the discount.")}
          <div class="sk-save"><button class="btn primary sm" data-save="skills">${icon("save")} Save skills &amp; hints</button>
            <span class="faint">${edits.skills ? "Unsaved changes" : ""}</span></div></div>`)}

        ${pane("supports", `
        ${card("Bonds", `<div class="list-scroll">${s.bonds.map((b, i) => `<div class="bond-row"><span class="ellipsis" title="${esc(b.name)}">${b.support_card_id ? gameIcon("support", b.support_card_id, "xs") : ""} ${esc(b.name)}</span>
            <input type="range" class="single" data-bondr="${i}" min="0" max="100" value="${b.evaluation}" style="--p:${b.evaluation}%">
            <input type="number" class="num xs" data-bond="${i}" min="0" max="100" value="${b.evaluation}"></div>`).join("")}</div>
          <div class="row"><button class="btn primary sm" data-save="bonds">${icon("save")} Save bonds</button><button class="btn sm" data-quick="bonds">Fill all 100</button></div>`)}

        ${card("Training screen", `<div class="faint">What the turn menu offers right now. Click a command to lock or unlock it — lasts until the next turn.</div>
          <div class="cmd-list">${s.commands.map((c) => `<button class="cmd-row ${c.enabled ? "on" : "off"}" data-cmdtog="${c.id}" title="Command ${c.id} · click to ${c.enabled ? "lock" : "unlock"}">
            <span class="sdot ${c.enabled ? "ok" : "bad"}"></span><span class="grow">${esc(cmdName(c.id))}</span>
            ${c.level ? `<span class="faint">Lv${c.level}</span>` : ""}<span class="faint mono">${c.id}</span></button>`).join("") || `<span class="faint">No commands — run Reconcile.</span>`}</div>
          <div class="kv"><span>Races</span><span>${s.race_entry_restriction ? "restricted (goal race only)" : "any race allowed"}</span></div>
          <div class="row"><button class="btn sm" data-c="unlock">Unlock all</button><button class="btn sm" data-c="lock">Lock all</button></div>`)}`)}

        ${pane("events", `
        ${card("Current events", `<div class="kv"><span>Active</span><span>${s.events.active ? `<b>${esc(s.events.active.title)}</b> <span class="faint">${esc(s.events.active.source || "")} · ${s.events.active.event_id} / ${s.events.active.story_id}</span>` : "none"}</span>
            <span title="Transient server keys mid-event">Pending data</span><span class="faint">${s.in_flight.length ? `${s.in_flight.length} keys` : "nothing"}</span></div>
          <div class="section" style="margin-top:6px">Queue · ${s.events.queue.length}</div>
          <div class="list-scroll">${s.events.queue.map((e) => `<div class="row"><span class="grow">${esc(e.title)} <span class="faint">${esc(e.source || "")} · ${e.event_id} / ${e.story_id}</span></span>
            <button class="wbtn" data-evdrop="${e.event_id}" title="Drop">${icon("close")}</button></div>`).join("") || `<div class="faint">Queue empty.</div>`}</div>
          <div class="row"><button class="btn sm" data-c="event_drop_all">Drop queue + active</button></div>
          <div class="section" style="margin-top:6px">Queue an event</div>
          <div class="ed-row"><label>Event<input type="number" class="num" data-qe></label><label>Story<input type="number" class="num wide" data-qs></label>
            <label>Choices<input type="number" class="num xs" data-qc value="1"></label><button class="btn sm" data-c="event_queue">Queue</button></div>`)}
        ${card(`Already seen · ${s.events.fired.length}`, `<div class="faint">Events the server won't show again. Un-fire to let them play again.</div>
          <div class="row"><label class="check">from turn <input type="number" class="num xs" data-uft value="${ci.turn}"> onward</label><button class="btn sm" data-c="event_undo_turn">Un-fire</button></div>
          <div class="list-scroll">${s.events.fired.map((x) => `<div class="row"><span class="grow ellipsis" title="${esc(x.key)}">${esc(x.title)} <span class="faint mono">${esc(x.key)}</span></span><span class="faint">${x.turn != null ? `T${x.turn}` : ""}</span>
              <button class="wbtn" data-unfire="${esc(x.key)}" title="Un-fire">${icon("undo")}</button></div>`).join("") || `<div class="faint">Nothing yet.</div>`}</div>`)}`)}

        ${pane("advanced", `
        ${card("Raw data", `<div class="seg wrap">${[s.state_key, ...s.scenario_keys].map((k) => `<button data-rawview="${esc(k)}" class="${k === rawKey ? "on" : ""}">${esc(k)}</button>`).join("")}</div>
          <div class="row"><input type="text" class="sm grow" data-rawq placeholder="Filter lines…" value="${esc(rawQ)}"><button class="btn sm" data-rawcopy>Copy</button><button class="btn sm" data-rawkey="${esc(rawKey)}" title="Open in the Save editor">Edit in Save…</button></div>
          <pre class="raw-view mono" data-rawout>Loading…</pre>
          <div class="section" style="margin-top:6px">Set a value by path</div>
          <div class="ed-row"><input type="text" class="sm mono" data-pp placeholder="chara_info.short_cut_state" style="flex:1;min-width:180px">
            <input type="text" class="sm mono" data-pv placeholder="JSON value" style="width:140px"></div>
          <div class="row"><label class="check"><input type="checkbox" data-pc> create if missing</label><label class="check"><input type="checkbox" data-pf> path is from whole viewer state</label>
            <button class="btn sm" data-c="data_patch">Set path</button></div>`)}
        ${card(`Parked careers · ${s.parked.length}`, parkedHtml())}`)}
        </div>`;
      });

      drawRaw();
      body.querySelector("[data-skpick]").replaceWith(picker({ placeholder: "Quick add skill…", width: 220, search: skillSearch(skills), row: skillRow,
        pick: (x) => { const arr = ci.skill_array ||= []; if (!arr.some((y) => y.skill_id === x.id)) arr.push({ skill_id: x.id, level: x.rarity >= 3 ? 1 : 1 }); edits.skills = true; render(); } }));
      body.querySelector("[data-hpick]").replaceWith(picker({ placeholder: "Quick add hint…", width: 220, search: skillSearch(skills), row: skillRow,
        pick: (x) => {
          const arr = ci.skill_tips_array ||= [];
          const h = arr.find((y) => y.group_id === x.group && y.rarity === x.rarity);
          if (h) h.level = Math.min(5, h.level + 1); else arr.push({ group_id: x.group, rarity: x.rarity, level: 1 });
          edits.skills = true; render();
        } }));
      body.querySelector("[data-progpick]").replaceWith(picker({ placeholder: "Add race…", width: 180,
        search: (q) => programs.filter((p) => p.name.toLowerCase().includes(q) || String(p.id) === q),
        row: (p) => `<span class="badge sm sc-${esc(p.grade)}">${esc(p.grade)}</span><span>${esc(p.name)}</span><span class="faint">#${p.id} · month ${p.month}${p.half === 1 ? " early" : " late"}</span>`,
        pick: (p) => { body.querySelector("[data-progsel]").innerHTML = `${esc(p.name)} <button class="btn primary sm" data-c="race_add" data-prog="${p.id}">Add race</button>`; } }));
    }

    async function drawRaw(fetch = !rawText) {
      const out = body.querySelector("[data-rawout]");
      if (!out) return;
      if (fetch) { try { rawText = JSON.stringify(await call("career.raw", { key: rawKey }), null, 2); } catch (ex) { rawText = ex.message; } }
      const q = rawQ.trim().toLowerCase();
      out.textContent = q ? rawText.split("\n").filter((l) => l.toLowerCase().includes(q)).join("\n") : rawText;
    }

    const LIVE = ["speed", "stamina", "power", "guts", "wiz", "vital", "max_vital", "skill_point", "motivation", "turn", "fans"];
    const sig = (st) => st?.active ? LIVE.map((k) => st.chara_info[k]).join(",") : "none";
    let polling = false;
    const timer = setInterval(async () => {
      if (polling || !s || document.hidden || !body.isConnected) return;
      polling = true;
      try {
        const n = await call("career.state");
        if (sig(n) === sig(s)) return;
        const ae = document.activeElement;
        if (!dirty && body.contains(ae) && ae.matches("input,select")) return;
        if (!dirty) { s = n; edits = {}; liveNote = ""; rawText = ""; render(); return; }
        liveNote = "The game changed this career while you were editing. Your unsaved edits would overwrite the new stats — Reload to pick them up.";
        const sum = body.querySelector("[data-summary]");
        if (sum && n.active) sum.outerHTML = summaryHtml(n.chara_info, n);
        const ln = body.querySelector("[data-livenote]"); if (ln) { ln.hidden = false; ln.querySelector("span").textContent = liveNote; }
        s.__fresh = n;
      } catch {   } finally { polling = false; }
    }, 2500);

    const val = (sel) => body.querySelector(sel)?.value;
    body.addEventListener("input", (e) => {
      const t = e.target;
      if (t.matches("[data-rawq]")) { rawQ = t.value; drawRaw(false); return; }
      if (!s?.active) return;
      if (!t.matches("[data-turn],[data-turnr],[data-to],[data-uft],[data-rt],[data-rr],[data-qe],[data-qs],[data-qc],[data-pp],[data-pv]")) dirty = true;
      const ci = s.chara_info;
      if (t.matches("[data-turnr]")) { body.querySelector("[data-turn]").value = t.value; t.style.setProperty("--p", `${(t.value - 1) / (s.end_turn - 1) * 100}%`); }
      else if (t.matches("[data-turn]")) { const r = body.querySelector("[data-turnr]"); r.value = t.value; }
      else if (t.dataset.f) { ci[t.dataset.f] = +t.value; }
      else if (t.dataset.to !== undefined) { to[t.dataset.to] = t.type === "checkbox" ? t.checked : +t.value; win.save(); }
      else if (t.dataset.sklv !== undefined) { ci.skill_array[+t.dataset.sklv].level = +t.value; }
      else if (t.dataset.hlv !== undefined) { ci.skill_tips_array[+t.dataset.hlv].level = +t.value; }
      else if (t.dataset.bondr !== undefined || t.dataset.bond !== undefined) {
        const i = +(t.dataset.bondr ?? t.dataset.bond);
        s.bonds[i].evaluation = +t.value;
        const row = t.closest(".bond-row");
        row.querySelector("[data-bond]").value = t.value;
        const r = row.querySelector("[data-bondr]"); r.value = t.value; r.style.setProperty("--p", `${t.value}%`);
      } else if (t.matches("[data-cdadd]") && t.value) { (ci.chara_effect_id_array ||= []).push(+t.value); render(); }
    });

    body.addEventListener("click", async (e) => {
      const b = e.target.closest("button");
      if (!b || !s) return;
      const d = b.dataset;
      if (d.ctab) {
        win.state.ctab = d.ctab; win.save();
        body.querySelectorAll("[data-ctab]").forEach((x) => x.classList.toggle("on", x === b));
        body.querySelectorAll("[data-pane]").forEach((x) => { x.hidden = x.dataset.pane !== d.ctab; });
        return;
      }
      const ci = s.chara_info;
      if (d.resume) return cmd("resume", { name: d.resume }, b, "Resumed");
      if (d.dropslot) {
        if (await ask(win, { title: `Delete parked career "${d.dropslot}"?`, danger: true, ok: "Delete" })) {
          if (await act(b, "Delete slot", () => call("career.cmd", { cmd: "drop_slot", name: d.dropslot }), "Slot deleted")) load();
        }
        return;
      }
      if (!s.active) return;
      if (d.rawview) { rawKey = d.rawview; rawText = ""; body.querySelectorAll("[data-rawview]").forEach((x) => x.classList.toggle("on", x === b));
        b.closest(".c-card").querySelector("[data-rawkey]").dataset.rawkey = rawKey; return drawRaw(); }
      if (d.rawcopy !== undefined) { navigator.clipboard.writeText(body.querySelector("[data-rawout]").textContent); toast("Copied"); return; }
      if (d.livereload !== undefined) { dirty = false; liveNote = ""; return load(); }
      if (d.lv) {
        const [k, i, step] = d.lv.split(":");
        const arr = k === "h" ? ci.skill_tips_array : ci.skill_array;
        arr[+i].level = Math.max(1, Math.min(k === "h" ? 5 : 6, arr[+i].level + +step));
        edits.skills = true; dirty = true; render(); return;
      }
      if (d.cmdtog) {
        const c = s.commands.find((x) => x.id === +d.cmdtog);
        return cmd("unlock", { command: [c.id], lock: c.enabled }, b, `${cmdName(c.id)} ${c.enabled ? "locked" : "unlocked"}`);
      }
      if (d.browse === "skills") {
        openBrowser(win, { title: "Learned skills", items: skills, filters: SKILL_FILTERS, tile: skillTile, detailed: true,
          isOn: (x) => (ci.skill_array || []).some((y) => y.skill_id === x.id),
          toggle: (x, on) => { ci.skill_array = (ci.skill_array || []).filter((y) => y.skill_id !== x.id); if (on) ci.skill_array.push({ skill_id: x.id, level: 1 }); edits.skills = true; },
          onDone: render });
        return;
      }
      if (d.browse === "hints") {
        const same = (x) => (y) => y.group_id === x.group && y.rarity === x.rarity;
        openBrowser(win, { title: "Hints", items: skills, filters: SKILL_FILTERS.filter(([v]) => v !== "unique"), tile: skillTile, detailed: true,
          isOn: (x) => (ci.skill_tips_array || []).some(same(x)),
          toggle: (x, on) => { ci.skill_tips_array = (ci.skill_tips_array || []).filter((y) => !same(x)(y)); if (on) ci.skill_tips_array.push({ group_id: x.group, rarity: x.rarity, level: 1 }); edits.skills = true; },
          onDone: render });
        return;
      }
      if (d.mood || d.style || d.skrm || d.hrm || d.cdrm || d.quick || d.browse) dirty = true;
      if (d.mood) { ci.motivation = +d.mood; render(); return; }
      if (d.style) { ci.race_running_style = +d.style; render(); return; }
      if (d.skrm !== undefined) { ci.skill_array.splice(+d.skrm, 1); edits.skills = true; render(); return; }
      if (d.hrm !== undefined) { ci.skill_tips_array.splice(+d.hrm, 1); edits.skills = true; render(); return; }
      if (d.cdrm !== undefined) { ci.chara_effect_id_array.splice(+d.cdrm, 1); render(); return; }
      if (d.racedel) return cmd("race_del", { turn: +d.racedel, refund_fans: true }, b, "Race removed");
      if (d.evdrop) return cmd("event_drop", { event_id: +d.evdrop }, b, "Event dropped");
      if (d.unfire) return cmd("event_undo", { keys: [d.unfire] }, b, "Un-fired");
      if (d.rawkey) return ui.open("save", { key: d.rawkey });
      if (d.quick === "max") {
        for (const [k] of STATS) { const cap = k === "wiz" ? "max_wiz" : `max_${k}`; body.querySelector(`[data-f="${cap}"]`).value = 9999; body.querySelector(`[data-f="${k}"]`).value = 9999; }
        body.querySelector('[data-f="skill_point"]').value = 99999;
        return;
      }
      if (d.quick === "apt") { for (const k of APT_FIELDS) body.querySelector(`[data-f="${k}"]`).value = 8; return; }
      if (d.quick === "bonds") { s.bonds.forEach((x) => { x.evaluation = 100; }); render(); return; }
      if (d.save === "stats" && s.__fresh && !(await ask(win, { title: "Overwrite the game's newer stats?", danger: true, ok: "Save anyway",
        text: "The career changed in the game after you started editing. Saving writes the values in this form, which can lower your trainee's stats. Reload to pick up the new values instead." }))) return;
      if (d.save === "stats") {
        const fields = { motivation: ci.motivation, race_running_style: ci.race_running_style };
        for (const k of [...STATS.map(([x]) => x), ...STATS.map(([x]) => (x === "wiz" ? "max_wiz" : `max_${x}`)), "skill_point", "fans", "vital", "max_vital", "chara_grade"])
          fields[k] = +val(`[data-f="${k}"]`);
        return patch(fields, b, { regrade: body.querySelector("[data-regrade]").checked });
      }
      if (d.save === "apt") { const fields = {}; for (const k of APT_FIELDS) fields[k] = +val(`[data-f="${k}"]`); return patch(fields, b); }
      if (d.save === "skills") return patch({ skill_array: ci.skill_array || [], skill_tips_array: ci.skill_tips_array || [] }, b);
      if (d.save === "conditions") return patch({ chara_effect_id_array: ci.chara_effect_id_array || [] }, b);
      if (d.save === "bonds") {
        const byTarget = new Map(s.bonds.map((x) => [x.target_id, x.evaluation]));
        const arr = (ci.evaluation_info_array || []).map((x) => ({ ...x, evaluation: byTarget.get(x.target_id) ?? x.evaluation }));
        return patch({ evaluation_info_array: arr }, b);
      }
      switch (d.c) {
        case "reconcile": return cmd("reconcile", { clear_flight: false }, b, "Reconciled");
        case "set_turn": return cmd("set_turn", { ...to, value: +val("[data-turn]") }, b, "Turn set");
        case "unlock": return cmd("unlock", { all: true }, b, "Commands unlocked");
        case "lock": return cmd("unlock", { all: true, lock: true }, b, "Commands locked");
        case "event_drop_all": return cmd("event_drop", { all: true }, b, "Events dropped");
        case "event_undo_turn": return cmd("event_undo", { turn: +val("[data-uft]") }, b, "Un-fired");
        case "event_queue": return cmd("event_queue", { event_id: +val("[data-qe]"), story: +val("[data-qs]"), choices: +val("[data-qc]") || 1 }, b, "Queued");
        case "race_add": return cmd("race_add", { program_id: +d.prog, turn: +val("[data-rt]"), rank: +val("[data-rr]"),
          with_rewards: body.querySelector("[data-rw]").checked }, b, "Race recorded");
        case "idle_complete":
          if (await ask(win, { title: "Complete this career?", ok: "Complete", text: "Every stat to 9999, max SP and hints, and the run marked finished. Finish it in the game to bank the veteran." }))
            return cmd("idle_complete", {}, b, "Career completed");
          return;
        case "park": {
          const name = await ask(win, { title: "Park this career", text: "Moves it into a save slot; the account then has no career running.", input: "text", placeholder: "slot name", ok: "Park" });
          if (name) return cmd("park", { name: name.trim() }, b, "Career parked");
          return;
        }
        case "abandon":
          if (await ask(win, { title: "Abandon this career?", danger: true, ok: "Abandon", text: "Removes every career key (undo is in History)." })) {
            if (await act(b, "Abandon", () => call("career.cmd", { cmd: "abandon", confirm: true }), "Career removed")) load();
          }
          return;
        case "data_patch": {
          const path = val("[data-pp]").trim();
          if (!path) return;
          if (await act(b, "Set path", () => call("career.data_patch", { path, value: val("[data-pv]"),
            create: body.querySelector("[data-pc]").checked, full_state: body.querySelector("[data-pf]").checked }), "Set")) load();
          return;
        }
        default:
      }
    });

    const off = store.on((kind) => {
      if (kind === "target") { s = null; lastLog = ""; body.innerHTML = empty("Loading…"); load(); }
    });
    body.innerHTML = empty("Loading…");
    load();
    return () => { clearInterval(timer); off?.(); };
  },
};
