(function () {
  "use strict";

  var SIDEBAR_KEY = "denstock.sidebar.scrollTop.v1";
  var sidebar = document.getElementById("app-sidebar");
  var content = document.getElementById("content");
  var controller = null;

  function storageGet(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (error) {
      // Private mode or a disabled storage backend must not break navigation.
    }
  }

  function saveSidebarScroll() {
    if (sidebar) storageSet(SIDEBAR_KEY, String(sidebar.scrollTop));
  }

  function keepActiveLinkVisible() {
    if (!sidebar) return;
    var active = sidebar.querySelector('[aria-current="page"]');
    if (!active) return;
    var top = active.offsetTop;
    var bottom = top + active.offsetHeight;
    if (top < sidebar.scrollTop) {
      sidebar.scrollTop = top;
    } else if (bottom > sidebar.scrollTop + sidebar.clientHeight) {
      sidebar.scrollTop = bottom - sidebar.clientHeight;
    }
  }

  function restoreSidebarScroll() {
    if (!sidebar) return;
    var saved = storageGet(SIDEBAR_KEY);
    if (saved !== null && /^\d+$/.test(saved)) {
      sidebar.scrollTop = parseInt(saved, 10);
    }
    keepActiveLinkVisible();
  }

  function normalizedHref(link) {
    try {
      return new URL(link.href, window.location.href);
    } catch (error) {
      return null;
    }
  }

  function shouldHandle(event, link) {
    if (!link || event.defaultPrevented || event.button !== 0) return false;
    if (event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) return false;
    if (link.target || link.hasAttribute("download")) return false;
    if (link.hasAttribute("data-full-navigation")) return false;
    var target = normalizedHref(link);
    if (!target || target.origin !== window.location.origin) return false;
    if (!/^https?:$/.test(target.protocol)) return false;
    if (target.pathname === window.location.pathname && target.search === window.location.search) {
      return false;
    }
    return !target.hash;
  }

  function setBusy(busy) {
    if (!content) return;
    content.setAttribute("aria-busy", busy ? "true" : "false");
    content.classList.toggle("content--loading", busy);
  }

  function fullNavigation(url) {
    window.location.assign(url);
  }

  function updateActiveNavigation(incomingSidebar) {
    if (!sidebar || !incomingSidebar) return;
    var incomingActive = incomingSidebar.querySelector('[aria-current="page"]');
    var activeHref = incomingActive ? incomingActive.getAttribute("href") : null;
    sidebar.querySelectorAll("a.nav__link").forEach(function (link) {
      var active = activeHref !== null && link.getAttribute("href") === activeHref;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function replacePage(parsed, url, push) {
    var incomingContent = parsed.querySelector("#content");
    var incomingSidebar = parsed.querySelector("#app-sidebar");
    var title = parsed.querySelector("title");
    if (!incomingContent || !incomingSidebar || !title) return false;

    content.replaceChildren.apply(content, Array.from(incomingContent.childNodes));
    document.title = title.textContent;
    updateActiveNavigation(incomingSidebar);
    if (push) {
      window.history.pushState({ denstockPartial: true }, "", url);
    }
    var toggle = document.getElementById("nav-toggle");
    if (toggle) toggle.checked = false;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    content.focus({ preventScroll: true });
    document.dispatchEvent(
      new CustomEvent("denstock:page-loaded", { detail: { root: content, url: url } })
    );
    saveSidebarScroll();
    return true;
  }

  function navigate(url, push) {
    if (!window.fetch || !window.DOMParser || !window.AbortController) {
      fullNavigation(url);
      return;
    }
    if (controller) controller.abort();
    controller = new AbortController();
    var current = controller;
    setBusy(true);
    window
      .fetch(url, {
        method: "GET",
        credentials: "same-origin",
        headers: {
          Accept: "text/html",
          "X-DenStock-Partial": "content",
        },
        signal: current.signal,
      })
      .then(function (response) {
        var type = response.headers.get("Content-Type") || "";
        if (!response.ok || response.redirected || type.indexOf("text/html") === -1) {
          throw new Error("Incompatible response");
        }
        return response.text();
      })
      .then(function (html) {
        if (current !== controller) return;
        var parsed = new DOMParser().parseFromString(html, "text/html");
        if (!replacePage(parsed, url, push)) throw new Error("Missing application shell");
      })
      .catch(function (error) {
        if (error.name !== "AbortError" && current === controller) fullNavigation(url);
      })
      .finally(function () {
        if (current === controller) {
          controller = null;
          setBusy(false);
        }
      });
  }

  if (sidebar && content) {
    restoreSidebarScroll();
    sidebar.addEventListener("scroll", saveSidebarScroll, { passive: true });
    sidebar.addEventListener("click", function (event) {
      var link = event.target.closest("a.nav__link");
      if (!shouldHandle(event, link)) return;
      event.preventDefault();
      navigate(link.href, true);
    });
    content.addEventListener("click", function (event) {
      var link = event.target.closest("a[data-partial-link]");
      if (!shouldHandle(event, link)) return;
      event.preventDefault();
      navigate(link.href, true);
    });
    window.addEventListener("beforeunload", saveSidebarScroll);
    window.addEventListener("popstate", function () {
      navigate(window.location.href, false);
    });
    window.history.replaceState({ denstockPartial: true }, "", window.location.href);
  }

  window.DenStockNavigation = {
    sidebarKey: SIDEBAR_KEY,
    navigate: navigate,
    shouldHandle: shouldHandle,
  };
})();
