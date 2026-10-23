
import { act, call, dateTime, esc, gameIcon, icon, keepScroll, loadCatalog, store } from "./core.js";
import { customSupportWin } from "./custom-support.js";
import { ask, empty, itemRow, itemSearch, opts, picker, supportRow, supportSearch, targetPicker, umaRow, umaSearch } from "./widgets.js";

const TABS = [["mail", "Mail"], ["paid", "Paid features"], ["titles", "Titles"], ["epithets", "Epithets"], ["songs", "Songs"], ["friends", "Friend cards"], ["custom", "Custom cards · experimental"]];
const KINDS = [["item", "Item"], ["90", "Carats"], ["50", "Uma card"], ["51", "Support card"], ["102", "Star pieces"], ["80", "Song"], ["70", "Outfit"]];
const PAID = { umasuku: "Umasuku (daily carats pass)", umaplan: "Umaplan (pick-two inspiration)", reroll_pass: "Spark re-roll pass", order_pass: "Spark order pass" };
const REWARD = { 90: "Carats", 50: "Uma", 51: "Support", 70: "Outfit", 80: "Song", 102: "Pieces" };

export const moreWin = {
  title: "Mail & extras", icon: "sparkle", size: [620, 460], place: [0.25, 0.06, 0.65, 0.88], key: "3",
  tools(win) { return [targetPicker(win)]; },
  mount(body, win) {
    win.state.tab ??= "mail";
    let host = null;
    let cat = null, d = {}, mail = { kind: "item", id: null, label: "", num: 1, msg: "", copies: 1 }, epChara = null, q = "";
    body.classList.add("flush");

    async function load() {
      if (win.state.tab === "custom") return render();
      try {
        cat = await loadCatalog();
        const t = win.state.tab;
        if (t === "mail") d.mail = await call("mail.list");
        if (t === "paid") d.paid = await call("paid.list");
        if (t === "titles") d.titles = await call("honors.list");
        if (t === "epithets") d.charas = await call("charas.list");
        if (t === "songs") d.songs = await call("music.list");
        if (t === "friends") d.friends = await call("friends.list");
      } catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      render();
    }
    const tabs = () => `<div class="tabs" style="margin:0">${TABS.map(([k, t]) => `<button data-tab="${k}" class="${win.state.tab === k ? "on" : ""}">${t}</button>`).join("")}</div>`;
    const frame = (top, main, foot = "") => `<div class="pane-top">${tabs()}${top}</div><div class="pane-scroll">${main}</div>${foot ? `<div class="pane-foot">${foot}</div>` : ""}`;
    const search = (ph) => `<div class="filters"><label class="search"><span>${icon("search")}</span><input type="text" data-q placeholder="${ph}" value="${esc(q)}" spellcheck="false"></label></div>`;

    function rewardName(p) {
      const t = p.reward_type, id = p.reward_id;
      if (t === 90) return "Carats";
      if (t === 50) return cat.umaById.get(id)?.name ? `${cat.umaById.get(id).name} (${cat.umaById.get(id).title})` : `Uma ${id}`;
      if (t === 51) return cat.supportById.get(id) ? `${cat.supportById.get(id).name} (${cat.supportById.get(id).title})` : `Support ${id}`;
      if (t === 102) return `Pieces ${id}`;
      if (t === 80) return `Song ${id}`;
      return cat.itemById.get(id)?.name || `${REWARD[t] || `Type ${t}`} ${id}`;
    }

    function unmountCustom() {
      if (!host) return;
      for (const f of host.offs) f?.();
      host.cleanup?.();
      host = null;
    }
    function renderCustom() {
      if (host) return;
      body.innerHTML = `<div class="pane-top">${tabs()}
          <div class="notebox">Experimental. Custom cards reach players only in a custom umapack they copy into
            <span class="mono">hachimi/umapacks/</span> with the umapack-capable plugin; the server serves the rest.</div></div>
        <div class="more-host win-body" style="flex:1;min-height:0;position:relative"></div>`;
      const pane = body.querySelector(".more-host");
      host = { kind: win.kind + ":custom", el: win.el, body: pane, offs: [],
        state: win.state.t_custom ||= {}, save: () => win.save(), parent: win };
      host.cleanup = customSupportWin.mount(pane, host);
    }

    function render() {
      const t = win.state.tab;
      if (t === "custom") return renderCustom();
      unmountCustom();
      if (!cat || !d) return;
      keepScroll(body, () => { body.innerHTML = ({ mail: renderMail, paid: renderPaid, titles: renderTitles, epithets: renderEpithets,
        songs: renderSongs, friends: renderFriends })[t]?.() || ""; });
      if (t === "mail") mountMailPicker();
      if (t === "friends") mountFriendPicker();
    }

    function renderMail() {
      if (!d.mail) return frame("", empty("Loading…"));
      const box = [...d.mail].reverse();
      const unclaimed = box.filter((p) => !p.state).length;
      return frame(`<div class="ed-sec"><div class="section">Send a present</div><div class="ed-row">
          <label>Kind<select class="sm" data-mk>${opts(KINDS, mail.kind)}</select></label>
          ${mail.kind === "90" ? "" : `<label>What<span data-mpick></span></label>`}
          <label>Amount<input type="number" class="num wide" data-mn value="${mail.num}" min="1"></label>
          <label>Copies<input type="number" class="num xs" data-mc value="${mail.copies}" min="1" max="100"></label></div>
          <div class="ed-row"><label style="flex:1">Message<input type="text" class="sm" data-mm value="${esc(mail.msg)}" placeholder="optional" style="width:100%"></label>
          <button class="btn primary sm" data-a="send">${icon("mail")} Send</button></div>
          <div class="faint" data-mwhat>${mail.kind === "90" ? "Carats (free)" : mail.id ? `Selected: ${esc(mail.label)} · #${mail.id}` : "Pick what to send."}</div></div>
          <div class="count-line"><span><b>${unclaimed}</b> unclaimed · ${box.length} total</span><span class="grow"></span>
            <button class="btn sm" data-a="clear">Clear unclaimed</button><button class="btn danger sm" data-a="clearall">Clear all</button></div>`,
        `<table class="grid left"><thead><tr><th>Present</th><th>Amount</th><th>State</th><th>Message</th><th>Created</th><th></th></tr></thead><tbody>
          ${box.map((p) => `<tr><td>${esc(rewardName(p))} <span class="faint">${p.reward_type}/${p.reward_id}</span></td><td>${(p.reward_count || 0).toLocaleString()}</td>
            <td>${p.state ? `<span class="faint">claimed</span>` : `<span class="accent">waiting</span>`}</td>
            <td class="ellipsis" style="max-width:220px">${esc(p.free_message || "")}</td><td class="faint">${esc(p.create_time || "")}</td>
            <td><button class="wbtn" data-mdel="${p.present_id}" title="Delete">${icon("close")}</button></td></tr>`).join("")}</tbody></table>`);
    }
    function mountMailPicker() {
      const ph = body.querySelector("[data-mpick]");
      if (!ph) return;
      const k = mail.kind;
      const cfg = k === "item" ? { search: itemSearch(cat), row: itemRow, label: (x) => x.name }
        : k === "50" || k === "102" ? { search: umaSearch(cat), row: umaRow, label: (x) => `${x.name} (${x.title})`, id: (x) => (k === "102" ? x.piece_id : x.id) }
        : k === "51" ? { search: supportSearch(cat), row: supportRow, label: (x) => `${x.name} (${x.title})` }
        : null;
      if (!cfg) { ph.outerHTML = `<input type="number" class="num wide" data-mid placeholder="id" value="${mail.id ?? ""}">`; return; }
      ph.replaceWith(picker({ placeholder: "Search…", width: 200, search: cfg.search, row: cfg.row,
        pick: (x) => { mail.id = cfg.id ? cfg.id(x) : x.id; mail.label = cfg.label(x); mail.cat = x.category;
          body.querySelector("[data-mwhat]").textContent = `Selected: ${mail.label} · #${mail.id}`; } }));
    }

    function renderPaid() {
      if (!d.paid) return frame("", empty("Loading…"));
      const now = Date.now() / 1000;
      return frame("", `<div class="editor">${d.paid.features.map((p) => `<div class="ed-sec"><div class="row"><div class="grow"><b>${esc(PAID[p.name] || p.name)}</b>
          <div class="faint">${p.active ? `active until ${dateTime(p.end)} (${Math.ceil((p.end - now) / 86400)} days)` : "not held"}</div></div>
          <input type="number" class="num xs" data-days="${p.name}" value="30" min="1"> days
          <button class="btn primary sm" data-grant="${p.name}">${p.active ? "Extend" : "Grant"}</button>
          <button class="btn sm" data-revoke="${p.name}" ${p.active ? "" : "disabled"}>Revoke</button></div></div>`).join("")}
        <div class="faint">Re-roll tickets: ${d.paid.tickets.reroll} · spark order tickets: ${d.paid.tickets.order} (items ${d.paid.ticket_items.reroll} / ${d.paid.ticket_items.order}, edit them under Items).
          The client re-reads these at login: restart the game or return to the title screen.</div></div>`,
        `<button class="btn primary sm" data-grant="all">Grant all for 30 days</button><button class="btn sm" data-revoke="all">Revoke all</button>`);
    }

    function renderTitles() {
      if (!d.titles) return frame("", empty("Loading…"));
      const owned = d.titles.owned;
      const ql = q.toLowerCase();
      const list = cat.misc.honors.filter((h) => !ql || h.name.toLowerCase().includes(ql) || String(h.id) === ql);
      return frame(search("Title name or id") + `<div class="count-line"><span><b>${Object.keys(owned).length}</b> of ${cat.misc.honors.length} owned</span></div>`,
        `<div class="buy-list">${list.map((h) => `<label class="buy ${owned[h.id] ? "on" : ""}"><input type="checkbox" data-honor="${h.id}" ${owned[h.id] ? "checked" : ""}>
          <span class="grow">${esc(h.name)}</span><span class="faint">#${h.id}${owned[h.id] ? ` · ${esc(owned[h.id])}` : ""}</span></label>`).join("")}</div>`,
        `<button class="btn primary sm" data-a="honors-all">Grant all titles</button><button class="btn sm" data-a="honors-shown">Grant shown</button>
         <button class="btn danger sm" data-a="honors-none">Remove shown</button>`);
    }

    function renderEpithets() {
      if (!d.charas) return frame("", empty("Loading…"));
      const charas = Object.entries(cat.misc.charas).sort((a, b) => a[1].localeCompare(b[1]));
      epChara ??= +charas[0]?.[0];
      const owned = new Set(d.charas.epithets[String(epChara)] || []);
      const list = cat.misc.nicknames.filter((n) => !n.chara || n.chara === epChara);
      return frame(`<div class="filters"><label>Character <select data-epc>${opts(charas, epChara)}</select></label>
          <span class="faint">${owned.size} of ${list.length} owned</span></div>`,
        `<div class="buy-list">${list.map((n) => `<label class="buy ${owned.has(n.id) ? "on" : ""}"><input type="checkbox" data-nick="${n.id}" ${owned.has(n.id) ? "checked" : ""}>
          <span class="grow">${esc(n.name)}</span><span class="faint">#${n.id}${n.chara ? "" : " · any uma"}</span></label>`).join("")}</div>`,
        `<button class="btn sm" data-a="ep-all">Tick all</button><button class="btn sm" data-a="ep-none">Untick all</button>
         <span class="grow"></span><button class="btn primary sm" data-a="ep-save">${icon("save")} Save epithets</button>`);
    }

    function renderSongs() {
      if (!d.songs) return frame("", empty("Loading…"));
      const owned = new Set(d.songs.owned);
      const ql = q.toLowerCase();
      const list = d.songs.songs.filter((s) => !ql || s.name.toLowerCase().includes(ql));
      return frame(search("Song") + `<div class="count-line"><span><b>${owned.size}</b> of ${d.songs.songs.length} owned</span></div>`,
        `<div class="buy-list">${list.map((s) => `<label class="buy ${owned.has(s.id) ? "on" : ""}"><input type="checkbox" data-song="${s.id}" ${owned.has(s.id) ? "checked" : ""} ${s.released ? "" : "disabled"}>
          <span class="grow">${esc(s.name)}</span><span class="faint">#${s.id}${s.released ? "" : " · not released in this region yet"}</span></label>`).join("")}</div>`,
        `<button class="btn primary sm" data-a="songs-all">Unlock every released song</button>`);
    }

    function renderFriends() {
      if (!d.friends) return frame("", empty("Loading…"));
      return frame(`<div class="ed-sec"><div class="section">Add a borrowable friend card</div><div class="ed-row">
          <label>Card<span data-fpick></span></label><label>Uncap<input type="number" class="num xs" data-flb value="4" min="0" max="4"></label>
          <label>Level<input type="number" class="num xs" data-flv placeholder="max" min="1" max="50"></label>
          <label>Friend name<input type="text" class="sm" data-fname value="Friend" style="width:130px"></label></div>
          <div class="faint">Shows on the career's Borrow Card screen, for when no real friend lends what a deck needs.</div></div>`,
        `<div class="vt-list">${d.friends.map((x) => `<div class="row" style="padding:6px 4px;border-bottom:1px solid var(--border-soft)">
          ${gameIcon("support", x.support_card_id, "sm")}<div class="grow"><b>${esc(cat.supportById.get(x.support_card_id)?.name || x.support_card_id)}</b>
          <div class="faint">${esc(cat.supportById.get(x.support_card_id)?.title || "")} · lent by ${esc(x.name)} · uncap ${x.lb}</div></div>
          <button class="btn icon-btn danger" data-frm="${x.index}" title="Remove">${icon("trash")}</button></div>`).join("") || `<div class="faint">None yet.</div>`}</div>`);
    }
    function mountFriendPicker() {
      const ph = body.querySelector("[data-fpick]");
      if (!ph) return;
      ph.replaceWith(picker({ placeholder: "Search support card…", width: 220, search: supportSearch(cat), row: supportRow,
        pick: async (x) => {
          const args = { support_card_id: x.id, lb: body.querySelector("[data-flb]").value, level: body.querySelector("[data-flv]").value,
                         name: body.querySelector("[data-fname]").value };
          if (await act(null, "Friend card", () => call("friends.add", args), `Added ${x.name}`)) load();
        } }));
    }

    async function onAction(a, btn) {
      if (a === "send") {
        const kind = mail.kind;
        const id = kind === "90" ? 43 : mail.id ?? +(body.querySelector("[data-mid]")?.value || 0);
        if (!id) return;
        const category = kind === "item" ? (mail.cat ?? cat.itemById.get(id)?.category ?? 0) : +kind;
        if (await act(btn, "Send mail", () => call("mail.send", { category, item_id: id, num: mail.num, copies: mail.copies, message: mail.msg }),
          (r) => `Sent ${r.sent}`)) load();
      } else if (a === "clear" || a === "clearall") {
        if (a === "clearall" && !(await ask(win, { title: "Clear the whole present box?", danger: true, ok: "Clear",
          text: "Claimed rows are the already-redeemed ledger; dropping them can't un-grant anything." }))) return;
        if (await act(btn, "Clear mail", () => call("mail.clear", { all: a === "clearall" }), (r) => `Removed ${r.removed}`)) load();
      } else if (a.startsWith("honors")) {
        const ql = q.toLowerCase();
        const ids = cat.misc.honors.filter((h) => !ql || h.name.toLowerCase().includes(ql) || String(h.id) === ql).map((h) => h.id);
        const args = a === "honors-all" ? { all: true } : { ids, action: a === "honors-none" ? "remove" : "add" };
        if (await act(btn, "Titles", () => call("honors.set", args), (r) => `${r.owned} titles owned`)) load();
      } else if (a === "ep-all" || a === "ep-none") {
        for (const el of body.querySelectorAll("[data-nick]")) el.checked = a === "ep-all";
      } else if (a === "ep-save") {
        const ids = [...body.querySelectorAll("[data-nick]:checked")].map((el) => +el.dataset.nick);
        if (await act(btn, "Epithets", () => call("epithets.set", { chara_id: epChara, ids }), "Epithets saved")) load();
      } else if (a === "songs-all") {
        if (await act(btn, "Songs", () => call("music.set", { all: true }), (r) => `${r.changed} songs unlocked`)) load();
      }
    }

    body.addEventListener("click", async (e) => {
      if (e.target.closest(".more-host")) return;
      const b = e.target.closest("button");
      if (!b) return;
      const t = b.dataset;
      if (t.tab) { win.state.tab = t.tab; win.save(); q = ""; unmountCustom(); render(); load(); return; }
      if (t.mdel) { if (await act(b, "Delete mail", () => call("mail.delete", { ids: [+t.mdel] }), "Deleted")) load(); return; }
      if (t.grant) { const days = body.querySelector(`[data-days="${t.grant}"]`)?.value || 30;
        if (await act(b, "Paid feature", () => call("paid.grant", { name: t.grant, days }), "Granted")) load(); return; }
      if (t.revoke) { if (await act(b, "Paid feature", () => call("paid.grant", { name: t.revoke, revoke: true }), "Revoked")) load(); return; }
      if (t.frm !== undefined) { if (await act(b, "Friend card", () => call("friends.remove", { indexes: [+t.frm] }), "Removed")) load(); return; }
      if (t.a) onAction(t.a, b);
    });
    body.addEventListener("change", async (e) => {
      if (e.target.closest(".more-host")) return;
      const el = e.target;
      if (el.matches("[data-mk]")) { mail.kind = el.value; mail.id = null; render(); }
      else if (el.matches("[data-mn]")) mail.num = +el.value;
      else if (el.matches("[data-mc]")) mail.copies = +el.value;
      else if (el.matches("[data-mm]")) mail.msg = el.value;
      else if (el.matches("[data-mid]")) mail.id = +el.value;
      else if (el.matches("[data-epc]")) { epChara = +el.value; render(); }
      else if (el.dataset.honor) {
        if (await act(null, "Title", () => call("honors.set", { ids: [+el.dataset.honor], action: el.checked ? "add" : "remove" }))) load();
      } else if (el.dataset.song) {
        if (await act(null, "Song", () => call("music.set", { ids: [+el.dataset.song], action: el.checked ? "add" : "remove" }))) load();
      }
    });
    body.addEventListener("input", (e) => { if (!e.target.closest(".more-host") && e.target.matches("[data-q]")) { q = e.target.value; render(); } });

    win.reveal = (o) => { if (o.tab) { win.state.tab = o.tab; load(); } };
    const off = store.on((kind) => { if (kind === "target" && win.state.tab !== "custom") { d = {}; load(); } });
    body.innerHTML = empty("Loading…");
    load();
    return () => { unmountCustom(); off?.(); };
  },
};
