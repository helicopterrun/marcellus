// Identities: name (promotes + retro-labels past events), merge via picker
// dialog, exclude/include single sightings (soft, undo-able), delete.
// Confirmation dialogs are SC.dialog
// (util.js) -- shared with eventalign.js's restart confirm and zones.js's
// import confirm, all reusing the map editor's .me-overlay/.me-dialog
// classes; everything else here is SC helpers.

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

function clusterMeta(id) {
  const section = document.querySelector('.id-cluster[data-cluster="' + id + '"]');
  if (!section) return null;
  return {
    section,
    id: String(id),
    name: section.dataset.name || null,
    title: section.querySelector('.id-title').textContent,
    thumb: section.dataset.thumb || null,
  };
}

const dialog = SC.dialog;

function clusterRow(meta, extra) {
  const row = SC.el('div', { class: 'id-pick-row' }, [
    meta.thumb
      ? SC.el('img', { src: meta.thumb, alt: '' }, [])
      : SC.el('span', { class: 'id-pick-noimg', text: '?' }, []),
    SC.el('span', { text: meta.title + (extra ? ' · ' + extra : '') }, []),
  ]);
  return row;
}

async function doMerge(fromId, intoId) {
  try {
    await post('/enrich/clusters/' + fromId + '/merge', { into: Number(intoId) });
  } catch (e) {
    SC.toast('merge failed: ' + e.message, true);
    return;
  }
  SC.toast('merged');
  window.location.reload();
}

// Merge confirm for a "looks like" suggestion: show both clusters.
function confirmMerge(fromId, intoId) {
  const from = clusterMeta(fromId);
  const into = clusterMeta(intoId);
  if (!from || !into) return;
  // The named (or older) cluster survives; merge the unnamed one into it.
  let loser = from, winner = into;
  if (from.name && !into.name) { loser = into; winner = from; }
  dialog('Merge these two identities?', [
    clusterRow(loser, 'goes away'),
    clusterRow(winner, 'keeps its sightings + gains the others'),
  ], [
    { label: 'cancel' },
    { label: 'merge', kind: 'btn-primary', onclick: () => doMerge(loser.id, winner.id) },
  ]);
}

// Manual merge: pick the surviving cluster from a list.
function pickMergeTarget(fromId) {
  const from = clusterMeta(fromId);
  const others = Array.from(document.querySelectorAll('.id-cluster'))
    .map(s => clusterMeta(s.dataset.cluster))
    .filter(m => m && m.id !== String(fromId));
  if (!others.length) { SC.toast('no other cluster to merge into', true); return; }
  const rows = others.map(m => {
    const row = clusterRow(m, null);
    row.classList.add('id-pick-tap');
    row.addEventListener('click', () => { overlay.remove(); doMerge(fromId, m.id); });
    return row;
  });
  const overlay = dialog('Merge "' + from.title + '" into…', rows, [{ label: 'cancel' }]);
}

document.querySelectorAll('.name-cluster').forEach(btn => {
  btn.addEventListener('click', async () => {
    const id = btn.dataset.id;
    const input = document.querySelector('.cluster-name[data-id="' + id + '"]');
    const name = ((input && input.value) || '').trim();
    if (!name) { SC.toast('enter a name first', true); input && input.focus(); return; }
    btn.disabled = true;
    try {
      const res = await post('/enrich/clusters/' + id + '/name', { name });
      const meta = clusterMeta(id);
      if (meta) {
        meta.section.dataset.name = name;
        meta.section.querySelector('.id-title').textContent = name;
        const badge = meta.section.querySelector('.badge');
        badge.classList.remove('unknown');
        badge.classList.add('recognized');
        badge.textContent = 'known';
        btn.textContent = 'rename';
      }
      SC.toast('named "' + name + '" — relabeled ' + (res.relabeled || 0) + ' past events');
    } catch (e) {
      SC.toast('naming failed: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });
});

document.querySelectorAll('.id-similar').forEach(btn => {
  btn.addEventListener('click', () => confirmMerge(btn.dataset.id, btn.dataset.other));
});

document.querySelectorAll('.merge-cluster').forEach(btn => {
  btn.addEventListener('click', () => pickMergeTarget(btn.dataset.id));
});

document.querySelectorAll('.delete-cluster').forEach(btn => {
  btn.addEventListener('click', () => {
    const meta = clusterMeta(btn.dataset.id);
    if (!meta) return;
    dialog('Delete "' + meta.title + '"?', [
      SC.el('p', { text: 'Its sightings stay in history but lose this identity.' }, []),
    ], [
      { label: 'cancel' },
      {
        label: 'delete', kind: 'btn-danger',
        onclick: async () => {
          try {
            await post('/enrich/clusters/' + meta.id + '/delete');
            meta.section.remove();
          } catch (e) {
            SC.toast('delete failed: ' + e.message, true);
          }
        },
      },
    ]);
  });
});

document.querySelectorAll('.id-exclude').forEach(btn => {
  btn.addEventListener('click', async () => {
    const eventId = btn.dataset.event;
    const card = btn.closest('.id-sighting');
    btn.disabled = true;
    try {
      await setExcluded(eventId, true);
    } catch (e) {
      SC.toast('exclude failed: ' + e.message, true);
      btn.disabled = false;
      return;
    }
    card && card.remove();
    SC.toast('Sighting excluded', false, {
      text: 'Undo',
      callback: async () => {
        try {
          await setExcluded(eventId, false);
        } catch (e) {
          SC.toast('undo failed: ' + e.message, true);
          return;
        }
        window.location.reload();
      },
    });
  });
});

document.querySelectorAll('.id-include').forEach(btn => {
  btn.addEventListener('click', async () => {
    const eventId = btn.dataset.event;
    btn.disabled = true;
    try {
      await setExcluded(eventId, false);
    } catch (e) {
      SC.toast('include failed: ' + e.message, true);
      btn.disabled = false;
      return;
    }
    SC.toast('sighting included');
    window.location.reload();
  });
});
