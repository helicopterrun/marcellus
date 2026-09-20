(function () {
  "use strict";
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  window.ElsinoreTokens = { cssVar: cssVar };
})();
