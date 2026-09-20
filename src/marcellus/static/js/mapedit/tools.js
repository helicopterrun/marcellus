// Tool state machine + pointer routing for the map editor.
//
// One active tool owns the map's pointer gestures. Every drag begins with a
// pointerdown on the SVG and is then tracked on WINDOW-scoped move/up
// listeners — never setPointerCapture: iOS Safari drops a captured pointer
// stream if the touched element is re-rendered away.
//
// Hit-testing rides on data-* attributes the renderer stamps on its nodes:
//   data-cam on the camera group; data-hit = wedge | body | aim | fov-edge |
//   secure-rect | secure-handle (data-handle 0..7).

import {
  CARDINAL_DEG, clamp01, dirAz, normAngle, snap,
} from "./geometry.js";

const TAP_PX = 6;

export class Tools {
  constructor(store, view, renderer, callbacks) {
    this.store = store;
    this.view = view;
    this.renderer = renderer;
    this.cb = callbacks; // {onSelect(sel), onToolChange(name)}
    this.tool = "select";
    this.drag = null;
    renderer.svg.style.touchAction = "none";
    renderer.svg.addEventListener("pointerdown", (ev) => this._down(ev));
  }

  setTool(name) {
    if (this.drag) this._endDrag(false);
    this.tool = name;
    this.cb.onToolChange(name);
  }

  select(sel) {
    this.renderer.setSelection(sel);
    this.cb.onSelect(sel);
  }

  cancel() {
    // Esc: abort a live drag (reverting it), else deselect / leave the tool.
    if (this.drag) { this._endDrag(true); return; }
    if (this.tool !== "select") { this.setTool("select"); return; }
    this.select(null);
  }

  // Snap step in unit coords from the grid setting (Shift bypasses at the
  // call site). Without a map scale a fine fixed step keeps coords tidy.
  _gridStep() {
    const scale = this.store.doc.map_scale_ft;
    const ft = this.renderer.gridFt();
    return scale && ft ? ft / scale : 0.005;
  }

  _snapAngle(az, ev, faces) {
    if (ev.altKey) return normAngle(az);
    let out = snap(normAngle(az), 5);
    // Compass magnet: within 2° of the camera's published facing direction.
    const target = faces !== undefined ? CARDINAL_DEG[faces] : undefined;
    if (target !== undefined) {
      const d = Math.abs(((az - target + 540) % 360) - 180);
      if (d <= 2) out = target;
    }
    return normAngle(out);
  }

  _locked(cam) {
    const e = (this.store.doc.camera_layout || {})[cam];
    return !!(e && e.locked);
  }

  _down(ev) {
    if (this.view.pinchActive || this.drag) return;
    if (ev.button !== undefined && ev.button > 1) return;
    const t = ev.target;
    const hit = t.getAttribute && t.getAttribute("data-hit");
    const camG = t.closest && t.closest("[data-cam]");
    const cam = camG ? camG.getAttribute("data-cam") : null;

    if (this.tool === "area") { this._startSecureDraw(ev); return; }
    if (this.tool === "scale") { this._startScaleDraw(ev); return; }
    if (this.tool === "landmark") {
      // Pan still works while calibrating; a tap places the map half of a
      // landmark pair.
      this._startPanOrTap(ev, () => this.cb.onLandmarkMapClick(this.view.clientToUnit(ev)));
      return;
    }
    if (this.tool !== "select") return;

    if (cam && hit === "body" && !this._locked(cam)) {
      this._startCameraMove(ev, cam); return;
    }
    if (cam && (hit === "aim") && !this._locked(cam)) {
      this._startAim(ev, cam); return;
    }
    if (cam && hit === "fov-edge" && !this._locked(cam)) {
      this._startFov(ev, cam); return;
    }
    if (cam) { this._startPanOrTap(ev, () => this.select({ kind: "camera", camera: cam })); return; }
    if (hit === "secure-handle") {
      this._startSecureResize(ev, parseInt(t.getAttribute("data-handle"), 10)); return;
    }
    if (hit === "secure-rect") {
      this._startPanOrTap(ev, () => this.select({ kind: "secure" })); return;
    }
    this._startPanOrTap(ev, () => this.select(null));
  }

