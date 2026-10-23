
import { REGION_NAME, act, ago, bytes, call, color, dateTime, esc, fmt, gameIcon, icon, keepScroll, loadCatalog, loadUsers,
  reportError, store, userName } from "./core.js";
import { ask, download, empty, opts, pickFile } from "./widgets.js";

const SORTS = [["seen", "Last seen"], ["name", "Name"], ["id", "Viewer id"], ["carats", "Carats"], ["veterans", "Veterans"],
               ["size", "Save size"]];

export const usersWin = {
  title: "Users", icon: "users", size: [760, 520], place: [0, 0, 0.62, 1], key: "1",
  tools(win) {
    const b = document.createElement("button");
    b.className = "wbtn";
    b.title = "Reload";
    b.innerHTML = icon("refresh");
    b.onclick = () => { loadUsers(); win.refresh(); };
    return [b];
  },
  mount(body, win) {
    win.state.q ??= "";
    win.state.sort ??= "seen";
    let detail = null, loading = false, detailFor = null;
    body.classList.add("flush");
    body.innerHTML = `<div class="split"><div class="side"><div class="pane-top">
        <div class="row"><label class="search"><span>${icon("search")}</span><input type="text" data-q placeholder="Name or viewer id" spellcheck="false"></label></div>
        <div class="filters"><label>Sort <select data-sort>${opts(SORTS, win.state.sort)}</select></label><span class="faint" data-count></span></div>
      </div><div class="pane-scroll"><div class="ulist"></div></div>
      <div class="pane-foot"><button class="btn sm" data-new>${icon("plus")} New save…</button></div></div>
      <div class="main"><div class="pane-scroll" data-detail></div></div></div>`;
    const list = body.querySelector(".ulist");
    const det = body.querySelector("[data-detail]");
    const q = body.querySelector("[data-q]");
    q.value = win.state.q;

    const users = () => store.users[store.region]?.users || [];
    function sorted() {
      const s = win.state.q.trim().toLowerCase();
      const out = users().filter((u) => !s || u.id.includes(s) || (u.name || "").toLowerCase().includes(s));
      const by = {
        seen: (a, b) => (b.last_seen || 0) - (a.last_seen || 0), name: (a, b) => userName(a).localeCompare(userName(b)),
        id: (a, b) => a.id.localeCompare(b.id), carats: (a, b) => ((b.fcoin || 0) + (b.coin || 0)) - ((a.fcoin || 0) + (a.coin || 0)),
        veterans: (a, b) => (b.veterans || 0) - (a.veterans || 0), size: (a, b) => b.bytes - a.bytes,
      }[win.state.sort];
      return out.sort(by);
    }
    function renderList() {
      const rows = sorted();
      body.querySelector("[data-count]").textContent = `${rows.length} of ${users().length} · ${REGION_NAME[store.region]}`;
      keepScroll(list, () => {
        list.innerHTML = rows.map((u) => `<button class="urow ${u.id === store.viewer ? "on" : ""}" data-id="${esc(u.id)}">
          <div class="avatar sm ${u.leader ? "has-img" : ""}" style="--c:${color(u.id)}">${u.leader ? gameIcon("chara", u.leader, "av") : ""}
            <span class="initial">${esc(userName(u)[0]?.toUpperCase() || "?")}</span></div>
          <div class="grow"><span class="name ellipsis">${esc(userName(u))}${u.default && store.users[store.region]?.chosen ? ` <span class="tag" title="Opens by default">default</span>` : ""}</span>
            <span class="faint ellipsis">${esc(u.id)} · ${u.last_seen ? ago(u.last_seen) : "no login"}${u.career ? " · in career" : ""}</span></div>
          <span class="faint">${bytes(u.bytes)}</span></button>`).join("") || `<div class="faint">No users.</div>`;
      });
    }

    async function loadDetail() {
      const vid = store.viewer;
      if (!vid) { det.innerHTML = empty("Pick a user."); return; }
      loading = true;
      detailFor = `${store.region}:${vid}`;
      if (!detail) det.innerHTML = empty("Loading…");
      try {
        const d = await call("users.detail");
        if (detailFor === `${store.region}:${vid}`) detail = d;
      } catch (ex) { detail = null; det.innerHTML = empty(esc(ex.message)); loading = false; return; }
      loading = false;
      renderDetail();
    }

    function tile(label, value, sub, open) {
      return `<div class="tile ${open ? "link" : ""}" ${open ? `data-open="${open}"` : ""}><div class="top"><span class="label">${label}</span>
        <span class="value">${value}</span></div>${sub ? `<div class="detail">${sub}</div>` : ""}</div>`;
    }

    function renderDetail() {
      const d = detail;
      if (!d) return;
      const cat = store.catalogs[store.region];
      const p = d.profile, a = d.account, c = d.counts;
      const charas = cat ? Object.entries(cat.misc.charas).sort((x, y) => x[1].localeCompare(y[1])) : [];
      const honors = cat?.misc.honors || [];
      keepScroll(det, () => {
        det.innerHTML = `<div class="editor">
        <div class="ed-head"><div class="avatar ${p.leader_chara_id ? "has-img" : ""}" style="--c:${color(d.id)};width:56px;height:56px;border-radius:16px">
            ${p.leader_chara_id ? gameIcon("chara", p.leader_chara_id, "av") : ""}<span class="initial">${esc((p.name || "?")[0])}</span></div>
          <div class="grow"><div class="trainer">${esc(p.name || "(no name)")}</div>
            <div class="faint">${esc(d.id)} · ${REGION_NAME[d.region]} · last seen ${a.last_seen ? ago(a.last_seen) : "never"}</div></div>
          ${(() => { const meta = store.users[store.region]; const on = meta?.default === d.id && meta?.chosen;
            return `<button class="btn sm ${on ? "" : "ghost"}" data-act="${on ? "undefault" : "default"}"
              title="${on ? "Tools and the panel open this account when none is picked. Click to go back to the last seen account." : "Open this account by default in the panel and the command-line tools"}">
              ${on ? "Default account ✓" : "Make default"}</button>`; })()}
          <button class="btn sm" data-act="export">${icon("download")} Export save</button></div>

        <div class="tiles sm">
          ${tile("Free carats", fmt(d.wallet.fcoin), "", "items")}
          ${tile("Paid carats", fmt(d.wallet.coin), "", "items")}
          ${tile("Money", fmt(d.money), "", "items")}
          ${tile("TP", `${fmt(d.tp.current_tp)}<span class="faint">/${fmt(d.tp.max_tp)}</span>`, "", "items")}
          ${tile("Umas", fmt(c.umas), `${fmt(c.characters)} characters`, "umas")}
          ${tile("Support cards", fmt(c.supports), "", "supports")}
          ${tile("Veterans", fmt(c.veterans), d.best_veteran ? `best ${esc(d.best_veteran.rank)} · ${fmt(d.best_veteran.score)}` : "", "legacy")}
          ${tile("Items", fmt(c.items), `${fmt(c.pieces)} piece stacks`, "items")}
          ${tile("Mail", fmt(c.mail_unclaimed), `${fmt(c.mail_total)} total`, "more")}
          ${tile("Career", d.career ? `T${d.career.turn}` : "–", d.career ? esc(d.career.card) : d.parked.length ? `${d.parked.length} parked` : "none", "career")}
        </div>

        <div class="ed-sec"><div class="section">Profile</div>
          <div class="ed-row">
            <label>Name<input type="text" class="sm" data-p="name" value="${esc(p.name || "")}" maxlength="24"></label>
            <label>Comment<input type="text" class="sm" data-p="comment" value="${esc(p.comment || "")}" style="width:260px"></label>
            <label>Sex<select class="sm" data-p="sex">${opts([[1, "Male"], [2, "Female"]], p.sex)}</select></label>
          </div>
          <div class="ed-row">
            <label>Leader<select class="sm" data-p="leader_chara_id">${opts([["", "–"], ...charas], p.leader_chara_id)}</select></label>
            <label>Title<select class="sm" data-p="honor_id" style="max-width:280px">${opts([["", "–"], ...honors.map((h) => [h.id, h.name])], p.honor_id)}</select></label>
            <label>Birthday<input type="text" class="sm" data-p="birth_day" value="${esc(p.birth_day || "")}" placeholder="YYYYMMDD" style="width:110px"></label>
            <button class="btn primary sm" data-act="profile">${icon("save")} Save profile</button>
          </div>
          <div class="faint">Registered ${esc(p.register_time || "–")} · created ${esc(p.create_time || "–")} · tutorial step ${fmt(p.tutorial_step)}
            · directory level ${fmt(d.social.directory_level)} · ${fmt(d.social.single_mode_play_count)} careers</div>
        </div>

        <div class="ed-sec"><div class="section">Account</div>
          <div class="kv">
            <span>Account row</span><span>${a.exists ? `created ${dateTime(a.created_at)} · last seen ${dateTime(a.last_seen)}` : "none (the save exists without a login record)"}</span>
            <span>Link password</span><span>${a.password_reset ? `<span class="warn-t">reset: any password works once</span>` : a.has_password ? "set" : "not set"}</span>
            <span>Deletion</span><span>${a.pending_deletion_at ? `<span class="warn-t">pending until ${dateTime(a.pending_deletion_at)}</span>
              <button class="linkish" data-act="cancel-del">cancel it</button>` : "none pending"}</span>
            <span>Device</span><span class="mono">${esc(a.udid || "–")}</span>
          </div>
          <div class="ed-row"><input type="password" class="sm" data-pw placeholder="New link password" autocomplete="new-password">
            <button class="btn sm" data-act="set-pw">Set password</button>
            <button class="btn sm" data-act="reset-pw" title="The next link login with any password sets the new one">Reset (accept any)</button></div>
        </div>

        <div class="ed-sec"><div class="section">Save</div>
          <div class="faint">${d.keys.length} keys · ${bytes(d.keys.reduce((n, k) => n + k.size, 0))}
            · paid features: ${Object.entries(d.paid).filter(([, t]) => t > Date.now() / 1000).map(([n]) => n).join(", ") || "none"}</div>
          <div class="ed-row">
            <button class="btn sm" data-open="save">${icon("database")} Open raw save</button>
            <button class="btn sm" data-act="import">${icon("upload")} Import save…</button>
            <button class="btn sm" data-act="copy">${icon("copy")} Copy from another viewer…</button>
          </div>
        </div>

        <div class="ed-sec danger-zone"><div class="section">Danger zone</div>
          <div class="ed-row"><span class="faint grow">Deletes this viewer's whole save${a.exists ? " and its login record" : ""}. The save can be restored from History; the login record can't.</span>
            <button class="btn danger sm" data-act="delete">${icon("trash")} Delete account</button></div>
        </div></div>`;
      });
    }

    async function doAct(name, btn) {
      const d = detail;
      if (name === "profile") {
        const fields = {};
        for (const el of det.querySelectorAll("[data-p]")) if (el.value !== "") fields[el.dataset.p] = el.value;
        await act(btn, "Saving profile", () => call("users.profile", fields), "Profile saved");
      } else if (name === "set-pw") {
        const pw = det.querySelector("[data-pw]").value;
        await act(btn, "Setting password", () => call("users.set_password", { password: pw }), "Password set");
      } else if (name === "reset-pw") {
        await act(btn, "Resetting password", () => call("users.reset_password"), "Password reset: the next link login sets it");
      } else if (name === "cancel-del") {
        await act(btn, "Cancelling deletion", () => call("users.cancel_deletion"), "Deletion cancelled");
      } else if (name === "export") {
        const data = await act(btn, "Exporting", () => call("save.export"));
        if (data) download(`umaps-${store.region}-${d.id}.json`, data);
        return;
      } else if (name === "import") {
        const f = await pickFile();
        if (!f) return;
        let data;
        try { data = JSON.parse(f.text); } catch (ex) { return reportError("Import", ex); }
        const mode = await ask(win, { title: "Import save", ok: "Import",
          text: `${esc(f.name)}: ${Object.keys(data).length} keys. <b>Merge</b> replaces only the keys in the file; type <b>replace</b> to wipe the save first.`,
          input: "text", value: "merge" });
        if (!mode) return;
        await act(btn, "Importing", () => call("save.import", { data, mode: mode.trim() }), "Imported");
      } else if (name === "default" || name === "undefault") {
        await act(btn, "Default account", () => call("users.set_default", { on: name === "default" }),
          name === "default" ? "Default account set" : "Default is now the last seen account");
      } else if (name === "copy") {
        const src = await ask(win, { title: "Copy another viewer's save onto this one",
          text: `Everything in <b>${esc(d.id)}</b> is replaced (undo is in History).`, input: "text", placeholder: "Source viewer id", ok: "Copy" });
        if (!src) return;
        await act(btn, "Copying save", () => call("users.copy", { source: src, overwrite: true }), "Save copied");
      } else if (name === "delete") {
        const typed = await ask(win, { title: "Delete this account?", danger: true, ok: "Delete",
          text: `Type the viewer id <b>${esc(d.id)}</b> to confirm.`, input: "text" });
        if (typed === null) return;
        const r = await act(btn, "Deleting", () => call("users.delete", { confirm: typed.trim() }), "Account deleted");
        if (r) { await loadUsers(); store.setTarget(store.region, store.users[store.region]?.default); }
        return;
      }
      loadUsers();
      loadDetail();
    }

    body.addEventListener("click", async (e) => {
      const row = e.target.closest(".urow");
      if (row) { store.setTarget(store.region, row.dataset.id); return; }
      const b = e.target.closest("[data-act]");
      if (b) doAct(b.dataset.act, b);
      if (e.target.closest("[data-new]")) {
        const id = await ask(win, { title: `New ${REGION_NAME[store.region]} save`, input: "text", placeholder: "Viewer id (digits)", ok: "Create",
          text: "Creates an empty save; the server seeds the rest on that viewer's first login." });
        if (!id) return;
        const vid = id.replace(/\s+/g, "");
        if (await act(null, "Creating save", () => call("users.new", {}, { viewer: vid }), "Save created")) {
          await loadUsers();
          store.setTarget(store.region, vid);
        }
      }
    });
    q.addEventListener("input", () => { win.state.q = q.value; win.save(); renderList(); });
    body.querySelector("[data-sort]").addEventListener("change", (e) => { win.state.sort = e.target.value; win.save(); renderList(); });

    win.refresh = () => { detail = null; loadDetail(); };
    const off = store.on((kind) => {
      if (kind === "users") { renderList(); if (!detail && !loading) loadDetail(); }
      if (kind === "target") { detail = null; renderList(); loadDetail(); }
      if (kind === "catalog") renderDetail();
      if (kind === "history" && !loading) loadDetail();
    });
    loadCatalog().catch(() => {});
    renderList();
    loadDetail();
    return off;
  },
};

