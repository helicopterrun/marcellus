// Incremental SVG renderer for the map editor.
//
// The scene graph is built ONCE (fixed group order below); every subsequent
// change patches attributes on persistent nodes. Pan/zoom touches only the
// root viewBox. Nodes are never removed while a pointer may be down on them
// (iOS Safari drops the pointer stream of a removed element) — camera groups
// are reconciled by name only when the camera set itself changes.
//
//   g#floorplan   plan <image>, aspect-corrected rotation, dim control
//   g#grid        feet grid (needs map_scale_ft)
//   g#secure      secure-area rect + handles
//   g#overlays    extension point for the later observer round
//   g#cameras     one persistent <g data-cam> per camera
//   g#tool        transient tool artwork (rubber bands, pins, ghosts)
//   g#hud         (reserved) — the north marker + scale bar HUD ended up as
//                 HTML over the stage (screen-space needs no counter-scaling)

import {
  azDir, cardinalOf, floorplanTransform, mapAspect, wedgePath,
} from "./geometry.js";

const NS = "http://www.w3.org/2000/svg";
export const REACH = 0.25; // wedge radius in unit coords (interaction pie)

// Colors, resolved lazily via ElsinoreTokens.cssVar() (tokens.js may not have
// run / the stylesheet may not have painted yet at import time) with the
// current triage.css value baked in as a fallback.
function cssVar(name, fallback) {
  const t = window.ElsinoreTokens;
  return (t && t.cssVar(name)) || fallback;
}
const PALETTE = {
  surface: () => cssVar("--surface", "#1C1D24"),
  muted: () => cssVar("--muted", "#9AA3AB"),
  deep: () => cssVar("--deep", "#0B0C10"),
  ok: () => cssVar("--ok", "#4caf82"),
  warn: () => cssVar("--warn", "#e3b341"),
  accent2: () => cssVar("--accent-2", "#ffb454"),
  text: () => cssVar("--text", "#E8EAED"),
};

function make(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}

export class Renderer {
  constructor(stageEl, store, view) {
    this.store = store;
    this.view = view;
    this.selection = null; // {kind:"camera", camera} | {kind:"secure"} | null
    this.dimFloorplan = parseFloat(localStorage.getItem("me_dim") || "0.35");

    this.svg = make("svg", { class: "me-svg" }, stageEl);
    this.svg.setAttribute("preserveAspectRatio", "none");
    this.gFloorplan = make("g", { id: "me-floorplan" }, this.svg);
    this.gGrid = make("g", { id: "me-grid" }, this.svg);
    this.gSecure = make("g", { id: "me-secure" }, this.svg);
    this.gOverlays = make("g", { id: "me-overlays" }, this.svg);
    this.gCameras = make("g", { id: "me-cameras" }, this.svg);
    this.gTool = make("g", { id: "me-tool" }, this.svg);
    this.gHud = make("g", { id: "me-hud" }, this.svg);

    this.fpImage = null;
    this.camNodes = new Map(); // name -> node bundle
    this.secureNodes = null;
    this._overlays = new Map();

    // HUD lives in HTML over the stage (screen-space, so no counter-scaling):
    // a north marker and a scale bar sized to a round number of feet.
    this.hudNorth = document.createElement("div");
    this.hudNorth.className = "me-hud-n";
    this.hudNorth.textContent = "N";
    stageEl.appendChild(this.hudNorth);
    this.hudScale = document.createElement("div");
    this.hudScale.className = "me-hud-scale";
    this.hudScaleLabel = document.createElement("span");
    this.hudScale.appendChild(this.hudScaleLabel);
    this.hudScale.style.display = "none";
    stageEl.appendChild(this.hudScale);

    view.onChange(() => this.updateViewBox());
    store.subscribe((keys) => this.update(keys));
  }

  setSelection(sel) {
    this.selection = sel;
    for (const name of this.camNodes.keys()) this._updateCamera(name);
    this._updateSecure();
    // Raise the selected camera above its neighbors: an overlapping wedge
    // later in the DOM would otherwise swallow the selected camera's drag
    // handles. Safe here — selection changes only from a completed tap,
    // never mid-drag (moving a node under an active pointer kills the
    // pointer stream on iOS).
    const name = this.selectedCamera();
    const n = name && this.camNodes.get(name);
    if (n && n.g.parentNode === this.gCameras && this.gCameras.lastChild !== n.g) {
      this.gCameras.appendChild(n.g);
    }
  }

