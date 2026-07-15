(function () {
  "use strict";

  function decimal(value) {
    const normalized = String(value || "").trim().replace(",", ".");
    if (!/^\d+(?:\.\d+)?$/.test(normalized)) return null;
    const parts = normalized.split(".");
    const fraction = parts[1] || "";
    return {
      units: BigInt(parts[0] + fraction),
      scale: 10n ** BigInt(fraction.length),
    };
  }

  function wholeRubles(usdValue, rateValue, markupValue) {
    const usd = decimal(usdValue);
    const rate = decimal(rateValue);
    const markup = decimal(markupValue);
    if (!usd || !rate || !markup) return null;

    const markupFactor = 100n * markup.scale + markup.units;
    const numerator = usd.units * rate.units * markupFactor;
    const denominator = usd.scale * rate.scale * markup.scale * 100n;
    return (numerator + denominator / 2n) / denominator;
  }

  function init(form) {
    if (!form || form.dataset.priceSettingsReady === "true") return;
    const rate = form.querySelector("#id_current_usd_rate");
    const markups = {
      brp: form.querySelector("#id_brp_markup_percent"),
      polaris: form.querySelector("#id_polaris_markup_percent"),
    };
    const outputs = form.querySelectorAll("[data-price-example]");
    if (!rate || !markups.brp || !markups.polaris || !outputs.length) return;

    const render = function () {
      outputs.forEach(function (output) {
        const markup = markups[output.dataset.priceExample];
        const result = wholeRubles(output.dataset.usdPrice, rate.value, markup.value);
        output.textContent = result === null ? "0" : result.toString();
      });
    };
    [rate, markups.brp, markups.polaris].forEach(function (input) {
      input.addEventListener("input", render);
    });
    form.dataset.priceSettingsReady = "true";
    render();
  }

  function initAll(root) {
    (root || document).querySelectorAll("[data-price-settings-form]").forEach(init);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initAll(document);
  });
  document.addEventListener("denstock:page-loaded", function (event) {
    initAll(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
