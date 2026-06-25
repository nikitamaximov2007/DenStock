// Слой 11 — поле сканера в топбаре (прогрессивное улучшение).
// Enter -> fetch на /scanner/resolve/ -> рендер статус-блока; автофокус обратно
// в поле; защита от пустого ввода и двойного скана (~1.2 c). Резолв только
// распознаёт — никаких складских действий здесь нет.
(function () {
  "use strict";

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : value;
    return div.innerHTML;
  }

  function initTopbar(field) {
    var input = field.querySelector(".scanfield__input");
    var resultBox = document.getElementById("scan-topbar-result");
    var url = field.getAttribute("data-scan-resolve-url");
    var csrf = field.getAttribute("data-scan-csrf");
    if (!input || !url) {
      return;
    }
    var lastCode = "";
    var lastTime = 0;

    function render(data) {
      if (!resultBox) {
        return;
      }
      var kind = "error";
      if (data.status === "found") {
        kind = "success";
      } else if (data.status === "ambiguous") {
        kind = "warning";
      }
      var inner;
      if (data.found && data.url) {
        inner = '<a href="' + data.url + '">' + escapeHtml(data.label) + "</a>";
      } else {
        inner = escapeHtml(data.message || "");
      }
      resultBox.innerHTML = '<span class="status status--' + kind + '">' + inner + "</span>";
    }

    function resolve(code) {
      var body = new URLSearchParams();
      body.append("code", code);
      body.append("context", "topbar");
      fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": csrf || "", "X-Requested-With": "XMLHttpRequest" },
        body: body,
      })
        .then(function (response) {
          return response.json();
        })
        .then(render)
        .catch(function () {
          if (resultBox) {
            resultBox.innerHTML =
              '<span class="status status--error">Ошибка соединения.</span>';
          }
        })
        .finally(function () {
          input.value = "";
          input.focus();
        });
    }

    input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      var code = input.value.trim();
      if (!code) {
        return; // защита от пустого ввода
      }
      var now = Date.now();
      if (code === lastCode && now - lastTime < 1200) {
        return; // анти-дребезг: одинаковый код в пределах ~1.2 c
      }
      lastCode = code;
      lastTime = now;
      resolve(code);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var field = document.querySelector(".scanfield[data-scan-resolve-url]");
    if (field) {
      initTopbar(field);
    }
    var pageInput = document.getElementById("scanner-page-input");
    if (pageInput) {
      pageInput.focus(); // автофокус на странице сканера
    }
  });
})();
