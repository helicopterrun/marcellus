// Face captures: one card per visit. Keep/discard the whole visit (undo-able),
// flip through its frames in the filmstrip, open the full frame in a
// lightbox (where a single bad frame can be discarded), manual rescan.

const VIEW = new URLSearchParams(window.location.search).get('review') || 'pending';

async function review(id, value, visit) {
  try {
    await SC.fetchJson('/faces/captures/' + id + '/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ review: value, visit: !!visit }),
    });
    return true;
  } catch (e) {
    SC.toast('review failed: ' + e.message, true);
    return false;
  }
}

// A card leaves the view when its new state no longer matches the filter.
function leaves(value) {
  return VIEW !== 'all' && VIEW !== value;
}

async function reviewVisit(card, value) {
  const id = card.dataset.id;
  card.classList.add('busy');
  const ok = await review(id, value, true);
  card.classList.remove('busy');
  if (!ok) return;
  if (!leaves(value)) {
    SC.toast(value === 'kept' ? 'Kept' : 'Discarded');
    return;
  }
  const parent = card.parentNode;
  const next = card.nextSibling;
  card.remove();
  SC.toast(value === 'kept' ? 'Kept visit' : 'Discarded visit', false, {
    text: 'Undo',
    callback: async () => {
      if (await review(id, 'pending', true)) parent.insertBefore(card, next);
    },
  });
}

document.querySelectorAll('.fc-card').forEach(card => {
  card.querySelector('.fc-keep').addEventListener('click', () => reviewVisit(card, 'kept'));
  card.querySelector('.fc-discard').addEventListener('click', () => reviewVisit(card, 'discarded'));

  // Filmstrip: swap the hero.
  const hero = card.querySelector('.fc-hero');
  card.querySelectorAll('.fc-frame').forEach(fr => {
    fr.addEventListener('click', () => {
      card.querySelectorAll('.fc-frame.active').forEach(a => a.classList.remove('active'));
      fr.classList.add('active');
      hero.dataset.id = fr.dataset.id;
      hero.querySelector('img').src = fr.dataset.src;
    });
  });
  hero.addEventListener('click', () => openFrame(card, hero.dataset.id));
});

function frames(card) {
  const film = Array.from(card.querySelectorAll('.fc-frame'));
  if (film.length) {
    return film.map(f => ({
      id: f.dataset.id, offset: Number(f.dataset.offset), score: f.dataset.score,
      box: parseBox(f.dataset.box), node: f,
    }));
  }
  const hero = card.querySelector('.fc-hero');
  return [{ id: hero.dataset.id, offset: null, score: null, box: parseBox(hero.dataset.box), node: null }];
}

function parseBox(raw) {
  try { return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
}

function openFrame(card, startId) {
  let list = frames(card);
  let idx = Math.max(0, list.findIndex(f => f.id === String(startId)));
  // The face the gate picked, outlined: frames often hold two people.
  const img = SC.el('img', { class: 'fc-lb-img', alt: '' }, []);
  const mark = SC.el('span', { class: 'fc-lb-box' }, []);
  const frame = SC.el('span', { class: 'fc-lb-frame' }, [img, mark]);
  const caption = SC.el('div', { class: 'id-lb-caption' }, []);
  const bar = SC.el('div', { class: 'id-lb-bar' }, []);
  const when = card.querySelector('.fc-title time');

  function show() {
    const f = list[idx];
    img.src = '/faces/captures/' + f.id + '/full.jpg';
    mark.hidden = !f.box;
    if (f.box) {
      mark.style.left = (f.box[0] * 100) + '%';
      mark.style.top = (f.box[1] * 100) + '%';
      mark.style.width = ((f.box[2] - f.box[0]) * 100) + '%';
      mark.style.height = ((f.box[3] - f.box[1]) * 100) + '%';
    }
    caption.textContent = '';
    caption.append(SC.el('b', { text: when ? when.textContent : '' }, []));
    const bits = [];
    if (f.offset !== null && !Number.isNaN(f.offset)) bits.push((f.offset >= 0 ? '+' : '') + (f.offset / 1000).toFixed(1) + 's');
    if (f.score) bits.push('face ' + f.score);
    if (list.length > 1) bits.push((idx + 1) + ' / ' + list.length);
    if (bits.length) caption.append(SC.el('span', { text: ' · ' + bits.join(' · ') }, []));
    bar.textContent = '';
    bar.append(SC.el('a', {
      class: 'btn btn-neutral', href: '/faces/captures/' + f.id + '/full.jpg',
      target: '_blank', rel: 'noopener', text: 'Full size',
    }, []));
    if (list.length > 1) {
      const drop = SC.el('button', { type: 'button', class: 'btn btn-danger', text: 'Discard this frame' }, []);
      drop.addEventListener('click', async () => {
        drop.disabled = true;
        if (!(await review(f.id, 'discarded', false))) { drop.disabled = false; return; }
        if (f.node && leaves('discarded')) {
          const wasActive = f.node.classList.contains('active');
          f.node.remove();
          list.splice(idx, 1);
          if (wasActive && list[0].node) list[0].node.click();
          if (list.length < 2) {
            const film = card.querySelector('.fc-film');
            if (film) film.remove();
          }
        }
        SC.toast('frame discarded');
        idx = Math.min(idx, list.length - 1);
        if (list.length) show();
      });
      bar.appendChild(drop);
    }
  }
  function step(d) {
    if (list.length < 2) return;
    idx = (idx + d + list.length) % list.length;
    show();
  }
  const prev = SC.el('button', { type: 'button', class: 'sc-lb-nav sc-lb-prev', 'aria-label': 'Previous frame', text: '‹' }, []);
  const next = SC.el('button', { type: 'button', class: 'sc-lb-nav sc-lb-next', 'aria-label': 'Next frame', text: '›' }, []);
  prev.addEventListener('click', () => step(-1));
  next.addEventListener('click', () => step(1));
  if (list.length < 2) { prev.hidden = true; next.hidden = true; }
  SC.lightbox([prev, SC.el('div', { class: 'sc-lb-body' }, [frame, caption, bar]), next], {
    onKey: ev => {
      if (ev.key === 'ArrowLeft') step(-1);
      else if (ev.key === 'ArrowRight') step(1);
    },
  });
  show();
}

const rescan = document.getElementById('rescan-captures');
if (rescan) {
  rescan.addEventListener('click', async () => {
    rescan.disabled = true;
    rescan.textContent = 'Checking…';
    try {
      const data = await SC.fetchJson('/faces/captures/scan', { method: 'POST' });
      const s = data.summary || {};
      if (s.captured) {
        SC.toast(s.captured + ' new face' + (s.captured === 1 ? '' : 's'));
        setTimeout(() => window.location.reload(), 900);
      } else {
        SC.toast('Nothing new' + (s.no_face ? ' (' + s.no_face + ' frames had no face)' : ''));
      }
    } catch (e) {
      SC.toast('capture failed: ' + e.message, true);
    } finally {
      rescan.disabled = false;
      rescan.textContent = 'Check now';
    }
  });
}
