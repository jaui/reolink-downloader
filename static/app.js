"use strict";

// ---------- Zustand (im URL-Hash) ----------
const DEFAULTS = { cam: "all", p: "1", v: "1", near: "1", light: "all", rv: "", q: "", from: "", to: "", at: "", ev: "" };
const S = { ...DEFAULTS };
let DATA = null;
let STATUS = null;
let BY_ID = new Map();
let renderedEv = null;

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CLS = { person: "Person", bicycle: "Fahrrad", car: "Auto", truck: "LKW/Transporter", bus: "Bus",
  motorcycle: "Motorrad", animal: "Tier", fp: "Fehlalarm" };
const ICON = { person: "🚶", vehicle: "🚗", other: "🐾", fp: "⊘" };
// Nachklassifikation: Klassen mit Tastenkürzel (im Detail)
const REVIEW = [["person", "p"], ["bicycle", ""], ["car", "a"], ["truck", "l"], ["bus", ""], ["motorcycle", ""],
  ["animal", "t"], ["fp", "f"]];

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  for (const k of Object.keys(DEFAULTS)) S[k] = h.has(k) ? h.get(k) : DEFAULTS[k];
}

function writeHash(push) {
  const h = new URLSearchParams();
  for (const k of Object.keys(DEFAULTS)) if (S[k] !== DEFAULTS[k]) h.set(k, S[k]);
  const s = "#" + h.toString();
  if (s === location.hash || (s === "#" && !location.hash)) return render();
  if (push) location.hash = s;               // -> hashchange -> render
  else { history.replaceState(null, "", s || "#"); render(); }
}

// ---------- Filter ----------
const normPlate = (s) => String(s).toUpperCase().replace(/[^A-Z0-9]/g, "");

function trackOk(t) {
  if (S.rv === "fp") return t.kind === "fp" || t.kind === "other";
  if (S.rv === "open" && t.review) return false;
  if (S.rv === "done" && t.review?.by !== "manual") return false;
  if (t.kind === "person") return S.p === "1";
  if (t.kind === "vehicle") return S.v === "1" && (S.near !== "1" || (t.near && t.moving) || !!t.plate);
  return false;
}

function queryOk(t) {
  if (!S.q) return true;
  if (!t.plate) return false;
  const qn = normPlate(S.q), ql = S.q.toLowerCase();
  const m = t.plate.match;
  return (qn && (t.plate.text.includes(qn) || (m && normPlate(m.plate).includes(qn))))
      || (m && m.label.toLowerCase().includes(ql));
}

/** Höchstzahl gleichzeitig sichtbarer Tracks. Eine kurz verdeckte Person bekommt vom Tracker
 *  eine neue Track-ID, daher ist die Track-Anzahl nur eine Obergrenze. */
function maxSimultaneous(tracks) {
  const pts = [];
  for (const t of tracks) { pts.push([t.t_first, 1], [t.t_last, -1]); }
  pts.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let c = 0, m = 0;
  for (const [, d] of pts) { c += d; m = Math.max(m, c); }
  return m;
}

/** "2" oder "1–3" (gleichzeitig – Tracks) */
const pplText = (x) => (x.ppl === x.np ? `${x.np}` : `${x.ppl}–${x.np}`);
const pplTitle = (x) => (x.ppl === x.np ? `${x.np} Person${x.np > 1 ? "en" : ""}`
  : `${x.ppl} gleichzeitig im Bild, ${x.np} Spuren (eine Person kann nach Verdeckung eine neue Spur bekommen)`);

function view(e) {
  if (S.cam !== "all" && e.camera !== S.cam) return null;
  if (S.light === "day" && e.night) return null;
  if (S.light === "night" && !e.night) return null;
  if (S.from && e.date < S.from) return null;
  if (S.to && e.date > S.to) return null;
  const tracks = e.tracks.filter((t) => trackOk(t) && queryOk(t));
  if (!tracks.length) return null;
  const persons = tracks.filter((t) => t.kind === "person");
  const np = persons.length;
  const ppl = maxSimultaneous(persons);   // mindestens so viele Personen (gleichzeitig im Bild)
  const nv = tracks.filter((t) => t.kind === "vehicle").length;
  const nx = tracks.length - np - nv;
  const plates = tracks.filter((t) => t.plate);
  const hits = plates.filter((t) => t.plate.match);
  const lead = hits[0] || plates[0]
    || [...tracks].sort((a, b) => (b.kind === "vehicle") - (a.kind === "vehicle") || b.area - a.area)[0];
  return { e, tracks, np, ppl, nv, nx, plates, hits, lead, thumb: lead.plate_crop || lead.crop };
}

function filtered() {
  if (!DATA) return [];
  return DATA.events.map(view).filter(Boolean);
}

// ---------- Hilfen ----------
function fmtDate(d, opts = { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" }) {
  return new Date(d + "T00:00:00").toLocaleDateString("de-DE", opts);
}
const hourOf = (v) => parseInt(v.e.time.slice(0, 2), 10);

function plateBadge(t, full) {
  const p = t.plate;
  if (!p) return "";
  if (p.match) {
    const weak = p.match.type !== "exakt";
    return `<span class="badge plate hit${weak ? " weak" : ""}" title="${esc(p.match.type)}: gelesen ${esc(p.text)}">${esc(p.match.plate)}${full ? " · " + esc(p.match.label) : ""}</span>`;
  }
  return `<span class="badge plate" title="Lesung, ${p.n_reads}/${p.n_total} Frames">${esc(p.text)}</span>`;
}

const pad = (n) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
const monthName = (ym) => new Date(ym + "-01T00:00:00").toLocaleDateString("de-DE", { month: "long" });
const daysIn = (ym) => new Date(+ym.slice(0, 4), +ym.slice(5, 7), 0).getDate();
function today() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

/** Drilldown-Position aus S.at: all | JJJJ | JJJJ-MM | JJJJ-MM-TT | JJJJ-MM-TTTHH; leer = heute. */
function pos() {
  const a = S.at || today();
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})$/.exec(a);
  if (m) return { lvl: "hour", y: m[1], m: a.slice(0, 7), d: a.slice(0, 10), h: +m[4] };
  if (/^\d{4}-\d{2}-\d{2}$/.test(a)) return { lvl: "day", y: a.slice(0, 4), m: a.slice(0, 7), d: a };
  if (/^\d{4}-\d{2}$/.test(a)) return { lvl: "month", y: a.slice(0, 4), m: a };
  if (/^\d{4}$/.test(a)) return { lvl: "year", y: a };
  return { lvl: "all" };
}
const hourAt = (d, h) => `${d}T${pad(h)}`;

