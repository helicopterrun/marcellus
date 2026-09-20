/* Placement planner — pure-physics camera siting calculator.
 * Mirrors marcellus/analysis/optics.py (kept in sync by tests/test_optics.py).
 * Horizontal: focal/zoom -> HFOV -> object width px (top-down wedge + px/distance chart).
 * Vertical:   mount height + down-angle -> VFOV -> bbox height px (side elevation).
 * References: 320x320 model input, face min_area 500 px2, DORI px/m bands. */
(function () {
  "use strict";

  // Colors used by the hand-built SVG charts below. Resolved lazily via
  // ElsinoreTokens.cssVar() (tokens.js / the stylesheet may not have run yet
  // at import time) with the current triage.css value as a fallback.
  // Zone-fill / chrome colors below have no matching token and stay literal.
  function cssVar(name, fallback) {
    var t = window.ElsinoreTokens;
    return (t && t.cssVar(name)) || fallback;
  }
  var PALETTE = {
    ok: function () { return cssVar("--ok", "#4ade80"); },
    danger: function () { return cssVar("--danger", "#f87171"); },
    warn: function () { return cssVar("--warn", "#fbbf24"); },
    info: function () { return cssVar("--info", "#3b82f6"); },
    laneVehicle: function () { return cssVar("--lane-vehicle", "#a78bfa"); },
    laneVehicleLight: function () { return cssVar("--lane-vehicle", "#c4b5fd"); },
    deep: function () { return cssVar("--deep", "#0f1115"); },
    surface: function () { return cssVar("--surface", "#1a1d26"); },
    // zone-fill / chrome — no token equivalent, kept as plain literals
    idMarker: "#5b21b6",
    gridLine: "#2a2f3a",
    axisLabel: "#6b7280",
    targetLine: "#14532d",
    halfTargetLine: "#78350f",
    white: "#fff",
    ground: "#4b5563",
    blindZone: "#7f1d1d",
    distLine: "#e6e6e6",
    chromeLabel: "#8a92a6",
  };

  var P = JSON.parse(document.getElementById("plan-presets").textContent);
  var byId = {};
  P.lenses.forEach(function (l) { byId["lens:" + l.id] = l; });
  P.resolutions.forEach(function (r) { byId["res:" + r.id] = r; });
  P.objects.forEach(function (o) { byId["obj:" + o.id] = o; });
  var FT_PER_M = P.refs.ft_per_m;
  var FACE_MIN_AREA = P.refs.face_min_area_px2;       // floor to attempt recognition
  var FACE_FLOOR = P.refs.face_recog_floor_px2;       // empirical floor to actually recognize

  var $ = function (id) { return document.getElementById(id); };

  // ---- state ----
  var state = {
    lensId: "dahua-5442-vf",
    focal: 2.7,
    hfovOverride: null,
    vfovOverride: null, // published VFOV from a deployed quick-pick
    customHfov: 90,
    resId: "sub-720",
    objId: "face",
    dist: 20,
    heightFt: 10,
    tiltDeg: 12,
    faceHeightFt: 5.4,
    obj: { width_ft: 0.5, aspect: 1.4, target_px: 80 },
  };

  // ---- geometry (mirror of optics.py) ----
  var rad = function (d) { return d * Math.PI / 180; };
  var deg = function (r) { return r * 180 / Math.PI; };
  function hfovFromFocal(f, w, f0) { return deg(2 * Math.atan(w / (2 * (f + (f0 || 0))))); }
  function focalFromHfov(h, w, f0) { return w / (2 * Math.tan(rad(h) / 2)) - (f0 || 0); }
  function vfovFromHfov(h, dw, dh) { return deg(2 * Math.atan(Math.tan(rad(h) / 2) * dh / dw)); }
  function pxPerFt(dw, h, d) { return dw / (2 * d * Math.tan(rad(h) / 2)); }
  function objWidthPx(wf, dw, h, d) { return wf * pxPerFt(dw, h, d); }
  function maxDistFt(wf, dw, h, t) { return wf * dw / (2 * t * Math.tan(rad(h) / 2)); }
  function bboxHeightPx(bottom, top, ch, d, vf, dh) {
    return (Math.atan((ch - bottom) / d) - Math.atan((ch - top) / d)) / rad(vf) * dh;
  }
  function faceDep(ch, d, eye) { return deg(Math.atan((ch - eye) / d)); }
  function doriDistFt(dw, h, ppm) { return dw * FT_PER_M / (2 * ppm * Math.tan(rad(h) / 2)); }
  function groundCoverage(ch, tilt, vf) {
    function hit(dep) { return dep <= 0 ? null : dep >= 90 ? 0 : ch / Math.tan(rad(dep)); }
    var near = hit(tilt + vf / 2);
    return { near: near == null ? 0 : near, far: hit(tilt - vf / 2) };
  }

  function lens() { return byId["lens:" + state.lensId]; }
  function res() { return byId["res:" + state.resId]; }

  function effectiveHfov() {
    var l = lens();
    if (l.type === "custom") return state.customHfov;
    if (state.hfovOverride != null) return state.hfovOverride;
    if (l.type === "varifocal") return hfovFromFocal(state.focal, l.sensor_width_mm, l.focal_offset_mm);
    return l.hfov;
  }
  function effectiveVfov() {
    if (state.vfovOverride != null) return state.vfovOverride;
    return vfovFromHfov(effectiveHfov(), res().w, res().h);
  }

  // ---- populate selects ----
  function fillSelect(el, items, value, render) {
    el.innerHTML = "";
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = it.id; o.textContent = render(it); el.appendChild(o);
    });
    if (value != null) el.value = value;
  }
  fillSelect($("sel-lens"), P.lenses, state.lensId, function (l) { return l.label; });
  fillSelect($("sel-res"), P.resolutions, state.resId, function (r) { return r.label; });
  fillSelect($("sel-object"), P.objects, state.objId, function (o) { return o.label; });
  P.cameras.forEach(function (c) {
    var o = document.createElement("option");
    o.value = c.id; o.textContent = c.id + " (" + c.hfov + "°, " + c.mount_ft + "ft)";
    $("sel-camera").appendChild(o);
  });

  function syncLensControls() {
    var l = lens();
    $("wrap-focal").style.display = l.type === "varifocal" ? "" : "none";
    $("wrap-hfov-custom").style.display = l.type === "custom" ? "" : "none";
    if (l.type === "varifocal") { $("in-focal").min = l.focal_min; $("in-focal").max = l.focal_max; }
  }
  function loadObject() {
    var o = byId["obj:" + state.objId];
    state.obj = { width_ft: o.width_ft, aspect: o.aspect, target_px: o.target_px };
    $("in-owidth").value = o.width_ft; $("in-oaspect").value = o.aspect; $("in-otarget").value = o.target_px;
  }
  function setCls(el, cls) { el.className = "cell-class " + cls; }

  function setView(v) {
    ["top", "side", "chart"].forEach(function (name) {
      var el = document.querySelector(".view-" + name);
      if (el) el.style.display = (v === "all" || v === name) ? "" : "none";
    });
  }

  // ---- main recompute ----
  function recompute() {
    var hfov = effectiveHfov(), vfov = effectiveVfov();
    var detW = res().w, detH = res().h, o = state.obj;
    var objH = o.width_ft * o.aspect;
    var faceH = state.faceHeightFt;
    // subject vertical extent off the ground: a face sits at eye level,
    // everything else stands on the ground.
    var subjBot, subjTop;
    if (state.objId === "face") { subjBot = Math.max(faceH - objH / 2, 0); subjTop = faceH + objH / 2; }
    else { subjBot = 0; subjTop = objH; }
    var good = maxDistFt(o.width_ft, detW, hfov, o.target_px);
    var marg = maxDistFt(o.width_ft, detW, hfov, o.target_px / 2);

    $("out-hfov").textContent = hfov.toFixed(1);
    $("out-vfov").textContent = vfov.toFixed(1);
    $("out-maxdist").textContent = good.toFixed(1);
    $("out-maxdist-marg").textContent = marg.toFixed(1);
    $("out-pxft").textContent = pxPerFt(detW, hfov, state.dist).toFixed(1);
    if (lens().type === "varifocal") $("focal-val").textContent = (+state.focal).toFixed(1);

    // distance panel — width from HFOV, height geometric from VFOV + mount geometry
    var wpx = objWidthPx(o.width_ft, detW, hfov, state.dist);
    var hpx = bboxHeightPx(subjBot, subjTop, state.heightFt, state.dist, vfov, detH);
    var area = wpx * Math.max(hpx, 0);
    $("out-w").textContent = wpx.toFixed(0);
    $("out-h").textContent = hpx.toFixed(0);
    var areaTier = area >= FACE_FLOOR ? "ok" : area >= FACE_MIN_AREA ? "warn" : "noise";
    var areaTag = area >= FACE_FLOOR ? " recog" : area >= FACE_MIN_AREA ? " attempt" : " < min";
    $("out-area").textContent = Math.round(area).toLocaleString() + " px²" + areaTag;
    setCls($("out-area"), areaTier);
    var v = $("out-verdict");
    if (wpx >= o.target_px) { v.textContent = "good"; setCls(v, "ok"); }
    else if (wpx >= o.target_px / 2) { v.textContent = "marginal"; setCls(v, "warn"); }
    else { v.textContent = "too small"; setCls(v, "noise"); }

    // elevation outputs
    var cov = groundCoverage(state.heightFt, state.tiltDeg, vfov);
    $("out-ground").textContent = cov.near.toFixed(1) + " – " +
      (cov.far == null ? "horizon" : cov.far.toFixed(0) + " ft");
    var eye = faceH;
    var fd = faceDep(state.heightFt, state.dist, eye);
    var fa = $("out-faceang");
    fa.textContent = (fd >= 0 ? "↓" : "↑") + Math.abs(fd).toFixed(0) + "° " +
      (fd <= 12 ? "frontal" : fd <= 32 ? "oblique" : "top of head");
    setCls(fa, fd <= 12 ? "ok" : fd <= 32 ? "warn" : "noise");
    // in-frame: subject's bottom within the cone's bottom ray, top within the top ray
    var botDep = deg(Math.atan((state.heightFt - subjBot) / state.dist));
    var topDep = deg(Math.atan((state.heightFt - subjTop) / state.dist));
    var coneBot = state.tiltDeg + vfov / 2, coneTop = state.tiltDeg - vfov / 2;
    var feetIn = botDep <= coneBot, headIn = topDep >= coneTop;
    var inf = $("out-inframe");
    if (feetIn && headIn) { inf.textContent = "fully"; setCls(inf, "ok"); }
    else if (!feetIn && !headIn) { inf.textContent = "out of frame"; setCls(inf, "noise"); }
    else { inf.textContent = feetIn ? "head cut" : "feet cut"; setCls(inf, "warn"); }

    drawChart(hfov, detW, o, good, marg);
    drawWedge(hfov, good, marg);
    drawElevation(vfov, subjBot, subjTop, eye, cov, feetIn && headIn);
  }

  // ---- px-vs-distance chart (with DORI markers) ----
  function drawChart(hfov, detW, o, good, marg) {
    var W = 520, H = 300, ml = 46, mr = 14, mt = 12, mb = 34;
    var x0 = ml, x1 = W - mr, y0 = H - mb, y1 = mt;
    var xmax = Math.max(marg * 1.25, state.dist * 1.1, 15);
    var ymax = Math.max(o.target_px * 2.5, objWidthPx(o.width_ft, detW, hfov, Math.max(state.dist, 1)) * 1.1, 10);
    var sx = function (d) { return x0 + (d / xmax) * (x1 - x0); };
    var sy = function (p) { return y0 - (Math.min(p, ymax) / ymax) * (y0 - y1); };
    var parts = ['<rect x="0" y="0" width="' + W + '" height="' + H + '" fill=PALETTE.deep()/>'];
    parts.push(line(x0, y0, x1, y0, PALETTE.gridLine), line(x0, y0, x0, y1, PALETTE.gridLine));
    for (var gx = 0; gx <= xmax; gx += niceStep(xmax)) {
      parts.push(line(sx(gx), y0, sx(gx), y1, PALETTE.surface()), txt(sx(gx), y0 + 14, gx.toFixed(0), PALETTE.axisLabel, "middle"));
    }
    // DORI identify/recognise crossovers
    [["identification", "ID"], ["recognition", "Rec"]].forEach(function (pair) {
      var d = doriDistFt(detW, hfov, P.dori[pair[0]]);
      if (d > 0 && d <= xmax) {
        parts.push(line(sx(d), y0, sx(d), y1, PALETTE.idMarker, "2 3"));
        parts.push(txt(sx(d), y1 + 9, pair[1], PALETTE.laneVehicle(), "middle"));
      }
    });
    parts.push(line(x0, sy(o.target_px), x1, sy(o.target_px), PALETTE.targetLine, "4 3"));
    parts.push(txt(x1, sy(o.target_px) - 4, "target " + o.target_px + "px", PALETTE.ok(), "end"));
    parts.push(line(x0, sy(o.target_px / 2), x1, sy(o.target_px / 2), PALETTE.halfTargetLine, "4 3"));
    parts.push(txt(x1, sy(o.target_px / 2) - 4, "½ target", PALETTE.warn(), "end"));
    var pts = [];
    for (var i = 0; i <= 120; i++) {
      var d = xmax * i / 120;
      if (d < 0.5) continue;
      pts.push(sx(d).toFixed(1) + "," + sy(objWidthPx(o.width_ft, detW, hfov, d)).toFixed(1));
    }
    parts.push('<polyline points="' + pts.join(" ") + '" fill="none" stroke=PALETTE.info() stroke-width="2"/>');
    if (good <= xmax) parts.push(dot(sx(good), sy(o.target_px), PALETTE.ok()));
    if (marg <= xmax) parts.push(dot(sx(marg), sy(o.target_px / 2), PALETTE.warn()));
    parts.push(line(sx(state.dist), y0, sx(state.dist), y1, PALETTE.distLine, "2 3"));
    parts.push(txt(sx(state.dist), y1 + 10, state.dist + "ft", PALETTE.distLine, "middle"));
    parts.push(txt(14, (y0 + y1) / 2, "px wide", PALETTE.axisLabel, "middle", "rotate(-90 14 " + ((y0 + y1) / 2) + ")"));
    parts.push(txt((x0 + x1) / 2, H - 4, "distance (ft)", PALETTE.axisLabel, "middle"));
    $("plan-chart").innerHTML = parts.join("");

    var dId = doriDistFt(detW, hfov, P.dori.identification);
    var dRec = doriDistFt(detW, hfov, P.dori.recognition);
    $("dori-legend").innerHTML = "DORI (Frigate's face-rec guide): identify ≤ <b style='color:" +
      PALETTE.laneVehicle() + "'>" + dId.toFixed(0) + " ft</b>, recognise ≤ <b style='color:" +
      PALETTE.laneVehicle() + "'>" + dRec.toFixed(0) + " ft</b>.";
  }

  // ---- top-down FOV wedge ----
  function drawWedge(hfov, good, marg) {
    var CX = 180, CY = 330, RMAX = 285;
    var rawScale = Math.max(marg * 1.12, state.dist * 1.1, 8);
    var step = niceStep(rawScale);
    var scaleFt = step * Math.ceil(rawScale / step);
    var rpx = function (ft) { return Math.min(ft / scaleFt, 1) * RMAX; };
    function edge(d, r) { var a = rad(d); return { x: CX + Math.sin(a) * r, y: CY - Math.cos(a) * r }; }
    function arc(d1, d2, r, color, dash, width) {
      var p1 = edge(d1, r), p2 = edge(d2, r), large = (d2 - d1) > 180 ? 1 : 0;
      return '<path d="M ' + f(p1.x) + ' ' + f(p1.y) + ' A ' + f(r) + ' ' + f(r) + ' 0 ' + large +
        ' 1 ' + f(p2.x) + ' ' + f(p2.y) + '" fill="none" stroke="' + color + '" stroke-width="' +
        (width || 1) + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : "") + "/>";
    }
    var half = hfov / 2, lEdge = edge(-half, RMAX), rEdge = edge(half, RMAX), large = hfov > 180 ? 1 : 0;
    var parts = ['<rect x="0" y="0" width="360" height="360" fill=PALETTE.deep()/>'];
    parts.push('<path d="M ' + CX + ' ' + CY + ' L ' + f(lEdge.x) + ' ' + f(lEdge.y) + ' A ' + RMAX +
      ' ' + RMAX + ' 0 ' + large + ' 1 ' + f(rEdge.x) + ' ' + f(rEdge.y) +
      ' Z" fill=PALETTE.info() fill-opacity="0.16" stroke=PALETTE.info() stroke-opacity="0.5" stroke-width="1"/>');
    for (var rft = step; rft <= scaleFt + 0.01; rft += step) {
      parts.push(arc(-half, half, rpx(rft), PALETTE.gridLine, null, 1));
      parts.push(txt(CX - 5, CY - rpx(rft) + 3, rft + " ft", PALETTE.axisLabel, "end"));
    }
    if (rpx(marg) <= RMAX) {
      parts.push(arc(-half, half, rpx(marg), PALETTE.warn(), "4 3", 1.6));
      parts.push(txt(CX + 6, CY - rpx(marg) + 3, "marg " + marg.toFixed(0) + " ft", PALETTE.warn(), "start"));
    }
    parts.push(arc(-half, half, rpx(good), PALETTE.ok(), null, 2));
    parts.push(txt(CX + 6, CY - rpx(good) + 3, "good " + good.toFixed(0) + " ft", PALETTE.ok(), "start"));
    if (rpx(state.dist) <= RMAX) parts.push(dot(CX, CY - rpx(state.dist), PALETTE.distLine));
    parts.push('<circle cx="' + CX + '" cy="' + CY + '" r="4" fill=PALETTE.info() stroke=PALETTE.white stroke-width="1.2"/>');
    parts.push(txt(CX, CY + 18, hfov.toFixed(0) + "° HFOV", PALETTE.chromeLabel, "middle"));
    $("plan-wedge").innerHTML = parts.join("");
  }

  // ---- side elevation: VFOV cone -> ground, to-scale subject ----
  function drawElevation(vfov, subjBot, subjTop, eye, cov, inFrame) {
    var W = 560, H = 300, padL = 40, padR = 16, padT = 16, padB = 28;
    var plotW = W - padL - padR, plotH = H - padT - padB, gy = H - padB;
    var farFt = cov.far == null ? Math.max(state.dist * 1.4, 30) : cov.far;
    var maxX = Math.max(farFt, state.dist * 1.15, 12);
    var maxY = Math.max(state.heightFt, subjTop, eye, 6) * 1.12;
    var s = Math.min(plotW / maxX, plotH / maxY); // equal scale -> true angles
    var sx = function (ft) { return padL + ft * s; };
    var sy = function (ft) { return gy - ft * s; };
    var camX = sx(0), camY = sy(state.heightFt);
    var parts = ['<rect x="0" y="0" width="' + W + '" height="' + H + '" fill=PALETTE.deep()/>'];

    // VFOV cone rays
    var bot = state.tiltDeg + vfov / 2, top = state.tiltDeg - vfov / 2;
    function rayEnd(dep) {
      if (dep > 0) {
        var xg = state.heightFt / Math.tan(rad(dep));
        if (xg <= maxX) return { x: xg, y: 0 };
        return { x: maxX, y: state.heightFt - maxX * Math.tan(rad(dep)) };
      }
      return { x: maxX, y: state.heightFt + maxX * Math.tan(rad(-dep)) }; // up toward horizon
    }
    var te = rayEnd(top), be = rayEnd(bot);
    parts.push('<polygon points="' + f(camX) + "," + f(camY) + " " + f(sx(te.x)) + "," + f(sy(te.y)) +
      " " + f(sx(be.x)) + "," + f(sy(be.y)) + '" fill=PALETTE.info() fill-opacity="0.13"/>');
    parts.push(line(camX, camY, sx(te.x), sy(te.y), PALETTE.info(), null));
    parts.push(line(camX, camY, sx(be.x), sy(be.y), PALETTE.info(), null));

    // ground + coverage swath
    parts.push(line(padL, gy, sx(maxX), gy, PALETTE.ground));
    if (cov.near < maxX) {
      var fEnd = cov.far == null ? maxX : Math.min(cov.far, maxX);
      parts.push('<rect x="' + f(sx(cov.near)) + '" y="' + f(gy) + '" width="' + f((fEnd - cov.near) * s) +
        '" height="5" fill=PALETTE.ok() fill-opacity="0.5"/>');
      parts.push(line(sx(cov.near), gy - 5, sx(cov.near), gy + 5, PALETTE.ok()));
      parts.push(txt(sx(cov.near), gy + 16, cov.near.toFixed(0) + "ft", PALETTE.ok(), "middle"));
      if (cov.far != null && cov.far <= maxX) {
        parts.push(line(sx(cov.far), gy - 5, sx(cov.far), gy + 5, PALETTE.ok()));
        parts.push(txt(sx(cov.far), gy + 16, cov.far.toFixed(0) + "ft", PALETTE.ok(), "middle"));
      }
    }
    // blind zone under mast
    if (cov.near > 0.2) parts.push('<rect x="' + f(sx(0)) + '" y="' + f(gy) + '" width="' +
      f(cov.near * s) + '" height="5" fill=PALETTE.blindZone fill-opacity="0.6"/>');

    // mast + camera
    parts.push(line(camX, gy, camX, camY, PALETTE.axisLabel, null, 2));
    parts.push('<circle cx="' + f(camX) + '" cy="' + f(camY) + '" r="4" fill=PALETTE.info() stroke=PALETTE.white stroke-width="1.2"/>');
    parts.push(txt(camX + 6, camY - 4, state.heightFt + "ft", PALETTE.chromeLabel, "start"));

    // face-height reference line across the plot
    parts.push(line(padL, sy(eye), sx(maxX), sy(eye), PALETTE.laneVehicle(), "1 5"));
    parts.push(txt(sx(maxX), sy(eye) - 3, "face " + eye.toFixed(1) + " ft", PALETTE.laneVehicle(), "end"));
    if (state.dist <= maxX) {
      var col = inFrame ? PALETTE.ok() : PALETTE.danger();
      var halfW = Math.max(state.obj.width_ft / 2, 0.2);
      // subject bbox (subjBot..subjTop)
      parts.push('<rect x="' + f(sx(state.dist - halfW)) + '" y="' + f(sy(subjTop)) + '" width="' +
        f(2 * halfW * s) + '" height="' + f((subjTop - subjBot) * s) + '" fill="none" stroke="' + col + '" stroke-width="1.5"/>');
      // camera -> face sightline, down-angle labelled (the point of this view)
      var fdeg = deg(Math.atan((state.heightFt - eye) / state.dist));
      parts.push(line(camX, camY, sx(state.dist), sy(eye), PALETTE.laneVehicle(), "3 2"));
      var mx = (camX + sx(state.dist)) / 2, my = (camY + sy(eye)) / 2;
      parts.push(txt(mx, my - 3, (fdeg >= 0 ? "↓" : "↑") + Math.abs(fdeg).toFixed(0) + "° to face", PALETTE.laneVehicleLight(), "middle"));
      // the face itself, at eye height
      parts.push('<circle cx="' + f(sx(state.dist)) + '" cy="' + f(sy(eye)) + '" r="' +
        f(Math.max(0.35 * s, 2.5)) + '" fill="' + col + '"/>');
      parts.push(txt(sx(state.dist), gy + 16, state.dist + "ft", PALETTE.distLine, "middle"));
    }
    parts.push(txt(W - padR, padT + 4, state.tiltDeg + "° down · " + vfov.toFixed(0) + "° VFOV", PALETTE.chromeLabel, "end"));
    $("plan-elev").innerHTML = parts.join("");
  }

  // ---- tiny SVG helpers ----
  function f(n) { return (+n).toFixed(1); }
  function line(x1, y1, x2, y2, color, dash, width) {
    return '<line x1="' + f(x1) + '" y1="' + f(y1) + '" x2="' + f(x2) + '" y2="' + f(y2) + '" stroke="' +
      color + '"' + (width ? ' stroke-width="' + width + '"' : "") + (dash ? ' stroke-dasharray="' + dash + '"' : "") + "/>";
  }
  function txt(x, y, s, color, anchor, transform) {
    return '<text x="' + f(x) + '" y="' + f(y) + '" fill="' + color + '" font-size="11" font-family="sans-serif"' +
      ' text-anchor="' + (anchor || "start") + '"' + (transform ? ' transform="' + transform + '"' : "") + ">" + s + "</text>";
  }
  function dot(x, y, color) {
    return '<circle cx="' + f(x) + '" cy="' + f(y) + '" r="3.5" fill="' + color + '" stroke=PALETTE.deep() stroke-width="1"/>';
  }
  function niceStep(max) {
    var raw = max / 6, pow = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / pow;
    return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * pow;
  }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // ---- events ----
  $("sel-lens").addEventListener("change", function () {
    state.lensId = this.value; state.hfovOverride = null; state.vfovOverride = null;
    var l = lens(); if (l.type === "varifocal") state.focal = l.focal_min;
    syncLensControls(); recompute();
  });
  $("sel-camera").addEventListener("change", function () {
    var picked = this.value;
    var c = P.cameras.find(function (x) { return x.id === picked; });
    if (!c) return;
    state.lensId = c.lens; state.resId = c.res;
    state.hfovOverride = c.hfov; state.vfovOverride = c.vfov != null ? c.vfov : null;
    state.heightFt = c.mount_ft; state.tiltDeg = c.tilt_deg;
    $("sel-lens").value = c.lens; $("sel-res").value = c.res;
    $("in-height").value = c.mount_ft; $("height-val").textContent = c.mount_ft;
    $("in-tilt").value = c.tilt_deg; $("tilt-val").textContent = c.tilt_deg;
    var l = lens();
    if (l.type === "varifocal") {
      state.focal = clamp(focalFromHfov(c.hfov, l.sensor_width_mm, l.focal_offset_mm), l.focal_min, l.focal_max);
      $("in-focal").value = state.focal;
    } else if (l.type === "custom") { state.customHfov = c.hfov; $("in-hfov").value = c.hfov; }
    syncLensControls(); recompute();
  });
  $("in-focal").addEventListener("input", function () {
    state.focal = +this.value; state.hfovOverride = null; state.vfovOverride = null; recompute();
  });
  $("in-hfov").addEventListener("input", function () {
    state.customHfov = +this.value; state.vfovOverride = null; recompute();
  });
  $("sel-res").addEventListener("change", function () { state.resId = this.value; state.vfovOverride = null; recompute(); });
  $("sel-object").addEventListener("change", function () { state.objId = this.value; loadObject(); recompute(); });
  $("in-dist").addEventListener("input", function () {
    state.dist = +this.value; $("dist-val").textContent = (+this.value).toFixed(1).replace(/\.0$/, ""); recompute();
  });
  $("in-height").addEventListener("input", function () {
    state.heightFt = +this.value; $("height-val").textContent = (+this.value).toFixed(1).replace(/\.0$/, ""); recompute();
  });
  $("in-tilt").addEventListener("input", function () {
    state.tiltDeg = +this.value; $("tilt-val").textContent = this.value; recompute();
  });
  $("in-faceht").addEventListener("input", function () {
    state.faceHeightFt = +this.value; $("faceht-val").textContent = (+this.value).toFixed(1); recompute();
  });
  ["in-owidth", "in-oaspect", "in-otarget"].forEach(function (id) {
    $(id).addEventListener("input", function () {
      state.obj = {
        width_ft: +$("in-owidth").value || 0.01,
        aspect: +$("in-oaspect").value || 0.01,
        target_px: +$("in-otarget").value || 1,
      };
      recompute();
    });
  });

  document.querySelectorAll(".vbtn").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".vbtn").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      setView(b.dataset.view);
    });
  });

  // ---- init ----
  syncLensControls(); loadObject(); setView("top"); recompute();
})();
