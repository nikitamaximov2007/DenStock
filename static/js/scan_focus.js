// Непрерывное сканирование на экранах с полной перезагрузкой (PRG): держит
// поле [data-scan-input] сфокусированным и готовым между сканами без скачков
// прокрутки и не даёт «пустому» Enter/CR со сканера уйти на сервер.
//
// Почему это нужно: страницы скана (инвентаризация ячейки, приёмка,
// перемещение, действия) после каждого скана делают POST/GET + перезагрузку.
// Голого HTML-autofocus + якоря #scan недостаточно: при восстановлении
// прокрутки и на длинной таблице фокус часто оказывается на <body>, а
// хвостовой Enter со сканера отправляет пустую форму («Пустой скан»), и
// ошибка зацикливается, пока пользователь вручную не кликнет в поле.
(function () {
  "use strict";

  var EDITABLE = /^(input|textarea|select)$/i;

  function scanInput() {
    return document.querySelector("[data-scan-input]");
  }

  function isVisible(el) {
    return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  }

  // Единая функция восстановления фокуса. options.clear — очистить поле.
  function focusScanInput(options) {
    options = options || {};
    var input = scanInput();
    if (!input || input.disabled || input.readOnly || !isVisible(input)) {
      return false;
    }
    if (options.clear === true) {
      input.value = "";
    }
    try {
      // preventScroll: не поднимаем страницу, даже если поле вне вида.
      input.focus({ preventScroll: true });
    } catch (err) {
      input.focus();
    }
    if (typeof input.select === "function") {
      input.select();
    }
    return document.activeElement === input;
  }

  // Пользователь сознательно работает в другом поле (правка количества,
  // описание ячейки, поиск, select) — не отбираем у него ввод.
  function userIsEditingElsewhere() {
    var active = document.activeElement;
    if (!active || active === document.body) {
      return false;
    }
    if (active.hasAttribute && active.hasAttribute("data-scan-input")) {
      return false;
    }
    if (active.isContentEditable) {
      return true;
    }
    return EDITABLE.test(active.tagName);
  }

  // CSS-модалка (например, расчёт стоимости ячейки) открыта, когда её id в хэше.
  function modalOpen() {
    return document.querySelector(".css-modal:target") !== null;
  }

  function markReady() {
    var input = scanInput();
    var indicator = document.querySelector("[data-scan-indicator]");
    if (!indicator) {
      return;
    }
    var ready = !!input && document.activeElement === input;
    indicator.classList.toggle("is-ready", ready);
    indicator.textContent = ready
      ? indicator.getAttribute("data-ready-label") || "Сканер готов"
      : indicator.getAttribute("data-idle-label") ||
        "Нажмите, чтобы продолжить сканирование";
  }

  function maybeFocus() {
    if (!userIsEditingElsewhere() && !modalOpen()) {
      focusScanInput();
    }
    markReady();
  }

  var submitting = false;

  function bindForm(input) {
    var form = input.form;
    if (!form || form.dataset.scanFocusBound === "1") {
      return;
    }
    form.dataset.scanFocusBound = "1"; // без повторной привязки listener'ов

    form.addEventListener("submit", function (event) {
      // Пустой скан (хвостовой Enter/CR со сканера) не уходит на сервер;
      // серверная валидация при этом сохраняется как защита.
      if ((input.value || "").trim() === "") {
        event.preventDefault();
        focusScanInput({ clear: true });
        markReady();
        return;
      }
      // Защита от двойной отправки: пока идёт PRG-перезагрузка, повторный
      // submit (второй Enter / CR+LF) игнорируется. Полная перезагрузка
      // сбрасывает флаг сама; pageshow снимает его при bfcache-возврате.
      if (submitting) {
        event.preventDefault();
        return;
      }
      submitting = true;
    });
  }

  function bindIndicator() {
    var indicator = document.querySelector("[data-scan-indicator]");
    if (!indicator || indicator.dataset.scanFocusBound === "1") {
      return;
    }
    indicator.dataset.scanFocusBound = "1";
    indicator.addEventListener("click", function () {
      focusScanInput();
      markReady();
    });
  }

  function init() {
    var input = scanInput();
    if (!input) {
      return; // на странице нет поля скана
    }
    bindForm(input);
    bindIndicator();
    input.addEventListener("blur", function () {
      // отложенно: клик в кнопку/строку не должен «мигать» индикатором
      window.setTimeout(markReady, 0);
    });
    input.addEventListener("focus", markReady);
    focusScanInput(); // начальный фокус после (пере)загрузки
    markReady();
  }

  document.addEventListener("DOMContentLoaded", init);
  // Возврат Назад/Вперёд из bfcache: страница восстановлена — снова готова.
  window.addEventListener("pageshow", function () {
    submitting = false;
    maybeFocus();
  });
  // Возврат на вкладку браузера.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      maybeFocus();
    }
  });
  window.addEventListener("focus", maybeFocus);
  // Закрытие CSS-модалки (переход хэша обратно на #scan) возвращает фокус.
  window.addEventListener("hashchange", maybeFocus);

  // Экспорт для отладки и browser-smoke.
  window.DenScanFocus = { focusScanInput: focusScanInput };
})();
