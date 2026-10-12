const test = require("node:test");
const assert = require("node:assert/strict");
const pickerApi = require("../../static/js/part_name_picker.js");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.classes = new Set();
    this.classList = {
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
    };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) {
    (this.listeners[name] ||= []).push(callback);
  }
  dispatch(name, event = {}) {
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const callback of this.listeners[name] || []) callback(event);
    return event;
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, before) {
    child.parentNode = this;
    const index = before ? this.children.indexOf(before) : -1;
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
  }
  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
    child.parentNode = null;
  }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
  }
  querySelector(selector) { return selector === "[name=part_id]" ? this.partField || null : null; }
}

function harness(fetchJson) {
  const parent = new Element("div");
  const input = new Element("input");
  input.id = "scan";
  input.setAttribute("data-part-picker-url", "/scanner/part-search/");
  input.setAttribute("data-part-picker-form", "pick-form");
  input.setAttribute("data-part-picker-status", "status");
  parent.appendChild(input);

  const form = new Element("form");
  form.partField = new Element("input");
  form.submissions = 0;
  form.requestSubmit = () => { form.submissions += 1; };
  const status = new Element("p");
  const document = {
    createElement: (tag) => new Element(tag),
    getElementById: (id) => ({ "pick-form": form, status }[id] || null),
    querySelectorAll: () => [input],
  };
  const timers = [];
  const pickers = pickerApi.init(document, fetchJson);
  // init uses a document-level selection in production; this fake exposes it
  // through querySelectorAll so it exercises the same duplicate-bind guard.
  return {
    document, form, input, status, picker: pickers[0], timers,
    schedule: (fn) => { timers.push(fn); return timers.length - 1; },
    fireTimer: () => timers.shift()?.(),
  };
}

test("keyboard chooses a concrete suggestion and submits once", async () => {
  const h = harness(async () => ({ results: [{ id: 17, name: "Сальник 25*62*7" }] }));
  h.input.value = "сальник";
  h.input.dispatch("input");
  h.picker.search("сальник");
  await new Promise(setImmediate);
  assert.equal(h.picker.list.children.length, 1);

  h.input.dispatch("keydown", { key: "ArrowDown" });
  const enter = h.input.dispatch("keydown", { key: "Enter" });
  assert.equal(enter.defaultPrevented, true);
  assert.equal(h.form.partField.value, "17");
  assert.equal(h.form.submissions, 1);
});

test("mouse or touch selection prevents blur and submits the selected row", async () => {
  const h = harness(async () => ({ results: [{ id: 23, name: "Seal 25*62*7" }] }));
  h.input.value = "seal";
  h.picker.search("seal");
  await new Promise(setImmediate);
  const event = h.picker.list.children[0].dispatch("pointerdown");
  assert.equal(event.defaultPrevented, true);
  assert.equal(h.form.partField.value, "23");
  assert.equal(h.form.submissions, 1);
});

test("typing a new query closes old choices and ignores stale responses", async () => {
  const pending = {};
  const h = harness((url) => new Promise((resolve) => { pending[url] = resolve; }));

  h.input.value = "seal";
  h.picker.search("seal");
  h.input.value = "bearing";
  h.input.dispatch("input");
  pending["/scanner/part-search/?q=seal"]({
    results: [{ id: 1, name: "Stale seal" }],
  });
  await new Promise(setImmediate);
  assert.equal(h.picker.list.hidden, true);
  assert.equal(h.picker.state.items.length, 0);
});

test("initialization does not bind duplicate handlers", () => {
  let calls = 0;
  const h = harness(async () => { calls += 1; return { results: [] }; });
  const again = pickerApi.init(h.document, async () => ({ results: [] }));
  assert.equal(again.length, 0);
  h.input.value = "seal";
  h.input.dispatch("input");
  assert.equal(h.picker.state.seq, 1);
  assert.equal(calls, 0);
});
