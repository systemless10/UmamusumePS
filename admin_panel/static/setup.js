
import { REGION_NAME, act, dateTime, esc, icon, loadSetup, setup, setupPost, toast } from "./core.js";
import { dot, empty } from "./widgets.js";

const FEATURES = [
  ["Save editing (users, umas, cards, career…)", (s, r) => s.usable.includes(r), "per region: needs that region's master.mdb"],
  ["PvP: Champions Meeting, League of Heroes", (s) => s.usable.includes("global"), "Global master.mdb"],
  ["Plugin settings, game-running checks", (s, r) => s.game_install && s.regions[r].game, "per region: that game install"],
];

export const setupWin = {
  title: "Setup", icon: "settings", size: [640, 480], place: [0.15, 0.03, 0.7, 0.94], key: "x", group: "System", worker: false,
  mount(body, win) {
    let d = null, draft = null;
    body.classList.add("flush");

    async function load() {
      try { d = await loadSetup(); } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      draft = JSON.parse(JSON.stringify(d.settings));
      render();
    }

    const changed = () => JSON.stringify(draft) !== JSON.stringify(d.settings);

    function pathRow(r, kind, label, eff, check, auto) {
      const val = draft.regions[r][kind];
      const status = check?.ok ? dot(true, kind === "master"
          ? `${REGION_NAME[check.region]} master · ${check.tables} tables · ${dateTime(check.modified)}`
          : `found${check.hachimi ? " · Hachimi installed" : " · no Hachimi"}`)
        : dot(false, check?.error || "not found");
      return `<label style="display:grid;gap:4px">${esc(label)}
          <div class="row"><input type="text" class="sm grow mono" data-r="${r}" data-k="${kind}" value="${esc(val)}"
            placeholder="${esc(eff && auto ? `auto: ${eff}` : "auto-detect")}" spellcheck="false">
            <button class="btn sm" data-check="${r}:${kind}" title="Check this path">${icon("check")}</button></div>
          <span class="faint" style="font-size:12px" data-status="${r}:${kind}">${val || eff ? status : dot(false, "nothing detected: enter a path")}${!val && eff ? " (auto-detected)" : ""}</span></label>`;
    }

    function regionCard(r) {
      const e = d.regions[r], on = draft.regions[r].enabled;
      return `<div class="ed-sec" style="${on ? "" : "opacity:.6"}">
        <div class="row"><b class="grow">${REGION_NAME[r]}</b>
          ${e.usable ? dot(true, "usable") : on ? dot(false, "no valid master.mdb") : dot(null, "off")}
          <label class="row" style="gap:6px"><input type="checkbox" data-en="${r}" ${on ? "checked" : ""}> on</label></div>
        ${draftGame() ? pathRow(r, "game_dir", "Game install folder", e.game_dir, e.game_check, e.game_auto) : ""}
        ${pathRow(r, "master", "master.mdb", e.master, e.master_check, e.master_auto)}
        ${draftGame() ? `<span class="faint" style="font-size:12px">Leave master.mdb empty to use the one in the game folder${r === "global" ? " (or the repo's master.mdb)" : ""}.</span>` : ""}
      </div>`;
    }
    const draftGame = () => draft.game_install;

    function render() {
      const s = d;
      body.innerHTML = `<div class="pane-top">
          ${s.setup_mode ? `<div class="notebox"><b>Setup mode.</b> Region workers are stopped until you finish.
            Pick where each region's master.mdb is (and the game installs, if you use them), save, then finish.</div>` : ""}
          ${!s.saved ? `<div class="faint" style="font-size:12px">Nothing saved yet: everything below is auto-detected.</div>` : ""}
        </div>
        <div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
          <div class="ed-sec">
            <label class="row" style="gap:8px;align-items:flex-start"><input type="checkbox" data-game ${draft.game_install ? "checked" : ""}>
              <span><b>Use the game installs</b><br><span class="faint" style="font-size:12px">Off: the panel runs from master.mdb files alone
                (copy them anywhere). Plugin settings and game-running checks are turned off.</span></span></label>
          </div>
          ${regionCard("global")}
          ${regionCard("jp")}
          <div class="ed-sec"><b>Features with this setup</b>
            ${FEATURES.map(([name, ok, need]) => {
              const per = need.startsWith("per region");
              const state = per ? ["global", "jp"].map((r) => `${r === "global" ? "GL" : "JP"} ${ok(s, r) ? "✓" : "–"}`).join(" · ") : null;
              const any = per ? ["global", "jp"].some((r) => ok(s, r)) : ok(s);
              return `<div class="row"><span class="grow">${dot(any ? true : false, name)}</span>
                <span class="faint" style="font-size:12px">${per ? state + " · " : ""}${esc(per ? need.replace("per region: ", "") : need)}</span></div>`;
            }).join("")}
            <span class="faint" style="font-size:12px">Reflects the saved setup. Save to update it.</span>
          </div>
          <p class="faint" style="font-size:12px">Saved in <span class="mono">${esc(s.file)}</span>. The game server (start-server.sh) keeps its own paths.</p>
        </div>
        <div class="pane-top" style="border-top:1px solid var(--border-soft)">
          <div class="row"><span class="grow faint" style="font-size:12px">${changed() ? "Unsaved changes." : "Saved."}</span>
            <button class="btn sm" data-a="save" ${changed() ? "" : "disabled"}>${icon("save")} Save and restart workers</button>
            ${s.setup_mode ? `<button class="btn primary sm" data-a="finish" ${s.usable.length && !changed() ? "" : "disabled"}
              title="${s.usable.length ? "" : "Needs a region with a valid master.mdb"}">${icon("check")} Finish setup</button>` : ""}</div>
        </div>`;
    }

    body.addEventListener("input", (e) => {
      const t = e.target;
      if (t.dataset.k) draft.regions[t.dataset.r][t.dataset.k] = t.value.trim();
      const save = body.querySelector('[data-a="save"]');
      if (save) save.disabled = !changed();
    });
    body.addEventListener("change", (e) => {
      const t = e.target;
      if (t.dataset.en) draft.regions[t.dataset.en].enabled = t.checked;
      else if ("game" in t.dataset) draft.game_install = t.checked;
      else return;
      render();
    });
    body.addEventListener("click", async (e) => {
      const c = e.target.closest("[data-check]");
      if (c) {
        const [r, kind] = c.dataset.check.split(":");
        const path = draft.regions[r][kind] || (kind === "master" ? d.regions[r].master : d.regions[r].game_dir);
        const out = body.querySelector(`[data-status="${c.dataset.check}"]`);
        const res = await act(c, "Check path", () => setupPost("/check", { kind, region: r, path }));
        if (res && out) out.innerHTML = res.ok
          ? dot(true, kind === "master" ? `${REGION_NAME[res.region]} master · ${res.tables} tables · “${res.sample}”`
            : `found${res.master ? " · has master.mdb" : " · no master.mdb inside"}${res.hachimi ? " · Hachimi installed" : ""}`)
          : dot(false, res.error);
        return;
      }
      const b = e.target.closest("[data-a]");
      if (!b) return;
      if (b.dataset.a === "save") {
        const r = await act(b, "Setup", () => setupPost("", { settings: draft }), "Saved. Workers restart on next use.");
        if (r) { await load(); }
      }
      if (b.dataset.a === "finish") {
        const r = await act(b, "Setup", () => setupPost("/finish", {}));
        if (r) { toast("Setup finished"); location.reload(); }
      }
    });

    win.refresh = load;
    win.beforeClose = async () => !setup.info?.setup_mode || (toast("Finish setup first (or quit the panel)."), false);
    body.innerHTML = empty("Loading…");
    load();
  },
};
