/* ==========================================================================
   THE HUMAN WAITING ROOM — the door, not the room.

   /arcade.js is the largest thing the landing page can load, and nobody
   needs it until they press START. This file is what ships on every visit
   instead: it reveals the launcher (the arcade's proof of life used to be
   arcade.js itself running), and fetches /arcade.js and /arcade.css the
   first time someone asks for the room — on click, on a ?arcade= deep link,
   or speculatively on hover/focus so the click feels instant.

   Contracts this file lives inside, the same as arcade.js:

   - CSP is script-src 'self' / style-src 'self' with no 'unsafe-inline'. The
     script and stylesheet are injected as same-origin <script> and <link>
     elements, which the policy allows; nothing inline is created.
   - The launcher ships hidden and is revealed only once this script runs, so
     a visitor without JavaScript never sees a control that does nothing.
   - The asset URLs come from the launcher's data attributes, so the cache
     token lives in the HTML with every other asset reference and the build's
     token check covers them.
   - Once arcade.js has loaded it owns the launcher: it attaches its own click
     handler and handles ?arcade= itself, as it did when it loaded
     eagerly. This file steps aside after the first load.
   ========================================================================== */
(function () {
  "use strict";

  var doc = document;
  var launcher = doc.getElementById("arcade-launch");
  if (!launcher || !doc.createElement("canvas").getContext) return;

  var scriptSrc = launcher.getAttribute("data-arcade-script");
  var styleHref = launcher.getAttribute("data-arcade-style");
  if (!scriptSrc || !styleHref) return;

  // Same reveal arcade.js performs, moved here so it happens on every visit
  // without the room itself being downloaded.
  launcher.hidden = false;
  if (typeof launcher.closest === "function") {
    var launchWrap = launcher.closest(".footer-arcade");
    if (launchWrap) launchWrap.hidden = false;
  }

  var loading = null;

  function load() {
    if (window.__amwArcade) return Promise.resolve(window.__amwArcade);
    if (loading) return loading;
    launcher.setAttribute("aria-busy", "true");
    loading = new Promise(function (resolve, reject) {
      var link = doc.createElement("link");
      link.rel = "stylesheet";
      link.href = styleHref;
      doc.head.appendChild(link);

      var script = doc.createElement("script");
      script.src = scriptSrc;
      script.async = true;
      script.onload = function () {
        launcher.removeAttribute("aria-busy");
        if (window.__amwArcade) {
          resolve(window.__amwArcade);
          return;
        }
        // The file arrived but never announced itself. Drop the failed
        // attempt so the next press injects again instead of reusing it.
        loading = null;
        reject(new Error("arcade did not initialise"));
      };
      script.onerror = function () {
        launcher.removeAttribute("aria-busy");
        loading = null;
        reject(new Error("arcade failed to load"));
      };
      doc.body.appendChild(script);
    });
    return loading;
  }

  // First click: fetch, then open. Later clicks: arcade.js has attached its
  // own listener by then, so this one does nothing.
  launcher.addEventListener("click", function () {
    if (window.__amwArcade) return;
    load()
      .then(function (arcade) {
        arcade.open();
      })
      .catch(function () {
        /* The launcher stays usable; a retry re-injects the script. */
      });
  });

  // Warm the cache once intent is likely; a hover or a focus is cheap to
  // guess wrong on and makes the eventual press instant.
  function warm() {
    load().catch(function () {});
  }
  launcher.addEventListener("pointerenter", warm, { once: true });
  launcher.addEventListener("focus", warm, { once: true });

  // ?arcade=<cabinet-id> opens straight into a cabinet, ?arcade=1 opens the
  // selector. arcade.js reads the parameter itself once it runs, so this only
  // has to start the download.
  try {
    if (new URLSearchParams(window.location.search).get("arcade")) warm();
  } catch (error) {
    /* URLSearchParams is absent on very old engines; the launcher still works. */
  }
})();
