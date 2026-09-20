// The reel: a fixed aperture over a vertically moving column of time.
//
// Reading the layout left to right: time, then severity, then object tracks,
// then the tide coming in from the right edge. The tide is behind everything --
// motion is a steering signal rather than a readable quantity, so it gets area
// and low contrast instead of a column of its own. That order, the lane
// vocabulary, the rungs and the aperture are Elsinore's (Elsinore/ReelScrubberView.swift,
// ReelGranularity.swift); this is the same instrument on the same /v1 endpoints.
//
// Replaces a 72px horizontal strip. Three measurements from this fleet forced
// that, and each is cited at the code it explains:
//
//   * median tracked-object duration is 2s. At a 6h window across ~1200px that
//     is 0.11px, so every event was floored to an identical 2px tick and the
//     span carried nothing. Hence the rungs.
//   * 132 pairs of events overlap in time per day on gate-face. All of them
//     were drawn on one 5px row, on top of each other. Hence the lanes.
//   * detection scores are bimodal and high (p10 0.83, median 0.87), so
//     confidence separates nothing here. The channel it would have spent goes
//     to review severity instead. Hence the spine.
(function () {
  var camSel = document.getElementById("sv-camera");
  var canvas = document.getElementById("sv-reel");
  var frame = document.getElementById("sv-frame");
  var clock = document.getElementById("sv-clock");
  var moment = document.getElementById("sv-moment");
  var rungBox = document.getElementById("sv-rungs");
  var laneBox = document.getElementById("sv-lanes");
  var status = document.getElementById("sv-status");
  var frigateLink = document.getElementById("sv-frigate-link");
  if (!camSel || !canvas) return;

  // The six rungs ride the six sprite tiers the cache already generates --
  // scrub.recent_interval_s (1), aged_interval_s (5) and the four
  // derived_intervals_s (60/300/900/3600) -- at one cell per row. `major` is
  // the clock boundary a person actually counts in at that rung, which is not
  // simply the next rung up: at 5s a row, counting by minutes is legible and
  // counting by 15s is not.
  var RUNGS = [
    { s: 1, label: "1s", major: 60 },
    { s: 5, label: "5s", major: 60 },
    { s: 60, label: "1m", major: 3600 },
    { s: 300, label: "5m", major: 3600 },
    { s: 900, label: "15m", major: 86400 },
    { s: 3600, label: "1h", major: 86400 }
  ];
  var LANES = ["person", "vehicle", "animal", "package"];
  // Elsinore's table. An unrecognised label is deliberately laneless and
  // dropped rather than forced into a lane: a Frigate release that adds a
  // label should be invisible here, not silently counted as an animal.
  var LANE_OF = {};
  [["animal", "bird cat deer dog fox horse rabbit raccoon skunk squirrel"],
   ["vehicle", "bicycle boat bus car motorcycle train truck"],
   ["person", "person"],
   ["package", "package amazon usps fedex ups dhl"]]
    .forEach(function (pair) {
      pair[1].split(" ").forEach(function (l) { LANE_OF[l] = pair[0]; });
    });

  var ROW_H = 34;
  // Above this, one row is long enough that a track would be a few pixels tall
  // and its entry/exit caps would assert a precision the rung cannot show. The
  // lanes come back as per-row counts instead (ReelGranularity.objects).
  var COUNTS_ABOVE = 600;
  // The scrub cache is the only source of stills, so there is no reason to hold
  // a window wider than it can ever fill.
  var RETENTION_S = 4 * 86400;
  var MAX_MOTION_POINTS = 4000;   // keep the response small; the cap in the API is 20k
  var MAX_WINDOW_S = 26 * 3600;   // the widest window we will ask for in one go
  var LOAD_COOLDOWN_MS = 3000;    // floor between refreshes that are not strictly needed

  var state = {
    camera: (function () { var c = camSel.querySelector(".vis-chip.on"); return c ? c.dataset.cam : ""; })(),
    rung: 2,
    cursor: null,
    lanes: {},
    selected: null,
    reel: null,
    sheets: [],
    win: null,          // the window currently loaded
    loading: false,
    images: {},         // url -> Image; the browser cache does the real work
    tracks: []          // last placement, for hit-testing
  };
  LANES.forEach(function (l) { state.lanes[l] = true; });

  function cssVar(n) {
    return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  }
  // Canvas has no cascade, so the reel reads the site's own mono stack rather
  // than naming a family here that the stylesheet may not be loading.
  function mono(px) { return px + 'px ' + (cssVar("--font-mono") || "ui-monospace, monospace"); }
  function rung() { return RUNGS[state.rung]; }
  function pxPerS() { return ROW_H / rung().s; }
  function reelH() { return canvas.clientHeight || 320; }
  function visibleSpan() { return reelH() / pxPerS(); }
  function now() { return Date.now() / 1000; }
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function hhmmss(t) {
    var d = new Date(t * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  function hhmm(t) {
    var d = new Date(t * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }
  // The moment card is built with innerHTML, and three of the fields in it are
  // not ours: `sub_label` is whatever the face/plate/carrier recogniser wrote,
  // `zones` come from Frigate's config, `label` from its model. None should be
  // able to close a tag.
  function esc(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function laneOf(ev) { return LANE_OF[ev.label] || null; }
  function evEnd(ev) { return ev.end === null || ev.end === undefined ? now() : ev.end; }
  function laneOn(ev) { var l = laneOf(ev); return l && state.lanes[l]; }

  // --- Windowing -----------------------------------------------------------
  // One fetch covers several screens so a drag is never gated on the network,
  // and a refetch fires only when the cursor nears the edge of what is loaded.
  function windowFor(t) {
    var want = Math.min(Math.max(visibleSpan() * 3, 1800), MAX_WINDOW_S);
    var end = Math.min(now(), t + want / 2);
    var start = Math.max(end - want, now() - RETENTION_S);
    // `span` is what we actually hold, not what we asked for. Near the
    // retention floor those differ, and every edge test below is against the
    // real extent -- a window recorded as wider than it is never reloads.
    return { start: start, end: end, span: end - start };
  }

  // True only when the loaded window genuinely cannot answer where the cursor
  // is going. Keeping the live edge fresh is a separate, rate-limited job (the
  // interval at the bottom): folding it in here made every pointermove near
  // "now" a fetch.
  function needsLoad() {
    if (!state.win || !state.reel) return true;
    var edge = state.win.span * 0.2;
    // Running out of loaded past, and there is more past to be had.
    if (state.cursor < state.win.start + edge && state.win.start > now() - RETENTION_S) {
      return true;
    }
    // Scrubbed past the end of what is loaded.
    if (state.cursor > state.win.end + 1) return true;
    // A coarser rung wants more context than the window holds -- unless we are
    // already at the widest window we will fetch. Without that second clause
    // the 1h rung asks for 18h on screen, can never satisfy `span/2`, and
    // reloads forever.
    return visibleSpan() > state.win.span / 2 && state.win.span < MAX_WINDOW_S - 1;
  }

  // A failed fetch used to leave the previous camera's reel/frame on screen
  // with only the status line saying "unavailable" -- easy to mistake for
  // the current camera having gone quiet. Clear the stale picture and name
  // the failure, with a Retry that re-issues the same load.
  function showLoadError(message) {
    state.reel = null;
    state.sheets = [];
    state.win = null;
    setStatus(message);
    drawReel();
    if (moment) moment.innerHTML = "";
    frame.style.backgroundImage = "none";
    frame.style.aspectRatio = "";
    frame.textContent = "";
    frame.appendChild(SC.el("div", { class: "empty error", text: message }, []));
    var retry = SC.el("button", { type: "button", class: "btn-neutral", text: "Retry" }, []);
    retry.addEventListener("click", function () { load(true); });
    frame.appendChild(retry);
  }

  var lastLoadAt = 0;
  async function load(force) {
    if (state.loading) return;
    // A load that is not strictly needed waits out the cooldown; one that is
    // (the cursor is outside what we hold) never does.
    var outside = !state.win || state.cursor < state.win.start || state.cursor > state.win.end;
    if (!force && !outside && Date.now() - lastLoadAt < LOAD_COOLDOWN_MS) return;
    lastLoadAt = Date.now();
    state.loading = true;
    setStatus("loading…");
    var w = windowFor(state.cursor);
    // The sidecar re-buckets motion to whatever scale is asked for, so the reel
    // can simply ask for one reading per row -- except where that would make a
    // series long enough to be worth nobody's bandwidth.
    var scale = Math.max(rung().s, (w.end - w.start) / MAX_MOTION_POINTS);
    var q = "?start=" + w.start + "&end=" + w.end;
    try {
      var res = await Promise.all([
        fetch("/v1/reel/" + encodeURIComponent(state.camera) + q + "&motion_scale=" + scale),
        fetch("/v1/scrub/" + encodeURIComponent(state.camera) + "/sheets" + q)
      ]);
      if (!res[0].ok || !res[1].ok) {
        var code = !res[0].ok ? res[0].status : res[1].status;
        showLoadError("Failed to load — HTTP " + code);
        return;
      }
      state.reel = await res[0].json();
      var sheets = (await res[1].json()).sheets || [];
      // Finest tier first, so the frame lookup lands on the highest-cadence
      // sheet that covers the moment rather than whichever tier sorted first.
      sheets.sort(function (a, b) { return a.interval - b.interval || a.start - b.start; });
      state.sheets = sheets;
      state.win = w;
    } catch (e) {
      showLoadError("Failed to load — " + (e && e.message ? e.message : "network error"));
      return;
    } finally {
      state.loading = false;
    }
    render();
  }

  function setStatus(text) { if (status) status.textContent = text; }

  // --- Reel ----------------------------------------------------------------
  function drawReel() {
    var dpr = window.devicePixelRatio || 1;
    var W = canvas.clientWidth || 300;
    var H = reelH();
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = cssVar("--deep");
    ctx.fillRect(0, 0, W, H);
    if (!state.reel) return;

    var R = rung();
    var pps = pxPerS();
    var cursor = state.cursor;
    function y(t) { return H / 2 + (t - cursor) * pps; }
    var span = H / pps;
    var t0 = cursor - span / 2 - R.s;
    var t1 = cursor + span / 2 + R.s;
    var liveT = now();

    // Columns. The time column is the readout's own ground; nothing else draws
    // into it, so a timestamp is never crossed by a rule or a track.
    var TIME_W = W < 240 ? 46 : 56;
    var SPINE_X = TIME_W + 4, SPINE_W = 7;
    var LANE_X0 = SPINE_X + SPINE_W + 7;
    var LANE_W = Math.max(16, Math.min(25, (W - LANE_X0 - 50) / 4));
    var LANE_RIGHT = LANE_X0 + LANE_W * 4;
    var TIDE_MAX = Math.max(28, W - LANE_RIGHT - 8);
    var MONO = mono(11);

    // Tide -- motion, behind everything, in from the right edge.
    var m = state.reel.motion;
    if (m && m.values && m.values.length) {
      var max = 1;
      m.values.forEach(function (v) { if (v > max) max = v; });
      var i0 = Math.max(0, Math.floor((t0 - m.start) / m.interval));
      var i1 = Math.min(m.values.length - 1, Math.ceil((t1 - m.start) / m.interval));
      if (i1 > i0) {
        var px = function (i) { return W - ((m.values[i] || 0) / max) * TIDE_MAX; };
        var ty = function (i) { return y(m.start + i * m.interval); };
        ctx.beginPath();
        ctx.moveTo(W, ty(i0));
        for (var i = i0; i <= i1; i++) ctx.lineTo(px(i), ty(i));
        ctx.lineTo(W, ty(i1));
        ctx.closePath();
        var g = ctx.createLinearGradient(W - TIDE_MAX, 0, W, 0);
        g.addColorStop(0, "transparent");
        g.addColorStop(1, cssVar("--tide") || cssVar("--muted-3"));
        ctx.globalAlpha = 0.5; ctx.fillStyle = g; ctx.fill(); ctx.globalAlpha = 1;
        ctx.beginPath();
        for (var j = i0; j <= i1; j++) {
          if (j === i0) ctx.moveTo(px(j), ty(j)); else ctx.lineTo(px(j), ty(j));
        }
        ctx.strokeStyle = cssVar("--tide") || cssVar("--muted-3");
        ctx.globalAlpha = 0.85; ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1;
      }
    }

    // Not recorded -- hatched. A 30s hole in a 6h strip was 0.2px of flat
    // --surface-2 and read as quiet; a hole in the evidence should look like one.
    var rec = state.reel.recorded || [];
    var holes = [];
    var prev = state.win.start;
    rec.forEach(function (r) {
      if (r[0] > prev) holes.push([prev, r[0]]);
      prev = Math.max(prev, r[1]);
    });
    if (prev < Math.min(state.win.end, liveT)) holes.push([prev, Math.min(state.win.end, liveT)]);
    ctx.save();
    ctx.beginPath(); ctx.rect(TIME_W, 0, W - TIME_W, H); ctx.clip();
    holes.forEach(function (h) {
      var ya = y(h[0]), yb = y(h[1]);
      if (yb < -4 || ya > H + 4) return;
      var hh = Math.max(2, yb - ya);
      ctx.fillStyle = cssVar("--gap-c") || cssVar("--muted-3");
      ctx.globalAlpha = 0.16;
      ctx.fillRect(TIME_W, ya, W - TIME_W, hh);
      ctx.globalAlpha = 0.5;
      ctx.strokeStyle = cssVar("--gap-c") || cssVar("--muted-3");
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (var d = -hh; d < W; d += 9) {
        ctx.moveTo(TIME_W + d, ya + hh);
        ctx.lineTo(TIME_W + d + hh, ya);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
    ctx.restore();

    // Rows. A row starting on the rung's counting landmark gets the heavier
    // line, so what the eye counts by matches what the rung steps by.
    ctx.font = MONO;
    ctx.textBaseline = "middle";
    var first = Math.floor(t0 / R.s) * R.s;
    for (var t = first; t < t1; t += R.s) {
      var gy = Math.round(y(t)) + 0.5;
      if (gy < -1 || gy > H + 1) continue;
      var major = Math.abs(t % R.major) < 1e-6;
      ctx.strokeStyle = cssVar("--gridline") || cssVar("--stroke");
      ctx.globalAlpha = major ? 0.55 : 0.22;
      ctx.lineWidth = major ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(TIME_W - 6, gy); ctx.lineTo(W, gy); ctx.stroke();
      ctx.globalAlpha = 1;

      // Dead band: the aperture belongs to the amber readout, so rows go silent
      // within half a row of centre and fade back over the next. Without it a
      // reel parked between two detents puts three clocks in the aperture, two
      // of them the same.
      var dist = Math.abs(gy - H / 2) / ROW_H;
      var a = dist < 0.55 ? 0 : Math.min(1, (dist - 0.55) / 0.7);
      if (a > 0.02) {
        ctx.globalAlpha = a * (t > liveT ? 0.35 : 1);
        ctx.fillStyle = major ? cssVar("--text") : cssVar("--muted-3");
        ctx.fillText(R.s < 60 ? hhmmss(t) : hhmm(t), 4, gy - ROW_H / 2);
        ctx.globalAlpha = 1;
      }
    }

    // Severity spine -- Frigate's own alert/detection call, which the strip
    // never drew at all.
    (state.reel.reviews || []).forEach(function (rv) {
      var ya = y(rv.start), yb = y(rv.end === null ? liveT : rv.end);
      if (yb < -2 || ya > H + 2) return;
      var alert = rv.severity === "alert";
      ctx.fillStyle = alert ? cssVar("--accent-2") : cssVar("--muted-3");
      ctx.globalAlpha = alert ? 0.95 : 0.5;
      ctx.fillRect(SPINE_X + (alert ? 0 : 2), ya, alert ? SPINE_W : 3,
                   Math.max(alert ? 3 : 2, yb - ya));
      ctx.globalAlpha = 1;
    });

    // Tracks, or per-lane counts where a span would be a few pixels tall.
    state.tracks = [];
    var evs = (state.reel.events || []).filter(function (e) {
      return laneOn(e) && evEnd(e) > t0 && e.start < t1;
    });

    if (R.s >= COUNTS_ABOVE) {
      ctx.textAlign = "center";
      for (var rt = first; rt < t1; rt += R.s) {
        var ya = y(rt), yb = y(rt + R.s);
        if (yb < 0 || ya > H) continue;
        for (var li = 0; li < LANES.length; li++) {
          var ln = LANES[li];
          if (!state.lanes[ln]) continue;
          var n = 0;
          for (var k = 0; k < evs.length; k++) {
            if (laneOf(evs[k]) === ln && evs[k].start >= rt && evs[k].start < rt + R.s) n++;
          }
          if (!n) continue;
          var cx = LANE_X0 + li * LANE_W + LANE_W / 2;
          var cy = (ya + yb) / 2;
          ctx.fillStyle = cssVar("--lane-" + ln);
          ctx.globalAlpha = 0.3;
          ctx.beginPath();
          ctx.arc(cx, cy, Math.min(11, 4 + Math.sqrt(n) * 2.6), 0, Math.PI * 2);
          ctx.fill();
          ctx.globalAlpha = 1;
          ctx.fillText(String(n), cx, cy);
        }
      }
      ctx.textAlign = "left";
    } else {
      // Sub-column packing inside a lane. Peak concurrency measured 3 across
      // the busy cameras, so two sub-columns clear every real collision; a
      // third simultaneous object of the same kind stacks on the first column
      // rather than being dropped.
      var packed = {};
      LANES.forEach(function (l) { packed[l] = [[], []]; });
      evs.slice().sort(function (a, b) { return a.start - b.start; }).forEach(function (e) {
        var ln = laneOf(e);
        var cols = packed[ln];
        var col = 0;
        for (var c = 0; c < cols.length; c++) {
          var last = cols[c][cols[c].length - 1];
          if (!last || evEnd(last) < e.start) { col = c; break; }
        }
        cols[col].push(e);
        var x = LANE_X0 + LANES.indexOf(ln) * LANE_W + 7 + col * 9;
        var ya2 = y(e.start);
        var yb2 = Math.max(y(evEnd(e)), ya2 + 3);
        state.tracks.push({ e: e, x: x, ya: ya2, yb: yb2, lane: ln });
      });
      state.tracks.forEach(function (tr) {
        var on = state.selected === tr.e.id;
        ctx.strokeStyle = ctx.fillStyle = cssVar("--lane-" + tr.lane);
        ctx.globalAlpha = on ? 1 : 0.78;
        ctx.lineWidth = on ? 3 : 2;
        ctx.beginPath(); ctx.moveTo(tr.x, tr.ya); ctx.lineTo(tr.x, tr.yb); ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillRect(tr.x - 4, tr.ya - 1, 9, 1.6);          // entry: we saw it arrive
        if (tr.e.end !== null && tr.e.end !== undefined) {
          ctx.fillRect(tr.x - 4, tr.yb, 9, 1.6);            // exit: we saw it leave
        } else {
          // No exit cap means "we never saw it leave", not "it left here".
          ctx.globalAlpha = 0.35;
          ctx.beginPath(); ctx.arc(tr.x, tr.yb + 4, 1.6, 0, Math.PI * 2); ctx.fill();
        }
        ctx.globalAlpha = 1;
        if (on) {
          var txt = tr.e.label
            + (tr.e.sub_label ? " · " + tr.e.sub_label : "")
            + (tr.e.zones && tr.e.zones.length ? " · " + tr.e.zones[0] : "");
          ctx.font = MONO;
          var tw = ctx.measureText(txt).width;
          var bx = Math.min(tr.x + 9, W - tw - 9);
          ctx.fillStyle = cssVar("--surface");
          ctx.globalAlpha = 0.93;
          ctx.fillRect(bx - 4, tr.ya - 8, tw + 8, 15);
          ctx.globalAlpha = 1;
          ctx.strokeStyle = cssVar("--lane-" + tr.lane);
          ctx.globalAlpha = 0.5;
          ctx.strokeRect(bx - 4.5, tr.ya - 8.5, tw + 9, 16);
          ctx.globalAlpha = 1;
          ctx.fillStyle = cssVar("--text");
          ctx.fillText(txt, bx, tr.ya);
        }
      });
    }

    // Live edge -- past above, future below.
    var ly = y(liveT);
    if (ly > -2 && ly < H + 2) {
      ctx.strokeStyle = cssVar("--live");
      ctx.globalAlpha = 0.75; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(TIME_W - 6, ly); ctx.lineTo(W - 4, ly); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar("--live");
      ctx.font = mono(10);
      ctx.textAlign = "right"; ctx.fillText("NOW", W - 6, ly - 8); ctx.textAlign = "left";
    }

    // Aperture + readout. Fixed in place; the reel moves behind it.
    var apH = ROW_H * 1.5;
    var apY = Math.round(H / 2 - apH / 2) + 0.5;
    ctx.fillStyle = "rgba(255,255,255,.035)";
    ctx.strokeStyle = "rgba(255,255,255,.20)";
    ctx.lineWidth = 1;
    ctx.fillRect(5, apY, W - 10, apH);
    ctx.strokeRect(5, apY, W - 10, apH);
    ctx.fillStyle = cssVar("--accent-2");
    ctx.font = mono(15);
    ctx.fillText(hhmmss(cursor), 8, H / 2);
    ctx.beginPath(); ctx.moveTo(0, H / 2 - 5); ctx.lineTo(6, H / 2); ctx.lineTo(0, H / 2 + 5); ctx.fill();
    ctx.beginPath(); ctx.moveTo(W, H / 2 - 5); ctx.lineTo(W - 6, H / 2); ctx.lineTo(W, H / 2 + 5); ctx.fill();

    ctx.fillStyle = cssVar("--muted-3");
    ctx.font = mono(10);
    ctx.textAlign = "right";
    ctx.fillText(R.label + " / row", W - 6, 12);
    ctx.textAlign = "left";
  }

  // --- Frame ---------------------------------------------------------------
  function sheetFor(t) {
    for (var i = 0; i < state.sheets.length; i++) {
      var s = state.sheets[i];
      if (t >= s.start && t < s.start + s.interval * s.count) return s;
    }
    return null;
  }

  // A bounded, asymmetric rule rather than a pure floor. An event's start is
  // the first moment of activity, and the floor at that instant lands on the
  // pre-event rest frame -- which can be a whole interval stale and shows an
  // empty scene. So allow a small forward budget before flooring, and fall back
  // to the plain floor when that cell is out of range (the honest answer at the
  // trailing edge of a still-filling sheet).
  function cellIndex(s, t) {
    var budget = Math.min(s.interval / 2, 2);
    var idx = Math.floor((t - s.start + budget) / s.interval);
    if (idx >= 0 && idx < s.count) return idx;
    idx = Math.floor((t - s.start) / s.interval);
    return idx >= 0 && idx < s.count ? idx : null;
  }

  function drawFrame() {
    var t = state.cursor;
    var s = sheetFor(t);
    var idx = s ? cellIndex(s, t) : null;
    if (clock) clock.textContent = new Date(t * 1000).toLocaleString();
    if (frigateLink) {
      frigateLink.href = "/review?id=" + encodeURIComponent(state.camera)
        + "&recording.timestamp=" + Math.floor(t);
    }
    if (!s || idx === null) {
      frame.style.backgroundImage = "none";
      frame.textContent = "no frame cached for this moment";
      return;
    }
    frame.textContent = "";
    var col = idx % s.cols;
    var row = Math.floor(idx / s.cols);
    frame.style.aspectRatio = s.cell_w + " / " + s.cell_h;
    frame.style.backgroundImage = "url('" + s.url + "')";
    frame.style.backgroundSize = (s.cols * 100) + "% " + (s.rows * 100) + "%";
    frame.style.backgroundPosition =
      (s.cols > 1 ? (col * 100) / (s.cols - 1) : 0) + "% " +
      (s.rows > 1 ? (row * 100) / (s.rows - 1) : 0) + "%";
    preload(t, s);
  }

  // Warm the neighbouring sheets so stepping across a boundary is instant. The
  // map is only a handle on the fetch -- the browser cache does the real work --
  // so it is capped: an hour of scrubbing would otherwise pin every decoded
  // sheet it ever touched in memory.
  var PRELOAD_CAP = 40;
  function preload(t, current) {
    var page = current.interval * current.cols * current.rows;
    [t - page, t + page].forEach(function (tt) {
      var s = sheetFor(tt);
      if (!s || state.images[s.url]) return;
      var img = new Image();
      img.src = s.url;
      state.images[s.url] = img;
    });
    var urls = Object.keys(state.images);
    for (var i = 0; i < urls.length - PRELOAD_CAP; i++) delete state.images[urls[i]];
  }

  // --- Moment card ---------------------------------------------------------
  // The answer to "what am I looking at", which the strip could not give:
  // who is here, in which zone, how sure, how long, and whether Frigate
  // treated it as an alert.
  function drawMoment() {
    if (!moment) return;
    if (!state.reel) { moment.innerHTML = ""; return; }
    var t = state.cursor;
    var here = (state.reel.events || []).filter(function (e) {
      return laneOn(e) && t >= e.start - 0.5 && t <= evEnd(e) + 0.5;
    }).sort(function (a, b) { return a.start - b.start; });
    var rv = (state.reel.reviews || []).find(function (r) {
      return t >= r.start && t <= (r.end === null ? now() : r.end);
    });
    var recorded = (state.reel.recorded || []).some(function (r) {
      return t >= r[0] && t < r[1];
    });

    var sevClass = rv ? rv.severity : (recorded ? "none" : "gap");
    var sevText = rv ? rv.severity : (recorded ? "no review" : "not recorded");
    var ago = Math.max(0, Math.round((now() - t) / 60));

    var html = '<div class="sv-moment-head">'
      + '<span class="sv-moment-t">' + esc(hhmmss(t)) + "</span>"
      + '<span class="sv-moment-rel">' + (ago === 0 ? "now" : ago + " min ago") + "</span>"
      + '<span class="sv-sev ' + esc(sevClass) + '">' + esc(sevText) + "</span></div>";

    if (!here.length) {
      html += '<p class="sv-moment-empty">' + (recorded
        ? "Nothing tracked at this moment."
        : "No recording covers this moment — the segment is missing, not empty.")
        + "</p>";
    } else {
      html += '<div class="sv-tracklist">';
      here.forEach(function (e) {
        var ln = laneOf(e);
        var dur = Math.max(1, Math.round(evEnd(e) - e.start));
        html += '<button type="button" class="sv-trk' + (state.selected === e.id ? " on" : "")
          + '" data-id="' + esc(e.id) + '">'
          + '<span class="sv-swatch" style="background:var(--lane-' + esc(ln) + ')"></span>'
          + '<span class="sv-lbl">' + esc(e.label)
          + (e.sub_label ? ' <span class="sv-sub">' + esc(e.sub_label) + "</span>" : "")
          + "</span>"
          + '<span class="sv-zone">'
          + (e.zones && e.zones.length ? esc(e.zones.join(" + ")) : "—") + "</span>"
          + '<span class="sv-meta">' + dur + "s"
          + (e.end === null || e.end === undefined ? " · live" : "")
          + (e.score ? " · " + e.score.toFixed(2) : "")
          + (e.has_clip ? " · clip" : "") + "</span></button>";
      });
      html += "</div>";
    }
    moment.innerHTML = html;
    Array.prototype.forEach.call(moment.querySelectorAll(".sv-trk"), function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.dataset.id;
        var ev = (state.reel.events || []).find(function (e) { return e.id === id; });
        state.selected = state.selected === id ? null : id;
        if (state.selected && ev) state.cursor = clampCursor(ev.start);
        render();
      });
    });
  }

  // --- Render --------------------------------------------------------------
  var frameReq = null;
  function render() {
    if (frameReq) return;
    frameReq = requestAnimationFrame(function () {
      frameReq = null;
      drawReel();
      drawFrame();
      drawMoment();
      if (status && !state.loading) {
        var n = state.reel ? (state.reel.events || []).filter(laneOn).length : 0;
        var alerts = state.reel
          ? (state.reel.reviews || []).filter(function (r) { return r.severity === "alert"; }).length
          : 0;
        setStatus(n + " tracked · " + alerts + " alert" + (alerts === 1 ? "" : "s")
          + " · " + Math.round(visibleSpan() / 60) + " min on screen");
      }
    });
  }

  function clampCursor(t) {
    return Math.min(now(), Math.max(now() - RETENTION_S, t));
  }
  function moveTo(t) {
    state.cursor = clampCursor(t);
    render();
    if (needsLoad()) load();
  }

  // --- Gestures ------------------------------------------------------------
  // Drag down = earlier: turning the drum toward you, the app's orientation.
  var dragY = null, moved = 0, lastT = 0, velocity = 0, coasting = null;

  function stopCoast() {
    if (coasting) { cancelAnimationFrame(coasting); coasting = null; }
  }

  // Where a release comes to rest. Under a minute a row, the picture cadence is
  // comparable to the row, so the reel settles on a cached frame; past that the
  // row is the coarser fact and wins (ReelGranularity.snapsToFrames).
  function snapCursor() {
    if (rung().s <= 60) {
      var sh = sheetFor(state.cursor);
      if (sh) {
        moveTo(sh.start + Math.round((state.cursor - sh.start) / sh.interval) * sh.interval);
        return;
      }
    }
    moveTo(Math.round(state.cursor / rung().s) * rung().s);
  }

  canvas.style.touchAction = "none";
  canvas.addEventListener("pointerdown", function (e) {
    stopCoast();
    dragY = e.clientY; moved = 0; velocity = 0;
    lastT = performance.now();
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", function (e) {
    if (dragY === null) return;
    var dy = e.clientY - dragY;
    var t = performance.now();
    // Smoothed, so one jittery frame before release does not fling the reel.
    if (t > lastT) velocity = 0.6 * velocity + 0.4 * (dy / ((t - lastT) / 1000));
    lastT = t;
    dragY = e.clientY;
    moved += Math.abs(dy);
    moveTo(state.cursor - dy / pxPerS());
  });
  function release(e) {
    if (dragY === null) return;
    dragY = null;
    if (moved >= 4) {                     // a drag: coast, then settle
      if (Math.abs(velocity) > 220) {
        coasting = requestAnimationFrame(function coast() {
          velocity *= 0.94;
          if (Math.abs(velocity) < 30) { coasting = null; snapCursor(); return; }
          moveTo(state.cursor - (velocity / 60) / pxPerS());
          coasting = requestAnimationFrame(coast);
        });
      } else {
        snapCursor();
      }
      return;
    }
    var rect = canvas.getBoundingClientRect();
    var px = e.clientX - rect.left, py = e.clientY - rect.top;
    var best = null, bd = 14;
    state.tracks.forEach(function (tr) {
      var dx = Math.abs(tr.x - px);
      var dy = py < tr.ya ? tr.ya - py : (py > tr.yb ? py - tr.yb : 0);
      var d = Math.sqrt(dx * dx + dy * dy);
      if (d < bd) { bd = d; best = tr; }
    });
    state.selected = best ? best.e.id : null;
    if (best) moveTo(best.e.start); else render();
  }
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", function () { dragY = null; stopCoast(); });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    stopCoast();
    moveTo(state.cursor + e.deltaY / pxPerS());
  }, { passive: false });

  // Fine scrub: drag the image itself, one cached frame per 14px.
  var fineX = null;
  if (frame) {
    frame.style.touchAction = "pan-y";
    frame.addEventListener("pointerdown", function (e) { fineX = e.clientX; });
    frame.addEventListener("pointermove", function (e) {
      if (fineX === null) return;
      var dx = e.clientX - fineX;
      if (Math.abs(dx) < 14) return;
      fineX = e.clientX;
      var s = sheetFor(state.cursor);
      moveTo(state.cursor + (dx > 0 ? 1 : -1) * (s ? s.interval : 5));
    });
    frame.addEventListener("pointerup", function () { fineX = null; });
    frame.addEventListener("pointercancel", function () { fineX = null; });
  }

  window.addEventListener("keydown", function (e) {
    if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowUp") { moveTo(state.cursor - rung().s); e.preventDefault(); }
    if (e.key === "ArrowDown") { moveTo(state.cursor + rung().s); e.preventDefault(); }
    if (e.key === "ArrowLeft") { setRung(state.rung + 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { setRung(state.rung - 1); e.preventDefault(); }
  });

  function setRung(i) {
    var next = Math.max(0, Math.min(RUNGS.length - 1, i));
    if (next === state.rung) return;
    state.rung = next;
    Array.prototype.forEach.call(rungBox.querySelectorAll("button"), function (b) {
      b.setAttribute("aria-pressed", String(+b.dataset.i === state.rung));
    });
    render();
    if (needsLoad()) load();
  }

  // --- Controls ------------------------------------------------------------
  rungBox.innerHTML = RUNGS.map(function (r, i) {
    return '<button type="button" data-i="' + i + '" aria-pressed="' + (i === state.rung)
      + '" title="' + r.label + ' per row">' + r.label + "</button>";
  }).join("");
  Array.prototype.forEach.call(rungBox.querySelectorAll("button"), function (b) {
    b.addEventListener("click", function () { setRung(+b.dataset.i); });
  });

  laneBox.innerHTML = LANES.map(function (l) {
    return '<button type="button" class="sv-chip" data-l="' + l + '" aria-pressed="true">'
      + '<span class="sv-dot" style="background:var(--lane-' + l + ')"></span>' + l + "</button>";
  }).join("");
  Array.prototype.forEach.call(laneBox.querySelectorAll(".sv-chip"), function (b) {
    b.addEventListener("click", function () {
      var l = b.dataset.l;
      state.lanes[l] = !state.lanes[l];
      // Turning the last lane off is not a state anyone means to be in: it
      // empties the reel and reads as broken rather than as a narrow filter.
      if (!LANES.some(function (x) { return state.lanes[x]; })) {
        LANES.forEach(function (x) { state.lanes[x] = true; });
      }
      Array.prototype.forEach.call(laneBox.querySelectorAll(".sv-chip"), function (c) {
        c.setAttribute("aria-pressed", String(!!state.lanes[c.dataset.l]));
      });
      render();
    });
  });

  camSel.addEventListener("click", function (e) {
    var chip = e.target.closest(".vis-chip");
    if (!chip || chip.classList.contains("on")) return;
    var chips = camSel.querySelectorAll(".vis-chip");
    for (var i = 0; i < chips.length; i++) chips[i].classList.toggle("on", chips[i] === chip);
    state.camera = chip.dataset.cam;
    state.selected = null;
    state.reel = null;
    state.sheets = [];
    state.win = null;
    load(true);
  });
  window.addEventListener("resize", function () { render(); if (needsLoad()) load(); });

  // Keep the live edge honest while parked near it, without fighting a drag.
  setInterval(function () {
    if (dragY !== null || coasting || state.loading) return;
    if (state.win && now() - state.cursor < 300) load();
  }, 30000);

  state.cursor = now() - 60;
  load(true);
})();
