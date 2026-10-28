/* Pacesetter dashboard — plain JS, Leaflet for the map, hand-built SVG charts. */
(() => {
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const NS = "http://www.w3.org/2000/svg";
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  del(k) { try { sessionStorage.removeItem(k); } catch {} },
};
const prefs = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// ───────────────────────── units ─────────────────────────
// Everything is stored in km and min/km; these convert for display.
const MI = 0.621371;
let UNIT = prefs.get("ps_unit") === "mi" ? "mi" : "km";
const dv = (km) => (UNIT === "mi" ? km * MI : km);
const fmtD = (km, dp) => { const v = dv(km); return v.toFixed(dp ?? (v < 10 ? 1 : 0)) + " " + UNIT; };
const pu = () => "/" + UNIT;
const paceU = (p) => (UNIT === "mi" ? p / MI : p);
const fmtPaceRaw = (p) => { if (!isFinite(p) || p <= 0) return "–"; const m = Math.floor(p); const s = Math.round((p - m) * 60); return s === 60 ? `${m + 1}:00` : `${m}:${String(s).padStart(2, "0")}`; };
const fmtPace = (p) => fmtPaceRaw(paceU(p));
const deltaU = (secPerKm) => (UNIT === "mi" ? secPerKm / MI : secPerKm);
const fmtDelta = (secPerKm) => { if (!isFinite(secPerKm)) return "–"; const v = deltaU(secPerKm); return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(0) + "s"; };
const fmtDur = (sec) => { const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
const fmtClock = (sec) => { const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60); return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`; };
const fmtDate = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtGrade = (g) => { const v = Math.abs(g) < 0.05 ? 0 : g; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1) + "%"; };

// ───────────────────────── auth + load ─────────────────────────
async function load(pw) {
  const r = await fetch("/api/dash", { headers: { Authorization: `Bearer ${pw}` }, cache: "no-store" });
  if (r.status === 401) throw new Error("Wrong password");
  if (!r.ok) { let msg = `Server error (${r.status})`; try { msg = (await r.json()).error || msg; } catch {} throw new Error(msg); }
  return r.json();
}
async function start(pw) {
  $("#loginErr").textContent = "";
  try {
    const data = await load(pw);
    store.set("ps_pw", pw);
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    init(data);
  } catch (e) {
    store.del("ps_pw");
    $("#loginErr").textContent = e.message;
  }
}
$("#loginForm").addEventListener("submit", (e) => { e.preventDefault(); start($("#pw").value); });
$("#logout").addEventListener("click", () => { store.del("ps_pw"); location.reload(); });
const saved = store.get("ps_pw");
if (saved) start(saved);

// ───────────────────────── data model ─────────────────────────
let S = null; // state

function undelta(a) { const out = new Array(a.length); let acc = 0; for (let i = 0; i < a.length; i++) { acc += a[i]; out[i] = acc; } return out; }
function smooth(a, k = 2) { return a.map((_, i) => { const w = a.slice(Math.max(0, i - k), i + k + 1).sort((x, y) => x - y); return w[w.length >> 1]; }); }

function decodeRun(r) {
  const t = undelta(r.t), lat = undelta(r.lat).map((v) => v / 1e5), lon = undelta(r.lon).map((v) => v / 1e5);
  const ele = undelta(r.ele).map((v) => v / 10), d = undelta(r.d).map((v) => v / 1000);
  const p = r.p.map((v) => v / 100);
  const se = smooth(ele, 2);
  let gain = 0; for (let i = 1; i < se.length; i++) gain += Math.max(0, se[i] - se[i - 1]);
  // Song windows include stops, so songs are compared against the elapsed-time average, not Strava's moving pace.
  const epace = t[t.length - 1] / 60 / (d[d.length - 1] || r.km || 1);
  return { ...r, t, lat, lon, ele, d, p, gain, epace, date: new Date(r.start), songs: [] };
}

function interp(xs, ys, x) {
  if (x <= xs[0]) return ys[0];
  const n = xs.length;
  if (x >= xs[n - 1]) return ys[n - 1];
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  const f = (x - xs[lo]) / ((xs[hi] - xs[lo]) || 1);
  return ys[lo] + f * (ys[hi] - ys[lo]);
}
function idxAt(xs, x) { let lo = 0, hi = xs.length - 1; if (x >= xs[hi]) return hi; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; } return lo; }

function segType(g) { return g > 10 ? "wall" : g > 5 ? "steep" : g > 2 ? "moderate" : g < -1 ? "descent" : "flat"; }

// Spotify's played_at marks when a track ENDED; start = played_at − duration.
function align(runs, plays, tracks) {
  for (const run of runs) {
    const r0 = run.t0, r1 = run.t0 + run.t[run.t.length - 1];
    for (const [id, ms, dur, name, artist] of plays) {
      const end = ms / 1000, st = end - dur / 1000;
      if (end <= r0 || st >= r1) continue;
      const s = Math.max(st, r0) - r0, e = Math.min(end, r1) - r0;
      if (e - s < 20) continue;
      const d0 = interp(run.t, run.d, s), d1 = interp(run.t, run.d, e);
      const e0 = interp(run.t, run.ele, s), e1 = interp(run.t, run.ele, e);
      const km = d1 - d0;
      const pace = km > 0.03 ? (e - s) / 60 / km : NaN;
      const grade = km > 0.03 ? ((e1 - e0) / (km * 1000)) * 100 : 0;
      const tr = tracks[id] || { n: name, a: artist, f: null };
      const reward = isFinite(pace) ? clamp(1 - Math.abs(pace - run.epace), -1, 1) : null;
      const delta = isFinite(pace) ? (pace - run.epace) * 60 : NaN; // s/km vs elapsed-time run average; negative = faster
      run.songs.push({ id, name: tr.n || name, artist: tr.a || artist, f: tr.f, s, e, d0, d1, pace, grade, delta,
        type: segType(grade), reward, run });
    }
    run.songs.sort((a, b) => a.s - b.s);
    run.songs.forEach((sg, i) => (sg.idx = i + 1));
  }
}

function init(data) {
  const tracks = data.tracks;
  const live = data.live || { connected: false, plays: [] };
  const seen = new Set();
  const plays = [...data.plays, ...(live.plays || [])].filter((p) => {
    const k = p[0] + "|" + p[1]; if (seen.has(k)) return false; seen.add(k); return true;
  }).sort((a, b) => a[1] - b[1]);
  const runs = data.runs.map(decodeRun).sort((a, b) => b.date - a.date);
  align(runs, plays, tracks);
  const segs = runs.flatMap((r) => r.songs);
  S = { data, tracks, live, plays, runs, segs, run: runs[0],
        colorMode: "song", base: "street", hl: null, hist: "km", deltaMode: "fast",
        cur: [], pt: null, playing: false, speed: 60, animate: true };

  $("#snapInfo").textContent = `${data.source === "redis" ? "Synced" : "Snapshot"} ${new Date(data.generated).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
  const li = $("#liveInfo");
  if (live.connected) {
    li.innerHTML = `<span class="dot on"></span>Logger live · ${live.plays.length} new play${live.plays.length === 1 ? "" : "s"}`;
    li.title = live.meta && live.meta.last_run_ms ? `Last pull ${new Date(+live.meta.last_run_ms).toLocaleString()}` : "";
  } else {
    li.innerHTML = `<span class="dot off"></span>Logger not connected`;
    li.title = live.error || "";
  }

  const sel = $("#runSel");
  sel.addEventListener("change", () => selectRun(runs[+sel.value], false));
  segControl("#colorMode", "m", (v) => { S.colorMode = v; drawRoute(false); });
  segControl("#baseMode", "b", (v) => { S.base = v; setBase(); });
  segControl("#histMode", "h", (v) => { S.hist = v; withAnim(renderHistory); });
  segControl("#deltaMode", "d", (v) => { S.deltaMode = v; withAnim(renderSongDelta); });
  segControl("#speedSel", "s", (v) => { S.speed = +v; });
  segControl("#unitSel", "u", (v) => { UNIT = v; prefs.set("ps_unit", v); renderAll(); }, UNIT);
  ["qGrade", "qDist", "qK"].forEach((id) => $("#" + id).addEventListener("input", renderModel));
  $("#playBtn").addEventListener("click", togglePlay);
  $("#scrub").addEventListener("input", () => { stopPlay(); S.pt = +$("#scrub").value; setCursor(idxAt(S.run.t, S.pt)); });
  addEventListener("keydown", (e) => {
    if (e.code !== "Space" || /INPUT|SELECT|TEXTAREA|BUTTON/.test(document.activeElement.tagName)) return;
    const r = $("#runs").getBoundingClientRect();
    if (r.top < innerHeight && r.bottom > 0) { e.preventDefault(); togglePlay(); }
  });

  document.body.classList.add("intro");
  renderAll();
  S.animate = false;
  setTimeout(() => document.body.classList.remove("intro"), 1200);
  initNavSpy();
  let rt, lastW = innerWidth;
  addEventListener("resize", () => { if (innerWidth === lastW) return; lastW = innerWidth; clearTimeout(rt); rt = setTimeout(renderAll, 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { setBase(); renderAll(); });
}

function segControl(sel, key, onPick, initial) {
  const btns = $$(`${sel} button`);
  const mark = (v) => btns.forEach((x) => x.classList.toggle("on", x.dataset[key] === v));
  if (initial != null) mark(initial);
  btns.forEach((b) => b.addEventListener("click", () => { mark(b.dataset[key]); onPick(b.dataset[key]); }));
}
function withAnim(fn) { S.animate = true; try { fn(); } finally { S.animate = false; } }
const A = (cls) => (S.animate ? cls : "");

function renderAll() {
  renderHero(); renderKpis(); renderRace(); renderTrends(); renderRun(false);
  renderListening(); renderMusicPerf(); renderModel();
}

function selectRun(run, scroll) {
  stopPlay();
  S.run = run; S.hl = null; S.pt = null;
  withAnim(() => { renderRun(true); renderHistory(); renderCalendar(); });
  if (scroll) $("#runs").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth" });
}

function initNavSpy() {
  const links = [...$$("#nav a")], byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) links.forEach((a) => a.classList.toggle("on", a === byId.get(e.target.id)));
  }, { rootMargin: "-40% 0px -55% 0px" });
  byId.forEach((_, id) => io.observe(document.getElementById(id)));
}

// ───────────────────────── tooltip ─────────────────────────
const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html; tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => (tip.style.display = "none");

// ───────────────────────── svg helpers ─────────────────────────
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function txt(parent, x, y, s, attrs = {}) { const t = el("text", { x, y, ...attrs }, parent); t.textContent = s; return t; }
function svgBox(container, h, minW = 280) {
  container.innerHTML = "";
  const w = Math.max(minW, container.clientWidth || 600);
  const svg = el("svg", { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: "img" }, container);
  svg.style.display = "block";
  return { svg, w, h };
}
function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || 1, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function mixHex(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const A2 = p(a), B = p(b);
  return "#" + A2.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
const seq = (t) => mixHex(css("--seq-lo"), css("--seq-hi"), clamp(t, 0, 1));
// Diverging: blue = faster than the run average, red = slower, gray = on average. Saturates at ±30 s/km.
const divColor = (v) => (isFinite(v) ? mixHex(css("--div-mid"), css(v < 0 ? "--div-neg" : "--div-pos"), clamp(Math.abs(v) / 30, 0, 1)) : css("--muted"));
function drawLine(poly) {
  if (!S.animate || reducedMotion()) return;
  const len = poly.getTotalLength ? poly.getTotalLength() : 0;
  if (!len) return;
  poly.style.strokeDasharray = len; poly.style.strokeDashoffset = len; poly.classList.add("draw");
}
function countUp(node, to, fmt, ms = 800) {
  if (!S.animate || reducedMotion()) { node.textContent = fmt(to); return; }
  const t0 = performance.now();
  const step = (now) => { const f = Math.min(1, (now - t0) / ms), e = 1 - (1 - f) ** 3; node.textContent = fmt(to * e); if (f < 1) requestAnimationFrame(step); };
  node.textContent = fmt(0); requestAnimationFrame(step);
}
function sparkBars(vals, { w = 120, h = 30, hiLast = true } = {}) {
  const mx = Math.max(1e-9, ...vals), bw = w / vals.length;
  const bars = vals.map((v, i) => { const bh = Math.max(v > 0 ? 2 : 0, (v / mx) * (h - 2)); return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(2, bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${hiLast && i === vals.length - 1 ? css("--s1") : css("--seq-lo")}" class="${A("ay")}" style="animation-delay:${i * 30}ms"/>`; }).join("");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${bars}</svg>`;
}
function sparkLine(vals, { w = 120, h = 30, invert = false } = {}) {
  if (vals.length < 2) return "";
  const lo = Math.min(...vals), hi = Math.max(...vals), sp = hi - lo || 1;
  const pts = vals.map((v, i) => [(i / (vals.length - 1)) * (w - 6) + 3, invert ? 3 + ((v - lo) / sp) * (h - 6) : h - 3 - ((v - lo) / sp) * (h - 6)]);
  const [lx, ly] = pts[pts.length - 1];
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts.map((p) => p.map((x) => x.toFixed(1)).join(",")).join(" ")}" fill="none" stroke="${css("--s1")}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${lx}" cy="${ly}" r="3.5" fill="${css("--s1")}" stroke="${css("--surface")}" stroke-width="2"/></svg>`;
}

// Horizontal bar list: [{label, sub, value, text}]
function barList(container, items, { color = css("--s1"), max, onClick } = {}) {
  container.innerHTML = "";
  if (!items.length) { container.innerHTML = `<div class="empty">Nothing yet</div>`; return; }
  const rowH = 28, labelW = Math.min(200, (container.clientWidth || 500) * 0.42);
  const { svg, w } = svgBox(container, items.length * rowH + 4);
  const m = max ?? Math.max(...items.map((d) => d.value));
  const x0 = labelW + 8, bw = w - x0 - 72;
  items.forEach((d, i) => {
    const y = i * rowH + 4;
    const g = el("g", {}, svg);
    const maxChars = Math.floor(labelW / 6.4);
    txt(g, labelW, y + 15, d.label.length > maxChars ? d.label.slice(0, maxChars - 1) + "…" : d.label, { "text-anchor": "end", class: "ink" });
    const len = Math.max(2, (d.value / m) * bw);
    const bar = el("rect", { x: x0, y: y + 5, width: len, height: 14, rx: 3, fill: d.color || color, class: A("ax") }, g);
    bar.style.animationDelay = `${i * 40}ms`;
    txt(g, x0 + len + 6, y + 16, d.text ?? d.value);
    el("rect", { x: 0, y, width: w, height: rowH, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => { bar.setAttribute("opacity", 0.8); showTip(`<b>${esc(d.label)}</b>${d.sub ? `<div class="m">${esc(d.sub)}</div>` : ""}<div>${esc(d.text ?? d.value)}</div>`, ev); });
    g.addEventListener("mouseleave", () => { bar.removeAttribute("opacity"); hideTip(); });
    if (onClick) { g.style.cursor = "pointer"; g.addEventListener("click", () => onClick(d)); }
  });
}

// ───────────────────────── weekly helpers ─────────────────────────
const DAY = 864e5, WEEK = 7 * DAY;
const addDays = (d, k) => { const x = new Date(d); x.setDate(x.getDate() + k); return x; };
const mondayOf = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
const kmIn = (start, days = 7) => S.runs.filter((r) => r.date >= start && r.date < addDays(start, days)).reduce((a, r) => a + r.km, 0);

// Songs aggregated across every run they played on (segments ≥ 200 m so pace is meaningful).
function songStats() {
  const agg = new Map();
  for (const s of S.segs) {
    if (!isFinite(s.delta) || s.d1 - s.d0 < 0.2) continue;
    const a = agg.get(s.id) || { id: s.id, name: s.name, artist: s.artist, segs: [], f: s.f };
    a.segs.push(s); agg.set(s.id, a);
  }
  return [...agg.values()].map((a) => ({ ...a, n: a.segs.length, v: a.segs.reduce((x, s) => x + s.delta, 0) / a.segs.length }));
}

// ───────────────────────── hero: latest run ─────────────────────────
function renderHero() {
  const run = S.runs[0], box = $("#heroCard");
  if (!run) { box.innerHTML = `<div class="empty">No runs synced yet</div>`; return; }
  const recent = S.runs.filter((r) => run.date - r.date < 90 * DAY && r !== run);
  const avgPace = recent.length ? recent.reduce((a, r) => a + r.sec, 0) / 60 / recent.reduce((a, r) => a + r.km, 0) : NaN;
  const dP = (run.sec / 60 / run.km - avgPace) * 60; // elapsed-time pace on both sides; negative = faster
  const first = run.songs[0];
  const scored = run.songs.filter((s) => isFinite(s.delta) && s.d1 - s.d0 >= 0.2);
  const best = scored.length ? scored.reduce((a, s) => (s.delta < a.delta ? s : a)) : null;
  const dayStr = run.date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  const ago = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(run.date).setHours(0, 0, 0, 0)) / DAY);
  const when = ago === 0 ? "Today" : ago === 1 ? "Yesterday" : `${ago} days ago`;
  box.innerHTML = `
    <div class="hero-top">
      <div class="eyebrow"><b>Latest run</b> · ${when}, ${esc(dayStr)} · ${esc(run.name)}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${!isFinite(dP) ? "" : Math.abs(deltaU(dP)) < 3 ? `<span class="badge"><span class="ic">●</span>Right on your 90-day average pace</span>`
          : `<span class="badge ${dP < 0 ? "good" : "bad"}"><span class="ic">${dP < 0 ? "▲" : "▼"}</span>${fmtDelta(Math.abs(dP)).slice(1)}${pu()} ${dP < 0 ? "faster" : "slower"} than your 90-day average</span>`}
        <button id="heroReplay">▶ Replay with music</button>
      </div>
    </div>
    <div class="hero-main">
      <div class="hero-fig"><span id="heroKm"></span><small>${UNIT}</small></div>
      <div class="hero-stats">
        <div><span class="v">${fmtDur(run.sec)}</span><span class="l">Time</span></div>
        <div><span class="v">${fmtPace(run.pace)}<span style="font-size:13px;color:var(--ink-2)">${pu()}</span></span><span class="l">Avg pace</span></div>
        <div><span class="v">${Math.round(run.gain)} m</span><span class="l">Climb</span></div>
        <div><span class="v">${run.songs.length}</span><span class="l">Songs</span></div>
      </div>
    </div>
    ${run.songs.length ? `<div class="callouts">
      ${first ? `<div class="callout" data-song="${first.idx}"><div class="ic">♪</div><div><div class="k">Opened with</div><div class="t">${esc(first.name)}</div><div class="a">${esc(first.artist)}</div></div></div>` : ""}
      ${best ? `<div class="callout" data-song="${best.idx}"><div class="ic" style="color:var(--div-neg)">⚡</div><div><div class="k">Fastest song · ${fmtDelta(best.delta)}${pu()}</div><div class="t">${esc(best.name)}</div><div class="a">${esc(best.artist)} · ${fmtPace(best.pace)}${pu()}</div></div></div>` : ""}
    </div>
    <div id="heroRibbon"></div>
    <div class="legend"><span>Each block is a song, sized by distance</span><span>Faster</span><span class="ramp div"></span><span>Slower than this run's average</span></div>` : `<div class="empty">No songs were captured on this run.</div>`}`;
  countUp($("#heroKm"), dv(run.km), (v) => v.toFixed(2), 900);
  if (run.songs.length) ribbon($("#heroRibbon"), run, { onPick: (sg) => { if (S.run !== run) selectRun(run, false); highlight(sg); $("#runs").scrollIntoView({ behavior: "smooth" }); } });
  box.querySelectorAll("[data-song]").forEach((c) => c.addEventListener("click", () => {
    if (S.run !== run) selectRun(run, false);
    highlight(run.songs[+c.dataset.song - 1]); $("#runs").scrollIntoView({ behavior: "smooth" });
  }));
  $("#heroReplay").addEventListener("click", () => {
    if (S.run !== run) selectRun(run, false);
    $("#runs").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth" });
    S.pt = 0; setTimeout(() => { if (!S.playing) togglePlay(); }, reducedMotion() ? 0 : 600);
  });
}

// Soundtrack ribbon: one block per song across the run's distance, colored by pace vs run average.
function ribbon(container, run, { onPick, h = 30 } = {}) {
  const { svg, w } = svgBox(container, h + 20);
  const m = { l: 2, r: 2 }, iw = w - m.l - m.r, x1 = run.d[run.d.length - 1] || 1;
  const X = (km) => m.l + (km / x1) * iw;
  el("rect", { x: m.l, y: h / 2 - 2, width: iw, height: 4, rx: 2, fill: css("--surface-2") }, svg);
  run.songs.forEach((sg, i) => {
    const a = X(sg.d0), b = X(sg.d1), g = el("g", { class: "click" }, svg);
    const blk = el("rect", { x: a + 1, y: 0, width: Math.max(2, b - a - 2), height: h, rx: 4, fill: divColor(sg.delta), class: A("ay") }, g);
    blk.style.animationDelay = `${i * 35}ms`;
    if (b - a > 16) txt(g, (a + b) / 2, h + 14, sg.idx, { "text-anchor": "middle" });
    el("rect", { x: a, y: 0, width: Math.max(4, b - a), height: h + 18, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => { blk.setAttribute("stroke", css("--ink")); blk.setAttribute("stroke-width", 2);
      showTip(`<b>${sg.idx}. ${esc(sg.name)}</b><div class="m">${esc(sg.artist)}</div><div>${fmtD(sg.d0, 1).split(" ")[0]}–${fmtD(sg.d1, 1)} · ${fmtPace(sg.pace)}${pu()}</div><div>${fmtDelta(sg.delta)}${pu()} vs run average · grade ${fmtGrade(sg.grade)}</div>`, ev); });
    g.addEventListener("mouseleave", () => { blk.removeAttribute("stroke"); hideTip(); });
    if (onPick) g.addEventListener("click", () => onPick(sg));
  });
  const cur = el("line", { y1: -2, y2: h + 2, stroke: css("--ink"), "stroke-width": 2, opacity: 0 }, svg);
  svg.style.overflow = "visible";
  return { set(i) { if (i == null) return cur.setAttribute("opacity", 0); const x = X(run.d[i]); cur.setAttribute("x1", x); cur.setAttribute("x2", x); cur.setAttribute("opacity", 1); } };
}

// ───────────────────────── KPIs ─────────────────────────
function renderKpis() {
  const { runs, segs, data } = S;
  if (!runs.length) { $("#kpis").innerHTML = ""; return; }
  const w0 = mondayOf(new Date());
  const weeks = Array.from({ length: 10 }, (_, i) => kmIn(addDays(w0, -7 * (9 - i))));
  const thisWk = weeks[9], lastWk = weeks[8];
  const wkChg = thisWk - lastWk;
  const last30 = runs.filter((r) => Date.now() - r.date < 30 * DAY), prev30 = runs.filter((r) => Date.now() - r.date >= 30 * DAY && Date.now() - r.date < 60 * DAY);
  const pace = (rs) => (rs.length ? rs.reduce((a, r) => a + r.sec, 0) / 60 / rs.reduce((a, r) => a + r.km, 0) : NaN);
  const p30 = pace(last30), pPrev = pace(prev30), pChg = (p30 - pPrev) * 60;
  const longest = runs.reduce((a, r) => (r.km > a.km ? r : a), runs[0]);
  const total = runs.reduce((a, r) => a + r.km, 0);
  const power = songStats().filter((s) => s.n >= 2).sort((a, b) => a.v - b.v)[0] || songStats().sort((a, b) => a.v - b.v)[0];
  const lastPaces = runs.slice(0, 12).reverse().map((r) => paceU(r.pace));
  const chg = (v, good, txt2) => `<span class="chg ${good ? "up" : "down"}">${good ? "▲" : "▼"} ${txt2}</span>`;
  const tiles = [
    { l: "This week", to: dv(thisWk), fmt: (v) => v.toFixed(1) + " " + UNIT,
      s: lastWk || thisWk ? (thisWk >= lastWk ? chg(wkChg, true, `${dv(Math.abs(wkChg)).toFixed(1)} ${UNIT} vs last week`) : `${dv(lastWk - thisWk).toFixed(1)} ${UNIT} to go to match last week`) : "No runs yet",
      x: sparkBars(weeks.map(dv)), title: "Distance per week, last 10 weeks" },
    { l: "Avg pace · last 30 days", to: null, v: `${fmtPace(p30)}<span style="font-size:14px;color:var(--ink-2)">${pu()}</span>`,
      s: isFinite(pChg) ? chg(pChg, pChg < 0, `${fmtDelta(Math.abs(pChg)).slice(1)}${pu()} ${pChg < 0 ? "faster" : "slower"} than prior 30`) : `${last30.length} runs`,
      x: sparkLine(lastPaces, { invert: true }), title: "Pace of the last 12 runs (higher = faster)" },
    { l: "Longest run", to: dv(longest.km), fmt: (v) => v.toFixed(1) + " " + UNIT, s: `${fmtDate(longest.date)} · ${fmtPace(longest.pace)}${pu()}`, link: () => selectRun(longest, true) },
    { l: "Total distance", to: dv(total), fmt: (v) => Math.round(v) + " " + UNIT, s: `${runs.length} runs since ${fmtDate(runs[runs.length - 1].date)}` },
    power ? { l: "⚡ Power song", v: esc(power.name), sm: true, s: `${fmtDelta(power.v)}${pu()} vs run avg · ${power.n} run${power.n > 1 ? "s" : ""}`, link: () => $("#music-perf").scrollIntoView({ behavior: "smooth" }) } : null,
    { l: "Songs matched to runs", to: segs.length, fmt: (v) => Math.round(v), s: `${data.library.with_features}/${data.library.size} liked tracks have audio features` },
  ].filter(Boolean);
  const box = $("#kpis"); box.innerHTML = "";
  tiles.forEach((k, i) => {
    const d = document.createElement("div");
    d.className = "kpi" + (k.link ? " link" : ""); d.style.animationDelay = `${80 + i * 50}ms`;
    if (k.title) d.title = k.title;
    d.innerHTML = `<div class="l">${k.l}</div><div class="v${k.sm ? " sm" : ""}">${k.v || ""}</div><div class="s">${k.s}</div>${k.x || ""}`;
    if (k.to != null) countUp(d.querySelector(".v"), k.to, k.fmt);
    if (k.link) d.addEventListener("click", k.link);
    box.appendChild(d);
  });
}

// ───────────────────────── trends ─────────────────────────
function renderTrends() { renderCalendar(); renderHistory(); }

function renderCalendar() {
  const box = $("#calendar"), WK = 15;
  const end = addDays(mondayOf(new Date()), 6), start = addDays(mondayOf(new Date()), -7 * (WK - 1));
  const byDay = new Map();
  for (const r of S.runs) {
    if (r.date < start) continue;
    const k = r.date.toDateString(), cur = byDay.get(k) || { km: 0, runs: [] };
    cur.km += r.km; cur.runs.push(r); byDay.set(k, cur);
  }
  const mx = Math.max(5, ...[...byDay.values()].map((d) => d.km));
  const active = [...byDay.keys()].length;
  $("#calNote").textContent = `${active} running days in the last ${WK} weeks · ${fmtD(kmIn(start, WK * 7))} total. Stronger color = longer.`;
  const lw = 30, cw = clamp(Math.floor(((box.clientWidth || 500) - lw) / WK) - 4, 10, 30), gap = 4;
  const { svg } = svgBox(box, 20 + 7 * (cw + gap), lw + WK * (cw + gap));
  ["Mon", "", "Wed", "", "Fri", "", "Sun"].forEach((d, i) => d && txt(svg, 0, 20 + i * (cw + gap) + cw * 0.7, d));
  let lastMonth = -1;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  for (let wk = 0; wk < WK; wk++) {
    const ws = addDays(start, 7 * wk), x = lw + wk * (cw + gap);
    if (ws.getMonth() !== lastMonth) { txt(svg, x, 11, ws.toLocaleDateString(undefined, { month: "short" })); lastMonth = ws.getMonth(); }
    for (let dw = 0; dw < 7; dw++) {
      const day = addDays(ws, dw), y = 20 + dw * (cw + gap);
      if (day > end) continue;
      const rec = byDay.get(day.toDateString()), future = day > today;
      const sel = rec && rec.runs.includes(S.run);
      const cell = el("rect", { x, y, width: cw, height: cw, rx: 3,
        fill: rec ? seq(0.15 + 0.85 * (rec.km / mx)) : future ? "transparent" : css("--surface-2"),
        stroke: sel ? css("--ink") : future ? css("--grid") : "none", "stroke-width": sel ? 2 : 1, class: rec ? A("ap") : "" }, svg);
      if (rec) cell.style.animationDelay = `${wk * 25 + dw * 10}ms`;
      if (day.getTime() === today.getTime()) el("circle", { cx: x + cw / 2, cy: y + cw / 2, r: 2, fill: css("--ink-2") }, svg);
      cell.addEventListener("mousemove", (ev) => showTip(`<b>${day.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</b>${rec ? rec.runs.map((r) => `<div>${esc(r.name)} · ${fmtD(r.km)} · ${fmtPace(r.pace)}${pu()}${r.songs.length ? ` · ♪ ${r.songs.length}` : ""}</div>`).join("") + `<div class="m">Click to replay</div>` : `<div class="m">${future ? "Upcoming" : "Rest day"}</div>`}`, ev));
      cell.addEventListener("mouseleave", hideTip);
      if (rec) { cell.style.cursor = "pointer"; cell.addEventListener("click", () => { hideTip(); selectRun(rec.runs[0], true); }); }
    }
  }
}

function renderHistory() {
  const box = $("#histChart"), runs = [...S.runs].reverse();
  if (!runs.length) { box.innerHTML = `<div class="empty">No runs yet</div>`; return; }
  const mode = S.hist;
  const val = (r) => (mode === "km" ? dv(r.km) : mode === "pace" ? paceU(r.pace) : r.sec / 60);
  const fmtV = (v) => (mode === "km" ? v.toFixed(1) + " " + UNIT : mode === "pace" ? fmtPaceRaw(v) + pu() : fmtDur(v * 60));
  const fmtTick = (v) => (mode === "km" ? v : mode === "pace" ? fmtPaceRaw(v) : v >= 60 ? `${Math.floor(v / 60)}h${v % 60 ? String(Math.round(v % 60)).padStart(2, "0") : ""}` : `${v}m`);
  const vs = runs.map(val);
  const roll = vs.map((_, i) => { const w = vs.slice(Math.max(0, i - 4), i + 1); return w.reduce((a, b) => a + b, 0) / w.length; });
  const best = mode === "pace" ? Math.min(...vs) : Math.max(...vs);
  $("#histNote").textContent = `${runs.length} runs · ${mode === "pace" ? "fastest" : "best"} ${fmtV(best)}`;
  const { svg, w, h } = svgBox(box, 260);
  const m = { l: 48, r: 14, t: 14, b: 26 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const t0 = +runs[0].date, t1 = Math.max(+runs[runs.length - 1].date, t0 + DAY);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  const pad = (hi - lo) * 0.12 || 1; lo = mode === "pace" ? lo - pad : Math.max(0, lo - pad); hi += pad;
  const X = (d) => m.l + ((+d - t0) / (t1 - t0)) * iw;
  const Y = mode === "pace" ? (v) => m.t + ((v - lo) / (hi - lo)) * ih : (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
  niceTicks(lo, hi, 4).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); txt(svg, m.l - 6, Y(v) + 4, fmtTick(v), { "text-anchor": "end" }); });
  for (let d = new Date(new Date(t0).getFullYear(), new Date(t0).getMonth() + 1, 1); +d <= t1; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) {
    el("line", { x1: X(d), x2: X(d), y1: m.t, y2: m.t + ih, stroke: css("--grid"), "stroke-dasharray": "2 3" }, svg);
    txt(svg, X(d) + 3, h - 8, d.toLocaleDateString(undefined, { month: "short" }));
  }
  if (mode === "pace") txt(svg, m.l + 4, m.t + 10, "faster ↑");
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
  const avg = el("polyline", { points: runs.map((r, i) => `${X(r.date).toFixed(1)},${Y(roll[i]).toFixed(1)}`).join(" "), fill: "none", stroke: css("--s2"), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
  drawLine(avg);
  runs.forEach((r, i) => {
    const c = el("circle", { cx: X(r.date), cy: Y(vs[i]), r: 5, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2, class: A("ap") }, svg);
    c.style.animationDelay = `${i * 20}ms`;
  });
  const si = runs.indexOf(S.run);
  el("circle", { r: 9, fill: "none", stroke: css("--ink"), "stroke-width": 2, opacity: si >= 0 ? 1 : 0, cx: si >= 0 ? X(S.run.date) : 0, cy: si >= 0 ? Y(vs[si]) : 0 }, svg);
  const hov = el("circle", { r: 7, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2, opacity: 0 }, svg);
  const vl = el("line", { y1: m.t, y2: m.t + ih, stroke: css("--ink-2"), opacity: 0 }, svg);
  svg.insertBefore(vl, svg.firstChild);
  const hit = el("rect", { x: m.l - 8, y: 0, width: iw + 16, height: h, fill: "transparent", class: "click" }, svg);
  const nearest = (ev) => { const r = svg.getBoundingClientRect(), x = ev.clientX - r.left; let bi = 0, bd = Infinity; runs.forEach((run, i) => { const dd = Math.abs(X(run.date) - x); if (dd < bd) { bd = dd; bi = i; } }); return bi; };
  hit.addEventListener("mousemove", (ev) => {
    const i = nearest(ev), r = runs[i];
    hov.setAttribute("cx", X(r.date)); hov.setAttribute("cy", Y(vs[i])); hov.setAttribute("opacity", 1);
    vl.setAttribute("x1", X(r.date)); vl.setAttribute("x2", X(r.date)); vl.setAttribute("opacity", 0.5);
    showTip(`<b>${esc(r.name)}</b><div class="m">${r.date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</div><div>${fmtD(r.km)} · ${fmtPace(r.pace)}${pu()} · ${fmtDur(r.sec)}</div><div class="m">5-run avg ${fmtV(roll[i])}${r.songs.length ? ` · ♪ ${r.songs.length} songs` : ""}</div>`, ev);
  });
  hit.addEventListener("mouseleave", () => { hov.setAttribute("opacity", 0); vl.setAttribute("opacity", 0); hideTip(); });
  hit.addEventListener("click", (ev) => { hideTip(); selectRun(runs[nearest(ev)], true); });
}

// ───────────────────────── map ─────────────────────────
let map, baseLayer, routeLayer, cursor;
function tileUrl() {
  if (S.base === "topo") return ["https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", '© OpenStreetMap, SRTM · © <a href="https://opentopomap.org">OpenTopoMap</a>', 17];
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  return [`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, "© OpenStreetMap © CARTO", 19];
}
function setBase() {
  if (!map) return;
  if (baseLayer) map.removeLayer(baseLayer);
  const [u, a, mz] = tileUrl();
  baseLayer = L.tileLayer(u, { attribution: a, maxZoom: mz, subdomains: "abc" }).addTo(map);
}
function songAt(run, t) { return run.songs.find((s) => t >= s.s && t < s.e); }

function drawRoute(fit) {
  const run = S.run;
  if (!map) {
    map = L.map("map", { zoomControl: true, scrollWheelZoom: false });
    setBase();
  }
  if (routeLayer) map.removeLayer(routeLayer);
  routeLayer = L.layerGroup().addTo(map);
  const n = run.lat.length, cm = css("--muted");
  const ps = run.p.map(paceU);
  const pMin = Math.min(...ps.filter((p) => p > paceU(2.5))), pMax = Math.min(paceU(9), Math.max(...ps.filter((p) => p < paceU(12))));
  const eMin = Math.min(...run.ele), eMax = Math.max(...run.ele);
  for (let i = 0; i < n - 1; i++) {
    const tm = (run.t[i] + run.t[i + 1]) / 2, sg = songAt(run, tm);
    let color, weight = 5, op = 0.95;
    if (S.colorMode === "song") { color = sg ? divColor(sg.delta) : cm; if (!sg) op = 0.5; }
    else if (S.colorMode === "pace") color = seq(1 - (clamp(ps[i + 1], pMin, pMax) - pMin) / (pMax - pMin || 1));
    else color = seq((run.ele[i] - eMin) / (eMax - eMin || 1));
    if (S.hl) { if (sg === S.hl) weight = 9; else op *= 0.45; }
    const line = L.polyline([[run.lat[i], run.lon[i]], [run.lat[i + 1], run.lon[i + 1]]], { color, weight, opacity: op, lineCap: "round" }).addTo(routeLayer);
    const info = `<b>${fmtD(run.d[i], 2)}</b> · ${run.ele[i].toFixed(0)} m · ${fmtPace(run.p[i + 1])}${pu()}` + (sg ? `<br>♪ ${esc(sg.name)} — ${esc(sg.artist)}` : "");
    line.bindTooltip(info, { sticky: true });
    line.on("mouseover", () => { if (!S.playing) setCursor(i); });
    if (sg) line.on("click", () => highlight(sg));
  }
  if (S.colorMode === "song") {
    for (const sg of run.songs) {
      const la = interp(run.t, run.lat, sg.s), lo = interp(run.t, run.lon, sg.s);
      L.marker([la, lo], { icon: L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9],
        html: `<div style="width:18px;height:18px;border-radius:50%;background:${css("--surface")};border:2px solid ${divColor(sg.delta)};font:600 10px system-ui;display:grid;place-items:center;color:${css("--ink")}">${sg.idx}</div>` }) })
        .bindTooltip(`♪ ${esc(sg.name)}<br><span style="opacity:.7">${esc(sg.artist)} · ${fmtDelta(sg.delta)}${pu()}</span>`).on("click", () => highlight(sg)).addTo(routeLayer);
    }
  }
  const dot = (i, label) => L.circleMarker([run.lat[i], run.lon[i]], { radius: 6, color: css("--surface"), weight: 2, fillColor: css("--ink"), fillOpacity: 1 }).bindTooltip(label).addTo(routeLayer);
  dot(0, "Start"); dot(n - 1, "Finish");
  cursor = L.circleMarker([run.lat[0], run.lon[0]], { radius: 8, color: css("--surface"), weight: 3, fillColor: css("--ink"), fillOpacity: 0, opacity: 0, interactive: false }).addTo(routeLayer);
  if (S.ci != null) moveCursor(S.ci);
  if (fit) map.fitBounds(L.latLngBounds(run.lat.map((la, i) => [la, run.lon[i]])), { padding: [20, 20] });

  const lg = $("#mapLegend");
  if (S.colorMode === "song") lg.innerHTML = run.songs.length
    ? `<span>Song pace</span><span>faster</span><span class="ramp div"></span><span>slower than run average</span><span><span class="sw" style="background:${cm}"></span>No song captured</span><span>Numbers match the table · click a song to isolate it</span>`
    : `<span>No plays were captured during this run. The logger started collecting on ${S.plays.length ? fmtDate(new Date(S.plays[0][1])) : "—"}.</span>`;
  else if (S.colorMode === "pace") lg.innerHTML = `<span>Pace</span><span>${fmtPaceRaw(pMax)}</span><span class="ramp"></span><span>${fmtPaceRaw(pMin)} ${pu()} (faster)</span>`;
  else lg.innerHTML = `<span>Elevation</span><span>${eMin.toFixed(0)} m</span><span class="ramp"></span><span>${eMax.toFixed(0)} m</span>`;
}
function moveCursor(i) {
  if (!cursor) return;
  if (i == null) { cursor.setStyle({ fillOpacity: 0, opacity: 0 }); return; }
  const ll = [S.run.lat[i], S.run.lon[i]];
  cursor.setLatLng(ll); cursor.setStyle({ fillOpacity: 1, opacity: 1 }); cursor.bringToFront();
  if (S.playing && map.panInside) map.panInside(ll, { padding: [60, 60], animate: false });
}
function highlight(sg) {
  S.hl = S.hl === sg ? null : sg;
  drawRoute(false); renderSongTable();
  if (S.hl) {
    const r = S.run, pts = [];
    for (let i = 0; i < r.t.length; i++) if (r.t[i] >= sg.s && r.t[i] <= sg.e) pts.push([r.lat[i], r.lon[i]]);
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 16 });
    if (!S.playing) { S.pt = sg.s; setCursor(idxAt(r.t, sg.s)); }
  }
}

// ───────────────────────── replay ─────────────────────────
// One cursor (a sample index into the run) drives the map, ribbon, charts and now-playing card.
let raf = 0, lastTs = 0, lastSong = null;
function setCursor(i) {
  S.ci = i;
  S.cur.forEach((c) => c.set(i));
  moveCursor(i);
  renderNowPlaying(i);
}
function restoreCursor() { setCursor(S.pt == null ? null : idxAt(S.run.t, S.pt)); }
function togglePlay() {
  if (S.playing) return stopPlay();
  const T = S.run.t[S.run.t.length - 1];
  if (S.pt == null || S.pt >= T - 1) S.pt = 0;
  S.playing = true; lastTs = performance.now();
  $("#playBtn").textContent = "❚❚"; $("#playBtn").setAttribute("aria-label", "Pause replay");
  $("#nowPlaying").classList.add("playing");
  raf = requestAnimationFrame(tick);
}
function stopPlay() {
  if (!S || !S.playing) return;
  S.playing = false; cancelAnimationFrame(raf);
  $("#playBtn").textContent = "▶"; $("#playBtn").setAttribute("aria-label", "Play replay");
  $("#nowPlaying").classList.remove("playing");
}
function tick(now) {
  const T = S.run.t[S.run.t.length - 1];
  S.pt = Math.min(T, S.pt + ((now - lastTs) / 1000) * S.speed);
  lastTs = now;
  $("#scrub").value = S.pt;
  setCursor(idxAt(S.run.t, S.pt));
  if (S.pt >= T) return stopPlay();
  raf = requestAnimationFrame(tick);
}
function renderNowPlaying(i) {
  const run = S.run, box = $("#nowPlaying");
  if (!box.querySelector(".eq")) box.innerHTML = `<div class="eq"><i></i><i></i><i></i><i></i></div><div class="np-body"></div><div class="np-stats"></div>`;
  const body = box.querySelector(".np-body"), stats = box.querySelector(".np-stats");
  if (i == null) {
    lastSong = null;
    body.innerHTML = `<div class="k">${run.songs.length ? `${run.songs.length} songs on this run` : "No songs captured"}</div><div class="t">Press play to replay the run${run.songs.length ? " with its soundtrack" : ""}</div><div class="a">Space bar works too · hover the charts to scrub</div>`;
    stats.innerHTML = `<div><b>${fmtD(run.km)}</b><span>distance</span></div><div><b>${fmtDur(run.sec)}</b><span>time</span></div>`;
    return;
  }
  const t = run.t[i], sg = songAt(run, t);
  if (sg !== lastSong && sg && S.playing && !reducedMotion()) { box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash"); }
  if (sg !== lastSong) $$("#songTable tr.song").forEach((tr) => tr.classList.toggle("now", sg && +tr.dataset.i === sg.idx));
  lastSong = sg;
  body.innerHTML = `<div class="k">${sg ? `Now playing · song ${sg.idx} of ${run.songs.length}${isFinite(sg.delta) ? ` · ${fmtDelta(sg.delta)}${pu()} vs avg` : ""}` : "Between songs"}</div>
    <div class="t">${sg ? esc(sg.name) : "—"}</div><div class="a">${sg ? esc(sg.artist) : "No play captured here"}</div>`;
  stats.innerHTML = `<div><b>${fmtClock(t)}</b><span>elapsed</span></div><div><b>${fmtD(run.d[i], 2)}</b><span>distance</span></div><div><b>${fmtPace(run.p[i])}</b><span>pace${pu()}</span></div><div><b>${run.ele[i].toFixed(0)} m</b><span>elevation</span></div>`;
}

// ───────────────────────── run charts ─────────────────────────
function lineChart(container, run, ys, { h = 180, area = false, avg = null, fmtY = (v) => v.toFixed(0), invert = false } = {}) {
  const { svg, w } = svgBox(container, h);
  const m = { l: 44, r: 12, t: 22, b: 24 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const xs = run.d.map(dv), x1 = xs[xs.length - 1] || 1;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (avg != null) { lo = Math.min(lo, avg); hi = Math.max(hi, avg); }
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = (v) => m.l + (v / x1) * iw;
  const Y = invert ? (v) => m.t + ((v - lo) / (hi - lo)) * ih : (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
  // song bands
  run.songs.forEach((sg) => {
    const a = X(dv(sg.d0)), b = X(dv(sg.d1));
    el("rect", { x: a, y: m.t, width: Math.max(1, b - a - 2), height: ih, fill: sg.idx % 2 ? css("--band-a") : css("--band-b") }, svg);
    el("rect", { x: a, y: m.t - 4, width: Math.max(1, b - a - 2), height: 3, rx: 1.5, fill: divColor(sg.delta) }, svg);
    if (b - a > 14) txt(svg, (a + b) / 2, m.t - 8, sg.idx, { "text-anchor": "middle" });
  });
  niceTicks(lo, hi, 4).forEach((v) => {
    el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg);
    txt(svg, m.l - 6, Y(v) + 4, fmtY(v), { "text-anchor": "end" });
  });
  niceTicks(0, x1, Math.max(3, Math.floor(iw / 90))).forEach((v) => txt(svg, X(v), h - 6, v + (v === 0 ? "" : " " + UNIT), { "text-anchor": "middle" }));
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
  const pts = xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" ");
  if (area) el("polygon", { points: `${X(0)},${m.t + ih} ${pts} ${X(x1)},${m.t + ih}`, fill: css("--s1"), "fill-opacity": 0.14 }, svg);
  drawLine(el("polyline", { points: pts, fill: "none", stroke: css("--s1"), "stroke-width": 2, "stroke-linejoin": "round" }, svg));
  if (avg != null) {
    el("line", { x1: m.l, x2: w - m.r, y1: Y(avg), y2: Y(avg), stroke: css("--ink-2"), "stroke-dasharray": "4 4" }, svg);
    txt(svg, w - m.r, Y(avg) - 5, `avg ${fmtY(avg)}`, { "text-anchor": "end", class: "ink" });
  }
  const vline = el("line", { y1: m.t, y2: m.t + ih, stroke: css("--ink"), "stroke-width": 1.5, opacity: 0 }, svg);
  const vdot = el("circle", { r: 5, fill: css("--ink"), stroke: css("--surface"), "stroke-width": 2, opacity: 0 }, svg);
  const hit = el("rect", { x: m.l, y: 0, width: iw, height: h, fill: "transparent" }, svg);
  const handle = { set(i) {
    if (i == null) { vline.setAttribute("opacity", 0); vdot.setAttribute("opacity", 0); return; }
    vline.setAttribute("x1", X(xs[i])); vline.setAttribute("x2", X(xs[i])); vline.setAttribute("opacity", 0.7);
    vdot.setAttribute("cx", X(xs[i])); vdot.setAttribute("cy", Y(ys[i])); vdot.setAttribute("opacity", 1);
  } };
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), d = ((ev.clientX - r.left - m.l) / iw) * x1;
    const i = idxAt(xs, d);
    if (!S.playing) setCursor(i); else handle.set(i);
    const sg = songAt(run, run.t[i]);
    showTip(`<b>${xs[i].toFixed(2)} ${UNIT}</b><div>${run.ele[i].toFixed(0)} m · ${fmtPace(run.p[i])}${pu()}</div>${sg ? `<div class="m">♪ ${sg.idx}. ${esc(sg.name)}</div>` : ""}`, ev);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); if (!S.playing) restoreCursor(); });
  hit.addEventListener("click", (ev) => { const r = svg.getBoundingClientRect(); S.pt = run.t[idxAt(xs, ((ev.clientX - r.left - m.l) / iw) * x1)]; $("#scrub").value = S.pt; });
  return handle;
}

function renderRun(fit) {
  const run = S.run;
  const sel = $("#runSel");
  sel.innerHTML = S.runs.map((r, i) => `<option value="${i}">${fmtDate(r.date)} · ${esc(r.name)} · ${fmtD(r.km)}${r.songs.length ? ` · ♪ ${r.songs.length}` : ""}</option>`).join("");
  sel.value = S.runs.indexOf(run);
  $("#stravaLink").href = run.url;
  const T = run.t[run.t.length - 1];
  $("#scrub").max = T; $("#scrub").value = S.pt ?? 0;
  drawRoute(fit || !map);
  S.cur = [];
  if (run.songs.length) S.cur.push(ribbon($("#runRibbon"), run, { onPick: highlight }));
  else $("#runRibbon").innerHTML = `<div class="empty">No songs were captured on this run.</div>`;
  S.cur.push(lineChart($("#elevChart"), run, run.ele, { area: true, fmtY: (v) => v.toFixed(0) }));
  const pace = smooth(run.p.map((p) => paceU(clamp(p, 3, 10))), 3);
  S.cur.push(lineChart($("#paceChart"), run, pace, { h: 160, avg: paceU(run.pace), fmtY: fmtPaceRaw, invert: true }));
  renderSongTable();
  lastSong = undefined;
  restoreCursor();
}

function renderSongTable() {
  const run = S.run, box = $("#songTable");
  $("#songNote").textContent = run.songs.length
    ? `${run.songs.length} songs · click a row to isolate it on the map. Δ is seconds${pu()} vs this run's ${fmtPace(run.epace)}${pu()} average including stops.`
    : "No plays were captured during this run.";
  if (!run.songs.length) { box.innerHTML = `<div class="empty">Keep the play logger running and songs will show up here on future runs.</div>`; return; }
  const curSong = S.ci != null ? songAt(run, run.t[S.ci]) : null;
  box.innerHTML = `<table><thead><tr><th>#</th><th>Song</th><th>Artist</th><th class="num">${UNIT}</th><th class="num">Pace</th><th class="num">Δ s${pu()}</th><th class="num">Grade</th><th class="num">BPM</th><th class="num">Energy</th></tr></thead><tbody>${
    run.songs.map((sg) => `<tr class="song${S.hl === sg ? " on" : ""}${curSong === sg ? " now" : ""}" data-i="${sg.idx}">
      <td><span class="sw" style="background:${divColor(sg.delta)}"></span>${sg.idx}</td><td>${esc(sg.name)}</td><td style="color:var(--ink-2)">${esc(sg.artist)}</td>
      <td class="num">${dv(sg.d0).toFixed(1)}–${dv(sg.d1).toFixed(1)}</td><td class="num">${fmtPace(sg.pace)}</td>
      <td class="num" style="color:${sg.delta < 0 ? "var(--good)" : "var(--bad)"}">${fmtDelta(sg.delta)}</td>
      <td class="num">${fmtGrade(sg.grade)}</td><td class="num">${sg.f && sg.f.tempo ? sg.f.tempo.toFixed(0) : "–"}</td><td class="num">${sg.f && sg.f.energy != null ? sg.f.energy.toFixed(2) : "–"}</td></tr>`).join("")
  }</tbody></table>`;
  box.querySelectorAll("tr.song").forEach((tr) => tr.addEventListener("click", () => highlight(run.songs[+tr.dataset.i - 1])));
}

// ───────────────────────── listening ─────────────────────────
function renderListening() {
  const { plays, segs, live, data } = S;
  const first = plays[0], last = plays[plays.length - 1];
  $("#listenLede").textContent = plays.length
    ? `${plays.length} plays logged from ${fmtDate(new Date(first[1]))} to ${fmtDate(new Date(last[1]))}${live.connected ? `, ${live.plays.length} of them pulled live from the cloud logger since the last sync` : ""}. ${segs.length} overlapped a run.`
    : "No plays logged yet.";
  const bySong = new Map(), byArtist = new Map();
  for (const [id, , , name, artist] of plays) {
    const t = S.tracks[id] || { n: name, a: artist };
    const cur = bySong.get(id) || { label: t.n || name, sub: t.a || artist, value: 0 };
    cur.value++; bySong.set(id, cur);
    for (const a of String(t.a || artist || "").split(", ").filter(Boolean)) byArtist.set(a, (byArtist.get(a) || 0) + 1);
  }
  const topS = [...bySong.values()].sort((a, b) => b.value - a.value).slice(0, 10).map((d) => ({ ...d, text: `${d.value} play${d.value > 1 ? "s" : ""}` }));
  barList($("#topSongs"), topS);
  barList($("#topArtists"), [...byArtist].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([a, v]) => ({ label: a, value: v, text: `${v} play${v > 1 ? "s" : ""}` })));
  barList($("#libArtists"), data.library.top_artists.slice(0, 10).map(([a, v]) => ({ label: a, value: v, text: `${v} tracks` })), { color: css("--s3") });

  // by hour, stacked: during run vs other
  const inRun = (p) => S.runs.some((r) => { const end = p[1] / 1000; return end > r.t0 && end - p[2] / 1000 < r.t0 + r.t[r.t.length - 1]; });
  const H = Array.from({ length: 24 }, () => [0, 0]);
  for (const p of plays) H[new Date(p[1]).getHours()][inRun(p) ? 0 : 1]++;
  const box = $("#byHour"), { svg, w, h } = svgBox(box, 170);
  const m = { l: 28, r: 8, t: 8, b: 22 }, iw = w - m.l - m.r, ih = h - m.t - m.b, bw = iw / 24;
  const mx = Math.max(1, ...H.map((x) => x[0] + x[1]));
  const Y = (v) => (v / mx) * ih;
  niceTicks(0, mx, 3).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih - Y(v), y2: m.t + ih - Y(v), stroke: css("--grid") }, svg); txt(svg, m.l - 6, m.t + ih - Y(v) + 4, v, { "text-anchor": "end" }); });
  H.forEach(([a, b], i) => {
    const x = m.l + i * bw + 2, bwi = Math.max(2, bw - 4), g = el("g", { class: A("ay") }, svg);
    g.style.animationDelay = `${i * 20}ms`;
    if (a) el("rect", { x, y: m.t + ih - Y(a), width: bwi, height: Y(a), rx: 2, fill: css("--s1") }, g);
    if (b) el("rect", { x, y: m.t + ih - Y(a) - Y(b), width: bwi, height: Math.max(0, Y(b) - (a ? 2 : 0)), rx: 2, fill: css("--axis") }, g);
    if (i % 3 === 0) txt(svg, x + bwi / 2, h - 6, i === 0 ? "12a" : i === 12 ? "12p" : i < 12 ? i + "a" : i - 12 + "p", { "text-anchor": "middle" });
    el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => showTip(`<b>${i}:00–${i}:59</b><div>${a} during a run · ${b} other</div>`, ev));
    g.addEventListener("mouseleave", hideTip);
  });
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
}

