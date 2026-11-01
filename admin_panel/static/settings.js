
import { REGION_NAME, act, call, esc, icon, store } from "./core.js";
import { empty } from "./widgets.js";

export const settingsWin = {
  title: "Settings", icon: "settings", size: [520, 320], place: [0.25, 0.1, 0.5, 0.6], key: "g", group: "System",
  mount(body, win) {
    let v = null, up = null, upMsg = "";
    body.classList.add("flush");

    async function load() {
      try {
        v = (await call("system.get", {}, { region: store.region, viewer: null })).versions;
        up = await call("umapack.sync_status", {}, { region: store.region, viewer: null });
      } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      render();
    }

    function render() {
      body.innerHTML = `<div class="pane-top"><b>${REGION_NAME[store.region]} settings</b></div>
        <div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
          <div class="ed-sec">
            <b>Game version</b>
            <div class="ed-row">
              <label>Source<select class="sm" data-v="source">
                <option value="client" ${v.source === "client" ? "selected" : ""}>From the game client</option>
                <option value="custom" ${v.source === "custom" ? "selected" : ""}>Custom values</option></select></label>
              <label>Resource version<input type="text" class="sm mono" data-v="resource_version" value="${esc(v.resource_version)}" style="width:170px"></label>
              <label>App version<input type="text" class="sm mono" data-v="app_version" value="${esc(v.app_version)}" placeholder="not seen yet" style="width:90px"></label>
            </div>
            <div class="row"><span class="faint grow" style="font-size:12px">${v.source === "client"
              ? "Raised automatically to the newest RES-VER / APP-VER any game client sends, so a game update needs no bump. Never lowered."
              : "Always served as typed; client headers are ignored."}</span>
              <button class="btn sm" data-a="ver">${icon("save")} Save</button></div>
          </div>
          ${up?.available ? `<div class="ed-sec">
            <b>Umapacks</b>
            <div class="ed-row">
              <label class="grow">Umapacks folder<input type="text" class="sm mono" data-up="folder" value="${esc(up.folder)}" style="width:100%"></label>
            </div>
            <div class="row"><span class="faint grow" style="font-size:12px">${upMsg ? esc(upMsg)
              : "Adds the trainees and cards the packs in this folder carry. Use the game's hachimi/umapacks, or any folder holding the .umapack files. The folder is checked again whenever the game connects."}</span>
              <button class="btn sm" data-a="sync">${icon("refresh")} Sync umapacks</button></div>
          </div>` : ""}
        </div>`;
    }

    body.addEventListener("click", async (e) => {
      const sync = e.target.closest('[data-a="sync"]');
      if (sync) {
        const folder = body.querySelector('[data-up="folder"]').value.trim();
        const r = await act(sync, "Sync umapacks", () => call("umapack.sync", { folder }, { region: store.region, viewer: null }), "Synced");
        if (r) {
          up.folder = r.folder;
          const added = r.added.map((x) => x.what).join(", ");
          upMsg = (r.added.length ? `Added ${added}. Restart the game to see it.` : `Nothing new in ${r.packs.length} pack${r.packs.length === 1 ? "" : "s"}.`)
            + (r.errors.length ? ` Skipped: ${r.errors.join("; ")}` : "");
          render();
        }
        return;
      }
      const b = e.target.closest('[data-a="ver"]');
      if (!b) return;
      const args = Object.fromEntries([...body.querySelectorAll("[data-v]")].map((i) => [i.dataset.v, i.value.trim()]));
      const r = await act(b, "Game version", () => call("system.set_versions", args, { region: store.region, viewer: null }), "Saved");
      if (r) { v = r.versions; render(); }
    });

    win.refresh = load;
    const off = store.on((k) => { if (k === "target") load(); });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
