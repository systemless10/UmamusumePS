
import { REGION_NAME, act, call, dateTime, esc, icon, store, toast } from "./core.js";
import { dot, empty } from "./widgets.js";
import { runJob } from "./jobs.js";

const MODES = {
  hosts: ["DNS modified", "/etc/hosts sends the game's API domain to this PC. The plugin's own redirect stays off."],
  plugin: ["DNS untouched", "The Hachimi plugin redirects to the address below. Nothing system-wide changes."],
  lan: ["Other device", "Serve a phone or another PC over LAN / Tailscale. Use this PC's address as the host."],
};

const PACK_KEYS = ["custom_support", "server_events", "unreleased_trainees"];
const packRow = (cfg) => {
  const on = cfg.filter((c) => PACK_KEYS.includes(c.key)).map((c) => !!c.value);
  const all = on.every(Boolean), mixed = !all && on.some(Boolean);
  return `<label class="row" style="gap:8px;align-items:flex-start"><input type="checkbox" data-cfgpack onchange="this.dataset.touched=1" ${all ? "checked" : ""} ${mixed ? "data-mixed" : ""}>
    <span><b style="font-size:13px">UmaPack</b> <span class="faint" style="font-size:12px">Custom support cards, server events and unreleased trainees from the published pack.${mixed ? " (partly on)" : ""}</span></span></label>`;
};

