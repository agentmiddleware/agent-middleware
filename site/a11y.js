/* Accessibility preferences panel.
   Injects a floating toggle + panel, persists choices, and applies them as
   data attributes on <html> so styles.css can respond. No network calls. */
(function () {
  "use strict";

  var STORAGE_KEY = "amw-a11y";
  var MIN_SCALE = 0.9;
  var MAX_SCALE = 1.4;
  var STEP = 0.1;
  var root = document.documentElement;

  var defaults = {
    scale: 1,
    contrast: false,
    motion: false,
    spacing: false,
    underline: false,
  };

  function loadState() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return Object.assign({}, defaults);
      var parsed = JSON.parse(raw);
      var state = Object.assign({}, defaults, parsed);
      state.scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(state.scale) || 1));
      return state;
    } catch (error) {
      return Object.assign({}, defaults);
    }
  }

  function saveState(state) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (error) {
      /* private mode: preferences simply do not persist */
    }
  }

  function applyState(state) {
    root.style.setProperty("--a11y-scale", String(state.scale));
    if (state.contrast) {
      root.setAttribute("data-a11y-contrast", "high");
    } else {
      root.removeAttribute("data-a11y-contrast");
    }
    if (state.motion) {
      root.setAttribute("data-a11y-motion", "reduce");
    } else {
      root.removeAttribute("data-a11y-motion");
    }
    if (state.spacing) {
      root.setAttribute("data-a11y-spacing", "wide");
    } else {
      root.removeAttribute("data-a11y-spacing");
    }
    if (state.underline) {
      root.setAttribute("data-a11y-underline", "on");
    } else {
      root.removeAttribute("data-a11y-underline");
    }
  }

  var state = loadState();
  applyState(state);

  function build() {
    if (document.getElementById("amw-a11y-panel")) return;
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "amw-a11y-toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", "amw-a11y-panel");
    toggle.setAttribute("aria-label", "Accessibility options");
    toggle.textContent = "Aa";

    var panel = document.createElement("div");
    panel.className = "amw-a11y-panel";
    panel.id = "amw-a11y-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Accessibility options");
    panel.setAttribute("tabindex", "-1");
    panel.hidden = true;

    var heading = document.createElement("h2");
    heading.textContent = "Accessibility";
    panel.appendChild(heading);

    /* Text size row */
    var sizeRow = document.createElement("div");
    sizeRow.className = "amw-a11y-row";
    var sizeLabel = document.createElement("span");
    sizeLabel.textContent = "Text size";
    sizeLabel.id = "amw-a11y-size-label";
    var sizeControls = document.createElement("span");
    sizeControls.className = "amw-a11y-size";
    sizeControls.setAttribute("role", "group");
    sizeControls.setAttribute("aria-labelledby", "amw-a11y-size-label");

    var smaller = document.createElement("button");
    smaller.type = "button";
    smaller.textContent = "−";
    smaller.setAttribute("aria-label", "Decrease text size");

    var readout = document.createElement("output");
    readout.setAttribute("aria-live", "polite");

    var larger = document.createElement("button");
    larger.type = "button";
    larger.textContent = "+";
    larger.setAttribute("aria-label", "Increase text size");

    sizeControls.appendChild(smaller);
    sizeControls.appendChild(readout);
    sizeControls.appendChild(larger);
    sizeRow.appendChild(sizeLabel);
    sizeRow.appendChild(sizeControls);
    panel.appendChild(sizeRow);

    function renderScale() {
      readout.value = Math.round(state.scale * 100) + "%";
    }
    renderScale();

    smaller.addEventListener("click", function () {
      state.scale = Math.max(MIN_SCALE, Math.round((state.scale - STEP) * 10) / 10);
      applyState(state);
      saveState(state);
      renderScale();
    });
    larger.addEventListener("click", function () {
      state.scale = Math.min(MAX_SCALE, Math.round((state.scale + STEP) * 10) / 10);
      applyState(state);
      saveState(state);
      renderScale();
    });

    /* Switch rows */
    var switches = [
      { key: "contrast", label: "High contrast" },
      { key: "motion", label: "Reduce motion" },
      { key: "spacing", label: "Comfortable spacing" },
      { key: "underline", label: "Underline links" },
    ];

    var switchButtons = {};

    switches.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "amw-a11y-row";
      var label = document.createElement("span");
      label.textContent = item.label;
      label.id = "amw-a11y-lbl-" + item.key;

      var control = document.createElement("button");
      control.type = "button";
      control.className = "amw-a11y-switch";
      control.id = "amw-a11y-sw-" + item.key;
      control.setAttribute("aria-pressed", state[item.key] ? "true" : "false");
      control.setAttribute("aria-labelledby", label.id + " " + control.id);
      control.textContent = state[item.key] ? "On" : "Off";

      control.addEventListener("click", function () {
        state[item.key] = !state[item.key];
        control.setAttribute("aria-pressed", state[item.key] ? "true" : "false");
        control.textContent = state[item.key] ? "On" : "Off";
        applyState(state);
        saveState(state);
      });

      switchButtons[item.key] = control;
      row.appendChild(label);
      row.appendChild(control);
      panel.appendChild(row);
    });

    /* Reset */
    var reset = document.createElement("button");
    reset.type = "button";
    reset.className = "amw-a11y-reset";
    reset.textContent = "Reset to defaults";
    reset.addEventListener("click", function () {
      state = Object.assign({}, defaults);
      applyState(state);
      saveState(state);
      renderScale();
      Object.keys(switchButtons).forEach(function (key) {
        switchButtons[key].setAttribute("aria-pressed", "false");
        switchButtons[key].textContent = "Off";
      });
    });
    panel.appendChild(reset);

    function openPanel() {
      panel.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      panel.focus();
    }

    function closePanel(refocus) {
      panel.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (refocus) toggle.focus();
    }

    toggle.addEventListener("click", function () {
      if (panel.hidden) {
        openPanel();
      } else {
        closePanel(false);
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        closePanel(true);
      }
    });

    document.addEventListener("click", function (event) {
      if (panel.hidden) return;
      if (panel.contains(event.target) || toggle.contains(event.target)) return;
      closePanel(false);
    });

    document.body.appendChild(toggle);
    document.body.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
