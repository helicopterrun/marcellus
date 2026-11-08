// Accuracy: blind face-pair labelling. The server sends only the pair's face
// crops (no distance, ids, names, camera or times), so answers are a fair
// test of the model. Results arrive only once every drawn pair is labelled.
// All inserted text goes through SC.el's textContent, never innerHTML.

(function () {
  var main = document.querySelector('main.fa');
  var root = document.getElementById('fa-root');
  if (!main || !root) return;
  var el = SC.el;
  var matchThreshold = main.dataset.matchThreshold;
  var clusterThreshold = main.dataset.clusterThreshold;

  var state = null;
  var history = [];   // pair_ids labelled in this page session, oldest first
  var busy = false;
  var error = null;
  var emptyNote = null;    // inline message when a draw found no qualifying pairs
  var SINCE_KEY = 'fa-since';
  var sinceValue = '';     // yyyy-mm-dd from the date input; '' = all history
  try { sinceValue = localStorage.getItem(SINCE_KEY) || ''; } catch (e) { /* storage blocked */ }
  var imagesPending = 0;   // answers wait until both main faces have been seen

  function post(path, body) {
    return SC.fetchJson(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // Local-midnight epoch seconds for a yyyy-mm-dd string, or null if empty/bad.
  function sinceEpoch(v) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || '');
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3]).getTime() / 1000;
  }

  function sinceControl() {
    var input = el('input', { type: 'date', id: 'fa-since', class: 'fa-since-input' });
    input.value = sinceValue;
    input.disabled = busy;
    input.addEventListener('change', function () {
      sinceValue = input.value;
      try { localStorage.setItem(SINCE_KEY, sinceValue); } catch (e) { /* storage blocked */ }
    });
    return el('div', { class: 'fa-since' }, [
      el('label', { for: 'fa-since', text: 'Only sightings since' }),
      input,
    ]);
  }

  function pct(x) { return Math.round(x * 100) + '%'; }

  function rateLine(label, r) {
    if (!r || !r.n) return label + ': No data';
    return label + ': ' + r.hits + ' of ' + r.n + ' (' + pct(r.rate) +
      '), likely between ' + pct(r.low) + ' and ' + pct(r.high);
  }

  function side(data) {
    var crops = data.crops || [];
    var box = el('div', { class: 'fa-side' });
    if (crops[0]) {
      var main = el('img', { class: 'fa-main', alt: 'Face' });
      imagesPending += 1;
      var seen = function () { imagesPending -= 1; };
      main.addEventListener('load', seen, { once: true });
      main.addEventListener('error', seen, { once: true });
      main.src = crops[0];
      box.appendChild(main);
    }
    if (crops.length > 1) {
      var row = el('div', { class: 'fa-thumbs' });
      crops.slice(1).forEach(function (u) {
        row.appendChild(el('img', { class: 'fa-thumb', src: u, alt: 'Another view of this face' }));
      });
      box.appendChild(row);
    }
    return box;
  }

  function button(text, key, cls, handler, disabled) {
    var b = el('button', { type: 'button', class: 'fa-btn ' + cls });
    b.appendChild(document.createTextNode(text + ' '));
    if (key) b.appendChild(el('kbd', { text: key }));
    if (disabled) b.disabled = true;
    b.addEventListener('click', handler);
    return b;
  }

  function renderIntro() {
    root.appendChild(el('p', {
      class: 'pp-lede',
      text: "You'll see two faces at a time. Say whether they're the same person. " +
        "The app doesn't show what it thinks, so your answers are a fair test. " +
        'About 50 pairs takes a few minutes.',
    }));
    var start = el('button', { type: 'button', class: 'btn btn-primary', text: 'Start' });
    start.disabled = busy;
    start.addEventListener('click', function () { draw(50); });
    root.appendChild(sinceControl());
    root.appendChild(start);
    if (emptyNote) root.appendChild(el('p', { class: 'fa-note', role: 'status', text: emptyNote }));
  }

  function renderPair() {
    var pair = state.pair;
    imagesPending = 0;
    root.appendChild(el('p', { class: 'fa-progress', text: (state.done + 1) + ' of ' + state.total }));
    var faces = el('div', { class: 'fa-faces' });
    faces.appendChild(side(pair.a));
    faces.appendChild(side(pair.b));
    root.appendChild(faces);
    var bar = el('div', { class: 'fa-actions' });
    bar.appendChild(button('Same person', 'S', 'fa-same', function () { label('same'); }, busy));
    bar.appendChild(button('Different people', 'D', 'fa-diff', function () { label('different'); }, busy));
    bar.appendChild(button("Can't tell", 'U', 'fa-unsure', function () { label('unsure'); }, busy));
    bar.appendChild(button('Back', 'Z', 'fa-back', back, busy || !history.length));
    root.appendChild(bar);
  }

  function table(res) {
    var t = el('table', { class: 'fa-table' });
    var head = el('tr');
    ['Distance', 'Same', 'Different', "Can't tell"].forEach(function (h) {
      head.appendChild(el('th', { text: h }));
    });
    t.appendChild(el('thead', {}, [head]));
    var body = el('tbody');
    res.bands.forEach(function (b) {
      body.appendChild(el('tr', {}, [
        el('td', { text: b.lo.toFixed(2) + ' to ' + b.hi.toFixed(2) }),
        el('td', { text: String(b.same) }),
        el('td', { text: String(b.different) }),
        el('td', { text: String(b.unsure) }),
      ]));
    });
    t.appendChild(body);
    return t;
  }

  function link(id, text) {
    return el('a', { href: '/enrich/clusters?cluster=' + encodeURIComponent(id), text: text });
  }

  function renderResults() {
    var res = state.results;
    root.appendChild(el('h2', { class: 'fa-h', text: 'Results' }));
    var list = el('ul', { class: 'fa-summary' });
    list.appendChild(el('li', { text: rateLine('IDs that are really a duplicate of another ID', res.split) }));
    list.appendChild(el('li', { text: rateLine('Sightings on the wrong ID', res.join) }));
    list.appendChild(el('li', { text: "Can't tell: " + res.unsure }));
    root.appendChild(list);

    root.appendChild(table(res));
    root.appendChild(el('p', {
      class: 'pp-lede',
      text: 'Lower distance means the model thinks they look more alike. A new sighting joins an ' +
        'unnamed ID at ' + clusterThreshold + ' or less and a named one at ' + matchThreshold + ' or less.',
    }));

    root.appendChild(el('h2', { class: 'fa-h', text: 'Marked the same person' }));
    if (!res.duplicates.length) {
      root.appendChild(el('p', { class: 'pp-lede', text: 'None.' }));
    } else {
      var dl = el('ul', { class: 'fa-links' });
      res.duplicates.forEach(function (d) {
        dl.appendChild(el('li', {}, [
          link(d.a, d.a_handle), document.createTextNode(' and '), link(d.b, d.b_handle),
          document.createTextNode(' (distance ' + d.distance.toFixed(2) + ')'),
        ]));
      });
      root.appendChild(dl);
    }

    root.appendChild(el('h2', { class: 'fa-h', text: 'Marked as the wrong ID' }));
    if (!res.wrong_joins.length) {
      root.appendChild(el('p', { class: 'pp-lede', text: 'None.' }));
    } else {
      var wl = el('ul', { class: 'fa-links' });
      res.wrong_joins.forEach(function (w) {
        var faces = el('span', { class: 'fa-wrong' });
        (w.event_ids || []).forEach(function (id) {
          faces.appendChild(el('img', {
            class: 'fa-thumb', src: '/enrich/face/' + encodeURIComponent(id) + '.jpg', alt: 'Face',
          }));
        });
        wl.appendChild(el('li', {}, [
          faces, link(w.cluster, w.handle),
          document.createTextNode(' (distance ' + w.distance.toFixed(2) + ')'),
        ]));
      });
      root.appendChild(wl);
    }

    var more = el('button', { type: 'button', class: 'btn btn-primary', text: 'Label 50 more' });
    more.disabled = busy;
    more.addEventListener('click', function () { draw(50); });
    root.appendChild(sinceControl());
    root.appendChild(more);
    if (emptyNote) root.appendChild(el('p', { class: 'fa-note', role: 'status', text: emptyNote }));
  }

  function render() {
    root.replaceChildren();
    if (error) root.appendChild(el('p', { class: 'fa-error', role: 'alert', text: error }));
    if (!state) return;
    if (state.pair) renderPair();
    else if (state.results) renderResults();
    else renderIntro();
  }

  async function run(fn) {
    if (busy) return;
    busy = true;
    error = null;
    render();
    try {
      await fn();
    } catch (e) {
      error = 'That did not work: ' + e.message;
    }
    busy = false;
    render();
  }

  function draw(count) {
    return run(async function () {
      var since = sinceEpoch(sinceValue);
      var body = { count: count };
      if (since !== null) body.since = since;
      state = await post('/enrich/accuracy/draw', body);
      history = [];
      emptyNote = null;
      if (state.drawn === 0 && since !== null) {
        emptyNote = 'No new pairs from sightings since ' + sinceValue + '.';
      }
    });
  }

  function label(value) {
    if (!state || !state.pair || imagesPending > 0) return;
    var id = state.pair.pair_id;
    return run(async function () {
      state = await post('/enrich/accuracy/pairs/' + id + '/label', { label: value });
      history.push(id);
    });
  }

  function back() {
    if (!history.length || !state || !state.pair) return;
    var id = history[history.length - 1];
    return run(async function () {
      state = await post('/enrich/accuracy/pairs/' + id + '/label', { label: null });
      history.pop();
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey || e.repeat || busy || !state || !state.pair) return;
    if (e.target.closest && e.target.closest('input, textarea, select')) return;
    var k = e.key.toLowerCase();
    if (k === 's') label('same');
    else if (k === 'd') label('different');
    else if (k === 'u') label('unsure');
    else if (k === 'z' || e.key === 'ArrowLeft') back();
    else return;
    e.preventDefault();
  });

  run(async function () { state = await SC.fetchJson('/enrich/accuracy/state.json'); });
})();
