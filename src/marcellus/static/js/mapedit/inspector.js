// Inspector panel: camera list, contextual detail for the selection,
// map settings when nothing is selected, and the sticky save bar.
// Every numeric commit goes through store.edit() so it is undoable.

import { CARDINALS, cardinalOf, clamp01, mapAspect, unitToFt } from "./geometry.js";

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c) n.appendChild(c);
  return n;
}

export class Inspector {
  constructor(panelEl, store, view, renderer, tools) {
    this.el = panelEl;
    this.store = store;
    this.view = view;
    this.renderer = renderer;
    this.tools = tools;
    this.selection = null;
    this.listEl = el("div", { class: "me-camlist" });
    this.detailEl = el("div", { class: "me-detail" });
    this.saveBar = el("div", { class: "me-savebar" });
    panelEl.appendChild(this.listEl);
    panelEl.appendChild(this.detailEl);
    panelEl.appendChild(this.saveBar);
    store.subscribe(() => { this.renderList(); this.renderDetail(); this.renderSaveBar(); });
  }

  setSelection(sel) {
    this.selection = sel;
    this.renderList();
    this.renderDetail();
  }

  // ---- Field helpers ---------------------------------------------------

  _num({ label, value, min, max, step, unit, disabled, commit }) {
    const input = el("input", { type: "number", class: "me-num" });
    if (min !== undefined) input.min = min;
    if (max !== undefined) input.max = max;
    if (step !== undefined) input.step = step;
    input.value = value === undefined || value === null || Number.isNaN(value)
      ? "" : String(value);
    if (disabled) input.disabled = true;
    input.addEventListener("change", () => {
      const v = input.value === "" ? null : parseFloat(input.value);
      if (v !== null && Number.isNaN(v)) return;
      commit(v);
    });
    input.addEventListener("keydown", (ev) => ev.stopPropagation());
    return el("label", { class: "me-field" },
      el("span", { class: "me-field-label", text: label }),
      input,
      unit ? el("span", { class: "me-field-unit", text: unit }) : null);
  }

  _select({ label, value, options, disabled, commit }) {
    const sel = el("select", { class: "me-num" });
    for (const o of options) {
      const opt = el("option", { value: o.value, text: o.label });
      if (String(o.value) === String(value ?? "")) opt.selected = true;
      sel.appendChild(opt);
    }
    if (disabled) sel.disabled = true;
    sel.addEventListener("change", () => commit(sel.value));
    return el("label", { class: "me-field" },
      el("span", { class: "me-field-label", text: label }), sel);
  }

  // ---- Camera list -----------------------------------------------------

  renderList() {
    const doc = this.store.doc;
    if (!doc) return;
    this.listEl.textContent = "";
    this.listEl.appendChild(el("div", { class: "me-sec-title", text: "Cameras" }));
    for (const cam of this.store.availableCameras) {
      const entry = (doc.camera_layout || {})[cam];
      const placed = entry && entry.x !== undefined;
      const selected = this.selection?.kind === "camera" && this.selection.camera === cam;
      const row = el("div", {
        class: "me-camrow" + (selected ? " sel" : ""),
        onclick: () => {
          this.tools.select({ kind: "camera", camera: cam });
        },
      },
      el("span", {
        class: "me-camdot" + (placed ? " placed" : ""),
        text: entry && entry.locked ? "🔒" : "●",
      }),
      el("span", { class: "me-camname", text: cam }),
      placed
        ? el("span", {
          class: "me-camaz",
          text: entry.azimuth !== undefined
            ? `${Math.round(entry.azimuth)}° ${cardinalOf(entry.azimuth)}` : "unaimed",
        })
        : el("button", {
          class: "btn-primary me-place", text: "Place",
          onclick: (ev) => { ev.stopPropagation(); this.placeCamera(cam); },
        }));
      this.listEl.appendChild(row);
    }
  }

  placeCamera(cam) {
    const v = this.view.view;
    const cx = v.x + v.w / 2, cy = v.y + this.view.viewH() / 2;
    this.store.edit(`place ${cam}`, (doc) => {
      if (!doc.camera_layout) doc.camera_layout = {};
      doc.camera_layout[cam] = {
        x: +clamp01(cx).toFixed(4),
        y: +clamp01(cy).toFixed(4),
      };
    }, ["camera_layout"]);
    this.tools.select({ kind: "camera", camera: cam });
  }

