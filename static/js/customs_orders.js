(function () {
  "use strict";

  function initialize() {
    var form = document.querySelector("[data-customs-selection]");
    if (!form || form.dataset.initialized) return;
    form.dataset.initialized = "true";
    var rows = Array.from(form.querySelectorAll("[data-customs-boundary-row]"));
    var previews = JSON.parse(document.getElementById("customs-selection-previews").textContent);
    var dialog = form.querySelector("[data-customs-number-dialog]");
    var confirm = form.querySelector("[data-customs-confirm]");
    var number = form.querySelector('[name="number"]');

    function selectBoundary(index) {
      rows.forEach(function (row, current) {
        var selected = current <= index;
        var checkmark = row.querySelector("[data-prefix-check]");
        row.classList.toggle("customs-source--selected", selected);
        checkmark.textContent = selected ? "✓" : "○";
        checkmark.setAttribute("aria-label", selected ? "Выбрано" : "Не выбрано");
      });
      var preview = previews[index];
      form.querySelector("[data-prefix-count]").textContent = preview.count;
      form.querySelector("[data-prefix-quantity]").textContent = preview.quantity;
      form.querySelector("[data-prefix-amount]").textContent = preview.amount === null
        ? "Не заполнена оптовая цена" : preview.amount;
      confirm.disabled = false;
    }

    rows.forEach(function (row, index) {
      var radio = row.querySelector('[name="boundary"]');
      row.addEventListener("click", function (event) {
        if (event.target.closest("a, button")) return;
        radio.checked = true;
        selectBoundary(index);
      });
      radio.addEventListener("change", function () {
        if (radio.checked) selectBoundary(index);
      });
    });
    confirm.addEventListener("click", function () {
      if (!form.querySelector('[name="boundary"]:checked')) return;
      dialog.showModal();
      number.focus();
    });
    form.querySelector("[data-customs-dialog-cancel]").addEventListener("click", function () {
      dialog.close();
    });
    form.addEventListener("submit", function () {
      form.querySelector('button[type="submit"]').disabled = true;
    });
    var checked = form.querySelector('[name="boundary"]:checked');
    if (checked) selectBoundary(rows.indexOf(checked.closest("tr")));
  }

  document.addEventListener("denstock:page-loaded", initialize);
  initialize();
})();
