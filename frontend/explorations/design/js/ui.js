/* Shared UI engine: streaming text, stage sequencing, chart rendering,
   Simple/Analyst mode. Every screen HTML file includes this after mock.js. */

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : n);

let UI_MODE = "simple"; // "simple" | "analyst"

function applyMode(mode) {
  UI_MODE = mode;
  document.querySelectorAll(".analyst-only").forEach((el) => el.classList.toggle("hide", mode !== "analyst"));
  document.querySelectorAll(".simple-only").forEach((el) => el.classList.toggle("hide", mode === "analyst"));
  document.querySelectorAll(".seg button[data-mode]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
  });
}
function initModeToggle() {
  const seg = document.querySelector(".seg[data-role='mode']");
  if (!seg) return;
  seg.querySelectorAll("button[data-mode]").forEach((b) => {
    b.onclick = () => {
      applyMode(b.dataset.mode);
      toast(b.dataset.mode === "analyst" ? "Switched to Analyst" : "Switched to Simple");
    };
  });
  applyMode(UI_MODE);
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
}

/* ---------------- SQL syntax highlight + typing ---------------- */
function highlightSQL(text) {
  const kw = ["SELECT", "FROM", "JOIN", "ON", "WHERE", "GROUP BY", "ORDER BY",
    "LIMIT", "SUM", "AVG", "COUNT", "ABS", "DESC", "ASC", "AS", "DATE_TRUNC"];
  let out = esc(text);
  kw.forEach((k) => {
    out = out.replace(new RegExp("\b" + k + "\b", "g"), '<span class="kw">' + k + "</span>");
  });
  return out.replace(/'([^']*)'/g, "<span class=\"str\">'$1'</span>");
}
async function typeSQL(el, sql) {
  if (reduced()) { el.innerHTML = highlightSQL(sql); return; }
  for (let i = 0; i <= sql.length; i += 4) {
    el.innerHTML = highlightSQL(sql.slice(0, i)) + '<span class="caret"></span>';
    await sleep(9);
  }
  el.innerHTML = highlightSQL(sql);
}
async function streamText(el, html) {
  if (reduced()) { el.innerHTML = html; el.setAttribute("aria-live", "polite"); return; }
  const words = html.split(" ");
  for (let i = 0; i < words.length; i++) {
    el.innerHTML = words.slice(0, i + 1).join(" ") + '<span class="caret"></span>';
    await sleep(32);
  }
  el.innerHTML = html;
  el.setAttribute("aria-live", "polite");
}

/* ---------------- stage trace (analyst) / working line (simple) ---------------- */
const STAGES = ["Schema retrieved", "SQL generated", "Validated read-only", "Executed", "Chart ready"];
function buildTrace() {
  const trace = document.createElement("div");
  trace.className = "trace analyst-only" + (UI_MODE !== "analyst" ? " hide" : "");
  trace.innerHTML = STAGES.map((s) => '<span class="stage" data-label="' + esc(s) + '"><span class="tick">o</span>' + esc(s) + "</span>").join("");
  const working = document.createElement("div");
  working.className = "working simple-only" + (UI_MODE === "analyst" ? " hide" : "");
  working.innerHTML = '<span class="spin"></span><span>Working on it&hellip;</span>';
  return { trace, working };
}
function setStage(el, cls) {
  if (!el) return;
  const label = el.dataset.label;
  el.className = "stage " + cls;
  const mark = cls === "run" ? '<span class="spin"></span>'
    : cls === "done" ? '<span class="tick">&check;</span>'
    : cls === "fail" ? '<span class="tick">&times;</span>'
    : '<span class="tick">o</span>';
  el.innerHTML = mark + esc(label);
}

/* ---------------- the thinking rail ---------------- */
function railStage(exchangeEl, node) {
  exchangeEl.classList.add("live");
  if (node) exchangeEl.appendChild(node);
}
function railStop(exchangeEl) {
  exchangeEl.classList.remove("live");
  exchangeEl.classList.add("stopped");
}

/* ============================================================
   CHARTS -- hand-rolled inline SVG.
   colorway cycles chart-1..chart-6, gridlines only (no axis box),
   value labels on the marks themselves, no chart title, no legend
   except donut. viewBox + width:100% so no resize listener is needed.
   ============================================================ */
const PALETTE = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "var(--chart-6)"];

function gridlines(W, H, pad, base, n) {
  let svg = "";
  for (let g = 0; g <= n; g++) {
    const y = pad + (base - pad) * (g / n);
    svg += '<line x1="' + pad + '" x2="' + (W - pad) + '" y1="' + y + '" y2="' + y + '" stroke="var(--chart-grid)" stroke-width="1"/>';
  }
  return svg;
}

