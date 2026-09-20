// Zone handling settings: place classes, per-zone overrides, camera neighbors.
(function () {
  var PLACES = ["street", "yard", "doors", "private", "off_limits"];
  var PLACE_LABELS = {
    // Display names mirror the app's (AttentionSettingsModel.swift) so the
    // two surfaces speak one vocabulary.
    street: "Public",
    yard: "Semi-private",
    doors: "Entry / exit",
    private: "Private",
    off_limits: "Restricted",
  };
  var SUBJECTS = ["person", "vehicle", "animal", "thing"];
  var LEVELS = ["log", "quiet", "notify", "urgent"];
  // Override values are routing *levels*, displayed in the app's outcome
  // vocabulary (Log/Glance/Notify/Alarm — glance ⇄ quiet, alarm ⇄ urgent).
  var LEVEL_LABELS = { log: "Log", quiet: "Glance", notify: "Notify", urgent: "Alarm" };

  var banner = document.getElementById("zones-banner");
  var zonesList = document.getElementById("zones-list");
  var neighborsList = document.getElementById("neighbors-list");
  var saveBtn = document.getElementById("save-btn");
  var saveState = document.getElementById("save-state");

  var doc = null; // the settings document we mutate and PUT back
  var rev = null; // settings revision — stale PUTs 409 instead of clobbering
  var dirty = false;

  var showBanner = SC.banner(banner);
  var fetchJson = SC.fetchJson;
  var el = SC.el;

  function markDirty() {
    dirty = true;
    saveState.textContent = "unsaved changes";
  }


  function renderZone(zone) {
    var card = el("div", { class: "stat-card" });
    var title = el("div", { class: "stat-label" });
    var configured = (doc.zone_names || {})[zone.zone] || "";
    title.appendChild(el("strong", { text: configured || zone.friendly_name || zone.zone }));
    if (configured || zone.friendly_name) {
      title.appendChild(el("span", {
        class: "help", text: "  (" + zone.zone + ")", style: "margin-left:0.4em",
      }));
    }
    card.appendChild(title);
    card.appendChild(el("div", {
      class: "help", text: "cameras: " + zone.cameras.join(", "),
      style: "margin:0.25em 0 0.5em",
    }));

    // Display name for notification copy ("Person in {name}"). Sidecar-side
    // (settings.zone_names); wins over Frigate's friendly_name.
    var nameRow = el("div", { style: "margin:0.25em 0" });
    nameRow.appendChild(el("span", { text: "Name: ", class: "help" }));
    var nameInput = el("input", {
      type: "text", placeholder: zone.friendly_name || "e.g. the back walkway",
      style: "width:180px",
    });
    nameInput.value = configured;
    nameInput.addEventListener("input", function () {
      if (!doc.zone_names) doc.zone_names = {};
      var v = nameInput.value.trim();
      if (v) doc.zone_names[zone.zone] = v;
      else delete doc.zone_names[zone.zone];
      markDirty();
    });
    nameRow.appendChild(nameInput);
    card.appendChild(nameRow);

    // Place class picker.
    var classRow = el("div", { style: "margin:0.25em 0" });
    classRow.appendChild(el("span", { text: "Place: ", class: "help" }));
    var select = el("select", {});
    var current = (doc.zone_classes || {})[zone.zone] || "";
    var guessOpt = el("option", {
      value: "", text: "guess: " + PLACE_LABELS[zone.guessed_class],
    });
    select.appendChild(guessOpt);
    PLACES.forEach(function (p) {
      var opt = el("option", { value: p, text: PLACE_LABELS[p] });
      if (p === current) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      if (!doc.zone_classes) doc.zone_classes = {};
      if (select.value) doc.zone_classes[zone.zone] = select.value;
      else delete doc.zone_classes[zone.zone];
      markDirty();
    });
    classRow.appendChild(select);
    card.appendChild(classRow);

    // Per-subject overrides.
    var ovRow = el("div", { style: "margin:0.25em 0" });
    SUBJECTS.forEach(function (subject) {
      var wrap = el("label", { style: "margin-right:0.6em;white-space:nowrap" });
      wrap.appendChild(el("span", { class: "help", text: subject + " " }));
      var sel = el("select", {});
      sel.appendChild(el("option", { value: "", text: "inherit" }));
      var ov = ((doc.zone_overrides || {})[zone.zone] || {})[subject] || "";
      LEVELS.forEach(function (lvl) {
        var opt = el("option", { value: lvl, text: LEVEL_LABELS[lvl] });
        if (lvl === ov) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function () {
        if (!doc.zone_overrides) doc.zone_overrides = {};
        var row = doc.zone_overrides[zone.zone] || {};
        if (sel.value) row[subject] = sel.value;
        else delete row[subject];
        if (Object.keys(row).length) doc.zone_overrides[zone.zone] = row;
        else delete doc.zone_overrides[zone.zone];
        markDirty();
      });
      wrap.appendChild(sel);
      ovRow.appendChild(wrap);
    });
    card.appendChild(ovRow);
    return card;
  }

  function neighborSet(camera) {
    // Symmetric closure for display; edits write only the explicit map.
    var table = doc.camera_neighbors || {};
    var out = {};
    (table[camera] || []).forEach(function (n) { out[n] = true; });
    Object.keys(table).forEach(function (cam) {
      if ((table[cam] || []).indexOf(camera) !== -1) out[cam] = true;
    });
    delete out[camera];
    return out;
  }

  function toggleNeighbor(a, b, on) {
    if (!doc.camera_neighbors) doc.camera_neighbors = {};
    var table = doc.camera_neighbors;
    if (on) {
      var list = table[a] || [];
      if (list.indexOf(b) === -1) list.push(b);
      table[a] = list;
    } else {
      // Unticking must break the pair in BOTH declared directions, or the
      // symmetric closure resurrects it.
      [[a, b], [b, a]].forEach(function (pair) {
        var list = table[pair[0]] || [];
        var i = list.indexOf(pair[1]);
        if (i !== -1) list.splice(i, 1);
        if (!list.length) delete table[pair[0]];
        else table[pair[0]] = list;
      });
    }
    markDirty();
  }

  // Which per-camera disclosure rows are open — survives re-renders so
  // ticking a box doesn't slam the row shut.
  var nbOpen = {};

  function renderNeighbors(cameras) {
    neighborsList.textContent = "";
    neighborsList.classList.remove("skeleton");
    cameras.forEach(function (camera) {
      var linked = neighborSet(camera);
      var names = Object.keys(linked).sort();
      var det = el("details", { class: "nb-row" });
      if (nbOpen[camera]) det.open = true;
      det.addEventListener("toggle", function () { nbOpen[camera] = det.open; });
      var sum = el("summary", {}, [
        el("strong", { text: camera }),
        el("span", {
          class: "help",
          text: names.length ? " — " + names.join(", ") : " — no neighbors",
        }),
      ]);
      det.appendChild(sum);
      var chips = el("div", { class: "nb-chips" });
      cameras.forEach(function (other) {
        if (other === camera) return;
        var label = el("label", { class: "nb-chip" + (linked[other] ? " on" : "") });
        var box = el("input", { type: "checkbox" });
        box.checked = !!linked[other];
        box.addEventListener("change", function () {
          toggleNeighbor(camera, other, box.checked);
          renderNeighbors(cameras); // re-render so the mirror row updates
        });
        label.appendChild(box);
        label.appendChild(document.createTextNode(other));
        chips.appendChild(label);
      });
      det.appendChild(chips);
      neighborsList.appendChild(det);
    });
  }

  // Re-pull the server document and re-render the zones/neighbors lists from
  // it -- used at init and after a reload-vs-overwrite conflict choice.
  async function loadFromServer() {
    var data = await fetchJson("/v1/push/settings");
    doc = data.settings;
    rev = data.rev;
    zonesList.textContent = "";
    zonesList.classList.remove("skeleton");
    (data.available_zones || []).forEach(function (zone) {
      zonesList.appendChild(renderZone(zone));
    });
    renderNeighbors(data.available_cameras || []);
    return data;
  }

  async function doSave(useRev) {
    // Send the whole settings doc; camera_neighbors is sticky server-side
    // when absent, so it must always be sent explicitly (even empty).
    if (!doc.camera_neighbors) doc.camera_neighbors = {};
    return fetch("/v1/push/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ rev: useRev }, doc)),
    });
  }

  saveBtn.addEventListener("click", async function () {
    saveBtn.disabled = true;
    saveState.textContent = "saving...";
    var resp;
    try {
      resp = await doSave(rev);
    } catch (err) {
      saveState.textContent = "error: " + err.message;
      saveBtn.disabled = false;
      return;
    }
    if (resp.status === 409) {
      saveBtn.disabled = false;
      saveState.textContent = "conflict — resolve to continue";
      SC.conflictDialog({
        onReload: async function () {
          try {
            await loadFromServer();
            dirty = false;
            saveState.textContent = "reloaded";
          } catch (err) {
            showBanner(err.message, true);
          }
        },
        onOverwrite: async function () {
          saveState.textContent = "saving...";
          try {
            var fresh = await fetchJson("/v1/push/settings");
            var resp2 = await doSave(fresh.rev);
            if (!resp2.ok) throw new Error("HTTP " + resp2.status);
            var j = await resp2.json();
            rev = j.rev;
            dirty = false;
            saveState.textContent = "saved ✓";
          } catch (err) {
            saveState.textContent = "error: " + err.message;
          }
        },
      });
      return;
    }
    try {
      if (!resp.ok) {
        var msg = "HTTP " + resp.status;
        try {
          var errJson = await resp.json();
          msg = JSON.stringify(errJson.detail || errJson);
        } catch (e) { /* keep status text */ }
        throw new Error(msg);
      }
      var okJson = await resp.json();
      rev = okJson.rev;
      dirty = false;
      saveState.textContent = "saved ✓";
    } catch (err) {
      saveState.textContent = "error: " + err.message;
    }
    saveBtn.disabled = false;
  });

  // ---- Export / import (instance-to-instance sync via the browser) ----

  var syncState = document.getElementById("sync-state");

  document.getElementById("export-btn").addEventListener("click", async function () {
    try {
      // Fresh GET: export what the engine is actually running, not the
      // page's possibly-unsaved draft.
      var data = await fetchJson("/v1/push/settings");
      var settings = data.settings;
      if (settings.floorplan) {
        // Carry the floorplan image itself, base64-embedded — the settings
        // key alone is just metadata and the target instance wouldn't have
        // the file. Stripped back out on import before the PUT.
        var imgResp = await fetch("/v1/push/floorplan", { credentials: "same-origin" });
        if (imgResp.ok) {
          var dataUrl = await new Promise(function (resolve, reject) {
            imgResp.blob().then(function (b) {
              var reader = new FileReader();
              reader.onload = function () { resolve(reader.result); };
              reader.onerror = reject;
              reader.readAsDataURL(b);
            }, reject);
          });
          settings = Object.assign({}, settings, {
            _floorplan_image: String(dataUrl).split(",")[1],
          });
        }
      }
      var blob = new Blob(
        [JSON.stringify(settings, null, 2)], { type: "application/json" }
      );
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "push-settings-" + location.hostname + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
      syncState.textContent = "exported";
    } catch (err) {
      syncState.textContent = "export error: " + err.message;
    }
  });

  async function applyImport(imported) {
    syncState.textContent = "importing...";
    try {
      var image = imported._floorplan_image;
      delete imported._floorplan_image;
      if (image && imported.floorplan) {
        // Upload the embedded image first; the PUT below then applies the
        // exported floorplan metadata (incl. the calibration line) on top.
        var bin = atob(image);
        var bytes = new Uint8Array(bin.length);
        for (var bi = 0; bi < bin.length; bi++) bytes[bi] = bin.charCodeAt(bi);
        await fetchJson("/v1/push/floorplan", { method: "POST", body: bytes });
      } else if (!image) {
        // No embedded image (older export): leave the target's floorplan
        // alone rather than pointing its settings at a file it can't have.
        delete imported.floorplan;
      }
      await fetchJson("/v1/push/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(imported),
      });
      syncState.textContent = "imported ✓ — reloading";
      location.reload();
    } catch (err) {
      syncState.textContent = "import error: " + err.message;
    }
  }

  document.getElementById("import-file").addEventListener("change", async function (ev) {
    var file = ev.target.files && ev.target.files[0];
    ev.target.value = ""; // allow re-picking the same file
    if (!file) return;
    var imported;
    try {
      imported = JSON.parse(await file.text());
      if (typeof imported !== "object" || !imported || Array.isArray(imported)) {
        throw new Error("not a settings document");
      }
    } catch (err) {
      syncState.textContent = "import error: " + err.message;
      return;
    }
    // This PUTs the whole policy doc over whatever is running -- confirm
    // before clobbering it, same danger-confirm as enrich.js's cluster delete.
    SC.dialog("Replace all settings with the imported file?", [
      el("p", { text: "This overwrites the running configuration immediately." }),
    ], [
      { label: "cancel" },
      { label: "replace", kind: "btn-danger", onclick: function () { return applyImport(imported); } },
    ]);
  });

  // Unsaved edits (zone names/classes/overrides, camera neighbors) are lost
  // on navigation with no other warning -- same guard as mapedit/main.js.
  window.addEventListener("beforeunload", function (ev) {
    if (dirty) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });

  (async function init() {
    try {
      var data = await loadFromServer();
      if (!(data.available_zones || []).length) {
        showBanner("No zones found in the Frigate config.", false);
      }
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
})();
