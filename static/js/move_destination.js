(function () {
  "use strict";

  var nextWidgetId = 1;

  function initWidget(widget) {
    if (widget.dataset.moveDestinationBound === "1") return;
    var input = widget.querySelector("[data-move-destination-input]");
    var hidden = widget.querySelector("[data-move-destination-id]");
    var list = widget.querySelector("[data-move-destination-options]");
    var status = widget.querySelector("[data-move-destination-status]");
    var form = widget.closest("form");
    var searchUrl = widget.getAttribute("data-search-url");
    var exclude = widget.getAttribute("data-exclude-location") || "";
    if (!input || !hidden || !list || !status || !form || !searchUrl) return;

    widget.dataset.moveDestinationBound = "1";
    var widgetId = "move-destination-" + nextWidgetId++;
    var rows = [];
    var activeIndex = -1;
    var timer = null;
    var controller = null;

    function setStatus(text, loading) {
      status.textContent = text;
      widget.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function closeList() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      activeIndex = -1;
    }

    function openList() {
      if (!rows.length) {
        closeList();
        return;
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function selectRow(row) {
      input.value = row.code;
      hidden.value = String(row.id);
      setStatus("Выбрана ячейка " + row.code + ".", false);
      closeList();
    }

    function setActive(index) {
      if (!rows.length) return;
      activeIndex = (index + rows.length) % rows.length;
      list.querySelectorAll('[role="option"]').forEach(function (option, optionIndex) {
        var active = optionIndex === activeIndex;
        option.classList.toggle("is-active", active);
        option.setAttribute("aria-selected", active ? "true" : "false");
        if (active) {
          input.setAttribute("aria-activedescendant", option.id);
          option.scrollIntoView({ block: "nearest" });
        }
      });
      openList();
    }

    function render(results) {
      rows = Array.isArray(results) ? results : [];
      activeIndex = -1;
      list.replaceChildren();
      rows.forEach(function (row, index) {
        var option = document.createElement("li");
        option.id = widgetId + "-option-" + row.id;
        option.className = "move-destination__option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.textContent = row.code + (row.name ? " · " + row.name : "");
        option.addEventListener("mousedown", function (event) {
          event.preventDefault();
        });
        option.addEventListener("click", function () {
          selectRow(rows[index]);
          input.focus();
        });
        list.appendChild(option);
      });
      if (rows.length) {
        setStatus("Найдено ячеек: " + rows.length + ".", false);
        openList();
      } else {
        setStatus("Ячейки не найдены.", false);
        closeList();
      }
    }

    function load(query, exact) {
      if (controller) controller.abort();
      controller = window.AbortController ? new AbortController() : null;
      var url = new URL(searchUrl, window.location.href);
      url.searchParams.set("q", query || "");
      if (exclude) url.searchParams.set("exclude", exclude);
      setStatus("Загрузка ячеек...", true);
      return fetch(url.toString(), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller ? controller.signal : undefined,
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Location search failed");
          return response.json();
        })
        .then(function (payload) {
          render(payload.results);
          if (exact) {
            var normalized = (query || "").trim().toLocaleLowerCase("ru-RU");
            var match = rows.find(function (row) {
              return (
                row.code.toLocaleLowerCase("ru-RU") === normalized ||
                (row.barcode || "").toLocaleLowerCase("ru-RU") === normalized
              );
            });
            if (match) {
              selectRow(match);
            } else {
              hidden.value = "";
              setStatus("Ячейки не найдены.", false);
            }
          }
        })
        .catch(function (error) {
          if (error.name === "AbortError") return;
          rows = [];
          closeList();
          setStatus("Не удалось загрузить ячейки. Повторите попытку.", false);
        });
    }

    input.addEventListener("focus", function () {
      load(input.value.trim(), false);
    });
    input.addEventListener("input", function () {
      hidden.value = "";
      window.clearTimeout(timer);
      closeList();
      timer = window.setTimeout(function () {
        load(input.value.trim(), false);
      }, 160);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(activeIndex + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(activeIndex - 1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        window.clearTimeout(timer);
        timer = null;
        if (activeIndex >= 0 && rows[activeIndex]) {
          selectRow(rows[activeIndex]);
        } else if (input.value.trim()) {
          load(input.value.trim(), true);
        }
      } else if (event.key === "Escape") {
        closeList();
      }
    });
    document.addEventListener("click", function (event) {
      if (!widget.contains(event.target)) closeList();
    });
    form.addEventListener("submit", function (event) {
      if (
        event.submitter &&
        (event.submitter.value === "reset" ||
          event.submitter.hasAttribute("data-move-destination-cancel"))
      ) {
        return;
      }
      if (!input.value.trim()) {
        event.preventDefault();
        setStatus("Выберите новую ячейку.", false);
        input.focus();
        return;
      }
      if (form.dataset.moveSubmitting === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.moveSubmitting = "1";
      if (event.submitter) {
        var submitter = event.submitter;
        window.setTimeout(function () {
          submitter.disabled = true;
        }, 0);
      }
    });
    if (document.activeElement === input) {
      load(input.value.trim(), false);
    }
  }

  function init(root) {
    (root || document).querySelectorAll("[data-move-destination]").forEach(initWidget);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  document.addEventListener("denstock:page-loaded", function (event) {
    init(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
