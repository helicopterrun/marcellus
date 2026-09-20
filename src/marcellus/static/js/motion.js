// Motion page: preset buttons populate the form inputs and submit.
document.querySelectorAll('.preset').forEach(b => {
  b.addEventListener('click', () => {
    const form = document.querySelector('.motion-form');
    if (!form) return;
    form.querySelector('input[name=baseline]').value = b.dataset.base || '';
    form.querySelector('input[name=target]').value = b.dataset.tgt || '';
    form.submit();
  });
});
