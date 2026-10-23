
import { act, call, dateTime, esc, icon, store, toast } from "./core.js";
import { ask, empty, modal } from "./widgets.js";

const DAY = 86400;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const iso = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
const dayStart = (ts) => Math.floor(ts / DAY) * DAY;
const fromIso = (s) => Date.parse(s + "T00:00:00Z") / 1000;
const short = (ts) => { const d = new Date(ts * 1000); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`; };
const long = (ts) => `${short(ts)}, ${new Date(ts * 1000).getUTCFullYear()}`;
const localInput = (ts) => {
  const d = new Date(ts * 1000);
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
};
const COLORS = { banners: "#8fb8e6", story_event: "#e3b56b", legend_race: "#e08a7a", training_challenge: "#b59be6",
  factor_research: "#8fc9a4", transfer: "#d9a0c8", home: "#a7a2b0", shops: "#c9c08f" };
const RANGES = [[14, "Zoom: close"], [42, "Zoom: medium"], [91, "Zoom: far"]];
const HELP = { banners: "Gacha banners: who you can pull", story_event: "Limited story event with its own story, missions and shop",
  legend_race: "Boss race against a legendary uma", training_challenge: "Trainer Aptitude Test event",
  factor_research: "Agnes Tachyon's research event", transfer: "Trade trained umas in for rewards",
  home: "The seasonal look of the home screen", shops: "Exchange shops that belong to events" };
const FAMILY = { story_event: "Story event", training_challenge: "Trainer Aptitude Test", factor_research: "Tachyon research",
  legend_race: "Legend Race", champions: "Champions Meeting", heroes: "League of Heroes", challenge_match: "Racing Carnival",
  team_building: "Team Building", map_event: "Map event" };

export const syncWin = {
  title: "Schedule", icon: "clock", size: [760, 560], place: [0.12, 0.05, 0.76, 0.88],
  mount(body, win) {
    let d = null, ev = null;
    let picked = null, month = null, span = 42, center = null, focusItem = null, calOpen = false, dayOpen = false;
    let bring = null;
    const target = () => ({ region: store.region, viewer: store.viewer });

    async function load() {
      try { [d, ev] = await Promise.all([call("sync.get", {}, target()), call("events.list", {}, target())]); }
      catch (ex) { body.innerHTML = empty(esc(ex.message)); return; }
      if (!d.all_groups || !d.timeline) {
        body.innerHTML = empty("The panel server is running older code. Restart the admin panel (python3 -m admin_panel) to load the Schedule window.");
        return;
      }
      const today = dayStart(d.real_time);
      picked ??= d.sync ? fromIso(d.sync.day) : today - DAY;
      month ??= picked;
      center ??= picked;
      bring ??= new Set(d.sync?.groups || Object.keys(d.all_groups));
      render();
    }

    const today = () => dayStart(d.real_time);
    const lastDay = () => today() - DAY;
    const firstDay = () => fromIso(d.first_day);
    const rerun = (group) => ev.schedules.find((e) => e.family === group);
    const lineup = () => d.lineup || { only: false, banners: [] };
    const picks = (key) => lineup().banners.find((b) => b.gacha_id === key);
    const isRerunFamily = (group) => !!ev.families[group];
    function items() {
      const out = d.timeline.slice();
      const have = new Set(out.map((i) => `${i.group}:${i.key}`));
      for (const [fam, rows] of Object.entries(ev.families)) {
        for (const r of rows) {
          if (have.has(`${fam}:${r.id}`) || !r.start_date || r.end_date >= 1893456000 || r.end_date <= firstDay()) continue;
          out.push({ group: fam, key: r.id, label: r.name || `${FAMILY[fam] || d.all_groups[fam] || fam} #${r.id}`, start: r.start_date, end: r.end_date, ported: true });
        }
      }
      return out;
    }
    const labelOf = (group, key) => items().find((i) => i.group === group && i.key === key)?.label || `${FAMILY[group] || d.all_groups[group] || group} #${key}`;
    const liveAt = (t) => items().filter((i) => i.start <= t && t <= i.end);

    function status() {
      const groups = d.published?.groups;
      const synced = d.published
        ? `Playing <b>${esc(d.published.day)}</b>${groups ? ` <span class="faint">(${groups.map((g) => esc(d.all_groups[g] || g)).join(", ")} only)</span>` : ""} · in game it is now <b>${esc(dateTime(d.game_time))}</b>`
        : `Following the stock schedule`;
      const same = (d.sync?.day || null) === (d.published?.day || null)
        && JSON.stringify(d.sync?.groups || null) === JSON.stringify(d.published?.groups || null);
      const pending = same ? "" : ` · <span style="color:var(--warn)">${d.sync ? `${esc(d.sync.day)} saved but not published` : "not published"}</span>`;
      const now = d.real_time;
      const left = (t) => { const n = Math.ceil((t - now) / DAY); return n <= 1 ? "ends within a day" : `${n} days left`; };
      const state = (e) => e.end_date < now ? ["ended", 2] : e.start_date > now
        ? [`starts in ${Math.ceil((e.start_date - now) / DAY)} day${Math.ceil((e.start_date - now) / DAY) === 1 ? "" : "s"}`, 1] : [`on now · ${left(e.end_date)}`, 0];
      const rows = ev.schedules.map((e) => ({ e, st: state(e) })).sort((a, b) => a.st[1] - b.st[1] || a.e.end_date - b.e.end_date);
      const now2 = d.real_time;
      const bans = lineup().banners.map((b) => {
        const st = b.end_date < now2 ? ["ended", 2] : b.start_date > now2 ? [`starts ${short(b.start_date)}`, 1] : [`on now · ${left(b.end_date)}`, 0];
        return `<tr class="${st[1] === 2 ? "faint" : ""}">
          <td><span class="sch-type"><i style="background:${COLORS.banners}"></i>${esc(kindOfLabel(labelOf("banners", b.gacha_id)))}</span></td>
          <td><b class="linkish" data-item="banners:${b.gacha_id}">${esc(shortLabel({ label: labelOf("banners", b.gacha_id) }))}</b></td>
          <td class="faint">${short(b.start_date)} → ${short(b.end_date)}</td>
          <td><span class="sch-state s${st[1]}">${esc(st[0])}</span></td>
          <td style="text-align:right"><button class="btn sm ghost" data-unban="${b.gacha_id}" title="Remove from your lineup">${icon("close")} Remove</button></td></tr>`;
      }).join("");
      const running = rows.filter((r) => r.st[1] === 0).length, ended = rows.filter((r) => r.st[1] === 2).length;
      const table = rows.map(({ e, st }) => {
        const live = ev.published.some((p) => p.family === e.family && p.event_id === e.event_id);
        return `<tr class="${st[1] === 2 ? "faint" : ""}">
          <td><span class="sch-type"><i style="background:${COLORS[e.family] || "var(--accent)"}"></i>${esc(FAMILY[e.family] || e.family)}</span></td>
          <td><b>${esc(labelOf(e.family, e.event_id).replace(/^Legend Race: (vs )?/, ""))}</b></td>
          <td class="faint">${short(e.start_date)} → ${short(e.end_date)}</td>
          <td><span class="sch-state s${st[1]}">${esc(st[0])}</span>${live ? "" : ` <span class="tag" style="--c:var(--warn)" title="Saved, but the game doesn't have it yet">not published</span>`}</td>
          <td style="text-align:right"><button class="btn sm ghost" data-stop="${e.family}" title="Stop this rerun">${icon("close")} Stop</button></td></tr>`;
      }).join("");
      const summary = `<span class="grow">Current events <span class="faint" style="font-weight:400">·
          ${d.published ? `playing ${esc(d.published.day)}` : "today's real schedule"}
          ${rows.length ? ` · ${running} rerun${running === 1 ? "" : "s"} on now${ended ? `, ${ended} ended` : ""}` : " · no reruns"}${lineup().banners.length ? ` · ${lineup().banners.length} picked banner${lineup().banners.length === 1 ? "" : "s"}${lineup().only ? " (only these)" : ""}` : ""}</span></span>`;
      return `<details class="sch-help" ${win.state.helpSeen ? "" : "open"}><summary>How this works</summary>
          <ol><li><b>Pick a past day</b>: click the timeline, or use the date button above it. Below it you see what was running that day.</li>
            <li><b>Use this day in game</b> makes the game run those banners and events again, moving forward from that day.
              Untick a type to leave it on today's real schedule.</li>
            <li><b>Bring back one event</b>: click any bar or event name and run it on dates you choose. This is a rerun, and it
              replaces every other event of its type until you stop it.</li>
            <li>Restart the game to see a change.</li></ol></details>
        <details class="sch-cur" ${win.state.curOpen ? "open" : ""}><summary>${summary}</summary>
          <div class="sch-curbody">
            <div class="faint" style="font-size:12px">${synced}${pending}</div>
            <div class="sch-bansec">
              <div class="row" style="gap:10px;flex-wrap:wrap"><b class="grow">Your banners</b>
                <label class="sch-seg"><input type="radio" name="banmode" data-banmode="0" ${lineup().only ? "" : "checked"}><span>Add to the normal ones</span></label>
                <label class="sch-seg"><input type="radio" name="banmode" data-banmode="1" ${lineup().only ? "checked" : ""}><span>Only my banners</span></label></div>
              ${bans ? `<table class="sch-curtab"><thead><tr><th>Type</th><th>Banner</th><th>Dates</th><th>Status</th><th></th></tr></thead><tbody>${bans}</tbody></table>`
                : `<div class="faint" style="font-size:12px">None picked. Click any banner on the timeline and choose “Run this banner” to set its dates.</div>`}
            </div>
            ${rows.length ? `<div class="faint" style="font-size:12px">Reruns: each one replaces every other event of its type while it's set.</div>
              <table class="sch-curtab"><thead><tr><th>Type</th><th>Event</th><th>Dates</th><th>Status</th><th></th></tr></thead>
              <tbody>${table}</tbody></table>` : `<div class="faint">No reruns. Click an event on the timeline to bring one back.</div>`}
          </div></details>`;
    }

    function calendar() {
      const m = new Date(month * 1000), y = m.getUTCFullYear(), mo = m.getUTCMonth();
      const first = Date.UTC(y, mo, 1) / 1000, days = new Date(Date.UTC(y, mo + 1, 0)).getUTCDate();
      const lead = (new Date(first * 1000).getUTCDay() + 6) % 7;
      const starts = new Map();
      for (const i of items()) {
        const k = dayStart(i.start);
        if (k >= first && k < first + days * DAY) (starts.get(k) || starts.set(k, []).get(k)).push(i);
      }
      const y0 = new Date(firstDay() * 1000).getUTCFullYear(), y1 = new Date(today() * 1000).getUTCFullYear() + 1;
      let cells = "";
      for (let i = 0; i < lead; i++) cells += `<span></span>`;
      for (let n = 0; n < days; n++) {
        const t = first + n * DAY, here = starts.get(t) || [];
        const groups = [...new Set(here.map((i) => i.group))];
        const cls = [t === picked ? "on" : "", t === today() ? "today" : "", t > lastDay() || t < firstDay() ? "off" : ""].join(" ");
        cells += `<button class="sch-day ${cls}" data-day="${t}" title="${esc(here.map((i) => `${i.label} starts`).join("\n"))}">
          <span>${n + 1}</span><span class="sch-dots">${groups.map((g) => `<i style="background:${COLORS[g]}"></i>`).join("")}</span></button>`;
      }
      return `<div class="sch-cal">
        <div class="row" style="gap:6px">
          <button class="btn sm ghost" data-month="-1" title="Previous month">‹</button>
          <select data-mon>${MONTHS.map((n, i) => `<option value="${i}" ${i === mo ? "selected" : ""}>${n}</option>`).join("")}</select>
          <select data-year>${Array.from({ length: y1 - y0 + 1 }, (_, i) => y0 + i).map((v) => `<option ${v === y ? "selected" : ""}>${v}</option>`).join("")}</select>
          <button class="btn sm ghost" data-month="1" title="Next month">›</button>
          <span class="grow"></span>
          <button class="btn sm ghost" data-jump="yesterday">Yesterday</button>
        </div>
        <div class="sch-grid">${["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"].map((w) => `<b>${w}</b>`).join("")}${cells}</div>
        <div class="faint" style="font-size:11px">Dots mark events starting that day. Days from today on can be looked at, not played.</div>
      </div>`;
    }

    const KIND = { story_event: "Story event", legend_race: "Legend Race", training_challenge: "Trainer Aptitude Test",
      factor_research: "Tachyon research", transfer: "Transfer event", home: "Home screen look", shops: "Event shop" };
    const kindOf = (i) => i.group === "banners" ? (i.label.startsWith("Uma") ? "Uma banner" : "Support banner") : KIND[i.group] || i.group;
    const kindOfLabel = (label) => label.startsWith("Uma") ? "Uma banner" : "Support banner";
    const nameOf = (i) => i.label.replace(/^(Uma|Support) banner: /, "").replace(/^Legend Race: (vs )?/, "");
    function dayPanel() {
      const t = picked + (d.real_time - today());
      const live = liveAt(t);
      const playable = picked >= firstDay() && picked <= lastDay();
      const all = bring.size === Object.keys(d.all_groups).length;
      const lines = [];
      const runs = ev.schedules.filter((e) => e.end_date >= d.real_time);
      for (const g of Object.keys(d.all_groups)) {
        if (!bring.has(g)) continue;
        for (const i of live.filter((x) => x.group === g)) lines.push(`<div class="sch-get" data-item="${g}:${i.key}">
            <i style="background:${COLORS[g]}"></i>
            <span class="grow"><b>${esc(nameOf(i))}</b><span class="faint"> · ${esc(kindOf(i))} · until ${short(i.end)}</span></span></div>`);
      }
      const note = !runs.length ? "" : `<div class="sch-note">You also have ${runs.length} rerun${runs.length === 1 ? "" : "s"} on
          (${runs.map((e) => esc(FAMILY[e.family] || e.family)).join(", ")}). Switching to this day stops them.</div>`;
      const chips = Object.entries(d.all_groups).map(([g, label]) => `<label class="sch-typechip ${bring.has(g) ? "on" : ""}">
          <input type="checkbox" data-bring="${g}" ${bring.has(g) ? "checked" : ""}><i style="background:${COLORS[g]}"></i>${esc(label)}</label>`).join("");
      return `<div class="ed-sec sch-drawer sch-dayp" style="--c:var(--accent)">
        <div class="row"><b class="grow" style="font-size:15px">${long(picked)}</b>
          <button class="btn sm ghost" data-close-day title="Close">${icon("close")}</button></div><div>
          ${playable ? "" : `<span class="faint"> · ${picked > lastDay() ? "today or later: this is already the live schedule" : "before the game launched"}</span>`}</div>
        <div class="faint" style="font-size:12px">${playable ? "Running on this day:" : "Scheduled on this day:"}</div>
        <div class="sch-gets">${lines.join("") || `<div class="faint">Nothing.</div>`}</div>
        ${note}
        <details class="sch-types" ${all ? "" : "open"}><summary>${all ? "Only some types…" : `Only ${bring.size} type${bring.size === 1 ? "" : "s"} from this day (the rest stay as today)`}</summary>
          <div class="sch-typechips">${chips}</div></details>
        <button class="btn primary" data-play ${playable && bring.size ? "" : "disabled"}>${icon("play")} Switch the game to ${short(picked)}</button>
        <button class="btn sm ghost" data-match ${d.published || d.sync ? "" : "disabled"}>${icon("undo")} Back to today's real schedule</button>
      </div>`;
    }

    const PX = { 14: 64, 42: 26, 91: 12 };
    const shortLabel = (i) => i.label.replace(/^(Uma|Support) banner: /, "").replace(/^Legend Race: (vs )?/, "");
    const SPILL = 240;
    const textPx = (str) => Math.min(SPILL, str.length * 6.4 + 18);
    function range() {
      const all = items();
      const lo = Math.min(...all.map((i) => i.start)), hi = Math.max(today() + 60 * DAY, ...all.map((i) => i.end));
      return [dayStart(lo) - DAY, dayStart(hi) + 8 * DAY];
    }
    function timeline() {
      const [from, to] = range(), px = PX[span] || 26;
      const x = (t) => Math.round(((t - from) / DAY) * px);
      const width = x(to);
      const ticks = [];
      for (let t = from; t <= to; t += DAY) {
        const dt = new Date(t * 1000), dom = dt.getUTCDate();
        const show = px >= 50 ? true : px >= 20 ? dt.getUTCDay() === 1 : dom === 1 || dom === 15;
        if (show) ticks.push(`<span class="${dom === 1 ? "mon" : ""}" style="left:${x(t)}px">${dom === 1 || px < 50 ? short(t) : dom}</span>`);
      }
      const nowT = d.real_time;
      const marks = `<div class="sch-mark picked" style="left:${x(picked) + px / 2}px" title="Picked day">${picked === today() ? "" : `<span>${short(picked)}</span>`}</div>
        <div class="sch-mark now" style="left:${x(nowT)}px" title="Now"><span>Today</span></div>`;
      const names = [], tracks = [];
      const lanes = Object.entries(d.all_groups).flatMap(([g, label]) => g === "banners"
        ? [[g, "Uma banners", (i) => i.label.startsWith("Uma"), COLORS.banners, "Trainee gacha: which umas you can pull"],
           [g, "Support banners", (i) => !i.label.startsWith("Uma"), "#7fd0d0", "Support card gacha: which cards you can pull"]]
        : [[g, label, () => true, COLORS[g], HELP[g] || ""]]);
      for (const [g, label, keep, color, help] of lanes) {
        const here = items().filter((i) => i.group === g && keep(i)).sort((a, b) => a.start - b.start);
        const rows = [];
        for (const i of here) {
          const left = x(i.start), w = Math.max(6, x(i.end) - left), reach = left + Math.max(w, textPx(shortLabel(i))) + 6;
          const r = rows.find((row) => row.end <= left) || (rows.push({ end: 0, bars: [] }), rows.at(-1));
          r.end = reach;
          r.bars.push({ i, left, w });
        }
        const over = rerun(g);
        const mine = g === "banners" ? lineup().banners.filter((b) => here.some((i) => i.key === b.gacha_id)
          || keep({ label: labelOf(g, b.gacha_id) })) : [];
        const bars = rows.map((row, n) => row.bars.map(({ i, left, w }) => {
          const masked = (over && over.event_id !== i.key) || (g === "banners" && lineup().only && !picks(i.key));
          const sel = focusItem && focusItem.group === g && focusItem.key === i.key;
          const fits = textPx(shortLabel(i)) <= w;
          return `<button class="sch-bar ${masked ? "masked" : ""} ${sel ? "on" : ""} ${fits ? "" : "spill"}" data-item="${g}:${i.key}"
            data-s="${left}" data-e="${left + w}" data-c="${color}"
            style="left:${left}px;width:${fits ? w : textPx(shortLabel(i))}px;--w:${w}px;top:${n * 24}px;--c:${color}"
            title="${esc(`${i.label}\n${long(i.start)} → ${long(i.end)}${i.ported ? "\nported event" : ""}${masked ? "\nhidden by your rerun" : ""}`)}"><span>${esc(shortLabel(i))}</span></button>`;
        }).join("")).join("");
        let rr = "";
        if (over) {
          const left = x(over.start_date), w = Math.max(6, x(over.end_date) - left);
          rr = `<div class="sch-bar rerun" data-item="${g}:${over.event_id}" style="left:${left}px;width:${w}px;top:${rows.length * 24}px;--c:${COLORS[g]}"
            title="Your rerun (real dates)"><span>${icon("restart")} ${esc(labelOf(g, over.event_id).replace(/^Legend Race: /, ""))}</span></div>`;
        }
        const prow = [];
        for (const b of mine) {
          const left = x(b.start_date), w = Math.max(6, x(b.end_date) - left);
          let n = prow.findIndex((end) => end <= left);
          if (n < 0) { prow.push(0); n = prow.length - 1; }
          prow[n] = left + Math.max(w, textPx(shortLabel({ label: labelOf(g, b.gacha_id) }))) + 24;
          const sel = focusItem && focusItem.group === g && focusItem.key === b.gacha_id;
          rr += `<button class="sch-bar rerun ${sel ? "on" : ""}" data-item="${g}:${b.gacha_id}" data-s="${left}" data-e="${left + w}" data-c="${color}"
            style="left:${left}px;width:${w}px;top:${(rows.length + n) * 24}px;--c:${color}"
            title="${esc(`Your banner: ${labelOf(g, b.gacha_id)}\n${long(b.start_date)} → ${long(b.end_date)}`)}"><span>${icon("restart")} ${esc(shortLabel({ label: labelOf(g, b.gacha_id) }))}</span></button>`;
        }
        const h = Math.max(1, rows.length + prow.length + (over ? 1 : 0)) * 24;
        names.push(`<div class="sch-lname" style="height:${h}px" title="${esc(help)}"><i style="background:${color}"></i>${esc(label)}</div>`);
        tracks.push(`<div class="sch-track" style="height:${h}px">${bars}${rr}</div>`);
      }
      return `<div class="ed-sec sch-tlsec">
        <div class="row sch-tlhead" style="gap:6px;flex-wrap:wrap">
          <b class="grow">Timeline <span class="faint" style="font-weight:400">when each event originally ran</span></b>
          <div class="sch-calwrap"><button class="btn sm" data-cal>${icon("clock")} ${long(picked)}</button>
            ${calOpen ? `<div class="sch-pop">${calendar()}</div>` : ""}</div>
          <button class="btn sm ghost" data-pan="-1" title="Earlier">‹</button>
          <button class="btn sm ghost" data-pan="0" title="Scroll to the picked day">Picked</button>
          <button class="btn sm ghost" data-pan="today" title="Scroll to today">Today</button>
          <button class="btn sm ghost" data-pan="1" title="Later">›</button>
          <select data-span style="width:auto;flex:none" title="Zoom">${RANGES.map(([v, t]) => `<option value="${v}" ${v === span ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        <div class="sch-tl">
          ${focusItem ? `<div class="sch-float">${drawer()}</div>` : dayOpen ? `<div class="sch-float">${dayPanel()}</div>` : ""}
          <div class="sch-names"><div class="sch-lname"></div>${names.join("")}</div>
          <div class="sch-scroll" data-from="${from}" data-px="${px}">
            <div class="sch-canvas" style="width:${width}px">
              <div class="sch-ticks">${ticks.join("")}</div>
              <div class="sch-guide" data-g="s"></div><div class="sch-guide" data-g="e"></div>
              ${tracks.join("")}
              ${marks}
            </div></div>
        </div>
        <div class="faint" style="font-size:11px">Drag or scroll sideways to move. Click a bar to open that event, or empty space to pick that day.
          Gold line = picked day, red line = today. Faded = hidden by your rerun, outlined with ↺ = your rerun.</div>
      </div>`;
    }
    function guides() {
      const bar = body.querySelector(".sch-bar.on[data-s]");
      for (const g of body.querySelectorAll(".sch-guide")) {
        g.hidden = !bar;
        if (bar) {
          g.style.left = bar.dataset[g.dataset.g] + "px"; g.style.setProperty("--c", bar.dataset.c);
          g.style.bottom = "auto"; g.style.height = bar.getBoundingClientRect().bottom - g.parentElement.getBoundingClientRect().top + "px";
        }
      }
    }

    let tlScroll = null, scrollTo = "picked";
    function placeScroll() {
      const sc = body.querySelector(".sch-scroll");
      if (!sc) return;
      const from = +sc.dataset.from, px = +sc.dataset.px;
      const at = (t) => ((t - from) / DAY) * px - sc.clientWidth / 2;
      if (scrollTo === "picked") sc.scrollLeft = at(picked);
      else if (scrollTo === "today") sc.scrollLeft = at(d.real_time);
      else if (tlScroll != null) sc.scrollLeft = tlScroll;
      scrollTo = null;
      sc.addEventListener("scroll", () => { tlScroll = sc.scrollLeft; }, { passive: true });
    }

    function drawer() {
      if (!focusItem) return "";
      const i = items().find((x) => x.group === focusItem.group && x.key === focusItem.key);
      if (!i) return "";
      const g = i.group, days = Math.max(1, Math.round((i.end - i.start) / DAY));
      const over = rerun(g);
      const canRerun = isRerunFamily(g);
      return `<div class="ed-sec sch-drawer" style="--c:${COLORS[g]}">
        <div class="row"><i class="sch-sw" style="background:${COLORS[g]}"></i><b class="grow">${esc(i.label)}</b>
          <span class="faint">${esc(d.all_groups[g] || g)}${i.ported ? " · ported" : ""}</span>
          <button class="btn sm ghost" data-close-item>${icon("close")}</button></div>
        <div class="faint">Stock window ${long(i.start)} → ${long(i.end)} (${days} day${days === 1 ? "" : "s"})
          ${over ? over.event_id === i.key ? " · <b>this is your rerun</b>" : ` · hidden while your ${esc(d.all_groups[g])} rerun runs` : ""}</div>
        ${canRerun ? `<div class="row" style="flex-wrap:wrap;gap:8px">
            <label class="faint">Start <input type="datetime-local" data-r-start value="${localInput(over?.event_id === i.key ? over.start_date : d.real_time)}" title="Your local time"></label>
            <label class="faint">Days <input type="number" data-r-days min="1" max="90" value="${over?.event_id === i.key ? Math.round((over.end_date - over.start_date) / DAY) : days}" style="width:60px"></label>
            <button class="btn primary sm" data-rerun="${g}:${i.key}">${icon("play")} ${over?.event_id === i.key ? "Update rerun" : over ? "Bring this back instead" : "Bring this event back"}</button>
            ${over?.event_id === i.key ? `<button class="btn sm" data-stop="${g}">${icon("trash")} Stop rerun</button>` : ""}
          </div>` : ""}
        ${g === "banners" ? (() => { const p = picks(i.key);
          return `<div class="row" style="flex-wrap:wrap;gap:8px">
            <label class="faint">Start <input type="datetime-local" data-r-start value="${localInput(p ? p.start_date : d.real_time)}" title="Your local time"></label>
            <label class="faint">Days <input type="number" data-r-days min="1" max="365" value="${p ? Math.round((p.end_date - p.start_date) / DAY) : days}" style="width:64px"></label>
            <button class="btn primary sm" data-pickban="${i.key}">${icon("play")} ${p ? "Update dates" : "Run this banner"}</button>
            ${p ? `<button class="btn sm" data-unban="${i.key}">${icon("trash")} Remove</button>` : ""}
          </div>
          ${p ? `<div class="faint" style="font-size:12px">In your lineup: ${long(p.start_date)} → ${long(p.end_date)}</div>` : ""}`; })() : ""}
        <div class="row" style="flex-wrap:wrap;gap:8px">
          ${i.start < today() ? `<button class="btn sm" data-only="${g}:${i.start}">${icon("clock")} Use only ${esc(d.all_groups[g] || g)} from ${short(i.start)}</button>` : ""}
          <button class="btn sm ghost" data-pick="${dayStart(i.start)}">Show its first day</button>
        </div>
        <div class="faint" style="font-size:11px">${g === "banners" ? `Runs this banner on the dates above, next to the others${lineup().only ? " (only your picked banners run)" : ""}. Pick which banners run in Current events.`
          : canRerun ? "Bringing it back runs just this event on the dates above, in place of every other " + esc(d.all_groups[g]) + "."
          : "This type has no rerun of its own: use the day it ran (the button above) to bring it over."}</div>
      </div>`;
    }

    function render() {
      const keep = body.querySelector(".pane-scroll")?.scrollTop;
      body.innerHTML = `<div class="pane-top">${status()}</div>
        <div class="pane-scroll">
          ${timeline()}
          ${coming()}
        </div>
        <div class="pane-foot faint">Pack ${esc(d.digest ? d.digest.slice(0, 12) : "none")} · ${d.enabled ? "private master on" : "private master off"} ·
          restart the game (server_events=true in redirect.cfg) to load a change. Server time is never changed;
          Champions Meeting, fan raids and collect events keep their stock dates. Days are UTC.</div>`;
      if (keep) body.querySelector(".pane-scroll").scrollTop = keep;
      placeScroll();
      guides();
    }

    function coming() {
      const up = d.coming.filter((i) => isRerunFamily(i.group));
      if (!d.coming.length) return "";
      return `<div class="ed-sec"><b>Coming to ${store.region === "jp" ? "JP" : "Global"}</b>
        <div class="faint" style="font-size:12px">In your game files with a start date still ahead. ${up.length ? "Click one to start it now as a rerun." : ""}</div>
        ${d.coming.map((i) => `<div class="sch-li" data-item="${i.group}:${i.key}"><i class="sch-sw" style="background:${COLORS[i.group]}"></i>
          ${esc(i.label)} <span class="faint">· ${esc(d.all_groups[i.group] || i.group)} · official ${short(i.start)} → ${short(i.end)}</span></div>`).join("")}</div>`;
    }

    async function play(btn, day, groups) {
      const all = groups.length === Object.keys(d.all_groups).length;
      const clash = ev.schedules.filter((e) => groups.includes(e.family) && e.end_date >= d.real_time);
      const list = clash.length ? `<br><br><b style="color:var(--warn)">This stops your reruns:</b><ul>${clash.map((e) =>
        `<li>${esc(FAMILY[e.family] || e.family)}: ${esc(labelOf(e.family, e.event_id))}</li>`).join("")}</ul>
        To keep one, untick its type under “Only some types…” first.` : "";
      if (!await ask(win, { title: `Switch the game to ${day}?`, ok: clash.length ? "Switch and stop reruns" : "Switch", danger: !!clash.length,
        text: `${all ? "Every event type" : groups.map((g) => esc(d.all_groups[g])).join(", ")} will run as on ${esc(day)}, moving forward from there.
          ${!all ? "Other types keep today's real schedule." : ""}${list}` })) return;
      await act(btn, "Schedule", () => call("sync.set", { day, groups: all ? null : groups, stop_families: clash.map((e) => e.family) }, target()),
        `Playing ${day}. Restart the game to load it.`);
      load();
    }

    async function saveLineup(btn, value) {
      if (value.only && !value.banners.some((b) => b.end_date > Date.now() / 1000)
        && !await ask(win, { title: "No banners would be open", danger: true, ok: "Save anyway",
          text: "“Only my banners” is on but none of your picked banners is running now or later, so only the standard banners will show." })) return;
      await act(btn, "Banners", () => call("sync.banners", value, target()), "Published. Restart the game to load it.");
      load();
    }

    async function doRerun(btn, g, key) {
      const sec = btn.closest(".sch-drawer");
      const start = Math.floor(new Date(sec.querySelector("[data-r-start]").value).getTime() / 1000);
      const days = Math.max(1, parseInt(sec.querySelector("[data-r-days]").value, 10) || 14);
      if (!start) return toast("Pick a start time");
      const end = start + days * DAY, now = Math.floor(Date.now() / 1000);
      const over = rerun(g);
      let text = `${esc(labelOf(g, key))} runs ${esc(dateTime(start))} → ${esc(dateTime(end))}.`;
      let danger = false;
      if (end <= now) {
        danger = true;
        text = `<b style="color:var(--error)">This window has already ended</b> (${esc(dateTime(end))}). The event won't appear in game at all, and every other ${esc(d.all_groups[g])} is hidden while this rerun is set.<br><br>${text}`;
      } else if (start < now - 3600) {
        danger = true;
        const gone = Math.floor((now - start) / DAY);
        text = `<b style="color:var(--warn)">This start date is in the past.</b> Players join mid-way: ${gone ? `${gone} day${gone === 1 ? "" : "s"}` : "part"} of the event
          ${gone ? "are" : "is"} already over, and missions or episodes dated in that part unlock at once.<br><br>${text}`;
      }
      if (over && over.event_id !== key) text += `<br><br>It replaces your rerun of ${esc(labelOf(g, over.event_id))}.`;
      if (d.published && (!d.published.groups || d.published.groups.includes(g))) text += `<br><br>It wins over the synced day for ${esc(d.all_groups[g])}.`;
      if (!await ask(win, { title: danger ? "Rerun with a past date?" : "Start this rerun?", text, ok: danger ? "Rerun anyway" : "Rerun", danger })) return;
      await act(btn, "Schedule", () => call("events.set", { family: g, event_id: key, start_date: start, end_date: end }, target()),
        "Published. Restart the game to load it.");
      load();
    }

    body.addEventListener("change", (e) => {
      const m = e.target.closest?.("[data-banmode]");
      if (m) saveLineup(null, { only: m.dataset.banmode === "1", banners: lineup().banners });
    });
    body.addEventListener("toggle", (e) => {
      if (e.target.matches?.(".sch-help")) { win.state.helpSeen = !e.target.open; win.save?.(); }
      if (e.target.matches?.(".sch-cur")) { win.state.curOpen = e.target.open; win.save?.(); }
    }, true);
    body.addEventListener("change", (e) => {
      const t = e.target;
      if (t.matches("[data-bring]")) { t.checked ? bring.add(t.dataset.bring) : bring.delete(t.dataset.bring); render(); body.querySelector(".sch-types")?.setAttribute("open", ""); }
      else if (t.matches("[data-mon], [data-year]")) {
        month = Date.UTC(+body.querySelector("[data-year]").value, +body.querySelector("[data-mon]").value, 1) / 1000; render();
      } else if (t.matches("[data-span]")) { span = +t.value; scrollTo = "picked"; render(); }
    });

    body.addEventListener("click", async (e) => {
      const t = e.target;
      if (calOpen && !t.closest(".sch-calwrap")) { calOpen = false; render(); return; }
      const btn = t.closest("button, [data-item]");
      if (!btn) return;
      if (btn.matches("[data-cal]")) { calOpen = !calOpen; month = picked; render(); }
      else if (btn.matches("[data-day]")) { picked = +btn.dataset.day; focusItem = null; dayOpen = true; calOpen = false; scrollTo = "picked"; render(); }
      else if (btn.matches("[data-month]")) {
        const m = new Date(month * 1000);
        month = Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + +btn.dataset.month, 1) / 1000; render();
      } else if (btn.matches("[data-jump]")) { picked = lastDay(); month = picked; calOpen = false; scrollTo = "picked"; render(); }
      else if (btn.matches("[data-pan]")) {
        const v = btn.dataset.pan, sc = body.querySelector(".sch-scroll");
        if (v === "0" || v === "today") { scrollTo = v === "0" ? "picked" : "today"; placeScroll(); }
        else sc.scrollBy({ left: +v * sc.clientWidth * 0.7, behavior: "smooth" });
      } else if (btn.matches("[data-pick]")) { picked = +btn.dataset.pick; month = picked; focusItem = null; dayOpen = true; scrollTo = "picked"; render(); }
      else if (btn.matches("[data-bring-all]")) {
        bring = bring.size === Object.keys(d.all_groups).length ? new Set() : new Set(Object.keys(d.all_groups)); render();
      } else if (btn.matches("[data-item]")) {
        const [g, k] = btn.dataset.item.split(":");
        focusItem = { group: g, key: +k };
        render();
        body.querySelector(".sch-drawer")?.scrollIntoView({ block: "nearest" });
      } else if (btn.matches("[data-close-item]")) { focusItem = null; render(); }
      else if (btn.matches("[data-close-day]")) { dayOpen = false; render(); }
      else if (btn.matches("[data-play]")) play(btn, iso(picked), [...bring]);
      else if (btn.matches("[data-only]")) {
        const [g, start] = btn.dataset.only.split(":");
        const day = iso(Math.min(dayStart(+start), lastDay()));
        picked = fromIso(day); month = center = picked; bring = new Set([g]);
        render();
        play(body.querySelector("[data-play]"), day, [g]);
      } else if (btn.matches("[data-match]")) {
        if (!await ask(win, { title: "Match official?", text: "Undoes the day sync. Event reruns stay unless you stop them too.", ok: "Match official" })) return;
        await act(btn, "Schedule", () => call("sync.set", { day: null }, target()), "Back on the stock schedule. Restart the game to load it.");
        load();
      } else if (btn.matches("[data-rerun]")) {
        const [g, k] = btn.dataset.rerun.split(":");
        doRerun(btn, g, +k);
      } else if (btn.matches("[data-pickban]")) {
        const key = +btn.dataset.pickban, sec = btn.closest(".sch-drawer");
        const start = Math.floor(new Date(sec.querySelector("[data-r-start]").value).getTime() / 1000);
        const n = Math.max(1, parseInt(sec.querySelector("[data-r-days]").value, 10) || 7);
        if (!start) return toast("Pick a start time");
        const end = start + n * DAY, now = Math.floor(Date.now() / 1000);
        if (end <= now && !await ask(win, { title: "This window has already ended", danger: true, ok: "Save anyway",
          text: `${esc(labelOf("banners", key))} would run ${esc(dateTime(start))} → ${esc(dateTime(end))}, which is over, so it won't show in game.` })) return;
        const rest = lineup().banners.filter((b) => b.gacha_id !== key);
        saveLineup(btn, { only: lineup().only, banners: [...rest, { gacha_id: key, start_date: start, end_date: end }] });
      } else if (btn.matches("[data-unban]")) {
        const key = +btn.dataset.unban;
        saveLineup(btn, { only: lineup().only, banners: lineup().banners.filter((b) => b.gacha_id !== key) });
      } else if (btn.matches("[data-stop]")) {
        const g = btn.dataset.stop;
        if (!await ask(win, { title: "Stop this rerun?", text: "The event disappears after the next game restart. Saved progress is kept.", ok: "Stop rerun", danger: true })) return;
        await act(btn, "Schedule", () => call("events.set", { family: g }, target()), "Published. Restart the game to load it.");
        load();
      }
    });

    let drag = null;
    body.addEventListener("pointerdown", (e) => {
      const sc = e.target.closest(".sch-scroll");
      if (!sc || e.button !== 0 || e.target.closest(".sch-bar")) return;
      drag = { sc, x: e.clientX, left: sc.scrollLeft, moved: false };
      sc.setPointerCapture(e.pointerId);
      sc.classList.add("dragging");
    });
    body.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      if (Math.abs(dx) > 4) drag.moved = true;
      drag.sc.scrollLeft = drag.left - dx;
    });
    body.addEventListener("pointerup", (e) => {
      if (!drag) return;
      const { sc, moved } = drag;
      drag = null;
      sc.classList.remove("dragging");
      if (moved) return;
      const r = sc.getBoundingClientRect();
      const t = +sc.dataset.from + ((e.clientX - r.left + sc.scrollLeft) / +sc.dataset.px) * DAY;
      picked = dayStart(t); month = picked; focusItem = null; dayOpen = true; render();
    });
    body.addEventListener("wheel", (e) => {
      const sc = e.target.closest(".sch-scroll");
      if (!sc || e.ctrlKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      e.preventDefault();
      sc.scrollLeft += e.deltaY;
    }, { passive: false });

    win.refresh = load;
    const off = store.on((k) => { if (k === "target") { picked = month = center = bring = focusItem = null; scrollTo = "picked"; load(); } });
    body.innerHTML = empty("Loading…");
    load();
    return off;
  },
};
