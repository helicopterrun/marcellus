// Triage list: persist session tag in localStorage.
SC.bindSessionInput();

// Phones: collapse the filter bar behind its "Filters" disclosure unless a
// non-default filter is active (so a filtered view stays self-explanatory).
(function () {
  var wrap = document.querySelector(".filters-wrap");
  if (!wrap || window.innerWidth > 720) return;
  if (!window.location.search) wrap.removeAttribute("open");
})();