  selectedCamera() {
    return this.selection?.kind === "camera" ? this.selection.camera : null;
  }

  // Numbered map pins for the landmark tool (transient, live in g#tool).
  setToolPins(pins) {
    this._toolPins = pins || [];
    this._updateToolPins();
  }

  _updateToolPins() {
    if (!this._pinsG) {
      this._pinsG = make("g", {}, this.gTool);
      this._pinsG.style.pointerEvents = "none";
    }
    this._pinsG.textContent = "";
    const sz = (v) => this.view.sz(v);
    for (const p of this._toolPins || []) {
      make("circle", {
        cx: p.x, cy: p.y, r: sz(0.012),
        fill: p.pending ? PALETTE.warn() : PALETTE.ok(),
        stroke: PALETTE.deep(), "stroke-width": sz(0.003),
      }, this._pinsG);
      const t = make("text", {
        x: p.x, y: p.y + sz(0.008), "text-anchor": "middle",
        "font-size": sz(0.02), fill: PALETTE.deep(),
        "font-weight": "700",
      }, this._pinsG);
      t.textContent = String(p.label);
    }
  }

  // Alignment guides while dragging a camera: full-length dashed lines
  // through the coordinate the drag just locked onto. Transient (g#tool).
  setGuides(guides) {
    if (!this._guidesG) {
      this._guidesG = make("g", {}, this.gTool);
      this._guidesG.style.pointerEvents = "none";
    }
    this._guidesG.textContent = "";
    const sz = (v) => this.view.sz(v);
    for (const g of guides || []) {
      make("line", {
        x1: g.axis === "x" ? g.at : 0, y1: g.axis === "x" ? 0 : g.at,
        x2: g.axis === "x" ? g.at : 1, y2: g.axis === "x" ? 1 : g.at,
        stroke: PALETTE.ok(), "stroke-opacity": "0.9",
        "stroke-width": sz(0.0025),
        "stroke-dasharray": `${sz(0.01)} ${sz(0.006)}`,
      }, this._guidesG);
    }
  }

  // Overlay extension point for the later observer round.
  registerOverlay(ov) {
    const g = make("g", { "data-overlay": ov.id }, this.gOverlays);
    this._overlays.set(ov.id, { ov, g });
    ov.mount(g);
  }

  updateViewBox() {
    const v = this.view.view;
    this.svg.setAttribute(
      "viewBox", `${v.x} ${v.y} ${v.w} ${this.view.viewH()}`,
    );
    // sz() depends on stage width only, but fullscreen/resize lands here too.
    this.updateAll();
  }

  update(keys) {
    if (!keys || keys.has("floorplan") || keys.has("map_scale_ft")) {
      this._updateFloorplan();
      this._updateGrid();
      this._updateHud();
    }
    if (!keys || keys.has("secure_area")) this._updateSecure();
    if (!keys || keys.has("camera_layout") || keys.has("camera_optics")) {
      this._reconcileCameras();
    }
    for (const { ov } of this._overlays.values()) {
      ov.update(this.store.doc, this.view.view);
    }
  }

  updateAll() {
    this._updateFloorplan();
    this._updateGrid();
    this._updateSecure();
    this._reconcileCameras();
    this._updateToolPins();
    this._updateHud();
  }

