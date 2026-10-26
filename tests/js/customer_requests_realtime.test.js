const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(
  path.join(__dirname, "../../static/js/customer_requests_realtime.js"), "utf8"
);

test("pages without a realtime root retain the delegated Enter shortcut", () => {
  const listeners = {};
  const document = {
    querySelector() { return null; },
    addEventListener(name, callback) { listeners[name] = callback; },
  };
  const localStorage = { getItem() { return null; } };
  const window = { fetch() {} };
  vm.runInNewContext(script, {
    document, localStorage, window,
    setInterval() { throw new Error("polling must not start without a root"); },
  });
  let submitted = 0;
  const form = { dataset: {}, requestSubmit() { submitted += 1; } };
  const composer = {
    value: "Тест",
    matches(selector) { return selector === 'textarea[name="text"]'; },
    closest(selector) { return selector === "[data-reply-form]" ? form : null; },
  };
  const event = {
    key: "Enter", target: composer, shiftKey: false, isComposing: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  listeners.keydown(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(submitted, 1);
});

test("pages with a realtime root initialize polling from its cursor", async () => {
  const root = { dataset: { cursor: "7", endpoint: "/customer-requests/events/" } };
  const document = {
    hidden: false,
    querySelector(selector) { return selector === "[data-workspace-realtime]" ? root : null; },
    addEventListener() {},
  };
  const requests = [];
  let poll;
  vm.runInNewContext(script, {
    document,
    localStorage: { getItem() { return null; } },
    window: { fetch() {} },
    fetch: async (url) => { requests.push(url); return { ok: true, json: async () => ({ events: [] }) }; },
    setInterval(callback) { poll = callback; },
  });
  await poll();
  assert.deepEqual(requests, ["/customer-requests/events/?after=7"]);
});
