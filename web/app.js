"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
// What router/agents.py can pass to each agent kind.
const MODEL_KINDS = ["claude", "codex", "kimi", "cursor", "opencode", "droid"];
const EFFORT_KINDS = ["claude", "codex", "kimi", "cursor", "droid"];
const AFF_STEPS = [["1", "适合"], ["0.5", "一般"], ["0", "不适合"]];
const SAMPLE_TASKS = [
  "把这个变量重命名",
  "给 NetDemoHud 加一个显示 ping 的文本",
  "批量把所有文件的 Debug.Log 替换成 Logger.Info",
  "review 一下当前的 diff",
  "解释一下 FizzySteamworks 的 relay 是怎么工作的",
  "修复登录偶现的崩溃，原因不明",
  "设计多人联机的状态同步架构",
];

const S = {
  policy: null, base: "", source: "", path: "", installed: {}, models: {}, defaults: null,
  tab: "chart", sel: null, axes: { x: null, y: null }, choice: {},
  map: null, mapKey: "",
  log: [], showLog: true, hidden: new Set(),
  trial: null, trialTask: "", useBackend: true, trialBusy: false,
  errors: [],
};

// ---------------------------------------------------------------- data helpers
const dims = (type) => (S.policy.dimensions || []).filter((d) => !type || d.type === type);
const dimById = (id) => (S.policy.dimensions || []).find((d) => d.id === id);
const nLevels = (d) => (d.levels || []).length;
const profiles = () => S.policy.profiles || [];
const profIndex = (id) => profiles().findIndex((p) => p.id === id);
const profById = (id) => profiles().find((p) => p.id === id) || null;
const selProfile = () => profById(S.sel);
// A profile's coordinate on a score dimension; undefined = it does not care about that dimension.
const getPoint = (p, d) => (p.point && typeof p.point[d.id] === "number" ? p.point[d.id] : undefined);
function setPoint(p, d, v) {
  if (v == null) { if (p.point) delete p.point[d.id]; if (p.point && !Object.keys(p.point).length) delete p.point; return; }
  p.point = p.point || {}; p.point[d.id] = Math.round(v * 100) / 100;
}
const shortLevel = (t) => { const s = String(t).split(":")[0].trim(); return s.length > 18 ? s.slice(0, 17) + "…" : s; };
const isInstalled = (p) => S.installed[p.kind] !== false;
function specialty(p) {
  // "only review" style profiles: affinity default 0 on a choice dimension
  for (const aff of Object.values(p.affinity || {})) {
    if (Number(aff["*"] ?? 1) === 0) {
      const ok = Object.entries(aff).filter(([k, v]) => k !== "*" && Number(v) > 0).map(([k]) => k);
      return ok.length ? ok.join("/") : "—";
    }
  }
  return null;
}
const isDirty = () => JSON.stringify(S.policy) !== S.base;
const clone = (o) => JSON.parse(JSON.stringify(o));
function uniqueId(base, taken) { let i = 1, id = base; while (taken.includes(id)) id = `${base}-${++i}`; return id.slice(0, 32); }

// The policy's model catalog: model id -> {label, vendor, via: {kind: that agent's model id}}.
// A profile's model entry is the one whose `via` for its kind is the profile's model.
const catalogModels = () => S.policy.models || {};
function modelEntry(p) {
  for (const [id, m] of Object.entries(catalogModels())) if (m.via && p.model && m.via[p.kind] === p.model) return { id, ...m };
  return null;
}
// Colour belongs to the model's vendor (as on a model leaderboard), not to the agent
// that runs it; profiles of one vendor get different lightness so territories stay apart.
const VENDORS = {
  anthropic: ["Anthropic", [15, 63, 62]], openai: ["OpenAI", [0, 0, 92]], zai: ["Z.ai", [205, 90, 60]],
  deepseek: ["DeepSeek", [235, 85, 72]], xiaomi: ["Xiaomi", [48, 95, 55]], moonshot: ["Moonshot", [170, 60, 50]],
  google: ["Google", [140, 55, 55]], cursor: ["Cursor", [290, 70, 72]],
};
const KIND_VENDOR = { claude: "anthropic", codex: "openai", kimi: "moonshot", cursor: "cursor", gemini: "google" };
const NATIVE_KIND = { anthropic: "claude", openai: "codex", moonshot: "kimi", cursor: "cursor" };
const vendorOf = (p) => (modelEntry(p) || {}).vendor || KIND_VENDOR[p.kind] || p.kind;
const vendorName = (v) => (VENDORS[v] || [v])[0];
const SHADE = [0, -16, 8, -28, 14, -38];
const hashHue = (s) => [...String(s)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % 360;
const vendorHsl = (v) => (VENDORS[v] || [v, [hashHue(v), 62, 64]])[1];
const vendorColor = (v) => { const [h, s, l] = vendorHsl(v); return `hsl(${h} ${s}% ${l}%)`; };
// One shade per model (so an effort line is one colour); models of one vendor differ.
const shadeKey = (p) => (modelEntry(p) || {}).id || p.id;
function color(id, a = 1) {
  const p = profById(id);
  if (!p) return `rgb(161 161 161 / ${a})`;
  const v = vendorOf(p), [h, s, l] = vendorHsl(v);
  const keys = [...new Set(profiles().filter((q) => vendorOf(q) === v).map(shadeKey))];
  const k = keys.indexOf(shadeKey(p));
  return `hsl(${h} ${s}% ${clamp(l + SHADE[k % SHADE.length], 32, 96)}% / ${a})`;
}
// Agents that can run this profile's model, from the catalog (the current kind always included).
function harnesses(p) {
  const e = modelEntry(p), ks = e ? Object.keys(e.via) : [];
  return ks.includes(p.kind) ? ks : [p.kind, ...ks];
}
function switchHarness(p, kind) {
  const e = modelEntry(p);
  if (!e || !e.via[kind] || kind === p.kind) return;
  p.kind = kind; p.model = e.via[kind];
}
const harnessSeg = (p) => `<span class="seg harness">${harnesses(p).map((k) => `<button data-harness="${esc(k)}" class="${k === p.kind ? "on" : ""} ${S.installed[k] === false ? "warn" : ""}" title="${esc(k)}${S.installed[k] === false ? "（未安装）" : ""}">${esc(k)}</button>`).join("")}</span>`;
const swatch = (p) => `<span class="swatch ${specialty(p) ? "ring" : ""}" style="background:${color(p.id)};color:${color(p.id)}"></span>`;

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (res.status === 401) { showAuth(); throw new Error("unauthorized"); }
  return { status: res.status, body: await res.json().catch(() => ({})) };
}
function showAuth() {
  $("#main").innerHTML = `<div class="card pad"><h3>需要访问令牌</h3><p>请用 <code>herdr-router ui</code> 在终端打印的、带 <code>?token=</code> 的链接打开本页。打开一次后会记住。</p></div>`;
}
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

async function load() {
  const { body } = await api("/api/policy");
  S.policy = body.policy; S.base = JSON.stringify(body.policy);
  S.source = body.source; S.path = body.path; S.installed = body.installed || {};
  const sd = dims("score").map((d) => d.id);
  if (!sd.includes(S.axes.x)) S.axes.x = sd.includes("duration") ? "duration" : sd[0];
  if (!sd.includes(S.axes.y)) S.axes.y = sd.includes("difficulty") ? "difficulty" : (sd.find((x) => x !== S.axes.x) || sd[0]);
  if (S.sel && profIndex(S.sel) < 0) S.sel = null;
  try { S.log = (await api("/api/log?limit=200")).body.entries || []; } catch { S.log = []; }
  S.errors = [];
  render();
  // a saved policy replaces the built-in default wholesale, so new default profiles never
  // show up by themselves; the profile list offers to add them
  api("/api/policy/default").then(({ body: d }) => { S.defaults = d.policy || null; if (S.tab === "chart" && !S.sel) renderEditor(); }).catch(() => {});
  // cursor-agent --list-models can take a few seconds on a cold cache; do not block the page on it
  api("/api/models").then(({ body: m }) => { S.models = m.models || {}; if (S.tab === "chart" && S.sel) renderEditor(); }).catch(() => {});
}

