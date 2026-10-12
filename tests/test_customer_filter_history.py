"""История поисковых строк «Найти клиента» в корзине быстрых действий.

Логика MRU (static/js/customer_filter_history.js) проверяется под node без
браузера; разметка и неизменность порядка клиентов проверяются через Django.

Корень дефекта: у поля не было своей истории. Выпадающий список с часами был
нативной историей браузера: она пополняется только при обычной отправке формы
(корзина давно сохраняется автосохранением) и сортируется по частоте, поэтому
в ней навсегда остались первые шесть строк.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.customers.services import customers_by_recent_activity
from tests.test_customer_recency_order import (  # noqa: F401 - фикстуры
    _customer,
    _sale,
    _scan_page_with_a_cart,
    admin,
    env,
    make_user,
)

JS_PATH = Path(__file__).resolve().parents[1] / "static" / "js" / "customer_filter_history.js"

HARNESS = r"""
const H = require(process.argv[1]);

function memoryStorage(initial) {
  const data = Object.assign({}, initial || {});
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
  };
}

// Минимальный DOM: поле поиска, datalist истории и список клиентов.
function makeDom(scope) {
  const elements = {};
  function element(tag, attrs) {
    const node = {
      tagName: tag, attrs: Object.assign({}, attrs || {}), listeners: {}, children: [], value: "",
      getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      dispatch(type, event) {
        const ev = Object.assign({ type, defaultPrevented: false,
          preventDefault() { this.defaultPrevented = true; } }, event || {});
        (this.listeners[type] || []).forEach((fn) => fn(ev));
        return ev;
      },
      appendChild(child) { this.children.push(child); return child; },
      removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
      get firstChild() { return this.children[0] || null; },
    };
    if (attrs && attrs.id) elements[attrs.id] = node;
    return node;
  }
  const input = element("input", {
    id: "customer-filter-sale", list: "customer-filter-history-sale",
    "data-customer-filter": "customer-select-sale", "data-customer-filter-history": scope,
  });
  const list = element("datalist", { id: "customer-filter-history-sale" });
  const select = element("select", { id: "customer-select-sale" });
  const doc = {
    getElementById: (id) => elements[id] || null,
    createElement: (tag) => element(tag),
    querySelectorAll: () => [input],
  };
  return { doc, input, list, select,
           shown: () => list.children.map((o) => o.value) };
}

function type(dom, text) {
  // Набор по буквам: input на каждую букву, как в браузере.
  dom.input.value = "";
  for (const ch of text) {
    dom.input.value += ch;
    dom.input.dispatch("input", { inputType: "insertText" });
  }
}
function enter(dom) { return dom.input.dispatch("keydown", { key: "Enter" }); }
function search(dom, text) { type(dom, text); enter(dom); }

const OLD_SIX = ["денис", "пя", "петр", "анна", "ш", "яру"];

