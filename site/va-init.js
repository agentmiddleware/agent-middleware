/* Vercel Web Analytics queue shim.
   Emitted only when PUBLIC_ENABLE_VERCEL_ANALYTICS=true; see build_site.py.
   Kept as a same-origin file (not an inline <script>) so the site's
   Content-Security-Policy needs no 'unsafe-inline' for scripts. */
window.va =
  window.va ||
  function () {
    (window.vaq = window.vaq || []).push(arguments);
  };
