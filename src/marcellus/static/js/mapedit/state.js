// Settings store + command history for the map editor.
//
// The editor mutates a working copy of the push-settings document; every
// mutation flows through a gesture (drag) or a one-shot edit, both of which
// snapshot the edited top-level keys before/after — so undo/redo is a
// key-level restore, immune to partial-patch bugs. Undo works across save
// (saving doesn't clear history; it just moves the savedDoc baseline).

// Only these top-level keys are ever edited here; dirty-ness and history
// snapshots consider nothing else.
export const EDIT_KEYS = [
  "camera_layout", "camera_optics", "secure_area", "map_scale_ft", "floorplan",
];

const HISTORY_CAP = 100;

const clone = (v) => (v === undefined ? undefined : structuredClone(v));

function pickEdit(doc) {
  const out = {};
  for (const k of EDIT_KEYS) out[k] = clone(doc[k]);
  return out;
}

function sameJson(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export class Store {
  constructor() {
    this.doc = null;        // working document (mutated by edits)
    this.savedDoc = null;   // last state acked by the server
    this.rev = null;
    this.availableCameras = [];
    this.derivedHeadings = {};
    this._history = [];     // [{label, before, after}] — snapshots of EDIT_KEYS
    this._future = [];
    this._gesture = null;   // {label, before} while a drag is in flight
    this._subs = [];
  }

  async load() {
    const r = await window.SC.fetchJson("/v1/push/settings");
    this.doc = r.settings;
    this.savedDoc = clone(r.settings);
    this.rev = r.rev;
    this.availableCameras = r.available_cameras || [];
    this.derivedHeadings = r.derived_headings || {};
    this._emit(new Set(EDIT_KEYS));
  }

  subscribe(fn) { this._subs.push(fn); }
  _emit(keys) { this._subs.forEach((fn) => fn(keys || new Set(EDIT_KEYS))); }

  dirty() {
    if (!this.doc) return false;
    return EDIT_KEYS.some((k) => !sameJson(this.doc[k], this.savedDoc[k]));
  }

  // ---- Edits ----------------------------------------------------------
  // beginGesture/endGesture bracket a drag: any number of mutate() calls in
  // between collapse into ONE history entry labeled e.g. "move doorbell".

  beginGesture(label) {
    if (this._gesture) this.endGesture();
    this._gesture = { label, before: pickEdit(this.doc) };
  }

  // Mutate the doc inside a gesture (or standalone for transient previews —
  // then nothing lands in history until an edit() or gesture commits).
  mutate(fn, keys) {
    fn(this.doc);
    this._emit(keys ? new Set(keys) : new Set(EDIT_KEYS));
  }

  endGesture() {
    const g = this._gesture;
    this._gesture = null;
    if (!g) return;
    const after = pickEdit(this.doc);
    if (sameJson(g.before, after)) return; // no-op drag: no history entry
    this._push({ label: g.label, before: g.before, after });
  }

  cancelGesture() {
    const g = this._gesture;
    this._gesture = null;
    if (!g) return;
    for (const k of EDIT_KEYS) this.doc[k] = clone(g.before[k]);
    this._emit(new Set(EDIT_KEYS));
  }

  // One-shot undoable edit (numeric input commit, button action).
  edit(label, fn, keys) {
    const before = pickEdit(this.doc);
    fn(this.doc);
    const after = pickEdit(this.doc);
    if (!sameJson(before, after)) this._push({ label, before, after });
    this._emit(keys ? new Set(keys) : new Set(EDIT_KEYS));
  }

  _push(entry) {
    this._history.push(entry);
    if (this._history.length > HISTORY_CAP) this._history.shift();
    this._future = [];
  }

  // ---- History --------------------------------------------------------

  canUndo() { return this._history.length > 0; }
  canRedo() { return this._future.length > 0; }
  undoLabel() { return this.canUndo() ? this._history[this._history.length - 1].label : null; }
  redoLabel() { return this.canRedo() ? this._future[this._future.length - 1].label : null; }

  undo() {
    if (this._gesture) { this.cancelGesture(); return; }
    const e = this._history.pop();
    if (!e) return;
    for (const k of EDIT_KEYS) this.doc[k] = clone(e.before[k]);
    this._future.push(e);
    this._emit(new Set(EDIT_KEYS));
  }

  redo() {
    const e = this._future.pop();
    if (!e) return;
    for (const k of EDIT_KEYS) this.doc[k] = clone(e.after[k]);
    this._history.push(e);
    this._emit(new Set(EDIT_KEYS));
  }

  // ---- Save -----------------------------------------------------------
  // PUT the saved baseline merged with the edited keys (never round-trips
  // derived fields we didn't touch) + rev. Returns:
  //   {ok:true}                 saved
  //   {conflict:true}           someone else saved first (rev mismatch)
  //   {error:"..."}             validation/network failure
  // On conflict the caller decides: reload() or save({force:true}) which
  // re-reads the fresh rev and overwrites.

  _body() {
    const body = clone(this.savedDoc);
    for (const k of EDIT_KEYS) body[k] = clone(this.doc[k]);
    // Explicit null = clear for the nullable keys; undefined would read as
    // "sticky, keep server value" and make Clear buttons silently no-op.
    for (const k of ["secure_area", "map_scale_ft", "floorplan"]) {
      if (body[k] === undefined) body[k] = null;
    }
    return body;
  }

  async save({ force = false } = {}) {
    if (force) {
      try {
        const fresh = await window.SC.fetchJson("/v1/push/settings");
        this.rev = fresh.rev;
      } catch (e) {
        return { error: "reload failed: " + e.message };
      }
    }
    const body = this._body();
    body.rev = this.rev;
    let resp;
    try {
      resp = await fetch("/v1/push/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      return { error: e.message };
    }
    if (resp.status === 409) return { conflict: true };
    if (!resp.ok) {
      let msg = "HTTP " + resp.status;
      try {
        const j = await resp.json();
        msg = JSON.stringify(j.detail?.detail ?? j.detail ?? j);
      } catch (e) { /* keep status text */ }
      return { error: msg };
    }
    const j = await resp.json();
    this.rev = j.rev;
    delete body.rev;
    this.savedDoc = body;
    this._emit(new Set());
    return { ok: true };
  }

  // Throw away local edits and re-pull the server document (keeps history so
  // even a reload is undoable).
  async reload() {
    const before = pickEdit(this.doc);
    const r = await window.SC.fetchJson("/v1/push/settings");
    this.doc = r.settings;
    this.savedDoc = clone(r.settings);
    this.rev = r.rev;
    this.availableCameras = r.available_cameras || [];
    const after = pickEdit(this.doc);
    if (!sameJson(before, after)) this._push({ label: "reload", before, after });
    this._emit(new Set(EDIT_KEYS));
  }
}
