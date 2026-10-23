
import { act, ago, esc, icon, reportError, store, toast, ui } from "./core.js";
import { empty } from "./widgets.js";

const listeners = new Set();
let polling = null;

async function api(path, body) {
  const res = await fetch(`/api/_jobs${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.detail = data.detail || "";
    throw err;
  }
  return data.result;
}

export const listJobs = () => api("");

export async function runJob(btn, recipe, params = {}, { region = store.region, onDone, quiet = false } = {}) {
  const job = await act(btn, "Starting job", () => api("/start", { region, recipe, params }));
  if (!job) return null;
  toast(`Started: ${job.title}`);
  if (!quiet) ui.open("jobs", { job: job.id });
  watch(job.id, onDone);
  return job;
}

function watch(id, onDone) {
  const fn = (jobs) => {
    const j = jobs.find((x) => x.id === id);
    if (j && j.state !== "running") {
      listeners.delete(fn);
      toast(`${j.state === "done" ? "Finished" : j.state === "failed" ? "Failed" : "Cancelled"}: ${j.title}`);
      if (j.state === "failed") reportError(j.title, { message: "The job failed; see its log in Jobs.", detail: "" });
      onDone?.(j);
    }
  };
  listeners.add(fn);
  poll();
}

function poll() {
  if (polling) return;
  polling = setInterval(async () => {
    let jobs;
    try { jobs = await listJobs(); } catch { return; }
    for (const fn of [...listeners]) fn(jobs);
    store.emit("jobs", jobs);
    if (!listeners.size && !jobs.some((j) => j.state === "running")) { clearInterval(polling); polling = null; }
  }, 1500);
}

const dur = (j) => {
  const s = Math.round((j.ended || Date.now() / 1000) - j.started);
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
};

export const jobsWin = {
  title: "Jobs", icon: "terminal", size: [620, 400], place: [0.25, 0.3, 0.7, 0.66], key: "j", group: "System",
  mount(body, win) {
    body.classList.add("flush");
    body.innerHTML = `<div class="split"><div class="side"><div class="pane-scroll" data-list></div></div>
      <div class="main" data-main style="padding:12px 16px;gap:8px"></div></div>`;
    const list = body.querySelector("[data-list]"), main = body.querySelector("[data-main]");
    let jobs = [], openId = win.state.open || null, text = "", from = 0, follow = true, timer = null, tailing = false;

    function renderList() {
      list.innerHTML = jobs.length ? jobs.map((j) => `<div class="jobrow ${j.id === openId ? "on" : ""}" data-id="${j.id}">
          <b style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(j.title)}</b>
          <span class="st-${j.state}">${esc(j.state)}</span>
          <span class="faint">${j.region.toUpperCase()} · ${ago(j.started)}</span><span class="faint">${dur(j)}</span></div>`).join("")
        : empty("No jobs yet. Builds and ports started from other windows show up here.");
    }
    function renderMain(j) {
      if (!j) { main.innerHTML = empty("Pick a job."); return; }
      const steps = j.steps.map((s, i) => `<span class="${i < j.step ? "st-done" : i === j.step && j.state === "running" ? "st-running" : i === j.step && j.state === "failed" ? "st-failed" : "faint"}">${i + 1}. ${esc(s)}</span>`).join(" · ");
      main.innerHTML = `<div class="row"><b class="grow">${esc(j.title)}</b><span class="st-${j.state}">${esc(j.state)}</span>
          ${j.state === "running" ? `<button class="btn sm" data-cancel>${icon("close")} Cancel</button>` : ""}</div>
        <div class="faint" style="font-size:12px">${steps}</div>
        <pre class="joblog" data-log></pre>`;
      const log = main.querySelector("[data-log]");
      log.textContent = text;
      log.addEventListener("scroll", () => { follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 30; });
      if (follow) log.scrollTop = log.scrollHeight;
    }
    async function tail() {
      if (!openId) return renderMain(null);
      if (tailing) return;
      tailing = true;
      const id = openId, at = from;
      let j;
      try { j = await api(`/${id}?from=${at}`); } catch { return; } finally { tailing = false; }
      if (id !== openId || at !== from) return tail();
      if (j.text) { text += j.text; from += new TextEncoder().encode(j.text).length; }
      const log = main.querySelector("[data-log]");
      if (log && main.dataset.id === String(openId) && main.dataset.state === j.state) {
        log.textContent = text;
        if (follow) log.scrollTop = log.scrollHeight;
      } else {
        main.dataset.id = openId; main.dataset.state = j.state;
        renderMain(j);
      }
      clearTimeout(timer);
      if (j.state === "running" || j.text) timer = setTimeout(tail, 1200);
    }
    function open(id) {
      openId = id; win.state.open = id; win.save();
      text = ""; from = 0; follow = true; main.dataset.id = "";
      renderList(); tail();
    }
    async function load() {
      try { jobs = await listJobs(); } catch (ex) { list.innerHTML = empty(esc(ex.message)); return; }
      if (openId && !jobs.some((j) => j.id === openId)) openId = null;
      if (!openId && jobs[0]) openId = jobs[0].id;
      renderList();
      if (main.dataset.id !== String(openId)) open(openId);
    }
    list.addEventListener("click", (e) => { const r = e.target.closest("[data-id]"); if (r) open(+r.dataset.id); });
    main.addEventListener("click", async (e) => {
      if (e.target.closest("[data-cancel]")) { await act(e.target.closest("button"), "Cancel job", () => api(`/${openId}/cancel`, {})); load(); }
    });
    win.reveal = (opts) => { if (opts?.job) { load().then(() => open(opts.job)); } };
    win.refresh = load;
    const off = store.on((k, d) => { if (k === "jobs") { jobs = d; renderList(); } });
    const iv = setInterval(() => { if (jobs.some((j) => j.state === "running")) load(); }, 3000);
    load();
    return () => { off(); clearInterval(iv); clearTimeout(timer); };
  },
};
