// Автосохранение черновика корзины «Действий со склада».
//
// Каждое изменённое поле (количество, вес брутто/нетто, область применения,
// клиент) сразу сохраняется на сервере: черновик живёт там, а не в браузере,
// и переживает перезагрузку. «Провести» сначала дожидается всех сохранений и
// только потом отправляет форму; сервер проводит сохранённый черновик.
//
// Запросы одной корзины идут строго по одному: ответ на старый запрос не может
// прийти после нового и перезаписать его. Сохраняется всегда ТЕКУЩЕЕ значение
// поля в момент отправки, поэтому быстрый ввод 1 -> 12 -> 123 сохраняет 123.
(function () {
  "use strict";

  // Чистая очередь без DOM: её поведение проверяют тесты под node.
  function createAutosaveQueue(options) {
    var send = options.send;
    var delayMs = options.delayMs === undefined ? 600 : options.delayMs;
    var schedule = options.schedule || function (fn, ms) { return setTimeout(fn, ms); };
    var cancel = options.cancel || function (handle) { clearTimeout(handle); };
    var onIdle = options.onIdle || function () {};
    var timers = {};
    var dirty = [];
    var failed = {};
    var running = false;
    var idleWaiters = [];

    function markDirty(key) {
      if (dirty.indexOf(key) === -1) dirty.push(key);
    }

    function settle() {
      var waiters = idleWaiters;
      idleWaiters = [];
      waiters.forEach(function (resolve) { resolve(); });
    }

    function pump() {
      if (running) return;
      if (!dirty.length) {
        if (!Object.keys(timers).length) onIdle(Object.keys(failed).length === 0);
        settle();
        return;
      }
      running = true;
      var key = dirty.shift();
      Promise.resolve()
        .then(function () { return send(key); })
        .then(
          function (result) {
            if (result && result.ok) delete failed[key];
            else failed[key] = true;
          },
          function () { failed[key] = true; }
        )
        .then(function () {
          running = false;
          pump();
        });
    }

    function touch(key, immediate) {
      if (timers[key] !== undefined) {
        cancel(timers[key]);
        delete timers[key];
      }
      if (immediate) {
        markDirty(key);
        pump();
        return;
      }
      timers[key] = schedule(function () {
        delete timers[key];
        markDirty(key);
        pump();
      }, delayMs);
    }

    // Сохранить всё отложенное и всё, что не сохранилось раньше, и дождаться.
    // true - на сервере всё, что видит сотрудник.
    function flush() {
      Object.keys(timers).forEach(function (key) {
        cancel(timers[key]);
        delete timers[key];
        markDirty(key);
      });
      Object.keys(failed).forEach(markDirty);
      return new Promise(function (resolve) {
        idleWaiters.push(resolve);
        pump();
      }).then(function () {
        return Object.keys(failed).length === 0;
      });
    }

    function pending() {
      return running || dirty.length > 0 || Object.keys(timers).length > 0;
    }

    return { touch: touch, flush: flush, pending: pending };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createAutosaveQueue: createAutosaveQueue };
    return;
  }

  var STATUS = {
    saving: "Сохранение...",
    saved: "Сохранено",
    rejected: "Не сохранено: исправьте отмеченные поля.",
    offline: "Нет связи с сервером: изменения ещё не сохранены.",
  };

  function randomId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  function groupKey(input) {
    var group = input.dataset.autosaveGroup;
    if (group === "quantity") return "quantity|" + input.dataset.rowKey;
    if (group === "customs") return "customs|" + input.dataset.partId;
    return "customer|";
  }

  function fieldName(input) {
    return input.name === "customer_id" ? "customer" : input.name;
  }

  function bindPanel(panel) {
    if (panel.dataset.autosaveBound === "1") return;
    panel.dataset.autosaveBound = "1";
    var url = panel.dataset.autosaveUrl;
    var kind = panel.dataset.kind;
    var client = randomId();
    var rev = 0;
    var statusNode = panel.querySelector("[data-autosave-status]");
    var csrfInput = panel.querySelector('input[name="csrfmiddlewaretoken"]');
    var csrf = csrfInput ? csrfInput.value : "";

    // Последняя ошибка сервера или сети важнее общей подсказки: «корзина уже
    // проведена в другой вкладке» должна остаться видна.
    var lastError = "";

    function setStatus(text) {
      if (statusNode) statusNode.textContent = text;
    }

    panel.denstockAutosaveProblem = function () {
      return lastError || STATUS.rejected;
    };

    function inputsFor(key) {
      var bits = key.split("|");
      var selector = '[data-autosave-group="' + bits[0] + '"]';
      if (bits[0] === "quantity") selector += '[data-row-key="' + bits[1] + '"]';
      if (bits[0] === "customs") selector += '[data-part-id="' + bits[1] + '"]';
      return Array.prototype.slice.call(panel.querySelectorAll(selector));
    }

    function showErrors(inputs, errors) {
      var byField = {};
      Object.keys(errors || {}).forEach(function (key) {
        var bits = key.split(":");
        byField[bits[1]] = errors[key];
      });
      inputs.forEach(function (input) {
        var field = fieldName(input);
        var message = byField[field] || "";
        var holder = input.parentElement;
        var span = holder && holder.querySelector('[data-autosave-error="' + field + '"]');
        if (span) span.textContent = message;
        if (message) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
      });
    }

    function applySaved(key, saved) {
      if (key.indexOf("quantity|") !== 0 || !saved) return;
      var rowKey = key.slice("quantity|".length);
      var cell = panel.querySelector('[data-row-total="' + rowKey + '"]');
      if (cell && saved.total) cell.textContent = saved.total;
      var total = panel.querySelector("[data-panel-total]");
      if (total && saved.panel_total) total.textContent = saved.panel_total;
    }

    function send(key) {
      var inputs = inputsFor(key);
      if (!inputs.length) return Promise.resolve({ ok: true });
      var group = key.split("|")[0];
      var body = new URLSearchParams();
      body.set("csrfmiddlewaretoken", csrf);
      body.set("kind", kind);
      body.set("group", group);
      body.set("client", client);
      rev += 1;
      body.set("rev", String(rev));
      if (group === "quantity") {
        body.set("row_key", inputs[0].dataset.rowKey);
        body.set("quantity", inputs[0].value);
      } else if (group === "customs") {
        body.set("part_id", inputs[0].dataset.partId);
        inputs.forEach(function (input) {
          if (!body.has(input.name)) body.set(input.name, input.value);
        });
      } else {
        body.set("customer_id", inputs[0].value);
      }
      setStatus(STATUS.saving);
      return fetch(url, {
        method: "POST",
        body: body,
        credentials: "same-origin",
        keepalive: true,
        headers: { "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest" },
      })
        .then(function (response) {
          return response.json().catch(function () {
            return { ok: false, error: STATUS.offline };
          });
        })
        .then(function (data) {
          if (data.stale) return { ok: true };
          showErrors(inputs, data.errors);
          if (data.ok) applySaved(key, data.saved);
          lastError = data.error || "";
          return { ok: Boolean(data.ok) };
        })
        .catch(function () {
          lastError = STATUS.offline;
          return { ok: false };
        });
    }

    var queue = createAutosaveQueue({
      send: send,
      onIdle: function (ok) {
        if (ok) lastError = "";
        setStatus(ok ? STATUS.saved : panel.denstockAutosaveProblem());
      },
    });
    panel.denstockAutosave = queue;

    function syncTwins(input) {
      // Одна деталь в двух ячейках: таможенные поля у неё общие.
      if (input.dataset.autosaveGroup !== "customs") return;
      inputsFor(groupKey(input)).forEach(function (twin) {
        if (twin !== input && twin.name === input.name) twin.value = input.value;
      });
    }

    panel.querySelectorAll("[data-autosave-group]").forEach(function (input) {
      var key = groupKey(input);
      if (input.tagName === "SELECT") {
        input.addEventListener("change", function () {
          syncTwins(input);
          queue.touch(key, true);
        });
      } else {
        input.addEventListener("input", function () {
          syncTwins(input);
          queue.touch(key, false);
        });
        input.addEventListener("change", function () {
          queue.touch(key, true);
        });
      }
    });

    // Enter в поле строки сохраняет строку, а не отправляет старую форму.
    panel.querySelectorAll(".cart-quantity-form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var rowInput = form.querySelector('[data-autosave-group="quantity"]');
        if (rowInput) queue.touch(groupKey(rowInput), true);
        Array.prototype.forEach.call(
          panel.querySelectorAll('[form="' + form.id + '"][data-autosave-group]'),
          function (input) { queue.touch(groupKey(input), true); }
        );
      });
    });
    panel.querySelectorAll("[data-autosave-fallback]").forEach(function (button) {
      // .btn задаёт display, поэтому одного атрибута hidden мало.
      button.hidden = true;
      button.style.display = "none";
    });

    // Значение на странице может отличаться от сохранённого: клиент только что
    // создан (?customer_id=), браузер восстановил ввод. Сохраняем сразу.
    panel.querySelectorAll("[data-autosave-group]").forEach(function (input) {
      var differs = input.tagName === "SELECT"
        ? (input.dataset.savedValue !== undefined
            ? input.value !== input.dataset.savedValue
            : Array.prototype.some.call(input.options, function (option) {
              return option.selected !== option.defaultSelected;
            }))
        : input.value !== input.defaultValue;
      if (differs) queue.touch(groupKey(input), true);
    });
  }

  function bindAll(root) {
    (root || document).querySelectorAll("[data-cart-autosave]").forEach(bindPanel);
  }

  // «Провести»: сначала все сохранения, потом обычная отправка формы. Слушатель
  // на document в фазе перехвата срабатывает раньше защиты от двойной отправки
  // (app_shell.js), поэтому кнопка не блокируется, пока идёт сохранение.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("[data-autosave-complete]")) return;
    var panel = form.closest("[data-cart-autosave]");
    var queue = panel && panel.denstockAutosave;
    if (!queue) return;
    if (form.dataset.autosaveFlushed === "1") {
      delete form.dataset.autosaveFlushed;
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (form.dataset.autosaveFlushing === "1") return;
    form.dataset.autosaveFlushing = "1";
    var submitter = event.submitter;
    var status = panel.querySelector("[data-autosave-status]");
    if (status) status.textContent = STATUS.saving;
    queue.flush().then(function (ok) {
      delete form.dataset.autosaveFlushing;
      if (!ok) {
        if (status) status.textContent = panel.denstockAutosaveProblem();
        var invalid = panel.querySelector('[aria-invalid="true"]');
        if (invalid) invalid.focus();
        return;
      }
      // Новой задачей, а не прямо здесь: пока браузер ещё внутри исходного
      // события submit (когда сохранять было нечего), повторная отправка
      // формы молча игнорируется.
      setTimeout(function () {
        form.dataset.autosaveFlushed = "1";
        if (form.requestSubmit) {
          form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
        } else {
          form.submit();
        }
      }, 0);
    });
  }, true);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { bindAll(document); });
  } else {
    bindAll(document);
  }
  document.addEventListener("denstock:page-loaded", function (event) {
    bindAll(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