function groupBy(views, keyOf) {
  const g = new Map();
  for (const x of views) {
    const k = keyOf(x);
    if (!g.has(k)) g.set(k, []);
    g.get(k).push(x);
  }
  return g;
}

/** Nachbarn in einer sortierbaren Schlüsselliste (auch wenn cur selbst fehlt). */
function neighbors(cur, keys) {
  const s = [...new Set(keys)].sort();
  return { prev: s.filter((k) => k < cur).pop() || "", next: s.find((k) => k > cur) || "" };
}

// ---------- Zeitstreifen (Stunden / Tage / Monate) ----------
function bins(views, n, binOf) {
  const p = new Array(n).fill(0), v = new Array(n).fill(0), x = new Array(n).fill(0);
  for (const w of views) {
    const b = binOf(w);
    if (b < 0 || b >= n) continue;
    if (w.np) p[b]++;
    if (w.nv) v[b]++;
    if (w.nx) x[b]++;
  }
  return { p, v, x };
}

function binMax(groups, n, binOf) {
  let m = 1;
  for (const vs of groups.values()) { const b = bins(vs, n, binOf); m = Math.max(m, ...b.p, ...b.v, ...b.x); }
  return m;
}

/** o: { label(i), show(i), title(i), at(i) -> Drilldown-Ziel, cur -> markiertes Fach } */
function strip(views, n, binOf, max, o) {
  const b = bins(views, n, binOf);
  const cell = (c, cls, i, what) => `<div class="h ${c ? cls : ""}${o.cur === i ? " cur" : ""}" style="--i:${c ? Math.max(0.25, c / max) : 0}"${
    o.at && c ? ` data-at="${o.at(i)}"` : ""} title="${esc(o.title(i))}: ${c} ${what}"></div>`;
  const row = (arr, cls, what) => `<div class="strip${o.at ? " click" : ""}" style="--n:${n}">${arr.map((c, i) => cell(c, cls, i, what)).join("")}</div>`;
  const rows = [];
  if (S.rv === "fp") rows.push(row(b.x, "x", "mit Fehlalarmen"));
  else {
    if (S.p === "1") rows.push(row(b.p, "p", "mit Personen"));
    if (S.v === "1") rows.push(row(b.v, "v", "mit Fahrzeugen"));
  }
  const axis = `<div class="strip-axis" style="--n:${n}">${[...Array(n).keys()].map((i) => `<span${o.show(i) ? ' class="on"' : ""}>${o.label(i)}</span>`).join("")}</div>`;
  return `<div class="strip-rows">${rows.join("")}${axis}</div>`;
}

const hourStrip = (d) => ({ label: (h) => h, show: (h) => h % 6 === 0, title: (h) => `${pad(h)} Uhr`, at: (h) => hourAt(d, h) });

function countsHtml(vs) {
  if (S.rv === "fp") return `<span class="cnt other" title="Ereignisse mit Fehlalarmen/Sonstigem">${ICON.fp} ${nf(vs.length)}</span>`;
  const np = vs.filter((x) => x.np).length, nv = vs.filter((x) => x.nv).length;
  return (S.p === "1" ? `<span class="cnt person" title="Ereignisse mit Personen">${ICON.person} ${nf(np)}</span>` : "")
    + (S.v === "1" ? `<span class="cnt vehicle" title="Ereignisse mit Fahrzeugen">${ICON.vehicle} ${nf(nv)}</span>` : "");
}

function hitsHtml(vs) {
  const labels = new Map();
  for (const x of vs) for (const t of x.hits) labels.set(t.plate.match.plate, t);
  return [...labels.values()].map((t) => plateBadge(t, true)).join("");
}

/** Zeile in Jahres-/Monats-/Tagesliste. */
function rowHtml(at, head, vs, stripHtml) {
  return `<a class="day${vs.length ? "" : " empty-day"}" href="#" data-at="${at}">
    <div class="day-date">${head}</div>
    ${stripHtml}
    <div class="day-counts">${countsHtml(vs)}</div>
    <div class="day-hits">${hitsHtml(vs)}</div>
    ${dayProgressHtml(at)}
  </a>`;
}

function levelHead(title, nav, vs, extra = "") {
  return `<section class="dayhead">
    <h2>${title}
      <span class="day-counts">${countsHtml(vs)}</span>
      <span class="daynav">
        <button type="button" data-at="${nav.prev}" ${nav.prev ? "" : "disabled"} title="${esc(nav.prevTitle)}">‹</button>
        <button type="button" data-at="${nav.next}" ${nav.next ? "" : "disabled"} title="${esc(nav.nextTitle)}">›</button>
      </span>
    </h2>
    ${extra}
  </section>`;
}

// ---------- Rendering ----------
function render() {
  syncFilterUI();
  renderCrumbs();
  if (!DATA) { $("#main").innerHTML = `<p class="empty">Lade Ereignisse …</p>`; return; }
  const views = filtered();
  renderMain(views);
  renderDetail(views);
}

