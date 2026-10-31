// Identities: name (promotes + retro-labels past events), merge via a
// searchable picker, exclude/include single sightings (soft, undo-able),
// delete, and a lightbox for a sighting's face stack. Confirmations are
// SC.dialog and the viewer is SC.lightbox (util.js).
//
// The page is paginated, so cluster lookups for merging go through
// /enrich/clusters/index.json rather than the DOM: a "looks like" partner
// or a merge target is often not on the current page.

function post(path, body) {
  return SC.fetchJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function patch(path, body) {
  return SC.fetchJson(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

async function setExcluded(eventId, excluded) {
  return patch('/enrich/events/' + eventId, { excluded });
}

let indexPromise = null;
function clusterIndex() {
  if (!indexPromise) {
    indexPromise = SC.fetchJson('/enrich/clusters/index.json')
      .then(d => d.clusters.map(c => ({
        id: String(c.cluster_id),
        name: c.name || null,
        title: c.name || 'Unknown #' + c.cluster_id,
        thumb: c.avatar || null,
        count: c.observation_count,
      })))
      .catch(e => { indexPromise = null; throw e; });
  }
  return indexPromise;
}

async function clusterMeta(id) {
  const all = await clusterIndex();
  return all.find(m => m.id === String(id)) || null;
}

function section(id) {
  return document.querySelector('.id-cluster[data-cluster="' + id + '"]');
}

function clusterRow(meta, extra) {
  return SC.el('div', { class: 'id-pick-row' }, [
    meta.thumb
      ? SC.el('img', { src: meta.thumb, alt: '', loading: 'lazy' }, [])
      : SC.el('span', { class: 'id-pick-noimg', text: '?' }, []),
    SC.el('span', { class: 'id-pick-text' }, [
      SC.el('b', { text: meta.title }, []),
      SC.el('span', {
        text: meta.count + ' sighting' + (meta.count === 1 ? '' : 's') + (extra ? ' · ' + extra : ''),
      }, []),
    ]),
  ]);
}

async function doMerge(fromId, intoId) {
  try {
    await post('/enrich/clusters/' + fromId + '/merge', { into: Number(intoId) });
  } catch (e) {
    SC.toast('merge failed: ' + e.message, true);
    return;
  }
  SC.toast('merged');
  const into = section(intoId);
  const from = section(fromId);
  // Land on the survivor; reload so its strip and count include the merged sightings.
  if (into) window.location.hash = 'cluster-' + intoId;
  else if (from) from.remove();
  window.location.reload();
}

// Merge confirm for a "looks like" suggestion: show both clusters.
async function confirmMerge(fromId, intoId) {
  let from, into;
  try {
    [from, into] = await Promise.all([clusterMeta(fromId), clusterMeta(intoId)]);
  } catch (e) {
    SC.toast('could not load identities: ' + e.message, true);
    return;
  }
  if (!from || !into) { SC.toast('that identity no longer exists, reload the page', true); return; }
  // The named cluster survives; otherwise the bigger one does.
  let loser = from, winner = into;
  if ((from.name && !into.name) || (!from.name === !into.name && from.count > into.count)) {
    loser = into; winner = from;
  }
  SC.dialog('Merge these two identities?', [
    clusterRow(loser, 'goes away'),
    clusterRow(winner, 'keeps everything'),
  ], [
    { label: 'Cancel' },
    { label: 'Merge', kind: 'btn-primary', onclick: () => doMerge(loser.id, winner.id) },
  ]);
}

// Manual merge: search for the surviving cluster.
async function pickMergeTarget(fromId) {
  let all;
  try {
    all = await clusterIndex();
  } catch (e) {
    SC.toast('could not load identities: ' + e.message, true);
    return;
  }
  const from = all.find(m => m.id === String(fromId));
  const others = all.filter(m => m.id !== String(fromId));
  if (!from || !others.length) { SC.toast('no other identity to merge into', true); return; }

  const search = SC.el('input', {
    type: 'search', class: 'id-pick-search', placeholder: 'Search by name or #number',
    'aria-label': 'Search identities',
  }, []);
  const list = SC.el('div', { class: 'id-pick-list' }, []);
  let overlay = null;
  function render() {
    const q = search.value.trim().toLowerCase().replace(/^#/, '');
    const hits = others.filter(m => !q || m.title.toLowerCase().includes(q) || m.id === q);
    list.textContent = '';
    hits.slice(0, 60).forEach(m => {
      const row = clusterRow(m, null);
      row.classList.add('id-pick-tap');
      row.tabIndex = 0;
      const pick = () => { overlay.remove(); doMerge(fromId, m.id); };
      row.addEventListener('click', pick);
      row.addEventListener('keydown', ev => { if (ev.key === 'Enter') pick(); });
      list.appendChild(row);
    });
    if (!hits.length) list.appendChild(SC.el('p', { class: 'muted', text: 'No match.' }, []));
    else if (hits.length > 60) {
      list.appendChild(SC.el('p', { class: 'muted', text: (hits.length - 60) + ' more, refine the search' }, []));
    }
  }
  search.addEventListener('input', render);
  render();
  overlay = SC.dialog('Merge "' + from.title + '" into…', [search, list], [{ label: 'Cancel' }]);
  overlay.querySelector('.me-dialog').classList.add('id-pick-dialog');
  search.focus();
}

// ---- naming ------------------------------------------------------------

document.querySelectorAll('.id-name-form').forEach(form => {
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const id = form.dataset.id;
    const input = form.querySelector('.cluster-name');
    const btn = form.querySelector('.name-cluster');
    const name = (input.value || '').trim();
    if (!name) { SC.toast('enter a name first', true); input.focus(); return; }
    btn.disabled = true;
    try {
      const res = await post('/enrich/clusters/' + id + '/name', { name });
      const sec = section(id);
      if (sec) {
        sec.dataset.name = name;
        sec.classList.add('id-named');
        sec.querySelector('.id-title').textContent = name;
        const badge = sec.querySelector('.badge');
        badge.classList.remove('unknown');
        badge.classList.add('recognized');
        badge.textContent = 'named';
        btn.textContent = 'Save';
        form.hidden = true;
        const menu = sec.querySelector('.id-menu-pop');
        if (menu && !menu.querySelector('.rename-cluster')) {
          const rn = SC.el('button', { type: 'button', class: 'rename-cluster', 'data-id': id, text: 'Rename' }, []);
          rn.addEventListener('click', () => startRename(rn));
          menu.prepend(rn);
        }
      }
      indexPromise = null;
      SC.toast('Named "' + name + '", relabeled ' + (res.relabeled || 0) + ' past events');
    } catch (e) {
      SC.toast('naming failed: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });
});

function startRename(btn) {
  const sec = section(btn.dataset.id);
  if (!sec) return;
  btn.closest('details').removeAttribute('open');
  const form = sec.querySelector('.id-name-form');
  form.hidden = false;
  const input = form.querySelector('.cluster-name');
  input.focus();
  input.select();
}
document.querySelectorAll('.rename-cluster').forEach(btn => {
  btn.addEventListener('click', () => startRename(btn));
});

// ---- merge / delete ----------------------------------------------------

document.querySelectorAll('.id-similar').forEach(btn => {
  btn.addEventListener('click', () => confirmMerge(btn.dataset.id, btn.dataset.other));
});

document.querySelectorAll('.merge-cluster').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('details').removeAttribute('open');
    pickMergeTarget(btn.dataset.id);
  });
});

document.querySelectorAll('.delete-cluster').forEach(btn => {
  btn.addEventListener('click', () => {
    btn.closest('details').removeAttribute('open');
    const sec = section(btn.dataset.id);
    if (!sec) return;
    const title = sec.querySelector('.id-title').textContent;
    SC.dialog('Delete "' + title + '"?', [
      SC.el('p', { text: 'Its sightings stay in history but lose this identity. This can’t be undone.' }, []),
    ], [
      { label: 'Cancel' },
      {
        label: 'Delete', kind: 'btn-danger',
        onclick: async () => {
          try {
            await post('/enrich/clusters/' + btn.dataset.id + '/delete');
            sec.remove();
            indexPromise = null;
            SC.toast('deleted ' + title);
          } catch (e) {
            SC.toast('delete failed: ' + e.message, true);
          }
        },
      },
    ]);
  });
});

// Overflow menus: one open at a time, close on outside click / Escape.
document.addEventListener('click', ev => {
  document.querySelectorAll('details.id-menu[open]').forEach(d => {
    if (!d.contains(ev.target)) d.removeAttribute('open');
  });
});
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  document.querySelectorAll('details.id-menu[open]').forEach(d => d.removeAttribute('open'));
});

