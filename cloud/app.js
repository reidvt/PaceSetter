/* Pacesetter dashboard — plain JS, Leaflet for the map, hand-built SVG charts. */
(() => {
"use strict";

const $ = (s) => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  del(k) { try { sessionStorage.removeItem(k); } catch {} },
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtPace = (p) => { if (!isFinite(p) || p <= 0) return "–"; const m = Math.floor(p); let s = Math.round((p - m) * 60); return s === 60 ? `${m + 1}:00` : `${m}:${String(s).padStart(2, "0")}`; };
const fmtDur = (sec) => { const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return h ? `${h}h ${m}m` : `${m}m`; };
const fmtDate = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
const fmtGrade = (g) => { const v = Math.abs(g) < 0.05 ? 0 : g; return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1) + "%"; };
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

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

function decodeRun(r) {
  const t = undelta(r.t), lat = undelta(r.lat).map((v) => v / 1e5), lon = undelta(r.lon).map((v) => v / 1e5);
  const ele = undelta(r.ele).map((v) => v / 10), d = undelta(r.d).map((v) => v / 1000);
  const p = r.p.map((v) => v / 100);
  return { ...r, t, lat, lon, ele, d, p, date: new Date(r.start), songs: [] };
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
      const reward = isFinite(pace) ? clamp(1 - Math.abs(pace - run.pace), -1, 1) : null;
      run.songs.push({ id, name: tr.n || name, artist: tr.a || artist, f: tr.f, s, e, d0, d1, pace, grade,
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
  S = { data, tracks, live, plays, runs, segs, run: runs.find((r) => r.songs.length) || runs[0],
        colorMode: "song", base: "street", hl: null };

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
  sel.innerHTML = runs.map((r, i) => `<option value="${i}">${fmtDate(r.date)} · ${esc(r.name)} · ${r.km.toFixed(1)} km${r.songs.length ? ` · ♪ ${r.songs.length}` : ""}</option>`).join("");
  sel.value = runs.indexOf(S.run);
  sel.addEventListener("change", () => { S.run = runs[+sel.value]; S.hl = null; renderRun(true); });
  document.querySelectorAll("#colorMode button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#colorMode button").forEach((x) => x.classList.toggle("on", x === b));
    S.colorMode = b.dataset.m; drawRoute(false);
  }));
  document.querySelectorAll("#baseMode button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#baseMode button").forEach((x) => x.classList.toggle("on", x === b));
    S.base = b.dataset.b; setBase();
  }));
  ["qGrade", "qDist", "qK"].forEach((id) => $("#" + id).addEventListener("input", renderModel));

  renderAll();
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(renderAll, 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { setBase(); renderAll(); });
}

function renderAll() { renderKpis(); renderRace(); renderRun(false); renderListening(); renderMusicPerf(); renderModel(); }

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
function svgBox(container, h) {
  container.innerHTML = "";
  const w = Math.max(280, container.clientWidth || 600);
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
  const A = p(a), B = p(b);
  return "#" + A.map((v, i) => Math.round(v + (B[i] - v) * t).toString(16).padStart(2, "0")).join("");
}
const seq = (t) => mixHex(css("--seq-lo"), css("--seq-hi"), clamp(t, 0, 1));

