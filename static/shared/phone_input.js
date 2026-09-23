// Маска российского телефона для полей [data-phone-input]. Обычный режим
// `ru` сохраняет внутренние поля совместимыми с историческими номерами, а
// режим `ru-mobile` используется публичной заявкой PRO-STOR.
//
// В обычном режиме поле показывает канонический вид «+7 900 123-45-67».
// В мобильном режиме фиксированы «+7 (9», пользователь вводит остальные
// девять цифр, а первая любая цифра считается цифрой после обязательной «9».
// Значение всегда собирается заново из цифр, поэтому двойного «+7» не бывает.
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

  function mobileNationalDigits(value) {
    var text = String(value == null ? "" : value).trim();
    var digits = text.replace(/\D+/g, "");
    if (!digits) {
      return "";
    }
    if (text.indexOf("+7") === 0 && digits.charAt(0) === "7") {
      digits = digits.slice(1);
    } else if (digits.length === 11 && (digits.charAt(0) === "7" || digits.charAt(0) === "8")) {
      digits = digits.slice(1);
    } else if (digits.length > 10) {
      return "";
    } else if (digits.length < 10 && text.charAt(0) !== "(") {
      digits = "9" + digits;
    }
    return digits.length <= NATIONAL_LENGTH && digits.charAt(0) === "9" ? digits : "";
  }

  function formatMobile(national) {
    var entered = national.slice(1);
    var text = "+7 (9" + entered.slice(0, 2);
    if (national.length >= 3) {
      text += ")";
    }
    if (entered.length > 2) {
      text += " " + entered.slice(2, 5);
    }
    if (entered.length > 5) {
      text += "-" + entered.slice(5, 7);
    }
    if (entered.length > 7) {
      text += "-" + entered.slice(7, 9);
    }
    return text;
  }

  function mobileMaskValue(value) {
    var national = mobileNationalDigits(value);
    return national ? formatMobile(national) : "";
  }

  function mobileDigitsBeforeCaret(value, caret) {
    var digits = digitsBeforeCaret(value, caret);
    if (value.indexOf("+7") === 0) {
      return Math.max(0, digits - 2); // country 7 and fixed mobile 9
    }
    return digits;
  }

  function mobileCaretForDigits(text, enteredDigits) {
    var fixedNine = text.indexOf("9", 4);
    if (fixedNine < 0) {
      return text.length;
    }
    if (enteredDigits <= 0) {
      return fixedNine + 1;
    }
    var seen = 0;
    for (var index = fixedNine + 1; index < text.length; index += 1) {
      if (/\d/.test(text.charAt(index))) {
        seen += 1;
        if (seen === enteredDigits) {
          return index + 1;
        }
      }
    }
    return text.length;
  }

  function applyMobile(field) {
    var before = field.value;
    var masked = mobileMaskValue(before);
    var caret = field.selectionStart;
    var enteredDigits = null;
    if (caret !== null && caret !== undefined) {
      enteredDigits = mobileDigitsBeforeCaret(before, caret);
    }
    field.value = masked;
    if (enteredDigits !== null) {
      try {
        var position = mobileCaretForDigits(masked, enteredDigits);
        field.setSelectionRange(position, position);
      } catch (error) {
        // Some mobile browsers do not allow changing the caret during input.
      }
    }
  }

  function removeMobileDigit(field, direction) {
    var start = field.selectionStart;
    var end = field.selectionEnd;
    if (start === null || end === null || start !== end) {
      return false;
    }
    var index = start + direction;
    while (index >= 5 && index < field.value.length && !/\d/.test(field.value.charAt(index))) {
      index += direction;
    }
    if (index < 5 || index >= field.value.length) {
      field.setSelectionRange(start, start);
      return true;
    }
    field.value = field.value.slice(0, index) + field.value.slice(index + 1);
    var caret = direction < 0 ? index : start;
    field.setSelectionRange(caret, caret);
    applyMobile(field);
    return true;
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
    var mask = field.dataset.phoneInput === "ru-mobile" ? applyMobile : apply;
    field.addEventListener("input", function () {
      mask(field);
    });
    field.addEventListener("blur", function () {
      mask(field);
    });
    if (field.dataset.phoneInput === "ru-mobile") {
      field.addEventListener("keydown", function (event) {
        if (event.key === "Backspace" && removeMobileDigit(field, -1)) {
          event.preventDefault();
        } else if (event.key === "Delete" && removeMobileDigit(field, 1)) {
          event.preventDefault();
        }
      });
    }
    if (field.value) {
      mask(field);
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
    module.exports = {
      maskValue: maskValue,
      nationalDigits: nationalDigits,
      mobileMaskValue: mobileMaskValue,
      mobileNationalDigits: mobileNationalDigits,
    };
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