const scenarios = {
  emptyHistory() {
    const dom = makeDom("7");
    H.init(dom.doc, memoryStorage());
    return { shown: dom.shown() };
  },
  firstQuery() {
    const dom = makeDom("7"); const storage = memoryStorage();
    H.init(dom.doc, storage); search(dom, "иван");
    return { shown: dom.shown() };
  },
  customerOptionsFilterWithSearch() {
    const dom = makeDom("7");
    dom.select.options = [
      { hidden: false, dataset: { customerSearch: "" } },
      { hidden: false, dataset: { customerSearch: "иванов +7 912" } },
      { hidden: false, dataset: { customerSearch: "сидоров +7 495" } },
    ];
    H.init(dom.doc, memoryStorage()); type(dom, "иван");
    return { hidden: dom.select.options.map((option) => option.hidden) };
  },
  ownerExample() {
    const dom = makeDom("7"); const storage = memoryStorage();
    H.init(dom.doc, storage);
    // Шесть запросов, введённых по порядку от старого к новому.
    OLD_SIX.slice().reverse().forEach((q) => search(dom, q));
    const six = dom.shown();
    search(dom, "иван");
    const afterIvan = dom.shown();
    search(dom, "петр");
    const afterPetr = dom.shown();
    return { six, afterIvan, afterPetr };
  },
  keystrokesAloneRecordNothing() {
    const dom = makeDom("7"); const storage = memoryStorage();
    H.init(dom.doc, storage); type(dom, "иванов");
    return { shown: dom.shown(), stored: storage.data };
  },
  enterNeverSubmitsTheSaleForm() {
    const dom = makeDom("7"); H.init(dom.doc, memoryStorage());
    type(dom, "иван"); const ev = enter(dom);
    type(dom, ""); const empty = enter(dom);
    return {
      prevented: ev.defaultPrevented, emptyPrevented: empty.defaultPrevented, shown: dom.shown(),
    };
  },
  changeAndClientChoiceRecord() {
    const dom = makeDom("7"); H.init(dom.doc, memoryStorage());
    type(dom, "анна"); dom.input.dispatch("change");
    type(dom, "+7 912"); dom.select.dispatch("change");
    return { shown: dom.shown() };
  },
  pickingFromHistoryMovesItUp() {
    const dom = makeDom("7"); H.init(dom.doc, memoryStorage());
    ["a", "b", "c"].forEach((q) => search(dom, q));
    dom.input.value = "a"; dom.input.dispatch("input", { inputType: "insertReplacementText" });
    const afterPick = dom.shown();
    dom.input.value = "b"; dom.input.dispatch("input", {});  // Safari: без inputType
    return { afterPick, afterSafariPick: dom.shown() };
  },
  caseAndSpacesDoNotDuplicate() {
    const dom = makeDom("7"); H.init(dom.doc, memoryStorage());
    search(dom, "Иван  Петров "); search(dom, "иван петров"); search(dom, "   ");
    return { shown: dom.shown() };
  },
  survivesReload() {
    const storage = memoryStorage();
    const first = makeDom("7"); H.init(first.doc, storage);
    ["1", "2", "3"].forEach((q) => search(first, q));
    const reloaded = makeDom("7"); H.init(reloaded.doc, storage);
    return { shown: reloaded.shown() };
  },
  rapidSearchesKeepOrder() {
    const dom = makeDom("7"); H.init(dom.doc, memoryStorage());
    for (let i = 1; i <= 20; i++) search(dom, "q" + i);
    return { shown: dom.shown() };
  },
  separateUsersDoNotSeeEachOther() {
    const storage = memoryStorage();
    const a = makeDom("7"); H.init(a.doc, storage); search(a, "секрет");
    const b = makeDom("8"); H.init(b.doc, storage);
    return { other: b.shown(), keys: Object.keys(storage.data) };
  },
  damagedStorageIsTolerated() {
    const broken = makeDom("7");
    H.init(broken.doc, memoryStorage({ "denstock:customer-filter-history:7": "{не json" }));
    const throwing = makeDom("7");
    const denied = () => { throw new Error("denied"); };
    H.init(throwing.doc, { getItem: denied, setItem: denied });
    search(throwing, "иван");
    const mixed = makeDom("7");
    H.init(mixed.doc, memoryStorage({ "denstock:customer-filter-history:7":
      JSON.stringify(["a", 5, "A", " b ", "", "c", "d", "e", "f", "g"]) }));
    return { broken: broken.shown(), throwing: throwing.shown(), mixed: mixed.shown() };
  },
};

