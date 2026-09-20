// Bootstrap for the CAD-style /map editor.

import { Store } from "./state.js";
import { MapView } from "./view.js";
import { Renderer } from "./renderer.js";
import { Tools } from "./tools.js";
import { Inspector } from "./inspector.js";

const stage = document.getElementById("me-stage");
const panel = document.getElementById("me-panel");
const toolbarEl = document.getElementById("me-toolbar");
const banner = document.getElementById("me-banner");

const store = new Store();
const view = new MapView(stage, () => store.doc);
const renderer = new Renderer(stage, store, view);

let inspector = null;
const tools = new Tools(store, view, renderer, {
  onSelect: (sel) => {
    if (inspector) inspector.setSelection(sel);
    // On a phone, selecting something on the map is a request to edit it:
    // raise the sheet out of peek so the detail form is visible.
    if (sel && isPhone() && sheetState === "peek") setSheet("half");
  },
  onToolChange: (name) => {
    syncToolbar(name);
    if (name !== "landmark" && inspector?.landmark) inspector.cancelLandmark();
  },
  onScaleLine: (line) => inspector && inspector.scaleDialog(line),
  onLandmarkMapClick: (p) => inspector && inspector.landmarkMapClick(p),
});
inspector = new Inspector(panel, store, view, renderer, tools);

// ---- Mobile bottom sheet -----------------------------------------------
// Under 820px the inspector becomes a bottom sheet over the map with three
// snap states. The handle drags it; a tap toggles peek <-> half. The classes
// are inert on desktop (all sheet CSS lives inside the media query).

const isPhone = () => window.matchMedia("(max-width: 820px)").matches;
const SHEET_STATES = ["peek", "half", "full"];
let sheetState = "half";

function setSheet(s) {
  sheetState = s;
  for (const st of SHEET_STATES) panel.classList.toggle("me-sheet-" + st, st === s);
}
setSheet("half");

const handle = document.createElement("div");
handle.className = "me-sheet-handle";
handle.appendChild(Object.assign(document.createElement("span"), { className: "me-sheet-pill" }));
panel.prepend(handle);