// Horizontal bar list: [{label, sub, value, text}]
function barList(container, items, { color = css("--s1"), max } = {}) {
  container.innerHTML = "";
  if (!items.length) { container.innerHTML = `<div class="empty">Nothing yet</div>`; return; }
  const rowH = 28, labelW = Math.min(200, (container.clientWidth || 500) * 0.42);
  const { svg, w } = svgBox(container, items.length * rowH + 4);
  const m = max ?? Math.max(...items.map((d) => d.value));
  const x0 = labelW + 8, bw = w - x0 - 72;
  items.forEach((d, i) => {
    const y = i * rowH + 4;
    const g = el("g", {}, svg);
    const t = el("text", { x: labelW, y: y + 15, "text-anchor": "end", class: "ink" }, g);
    const maxChars = Math.floor(labelW / 6.4);
    t.textContent = d.label.length > maxChars ? d.label.slice(0, maxChars - 1) + "…" : d.label;
    const len = Math.max(2, (d.value / m) * bw);
    el("rect", { x: x0, y: y + 5, width: len, height: 14, rx: 3, fill: color }, g);
    const v = el("text", { x: x0 + len + 6, y: y + 16 }, g); v.textContent = d.text ?? d.value;
    el("rect", { x: 0, y, width: w, height: rowH, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => showTip(`<b>${esc(d.label)}</b>${d.sub ? `<div class="m">${esc(d.sub)}</div>` : ""}<div>${esc(d.text ?? d.value)}</div>`, ev));
    g.addEventListener("mouseleave", hideTip);
  });
}

// ───────────────────────── KPIs ─────────────────────────
function renderKpis() {
  const { runs, plays, data, segs } = S;
  const km = runs.reduce((a, r) => a + r.km, 0), sec = runs.reduce((a, r) => a + r.sec, 0);
  const avgPace = sec / 60 / km;
  const longest = runs.reduce((a, r) => (r.km > a.km ? r : a), runs[0]);
  const last4 = runs.filter((r) => Date.now() - r.date < 28 * 864e5).reduce((a, r) => a + r.km, 0);
  const k = [
    [runs.length, "Runs", `${fmtDate(runs[runs.length - 1].date)} – ${fmtDate(runs[0].date)}`],
    [km.toFixed(0) + " km", "Total distance", `${last4.toFixed(0)} km in the last 4 weeks`],
    [fmtPace(avgPace) + " /km", "Average pace", fmtDur(sec) + " running"],
    [longest.km.toFixed(1) + " km", "Longest run", fmtDate(longest.date)],
    [segs.length, "Songs matched to runs", `${plays.length} plays logged in total`],
    [`${data.library.with_features}/${data.library.size}`, "Tracks with audio features", "Liked Songs library"],
  ];
  $("#kpis").innerHTML = k.map(([v, l, s]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");
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
  const n = run.lat.length, c1 = css("--s1"), c2 = css("--s2"), cm = css("--muted");
  const pMin = Math.min(...run.p.filter((p) => p > 2.5)), pMax = Math.min(9, Math.max(...run.p.filter((p) => p < 12)));
  const eMin = Math.min(...run.ele), eMax = Math.max(...run.ele);
  for (let i = 0; i < n - 1; i++) {
    const tm = (run.t[i] + run.t[i + 1]) / 2, sg = songAt(run, tm);
    let color, weight = 5, op = 0.95;
    if (S.colorMode === "song") { color = sg ? (sg.idx % 2 ? c1 : c2) : cm; if (!sg) op = 0.6; }
    else if (S.colorMode === "pace") color = seq(1 - (clamp(run.p[i + 1], pMin, pMax) - pMin) / (pMax - pMin || 1));
    else color = seq((run.ele[i] - eMin) / (eMax - eMin || 1));
    if (S.hl && sg === S.hl) weight = 9;
    const line = L.polyline([[run.lat[i], run.lon[i]], [run.lat[i + 1], run.lon[i + 1]]], { color, weight, opacity: op, lineCap: "round" }).addTo(routeLayer);
    const info = `<b>${run.d[i].toFixed(2)} km</b> · ${run.ele[i].toFixed(0)} m · ${fmtPace(run.p[i + 1])}/km` + (sg ? `<br>♪ ${esc(sg.name)} — ${esc(sg.artist)}` : "");
    line.bindTooltip(info, { sticky: true });
    if (sg) line.on("click", () => highlight(sg));
  }
  if (S.colorMode === "song") {
    for (const sg of run.songs) {
      const la = interp(run.t, run.lat, sg.s), lo = interp(run.t, run.lon, sg.s);
      L.marker([la, lo], { icon: L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9],
        html: `<div style="width:18px;height:18px;border-radius:50%;background:${css("--surface")};border:2px solid ${sg.idx % 2 ? c1 : c2};font:600 10px system-ui;display:grid;place-items:center;color:${css("--ink")}">${sg.idx}</div>` }) })
        .bindTooltip(`♪ ${esc(sg.name)}<br><span style="opacity:.7">${esc(sg.artist)}</span>`).on("click", () => highlight(sg)).addTo(routeLayer);
    }
  }
  const dot = (i, label) => L.circleMarker([run.lat[i], run.lon[i]], { radius: 6, color: css("--surface"), weight: 2, fillColor: css("--ink"), fillOpacity: 1 }).bindTooltip(label).addTo(routeLayer);
  dot(0, "Start"); dot(n - 1, "Finish");
  cursor = L.circleMarker([run.lat[0], run.lon[0]], { radius: 7, color: css("--surface"), weight: 3, fillColor: css("--s2"), fillOpacity: 1, opacity: 0 }).addTo(routeLayer);
  cursor.setStyle({ fillOpacity: 0 });
  if (fit) map.fitBounds(L.latLngBounds(run.lat.map((la, i) => [la, run.lon[i]])), { padding: [20, 20] });

  const lg = $("#mapLegend");
  if (S.colorMode === "song") lg.innerHTML = run.songs.length
    ? `<span><span class="sw" style="background:${c1}"></span><span class="sw" style="background:${c2}"></span>Songs alternate colors; numbers match the table</span><span><span class="sw" style="background:${cm}"></span>No song captured</span>`
    : `<span>No plays were captured during this run. The logger started collecting on ${S.plays.length ? fmtDate(new Date(S.plays[0][1])) : "—"}.</span>`;
  else if (S.colorMode === "pace") lg.innerHTML = `<span>Pace</span><span>${fmtPace(pMax)}</span><span class="ramp"></span><span>${fmtPace(pMin)} /km (faster)</span>`;
  else lg.innerHTML = `<span>Elevation</span><span>${eMin.toFixed(0)} m</span><span class="ramp"></span><span>${eMax.toFixed(0)} m</span>`;
}
function moveCursor(i) {
  if (!cursor) return;
  if (i == null) { cursor.setStyle({ fillOpacity: 0, opacity: 0 }); return; }
  cursor.setLatLng([S.run.lat[i], S.run.lon[i]]); cursor.setStyle({ fillOpacity: 1, opacity: 1 });
}
function highlight(sg) {
  S.hl = S.hl === sg ? null : sg;
  drawRoute(false); renderSongTable();
  if (S.hl) {
    const r = S.run, pts = [];
    for (let i = 0; i < r.t.length; i++) if (r.t[i] >= sg.s && r.t[i] <= sg.e) pts.push([r.lat[i], r.lon[i]]);
    if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 16 });
  }
}

