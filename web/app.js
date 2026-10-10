"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
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
  policy: null, base: "", source: "", path: "", installed: {},
  tab: "chart", sel: null, axes: { x: null, y: null },
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
const selProfile = () => profiles().find((p) => p.id === S.sel) || null;
const getRange = (p, d) => (p.ranges && p.ranges[d.id]) || [0, nLevels(d) - 1];
function setRange(p, d, r) { p.ranges = p.ranges || {}; p.ranges[d.id] = r; }
const shortLevel = (t) => { const s = String(t).split(":")[0].trim(); return s.length > 18 ? s.slice(0, 17) + "…" : s; };
const hue = (id) => { const i = Math.max(0, profIndex(id)); return Math.round((i * 137.508 + 210) % 360); };
const color = (id, a = 1) => `hsl(${hue(id)} 68% 52% / ${a})`;
function specialty(p) {
  // "only review" style profiles: affinity default 0 on a choice dimension
  for (const [did, aff] of Object.entries(p.affinity || {})) {
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
}

// ---------------------------------------------------------------- top-level render
function render() {
  if (!S.policy) return;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  renderStatus();
  renderErrors();
  const main = $("#main");
  if (S.tab === "chart") main.innerHTML = `<div class="split"><div class="card">${chartControls()}<div id="chart"></div>${chipsHtml()}</div><div class="card" id="editor"></div></div>`;
  if (S.tab === "dims") main.innerHTML = dimsHtml();
  if (S.tab === "trial") main.innerHTML = trialHtml();
  if (S.tab === "settings") main.innerHTML = settingsHtml();
  if (S.tab === "json") main.innerHTML = jsonHtml();
  if (S.tab === "chart") { renderChart(); renderEditor(); }
  markErrors();
}
function renderStatus() {
  $("#dirty").hidden = !isDirty();
  $("#source").textContent = S.source === "user" ? `已保存的配置 · ${S.path}` : "使用内置默认值（保存后写入你的配置目录）";
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
  if (opts.chart && S.tab === "chart") renderChart();
  if (opts.editor && S.tab === "chart") renderEditor();
  if (opts.all) render();
}

// ---------------------------------------------------------------- chart
const G = { W: 660, H: 476, L: 120, R: 14, T: 30, B: 58 };
function fitText(text, px) {
  // ~11px per CJK character, ~6.5px otherwise, at the 11px label size
  let w = 0, out = "";
  for (const ch of text) {
    w += /[\u2e80-\uffff]/.test(ch) ? 11 : 6.5;
    if (w > px) return out.length ? out.slice(0, -1) + "…" : "";
    out += ch;
  }
  return out;
}
function fitGeometry() {
  // Draw at the container's real pixel width so labels keep their font size on phones.
  const host = $("#chart");
  const w = Math.round(Math.max(340, Math.min(780, (host && host.clientWidth) || 660)));
  G.W = w; G.H = Math.round(Math.max(380, w * 0.72)); G.L = w < 520 ? 96 : 120;
}
function geom() {
  const xd = dimById(S.axes.x), yd = dimById(S.axes.y);
  const nx = nLevels(xd), ny = nLevels(yd);
  return { xd, yd, nx, ny, cw: (G.W - G.L - G.R) / nx, ch: (G.H - G.T - G.B) / ny };
}
function chartControls() {
  const opts = (sel) => dims("score").map((d) => `<option value="${esc(d.id)}" ${d.id === sel ? "selected" : ""}>${esc(d.label || d.id)}</option>`).join("");
  return `<div class="row"><label>横轴 <select data-axis="x">${opts(S.axes.x)}</select></label>
    <label>纵轴 <select data-axis="y">${opts(S.axes.y)}</select></label>
    <label><input type="checkbox" data-act="toggle-log" ${S.showLog ? "checked" : ""}> 历史任务 (${S.log.length})</label>
    ${S.trial ? `<span class="hint">试运行：${esc(S.trialTask.slice(0, 24))} → <b>${esc(S.trial.profile_id || "无")}</b> <button class="ghost" data-act="clear-trial">清除</button></span>` : ""}</div>
    <p class="hint">拖动矩形移动档位，拖边或角调整区间。其他维度的区间在档位面板里编辑。</p>`;
}
function chipsHtml() {
  return `<div class="chips">${profiles().map((p) => `<span class="chip ${p.id === S.sel ? "sel" : ""} ${p.enabled === false || S.hidden.has(p.id) ? "off" : ""}" data-pick="${esc(p.id)}"><span class="swatch" style="background:${color(p.id)}"></span>${esc(p.label || p.id)}</span>`).join("")}
    <button class="ghost" data-act="new-profile">＋ 新建档位</button></div>`;
}
function renderChart() {
  const host = $("#chart"); if (!host) return;
  fitGeometry();
  const { xd, yd, nx, ny, cw, ch } = geom();
  const X = (i) => G.L + i * cw, Y = (j) => G.T + (ny - j) * ch; // left edge of cell i, top edge of cell j-1
  let s = `<svg class="chart" viewBox="0 0 ${G.W} ${G.H}" role="img" aria-label="档位坐标图"><rect class="bg" x="${G.L}" y="${G.T}" width="${nx * cw}" height="${ny * ch}" fill="transparent"/>`;
  for (let i = 0; i <= nx; i++) s += `<line class="gridline" x1="${X(i)}" x2="${X(i)}" y1="${G.T}" y2="${G.T + ny * ch}"/>`;
  for (let j = 0; j <= ny; j++) s += `<line class="gridline" x1="${G.L}" x2="${G.L + nx * cw}" y1="${Y(j)}" y2="${Y(j)}"/>`;
  xd.levels.forEach((t, i) => (s += `<text class="axis-label" x="${X(i) + cw / 2}" y="${G.T + ny * ch + 16}" text-anchor="middle">${i}<title>${esc(t)}</title></text><text class="axis-label" x="${X(i) + cw / 2}" y="${G.T + ny * ch + 30}" text-anchor="middle">${esc(shortLevel(t).slice(0, Math.max(4, Math.floor(cw / 7))))}</text>`));
  yd.levels.forEach((t, j) => (s += `<text class="axis-label" x="${G.L - 8}" y="${Y(j + 1) + ch / 2 + 4}" text-anchor="end">${esc(shortLevel(t).slice(0, 14))} ${j}<title>${esc(t)}</title></text>`));
  s += `<text class="axis-title" x="${G.L + (nx * cw) / 2}" y="${G.H - 6}" text-anchor="middle">${esc(xd.label || xd.id)} →</text>`;
  s += `<text class="axis-title" x="${G.L - 8}" y="${G.T - 12}" text-anchor="end">${esc(yd.label || yd.id)} ↑</text>`;

  // trial heat (product of the two marginals)
  const ta = S.trial && (S.trial.assessment || {});
  if (ta && ta.dists && ta.dists[xd.id] && ta.dists[yd.id]) {
    const dx = ta.dists[xd.id], dy = ta.dists[yd.id];
    let maxp = 0; const cells = [];
    for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) { const p = (dx[i] || 0) * (dy[j] || 0); cells.push([i, j, p]); maxp = Math.max(maxp, p); }
    for (const [i, j, p] of cells) if (p > 0.005) s += `<rect x="${X(i)}" y="${Y(j + 1)}" width="${cw}" height="${ch}" fill="var(--accent)" opacity="${(0.06 + 0.4 * p / maxp).toFixed(3)}" pointer-events="none"/>`;
  }

  // profiles, grouped by identical footprint so stacked ones stay visible
  const visible = profiles().filter((p) => !S.hidden.has(p.id));
  const groups = new Map();
  for (const p of visible) {
    const k = `${getRange(p, xd)}|${getRange(p, yd)}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(p);
  }
  const order = [...visible].sort((a, b) => (a.id === S.sel) - (b.id === S.sel));
  const insetOf = new Map();
  for (const g of groups.values()) g.forEach((p, k) => insetOf.set(p.id, 3 + k * 5));
  for (const p of order) {
    const [x0, x1] = getRange(p, xd), [y0, y1] = getRange(p, yd);
    const ins = insetOf.get(p.id);
    const rx = X(x0) + ins, ry = Y(y1 + 1) + ins, rw = (x1 - x0 + 1) * cw - 2 * ins, rh = (y1 - y0 + 1) * ch - 2 * ins;
    const sel = p.id === S.sel, off = p.enabled === false, spec = specialty(p);
    const fillA = sel ? 0.22 : spec ? 0.03 : 0.1;
    const dash = off ? 'stroke-dasharray="5 4"' : spec && !sel ? 'stroke-dasharray="1 4" stroke-linecap="round"' : "";
    s += `<g class="prof ${sel ? "sel" : ""}"><rect class="body" data-drag="move" data-id="${esc(p.id)}" x="${rx}" y="${ry}" width="${Math.max(4, rw)}" height="${Math.max(4, rh)}" rx="6" fill="${color(p.id, fillA)}" stroke="${color(p.id)}" stroke-width="${sel ? 2.5 : spec ? 1.6 : 1.2}" ${dash}><title>${esc(p.label || p.id)}${spec ? ` · 仅 ${esc(spec)}` : ""}</title></rect></g>`;
  }
  // labels stacked per top-left corner, so profiles sharing a corner do not overprint
  const corners = new Map();
  for (const g of groups.values()) for (const p of g) {
    const k = `${getRange(p, xd)[0]}|${getRange(p, yd)[1]}`;
    if (!corners.has(k)) corners.set(k, []);
    corners.get(k).push(p);
  }
  const cornerKeys = [...corners.keys()].map((k) => k.split("|").map(Number));
  for (const list of corners.values()) {
    list.sort((a, b) => (specialty(a) ? 1 : 0) - (specialty(b) ? 1 : 0));
    const [x0] = getRange(list[0], xd), [y0, y1] = getRange(list[0], yd);
    // room until the next label column on the same top row
    const nextX = Math.min(nx, ...cornerKeys.filter(([cx, cy]) => cy === y1 && cx > x0).map(([cx]) => cx));
    const room = (nextX - x0) * cw - 12;
    const tallest = Math.max(...list.map((p) => getRange(p, yd)[1] - getRange(p, yd)[0] + 1));
    const maxLines = Math.max(1, Math.floor((tallest * ch - 10) / 13));
    list.slice(0, maxLines).forEach((p, k) => {
      const more = k === maxLines - 1 && list.length > maxLines ? ` +${list.length - maxLines}` : "";
      const spec = specialty(p);
      const full = `${p.label || p.id}${spec ? ` · 仅${spec}` : ""}${p.enabled === false ? "（停用）" : ""}`;
      s += `<text class="plabel" x="${X(x0) + 8}" y="${Y(y1 + 1) + 16 + k * 13}" fill="${color(p.id)}" font-weight="${p.id === S.sel ? 700 : 500}">${esc(fitText(full, room - (more ? 24 : 0)))}${more}<title>${esc(full)}</title></text>`;
    });
  }
  // history dots at expected values
  if (S.showLog) S.log.forEach((e, k) => {
    const ex = e.expected && e.expected[xd.id], ey = e.expected && e.expected[yd.id];
    if (typeof ex !== "number" || typeof ey !== "number") return;
    const jx = ((k * 37) % 9 - 4) * 0.8, jy = ((k * 53) % 9 - 4) * 0.8;
    const fill = profIndex(e.profile_id) >= 0 ? color(e.profile_id) : "var(--muted)";
    s += `<circle class="dot" cx="${X(ex) + cw / 2 + jx}" cy="${Y(ey + 1) + ch / 2 + jy}" r="4" fill="${fill}"><title>${esc(e.task)} → ${esc(e.profile_id)}</title></circle>`;
  });
  // trial crosshair
  if (ta && ta.expected && typeof ta.expected[xd.id] === "number" && typeof ta.expected[yd.id] === "number") {
    const cx = X(ta.expected[xd.id]) + cw / 2, cy = Y(ta.expected[yd.id] + 1) + ch / 2;
    s += `<line class="cross" x1="${cx}" x2="${cx}" y1="${G.T}" y2="${G.T + ny * ch}"/><line class="cross" x1="${G.L}" x2="${G.L + nx * cw}" y1="${cy}" y2="${cy}"/><circle cx="${cx}" cy="${cy}" r="6" fill="var(--ink)"/>`;
  }
  // handles for the selected profile
  const sp = selProfile();
  if (sp && !S.hidden.has(sp.id)) {
    const [x0, x1] = getRange(sp, xd), [y0, y1] = getRange(sp, yd);
    const l = X(x0), r = X(x1 + 1), t = Y(y1 + 1), b = Y(y0);
    const id = esc(sp.id);
    s += `<line class="edge ew" data-drag="l" data-id="${id}" x1="${l}" x2="${l}" y1="${t}" y2="${b}"/>`;
    s += `<line class="edge ew" data-drag="r" data-id="${id}" x1="${r}" x2="${r}" y1="${t}" y2="${b}"/>`;
    s += `<line class="edge ns" data-drag="t" data-id="${id}" x1="${l}" x2="${r}" y1="${t}" y2="${t}"/>`;
    s += `<line class="edge ns" data-drag="b" data-id="${id}" x1="${l}" x2="${r}" y1="${b}" y2="${b}"/>`;
    for (const [m, hx, hy] of [["lt", l, t], ["rt", r, t], ["lb", l, b], ["rb", r, b]])
      s += `<circle class="handle" data-drag="${m}" data-id="${id}" cx="${hx}" cy="${hy}" r="7" style="cursor:${m === "lt" || m === "rb" ? "nwse" : "nesw"}-resize"/>`;
  }
  host.innerHTML = s + "</svg>";
}

let drag = null;
function cellAt(e) {
  const svg = $("#chart svg"), { nx, ny, cw, ch } = geom();
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  return { cx: clamp(Math.floor((p.x - G.L) / cw), 0, nx - 1), cy: clamp(ny - 1 - Math.floor((p.y - G.T) / ch), 0, ny - 1) };
}
function onChartDown(e) {
  const host = $("#chart"); if (!host || !host.contains(e.target)) return;
  const t = e.target.closest("[data-drag]");
  if (!t) { if (e.target.classList.contains("bg") && S.sel) { S.sel = null; renderChart(); renderEditor(); } return; }
  e.preventDefault();
  const p = profiles().find((q) => q.id === t.dataset.id); if (!p) return;
  const { xd, yd } = geom();
  if (S.sel !== p.id) { S.sel = p.id; renderEditor(); refreshChips(); }
  drag = { mode: t.dataset.drag, p, start: cellAt(e), x: [...getRange(p, xd)], y: [...getRange(p, yd)], moved: false };
  host.setPointerCapture(e.pointerId);
  renderChart();
}
function onChartMove(e) {
  if (!drag) return;
  const { xd, yd, nx, ny } = geom();
  const c = cellAt(e);
  let [x0, x1] = drag.x, [y0, y1] = drag.y;
  const m = drag.mode;
  if (m === "move") {
    const dx = clamp(c.cx - drag.start.cx, -x0, nx - 1 - x1), dy = clamp(c.cy - drag.start.cy, -y0, ny - 1 - y1);
    x0 += dx; x1 += dx; y0 += dy; y1 += dy;
  } else {
    if (m.includes("l")) x0 = Math.min(c.cx, x1);
    if (m.includes("r")) x1 = Math.max(c.cx, x0);
    if (m.includes("t")) y1 = Math.max(c.cy, y0);
    if (m.includes("b")) y0 = Math.min(c.cy, y1);
  }
  const cur = [getRange(drag.p, xd), getRange(drag.p, yd)];
  if (cur[0][0] === x0 && cur[0][1] === x1 && cur[1][0] === y0 && cur[1][1] === y1) return;
  setRange(drag.p, xd, [x0, x1]); setRange(drag.p, yd, [y0, y1]);
  drag.moved = true;
  renderChart(); renderStatus();
}
function onChartUp() {
  if (!drag) return;
  const moved = drag.moved; drag = null;
  if (moved) renderEditor();
}
function refreshChips() {
  const host = $("#chart"); if (!host) return;
  const old = host.parentElement.querySelector(".chips");
  if (old) old.outerHTML = chipsHtml();
}

// ---------------------------------------------------------------- profile editor
function renderEditor() {
  const host = $("#editor"); if (!host) return;
  const p = selProfile();
  if (!p) {
    host.innerHTML = `<h3>档位</h3><p class="muted">点击图上的矩形或下方标签来编辑档位。</p>
      <p class="hint">每个档位 = 一个 agent + 模型 + effort，加上它在每个维度上负责的区间。路由时按「任务落在区间外的概率」打分：超出上限（能力不够）重罚，低于下限（大材小用）轻罚，再加一点成本。分数最低者胜出。</p>`;
    return;
  }
  const i = profIndex(p.id), P = `profiles[${i}]`;
  const kinds = Object.keys(S.installed).length ? Object.keys(S.installed) : [p.kind];
  const kindOpts = kinds.map((k) => `<option value="${esc(k)}" ${k === p.kind ? "selected" : ""}>${esc(k)}${S.installed[k] ? " ✓" : "（未安装）"}</option>`).join("");
  const effOpts = ["", ...EFFORTS].map((e) => `<option value="${e}" ${(p.effort || "") === e ? "selected" : ""}>${e || "（agent 默认）"}</option>`).join("");
  const ranges = dims("score").map((d) => {
    const [a, b] = getRange(p, d), n = nLevels(d);
    const opts = (v) => d.levels.map((t, k) => `<option value="${k}" ${k === v ? "selected" : ""}>${k} · ${esc(shortLevel(t))}</option>`).join("");
    return `<label>${esc(d.label || d.id)}</label><span class="row" data-path="${P}.ranges.${esc(d.id)}"><select data-range="${esc(d.id)}" data-end="0">${opts(a)}</select> 到 <select data-range="${esc(d.id)}" data-end="1">${opts(b)}</select>${a === 0 && b === n - 1 ? '<span class="hint">全覆盖</span>' : ""}</span>`;
  }).join("");
  const affinity = dims("choice").map((d) => {
    const aff = (p.affinity && p.affinity[d.id]) || {};
    const def = aff["*"] ?? 1;
    const seg = (opt, cur, inherit) => `<span class="seg">${inherit ? `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="inherit" class="${cur === undefined ? "on" : ""}">默认</button>` : ""}${AFF_STEPS.map(([v, t]) => `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="${v}" class="${cur !== undefined && Number(cur) === Number(v) ? "on" : ""}">${t}</button>`).join("")}</span>`;
    return `<div class="stack" data-path="${P}.affinity.${esc(d.id)}"><div><b>${esc(d.label || d.id)}</b> <span class="hint">未单独设置的选项按「默认」</span></div>
      <div class="aff"><span>默认</span>${seg("*", def, false)}${Object.keys(d.options).map((o) => `<span title="${esc(d.options[o])}">${esc(o)}</span>${seg(o, aff[o], true)}`).join("")}</div></div>`;
  }).join("");
  host.innerHTML = `<h3><span class="swatch" style="background:${color(p.id)}"></span> ${esc(p.label || p.id)}</h3>
    <div class="grid2">
      <label>ID</label><input data-f="id" data-path="${P}.id" value="${esc(p.id)}" spellcheck="false">
      <label>名称</label><input data-f="label" data-path="${P}.label" value="${esc(p.label || "")}">
      <label>Agent</label><select data-f="kind" data-path="${P}.kind">${kindOpts}</select>
      <label>模型</label><input data-f="model" data-path="${P}.model" value="${esc(p.model || "")}" placeholder="（agent 默认）" spellcheck="false">
      <label>Effort</label><select data-f="effort" data-path="${P}.effort">${effOpts}</select>
      <label>成本</label><span class="row"><input type="number" step="0.1" min="0" data-f="cost" data-path="${P}.cost" value="${p.cost ?? 0}"><span class="hint">分数相近时选更便宜的</span></span>
      <label>启用</label><span class="row"><input type="checkbox" data-f="enabled" ${p.enabled !== false ? "checked" : ""}> <label><input type="checkbox" data-act="hide" ${S.hidden.has(p.id) ? "checked" : ""}> 图上隐藏</label></span>
    </div>
    <hr class="sep"><h3>坐标区间</h3><div class="ranges">${ranges}</div>
    ${affinity ? `<hr class="sep"><h3>类型亲和度</h3>${affinity}` : ""}
    <hr class="sep"><h3>Prompt 前缀</h3><textarea rows="3" data-f="prompt_prefix" data-path="${P}.prompt_prefix" placeholder="派发时加在任务前面，可留空">${esc(p.prompt_prefix || "")}</textarea>
    <hr class="sep"><div class="row"><button data-act="dup-profile">复制档位</button><button class="danger" data-act="del-profile">删除档位</button></div>`;
  markErrors();
}

function setProfileField(p, f, el) {
  if (f === "enabled") p.enabled = el.checked;
  else if (f === "cost") p.cost = el.value === "" ? 0 : Number(el.value);
  else if (["model", "effort", "prompt_prefix"].includes(f)) { if (el.value === "") delete p[f]; else p[f] = el.value; }
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
    if (p.ranges && old in p.ranges) { p.ranges[newId] = p.ranges[old]; delete p.ranges[old]; }
    if (p.affinity && old in p.affinity) { p.affinity[newId] = p.affinity[old]; delete p.affinity[old]; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && old in v) { v[newId] = v[old]; delete v[old]; }
  if (S.axes.x === old) S.axes.x = newId;
  if (S.axes.y === old) S.axes.y = newId;
}
function dropDimRefs(id) {
  for (const p of profiles()) {
    if (p.ranges) delete p.ranges[id];
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
    const r = p.ranges && p.ranges[d.id];
    if (r && (r[0] > hi || r[1] > hi)) { p.ranges[d.id] = [Math.min(r[0], hi), Math.min(r[1], hi)]; n++; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && v[d.id] > hi) v[d.id] = hi;
  if (n) toast(`刻度变少了，已收缩 ${n} 个档位的区间`);
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
      <td><b>${r.total}</b></td><td class="bk">${Object.entries(r.breakdown).filter(([, v]) => v).map(([k2, v]) => `${esc((dimById(k2) || {}).label || (k2 === "cost" ? "成本" : k2))} ${v}`).join(" · ") || "0"}${r.available ? "" : `<br>✗ ${esc(r.why_unavailable)}`}</td></tr>`).join("");
    const err = a.details && a.details.systemone_error ? `<p class="hint">决策模型不可用，已用关键词规则：${esc(a.details.systemone_error)}</p>` : "";
    result = `<div class="split" style="margin-top:16px"><div class="card stack"><h3>维度判断 <span class="muted">${esc(a.source)} · 置信度 ${a.confidence}${a.bumped ? " · 低置信度→难度上调" : ""}</span></h3>${err}${dimBlocks}</div>
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
  const task = ($("#trial-task") || {}).value?.trim() || S.trialTask;
  if (!task) { toast("先输入任务"); return; }
  S.trialTask = task; S.trialBusy = true; render();
  try {
    const { status, body } = await api("/api/route", { method: "POST", body: JSON.stringify({ task, policy: S.policy, use_backend: S.useBackend }) });
    if (status === 422) { S.errors = body.errors || []; toast("配置有错误，先修正再试运行"); }
    else if (status !== 200) toast(body.error || `请求失败 ${status}`);
    else { S.trial = body.decision || { assessment: body.assessment, error: body.error, ranking: [] }; S.errors = []; }
  } finally { S.trialBusy = false; render(); }
}

// ---------------------------------------------------------------- settings + json
function settingsHtml() {
  const sc = S.policy.scoring || {}, be = S.policy.backend || {}, dp = S.policy.dispatch || {};
  const num = (path, v, step, hint) => `<span class="row"><input type="number" step="${step}" min="0" data-set="${path}" data-path="${path}" value="${v ?? ""}">${hint ? `<span class="hint">${hint}</span>` : ""}</span>`;
  const aliases = Object.entries(S.policy.aliases || {});
  const atRows = aliases.filter(([k]) => k.startsWith("@")).map(([k, v]) => `<tr data-path="aliases.${esc(k)}"><td><input value="${esc(k)}" data-alias-key="${esc(k)}" spellcheck="false"></td><td><select data-alias-val="${esc(k)}">${profiles().map((p) => `<option value="${esc(p.id)}" ${p.id === v ? "selected" : ""}>${esc(p.label || p.id)}</option>`).join("")}</select></td><td><button class="ghost danger" data-alias-del="${esc(k)}">删除</button></td></tr>`).join("");
  const bang = Object.fromEntries(aliases.filter(([k]) => k.startsWith("!")));
  return `<div class="split"><div class="card"><h3>打分</h3><div class="grid2">
      <label>超出上限罚分</label>${num("scoring.under_penalty", sc.under_penalty, 0.1, "任务比档位难（能力不够）")}
      <label>低于下限罚分</label>${num("scoring.over_penalty", sc.over_penalty, 0.1, "任务比档位简单（大材小用）")}
      <label>成本权重</label>${num("scoring.cost_weight", sc.cost_weight, 0.01, "罚分 += 成本 × 权重")}
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
  const tab = t.closest("#tabs button");
  if (tab) { S.tab = tab.dataset.tab; render(); return; }
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
  if (pick) { S.sel = pick.dataset.pick; renderChart(); renderEditor(); refreshChips(); return; }
  const sample = t.closest("[data-sample]");
  if (sample) { S.trialTask = sample.dataset.sample; const ta = $("#trial-task"); if (ta) ta.value = S.trialTask; return runTrial(); }
  const aff = t.closest("[data-aff]");
  if (aff) {
    const p = selProfile(); if (!p) return;
    const { aff: did, opt, val } = aff.dataset;
    p.affinity = p.affinity || {}; const m = (p.affinity[did] = p.affinity[did] || {});
    if (val === "inherit" || (opt === "*" && val === "1")) delete m[opt]; else m[opt] = Number(val);
    if (!Object.keys(m).length) delete p.affinity[did];
    if (!Object.keys(p.affinity).length) delete p.affinity;
    changed({ editor: true }); return;
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
    S.policy.dimensions.splice(i, 1); dropDimRefs(d.id); fixAxes(); changed({ all: true }); return;
  }
  const adel = t.closest("[data-alias-del]");
  if (adel) { delete S.policy.aliases[adel.dataset.aliasDel]; changed({ all: true }); return; }

  const act = t.closest("[data-act]")?.dataset.act;
  if (!act) return;
  const p = selProfile();
  if (act === "new-profile") {
    const id = uniqueId("new-profile", profiles().map((q) => q.id));
    profiles().push({ id, label: "新档位", kind: "claude", cost: 1, enabled: true, ranges: {} });
    S.sel = id; changed({ all: true });
  } else if (act === "dup-profile" && p) {
    const c = clone(p); c.id = uniqueId(`${p.id}-copy`, profiles().map((q) => q.id)); c.label = `${p.label || p.id} 副本`;
    profiles().splice(profIndex(p.id) + 1, 0, c); S.sel = c.id; changed({ all: true });
  } else if (act === "del-profile" && p) {
    const refs = Object.entries(S.policy.aliases || {}).filter(([, v]) => v === p.id).map(([k]) => k);
    if (!confirm(`删除档位「${p.label || p.id}」？${refs.length ? `\n别名 ${refs.join(", ")} 也会一起删除。` : ""}`)) return;
    profiles().splice(profIndex(p.id), 1); refs.forEach((k) => delete S.policy.aliases[k]);
    S.sel = null; changed({ all: true });
  } else if (act === "new-dim-score" || act === "new-dim-choice") {
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
  if (t.dataset.f && t.dataset.f !== "id" && t.dataset.f !== "kind" && t.dataset.f !== "effort") {
    const p = selProfile(); if (!p) return;
    setProfileField(p, t.dataset.f, t);
    changed({ chart: ["label", "enabled"].includes(t.dataset.f) });
    if (t.dataset.f === "label" || t.dataset.f === "enabled") refreshChips();
  } else if (t.dataset.dim && ["label", "weight", "instructions"].includes(t.dataset.df)) {
    const d = S.policy.dimensions[+t.dataset.dim];
    d[t.dataset.df] = t.dataset.df === "weight" ? Number(t.value) : t.value;
    changed();
  } else if (t.dataset.set && t.type !== "checkbox" && t.tagName !== "SELECT") {
    setPath(S.policy, t.dataset.set, t.type === "number" ? (t.value === "" ? undefined : Number(t.value)) : t.value);
    changed();
  } else if (t.id === "trial-task") S.trialTask = t.value;
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.axis) { S.axes[t.dataset.axis] = t.value; render(); return; }
  if (t.dataset.act === "toggle-log") { S.showLog = t.checked; renderChart(); return; }
  if (t.dataset.act === "toggle-backend") { S.useBackend = t.checked; return; }
  if (t.dataset.act === "hide") { const p = selProfile(); if (p) { t.checked ? S.hidden.add(p.id) : S.hidden.delete(p.id); renderChart(); refreshChips(); } return; }
  const p = selProfile();
  if (t.dataset.f === "id" && p) { renameProfile(p, t.value); changed({ all: true }); return; }
  if ((t.dataset.f === "kind" || t.dataset.f === "effort") && p) { setProfileField(p, t.dataset.f, t); changed(); return; }
  if (t.dataset.range && p) {
    const d = dimById(t.dataset.range), r = [...getRange(p, d)], end = +t.dataset.end, v = +t.value;
    r[end] = v; if (r[0] > r[1]) r[1 - end] = v;
    setRange(p, d, r); changed({ chart: true, editor: true }); return;
  }
  if (t.dataset.dim && t.dataset.df === "id") { renameDim(S.policy.dimensions[+t.dataset.dim], t.value); changed({ all: true }); return; }
  if (t.dataset.dim && t.dataset.df === "type") {
    const d = S.policy.dimensions[+t.dataset.dim];
    if (t.value === d.type) return;
    if (t.value === "score" ? false : dims("score").length <= 1) { toast("至少保留一个评分维度"); render(); return; }
    dropDimRefs(d.id);
    if (t.value === "score") { delete d.options; d.levels = ["Low", "Medium", "High"]; }
    else { delete d.levels; d.options = { a: "Option A", b: "Option B" }; }
    d.type = t.value; fixAxes(); changed({ all: true }); return;
  }
  if (t.dataset.dimlist) {
    const d = S.policy.dimensions[+t.dataset.dimlist];
    if (d.type === "score" ? applyLevels(d, t.value) : applyOptions(d, t.value)) changed({ all: true });
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
window.addEventListener("beforeunload", (e) => { if (S.policy && isDirty()) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });

load().catch((e) => { if (e.message !== "unauthorized") $("#main").innerHTML = `<p class="pad">加载失败：${esc(e.message)}</p>`; });