function renderBar(a) {
  const W = 640, H = 200, pad = 30, base = H - 28;
  const vals = a.rows.map((r) => Number(r[1]));
  const max = Math.max.apply(null, vals) * 1.16;
  const slot = (W - pad * 2) / vals.length, bw = Math.min(72, slot * 0.56);
  let svg = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Bar chart of ' + esc(a.columns[1]) + ' by ' + esc(a.columns[0]) + '">';
  svg += gridlines(W, H, pad, base, 3);
  vals.forEach((v, i) => {
    const h = Math.max(3, (v / max) * (base - 24));
    const x = pad + slot * i + (slot - bw) / 2;
    const delay = reduced() ? "0s" : (i * 60) + "ms";
    svg += '<rect class="bar" style="transition-delay:' + delay + '" x="' + x.toFixed(1) + '" y="' + (base - h).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="5" fill="' + PALETTE[i % PALETTE.length] + '"/>';
    svg += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (base + 16) + '" text-anchor="middle" font-size="11" fill="var(--chart-label)">' + esc(a.rows[i][0]) + "</text>";
    svg += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (base - h - 7).toFixed(1) + '" text-anchor="middle" font-size="10.5" fill="var(--chart-label)">' + esc(fmt(v)) + "</text>";
  });
  return svg + "</svg>";
}
/* bars ship at final height/y; the .bar CSS transition (height,y) plus a
   forced style flush gives the "grow into place" motion on first paint */
function animateBarsIn(container) {
  if (reduced()) return;
  const rects = container.querySelectorAll(".bar");
  rects.forEach((r) => {
    const h = parseFloat(r.getAttribute("height")), y = parseFloat(r.getAttribute("y"));
    const base = y + h;
    r.setAttribute("height", 0);
    r.setAttribute("y", base);
    r.getBoundingClientRect();
    requestAnimationFrame(() => { r.setAttribute("height", h); r.setAttribute("y", y); });
  });
}

function renderLine(a) {
  const W = 640, H = 200, pad = 30, base = H - 28;
  const vals = a.rows.map((r) => Number(r[1]));
  const max = Math.max.apply(null, vals) * 1.16;
  const step = (W - pad * 2) / (vals.length - 1);
  const pts = vals.map((v, i) => [pad + step * i, base - (v / max) * (base - 24)]);
  let svg = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Line chart of ' + esc(a.columns[1]) + ' over ' + esc(a.columns[0]) + '">';
  svg += gridlines(W, H, pad, base, 3);
  const path = pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L");
  const len = 900;
  svg += '<path class="area" d="M' + path + " L" + pts[pts.length - 1][0].toFixed(1) + " " + base + " L" + pts[0][0].toFixed(1) + " " + base + ' Z" fill="var(--chart-2)"/>';
  svg += '<path class="line-path" style="--len:' + len + '" d="M' + path + '" fill="none" stroke="var(--chart-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" pathLength="' + len + '"/>';
  pts.forEach((p, i) => {
    svg += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="3.5" fill="#fff" stroke="var(--chart-1)" stroke-width="2"/>';
    svg += '<text x="' + p[0].toFixed(1) + '" y="' + (base + 16) + '" text-anchor="middle" font-size="11" fill="var(--chart-label)">' + esc(a.rows[i][0]) + "</text>";
  });
  return svg + "</svg>";
}

function renderDonut(a) {
  const size = 168, r = 62, cx = size / 2, cy = size / 2, sw = 26;
  const total = a.rows.reduce((s, row) => s + Number(row[1]), 0);
  let acc = -90, segs = "";
  const C = 2 * Math.PI * r;
  a.rows.forEach((row, i) => {
    const frac = Number(row[1]) / total;
    const dash = C * frac;
    segs += '<circle class="donut-seg" cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + PALETTE[i % PALETTE.length] +
      '" stroke-width="' + sw + '" stroke-dasharray="' + dash.toFixed(1) + " " + (C - dash).toFixed(1) +
      '" transform="rotate(' + acc + " " + cx + " " + cy + ')"><title>' + esc(row[0]) + ": " + esc(fmt(row[1])) + (a.chart_spec.unit || "") + "</title></circle>";
    acc += frac * 360;
  });
  const svg = '<svg class="chart" viewBox="0 0 ' + size + " " + size + '" style="width:' + size + "px;flex:0 0 " + size + 'px" role="img" aria-label="Donut chart of ' + esc(a.columns[0]) + ' share of ' + esc(a.columns[1]) + '">' + segs + "</svg>";
  const legend = '<div class="legend">' + a.rows.map((row, i) =>
    '<div class="legend-row"><span class="legend-sw" style="background:' + PALETTE[i % PALETTE.length] + '"></span><span>' + esc(row[0]) + '</span><span class="legend-v">' + esc(fmt(row[1])) + (a.chart_spec.unit || "") + "</span></div>"
  ).join("") + "</div>";
  return '<div class="donut-wrap">' + svg + legend + "</div>";
}