// ---- exclude / include -------------------------------------------------

async function exclude(card) {
  const eventId = card.dataset.event;
  try {
    await setExcluded(eventId, true);
  } catch (e) {
    SC.toast('exclude failed: ' + e.message, true);
    return false;
  }
  const parent = card.parentNode;
  const next = card.nextSibling;
  card.remove();
  SC.toast('Sighting excluded', false, {
    text: 'Undo',
    callback: async () => {
      try {
        await setExcluded(eventId, false);
      } catch (e) {
        SC.toast('undo failed: ' + e.message, true);
        return;
      }
      parent.insertBefore(card, next);
    },
  });
  return true;
}

document.querySelectorAll('.id-exclude').forEach(btn => {
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    await exclude(btn.closest('.id-sighting'));
    btn.disabled = false;
  });
});

document.querySelectorAll('.id-include').forEach(btn => {
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await setExcluded(btn.dataset.event, false);
    } catch (e) {
      SC.toast('include failed: ' + e.message, true);
      btn.disabled = false;
      return;
    }
    SC.toast('sighting included');
    window.location.reload();
  });
});

// ---- sighting lightbox -------------------------------------------------

function openSighting(card) {
  const strip = card.parentNode;
  const cards = Array.from(strip.querySelectorAll('.id-sighting'));
  const sec = card.closest('.id-cluster');
  const title = sec.querySelector('.id-title').textContent;
  const stage = SC.el('div', { class: 'id-lb-stage' }, []);
  const caption = SC.el('div', { class: 'id-lb-caption' }, []);
  const bar = SC.el('div', { class: 'id-lb-bar' }, []);
  let idx = cards.indexOf(card);

  function show() {
    const c = cards[idx];
    if (!c.isConnected) return;
    const eventId = c.dataset.event;
    stage.textContent = '';
    const stack = c.querySelector('.id-stack');
    if (stack) {
      const faces = stack.cloneNode(true);
      faces.hidden = false;
      faces.className = 'id-lb-faces';
      faces.querySelectorAll('img[data-src]').forEach(img => { img.src = img.dataset.src; });
      stage.appendChild(faces);
    } else {
      // No crops yet: Frigate's full snapshot (via the authed proxy), and
      // the thumbnail if that fails.
      const thumb = c.querySelector('img').src;
      const snap = SC.el('img', { class: 'id-lb-snap', src: '/api/events/' + eventId + '/snapshot.jpg', alt: '' }, []);
      snap.addEventListener('error', () => { snap.className = 'id-lb-thumb'; snap.src = thumb; }, { once: true });
      stage.appendChild(snap);
    }
    const ts = parseFloat(c.dataset.ts);
    caption.textContent = '';
    caption.append(
      SC.el('b', { text: title }, []),
      SC.el('span', { text: ' · ' + (ts ? SC.when(ts) : '') + (c.dataset.distance ? ' · distance ' + c.dataset.distance : '') }, []),
      SC.el('span', { class: 'id-lb-pos', text: ' · ' + (idx + 1) + ' / ' + cards.length }, []),
    );
    bar.textContent = '';
    bar.append(
      SC.el('a', { class: 'btn btn-neutral', href: '/event/' + eventId, text: 'Open event' }, []),
      SC.el('a', { class: 'btn btn-neutral', href: '/api/events/' + eventId + '/snapshot.jpg', target: '_blank', rel: 'noopener', text: 'Snapshot' }, []),
    );
    if (!c.classList.contains('id-excluded')) {
      const ex = SC.el('button', { type: 'button', class: 'btn btn-danger', text: 'Not this person' }, []);
      ex.addEventListener('click', async () => {
        ex.disabled = true;
        if (await exclude(c)) {
          cards.splice(idx, 1);
          if (!cards.length) { lb.close(); return; }
          idx = Math.min(idx, cards.length - 1);
          show();
        } else {
          ex.disabled = false;
        }
      });
      bar.appendChild(ex);
    }
  }

  function step(d) {
    if (cards.length < 2) return;
    idx = (idx + d + cards.length) % cards.length;
    show();
  }
  const prev = SC.el('button', { type: 'button', class: 'sc-lb-nav sc-lb-prev', 'aria-label': 'Previous sighting', text: '‹' }, []);
  const next = SC.el('button', { type: 'button', class: 'sc-lb-nav sc-lb-next', 'aria-label': 'Next sighting', text: '›' }, []);
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  if (cards.length < 2) { prev.hidden = true; next.hidden = true; }

  const lb = SC.lightbox([prev, SC.el('div', { class: 'sc-lb-body' }, [stage, caption, bar]), next], {
    onKey: ev => {
      if (ev.key === 'ArrowLeft') step(-1);
      else if (ev.key === 'ArrowRight') step(1);
    },
  });
  show();
}

