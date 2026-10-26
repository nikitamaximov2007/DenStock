const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(
  path.join(__dirname, "../../static/js/quick_actions_keyboard.js"), "utf8"
);
const names = ["gross_weight_kg", "net_weight_kg", "application_area", "quantity"];

function fixture() {
  const listeners = {};
  const document = {
    activeElement: null,
    addEventListener(name, handler) { listeners[name] = handler; },
    querySelectorAll() { return []; }, // Customer search is tested separately.
  };
  const rows = [];

  function addRow() {
    const row = {
      fields: {},
      nextElementSibling: null,
      previousElementSibling: null,
      matches(selector) { return selector === "[data-cart-nav-row]"; },
      querySelector(selector) {
        const name = selector.match(/data-cart-nav="([^"]+)"/);
        return name ? this.fields[name[1]] || null : null;
      },
    };
    names.forEach((name) => {
      const isSelect = name === "application_area";
      const field = {
        tagName: isSelect ? "SELECT" : "INPUT",
        dataset: { cartNav: name },
        value: isSelect ? "СНЕГОХОД" : "12",
        selectionStart: 2,
        selectionEnd: 2,
        open: false,
        closest(selector) {
          return selector === "[data-cart-nav]" ? this : row;
        },
        matches(selector) {
          if (selector !== ":open") throw new Error(`unexpected selector: ${selector}`);
          return this.open;
        },
        focus() { document.activeElement = this; },
        setSelectionRange(start, end) {
          this.selectionStart = start;
          this.selectionEnd = end;
        },
      };
      row.fields[name] = field;
    });
    if (rows.length) {
      rows[rows.length - 1].nextElementSibling = row;
      row.previousElementSibling = rows[rows.length - 1];
    }
    rows.push(row);
    return row;
  }

  vm.runInNewContext(script, { document });
  function press(field, key, extra = {}) {
    field.focus();
    const event = {
      target: field,
      key,
      defaultPrevented: false,
      isComposing: false,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
      preventDefault() { this.defaultPrevented = true; },
      ...extra,
    };
    listeners.keydown(event);
    return event;
  }
  return { addRow, document, press };
}

test("right moves net weight through closed application select to quantity", () => {
  const ui = fixture();
  const row = ui.addRow();
  const net = row.fields.net_weight_kg;
  const area = row.fields.application_area;
  const quantity = row.fields.quantity;
  assert.equal(ui.press(net, "ArrowRight").defaultPrevented, true);
  assert.equal(ui.document.activeElement, area);
  assert.equal(ui.press(area, "ArrowRight").defaultPrevented, true);
  assert.equal(ui.document.activeElement, quantity);
  assert.equal(area.value, "СНЕГОХОД");
});

test("left moves quantity through closed application select to net weight", () => {
  const ui = fixture();
  const row = ui.addRow();
  const quantity = row.fields.quantity;
  const area = row.fields.application_area;
  quantity.selectionStart = quantity.selectionEnd = 0;
  assert.equal(ui.press(quantity, "ArrowLeft").defaultPrevented, true);
  assert.equal(ui.document.activeElement, area);
  assert.equal(ui.press(area, "ArrowLeft").defaultPrevented, true);
  assert.equal(ui.document.activeElement, row.fields.net_weight_kg);
  assert.equal(area.value, "СНЕГОХОД");
});

test("open select and vertical arrows retain native behavior", () => {
  const ui = fixture();
  const area = ui.addRow().fields.application_area;
  area.open = true;
  for (const key of ["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"]) {
    assert.equal(ui.press(area, key).defaultPrevented, false);
    assert.equal(ui.document.activeElement, area);
  }
  area.open = false;
  assert.equal(ui.press(area, "ArrowDown").defaultPrevented, false);
  assert.equal(area.value, "СНЕГОХОД");
});

test("browser without :open keeps select navigation native", () => {
  const ui = fixture();
  const area = ui.addRow().fields.application_area;
  area.matches = () => { throw new Error("unsupported selector"); };
  assert.equal(ui.press(area, "ArrowRight").defaultPrevented, false);
  assert.equal(ui.document.activeElement, area);
});