function renderMain(views) {
  const p = pos();
  if (p.lvl === "day" || p.lvl === "hour") renderDay(views, p);
  else if (p.lvl === "month") renderDays(views, p);
  else renderPeriods(views, p);
}

function renderCrumbs() {
  const p = pos();
  const items = [["all", "Alle Jahre"]];
  if (p.y) items.push([p.y, p.y]);
  if (p.m) items.push([p.m, monthName(p.m)]);
  if (p.d) items.push([p.d, fmtDate(p.d, { weekday: "short", day: "2-digit", month: "2-digit" })]);
  if (p.lvl === "hour") items.push([hourAt(p.d, p.h), `${pad(p.h)}–${pad(p.h + 1)} Uhr`]);
  const html = items.map(([at, label], i) => i === items.length - 1
    ? `<span aria-current="page">${esc(label)}</span>` : `<a href="#" data-at="${at}">${esc(label)}</a>`);
  const isToday = p.d === today() && p.lvl === "day";
  $("#crumbs").innerHTML = `<span>›</span>` + html.join("<span>›</span>")
    + (isToday ? "" : ` <a href="#" data-at="" class="today">Heute</a>`);
}

// ---------- Download-/Erkennungsfortschritt ----------
const nf = (n, d = 0) => Number(n).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
const gb = (b) => nf(b / 1e9, 1) + " GB";
const pct = (a, b) => (b ? Math.min(100, (100 * a) / b) : 0);