// ───────────────────────── run charts ─────────────────────────
function smooth(a, k = 2) { return a.map((_, i) => { const w = a.slice(Math.max(0, i - k), i + k + 1).sort((x, y) => x - y); return w[w.length >> 1]; }); }

function lineChart(container, run, ys, { h = 180, area = false, avg = null, fmtY = (v) => v.toFixed(0), unit = "" } = {}) {
  const { svg, w } = svgBox(container, h);
  const m = { l: 44, r: 12, t: 22, b: 24 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const xs = run.d, x1 = xs[xs.length - 1] || 1;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (avg != null) { lo = Math.min(lo, avg); hi = Math.max(hi, avg); }
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = (v) => m.l + (v / x1) * iw, Y = (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
  // song bands
  run.songs.forEach((sg) => {
    const a = X(sg.d0), b = X(sg.d1);
    el("rect", { x: a, y: m.t, width: Math.max(1, b - a - 2), height: ih, fill: sg.idx % 2 ? css("--band-a") : css("--band-b") }, svg);
    if (b - a > 14) { const t = el("text", { x: (a + b) / 2, y: m.t - 6, "text-anchor": "middle" }, svg); t.textContent = sg.idx; }
  });
  niceTicks(lo, hi, 4).forEach((v) => {
    el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg);
    const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg); t.textContent = fmtY(v);
  });
  niceTicks(0, x1, Math.max(3, Math.floor(iw / 90))).forEach((v) => { const t = el("text", { x: X(v), y: h - 6, "text-anchor": "middle" }, svg); t.textContent = v + (v === 0 ? "" : " km"); });
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
  const pts = xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" ");
  if (area) el("polygon", { points: `${X(0)},${m.t + ih} ${pts} ${X(x1)},${m.t + ih}`, fill: css("--s1"), "fill-opacity": 0.14 }, svg);
  el("polyline", { points: pts, fill: "none", stroke: css("--s1"), "stroke-width": 2, "stroke-linejoin": "round" }, svg);
  if (avg != null) {
    el("line", { x1: m.l, x2: w - m.r, y1: Y(avg), y2: Y(avg), stroke: css("--ink-2"), "stroke-dasharray": "4 4" }, svg);
    const t = el("text", { x: w - m.r, y: Y(avg) - 5, "text-anchor": "end", class: "ink" }, svg); t.textContent = `avg ${fmtY(avg)}`;
  }
  const vline = el("line", { y1: m.t, y2: m.t + ih, stroke: css("--ink-2"), opacity: 0 }, svg);
  const vdot = el("circle", { r: 4, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2, opacity: 0 }, svg);
  const hit = el("rect", { x: m.l, y: 0, width: iw, height: h, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), km = ((ev.clientX - r.left - m.l) / iw) * x1;
    let i = 0; while (i < xs.length - 1 && xs[i + 1] < km) i++;
    vline.setAttribute("x1", X(xs[i])); vline.setAttribute("x2", X(xs[i])); vline.setAttribute("opacity", 0.6);
    vdot.setAttribute("cx", X(xs[i])); vdot.setAttribute("cy", Y(ys[i])); vdot.setAttribute("opacity", 1);
    const sg = songAt(run, run.t[i]);
    showTip(`<b>${xs[i].toFixed(2)} km</b><div>${run.ele[i].toFixed(0)} m · ${fmtPace(run.p[i])}/km</div>${sg ? `<div class="m">♪ ${sg.idx}. ${esc(sg.name)}</div>` : ""}`, ev);
    moveCursor(i);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); vline.setAttribute("opacity", 0); vdot.setAttribute("opacity", 0); moveCursor(null); });
}