  // ---- Detail ----------------------------------------------------------

  renderDetail() {
    this.detailEl.textContent = "";
    const doc = this.store.doc;
    if (!doc) return;
    if (this.landmark) this._landmarkDetail();
    else if (this.selection?.kind === "camera") this._cameraDetail(this.selection.camera);
    else if (this.selection?.kind === "secure") this._secureDetail();
    else this._mapDetail();
  }

  _ftFields(container, get, set, locked) {
    // Position in feet when the map is scaled, raw units otherwise.
    const doc = this.store.doc;
    const ft = unitToFt(doc);
    const p = get();
    if (ft) {
      container.appendChild(this._num({
        label: "x", unit: "ft", step: 0.1, disabled: locked,
        value: p.x === undefined ? null : +(p.x * ft.x).toFixed(1),
        commit: (v) => v !== null && set({ x: clamp01(v / ft.x) }),
      }));
      container.appendChild(this._num({
        label: "y", unit: "ft", step: 0.1, disabled: locked,
        value: p.y === undefined ? null : +(p.y * ft.y).toFixed(1),
        commit: (v) => v !== null && set({ y: clamp01(v / ft.y) }),
      }));
    } else {
      container.appendChild(this._num({
        label: "x", unit: "·", step: 0.005, min: 0, max: 1, disabled: locked,
        value: p.x === undefined ? null : p.x,
        commit: (v) => v !== null && set({ x: clamp01(v) }),
      }));
      container.appendChild(this._num({
        label: "y", unit: "·", step: 0.005, min: 0, max: 1, disabled: locked,
        value: p.y === undefined ? null : p.y,
        commit: (v) => v !== null && set({ y: clamp01(v) }),
      }));
    }
  }

