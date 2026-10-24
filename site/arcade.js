/* ==========================================================================
   THE HUMAN WAITING ROOM — an arcade for the party that is not doing the work.
   ==========================================================================

   This product authorizes, meters, and receipts actions taken by agents. The
   human's job during that loop is to wait. This file is what the waiting looks
   like: the page fades out, a cabinet-select screen fades in, and four
   deliberately generic cabinets keep the human occupied while the agents do
   the actual work.

   Contracts this file lives inside:

   - CSP is script-src 'self' with no 'unsafe-inline', so this is a file, not
     an inline <script>, and it injects no <style> or event-handler attributes.
   - The launcher button ships hidden and is revealed only once this script
     runs, so a visitor without JavaScript never sees a control that does
     nothing.
   - Opening the arcade sets display:none on the particle canvas. /wave.js
     already watches that canvas with an IntersectionObserver and unschedules
     its frame loop when it leaves view, so the renderer pauses on open and
     resumes on close without either file knowing about the other.
   - Every cabinet is generic by construction. No arcade trademark, character,
     or company name appears here, and a contract test enforces that.
   - Game-over "receipts" are theatre and say so in their own body text. This
     site's entire pitch is that a real receipt is verifiable, so a fake one
     must be unmistakable at a glance.

   Debug/automation handle: window.__amwArcade — see the bottom of the file.
   ========================================================================== */

(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;

  /* ---- logical screen ----------------------------------------------------

     Everything is authored against a fixed 320x240 field and drawn through an
     integer scale, so sprites stay blocky while text is rasterised at the
     scaled size and stays legible. */
  var W = 320;
  var H = 240;

  /* Sprite labels for the first-person cabinets. Eight characters or fewer:
     the label band is drawn at the sprite's own width and a longer string
     overruns it into the corridor. */
  var LOOSE_CALLS = [
    "tools/*", "refund", "db:drop", "keys:rw", "admin:*", "payout", "prod:ssh", "iam:*"
  ];
  var STEP = 1 / 60; // fixed simulation timestep
  var MAX_CATCHUP = 5; // never simulate more than this many steps per frame

  var PALETTE = {
    bg: "#05070f",
    grid: "#1f2b4a",
    ink: "#e9edf8",
    dim: "#9ca8cb",
    brass: "#f0b43c",
    bright: "#ffd166",
    verify: "#4ade80",
    danger: "#ff7b6b",
    /* Four wall shades for the first-person cabinets, darkest last. A wall
       painted in `ink` (near-white) reads as a light source rather than as
       stone, which is what the corridors looked like before this ramp existed.
       Four steps, not a gradient: a continuous ramp needs colours this palette
       does not have, and banding is how depth was said at this vintage. */
    wall: ["#8b7cd4", "#5f52a8", "#463a86", "#1f2b4a"]
  };

  var BOOT_LINES = [
    "AGENT MIDDLEWARE API — HUMAN WAITING ROOM v1.0",
    "PURPOSE: OCCUPY THE PARTY NOT DOING THE WORK",
    "PROVISIONING ENTERTAINMENT PERMIT ....... GRANTED",
    "  SCOPE: fun:play   TTL: UNTIL YOU GET BORED",
    "METERING JOY AT 0.00 CREDITS PER SMILE",
    "AUDIT: human.entered_waiting_room  ACTOR: YOU",
    "AUDIO ........................ DENIED BY DEFAULT",
    "AGENTS ARE WORKING. YOU ARE NOT ON THE CRITICAL PATH.",
  ];

  /* Rotating status ticker: the whole conceit is that real work is happening
     somewhere else, at a pace nobody will explain to you. */
  var AGENT_STATUS = [
    "AGENT-7 · NEGOTIATING A PERMIT WITH ITSELF",
    "AGENT-3 · METERING SOMETHING. IT WILL NOT SAY WHAT.",
    "AGENT-9 · WRITING A RECEIPT LONGER THAN THE ACTION",
    "AGENT-4 · ARGUING WITH POLICY. POLICY IS WINNING.",
    "AGENT-1 · IDLE, BUT BILLING FOR READINESS",
    "AGENT-6 · RE-READING THE AUDIT LOG FOR PLEASURE",
    "AGENT-2 · WAITING ON AGENT-5",
    "AGENT-5 · WAITING ON AGENT-2",
    "AGENT-8 · ESTIMATED COMPLETION: SOON, PROBABLY, SEE RECEIPT"
  ];

  var PAUSE_LINES = [
    "PAUSED — HUMAN IDLE.",
    "AGENTS UNAFFECTED.",
    "PRESS P TO RESUME BEING BUSY."
  ];

  /* ---- small utilities --------------------------------------------------- */

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function clamp(value, low, high) {
    return value < low ? low : value > high ? high : value;
  }

  /* Signed shortest angle from `b` to `a`, in (-pi, pi].

     JavaScript's `%` keeps the sign of its left operand, so the usual
     "+ 3*PI then % 2*PI" trick silently breaks once the left side goes
     negative — which it does as soon as a heading is left to accumulate past
     3*PI. Written out rather than golfed, because the golfed version reads as
     correct right up until the frame it stops being. */
  function angleDelta(a, b) {
    var d = (a - b) % (Math.PI * 2);
    if (d <= -Math.PI) d += Math.PI * 2;
    else if (d > Math.PI) d -= Math.PI * 2;
    return d;
  }

  /* Seeded generator: a deterministic arcade is a testable arcade. Every run
     starts from a fixed seed unless a caller reseeds it. */
  function makeRandom(seed) {
    var state = seed >>> 0 || 1;
    return function () {
      state = (state * 1664525 + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }

  function prefersStaticMotion() {
    if (root.getAttribute("data-a11y-motion") === "reduce") return true;
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function contrastHigh() {
    return root.getAttribute("data-a11y-contrast") === "high";
  }

  /* ---- fake receipts -----------------------------------------------------

     Deliberately unmistakable. The real artifact lives at /proof/ and is
     verifiable offline; this one announces that it is not. */
  function receiptId(random) {
    var alphabet = "0123456789abcdef";
    var out = "";
    for (var i = 0; i < 12; i += 1) {
      out += alphabet.charAt(Math.floor(random() * alphabet.length));
    }
    return "rcpt_sim_" + out;
  }

  var RECEIPT_VERDICTS = [
    "ALLOWED — NOBODY CHECKED",
    "ALLOWED — POLICY WAS ASLEEP",
    "ALLOWED — RETROACTIVELY, WHICH IS THE BEST KIND",
    "ALLOWED — THE AGENTS VOTED AND YOU LOST"
  ];

  function buildReceipt(cabinet, score, random) {
    var wrap = el("div", "arcade-receipt");
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Simulated receipt");

    wrap.appendChild(el("p", "arcade-receipt-stamp", "SIMULATED · NOT A REAL RECEIPT"));

    var rows = [
      ["receipt_id", receiptId(random)],
      ["action", "arcade.play"],
      ["tool", "cabinet/" + cabinet.id],
      ["units", score + " RECEIPTS SCORED"],
      ["amount", "0.00 CREDITS — HUMAN LABOR IS UNBILLABLE"],
      ["verdict", RECEIPT_VERDICTS[Math.floor(random() * RECEIPT_VERDICTS.length)]],
      ["signature", "—— UNSIGNED · UNVERIFIABLE · THEATRE ——"]
    ];

    var list = el("dl", "arcade-receipt-rows");
    rows.forEach(function (row) {
      list.appendChild(el("dt", null, row[0]));
      list.appendChild(el("dd", null, row[1]));
    });
    wrap.appendChild(list);

    var note = el("p", "arcade-receipt-note");
    note.appendChild(
      doc.createTextNode("This artifact is a prop. Real receipts are signed and verify offline — ")
    );
    var link = el("a", null, "see one at /proof/");
    link.href = "/proof/";
    note.appendChild(link);
    note.appendChild(doc.createTextNode("."));
    wrap.appendChild(note);

    return wrap;
  }

  /* ======================================================================
     CABINET TOOLKIT

     Twenty-five cabinets could each hand-roll their own fillRect calls. A
     hundred cannot: the roster is now large enough that the difference
     between "a game" and "coloured boxes moving" has to come from shared
     machinery rather than from how much patience the author had that day.

     Four things live here, and every cabinet below is built out of them:

       sprites     compact string art, run-length compiled once
       particles   one pooled emitter, no allocation in the frame loop
       fx          shake, flash, hitstop, floating numbers — the "juice"
       backdrops   starfields, parallax bands, horizons, vignettes

     All of it is palette-aware: a sprite names ink keys ("verify", "danger")
     rather than hex, so every cabinet follows the high-contrast switch and
     nobody has to remember to.
     ====================================================================== */

  /* ---- sprites -----------------------------------------------------------

     Authored as rows of single characters. `.` is transparent; every other
     character indexes the palette passed alongside. Compiled once into
     horizontal runs, because a 16x16 sprite drawn cell-by-cell is 256 fill
     calls and drawn as runs is usually fewer than 40 — and at twenty sprites
     a frame that is the difference between a smooth cabinet and a slideshow.

     Palette entries name ink keys where they can ("verify"), and fall back to
     being used literally, so a sprite can still hard-code a colour the ink
     does not carry. */
  function makeSprite(rows, palette) {
    var runs = [];
    var height = rows.length;
    var width = 0;
    for (var y = 0; y < height; y += 1) {
      var row = rows[y];
      if (row.length > width) width = row.length;
      var x = 0;
      while (x < row.length) {
        var ch = row.charAt(x);
        if (ch === "." || ch === " ") { x += 1; continue; }
        var run = 1;
        while (x + run < row.length && row.charAt(x + run) === ch) run += 1;
        runs.push({ x: x, y: y, w: run, key: palette[parseInt(ch, 36)] });
        x += run;
      }
    }
    return {
      w: width,
      h: height,
      runs: runs,
      /* Drawn from a top-left origin in logical pixels. `scale` is a whole
         number by contract — a fractional one lands sprite cells on half
         pixels and the canvas antialiases the edges back into mush. */
      draw: function (ctx, ink, x, y, scale, flip) {
        var s = Math.max(1, Math.round(scale || 1));
        var ox = Math.round(x);
        var oy = Math.round(y);
        for (var i = 0; i < runs.length; i += 1) {
          var r = runs[i];
          ctx.fillStyle = ink[r.key] || r.key;
          var rx = flip ? width - r.x - r.w : r.x;
          ctx.fillRect(ox + rx * s, oy + r.y * s, r.w * s, s);
        }
      },
      /* Centre-origin draw, which is what almost every caller actually
         wants and what everyone kept re-deriving by hand. */
      drawAt: function (ctx, ink, cx, cy, scale, flip) {
        var s = Math.max(1, Math.round(scale || 1));
        this.draw(ctx, ink, cx - (width * s) / 2, cy - (height * s) / 2, s, flip);
      }
    };
  }

  /* A tinted silhouette of a sprite: every run painted one colour. Used for
     shadows, hit flashes, and the "this thing is invulnerable" blink, none of
     which are worth authoring a second sprite for. */
  function drawSilhouette(sprite, ctx, colour, cx, cy, scale) {
    var s = Math.max(1, Math.round(scale || 1));
    ctx.fillStyle = colour;
    var ox = Math.round(cx - (sprite.w * s) / 2);
    var oy = Math.round(cy - (sprite.h * s) / 2);
    for (var i = 0; i < sprite.runs.length; i += 1) {
      var r = sprite.runs[i];
      ctx.fillRect(ox + r.x * s, oy + r.y * s, r.w * s, s);
    }
  }

  /* ---- particles ---------------------------------------------------------

     One fixed pool per cabinet, reused forever. A particle system that
     allocates per emit is the single easiest way to make a 60fps canvas
     stutter every few seconds when the collector runs, and this arcade runs
     a fixed-timestep loop that shows every one of those pauses. */
  function makeParticles(limit) {
    var cap = limit || 96;
    var pool = [];
    for (var i = 0; i < cap; i += 1) {
      pool.push({ live: false, x: 0, y: 0, vx: 0, vy: 0, life: 0, max: 1, colour: "ink", size: 1, gravity: 0 });
    }
    var cursor = 0;

    function take() {
      // Round-robin rather than "find a dead one": at the cap, the oldest
      // particle is the right one to steal, and the search is the expensive
      // part of every naive pool.
      var p = pool[cursor];
      cursor = (cursor + 1) % cap;
      return p;
    }

    return {
      pool: pool,
      burst: function (x, y, count, opts) {
        var o = opts || {};
        for (var i = 0; i < count; i += 1) {
          var p = take();
          var a = o.angle == null ? Math.random() * Math.PI * 2 : o.angle + (Math.random() - 0.5) * (o.spread || 0.8);
          var speed = (o.speed || 40) * (0.4 + Math.random() * 0.9);
          p.live = true;
          p.x = x;
          p.y = y;
          p.vx = Math.cos(a) * speed;
          p.vy = Math.sin(a) * speed;
          p.max = p.life = (o.life || 0.5) * (0.6 + Math.random() * 0.8);
          p.colour = o.colour || "bright";
          p.size = o.size || 2;
          p.gravity = o.gravity || 0;
        }
      },
      update: function (dt) {
        for (var i = 0; i < cap; i += 1) {
          var p = pool[i];
          if (!p.live) continue;
          p.life -= dt;
          if (p.life <= 0) { p.live = false; continue; }
          p.vy += p.gravity * dt;
          p.x += p.vx * dt;
          p.y += p.vy * dt;
        }
      },
      draw: function (ctx, ink) {
        for (var i = 0; i < cap; i += 1) {
          var p = pool[i];
          if (!p.live) continue;
          // Shrinking rather than fading: this palette has no alpha to spend
          // and a particle that ends as a single pixel reads as a spark.
          var k = p.life / p.max;
          var size = Math.max(1, Math.round(p.size * (k > 0.5 ? 1 : k * 2)));
          ctx.fillStyle = ink[p.colour] || p.colour;
          ctx.fillRect(Math.round(p.x) - (size >> 1), Math.round(p.y) - (size >> 1), size, size);
        }
      },
      clear: function () {
        for (var i = 0; i < cap; i += 1) pool[i].live = false;
      }
    };
  }

  /* ---- juice -------------------------------------------------------------

     Screen shake, colour flashes, hitstop and floating numbers. These are the
     four effects that separate a cabinet that responds from one that merely
     updates, and all four are two lines each once they live somewhere shared.

     Every one of them is suppressed under reduced motion: shake in particular
     is the kind of involuntary movement that preference exists to
     stop, and a cabinet is still perfectly playable without it. */
  function makeFx() {
    var shakeAmount = 0;
    var flashAmount = 0;
    var flashColour = "bright";
    var stop = 0;
    var pops = [];
    var still = prefersStaticMotion();

    return {
      /* True while hitstop is holding the simulation. A cabinet checks this
         at the top of update and returns early — which is what makes a hit
         feel like it landed rather than like it was tallied. */
      frozen: function () { return stop > 0; },
      shake: function (amount) { if (!still) shakeAmount = Math.max(shakeAmount, amount); },
      flash: function (colour, amount) {
        flashColour = colour || "bright";
        flashAmount = Math.max(flashAmount, amount == null ? 1 : amount);
      },
      freeze: function (seconds) { if (!still) stop = Math.max(stop, seconds); },
      pop: function (x, y, text, colour) {
        pops.push({ x: x, y: y, text: String(text), colour: colour || "bright", life: 0.8 });
        if (pops.length > 12) pops.shift();
      },
      update: function (dt) {
        if (stop > 0) stop -= dt;
        shakeAmount = Math.max(0, shakeAmount - dt * 26);
        flashAmount = Math.max(0, flashAmount - dt * 4);
        for (var i = pops.length - 1; i >= 0; i -= 1) {
          pops[i].life -= dt;
          pops[i].y -= 22 * dt;
          if (pops[i].life <= 0) pops.splice(i, 1);
        }
      },
      /* Wraps the cabinet's own drawing. save/restore rather than an
         un-translate, so a cabinet that leaves a transform on the context
         cannot leak it into the next frame. */
      begin: function (ctx) {
        ctx.save();
        if (shakeAmount > 0.2) {
          ctx.translate(
            Math.round((Math.random() - 0.5) * shakeAmount),
            Math.round((Math.random() - 0.5) * shakeAmount)
          );
        }
      },
      end: function (ctx, ink) {
        ctx.restore();
        for (var i = 0; i < pops.length; i += 1) {
          var p = pops[i];
          ctx.font = '8px "IBM Plex Mono", monospace';
          ctx.textAlign = "center";
          ctx.fillStyle = ink[p.colour] || p.colour;
          ctx.fillText(p.text, Math.round(p.x), Math.round(p.y));
          ctx.textAlign = "left";
        }
        if (flashAmount > 0.02) {
          // A band top and bottom rather than a full-screen wash: a filled
          // rectangle over the whole field at this palette hides the game,
          // and the bands read as the same event.
          var h = Math.max(1, Math.round(flashAmount * 5));
          ctx.fillStyle = ink[flashColour] || flashColour;
          ctx.fillRect(0, 0, W, h);
          ctx.fillRect(0, H - h, W, h);
        }
      },
      reset: function () {
        shakeAmount = 0;
        flashAmount = 0;
        stop = 0;
        pops.length = 0;
        still = prefersStaticMotion();
      }
    };
  }

  /* ---- backdrops ---------------------------------------------------------

     Four reusable grounds. A cabinet drawn on flat `bg` reads as a prototype
     no matter how good its sprites are, and these cost one call each. */

  function drawStarfield(ctx, ink, scroll, density) {
    // Deterministic from the cell index rather than from a stored array: the
    // field is infinite, costs nothing to seek, and is identical every run.
    var count = density || 40;
    for (var i = 0; i < count; i += 1) {
      var seed = i * 2654435761 % 2147483647;
      var x = (seed % W + W - (scroll * (0.3 + (i % 3) * 0.35)) % W) % W;
      var y = (seed >> 8) % H;
      ctx.fillStyle = i % 5 === 0 ? ink.ink : i % 3 === 0 ? ink.dim : ink.grid;
      ctx.fillRect(Math.floor(x), y, i % 7 === 0 ? 2 : 1, 1);
    }
  }

  function drawParallaxBands(ctx, ink, scroll, horizon) {
    var base = horizon == null ? H * 0.55 : horizon;
    ctx.fillStyle = ink.grid;
    for (var b = -1; b < 12; b += 1) {
      var height = 18 + (b % 4) * 13;
      var x = b * 46 - (scroll * 0.25) % 46;
      ctx.fillRect(x, base - height, 30, height);
    }
    ctx.fillStyle = ink.wall[3];
    for (var f = -1; f < 9; f += 1) {
      var fh = 10 + (f % 3) * 8;
      var fx = f * 62 - (scroll * 0.6) % 62;
      ctx.fillRect(fx, base - fh, 40, fh);
    }
  }

  function drawHorizonGrid(ctx, ink, scroll) {
    var horizon = H * 0.42;
    ctx.fillStyle = ink.bg;
    ctx.fillRect(0, 0, W, horizon);
    ctx.fillStyle = ink.wall[3];
    ctx.fillRect(0, horizon, W, H - horizon);
    ctx.fillStyle = ink.grid;
    // Perspective rows: spacing grows with the square of the distance below
    // the horizon, which is the cheap trick that sells depth at this budget.
    for (var i = 1; i < 12; i += 1) {
      var t = (i + (scroll % 1)) / 12;
      var y = horizon + (H - horizon) * t * t;
      ctx.fillRect(0, Math.floor(y), W, 1);
    }
    for (var c = -6; c <= 6; c += 1) {
      var topX = W / 2 + c * 6;
      var bottomX = W / 2 + c * 54;
      for (var s = 0; s < 20; s += 1) {
        var k = s / 20;
        var py = horizon + (H - horizon) * k * k;
        var px = topX + (bottomX - topX) * k * k;
        ctx.fillRect(Math.floor(px), Math.floor(py), 1, 2);
      }
    }
  }

  /* A chunky vignette: 8px blocks darkened toward the edges. Sells "screen"
     rather than "web page" and costs one pass of about 120 fills. */
  function drawVignette(ctx, ink, strength) {
    var k = strength == null ? 1 : strength;
    ctx.fillStyle = ink.bg;
    for (var y = 0; y < H; y += 8) {
      for (var x = 0; x < W; x += 8) {
        var dx = (x + 4 - W / 2) / (W / 2);
        var dy = (y + 4 - H / 2) / (H / 2);
        var d = dx * dx + dy * dy;
        if (d * k < 0.95) continue;
        ctx.fillRect(x, y, 8, 8);
      }
    }
  }

  /* ---- small shared helpers ---------------------------------------------- */

  function centreText(ctx, ink, text, y, colour, size) {
    ctx.font = (size || 8) + 'px "IBM Plex Mono", monospace';
    ctx.textAlign = "center";
    ctx.fillStyle = ink[colour] || colour || ink.ink;
    ctx.fillText(text, W / 2, y);
    ctx.textAlign = "left";
  }

  function drawBar(ctx, ink, x, y, w, h, fraction, colour) {
    ctx.fillStyle = ink.grid;
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = ink[colour] || colour || ink.verify;
    ctx.fillRect(x, y, Math.max(0, Math.round(w * clamp(fraction, 0, 1))), h);
  }

  function drawPanel(ctx, ink, x, y, w, h) {
    ctx.fillStyle = ink.bg;
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = ink.grid;
    ctx.fillRect(x, y, w, 1);
    ctx.fillRect(x, y + h - 1, w, 1);
    ctx.fillRect(x, y, 1, h);
    ctx.fillRect(x + w - 1, y, 1, h);
  }

  /* Rising difficulty that never divides by zero and never runs away: used by
     most of the roster so "level 40" is hard rather than impossible. */
  function ramp(level, base, step, ceiling) {
    return Math.min(ceiling, base + (level - 1) * step);
  }

  /* ======================================================================
     CABINETS

     Each cabinet is a factory returning an object with reset/update/draw and
     the fields the shell reads: score, lives, over, hud().
     ====================================================================== */

  /* ---- 1. SCOPE CREEP ----------------------------------------------------

     A descending grid of permission scopes marches toward PRODUCTION. You
     hold a deny key. Every scope you deny makes the survivors faster, and a
     consultant occasionally drifts past to add one more requirement, which is
     the joke and also the documented behaviour. */
  function cabinetScopeCreep(random) {
    // Labels are kept to eight characters and the columns spaced wider than
    // the widest of them: at 8px monospace a nine-character scope overruns its
    // neighbour and the grid reads as mush.
    var SCOPES = [
      "*:*", "admin:*", "db:drop", "bill:*", "iam:*",
      "prod:ssh", "keys:rw", "audit:w", "refund:*", "fs:rm-rf"
    ];
    var COLS = 6;
    var ROWS = 4;
    var COL_STEP = 52;
    var COL_ORIGIN = 34;

    var game = { id: "scope-creep", score: 0, lives: 3, over: false };
    var scopes, player, shots, bombs, dir, speed, wave, consultant, cooldown, message, messageAge;

    function spawnWave() {
      scopes = [];
      for (var r = 0; r < ROWS; r += 1) {
        for (var c = 0; c < COLS; c += 1) {
          scopes.push({
            x: COL_ORIGIN + c * COL_STEP,
            y: 34 + r * 22,
            label: SCOPES[(r * COLS + c) % SCOPES.length],
            alive: true,
            value: (ROWS - r) * 10
          });
        }
      }
      dir = 1;
      speed = 10 + wave * 3;
      bombs = [];
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      wave = 1;
      shots = [];
      consultant = null;
      cooldown = 0;
      message = "";
      messageAge = 0;
      player = { x: W / 2, w: 22 };
      spawnWave();
    };

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function liveScopes() {
      return scopes.filter(function (s) {
        return s.alive;
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;

      if (input.left) player.x -= 130 * dt;
      if (input.right) player.x += 130 * dt;
      if (input.pointerX != null) player.x = input.pointerX;
      player.x = clamp(player.x, player.w / 2, W - player.w / 2);

      cooldown -= dt;
      if (input.fire && cooldown <= 0) {
        shots.push({ x: player.x, y: H - 26 });
        cooldown = 0.28;
      }

      shots.forEach(function (shot) {
        shot.y -= 190 * dt;
      });
      shots = shots.filter(function (shot) {
        return shot.y > -6;
      });

      var living = liveScopes();
      if (!living.length) {
        wave += 1;
        say("WAVE CLEARED. REQUIREMENTS GATHERED ANYWAY.");
        spawnWave();
        living = liveScopes();
      }

      // Classic side-step march: drift until the block touches an edge, then
      // drop a row and reverse. Speed scales with how few are left, so the
      // last scope is always the fastest — as in life.
      var pace = speed * (1 + (ROWS * COLS - living.length) / (ROWS * COLS) * 2.2);
      var minX = W;
      var maxX = 0;
      living.forEach(function (s) {
        s.x += dir * pace * dt;
        if (s.x < minX) minX = s.x;
        if (s.x > maxX) maxX = s.x;
      });
      // Labels are centred, so the turn margin has to clear half a label or
      // the widest scopes clip against the bezel.
      if (maxX > W - 24 || minX < 24) {
        dir *= -1;
        living.forEach(function (s) {
          s.y += 10;
          s.x = clamp(s.x, 24, W - 24);
        });
      }

      // Escalation bombs.
      if (random() < dt * (0.8 + wave * 0.25) && living.length) {
        var shooter = living[Math.floor(random() * living.length)];
        bombs.push({ x: shooter.x, y: shooter.y + 6 });
      }
      bombs.forEach(function (bomb) {
        bomb.y += 96 * dt;
      });

      // The consultant: pure overhead, worth points, adds one scope on death.
      if (!consultant && random() < dt * 0.22) {
        consultant = { x: -16, y: 20, dir: 1 };
      }
      if (consultant) {
        consultant.x += 62 * dt * consultant.dir;
        if (consultant.x > W + 16) consultant = null;
      }

      // Collisions: shots vs scopes.
      shots.forEach(function (shot) {
        living.forEach(function (s) {
          if (
            s.alive &&
            Math.abs(shot.x - s.x) < 15 &&
            Math.abs(shot.y - s.y) < 9
          ) {
            s.alive = false;
            shot.y = -99;
            game.score += s.value;
          }
        });
        if (
          consultant &&
          Math.abs(shot.x - consultant.x) < 16 &&
          Math.abs(shot.y - consultant.y) < 9
        ) {
          game.score += 150;
          shot.y = -99;
          consultant = null;
          var extra = liveScopes()[0];
          if (extra) {
            scopes.push({
              x: clamp(extra.x + 20, 24, W - 24),
              y: extra.y,
              label: SCOPES[Math.floor(random() * SCOPES.length)],
              alive: true,
              value: 10
            });
          }
          say("CONSULTANT DENIED. ONE MORE REQUIREMENT ADDED.");
        }
      });
      shots = shots.filter(function (shot) {
        return shot.y > -6;
      });

      // Bombs vs player.
      bombs.forEach(function (bomb) {
        if (
          bomb.y > H - 30 &&
          bomb.y < H - 14 &&
          Math.abs(bomb.x - player.x) < player.w / 2 + 3
        ) {
          bomb.y = H + 99;
          game.lives -= 1;
          say("PRIVILEGE ESCALATED THROUGH YOUR CHEST.");
          if (game.lives <= 0) game.over = true;
        }
      });
      bombs = bombs.filter(function (bomb) {
        return bomb.y < H + 8;
      });

      // Anything reaching production ends the run outright.
      var breached = liveScopes().some(function (s) {
        return s.y > H - 40;
      });
      if (breached) {
        game.lives = 0;
        game.over = true;
        say("SCOPE REACHED PRODUCTION.");
      }

      messageAge += dt;
      if (messageAge > 2.6) message = "";
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.danger;
      ctx.fillRect(0, H - 34, W, 1);
      ctx.fillStyle = ink.dim;
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.fillText("PRODUCTION", 4, H - 37);

      ctx.font = '8px "IBM Plex Mono", monospace';
      liveScopes().forEach(function (s) {
        ctx.fillStyle = s.value > 30 ? ink.danger : ink.brass;
        ctx.textAlign = "center";
        ctx.fillText(s.label, s.x, s.y);
      });

      if (consultant) {
        ctx.fillStyle = ink.verify;
        ctx.textAlign = "center";
        ctx.fillText("[CONSULTANT]", consultant.x, consultant.y);
      }

      ctx.textAlign = "left";
      ctx.fillStyle = ink.bright;
      shots.forEach(function (shot) {
        ctx.fillRect(shot.x - 1, shot.y - 5, 2, 6);
      });
      ctx.fillStyle = ink.danger;
      bombs.forEach(function (bomb) {
        ctx.fillRect(bomb.x - 1, bomb.y, 2, 5);
      });

      ctx.fillStyle = ink.ink;
      ctx.fillRect(player.x - player.w / 2, H - 24, player.w, 6);
      ctx.fillRect(player.x - 2, H - 29, 4, 5);

      if (message) {
        ctx.fillStyle = ink.dim;
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, H - 6);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "WAVE " + wave;
    };

    return game;
  }

  /* ---- 2. TOKEN BUCKET ---------------------------------------------------

     Paddle-and-ball against a bucket of tokens. The bucket refills on a timer,
     so the board can never be permanently cleared: you are not playing to win,
     you are playing to stay under the limit, forever. */
  function cabinetTokenBucket(random) {
    var COLS = 10;
    var game = { id: "token-bucket", score: 0, lives: 3, over: false };
    var bricks, paddle, ball, refillTimer, refills, message, messageAge, launched;

    function makeRow(y) {
      var row = [];
      for (var c = 0; c < COLS; c += 1) {
        row.push({ x: 8 + c * 30, y: y, w: 26, h: 8, alive: true });
      }
      return row;
    }

    function resetBall() {
      ball = { x: W / 2, y: H - 46, vx: 0, vy: 0, r: 2 };
      launched = false;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      bricks = [];
      for (var r = 0; r < 4; r += 1) {
        bricks = bricks.concat(makeRow(30 + r * 12));
      }
      paddle = { x: W / 2, w: 42 };
      refillTimer = 9;
      refills = 0;
      message = "";
      messageAge = 0;
      resetBall();
    };

    function say(text) {
      message = text;
      messageAge = 0;
    }

    game.update = function (dt, input) {
      if (game.over) return;

      if (input.left) paddle.x -= 165 * dt;
      if (input.right) paddle.x += 165 * dt;
      if (input.pointerX != null) paddle.x = input.pointerX;
      paddle.x = clamp(paddle.x, paddle.w / 2, W - paddle.w / 2);

      if (!launched) {
        ball.x = paddle.x;
        ball.y = H - 26;
        if (input.fire) {
          launched = true;
          ball.vx = (random() < 0.5 ? -1 : 1) * 70;
          ball.vy = -128;
        }
        return;
      }

      ball.x += ball.vx * dt;
      ball.y += ball.vy * dt;

      if (ball.x < 3) {
        ball.x = 3;
        ball.vx = Math.abs(ball.vx);
      }
      if (ball.x > W - 3) {
        ball.x = W - 3;
        ball.vx = -Math.abs(ball.vx);
      }
      if (ball.y < 20) {
        ball.y = 20;
        ball.vy = Math.abs(ball.vy);
      }

      // Paddle: contact point steers, so the rate limiter has some say.
      if (
        ball.vy > 0 &&
        ball.y > H - 24 &&
        ball.y < H - 16 &&
        Math.abs(ball.x - paddle.x) < paddle.w / 2 + 2
      ) {
        ball.y = H - 24;
        ball.vy = -Math.abs(ball.vy);
        ball.vx = clamp((ball.x - paddle.x) / (paddle.w / 2), -1, 1) * 130;
      }

      // One bounce per frame no matter how many tokens the ball overlaps —
      // flipping once per brick can cancel itself out and tunnel the ball.
      var bounced = false;
      bricks.forEach(function (brick) {
        if (!brick.alive) return;
        if (
          ball.x > brick.x - 2 &&
          ball.x < brick.x + brick.w + 2 &&
          ball.y > brick.y - 2 &&
          ball.y < brick.y + brick.h + 2
        ) {
          brick.alive = false;
          game.score += 15;
          bounced = true;
        }
      });
      if (bounced) ball.vy *= -1;

      if (ball.y > H - 6) {
        game.lives -= 1;
        say("REQUEST DROPPED. 429.");
        resetBall();
        if (game.lives <= 0) game.over = true;
        return;
      }

      // The refill. The bucket is never empty for long, which is the entire
      // point of a bucket.
      refillTimer -= dt;
      if (refillTimer <= 0) {
        refills += 1;
        refillTimer = Math.max(4.5, 9 - refills * 0.6);
        bricks.forEach(function (brick) {
          brick.y += 12;
        });
        bricks = bricks.concat(makeRow(30));
        say("BUCKET REFILLED. IT ALWAYS REFILLS.");
        var flooded = bricks.some(function (brick) {
          return brick.alive && brick.y > H - 40;
        });
        if (flooded) {
          game.over = true;
          say("BUCKET OVERFLOWED INTO PRODUCTION.");
        }
      }

      messageAge += dt;
      if (messageAge > 2.4) message = "";
    };

    game.draw = function (ctx, ink) {
      bricks.forEach(function (brick) {
        if (!brick.alive) return;
        ctx.fillStyle = brick.y > H - 70 ? ink.danger : ink.brass;
        ctx.fillRect(brick.x, brick.y, brick.w, brick.h);
        ctx.fillStyle = ink.bg;
        ctx.font = '6px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText("TKN", brick.x + brick.w / 2, brick.y + 6);
      });

      ctx.textAlign = "left";
      ctx.fillStyle = ink.ink;
      ctx.fillRect(paddle.x - paddle.w / 2, H - 20, paddle.w, 5);
      ctx.fillRect(ball.x - ball.r, ball.y - ball.r, ball.r * 2, ball.r * 2);

      ctx.fillStyle = ink.dim;
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      if (!launched) {
        ctx.fillText("SPACE TO ISSUE A REQUEST", W / 2, H - 32);
      } else if (message) {
        ctx.fillText(message, W / 2, H - 6);
      }
      ctx.textAlign = "left";
    };

    game.hud = function () {
      return "REFILL " + Math.max(0, Math.ceil(refillTimer)) + "s";
    };

    return game;
  }

  /* ---- 3. APPEND-ONLY ----------------------------------------------------

     A ledger that grows and may never cross itself, because crossing itself
     would be rewriting history. Vacuum requests appear; they are refused, and
     refusing them still makes the ledger longer. */
  function cabinetAppendOnly(random) {
    var CELL = 8;
    var COLS = Math.floor(W / CELL);
    // Leave a clear band under the grid: the legend is drawn at the bottom of
    // the screen and a full-height field would run underneath it.
    var ROWS = Math.floor((H - 36) / CELL);
    var game = { id: "append-only", score: 0, lives: 1, over: false };
    var body, dir, nextDir, entry, vacuum, timer, rate, message, messageAge;

    function placeCell(avoid) {
      for (var attempt = 0; attempt < 200; attempt += 1) {
        var cell = {
          x: Math.floor(random() * COLS),
          y: Math.floor(random() * ROWS)
        };
        var clash = avoid.some(function (part) {
          return part.x === cell.x && part.y === cell.y;
        });
        if (!clash) return cell;
      }
      return { x: 0, y: 0 };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 1;
      game.over = false;
      body = [
        { x: 6, y: Math.floor(ROWS / 2) },
        { x: 5, y: Math.floor(ROWS / 2) },
        { x: 4, y: Math.floor(ROWS / 2) }
      ];
      dir = { x: 1, y: 0 };
      nextDir = { x: 1, y: 0 };
      entry = placeCell(body);
      vacuum = null;
      timer = 0;
      rate = 0.14;
      message = "";
      messageAge = 0;
    };

    function say(text) {
      message = text;
      messageAge = 0;
    }

    game.update = function (dt, input) {
      if (game.over) return;

      // Queue the turn rather than applying it mid-cell: a 180 into your own
      // neck should be refused, not fatal.
      if (input.left && dir.x === 0) nextDir = { x: -1, y: 0 };
      if (input.right && dir.x === 0) nextDir = { x: 1, y: 0 };
      if (input.up && dir.y === 0) nextDir = { x: 0, y: -1 };
      if (input.down && dir.y === 0) nextDir = { x: 0, y: 1 };

      messageAge += dt;
      if (messageAge > 2.4) message = "";

      timer += dt;
      if (timer < rate) return;
      timer = 0;
      dir = nextDir;

      var head = {
        x: body[0].x + dir.x,
        y: body[0].y + dir.y
      };

      if (head.x < 0 || head.y < 0 || head.x >= COLS || head.y >= ROWS) {
        game.over = true;
        say("LEDGER LEFT THE JURISDICTION.");
        return;
      }

      var selfHit = body.some(function (part) {
        return part.x === head.x && part.y === head.y;
      });
      if (selfHit) {
        game.over = true;
        game.lives = 0;
        say("HISTORY REWRITTEN. THAT IS NOT PERMITTED.");
        return;
      }

      body.unshift(head);

      if (entry && head.x === entry.x && head.y === entry.y) {
        game.score += 25;
        entry = placeCell(body);
        rate = Math.max(0.06, rate - 0.004);
        if (!vacuum && random() < 0.35) vacuum = placeCell(body.concat([entry]));
      } else if (vacuum && head.x === vacuum.x && head.y === vacuum.y) {
        // The refusal is the feature. It also costs you, because refusals are
        // themselves appended.
        vacuum = null;
        game.score += 5;
        say("VACUUM DENIED: THE LEDGER IS APPEND-ONLY.");
        body.push({ x: body[body.length - 1].x, y: body[body.length - 1].y });
      } else {
        body.pop();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.grid;
      for (var gx = 0; gx < COLS; gx += 4) {
        ctx.fillRect(gx * CELL, 20, 1, ROWS * CELL);
      }

      if (entry) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(entry.x * CELL + 1, 20 + entry.y * CELL + 1, CELL - 2, CELL - 2);
      }
      if (vacuum) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(vacuum.x * CELL + 1, 20 + vacuum.y * CELL + 1, CELL - 2, CELL - 2);
      }

      body.forEach(function (part, index) {
        ctx.fillStyle = index === 0 ? ink.bright : ink.brass;
        ctx.fillRect(part.x * CELL + 1, 20 + part.y * CELL + 1, CELL - 2, CELL - 2);
      });

      ctx.fillStyle = ink.dim;
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillText(message || "GREEN = ENTRY · RED = VACUUM REQUEST", W / 2, H - 4);
      ctx.textAlign = "left";
    };

    game.hud = function () {
      return "ENTRIES " + body.length;
    };

    return game;
  }

  /* ---- 4. RACE CONDITION -------------------------------------------------

     A rally against an agent that does not blink and occasionally applies
     eventual consistency to its own paddle position. First to seven. */
  function cabinetRaceCondition(random) {
    var TARGET = 7;
    var TAUNTS = [
      "I HAVE ALREADY WON. THE RECEIPT IS PENDING.",
      "YOUR MOVE WAS VALID. IT WAS ALSO LATE.",
      "I READ YOUR PADDLE FROM A STALE REPLICA.",
      "THIS RALLY IS IDEMPOTENT. YOU STILL LOSE ONCE.",
      "I DO NOT BLINK. BLINKING IS UNAUDITED."
    ];

    var game = { id: "race-condition", score: 0, lives: 1, over: false };
    var you, them, ball, theirScore, taunt, tauntAge, teleportTimer;

    function serve(towardYou) {
      ball = {
        x: W / 2,
        y: H / 2,
        vx: towardYou ? -95 : 95,
        vy: (random() * 2 - 1) * 60
      };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 1;
      game.over = false;
      you = { y: H / 2, h: 34 };
      them = { y: H / 2, h: 34 };
      theirScore = 0;
      taunt = "";
      tauntAge = 0;
      teleportTimer = 3.5;
      serve(random() < 0.5);
    };

    function say(text) {
      taunt = text;
      tauntAge = 0;
    }

    game.update = function (dt, input) {
      if (game.over) return;

      if (input.up) you.y -= 155 * dt;
      if (input.down) you.y += 155 * dt;
      if (input.pointerY != null) you.y = input.pointerY;
      you.y = clamp(you.y, 22 + you.h / 2, H - 8 - you.h / 2);

      // The opponent tracks honestly, then cheats on a timer and files the
      // cheat under a respectable word.
      var chase = ball.y - them.y;
      them.y += clamp(chase, -118 * dt, 118 * dt);
      teleportTimer -= dt;
      if (teleportTimer <= 0 && ball.vx > 0) {
        teleportTimer = 3.2 + random() * 2.4;
        them.y = ball.y;
        say("EVENTUAL CONSISTENCY APPLIED.");
      }
      them.y = clamp(them.y, 22 + them.h / 2, H - 8 - them.h / 2);

      ball.x += ball.vx * dt;
      ball.y += ball.vy * dt;
      if (ball.y < 24) {
        ball.y = 24;
        ball.vy = Math.abs(ball.vy);
      }
      if (ball.y > H - 10) {
        ball.y = H - 10;
        ball.vy = -Math.abs(ball.vy);
      }

      if (ball.vx < 0 && ball.x < 16 && Math.abs(ball.y - you.y) < you.h / 2 + 3) {
        ball.x = 16;
        ball.vx = Math.abs(ball.vx) * 1.05;
        ball.vy += clamp((ball.y - you.y) / (you.h / 2), -1, 1) * 45;
      }
      if (
        ball.vx > 0 &&
        ball.x > W - 16 &&
        Math.abs(ball.y - them.y) < them.h / 2 + 3
      ) {
        ball.x = W - 16;
        ball.vx = -Math.abs(ball.vx) * 1.05;
        ball.vy += clamp((ball.y - them.y) / (them.h / 2), -1, 1) * 45;
      }

      if (ball.x < 4) {
        theirScore += 1;
        say(TAUNTS[Math.floor(random() * TAUNTS.length)]);
        serve(false);
      } else if (ball.x > W - 4) {
        game.score += 1;
        say("CONFLICT RESOLVED IN YOUR FAVOUR. LOGGED RELUCTANTLY.");
        serve(true);
      }

      if (game.score >= TARGET || theirScore >= TARGET) {
        game.over = true;
        game.lives = 0;
        say(
          game.score >= TARGET
            ? "YOU WON. THE AGENTS WILL REVIEW THE FOOTAGE."
            : "AGENT-7 WON. IT WAS ALWAYS GOING TO."
        );
      }

      tauntAge += dt;
      if (tauntAge > 3.2) taunt = "";
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.grid;
      for (var y = 24; y < H - 6; y += 10) {
        ctx.fillRect(W / 2 - 1, y, 2, 5);
      }

      ctx.fillStyle = ink.ink;
      ctx.fillRect(8, you.y - you.h / 2, 4, you.h);
      ctx.fillStyle = ink.danger;
      ctx.fillRect(W - 12, them.y - them.h / 2, 4, them.h);

      ctx.fillStyle = ink.bright;
      ctx.fillRect(ball.x - 2, ball.y - 2, 4, 4);

      ctx.font = '9px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = ink.ink;
      ctx.fillText(String(game.score), W / 2 - 26, 32);
      ctx.fillStyle = ink.danger;
      ctx.fillText(String(theirScore), W / 2 + 26, 32);

      if (taunt) {
        ctx.fillStyle = ink.dim;
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillText(taunt, W / 2, H - 4);
      }
      ctx.textAlign = "left";
    };

    game.hud = function () {
      return "AGENT-7 " + theirScore + " / " + TARGET;
    };

    return game;
  }

  /* ======================================================================
     RAYCAST ENGINE

     Shared by the two first-person cabinets. A grid map, a DDA per column,
     and billboarded sprites — the 1992 technique, which is the only one that
     fits this canvas and this palette. Nothing here is 3D: every wall is an
     axis-aligned unit square, so a column's height is one divide.

     Columns are drawn COLUMN_W wide rather than one pixel each. That is two
     things at once: a quarter of the fill calls, and the chunky vertical banding
     the rest of the design system is built out of. A one-pixel column on a
     320-wide canvas would render a smooth wall, which would look wrong here.
     ====================================================================== */

  var COLUMN_W = 4;
  var FOV = 0.66; // camera plane half-width; ~60 degrees horizontal

  function makeCaster(mapRows) {
    /* Solids are named explicitly and everything else is open floor. The
       inverse — defaulting to solid — silently turns a level that uses any
       other character for floor into a block of stone, which the cabinet then
       reports as "cleared" on its first frame because nothing can spawn in it.
         #  wall
         =  boundary marker (verify-tinted)
         !  hot wall (danger-tinted)
         anything else  open floor */
    var map = mapRows.map(function (row) {
      return row.split("").map(function (ch) {
        return ch === "#" ? 1 : ch === "=" ? 2 : ch === "!" ? 3 : 0;
      });
    });
    var MH = map.length;
    var MW = map[0].length;

    function solid(x, y) {
      var cx = Math.floor(x);
      var cy = Math.floor(y);
      if (cx < 0 || cy < 0 || cx >= MW || cy >= MH) return 1;
      return map[cy][cx];
    }

    /* Walls are stepped through in grid units (DDA) rather than sampled at a
       fixed distance: sampling misses thin walls at grazing angles and costs
       more the further the ray travels. Returns the perpendicular distance —
       not the euclidean one, which would fisheye the view. */
    function cast(px, py, rdx, rdy) {
      var mapX = Math.floor(px);
      var mapY = Math.floor(py);
      var deltaX = rdx === 0 ? 1e30 : Math.abs(1 / rdx);
      var deltaY = rdy === 0 ? 1e30 : Math.abs(1 / rdy);
      var stepX, stepY, sideX, sideY;

      if (rdx < 0) { stepX = -1; sideX = (px - mapX) * deltaX; }
      else { stepX = 1; sideX = (mapX + 1 - px) * deltaX; }
      if (rdy < 0) { stepY = -1; sideY = (py - mapY) * deltaY; }
      else { stepY = 1; sideY = (mapY + 1 - py) * deltaY; }

      var side = 0;
      var tile = 0;
      // Bounded rather than while(true): a ray that escapes the map through a
      // gap would otherwise walk forever on a malformed level.
      for (var guard = 0; guard < 64; guard += 1) {
        if (sideX < sideY) { sideX += deltaX; mapX += stepX; side = 0; }
        else { sideY += deltaY; mapY += stepY; side = 1; }
        if (mapX < 0 || mapY < 0 || mapX >= MW || mapY >= MH) { tile = 1; break; }
        tile = map[mapY][mapX];
        if (tile) break;
      }
      var dist = side === 0
        ? (mapX - px + (1 - stepX) / 2) / (rdx || 1e-30)
        : (mapY - py + (1 - stepY) / 2) / (rdy || 1e-30);
      return { dist: Math.max(0.0001, dist), side: side, tile: tile || 1 };
    }

    return { map: map, w: MW, h: MH, solid: solid, cast: cast };
  }

  /* Move with a wall slide: each axis is tested on its own so a player who
     walks into a corner slides along it instead of sticking. Sticking in a
     corridor shooter reads as a broken control, not as a wall. */
  function casterMove(caster, ent, dx, dy) {
    var pad = 0.18;
    if (!caster.solid(ent.x + dx + (dx > 0 ? pad : -pad), ent.y)) ent.x += dx;
    if (!caster.solid(ent.x, ent.y + dy + (dy > 0 ? pad : -pad))) ent.y += dy;
  }

  /* Renders walls into ctx and returns the per-column depth buffer, which the
     sprite pass needs to avoid drawing enemies through walls. */
  function casterDrawWalls(ctx, ink, caster, cam) {
    var zbuf = [];
    var planeX = -cam.dirY * FOV;
    var planeY = cam.dirX * FOV;
    var horizon = H / 2;

    // Ceiling and floor as two flat bands. A textured floor would cost more
    // than the whole rest of the frame and read as noise at this palette.
    ctx.fillStyle = ink.bg;
    ctx.fillRect(0, 0, W, horizon);
    ctx.fillStyle = ink.grid;
    ctx.fillRect(0, horizon, W, H - horizon);

    for (var x = 0; x < W; x += COLUMN_W) {
      var camX = 2 * (x + COLUMN_W / 2) / W - 1;
      var hit = caster.cast(cam.x, cam.y, cam.dirX + planeX * camX, cam.dirY + planeY * camX);
      var lineH = Math.floor(H / hit.dist);
      var top = Math.floor(horizon - lineH / 2);

      // Four depth bands instead of a continuous ramp: a smooth gradient needs
      // colours this palette does not have, and banding is the period-correct
      // way to say "further away".
      var shade;
      if (hit.tile === 2) shade = hit.side ? ink.verify : ink.bright;
      else if (hit.tile === 3) shade = hit.side ? ink.danger : ink.brass;
      else {
        var band = hit.dist < 2.5 ? 0 : hit.dist < 5 ? 1 : hit.dist < 8 ? 2 : 3;
        // A wall seen edge-on drops one band, which is what separates a
        // corner from a flat run at this palette depth.
        if (hit.side) band = Math.min(3, band + 1);
        shade = ink.wall[band];
      }

      ctx.fillStyle = shade;
      ctx.fillRect(x, Math.max(0, top), COLUMN_W, Math.min(H, lineH));
      for (var c = 0; c < COLUMN_W; c += 1) zbuf[x + c] = hit.dist;
    }
    return zbuf;
  }

  /* Billboarded sprites: transform into camera space, reject anything behind
     the plane, then draw as a stack of chunky columns so a sprite occluded by
     a wall corner is clipped per column rather than all-or-nothing. */
  function casterDrawSprites(ctx, ink, cam, sprites, zbuf) {
    var planeX = -cam.dirY * FOV;
    var planeY = cam.dirX * FOV;
    var inv = 1 / (planeX * cam.dirY - cam.dirX * planeY);

    sprites
      .map(function (s) {
        var relX = s.x - cam.x;
        var relY = s.y - cam.y;
        return {
          s: s,
          tx: inv * (cam.dirY * relX - cam.dirX * relY),
          ty: inv * (-planeY * relX + planeX * relY)
        };
      })
      .filter(function (p) { return p.ty > 0.25; })
      .sort(function (a, b) { return b.ty - a.ty; })
      .forEach(function (p) {
        var screenX = Math.floor((W / 2) * (1 + p.tx / p.ty));
        var size = Math.abs(Math.floor(H / p.ty)) * (p.s.scale || 0.7);
        var top = Math.floor(H / 2 - size / 2 + (p.s.lift || 0) / p.ty);
        var left = Math.floor(screenX - size / 2);

        var visible = 0;
        for (var x = left; x < left + size; x += COLUMN_W) {
          if (x < 0 || x >= W) continue;
          if (zbuf[x] != null && p.ty >= zbuf[x]) continue;
          visible += 1;
          ctx.fillStyle = p.s.color;
          ctx.fillRect(x, Math.max(0, top), COLUMN_W, Math.min(H - Math.max(0, top), size));
        }

        // A label band, so the thing you are shooting says what it is. The
        // whole point of these cabinets is the vocabulary.
        //
        // Gated on a column of the sprite actually having survived the depth
        // test. The body columns are rejected against zbuf individually, but
        // the label was painted unconditionally — so a call standing behind a
        // wall announced itself through it, which both looks broken and gives
        // away a target the corridor is supposed to be hiding.
        if (visible > 0 && p.s.label && p.ty < 7 && size > 22) {
          ctx.fillStyle = ink.bg;
          ctx.fillRect(left, top - 9, size, 8);
          ctx.fillStyle = p.s.color;
          ctx.font = '7px "IBM Plex Mono", monospace';
          ctx.textAlign = "center";
          ctx.fillText(p.s.label, screenX, top - 2);
          ctx.textAlign = "left";
        }
      });
  }

  /* The muzzle/weapon furniture both first-person cabinets share. */
  function casterDrawWeapon(ctx, ink, kick, flash) {
    var baseY = H - 6 + kick * 5;
    ctx.fillStyle = ink.dim;
    ctx.fillRect(W / 2 - 10, baseY - 26, 20, 26);
    ctx.fillStyle = ink.ink;
    ctx.fillRect(W / 2 - 6, baseY - 34, 12, 10);
    if (flash > 0) {
      ctx.fillStyle = ink.bright;
      ctx.fillRect(W / 2 - 5, baseY - 42, 10, 9);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(W / 2 - 9, baseY - 39, 18, 4);
    }
    // Reticle
    ctx.fillStyle = ink.verify;
    ctx.fillRect(W / 2 - 5, H / 2, 3, 1);
    ctx.fillRect(W / 2 + 2, H / 2, 3, 1);
    ctx.fillRect(W / 2, H / 2 - 5, 1, 3);
    ctx.fillRect(W / 2, H / 2 + 2, 1, 3);
  }

  /* Hitscan: the nearest sprite within `spread` radians of the view axis and
     in front of a wall. Hitscan rather than a projectile because a travelling
     bullet at this resolution is a single pixel nobody can see. */
  function casterHitscan(cam, sprites, caster) {
    var best = null;
    sprites.forEach(function (s) {
      if (s.dead) return;
      var relX = s.x - cam.x;
      var relY = s.y - cam.y;
      var dist = Math.sqrt(relX * relX + relY * relY);
      if (dist < 0.01) return;
      var along = (relX * cam.dirX + relY * cam.dirY) / dist;
      if (along < 0) return; // behind the camera plane
      var spread = Math.atan2(Math.abs(relX * cam.dirY - relY * cam.dirX), relX * cam.dirX + relY * cam.dirY);
      // A fixed angular cone covers less and less of a target the closer it
      // stands, so an enemy that walks into your face becomes unhittable at
      // the moment it is most dangerous. On the corridor cabinets, this reads
      // as a gun that stops working under pressure. Accept
      // either the cone or a small lateral miss distance; beyond ~2.5 units
      // the cone is the narrower of the two and nothing changes.
      if (spread > 0.16 && Math.sin(spread) * dist > 0.4) return;
      // A wall between us and it means the shot stops at the wall.
      if (caster.cast(cam.x, cam.y, relX / dist, relY / dist).dist < dist - 0.2) return;
      if (!best || dist < best.dist) best = { sprite: s, dist: dist };
    });
    return best;
  }

  /* ---- 5. BLAST RADIUS ---------------------------------------------------

     First person, down the corridors of a permit boundary. Unscoped calls
     wander the maze looking for the tool; you get there first. Clear the
     floor and the next one has more of them, moving faster.

     Controls are turn-and-walk rather than mouselook, which is both what this
     input surface offers (five keys, no pointer lock) and what a cabinet of
     this vintage would have had anyway. */
  function cabinetBlastRadius(random) {
    var MAP = [
      "################",
      "#....#.....#...#",
      "#.##.#.###.#.#.#",
      "#.#..#...#...#.#",
      "#.#.###.###.##.#",
      "#.#.....#....#.#",
      "#.#####.#.####.#",
      "#.......#......#",
      "#.####.###.###.#",
      "#.#..#.....#...#",
      "#.#.###.##.#.#.#",
      "#.#...#..#.#.#.#",
      "#.###.##.#.#.#.#",
      "#.....#....#...#",
      "#.###.#.####.#.#",
      "################"
    ];
    var caster = makeCaster(MAP);
    var game = { id: "blast-radius", score: 0, lives: 3, over: false };
    var cam, calls, floor, kick, flash, cooldown, hurt, message, messageAge, open;

    /* Reachable open cells, flood-filled from the spawn. Spawning from the
       raw map instead would eventually drop a call into a sealed pocket,
       where it is unkillable and the floor can never be cleared. */
    function reachable(sx, sy) {
      var seen = {};
      var out = [];
      var queue = [[sx, sy]];
      seen[sx + "," + sy] = true;
      while (queue.length) {
        var cell = queue.shift();
        out.push(cell);
        [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function (d) {
          var nx = cell[0] + d[0];
          var ny = cell[1] + d[1];
          var key = nx + "," + ny;
          if (seen[key]) return;
          if (nx < 0 || ny < 0 || nx >= caster.w || ny >= caster.h) return;
          if (caster.map[ny][nx]) return;
          seen[key] = true;
          queue.push([nx, ny]);
        });
      }
      return out;
    }

    function spawnFloor() {
      calls = [];
      var count = Math.min(3 + floor * 2, 12);
      // Never adjacent to the player: a call spawned in your face is a retry
      // lost to nothing you could have reacted to.
      var far = open.filter(function (cell) {
        var dx = cell[0] + 0.5 - cam.x;
        var dy = cell[1] + 0.5 - cam.y;
        return Math.sqrt(dx * dx + dy * dy) > 4;
      });
      for (var i = 0; i < count && far.length; i += 1) {
        var pick = far[Math.floor(random() * far.length)];
        calls.push({
          x: pick[0] + 0.5,
          y: pick[1] + 0.5,
          color: "",
          label: LOOSE_CALLS[Math.floor(random() * LOOSE_CALLS.length)],
          scale: 0.62,
          speed: 0.55 + floor * 0.14,
          dead: false
        });
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      floor = 1;
      kick = 0;
      flash = 0;
      cooldown = 0;
      hurt = 0;
      message = "FLOOR 1";
      messageAge = 0;
      cam = { x: 1.5, y: 1.5, dirX: 1, dirY: 0, angle: 0 };
      open = reachable(1, 1);
      spawnFloor();
    };

    game.update = function (dt, input) {
      if (game.over) return;

      var turn = 2.1 * dt;
      // Turning reads only the direction flags. A drag sets those through the
      // shell's swipe latch, so touch steers without this cabinet knowing
      // anything about pointers — and, crucially, a hovering mouse does not
      // steer at all. Treating pointerX as a turn RATE (which an earlier
      // version did) spins the camera forever the moment the pointer rests
      // anywhere off-centre, including on a desktop with no button held.
      if (input.left) cam.angle -= turn;
      if (input.right) cam.angle += turn;
      cam.dirX = Math.cos(cam.angle);
      cam.dirY = Math.sin(cam.angle);

      var pace = 2.0 * dt;
      if (input.up) casterMove(caster, cam, cam.dirX * pace, cam.dirY * pace);
      if (input.down) casterMove(caster, cam, -cam.dirX * pace * 0.7, -cam.dirY * pace * 0.7);

      cooldown -= dt;
      kick = Math.max(0, kick - dt * 6);
      flash = Math.max(0, flash - dt * 8);
      hurt = Math.max(0, hurt - dt * 2);
      messageAge += dt;

      if (input.fire && cooldown <= 0) {
        cooldown = 0.28;
        kick = 1;
        flash = 1;
        var hit = casterHitscan(cam, calls, caster);
        if (hit) {
          hit.sprite.dead = true;
          game.score += 25;
          message = "DENIED " + hit.sprite.label;
          messageAge = 0;
        } else {
          // Missing costs score, not a retry: the joke is the metering, and a
          // shot that debits nothing would make firing blindly optimal.
          game.score = Math.max(0, game.score - 5);
        }
      }

      calls = calls.filter(function (c) { return !c.dead; });

      calls.forEach(function (c) {
        var dx = cam.x - c.x;
        var dy = cam.y - c.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        casterMove(caster, c, (dx / dist) * c.speed * dt, (dy / dist) * c.speed * dt);
        if (dist < 0.55 && hurt <= 0) {
          hurt = 1;
          game.lives -= 1;
          message = "REACHED THE TOOL";
          messageAge = 0;
          // Shoved back to the spawn rather than killed outright: losing the
          // floor's progress to one bad corner would be the cheap version.
          cam.x = 1.5;
          cam.y = 1.5;
          if (game.lives <= 0) game.over = true;
        }
      });

      if (!calls.length && !game.over) {
        floor += 1;
        game.score += 100;
        message = "FLOOR " + floor;
        messageAge = 0;
        spawnFloor();
      }
    };

    game.draw = function (ctx, ink) {
      calls.forEach(function (c) { c.color = ink.danger; });
      var zbuf = casterDrawWalls(ctx, ink, caster, cam);
      casterDrawSprites(ctx, ink, cam, calls, zbuf);
      casterDrawWeapon(ctx, ink, kick, flash);

      if (hurt > 0) {
        // A full-screen red wash would be unreadable on this palette; a border
        // says the same thing and leaves the corridor visible.
        ctx.fillStyle = ink.danger;
        ctx.fillRect(0, 0, W, 2);
        ctx.fillRect(0, H - 2, W, 2);
        ctx.fillRect(0, 0, 2, H);
        ctx.fillRect(W - 2, 0, 2, H);
      }

      drawMinimap(ctx, ink, caster, cam, calls);

      if (message && messageAge < 2) {
        ctx.fillStyle = ink.bright;
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 32);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "FLOOR " + floor + " · " + calls.length + " LOOSE";
    };

    return game;
  }

  /* A corner minimap. Both first-person cabinets need one: turn-and-walk with
     no strafe is disorienting without a plan view, and this is cheaper to read
     than making the maze simpler. */
  function drawMinimap(ctx, ink, caster, cam, sprites) {
    var cell = 3;
    var ox = W - caster.w * cell - 3;
    var oy = 22;
    ctx.fillStyle = ink.bg;
    ctx.fillRect(ox - 1, oy - 1, caster.w * cell + 2, caster.h * cell + 2);
    for (var y = 0; y < caster.h; y += 1) {
      for (var x = 0; x < caster.w; x += 1) {
        if (!caster.map[y][x]) continue;
        ctx.fillStyle = ink.grid;
        ctx.fillRect(ox + x * cell, oy + y * cell, cell, cell);
      }
    }
    sprites.forEach(function (s) {
      ctx.fillStyle = ink.danger;
      ctx.fillRect(ox + s.x * cell - 1, oy + s.y * cell - 1, 2, 2);
    });
    ctx.fillStyle = ink.verify;
    ctx.fillRect(ox + cam.x * cell - 1, oy + cam.y * cell - 1, 2, 2);
  }

  /* ---- 6. HOLD THE LINE --------------------------------------------------

     The other half of the first-person pair, and the inverse problem. You do
     not advance: you stand on the boundary itself while calls arrive down four
     corridors. Anything that reaches the post is a call that got through. */
  function cabinetHoldTheLine(random) {
    var MAP = [
      "#####==#####",
      "#####..#####",
      "#####..#####",
      "##.......###",
      "##.......###",
      "=..........=",
      "=..........=",
      "##.......###",
      "##.......###",
      "#####..#####",
      "#####..#####",
      "#####==#####"
    ];
    var caster = makeCaster(MAP);
    var MOUTHS = [
      { x: 6, y: 1.2 }, { x: 6, y: 10.8 }, { x: 1.2, y: 6 }, { x: 10.8, y: 6 }
    ];

    var game = { id: "hold-the-line", score: 0, lives: 3, over: false };
    var cam, calls, wave, spawnTimer, remaining, kick, flash, cooldown, hurt, message, messageAge, post;

    function startWave() {
      remaining = 4 + wave * 2;
      spawnTimer = 0.6;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      wave = 1;
      calls = [];
      kick = 0;
      flash = 0;
      cooldown = 0;
      hurt = 0;
      message = "WAVE 1";
      messageAge = 0;
      post = { x: 6, y: 6 };
      cam = { x: post.x, y: post.y, dirX: 0, dirY: -1, angle: -Math.PI / 2 };
      startWave();
    };

    game.update = function (dt, input) {
      if (game.over) return;

      var turn = 2.4 * dt;
      // Direction flags only — see the note in BLAST RADIUS on why a pointer
      // position must not drive a camera.
      if (input.left) cam.angle -= turn;
      if (input.right) cam.angle += turn;
      cam.dirX = Math.cos(cam.angle);
      cam.dirY = Math.sin(cam.angle);

      // Step off the post only far enough to change an angle. Wandering is the
      // other cabinet's game; here leaving the line is the failure.
      var pace = 1.4 * dt;
      if (input.up) casterMove(caster, cam, cam.dirX * pace, cam.dirY * pace);
      if (input.down) casterMove(caster, cam, -cam.dirX * pace, -cam.dirY * pace);
      var offX = cam.x - post.x;
      var offY = cam.y - post.y;
      var leash = Math.sqrt(offX * offX + offY * offY);
      if (leash > 1.6) {
        cam.x = post.x + (offX / leash) * 1.6;
        cam.y = post.y + (offY / leash) * 1.6;
      }

      cooldown -= dt;
      kick = Math.max(0, kick - dt * 6);
      flash = Math.max(0, flash - dt * 8);
      hurt = Math.max(0, hurt - dt * 2);
      messageAge += dt;

      if (input.fire && cooldown <= 0) {
        cooldown = 0.24;
        kick = 1;
        flash = 1;
        var hit = casterHitscan(cam, calls, caster);
        if (hit) {
          hit.sprite.dead = true;
          game.score += 20;
        } else {
          game.score = Math.max(0, game.score - 4);
        }
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0 && remaining > 0) {
        remaining -= 1;
        spawnTimer = Math.max(0.35, 1.5 - wave * 0.12);
        var mouth = MOUTHS[Math.floor(random() * MOUTHS.length)];
        calls.push({
          x: mouth.x,
          y: mouth.y,
          color: "",
          label: LOOSE_CALLS[Math.floor(random() * LOOSE_CALLS.length)],
          scale: 0.6,
          speed: 0.5 + wave * 0.1,
          dead: false
        });
      }

      calls = calls.filter(function (c) { return !c.dead; });
      calls.forEach(function (c) {
        var dx = post.x - c.x;
        var dy = post.y - c.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        casterMove(caster, c, (dx / dist) * c.speed * dt, (dy / dist) * c.speed * dt);
        if (dist < 0.8) {
          c.dead = true;
          if (hurt <= 0) {
            hurt = 1;
            game.lives -= 1;
            message = "GOT THROUGH";
            messageAge = 0;
            if (game.lives <= 0) game.over = true;
          }
        }
      });

      if (!calls.length && remaining <= 0 && !game.over) {
        wave += 1;
        game.score += 60;
        message = "WAVE " + wave;
        messageAge = 0;
        startWave();
      }
    };

    game.draw = function (ctx, ink) {
      calls.forEach(function (c) { c.color = ink.danger; });
      var zbuf = casterDrawWalls(ctx, ink, caster, cam);
      casterDrawSprites(ctx, ink, cam, calls, zbuf);
      casterDrawWeapon(ctx, ink, kick, flash);

      if (hurt > 0) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(0, 0, W, 2);
        ctx.fillRect(0, H - 2, W, 2);
        ctx.fillRect(0, 0, 2, H);
        ctx.fillRect(W - 2, 0, 2, H);
      }

      drawMinimap(ctx, ink, caster, cam, calls);

      if (message && messageAge < 2) {
        ctx.fillStyle = ink.bright;
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 32);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "WAVE " + wave + " · " + (calls.length + remaining) + " INBOUND";
    };

    return game;
  }

  /* ---- 7. RETRY STORM ----------------------------------------------------

     Free-floating duplicate requests. Shooting one does not remove it — it
     splits into two smaller ones, because a retry of a retry is two retries.
     Only the smallest tier can actually be cleared, and only by ramming it
     with the idempotency key you are carrying. */
  function cabinetRetryStorm(random) {
    var game = { id: "retry-storm", score: 0, lives: 3, over: false };
    var ship, dupes, shots, wave, cooldown, invuln, message, messageAge;

    function spawnWave() {
      dupes = [];
      for (var i = 0; i < 2 + wave; i += 1) {
        var edge = random();
        dupes.push({
          x: edge < 0.5 ? 8 : W - 8,
          y: random() * H,
          vx: (random() - 0.5) * 34,
          vy: (random() - 0.5) * 34,
          tier: 3
        });
      }
    }

    function radius(tier) { return tier * 5; }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      wave = 1;
      shots = [];
      cooldown = 0;
      invuln = 1.5;
      message = "";
      messageAge = 0;
      ship = { x: W / 2, y: H / 2, angle: -Math.PI / 2, vx: 0, vy: 0 };
      spawnWave();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      cooldown -= dt;
      invuln = Math.max(0, invuln - dt);

      if (input.left) ship.angle -= 3 * dt;
      if (input.right) ship.angle += 3 * dt;
      if (input.up) {
        ship.vx += Math.cos(ship.angle) * 150 * dt;
        ship.vy += Math.sin(ship.angle) * 150 * dt;
      }
      // Drag, so the ship is steerable at this screen size. Frictionless drift
      // on a 320px board means one thrust and you are gone.
      ship.vx *= 0.99;
      ship.vy *= 0.99;
      ship.x = (ship.x + ship.vx * dt + W) % W;
      ship.y = (ship.y + ship.vy * dt + H) % H;

      if (input.fire && cooldown <= 0) {
        cooldown = 0.3;
        shots.push({
          x: ship.x, y: ship.y,
          vx: Math.cos(ship.angle) * 210,
          vy: Math.sin(ship.angle) * 210,
          life: 1.1
        });
      }

      shots.forEach(function (s) {
        s.x = (s.x + s.vx * dt + W) % W;
        s.y = (s.y + s.vy * dt + H) % H;
        s.life -= dt;
      });
      shots = shots.filter(function (s) { return s.life > 0; });

      dupes.forEach(function (d) {
        d.x = (d.x + d.vx * dt + W) % W;
        d.y = (d.y + d.vy * dt + H) % H;
      });

      // Shot hits: split rather than clear.
      var spawned = [];
      shots.forEach(function (s) {
        dupes.forEach(function (d) {
          if (d.hit) return;
          if (Math.hypot(d.x - s.x, d.y - s.y) > radius(d.tier)) return;
          d.hit = true;
          s.life = 0;
          if (d.tier > 1) {
            game.score += 5;
            for (var k = 0; k < 2; k += 1) {
              spawned.push({
                x: d.x, y: d.y,
                vx: (random() - 0.5) * 70,
                vy: (random() - 0.5) * 70,
                tier: d.tier - 1
              });
            }
            message = "RETRIED — NOW THERE ARE TWO";
            messageAge = 0;
          } else {
            // A tier-1 shot is just noise; it neither clears nor splits.
            d.hit = false;
            s.life = 0;
          }
        });
      });
      dupes = dupes.filter(function (d) { return !d.hit; }).concat(spawned);
      shots = shots.filter(function (s) { return s.life > 0; });

      // Ramming: the idempotency key. Only tier 1 is absorbed; anything bigger
      // is a collision and costs a retry.
      dupes.forEach(function (d) {
        if (Math.hypot(d.x - ship.x, d.y - ship.y) > radius(d.tier) + 4) return;
        if (d.tier === 1) {
          d.hit = true;
          game.score += 30;
          message = "KEYED — ABSORBED";
          messageAge = 0;
        } else if (invuln <= 0) {
          invuln = 1.5;
          game.lives -= 1;
          ship.x = W / 2; ship.y = H / 2; ship.vx = 0; ship.vy = 0;
          message = "COLLIDED";
          messageAge = 0;
          if (game.lives <= 0) game.over = true;
        }
      });
      dupes = dupes.filter(function (d) { return !d.hit; });

      if (!dupes.length && !game.over) {
        wave += 1;
        game.score += 80;
        message = "STORM CLEARED";
        messageAge = 0;
        spawnWave();
      }
    };

    game.draw = function (ctx, ink) {
      dupes.forEach(function (d) {
        ctx.fillStyle = d.tier === 1 ? ink.verify : ink.danger;
        var r = radius(d.tier);
        ctx.fillRect(d.x - r, d.y - r, r * 2, r * 2);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(d.x - r + 2, d.y - r + 2, r * 2 - 4, r * 2 - 4);
      });

      ctx.fillStyle = ink.brass;
      shots.forEach(function (s) { ctx.fillRect(s.x - 1, s.y - 1, 2, 2); });

      // Ship: a chunky arrow, blinking while the collision grace lasts.
      if (invuln <= 0 || Math.floor(invuln * 12) % 2 === 0) {
        ctx.save();
        ctx.translate(ship.x, ship.y);
        ctx.rotate(ship.angle);
        ctx.fillStyle = ink.ink;
        ctx.fillRect(-4, -3, 8, 6);
        ctx.fillStyle = ink.verify;
        ctx.fillRect(4, -1, 4, 2);
        ctx.restore();
      }

      if (message && messageAge < 1.8) {
        ctx.fillStyle = ink.bright;
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 32);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "STORM " + wave + " · " + dupes.length + " DUPES"; };
    return game;
  }

  /* ---- 8. DOUBLE SPEND ---------------------------------------------------

     Cross the settlement lanes to the ledger and back. Each lane carries
     charges moving at its own rate; touching one is the same request being
     charged twice, which is the one thing this product exists to prevent. */
  function cabinetDoubleSpend(random) {
    var game = { id: "double-spend", score: 0, lives: 3, over: false };
    var LANES = 6;
    var LANE_H = 26;
    var TOP = 44;
    var player, lanes, round, message, messageAge, invuln;

    function buildLanes() {
      lanes = [];
      for (var i = 0; i < LANES; i += 1) {
        var dir = i % 2 === 0 ? 1 : -1;
        var speed = (28 + random() * 26 + round * 7) * dir;
        var charges = [];
        var gap = 74 + random() * 34;
        for (var x = 0; x < W + gap; x += gap) {
          charges.push({ x: x, w: 22 + Math.floor(random() * 14) });
        }
        lanes.push({ y: TOP + i * LANE_H, speed: speed, charges: charges });
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      message = "";
      messageAge = 0;
      invuln = 0;
      player = { x: W / 2, y: H - 12 };
      buildLanes();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      invuln = Math.max(0, invuln - dt);

      // Stepped movement on a cooldown, not free gliding: the lanes are a
      // timing puzzle and analogue movement turns them into a smear.
      player.step = (player.step || 0) - dt;
      if (player.step <= 0) {
        var moved = false;
        if (input.left) { player.x -= 18; moved = true; }
        if (input.right) { player.x += 18; moved = true; }
        if (input.up) { player.y -= LANE_H; moved = true; }
        if (input.down) { player.y += LANE_H; moved = true; }
        if (moved) player.step = 0.12;
      }
      player.x = clamp(player.x, 6, W - 6);
      player.y = clamp(player.y, 6, H - 12);

      lanes.forEach(function (lane) {
        lane.charges.forEach(function (c) {
          c.x += lane.speed * dt;
          if (lane.speed > 0 && c.x > W + 30) c.x = -30;
          if (lane.speed < 0 && c.x < -30) c.x = W + 30;
        });
      });

      if (invuln <= 0) {
        lanes.forEach(function (lane) {
          if (Math.abs(player.y - lane.y) > 9) return;
          lane.charges.forEach(function (c) {
            if (player.x + 4 < c.x || player.x - 4 > c.x + c.w) return;
            invuln = 1.2;
            game.lives -= 1;
            message = "CHARGED TWICE";
            messageAge = 0;
            player.x = W / 2;
            player.y = H - 12;
            if (game.lives <= 0) game.over = true;
          });
        });
      }

      if (player.y < TOP - 10 && !game.over) {
        round += 1;
        game.score += 50 + round * 10;
        message = "SETTLED ONCE";
        messageAge = 0;
        player.x = W / 2;
        player.y = H - 12;
        buildLanes();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.verify;
      ctx.fillRect(0, TOP - 14, W, 1);
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEDGER", 4, TOP - 17);

      lanes.forEach(function (lane) {
        ctx.fillStyle = ink.grid;
        ctx.fillRect(0, lane.y + 10, W, 1);
        lane.charges.forEach(function (c) {
          ctx.fillStyle = ink.danger;
          ctx.fillRect(c.x, lane.y - 7, c.w, 14);
          ctx.fillStyle = ink.bg;
          ctx.fillRect(c.x + 2, lane.y - 5, c.w - 4, 10);
          ctx.fillStyle = ink.danger;
          ctx.fillText("$", c.x + c.w / 2 - 2, lane.y + 3);
        });
      });

      if (invuln <= 0 || Math.floor(invuln * 12) % 2 === 0) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(player.x - 4, player.y - 5, 8, 10);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(player.x - 2, player.y - 3, 4, 3);
      }

      if (message && messageAge < 1.6) {
        ctx.fillStyle = ink.bright;
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 32);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "CROSSING " + round; };
    return game;
  }

  /* ---- 9. BACKPRESSURE ---------------------------------------------------

     Work arrives faster than it drains. Stack it into the queue; a completed
     row is a batch that drains. Reach the top and the queue has overflowed,
     which downstream will experience as your outage. */
  function cabinetBackpressure(random) {
    var COLS = 10;
    var ROWS = 15;
    var CELL = 12;
    var OX = (W - COLS * CELL) / 2;
    var OY = H - ROWS * CELL - 4;

    // Deliberately not the canonical seven: four shapes keep the piece table
    // short enough to read and the well solvable at this speed.
    var SHAPES = [
      [[0, 0], [1, 0], [0, 1], [1, 1]],
      [[0, 0], [1, 0], [2, 0], [3, 0]],
      [[0, 0], [0, 1], [1, 1], [2, 1]],
      [[0, 1], [1, 1], [1, 0], [2, 0]]
    ];

    var game = { id: "backpressure", score: 0, lives: 3, over: false };
    var grid, piece, drop, dropRate, moveCd, batches, message, messageAge;

    function newPiece() {
      piece = {
        cells: SHAPES[Math.floor(random() * SHAPES.length)].map(function (c) { return [c[0], c[1]]; }),
        x: 3,
        y: 0
      };
      if (collides(piece.cells, piece.x, piece.y)) {
        game.lives = 0;
        game.over = true;
        message = "QUEUE OVERFLOW";
        messageAge = 0;
      }
    }

    function collides(cells, px, py) {
      return cells.some(function (c) {
        var x = px + c[0];
        var y = py + c[1];
        if (x < 0 || x >= COLS || y >= ROWS) return true;
        if (y < 0) return false;
        return !!grid[y][x];
      });
    }

    function settle() {
      piece.cells.forEach(function (c) {
        var x = piece.x + c[0];
        var y = piece.y + c[1];
        if (y >= 0 && y < ROWS && x >= 0 && x < COLS) grid[y][x] = 1;
      });
      // Drain any full rows.
      var drained = 0;
      for (var y = ROWS - 1; y >= 0; y -= 1) {
        var full = grid[y].every(function (v) { return v; });
        if (!full) continue;
        grid.splice(y, 1);
        grid.unshift(new Array(COLS).fill(0));
        drained += 1;
        y += 1; // re-test the row that just shifted down into y
      }
      if (drained) {
        batches += drained;
        game.score += drained * drained * 40;
        dropRate = Math.max(0.12, dropRate - 0.015 * drained);
        message = drained > 1 ? "BATCH DRAINED ×" + drained : "BATCH DRAINED";
        messageAge = 0;
      }
      newPiece();
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      grid = [];
      for (var y = 0; y < ROWS; y += 1) grid.push(new Array(COLS).fill(0));
      drop = 0;
      dropRate = 0.55;
      moveCd = 0;
      batches = 0;
      message = "";
      messageAge = 0;
      newPiece();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      moveCd -= dt;

      if (moveCd <= 0) {
        if (input.left && !collides(piece.cells, piece.x - 1, piece.y)) { piece.x -= 1; moveCd = 0.11; }
        else if (input.right && !collides(piece.cells, piece.x + 1, piece.y)) { piece.x += 1; moveCd = 0.11; }
      }

      if (input.fire && !piece.rotated) {
        // Rotate about the piece's own bounding box, then reject the rotation
        // outright if it would overlap: wall kicks are more machinery than
        // this well needs.
        var turned = piece.cells.map(function (c) { return [-c[1] + 1, c[0]]; });
        if (!collides(turned, piece.x, piece.y)) piece.cells = turned;
        piece.rotated = true;
      }
      if (!input.fire) piece.rotated = false;

      drop -= dt * (input.down ? 8 : 1);
      if (drop <= 0) {
        drop = dropRate;
        if (collides(piece.cells, piece.x, piece.y + 1)) settle();
        else piece.y += 1;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.grid;
      ctx.fillRect(OX - 2, OY - 2, COLS * CELL + 4, ROWS * CELL + 4);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(OX, OY, COLS * CELL, ROWS * CELL);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (!grid[y][x]) continue;
          ctx.fillStyle = ink.dim;
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL - 1, CELL - 1);
        }
      }
      if (piece) {
        ctx.fillStyle = ink.brass;
        piece.cells.forEach(function (c) {
          var x = piece.x + c[0];
          var y = piece.y + c[1];
          if (y < 0) return;
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL - 1, CELL - 1);
        });
      }

      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("QUEUE DEPTH", 4, 34);
      ctx.fillStyle = ink.danger;
      ctx.fillText("OVERFLOW", 4, 44);
      ctx.fillStyle = ink.grid;
      ctx.fillRect(0, OY, OX - 4, 1);

      if (message && messageAge < 1.6) {
        ctx.fillStyle = ink.verify;
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 30);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "BATCHES " + batches; };
    return game;
  }

  /* ---- 10. NONCE BURN ----------------------------------------------------

     A grid of nonces. Each can be used once, until it expires. Move
     the cursor, burn the live ones. Burning a spent cell is a replay, and
     replays are what the nonce exists to stop. */
  function cabinetNonceBurn(random) {
    var COLS = 5;
    var ROWS = 4;
    var CELL = 46;
    var OX = (W - COLS * CELL) / 2;
    var OY = 42;

    var game = { id: "nonce-burn", score: 0, lives: 3, over: false };
    var cells, cursor, moveCd, fireLatch, spawnTimer, round, message, messageAge;

    function idx(cx, cy) { return cy * COLS + cx; }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cells = [];
      for (var i = 0; i < COLS * ROWS; i += 1) cells.push({ live: false, ttl: 0, spent: false });
      cursor = { x: 2, y: 1 };
      moveCd = 0;
      fireLatch = false;
      spawnTimer = 0.4;
      round = 1;
      message = "";
      messageAge = 0;
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      moveCd -= dt;

      if (moveCd <= 0) {
        var moved = false;
        if (input.left) { cursor.x -= 1; moved = true; }
        else if (input.right) { cursor.x += 1; moved = true; }
        else if (input.up) { cursor.y -= 1; moved = true; }
        else if (input.down) { cursor.y += 1; moved = true; }
        if (moved) moveCd = 0.13;
        cursor.x = clamp(cursor.x, 0, COLS - 1);
        cursor.y = clamp(cursor.y, 0, ROWS - 1);
      }

      if (input.fire && !fireLatch) {
        fireLatch = true;
        var cell = cells[idx(cursor.x, cursor.y)];
        if (cell.live) {
          cell.live = false;
          cell.spent = true;
          cell.ttl = 0;
          game.score += 15 + Math.floor(round * 2);
          message = "BURNED";
          messageAge = 0;
        } else {
          // Pressing an empty or spent cell is the replay.
          game.lives -= 1;
          message = cell.spent ? "REPLAY REJECTED" : "NO NONCE THERE";
          messageAge = 0;
          if (game.lives <= 0) game.over = true;
        }
      }
      if (!input.fire) fireLatch = false;

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.25, 1.1 - round * 0.05);
        var free = [];
        cells.forEach(function (c, i) { if (!c.live) free.push(i); });
        if (free.length) {
          var pick = free[Math.floor(random() * free.length)];
          cells[pick].live = true;
          cells[pick].spent = false;
          cells[pick].ttl = Math.max(1.1, 2.6 - round * 0.08);
          cells[pick].max = cells[pick].ttl;
        }
      }

      cells.forEach(function (c) {
        if (!c.live) return;
        c.ttl -= dt;
        if (c.ttl > 0) return;
        // An expired nonce is not a life lost — it is just gone. Losing a
        // retry to the clock would make the board unplayable at speed.
        c.live = false;
        c.spent = false;
        game.score = Math.max(0, game.score - 3);
      });

      round += dt * 0.25;
    };

    game.draw = function (ctx, ink) {
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BURN EACH NONCE ONCE — BEFORE IT EXPIRES", 6, 32);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          var c = cells[idx(x, y)];
          var px = OX + x * CELL;
          var py = OY + y * CELL;
          ctx.fillStyle = ink.grid;
          ctx.fillRect(px, py, CELL - 6, CELL - 6);
          ctx.fillStyle = ink.bg;
          ctx.fillRect(px + 2, py + 2, CELL - 10, CELL - 10);

          if (c.live) {
            ctx.fillStyle = ink.brass;
            ctx.fillRect(px + 6, py + 6, CELL - 18, CELL - 18);
            // TTL as a draining bar rather than a ring: an arc at this size is
            // four grey pixels and reads as nothing.
            var frac = clamp(c.ttl / (c.max || 1), 0, 1);
            ctx.fillStyle = c.ttl < 0.6 ? ink.danger : ink.verify;
            ctx.fillRect(px + 4, py + CELL - 12, Math.floor((CELL - 14) * frac), 3);
          } else if (c.spent) {
            ctx.fillStyle = ink.dim;
            ctx.fillRect(px + 12, py + 16, CELL - 30, 3);
          }
        }
      }

      var cx = OX + cursor.x * CELL;
      var cy = OY + cursor.y * CELL;
      ctx.fillStyle = ink.verify;
      ctx.fillRect(cx - 2, cy - 2, CELL - 2, 2);
      ctx.fillRect(cx - 2, cy + CELL - 6, CELL - 2, 2);
      ctx.fillRect(cx - 2, cy - 2, 2, CELL - 2);
      ctx.fillRect(cx + CELL - 6, cy - 2, 2, CELL - 2);

      if (message && messageAge < 1.2) {
        ctx.fillStyle = ink.bright;
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, H - 8);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "TIER " + Math.floor(round); };
    return game;
  }

  /* ---- 11. KEY ROTATION --------------------------------------------------

     Collect every shard of the new signing key while the revocation sweepers
     walk the old one out of the maze. Touching a sweeper is being caught
     holding a key that is no longer valid. */
  function cabinetKeyRotation(random) {
    var MAZE = [
      "###############",
      "#......#......#",
      "#.####.#.####.#",
      "#.#..........##",
      "#.#.##.#.##.#.#",
      "#......#......#",
      "##.###.#.###.##",
      "#......#......#",
      "#.####.#.####.#",
      "#.....       .#",
      "#.###.##.##.#.#",
      "#......#......#",
      "###############"
    ];
    var CELL = 16;
    var COLS = MAZE[0].length;
    var ROWS = MAZE.length;
    var OX = (W - COLS * CELL) / 2;
    var OY = (H - ROWS * CELL) / 2 + 8;

    var game = { id: "key-rotation", score: 0, lives: 3, over: false };
    var walls, shards, player, sweepers, moveCd, rotation, message, messageAge, invuln;

    function wall(x, y) {
      if (x < 0 || y < 0 || x >= COLS || y >= ROWS) return true;
      return walls[y][x];
    }

    function layout() {
      walls = MAZE.map(function (row) {
        return row.split("").map(function (ch) { return ch === "#" ? 1 : 0; });
      });
      shards = [];
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (walls[y][x]) continue;
          if (x === 1 && y === 1) continue;
          shards.push({ x: x, y: y });
        }
      }
      player = { x: 1, y: 1 };
      sweepers = [
        { x: COLS - 2, y: ROWS - 2, dx: -1, dy: 0, step: 0 },
        { x: COLS - 2, y: 1, dx: 0, dy: 1, step: 0 },
        { x: 1, y: ROWS - 2, dx: 1, dy: 0, step: 0 }
      ];
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      rotation = 1;
      moveCd = 0;
      invuln = 0;
      message = "";
      messageAge = 0;
      layout();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      moveCd -= dt;
      invuln = Math.max(0, invuln - dt);

      if (moveCd <= 0) {
        var nx = player.x;
        var ny = player.y;
        if (input.left) nx -= 1;
        else if (input.right) nx += 1;
        else if (input.up) ny -= 1;
        else if (input.down) ny += 1;
        if ((nx !== player.x || ny !== player.y) && !wall(nx, ny)) {
          player.x = nx;
          player.y = ny;
          moveCd = 0.11;
        }
      }

      shards = shards.filter(function (s) {
        if (s.x !== player.x || s.y !== player.y) return true;
        game.score += 10;
        return false;
      });

      sweepers.forEach(function (s) {
        s.step -= dt;
        if (s.step > 0) return;
        s.step = Math.max(0.10, 0.24 - rotation * 0.02);
        // Head toward the player when the way is open, otherwise take any
        // turn that is not a reversal. Enough to be threatening; not enough
        // to be unfair on a maze this size.
        var options = [[1, 0], [-1, 0], [0, 1], [0, -1]].filter(function (d) {
          return !wall(s.x + d[0], s.y + d[1]);
        });
        if (!options.length) return;
        var forward = options.filter(function (d) {
          return !(d[0] === -s.dx && d[1] === -s.dy);
        });
        var pool = forward.length ? forward : options;
        pool.sort(function (a, b) {
          var da = Math.abs(s.x + a[0] - player.x) + Math.abs(s.y + a[1] - player.y);
          var db = Math.abs(s.x + b[0] - player.x) + Math.abs(s.y + b[1] - player.y);
          return da - db;
        });
        var choice = random() < 0.72 ? pool[0] : pool[Math.floor(random() * pool.length)];
        s.dx = choice[0];
        s.dy = choice[1];
        s.x += s.dx;
        s.y += s.dy;
      });

      if (invuln <= 0) {
        sweepers.forEach(function (s) {
          if (invuln > 0 || game.over) return;
          if (s.x !== player.x || s.y !== player.y) return;
          invuln = 1.4;
          game.lives -= 1;
          message = "REVOKED";
          messageAge = 0;
          player.x = 1;
          player.y = 1;
          if (game.lives <= 0) game.over = true;
        });
      }

      if (!shards.length && !game.over) {
        rotation += 1;
        game.score += 120;
        message = "KEY ROTATED";
        messageAge = 0;
        layout();
      }
    };

    game.draw = function (ctx, ink) {
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (!walls[y][x]) continue;
          ctx.fillStyle = ink.grid;
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL - 1, CELL - 1);
        }
      }
      ctx.fillStyle = ink.brass;
      shards.forEach(function (s) {
        ctx.fillRect(OX + s.x * CELL + 6, OY + s.y * CELL + 6, 4, 4);
      });
      ctx.fillStyle = ink.danger;
      sweepers.forEach(function (s) {
        ctx.fillRect(OX + s.x * CELL + 2, OY + s.y * CELL + 2, CELL - 5, CELL - 5);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(OX + s.x * CELL + 5, OY + s.y * CELL + 5, 3, 3);
        ctx.fillStyle = ink.danger;
      });
      if (invuln <= 0 || Math.floor(invuln * 12) % 2 === 0) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(OX + player.x * CELL + 3, OY + player.y * CELL + 3, CELL - 7, CELL - 7);
      }

      ctx.font = '7px "IBM Plex Mono", monospace';
      if (message && messageAge < 1.5) {
        ctx.fillStyle = ink.bright;
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, H - 3);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "ROTATION " + rotation + " · " + shards.length + " LEFT"; };
    return game;
  }

  /* ---- 12. TAIL LATENCY --------------------------------------------------

     A runner along the latency chart. The floor is p50 and it is fine. The
     spikes are the tail, and the tail is what your users actually experience,
     so the tail is the part you have to clear. */
  function cabinetTailLatency(random) {
    var game = { id: "tail-latency", score: 0, lives: 3, over: false };
    var GROUND = H - 40;
    var runner, spikes, speed, spawnIn, distance, message, messageAge, invuln, trace;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      runner = { y: GROUND, vy: 0, onGround: true, duck: false };
      spikes = [];
      speed = 96;
      spawnIn = 1.1;
      distance = 0;
      invuln = 0;
      message = "";
      messageAge = 0;
      trace = [];
      for (var i = 0; i < W; i += 4) trace.push(GROUND + 8 + Math.sin(i * 0.11) * 3);
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      invuln = Math.max(0, invuln - dt);
      distance += speed * dt;
      speed = Math.min(230, 96 + distance * 0.012);
      game.score = Math.floor(distance / 10);

      if ((input.up || input.fire) && runner.onGround) {
        runner.vy = -232;
        runner.onGround = false;
      }
      runner.duck = !!input.down && runner.onGround;

      runner.vy += 640 * dt;
      runner.y += runner.vy * dt;
      if (runner.y >= GROUND) {
        runner.y = GROUND;
        runner.vy = 0;
        runner.onGround = true;
      }

      spawnIn -= dt;
      if (spawnIn <= 0) {
        spawnIn = Math.max(0.55, 1.35 - distance * 0.00035) + random() * 0.4;
        var tall = random() < 0.35;
        spikes.push({
          x: W + 10,
          h: tall ? 34 : 20,
          w: 8 + Math.floor(random() * 8),
          high: tall && random() < 0.5
        });
      }

      spikes.forEach(function (s) { s.x -= speed * dt; });
      spikes = spikes.filter(function (s) { return s.x > -20; });

      var rh = runner.duck ? 8 : 16;
      var ry = runner.y - rh;
      spikes.forEach(function (s) {
        if (invuln > 0) return;
        // A "high" spike hangs from above: it is the one you duck rather than
        // jump, so the two controls both have a reason to exist.
        // A standing runner occupies GROUND-16..GROUND and a ducking one
        // GROUND-8..GROUND, so a hanging spike has to reach below GROUND-16
        // to threaten the first and stop above GROUND-8 to spare the second.
        // At the original 26 it ended at GROUND-18 and cleared a standing
        // runner entirely, which made ducking decorative.
        var sy = s.high ? GROUND - 44 : GROUND - s.h;
        var sh = s.high ? 32 : s.h;
        if (s.x > 16 + 10 || s.x + s.w < 16 - 6) return;
        if (ry + rh < sy || ry > sy + sh) return;
        invuln = 1.2;
        game.lives -= 1;
        message = "TAIL EVENT";
        messageAge = 0;
        if (game.lives <= 0) game.over = true;
      });

      trace.push(GROUND + 8 + Math.sin(distance * 0.02) * 3 + random() * 2);
      if (trace.length > W / 4) trace.shift();
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.grid;
      ctx.fillRect(0, GROUND + 2, W, 1);

      // The p50 trace under the floor: flat, boring, and not the problem.
      ctx.fillStyle = ink.grid;
      trace.forEach(function (v, i) { ctx.fillRect(i * 4, v, 3, 1); });

      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("p50", 4, GROUND + 22);
      ctx.fillStyle = ink.danger;
      ctx.fillText("p99", 4, 32);

      spikes.forEach(function (s) {
        ctx.fillStyle = ink.danger;
        if (s.high) ctx.fillRect(s.x, GROUND - 44, s.w, 32);
        else ctx.fillRect(s.x, GROUND - s.h, s.w, s.h);
      });

      if (invuln <= 0 || Math.floor(invuln * 12) % 2 === 0) {
        var rh = runner.duck ? 8 : 16;
        ctx.fillStyle = ink.verify;
        ctx.fillRect(10, runner.y - rh, 12, rh);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(13, runner.y - rh + 3, 3, 3);
      }

      if (message && messageAge < 1.4) {
        ctx.fillStyle = ink.bright;
        ctx.textAlign = "center";
        ctx.fillText(message, W / 2, 30);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () { return "p99 " + Math.floor(speed) + "ms"; };
    return game;
  }

  /* ---- 13. COUNTERSIGN ---------------------------------------------------

     The round-based tactical shooter, which is the one console-era first
     person shape the other two cabinets do not cover: they are both endless
     waves, and this one is a bomb round. A rogue agent plants an *unsigned*
     action on one of two sites; the fuse is its TTL; you either eliminate the
     whole crew before the plant lands or you stand on the thing and hold the
     countersign key until the action is void again. Losing the fuse costs a
     retry and restarts the round, which is the genre's whole tension: the
     round is the unit, not the life.

     The buy phase is a caption rather than a menu. Five keys is not a shop,
     and a loadout line between rounds says the same joke in one frame. */
  function cabinetCountersign(random) {
    /* Open plan with pillars rather than sealed rooms. The crew chase by
       walking straight at you — there is no pathfinder in this file — so a
       map of small rooms joined by single doorways is one where half the
       hostiles spend the round pressed against a wall you cannot see. Every
       other row here runs clear from side to side, and the two floors are
       joined by two full-height openings. */
    var MAP = [
      "################",
      "#..............#",
      "#.==.#....#.##.#",
      "#....#....#....#",
      "#..............#",
      "####.######.####",
      "#..............#",
      "#....#....#....#",
      "#.##.#....#.==.#",
      "#..............#",
      "#..............#",
      "################"
    ];
    var caster = makeCaster(MAP);
    // The two plantable sites, in the corners the boundary markers stand in.
    var SITES = [
      { x: 2.5, y: 3.5, name: "SITE A" },
      { x: 13.5, y: 9.5, name: "SITE B" }
    ];
    // Hostile spawns, kept away from both sites so a round never opens with a
    // crew already standing on the thing they are about to plant.
    var SPAWNS = [
      { x: 8.5, y: 1.5 }, { x: 14.5, y: 1.5 },
      { x: 1.5, y: 10.5 }, { x: 14.5, y: 10.5 },
      { x: 8.5, y: 6.5 }
    ];
    var LOADOUTS = [
      "LOADOUT: ONE REVOCATION KEY. IT IS ENOUGH.",
      "BOUGHT: SECOND OPINION. NOBODY ASKED FOR IT.",
      "BOUGHT: FASTER AUDIT. SAME AUDITOR.",
      "BOUGHT: ARMOUR, BILLED AS A SUBSCRIPTION.",
      "BOUGHT: NOTHING. THE BUDGET IS THE POLICY."
    ];

    var PLANT_DELAY = 9;   // seconds of round time before the crew commits
    var FUSE = 32;         // TTL on the planted action
    var DEFUSE_TIME = 3.2; // hold-to-countersign

    var game = { id: "countersign", score: 0, lives: 3, over: false };
    var cam, crew, round, phase, timer, fuse, site, defuse, cooldown, kick,
      flash, hurt, message, messageAge, loadout, armour;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function startRound() {
      phase = "hunt";
      timer = PLANT_DELAY;
      fuse = FUSE;
      site = null;
      defuse = 0;
      crew = [];
      var count = Math.min(6, 2 + round);
      for (var i = 0; i < count; i += 1) {
        var spawn = SPAWNS[(i + round) % SPAWNS.length];
        crew.push({
          x: spawn.x + (random() - 0.5) * 0.4,
          y: spawn.y + (random() - 0.5) * 0.4,
          label: LOOSE_CALLS[Math.floor(random() * LOOSE_CALLS.length)],
          scale: 0.72,
          shootTimer: 1.5 + random() * 2,
          speed: 0.9 + round * 0.08,
          // Every hostile holds its own firing distance, so a crew arrives as
          // a firing line rather than as one clump on top of the player.
          standoff: 2.4 + random() * 1.6,
          dead: false
        });
      }
      // The player restarts each round at the post, the way a round-based
      // shooter resets the map rather than continuing from where the last one
      // ended. Continuing is a wave shooter, and the arcade has two already.
      cam = { x: 8.5, y: 4.5, dirX: 0, dirY: -1, angle: -Math.PI / 2 };
      armour = 100;
      loadout = LOADOUTS[round % LOADOUTS.length];
      say("ROUND " + round + " — " + loadout);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      cooldown = 0;
      kick = 0;
      flash = 0;
      hurt = 0;
      messageAge = 0;
      startRound();
    };

    function alive() {
      return crew.filter(function (c) { return !c.dead; });
    }

    /* One radius, read by both the defuse and the "does this press shoot
       instead" test. They were a manhattan distance apart before, so there
       was a ring around the beacon where holding the key neither
       countersigned nor fired at anything. */
    function atSite() {
      if (phase !== "planted") return false;
      var dx = cam.x - site.x;
      var dy = cam.y - site.y;
      return dx * dx + dy * dy < 1.7;
    }

    function sees(from, to) {
      var dx = to.x - from.x;
      var dy = to.y - from.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 0.001) return 0;
      return caster.cast(from.x, from.y, dx / dist, dy / dist).dist >= dist - 0.2
        ? dist
        : 0;
    }

    function loseRound(reason) {
      game.lives -= 1;
      say(reason);
      if (game.lives <= 0) {
        game.over = true;
        return;
      }
      startRound();
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      kick = Math.max(0, kick - dt * 4);
      flash = Math.max(0, flash - dt * 6);
      hurt = Math.max(0, hurt - dt);
      cooldown = Math.max(0, cooldown - dt);

      var turn = 2.4 * dt;
      if (input.left) cam.angle -= turn;
      if (input.right) cam.angle += turn;
      cam.dirX = Math.cos(cam.angle);
      cam.dirY = Math.sin(cam.angle);

      var step = 2.2 * dt;
      if (input.up) casterMove(caster, cam, cam.dirX * step, cam.dirY * step);
      if (input.down) casterMove(caster, cam, -cam.dirX * step, -cam.dirY * step);

      // ---- the plant -------------------------------------------------
      if (phase === "hunt") {
        timer -= dt;
        if (!alive().length) {
          game.score += 100 + round * 20;
          round += 1;
          say("CREW ELIMINATED PRE-PLANT — ROUND WON");
          startRound();
          return;
        }
        if (timer <= 0) {
          phase = "planted";
          // Planted at whichever site the crew is closest to, so the round
          // reads as a decision the crew made rather than a coin flip.
          var planter = alive()[0];
          site = SITES[0];
          SITES.forEach(function (candidate) {
            var here = Math.abs(candidate.x - planter.x) + Math.abs(candidate.y - planter.y);
            var there = Math.abs(site.x - planter.x) + Math.abs(site.y - planter.y);
            if (here < there) site = candidate;
          });
          say("UNSIGNED ACTION PLANTED — " + site.name);
        }
      } else if (phase === "planted") {
        fuse -= dt;
        if (fuse <= 0) {
          loseRound("FUSE EXPIRED — THE ACTION EXECUTED UNSIGNED");
          return;
        }

        if (atSite() && input.fire) {
          // Holding fire on the site countersigns instead of shooting: one
          // key, and standing on the thing is the only place the meaning
          // changes, so it cannot be pressed by accident somewhere it matters.
          defuse += dt;
          if (defuse >= DEFUSE_TIME) {
            game.score += 250 + Math.floor(fuse) * 5;
            round += 1;
            say("COUNTERSIGNED WITH " + Math.ceil(fuse) + "s LEFT");
            startRound();
            return;
          }
        } else if (defuse > 0) {
          // Interrupting drains rather than resets: a defuse you had to break
          // off to shoot somebody is the round the genre is actually about.
          defuse = Math.max(0, defuse - dt * 0.6);
        }
      }

      // ---- shooting --------------------------------------------------
      if (input.fire && !atSite() && cooldown <= 0) {
        cooldown = 0.22;
        kick = 1;
        flash = 1;
        var hit = casterHitscan(cam, alive(), caster);
        if (hit) {
          hit.sprite.dead = true;
          game.score += 25;
        }
      }

      // ---- the crew --------------------------------------------------
      alive().forEach(function (c) {
        var target = phase === "planted" ? { x: site.x, y: site.y } : cam;
        var dx = target.x - c.x;
        var dy = target.y - c.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        // Defending the plant rather than chasing: once it is down the crew
        // wants the fuse to run, so they hold the site instead of the player.
        //
        // And they stop at their standoff instead of walking into you. A
        // hostile at arm's length is not a harder shot, it is an impossible
        // one: the hitscan cone is a fixed angle, so the closer a target
        // stands the less of it that cone covers, and a crew that hugged the
        // player made the cabinet unwinnable rather than difficult.
        var hold = phase === "planted" ? 1.4 : c.standoff;
        var drive = dist > hold ? 1 : dist < hold - 0.6 ? -0.7 : 0;
        if (drive) {
          casterMove(caster, c, (dx / dist) * c.speed * drive * dt, (dy / dist) * c.speed * drive * dt);
        }

        c.shootTimer -= dt;
        if (c.shootTimer > 0) return;
        c.shootTimer = 1.4 + random() * 1.6;
        var range = sees(c, cam);
        if (!range || range > 9) return;
        // Range falloff, so a corridor duel is winnable and a point-blank
        // surprise is not.
        if (random() > 0.5 - range * 0.03) return;
        hurt = 0.5;
        // Armour, not an instant retry: a round-based shooter that spent a
        // life per bullet ended the run before the first plant ever landed.
        armour -= 18 + Math.floor(random() * 14);
        if (armour > 0) return;
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        say("TAKEN — RETRY, SAME ROUND");
        startRound();
      });
    };

    game.draw = function (ctx, ink) {
      var sprites = alive().map(function (c) {
        return { x: c.x, y: c.y, color: ink.danger, label: c.label, scale: c.scale };
      });
      if (phase === "planted") {
        sprites.push({
          x: site.x,
          y: site.y,
          color: fuse < 8 && Math.floor(fuse * 4) % 2 === 0 ? ink.bright : ink.brass,
          label: "UNSIGNED",
          scale: 0.42,
          lift: 40
        });
      }

      var zbuf = casterDrawWalls(ctx, ink, caster, cam);
      casterDrawSprites(ctx, ink, cam, sprites, zbuf);
      casterDrawWeapon(ctx, ink, kick, flash);
      drawMinimap(ctx, ink, caster, cam, sprites);

      if (hurt > 0) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(0, 0, W, 3);
        ctx.fillRect(0, H - 3, W, 3);
      }

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("CREW " + alive().length, 4, 10);
      ctx.fillStyle = ink.grid;
      ctx.fillRect(4, H - 10, 72, 6);
      ctx.fillStyle = armour > 35 ? ink.verify : ink.danger;
      ctx.fillRect(4, H - 10, Math.max(0, Math.floor(armour * 0.72)), 6);

      if (phase === "planted") {
        ctx.fillStyle = fuse < 8 ? ink.danger : ink.brass;
        ctx.fillText("TTL " + fuse.toFixed(1) + "s", 4, 20);
        if (defuse > 0) {
          var width = Math.floor((defuse / DEFUSE_TIME) * 120);
          ctx.fillStyle = ink.grid;
          ctx.fillRect(W / 2 - 60, H - 52, 120, 6);
          ctx.fillStyle = ink.verify;
          ctx.fillRect(W / 2 - 60, H - 52, width, 6);
          ctx.textAlign = "center";
          ctx.fillStyle = ink.verify;
          ctx.fillText("COUNTERSIGNING", W / 2, H - 56);
          ctx.textAlign = "left";
        }
      } else {
        ctx.fillStyle = ink.verify;
        ctx.fillText("PLANT IN " + Math.max(0, timer).toFixed(1) + "s", 4, 20);
      }

      if (messageAge < 2.6) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 34);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "ROUND " + round + " · ARMOUR " + Math.max(0, armour);
    };
    return game;
  }

  /* ---- 14. HAPPY PATH ----------------------------------------------------

     The side-scrolling platformer, which is the console shape everything else
     in the roster is measured against. You are a request, running left to
     right along the path the demo took. The gaps are the cases nobody wrote,
     the regressions patrol the platforms, and landing on one squashes it —
     touching one at ground level does not. The flag at the end is a release.

     Level geometry is generated once per stage from the seeded generator, so
     a stage is the same stage every time it is played at the same seed, which
     is what makes a platformer learnable rather than merely random. */
  function cabinetHappyPath(random) {
    var GROUND = H - 34;
    var GRAVITY = 520;
    var JUMP = -196;
    var RUN = 96;

    var game = { id: "happy-path", score: 0, lives: 3, over: false };
    var runner, camX, stage, platforms, gaps, hazards, coins, flagX,
      message, messageAge, clock;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function buildStage() {
      platforms = [];
      gaps = [];
      hazards = [];
      coins = [];
      var x = 60;
      var length = 1400 + stage * 260;

      while (x < length) {
        // A gap, then a run of solid path, then the furniture standing on it.
        var gapWidth = 26 + Math.floor(random() * (16 + stage * 4));
        gaps.push({ x: x, w: gapWidth });
        x += gapWidth;

        var runWidth = 110 + Math.floor(random() * 120);
        if (random() < 0.55) {
          var ledgeX = x + 20 + random() * (runWidth - 60);
          var ledgeY = GROUND - 34 - Math.floor(random() * 30);
          platforms.push({ x: ledgeX, y: ledgeY, w: 46 + random() * 30 });
          coins.push({ x: ledgeX + 18, y: ledgeY - 12, taken: false });
        }
        if (random() < 0.75) {
          hazards.push({
            x: x + 30 + random() * (runWidth - 50),
            y: GROUND - 10,
            dir: random() < 0.5 ? -1 : 1,
            span: 24 + random() * 30,
            home: 0,
            dead: false
          });
        }
        coins.push({ x: x + runWidth / 2, y: GROUND - 24, taken: false });
        x += runWidth;
      }
      hazards.forEach(function (h) { h.home = h.x; });
      flagX = x + 40;
      // A stage clock, because a platformer without one is a place to stand
      // rather than a level. Generous, and it scales with the stage length.
      clock = 55 + stage * 6;
      camX = 0;
      runner = { x: 30, y: GROUND - 12, vy: 0, w: 8, h: 12, onGround: true, face: 1 };
    }

    function solidAt(x) {
      // The floor exists everywhere except inside a gap. One pass, because a
      // platformer that asks "am I over a hole" on every frame for every body
      // is the cheapest thing in the file and the one that has to be right.
      for (var i = 0; i < gaps.length; i += 1) {
        if (x > gaps[i].x && x < gaps[i].x + gaps[i].w) return false;
      }
      return true;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      stage = 1;
      messageAge = 0;
      buildStage();
      say("STAGE 1 — SHIP IT");
    };

    function fall() {
      game.lives -= 1;
      if (game.lives <= 0) {
        game.over = true;
        return;
      }
      say("FELL THROUGH AN UNHANDLED CASE");
      runner.x = Math.max(30, runner.x - 90);
      runner.y = GROUND - 40;
      runner.vy = 0;
      // Nudge out of the hole rather than respawning back into it.
      while (!solidAt(runner.x) && runner.x > 20) runner.x -= 6;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      clock -= dt;
      if (clock <= 0) {
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        buildStage();
        say("TIMED OUT — THE RELEASE SLIPPED");
        return;
      }

      if (input.left) { runner.x -= RUN * dt; runner.face = -1; }
      if (input.right) { runner.x += RUN * dt; runner.face = 1; }
      if (runner.x < 4) runner.x = 4;

      if (input.fire && runner.onGround) {
        runner.vy = JUMP;
        runner.onGround = false;
      }

      runner.vy += GRAVITY * dt;
      runner.y += runner.vy * dt;
      runner.onGround = false;

      // Ledges first: a ledge over a gap is the only thing keeping the player
      // out of it, so it has to win the collision.
      platforms.forEach(function (p) {
        if (runner.x + 4 < p.x || runner.x - 4 > p.x + p.w) return;
        if (runner.vy < 0) return;
        if (runner.y >= p.y - 12 && runner.y <= p.y + 6) {
          runner.y = p.y - 12;
          runner.vy = 0;
          runner.onGround = true;
        }
      });

      if (!runner.onGround && runner.y >= GROUND - 12 && solidAt(runner.x)) {
        runner.y = GROUND - 12;
        runner.vy = 0;
        runner.onGround = true;
      }

      if (runner.y > H + 20) {
        fall();
        return;
      }

      hazards.forEach(function (h) {
        if (h.dead) return;
        h.x += h.dir * 26 * dt;
        if (h.x < h.home - h.span || h.x > h.home + h.span) h.dir *= -1;
        if (!solidAt(h.x)) h.dir *= -1;

        var dx = Math.abs(h.x - runner.x);
        var dy = h.y - runner.y;
        if (dx > 8 || dy < 0 || dy > 16) return;
        if (runner.vy > 40) {
          // Stomped: descending onto it is a fix, walking into it is not.
          h.dead = true;
          runner.vy = JUMP * 0.7;
          game.score += 15;
          return;
        }
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        say("REGRESSION HIT THE REQUEST");
        runner.x = Math.max(20, runner.x - 40);
      });

      coins.forEach(function (c) {
        if (c.taken) return;
        if (Math.abs(c.x - runner.x) > 8 || Math.abs(c.y - runner.y) > 12) return;
        c.taken = true;
        game.score += 5;
      });

      if (runner.x > flagX) {
        stage += 1;
        game.score += 120;
        buildStage();
        say("STAGE " + stage + " — THE PATH GOT LONGER");
      }

      camX = clamp(runner.x - 96, 0, Math.max(0, flagX - W + 60));
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      // Parallax skyline: one band at a third of the scroll rate, drawn as
      // blocks because a skyline at this palette is blocks.
      ctx.fillStyle = ink.grid;
      for (var b = -1; b < 14; b += 1) {
        var bx = b * 40 - (camX * 0.3) % 40;
        ctx.fillRect(bx, GROUND - 60 - (b % 3) * 14, 26, 60 + (b % 3) * 14);
      }

      ctx.fillStyle = ink.wall[2];
      for (var x = 0; x < W; x += 4) {
        if (!solidAt(x + camX)) continue;
        ctx.fillRect(x, GROUND, 4, H - GROUND);
      }
      ctx.fillStyle = ink.wall[1];
      for (var t = 0; t < W; t += 4) {
        if (!solidAt(t + camX)) continue;
        ctx.fillRect(t, GROUND, 4, 3);
      }

      platforms.forEach(function (p) {
        var px = p.x - camX;
        if (px < -80 || px > W) return;
        ctx.fillStyle = ink.brass;
        ctx.fillRect(px, p.y, p.w, 4);
        ctx.fillStyle = ink.wall[2];
        ctx.fillRect(px, p.y + 4, p.w, 3);
      });

      coins.forEach(function (c) {
        if (c.taken) return;
        var cx = c.x - camX;
        if (cx < -8 || cx > W) return;
        ctx.fillStyle = ink.bright;
        ctx.fillRect(cx - 2, c.y - 3, 4, 6);
      });

      hazards.forEach(function (h) {
        if (h.dead) return;
        var hx = h.x - camX;
        if (hx < -12 || hx > W) return;
        ctx.fillStyle = ink.danger;
        ctx.fillRect(hx - 5, h.y - 6, 10, 10);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(hx - 3, h.y - 4, 2, 2);
        ctx.fillRect(hx + 1, h.y - 4, 2, 2);
      });

      var fx = flagX - camX;
      if (fx < W + 20) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(fx, GROUND - 44, 2, 44);
        ctx.fillRect(fx + 2, GROUND - 44, 14, 9);
      }

      var rx = Math.floor(runner.x - camX);
      var ry = Math.floor(runner.y);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(rx - 4, ry, 8, 12);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(rx + (runner.face > 0 ? 0 : -3), ry + 3, 3, 2);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = clock < 10 ? ink.danger : ink.dim;
      ctx.fillText(Math.max(0, Math.ceil(clock)) + "s", W - 30, 12);
      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 22);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "STAGE " + stage + " · " + Math.max(0, Math.ceil(clock)) + "s TO SHIP";
    };
    return game;
  }

  /* ---- 15. LEAST PRIVILEGE -----------------------------------------------

     The top-down room-at-a-time adventure. A three-by-three grid of rooms,
     one screen each, no scrolling between them: you leave through a doorway
     and the next room is simply there, which is what this genre did before
     hardware could scroll a world. Keys are scoped permits, the locked room
     is the vault, and the blade is a revocation — swing it at a daemon and
     the grant it was holding goes away.

     Rooms are generated from the seeded generator but their doorways are not:
     a doorway that generated itself into a wall makes a room a dead end the
     player cannot tell from a puzzle. Every adjacency gets a real opening. */
  function cabinetLeastPrivilege(random) {
    var COLS = 15;
    var ROWS = 11;
    var TILE = 16;
    var OX = (W - COLS * TILE) / 2;
    var OY = 28;
    var GRID = 3; // rooms per side

    var game = { id: "least-privilege", score: 0, lives: 3, over: false };
    var rooms, here, hero, blade, keys, floorNo, message, messageAge, hurt;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function roomAt(rx, ry) {
      return rooms[ry * GRID + rx];
    }

    function buildFloor() {
      rooms = [];
      for (var ry = 0; ry < GRID; ry += 1) {
        for (var rx = 0; rx < GRID; rx += 1) {
          var tiles = [];
          for (var y = 0; y < ROWS; y += 1) {
            var row = [];
            for (var x = 0; x < COLS; x += 1) {
              var edge = x === 0 || y === 0 || x === COLS - 1 || y === ROWS - 1;
              // Interior pillars, never on the centre lines the doorways open
              // onto, so a room can always be crossed both ways.
              var pillar =
                !edge &&
                x % 3 === 0 &&
                y % 3 === 0 &&
                y !== (ROWS - 1) / 2 &&
                x !== (COLS - 1) / 2 &&
                random() < 0.55;
              row.push(edge || pillar ? 1 : 0);
            }
            tiles.push(row);
          }
          // Doorways on every edge that has a neighbour.
          if (ry > 0) tiles[0][(COLS - 1) / 2] = 0;
          if (ry < GRID - 1) tiles[ROWS - 1][(COLS - 1) / 2] = 0;
          if (rx > 0) tiles[(ROWS - 1) / 2][0] = 0;
          if (rx < GRID - 1) tiles[(ROWS - 1) / 2][COLS - 1] = 0;

          var isStart = rx === 0 && ry === 0;
          var isVault = rx === GRID - 1 && ry === GRID - 1;
          var daemons = [];
          var count = isStart ? 0 : 1 + Math.floor(random() * 2) + Math.floor(floorNo / 2);
          for (var d = 0; d < Math.min(4, count); d += 1) {
            daemons.push({
              x: 3 + Math.floor(random() * (COLS - 6)),
              y: 2 + Math.floor(random() * (ROWS - 4)),
              dx: random() < 0.5 ? -1 : 1,
              dy: 0,
              cool: random(),
              dead: false
            });
          }
          rooms.push({
            tiles: tiles,
            daemons: daemons,
            // A key in most rooms that are neither the start nor the vault.
            // Which rooms is a roll; *how many* is not — see below.
            key: !isStart && !isVault && random() < 0.6,
            vault: isVault,
            opened: false,
            cleared: false
          });
        }
      }

      // The vault wants two permits and `keys` resets with the floor, so a
      // floor that rolled fewer than two is a floor with no way out — about
      // one in fifty at p=0.6 over seven rooms, which is often enough that a
      // player would meet it and read it as the game being broken. Top the
      // supply up rather than raising p: the distribution is the texture, the
      // floor being finishable is the contract.
      var carriers = rooms.filter(function (r) { return !r.vault && r !== rooms[0]; });
      var supply = carriers.filter(function (r) { return r.key; });
      while (supply.length < 2 && supply.length < carriers.length) {
        var empty = carriers.filter(function (r) { return !r.key; });
        var pick = empty[Math.floor(random() * empty.length)];
        pick.key = true;
        supply.push(pick);
      }
      here = { x: 0, y: 0 };
      hero = { x: 7, y: 5, face: 0 };
      keys = 0;
      blade = { active: 0, dir: 0 };
    }

    function solid(tx, ty) {
      var room = roomAt(here.x, here.y);
      if (ty < 0 || tx < 0 || ty >= ROWS || tx >= COLS) return true;
      return room.tiles[ty][tx] === 1;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      floorNo = 1;
      hurt = 0;
      messageAge = 0;
      buildFloor();
      say("FLOOR 1 — FIND TWO PERMITS, THEN THE VAULT");
    };

    function enter(dx, dy) {
      here.x = clamp(here.x + dx, 0, GRID - 1);
      here.y = clamp(here.y + dy, 0, GRID - 1);
      // Arrive just inside the opposite doorway.
      if (dx > 0) hero.x = 1;
      if (dx < 0) hero.x = COLS - 2;
      if (dy > 0) hero.y = 1;
      if (dy < 0) hero.y = ROWS - 2;
    }

    var moveCool = 0;

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      hurt = Math.max(0, hurt - dt);
      moveCool -= dt;
      blade.active = Math.max(0, blade.active - dt);

      var room = roomAt(here.x, here.y);

      // Grid-stepped movement on a cooldown, not free pixels: this genre's
      // rooms are read as tiles, and a body that stands between two of them
      // makes every collision above ambiguous.
      if (moveCool <= 0) {
        var mx = 0;
        var my = 0;
        if (input.left) mx = -1;
        else if (input.right) mx = 1;
        else if (input.up) my = -1;
        else if (input.down) my = 1;

        if (mx || my) {
          blade.dir = mx ? (mx > 0 ? 1 : 3) : (my > 0 ? 2 : 0);
          hero.face = blade.dir;
          var nx = hero.x + mx;
          var ny = hero.y + my;
          moveCool = 0.11;

          if (nx < 0 || nx >= COLS || ny < 0 || ny >= ROWS) {
            enter(mx, my);
          } else if (!solid(nx, ny)) {
            hero.x = nx;
            hero.y = ny;
          }
        }
      }

      if (input.fire && blade.active <= 0) {
        blade.active = 0.18;
        var bx = hero.x + (blade.dir === 1 ? 1 : blade.dir === 3 ? -1 : 0);
        var by = hero.y + (blade.dir === 2 ? 1 : blade.dir === 0 ? -1 : 0);
        room.daemons.forEach(function (d) {
          if (d.dead) return;
          if (d.x !== bx || d.y !== by) return;
          d.dead = true;
          game.score += 20;
        });
      }

      room.daemons.forEach(function (d) {
        if (d.dead) return;
        d.cool -= dt;
        if (d.cool <= 0) {
          d.cool = 0.34;
          // Drifts toward the player on one axis, bounces off walls on the
          // other. Cheap, and it reads as a patrol that has noticed you.
          var towardX = hero.x > d.x ? 1 : hero.x < d.x ? -1 : 0;
          var towardY = hero.y > d.y ? 1 : hero.y < d.y ? -1 : 0;
          var stepX = random() < 0.6 ? towardX : d.dx;
          var stepY = stepX ? 0 : towardY;
          if (!solid(d.x + stepX, d.y + stepY)) {
            d.x += stepX;
            d.y += stepY;
          } else {
            d.dx *= -1;
          }
        }
        if (d.x === hero.x && d.y === hero.y && hurt <= 0) {
          hurt = 1.1;
          game.lives -= 1;
          if (game.lives <= 0) {
            game.over = true;
            return;
          }
          say("A DAEMON TOOK A GRANT BACK");
        }
      });

      if (!room.daemons.some(function (d) { return !d.dead; }) && !room.cleared) {
        room.cleared = true;
        game.score += 30;
      }

      if (room.key && room.cleared) {
        room.key = false;
        keys += 1;
        game.score += 40;
        say("SCOPED PERMIT " + keys + " — EXPIRES ON USE");
      }

      if (room.vault && !room.opened) {
        if (keys >= 2) {
          room.opened = true;
          keys -= 2;
          floorNo += 1;
          game.score += 200;
          buildFloor();
          say("VAULT SIGNED — FLOOR " + floorNo);
        } else if (messageAge > 1.6) {
          say("VAULT NEEDS TWO PERMITS. YOU HAVE " + keys + ".");
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      var room = roomAt(here.x, here.y);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          var px = OX + x * TILE;
          var py = OY + y * TILE;
          if (room.tiles[y][x]) {
            // Walls in the brightest wall shade with a dark mortar line. At
            // wall[2] against a grid-coloured floor checker the two sat one
            // step apart in value and the room read as a single chequerboard
            // with no walls in it — the doorways especially disappeared.
            ctx.fillStyle = room.vault ? ink.brass : ink.wall[1];
            ctx.fillRect(px, py, TILE, TILE);
            ctx.fillStyle = ink.bg;
            ctx.fillRect(px + TILE - 2, py, 2, TILE);
            ctx.fillRect(px, py + TILE - 2, TILE, 2);
          } else if ((x + y) % 2 === 0) {
            // Floor: a pip rather than a filled cell, so the pattern says
            // "tiles" without competing with the walls for brightness.
            ctx.fillStyle = ink.grid;
            ctx.fillRect(px + TILE / 2 - 1, py + TILE / 2 - 1, 2, 2);
          }
        }
      }

      if (room.key && room.cleared === false) {
        ctx.fillStyle = ink.dim;
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText("CLEAR THE ROOM TO RELEASE THE PERMIT", W / 2, OY + ROWS * TILE + 10);
        ctx.textAlign = "left";
      }

      room.daemons.forEach(function (d) {
        if (d.dead) return;
        ctx.fillStyle = ink.danger;
        ctx.fillRect(OX + d.x * TILE + 2, OY + d.y * TILE + 2, TILE - 4, TILE - 4);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(OX + d.x * TILE + 5, OY + d.y * TILE + 6, 2, 2);
        ctx.fillRect(OX + d.x * TILE + 9, OY + d.y * TILE + 6, 2, 2);
      });

      var hx = OX + hero.x * TILE;
      var hy = OY + hero.y * TILE;
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.bright : ink.verify;
      ctx.fillRect(hx + 3, hy + 2, TILE - 6, TILE - 4);

      if (blade.active > 0) {
        var bx = hx + (blade.dir === 1 ? TILE : blade.dir === 3 ? -TILE : 0);
        var by = hy + (blade.dir === 2 ? TILE : blade.dir === 0 ? -TILE : 0);
        ctx.fillStyle = ink.bright;
        ctx.fillRect(bx + 5, by + 5, TILE - 10, TILE - 10);
      }

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("PERMITS " + keys, 4, 12);
      ctx.fillText("ROOM " + (here.x + 1) + "," + (here.y + 1), W - 62, 12);
      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 22);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "FLOOR " + floorNo + " · PERMITS " + keys;
    };
    return game;
  }

  /* ---- 16. ESCALATION ----------------------------------------------------

     The turn-based console RPG, reduced to the one screen it is actually
     remembered as: a menu, a monster, and two numbers going down. The party
     is you; the encounters are the things a request meets on its way through
     a trust plane, and they escalate because that is what an unreviewed
     request does. Four verbs, one key to commit them, and a damage roll the
     player can feel the shape of after two fights.

     Menu-driven rather than twitch, which also makes this the one cabinet on
     the roster a person can play badly and still finish a thought during. */
  function cabinetEscalation(random) {
    var ACTIONS = [
      { name: "REQUEST", note: "a plain call. cheap, honest, small." },
      { name: "ESCALATE", note: "more privilege than you need. it shows." },
      { name: "REVOKE", note: "take a grant back. hurts what holds one." },
      { name: "ROTATE", note: "new keys. heals you. wastes a turn." }
    ];
    var FOES = [
      { name: "POLICY DAEMON", hp: 40, hit: 7 },
      { name: "RATE LIMITER", hp: 55, hit: 9 },
      { name: "AUDIT BACKLOG", hp: 70, hit: 11 },
      { name: "BUDGET OWNER", hp: 90, hit: 14 },
      { name: "THE COMMITTEE", hp: 120, hit: 17 }
    ];

    var game = { id: "escalation", score: 0, lives: 3, over: false };
    var hero, foe, tier, pick, phase, timer, log, shake, held;

    function nextFoe() {
      var base = FOES[Math.min(FOES.length - 1, tier - 1)];
      var over = Math.max(0, tier - FOES.length);
      foe = {
        name: base.name + (over ? " +" + over : ""),
        hp: base.hp + over * 26,
        max: base.hp + over * 26,
        hit: base.hit + over * 3,
        guard: 0
      };
    }

    function write(line) {
      log.unshift(line);
      if (log.length > 3) log.pop();
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      tier = 1;
      pick = 0;
      phase = "choose";
      timer = 0;
      shake = 0;
      held = false;
      log = [];
      hero = { hp: 60, max: 60, mp: 5 };
      nextFoe();
      write("A " + foe.name + " BLOCKS THE CALL.");
    };

    function heroTurn() {
      var action = ACTIONS[pick];
      var roll = Math.floor(random() * 6);

      if (action.name === "REQUEST") {
        var dmg = 8 + roll;
        foe.hp -= dmg;
        write("REQUEST LANDS FOR " + dmg + ".");
      } else if (action.name === "ESCALATE") {
        // High roll, real cost: the whole point of the verb is that it works
        // and that using it is visible afterwards.
        var big = 18 + roll * 2;
        foe.hp -= big;
        hero.hp -= 6;
        game.score += 5;
        write("ESCALATED FOR " + big + ". THE AUDIT NOTICED.");
      } else if (action.name === "REVOKE") {
        var cut = 10 + roll;
        foe.hp -= cut;
        foe.guard = 2;
        write("REVOKED A GRANT. -" + cut + ", AND IT IS SLOWER.");
      } else {
        if (hero.mp <= 0) {
          write("NO KEYS LEFT TO ROTATE. TURN WASTED.");
        } else {
          hero.mp -= 1;
          hero.hp = Math.min(hero.max, hero.hp + 18);
          write("ROTATED KEYS. +18, AND ONE FEWER SPARE.");
        }
      }

      if (foe.hp <= 0) {
        game.score += 60 + tier * 20;
        tier += 1;
        hero.mp += 1;
        hero.hp = Math.min(hero.max, hero.hp + 10);
        nextFoe();
        write("CLEARED. NEXT: " + foe.name + ".");
        phase = "choose";
        return;
      }
      phase = "foe";
      timer = 0.7;
    }

    function foeTurn() {
      if (foe.guard > 0) {
        foe.guard -= 1;
        write(foe.name + " IS STILL RE-READING THE POLICY.");
        phase = "choose";
        return;
      }
      var dmg = Math.floor(foe.hit * (0.7 + random() * 0.6));
      hero.hp -= dmg;
      shake = 0.3;
      write(foe.name + " DENIES YOU FOR " + dmg + ".");
      if (hero.hp <= 0) {
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        hero.hp = hero.max;
        hero.mp = Math.max(1, hero.mp);
        write("THE CALL DIED. RETRY WITH THE SAME PARTY.");
      }
      phase = "choose";
    }

    game.update = function (dt, input) {
      if (game.over) return;
      shake = Math.max(0, shake - dt);

      if (phase === "foe") {
        timer -= dt;
        if (timer <= 0) foeTurn();
        return;
      }

      // Edge-triggered menu: a held key that repeated every frame would run
      // the cursor down the list and commit something the player never chose.
      var pressed = input.up || input.down || input.fire;
      if (!pressed) {
        held = false;
        return;
      }
      if (held) return;
      held = true;

      if (input.up) pick = (pick + ACTIONS.length - 1) % ACTIONS.length;
      else if (input.down) pick = (pick + 1) % ACTIONS.length;
      else heroTurn();
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      var jolt = shake > 0 ? (Math.floor(shake * 40) % 2 ? 2 : -2) : 0;

      // The foe, as a slab with a face. A sprite this size at this palette is
      // read as a silhouette anyway.
      var fw = 74;
      var fh = 54;
      ctx.fillStyle = ink.danger;
      ctx.fillRect(W / 2 - fw / 2 + jolt, 34, fw, fh);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(W / 2 - 22 + jolt, 50, 12, 8);
      ctx.fillRect(W / 2 + 10 + jolt, 50, 12, 8);
      ctx.fillRect(W / 2 - 18 + jolt, 72, 36, 4);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = ink.ink;
      ctx.fillText(foe.name, W / 2, 26);
      ctx.textAlign = "left";

      // Foe health as a bar, because a number nobody can read at 8px is not a
      // health bar.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(W / 2 - 50, 94, 100, 5);
      ctx.fillStyle = ink.danger;
      ctx.fillRect(W / 2 - 50, 94, Math.max(0, Math.floor((foe.hp / foe.max) * 100)), 5);

      // Log panel.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(6, 104, W - 12, 34);
      ctx.fillStyle = ink.ink;
      for (var i = 0; i < log.length; i += 1) {
        ctx.fillText(log[i], 10, 115 + i * 10);
      }

      // Menu.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(6, 144, 150, 52);
      ACTIONS.forEach(function (action, index) {
        var y = 156 + index * 12;
        if (index === pick) {
          ctx.fillStyle = ink.brass;
          ctx.fillRect(8, y - 8, 146, 11);
          ctx.fillStyle = ink.bg;
        } else {
          ctx.fillStyle = ink.ink;
        }
        ctx.fillText(action.name, 14, y);
      });
      ctx.fillStyle = ink.dim;
      ctx.fillText(ACTIONS[pick].note, 10, 208);

      // Party panel.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(164, 144, W - 170, 52);
      ctx.fillStyle = ink.ink;
      ctx.fillText("YOU", 170, 156);
      ctx.fillStyle = ink.grid;
      ctx.fillRect(170, 162, 120, 5);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(170, 162, Math.max(0, Math.floor((hero.hp / hero.max) * 120)), 5);
      ctx.fillStyle = ink.dim;
      ctx.fillText("HP " + Math.max(0, hero.hp) + "/" + hero.max, 170, 178);
      ctx.fillText("SPARE KEYS " + hero.mp, 170, 190);

      ctx.fillStyle = ink.dim;
      ctx.fillText(phase === "foe" ? "…THINKING" : "↑ ↓ CHOOSE · SPACE COMMIT", 10, 228);
    };

    game.hud = function () {
      return "TIER " + tier + " · HP " + Math.max(0, hero.hp);
    };
    return game;
  }

  /* ---- 17. ARBITRATION ---------------------------------------------------

     The one-on-one fighter. Two parties to a dispute, two health bars, a
     round timer, and best of three. The genre's real subject is spacing, so
     that is what this models: a strike has reach and recovery, a block eats
     most of a hit but not the chip, and the opponent reads your distance
     rather than rolling dice.

     Five keys means no motion inputs and no special moves. What is left is
     the part of a fighter that survives at 8 bits: get in, hit, get out. */
  function cabinetArbitration(random) {
    var FLOOR = H - 42;
    var REACH = 26;

    var game = { id: "arbitration", score: 0, lives: 3, over: false };
    var you, foe, round, wins, losses, clock, freeze, message, messageAge, tier;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function fighter(x, face) {
      return {
        x: x,
        vy: 0,
        y: FLOOR,
        face: face,
        hp: 100,
        strike: 0,   // >0 while the arm is out
        recover: 0,  // >0 while it cannot strike again
        block: false,
        stun: 0,
        landed: false
      };
    }

    function newRound() {
      you = fighter(80, 1);
      foe = fighter(240, -1);
      clock = 45;
      freeze = 0.8;
      say("ROUND " + round + " — ARGUE");
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      wins = 0;
      losses = 0;
      tier = 1;
      messageAge = 0;
      newRound();
    };

    function hitCheck(attacker, defender) {
      if (attacker.strike <= 0 || attacker.landed) return;
      var gap = Math.abs(attacker.x - defender.x);
      var facing = (defender.x - attacker.x) * attacker.face > 0;
      if (!facing || gap > REACH) return;
      attacker.landed = true;
      // Blocking is worth having and is not a wall: chip damage keeps a
      // turtling opponent from being a stalemate, which is the failure mode
      // this genre spent a decade fixing.
      var dmg = defender.block ? 3 : 9 + Math.floor(random() * 4);
      defender.hp -= dmg;
      defender.stun = defender.block ? 0.12 : 0.3;
      defender.x += attacker.face * (defender.block ? 3 : 7);
      if (attacker === you) game.score += defender.block ? 1 : 4;
    }

    function endRound(playerWon) {
      if (playerWon) {
        wins += 1;
        game.score += 80;
        say("AWARD TO YOU");
      } else {
        losses += 1;
        say("AWARD AGAINST YOU");
      }
      if (wins >= 2) {
        tier += 1;
        wins = 0;
        losses = 0;
        round = 1;
        game.score += 150;
        say("MATCH WON — NEXT ARBITER");
        newRound();
        return;
      }
      if (losses >= 2) {
        game.lives -= 1;
        wins = 0;
        losses = 0;
        round = 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        say("MATCH LOST — RETRY");
        newRound();
        return;
      }
      round += 1;
      newRound();
    }

    function stepBody(body, dt) {
      body.strike = Math.max(0, body.strike - dt);
      body.recover = Math.max(0, body.recover - dt);
      body.stun = Math.max(0, body.stun - dt);
      if (body.strike <= 0) body.landed = false;
      body.y += body.vy * dt;
      if (body.y < FLOOR) body.vy += 900 * dt;
      if (body.y >= FLOOR) {
        body.y = FLOOR;
        body.vy = 0;
      }
      body.x = clamp(body.x, 16, W - 16);
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      if (freeze > 0) {
        freeze -= dt;
        return;
      }

      clock -= dt;
      you.face = foe.x >= you.x ? 1 : -1;
      foe.face = you.x >= foe.x ? 1 : -1;

      // ---- you --------------------------------------------------------
      you.block = !!input.down && you.y >= FLOOR;
      if (!you.stun && !you.block) {
        var speed = 74 * dt;
        if (input.left) you.x -= speed;
        if (input.right) you.x += speed;
        if (input.up && you.y >= FLOOR) you.vy = -260;
        if (input.fire && you.recover <= 0) {
          you.strike = 0.14;
          you.recover = 0.34;
        }
      }

      // ---- the arbiter -------------------------------------------------
      // A distance machine, not a random one: it closes when far, strikes in
      // range, blocks when your arm is out, and backs off after trading. The
      // tier turns the dials rather than adding new behaviour.
      var gap = Math.abs(foe.x - you.x);
      var aggression = 0.5 + tier * 0.12;
      foe.block = you.strike > 0 && gap < REACH + 8 && random() < 0.6;
      if (!foe.stun && !foe.block) {
        var pace = (56 + tier * 9) * dt;
        if (gap > REACH - 4) foe.x += foe.face * pace;
        else if (gap < REACH - 16) foe.x -= foe.face * pace * 0.8;
        if (gap <= REACH && foe.recover <= 0 && random() < aggression * dt * 8) {
          foe.strike = 0.14;
          foe.recover = 0.42 - tier * 0.02;
        }
      }

      stepBody(you, dt);
      stepBody(foe, dt);
      hitCheck(you, foe);
      hitCheck(foe, you);

      if (foe.hp <= 0) {
        freeze = 1;
        endRound(true);
        return;
      }
      if (you.hp <= 0) {
        freeze = 1;
        endRound(false);
        return;
      }
      if (clock <= 0) {
        freeze = 1;
        endRound(you.hp >= foe.hp);
      }
    };

    function drawFighter(ctx, ink, body, color) {
      var x = Math.floor(body.x);
      var y = Math.floor(body.y);
      ctx.fillStyle = body.stun > 0 ? ink.bright : color;
      ctx.fillRect(x - 7, y - 34, 14, 22);          // torso
      ctx.fillRect(x - 5, y - 12, 4, 12);           // legs
      ctx.fillRect(x + 1, y - 12, 4, 12);
      ctx.fillRect(x - 5, y - 44, 10, 10);          // head
      if (body.block) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(x + (body.face > 0 ? 6 : -10), y - 34, 4, 20);
      } else if (body.strike > 0) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(x + (body.face > 0 ? 6 : -24), y - 32, 18, 4);
      }
    }

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      // Crowd band and floor. The crowd is committee members, which is the
      // only joke this cabinet needs in the background.
      ctx.fillStyle = ink.grid;
      for (var c = 0; c < 20; c += 1) {
        ctx.fillRect(c * 17 + 3, 58 + (c % 3) * 3, 11, 14);
      }
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(0, FLOOR, W, H - FLOOR);
      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(0, FLOOR, W, 3);

      drawFighter(ctx, ink, foe, ink.danger);
      drawFighter(ctx, ink, you, ink.verify);

      // Health bars, drained from the centre outward the way this genre does.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(8, 12, 132, 8);
      ctx.fillRect(W - 140, 12, 132, 8);
      ctx.fillStyle = ink.verify;
      var yw = Math.max(0, Math.floor((you.hp / 100) * 132));
      ctx.fillRect(8 + (132 - yw), 12, yw, 8);
      ctx.fillStyle = ink.danger;
      ctx.fillRect(W - 140, 12, Math.max(0, Math.floor((foe.hp / 100) * 132)), 8);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = ink.brass;
      ctx.fillText(String(Math.max(0, Math.ceil(clock))), W / 2, 20);
      ctx.fillStyle = ink.dim;
      ctx.fillText("YOU " + wins + " — " + losses + " ARBITER", W / 2, 32);
      if (messageAge < 2 || freeze > 0) {
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 48);
      }
      ctx.textAlign = "left";
    };

    game.hud = function () {
      return "ARBITER " + tier + " · ROUND " + round;
    };
    return game;
  }

  /* ---- 18. THROUGHPUT ----------------------------------------------------

     The top-down circuit racer. Whole track on one screen, no scrolling: this
     is the layout the genre used before hardware could rotate a road, and it
     is the one that still reads at 320x240.

     The track is a bitmap of drivable cells. Off it you do not crash, you get
     slow — which is the honest version of the metaphor: exceeding your
     throughput budget does not stop the work, it just makes everything take
     longer, and the rivals do not wait. Checkpoints exist so that reversing
     over the line cannot farm laps. */
  function cabinetThroughput(random) {
    var TRACK = [
      "################################",
      "################################",
      "##............................##",
      "##............................##",
      "##............................##",
      "##...######################...##",
      "##...######################...##",
      "##...######################...##",
      "##...######################...##",
      "##...######################...##",
      "##............................##",
      "##............................##",
      "##............................##",
      "################################",
      "################################"
    ];
    // Drivable is '.', wall/infield is '#': inverted from the raycaster maps
    // on purpose — a circuit is a ring of road drawn inside a solid, and
    // writing it the other way round makes the shape unreadable in source.
    var CELL = 10;
    var COLS = TRACK[0].length;
    var ROWS = TRACK.length;
    var OX = (W - COLS * CELL) / 2;
    var OY = (H - ROWS * CELL) / 2 + 6;
    // Four quadrant checkpoints, cleared in order, running anticlockwise from
    // the bottom straight. The line sits on the bottom straight behind the
    // first of them, so a lap is a lap rather than a reversed dab over it.
    var GATES = [
      { x: 22, y: 11 }, { x: 28, y: 7 }, { x: 10, y: 3 }, { x: 3, y: 7 }
    ];
    var LINE_CELL = { x: 8, y: 11 };

    var game = { id: "throughput", score: 0, lives: 3, over: false };
    var car, rivals, lap, gate, best, clock, budget, message, messageAge;

    function drivable(px, py) {
      var cx = Math.floor((px - OX) / CELL);
      var cy = Math.floor((py - OY) / CELL);
      if (cx < 0 || cy < 0 || cx >= COLS || cy >= ROWS) return false;
      return TRACK[cy].charAt(cx) === ".";
    }

    function cellCentre(cx, cy) {
      return { x: OX + cx * CELL + CELL / 2, y: OY + cy * CELL + CELL / 2 };
    }

    function say(text) {
      message = text;
      messageAge = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      lap = 1;
      gate = 0;
      best = 0;
      clock = 0;
      // A time budget rather than a field that can lap you. Arcade racers ran
      // on an extend clock for a reason: a rival marginally faster than a
      // clean lap means perfect driving still loses, which is not a race, it
      // is a countdown wearing a car.
      budget = 30;
      messageAge = 0;
      var start = cellCentre(LINE_CELL.x, LINE_CELL.y);
      car = { x: start.x, y: start.y, angle: 0, speed: 0 };
      rivals = [];
      for (var i = 0; i < 3; i += 1) {
        var seat = cellCentre(LINE_CELL.x + 1 + i, LINE_CELL.y + (i % 2 ? -1 : 0));
        rivals.push({
          x: seat.x,
          y: seat.y,
          gate: 0,
          lap: 1,
          pace: 34 + i * 6 + random() * 6
        });
      }
      say("GREEN — HOLD YOUR BUDGET");
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      clock += dt;
      budget -= dt;

      if (budget <= 0) {
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        var restart = cellCentre(LINE_CELL.x, LINE_CELL.y);
        car = { x: restart.x, y: restart.y, angle: 0, speed: 0 };
        gate = 0;
        clock = 0;
        budget = 30;
        say("BUDGET EXHAUSTED — BACK TO THE LINE");
        return;
      }

      if (input.left) car.angle -= 2.6 * dt;
      if (input.right) car.angle += 2.6 * dt;

      var onRoad = drivable(car.x, car.y);
      var ceiling = onRoad ? 92 : 34;
      if (input.up) car.speed += 118 * dt;
      else car.speed -= 46 * dt;
      if (input.down) car.speed -= 150 * dt;
      car.speed = clamp(car.speed, 0, ceiling);

      var nx = car.x + Math.cos(car.angle) * car.speed * dt;
      var ny = car.y + Math.sin(car.angle) * car.speed * dt;
      // The infield and the outer field are the same rule: you may leave the
      // road, you may not leave the board.
      if (nx > OX && nx < OX + COLS * CELL) car.x = nx;
      if (ny > OY && ny < OY + ROWS * CELL) car.y = ny;

      var target = cellCentre(GATES[gate].x, GATES[gate].y);
      if (Math.abs(car.x - target.x) < 22 && Math.abs(car.y - target.y) < 22) {
        gate += 1;
        game.score += 10;
        // Capped: without a ceiling a clean driver banks minutes by lap four
        // and the clock stops meaning anything for the rest of the run.
        budget = Math.min(45, budget + 3.5);
        if (gate >= GATES.length) {
          gate = 0;
          lap += 1;
          game.score += 100;
          budget = Math.min(45, budget + 6);
          if (!best || clock < best) best = clock;
          say("LAP " + lap + " — " + clock.toFixed(1) + "s · EXTENDED");
          clock = 0;
        }
      }

      rivals.forEach(function (r) {
        // Rivals drive the gate ring rather than the road: an opponent that
        // needs its own physics is an opponent that gets stuck on the infield
        // while the player watches, which is worse than one that cheats.
        var to = cellCentre(GATES[r.gate].x, GATES[r.gate].y);
        var dx = to.x - r.x;
        var dy = to.y - r.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        r.x += (dx / dist) * r.pace * dt;
        r.y += (dy / dist) * r.pace * dt;
        if (dist < 12) {
          r.gate = (r.gate + 1) % GATES.length;
          if (r.gate === 0) r.lap += 1;
        }

        // Rivals are traffic, not a timer: touching one scrubs your speed,
        // which costs budget, which is the only currency here.
        if (Math.abs(r.x - car.x) < 8 && Math.abs(r.y - car.y) < 8) {
          car.speed *= 0.35;
        }
      });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      // The whole board is painted, not just the road: an unpainted verge is
      // the same black as the void outside the board, so a car that left the
      // circuit appeared to be driving on nothing. And the road's two checker
      // shades have to be two shades — grid and wall[3] are the same colour in
      // this palette, so the surface came out flat.
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          var px = OX + x * CELL;
          var py = OY + y * CELL;
          var road = TRACK[y].charAt(x) === ".";
          ctx.fillStyle = road
            ? ((x + y) % 2 === 0 ? ink.wall[1] : ink.wall[2])
            : ink.wall[3];
          ctx.fillRect(px, py, CELL, CELL);
        }
      }

      // Start/finish line: a checker band across the bottom straight.
      var line = cellCentre(LINE_CELL.x, LINE_CELL.y);
      ctx.fillStyle = ink.ink;
      for (var s = 0; s < 8; s += 1) {
        ctx.fillRect(line.x - 4 + (s % 2) * 4, line.y - 16 + s * 4, 4, 4);
      }

      var next = cellCentre(GATES[gate].x, GATES[gate].y);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(next.x - 8, next.y - 1, 16, 2);
      ctx.fillRect(next.x - 1, next.y - 8, 2, 16);

      rivals.forEach(function (r) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.floor(r.x) - 3, Math.floor(r.y) - 3, 7, 7);
      });

      ctx.fillStyle = drivable(car.x, car.y) ? ink.bright : ink.brass;
      ctx.fillRect(Math.floor(car.x) - 4, Math.floor(car.y) - 4, 9, 9);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(
        Math.floor(car.x + Math.cos(car.angle) * 3) - 1,
        Math.floor(car.y + Math.sin(car.angle) * 3) - 1,
        2,
        2
      );

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LAP " + lap, 6, 12);
      ctx.fillText(clock.toFixed(1) + "s", 6, 22);
      ctx.fillStyle = budget < 8 ? ink.danger : ink.verify;
      ctx.fillText("BUDGET " + Math.ceil(budget) + "s", W / 2 - 34, 12);
      ctx.fillStyle = ink.dim;
      if (best) ctx.fillText("BEST " + best.toFixed(1) + "s", W - 74, 12);
      if (!drivable(car.x, car.y)) {
        ctx.fillStyle = ink.danger;
        ctx.fillText("OVER BUDGET — THROTTLED", W - 148, 22);
      }
      if (messageAge < 2.2) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, H - 8);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "LAP " + lap + " · BUDGET " + Math.max(0, Math.ceil(budget)) + "s";
    };
    return game;
  }

  /* ---- 19. SIDE CHANNEL --------------------------------------------------

     The stealth cabinet. Observers sweep vision cones across a floor of
     server aisles; you are a request that is not supposed to be readable,
     moving between the racks to reach the egress. Being seen does not kill
     you outright — it fills a suspicion meter, and a full meter is the loss,
     because that is what a side channel actually is: not one observation, but
     enough of them.

     Crouching (the fire key, held) halves your speed and makes you unreadable
     at anything but point-blank range. */
  function cabinetSideChannel(random) {
    var CELL = 16;
    var COLS = 20;
    var ROWS = 13;
    var OX = 0;
    var OY = 24;

    var game = { id: "side-channel", score: 0, lives: 3, over: false };
    var racks, watchers, you, exitCell, token, suspicion, floorNo,
      message, messageAge;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function blocked(px, py) {
      var cx = Math.floor((px - OX) / CELL);
      var cy = Math.floor((py - OY) / CELL);
      if (cx < 0 || cy < 0 || cx >= COLS || cy >= ROWS) return true;
      return racks[cy][cx] === 1;
    }

    function buildFloor() {
      racks = [];
      for (var y = 0; y < ROWS; y += 1) {
        var row = [];
        for (var x = 0; x < COLS; x += 1) {
          // Aisles: solid rows of rack with a gap punched through, so cover
          // exists and so does a way past it.
          var isRack = y % 3 === 1 && x > 1 && x < COLS - 2 && (x % 7 !== 3);
          row.push(isRack ? 1 : 0);
        }
        racks.push(row);
      }
      watchers = [];
      var count = Math.min(5, 2 + floorNo);
      for (var w = 0; w < count; w += 1) {
        watchers.push({
          x: OX + (4 + w * 4) * CELL + CELL / 2,
          y: OY + ((w % 3) * 3 + 2) * CELL + CELL / 2,
          angle: random() * Math.PI * 2,
          sweep: (random() < 0.5 ? -1 : 1) * (0.5 + floorNo * 0.08)
        });
      }
      you = { x: OX + CELL, y: OY + CELL / 2 + CELL * (ROWS - 1) };
      exitCell = { x: COLS - 1, y: 0 };
      token = { x: OX + CELL * (COLS - 3), y: OY + CELL * (ROWS - 2), taken: false };
      suspicion = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      floorNo = 1;
      messageAge = 0;
      buildFloor();
      say("FLOOR 1 — TAKE THE TOKEN, THEN EGRESS");
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;

      var crouched = !!input.fire;
      var speed = (crouched ? 34 : 68) * dt;
      var nx = you.x + (input.right ? speed : 0) - (input.left ? speed : 0);
      var ny = you.y + (input.down ? speed : 0) - (input.up ? speed : 0);
      if (!blocked(nx, you.y)) you.x = nx;
      if (!blocked(you.x, ny)) you.y = ny;

      var seen = false;
      watchers.forEach(function (w) {
        // Wrapped, not accumulated: an unbounded heading is what made the
        // cone test below wrong in the first place, and it is also a float
        // that grows all run.
        w.angle = (w.angle + w.sweep * dt) % (Math.PI * 2);
        var dx = you.x - w.x;
        var dy = you.y - w.y;
        var dist = Math.sqrt(dx * dx + dy * dy);
        var range = crouched ? 26 : 84;
        if (dist > range) return;
        var toward = Math.atan2(dy, dx);
        if (Math.abs(angleDelta(toward, w.angle)) > 0.42) return;
        // Line of sight is sampled along the ray rather than assumed: a rack
        // between you and a watcher is the entire point of the racks.
        for (var t = 6; t < dist; t += 5) {
          if (blocked(w.x + Math.cos(toward) * t, w.y + Math.sin(toward) * t)) return;
        }
        seen = true;
      });

      if (seen) {
        suspicion = Math.min(1, suspicion + dt * 0.45);
        if (suspicion >= 1) {
          game.lives -= 1;
          if (game.lives <= 0) {
            game.over = true;
            return;
          }
          say("OBSERVED ENOUGH TIMES TO BE A CHANNEL");
          buildFloor();
          return;
        }
      } else {
        suspicion = Math.max(0, suspicion - dt * 0.22);
      }

      if (!token.taken && Math.abs(you.x - token.x) < 10 && Math.abs(you.y - token.y) < 10) {
        token.taken = true;
        game.score += 60;
        say("TOKEN LIFTED — NOW LEAVE");
      }

      var exitX = OX + exitCell.x * CELL + CELL / 2;
      var exitY = OY + exitCell.y * CELL + CELL / 2;
      if (token.taken && Math.abs(you.x - exitX) < 12 && Math.abs(you.y - exitY) < 12) {
        floorNo += 1;
        game.score += 150;
        buildFloor();
        say("EGRESS CLEAN — FLOOR " + floorNo);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (!racks[y][x]) continue;
          ctx.fillStyle = ink.wall[2];
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL, CELL);
          ctx.fillStyle = ink.wall[3];
          ctx.fillRect(OX + x * CELL + 2, OY + y * CELL + 3, CELL - 4, 2);
          ctx.fillRect(OX + x * CELL + 2, OY + y * CELL + 9, CELL - 4, 2);
        }
      }

      // Cones as stepped wedges: three arcs of blocks, which is how a cone
      // was drawn before anyone had an alpha channel to spare.
      watchers.forEach(function (w) {
        ctx.fillStyle = ink.grid;
        for (var t = 8; t < 84; t += 6) {
          for (var a = -0.42; a <= 0.42; a += 0.14) {
            var px = w.x + Math.cos(w.angle + a) * t;
            var py = w.y + Math.sin(w.angle + a) * t;
            if (blocked(px, py)) continue;
            ctx.fillRect(Math.floor(px) - 1, Math.floor(py) - 1, 3, 3);
          }
        }
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.floor(w.x) - 4, Math.floor(w.y) - 4, 8, 8);
      });

      var exitX = OX + exitCell.x * CELL;
      var exitY = OY + exitCell.y * CELL;
      ctx.fillStyle = token.taken ? ink.verify : ink.grid;
      ctx.fillRect(exitX + 2, exitY + 2, CELL - 4, CELL - 4);

      if (!token.taken) {
        ctx.fillStyle = ink.brass;
        ctx.fillRect(Math.floor(token.x) - 4, Math.floor(token.y) - 4, 8, 8);
      }

      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.floor(you.x) - 4, Math.floor(you.y) - 5, 8, 10);

      ctx.fillStyle = ink.grid;
      ctx.fillRect(6, 8, 120, 6);
      ctx.fillStyle = suspicion > 0.6 ? ink.danger : ink.brass;
      ctx.fillRect(6, 8, Math.floor(suspicion * 120), 6);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SUSPICION", 132, 14);
      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, H - 6);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "FLOOR " + floorNo + " · " + (token.taken ? "EGRESS" : "TOKEN");
    };
    return game;
  }

  /* ---- 20. COLD STORAGE --------------------------------------------------

     Survival horror, which at this palette is one mechanic: you cannot see,
     and you cannot shoot everything. The floor is an archive nobody has read
     in years; the shamblers are replicas of records that were never garbage
     collected. Ammo is scarce enough that walking past something is usually
     the right call, which is the genre's actual lesson.

     The dark is drawn as a mask of blocks rather than a gradient: a radial
     falloff needs colours this palette does not have, and a chunky vignette
     is what this generation's horror looked like anyway. */
  function cabinetColdStorage(random) {
    var CELL = 16;
    var COLS = 20;
    var ROWS = 13;
    var OY = 24;
    var LIGHT = 54;

    var game = { id: "cold-storage", score: 0, lives: 3, over: false };
    var walls, shamblers, shards, you, ammo, depth, exitAt, message, messageAge, hurt, cooldown;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function blocked(px, py) {
      var cx = Math.floor(px / CELL);
      var cy = Math.floor((py - OY) / CELL);
      if (cx < 0 || cy < 0 || cx >= COLS || cy >= ROWS) return true;
      return walls[cy][cx] === 1;
    }

    function buildFloor() {
      walls = [];
      for (var y = 0; y < ROWS; y += 1) {
        var row = [];
        for (var x = 0; x < COLS; x += 1) {
          var edge = x === 0 || y === 0 || x === COLS - 1 || y === ROWS - 1;
          var stack = !edge && x % 4 === 2 && y % 2 === 1 && random() < 0.8;
          row.push(edge || stack ? 1 : 0);
        }
        walls.push(row);
      }
      you = { x: CELL * 1.5, y: OY + CELL * 1.5, face: 1, faceY: 0 };
      shards = [];
      for (var s = 0; s < 3; s += 1) {
        shards.push({
          x: CELL * (5 + Math.floor(random() * (COLS - 8))) + CELL / 2,
          y: OY + CELL * (2 + Math.floor(random() * (ROWS - 4))) + CELL / 2,
          taken: false
        });
      }
      shamblers = [];
      var count = Math.min(7, 3 + depth);
      for (var i = 0; i < count; i += 1) {
        shamblers.push({
          x: CELL * (8 + Math.floor(random() * (COLS - 10))) + CELL / 2,
          y: OY + CELL * (1 + Math.floor(random() * (ROWS - 2))) + CELL / 2,
          hp: 1,
          dead: false
        });
      }
      exitAt = { x: CELL * (COLS - 2) + CELL / 2, y: OY + CELL * (ROWS - 2) + CELL / 2 };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      depth = 1;
      ammo = 10;
      hurt = 0;
      cooldown = 0;
      messageAge = 0;
      buildFloor();
      say("B1 — THREE SHARDS, THEN THE STAIR");
    };

    function shardsLeft() {
      return shards.filter(function (s) { return !s.taken; }).length;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      hurt = Math.max(0, hurt - dt);
      cooldown = Math.max(0, cooldown - dt);

      var speed = 54 * dt;
      var mx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var my = (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (mx || my) {
        you.face = mx;
        you.faceY = mx ? 0 : my;
      }
      if (!blocked(you.x + mx * speed, you.y)) you.x += mx * speed;
      if (!blocked(you.x, you.y + my * speed)) you.y += my * speed;

      if (input.fire && cooldown <= 0 && ammo > 0) {
        cooldown = 0.45;
        ammo -= 1;
        // Hitscan down the facing axis, short range: a torch-lit archive is
        // not a place you take long shots in.
        var hit = null;
        shamblers.forEach(function (s) {
          if (s.dead) return;
          var dx = s.x - you.x;
          var dy = s.y - you.y;
          var alongAxis = you.faceY ? dy * you.faceY : dx * you.face;
          var offAxis = you.faceY ? Math.abs(dx) : Math.abs(dy);
          if (alongAxis < 0 || alongAxis > LIGHT || offAxis > 10) return;
          if (!hit || alongAxis < hit.d) hit = { s: s, d: alongAxis };
        });
        if (hit) {
          hit.s.hp -= 1;
          if (hit.s.hp <= 0) {
            hit.s.dead = true;
            game.score += 30;
            // Ammo comes back off a kill and only off a kill, so a missed
            // shot is a real loss rather than a rounding error.
            if (random() < 0.5) ammo += 1;
          }
        }
      }

      shamblers.forEach(function (s) {
        if (s.dead) return;
        var dx = you.x - s.x;
        var dy = you.y - s.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        // Always coming, faster once they have you: a shambler that idles
        // outside a wake radius lets the player stand in the far corner of
        // the floor indefinitely, and the whole cabinet is the pressure.
        var step = (dist < 100 ? 22 : 13) * dt;
        if (!blocked(s.x + (dx / dist) * step, s.y)) s.x += (dx / dist) * step;
        if (!blocked(s.x, s.y + (dy / dist) * step)) s.y += (dy / dist) * step;
        if (dist < 9 && hurt <= 0) {
          hurt = 1.8;
          game.lives -= 1;
          // Knocked clear, and the replica is pushed back with you. Without
          // it a cornered player took all three retries inside four seconds:
          // the invulnerability window ran out with the thing still standing
          // on them.
          var shove = 14;
          if (!blocked(you.x - (dx / dist) * shove, you.y)) you.x -= (dx / dist) * shove;
          if (!blocked(you.x, you.y - (dy / dist) * shove)) you.y -= (dy / dist) * shove;
          if (!blocked(s.x + (dx / dist) * shove, s.y)) s.x += (dx / dist) * shove;
          if (!blocked(s.x, s.y + (dy / dist) * shove)) s.y += (dy / dist) * shove;
          if (game.lives <= 0) {
            game.over = true;
            return;
          }
          say("A REPLICA GOT A HAND ON THE REQUEST");
        }
      });

      shards.forEach(function (s) {
        if (s.taken) return;
        if (Math.abs(s.x - you.x) > 9 || Math.abs(s.y - you.y) > 9) return;
        s.taken = true;
        ammo += 2;
        game.score += 50;
        say(shardsLeft() ? shardsLeft() + " SHARDS LEFT" : "STAIR UNLOCKED");
      });

      if (!shardsLeft() && Math.abs(you.x - exitAt.x) < 10 && Math.abs(you.y - exitAt.y) < 10) {
        depth += 1;
        game.score += 200;
        ammo += 4;
        buildFloor();
        say("B" + depth + " — IT IS COLDER DOWN HERE");
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (!walls[y][x]) continue;
          ctx.fillStyle = ink.wall[3];
          ctx.fillRect(x * CELL, OY + y * CELL, CELL, CELL);
          ctx.fillStyle = ink.wall[2];
          ctx.fillRect(x * CELL, OY + y * CELL, CELL, 2);
        }
      }

      if (!shardsLeft()) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(exitAt.x - 6, exitAt.y - 6, 12, 12);
      }

      shards.forEach(function (s) {
        if (s.taken) return;
        ctx.fillStyle = ink.brass;
        ctx.fillRect(s.x - 3, s.y - 4, 6, 8);
      });

      shamblers.forEach(function (s) {
        if (s.dead) return;
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.floor(s.x) - 5, Math.floor(s.y) - 6, 10, 12);
      });

      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 10) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(Math.floor(you.x) - 4, Math.floor(you.y) - 5, 8, 10);

      // The dark, last and over everything: 8x8 blocks outside the torch.
      ctx.fillStyle = ink.bg;
      for (var by = OY; by < H; by += 8) {
        for (var bx = 0; bx < W; bx += 8) {
          var dx = bx + 4 - you.x;
          var dy = by + 4 - you.y;
          if (dx * dx + dy * dy < LIGHT * LIGHT) continue;
          ctx.fillRect(bx, by, 8, 8);
        }
      }

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ammo ? ink.dim : ink.danger;
      ctx.fillText("ROUNDS " + ammo, 6, 14);
      ctx.fillStyle = ink.dim;
      ctx.fillText("SHARDS " + (3 - shardsLeft()) + "/3", W - 76, 14);
      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 20);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "B" + depth + " · ROUNDS " + ammo;
    };
    return game;
  }

  /* ---- 21. BLOCK STORE ---------------------------------------------------

     The sandbox: dig, carry, place, and be somewhere defensible when the
     lights go out. One key does both verbs because there is only one key —
     facing a block mines it, facing empty space places one — and that turns
     out to be the whole loop rather than a compromise.

     Themed as an object store, because a block store is what one is: you are
     paid per block retrieved, the night is a scheduled scan, and the wall you
     stack between yourself and it is the only policy that has ever worked. */
  function cabinetBlockStore(random) {
    var CELL = 12;
    var COLS = 26;
    var ROWS = 18;
    var OX = (W - COLS * CELL) / 2;
    var OY = 6;
    var SKY = 5; // rows of open air above the surface

    var game = { id: "block-store", score: 0, lives: 3, over: false };
    var grid, you, held, dayClock, night, prowlers, cycle, message, messageAge, hurt, actCool;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function at(cx, cy) {
      if (cx < 0 || cy < 0 || cx >= COLS || cy >= ROWS) return 1;
      return grid[cy][cx];
    }

    function buildWorld() {
      grid = [];
      for (var y = 0; y < ROWS; y += 1) {
        var row = [];
        for (var x = 0; x < COLS; x += 1) {
          if (y < SKY) row.push(0);
          else if (y === SKY) row.push(1);
          // 2 is ore: worth score, and the only thing down here that is.
          else row.push(random() < 0.12 ? 2 : 1);
        }
        grid.push(row);
      }
      you = { x: 3, y: SKY - 1, face: 1, fall: 0 };
      held = 0;
      prowlers = [];
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cycle = 1;
      dayClock = 42;
      night = false;
      hurt = 0;
      actCool = 0;
      messageAge = 0;
      buildWorld();
      say("DAY 1 — DIG. THE SCAN RUNS AT DUSK.");
    };

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      hurt = Math.max(0, hurt - dt);
      actCool = Math.max(0, actCool - dt);
      dayClock -= dt;

      if (dayClock <= 0) {
        night = !night;
        dayClock = night ? 26 : 42;
        if (night) {
          say("SCAN RUNNING — GET BEHIND SOMETHING");
          var count = Math.min(4, 1 + cycle);
          for (var p = 0; p < count; p += 1) {
            prowlers.push({
              x: (p % 2 ? COLS - 2 : 1) + random(),
              y: SKY - 1,
              speed: 1.7 + cycle * 0.22
            });
          }
        } else {
          cycle += 1;
          prowlers = [];
          game.score += 80;
          say("DAY " + cycle + " — YOU SURVIVED THE SCAN");
        }
      }

      // ---- movement: step on a grid, fall if unsupported ----------------
      if (actCool <= 0) {
        var mx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
        var my = (input.down ? 1 : 0) - (input.up ? 1 : 0);
        if (mx) {
          you.face = mx;
          // Walk into a block and you climb it if the step is one high, which
          // is what keeps a dug trench from being a trap.
          if (!at(you.x + mx, you.y)) {
            you.x += mx;
            actCool = 0.1;
          } else if (!at(you.x + mx, you.y - 1) && !at(you.x, you.y - 1)) {
            you.x += mx;
            you.y -= 1;
            actCool = 0.14;
          }
        } else if (my > 0 && !at(you.x, you.y + 1)) {
          you.y += 1;
          actCool = 0.1;
        } else if (my < 0 && !at(you.x, you.y - 1) && at(you.x, you.y + 1)) {
          // Jump: only from solid ground, only one block, only into air.
          you.y -= 1;
          actCool = 0.16;
        }
      }

      if (!at(you.x, you.y + 1) && you.y < ROWS - 1) {
        you.fall += dt;
        if (you.fall > 0.14) {
          you.y += 1;
          you.fall = 0;
        }
      } else {
        you.fall = 0;
      }

      // ---- one key, two verbs -------------------------------------------
      if (input.fire && actCool <= 0) {
        actCool = 0.22;
        // The aim is the held direction, falling back to the way you face.
        // Without the vertical case there is no way down: the surface row is
        // air at head height, so every press placed a block instead of
        // digging and the ore below was unreachable.
        var tx = you.x;
        var ty = you.y;
        if (input.down) ty += 1;
        else if (input.up) ty -= 1;
        else tx += you.face;
        var target = at(tx, ty);
        if (target) {
          if (tx >= 0 && tx < COLS && ty >= 0 && ty < ROWS) {
            if (target === 2) {
              game.score += 25;
              say("COLD OBJECT RETRIEVED");
            }
            grid[ty][tx] = 0;
            held += 1;
          }
        } else if (held > 0) {
          grid[ty][tx] = 1;
          held -= 1;
        }
      }

      // ---- the scan ------------------------------------------------------
      prowlers.forEach(function (p) {
        var toward = you.x > p.x ? 1 : -1;
        var nx = p.x + toward * p.speed * dt;
        // A prowler walks the surface and cannot dig: a wall or a trench is
        // real cover, which is what makes the building half of the loop mean
        // something.
        if (at(Math.floor(nx), Math.round(p.y))) {
          if (!at(Math.floor(nx), Math.round(p.y) - 1)) p.y -= 1;
          return;
        }
        p.x = nx;
        if (!at(Math.floor(p.x), Math.round(p.y) + 1)) p.y += 1;

        if (Math.abs(p.x - you.x) < 0.8 && Math.abs(p.y - you.y) < 0.8 && hurt <= 0) {
          hurt = 1.2;
          game.lives -= 1;
          if (game.lives <= 0) {
            game.over = true;
            return;
          }
          say("THE SCAN FOUND YOU IN THE OPEN");
        }
      });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      ctx.fillStyle = night ? ink.bg : ink.grid;
      ctx.fillRect(OX, OY, COLS * CELL, SKY * CELL);
      // Sun or moon, as one block on a rail across the sky.
      var phase = 1 - dayClock / (night ? 26 : 42);
      ctx.fillStyle = night ? ink.dim : ink.bright;
      ctx.fillRect(OX + phase * (COLS * CELL - 10), OY + 8, 8, 8);

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          var tile = grid[y][x];
          if (!tile) continue;
          ctx.fillStyle = tile === 2 ? ink.brass : y === SKY ? ink.verify : ink.wall[2];
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL, CELL);
          ctx.fillStyle = ink.bg;
          ctx.fillRect(OX + x * CELL + CELL - 1, OY + y * CELL, 1, CELL);
          ctx.fillRect(OX + x * CELL, OY + y * CELL + CELL - 1, CELL, 1);
        }
      }

      prowlers.forEach(function (p) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(OX + p.x * CELL, OY + p.y * CELL + 1, CELL - 2, CELL - 1);
      });

      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 10) % 2 === 0 ? ink.bright : ink.verify;
      ctx.fillRect(OX + you.x * CELL + 2, OY + you.y * CELL + 1, CELL - 4, CELL - 2);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(OX + you.x * CELL + (you.face > 0 ? CELL - 5 : 3), OY + you.y * CELL + 4, 2, 2);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BLOCKS " + held, 4, H - 4);
      ctx.fillStyle = night ? ink.danger : ink.verify;
      ctx.fillText((night ? "SCAN " : "DAY ") + Math.ceil(dayClock) + "s", W - 78, H - 4);
      if (messageAge < 2.6) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 20);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "DAY " + cycle + " · " + (night ? "SCAN" : "CLEAR");
    };
    return game;
  }

  /* ---- 22. LAST QUORUM ---------------------------------------------------

     The battle royale, which is a genre made of one idea: the board shrinks.
     Here the board is a quorum and the ring is the set of nodes still willing
     to vote — stand outside it and you are partitioned, which costs you
     steadily rather than instantly.

     Everyone else in the match is an agent with the same objective, so the
     honest play is the one the genre is famous for: let them meet first. */
  function cabinetLastQuorum(random) {
    var game = { id: "last-quorum", score: 0, lives: 3, over: false };
    var you, rivals, ring, shots, match, message, messageAge, cooldown, hurt;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function startMatch() {
      you = { x: W / 2, y: H / 2, hp: 100, face: { x: 1, y: 0 } };
      rivals = [];
      var count = Math.min(9, 4 + match);
      for (var i = 0; i < count; i += 1) {
        var a = (i / count) * Math.PI * 2;
        rivals.push({
          x: W / 2 + Math.cos(a) * 120,
          y: H / 2 + Math.sin(a) * 90,
          hp: 40,
          cool: 1 + random() * 2,
          dead: false
        });
      }
      ring = { x: W / 2, y: H / 2, r: 150, target: 150, hold: 8 };
      shots = [];
      cooldown = 0;
      hurt = 0;
      say("MATCH " + match + " — " + (count + 1) + " IN THE QUORUM");
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      match = 1;
      messageAge = 0;
      startMatch();
    };

    function alive() {
      return rivals.filter(function (r) { return !r.dead; });
    }

    function fire(from, dx, dy, mine) {
      var dist = Math.sqrt(dx * dx + dy * dy) || 1;
      shots.push({
        x: from.x,
        y: from.y,
        dx: (dx / dist) * 150,
        dy: (dy / dist) * 150,
        mine: mine,
        life: 1.4
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      cooldown = Math.max(0, cooldown - dt);
      hurt = Math.max(0, hurt - dt);

      // ---- the ring -----------------------------------------------------
      ring.hold -= dt;
      if (ring.hold <= 0 && ring.target > 34) {
        ring.target = Math.max(34, ring.target - 26);
        ring.hold = 9;
        say("QUORUM SHRANK TO " + Math.round(ring.target));
      }
      ring.r += (ring.target - ring.r) * Math.min(1, dt * 0.6);

      var mx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var my = (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (mx || my) {
        you.face = { x: mx, y: my };
        var speed = 70 * dt;
        you.x = clamp(you.x + mx * speed, 4, W - 4);
        you.y = clamp(you.y + my * speed, 20, H - 4);
      }

      if (input.fire && cooldown <= 0) {
        cooldown = 0.3;
        fire(you, you.face.x, you.face.y, true);
      }

      var outside = Math.sqrt(
        (you.x - ring.x) * (you.x - ring.x) + (you.y - ring.y) * (you.y - ring.y)
      ) > ring.r;
      if (outside) {
        you.hp -= 14 * dt;
        hurt = 0.2;
      }

      // ---- the field ----------------------------------------------------
      alive().forEach(function (r) {
        var dx = you.x - r.x;
        var dy = you.y - r.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        // Rivals want the middle of the ring and take a shot at whatever is
        // in front of them on the way, which is close enough to how a real
        // lobby behaves and cheaper than pathing to each other.
        var toRingX = ring.x - r.x;
        var toRingY = ring.y - r.y;
        var ringDist = Math.sqrt(toRingX * toRingX + toRingY * toRingY) || 1;
        var pull = ringDist > ring.r - 20 ? 1 : 0.25;
        var chase = dist < 90 ? 0.8 : 0;
        r.x += ((toRingX / ringDist) * pull + (dx / dist) * chase) * 44 * dt;
        r.y += ((toRingY / ringDist) * pull + (dy / dist) * chase) * 44 * dt;

        r.cool -= dt;
        if (r.cool <= 0 && dist < 120) {
          r.cool = 1.1 + random();
          fire(r, dx, dy, false);
        }
      });

      shots.forEach(function (s) {
        s.x += s.dx * dt;
        s.y += s.dy * dt;
        s.life -= dt;
        if (s.mine) {
          alive().forEach(function (r) {
            if (Math.abs(r.x - s.x) > 6 || Math.abs(r.y - s.y) > 6) return;
            s.life = 0;
            r.hp -= 20;
            if (r.hp <= 0) {
              r.dead = true;
              game.score += 60;
            }
          });
        } else if (Math.abs(you.x - s.x) < 6 && Math.abs(you.y - s.y) < 6) {
          s.life = 0;
          you.hp -= 12;
          hurt = 0.3;
        }
      });
      shots = shots.filter(function (s) {
        return s.life > 0 && s.x > -8 && s.x < W + 8 && s.y > -8 && s.y < H + 8;
      });

      if (you.hp <= 0) {
        game.lives -= 1;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        say("PARTITIONED OUT — " + (alive().length + 1) + "th");
        startMatch();
        return;
      }

      if (!alive().length) {
        game.score += 300;
        match += 1;
        say("LAST IN THE QUORUM — NOBODY LEFT TO OUTVOTE YOU");
        startMatch();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      // The ring, as a dotted circle of blocks. Everything outside it is
      // shaded, so "inside" is legible without a fill.
      ctx.fillStyle = ink.grid;
      for (var y = 20; y < H; y += 6) {
        for (var x = 0; x < W; x += 6) {
          var dx = x - ring.x;
          var dy = y - ring.y;
          if (dx * dx + dy * dy < ring.r * ring.r) continue;
          ctx.fillRect(x, y, 3, 3);
        }
      }
      ctx.fillStyle = ink.verify;
      for (var a = 0; a < Math.PI * 2; a += 0.09) {
        ctx.fillRect(
          Math.floor(ring.x + Math.cos(a) * ring.r) - 1,
          Math.floor(ring.y + Math.sin(a) * ring.r) - 1,
          2,
          2
        );
      }

      alive().forEach(function (r) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.floor(r.x) - 4, Math.floor(r.y) - 4, 8, 8);
      });

      shots.forEach(function (s) {
        ctx.fillStyle = s.mine ? ink.bright : ink.brass;
        ctx.fillRect(Math.floor(s.x) - 1, Math.floor(s.y) - 1, 2, 2);
      });

      ctx.fillStyle = hurt > 0 ? ink.bright : ink.verify;
      ctx.fillRect(Math.floor(you.x) - 4, Math.floor(you.y) - 5, 8, 10);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.grid;
      ctx.fillRect(6, 8, 90, 6);
      ctx.fillStyle = you.hp > 30 ? ink.verify : ink.danger;
      ctx.fillRect(6, 8, Math.max(0, Math.floor((you.hp / 100) * 90)), 6);
      ctx.fillStyle = ink.dim;
      ctx.fillText("ALIVE " + (alive().length + 1), W - 74, 14);
      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 26);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "MATCH " + match + " · HP " + Math.max(0, Math.floor(you.hp));
    };
    return game;
  }

  /* ---- 23. HEARTBEAT -----------------------------------------------------

     The rhythm cabinet. Four lanes of health checks fall toward a judgement
     line and you answer each one on the beat: miss enough and the service is
     declared down. The chart is generated from the seeded generator against a
     fixed tempo, so the same seed is the same song — a rhythm game whose
     notes move is not a rhythm game.

     Silent by construction. The page never plays audio (the boot screen says
     so), so the beat is carried by a pulsing bar and by the notes themselves,
     which is how this genre worked on hardware with one sound channel. */
  function cabinetHeartbeat(random) {
    var LANES = 4;
    var LANE_W = 40;
    var OX = (W - LANES * LANE_W) / 2;
    var LINE = H - 44;
    var FALL = 118;      // px per second
    var WINDOW = 14;     // px of tolerance at the line
    var LANE_KEYS = ["left", "up", "down", "right"];
    var LANE_NAMES = ["PING", "READY", "LIVE", "SYNC"];

    var game = { id: "heartbeat", score: 0, lives: 3, over: false };
    var notes, beatTimer, beat, combo, bestCombo, health, tempo, bar,
      flashes, held, judgement, judgementAge;

    function judge(text) {
      judgement = text;
      judgementAge = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      notes = [];
      beatTimer = 0;
      beat = 0;
      combo = 0;
      bestCombo = 0;
      health = 1;
      tempo = 0.42;
      bar = 1;
      flashes = [0, 0, 0, 0];
      held = [false, false, false, false];
      judgement = "READY";
      judgementAge = 0;
    };

    function spawnBeat() {
      // One note most beats, two on the downbeat of a later bar. Density
      // rises with the bar count rather than at random, so the chart has a
      // shape a player can learn instead of a difficulty that flickers.
      var density = Math.min(0.85, 0.45 + bar * 0.03);
      var placed = 0;
      for (var lane = 0; lane < LANES; lane += 1) {
        if (random() > density / LANES * 1.6) continue;
        notes.push({ lane: lane, y: -8, hit: false });
        placed += 1;
        if (placed >= (beat % 4 === 0 && bar > 3 ? 2 : 1)) break;
      }
      if (!placed) notes.push({ lane: Math.floor(random() * LANES), y: -8, hit: false });
    }

    function drop(reason) {
      combo = 0;
      health -= 0.14;
      judge(reason);
      if (health > 0) return;
      game.lives -= 1;
      health = 1;
      if (game.lives <= 0) game.over = true;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      judgementAge += dt;

      beatTimer -= dt;
      if (beatTimer <= 0) {
        beatTimer += tempo;
        beat += 1;
        if (beat % 8 === 0) {
          bar += 1;
          // The tempo creeps, which is the only difficulty curve this genre
          // has ever needed.
          tempo = Math.max(0.22, tempo - 0.012);
          game.score += 20;
        }
        spawnBeat();
      }

      notes.forEach(function (n) { n.y += FALL * dt; });

      var pressed = [!!input.left, !!input.up, !!input.down, !!input.right];
      for (var lane = 0; lane < LANES; lane += 1) {
        if (!pressed[lane]) {
          held[lane] = false;
          continue;
        }
        if (held[lane]) continue;
        held[lane] = true;
        flashes[lane] = 0.16;

        // Nearest unhit note in this lane, and only if it is inside the
        // window: a lane press with nothing near it is a miss, or the game is
        // one where mashing all four keys wins.
        var best = null;
        notes.forEach(function (n) {
          if (n.hit || n.lane !== lane) return;
          var off = Math.abs(n.y - LINE);
          if (off > WINDOW * 2.2) return;
          if (!best || off < best.off) best = { note: n, off: off };
        });
        if (!best) {
          // Cheaper than a miss: hitting a lane early is a player learning
          // where the lanes are, and a miss is the service going unanswered.
          combo = 0;
          health -= 0.05;
          judge("EARLY — NOTHING THERE");
          continue;
        }
        best.note.hit = true;
        if (best.off <= WINDOW * 0.5) {
          combo += 1;
          game.score += 10 + Math.floor(combo / 5) * 2;
          judge("ON BEAT ×" + combo);
        } else if (best.off <= WINDOW) {
          combo += 1;
          game.score += 5;
          judge("LATE — STILL COUNTS");
        } else {
          drop("OFF BEAT");
        }
        if (combo > bestCombo) bestCombo = combo;
      }

      notes.forEach(function (n) {
        if (n.hit || n.y <= LINE + WINDOW) return;
        n.hit = true;
        drop("MISSED CHECK");
      });
      notes = notes.filter(function (n) { return n.y < H + 12 && !n.hit; });

      for (var f = 0; f < LANES; f += 1) flashes[f] = Math.max(0, flashes[f] - dt);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      for (var lane = 0; lane < LANES; lane += 1) {
        var x = OX + lane * LANE_W;
        // Every lane the same shade, separated by the gutter the width leaves.
        // Alternating lane against background read as two wide lanes rather
        // than four, which is the one thing this cabinet cannot afford to be
        // ambiguous about.
        ctx.fillStyle = ink.grid;
        ctx.fillRect(x, 18, LANE_W - 3, H - 30);
        ctx.fillStyle = flashes[lane] > 0 ? ink.bright : ink.wall[2];
        ctx.fillRect(x, LINE, LANE_W - 2, 4);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.dim;
        ctx.textAlign = "center";
        ctx.fillText(LANE_NAMES[lane], x + LANE_W / 2 - 1, H - 26);
        ctx.fillText(["←", "↑", "↓", "→"][lane], x + LANE_W / 2 - 1, H - 14);
        ctx.textAlign = "left";
      }

      notes.forEach(function (n) {
        var x = OX + n.lane * LANE_W;
        var close = Math.abs(n.y - LINE) < WINDOW;
        ctx.fillStyle = close ? ink.verify : ink.brass;
        ctx.fillRect(x + 4, Math.floor(n.y) - 4, LANE_W - 10, 8);
      });

      // The beat, as a bar that pulses on the downbeat. This is the metronome
      // a silent cabinet has to give you instead of a click track.
      var pulse = 1 - beatTimer / tempo;
      ctx.fillStyle = ink.grid;
      ctx.fillRect(0, 0, W, 14);
      ctx.fillStyle = beat % 4 === 0 ? ink.bright : ink.brass;
      ctx.fillRect(0, 0, Math.floor(pulse * W), 3);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BAR " + bar, 4, 11);
      ctx.fillStyle = health > 0.4 ? ink.verify : ink.danger;
      ctx.fillRect(W - 96, 4, 92, 6);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(W - 96 + Math.floor(health * 92), 4, 92 - Math.floor(health * 92), 6);

      if (judgementAge < 1) {
        ctx.textAlign = "center";
        ctx.fillStyle = combo > 0 ? ink.verify : ink.danger;
        ctx.fillText(judgement, W / 2, LINE - 24);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "COMBO " + combo + " · BEST " + bestCombo;
    };
    return game;
  }

  /* ---- 24. BRUTE FORCE ---------------------------------------------------

     The belt-scrolling brawler: a shallow strip of floor, a crowd walking in
     from both edges, and one button. The depth axis is what separates this
     from a side-scroller — you line up on somebody's row before you can hit
     them, and the whole skill is not being surrounded while you do it.

     The crowd is a credential-stuffing run, which is the one attack in this
     product's domain that is literally a brawl: no cleverness, just volume,
     and the answer is to stand somewhere the volume cannot all reach you. */
  function cabinetBruteForce(random) {
    var TOP = 130;      // shallowest row of the belt
    var BOTTOM = H - 26;
    var REACH = 22;

    var game = { id: "brute-force", score: 0, lives: 3, over: false };
    var you, crowd, wave, spawnLeft, scroll, message, messageAge, hurt, swing, cooldown;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function startWave() {
      crowd = [];
      spawnLeft = 4 + wave * 2;
      say("WAVE " + wave + " — " + spawnLeft + " ATTEMPTS INBOUND");
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      wave = 1;
      scroll = 0;
      hurt = 0;
      swing = 0;
      cooldown = 0;
      messageAge = 0;
      you = { x: W / 2, y: (TOP + BOTTOM) / 2, face: 1 };
      startWave();
    };

    function spawn() {
      var fromLeft = random() < 0.5;
      crowd.push({
        x: fromLeft ? -12 : W + 12,
        y: TOP + random() * (BOTTOM - TOP),
        hp: 2 + Math.floor(wave / 3),
        cool: 0,
        face: fromLeft ? 1 : -1
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      hurt = Math.max(0, hurt - dt);
      swing = Math.max(0, swing - dt);
      cooldown = Math.max(0, cooldown - dt);
      scroll += 14 * dt;

      var speed = 62 * dt;
      if (input.left) { you.x -= speed; you.face = -1; }
      if (input.right) { you.x += speed; you.face = 1; }
      // The depth axis moves at half rate, the way this genre always did it:
      // it is a positioning axis, not a dodging one.
      if (input.up) you.y -= speed * 0.5;
      if (input.down) you.y += speed * 0.5;
      you.x = clamp(you.x, 8, W - 8);
      you.y = clamp(you.y, TOP, BOTTOM);

      if (input.fire && cooldown <= 0) {
        cooldown = 0.26;
        swing = 0.14;
        var landed = 0;
        crowd.forEach(function (c) {
          if (Math.abs(c.y - you.y) > 10) return;
          var gap = (c.x - you.x) * you.face;
          if (gap < 0 || gap > REACH) return;
          c.hp -= 1;
          c.x += you.face * 8;
          landed += 1;
          if (c.hp <= 0) {
            c.down = true;
            game.score += 20;
          }
        });
        if (landed > 1) game.score += 15; // a hit that caught two of them
      }

      if (spawnLeft > 0 && crowd.length < 5 && random() < dt * 1.6) {
        spawn();
        spawnLeft -= 1;
      }

      crowd.forEach(function (c) {
        if (c.down) return;
        var dx = you.x - c.x;
        var dy = you.y - c.y;
        c.face = dx > 0 ? 1 : -1;
        var pace = (26 + wave * 2) * dt;
        // Close the depth first, then the distance: a crowd that converges on
        // a diagonal all arrives at once and there is no play in that.
        if (Math.abs(dy) > 4) c.y += (dy > 0 ? 1 : -1) * pace * 0.6;
        else if (Math.abs(dx) > REACH - 6) c.x += (dx > 0 ? 1 : -1) * pace;

        c.cool -= dt;
        if (Math.abs(dx) <= REACH - 4 && Math.abs(dy) <= 10 && c.cool <= 0) {
          c.cool = 1.1;
          if (hurt > 0) return;
          hurt = 1;
          game.lives -= 1;
          if (game.lives <= 0) {
            game.over = true;
            return;
          }
          say("STUFFED — ONE GOT THROUGH");
        }
      });
      crowd = crowd.filter(function (c) { return !c.down; });

      if (!crowd.length && spawnLeft <= 0) {
        wave += 1;
        game.score += 90;
        startWave();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);

      // Background wall with a scrolling repeat, so the belt reads as moving
      // even though the fight is stationary.
      ctx.fillStyle = ink.grid;
      ctx.fillRect(0, 20, W, TOP - 20);
      ctx.fillStyle = ink.wall[3];
      for (var d = -1; d < 12; d += 1) {
        ctx.fillRect((d * 34 - scroll % 34), 34, 20, TOP - 54);
      }
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(0, TOP - 4, W, 4);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, TOP, W, H - TOP);

      var bodies = crowd.slice();
      bodies.push(you);
      // Painter's algorithm on the depth axis: whoever is further down the
      // belt is nearer the camera and draws last.
      bodies.sort(function (a, b) { return a.y - b.y; });

      bodies.forEach(function (b) {
        var mine = b === you;
        var x = Math.floor(b.x);
        var y = Math.floor(b.y);
        var scale = 0.8 + (b.y - TOP) / (BOTTOM - TOP) * 0.4;
        var hgt = Math.floor(22 * scale);
        ctx.fillStyle = mine
          ? (hurt > 0 && Math.floor(hurt * 10) % 2 === 0 ? ink.bright : ink.verify)
          : ink.danger;
        ctx.fillRect(x - 5, y - hgt, 10, hgt);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x - 3, y - hgt + 3, 2, 2);
        ctx.fillRect(x + 1, y - hgt + 3, 2, 2);
        if (mine && swing > 0) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x + (you.face > 0 ? 5 : -5 - REACH), y - hgt + 6, REACH, 4);
        }
      });

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("QUEUED " + spawnLeft, 4, 12);
      ctx.fillText("ON THE FLOOR " + crowd.length, W - 118, 12);
      if (messageAge < 2.2) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 26);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "WAVE " + wave + " · " + (spawnLeft + crowd.length) + " LEFT";
    };
    return game;
  }

  /* ---- 25. CATALOG -------------------------------------------------------

     The creature-collector, which on a console is two games stitched
     together: walking around, and a capture that is decided in a moment. The
     walk is the unregistered half of a tool estate — every tall patch of
     shadow IT is a thing somebody is calling in production that nobody has
     written down. Meeting one opens the capture: a marker sweeps a bar, and
     the narrow band in the middle is the scope that actually fits the tool.

     Register the whole floor and the estate grows a new one, because it
     always does. */
  function cabinetCatalog(random) {
    var CELL = 16;
    var COLS = 20;
    var ROWS = 11;
    var OY = 34;
    var TOOLS = [
      "pdf-gen", "crm-sync", "s3-copy", "mailer", "geocode",
      "ocr", "invoice", "scraper", "tokenise", "webhook"
    ];

    var game = { id: "catalog", score: 0, lives: 3, over: false };
    var patches, you, mode, quarry, sweep, dir, band, registered, estate,
      moveCool, message, messageAge, held;

    function say(text) {
      message = text;
      messageAge = 0;
    }

    function buildEstate() {
      patches = [];
      for (var y = 0; y < ROWS; y += 1) {
        var row = [];
        for (var x = 0; x < COLS; x += 1) {
          row.push(x > 0 && y > 0 && x < COLS - 1 && y < ROWS - 1 && random() < 0.3 ? 1 : 0);
        }
        patches.push(row);
      }
      you = { x: 1, y: 1 };
      mode = "walk";
      quarry = null;
      registered = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      estate = 1;
      moveCool = 0;
      messageAge = 0;
      held = false;
      buildEstate();
      say("ESTATE 1 — SIX TOOLS TO REGISTER");
    };

    function encounter() {
      mode = "capture";
      quarry = {
        name: TOOLS[Math.floor(random() * TOOLS.length)],
        // A rarer tool has a narrower scope that fits it, which is the only
        // difficulty dial this needs.
        tier: 1 + Math.floor(random() * 3)
      };
      sweep = 0;
      dir = 1;
      band = 0.3 - quarry.tier * 0.055 + estate * -0.01;
      band = Math.max(0.06, band);
    }

    game.update = function (dt, input) {
      if (game.over) return;
      messageAge += dt;
      moveCool -= dt;

      if (mode === "capture") {
        sweep += dir * dt * (0.85 + quarry.tier * 0.25 + estate * 0.08);
        if (sweep > 1) { sweep = 1; dir = -1; }
        if (sweep < 0) { sweep = 0; dir = 1; }

        if (!input.fire) {
          held = false;
          return;
        }
        if (held) return;
        held = true;

        var off = Math.abs(sweep - 0.5);
        if (off <= band / 2) {
          registered += 1;
          game.score += 40 + quarry.tier * 20;
          say(quarry.name + " REGISTERED — SCOPE FITS");
          mode = "walk";
          quarry = null;
          if (registered >= 6) {
            estate += 1;
            game.score += 180;
            buildEstate();
            say("ESTATE " + estate + " — MORE OF IT APPEARED");
          }
          return;
        }
        // A miss costs the encounter, not a life: the genre's failure is the
        // one that got away, and it is worse than a hit point.
        game.lives -= 1;
        mode = "walk";
        quarry = null;
        if (game.lives <= 0) {
          game.over = true;
          return;
        }
        say("SCOPE TOO " + (sweep < 0.5 ? "NARROW" : "WIDE") + " — IT STAYS UNREGISTERED");
        return;
      }

      held = !!input.fire;
      if (moveCool > 0) return;
      var mx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var my = mx ? 0 : (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (!mx && !my) return;
      moveCool = 0.13;
      you.x = clamp(you.x + mx, 0, COLS - 1);
      you.y = clamp(you.y + my, 0, ROWS - 1);
      if (patches[you.y][you.x] && random() < 0.34) encounter();
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.font = '8px "IBM Plex Mono", monospace';

      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          var px = x * CELL;
          var py = OY + y * CELL;
          ctx.fillStyle = (x + y) % 2 === 0 ? ink.bg : ink.grid;
          ctx.fillRect(px, py, CELL, CELL);
          if (!patches[y][x]) continue;
          ctx.fillStyle = ink.wall[1];
          for (var blade = 0; blade < 3; blade += 1) {
            ctx.fillRect(px + 3 + blade * 4, py + 4 + (blade % 2) * 2, 2, CELL - 8);
          }
        }
      }

      ctx.fillStyle = ink.verify;
      ctx.fillRect(you.x * CELL + 4, OY + you.y * CELL + 3, CELL - 8, CELL - 6);

      ctx.fillStyle = ink.dim;
      ctx.fillText("REGISTERED " + registered + "/6", 4, 14);
      ctx.fillText("ESTATE " + estate, W - 74, 14);

      if (mode === "capture") {
        // The capture panel sits over the walk, the way an encounter screen
        // slid over the map on hardware that could not hold two scenes.
        ctx.fillStyle = ink.bg;
        ctx.fillRect(24, 62, W - 48, 116);
        ctx.fillStyle = ink.brass;
        ctx.fillRect(24, 62, W - 48, 2);
        ctx.fillRect(24, 176, W - 48, 2);
        ctx.textAlign = "center";
        ctx.fillStyle = ink.ink;
        ctx.fillText("UNREGISTERED TOOL", W / 2, 80);
        ctx.fillStyle = ink.bright;
        ctx.fillText(quarry.name + "  ·  TIER " + quarry.tier, W / 2, 96);
        ctx.fillStyle = ink.dim;
        ctx.fillText("STOP THE MARKER IN THE SCOPE", W / 2, 166);
        ctx.textAlign = "left";

        var barX = 44;
        var barW = W - 88;
        ctx.fillStyle = ink.grid;
        ctx.fillRect(barX, 122, barW, 14);
        ctx.fillStyle = ink.verify;
        ctx.fillRect(barX + barW * (0.5 - band / 2), 122, barW * band, 14);
        ctx.fillStyle = ink.danger;
        ctx.fillRect(barX + barW * sweep - 1, 116, 3, 26);
      }

      if (messageAge < 2.4) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bright;
        ctx.fillText(message, W / 2, 26);
        ctx.textAlign = "left";
      }
    };

    game.hud = function () {
      return "ESTATE " + estate + " · " + registered + "/6";
    };
    return game;
  }

  /* ---- 26. RATE GATE -----------------------------------------------------

     One button, one bird-shaped request, an endless run of rate limits with a
     gap in them. The genre that ate mobile in 2013, and the purest expression
     of this product's most-asked question: how many calls per second is that
     tool actually going to let you make? */
  function cabinetRateGate(random) {
    var PACKET = makeSprite([
      "..111...",
      ".1122111",
      "11222211",
      ".1112211",
      "...11111"
    ], ["verify", "bright"]);
    var GRAVITY = 460;
    var FLAP = -158;

    var game = { id: "rate-gate", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var bird, gates, scroll, speed, held, best, message, messageAge;

    function spawnGate(x) {
      var gap = Math.max(46, 78 - game.score * 0.5);
      var top = 26 + random() * (H - gap - 60);
      gates.push({ x: x, top: top, gap: gap, scored: false });
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      bird = { y: H / 2, vy: 0, tilt: 0 };
      gates = [];
      scroll = 0;
      speed = 62;
      held = false;
      best = 0;
      messageAge = 9;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 3; i += 1) spawnGate(W + i * 110);
    };

    function crash(reason) {
      game.lives -= 1;
      fx.shake(9);
      fx.flash("danger", 1);
      bits.burst(70, bird.y, 14, { colour: "danger", speed: 70, life: 0.5 });
      message = reason;
      messageAge = 0;
      if (game.lives <= 0) {
        game.over = true;
        return;
      }
      bird.y = H / 2;
      bird.vy = 0;
      gates = [];
      for (var i = 0; i < 3; i += 1) spawnGate(W + i * 110);
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;
      scroll += speed * dt;
      speed = Math.min(120, 62 + game.score * 0.7);

      // Edge-triggered: a held key is one flap, not lift.
      if (input.fire || input.up) {
        if (!held) {
          held = true;
          bird.vy = FLAP;
          bits.burst(66, bird.y + 4, 3, { colour: "dim", speed: 26, angle: Math.PI * 0.5, spread: 1, life: 0.3 });
        }
      } else {
        held = false;
      }

      bird.vy += GRAVITY * dt;
      bird.y += bird.vy * dt;
      bird.tilt = clamp(bird.vy / 260, -1, 1);

      if (bird.y < 4 || bird.y > H - 10) {
        crash(bird.y < 4 ? "CEILING — THAT IS ALSO A LIMIT" : "FLOOR — REQUEST DROPPED");
        return;
      }

      for (var i = gates.length - 1; i >= 0; i -= 1) {
        var g = gates[i];
        g.x -= speed * dt;
        if (!g.scored && g.x + 12 < 66) {
          g.scored = true;
          game.score += 1;
          if (game.score > best) best = game.score;
          fx.pop(66, bird.y - 14, "+1", "verify");
          bits.burst(78, bird.y, 4, { colour: "verify", speed: 40, life: 0.35 });
        }
        if (g.x < -16) {
          gates.splice(i, 1);
          spawnGate(gates.length ? gates[gates.length - 1].x + 96 + random() * 30 : W + 60);
          continue;
        }
        // The packet is 8 wide and sits at x=62..70.
        if (g.x < 74 && g.x + 12 > 62 && (bird.y < g.top || bird.y > g.top + g.gap)) {
          crash("THROTTLED AT THE GATE");
          return;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, scroll * 0.5, 30);
      fx.begin(ctx);

      gates.forEach(function (g) {
        ctx.fillStyle = ink.wall[2];
        ctx.fillRect(g.x, 0, 12, g.top);
        ctx.fillRect(g.x, g.top + g.gap, 12, H - g.top - g.gap);
        ctx.fillStyle = ink.brass;
        ctx.fillRect(g.x - 2, g.top - 6, 16, 6);
        ctx.fillRect(g.x - 2, g.top + g.gap, 16, 6);
      });

      bits.draw(ctx, ink);
      // Tilt is faked with a vertical offset per column rather than a real
      // rotation: rotating a canvas at this scale resamples the sprite and
      // undoes the whole pixel grid.
      PACKET.drawAt(ctx, ink, 66, bird.y + bird.tilt * 2, 1);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("PASSED " + game.score, 4, 12);
      if (messageAge < 1.8) centreText(ctx, ink, message, 30, "danger");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "GATES " + game.score + " · " + Math.round(speed) + " rps"; };
    return game;
  }

  /* ---- 27. MERGE LEDGER --------------------------------------------------

     The sliding-merge grid, which is the puzzle every phone had for a year.
     Entries slide, equal entries combine, and the board fills whether or not
     you were ready — which is what a ledger does. */
  function cabinetMergeLedger(random) {
    var SIZE = 4;
    var CELL = 34;
    var OX = (W - SIZE * CELL) / 2;
    var OY = 44;

    var game = { id: "merge-ledger", score: 0, lives: 1, over: false };
    var fx = makeFx();
    var grid, held, best, moved, message, messageAge;

    function empties() {
      var out = [];
      for (var i = 0; i < SIZE * SIZE; i += 1) if (!grid[i]) out.push(i);
      return out;
    }

    function spawn() {
      var free = empties();
      if (!free.length) return;
      grid[free[Math.floor(random() * free.length)]] = random() < 0.85 ? 1 : 2;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 1;
      game.over = false;
      grid = [];
      for (var i = 0; i < SIZE * SIZE; i += 1) grid.push(0);
      held = false;
      best = 1;
      messageAge = 9;
      fx.reset();
      spawn();
      spawn();
    };

    /* One slide routine, fed a traversal order per direction. Four hand-rolled
       copies of this is how merge bugs get in — the classic being a row that
       merges twice in one move. */
    function slide(dx, dy) {
      moved = false;
      var merged = {};
      var xs = [], ys = [];
      for (var i = 0; i < SIZE; i += 1) { xs.push(i); ys.push(i); }
      if (dx > 0) xs.reverse();
      if (dy > 0) ys.reverse();

      ys.forEach(function (y) {
        xs.forEach(function (x) {
          var from = y * SIZE + x;
          if (!grid[from]) return;
          var cx = x, cy = y;
          while (true) {
            var nx = cx + dx, ny = cy + dy;
            if (nx < 0 || ny < 0 || nx >= SIZE || ny >= SIZE) break;
            var to = ny * SIZE + nx;
            var here = cy * SIZE + cx;
            if (!grid[to]) {
              grid[to] = grid[here];
              grid[here] = 0;
              cx = nx; cy = ny;
              moved = true;
              continue;
            }
            if (grid[to] === grid[here] && !merged[to]) {
              grid[to] += 1;
              grid[here] = 0;
              merged[to] = true;
              moved = true;
              game.score += Math.pow(2, grid[to]);
              if (grid[to] > best) best = grid[to];
              fx.pop(OX + nx * CELL + CELL / 2, OY + ny * CELL, "×2", "verify");
              fx.shake(3);
            }
            break;
          }
        });
      });
    }

    function stuck() {
      if (empties().length) return false;
      for (var y = 0; y < SIZE; y += 1) {
        for (var x = 0; x < SIZE; x += 1) {
          var v = grid[y * SIZE + x];
          if (x + 1 < SIZE && grid[y * SIZE + x + 1] === v) return false;
          if (y + 1 < SIZE && grid[(y + 1) * SIZE + x] === v) return false;
        }
      }
      return true;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;

      var dx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var dy = dx ? 0 : (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (!dx && !dy) {
        held = false;
        return;
      }
      if (held) return;
      held = true;

      slide(dx, dy);
      if (moved) spawn();
      if (stuck()) {
        game.lives = 0;
        game.over = true;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(OX - 3, OY - 3, SIZE * CELL + 6, SIZE * CELL + 6);

      for (var i = 0; i < SIZE * SIZE; i += 1) {
        var x = OX + (i % SIZE) * CELL;
        var y = OY + Math.floor(i / SIZE) * CELL;
        var v = grid[i];
        ctx.fillStyle = v ? (v >= 9 ? ink.bright : v >= 6 ? ink.brass : v >= 3 ? ink.verify : ink.wall[1]) : ink.grid;
        ctx.fillRect(x + 1, y + 1, CELL - 3, CELL - 3);
        if (!v) continue;
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = v >= 3 ? ink.bg : ink.ink;
        ctx.fillText(String(Math.pow(2, v)), x + CELL / 2 - 1, y + CELL / 2);
        ctx.textAlign = "left";
      }

      centreText(ctx, ink, "MERGE THE LEDGER", 18, "dim");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("TOP ENTRY " + Math.pow(2, best), 4, H - 8);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "TOP " + Math.pow(2, best); };
    return game;
  }

  /* ---- 28. TAP FORGE -----------------------------------------------------

     The idle game, which is the genre that admits what metering already knows:
     the number only ever goes up, and the interesting decision is what to buy
     with it. Tap to mint a receipt by hand; spend receipts on workers who mint
     them for you; the quota rises faster than you do.

     A retry is lost by missing a quota, so there is a fail state — an idle
     game with no clock is a spreadsheet. */
  function cabinetTapForge(random) {
    var UPGRADES = [
      { name: "SIGNER", cost: 25, rate: 1.2 },
      { name: "BATCHER", cost: 120, rate: 5 },
      { name: "SHARD", cost: 600, rate: 22 },
      { name: "REGION", cost: 2800, rate: 95 }
    ];

    var game = { id: "tap-forge", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var bank, owned, pick, rate, quota, quotaLeft, era, held, message, messageAge, scoreRemainder;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      bank = 0;
      scoreRemainder = 0;
      owned = [0, 0, 0, 0];
      pick = 0;
      rate = 0;
      era = 1;
      quota = 40;
      quotaLeft = 30;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
    };

    function cost(i) {
      return Math.floor(UPGRADES[i].cost * Math.pow(1.6, owned[i]));
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      // Passive income, then the quota clock. Both run whether or not the
      // human is doing anything, which is the joke and the genre.
      var earned = rate * dt;
      bank += earned;
      scoreRemainder += earned;
      var wholeScore = Math.floor(scoreRemainder);
      game.score += wholeScore;
      scoreRemainder -= wholeScore;
      quotaLeft -= dt;

      var pressed = input.fire || input.up || input.down;
      if (!pressed) held = false;

      if (input.fire && !held) {
        held = true;
        // Fire buys when you can afford the highlighted line, and mints by
        // hand when you cannot. One button, and it always does the thing the
        // player has the resources for.
        var price = cost(pick);
        if (bank >= price) {
          bank -= price;
          owned[pick] += 1;
          rate += UPGRADES[pick].rate;
          fx.flash("verify", 0.8);
          fx.pop(W / 2, 70, UPGRADES[pick].name + " +1", "verify");
        } else {
          bank += era;
          game.score += era;
          bits.burst(W / 2, 96, 4, { colour: "brass", speed: 50, life: 0.4 });
          fx.pop(W / 2 + (random() - 0.5) * 40, 92, "+" + era, "brass");
        }
      } else if (input.up && !held) {
        held = true;
        pick = (pick + UPGRADES.length - 1) % UPGRADES.length;
      } else if (input.down && !held) {
        held = true;
        pick = (pick + 1) % UPGRADES.length;
      }

      if (quotaLeft <= 0) {
        if (game.score >= quota) {
          era += 1;
          quota = Math.floor(quota * 3.4);
          quotaLeft = 30;
          fx.flash("verify", 1);
          message = "QUOTA MET — ERA " + era;
          messageAge = 0;
        } else {
          game.lives -= 1;
          quotaLeft = 30;
          fx.shake(8);
          fx.flash("danger", 1);
          message = "QUOTA MISSED";
          messageAge = 0;
          if (game.lives <= 0) game.over = true;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);

      centreText(ctx, ink, "RECEIPTS MINTED", 16, "dim");
      centreText(ctx, ink, String(Math.floor(bank)), 34, "bright", 16);
      centreText(ctx, ink, rate.toFixed(1) + "/s PASSIVE", 48, "verify");

      bits.draw(ctx, ink);
      drawPanel(ctx, ink, 8, 58, W - 16, 46);
      centreText(ctx, ink, "TAP TO MINT BY HAND", 82, "dim");

      drawPanel(ctx, ink, 8, 110, W - 16, 74);
      UPGRADES.forEach(function (u, i) {
        var y = 124 + i * 16;
        var afford = bank >= cost(i);
        if (i === pick) {
          ctx.fillStyle = ink.grid;
          ctx.fillRect(10, y - 9, W - 20, 14);
        }
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.fillStyle = afford ? ink.verify : ink.dim;
        ctx.fillText(u.name + " ×" + owned[i], 16, y);
        ctx.textAlign = "right";
        ctx.fillText(cost(i) + " rc", W - 16, y);
        ctx.textAlign = "left";
      });

      drawBar(ctx, ink, 8, 196, W - 16, 6, quotaLeft / 30, quotaLeft < 8 ? "danger" : "brass");
      centreText(ctx, ink, "QUOTA " + game.score + " / " + quota, 214, game.score >= quota ? "verify" : "danger");
      if (messageAge < 2) centreText(ctx, ink, message, 230, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ERA " + era + " · " + rate.toFixed(1) + "/s"; };
    return game;
  }

  /* ---- 29. DROP STACK ----------------------------------------------------

     Stack the deploys. Each one swings across and lands where you dropped it;
     whatever hangs over the edge shears off, so the tower narrows toward
     nothing and the only question is how many you get before it does. */
  function cabinetDropStack(random) {
    var game = { id: "drop-stack", score: 0, lives: 1, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var stack, current, camera, held, perfect;

    game.reset = function () {
      game.score = 0;
      game.lives = 1;
      game.over = false;
      stack = [{ x: W / 2 - 40, w: 80 }];
      camera = 0;
      perfect = 0;
      held = false;
      fx.reset();
      bits.clear();
      nextSlab();
    };

    function nextSlab() {
      var top = stack[stack.length - 1];
      var speed = Math.min(150, 58 + stack.length * 4);
      // Starting flush over the tower rather than off-screen: a slab that
      // begins beyond the edge makes the first second of every run an
      // unwinnable tap, which reads as the cabinet cheating rather than as
      // the player being early.
      current = {
        x: top.x,
        w: top.w,
        dir: stack.length % 2 ? 1 : -1,
        speed: speed
      };
    }

    function drop() {
      var top = stack[stack.length - 1];
      var left = Math.max(current.x, top.x);
      var right = Math.min(current.x + current.w, top.x + top.w);
      var overlap = right - left;

      if (overlap <= 0) {
        bits.burst(current.x + current.w / 2, H - 40 - stack.length * 8 + camera, 16, {
          colour: "danger", speed: 80, life: 0.6, gravity: 200
        });
        fx.shake(10);
        game.lives = 0;
        game.over = true;
        return;
      }

      var lost = current.w - overlap;
      if (lost < 2) {
        // Landing flush is worth rewarding, and is the only way to stop the
        // tower narrowing: a stacker with no perfect bonus always ends the same.
        perfect += 1;
        overlap = Math.min(96, overlap + 2);
        left = Math.max(0, left - 1);
        game.score += 12 + perfect * 4;
        fx.flash("verify", 0.7);
        fx.pop(left + overlap / 2, H - 46 - stack.length * 8 + camera, "FLUSH ×" + perfect, "verify");
      } else {
        perfect = 0;
        game.score += 5;
        bits.burst(lost > 0 && current.x < top.x ? left : right, H - 40 - stack.length * 8 + camera, 6, {
          colour: "brass", speed: 40, life: 0.5, gravity: 260
        });
      }

      stack.push({ x: left, w: overlap });
      fx.shake(2);
      if (stack.length > 8) camera += 8;
      nextSlab();
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      current.x += current.dir * current.speed * dt;
      if (current.x < -current.w) current.dir = 1;
      if (current.x > W) current.dir = -1;

      if (input.fire) {
        if (!held) {
          held = true;
          drop();
        }
      } else {
        held = false;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, camera * 2, 26);
      fx.begin(ctx);

      stack.forEach(function (slab, i) {
        var y = H - 24 - i * 8 + camera;
        if (y < -10 || y > H) return;
        ctx.fillStyle = i === stack.length - 1 ? ink.verify : i % 2 ? ink.wall[1] : ink.wall[2];
        ctx.fillRect(Math.round(slab.x), y, Math.round(slab.w), 8);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(slab.x), y + 7, Math.round(slab.w), 1);
      });

      var cy = H - 24 - stack.length * 8 + camera;
      ctx.fillStyle = ink.brass;
      ctx.fillRect(Math.round(current.x), cy, Math.round(current.w), 8);

      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("HEIGHT " + stack.length, 4, 12);
      if (perfect > 1) centreText(ctx, ink, "FLUSH STREAK ×" + perfect, 26, "verify");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "HEIGHT " + stack.length + " · FLUSH " + perfect; };
    return game;
  }

  /* ---- 30. SLICE QUEUE ---------------------------------------------------

     Payloads arc up out of the queue and you cut them open in mid-air. The
     signed ones are yours to inspect; the unsigned ones detonate, and the
     whole skill is that they look almost identical for the half second you
     have to decide. */
  function cabinetSliceQueue(random) {
    var game = { id: "slice-queue", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(80);
    var items, blade, spawnTimer, wave, combo, comboTimer, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      items = [];
      blade = { x: W / 2, y: H - 40, swing: 0 };
      spawnTimer = 0.6;
      wave = 1;
      combo = 0;
      comboTimer = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    function toss() {
      var bad = random() < Math.min(0.34, 0.12 + wave * 0.02);
      items.push({
        x: 30 + random() * (W - 60),
        y: H + 8,
        vx: (random() - 0.5) * 46,
        vy: -(132 + random() * 34),
        bad: bad,
        spin: random() * 6,
        cut: false
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      if (comboTimer > 0) comboTimer -= dt; else combo = 0;

      // The blade tracks the pointer when there is one and the keys otherwise,
      // so a phone slices by dragging and a keyboard slices by steering.
      if (input.pointerX != null) {
        blade.x = input.pointerX;
        blade.y = input.pointerY;
      } else {
        var speed = 150 * dt;
        if (input.left) blade.x -= speed;
        if (input.right) blade.x += speed;
        if (input.up) blade.y -= speed;
        if (input.down) blade.y += speed;
      }
      blade.x = clamp(blade.x, 0, W);
      blade.y = clamp(blade.y, 0, H);
      blade.swing = Math.max(0, blade.swing - dt * 4);
      if (input.fire && !held) {
        held = true;
        blade.swing = 1;
      } else if (!input.fire) {
        held = false;
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.35, 1.1 - wave * 0.05);
        toss();
        if (random() < 0.3) toss();
      }

      for (var i = items.length - 1; i >= 0; i -= 1) {
        var it = items[i];
        it.vy += 190 * dt;
        it.x += it.vx * dt;
        it.y += it.vy * dt;
        it.spin += dt * 5;

        var hit = !it.cut && Math.abs(it.x - blade.x) < 13 && Math.abs(it.y - blade.y) < 13;
        if (hit) {
          it.cut = true;
          if (it.bad) {
            game.lives -= 1;
            combo = 0;
            fx.shake(10);
            fx.flash("danger", 1);
            bits.burst(it.x, it.y, 18, { colour: "danger", speed: 90, life: 0.6, gravity: 120 });
            if (game.lives <= 0) { game.over = true; return; }
          } else {
            combo += 1;
            comboTimer = 1.1;
            game.score += 10 * Math.min(5, combo);
            fx.pop(it.x, it.y - 8, combo > 1 ? "×" + combo : "+10", "verify");
            bits.burst(it.x, it.y, 10, { colour: "verify", speed: 70, life: 0.5, gravity: 150 });
          }
          items.splice(i, 1);
          continue;
        }

        if (it.y > H + 20) {
          items.splice(i, 1);
          if (!it.bad && !it.cut) {
            // Only signed payloads cost anything when missed: letting an
            // unsigned one fall past is the right call.
            game.lives -= 1;
            combo = 0;
            fx.flash("danger", 0.6);
            if (game.lives <= 0) { game.over = true; return; }
          }
        }
      }

      wave = 1 + Math.floor(game.score / 250);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 12, W, 12);

      items.forEach(function (it) {
        var wobble = Math.sin(it.spin) * 2;
        ctx.fillStyle = it.bad ? ink.danger : ink.brass;
        ctx.fillRect(Math.round(it.x) - 7, Math.round(it.y) - 7 + wobble, 14, 14);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(it.x) - 4, Math.round(it.y) - 2 + wobble, 8, 2);
        if (!it.bad) {
          ctx.fillStyle = ink.verify;
          ctx.fillRect(Math.round(it.x) - 2, Math.round(it.y) - 5 + wobble, 4, 2);
        }
      });

      bits.draw(ctx, ink);

      var reach = blade.swing > 0 ? 12 : 7;
      ctx.fillStyle = blade.swing > 0 ? ink.bright : ink.ink;
      ctx.fillRect(Math.round(blade.x) - reach, Math.round(blade.y) - 1, reach * 2, 2);
      ctx.fillRect(Math.round(blade.x) - 1, Math.round(blade.y) - reach, 2, reach * 2);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("WAVE " + wave, 4, 12);
      if (combo > 1) centreText(ctx, ink, "COMBO ×" + combo, 24, "verify");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · COMBO " + combo; };
    return game;
  }

  /* ---- 31. LANE HOP ------------------------------------------------------

     Cross the lanes. Every lane is a service with its own traffic and its own
     rhythm, the log rafts are batch windows that carry you if you time them,
     and the board scrolls whether or not you moved — the genre's one cruelty,
     and the thing that separates it from the arcade original it descends from. */
  function cabinetLaneHop(random) {
    var LANE_H = 20;
    var LANES = Math.ceil(H / LANE_H) + 2;

    var game = { id: "lane-hop", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var lanes, hopper, scroll, best, held, drift;

    function makeLane(index) {
      var kind = index % 5 === 0 ? "safe" : random() < 0.55 ? "traffic" : "batch";
      var speed = (40 + random() * 60) * (random() < 0.5 ? -1 : 1) * (1 + index * 0.008);
      var things = [];
      var count = kind === "safe" ? 0 : 2 + Math.floor(random() * 3);
      for (var i = 0; i < count; i += 1) {
        things.push({ x: (i * W) / count + random() * 30, w: kind === "batch" ? 44 : 22 });
      }
      return { kind: kind, speed: speed, things: things };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      lanes = [];
      for (var i = 0; i < LANES; i += 1) lanes.push(makeLane(i));
      hopper = { x: W / 2, row: 2 };
      scroll = 0;
      best = 0;
      drift = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    function laneAt(row) {
      return lanes[row % lanes.length];
    }

    function die(reason) {
      game.lives -= 1;
      fx.shake(9);
      fx.flash("danger", 1);
      bits.burst(hopper.x, H - 30 - hopper.row * LANE_H + scroll, 14, { colour: "danger", speed: 80, life: 0.5 });
      if (game.lives <= 0) { game.over = true; return; }
      hopper.x = W / 2;
      hopper.row = Math.max(0, hopper.row - 2);
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      lanes.forEach(function (lane) {
        lane.things.forEach(function (t) {
          t.x += lane.speed * dt;
          if (t.x > W + 50) t.x = -50;
          if (t.x < -50) t.x = W + 50;
        });
      });

      var pressed = input.up || input.down || input.left || input.right;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.up) {
          hopper.row += 1;
          game.score += 5;
          if (hopper.row > best) best = hopper.row;
        } else if (input.down && hopper.row > 0) hopper.row -= 1;
        else if (input.left) hopper.x -= 18;
        else if (input.right) hopper.x += 18;
        hopper.x = clamp(hopper.x, 8, W - 8);
      }

      // The camera creeps forward on its own; falling off the bottom is a loss.
      scroll += (14 + best * 0.25) * dt;
      var y = H - 30 - hopper.row * LANE_H + scroll;

      var lane = laneAt(hopper.row);
      if (lane.kind === "batch") {
        // Riding a batch window carries you with it — and off the edge if you
        // stay aboard too long.
        var riding = lane.things.some(function (t) {
          return hopper.x > t.x && hopper.x < t.x + t.w;
        });
        if (riding) {
          hopper.x += lane.speed * dt;
          drift = lane.speed;
          if (hopper.x < 2 || hopper.x > W - 2) { die("carried off"); return; }
        } else if (y < H && y > -10) {
          die("no batch window");
          return;
        }
      } else if (lane.kind === "traffic") {
        var struck = lane.things.some(function (t) {
          return hopper.x > t.x - 6 && hopper.x < t.x + t.w + 6;
        });
        if (struck) { die("hit by traffic"); return; }
      }

      if (y > H + 8) { die("fell behind"); return; }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);

      for (var row = 0; row < LANES; row += 1) {
        var y = H - 30 - row * LANE_H + scroll;
        if (y < -LANE_H || y > H) continue;
        var lane = laneAt(row);
        ctx.fillStyle = lane.kind === "safe" ? ink.wall[3] : lane.kind === "batch" ? ink.grid : ink.bg;
        ctx.fillRect(0, y - LANE_H + 4, W, LANE_H);
        lane.things.forEach(function (t) {
          ctx.fillStyle = lane.kind === "batch" ? ink.wall[1] : ink.danger;
          ctx.fillRect(Math.round(t.x), y - LANE_H + 7, t.w, LANE_H - 6);
          if (lane.kind !== "batch") {
            ctx.fillStyle = ink.bright;
            ctx.fillRect(Math.round(t.x) + (lane.speed > 0 ? t.w - 4 : 1), y - LANE_H + 9, 3, 3);
          }
        });
      }

      bits.draw(ctx, ink);
      var hy = H - 30 - hopper.row * LANE_H + scroll;
      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(hopper.x) - 5, Math.round(hy) - 10, 10, 10);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(Math.round(hopper.x) - 3, Math.round(hy) - 7, 2, 2);
      ctx.fillRect(Math.round(hopper.x) + 1, Math.round(hy) - 7, 2, 2);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LANES " + best, 4, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LANE " + hopper.row + " · BEST " + best; };
    return game;
  }

  /* ---- 32. SWARM ---------------------------------------------------------

     The survivor auto-shooter. You never press fire — the revocation aura
     fires itself on a timer and all you do is move, which is the whole design
     insight of the genre and, as it happens, an accurate picture of a policy
     engine: it is always running, and your only input is where you stand. */
  function cabinetSwarm(random) {
    var WALKER = makeSprite([
      ".111.",
      "12221",
      "12221",
      ".111.",
      ".1.1."
    ], ["danger", "bg"]);

    var game = { id: "swarm", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(90);
    var hero, mob, shots, level, xp, need, timer, cooldown, aura, spawnTimer, hurt;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      hero = { x: W / 2, y: H / 2, hp: 100 };
      mob = [];
      shots = [];
      level = 1;
      xp = 0;
      need = 6;
      timer = 0;
      cooldown = 0;
      aura = { rate: 0.75, damage: 1, count: 1 };
      spawnTimer = 0;
      hurt = 0;
      fx.reset();
      bits.clear();
    };

    function spawn() {
      var edge = Math.floor(random() * 4);
      var x = edge === 0 ? -8 : edge === 1 ? W + 8 : random() * W;
      var y = edge === 2 ? -8 : edge === 3 ? H + 8 : random() * H;
      // Capped, and scaling in both directions: an auto-shooter whose crowd
      // stops getting faster is one a level-20 aura clears forever, which the
      // random-hand soak found by surviving five minutes without trying.
      if (mob.length > 90) return;
      var elite = level > 6 && random() < 0.18;
      mob.push({
        x: x,
        y: y,
        hp: (1 + Math.floor(level / 3)) * (elite ? 4 : 1),
        speed: (24 + level * 2.4) * (elite ? 0.8 : 1),
        elite: elite
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      timer += dt;
      hurt = Math.max(0, hurt - dt);

      var speed = 62 * dt;
      var mx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var my = (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (mx && my) { mx *= 0.7071; my *= 0.7071; }
      hero.x = clamp(hero.x + mx * speed, 6, W - 6);
      hero.y = clamp(hero.y + my * speed, 16, H - 6);

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.09, 1.1 - level * 0.06);
        spawn();
        if (level > 4) spawn();
        if (level > 9 && random() < 0.6) spawn();
      }

      // The aura fires on its own, at the nearest targets, forever.
      cooldown -= dt;
      if (cooldown <= 0 && mob.length) {
        cooldown = aura.rate;
        var sorted = mob.slice().sort(function (a, b) {
          return (a.x - hero.x) * (a.x - hero.x) + (a.y - hero.y) * (a.y - hero.y) -
                 ((b.x - hero.x) * (b.x - hero.x) + (b.y - hero.y) * (b.y - hero.y));
        });
        for (var i = 0; i < Math.min(aura.count, sorted.length); i += 1) {
          var t = sorted[i];
          var d = Math.hypot(t.x - hero.x, t.y - hero.y) || 1;
          shots.push({ x: hero.x, y: hero.y, vx: (t.x - hero.x) / d * 140, vy: (t.y - hero.y) / d * 140, life: 1.6 });
        }
      }

      for (var s = shots.length - 1; s >= 0; s -= 1) {
        var sh = shots[s];
        sh.x += sh.vx * dt;
        sh.y += sh.vy * dt;
        sh.life -= dt;
        if (sh.life <= 0) { shots.splice(s, 1); continue; }
        for (var m = mob.length - 1; m >= 0; m -= 1) {
          var e = mob[m];
          if (Math.abs(e.x - sh.x) > 6 || Math.abs(e.y - sh.y) > 6) continue;
          e.hp -= aura.damage;
          shots.splice(s, 1);
          if (e.hp <= 0) {
            mob.splice(m, 1);
            game.score += 5;
            xp += 1;
            bits.burst(e.x, e.y, 6, { colour: "danger", speed: 60, life: 0.4 });
          }
          break;
        }
      }

      for (var k = mob.length - 1; k >= 0; k -= 1) {
        var q = mob[k];
        var dd = Math.hypot(hero.x - q.x, hero.y - q.y) || 1;
        q.x += ((hero.x - q.x) / dd) * q.speed * dt;
        q.y += ((hero.y - q.y) / dd) * q.speed * dt;
        if (dd < 8 && hurt <= 0) {
          hurt = 0.6;
          hero.hp -= 12;
          fx.shake(6);
          fx.flash("danger", 0.8);
          if (hero.hp <= 0) {
            game.lives -= 1;
            hero.hp = 100;
            mob.length = 0;
            if (game.lives <= 0) { game.over = true; return; }
            // The crowd was just emptied out from under this loop, so the
            // remaining indices point at nothing. Leaving rather than
            // continuing: the alternative reads the hole and throws.
            break;
          }
        }
      }

      if (xp >= need) {
        // Levelling picks its own upgrade. A menu here would be the better
        // game and the wrong cabinet: this one is about not stopping.
        xp -= need;
        level += 1;
        need = Math.floor(need * 1.35);
        game.score += 25;
        var roll = level % 3;
        if (roll === 0) aura.count += 1;
        else if (roll === 1) aura.rate = Math.max(0.16, aura.rate * 0.86);
        else aura.damage += 1;
        fx.flash("verify", 1);
        fx.pop(hero.x, hero.y - 14, "LEVEL " + level, "verify");
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = ink.grid;
      for (var g = 0; g < W; g += 24) ctx.fillRect(g, 14, 1, H - 14);
      for (var r = 14; r < H; r += 24) ctx.fillRect(0, r, W, 1);
      fx.begin(ctx);

      mob.forEach(function (e) { WALKER.drawAt(ctx, ink, e.x, e.y, e.elite ? 3 : 2); });
      shots.forEach(function (s) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(s.x) - 1, Math.round(s.y) - 1, 3, 3);
      });
      bits.draw(ctx, ink);

      ctx.fillStyle = hurt > 0 ? ink.bright : ink.verify;
      ctx.fillRect(Math.round(hero.x) - 4, Math.round(hero.y) - 5, 8, 10);

      drawBar(ctx, ink, 4, 4, 80, 5, hero.hp / 100, hero.hp > 35 ? "verify" : "danger");
      drawBar(ctx, ink, W - 84, 4, 80, 5, xp / need, "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LV " + level, W / 2 - 14, 10);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LV " + level + " · " + mob.length + " INBOUND"; };
    return game;
  }

  /* ---- 33. BULLET LEDGER -------------------------------------------------

     Curtain-fire dodging. The patterns are denials radiating out of a policy
     core, your hitbox is one pixel at the centre of a much larger sprite (the
     genre's oldest kindness), and grazing a denial without being hit by it is
     worth more than avoiding it entirely. */
  function cabinetBulletLedger(random) {
    var game = { id: "bullet-ledger", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(70);
    var ship, shots, core, phase, phaseTimer, fireTimer, graze, hurt, wave;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      ship = { x: W / 2, y: H - 34 };
      shots = [];
      core = { x: W / 2, y: 48, hp: 60, max: 60, angle: 0 };
      phase = 0;
      phaseTimer = 6;
      fireTimer = 0;
      graze = 0;
      hurt = 0;
      wave = 1;
      fx.reset();
      bits.clear();
    };

    function emit(count, speed, spin) {
      for (var i = 0; i < count; i += 1) {
        var a = core.angle + (i / count) * Math.PI * 2 + spin;
        shots.push({ x: core.x, y: core.y, vx: Math.cos(a) * speed, vy: Math.sin(a) * speed });
      }
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      core.angle += dt * 0.9;

      // Focus: holding fire halves your speed for threading a gap, which is
      // the other half of the genre's grammar.
      var focus = input.fire;
      var speed = (focus ? 42 : 92) * dt;
      if (input.left) ship.x -= speed;
      if (input.right) ship.x += speed;
      if (input.up) ship.y -= speed;
      if (input.down) ship.y += speed;
      ship.x = clamp(ship.x, 5, W - 5);
      ship.y = clamp(ship.y, 18, H - 5);

      phaseTimer -= dt;
      if (phaseTimer <= 0) {
        phase = (phase + 1) % 3;
        phaseTimer = 6;
      }

      fireTimer -= dt;
      if (fireTimer <= 0) {
        var tier = Math.min(3, 1 + Math.floor(wave / 2));
        if (phase === 0) { emit(8 + tier * 2, 48, 0); fireTimer = 0.5; }
        else if (phase === 1) { emit(3, 62, Math.sin(core.angle * 2)); fireTimer = 0.16; }
        else {
          var d = Math.atan2(ship.y - core.y, ship.x - core.x);
          for (var i = -1; i <= 1; i += 1) {
            shots.push({ x: core.x, y: core.y, vx: Math.cos(d + i * 0.22) * 76, vy: Math.sin(d + i * 0.22) * 76 });
          }
          fireTimer = 0.42;
        }
      }

      for (var s = shots.length - 1; s >= 0; s -= 1) {
        var b = shots[s];
        b.x += b.vx * dt;
        b.y += b.vy * dt;
        if (b.x < -10 || b.x > W + 10 || b.y < -10 || b.y > H + 10) { shots.splice(s, 1); continue; }
        var dx = Math.abs(b.x - ship.x), dy = Math.abs(b.y - ship.y);
        if (dx < 2 && dy < 2) {
          shots.splice(s, 1);
          game.lives -= 1;
          hurt = 1;
          fx.shake(11);
          fx.flash("danger", 1);
          bits.burst(ship.x, ship.y, 16, { colour: "danger", speed: 80, life: 0.6 });
          if (game.lives <= 0) { game.over = true; return; }
        } else if (dx < 9 && dy < 9 && !b.grazed) {
          b.grazed = true;
          graze += 1;
          game.score += 2;
          if (graze % 10 === 0) fx.pop(ship.x, ship.y - 12, "GRAZE ×" + graze, "brass");
        }
      }

      // The core takes damage from proximity: you have no gun, so the only
      // way to end a wave is to be brave about where you stand.
      if (Math.abs(ship.x - core.x) < 26 && Math.abs(ship.y - core.y) < 26) {
        core.hp -= 14 * dt;
        game.score += Math.round(20 * dt);
        if (core.hp <= 0) {
          wave += 1;
          core.hp = core.max = 60 + wave * 22;
          core.x = 40 + random() * (W - 80);
          shots.length = 0;
          game.score += 120;
          fx.flash("verify", 1);
          bits.burst(core.x, core.y, 24, { colour: "verify", speed: 110, life: 0.8 });
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 24);
      fx.begin(ctx);

      ctx.fillStyle = ink.danger;
      ctx.fillRect(core.x - 10, core.y - 10, 20, 20);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(core.x - 5, core.y - 5, 10, 10);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(core.x - 2, core.y - 2, 4, 4);

      shots.forEach(function (b) {
        ctx.fillStyle = b.grazed ? ink.brass : ink.danger;
        ctx.fillRect(Math.round(b.x) - 2, Math.round(b.y) - 2, 4, 4);
      });
      bits.draw(ctx, ink);

      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(Math.round(ship.x) - 4, Math.round(ship.y) - 5, 8, 10);
      // The one-pixel hitbox, drawn, because a genre that hides it teaches
      // the wrong lesson in the ten seconds this cabinet gets.
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(ship.x), Math.round(ship.y), 1, 1);

      drawBar(ctx, ink, 60, 6, W - 120, 5, core.hp / core.max, "danger");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("W" + wave, 4, 12);
      ctx.fillText("GRAZE " + graze, W - 60, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · GRAZE " + graze; };
    return game;
  }

  /* ---- 34. CHOKEPOINT ----------------------------------------------------

     Tower defence. Calls walk a fixed path toward the tool; you spend budget
     placing denials beside it. The genre's real subject is that you cannot
     cover everything, so you pick where the path bends. */
  function cabinetChokepoint(random) {
    var CELL = 16;
    var COLS = 20;
    var ROWS = 12;
    var OY = 30;
    // A fixed serpentine route: a generated one makes the first ten seconds
    // of every run a reading exercise instead of a placement decision.
    var PATH = [];
    (function () {
      var y = 2;
      for (var leg = 0; leg < 3; leg += 1) {
        var left = leg % 2 === 0;
        for (var x = 0; x < COLS; x += 1) PATH.push({ x: left ? x : COLS - 1 - x, y: y });
        y += 4;
        if (y < ROWS) {
          var cx = left ? COLS - 1 : 0;
          PATH.push({ x: cx, y: y - 3 });
          PATH.push({ x: cx, y: y - 2 });
          PATH.push({ x: cx, y: y - 1 });
        }
      }
    })();

    var game = { id: "chokepoint", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var towers, calls, cursor, budget, wave, spawnLeft, spawnTimer, held, message, messageAge;

    function onPath(cx, cy) {
      return PATH.some(function (p) { return p.x === cx && p.y === cy; });
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      towers = [];
      calls = [];
      cursor = { x: 3, y: 4 };
      budget = 60;
      wave = 1;
      spawnLeft = 6;
      spawnTimer = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        // Fire is tested before the directions, here and in every other
        // edge-triggered cabinet. Holding a direction and tapping the action
        // is the ordinary gesture on the touch pad, and a chain that reads
        // the arrows first silently eats the tap for as long as the thumb is
        // down — which reads as an action button that only works sometimes.
        if (input.fire) {
          var taken = towers.some(function (t) { return t.x === cursor.x && t.y === cursor.y; });
          if (onPath(cursor.x, cursor.y)) {
            message = "THE PATH IS NOT YOURS TO BUILD ON";
            messageAge = 0;
          } else if (taken) {
            message = "ALREADY DENIED HERE";
            messageAge = 0;
          } else if (budget < 25) {
            message = "BUDGET SHORT";
            messageAge = 0;
          } else {
            budget -= 25;
            towers.push({ x: cursor.x, y: cursor.y, cool: 0 });
            fx.flash("verify", 0.6);
          }
        } else if (input.left) cursor.x = Math.max(0, cursor.x - 1);
        else if (input.right) cursor.x = Math.min(COLS - 1, cursor.x + 1);
        else if (input.up) cursor.y = Math.max(0, cursor.y - 1);
        else if (input.down) cursor.y = Math.min(ROWS - 1, cursor.y + 1);
      }

      spawnTimer -= dt;
      if (spawnLeft > 0 && spawnTimer <= 0) {
        spawnTimer = Math.max(0.35, 1.2 - wave * 0.06);
        spawnLeft -= 1;
        calls.push({ t: 0, hp: 2 + wave, max: 2 + wave, speed: 1.4 + wave * 0.12 });
      }

      for (var i = calls.length - 1; i >= 0; i -= 1) {
        var c = calls[i];
        c.t += c.speed * dt;
        if (c.t >= PATH.length - 1) {
          calls.splice(i, 1);
          game.lives -= 1;
          fx.shake(9);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        }
      }

      towers.forEach(function (t) {
        t.cool -= dt;
        if (t.cool > 0) return;
        for (var i = 0; i < calls.length; i += 1) {
          var node = PATH[Math.min(PATH.length - 1, Math.floor(calls[i].t))];
          if (Math.abs(node.x - t.x) > 2 || Math.abs(node.y - t.y) > 2) continue;
          t.cool = 0.55;
          calls[i].hp -= 1;
          bits.burst(node.x * CELL + 8, OY + node.y * CELL + 8, 3, { colour: "bright", speed: 40, life: 0.3 });
          if (calls[i].hp <= 0) {
            calls.splice(i, 1);
            budget += 14;
            game.score += 20;
          }
          break;
        }
      });

      if (!spawnLeft && !calls.length) {
        wave += 1;
        spawnLeft = 5 + wave * 2;
        budget += 40;
        game.score += 60;
        message = "WAVE " + wave;
        messageAge = 0;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);

      PATH.forEach(function (p) {
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(p.x * CELL, OY + p.y * CELL, CELL, CELL);
      });
      towers.forEach(function (t) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(t.x * CELL + 3, OY + t.y * CELL + 3, CELL - 6, CELL - 6);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(t.x * CELL + 6, OY + t.y * CELL + 6, CELL - 12, CELL - 12);
      });
      calls.forEach(function (c) {
        var node = PATH[Math.min(PATH.length - 1, Math.floor(c.t))];
        var x = node.x * CELL + 8, y = OY + node.y * CELL + 8;
        ctx.fillStyle = ink.danger;
        ctx.fillRect(x - 5, y - 5, 10, 10);
        drawBar(ctx, ink, x - 6, y - 9, 12, 2, c.hp / c.max, "brass");
      });

      ctx.fillStyle = ink.bright;
      ctx.fillRect(cursor.x * CELL, OY + cursor.y * CELL, CELL, 2);
      ctx.fillRect(cursor.x * CELL, OY + cursor.y * CELL + CELL - 2, CELL, 2);
      ctx.fillRect(cursor.x * CELL, OY + cursor.y * CELL, 2, CELL);
      ctx.fillRect(cursor.x * CELL + CELL - 2, OY + cursor.y * CELL, 2, CELL);

      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BUDGET " + budget, 4, 12);
      ctx.fillText("WAVE " + wave, W - 60, 12);
      ctx.fillText("DENIAL COSTS 25", 4, 24);
      if (messageAge < 2) centreText(ctx, ink, message, H - 6, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · " + budget + " BUDGET"; };
    return game;
  }

  /* ---- 35. DECK OF SCOPES ------------------------------------------------

     The deck-builder, compressed to the one turn it is actually about: you
     hold five scopes, each costs energy, and the thing across the table is
     going to hit you for a number it has already told you. Everything else in
     the genre is that decision with more nouns. */
  function cabinetDeckOfScopes(random) {
    var CARDS = [
      { name: "DENY", cost: 1, dmg: 6, block: 0, note: "small, honest" },
      { name: "REVOKE", cost: 2, dmg: 11, block: 0, note: "takes a grant back" },
      { name: "AUDIT", cost: 1, dmg: 3, block: 5, note: "look, and cover" },
      { name: "QUARANTINE", cost: 2, dmg: 0, block: 12, note: "nothing gets through" },
      { name: "ESCALATE", cost: 3, dmg: 20, block: -4, note: "loud, and it costs" }
    ];

    var game = { id: "deck-of-scopes", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var hand, pick, energy, block, hp, foe, tier, phase, timer, held, log;

    function newFoe() {
      foe = {
        hp: 26 + tier * 12,
        max: 26 + tier * 12,
        hit: 6 + tier * 3,
        wind: 2
      };
    }

    function draw5() {
      hand = [];
      for (var i = 0; i < 5; i += 1) hand.push(CARDS[Math.floor(random() * CARDS.length)]);
      energy = 3;
      pick = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      hp = 40;
      block = 0;
      tier = 1;
      phase = "you";
      timer = 0;
      held = false;
      log = "THE DECK IS THE POLICY.";
      newFoe();
      draw5();
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);

      if (phase === "foe") {
        timer -= dt;
        if (timer > 0) return;
        var incoming = Math.max(0, foe.hit - block);
        hp -= incoming;
        block = 0;
        fx.shake(incoming ? 7 : 2);
        log = incoming ? "TOOK " + incoming : "BLOCKED IT ALL";
        if (hp <= 0) {
          game.lives -= 1;
          hp = 40;
          if (game.lives <= 0) { game.over = true; return; }
          log = "RUN ENDED. RESHUFFLE.";
        }
        phase = "you";
        draw5();
        return;
      }

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        playCard();
      } else if (input.left) pick = (pick + hand.length - 1) % hand.length;
      else if (input.right) pick = (pick + 1) % hand.length;
      else if (input.down || input.up) {
        // End turn: the deliberate skip the genre needs, on the axis the hand
        // does not use.
        phase = "foe";
        timer = 0.7;
      }

      function playCard() {
        var card = hand[pick];
        if (!card) return;
        if (card.cost > energy) {
          log = "NOT ENOUGH ENERGY";
          return;
        }
        energy -= card.cost;
        block = Math.max(0, block + card.block);
        if (card.dmg) {
          foe.hp -= card.dmg;
          fx.pop(W / 2, 60, "-" + card.dmg, "danger");
        }
        log = card.name + " PLAYED";
        hand.splice(pick, 1);
        if (pick >= hand.length) pick = Math.max(0, hand.length - 1);
        game.score += card.dmg;

        if (foe.hp <= 0) {
          tier += 1;
          game.score += 80;
          hp = Math.min(40, hp + 8);
          newFoe();
          draw5();
          fx.flash("verify", 1);
          log = "CLEARED. NEXT TIER.";
          return;
        }
        if (!hand.length || energy <= 0) {
          phase = "foe";
          timer = 0.7;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);

      ctx.fillStyle = ink.danger;
      ctx.fillRect(W / 2 - 30, 26, 60, 40);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(W / 2 - 18, 38, 10, 6);
      ctx.fillRect(W / 2 + 8, 38, 10, 6);
      drawBar(ctx, ink, W / 2 - 40, 72, 80, 5, foe.hp / foe.max, "danger");
      centreText(ctx, ink, "TIER " + tier + " · HITS FOR " + foe.hit, 86, "dim");

      drawPanel(ctx, ink, 6, 96, W - 12, 20);
      centreText(ctx, ink, log, 108, "ink");

      hand.forEach(function (card, i) {
        var x = 8 + i * 61;
        var y = 130;
        ctx.fillStyle = i === pick ? ink.brass : ink.wall[2];
        ctx.fillRect(x, y, 56, 62);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 2, y + 2, 52, 58);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = i === pick ? ink.bright : ink.ink;
        ctx.fillText(card.name.slice(0, 9), x + 5, y + 14);
        ctx.fillStyle = ink.dim;
        ctx.fillText(card.cost + " NRG", x + 5, y + 28);
        if (card.dmg) ctx.fillText(card.dmg + " DMG", x + 5, y + 40);
        if (card.block) ctx.fillText(card.block + " BLK", x + 5, y + 52);
      });

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.verify;
      ctx.fillText("HP " + Math.max(0, hp) + "/40", 6, 208);
      ctx.fillStyle = ink.brass;
      ctx.fillText("ENERGY " + energy, 90, 208);
      ctx.fillStyle = ink.dim;
      ctx.fillText("BLOCK " + block, 176, 208);
      centreText(ctx, ink, "← → pick · SPACE play · ↑↓ end turn", 226, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "TIER " + tier + " · HP " + Math.max(0, hp); };
    return game;
  }

  /* ---- 36. BACKSTOP ------------------------------------------------------

     Wave defence around a core you cannot move. Everything the genre does
     with turrets and repair drones, compressed into one gun that overheats:
     the interesting decision is when to stop shooting. */
  function cabinetBackstop(random) {
    var game = { id: "backstop", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(70);
    var angle, heat, locked, shots, foes, wave, spawnLeft, spawnTimer, core, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      angle = -Math.PI / 2;
      heat = 0;
      locked = false;
      shots = [];
      foes = [];
      wave = 1;
      spawnLeft = 6;
      spawnTimer = 0.5;
      core = { hp: 100 };
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (input.left) angle -= 2.4 * dt;
      if (input.right) angle += 2.4 * dt;

      // Heat: firing adds, not firing sheds, and a lockout costs you the
      // seconds a wave needs to reach the core.
      if (input.fire && !locked) {
        heat += 0.85 * dt;
        if (!held) {
          held = true;
          shots.push({ x: W / 2, y: H / 2, vx: Math.cos(angle) * 190, vy: Math.sin(angle) * 190, life: 1.4 });
          bits.burst(W / 2 + Math.cos(angle) * 14, H / 2 + Math.sin(angle) * 14, 2, { colour: "bright", speed: 30, life: 0.2 });
        }
      } else {
        held = false;
        heat -= 0.5 * dt;
      }
      heat = clamp(heat, 0, 1);
      if (heat >= 1) locked = true;
      if (locked && heat <= 0.25) locked = false;

      spawnTimer -= dt;
      if (spawnLeft > 0 && spawnTimer <= 0) {
        spawnTimer = Math.max(0.3, 1.3 - wave * 0.07);
        spawnLeft -= 1;
        var a = random() * Math.PI * 2;
        foes.push({ x: W / 2 + Math.cos(a) * 190, y: H / 2 + Math.sin(a) * 190, speed: 20 + wave * 2.4, hp: 1 + Math.floor(wave / 4) });
      }

      for (var s = shots.length - 1; s >= 0; s -= 1) {
        var sh = shots[s];
        sh.x += sh.vx * dt;
        sh.y += sh.vy * dt;
        sh.life -= dt;
        if (sh.life <= 0) { shots.splice(s, 1); continue; }
        for (var f = foes.length - 1; f >= 0; f -= 1) {
          if (Math.abs(foes[f].x - sh.x) > 7 || Math.abs(foes[f].y - sh.y) > 7) continue;
          foes[f].hp -= 1;
          shots.splice(s, 1);
          if (foes[f].hp <= 0) {
            bits.burst(foes[f].x, foes[f].y, 8, { colour: "danger", speed: 70, life: 0.45 });
            foes.splice(f, 1);
            game.score += 15;
          }
          break;
        }
      }

      for (var i = foes.length - 1; i >= 0; i -= 1) {
        var e = foes[i];
        var d = Math.hypot(W / 2 - e.x, H / 2 - e.y) || 1;
        e.x += ((W / 2 - e.x) / d) * e.speed * dt;
        e.y += ((H / 2 - e.y) / d) * e.speed * dt;
        if (d < 14) {
          foes.splice(i, 1);
          core.hp -= 12;
          fx.shake(8);
          fx.flash("danger", 0.9);
          if (core.hp <= 0) {
            game.lives -= 1;
            core.hp = 100;
            foes.length = 0;
            if (game.lives <= 0) { game.over = true; return; }
          }
        }
      }

      if (!spawnLeft && !foes.length) {
        wave += 1;
        spawnLeft = 5 + wave * 2;
        core.hp = Math.min(100, core.hp + 15);
        game.score += 70;
        fx.flash("verify", 0.8);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 20);
      fx.begin(ctx);

      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(W / 2 - 12, H / 2 - 12, 24, 24);
      ctx.fillStyle = core.hp > 40 ? ink.verify : ink.danger;
      ctx.fillRect(W / 2 - 8, H / 2 - 8, 16, 16);
      ctx.fillStyle = locked ? ink.danger : ink.bright;
      ctx.fillRect(
        Math.round(W / 2 + Math.cos(angle) * 12) - 2,
        Math.round(H / 2 + Math.sin(angle) * 12) - 2, 5, 5
      );

      foes.forEach(function (e) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.round(e.x) - 5, Math.round(e.y) - 5, 10, 10);
      });
      shots.forEach(function (s) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(s.x) - 1, Math.round(s.y) - 1, 3, 3);
      });
      bits.draw(ctx, ink);

      drawBar(ctx, ink, 4, 4, 90, 5, core.hp / 100, core.hp > 40 ? "verify" : "danger");
      drawBar(ctx, ink, W - 94, 4, 90, 5, heat, locked ? "danger" : "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = locked ? ink.danger : ink.dim;
      ctx.fillText(locked ? "OVERHEATED" : "HEAT", W - 94, 18);
      ctx.fillStyle = ink.dim;
      ctx.fillText("WAVE " + wave, 4, 18);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · CORE " + Math.max(0, core.hp); };
    return game;
  }

  /* ---- 37. COLD MOVE -----------------------------------------------------

     Crate-pushing. Shove each cold record onto a pad; a record shoved into a
     corner is stuck forever, and so is the run — the genre's whole tension is
     that it has no undo and neither does a migration. */
  function cabinetColdMove(random) {
    var CELL = 18;
    var COLS = 11;
    var ROWS = 9;
    var OX = (W - COLS * CELL) / 2;
    var OY = 40;

    var game = { id: "cold-move", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var walls, crates, pads, hero, level, moves, held, message, messageAge;

    function build() {
      walls = [];
      for (var y = 0; y < ROWS; y += 1) {
        var row = [];
        for (var x = 0; x < COLS; x += 1) {
          row.push(x === 0 || y === 0 || x === COLS - 1 || y === ROWS - 1 ? 1 : 0);
        }
        walls.push(row);
      }
      // Interior blocks, never on the border ring the player needs to walk.
      var blocks = Math.min(7, 2 + level);
      for (var b = 0; b < blocks; b += 1) {
        var bx = 2 + Math.floor(random() * (COLS - 4));
        var by = 2 + Math.floor(random() * (ROWS - 4));
        walls[by][bx] = 1;
      }
      crates = [];
      pads = [];
      var count = Math.min(4, 1 + Math.floor(level / 2));
      for (var c = 0; c < count; c += 1) {
        var cx, cy, guard = 0;
        do {
          cx = 2 + Math.floor(random() * (COLS - 4));
          cy = 2 + Math.floor(random() * (ROWS - 4));
          guard += 1;
        } while (guard < 60 && (walls[cy][cx] || crates.some(function (k) { return k.x === cx && k.y === cy; })));
        walls[cy][cx] = 0;
        crates.push({ x: cx, y: cy });
        var px, py, pguard = 0;
        do {
          px = 1 + Math.floor(random() * (COLS - 2));
          py = 1 + Math.floor(random() * (ROWS - 2));
          pguard += 1;
        } while (pguard < 60 && (walls[py][px] || pads.some(function (k) { return k.x === px && k.y === py; })));
        walls[py][px] = 0;
        pads.push({ x: px, y: py });
      }
      hero = { x: 1, y: 1 };
      walls[1][1] = 0;
      moves = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    function crateAt(x, y) {
      for (var i = 0; i < crates.length; i += 1) if (crates[i].x === x && crates[i].y === y) return crates[i];
      return null;
    }

    function solved() {
      return pads.every(function (p) { return crateAt(p.x, p.y); });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;

      var dx = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var dy = dx ? 0 : (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (!dx && !dy) { held = false; return; }
      if (held) return;
      held = true;

      var nx = hero.x + dx, ny = hero.y + dy;
      if (walls[ny] && walls[ny][nx]) return;
      var crate = crateAt(nx, ny);
      if (crate) {
        var bx = nx + dx, by = ny + dy;
        if ((walls[by] && walls[by][bx]) || crateAt(bx, by)) return;
        crate.x = bx;
        crate.y = by;
        if (pads.some(function (p) { return p.x === bx && p.y === by; })) {
          fx.flash("verify", 0.6);
          game.score += 15;
        }
      }
      hero.x = nx;
      hero.y = ny;
      moves += 1;

      if (solved()) {
        level += 1;
        game.score += 100;
        message = "FILED — LEVEL " + level;
        messageAge = 0;
        fx.flash("verify", 1);
        build();
      } else if (moves > 90 + level * 20) {
        // The soft failure the genre lacks: no undo, so a wedged board has to
        // end on a clock rather than on the player noticing.
        game.lives -= 1;
        message = "WEDGED — RESHUFFLING";
        messageAge = 0;
        fx.shake(8);
        if (game.lives <= 0) { game.over = true; return; }
        build();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (!walls[y][x]) continue;
          ctx.fillStyle = ink.wall[1];
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL - 1, CELL - 1);
        }
      }
      pads.forEach(function (p) {
        ctx.fillStyle = ink.grid;
        ctx.fillRect(OX + p.x * CELL + 3, OY + p.y * CELL + 3, CELL - 7, CELL - 7);
        ctx.fillStyle = ink.verify;
        ctx.fillRect(OX + p.x * CELL + 7, OY + p.y * CELL + 7, 3, 3);
      });
      crates.forEach(function (c) {
        var on = pads.some(function (p) { return p.x === c.x && p.y === c.y; });
        ctx.fillStyle = on ? ink.verify : ink.brass;
        ctx.fillRect(OX + c.x * CELL + 2, OY + c.y * CELL + 2, CELL - 5, CELL - 5);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(OX + c.x * CELL + 5, OY + c.y * CELL + 5, CELL - 11, CELL - 11);
      });
      ctx.fillStyle = ink.bright;
      ctx.fillRect(OX + hero.x * CELL + 4, OY + hero.y * CELL + 3, CELL - 9, CELL - 6);

      centreText(ctx, ink, "COLD MOVE — NO UNDO", 20, "dim");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 6, 32);
      ctx.fillText("MOVES " + moves, W - 74, 32);
      if (messageAge < 2) centreText(ctx, ink, message, H - 8, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · MOVES " + moves; };
    return game;
  }

  /* ---- 38. QUORUM FLIP ---------------------------------------------------

     Lights-out on a grid of nodes. Toggling one toggles its neighbours, the
     goal is unanimity, and the reason it is hard is the reason distributed
     consensus is hard: every local fix has non-local consequences. */
  function cabinetQuorumFlip(random) {
    var SIZE = 5;
    var CELL = 30;
    var OX = (W - SIZE * CELL) / 2;
    var OY = 52;

    var game = { id: "quorum-flip", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var cells, cursor, round, taps, held, message, messageAge;

    function toggle(x, y) {
      if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) return;
      cells[y * SIZE + x] = !cells[y * SIZE + x];
    }

    function deal() {
      cells = [];
      for (var i = 0; i < SIZE * SIZE; i += 1) cells.push(true);
      // Scrambled by playing legal moves backwards, so every board is
      // guaranteed solvable — a random fill is not, and half of them would be
      // impossible.
      var shuffles = Math.min(14, 3 + round * 2);
      for (var s = 0; s < shuffles; s += 1) {
        var x = Math.floor(random() * SIZE);
        var y = Math.floor(random() * SIZE);
        toggle(x, y); toggle(x - 1, y); toggle(x + 1, y); toggle(x, y - 1); toggle(x, y + 1);
      }
      taps = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      cursor = { x: 2, y: 2 };
      held = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        toggle(cursor.x, cursor.y);
        toggle(cursor.x - 1, cursor.y);
        toggle(cursor.x + 1, cursor.y);
        toggle(cursor.x, cursor.y - 1);
        toggle(cursor.x, cursor.y + 1);
        taps += 1;
        fx.shake(2);
        if (cells.every(function (c) { return c; })) {
          round += 1;
          game.score += 120 - Math.min(100, taps * 4);
          message = "QUORUM — ROUND " + round;
          messageAge = 0;
          fx.flash("verify", 1);
          deal();
        } else if (taps > 12 + round * 3) {
          game.lives -= 1;
          message = "NO CONSENSUS";
          messageAge = 0;
          fx.shake(8);
          if (game.lives <= 0) { game.over = true; return; }
          deal();
        }
      } else if (input.left) cursor.x = Math.max(0, cursor.x - 1);
      else if (input.right) cursor.x = Math.min(SIZE - 1, cursor.x + 1);
      else if (input.up) cursor.y = Math.max(0, cursor.y - 1);
      else if (input.down) cursor.y = Math.min(SIZE - 1, cursor.y + 1);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "GET EVERY NODE TO AGREE", 26, "dim");
      for (var i = 0; i < SIZE * SIZE; i += 1) {
        var x = OX + (i % SIZE) * CELL;
        var y = OY + Math.floor(i / SIZE) * CELL;
        ctx.fillStyle = cells[i] ? ink.verify : ink.wall[3];
        ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 8, y + 8, CELL - 17, CELL - 17);
      }
      var cx = OX + cursor.x * CELL, cy = OY + cursor.y * CELL;
      ctx.fillStyle = ink.bright;
      ctx.fillRect(cx, cy, CELL - 1, 2);
      ctx.fillRect(cx, cy + CELL - 3, CELL - 1, 2);
      ctx.fillRect(cx, cy, 2, CELL - 1);
      ctx.fillRect(cx + CELL - 3, cy, 2, CELL - 1);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUND " + round, 6, 16);
      ctx.fillText("TAPS " + taps, W - 62, 16);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · TAPS " + taps; };
    return game;
  }

  /* ---- 39. IDEMPOTENCY ---------------------------------------------------

     Memory pairs. Every request has one matching twin, and turning the same
     pair up twice is the definition of the property this cabinet is named
     after: doing it again changes nothing. */
  function cabinetIdempotency(random) {
    var COLS = 6;
    var ROWS = 4;
    var CELL = 40;
    var OX = (W - COLS * CELL) / 2;
    var OY = 52;
    var FACES = ["GET", "PUT", "DEL", "SUM", "SIG", "TTL", "ACK", "NAK", "REF", "IDX", "TXN", "SEQ"];

    var game = { id: "idempotency", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var cards, cursor, first, second, holdTimer, round, tries, held, message, messageAge;

    function deal() {
      var pool = [];
      for (var i = 0; i < (COLS * ROWS) / 2; i += 1) {
        pool.push(FACES[i % FACES.length], FACES[i % FACES.length]);
      }
      for (var s = pool.length - 1; s > 0; s -= 1) {
        var j = Math.floor(random() * (s + 1));
        var t = pool[s]; pool[s] = pool[j]; pool[j] = t;
      }
      cards = pool.map(function (face) { return { face: face, up: false, done: false }; });
      first = null;
      second = null;
      holdTimer = 0;
      tries = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      cursor = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;

      if (holdTimer > 0) {
        holdTimer -= dt;
        if (holdTimer <= 0) {
          if (first.face === second.face) {
            first.done = second.done = true;
            game.score += 30;
            fx.flash("verify", 0.7);
          } else {
            first.up = second.up = false;
          }
          first = second = null;
          if (cards.every(function (c) { return c.done; })) {
            round += 1;
            game.score += 150 - Math.min(120, tries * 5);
            message = "ALL PAIRS — ROUND " + round;
            messageAge = 0;
            deal();
          }
        }
        return;
      }

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var card = cards[cursor];
        if (card.done || card.up) return;
        card.up = true;
        if (!first) { first = card; return; }
        second = card;
        tries += 1;
        holdTimer = 0.55;
        if (tries > 14 + round * 4) {
          game.lives -= 1;
          message = "TOO MANY REPLAYS";
          messageAge = 0;
          fx.shake(7);
          if (game.lives <= 0) { game.over = true; return; }
          deal();
        }
      } else if (input.left) cursor = (cursor + cards.length - 1) % cards.length;
      else if (input.right) cursor = (cursor + 1) % cards.length;
      else if (input.up) cursor = (cursor + cards.length - COLS) % cards.length;
      else if (input.down) cursor = (cursor + COLS) % cards.length;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "TURN THE SAME REQUEST TWICE", 28, "dim");
      cards.forEach(function (card, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        if (card.done) {
          ctx.fillStyle = ink.grid;
          ctx.fillRect(x + 3, y + 3, CELL - 7, CELL - 7);
        } else if (card.up) {
          ctx.fillStyle = ink.brass;
          ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
          ctx.font = '8px "IBM Plex Mono", monospace';
          ctx.textAlign = "center";
          ctx.fillStyle = ink.bg;
          ctx.fillText(card.face, x + CELL / 2 - 1, y + CELL / 2);
          ctx.textAlign = "left";
        } else {
          ctx.fillStyle = ink.wall[2];
          ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
          ctx.fillStyle = ink.wall[3];
          ctx.fillRect(x + 8, y + 8, CELL - 17, CELL - 17);
        }
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x, y, CELL - 1, 2);
          ctx.fillRect(x, y + CELL - 3, CELL - 1, 2);
          ctx.fillRect(x, y, 2, CELL - 1);
          ctx.fillRect(x + CELL - 3, y, 2, CELL - 1);
        }
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUND " + round, 6, 16);
      ctx.fillText("REPLAYS " + tries, W - 84, 16);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · REPLAYS " + tries; };
    return game;
  }

  /* ---- 40. REPLAY ORDER --------------------------------------------------

     Watch a sequence, then reproduce it. An audit log is only useful if you
     can replay it in order, and one wrong step invalidates the whole thing —
     which is both how this genre works and how a hash chain works. */
  function cabinetReplayOrder(random) {
    var PADS = [
      { key: "left", label: "◀", colour: "danger" },
      { key: "up", label: "▲", colour: "verify" },
      { key: "down", label: "▼", colour: "brass" },
      { key: "right", label: "▶", colour: "bright" }
    ];

    var game = { id: "replay-order", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var order, step, phase, timer, lit, held, round, message, messageAge;

    function extend() {
      order.push(Math.floor(random() * PADS.length));
      phase = "show";
      step = 0;
      timer = 0.45;
      lit = -1;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      order = [];
      round = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      extend();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;

      if (phase === "show") {
        timer -= dt;
        if (timer > 0) return;
        if (lit >= 0) {
          lit = -1;
          timer = 0.12;
          step += 1;
          if (step >= order.length) {
            phase = "input";
            step = 0;
          }
          return;
        }
        lit = order[step];
        timer = Math.max(0.16, 0.44 - order.length * 0.012);
        return;
      }

      var pressedIndex = -1;
      for (var i = 0; i < PADS.length; i += 1) if (input[PADS[i].key]) pressedIndex = i;
      if (pressedIndex < 0) { held = false; lit = -1; return; }
      if (held) return;
      held = true;
      lit = pressedIndex;

      if (pressedIndex === order[step]) {
        step += 1;
        game.score += 4;
        if (step >= order.length) {
          round += 1;
          game.score += 25 + order.length * 5;
          message = "REPLAYED " + order.length + " IN ORDER";
          messageAge = 0;
          fx.flash("verify", 0.8);
          extend();
        }
      } else {
        game.lives -= 1;
        fx.shake(9);
        fx.flash("danger", 1);
        message = "OUT OF ORDER AT STEP " + (step + 1);
        messageAge = 0;
        if (game.lives <= 0) { game.over = true; return; }
        order = [];
        extend();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, phase === "show" ? "WATCH THE LOG" : "REPLAY IT", 24, phase === "show" ? "brass" : "verify");
      centreText(ctx, ink, "LENGTH " + order.length, 38, "dim");

      PADS.forEach(function (pad, i) {
        var x = 24 + i * 70;
        var on = lit === i;
        ctx.fillStyle = on ? ink[pad.colour] : ink.wall[3];
        ctx.fillRect(x, 70, 56, 90);
        ctx.fillStyle = on ? ink.bg : ink[pad.colour];
        ctx.font = '16px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillText(pad.label, x + 28, 115);
        ctx.textAlign = "left";
      });

      // Progress dots, so a long chain reads as progress rather than as luck.
      for (var d = 0; d < order.length; d += 1) {
        var dx = 10 + d * 7;
        if (dx > W - 10) break;
        ctx.fillStyle = phase === "input" && d < step ? ink.verify : ink.grid;
        ctx.fillRect(dx, 178, 5, 5);
      }

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUND " + round, 6, 16);
      if (messageAge < 2) centreText(ctx, ink, message, 204, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · LEN " + order.length; };
    return game;
  }

  /* ---- 41. MATCH POLICY --------------------------------------------------

     Swap adjacent rules to line three of a kind up. Cleared rules fall out of
     the stack and the ones above drop into their place, which is what
     happens to a policy file nobody is maintaining. */
  function cabinetMatchPolicy(random) {
    var COLS = 7;
    var ROWS = 8;
    var CELL = 26;
    var OX = (W - COLS * CELL) / 2;
    var OY = 30;
    var KINDS = 5;

    var game = { id: "match-policy", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var grid, cursor, held, chain, settle, moves, level;

    function at(x, y) { return grid[y * COLS + x]; }
    function set(x, y, v) { grid[y * COLS + x] = v; }

    function fill() {
      grid = [];
      for (var i = 0; i < COLS * ROWS; i += 1) grid.push(1 + Math.floor(random() * KINDS));
    }

    function findRuns() {
      var marks = {};
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS - 2; x += 1) {
          var v = at(x, y);
          if (v && v === at(x + 1, y) && v === at(x + 2, y)) {
            marks[y * COLS + x] = marks[y * COLS + x + 1] = marks[y * COLS + x + 2] = true;
          }
        }
      }
      for (var x2 = 0; x2 < COLS; x2 += 1) {
        for (var y2 = 0; y2 < ROWS - 2; y2 += 1) {
          var v2 = at(x2, y2);
          if (v2 && v2 === at(x2, y2 + 1) && v2 === at(x2, y2 + 2)) {
            marks[y2 * COLS + x2] = marks[(y2 + 1) * COLS + x2] = marks[(y2 + 2) * COLS + x2] = true;
          }
        }
      }
      return Object.keys(marks);
    }

    function collapse() {
      for (var x = 0; x < COLS; x += 1) {
        var write = ROWS - 1;
        for (var y = ROWS - 1; y >= 0; y -= 1) {
          if (at(x, y)) { set(x, write, at(x, y)); write -= 1; }
        }
        while (write >= 0) { set(x, write, 1 + Math.floor(random() * KINDS)); write -= 1; }
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cursor = { x: 3, y: 4 };
      held = false;
      chain = 0;
      settle = 0.01;
      moves = 30;
      level = 1;
      fx.reset();
      bits.clear();
      fill();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (settle > 0) {
        settle -= dt;
        if (settle > 0) return;
        var runs = findRuns();
        if (runs.length) {
          chain += 1;
          game.score += runs.length * 10 * chain;
          runs.forEach(function (key) {
            var idx = parseInt(key, 10);
            bits.burst(OX + (idx % COLS) * CELL + CELL / 2, OY + Math.floor(idx / COLS) * CELL + CELL / 2, 3,
              { colour: "bright", speed: 40, life: 0.35 });
            grid[idx] = 0;
          });
          fx.shake(2 + chain);
          collapse();
          settle = 0.16;
          return;
        }
        chain = 0;
      }

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        // Fire swaps with the tile to the right, wrapping to the row below at
        // the edge: one button, and every adjacency is still reachable.
        var tx = cursor.x < COLS - 1 ? cursor.x + 1 : cursor.x;
        var ty = cursor.x < COLS - 1 ? cursor.y : Math.min(ROWS - 1, cursor.y + 1);
        if (tx === cursor.x && ty === cursor.y) return;
        var a = at(cursor.x, cursor.y), b = at(tx, ty);
        set(cursor.x, cursor.y, b); set(tx, ty, a);
        moves -= 1;
        if (!findRuns().length) {
          set(cursor.x, cursor.y, a); set(tx, ty, b); // no match: swap back
          fx.shake(3);
        } else {
          settle = 0.08;
        }
        if (moves <= 0) {
          game.lives -= 1;
          moves = 30;
          if (game.lives <= 0) { game.over = true; return; }
          level += 1;
          fill();
          settle = 0.2;
        }
        return;
      }
      if (input.left) cursor.x = Math.max(0, cursor.x - 1);
      else if (input.right) cursor.x = Math.min(COLS - 1, cursor.x + 1);
      else if (input.up) cursor.y = Math.max(0, cursor.y - 1);
      else if (input.down) cursor.y = Math.min(ROWS - 1, cursor.y + 1);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      var palette = ["grid", "danger", "verify", "brass", "wall", "bright"];
      for (var i = 0; i < COLS * ROWS; i += 1) {
        var v = grid[i];
        if (!v) continue;
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        var key = palette[v];
        ctx.fillStyle = key === "wall" ? ink.wall[1] : ink[key];
        ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 6, y + 6, CELL - 13, CELL - 13);
      }
      var cx = OX + cursor.x * CELL, cy = OY + cursor.y * CELL;
      ctx.fillStyle = ink.ink;
      ctx.fillRect(cx, cy, CELL - 1, 2);
      ctx.fillRect(cx, cy + CELL - 3, CELL - 1, 2);
      ctx.fillRect(cx, cy, 2, CELL - 1);
      ctx.fillRect(cx + CELL - 3, cy, 2, CELL - 1);
      bits.draw(ctx, ink);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = moves < 8 ? ink.danger : ink.dim;
      ctx.fillText("SWAPS " + moves, 6, 18);
      if (chain > 1) centreText(ctx, ink, "CASCADE ×" + chain, 18, "verify");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "SWAPS " + moves + " · CHAIN " + chain; };
    return game;
  }

  /* ---- 42. COLD START ----------------------------------------------------

     Physics hill-climbing. Throttle and brake on a two-wheeled rig over
     procedural terrain, with fuel that only comes from checkpoints. Named for
     the thing every serverless deck promises is not a problem. */
  function cabinetColdStart(random) {
    var game = { id: "cold-start", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var car, ground, camX, fuel, distance, best, flip, checkpoint;

    function heightAt(x) {
      // Three summed sines: cheap, continuous, and deterministic in x, so the
      // hill under the wheels is the same hill on the way back.
      return H - 62 + Math.sin(x * 0.013) * 26 + Math.sin(x * 0.031 + 1.7) * 12 + Math.sin(x * 0.005) * 20;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      car = { x: 40, y: heightAt(40) - 10, vx: 0, vy: 0, angle: 0, spin: 0, onGround: true };
      camX = 0;
      fuel = 100;
      distance = 0;
      best = 0;
      checkpoint = 300;
      fx.reset();
      bits.clear();
    };

    function wreck(reason) {
      game.lives -= 1;
      fx.shake(11);
      fx.flash("danger", 1);
      bits.burst(car.x - camX, car.y, 18, { colour: "danger", speed: 90, life: 0.7, gravity: 180 });
      if (game.lives <= 0) { game.over = true; return; }
      car.x = Math.max(40, car.x - 120);
      car.y = heightAt(car.x) - 10;
      car.vx = car.vy = car.spin = 0;
      car.angle = 0;
      fuel = Math.max(35, fuel);
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      var throttle = input.right || input.up;
      var brake = input.left || input.down;
      if (throttle && fuel > 0) {
        car.vx += 96 * dt;
        fuel -= 7 * dt;
        if (car.onGround) bits.burst(car.x - camX - 8, car.y + 6, 1, { colour: "dim", speed: 30, life: 0.3 });
      }
      if (brake) car.vx -= 78 * dt;
      car.vx *= 0.995;
      car.vx = clamp(car.vx, -70, 150);

      car.vy += 420 * dt;
      car.x += car.vx * dt;
      car.y += car.vy * dt;

      var floor = heightAt(car.x) - 10;
      if (car.y >= floor) {
        if (!car.onGround && Math.abs(car.spin) > 2.6) {
          // Landing mid-rotation is a wreck; landing flat after a full turn
          // is worth something. The genre in one rule.
          wreck("bad landing");
          return;
        }
        if (!car.onGround && Math.abs(car.spin) > 0.4) {
          game.score += 40;
          fx.pop(car.x - camX, car.y - 20, "FLIP +40", "verify");
        }
        car.y = floor;
        car.vy = 0;
        car.onGround = true;
        car.spin = 0;
        // Slope drags the nose: the angle is the terrain derivative.
        car.angle = (heightAt(car.x + 6) - heightAt(car.x - 6)) / 12;
      } else {
        car.onGround = false;
        if (throttle) car.spin += 2.4 * dt;
        if (brake) car.spin -= 2.4 * dt;
        car.angle += car.spin * dt;
      }

      if (car.x > distance) {
        game.score += Math.floor((car.x - distance) * 0.2);
        distance = car.x;
        if (distance > best) best = distance;
      }
      if (distance > checkpoint) {
        checkpoint += 300;
        fuel = Math.min(100, fuel + 45);
        game.score += 60;
        fx.flash("verify", 0.8);
      }
      if (fuel <= 0 && Math.abs(car.vx) < 4) { wreck("out of fuel"); return; }
      camX = car.x - 90;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, camX, H * 0.5);
      fx.begin(ctx);

      ctx.fillStyle = ink.wall[2];
      for (var x = 0; x < W; x += 2) {
        var h = heightAt(x + camX);
        ctx.fillRect(x, h, 2, H - h);
        ctx.fillStyle = ink.verify;
        ctx.fillRect(x, h, 2, 2);
        ctx.fillStyle = ink.wall[2];
      }

      var sx = Math.round(car.x - camX), sy = Math.round(car.y);
      var lean = Math.round(car.angle * 6);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(sx - 10, sy - 6 - lean, 20, 8);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(sx - 10, sy + 2 - lean, 6, 6);
      ctx.fillRect(sx + 4, sy + 2 + lean, 6, 6);
      bits.draw(ctx, ink);

      drawBar(ctx, ink, 4, 4, 70, 5, fuel / 100, fuel > 30 ? "verify" : "danger");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("FUEL", 4, 18);
      ctx.fillText(Math.floor(distance / 10) + "m", W - 54, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(distance / 10) + "m · FUEL " + Math.max(0, Math.round(fuel)); };
    return game;
  }

  /* ---- 43. SWISH RATE ----------------------------------------------------

     Arcade hoops. A power meter, a moving hoop, and a shot clock. Nothing but
     release timing, which is the only sports mechanic that survives being
     reduced to one button. */
  function cabinetSwishRate(random) {
    var game = { id: "swish-rate", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var power, rising, ball, hoop, clock, streak, held, made, attempts;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      power = 0;
      rising = true;
      ball = null;
      hoop = { x: W - 60, y: 60, dir: 1, speed: 26 };
      clock = 30;
      streak = 0;
      made = 0;
      attempts = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      clock -= dt;

      hoop.x += hoop.dir * hoop.speed * dt;
      if (hoop.x < W / 2 || hoop.x > W - 24) hoop.dir *= -1;
      hoop.speed = 26 + made * 2.4;

      if (!ball) {
        if (rising) { power += dt * 1.35; if (power >= 1) { power = 1; rising = false; } }
        else { power -= dt * 1.35; if (power <= 0) { power = 0; rising = true; } }
        if (input.fire && !held) {
          held = true;
          attempts += 1;
          ball = { x: 34, y: H - 40, vx: 60 + power * 150, vy: -(90 + power * 150), scored: false };
        }
      }
      if (!input.fire) held = false;

      if (ball) {
        ball.vy += 300 * dt;
        ball.x += ball.vx * dt;
        ball.y += ball.vy * dt;
        if (!ball.scored && Math.abs(ball.x - hoop.x) < 11 && Math.abs(ball.y - hoop.y) < 6 && ball.vy > 0) {
          ball.scored = true;
          made += 1;
          streak += 1;
          game.score += 20 * Math.min(5, streak);
          clock = Math.min(40, clock + 4);
          fx.flash("verify", 0.9);
          fx.pop(hoop.x, hoop.y - 14, streak > 1 ? "×" + streak : "SWISH", "verify");
          bits.burst(hoop.x, hoop.y, 12, { colour: "verify", speed: 70, life: 0.5, gravity: 90 });
        }
        if (ball.y > H + 10 || ball.x > W + 10) {
          if (!ball.scored) { streak = 0; fx.shake(3); }
          ball = null;
        }
      }

      if (clock <= 0) {
        game.lives -= 1;
        clock = 30;
        streak = 0;
        fx.flash("danger", 1);
        if (game.lives <= 0) game.over = true;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 20, W, 20);

      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(hoop.x + 12, hoop.y - 20, 4, 26);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(hoop.x - 12, hoop.y, 24, 3);
      ctx.fillStyle = ink.grid;
      for (var n = 0; n < 5; n += 1) ctx.fillRect(hoop.x - 10 + n * 5, hoop.y + 3, 2, 7);

      ctx.fillStyle = ink.verify;
      ctx.fillRect(28, H - 34, 12, 14);
      if (ball) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(ball.x) - 3, Math.round(ball.y) - 3, 7, 7);
      }
      bits.draw(ctx, ink);

      drawBar(ctx, ink, 8, H - 14, W - 16, 8, power, power > 0.75 ? "danger" : "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = clock < 8 ? ink.danger : ink.dim;
      ctx.fillText("CLOCK " + Math.ceil(clock), 4, 12);
      ctx.fillStyle = ink.dim;
      ctx.fillText("MADE " + made + "/" + attempts, W - 88, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "MADE " + made + " · STREAK " + streak; };
    return game;
  }

  /* ---- 44. SIEGE BUDGET --------------------------------------------------

     Lob a payload at a stack of legacy. Angle, power, one shot at a time, and
     a budget that only refills when something falls over — the physics-siege
     genre, and an honest picture of a migration. */
  function cabinetSiegeBudget(random) {
    var game = { id: "siege-budget", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(70);
    var angle, power, charging, shot, blocks, shots, wave, held;

    function build() {
      blocks = [];
      var cols = 2 + Math.min(3, Math.floor(wave / 2));
      for (var c = 0; c < cols; c += 1) {
        var height = 2 + Math.floor(random() * 3);
        for (var r = 0; r < height; r += 1) {
          blocks.push({
            x: 200 + c * 26,
            y: H - 30 - r * 16,
            vy: 0,
            hp: 1 + Math.floor(wave / 3),
            fallen: false
          });
        }
      }
      shots = 3 + Math.floor(wave / 2);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      angle = 0.7;
      power = 0.5;
      charging = false;
      shot = null;
      wave = 1;
      held = false;
      fx.reset();
      bits.clear();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (!shot) {
        if (input.up) angle = Math.min(1.35, angle + dt);
        if (input.down) angle = Math.max(0.12, angle - dt);
        if (input.fire) {
          charging = true;
          power = Math.min(1, power + dt * 0.8);
        } else if (charging) {
          charging = false;
          shots -= 1;
          shot = {
            x: 22, y: H - 34,
            vx: Math.cos(angle) * (90 + power * 160),
            vy: -Math.sin(angle) * (90 + power * 160)
          };
          power = 0.2;
        }
      }

      if (shot) {
        shot.vy += 260 * dt;
        shot.x += shot.vx * dt;
        shot.y += shot.vy * dt;
        for (var i = blocks.length - 1; i >= 0; i -= 1) {
          var b = blocks[i];
          if (b.fallen) continue;
          if (Math.abs(b.x - shot.x) > 12 || Math.abs(b.y - shot.y) > 10) continue;
          b.hp -= 1;
          shot.vx *= 0.45;
          shot.vy = -Math.abs(shot.vy) * 0.4;
          fx.shake(6);
          bits.burst(b.x, b.y, 8, { colour: "brass", speed: 60, life: 0.5, gravity: 200 });
          if (b.hp <= 0) {
            b.fallen = true;
            game.score += 30;
            shots += 1;
          }
          break;
        }
        if (shot.y > H || shot.x > W + 20) shot = null;
      }

      // Unsupported blocks drop, which is what makes a good hit worth more
      // than a lucky one.
      blocks.forEach(function (b) {
        if (b.fallen) return;
        var supported = b.y > H - 40 || blocks.some(function (o) {
          return !o.fallen && o !== b && Math.abs(o.x - b.x) < 8 && o.y - b.y > 8 && o.y - b.y < 24;
        });
        if (!supported) {
          b.vy += 300 * dt;
          b.y += b.vy * dt;
          if (b.y > H - 30) { b.y = H - 30; b.vy = 0; }
        }
      });

      var standing = blocks.filter(function (b) { return !b.fallen; }).length;
      if (!standing) {
        wave += 1;
        game.score += 150;
        fx.flash("verify", 1);
        build();
      } else if (shots <= 0 && !shot) {
        game.lives -= 1;
        fx.shake(8);
        if (game.lives <= 0) { game.over = true; return; }
        build();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, 0, H * 0.6);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 22, W, 22);

      blocks.forEach(function (b) {
        if (b.fallen) return;
        ctx.fillStyle = b.hp > 1 ? ink.wall[1] : ink.brass;
        ctx.fillRect(b.x - 11, b.y - 7, 22, 15);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(b.x - 8, b.y - 4, 16, 9);
      });

      ctx.fillStyle = ink.verify;
      ctx.fillRect(14, H - 40, 16, 18);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(
        Math.round(22 + Math.cos(angle) * 18) - 2,
        Math.round(H - 34 - Math.sin(angle) * 18) - 2, 5, 5
      );
      if (shot) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(shot.x) - 3, Math.round(shot.y) - 3, 6, 6);
      }
      bits.draw(ctx, ink);

      drawBar(ctx, ink, 8, 8, 70, 5, power, "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SHOTS " + shots, 8, 24);
      ctx.fillText("WAVE " + wave, W - 60, 14);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · SHOTS " + shots; };
    return game;
  }

  /* ---- 45. TILT ----------------------------------------------------------

     A pinball table with two flippers and one ball. Bumpers are services that
     pay out; the drain is what happens to a request nobody catches. */
  function cabinetTilt(random) {
    var game = { id: "tilt", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var ball, flipL, flipR, bumpers, multiplier, combo;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      ball = { x: W / 2, y: 40, vx: 26, vy: 60 };
      flipL = 0;
      flipR = 0;
      multiplier = 1;
      combo = 0;
      bumpers = [
        { x: 80, y: 70, r: 12, hit: 0 },
        { x: 160, y: 50, r: 14, hit: 0 },
        { x: 240, y: 70, r: 12, hit: 0 },
        { x: 120, y: 120, r: 10, hit: 0 },
        { x: 200, y: 120, r: 10, hit: 0 }
      ];
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      flipL = input.left || input.fire ? Math.min(1, flipL + dt * 9) : Math.max(0, flipL - dt * 7);
      flipR = input.right || input.fire ? Math.min(1, flipR + dt * 9) : Math.max(0, flipR - dt * 7);

      ball.vy += 190 * dt;
      ball.x += ball.vx * dt;
      ball.y += ball.vy * dt;

      if (ball.x < 8) { ball.x = 8; ball.vx = Math.abs(ball.vx) * 0.9; }
      if (ball.x > W - 8) { ball.x = W - 8; ball.vx = -Math.abs(ball.vx) * 0.9; }
      if (ball.y < 8) { ball.y = 8; ball.vy = Math.abs(ball.vy) * 0.9; }

      bumpers.forEach(function (b) {
        b.hit = Math.max(0, b.hit - dt * 4);
        var dx = ball.x - b.x, dy = ball.y - b.y;
        var d = Math.hypot(dx, dy);
        if (d > b.r + 4) return;
        var nx = dx / (d || 1), ny = dy / (d || 1);
        ball.x = b.x + nx * (b.r + 5);
        ball.y = b.y + ny * (b.r + 5);
        var speed = Math.max(110, Math.hypot(ball.vx, ball.vy) * 1.05);
        ball.vx = nx * speed;
        ball.vy = ny * speed;
        b.hit = 1;
        combo += 1;
        multiplier = 1 + Math.floor(combo / 5);
        game.score += 10 * multiplier;
        fx.shake(3);
        bits.burst(b.x, b.y, 5, { colour: "brass", speed: 60, life: 0.35 });
      });

      // Flippers: two slabs at the bottom corners that kick the ball back up
      // when raised. Not a rigid-body sim — a kick impulse in the right
      // direction is indistinguishable at this resolution and cannot wedge.
      var flipY = H - 24;
      if (ball.y > flipY - 6 && ball.y < flipY + 10) {
        if (ball.x < W / 2 && flipL > 0.35) {
          ball.vy = -Math.abs(ball.vy) - 60;
          ball.vx = Math.abs(ball.vx) + 30;
          fx.shake(2);
          combo = 0;
        } else if (ball.x >= W / 2 && flipR > 0.35) {
          ball.vy = -Math.abs(ball.vy) - 60;
          ball.vx = -Math.abs(ball.vx) - 30;
          fx.shake(2);
          combo = 0;
        }
      }

      if (ball.y > H + 6) {
        game.lives -= 1;
        combo = 0;
        multiplier = 1;
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        ball = { x: W / 2, y: 40, vx: (random() - 0.5) * 60, vy: 60 };
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 0, 6, H);
      ctx.fillRect(W - 6, 0, 6, H);

      bumpers.forEach(function (b) {
        ctx.fillStyle = b.hit > 0 ? ink.bright : ink.wall[1];
        ctx.fillRect(b.x - b.r, b.y - b.r, b.r * 2, b.r * 2);
        ctx.fillStyle = b.hit > 0 ? ink.bg : ink.brass;
        ctx.fillRect(b.x - b.r + 4, b.y - b.r + 4, b.r * 2 - 8, b.r * 2 - 8);
      });

      var fy = H - 24;
      ctx.fillStyle = flipL > 0.35 ? ink.verify : ink.wall[2];
      ctx.fillRect(24, fy - Math.round(flipL * 6), 54, 6);
      ctx.fillStyle = flipR > 0.35 ? ink.verify : ink.wall[2];
      ctx.fillRect(W - 78, fy - Math.round(flipR * 6), 54, 6);

      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(ball.x) - 4, Math.round(ball.y) - 4, 8, 8);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("×" + multiplier, 10, 14);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "MULT ×" + multiplier + " · COMBO " + combo; };
    return game;
  }

  /* ---- 46. SOFT LANDING --------------------------------------------------

     Set an agent down on the pad with the fuel you were given. Thrust, drift,
     and a descent rate that has to be under the limit at the moment of
     contact — the oldest simulation there is, and still the best argument for
     reading the gauge instead of the ground. */
  function cabinetSoftLanding(random) {
    var game = { id: "soft-landing", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var pod, pad, fuel, level, terrain, message, messageAge;

    function build() {
      terrain = [];
      var y = H - 30;
      for (var x = 0; x <= W; x += 8) {
        y = clamp(y + (random() - 0.5) * 18, H - 70, H - 14);
        terrain.push(y);
      }
      var padIndex = 3 + Math.floor(random() * (terrain.length - 8));
      var padY = terrain[padIndex];
      for (var i = padIndex; i < padIndex + 4; i += 1) terrain[i] = padY;
      pad = { x: padIndex * 8, w: 32, y: padY };
      pod = { x: 30 + random() * (W - 60), y: 20, vx: (random() - 0.5) * 20, vy: 6 };
      fuel = 100;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      messageAge = 9;
      fx.reset();
      bits.clear();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (fuel > 0) {
        if (input.up || input.fire) { pod.vy -= 52 * dt; fuel -= 22 * dt; bits.burst(pod.x, pod.y + 7, 2, { colour: "brass", speed: 40, angle: Math.PI / 2, spread: 0.7, life: 0.3 }); }
        if (input.left) { pod.vx -= 24 * dt; fuel -= 8 * dt; }
        if (input.right) { pod.vx += 24 * dt; fuel -= 8 * dt; }
      }
      pod.vy += 22 * dt;
      pod.x += pod.vx * dt;
      pod.y += pod.vy * dt;
      pod.x = clamp(pod.x, 4, W - 4);

      var groundY = terrain[Math.min(terrain.length - 1, Math.max(0, Math.floor(pod.x / 8)))];
      if (pod.y >= groundY - 6) {
        var onPad = pod.x > pad.x && pod.x < pad.x + pad.w;
        var gentle = pod.vy < 26 && Math.abs(pod.vx) < 14;
        if (onPad && gentle) {
          level += 1;
          game.score += 150 + Math.floor(fuel);
          message = "TOUCHDOWN — " + Math.floor(fuel) + " FUEL LEFT";
          messageAge = 0;
          fx.flash("verify", 1);
          build();
        } else {
          game.lives -= 1;
          message = onPad ? "TOO FAST" : "MISSED THE PAD";
          messageAge = 0;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(pod.x, pod.y, 20, { colour: "danger", speed: 90, life: 0.7, gravity: 60 });
          if (game.lives <= 0) { game.over = true; return; }
          build();
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 34);
      fx.begin(ctx);

      ctx.fillStyle = ink.wall[2];
      terrain.forEach(function (y, i) { ctx.fillRect(i * 8, y, 8, H - y); });
      ctx.fillStyle = ink.verify;
      ctx.fillRect(pad.x, pad.y - 2, pad.w, 3);

      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(pod.x) - 5, Math.round(pod.y) - 6, 10, 10);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(Math.round(pod.x) - 6, Math.round(pod.y) + 4, 3, 4);
      ctx.fillRect(Math.round(pod.x) + 3, Math.round(pod.y) + 4, 3, 4);

      ctx.font = '8px "IBM Plex Mono", monospace';
      var safe = pod.vy < 26 && Math.abs(pod.vx) < 14;
      ctx.fillStyle = safe ? ink.verify : ink.danger;
      ctx.fillText("VSPD " + pod.vy.toFixed(0), 4, 12);
      ctx.fillText("HSPD " + Math.abs(pod.vx).toFixed(0), 4, 22);
      drawBar(ctx, ink, W - 74, 6, 70, 5, fuel / 100, fuel > 25 ? "brass" : "danger");
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "PAD " + level + " · FUEL " + Math.max(0, Math.round(fuel)); };
    return game;
  }

  /* ---- 47. TICKET QUEUE --------------------------------------------------

     Service management. Tickets arrive at the counter with a patience bar;
     you walk to one, pick it up, walk to the matching desk, and drop it. The
     genre is about routing under load, which is the same problem the product
     solves with less running. */
  function cabinetTicketQueue(random) {
    var DESKS = [
      { x: 30, label: "AUTH", colour: "verify" },
      { x: 120, label: "METER", colour: "brass" },
      { x: 210, label: "AUDIT", colour: "danger" },
      { x: 285, label: "SIGN", colour: "bright" }
    ];

    var game = { id: "ticket-queue", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var clerk, tickets, carrying, spawnTimer, shift, served, dropped, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      clerk = { x: W / 2 };
      tickets = [];
      carrying = null;
      spawnTimer = 1;
      shift = 1;
      served = 0;
      dropped = 0;
      held = false;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);

      var speed = 96 * dt;
      if (input.left) clerk.x -= speed;
      if (input.right) clerk.x += speed;
      clerk.x = clamp(clerk.x, 8, W - 8);

      spawnTimer -= dt;
      if (spawnTimer <= 0 && tickets.length < 6) {
        spawnTimer = Math.max(0.7, 2.4 - shift * 0.16);
        tickets.push({
          x: 20 + random() * (W - 40),
          desk: Math.floor(random() * DESKS.length),
          patience: 1,
          rate: 0.05 + shift * 0.008
        });
      }

      for (var i = tickets.length - 1; i >= 0; i -= 1) {
        var t = tickets[i];
        if (t === carrying) continue;
        t.patience -= t.rate * dt;
        if (t.patience <= 0) {
          tickets.splice(i, 1);
          dropped += 1;
          game.lives -= 1;
          fx.shake(8);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        }
      }

      if (input.fire && !held) {
        held = true;
        if (carrying) {
          var desk = DESKS[carrying.desk];
          if (Math.abs(clerk.x - desk.x) < 20) {
            tickets.splice(tickets.indexOf(carrying), 1);
            served += 1;
            game.score += 25 + Math.round(carrying.patience * 30);
            fx.pop(desk.x, H - 60, "+" + (25 + Math.round(carrying.patience * 30)), "verify");
            fx.flash("verify", 0.6);
            carrying = null;
            if (served % 8 === 0) shift += 1;
          } else {
            carrying.x = clerk.x;
            carrying = null;
          }
        } else {
          for (var k = 0; k < tickets.length; k += 1) {
            if (Math.abs(tickets[k].x - clerk.x) > 14) continue;
            carrying = tickets[k];
            break;
          }
        }
      } else if (!input.fire) held = false;

      if (carrying) carrying.x = clerk.x;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 40, W, 40);

      DESKS.forEach(function (d) {
        ctx.fillStyle = ink[d.colour];
        ctx.fillRect(d.x - 18, H - 56, 36, 16);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bg;
        ctx.fillText(d.label, d.x, H - 47);
        ctx.textAlign = "left";
      });

      tickets.forEach(function (t) {
        var y = t === carrying ? H - 78 : 60;
        ctx.fillStyle = ink[DESKS[t.desk].colour];
        ctx.fillRect(Math.round(t.x) - 7, y, 14, 16);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(t.x) - 4, y + 3, 8, 2);
        drawBar(ctx, ink, Math.round(t.x) - 8, y - 5, 16, 3, t.patience, t.patience > 0.35 ? "verify" : "danger");
      });

      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(clerk.x) - 5, H - 76, 10, 20);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SHIFT " + shift, 4, 12);
      ctx.fillText("SERVED " + served, 4, 22);
      ctx.fillStyle = ink.danger;
      ctx.fillText("ABANDONED " + dropped, W - 106, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "SHIFT " + shift + " · SERVED " + served; };
    return game;
  }

  /* ---- 48. ROUTE TABLE ---------------------------------------------------

     Traffic control as a routing problem. Requests enter from the edges and
     you flip junction switches to keep two of them off the same square. Every
     collision is two calls that met where the table said they would not. */
  function cabinetRouteTable(random) {
    var CELL = 24;
    var COLS = 13;
    var ROWS = 8;
    var OX = (W - COLS * CELL) / 2;
    var OY = 36;

    var game = { id: "route-table", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var junctions, cars, cursor, spawnTimer, level, delivered, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      junctions = [];
      for (var y = 1; y < ROWS - 1; y += 2) {
        for (var x = 2; x < COLS - 2; x += 3) junctions.push({ x: x, y: y, open: random() < 0.5 });
      }
      cars = [];
      cursor = 0;
      spawnTimer = 0.8;
      level = 1;
      delivered = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.fire) {
          junctions[cursor].open = !junctions[cursor].open;
          fx.shake(2);
        } else if (input.left) cursor = (cursor + junctions.length - 1) % junctions.length;
        else if (input.right) cursor = (cursor + 1) % junctions.length;
        else if (input.up || input.down) cursor = (cursor + 3) % junctions.length;
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.5, 1.8 - level * 0.09);
        var lane = 1 + Math.floor(random() * (ROWS - 2));
        cars.push({ x: -1, y: lane, speed: 1.4 + level * 0.1, dy: 0 });
      }

      for (var i = cars.length - 1; i >= 0; i -= 1) {
        var c = cars[i];
        c.x += c.speed * dt;
        // A junction diverts a request one row down when open, which is the
        // whole control surface: you are choosing which lane a call lands in.
        junctions.forEach(function (j) {
          if (!j.open) return;
          if (Math.abs(c.x - j.x) < 0.08 && c.y === j.y) c.y = Math.min(ROWS - 1, c.y + 1);
        });
        if (c.x > COLS) {
          cars.splice(i, 1);
          delivered += 1;
          game.score += 12;
          if (delivered % 10 === 0) level += 1;
        }
      }

      for (var a = 0; a < cars.length; a += 1) {
        for (var b = a + 1; b < cars.length; b += 1) {
          if (cars[a].y !== cars[b].y) continue;
          if (Math.abs(cars[a].x - cars[b].x) > 0.7) continue;
          bits.burst(OX + cars[a].x * CELL, OY + cars[a].y * CELL, 12, { colour: "danger", speed: 70, life: 0.5 });
          cars.splice(b, 1);
          cars.splice(a, 1);
          game.lives -= 1;
          fx.shake(10);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          return;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var y = 0; y < ROWS; y += 1) {
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(OX, OY + y * CELL - 3, COLS * CELL, 6);
      }
      junctions.forEach(function (j, i) {
        ctx.fillStyle = j.open ? ink.verify : ink.wall[1];
        ctx.fillRect(OX + j.x * CELL - 5, OY + j.y * CELL - 8, 10, 16);
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(OX + j.x * CELL - 8, OY + j.y * CELL - 12, 16, 2);
          ctx.fillRect(OX + j.x * CELL - 8, OY + j.y * CELL + 10, 16, 2);
        }
      });
      cars.forEach(function (c) {
        ctx.fillStyle = ink.brass;
        ctx.fillRect(Math.round(OX + c.x * CELL) - 6, OY + c.y * CELL - 4, 12, 8);
      });
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("DELIVERED " + delivered, 4, 14);
      ctx.fillText("LEVEL " + level, W - 66, 14);
      centreText(ctx, ink, "← → pick a junction · SPACE divert", H - 8, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + delivered + " ROUTED"; };
    return game;
  }

  /* ---- 49. LIFT SLA ------------------------------------------------------

     An elevator with a service level to meet. Callers appear on floors with a
     clock; you carry them to the floor they asked for. Everything about this
     is queueing theory wearing a friendlier hat. */
  function cabinetLiftSla(random) {
    var FLOORS = 6;
    var FLOOR_H = 30;
    var OY = 26;

    var game = { id: "lift-sla", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var car, riders, waiting, spawnTimer, delivered, breach, held, level;

    function floorY(f) { return OY + (FLOORS - 1 - f) * FLOOR_H; }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      car = { floor: 0, y: floorY(0), target: 0 };
      riders = [];
      waiting = [];
      spawnTimer = 1;
      delivered = 0;
      breach = 0;
      level = 1;
      held = false;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);

      var pressed = input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.up) car.target = Math.min(FLOORS - 1, car.target + 1);
        else if (input.down) car.target = Math.max(0, car.target - 1);
      }

      var want = floorY(car.target);
      var diff = want - car.y;
      car.y += clamp(diff, -70 * dt, 70 * dt);
      var atFloor = Math.abs(diff) < 1.5;
      if (atFloor) car.floor = car.target;

      spawnTimer -= dt;
      if (spawnTimer <= 0 && waiting.length < 6) {
        spawnTimer = Math.max(0.9, 2.6 - level * 0.15);
        var from = Math.floor(random() * FLOORS);
        var to = Math.floor(random() * FLOORS);
        if (to === from) to = (to + 1) % FLOORS;
        waiting.push({ from: from, to: to, wait: 1 });
      }

      for (var i = waiting.length - 1; i >= 0; i -= 1) {
        var w = waiting[i];
        w.wait -= (0.045 + level * 0.004) * dt * 10 * 0.1;
        if (w.wait <= 0) {
          waiting.splice(i, 1);
          breach += 1;
          game.lives -= 1;
          fx.shake(9);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          continue;
        }
        if (atFloor && w.from === car.floor && riders.length < 3) {
          waiting.splice(i, 1);
          riders.push(w);
          fx.flash("brass", 0.4);
        }
      }

      if (atFloor) {
        for (var r = riders.length - 1; r >= 0; r -= 1) {
          if (riders[r].to !== car.floor) continue;
          var rider = riders.splice(r, 1)[0];
          delivered += 1;
          game.score += 20 + Math.round(rider.wait * 25);
          fx.pop(W / 2, car.y - 12, "+" + (20 + Math.round(rider.wait * 25)), "verify");
          if (delivered % 8 === 0) level += 1;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var f = 0; f < FLOORS; f += 1) {
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(20, floorY(f) + 20, W - 40, 3);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.dim;
        ctx.fillText("F" + f, 6, floorY(f) + 12);
      }
      waiting.forEach(function (w, i) {
        var y = floorY(w.from);
        var x = 120 + (i % 4) * 22;
        ctx.fillStyle = w.wait > 0.35 ? ink.brass : ink.danger;
        ctx.fillRect(x, y + 6, 8, 14);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.dim;
        ctx.fillText("→" + w.to, x + 9, y + 14);
      });
      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(60, Math.round(car.y), 46, 24);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(62, Math.round(car.y) + 2, 42, 20);
      riders.forEach(function (r, i) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(66 + i * 13, Math.round(car.y) + 6, 8, 14);
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("DELIVERED " + delivered, W - 116, 12);
      ctx.fillStyle = ink.danger;
      ctx.fillText("BREACHED " + breach, W - 116, 22);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "F" + car.floor + " · " + riders.length + " ABOARD"; };
    return game;
  }

  /* ---- 50. ON CALL -------------------------------------------------------

     Incident response. Alerts light up across a board of services; you walk to
     one and hold to work it. Two alerts on the same service escalate, and an
     escalation is the one thing you cannot fix by being fast. */
  function cabinetOnCall(random) {
    var COLS = 5;
    var ROWS = 3;
    var CELL = 56;
    var OX = (W - COLS * CELL) / 2;
    var OY = 50;

    var game = { id: "on-call", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var services, cursor, spawnTimer, night, resolved, held, working;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      services = [];
      for (var i = 0; i < COLS * ROWS; i += 1) services.push({ alert: 0, timer: 0 });
      cursor = 7;
      spawnTimer = 1.2;
      night = 1;
      resolved = 0;
      working = 0;
      held = false;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);

      var pressed = input.left || input.right || input.up || input.down;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.left) cursor = (cursor + services.length - 1) % services.length;
        else if (input.right) cursor = (cursor + 1) % services.length;
        else if (input.up) cursor = (cursor + services.length - COLS) % services.length;
        else if (input.down) cursor = (cursor + COLS) % services.length;
        working = 0;
      }

      // Holding is the verb: an incident takes time, and the time is the
      // resource the genre is actually about.
      if (input.fire && services[cursor].alert) {
        working += dt;
        if (working >= 1.1) {
          working = 0;
          services[cursor].alert = 0;
          services[cursor].timer = 0;
          resolved += 1;
          game.score += 40;
          fx.flash("verify", 0.7);
          fx.pop(OX + (cursor % COLS) * CELL + CELL / 2, OY + Math.floor(cursor / COLS) * CELL, "RESOLVED", "verify");
          if (resolved % 6 === 0) night += 1;
        }
      } else {
        working = Math.max(0, working - dt * 2);
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.8, 3.2 - night * 0.22);
        var free = [];
        services.forEach(function (s, i) { if (!s.alert) free.push(i); });
        if (free.length) services[free[Math.floor(random() * free.length)]].alert = 1;
      }

      for (var i = 0; i < services.length; i += 1) {
        var s = services[i];
        if (!s.alert) continue;
        s.timer += dt;
        if (s.timer > 9 - Math.min(5, night * 0.4)) {
          s.timer = 0;
          s.alert += 1;
          if (s.alert > 2) {
            s.alert = 0;
            game.lives -= 1;
            fx.shake(11);
            fx.flash("danger", 1);
            if (game.lives <= 0) { game.over = true; return; }
          }
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "NIGHT " + night + " — HOLD TO WORK AN ALERT", 24, "dim");
      services.forEach(function (s, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        ctx.fillStyle = s.alert > 1 ? ink.danger : s.alert ? ink.brass : ink.wall[3];
        ctx.fillRect(x + 3, y + 3, CELL - 8, CELL - 8);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 10, y + 10, CELL - 22, CELL - 22);
        if (s.alert) {
          ctx.fillStyle = s.alert > 1 ? ink.bright : ink.brass;
          ctx.fillRect(x + 18, y + 18, CELL - 38, CELL - 38);
        }
        if (i === cursor) {
          ctx.fillStyle = ink.ink;
          ctx.fillRect(x, y, CELL - 2, 2);
          ctx.fillRect(x, y + CELL - 4, CELL - 2, 2);
          ctx.fillRect(x, y, 2, CELL - 2);
          ctx.fillRect(x + CELL - 4, y, 2, CELL - 2);
        }
      });
      if (working > 0) drawBar(ctx, ink, W / 2 - 40, H - 26, 80, 6, working / 1.1, "verify");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("RESOLVED " + resolved, 4, 14);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "NIGHT " + night + " · RESOLVED " + resolved; };
    return game;
  }

  /* ---- 51. HARVEST WINDOW ------------------------------------------------

     Plant, wait, collect — and the window in which a crop is worth anything
     is short. A farming loop is a retention loop with soil on it, and the
     product-shaped version is batch scheduling. */
  function cabinetHarvestWindow(random) {
    var COLS = 6;
    var ROWS = 4;
    var CELL = 40;
    var OX = (W - COLS * CELL) / 2;
    var OY = 48;

    var game = { id: "harvest-window", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var plots, cursor, season, quota, banked, clock, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      plots = [];
      for (var i = 0; i < COLS * ROWS; i += 1) plots.push({ age: -1 });
      cursor = 0;
      season = 1;
      quota = 6;
      banked = 0;
      clock = 40;
      held = false;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      clock -= dt;

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.fire) {
          var plot = plots[cursor];
          if (plot.age < 0) {
            plot.age = 0;
          } else if (plot.age > 3.2 && plot.age < 6.4) {
            // The window: too early is a waste, too late is spoiled.
            plot.age = -1;
            banked += 1;
            game.score += 30;
            fx.pop(OX + (cursor % COLS) * CELL + CELL / 2, OY + Math.floor(cursor / COLS) * CELL, "+30", "verify");
          } else if (plot.age >= 6.4) {
            plot.age = -1;
            game.score += 2;
            fx.shake(3);
          }
        } else if (input.left) cursor = (cursor + plots.length - 1) % plots.length;
        else if (input.right) cursor = (cursor + 1) % plots.length;
        else if (input.up) cursor = (cursor + plots.length - COLS) % plots.length;
        else if (input.down) cursor = (cursor + COLS) % plots.length;
      }

      plots.forEach(function (p) { if (p.age >= 0) p.age += dt; });

      if (clock <= 0) {
        if (banked >= quota) {
          season += 1;
          quota = Math.floor(quota * 1.6);
          banked = 0;
          clock = 40;
          fx.flash("verify", 1);
        } else {
          game.lives -= 1;
          banked = 0;
          clock = 40;
          fx.shake(10);
          fx.flash("danger", 1);
          if (game.lives <= 0) game.over = true;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "SEASON " + season + " — BANK " + quota + " IN THE WINDOW", 22, "dim");
      plots.forEach(function (p, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
        if (p.age >= 0) {
          var ripe = p.age > 3.2 && p.age < 6.4;
          var spoiled = p.age >= 6.4;
          var size = Math.min(CELL - 14, 6 + p.age * 4);
          ctx.fillStyle = spoiled ? ink.danger : ripe ? ink.verify : ink.brass;
          ctx.fillRect(x + CELL / 2 - size / 2, y + CELL / 2 - size / 2, size, size);
        }
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x, y, CELL - 1, 2);
          ctx.fillRect(x, y + CELL - 3, CELL - 1, 2);
        }
      });
      drawBar(ctx, ink, 8, H - 22, W - 16, 6, clock / 40, clock < 10 ? "danger" : "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BANKED " + banked + "/" + quota, 8, H - 28);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "SEASON " + season + " · " + banked + "/" + quota; };
    return game;
  }

  /* ---- 52. TUNNEL --------------------------------------------------------

     A perspective tunnel with gaps in it. Rings rush the camera, each with one
     safe sector, and you rotate to meet it — the endless runner rebuilt out of
     the one 3D effect a 320x240 canvas can afford. */
  function cabinetTunnel(random) {
    var SECTORS = 8;

    var game = { id: "tunnel", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var angle, rings, speed, distance, spawnZ, hurt;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      angle = 0;
      rings = [];
      speed = 34;
      distance = 0;
      spawnZ = 0;
      hurt = 0;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 5; i += 1) rings.push({ z: 20 + i * 22, gap: Math.floor(random() * SECTORS), passed: false });
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);

      var turn = 3.4 * dt;
      if (input.left) angle -= turn;
      if (input.right) angle += turn;
      angle = (angle + Math.PI * 2) % (Math.PI * 2);

      speed = Math.min(110, 34 + distance * 0.02);
      distance += speed * dt;

      for (var i = rings.length - 1; i >= 0; i -= 1) {
        var r = rings[i];
        r.z -= speed * dt * 0.6;
        if (r.z <= 2 && !r.passed) {
          r.passed = true;
          var sector = Math.floor(((angle / (Math.PI * 2)) * SECTORS + 0.5)) % SECTORS;
          if (sector !== r.gap) {
            game.lives -= 1;
            hurt = 0.8;
            fx.shake(12);
            fx.flash("danger", 1);
            bits.burst(W / 2, H / 2, 20, { colour: "danger", speed: 110, life: 0.6 });
            if (game.lives <= 0) { game.over = true; return; }
          } else {
            game.score += 15;
            fx.pop(W / 2, H / 2 - 30, "+15", "verify");
          }
        }
        if (r.z < -4) {
          rings.splice(i, 1);
          rings.push({ z: 108, gap: Math.floor(random() * SECTORS), passed: false });
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      var cx = W / 2, cy = H / 2;

      // Rings are drawn back to front as rings of blocks, one block per
      // sector, with the gap simply not drawn.
      rings.slice().sort(function (a, b) { return b.z - a.z; }).forEach(function (r) {
        if (r.z <= 0.5) return;
        var radius = 900 / r.z;
        if (radius > 300) return;
        for (var s = 0; s < SECTORS; s += 1) {
          if (s === r.gap) continue;
          var a = (s / SECTORS) * Math.PI * 2;
          var size = Math.max(2, Math.round(60 / r.z));
          ctx.fillStyle = r.z < 20 ? ink.danger : r.z < 45 ? ink.wall[1] : ink.wall[2];
          ctx.fillRect(
            Math.round(cx + Math.cos(a) * radius) - size / 2,
            Math.round(cy + Math.sin(a) * radius) - size / 2,
            size, size
          );
        }
      });

      bits.draw(ctx, ink);
      // The ship sits on the ring the player is steering, at a fixed radius.
      var sr = 46;
      ctx.fillStyle = hurt > 0 ? ink.bright : ink.verify;
      ctx.fillRect(Math.round(cx + Math.cos(angle) * sr) - 4, Math.round(cy + Math.sin(angle) * sr) - 4, 9, 9);
      ctx.fillStyle = ink.grid;
      ctx.fillRect(cx - 1, cy - 1, 3, 3);

      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.floor(distance) + "m", 4, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(distance) + "m · " + Math.round(speed) + " m/s"; };
    return game;
  }

  /* ---- 53. UPTIME --------------------------------------------------------

     Climb by bouncing. Every platform is a release that holds your weight
     for one landing. The camera only goes up, and falling past the bottom of the
     window is the outage. */
  function cabinetUptime(random) {
    var game = { id: "uptime", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var hopper, plats, camera, height, best;

    function seed() {
      plats = [];
      for (var i = 0; i < 12; i += 1) {
        plats.push({
          x: random() * (W - 40),
          y: H - 20 - i * 22,
          w: 40,
          kind: i === 0 ? "solid" : random() < 0.16 ? "brittle" : random() < 0.12 ? "mover" : "solid",
          dir: random() < 0.5 ? -1 : 1,
          used: false
        });
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      hopper = { x: W / 2, y: H - 40, vx: 0, vy: 0 };
      camera = 0;
      height = 0;
      best = 0;
      fx.reset();
      bits.clear();
      seed();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (input.left) hopper.vx -= 250 * dt;
      if (input.right) hopper.vx += 250 * dt;
      hopper.vx *= 0.9;
      hopper.vx = clamp(hopper.vx, -130, 130);
      hopper.x += hopper.vx * dt;
      if (hopper.x < -6) hopper.x = W + 6;
      if (hopper.x > W + 6) hopper.x = -6;

      hopper.vy += 460 * dt;
      hopper.y += hopper.vy * dt;

      plats.forEach(function (p) {
        if (p.kind === "mover") {
          p.x += p.dir * 40 * dt;
          if (p.x < 0 || p.x > W - p.w) p.dir *= -1;
        }
        if (hopper.vy < 0 || p.used) return;
        var py = p.y + camera;
        if (hopper.y < py - 4 || hopper.y > py + 8) return;
        if (hopper.x < p.x - 4 || hopper.x > p.x + p.w + 4) return;
        hopper.vy = -230;
        bits.burst(hopper.x, py, 4, { colour: "verify", speed: 40, life: 0.3 });
        fx.shake(1);
        if (p.kind === "brittle") p.used = true;
      });

      // The camera only rises, which is the genre's contract with the player.
      var lift = H * 0.42 - hopper.y;
      if (lift > 0) {
        camera += lift;
        hopper.y += lift;
        height += lift;
        game.score = Math.floor(height / 10);
        if (height > best) best = height;
      }

      // Recycle platforms that fell off the bottom to the top of the stack.
      plats.forEach(function (p) {
        if (p.y + camera < H + 20) return;
        p.y -= 12 * 22;
        p.x = random() * (W - 40);
        p.used = false;
        p.kind = random() < 0.18 ? "brittle" : random() < 0.14 ? "mover" : "solid";
      });

      if (hopper.y > H + 12) {
        game.lives -= 1;
        fx.shake(10);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        hopper.y = H * 0.4;
        hopper.vy = -200;
        hopper.x = W / 2;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, camera * 0.4, 30);
      fx.begin(ctx);
      plats.forEach(function (p) {
        var y = p.y + camera;
        if (y < -8 || y > H) return;
        ctx.fillStyle = p.used ? ink.grid : p.kind === "brittle" ? ink.danger : p.kind === "mover" ? ink.brass : ink.verify;
        ctx.fillRect(Math.round(p.x), Math.round(y), p.w, 5);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(hopper.x) - 5, Math.round(hopper.y) - 8, 10, 10);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.floor(height / 10) + "m UP", 4, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(height / 10) + "m · BEST " + Math.floor(best / 10); };
    return game;
  }

  /* ---- 54. GROWTH --------------------------------------------------------

     The arena snake. You grow by absorbing orphaned records, you die by
     crossing your own trail, and the rival lines are doing the same thing to
     the same field — so the late game is about space rather than speed. */
  function cabinetGrowth(random) {
    var game = { id: "growth", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var body, dir, pending, food, rivals, speed, tick, length;

    function spawnFood(n) {
      for (var i = 0; i < n; i += 1) food.push({ x: 10 + random() * (W - 20), y: 20 + random() * (H - 30) });
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      body = [{ x: W / 2, y: H / 2 }];
      dir = { x: 1, y: 0 };
      pending = 0;
      length = 8;
      food = [];
      spawnFood(22);
      rivals = [];
      for (var r = 0; r < 3; r += 1) {
        rivals.push({
          trail: [{ x: random() * W, y: random() * H }],
          dir: { x: random() < 0.5 ? 1 : -1, y: 0 },
          turn: 0,
          len: 12
        });
      }
      speed = 52;
      tick = 0;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (input.left && dir.x === 0) dir = { x: -1, y: 0 };
      else if (input.right && dir.x === 0) dir = { x: 1, y: 0 };
      else if (input.up && dir.y === 0) dir = { x: 0, y: -1 };
      else if (input.down && dir.y === 0) dir = { x: 0, y: 1 };

      tick += dt;
      var step = 1 / 30;
      while (tick >= step) {
        tick -= step;
        var head = body[0];
        var nx = head.x + dir.x * speed * step;
        var ny = head.y + dir.y * speed * step;
        if (nx < 2 || nx > W - 2 || ny < 14 || ny > H - 2) { die(); return; }
        body.unshift({ x: nx, y: ny });
        while (body.length > length) body.pop();

        // Self-collision skips the neck, which is always within a body width
        // of the head and would otherwise kill on the first turn.
        for (var i = 8; i < body.length; i += 1) {
          if (Math.abs(body[i].x - nx) < 3 && Math.abs(body[i].y - ny) < 3) { die(); return; }
        }
        for (var r = 0; r < rivals.length; r += 1) {
          for (var k = 0; k < rivals[r].trail.length; k += 1) {
            var t = rivals[r].trail[k];
            if (Math.abs(t.x - nx) < 3 && Math.abs(t.y - ny) < 3) { die(); return; }
          }
        }

        for (var f = food.length - 1; f >= 0; f -= 1) {
          if (Math.abs(food[f].x - nx) > 5 || Math.abs(food[f].y - ny) > 5) continue;
          food.splice(f, 1);
          length += 4;
          game.score += 10;
          bits.burst(nx, ny, 5, { colour: "verify", speed: 50, life: 0.35 });
          spawnFood(1);
        }
      }

      rivals.forEach(function (rv) {
        rv.turn -= dt;
        if (rv.turn <= 0) {
          rv.turn = 0.5 + random();
          rv.dir = random() < 0.5 ? { x: rv.dir.y, y: rv.dir.x } : { x: -rv.dir.y, y: -rv.dir.x };
        }
        var h = rv.trail[0];
        var nx = clamp(h.x + rv.dir.x * 44 * dt, 4, W - 4);
        var ny = clamp(h.y + rv.dir.y * 44 * dt, 16, H - 4);
        rv.trail.unshift({ x: nx, y: ny });
        while (rv.trail.length > rv.len) rv.trail.pop();
      });
    };

    function die() {
      game.lives -= 1;
      fx.shake(11);
      fx.flash("danger", 1);
      bits.burst(body[0].x, body[0].y, 18, { colour: "danger", speed: 90, life: 0.6 });
      if (game.lives <= 0) { game.over = true; return; }
      body = [{ x: W / 2, y: H / 2 }];
      dir = { x: 1, y: 0 };
      length = 8;
    }

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.grid;
      ctx.fillRect(0, 13, W, 1);
      food.forEach(function (f) {
        ctx.fillStyle = ink.brass;
        ctx.fillRect(Math.round(f.x) - 2, Math.round(f.y) - 2, 4, 4);
      });
      rivals.forEach(function (rv) {
        rv.trail.forEach(function (t, i) {
          ctx.fillStyle = i === 0 ? ink.danger : ink.wall[1];
          ctx.fillRect(Math.round(t.x) - 3, Math.round(t.y) - 3, 6, 6);
        });
      });
      body.forEach(function (b, i) {
        ctx.fillStyle = i === 0 ? ink.bright : ink.verify;
        ctx.fillRect(Math.round(b.x) - 3, Math.round(b.y) - 3, 6, 6);
      });
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LENGTH " + length, 4, 10);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LENGTH " + length; };
    return game;
  }

  /* ---- 55. ABSORB --------------------------------------------------------

     Eat what is smaller, run from what is bigger, and get slower the bigger
     you get. Consolidation as a physics problem. */
  function cabinetAbsorb(random) {
    var game = { id: "absorb", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var me, blobs, era;

    function spawnBlob(sizeHint) {
      var edge = random();
      blobs.push({
        x: edge < 0.5 ? (random() < 0.5 ? -12 : W + 12) : random() * W,
        y: edge < 0.5 ? random() * H : (random() < 0.5 ? -12 : H + 12),
        r: Math.max(3, sizeHint * (0.4 + random() * 1.5)),
        vx: (random() - 0.5) * 40,
        vy: (random() - 0.5) * 40
      });
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      me = { x: W / 2, y: H / 2, r: 7 };
      blobs = [];
      era = 1;
      for (var i = 0; i < 14; i += 1) spawnBlob(7);
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      // Bigger is slower: without that, absorbing is strictly good and the
      // cabinet has no second act.
      var speed = (120 - Math.min(80, me.r * 3)) * dt;
      if (input.left) me.x -= speed;
      if (input.right) me.x += speed;
      if (input.up) me.y -= speed;
      if (input.down) me.y += speed;
      me.x = clamp(me.x, me.r, W - me.r);
      me.y = clamp(me.y, me.r + 10, H - me.r);

      for (var i = blobs.length - 1; i >= 0; i -= 1) {
        var b = blobs[i];
        b.x += b.vx * dt;
        b.y += b.vy * dt;
        if (b.x < -30 || b.x > W + 30 || b.y < -30 || b.y > H + 30) {
          blobs.splice(i, 1);
          spawnBlob(me.r);
          continue;
        }
        var d = Math.hypot(b.x - me.x, b.y - me.y);
        if (d > b.r + me.r - 2) continue;
        if (b.r < me.r - 0.6) {
          me.r = Math.sqrt(me.r * me.r + b.r * b.r * 0.6);
          game.score += Math.round(b.r * 3);
          bits.burst(b.x, b.y, 6, { colour: "verify", speed: 50, life: 0.4 });
          blobs.splice(i, 1);
          spawnBlob(me.r);
          fx.shake(1);
        } else {
          game.lives -= 1;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(me.x, me.y, 20, { colour: "danger", speed: 90, life: 0.6 });
          if (game.lives <= 0) { game.over = true; return; }
          me.r = 7;
          me.x = W / 2;
          me.y = H / 2;
          return;
        }
      }
      era = 1 + Math.floor(me.r / 8);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = ink.grid;
      for (var g = 0; g < W; g += 20) ctx.fillRect(g, 12, 1, H - 12);
      fx.begin(ctx);
      blobs.forEach(function (b) {
        ctx.fillStyle = b.r < me.r ? ink.brass : ink.danger;
        var s = Math.round(b.r * 2);
        ctx.fillRect(Math.round(b.x - b.r), Math.round(b.y - b.r), s, s);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(b.x - b.r) + 2, Math.round(b.y - b.r) + 2, s - 4, s - 4);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      var ms = Math.round(me.r * 2);
      ctx.fillRect(Math.round(me.x - me.r), Math.round(me.y - me.r), ms, ms);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(Math.round(me.x - me.r) + 3, Math.round(me.y - me.r) + 3, ms - 6, ms - 6);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("MASS " + Math.round(me.r * 10), 4, 10);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "MASS " + Math.round(me.r * 10) + " · TIER " + era; };
    return game;
  }

  /* ---- 56. CAVERN --------------------------------------------------------

     Hold to climb, release to fall, and the corridor narrows. One button, no
     forgiveness, and the walls are generated from the distance travelled so
     the run is the same run every time. */
  function cabinetCavern(random) {
    var game = { id: "cavern", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var flyer, scroll, speed, pillars, hurt;

    function roof(x) {
      return 18 + Math.sin(x * 0.021) * 16 + Math.sin(x * 0.007 + 2) * 12;
    }
    function floorAt(x) {
      var squeeze = Math.min(70, 26 + x * 0.006);
      return H - 18 - Math.sin(x * 0.017 + 1.2) * 14 - Math.sin(x * 0.005) * 10 - (70 - squeeze);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      flyer = { y: H / 2, vy: 0 };
      scroll = 0;
      speed = 62;
      pillars = [];
      hurt = 0;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);

      speed = Math.min(140, 62 + scroll * 0.012);
      scroll += speed * dt;
      flyer.vy += (input.fire || input.up ? -300 : 260) * dt;
      flyer.vy = clamp(flyer.vy, -140, 190);
      flyer.y += flyer.vy * dt;

      if (Math.floor(scroll / 140) > pillars.length - 1) {
        pillars.push({ x: scroll + W, gap: 40 + random() * 30, top: 30 + random() * 90 });
      }

      var hitTop = flyer.y < roof(scroll + 60) + 4;
      var hitFloor = flyer.y > floorAt(scroll + 60) - 4;
      var hitPillar = pillars.some(function (p) {
        var sx = p.x - scroll;
        if (sx > 66 || sx < 54) return false;
        return flyer.y < p.top || flyer.y > p.top + p.gap;
      });

      if (hitTop || hitFloor || hitPillar) {
        game.lives -= 1;
        hurt = 0.8;
        fx.shake(12);
        fx.flash("danger", 1);
        bits.burst(60, flyer.y, 16, { colour: "danger", speed: 90, life: 0.5 });
        if (game.lives <= 0) { game.over = true; return; }
        scroll = Math.max(0, scroll - 260);
        flyer.y = (roof(scroll + 60) + floorAt(scroll + 60)) / 2;
        flyer.vy = 0;
        return;
      }
      game.score = Math.floor(scroll / 10);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var x = 0; x < W; x += 2) {
        var wx = x + scroll;
        var r = roof(wx), f = floorAt(wx);
        ctx.fillStyle = ink.wall[2];
        ctx.fillRect(x, 0, 2, r);
        ctx.fillRect(x, f, 2, H - f);
        ctx.fillStyle = ink.wall[1];
        ctx.fillRect(x, r - 2, 2, 2);
        ctx.fillRect(x, f, 2, 2);
      }
      pillars.forEach(function (p) {
        var sx = p.x - scroll;
        if (sx < -14 || sx > W) return;
        ctx.fillStyle = ink.danger;
        ctx.fillRect(sx, 0, 10, p.top);
        ctx.fillRect(sx, p.top + p.gap, 10, H - p.top - p.gap);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = hurt > 0 ? ink.bright : ink.verify;
      ctx.fillRect(56, Math.round(flyer.y) - 4, 12, 8);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.floor(scroll / 10) + "m", 4, 10);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(scroll / 10) + "m"; };
    return game;
  }

  /* ---- 57. TAP ORDER -----------------------------------------------------

     Four columns of arriving calls, one lit at a time, and you answer the one
     at the line. Speed rises until you miss. The purest reaction cabinet
     there is and the one that best suits a thumb. */
  function cabinetTapOrder(random) {
    var LANES = 4;
    var LANE_W = 74;
    var OX = (W - LANES * LANE_W) / 2;
    var KEYS = ["left", "up", "down", "right"];

    var game = { id: "tap-order", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var rows, speed, held, streak, missed;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      rows = [];
      for (var i = 0; i < 5; i += 1) rows.push({ y: -i * 52, lane: Math.floor(random() * LANES), hit: false });
      speed = 74;
      streak = 0;
      missed = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      speed = Math.min(240, 74 + game.score * 0.4);

      rows.forEach(function (r) { r.y += speed * dt; });

      for (var i = rows.length - 1; i >= 0; i -= 1) {
        if (rows[i].y <= H + 20) continue;
        if (!rows[i].hit) {
          missed += 1;
          game.lives -= 1;
          streak = 0;
          fx.shake(9);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        }
        rows.splice(i, 1);
      }
      while (rows.length < 5) {
        var top = rows.reduce(function (a, r) { return Math.min(a, r.y); }, H);
        rows.push({ y: top - 52, lane: Math.floor(random() * LANES), hit: false });
      }

      var pressed = -1;
      for (var k = 0; k < LANES; k += 1) if (input[KEYS[k]]) pressed = k;
      if (pressed < 0) { held = false; return; }
      if (held) return;
      held = true;

      // The lowest unhit row is the only one that can be answered: answering
      // out of order is the mistake this cabinet exists to punish.
      var target = null;
      rows.forEach(function (r) { if (!r.hit && (!target || r.y > target.y)) target = r; });
      if (!target) return;
      if (target.lane === pressed && target.y > H * 0.45) {
        target.hit = true;
        streak += 1;
        game.score += 5 + Math.min(20, streak);
        bits.burst(OX + target.lane * LANE_W + LANE_W / 2, target.y, 6, { colour: "verify", speed: 60, life: 0.35 });
        if (streak % 10 === 0) fx.pop(W / 2, H / 2, "STREAK " + streak, "bright");
      } else {
        streak = 0;
        game.lives -= 1;
        fx.shake(8);
        fx.flash("danger", 0.9);
        if (game.lives <= 0) { game.over = true; return; }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var l = 0; l < LANES; l += 1) {
        ctx.fillStyle = l % 2 ? ink.bg : ink.grid;
        ctx.fillRect(OX + l * LANE_W, 0, LANE_W - 2, H);
      }
      rows.forEach(function (r) {
        if (r.hit) return;
        ctx.fillStyle = r.y > H * 0.45 ? ink.verify : ink.wall[1];
        ctx.fillRect(OX + r.lane * LANE_W + 3, Math.round(r.y) - 22, LANE_W - 8, 44);
      });
      ctx.fillStyle = ink.brass;
      ctx.fillRect(0, Math.round(H * 0.45), W, 2);
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("STREAK " + streak, 4, 12);
      centreText(ctx, ink, "← ↑ ↓ →", H - 8, "dim");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "STREAK " + streak + " · MISSED " + missed; };
    return game;
  }

  /* ---- 58. AIM DRILL -----------------------------------------------------

     Targets appear, shrink, and expire. Hit rate is the score and the clock
     only extends on accuracy, so spraying is strictly worse than waiting. */
  function cabinetAimDrill(random) {
    var game = { id: "aim-drill", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var cursor, targets, clock, hits, shotsFired, held, tier;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cursor = { x: W / 2, y: H / 2 };
      targets = [];
      clock = 25;
      hits = 0;
      shotsFired = 0;
      tier = 1;
      held = false;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 3; i += 1) add();
    };

    function add() {
      targets.push({
        x: 16 + random() * (W - 32),
        y: 26 + random() * (H - 46),
        life: 2.4,
        max: 2.4
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      clock -= dt;

      if (input.pointerX != null) {
        cursor.x = input.pointerX;
        cursor.y = input.pointerY;
      } else {
        var speed = 150 * dt;
        if (input.left) cursor.x -= speed;
        if (input.right) cursor.x += speed;
        if (input.up) cursor.y -= speed;
        if (input.down) cursor.y += speed;
      }
      cursor.x = clamp(cursor.x, 0, W);
      cursor.y = clamp(cursor.y, 0, H);

      for (var i = targets.length - 1; i >= 0; i -= 1) {
        targets[i].life -= dt * (0.8 + tier * 0.12);
        if (targets[i].life > 0) continue;
        targets.splice(i, 1);
        add();
        game.lives -= 1;
        fx.flash("danger", 0.8);
        if (game.lives <= 0) { game.over = true; return; }
      }

      if (input.fire && !held) {
        held = true;
        shotsFired += 1;
        var got = false;
        for (var t = targets.length - 1; t >= 0; t -= 1) {
          var r = 6 + (targets[t].life / targets[t].max) * 8;
          if (Math.hypot(targets[t].x - cursor.x, targets[t].y - cursor.y) > r) continue;
          bits.burst(targets[t].x, targets[t].y, 8, { colour: "verify", speed: 70, life: 0.4 });
          targets.splice(t, 1);
          add();
          hits += 1;
          got = true;
          game.score += 20;
          clock = Math.min(30, clock + 1.1);
          if (hits % 10 === 0) tier += 1;
          break;
        }
        if (!got) {
          clock -= 1.4;
          fx.shake(4);
        }
      } else if (!input.fire) held = false;

      if (clock <= 0) {
        game.lives -= 1;
        clock = 20;
        if (game.lives <= 0) game.over = true;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = ink.grid;
      for (var g = 0; g < W; g += 32) ctx.fillRect(g, 16, 1, H - 16);
      fx.begin(ctx);
      targets.forEach(function (t) {
        var r = Math.round(6 + (t.life / t.max) * 8);
        ctx.fillStyle = ink.danger;
        ctx.fillRect(t.x - r, t.y - r, r * 2, r * 2);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(t.x - r + 3, t.y - r + 3, r * 2 - 6, r * 2 - 6);
        ctx.fillStyle = ink.bright;
        ctx.fillRect(t.x - 2, t.y - 2, 4, 4);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(cursor.x) - 7, Math.round(cursor.y), 5, 1);
      ctx.fillRect(Math.round(cursor.x) + 3, Math.round(cursor.y), 5, 1);
      ctx.fillRect(Math.round(cursor.x), Math.round(cursor.y) - 7, 1, 5);
      ctx.fillRect(Math.round(cursor.x), Math.round(cursor.y) + 3, 1, 5);
      var acc = shotsFired ? Math.round((hits / shotsFired) * 100) : 100;
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ACC " + acc + "%", 4, 12);
      drawBar(ctx, ink, W - 84, 6, 80, 5, clock / 30, clock < 8 ? "danger" : "brass");
      fx.end(ctx, ink);
    };

    game.hud = function () {
      var acc = shotsFired ? Math.round((hits / shotsFired) * 100) : 100;
      return "HITS " + hits + " · ACC " + acc + "%";
    };
    return game;
  }

  /* ---- 59. COLD PATH -----------------------------------------------------

     Wait for green, then answer as fast as you can. Answering early is worse
     than answering slowly, which is the one thing a reaction test and a
     deployment gate agree on. */
  function cabinetColdPath(random) {
    var game = { id: "cold-path", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var state, timer, reaction, best, round, held, history;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      state = "wait";
      timer = 1 + random() * 2.4;
      reaction = 0;
      best = 0;
      round = 1;
      held = false;
      history = [];
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);

      if (state === "wait") {
        timer -= dt;
        if (input.fire && !held) {
          held = true;
          state = "early";
          timer = 1.2;
          game.lives -= 1;
          fx.shake(9);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        } else if (timer <= 0) {
          state = "go";
          reaction = 0;
        }
      } else if (state === "go") {
        reaction += dt;
        if (input.fire && !held) {
          held = true;
          var ms = Math.round(reaction * 1000);
          history.unshift(ms);
          if (history.length > 5) history.pop();
          if (!best || ms < best) best = ms;
          game.score += Math.max(5, 600 - ms);
          state = "result";
          timer = 1.1;
          fx.flash("verify", 0.8);
        } else if (reaction > 1.6) {
          state = "slow";
          timer = 1.1;
          game.lives -= 1;
          if (game.lives <= 0) { game.over = true; return; }
        }
      } else {
        timer -= dt;
        if (timer <= 0) {
          state = "wait";
          timer = 1 + random() * 2.4;
          round += 1;
        }
      }
      if (!input.fire) held = false;
    };

    game.draw = function (ctx, ink) {
      var colour = state === "go" ? ink.verify : state === "early" || state === "slow" ? ink.danger : ink.wall[3];
      ctx.fillStyle = colour;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(20, 40, W - 40, H - 90);
      var label = state === "wait" ? "COLD…" : state === "go" ? "GO" :
                  state === "early" ? "EARLY — THAT IS A DENIAL" :
                  state === "slow" ? "TOO SLOW" : Math.round(reaction * 1000) + " ms";
      centreText(ctx, ink, label, H / 2 - 6, state === "go" ? "verify" : "ink", state === "go" ? 16 : 8);
      centreText(ctx, ink, "ROUND " + round, H / 2 + 18, "dim");
      history.forEach(function (ms, i) {
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.dim;
        ctx.fillText(ms + "ms", 26, H - 40 + i * 8);
      });
      if (best) {
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.brass;
        ctx.fillText("BEST " + best + "ms", W - 96, H - 16);
      }
      fx.end(ctx, ink);
    };

    game.hud = function () { return best ? "BEST " + best + "ms" : "ROUND " + round; };
    return game;
  }

  /* ---- 60. POP THE QUEUE -------------------------------------------------

     Jobs surface out of a grid of slots and go back down whether or not you
     got to them. The classic mole cabinet, and the most honest visualisation
     of a backlog anyone has built. */
  function cabinetPopTheQueue(random) {
    var COLS = 4;
    var ROWS = 3;
    var CELL = 62;
    var OX = (W - COLS * CELL) / 2;
    var OY = 46;

    var game = { id: "pop-the-queue", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var slots, cursor, spawnTimer, level, popped, missed, held;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      slots = [];
      for (var i = 0; i < COLS * ROWS; i += 1) slots.push({ up: 0, bad: false });
      cursor = 5;
      spawnTimer = 0.7;
      level = 1;
      popped = 0;
      missed = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.fire) {
          var s = slots[cursor];
          if (s.up > 0) {
            if (s.bad) {
              game.lives -= 1;
              fx.shake(10);
              fx.flash("danger", 1);
              if (game.lives <= 0) { game.over = true; return; }
            } else {
              popped += 1;
              game.score += 15;
              bits.burst(OX + (cursor % COLS) * CELL + CELL / 2, OY + Math.floor(cursor / COLS) * CELL + CELL / 2, 6,
                { colour: "verify", speed: 60, life: 0.35 });
              if (popped % 12 === 0) level += 1;
            }
            s.up = 0;
          } else {
            fx.shake(2);
          }
        } else if (input.left) cursor = (cursor + slots.length - 1) % slots.length;
        else if (input.right) cursor = (cursor + 1) % slots.length;
        else if (input.up) cursor = (cursor + slots.length - COLS) % slots.length;
        else if (input.down) cursor = (cursor + COLS) % slots.length;
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.24, 0.9 - level * 0.05);
        var free = [];
        slots.forEach(function (s, i) { if (!s.up) free.push(i); });
        if (free.length) {
          var pickIndex = free[Math.floor(random() * free.length)];
          slots[pickIndex].up = Math.max(0.5, 1.5 - level * 0.06);
          slots[pickIndex].bad = random() < Math.min(0.3, 0.08 + level * 0.02);
        }
      }

      slots.forEach(function (s) {
        if (!s.up) return;
        s.up -= dt;
        if (s.up > 0) return;
        s.up = 0;
        if (!s.bad) {
          missed += 1;
          if (missed % 4 === 0) {
            game.lives -= 1;
            fx.flash("danger", 0.7);
            if (game.lives <= 0) game.over = true;
          }
        }
      });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "POP THE QUEUE — NOT THE POISONED ONES", 24, "dim", 7);
      slots.forEach(function (s, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(x + 6, y + 6, CELL - 14, CELL - 14);
        if (s.up > 0) {
          var rise = Math.min(1, s.up * 3);
          var size = Math.round((CELL - 26) * rise);
          ctx.fillStyle = s.bad ? ink.danger : ink.brass;
          ctx.fillRect(x + CELL / 2 - size / 2, y + CELL / 2 - size / 2, size, size);
          ctx.fillStyle = ink.bg;
          ctx.fillRect(x + CELL / 2 - size / 4, y + CELL / 2 - size / 4, size / 2, size / 4);
        }
        if (i === cursor) {
          ctx.fillStyle = ink.ink;
          ctx.fillRect(x + 4, y + 4, CELL - 10, 2);
          ctx.fillRect(x + 4, y + CELL - 8, CELL - 10, 2);
          ctx.fillRect(x + 4, y + 4, 2, CELL - 10);
          ctx.fillRect(x + CELL - 8, y + 4, 2, CELL - 10);
        }
      });
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("POPPED " + popped, 4, 14);
      ctx.fillStyle = ink.danger;
      ctx.fillText("MISSED " + missed, W - 84, 14);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · POPPED " + popped; };
    return game;
  }

  /* ---- 61. SPIN PLATES ---------------------------------------------------

     Six services, each slowing down on its own, and one of you. Tending one
     is the only thing you can do and every second spent tending is a second
     the other five are decaying. Operations, essentially. */
  function cabinetSpinPlates(random) {
    var COUNT = 6;

    var game = { id: "spin-plates", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var plates, cursor, held, uptime, decay, dropped;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      plates = [];
      for (var i = 0; i < COUNT; i += 1) plates.push({ spin: 0.7 + random() * 0.3, phase: random() * 6 });
      cursor = 0;
      held = false;
      uptime = 0;
      decay = 0.05;
      dropped = 0;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      uptime += dt;
      decay = 0.045 + uptime * 0.0016;
      game.score = Math.floor(uptime * 8);

      var pressed = input.left || input.right;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.left) cursor = (cursor + COUNT - 1) % COUNT;
        else cursor = (cursor + 1) % COUNT;
      }

      plates.forEach(function (p, i) {
        p.phase += p.spin * dt * 8;
        if (i === cursor && input.fire) {
          p.spin = Math.min(1, p.spin + 1.1 * dt);
        } else {
          p.spin -= decay * dt;
        }
        if (p.spin <= 0) {
          p.spin = 0.55;
          dropped += 1;
          game.lives -= 1;
          fx.shake(11);
          fx.flash("danger", 1);
          if (game.lives <= 0) game.over = true;
        }
      });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "KEEP EVERY SERVICE SPINNING", 20, "dim");
      plates.forEach(function (p, i) {
        var x = 34 + (i % 3) * 100;
        var y = 70 + Math.floor(i / 3) * 84;
        ctx.fillStyle = ink.wall[2];
        ctx.fillRect(x + 14, y + 10, 4, 44);
        var wobble = Math.sin(p.phase) * (1 - p.spin) * 8;
        var colour = p.spin > 0.6 ? ink.verify : p.spin > 0.3 ? ink.brass : ink.danger;
        ctx.fillStyle = colour;
        ctx.fillRect(x - 4 + wobble, y, 40, 8);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 4 + wobble, y + 2, 24, 4);
        drawBar(ctx, ink, x - 2, y + 58, 36, 4, p.spin, colour === ink.danger ? "danger" : "verify");
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x + 12, y + 66, 8, 4);
        }
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("UPTIME " + uptime.toFixed(1) + "s", 4, 12);
      ctx.fillStyle = ink.danger;
      ctx.fillText("DROPPED " + dropped, W - 92, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "UPTIME " + uptime.toFixed(0) + "s · DROPPED " + dropped; };
    return game;
  }

  /* ---- 62. SHARD FIELD ---------------------------------------------------

     Drifting shards of a partitioned store. Shoot one and it splits; the
     field only clears when every piece is small enough to collect, which is
     what re-sharding feels like from the inside. */
  function cabinetShardField(random) {
    var game = { id: "shard-field", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(70);
    var ship, shards, shots, wave, hurt, cooldown;

    function spawn(x, y, tier) {
      var a = random() * Math.PI * 2;
      shards.push({
        x: x, y: y, tier: tier,
        vx: Math.cos(a) * (14 + tier * 8),
        vy: Math.sin(a) * (14 + tier * 8),
        spin: random() * 4
      });
    }

    function seed() {
      shards = [];
      for (var i = 0; i < 2 + wave; i += 1) {
        spawn(random() < 0.5 ? 10 : W - 10, random() * H, 3);
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      ship = { x: W / 2, y: H / 2, angle: -Math.PI / 2, vx: 0, vy: 0 };
      shots = [];
      wave = 1;
      hurt = 0;
      cooldown = 0;
      fx.reset();
      bits.clear();
      seed();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      cooldown = Math.max(0, cooldown - dt);

      if (input.left) ship.angle -= 3 * dt;
      if (input.right) ship.angle += 3 * dt;
      if (input.up) {
        ship.vx += Math.cos(ship.angle) * 110 * dt;
        ship.vy += Math.sin(ship.angle) * 110 * dt;
        bits.burst(ship.x - Math.cos(ship.angle) * 6, ship.y - Math.sin(ship.angle) * 6, 1,
          { colour: "brass", speed: 30, life: 0.25 });
      }
      ship.vx *= 0.99;
      ship.vy *= 0.99;
      ship.x = (ship.x + ship.vx * dt + W) % W;
      ship.y = (ship.y + ship.vy * dt + H) % H;

      if (input.fire && cooldown <= 0) {
        cooldown = 0.22;
        shots.push({ x: ship.x, y: ship.y, vx: Math.cos(ship.angle) * 190, vy: Math.sin(ship.angle) * 190, life: 1.1 });
      }

      shots.forEach(function (s) {
        s.x = (s.x + s.vx * dt + W) % W;
        s.y = (s.y + s.vy * dt + H) % H;
        s.life -= dt;
      });
      shots = shots.filter(function (s) { return s.life > 0; });

      for (var i = shards.length - 1; i >= 0; i -= 1) {
        var sh = shards[i];
        sh.x = (sh.x + sh.vx * dt + W) % W;
        sh.y = (sh.y + sh.vy * dt + H) % H;
        sh.spin += dt * 2;
        var size = sh.tier * 5;

        for (var s = shots.length - 1; s >= 0; s -= 1) {
          if (Math.abs(shots[s].x - sh.x) > size || Math.abs(shots[s].y - sh.y) > size) continue;
          shots.splice(s, 1);
          shards.splice(i, 1);
          game.score += sh.tier * 12;
          bits.burst(sh.x, sh.y, 8, { colour: "brass", speed: 60, life: 0.5 });
          fx.shake(3);
          if (sh.tier > 1) {
            spawn(sh.x, sh.y, sh.tier - 1);
            spawn(sh.x, sh.y, sh.tier - 1);
          }
          break;
        }
        if (!shards[i]) continue;
        if (hurt <= 0 && Math.abs(sh.x - ship.x) < size && Math.abs(sh.y - ship.y) < size) {
          hurt = 1.2;
          game.lives -= 1;
          fx.shake(12);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          ship.vx = ship.vy = 0;
        }
      }

      if (!shards.length) {
        wave += 1;
        game.score += 100;
        fx.flash("verify", 1);
        seed();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 40);
      fx.begin(ctx);
      shards.forEach(function (sh) {
        var size = sh.tier * 5;
        ctx.fillStyle = ink.wall[1];
        ctx.fillRect(Math.round(sh.x) - size, Math.round(sh.y) - size, size * 2, size * 2);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(sh.x) - size + 3, Math.round(sh.y) - size + 3, size * 2 - 6, size * 2 - 6);
      });
      shots.forEach(function (s) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(s.x) - 1, Math.round(s.y) - 1, 3, 3);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 10) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(Math.round(ship.x) - 4, Math.round(ship.y) - 4, 8, 8);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(ship.x + Math.cos(ship.angle) * 6) - 1, Math.round(ship.y + Math.sin(ship.angle) * 6) - 1, 3, 3);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("WAVE " + wave + " · " + shards.length + " SHARDS", 4, 10);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · " + shards.length + " LEFT"; };
    return game;
  }

  /* ---- 63. INTERCEPT -----------------------------------------------------

     Missile defence. Unsigned calls fall on six tools; you place bursts ahead
     of them and let the blast do the work. Ammunition is finite per wave, so
     one burst that catches three is worth more than three that catch one. */
  function cabinetIntercept(random) {
    var game = { id: "intercept", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(80);
    var cursor, bursts, falling, cities, ammo, wave, spawnLeft, spawnTimer, held;

    function seedWave() {
      spawnLeft = 5 + wave * 2;
      spawnTimer = 0.6;
      ammo = 10 + wave;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cursor = { x: W / 2, y: H / 2 };
      bursts = [];
      falling = [];
      cities = [];
      for (var i = 0; i < 6; i += 1) cities.push({ x: 22 + i * 55, alive: true });
      wave = 1;
      held = false;
      fx.reset();
      bits.clear();
      seedWave();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (input.pointerX != null) {
        cursor.x = input.pointerX;
        cursor.y = input.pointerY;
      } else {
        var speed = 140 * dt;
        if (input.left) cursor.x -= speed;
        if (input.right) cursor.x += speed;
        if (input.up) cursor.y -= speed;
        if (input.down) cursor.y += speed;
      }
      cursor.x = clamp(cursor.x, 4, W - 4);
      cursor.y = clamp(cursor.y, 10, H - 30);

      if (input.fire && !held && ammo > 0) {
        held = true;
        ammo -= 1;
        bursts.push({ x: cursor.x, y: cursor.y, r: 2, grow: true });
      } else if (!input.fire) held = false;

      spawnTimer -= dt;
      if (spawnLeft > 0 && spawnTimer <= 0) {
        spawnTimer = Math.max(0.3, 1.4 - wave * 0.08);
        spawnLeft -= 1;
        var live = cities.filter(function (c) { return c.alive; });
        if (live.length) {
          var target = live[Math.floor(random() * live.length)];
          falling.push({ x: random() * W, y: -6, tx: target.x, speed: 22 + wave * 3 });
        }
      }

      bursts.forEach(function (b) {
        if (b.grow) { b.r += 46 * dt; if (b.r > 22) b.grow = false; }
        else b.r -= 30 * dt;
      });
      bursts = bursts.filter(function (b) { return b.r > 0; });

      for (var i = falling.length - 1; i >= 0; i -= 1) {
        var f = falling[i];
        var dx = f.tx - f.x;
        var d = Math.hypot(dx, H - 26 - f.y) || 1;
        f.x += (dx / d) * f.speed * dt;
        f.y += ((H - 26 - f.y) / d) * f.speed * dt;

        var caught = bursts.some(function (b) { return Math.hypot(b.x - f.x, b.y - f.y) < b.r; });
        if (caught) {
          falling.splice(i, 1);
          game.score += 25;
          bits.burst(f.x, f.y, 8, { colour: "verify", speed: 60, life: 0.4 });
          continue;
        }
        if (f.y >= H - 26) {
          falling.splice(i, 1);
          var hitCity = null;
          cities.forEach(function (c) { if (c.alive && Math.abs(c.x - f.x) < 20) hitCity = c; });
          if (hitCity) {
            hitCity.alive = false;
            game.lives -= 1;
            fx.shake(12);
            fx.flash("danger", 1);
            bits.burst(hitCity.x, H - 20, 20, { colour: "danger", speed: 90, life: 0.7 });
            if (game.lives <= 0) { game.over = true; return; }
          }
        }
      }

      if (!spawnLeft && !falling.length) {
        wave += 1;
        game.score += 80 + ammo * 5;
        cities.forEach(function (c) { if (!c.alive && random() < 0.4) c.alive = true; });
        seedWave();
        fx.flash("verify", 0.8);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 26);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 22, W, 22);
      cities.forEach(function (c) {
        ctx.fillStyle = c.alive ? ink.verify : ink.grid;
        ctx.fillRect(c.x - 14, H - 30, 28, 10);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(c.x - 9, H - 27, 4, 4);
        ctx.fillRect(c.x + 4, H - 27, 4, 4);
      });
      falling.forEach(function (f) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.round(f.x) - 2, Math.round(f.y) - 4, 4, 8);
      });
      bursts.forEach(function (b) {
        ctx.fillStyle = b.grow ? ink.bright : ink.brass;
        var r = Math.round(b.r);
        ctx.fillRect(Math.round(b.x) - r, Math.round(b.y) - r, r * 2, r * 2);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(b.x) - r + 3, Math.round(b.y) - r + 3, r * 2 - 6, r * 2 - 6);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ammo ? ink.verify : ink.danger;
      ctx.fillRect(Math.round(cursor.x) - 6, Math.round(cursor.y), 13, 1);
      ctx.fillRect(Math.round(cursor.x), Math.round(cursor.y) - 6, 1, 13);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BURSTS " + ammo, 4, 12);
      ctx.fillText("WAVE " + wave, W - 60, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · BURSTS " + ammo; };
    return game;
  }

  /* ---- 64. ARTILLERY -----------------------------------------------------

     Two emplacements, one hill, alternating shots, and wind. Adjust angle and
     power, fire, and watch. Turn-based aiming is the one competitive shape
     that works with a single button and no reflexes at all. */
  function cabinetArtillery(random) {
    var game = { id: "artillery", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var angle, power, charging, shell, wind, turn, foeHp, myHp, terrain, message, messageAge, round;

    function buildTerrain() {
      terrain = [];
      for (var x = 0; x <= W; x += 4) {
        terrain.push(H - 44 - Math.sin(x * 0.012 + round) * 22 - Math.sin(x * 0.03) * 8);
      }
      wind = (random() - 0.5) * 30;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      angle = 0.8;
      power = 0.5;
      charging = false;
      shell = null;
      turn = "you";
      foeHp = 3;
      myHp = 3;
      round = 1;
      messageAge = 9;
      fx.reset();
      bits.clear();
      buildTerrain();
    };

    function groundAt(x) {
      return terrain[clamp(Math.floor(x / 4), 0, terrain.length - 1)];
    }

    function launch(fromX, fromY, ang, pow, mine) {
      shell = {
        x: fromX, y: fromY,
        vx: Math.cos(ang) * (70 + pow * 150) * (mine ? 1 : -1),
        vy: -Math.sin(ang) * (70 + pow * 150),
        mine: mine
      };
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (shell) {
        shell.vx += wind * dt * 0.4;
        shell.vy += 190 * dt;
        shell.x += shell.vx * dt;
        shell.y += shell.vy * dt;
        var hitGround = shell.y >= groundAt(shell.x);
        var targetX = shell.mine ? W - 24 : 24;
        var hitTarget = Math.abs(shell.x - targetX) < 12 && shell.y > groundAt(targetX) - 22;
        if (hitTarget || hitGround || shell.x < -20 || shell.x > W + 20) {
          bits.burst(shell.x, Math.min(shell.y, H), 12, { colour: hitTarget ? "verify" : "brass", speed: 70, life: 0.5, gravity: 120 });
          fx.shake(hitTarget ? 10 : 4);
          if (hitTarget) {
            if (shell.mine) {
              foeHp -= 1;
              game.score += 60;
              message = "DIRECT HIT";
              if (foeHp <= 0) {
                round += 1;
                game.score += 150;
                foeHp = 3;
                myHp = Math.min(3, myHp + 1);
                buildTerrain();
                message = "EMPLACEMENT DOWN — ROUND " + round;
              }
            } else {
              myHp -= 1;
              message = "TAKEN A HIT";
              if (myHp <= 0) {
                game.lives -= 1;
                myHp = 3;
                foeHp = 3;
                if (game.lives <= 0) { game.over = true; return; }
              }
            }
          } else {
            message = shell.mine ? "SHORT" : "THEY MISSED";
          }
          messageAge = 0;
          shell = null;
          turn = turn === "you" ? "foe" : "you";
          if (turn === "foe") {
            // The opponent walks its aim toward you over successive rounds.
            var guess = 0.65 + random() * 0.4;
            var pw = 0.45 + random() * 0.4 + round * 0.02;
            launch(W - 24, groundAt(W - 24) - 12, guess, Math.min(1, pw), false);
          }
        }
        return;
      }

      if (turn !== "you") return;
      if (input.up) angle = Math.min(1.4, angle + dt);
      if (input.down) angle = Math.max(0.15, angle - dt);
      if (input.fire) {
        charging = true;
        power = Math.min(1, power + dt * 0.7);
      } else if (charging) {
        charging = false;
        launch(24, groundAt(24) - 12, angle, power, true);
        power = 0.2;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, 0, H * 0.55);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      terrain.forEach(function (y, i) { ctx.fillRect(i * 4, y, 4, H - y); });
      ctx.fillStyle = ink.verify;
      ctx.fillRect(16, groundAt(24) - 14, 18, 14);
      ctx.fillStyle = ink.danger;
      ctx.fillRect(W - 34, groundAt(W - 24) - 14, 18, 14);
      if (shell) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(shell.x) - 2, Math.round(shell.y) - 2, 5, 5);
      }
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(24 + Math.cos(angle) * 20) - 2, Math.round(groundAt(24) - 14 - Math.sin(angle) * 20) - 2, 4, 4);

      drawBar(ctx, ink, 8, 8, 70, 5, power, "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("WIND " + (wind > 0 ? "→" : "←") + Math.abs(wind).toFixed(0), 8, 24);
      ctx.fillStyle = ink.verify;
      ctx.fillText("YOU " + myHp, W - 96, 12);
      ctx.fillStyle = ink.danger;
      ctx.fillText("THEM " + foeHp, W - 46, 12);
      if (messageAge < 1.8) centreText(ctx, ink, message, 40, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · " + (turn === "you" ? "YOUR SHOT" : "THEIRS"); };
    return game;
  }

  /* ---- 65. INVERT --------------------------------------------------------

     A runner with one verb: flip which way down is. The obstacles alternate
     between floor and ceiling, so the whole run is a rhythm of inversions
     rather than a series of jumps. */
  function cabinetInvert(random) {
    var game = { id: "invert", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var runner, blocks, scroll, speed, held, hurt;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      runner = { y: H - 40, vy: 0, flipped: false };
      blocks = [];
      scroll = 0;
      speed = 82;
      held = false;
      hurt = 0;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 6; i += 1) blocks.push({ x: 200 + i * 90, top: random() < 0.5, h: 20 + random() * 26 });
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      speed = Math.min(190, 82 + scroll * 0.01);
      scroll += speed * dt;
      game.score = Math.floor(scroll / 12);

      if (input.fire || input.up) {
        if (!held) {
          held = true;
          runner.flipped = !runner.flipped;
          fx.shake(2);
          bits.burst(50, runner.y, 5, { colour: "verify", speed: 40, life: 0.3 });
        }
      } else held = false;

      var target = runner.flipped ? 24 : H - 40;
      runner.y += (target - runner.y) * Math.min(1, dt * 11);

      for (var i = blocks.length - 1; i >= 0; i -= 1) {
        var b = blocks[i];
        b.x -= speed * dt;
        if (b.x < -40) {
          blocks.splice(i, 1);
          var last = blocks.reduce(function (a, k) { return Math.max(a, k.x); }, W);
          blocks.push({ x: last + 70 + random() * 60, top: random() < 0.5, h: 20 + random() * 30 });
          continue;
        }
        if (b.x > 62 || b.x + 22 < 38) continue;
        var inTop = runner.y < H / 2;
        if (b.top === inTop && hurt <= 0) {
          hurt = 1;
          game.lives -= 1;
          fx.shake(11);
          fx.flash("danger", 1);
          bits.burst(50, runner.y, 14, { colour: "danger", speed: 80, life: 0.5 });
          if (game.lives <= 0) { game.over = true; return; }
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, scroll, 26);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(0, 0, W, 16);
      ctx.fillRect(0, H - 16, W, 16);
      blocks.forEach(function (b) {
        ctx.fillStyle = ink.danger;
        if (b.top) ctx.fillRect(Math.round(b.x), 16, 22, b.h);
        else ctx.fillRect(Math.round(b.x), H - 16 - b.h, 22, b.h);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(44, Math.round(runner.y) - 8, 12, 16);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.floor(scroll / 12) + "m", 4, 28);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(scroll / 12) + "m · " + Math.round(speed) + " m/s"; };
    return game;
  }

  /* ---- 66. DEPTH CHARGE --------------------------------------------------

     Hunt from the surface with sonar. Contacts are only visible in the ping;
     between pings you are working from memory, which is what makes it a game
     rather than a shooting gallery. */
  function cabinetDepthCharge(random) {
    var game = { id: "depth-charge", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var boat, charges, contacts, ping, pingTimer, wave, spawnLeft, held, leaked;

    function seed() {
      spawnLeft = 4 + wave;
      contacts = [];
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      boat = { x: W / 2 };
      charges = [];
      ping = 0;
      pingTimer = 0;
      wave = 1;
      leaked = 0;
      held = false;
      fx.reset();
      bits.clear();
      seed();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      var speed = 84 * dt;
      if (input.left) boat.x -= speed;
      if (input.right) boat.x += speed;
      boat.x = clamp(boat.x, 12, W - 12);

      pingTimer -= dt;
      if (pingTimer <= 0) {
        pingTimer = 2.4;
        ping = 1;
      }
      ping = Math.max(0, ping - dt * 0.9);

      if (input.fire && !held) {
        held = true;
        charges.push({ x: boat.x, y: 26, vy: 46 });
      } else if (!input.fire) held = false;

      if (spawnLeft > 0 && contacts.length < 4 && random() < dt * 0.9) {
        spawnLeft -= 1;
        contacts.push({
          x: random() < 0.5 ? -10 : W + 10,
          y: 70 + random() * (H - 100),
          vx: (random() < 0.5 ? 1 : -1) * (16 + wave * 2)
        });
      }

      for (var i = charges.length - 1; i >= 0; i -= 1) {
        var c = charges[i];
        c.y += c.vy * dt;
        if (c.y > H) { charges.splice(i, 1); continue; }
        for (var k = contacts.length - 1; k >= 0; k -= 1) {
          if (Math.abs(contacts[k].x - c.x) > 12 || Math.abs(contacts[k].y - c.y) > 10) continue;
          bits.burst(contacts[k].x, contacts[k].y, 12, { colour: "verify", speed: 70, life: 0.5 });
          contacts.splice(k, 1);
          charges.splice(i, 1);
          game.score += 40;
          fx.shake(6);
          break;
        }
      }

      for (var m = contacts.length - 1; m >= 0; m -= 1) {
        contacts[m].x += contacts[m].vx * dt;
        if (contacts[m].x > W + 20 || contacts[m].x < -20) {
          contacts.splice(m, 1);
          leaked += 1;
          game.lives -= 1;
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        }
      }

      if (!spawnLeft && !contacts.length) {
        wave += 1;
        game.score += 90;
        fx.flash("verify", 0.8);
        seed();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 32, W, H - 32);
      ctx.fillStyle = ink.grid;
      for (var y = 40; y < H; y += 16) ctx.fillRect(0, y, W, 1);

      // Contacts are only drawn while the ping is live.
      if (ping > 0.05) {
        contacts.forEach(function (c) {
          ctx.fillStyle = ink.danger;
          ctx.fillRect(Math.round(c.x) - 8, Math.round(c.y) - 4, 16, 8);
        });
        ctx.fillStyle = ink.verify;
        var r = Math.round((1 - ping) * 200);
        ctx.fillRect(Math.round(boat.x) - r, 30, r * 2, 1);
      }
      charges.forEach(function (c) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(c.x) - 2, Math.round(c.y) - 3, 5, 6);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(boat.x) - 12, 20, 24, 10);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("WAVE " + wave, 4, 12);
      ctx.fillText("PING " + pingTimer.toFixed(1), W - 70, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "WAVE " + wave + " · LEAKED " + leaked; };
    return game;
  }

  /* ---- 67. PIPE PERMIT ---------------------------------------------------

     Rotate segments to carry flow from the source to the sink before the flow
     arrives. The pressure does not wait for you to finish thinking, which is
     the only difference between this and a wiring diagram. */
  function cabinetPipePermit(random) {
    var COLS = 8;
    var ROWS = 6;
    var CELL = 30;
    var OX = (W - COLS * CELL) / 2;
    var OY = 46;
    // Bit per side: 1 up, 2 right, 4 down, 8 left.
    var SHAPES = [3, 6, 12, 9, 5, 10, 7, 11, 13, 14];

    var game = { id: "pipe-permit", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var grid, cursor, flow, level, held, filled, message, messageAge;

    function rotate(mask) {
      return ((mask << 1) | (mask >> 3)) & 15;
    }

    function build() {
      grid = [];
      for (var i = 0; i < COLS * ROWS; i += 1) {
        grid.push({ mask: SHAPES[Math.floor(random() * SHAPES.length)], wet: false });
      }
      grid[0].mask = 6;
      grid[0].wet = true;
      grid[COLS * ROWS - 1].mask = 9;
      flow = Math.max(9, 22 - level * 1.4);
      filled = 1;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      cursor = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    function spread() {
      // One breadth step per tick from every wet cell into any neighbour whose
      // opening faces back. Cheap, and it makes the flow visibly crawl.
      var added = 0;
      var snapshot = grid.map(function (c) { return c.wet; });
      for (var i = 0; i < grid.length; i += 1) {
        if (!snapshot[i]) continue;
        var x = i % COLS, y = Math.floor(i / COLS);
        var dirs = [
          { bit: 1, back: 4, nx: x, ny: y - 1 },
          { bit: 2, back: 8, nx: x + 1, ny: y },
          { bit: 4, back: 1, nx: x, ny: y + 1 },
          { bit: 8, back: 2, nx: x - 1, ny: y }
        ];
        for (var d = 0; d < dirs.length; d += 1) {
          var dir = dirs[d];
          if (!(grid[i].mask & dir.bit)) continue;
          if (dir.nx < 0 || dir.ny < 0 || dir.nx >= COLS || dir.ny >= ROWS) continue;
          var n = grid[dir.ny * COLS + dir.nx];
          if (n.wet || !(n.mask & dir.back)) continue;
          n.wet = true;
          added += 1;
        }
      }
      return added;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      flow -= dt;

      if (flow <= 0) {
        flow = 1.1;
        var added = spread();
        filled += added;
        game.score += added * 3;
        if (grid[COLS * ROWS - 1].wet) {
          level += 1;
          game.score += 140;
          message = "SINK REACHED — LEVEL " + level;
          messageAge = 0;
          fx.flash("verify", 1);
          build();
          return;
        }
        if (!added) {
          game.lives -= 1;
          message = "FLOW STALLED";
          messageAge = 0;
          fx.shake(10);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          build();
          return;
        }
      }

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;
      if (input.fire) {
        // A wet segment has already committed; rotating it would rewrite
        // history, which this product has opinions about.
        if (grid[cursor].wet) { fx.shake(3); return; }
        grid[cursor].mask = rotate(grid[cursor].mask);
      } else if (input.left) cursor = (cursor + grid.length - 1) % grid.length;
      else if (input.right) cursor = (cursor + 1) % grid.length;
      else if (input.up) cursor = (cursor + grid.length - COLS) % grid.length;
      else if (input.down) cursor = (cursor + COLS) % grid.length;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      grid.forEach(function (cell, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(x + 1, y + 1, CELL - 3, CELL - 3);
        var mid = CELL / 2;
        ctx.fillStyle = cell.wet ? ink.verify : ink.wall[1];
        ctx.fillRect(x + mid - 3, y + mid - 3, 6, 6);
        if (cell.mask & 1) ctx.fillRect(x + mid - 3, y + 1, 6, mid - 1);
        if (cell.mask & 2) ctx.fillRect(x + mid, y + mid - 3, mid - 2, 6);
        if (cell.mask & 4) ctx.fillRect(x + mid - 3, y + mid, 6, mid - 2);
        if (cell.mask & 8) ctx.fillRect(x + 1, y + mid - 3, mid - 1, 6);
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x, y, CELL - 1, 2);
          ctx.fillRect(x, y + CELL - 3, CELL - 1, 2);
        }
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = flow < 4 ? ink.danger : ink.dim;
      ctx.fillText("FLOW IN " + flow.toFixed(1) + "s", 6, 18);
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, W - 66, 18);
      if (messageAge < 2) centreText(ctx, ink, message, H - 8, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + filled + " WET"; };
    return game;
  }

  /* ---- 68. CIRCUIT ROUTE -------------------------------------------------

     Draw a trace from each source to its matching sink without crossing
     another trace. Every pair you connect makes the remaining space worse,
     which is the entire discipline of physical layout. */
  function cabinetCircuitRoute(random) {
    var SIZE = 7;
    var CELL = 28;
    var OX = (W - SIZE * CELL) / 2;
    var OY = 40;

    var game = { id: "circuit-route", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var pads, occupied, cursor, drawing, trace, level, held, done, message, messageAge;

    function build() {
      pads = [];
      occupied = {};
      var pairs = Math.min(4, 2 + Math.floor(level / 2));
      for (var p = 0; p < pairs; p += 1) {
        var a = { x: Math.floor(random() * SIZE), y: Math.floor(random() * SIZE) };
        var b = { x: Math.floor(random() * SIZE), y: Math.floor(random() * SIZE) };
        if ((a.x === b.x && a.y === b.y) || occupied[a.y * SIZE + a.x] || occupied[b.y * SIZE + b.x]) { p -= 1; continue; }
        occupied[a.y * SIZE + a.x] = "pad" + p;
        occupied[b.y * SIZE + b.x] = "pad" + p;
        pads.push({ id: p, a: a, b: b, wired: false });
      }
      done = 0;
      drawing = null;
      trace = [];
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      cursor = { x: 0, y: 0 };
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    function padAt(x, y) {
      for (var i = 0; i < pads.length; i += 1) {
        if (pads[i].a.x === x && pads[i].a.y === y) return { pair: pads[i], end: "a" };
        if (pads[i].b.x === x && pads[i].b.y === y) return { pair: pads[i], end: "b" };
      }
      return null;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var here = padAt(cursor.x, cursor.y);
        if (!drawing) {
          if (here && !here.pair.wired) {
            drawing = here;
            trace = [{ x: cursor.x, y: cursor.y }];
          } else fx.shake(3);
        } else {
          if (here && here.pair === drawing.pair && here.end !== drawing.end) {
            drawing.pair.wired = true;
            trace.forEach(function (t) { occupied[t.y * SIZE + t.x] = "wire"; });
            done += 1;
            game.score += 60;
            fx.flash("verify", 0.8);
            drawing = null;
            trace = [];
            if (done >= pads.length) {
              level += 1;
              game.score += 150;
              message = "BOARD ROUTED — LEVEL " + level;
              messageAge = 0;
              build();
            }
          } else {
            drawing = null;
            trace = [];
            fx.shake(4);
          }
        }
        return;
      }

      var nx = cursor.x + (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var ny = cursor.y + (input.down ? 1 : 0) - (input.up ? 1 : 0);
      if (nx < 0 || ny < 0 || nx >= SIZE || ny >= SIZE) return;
      if (drawing) {
        var key = ny * SIZE + nx;
        var blocked = occupied[key] && occupied[key] !== "pad" + drawing.pair.id;
        if (blocked) {
          // Running into another net drops the trace: no partial routes, the
          // way a shorted board is not a partly working board.
          drawing = null;
          trace = [];
          fx.shake(6);
          game.lives -= 1;
          if (game.lives <= 0) { game.over = true; return; }
        } else {
          trace.push({ x: nx, y: ny });
        }
      }
      cursor.x = nx;
      cursor.y = ny;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var i = 0; i < SIZE * SIZE; i += 1) {
        var x = OX + (i % SIZE) * CELL;
        var y = OY + Math.floor(i / SIZE) * CELL;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
        if (occupied[i] === "wire") {
          ctx.fillStyle = ink.verify;
          ctx.fillRect(x + 9, y + 9, CELL - 19, CELL - 19);
        }
      }
      var colours = ["danger", "brass", "bright", "verify"];
      pads.forEach(function (p) {
        [p.a, p.b].forEach(function (end) {
          ctx.fillStyle = ink[colours[p.id % colours.length]];
          ctx.fillRect(OX + end.x * CELL + 5, OY + end.y * CELL + 5, CELL - 11, CELL - 11);
          if (p.wired) {
            ctx.fillStyle = ink.bg;
            ctx.fillRect(OX + end.x * CELL + 10, OY + end.y * CELL + 10, CELL - 21, CELL - 21);
          }
        });
      });
      trace.forEach(function (t) {
        ctx.fillStyle = ink.ink;
        ctx.fillRect(OX + t.x * CELL + 10, OY + t.y * CELL + 10, CELL - 21, CELL - 21);
      });
      ctx.fillStyle = ink.bright;
      ctx.fillRect(OX + cursor.x * CELL, OY + cursor.y * CELL, CELL - 2, 2);
      ctx.fillRect(OX + cursor.x * CELL, OY + cursor.y * CELL + CELL - 4, CELL - 2, 2);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUTED " + done + "/" + pads.length, 6, 18);
      ctx.fillText("LEVEL " + level, W - 66, 18);
      if (messageAge < 2) centreText(ctx, ink, message, H - 8, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + done + "/" + pads.length; };
    return game;
  }

  /* ---- 69. FACTORY LINE --------------------------------------------------

     A belt of parts and three stamps. Each part wants one stamp; hitting it
     with the wrong one scraps it. Speed rises, and the belt never stops,
     because a belt that stops is not a factory. */
  function cabinetFactoryLine(random) {
    var STAMPS = ["SIGN", "METER", "AUDIT"];

    var game = { id: "factory-line", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var parts, pick, speed, spawnTimer, shipped, scrapped, held, shift;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      parts = [];
      pick = 0;
      speed = 40;
      spawnTimer = 0.9;
      shipped = 0;
      scrapped = 0;
      shift = 1;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      speed = 40 + shift * 6;

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.fire) {
          // The stamp lands on whatever is under the press right now.
          var target = null;
          parts.forEach(function (p) { if (Math.abs(p.x - W / 2) < 18 && !p.done) target = p; });
          if (!target) {
            fx.shake(3);
          } else if (target.want === pick) {
            target.done = true;
            shipped += 1;
            game.score += 25;
            bits.burst(target.x, 118, 6, { colour: "verify", speed: 50, life: 0.35 });
            if (shipped % 10 === 0) shift += 1;
          } else {
            target.done = true;
            target.scrap = true;
            scrapped += 1;
            game.lives -= 1;
            fx.shake(9);
            fx.flash("danger", 1);
            if (game.lives <= 0) { game.over = true; return; }
          }
        } else if (input.left) pick = (pick + STAMPS.length - 1) % STAMPS.length;
        else if (input.right) pick = (pick + 1) % STAMPS.length;
      }

      spawnTimer -= dt;
      if (spawnTimer <= 0) {
        spawnTimer = Math.max(0.5, 1.5 - shift * 0.05);
        parts.push({ x: -14, want: Math.floor(random() * STAMPS.length), done: false, scrap: false });
      }

      for (var i = parts.length - 1; i >= 0; i -= 1) {
        parts[i].x += speed * dt;
        if (parts[i].x < W + 16) continue;
        var p = parts.splice(i, 1)[0];
        if (!p.done) {
          game.lives -= 1;
          fx.flash("danger", 0.8);
          if (game.lives <= 0) { game.over = true; return; }
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 110, W, 26);
      ctx.fillStyle = ink.grid;
      for (var t = 0; t < W; t += 12) ctx.fillRect(t, 134, 6, 2);

      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(W / 2 - 22, 60, 44, 40);
      ctx.fillStyle = ink[["verify", "brass", "danger"][pick]];
      ctx.fillRect(W / 2 - 16, 84, 32, 14);
      ctx.font = '7px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = ink.bg;
      ctx.fillText(STAMPS[pick], W / 2, 92);
      ctx.textAlign = "left";

      parts.forEach(function (p) {
        ctx.fillStyle = p.scrap ? ink.danger : p.done ? ink.verify : ink[["verify", "brass", "danger"][p.want]];
        ctx.fillRect(Math.round(p.x) - 10, 112, 20, 20);
        if (!p.done) {
          ctx.fillStyle = ink.bg;
          ctx.font = '7px "IBM Plex Mono", monospace';
          ctx.textAlign = "center";
          ctx.fillText(STAMPS[p.want].charAt(0), Math.round(p.x), 124);
          ctx.textAlign = "left";
        }
      });
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SHIPPED " + shipped, 4, 14);
      ctx.fillStyle = ink.danger;
      ctx.fillText("SCRAP " + scrapped, W - 76, 14);
      centreText(ctx, ink, "← → change stamp · SPACE press", H - 10, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "SHIFT " + shift + " · SHIPPED " + shipped; };
    return game;
  }

  /* ---- 70. BRIDGE BUILD --------------------------------------------------

     Span the gap with a beam you extend by holding. Too short and the load
     falls in; too long and it topples past the far side. One button, and the
     genre's whole appeal is that you can see how wrong you were. */
  function cabinetBridgeBuild(random) {
    var game = { id: "bridge-build", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var state, beam, walker, here, next, crossed, held, perfect;

    function layout() {
      here = { x: 40, w: 40 + random() * 26 };
      next = { x: here.x + here.w + 30 + random() * 80, w: 30 + random() * 40 };
      beam = 0;
      state = "build";
      walker = here.x + here.w - 10;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      crossed = 0;
      perfect = 0;
      held = false;
      fx.reset();
      bits.clear();
      layout();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (state === "build") {
        if (input.fire) { beam += 74 * dt; held = true; }
        else if (held) { held = false; state = "drop"; }
        return;
      }
      if (state === "drop") {
        state = "walk";
        return;
      }
      if (state === "walk") {
        walker += 66 * dt;
        var tip = here.x + here.w + beam;
        var reachesNext = tip >= next.x && tip <= next.x + next.w;
        if (walker > tip && !reachesNext) {
          game.lives -= 1;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(walker, H - 70, 16, { colour: "danger", speed: 70, life: 0.7, gravity: 200 });
          if (game.lives <= 0) { game.over = true; return; }
          perfect = 0;
          layout();
          return;
        }
        if (walker >= next.x + 12) {
          crossed += 1;
          var middle = Math.abs(tip - (next.x + next.w / 2));
          if (middle < 6) {
            perfect += 1;
            game.score += 60 + perfect * 20;
            fx.pop(tip, H - 90, "PERFECT ×" + perfect, "verify");
          } else {
            perfect = 0;
            game.score += 30;
          }
          fx.flash("verify", 0.7);
          layout();
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, crossed * 40, H * 0.5);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(here.x, H - 60, here.w, 60);
      ctx.fillRect(next.x, H - 60, next.w, 60);

      var tip = here.x + here.w;
      ctx.fillStyle = ink.brass;
      if (state === "build") ctx.fillRect(tip - 3, H - 60 - beam, 5, beam);
      else ctx.fillRect(tip, H - 63, beam, 4);

      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(walker) - 4, H - 74, 8, 12);
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("CROSSED " + crossed, 4, 14);
      if (perfect > 0) centreText(ctx, ink, "PERFECT STREAK ×" + perfect, 28, "verify");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "CROSSED " + crossed + " · PERFECT " + perfect; };
    return game;
  }

  /* ---- 71. SORT KEYS -----------------------------------------------------

     Pour keys between tubes until each tube holds one kind. A key only pours
     onto its own kind or into empty space, so every move is either progress
     or a wasted tube — which is why the genre is a planning puzzle wearing a
     liquid animation. */
  function cabinetSortKeys(random) {
    var TUBES = 6;
    var DEPTH = 4;

    var game = { id: "sort-keys", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var tubes, cursor, picked, level, moves, held, message, messageAge;

    function deal() {
      var kinds = Math.min(4, 2 + Math.floor(level / 2));
      var pool = [];
      for (var k = 1; k <= kinds; k += 1) for (var d = 0; d < DEPTH; d += 1) pool.push(k);
      for (var s = pool.length - 1; s > 0; s -= 1) {
        var j = Math.floor(random() * (s + 1));
        var t = pool[s]; pool[s] = pool[j]; pool[j] = t;
      }
      tubes = [];
      for (var i = 0; i < TUBES; i += 1) tubes.push([]);
      pool.forEach(function (v, i) { tubes[i % kinds].push(v); });
      moves = 0;
      picked = -1;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      cursor = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    function solved() {
      return tubes.every(function (t) {
        return !t.length || (t.length === DEPTH && t.every(function (v) { return v === t[0]; }));
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.left) cursor = (cursor + TUBES - 1) % TUBES;
      else if (input.right) cursor = (cursor + 1) % TUBES;
      else if (input.fire) {
        if (picked < 0) {
          if (!tubes[cursor].length) { fx.shake(3); return; }
          picked = cursor;
        } else if (picked === cursor) {
          picked = -1;
        } else {
          var from = tubes[picked], to = tubes[cursor];
          var v = from[from.length - 1];
          if (to.length >= DEPTH || (to.length && to[to.length - 1] !== v)) {
            fx.shake(4);
            picked = -1;
            return;
          }
          while (from.length && from[from.length - 1] === v && to.length < DEPTH) {
            to.push(from.pop());
          }
          moves += 1;
          picked = -1;
          game.score += 5;
          if (solved()) {
            level += 1;
            game.score += 160;
            message = "SORTED — LEVEL " + level;
            messageAge = 0;
            fx.flash("verify", 1);
            deal();
          } else if (moves > 24 + level * 4) {
            game.lives -= 1;
            message = "OUT OF MOVES";
            messageAge = 0;
            fx.shake(9);
            if (game.lives <= 0) { game.over = true; return; }
            deal();
          }
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "ONE KIND PER TUBE", 22, "dim");
      var colours = ["grid", "verify", "brass", "danger", "bright"];
      tubes.forEach(function (tube, i) {
        var x = 20 + i * 48;
        var baseY = H - 40;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(x, baseY - DEPTH * 22, 34, DEPTH * 22 + 4);
        tube.forEach(function (v, d) {
          ctx.fillStyle = ink[colours[v]] || ink.grid;
          ctx.fillRect(x + 3, baseY - (d + 1) * 22 + 2, 28, 20);
        });
        if (i === picked) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x, baseY - DEPTH * 22 - 8, 34, 4);
        }
        if (i === cursor) {
          ctx.fillStyle = ink.ink;
          ctx.fillRect(x + 13, baseY + 8, 8, 6);
        }
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("MOVES " + moves, 6, 16);
      ctx.fillText("LEVEL " + level, W - 66, 16);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · MOVES " + moves; };
    return game;
  }

  /* ---- 72. BLAST MAP -----------------------------------------------------

     Sweep a grid for unsigned cells using nothing but the counts around the
     ones you have already opened. The genre is deduction under a rule that
     one wrong click ends the board, which is also the deployment model most
     teams are on. */
  function cabinetBlastMap(random) {
    var COLS = 12;
    var ROWS = 9;
    var CELL = 20;
    var OX = (W - COLS * CELL) / 2;
    var OY = 40;

    var game = { id: "blast-map", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var cells, cursor, level, cleared, held, flagging, message, messageAge;

    function idx(x, y) { return y * COLS + x; }

    function neighbours(x, y) {
      var out = [];
      for (var dy = -1; dy <= 1; dy += 1) {
        for (var dx = -1; dx <= 1; dx += 1) {
          if (!dx && !dy) continue;
          var nx = x + dx, ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= COLS || ny >= ROWS) continue;
          out.push(idx(nx, ny));
        }
      }
      return out;
    }

    function deal() {
      cells = [];
      for (var i = 0; i < COLS * ROWS; i += 1) cells.push({ mine: false, open: false, flag: false, count: 0 });
      var mines = Math.min(28, 12 + level * 2);
      while (mines > 0) {
        var pick = Math.floor(random() * cells.length);
        if (cells[pick].mine) continue;
        cells[pick].mine = true;
        mines -= 1;
      }
      for (var y = 0; y < ROWS; y += 1) {
        for (var x = 0; x < COLS; x += 1) {
          if (cells[idx(x, y)].mine) continue;
          cells[idx(x, y)].count = neighbours(x, y).filter(function (n) { return cells[n].mine; }).length;
        }
      }
      cursor = { x: 0, y: 0 };
      cleared = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      flagging = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    function open(x, y) {
      var cell = cells[idx(x, y)];
      if (cell.open || cell.flag) return;
      cell.open = true;
      cleared += 1;
      game.score += 4;
      if (cell.count === 0 && !cell.mine) {
        neighbours(x, y).forEach(function (n) {
          if (!cells[n].open) open(n % COLS, Math.floor(n / COLS));
        });
      }
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var cell = cells[idx(cursor.x, cursor.y)];
        if (cell.mine) {
          game.lives -= 1;
          message = "UNSIGNED CELL — BOARD LOST";
          messageAge = 0;
          fx.shake(12);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          deal();
          return;
        }
        open(cursor.x, cursor.y);
        var safe = cells.filter(function (c) { return !c.mine; }).length;
        if (cells.filter(function (c) { return c.open; }).length >= safe) {
          level += 1;
          game.score += 200;
          message = "SWEPT — LEVEL " + level;
          messageAge = 0;
          fx.flash("verify", 1);
          deal();
        }
        return;
      }
      if (input.left) cursor.x = (cursor.x + COLS - 1) % COLS;
      else if (input.right) cursor.x = (cursor.x + 1) % COLS;
      else if (input.up) cursor.y = (cursor.y + ROWS - 1) % ROWS;
      else if (input.down) cursor.y = (cursor.y + 1) % ROWS;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      cells.forEach(function (cell, i) {
        var x = OX + (i % COLS) * CELL;
        var y = OY + Math.floor(i / COLS) * CELL;
        if (!cell.open) {
          ctx.fillStyle = ink.wall[1];
          ctx.fillRect(x + 1, y + 1, CELL - 3, CELL - 3);
          ctx.fillStyle = ink.wall[2];
          ctx.fillRect(x + 3, y + 3, CELL - 7, CELL - 7);
        } else {
          ctx.fillStyle = ink.grid;
          ctx.fillRect(x + 1, y + 1, CELL - 3, CELL - 3);
          if (cell.count) {
            ctx.font = '8px "IBM Plex Mono", monospace';
            ctx.textAlign = "center";
            ctx.fillStyle = cell.count > 2 ? ink.danger : cell.count > 1 ? ink.brass : ink.verify;
            ctx.fillText(String(cell.count), x + CELL / 2 - 1, y + CELL / 2);
            ctx.textAlign = "left";
          }
        }
      });
      var cx = OX + cursor.x * CELL, cy = OY + cursor.y * CELL;
      ctx.fillStyle = ink.bright;
      ctx.fillRect(cx, cy, CELL - 1, 2);
      ctx.fillRect(cx, cy + CELL - 3, CELL - 1, 2);
      ctx.fillRect(cx, cy, 2, CELL - 1);
      ctx.fillRect(cx + CELL - 3, cy, 2, CELL - 1);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 6, 20);
      ctx.fillText("OPENED " + cleared, W - 90, 20);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · OPENED " + cleared; };
    return game;
  }

  /* ---- 73. WORD LOCK -----------------------------------------------------

     Guess the five-letter scope. Right letter in the right slot goes green,
     right letter in the wrong slot goes amber, and you have six tries. Built
     for five keys: up and down cycle a letter, left and right change slot. */
  function cabinetWordLock(random) {
    var WORDS = [
      "SCOPE", "TOKEN", "GRANT", "AUDIT", "PROOF", "NONCE", "LEDGE",
      "QUOTA", "CLAIM", "TRUST", "LIMIT", "BATCH", "CHAIN", "STAMP"
    ];
    var ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

    var game = { id: "word-lock", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var answer, guesses, current, slot, held, round, message, messageAge;

    function newWord() {
      answer = WORDS[Math.floor(random() * WORDS.length)];
      guesses = [];
      current = ["A", "A", "A", "A", "A"];
      slot = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      newWord();
    };

    function scoreGuess(word) {
      var marks = [];
      for (var i = 0; i < 5; i += 1) {
        marks.push(word[i] === answer.charAt(i) ? 2 : answer.indexOf(word[i]) >= 0 ? 1 : 0);
      }
      return marks;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var word = current.join("");
        var marks = scoreGuess(current);
        guesses.push({ word: word, marks: marks });
        var solvedIt = marks.every(function (m) { return m === 2; });
        if (solvedIt) {
          round += 1;
          game.score += 200 - guesses.length * 20;
          message = "LOCK OPEN — " + word;
          messageAge = 0;
          fx.flash("verify", 1);
          newWord();
        } else if (guesses.length >= 6) {
          game.lives -= 1;
          message = "IT WAS " + answer;
          messageAge = 0;
          fx.shake(10);
          if (game.lives <= 0) { game.over = true; return; }
          newWord();
        }
        return;
      }
      if (input.left) slot = (slot + 4) % 5;
      else if (input.right) slot = (slot + 1) % 5;
      else if (input.up || input.down) {
        var at = ALPHABET.indexOf(current[slot]);
        current[slot] = ALPHABET.charAt((at + (input.up ? 1 : 25)) % 26);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "GUESS THE SCOPE", 18, "dim");
      guesses.forEach(function (g, row) {
        for (var i = 0; i < 5; i += 1) {
          var x = W / 2 - 100 + i * 40;
          var y = 30 + row * 26;
          ctx.fillStyle = g.marks[i] === 2 ? ink.verify : g.marks[i] === 1 ? ink.brass : ink.wall[2];
          ctx.fillRect(x, y, 36, 22);
          ctx.font = '10px "IBM Plex Mono", monospace';
          ctx.textAlign = "center";
          ctx.fillStyle = ink.bg;
          ctx.fillText(g.word.charAt(i), x + 18, y + 12);
          ctx.textAlign = "left";
        }
      });
      for (var i = 0; i < 5; i += 1) {
        var x = W / 2 - 100 + i * 40;
        var y = 30 + guesses.length * 26;
        ctx.fillStyle = i === slot ? ink.bright : ink.wall[3];
        ctx.fillRect(x, y, 36, 22);
        ctx.font = '10px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = i === slot ? ink.bg : ink.ink;
        ctx.fillText(current[i], x + 18, y + 12);
        ctx.textAlign = "left";
      }
      centreText(ctx, ink, "↑ ↓ letter · ← → slot · SPACE submit", H - 22, "dim", 7);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("TRY " + (guesses.length + 1) + "/6", 6, 16);
      if (messageAge < 2) centreText(ctx, ink, message, H - 8, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LOCK " + round + " · TRY " + (guesses.length + 1) + "/6"; };
    return game;
  }

  /* ---- 74. TILE AUDIT ----------------------------------------------------

     Clear the stack by matching free pairs. A tile is free when nothing sits
     on its left or its right, so the order you clear in decides whether the
     board opens up or seizes — which is the same property a dependency graph
     has. */
  function cabinetTileAudit(random) {
    var FACES = ["SIG", "TTL", "REF", "TXN", "IDX", "ACK", "SUM", "SEQ"];

    var game = { id: "tile-audit", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var tiles, cursor, picked, level, held, clock, message, messageAge;

    function deal() {
      tiles = [];
      var rows = 4;
      var cols = 7;
      var pool = [];
      for (var i = 0; i < (rows * cols) / 2; i += 1) {
        var face = FACES[i % FACES.length];
        pool.push(face, face);
      }
      for (var s = pool.length - 1; s > 0; s -= 1) {
        var j = Math.floor(random() * (s + 1));
        var t = pool[s]; pool[s] = pool[j]; pool[j] = t;
      }
      pool.forEach(function (face, i) {
        tiles.push({ face: face, x: i % cols, y: Math.floor(i / cols), gone: false });
      });
      cursor = 0;
      picked = -1;
      clock = 60 + level * 5;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    function free(tile) {
      if (tile.gone) return false;
      var left = tiles.some(function (t) { return !t.gone && t.y === tile.y && t.x === tile.x - 1; });
      var right = tiles.some(function (t) { return !t.gone && t.y === tile.y && t.x === tile.x + 1; });
      return !left || !right;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      clock -= dt;
      if (clock <= 0) {
        game.lives -= 1;
        message = "AUDIT WINDOW CLOSED";
        messageAge = 0;
        fx.shake(9);
        if (game.lives <= 0) { game.over = true; return; }
        deal();
        return;
      }

      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var tile = tiles[cursor];
        if (!tile || tile.gone || !free(tile)) { fx.shake(3); return; }
        if (picked < 0) { picked = cursor; return; }
        if (picked === cursor) { picked = -1; return; }
        var other = tiles[picked];
        if (other.face === tile.face) {
          other.gone = tile.gone = true;
          game.score += 30;
          fx.flash("verify", 0.6);
          if (tiles.every(function (t) { return t.gone; })) {
            level += 1;
            game.score += 200;
            message = "STACK CLEARED — LEVEL " + level;
            messageAge = 0;
            deal();
          }
        } else {
          fx.shake(5);
        }
        picked = -1;
        return;
      }
      var step = input.left ? -1 : input.right ? 1 : input.up ? -7 : 7;
      for (var guard = 0; guard < tiles.length; guard += 1) {
        cursor = (cursor + step + tiles.length) % tiles.length;
        if (!tiles[cursor].gone) break;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      tiles.forEach(function (t, i) {
        if (t.gone) return;
        var x = 22 + t.x * 40;
        var y = 46 + t.y * 42;
        var open = free(t);
        ctx.fillStyle = i === picked ? ink.bright : open ? ink.wall[1] : ink.wall[3];
        ctx.fillRect(x, y, 36, 38);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(x + 2, y + 2, 32, 34);
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = open ? ink.ink : ink.dim;
        ctx.fillText(t.face, x + 18, y + 20);
        ctx.textAlign = "left";
        if (i === cursor) {
          ctx.fillStyle = ink.verify;
          ctx.fillRect(x, y + 36, 36, 3);
        }
      });
      drawBar(ctx, ink, 6, 16, W - 12, 5, clock / (60 + level * 5), clock < 12 ? "danger" : "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 6, 12);
      if (messageAge < 2) centreText(ctx, ink, message, H - 8, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + Math.ceil(clock) + "s"; };
    return game;
  }

  /* ---- 75. PATIENCE ------------------------------------------------------

     A one-column solitaire: play a card one higher or one lower than the pile
     top, or draw a new one at the cost of a card from a small stock. The
     smallest complete card game that still has a decision in it. */
  function cabinetPatience(random) {
    var game = { id: "patience", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var hand, pileTop, stock, cursor, held, streak, round, message, messageAge;

    function deal() {
      hand = [];
      for (var i = 0; i < 6; i += 1) hand.push(1 + Math.floor(random() * 13));
      pileTop = 1 + Math.floor(random() * 13);
      stock = 8;
      cursor = 0;
      streak = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      round = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      deal();
    };

    function playable(v) {
      var diff = Math.abs(v - pileTop);
      return diff === 1 || diff === 12; // king wraps to ace
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var card = hand[cursor];
        if (card == null) return;
        if (playable(card)) {
          pileTop = card;
          hand.splice(cursor, 1);
          if (cursor >= hand.length) cursor = Math.max(0, hand.length - 1);
          streak += 1;
          game.score += 10 * streak;
          fx.flash("verify", 0.5);
          if (!hand.length) {
            round += 1;
            game.score += 150;
            message = "HAND CLEARED — ROUND " + round;
            messageAge = 0;
            deal();
          }
        } else {
          fx.shake(4);
          streak = 0;
        }
        return;
      }
      if (input.up || input.down) {
        // Draw: turn the top of the stock onto the pile, at a cost.
        if (stock > 0) {
          stock -= 1;
          pileTop = 1 + Math.floor(random() * 13);
          streak = 0;
        } else {
          game.lives -= 1;
          message = "STOCK EMPTY";
          messageAge = 0;
          fx.shake(9);
          if (game.lives <= 0) { game.over = true; return; }
          deal();
        }
        return;
      }
      if (input.left) cursor = (cursor + hand.length - 1) % hand.length;
      else if (input.right) cursor = (cursor + 1) % hand.length;
    };

    function label(v) {
      return v === 1 ? "A" : v === 11 ? "J" : v === 12 ? "Q" : v === 13 ? "K" : String(v);
    }

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "ONE HIGHER OR ONE LOWER", 20, "dim");
      ctx.fillStyle = ink.brass;
      ctx.fillRect(W / 2 - 26, 44, 52, 66);
      ctx.font = '20px "IBM Plex Mono", monospace';
      ctx.textAlign = "center";
      ctx.fillStyle = ink.bg;
      ctx.fillText(label(pileTop), W / 2, 78);
      ctx.textAlign = "left";

      hand.forEach(function (card, i) {
        var x = 20 + i * 48;
        var y = H - 90;
        var ok = playable(card);
        ctx.fillStyle = i === cursor ? ink.bright : ok ? ink.verify : ink.wall[2];
        ctx.fillRect(x, y, 40, 56);
        ctx.font = '14px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bg;
        ctx.fillText(label(card), x + 20, y + 28);
        ctx.textAlign = "left";
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("STOCK " + stock, 6, 16);
      ctx.fillText("STREAK " + streak, W - 78, 16);
      centreText(ctx, ink, "↑ ↓ draw from stock", H - 18, "dim", 7);
      if (messageAge < 2) centreText(ctx, ink, message, H - 6, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · STOCK " + stock; };
    return game;
  }

  /* ---- 76. MATE IN ONE ---------------------------------------------------

     One move, one board, one correct answer. A puzzle cabinet where the whole
     content is recognising the shape — no clock pressure, just a queue of
     positions that get less obvious. */
  function cabinetMateInOne(random) {
    var SIZE = 6;
    var CELL = 28;
    var OX = (W - SIZE * CELL) / 2;
    var OY = 48;

    var game = { id: "mate-in-one", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var rook, king, blocker, cursor, level, held, message, messageAge, solvedCount;

    function build() {
      // Generated backwards from a known mate: place the enemy key on an
      // edge, put the rook where it can reach the mating file, and add a
      // blocker that rules out one wrong answer.
      king = { x: Math.floor(random() * SIZE), y: 0 };
      var mateY = SIZE - 1;
      rook = { x: Math.floor(random() * SIZE), y: 2 + Math.floor(random() * (SIZE - 3)) };
      if (rook.x === king.x) rook.x = (rook.x + 1) % SIZE;
      blocker = { x: (king.x + 2 + Math.floor(random() * 2)) % SIZE, y: 1 + Math.floor(random() * 2) };
      cursor = { x: rook.x, y: rook.y };
      // The answer is: move the rook onto the king's file (same column),
      // anywhere it is not blocked.
      if (blocker.x === king.x) blocker.x = (blocker.x + 1) % SIZE;
      void mateY;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      solvedCount = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        var legal = cursor.x === rook.x || cursor.y === rook.y;
        var correct = legal && cursor.x === king.x && cursor.y !== king.y;
        if (correct) {
          solvedCount += 1;
          level += 1;
          game.score += 120;
          message = "MATE — THE FILE WAS THE ANSWER";
          messageAge = 0;
          fx.flash("verify", 1);
          build();
        } else {
          game.lives -= 1;
          message = legal ? "NOT MATE" : "THAT IS NOT A LEGAL ROOK MOVE";
          messageAge = 0;
          fx.shake(8);
          if (game.lives <= 0) { game.over = true; return; }
          build();
        }
        return;
      }
      if (input.left) cursor.x = (cursor.x + SIZE - 1) % SIZE;
      else if (input.right) cursor.x = (cursor.x + 1) % SIZE;
      else if (input.up) cursor.y = (cursor.y + SIZE - 1) % SIZE;
      else if (input.down) cursor.y = (cursor.y + 1) % SIZE;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "ONE MOVE. PUT IT ON THE FILE.", 24, "dim");
      for (var y = 0; y < SIZE; y += 1) {
        for (var x = 0; x < SIZE; x += 1) {
          ctx.fillStyle = (x + y) % 2 ? ink.wall[3] : ink.wall[2];
          ctx.fillRect(OX + x * CELL, OY + y * CELL, CELL, CELL);
        }
      }
      ctx.fillStyle = ink.danger;
      ctx.fillRect(OX + king.x * CELL + 6, OY + king.y * CELL + 6, CELL - 12, CELL - 12);
      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(OX + blocker.x * CELL + 8, OY + blocker.y * CELL + 8, CELL - 16, CELL - 16);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(OX + rook.x * CELL + 5, OY + rook.y * CELL + 5, CELL - 10, CELL - 10);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(OX + cursor.x * CELL, OY + cursor.y * CELL, CELL, 2);
      ctx.fillRect(OX + cursor.x * CELL, OY + cursor.y * CELL + CELL - 2, CELL, 2);
      ctx.fillRect(OX + cursor.x * CELL, OY + cursor.y * CELL, 2, CELL);
      ctx.fillRect(OX + cursor.x * CELL + CELL - 2, OY + cursor.y * CELL, 2, CELL);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("PUZZLE " + level, 6, 16);
      if (messageAge < 2.4) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "PUZZLE " + level + " · SOLVED " + solvedCount; };
    return game;
  }

  /* ---- 77. LONG POLL -----------------------------------------------------

     Fishing, which is the original idle loop: cast, wait, and answer the
     tug in the window it gives you. Named for the transport that works the
     same way and is honest about it. */
  function cabinetLongPoll(random) {
    var game = { id: "long-poll", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var state, timer, depth, tension, caught, missed, held, best;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      state = "idle";
      timer = 0;
      depth = 0;
      tension = 0;
      caught = 0;
      missed = 0;
      best = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      if (state === "idle") {
        if (input.fire && !held) {
          held = true;
          state = "waiting";
          timer = 0.8 + random() * 3.4;
          depth = 40 + random() * 120;
        }
      } else if (state === "waiting") {
        timer -= dt;
        if (input.fire && !held) {
          held = true;
          missed += 1;
          fx.shake(5);
          state = "idle";
        } else if (timer <= 0) {
          state = "bite";
          timer = Math.max(0.28, 0.75 - caught * 0.02);
          bits.burst(W / 2, 70 + depth, 6, { colour: "verify", speed: 40, life: 0.4 });
        }
      } else if (state === "bite") {
        timer -= dt;
        if (input.fire && !held) {
          held = true;
          state = "reel";
          tension = 0.5;
        } else if (timer <= 0) {
          missed += 1;
          state = "idle";
          if (missed % 4 === 0) {
            game.lives -= 1;
            fx.flash("danger", 0.9);
            if (game.lives <= 0) { game.over = true; return; }
          }
        }
      } else if (state === "reel") {
        // Holding reels in and raises tension; letting go drops both. The
        // window is narrow and moves, which is the whole minigame.
        tension += (input.fire ? 0.72 : -0.85) * dt;
        depth -= (input.fire ? 44 : -18) * dt;
        if (tension > 1) {
          missed += 1;
          fx.shake(9);
          fx.flash("danger", 0.9);
          state = "idle";
          game.lives -= 1;
          if (game.lives <= 0) { game.over = true; return; }
        } else if (depth <= 0) {
          caught += 1;
          var value = 20 + Math.round(depth * -1) + caught * 4;
          game.score += Math.max(20, value);
          if (caught > best) best = caught;
          fx.flash("verify", 1);
          fx.pop(W / 2, 60, "+" + Math.max(20, value), "verify");
          state = "idle";
        }
        tension = Math.max(0, tension);
      }
      if (!input.fire) held = false;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 60, W, H - 60);
      ctx.fillStyle = ink.grid;
      for (var y = 70; y < H; y += 18) ctx.fillRect(0, y, W, 1);
      ctx.fillStyle = ink.wall[1];
      ctx.fillRect(W / 2 - 30, 40, 60, 20);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(W / 2 - 1, 56, 2, Math.max(4, 14 + depth));
      var hookY = 70 + depth;
      ctx.fillStyle = state === "bite" ? ink.bright : ink.verify;
      ctx.fillRect(W / 2 - 4, hookY - 4, 8, 8);
      bits.draw(ctx, ink);
      var label = state === "idle" ? "SPACE TO CAST" :
                  state === "waiting" ? "WAITING…" :
                  state === "bite" ? "TUG — NOW" : "REEL: HOLD, BUT NOT TOO HARD";
      centreText(ctx, ink, label, 22, state === "bite" ? "bright" : "dim");
      if (state === "reel") drawBar(ctx, ink, 40, 30, W - 80, 6, tension, tension > 0.75 ? "danger" : "verify");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("CAUGHT " + caught, 4, 12);
      ctx.fillStyle = ink.danger;
      ctx.fillText("LOST " + missed, W - 66, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "CAUGHT " + caught + " · LOST " + missed; };
    return game;
  }

  /* ---- 78. PET AGENT -----------------------------------------------------

     Keep one agent alive. It gets hungry, bored and unpatched at different
     rates, and you can only attend to one need at a time. The virtual-pet
     genre is a resource-allocation game wearing a face. */
  function cabinetPetAgent(random) {
    var NEEDS = [
      { name: "TOKENS", drain: 0.055, colour: "brass" },
      { name: "ATTENTION", drain: 0.04, colour: "verify" },
      { name: "PATCHES", drain: 0.03, colour: "danger" }
    ];

    var game = { id: "pet-agent", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var levels, pick, age, held, mood, era;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      levels = [0.8, 0.8, 0.8];
      pick = 0;
      age = 0;
      mood = 1;
      era = 1;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      age += dt;
      era = 1 + Math.floor(age / 25);
      game.score = Math.floor(age * 6);

      var pressed = input.left || input.right;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        pick = (pick + (input.right ? 1 : NEEDS.length - 1)) % NEEDS.length;
      }

      NEEDS.forEach(function (need, i) {
        var drain = need.drain * (1 + era * 0.14);
        if (i === pick && input.fire) {
          levels[i] = Math.min(1, levels[i] + 0.5 * dt);
          if (random() < dt * 8) bits.burst(W / 2, 108, 1, { colour: need.colour, speed: 30, life: 0.4 });
        } else {
          levels[i] -= drain * dt;
        }
        if (levels[i] <= 0) {
          levels[i] = 0.5;
          game.lives -= 1;
          fx.shake(11);
          fx.flash("danger", 1);
          if (game.lives <= 0) game.over = true;
        }
      });
      mood = (levels[0] + levels[1] + levels[2]) / 3;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "GENERATION " + era, 20, "dim");

      var bounce = Math.sin(age * 3) * (mood > 0.5 ? 3 : 0);
      var face = mood > 0.66 ? ink.verify : mood > 0.33 ? ink.brass : ink.danger;
      ctx.fillStyle = face;
      ctx.fillRect(W / 2 - 26, 60 + bounce, 52, 46);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(W / 2 - 16, 74 + bounce, 8, 8);
      ctx.fillRect(W / 2 + 8, 74 + bounce, 8, 8);
      if (mood > 0.5) ctx.fillRect(W / 2 - 12, 92 + bounce, 24, 4);
      else ctx.fillRect(W / 2 - 12, 96 + bounce, 24, 3);
      bits.draw(ctx, ink);

      NEEDS.forEach(function (need, i) {
        var y = 138 + i * 26;
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.fillStyle = i === pick ? ink.bright : ink.dim;
        ctx.fillText((i === pick ? "▶ " : "  ") + need.name, 20, y + 4);
        drawBar(ctx, ink, 120, y - 4, 170, 8, levels[i], levels[i] > 0.35 ? need.colour : "danger");
      });
      centreText(ctx, ink, "← → pick a need · SPACE hold to tend", H - 10, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "GEN " + era + " · " + Math.floor(age) + "s ALIVE"; };
    return game;
  }

  /* ---- 79. CHECKOUT ------------------------------------------------------

     Darts, reduced to the only interesting part: you have a number to finish
     on, three throws, and a board that punishes greed. A moving crosshair,
     one button, and arithmetic. */
  function cabinetCheckout(random) {
    var game = { id: "checkout", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var target, remaining, throwsLeft, aim, dir, held, leg, message, messageAge;

    var SEGMENTS = [20, 5, 12, 9, 14, 11, 8, 16, 7, 19, 3, 17, 2, 15, 10, 6, 13, 4, 18, 1];

    function newLeg() {
      remaining = 41 + Math.floor(random() * 60);
      throwsLeft = 3;
      target = remaining;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      leg = 1;
      aim = 0;
      dir = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
      newLeg();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      aim += dir * dt * (0.9 + leg * 0.06);
      if (aim > 1) { aim = 1; dir = -1; }
      if (aim < 0) { aim = 0; dir = 1; }

      if (!input.fire) { held = false; return; }
      if (held) return;
      held = true;

      var index = Math.min(SEGMENTS.length - 1, Math.floor(aim * SEGMENTS.length));
      var value = SEGMENTS[index];
      // A near-centre release doubles: the greed decision the genre is about.
      var centred = Math.abs(aim - 0.5) < 0.06;
      if (centred) value *= 2;
      throwsLeft -= 1;
      bits.burst(W / 2 + (aim - 0.5) * 200, 110, 5, { colour: centred ? "bright" : "brass", speed: 40, life: 0.4 });

      if (value === remaining) {
        game.score += 120 + leg * 20;
        leg += 1;
        message = "CHECKOUT ON " + value;
        messageAge = 0;
        fx.flash("verify", 1);
        newLeg();
        return;
      }
      if (value > remaining) {
        message = "BUST — " + value + " OVER";
        messageAge = 0;
        fx.shake(7);
        throwsLeft = 0;
      } else {
        remaining -= value;
        game.score += value;
        message = "SCORED " + value;
        messageAge = 0;
      }

      if (throwsLeft <= 0) {
        game.lives -= 1;
        message = "LEG LOST ON " + remaining;
        messageAge = 0;
        if (game.lives <= 0) { game.over = true; return; }
        newLeg();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "FINISH ON " + remaining, 30, "bright", 16);
      centreText(ctx, ink, "FROM " + target + " · " + throwsLeft + " DARTS LEFT", 48, "dim");

      for (var i = 0; i < SEGMENTS.length; i += 1) {
        var x = W / 2 - 100 + i * 10;
        ctx.fillStyle = i % 2 ? ink.wall[2] : ink.wall[1];
        ctx.fillRect(x, 80, 9, 40);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = ink.dim;
        ctx.fillText(String(SEGMENTS[i]), x + 4, 100);
        ctx.textAlign = "left";
      }
      ctx.fillStyle = ink.grid;
      ctx.fillRect(W / 2 - 8, 76, 16, 48);
      var ax = W / 2 - 100 + aim * 200;
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(ax) - 1, 70, 3, 58);
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEG " + leg, 6, 14);
      centreText(ctx, ink, "CENTRE BAND DOUBLES", 140, "verify", 7);
      if (messageAge < 2) centreText(ctx, ink, message, 170, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEG " + leg + " · NEEDS " + remaining; };
    return game;
  }

  /* ---- 80. ONE UNDER -----------------------------------------------------

     Top-down golf. Aim, power, and a ball that runs out of pace on the way;
     the rough slows it and the water resets the hole. Par is a budget and
     going over it costs you. */
  function cabinetOneUnder(random) {
    var game = { id: "one-under", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var ball, hole, hazards, aim, power, charging, strokes, par, holeNo, message, messageAge;

    function build() {
      ball = { x: 30, y: H / 2, vx: 0, vy: 0 };
      hole = { x: W - 40 - random() * 40, y: 30 + random() * (H - 60) };
      hazards = [];
      for (var i = 0; i < 2 + Math.min(4, holeNo); i += 1) {
        hazards.push({
          x: 80 + random() * (W - 150),
          y: 20 + random() * (H - 40),
          r: 12 + random() * 14,
          water: random() < 0.4
        });
      }
      par = 3;
      strokes = 0;
      aim = 0;
      power = 0.4;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      holeNo = 1;
      messageAge = 9;
      fx.reset();
      bits.clear();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      var moving = Math.hypot(ball.vx, ball.vy) > 3;
      if (moving) {
        var drag = 0.985;
        hazards.forEach(function (h) {
          if (h.water) return;
          if (Math.hypot(h.x - ball.x, h.y - ball.y) < h.r) drag = 0.93;
        });
        ball.vx *= drag;
        ball.vy *= drag;
        ball.x += ball.vx * dt;
        ball.y += ball.vy * dt;
        if (ball.x < 4 || ball.x > W - 4) ball.vx *= -0.6;
        if (ball.y < 4 || ball.y > H - 4) ball.vy *= -0.6;
        ball.x = clamp(ball.x, 4, W - 4);
        ball.y = clamp(ball.y, 4, H - 4);

        var drowned = hazards.some(function (h) {
          return h.water && Math.hypot(h.x - ball.x, h.y - ball.y) < h.r;
        });
        if (drowned) {
          strokes += 1;
          ball.x = 30; ball.y = H / 2; ball.vx = ball.vy = 0;
          message = "IN THE WATER — PENALTY";
          messageAge = 0;
          fx.shake(7);
        }
        if (Math.hypot(hole.x - ball.x, hole.y - ball.y) < 7) {
          var delta = strokes - par;
          game.score += Math.max(20, 140 - delta * 40);
          message = delta < 0 ? "UNDER PAR" : delta === 0 ? "PAR" : "OVER BY " + delta;
          messageAge = 0;
          fx.flash("verify", 1);
          bits.burst(hole.x, hole.y, 12, { colour: "verify", speed: 60, life: 0.5 });
          if (delta > 2) {
            game.lives -= 1;
            if (game.lives <= 0) { game.over = true; return; }
          }
          holeNo += 1;
          build();
        }
        return;
      }
      ball.vx = ball.vy = 0;

      if (input.left) aim -= 2.2 * dt;
      if (input.right) aim += 2.2 * dt;
      if (input.fire) {
        charging = true;
        power = Math.min(1, power + dt * 0.85);
      } else if (charging) {
        charging = false;
        strokes += 1;
        ball.vx = Math.cos(aim) * (90 + power * 190);
        ball.vy = Math.sin(aim) * (90 + power * 190);
        power = 0.2;
        if (strokes > par + 3) {
          game.lives -= 1;
          message = "OFF THE CARD";
          messageAge = 0;
          if (game.lives <= 0) { game.over = true; return; }
          holeNo += 1;
          build();
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      hazards.forEach(function (h) {
        ctx.fillStyle = h.water ? ink.grid : ink.wall[2];
        ctx.fillRect(h.x - h.r, h.y - h.r, h.r * 2, h.r * 2);
      });
      ctx.fillStyle = ink.bg;
      ctx.fillRect(hole.x - 5, hole.y - 5, 10, 10);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(hole.x - 1, hole.y - 16, 2, 12);
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(ball.x) - 3, Math.round(ball.y) - 3, 6, 6);
      if (Math.hypot(ball.vx, ball.vy) < 3) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(ball.x + Math.cos(aim) * 18) - 2, Math.round(ball.y + Math.sin(aim) * 18) - 2, 4, 4);
        drawBar(ctx, ink, 8, H - 12, 90, 6, power, "brass");
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("HOLE " + holeNo + " · PAR " + par, 6, 14);
      ctx.fillText("STROKES " + strokes, W - 94, 14);
      if (messageAge < 2) centreText(ctx, ink, message, 30, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "HOLE " + holeNo + " · " + strokes + "/" + par; };
    return game;
  }

  /* ---- 81. STRIKE QUOTA --------------------------------------------------

     Bowling with a moving marker for line and a second for power. Two throws
     a frame, ten frames a game, and a quota you have to beat. */
  function cabinetStrikeQuota(random) {
    var game = { id: "strike-quota", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var pins, ball, marker, dir, phase, line, frame, throwNo, framePins, quota, held, message, messageAge;

    function rack() {
      pins = [];
      var rows = 4;
      var i = 0;
      for (var r = 0; r < rows; r += 1) {
        for (var c = 0; c <= r; c += 1) {
          pins.push({ x: W / 2 + (c - r / 2) * 16, y: 50 + r * 14, down: false, id: i++ });
        }
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      frame = 1;
      throwNo = 1;
      framePins = 0;
      quota = 4;
      phase = "line";
      marker = 0;
      dir = 1;
      ball = null;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
      rack();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (ball) {
        ball.y -= ball.speed * dt;
        ball.x += ball.drift * dt;
        pins.forEach(function (p) {
          if (p.down) return;
          if (Math.abs(p.x - ball.x) > 9 || Math.abs(p.y - ball.y) > 9) return;
          p.down = true;
          framePins += 1;
          game.score += 10;
          bits.burst(p.x, p.y, 5, { colour: "bright", speed: 60, life: 0.4 });
          ball.drift += (random() - 0.5) * 30;
        });
        if (ball.y < 30) {
          ball = null;
          if (throwNo === 1 && framePins < pins.length) {
            throwNo = 2;
            phase = "line";
          } else {
            if (framePins >= quota) {
              game.score += 40;
              message = framePins === pins.length ? "STRIKE" : "QUOTA MET";
            } else {
              game.lives -= 1;
              message = "QUOTA MISSED — " + framePins + "/" + quota;
              fx.shake(9);
              if (game.lives <= 0) { game.over = true; return; }
            }
            messageAge = 0;
            frame += 1;
            quota = Math.min(10, 4 + Math.floor(frame / 2));
            throwNo = 1;
            framePins = 0;
            phase = "line";
            rack();
          }
        }
        return;
      }

      marker += dir * dt * 1.3;
      if (marker > 1) { marker = 1; dir = -1; }
      if (marker < 0) { marker = 0; dir = 1; }

      if (input.fire && !held) {
        held = true;
        if (phase === "line") {
          line = marker;
          phase = "power";
          marker = 0;
          dir = 1;
        } else {
          ball = {
            x: W / 2 + (line - 0.5) * 120,
            y: H - 20,
            speed: 120 + marker * 190,
            drift: (0.5 - line) * 40
          };
          phase = "line";
        }
      } else if (!input.fire) held = false;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(W / 2 - 70, 20, 140, H - 20);
      pins.forEach(function (p) {
        if (p.down) return;
        ctx.fillStyle = ink.ink;
        ctx.fillRect(p.x - 4, p.y - 8, 8, 14);
        ctx.fillStyle = ink.danger;
        ctx.fillRect(p.x - 4, p.y - 4, 8, 3);
      });
      bits.draw(ctx, ink);
      if (ball) {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(Math.round(ball.x) - 6, Math.round(ball.y) - 6, 12, 12);
      } else {
        var mx = W / 2 - 70 + marker * 140;
        ctx.fillStyle = phase === "line" ? ink.bright : ink.brass;
        ctx.fillRect(Math.round(mx) - 2, H - 24, 5, 12);
        centreText(ctx, ink, phase === "line" ? "SET THE LINE" : "SET THE POWER", H - 32, "dim", 7);
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("FRAME " + frame + " · THROW " + throwNo, 4, 12);
      ctx.fillStyle = framePins >= quota ? ink.verify : ink.brass;
      ctx.fillText("QUOTA " + framePins + "/" + quota, W - 96, 12);
      if (messageAge < 2) centreText(ctx, ink, message, 32, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "FRAME " + frame + " · " + framePins + "/" + quota; };
    return game;
  }

  /* ---- 82. SERVICE MENU --------------------------------------------------

     Short-order cooking: tickets arrive with two or three components, you
     assemble in order, and a wrong component throws the plate. The fastest
     way to understand a pipeline is to have to run one. */
  function cabinetServiceMenu(random) {
    var PARTS = ["AUTH", "METER", "SIGN", "LOG"];

    var game = { id: "service-menu", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var order, built, pick, timer, served, level, held;

    function newOrder() {
      var size = 2 + Math.min(2, Math.floor(level / 3));
      order = [];
      for (var i = 0; i < size; i += 1) order.push(Math.floor(random() * PARTS.length));
      built = [];
      timer = Math.max(3.5, 9 - level * 0.35);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      served = 0;
      pick = 0;
      held = false;
      fx.reset();
      bits.clear();
      newOrder();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      timer -= dt;
      if (timer <= 0) {
        game.lives -= 1;
        fx.shake(10);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        newOrder();
        return;
      }

      var pressed = input.left || input.right || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        if (order[built.length] === pick) {
          built.push(pick);
          bits.burst(W / 2, 150, 4, { colour: "verify", speed: 40, life: 0.3 });
          if (built.length === order.length) {
            served += 1;
            game.score += 40 + Math.round(timer * 6);
            fx.flash("verify", 0.8);
            if (served % 5 === 0) level += 1;
            newOrder();
          }
        } else {
          built = [];
          fx.shake(7);
          fx.flash("danger", 0.8);
        }
        return;
      }
      pick = (pick + (input.right ? 1 : PARTS.length - 1)) % PARTS.length;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "TICKET — IN THIS ORDER", 22, "dim");
      order.forEach(function (p, i) {
        var x = W / 2 - (order.length * 34) / 2 + i * 34;
        ctx.fillStyle = i < built.length ? ink.verify : ink.wall[2];
        ctx.fillRect(x, 34, 30, 26);
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bg;
        ctx.fillText(PARTS[p], x + 15, 48);
        ctx.textAlign = "left";
      });
      drawBar(ctx, ink, 30, 70, W - 60, 6, timer / 9, timer < 2.5 ? "danger" : "brass");

      PARTS.forEach(function (p, i) {
        var x = 20 + i * 72;
        ctx.fillStyle = i === pick ? ink.bright : ink.wall[1];
        ctx.fillRect(x, 120, 62, 44);
        ctx.font = '8px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = i === pick ? ink.bg : ink.ink;
        ctx.fillText(p, x + 31, 144);
        ctx.textAlign = "left";
      });
      bits.draw(ctx, ink);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SERVED " + served, 6, 14);
      ctx.fillText("LEVEL " + level, W - 66, 14);
      centreText(ctx, ink, "← → choose · SPACE add", H - 12, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · SERVED " + served; };
    return game;
  }

  /* ---- 83. DRIFT QUEUE ---------------------------------------------------

     Top-down drift racing on a scrolling road. Hold a turn and the tail steps
     out; the scoring rewards holding a slide through a corner rather than
     driving it cleanly, because that is the genre's whole personality. */
  function cabinetDriftQueue(random) {
    var game = { id: "drift-queue", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var car, road, scroll, speed, driftMeter, combo, hurt;

    function roadCentre(y) {
      return W / 2 + Math.sin(y * 0.009) * 62 + Math.sin(y * 0.021 + 2) * 26;
    }
    function roadWidth(y) {
      return 78 - Math.min(28, y * 0.0022);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      car = { x: W / 2, angle: 0, slip: 0 };
      scroll = 0;
      speed = 90;
      driftMeter = 0;
      combo = 0;
      hurt = 0;
      fx.reset();
      bits.clear();
      void road;
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      speed = Math.min(210, 90 + scroll * 0.006);
      scroll += speed * dt;

      var steer = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      var handbrake = input.fire || input.down;
      car.slip += (steer * (handbrake ? 3.4 : 1.6) - car.slip * (handbrake ? 1.4 : 4)) * dt;
      car.slip = clamp(car.slip, -1.6, 1.6);
      car.x += car.slip * 66 * dt;

      var centre = roadCentre(scroll);
      var half = roadWidth(scroll) / 2;
      var off = Math.abs(car.x - centre);

      if (Math.abs(car.slip) > 0.7 && off < half) {
        driftMeter += dt;
        combo = 1 + Math.floor(driftMeter * 2);
        game.score += Math.round(24 * dt * combo);
        if (random() < dt * 20) {
          bits.burst(car.x - car.slip * 8, H - 46, 1, { colour: "dim", speed: 30, life: 0.5 });
        }
      } else {
        if (driftMeter > 1.2) fx.pop(car.x, H - 70, "DRIFT ×" + combo, "verify");
        driftMeter = 0;
        combo = 0;
      }

      if (off > half) {
        if (hurt <= 0) {
          hurt = 1;
          game.lives -= 1;
          driftMeter = 0;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(car.x, H - 46, 16, { colour: "danger", speed: 80, life: 0.5 });
          if (game.lives <= 0) { game.over = true; return; }
        }
        car.x += (centre - car.x) * 3 * dt;
        car.slip *= 0.5;
      }
      car.x = clamp(car.x, 6, W - 6);
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      for (var y = 0; y < H; y += 4) {
        var wy = scroll + (H - y) * 1.4;
        var c = roadCentre(wy);
        var hw = roadWidth(wy) / 2;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(c - hw, y, hw * 2, 4);
        ctx.fillStyle = ink.wall[1];
        ctx.fillRect(c - hw - 3, y, 3, 4);
        ctx.fillRect(c + hw, y, 3, 4);
        if (Math.floor(wy / 26) % 2 === 0) {
          ctx.fillStyle = ink.grid;
          ctx.fillRect(c - 1, y, 2, 4);
        }
      }
      bits.draw(ctx, ink);
      var lean = Math.round(car.slip * 4);
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(Math.round(car.x) - 7 + lean, H - 54, 14, 20);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(Math.round(car.x) - 4 + lean, H - 50, 8, 6);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.round(speed) + " km/h", 4, 12);
      if (combo) centreText(ctx, ink, "DRIFT ×" + combo, 24, "verify");
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(scroll / 20) + "m · DRIFT ×" + combo; };
    return game;
  }

  /* ---- 84. MINE CART -----------------------------------------------------

     A cart on rails that only ever goes forward. You pick the branch and you
     jump the gaps; the track is generated far enough ahead to be readable and
     no further, which is the honest version of a roadmap. */
  function cabinetMineCart(random) {
    var game = { id: "mine-cart", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var cart, rails, scroll, speed, held, hurt, ore;

    function railY(lane) { return H - 50 - lane * 42; }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      cart = { lane: 0, y: railY(0), vy: 0, air: false };
      rails = [];
      scroll = 0;
      speed = 96;
      ore = 0;
      held = false;
      hurt = 0;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 14; i += 1) addSegment(120 + i * 70);
    };

    function addSegment(x) {
      rails.push({
        x: x,
        lanes: [true, random() < 0.6, random() < 0.35],
        gap: random() < 0.2,
        ore: random() < 0.4 ? Math.floor(random() * 3) : -1,
        rock: random() < 0.25 ? Math.floor(random() * 3) : -1
      });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      speed = Math.min(220, 96 + scroll * 0.008);
      scroll += speed * dt;
      game.score = Math.floor(scroll / 12) + ore * 15;

      var pressed = input.up || input.down || input.fire;
      if (!pressed) held = false;
      else if (!held) {
        held = true;
        if (input.up) cart.lane = Math.min(2, cart.lane + 1);
        else if (input.down) cart.lane = Math.max(0, cart.lane - 1);
        else if (input.fire && !cart.air) { cart.vy = -170; cart.air = true; }
      }

      var target = railY(cart.lane);
      if (cart.air) {
        cart.vy += 460 * dt;
        cart.y += cart.vy * dt;
        if (cart.y >= target) { cart.y = target; cart.air = false; cart.vy = 0; }
      } else {
        cart.y += (target - cart.y) * Math.min(1, dt * 12);
      }

      for (var i = rails.length - 1; i >= 0; i -= 1) {
        var seg = rails[i];
        seg.x -= speed * dt;
        if (seg.x < -80) {
          rails.splice(i, 1);
          addSegment(rails.reduce(function (a, s) { return Math.max(a, s.x); }, W) + 70);
          continue;
        }
        if (seg.x > 66 || seg.x < 34) continue;
        if (seg.ore === cart.lane && !cart.air) {
          seg.ore = -1;
          ore += 1;
          bits.burst(50, cart.y, 5, { colour: "brass", speed: 50, life: 0.4 });
        }
        var onMissingRail = !seg.lanes[cart.lane] && !cart.air;
        var intoRock = seg.rock === cart.lane && !cart.air;
        if ((onMissingRail || intoRock || (seg.gap && !cart.air)) && hurt <= 0) {
          hurt = 1;
          game.lives -= 1;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(50, cart.y, 16, { colour: "danger", speed: 80, life: 0.5 });
          if (game.lives <= 0) { game.over = true; return; }
          cart.lane = 0;
        }
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 24, W, 24);
      rails.forEach(function (seg) {
        for (var lane = 0; lane < 3; lane += 1) {
          if (!seg.lanes[lane] || seg.gap) continue;
          ctx.fillStyle = ink.wall[1];
          ctx.fillRect(Math.round(seg.x), railY(lane) + 10, 64, 4);
          ctx.fillStyle = ink.wall[2];
          for (var t = 0; t < 4; t += 1) ctx.fillRect(Math.round(seg.x) + t * 16, railY(lane) + 14, 5, 5);
        }
        if (seg.ore >= 0) {
          ctx.fillStyle = ink.brass;
          ctx.fillRect(Math.round(seg.x) + 26, railY(seg.ore) - 6, 10, 10);
        }
        if (seg.rock >= 0) {
          ctx.fillStyle = ink.danger;
          ctx.fillRect(Math.round(seg.x) + 22, railY(seg.rock) - 4, 18, 14);
        }
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(40, Math.round(cart.y) - 12, 22, 16);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(44, Math.round(cart.y) - 8, 14, 8);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ORE " + ore, 4, 12);
      ctx.fillText(Math.floor(scroll / 12) + "m", W - 56, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(scroll / 12) + "m · ORE " + ore; };
    return game;
  }

  /* ---- 85. THRUST BUDGET -------------------------------------------------

     A jetpack corridor. Fuel burns while you hold and refills on the ground,
     so the run is a series of decisions about how much altitude a gap is
     actually worth. */
  function cabinetThrustBudget(random) {
    var game = { id: "thrust-budget", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var flyer, hazards, scroll, speed, fuel, hurt, coins, collected;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      flyer = { y: H - 40, vy: 0 };
      hazards = [];
      coins = [];
      scroll = 0;
      speed = 88;
      fuel = 100;
      collected = 0;
      hurt = 0;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 8; i += 1) seedAt(200 + i * 90);
    };

    function seedAt(x) {
      hazards.push({ x: x, y: 30 + random() * (H - 90), h: 20 + random() * 40, moving: random() < 0.3, dir: 1 });
      if (random() < 0.7) coins.push({ x: x + 40, y: 30 + random() * (H - 80), got: false });
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      hurt = Math.max(0, hurt - dt);
      speed = Math.min(190, 88 + scroll * 0.007);
      scroll += speed * dt;
      game.score = Math.floor(scroll / 14) + collected * 20;

      var thrusting = (input.fire || input.up) && fuel > 0;
      if (thrusting) {
        flyer.vy -= 320 * dt;
        fuel -= 26 * dt;
        bits.burst(46, flyer.y + 10, 2, { colour: "brass", speed: 40, angle: Math.PI / 2, spread: 0.5, life: 0.25 });
      }
      flyer.vy += 240 * dt;
      flyer.y += flyer.vy * dt;
      if (flyer.y > H - 26) {
        flyer.y = H - 26;
        flyer.vy = 0;
        fuel = Math.min(100, fuel + 42 * dt);
      }
      if (flyer.y < 10) { flyer.y = 10; flyer.vy = 0; }

      hazards.forEach(function (hz) {
        hz.x -= speed * dt;
        if (hz.moving) {
          hz.y += hz.dir * 40 * dt;
          if (hz.y < 20 || hz.y > H - 60) hz.dir *= -1;
        }
        if (hz.x > 62 || hz.x < 30) return;
        if (flyer.y > hz.y && flyer.y < hz.y + hz.h && hurt <= 0) {
          hurt = 1;
          game.lives -= 1;
          fx.shake(12);
          fx.flash("danger", 1);
          bits.burst(46, flyer.y, 16, { colour: "danger", speed: 80, life: 0.5 });
          if (game.lives <= 0) { game.over = true; }
        }
      });
      hazards = hazards.filter(function (hz) { return hz.x > -30; });

      coins.forEach(function (c) {
        c.x -= speed * dt;
        if (c.got || Math.abs(c.x - 46) > 10 || Math.abs(c.y - flyer.y) > 10) return;
        c.got = true;
        collected += 1;
        bits.burst(c.x, c.y, 5, { colour: "brass", speed: 50, life: 0.35 });
      });
      coins = coins.filter(function (c) { return c.x > -20 && !c.got; });

      while (hazards.length < 8) {
        seedAt(hazards.reduce(function (a, h) { return Math.max(a, h.x); }, W) + 80 + random() * 40);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, scroll * 0.4, 22);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(0, H - 18, W, 18);
      ctx.fillRect(0, 0, W, 8);
      hazards.forEach(function (hz) {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(Math.round(hz.x), Math.round(hz.y), 12, hz.h);
      });
      coins.forEach(function (c) {
        ctx.fillStyle = ink.brass;
        ctx.fillRect(Math.round(c.x) - 4, Math.round(c.y) - 4, 8, 8);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = hurt > 0 && Math.floor(hurt * 12) % 2 === 0 ? ink.danger : ink.verify;
      ctx.fillRect(40, Math.round(flyer.y) - 8, 12, 18);
      drawBar(ctx, ink, 4, 4, 70, 5, fuel / 100, fuel > 25 ? "brass" : "danger");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("FUEL", 4, 18);
      ctx.fillText(Math.floor(scroll / 14) + "m", W - 56, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(scroll / 14) + "m · FUEL " + Math.round(fuel); };
    return game;
  }

  /* ---- 86. WALL JUMP -----------------------------------------------------

     A vertical shaft with two walls and one verb. You stick where you land
     and push off; the shaft rises on its own, so hesitating is the loss
     condition rather than a pause. */
  function cabinetWallJump(random) {
    var game = { id: "wall-jump", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var climber, spikes, camera, rise, held, height;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      climber = { x: 40, y: H - 60, vx: 0, vy: 0, side: -1, stuck: true };
      spikes = [];
      camera = 0;
      rise = 8;
      height = 0;
      held = false;
      fx.reset();
      bits.clear();
      for (var i = 0; i < 8; i += 1) spikes.push({ y: -i * 90 - 60, side: random() < 0.5 ? -1 : 1, h: 30 });
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      rise = Math.min(46, 8 + height * 0.012);
      camera += rise * dt;

      if (input.fire || input.up) {
        if (!held && climber.stuck) {
          held = true;
          climber.stuck = false;
          climber.vx = -climber.side * 140;
          climber.vy = -180;
          bits.burst(climber.x, climber.y, 5, { colour: "verify", speed: 40, life: 0.3 });
        }
      } else held = false;

      if (!climber.stuck) {
        climber.vy += 420 * dt;
        climber.x += climber.vx * dt;
        climber.y += climber.vy * dt;
        if (climber.x <= 30) { climber.x = 30; climber.side = -1; climber.stuck = true; climber.vx = climber.vy = 0; }
        if (climber.x >= W - 30) { climber.x = W - 30; climber.side = 1; climber.stuck = true; climber.vx = climber.vy = 0; }
      } else {
        climber.y += 22 * dt;
      }

      var screenY = climber.y + camera;
      height = Math.max(height, -climber.y + H);
      game.score = Math.floor(height / 8);

      var died = screenY > H + 10;
      spikes.forEach(function (s) {
        var sy = s.y + camera;
        if (sy < -40 || sy > H + 40) return;
        var onSide = (s.side < 0 && climber.x < 44) || (s.side > 0 && climber.x > W - 44);
        if (onSide && screenY > sy && screenY < sy + s.h) died = true;
      });

      if (died) {
        game.lives -= 1;
        fx.shake(12);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        climber.y = -camera + H - 60;
        climber.x = 40;
        climber.side = -1;
        climber.stuck = true;
        climber.vx = climber.vy = 0;
      }

      spikes.forEach(function (s) {
        if (s.y + camera <= H + 60) return;
        s.y -= 8 * 90;
        s.side = random() < 0.5 ? -1 : 1;
      });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(0, 0, 26, H);
      ctx.fillRect(W - 26, 0, 26, H);
      spikes.forEach(function (s) {
        var sy = s.y + camera;
        if (sy < -40 || sy > H) return;
        ctx.fillStyle = ink.danger;
        if (s.side < 0) ctx.fillRect(26, sy, 12, s.h);
        else ctx.fillRect(W - 38, sy, 12, s.h);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = climber.stuck ? ink.verify : ink.bright;
      ctx.fillRect(Math.round(climber.x) - 6, Math.round(climber.y + camera) - 8, 12, 16);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText(Math.floor(height / 8) + "m", 32, 14);
      fx.end(ctx, ink);
    };

    game.hud = function () { return Math.floor(height / 8) + "m UP"; };
    return game;
  }

  /* ---- 87. COLD SLOPE ----------------------------------------------------

     Downhill through gates. Miss a gate and the run is disqualified at the
     bottom rather than stopped in the middle, so the pressure is cumulative
     and you always find out too late. */
  function cabinetColdSlope(random) {
    var game = { id: "cold-slope", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var skier, gates, scroll, speed, missedGates, run, trees;

    function seed() {
      gates = [];
      trees = [];
      for (var i = 0; i < 12; i += 1) {
        gates.push({ y: -i * 120 - 100, x: 50 + random() * (W - 100), w: 46, passed: false, missed: false });
      }
      for (var t = 0; t < 24; t += 1) {
        trees.push({ x: random() * W, y: -random() * 1400 });
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      skier = { x: W / 2, vx: 0 };
      scroll = 0;
      speed = 110;
      missedGates = 0;
      run = 1;
      fx.reset();
      bits.clear();
      seed();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      speed = Math.min(260, 110 + scroll * 0.01);
      scroll += speed * dt;

      var steer = (input.right ? 1 : 0) - (input.left ? 1 : 0);
      skier.vx += steer * 260 * dt;
      skier.vx *= 0.92;
      skier.x = clamp(skier.x + skier.vx * dt, 8, W - 8);

      gates.forEach(function (g) {
        var gy = g.y + scroll;
        if (g.passed || gy < H - 60) return;
        g.passed = true;
        var through = Math.abs(skier.x - g.x) < g.w / 2;
        if (through) {
          game.score += 25;
          bits.burst(g.x, H - 60, 5, { colour: "verify", speed: 40, life: 0.3 });
        } else {
          g.missed = true;
          missedGates += 1;
          fx.shake(6);
          fx.flash("danger", 0.6);
        }
      });

      trees.forEach(function (t) {
        var ty = t.y + scroll;
        if (ty < H - 40 || ty > H - 20) return;
        if (Math.abs(t.x - skier.x) > 8) return;
        t.y -= 3000;
        game.lives -= 1;
        fx.shake(12);
        fx.flash("danger", 1);
        bits.burst(skier.x, H - 30, 16, { colour: "danger", speed: 80, life: 0.5 });
        if (game.lives <= 0) game.over = true;
      });

      if (gates.every(function (g) { return g.passed; })) {
        if (missedGates > 2) {
          game.lives -= 1;
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        } else {
          game.score += 180 - missedGates * 40;
        }
        run += 1;
        missedGates = 0;
        scroll = 0;
        seed();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      trees.forEach(function (t) {
        var ty = t.y + scroll;
        if (ty < -20 || ty > H) return;
        ctx.fillStyle = ink.wall[1];
        ctx.fillRect(Math.round(t.x) - 5, Math.round(ty) - 12, 10, 14);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(t.x) - 2, Math.round(ty) + 1, 4, 5);
      });
      gates.forEach(function (g) {
        var gy = g.y + scroll;
        if (gy < -10 || gy > H) return;
        ctx.fillStyle = g.missed ? ink.danger : g.passed ? ink.grid : ink.brass;
        ctx.fillRect(g.x - g.w / 2, gy, 4, 16);
        ctx.fillRect(g.x + g.w / 2 - 4, gy, 4, 16);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(skier.x) - 4, H - 40, 8, 14);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = missedGates > 1 ? ink.danger : ink.dim;
      ctx.fillText("MISSED " + missedGates + "/3", 4, 12);
      ctx.fillStyle = ink.dim;
      ctx.fillText("RUN " + run, W - 56, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "RUN " + run + " · MISSED " + missedGates; };
    return game;
  }

  /* ---- 88. HANDOFF -------------------------------------------------------

     Parkour as a rhythm of leaps between moving platforms. Each platform is a
     service you are briefly standing on; they move, and the one you are on is
     always about to stop being available. */
  function cabinetHandoff(random) {
    var game = { id: "handoff", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var runner, plats, camera, held, chain;

    function makePlat(x) {
      return {
        x: x,
        y: 90 + random() * (H - 130),
        w: 44 + random() * 34,
        drift: (random() - 0.5) * 40,
        life: 2.4 + random() * 2
      };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      runner = { x: 60, y: 140, vx: 0, vy: 0, on: null };
      plats = [];
      for (var i = 0; i < 6; i += 1) plats.push(makePlat(40 + i * 80));
      runner.on = plats[0];
      camera = 0;
      chain = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      plats.forEach(function (p) {
        p.y += p.drift * dt;
        if (p.y < 60 || p.y > H - 30) p.drift *= -1;
        if (p.life > 0) p.life -= dt;
      });

      if (input.fire || input.up) {
        if (!held && runner.on) {
          held = true;
          runner.vx = 132;
          runner.vy = -180;
          runner.on = null;
          chain += 1;
          game.score += 10 * Math.min(6, chain);
          bits.burst(runner.x, runner.y, 5, { colour: "verify", speed: 40, life: 0.3 });
        }
      } else held = false;

      if (!runner.on) {
        runner.vy += 400 * dt;
        runner.x += runner.vx * dt;
        runner.y += runner.vy * dt;
        plats.forEach(function (p) {
          if (runner.vy < 0 || p.life <= 0) return;
          if (runner.x < p.x - 4 || runner.x > p.x + p.w + 4) return;
          if (runner.y < p.y - 6 || runner.y > p.y + 10) return;
          runner.on = p;
          runner.y = p.y - 6;
          runner.vx = runner.vy = 0;
          fx.shake(1);
        });
      } else {
        runner.x = runner.on.x + runner.on.w / 2;
        runner.y = runner.on.y - 6;
        if (runner.on.life <= 0) runner.on = null;
      }

      camera = Math.max(camera, runner.x - 90);
      // Recycle platforms behind the camera into fresh ones ahead.
      plats.forEach(function (p, i) {
        if (p.x + p.w > camera - 30) return;
        plats[i] = makePlat(plats.reduce(function (a, k) { return Math.max(a, k.x); }, camera) + 70 + random() * 50);
      });

      if (runner.y > H + 12 || runner.x < camera - 20) {
        game.lives -= 1;
        chain = 0;
        fx.shake(12);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        var safe = plats.reduce(function (a, p) { return p.x > camera + 10 && (!a || p.x < a.x) ? p : a; }, null) || plats[0];
        runner.on = safe;
        runner.x = safe.x + safe.w / 2;
        runner.y = safe.y - 6;
        runner.vx = runner.vy = 0;
        safe.life = Math.max(safe.life, 2);
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, camera, H * 0.6);
      fx.begin(ctx);
      plats.forEach(function (p) {
        var x = p.x - camera;
        if (x < -90 || x > W) return;
        ctx.fillStyle = p.life > 1 ? ink.verify : p.life > 0 ? ink.brass : ink.grid;
        ctx.fillRect(Math.round(x), Math.round(p.y), Math.round(p.w), 6);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(runner.x - camera) - 4, Math.round(runner.y) - 12, 9, 13);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("CHAIN " + chain, 4, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "CHAIN " + chain; };
    return game;
  }

  /* ---- 89. BUBBLE QUEUE --------------------------------------------------

     Fire matching entries into a descending queue; three of a kind detach.
     The ceiling comes down on a timer, so a shot that does not clear anything
     costs you twice. */
  function cabinetBubbleQueue(random) {
    var COLS = 10;
    var CELL = 24;
    var OX = (W - COLS * CELL) / 2;
    var KINDS = 4;

    var game = { id: "bubble-queue", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var rows, shot, aim, loaded, dropTimer, level, held;

    function seed() {
      rows = [];
      for (var r = 0; r < 4; r += 1) {
        var row = [];
        for (var c = 0; c < COLS; c += 1) row.push(1 + Math.floor(random() * KINDS));
        rows.push(row);
      }
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      aim = 0;
      shot = null;
      loaded = 1 + Math.floor(random() * KINDS);
      dropTimer = 10;
      level = 1;
      held = false;
      fx.reset();
      bits.clear();
      seed();
    };

    function clearFrom(r, c, kind, seen) {
      if (r < 0 || c < 0 || r >= rows.length || c >= COLS) return [];
      var key = r + ":" + c;
      if (seen[key] || rows[r][c] !== kind) return [];
      seen[key] = true;
      var out = [{ r: r, c: c }];
      return out
        .concat(clearFrom(r - 1, c, kind, seen))
        .concat(clearFrom(r + 1, c, kind, seen))
        .concat(clearFrom(r, c - 1, kind, seen))
        .concat(clearFrom(r, c + 1, kind, seen));
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      dropTimer -= dt;

      if (dropTimer <= 0) {
        dropTimer = Math.max(4, 10 - level * 0.4);
        var row = [];
        for (var c = 0; c < COLS; c += 1) row.push(1 + Math.floor(random() * KINDS));
        rows.unshift(row);
        if (rows.length > 7) {
          game.lives -= 1;
          fx.shake(11);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
          seed();
        }
      }

      if (input.left) aim -= 1.8 * dt;
      if (input.right) aim += 1.8 * dt;
      aim = clamp(aim, -1.1, 1.1);

      if (shot) {
        shot.x += Math.sin(aim) * 220 * dt;
        shot.y -= 220 * dt;
        if (shot.x < OX || shot.x > OX + COLS * CELL) shot.x = clamp(shot.x, OX, OX + COLS * CELL);
        var landRow = Math.floor((shot.y - 20) / CELL);
        if (shot.y <= 20 + rows.length * CELL) {
          var col = clamp(Math.floor((shot.x - OX) / CELL), 0, COLS - 1);
          var r = clamp(landRow, 0, rows.length - 1);
          rows[r][col] = shot.kind;
          var group = clearFrom(r, col, shot.kind, {});
          if (group.length >= 3) {
            group.forEach(function (g) {
              rows[g.r][g.c] = 0;
              bits.burst(OX + g.c * CELL + CELL / 2, 20 + g.r * CELL + CELL / 2, 4, { colour: "verify", speed: 50, life: 0.35 });
            });
            game.score += group.length * 15;
            dropTimer = Math.min(dropTimer + group.length * 0.5, 12);
            fx.flash("verify", 0.6);
            if (rows.every(function (rw) { return rw.every(function (v) { return !v; }); })) {
              level += 1;
              game.score += 150;
              seed();
            }
          } else {
            dropTimer -= 1;
          }
          shot = null;
          loaded = 1 + Math.floor(random() * KINDS);
        }
        return;
      }

      if (input.fire && !held) {
        held = true;
        shot = { x: W / 2, y: H - 30, kind: loaded };
      } else if (!input.fire) held = false;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      var colours = ["grid", "verify", "brass", "danger", "bright"];
      rows.forEach(function (row, r) {
        row.forEach(function (v, c) {
          if (!v) return;
          ctx.fillStyle = ink[colours[v]] || ink.grid;
          ctx.fillRect(OX + c * CELL + 2, 20 + r * CELL + 2, CELL - 4, CELL - 4);
          ctx.fillStyle = ink.bg;
          ctx.fillRect(OX + c * CELL + 7, 20 + r * CELL + 7, CELL - 14, CELL - 14);
        });
      });
      bits.draw(ctx, ink);
      if (shot) {
        ctx.fillStyle = ink[colours[shot.kind]];
        ctx.fillRect(Math.round(shot.x) - 8, Math.round(shot.y) - 8, 16, 16);
      }
      ctx.fillStyle = ink[colours[loaded]];
      ctx.fillRect(W / 2 - 8, H - 26, 16, 16);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(W / 2 + Math.sin(aim) * 26) - 2, Math.round(H - 26 - Math.cos(aim) * 26) - 2, 4, 4);
      drawBar(ctx, ink, 6, 6, W - 12, 4, dropTimer / 10, dropTimer < 3 ? "danger" : "brass");
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 6, H - 6);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + rows.length + " ROWS"; };
    return game;
  }

  /* ---- 90. SCOPE MATCH ---------------------------------------------------

     Fall through rotating rings, and only the sector matching your current
     scope lets you pass. Your colour changes at every gate, so the read has
     to be done again each time. */
  function cabinetScopeMatch(random) {
    var COLOURS = ["verify", "brass", "danger", "bright"];

    var game = { id: "scope-match", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(50);
    var ball, rings, scroll, speed, mine, held, passed;

    function makeRing(y) {
      return { y: y, spin: (random() < 0.5 ? -1 : 1) * (0.9 + random() * 1.1), angle: random() * Math.PI * 2, passed: false };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      ball = { y: 40, vy: 0 };
      rings = [];
      for (var i = 0; i < 5; i += 1) rings.push(makeRing(160 + i * 150));
      scroll = 0;
      speed = 44;
      mine = 0;
      passed = 0;
      held = false;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      speed = Math.min(120, 44 + passed * 2.4);

      // One button: hold to slow the descent, release to drop.
      ball.vy += (input.fire || input.up ? -260 : 300) * dt;
      ball.vy = clamp(ball.vy, -90, 150);
      scroll += Math.max(12, speed + ball.vy * 0.4) * dt;

      rings.forEach(function (r) {
        r.angle += r.spin * dt;
        var sy = r.y - scroll;
        if (r.passed || sy > 90) return;
        r.passed = true;
        passed += 1;
        // The gate at the ball's x (centre) is the sector currently facing up.
        var sector = Math.floor((((r.angle % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2)) / (Math.PI / 2)) % 4;
        if (sector === mine) {
          game.score += 30;
          mine = Math.floor(random() * COLOURS.length);
          fx.flash("verify", 0.6);
          bits.burst(W / 2, 90, 8, { colour: COLOURS[mine], speed: 60, life: 0.4 });
        } else {
          game.lives -= 1;
          fx.shake(11);
          fx.flash("danger", 1);
          if (game.lives <= 0) { game.over = true; return; }
        }
      });

      while (rings.filter(function (r) { return !r.passed; }).length < 4) {
        var deepest = rings.reduce(function (a, r) { return Math.max(a, r.y); }, scroll);
        rings.push(makeRing(deepest + 140 + random() * 60));
      }
      rings = rings.filter(function (r) { return r.y - scroll > -60; });
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      rings.forEach(function (r) {
        var sy = r.y - scroll;
        if (sy < -40 || sy > H + 40) return;
        for (var s = 0; s < 4; s += 1) {
          var a = r.angle + (s * Math.PI) / 2;
          ctx.fillStyle = ink[COLOURS[s]];
          ctx.fillRect(
            Math.round(W / 2 + Math.cos(a) * 52) - 9,
            Math.round(sy + Math.sin(a) * 18) - 5,
            18, 10
          );
        }
        ctx.fillStyle = ink.grid;
        ctx.fillRect(W / 2 - 60, Math.round(sy) - 1, 120, 2);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink[COLOURS[mine]];
      ctx.fillRect(W / 2 - 7, 84, 14, 14);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(W / 2 - 3, 88, 6, 6);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("GATES " + passed, 4, 12);
      centreText(ctx, ink, "MATCH THE SECTOR TO YOUR SCOPE", H - 8, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "GATES " + passed; };
    return game;
  }

  /* ---- 91. REORDER -------------------------------------------------------

     A sliding tile puzzle over a sequence that has to end up in order. One
     blank, four moves, and an optimal solution you will not find. */
  function cabinetReorder(random) {
    var SIZE = 4;
    var CELL = 40;
    var OX = (W - SIZE * CELL) / 2;
    var OY = 46;

    var game = { id: "reorder", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var tiles, blank, moves, level, held, message, messageAge;

    function shuffle() {
      tiles = [];
      for (var i = 0; i < SIZE * SIZE; i += 1) tiles.push(i);
      blank = SIZE * SIZE - 1;
      // Shuffled by legal moves, so every board is solvable.
      var steps = 30 + level * 12;
      for (var s = 0; s < steps; s += 1) {
        var options = neighbours(blank);
        var pick = options[Math.floor(random() * options.length)];
        tiles[blank] = tiles[pick];
        tiles[pick] = SIZE * SIZE - 1;
        blank = pick;
      }
      moves = 0;
    }

    function neighbours(i) {
      var out = [];
      var x = i % SIZE, y = Math.floor(i / SIZE);
      if (x > 0) out.push(i - 1);
      if (x < SIZE - 1) out.push(i + 1);
      if (y > 0) out.push(i - SIZE);
      if (y < SIZE - 1) out.push(i + SIZE);
      return out;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      shuffle();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      // Arrows move a tile INTO the blank, which is the convention every
      // player of this genre already has in their hands.
      var from = -1;
      if (input.left) from = blank % SIZE < SIZE - 1 ? blank + 1 : -1;
      else if (input.right) from = blank % SIZE > 0 ? blank - 1 : -1;
      else if (input.up) from = blank + SIZE < SIZE * SIZE ? blank + SIZE : -1;
      else if (input.down) from = blank - SIZE >= 0 ? blank - SIZE : -1;
      if (from < 0) { fx.shake(2); return; }

      tiles[blank] = tiles[from];
      tiles[from] = SIZE * SIZE - 1;
      blank = from;
      moves += 1;

      var solvedIt = tiles.every(function (v, i) { return v === i; });
      if (solvedIt) {
        level += 1;
        game.score += Math.max(60, 400 - moves * 4);
        message = "IN ORDER — LEVEL " + level;
        messageAge = 0;
        fx.flash("verify", 1);
        shuffle();
      } else if (moves > 160 + level * 30) {
        game.lives -= 1;
        message = "OUT OF PATIENCE";
        messageAge = 0;
        fx.shake(8);
        if (game.lives <= 0) { game.over = true; return; }
        shuffle();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "PUT THE SEQUENCE BACK IN ORDER", 24, "dim", 7);
      tiles.forEach(function (v, i) {
        if (v === SIZE * SIZE - 1) return;
        var x = OX + (i % SIZE) * CELL;
        var y = OY + Math.floor(i / SIZE) * CELL;
        ctx.fillStyle = v === i ? ink.verify : ink.wall[1];
        ctx.fillRect(x + 2, y + 2, CELL - 5, CELL - 5);
        ctx.font = '10px "IBM Plex Mono", monospace';
        ctx.textAlign = "center";
        ctx.fillStyle = ink.bg;
        ctx.fillText(String(v + 1), x + CELL / 2 - 1, y + CELL / 2);
        ctx.textAlign = "left";
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("MOVES " + moves, 6, 16);
      ctx.fillText("LEVEL " + level, W - 66, 16);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · MOVES " + moves; };
    return game;
  }

  /* ---- 92. BREAKER -------------------------------------------------------

     A paddle, a ball, and a wall of deprecated endpoints. The twist over the
     original is that some bricks respawn: a deprecation nobody finished is
     back next release. */
  function cabinetBreaker(random) {
    var COLS = 10;
    var ROWS = 5;
    var BW = 30;
    var BH = 14;
    var OX = (W - COLS * BW) / 2;

    var game = { id: "breaker", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var paddle, ball, bricks, level, combo;

    function seed() {
      bricks = [];
      for (var r = 0; r < ROWS; r += 1) {
        for (var c = 0; c < COLS; c += 1) {
          bricks.push({
            x: OX + c * BW,
            y: 30 + r * BH,
            alive: true,
            zombie: random() < Math.min(0.28, 0.06 + level * 0.03),
            timer: 0
          });
        }
      }
    }

    function serve() {
      ball = { x: W / 2, y: H - 40, vx: (random() - 0.5) * 90, vy: -150 };
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      paddle = { x: W / 2, w: 52 };
      level = 1;
      combo = 0;
      fx.reset();
      bits.clear();
      seed();
      serve();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);

      var speed = 168 * dt;
      if (input.left) paddle.x -= speed;
      if (input.right) paddle.x += speed;
      if (input.pointerX != null) paddle.x = input.pointerX;
      paddle.x = clamp(paddle.x, paddle.w / 2, W - paddle.w / 2);

      ball.x += ball.vx * dt;
      ball.y += ball.vy * dt;
      if (ball.x < 4) { ball.x = 4; ball.vx = Math.abs(ball.vx); }
      if (ball.x > W - 4) { ball.x = W - 4; ball.vx = -Math.abs(ball.vx); }
      if (ball.y < 14) { ball.y = 14; ball.vy = Math.abs(ball.vy); }

      if (ball.y > H - 26 && ball.y < H - 18 && Math.abs(ball.x - paddle.x) < paddle.w / 2 + 3) {
        ball.vy = -Math.abs(ball.vy);
        ball.vx += (ball.x - paddle.x) * 3.2;
        ball.vx = clamp(ball.vx, -190, 190);
        combo = 0;
        fx.shake(1);
      }

      bricks.forEach(function (b) {
        if (b.zombie && !b.alive) {
          b.timer -= dt;
          if (b.timer <= 0) b.alive = true;
        }
        if (!b.alive) return;
        if (ball.x < b.x || ball.x > b.x + BW - 2) return;
        if (ball.y < b.y || ball.y > b.y + BH - 2) return;
        b.alive = false;
        b.timer = 5 + random() * 4;
        ball.vy = -ball.vy;
        combo += 1;
        game.score += 10 * Math.min(6, combo);
        bits.burst(b.x + BW / 2, b.y + BH / 2, 5, { colour: b.zombie ? "danger" : "brass", speed: 50, life: 0.35 });
        fx.shake(2);
      });

      if (bricks.every(function (b) { return !b.alive; })) {
        level += 1;
        game.score += 200;
        fx.flash("verify", 1);
        seed();
        serve();
      }

      if (ball.y > H + 6) {
        game.lives -= 1;
        combo = 0;
        fx.shake(10);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        serve();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      bricks.forEach(function (b) {
        if (!b.alive) {
          if (!b.zombie) return;
          ctx.fillStyle = ink.grid;
          ctx.fillRect(b.x + 1, b.y + 1, BW - 4, 2);
          return;
        }
        ctx.fillStyle = b.zombie ? ink.danger : ink.wall[1];
        ctx.fillRect(b.x + 1, b.y + 1, BW - 4, BH - 4);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(b.x + 3, b.y + 3, BW - 8, BH - 8);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(Math.round(paddle.x - paddle.w / 2), H - 20, paddle.w, 6);
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(ball.x) - 3, Math.round(ball.y) - 3, 6, 6);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 4, 12);
      if (combo > 1) ctx.fillText("×" + combo, W - 34, 12);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · ×" + Math.max(1, combo); };
    return game;
  }

  /* ---- 93. BANK SHOT -----------------------------------------------------

     Pocket the target balls off the cushions. Aim and power, one shot, and a
     cue ball that keeps whatever momentum you gave it — the whole game is
     thinking one bounce further than feels necessary. */
  function cabinetBankShot(random) {
    var game = { id: "bank-shot", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var cue, balls, pockets, aim, power, charging, shots, rack, message, messageAge;

    function setup() {
      cue = { x: 60, y: H / 2, vx: 0, vy: 0 };
      balls = [];
      for (var i = 0; i < 3 + Math.min(3, rack); i += 1) {
        balls.push({ x: 150 + random() * 130, y: 40 + random() * (H - 80), vx: 0, vy: 0, in: false });
      }
      shots = 3 + Math.floor(rack / 2);
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      rack = 1;
      aim = 0;
      power = 0.4;
      charging = false;
      messageAge = 9;
      pockets = [
        { x: 8, y: 20 }, { x: W - 8, y: 20 }, { x: 8, y: H - 12 },
        { x: W - 8, y: H - 12 }, { x: W / 2, y: 20 }, { x: W / 2, y: H - 12 }
      ];
      fx.reset();
      bits.clear();
      setup();
    };

    function moving() {
      if (Math.hypot(cue.vx, cue.vy) > 4) return true;
      return balls.some(function (b) { return !b.in && Math.hypot(b.vx, b.vy) > 4; });
    }

    function physics(b, dt) {
      b.x += b.vx * dt;
      b.y += b.vy * dt;
      b.vx *= 0.988;
      b.vy *= 0.988;
      if (b.x < 8) { b.x = 8; b.vx = Math.abs(b.vx); }
      if (b.x > W - 8) { b.x = W - 8; b.vx = -Math.abs(b.vx); }
      if (b.y < 20) { b.y = 20; b.vy = Math.abs(b.vy); }
      if (b.y > H - 12) { b.y = H - 12; b.vy = -Math.abs(b.vy); }
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (moving()) {
        physics(cue, dt);
        balls.forEach(function (b) {
          if (b.in) return;
          physics(b, dt);
          var d = Math.hypot(b.x - cue.x, b.y - cue.y);
          if (d < 11 && d > 0.01) {
            var nx = (b.x - cue.x) / d, ny = (b.y - cue.y) / d;
            var speed = Math.hypot(cue.vx, cue.vy);
            b.vx += nx * speed * 0.8;
            b.vy += ny * speed * 0.8;
            cue.vx *= 0.35;
            cue.vy *= 0.35;
            fx.shake(2);
          }
          pockets.forEach(function (p) {
            if (b.in || Math.hypot(p.x - b.x, p.y - b.y) > 11) return;
            b.in = true;
            game.score += 60;
            bits.burst(p.x, p.y, 8, { colour: "verify", speed: 50, life: 0.4 });
            fx.flash("verify", 0.6);
          });
        });
        pockets.forEach(function (p) {
          if (Math.hypot(p.x - cue.x, p.y - cue.y) > 11) return;
          cue.x = 60; cue.y = H / 2; cue.vx = cue.vy = 0;
          message = "SCRATCH";
          messageAge = 0;
          fx.shake(8);
        });

        if (!moving()) {
          if (balls.every(function (b) { return b.in; })) {
            rack += 1;
            game.score += 200;
            message = "RACK CLEARED";
            messageAge = 0;
            setup();
          } else if (shots <= 0) {
            game.lives -= 1;
            message = "OUT OF SHOTS";
            messageAge = 0;
            if (game.lives <= 0) { game.over = true; return; }
            setup();
          }
        }
        return;
      }

      if (input.left) aim -= 2 * dt;
      if (input.right) aim += 2 * dt;
      if (input.fire) {
        charging = true;
        power = Math.min(1, power + dt * 0.85);
      } else if (charging) {
        charging = false;
        shots -= 1;
        cue.vx = Math.cos(aim) * (80 + power * 260);
        cue.vy = Math.sin(aim) * (80 + power * 260);
        power = 0.2;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(4, 16, W - 8, H - 28);
      pockets.forEach(function (p) {
        ctx.fillStyle = ink.bg;
        ctx.fillRect(p.x - 7, p.y - 7, 14, 14);
      });
      balls.forEach(function (b) {
        if (b.in) return;
        ctx.fillStyle = ink.brass;
        ctx.fillRect(Math.round(b.x) - 5, Math.round(b.y) - 5, 10, 10);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(b.x) - 2, Math.round(b.y) - 2, 4, 4);
      });
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(Math.round(cue.x) - 5, Math.round(cue.y) - 5, 10, 10);
      if (!moving()) {
        ctx.fillStyle = ink.bright;
        for (var t = 1; t < 5; t += 1) {
          ctx.fillRect(Math.round(cue.x + Math.cos(aim) * t * 10) - 1, Math.round(cue.y + Math.sin(aim) * t * 10) - 1, 3, 3);
        }
        drawBar(ctx, ink, 8, 6, 80, 5, power, "brass");
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("SHOTS " + shots, W - 96, 12);
      ctx.fillText("RACK " + rack, W - 44, 12);
      if (messageAge < 2) centreText(ctx, ink, message, H - 4, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "RACK " + rack + " · SHOTS " + shots; };
    return game;
  }

  /* ---- 94. DRAW WEIGHT ---------------------------------------------------

     Archery with wind and a drop. Hold to draw, release to loose, and the
     arrow does not travel in a straight line no matter how much you would
     like it to. */
  function cabinetDrawWeight(random) {
    var game = { id: "draw-weight", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var draw, angle, arrow, target, wind, round, hits, held, message, messageAge;

    function place() {
      target = { x: 190 + random() * (W - 220), y: 40 + random() * (H - 100), r: Math.max(9, 22 - round) };
      wind = (random() - 0.5) * 46;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      draw = 0;
      angle = -0.35;
      arrow = null;
      round = 1;
      hits = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
      place();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (arrow) {
        arrow.vy += 130 * dt;
        arrow.vx += wind * dt;
        arrow.x += arrow.vx * dt;
        arrow.y += arrow.vy * dt;
        var d = Math.hypot(arrow.x - target.x, arrow.y - target.y);
        if (d < target.r) {
          var inner = d < target.r * 0.4;
          hits += 1;
          round += 1;
          game.score += inner ? 150 : 70;
          message = inner ? "GOLD" : "ON THE TARGET";
          messageAge = 0;
          fx.flash("verify", 1);
          bits.burst(target.x, target.y, 12, { colour: "verify", speed: 60, life: 0.5 });
          arrow = null;
          place();
          return;
        }
        if (arrow.x > W + 10 || arrow.y > H + 10) {
          arrow = null;
          game.lives -= 1;
          message = "MISSED";
          messageAge = 0;
          fx.shake(7);
          if (game.lives <= 0) { game.over = true; return; }
          place();
        }
        return;
      }

      if (input.up) angle -= 1.1 * dt;
      if (input.down) angle += 1.1 * dt;
      angle = clamp(angle, -1.2, 0.5);
      if (input.fire) {
        held = true;
        draw = Math.min(1, draw + dt * 0.8);
      } else if (held) {
        held = false;
        arrow = {
          x: 26, y: H - 60,
          vx: Math.cos(angle) * (90 + draw * 200),
          vy: Math.sin(angle) * (90 + draw * 200)
        };
        draw = 0;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawParallaxBands(ctx, ink, 0, H * 0.72);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, H - 22, W, 22);
      var r = target.r;
      ctx.fillStyle = ink.danger;
      ctx.fillRect(target.x - r, target.y - r, r * 2, r * 2);
      ctx.fillStyle = ink.ink;
      ctx.fillRect(target.x - r * 0.6, target.y - r * 0.6, r * 1.2, r * 1.2);
      ctx.fillStyle = ink.brass;
      ctx.fillRect(target.x - r * 0.25, target.y - r * 0.25, r * 0.5, r * 0.5);
      bits.draw(ctx, ink);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(20, H - 74, 10, 22);
      if (arrow) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(arrow.x) - 4, Math.round(arrow.y) - 1, 9, 2);
      } else {
        ctx.fillStyle = ink.bright;
        for (var t = 1; t < 4; t += 1) {
          ctx.fillRect(Math.round(26 + Math.cos(angle) * t * 12) - 1, Math.round(H - 60 + Math.sin(angle) * t * 12) - 1, 3, 3);
        }
        drawBar(ctx, ink, 8, 8, 74, 6, draw, draw > 0.85 ? "danger" : "brass");
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("WIND " + (wind > 0 ? "→" : "←") + Math.abs(wind).toFixed(0), 8, 26);
      ctx.fillText("ROUND " + round, W - 70, 14);
      if (messageAge < 2) centreText(ctx, ink, message, 40, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · HITS " + hits; };
    return game;
  }

  /* ---- 95. CROSSFADE -----------------------------------------------------

     Two decks drifting out of sync and one fader. Hold the mix inside the
     window and the crowd meter climbs; let either deck run away and it drops.
     Beat-matching is a control-loop problem, which is why it belongs here. */
  function cabinetCrossfade(random) {
    var game = { id: "crossfade", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var deckA, deckB, fader, crowd, set, phaseTimer;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      deckA = { phase: 0, rate: 1 };
      deckB = { phase: 0.4, rate: 1.06 };
      fader = 0.5;
      crowd = 0.6;
      set = 1;
      phaseTimer = 0;
      fx.reset();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      phaseTimer += dt;

      if (input.left) fader -= 0.9 * dt;
      if (input.right) fader += 0.9 * dt;
      fader = clamp(fader, 0, 1);

      deckA.phase = (deckA.phase + deckA.rate * dt) % 1;
      deckB.phase = (deckB.phase + deckB.rate * dt) % 1;

      // Nudge the trailing deck with the action key: the one real verb.
      if (input.fire) deckB.phase = (deckB.phase + 0.55 * dt) % 1;

      var drift = Math.abs(deckA.phase - deckB.phase);
      drift = Math.min(drift, 1 - drift);
      var wantFader = 0.5 + (deckA.phase - deckB.phase);
      var faderOff = Math.abs(fader - clamp(wantFader, 0, 1));

      if (drift < 0.08 && faderOff < 0.25) {
        crowd = Math.min(1, crowd + 0.24 * dt);
        game.score += Math.round(30 * dt);
      } else {
        crowd -= (0.16 + set * 0.02) * dt;
      }

      if (crowd <= 0) {
        crowd = 0.55;
        game.lives -= 1;
        fx.shake(10);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
      }
      if (phaseTimer > 22) {
        phaseTimer = 0;
        set += 1;
        deckB.rate = 1 + (random() - 0.3) * 0.14;
        fx.flash("verify", 0.8);
        game.score += 120;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "SET " + set + " — HOLD THE MIX", 20, "dim");

      [deckA, deckB].forEach(function (deck, i) {
        var y = 46 + i * 62;
        ctx.fillStyle = ink.wall[3];
        ctx.fillRect(20, y, W - 40, 44);
        for (var b = 0; b < 16; b += 1) {
          var lit = Math.floor(deck.phase * 16) === b;
          ctx.fillStyle = lit ? (i ? ink.brass : ink.verify) : ink.wall[2];
          ctx.fillRect(26 + b * ((W - 52) / 16), y + 8, 12, 28);
        }
        ctx.font = '7px "IBM Plex Mono", monospace';
        ctx.fillStyle = ink.dim;
        ctx.fillText(i ? "DECK B" : "DECK A", 24, y + 42);
      });

      drawBar(ctx, ink, 30, 178, W - 60, 10, fader, "ink");
      ctx.fillStyle = ink.bright;
      ctx.fillRect(Math.round(30 + fader * (W - 60)) - 2, 174, 5, 18);
      centreText(ctx, ink, "← → FADER · SPACE NUDGE DECK B", 204, "dim", 7);
      drawBar(ctx, ink, 30, 214, W - 60, 8, crowd, crowd > 0.35 ? "verify" : "danger");
      centreText(ctx, ink, "CROWD", 232, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "SET " + set + " · CROWD " + Math.round(crowd * 100) + "%"; };
    return game;
  }

  /* ---- 96. ORBITAL -------------------------------------------------------

     Slingshot a probe past a gravity well onto a target orbit. You get one
     burn and the well does the rest, so the whole skill is predicting a curve
     you cannot draw. */
  function cabinetOrbital(random) {
    var game = { id: "orbital", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(60);
    var probe, well, goal, aim, power, charging, launches, level, trail, message, messageAge;

    function build() {
      well = { x: W / 2 + (random() - 0.5) * 50, y: H / 2, mass: 5200 + level * 500 };
      goal = { x: W - 40 - random() * 30, y: 30 + random() * (H - 60), r: Math.max(9, 18 - level) };
      probe = null;
      aim = -0.5;
      power = 0.4;
      trail = [];
      launches = 3;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      messageAge = 9;
      fx.reset();
      bits.clear();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (probe) {
        var dx = well.x - probe.x, dy = well.y - probe.y;
        var d2 = Math.max(80, dx * dx + dy * dy);
        var d = Math.sqrt(d2);
        probe.vx += (dx / d) * (well.mass / d2) * dt * 60;
        probe.vy += (dy / d) * (well.mass / d2) * dt * 60;
        probe.x += probe.vx * dt;
        probe.y += probe.vy * dt;
        trail.push({ x: probe.x, y: probe.y });
        if (trail.length > 80) trail.shift();

        if (d < 14) {
          bits.burst(probe.x, probe.y, 16, { colour: "danger", speed: 70, life: 0.6 });
          fx.shake(9);
          probe = null;
          message = "FELL INTO THE WELL";
          messageAge = 0;
        } else if (Math.hypot(goal.x - probe.x, goal.y - probe.y) < goal.r) {
          level += 1;
          game.score += 180 + launches * 40;
          message = "ORBIT ACHIEVED";
          messageAge = 0;
          fx.flash("verify", 1);
          bits.burst(goal.x, goal.y, 16, { colour: "verify", speed: 60, life: 0.6 });
          build();
          return;
        } else if (probe.x < -30 || probe.x > W + 30 || probe.y < -30 || probe.y > H + 30) {
          probe = null;
          message = "LOST TO SPACE";
          messageAge = 0;
        }
        if (!probe && launches <= 0) {
          game.lives -= 1;
          if (game.lives <= 0) { game.over = true; return; }
          build();
        }
        return;
      }

      if (input.up) aim -= 1.4 * dt;
      if (input.down) aim += 1.4 * dt;
      if (input.fire) {
        charging = true;
        power = Math.min(1, power + dt * 0.8);
      } else if (charging) {
        charging = false;
        launches -= 1;
        trail = [];
        probe = { x: 24, y: H / 2, vx: Math.cos(aim) * (60 + power * 130), vy: Math.sin(aim) * (60 + power * 130) };
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      drawStarfield(ctx, ink, 0, 40);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(well.x - 12, well.y - 12, 24, 24);
      ctx.fillStyle = ink.danger;
      ctx.fillRect(well.x - 7, well.y - 7, 14, 14);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(well.x - 3, well.y - 3, 6, 6);
      ctx.fillStyle = ink.verify;
      ctx.fillRect(goal.x - goal.r, goal.y - goal.r, goal.r * 2, goal.r * 2);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(goal.x - goal.r + 3, goal.y - goal.r + 3, goal.r * 2 - 6, goal.r * 2 - 6);
      trail.forEach(function (t, i) {
        if (i % 2) return;
        ctx.fillStyle = ink.grid;
        ctx.fillRect(Math.round(t.x), Math.round(t.y), 1, 1);
      });
      bits.draw(ctx, ink);
      if (probe) {
        ctx.fillStyle = ink.bright;
        ctx.fillRect(Math.round(probe.x) - 2, Math.round(probe.y) - 2, 5, 5);
      } else {
        ctx.fillStyle = ink.verify;
        ctx.fillRect(18, H / 2 - 6, 12, 12);
        ctx.fillStyle = ink.bright;
        for (var t = 1; t < 4; t += 1) {
          ctx.fillRect(Math.round(24 + Math.cos(aim) * t * 12) - 1, Math.round(H / 2 + Math.sin(aim) * t * 12) - 1, 3, 3);
        }
        drawBar(ctx, ink, 8, 8, 74, 5, power, "brass");
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("BURNS " + launches, 8, 24);
      ctx.fillText("LEVEL " + level, W - 66, 14);
      if (messageAge < 2) centreText(ctx, ink, message, 40, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · BURNS " + launches; };
    return game;
  }

  /* ---- 97. SPOT KICK -----------------------------------------------------

     A penalty shootout, alternating: you take one, then you keep one. Both
     halves are a read — the keeper commits early and so do you. */
  function cabinetSpotKick(random) {
    var game = { id: "spot-kick", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var bits = makeParticles(40);
    var phase, pick, keeper, ball, scored, conceded, round, timer, held, message, messageAge;

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      phase = "shoot";
      pick = 1;
      keeper = 1;
      ball = null;
      scored = 0;
      conceded = 0;
      round = 1;
      timer = 0;
      held = false;
      messageAge = 9;
      fx.reset();
      bits.clear();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      bits.update(dt);
      messageAge += dt;

      if (ball) {
        timer -= dt;
        if (timer > 0) return;
        var saved = keeper === ball.lane;
        if (ball.mine) {
          if (saved) {
            message = "SAVED";
            fx.shake(7);
          } else {
            scored += 1;
            game.score += 90;
            message = "SCORED";
            fx.flash("verify", 0.9);
            bits.burst(60 + ball.lane * 100, 70, 10, { colour: "verify", speed: 60, life: 0.5 });
          }
        } else {
          if (saved) {
            game.score += 70;
            message = "YOU SAVED IT";
            fx.flash("verify", 0.9);
          } else {
            conceded += 1;
            message = "CONCEDED";
            fx.shake(9);
            if (conceded - scored > 2) {
              game.lives -= 1;
              scored = conceded = 0;
              if (game.lives <= 0) { game.over = true; return; }
            }
          }
        }
        messageAge = 0;
        ball = null;
        round += 1;
        phase = phase === "shoot" ? "keep" : "shoot";
        return;
      }

      var pressed = input.left || input.right || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;
      if (input.left) pick = Math.max(0, pick - 1);
      else if (input.right) pick = Math.min(2, pick + 1);
      else if (input.fire) {
        if (phase === "shoot") {
          keeper = Math.floor(random() * 3);
          ball = { lane: pick, mine: true };
        } else {
          keeper = pick;
          ball = { lane: Math.floor(random() * 3), mine: false };
        }
        timer = 0.65;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.wall[3];
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      ctx.fillStyle = ink.wall[2];
      ctx.fillRect(30, 30, W - 60, 80);
      ctx.fillStyle = ink.bg;
      ctx.fillRect(34, 34, W - 68, 72);
      for (var l = 0; l < 3; l += 1) {
        var x = 40 + l * 82;
        ctx.fillStyle = l === pick ? ink.bright : ink.grid;
        ctx.fillRect(x, 40, 74, 60);
      }
      if (ball) {
        ctx.fillStyle = ink.ink;
        ctx.fillRect(40 + ball.lane * 82 + 32, 62, 12, 12);
        ctx.fillStyle = ink.danger;
        ctx.fillRect(40 + keeper * 82 + 22, 52, 32, 34);
      } else {
        ctx.fillStyle = ink.danger;
        ctx.fillRect(W / 2 - 16, 52, 32, 34);
      }
      bits.draw(ctx, ink);
      centreText(ctx, ink, phase === "shoot" ? "YOUR KICK — PICK A CORNER" : "THEIR KICK — PICK A DIVE", 132, "dim");
      centreText(ctx, ink, "YOU " + scored + "   THEM " + conceded, 158, "ink", 16);
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUND " + round, 6, 14);
      if (messageAge < 1.6) centreText(ctx, ink, message, 186, "bright");
      centreText(ctx, ink, "← → choose · SPACE commit", H - 10, "dim", 7);
      fx.end(ctx, ink);
    };

    game.hud = function () { return "YOU " + scored + " · THEM " + conceded; };
    return game;
  }

  /* ---- 98. HANDSHAKE -----------------------------------------------------

     Repeat a pattern of long and short presses. The protocol grows by one
     symbol each round and the timing window narrows — a handshake that gets
     stricter the longer it goes, which is a fair description of most of them. */
  function cabinetHandshake(random) {
    var game = { id: "handshake", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var pattern, step, phase, timer, pressTime, held, round, message, messageAge;

    function extend() {
      pattern.push(random() < 0.5 ? "short" : "long");
      phase = "show";
      step = 0;
      timer = 0.5;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      pattern = [];
      round = 1;
      held = false;
      pressTime = 0;
      messageAge = 9;
      fx.reset();
      extend();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;

      if (phase === "show") {
        timer -= dt;
        if (timer > 0) return;
        step += 1;
        if (step > pattern.length) {
          phase = "input";
          step = 0;
        } else {
          timer = pattern[step - 1] === "long" ? 0.62 : 0.26;
          timer += 0.22;
        }
        return;
      }

      if (input.fire) {
        held = true;
        pressTime += dt;
        return;
      }
      if (!held) return;
      held = false;
      var symbol = pressTime > 0.32 ? "long" : "short";
      pressTime = 0;

      if (symbol === pattern[step]) {
        step += 1;
        game.score += 6;
        if (step >= pattern.length) {
          round += 1;
          game.score += 40 + pattern.length * 8;
          message = "HANDSHAKE COMPLETE (" + pattern.length + ")";
          messageAge = 0;
          fx.flash("verify", 0.9);
          extend();
        }
      } else {
        game.lives -= 1;
        message = "PROTOCOL MISMATCH AT " + (step + 1);
        messageAge = 0;
        fx.shake(9);
        fx.flash("danger", 1);
        if (game.lives <= 0) { game.over = true; return; }
        pattern = [];
        extend();
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, phase === "show" ? "LISTEN" : "REPEAT IT", 30, phase === "show" ? "brass" : "verify", 16);

      pattern.forEach(function (sym, i) {
        var x = 20 + (i % 12) * 24;
        var y = 70 + Math.floor(i / 12) * 30;
        var lit = phase === "show" ? step - 1 === i : i < step;
        ctx.fillStyle = lit ? ink.bright : ink.wall[2];
        ctx.fillRect(x, y, sym === "long" ? 20 : 9, 14);
      });

      if (phase === "input") {
        drawBar(ctx, ink, 40, H - 50, W - 80, 8, Math.min(1, pressTime / 0.6), pressTime > 0.32 ? "verify" : "brass");
        centreText(ctx, ink, pressTime > 0.32 ? "LONG" : "SHORT", H - 60, "dim", 7);
      }
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("ROUND " + round, 6, 14);
      ctx.fillText("LENGTH " + pattern.length, W - 92, 14);
      if (messageAge < 2) centreText(ctx, ink, message, H - 14, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "ROUND " + round + " · LEN " + pattern.length; };
    return game;
  }

  /* ---- 99. UNTANGLE ------------------------------------------------------

     Drag nodes until no dependency crosses another. Every graph here is
     planar by construction, so a clean layout always exists — the difficulty
     is entirely in seeing it. */
  function cabinetUntangle(random) {
    var game = { id: "untangle", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var nodes, edges, cursor, grabbed, level, held, clock, message, messageAge;

    function build() {
      var count = 4 + Math.min(4, level);
      nodes = [];
      for (var i = 0; i < count; i += 1) {
        var a = (i / count) * Math.PI * 2;
        // Laid out on a circle (planar), then scrambled into random slots.
        nodes.push({ x: W / 2 + Math.cos(a) * 80, y: H / 2 + Math.sin(a) * 62 });
      }
      edges = [];
      for (var e = 0; e < count; e += 1) edges.push([e, (e + 1) % count]);
      for (var extra = 0; extra < Math.min(3, level); extra += 1) {
        var a2 = Math.floor(random() * count);
        var b2 = (a2 + 2 + Math.floor(random() * (count - 3))) % count;
        edges.push([a2, b2]);
      }
      // Scramble positions: the graph stays planar, the drawing does not.
      nodes.forEach(function (n) {
        n.x = 30 + random() * (W - 60);
        n.y = 40 + random() * (H - 80);
      });
      cursor = 0;
      grabbed = false;
      clock = 40 + level * 4;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    function crosses() {
      function ccw(a, b, c) { return (c.y - a.y) * (b.x - a.x) > (b.y - a.y) * (c.x - a.x); }
      var count = 0;
      for (var i = 0; i < edges.length; i += 1) {
        for (var j = i + 1; j < edges.length; j += 1) {
          var e1 = edges[i], e2 = edges[j];
          if (e1[0] === e2[0] || e1[0] === e2[1] || e1[1] === e2[0] || e1[1] === e2[1]) continue;
          var a = nodes[e1[0]], b = nodes[e1[1]], c = nodes[e2[0]], d = nodes[e2[1]];
          if (ccw(a, c, d) !== ccw(b, c, d) && ccw(a, b, c) !== ccw(a, b, d)) count += 1;
        }
      }
      return count;
    }

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      clock -= dt;
      if (clock <= 0) {
        game.lives -= 1;
        message = "STILL TANGLED";
        messageAge = 0;
        fx.shake(9);
        if (game.lives <= 0) { game.over = true; return; }
        build();
        return;
      }

      if (grabbed) {
        var speed = 84 * dt;
        if (input.left) nodes[cursor].x -= speed;
        if (input.right) nodes[cursor].x += speed;
        if (input.up) nodes[cursor].y -= speed;
        if (input.down) nodes[cursor].y += speed;
        nodes[cursor].x = clamp(nodes[cursor].x, 10, W - 10);
        nodes[cursor].y = clamp(nodes[cursor].y, 20, H - 16);
      }

      if (input.fire) {
        if (!held) {
          held = true;
          grabbed = !grabbed;
          if (!grabbed && crosses() === 0) {
            level += 1;
            game.score += 150 + Math.round(clock * 3);
            message = "PLANAR — LEVEL " + level;
            messageAge = 0;
            fx.flash("verify", 1);
            build();
          }
        }
        return;
      }
      held = false;

      if (!grabbed) {
        var pressed = input.left || input.right || input.up || input.down;
        if (!pressed) return;
        if (input.left || input.up) cursor = (cursor + nodes.length - 1) % nodes.length;
        else cursor = (cursor + 1) % nodes.length;
        held = true;
      }
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      var bad = crosses();
      edges.forEach(function (e) {
        var a = nodes[e[0]], b = nodes[e[1]];
        var steps = Math.max(2, Math.round(Math.hypot(b.x - a.x, b.y - a.y) / 4));
        for (var s = 0; s <= steps; s += 1) {
          ctx.fillStyle = ink.wall[1];
          ctx.fillRect(Math.round(a.x + ((b.x - a.x) * s) / steps), Math.round(a.y + ((b.y - a.y) * s) / steps), 2, 2);
        }
      });
      nodes.forEach(function (n, i) {
        ctx.fillStyle = i === cursor ? (grabbed ? ink.bright : ink.brass) : ink.verify;
        ctx.fillRect(Math.round(n.x) - 6, Math.round(n.y) - 6, 12, 12);
        ctx.fillStyle = ink.bg;
        ctx.fillRect(Math.round(n.x) - 3, Math.round(n.y) - 3, 6, 6);
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = bad ? ink.danger : ink.verify;
      ctx.fillText("CROSSINGS " + bad, 6, 14);
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, W - 66, 14);
      drawBar(ctx, ink, 6, 18, W - 12, 3, clock / (40 + level * 4), clock < 10 ? "danger" : "brass");
      centreText(ctx, ink, grabbed ? "MOVING — SPACE TO DROP" : "SPACE TO GRAB", H - 8, "dim", 7);
      if (messageAge < 2) centreText(ctx, ink, message, H - 20, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · " + crosses() + " CROSSINGS"; };
    return game;
  }

  /* ---- 100. GRID PROOF ---------------------------------------------------

     A nonogram: the numbers along each row and column say how many cells are
     filled and in what runs, and the grid is the only proof. Deduction with
     no hidden information and no luck, which makes it the right cabinet to
     end a hundred on. */
  function cabinetGridProof(random) {
    var SIZE = 5;
    var CELL = 26;
    var OX = (W - SIZE * CELL) / 2 + 24;
    var OY = 70;

    var game = { id: "grid-proof", score: 0, lives: 3, over: false };
    var fx = makeFx();
    var answer, marks, cursor, rowHints, colHints, level, held, mistakes, message, messageAge;

    function hintsFor(values) {
      var out = [];
      var run = 0;
      values.forEach(function (v) {
        if (v) run += 1;
        else if (run) { out.push(run); run = 0; }
      });
      if (run) out.push(run);
      return out.length ? out : [0];
    }

    function build() {
      answer = [];
      for (var i = 0; i < SIZE * SIZE; i += 1) answer.push(random() < 0.55 ? 1 : 0);
      marks = answer.map(function () { return 0; });
      rowHints = [];
      colHints = [];
      for (var r = 0; r < SIZE; r += 1) rowHints.push(hintsFor(answer.slice(r * SIZE, r * SIZE + SIZE)));
      for (var c = 0; c < SIZE; c += 1) {
        var col = [];
        for (var y = 0; y < SIZE; y += 1) col.push(answer[y * SIZE + c]);
        colHints.push(hintsFor(col));
      }
      cursor = 0;
      mistakes = 0;
    }

    game.reset = function () {
      game.score = 0;
      game.lives = 3;
      game.over = false;
      level = 1;
      held = false;
      messageAge = 9;
      fx.reset();
      build();
    };

    game.update = function (dt, input) {
      if (game.over) return;
      fx.update(dt);
      messageAge += dt;
      var pressed = input.left || input.right || input.up || input.down || input.fire;
      if (!pressed) { held = false; return; }
      if (held) return;
      held = true;

      if (input.fire) {
        if (marks[cursor]) { marks[cursor] = 0; return; }
        marks[cursor] = 1;
        if (!answer[cursor]) {
          mistakes += 1;
          fx.shake(7);
          fx.flash("danger", 0.8);
          if (mistakes >= 3) {
            game.lives -= 1;
            message = "TOO MANY WRONG CELLS";
            messageAge = 0;
            if (game.lives <= 0) { game.over = true; return; }
            build();
            return;
          }
        } else {
          game.score += 8;
        }
        var solvedIt = answer.every(function (v, i) { return v === marks[i]; });
        if (solvedIt) {
          level += 1;
          game.score += 200;
          message = "PROVEN — LEVEL " + level;
          messageAge = 0;
          fx.flash("verify", 1);
          build();
        }
        return;
      }
      if (input.left) cursor = (cursor + marks.length - 1) % marks.length;
      else if (input.right) cursor = (cursor + 1) % marks.length;
      else if (input.up) cursor = (cursor + marks.length - SIZE) % marks.length;
      else if (input.down) cursor = (cursor + SIZE) % marks.length;
    };

    game.draw = function (ctx, ink) {
      ctx.fillStyle = ink.bg;
      ctx.fillRect(0, 0, W, H);
      fx.begin(ctx);
      centreText(ctx, ink, "THE GRID IS THE ONLY PROOF", 24, "dim");
      ctx.font = '7px "IBM Plex Mono", monospace';
      rowHints.forEach(function (hint, r) {
        ctx.textAlign = "right";
        ctx.fillStyle = ink.dim;
        ctx.fillText(hint.join(" "), OX - 6, OY + r * CELL + CELL / 2);
        ctx.textAlign = "left";
      });
      colHints.forEach(function (hint, c) {
        ctx.textAlign = "center";
        ctx.fillStyle = ink.dim;
        hint.forEach(function (n, i) {
          ctx.fillText(String(n), OX + c * CELL + CELL / 2, OY - 22 + i * 9);
        });
        ctx.textAlign = "left";
      });
      marks.forEach(function (m, i) {
        var x = OX + (i % SIZE) * CELL;
        var y = OY + Math.floor(i / SIZE) * CELL;
        ctx.fillStyle = m ? (answer[i] ? ink.verify : ink.danger) : ink.wall[3];
        ctx.fillRect(x + 1, y + 1, CELL - 3, CELL - 3);
        if (i === cursor) {
          ctx.fillStyle = ink.bright;
          ctx.fillRect(x, y, CELL - 1, 2);
          ctx.fillRect(x, y + CELL - 3, CELL - 1, 2);
          ctx.fillRect(x, y, 2, CELL - 1);
          ctx.fillRect(x + CELL - 3, y, 2, CELL - 1);
        }
      });
      ctx.font = '8px "IBM Plex Mono", monospace';
      ctx.fillStyle = ink.dim;
      ctx.fillText("LEVEL " + level, 6, 14);
      ctx.fillStyle = mistakes ? ink.danger : ink.dim;
      ctx.fillText("WRONG " + mistakes + "/3", W - 78, 14);
      if (messageAge < 2) centreText(ctx, ink, message, H - 10, "bright");
      fx.end(ctx, ink);
    };

    game.hud = function () { return "LEVEL " + level + " · WRONG " + mistakes + "/3"; };
    return game;
  }

  var CABINETS = [
    {
      id: "blast-radius",
      name: "BLAST RADIUS",
      genre: "FPS",
      family: "SHOOT",
      tagline: "Walk the permit boundary. Nothing unscoped reaches the tool.",
      controls: "← → turn · ↑ ↓ walk · SPACE fire",
      pad: "dpad+fire",
      make: cabinetBlastRadius
    },
    {
      id: "hold-the-line",
      name: "HOLD THE LINE",
      genre: "FPS",
      family: "SHOOT",
      tagline: "Four corridors. One post. Do not let a call through.",
      controls: "← → turn · ↑ ↓ step · SPACE fire",
      pad: "dpad+fire",
      make: cabinetHoldTheLine
    },
    {
      id: "scope-creep",
      name: "SCOPE CREEP",
      genre: "SHOOTER",
      family: "SHOOT",
      tagline: "Deny the permissions before they reach production.",
      controls: "← → move · SPACE deny",
      pad: "lr+fire",
      make: cabinetScopeCreep
    },
    {
      id: "retry-storm",
      name: "RETRY STORM",
      genre: "SHOOTER",
      family: "SHOOT",
      tagline: "Shoot a duplicate and now there are two of them.",
      controls: "← → turn · ↑ thrust · SPACE fire",
      pad: "dpad+fire",
      make: cabinetRetryStorm
    },
    {
      id: "token-bucket",
      name: "TOKEN BUCKET",
      genre: "ARCADE",
      family: "TIMING",
      tagline: "Drain the burst. The bucket refills. Forever.",
      controls: "← → limiter · SPACE serve",
      pad: "lr+fire",
      make: cabinetTokenBucket
    },
    {
      id: "double-spend",
      name: "DOUBLE SPEND",
      genre: "ARCADE",
      family: "TIMING",
      tagline: "Retry a finished call with the same key and payload: original receipt, no new dispatch.",
      controls: "arrows step",
      pad: "dpad",
      make: cabinetDoubleSpend
    },
    {
      id: "backpressure",
      name: "BACKPRESSURE",
      genre: "PUZZLE",
      family: "PUZZLE",
      tagline: "Work arrives faster than it drains. Stack it anyway.",
      controls: "← → shift · SPACE rotate · ↓ drop",
      pad: "dpad+fire",
      make: cabinetBackpressure
    },
    {
      id: "append-only",
      name: "APPEND-ONLY",
      genre: "PUZZLE",
      family: "PUZZLE",
      tagline: "The ledger grows. It may never cross itself.",
      controls: "arrows steer the write head",
      pad: "dpad",
      make: cabinetAppendOnly
    },
    {
      id: "nonce-burn",
      name: "NONCE BURN",
      genre: "REFLEX",
      family: "TIMING",
      tagline: "Each one is good once, and not for long.",
      controls: "arrows move · SPACE burn",
      pad: "dpad+fire",
      make: cabinetNonceBurn
    },
    {
      id: "key-rotation",
      name: "KEY ROTATION",
      genre: "MAZE",
      family: "ACTION",
      tagline: "Collect the new key before the revocations reach you.",
      controls: "arrows move",
      pad: "dpad",
      make: cabinetKeyRotation
    },
    {
      id: "tail-latency",
      name: "TAIL LATENCY",
      genre: "RUNNER",
      family: "RUN",
      tagline: "p50 is fine. p50 is not what your users get.",
      controls: "↑ jump · ↓ duck",
      pad: "ud",
      make: cabinetTailLatency
    },
    {
      id: "race-condition",
      name: "RACE CONDITION",
      genre: "DUEL",
      family: "ACTION",
      tagline: "Rally against an agent that does not blink.",
      controls: "↑ ↓ move · first to 7",
      pad: "ud",
      make: cabinetRaceCondition
    },
    {
      id: "countersign",
      name: "COUNTERSIGN",
      genre: "FPS",
      family: "SHOOT",
      tagline: "The fuse is a TTL. Hold the key down and it never fires.",
      controls: "← → turn · ↑ ↓ walk · SPACE fire / hold to countersign",
      pad: "dpad+fire",
      make: cabinetCountersign
    },
    {
      id: "happy-path",
      name: "HAPPY PATH",
      genre: "PLATFORM",
      family: "RUN",
      tagline: "Run right. The gaps are the cases nobody wrote.",
      controls: "← → run · SPACE jump",
      pad: "lr+fire",
      make: cabinetHappyPath
    },
    {
      id: "least-privilege",
      name: "LEAST PRIVILEGE",
      genre: "ADVENTURE",
      family: "QUEST",
      tagline: "Nine rooms, two permits, one vault. Take only what opens it.",
      controls: "arrows move · SPACE revoke",
      pad: "dpad+fire",
      make: cabinetLeastPrivilege
    },
    {
      id: "escalation",
      name: "ESCALATION",
      genre: "RPG",
      family: "QUEST",
      tagline: "Turn-based. The committee has more hit points than you.",
      controls: "↑ ↓ choose · SPACE commit",
      pad: "ud+fire",
      make: cabinetEscalation
    },
    {
      id: "arbitration",
      name: "ARBITRATION",
      genre: "FIGHTING",
      family: "ACTION",
      tagline: "Two parties, one dispute, best of three.",
      controls: "← → step · ↑ jump · ↓ block · SPACE strike",
      pad: "dpad+fire",
      make: cabinetArbitration
    },
    {
      id: "throughput",
      name: "THROUGHPUT",
      genre: "RACING",
      family: "DRIVE",
      tagline: "Off the road you are not crashed, only throttled.",
      controls: "← → steer · ↑ throttle · ↓ brake",
      pad: "dpad",
      make: cabinetThroughput
    },
    {
      id: "side-channel",
      name: "SIDE CHANNEL",
      genre: "STEALTH",
      family: "ACTION",
      tagline: "One observation is nothing. Enough of them is the leak.",
      controls: "arrows move · SPACE hold to crouch",
      pad: "dpad+fire",
      make: cabinetSideChannel
    },
    {
      id: "cold-storage",
      name: "COLD STORAGE",
      genre: "HORROR",
      family: "ACTION",
      tagline: "Three shards, one torch, and not enough rounds.",
      controls: "arrows move · SPACE fire",
      pad: "dpad+fire",
      make: cabinetColdStorage
    },
    {
      id: "block-store",
      name: "BLOCK STORE",
      genre: "SANDBOX",
      family: "MANAGE",
      tagline: "Dig all day. Be behind a wall when the scan runs.",
      controls: "arrows move · SPACE mine or place",
      pad: "dpad+fire",
      make: cabinetBlockStore
    },
    {
      id: "last-quorum",
      name: "LAST QUORUM",
      genre: "ROYALE",
      family: "ACTION",
      tagline: "The quorum shrinks. Outside it you are only partitioned.",
      controls: "arrows move · SPACE fire",
      pad: "dpad+fire",
      make: cabinetLastQuorum
    },
    {
      id: "heartbeat",
      name: "HEARTBEAT",
      genre: "RHYTHM",
      family: "TIMING",
      tagline: "Four lanes of health checks. Answer them on the beat.",
      controls: "← ↑ ↓ → hit the lanes",
      pad: "lanes",
      make: cabinetHeartbeat
    },
    {
      id: "brute-force",
      name: "BRUTE FORCE",
      genre: "BRAWLER",
      family: "ACTION",
      tagline: "No cleverness, just volume. Do not get surrounded.",
      controls: "arrows move · SPACE strike",
      pad: "dpad+fire",
      make: cabinetBruteForce
    },
    {
      id: "catalog",
      name: "CATALOG",
      genre: "COLLECT",
      family: "MANAGE",
      tagline: "Every tool in the estate that nobody wrote down.",
      controls: "arrows move · SPACE bind the scope",
      pad: "dpad+fire",
      make: cabinetCatalog
    },
    {
      id: "rate-gate",
      name: "RATE GATE",
      genre: "ONE-TOUCH",
      family: "RUN",
      tagline: "One button. Endless rate limits, each with a gap in it.",
      controls: "SPACE flap",
      pad: "tap",
      make: cabinetRateGate
    },
    {
      id: "merge-ledger",
      name: "MERGE LEDGER",
      genre: "MERGE",
      family: "PUZZLE",
      tagline: "Equal entries combine. The board fills either way.",
      controls: "arrows slide",
      pad: "dpad",
      make: cabinetMergeLedger
    },
    {
      id: "tap-forge",
      name: "TAP FORGE",
      genre: "IDLE",
      family: "MANAGE",
      tagline: "Mint by hand until something mints for you.",
      controls: "↑ ↓ choose · SPACE mint or buy",
      pad: "ud+fire",
      make: cabinetTapForge
    },
    {
      id: "drop-stack",
      name: "DROP STACK",
      genre: "STACKER",
      family: "MANAGE",
      tagline: "Whatever hangs over the edge shears off.",
      controls: "SPACE drop",
      pad: "tap",
      make: cabinetDropStack
    },
    {
      id: "slice-queue",
      name: "SLICE QUEUE",
      genre: "SLICE",
      family: "TIMING",
      tagline: "Cut the signed ones. Let the unsigned ones fall.",
      controls: "arrows move the blade · SPACE swing",
      pad: "dpad+fire",
      make: cabinetSliceQueue
    },
    {
      id: "lane-hop",
      name: "LANE HOP",
      genre: "CROSSING",
      family: "RUN",
      tagline: "Every lane is a service with its own traffic.",
      controls: "arrows hop",
      pad: "dpad",
      make: cabinetLaneHop
    },
    {
      id: "swarm",
      name: "SWARM",
      genre: "SURVIVOR",
      family: "ACTION",
      tagline: "You never press fire. The aura is always running.",
      controls: "arrows move",
      pad: "dpad",
      make: cabinetSwarm
    },
    {
      id: "bullet-ledger",
      name: "BULLET LEDGER",
      genre: "BULLET HELL",
      family: "SHOOT",
      tagline: "Your hitbox is one pixel. Grazing pays.",
      controls: "arrows move · SPACE focus",
      pad: "dpad+fire",
      make: cabinetBulletLedger
    },
    {
      id: "chokepoint",
      name: "CHOKEPOINT",
      genre: "TOWER DEF",
      family: "SHOOT",
      tagline: "You cannot cover everything. Pick where the path bends.",
      controls: "arrows move · SPACE place a denial",
      pad: "dpad+fire",
      make: cabinetChokepoint
    },
    {
      id: "deck-of-scopes",
      name: "DECK OF SCOPES",
      genre: "DECKBUILD",
      family: "QUEST",
      tagline: "Five scopes, three energy, and it already told you what it hits for.",
      controls: "← → pick · SPACE play · ↑ ↓ end turn",
      pad: "dpad+fire",
      make: cabinetDeckOfScopes
    },
    {
      id: "backstop",
      name: "BACKSTOP",
      genre: "DEFENCE",
      family: "SHOOT",
      tagline: "One gun, and it overheats. Knowing when to stop is the game.",
      controls: "← → aim · SPACE fire",
      pad: "lr+fire",
      make: cabinetBackstop
    },
    {
      id: "cold-move",
      name: "COLD MOVE",
      genre: "SOKOBAN",
      family: "PUZZLE",
      tagline: "Push each record onto its pad. There is no undo.",
      controls: "arrows push",
      pad: "dpad",
      make: cabinetColdMove
    },
    {
      id: "quorum-flip",
      name: "QUORUM FLIP",
      genre: "LIGHTS",
      family: "PUZZLE",
      tagline: "Every local fix has non-local consequences.",
      controls: "arrows move · SPACE flip",
      pad: "dpad+fire",
      make: cabinetQuorumFlip
    },
    {
      id: "idempotency",
      name: "IDEMPOTENCY",
      genre: "MEMORY",
      family: "PUZZLE",
      tagline: "Every request has one matching twin.",
      controls: "arrows move · SPACE turn",
      pad: "dpad+fire",
      make: cabinetIdempotency
    },
    {
      id: "replay-order",
      name: "REPLAY ORDER",
      genre: "SEQUENCE",
      family: "TIMING",
      tagline: "A log is only useful if you can replay it in order.",
      controls: "← ↑ ↓ → repeat the log",
      pad: "lanes",
      make: cabinetReplayOrder
    },
    {
      id: "match-policy",
      name: "MATCH POLICY",
      genre: "MATCH-3",
      family: "PUZZLE",
      tagline: "Line up three rules and watch the file collapse.",
      controls: "arrows move · SPACE swap",
      pad: "dpad+fire",
      make: cabinetMatchPolicy
    },
    {
      id: "cold-start",
      name: "COLD START",
      genre: "CLIMB",
      family: "RUN",
      tagline: "Throttle, brake, and land the flips you started.",
      controls: "↑ → throttle · ↓ ← brake",
      pad: "dpad",
      make: cabinetColdStart
    },
    {
      id: "swish-rate",
      name: "SWISH RATE",
      genre: "HOOPS",
      family: "SPORT",
      tagline: "A power meter, a moving hoop, a shot clock.",
      controls: "SPACE shoot on the meter",
      pad: "tap",
      make: cabinetSwishRate
    },
    {
      id: "siege-budget",
      name: "SIEGE BUDGET",
      genre: "SIEGE",
      family: "SHOOT",
      tagline: "Lob a payload at the legacy stack. Topple it.",
      controls: "↑ ↓ angle · SPACE hold to charge",
      pad: "ud+fire",
      make: cabinetSiegeBudget
    },
    {
      id: "tilt",
      name: "TILT",
      genre: "PINBALL",
      family: "SPORT",
      tagline: "Bumpers pay. The drain does not.",
      controls: "← → flippers · SPACE both",
      pad: "lr+fire",
      make: cabinetTilt
    },
    {
      id: "soft-landing",
      name: "SOFT LANDING",
      genre: "LANDER",
      family: "DRIVE",
      tagline: "Read the gauge, not the ground.",
      controls: "↑ thrust · ← → drift",
      pad: "dpad",
      make: cabinetSoftLanding
    },
    {
      id: "ticket-queue",
      name: "TICKET QUEUE",
      genre: "SERVICE",
      family: "MANAGE",
      tagline: "Routing under load, with a patience bar on every caller.",
      controls: "← → walk · SPACE pick up or drop",
      pad: "lr+fire",
      make: cabinetTicketQueue
    },
    {
      id: "route-table",
      name: "ROUTE TABLE",
      genre: "TRAFFIC",
      family: "MANAGE",
      tagline: "Two calls met where the table said they would not.",
      controls: "← → pick a junction · SPACE divert",
      pad: "lr+fire",
      make: cabinetRouteTable
    },
    {
      id: "lift-sla",
      name: "LIFT SLA",
      genre: "ELEVATOR",
      family: "MANAGE",
      tagline: "Queueing theory in a friendlier hat.",
      controls: "↑ ↓ call the car",
      pad: "ud",
      make: cabinetLiftSla
    },
    {
      id: "on-call",
      name: "ON CALL",
      genre: "INCIDENT",
      family: "MANAGE",
      tagline: "Hold to work it. Escalation is the one thing speed cannot fix.",
      controls: "arrows move · SPACE hold to work",
      pad: "dpad+fire",
      make: cabinetOnCall
    },
    {
      id: "harvest-window",
      name: "HARVEST WINDOW",
      genre: "FARM",
      family: "MANAGE",
      tagline: "Too early is waste. Too late is spoiled.",
      controls: "arrows move · SPACE plant or reap",
      pad: "dpad+fire",
      make: cabinetHarvestWindow
    },
    {
      id: "tunnel",
      name: "TUNNEL",
      genre: "TUNNEL",
      family: "RUN",
      tagline: "Rings rush the camera. One sector is open.",
      controls: "← → rotate",
      pad: "lr",
      make: cabinetTunnel
    },
    {
      id: "uptime",
      name: "UPTIME",
      genre: "CLIMBER",
      family: "RUN",
      tagline: "Every release holds your weight for one landing.",
      controls: "← → drift",
      pad: "lr",
      make: cabinetUptime
    },
    {
      id: "growth",
      name: "GROWTH",
      genre: "ARENA",
      family: "ACTION",
      tagline: "You die by crossing your own trail. So do they.",
      controls: "arrows turn",
      pad: "dpad",
      make: cabinetGrowth
    },
    {
      id: "absorb",
      name: "ABSORB",
      genre: "ARENA",
      family: "ACTION",
      tagline: "Eat what is smaller. Get slower doing it.",
      controls: "arrows move",
      pad: "dpad",
      make: cabinetAbsorb
    },
    {
      id: "cavern",
      name: "CAVERN",
      genre: "ONE-TOUCH",
      family: "RUN",
      tagline: "Hold to climb, release to fall, and it narrows.",
      controls: "SPACE hold to climb",
      pad: "tap",
      make: cabinetCavern
    },
    {
      id: "tap-order",
      name: "TAP ORDER",
      genre: "REACTION",
      family: "TIMING",
      tagline: "Answer the one at the line. Out of order is a miss.",
      controls: "← ↑ ↓ → answer",
      pad: "lanes",
      make: cabinetTapOrder
    },
    {
      id: "aim-drill",
      name: "AIM DRILL",
      genre: "AIM",
      family: "SHOOT",
      tagline: "The clock only extends on accuracy. Spraying is worse.",
      controls: "arrows aim · SPACE fire",
      pad: "dpad+fire",
      make: cabinetAimDrill
    },
    {
      id: "cold-path",
      name: "COLD PATH",
      genre: "REACTION",
      family: "TIMING",
      tagline: "Early is worse than slow. Wait for green.",
      controls: "SPACE on green",
      pad: "tap",
      make: cabinetColdPath
    },
    {
      id: "pop-the-queue",
      name: "POP THE QUEUE",
      genre: "WHACK",
      family: "ACTION",
      tagline: "Jobs surface and sink whether or not you got to them.",
      controls: "arrows move · SPACE pop",
      pad: "dpad+fire",
      make: cabinetPopTheQueue
    },
    {
      id: "spin-plates",
      name: "SPIN PLATES",
      genre: "UPKEEP",
      family: "MANAGE",
      tagline: "Every second spent tending one is five others decaying.",
      controls: "← → pick · SPACE hold to tend",
      pad: "lr+fire",
      make: cabinetSpinPlates
    },
    {
      id: "shard-field",
      name: "SHARD FIELD",
      genre: "SPACE",
      family: "SHOOT",
      tagline: "Shoot a shard and it splits. Re-sharding, from inside.",
      controls: "← → turn · ↑ thrust · SPACE fire",
      pad: "dpad+fire",
      make: cabinetShardField
    },
    {
      id: "intercept",
      name: "INTERCEPT",
      genre: "DEFENCE",
      family: "SHOOT",
      tagline: "One burst that catches three beats three that catch one.",
      controls: "arrows aim · SPACE burst",
      pad: "dpad+fire",
      make: cabinetIntercept
    },
    {
      id: "artillery",
      name: "ARTILLERY",
      genre: "ARTILLERY",
      family: "SHOOT",
      tagline: "Angle, power, wind, and one shot each.",
      controls: "↑ ↓ angle · SPACE hold to charge",
      pad: "ud+fire",
      make: cabinetArtillery
    },
    {
      id: "invert",
      name: "INVERT",
      genre: "RUNNER",
      family: "RUN",
      tagline: "One verb: flip which way down is.",
      controls: "SPACE invert",
      pad: "tap",
      make: cabinetInvert
    },
    {
      id: "depth-charge",
      name: "DEPTH CHARGE",
      genre: "SONAR",
      family: "SHOOT",
      tagline: "Between pings you are working from memory.",
      controls: "← → steer · SPACE drop",
      pad: "lr+fire",
      make: cabinetDepthCharge
    },
    {
      id: "pipe-permit",
      name: "PIPE PERMIT",
      genre: "PIPES",
      family: "PUZZLE",
      tagline: "The pressure does not wait for you to finish thinking.",
      controls: "arrows move · SPACE rotate",
      pad: "dpad+fire",
      make: cabinetPipePermit
    },
    {
      id: "circuit-route",
      name: "CIRCUIT ROUTE",
      genre: "ROUTING",
      family: "PUZZLE",
      tagline: "Every pair you connect makes the rest of the board worse.",
      controls: "arrows draw · SPACE start or finish",
      pad: "dpad+fire",
      make: cabinetCircuitRoute
    },
    {
      id: "factory-line",
      name: "FACTORY LINE",
      genre: "FACTORY",
      family: "MANAGE",
      tagline: "Three stamps, one belt, and it never stops.",
      controls: "← → stamp · SPACE press",
      pad: "lr+fire",
      make: cabinetFactoryLine
    },
    {
      id: "bridge-build",
      name: "BRIDGE BUILD",
      genre: "SPAN",
      family: "MANAGE",
      tagline: "You can see how wrong you were.",
      controls: "SPACE hold to extend",
      pad: "tap",
      make: cabinetBridgeBuild
    },
    {
      id: "sort-keys",
      name: "SORT KEYS",
      genre: "SORT",
      family: "PUZZLE",
      tagline: "Every pour is progress or a wasted tube.",
      controls: "← → pick · SPACE lift or pour",
      pad: "lr+fire",
      make: cabinetSortKeys
    },
    {
      id: "blast-map",
      name: "BLAST MAP",
      genre: "MINEFIELD",
      family: "PUZZLE",
      tagline: "Deduction, where one wrong click ends the board.",
      controls: "arrows move · SPACE open",
      pad: "dpad+fire",
      make: cabinetBlastMap
    },
    {
      id: "word-lock",
      name: "WORD LOCK",
      genre: "WORD",
      family: "PUZZLE",
      tagline: "Five letters, six tries, one scope.",
      controls: "↑ ↓ letter · ← → slot · SPACE submit",
      pad: "dpad+fire",
      make: cabinetWordLock
    },
    {
      id: "tile-audit",
      name: "TILE AUDIT",
      genre: "TILES",
      family: "PUZZLE",
      tagline: "The order you clear in decides whether the board opens.",
      controls: "arrows move · SPACE pick a pair",
      pad: "dpad+fire",
      make: cabinetTileAudit
    },
    {
      id: "patience",
      name: "PATIENCE",
      genre: "CARDS",
      family: "PUZZLE",
      tagline: "One higher or one lower, and a stock that runs out.",
      controls: "← → pick · SPACE play · ↑ ↓ draw",
      pad: "dpad+fire",
      make: cabinetPatience
    },
    {
      id: "mate-in-one",
      name: "MATE IN ONE",
      genre: "CHESS",
      family: "PUZZLE",
      tagline: "One move, one board, one correct answer.",
      controls: "arrows move · SPACE commit",
      pad: "dpad+fire",
      make: cabinetMateInOne
    },
    {
      id: "long-poll",
      name: "LONG POLL",
      genre: "FISHING",
      family: "MANAGE",
      tagline: "Cast, wait, and answer the tug in the window it gives you.",
      controls: "SPACE cast, strike, and reel",
      pad: "tap",
      make: cabinetLongPoll
    },
    {
      id: "pet-agent",
      name: "PET AGENT",
      genre: "CARE",
      family: "MANAGE",
      tagline: "Three needs, one of you, different drain rates.",
      controls: "← → pick · SPACE hold to tend",
      pad: "lr+fire",
      make: cabinetPetAgent
    },
    {
      id: "checkout",
      name: "CHECKOUT",
      genre: "DARTS",
      family: "SPORT",
      tagline: "A number to finish on, three throws, and a board that punishes greed.",
      controls: "SPACE throw",
      pad: "tap",
      make: cabinetCheckout
    },
    {
      id: "one-under",
      name: "ONE UNDER",
      genre: "GOLF",
      family: "SPORT",
      tagline: "Par is a budget. The water is a reset.",
      controls: "← → aim · SPACE hold to power",
      pad: "lr+fire",
      make: cabinetOneUnder
    },
    {
      id: "strike-quota",
      name: "STRIKE QUOTA",
      genre: "BOWLING",
      family: "SPORT",
      tagline: "Set the line, then the power. Two throws a frame.",
      controls: "SPACE set line then power",
      pad: "tap",
      make: cabinetStrikeQuota
    },
    {
      id: "service-menu",
      name: "SERVICE MENU",
      genre: "COOKING",
      family: "MANAGE",
      tagline: "Assemble in order. A wrong component throws the plate.",
      controls: "← → choose · SPACE add",
      pad: "lr+fire",
      make: cabinetServiceMenu
    },
    {
      id: "drift-queue",
      name: "DRIFT QUEUE",
      genre: "RACING",
      family: "DRIVE",
      tagline: "Holding the slide pays more than driving it clean.",
      controls: "← → steer · SPACE handbrake",
      pad: "lr+fire",
      make: cabinetDriftQueue
    },
    {
      id: "mine-cart",
      name: "MINE CART",
      genre: "RAILS",
      family: "RUN",
      tagline: "Pick the branch, jump the gap, only ever forward.",
      controls: "↑ ↓ switch rail · SPACE jump",
      pad: "ud+fire",
      make: cabinetMineCart
    },
    {
      id: "thrust-budget",
      name: "THRUST BUDGET",
      genre: "JETPACK",
      family: "RUN",
      tagline: "Fuel burns while you hold and refills on the ground.",
      controls: "SPACE hold to fly",
      pad: "tap",
      make: cabinetThrustBudget
    },
    {
      id: "wall-jump",
      name: "WALL JUMP",
      genre: "SHAFT",
      family: "RUN",
      tagline: "You stick where you land. Hesitating is the loss.",
      controls: "SPACE push off",
      pad: "tap",
      make: cabinetWallJump
    },
    {
      id: "cold-slope",
      name: "COLD SLOPE",
      genre: "DOWNHILL",
      family: "RUN",
      tagline: "Miss a gate and you find out at the bottom.",
      controls: "← → carve",
      pad: "lr",
      make: cabinetColdSlope
    },
    {
      id: "handoff",
      name: "HANDOFF",
      genre: "PARKOUR",
      family: "RUN",
      tagline: "The service you are standing on is about to go away.",
      controls: "SPACE leap",
      pad: "tap",
      make: cabinetHandoff
    },
    {
      id: "bubble-queue",
      name: "BUBBLE QUEUE",
      genre: "BUBBLE",
      family: "PUZZLE",
      tagline: "A shot that clears nothing costs you twice.",
      controls: "← → aim · SPACE fire",
      pad: "lr+fire",
      make: cabinetBubbleQueue
    },
    {
      id: "scope-match",
      name: "SCOPE MATCH",
      genre: "RINGS",
      family: "RUN",
      tagline: "Your scope changes at every gate. Read it again.",
      controls: "SPACE hold to slow the fall",
      pad: "tap",
      make: cabinetScopeMatch
    },
    {
      id: "reorder",
      name: "REORDER",
      genre: "SLIDE",
      family: "PUZZLE",
      tagline: "One blank, four moves, an optimum you will not find.",
      controls: "arrows slide a tile in",
      pad: "dpad",
      make: cabinetReorder
    },
    {
      id: "breaker",
      name: "BREAKER",
      genre: "BREAKOUT",
      family: "SPORT",
      tagline: "A deprecation nobody finished is back next release.",
      controls: "← → paddle",
      pad: "lr",
      make: cabinetBreaker
    },
    {
      id: "bank-shot",
      name: "BANK SHOT",
      genre: "POOL",
      family: "SPORT",
      tagline: "Think one bounce further than feels necessary.",
      controls: "← → aim · SPACE hold to power",
      pad: "lr+fire",
      make: cabinetBankShot
    },
    {
      id: "draw-weight",
      name: "DRAW WEIGHT",
      genre: "ARCHERY",
      family: "SPORT",
      tagline: "Wind, drop, and no straight lines.",
      controls: "↑ ↓ elevation · SPACE hold to draw",
      pad: "ud+fire",
      make: cabinetDrawWeight
    },
    {
      id: "crossfade",
      name: "CROSSFADE",
      genre: "MIXING",
      family: "TIMING",
      tagline: "Beat-matching is a control loop with a crowd attached.",
      controls: "← → fader · SPACE nudge the second deck",
      pad: "lr+fire",
      make: cabinetCrossfade
    },
    {
      id: "orbital",
      name: "ORBITAL",
      genre: "ORBIT",
      family: "DRIVE",
      tagline: "One burn. The well does the rest.",
      controls: "↑ ↓ angle · SPACE hold to burn",
      pad: "ud+fire",
      make: cabinetOrbital
    },
    {
      id: "spot-kick",
      name: "SPOT KICK",
      genre: "SHOOTOUT",
      family: "SPORT",
      tagline: "You take one, then you keep one. Both are a read.",
      controls: "← → choose · SPACE commit",
      pad: "lr+fire",
      make: cabinetSpotKick
    },
    {
      id: "handshake",
      name: "HANDSHAKE",
      genre: "PROTOCOL",
      family: "TIMING",
      tagline: "Long and short presses, and it gets stricter.",
      controls: "SPACE tap short or hold long",
      pad: "tap",
      make: cabinetHandshake
    },
    {
      id: "untangle",
      name: "UNTANGLE",
      genre: "GRAPH",
      family: "PUZZLE",
      tagline: "A clean layout always exists. Seeing it is the work.",
      controls: "arrows move · SPACE grab or drop",
      pad: "dpad+fire",
      make: cabinetUntangle
    },
    {
      id: "grid-proof",
      name: "GRID PROOF",
      genre: "NONOGRAM",
      family: "PUZZLE",
      tagline: "No hidden information and no luck.",
      controls: "arrows move · SPACE fill",
      pad: "dpad+fire",
      make: cabinetGridProof
    }
  ];

  // Appended rather than written into the literal above, which is declared
  // before the roster exists. The line it replaces said "FOUR CABINETS READY"
  // and had already started lying by the time there were twelve.
  BOOT_LINES.push(CABINETS.length + " CABINETS READY. NONE OF THEM ARE LOAD-BEARING.");

  /* ======================================================================
     SHELL: overlay, focus management, boot, select, play loop
     ====================================================================== */

  var launcher = doc.getElementById("arcade-launch");
  if (!launcher || !doc.createElement("canvas").getContext) return;

  var overlay = null;
  var nodes = {};
  var isOpen = false;
  var screen = "closed"; // closed | boot | select | play | over
  var current = null;
  var game = null;
  var random = makeRandom(20260819);
  var rafId = 0;
  var lastNow = 0;
  var accumulator = 0;
  var paused = false;
  var statusIndex = 0;
  var statusTimer = 0;
  var bootIndex = 0;
  var bootTimer = 0;
  var lastFocus = null;
  var announceTimer = 0;

  var swipeFrom = null;
  /* A tap's pointerdown and pointerup can both land inside one animation
     frame, and the simulation only samples input once per fixed step — so a
     raw "true on down, false on up" fire would be invisible to the game about
     as often as not. A tap therefore latches fire for a few steps, and the
     three sources (key held, pointer held, tap pulse) are tracked separately
     so releasing one cannot cancel another. */
  var FIRE_PULSE_STEPS = 4;
  var pointerHeld = false;
  var pointerFireSteps = 0;
  var keyFire = false;

  var input = {
    left: false,
    right: false,
    up: false,
    down: false,
    fire: false,
    pointerX: null,
    pointerY: null
  };

  /* ---- best scores -------------------------------------------------------

     Per-cabinet bests, in localStorage next to the accessibility panel's own
     key. Every read and write is wrapped: a private window, a browser set to
     block site data, or a corrupted value all throw or return junk here, and
     none of those is a reason for the waiting room to fail to open. The score
     is a joke about metering — losing it costs nothing. */

  var BEST_KEY = "amw-arcade-best";

  function loadBest() {
    try {
      var raw = window.localStorage.getItem(BEST_KEY);
      var parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (error) {
      return {};
    }
  }

  /* Returns "" for a run that did not beat the stored best, "stored" when the
     new best was written, and "unstored" when it was a best but the write
     failed. The three are distinct because the run-complete screen tells the
     visitor their score was kept, and saying that after a quota error or in a
     window with storage blocked is simply untrue — the reload that loses it
     is coming either way, and the honest line costs nothing. */
  function recordBest(id, score) {
    var all = loadBest();
    if (typeof all[id] === "number" && all[id] >= score) return "";
    all[id] = score;
    try {
      window.localStorage.setItem(BEST_KEY, JSON.stringify(all));
    } catch (error) {
      return "unstored";
    }
    return "stored";
  }

  function inkPalette() {
    if (!contrastHigh()) return PALETTE;
    // Tracks the high-contrast block in styles.css. These had drifted: the
    // values here were still the retired charcoal-and-ember palette, so a
    // high-contrast visitor got a differently-coloured arcade to everyone
    // else's.
    return {
      bg: "#000000",
      grid: "#5b53a0",
      ink: "#ffffff",
      dim: "#ddd9ff",
      brass: "#ffd24d",
      bright: "#ffe680",
      verify: "#6cff9f",
      danger: "#ff8f85",
      wall: ["#ffffff", "#c9c4f0", "#8f86d6", "#413a7a"]
    };
  }

  function buildOverlay() {
    overlay = el("div", "arcade-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "arcade-title");
    overlay.hidden = true;

    var frame = el("div", "arcade-frame");

    var head = el("header", "arcade-head");
    var title = el("h2", "arcade-title", "THE HUMAN WAITING ROOM");
    title.id = "arcade-title";
    head.appendChild(title);
    nodes.status = el("p", "arcade-status", AGENT_STATUS[0]);
    head.appendChild(nodes.status);

    var close = el("button", "arcade-close", "EXIT [ESC]");
    close.type = "button";
    close.addEventListener("click", function () {
      closeArcade();
    });
    head.appendChild(close);
    frame.appendChild(head);

    nodes.boot = el("pre", "arcade-boot");
    nodes.boot.setAttribute("aria-hidden", "true");
    frame.appendChild(nodes.boot);

    nodes.select = el("div", "arcade-select");
    nodes.select.hidden = true;

    var selectHead = el("div", "arcade-select-head");
    selectHead.appendChild(el("h2", "arcade-select-title", "SELECT A CABINET"));
    nodes.selectCount = el("p", "arcade-credits", "");
    selectHead.appendChild(nodes.selectCount);
    nodes.select.appendChild(selectHead);

    /* Filters run on the cabinet's *family*, not its genre. The roster is
       still the source of truth, but at a hundred cabinets there are ~90
       distinct genres, and a filter row with ninety chips in it is a worse
       maze than the grid it is meant to tame. Each tile keeps its specific
       genre on the badge — the family is the shelf, the genre is the label. */
    var genres = ["ALL"];
    CABINETS.forEach(function (cabinet) {
      var family = cabinet.family || cabinet.genre;
      if (genres.indexOf(family) === -1) genres.push(family);
    });
    nodes.filters = el("div", "arcade-filters");
    nodes.filters.setAttribute("role", "group");
    nodes.filters.setAttribute("aria-label", "Filter cabinets by genre");
    genres.forEach(function (genre) {
      var chip = el("button", "arcade-filter", genre);
      chip.type = "button";
      chip.setAttribute("data-genre", genre);
      chip.setAttribute("aria-pressed", genre === "ALL" ? "true" : "false");
      chip.addEventListener("click", function () {
        applyFilter(genre);
      });
      nodes.filters.appendChild(chip);
    });
    nodes.select.appendChild(nodes.filters);

    nodes.grid = el("div", "arcade-cabinets");
    CABINETS.forEach(function (cabinet) {
      var button = el("button", "arcade-cabinet");
      button.type = "button";
      button.setAttribute("data-cabinet", cabinet.id);
      button.setAttribute("data-genre", cabinet.genre);
      button.setAttribute("data-family", cabinet.family || cabinet.genre);

      // Decorative: the marquee is drawn by CSS off the genre, and a screen
      // reader announcing "image" for a gradient helps nobody.
      var art = el("span", "arcade-cabinet-art");
      art.setAttribute("aria-hidden", "true");
      button.appendChild(art);

      button.appendChild(el("span", "arcade-cabinet-genre", cabinet.genre));
      button.appendChild(el("span", "arcade-cabinet-name", cabinet.name));
      button.appendChild(el("span", "arcade-cabinet-tag", cabinet.tagline));
      button.appendChild(el("span", "arcade-cabinet-controls", cabinet.controls));
      button.appendChild(el("span", "arcade-cabinet-best", ""));
      button.addEventListener("click", function () {
        startGame(cabinet.id);
      });
      nodes.grid.appendChild(button);
    });
    nodes.select.appendChild(nodes.grid);

    /* Attract mode. A cabinet plays itself under the grid, which is the one
       piece of arcade furniture that cannot be faked with CSS: it has to be a
       real cabinet running real frames, or it is a video of a game rather than
       the game. Reduced motion gets a still frame instead. */
    nodes.attract = el("div", "arcade-attract");
    nodes.attractCanvas = el("canvas", "arcade-attract-canvas");
    nodes.attractCanvas.width = W;
    nodes.attractCanvas.height = H;
    nodes.attractCanvas.setAttribute("aria-hidden", "true");
    nodes.attract.appendChild(nodes.attractCanvas);
    nodes.attractLabel = el("p", "arcade-attract-label", "");
    nodes.attract.appendChild(nodes.attractLabel);
    nodes.select.appendChild(nodes.attract);

    frame.appendChild(nodes.select);

    nodes.stage = el("div", "arcade-stage");
    nodes.stage.hidden = true;

    var hud = el("div", "arcade-hud");
    nodes.hudName = el("span", "arcade-hud-name", "");
    nodes.hudScore = el("span", null, "RECEIPTS 0");
    nodes.hudLives = el("span", null, "RETRIES 3");
    nodes.hudExtra = el("span", null, "");
    hud.appendChild(nodes.hudName);
    hud.appendChild(nodes.hudScore);
    hud.appendChild(nodes.hudLives);
    hud.appendChild(nodes.hudExtra);
    nodes.stage.appendChild(hud);

    nodes.canvas = el("canvas", "arcade-canvas");
    // Focusable and role="application": the running cabinet consumes arrow
    // keys and space itself, and focus has to land somewhere inside the
    // dialog when the cabinet-select buttons are hidden on start.
    nodes.canvas.setAttribute("role", "application");
    nodes.canvas.setAttribute("tabindex", "0");
    nodes.canvas.setAttribute("aria-label", "Arcade cabinet screen");
    nodes.stage.appendChild(nodes.canvas);

    nodes.stage.appendChild(buildPad());

    var stageFoot = el("div", "arcade-stagefoot");
    nodes.hint = el("span", "arcade-hint", "");
    nodes.padToggle = el("button", "arcade-padtoggle", "TOUCH ON");
    nodes.padToggle.type = "button";
    nodes.padToggle.addEventListener("click", function () {
      setPadEnabled(!padEnabled);
    });
    var back = el("button", "arcade-back", "← CABINETS");
    back.type = "button";
    back.addEventListener("click", showSelect);
    stageFoot.appendChild(nodes.hint);
    stageFoot.appendChild(nodes.padToggle);
    stageFoot.appendChild(back);
    nodes.stage.appendChild(stageFoot);
    frame.appendChild(nodes.stage);

    nodes.over = el("div", "arcade-over");
    nodes.over.hidden = true;
    frame.appendChild(nodes.over);

    nodes.live = el("p", "arcade-live");
    nodes.live.setAttribute("role", "status");
    nodes.live.setAttribute("aria-live", "polite");
    frame.appendChild(nodes.live);

    overlay.appendChild(frame);
    doc.body.appendChild(overlay);

    nodes.ctx = nodes.canvas.getContext("2d");
    nodes.canvas.addEventListener("pointermove", onPointer);
    nodes.canvas.addEventListener("pointerdown", onPointerDown);
    nodes.canvas.addEventListener("pointerup", releasePointer);
    nodes.canvas.addEventListener("pointercancel", releasePointer);
    nodes.canvas.addEventListener("pointerleave", function () {
      releasePointer();
      input.pointerX = null;
      input.pointerY = null;
    });
  }

  /* ---- pointer and touch -------------------------------------------------

     A phone has no arrow keys and no space bar, so the pointer has to carry
     all three verbs: position (drag), fire (tap), and direction (swipe). The
     cabinets that read a position prefer pointerX/pointerY over the direction
     flags, so latching a swipe direction here cannot fight a drag there. */
  var SWIPE_MIN = 10; // logical px before a drag counts as a swipe

  function canvasPoint(event) {
    var rect = nodes.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {
      x: ((event.clientX - rect.left) / rect.width) * W,
      y: ((event.clientY - rect.top) / rect.height) * H
    };
  }

  function clearDirections() {
    input.left = false;
    input.right = false;
    input.up = false;
    input.down = false;
  }

  function onPointerDown(event) {
    var point = canvasPoint(event);
    if (!point) return;
    input.pointerX = point.x;
    input.pointerY = point.y;
    swipeFrom = point;
    // A tap is the fire button: it denies a scope and serves a request.
    pointerHeld = true;
    pointerFireSteps = FIRE_PULSE_STEPS;
    input.fire = true;
    if (screen === "play" && nodes.canvas.focus) nodes.canvas.focus();
  }

  function onPointer(event) {
    var point = canvasPoint(event);
    if (!point) return;
    input.pointerX = point.x;
    input.pointerY = point.y;
    if (!swipeFrom) return;
    var dx = point.x - swipeFrom.x;
    var dy = point.y - swipeFrom.y;
    if (Math.abs(dx) < SWIPE_MIN && Math.abs(dy) < SWIPE_MIN) return;
    // Latch the dominant axis and re-anchor, so a long drag can turn more
    // than once. Cabinets that steer by held direction read this; the rest
    // ignore it in favour of the position above.
    clearDirections();
    if (Math.abs(dx) > Math.abs(dy)) {
      input[dx > 0 ? "right" : "left"] = true;
    } else {
      input[dy > 0 ? "down" : "up"] = true;
    }
    swipeFrom = point;
  }

  function releasePointer() {
    swipeFrom = null;
    pointerHeld = false;
    if (!keyFire && pointerFireSteps <= 0) input.fire = false;
    // Lifting the pointer ends the gesture, so both the aim and any direction
    // the swipe latched have to go with it. Previously only pointerleave
    // cleared the aim and nothing ever cleared the latch, which left a
    // direction held down after the finger was gone — harmless on the
    // cabinets that latch a heading of their own, but on a first-person
    // cabinet it is a camera that never stops turning.
    input.pointerX = null;
    input.pointerY = null;
    clearDirections();
  }

  /* Run once per simulation step, before the cabinet reads input, so a tap's
     pulse is measured in steps the game actually took rather than in
     wall-clock time it may have slept through. */
  function advanceInput() {
    if (pointerFireSteps > 0) {
      pointerFireSteps -= 1;
      if (pointerFireSteps === 0 && !pointerHeld && !keyFire) input.fire = false;
    }
  }

  /* ======================================================================
     TOUCH CONTROLS

     The arcade shipped keyboard-first with a pointer bolted on: drag to move,
     tap to fire, and a swipe latch for direction. That is playable on a phone
     in about the way a piano is playable through a letterbox — the finger
     covers the thing it is steering, and a swipe latch cannot express "hold
     left while firing", which most of this roster now needs.

     So the phone gets real buttons under the screen. They are not a fallback:
     on a coarse pointer they are the primary control surface, the canvas keeps
     its full size above them, and every cabinet declares which layout it wants.

     Rules this obeys:
     - A button is held, not tapped: pointerdown sets the input slot, pointerup
       clears it, and the pointer is captured so a finger that slides off the
       button still releases it. A stuck direction is the worst bug this layer
       can have.
     - The same buttons are real <button>s, so a keyboard or switch user can
       reach them. A click (no pointer hold) pulses the slot for a few
       simulation steps, the way a tap on the canvas already does.
     - Nothing here is required. Every cabinet is still fully playable on the
       arrows and space, and the pad can be turned off.
     ====================================================================== */

  /* Layouts, keyed by what a cabinet actually reads. Naming them by input
     rather than by genre is deliberate: two cabinets that read the same keys
     must get the same pad, or the muscle memory resets between them. */
  var PAD_LAYOUTS = {
    "dpad+fire": { dpad: true, action: "FIRE" },
    "dpad": { dpad: true },
    "lr+fire": { lr: true, action: "FIRE" },
    "lr": { lr: true },
    "ud+fire": { ud: true, action: "FIRE" },
    "ud": { ud: true },
    "lanes": { lanes: true },
    "tap": { action: "TAP" }
  };

  var padNodes = null;
  var padEnabled = null; // null = not yet decided; decided on first open
  var padPulse = {};     // slot -> simulation steps remaining for a click pulse

  function coarsePointer() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(pointer: coarse)").matches
    );
  }

  /* A press can arrive from a finger (held) or from a click (pulsed). They are
     tracked separately for the same reason the fire key is: releasing one must
     not cancel the other. */
  function padSet(slot, down) {
    if (down) {
      input[slot] = true;
      return;
    }
    if (padPulse[slot] > 0) return; // a pulse is still running; let it finish
    input[slot] = false;
    if (slot === "fire" && (keyFire || pointerHeld)) input.fire = true;
  }

  function padTap(slot) {
    padPulse[slot] = FIRE_PULSE_STEPS;
    input[slot] = true;
  }

  /* Called once per simulation step from the same place the tap pulse is
     aged, so a click lasts a fixed number of *steps* rather than a fixed
     number of milliseconds the loop may have slept through. */
  function advancePad() {
    for (var slot in padPulse) {
      if (!padPulse[slot]) continue;
      padPulse[slot] -= 1;
      if (padPulse[slot] <= 0) {
        padPulse[slot] = 0;
        if (slot === "fire" && (keyFire || pointerHeld)) continue;
        input[slot] = false;
      }
    }
  }

  function padButton(slot, label, className) {
    var button = el("button", "arcade-padkey " + className, label);
    button.type = "button";
    button.setAttribute("data-slot", slot);
    button.setAttribute("aria-label", label + " control");

    button.addEventListener("pointerdown", function (event) {
      event.preventDefault();
      if (button.setPointerCapture) {
        try { button.setPointerCapture(event.pointerId); } catch (err) { /* not fatal */ }
      }
      button.classList.add("is-down");
      padSet(slot, true);
      // A short tick of haptic feedback where the platform offers it, and
      // never under reduced motion — a vibration is motion.
      if (!prefersStaticMotion() && navigator.vibrate) {
        try { navigator.vibrate(8); } catch (err) { /* refused, fine */ }
      }
      if (screen === "play" && nodes.canvas.focus) nodes.canvas.focus({ preventScroll: true });
    });

    function release(event) {
      if (event) event.preventDefault();
      button.classList.remove("is-down");
      padSet(slot, false);
    }
    button.addEventListener("pointerup", release);
    button.addEventListener("pointercancel", release);
    button.addEventListener("pointerleave", release);

    // Keyboard and assistive activation: no pointer sequence arrives, so the
    // slot is pulsed instead of held.
    button.addEventListener("click", function (event) {
      event.preventDefault();
      padTap(slot);
    });
    return button;
  }

  function buildPad() {
    var wrap = el("div", "arcade-pad");
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Touch controls");

    var left = el("div", "arcade-pad-left");
    var dpad = el("div", "arcade-dpad");
    var up = padButton("up", "▲", "arcade-pad-up");
    var down = padButton("down", "▼", "arcade-pad-down");
    var leftKey = padButton("left", "◀", "arcade-pad-left-key");
    var rightKey = padButton("right", "▶", "arcade-pad-right-key");
    dpad.appendChild(up);
    dpad.appendChild(leftKey);
    dpad.appendChild(rightKey);
    dpad.appendChild(down);
    left.appendChild(dpad);

    var lanes = el("div", "arcade-lanes");
    var laneKeys = [
      padButton("left", "◀", "arcade-lane"),
      padButton("up", "▲", "arcade-lane"),
      padButton("down", "▼", "arcade-lane"),
      padButton("right", "▶", "arcade-lane")
    ];
    laneKeys.forEach(function (key) { lanes.appendChild(key); });

    var right = el("div", "arcade-pad-right");
    var action = padButton("fire", "FIRE", "arcade-pad-action");
    right.appendChild(action);

    wrap.appendChild(left);
    wrap.appendChild(lanes);
    wrap.appendChild(right);

    padNodes = {
      wrap: wrap,
      dpad: dpad,
      lanes: lanes,
      action: action,
      up: up,
      down: down,
      leftKey: leftKey,
      rightKey: rightKey,
      right: right
    };
    return wrap;
  }

  /* Applies a cabinet's declared layout. Unused buttons are removed from the
     accessibility tree as well as hidden, so a screen-reader user is not
     offered a control the running cabinet ignores. */
  function applyPad(scheme) {
    if (!padNodes) return;
    var layout = PAD_LAYOUTS[scheme] || PAD_LAYOUTS["dpad+fire"];

    function show(node, on) {
      node.hidden = !on;
      if (on) node.removeAttribute("aria-hidden");
      else node.setAttribute("aria-hidden", "true");
    }

    show(padNodes.dpad, !!(layout.dpad || layout.lr || layout.ud));
    show(padNodes.lanes, !!layout.lanes);
    show(padNodes.right, !!layout.action);
    show(padNodes.up, !!(layout.dpad || layout.ud));
    show(padNodes.down, !!(layout.dpad || layout.ud));
    show(padNodes.leftKey, !!(layout.dpad || layout.lr));
    show(padNodes.rightKey, !!(layout.dpad || layout.lr));
    padNodes.dpad.setAttribute("data-axis", layout.dpad ? "both" : layout.lr ? "x" : "y");
    if (layout.action) padNodes.action.textContent = layout.action;
    padNodes.wrap.hidden = !padEnabled;
  }

  function setPadEnabled(on) {
    padEnabled = !!on;
    if (padNodes) padNodes.wrap.hidden = !padEnabled;
    if (nodes.padToggle) {
      nodes.padToggle.setAttribute("aria-pressed", padEnabled ? "true" : "false");
      nodes.padToggle.textContent = padEnabled ? "TOUCH ON" : "TOUCH OFF";
    }
    // Releasing everything on toggle-off: a slot held by a button that just
    // vanished would otherwise stay held forever.
    if (!padEnabled) {
      clearDirections();
      if (!keyFire && !pointerHeld) input.fire = false;
    }
    try {
      window.localStorage.setItem(PAD_KEY, padEnabled ? "1" : "0");
    } catch (err) { /* private window; the default still applies next time */ }
  }

  var PAD_KEY = "amw-arcade-pad";

  function initialPadState() {
    try {
      var stored = window.localStorage.getItem(PAD_KEY);
      if (stored === "1") return true;
      if (stored === "0") return false;
    } catch (err) { /* fall through to the device default */ }
    // Mobile-first: a touch device gets the pad without being asked.
    return coarsePointer();
  }

  /* ---- focus containment -------------------------------------------------

     The page's skip link sits at body level, outside the regions this makes
     inert, and above the overlay in the stacking order, so a Tab that escaped
     would land on a control the visitor cannot even see. Containment is
     therefore handled at the document, not on the overlay: once focus is out
     — which happens whenever starting a cabinet hides the button that had it
     — an overlay-scoped listener would never run again. */
  function focusables() {
    return Array.prototype.filter.call(
      overlay.querySelectorAll("button, a[href], canvas[tabindex]"),
      function (node) {
        return !node.disabled && node.offsetParent !== null;
      }
    );
  }

  function trapFocus(event) {
    var list = focusables();
    if (!list.length) return;
    var first = list[0];
    var last = list[list.length - 1];
    if (!overlay.contains(doc.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }
    if (event.shiftKey && doc.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && doc.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function announce(text) {
    if (nodes.live) nodes.live.textContent = text;
  }

  function resizeCanvas() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var rect = nodes.canvas.getBoundingClientRect();
    var scale = Math.max(1, Math.floor(Math.min(rect.width / W, rect.height / H)));
    if (!rect.width) scale = 2;
    nodes.canvas.width = Math.round(W * scale * dpr);
    nodes.canvas.height = Math.round(H * scale * dpr);
    nodes.ctx.setTransform(scale * dpr, 0, 0, scale * dpr, 0, 0);
    nodes.ctx.imageSmoothingEnabled = false;
    nodes.ctx.textBaseline = "middle";
  }

  /* ---- open / close ------------------------------------------------------ */

  function openArcade() {
    if (isOpen) return;
    if (!overlay) buildOverlay();
    if (padEnabled === null) setPadEnabled(initialPadState());
    lastFocus = doc.activeElement;
    isOpen = true;
    overlay.hidden = false;
    root.classList.add("arcade-open");
    // The particle field's IntersectionObserver unschedules its loop the
    // moment the canvas stops intersecting, so hiding it here is the pause.
    setBackgroundInert(true);
    random = makeRandom(20260819);
    startBoot();
    var close = overlay.querySelector(".arcade-close");
    if (close) close.focus();
    announce("Arcade open. The agents continue working.");
  }

  function closeArcade() {
    if (!isOpen) return;
    isOpen = false;
    stopLoop();
    screen = "closed";
    current = null;
    game = null;
    overlay.hidden = true;
    root.classList.remove("arcade-open");
    setBackgroundInert(false);
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  function setBackgroundInert(on) {
    // The skip link is a body-level sibling rather than part of any of these
    // landmarks, so listing only the landmarks would leave one
    // tabbable control outside the dialog.
    var regions = [
      doc.querySelector(".skip-link"),
      doc.querySelector(".site-nav"),
      doc.getElementById("main"),
      doc.querySelector(".site-footer")
    ];
    regions.forEach(function (region) {
      if (!region) return;
      if (on) {
        region.setAttribute("aria-hidden", "true");
        if ("inert" in region) region.inert = true;
      } else {
        region.removeAttribute("aria-hidden");
        if ("inert" in region) region.inert = false;
      }
    });
  }

  /* ---- boot -------------------------------------------------------------- */

  function startBoot() {
    screen = "boot";
    nodes.boot.hidden = false;
    nodes.select.hidden = true;
    nodes.stage.hidden = true;
    nodes.over.hidden = true;
    bootIndex = 0;
    bootTimer = 0;
    nodes.boot.textContent = "";
    if (prefersStaticMotion()) {
      // One frame, no typing: the boot sequence is decoration, and decoration
      // is what reduced motion asks us to drop.
      nodes.boot.textContent = BOOT_LINES.join("\n");
      bootIndex = BOOT_LINES.length;
      showSelect();
      return;
    }
    startLoop();
  }

  function tickBoot(dt) {
    bootTimer += dt;
    if (bootTimer < 0.16) return;
    bootTimer = 0;
    if (bootIndex >= BOOT_LINES.length) {
      showSelect();
      return;
    }
    nodes.boot.textContent += (bootIndex ? "\n" : "") + BOOT_LINES[bootIndex];
    bootIndex += 1;
  }

  /* ---- select screen behaviour ------------------------------------------ */

  var activeGenre = "ALL";

  function applyFilter(genre) {
    activeGenre = genre;
    Array.prototype.forEach.call(
      nodes.filters.querySelectorAll(".arcade-filter"),
      function (chip) {
        chip.setAttribute(
          "aria-pressed",
          chip.getAttribute("data-genre") === genre ? "true" : "false"
        );
      }
    );
    var shown = 0;
    visibleCabinets().forEach(function (button) {
      var match = genre === "ALL" || button.getAttribute("data-family") === genre;
      // `hidden` rather than display:none in CSS: a filtered-out tile must
      // leave the tab order as well as the layout, and hidden does both
      // without the stylesheet having to know about filtering at all.
      button.hidden = !match;
      if (match) shown += 1;
    });
    // A hundred tiles is enough that "how many am I looking at" stops being
    // obvious from the grid itself.
    if (nodes.selectCount) {
      nodes.selectCount.textContent =
        (genre === "ALL" ? shown + " CABINETS" : shown + " OF " + CABINETS.length) +
        " · FREE PLAY · ALSO METERED";
    }
    // Repick immediately rather than letting the running demo finish. It draws
    // from the filtered pool, so leaving it alone shows a PUZZLE cabinet under
    // an FPS-only filter until it happens to die — and under reduced motion,
    // where no loop is running to cycle it, indefinitely.
    pickAttract();
    drawAttract();

    announce(
      shown + (shown === 1 ? " cabinet" : " cabinets") +
      (genre === "ALL" ? " available." : " in " + genre + ".")
    );
  }

  function visibleCabinets() {
    return Array.prototype.slice.call(nodes.grid.querySelectorAll(".arcade-cabinet"));
  }

  function refreshBests() {
    var best = loadBest();
    visibleCabinets().forEach(function (button) {
      var id = button.getAttribute("data-cabinet");
      var score = best[id];
      var label = button.querySelector(".arcade-cabinet-best");
      if (label) label.textContent = typeof score === "number" ? "BEST " + score : "UNPLAYED";
    });
  }

  /* Arrow keys walk the tile grid. The tiles are buttons, so Tab already
     works; this is the control an actual cabinet select would have, and it
     matters most on the twelve-tile grid where tabbing across three rows is
     tedious. Columns are read from layout rather than assumed, because the
     grid reflows to one column on a phone. */
  function onGridKey(event, slot) {
    if (screen !== "select") return;
    // Keyed off the resolved slot, not event.key: WASD aliases the arrows
    // everywhere else in the arcade, and reading the raw key here made this
    // the one screen where the two sets disagreed — W and S silently did
    // nothing, and the caller had already swallowed them.
    var dir = { left: -1, right: 1, up: -1, down: 1 }[slot];
    if (dir == null) return;
    var tiles = visibleCabinets().filter(function (b) { return !b.hidden; });
    var index = tiles.indexOf(doc.activeElement);
    if (index === -1) return;
    event.preventDefault();

    var step = 1;
    if (slot === "up" || slot === "down") {
      var top = tiles[0].offsetTop;
      var perRow = tiles.filter(function (b) { return b.offsetTop === top; }).length;
      step = Math.max(1, perRow);
    }
    var next = index + dir * step;
    if (next < 0 || next >= tiles.length) return;
    tiles[next].focus();
  }

  /* ---- attract mode ------------------------------------------------------

     A cabinet playing itself while nobody is at the controls. The demo hands
     are deliberately mediocre: inputs are re-rolled a few times a second, so
     it looks like someone is playing rather than like a scripted replay, and
     it dies often enough to cycle through the roster. */

  var attract = null;
  var attractTimer = 0;
  var attractInput = { left: false, right: false, up: false, down: false, fire: false, pointerX: null, pointerY: null };
  var attractRoll = 0;

  function pickAttract() {
    var pool = CABINETS.filter(function (c) {
      return activeGenre === "ALL" || (c.family || c.genre) === activeGenre;
    });
    if (!pool.length) pool = CABINETS;
    var cabinet = pool[Math.floor(random() * pool.length)];
    var instance = cabinet.make(random);
    instance.reset();
    attract = { cabinet: cabinet, game: instance };
    attractTimer = 0;
    if (nodes.attractLabel) {
      nodes.attractLabel.textContent = "ATTRACT MODE — " + cabinet.name + " · " + cabinet.genre;
    }
  }

  function drawAttract() {
    if (!attract || !nodes.attractCtx) return;
    var ctx = nodes.attractCtx;
    var ink = inkPalette();
    ctx.fillStyle = ink.bg;
    ctx.fillRect(0, 0, W, H);
    attract.game.draw(ctx, ink);
    ctx.fillStyle = ink.dim;
    ctx.font = '8px "IBM Plex Mono", monospace';
    ctx.textAlign = "center";
    ctx.fillText("DEMO", W / 2, H - 6);
    ctx.textAlign = "left";
  }

  function tickAttract(dt) {
    if (!attract) pickAttract();
    if (!attract) return;

    attractTimer += dt;
    attractRoll -= dt;
    if (attractRoll <= 0) {
      attractRoll = 0.18 + random() * 0.3;
      attractInput.left = random() < 0.3;
      attractInput.right = !attractInput.left && random() < 0.35;
      attractInput.up = random() < 0.45;
      attractInput.down = !attractInput.up && random() < 0.12;
      attractInput.fire = random() < 0.55;
    }

    if (!attract.game.over) attract.game.update(STEP, attractInput);
    drawAttract();

    // Cycle on death or on a timer, whichever comes first: some cabinets are
    // survivable enough for a demo hand to sit on one board indefinitely.
    if (attract.game.over || attractTimer > 14) pickAttract();
  }

  function showSelect() {
    screen = "select";
    current = null;
    game = null;
    nodes.boot.hidden = false;
    nodes.select.hidden = false;
    nodes.stage.hidden = true;
    nodes.over.hidden = true;
    if (!prefersStaticMotion()) startLoop();
    else stopLoop();
    // Coming back from a run hides the stage while its ← CABINETS button still
    // holds focus. Engines disagree about whether that resets activeElement to
    // body or leaves it on the now-invisible button, and the second case
    // strands keyboard and screen-reader focus in a hidden subtree — so test
    // whether focus is still on something visible rather than trusting either
    // behaviour.
    refreshBests();
    if (nodes.attractCanvas && !nodes.attractCtx) {
      nodes.attractCtx = nodes.attractCanvas.getContext("2d");
    }
    if (prefersStaticMotion()) {
      // One still frame rather than a running demo: an attract loop is
      // the kind of unrequested motion this preference exists to stop.
      if (!attract) pickAttract();
      drawAttract();
    }

    var first = nodes.select.querySelector(".arcade-cabinet:not([hidden])");
    var active = doc.activeElement;
    var activeVisible =
      active &&
      active !== doc.body &&
      overlay.contains(active) &&
      active.offsetParent !== null;
    if (first && isOpen && !activeVisible) first.focus();
    announce(
      "Cabinet select. " + CABINETS.length + " cabinets available." +
      " Use arrow keys to move between cabinets."
    );
  }

  /* ---- play -------------------------------------------------------------- */

  function startGame(id) {
    var cabinet = null;
    CABINETS.forEach(function (entry) {
      if (entry.id === id) cabinet = entry;
    });
    if (!cabinet) return;
    current = cabinet;
    game = cabinet.make(random);
    game.reset();
    screen = "play";
    paused = false;
    nodes.boot.hidden = true;
    nodes.select.hidden = true;
    nodes.over.hidden = true;
    nodes.stage.hidden = false;
    nodes.hudName.textContent = cabinet.name;
    nodes.hint.textContent =
      cabinet.controls + touchHint(cabinet) + " · P pause · ESC exit";
    nodes.canvas.setAttribute("aria-label", cabinet.name + " — " + cabinet.tagline);
    applyPad(cabinet.pad);
    resizeCanvas();
    updateHud();
    clearDirections();
    clearPointerAim();
    input.fire = false;
    pointerHeld = false;
    pointerFireSteps = 0;
    keyFire = false;
    // Starting a cabinet hides the button that had focus, which would drop
    // focus to the body and let the next Tab leave the dialog. The screen
    // itself takes it instead — it is the control now.
    nodes.canvas.focus();
    startLoop();
    announce(cabinet.name + " started. " + cabinet.controls);
  }

  /* Touch verbs differ per cabinet, and a phone visitor has no way to guess
     them from a line that only names keys. */
  function touchHint(cabinet) {
    var coarse =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(pointer: coarse)").matches;
    if (!coarse) return "";
    if (cabinet.id === "append-only") return " · or swipe to steer";
    if (cabinet.id === "race-condition") return " · or drag to move";
    if (cabinet.id === "heartbeat") return " · or swipe toward a lane";
    if (cabinet.id === "escalation") return " · or swipe to choose, tap to commit";
    return " · or drag to move, tap to fire";
  }

  function updateHud() {
    if (!game) return;
    nodes.hudScore.textContent = "RECEIPTS " + game.score;
    nodes.hudLives.textContent = "RETRIES " + Math.max(0, game.lives);
    nodes.hudExtra.textContent = game.hud ? game.hud() : "";
  }

  function endGame() {
    screen = "over";
    stopLoop();
    nodes.stage.hidden = true;
    nodes.over.hidden = false;
    nodes.over.textContent = "";

    var heading = el("p", "arcade-over-heading", "RUN COMPLETE — " + current.name);
    nodes.over.appendChild(heading);
    var bestState = recordBest(current.id, game.score);
    nodes.over.appendChild(
      el("p", "arcade-over-score", game.score + " RECEIPTS SCORED. NONE OF THEM COUNT.")
    );
    if (bestState) {
      nodes.over.appendChild(
        el(
          "p",
          "arcade-over-best",
          bestState === "stored"
            ? "NEW BEST ON " + current.name + " — STORED IN THIS BROWSER ONLY"
            : "NEW BEST ON " + current.name + " — NOT SAVED, STORAGE IS UNAVAILABLE"
        )
      );
    }
    nodes.over.appendChild(buildReceipt(current, game.score, random));

    var actions = el("div", "arcade-over-actions");
    var again = el("button", "arcade-again", "PLAY AGAIN");
    again.type = "button";
    again.addEventListener("click", function () {
      startGame(current.id);
    });
    var back = el("button", "arcade-back", "← CABINETS");
    back.type = "button";
    back.addEventListener("click", showSelect);
    actions.appendChild(again);
    actions.appendChild(back);
    nodes.over.appendChild(actions);
    again.focus();
    announce("Run complete. " + game.score + " receipts scored. A simulated receipt was issued.");
  }

  /* ---- frame loop -------------------------------------------------------- */

  function startLoop() {
    if (rafId) return;
    lastNow = 0;
    accumulator = 0;
    rafId = window.requestAnimationFrame(frame);
  }

  function stopLoop() {
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
  }

  function frame(now) {
    rafId = 0;
    if (!isOpen) return;

    var dt = lastNow ? Math.min((now - lastNow) / 1000, 0.25) : STEP;
    lastNow = now;

    // Status ticker runs on every screen: the agents are always busy.
    statusTimer += dt;
    if (statusTimer > 4 && !prefersStaticMotion()) {
      statusTimer = 0;
      statusIndex = (statusIndex + 1) % AGENT_STATUS.length;
      nodes.status.textContent = AGENT_STATUS[statusIndex];
    }

    if (screen === "boot") {
      tickBoot(dt);
    } else if (screen === "select") {
      if (!prefersStaticMotion()) tickAttract(dt);
    } else if (screen === "play" && game) {
      if (!paused) {
        accumulator += dt;
        var steps = 0;
        while (accumulator >= STEP && steps < MAX_CATCHUP) {
          advanceInput();
          advancePad();
          game.update(STEP, input);
          accumulator -= STEP;
          steps += 1;
          if (game.over) break;
        }
        if (accumulator > STEP * MAX_CATCHUP) accumulator = 0;
      }
      render();
      updateHud();
      announceTimer += dt;
      if (announceTimer > 8) {
        announceTimer = 0;
        announce(
          "Score " + game.score + " receipts, " + Math.max(0, game.lives) + " retries left."
        );
      }
      if (game.over) {
        endGame();
        return;
      }
    }

    // Only reschedule if nothing already did. tickBoot's hand-off to the
    // select screen calls startLoop() from inside this very frame, where
    // rafId has already been zeroed — without this guard that leaves two
    // loops running and stopLoop() able to cancel only the newer one.
    if (isOpen && screen !== "over" && !rafId) {
      rafId = window.requestAnimationFrame(frame);
    }
  }

  function render() {
    var ctx = nodes.ctx;
    var ink = inkPalette();
    ctx.fillStyle = ink.bg;
    ctx.fillRect(0, 0, W, H);

    ctx.fillStyle = ink.grid;
    ctx.fillRect(0, 18, W, 1);

    ctx.font = '8px "IBM Plex Mono", monospace';
    ctx.fillStyle = ink.dim;
    ctx.textAlign = "left";
    ctx.fillText(current ? current.name : "", 4, 10);
    ctx.textAlign = "right";
    ctx.fillText("RECEIPTS " + game.score, W - 4, 10);
    ctx.textAlign = "left";

    game.draw(ctx, ink);

    if (paused) {
      ctx.fillStyle = "rgba(5, 7, 13, 0.86)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = ink.brass;
      ctx.textAlign = "center";
      ctx.font = '9px "IBM Plex Mono", monospace';
      PAUSE_LINES.forEach(function (line, index) {
        ctx.fillText(line, W / 2, H / 2 - 12 + index * 14);
      });
      ctx.textAlign = "left";
    }
  }

  /* ---- input ------------------------------------------------------------- */

  var KEYS = {
    ArrowLeft: "left",
    ArrowRight: "right",
    ArrowUp: "up",
    ArrowDown: "down",
    a: "left",
    d: "right",
    w: "up",
    s: "down",
    A: "left",
    D: "right",
    W: "up",
    S: "down",
    " ": "fire",
    Spacebar: "fire"
  };

  function onKeyDown(event) {
    if (!isOpen) return;
    if (event.key === "Tab") {
      trapFocus(event);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeArcade();
      return;
    }
    if ((event.key === "p" || event.key === "P") && screen === "play") {
      paused = !paused;
      announce(paused ? "Paused." : "Resumed.");
      render();
      return;
    }
    var slot = KEYS[event.key];
    if (!slot) return;
    // On the select screen the arrows walk the tile grid instead. Space is
    // deliberately left alone here so it still activates the focused tile.
    if (screen === "select" && slot !== "fire") {
      onGridKey(event, slot);
      return;
    }
    // Only a running cabinet claims the game keys. On the menus they belong
    // to the browser, or Space could never choose a cabinet or press PLAY
    // AGAIN — both of which are buttons the keyboard has to be able to
    // activate.
    if (screen !== "play") return;
    var onControl =
      event.target &&
      typeof event.target.closest === "function" &&
      event.target.closest("button, a[href]");
    if (slot === "fire" && onControl) return;
    // Arrow keys and space scroll the page underneath otherwise.
    event.preventDefault();
    if (slot === "fire") keyFire = true;
    input[slot] = true;
    clearPointerAim();
  }

  /* Keyboard reclaims aim from the pointer: a cabinet that reads pointerX
     would otherwise pin the player where the mouse was last seen. */
  function clearPointerAim() {
    input.pointerX = null;
    input.pointerY = null;
    swipeFrom = null;
  }

  function onKeyUp(event) {
    var slot = KEYS[event.key];
    if (!slot) return;
    if (slot === "fire") {
      keyFire = false;
      // A pointer may still be holding fire, or a tap's pulse may still be
      // owed steps; releasing the key must not cancel either.
      if (pointerHeld || pointerFireSteps > 0) return;
    }
    input[slot] = false;
  }

  doc.addEventListener("keydown", onKeyDown);
  doc.addEventListener("keyup", onKeyUp);

  window.addEventListener("resize", function () {
    if (isOpen && screen === "play") resizeCanvas();
  });

  doc.addEventListener("visibilitychange", function () {
    if (doc.hidden && screen === "play") {
      paused = true;
      if (nodes.ctx) render();
    }
  });

  /* ---- launcher ---------------------------------------------------------- */

  // The launcher ships hidden so a visitor without JavaScript never sees a
  // control that cannot do anything; revealing it is this script's proof of
  // life. The wrapper carries the explanatory copy, so it is revealed too.
  launcher.hidden = false;
  if (typeof launcher.closest === "function") {
    var launchWrap = launcher.closest(".footer-arcade");
    if (launchWrap) launchWrap.hidden = false;
  }
  launcher.addEventListener("click", function () {
    openArcade();
  });

  // ?arcade=<cabinet-id> opens straight into a cabinet. Used for screenshots
  // and headless checks; harmless in a visitor's hands.
  try {
    var requested = new URLSearchParams(window.location.search).get("arcade");
    if (requested) {
      openArcade();
      if (requested !== "1" && requested !== "true") startGame(requested);
    }
  } catch (error) {
    /* URLSearchParams is absent on very old engines; the launcher still works. */
  }

  window.__amwArcade = {
    open: openArcade,
    close: closeArcade,
    start: startGame,
    select: showSelect,
    /* Buttons only. pointerX/pointerY live on the same object but hold a
       number or null, and coercing them with !! would pin a cabinet's player
       at the clamp floor instead of the requested position — use aim(). */
    press: function (name, down) {
      if (name === "pointerX" || name === "pointerY") return false;
      if (!Object.prototype.hasOwnProperty.call(input, name)) return false;
      input[name] = !!down;
      if (name === "fire") keyFire = !!down;
      return true;
    },
    aim: function (x, y) {
      input.pointerX = typeof x === "number" ? x : null;
      input.pointerY = typeof y === "number" ? y : null;
    },
    /* Advance the simulation deterministically, bypassing wall-clock timing so
       automated checks never race a frame budget. */
    step: function (count) {
      var n = count || 1;
      for (var i = 0; i < n && game && !game.over; i += 1) {
        advanceInput();
        advancePad();
        game.update(STEP, input);
      }
      if (game && nodes.ctx) {
        render();
        updateHud();
        if (game.over && screen === "play") endGame();
      }
      return this.state();
    },
    state: function () {
      return {
        open: isOpen,
        screen: screen,
        cabinet: current ? current.id : null,
        score: game ? game.score : 0,
        lives: game ? game.lives : 0,
        over: game ? game.over : false,
        paused: paused,
        reducedMotion: prefersStaticMotion(),
        // What the cabinet is actually being told, so a check can prove a tap
        // or a swipe arrived rather than inferring it from a score.
        keys: {
          left: input.left,
          right: input.right,
          up: input.up,
          down: input.down,
          fire: input.fire,
          pointerX: input.pointerX,
          pointerY: input.pointerY
        }
      };
    }
  };
})();
