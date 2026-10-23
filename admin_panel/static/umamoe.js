
import { act, call, esc, gameIcon, icon, toast } from "./core.js";
import { aptRow, empty, modal, opts } from "./widgets.js";

const KEY_STORE = "umamoe.apiKey", TARGET_STORE = "umamoe.target";
const read = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
const write = (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {   } };
const SHOW = [["all", "Everything"], ["star", "Star uma"], ["pinned", "Pinned veterans"], ["veteran", "Veterans"], ["team", "Team Trials"]];
const SPARKS = [["blue", "Blue"], ["pink", "Pink"], ["green", "Green"], ["white", "White"]];
const DIST = { 1: "Sprint", 2: "Mile", 3: "Medium", 4: "Long", 5: "Dirt" };

export function openUmaMoe(win, imported) {
  const saved = read(KEY_STORE);
  const m = modal(win, `<div class="veteran-picker umamoe">
      <header><h3>Import from uma.moe</h3><button type="button" class="btn" data-x>Close</button></header>
      <div class="row">
        <label>API key<input type="password" data-key value="${esc(saved)}" placeholder="uma_k_…" autocomplete="off" spellcheck="false"></label>
        <label>Trainer id, profile link or veteran UUID<input type="search" data-target value="${esc(read(TARGET_STORE))}" placeholder="123456789012" spellcheck="false"></label>
      </div>
      <div class="filters"><label><input type="checkbox" data-remember ${saved ? "checked" : ""}> Remember key in this browser</label>
        <span class="grow"></span><button type="button" class="btn primary" data-fetch>${icon("download")} Fetch</button></div>
      <div data-results></div>
    </div>`, { wide: true });
  const $ = (s) => m.box.querySelector(s);
  const out = $("[data-results]");
  const sel = new Set();
  let res = null, args = null, show = "all";

  const credentials = () => {
    const key = $("[data-key]").value.trim(), target = $("[data-target]").value.trim();
    write(KEY_STORE, $("[data-remember]").checked ? key : "");
    write(TARGET_STORE, target);
    return { api_key: key, target };
  };

  async function fetch(btn) {
    args = credentials();
    out.innerHTML = empty("Fetching from uma.moe…");
    const r = await act(btn, "uma.moe", () => call("umamoe.fetch", args));
    if (!r) { out.innerHTML = ""; return; }
    res = r;
    sel.clear();
    for (const e of res.entries) if ((e.star || e.pinned) && !e.problem && e.imported == null) sel.add(e.key);
    render();
  }

  const shown = () => res.entries.filter((e) => show === "all" || (show === "star" ? e.star : show === "pinned" ? e.pinned : e.kind === show));
  function render() {
    if (!res) return;
    const es = shown(), t = res.trainer;
    const pinned = res.entries.filter((e) => e.pinned).length;
    const notes = [res.entries.some((e) => e.kind === "star") && "The star uma's stats, skills and aptitudes aren't published by uma.moe; they're generated from the character (edit them after import).",
      res.entries.some((e) => e.star) && "The star uma's grandparents aren't published; they get placeholders.",
      res.entries.some((e) => e.kind === "team" && !e.star) && "Team Trials umas have no parent data; they get placeholder parents."].filter(Boolean);
    out.innerHTML = `<p class="faint">${esc(t.name || "Trainer")} · ${esc(t.account_id || "")} — ${res.entries.length} uma(s)${pinned ? `, ${pinned} pinned` : ""}.</p>
      ${notes.map((n) => `<p class="faint">${esc(n)}</p>`).join("")}
      <div class="filters"><label>Show <select data-show>${opts(SHOW, show)}</select></label>
        <button class="linkish" data-all>Select shown</button><button class="linkish" data-none>Clear</button></div>
      <div class="vt-list umamoe-list">${es.map(row).join("") || empty(res.entries.length ? "Nothing matches." : "This trainer has no public veterans or Team Trials umas.")}</div>
      <div class="filters"><label><input type="checkbox" data-replace> Replace umas already imported</label><span class="grow"></span>
        <button class="btn primary" data-import ${sel.size ? "" : "disabled"}>${icon("plus")} Import ${sel.size} to roster</button></div>`;
  }

  const row = (e) => `<div class="vrow ${sel.has(e.key) ? "on" : ""} ${e.problem ? "off" : ""}" data-key="${esc(e.key)}">
      <input type="checkbox" data-pick ${sel.has(e.key) ? "checked" : ""} ${e.problem ? "disabled" : ""}>
      ${gameIcon("card", e.card_id)}
      <div class="vn"><b>${e.star ? "★ " : e.pinned ? "📌 " : ""}${esc(e.name)}</b><span class="faint">
        ${[e.star && "Star uma", e.kind === "team" ? `Team Trials${DIST[e.distance] ? ` · ${DIST[e.distance]}` : ""}` : e.kind === "veteran" && "Veteran",
           e.created].filter(Boolean).map(esc).join(" · ")}</span></div>
      <div class="vs"><span><span class="rank-badge">${esc(e.rank_label)}</span> <b>${e.score.toLocaleString()}</b></span>
        <span class="faint">${e.stats ? e.stats.join(" / ") : "stats not published"}</span></div>
      <div class="vk">${e.apt ? aptRow(e.apt) : ""}${e.parents ? `<span class="faint">Parents: ${e.parents.map(esc).join(" × ")}</span>
        <span class="faint">${SPARKS.map(([k, l]) => `${l} ${e.spark_stars[k]}★`).join(" · ")}</span>` : ""}
        ${e.skills != null ? `<span class="faint">${e.skills} skills</span>` : ""}</div>
      <div>${e.problem ? `<span class="warn-t">${esc(e.problem)}</span>` : e.imported != null ? `<span class="ok-t">In roster #${e.imported}</span>` : ""}</div></div>`;

  async function doImport(btn) {
    const keys = [...sel];
    const r = await act(btn, "uma.moe import", () => call("umamoe.import", { ...args, keys, replace: $("[data-replace]").checked }));
    if (!r) return;
    const n = r.added.length + r.replaced.length;
    toast(`uma.moe: ${r.added.length} added${r.replaced.length ? `, ${r.replaced.length} replaced` : ""}${r.skipped.length ? `, ${r.skipped.length} skipped` : ""}`);
    if (r.skipped.length) toast(r.skipped.map((s) => s.reason).filter((v, i, a) => a.indexOf(v) === i).join(" "));
    if (n) imported();
    await fetch(null);
  }

  m.box.addEventListener("click", (e) => {
    e.stopPropagation();
    const t = e.target;
    if (t.closest("[data-x]")) return m.close();
    if (t.closest("[data-fetch]")) return fetch(t.closest("button"));
    if (t.closest("[data-import]")) return doImport(t.closest("button"));
    if (t.closest("[data-all]")) { shown().forEach((x) => !x.problem && sel.add(x.key)); return render(); }
    if (t.closest("[data-none]")) { sel.clear(); return render(); }
    const r = t.closest(".vrow");
    if (r && !r.classList.contains("off")) { sel.has(r.dataset.key) ? sel.delete(r.dataset.key) : sel.add(r.dataset.key); render(); }
  });
  m.box.addEventListener("change", (e) => {
    if (e.target.matches("[data-show]")) { show = e.target.value; render(); }
    if (e.target.matches("[data-remember]") && !e.target.checked) write(KEY_STORE, "");
  });
  $("[data-target]").addEventListener("keydown", (e) => { if (e.key === "Enter") fetch($("[data-fetch]")); });
  ($("[data-key]").value ? $("[data-target]") : $("[data-key]")).focus();
}