// ───────────────────────── music × pace ─────────────────────────
function renderMusicPerf() { renderSongDelta(); renderTempoScatter(); }

function renderSongDelta() {
  const box = $("#songDelta"), all = songStats();
  if (!all.length) { box.innerHTML = `<div class="empty">No songs matched to runs yet</div>`; $("#deltaNote").textContent = ""; return; }
  const mode = S.deltaMode;
  const items = (mode === "fast" ? all.sort((a, b) => a.v - b.v) : mode === "slow" ? all.sort((a, b) => b.v - a.v) : all.sort((a, b) => b.n - a.n || a.v - b.v)).slice(0, 12);
  $("#deltaNote").textContent = `${all.length} songs, ${mode === "fast" ? "the 12 you ran fastest to" : mode === "slow" ? "the 12 you ran slowest to" : "the 12 that played on the most runs"}. Seconds${pu()} vs run average.`;
  const rowH = 30, labelW = Math.min(200, (box.clientWidth || 500) * 0.42);
  const { svg, w } = svgBox(box, items.length * rowH + 8);
  const vals = items.map((d) => deltaU(d.v)), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals, lo + 5);
  const x0 = labelW + 10, iw = w - x0 - 10, padL = lo < 0 ? 40 : 4, padR = hi > 0 ? 40 : 4;
  const XV = (v) => x0 + padL + ((v - lo) / (hi - lo)) * (iw - padL - padR), cx = XV(0);
  el("line", { x1: cx, x2: cx, y1: 0, y2: items.length * rowH + 4, stroke: css("--axis") }, svg);
  items.forEach((s, i) => {
    const v = deltaU(s.v), y = i * rowH + 4, g = el("g", {}, svg), len = Math.abs(XV(v) - cx);
    const mc = Math.floor(labelW / 6.4), lab = s.name.length > mc - 3 ? s.name.slice(0, mc - 4) + "…" : s.name;
    txt(g, labelW, y + 12, lab, { "text-anchor": "end", class: "ink" });
    txt(g, labelW, y + 24, `${s.n} run${s.n > 1 ? "s" : ""}`, { "text-anchor": "end", style: "font-size:10px" });
    const bar = el("rect", { x: v < 0 ? cx - len : cx, y: y + 7, width: Math.max(2, len), height: 14, rx: 3, fill: css(v < 0 ? "--div-neg" : "--div-pos"), class: A(v < 0 ? "axr" : "ax") }, g);
    bar.style.animationDelay = `${i * 40}ms`;
    txt(g, v < 0 ? cx - len - 5 : cx + len + 5, y + 18, (v < 0 ? "−" : "+") + Math.abs(v).toFixed(0) + "s", { "text-anchor": v < 0 ? "end" : "start" });
    el("rect", { x: 0, y, width: w, height: rowH, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => { bar.setAttribute("opacity", 0.8); showTip(`<b>${esc(s.name)}</b><div class="m">${esc(s.artist)}</div><div>${fmtDelta(s.v)}${pu()} vs run average, over ${s.n} run${s.n > 1 ? "s" : ""}</div>${s.segs.slice(0, 4).map((x) => `<div class="m">${fmtDate(x.run.date)} · ${fmtPace(x.pace)}${pu()} · ${fmtDelta(x.delta)}</div>`).join("")}<div class="m">Click to open the latest run</div>`, ev); });
    g.addEventListener("mouseleave", () => { bar.removeAttribute("opacity"); hideTip(); });
    g.style.cursor = "pointer";
    g.addEventListener("click", () => { hideTip(); const sg = s.segs.reduce((a, x) => (x.run.date > a.run.date ? x : a)); selectRun(sg.run, true); highlight(sg); });
  });
  txt(svg, cx - 6, items.length * rowH + 6, "← faster", { "text-anchor": "end", style: "font-size:10px" });
  txt(svg, cx + 6, items.length * rowH + 6, "slower →", { style: "font-size:10px" });
  svg.setAttribute("height", items.length * rowH + 10); svg.setAttribute("viewBox", `0 0 ${w} ${items.length * rowH + 10}`);
}

