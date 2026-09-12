// Маска российского телефона для полей [data-phone-input]. Одна и та же и в
// DenisStock, и в публичной заявке PRO-STOR: правило записи номера в проекте
// одно, значит и реализация одна.
//
// Что делает: пока номер доказуемо российский, поле показывает канонический
// вид «+7 900 123-45-67». Префикс «+7 9» появляется сам, как только человек
// набрал первую девятку, и «8» в начале молча заменяется на «+7», поэтому
// двойного «+7» не бывает: значение каждый раз собирается заново из цифр, а не
// дописывается к прежнему тексту. Поэтому же корректно вставляются
// «89001234567», «79001234567», «+79001234567» и «9001234567».
//
// Чего НЕ делает: не трогает ввод, который не начинается с 7, 8 или 9. Номер
// «+49 30 123456» это немецкий номер, а не российский без восьмёрки, и
// приписать ему «+7» значило бы выдумать факт (то же правило, что в
// apps/core/phones.py). Городской номер набирают через 8 или +7, и он тоже
// раскладывается по этой маске.
//
// Маска - только удобство ввода. Канон записи считает сервер
// (`canonical_phone_text`), валидация телефона тоже серверная: с выключенным
// JS поле остаётся обычным текстовым и заявка проходит.
(function () {
  "use strict";

  var RU_START = /^[789]$/;
  var NATIONAL_LENGTH = 10;

  // Цифры номера без кода страны и без междугородней восьмёрки. Пусто, если
  // цифр больше, чем в российском номере: обрезать лишние нельзя, иначе из
  // вставленного «с добавочным» получился бы другой, внешне правильный номер.
  function nationalDigits(value) {
    var digits = String(value == null ? "" : value).replace(/\D+/g, "");
    if (!digits) {
      return "";
    }
    if (digits.charAt(0) === "7" || digits.charAt(0) === "8") {
      digits = digits.slice(1);
    }
    return digits.length > NATIONAL_LENGTH ? "" : digits;
  }

  // «9001234567» -> «+7 900 123-45-67»; неполный номер форматируется настолько,
  // насколько цифр уже набрали, и обрывается без висящего разделителя.
  function formatNational(digits) {
    if (!digits) {
      return "";
    }
    var text = "+7 " + digits.slice(0, 3);
    if (digits.length > 3) {
      text += " " + digits.slice(3, 6);
    }
    if (digits.length > 6) {
      text += "-" + digits.slice(6, 8);
    }
    if (digits.length > 8) {
      text += "-" + digits.slice(8, 10);
    }
    return text;
  }

  function looksRussian(value) {
    var text = String(value == null ? "" : value).trim();
    if (text.charAt(0) === "+") {
      text = text.slice(1).replace(/^\s+/, "");
    }
    var digits = text.replace(/\D+/g, "");
    return digits.length > 0 && RU_START.test(digits.charAt(0));
  }

  // Разделяемая с тестами чистая функция: текст поля -> текст поля.
  function maskValue(value) {
    var text = String(value == null ? "" : value);
    if (!looksRussian(text)) {
      return text;
    }
    // Пусто на выходе разбора означает «пока не номер»: набрана одна «8», или
    // цифр больше, чем у российского номера. Такой текст остаётся как есть -
    // иначе первая цифра исчезала бы из поля, а лишние цифры пропадали бы
    // молча. Поэтому же поле всегда можно очистить backspace.
    var national = nationalDigits(text);
    return national ? formatNational(national) : text;
  }

  // Каретка считается по количеству цифр перед ней, иначе правка середины
  // номера каждый раз отбрасывала бы курсор в конец.
  function caretForDigits(text, digitsBefore) {
    if (digitsBefore <= 0) {
      return text.length && text.indexOf("+7 ") === 0 ? 3 : 0;
    }
    var seen = 0;
    for (var index = 0; index < text.length; index += 1) {
      if (/\d/.test(text.charAt(index))) {
        seen += 1;
        if (seen === digitsBefore) {
          return index + 1;
        }
      }
    }
    return text.length;
  }

  function digitsBeforeCaret(value, caret) {
    return value.slice(0, caret).replace(/\D+/g, "").length;
  }

  function apply(field) {
    var before = field.value;
    var masked = maskValue(before);
    if (masked === before) {
      return;
    }
    var caret = field.selectionStart;
    var digitsBefore = null;
    if (caret !== null && caret !== undefined) {
      // Если в набранном номере кода страны не было, маска добавила семёрку
      // перед кареткой, и цифр перед ней стало на одну больше. Ведущие 7 и 8
      // маска заменяет на свою семёрку, там сдвига нет.
      digitsBefore = digitsBeforeCaret(before, caret);
      if (!/^[78]/.test(before.replace(/\D+/g, ""))) {
        digitsBefore += 1;
      }
    }
    field.value = masked;
    if (digitsBefore !== null) {
      try {
        var position = caretForDigits(masked, digitsBefore);
        field.setSelectionRange(position, position);
      } catch (error) {
        // Некоторые типы полей не дают двигать каретку: значение уже верное.
      }
    }
  }

  function bind(field) {
    if (field.dataset.phoneInputBound === "1") {
      return;
    }
    field.dataset.phoneInputBound = "1";
    field.addEventListener("input", function () {
      apply(field);
    });
    field.addEventListener("blur", function () {
      apply(field);
    });
    if (field.value) {
      apply(field);
    }
  }

  function bindAll(root) {
    var fields = (root || document).querySelectorAll("[data-phone-input]");
    for (var index = 0; index < fields.length; index += 1) {
      bind(fields[index]);
    }
  }

  if (typeof module === "object" && module.exports) {
    // Для узлового теста маски: чистые функции без DOM.
    module.exports = { maskValue: maskValue, nationalDigits: nationalDigits };
  }

  if (typeof document === "undefined") {
    return;
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindAll(document);
    });
  } else {
    bindAll(document);
  }
  // Экраны DenisStock подменяют содержимое без полной перезагрузки.
  document.addEventListener("denstock:page-loaded", function (event) {
    bindAll(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