function renderRun(fit) {
  const run = S.run;
  $("#stravaLink").href = run.url;
  drawRoute(fit || !map);
  lineChart($("#elevChart"), run, run.ele, { area: true, fmtY: (v) => v.toFixed(0) });
  const pace = smooth(run.p.map((p) => clamp(p, 3, 10)), 3);
  lineChart($("#paceChart"), run, pace, { h: 150, avg: run.pace, fmtY: fmtPace });
  renderSongTable();
}

function renderSongTable() {
  const run = S.run, box = $("#songTable");
  $("#songNote").textContent = run.songs.length
    ? `${run.songs.length} songs · click a row to find it on the map. Δ is seconds per km vs this run's ${fmtPace(run.pace)}/km average.`
    : "No plays were captured during this run.";
  if (!run.songs.length) { box.innerHTML = `<div class="empty">Keep the play logger running and songs will show up here on future runs.</div>`; return; }
  const c1 = css("--s1"), c2 = css("--s2");
  box.innerHTML = `<table><thead><tr><th>#</th><th>Song</th><th>Artist</th><th class="num">km</th><th class="num">Pace</th><th class="num">Δ s/km</th><th class="num">Grade</th><th class="num">BPM</th><th class="num">Energy</th></tr></thead><tbody>${
    run.songs.map((sg) => { const dlt = (sg.pace - run.pace) * 60; return `<tr class="song${S.hl === sg ? " on" : ""}" data-i="${sg.idx}">
      <td><span class="sw" style="background:${sg.idx % 2 ? c1 : c2}"></span>${sg.idx}</td><td>${esc(sg.name)}</td><td style="color:var(--ink-2)">${esc(sg.artist)}</td>
      <td class="num">${sg.d0.toFixed(1)}–${sg.d1.toFixed(1)}</td><td class="num">${fmtPace(sg.pace)}</td>
      <td class="num" style="color:${dlt < 0 ? "var(--good)" : "var(--bad)"}">${isFinite(dlt) ? (dlt > 0 ? "+" : "−") + Math.abs(dlt).toFixed(0) : "–"}</td>
      <td class="num">${fmtGrade(sg.grade)}</td><td class="num">${sg.f && sg.f.tempo ? sg.f.tempo.toFixed(0) : "–"}</td><td class="num">${sg.f && sg.f.energy != null ? sg.f.energy.toFixed(2) : "–"}</td></tr>`; }).join("")
  }</tbody></table>`;
  box.querySelectorAll("tr.song").forEach((tr) => tr.addEventListener("click", () => highlight(run.songs[+tr.dataset.i - 1])));
}

// ───────────────────────── listening ─────────────────────────
function renderListening() {
  const { plays, segs, live, data } = S;
  const first = plays[0], last = plays[plays.length - 1];
  $("#listenLede").textContent = plays.length
    ? `${plays.length} plays logged from ${fmtDate(new Date(first[1]))} to ${fmtDate(new Date(last[1]))}${live.connected ? `, ${live.plays.length} of them pulled live from the cloud logger since the last sync` : ""}. ${segs.length} overlapped a run. These charts fill in as the logger keeps running.`
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
  niceTicks(0, mx, 3).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih - Y(v), y2: m.t + ih - Y(v), stroke: css("--grid") }, svg); const t = el("text", { x: m.l - 6, y: m.t + ih - Y(v) + 4, "text-anchor": "end" }, svg); t.textContent = v; });
  H.forEach(([a, b], i) => {
    const x = m.l + i * bw + 2, bwi = Math.max(2, bw - 4), g = el("g", {}, svg);
    if (a) el("rect", { x, y: m.t + ih - Y(a), width: bwi, height: Y(a), rx: 2, fill: css("--s1") }, g);
    if (b) el("rect", { x, y: m.t + ih - Y(a) - Y(b), width: bwi, height: Math.max(0, Y(b) - (a ? 2 : 0)), rx: 2, fill: css("--axis") }, g);
    if (i % 3 === 0) { const t = el("text", { x: x + bwi / 2, y: h - 6, "text-anchor": "middle" }, svg); t.textContent = i === 0 ? "12a" : i === 12 ? "12p" : i < 12 ? i + "a" : i - 12 + "p"; }
    el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => showTip(`<b>${i}:00–${i}:59</b><div>${a} during a run · ${b} other</div>`, ev));
    g.addEventListener("mouseleave", hideTip);
  });
  el("line", { x1: m.l, x2: w - m.r, y1: m.t + ih, y2: m.t + ih, stroke: css("--axis") }, svg);
}