  // ---- Generic drag plumbing ------------------------------------------

  _startDrag(ev, { label, onMove, onTap, onEnd }) {
    ev.preventDefault();
    if (label) this.store.beginGesture(label);
    const start = { x: ev.clientX, y: ev.clientY };
    let moved = false;
    const move = (mv) => {
      if (this.view.pinchActive) return;
      if (!moved && Math.hypot(mv.clientX - start.x, mv.clientY - start.y) < TAP_PX) return;
      moved = true;
      onMove(mv);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      this.drag = null;
      if (label) {
        if (moved) this.store.endGesture();
        else this.store.cancelGesture();
      }
      if (!moved && onTap) onTap();
      if (onEnd) onEnd(moved);
    };
    this.drag = {
      abort: () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
        this.drag = null;
        if (label) this.store.cancelGesture();
        // Aborts still need the cleanup half of onEnd (transient artwork:
        // scale line, alignment guides) — moved=false keeps commits out.
        if (onEnd) onEnd(false);
      },
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }

  _endDrag(revert) {
    if (!this.drag) return;
    if (revert) this.drag.abort();
  }

  _startPanOrTap(ev, onTap) {
    let last = { x: ev.clientX, y: ev.clientY };
    this._startDrag(ev, {
      onMove: (mv) => {
        this.view.panByClient(mv.clientX - last.x, mv.clientY - last.y);
        last = { x: mv.clientX, y: mv.clientY };
      },
      onTap,
    });
  }

  // ---- Camera drags ----------------------------------------------------

  _entry(doc, cam) {
    if (!doc.camera_layout) doc.camera_layout = {};
    if (!doc.camera_layout[cam]) doc.camera_layout[cam] = {};
    return doc.camera_layout[cam];
  }

  _startCameraMove(ev, cam) {
    this.select({ kind: "camera", camera: cam });
    // Other placed cameras' coordinates, for alignment magnets: when the
    // dragged camera comes within ~1% of the view width of a neighbor's x
    // or y, lock onto it and show a guide line (Shift bypasses, like grid).
    const others = Object.entries(this.store.doc.camera_layout || {})
      .filter(([name, e]) => name !== cam && e && e.x !== undefined)
      .map(([, e]) => ({ x: e.x, y: e.y }));
    this._startDrag(ev, {
      label: `move ${cam}`,
      onMove: (mv) => {
        const p = this.view.clientToUnit(mv);
        const step = mv.shiftKey ? 0 : this._gridStep();
        let x = clamp01(snap(p.x, step)), y = clamp01(snap(p.y, step));
        const guides = [];
        if (!mv.shiftKey) {
          const tol = this.view.view.w * 0.01;
          let bx = null, by = null;
          for (const o of others) {
            if (Math.abs(p.x - o.x) < (bx === null ? tol : Math.abs(p.x - bx))) bx = o.x;
            if (Math.abs(p.y - o.y) < (by === null ? tol : Math.abs(p.y - by))) by = o.y;
          }
          if (bx !== null) { x = bx; guides.push({ axis: "x", at: bx }); }
          if (by !== null) { y = by; guides.push({ axis: "y", at: by }); }
        }
        this.renderer.setGuides(guides);
        this.store.mutate((doc) => {
          const e = this._entry(doc, cam);
          e.x = +x.toFixed(4);
          e.y = +y.toFixed(4);
        }, ["camera_layout"]);
      },
      onEnd: () => this.renderer.setGuides([]),
    });
  }

  _startAim(ev, cam) {
    this.select({ kind: "camera", camera: cam });
    const faces = ((this.store.doc.camera_optics || {})[cam] || {}).faces;
    this._startDrag(ev, {
      label: `aim ${cam}`,
      onMove: (mv) => {
        const p = this.view.clientToUnit(mv);
        this.store.mutate((doc) => {
          const e = this._entry(doc, cam);
          if (e.x === undefined) return;
          const dx = p.x - e.x, dy = p.y - e.y;
          if (Math.hypot(dx, dy) < this.view.sz(0.01)) return;
          const az = this._snapAngle(dirAz(dx, dy), mv, faces);
          e.azimuth = +az.toFixed(1);
          if (e.fov === undefined) {
            const optics = (doc.camera_optics || {})[cam];
            e.fov = optics && optics.hfov ? optics.hfov : 90;
          }
        }, ["camera_layout"]);
      },
    });
  }

