
import { REGION_NAME, color, esc, gameIcon, icon, loadCatalog, loadSetup, loadUsers, prefs, setup, store, toast, ui, unavailable, userName } from "./core.js";
import { GROUPS, WINDOWS, drag } from "./windows.js";

const desktop = document.getElementById("desktop");
const dockApps = document.getElementById("dock-apps");
const dockRegion = document.getElementById("dock-region");
const dockLanguage = document.getElementById("dock-language");
const dockTarget = document.getElementById("dock-target");
const conn = document.getElementById("conn");
const hint = document.getElementById("wallpaper-hint");

const open = new Map();
const layout = prefs.get("layout", {});
let z = 10;
let focused = null;

function saveSession() {
  prefs.set("layout", layout);
  prefs.set("open", [...open.values()].sort((a, b) => a.z - b.z).map((w) => w.kind));
}

function placement(kind) {
  if (layout[kind]) return layout[kind];
  const G = 14;
  const W = desktop.clientWidth - G, H = desktop.clientHeight - G;
  const [fx, fy, fw, fh] = WINDOWS[kind].place;
  const [mw, mh] = WINDOWS[kind].size;
  const w = Math.max(Math.min(mw, W - G), Math.round(W * fw) - G);
  const h = Math.max(Math.min(mh, H - G), Math.round(H * fh) - G);
  return { x: Math.min(G + Math.round(W * fx), Math.max(G, W - w)), y: Math.min(G + Math.round(H * fy), Math.max(G, H - h)), w, h };
}

function focus(win) {
  if (focused === win) return;
  focused?.el.classList.remove("focused");
  focused = win;
  if (win) {
    win.z = ++z;
    win.el.style.zIndex = win.z;
    win.el.classList.add("focused");
  }
  syncDock();
  saveSession();
}

function openWindow(kind, opts = {}) {
  const def = WINDOWS[kind];
  if (!def) return;
  const why = unavailable(def);
  if (why && !open.has(kind)) {
    if (!opts.quiet) toast(`${def.title}: ${why}`);
    return;
  }
  let win = open.get(kind);
  if (win) {
    win.reveal?.(opts);
    focus(win);
    return win;
  }
  const p = placement(kind);
  const el = document.createElement("section");
  el.className = "win" + (p.max ? " max" : "");
  Object.assign(el.style, { left: p.x + "px", top: p.y + "px", width: p.w + "px", height: p.h + "px" });
  el.innerHTML = `<header class="titlebar"><span class="icon">${icon(def.icon)}</span>
      <span class="title">${esc(def.title)}</span><div class="tools"></div>
      <button class="wbtn" data-w="max" title="Maximize">${icon("maximize")}</button>
      <button class="wbtn close" data-w="close" title="Close (Esc)">${icon("close")}</button></header>
    <div class="win-body"></div><div class="resize"></div>`;
  desktop.append(el);

  win = {
    kind, el, z: 0, offs: [],
    body: el.querySelector(".win-body"),
    state: prefs.get("wstate." + kind, {}),
    save() { prefs.set("wstate." + kind, this.state); },
  };
  open.set(kind, win);

  const remember = () => {
    layout[kind] = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, max: el.classList.contains("max") };
    saveSession();
  };
  const bar = el.querySelector(".titlebar");
  drag(bar, el, remember);
  bar.addEventListener("dblclick", (e) => { if (!e.target.closest("button, select")) toggleMax(); });
  const toggleMax = () => { el.classList.toggle("max"); remember(); };
  el.querySelector('[data-w="max"]').onclick = toggleMax;
  el.querySelector('[data-w="close"]').onclick = () => closeWindow(kind);
  el.addEventListener("pointerdown", () => focus(win), true);
  resizer(el.querySelector(".resize"), el, remember);

  const tools = def.tools?.(win) || [];
  el.querySelector(".tools").append(...tools);
  win.cleanup = def.mount(win.body, win);
  win.reveal?.(opts);
  focus(win);
  syncDock();
  return win;
}

async function closeWindow(kind) {
  const win = open.get(kind);
  if (!win) return;
  if (win.beforeClose && !await win.beforeClose()) return;
  win.cleanup?.();
  for (const off of win.offs) off();
  win.el.remove();
  open.delete(kind);
  if (focused === win) {
    focused = null;
    const top = [...open.values()].sort((a, b) => b.z - a.z)[0];
    if (top) focus(top);
  }
  syncDock();
  saveSession();
}

