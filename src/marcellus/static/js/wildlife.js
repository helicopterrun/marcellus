// Wildlife finder review: keep/discard a visit, send one frame to Frigate+.

async function post(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const d = data.detail;
    throw new Error((d && (d.message || d.error)) || d || res.statusText);
  }
  return data;
}

const reviewFilter = new URLSearchParams(window.location.search).get('review') || 'pending';

document.querySelectorAll('.keep-visit, .discard-visit').forEach(btn => {
  btn.addEventListener('click', async () => {
    const section = btn.closest('.wildlife-visit');
    const value = btn.classList.contains('keep-visit') ? 'kept' : 'discarded';
    try {
      await post('/wildlife/visits/' + btn.dataset.id + '/review', { review: value });
    } catch (e) {
      SC.toast('review failed: ' + e.message, true);
      return;
    }
    if (reviewFilter === 'pending') section.remove();
    else SC.toast('marked ' + value);
  });
});

document.querySelectorAll('.send-plus').forEach(btn => {
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'sending…';
    try {
      await post('/wildlife/boxes/' + btn.dataset.id + '/plus');
    } catch (e) {
      SC.toast('Frigate+ upload failed: ' + e.message, true);
      btn.disabled = false;
      btn.textContent = 'Frigate+';
      return;
    }
    const done = document.createElement('span');
    done.className = 'sent';
    done.textContent = 'sent ✓';
    btn.replaceWith(done);
    SC.toast('Sent to Frigate+ — label it at plus.frigate.video');
  });
});
