// High-res cross-camera face captures: keep/discard review, manual rescan.

async function review(id, value, visit) {
  const res = await fetch('/faces/captures/' + id + '/review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ review: value, visit: !!visit }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    SC.toast('review failed: ' + detail, true);
    return false;
  }
  return true;
}

document.querySelectorAll('.capture-card .keep, .capture-card .discard').forEach(btn => {
  btn.addEventListener('click', async () => {
    const card = btn.closest('.capture-card');
    const value = btn.classList.contains('keep') ? 'kept' : 'discarded';
    if (await review(card.dataset.id, value, false)) card.remove();
  });
});

document.querySelectorAll('.discard-visit').forEach(btn => {
  btn.addEventListener('click', async () => {
    const section = btn.closest('.capture-visit');
    if (await review(btn.dataset.id, 'discarded', true)) section.remove();
  });
});

const rescan = document.getElementById('rescan-captures');
if (rescan) {
  rescan.addEventListener('click', async () => {
    rescan.disabled = true;
    rescan.textContent = 'capturing…';
    try {
      const res = await fetch('/faces/captures/scan', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        SC.toast('capture failed: ' + (data.detail || res.statusText), true);
        return;
      }
      const s = data.summary || {};
      SC.toast(`captured ${s.captured || 0}, no-rec ${s.no_recording || 0}, dedup ${s.deduped || 0}`);
      setTimeout(() => window.location.reload(), 900);
    } finally {
      rescan.disabled = false;
      rescan.textContent = 'Run capture now';
    }
  });
}
