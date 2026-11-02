// Settings tabs: show one [data-settings-panel] at a time. The URL hash is the
// state. Without JS every panel stays visible and the nav links are plain
// anchors; panels are hidden only from here, with the `hidden` attribute
// (hidden, not removed, so field values and unsaved edits persist).
(function () {
  var nav = document.getElementById("settings-nav");
  var panels = Array.prototype.slice.call(
    document.querySelectorAll("[data-settings-panel]")
  );
  if (!nav || !panels.length) return;

  var DEFAULT_PANEL = "zones";
  var tabs = Array.prototype.slice.call(nav.querySelectorAll("a[data-tab]"));
  var saveBars = Array.prototype.slice.call(
    document.querySelectorAll("[data-save-panels]")
  );
  var current = null;

  function panelById(id) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].id === id) return panels[i];
    }
    return null;
  }

  function targetFromHash() {
    var raw = (window.location.hash || "").replace(/^#/, "");
    if (!raw) return { panel: panelById(DEFAULT_PANEL), inner: null };
    var id;
    try { id = decodeURIComponent(raw); } catch (e) { id = raw; }
    var el = document.getElementById(id);
    if (!el) return { panel: panelById(DEFAULT_PANEL), inner: null };
    var panel = el.closest("[data-settings-panel]");
    if (!panel) return { panel: panelById(DEFAULT_PANEL), inner: null };
    return { panel: panel, inner: el === panel ? null : el };
  }

  function centerActiveTab(tab) {
    // Horizontal only: never scroll the page vertically for this.
    if (!tab || nav.scrollWidth <= nav.clientWidth) return;
    nav.scrollLeft = tab.offsetLeft - (nav.clientWidth - tab.offsetWidth) / 2;
  }

  function show(panel) {
    var changed = current !== panel;
    panels.forEach(function (p) { p.hidden = p !== panel; });
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-tab") === panel.id;
      t.classList.toggle("active", on);
      if (on) t.setAttribute("aria-current", "true");
      else t.removeAttribute("aria-current");
      if (on) centerActiveTab(t);
    });
    saveBars.forEach(function (bar) {
      var ids = (bar.getAttribute("data-save-panels") || "").split(/\s+/);
      bar.hidden = ids.indexOf(panel.id) === -1;
    });
    current = panel;
    if (changed) {
      // Panels that need a real size to draw can redraw on this.
      document.dispatchEvent(
        new CustomEvent("settings:panelshown", { detail: { id: panel.id, panel: panel } })
      );
    }
  }

  function reveal(inner) {
    // Open the <details> chain around a deep-link target, then scroll to it.
    var node = inner;
    while (node && node !== document.body) {
      if (node.tagName === "DETAILS") node.open = true;
      node = node.parentElement;
    }
    inner.scrollIntoView({ block: "start" });
  }

  function apply() {
    var t = targetFromHash();
    show(t.panel);
    if (t.inner) {
      reveal(t.inner);
      // Content above can still grow once the page scripts render it.
      window.addEventListener("load", function () {
        if (current === t.panel) t.inner.scrollIntoView({ block: "start" });
      }, { once: true });
    } else if (window.location.hash) {
      window.scrollTo(0, 0);
    }
  }

  // Unsaved-edit dots on tabs: the save-state text is the pages' own signal.
  function watchState(stateId, tabIds) {
    var el = document.getElementById(stateId);
    if (!el || !window.MutationObserver) return;
    function sync() {
      var dirty = /unsaved/i.test(el.textContent || "");
      tabs.forEach(function (t) {
        if (tabIds.indexOf(t.getAttribute("data-tab")) !== -1) {
          t.classList.toggle("has-unsaved", dirty);
        }
      });
    }
    new MutationObserver(sync).observe(el, {
      childList: true, characterData: true, subtree: true,
    });
    sync();
  }
  watchState("save-state", ["zones", "routing", "notifications"]);
  watchState("tuning-state", ["tuning"]);

  window.addEventListener("hashchange", apply);
  apply();
})();