test("text editing, Tab and modified shortcuts remain native", () => {
  const ui = fixture();
  const row = ui.addRow();
  const net = row.fields.net_weight_kg;
  net.selectionStart = net.selectionEnd = 1;
  assert.equal(ui.press(net, "ArrowLeft").defaultPrevented, false);
  assert.equal(ui.press(net, "ArrowRight").defaultPrevented, false);
  assert.equal(ui.press(net, "Tab").defaultPrevented, false);
  assert.equal(ui.press(net, "ArrowRight", { ctrlKey: true }).defaultPrevented, false);
});

test("delegated handler includes rows added after initialization", () => {
  const ui = fixture();
  const first = ui.addRow();
  const second = ui.addRow();
  assert.equal(ui.press(first.fields.gross_weight_kg, "ArrowDown").defaultPrevented, true);
  assert.equal(ui.document.activeElement, second.fields.gross_weight_kg);
  assert.equal(ui.press(second.fields.quantity, "ArrowUp").defaultPrevented, true);
  assert.equal(ui.document.activeElement, first.fields.quantity);
  assert.equal(ui.press(second.fields.application_area, "ArrowRight").defaultPrevented, true);
  assert.equal(ui.document.activeElement, second.fields.quantity);
});

test("customer search initializes only containers, including multiple cart panels", async () => {
  const roots = ["sale", "reserve"].map((kind) => {
    const handlers = {};
    const input = {
      dataset: { customerFilter: `customer-select-${kind}`, customerSearchUrl: "/customer-search/" },
      value: "",
      addEventListener(name, handler) { handlers[name] = handler; },
      setAttribute() {},
      removeAttribute() {},
    };
    const list = {
      id: `customer-results-${kind}`,
      hidden: true,
      children: [],
      replaceChildren() { this.children = []; },
      append(item) { this.children.push(item); },
    };
    const root = {
      querySelector(selector) { return selector === "[data-customer-filter]" ? input : list; },
      contains(target) { return target === input; },
    };
    return { root, input, list, handlers };
  });
  const optionWithSameAttribute = { dataset: { customerSearch: "test" } };
  const pending = [];
  let searchResponse = async () => ({ ok: true, json: async () => ({ results: [{ id: 1, label: "Тестовый клиент" }] }) });
  const document = {
    activeElement: null,
    addEventListener() {},
    querySelectorAll(selector) {
      const containers = roots.map(({ root }) => root);
      if (selector === ".customer-search[data-customer-search]") return containers;
      if (selector === "[data-customer-search]") return [...containers, optionWithSameAttribute];
      throw new Error(`unexpected selector: ${selector}`);
    },
    getElementById(id) { return { id, options: [] }; },
    createElement() {
      return {
        setAttribute() {},
        addEventListener() {},
      };
    },
  };
  const window = { location: { href: "http://localhost:8002/inventory/actions/" } };
  vm.runInNewContext(script, {
    document, window, URL, AbortController,
    setTimeout(callback) { pending.push(callback); return pending.length; },
    clearTimeout() {},
    fetch: (url) => searchResponse(url),
  });
  for (const item of roots) {
    item.input.value = "Тест";
    document.activeElement = item.input;
    item.handlers.input();
    await pending.shift()();
    assert.equal(item.list.hidden, false);
    assert.equal(item.list.children[0].textContent, "Тестовый клиент");
  }

  let resolveOld;
  searchResponse = (url) => url.searchParams.get("q") === "Старый"
    ? new Promise((resolve) => { resolveOld = resolve; })
    : Promise.resolve({ ok: true, json: async () => ({ results: [{ id: 2, label: "Новый клиент" }] }) });
  const item = roots[0];
  document.activeElement = item.input;
  item.input.value = "Старый";
  item.handlers.input();
  const oldRequest = pending.shift()();
  item.input.value = "Новый";
  item.handlers.input();
  await pending.shift()();
  resolveOld({ ok: true, json: async () => ({ results: [{ id: 3, label: "Устаревший ответ" }] }) });
  await oldRequest;
  assert.equal(item.list.children[0].textContent, "Новый клиент");
});
