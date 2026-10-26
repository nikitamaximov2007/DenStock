(function () {
  "use strict";

  var fields = ["gross_weight_kg", "net_weight_kg", "application_area", "quantity"];

  // Делегирование охватывает и строки, добавленные после загрузки страницы.
  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    var input = event.target.closest && event.target.closest("[data-cart-nav]");
    if (!input) return;
    var row = input.closest("[data-cart-nav-row]");
    if (!row) return;
    var index = fields.indexOf(input.dataset.cartNav);
    var target = null;
    if (input.tagName === "SELECT") {
      // Закрытый select: горизонтальные стрелки только переводят фокус.
      // Открытый picker и все остальные клавиши оставляем браузеру.
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      try {
        if (input.matches(":open")) return;
      } catch (error) {
        return; // Старый браузер без :open: не рискуем менять выбор в picker.
      }
      var neighbor = event.key === "ArrowRight" ? fields[index + 1] : fields[index - 1];
      target = row.querySelector('[data-cart-nav="' + neighbor + '"]');
    } else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      // Пока курсор движется внутри текста, оставляем обычное редактирование.
      if (input.selectionStart !== input.selectionEnd) return;
      if (event.key === "ArrowRight" && input.selectionEnd === input.value.length) {
        target = row.querySelector('[data-cart-nav="' + fields[index + 1] + '"]');
      } else if (event.key === "ArrowLeft" && input.selectionStart === 0) {
        target = row.querySelector('[data-cart-nav="' + fields[index - 1] + '"]');
      }
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      var sibling = event.key === "ArrowDown" ? row.nextElementSibling : row.previousElementSibling;
      if (sibling && sibling.matches("[data-cart-nav-row]")) {
        target = sibling.querySelector('[data-cart-nav="' + fields[index] + '"]');
      }
    }
    if (!target) return;
    event.preventDefault();
    target.focus();
    if (target.tagName === "INPUT") {
      var caret = event.key === "ArrowLeft" ? 0 : target.value.length;
      target.setSelectionRange(caret, caret);
    }
  });

  document.querySelectorAll(".customer-search[data-customer-search]").forEach(function (root) {
    var input = root.querySelector("[data-customer-filter]");
    var select = document.getElementById(input.dataset.customerFilter);
    var list = root.querySelector('[role="listbox"]');
    var timer = null;
    var controller = null;
    var generation = 0;
    var active = -1;
    var results = [];

    function invalidate() {
      generation += 1;
      clearTimeout(timer);
      if (controller) controller.abort();
      controller = null;
    }

    function close() {
      invalidate();
      list.hidden = true;
      list.replaceChildren();
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      results = [];
      active = -1;
    }

    function highlight(index) {
      active = index;
      Array.from(list.querySelectorAll('[role="option"]')).forEach(function (option, i) {
        option.setAttribute("aria-selected", i === index ? "true" : "false");
      });
      if (index >= 0) {
        var option = list.querySelectorAll('[role="option"]')[index];
        input.setAttribute("aria-activedescendant", option.id);
        option.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function choose(customer) {
      var value = String(customer.id);
      var option = Array.from(select.options).find(function (item) { return item.value === value; });
      if (!option) {
        option = new Option(customer.label, value);
        select.add(option);
      }
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      input.value = customer.label;
      close();
      input.focus();
    }

    function render(items) {
      list.replaceChildren();
      results = items;
      active = -1;
      if (!items.length) {
        var empty = document.createElement("div");
        empty.className = "customer-search__empty";
        empty.textContent = "Клиенты не найдены";
        list.append(empty);
      } else {
        items.forEach(function (customer, index) {
          var option = document.createElement("button");
          option.type = "button";
          option.id = list.id + "-" + index;
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", "false");
          option.textContent = customer.label;
          option.addEventListener("click", function () { choose(customer); });
          list.append(option);
        });
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    input.addEventListener("input", function () {
      invalidate();
      list.hidden = true;
      list.replaceChildren();
      results = [];
      active = -1;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      var query = input.value.trim().replace(/\s+/g, " ");
      if (!query) return; // Очистка поиска не сбрасывает выбранного клиента.
      var current = generation;
      timer = setTimeout(async function () {
        controller = new AbortController();
        try {
          var url = new URL(input.dataset.customerSearchUrl, window.location.href);
          url.searchParams.set("q", query);
          var response = await fetch(url, { signal: controller.signal });
          if (!response.ok) throw new Error("search failed");
          var data = await response.json();
          if (current === generation && document.activeElement === input) render(data.results);
        } catch (error) {
          if (error.name !== "AbortError" && current === generation) {
            list.textContent = "Не удалось найти клиентов";
            list.hidden = false;
            input.setAttribute("aria-expanded", "true");
          }
        }
      }, 180);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        if (!list.hidden) { event.preventDefault(); close(); }
      } else if (!list.hidden && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        if (results.length) highlight(
          event.key === "ArrowDown" ? (active + 1) % results.length
            : (active - 1 + results.length) % results.length
        );
      } else if (event.key === "Enter") {
        event.preventDefault(); // Поиск никогда не отправляет форму продажи.
        if (!list.hidden && active >= 0) choose(results[active]);
      }
    });

    document.addEventListener("pointerdown", function (event) {
      if (!root.contains(event.target)) close();
    });
  });
})();