  // Scale bar: pick the round feet value whose on-screen bar lands nearest
  // ~90px at the current zoom. Hidden until the map has a scale.
  _updateHud() {
    const doc = this.store.doc;
    const scale = doc && doc.map_scale_ft;
    const w = this.view.stageEl.clientWidth;
    if (!scale || !w) { this.hudScale.style.display = "none"; return; }
    const ftPerPx = (this.view.view.w * scale) / w;
    const targetFt = ftPerPx * 90;
    const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000];
    const ft = NICE.find((v) => v >= targetFt) || NICE[NICE.length - 1];
    this.hudScale.style.display = "";
    this.hudScale.style.width = (ft / ftPerPx).toFixed(1) + "px";
    this.hudScaleLabel.textContent = `${ft} ft`;
  }

  setDim(v) {
    this.dimFloorplan = v;
    localStorage.setItem("me_dim", String(v));
    this._updateFloorplan();
  }

  // ---- Floorplan -------------------------------------------------------

  _updateFloorplan() {
    const doc = this.store.doc;
    const fp = doc && doc.floorplan;
    if (!fp) {
      if (this.fpImage) { this.fpImage.remove(); this.fpImage = null; }
      this.gFloorplan.style.display = "none";
      return;
    }
    this.gFloorplan.style.display = "";
    if (!this.fpImage) {
      this.fpImage = make("image", {
        x: 0, y: 0, width: 1, height: 1,
        preserveAspectRatio: "none",
      }, this.gFloorplan);
      this.fpImage.style.pointerEvents = "none";
    }
    const href = "/v1/push/floorplan?v=" + (fp.uploaded_at || 0);
    if (this.fpImage.getAttribute("href") !== href) {
      this.fpImage.setAttribute("href", href);
    }
    const t = floorplanTransform(doc);
    if (t) this.fpImage.setAttribute("transform", t);
    else this.fpImage.removeAttribute("transform");
    // Content is ALWAYS the unit square: y 0..1 spans the plan's full height
    // (layout coords, secure_area and the server's ground math all assume
    // this); the viewBox height (view.viewH) is what corrects the aspect.
    this.fpImage.setAttribute("height", 1);
    // Dim = how much the plan recedes so overlays read on light drawings.
    this.fpImage.setAttribute("opacity", String(1 - this.dimFloorplan));
  }

  // ---- Grid ------------------------------------------------------------

  gridFt() {
    return parseFloat(localStorage.getItem("me_grid_ft") || "0") || 0;
  }

  setGridFt(ft) {
    localStorage.setItem("me_grid_ft", String(ft));
    this._updateGrid();
  }

  _updateGrid() {
    const doc = this.store.doc;
    this.gGrid.textContent = "";
    const scale = doc && doc.map_scale_ft;
    const ft = this.gridFt();
    if (!scale || !ft) return;
    const stepX = ft / scale;
    const aspect = mapAspect(doc);
    if (stepX < 0.004) return; // denser than ~250 lines: illegible, skip
    const w = this.view.sz(0.0012);
    for (let x = 0; x <= 1 + 1e-9; x += stepX) {
      make("line", {
        x1: x, y1: 0, x2: x, y2: 1,
        stroke: PALETTE.muted(), "stroke-opacity": "0.25",
        "stroke-width": w,
      }, this.gGrid);
    }
    // Square feet: a unit-y step covers scale·aspect ft, so divide it out.
    const stepY = stepX / aspect;
    for (let y = 0; y <= 1 + 1e-9; y += stepY) {
      make("line", {
        x1: 0, y1: y, x2: 1, y2: y,
        stroke: PALETTE.muted(), "stroke-opacity": "0.25",
        "stroke-width": w,
      }, this.gGrid);
    }
    this.gGrid.style.pointerEvents = "none";
  }

  // ---- Secure area -----------------------------------------------------

  _updateSecure() {
    const sa = this.store.doc && this.store.doc.secure_area;
    if (!sa) {
      if (this.secureNodes) {
        this.secureNodes.g.style.display = "none";
      }
      return;
    }
    if (!this.secureNodes) {
      const g = make("g", {}, this.gSecure);
      const rect = make("rect", {
        fill: PALETTE.ok(), "fill-opacity": "0.07",
        stroke: PALETTE.ok(), "stroke-opacity": "0.85",
        "data-hit": "secure-rect",
      }, g);
      rect.style.cursor = "pointer";
      const handles = [];
      for (let i = 0; i < 8; i++) {
        const h = make("rect", {
          fill: PALETTE.ok(), stroke: PALETTE.deep(),
          "data-hit": "secure-handle", "data-handle": String(i),
        }, g);
        h.style.display = "none";
        handles.push(h);
      }
      this.secureNodes = { g, rect, handles };
    }
    const n = this.secureNodes;
    n.g.style.display = "";
    const x0 = Math.min(sa.x0, sa.x1), x1 = Math.max(sa.x0, sa.x1);
    const y0 = Math.min(sa.y0, sa.y1), y1 = Math.max(sa.y0, sa.y1);
    n.rect.setAttribute("x", x0); n.rect.setAttribute("y", y0);
    n.rect.setAttribute("width", x1 - x0); n.rect.setAttribute("height", y1 - y0);
    n.rect.setAttribute("stroke-width", this.view.sz(0.004));
    n.rect.setAttribute("stroke-dasharray",
      `${this.view.sz(0.014)} ${this.view.sz(0.008)}`);
    const sel = this.selection?.kind === "secure";
    // 8 handles: corners then edge midpoints (N E S W), shown when selected.
    const hs = this.view.sz(0.016);
    const pos = [
      [x0, y0], [x1, y0], [x1, y1], [x0, y1],
      [(x0 + x1) / 2, y0], [x1, (y0 + y1) / 2],
      [(x0 + x1) / 2, y1], [x0, (y0 + y1) / 2],
    ];
    n.handles.forEach((h, i) => {
      h.style.display = sel ? "" : "none";
      h.setAttribute("x", pos[i][0] - hs / 2);
      h.setAttribute("y", pos[i][1] - hs / 2);
      h.setAttribute("width", hs); h.setAttribute("height", hs);
      h.setAttribute("stroke-width", this.view.sz(0.002));
      h.style.cursor = ["nwse-resize", "nesw-resize", "nwse-resize", "nesw-resize",
        "ns-resize", "ew-resize", "ns-resize", "ew-resize"][i];
    });
  }

  // ---- Cameras ---------------------------------------------------------

  _reconcileCameras() {
    const names = this.store.availableCameras;
    for (const name of names) {
      if (!this.camNodes.has(name)) this._buildCamera(name);
    }
    for (const [name, n] of this.camNodes) {
      if (!names.includes(name)) { n.g.remove(); this.camNodes.delete(name); }
    }
    for (const name of names) this._updateCamera(name);
  }

  _buildCamera(name) {
    const g = make("g", { "data-cam": name }, this.gCameras);
    // Wedge fill: also the click-to-select surface.
    const wedge = make("path", {
      fill: PALETTE.accent2(), stroke: "none", "data-hit": "wedge",
    }, g);
    wedge.style.pointerEvents = "fill";
    // FOV edges: visible dashed line + invisible fat grab line per side.
    const edges = [], grabs = [];
    for (let i = 0; i < 2; i++) {
      edges.push(make("line", {
        stroke: PALETTE.accent2(), "stroke-opacity": "0.6",
      }, g));
      const grab = make("line", {
        stroke: "transparent", "data-hit": "fov-edge", "data-edge": String(i),
      }, g);
      grab.style.cursor = "col-resize";
      grabs.push(grab);
    }
    // Aim handle: a knob on the wedge's mid-arc — drag to swing azimuth.
    const aimStem = make("line", {
      stroke: PALETTE.accent2(), "stroke-opacity": "0.5",
    }, g);
    const aim = make("circle", {
      fill: PALETTE.accent2(), stroke: PALETTE.deep(),
      "data-hit": "aim",
    }, g);
    aim.style.cursor = "grab";
    // Selection ring under the body.
    const ring = make("circle", {
      fill: "none", stroke: PALETTE.accent2(), "stroke-opacity": "0.8",
    }, g);
    // Camera body + fat move-hit circle.
    const body = make("circle", {
      stroke: PALETTE.surface(),
    }, g);
    const hit = make("circle", { fill: "transparent", "data-hit": "body" }, g);
    hit.style.cursor = "move";
    hit.style.touchAction = "none";
    // Label with halo (paint-order) + lock glyph.
    const label = make("text", {
      fill: PALETTE.text(), "text-anchor": "middle",
      "paint-order": "stroke", stroke: PALETTE.deep(),
      "stroke-opacity": "0.85",
    }, g);
    label.style.pointerEvents = "none";
    label.style.fontFamily = "inherit";
    this.camNodes.set(name, {
      g, wedge, edges, grabs, aimStem, aim, ring, body, hit, label,
    });
  }

  camGeom(name) {
    const doc = this.store.doc;
    const layout = (doc.camera_layout || {})[name];
    if (!layout || layout.x === undefined) return null;
    const optics = (doc.camera_optics || {})[name] || {};
    const az = layout.azimuth;
    return {
      x: layout.x, y: layout.y,
      az: az === undefined ? null : az,
      fov: layout.fov || optics.hfov || 90,
      locked: !!layout.locked,
    };
  }

  _updateCamera(name) {
    const n = this.camNodes.get(name);
    if (!n) return;
    const geo = this.camGeom(name);
    if (!geo) { n.g.style.display = "none"; return; }
    n.g.style.display = "";
    const sz = (v) => this.view.sz(v);
    const selName = this.selectedCamera();
    const selected = selName === name;
    n.g.setAttribute("opacity", selName && !selected ? "0.3" : "1");

    const hasAim = geo.az !== null;
    if (hasAim) {
      n.wedge.style.display = "";
      n.wedge.setAttribute("d", wedgePath(geo, geo.az, geo.fov, REACH));
      n.wedge.setAttribute("fill-opacity", selected ? "0.26" : "0.13");
      const dirs = [geo.az - geo.fov / 2, geo.az + geo.fov / 2].map(azDir);
      dirs.forEach((d, i) => {
        const ex = geo.x + d.x * REACH, ey = geo.y + d.y * REACH;
        for (const line of [n.edges[i], n.grabs[i]]) {
          line.setAttribute("x1", geo.x); line.setAttribute("y1", geo.y);
          line.setAttribute("x2", ex); line.setAttribute("y2", ey);
          line.style.display = "";
        }
        n.edges[i].setAttribute("stroke-width", sz(0.0035));
        n.edges[i].setAttribute("stroke-dasharray", `${sz(0.012)} ${sz(0.008)}`);
        n.grabs[i].setAttribute("stroke-width", sz(0.035));
        n.grabs[i].style.pointerEvents = geo.locked || !selected ? "none" : "stroke";
      });
      const mid = azDir(geo.az);
      const ax = geo.x + mid.x * REACH, ay = geo.y + mid.y * REACH;
      n.aimStem.setAttribute("x1", geo.x); n.aimStem.setAttribute("y1", geo.y);
      n.aimStem.setAttribute("x2", ax); n.aimStem.setAttribute("y2", ay);
      n.aimStem.setAttribute("stroke-width", sz(0.002));
      n.aimStem.removeAttribute("stroke-dasharray");
      n.aimStem.style.display = selected ? "" : "none";
      n.aim.setAttribute("cx", ax); n.aim.setAttribute("cy", ay);
      n.aim.setAttribute("r", sz(selected ? 0.016 : 0.011));
      n.aim.setAttribute("stroke-width", sz(0.004));
      n.aim.style.display = geo.locked ? "none" : "";
      n.aim.style.pointerEvents = geo.locked ? "none" : "fill";
    } else {
      n.wedge.style.display = "none";
      n.edges.forEach((e) => { e.style.display = "none"; });
      n.grabs.forEach((e) => { e.style.display = "none"; });
      // No azimuth yet: a selected, unlocked camera still gets a ghost aim
      // knob (pointing north) so aiming it the first time is one drag.
      if (selected && !geo.locked) {
        const ax = geo.x, ay = geo.y - REACH;
        n.aimStem.setAttribute("x1", geo.x); n.aimStem.setAttribute("y1", geo.y);
        n.aimStem.setAttribute("x2", ax); n.aimStem.setAttribute("y2", ay);
        n.aimStem.setAttribute("stroke-width", sz(0.002));
        n.aimStem.setAttribute("stroke-dasharray", `${sz(0.01)} ${sz(0.008)}`);
        n.aimStem.style.display = "";
        n.aim.setAttribute("cx", ax); n.aim.setAttribute("cy", ay);
        n.aim.setAttribute("r", sz(0.016));
        n.aim.setAttribute("stroke-width", sz(0.004));
        n.aim.style.display = "";
        n.aim.style.pointerEvents = "fill";
      } else {
        n.aim.style.display = "none";
        n.aimStem.style.display = "none";
      }
    }

    n.ring.setAttribute("cx", geo.x); n.ring.setAttribute("cy", geo.y);
    n.ring.setAttribute("r", sz(0.024));
    n.ring.setAttribute("stroke-width", sz(0.004));
    n.ring.style.display = selected ? "" : "none";

    n.body.setAttribute("cx", geo.x); n.body.setAttribute("cy", geo.y);
    n.body.setAttribute("r", sz(selected ? 0.013 : 0.010));
    n.body.setAttribute("stroke-width", sz(0.0035));
    n.body.setAttribute("fill", selected
      ? PALETTE.accent2() : PALETTE.muted());
    n.hit.setAttribute("cx", geo.x); n.hit.setAttribute("cy", geo.y);
    n.hit.setAttribute("r", sz(0.03));
    n.hit.style.cursor = geo.locked ? "default" : "move";

    n.label.setAttribute("x", geo.x);
    n.label.setAttribute("y", geo.y + sz(0.052));
    n.label.setAttribute("font-size", sz(0.024));
    n.label.setAttribute("stroke-width", sz(0.006));
    n.label.textContent = (geo.locked ? "🔒 " : "") + name +
      (hasAim && selected ? ` · ${Math.round(geo.az)}° ${cardinalOf(geo.az)}` : "");
  }
}
