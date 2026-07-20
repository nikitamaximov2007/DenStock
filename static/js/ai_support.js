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

  function newToken() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (char) {
      var random = Math.floor(Math.random() * 16);
      var value = char === "x" ? random : (random & 3) | 8;
      return value.toString(16);
    });
  }

  function messageArticle(role, textValue, processing) {
    var article = document.createElement("article");
    article.className = "ai-message ai-message--" + role;
    article.dataset.supportOptimistic = "1";
    if (processing) {
      article.classList.add("ai-message--processing");
      article.dataset.supportProcessing = "1";
    }

    var meta = document.createElement("div");
    meta.className = "ai-message__meta";
    meta.textContent = role === "user" ? "Вы - сейчас" : "ИИ-поддержка";
    article.appendChild(meta);

    var textNode = document.createElement("div");
    textNode.className = "ai-message__text";
    textNode.textContent = textValue;
    if (processing) textNode.setAttribute("role", "status");
    article.appendChild(textNode);
    return article;
  }

  function bindComposer(scope) {
    var form = scope.querySelector("[data-support-composer]");
    if (!form || form.dataset.supportComposerBound === "1") return;
    form.dataset.supportComposerBound = "1";
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented || form.dataset.supportOptimistic === "1") return;
      var input = form.querySelector('textarea[name="text"]');
      var list = scope.querySelector(".ai-support__messages");
      var textValue = input ? input.value.trim() : "";
      if (!list || !textValue) return;

      form.dataset.supportOptimistic = "1";
      form.setAttribute("aria-busy", "true");
      input.readOnly = true;
      list.appendChild(messageArticle("user", textValue, false));
      list.appendChild(messageArticle("assistant", "ИИ анализирует вопрос...", true));
      list.lastElementChild.scrollIntoView({ block: "nearest" });
    });
  }

  function retryQuestion(button, scope) {
    var article = button.closest(".ai-message");
    var question = article;
    while (question && !question.classList.contains("ai-message--user")) {
      question = question.previousElementSibling;
    }
    var textNode = question && question.querySelector(".ai-message__text");
    var form = scope.querySelector("[data-support-composer]");
    var input = form && form.querySelector('textarea[name="text"]');
    if (!textNode || !input) return;
    input.value = textNode.textContent.trim();
    input.readOnly = false;
    var token = form.querySelector('[name="idempotency_token"]');
    if (token) token.value = newToken();
    input.focus();
    input.scrollIntoView({ block: "center" });
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
    scope.querySelectorAll("[data-support-retry]").forEach(function (button) {
      if (button.dataset.supportBound === "1") return;
      button.dataset.supportBound = "1";
      button.addEventListener("click", function () {
        retryQuestion(button, scope);
      });
    });
    var pageQuestion = scope.querySelector("[data-support-page-question]");
    var sourcePath = storageGet();
    if (pageQuestion) {
      pageQuestion.hidden = !sourcePath || sourcePath.indexOf("/ai-support/") === 0;
    }
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
    bindComposer(scope);
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
  window.addEventListener("pageshow", function (event) {
    if (event.persisted && document.querySelector("[data-support-optimistic]")) {
      window.location.reload();
    }
  });
  bind(document);
})();
