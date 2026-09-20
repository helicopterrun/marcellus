// Toybox — 50 states map quiz.
//
// Mechanic: you're named a state, you click it on the map. Click the right
// path (or close enough for tiny states) → correct. Wrong → reveal the answer
// and move on. 50 rounds, score is how many you nailed. High scores persist
// server-side (arcade style) via /toybox/scores.
//
// Depends on toybox_states.js for STATE_PATHS, STATE_NAMES, QUIZ_STATES.

(function () {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";
  const GAME = "states50";
  const HIT_TOLERANCE = 16; // viewBox units of slack toward a tiny target's center

  const map = document.getElementById("tb-map");
  const targetEl = document.getElementById("tb-target");
  const scoreEl = document.getElementById("tb-score");
  const roundEl = document.getElementById("tb-round");
  const timeEl = document.getElementById("tb-time");
  const startBtn = document.getElementById("tb-start");
  const feedbackEl = document.getElementById("tb-feedback");
  const boardEl = document.getElementById("tb-board");

  const overlay = document.getElementById("tb-overlay");
  const finalScoreEl = document.getElementById("tb-final-score");
  const nameForm = document.getElementById("tb-name-form");
  const nameInput = document.getElementById("tb-name");
  const againBtn = document.getElementById("tb-again");

  // Built lazily since the template has no mount point for it: a submit
  // error used to be swallowed and the form hidden, so a flaky save looked
  // like it had gone through. This keeps the form up with the reason.
  const submitErrorEl = document.createElement("div");
  submitErrorEl.className = "help warn-note";
  submitErrorEl.hidden = true;
  nameForm.insertBefore(submitErrorEl, nameForm.querySelector("button"));
  function setSubmitError(msg) {
    submitErrorEl.textContent = msg || "";
    submitErrorEl.hidden = !msg;
  }

  const paths = {}; // code -> <path>
  const centroids = {}; // code -> {x, y}

  let order = []; // shuffled quiz codes
  let idx = 0;
  let score = 0;
  let locked = false; // ignore clicks between rounds
  let running = false;
  let startTs = 0;
  let timer = null;

  // Zoom/pan state. The SVG viewBox is the camera; we move/scale it on pinch,
  // drag, and wheel. clickPoint() uses getScreenCTM() so hit-testing keeps
  // working at any zoom without extra math.
  const BASE = { x: 0, y: 0, w: 959, h: 593 };
  const VIEW = { ...BASE };
  const MIN_W = BASE.w / 8; // max zoom-in (1/8th of the map width)
  const TAP_SLOP = 8; // px of finger travel before a tap becomes a pan
  const pointers = new Map(); // pointerId -> {x, y}
  let gesture = null;
  let suppressClick = false; // true after a pan/pinch so it isn't read as a tap

  // --- map build -----------------------------------------------------------

  function buildMap() {
    for (const code of Object.keys(STATE_PATHS)) {
      const p = document.createElementNS(SVGNS, "path");
      p.setAttribute("d", STATE_PATHS[code]);
      p.setAttribute("class", "tb-state");
      p.dataset.code = code;
      const name = STATE_NAMES[code] || code;
      const title = document.createElementNS(SVGNS, "title");
      title.textContent = name; // only useful as a hover crutch; harmless
      p.appendChild(title);
      // DC is rendered but never a quiz target and isn't clickable for scoring.
      if (code === "DC") p.classList.add("tb-dc");
      map.appendChild(p);
      paths[code] = p;
    }
    // Centroids from bbox centers (good enough for the tiny-state slack rule).
    for (const code of Object.keys(paths)) {
      const b = paths[code].getBBox();
      centroids[code] = { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    }
    map.addEventListener("click", onMapClick);
    setupZoom();
    applyView();
  }

  // Which state did this click land on? Hit-test geometrically against each
  // path's fill — the same test the browser uses to paint the map — in viewBox
  // space, so it's correct at any zoom. This is immune to the touch quirks that
  // make evt.target unreliable (pointer-capture retargeting, synthetic-click
  // coordinates, the <title> child). Falls back to the DOM target if a browser
  // lacks isPointInFill.
  function stateAt(evt) {
    const pt = clickPoint(evt); // SVGPoint in viewBox coordinates
    for (const code of Object.keys(paths)) {
      if (code === "DC") continue; // rendered but not a quiz target
      const p = paths[code];
      if (p.isPointInFill && p.isPointInFill(pt)) return p;
    }
    let s = evt.target && evt.target.closest ? evt.target.closest(".tb-state") : null;
    if (!s) {
      const hit = document.elementFromPoint(evt.clientX, evt.clientY);
      s = hit && hit.closest ? hit.closest(".tb-state") : null;
    }
    return s;
  }

  // Translate a DOM click into viewBox coordinates.
  function clickPoint(evt) {
    const pt = map.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    return pt.matrixTransform(map.getScreenCTM().inverse());
  }

  // --- zoom & pan ----------------------------------------------------------

  function applyView() {
    map.setAttribute("viewBox", `${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`);
  }

  function resetZoom() {
    Object.assign(VIEW, BASE);
    applyView();
  }

  // Keep aspect locked to the base map and the view inside the map bounds.
  function clampView() {
    VIEW.w = Math.min(BASE.w, Math.max(MIN_W, VIEW.w));
    VIEW.h = VIEW.w * (BASE.h / BASE.w);
    VIEW.x = Math.min(BASE.w - VIEW.w, Math.max(0, VIEW.x));
    VIEW.y = Math.min(BASE.h - VIEW.h, Math.max(0, VIEW.y));
  }

  // Zoom by `factor` about a screen point, keeping that point pinned.
  function zoomAt(clientX, clientY, factor) {
    const rect = map.getBoundingClientRect();
    const fracX = (clientX - rect.left) / rect.width;
    const fracY = (clientY - rect.top) / rect.height;
    const svgX = VIEW.x + fracX * VIEW.w;
    const svgY = VIEW.y + fracY * VIEW.h;
    VIEW.w *= factor;
    clampView(); // settles w (and h) within limits
    VIEW.x = svgX - fracX * VIEW.w;
    VIEW.y = svgY - fracY * VIEW.h;
    clampView();
    applyView();
  }

  function setupZoom() {
    map.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 0.85 : 1 / 0.85);
      },
      { passive: false }
    );

    // Capture only once a real gesture begins. Capturing on a plain tap would
    // retarget the resulting click to the <svg> root and break hit detection.
    const capture = (id) => {
      if (map.setPointerCapture) {
        try {
          map.setPointerCapture(id);
        } catch (_) {
          /* pointer already gone */
        }
      }
    };

    map.addEventListener("pointerdown", (e) => {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 1) {
        suppressClick = false; // fresh gesture; clear any stuck flag
        gesture = { mode: "maybe-tap", startX: e.clientX, startY: e.clientY, vb: { ...VIEW } };
      } else if (pointers.size === 2) {
        gesture = startPinch();
        suppressClick = true;
        for (const id of pointers.keys()) capture(id);
      }
    });

    map.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId) || !gesture) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const rect = map.getBoundingClientRect();
      if (gesture.mode === "pinch" && pointers.size >= 2) {
        const pts = [...pointers.values()];
        const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
        VIEW.w = gesture.vb.w * (gesture.startDist / dist);
        clampView();
        const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
        const fracX = (mid.x - rect.left) / rect.width;
        const fracY = (mid.y - rect.top) / rect.height;
        VIEW.x = gesture.svgMid.x - fracX * VIEW.w;
        VIEW.y = gesture.svgMid.y - fracY * VIEW.h;
        clampView();
        applyView();
      } else if (gesture.mode === "maybe-tap" || gesture.mode === "pan") {
        const dx = e.clientX - gesture.startX;
        const dy = e.clientY - gesture.startY;
        if (gesture.mode === "maybe-tap" && Math.hypot(dx, dy) > TAP_SLOP) {
          gesture.mode = "pan";
          suppressClick = true;
          capture(e.pointerId);
        }
        if (gesture.mode === "pan") {
          VIEW.x = gesture.vb.x - (dx / rect.width) * gesture.vb.w;
          VIEW.y = gesture.vb.y - (dy / rect.height) * gesture.vb.h;
          clampView();
          applyView();
        }
      }
    });

    const endPointer = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size === 0) {
        gesture = null;
      } else if (pointers.size === 1) {
        // Dropped from pinch to one finger: continue as a pan.
        const p = [...pointers.values()][0];
        gesture = { mode: "pan", startX: p.x, startY: p.y, vb: { ...VIEW } };
        suppressClick = true;
      }
    };
    map.addEventListener("pointerup", endPointer);
    map.addEventListener("pointercancel", endPointer);
    map.addEventListener("dblclick", (e) => {
      e.preventDefault();
      resetZoom();
    });
  }

  function startPinch() {
    const pts = [...pointers.values()];
    const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
    const rect = map.getBoundingClientRect();
    const mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
    const fracX = (mid.x - rect.left) / rect.width;
    const fracY = (mid.y - rect.top) / rect.height;
    return {
      mode: "pinch",
      startDist: dist,
      vb: { ...VIEW },
      svgMid: { x: VIEW.x + fracX * VIEW.w, y: VIEW.y + fracY * VIEW.h },
    };
  }

  // --- game flow -----------------------------------------------------------

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function startGame() {
    order = shuffle(QUIZ_STATES);
    idx = 0;
    score = 0;
    running = true;
    locked = false;
    scoreEl.textContent = "0";
    overlay.hidden = true;
    startBtn.textContent = "RESTART";
    for (const code of Object.keys(paths)) {
      paths[code].classList.remove("ok", "miss", "reveal");
    }
    resetZoom();
    startTs = Date.now();
    if (timer) clearInterval(timer);
    timer = setInterval(tick, 250);
    nextRound();
  }

  function tick() {
    const s = Math.floor((Date.now() - startTs) / 1000);
    const m = Math.floor(s / 60);
    timeEl.textContent = m + ":" + String(s % 60).padStart(2, "0");
  }

  function nextRound() {
    if (idx >= order.length) return endGame();
    locked = false;
    roundEl.textContent = idx + 1 + "/" + order.length;
    targetEl.textContent = STATE_NAMES[order[idx]];
  }

  function currentTarget() {
    return order[idx];
  }

  function onMapClick(evt) {
    if (suppressClick) {
      suppressClick = false; // this "click" was the tail of a pan/pinch
      return;
    }
    if (!running || locked) return;
    const target = currentTarget();
    const clicked = stateAt(evt);
    const clickedCode = clicked ? clicked.dataset.code : null;

    let correct = clickedCode === target;
    if (!correct) {
      // Tiny-state mercy: count it if the click landed near the target's center.
      const pt = clickPoint(evt);
      const c = centroids[target];
      const dist = Math.hypot(pt.x - c.x, pt.y - c.y);
      if (dist <= HIT_TOLERANCE) correct = true;
    }

    locked = true;
    if (correct) {
      score++;
      scoreEl.textContent = String(score);
      paths[target].classList.add("ok");
      flash("✓ " + STATE_NAMES[target], "ok");
    } else {
      if (clickedCode && clickedCode !== "DC") paths[clickedCode].classList.add("miss");
      paths[target].classList.add("reveal");
      flash("✗ that was " + STATE_NAMES[target], "miss");
    }
    setTimeout(() => {
      paths[target].classList.remove("ok", "reveal");
      if (clickedCode && paths[clickedCode]) paths[clickedCode].classList.remove("miss");
      idx++;
      nextRound();
    }, 850);
  }

  function flash(msg, kind) {
    feedbackEl.textContent = msg;
    feedbackEl.className = "tb-feedback show " + kind;
    setTimeout(() => {
      feedbackEl.className = "tb-feedback";
    }, 800);
  }

  // --- game over + high scores --------------------------------------------

  function endGame() {
    running = false;
    if (timer) clearInterval(timer);
    targetEl.textContent = "Press START";
    roundEl.textContent = "50/50";
    finalScoreEl.textContent = String(score);
    overlay.hidden = false;
    document.getElementById("tb-result-title").textContent =
      score === 50 ? "PERFECT!" : "GAME OVER";

    qualifies(score).then((isHigh) => {
      nameForm.hidden = !isHigh;
      if (isHigh) {
        setTimeout(() => nameInput.focus(), 50);
      }
    });
  }

  async function qualifies(s) {
    try {
      const r = await fetch("/toybox/scores?game=" + GAME);
      const data = await r.json();
      const board = data.scores || [];
      if (board.length < 10) return true;
      const lowest = board[board.length - 1].score;
      return s > lowest;
    } catch (e) {
      return true; // if the board can't load, still let them try to save
    }
  }

  function renderBoard(scores) {
    boardEl.innerHTML = "";
    if (!scores.length) {
      const li = document.createElement("li");
      li.style.color = "var(--muted-2)";
      li.textContent = "No scores yet — be the first!";
      boardEl.appendChild(li);
      return;
    }
    for (const s of scores) {
      const li = document.createElement("li");
      const rank = document.createElement("span");
      rank.className = "tb-rank";
      rank.textContent = String(s.rank).padStart(2, "0");
      const name = document.createElement("span");
      name.className = "tb-name";
      name.textContent = s.name;
      const pts = document.createElement("span");
      pts.className = "tb-pts";
      pts.textContent = String(s.score);
      li.append(rank, name, pts);
      boardEl.appendChild(li);
    }
  }

  async function submitScore(evt) {
    evt.preventDefault();
    const name = nameInput.value.trim();
    if (!name) {
      nameInput.focus();
      return;
    }
    setSubmitError("");
    try {
      const r = await fetch("/toybox/scores", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ game: GAME, name: name, score: score }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.scores) renderBoard(data.scores);
      nameForm.hidden = true;
      nameInput.value = "";
    } catch (e) {
      // Keep the form up with the score still filled in -- a swallowed
      // failure used to hide the form and lose the score silently.
      setSubmitError("Couldn't save your score (" + e.message + ") — try again?");
    }
  }

  // --- wire up -------------------------------------------------------------

  buildMap();
  if (!boardEl.children.length) renderBoard([]); // server-rendered board was empty
  startBtn.addEventListener("click", startGame);
  againBtn.addEventListener("click", startGame);
  nameForm.addEventListener("submit", submitScore);
})();
