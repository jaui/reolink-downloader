"use strict";

// ---------- Zustand (im URL-Hash) ----------
const DEFAULTS = { cam: "all", p: "1", v: "1", near: "1", light: "all", q: "", from: "", to: "", day: "", ev: "" };
const S = { ...DEFAULTS };
let DATA = null;
let BY_ID = new Map();
let renderedEv = null;

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CLS = { person: "Person", car: "Auto", bus: "Bus", truck: "LKW" };
const ICON = { person: "🚶", vehicle: "🚗" };

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

function view(e) {
  if (S.cam !== "all" && e.camera !== S.cam) return null;
  if (S.light === "day" && e.night) return null;
  if (S.light === "night" && !e.night) return null;
  if (S.from && e.date < S.from) return null;
  if (S.to && e.date > S.to) return null;
  const tracks = e.tracks.filter((t) => trackOk(t) && queryOk(t));
  if (!tracks.length) return null;
  const np = tracks.filter((t) => t.kind === "person").length;
  const nv = tracks.length - np;
  const plates = tracks.filter((t) => t.plate);
  const hits = plates.filter((t) => t.plate.match);
  const lead = hits[0] || plates[0]
    || [...tracks].sort((a, b) => (b.kind === "vehicle") - (a.kind === "vehicle") || b.area - a.area)[0];
  return { e, tracks, np, nv, plates, hits, lead, thumb: lead.plate_crop || lead.crop };
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

function strip(views, max, clickable) {
  const p = new Array(24).fill(0), v = new Array(24).fill(0);
  for (const x of views) { const h = hourOf(x); if (x.np) p[h]++; if (x.nv) v[h]++; }
  const cell = (n, cls, h) =>
    `<div class="h ${n ? cls : ""}" style="--i:${n ? Math.max(0.25, n / max) : 0}" data-h="${h}" title="${String(h).padStart(2, "0")} Uhr: ${n} ${cls === "p" ? "mit Personen" : "mit Fahrzeugen"}"></div>`;
  const rows = [];
  if (S.p === "1") rows.push(`<div class="strip${clickable ? " click" : ""}">${p.map((n, h) => cell(n, "p", h)).join("")}</div>`);
  if (S.v === "1") rows.push(`<div class="strip${clickable ? " click" : ""}">${v.map((n, h) => cell(n, "v", h)).join("")}</div>`);
  const axis = `<div class="strip-axis">${[...Array(24).keys()].map((h) => `<span>${h}</span>`).join("")}</div>`;
  return { html: `<div class="strip-rows">${rows.join("")}${axis}</div>`, max: Math.max(...p, ...v) };
}

function hourMax(groups) {
  let m = 1;
  for (const vs of groups.values()) {
    const p = new Array(24).fill(0), v = new Array(24).fill(0);
    for (const x of vs) { const h = hourOf(x); if (x.np) p[h]++; if (x.nv) v[h]++; }
    m = Math.max(m, ...p, ...v);
  }
  return m;
}

function byDay(views) {
  const g = new Map();
  for (const x of views) {
    if (!g.has(x.e.date)) g.set(x.e.date, []);
    g.get(x.e.date).push(x);
  }
  return g;
}

// ---------- Rendering ----------
function render() {
  syncFilterUI();
  renderCrumbs();
  if (!DATA) { $("#main").innerHTML = `<p class="empty">Lade Ereignisse …</p>`; return; }
  const views = filtered();
  if (S.day) renderDay(views); else renderDays(views);
  renderDetail(views);
}

function renderCrumbs() {
  const parts = [];
  if (S.day) parts.push(`<span>›</span><a href="#" data-nav="days">Alle Tage</a><span>›</span><span>${esc(fmtDate(S.day))}</span>`);
  $("#crumbs").innerHTML = parts.join("");
}

function renderDays(views) {
  const groups = byDay(views);
  const days = [...groups.keys()].sort().reverse();
  if (!days.length) {
    $("#main").innerHTML = `<p class="empty">Keine Ereignisse für diese Filter.</p>`;
    return;
  }
  const max = hourMax(groups);
  const html = days.map((d) => {
    const vs = groups.get(d);
    const np = vs.filter((x) => x.np).length, nv = vs.filter((x) => x.nv).length;
    const labels = new Map();
    for (const x of vs) for (const t of x.hits) labels.set(t.plate.match.plate, t);
    const hits = [...labels.values()].map((t) => plateBadge(t, true)).join("");
    return `<a class="day" href="#" data-day="${d}">
      <div class="day-date">${esc(fmtDate(d, { day: "2-digit", month: "2-digit", year: "numeric" }))}<small>${esc(fmtDate(d, { weekday: "long" }))}</small></div>
      ${strip(vs, max, false).html}
      <div class="day-counts">
        ${S.p === "1" ? `<span class="cnt person" title="Ereignisse mit Personen">${ICON.person} ${np}</span>` : ""}
        ${S.v === "1" ? `<span class="cnt vehicle" title="Ereignisse mit Fahrzeugen">${ICON.vehicle} ${nv}</span>` : ""}
      </div>
      <div class="day-hits">${hits}</div>
    </a>`;
  }).join("");
  $("#main").innerHTML = `<p class="summary">${views.length} Ereignisse an ${days.length} Tagen</p><div class="days">${html}</div>`;
}

function renderDay(views) {
  const groups = byDay(views);
  const days = [...groups.keys()].sort();
  const vs = groups.get(S.day) || [];
  const i = days.indexOf(S.day);
  const prev = i > 0 ? days[i - 1] : (i < 0 ? days.filter((d) => d < S.day).pop() : null);
  const next = i >= 0 ? days[i + 1] : days.find((d) => d > S.day);
  const st = strip(vs, hourMax(new Map([[S.day, vs]])), true);
  const np = vs.filter((x) => x.np).length, nv = vs.filter((x) => x.nv).length;

  let prevView = null;
  const cards = vs.map((x) => {
    const e = x.e;
    const cont = prevView && prevView.e.camera === e.camera
      && (Date.parse(e.start) - Date.parse(prevView.e.start)) / 1000 <= prevView.e.duration + 60;
    prevView = x;
    const counts = [
      x.np ? `<span class="cnt person">${ICON.person}${x.np > 1 ? " " + x.np : ""}</span>` : "",
      x.nv ? `<span class="cnt vehicle">${ICON.vehicle}${x.nv > 1 ? " " + x.nv : ""}</span>` : "",
    ].join("");
    const plates = x.plates.map((t) => plateBadge(t, false)).join("");
    return `<button type="button" class="card${cont ? " cont" : ""}${e.id === S.ev ? " active" : ""}" data-ev="${esc(e.id)}" data-h="${hourOf(x)}">
      <div class="thumb">${x.thumb ? `<img loading="lazy" src="${esc(x.thumb)}" alt="">` : ""}</div>
      <div class="meta">
        <div class="row"><span class="time">${esc(e.time)}</span>${counts}${e.night ? `<span class="badge night">Nacht</span>` : ""}</div>
        <div class="row"><span class="cam">${esc(e.camera)}${e.duration ? " · " + Math.round(e.duration) + " s" : ""}</span></div>
        ${plates ? `<div class="row">${plates}</div>` : ""}
      </div>
    </button>`;
  }).join("");

  $("#main").innerHTML = `
    <section class="dayhead">
      <h2>${esc(fmtDate(S.day, { weekday: "long", day: "2-digit", month: "long", year: "numeric" }))}
        <span class="day-counts">
          ${S.p === "1" ? `<span class="cnt person">${ICON.person} ${np}</span>` : ""}
          ${S.v === "1" ? `<span class="cnt vehicle">${ICON.vehicle} ${nv}</span>` : ""}
        </span>
        <span class="daynav">
          <button type="button" data-day="${prev || ""}" ${prev ? "" : "disabled"} title="Vorheriger Tag">‹</button>
          <button type="button" data-day="${next || ""}" ${next ? "" : "disabled"} title="Nächster Tag">›</button>
        </span>
      </h2>
      ${st.html}
    </section>
    ${vs.length ? `<div class="grid">${cards}</div>` : `<p class="empty">Keine Ereignisse an diesem Tag für diese Filter.</p>`}`;
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
  const idx = list.findIndex((x) => x.e.id === e.id);
  const prev = idx > 0 ? list[idx - 1].e.id : "";
  const next = idx >= 0 && idx < list.length - 1 ? list[idx + 1].e.id : "";
  if (renderedEv === e.id) {      // nur Navigation aktualisieren, Video nicht neu laden
    panel.querySelector("[data-go=prev]").dataset.target = prev;
    panel.querySelector("[data-go=prev]").disabled = !prev;
    panel.querySelector("[data-go=next]").dataset.target = next;
    panel.querySelector("[data-go=next]").disabled = !next;
    return;
  }
  renderedEv = e.id;
  const v = view(e) || { tracks: [], lead: e.tracks[0] };
  const ok = new Set(v.tracks);
  const lead = v.lead || e.tracks[0];
  const start = Math.max(0, (v.tracks[0] || e.tracks[0]).t_first - 2);

  const tracks = e.tracks.map((t, i) => `
    <button type="button" class="track${ok.has(t) ? "" : " dim"}" data-ti="${i}">
      ${t.crop ? `<img loading="lazy" src="${esc(t.plate_crop || t.crop)}" alt="">` : "<span></span>"}
      <span>
        <span class="t-title">${ICON[t.kind] || ""} ${esc(CLS[t.cls] || t.cls)} ${t.color ? `<span class="t-meta">${esc(t.color)}</span>` : ""} ${plateBadge(t, true)}</span>
        <span class="t-meta">${t.t_first.toFixed(1)}–${t.t_last.toFixed(1)} s · Größe ${(t.area * 100).toFixed(1)} % · Bewegung ${t.path.toFixed(2)}${t.conf != null ? " · Konf. " + t.conf : ""}${t.night ? " · IR" : ""}${ok.has(t) ? "" : " · ausgefiltert"}</span>
      </span>
    </button>`).join("");

  const plates = e.tracks.filter((t) => t.plate).map((t) => {
    const p = t.plate;
    const alts = p.alternatives.map((a) => `${esc(a.text)} (${a.n})`).join(", ");
    return `<div class="platebox">
      <div class="row">${plateBadge(t, true)} <span class="t-meta">Lesung ${esc(p.text)} · ${p.n_reads}/${p.n_total} Frames · Konf. ${p.conf}${p.match ? " · Treffer " + esc(p.match.type) : ""}</span></div>
      ${p.img ? `<img loading="lazy" src="${esc(p.img)}" alt="Kennzeichen">` : ""}
      ${alts ? `<div class="t-meta">Weitere Lesungen: ${alts}</div>` : ""}
    </div>`;
  }).join("");

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
      ${plates}
      <div class="big-crop">${lead && (lead.plate_crop || lead.crop) ? `<img id="bigcrop" src="${esc(lead.plate_crop || lead.crop)}" alt="Bildausschnitt">` : ""}</div>
      <div class="tracks">${tracks}</div>
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
  panel.querySelectorAll(".track").forEach((el) => el.addEventListener("click", () => {
    const t = e.tracks[+el.dataset.ti];
    const img = $("#bigcrop");
    if (img && (t.plate_crop || t.crop)) img.src = t.plate_crop || t.crop;
    if (vid && !vid.hidden) { vid.currentTime = Math.max(0, t.best_t - 1); vid.play().catch(() => {}); }
  }));
}

// ---------- Filter-UI ----------
function buildCamSeg() {
  const cams = ["all", ...(DATA?.cameras || [])];
  $("#f-cam").innerHTML = cams.map((c) => `<button type="button" data-v="${esc(c)}">${c === "all" ? "Alle Kameras" : esc(c)}</button>`).join("");
}

function syncFilterUI() {
  for (const b of $("#f-cam").children) b.setAttribute("aria-pressed", b.dataset.v === S.cam);
  for (const b of $("#f-light").children) b.setAttribute("aria-pressed", b.dataset.v === S.light);
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
    const a = ev.target.closest("[data-nav=days], .brand");
    if (a) { ev.preventDefault(); S.day = ""; S.ev = ""; writeHash(true); return; }
    const d = ev.target.closest("[data-day]");
    if (d && d.dataset.day) { ev.preventDefault(); S.day = d.dataset.day; S.ev = ""; writeHash(true); window.scrollTo(0, 0); return; }
    const c = ev.target.closest(".card[data-ev]");
    if (c) { S.ev = c.dataset.ev; writeHash(true); return; }
    const h = ev.target.closest(".strip.click .h");
    if (h) {
      const card = document.querySelector(`.card[data-h="${h.dataset.h}"]`);
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
      card?.focus({ preventScroll: true });
      return;
    }
    const go = ev.target.closest("[data-go]");
    if (go) {
      if (go.dataset.go === "close") closeDetail();
      else if (go.dataset.target) { S.ev = go.dataset.target; writeHash(false); scrollToActive(); }
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (!S.ev || ev.target.matches("input")) return;
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

readHash();
bindUI();
load();
setInterval(() => { if (document.visibilityState === "visible") load(); }, 60000);
