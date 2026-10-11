"use strict";

const tr = window.RouterI18n.t;
window.RouterI18n.apply();

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
// What router/agents.py can pass to each agent kind.
const MODEL_KINDS = ["claude", "codex", "kimi", "cursor", "opencode", "droid"];
const EFFORT_KINDS = ["claude", "codex", "kimi", "cursor", "droid"];
const AFF_STEPS = [["1", "suitable"], ["0.5", "fair"], ["0", "unsuitable"]];
const SAMPLE_TASKS = [
  "rename_this_variable",
  "add_a_ping_display_to_netdemohud",
  "sample_bulk_logging",
  "review_the_current_diff",
  "explain_how_the_fizzysteamworks_relay_works",
  "sample_login_crash",
  "design_state_synchronization_for_multiplayer",
];

const S = {
  policy: null, base: "", source: "", path: "", installed: {}, models: {}, defaults: null,
  tab: "chart", sel: null, axes: { x: null, y: null }, choice: {},
  map: null, mapKey: "",
  log: [], showLog: true, showDisabled: true, showLabels: true, hidden: new Set(),
  search: "", filter: "all",
  trial: null, trialTask: "", trialContext: null, useBackend: true, trialBusy: false,
  saveBusy: false, errors: [], errorPolicy: "",
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

// History tracks the draft, independently of the acknowledged save baseline.
// An input element or a drag object is the transaction key; discrete actions have no key.
const H = { past: [], future: [], current: "", group: null, limit: 100 };
function remember(group = null) {
  const next = JSON.stringify(S.policy);
  if (next === H.current) return false;
  if (!group || H.group !== group) {
    H.past.push(H.current);
    if (H.past.length > H.limit) H.past.shift();
  }
  H.current = next; H.group = group; H.future = [];
  return true;
}
function historyStep(redo = false) {
  if (drag) return;
  const from = redo ? H.future : H.past, to = redo ? H.past : H.future;
  if (!from.length) return;
  clearTimeout(changeTimer); pendingChange = {};
  to.push(H.current); H.current = from.pop(); H.group = null;
  S.policy = JSON.parse(H.current); fixAxes(); fixChoice();
  if (!selProfile()) S.sel = null;
  S.errors = []; S.errorPolicy = "";
  render();
}
function fixChoice() {
  for (const id of Object.keys(S.choice)) {
    const d = dimById(id);
    if (!d || d.type !== "choice" || !(S.choice[id] in d.options)) delete S.choice[id];
  }
}
function changeSummary() {
  const base = JSON.parse(S.base), before = new Map((base.profiles || []).map((p) => [p.id, p]));
  let added = 0, edited = 0;
  for (const p of profiles()) {
    if (!before.has(p.id)) added++;
    else { if (JSON.stringify(p) !== JSON.stringify(before.get(p.id))) edited++; before.delete(p.id); }
  }
  const parts = [added && tr("added_p0_profiles", { p0: added }), edited && tr("edited_p0_profiles", { p0: edited }), before.size && tr("removed_p0_profiles", { p0: before.size })].filter(Boolean);
  for (const [key, label] of [["dimensions", tr("dimensions")], ["aliases", tr("aliases")], ["scoring", tr("scoring_settings")], ["backend", tr("decision_model")], ["dispatch", tr("dispatch_settings")], ["models", tr("model_catalog")], ["binaries", tr("cli_mapping")], ["env", tr("environment")]]) {
    if (JSON.stringify(base[key]) !== JSON.stringify(S.policy[key])) parts.push(tr("p0_changed", { p0: label }));
  }
  return parts.join(" · ") || (isDirty() ? tr("configuration_changed") : tr("draft_matches_the_saved_version"));
}

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
const harnessSeg = (p) => `<span class="seg harness" role="group" aria-label="${tr("run_agent")}">${harnesses(p).map((k) => `<button data-harness="${esc(k)}" aria-pressed="${k === p.kind}" class="${k === p.kind ? "on" : ""} ${S.installed[k] === false ? "warn" : ""}" title="${esc(k)}${S.installed[k] === false ? tr("not_installed") : ""}">${esc(k)}</button>`).join("")}</span>`;
const swatch = (p) => `<span class="swatch ${specialty(p) ? "ring" : ""}" style="background:${color(p.id)};color:${color(p.id)}"></span>`;

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (res.status === 401) { showAuth(); throw new Error("unauthorized"); }
  return { status: res.status, body: await res.json().catch(() => ({})) };
}
function showAuth() {
  $("#main").innerHTML = `<div class="card pad"><h3>${tr("access_token_required")}</h3><p>${tr("use")} <code>herdr-router ui</code> ${tr("to_print_a_link_containing")} <code>?token=</code> ${tr("to_open_this_page_access_will_be_remembered")}</p></div>`;
}
let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

async function load() {
  const { body } = await api("/api/policy");
  S.policy = body.policy; S.base = JSON.stringify(body.policy);
  H.current = S.base; H.past = []; H.future = []; H.group = null;
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
  api("/api/policy/default").then(({ body: d }) => { S.defaults = d.policy || null; if (S.tab === "chart" && !S.sel && !$("#editor")?.contains(document.activeElement)) renderEditor(); }).catch(() => {});
  // cursor-agent --list-models can take a few seconds on a cold cache; do not block the page on it
  api("/api/models").then(({ body: m }) => { S.models = m.models || {}; if (S.tab === "chart" && S.sel && !$("#editor")?.contains(document.activeElement)) renderEditor(); }).catch(() => {});
}

// ---------------------------------------------------------------- territory map
// The server samples who wins a task at each point of the plane (score.territory),
// so the map always agrees with real routing. Refreshes are debounced while typing
// and throttled while dragging, so the map follows a dot as it moves.
let mapTimer, mapSeq = 0, mapBusy = false, mapAgain = false;
const sliceKey = () => JSON.stringify({ policy: S.policy, x: S.axes.x, y: S.axes.y, choice: S.choice });
function refreshMap(delay = 150) {
  clearTimeout(mapTimer);
  mapTimer = setTimeout(fetchMap, delay);
}
async function fetchMap() {
  // coarse while a dot is being dragged (fast enough to follow it), fine once it is dropped
  const req = JSON.stringify({ policy: S.policy, x: S.axes.x, y: S.axes.y, choice: S.choice, steps: drag ? 48 : 96 });
  const key = sliceKey(), axes = { ...S.axes };
  if (req === S.mapKey) return;
  if (mapBusy) { mapAgain = true; return; }
  mapBusy = true;
  const seq = ++mapSeq;
  let res;
  try { res = await api("/api/map", { method: "POST", body: req }); } catch { mapBusy = false; if (mapAgain) { mapAgain = false; refreshMap(); } return; }
  mapBusy = false;
  if (seq === mapSeq && key === sliceKey()) {
    S.mapKey = res.status === 200 ? req : "";
    S.map = res.status === 200 ? { ...res.body, key, xd: axes.x, yd: axes.y, url: "", urlKey: "" } : null;
    if (S.tab === "chart") { renderChart(); if (!S.sel) updateList(); }
  }
  else mapAgain = true;
  if (mapAgain) { mapAgain = false; fetchMap(); }
}
const mapReady = () => S.map && S.map.key === sliceKey();
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
  clearTimeout(changeTimer); pendingChange = {};
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
  renderStatus();
  renderErrors();
  const main = $("#main");
  if (S.tab === "chart") main.innerHTML = `${cmdbarHtml()}<div id="trialbox">${trialBoxHtml()}</div><div class="split"><div class="card">${toolbarHtml()}<div id="chart"></div><div class="chips offplane" id="offplane"></div><div class="cellinfo" id="cellinfo"></div>${legendHtml()}</div><aside class="card" id="editor"></aside></div>`;
  if (S.tab === "dims") main.innerHTML = dimsHtml();
  if (S.tab === "trial") main.innerHTML = trialHtml();
  if (S.tab === "settings") main.innerHTML = settingsHtml();
  if (S.tab === "json") main.innerHTML = jsonHtml();
  if (S.tab === "chart") { renderChart(); renderEditor(); refreshMap(0); }
  markErrors();
}
function renderStatus() {
  window.RouterI18n.apply();
  $("#dirty").hidden = !isDirty();
  $("#source").textContent = S.source === "user" ? S.path.replace(/^\/(Users|home)\/[^/]+/, "~") : tr("source_defaults");
  const revert = $("#btn-revert"), saveButton = $("#btn-save");
  revert.textContent = tr("discard_changes"); revert.disabled = !isDirty() || S.saveBusy;
  saveButton.disabled = S.saveBusy; saveButton.textContent = S.saveBusy ? tr("saving") : tr("save");
  if (!$("#draft-controls")) {
    const box = document.createElement("div"); box.id = "draft-controls"; box.className = "row pad";
    box.innerHTML = `<button id="btn-undo" data-i18n="undo" data-i18n-title="undo_title" title="${tr("undo_title")}">${tr("undo")}</button><button id="btn-redo" data-i18n="redo" data-i18n-title="redo_ctrl_shift_z" title="${tr("redo_ctrl_shift_z")}">${tr("redo")}</button><span id="draft-summary" class="hint change-summary" role="status"></span>`;
    $("#main").before(box);
  }
  $("#btn-undo").disabled = !H.past.length || !!drag;
  $("#btn-redo").disabled = !H.future.length || !!drag;
  $("#draft-summary").textContent = changeSummary();
  updateTrialValidity();
}
function updateTrialValidity() {
  document.querySelectorAll("[data-stale]").forEach((el) => { el.hidden = !trialStale(); el.textContent = staleText(); });
  document.querySelectorAll("[data-trial-validity]").forEach((el) => { el.textContent = trialStale() ? tr("previous_trial_result") : tr("recommended_profile"); });
}
function renderErrors() {
  const box = $("#errors");
  box.hidden = !S.errors.length;
  box.innerHTML = S.errors.length ? `<strong>${tr("found")} ${S.errors.length} ${tr("configuration_errors")}</strong><ul>${S.errors.map((e, i) => `<li><button class="ghost" data-err="${i}"><code>${esc(e.path)}</code> ${esc(e.message)}</button></li>`).join("")}</ul>` : "";
}
function markErrors() {
  document.querySelectorAll(".err[data-path]").forEach((el) => { el.classList.remove("err"); el.removeAttribute("aria-invalid"); });
  for (const e of S.errors) document.querySelectorAll(`[data-path="${CSS.escape(e.path)}"]`).forEach((el) => { el.classList.add("err"); el.setAttribute("aria-invalid", "true"); });
}
let changeTimer, pendingChange = {};
function changed(opts = {}) {
  const modified = remember(opts.group);
  if (modified) {
    hoverSeq++; clearTimeout(hoverTimer);
    if (S.errors.length) { S.errors = []; S.errorPolicy = ""; renderErrors(); markErrors(); }
  }
  if (opts.defer) {
    pendingChange.chart ||= opts.chart;
    clearTimeout(changeTimer);
    changeTimer = setTimeout(() => { const next = pendingChange; pendingChange = {}; paintChange(next); }, 120);
  } else paintChange(opts);
  if (S.tab === "chart") refreshMap();
}
function paintChange(opts) {
  renderStatus();
  if (opts.all) render();
  else if (S.tab === "chart") {
    if (opts.chart) renderChart();
    if (opts.editor || (opts.list && !S.sel)) renderEditor();
  }
}