function renderDriver(a) {
  const W = 640, H = 210, pad = 30, rowH = (H - 40) / a.rows.length;
  const vals = a.rows.map((r) => Number(r[1]));
  const max = Math.max.apply(null, vals.map(Math.abs)) * 1.25;
  const midX = W / 2, halfW = (W - pad * 2) / 2;
  let svg = '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Driver breakdown of ' + esc(a.columns[0]) + '">';
  svg += '<line x1="' + midX + '" x2="' + midX + '" y1="10" y2="' + (H - 10) + '" stroke="var(--chart-grid)" stroke-width="1"/>';
  a.rows.forEach((row, i) => {
    const v = Number(row[1]);
    const y = 26 + i * rowH;
    const w = Math.max(2, (Math.abs(v) / max) * halfW);
    const x = v >= 0 ? midX : midX - w;
    const fill = v >= 0 ? "var(--pos)" : "var(--neg)";
    svg += '<rect class="bar" x="' + x.toFixed(1) + '" y="' + (y - 8).toFixed(1) + '" width="' + w.toFixed(1) + '" height="16" rx="4" fill="' + fill + '"/>';
    const labelX = v >= 0 ? midX - 8 : midX + 8;
    svg += '<text x="' + labelX + '" y="' + (y + 4) + '" text-anchor="' + (v >= 0 ? "end" : "start") + '" font-size="11" fill="var(--chart-label)">' + esc(row[0]) + "</text>";
    const valX = v >= 0 ? x + w + 6 : x - 6;
    svg += '<text x="' + valX.toFixed(1) + '" y="' + (y + 4) + '" text-anchor="' + (v >= 0 ? "start" : "end") + '" font-size="10.5" fill="var(--chart-label)">' + (v >= 0 ? "+" : "") + v + "</text>";
  });
  return svg + "</svg>";
}

function renderMetric(a) {
  const deltaHtml = a.delta
    ? '<span class="delta ' + (a.delta.dir === "up" ? "delta-up" : "delta-down") + '">' + (a.delta.dir === "up" ? "&uarr;" : "&darr;") + " " + esc(a.delta.label) + "</span>"
    : "";
  return '<div class="metric-big">' + esc(fmt(a.rows[0][1])) + "</div>" + deltaHtml;
}

function renderChart(a) {
  switch (a.chart_spec.type) {
    case "line": return renderLine(a);
    case "donut": return renderDonut(a);
    case "driver": return renderDriver(a);
    case "metric": return renderMetric(a);
    default: return renderBar(a);
  }
}

/* ============================================================
   ANSWER CARD ASSEMBLY -- fixed vertical order, per spec section 6.
   Never reorder: headline, subhead, read-as, vizbar, chart,
   explanation, caveat, sql (collapsed), follow-ups.
   ============================================================ */
function buildReadAs(a) {
  const low = a.confidence < 75;
  const box = document.createElement("div");
  box.className = "readas" + (low ? " low" : "");
  box.innerHTML =
    '<svg class="i"><use href="#i-info"/></svg>' +
    '<div style="flex:1;min-width:0">' +
    "<b>Read as:</b> " + esc(a.interpretation) + " &middot; confidence " + a.confidence + "%" +
    (low
      ? '<div class="readas-actions"><button class="mini act-yes">That&#39;s right</button><button class="mini act-no">Not what I meant</button></div>'
      : "") +
    "</div>";
  return box;
}

function buildVizbar(a, chartHost) {
  const bar = document.createElement("div");
  bar.className = "vizbar";
  const isChartKind = ["bar", "line", "donut", "driver"].includes(a.chart_spec.type);
  const toggle = isChartKind
    ? '<span class="pill-toggle" role="group" aria-label="Chart type">' +
      '<button class="mini act-chart" data-type="' + a.chart_spec.type + '" aria-pressed="true">' + (a.chart_spec.type === "line" ? "Line" : a.chart_spec.type === "donut" ? "Donut" : a.chart_spec.type === "driver" ? "Driver" : "Bar") + "</button>" +
      '<button class="mini act-table" aria-pressed="false">Table</button></span>'
    : "";
  bar.innerHTML =
    '<div class="row" style="gap:6px">' + toggle + "</div>" +
    '<div class="row" style="gap:6px">' +
    '<button class="mini act-pin"><svg class="i"><use href="#i-pin"/></svg>Pin</button>' +
    '<button class="mini act-csv"><svg class="i"><use href="#i-download"/></svg>CSV</button>' +
    '<span class="route analyst-only' + (UI_MODE !== "analyst" ? " hide" : "") + (a.meta.route === "local" ? "" : " remote") + '">' + esc(a.meta.route) + " &middot; " + a.meta.ms + "ms</span>" +
    "</div>";
  return bar;
}

