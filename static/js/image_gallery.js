// Слой 24 (UI) — переключение основного фото по клику на миниатюру.
// Только просмотр в браузере: НЕ меняет is_primary в БД (для этого — форма
// «Сделать основным»). Без зависимостей; страница без JS остаётся рабочей.
(function () {
  "use strict";

  function initBlock(block) {
    var main = block.querySelector("[data-gallery-main]");
    if (!main) {
      return;
    }
    var thumbs = block.querySelectorAll("[data-gallery-thumb]");

    thumbs.forEach(function (thumb) {
      thumb.addEventListener("click", function () {
        var full = thumb.getAttribute("data-full");
        if (!full) {
          return;
        }
        // Сменить только просмотр основного изображения.
        main.setAttribute("src", full);
        main.setAttribute("alt", thumb.getAttribute("data-alt") || "Фото");

        // Перенести visual-состояние «выбрано для просмотра».
        block.querySelectorAll(".photo-thumb--selected").forEach(function (figure) {
          figure.classList.remove("photo-thumb--selected");
        });
        thumbs.forEach(function (other) {
          other.setAttribute("aria-current", "false");
        });
        var figure = thumb.closest(".photo-thumb");
        if (figure) {
          figure.classList.add("photo-thumb--selected");
        }
        thumb.setAttribute("aria-current", "true");
      });
    });
  }

  function init() {
    document.querySelectorAll("[data-image-gallery]").forEach(initBlock);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
