(function () {
  "use strict";

  // booking_click fires only when the optional PUBLIC_BOOKING_URL is
  // configured at build time; email_click is the primary intake path.
  const allowedEvents = new Set([
    "booking_click",
    "email_click",
    "proof_click",
  ]);

  document.addEventListener("click", function (event) {
    const target = event.target.closest("[data-analytics-event]");
    if (!target) return;

    const eventName = target.dataset.analyticsEvent;
    if (!allowedEvents.has(eventName) || typeof window.va !== "function") return;

    // Event names contain no link target, email address, booking URL, or other PII.
    window.va("event", { name: eventName });
  });
})();
