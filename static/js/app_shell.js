// App shell: только UI-поведение (без бизнес-логики). Мобильное меню открывается
// pure-CSS чекбоксом #nav-toggle; здесь — прогрессивные улучшения: закрытие по Esc
// и запоминание свёрнутых разделов меню между страницами.
(function () {
  "use strict";

  var toggle = document.getElementById("nav-toggle");

  // Esc закрывает выехавшее мобильное меню.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && toggle && toggle.checked) {
      toggle.checked = false;
    }
  });

  // Запоминаем, какие разделы меню свёрнуты (по индексу), между переходами.
  var KEY = "denstock.nav.groups";
  var state = {};
  try {
    state = JSON.parse(localStorage.getItem(KEY) || "{}") || {};
  } catch (e) {
    state = {};
  }

  var groups = document.querySelectorAll("[data-nav-group]");
  Array.prototype.forEach.call(groups, function (group) {
    var id = group.getAttribute("data-nav-group");
    if (Object.prototype.hasOwnProperty.call(state, id)) {
      group.open = !!state[id];
    }
    group.addEventListener("toggle", function () {
      state[id] = group.open;
      try {
        localStorage.setItem(KEY, JSON.stringify(state));
      } catch (e) {
        /* localStorage может быть недоступен — не критично */
      }
    });
  });

  // UI guard for mutation forms. The database token remains authoritative;
  // this prevents an accidental second Enter while the first POST is loading.
  function bindMutationForms(root) {
    var forms = (root || document).querySelectorAll("[data-idempotent-form]");
    Array.prototype.forEach.call(forms, function (form) {
      if (form.dataset.idempotentBound === "1") {
        return;
      }
      form.dataset.idempotentBound = "1";
      form.addEventListener("submit", function (event) {
        if (form.dataset.submitting === "1") {
          event.preventDefault();
          return;
        }
        form.dataset.submitting = "1";
        var button = form.querySelector('[type="submit"]');
        if (button) {
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
          if (button.dataset.progressLabel) {
            button.textContent = button.dataset.progressLabel;
          }
        }
      });
    });
  }

  bindMutationForms(document);
  document.addEventListener("denstock:page-loaded", function (event) {
    bindMutationForms(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