function resizer(handle, el, onEnd) {
  handle.addEventListener("pointerdown", (e) => {
    e.stopPropagation();
    const sx = e.clientX, sy = e.clientY, w = el.offsetWidth, h = el.offsetHeight;
    handle.setPointerCapture(e.pointerId);
    el.classList.remove("max");
    const move = (m) => {
      el.style.width = Math.max(300, Math.min(w + m.clientX - sx, desktop.clientWidth - el.offsetLeft)) + "px";
      el.style.height = Math.max(180, Math.min(h + m.clientY - sy, desktop.clientHeight - el.offsetTop)) + "px";
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      onEnd();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

function toggle(kind) {
  const win = open.get(kind);
  if (win && focused === win) closeWindow(kind);
  else openWindow(kind);
}

function renderDock() {
  const setupMode = setup.info?.setup_mode;
  dockApps.innerHTML = GROUPS.map((grp) => {
    const btns = Object.entries(WINDOWS).filter(([kind, d]) => d.dock !== false && d.group === grp && (!setupMode || kind === "setup")).map(([kind, d]) => {
      const why = unavailable(d);
      return `<button class="dock-btn ${why ? "off" : ""}" data-kind="${kind}" ${why ? 'aria-disabled="true"' : ""}>${icon(d.icon)}<span class="tip">${esc(d.title)}${why ? `<small>${esc(why)}</small>` : d.key ? `<kbd>Alt+${d.key}</kbd>` : ""}</span></button>`;
    }).join("");
    return btns && `<div class="dock-group"><span>${esc(grp)}</span></div>${btns}`;
  }).join("");
  syncDock();
}
dockApps.addEventListener("click", (e) => {
  const b = e.target.closest("[data-kind]");
  if (b) toggle(b.dataset.kind);
});

function syncDock() {
  for (const b of dockApps.querySelectorAll(".dock-btn")) {
    const win = open.get(b.dataset.kind);
    b.classList.toggle("open", !!win);
    b.classList.toggle("focused", !!win && win === focused);
  }
  hint.hidden = open.size > 0;
}

function renderRegion() {
  const usable = setup.info && !setup.info.setup_mode ? setup.info.usable : [];
  dockRegion.innerHTML = ["global", "jp"].filter((r) => usable.includes(r)).map((r) =>
    `<button class="reg-btn ${store.region === r ? "on" : ""}" data-region="${r}" title="Edit ${REGION_NAME[r]} saves">${r === "global" ? "GL" : "JP"}</button>`).join("");
}
dockRegion.addEventListener("click", (e) => {
  const b = e.target.closest("[data-region]");
  if (b) store.setTarget(b.dataset.region);
});

function renderTarget() {
  const u = store.user();
  const lead = u?.leader;
  dockTarget.innerHTML = `<button title=""><div class="avatar sm ${lead ? "has-img" : ""}" style="--c:${color(store.viewer)}">
      ${lead ? gameIcon("chara", lead, "av") : ""}<span class="initial">${esc(userName(u)[0]?.toUpperCase() || "?")}</span></div></button>
    <span class="tip">${esc(userName(u))} · ${esc(store.viewer)} · ${REGION_NAME[store.region]}</span>`;
}
dockTarget.addEventListener("click", () => openWindow("users"));

store.on((kind) => {
  if (kind === "conn") {
    conn.classList.toggle("down", !store.connected);
    conn.title = store.connected ? "Connected" : "Lost the admin panel server";
  }
  if (kind === "target" || kind === "users") { renderRegion(); renderTarget(); }
  if (kind === "target" || kind === "setup") renderDock();
  if (kind === "setup") renderRegion();
});

const keys = Object.fromEntries(Object.entries(WINDOWS).filter(([, d]) => d.key).map(([k, d]) => [d.key, k]));
document.addEventListener("keydown", (e) => {
  if (e.altKey && keys[e.key]) { e.preventDefault(); toggle(keys[e.key]); return; }
  const typing = e.target.closest?.("input, select, textarea");
  if (e.key === "Escape" && focused && !typing && !focused.el.querySelector(".modal")) closeWindow(focused.kind);
});

const languageLabel = "Prefer English names when available (including JP); otherwise use the original name. Changing this refreshes the panel.";
dockLanguage.innerHTML = `<button class="reg-btn ${store.preferEnglish ? "on" : ""}" aria-label="${languageLabel}" aria-pressed="${store.preferEnglish}" title="${languageLabel}">EN</button>`;
dockLanguage.querySelector("button").addEventListener("click", () => {
  prefs.set("preferEnglish", !store.preferEnglish);
  location.reload();
});
ui.open = openWindow;
try { await loadSetup(); } catch {   }
renderDock();
renderRegion();
if (setup.info?.setup_mode) {
  openWindow("setup");
} else {
  renderTarget();
  if (!setup.info || setup.info.usable.includes(store.region)) {
    loadUsers(store.region);
    loadCatalog(store.region).catch(() => {});
  }
  for (const kind of prefs.get("open", null) ?? ["users"]) openWindow(kind, { quiet: true });
}