// ---------------------------------------------------------------- chart tab chrome
function cmdbarHtml() {
  return composerHtml("cmd");
}
function composerHtml(id) {
  return `<div class="card task-composer"><label for="${id}"><strong>${tr("enter_a_task_to_see_a_recommendation")}</strong></label>
    <p class="hint">${tr("composer_help")}</p>
    <textarea id="${id}" rows="3" placeholder="${tr("task_placeholder")}" spellcheck="false">${esc(S.trialTask)}</textarea>
    <div class="row spread">
    <label class="check" title="${tr("backend_toggle_title")}"><input type="checkbox" data-act="toggle-backend" ${S.useBackend ? "checked" : ""}>Jev</label>
    <button class="primary" data-act="run-trial" ${S.trialBusy ? "disabled" : ""}>${S.trialBusy ? tr("assessing") : tr("try_routing")}</button></div>
    <details class="task-examples"><summary>${tr("choose_a_sample_task")}</summary><div class="chips">${SAMPLE_TASKS.map((key) => tr(key)).map((x) => `<button class="chip" data-sample="${esc(x)}">${esc(x)}</button>`).join("")}</div></details></div>`;
}
function toolbarHtml() {
  const opts = (sel) => dims("score").map((d) => `<option value="${esc(d.id)}" ${d.id === sel ? "selected" : ""}>${esc(d.label || d.id)}</option>`).join("");
  const choice = dims("choice").map((d) => `<label>${esc(d.label || d.id)} <select data-choice="${esc(d.id)}"><option value="">${tr("any")}</option>${Object.keys(d.options).map((o) => `<option value="${esc(o)}" ${S.choice[d.id] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`).join("");
  return `<div class="toolbar"><label>${tr("x_axis")} <select data-axis="x">${opts(S.axes.x)}</select></label><label>${tr("y_axis")} <select data-axis="y">${opts(S.axes.y)}</select></label>${choice}
    <span class="chart-layers row"><label class="check"><input type="checkbox" data-act="toggle-log" ${S.showLog ? "checked" : ""}>${tr("history")} ${S.log.length}</label>
    <label class="check"><input type="checkbox" data-act="toggle-disabled" ${S.showDisabled ? "checked" : ""}>${tr("show_disabled_profiles")}</label>
    <label class="check"><input type="checkbox" data-act="toggle-labels" ${S.showLabels ? "checked" : ""}>${tr("show_labels")}</label></span></div>
    <p class="hint" id="map-slice-description">${sliceDescription()} ${tr("map_layers_help")}</p>`;
}
function sliceDescription() {
  const choices = dims("choice").map((d) => `${esc(d.label || d.id)}${tr("colon")}${esc(S.choice[d.id] || tr("any_uniform_prior"))}`).join(tr("clause_separator"));
  const other = dims("score").filter((d) => ![S.axes.x, S.axes.y].includes(d.id)).map((d) => esc(d.label || d.id)).join(tr("list_separator"));
  return tr("slice_description", { p0: choices ? `${tr("comma")}${choices}` : "", p1: other ? tr("p0_is_excluded_from_this_slice", { p0: other }) : "" });
}
function legendHtml() {
  return `<div class="legend"><span><span class="lg-sq"></span>${tr("legend_background")}</span><span><span class="lg-fill"></span>${tr("color_model_vendor_drag_to_any_position")}</span><span><span class="lg-ring"></span>${tr("specialist")}</span><span><span class="lg-ring dash"></span>${tr("not_installed_2")}</span><span><span class="lg-line"></span>${tr("ignores_this_axis")}</span><span><span class="lg-eff"></span>${tr("reasoning_levels_of_one_model_low_max")}</span></div>`;
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
  return `${p.label || p.id}${via}${specialty(p) ? ` · ${specialty(p)}` : ""}${p.enabled === false ? tr("disabled") : ""}`;
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
  let s = `<svg class="chart" viewBox="0 0 ${G.W} ${G.H}" role="group" aria-label="${tr("map_accessible_label")}">`;
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
  const visible = profiles().filter((p) => !S.hidden.has(p.id) && (S.showDisabled || p.enabled !== false));
  const pos = anchors(g, visible);
  const placed = visible.filter((p) => pos.has(p.id));
  const sp = selProfile(), trialWinner = S.trial && S.trial.profile_id;
  const sameModel = (p) => !sp || shadeKey(p) === shadeKey(sp);
  const emphasis = (p) => sameModel(p) ? 1 : 0.3;

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
    s += `<polyline class="effline" points="${pts}" stroke="${color(ln.ps[0].id)}" opacity="${emphasis(ln.ps[0])}" style="stroke-width:${sp && sameModel(ln.ps[0]) ? 3 : 1.5}px" ${off ? 'stroke-dasharray="3 4"' : ""}/>`;
    ln.ps.forEach((p, k) => lineOf.set(p.id, { ln, head: k === ln.ps.length - 1 }));
  }
  const chartLabel = (p) => {
    const l = lineOf.get(p.id);
    if (!l || p.id === S.sel || p.id === trialWinner) return labelText(p);
    return l.head ? `${l.ln.entry.label} · ${p.effort}${p.enabled === false ? tr("disabled") : ""}` : `${p.effort}${p.enabled === false ? tr("disabled") : ""}`;
  };
  const rankKey = (p) => (p.id === S.sel ? -2 : p.id === trialWinner ? -1 : lineOf.get(p.id)?.head ? -1 + 1e-3 : -(sh[p.id] || 0));
  const order = [...placed].sort((a, b) => rankKey(a) - rankKey(b));
  const labels = S.showLabels ? placeLabels(g, order, pos, chartLabel, new Set([S.sel, trialWinner].filter(Boolean))) : new Map();
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
    s += `<g class="dotg" data-drag="move" data-id="${esc(p.id)}" data-chart-pick="${esc(p.id)}" role="button" tabindex="0" aria-label="${tr("edit")} ${esc(labelText(p))}" aria-pressed="${p.id === S.sel}" opacity="${(p.enabled === false ? 0.35 : 1) * emphasis(p)}"><circle cx="${c.dx}" cy="${c.dy}" r="10" fill="transparent"/>${body}`;
    if (p.id === S.sel) s += `<circle class="dot-ring" cx="${c.dx}" cy="${c.dy}" r="10"/>`;
    if (p.id === trialWinner) s += `<circle class="pulse" cx="${c.dx}" cy="${c.dy}" r="7"/>`;
    s += `<title>${esc(labelText(p))}${p.note ? "\n" + esc(p.note) : ""}\n${esc(p.kind)}${p.model ? " · " + esc(p.model) : ""}${p.effort ? " · " + esc(p.effort) : ""}${inst ? "" : tr("not_installed_3")}</title></g>`;
  }
  for (const p of order) {
    if (!labels.has(p.id)) continue;
    const { box, t } = labels.get(p.id);
    const strong = p.id === S.sel || p.id === trialWinner || (sh[p.id] || 0) > 0 || !mapReady();
    const small = lineOf.has(p.id) && !lineOf.get(p.id).head && p.id !== S.sel && p.id !== trialWinner;
    s += `<text class="plabel ${small ? "eff" : ""}" x="${box.x0 + 2}" y="${box.y0 + 11.5}" fill="${strong ? "var(--ink)" : "var(--muted)"}" font-weight="${p.id === S.sel ? 700 : 500}" opacity="${(p.enabled === false ? 0.5 : 1) * emphasis(p)}">${esc(t)}</text>`;
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
  if (offBox) offBox.innerHTML = off.length ? `<span class="hint">${tr("ignores_both_axes")}</span>${off.map((p) => `<button class="chip ${p.id === S.sel ? "on" : ""}" data-pick="${esc(p.id)}">${swatch(p)}${esc(labelText(p))}</button>`).join("")}` : "";
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
  if (!v) { hoverSeq++; clearTimeout(hoverTimer); hov.setAttribute("visibility", "hidden"); info.innerHTML = ""; return; }
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
  const key = sliceKey();
  hoverTimer = setTimeout(async () => {
    let res;
    try { res = await api("/api/map", { method: "POST", body: JSON.stringify({ policy: S.policy, x: S.axes.x, y: S.axes.y, choice: S.choice, at: [v.x, v.y] }) }); } catch { return; }
    if (seq !== hoverSeq || key !== sliceKey() || res.status !== 200 || !info.isConnected) return;
    const [first, ...rest] = res.body.top;
    info.innerHTML = `${where} → ${first ? `<b>${name(first.id)}</b> ${tr("penalty")} ${first.total}` : tr("no_available_profiles")}${rest.length ? ` <span class="dim">${tr("runner_up")} ${rest.map((r) => `${name(r.id)} ${r.total}`).join(" · ")}</span>` : ""} <span class="hint">${tr("lower_penalties_are_better")}</span>`;
  }, 140);
}
function onChartDown(e) {
  if (e.button !== 0 || drag) return;
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
  H.group = null;
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
  changed({ chart: true, group: drag });
  const info = $("#cellinfo");
  if (info) info.innerHTML = `<b>${esc(drag.p.label || drag.p.id)}</b> → ${[nxv != null && `${esc(xd.label || xd.id)} ${fmt(nxv)}`, nyv != null && `${esc(yd.label || yd.id)} ${fmt(nyv)}`].filter(Boolean).join(" · ")}`;
  refreshMap(0); // throttled by fetchMap: one request in flight, the latest state follows
}
function onChartUp() {
  if (!drag) return;
  const moved = drag.moved; drag = null;
  H.group = null;
  if (moved) changed({ editor: true });
  renderChart(); // the agent switcher is hidden while a dot is held
}

// ---------------------------------------------------------------- inspector
function renderEditor() {
  const host = $("#editor"); if (!host) return;
  const focus = editorFocusKey(host);
  const p = selProfile();
  host.innerHTML = p ? editorHtml(p) : listHtml();
  markErrors();
  if (focus) host.querySelector(focus)?.focus();
}
function editorFocusKey(host) {
  const el = document.activeElement; if (!host.contains(el)) return null;
  if (el.id) return `#${CSS.escape(el.id)}`;
  for (const key of ["enable", "harness", "effort", "f", "ptoff"]) {
    if (el.dataset[key] !== undefined) return `[data-${key}="${CSS.escape(el.dataset[key])}"]`;
  }
  if (el.dataset.aff !== undefined) return `[data-aff="${CSS.escape(el.dataset.aff)}"][data-opt="${CSS.escape(el.dataset.opt)}"][data-val="${CSS.escape(el.dataset.val)}"]`;
  return null;
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
  toast(tr("added_p0_profiles_save_to_activate", { p0: add.length }));
  changed({ all: true });
}
function listHtml() {
  const fresh = newDefaults();
  return `<div class="insp-head"><h3>${tr("profiles")} <span class="muted mono">${profiles().length}</span></h3><button data-act="new-profile">${tr("new")}</button></div>
    ${fresh.length ? `<div class="banner"><div>${tr("built_in_defaults_contain")} ${fresh.length} ${tr("new_profiles")}<span class="mono">${esc(fresh.slice(0, 6).map((p) => p.label || p.id).join(tr("list_separator")))}${fresh.length > 6 ? " …" : ""}</span></div><button data-act="merge-defaults">${tr("add")}</button></div>` : ""}
    <div class="profile-tools"><div class="profile-search"><label for="profile-search">${tr("search_profiles")}</label><input id="profile-search" type="search" value="${esc(S.search)}" placeholder="${tr("name_model_agent_or_note")}" style="width:100%"></div>
      <div class="profile-filter"><label for="profile-filter">${tr("filter_by_status")}</label> <select id="profile-filter">${[["all", tr("all")], ["enabled", tr("enabled")], ["disabled", tr("disabled_2")], ["missing", tr("cli_not_installed")], ["zero", tr("zero_coverage_in_this_slice")]].map(([v, l]) => `<option value="${v}" ${S.filter === v ? "selected" : ""}>${l}</option>`).join("")}</select></div></div>
    <p class="hint">${tr("coverage_help")}</p>
    <div id="profile-results">${profileRowsHtml()}</div>`;
}
function updateList() {
  const box = $("#profile-results");
  if (box) { const focus = editorFocusKey(box); box.innerHTML = profileRowsHtml(); if (focus) box.querySelector(focus)?.focus(); }
}
function profileRowsHtml() {
  const sh = shares(), ready = mapReady();
  const query = S.search.trim().toLocaleLowerCase();
  const matches = profiles().filter((p) => {
    if (query && ![p.id, p.label, p.model, p.kind, p.effort, p.note, modelEntry(p)?.label, vendorName(vendorOf(p))].filter(Boolean).join(" ").toLocaleLowerCase().includes(query)) return false;
    if (S.filter === "enabled") return p.enabled !== false;
    if (S.filter === "disabled") return p.enabled === false;
    if (S.filter === "missing") return !isInstalled(p);
    if (S.filter === "zero") return ready && p.enabled !== false && isInstalled(p) && !sh[p.id];
    return true;
  });
  const vendors = [...new Set(matches.map(vendorOf))];
  const row = (p) => {
    const spec = specialty(p), n = sh[p.id] || 0;
    const status = [p.enabled === false ? tr("disabled_2") : tr("enabled"), !isInstalled(p) && tr("cli_not_installed_2"), S.hidden.has(p.id) && tr("hide_on_chart_only"), ready && p.enabled !== false && isInstalled(p) && !n && tr("zero_coverage_in_this_slice")].filter(Boolean).join(" · ");
    const meta = [status, p.kind, p.model ? p.model.split("/").pop() : tr("default_model"), p.effort, spec && tr("specialist_p0", { p0: spec }), `$${p.cost ?? 0}`, p.time != null && tr("p0_minutes", { p0: p.time })].filter(Boolean).join(" · ");
    return `<div class="prow ${p.id === S.sel ? "sel" : ""} ${p.enabled === false ? "off" : ""}">
      ${swatch(p)}<button class="name ghost" data-pick="${esc(p.id)}" title="${esc(meta)}" style="text-align:left;white-space:normal">${esc(p.label || p.id)}</button>
      <span class="wins ${n ? "" : "zero"}" title="${tr("sampled_coverage_in_the_current_slice")}">${ready ? n ? `${(n * 100).toFixed(1)}%` : "0%" : tr("pending_update")}</span>
      <input type="checkbox" data-enable="${esc(p.id)}" data-path="profiles[${profIndex(p.id)}].enabled" ${p.enabled !== false ? "checked" : ""} aria-label="${tr("enable")} ${esc(p.label || p.id)}">
      <span class="meta">${esc(meta)}</span></div>`;
  };
  return `<p class="hint" role="status">${tr("showing")} ${matches.length} / ${profiles().length} ${tr("profiles_2")}${ready ? "" : tr("slice_sampling_pending")}</p>${matches.length ? vendors.map((v) => `<div class="group"><div class="group-h"><span class="swatch" style="background:${vendorColor(v)}"></span>${esc(vendorName(v))}</div>${matches.filter((p) => vendorOf(p) === v).map(row).join("")}</div>`).join("") : `<p class="profile-empty hint">${S.filter === "zero" && !ready ? tr("coverage_not_ready") : tr("no_matching_profiles")}</p>`}`;
}
function editorHtml(p) {
  const i = profIndex(p.id), P = `profiles[${i}]`;
  const kinds = Object.keys(S.installed).length ? Object.keys(S.installed) : [p.kind];
  if (!kinds.includes(p.kind)) kinds.unshift(p.kind);
  const kindOpts = kinds.map((k) => `<option value="${esc(k)}" ${k === p.kind ? "selected" : ""}>${esc(k)}${S.installed[k] === false ? tr("not_installed") : ""}</option>`).join("");
  const catalog = S.models[p.kind] || [];
  const entry = catalog.find((m) => m.id === p.model);
  const noModel = !MODEL_KINDS.includes(p.kind), noEffort = !EFFORT_KINDS.includes(p.kind);
  const effList = entry && entry.efforts?.length ? entry.efforts : EFFORTS;
  const effSeg = ["", ...effList, ...(p.effort && !effList.includes(p.effort) ? [p.effort] : [])].map((e) =>
    `<button data-effort="${e}" aria-pressed="${(p.effort || "") === e}" class="${(p.effort || "") === e ? "on" : ""} ${e && !effList.includes(e) ? "warn" : ""}" ${e && !effList.includes(e) ? `title="${tr("unsupported_effort")}"` : ""}>${e || tr("default")}</button>`).join("");
  const me = modelEntry(p);
  const modelHint = noModel ? tr("model_unsupported_hint")
    : me ? `${esc(me.label)} · ${esc(vendorName(me.vendor))}` : entry ? esc(entry.label) : p.model && catalog.length ? tr("unknown_model_hint") : catalog.length ? tr("choose_from_p0_models_click_the_input_to_select", { p0: catalog.length }) : "";
  const effortHint = noEffort ? (p.kind === "opencode" ? tr("opencode_effort_hint") : tr("this_agent_does_not_support_an_effort_argument")) : "";
  const coords = dims("score").map((d) => {
    const v = getPoint(p, d), n = nLevels(d), on = v != null;
    return `<label class="lbl" for="pt-${esc(d.id)}">${esc(d.label || d.id)}</label><div class="pt" data-path="${P}.point.${esc(d.id)}">
        <input type="range" min="0" max="${n - 1}" step="0.01" value="${on ? v : (n - 1) / 2}" data-pt="${esc(d.id)}" aria-label="${esc(d.label || d.id)}${tr("coordinate_slider")}" data-path="${P}.point.${esc(d.id)}" ${on ? "" : "disabled"}>
        <input id="pt-${esc(d.id)}" class="mono pt-v" type="number" min="0" max="${n - 1}" step="0.01" value="${on ? v : ""}" data-pt="${esc(d.id)}" data-path="${P}.point.${esc(d.id)}" style="width:5em" ${on ? "" : "disabled"}>
        <label class="check"><input type="checkbox" data-ptoff="${esc(d.id)}" ${on ? "" : "checked"}>${tr("ignore")}</label></div>
      <span class="desc">${on ? esc(shortLevel(d.levels[Math.round(v)])) : tr("no_penalty_on_this_dimension")}</span>`;
  }).join("");
  const affinity = dims("choice").map((d) => {
    const aff = (p.affinity && p.affinity[d.id]) || {};
    const def = aff["*"] ?? 1;
    const seg = (opt, cur, inherit) => `<span class="seg" data-path="${P}.affinity.${esc(d.id)}.${esc(opt)}" role="group" aria-label="${esc(d.label || d.id)} · ${esc(opt === "*" ? tr("default_affinity") : opt)}">${inherit ? `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="inherit" aria-pressed="${cur === undefined}" class="${cur === undefined ? "on" : ""}">${tr("default")}</button>` : ""}${AFF_STEPS.map(([v, labelKey]) => `<button data-aff="${esc(d.id)}" data-opt="${esc(opt)}" data-val="${v}" aria-pressed="${cur !== undefined && Number(cur) === Number(v)}" class="${cur !== undefined && Number(cur) === Number(v) ? "on" : ""}">${esc(tr(labelKey))}</button>`).join("")}</span>`;
    return `<div class="stack" data-path="${P}.affinity.${esc(d.id)}"><div class="row spread"><span class="sect" style="margin:0">${esc(d.label || d.id)}</span><span class="hint">${tr("unspecified_options_use")}</span></div>
      <div class="aff"><span>*</span>${seg("*", def, false)}${Object.keys(d.options).map((o) => `<span title="${esc(d.options[o])}">${esc(o)}</span>${seg(o, aff[o], true)}`).join("")}</div></div>`;
  }).join("");
  return `<button class="ghost back" data-act="back">${tr("all_profiles")}</button>
    <div class="ed-title">${swatch(p)}<h3>${esc(p.label || p.id)}</h3><span class="mono">${esc(p.id)}</span></div>
    ${p.note ? `<p class="hint note">${esc(p.note)}</p>` : ""}
    <section class="editor-section"><h3>${tr("basic_information")}</h3><div class="editor-section-body grid2">
      ${me ? `<label>${tr("run_with")}</label><div>${harnessSeg(p)}<div class="hint">${tr("switch_agent_hint")}</div></div>` : ""}
      <label for="profile-kind">Agent</label><select id="profile-kind" data-f="kind" data-path="${P}.kind">${kindOpts}</select>
      <label for="profile-model">${tr("model")}</label><div><input id="profile-model" data-f="model" data-path="${P}.model" value="${esc(p.model || "")}" placeholder="${tr("agent_default")}" list="dl-models" spellcheck="false" autocomplete="off" style="width:100%" class="mono">
        <datalist id="dl-models">${catalog.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("")}</datalist>
        ${modelHint ? `<div class="hint">${modelHint}</div>` : ""}</div>
      <label>${tr("reasoning_effort")}</label><div><span class="seg" role="group" aria-label="${tr("reasoning_effort")}" data-path="${P}.effort">${effSeg}</span>${effortHint ? `<div class="hint">${effortHint}</div>` : ""}</div>
      <label for="profile-label">${tr("name")}</label><input id="profile-label" data-f="label" data-path="${P}.label" value="${esc(p.label || "")}">
      <label for="profile-id">${tr("profile_id")}</label><input id="profile-id" data-f="id" data-path="${P}.id" value="${esc(p.id)}" spellcheck="false" class="mono">
      <label>${tr("status")}</label><span class="row"><label class="check"><input type="checkbox" data-f="enabled" data-path="${P}.enabled" ${p.enabled !== false ? "checked" : ""}>${tr("enable_routing")}</label><label class="check"><input type="checkbox" data-act="hide" ${S.hidden.has(p.id) ? "checked" : ""}>${tr("hide_on_chart_only")}</label></span>
    </div><p class="hint">${tr("hiding_on_the_chart_does_not_change_routing")}${isInstalled(p) ? "" : tr("missing_cli_hint")}</p></section>
    <section class="editor-section"><h3>${tr("coordinates")}</h3><div class="editor-section-body rng">${coords}</div>
    <p class="hint">${tr("coordinates_help")}</p></section>
    <section class="editor-section"><h3>${tr("task_affinity")}</h3><div class="editor-section-body">${affinity || `<p class="hint">${tr("this_configuration_has_no_choice_dimensions")}</p>`}</div></section>
    <details class="editor-section"><summary>${tr("advanced_settings")}</summary><div class="editor-section-body stack"><div class="grid2">
      <label for="profile-cost">${tr("cost")}</label><span class="row"><input id="profile-cost" type="number" step="0.01" min="0" data-f="cost" data-path="${P}.cost" value="${p.cost ?? 0}"><span class="hint">${tr("cost_hint")}</span></span>
      <label for="profile-time">${tr("time")}</label><span class="row"><input id="profile-time" type="number" step="0.1" min="0" data-f="time" data-path="${P}.time" value="${p.time ?? 0}"><span class="hint">${tr("minutes_per_task_weighted_by_time_weight")}</span></span></div>
      <div><label for="profile-prefix">${tr("task_prefix")}</label><textarea id="profile-prefix" rows="3" data-f="prompt_prefix" data-path="${P}.prompt_prefix" placeholder="${tr("prepended_to_the_task_on_dispatch_may_be_empty")}">${esc(p.prompt_prefix || "")}</textarea></div>
      <div><label for="profile-note">${tr("note")}</label><textarea id="profile-note" rows="2" data-f="note" data-path="${P}.note">${esc(p.note || "")}</textarea></div></div></details>
    <hr class="sep"><div class="row"><button data-act="dup-profile">${tr("duplicate_profile")}</button><button class="danger" data-act="del-profile">${tr("delete_profile")}</button></div>`;
}

function setProfileField(p, f, el) {
  if (f === "enabled") p.enabled = el.checked;
  else if (f === "cost") p.cost = el.value === "" ? 0 : Number(el.value);
  else if (f === "time") { if (el.value === "") delete p.time; else p.time = Number(el.value); }
  else if (["model", "prompt_prefix", "note"].includes(f)) { const v = f === "model" ? el.value.trim() : el.value; if (v === "") delete p[f]; else p[f] = v; }
  else if (f !== "id") p[f] = el.value;
}
function renameProfile(p, newId) {
  newId = newId.trim();
  if (!newId || newId === p.id) return;
  if (profIndex(newId) >= 0) { toast(tr("id_p0_already_exists", { p0: newId })); renderEditor(); return; }
  const old = p.id;
  p.id = newId;
  for (const [k, v] of Object.entries(S.policy.aliases || {})) if (v === old) S.policy.aliases[k] = newId;
  if (S.hidden.delete(old)) S.hidden.add(newId);
  S.sel = newId;
  toast(tr("renamed_and_updated_its_aliases"));
}

// ---------------------------------------------------------------- trial summary (chart tab)
const trialPolicy = () => S.trialContext ? JSON.parse(S.trialContext.policy) : S.policy;
const trialDims = () => trialPolicy().dimensions || [];
const penaltyLabel = (id) => (trialDims().find((d) => d.id === id) || {}).label || ({ cost: tr("cost"), time: tr("time") }[id] || id);
const penaltyText = (v) => String(Math.round(Number(v) * 10000) / 10000);
function trialStale() {
  return !!S.trial && !!S.trialContext && (S.trialContext.policy !== JSON.stringify(S.policy) || S.trialContext.task !== S.trialTask.trim() || S.trialContext.useBackend !== S.useBackend);
}
function staleText() {
  const policyChanged = S.trialContext?.policy !== JSON.stringify(S.policy);
  return tr("stale_result", { p0: policyChanged ? tr("draft_configuration_changed") : tr("task_or_assessment_method_changed") });
}
function trialReason(t) {
  const ranked = (t.ranking || []).filter((r) => r.available), win = (t.ranking || []).find((r) => r.profile_id === t.profile_id);
  if (!win) return tr("no_recommendation");
  const parts = Object.entries(win.breakdown || {}).filter(([, v]) => Number(v) !== 0).map(([k, v]) => `${penaltyLabel(k)} ${penaltyText(v)}`).join(tr("list_separator")) || tr("all_scoring_terms_are_0");
  if (t.reason === "explicitly pinned") return tr("pinned_reason", { p0: penaltyText(win.total), p1: parts });
  const runner = ranked.find((r) => r.profile_id !== t.profile_id);
  let reason = tr("lowest_penalty_reason", { p0: penaltyText(win.total), p1: parts });
  if (runner) {
    const gap = Number(runner.total) - Number(win.total);
    if (gap === 0) reason += tr("tied_penalty_reason", { p0: runner.label });
    else if (gap > 0) {
      const keys = new Set([...Object.keys(runner.breakdown || {}), ...Object.keys(win.breakdown || {})]);
      const differences = [...keys].map((k) => [k, Number(runner.breakdown?.[k] || 0) - Number(win.breakdown?.[k] || 0)]).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, 2);
      reason += tr("compared_with_runner_up_p0_p1_less_penalty_p2", { p0: runner.label, p1: penaltyText(gap), p2: differences.length ? tr("main_advantages_p0", { p0: differences.map(([k, v]) => tr("p0_p1_less_penalty", { p0: penaltyLabel(k), p1: penaltyText(v) })).join(tr("list_separator")) }) : "" });
    }
  } else reason += tr("only_one_profile_was_available_for_this_request");
  return reason;
}
function startupCommand(t = S.trial) {
  if (!t?.kind) return "";
  const bin = (trialPolicy().binaries || {})[t.kind] || t.kind;
  const quote = (arg) => /^[a-zA-Z0-9_./:=,@+\-]+$/.test(String(arg)) ? String(arg) : "'" + String(arg).replace(/'/g, "'\\''") + "'";
  return [bin, ...(t.args || [])].map(quote).join(" ");
}
async function copyCommand() {
  const command = startupCommand(); if (!command) return;
  try { await navigator.clipboard.writeText(command); toast(tr("startup_command_copied")); }
  catch { const el = $("[data-command]"); if (el) { el.focus(); el.select(); } toast(tr("copy_failed")); }
}
function trialBoxHtml() {
  const t = S.trial; if (!t) return "";
  const a = t.assessment || {};
  const top = (t.ranking || []).filter((r) => r.available).slice(0, 5);
  const max = Math.max(0.01, ...top.map((r) => r.total));
  const choices = Object.entries(a.choices || {}).filter(([, v]) => v).map(([k, v]) => `${penaltyLabel(k)}=${v}`).join(" · ");
  const source = ({ systemone: tr("decision_model"), heuristic: tr("keyword_rules") })[a.source] || a.source || tr("unknown_source");
  const modelLabel = Object.values(trialPolicy().models || {}).find((m) => m.via?.[t.kind] === t.model)?.label || t.model || tr("agent_default_model");
  return `<div class="card trialbox result-summary"><div class="row spread"><span class="trial-task">${esc(S.trialContext?.task || S.trialTask)}</span><button class="ghost" data-act="clear-trial">${tr("clear_result")}</button></div>
    <p class="stale-result" data-stale role="status" ${trialStale() ? "" : "hidden"}>${esc(staleText())}</p>
    <p class="sect" data-trial-validity>${trialStale() ? tr("previous_trial_result") : tr("recommended_profile")}</p>
    ${t.profile_id ? `<div class="winner">${swatch(profById(t.profile_id) || { id: t.profile_id })}<strong>${esc(modelLabel)}</strong></div>
      <div class="result-meta row"><span class="pill">Agent${tr("colon")}${esc(t.kind)}</span><span class="pill">${tr("reasoning_effort_2")}${esc(t.effort || tr("default"))}</span><span>${esc(t.profile_label || t.profile_id)} · ${esc(t.profile_id)}</span></div>
      <p class="result-reason">${esc(trialReason(t))}</p>` : `<p class="danger">${esc(t.error || tr("no_available_profiles_2"))}</p>`}
    <p class="hint result-meta">${esc(source)}${a.confidence != null ? tr("confidence_p0", { p0: esc(a.confidence) }) : ""}${a.bumped ? tr("low_confidence_increased_difficulty") : ""}${choices ? ` · ${esc(choices)}` : ""}</p>
    ${a.details && a.details.systemone_error ? `<p class="hint">${tr("decision_model_unavailable_using_keyword_rules")}${esc(a.details.systemone_error)}</p>` : ""}
    <div class="rk">${top.map((r) => { const win = r.profile_id === t.profile_id ? "win" : ""; const p = profById(r.profile_id);
      return `<span class="n ${win}">${p ? swatch(p) : ""}${esc(r.label)}</span><span class="track"><span class="fill ${win}" style="width:${Math.max(2, (r.total / max) * 100).toFixed(1)}%"></span></span><span class="v ${win}">${r.total}</span>`; }).join("")}</div>
    <p class="hint">${tr("result_penalty_help")}</p>
    ${t.profile_id ? `<details class="result-details"><summary>${tr("startup_command")}</summary><label class="hint" for="startup-command">${tr("cli_command_for_manual_startup")}</label><textarea id="startup-command" class="cmdline" rows="2" readonly data-command>${esc(startupCommand(t))}</textarea><button data-act="copy-command">${tr("copy_startup_command")}</button>${t.kind === "droid" && (t.args || []).includes("--settings") ? `<p class="hint">${tr("droid_settings_hint")}</p>` : ""}</details>` : ""}
    <details class="result-details"><summary>${tr("diagnostics_summary")}</summary>${trialDiagnosticsHtml()}</details></div>`;
}

// ---------------------------------------------------------------- dimensions tab
function dimsHtml() {
  const cards = S.policy.dimensions.map((d, i) => {
    const P = `dimensions[${i}]`;
    const body = d.type === "score"
      ? `<label>${tr("levels")}</label><div><textarea rows="${Math.max(3, nLevels(d))}" data-dimlist="${i}" data-path="${P}.levels" spellcheck="false">${esc(d.levels.join("\n"))}</textarea><div class="hint">${tr("levels_help")}</div></div>`
      : `<label>${tr("options")}</label><div><textarea rows="${Math.max(3, Object.keys(d.options).length)}" data-dimlist="${i}" data-path="${P}.options" spellcheck="false">${esc(Object.entries(d.options).map(([k, v]) => `${k}: ${v}`).join("\n"))}</textarea><div class="hint">${tr("options_help")}</div></div>`;
    return `<div class="card dimcard" id="dim-${i}">
      <div class="row" style="justify-content:space-between"><h3>${esc(d.label || d.id)} <span class="muted">· ${d.type === "score" ? tr("score") : tr("choice")}</span></h3>
        <span class="row"><button class="ghost" data-dim-move="${i}" data-dir="-1" ${i === 0 ? "disabled" : ""}>↑</button><button class="ghost" data-dim-move="${i}" data-dir="1" ${i === S.policy.dimensions.length - 1 ? "disabled" : ""}>↓</button><button class="danger" data-dim-del="${i}">${tr("delete")}</button></span></div>
      <div class="grid2">
        <label>ID</label><input data-dim="${i}" data-df="id" data-path="${P}.id" value="${esc(d.id)}" spellcheck="false">
        <label>${tr("name")}</label><input data-dim="${i}" data-df="label" data-path="${P}.label" value="${esc(d.label || "")}">
        <label>${tr("type")}</label><select data-dim="${i}" data-df="type" data-path="${P}.type"><option value="score" ${d.type === "score" ? "selected" : ""}>${tr("score_ordered_levels")}</option><option value="choice" ${d.type === "choice" ? "selected" : ""}>${tr("choice_categories")}</option></select>
        <label>${tr("weight")}</label><span class="row"><input type="number" step="0.1" min="0" data-dim="${i}" data-df="weight" data-path="${P}.weight" value="${d.weight ?? 1}"><span class="hint">${tr("multiplies_this_dimension_s_penalty")}</span></span>
        <label>${tr("question")}</label><textarea rows="2" data-dim="${i}" data-df="instructions" data-path="${P}.instructions">${esc(d.instructions || "")}</textarea>
        ${body}
      </div></div>`;
  }).join("");
  return `<p class="hint">${tr("dimensions_help")}</p>${cards}
    <div class="row" style="margin-top:12px"><button data-act="new-dim-score">${tr("add_score_dimension")}</button><button data-act="new-dim-choice">${tr("add_choice_dimension")}</button></div>`;
}
function renameDim(d, newId) {
  newId = newId.trim();
  if (!newId || newId === d.id) return;
  if (dimById(newId)) { toast(tr("dimension_p0_already_exists", { p0: newId })); render(); return; }
  const old = d.id; d.id = newId;
  for (const p of profiles()) {
    if (p.point && old in p.point) { p.point[newId] = p.point[old]; delete p.point[old]; }
    if (p.affinity && old in p.affinity) { p.affinity[newId] = p.affinity[old]; delete p.affinity[old]; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && old in v) { v[newId] = v[old]; delete v[old]; }
  if (S.axes.x === old) S.axes.x = newId;
  if (S.axes.y === old) S.axes.y = newId;
  if (old in S.choice) { S.choice[newId] = S.choice[old]; delete S.choice[old]; }
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
  if (lines.length < 2) { toast(tr("score_dimensions_need_at_least_2_levels")); return false; }
  d.levels = lines;
  const hi = lines.length - 1; let n = 0;
  for (const p of profiles()) {
    if (getPoint(p, d) > hi) { setPoint(p, d, hi); n++; }
  }
  for (const v of Object.values(S.policy.aliases || {})) if (v && typeof v === "object" && v[d.id] > hi) v[d.id] = hi;
  if (n) toast(tr("levels_reduced", { p0: n }));
  return true;
}
function applyOptions(d, text) {
  const opts = {};
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*([a-z][a-z0-9_-]*)\s*[:：]\s*(.+?)\s*$/);
    if (m) opts[m[1]] = m[2];
  }
  if (Object.keys(opts).length < 2) { toast(tr("options_minimum")); return false; }
  d.options = opts;
  let n = 0;
  for (const p of profiles()) {
    const a = p.affinity && p.affinity[d.id];
    if (a) for (const k of Object.keys(a)) if (k !== "*" && !(k in opts)) { delete a[k]; n++; }
  }
  if (n) toast(tr("affinities_removed", { p0: n }));
  return true;
}

// ---------------------------------------------------------------- trial tab
function trialDiagnosticsHtml() {
  const t = S.trial;
  let result = "";
  if (t) {
    const a = t.assessment || {};
    const dimBlocks = trialDims().map((d) => {
      const dist = (a.dists || {})[d.id];
      if (!dist) return `<div><b>${esc(d.label || d.id)}</b> <span class="muted">${tr("no_data")}</span></div>`;
      const rows = d.type === "score" ? d.levels.map((lv, k) => [`${k} · ${shortLevel(lv)}`, dist[k] || 0, lv]) : Object.keys(d.options).map((o) => [o, dist[o] || 0, d.options[o]]);
      const exp = d.type === "score" && a.expected ? ` <span class="muted">${tr("expected")} ${a.expected[d.id]}</span>` : "";
      return `<div><b>${esc(d.label || d.id)}</b>${exp}<div class="bars">${rows.map(([l, p, full]) => `<span class="lbl" title="${esc(full)}">${esc(l)}</span><span><span class="bar" style="width:${(p * 100).toFixed(1)}%;display:block"></span></span><span class="muted">${(p * 100).toFixed(0)}%</span>`).join("")}</div></div>`;
    }).join("");
    const rank = (t.ranking || []).map((r, k) => `<tr class="${r.profile_id === t.profile_id ? "win" : ""} ${r.available ? "" : "na"}"><td>${k + 1}</td>
      <td><span class="swatch" style="background:${color(r.profile_id)}"></span> ${esc(r.label)}<div class="bk">${esc(r.profile_id)} · ${esc(r.kind)}</div></td>
      <td><b>${esc(r.total)}</b></td><td class="bk">${penaltyStackHtml(r)}${r.available ? "" : `<br>${tr("unavailable")}${esc(r.why_unavailable === "disabled" ? tr("disabled_2") : (r.why_unavailable || tr("cli_not_installed_2")))}`}</td></tr>`).join("");
    const err = a.details && a.details.systemone_error ? `<p class="hint">${tr("decision_model_unavailable_using_keyword_rules")}${esc(a.details.systemone_error)}</p>` : "";
    result = `<div class="split even" style="margin-top:12px"><div class="card stack"><h3>${tr("dimension_assessment")} <span class="muted">${esc(a.source)} ${tr("confidence")} ${esc(a.confidence)}${a.bumped ? tr("low_confidence_difficulty_increased") : ""}</span></h3>${err}${dimBlocks}</div>
      <div class="card"><h3>${tr("profile_ranking")} ${t.profile_id ? `→ ${esc(t.profile_label || t.profile_id)}` : ""}</h3>${t.error ? `<p class="danger">${esc(t.error)}</p>` : ""}
      ${t.args ? `<p class="hint">${tr("launch_arguments_kind")} ${esc(t.kind)}${t.args.length ? " -- " + esc(t.args.join(" ")) : ""}</p>` : ""}
      <p class="hint">${tr("ranking_help")}</p><div class="table-scroll"><table><thead><tr><th>#</th><th>${tr("profiles")}</th><th>${tr("penalty")}</th><th>${tr("breakdown")}</th></tr></thead><tbody>${rank}</tbody></table></div></div></div>`;
  }
  return result;
}
function penaltyStackHtml(r) {
  const parts = Object.entries(r.breakdown || {}), total = Math.max(0.0001, Number(r.total));
  const segments = parts.filter(([, v]) => Number(v) > 0).map(([k, v]) => `<span class="penalty-segment" style="width:${(Number(v) / total * 100).toFixed(2)}%" title="${esc(penaltyLabel(k))} ${esc(penaltyText(v))}"></span>`).join("");
  return `${segments ? `<div class="penalty-stack" aria-hidden="true">${segments}</div>` : ""}<div>${parts.map(([k, v]) => `${esc(penaltyLabel(k))} ${esc(penaltyText(v))}`).join(" · ") || "0"}</div>`;
}
function trialHtml() {
  return `${composerHtml("trial-task")}<div id="trialbox">${trialBoxHtml()}</div>`;
}
function renderTrialState() {
  const box = $("#trialbox"); if (box) box.innerHTML = trialBoxHtml();
  document.querySelectorAll('[data-act="run-trial"]').forEach((b) => { b.disabled = S.trialBusy; b.textContent = S.trialBusy ? tr("assessing") : tr("try_routing"); });
  document.querySelectorAll("[data-choice]").forEach((el) => { el.value = S.choice[el.dataset.choice] || ""; });
  const description = $("#map-slice-description");
  if (description) description.innerHTML = tr("slice_and_layers_help", { p0: sliceDescription() });
  renderStatus(); renderErrors(); markErrors();
  if (S.tab === "chart") { renderChart(); refreshMap(0); }
}
async function runTrial() {
  if (S.trialBusy) return;
  const task = (($("#cmd") || $("#trial-task"))?.value ?? S.trialTask).trim();
  if (!task) { toast(tr("enter_a_task_first")); return; }
  const context = { task, policy: JSON.stringify(S.policy), useBackend: S.useBackend };
  const choiceAtStart = JSON.stringify(S.choice);
  S.trialTask = task; S.trialBusy = true; renderTrialState();
  try {
    const { status, body } = await api("/api/route", { method: "POST", body: JSON.stringify({ task, policy: JSON.parse(context.policy), use_backend: context.useBackend }) });
    if (status === 422) {
      if (context.policy === JSON.stringify(S.policy)) { S.errors = body.errors || []; S.errorPolicy = context.policy; }
      toast(tr("trial_invalid_configuration"));
    }
    else if (status !== 200) toast(body.error || tr("request_failed_p0", { p0: status }));
    else {
      S.trial = body.decision || { assessment: body.assessment, error: body.error, ranking: [] }; S.trialContext = context;
      if (S.errorPolicy === context.policy) { S.errors = []; S.errorPolicy = ""; }
      // show the territory for the task's own type, so the map agrees with the ranking
      const ch = (S.trial.assessment || {}).choices || {};
      if (!trialStale() && choiceAtStart === JSON.stringify(S.choice)) {
        for (const d of dims("choice")) if (ch[d.id]) S.choice[d.id] = ch[d.id]; else delete S.choice[d.id];
      }
    }
  } catch (ex) { if (ex.message !== "unauthorized") toast(tr("trial_failed_p0", { p0: ex.message })); }
  finally { S.trialBusy = false; renderTrialState(); }
}

// ---------------------------------------------------------------- settings + json
function settingsHtml() {
  const sc = S.policy.scoring || {}, be = S.policy.backend || {}, dp = S.policy.dispatch || {};
  const num = (path, v, step, hint) => `<span class="row"><input type="number" step="${step}" min="0" data-set="${path}" data-path="${path}" value="${v ?? ""}">${hint ? `<span class="hint">${hint}</span>` : ""}</span>`;
  const aliases = Object.entries(S.policy.aliases || {});
  const atRows = aliases.filter(([k]) => k.startsWith("@")).map(([k, v]) => `<tr data-path="aliases.${esc(k)}"><td><input value="${esc(k)}" data-alias-key="${esc(k)}" spellcheck="false"></td><td><select data-alias-val="${esc(k)}">${profiles().map((p) => `<option value="${esc(p.id)}" ${p.id === v ? "selected" : ""}>${esc(p.label || p.id)}</option>`).join("")}</select></td><td><button class="ghost danger" data-alias-del="${esc(k)}">${tr("delete")}</button></td></tr>`).join("");
  const bang = Object.fromEntries(aliases.filter(([k]) => k.startsWith("!")));
  return `<div class="card pad"><label for="ui-language">${tr("language")}</label>
    <select id="ui-language"><option value="en" ${window.RouterI18n.language === "en" ? "selected" : ""}>${tr("english")}</option><option value="zh" ${window.RouterI18n.language === "zh" ? "selected" : ""}>${tr("chinese")}</option></select></div>
    <div class="split even"><div class="card"><h3>${tr("scoring")}</h3><div class="grid2">
      <label>${tr("above_capability_penalty")}</label>${num("scoring.under_penalty", sc.under_penalty, 0.1, tr("task_exceeds_profile_capability"))}
      <label>${tr("below_capability_penalty")}</label>${num("scoring.over_penalty", sc.over_penalty, 0.1, tr("task_is_simpler_than_the_profile_capability"))}
      <label>${tr("cost_weight")}</label>${num("scoring.cost_weight", sc.cost_weight, 0.01, tr("penalty_usd_per_task_weight"))}
      ${[...new Set(profiles().map((p) => p.kind))].map((k) => `<label>${tr("cost_multiplier")} ${esc(k)}</label>${num(`scoring.agent_cost.${k}`, (sc.agent_cost || {})[k] ?? 1, 0.05, tr("agent_cost_hint"))}`).join("")}
      <label>${tr("time_weight")}</label>${num("scoring.time_weight", sc.time_weight, 0.01, tr("penalty_minutes_per_task_weight"))}
      <label>${tr("tolerance")}</label>${num("scoring.tolerance", sc.tolerance, 0.05, tr("tolerance_hint"))}
      <label>${tr("minimum_rule_confidence")}</label>${num("scoring.min_confidence", sc.min_confidence, 0.05, tr("confidence_hint"))}
    </div>
    <hr class="sep"><h3>${tr("decision_model")}</h3><div class="grid2">
      <label>${tr("enable")}</label><input type="checkbox" data-set="backend.enabled" ${be.enabled !== false ? "checked" : ""}>
      <label>${tr("url")}</label><input data-set="backend.base_url" data-path="backend.base_url" value="${esc(be.base_url || "")}" spellcheck="false">
      <label>${tr("model")}</label><input data-set="backend.model" value="${esc(be.model || "")}" spellcheck="false">
      <label>${tr("api_key_environment_variable")}</label><input data-set="backend.api_key_env" value="${esc(be.api_key_env || "")}" spellcheck="false">
      <label>${tr("timeout_seconds")}</label>${num("backend.timeout_s", be.timeout_s, 0.5)}
      <label>${tr("include_repository_size")}</label><input type="checkbox" data-set="backend.include_repo_signals" ${be.include_repo_signals ? "checked" : ""}>
    </div>
    <hr class="sep"><h3>${tr("dispatch")}</h3><div class="grid2">
      <label>${tr("split_direction")}</label><select data-set="dispatch.direction">${["auto", "right", "down"].map((d) => `<option ${dp.direction === d ? "selected" : ""}>${d}</option>`).join("")}</select>
      <label>${tr("startup_timeout_ms")}</label>${num("dispatch.agent_start_timeout_ms", dp.agent_start_timeout_ms, 1000)}
    </div></div>
    <div class="card"><h3>${tr("aliases_2")}</h3><p class="hint">${tr("in_a_task_write")} <code>${tr("name_2")}</code> ${tr("to_select_a_profile_directly_or_use")} <code>${tr("profileid")}</code>${tr("full_stop")}</p>
      <div class="table-scroll"><table><tbody>${atRows}</tbody></table></div><button class="ghost" data-act="new-alias">${tr("add_alias")}</button>
      <hr class="sep"><h3>${tr("dimension_overrides")}</h3><p class="hint">${tr("in_a_task_write")} <code>!hard</code> ${tr("to_pin_a_dimension_or_use")} <code>${tr("dimensionid_value")}</code>${tr("full_stop")}</p>
      <textarea id="bang-json" rows="8" data-path="aliases" spellcheck="false">${esc(JSON.stringify(bang, null, 2))}</textarea>
      <button class="ghost" data-act="apply-bang">${tr("apply")}</button></div></div>`;
}
function jsonHtml() {
  return `<div class="card"><h3>${tr("full_configuration_json")}</h3><p class="hint">${tr("json_help")}</p>
    <textarea id="json-text" rows="30" spellcheck="false">${esc(JSON.stringify(S.policy, null, 2))}</textarea>
    <div class="row"><button data-act="apply-json">${tr("apply")}</button></div></div>`;
}
function setPath(obj, path, value) {
  const keys = path.split("."); let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] = o[k] || {};
  o[keys[keys.length - 1]] = value;
}

