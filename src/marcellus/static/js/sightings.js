// Encounters: external-source sightings gallery (filter chips, hover
// playback, in-page player). No-op when the section is absent.
(function () {
  var ws = document.querySelector("[data-ws]");
  if (!ws) return;
  var KEY = "marcellus_sightings_view";
  var FILTERS = ["wildlife", "people", "empty", "all"];
  var chips = Array.prototype.slice.call(ws.querySelectorAll(".ws-chip"));
  var days = Array.prototype.slice.call(ws.querySelectorAll(".ws-day"));

  function visible(day) {
    var f = ws.getAttribute("data-ws-filter");
    return Array.prototype.slice.call((day || ws).querySelectorAll(".ws-shot")).filter(function (c) {
      return f === "all" || c.getAttribute("data-class") === f;
    });
  }

  function apply(f) {
    ws.setAttribute("data-ws-filter", f);
    chips.forEach(function (c) { c.classList.toggle("on", c.getAttribute("data-filter") === f); });
    days.forEach(function (day) {
      var more = day.querySelector(".ws-more");
      if (!more) return;
      var total = parseInt(more.getAttribute("data-day-total"), 10) || 0;
      var shown = visible(day).length;
      var hid = total - shown;
      day.hidden = total === 0;
      if (hid > 0 && f !== "all") {
        more.textContent = "";
        var a = document.createElement("a");
        a.href = "#";
        a.className = "page-link";
        a.textContent = shown === 0 ? "Nothing in this view — show all " + total
                                    : hid + " more — show all " + total;
        a.addEventListener("click", function (e) { e.preventDefault(); select("all"); });
        more.appendChild(a);
        more.hidden = false;
      } else {
        more.hidden = true;
        more.textContent = "";
      }
    });
  }

  function select(f) {
    if (FILTERS.indexOf(f) < 0) f = "wildlife";
    apply(f);
    try { localStorage.setItem(KEY, f); } catch (e) { /* storage unavailable */ }
  }

  chips.forEach(function (c) {
    c.addEventListener("click", function () { select(c.getAttribute("data-filter")); });
  });
  var saved = "wildlife";
  try { saved = localStorage.getItem(KEY) || "wildlife"; } catch (e) { /* ignore */ }
  apply(FILTERS.indexOf(saved) >= 0 ? saved : "wildlife");

  // Hover playback (pointer devices only).
  var canHover = !(window.matchMedia && window.matchMedia("(hover: none)").matches);
  var shots = Array.prototype.slice.call(ws.querySelectorAll(".ws-shot"));
  if (canHover) {
    shots.forEach(function (s) {
      var v = s.querySelector("video");
      if (!v) return;
      s.addEventListener("mouseenter", function () {
        v.currentTime = 0;
        var p = v.play();
        if (p && p.catch) p.catch(function () {});
      });
      s.addEventListener("mouseleave", function () { v.pause(); });
    });
  }

  // Lightbox.
  var lb = ws.querySelector(".ws-lightbox");
  if (!lb) return;
  var lbVideo = lb.querySelector(".ws-lb-video");
  var lbImg = lb.querySelector(".ws-lb-img");
  var lbTitle = lb.querySelector(".ws-lb-title");
  var lbStreams = lb.querySelector(".ws-lb-streams");
  var lbOpen = lb.querySelector(".ws-lb-open");
  var cur = null;
  var SUBJ = { animal: "Animal", person: "Person", vehicle: "Vehicle", none: "Empty", unknown: "New" };

  function itemOf(card) {
    try { return JSON.parse(card.getAttribute("data-item") || "null"); } catch (e) { return null; }
  }

  function setStream(item, idx, keepTime) {
    var st = (item.streams || [])[idx];
    var t = keepTime ? lbVideo.currentTime : 0;
    Array.prototype.forEach.call(lbStreams.children, function (b, i) { b.classList.toggle("on", i === idx); });
    if (!st) {
      lbVideo.hidden = true;
      lbVideo.removeAttribute("src");
      lbVideo.load();
      lbImg.hidden = !item.poster_url;
      if (item.poster_url) lbImg.src = item.poster_url;
      lbOpen.hidden = true;
      return;
    }
    lbImg.hidden = true;
    lbImg.removeAttribute("src");
    lbVideo.hidden = false;
    lbVideo.src = st.clip_url;
    lbVideo.poster = item.poster_url || "";
    lbOpen.href = st.clip_url;
    lbOpen.hidden = false;
    lbVideo.muted = false;
    if (keepTime) {
      lbVideo.addEventListener("loadedmetadata", function seek() {
        lbVideo.removeEventListener("loadedmetadata", seek);
        try { lbVideo.currentTime = t; } catch (e) { /* ignore */ }
      });
    }
    var p = lbVideo.play();
    if (p && p.catch) p.catch(function () {});
  }

  function show(card) {
    var item = itemOf(card);
    if (!item) return;
    cur = card;
    var bits = [item.day_label, item.time_label, SUBJ[item.subject] || item.subject];
    if (item.conf_pct !== null && item.conf_pct !== undefined) bits.push(item.conf_pct + "%");
    lbTitle.textContent = bits.filter(Boolean).join(" · ");
    lbStreams.textContent = "";
    var streams = item.streams || [];
    if (streams.length > 1) {
      streams.forEach(function (st, i) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "ws-chip";
        b.textContent = st.label;
        b.addEventListener("click", function () { setStream(item, i, true); });
        lbStreams.appendChild(b);
      });
    }
    lb.hidden = false;
    setStream(item, 0, false);
  }

  function close() {
    lb.hidden = true;
    lbVideo.pause();
    lbVideo.removeAttribute("src");
    lbVideo.load();
    lbImg.removeAttribute("src");
    if (cur) cur.focus();
    cur = null;
  }

  function step(d) {
    var list = visible();
    var i = list.indexOf(cur);
    if (i < 0 || !list.length) return;
    show(list[(i + d + list.length) % list.length]);
  }

  shots.forEach(function (s) {
    s.addEventListener("click", function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      e.preventDefault();
      show(s);
    });
  });
  lb.addEventListener("click", function (e) {
    if (e.target === lb) close();
  });
  lb.querySelector('[data-lb="close"]').addEventListener("click", close);
  lb.querySelector('[data-lb="prev"]').addEventListener("click", function () { step(-1); });
  lb.querySelector('[data-lb="next"]').addEventListener("click", function () { step(1); });
  document.addEventListener("keydown", function (e) {
    if (lb.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
  });
})();
