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

  function showStaleBanner() {
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
    setCell("frigate", s.frigate.reachable ? s.frigate.version : "unreachable",
      s.frigate.reachable ? "ok" : "noise");
    setCell("scrub", s.scrub.enabled ? "on" : "off", s.scrub.enabled ? "ok" : "muted");
    setCell("push", s.push.enabled ? "on" : "off", s.push.enabled ? "ok" : "muted");
    setCell("devices", String(s.push.device_count));
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
    if (host.disk_pct !== undefined) setHw("disk", host.disk_pct + "%");
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
  start();
  tick();
  refreshEvents();
})();