// ---------------------------------------------------------------- save / revert
async function save() {
  if (S.saveBusy || !S.policy) return;
  const submitted = JSON.stringify(S.policy);
  S.saveBusy = true; if (!drag) H.group = null; renderStatus();
  try {
    const { status, body } = await api("/api/policy", { method: "PUT", body: submitted });
    if (status === 200) {
      S.base = submitted; S.source = "user"; S.path = body.path;
      if (S.errorPolicy === submitted) { S.errors = []; S.errorPolicy = ""; }
      toast(isDirty() ? tr("submitted_saved") : tr("saved_and_active_immediately"));
    } else if (status === 422) {
      if (submitted === JSON.stringify(S.policy)) { S.errors = body.errors || []; S.errorPolicy = submitted; }
      toast(tr("submitted_version_has_p0_errors_not_saved_p1", { p0: (body.errors || []).length, p1: submitted === JSON.stringify(S.policy) ? "" : tr("the_current_draft_has_newer_changes") }));
    } else toast(body.error || tr("save_failed_p0", { p0: status }));
  } catch (ex) { if (ex.message !== "unauthorized") toast(tr("save_failed_p0_2", { p0: ex.message })); }
  finally { S.saveBusy = false; renderStatus(); renderErrors(); markErrors(); }
}


