
import { act, call, esc, icon, store, toast } from "./core.js";
import { ask, dot, empty, opts } from "./widgets.js";

const BANDS = [["round1", "Round 1"], ["round2_a", "Round 2 · Group A"], ["round2_b", "Round 2 · Group B"],
  ["final_a", "A Finals"], ["final_b", "B Finals"]];
const dur = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${s % 60}s`);

export const champions = {
  title: "Champions Meeting", icon: "medal", size: [560, 420], place: [0.2, 0.08, 0.55, 0.8], key: "m",
  mount(body, win) {
    let data = null, teams = null;
    const target = () => ({ region: store.region, viewer: store.viewer });

    async function load() {
      if (store.region !== "global") { body.innerHTML = empty("Champions Meeting is served on Global only."); return; }
      try { data = await call("champions.status", {}, target()); }
      catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      render();
    }

    function runSection() {
      if (!data.running) {
        return `<div class="ed-sec"><b>Serve a Champions Meeting</b>
          <div class="row" style="flex-wrap:wrap">
            <select class="grow" data-cm>${opts(data.cms.map((c) => [c.id, `CM ${c.id} · ${c.name}`]), data.cms[0]?.id)}</select>
            <label class="faint" title="How long each stage's window lasts">Each stage <input type="number" data-len min="10" max="1440" value="60" style="width:70px"> min</label>
            <label class="faint" title="Held: a stage stays open until you move it on, instead of ending with its window">
              <input type="checkbox" data-hold checked> Wait for me between stages</label>
          </div>
          <div class="row"><button class="btn primary sm" data-start>${icon("play")} Start</button>
            <span class="faint" style="font-size:12px">Restart the game afterwards to load it.</span></div></div>`;
      }
      const run = data.run, cm = data.cms.find((c) => c.id === run.champions_id);
      const idx = data.stages.findIndex(([k]) => k === data.stage);
      const ended = data.stage === "ended";
      const steps = data.stages.filter(([k]) => k !== "ended").map(([k, label], i) => {
        if (i === idx) return `<div class="pvp-step now">${esc(label)}<small>now</small></div>`;
        if (i < idx || ended) return `<div class="pvp-step done">${icon("check")} ${esc(label)}</div>`;
        return `<button class="pvp-step" data-goto="${k}" title="Skip ahead to ${esc(label)}">${esc(label)}<small>skip to</small></button>`;
      }).join("");
      const clock = ended ? "" : run.hold === false
        ? `This stage ends in ${dur(data.stage_ends_in)}, then the next one starts by itself.`
        : `Waiting for you: this stage stays open until you press Next stage.`;
      const next = data.stages[idx + 1]?.[1];
      return `<div class="ed-sec">
        <div class="pvp-head"><b class="grow">CM ${run.champions_id} · ${esc(cm?.name || "")}</b>
          ${ended ? dot(false, "ended") : dot(true, "live")}</div>
        <div class="pvp-steps">${steps}</div>
        <p class="faint" style="font-size:12px;margin:0">${ended ? "This CM is over. Stop it, or start one again to replay." : clock}</p>
        <div class="row" style="margin-top:8px">
          ${ended ? "" : `<button class="btn primary sm" data-advance>${icon("bolt")} ${next === "Ended" ? "End the CM" : "Next stage: " + esc(next)}</button>
          <button class="btn sm" data-hold-toggle title="${run.hold === false ? "Keep each stage open until you move it on" : "Let each stage end on its own timer"}">${icon(run.hold === false ? "pause" : "play")} ${run.hold === false ? "Wait for me" : "Run on timer"}</button>`}
          <span class="grow"></span>
          <button class="btn sm" data-stop>${icon("trash")} Stop CM</button>
        </div>
        <p class="faint" style="font-size:12px;margin:6px 0 0">After a stage change, back out to Home in the game and open Champions Meeting again. Stages only move forward.</p></div>`;
    }

    function viewerSection() {
      const v = data.viewer;
      if (!data.running) return "";
      if (!v) return `<div class="ed-sec"><b>This trainer</b><span class="faint">${store.viewer ? "Hasn't entered this CM yet." : "Pick a trainer in the dock to see their run."}</span></div>`;
      const roundRows = Object.entries(v.rounds || {}).map(([k, r]) => {
        const name = { 1: "Round 1", 2: "Round 2 · Group B", 3: "Round 2 · Group A" }[r.round_id] || `Round ${k}`;
        const n = r.entries.length;
        return `<span>${name}</span><span>best day ${Math.max(0, r.best)} wins · ${n} ${n === 1 ? "day" : "days"} played${r.claimed ? "" : " · <i>rewards unclaimed</i>"}</span>`;
      }).join("");
      const f = v.final;
      const total = f?.total_entry_num > 0 && f.total_entry_num < 1e6 ? ` of ${f.total_entry_num}` : "";
      const fin = f?.rank ? `${{ 4: "B Finals", 5: "A Finals" }[f.round_id] || "Finals"} · placed ${f.rank}${total}` : "not raced yet";
      return `<div class="ed-sec"><b>This trainer</b>
        <div class="pvp-kv"><span>League</span><span>${v.league === 1 ? "Open League" : v.league === 2 ? "Graded League" : "not picked"}</span>
          <span>Qualifier wins</span><span>${v.cum_wins ?? 0}</span>
          ${roundRows}
          <span>Finals</span><span>${fin}</span></div>
        <div class="row"><button class="btn sm" data-reset>${icon("restart")} Reset this trainer's run</button></div></div>`;
    }

    function poolSection() {
      const p = data.pool, o = data.options || {};
      const bandRow = ([k, label]) => `<span>${label}</span>
        <input type="number" class="num" data-band="${k}" data-i="0" min="0" max="100" value="${p.bands[k][0]}"><span class="faint">to</span>
        <input type="number" class="num" data-band="${k}" data-i="1" min="0" max="100" value="${p.bands[k][1]}">
        <button class="btn sm" data-browse="${k}" title="See the teams in this range">${icon("search")}</button>`;
      const snaps = [["auto", "Auto (same CM, else newest)"], ...data.snapshots.map((s) => [s.snapshot, `${s.snapshot.replace(/^cm(\d+)-?/, "CM$1 ").trim()} · ${s.course || ""}`])];
      const pins = (p.pinned || []).length;
      return `<div class="ed-sec"><b>Opponents</b>
        <span class="faint" style="font-size:12px">Each race draws two real Global CM teams whose strength falls in that round's range (0 = weakest, 100 = strongest).</span>
        <div class="row"><span class="grow">Teams from</span><select data-snapshot style="width:auto;max-width:320px;flex:none">${opts(snaps, p.snapshot)}</select></div>
        <div class="pvp-bands">${BANDS.map(bandRow).join("")}</div>
        <div class="row"><span class="grow" title="0 = finals use the band as set; 1 = each qualifier win pushes the finals' weakest opponents up toward the top">Harder finals the more you win <span class="faint">(0–1)</span></span>
          <input type="number" class="num" data-pressure min="0" max="1" step="0.1" value="${p.final_win_pressure}" style="width:64px;flex:none"></div>
        <div class="row"><span class="grow">Opponent mood in qualifiers</span><select data-mood style="width:auto;flex:none">${opts([["random", "Random"], [5, "Great"], [4, "Good"], [3, "Normal"], [2, "Bad"], [1, "Awful"]], p.mood)}</select></div>
        <div class="row"><span class="grow">Pinned teams <span class="faint">(always drawn first; pin from the 🔍 lists)</span></span><span class="faint">${pins || "none"}</span>
          ${pins ? `<button class="btn sm" data-unpin>Clear</button>` : ""}</div>
        <div class="row"><button class="btn primary sm" data-save-pool>${icon("save")} Save opponents</button></div>
        ${teams ? teamList() : ""}</div>
      <div class="ed-sec"><b>Rules</b>
        <label><input type="checkbox" data-opt="unlimited_entries" ${o.unlimited_entries ? "checked" : ""}> Unlimited race entries per day</label>
        <label><input type="checkbox" data-opt="any_team_rank" ${o.any_team_rank ? "checked" : ""}> Allow Graded League without the Team Trials class</label></div>`;
    }

    function teamList() {
      const pinned = new Set(data.pool.pinned || []);
      return `<div style="margin-top:8px"><div class="faint">${teams.count} teams in ${esc(teams.label)} (${esc(teams.snapshot)}), strongest first</div>
        ${teams.teams.map((t) => `<div class="row"><span class="pill">${t.percentile.toFixed(1)}</span>
          <span class="grow">${esc(t.trainer)} · ${t.members.map((m) => esc(m.name || m.card_id)).join(" / ")}</span>
          <button class="btn sm" data-pin="${esc(t.id)}">${pinned.has(t.id) ? icon("check") + " Pinned" : "Pin"}</button></div>`).join("")}</div>`;
    }

    function render() {
      body.innerHTML = `<div style="display:grid;gap:14px;align-content:start">${runSection() + viewerSection() + poolSection()}</div>`;
    }

    function poolArgs() {
      const bands = {};
      body.querySelectorAll("[data-band]").forEach((el) => {
        (bands[el.dataset.band] ??= [0, 100])[+el.dataset.i] = +el.value;
      });
      return { bands, snapshot: body.querySelector("[data-snapshot]").value,
        final_win_pressure: +body.querySelector("[data-pressure]").value,
        mood: body.querySelector("[data-mood]").value };
    }

    body.addEventListener("change", async (e) => {
      const opt = e.target.closest("[data-opt]");
      if (!opt) return;
      const r = await act(opt, "Champions Meeting", () => call("champions.options", { [opt.dataset.opt]: opt.checked }, target()));
      if (r) data.options = r; else opt.checked = !opt.checked;
      opt.disabled = false;
    });

    body.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const T = "Champions Meeting";
      if (btn.matches("[data-start]")) {
        const champions_id = +body.querySelector("[data-cm]").value;
        const stage_len = Math.max(10, +body.querySelector("[data-len]").value || 60) * 60;
        const hold = body.querySelector("[data-hold]").checked;
        await act(btn, T, () => call("champions.start", { champions_id, stage_len, hold }, target()),
          "Published. Restart the game to load it.");
      } else if (btn.matches("[data-advance]")) {
        await act(btn, T, () => call("champions.advance", {}, target()), "Next stage. Back out to Home and in.");
      } else if (btn.dataset.goto) {
        if (!await ask(win, { title: "Skip ahead?", text: "Stages in between end without you. The clock can't go back.", ok: "Go" })) return;
        await act(btn, T, () => call("champions.goto", { stage: btn.dataset.goto }, target()));
      } else if (btn.matches("[data-hold-toggle]")) {
        await act(btn, T, () => call("champions.hold", { hold: data.run.hold === false }, target()));
      } else if (btn.matches("[data-stop]")) {
        if (!await ask(win, { title: "Stop the CM?", text: "It disappears after the next game restart. Time stays where it is until real time catches up.", ok: "Stop", danger: true })) return;
        await act(btn, T, () => call("champions.stop", {}, target()), "Stopped. Restart the game.");
      } else if (btn.matches("[data-reset]")) {
        if (!await ask(win, { title: "Reset this trainer's run?", text: "League, entries, results and the final are forgotten. Rewards already given stay.", ok: "Reset", danger: true })) return;
        await act(btn, T, () => call("champions.reset_viewer", {}, target()));
      } else if (btn.matches("[data-save-pool]")) {
        await act(btn, T, () => call("champions.pool", poolArgs(), target()), "Pool saved.");
      } else if (btn.dataset.browse) {
        const k = btn.dataset.browse, a = poolArgs();
        const [min, max] = a.bands[k];
        Object.assign(data.pool, a);
        try {
          teams = await call("champions.teams", { snapshot: a.snapshot, min, max, limit: 60 }, target());
          teams.label = BANDS.find(([b]) => b === k)[1];
        } catch (ex) { toast(ex.message); return; }
        render();
        return;
      } else if (btn.dataset.pin) {
        const pinned = new Set(data.pool.pinned || []);
        pinned.has(btn.dataset.pin) ? pinned.delete(btn.dataset.pin) : pinned.add(btn.dataset.pin);
        await act(btn, T, () => call("champions.pool", { ...poolArgs(), pinned: [...pinned] }, target()));
      } else if (btn.matches("[data-unpin]")) {
        await act(btn, T, () => call("champions.pool", { ...poolArgs(), pinned: [] }, target()));
      } else return;
      load();
    });

    const off = store.on((k) => { if (k === "target") load(); });
    load();
    return off;
  },
};
