// Shared page utilities. Loaded from base.html before every page script.
// ES5 on purpose — same convention as the big page scripts (no build step).
(function () {
  "use strict";

  // fetch that surfaces HTML error pages readably instead of as a
  // SyntaxError, and turns 401 into an actionable message.
  async function fetchJson(url, opts) {
    var resp = await fetch(url, opts);
    var raw = await resp.text();
    var data;
    try { data = JSON.parse(raw); }
    catch (e) { throw new Error("HTTP " + resp.status + " — " + raw.slice(0, 200)); }
    if (resp.status === 401) {
      throw new Error("Not authorized — open Frigate and log in first, then reload this page.");
    }
    if (!resp.ok) {
      var detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data));
    }
    return data;
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  // Returns a showBanner(text, isError) bound to a page's banner element.
  function banner(node) {
    return function (text, isError) {
      node.textContent = text;
      node.style.display = "block";
      node.style.color = isError ? "var(--warn, #e8a735)" : "var(--muted)";
    };
  }

  // Small modal dialog: a title, content nodes, and action buttons (each
  // {label, kind, onclick}; kind defaults to "btn-neutral", pass "btn-danger"
  // for the destructive action). Markup/classes are the map editor's
  // .me-overlay/.me-dialog/.me-actions (triage.css) -- shared here so a
  // confirm-before-disruptive-action dialog isn't reimplemented per page.
  function dialog(title, contentNodes, actions) {
    var buttons = actions.map(function (a) {
      return el("button", { class: "btn " + (a.kind || "btn-neutral"), text: a.label }, []);
    });
    var overlay = el("div", { class: "me-overlay" }, [
      el("div", { class: "me-dialog" },
        [el("h3", { text: title }, [])].concat(contentNodes, [
          el("div", { class: "me-actions" }, buttons),
        ])),
    ]);
    actions.forEach(function (a, i) {
      buttons[i].addEventListener("click", async function () {
        if (a.onclick) await a.onclick();
        overlay.remove();
      });
    });
    overlay.addEventListener("click", function (e) { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    return overlay;
  }

  // Transient toast, replaces alert(): non-blocking, auto-dismisses.
  // Optional `action` ({text, callback}) adds a clickable button, e.g. undo.
  var toastNode = null;
  var toastTimer = null;
  function toast(msg, isError, action) {
    if (!toastNode) {
      toastNode = el("div", { class: "sc-toast", role: "status" });
      document.body.appendChild(toastNode);
    }
    toastNode.innerHTML = "";
    toastNode.appendChild(el("span", { text: msg }));
    if (action) {
      toastNode.appendChild(el("button", { type: "button", text: action.text }, []));
      toastNode.lastChild.addEventListener("click", function () {
        clearTimeout(toastTimer);
        toastNode.classList.remove("show");
        action.callback();
      });
    }
    toastNode.classList.toggle("error", !!isError);
    toastNode.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastNode.classList.remove("show"); }, 4000);
  }

  // Full-screen media viewer: dark backdrop, content nodes centered, closes
  // on the close button, a backdrop click or Escape. `onKey(ev)` sees every
  // other keydown while open (arrow-key stepping). Returns {overlay, close}.
  function lightbox(contentNodes, opts) {
    opts = opts || {};
    var closeBtn = el("button", { type: "button", class: "sc-lb-close", "aria-label": "Close", text: "\u00d7" }, []);
    var overlay = el("div", { class: "sc-lightbox", role: "dialog", "aria-modal": "true" },
      [closeBtn].concat(contentNodes));
    function onKey(ev) {
      if (ev.key === "Escape") { close(); return; }
      if (opts.onKey) opts.onKey(ev);
    }
    function close() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      if (opts.onClose) opts.onClose();
    }
    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    closeBtn.focus();
    return { overlay: overlay, close: close };
  }

  // Times in the VIEWER's timezone. Server-side fmt_ts renders the box's
  // clock (UTC on the NVR), so pages emit <time data-ts="<epoch s>"
  // data-fmt="when|ago|short"> with that as the no-JS fallback and this
  // rewrites them on load.
  function when(ts) {
    var d = new Date(ts * 1000);
    var sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric",
      year: sameYear ? undefined : "numeric", hour: "numeric", minute: "2-digit",
    });
  }
  function ago(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    if (s < 86400 * 45) return Math.round(s / 86400) + " d ago";
    return Math.round(s / (86400 * 30)) + " mo ago";
  }
  function short(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function localizeTimes(root) {
    (root || document).querySelectorAll("time[data-ts]").forEach(function (t) {
      var ts = parseFloat(t.dataset.ts);
      if (!ts) return;
      var fmt = t.dataset.fmt || "when";
      t.textContent = fmt === "ago" ? ago(ts) : fmt === "short" ? short(ts) : when(ts);
      t.setAttribute("datetime", new Date(ts * 1000).toISOString());
      if (fmt !== "when") t.title = when(ts);
    });
  }
  localizeTimes(document);

  // Reload-vs-overwrite conflict dialog: shared by any page that PUTs a
  // revisioned document and can hit a 409 (stale rev) -- mapedit's
  // inspector.js and zones.js both save /v1/push/settings this way.
  // `onReload` re-pulls the server doc and discards local edits; `onOverwrite`
  // re-saves the local doc against the server's current rev.
  function conflictDialog(opts) {
    return dialog("Settings changed elsewhere while you were editing.", [], [
      { label: "Reload & lose my edits", kind: "btn-neutral", onclick: opts.onReload },
      { label: "Overwrite with mine", kind: "btn-primary", onclick: opts.onOverwrite },
    ]);
  }

  // Triage session tag: shared between the list and detail pages.
  function bindSessionInput() {
    var input = document.getElementById("session");
    if (!input) return;
    input.value = localStorage.getItem("triage_session") || "";
    input.addEventListener("change", function () {
      localStorage.setItem("triage_session", input.value);
    });
  }

  // Nav dropdowns (<details class="nav-group">): close on outside click and
  // Escape, so a stray tap on iOS doesn't leave the menu pinned open.
  document.addEventListener("click", function (ev) {
    document.querySelectorAll("details.nav-group[open]").forEach(function (d) {
      if (!d.contains(ev.target)) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    document.querySelectorAll("details.nav-group[open]").forEach(function (d) {
      d.removeAttribute("open");
    });
  });

  window.SC = {
    fetchJson: fetchJson,
    el: el,
    banner: banner,
    dialog: dialog,
    lightbox: lightbox,
    when: when,
    ago: ago,
    localizeTimes: localizeTimes,
    conflictDialog: conflictDialog,
    toast: toast,
    bindSessionInput: bindSessionInput,
  };
})();