handle.addEventListener("pointerdown", (ev) => {
  ev.preventDefault();
  const frameH = panel.parentElement.getBoundingClientRect().height;
  const startH = panel.getBoundingClientRect().height;
  const startY = ev.clientY;
  let moved = false;
  panel.classList.add("me-sheet-dragging");
  const move = (mv) => {
    if (!moved && Math.abs(mv.clientY - startY) < 6) return;
    moved = true;
    const h = Math.min(frameH * 0.9, Math.max(64, startH + (startY - mv.clientY)));
    panel.style.height = h + "px";
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    panel.classList.remove("me-sheet-dragging");
    if (!moved) {
      setSheet(sheetState === "peek" ? "half" : "peek");
      return;
    }
    const f = panel.getBoundingClientRect().height / frameH;
    panel.style.height = "";
    setSheet(f < 0.28 ? "peek" : f < 0.65 ? "half" : "full");
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
});

// ---- Toolbar -----------------------------------------------------------

// Stroke icons in the header-nav style (24-box, currentColor, 1.6 width).
const icon = (paths) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const ICONS = {
  select: icon('<path d="M6.5 4.5 18 11.5l-5.4 1.2 2.3 5.4-2.5 1-2.3-5.4-3.6 3.3z"/>'),
  area: icon('<rect x="4.5" y="6" width="15" height="12" rx="1" stroke-dasharray="3.4 2.4"/>'),
  scale: icon('<path d="M4.5 7.5v9M19.5 7.5v9M4.5 12h15M8.5 10v4M12 10v4M15.5 10v4"/>'),
  "zoom-in": icon('<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6 20 20M11 8.6v4.8M8.6 11h4.8"/>'),
  "zoom-out": icon('<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6 20 20M8.6 11h4.8"/>'),
  fit: icon('<path d="M4 8.5V5a1 1 0 0 1 1-1h3.5M20 8.5V5a1 1 0 0 0-1-1h-3.5M4 15.5V19a1 1 0 0 0 1 1h3.5M20 15.5V19a1 1 0 0 1-1 1h-3.5"/><rect x="9" y="9.5" width="6" height="5" rx="0.5"/>'),
  full: icon('<path d="M13.5 4H20v6.5M20 4l-6.5 6.5M10.5 20H4v-6.5M4 20l6.5-6.5"/>'),
};

const TOOL_BUTTONS = [
  { id: "select", title: "Select / move / aim (Esc)" },
  { id: "area", title: "Draw secure area (drag once)" },
  { id: "scale", title: "Calibrate scale: drag a line of known length" },
];
const ACTION_BUTTONS = [
  { id: "zoom-in", title: "Zoom in", run: () => view.zoom(1 / 1.3) },
  { id: "zoom-out", title: "Zoom out", run: () => view.zoom(1.3) },
  { id: "fit", title: "Fit to plan (F, double-click)", run: () => view.fit() },
  { id: "full", title: "Fullscreen map", run: () => view.toggleFull() },
];

function toolButton(id, title, onClick) {
  const b = document.createElement("button");
  b.className = "me-toolbtn";
  b.innerHTML = ICONS[id];
  b.title = title;
  b.setAttribute("aria-label", title);
  b.addEventListener("click", onClick);
  toolbarEl.appendChild(b);
  return b;
}

const toolBtns = new Map();
for (const t of TOOL_BUTTONS) {
  toolBtns.set(t.id, toolButton(t.id, t.title, () => tools.setTool(t.id)));
}
toolbarEl.appendChild(Object.assign(document.createElement("div"), { className: "me-toolgap" }));
for (const a of ACTION_BUTTONS) toolButton(a.id, a.title, a.run);

function syncToolbar(active) {
  for (const [id, b] of toolBtns) b.classList.toggle("active", id === active);
}
syncToolbar("select");

// ---- Keyboard ----------------------------------------------------------

document.addEventListener("keydown", (ev) => {
  const mod = ev.metaKey || ev.ctrlKey;
  if (mod && ev.key.toLowerCase() === "z") {
    ev.preventDefault();
    if (ev.shiftKey) store.redo(); else store.undo();
    return;
  }
  if (ev.key === "Escape") {
    if (view.isFull() && !tools.drag) { view.toggleFull(); return; }
    tools.cancel();
    return;
  }
  if (ev.key.toLowerCase() === "f" && !mod) { view.fit(); return; }
  // Arrow nudges: one grid step (Shift = 5 steps) on the selected camera.
  const sel = renderer.selection;
  if (sel?.kind === "camera" && ev.key.startsWith("Arrow")) {
    const cam = sel.camera;
    const e = (store.doc.camera_layout || {})[cam];
    if (!e || e.locked || e.x === undefined) return;
    ev.preventDefault();
    const step = tools._gridStep() * (ev.shiftKey ? 5 : 1);
    const d = {
      ArrowUp: [0, -step], ArrowDown: [0, step],
      ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    }[ev.key];
    if (!d) return;
    store.edit(`nudge ${cam}`, (doc) => {
      const en = doc.camera_layout[cam];
      en.x = +Math.min(1, Math.max(0, en.x + d[0])).toFixed(4);
      en.y = +Math.min(1, Math.max(0, en.y + d[1])).toFixed(4);
    }, ["camera_layout"]);
  }
});

window.addEventListener("beforeunload", (ev) => {
  if (store.dirty()) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

// ---- Boot --------------------------------------------------------------

(async () => {
  try {
    await store.load();
  } catch (e) {
    banner.style.display = "";
    banner.textContent = "Failed to load settings: " + e.message;
    return;
  }
  stage.classList.remove("skeleton");
  view.fit();
})();
