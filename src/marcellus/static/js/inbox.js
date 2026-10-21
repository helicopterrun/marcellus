// Review inbox: one key (or tap) per card records the verdict and queues the
// Frigate+ upload; the card flips to a result line with an Undo until the
// outbox sends it. Focus moves to the next undecided card on its own.
(function () {
  "use strict";

  var main = document.querySelector("main.inbox");
  var grid = document.getElementById("ib-grid");
  if (!main || !grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll(".ib-card"));
  var budget = parseInt(main.dataset.budget, 10) || 15;
  var done = parseInt(main.dataset.done, 10) || 0;
  var doneNode = document.getElementById("ib-done");
  var barFill = document.getElementById("ib-bar-fill");
  var queuedNode = document.getElementById("ib-ob-queued");
  var finished = document.getElementById("ib-finished");
  var focused = 0;
  var lastDecided = null;
  // On a phone, an animated scroll still settling when the next tap lands
  // can swallow that tap (iOS Safari), so touch scrolls jump instead.
  var scrollBehavior = window.matchMedia && window.matchMedia("(pointer: coarse)").matches
    ? "auto" : "smooth";

  var NAMES = {
    event: { tp: "Real", fp: "False alarm", wrong: "Wrong label", skip: "Skipped" },
    visit: { tp: "Animal", fp: "Not an animal", wrong: "Animal", skip: "Skipped" },
  };

  function setDone(n) {
    done = Math.max(0, n);
    doneNode.textContent = done;
    barFill.style.width = Math.min(100, Math.round((100 * done) / budget)) + "%";
  }

  function setOutbox(ob) {
    if (ob && queuedNode) queuedNode.textContent = ob.queued + " queued";
  }

  function undecided() {
    return cards.filter(function (c) { return !c.classList.contains("decided"); });
  }

  function focusCard(i) {
    if (!cards.length) return;
    focused = Math.max(0, Math.min(cards.length - 1, i));
    var c = cards[focused];
    cards.forEach(function (x) { x.classList.toggle("focused", x === c); });
    c.focus({ preventScroll: true });
    c.scrollIntoView({ block: "center", behavior: scrollBehavior });
  }

  function focusNextUndecided(from) {
    for (var k = 1; k <= cards.length; k++) {
      var i = (from + k) % cards.length;
      if (!cards[i].classList.contains("decided")) { focusCard(i); return; }
    }
    if (finished) {
      finished.hidden = false;
      finished.scrollIntoView({ block: "center", behavior: scrollBehavior });
    }
  }

  // Countdown on a queued upload; stops at "uploading" (the outbox picks it
  // up on its next tick; the coverage page shows the outcome).
  function countdown(card, label, secs) {
    var text = card.querySelector(".ib-result-text");
    clearInterval(card._timer);
    function tick() {
      if (secs > 0) {
        text.textContent = label + " · Frigate+ in " + secs + "s";
        secs -= 1;
      } else {
        text.textContent = label + " · uploading to Frigate+";
        clearInterval(card._timer);
        card.querySelector("[data-undo]").hidden = true;
      }
    }
    tick();
    card._timer = setInterval(tick, 1000);
  }

  function showResult(card, verdict, plus) {
    var label = NAMES[card.dataset.kind][verdict];
    var text = card.querySelector(".ib-result-text");
    card.querySelector("[data-undo]").hidden = false;
    if (plus.status === "queued") {
      countdown(card, label + (plus.frames ? " (" + plus.frames + " frames)" : ""), plus.send_in_s);
    } else if (plus.status === "not_sent" || plus.status === "off") {
      text.textContent = label + " · not sent: " + plus.message;
    } else {
      text.textContent = label;
    }
  }

  async function decide(card, verdict, correct) {
    if (card.classList.contains("decided") || card.classList.contains("busy")) return;
    card.classList.add("busy");
    try {
      var res = await SC.fetchJson("/inbox/verdict", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: card.dataset.key, verdict: verdict, correct_label: correct || null }),
      });
      card.classList.add("decided", "v-" + verdict);
      card.dataset.verdict = verdict;
      card.querySelector(".ib-actions").hidden = true;
      var wrong = card.querySelector(".ib-wrong");
      if (wrong) wrong.hidden = true;
      card.querySelector(".ib-result").hidden = false;
      showResult(card, verdict, res.plus || {});
      setOutbox(res.outbox);
      setDone(done + 1);
      lastDecided = card;
      focusNextUndecided(cards.indexOf(card));
    } catch (e) {
      SC.toast("Save failed: " + (e.message || e), true);
    } finally {
      card.classList.remove("busy");
    }
  }

  async function undo(card) {
    if (!card || !card.classList.contains("decided")) return;
    try {
      var res = await SC.fetchJson("/inbox/undo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: card.dataset.key }),
      });
      setOutbox(res.outbox);
      if (!res.ok) { SC.toast(res.message || "Can't undo", true); return; }
      clearInterval(card._timer);
      card.classList.remove("decided", "v-" + card.dataset.verdict);
      delete card.dataset.verdict;
      card.querySelector(".ib-actions").hidden = false;
      card.querySelector(".ib-result").hidden = true;
      if (finished) finished.hidden = true;
      setDone(done - 1);
      focusCard(cards.indexOf(card));
    } catch (e) {
      SC.toast("Undo failed: " + (e.message || e), true);
    }
  }

  function openWrong(card) {
    var form = card.querySelector(".ib-wrong");
    if (!form) { decide(card, "wrong"); return; }
    card.querySelector(".ib-actions").hidden = true;
    form.hidden = false;
    form.querySelector("select").focus();
  }

  function closeWrong(card) {
    var form = card.querySelector(".ib-wrong");
    if (!form || form.hidden) return false;
    form.hidden = true;
    card.querySelector(".ib-actions").hidden = false;
    card.focus({ preventScroll: true });
    return true;
  }

  function toggleClip(card) {
    var url = card.dataset.clip;
    if (!url) return;
    var frame = card.querySelector(".ib-frame");
    var video = frame.querySelector("video");
    if (video) {
      video.remove();
      frame.classList.remove("playing");
      return;
    }
    video = document.createElement("video");
    video.src = url;
    video.controls = true;
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    video.className = "ib-video";
    frame.appendChild(video);
    frame.classList.add("playing");
  }

  function showFrame(card, btn) {
    var img = card.querySelector(".ib-img");
    img.src = btn.dataset.frame;
    var b = btn.dataset.box.split(",");
    var rect = card.querySelector(".ib-box");
    if (rect && b.length === 4) {
      rect.setAttribute("x", b[0]); rect.setAttribute("y", b[1]);
      rect.setAttribute("width", b[2]); rect.setAttribute("height", b[3]);
    }
    card.querySelectorAll(".ib-thumb").forEach(function (t) { t.classList.toggle("on", t === btn); });
  }

  cards.forEach(function (card, i) {
    card.addEventListener("focus", function () {
      focused = i;
      cards.forEach(function (x) { x.classList.toggle("focused", x === card); });
    });
    card.querySelectorAll("[data-v]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.v === "wrong") openWrong(card);
        else decide(card, b.dataset.v);
      });
    });
    var form = card.querySelector(".ib-wrong");
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        decide(card, "wrong", form.querySelector("select").value);
      });
      form.querySelector("[data-cancel]").addEventListener("click", function () { closeWrong(card); });
    }
    card.querySelector("[data-undo]").addEventListener("click", function () { undo(card); });
    var play = card.querySelector(".ib-play");
    if (play) play.addEventListener("click", function () { toggleClip(card); });
    card.querySelectorAll(".ib-thumb").forEach(function (t) {
      t.addEventListener("click", function () { showFrame(card, t); });
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var tag = e.target.tagName;
    if (e.key === "Escape") {
      if (cards[focused] && closeWrong(cards[focused])) e.preventDefault();
      return;
    }
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    var card = cards[focused];
    if (!card) return;
    var k = e.key;
    if (k === "j" || k === "ArrowDown" || k === "ArrowRight") { focusCard(focused + 1); }
    else if (k === "k" || k === "ArrowUp" || k === "ArrowLeft") { focusCard(focused - 1); }
    else if (k === "t") { decide(card, "tp"); }
    else if (k === "f") { decide(card, "fp"); }
    else if (k === "w") { openWrong(card); }
    else if (k === "s") { decide(card, "skip"); }
    else if (k === "u") { undo(card.classList.contains("decided") ? card : lastDecided); }
    else if (k === " ") { toggleClip(card); }
    else return;
    e.preventDefault();
  });

  if (undecided().length) focusCard(cards.indexOf(undecided()[0]));
})();