function renderTempoScatter() {
  const sb = $("#tempoScatter"), pts = S.segs.filter((s) => isFinite(s.pace) && s.f && s.f.tempo);
  if (!pts.length) { sb.innerHTML = `<div class="empty">No featured songs on runs yet</div>`; return; }
  const { svg, w, h } = svgBox(sb, 280);
  const m = { l: 48, r: 12, t: 12, b: 32 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const tx = pts.map((p) => p.f.tempo), py = pts.map((p) => paceU(p.pace));
  const sorted = [...py].sort((a, b) => a - b), cap = sorted[Math.floor(sorted.length * 0.95)] + 0.4;
  const xl = Math.min(...tx) - 8, xh = Math.max(...tx) + 8, yl = Math.min(...py) - 0.15, yh = Math.min(Math.max(...py), cap) + 0.15;
  const X = (v) => m.l + ((v - xl) / (xh - xl)) * iw, Y = (v) => m.t + ((v - yl) / (yh - yl)) * ih; // faster at top
  niceTicks(yl, yh, 4).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); txt(svg, m.l - 6, Y(v) + 4, fmtPaceRaw(v), { "text-anchor": "end" }); });
  niceTicks(xl, xh, 5).forEach((v) => txt(svg, X(v), h - 14, v, { "text-anchor": "middle" }));
  txt(svg, m.l + iw / 2, h - 1, "Tempo (BPM)", { "text-anchor": "middle" });
  txt(svg, 4, m.t + 4, "faster ↑");
  pts.forEach((p, i) => {
    const clipped = paceU(p.pace) > yh;
    const c = el("circle", { cx: X(p.f.tempo), cy: Y(Math.min(paceU(p.pace), yh)), r: 6, fill: clipped ? css("--surface") : divColor(p.delta), stroke: clipped ? divColor(p.delta) : css("--surface"), "stroke-width": 2, class: A("ap") + " click" }, svg);
    c.style.animationDelay = `${i * 8}ms`;
    c.addEventListener("mousemove", (ev) => { c.setAttribute("r", 8); showTip(`<b>${esc(p.name)}</b><div class="m">${esc(p.artist)}</div><div>${p.f.tempo.toFixed(0)} BPM · ${fmtPace(p.pace)}${pu()} · energy ${p.f.energy.toFixed(2)}</div><div class="m">${fmtDate(p.run.date)} · ${fmtDelta(p.delta)}${pu()} vs avg${clipped ? " · off the chart (probably a stop)" : ""}</div>`, ev); });
    c.addEventListener("mouseleave", () => { c.setAttribute("r", 6); hideTip(); });
    c.addEventListener("click", () => { hideTip(); selectRun(p.run, true); highlight(p); });
  });
}

