
import { esc, store, ui } from "./core.js";
import { usersWin } from "./users.js";
import { saveWin } from "./save.js";
import { supportsWin } from "./supports.js";
import { umasWin } from "./umas.js";
import { legacyWin } from "./legacy.js";
import { careerWin } from "./career.js";
import { itemsWin } from "./items.js";
import { moreWin } from "./more.js";
import { historyWin } from "./history.js";
import { tuningWin } from "./tuning.js";
import { bannersWin } from "./banners.js";
import { racesWin } from "./races.js";
import { champions } from "./champions.js";
import { syncWin } from "./sync.js";
import { jobsWin } from "./jobs.js";
import { serverWin } from "./server-config.js";
import { pvpWin } from "./pvp.js";
import { setupWin } from "./setup.js";

const errorsWin = {
  title: "Errors", icon: "alert", size: [420, 300], place: [0.55, 0.5, 0.45, 0.5], dock: false,
  mount(body, win) {
    let openId = null;
    const render = () => {
      const list = [...store.errors].reverse();
      body.innerHTML = list.length ? list.map((e) => `<div class="err ${e.id === openId ? "open" : ""}" data-id="${esc(e.id)}">
          <div class="row"><b class="err-title grow">${esc(e.title)}</b><span class="faint">${new Date(e.t * 1000).toLocaleTimeString()}</span></div>
          <div class="err-msg">${esc(e.message)}</div>
          ${e.id === openId && e.detail ? `<pre class="err-detail">${esc(e.detail)}</pre>` : ""}</div>`).join("")
        : `<div class="empty">No errors.</div>`;
    };
    body.addEventListener("click", (e) => {
      const el = e.target.closest(".err");
      if (!el || e.target.closest(".err-detail")) return;
      openId = openId === el.dataset.id ? null : el.dataset.id;
      render();
    });
    win.reveal = (opts) => { if (opts.error) { openId = opts.error; render(); } };
    const off = store.on((k) => { if (k === "errors") render(); });
    render();
    return off;
  },
};

export const GROUPS = ["Account", "Roster", "Game", "Content", "System"];
const g = (group, w, extra = {}) => ({ ...w, group, ...extra });

export const WINDOWS = {
  users: g("Account", usersWin),
  items: g("Account", itemsWin),
  more: g("Account", moreWin),
  save: g("Account", saveWin),
  umas: g("Roster", umasWin),
  legacy: g("Roster", legacyWin),
  supports: g("Roster", supportsWin),
  career: g("Roster", careerWin),
  sync: g("Game", syncWin),
  races: g("Game", racesWin),
  tuning: g("Game", tuningWin),
  banners: g("Game", bannersWin),
  pvp: g("Game", pvpWin, { needs: ["global"] }),
  champions: g("Game", champions, { dock: false, key: null }),
  setup: setupWin,
  server: serverWin,
  jobs: g("System", jobsWin, { worker: false }),
  history: g("System", historyWin),
  errors: { ...errorsWin, worker: false },
};

export function drag(handle, target, onEnd) {
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button, select, input")) return;
    const parent = target.offsetParent;
    const sx = e.clientX - target.offsetLeft, sy = e.clientY - target.offsetTop;
    handle.setPointerCapture(e.pointerId);
    const move = (m) => {
      target.style.left = Math.min(Math.max(m.clientX - sx, 0), parent.clientWidth - 60) + "px";
      target.style.top = Math.min(Math.max(m.clientY - sy, 0), parent.clientHeight - 40) + "px";
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      onEnd?.();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-open]");
  if (b) ui.open(b.dataset.open, b.dataset.openArg ? JSON.parse(b.dataset.openArg) : {});
});
