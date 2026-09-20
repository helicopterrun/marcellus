(function () {
  var forms = document.querySelectorAll('.search-form');
  if (!forms.length) return;

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function render(drop, events, q) {
    if (!events.length) {
      drop.innerHTML = '<div class="search-empty">'
        + (q ? 'No results for "' + escHtml(q) + '"' : 'No recent events')
        + '</div>';
      drop.hidden = false;
      return;
    }
    var html = q ? '' : '<div class="search-heading">Recent</div>';
    events.forEach(function (ev) {
      var t = new Date(ev.start_time * 1000);
      var when = t.toLocaleDateString(undefined, {month: 'short', day: 'numeric'})
        + ' ' + t.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
      var thumb = ev.has_snapshot
        ? '<img src="/api/events/' + encodeURIComponent(ev.id)
          + '/snapshot.jpg?quality=50&h=40" alt="" class="search-thumb">'
        : '';
      html += '<a href="/event/' + encodeURIComponent(ev.id) + '" class="search-result">'
        + thumb
        + '<span class="search-meta">'
        + '<span class="search-label">' + escHtml(ev.label) + '</span>'
        + '<span class="search-camera">' + escHtml(ev.camera) + ' · ' + when + '</span>'
        + '</span></a>';
    });
    drop.innerHTML = html;
    drop.hidden = false;
  }

  function bind(form) {
    var input = form.querySelector('.search-input');
    var drop = form.querySelector('.search-drop');
    if (!input || !drop) return;

    var isSheet = !!form.closest('.search-sheet');
    var timer = null;

    function search(q) {
      if (!q || q.length < 2) {
        if (isSheet) {
          fetch('/v1/events/search?limit=8')
            .then(function (r) { return r.json(); })
            .then(function (events) { render(drop, events, ''); })
            .catch(function () {});
        } else {
          drop.hidden = true;
        }
        return;
      }
      fetch('/v1/events/search?limit=8&q=' + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (events) { render(drop, events, q); })
        .catch(function () { drop.hidden = true; });
    }

    input.addEventListener('input', function () {
      clearTimeout(timer);
      var q = input.value.trim();
      timer = setTimeout(function () { search(q); }, 300);
    });

    input.addEventListener('focus', function () {
      var q = input.value.trim();
      if (q.length >= 2) search(q);
      else if (isSheet) search('');
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (!isSheet) drop.hidden = true;
        input.blur();
      }
    });

    document.addEventListener('click', function (e) {
      if (!isSheet && !form.contains(e.target)) drop.hidden = true;
    });
  }

  Array.prototype.forEach.call(forms, bind);
})();
