// Guide interactivity: live stat fill, walkthrough checklist persistence,
// and the index topic filter. Stats come from one JSON fetch so topic HTML
// stays a pure markdown render (guide.py).

(function () {
  // --- Live stats: fill every {{stat:...}} placeholder on the page.
  var statSpans = document.querySelectorAll('.guide-stat');
  if (statSpans.length) {
    SC.fetchJson('/guide/stats.json').then(function (data) {
      statSpans.forEach(function (span) {
        var v = data.stats[span.dataset.stat];
        if (v !== undefined) span.textContent = v;
      });
    }).catch(function () { /* placeholders keep their dash */ });
  }

  // --- Walkthrough checklists: progress per topic in localStorage.
  var page = document.querySelector('.guide-page');
  var slug = page ? page.dataset.slug : null;

  function storeKey(walkIndex) {
    return 'sidecar.guide.' + slug + '.' + walkIndex;
  }

  document.querySelectorAll('.walkthrough').forEach(function (walk) {
    if (!slug) return;
    var key = storeKey(walk.dataset.walkthrough);
    var boxes = walk.querySelectorAll('input[type="checkbox"]');
    var progress = walk.querySelector('.walkthrough-progress');

    var saved = [];
    try { saved = JSON.parse(localStorage.getItem(key) || '[]'); } catch (e) {}
    boxes.forEach(function (box) {
      if (saved.indexOf(Number(box.dataset.step)) !== -1) box.checked = true;
    });

    function update(persist) {
      var checked = [];
      boxes.forEach(function (box) {
        if (box.checked) checked.push(Number(box.dataset.step));
      });
      progress.textContent = checked.length + '/' + boxes.length + ' done';
      if (persist) {
        try { localStorage.setItem(key, JSON.stringify(checked)); } catch (e) {}
      }
    }
    update(false);

    boxes.forEach(function (box) {
      box.addEventListener('change', function () { update(true); });
    });
    walk.querySelector('.walkthrough-reset').addEventListener('click', function () {
      boxes.forEach(function (box) { box.checked = false; });
      update(true);
      SC.toast('walkthrough reset');
    });
  });

  // --- Full-text search: the index (title + section + body text) is fetched
  // lazily on first keystroke and filtered client-side. Result rows show a
  // snippet around the first body match.
  var searchIndex = null;
  var searchFetch = null;

  function loadIndex() {
    if (!searchFetch) {
      searchFetch = SC.fetchJson('/guide/search.json').then(function (data) {
        searchIndex = data.topics.map(function (t) {
          t.lowTitle = t.title.toLowerCase();
          t.lowText = t.text.toLowerCase();
          return t;
        });
      });
    }
    return searchFetch;
  }

  function snippet(topic, q) {
    var i = topic.lowText.indexOf(q);
    if (i === -1) return topic.section;
    var start = Math.max(0, i - 40);
    var s = topic.text.slice(start, i + q.length + 60);
    return (start ? '…' : '') + s + '…';
  }

  function renderResults(box, q) {
    var hits = searchIndex.filter(function (t) {
      return t.lowTitle.indexOf(q) !== -1 || t.lowText.indexOf(q) !== -1;
    }).slice(0, 10);
    box.textContent = '';
    if (!hits.length) {
      box.appendChild(SC.el('div', { class: 'guide-result-none', text: 'no matches' }, []));
    }
    hits.forEach(function (t) {
      box.appendChild(SC.el('a', { class: 'guide-result', href: '/guide/' + t.slug }, [
        SC.el('span', { class: 'guide-result-title', text: t.number + ' ' + t.title }, []),
        SC.el('span', { class: 'guide-result-snippet', text: snippet(t, q) }, []),
      ]));
    });
    box.hidden = false;
  }

  document.querySelectorAll('.guide-search').forEach(function (input) {
    var box = input.parentElement.querySelector('.guide-results');
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      if (q.length < 2) { box.hidden = true; return; }
      loadIndex().then(function () { renderResults(box, q); })
        .catch(function () { box.hidden = true; });
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { input.value = ''; box.hidden = true; }
    });
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('.guide-search-wrap')) {
      document.querySelectorAll('.guide-results').forEach(function (b) { b.hidden = true; });
    }
  });
})();
