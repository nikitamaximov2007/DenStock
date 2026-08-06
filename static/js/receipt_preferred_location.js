(function () {
  "use strict";

  function initForm(form) {
    if (form.dataset.receiptLocationGuidanceBound === "1") return;
    var part = form.querySelector("#id_part_type");
    var location = form.querySelector("#id_location");
    var status = form.querySelector("[data-receipt-location-guidance-status]");
    var guidanceUrl = form.getAttribute("data-location-guidance-url");
    if (!part || !location || !status || !guidanceUrl) return;

    form.dataset.receiptLocationGuidanceBound = "1";
    var autoLocationId = "";
    var locationRevision = 0;
    var requestNumber = 0;

    function setStatus(text) {
      status.textContent = text;
    }

    function clearAutoLocation() {
      if (autoLocationId && location.value === autoLocationId) location.value = "";
      autoLocationId = "";
    }

    function messageFor(payload) {
      if (payload.mode === "preferred" && payload.location) {
        return "Подставлена закреплённая ячейка " + payload.location.code + ". Её можно изменить.";
      }
      if (payload.mode === "current" && payload.location) {
        return "Подставлена текущая ячейка " + payload.location.code + ". Её можно изменить.";
      }
      if (payload.mode === "multiple") {
        return "Деталь сейчас находится в нескольких ячейках. Выберите ячейку вручную.";
      }
      if (payload.mode === "preferred_unavailable") {
        return "Закреплённая ячейка недоступна. Выберите активную ячейку вручную.";
      }
      return "Выберите ячейку для новой детали.";
    }

    function loadGuidance() {
      var partId = part.value;
      requestNumber += 1;
      var thisRequest = requestNumber;
      var locationRevisionAtStart = locationRevision;
      clearAutoLocation();
      if (!partId) {
        setStatus("");
        return;
      }

      var url = new URL(guidanceUrl, window.location.href);
      url.searchParams.set("part", partId);
      setStatus("Подбираем ячейку...");
      fetch(url.toString(), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) throw new Error("Receipt location guidance failed");
          return response.json();
        })
        .then(function (payload) {
          if (thisRequest !== requestNumber) return;
          if (payload.location && locationRevisionAtStart === locationRevision) {
            location.value = String(payload.location.id);
            autoLocationId = location.value;
            setStatus(messageFor(payload));
            return;
          }
          if (locationRevisionAtStart !== locationRevision) {
            setStatus("Оставлена выбранная ячейка.");
            return;
          }
          setStatus(messageFor(payload));
        })
        .catch(function () {
          if (thisRequest !== requestNumber) return;
          setStatus("Не удалось подобрать ячейку. Выберите её вручную.");
        });
    }

    part.addEventListener("change", loadGuidance);
    location.addEventListener("change", function () {
      locationRevision += 1;
      if (autoLocationId && location.value !== autoLocationId) autoLocationId = "";
    });
    if (part.value) loadGuidance();
  }

  function init(root) {
    (root || document).querySelectorAll("[data-receipt-location-guidance]").forEach(initForm);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init(document);
  });
  document.addEventListener("denstock:page-loaded", function (event) {
    init(event.detail && event.detail.root ? event.detail.root : document);
  });
})();