// ───────────────────────── race countdown + training plan ─────────────────────────
// Philadelphia Marathon: Sunday before Thanksgiving, 7:00 AM ET start.
const RACE = { name: "Philadelphia Marathon", date: new Date("2026-11-22T07:00:00-05:00"), km: 42.195 };
const fmtHMS = (sec) => { const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60); return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`; };
const kmMi = (km) => { const other = UNIT === "mi" ? `${km.toFixed(km < 10 ? 1 : 0)} km` : `${(km * MI).toFixed(1)} mi`; return `${fmtD(km)} <span class="d">(${other})</span>`; };

// Where the runner is now, from the logged runs.
function baseline(runs) {
  const now = Date.now();
  const last4 = runs.filter((r) => now - r.date < 28 * DAY);
  const weekly = last4.reduce((a, r) => a + r.km, 0) / 4;
  const longest = Math.max(0, ...runs.filter((r) => now - r.date < 42 * DAY).map((r) => r.km));
  // Riegel projection (t2 = t1 · (d2/d1)^1.06) from the strongest run ≥ 8 km in the last 8 weeks.
  let pred = null;
  for (const r of runs) {
    if (r.km < 8 || now - r.date > 56 * DAY) continue;
    const sec = r.sec * (RACE.km / r.km) ** 1.06;
    if (!pred || sec < pred.sec) pred = { sec, from: r };
  }
  return { weekly, longest, pred };
}

// Plan settings are saved server-side (POST /api/dash → Redis) so every device sees the same plan.
const PLAN_DEFAULTS = { goal_sec: null, runs_per_week: 4, long_day: "sun", peak_cap_km: 32, sessions: {} };
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseHMS = (s) => {
  const m = String(s).trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return null;
  const sec = +m[1] * 3600 + +m[2] * 60 + +(m[3] || 0);
  return sec >= 7200 && sec <= 25200 ? sec : null;
};

// Weekday offsets (Mon = 0) for the non-long runs, by long-run day and runs per week. First is the quality day.
const LAYOUT = {
  sun: { 3: [1, 3], 4: [2, 1, 4], 5: [1, 0, 3, 5] },
  sat: { 3: [1, 3], 4: [1, 0, 3], 5: [1, 0, 2, 4] },
};

// Long run climbs ≤ 3.2 km/week to the peak cap, peaks two weeks out, with a cutback mid-build;
// then a two-week taper. Midweek volume grows ~8%/week from the current base.
function buildPlan(b, cfg) {
  const w0 = mondayOf(new Date()), raceWk = mondayOf(RACE.date);
  const n = Math.max(1, Math.round((raceWk - w0) / WEEK) + 1);
  const goal = cfg.goal_sec || (b.pred && b.pred.sec);
  const mp = goal ? goal / 60 / RACE.km : 5.6;
  const P = { easy: mp + 0.6, long: mp + 0.45, mp, tempo: mp - 0.3 };
  const nBuild = Math.max(0, n - 2);
  const cut = nBuild >= 5 ? Math.floor(nBuild / 2) : -1;
  const L0 = Math.max(12, b.longest), STEP = 3.2;
  const climbs = nBuild - (cut >= 0 ? 1 : 0);
  const peak = Math.max(L0, Math.min(cfg.peak_cap_km, L0 + STEP * climbs));
  const step = climbs ? (peak - L0) / climbs : 0;
  const rpw = cfg.runs_per_week, layout = LAYOUT[cfg.long_day][rpw], longDay = cfg.long_day === "sat" ? 5 : 6;
  const mid0 = clamp(b.weekly, 18, 40) * { 3: 0.75, 4: 1, 5: 1.15 }[rpw];
  const at = (p) => `${fmtPace(p)}${pu()}`;
  const q = [
    (km) => ({ type: "Tempo", desc: `${fmtD(2)} easy · ${fmtD(Math.max(3, Math.round(km - 4)))} @ ${at(P.tempo)} · ${fmtD(2)} easy` }),
    (km) => ({ type: "Hills", desc: `${fmtD(2)} easy · 6–8 × 90 s hard uphill, jog down · rest easy` }),
    (km) => ({ type: "Marathon pace", desc: `${fmtD(2)} easy · ${fmtD(Math.max(4, Math.round(km - 3)))} @ ${at(P.mp)} · ${fmtD(1)} easy` }),
  ];
  const easy = { type: "Easy", desc: `Easy @ ${at(P.easy)}` };
  const weeks = [];
  let L = L0, climb = 0;
  for (let i = 0; i < n; i++) {
    const start = addDays(w0, 7 * i), out = n - 1 - i;
    let s, tag = "";
    if (out === 0) {
      tag = "Race week";
      s = [
        { role: "rw1", dow: 1, km: 8, type: "Easy + strides", desc: `Easy @ ${at(P.easy)} with 3 × ${fmtD(1.5)} @ ${at(P.mp)}` },
        { role: "rw2", dow: 3, km: 5, ...easy },
        { role: "rw3", dow: 5, km: 3, type: "Shakeout", desc: "Very easy, 4 short strides" },
        { role: "race", dow: 6, km: RACE.km, type: "Race", desc: `Target ${at(P.mp)} → ${goal ? fmtHMS(goal) : "–"}`, race: true },
      ];
    } else {
      let long, mid;
      if (out === 1) { long = Math.round(peak * 0.6); mid = mid0 * 0.75; tag = "Taper"; }
      else if (i === cut) { long = Math.round(L * 0.75); mid = mid0 * (1 + 0.08 * i) * 0.8; tag = "Cutback"; }
      else { climb++; L = L0 + step * climb; long = Math.round(L); mid = mid0 * (1 + 0.08 * i); if (out === 2) tag = "Peak"; }
      const qk = Math.round(mid * 0.4), ek = Math.max(5, Math.round((mid * 0.6) / (rpw - 2)));
      const qs = out === 1 ? { type: "Marathon pace", desc: `${fmtD(2)} easy · ${fmtD(5)} @ ${at(P.mp)} · ${fmtD(1)} easy` } : q[i % 3](qk);
      const mpFinish = !tag && climb >= 3 ? ` · last ${fmtD(Math.min(8, climb + 2))} @ ${at(P.mp)}` : "";
      s = [
        { role: "q", dow: layout[0], km: qk, ...qs },
        ...layout.slice(1).map((dow, j) => ({ role: "e" + (j + 1), dow, km: ek, ...easy })),
        { role: "long", dow: longDay, km: long, type: "Long", desc: `Long @ ${at(P.long)}${mpFinish}` },
      ];
    }
    // apply saved per-session edits
    for (const x of s) {
      x.key = `${isoDay(start)}|${x.role}`;
      const o = (!x.race && cfg.sessions[x.key]) || {};
      x.status = o.status || null;
      x.dowOrig = x.dow;
      x.moved = o.move != null && o.move !== x.dow;
      if (o.move != null) x.dow = o.move;
      x.d = x.race ? RACE.date : addDays(start, x.dow);
    }
    s.sort((a, b2) => a.dow - b2.dow);
    weeks.push({ start, out, sessions: s, tag, km: s.reduce((a, x) => a + (x.status === "skipped" ? 0 : x.km), 0) });
  }
  return { weeks, P, peak, n, goal };
}

// ── settings panel (wired once) ──
let saveTimer, raceTimer;
function initPlanSettings() {
  S.plan = { ...PLAN_DEFAULTS, ...(S.data.plan || {}) };
  S.plan.sessions = { ...(S.plan.sessions || {}) };
  const segs = [["#pRpw", "runs_per_week", Number], ["#pLong", "long_day", String]];
  for (const [sel, key, cast] of segs) {
    $$(`${sel} button`).forEach((b) => b.addEventListener("click", () => { S.plan[key] = cast(b.dataset.v); planChanged(); }));
  }
  $("#pCap").addEventListener("input", () => { S.plan.peak_cap_km = +$("#pCap").value; planChanged(); });
  $("#pGoal").addEventListener("change", () => {
    const v = $("#pGoal").value.trim();
    if (!v) { S.plan.goal_sec = null; planChanged(); return; }
    const sec = parseHMS(v);
    if (sec == null) { saveStatus("Goal must look like 3:55:00 (between 2:00 and 7:00)", true); return; }
    S.plan.goal_sec = sec; planChanged();
  });
  $("#pReset").addEventListener("click", () => { S.plan = { ...PLAN_DEFAULTS, sessions: {} }; planChanged(); });
  // per-session controls (event delegation; the table is re-rendered on every change)
  $("#planTable").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-act]"); if (!b) return;
    const key = b.closest("[data-key]").dataset.key, cur = S.plan.sessions[key] || {};
    const status = cur.status === b.dataset.act ? undefined : b.dataset.act;
    setSession(key, { ...cur, status });
  });
  $("#planTable").addEventListener("change", (ev) => {
    const sel = ev.target.closest("select[data-move]"); if (!sel) return;
    const key = sel.closest("[data-key]").dataset.key, cur = S.plan.sessions[key] || {};
    const v = +sel.value;
    setSession(key, { ...cur, move: v === +sel.dataset.orig ? undefined : v });
  });
}
function setSession(key, o) {
  const clean = Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));
  if (Object.keys(clean).length) S.plan.sessions[key] = clean; else delete S.plan.sessions[key];
  planChanged();
}
function saveStatus(msg, err) { const e = $("#planSave"); e.textContent = msg; e.style.color = err ? "var(--bad)" : "var(--muted)"; }
function planChanged() {
  renderRace();
  saveStatus("Saving…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/dash", { method: "POST", cache: "no-store",
        headers: { Authorization: `Bearer ${store.get("ps_pw")}`, "Content-Type": "application/json" },
        body: JSON.stringify({ plan: S.plan }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || `server error ${r.status}`);
      saveStatus(`Saved ${new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`);
    } catch (e) { saveStatus(`Couldn't save: ${e.message}`, true); }
  }, 700);
}
function syncSettingsUI() {
  const cfg = S.plan;
  $$("#pRpw button").forEach((b) => b.classList.toggle("on", +b.dataset.v === cfg.runs_per_week));
  $$("#pLong button").forEach((b) => b.classList.toggle("on", b.dataset.v === cfg.long_day));
  $("#pCap").value = cfg.peak_cap_km; $("#oCap").innerHTML = kmMi(cfg.peak_cap_km);
  if (document.activeElement !== $("#pGoal")) $("#pGoal").value = cfg.goal_sec ? fmtHMS(cfg.goal_sec) : "";
  $("#pGoal").placeholder = S.base0.pred ? fmtHMS(S.base0.pred.sec) : "4:00:00";
  $("#goalHint").textContent = cfg.goal_sec ? "Paces use your goal" : "Blank uses the projection";
}