// ---------------------------------------------------------------- events
document.addEventListener("click", async (e) => {
  const t = e.target;
  const tab = t.closest("#tabs button, [data-tab-go]");
  if (tab) { S.tab = tab.dataset.tab || tab.dataset.tabGo; render(); window.scrollTo(0, 0); return; }
  if (t.id === "btn-save") return save();
  if (t.id === "btn-undo") { historyStep(); return; }
  if (t.id === "btn-redo") { historyStep(true); return; }
  if (t.id === "btn-revert") {
    if (!S.saveBusy && isDirty() && confirm(tr("confirm_discard"))) {
      S.policy = JSON.parse(S.base); fixAxes(); fixChoice(); if (!selProfile()) S.sel = null;
      changed({ all: true });
    }
    return;
  }
  if (t.id === "btn-defaults") {
    const before = JSON.stringify(S.policy);
    try {
      const defaults = S.defaults || (await api("/api/policy/default")).body.policy;
      if (before !== JSON.stringify(S.policy)) { toast(tr("defaults_draft_changed")); return; }
      if (!defaults) { toast(tr("could_not_load_defaults")); return; }
      S.policy = clone(defaults); fixAxes(); fixChoice(); S.sel = null; toast(tr("built_in_defaults_loaded_save_to_activate")); changed({ all: true });
    } catch (ex) { toast(tr("failed_to_load_defaults_p0", { p0: ex.message })); }
    return;
  }
  const err = t.closest("[data-err]");
  if (err) {
    const path = S.errors[+err.dataset.err].path;
    let m;
    if ((m = path.match(/^profiles\[(\d+)\]/))) { S.tab = "chart"; S.sel = (profiles()[+m[1]] || {}).id || null; }
    else if ((m = path.match(/^dimensions\[(\d+)\]/))) { S.tab = "dims"; render(); $(`#dim-${m[1]}`)?.scrollIntoView({ block: "start" }); return; }
    else if (/^(aliases|scoring|backend|dispatch)/.test(path)) S.tab = "settings";
    render();
    const field = document.querySelector(`[data-path="${CSS.escape(path)}"]`);
    field?.closest("details")?.setAttribute("open", ""); field?.focus(); field?.scrollIntoView({ block: "center" }); return;
  }
  const pick = t.closest("[data-pick]");
  if (pick && !t.matches("input")) { S.sel = pick.dataset.pick; renderChart(); renderEditor(); return; }
  const sample = t.closest("[data-sample]");
  if (sample) { S.trialTask = sample.dataset.sample; const ta = $("#cmd") || $("#trial-task"); if (ta) { ta.value = S.trialTask; ta.focus(); } updateTrialValidity(); return; }
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
    if (S.policy.dimensions.length <= 1) { toast(tr("keep_at_least_one_dimension")); return; }
    if (d.type === "score" && dims("score").length <= 1) { toast(tr("keep_at_least_one_score_dimension")); return; }
    if (!confirm(tr("confirm_delete_dimension", { p0: d.label || d.id }))) return;
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
    profiles().push({ id, label: tr("new_profile"), kind: "claude", cost: 1, enabled: true, point });
    S.sel = id; changed({ all: true });
  } else if (act === "dup-profile" && p) {
    const c = clone(p); c.id = uniqueId(`${p.id}-copy`, profiles().map((q) => q.id)); c.label = tr("p0_copy", { p0: p.label || p.id });
    profiles().splice(profIndex(p.id) + 1, 0, c); S.sel = c.id; changed({ all: true });
  } else if (act === "del-profile" && p) {
    const refs = Object.entries(S.policy.aliases || {}).filter(([, v]) => v === p.id).map(([k]) => k);
    if (!confirm(tr("delete_profile_p0_p1", { p0: p.label || p.id, p1: refs.length ? tr("aliases_p0_will_also_be_deleted", { p0: refs.join(", ") }) : "" }))) return;
    profiles().splice(profIndex(p.id), 1); refs.forEach((k) => delete S.policy.aliases[k]);
    S.sel = null; changed({ all: true });
  } else if (act === "merge-defaults") mergeDefaults();
  else if (act === "back") { S.sel = null; renderChart(); renderEditor(); }
  else if (act === "new-dim-score" || act === "new-dim-choice") {
    const id = uniqueId("dim", S.policy.dimensions.map((d) => d.id));
    S.policy.dimensions.push(act === "new-dim-score"
      ? { id, label: tr("new_dimension"), type: "score", weight: 1, instructions: "How … is this task?", levels: ["Low", "Medium", "High"] }
      : { id, label: tr("new_dimension"), type: "choice", weight: 1, instructions: "Which … fits this task?", options: { a: "Option A", b: "Option B" } });
    changed({ all: true }); setTimeout(() => $(`#dim-${S.policy.dimensions.length - 1}`)?.scrollIntoView(), 0);
  } else if (act === "run-trial") runTrial();
  else if (act === "clear-trial") { S.trial = null; S.trialContext = null; renderTrialState(); }
  else if (act === "copy-command") copyCommand();
  else if (act === "new-alias") {
    const k = uniqueId("@alias", Object.keys(S.policy.aliases || {}));
    S.policy.aliases = S.policy.aliases || {}; S.policy.aliases[k] = profiles()[0].id; changed({ all: true });
  } else if (act === "apply-bang") {
    try {
      const v = JSON.parse($("#bang-json").value);
      for (const k of Object.keys(S.policy.aliases || {})) if (k.startsWith("!")) delete S.policy.aliases[k];
      for (const [k, val] of Object.entries(v)) S.policy.aliases[k.startsWith("!") ? k : `!${k}`] = val;
      toast(tr("applied")); changed({ all: true });
    } catch (ex) { toast(tr("invalid_json_p0", { p0: ex.message })); }
  } else if (act === "apply-json") {
    try {
      const next = JSON.parse($("#json-text").value);
      if (!next || !Array.isArray(next.profiles) || !Array.isArray(next.dimensions)
          || next.dimensions.some((d) => !d || !["score", "choice"].includes(d.type) || (d.type === "score" ? !Array.isArray(d.levels) || d.levels.length < 2 : !d.options || typeof d.options !== "object" || Array.isArray(d.options)))
          || !next.dimensions.some((d) => d.type === "score") || next.profiles.some((p) => !p || typeof p !== "object" || Array.isArray(p))) {
        toast(tr("invalid_configuration")); return;
      }
      S.policy = next; fixAxes(); fixChoice(); if (S.sel && profIndex(S.sel) < 0) S.sel = null; toast(tr("applied_to_editor_remember_to_save")); changed({ all: true });
    }
    catch (ex) { toast(tr("invalid_json_p0", { p0: ex.message })); }
  }
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.id === "profile-search") { S.search = t.value; updateList(); return; }
  if (t.dataset.f && !["id", "kind", "enabled"].includes(t.dataset.f)) {
    const p = selProfile(); if (!p) return;
    setProfileField(p, t.dataset.f, t);
    changed({ chart: t.dataset.f === "label", group: t, defer: true });
  } else if (t.dataset.dim && ["label", "weight", "instructions"].includes(t.dataset.df)) {
    const d = S.policy.dimensions[+t.dataset.dim];
    d[t.dataset.df] = t.dataset.df === "weight" ? Number(t.value) : t.value;
    changed({ group: t, defer: true });
  } else if (t.dataset.set && t.type !== "checkbox" && t.tagName !== "SELECT") {
    setPath(S.policy, t.dataset.set, t.type === "number" ? (t.value === "" ? undefined : Number(t.value)) : t.value);
    changed({ group: t, defer: true });
  } else if (t.dataset.pt) {
    const p = selProfile(), d = dimById(t.dataset.pt); if (!p || !d) return;
    if (t.value === "" || !Number.isFinite(Number(t.value))) return;
    setPoint(p, d, Number(t.value));
    const row = t.closest(".pt");
    if (row) {
      row.querySelectorAll("[data-pt]").forEach((el) => { if (el !== t) el.value = getPoint(p, d); });
      if (row.nextElementSibling) row.nextElementSibling.textContent = shortLevel(d.levels[Math.round(getPoint(p, d))] || tr("coordinate_outside_the_levels"));
    }
    changed({ chart: true, group: t, defer: true });
  } else if (t.id === "trial-task" || t.id === "cmd") { S.trialTask = t.value; updateTrialValidity(); }
});
document.addEventListener("focusout", (e) => { if (H.group === e.target) H.group = null; });

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.id === "ui-language") {
    // Preserve unapplied override JSON when re-rendering the settings page.
    const overrideDraft = $("#bang-json")?.value;
    window.RouterI18n.setLanguage(t.value);
    render();
    if (overrideDraft !== undefined && $("#bang-json")) $("#bang-json").value = overrideDraft;
    $("#toast").hidden = true;
    return;
  }
  if (t.id === "profile-filter") { S.filter = t.value; updateList(); return; }
  if (t.dataset.axis) { S.axes[t.dataset.axis] = t.value; render(); return; }
  if (t.dataset.choice !== undefined) { if (t.value) S.choice[t.dataset.choice] = t.value; else delete S.choice[t.dataset.choice]; render(); return; }
  if (t.dataset.act === "toggle-log") { S.showLog = t.checked; renderChart(); return; }
  if (t.dataset.act === "toggle-disabled") { S.showDisabled = t.checked; renderChart(); return; }
  if (t.dataset.act === "toggle-labels") { S.showLabels = t.checked; renderChart(); return; }
  if (t.dataset.act === "toggle-backend") { S.useBackend = t.checked; updateTrialValidity(); return; }
  if (t.dataset.act === "hide") { const p = selProfile(); if (p) { t.checked ? S.hidden.add(p.id) : S.hidden.delete(p.id); renderChart(); } return; }
  if (t.dataset.enable) { const q = profById(t.dataset.enable); if (q) { q.enabled = t.checked; changed({ chart: true, list: true }); } return; }
  const p = selProfile();
  if (t.dataset.f === "id" && p) { renameProfile(p, t.value); changed({ all: true }); return; }
  if (t.dataset.f === "enabled" && p) { setProfileField(p, "enabled", t); changed({ chart: true }); return; }
  if (t.dataset.f === "kind" && p) { setProfileField(p, "kind", t); changed({ chart: true, editor: true }); return; }
  if (t.dataset.f === "model" && p) { renderEditor(); return; }
  if (t.dataset.ptoff && p) { const d = dimById(t.dataset.ptoff); setPoint(p, d, t.checked ? null : (nLevels(d) - 1) / 2); changed({ chart: true, editor: true }); return; }
  if (t.dataset.pt && p) {
    if (t.value === "") t.value = getPoint(p, dimById(t.dataset.pt)) ?? "";
    H.group = null; return;
  }
  if (t.dataset.dim && t.dataset.df === "id") { renameDim(S.policy.dimensions[+t.dataset.dim], t.value); changed({ all: true }); return; }
  if (t.dataset.dim && t.dataset.df === "type") {
    const d = S.policy.dimensions[+t.dataset.dim];
    if (t.value === d.type) return;
    if (t.value === "score" ? false : dims("score").length <= 1) { toast(tr("keep_at_least_one_score_dimension")); render(); return; }
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
  if (e.isComposing) return;
  const modifier = e.metaKey || e.ctrlKey, key = e.key.toLowerCase();
  if (modifier && key === "s") { e.preventDefault(); save(); return; }
  const editing = e.target.matches("input, textarea, select, [contenteditable]");
  const deferredText = e.target.matches('[data-f="id"], [data-df="id"], [data-dimlist], [data-alias-key]');
  const bound = !deferredText && e.target.matches("[data-f], [data-dim], [data-set], [data-pt], [data-ptoff], [data-enable], [data-alias-val]");
  if (modifier && (key === "z" || key === "y") && (!editing || bound)) {
    e.preventDefault(); historyStep(key === "y" || e.shiftKey); return;
  }
  if (e.key === "Enter" && ["cmd", "trial-task"].includes(e.target.id) && (modifier || e.target.tagName === "INPUT")) { e.preventDefault(); runTrial(); return; }
  if (["Enter", " "].includes(e.key) && e.target.dataset.chartPick) {
    e.preventDefault(); S.sel = e.target.dataset.chartPick; renderChart(); renderEditor();
    document.querySelector(`[data-chart-pick="${CSS.escape(S.sel)}"]`)?.focus(); return;
  }
  if (e.key === "Escape" && S.tab === "chart" && S.sel && !e.target.matches("input, textarea, select")) { S.sel = null; renderChart(); renderEditor(); }
});

load().catch((e) => { if (e.message !== "unauthorized") $("#main").innerHTML = `<p class="pad">${tr("failed_to_load")}${esc(e.message)}</p>`; });