function fmtDur(s) {
  if (s == null) return "";
  if (s < 90) return `${Math.round(s)} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  return h >= 48 ? `${Math.round(h / 24)} Tage` : `${h} h ${m % 60} min`;
}

const STATE = {
  "läuft": ["s-run", "läuft"], plan: ["s-run", "listet"], fertig: ["s-done", "✓ fertig"],
  steht: ["s-warn", "⚠ steht"], abgebrochen: ["", "■ abgebrochen"], fehler: ["s-crit", "✕ Fehler"],
};

/** Sub-Stream-Jobs der gewählten Kamera(s): Grundlage für den Fortschritt je Tag. */
function subJobs() {
  return (STATUS?.jobs || []).filter((j) => j.stream === "sub" && (S.cam === "all" || j.camera === S.cam));
}

/** Fortschritt für einen Zeitraum: Präfix "JJJJ", "JJJJ-MM" oder "JJJJ-MM-TT". */
function dayProgress(prefix) {
  let clips = 0, done = 0, det = 0, known = false;
  for (const j of subJobs()) {
    for (const [date, d] of Object.entries(j.days)) {
      if (!date.startsWith(prefix)) continue;
      known = true;
      clips += d.clips; done += d.done; det += d.detected || 0;
    }
  }
  return known ? { clips, done, det } : null;
}

/** Tage aus den Download-Jobs (auch ohne Ereignisse), damit der Zeitverlauf lückenlos ist. */
function jobDays() {
  const s = new Set();
  if (S.q) return s;   // bei Kennzeichensuche nur Zeiträume mit Treffern
  for (const j of subJobs()) {
    for (const d of Object.keys(j.days)) if (!((S.from && d < S.from) || (S.to && d > S.to))) s.add(d);
  }
  return s;
}

function dayProgressHtml(date) {
  const p = dayProgress(date);
  if (!p) return "";
  if (!p.clips) return `<div class="day-prog complete">keine Aufnahmen auf der Kamera</div>`;
  if (p.done === p.clips && p.det === p.done) {
    return `<div class="day-prog complete">✓ ${nf(p.clips)} Clips geladen und ausgewertet</div>`;
  }
  return `<div class="day-prog" title="${nf(p.done)} von ${nf(p.clips)} Clips geladen, ${nf(p.det)} ausgewertet">
    <span>geladen ${nf(pct(p.done, p.clips))} %</span><div class="pbar dl"><i style="width:${pct(p.done, p.clips)}%"></i></div>
    <span>erkannt ${nf(pct(p.det, p.clips))} %</span><div class="pbar det"><i style="width:${pct(p.det, p.clips)}%"></i></div>
  </div>`;
}

function progressPanel() {
  const jobs = (STATUS?.jobs || []).filter((j) => S.cam === "all" || j.camera === S.cam);
  if (!jobs.length) return "";
  return `<section class="progress" aria-label="Download-Fortschritt">${jobs.map((j) => {
    const t = j.totals;
    const [cls, label] = STATE[j.state] || ["", j.state];
    const detName = j.stream === "sub" ? "erkannt" : "Kennzeichen gelesen";
    const detector = STATUS.detectors?.[j.stream === "sub" ? j.camera : ""];
    const cur = j.current;
    const foot = [];
    if (j.state === "läuft" && j.rate_bps) foot.push(`${nf(j.rate_bps / 1e6, 1)} MB/s`);
    if (j.eta_s) foot.push(`noch ca. ${fmtDur(j.eta_s)}`);
    if (cur) foot.push(`<span class="file">${esc(cur.file)}</span> ${nf(pct(cur.received, cur.size))} %`);
    if (j.message) foot.push(esc(j.message));
    if (j.state !== "läuft" && j.state !== "plan") foot.push(`Stand vor ${fmtDur(j.age)}`);
    if (detector) foot.push(detector.age < 120 ? `Erkennung aktiv` : `Erkennung ruht seit ${fmtDur(detector.age)}`);
    return `<article class="pcard">
      <h3>${esc(j.camera)} <span class="sub">${j.stream === "sub" ? "Sub-Stream" : "Main-Stream"}${j.selection && j.selection !== "alle" ? " · " + esc(j.selection) : ""}</span>
        <span class="badge state ${cls}">${label}</span></h3>
      <div class="prow">
        <div class="lbl"><span>geladen ${nf(pct(t.done, t.clips))} %</span><span>${nf(t.done)} / ${nf(t.clips)} Clips · ${gb(t.done_bytes)} / ${gb(t.bytes)}</span></div>
        <div class="pbar dl" role="progressbar" aria-valuenow="${Math.round(pct(t.done, t.clips))}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct(t.done, t.clips)}%"></i></div>
      </div>
      <div class="prow">
        <div class="lbl"><span>${detName} ${nf(pct(j.detected, t.clips))} %</span><span>${nf(j.detected)} / ${nf(t.clips)} Clips</span></div>
        <div class="pbar det" role="progressbar" aria-valuenow="${Math.round(pct(j.detected, t.clips))}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct(j.detected, t.clips)}%"></i></div>
      </div>
      <div class="pfoot">${foot.map((x) => `<span>${x}</span>`).join("")}</div>
      <div class="pfoot">${esc(j.from)} – ${esc(j.to)}</div>
    </article>`;
  }).join("")}</section>`;
}

/** Ebene "Alle Jahre" (Zeilen = Jahre, Streifen = Monate) und "Jahr" (Zeilen = Monate, Streifen = Tage). */
function renderPeriods(views, p) {
  const isAll = p.lvl === "all";
  const keyOfDate = isAll ? (d) => d.slice(0, 4) : (d) => d.slice(0, 7);
  const inScope = (d) => isAll || d.startsWith(p.y + "-");
  const scoped = views.filter((x) => inScope(x.e.date));
  const groups = groupBy(scoped, (x) => keyOfDate(x.e.date));
  for (const d of jobDays()) if (inScope(d) && !groups.has(keyOfDate(d))) groups.set(keyOfDate(d), []);
  const keys = [...groups.keys()].sort().reverse();
  const n = isAll ? 12 : 31;
  const binOf = isAll ? (x) => +x.e.date.slice(5, 7) - 1 : (x) => +x.e.date.slice(8, 10) - 1;
  const max = binMax(groups, n, binOf);

  const rows = keys.map((k) => {
    const vs = groups.get(k);
    if (isAll) {
      const o = { label: (i) => MONTHS[i].slice(0, 1), show: () => true, title: (i) => `${MONTHS[i]} ${k}`, at: (i) => `${k}-${pad(i + 1)}` };
      return rowHtml(k, `${k}<small>${nf(vs.length)} Ereignisse</small>`, vs, strip(vs, 12, binOf, max, o));
    }
    const o = { label: (i) => i + 1, show: (i) => i % 7 === 0, title: (i) => fmtDate(`${k}-${pad(i + 1)}`), at: (i) => `${k}-${pad(i + 1)}` };
    return rowHtml(k, `${esc(monthName(k))}<small>${nf(vs.length)} Ereignisse</small>`, vs, strip(vs, daysIn(k), binOf, max, o));
  }).join("");

  let head = "";
  if (!isAll) {
    const years = [...views.map((x) => x.e.date.slice(0, 4)), ...[...jobDays()].map((d) => d.slice(0, 4))];
    const nav = { ...neighbors(p.y, years), prevTitle: "Vorheriges Jahr", nextTitle: "Nächstes Jahr" };
    head = levelHead(esc(p.y), nav, scoped);
  }
  $("#main").innerHTML = progressPanel() + head + (keys.length
    ? `<p class="summary">${nf(scoped.length)} Ereignisse${isAll ? "" : " in " + esc(p.y)}</p><div class="days">${rows}</div>`
    : `<p class="empty">Keine Ereignisse für diese Filter.</p>`);
}

/** Ebene "Monat": Zeilen = Tage, Streifen = Stunden. */
function renderDays(views, p) {
  const scoped = views.filter((x) => x.e.date.startsWith(p.m));
  const groups = groupBy(scoped, (x) => x.e.date);
  for (const d of jobDays()) if (d.startsWith(p.m) && !groups.has(d)) groups.set(d, []);
  const days = [...groups.keys()].sort().reverse();
  const max = binMax(groups, 24, hourOf);
  const rows = days.map((d) => rowHtml(d,
    `${esc(fmtDate(d, { day: "2-digit", month: "2-digit", year: "numeric" }))}<small>${esc(fmtDate(d, { weekday: "long" }))}</small>`,
    groups.get(d), strip(groups.get(d), 24, hourOf, max, hourStrip(d)))).join("");
  const months = [...views.map((x) => x.e.date.slice(0, 7)), ...[...jobDays()].map((d) => d.slice(0, 7))];
  const nav = { ...neighbors(p.m, months), prevTitle: "Vorheriger Monat", nextTitle: "Nächster Monat" };
  const withEvents = days.filter((d) => groups.get(d).length).length;
  $("#main").innerHTML = progressPanel()
    + levelHead(`${esc(monthName(p.m))} ${esc(p.y)}`, nav, scoped, dayProgressHtml(p.m))
    + (days.length
      ? `<p class="summary">${nf(scoped.length)} Ereignisse an ${withEvents} von ${days.length} Tagen</p><div class="days">${rows}</div>`
      : `<p class="empty">Keine Ereignisse in diesem Monat für diese Filter.</p>`);
}

function cardHtml(x, prevView) {
  const e = x.e;
  const cont = prevView && prevView.e.camera === e.camera
    && (Date.parse(e.start) - Date.parse(prevView.e.start)) / 1000 <= prevView.e.duration + 60;
  const counts = [
    x.np ? `<span class="cnt person" title="${esc(pplTitle(x))}">${ICON.person}${x.np > 1 ? " " + pplText(x) : ""}</span>` : "",
    x.nv ? `<span class="cnt vehicle">${ICON.vehicle}${x.nv > 1 ? " " + x.nv : ""}</span>` : "",
    x.nx ? `<span class="cnt other">${ICON.fp}${x.nx > 1 ? " " + x.nx : ""}</span>` : "",
    x.tracks.some((t) => t.review?.by === "manual") ? `<span class="badge rv" title="von Hand geprüft">✓</span>` : "",
  ].join("");
  const plates = x.plates.map((t) => plateBadge(t, false)).join("");
  return `<button type="button" class="card${cont ? " cont" : ""}${e.id === S.ev ? " active" : ""}" data-ev="${esc(e.id)}">
    <div class="thumb">${x.thumb ? `<img loading="lazy" src="${esc(x.thumb)}" alt="">` : ""}</div>
    <div class="meta">
      <div class="row"><span class="time">${esc(e.time)}</span>${counts}${e.night ? `<span class="badge night">Nacht</span>` : ""}</div>
      <div class="row"><span class="cam">${esc(e.camera)}${e.duration ? " · " + Math.round(e.duration) + " s" : ""}</span></div>
      ${plates ? `<div class="row">${plates}</div>` : ""}
    </div>
  </button>`;
}

function cardsGrid(vs) {
  let prev = null;
  return `<div class="grid">${vs.map((x) => { const h = cardHtml(x, prev); prev = x; return h; }).join("")}</div>`;
}

/** Ebenen "Tag" (Karten nach Stunden gruppiert) und "Stunde" (Karten einer Stunde). */
function renderDay(views, p) {
  const dayViews = views.filter((x) => x.e.date === p.d);
  const isHour = p.lvl === "hour";
  const vs = isHour ? dayViews.filter((x) => hourOf(x) === p.h) : dayViews;
  const st = strip(dayViews, 24, hourOf, binMax(new Map([[p.d, dayViews]]), 24, hourOf),
    { ...hourStrip(p.d), cur: isHour ? p.h : undefined });
  const longDate = esc(fmtDate(p.d, { weekday: "long", day: "2-digit", month: "long", year: "numeric" }));
  let nav, title, body;
  if (isHour) {
    nav = { ...neighbors(hourAt(p.d, p.h), views.map((x) => hourAt(x.e.date, hourOf(x)))),
      prevTitle: "Vorherige Stunde mit Ereignissen", nextTitle: "Nächste Stunde mit Ereignissen" };
    title = `${longDate} · ${pad(p.h)}–${pad(p.h + 1)} Uhr`;
    body = vs.length ? cardsGrid(vs) : `<p class="empty">Keine Ereignisse in dieser Stunde für diese Filter.</p>`;
  } else {
    nav = { ...neighbors(p.d, views.map((x) => x.e.date)), prevTitle: "Vorheriger Tag mit Ereignissen", nextTitle: "Nächster Tag mit Ereignissen" };
    title = longDate;
    const hours = groupBy(vs, hourOf);
    body = vs.length
      ? [...hours.keys()].sort((a, b) => a - b).map((h) => `<h3 class="hour-h"><a href="#" data-at="${hourAt(p.d, h)}">${pad(h)}–${pad(h + 1)} Uhr</a>
          <span class="day-counts">${countsHtml(hours.get(h))}</span></h3>${cardsGrid(hours.get(h))}`).join("")
      : `<p class="empty">Keine Ereignisse an diesem Tag für diese Filter.${p.d === today() ? " Heute ist noch nichts geladen bzw. ausgewertet." : ""}</p>`;
  }
  $("#main").innerHTML = levelHead(title, nav, vs, st + dayProgressHtml(p.d)) + body;
}

function renderDetail(views) {
  const panel = $("#detail");
  let backdrop = $(".backdrop");
  const e = S.ev ? BY_ID.get(S.ev) : null;
  if (!e) {
    panel.hidden = true; panel.innerHTML = ""; renderedEv = null;
    backdrop?.remove();
    document.body.style.overflow = "";
    return;
  }
  if (!backdrop) {
    backdrop = document.createElement("div");
    backdrop.className = "backdrop";
    backdrop.addEventListener("click", closeDetail);
    document.body.appendChild(backdrop);
  }
  const list = views.filter((x) => x.e.date === e.date);
  let idx = list.findIndex((x) => x.e.id === e.id);
  let prev, next;
  if (idx >= 0) {
    prev = idx > 0 ? list[idx - 1].e.id : "";
    next = idx < list.length - 1 ? list[idx + 1].e.id : "";
  } else {   // Ereignis fällt nach einer Korrektur aus dem Filter: Nachbarn nach Zeit
    prev = list.filter((x) => x.e.start < e.start).pop()?.e.id || "";
    next = list.find((x) => x.e.start > e.start)?.e.id || "";
  }
  if (renderedEv === e.id) {      // nur Navigation aktualisieren, Video nicht neu laden
    panel.querySelector("[data-go=prev]").dataset.target = prev;
    panel.querySelector("[data-go=prev]").disabled = !prev;
    panel.querySelector("[data-go=next]").dataset.target = next;
    panel.querySelector("[data-go=next]").disabled = !next;
    renderDetailTracks(e);
    return;
  }
  renderedEv = e.id;
  const v = view(e) || { tracks: [], lead: e.tracks[0] };
  const lead = v.lead || e.tracks[0];
  selKey = lead?.key || "";
  const start = Math.max(0, (v.tracks[0] || e.tracks[0]).t_first - 2);

  const video = e.video
    ? `<video id="vid" controls muted autoplay playsinline preload="metadata" src="${esc(e.video)}#t=${start.toFixed(1)}"></video>
       <div class="novideo" id="novideo" hidden>Video kann im Browser nicht abgespielt werden${e.video.includes("/main/") ? " (Main-Stream H.265)" : ""}. <a href="${esc(e.video)}" download>Herunterladen</a></div>`
    : `<div class="novideo">Kein Video vorhanden (Clip noch nicht heruntergeladen).</div>`;

  panel.innerHTML = `
    <div class="detail-head">
      <button type="button" class="icon-btn" data-go="prev" data-target="${prev}" ${prev ? "" : "disabled"} title="Vorheriges (←)">‹</button>
      <button type="button" class="icon-btn" data-go="next" data-target="${next}" ${next ? "" : "disabled"} title="Nächstes (→)">›</button>
      <h3>${esc(e.time)} · ${esc(e.camera)}<small>${esc(fmtDate(e.date, { weekday: "long", day: "2-digit", month: "2-digit", year: "numeric" }))}</small></h3>
      <button type="button" class="icon-btn" data-go="close" title="Schließen (Esc)">✕</button>
    </div>
    <div class="detail-body">
      ${video}
      <div class="big-crop">${lead && (lead.plate_crop || lead.crop) ? `<img id="bigcrop" src="${esc(lead.plate_crop || lead.crop)}" alt="Bildausschnitt">` : ""}</div>
      <div id="dyn"></div>
      <dl class="kv">
        <dt>Datei</dt><dd>${esc(e.file)}</dd>
        <dt>Clip-Start</dt><dd>${esc(e.clip_start.replace("T", " "))}</dd>
        <dt>Quelle</dt><dd>${e.source === "main" ? "Main-Stream (Kennzeichen-Scan)" : "Sub-Stream-Erkennung"}</dd>
        <dt>Dauer</dt><dd>${e.duration} s</dd>
      </dl>
    </div>`;
  panel.hidden = false;
  if (matchMedia("(max-width: 720px)").matches) document.body.style.overflow = "hidden";
  panel.scrollTop = 0;

  const vid = $("#vid");
  if (vid) {
    vid.addEventListener("error", () => { vid.hidden = true; $("#novideo").hidden = false; });
  }
  renderDetailTracks(e);
}

// ---------- Detail: Tracks, Kennzeichen, Nachklassifikation ----------
let selKey = "";

function reviewText(t) {
  const r = t.review;
  if (!r) return "";
  if (r.by === "rule") return " · Fehlalarm per Regel";
  const parts = [];
  if (r.cls && r.cls !== t.orig_cls) parts.push(`korrigiert: ${CLS[t.orig_cls] || t.orig_cls} → ${CLS[r.cls] || r.cls}`);
  else if (r.cls) parts.push("bestätigt");
  if (r.vehicle || r.plate) parts.push("Kennzeichen von Hand");
  return parts.length ? " · " + parts.join(", ") : "";
}

/** Stehende Tracks gleicher Klasse an derselben Stelle (wie die Regel im Server). */
function sameSpot(cam, t) {
  if (t.moving || !t.box) return [];
  const iou = (a, b) => {
    const ix = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
    const iy = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
    const u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy;
    return u > 0 ? (ix * iy) / u : 0;
  };
  const out = [];
  for (const ev of DATA.events) {
    if (ev.camera !== cam) continue;
    for (const x of ev.tracks) {
      if (!x.moving && x.box && x.orig_cls === t.orig_cls && x.review?.by !== "manual"
          && iou(x.box, t.box) >= (DATA.rule_iou || 0.5)) out.push(x);
    }
  }
  return out;
}

function renderDetailTracks(e) {
  const dyn = $("#dyn");
  if (!dyn) return;
  const v = view(e) || { tracks: [] };
  const ok = new Set(v.tracks.map((t) => t.key));
  if (!e.tracks.some((t) => t.key === selKey)) selKey = e.tracks[0]?.key || "";
  const sel = e.tracks.find((t) => t.key === selKey);

  const tracks = e.tracks.map((t, i) => `
    <button type="button" class="track${ok.has(t.key) ? "" : " dim"}${t.key === selKey ? " sel" : ""}${t.kind === "fp" ? " fp" : ""}" data-ti="${i}" aria-pressed="${t.key === selKey}">
      ${t.crop ? `<img loading="lazy" src="${esc(t.plate_crop || t.crop)}" alt="">` : "<span></span>"}
      <span>
        <span class="t-title">${ICON[t.kind] || ""} ${esc(CLS[t.cls] || t.cls)} ${t.color ? `<span class="t-meta">${esc(t.color)}</span>` : ""} ${plateBadge(t, true)}${t.review?.by === "manual" ? ` <span class="badge rv">✓</span>` : ""}</span>
        <span class="t-meta">${t.t_first.toFixed(1)}–${t.t_last.toFixed(1)} s · Größe ${(t.area * 100).toFixed(1)} % · Bewegung ${t.path.toFixed(2)}${t.conf != null ? " · Konf. " + t.conf : ""}${t.night ? " · IR" : ""}${esc(reviewText(t))}${ok.has(t.key) ? "" : " · ausgefiltert"}</span>
      </span>
    </button>`).join("");

  const plates = e.tracks.filter((t) => t.plate && t.plate.n_total).map((t) => {
    const p = t.plate;
    const alts = p.alternatives.map((a) => `${esc(a.text)} (${a.n})`).join(", ");
    return `<div class="platebox">
      <div class="row">${plateBadge(t, true)} <span class="t-meta">Lesung ${esc(p.text)} · ${p.n_reads}/${p.n_total} Frames · Konf. ${p.conf}${p.match ? " · Treffer " + esc(p.match.type) : ""}</span></div>
      ${p.img ? `<img loading="lazy" src="${esc(p.img)}" alt="Kennzeichen">` : ""}
      ${alts ? `<div class="t-meta">Weitere Lesungen: ${alts}</div>` : ""}
    </div>`;
  }).join("");

  dyn.innerHTML = plates + `<div class="tracks">${tracks}</div>` + (sel ? reviewForm(e, sel) : "");

  const vid = $("#vid");
  dyn.querySelectorAll(".track").forEach((el) => el.addEventListener("click", () => {
    const t = e.tracks[+el.dataset.ti];
    selKey = t.key;
    const img = $("#bigcrop");
    if (img && (t.plate_crop || t.crop)) img.src = t.plate_crop || t.crop;
    if (vid && !vid.hidden) { vid.currentTime = Math.max(0, t.best_t - 1); vid.play().catch(() => {}); }
    renderDetailTracks(e);
  }));
  bindReview(e, sel);
}

function reviewForm(e, t) {
  const r = t.review?.by === "manual" ? t.review : null;
  const isVeh = t.kind === "vehicle" || ["car", "truck", "bus", "motorcycle"].includes(t.orig_cls);
  const spot = t.kind !== "fp" ? sameSpot(e.camera, t) : [];
  const wl = DATA.watchlist || [];
  const ruleId = t.review?.rule;
  const btns = REVIEW.map(([c, k]) => `<button type="button" data-cls="${c}" aria-pressed="${t.cls === c}"
      title="${esc(CLS[c])}${k ? " (Taste " + k.toUpperCase() + ")" : ""}">${esc(CLS[c])}</button>`).join("");
  return `<section class="review" aria-label="Nachklassifikation">
    <h4>Nachklassifikation <span class="t-meta">erkannt als ${esc(CLS[t.orig_cls] || t.orig_cls)}${t.conf != null ? " (" + t.conf + ")" : ""}</span></h4>
    <div class="seg rv-cls" role="group" aria-label="Klasse">${btns}</div>
    ${isVeh && t.kind !== "fp" ? `<div class="rv-row">
      <label>Fahrzeug <select id="rv-veh"><option value="">–</option>${wl.map((w) =>
        `<option value="${esc(w.plate)}"${r?.vehicle === w.plate ? " selected" : ""}>${esc(w.plate)} · ${esc(w.label)}</option>`).join("")}</select></label>
      <label>Kennzeichen <input id="rv-plate" type="text" maxlength="12" spellcheck="false" autocomplete="off"
        value="${esc(r?.plate || "")}" placeholder="${esc(t.plate?.text || "z. B. ABC123")}"></label>
    </div>` : ""}
    ${ruleId ? `<div class="rv-row"><span class="t-meta">Fehlalarm per Regel #${ruleId} (stehendes Objekt an dieser Stelle).</span>
      <button type="button" class="link" id="rv-unrule" data-rule="${ruleId}">Regel aufheben</button></div>` : ""}
    ${!ruleId && spot.length > 1 ? `<div class="rv-row"><button type="button" id="rv-rule" class="btn">
      ⊘ ${spot.length} stehende „${esc(CLS[t.orig_cls] || t.orig_cls)}“ an dieser Stelle als Fehlalarm</button></div>` : ""}
    <div class="rv-row">
      <button type="button" class="btn primary" id="rv-ok" title="Erkennung ist richtig (Taste B)">✓ Bestätigen</button>
      ${t.review?.by === "manual" ? `<button type="button" class="btn" id="rv-reset">Zurücksetzen</button>` : ""}
      <span class="t-meta" id="rv-msg" role="status"></span>
    </div>
  </section>`;
}

async function postJson(url, body, method = "POST") {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** Geänderte Ereignisse vom Server übernehmen, ohne alles neu zu laden. */
function mergeEvents(res) {
  const upd = new Map(res.events.map((e) => [e.id, e]));
  DATA.events = DATA.events.map((e) => upd.get(e.id) || e);
  DATA.rules = res.rules;
  for (const e of res.events) BY_ID.set(e.id, e);
  const views = filtered();
  renderMain(views);
  renderDetail(views);
}

async function saveReview(t, patch) {
  const r = t.review?.by === "manual" ? t.review : {};
  const body = { key: t.key, cls: r.cls || null, vehicle: r.vehicle || null, plate: r.plate || null, note: r.note || null, ...patch };
  const msg = $("#rv-msg");
  if (msg) msg.textContent = "speichert …";
  try {
    mergeEvents(await postJson("/api/label", body));
    const m = $("#rv-msg"); if (m) m.textContent = "gespeichert";
  } catch (err) {
    const m = $("#rv-msg"); if (m) m.textContent = "Fehler beim Speichern: " + err.message;
  }
}

async function reviewAction(fn) {
  try { mergeEvents(await fn()); } catch (err) {
    const m = $("#rv-msg"); if (m) m.textContent = "Fehler: " + err.message;
  }
}

function bindReview(e, t) {
  if (!t) return;
  $("#dyn .rv-cls")?.addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-cls]");
    if (b) saveReview(t, { cls: b.dataset.cls });
  });
  $("#rv-ok")?.addEventListener("click", () => saveReview(t, { cls: t.cls === "fp" && t.review?.by === "rule" ? t.orig_cls : t.cls }));
  $("#rv-reset")?.addEventListener("click", () => reviewAction(() => postJson("/api/label", { key: t.key, reset: true })));
  $("#rv-veh")?.addEventListener("change", (ev) => saveReview(t, { vehicle: ev.target.value || null }));
  $("#rv-plate")?.addEventListener("change", (ev) => saveReview(t, { plate: ev.target.value.trim() || null }));
  $("#rv-rule")?.addEventListener("click", () => reviewAction(() => postJson("/api/rule", { key: t.key })));
  $("#rv-unrule")?.addEventListener("click", (ev) =>
    reviewAction(() => postJson(`/api/rule/${ev.target.dataset.rule}`, null, "DELETE")));
}

function reviewKey(ev) {
  const e = BY_ID.get(S.ev);
  const t = e?.tracks.find((x) => x.key === selKey);
  if (!t) return false;
  const k = ev.key.toLowerCase();
  if (k === "b") { $("#rv-ok")?.click(); return true; }
  const hit = REVIEW.find(([, key]) => key && key === k);
  if (hit) { saveReview(t, { cls: hit[0] }); return true; }
  return false;
}

// ---------- Filter-UI ----------
function buildCamSeg() {
  const cams = ["all", ...(DATA?.cameras || [])];
  $("#f-cam").innerHTML = cams.map((c) => `<button type="button" data-v="${esc(c)}">${c === "all" ? "Alle Kameras" : esc(c)}</button>`).join("");
}

function syncFilterUI() {
  for (const b of $("#f-cam").children) b.setAttribute("aria-pressed", b.dataset.v === S.cam);
  for (const b of $("#f-light").children) b.setAttribute("aria-pressed", b.dataset.v === S.light);
  for (const b of $("#f-rv").children) b.setAttribute("aria-pressed", b.dataset.v === S.rv);
  $("#f-person").checked = S.p === "1";
  $("#f-vehicle").checked = S.v === "1";
  $("#f-near").checked = S.near === "1";
  $("#f-from").value = S.from;
  $("#f-to").value = S.to;
  if (document.activeElement !== $("#f-q")) $("#f-q").value = S.q;
}

function closeDetail() { S.ev = ""; writeHash(true); }

function bindUI() {
  $("#f-cam").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    S.cam = b.dataset.v; writeHash(false);
  });
  $("#f-light").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    S.light = b.dataset.v; writeHash(false);
  });
  $("#f-rv").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    S.rv = b.dataset.v; writeHash(false);
  });
  $("#f-person").addEventListener("change", (ev) => { S.p = ev.target.checked ? "1" : "0"; writeHash(false); });
  $("#f-vehicle").addEventListener("change", (ev) => { S.v = ev.target.checked ? "1" : "0"; writeHash(false); });
  $("#f-near").addEventListener("change", (ev) => { S.near = ev.target.checked ? "1" : "0"; writeHash(false); });
  $("#f-from").addEventListener("change", (ev) => { S.from = ev.target.value; writeHash(false); });
  $("#f-to").addEventListener("change", (ev) => { S.to = ev.target.value; writeHash(false); });
  let qt;
  $("#f-q").addEventListener("input", (ev) => {
    clearTimeout(qt);
    qt = setTimeout(() => { S.q = ev.target.value.trim(); writeHash(false); }, 200);
  });
  $("#btn-filter").addEventListener("click", () => {
    const open = $("#filters").classList.toggle("open");
    $("#btn-filter").setAttribute("aria-expanded", open);
  });
  $("#btn-refresh").addEventListener("click", () => load());

  document.addEventListener("click", (ev) => {
    // Drilldown: Marke = heute, data-at = Jahr/Monat/Tag/Stunde (auch Streifen-Fächer)
    const a = ev.target.closest(".brand, [data-at]");
    if (a && !a.disabled) {
      ev.preventDefault();
      S.at = a.classList.contains("brand") ? "" : a.dataset.at;
      S.ev = "";
      writeHash(true);
      window.scrollTo(0, 0);
      return;
    }
    const c = ev.target.closest(".card[data-ev]");
    if (c) { S.ev = c.dataset.ev; writeHash(true); return; }
    const go = ev.target.closest("[data-go]");
    if (go) {
      if (go.dataset.go === "close") closeDetail();
      else if (go.dataset.target) { S.ev = go.dataset.target; writeHash(false); scrollToActive(); }
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (!S.ev || ev.target.matches?.("input, select, textarea") || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key.length === 1 && reviewKey(ev)) { ev.preventDefault(); return; }
    if (ev.key === "Escape") closeDetail();
    else if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      const b = $(`[data-go=${ev.key === "ArrowLeft" ? "prev" : "next"}]`);
      if (b && b.dataset.target) { ev.preventDefault(); S.ev = b.dataset.target; writeHash(false); scrollToActive(); }
    }
  });

  window.addEventListener("hashchange", () => { readHash(); render(); });
}

function scrollToActive() {
  document.querySelector(".card.active")?.scrollIntoView({ block: "nearest" });
}

// ---------- Daten ----------
async function load() {
  $("#status").textContent = "lädt …";
  try {
    const r = await fetch("/api/events", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    DATA = await r.json();
    BY_ID = new Map(DATA.events.map((e) => [e.id, e]));
    buildCamSeg();
    $("#status").textContent = `Stand ${DATA.generated.slice(11)} · ${DATA.events.length} Clips`;
  } catch (err) {
    $("#status").textContent = "Fehler beim Laden";
  }
  render();
}

async function loadStatus() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) return;
    STATUS = await r.json();
  } catch (err) {
    return;
  }
  // nur die Listenansicht neu zeichnen; ein offenes Detail bleibt unberührt
  if (DATA) renderMain(filtered());
}

// ---------- Theme: System / Hell (Sonne) / Dunkel (Mond) ----------
function currentTheme() { return document.documentElement.dataset.theme || "system"; }

function applyTheme(v) {
  if (v === "light" || v === "dark") document.documentElement.dataset.theme = v;
  else delete document.documentElement.dataset.theme;
  try { if (v === "system") localStorage.removeItem("theme"); else localStorage.setItem("theme", v); } catch (e) { /* ohne Speicher */ }
  for (const b of $("#theme").children) b.setAttribute("aria-pressed", b.dataset.v === currentTheme());
}

$("#theme").addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (b) applyTheme(b.dataset.v);
});
applyTheme(currentTheme());

readHash();
bindUI();
load().then(loadStatus);
setInterval(() => { if (document.visibilityState === "visible") load(); }, 60000);
setInterval(() => { if (document.visibilityState === "visible") loadStatus(); }, 10000);