function renderRace() {
  if (!S.plan) initPlanSettings();
  const b = (S.base0 = baseline(S.runs)), plan = buildPlan(b, S.plan);
  syncSettingsUI();
  const cell = (id, l, cls = "big") => `<div><div class="${cls}" id="${id}"></div><div class="l">${l}</div></div>`;
  $("#countdown").innerHTML = cell("cdD", "days") + cell("cdH", "hours", "mid") + cell("cdM", "minutes", "mid") + cell("cdS", "seconds", "mid")
    + `<div><div class="mid">${plan.goal ? fmtHMS(plan.goal) : "–"}</div><div class="l">${S.plan.goal_sec ? "goal" : "projected"} finish · ${fmtPace(plan.P.mp)}${pu()}</div></div>`;
  const tick = () => {
    const ms = Math.max(0, RACE.date - Date.now());
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set("cdD", Math.floor(ms / DAY)); set("cdH", Math.floor((ms % DAY) / 36e5));
    set("cdM", String(Math.floor((ms % 36e5) / 6e4)).padStart(2, "0")); set("cdS", String(Math.floor((ms % 6e4) / 1e3)).padStart(2, "0"));
  };
  tick(); clearInterval(raceTimer); raceTimer = setInterval(tick, 1e3);

  // long-run readiness: longest recent run vs the planned peak and the full marathon
  const pct = (km) => clamp((km / RACE.km) * 100, 0, 100);
  $("#ready").innerHTML = `
    <div class="ready-h"><span><b>Long-run readiness</b> · longest run in the last 6 weeks is <b>${fmtD(b.longest)}</b>, ${Math.round(pct(b.longest))}% of the marathon</span><span>Plan peaks at ${fmtD(plan.peak)} (${Math.round(pct(plan.peak))}%)</span></div>
    <div class="bullet" title="Longest recent run, planned peak and race distance"><div class="plan" style="width:${pct(plan.peak)}%"></div><div class="fill" id="readyFill"></div><div class="mark" style="left:calc(${pct(plan.peak)}% - 1px)"></div></div>
    <div class="bullet-ax"><span style="left:0">0</span><span style="left:${pct(RACE.km / 2)}%">½ marathon</span><span style="left:100%">${fmtD(RACE.km)}</span></div>`;
  const fill = $("#readyFill");
  if (S.animate && !reducedMotion()) requestAnimationFrame(() => requestAnimationFrame(() => (fill.style.width = pct(b.longest) + "%")));
  else { fill.style.transition = "none"; fill.style.width = pct(b.longest) + "%"; }

  $("#raceLede").innerHTML = `Sunday, November 22, 2026 · 7:00 AM ET · ${plan.n} week${plan.n === 1 ? "" : "s"} including race week. `
    + `The plan starts from your last 4 weeks (${fmtD(b.weekly)}/week, longest run ${fmtD(b.longest)}) and recalculates with every sync.`
    + (b.pred ? ` Projected finish uses Riegel's formula on your ${fmtD(b.pred.from.km)} run from ${fmtDate(b.pred.from.date)}. It assumes marathon endurance you're still building, so treat it as a ceiling, not a promise.` : "");
  const reachable = Math.round(Math.max(12, b.longest) + 3.2 * Math.max(0, plan.n - 2 - (plan.n - 2 >= 5 ? 1 : 0)));
  $("#planNote").innerHTML = `Easy ${fmtPace(plan.P.easy)} · long ${fmtPace(plan.P.long)} · marathon ${fmtPace(plan.P.mp)} · tempo ${fmtPace(plan.P.tempo)} per ${UNIT}. `
    + (reachable < S.plan.peak_cap_km ? `With ${plan.n} weeks left, the long run tops out at ${fmtD(plan.peak)}, below your ${fmtD(S.plan.peak_cap_km)} cap, so it adds no more than ${fmtD(3.2)} a week. ` : "")
    + `Mark sessions done or skipped, or move them to another day. Skipped runs drop out of the week's total.`;

  const now = Date.now();
  const sessHtml = (s) => {
    const past = s.d < now - DAY && !s.race;
    const cls = ["sess", s.status || "", past && !s.status ? "past" : ""].join(" ");
    const moveSel = s.race ? "" : `<select data-move data-orig="${s.dowOrig}" aria-label="Move session">${DOW.map((n2, k) => `<option value="${k}"${k === s.dow ? " selected" : ""}>${n2}</option>`).join("")}</select>`;
    return `<div class="${cls}" data-key="${s.key}" title="${esc(s.desc)}">
      <div class="k">${s.race ? "🏁 " : ""}${kmMi(s.km)}</div>
      <div class="d">${s.race ? "Sun" : DOW[s.dow]} · ${esc(s.type)}${s.moved ? ' <span class="tag">moved</span>' : ""}</div>
      <div class="d">${esc(s.desc)}</div>
      ${s.race ? "" : `<div class="acts"><button data-act="done" class="${s.status === "done" ? "on" : ""}" aria-label="Mark done">✓ Done</button><button data-act="skipped" class="${s.status === "skipped" ? "on" : ""}">Skip</button>${moveSel}</div>`}
    </div>`;
  };
  $("#planTable").innerHTML = `<table class="plan"><thead><tr><th>Week</th><th>Sessions</th><th class="num">Plan</th><th class="num">Logged</th></tr></thead><tbody>${
    plan.weeks.map((w) => {
      const cur = now >= w.start && now < addDays(w.start, 7);
      return `<tr class="${cur ? "now" : ""}"><td><b>${fmtDate(w.start)}</b>${w.tag ? `<span class="tag">${w.tag}</span>` : ""}<div class="d">${w.out ? `${w.out} wk out` : "race"}</div></td>
        <td><div class="sessions">${w.sessions.map(sessHtml).join("")}</div></td>
        <td class="num">${fmtD(w.km, 0)}</td><td class="num">${w.start <= now ? fmtD(kmIn(w.start), 1) : "–"}</td></tr>`;
    }).join("")
  }</tbody></table>`;

  // weekly volume: last 8 logged weeks + plan weeks
  const box = $("#volChart"), w0 = mondayOf(new Date());
  const rows = [];
  for (let i = 8; i >= 1; i--) { const st = addDays(w0, -7 * i); rows.push({ st, act: kmIn(st), plan: null }); }
  plan.weeks.forEach((w) => rows.push({ st: w.start, act: w.start <= now ? kmIn(w.start) : null, plan: w.km, tag: w.tag }));
  const { svg, w, h } = svgBox(box, 220);
  const m = { l: 40, r: 8, t: 12, b: 30 }, iw = w - m.l - m.r, ih = h - m.t - m.b, bw = iw / rows.length;
  const mx = Math.max(dv(10), ...rows.map((r) => dv(Math.max(r.act || 0, r.plan || 0))));
  const Y = (v) => m.t + ih - (v / mx) * ih;
  niceTicks(0, mx, 4).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); txt(svg, m.l - 6, Y(v) + 4, v, { "text-anchor": "end" }); });
  txt(svg, m.l - 6, m.t - 2, UNIT, { "text-anchor": "end" });
  rows.forEach((r, i) => {
    const x = m.l + i * bw + 3, bwi = Math.max(3, bw - 6), g = el("g", {}, svg);
    if (r.act) { const bar = el("rect", { x, y: Y(dv(r.act)), width: bwi, height: m.t + ih - Y(dv(r.act)), rx: 3, fill: css("--s1"), class: A("ay") }, g); bar.style.animationDelay = `${i * 35}ms`; }
    if (r.plan) el("rect", { x: x + 0.75, y: Y(dv(r.plan)), width: bwi - 1.5, height: m.t + ih - Y(dv(r.plan)), rx: 3, fill: "none", stroke: css("--s2"), "stroke-width": 1.5 }, g);
    if (i % 2 === rows.length % 2 || bw > 44) txt(svg, x + bwi / 2, h - 12, fmtDate(r.st), { "text-anchor": "middle" });
    el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => showTip(`<b>Week of ${fmtDate(r.st)}</b>${r.tag ? `<div class="m">${r.tag}</div>` : ""}${r.act != null ? `<div>Logged ${fmtD(r.act, 1)}</div>` : ""}${r.plan ? `<div>Plan ${fmtD(r.plan, 0)}</div>` : ""}`, ev));
    g.addEventListener("mouseleave", hideTip);
  });
  const rx = m.l + (rows.length - 0.5) * bw;
  el("line", { x1: rx, x2: rx, y1: m.t, y2: m.t + ih, stroke: css("--ink-2"), "stroke-dasharray": "3 3" }, svg);
  txt(svg, rx - 4, m.t + 10, "Race", { "text-anchor": "end", class: "ink" });
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
}

