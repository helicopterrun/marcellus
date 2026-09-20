// Triage detail: label/clear keyboard shortcuts, prev/next nav, zone overlay toggle.
SC.bindSessionInput();
const sessionInput = document.getElementById('session');

const eventId = document.body.dataset.eventId;
const prevUrl = document.body.dataset.prevUrl;
const nextUrl = document.body.dataset.nextUrl;

// Warm the browser cache for the pages the triage loop is about to hit, so
// the post-label navigation lands on an already-fetched document.
[nextUrl, prevUrl].forEach((url) => {
  if (!url) return;
  const link = document.createElement('link');
  link.rel = 'prefetch';
  link.href = url;
  document.head.appendChild(link);
});

// Frigate+ submit toggle: persisted opt-in across pages. Defaults to off so
// the very first triage click can't surprise-submit to Plus.
const plusToggle = document.getElementById('plus-submit');
const plusStatus = document.getElementById('plus-status');
if (plusToggle && !plusToggle.disabled) {
  const saved = localStorage.getItem('triage_plus_submit');
  plusToggle.checked = saved === '1';
  plusToggle.addEventListener('change', () => {
    localStorage.setItem('triage_plus_submit', plusToggle.checked ? '1' : '0');
  });
}

function setPlusStatus(cls, text) {
  if (!plusStatus) return;
  plusStatus.classList.remove('sent', 'skipped', 'error');
  if (cls) plusStatus.classList.add(cls);
  plusStatus.textContent = text || '';
}

function setActionButtonsDisabled(disabled) {
  document.querySelectorAll('[data-label], [data-clear]').forEach(b => { b.disabled = disabled; });
}

async function applyLabel(label) {
  // Instant feedback: light the chosen button before the round-trip.
  document.querySelectorAll('[data-label]').forEach(b => {
    b.classList.toggle('active', b.dataset.label === label);
  });
  const note = document.getElementById('note').value;
  const session = sessionInput ? sessionInput.value : '';
  const submitPlus = !!(plusToggle && plusToggle.checked && !plusToggle.disabled);
  setActionButtonsDisabled(true);
  let res;
  try {
    res = await fetch('/label', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: eventId, label, note, session, submit_plus: submitPlus})
    });
  } finally {
    setActionButtonsDisabled(false);
  }
  if (!res.ok) { SC.toast('Save failed: ' + await res.text(), true); return; }
  const result = await res.json();
  const plus = result.plus || {status: 'not_requested'};

  // On Plus error, stop and surface the message so the user can decide.
  // On sent/skipped/not_requested, continue to the next event as before.
  if (plus.status === 'error') {
    setPlusStatus('error', 'Plus: ' + (plus.reason || 'submission failed'));
    return;
  }
  if (plus.status === 'sent') {
    // Brief flash before navigating away.
    setPlusStatus('sent', 'Plus: sent (' + (plus.plus_id || '') + ')');
  }
  if (!nextUrl) { window.location.reload(); return; }

  const before = result.before;
  let advanceTimer = setTimeout(() => { window.location = nextUrl; }, 3000);
  SC.toast('Marked ' + label, false, {
    text: 'Undo',
    callback: async () => {
      clearTimeout(advanceTimer);
      const undoBody = before === null || before === undefined
        ? {event_id: eventId}
        : {event_id: eventId, label: before, note, session};
      await fetch(before === null || before === undefined ? '/clear-label' : '/label', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(undoBody)
      });
      window.location.reload();
    }
  });
}

async function clearLabel() {
  setActionButtonsDisabled(true);
  let res;
  try {
    res = await fetch('/clear-label', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: eventId})
    });
  } finally {
    setActionButtonsDisabled(false);
  }
  if (!res.ok) { SC.toast('Clear failed: ' + await res.text(), true); return; }
  // Same toast treatment as a label: a beat of feedback before the reload,
  // rather than jumping straight to a blank page.
  SC.toast('Cleared');
  setTimeout(() => { window.location.reload(); }, 700);
}

document.querySelectorAll('[data-label]').forEach(b => {
  b.addEventListener('click', () => applyLabel(b.dataset.label));
});
document.querySelectorAll('[data-clear]').forEach(b => {
  b.addEventListener('click', clearLabel);
});

const zonesToggle = document.getElementById('zones-on');
const allZonesToggle = document.getElementById('all-zones-on');
const detailMedia = document.querySelector('.detail-media');
function applyZoneToggle() {
  if (!zonesToggle || !detailMedia) return;
  detailMedia.classList.toggle('zones-hide', !zonesToggle.checked);
  localStorage.setItem('triage_zones_on', zonesToggle.checked ? '1' : '0');
}
function applyAllZonesToggle() {
  if (!allZonesToggle || !detailMedia) return;
  detailMedia.classList.toggle('show-all-zones', allZonesToggle.checked);
  localStorage.setItem('triage_all_zones_on', allZonesToggle.checked ? '1' : '0');
}
if (zonesToggle && detailMedia) {
  const saved = localStorage.getItem('triage_zones_on');
  if (saved === '0') zonesToggle.checked = false;
  applyZoneToggle();
  zonesToggle.addEventListener('change', applyZoneToggle);
}
if (allZonesToggle && detailMedia) {
  const saved = localStorage.getItem('triage_all_zones_on');
  if (saved === '1') allZonesToggle.checked = true;
  applyAllZonesToggle();
  allZonesToggle.addEventListener('change', applyAllZonesToggle);
}

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 't') applyLabel('tp');
  else if (e.key === 'f') applyLabel('fp');
  else if (e.key === 's') applyLabel('skip');
  else if (e.key === 'c') clearLabel();
  else if (e.key === 'j' || e.key === 'ArrowRight') { if (nextUrl) window.location = nextUrl; }
  else if (e.key === 'k' || e.key === 'ArrowLeft') { if (prevUrl) window.location = prevUrl; }
  else if (e.key === 'z' && zonesToggle) { zonesToggle.checked = !zonesToggle.checked; applyZoneToggle(); }
  else if (e.key === 'a' && allZonesToggle) { allZonesToggle.checked = !allZonesToggle.checked; applyAllZonesToggle(); }
  else if (e.key === 'p' && plusToggle && !plusToggle.disabled) {
    plusToggle.checked = !plusToggle.checked;
    localStorage.setItem('triage_plus_submit', plusToggle.checked ? '1' : '0');
    setPlusStatus('skipped', 'Plus submit ' + (plusToggle.checked ? 'ON' : 'OFF'));
  }
});

document.querySelectorAll('.detail-tabs button[data-tab]').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.detail-tabs button[data-tab]').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(x => x.style.display = 'none');
    b.classList.add('active');
    document.getElementById('tab-' + b.dataset.tab).style.display = 'block';
  });
});