function buildTable(a) {
  const head = "<tr>" + a.columns.map((c, i) => "<th" + (i ? ' class="num"' : "") + ">" + esc(c) + "</th>").join("") + "</tr>";
  const body = a.rows.map((r) => "<tr>" + r.map((v, i) => "<td" + (i ? ' class="num"' : "") + ">" + esc(fmt(v)) + "</td>").join("") + "</tr>").join("");
  return "<table>" + head + body + "</table>";
}

function buildSQLBlock(a) {
  const wrap = document.createElement("div");
  wrap.className = "sql-block analyst-only" + (UI_MODE !== "analyst" ? " hide" : "");
  const btn = document.createElement("button");
  btn.className = "sql-toggle";
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML = '<svg class="i chev"><use href="#i-chevron-right"/></svg><span>Show the SQL</span>';
  const pre = document.createElement("pre");
  pre.className = "sql hide";
  wrap.appendChild(btn);
  wrap.appendChild(pre);
  let typed = false;
  btn.onclick = async () => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.querySelector("span").textContent = open ? "Hide the SQL" : "Show the SQL";
    pre.classList.toggle("hide", !open);
    if (open && !typed) { typed = true; await typeSQL(pre, a.sql); }
  };
  return wrap;
}

function buildChips(a) {
  const row = document.createElement("div");
  row.className = "chips";
  row.innerHTML = '<span class="chips-l">Follow up</span>' +
    a.followups.map((c) => '<button class="chip">' + esc(c) + "</button>").join("");
  row.querySelectorAll(".chip").forEach((c) => (c.onclick = () => askAndRender(c.textContent, $("stream"))));
  return row;
}

async function renderAnswerCard(a, stream) {
  const card = document.createElement("div");
  card.className = "card";

  const head = document.createElement("div");
  head.className = "ans-head";
  head.innerHTML = '<h2 class="answer-headline">' + esc(a.headline) + "</h2>" +
    '<p class="ans-sub">' + esc(a.sub || "") + "</p>";
  card.appendChild(head);

  card.appendChild(buildReadAs(a));

  const vizHost = document.createElement("div");
  const chartArea = document.createElement("div");
  chartArea.className = "chart-box";
  const tableArea = document.createElement("div");
  tableArea.className = "panel-body hide";
  tableArea.innerHTML = buildTable(a);
  card.appendChild(buildVizbar(a, chartArea));
  card.appendChild(chartArea);
  card.appendChild(tableArea);
  vizHost.remove();

  chartArea.innerHTML = renderChart(a);
  if (a.chart_spec.type === "bar" || a.chart_spec.type === "driver") animateBarsIn(chartArea);

  const explain = document.createElement("div");
  explain.className = "explain";
  card.appendChild(explain);

  if (a.caveat) {
    const cav = document.createElement("div");
    cav.className = "caveat";
    cav.innerHTML = '<svg class="i"><use href="#i-alert"/></svg><div><b>Worth checking.</b> ' + a.caveat + "</div>";
    card.appendChild(cav);
  }

  card.appendChild(buildSQLBlock(a));
  card.appendChild(buildChips(a));

  stream.appendChild(card);

  await streamText(explain, a.explanation);

  const chartBtn = card.querySelector(".act-chart"), tableBtn = card.querySelector(".act-table");
  if (chartBtn) {
    chartBtn.onclick = () => {
      chartBtn.setAttribute("aria-pressed", "true"); tableBtn.setAttribute("aria-pressed", "false");
      chartArea.classList.remove("hide"); tableArea.classList.add("hide");
    };
    tableBtn.onclick = () => {
      tableBtn.setAttribute("aria-pressed", "true"); chartBtn.setAttribute("aria-pressed", "false");
      tableArea.classList.remove("hide"); chartArea.classList.add("hide");
    };
  }
  card.querySelector(".act-pin").onclick = (ev) => { toast("Pinned to dashboard"); };
  card.querySelector(".act-csv").onclick = () => downloadCSV(a);
  const yes = card.querySelector(".act-yes"), no = card.querySelector(".act-no");
  if (yes) yes.onclick = () => { card.querySelector(".readas").classList.remove("low"); toast("Interpretation confirmed"); };
  if (no) no.onclick = () => toast("Try rephrasing with the region or time period named");

  return card;
}