export const serverWin = {
  title: "Server config", icon: "server", size: [620, 460], place: [0.18, 0.04, 0.64, 0.92], key: "s", group: "System",
  mount(body, win) {
    let d = null, draft = null;
    body.classList.add("flush");

    async function load() {
      try { d = await call("system.get", {}, { region: store.region, viewer: null }); }
      catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      draft = { ...d.profile };
      render();
    }

    function render() {
      const p = draft, reg = d.region, gl = reg === "global";
      const changed = JSON.stringify(p) !== JSON.stringify(d.profile);
      const srv = d.servers.length
        ? d.servers.map((s) => `${dot(true, `pid ${s.pid}`)} <span class="faint">${esc(s.host || "?")}:${esc(s.port || 443)}${s.ios_certs ? " · iOS certs" : ""} · since ${dateTime(s.started)}</span>`).join("<br>")
        : dot(false, "not running");
      const cfg = Object.fromEntries(d.cfg.map((c) => [c.key, c.value]));
      body.innerHTML = `<div class="pane-top">
          <div class="row"><b class="grow">${REGION_NAME[reg]} connection</b>
            <span class="faint">${d.game_running ? dot(null, "game running") : dot(true, "game closed")}</span></div>
          ${d.warnings.length ? `<div class="warnbox">${d.warnings.map((w) => `<span>${icon("alert")} ${esc(w)}</span>`).join("")}</div>` : ""}
        </div>
        <div class="pane-scroll" style="display:grid;gap:14px;align-content:start">
          <div class="modes">${Object.entries(MODES).map(([k, [t, sub]]) =>
            `<button class="mode ${p.mode === k ? "on" : ""}" data-mode="${k}"><b>${esc(t)}</b><span>${esc(sub)}</span></button>`).join("")}</div>
          <div class="ed-sec">
            <div class="ed-row">
              <label>Host<input type="text" class="sm" data-f="host" value="${esc(p.host)}" list="ips-${reg}" style="width:180px"></label>
              <datalist id="ips-${reg}">${["127.0.0.1", "127.0.0.2", ...d.ips].map((ip) => `<option value="${esc(ip)}">`).join("")}</datalist>
              <label>Port<input type="number" class="num" data-f="port" value="${esc(p.port)}" min="1" max="65535"></label>
              ${gl ? `<label class="row" style="gap:6px"><input type="checkbox" data-f="ios_certs" ${p.ios_certs ? "checked" : ""}> iOS cert chain</label>` : ""}
            </div>
            <div class="faint" style="font-size:12px">${p.mode === "hosts"
              ? `Game → <b>${esc(d.domain)}</b> → /etc/hosts → this PC (${esc(p.host)}:${esc(p.port)}).`
              : `Game → plugin → <b>${esc(`https://${p.host}${+p.port === 443 ? "" : ":" + p.port}`)}</b>. /etc/hosts should stay off.`}</div>
            <div class="row" style="flex-wrap:wrap">
              <button class="btn sm" data-a="save" ${changed ? "" : "disabled"}>${icon("save")} Save profile</button>
              <button class="btn primary sm" data-a="apply">${icon("check")} Save + apply to redirect.cfg</button>
            </div>
          </div>
          <div class="ed-sec">
            <div class="kv">
              <span>/etc/hosts</span><span>${dot(d.hosts === "on" ? true : d.hosts === "unknown" ? null : false, `${d.domain}: ${d.hosts}`)}
                <button class="btn sm" data-hosts="${d.hosts === "on" ? "off" : "on"}" style="margin-left:8px">${d.hosts === "on" ? "Turn off" : "Turn on"}</button></span>
              <span>Plugin redirect</span><span>${dot(cfg.enabled, cfg.enabled ? `on → ${cfg.server || "?"}` : "off")}</span>
              <span>Game server</span><span>${srv}</span>
              <span>Start with</span><span><code class="mono" data-cmd>${esc(d.start_command)}</code>
                <button class="btn sm" data-a="copy" style="margin-left:6px">${icon("copy")}</button></span>
            </div>
            <div class="faint" style="font-size:12px">A running server keeps its address until restarted. Restart it from your terminal with the command above.</div>
          </div>
          <div class="ed-sec">
            <b>Plugin settings <span class="faint mono" style="font-weight:400">${esc(d.cfg_path)}</span></b>
            ${d.hachimi ? d.cfg.filter((c) => !PACK_KEYS.includes(c.key) || c.key === PACK_KEYS[0]).map((c) => PACK_KEYS.includes(c.key) ? packRow(d.cfg) : c.key === "server" ? `<div class="ed-row"><label style="flex:1">${esc(c.label)}<span class="faint" style="font-size:11px">${esc(c.help)}</span>
                  <input type="text" class="sm" data-cfg="server" value="${esc(c.value)}" placeholder="https://127.0.0.1" style="width:100%"></label></div>`
              : `<label class="row" style="gap:8px;align-items:flex-start"><input type="checkbox" data-cfg="${c.key}" ${c.value ? "checked" : ""}>
                  <span><b style="font-size:13px">${esc(c.label)}</b> <span class="faint" style="font-size:12px">${esc(c.help)}</span></span></label>`).join("")
              : `<div class="faint">No Hachimi install found in ${esc(d.game_dir)}.</div>`}
            ${d.hachimi ? `<div class="row"><span class="faint grow" style="font-size:12px">Read at game launch: close the game first. A backup goes to redirect.cfg.bak-admin.</span>
              <button class="btn sm" data-a="cfg">${icon("save")} Save plugin settings</button></div>` : ""}
          </div>
        </div>`;
    }

    body.addEventListener("click", async (e) => {
      const m = e.target.closest("[data-mode]");
      if (m) {
        draft.mode = m.dataset.mode;
        if (draft.mode === "lan" && ["127.0.0.1", "127.0.0.2"].includes(draft.host) && d.ips[0]) draft.host = d.ips[0];
        if (draft.mode !== "lan" && !["127.0.0.1", "127.0.0.2"].includes(draft.host)) draft.host = d.region === "jp" ? "127.0.0.2" : "127.0.0.1";
        return render();
      }
      const h = e.target.closest("[data-hosts]");
      if (h) return runJob(h, "hosts", { state: h.dataset.hosts }, { onDone: load });
      const b = e.target.closest("[data-a]");
      if (!b) return;
      const target = { region: store.region, viewer: null };
      if (b.dataset.a === "copy") { navigator.clipboard?.writeText(d.start_command); return toast("Copied"); }
      if (b.dataset.a === "save" || b.dataset.a === "apply") {
        const r = await act(b, "Server config", () => call("system.set_profile", { ...draft, apply_cfg: b.dataset.a === "apply" }, target),
          b.dataset.a === "apply" ? "Saved and applied. Restart the server and the game." : "Profile saved");
        if (r) { d = r; draft = { ...d.profile }; render(); }
      }
      if (b.dataset.a === "cfg") {
        const values = {};
        for (const i of body.querySelectorAll("[data-cfg]")) values[i.dataset.cfg] = i.type === "checkbox" ? i.checked : i.value;
        const pack = body.querySelector("[data-cfgpack]");
        if (pack && (!pack.hasAttribute("data-mixed") || pack.dataset.touched)) for (const k of PACK_KEYS) values[k] = pack.checked;
        const r = await act(b, "Plugin settings", () => call("system.set_cfg", { values }, target), "Saved. Start the game to load it.");
        if (r) { d = r; draft = { ...d.profile }; render(); }
      }
    });
    body.addEventListener("input", (e) => {
      const f = e.target.dataset?.f;
      if (!f) return;
      draft[f] = e.target.type === "checkbox" ? e.target.checked : f === "port" ? +e.target.value : e.target.value.trim();
      const save = body.querySelector('[data-a="save"]');
      if (save) save.disabled = JSON.stringify(draft) === JSON.stringify(d.profile);
    });

    win.refresh = load;
    const off = store.on((k) => { if (k === "target") load(); });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
