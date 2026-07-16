// App shell: только UI-поведение (без бизнес-логики). Мобильное меню открывается
// pure-CSS чекбоксом #nav-toggle; здесь — прогрессивные улучшения: закрытие по Esc
// и независимое состояние раскрывающихся разделов между страницами.
(function () {
  "use strict";

  var toggle = document.getElementById("nav-toggle");

  // Esc закрывает выехавшее мобильное меню.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && toggle && toggle.checked) {
      toggle.checked = false;
    }
  });

  // Stable section keys keep state valid when labels or permissions change.
  var KEY = "denstock.nav.groups.v2";
  var state = {};
  try {
    var storedState = JSON.parse(localStorage.getItem(KEY) || "{}");
    state =
      storedState && typeof storedState === "object" && !Array.isArray(storedState)
        ? storedState
        : {};
  } catch (e) {
    state = {};
  }

  function saveGroupState() {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
    } catch (e) {
      // A disabled storage backend must not break the menu.
    }
  }

  function isActive(group) {
    return group.getAttribute("data-nav-active") === "true";
  }

  function setGroupOpen(group, open) {
    var button = group.querySelector("[data-nav-group-toggle]");
    var panel = group.querySelector("[data-nav-group-panel]");
    if (!button || !panel) return;
    group.classList.toggle("is-collapsed", !open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    panel.hidden = !open;
  }

  function applyGroupState() {
    var groups = document.querySelectorAll("[data-nav-group]");
    Array.prototype.forEach.call(groups, function (group) {
      var id = group.getAttribute("data-nav-group");
      var savedOpen = !Object.prototype.hasOwnProperty.call(state, id) || !!state[id];
      setGroupOpen(group, isActive(group) || savedOpen);
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-nav-group-toggle]");
    if (!button) return;
    var group = button.closest("[data-nav-group]");
    if (!group) return;
    var id = group.getAttribute("data-nav-group");
    var requestedOpen = button.getAttribute("aria-expanded") !== "true";
    state[id] = requestedOpen;
    saveGroupState();
    setGroupOpen(group, isActive(group) || requestedOpen);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
    var button = event.target.closest("[data-nav-group-toggle]");
    if (!button) return;
    event.preventDefault();
    button.click();
  });

  applyGroupState();
  window.DenStockSidebar = {
    groupKey: KEY,
    refresh: applyGroupState,
  };

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
