
import { REGION_NAME, act, ago, bytes, call, esc, icon, keepScroll, store } from "./core.js";
import { ask, empty } from "./widgets.js";

export const historyWin = {
  title: "History", icon: "clock", size: [560, 400], place: [0.45, 0.1, 0.55, 0.85], key: "9",
  tools(win) {
    const b = document.createElement("button");
    b.className = "wbtn";
    b.title = "Reload";
    b.innerHTML = icon("refresh");
    b.onclick = () => win.refresh();
    return [b];
  },
  mount(body, win) {
    win.state.mine ??= true;
    let rows = [], open = null, detail = null;
    body.classList.add("flush");

    async function load() {
      try { rows = await call("history.list", { this_viewer: win.state.mine, limit: 300 }); }
      catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      render();
    }
    async function openRow(id) {
      open = open === id ? null : id;
      detail = null;
      render();
      if (open) {
        try { detail = await call("history.get", { id }); } catch (ex) { detail = { error: ex.message }; }
        render();
      }
    }

    function render() {
      keepScroll(body, () => {
        body.innerHTML = `<div class="pane-top"><div class="filters">
            <label><input type="checkbox" data-mine ${win.state.mine ? "checked" : ""}> only ${esc(store.viewer)}</label>
            <span class="faint">${REGION_NAME[store.region]} · ${rows.length} change${rows.length === 1 ? "" : "s"} · kept in server/data${store.region === "jp" ? "/jp" : ""}/admin_history.sqlite3</span></div></div>
          <div class="pane-scroll"><div class="vt-list">${rows.map((r) => `<div>
            <div class="hist-row ${r.undone_by ? "undone" : ""} ${open === r.id ? "on" : ""}" data-id="${r.id}">
              <span class="faint">#${r.id}</span><span class="faint" title="${new Date(r.ts * 1000).toLocaleString()}">${ago(r.ts)}</span>
              <span class="ellipsis"><b>${esc(r.label)}</b> <span class="faint">${esc(r.viewer)} · ${r.keys.length} key${r.keys.length === 1 ? "" : "s"}: ${esc(r.keys.slice(0, 4).join(", "))}${r.keys.length > 4 ? "…" : ""}</span></span>
              <span>${r.undone_by ? `<span class="faint">undone by #${r.undone_by}</span>` : ""}</span></div>
            ${open === r.id ? `<div class="hist-body">${!detail ? `<div class="faint">Loading…</div>` : detail.error ? `<div class="neg">${esc(detail.error)}</div>` : `
              <table class="grid left"><thead><tr><th>Key</th><th>Before</th><th>Now</th></tr></thead><tbody>${detail.keys.map((k) => `<tr>
                <td class="mono">${esc(k.key)}</td><td>${k.before === null ? `<span class="faint">absent</span>` : bytes(k.before)}</td>
                <td>${k.now === null ? `<span class="faint">absent</span>` : bytes(k.now)}</td></tr>`).join("")}</tbody></table>
              ${detail.later.length ? `<div class="warn-t">Later changes touched the same keys and would be lost on those keys: ${detail.later.map((l) => `#${l.id} ${esc(l.label)}`).join(", ")}</div>` : ""}
              <div class="row"><button class="btn primary sm" data-undo="${r.id}">${icon("undo")} Undo this change</button>
                <span class="faint">Puts ${detail.keys.length === 1 ? "this key" : "these keys"} back exactly as they were before #${r.id}.</span></div>`}</div>` : ""}
            </div>`).join("") || empty("No changes yet.")}</div></div>`;
      });
    }

    body.addEventListener("click", async (e) => {
      const u = e.target.closest("[data-undo]");
      if (u) {
        const id = +u.dataset.undo;
        if (detail?.later?.length && !(await ask(win, { title: "Undo over later changes?", ok: "Undo anyway", danger: true,
          text: `${detail.later.length} later change(s) touched the same keys; their edits to those keys are replaced too.` }))) return;
        if (await act(u, "Undo", () => call("history.undo", { id }), `Undid #${id}`)) { open = null; load(); }
        return;
      }
      const row = e.target.closest(".hist-row");
      if (row) openRow(+row.dataset.id);
    });
    body.addEventListener("change", (e) => { if (e.target.matches("[data-mine]")) { win.state.mine = e.target.checked; win.save(); load(); } });

    win.refresh = load;
    const off = store.on((kind) => { if (kind === "target" || kind === "history") load(); });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