const name = process.argv[2];
Promise.resolve(scenarios[name]()).then((r) => process.stdout.write(JSON.stringify(r)));
"""


def _run(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен")
    result = subprocess.run(
        [node, "-e", HARNESS, str(JS_PATH), scenario],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_history_starts_empty():
    assert _run("emptyHistory") == {"shown": []}


def test_first_search_is_shown():
    assert _run("firstQuery") == {"shown": ["иван"]}


def test_customer_search_keeps_filtering_the_existing_sorted_options():
    assert _run("customerOptionsFilterWithSearch") == {"hidden": [False, False, True]}


def test_owner_example_newest_first_seventh_evicts_oldest_repeat_moves_up():
    result = _run("ownerExample")
    assert result["six"] == ["денис", "пя", "петр", "анна", "ш", "яру"]
    assert result["afterIvan"] == ["иван", "денис", "пя", "петр", "анна", "ш"]
    assert result["afterPetr"] == ["петр", "иван", "денис", "пя", "анна", "ш"]


def test_typing_alone_records_nothing():
    assert _run("keystrokesAloneRecordNothing") == {"shown": [], "stored": {}}


def test_enter_records_and_never_submits_the_sale_form():
    result = _run("enterNeverSubmitsTheSaleForm")
    assert result == {"prevented": True, "emptyPrevented": True, "shown": ["иван"]}


def test_leaving_the_field_or_choosing_a_client_records_the_search():
    assert _run("changeAndClientChoiceRecord") == {"shown": ["+7 912", "анна"]}


def test_picking_a_history_entry_moves_it_to_the_top():
    result = _run("pickingFromHistoryMovesItUp")
    assert result["afterPick"] == ["a", "c", "b"]
    assert result["afterSafariPick"] == ["b", "a", "c"]


def test_case_and_spaces_do_not_create_duplicates():
    assert _run("caseAndSpacesDoNotDuplicate") == {"shown": ["иван петров"]}


def test_history_survives_a_reload():
    assert _run("survivesReload") == {"shown": ["3", "2", "1"]}


def test_rapid_searches_keep_exactly_six_newest_first():
    assert _run("rapidSearchesKeepOrder") == {"shown": ["q20", "q19", "q18", "q17", "q16", "q15"]}


def test_history_is_separate_per_user():
    result = _run("separateUsersDoNotSeeEachOther")
    assert result["other"] == []
    assert result["keys"] == ["denstock:customer-filter-history:7"]


def test_damaged_or_unavailable_storage_is_tolerated():
    result = _run("damagedStorageIsTolerated")
    assert result["broken"] == []
    assert result["throwing"] == ["иван"]  # показано, хотя не сохранилось
    assert result["mixed"] == ["a", "b", "c", "d", "e", "f"]


def test_partial_navigation_binds_replaced_field_once():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node не установлен")
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const store = {};
const storage = {
  getItem: (key) => (key in store ? store[key] : null),
  setItem: (key, value) => { store[key] = String(value); },
};
let current = [];
const byId = {};
const listeners = {};
function element(attrs) {
  return {
    attrs, value: "", handlers: {}, children: [],
    getAttribute(name) { return this.attrs[name] || null; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = {}) {
      (this.handlers[type] || []).forEach((fn) => fn({ preventDefault() {}, ...event }));
    },
    appendChild(child) { this.children.push(child); },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
    get firstChild() { return this.children[0] || null; },
  };
}
const document = {
  readyState: "loading",
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  fire(type, event = {}) { (listeners[type] || []).forEach((fn) => fn(event)); },
  querySelectorAll() {
    return current.filter((node) => node.attrs["data-customer-filter-history"]);
  },
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return element({ tag }); },
};
function page() {
  const input = element({ list: "history", "data-customer-filter": "select",
    "data-customer-filter-history": "7" });
  const list = element({});
  const select = element({});
  select.options = [
    { hidden: false, dataset: { customerSearch: "" } },
    { hidden: false, dataset: { customerSearch: "иванов +7 912" } },
    { hidden: false, dataset: { customerSearch: "сидоров +7 495" } },
  ];
  byId.history = list; byId.select = select;
  const root = { querySelectorAll: () => [input] };
  return { input, list, select, root };
}
const window = { document, localStorage: storage };
vm.runInNewContext(source, { window, WeakSet });
const direct = page(); current = [direct.input];
document.fire("DOMContentLoaded");
const directBound = (direct.input.handlers.keydown || []).length;
const replacement = page(); current = [replacement.input];
document.fire("denstock:page-loaded", { detail: { root: replacement.root } });
document.fire("denstock:page-loaded", { detail: { root: replacement.root } });
replacement.input.value = "сидоров";
replacement.input.dispatch("input", { inputType: "insertText" });
const filtered = replacement.select.options.map((option) => option.hidden);
replacement.input.value = "клиент через меню";
replacement.input.dispatch("keydown", { key: "Enter" });
process.stdout.write(JSON.stringify({
  directBound,
  replacementBound: (replacement.input.handlers.keydown || []).length,
  filtered,
  stored: JSON.parse(store["denstock:customer-filter-history:7"] || "[]"),
}));
"""
    result = subprocess.run(
        [node, "-e", harness, str(JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "directBound": 1,
        "replacementBound": 1,
        "filtered": [False, True, False],
        "stored": ["клиент через меню"],
    }


# --- Разметка и неизменность порядка клиентов -----------------------------------------


def _field(html):
    return re.search(r'<input id="customer-filter-sale"[^>]*>', html).group(0)


def test_field_disables_native_autofill_and_uses_scoped_history(client, make_user, env):  # noqa: F811
    html = _scan_page_with_a_cart(client, make_user, env)
    field = _field(html)
    assert 'autocomplete="off"' in field
    assert 'list="customer-filter-history-sale"' in field
    assert 'placeholder="Имя или телефон"' in field
    assert re.search(r'data-customer-filter-history="\d+"', field)
    assert '<datalist id="customer-filter-history-sale"></datalist>' in html
    assert "js/customer_filter_history.js" in html


def test_client_list_order_is_unchanged(client, make_user, env):  # noqa: F811
    old = _customer("Аистов", phone="+7 912 000-00-01")
    fresh = _customer("Яковлев", phone="+7 912 000-00-02")
    never = _customer("Борисов")
    _sale(old, days_ago=30)
    _sale(fresh, days_ago=1)
    html = _scan_page_with_a_cart(client, make_user, env)
    select = html.split('id="customer-select-sale"')[1].split("</select>")[0]
    rendered = [int(pk) for pk in re.findall(r'<option value="(\d+)"', select)]
    expected = [c.pk for c in customers_by_recent_activity(limit=500)]
    assert rendered == expected
    assert rendered.index(fresh.pk) < rendered.index(old.pk) < rendered.index(never.pk)


def test_name_and_phone_search_data_is_unchanged(client, make_user, env):  # noqa: F811
    _customer("Яковлев", phone="+7 912 000-00-02")
    html = _scan_page_with_a_cart(client, make_user, env)
    assert 'data-customer-search="яковлев +7 912 000-00-02"' in html
    source = JS_PATH.read_text(encoding="utf-8")
    assert 'option.dataset.customerSearch' in source
    assert 'search.indexOf(term) === -1' in source
