// Viewport for the map editor: the visible window {x, y, w} in unit map
// coords. The stage element is a fixed-shape WINDOW onto the map; the
// rectangular viewBox height derives from the window shape so the floorplan
// keeps its own aspect whatever shape the window is.
//
// Owns the gestures that move the VIEW (wheel zoom, pinch, zoom buttons,
// fullscreen, fit); panning by drag is initiated by the tools layer (drag on
// empty space) via panByClient().

import { mapAspect } from "./geometry.js";

export class MapView {
  constructor(stageEl, getDoc) {
    this.stageEl = stageEl;
    this.getDoc = getDoc;
    this.view = { x: 0, y: 0, w: 1 };
    this._listeners = [];
    // Pinch state — tracked in the capture phase so a pinch wins even when
    // the first finger landed on a camera element; single-finger gestures
    // must check `pinchActive` and go quiet while two fingers are down.
    this.pinchActive = false;
    this._touchPts = {};
    this._pinchStart = null;
    this._bindGestures();
  }

  onChange(fn) { this._listeners.push(fn); }
  _emit() { this._listeners.forEach((fn) => fn(this.view)); }

  winAspect() {
    const r = this.stageEl.getBoundingClientRect();
    return r.width ? r.height / r.width : 1;
  }

  viewH() {
    return (this.view.w * this.winAspect()) / mapAspect(this.getDoc());
  }

  // THE client->unit mapping. Every map gesture goes through here so
  // zoom/pan can never desync a drag.
  clientToUnit(ev) {
    const r = this.stageEl.getBoundingClientRect();
    return {
      x: this.view.x + ((ev.clientX - r.left) / r.width) * this.view.w,
      y: this.view.y + ((ev.clientY - r.top) / r.height) * this.viewH(),
    };
  }

  // Marker/stroke/font size in map units: fixed in MAP space, normalized to
  // a 560px-wide stage so sizes stay familiar whatever the window width.
  sz(v) {
    const w = this.stageEl.clientWidth || 560;
    return v * (560 / w);
  }

  clamp() {
    const v = this.view;
    // 1/8 = 8x in; 3 = zoomed out far enough to see the whole map small.
    v.w = Math.min(3, Math.max(1 / 8, v.w));
    v.x = v.w >= 1 ? (1 - v.w) / 2 : Math.min(1 - v.w, Math.max(0, v.x));
    const h = this.viewH();
    v.y = h >= 1 ? (1 - h) / 2 : Math.min(1 - h, Math.max(0, v.y));
  }

  fit() {
    this.view = { x: 0, y: 0, w: 1 };
    this.clamp();
    this._emit();
  }

  zoom(factor, aroundUnit) {
    const v = this.view;
    const p = aroundUnit || { x: v.x + v.w / 2, y: v.y + this.viewH() / 2 };
    const oldW = v.w;
    v.w *= factor;
    this.clamp();
    // Keep `p` stationary on screen.
    v.x = p.x - (p.x - v.x) * (v.w / oldW);
    v.y = p.y - (p.y - v.y) * (v.w / oldW);
    this.clamp();
    this._emit();
  }

  panByClient(dxPx, dyPx) {
    const r = this.stageEl.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.view.x -= (dxPx / r.width) * this.view.w;
    this.view.y -= (dyPx / r.height) * this.viewH();
    this.clamp();
    this._emit();
  }

  isFull() { return this.stageEl.classList.contains("map-full"); }

  toggleFull() {
    // CSS-class fullscreen (not the Fullscreen API): works on iPhone Safari.
    this.stageEl.classList.toggle("map-full");
    document.body.classList.toggle("map-full-open", this.isFull());
    this.clamp(); // the window shape just changed
    this._emit();
  }

  _bindGestures() {
    const el = this.stageEl;

    el.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      // Proportional to the actual wheel delta, not a fixed step per event:
      // trackpads fire dozens of small events per flick and a fixed 1.15×
      // each made zoom unusably fast. Line/page delta modes normalize to
      // ~pixels; the exponent caps any single event at ~1.09×.
      let dy = ev.deltaY;
      if (ev.deltaMode === 1) dy *= 16;
      else if (ev.deltaMode === 2) dy *= 160;
      dy = Math.max(-45, Math.min(45, dy));
      this.zoom(Math.exp(dy * 0.0019), this.clientToUnit(ev));
    }, { passive: false });

    el.addEventListener("dblclick", () => this.fit());

    const touchIds = () => Object.keys(this._touchPts);

    el.addEventListener("pointerdown", (ev) => {
      if (ev.pointerType !== "touch") return;
      this._touchPts[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
      const ids = touchIds();
      if (ids.length === 2) {
        const a = this._touchPts[ids[0]], b = this._touchPts[ids[1]];
        const rect = el.getBoundingClientRect();
        this.pinchActive = true;
        this._pinchStart = {
          dist: Math.hypot(a.x - b.x, a.y - b.y),
          w: this.view.w,
          mid: {
            x: this.view.x + (((a.x + b.x) / 2 - rect.left) / rect.width) * this.view.w,
            y: this.view.y + (((a.y + b.y) / 2 - rect.top) / rect.height) * this.viewH(),
          },
        };
        ev.stopPropagation();
      }
    }, true);

    window.addEventListener("pointermove", (ev) => {
      if (ev.pointerType !== "touch" || !this._touchPts[ev.pointerId]) return;
      this._touchPts[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
      if (!this.pinchActive) return;
      const ids = touchIds();
      if (ids.length < 2) return;
      const a = this._touchPts[ids[0]], b = this._touchPts[ids[1]];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (dist < 10 || !this._pinchStart.dist) return;
      const rect = el.getBoundingClientRect();
      this.view.w = this._pinchStart.w * (this._pinchStart.dist / dist);
      this.clamp();
      const midClient = {
        x: ((a.x + b.x) / 2 - rect.left) / rect.width,
        y: ((a.y + b.y) / 2 - rect.top) / rect.height,
      };
      this.view.x = this._pinchStart.mid.x - midClient.x * this.view.w;
      this.view.y = this._pinchStart.mid.y - midClient.y * this.viewH();
      this.clamp();
      this._emit();
    });

    const endTouch = (ev) => {
      if (ev.pointerType !== "touch") return;
      delete this._touchPts[ev.pointerId];
      if (touchIds().length < 2) {
        this.pinchActive = false;
        this._pinchStart = null;
      }
    };
    window.addEventListener("pointerup", endTouch);
    window.addEventListener("pointercancel", endTouch);

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { this.clamp(); this._emit(); }, 120);
    });
  }
}