// ---------------------------------------------------------------- territory map
// The server samples who wins a task at each point of the plane (score.territory),
// so the map always agrees with real routing. Refreshes are debounced while typing
// and throttled while dragging, so the map follows a dot as it moves.
let mapTimer, mapSeq = 0, mapBusy = false, mapAgain = false;
function refreshMap(delay = 150) {
  clearTimeout(mapTimer);
  mapTimer = setTimeout(fetchMap, delay);
}
async function fetchMap() {
  // coarse while a dot is being dragged (fast enough to follow it), fine once it is dropped
  const req = JSON.stringify({ policy: S.policy, x: S.axes.x, y: S.axes.y, choice: S.choice, steps: drag ? 48 : 96 });
  if (req === S.mapKey) return;
  if (mapBusy) { mapAgain = true; return; }
  mapBusy = true;
  const seq = ++mapSeq;
  let res;
  try { res = await api("/api/map", { method: "POST", body: req }); } catch { mapBusy = false; return; }
  mapBusy = false;
  if (seq === mapSeq) {
    S.mapKey = req;
    S.map = res.status === 200 ? { ...res.body, xd: S.axes.x, yd: S.axes.y, url: "", urlKey: "" } : null;
    if (S.tab === "chart") { renderChart(); if (!S.sel) renderEditor(); }
  }
  if (mapAgain) { mapAgain = false; fetchMap(); }
}
const mapReady = () => S.map && S.map.xd === S.axes.x && S.map.yd === S.axes.y;
function winnerAt(x, y) {
  if (!mapReady()) return null;
  const m = S.map, n = m.grid.length;
  const i = clamp(Math.floor(((x - m.x[0]) / (m.x[1] - m.x[0])) * n), 0, n - 1);
  const j = clamp(Math.floor(((y - m.y[0]) / (m.y[1] - m.y[0])) * n), 0, n - 1);
  const k = m.grid[j][i];
  return k >= 0 ? m.ids[k] : null;
}
function shares() {
  const n = {};
  if (!mapReady()) return n;
  let total = 0;
  for (const row of S.map.grid) for (const k of row) { total++; if (k >= 0) n[S.map.ids[k]] = (n[S.map.ids[k]] || 0) + 1; }
  for (const id in n) n[id] = n[id] / total;
  return n;
}
// The sampled grid is painted one pixel per sample, then scaled up to the plot's
// pixel size with smoothing, which softens the stair steps between territories.
// Thin borders mark where the winner changes.
function mapImage(w, h) {
  if (!mapReady()) return "";
  const dpr = Math.min(2, window.devicePixelRatio || 1), W = Math.round(w * dpr), H = Math.round(h * dpr);
  const key = `${W}x${H}`;
  if (S.map.url && S.map.urlKey === key) return S.map.url;
  const grid = S.map.grid, n = grid.length;
  const small = document.createElement("canvas");
  small.width = small.height = n;
  const sc = small.getContext("2d");
  grid.forEach((row, j) => row.forEach((k, i) => {
    if (k < 0) return;
    sc.fillStyle = color(S.map.ids[k], 0.3);
    sc.fillRect(i, n - 1 - j, 1, 1);
  }));
  const big = document.createElement("canvas");
  big.width = W; big.height = H;
  const bc = big.getContext("2d");
  bc.imageSmoothingEnabled = true; bc.imageSmoothingQuality = "high";
  bc.drawImage(small, 0, 0, W, H);
  const sx = W / n, sy = H / n;
  bc.strokeStyle = "rgba(255,255,255,0.28)"; bc.lineWidth = dpr;
  bc.beginPath();
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const k = grid[j][i], top = (n - 1 - j) * sy;
    if (i + 1 < n && grid[j][i + 1] !== k) { bc.moveTo((i + 1) * sx, top); bc.lineTo((i + 1) * sx, top + sy); }
    if (j + 1 < n && grid[j + 1][i] !== k) { bc.moveTo(i * sx, top); bc.lineTo((i + 1) * sx, top); }
  }
  bc.stroke();
  S.map.urlKey = key;
  return (S.map.url = big.toDataURL());
}

// ---------------------------------------------------------------- top-level render
function render() {
  if (!S.policy) return;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  renderStatus();
  renderErrors();
  const main = $("#main");
  if (S.tab === "chart") main.innerHTML = `${cmdbarHtml()}<div class="split"><div><div class="card">${toolbarHtml()}<div id="chart"></div><div class="chips offplane" id="offplane"></div><div class="cellinfo" id="cellinfo"></div>${legendHtml()}</div><div id="trialbox">${trialBoxHtml()}</div></div><aside class="card" id="editor"></aside></div>`;
  if (S.tab === "dims") main.innerHTML = dimsHtml();
  if (S.tab === "trial") main.innerHTML = trialHtml();
  if (S.tab === "settings") main.innerHTML = settingsHtml();
  if (S.tab === "json") main.innerHTML = jsonHtml();
  if (S.tab === "chart") { renderChart(); renderEditor(); refreshMap(0); }
  markErrors();
}
function renderStatus() {
  $("#dirty").hidden = !isDirty();
  $("#source").textContent = S.source === "user" ? S.path.replace(/^\/(Users|home)\/[^/]+/, "~") : "内置默认值 · 保存后写入配置目录";
}
function renderErrors() {
  const box = $("#errors");
  box.hidden = !S.errors.length;
  box.innerHTML = S.errors.length ? `<strong>有 ${S.errors.length} 处配置错误，未保存：</strong><ul>${S.errors.map((e, i) => `<li data-err="${i}"><code>${esc(e.path)}</code> ${esc(e.message)}</li>`).join("")}</ul>` : "";
}
function markErrors() {
  for (const e of S.errors) document.querySelectorAll(`[data-path="${CSS.escape(e.path)}"]`).forEach((el) => el.classList.add("err"));
}
function changed(opts = {}) {
  renderStatus();
  if (opts.all) render();
  else if (S.tab === "chart") {
    if (opts.chart) renderChart();
    if (opts.editor || (opts.list && !S.sel)) renderEditor();
  }
  if (S.tab === "chart") refreshMap();
}