// ───────────────────────── music × pace ─────────────────────────
function renderMusicPerf() {
  const segs = S.segs.filter((s) => isFinite(s.pace));
  // diverging bars
  const box = $("#songDelta");
  if (!segs.length) { box.innerHTML = `<div class="empty">No songs matched to runs yet</div>`; }
  else {
    const items = segs.map((s) => ({ s, v: (s.pace - s.run.pace) * 60 })).sort((a, b) => a.v - b.v);
    const rowH = 24, labelW = Math.min(190, (box.clientWidth || 500) * 0.4);
    const { svg, w } = svgBox(box, items.length * rowH + 8);
    const mx = Math.max(...items.map((d) => Math.abs(d.v)), 5), x0 = labelW + 10, iw = w - x0 - 10, cx = x0 + iw / 2;
    el("line", { x1: cx, x2: cx, y1: 0, y2: items.length * rowH + 4, stroke: css("--axis") }, svg);
    items.forEach(({ s, v }, i) => {
      const y = i * rowH + 4, g = el("g", {}, svg), len = (Math.abs(v) / mx) * (iw / 2 - 34);
      const t = el("text", { x: labelW, y: y + 14, "text-anchor": "end", class: "ink" }, g);
      const mc = Math.floor(labelW / 6.4); t.textContent = s.name.length > mc ? s.name.slice(0, mc - 1) + "…" : s.name;
      el("rect", { x: v < 0 ? cx - len : cx, y: y + 4, width: Math.max(2, len), height: 13, rx: 3, fill: v < 0 ? css("--s1") : css("--s2") }, g);
      const vt = el("text", { x: v < 0 ? cx - len - 5 : cx + len + 5, y: y + 14, "text-anchor": v < 0 ? "end" : "start" }, g);
      vt.textContent = (v < 0 ? "−" : "+") + Math.abs(v).toFixed(0) + "s";
      el("rect", { x: 0, y, width: w, height: rowH, fill: "transparent" }, g);
      g.addEventListener("mousemove", (ev) => showTip(`<b>${esc(s.name)}</b><div class="m">${esc(s.artist)}</div><div>${fmtPace(s.pace)}/km vs ${fmtPace(s.run.pace)} avg · ${fmtDate(s.run.date)}</div><div>Grade ${fmtGrade(s.grade)} · ${s.d0.toFixed(1)}–${s.d1.toFixed(1)} km</div>`, ev));
      g.addEventListener("mouseleave", hideTip);
    });
  }
  box.previousElementSibling.innerHTML = `Seconds per km <span style="color:var(--s1)">faster</span> or <span style="color:var(--s2)">slower</span> than that run's average`;

  // scatter tempo vs pace
  const sb = $("#tempoScatter"), pts = segs.filter((s) => s.f && s.f.tempo);
  if (!pts.length) { sb.innerHTML = `<div class="empty">No featured songs on runs yet</div>`; return; }
  const { svg, w, h } = svgBox(sb, 260);
  const m = { l: 48, r: 12, t: 12, b: 32 }, iw = w - m.l - m.r, ih = h - m.t - m.b;
  const tx = pts.map((p) => p.f.tempo), py = pts.map((p) => p.pace);
  const xl = Math.min(...tx) - 8, xh = Math.max(...tx) + 8, yl = Math.min(...py) - 0.15, yh = Math.max(...py) + 0.15;
  const X = (v) => m.l + ((v - xl) / (xh - xl)) * iw, Y = (v) => m.t + ((v - yl) / (yh - yl)) * ih; // faster at top
  niceTicks(yl, yh, 4).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg); t.textContent = fmtPace(v); });
  niceTicks(xl, xh, 5).forEach((v) => { const t = el("text", { x: X(v), y: h - 14, "text-anchor": "middle" }, svg); t.textContent = v; });
  const xt = el("text", { x: m.l + iw / 2, y: h - 1, "text-anchor": "middle" }, svg); xt.textContent = "Tempo (BPM)";
  const yt = el("text", { x: 4, y: m.t + 4 }, svg); yt.textContent = "faster ↑";
  pts.forEach((p) => {
    const c = el("circle", { cx: X(p.f.tempo), cy: Y(p.pace), r: 6, fill: css("--s1"), stroke: css("--surface"), "stroke-width": 2 }, svg);
    c.addEventListener("mousemove", (ev) => showTip(`<b>${esc(p.name)}</b><div class="m">${esc(p.artist)}</div><div>${p.f.tempo.toFixed(0)} BPM · ${fmtPace(p.pace)}/km · energy ${p.f.energy.toFixed(2)}</div>`, ev));
    c.addEventListener("mouseleave", hideTip);
  });
}

