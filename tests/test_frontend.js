"use strict";

// Run: node tests/test_frontend.js
// Each case runs the complete, unmodified app.js in a fresh VM. Events enter
// through its real document listeners; fetch replies and timers are controlled.
// This small DOM supports only the markup/selectors the app uses. Canvas and
// SVG transforms are inert/identity: these are state tests, not visual tests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

const root = path.resolve(__dirname, "..");
const appFile = path.join(root, "web/app.js");
const htmlFile = path.join(root, "web/index.html");
const source = fs.readFileSync(appFile, "utf8");
const html = fs.readFileSync(htmlFile, "utf8");
const digest = (s) => crypto.createHash("sha256").update(s).digest("hex").slice(0, 12);
const copy = (value) => JSON.parse(JSON.stringify(value));
const decode = (s) => s.replace(/&(amp|lt|gt|quot|#39);/g,
  (_, key) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[key]);
const voidTags = new Set(["INPUT", "META", "LINK", "BR", "HR", "IMG"]);

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.attributes = {}; this.dataset = {};
    this.children = []; this.parentElement = null; this.listeners = new Map();
    this.value = ""; this.checked = false; this.disabled = false; this.hidden = false;
    this.clientWidth = 760; this._text = ""; this._html = "";
    this.classList = {
      contains: (c) => this.className.split(/\s+/).includes(c),
      add: (c) => { if (!this.classList.contains(c)) this.className = `${this.className} ${c}`.trim(); },
      remove: (c) => { this.className = this.className.split(/\s+/).filter((x) => x !== c).join(" "); },
      toggle: (c, on) => { const add = on ?? !this.classList.contains(c); this.classList[add ? "add" : "remove"](c); return add; },
    };
  }
  get id() { return this.attributes.id || ""; }
  set id(v) { this.attributes.id = v; }
  get className() { return this.attributes.class || ""; }
  set className(v) { this.attributes.class = v; }
  get type() { return this.attributes.type || (this.tagName === "INPUT" ? "text" : ""); }
  get isConnected() { return this.tagName === "DOCUMENT" || !!this.parentElement?.isConnected; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.clear(); this._text = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(markup) {
    this.clear(); this._html = markup;
    const stack = [this];
    for (const token of markup.match(/<\/?[\w:-]+\b[^>]*>|[^<]+/g) || []) {
      if (token.startsWith("</")) { if (stack.length > 1) stack.pop(); continue; }
      if (!token.startsWith("<")) { stack.at(-1)._text += decode(token); continue; }
      const tag = token.match(/^<([\w:-]+)/)[1];
      const node = new Element(tag);
      for (const a of token.slice(tag.length + 1).matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
        node.setAttribute(a[1], decode(a[2] ?? a[3] ?? a[4] ?? ""));
      }
      stack.at(-1).appendChild(node);
      if (!voidTags.has(node.tagName) && !token.endsWith("/>")) stack.push(node);
    }
    for (const node of this.descendants()) {
      if (node.tagName === "TEXTAREA") node.value = node.textContent;
      if (node.tagName === "SELECT") {
        const options = node.querySelectorAll("option");
        const selected = options.find((o) => o.getAttribute("selected") !== null) || options[0];
        node.value = selected ? selected.getAttribute("value") ?? selected.textContent : "";
      }
    }
  }
  clear() { for (const c of this.children) c.parentElement = null; this.children = []; this._text = ""; }
  appendChild(node) { node.parentElement = this; this.children.push(node); return node; }
  before(node) {
    assert.ok(this.parentElement, "before() requires a connected parent");
    node.parentElement = this.parentElement;
    this.parentElement.children.splice(this.parentElement.children.indexOf(this), 0, node);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    if (name === "value") this.value = String(value);
    if (["checked", "disabled", "hidden"].includes(name)) this[name] = true;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  *descendants() { for (const c of this.children) { yield c; yield* c.descendants(); } }
  matches(selector) {
    return selector.split(/,\s*/).some((branch) => {
      const parts = branch.trim().split(/\s+/);
      let node = this;
      if (!node.simpleMatch(parts.pop())) return false;
      while (parts.length) {
        const parent = parts.pop(); node = node.parentElement;
        while (node && !node.simpleMatch(parent)) node = node.parentElement;
        if (!node) return false;
      }
      return true;
    });
  }
  simpleMatch(selector) {
    const attrs = [...selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
    const base = selector.replace(/\[([\w-]+)(?:="([^"]*)")?\]/g, "");
    const tag = base.match(/^[\w-]+/), id = base.match(/#([\w-]+)/);
    return (!tag || tag[0].toUpperCase() === this.tagName)
      && (!id || this.id === id[1])
      && [...base.matchAll(/\.([\w-]+)/g)].every((m) => this.classList.contains(m[1]))
      && attrs.every((m) => this.getAttribute(m[1]) !== null && (m[2] === undefined || this.getAttribute(m[1]) === m[2]));
  }
  querySelectorAll(selector) { return [...this.descendants()].filter((n) => n.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let n = this; n; n = n.parentElement) if (n.matches(selector)) return n; return null; }
  contains(node) { for (let n = node; n; n = n.parentElement) if (n === this) return true; return false; }
  addEventListener(type, fn) { const list = this.listeners.get(type) || []; list.push(fn); this.listeners.set(type, list); }
  focus() { let doc = this; while (doc.parentElement) doc = doc.parentElement; doc.activeElement = this; }
  scrollIntoView() {}
  setPointerCapture() {}
  createSVGPoint() { return { x: 0, y: 0, matrixTransform() { return { x: this.x, y: this.y }; } }; }
  getScreenCTM() { return { inverse: () => ({}) }; }
  getContext() { return { fillRect() {}, drawImage() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} }; }
  toDataURL() { return "data:image/png;base64,stub"; }
}

const fixture = () => ({
  version: 3,
  dimensions: [
    { id: "duration", label: "Duration", type: "score", weight: 1, levels: ["Short", "Medium", "Long"] },
    { id: "difficulty", label: "Difficulty", type: "score", weight: 1, levels: ["Easy", "Medium", "Hard"] },
    { id: "task_type", label: "Task type", type: "choice", weight: 2, options: { implement: "Build", review: "Review" } },
  ],
  profiles: [
    { id: "alpha", label: "Alpha", kind: "codex", model: "model-alpha", enabled: true, point: { duration: 0.5, difficulty: 0.5 }, cost: 1 },
    { id: "beta", label: "Beta", kind: "claude", model: "model-beta", enabled: false, point: { duration: 1, difficulty: 1 }, note: "Archived reviewer" },
    { id: "gamma", label: "Gamma", kind: "kimi", model: "model-gamma", enabled: true, point: { duration: 2, difficulty: 2 } },
    { id: "delta", label: "Delta", kind: "codex", model: "model-delta", enabled: true, point: { difficulty: 1.5 }, note: "Quiet specialist" },
  ],
  aliases: { "@alpha": "alpha" }, models: {}, scoring: { cost_weight: 0.1 },
  backend: { enabled: true }, dispatch: {},
});
const mapReply = () => ({ x: [-0.5, 2.5], y: [-0.5, 2.5], ids: ["alpha", "delta"], grid: [[0, 0], [0, 0]] });
const trialReply = () => ({ decision: {
  profile_id: "alpha", profile_label: "Alpha", kind: "codex", model: "model-alpha", effort: "medium",
  args: ["-m", "model-alpha"], reason: "lowest penalty",
  assessment: { source: "heuristic", confidence: 0.8, expected: { duration: 1, difficulty: 1 }, choices: { task_type: "review" }, dists: {} },
  ranking: [
    { profile_id: "alpha", label: "Alpha", available: true, total: 1, breakdown: { difficulty: 0.9, cost: 0.1 } },
    { profile_id: "delta", label: "Delta", available: true, total: 2, breakdown: { difficulty: 1.8, cost: 0.2 } },
  ],
} });

async function harness({ manualMap = false, deferDefaults = false, policy = fixture() } = {}) {
  const document = new Element("document"); document.innerHTML = html;
  document.createElement = (tag) => new Element(tag);
  document.activeElement = document.querySelector("body");
  const window = new Element("window"); window.devicePixelRatio = 1; window.scrollTo = () => {};
  const requests = [], timers = new Map(), errors = [];
  let now = 0, timerId = 0;
  const response = (status, body) => ({ status, json: async () => copy(body) });
  const pendingRequest = (url, method, body) => new Promise((resolve, reject) => requests.push({
    url, method, body, done: false,
    reply(status, replyBody) { this.done = true; resolve(response(status, replyBody)); },
    fail() { this.done = true; reject(new Error("simulated offline")); },
  }));
  const fetch = (url, opts = {}) => {
    const method = opts.method || "GET";
    if (method === "GET") {
      if (url === "/api/policy/default" && deferDefaults) return pendingRequest(url, method, null);
      const replies = {
        "/api/policy": { policy, source: "user", path: "/isolated/policy.json", installed: { codex: true, claude: true, kimi: false } },
        "/api/policy/default": { policy: { ...copy(policy), profiles: [policy.profiles[1]] } },
        "/api/log?limit=200": { entries: [] }, "/api/models": { models: {} },
      };
      assert.ok(Object.hasOwn(replies, url), `Unexpected GET ${url}`);
      return Promise.resolve(response(200, replies[url]));
    }
    assert.ok(["/api/map", "/api/route", "/api/policy"].includes(url), `Unexpected request ${method} ${url}`);
    if (url === "/api/map" && !manualMap) return Promise.resolve(response(200, mapReply()));
    return pendingRequest(url, method, JSON.parse(opts.body));
  };
  const context = vm.createContext({
    document, window, fetch, CSS: { escape: (s) => s }, confirm: () => true,
    navigator: { clipboard: { writeText: async () => {} } },
    setTimeout: (fn, ms = 0) => { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout: (id) => timers.delete(id), console,
  });
  const flush = async () => { for (let i = 0; i < 3; i++) await new Promise((resolve) => setImmediate(resolve)); };
  const fire = (type, target, extra = {}) => {
    assert.ok(target, `Missing target for ${type}`);
    const event = { target, button: 0, pointerId: 1, pointerType: "mouse", defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...extra };
    const pending = (document.listeners.get(type) || []).map((fn) => fn(event));
    const completion = Promise.all(pending); completion.catch((e) => errors.push(e));
    completion.event = event;
    return completion;
  };
  const h = {
    document, requests, errors,
    el(selector) { const node = document.querySelector(selector); assert.ok(node, `Missing DOM control: ${selector}`); return node; },
    state(expr = "S.policy") { return copy(vm.runInContext(expr, context)); },
    profile(id = "alpha") { return h.state().profiles.find((p) => p.id === id); },
    request(url) { const req = requests.find((r) => r.url === url && !r.done); assert.ok(req, `No pending ${url} request`); return req; },
    fire, flush,
    async advance(ms = 0) {
      const end = now + ms;
      for (let i = 0; i < 1000; i++) {
        const next = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) { now = end; await flush(); return; }
        now = next[1].at; timers.delete(next[0]);
        const pending = next[1].fn();
        if (pending?.catch) pending.catch((e) => errors.push(e));
        await flush();
      }
      assert.fail("Timer loop did not settle");
    },
    click(selector) { const node = h.el(selector); assert.equal(node.disabled, false, `${selector} is disabled`); return fire("click", node); },
    async input(selector, value) { const node = h.el(selector); node.focus(); node.value = String(value); await fire("input", node); },
    async change(selector, value) {
      const node = h.el(selector);
      if (node.type === "checkbox") node.checked = value; else node.value = String(value);
      await fire("change", node); await fire("focusout", node); document.activeElement = document.querySelector("body");
    },
    async select(id = "alpha") { await h.click(`[data-pick="${id}"]`); },
    async editLabel(value) { await h.input('[data-f="label"]', value); await h.change('[data-f="label"]', value); await h.advance(120); },
    rows() { return h.el("#profile-results").querySelectorAll("[data-pick]").map((n) => n.dataset.pick).sort(); },
    dirty() { return !h.el("#dirty").hidden; },
    async tab(name) { await h.click(`#tabs [data-tab="${name}"]`); },
    async startTrial(task = "Review this change") { await h.input("#cmd, #trial-task", task); await h.click('[data-act="run-trial"]'); },
  };
  vm.runInContext(source, context, { filename: appFile, timeout: 5000 });
  await flush();
  assert.ok(h.state(), "Initial policy failed to load");
  assert.equal(document.querySelector("#main").textContent.includes("加载失败"), false);
  await h.advance(0);
  return h;
}

const cases = [];
const test = (name, fn, options = {}) => cases.push([name, fn, options]);

test("continuous typing is one undo step, redo restores the final text", async (h) => {
  await h.select();
  for (const value of ["A", "Al", "Alpha edited"]) await h.input('[data-f="label"]', value);
  await h.change('[data-f="label"]', "Alpha edited"); await h.advance(120);
  assert.equal(h.dirty(), true);
  await h.click("#btn-undo"); assert.equal(h.profile().label, "Alpha"); assert.equal(h.dirty(), false);
  assert.equal(h.el("#btn-undo").disabled, true, "One typing session must not create per-character history");
  await h.click("#btn-redo"); assert.equal(h.profile().label, "Alpha edited"); assert.equal(h.dirty(), true);
});

test("separate input sessions and a new branch have independent history", async (h) => {
  await h.select(); await h.editLabel("First"); await h.editLabel("Second");
  await h.click("#btn-undo"); assert.equal(h.profile().label, "First");
  await h.input('[data-f="note"]', "A new branch"); await h.change('[data-f="note"]', "A new branch"); await h.advance(120);
  assert.equal(h.el("#btn-redo").disabled, true, "A fresh edit invalidates the redo branch");
  await h.click("#btn-undo"); assert.equal(h.profile().note, undefined); assert.equal(h.profile().label, "First");
  await h.click("#btn-undo"); assert.equal(h.profile().label, "Alpha");
});

test("coordinate slider grouping synchronizes the numeric control", async (h) => {
  await h.select(); const before = h.profile().point;
  for (const value of [0.8, 1.1, 1.4]) await h.input('input[type="range"][data-pt="duration"]', value);
  assert.equal(Number(h.el('input[type="number"][data-pt="duration"]').value), 1.4);
  await h.change('input[type="range"][data-pt="duration"]', 1.4); await h.advance(120);
  await h.click("#btn-undo"); assert.deepEqual(h.profile().point, before);
  assert.equal(h.el("#btn-undo").disabled, true);
  await h.click("#btn-redo"); assert.equal(h.profile().point.duration, 1.4);
});

test("pointer moves form one drag transaction; a second drag is separate", async (h) => {
  const before = h.profile().point;
  const dragOnce = async () => {
    const dot = h.el('#chart [data-id="alpha"][data-drag]'); const circle = dot.querySelector("circle");
    const x = Number(circle.getAttribute("cx")), y = Number(circle.getAttribute("cy"));
    await h.fire("pointerdown", dot, { clientX: x, clientY: y });
    for (const step of [1, 2, 3]) await h.fire("pointermove", h.el("#chart"), { clientX: x + 25 * step, clientY: y - 15 * step });
    await h.fire("pointerup", h.el("#chart"));
  };
  await dragOnce(); const first = h.profile().point; assert.notDeepEqual(first, before);
  await dragOnce(); const second = h.profile().point; assert.notDeepEqual(second, first);
  await h.click("#btn-undo"); assert.deepEqual(h.profile().point, first);
  await h.click("#btn-undo"); assert.deepEqual(h.profile().point, before); assert.equal(h.el("#btn-undo").disabled, true);
  await h.click("#btn-redo"); assert.deepEqual(h.profile().point, first);
  await h.click("#btn-redo"); assert.deepEqual(h.profile().point, second);
});

test("deleting a profile and its aliases is reversible as one action", async (h) => {
  const before = h.state(); await h.select(); await h.click('[data-act="del-profile"]');
  assert.equal(h.profile(), undefined); assert.equal(h.state().aliases["@alpha"], undefined);
  await h.click("#btn-undo"); assert.deepEqual(h.state(), before); assert.equal(h.dirty(), false);
  await h.click("#btn-redo"); assert.equal(h.profile(), undefined);
});

test("applying JSON and loading defaults participate in undo history", async (h) => {
  const before = h.state(); const next = copy(before); next.scoring.cost_weight = 0.75;
  await h.tab("json"); await h.input("#json-text", JSON.stringify(next)); await h.click('[data-act="apply-json"]');
  await h.click("#btn-undo"); assert.deepEqual(h.state(), before);
  await h.click("#btn-redo"); assert.deepEqual(h.state(), next);
  await h.click("#btn-defaults"); assert.equal(h.state().profiles.length, 1);
  await h.click("#btn-undo"); assert.deepEqual(h.state(), next);
});

test("save acknowledges its submitted snapshot, not edits made in flight", async (h) => {
  await h.select(); await h.editLabel("Submitted");
  const saving = h.click("#btn-save"); const request = h.request("/api/policy");
  assert.equal(request.body.profiles[0].label, "Submitted");
  await h.editLabel("Still unsaved");
  await h.fire("keydown", h.el("body"), { key: "s", ctrlKey: true });
  assert.equal(h.requests.filter((r) => r.url === "/api/policy").length, 1, "Duplicate save while busy");
  request.reply(200, { path: "/isolated/policy.json" }); await saving; await h.flush();
  assert.equal(h.profile().label, "Still unsaved"); assert.equal(h.dirty(), true);
  assert.equal(h.state("JSON.parse(S.base)").profiles[0].label, "Submitted");
  await h.click("#btn-undo"); assert.equal(h.profile().label, "Submitted"); assert.equal(h.dirty(), false);
  await h.click("#btn-undo"); assert.equal(h.profile().label, "Alpha"); assert.equal(h.dirty(), true);
  await h.click("#btn-redo"); assert.equal(h.dirty(), false);
  await h.click("#btn-redo"); assert.equal(h.profile().label, "Still unsaved"); assert.equal(h.dirty(), true);
});

for (const failure of ["HTTP", "network", "validation"]) {
  test(`save ${failure} failure releases busy state and permits a corrected retry`, async (h) => {
    await h.select(); await h.editLabel("Draft"); const baseline = h.state("S.base");
    const saving = h.click("#btn-save"); const req = h.request("/api/policy");
    if (failure === "network") req.fail();
    else req.reply(failure === "validation" ? 422 : 500, { error: "Unavailable", errors: [{ path: "profiles[0].label", message: "Invalid label" }] });
    await saving; await h.flush();
    assert.equal(h.el("#btn-save").disabled, false); assert.equal(h.dirty(), true); assert.equal(h.state("S.base"), baseline);
    if (failure === "validation") {
      assert.equal(h.el("#errors").hidden, false); assert.equal(h.el('[data-f="label"]').getAttribute("aria-invalid"), "true");
      await h.editLabel("Corrected"); assert.equal(h.el("#errors").hidden, true);
      assert.equal(h.el('[data-f="label"]').getAttribute("aria-invalid"), null);
    }
    const retry = h.click("#btn-save"); h.request("/api/policy").reply(200, { path: "/isolated/policy.json" });
    await retry; await h.flush(); assert.equal(h.dirty(), false);
  });
}

test("late validation errors cannot mark a newer draft invalid", async (h) => {
  await h.select(); await h.editLabel("Rejected snapshot"); const saving = h.click("#btn-save");
  const req = h.request("/api/policy"); await h.editLabel("Corrected while pending");
  req.reply(422, { errors: [{ path: "profiles[0].label", message: "Snapshot invalid" }] });
  await saving; await h.flush(); assert.equal(h.profile().label, "Corrected while pending");
  assert.equal(h.el("#errors").hidden, true); assert.equal(h.el('[data-f="label"]').getAttribute("aria-invalid"), null);
  assert.equal(h.dirty(), true); assert.equal(h.el("#btn-save").disabled, false);
});

test("discard returns to the saved baseline and undo can recover the discarded draft", async (h) => {
  const before = h.state(); await h.select(); await h.editLabel("Discard this");
  await h.click("#btn-revert"); await h.flush(); assert.deepEqual(h.state(), before);
  assert.equal(h.dirty(), false);
  await h.click("#btn-undo"); assert.equal(h.profile().label, "Discard this"); assert.equal(h.dirty(), true);
  await h.click("#btn-redo"); assert.deepEqual(h.state(), before); assert.equal(h.dirty(), false);
});

test("trial uses the draft snapshot; later edits mark its result stale", async (h) => {
  await h.select(); await h.editLabel("Trial snapshot"); await h.startTrial();
  const req = h.request("/api/route"); assert.equal(req.body.policy.profiles[0].label, "Trial snapshot");
  await h.editLabel("Newer draft"); req.reply(200, trialReply()); await h.flush();
  assert.equal(h.state("S.trialBusy"), false); assert.equal(h.profile().label, "Newer draft");
  assert.equal(h.el("[data-stale]").hidden, false, "An old response must be labeled stale immediately");
  assert.match(h.el("[data-stale]").textContent, /草稿/);
  await h.click("#btn-undo"); assert.equal(h.el("[data-stale]").hidden, true, "Restoring the trial snapshot restores validity");
});

test("changing task or backend invalidates an existing trial without editing policy", async (h) => {
  await h.startTrial(); h.request("/api/route").reply(200, trialReply()); await h.flush();
  assert.equal(h.el("[data-stale]").hidden, true);
  await h.input("#cmd", "A different task"); await h.advance(120);
  assert.equal(h.el("[data-stale]").hidden, false);
  await h.input("#cmd", "Review this change"); await h.advance(120);
  assert.equal(h.el("[data-stale]").hidden, true);
  await h.change('[data-act="toggle-backend"]', false);
  assert.equal(h.el("[data-stale]").hidden, false); assert.equal(h.dirty(), false);
});

for (const failure of ["HTTP", "network"]) {
  test(`trial ${failure} failure can be retried`, async (h) => {
    await h.startTrial(); const req = h.request("/api/route");
    if (failure === "network") req.fail(); else req.reply(500, { error: "Try later" });
    await h.flush(); assert.equal(h.state("S.trialBusy"), false);
    assert.equal(h.el('[data-act="run-trial"]').disabled, false);
    await h.startTrial(); h.request("/api/route").reply(200, trialReply()); await h.flush();
    assert.equal(h.state("S.trial").profile_id, "alpha"); assert.equal(h.el("[data-stale]").hidden, true);
  });
}

test("search combines with independent enabled, disabled and missing-CLI filters", async (h) => {
  assert.deepEqual(h.rows(), ["alpha", "beta", "delta", "gamma"]);
  await h.input("#profile-search", "  MoDeL-  "); assert.deepEqual(h.rows(), ["alpha", "beta", "delta", "gamma"]);
  await h.change("#profile-filter", "enabled"); assert.deepEqual(h.rows(), ["alpha", "delta", "gamma"]);
  await h.change("#profile-filter", "missing"); assert.deepEqual(h.rows(), ["gamma"]);
  await h.input("#profile-search", "archived"); assert.deepEqual(h.rows(), []);
  await h.change("#profile-filter", "disabled"); assert.deepEqual(h.rows(), ["beta"]);
  await h.input("#profile-search", "codex"); assert.deepEqual(h.rows(), []);
  await h.change("#profile-filter", "enabled"); assert.deepEqual(h.rows(), ["alpha", "delta"]);
  assert.equal(h.dirty(), false); assert.equal(h.el("#btn-undo").disabled, true);
});

test("zero coverage requires a current server map and excludes unavailable profiles", async (h) => {
  await h.change("#profile-filter", "zero"); assert.deepEqual(h.rows(), []);
  h.request("/api/map").reply(200, mapReply()); await h.flush(); assert.deepEqual(h.rows(), ["delta"]);
  await h.change("#profile-filter", "all"); await h.select(); await h.editLabel("Changed slice");
  await h.click('[data-act="back"]'); await h.change("#profile-filter", "zero");
  assert.deepEqual(h.rows(), [], "Stale coverage must not classify profiles as zero");
}, { manualMap: true });

test("hiding is display-only; disabling edits the policy and is reversible", async (h) => {
  await h.select(); const before = h.state();
  await h.change('[data-act="hide"]', true); assert.deepEqual(h.state(), before); assert.equal(h.dirty(), false);
  assert.equal(h.document.querySelector('#chart [data-id="alpha"][data-drag]'), null);
  assert.equal(h.el("#btn-undo").disabled, true);
  await h.change('[data-f="enabled"]', false); assert.equal(h.profile().enabled, false); assert.equal(h.dirty(), true);
  await h.click("#btn-undo"); assert.deepEqual(h.state(), before);
  assert.equal(h.document.querySelector('#chart [data-id="alpha"][data-drag]'), null, "Undo must preserve the display-only hide toggle");
  await h.startTrial(); const req = h.request("/api/route"); assert.equal(req.body.policy.profiles[0].enabled, true);
  req.reply(200, trialReply()); await h.flush();
});

test("chart layer toggles change display without altering routing policy", async (h) => {
  const before = h.state();
  await h.change('[data-act="toggle-disabled"]', false);
  assert.equal(h.document.querySelector('#chart [data-id="beta"][data-drag]'), null);
  await h.change('[data-act="toggle-labels"]', false); assert.equal(h.document.querySelector("#chart .plabel"), null);
  await h.change('[data-act="toggle-log"]', false); assert.deepEqual(h.state(), before); assert.equal(h.dirty(), false);
  await h.change('[data-act="toggle-disabled"]', true); assert.ok(h.el('#chart [data-id="beta"][data-drag]'));
});

test("late map response is discarded and the newest draft is requested", async (h) => {
  const old = h.request("/api/map");
  await h.select(); await h.editLabel("Latest map draft"); await h.advance(150);
  old.reply(200, mapReply()); await h.flush();
  assert.equal(h.state("S.map"), null, "Old coverage must not overwrite the latest draft");
  const latest = h.request("/api/map"); assert.equal(latest.body.policy.profiles[0].label, "Latest map draft");
  latest.reply(200, mapReply()); await h.flush();
  await h.click('[data-act="back"]'); await h.change("#profile-filter", "zero"); assert.deepEqual(h.rows(), ["delta"]);
}, { manualMap: true });

for (const failure of ["HTTP", "network"]) {
  test(`map ${failure} failure allows the same draft to be retried`, async (h) => {
    const req = h.request("/api/map");
    if (failure === "network") req.fail(); else req.reply(500, { error: "Map temporarily unavailable" });
    await h.flush(); await h.tab("settings"); await h.tab("chart"); await h.advance(0);
    const retry = h.request("/api/map"); assert.deepEqual(retry.body, req.body);
    retry.reply(200, mapReply()); await h.flush();
    await h.change("#profile-filter", "zero"); assert.deepEqual(h.rows(), ["delta"]);
  }, { manualMap: true });
}

test("uncommitted ID text keeps native undo and does not undo another draft action", async (h) => {
  await h.select(); await h.editLabel("Committed label");
  await h.input('[data-f="id"]', "Uncommitted ID");
  assert.equal(h.profile().id, "alpha", "ID is only committed on change");
  const undo = h.fire("keydown", h.el('[data-f="id"]'), { key: "z", ctrlKey: true }); await undo;
  assert.equal(undo.event.defaultPrevented, false, "Native text undo must remain available before the ID is committed");
  assert.equal(h.profile().label, "Committed label", "Text undo must not revoke an earlier draft operation");
});

test("late defaults do not replace a focused search control", async (h) => {
  await h.input("#profile-search", "alpha"); const search = h.el("#profile-search");
  h.request("/api/policy/default").reply(200, { policy: fixture() }); await h.flush();
  assert.equal(search.isConnected, true, "Background defaults must not detach the active search field");
  assert.equal(h.document.activeElement, h.el("#profile-search")); assert.deepEqual(h.rows(), ["alpha"]);
}, { deferDefaults: true });

test("a queued newer map survives an in-flight network failure", async (h) => {
  const old = h.request("/api/map"); await h.select(); await h.editLabel("Recover latest"); await h.advance(150);
  old.fail(); await h.flush(); await h.advance(150);
  const latest = h.request("/api/map"); assert.equal(latest.body.policy.profiles[0].label, "Recover latest");
  latest.reply(200, mapReply()); await h.flush(); assert.ok(h.state("S.map"));
}, { manualMap: true });

(async () => {
  let failed = 0, changedDuringRun = false;
  console.log(`app.js SHA-256 ${digest(source)}; isolated VM + DOM/API stubs; no browser or network`);
  for (const [name, fn, options] of cases) {
    try {
      const h = await harness(options); await fn(h); await h.flush();
      assert.deepEqual(h.errors, [], "Unexpected error from an event handler");
      console.log(`PASS ${name}`);
    } catch (error) {
      failed++; console.error(`FAIL ${name}\n  ${error.stack}`);
    }
  }
  if (source !== fs.readFileSync(appFile, "utf8") || html !== fs.readFileSync(htmlFile, "utf8")) {
    changedDuringRun = true; console.error("FAIL front-end files changed during this run; rerun on stable files");
  }
  console.log(`${cases.length - failed}/${cases.length} passed; ${failed} failure(s)`);
  process.exitCode = failed || changedDuringRun ? 1 : 0;
})().catch((error) => { console.error(error); process.exitCode = 1; });
