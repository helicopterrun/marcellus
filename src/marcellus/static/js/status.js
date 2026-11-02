// Status dashboard refresh: poll /status.json every ~10s and patch the
// [data-k] cells in place — no HTML re-render, so focus/scroll survive.
// Paused while the tab is hidden.
(function () {
  var INTERVAL_MS = 10000;
  var timer = null;

  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var size = n;
    for (var i = 0; i < units.length; i++) {
      if (size < 1024 || i === units.length - 1) {
        return (i <= 1 ? Math.round(size) : size.toFixed(1)) + " " + units[i];
      }
      size /= 1024;
    }
  }

  function setCell(k, text, cls) {
    var n = document.querySelector('[data-k="' + k + '"]');
    if (!n) return;
    n.textContent = text;
    if (cls !== undefined) n.className = "stat-value cell-class " + cls;
  }

  // A failed poll used to leave every cell showing stale values with no
  // indication anything was wrong. This banner (built lazily, since neither
  // status.html nor cameras.html -- both load this script -- carry a fixed
  // banner mount point) says so, and clears on the next successful tick.
  var staleShowBanner = null;
  var staleBannerNode = null;
  var lastGoodAt = null;

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function hhmm(ms) {
    var d = new Date(ms);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  // --- Verdict banner (status page only) ---------------------------------
  // Rendered from the `verdict` key of /status.json: {level, problems, notes}.
  function renderVerdict(v) {
    var root = document.getElementById("verdict");
    if (!root || !v) return;
    var level = v.level || "warn";
    var problems = v.problems || [];
    var notes = v.notes || [];
    root.className = "verdict verdict-" + level;
    var head = root.querySelector('[data-verdict="head"]');
    var plist = root.querySelector('[data-verdict="problems"]');
    var nlist = root.querySelector('[data-verdict="notes"]');
    head.textContent = level === "ok" && !problems.length ? "All systems OK"
      : problems.length + " problem" + (problems.length === 1 ? "" : "s");
    plist.textContent = "";
    problems.forEach(function (p) {
      var li = document.createElement("li");
      li.className = p.level || "";
      if (p.href) {
        var a = document.createElement("a");
        a.href = p.href;
        a.textContent = p.message;
        li.appendChild(a);
      } else {
        li.textContent = p.message;
      }
      plist.appendChild(li);
    });
    nlist.textContent = "";
    notes.forEach(function (n) {
      var li = document.createElement("li");
      li.textContent = n;
      nlist.appendChild(li);
    });
  }

  function showStaleBanner() {
    if (document.getElementById("verdict")) {
      // Never keep showing a stale "All systems OK" when the data is gone.
      var root = document.getElementById("verdict");
      root.className = "verdict verdict-warn";
      root.querySelector('[data-verdict="head"]').textContent =
        "Can't reach the status data";
      var pl = root.querySelector('[data-verdict="problems"]');
      pl.textContent = "";
      var li = document.createElement("li");
      li.className = "warn";
      li.textContent = "The page couldn't load /status.json" +
        (lastGoodAt ? " since " + hhmm(lastGoodAt) : "") +
        ", so the tiles below may be out of date. Retrying every 10 seconds.";
      pl.appendChild(li);
      root.querySelector('[data-verdict="notes"]').textContent = "";
      return;
    }
    if (!staleShowBanner) {
      var main = document.getElementById("main-content") || document.body;
      staleBannerNode = document.createElement("div");
      staleBannerNode.className = "help page-intro";
      staleBannerNode.style.display = "none";
      staleBannerNode.setAttribute("role", "status");
      main.insertBefore(staleBannerNode, main.firstChild);
      staleShowBanner = SC.banner(staleBannerNode);
    }
    staleShowBanner(
      "Updating failed — showing values from " + (lastGoodAt ? hhmm(lastGoodAt) : "startup") + ".",
      true
    );
  }

  function clearStaleBanner() {
    if (staleBannerNode) staleBannerNode.style.display = "none";
  }

  function lagHtml(lag) {
    if (lag === null || lag === undefined) return "—";
    if (lag < 120) return '<span class="cell-class ok">' + Math.round(lag) + "s</span>";
    if (lag < 600) return '<span class="cell-class warn">' + Math.round(lag) + "s</span>";
    return '<span class="cell-class noise">' + Math.round(lag / 60) + "m</span>";
  }

  // --- Collapsible groups (status page) -----------------------------------
  var GROUPS_KEY = "marcellus.status.groups";
  function loadOpenGroups() {
    try {
      var raw = window.localStorage.getItem(GROUPS_KEY);
      var parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) { return {}; }
  }
  function saveOpenGroup(name, open) {
    try {
      var cur = loadOpenGroups();
      cur[name] = open;
      window.localStorage.setItem(GROUPS_KEY, JSON.stringify(cur));
    } catch (e) { /* storage unavailable: the page works without it */ }
  }
  var groupFlagged = {};
  function initGroups() {
    var stored = loadOpenGroups();
    document.querySelectorAll("details.stat-group").forEach(function (d) {
      var name = d.getAttribute("data-group");
      if (stored[name] === true) d.open = true;
      d.addEventListener("toggle", function () {
        if (d.dataset.auto) { delete d.dataset.auto; return; }
        saveOpenGroup(name, d.open);
      });
    });
  }
  // Open any group with a warning/bad tile (once per transition into
  // warning, so a viewer who closes it isn't fought on every poll) and show
  // the marker on its summary.
  function updateGroupFlags() {
    document.querySelectorAll("details.stat-group").forEach(function (d) {
      var name = d.getAttribute("data-group");
      var flagged = !!d.querySelector(
        ".cell-class.warn, .cell-class.noise, .hw-bar-fill.warn, .hw-bar-fill.noise");
      var marker = d.querySelector(".grp-flag");
      if (marker) marker.hidden = !flagged;
      if (flagged && !groupFlagged[name] && !d.open) {
        d.dataset.auto = "1";
        d.open = true;
      }
      groupFlagged[name] = flagged;
    });
  }
  function setDigest(name, text) {
    var n = document.querySelector('[data-digest="' + name + '"]');
    if (n && text) n.textContent = text;
  }
  function updateDigests(s) {
    var sc = s.scrub || {};
    var sizes = s.sizes || {};
    if (sc.enabled) {
      setDigest("scrub", "cycle " + (sc.last_cycle_s_ago !== null && sc.last_cycle_s_ago !== undefined
        ? sc.last_cycle_s_ago + "s ago" : "—") + ", cache " +
        (sizes.scrub_cache_capped ? "≥ " : "") + fmtBytes(sizes.scrub_cache));
    } else {
      setDigest("scrub", "scrub off, Marcellus DB " + fmtBytes(sizes.sidecar_db));
    }
    var hw = s.hardware || {};
    var parts = [];
    var dets = Object.keys(hw.detectors || {});
    if (dets.length) parts.push("inference " + hw.detectors[dets[0]] + " ms");
    if (hw.frigate_cpu !== undefined) parts.push("CPU " + hw.frigate_cpu + "%");
    setDigest("hardware", parts.join(", "));
    var pc = (s.push_stats || {}).counters || {};
    var pg = (s.push_stats || {}).gauges || {};
    setDigest("push", (pc["relay.send.ok"] || 0) + " sent, " + (pc["relay.send.failed"] || 0) +
      " failed, breaker " + (pg["relay.breaker.state"] ? "open" : "closed"));
    var c = s.counts || {};
    setDigest("counters", ((c.push_24h || {}).sent_total || 0) + " pushes (24h), " +
      (c.live_activities_open || 0) + " Live Activities open");
  }

  async function tick() {
    var s;
    try {
      s = await SC.fetchJson("/status.json");
    } catch (e) {
      showStaleBanner();
      return; // transient — try again next tick
    }
    lastGoodAt = Date.now();
    clearStaleBanner();
    renderVerdict(s.verdict);
    setCell("frigate", s.frigate.reachable ? s.frigate.version : "unreachable",
      s.frigate.reachable ? "ok" : "noise");
    setCell("scrub", s.scrub.enabled ? "on" : "off", s.scrub.enabled ? "ok" : "muted");
    setCell("push", s.push.enabled ? "on" : "off", s.push.enabled ? "ok" : "muted");
    setCell("devices", String(s.push.device_count));
    var rec = s.recording || {};
    var recStalled = rec.stalled || [];
    setCell("recording", !rec.enabled ? "off" : recStalled.length ? recStalled.length + " stalled" : "ok",
      !rec.enabled ? "muted" : recStalled.length ? "noise" : "ok");
    var recDetail = document.querySelector('[data-k="recording-detail"]');
    if (recDetail) recDetail.textContent = recStalled.map(function (c) { return c.camera; }).join(", ");
    setCell("last-cycle", s.scrub.last_cycle_s_ago !== null && s.scrub.last_cycle_s_ago !== undefined
      ? s.scrub.last_cycle_s_ago + "s ago" : "—");
    if (s.scrub.sheet_count !== undefined) setCell("sheets", String(s.scrub.sheet_count));
    setCell("scrub-cache", (s.sizes.scrub_cache_capped ? "≥ " : "") + fmtBytes(s.sizes.scrub_cache));
    setCell("mqtt", s.push.mqtt_connected ? "live" : "stale", s.push.mqtt_connected ? "ok" : "noise");
    setCell("frigate-online", s.push.frigate_online ? "online" : "offline",
      s.push.frigate_online ? "ok" : "noise");
    setCell("last-traffic", s.push.last_event_s_ago !== null && s.push.last_event_s_ago !== undefined
      ? s.push.last_event_s_ago + "s ago" : "—");
    setCell("sidecar-db", fmtBytes(s.sizes.sidecar_db));
    setCell("frigate-db", fmtBytes(s.sizes.frigate_db));

    // Push pipeline (wave 2A): relay retry/breaker + MQTT queue counters.
    var ps = s.push_stats || {};
    var pc = ps.counters || {};
    var pg = ps.gauges || {};
    function pcount(k) { return pc[k] !== undefined ? String(pc[k]) : "0"; }
    setCell("push-ok", pcount("relay.send.ok"));
    setCell("push-failed", pcount("relay.send.failed"));
    setCell("push-unregistered", pcount("relay.send.unregistered"));
    setCell("push-retries", pcount("relay.retry"));
    setCell("push-queue-depth", pg["mqtt.queue.depth"] !== undefined ? String(pg["mqtt.queue.depth"]) : "0");
    setCell("push-queue-hw", pg["mqtt.queue.high_water"] !== undefined ? String(pg["mqtt.queue.high_water"]) : "0");
    setCell("push-dropped", pcount("mqtt.dropped.overflow"));
    setCell("push-sweep-ended", pcount("pipeline.sweep.ended"));
    setCell("push-db-locked", pcount("db.locked.retry"));
    var breakerCell = document.querySelector('[data-k="push-breaker"]');
    if (breakerCell) {
      var breakerOpen = !!pg["relay.breaker.state"];
      var text = breakerOpen ? "open" : "closed";
      if (breakerOpen && pg["relay.breaker.open_until"]) {
        text += " until " + hhmm(pg["relay.breaker.open_until"] * 1000);
      }
      breakerCell.textContent = text;
      breakerCell.className = "stat-value cell-class " + (breakerOpen ? "noise" : "ok");
    }

    // Worst live-edge lag across cameras (status page tile).
    var worst = null;
    (s.scrub.cameras || []).forEach(function (c) {
      if (c.lag_s !== null && c.lag_s !== undefined && (worst === null || c.lag_s > worst)) {
        worst = c.lag_s;
      }
    });
    var worstCell = document.querySelector('[data-k="worst-lag"]');
    if (worstCell) worstCell.innerHTML = lagHtml(worst);

    // Hardware band (present only when Frigate/host stats resolved).
    var hw = s.hardware || {};
    Object.keys(hw.detectors || {}).forEach(function (name) {
      var cell = document.querySelector('[data-hw-det="' + name + '"]');
      if (!cell) return;
      var ms = hw.detectors[name];
      var cls = ms < 15 ? "ok" : ms < 30 ? "warn" : "noise";
      cell.innerHTML = '<span class="cell-class ' + cls + '">' + ms + " ms</span>";
    });
    function setHw(k, text) {
      var n = document.querySelector('[data-hw="' + k + '"]');
      if (n && text !== undefined && text !== null) n.textContent = text;
    }
    setHw("detection-fps", hw.detection_fps);
    if (hw.frigate_cpu !== undefined) setHw("frigate-cpu", hw.frigate_cpu + "% · " + hw.frigate_mem + "%");
    if (hw.gpu) setHw("gpu", hw.gpu.usage);
    var host = hw.host || {};
    if (host.load_1m !== undefined) setHw("load", host.load_1m + " · " + host.load_5m);
    if (host.mem_pct !== undefined) setHw("mem", host.mem_pct + "%");
    if (host.disk_pct !== undefined) {
      setHw("disk", host.disk_pct + "%");
      var diskCell = document.querySelector('[data-hw="disk"]');
      if (diskCell && diskCell.classList.contains("cell-class")) {
        diskCell.className = "cell-class " +
          (host.disk_pct > 90 ? "noise" : host.disk_pct > 75 ? "warn" : "ok");
        diskCell.setAttribute("data-hw", "disk");
      }
    }
    if (host.rss_bytes !== undefined) setHw("rss", fmtBytes(host.rss_bytes));
    Object.keys(hw.storage || {}).forEach(function (mount) {
      var wrap = document.querySelector('[data-hw-mount="' + CSS.escape(mount) + '"]');
      if (!wrap) return;
      var row = hw.storage[mount];
      var fill = wrap.querySelector(".hw-bar-fill");
      fill.style.width = row.pct + "%";
      fill.className = "hw-bar-fill " + (row.pct > 90 ? "noise" : row.pct > 75 ? "warn" : "ok");
      wrap.querySelector(".hw-mount-nums").textContent =
        fmtBytes(row.used_bytes) + " / " + fmtBytes(row.total_bytes);
    });

    // Per-camera lag badges (cameras page).
    (s.scrub.cameras || []).forEach(function (c) {
      var badge = document.querySelector('[data-cam-lag="' + c.camera + '"]');
      if (badge) badge.innerHTML = lagHtml(c.lag_s);
    });

    updateDigests(s);
    updateGroupFlags();
  }

  // --- Camera snapshots + recent Frigate events (30s cadence) -------------
  var MEDIA_INTERVAL_MS = 30000;
  var mediaTimer = null;

  function refreshSnaps() {
    var imgs = document.querySelectorAll("[data-cam-snap]");
    var t = Date.now();
    for (var i = 0; i < imgs.length; i++) {
      var cam = imgs[i].getAttribute("data-cam-snap");
      imgs[i].src = "/api/" + encodeURIComponent(cam) + "/latest.jpg?h=270&t=" + t;
    }
  }

  function relTime(epoch) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
    if (s < 90) return s + "s ago";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    if (s < 172800) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  async function refreshEvents() {
    var strip = document.getElementById("event-strip");
    if (!strip) return;
    var events;
    try {
      events = await SC.fetchJson("/api/events?limit=8");
    } catch (e) {
      return; // transient — keep whatever is showing
    }
    if (!Array.isArray(events)) return;
    strip.textContent = "";
    if (!events.length) {
      var none = document.createElement("span");
      none.className = "help";
      none.textContent = "no recent events";
      strip.appendChild(none);
      return;
    }
    events.forEach(function (ev) {
      var a = document.createElement("a");
      a.className = "event-card";
      a.href = "/live/" + encodeURIComponent(ev.camera);
      a.title = "Open " + ev.camera + " live stream";
      var img = document.createElement("img");
      img.src = "/api/events/" + encodeURIComponent(ev.id) + "/thumbnail.jpg";
      img.alt = ev.label + " on " + ev.camera;
      img.loading = "lazy";
      var meta = document.createElement("span");
      meta.className = "event-meta";
      var label = ev.sub_label || ev.label || "?";
      var live = !ev.end_time;
      meta.textContent = label + " · " + ev.camera + " · " +
        (live ? "live now" : relTime(ev.start_time));
      if (live) a.className += " live";
      a.appendChild(img);
      a.appendChild(meta);
      strip.appendChild(a);
    });
  }

  function mediaTick() { refreshSnaps(); refreshEvents(); }

  function start() {
    if (!timer) timer = setInterval(tick, INTERVAL_MS);
    if (!mediaTimer) mediaTimer = setInterval(mediaTick, MEDIA_INTERVAL_MS);
  }
  function stop() {
    clearInterval(timer); timer = null;
    clearInterval(mediaTimer); mediaTimer = null;
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop();
    else { tick(); mediaTick(); start(); }
  });
  initGroups();
  updateGroupFlags();
  start();
  tick();
  refreshEvents();
})();
