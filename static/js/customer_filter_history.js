/*
 * История поисковых строк поля «Найти клиента» (корзина быстрых действий).
 *
 * Хранит ТЕКСТ последних выполненных поисков, а не клиентов: шесть уникальных
 * строк, самая недавняя первой (MRU). Повторный поиск поднимает строку наверх.
 * Порядок карточек клиентов в списке выбора не меняется: это отдельный список.
 *
 * Поиск считается выполненным, когда оператор подтвердил строку: Enter,
 * уход из поля после правки (change), выбор клиента при заполненном поле или
 * выбор строки из истории. Отдельные нажатия клавиш не записываются.
 *
 * Хранится в localStorage этого браузера, отдельно для каждого пользователя
 * (ключ с его id). Прежний выпадающий список был нативной историей браузера,
 * которую браузер пополнял только при обычной отправке формы и сортировал по
 * частоте; поле теперь помечено autocomplete="off".
 */
(function (root) {
  "use strict";

  var LIMIT = 6;
  var KEY_PREFIX = "denstock:customer-filter-history:";

  function normalize(value) {
    return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  }

  function sameQuery(a, b) {
    return a.toLowerCase() === b.toLowerCase();
  }

  // Новый список: строка первой, без её прежних вхождений, не длиннее LIMIT.
  function remember(list, query) {
    var current = (list || []).slice(0, LIMIT);
    var term = normalize(query);
    if (!term) return current;
    var rest = current.filter(function (item) { return !sameQuery(item, term); });
    return [term].concat(rest).slice(0, LIMIT);
  }

  function load(storage, key) {
    try {
      var data = JSON.parse(storage.getItem(key) || "[]");
      if (!Array.isArray(data)) return [];
      var clean = [];
      data.forEach(function (item) {
        if (typeof item !== "string") return;
        var term = normalize(item);
        if (term && !clean.some(function (seen) { return sameQuery(seen, term); })) {
          clean.push(term);
        }
      });
      return clean.slice(0, LIMIT);
    } catch (error) {
      return [];
    }
  }

  function save(storage, key, list) {
    try {
      storage.setItem(key, JSON.stringify(list.slice(0, LIMIT)));
    } catch (error) {
      // Хранилище недоступно (приватный режим, квота): история просто не сохранится.
    }
  }

  function createHistory(storage, scope) {
    var key = KEY_PREFIX + String(scope || "anonymous");
    return {
      key: key,
      items: function () { return load(storage, key); },
      record: function (query) {
        var next = remember(load(storage, key), query);
        save(storage, key, next);
        return next;
      },
    };
  }

  function bind(input, options) {
    var doc = options.document;
    var history = createHistory(options.storage, input.getAttribute("data-customer-filter-history"));
    var list = doc.getElementById(input.getAttribute("list"));
    var select = doc.getElementById(input.getAttribute("data-customer-filter"));

    function render(items) {
      if (!list) return;
      while (list.firstChild) list.removeChild(list.firstChild);
      items.forEach(function (item) {
        var option = doc.createElement("option");
        option.value = item;
        list.appendChild(option);
      });
    }

    function commit() {
      if (!normalize(input.value)) return;
      render(history.record(input.value));
    }

    render(history.items());
    input.addEventListener("focus", function () { render(history.items()); });
    input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      // Enter в поле поиска не должен отправлять форму проведения продажи.
      event.preventDefault();
      commit();
    });
    input.addEventListener("change", commit);
    input.addEventListener("input", function (event) {
      // Выбор строки из истории (datalist): браузер подставляет значение целиком.
      var picked = !event || !event.inputType || event.inputType === "insertReplacementText";
      var term = normalize(input.value);
      if (picked && term && history.items().some(function (item) { return sameQuery(item, term); })) {
        commit();
      }
    });
    if (select) select.addEventListener("change", commit);
    return { history: history, commit: commit, render: render };
  }

  function init(doc, storage) {
    var bound = [];
    doc.querySelectorAll("[data-customer-filter-history]").forEach(function (input) {
      bound.push(bind(input, { document: doc, storage: storage }));
    });
    return bound;
  }

  var api = { LIMIT: LIMIT, KEY_PREFIX: KEY_PREFIX, normalize: normalize, remember: remember,
              load: load, createHistory: createHistory, bind: bind, init: init };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.DenisStockCustomerFilterHistory = api;
    var start = function () {
      var storage = null;
      try { storage = root.localStorage; } catch (error) { storage = null; }
      if (!storage) return;
      init(root.document, storage);
    };
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }
})(typeof window !== "undefined" ? window : this);
