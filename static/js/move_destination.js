(function () {
  "use strict";

  var nextWidgetId = 1;
  var GROUP_LABELS = {
    current: "Деталь уже лежит здесь",
    preferred: "Закреплённая ячейка",
    other: "Другие ячейки",
  };

  function initWidget(widget) {
    if (widget.dataset.moveDestinationBound === "1") return;
    var input = widget.querySelector("[data-move-destination-input]");
    var hidden = widget.querySelector("[data-move-destination-id]");
    var list = widget.querySelector("[data-move-destination-options]");
    var status = widget.querySelector("[data-move-destination-status]");
    var clearButton = widget.querySelector("[data-move-destination-clear]");
    var form = widget.closest("form");
    var searchUrl = widget.getAttribute("data-search-url");
    var exclude = widget.getAttribute("data-exclude-location") || "";
    var partId = widget.getAttribute("data-part-id") || "";
    var submitOnEnter = widget.hasAttribute("data-submit-on-enter");
    if (!input || !hidden || !list || !status || !form || !searchUrl) return;

    widget.dataset.moveDestinationBound = "1";
    var widgetId = "move-destination-" + nextWidgetId++;
    var rows = [];
    var activeIndex = -1;
    var timer = null;
    var controller = null;
    var loading = false;
    var selectedCode = "";

    function setStatus(text, busy) {
      status.textContent = text;
      widget.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function hasSelection() {
      return !!hidden.value && input.value === selectedCode;
    }

    // With a cell already chosen the field holds its code; reopening the list
    // must show every cell again, not only the one that matches that code.
    function browseQuery() {
      return hasSelection() ? "" : input.value.trim();
    }

    function syncSelection() {
      var selected = !!hidden.value;
      widget.classList.toggle("has-selection", selected);
      if (clearButton) clearButton.hidden = !input.value;
      list.querySelectorAll('[role="option"]').forEach(function (option) {
        option.classList.toggle(
          "is-selected",
          selected && option.getAttribute("data-location-id") === hidden.value
        );
      });
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
      selectedCode = row.code;
      setStatus("Выбрана ячейка " + row.code + ".", false);
      closeList();
      syncSelection();
    }

    function clearSelection() {
      window.clearTimeout(timer);
      input.value = "";
      hidden.value = "";
      selectedCode = "";
      syncSelection();
      if (document.activeElement === input) {
        load("", false);
      } else {
        input.focus();
      }
    }

    function submitForm() {
      var button = form.querySelector("[data-move-destination-submit]");
      if (typeof form.requestSubmit === "function") {
        if (button) {
          form.requestSubmit(button);
        } else {
          form.requestSubmit();
        }
      } else if (button) {
        button.click();
      }
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

    function preventBlur(element) {
      element.addEventListener("mousedown", function (event) {
        event.preventDefault();
      });
    }

    function appendText(parent, className, text) {
      var span = document.createElement("span");
      span.className = className;
      span.textContent = text;
      parent.appendChild(span);
    }

    function render(payload) {
      rows = payload && Array.isArray(payload.results) ? payload.results : [];
      activeIndex = -1;
      list.replaceChildren();
      var grouped = rows.some(function (row) {
        return row.group && row.group !== "other";
      });
      var lastGroup = null;
      rows.forEach(function (row, index) {
        if (grouped && row.group !== lastGroup) {
          var heading = document.createElement("li");
          heading.className = "move-destination__group";
          heading.setAttribute("role", "presentation");
          heading.textContent = GROUP_LABELS[row.group] || GROUP_LABELS.other;
          preventBlur(heading);
          list.appendChild(heading);
          lastGroup = row.group;
        }
        var option = document.createElement("li");
        option.id = widgetId + "-option-" + row.id;
        option.className = "move-destination__option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.setAttribute("data-location-id", String(row.id));
        appendText(option, "move-destination__option-code", row.code);
        if (row.name && row.name !== row.code) {
          appendText(option, "move-destination__option-meta", " · " + row.name);
        }
        if (row.physical) {
          appendText(option, "move-destination__option-stock", "сейчас здесь " + row.physical + " шт.");
        }
        preventBlur(option);
        option.addEventListener("click", function () {
          selectRow(rows[index]);
          input.focus();
        });
        list.appendChild(option);
      });
      syncSelection();
      if (rows.length) {
        var text = "Найдено ячеек: " + rows.length + ".";
        if (payload.truncated) {
          text = "Показаны первые " + rows.length + " из " + payload.total + ". Уточните код.";
        }
        if (hasSelection()) text = "Выбрана ячейка " + selectedCode + ". " + text;
        setStatus(text, false);
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
      if (partId) url.searchParams.set("part", partId);
      loading = true;
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
          loading = false;
          render(payload);
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
              selectedCode = "";
              syncSelection();
              setStatus("Ячейки не найдены.", false);
            }
          }
        })
        .catch(function (error) {
          if (error.name === "AbortError") return;
          loading = false;
          rows = [];
          closeList();
          setStatus("Не удалось загрузить ячейки. Повторите попытку.", false);
        });
    }

    input.addEventListener("focus", function () {
      load(browseQuery(), false);
    });
    input.addEventListener("click", function () {
      if (list.hidden && !loading) load(browseQuery(), false);
    });
    input.addEventListener("input", function () {
      hidden.value = "";
      selectedCode = "";
      syncSelection();
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
        } else if (submitOnEnter && hasSelection()) {
          submitForm();
        } else if (input.value.trim()) {
          load(input.value.trim(), true);
        }
      } else if (event.key === "Escape") {
        closeList();
      }
    });
    if (clearButton) {
      preventBlur(clearButton);
      clearButton.addEventListener("click", clearSelection);
    }
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
      closeList();
      if (event.submitter) {
        var submitter = event.submitter;
        window.setTimeout(function () {
          submitter.disabled = true;
          submitter.setAttribute("data-move-destination-disabled", "");
        }, 0);
      }
    });
    // Back/forward cache restores the page exactly as it was mid-submit; the
    // form must be usable again instead of silently ignoring the next click.
    window.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      delete form.dataset.moveSubmitting;
      form.querySelectorAll("[data-move-destination-disabled]").forEach(function (button) {
        button.disabled = false;
        button.removeAttribute("data-move-destination-disabled");
      });
    });
    syncSelection();
    if (document.activeElement === input) {
      load(browseQuery(), false);
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