// ───────────────────────── k-NN model ─────────────────────────
const aVec = (f) => [clamp(f.tempo / 200, 0, 1), f.energy, f.valence, f.dance];
function renderModel() {
  const g = +$("#qGrade").value, dp = +$("#qDist").value / 100, k = +$("#qK").value;
  $("#oGrade").textContent = g.toFixed(1) + "%"; $("#oDist").textContent = Math.round(dp * 100) + "%"; $("#oK").textContent = k;
  const train = S.segs.filter((s) => s.f && s.f.tempo && s.reward != null).map((s) => ({
    s, grade: clamp(s.grade, -6, 8), dist: ((s.d0 + s.d1) / 2) / s.run.km, a: aVec(s.f), r: s.reward }));
  const N = train.length;
  $("#modelNote").textContent = `${N} aligned segment${N === 1 ? "" : "s"} with audio features. The model switches from k-NN to gradient boosting at 20.`;
  $("#modelProg").style.width = Math.min(100, (N / 20) * 100) + "%";
  const box = $("#knnChart");
  if (!N) { box.innerHTML = `<div class="empty">No training segments yet</div>`; $("#targetBars").innerHTML = ""; $("#queue").innerHTML = ""; return; }
  const dist = (t) => Math.hypot((t.grade - g) / 10, t.dist - dp);
  const ranked = train.map((t) => ({ ...t, dd: dist(t) })).sort((a, b) => a.dd - b.dd);
  const nn = ranked.slice(0, Math.min(k, N));
  let ws = 0; const tgt = [0, 0, 0, 0];
  nn.forEach((t) => { const w = Math.max(t.r, 0.05) / (t.dd + 0.05); ws += w; t.a.forEach((v, i) => (tgt[i] += w * v)); });
  tgt.forEach((_, i) => (tgt[i] /= ws));

  // chart
  const { svg, w, h } = svgBox(box, 300);
  const m = { l: 44, r: 12, t: 12, b: 34 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const X = (v) => m.l + ((v + 6) / 14) * iw, Y = (v) => m.t + ih - v * ih;
  [-6, -4, -2, 0, 2, 4, 6, 8].forEach((v) => { el("line", { x1: X(v), x2: X(v), y1: m.t, y2: m.t + ih, stroke: v === 0 ? css("--axis") : css("--grid") }, svg); txt(svg, X(v), h - 18, v + "%", { "text-anchor": "middle" }); });
  [0, 0.25, 0.5, 0.75, 1].forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); txt(svg, m.l - 6, Y(v) + 4, v * 100 + "%", { "text-anchor": "end" }); });
  txt(svg, m.l + iw / 2, h - 2, "Grade", { "text-anchor": "middle" });
  txt(svg, m.l + 4, m.t + 12, "↑ distance into run");
  const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
  const qx = X(g), qy = Y(dp);
  nn.forEach((t) => el("line", { x1: qx, y1: qy, x2: X(t.grade), y2: Y(t.dist), stroke: css("--s2"), "stroke-width": 1.5, "stroke-dasharray": "3 3" }, svg));
  const rMin = Math.min(...train.map((t) => t.r)), rMax = Math.max(...train.map((t) => t.r));
  const nnSet = new Set(nn.map((t) => t.s));
  train.forEach((t) => {
    const isN = nnSet.has(t.s);
    const c = el("circle", { cx: X(t.grade), cy: Y(t.dist), r: isN ? 8 : 6, fill: seq((t.r - rMin) / (rMax - rMin || 1)),
      stroke: isN ? css("--s2") : css("--surface"), "stroke-width": 2 }, svg);
    c.addEventListener("mousemove", (ev) => showTip(`<b>${esc(t.s.name)}</b><div class="m">${esc(t.s.artist)}</div><div>Grade ${fmtGrade(t.s.grade)} · ${Math.round(t.dist * 100)}% into ${fmtDate(t.s.run.date)} run</div><div>Reward ${t.r.toFixed(2)} · ${fmtPace(t.s.pace)}${pu()} · ${t.s.f.tempo.toFixed(0)} BPM</div>`, ev));
    c.addEventListener("mouseleave", hideTip);
  });
  el("circle", { cx: qx, cy: qy, r: 7, fill: css("--s2"), stroke: css("--surface"), "stroke-width": 2 }, svg);
  hit.addEventListener("click", (ev) => {
    const r = svg.getBoundingClientRect();
    $("#qGrade").value = clamp(Math.round((((ev.clientX - r.left - m.l) / iw) * 14 - 6) * 2) / 2, -6, 8);
    $("#qDist").value = clamp(Math.round((1 - (ev.clientY - r.top - m.t) / ih) * 100), 0, 100);
    renderModel();
  });
  hit.style.cursor = "crosshair";

  // target bars
  const labels = ["Tempo", "Energy", "Valence", "Danceability"];
  barList($("#targetBars"), tgt.map((v, i) => ({ label: labels[i], value: v, text: i === 0 ? `${Math.round(v * 200)} BPM` : v.toFixed(2) })), { max: 1, color: css("--s2") });

  // queue: nearest library tracks in audio space
  const lib = Object.entries(S.tracks).filter(([, t]) => t.f && t.f.tempo).map(([id, t]) => {
    const d = Math.hypot(...aVec(t.f).map((v, i) => v - tgt[i]));
    return { id, t, d, match: clamp(1 - d / 2, 0, 1) };
  }).sort((a, b) => a.d - b.d).slice(0, 5);
  $("#queue").innerHTML = `<table><tbody>${lib.map((q, i) => `<tr><td style="color:var(--muted)">${i + 1}</td><td>${esc(q.t.n)}<div style="color:var(--ink-2);font-size:12px">${esc(q.t.a)}</div></td><td class="num">${q.t.f.tempo.toFixed(0)} BPM</td><td class="num">${Math.round(q.match * 100)}% match</td></tr>`).join("")}</tbody></table>`;
}
})();
