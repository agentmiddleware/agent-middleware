(function () {
  "use strict";

  function configValue(name) {
    return document.querySelector(`meta[name="${name}"]`)?.content.trim() || "";
  }

  const key = configValue("amw-posthog-key");
  const host = configValue("amw-posthog-host");
  // Source pages and unconfigured builds do not load a client or send events.
  if (!key || !["https://us.i.posthog.com", "https://eu.i.posthog.com"].includes(host)) return;
  if (window.amwPosthog) return;

  // Keep calls made while the local client loads, including UTM registration.
  // Bound the queue if loading is blocked; analytics must never block a CTA.
  const queue = [];
  window.amwPosthog = {
    config: { key, host },
    queue,
    register(properties) {
      if (queue.length < 50) queue.push(["register", properties]);
    },
    capture(name, properties) {
      if (queue.length < 50) queue.push(["capture", name, properties]);
    },
  };

  // booking_click remains part of the existing contract for configured bookings.
  const allowedEvents = new Set([
    "booking_click",
    "demo_key_click",
    "email_click",
    "proof_click",
  ]);

  document.addEventListener("click", function (event) {
    const target = event.target?.closest?.("[data-analytics-event]");
    if (!target) return;
    const eventName = target.dataset.analyticsEvent;
    const ctaId = target.dataset.ctaId;
    if (!allowedEvents.has(eventName) || !/^[a-z0-9_-]{1,80}$/.test(ctaId || "")) return;

    try {
      window.amwPosthog.capture(eventName, {
        product: "AMW",
        page: location.pathname,
        cta_id: ctaId,
      });
    } catch {
      // Navigation and demo-key behavior must survive analytics failures.
    }
  });

  const loader = document.createElement("script");
  loader.src = "/posthog.js?v=posthog-1";
  loader.async = true;
  loader.onerror = function () {
    queue.length = 0;
    window.amwPosthog = { register() {}, capture() {} };
  };
  document.head.appendChild(loader);
})();
