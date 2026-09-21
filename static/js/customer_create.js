(function () {
  "use strict";

  var form = document.querySelector("[data-client-create-form]");
  var button = form && form.querySelector("[data-client-create-submit]");
  var submitted = false;

  if (!form || !button || !form.querySelector("[name='client_create_token']")) {
    return;
  }

  form.addEventListener("submit", function (event) {
    if (!form.checkValidity()) {
      return;
    }
    if (submitted) {
      event.preventDefault();
      return;
    }
    submitted = true;
    button.disabled = true;
    button.textContent = "Создание...";
  });
}());
