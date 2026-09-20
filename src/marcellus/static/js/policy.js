// Routing + notifications policy editor: the outcomes matrix, recognition,
// zone overrides, quiet hours, sound and Live Activities settings. Shares
// the one #save-btn / dirty-state / doc lifecycle that zones.js owns —
// this file only mutates SC.policy.doc in place and calls markDirty().
(function () {
  var SUBJECTS_V3 = ["person", "vehicle", "animal", "thing", "package", "bin", "opening"];
  var SUBJECT_LABELS = {
    person: "Person", vehicle: "Vehicle", animal: "Animal", thing: "Thing",
    package: "Package", bin: "Bin", opening: "Opening",
  };
  var PLACES = ["street", "yard", "doors", "private", "off_limits"];
  var PLACE_LABELS = {
    street: "Street", yard: "Yard", doors: "Doors", private: "Private", off_limits: "Off-limits",
  };
  var OUTCOMES = ["off", "log", "glance", "notify", "alarm"];
  var OUTCOME_LABELS = { off: "Off", log: "Log", glance: "Glance", notify: "Notify", alarm: "Alarm" };

  var RECOGNITION_MODES = ["off", "relax_one", "relax_to_quiet"];
  var RECOGNITION_LABELS = { off: "Off", relax_one: "Relax one step", relax_to_quiet: "Relax to Glance" };

  // Zone-override levels use the older log/quiet/notify/urgent vocabulary
  // (policy_settings.LEVELS) but display the same outcome labels/colors.
  var ZONE_LEVELS = ["log", "quiet", "notify", "urgent"];
  var ZONE_LEVEL_LABELS = { log: "Log", quiet: "Glance", notify: "Notify", alarm: "Alarm", urgent: "Alarm" };
  // Map a value in either vocabulary to the data-level token the CSS keys on.
  var TO_DATA_LEVEL = {
    off: "off", log: "log", glance: "glance", notify: "notify", alarm: "alarm",
    quiet: "glance", urgent: "alarm",
  };

  var el = SC.el;

  function setLevel(node, value) {
    if (value && TO_DATA_LEVEL[value]) node.setAttribute("data-level", TO_DATA_LEVEL[value]);
    else node.removeAttribute("data-level");
  }

  // ---- Routing: outcomes matrix ----

  function renderOutcomes(container, doc, markDirty) {
    container.textContent = "";
    var wrap = el("div", { class: "table-scroll" });
    var table = el("table", { class: "motion-table matrix-table" });
    var thead = el("thead", {});
    var headRow = el("tr", {}, [el("th", { text: "" })]);
    PLACES.forEach(function (p) { headRow.appendChild(el("th", { text: PLACE_LABELS[p] })); });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody", {});
    if (!doc.outcomes) doc.outcomes = {};
    SUBJECTS_V3.forEach(function (subject) {
      var row = el("tr", {}, [el("td", { text: SUBJECT_LABELS[subject] })]);
      PLACES.forEach(function (place) {
        var td = el("td", {});
        var sel = el("select", {});
        var current = (doc.outcomes[subject] || {})[place];
        if (!current) {
          var blank = el("option", { value: "", text: "—" });
          blank.selected = true;
          sel.appendChild(blank);
        }
        OUTCOMES.forEach(function (o) {
          var opt = el("option", { value: o, text: OUTCOME_LABELS[o] });
          if (o === current) opt.selected = true;
          sel.appendChild(opt);
        });
        setLevel(sel, current || "");
        sel.addEventListener("change", function () {
          if (!sel.value) return;
          if (!doc.outcomes[subject]) doc.outcomes[subject] = {};
          doc.outcomes[subject][place] = sel.value;
          setLevel(sel, sel.value);
          markDirty();
        });
        td.appendChild(sel);
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  // ---- Routing: recognition ----

  function renderRecognition(container, doc, markDirty) {
    container.textContent = "";
    if (!doc.recognition) doc.recognition = {};
    [["known_person", "Known person"], ["known_vehicle", "Known vehicle"]].forEach(function (pair) {
      var key = pair[0], label = pair[1];
      var row = el("div", { class: "toggle-row" });
      row.appendChild(el("span", { class: "help", text: label + ": " }));
      var sel = el("select", {});
      RECOGNITION_MODES.forEach(function (m) {
        var opt = el("option", { value: m, text: RECOGNITION_LABELS[m] });
        if (m === (doc.recognition[key] || "off")) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function () {
        doc.recognition[key] = sel.value;
        markDirty();
      });
      row.appendChild(sel);
      container.appendChild(row);
    });
  }

  // ---- Routing: zone overrides ----

  function renderZoneOverrides(container, doc, availableZones, markDirty) {
    container.textContent = "";
    if (!doc.zone_overrides) doc.zone_overrides = {};
    var zoneKeys = {};
    (availableZones || []).forEach(function (z) { zoneKeys[z.zone] = true; });
    Object.keys(doc.zone_overrides).forEach(function (z) { zoneKeys[z] = true; });
    var zones = Object.keys(zoneKeys).sort();
    if (!zones.length) {
      container.appendChild(el("div", { class: "help", text: "No zones yet." }));
      return;
    }
    var wrap = el("div", { class: "table-scroll" });
    var table = el("table", { class: "motion-table matrix-table" });
    var thead = el("thead", {});
    var headRow = el("tr", {}, [el("th", { text: "zone" })]);
    SUBJECTS_V3.forEach(function (s) { headRow.appendChild(el("th", { text: SUBJECT_LABELS[s] })); });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody", {});
    zones.forEach(function (zone) {
      var row = el("tr", {}, [el("td", { text: zone })]);
      SUBJECTS_V3.forEach(function (subject) {
        var td = el("td", {});
        var sel = el("select", {});
        sel.appendChild(el("option", { value: "", text: "" }));
        ZONE_LEVELS.forEach(function (lvl) {
          sel.appendChild(el("option", { value: lvl, text: ZONE_LEVEL_LABELS[lvl] }));
        });
        var current = ((doc.zone_overrides[zone] || {})[subject]) || "";
        sel.value = current;
        setLevel(sel, current);
        sel.addEventListener("change", function () {
          var rowObj = doc.zone_overrides[zone] || {};
          if (sel.value) rowObj[subject] = sel.value;
          else delete rowObj[subject];
          if (Object.keys(rowObj).length) doc.zone_overrides[zone] = rowObj;
          else delete doc.zone_overrides[zone];
          setLevel(sel, sel.value);
          markDirty();
        });
        td.appendChild(sel);
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  // ---- Notifications: quiet hours ----

  function renderQuietHours(container, doc, markDirty) {
    container.textContent = "";
    var qh = doc.quiet_hours;
    var enableRow = el("div", { class: "toggle-row" });
    var enableBox = el("input", { type: "checkbox" });
    enableBox.checked = !!qh;
    var label = el("label", { class: "toggle-row" }, [enableBox, el("span", { text: "Quiet hours" })]);
    enableRow.appendChild(label);
    container.appendChild(enableRow);

    var fieldsRow = el("div", { class: "toggle-row", style: "flex-wrap:wrap" });
    var startInput = el("input", { type: "time" });
    startInput.value = (qh && qh.start) || "22:00";
    var endInput = el("input", { type: "time" });
    endInput.value = (qh && qh.end) || "07:00";
    var modeSel = el("select", {});
    [["cap_quiet", "Cap at Glance"], ["mute_sounds", "Mute sounds"]].forEach(function (pair) {
      var opt = el("option", { value: pair[0], text: pair[1] });
      if (pair[0] === ((qh && qh.mode) || "cap_quiet")) opt.selected = true;
      modeSel.appendChild(opt);
    });
    fieldsRow.appendChild(el("span", { class: "help", text: "start " }));
    fieldsRow.appendChild(startInput);
    fieldsRow.appendChild(el("span", { class: "help", text: "end " }));
    fieldsRow.appendChild(endInput);
    fieldsRow.appendChild(modeSel);
    fieldsRow.style.display = qh ? "flex" : "none";
    container.appendChild(fieldsRow);

    function writeBack() {
      if (!enableBox.checked) {
        doc.quiet_hours = null;
      } else {
        doc.quiet_hours = { start: startInput.value, end: endInput.value, mode: modeSel.value };
      }
      markDirty();
    }
    enableBox.addEventListener("change", function () {
      fieldsRow.style.display = enableBox.checked ? "flex" : "none";
      writeBack();
    });
    [startInput, endInput, modeSel].forEach(function (node) {
      node.addEventListener("change", writeBack);
    });
  }

  // ---- Notifications: sound + Live Activities ----

  function renderSoundAndLA(container, doc, availableOpenings, markDirty) {
    container.textContent = "";

    var soundRow = el("div", { class: "toggle-row" });
    soundRow.appendChild(el("span", { class: "help", text: "Escalation sound: " }));
    var soundInput = el("input", { type: "text" });
    soundInput.value = doc.escalation_sound || "urgent";
    soundInput.addEventListener("input", function () {
      if (soundInput.value.trim()) {
        doc.escalation_sound = soundInput.value.trim();
        markDirty();
      }
    });
    soundInput.addEventListener("blur", function () {
      if (!soundInput.value.trim()) {
        soundInput.value = doc.escalation_sound || "urgent";
      }
    });
    soundRow.appendChild(soundInput);
    container.appendChild(soundRow);

    var muteBox = el("input", { type: "checkbox" });
    muteBox.checked = !!doc.mute_sounds;
    muteBox.addEventListener("change", function () {
      doc.mute_sounds = muteBox.checked;
      markDirty();
    });
    container.appendChild(el("label", { class: "toggle-row" }, [muteBox, el("span", { text: "Mute sounds" })]));

    var geoBox = el("input", { type: "checkbox" });
    geoBox.checked = !!doc.geometric_dedup;
    geoBox.addEventListener("change", function () {
      doc.geometric_dedup = geoBox.checked;
      markDirty();
    });
    container.appendChild(el("label", { class: "toggle-row" }, [geoBox, el("span", { text: "Geometric dedup" })]));

    if (!doc.live_activities) doc.live_activities = {};
    var la = doc.live_activities;

    var deliveryRow = el("div", { class: "toggle-row" });
    deliveryRow.appendChild(el("span", { class: "help", text: "Delivery: " }));
    var deliverySel = el("select", {});
    [["la_first", "Live Activity first"], ["notifications", "Notifications only"]].forEach(function (pair) {
      var opt = el("option", { value: pair[0], text: pair[1] });
      if (pair[0] === (la.delivery || "la_first")) opt.selected = true;
      deliverySel.appendChild(opt);
    });
    deliverySel.addEventListener("change", function () {
      la.delivery = deliverySel.value;
      markDirty();
    });
    deliveryRow.appendChild(deliverySel);
    container.appendChild(deliveryRow);

    var laOnlyBox = el("input", { type: "checkbox" });
    laOnlyBox.checked = !!la.la_only;
    laOnlyBox.addEventListener("change", function () {
      la.la_only = laOnlyBox.checked;
      markDirty();
    });
    container.appendChild(el("label", { class: "toggle-row" }, [laOnlyBox, el("span", { text: "Live Activity only" })]));

    var picksWrap = el("div", { style: "margin:0.4em 0" });
    picksWrap.appendChild(el("div", { class: "help", text: "Opening picks:" }));
    var picks = {};
    (la.opening_picks || []).forEach(function (o) { picks[o] = true; });
    (availableOpenings || []).forEach(function (opening) {
      var name = typeof opening === "string" ? opening : opening.zone || opening.name;
      if (!name) return;
      var box = el("input", { type: "checkbox" });
      box.checked = !!picks[name];
      box.addEventListener("change", function () {
        var list = la.opening_picks || [];
        var i = list.indexOf(name);
        if (box.checked && i === -1) list.push(name);
        else if (!box.checked && i !== -1) list.splice(i, 1);
        la.opening_picks = list;
        markDirty();
      });
      picksWrap.appendChild(el("label", { class: "toggle-row" }, [box, el("span", { text: name })]));
    });
    if (!(availableOpenings || []).length) {
      picksWrap.appendChild(el("div", { class: "help", text: "No opening-class zones found." }));
    }
    container.appendChild(picksWrap);
  }

  // ---- Wiring ----

  function renderAll(doc, data) {
    var outcomesEl = document.getElementById("outcomes-matrix");
    var recognitionEl = document.getElementById("recognition-controls");
    var zoneOvEl = document.getElementById("zone-overrides-matrix");
    var quietEl = document.getElementById("quiet-hours-controls");
    var soundLaEl = document.getElementById("sound-la-controls");
    if (!outcomesEl) return; // page without the routing/notifications markup
    var markDirty = SC.policy.markDirty;
    renderOutcomes(outcomesEl, doc, markDirty);
    renderRecognition(recognitionEl, doc, markDirty);
    renderZoneOverrides(zoneOvEl, doc, (data && data.available_zones) || [], markDirty);
    renderQuietHours(quietEl, doc, markDirty);
    renderSoundAndLA(soundLaEl, doc, (data && data.available_openings) || [], markDirty);
  }

  if (window.SC && window.SC.policy) {
    window.SC.policy.onLoaded(renderAll);
  }
})();
