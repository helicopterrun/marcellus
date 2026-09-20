(function () {
  var btn = document.getElementById('more-btn');
  var sheet = document.getElementById('more-sheet');
  var backdrop = document.getElementById('more-backdrop');
  if (!btn || !sheet) return;

  var searchBtn = document.getElementById('search-btn');
  var searchSheet = document.getElementById('search-sheet');
  var searchBackdrop = document.getElementById('search-backdrop');

  function closeSearch() {
    if (!searchSheet) return;
    searchSheet.classList.remove('open');
    if (searchBackdrop) searchBackdrop.classList.remove('open');
  }

  function toggle() {
    closeSearch();
    var open = sheet.classList.toggle('open');
    backdrop.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open);
  }

  function closeMore() {
    if (!sheet.classList.contains('open')) return;
    sheet.classList.remove('open');
    backdrop.classList.remove('open');
    btn.setAttribute('aria-expanded', false);
  }

  function openSearch() {
    closeMore();
    searchSheet.classList.add('open');
    if (searchBackdrop) searchBackdrop.classList.add('open');
    var input = searchSheet.querySelector('.search-input');
    if (input) input.focus();
  }

  btn.addEventListener('click', toggle);
  backdrop.addEventListener('click', toggle);

  if (searchBtn && searchSheet) {
    searchBtn.addEventListener('click', openSearch);
    if (searchBackdrop) searchBackdrop.addEventListener('click', closeSearch);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (sheet.classList.contains('open')) toggle();
    else if (searchSheet && searchSheet.classList.contains('open')) closeSearch();
  });
})();