  _startFov(ev, cam) {
    this.select({ kind: "camera", camera: cam });
    this._startDrag(ev, {
      label: `fov ${cam}`,
      onMove: (mv) => {
        const p = this.view.clientToUnit(mv);
        this.store.mutate((doc) => {
          const e = this._entry(doc, cam);
          if (e.azimuth === undefined) return;
          const az = dirAz(p.x - e.x, p.y - e.y);
          const half = Math.abs(((az - e.azimuth + 540) % 360) - 180);
          let fov = Math.min(360, Math.max(10, half * 2));
          if (!mv.altKey) fov = snap(fov, 5);
          e.fov = +fov.toFixed(1);
        }, ["camera_layout"]);
      },
    });
  }

  // ---- Secure area -----------------------------------------------------

  _startSecureDraw(ev) {
    const origin = this.view.clientToUnit(ev);
    this._startDrag(ev, {
      label: "draw secure area",
      onMove: (mv) => {
        const p = this.view.clientToUnit(mv);
        const step = mv.shiftKey ? 0 : this._gridStep();
        this.store.mutate((doc) => {
          doc.secure_area = {
            x0: +clamp01(snap(origin.x, step)).toFixed(4),
            y0: +clamp01(snap(origin.y, step)).toFixed(4),
            x1: +clamp01(snap(p.x, step)).toFixed(4),
            y1: +clamp01(snap(p.y, step)).toFixed(4),
          };
        }, ["secure_area"]);
      },
      onEnd: (moved) => {
        if (moved) this.select({ kind: "secure" });
        this.setTool("select");
      },
    });
  }

  // ---- Scale calibration ----------------------------------------------
  // Drag a reference line over a feature of known length; the length prompt
  // and the map_scale_ft math live in the inspector (cb.onScaleLine).

  _startScaleDraw(ev) {
    const origin = this.view.clientToUnit(ev);
    const g = this.renderer.gTool;
    const NS = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(NS, "line");
    line.setAttribute("stroke", "var(--accent, #ffb454)");
    line.setAttribute("stroke-width", this.view.sz(0.005));
    line.setAttribute("stroke-dasharray",
      `${this.view.sz(0.014)} ${this.view.sz(0.008)}`);
    line.setAttribute("x1", origin.x); line.setAttribute("y1", origin.y);
    line.setAttribute("x2", origin.x); line.setAttribute("y2", origin.y);
    g.appendChild(line);
    let end = origin;
    this._startDrag(ev, {
      onMove: (mv) => {
        end = this.view.clientToUnit(mv);
        line.setAttribute("x2", end.x); line.setAttribute("y2", end.y);
      },
      onEnd: (moved) => {
        line.remove();
        this.setTool("select");
        if (moved) {
          this.cb.onScaleLine({ x0: origin.x, y0: origin.y, x1: end.x, y1: end.y });
        }
      },
    });
  }

  _startSecureResize(ev, handle) {
    // Handles 0..3 = corners NW NE SE SW; 4..7 = edges N E S W.
    const CORNER = [["x0", "y0"], ["x1", "y0"], ["x1", "y1"], ["x0", "y1"]];
    const EDGE = [["y0"], ["x1"], ["y1"], ["x0"]];
    const keys = handle < 4 ? CORNER[handle] : EDGE[handle - 4];
    this._startDrag(ev, {
      label: "resize secure area",
      onMove: (mv) => {
        const p = this.view.clientToUnit(mv);
        const step = mv.shiftKey ? 0 : this._gridStep();
        this.store.mutate((doc) => {
          const sa = doc.secure_area;
          if (!sa) return;
          for (const k of keys) {
            const v = k[0] === "x" ? p.x : p.y;
            sa[k] = +clamp01(snap(v, step)).toFixed(4);
          }
        }, ["secure_area"]);
      },
    });
  }
}