// ---------------------------------------------------------------- chart tab chrome
function cmdbarHtml() {
  return `<div class="cmdbar"><span class="prompt">›</span>
    <input type="text" id="cmd" value="${esc(S.trialTask)}" placeholder="输入一个任务回车：看它落在哪、派给谁（不会真的派发）" spellcheck="false" autocomplete="off">
    <label class="check" title="用决策模型 Jev 判断；关掉则只用关键词规则"><input type="checkbox" data-act="toggle-backend" ${S.useBackend ? "checked" : ""}>Jev</label>
    <button class="primary" data-act="run-trial" ${S.trialBusy ? "disabled" : ""}>${S.trialBusy ? "判断中…" : "试运行"}</button></div>`;
}
function toolbarHtml() {
  const opts = (sel) => dims("score").map((d) => `<option value="${esc(d.id)}" ${d.id === sel ? "selected" : ""}>${esc(d.label || d.id)}</option>`).join("");
  const choice = dims("choice").map((d) => `<label>${esc(d.label || d.id)} <select data-choice="${esc(d.id)}"><option value="">任意</option>${Object.keys(d.options).map((o) => `<option value="${esc(o)}" ${S.choice[d.id] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`).join("");
  return `<div class="toolbar"><label>横轴 <select data-axis="x">${opts(S.axes.x)}</select></label><label>纵轴 <select data-axis="y">${opts(S.axes.y)}</select></label>${choice}
    <label class="check"><input type="checkbox" data-act="toggle-log" ${S.showLog ? "checked" : ""}>历史 ${S.log.length}</label></div>`;
}
function legendHtml() {
  return `<div class="legend"><span><span class="lg-sq"></span>底色 = 任务落在这里会派给谁</span><span><span class="lg-fill"></span>颜色 = 模型厂商，拖到任意位置</span><span><span class="lg-ring"></span>专才</span><span><span class="lg-ring dash"></span>未安装</span><span><span class="lg-line"></span>不挑这个轴</span><span><span class="lg-eff"></span>同一模型的思考档位 low → max</span></div>`;
}

// ---------------------------------------------------------------- chart
const G = { W: 760, H: 500, L: 112, R: 12, T: 26, B: 46 };
const LABEL_H = 15, SNAP = 0.05;
function textW(text, px = 11.5) {
  let w = 0;
  for (const ch of text) w += /[⺀-￿]/.test(ch) ? px : px * 0.62;
  return w;
}
function fitText(text, maxW, px = 11.5) {
  if (textW(text, px) <= maxW) return text;
  let out = "";
  for (const ch of text) { if (textW(out + ch + "…", px) > maxW) break; out += ch; }
  return out + "…";
}
function fitGeometry() {
  // Draw at the container's real pixel width so text keeps its size on phones.
  const host = $("#chart");
  const w = Math.round(Math.max(300, Math.min(1000, (host && host.clientWidth) || 760)));
  const narrow = w < 560;
  G.W = w; G.H = Math.round(clamp(w * 0.64, 330, 580)); G.L = narrow ? 70 : 118; G.B = narrow ? 40 : 46;
}
function geom() {
  const xd = dimById(S.axes.x), yd = dimById(S.axes.y);
  const nx = nLevels(xd), ny = nLevels(yd);
  const gw = G.W - G.L - G.R, gh = G.H - G.T - G.B;
  return { xd, yd, nx, ny, gw, gh, cw: gw / nx, ch: gh / ny };
}
// The plane spans [-0.5, levels - 0.5]; level k sits at the middle of its band.
const px = (g, v) => G.L + (v + 0.5) * g.cw;
const py = (g, v) => G.T + (g.ny - v - 0.5) * g.ch;
const fmt = (v) => (Math.round(v * 100) / 100).toFixed(2).replace(/0$/, "");

// Where a profile is drawn. A profile without a coordinate on an axis does not
// care about it; it becomes a dashed line along that axis, handle at the far end.
function anchors(g, list) {
  const out = new Map(), groups = new Map();
  for (const p of list) {
    const vx = getPoint(p, g.xd), vy = getPoint(p, g.yd);
    if (vx == null && vy == null) continue;
    const a = { vx, vy, x: vx == null ? G.L + g.gw - 10 : px(g, vx), y: vy == null ? G.T + 10 : py(g, vy) };
    const k = `${Math.round(a.x)}|${Math.round(a.y)}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push([p, a]);
    out.set(p.id, a);
  }
  // profiles parked on the same spot fan out on a small circle (drawing only)
  for (const g2 of groups.values()) {
    const n = g2.length, R = n > 1 ? 10 + n * 2 : 0;
    g2.forEach(([p, a], k) => {
      const ang = -Math.PI / 2 + (2 * Math.PI * k) / n;
      a.dx = a.x + R * Math.cos(ang); a.dy = a.y + R * Math.sin(ang);
    });
  }
  return out;
}

// Greedy placement; a label with no free spot is dropped (its dot keeps a tooltip),
// except for the ids in `must`, which fall back to the right of their dot.
function placeLabels(g, order, pos, text, must = new Set()) {
  const boxes = [];
  const dots = [...pos.values()].map((c) => ({ x0: c.dx - 8, x1: c.dx + 8, y0: c.dy - 8, y1: c.dy + 8 }));
  const hit = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
  const inside = (b) => b.x0 >= G.L + 2 && b.x1 <= G.W - G.R - 2 && b.y0 >= G.T + 2 && b.y1 <= G.T + g.gh - 2;
  const free = (b) => inside(b) && !boxes.some((o) => hit(o, b)) && !dots.some((d) => hit(d, b));
  const out = new Map();
  for (const p of order) {
    const c = pos.get(p.id), cx = c.dx, cy = c.dy;
    const t = fitText(text(p), g.gw * 0.6), w = textW(t) + 4, h = LABEL_H;
    const near = [[cx + 11, cy - h / 2], [cx - 11 - w, cy - h / 2], [cx - w / 2, cy - 12 - h], [cx - w / 2, cy + 12]];
    let box = null, leader = false;
    for (const [x0, y0] of near) { const b = { x0, y0, x1: x0 + w, y1: y0 + h }; if (free(b)) { box = b; break; } }
    for (let r = 30; !box && r <= 120; r += 15) {
      for (let k = 0; k < 16 && !box; k++) {
        const a = (2 * Math.PI * k) / 16, bx = cx + r * Math.cos(a), by = cy + r * Math.sin(a);
        const b = { x0: bx - w / 2, y0: by - h / 2, x1: bx + w / 2, y1: by + h / 2 };
        if (free(b)) { box = b; leader = true; }
      }
    }
    if (!box && !must.has(p.id)) continue;
    if (!box) box = { x0: cx + 11, y0: cy - h / 2, x1: cx + 11 + w, y1: cy + h / 2 };
    boxes.push(box);
    out.set(p.id, { box, leader, t });
  }
  return out;
}
function labelText(p) {
  const native = NATIVE_KIND[vendorOf(p)], via = native && native !== p.kind ? ` · ${p.kind}` : "";
  return `${p.label || p.id}${via}${specialty(p) ? ` · ${specialty(p)}` : ""}${p.enabled === false ? "（停用）" : ""}`;
}
// Effort variants of one model (same catalog model, same agent, not specialists) form a line,
// drawn low -> max like the per-model curves on Artificial Analysis.
function effortLines(list) {
  const groups = new Map();
  for (const p of list) {
    const e = modelEntry(p);
    if (!e || !p.effort || specialty(p)) continue;
    const k = `${e.id}|${p.kind}`;
    if (!groups.has(k)) groups.set(k, { entry: e, ps: [] });
    groups.get(k).ps.push(p);
  }
  const lines = [...groups.values()].filter((g) => g.ps.length > 1);
  for (const g of lines) g.ps.sort((a, b) => EFFORTS.indexOf(a.effort) - EFFORTS.indexOf(b.effort));
  return lines;
}

function renderChart() {
  const host = $("#chart"); if (!host) return;
  fitGeometry();
  const g = geom(), { xd, yd, nx, ny, gw, gh, cw, ch } = g;
  const X = (i) => G.L + i * cw, Y = (j) => G.T + (ny - j) * ch; // band edges
  let s = `<svg class="chart" viewBox="0 0 ${G.W} ${G.H}" role="img" aria-label="档位领地图">`;
  s += `<rect class="plane" x="${G.L}" y="${G.T}" width="${gw}" height="${gh}" fill="transparent"/>`;
  const img = mapImage(gw, gh);
  if (img) s += `<image href="${img}" x="${G.L}" y="${G.T}" width="${gw}" height="${gh}" preserveAspectRatio="none" pointer-events="none"/>`;
  for (let i = 0; i < nx; i++) s += `<line class="gridline" x1="${px(g, i)}" x2="${px(g, i)}" y1="${G.T}" y2="${G.T + gh}"/>`;
  for (let j = 0; j < ny; j++) s += `<line class="gridline" x1="${G.L}" x2="${G.L + gw}" y1="${py(g, j)}" y2="${py(g, j)}"/>`;
  s += `<rect class="frame" x="${G.L}" y="${G.T}" width="${gw}" height="${gh}"/>`;
  s += `<g id="hov" visibility="hidden"><line class="cross" id="hov-x"/><line class="cross" id="hov-y"/><circle class="target" id="hov-c" r="5"/></g>`;

  // axes: ticks at each level
  xd.levels.forEach((t, i) => {
    const cx = px(g, i);
    s += `<text class="tick-i" x="${cx}" y="${G.T + gh + 15}" text-anchor="middle">${i}<title>${esc(t)}</title></text>`;
    s += `<text class="tick" x="${cx}" y="${G.T + gh + 29}" text-anchor="middle">${esc(fitText(shortLevel(t), cw - 8, 11))}<title>${esc(t)}</title></text>`;
  });
  yd.levels.forEach((t, j) => {
    s += `<text class="tick" x="6" y="${py(g, j) + 4}"><tspan class="tick-i">${j}</tspan> ${esc(fitText(shortLevel(t), G.L - 30, 11))}<title>${esc(t)}</title></text>`;
  });
  s += `<text class="axis-title" x="${G.L + gw}" y="${G.H - 3}" text-anchor="end">${esc(xd.label || xd.id)} →</text>`;
  s += `<text class="axis-title" x="6" y="${G.T - 10}">${esc(yd.label || yd.id)} ↑</text>`;

  // history: where earlier dispatches landed (expected values)
  if (S.showLog) S.log.forEach((e, k) => {
    const ex = e.expected && e.expected[xd.id], ey = e.expected && e.expected[yd.id];
    if (typeof ex !== "number" || typeof ey !== "number") return;
    const jx = ((k * 37) % 9 - 4) * 1.2, jy = ((k * 53) % 9 - 4) * 1.2;
    s += `<circle class="hist" cx="${px(g, ex) + jx}" cy="${py(g, ey) + jy}" r="2.5"><title>${esc(e.task)} → ${esc(e.profile_id)}</title></circle>`;
  });

  // trial target
  const ta = S.trial && (S.trial.assessment || {});
  const te = ta && ta.expected;
  if (te && typeof te[xd.id] === "number" && typeof te[yd.id] === "number") {
    const cx = px(g, te[xd.id]), cy = py(g, te[yd.id]);
    s += `<line class="cross" x1="${cx}" x2="${cx}" y1="${G.T}" y2="${G.T + gh}"/><line class="cross" x1="${G.L}" x2="${G.L + gw}" y1="${cy}" y2="${cy}"/>`;
    s += `<circle class="target" cx="${cx}" cy="${cy}" r="9"/><circle cx="${cx}" cy="${cy}" r="2.5" fill="var(--ink)"/>`;
  }

  const sh = shares();
  const visible = profiles().filter((p) => !S.hidden.has(p.id));
  const pos = anchors(g, visible);
  const placed = visible.filter((p) => pos.has(p.id));
  const sp = selProfile(), trialWinner = S.trial && S.trial.profile_id;

  // selected profile: guides to both axes with its coordinates
  if (sp && pos.has(sp.id)) {
    const a = pos.get(sp.id);
    if (a.vx != null) s += `<line class="guide" x1="${a.x}" x2="${a.x}" y1="${a.y}" y2="${G.T + gh}"/><text class="guide-v" x="${a.x}" y="${G.T + gh - 4}" text-anchor="middle">${fmt(a.vx)}</text>`;
    if (a.vy != null) s += `<line class="guide" x1="${G.L}" x2="${a.x}" y1="${a.y}" y2="${a.y}"/><text class="guide-v" x="${G.L + 4}" y="${a.y - 4}">${fmt(a.vy)}</text>`;
  }
  // profiles that ignore one axis: a dashed line along it
  for (const p of placed) {
    const a = pos.get(p.id);
    if (a.vx == null) s += `<line class="axisfree" x1="${G.L}" x2="${G.L + gw}" y1="${a.y}" y2="${a.y}" stroke="${color(p.id)}"/>`;
    if (a.vy == null) s += `<line class="axisfree" x1="${a.x}" x2="${a.x}" y1="${G.T}" y2="${G.T + gh}" stroke="${color(p.id)}"/>`;
  }

  // effort lines under the dots; on a line only the top effort carries the model name
  const lines = effortLines(placed), lineOf = new Map();
  for (const ln of lines) {
    const pts = ln.ps.map((p) => pos.get(p.id)).map((a) => `${a.dx},${a.dy}`).join(" ");
    const off = ln.ps.every((p) => p.enabled === false);
    s += `<polyline class="effline" points="${pts}" stroke="${color(ln.ps[0].id)}" ${off ? 'stroke-dasharray="3 4"' : ""}/>`;
    ln.ps.forEach((p, k) => lineOf.set(p.id, { ln, head: k === ln.ps.length - 1 }));
  }
  const chartLabel = (p) => {
    const l = lineOf.get(p.id);
    if (!l || p.id === S.sel || p.id === trialWinner) return labelText(p);
    return l.head ? `${l.ln.entry.label} · ${p.effort}${p.enabled === false ? "（停用）" : ""}` : `${p.effort}${p.enabled === false ? "（停用）" : ""}`;
  };
  const rankKey = (p) => (p.id === S.sel ? -2 : p.id === trialWinner ? -1 : lineOf.get(p.id)?.head ? -1 + 1e-3 : -(sh[p.id] || 0));
  const order = [...placed].sort((a, b) => rankKey(a) - rankKey(b));
  const labels = placeLabels(g, order, pos, chartLabel, new Set([S.sel, trialWinner].filter(Boolean)));
  for (const p of order) {
    if (!labels.has(p.id)) continue;
    const { box, leader } = labels.get(p.id), c = pos.get(p.id);
    if (leader) s += `<line class="leader" x1="${c.dx}" y1="${c.dy}" x2="${clamp(c.dx, box.x0, box.x1)}" y2="${clamp(c.dy, box.y0, box.y1)}"/>`;
  }
  for (const p of [...order].reverse()) {
    const c = pos.get(p.id), col = color(p.id), spec = specialty(p), inst = isInstalled(p);
    const body = spec || !inst
      ? `<circle cx="${c.dx}" cy="${c.dy}" r="5.5" fill="var(--bg)" stroke="${col}" stroke-width="2" ${inst ? "" : 'stroke-dasharray="2 2"'}/>`
      : `<circle cx="${c.dx}" cy="${c.dy}" r="6" fill="${col}" stroke="var(--bg)" stroke-width="1.5"/>`;
    s += `<g class="dotg" data-drag="move" data-id="${esc(p.id)}" opacity="${p.enabled === false ? 0.35 : 1}"><circle cx="${c.dx}" cy="${c.dy}" r="10" fill="transparent"/>${body}`;
    if (p.id === S.sel) s += `<circle class="dot-ring" cx="${c.dx}" cy="${c.dy}" r="10"/>`;
    if (p.id === trialWinner) s += `<circle class="pulse" cx="${c.dx}" cy="${c.dy}" r="7"/>`;
    s += `<title>${esc(labelText(p))}${p.note ? "\n" + esc(p.note) : ""}\n${esc(p.kind)}${p.model ? " · " + esc(p.model) : ""}${p.effort ? " · " + esc(p.effort) : ""}${inst ? "" : "\n未安装"}</title></g>`;
  }
  for (const p of order) {
    if (!labels.has(p.id)) continue;
    const { box, t } = labels.get(p.id);
    const strong = p.id === S.sel || p.id === trialWinner || (sh[p.id] || 0) > 0 || !mapReady();
    const small = lineOf.has(p.id) && !lineOf.get(p.id).head && p.id !== S.sel && p.id !== trialWinner;
    s += `<text class="plabel ${small ? "eff" : ""}" x="${box.x0 + 2}" y="${box.y0 + 11.5}" fill="${strong ? "var(--ink)" : "var(--muted)"}" font-weight="${p.id === S.sel ? 700 : 500}" opacity="${p.enabled === false ? 0.5 : 1}">${esc(t)}</text>`;
  }
  // the agent switcher sits under the selected dot (not while dragging it)
  let bar = "";
  if (sp && pos.has(sp.id) && !drag && harnesses(sp).length > 1) {
    const a = pos.get(sp.id), k = (host.clientWidth || G.W) / G.W;
    const below = a.dy < G.T + g.gh - 44;
    bar = `<div class="dotbar" style="left:${clamp(a.dx * k, 90, (G.W - 90) * k)}px;top:${(below ? a.dy + 16 : a.dy - 44) * k}px">${harnessSeg(sp).replace(/data-harness=/g, `data-id="${esc(sp.id)}" data-harness=`)}</div>`;
  }
  host.innerHTML = s + "</svg>" + bar;

  // profiles with no coordinate on either axis cannot be drawn on this plane
  const off = visible.filter((p) => !pos.has(p.id));
  const offBox = $("#offplane");
  if (offBox) offBox.innerHTML = off.length ? `<span class="hint">不挑这两个轴：</span>${off.map((p) => `<span class="chip ${p.id === S.sel ? "on" : ""}" data-pick="${esc(p.id)}">${swatch(p)}${esc(labelText(p))}</span>`).join("")}` : "";
}

// ---------------------------------------------------------------- chart interaction
let drag = null, hoverTimer, hoverSeq = 0;
function svgPoint(e) {
  const svg = $("#chart svg");
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}
// pointer -> plane coordinates, unclamped
function valAt(e) {
  const g = geom(), p = svgPoint(e);
  return { x: (p.x - G.L) / g.cw - 0.5, y: g.ny - 0.5 - (p.y - G.T) / g.ch, inside: p.x >= G.L && p.x <= G.L + g.gw && p.y >= G.T && p.y <= G.T + g.gh };
}
const snap = (v, hi) => clamp(Math.round(v / SNAP) * SNAP, 0, hi);
function readout(v) {
  const info = $("#cellinfo"), hov = $("#hov"); if (!info || !hov) return;
  if (!v) { hov.setAttribute("visibility", "hidden"); info.innerHTML = ""; return; }
  const g = geom(), cx = px(g, v.x), cy = py(g, v.y);
  $("#hov-x").setAttribute("x1", cx); $("#hov-x").setAttribute("x2", cx); $("#hov-x").setAttribute("y1", G.T); $("#hov-x").setAttribute("y2", G.T + g.gh);
  $("#hov-y").setAttribute("y1", cy); $("#hov-y").setAttribute("y2", cy); $("#hov-y").setAttribute("x1", G.L); $("#hov-y").setAttribute("x2", G.L + g.gw);
  $("#hov-c").setAttribute("cx", cx); $("#hov-c").setAttribute("cy", cy);
  hov.setAttribute("visibility", "visible");
  const where = `${esc(g.xd.label || g.xd.id)} ${fmt(v.x)} × ${esc(g.yd.label || g.yd.id)} ${fmt(v.y)}`;
  const w = winnerAt(v.x, v.y), name = (id) => esc((profById(id) || {}).label || id);
  info.innerHTML = `${where}${w ? ` → <b>${name(w)}</b>` : ""}`;
  // runner-up penalties come from the server, a beat later
  clearTimeout(hoverTimer);
  const seq = ++hoverSeq;
  hoverTimer = setTimeout(async () => {
    let res;
    try { res = await api("/api/map", { method: "POST", body: JSON.stringify({ policy: S.policy, x: S.axes.x, y: S.axes.y, choice: S.choice, at: [v.x, v.y] }) }); } catch { return; }
    if (seq !== hoverSeq || res.status !== 200) return;
    const [first, ...rest] = res.body.top;
    info.innerHTML = `${where} → ${first ? `<b>${name(first.id)}</b> ${first.total}` : "无可用档位"}${rest.length ? ` <span class="dim">· 次选 ${rest.map((r) => `${name(r.id)} ${r.total}`).join(" · ")}</span>` : ""}`;
  }, 140);
}
function onChartDown(e) {
  const host = $("#chart"); if (!host || !host.contains(e.target) || e.target.closest(".dotbar")) return;
  const t = e.target.closest("[data-drag]");
  if (!t) {
    const v = valAt(e);
    if (!v.inside) return;
    if (S.sel) { S.sel = null; renderChart(); renderEditor(); }
    readout(v);
    return;
  }
  e.preventDefault();
  const p = profById(t.dataset.id); if (!p) return;
  const { xd, yd } = geom();
  if (S.sel !== p.id) { S.sel = p.id; renderEditor(); }
  drag = { p, start: valAt(e), x: getPoint(p, xd), y: getPoint(p, yd), moved: false };
  host.setPointerCapture(e.pointerId);
  renderChart();
}
function onChartMove(e) {
  const host = $("#chart");
  if (!drag) {
    if (host && e.pointerType === "mouse" && host.contains(e.target) && $("#chart svg")) {
      const v = valAt(e);
      if (v.inside) readout(v); else readout(null);
    }
    return;
  }
  const { xd, yd, nx, ny } = geom(), v = valAt(e);
  const nxv = drag.x == null ? null : snap(drag.x + v.x - drag.start.x, nx - 1);
  const nyv = drag.y == null ? null : snap(drag.y + v.y - drag.start.y, ny - 1);
  if (nxv === getPoint(drag.p, xd) && nyv === getPoint(drag.p, yd)) return;
  if (nxv != null) setPoint(drag.p, xd, nxv);
  if (nyv != null) setPoint(drag.p, yd, nyv);
  drag.moved = true;
  renderChart(); renderStatus();
  const info = $("#cellinfo");
  if (info) info.innerHTML = `<b>${esc(drag.p.label || drag.p.id)}</b> → ${[nxv != null && `${esc(xd.label || xd.id)} ${fmt(nxv)}`, nyv != null && `${esc(yd.label || yd.id)} ${fmt(nyv)}`].filter(Boolean).join(" · ")}`;
  refreshMap(0); // throttled by fetchMap: one request in flight, the latest state follows
}
function onChartUp() {
  if (!drag) return;
  const moved = drag.moved; drag = null;
  if (moved) changed({ editor: true });
  renderChart(); // the agent switcher is hidden while a dot is held
}

// ---------------------------------------------------------------- inspector
function renderEditor() {
  const host = $("#editor"); if (!host) return;
  const p = selProfile();
  host.innerHTML = p ? editorHtml(p) : listHtml();
  markErrors();
}
function newDefaults() {
  if (S.source !== "user" || !S.defaults) return [];
  const have = new Set(profiles().map((p) => p.id));
  return (S.defaults.profiles || []).filter((p) => !have.has(p.id));
}
function mergeDefaults() {
  const add = newDefaults(); if (!add.length) return;
  S.policy.models = S.policy.models || {};
  for (const [id, m] of Object.entries(S.defaults.models || {})) if (!(id in S.policy.models)) S.policy.models[id] = clone(m);
  for (const p of add) profiles().push(clone(p));
  const ids = new Set(profiles().map((p) => p.id));
  S.policy.aliases = S.policy.aliases || {};
  for (const [k, v] of Object.entries(S.defaults.aliases || {})) if (!(k in S.policy.aliases) && (typeof v !== "string" || ids.has(v))) S.policy.aliases[k] = clone(v);
  toast(`已加入 ${add.length} 个档位，保存后生效`);
  changed({ all: true });
}
function listHtml() {
  const sh = shares(), ready = mapReady();
  const fresh = newDefaults();
  const vendors = [...new Set(profiles().map(vendorOf))];
  const row = (p) => {
    const spec = specialty(p), n = sh[p.id] || 0;
    const meta = [p.kind, p.model ? p.model.split("/").pop() : "默认模型", p.effort, spec && `仅 ${spec}`, `$${p.cost ?? 0}`, p.time != null && `${p.time}min`].filter(Boolean).join(" · ");
    return `<div class="prow ${p.id === S.sel ? "sel" : ""} ${p.enabled === false || S.hidden.has(p.id) ? "off" : ""}" data-pick="${esc(p.id)}">
      ${swatch(p)}<span class="name">${esc(p.label || p.id)}</span>
      <span class="wins ${n ? "" : "zero"}" title="在当前领地图上占的面积">${ready ? `${Math.round(n * 100)}%` : ""}</span>
      <input type="checkbox" data-enable="${esc(p.id)}" ${p.enabled !== false ? "checked" : ""} title="启用">
      <span class="meta">${esc(meta)}</span></div>`;
  };
  return `<div class="insp-head"><h3>档位 <span class="muted mono">${profiles().length}</span></h3><button data-act="new-profile">＋ 新建</button></div>
    ${fresh.length ? `<div class="banner"><div>内置默认值里有 ${fresh.length} 个档位不在你保存的配置里：<span class="mono">${esc(fresh.slice(0, 6).map((p) => p.label || p.id).join("、"))}${fresh.length > 6 ? " …" : ""}</span></div><button data-act="merge-defaults">加入</button></div>` : ""}
    <p class="hint">点图上的点或下面的行来编辑。百分比是它在当前领地图上占的面积，0% 说明被别的档位完全盖住。</p>
    ${vendors.map((v) => `<div class="group"><div class="group-h"><span class="swatch" style="background:${vendorColor(v)}"></span>${esc(vendorName(v))}</div>${profiles().filter((p) => vendorOf(p) === v).map(row).join("")}</div>`).join("")}`;
}
function editorHtml(p) {
  const i = profIndex(p.id), P = `profiles[${i}]`;
  const kinds = Object.keys(S.installed).length ? Object.keys(S.installed) : [p.kind];
  if (!kinds.includes(p.kind)) kinds.unshift(p.kind);
  const kindOpts = kinds.map((k) => `<option value="${esc(k)}" ${k === p.kind ? "selected" : ""}>${esc(k)}${S.installed[k] === false ? "（未安装）" : ""}</option>`).join("");
  const catalog = S.models[p.kind] || [];
  const entry = catalog.find((m) => m.id === p.model);
  const noModel = !MODEL_KINDS.includes(p.kind), noEffort = !EFFORT_KINDS.includes(p.kind);
  const effList = entry && entry.efforts.length ? entry.efforts : EFFORTS;
  const effSeg = ["", ...effList, ...(p.effort && !effList.includes(p.effort) ? [p.effort] : [])].map((e) =>
    `<button data-effort="${e}" class="${(p.effort || "") === e ? "on" : ""} ${e && !effList.includes(e) ? "warn" : ""}" ${e && !effList.includes(e) ? 'title="当前模型不支持这个等级"' : ""}>${e || "默认"}</button>`).join("");
  const me = modelEntry(p);
  const modelHint = noModel ? "router 还不会给这个 agent 传模型参数，这一项不生效"
    : me ? `${esc(me.label)} · ${esc(vendorName(me.vendor))}` : entry ? esc(entry.label) : p.model && catalog.length ? "不在已知列表里，会原样传给 CLI" : catalog.length ? `可选 ${catalog.length} 个，点输入框选择` : "";
  const effortHint = noEffort ? (p.kind === "opencode" ? "OpenCode 交互模式没有 effort 参数，用模型默认档" : "这个 agent 不支持 effort 参数") : "";
  const coords = dims("score").map((d) => {
    const v = getPoint(p, d), n = nLevels(d), on = v != null;
    return `<span class="lbl">${esc(d.label || d.id)}</span><div class="pt" data-path="${P}.point.${esc(d.id)}">
        <input type="range" min="0" max="${n - 1}" step="${SNAP}" value="${on ? v : (n - 1) / 2}" data-pt="${esc(d.id)}" ${on ? "" : "disabled"}>
        <span class="mono pt-v">${on ? fmt(v) : "—"}</span>
        <label class="check"><input type="checkbox" data-ptoff="${esc(d.id)}" ${on ? "" : "checked"}>不挑</label></div>
      <span class="desc">${on ? esc(shortLevel(d.levels[Math.round(v)])) : "这个维度上不罚分"}</span>`;
  }).join("");
  const affinity = dims("choice").map((d) => {
    const aff = (p.affinity && p.affinity[d.id]) || {};
    const def = aff["*"] ?? 1;
    const seg = (opt, cur, inherit) => `<span class="seg">${inherit ? `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="inherit" class="${cur === undefined ? "on" : ""}">默认</button>` : ""}${AFF_STEPS.map(([v, t]) => `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="${v}" class="${cur !== undefined && Number(cur) === Number(v) ? "on" : ""}">${t}</button>`).join("")}</span>`;
    return `<div class="stack" data-path="${P}.affinity.${esc(d.id)}"><div class="row spread"><span class="sect" style="margin:0">${esc(d.label || d.id)}</span><span class="hint">没单独设置的选项按 *</span></div>
      <div class="aff"><span>*</span>${seg("*", def, false)}${Object.keys(d.options).map((o) => `<span title="${esc(d.options[o])}">${esc(o)}</span>${seg(o, aff[o], true)}`).join("")}</div></div>`;
  }).join("");
  return `<button class="ghost back" data-act="back">← 全部档位</button>
    <div class="ed-title">${swatch(p)}<h3>${esc(p.label || p.id)}</h3><span class="mono">${esc(p.id)}</span></div>
    ${p.note ? `<p class="hint note">${esc(p.note)}</p>` : ""}
    <div class="grid2">
      ${me ? `<label>运行于</label><div>${harnessSeg(p)}<div class="hint">同一个模型换个 agent 来跑；模型 ID 会按目录自动换。</div></div>` : ""}
      <label>Agent</label><select data-f="kind" data-path="${P}.kind">${kindOpts}</select>
      <label>模型</label><div><input data-f="model" data-path="${P}.model" value="${esc(p.model || "")}" placeholder="（agent 默认）" list="dl-models" spellcheck="false" autocomplete="off" style="width:100%" class="mono">
        <datalist id="dl-models">${catalog.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("")}</datalist>
        ${modelHint ? `<div class="hint">${modelHint}</div>` : ""}</div>
      <label>Effort</label><div><span class="seg" data-path="${P}.effort">${effSeg}</span>${effortHint ? `<div class="hint">${effortHint}</div>` : ""}</div>
      <label>名称</label><input data-f="label" data-path="${P}.label" value="${esc(p.label || "")}">
      <label>ID</label><input data-f="id" data-path="${P}.id" value="${esc(p.id)}" spellcheck="false" class="mono">
      <label>成本</label><span class="row"><input type="number" step="0.01" min="0" data-f="cost" data-path="${P}.cost" value="${p.cost ?? 0}"><span class="hint">每任务美元 × ${S.policy.scoring?.cost_weight ?? 0.05}${(S.policy.scoring?.agent_cost || {})[p.kind] != null ? ` × ${esc(p.kind)} 系数 ${S.policy.scoring.agent_cost[p.kind]}` : ""}</span></span>
      <label>耗时</label><span class="row"><input type="number" step="0.1" min="0" data-f="time" data-path="${P}.time" value="${p.time ?? 0}"><span class="hint">每任务分钟 × ${S.policy.scoring?.time_weight ?? 0}</span></span>
      <label>状态</label><span class="row"><label class="check"><input type="checkbox" data-f="enabled" ${p.enabled !== false ? "checked" : ""}>启用</label><label class="check"><input type="checkbox" data-act="hide" ${S.hidden.has(p.id) ? "checked" : ""}>图上隐藏</label></span>
    </div>
    <hr class="sep"><p class="sect">坐标</p><div class="rng">${coords}</div>
    <p class="hint">任务离坐标半格以内不罚分；再远，比坐标高（能力不够）每级罚 ${S.policy.scoring?.under_penalty ?? 3}，比坐标低（大材小用）每级罚 ${S.policy.scoring?.over_penalty ?? 1}。也可以直接在图上拖点。</p>
    ${affinity ? `<hr class="sep"><p class="sect">类型亲和度</p>${affinity}` : ""}
    <hr class="sep"><p class="sect">Prompt 前缀</p><textarea rows="3" data-f="prompt_prefix" data-path="${P}.prompt_prefix" placeholder="派发时加在任务前面，可留空">${esc(p.prompt_prefix || "")}</textarea>
    <hr class="sep"><div class="row"><button data-act="dup-profile">复制档位</button><button class="danger" data-act="del-profile">删除档位</button></div>`;
}

function setProfileField(p, f, el) {
  if (f === "enabled") p.enabled = el.checked;
  else if (f === "cost") p.cost = el.value === "" ? 0 : Number(el.value);
  else if (f === "time") { if (el.value === "") delete p.time; else p.time = Number(el.value); }
  else if (["model", "prompt_prefix"].includes(f)) { const v = f === "model" ? el.value.trim() : el.value; if (v === "") delete p[f]; else p[f] = v; }
  else if (f !== "id") p[f] = el.value;
}
function renameProfile(p, newId) {
  newId = newId.trim();
  if (!newId || newId === p.id) return;
  if (profIndex(newId) >= 0) { toast(`ID「${newId}」已存在`); renderEditor(); return; }
  const old = p.id;
  p.id = newId;
  for (const [k, v] of Object.entries(S.policy.aliases || {})) if (v === old) S.policy.aliases[k] = newId;
  if (S.hidden.delete(old)) S.hidden.add(newId);
  S.sel = newId;
  toast(`已改名，并更新了指向它的别名`);
}

// ---------------------------------------------------------------- trial summary (chart tab)
function trialBoxHtml() {
  const t = S.trial; if (!t) return "";
  const a = t.assessment || {};
  const top = (t.ranking || []).filter((r) => r.available).slice(0, 5);
  const max = Math.max(0.01, ...top.map((r) => r.total));
  const bin = (S.policy.binaries || {})[t.kind] || t.kind;
  const choices = Object.entries(a.choices || {}).filter(([, v]) => v).map(([k, v]) => `${(dimById(k) || {}).label || k}=${v}`).join(" ");
  return `<div class="card trialbox"><div class="row spread"><span class="trial-task">${esc(S.trialTask)}</span>
      <span class="row"><span class="pill">${esc(a.source || "")}${a.confidence != null ? ` · ${a.confidence}` : ""}${a.bumped ? " · 难度上调" : ""}</span>${choices ? `<span class="pill">${esc(choices)}</span>` : ""}<button class="ghost" data-act="clear-trial">清除</button></span></div>
    ${t.profile_id ? `<div class="winner">${swatch(profById(t.profile_id) || { id: t.profile_id })}${esc(t.profile_label || t.profile_id)}<span class="mono muted" style="font-size:11px;font-weight:400">${esc(t.profile_id)}</span></div>
      <div class="cmdline">${esc([bin, ...(t.args || [])].join(" "))}</div>` : `<p class="danger">${esc(t.error || "没有可用档位")}</p>`}
    ${a.details && a.details.systemone_error ? `<p class="hint">决策模型不可用，已用关键词规则：${esc(a.details.systemone_error)}</p>` : ""}
    <div class="rk">${top.map((r) => { const win = r.profile_id === t.profile_id ? "win" : ""; const p = profById(r.profile_id);
      return `<span class="n ${win}">${p ? swatch(p) : ""}${esc(r.label)}</span><span class="track"><span class="fill ${win}" style="width:${Math.max(2, (r.total / max) * 100).toFixed(1)}%"></span></span><span class="v ${win}">${r.total}</span>`; }).join("")}</div>
    <div class="row spread" style="margin-top:8px"><span class="hint">罚分，越低越好</span><button class="ghost" data-tab-go="trial">各维度概率与完整排名 →</button></div></div>`;
}

// ---------------------------------------------------------------- dimensions tab
function dimsHtml() {
  const cards = S.policy.dimensions.map((d, i) => {
    const P = `dimensions[${i}]`;
    const body = d.type === "score"
      ? `<label>刻度</label><div><textarea rows="${Math.max(3, nLevels(d))}" data-dimlist="${i}" data-path="${P}.levels" spellcheck="false">${esc(d.levels.join("\n"))}</textarea><div class="hint">每行一级，第一行为 0。冒号前的部分会作为图上的短标签。</div></div>`
      : `<label>选项</label><div><textarea rows="${Math.max(3, Object.keys(d.options).length)}" data-dimlist="${i}" data-path="${P}.options" spellcheck="false">${esc(Object.entries(d.options).map(([k, v]) => `${k}: ${v}`).join("\n"))}</textarea><div class="hint">每行「key: 描述」，key 用小写字母、数字、- 或 _。</div></div>`;
    return `<div class="card dimcard" id="dim-${i}">
      <div class="row" style="justify-content:space-between"><h3>${esc(d.label || d.id)} <span class="muted">· ${d.type === "score" ? "评分" : "选择"}</span></h3>
        <span class="row"><button class="ghost" data-dim-move="${i}" data-dir="-1" ${i === 0 ? "disabled" : ""}>↑</button><button class="ghost" data-dim-move="${i}" data-dir="1" ${i === S.policy.dimensions.length - 1 ? "disabled" : ""}>↓</button><button class="danger" data-dim-del="${i}">删除</button></span></div>
      <div class="grid2">
        <label>ID</label><input data-dim="${i}" data-df="id" data-path="${P}.id" value="${esc(d.id)}" spellcheck="false">
        <label>名称</label><input data-dim="${i}" data-df="label" data-path="${P}.label" value="${esc(d.label || "")}">
        <label>类型</label><select data-dim="${i}" data-df="type" data-path="${P}.type"><option value="score" ${d.type === "score" ? "selected" : ""}>评分（有序刻度）</option><option value="choice" ${d.type === "choice" ? "selected" : ""}>选择（分类）</option></select>
        <label>权重</label><span class="row"><input type="number" step="0.1" min="0" data-dim="${i}" data-df="weight" data-path="${P}.weight" value="${d.weight ?? 1}"><span class="hint">这个维度的罚分乘以权重</span></span>
        <label>问题</label><textarea rows="2" data-dim="${i}" data-df="instructions" data-path="${P}.instructions">${esc(d.instructions || "")}</textarea>
        ${body}
      </div></div>`;
  }).join("");
  return `<p class="hint">每个维度就是问决策模型（Jev）的一个问题，一次请求同时问完。评分维度在坐标图上可作为横轴或纵轴，档位为它设置区间；选择维度通过「类型亲和度」影响打分。</p>${cards}
    <div class="row" style="margin-top:12px"><button data-act="new-dim-score">＋ 新增评分维度</button><button data-act="new-dim-choice">＋ 新增选择维度</button></div>`;
}
function renameDim(d, newId) {
  newId = newId.trim();
  if (!newId || newId === d.id) return;
  if (dimById(newId)) { toast(`维度「${newId}」已存在`); render(); return; }
  const old = d.id; d.id = newId;
  for (const p of profiles()) {
    if (p.point && old in p.point) { p.point[newId] = p.point[old]; delete p.point[old]; }
    if (p.affinity && old in p.affinity) { p.affinity[newId] = p.affinity[old]; delete p.affinity[old]; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && old in v) { v[newId] = v[old]; delete v[old]; }
  if (S.axes.x === old) S.axes.x = newId;
  if (S.axes.y === old) S.axes.y = newId;
}
function dropDimRefs(id) {
  for (const p of profiles()) {
    if (p.point) { delete p.point[id]; if (!Object.keys(p.point).length) delete p.point; }
    if (p.affinity) delete p.affinity[id];
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object") delete v[id];
}
function fixAxes() {
  const sd = dims("score").map((d) => d.id);
  if (!sd.includes(S.axes.x)) S.axes.x = sd[0];
  if (!sd.includes(S.axes.y)) S.axes.y = sd.find((x) => x !== S.axes.x) || sd[0];
}
function applyLevels(d, text) {
  const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
  if (lines.length < 2) { toast("评分维度至少需要 2 级"); return false; }
  d.levels = lines;
  const hi = lines.length - 1; let n = 0;
  for (const p of profiles()) {
    if (getPoint(p, d) > hi) { setPoint(p, d, hi); n++; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && v[d.id] > hi) v[d.id] = hi;
  if (n) toast(`刻度变少了，已把 ${n} 个档位的坐标移到最高一级`);
  return true;
}
function applyOptions(d, text) {
  const opts = {};
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*([a-z][a-z0-9_-]*)\s*[:：]\s*(.+?)\s*$/);
    if (m) opts[m[1]] = m[2];
  }
  if (Object.keys(opts).length < 2) { toast("选择维度至少需要 2 个「key: 描述」选项"); return false; }
  d.options = opts;
  let n = 0;
  for (const p of profiles()) {
    const a = p.affinity && p.affinity[d.id];
    if (a) for (const k of Object.keys(a)) if (k !== "*" && !(k in opts)) { delete a[k]; n++; }
  }
  if (n) toast(`已移除 ${n} 条指向被删选项的亲和度设置`);
  return true;
}

// ---------------------------------------------------------------- trial tab
function trialHtml() {
  const t = S.trial;
  let result = "";
  if (t) {
    const a = t.assessment || {};
    const dimBlocks = dims().map((d) => {
      const dist = (a.dists || {})[d.id];
      if (!dist) return `<div><b>${esc(d.label || d.id)}</b> <span class="muted">— 无数据</span></div>`;
      const rows = d.type === "score" ? d.levels.map((lv, k) => [`${k} · ${shortLevel(lv)}`, dist[k] || 0, lv]) : Object.keys(d.options).map((o) => [o, dist[o] || 0, d.options[o]]);
      const exp = d.type === "score" && a.expected ? ` <span class="muted">期望 ${a.expected[d.id]}</span>` : "";
      return `<div><b>${esc(d.label || d.id)}</b>${exp}<div class="bars">${rows.map(([l, p, full]) => `<span class="lbl" title="${esc(full)}">${esc(l)}</span><span><span class="bar" style="width:${(p * 100).toFixed(1)}%;display:block"></span></span><span class="muted">${(p * 100).toFixed(0)}%</span>`).join("")}</div></div>`;
    }).join("");
    const rank = (t.ranking || []).map((r, k) => `<tr class="${r.profile_id === t.profile_id ? "win" : ""} ${r.available ? "" : "na"}"><td>${k + 1}</td>
      <td><span class="swatch" style="background:${color(r.profile_id)}"></span> ${esc(r.label)}<div class="bk">${esc(r.profile_id)} · ${esc(r.kind)}</div></td>
      <td><b>${r.total}</b></td><td class="bk">${Object.entries(r.breakdown).filter(([, v]) => v).map(([k2, v]) => `${esc((dimById(k2) || {}).label || ({ cost: "成本", time: "耗时" }[k2] || k2))} ${v}`).join(" · ") || "0"}${r.available ? "" : `<br>✗ ${esc(r.why_unavailable)}`}</td></tr>`).join("");
    const err = a.details && a.details.systemone_error ? `<p class="hint">决策模型不可用，已用关键词规则：${esc(a.details.systemone_error)}</p>` : "";
    result = `<div class="split even" style="margin-top:12px"><div class="card stack"><h3>维度判断 <span class="muted">${esc(a.source)} · 置信度 ${a.confidence}${a.bumped ? " · 低置信度→难度上调" : ""}</span></h3>${err}${dimBlocks}</div>
      <div class="card"><h3>档位排名 ${t.profile_id ? `→ ${esc(t.profile_label || t.profile_id)}` : ""}</h3>${t.error ? `<p class="danger">${esc(t.error)}</p>` : ""}
      ${t.args ? `<p class="hint">启动参数：--kind ${esc(t.kind)}${t.args.length ? " -- " + esc(t.args.join(" ")) : ""}</p>` : ""}
      <div class="table-scroll"><table><thead><tr><th>#</th><th>档位</th><th>罚分</th><th>明细</th></tr></thead><tbody>${rank}</tbody></table></div></div></div>`;
  }
  return `<div class="card"><h3>试运行</h3><p class="hint">用编辑器里当前的配置（包括未保存的修改）判断一个任务，看各维度的概率和所有档位的排名。不会派发 agent。</p>
    <textarea id="trial-task" rows="3" placeholder="输入一个任务描述，例如：修复登录偶现的崩溃">${esc(S.trialTask)}</textarea>
    <div class="row"><button class="primary" data-act="run-trial" ${S.trialBusy ? "disabled" : ""}>${S.trialBusy ? "判断中…" : "试运行"}</button>
      <label><input type="checkbox" data-act="toggle-backend" ${S.useBackend ? "checked" : ""}> 使用决策模型（Jev）</label></div>
    <div class="chips">${SAMPLE_TASKS.map((x) => `<span class="chip" data-sample="${esc(x)}">${esc(x)}</span>`).join("")}</div></div>${result}`;
}
async function runTrial() {
  const task = ($("#cmd") || $("#trial-task") || {}).value?.trim() || S.trialTask;
  if (!task) { toast("先输入任务"); return; }
  S.trialTask = task; S.trialBusy = true; render();
  try {
    const { status, body } = await api("/api/route", { method: "POST", body: JSON.stringify({ task, policy: S.policy, use_backend: S.useBackend }) });
    if (status === 422) { S.errors = body.errors || []; toast("配置有错误，先修正再试运行"); }
    else if (status !== 200) toast(body.error || `请求失败 ${status}`);
    else {
      S.trial = body.decision || { assessment: body.assessment, error: body.error, ranking: [] }; S.errors = [];
      // show the territory for the task's own type, so the map agrees with the ranking
      const ch = (S.trial.assessment || {}).choices || {};
      for (const d of dims("choice")) if (ch[d.id]) S.choice[d.id] = ch[d.id]; else delete S.choice[d.id];
    }
  } finally { S.trialBusy = false; render(); }
}

// ---------------------------------------------------------------- settings + json
function settingsHtml() {
  const sc = S.policy.scoring || {}, be = S.policy.backend || {}, dp = S.policy.dispatch || {};
  const num = (path, v, step, hint) => `<span class="row"><input type="number" step="${step}" min="0" data-set="${path}" data-path="${path}" value="${v ?? ""}">${hint ? `<span class="hint">${hint}</span>` : ""}</span>`;
  const aliases = Object.entries(S.policy.aliases || {});
  const atRows = aliases.filter(([k]) => k.startsWith("@")).map(([k, v]) => `<tr data-path="aliases.${esc(k)}"><td><input value="${esc(k)}" data-alias-key="${esc(k)}" spellcheck="false"></td><td><select data-alias-val="${esc(k)}">${profiles().map((p) => `<option value="${esc(p.id)}" ${p.id === v ? "selected" : ""}>${esc(p.label || p.id)}</option>`).join("")}</select></td><td><button class="ghost danger" data-alias-del="${esc(k)}">删除</button></td></tr>`).join("");
  const bang = Object.fromEntries(aliases.filter(([k]) => k.startsWith("!")));
  return `<div class="split even"><div class="card"><h3>打分</h3><div class="grid2">
      <label>超出上限罚分</label>${num("scoring.under_penalty", sc.under_penalty, 0.1, "任务比档位难（能力不够）")}
      <label>低于下限罚分</label>${num("scoring.over_penalty", sc.over_penalty, 0.1, "任务比档位简单（大材小用）")}
      <label>成本权重</label>${num("scoring.cost_weight", sc.cost_weight, 0.01, "罚分 += 每任务美元 × 权重")}
      ${[...new Set(profiles().map((p) => p.kind))].map((k) => `<label>成本系数 · ${esc(k)}</label>${num(`scoring.agent_cost.${k}`, (sc.agent_cost || {})[k] ?? 1, 0.05, "这个 agent 上 1 美元实际算多少（包月可以调低）")}`).join("")}
      <label>耗时权重</label>${num("scoring.time_weight", sc.time_weight, 0.01, "罚分 += 每任务分钟 × 权重")}
      <label>容差</label>${num("scoring.tolerance", sc.tolerance, 0.05, "离坐标这么多级以内不罚分")}
      <label>规则置信度下限</label>${num("scoring.min_confidence", sc.min_confidence, 0.05, "仅关键词规则：低于此值难度上调一级")}
    </div>
    <hr class="sep"><h3>决策模型</h3><div class="grid2">
      <label>启用</label><input type="checkbox" data-set="backend.enabled" ${be.enabled !== false ? "checked" : ""}>
      <label>地址</label><input data-set="backend.base_url" data-path="backend.base_url" value="${esc(be.base_url || "")}" spellcheck="false">
      <label>模型</label><input data-set="backend.model" value="${esc(be.model || "")}" spellcheck="false">
      <label>密钥环境变量</label><input data-set="backend.api_key_env" value="${esc(be.api_key_env || "")}" spellcheck="false">
      <label>超时（秒）</label>${num("backend.timeout_s", be.timeout_s, 0.5)}
      <label>附带仓库规模</label><input type="checkbox" data-set="backend.include_repo_signals" ${be.include_repo_signals ? "checked" : ""}>
    </div>
    <hr class="sep"><h3>派发</h3><div class="grid2">
      <label>分屏方向</label><select data-set="dispatch.direction">${["auto", "right", "down"].map((d) => `<option ${dp.direction === d ? "selected" : ""}>${d}</option>`).join("")}</select>
      <label>启动超时（ms）</label>${num("dispatch.agent_start_timeout_ms", dp.agent_start_timeout_ms, 1000)}
    </div></div>
    <div class="card"><h3>@ 别名</h3><p class="hint">在任务里写 <code>@名字</code> 直接指定档位，也可以直接写 <code>@档位ID</code>。</p>
      <div class="table-scroll"><table><tbody>${atRows}</tbody></table></div><button class="ghost" data-act="new-alias">＋ 新增别名</button>
      <hr class="sep"><h3>! 维度指令</h3><p class="hint">在任务里写 <code>!hard</code> 这类指令，把维度固定到某个值；也可以直接写 <code>!维度ID=值</code>。</p>
      <textarea id="bang-json" rows="8" data-path="aliases" spellcheck="false">${esc(JSON.stringify(bang, null, 2))}</textarea>
      <button class="ghost" data-act="apply-bang">应用</button></div></div>`;
}
function jsonHtml() {
  return `<div class="card"><h3>完整配置（JSON）</h3><p class="hint">高级编辑：binaries、env.extra_path 等不在表单里的项在这里改。点「应用」后进入编辑器，仍需保存。</p>
    <textarea id="json-text" rows="30" spellcheck="false">${esc(JSON.stringify(S.policy, null, 2))}</textarea>
    <div class="row"><button data-act="apply-json">应用</button></div></div>`;
}
function setPath(obj, path, value) {
  const keys = path.split("."); let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] = o[k] || {};
  o[keys[keys.length - 1]] = value;
}

// ---------------------------------------------------------------- save / revert
async function save() {
  const { status, body } = await api("/api/policy", { method: "PUT", body: JSON.stringify(S.policy) });
  if (status === 200) { S.base = JSON.stringify(S.policy); S.source = "user"; S.path = body.path; S.errors = []; toast("已保存，立即生效"); }
  else if (status === 422) { S.errors = body.errors || []; toast(`有 ${S.errors.length} 处错误，未保存`); }
  else toast(body.error || `保存失败 ${status}`);
  render();
}


// ---------------------------------------------------------------- events
document.addEventListener("click", async (e) => {
  const t = e.target;
  const tab = t.closest("#tabs button, [data-tab-go]");
  if (tab) { S.tab = tab.dataset.tab || tab.dataset.tabGo; render(); window.scrollTo(0, 0); return; }
  if (t.id === "btn-save") return save();
  if (t.id === "btn-revert") { if (!isDirty() || confirm("放弃所有未保存的修改？")) await load(); return; }
  if (t.id === "btn-defaults") {
    const { body } = await api("/api/policy/default");
    S.policy = body.policy; fixAxes(); S.sel = null; toast("已载入内置默认值，保存后生效"); render(); return;
  }
  const err = t.closest("[data-err]");
  if (err) {
    const path = S.errors[+err.dataset.err].path;
    let m;
    if ((m = path.match(/^profiles\[(\d+)\]/))) { S.tab = "chart"; S.sel = (profiles()[+m[1]] || {}).id || null; }
    else if ((m = path.match(/^dimensions\[(\d+)\]/))) { S.tab = "dims"; render(); $(`#dim-${m[1]}`)?.scrollIntoView({ block: "start" }); return; }
    else if (/^(aliases|scoring|backend|dispatch)/.test(path)) S.tab = "settings";
    render(); return;
  }
  const pick = t.closest("[data-pick]");
  if (pick && !t.matches("input")) { S.sel = pick.dataset.pick; renderChart(); renderEditor(); return; }
  const sample = t.closest("[data-sample]");
  if (sample) { S.trialTask = sample.dataset.sample; const ta = $("#trial-task"); if (ta) ta.value = S.trialTask; return runTrial(); }
  const hb = t.closest("[data-harness]");
  if (hb) {
    const p = profById(hb.dataset.id || S.sel); if (!p) return;
    switchHarness(p, hb.dataset.harness); changed({ chart: true, editor: true }); return;
  }
  const eff = t.closest("[data-effort]");
  if (eff) {
    const p = selProfile(); if (!p) return;
    if (eff.dataset.effort) p.effort = eff.dataset.effort; else delete p.effort;
    changed({ editor: true }); return;
  }
  const aff = t.closest("[data-aff]");
  if (aff) {
    const p = selProfile(); if (!p) return;
    const { aff: did, opt, val } = aff.dataset;
    p.affinity = p.affinity || {}; const m = (p.affinity[did] = p.affinity[did] || {});
    if (val === "inherit" || (opt === "*" && val === "1")) delete m[opt]; else m[opt] = Number(val);
    if (!Object.keys(m).length) delete p.affinity[did];
    if (!Object.keys(p.affinity).length) delete p.affinity;
    changed({ editor: true, chart: true }); return;
  }
  const mv = t.closest("[data-dim-move]");
  if (mv) {
    const i = +mv.dataset.dimMove, j = i + +mv.dataset.dir, D = S.policy.dimensions;
    [D[i], D[j]] = [D[j], D[i]]; changed({ all: true }); return;
  }
  const del = t.closest("[data-dim-del]");
  if (del) {
    const i = +del.dataset.dimDel, d = S.policy.dimensions[i];
    if (S.policy.dimensions.length <= 1) { toast("至少保留一个维度"); return; }
    if (d.type === "score" && dims("score").length <= 1) { toast("至少保留一个评分维度"); return; }
    if (!confirm(`删除维度「${d.label || d.id}」？档位里对应的区间和亲和度也会一起删除。`)) return;
    S.policy.dimensions.splice(i, 1); dropDimRefs(d.id); delete S.choice[d.id]; fixAxes(); changed({ all: true }); return;
  }
  const adel = t.closest("[data-alias-del]");
  if (adel) { delete S.policy.aliases[adel.dataset.aliasDel]; changed({ all: true }); return; }

  const act = t.closest("[data-act]")?.dataset.act;
  if (!act || t.matches("input")) return;
  const p = selProfile();
  if (act === "new-profile") {
    const id = uniqueId("new-profile", profiles().map((q) => q.id));
    const point = {};
    for (const d of [dimById(S.axes.x), dimById(S.axes.y)]) point[d.id] = (nLevels(d) - 1) / 2;
    profiles().push({ id, label: "新档位", kind: "claude", cost: 1, enabled: true, point });
    S.sel = id; changed({ all: true });
  } else if (act === "dup-profile" && p) {
    const c = clone(p); c.id = uniqueId(`${p.id}-copy`, profiles().map((q) => q.id)); c.label = `${p.label || p.id} 副本`;
    profiles().splice(profIndex(p.id) + 1, 0, c); S.sel = c.id; changed({ all: true });
  } else if (act === "del-profile" && p) {
    const refs = Object.entries(S.policy.aliases || {}).filter(([, v]) => v === p.id).map(([k]) => k);
    if (!confirm(`删除档位「${p.label || p.id}」？${refs.length ? `\n别名 ${refs.join(", ")} 也会一起删除。` : ""}`)) return;
    profiles().splice(profIndex(p.id), 1); refs.forEach((k) => delete S.policy.aliases[k]);
    S.sel = null; changed({ all: true });
  } else if (act === "merge-defaults") mergeDefaults();
  else if (act === "back") { S.sel = null; renderChart(); renderEditor(); }
  else if (act === "new-dim-score" || act === "new-dim-choice") {
    const id = uniqueId("dim", S.policy.dimensions.map((d) => d.id));
    S.policy.dimensions.push(act === "new-dim-score"
      ? { id, label: "新维度", type: "score", weight: 1, instructions: "How … is this task?", levels: ["Low", "Medium", "High"] }
      : { id, label: "新维度", type: "choice", weight: 1, instructions: "Which … fits this task?", options: { a: "Option A", b: "Option B" } });
    changed({ all: true }); setTimeout(() => $(`#dim-${S.policy.dimensions.length - 1}`)?.scrollIntoView(), 0);
  } else if (act === "run-trial") runTrial();
  else if (act === "clear-trial") { S.trial = null; render(); }
  else if (act === "new-alias") {
    const k = uniqueId("@alias", Object.keys(S.policy.aliases || {}));
    S.policy.aliases = S.policy.aliases || {}; S.policy.aliases[k] = profiles()[0].id; changed({ all: true });
  } else if (act === "apply-bang") {
    try {
      const v = JSON.parse($("#bang-json").value);
      for (const k of Object.keys(S.policy.aliases || {})) if (k.startsWith("!")) delete S.policy.aliases[k];
      for (const [k, val] of Object.entries(v)) S.policy.aliases[k.startsWith("!") ? k : `!${k}`] = val;
      toast("已应用"); changed({ all: true });
    } catch (ex) { toast(`JSON 格式错误：${ex.message}`); }
  } else if (act === "apply-json") {
    try { S.policy = JSON.parse($("#json-text").value); fixAxes(); if (S.sel && profIndex(S.sel) < 0) S.sel = null; toast("已应用到编辑器，记得保存"); changed({ all: true }); }
    catch (ex) { toast(`JSON 格式错误：${ex.message}`); }
  }
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.f && !["id", "kind", "enabled"].includes(t.dataset.f)) {
    const p = selProfile(); if (!p) return;
    setProfileField(p, t.dataset.f, t);
    changed({ chart: t.dataset.f === "label" });
  } else if (t.dataset.dim && ["label", "weight", "instructions"].includes(t.dataset.df)) {
    const d = S.policy.dimensions[+t.dataset.dim];
    d[t.dataset.df] = t.dataset.df === "weight" ? Number(t.value) : t.value;
    changed();
  } else if (t.dataset.set && t.type !== "checkbox" && t.tagName !== "SELECT") {
    setPath(S.policy, t.dataset.set, t.type === "number" ? (t.value === "" ? undefined : Number(t.value)) : t.value);
    changed();
  } else if (t.dataset.pt) {
    const p = selProfile(), d = dimById(t.dataset.pt); if (!p || !d) return;
    setPoint(p, d, Number(t.value));
    const row = t.closest(".pt"); if (row) row.querySelector(".pt-v").textContent = fmt(Number(t.value));
    changed({ chart: true });
  } else if (t.id === "trial-task" || t.id === "cmd") S.trialTask = t.value;
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.axis) { S.axes[t.dataset.axis] = t.value; render(); return; }
  if (t.dataset.choice !== undefined) { if (t.value) S.choice[t.dataset.choice] = t.value; else delete S.choice[t.dataset.choice]; refreshMap(0); return; }
  if (t.dataset.act === "toggle-log") { S.showLog = t.checked; renderChart(); return; }
  if (t.dataset.act === "toggle-backend") { S.useBackend = t.checked; return; }
  if (t.dataset.act === "hide") { const p = selProfile(); if (p) { t.checked ? S.hidden.add(p.id) : S.hidden.delete(p.id); renderChart(); } return; }
  if (t.dataset.enable) { const q = profById(t.dataset.enable); if (q) { q.enabled = t.checked; changed({ chart: true, list: true }); } return; }
  const p = selProfile();
  if (t.dataset.f === "id" && p) { renameProfile(p, t.value); changed({ all: true }); return; }
  if (t.dataset.f === "enabled" && p) { setProfileField(p, "enabled", t); changed({ chart: true }); return; }
  if (t.dataset.f === "kind" && p) { setProfileField(p, "kind", t); changed({ chart: true, editor: true }); return; }
  if (t.dataset.f === "model" && p) { renderEditor(); return; }
  if (t.dataset.ptoff && p) { const d = dimById(t.dataset.ptoff); setPoint(p, d, t.checked ? null : (nLevels(d) - 1) / 2); changed({ chart: true, editor: true }); return; }
  if (t.dataset.pt && p) { renderEditor(); return; } // refresh the level name under the slider // refresh effort choices for the new model
  if (t.dataset.dim && t.dataset.df === "id") { renameDim(S.policy.dimensions[+t.dataset.dim], t.value); changed({ all: true }); return; }
  if (t.dataset.dim && t.dataset.df === "type") {
    const d = S.policy.dimensions[+t.dataset.dim];
    if (t.value === d.type) return;
    if (t.value === "score" ? false : dims("score").length <= 1) { toast("至少保留一个评分维度"); render(); return; }
    dropDimRefs(d.id); delete S.choice[d.id];
    if (t.value === "score") { delete d.options; d.levels = ["Low", "Medium", "High"]; }
    else { delete d.levels; d.options = { a: "Option A", b: "Option B" }; }
    d.type = t.value; fixAxes(); changed({ all: true }); return;
  }
  if (t.dataset.dimlist) {
    const d = S.policy.dimensions[+t.dataset.dimlist];
    if (d.type === "score" ? applyLevels(d, t.value) : applyOptions(d, t.value)) { if (d.type === "choice" && !(S.choice[d.id] in d.options)) delete S.choice[d.id]; changed({ all: true }); }
    else render();
    return;
  }
  if (t.dataset.set && (t.type === "checkbox" || t.tagName === "SELECT")) {
    setPath(S.policy, t.dataset.set, t.type === "checkbox" ? t.checked : t.value); changed(); return;
  }
  if (t.dataset.aliasKey !== undefined) {
    const old = t.dataset.aliasKey; let k = t.value.trim();
    if (!k.startsWith("@")) k = "@" + k;
    if (k !== old && !(k in S.policy.aliases)) { S.policy.aliases[k] = S.policy.aliases[old]; delete S.policy.aliases[old]; }
    changed({ all: true }); return;
  }
  if (t.dataset.aliasVal !== undefined) { S.policy.aliases[t.dataset.aliasVal] = t.value; changed(); }
});

let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => S.tab === "chart" && !drag && renderChart(), 120); });
document.addEventListener("pointerdown", onChartDown);
document.addEventListener("pointermove", onChartMove);
document.addEventListener("pointerup", onChartUp);
document.addEventListener("pointercancel", onChartUp);
document.addEventListener("pointerout", (e) => { if (!drag && e.target.closest && e.target.closest("#chart svg") && !e.relatedTarget?.closest?.("#chart svg")) readout(null); });
window.addEventListener("beforeunload", (e) => { if (S.policy && isDirty()) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); return; }
  if (e.key === "Enter" && e.target.id === "cmd" && !e.isComposing) { e.preventDefault(); runTrial(); return; }
  if (e.key === "Escape" && S.tab === "chart" && S.sel && !e.target.matches("input, textarea, select")) { S.sel = null; renderChart(); renderEditor(); }
});

load().catch((e) => { if (e.message !== "unauthorized") $("#main").innerHTML = `<p class="pad">加载失败：${esc(e.message)}</p>`; });
