/*
 * Выбор существующей карточки детали по части названия (приёмка, пересчёт).
 *
 * Сканер не меняется: код и Enter отправляют форму скана как раньше. Подсказки
 * появляются только после паузы в наборе (сканер вводит код быстрее), и Enter
 * выбирает подсказку ТОЛЬКО если сотрудник явно подсветил её стрелкой. Иначе
 * Enter - обычная отправка скана. Неоднозначная строка «сальник» сама по себе
 * ничего не проводит: сервер её тоже не принимает как код.
 *
 * Выбор отправляет отдельную скрытую форму с part_id; сервер проверяет ID
 * заново. Текст подсказок вставляется только через textContent.
 */
(function (root) {
  "use strict";

  var DEBOUNCE_MS = 250;
  var MIN_QUERY = 2;

  function createPicker(input, options) {
    var doc = options.document;
    var fetchJson = options.fetchJson;
    var schedule = options.schedule || function (fn, ms) { return setTimeout(fn, ms); };
    var cancel = options.cancel || function (handle) { clearTimeout(handle); };
    var url = input.getAttribute("data-part-picker-url");
    var form = doc.getElementById(input.getAttribute("data-part-picker-form"));
    var partField = form ? form.querySelector("[name=part_id]") : null;
    var status = doc.getElementById(input.getAttribute("data-part-picker-status"));

    var list = doc.createElement("ul");
    list.className = "part-picker__list";
    list.setAttribute("role", "listbox");
    list.id = input.id + "-suggestions";
    list.hidden = true;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);
    input.parentNode.insertBefore(list, input.nextSibling);

    var state = { items: [], active: -1, seq: 0, timer: null, picking: false };

    function close() {
      list.hidden = true;
      state.items = [];
      state.active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }

    function highlight(index) {
      state.active = index;
      Array.prototype.forEach.call(list.children, function (li, i) {
        var on = i === index;
        li.setAttribute("aria-selected", on ? "true" : "false");
        li.classList.toggle("is-active", on);
        if (on) input.setAttribute("aria-activedescendant", li.id);
      });
      if (index < 0) input.removeAttribute("aria-activedescendant");
    }

    function render(items) {
      while (list.firstChild) list.removeChild(list.firstChild);
      state.items = items;
      state.active = -1;
      if (!items.length) {
        var empty = doc.createElement("li");
        empty.className = "part-picker__empty";
        empty.textContent = "Ничего не найдено";
        list.appendChild(empty);
      }
      items.forEach(function (item, index) {
        var li = doc.createElement("li");
        li.id = list.id + "-" + index;
        li.className = "part-picker__item";
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        var name = doc.createElement("span");
        name.className = "part-picker__name";
        name.textContent = item.name;
        li.appendChild(name);
        var meta = [];
        if (item.article) meta.push("Артикул: " + item.article);
        if (item.manufacturer) meta.push(item.manufacturer);
        if (meta.length) {
          var small = doc.createElement("span");
          small.className = "part-picker__meta";
          small.textContent = meta.join(" · ");
          li.appendChild(small);
        }
        li.addEventListener("pointerdown", function (event) {
          event.preventDefault(); // мышь/касание: фокус не уйдёт до выбора
          pick(index);
        });
        li.addEventListener("mouseenter", function () { highlight(index); });
        list.appendChild(li);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function pick(index) {
      var item = state.items[index];
      if (!item || state.picking || !form || !partField) return;
      state.picking = true;
      partField.value = String(item.id);
      if (status) status.textContent = "Выбрано: " + item.name;
      close();
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }

    function search(query) {
      var mine = ++state.seq;
      return fetchJson(url + "?q=" + encodeURIComponent(query)).then(function (data) {
        // Устаревший ответ: пока он шёл, текст изменился или ушёл новый запрос.
        if (mine !== state.seq || input.value.trim() !== query) return;
        render((data && data.results) || []);
      }).catch(function () {
        if (mine === state.seq) close();
      });
    }

    input.addEventListener("input", function () {
      if (state.timer !== null) cancel(state.timer);
      state.timer = null;
      state.seq += 1; // любой уже отправленный запрос теперь устарел
      close(); // старый вариант нельзя выбрать, пока ищем новый запрос
      var query = input.value.trim();
      if (query.length < MIN_QUERY) { close(); return; }
      state.timer = schedule(function () { state.timer = null; search(query); }, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (event) {
      var open = !list.hidden && state.items.length > 0;
      if (event.key === "ArrowDown" && open) {
        event.preventDefault();
        highlight(Math.min(state.active + 1, state.items.length - 1));
      } else if (event.key === "ArrowUp" && open) {
        event.preventDefault();
        highlight(Math.max(state.active - 1, 0));
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        close();
      } else if (event.key === "Enter") {
        if (open && state.active >= 0) {
          event.preventDefault();
          pick(state.active);
          return;
        }
        // Обычный Enter (сканер): отменяем ожидающий поиск, форма скана уходит как раньше.
        if (state.timer !== null) cancel(state.timer);
        state.timer = null;
        state.seq += 1;
        close();
      }
    });

    input.addEventListener("blur", function () { schedule(close, 150); });

    return { state: state, list: list, render: render, pick: pick, close: close, search: search };
  }

  // root - документ или контейнер, подменённый частичной навигацией.
  function init(doc, fetchJson, root) {
    var pickers = [];
    (root || doc).querySelectorAll("[data-part-picker-url]").forEach(function (input) {
      if (input.getAttribute("data-part-picker-bound") === "1") return;
      input.setAttribute("data-part-picker-bound", "1");
      pickers.push(createPicker(input, { document: doc, fetchJson: fetchJson }));
    });
    return pickers;
  }

  var api = { createPicker: createPicker, init: init, DEBOUNCE_MS: DEBOUNCE_MS, MIN_QUERY: MIN_QUERY };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.DenisStockPartNamePicker = api;
    var fetchJson = function (target) {
      return root.fetch(target, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }).then(function (response) {
        if (!response.ok) throw new Error("search failed");
        return response.json();
      });
    };
    var start = function () { init(root.document, fetchJson); };
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
    // Частичная навигация подменяет содержимое без запуска скриптов страницы.
    root.document.addEventListener("denstock:page-loaded", function (event) {
      init(root.document, fetchJson, event.detail && event.detail.root);
    });
  }
})(typeof window !== "undefined" ? window : this);