// ───────────────────────── race countdown + training plan ─────────────────────────
// Philadelphia Marathon: Sunday before Thanksgiving, 7:00 AM ET start.
const RACE = { name: "Philadelphia Marathon", date: new Date("2026-11-22T07:00:00-05:00"), km: 42.195 };
const DAY = 864e5, WEEK = 7 * DAY, MI = 0.621371;
const addDays = (d, k) => { const x = new Date(d); x.setDate(x.getDate() + k); return x; };
const mondayOf = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
const fmtHMS = (sec) => { const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60); return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`; };
const kmMi = (km) => `${km.toFixed(km < 10 ? 1 : 0)} km <span class="d">(${(km * MI).toFixed(1)} mi)</span>`;

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
  const q = [
    (km) => ({ type: "Tempo", desc: `2 km easy · ${Math.max(3, Math.round(km - 4))} km @ ${fmtPace(P.tempo)} · 2 km easy` }),
    (km) => ({ type: "Hills", desc: `2 km easy · 6–8 × 90 s hard uphill, jog down · rest easy` }),
    (km) => ({ type: "Marathon pace", desc: `2 km easy · ${Math.max(4, Math.round(km - 3))} km @ ${fmtPace(P.mp)} · 1 km easy` }),
  ];
  const easy = { type: "Easy", desc: `Easy @ ${fmtPace(P.easy)}` };
  const weeks = [];
  let L = L0, climb = 0;
  for (let i = 0; i < n; i++) {
    const start = addDays(w0, 7 * i), out = n - 1 - i;
    let s, tag = "";
    if (out === 0) {
      tag = "Race week";
      s = [
        { role: "rw1", dow: 1, km: 8, type: "Easy + strides", desc: `Easy @ ${fmtPace(P.easy)} with 3 × 1.5 km @ ${fmtPace(P.mp)}` },
        { role: "rw2", dow: 3, km: 5, ...easy },
        { role: "rw3", dow: 5, km: 3, type: "Shakeout", desc: "Very easy, 4 short strides" },
        { role: "race", dow: 6, km: RACE.km, type: "Race", desc: `Target ${fmtPace(P.mp)}/km → ${goal ? fmtHMS(goal) : "–"}`, race: true },
      ];
    } else {
      let long, mid;
      if (out === 1) { long = Math.round(peak * 0.6); mid = mid0 * 0.75; tag = "Taper"; }
      else if (i === cut) { long = Math.round(L * 0.75); mid = mid0 * (1 + 0.08 * i) * 0.8; tag = "Cutback"; }
      else { climb++; L = L0 + step * climb; long = Math.round(L); mid = mid0 * (1 + 0.08 * i); if (out === 2) tag = "Peak"; }
      const qk = Math.round(mid * 0.4), ek = Math.max(5, Math.round((mid * 0.6) / (rpw - 2)));
      const qs = out === 1 ? { type: "Marathon pace", desc: `2 km easy · 5 km @ ${fmtPace(P.mp)} · 1 km easy` } : q[i % 3](qk);
      const mpFinish = !tag && climb >= 3 ? ` · last ${Math.min(8, climb + 2)} km @ ${fmtPace(P.mp)}` : "";
      s = [
        { role: "q", dow: layout[0], km: qk, ...qs },
        ...layout.slice(1).map((dow, j) => ({ role: "e" + (j + 1), dow, km: ek, ...easy })),
        { role: "long", dow: longDay, km: long, type: "Long", desc: `Long @ ${fmtPace(P.long)}${mpFinish}` },
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
    document.querySelectorAll(`${sel} button`).forEach((b) => b.addEventListener("click", () => { S.plan[key] = cast(b.dataset.v); planChanged(); }));
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
function syncSettingsUI(plan) {
  const cfg = S.plan;
  document.querySelectorAll("#pRpw button").forEach((b) => b.classList.toggle("on", +b.dataset.v === cfg.runs_per_week));
  document.querySelectorAll("#pLong button").forEach((b) => b.classList.toggle("on", b.dataset.v === cfg.long_day));
  $("#pCap").value = cfg.peak_cap_km; $("#oCap").textContent = cfg.peak_cap_km + " km";
  if (document.activeElement !== $("#pGoal")) $("#pGoal").value = cfg.goal_sec ? fmtHMS(cfg.goal_sec) : "";
  $("#pGoal").placeholder = S.base0.pred ? fmtHMS(S.base0.pred.sec) : "4:00:00";
  $("#goalHint").textContent = cfg.goal_sec ? "Paces use your goal" : "Blank uses the projection";
}

function renderRace() {
  if (!S.plan) initPlanSettings();
  const b = (S.base0 = baseline(S.runs)), plan = buildPlan(b, S.plan);
  syncSettingsUI(plan);
  const tick = () => {
    const ms = Math.max(0, RACE.date - Date.now());
    const d = Math.floor(ms / DAY), h = Math.floor((ms % DAY) / 36e5), m = Math.floor((ms % 36e5) / 6e4);
    const cell = (v, l) => `<div><div class="big">${v}</div><div class="l">${l}</div></div>`;
    $("#countdown").innerHTML = cell(d, `day${d === 1 ? "" : "s"}`) + cell(h, "hours") + cell(m, "minutes")
      + cell(plan.goal ? fmtHMS(plan.goal) : "–", `${S.plan.goal_sec ? "goal" : "projected"} finish · ${fmtPace(plan.P.mp)}/km`)
      + cell(Math.round(plan.peak) + " km", `peak long run (${(plan.peak * MI).toFixed(1)} mi)`);
  };
  tick(); clearInterval(raceTimer); raceTimer = setInterval(tick, 30e3);

  $("#raceLede").innerHTML = `Sunday, November 22, 2026 · 7:00 AM ET · ${plan.n} week${plan.n === 1 ? "" : "s"} including race week. `
    + `The plan starts from your last 4 weeks (${b.weekly.toFixed(0)} km/week, longest run ${b.longest.toFixed(1)} km) and recalculates with every sync.`
    + (b.pred ? ` Projected finish uses Riegel's formula on your ${b.pred.from.km.toFixed(1)} km run from ${fmtDate(b.pred.from.date)}. It assumes marathon endurance you're still building, so treat it as a ceiling, not a promise.` : "");
  const reachable = Math.round(Math.max(12, b.longest) + 3.2 * Math.max(0, plan.n - 2 - (plan.n - 2 >= 5 ? 1 : 0)));
  $("#planNote").innerHTML = `Easy ${fmtPace(plan.P.easy)} · long ${fmtPace(plan.P.long)} · marathon ${fmtPace(plan.P.mp)} · tempo ${fmtPace(plan.P.tempo)} per km. `
    + (reachable < S.plan.peak_cap_km ? `With ${plan.n} weeks left, the long run tops out at ${Math.round(plan.peak)} km, below your ${S.plan.peak_cap_km} km cap, so it adds no more than 3.2 km a week. ` : "")
    + `Mark sessions done or skipped, or move them to another day. Skipped runs drop out of the week's total.`;

  const logged = (start) => S.runs.filter((r) => r.date >= start && r.date < addDays(start, 7)).reduce((a, r) => a + r.km, 0);
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
        <td class="num">${w.km.toFixed(0)} km</td><td class="num">${w.start <= now ? logged(w.start).toFixed(1) + " km" : "–"}</td></tr>`;
    }).join("")
  }</tbody></table>`;

  // weekly volume: last 8 logged weeks + plan weeks
  const box = $("#volChart"), w0 = mondayOf(new Date());
  const rows = [];
  for (let i = 8; i >= 1; i--) { const st = addDays(w0, -7 * i); rows.push({ st, act: logged(st), plan: null }); }
  plan.weeks.forEach((w) => rows.push({ st: w.start, act: w.start <= now ? logged(w.start) : null, plan: w.km, tag: w.tag }));
  const { svg, w, h } = svgBox(box, 220);
  const m = { l: 40, r: 8, t: 12, b: 30 }, iw = w - m.l - m.r, ih = h - m.t - m.b, bw = iw / rows.length;
  const mx = Math.max(10, ...rows.map((r) => Math.max(r.act || 0, r.plan || 0)));
  const Y = (v) => m.t + ih - (v / mx) * ih;
  niceTicks(0, mx, 4).forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg); t.textContent = v; });
  rows.forEach((r, i) => {
    const x = m.l + i * bw + 3, bwi = Math.max(3, bw - 6), g = el("g", {}, svg);
    if (r.act) el("rect", { x, y: Y(r.act), width: bwi, height: m.t + ih - Y(r.act), rx: 3, fill: css("--s1") }, g);
    if (r.plan) el("rect", { x: x + 0.75, y: Y(r.plan), width: bwi - 1.5, height: m.t + ih - Y(r.plan), rx: 3, fill: "none", stroke: css("--s2"), "stroke-width": 1.5 }, g);
    if (i % 2 === rows.length % 2 || bw > 44) { const t = el("text", { x: x + bwi / 2, y: h - 12, "text-anchor": "middle" }, svg); t.textContent = fmtDate(r.st); }
    el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }, g);
    g.addEventListener("mousemove", (ev) => showTip(`<b>Week of ${fmtDate(r.st)}</b>${r.tag ? `<div class="m">${r.tag}</div>` : ""}${r.act != null ? `<div>Logged ${r.act.toFixed(1)} km</div>` : ""}${r.plan ? `<div>Plan ${r.plan.toFixed(0)} km</div>` : ""}`, ev));
    g.addEventListener("mouseleave", hideTip);
  });
  const rx = m.l + (rows.length - 0.5) * bw;
  el("line", { x1: rx, x2: rx, y1: m.t, y2: m.t + ih, stroke: css("--ink-2"), "stroke-dasharray": "3 3" }, svg);
  const rt = el("text", { x: rx - 4, y: m.t + 10, "text-anchor": "end", class: "ink" }, svg); rt.textContent = "Race";
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
  [-6, -4, -2, 0, 2, 4, 6, 8].forEach((v) => { el("line", { x1: X(v), x2: X(v), y1: m.t, y2: m.t + ih, stroke: v === 0 ? css("--axis") : css("--grid") }, svg); const t = el("text", { x: X(v), y: h - 18, "text-anchor": "middle" }, svg); t.textContent = v + "%"; });
  [0, 0.25, 0.5, 0.75, 1].forEach((v) => { el("line", { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), stroke: css("--grid") }, svg); const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg); t.textContent = v * 100 + "%"; });
  const xl = el("text", { x: m.l + iw / 2, y: h - 2, "text-anchor": "middle" }, svg); xl.textContent = "Grade";
  const yl = el("text", { x: m.l + 4, y: m.t + 12 }, svg); yl.textContent = "↑ distance into run";
  const qx = X(g), qy = Y(dp);
  nn.forEach((t) => el("line", { x1: qx, y1: qy, x2: X(t.grade), y2: Y(t.dist), stroke: css("--s2"), "stroke-width": 1.5, "stroke-dasharray": "3 3" }, svg));
  const rMin = Math.min(...train.map((t) => t.r)), rMax = Math.max(...train.map((t) => t.r));
  train.forEach((t) => {
    const isN = nn.includes(ranked.find((x) => x.s === t.s));
    const c = el("circle", { cx: X(t.grade), cy: Y(t.dist), r: isN ? 8 : 6, fill: seq((t.r - rMin) / (rMax - rMin || 1)),
      stroke: isN ? css("--s2") : css("--surface"), "stroke-width": 2 }, svg);
    c.addEventListener("mousemove", (ev) => showTip(`<b>${esc(t.s.name)}</b><div class="m">${esc(t.s.artist)}</div><div>Grade ${fmtGrade(t.s.grade)} · ${Math.round(t.dist * 100)}% into ${fmtDate(t.s.run.date)} run</div><div>Reward ${t.r.toFixed(2)} · ${fmtPace(t.s.pace)}/km · ${t.s.f.tempo.toFixed(0)} BPM</div>`, ev));
    c.addEventListener("mouseleave", hideTip);
  });
  el("circle", { cx: qx, cy: qy, r: 7, fill: css("--s2"), stroke: css("--surface"), "stroke-width": 2 }, svg);
  const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
  svg.insertBefore(hit, svg.firstChild.nextSibling);
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
