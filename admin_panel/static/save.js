
import { REGION_NAME, act, bytes, call, esc, icon, keepScroll, reportError, store } from "./core.js";
import { ask, download, empty, pickFile, targetPicker } from "./widgets.js";

const BIG = 1_500_000;

const preview = (v) => {
  if (v === null) return `<span class="jt-x">null</span>`;
  if (Array.isArray(v)) return `<span class="jt-x">[${v.length}]</span>`;
  if (typeof v === "object") return `<span class="jt-x">{${Object.keys(v).length}}</span>`;
  if (typeof v === "string") return `<span class="jt-s">"${esc(v.length > 80 ? v.slice(0, 80) + "…" : v)}"</span>`;
  if (typeof v === "boolean") return `<span class="jt-b">${v}</span>`;
  return `<span class="jt-n">${esc(v)}</span>`;
};
const joinPath = (path, k) => (path ? `${path}.${k}` : String(k));

export const saveWin = {
  title: "Raw save", icon: "database", size: [760, 520], place: [0.2, 0.04, 0.78, 0.92], key: "8",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    let keys = [], cur = null, value = undefined, text = "", dirty = false, forced = false;
    win.state.filter ??= "";
    body.classList.add("flush");
    body.innerHTML = `<div class="split"><div class="side"><div class="pane-top">
        <label class="search"><span>${icon("search")}</span><input type="text" data-filter placeholder="Filter keys" spellcheck="false"></label>
        <div class="row"><button class="btn sm" data-a="new">${icon("plus")} Key</button><button class="btn sm" data-a="export">${icon("download")} Export</button>
          <button class="btn sm" data-a="import">${icon("upload")} Import</button></div>
      </div><div class="pane-scroll"><div class="keylist"></div></div><div class="pane-foot faint" data-sum></div></div>
      <div class="main" data-main></div></div>`;
    const keylist = body.querySelector(".keylist");
    const main = body.querySelector("[data-main]");
    const filter = body.querySelector("[data-filter]");
    filter.value = win.state.filter;

    async function loadKeys() {
      try { keys = await call("save.keys"); } catch (ex) { keys = []; main.innerHTML = empty(esc(ex.message)); }
      renderKeys();
      body.querySelector("[data-sum]").textContent = `${keys.length} keys · ${bytes(keys.reduce((n, k) => n + k.size, 0))} · ${REGION_NAME[store.region]} ${store.viewer}`;
    }
    function renderKeys() {
      const f = win.state.filter.toLowerCase();
      keepScroll(keylist, () => {
        keylist.innerHTML = keys.filter((k) => !f || k.key.toLowerCase().includes(f)).map((k) =>
          `<button class="keyrow ${cur?.key === k.key ? "on" : ""}" data-key="${esc(k.key)}"><span class="k">${esc(k.key)}</span>
            <span class="typetag">${k.type === "array" ? `[${k.count}]` : k.type === "object" ? `{${k.count}}` : esc(k.type)}</span>
            <span class="sz">${bytes(k.size)}</span></button>`).join("") || `<div class="faint">No keys.</div>`;
      });
    }

    async function open(key, path = "") {
      if (dirty && !(await ask(win, { title: "Discard unsaved edits?", ok: "Discard", danger: true }))) return;
      cur = { key, path };
      dirty = false; forced = false;
      renderKeys();
      main.innerHTML = empty("Loading…");
      try {
        const r = await call("save.get", { key, path });
        value = r.value;
        text = JSON.stringify(value, null, 2);
      } catch (ex) { main.innerHTML = empty(esc(ex.message)); return; }
      renderMain();
    }

    function crumbs() {
      const parts = cur.path ? cur.path.split(".") : [];
      let acc = "";
      return `<button class="linkish" data-path="">${esc(cur.key)}</button>` + parts.map((p) => {
        acc = joinPath(acc, p);
        return ` <span class="faint">›</span> <button class="linkish" data-path="${esc(acc)}">${esc(p)}</button>`;
      }).join("");
    }

    function renderMain() {
      if (!cur) { main.innerHTML = empty("Pick a key on the left."); return; }
      const nested = value && typeof value === "object";
      const entries = nested ? (Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value)) : [];
      const big = text.length > BIG && !forced;
      main.innerHTML = `<div class="pane-top">
          <div class="row wrap"><div class="grow" style="font-weight:700">${crumbs()}</div>
            <span class="faint">${bytes(text.length)}</span></div>
          <div class="row wrap"><input type="text" class="sm mono" data-goto placeholder="path inside, e.g. data.chara_info.speed" style="flex:1;min-width:200px" spellcheck="false">
            <button class="btn sm" data-a="goto">Go</button></div>
        </div>
        <div class="pane-scroll">
          ${nested ? `<details ${entries.length <= 200 ? "open" : ""}><summary class="faint" style="cursor:pointer">${entries.length} ${Array.isArray(value) ? "items" : "fields"} — click one to open it</summary>
            <div class="json-tree" style="margin:6px 0 12px;max-height:260px;overflow:auto">${entries.slice(0, 2000).map(([k, v]) =>
              `<div class="jt-row" data-into="${esc(joinPath(cur.path, k))}"><span class="jt-k">${esc(k)}</span>${preview(v)}</div>`).join("")}
              ${entries.length > 2000 ? `<div class="faint">…${entries.length - 2000} more (use the path box)</div>` : ""}</div></details>` : ""}
          ${big ? `<div class="empty">This value is ${bytes(text.length)}. Open a smaller path above, or
              <button class="btn sm" data-a="force">Load it into the editor anyway</button></div>`
            : `<textarea class="code" data-text spellcheck="false" style="min-height:${nested ? 300 : 120}px">${esc(text)}</textarea>`}
        </div>
        <div class="pane-foot"><button class="btn primary sm" data-a="save" ${big ? "disabled" : ""}>${icon("save")} Save</button>
          <button class="btn sm" data-a="format" ${big ? "disabled" : ""}>Format</button>
          <button class="btn sm" data-a="reload">${icon("refresh")} Reload</button>
          <span class="grow faint" data-status></span>
          <button class="btn sm" data-a="dl">${icon("download")}</button>
          <button class="btn danger sm" data-a="del">${icon("trash")} Delete ${cur.path ? "this path" : "key"}</button></div>`;
      const ta = main.querySelector("[data-text]");
      ta?.addEventListener("input", () => {
        dirty = true;
        let ok = true;
        try { JSON.parse(ta.value); } catch { ok = false; }
        ta.classList.toggle("bad", !ok);
        main.querySelector("[data-status]").textContent = ok ? "edited" : "not valid JSON";
      });
      ta?.addEventListener("keydown", (e) => {
        if (e.key === "Tab") { e.preventDefault(); ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end"); }
        if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); main.querySelector('[data-a="save"]').click(); }
      });
    }

    async function onAction(a, btn) {
      if (a === "new") {
        const name = await ask(win, { title: "New state key", input: "text", placeholder: "key_name", ok: "Create" });
        if (!name) return;
        if (await act(btn, "Creating key", () => call("save.set", { key: name.trim(), value: "{}", create: true }), "Key created")) {
          await loadKeys(); open(name.trim());
        }
      } else if (a === "export") {
        const data = await act(btn, "Exporting", () => call("save.export"));
        if (data) download(`umaps-${store.region}-${store.viewer}.json`, data);
      } else if (a === "import") {
        const f = await pickFile();
        if (!f) return;
        let data;
        try { data = JSON.parse(f.text); } catch (ex) { return reportError("Import", ex); }
        const mode = await ask(win, { title: "Import save", ok: "Import", input: "text", value: "merge",
          text: `${esc(f.name)}: ${Object.keys(data).length} keys into ${esc(store.viewer)}. <b>merge</b> replaces the keys in the file; <b>replace</b> wipes the save first.` });
        if (!mode) return;
        if (await act(btn, "Importing", () => call("save.import", { data, mode: mode.trim() }), "Imported")) { cur = null; loadKeys(); renderMain(); }
      } else if (!cur) {
        return;
      } else if (a === "save") {
        const ta = main.querySelector("[data-text]");
        let parsed;
        try { parsed = JSON.parse(ta.value); } catch (ex) { return reportError("Not valid JSON", ex); }
        if (await act(btn, "Saving", () => call("save.set", { key: cur.key, path: cur.path, value: JSON.stringify(parsed) }), "Saved")) {
          dirty = false; value = parsed; text = JSON.stringify(parsed, null, 2); loadKeys(); renderMain();
        }
      } else if (a === "format") {
        const ta = main.querySelector("[data-text]");
        try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); } catch (ex) { reportError("Not valid JSON", ex); }
      } else if (a === "reload") {
        dirty = false; open(cur.key, cur.path);
      } else if (a === "force") {
        forced = true; renderMain();
      } else if (a === "dl") {
        download(`${cur.key}${cur.path ? "." + cur.path : ""}.json`, text);
      } else if (a === "goto") {
        const p = main.querySelector("[data-goto]").value.trim();
        open(cur.key, p);
      } else if (a === "del") {
        const what = cur.path ? `${cur.key}.${cur.path}` : cur.key;
        if (!(await ask(win, { title: `Delete ${what}?`, danger: true, ok: "Delete", text: "Undo is in History." }))) return;
        if (await act(btn, "Deleting", () => call("save.delete", { key: cur.key, path: cur.path }), `Deleted ${what}`)) {
          dirty = false;
          if (cur.path) open(cur.key, cur.path.split(".").slice(0, -1).join("."));
          else { cur = null; renderMain(); }
          loadKeys();
        }
      }
    }

    body.addEventListener("click", (e) => {
      const k = e.target.closest("[data-key]");
      if (k) return open(k.dataset.key);
      const into = e.target.closest("[data-into]");
      if (into) return open(cur.key, into.dataset.into);
      const p = e.target.closest("[data-path]");
      if (p) return open(cur.key, p.dataset.path);
      const b = e.target.closest("[data-a]");
      if (b) onAction(b.dataset.a, b);
    });
    body.addEventListener("keydown", (e) => {
      if (e.target.matches("[data-goto]") && e.key === "Enter") onAction("goto");
    });
    filter.addEventListener("input", () => { win.state.filter = filter.value; win.save(); renderKeys(); });

    win.reveal = (o) => { if (o.key) open(o.key, o.path || ""); };
    const off = store.on((kind) => {
      if (kind === "target") { cur = null; dirty = false; renderMain(); loadKeys(); }
      if (kind === "history") loadKeys();
    });
    renderMain();
    loadKeys();
    return off;
  },
};