  _cameraDetail(cam) {
    const doc = this.store.doc;
    const entry = (doc.camera_layout || {})[cam] || {};
    const optics = (doc.camera_optics || {})[cam] || {};
    const locked = !!entry.locked;
    const d = this.detailEl;

    d.appendChild(el("div", { class: "me-sec-title" },
      el("span", { text: cam }),
      el("button", {
        class: "btn-neutral me-lockbtn",
        text: locked ? "Unlock" : "Lock",
        title: "Lock = no drags, nudges or edits until unlocked. Saved with Save.",
        onclick: () => {
          this.store.edit(`${locked ? "unlock" : "lock"} ${cam}`, (dd) => {
            const e = dd.camera_layout?.[cam];
            if (!e) return;
            if (locked) delete e.locked; else e.locked = true;
          }, ["camera_layout"]);
        },
      })));

    const snap = el("img", {
      class: "me-snap", alt: cam,
      src: `/api/${encodeURIComponent(cam)}/latest.jpg?h=180`,
    });
    d.appendChild(snap);

    const grid = el("div", { class: "me-fields" });
    this._ftFields(grid,
      () => entry,
      (patch) => this.store.edit(`edit ${cam} position`, (dd) => {
        const e = dd.camera_layout[cam];
        if (patch.x !== undefined) e.x = +patch.x.toFixed(4);
        if (patch.y !== undefined) e.y = +patch.y.toFixed(4);
      }, ["camera_layout"]),
      locked || entry.x === undefined);
    grid.appendChild(this._num({
      label: "azimuth", unit: "°", min: 0, max: 359.9, step: 1,
      disabled: locked,
      value: entry.azimuth,
      commit: (v) => this.store.edit(`aim ${cam}`, (dd) => {
        const e = dd.camera_layout?.[cam];
        if (!e) return;
        if (v === null) { delete e.azimuth; return; }
        e.azimuth = +(((v % 360) + 360) % 360).toFixed(1);
        if (e.fov === undefined) e.fov = optics.hfov || 90;
      }, ["camera_layout"]),
    }));
    grid.appendChild(this._num({
      label: "field of view", unit: "°", min: 10, max: 360, step: 5,
      disabled: locked,
      value: entry.fov,
      commit: (v) => v !== null && this.store.edit(`fov ${cam}`, (dd) => {
        const e = dd.camera_layout?.[cam];
        if (e) e.fov = +Math.min(360, Math.max(10, v)).toFixed(1);
      }, ["camera_layout"]),
    }));
    d.appendChild(grid);

    d.appendChild(el("div", { class: "me-sec-sub", text: "Optics (rig facts)" }));
    const og = el("div", { class: "me-fields" });
    const opticsEdit = (label, fn) => this.store.edit(label, (dd) => {
      if (!dd.camera_optics) dd.camera_optics = {};
      if (!dd.camera_optics[cam]) dd.camera_optics[cam] = {};
      fn(dd.camera_optics[cam]);
    }, ["camera_optics"]);
    og.appendChild(this._num({
      label: "HFOV", unit: "°", min: 10, max: 360, step: 1, disabled: locked,
      value: optics.hfov,
      commit: (v) => opticsEdit(`edit ${cam} hfov`, (o) => {
        if (v === null) delete o.hfov; else o.hfov = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "mount", unit: "ft", min: 1, max: 500, step: 0.5, disabled: locked,
      value: optics.mount_ft,
      commit: (v) => opticsEdit(`edit ${cam} mount`, (o) => {
        if (v === null) delete o.mount_ft; else o.mount_ft = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "tilt", unit: "° down", min: -90, max: 90, step: 1, disabled: locked,
      value: optics.tilt_deg,
      commit: (v) => opticsEdit(`edit ${cam} tilt`, (o) => {
        if (v === null) delete o.tilt_deg; else o.tilt_deg = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "VFOV", unit: "°", min: 5, max: 180, step: 1, disabled: locked,
      value: optics.vfov,
      commit: (v) => opticsEdit(`edit ${cam} vfov`, (o) => {
        if (v === null) delete o.vfov; else o.vfov = +v.toFixed(1);
      }),
    }));
    const presets = window.LENS_PRESETS || [];
    if (presets.length) {
      og.appendChild(this._select({
        label: "lens", disabled: locked,
        value: optics.lens ?? "",
        options: [{ value: "", label: "—" }].concat(
          presets.map((p) => ({ value: p.id ?? p.name, label: p.name ?? p.id }))),
        commit: (v) => opticsEdit(`edit ${cam} lens`, (o) => {
          if (!v) { delete o.lens; return; }
          o.lens = v;
          const preset = presets.find((p) => String(p.id ?? p.name) === v);
          if (preset && preset.hfov) o.hfov = preset.hfov;
        }),
      }));
    }
    og.appendChild(this._select({
      label: "faces", disabled: locked,
      value: optics.faces ?? "",
      options: [{ value: "", label: "—" }].concat(
        CARDINALS.map((c) => ({ value: c, label: c }))),
      commit: (v) => opticsEdit(`edit ${cam} faces`, (o) => {
        if (!v) delete o.faces; else o.faces = v;
      }),
    }));
    d.appendChild(og);

    const canLandmark = !locked && entry.azimuth !== undefined && doc.map_scale_ft;
    const lmBtn = el("button", {
      class: "btn-primary", text: "Landmark calibrate",
      title: canLandmark
        ? "Measure HFOV, azimuth and tilt by matching ground landmarks between the snapshot and the floorplan"
        : "Needs the camera placed + aimed, the map scale set, and the camera unlocked",
      onclick: () => this.startLandmark(cam),
    });
    if (!canLandmark) lmBtn.disabled = true;
    d.appendChild(el("div", { class: "me-actions" },
      lmBtn,
      el("button", {
        class: "btn-neutral", text: "Remove from map", disabled: locked || undefined,
        onclick: () => this.store.edit(`remove ${cam} from map`, (dd) => {
          if (dd.camera_layout) delete dd.camera_layout[cam];
        }, ["camera_layout"]),
      })));
  }

  // ---- Landmark calibration -------------------------------------------
  // Pair clicks: snapshot (u,v) then map (mx,my); ≥2 pairs → Solve →
  // before/after + residuals → Apply = one undoable command.

  startLandmark(cam) {
    this.landmark = { camera: cam, matches: [], pending: null, report: null };
    this.tools.select({ kind: "camera", camera: cam });
    this.tools.setTool("landmark");
    this._syncLandmarkPins();
    this.renderDetail();
    this._openLmOverlay();
  }

  cancelLandmark() {
    if (!this.landmark) return;
    this.landmark = null;
    this._closeLmOverlay();
    this.renderer.setToolPins([]);
    if (this.tools.tool === "landmark") this.tools.setTool("select");
    this.renderDetail();
  }

  landmarkMapClick(p) {
    const lm = this.landmark;
    if (!lm || !lm.pending) return;
    lm.pending.mx = +p.x.toFixed(4);
    lm.pending.my = +p.y.toFixed(4);
    lm.matches.push(lm.pending);
    lm.pending = null;
    lm.report = null;
    this._syncLandmarkPins();
    this.renderDetail();
    // Pair complete: bring the big snapshot back for the next one.
    this._openLmOverlay();
  }

  // Large-snapshot overlay: marks are placed on a near-fullscreen image
  // (the panel thumbnail is too small to be accurate), then the overlay
  // closes so the map is clickable for the matching point, and reopens
  // when the pair completes.
  _openLmOverlay() {
    this._closeLmOverlay();
    const lm = this.landmark;
    if (!lm) return;
    const n = lm.matches.length;
    const overlay = el("div", { class: "me-overlay me-lmoverlay" });
    overlay.addEventListener("pointerdown", (ev) => {
      if (ev.target === overlay) this._closeLmOverlay();
    });
    const solveBtn = el("button", {
      class: "btn-primary",
      text: n >= 2 ? `Solve (${n} pairs)` : `Solve (needs ${2 - n} more)`,
      onclick: () => { this._closeLmOverlay(); this._landmarkSolve(); },
    });
    if (n < 2) solveBtn.disabled = true;
    overlay.appendChild(el("div", { class: "me-dialog me-lmdialog" },
      el("p", {
        class: "help",
        text: `Pair ${n + 1}: tap a ground landmark (gate post, path corner…). ` +
          "This closes so you can tap the same spot on the map.",
      }),
      this._lmImageWrap(true),
      el("div", { class: "me-actions" },
        solveBtn,
        el("button", {
          class: "btn-neutral", text: "Hide snapshot",
          onclick: () => this._closeLmOverlay(),
        }),
        el("button", {
          class: "btn-neutral", text: "Cancel calibration",
          onclick: () => this.cancelLandmark(),
        }))));
    document.body.appendChild(overlay);
    this._lmOverlayEl = overlay;
  }

  _closeLmOverlay() {
    if (this._lmOverlayEl) { this._lmOverlayEl.remove(); this._lmOverlayEl = null; }
  }

  // Snapshot + numbered dots; tapping places/moves the pending image mark.
  // Used small in the panel and large in the overlay.
  _lmImageWrap(large) {
    const lm = this.landmark;
    const wrap = el("div", { class: "me-lmwrap" + (large ? " me-lmwrap-lg" : "") });
    const img = el("img", {
      class: "me-snap", alt: lm.camera,
      src: `/api/${encodeURIComponent(lm.camera)}/latest.jpg?h=${large ? 1080 : 720}` +
        `&cache=${lm.cacheKey || (lm.cacheKey = Date.now())}`,
    });
    img.style.cursor = "crosshair";
    img.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const rect = img.getBoundingClientRect();
      // Clicking again while pending MOVES the pending point.
      lm.pending = {
        u: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
        v: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
      };
      if (large) this._closeLmOverlay();
      this.renderDetail();
    });
    wrap.appendChild(img);
    const all = lm.matches.concat(lm.pending ? [lm.pending] : []);
    all.forEach((m, i) => {
      const dot = el("span", {
        class: "me-lmdot" + (m.mx === undefined ? " pending" : ""),
        text: String(i + 1),
      });
      dot.style.left = (m.u * 100) + "%";
      dot.style.top = (m.v * 100) + "%";
      wrap.appendChild(dot);
    });
    return wrap;
  }

  _syncLandmarkPins() {
    const lm = this.landmark;
    if (!lm) { this.renderer.setToolPins([]); return; }
    this.renderer.setToolPins(
      lm.matches.map((m, i) => ({ x: m.mx, y: m.my, label: i + 1 })));
  }

  _landmarkDetail() {
    const lm = this.landmark;
    const d = this.detailEl;
    d.appendChild(el("div", { class: "me-sec-title" },
      el("span", { text: `Landmark · ${lm.camera}` }),
      el("button", { class: "btn-neutral", text: "Cancel", onclick: () => this.cancelLandmark() })));

    const n = lm.matches.length;
    d.appendChild(el("p", {
      class: "help",
      text: lm.pending
        ? `Now tap the SAME spot on the map (pair ${n + 1}).`
        : `Tap a ground landmark in the snapshot (gate post, path corner…) — ${n} pair${n === 1 ? "" : "s"} so far, 2–12 needed.`,
    }));

    d.appendChild(this._lmImageWrap(false));

    const actions = el("div", { class: "me-actions" });
    actions.appendChild(el("button", {
      class: "btn-neutral", text: "Enlarge snapshot",
      onclick: () => this._openLmOverlay(),
    }));
    const undoBtn = el("button", {
      class: "btn-neutral", text: lm.pending ? "Drop pending" : "Undo pair",
      onclick: () => {
        if (lm.pending) lm.pending = null;
        else lm.matches.pop();
        lm.report = null;
        this._syncLandmarkPins();
        this.renderDetail();
      },
    });
    if (!lm.pending && !n) undoBtn.disabled = true;
    actions.appendChild(undoBtn);
    const solveBtn = el("button", {
      class: "btn-primary", text: lm.solving ? "Solving…" : "Solve",
      onclick: () => this._landmarkSolve(),
    });
    if (n < 2 || lm.pending || lm.solving) solveBtn.disabled = true;
    actions.appendChild(solveBtn);
    d.appendChild(actions);

    if (lm.error) d.appendChild(el("p", { class: "help", text: "⚠ " + lm.error }));
    if (lm.report) this._landmarkReport(d, lm);
  }

  async _landmarkSolve() {
    const lm = this.landmark;
    lm.solving = true;
    lm.error = null;
    this.renderDetail();
    try {
      lm.report = await window.SC.fetchJson("/v1/push/map/landmark-solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ camera: lm.camera, matches: lm.matches }),
      });
    } catch (e) {
      lm.error = e.message;
    }
    lm.solving = false;
    this.renderDetail();
  }

  _landmarkReport(d, lm) {
    const r = lm.report;
    d.appendChild(el("div", { class: "me-lmreport" },
      el("div", { text: `HFOV ${r.hfov_before}° → ${r.hfov_after}°` }),
      el("div", { text: `azimuth ${r.azimuth_before}° → ${r.azimuth_after}°` }),
      el("div", { text: `tilt ${r.tilt_before}° → ${r.tilt_after}°` }),
      r.wedge_fov_after
        ? el("div", { text: `map wedge ${r.wedge_fov_after}° (ground span — wider than the lens when tilted)` })
        : null,
      el("div", { text: `fit error ${r.rms_ft} ft (per point: ${r.residual_ft.join(", ")})` })));
    const worst = Math.max(...r.residual_ft);
    if (worst > 8) {
      d.appendChild(el("p", {
        class: "help",
        text: `⚠ point ${r.residual_ft.indexOf(worst) + 1} fits poorly (${worst} ft) — mismatched click? Undo it and re-add.`,
      }));
    }
    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-primary", text: "Apply",
        onclick: () => {
          const cam = lm.camera;
          this.store.edit(`landmark-calibrate ${cam}`, (dd) => {
            if (!dd.camera_optics) dd.camera_optics = {};
            if (!dd.camera_optics[cam]) dd.camera_optics[cam] = {};
            const o = dd.camera_optics[cam];
            o.hfov = r.hfov_after;
            o.tilt_deg = r.tilt_after;
            if (r.vfov_after !== null && r.vfov_after !== undefined && o.vfov) {
              o.vfov = r.vfov_after;
            }
            const entry = (dd.camera_layout || {})[cam];
            if (entry) {
              entry.azimuth = r.azimuth_after;
              // The wedge shows ground coverage — wider than the lens
              // HFOV when the camera is tilted, so solved landmarks at
              // the frame edge still fall inside it.
              entry.fov = r.wedge_fov_after || r.hfov_after;
            }
          }, ["camera_optics", "camera_layout"]);
          this.cancelLandmark();
          this.flash(`${lm.camera} calibrated — Save to keep it.`);
        },
      })));
  }

  _secureDetail() {
    const doc = this.store.doc;
    const sa = doc.secure_area;
    const d = this.detailEl;
    d.appendChild(el("div", { class: "me-sec-title", text: "Secure area" }));
    if (!sa) {
      d.appendChild(el("p", { class: "help", text: "No secure area drawn." }));
      return;
    }
    const ft = unitToFt(doc);
    const grid = el("div", { class: "me-fields" });
    for (const k of ["x0", "y0", "x1", "y1"]) {
      const axis = k[0] === "x" ? "x" : "y";
      const scale = ft ? ft[axis] : 1;
      grid.appendChild(this._num({
        label: k, unit: ft ? "ft" : "·", step: ft ? 0.1 : 0.005,
        value: +(sa[k] * scale).toFixed(ft ? 1 : 4),
        commit: (v) => v !== null && this.store.edit("edit secure area", (dd) => {
          if (dd.secure_area) dd.secure_area[k] = +clamp01(v / scale).toFixed(4);
        }, ["secure_area"]),
      }));
    }
    d.appendChild(grid);
    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-neutral", text: "Clear secure area",
        onclick: () => {
          this.store.edit("clear secure area", (dd) => { dd.secure_area = null; },
            ["secure_area"]);
          this.tools.select(null);
        },
      })));
  }

  _mapDetail() {
    const doc = this.store.doc;
    const d = this.detailEl;
    d.appendChild(el("div", { class: "me-sec-title", text: "Map" }));

    const grid = el("div", { class: "me-fields" });
    grid.appendChild(this._num({
      label: "map width", unit: "ft", min: 10, max: 100000, step: 5,
      value: doc.map_scale_ft,
      commit: (v) => this.store.edit("set map width", (dd) => {
        dd.map_scale_ft = v && v > 0 ? v : null;
      }, ["map_scale_ft"]),
    }));
    if (doc.floorplan) {
      grid.appendChild(this._num({
        label: "rotate plan", unit: "°", min: -360, max: 360, step: 0.5,
        value: doc.floorplan.rotation_deg,
        commit: (v) => this.store.edit("rotate floorplan", (dd) => {
          const r = ((parseFloat(v) || 0) % 360 + 360) % 360;
          if (r) dd.floorplan.rotation_deg = +r.toFixed(1);
          else delete dd.floorplan.rotation_deg;
        }, ["floorplan"]),
      }));
    }
    grid.appendChild(this._select({
      label: "grid",
      value: String(this.renderer.gridFt() || ""),
      options: [
        { value: "", label: "off" },
        { value: "1", label: "1 ft" }, { value: "2", label: "2 ft" },
        { value: "5", label: "5 ft" }, { value: "10", label: "10 ft" },
      ],
      commit: (v) => this.renderer.setGridFt(parseFloat(v) || 0),
    }));
    d.appendChild(grid);

    const dim = el("input", {
      type: "range", min: "0", max: "0.8", step: "0.05",
      value: String(this.renderer.dimFloorplan),
    });
    dim.addEventListener("input", () => this.renderer.setDim(parseFloat(dim.value)));
    d.appendChild(el("label", { class: "me-field me-dim" },
      el("span", { class: "me-field-label", text: "dim plan" }), dim));

    // Floorplan upload / remove.
    const file = el("input", {
      type: "file", accept: "image/png,image/jpeg,image/webp",
      style: "display:none",
    });
    file.addEventListener("change", () => this._uploadFloorplan(file));
    d.appendChild(file);
    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-neutral",
        text: doc.floorplan ? "Replace floorplan…" : "Upload floorplan…",
        onclick: () => file.click(),
      }),
      doc.floorplan ? el("button", {
        class: "btn-neutral", text: "Remove floorplan",
        onclick: () => this.store.edit("remove floorplan", (dd) => {
          dd.floorplan = null;
        }, ["floorplan"]),
      }) : null,
      el("button", {
        class: "btn-neutral", text: "Auto-tune aim",
        title: "Replay the capture history: where two cameras saw the same object at the same instant, tune azimuth/tilt so their projections agree",
        onclick: (ev) => this._autotune(ev.target),
      }),
      el("button", {
        class: "btn-neutral", text: "Reload Frigate config",
        title: "Re-sync camera and zone names from Frigate",
        onclick: async (ev) => {
          const btn = ev.target;
          btn.disabled = true;
          try {
            await window.SC.fetchJson("/v1/push/frigate-config/refresh", { method: "POST" });
            await this.store.reload();
          } catch (e) {
            this.flash("config reload failed: " + e.message);
          }
          btn.disabled = false;
        },
      })));
    d.appendChild(el("p", {
      class: "help me-hint",
      text: "Select a camera to edit it. Drag its dot to move, the knob to aim, " +
        "a wedge edge to widen. Shift bypasses grid snap, Alt bypasses angle snap.",
    }));
  }

  async _autotune(btn) {
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Tuning…";
    let body;
    try {
      body = await window.SC.fetchJson("/v1/push/map/autotune?minutes=240", { method: "POST" });
    } catch (e) {
      btn.disabled = false;
      btn.textContent = prev;
      this.flash("auto-tune: " + e.message);
      return;
    }
    btn.disabled = false;
    btn.textContent = prev;
    const r = body.report;
    const changed = Object.keys(r.cameras || {}).sort().filter((cam) => {
      const c = r.cameras[cam];
      const dAz = Math.abs(((c.azimuth_after - c.azimuth_before + 540) % 360) - 180);
      return dAz >= 0.2 || Math.abs(c.tilt_after - c.tilt_before) >= 0.2;
    });
    const overlay = el("div", { class: "me-overlay" });
    const lines = el("div", { class: "me-lmreport" },
      el("div", { text: `RMS ${r.rms_before_ft} → ${r.rms_after_ft} ft (${body.elapsed_s}s)` }),
      ...changed.map((cam) => {
        const c = r.cameras[cam];
        return el("div", {
          text: `${cam}: azimuth ${c.azimuth_before}° → ${c.azimuth_after}°, ` +
            `tilt ${c.tilt_before}° → ${c.tilt_after}° (${c.pairs} pairs)`,
        });
      }),
      ...(r.warnings || []).map((w) => el("div", { text: "⚠ " + w })),
      changed.length ? null
        : el("div", { text: "No meaningful corrections — aim already agrees with the data." }));
    const actions = el("div", { class: "me-actions" },
      changed.length ? el("button", {
        class: "btn-primary", text: "Apply",
        onclick: () => {
          overlay.remove();
          this.store.edit("auto-tune aim", (dd) => {
            for (const cam of changed) {
              const entry = (dd.camera_layout || {})[cam];
              if (entry?.locked) continue; // bulk tools respect the lock
              const c = r.cameras[cam];
              if (entry) entry.azimuth = c.azimuth_after;
              if ((dd.camera_optics || {})[cam]) {
                dd.camera_optics[cam].tilt_deg = c.tilt_after;
              }
            }
          }, ["camera_layout", "camera_optics"]);
          this.flash("Auto-tune applied — Save to keep it.");
        },
      }) : null,
      el("button", {
        class: "btn-neutral", text: changed.length ? "Cancel" : "Close",
        onclick: () => overlay.remove(),
      }));
    overlay.appendChild(el("div", { class: "me-dialog" }, lines, actions));
    document.body.appendChild(overlay);
  }

  async _uploadFloorplan(fileInput) {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    fileInput.value = "";
    let resp;
    try {
      resp = await fetch("/v1/push/floorplan", {
        method: "POST",
        headers: { "Content-Type": f.type || "application/octet-stream" },
        body: f,
      });
    } catch (e) {
      this.flash("upload failed: " + e.message);
      return;
    }
    if (!resp.ok) {
      this.flash("upload failed: HTTP " + resp.status);
      return;
    }
    // The upload endpoint writes floorplan metadata server-side; re-pull so
    // doc/savedDoc agree with it.
    await this.store.reload();
  }

  flash(msg) {
    this._flashMsg = msg;
    this.renderSaveBar();
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._flashMsg = null;
      this.renderSaveBar();
    }, 6000);
  }

  // ---- Save bar --------------------------------------------------------

  renderSaveBar() {
    const s = this.store;
    const bar = this.saveBar;
    bar.textContent = "";
    const dirty = s.dirty();
    bar.classList.toggle("dirty", dirty);

    const undoBtn = el("button", {
      class: "btn-neutral me-undo",
      text: s.canUndo() ? `Undo ${s.undoLabel()}` : "Undo",
      onclick: () => s.undo(),
    });
    undoBtn.disabled = !s.canUndo();
    const redoBtn = el("button", {
      class: "btn-neutral me-undo",
      text: s.canRedo() ? `Redo ${s.redoLabel()}` : "Redo",
      onclick: () => s.redo(),
    });
    redoBtn.disabled = !s.canRedo();
    bar.appendChild(el("div", { class: "me-undoredo" }, undoBtn, redoBtn));

    if (this._flashMsg) {
      bar.appendChild(el("div", { class: "me-savemsg", text: this._flashMsg }));
    }

    if (dirty) {
      const saveBtn = el("button", {
        class: "btn-primary", text: "Save",
        onclick: () => this.doSave(saveBtn),
      });
      bar.appendChild(el("div", { class: "me-saverow" },
        el("span", { class: "me-dirtymark", text: "Unsaved changes" }),
        saveBtn,
        el("button", {
          class: "btn-neutral", text: "Discard",
          onclick: async () => { await this.store.reload(); },
        })));
    }
  }

  async doSave(btn) {
    btn.disabled = true;
    const r = await this.store.save();
    btn.disabled = false;
    if (r.ok) { this.flash("Saved."); return; }
    if (r.conflict) { this._conflictDialog(); return; }
    this.flash("Save failed: " + r.error);
  }

  // Scale tool finished a reference line: ask its real-world length and
  // derive the map width. A diagonal line is fine — the aspect-corrected
  // length is what's equated to the entered feet.
  scaleDialog(line) {
    const overlay = el("div", { class: "me-overlay" });
    const input = el("input", { type: "number", min: "0.5", step: "0.5", class: "me-num" });
    const apply = () => {
      const ft = parseFloat(input.value);
      if (!ft || ft <= 0) return;
      overlay.remove();
      const a = mapAspect(this.store.doc);
      const dx = line.x1 - line.x0, dy = (line.y1 - line.y0) * a;
      const unitLen = Math.hypot(dx, dy);
      if (unitLen < 1e-6) return;
      this.store.edit("calibrate scale", (dd) => {
        dd.map_scale_ft = +(ft / unitLen).toFixed(1);
        if (dd.floorplan) {
          dd.floorplan.calibration = {
            x0: +line.x0.toFixed(4), y0: +line.y0.toFixed(4),
            x1: +line.x1.toFixed(4), y1: +line.y1.toFixed(4),
            length_ft: ft,
          };
        }
      }, ["map_scale_ft", "floorplan"]);
      this.flash(`Map width set to ${(this.store.doc.map_scale_ft || 0).toFixed(0)} ft.`);
    };
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") apply();
      if (ev.key === "Escape") overlay.remove();
    });
    const box = el("div", { class: "me-dialog" },
      el("p", { text: "How long is the line you just drew, in feet?" }),
      el("div", { class: "me-actions" },
        input,
        el("button", { class: "btn-primary", text: "Set scale", onclick: apply }),
        el("button", {
          class: "btn-neutral", text: "Cancel",
          onclick: () => overlay.remove(),
        })));
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    input.focus();
  }

  _conflictDialog() {
    window.SC.conflictDialog({
      onReload: async () => { await this.store.reload(); },
      onOverwrite: async () => {
        const r = await this.store.save({ force: true });
        this.flash(r.ok ? "Saved." : "Save failed: " + (r.error || "conflict"));
      },
    });
  }
}
