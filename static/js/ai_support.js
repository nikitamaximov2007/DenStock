(function () {
  "use strict";

  var SOURCE_KEY = "denstock.aiSupport.sourcePath.v1";

  function storageGet() {
    try {
      return window.sessionStorage.getItem(SOURCE_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function storageSet(value) {
    try {
      window.sessionStorage.setItem(SOURCE_KEY, value);
    } catch (error) {
      // Disabled storage must not break support navigation.
    }
  }

  function browserFamily() {
    var ua = window.navigator.userAgent || "";
    if (/Edg\//.test(ua)) return "Edge";
    if (/Firefox\//.test(ua)) return "Firefox";
    if (/Chrome\//.test(ua)) return "Chrome";
    if (/Safari\//.test(ua) && !/Chrome\//.test(ua)) return "Safari";
    return "Other";
  }

  function setSafeContext(root) {
    var path = storageGet();
    var viewport = String(window.innerWidth) + "x" + String(window.innerHeight);
    (root || document).querySelectorAll('[name="route_path"], [data-support-route-path]').forEach(function (input) {
      input.value = path;
    });
    (root || document).querySelectorAll('[name="browser_family"], [data-support-browser]').forEach(function (input) {
      input.value = browserFamily();
    });
    (root || document).querySelectorAll('[name="viewport"], [data-support-viewport]').forEach(function (input) {
      input.value = viewport;
    });
  }

  function bind(root) {
    var scope = root || document;
    scope.querySelectorAll("[data-support-question]").forEach(function (button) {
      if (button.dataset.supportBound === "1") return;
      button.dataset.supportBound = "1";
      button.addEventListener("click", function () {
        var input = scope.querySelector('[data-support-composer] textarea[name="text"]');
        if (!input) return;
        input.value = button.dataset.supportQuestion || "";
        input.focus();
      });
    });
    var toggle = scope.querySelector("[data-support-history-toggle]");
    var history = scope.querySelector("[data-support-history]");
    if (toggle && history && toggle.dataset.supportBound !== "1") {
      toggle.dataset.supportBound = "1";
      toggle.addEventListener("click", function () {
        var open = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        history.classList.toggle("is-open", open);
      });
    }
    setSafeContext(scope);
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest('a[href^="/ai-support/"]');
    if (link && window.location.pathname.indexOf("/ai-support/") !== 0) {
      storageSet(window.location.pathname);
    }
  });
  document.addEventListener("denstock:page-loaded", function (event) {
    bind(event.detail && event.detail.root ? event.detail.root : document);
  });
  window.addEventListener("resize", function () {
    setSafeContext(document);
  });
  bind(document);
})();