document.querySelectorAll('.id-open').forEach(btn => {
  btn.addEventListener('click', () => openSighting(btn.closest('.id-sighting')));
});

// ---- face-crop backfill ------------------------------------------------
// The button only queues/un-queues; the enrichment worker does the work in
// idle cycles. While queued, poll the counts.
(function backfill() {
  const btn = document.querySelector('.id-backfill-btn');
  const text = document.querySelector('.id-backfill-text');
  if (!btn || !text) return;
  let timer = null;

  function render(st) {
    const running = st.queued > 0;
    text.textContent = '';
    if (running) {
      text.append('Building face crops: ', SC.el('b', { text: String(st.queued) }, []), ' queued · ' + st.done + ' done');
    } else if (st.ready > 0) {
      text.append(SC.el('b', { text: String(st.ready) }, []),
        ' older sightings have no face crops yet, so they show Frigate’s whole-person thumbnail.');
    } else {
      text.textContent = 'Face crops built for ' + st.done + ' older sightings'
        + (st.failed ? ' · ' + st.failed + ' no longer in Frigate' : '') + '.';
    }
    btn.dataset.run = running ? '0' : '1';
    btn.textContent = running ? 'Pause' : 'Build face crops';
    btn.hidden = !running && st.ready === 0;
    if (running && !timer) {
      timer = setInterval(async () => {
        try { render(await SC.fetchJson('/enrich/backfill.json')); } catch (e) { /* next tick */ }
      }, 30000);
    } else if (!running && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const st = await post('/enrich/backfill', { run: btn.dataset.run === '1' });
      render(st);
      SC.toast(st.queued ? 'queued ' + st.queued + ' sightings, crops build in the background' : 'paused');
    } catch (e) {
      SC.toast('backfill failed: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  if (btn.dataset.run === '0') {
    SC.fetchJson('/enrich/backfill.json').then(render).catch(() => {});
  }
})();
