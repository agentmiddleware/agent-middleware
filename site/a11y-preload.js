/* Apply saved accessibility preferences before first paint.
   Loaded synchronously in <head>. Kept as a same-origin file (not an inline
   <script>) so the site's Content-Security-Policy needs no 'unsafe-inline'. */
(function () {
  "use strict";
  try {
    var prefs = JSON.parse(window.localStorage.getItem("amw-a11y") || "{}");
    var root = document.documentElement;
    if (prefs.scale) root.style.setProperty("--a11y-scale", prefs.scale);
    if (prefs.contrast) root.setAttribute("data-a11y-contrast", "high");
    if (prefs.motion) root.setAttribute("data-a11y-motion", "reduce");
    if (prefs.spacing) root.setAttribute("data-a11y-spacing", "wide");
    if (prefs.underline) root.setAttribute("data-a11y-underline", "on");
  } catch (error) {
    /* private mode or corrupt value: fall back to unmodified defaults */
  }
})();