function buildEdgeCard(a) {
  const card = document.createElement("div");
  if (a.kind === "clarify") {
    card.className = "card card-clarify edge";
    card.innerHTML =
      '<span class="edge-ico"><svg class="i"><use href="#i-help"/></svg></span>' +
      '<div style="flex:1"><h3>A quick check</h3><p>' + esc(a.explanation) + '</p>' +
      '<div class="edge-opts">' + a.options.map((o) => '<button class="edge-opt"><span>' + esc(o) + '</span><svg class="i"><use href="#i-arrow-right"/></svg></button>').join("") + "</div></div>";
    card.querySelectorAll(".edge-opt").forEach((b) => (b.onclick = () => askAndRender(b.querySelector("span").textContent, $("stream"))));
  } else if (a.kind === "unanswerable") {
    card.className = "card card-unanswerable edge";
    card.innerHTML =
      '<span class="edge-ico"><svg class="i"><use href="#i-info"/></svg></span>' +
      '<div style="flex:1"><h3>Can&#39;t answer that yet</h3><p>' + esc(a.reason) + '</p>' +
      '<div class="needs"><b>What it would take:</b> ' + esc(a.needs) + '</div>' +
      '<div class="edge-opts"><button class="edge-opt"><span>' + esc(a.fallback) + '</span><svg class="i"><use href="#i-arrow-right"/></svg></button></div></div>';
    card.querySelector(".edge-opt").onclick = () => askAndRender(a.fallback, $("stream"));
  } else if (a.kind === "blocked") {
    card.className = "card card-blocked edge";
    card.innerHTML =
      '<span class="edge-ico"><svg class="i"><use href="#i-shield"/></svg></span>' +
      '<div style="flex:1"><h3>' + esc(a.title) + '</h3><p>' + a.body + "</p></div>";
  }
  return card;
}

/* ---------------- export ---------------- */
function toCSV(a) {
  const cell = (v) => { const s = v === null || v === undefined ? "" : String(v); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const lines = [a.columns.map(cell).join(",")];
  a.rows.forEach((r) => lines.push(r.map(cell).join(",")));
  return "﻿" + lines.join("\r\n") + "\r\n";
}
function downloadCSV(a) {
  const blob = new Blob([toCSV(a)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "speakql_" + a.kind + ".csv";
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("CSV exported");
}

/* ---------------- submit / render flow used by 04-05-06 ---------------- */
async function askAndRender(question, stream) {
  const bubble = document.createElement("div");
  bubble.className = "msg-user";
  bubble.textContent = question;
  stream.appendChild(bubble);

  const exchange = document.createElement("div");
  exchange.className = "exchange";
  const { trace, working } = buildTrace();
  exchange.appendChild(trace);
  exchange.appendChild(working);
  stream.appendChild(exchange);
  exchange.classList.add("live");

  const a = matchMock(question);
  const stageEls = trace.querySelectorAll(".stage");

  if (a.kind === "blocked") {
    setStage(stageEls[0], "run"); await sleep(reduced() ? 0 : 380); setStage(stageEls[0], "fail");
    for (let i = 1; i < stageEls.length; i++) stageEls[i].remove();
    working.remove();
    railStop(exchange);
    exchange.appendChild(buildEdgeCard(a));
    return;
  }
  if (a.kind === "unanswerable" || a.kind === "clarify") {
    setStage(stageEls[0], "run"); await sleep(reduced() ? 0 : 380); setStage(stageEls[0], "done");
    for (let i = 1; i < stageEls.length; i++) stageEls[i].remove();
    working.remove();
    exchange.classList.remove("live");
    exchange.appendChild(buildEdgeCard(a));
    return;
  }

  for (let i = 0; i < stageEls.length; i++) {
    setStage(stageEls[i], "run");
    await sleep(reduced() ? 0 : 320);
    setStage(stageEls[i], "done");
  }
  working.remove();
  exchange.classList.remove("live");
  await renderAnswerCard(a, exchange);
}

function matchMock(question) {
  const low = question.toLowerCase();
  const entries = Object.values(MOCK_ANSWERS);
  const found = entries.find((e) => e.question.toLowerCase() === low);
  return found || MOCK_ANSWERS.ranking;
}
