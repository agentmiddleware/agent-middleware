/* Particle field for the landing page and the /concept/ study.
   -------------------------------------------------------------
   A fixed lattice of luminous points on a ground plane, displaced by
   superposed traveling sine waves in a vertex shader, drawn additively
   into an offscreen target (half-float where the GPU allows), then
   bloomed, tonemapped, vignetted and grain-dithered in a composite pass.
   Plain WebGL, no libraries, no network.

   On the landing page the field is one persistent full-page layer and
   scroll is its director: sections declare a state via data-wave="name",
   and as they cross the viewport the renderer lerps between presets —
   the open sea of the hero condenses toward order at the gateway,
   becomes a laminar stream with traveling pulses through the governed
   loop, crystallizes into a gold-swept lattice at the proof section,
   falls near-still behind the honest limitations, and returns as a soft
   swell at the close. The ground the composite paints lerps from pure
   black into the design system's ledger ink so field and page are one
   surface. UI hooks: hovering a governed-loop card fires a pulse through
   the stream from that card's position; the primary (email) CTA emits one
   from center. A page with no data-wave sections (the concept study) renders
   the constant sea as before.

   Debug/design hooks (URL query): ?t=SECONDS freezes time for
   deterministic frames, ?grid=WxH pins the lattice, ?noadapt disables
   the frame-time governor. State is exposed at window.__amwWave.

   Accessibility contract: prefers-reduced-motion or the site widget's
   data-a11y-motion="reduce" renders one still frame — re-rendered only
   when scrolling lands in a section with a different field state, a
   discrete user-initiated update rather than continuous animation. The
   widget's high-contrast mode stops rendering entirely (the stylesheets
   hide the canvas). Both react live to toggling. */
(function () {
  "use strict";

  var CONFIG = {
    fovY: 55 * (Math.PI / 180),
    camHeight: 2.1,
    basePitch: 0.115, // radians above level; sets the horizon ~3/5 down
    near: 0.5,
    far: 130,
    spanX: 52, // world width of the lattice
    z0: 2.0, // nearest row
    spanZ: 47, // world depth of the lattice
    amp: 0.38, // master wave amplitude (world units)
    dotWorld: 0.0115, // dot radius in world units before projection
    sizeMin: 1.4, // device px
    sizeMax: 40, // device px
    focusDist: 5.5, // depth-of-field focal plane
    dofStrength: 9.0, // defocus growth in device px
    fadeStart: 32,
    fadeEnd: 46,
    nearFadeStart: 2.4,
    nearFadeEnd: 3.6,
    intensity: 3.4, // point energy before tonemap
    warmth: 0.2, // 0 = pure white crests, 1 = strongly gilded
    exposure: 1.2,
    bloom: 0.95,
    vignette: 0.52,
    grain: 0.05,
    parallaxYaw: 0.05, // radians of camera drift at screen edges
    parallaxPitch: 0.024,
    pointerStrength: 0.34, // world units of pointer swell
    staticTime: 7.3, // frozen phase for reduced-motion / ?t default
    dprCap: 2,
    // Default backing-store width, in device pixels, for a page that opts into
    // the low-resolution framebuffer (see pixelWidth below). 320 is a
    // console-era framebuffer width.
    pixelWidth: 320,
    // Lattice sizes, densest first; the governor walks down this list.
    levels: [
      [896, 640],
      [768, 544],
      [640, 448],
      [512, 352],
      [416, 288],
      [320, 224],
    ],
  };

  var canvas = document.getElementById("wave-canvas");
  var statsEl = document.getElementById("wave-stats");

  /* Rendering the field into a narrow framebuffer and letting the compositor
     upscale it is what makes the wave read as part of the pixel grid rather
     than as a photograph behind it — but only on a page that also paints it
     back with image-rendering:pixelated. This file is shared, and /concept/
     has its own stylesheet and its own full-resolution treatment, where a
     smoothly stretched 320px buffer would just be a blurred background.

     So the cap is opt-in, declared by the page on the canvas itself:
     data-pixel-width. No attribute, no cap. */
  var pixelWidth = Infinity;
  if (canvas && canvas.hasAttribute("data-pixel-width")) {
    /* The whole attribute has to be a positive integer, not merely start with
       one. parseInt would take "1.5" as 1 — a one-pixel-wide framebuffer, which
       is a stranger failure than any fallback — and "320px" as 320, quietly
       accepting a unit this attribute does not carry. Anything that is not a
       run of digits falls back to the configured default rather than resizing
       the field to whatever the string happened to begin with. */
    var declaredWidth = canvas.getAttribute("data-pixel-width").trim();
    pixelWidth =
      /^[0-9]+$/.test(declaredWidth) && Number(declaredWidth) > 0
        ? Number(declaredWidth)
        : CONFIG.pixelWidth;
  }
  if (!canvas) return;

  var params = new URLSearchParams(window.location.search);
  var frozenTime = null;
  if (params.has("t")) {
    var parsedT = parseFloat(params.get("t"));
    if (isFinite(parsedT)) frozenTime = parsedT;
  }
  var pinnedGrid = null;
  if (params.has("grid")) {
    var m = /^(\d+)x(\d+)$/.exec(params.get("grid") || "");
    if (m) pinnedGrid = [Math.min(+m[1], 2048), Math.min(+m[2], 2048)];
  }
  var adaptEnabled = !params.has("noadapt") && !pinnedGrid;

  /* ---- context ---------------------------------------------------------- */

  var gl = null;
  var isGL2 = false;
  try {
    gl = canvas.getContext("webgl2", CTX_OPTS());
    isGL2 = !!gl;
    if (!gl) gl = canvas.getContext("webgl", CTX_OPTS());
  } catch (error) {
    gl = null;
  }

  function CTX_OPTS() {
    return {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      powerPreference: "high-performance",
      preserveDrawingBuffer: false,
    };
  }

  var state = {
    mode: "off",
    count: 0,
    hdr: false,
    frame: 0,
    lastFrameMs: 0,
  };
  window.__amwWave = state;

  if (!gl) {
    // concept.css keeps the static gradient fallback underneath.
    document.documentElement.classList.add("wave-dead");
    setStats("STATIC BACKDROP · NO WEBGL");
    return;
  }

  /* ---- shaders ---------------------------------------------------------- */

  var FRAG_PRECISION =
    "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n" +
    "#else\nprecision mediump float;\n#endif\n";

  var POINT_VS =
    "precision highp float;\n" +
    "attribute vec2 aGrid;\n" +
    "uniform mat4 uVP;\n" +
    "uniform vec2 uGridN;\n" + // columns-1, rows-1
    "uniform vec3 uSpan;\n" + // spanX, z0, spanZ
    "uniform float uTime;\n" +
    "uniform float uAmp;\n" +
    "uniform float uCamY;\n" +
    "uniform vec3 uPointer;\n" + // world x, world z, strength
    "uniform vec2 uSizeRange;\n" +
    "uniform float uPointScale;\n" + // device px per world unit at dist 1
    "uniform float uDotWorld;\n" +
    "uniform vec2 uDof;\n" + // focus dist, strength px
    "uniform vec4 uFade;\n" + // far start/end, near start/end
    "uniform float uIntensity;\n" +
    "uniform float uWarmth;\n" +
    "uniform vec3 uMorph;\n" + // order, flow, crystal
    "uniform float uPulseF[3];\n" + // pulse front x positions
    "uniform float uPulseS[3];\n" + // pulse strengths
    "varying vec3 vColor;\n" +
    "void main() {\n" +
    "  vec2 g = aGrid / uGridN;\n" +
    "  float x = (g.x - 0.5) * uSpan.x;\n" +
    "  float z = -(uSpan.y + g.y * uSpan.z);\n" +
    "  vec2 p = vec2(x, z);\n" +
    "  float t = uTime;\n" +
    "  float order = uMorph.x;\n" +
    "  float flow = uMorph.y;\n" +
    "  float crystal = uMorph.z;\n" +
    // Superposed traveling waves: three planar directions, one radial
    // ripple, one very broad slow swell. Coefficients sum to ~3.5.
    "  float h = sin(x * 0.42 + t * 0.50)\n" +
    "          + 0.62 * sin(z * 0.35 - t * 0.38)\n" +
    "          + 0.40 * sin((x + z) * 0.24 + t * 0.60)\n" +
    "          + 0.30 * sin(length(p - vec2(6.0, -14.0)) * 0.55 - t * 0.85)\n" +
    "          + 1.15 * sin(x * 0.085 - t * 0.14) * sin(z * 0.071 + t * 0.10);\n" +
    // Order damps the residual chop on top of the preset's amplitude cut,
    // and crystal flattens the plane entirely.
    "  float y = h * uAmp * (1.0 - order * 0.45) * (1.0 - crystal);\n" +
    // Laminar flow: sharpened crest trains traveling +x — reads as calls
    // moving through the gateway. Lifts the surface a touch as it passes.
    "  float fl = flow * pow(0.5 + 0.5 * sin(x * 0.30 - t * 2.6 + z * 0.05), 6.0);\n" +
    "  y += fl * 0.10;\n" +
    // Interaction pulses: bright fronts sweeping +x from where the UI
    // fired them.
    "  float pulse = 0.0;\n" +
    "  for (int i = 0; i < 3; i++) {\n" +
    "    float dpx = x - uPulseF[i];\n" +
    "    pulse += uPulseS[i] * exp(-dpx * dpx / 7.0);\n" +
    "  }\n" +
    "  y += pulse * 0.08;\n" +
    "  float pd = length(p - uPointer.xy);\n" +
    "  y += uPointer.z * exp(-pd * pd * 0.10) * sin(t * 1.8 - pd * 1.1);\n" +
    "  vec4 world = vec4(x, y, z, 1.0);\n" +
    "  gl_Position = uVP * world;\n" +
    "  float dist = max(length(world.xyz - vec3(0.0, uCamY, 0.0)), 0.001);\n" +
    // Luminance: crests catch light, troughs sink; a per-dot hash gives a
    // slow shimmer without breaking the lattice.
    "  float crest = clamp(0.5 + 0.5 * (h / 3.5), 0.0, 1.0);\n" +
    "  float jig = fract(sin(dot(aGrid, vec2(127.1, 311.7))) * 43758.5453);\n" +
    "  float lum = mix(0.30, 1.0, crest * crest);\n" +
    "  lum *= 0.86 + 0.28 * sin(t * 0.9 + jig * 6.2831);\n" +
    // Order evens the sea's tonal range and rules the lattice like graph
    // paper: every eighth row and column brightens into a survey line.
    "  lum = mix(lum, 0.55, order * 0.55);\n" +
    "  float lineB = max(step(mod(aGrid.x, 8.0), 0.5), step(mod(aGrid.y, 8.0), 0.5));\n" +
    "  lum += order * lineB * 0.35;\n" +
    // Crystal: per-cell blocks twinkling slowly, plus a gold signing sweep
    // that crosses the lattice like a verification pass.
    "  float cell = fract(sin(dot(floor(aGrid / 8.0), vec2(12.9898, 78.233))) * 43758.5453);\n" +
    "  float cellLum = 0.30 + 0.70 * step(0.55, fract(cell + t * 0.04));\n" +
    "  lum = mix(lum, lum * cellLum + 0.10, crystal);\n" +
    "  float sweepX = mod(t * 5.0, 90.0) - 45.0;\n" +
    "  float sweep = crystal * exp(-(x - sweepX) * (x - sweepX) / 16.0);\n" +
    "  lum += fl * 0.9 + pulse * 0.9 + sweep * 0.8;\n" +
    "  lum *= 1.0 - smoothstep(uFade.x, uFade.y, dist);\n" +
    "  lum *= smoothstep(uFade.z, uFade.w, dist);\n" +
    // Projected size plus a defocus circle-of-confusion; brightness is
    // conserved so blurred dots spread, not brighten.
    "  float sizeFocus = uPointScale * uDotWorld / dist;\n" +
    "  float coc = uDof.y * abs(dist - uDof.x) / dist;\n" +
    "  float size = clamp(sizeFocus + coc, uSizeRange.x, uSizeRange.y);\n" +
    // Exponent < 2 under-conserves on purpose: strictly physical falloff
    // buries the far field, and the horizon should read as atmosphere.
    "  float energy = pow(sizeFocus / size, 1.6);\n" +
    "  gl_PointSize = size;\n" +
    "  lum *= energy * uIntensity;\n" +
    "  vec3 tint = mix(vec3(0.80, 0.89, 1.0), vec3(1.0, 0.93, 0.72),\n" +
    "                  crest * uWarmth * 2.0);\n" +
    // The signing sweep and the crystal state pull toward brass-gold.
    "  tint = mix(tint, vec3(1.0, 0.86, 0.55), min(crystal * 0.45 + sweep, 1.0));\n" +
    "  vColor = tint * lum;\n" +
    "}\n";

  var POINT_FS =
    FRAG_PRECISION +
    "varying vec3 vColor;\n" +
    "void main() {\n" +
    "  vec2 d = gl_PointCoord - 0.5;\n" +
    "  float r2 = dot(d, d) * 4.0;\n" +
    "  float fall = max(exp(-r2 * 3.0) - 0.0498, 0.0) * 1.052;\n" +
    "  gl_FragColor = vec4(vColor * fall, 1.0);\n" +
    "}\n";

  var QUAD_VS =
    "precision highp float;\n" +
    "attribute vec2 aPos;\n" +
    "varying vec2 vUv;\n" +
    "void main() {\n" +
    "  vUv = aPos * 0.5 + 0.5;\n" +
    "  gl_Position = vec4(aPos, 0.0, 1.0);\n" +
    "}\n";

  var DOWNSAMPLE_FS =
    FRAG_PRECISION +
    "uniform sampler2D uTex;\n" +
    "uniform vec2 uTexel;\n" +
    "varying vec2 vUv;\n" +
    "void main() {\n" +
    "  vec3 c = texture2D(uTex, vUv + uTexel * vec2(-1.0, -1.0)).rgb\n" +
    "         + texture2D(uTex, vUv + uTexel * vec2(1.0, -1.0)).rgb\n" +
    "         + texture2D(uTex, vUv + uTexel * vec2(-1.0, 1.0)).rgb\n" +
    "         + texture2D(uTex, vUv + uTexel * vec2(1.0, 1.0)).rgb;\n" +
    "  gl_FragColor = vec4(c * 0.25, 1.0);\n" +
    "}\n";

  var BLUR_FS =
    FRAG_PRECISION +
    "uniform sampler2D uTex;\n" +
    "uniform vec2 uDir;\n" + // texel step along one axis
    "varying vec2 vUv;\n" +
    "void main() {\n" +
    "  vec3 c = texture2D(uTex, vUv).rgb * 0.227027;\n" +
    "  c += texture2D(uTex, vUv + uDir * 1.384615).rgb * 0.316216;\n" +
    "  c += texture2D(uTex, vUv - uDir * 1.384615).rgb * 0.316216;\n" +
    "  c += texture2D(uTex, vUv + uDir * 3.230769).rgb * 0.070270;\n" +
    "  c += texture2D(uTex, vUv - uDir * 3.230769).rgb * 0.070270;\n" +
    "  gl_FragColor = vec4(c, 1.0);\n" +
    "}\n";

  var COMPOSITE_FS =
    FRAG_PRECISION +
    "uniform sampler2D uScene;\n" +
    "uniform sampler2D uBloom;\n" +
    "uniform float uExposure;\n" +
    "uniform float uBloomStrength;\n" +
    "uniform float uVignette;\n" +
    "uniform float uGrain;\n" +
    "uniform float uTime;\n" +
    "uniform vec2 uRes;\n" +
    "uniform vec3 uGround;\n" + // flat base the field floats over
    "varying vec2 vUv;\n" +
    "void main() {\n" +
    "  vec3 c = texture2D(uScene, vUv).rgb\n" +
    "         + texture2D(uBloom, vUv).rgb * uBloomStrength;\n" +
    // Filmic-ish shoulder: crest hotspots roll off instead of clipping.
    "  c = 1.0 - exp(-c * uExposure);\n" +
    "  vec2 v = (vUv - 0.5) * vec2(uRes.x / uRes.y, 1.0) * 0.85;\n" +
    "  c *= mix(1.0, smoothstep(1.05, 0.30, length(v)), uVignette);\n" +
    // The ground is added after the vignette so it stays a flat, exact
    // match for the design system's surfaces; only the field vignettes.
    "  c += uGround;\n" +
    // Blue-noise-ish grain: hides 8-bit banding in the dark gradients.
    "  float n = fract(sin(dot(gl_FragCoord.xy + uTime, vec2(12.9898, 78.233)))\n" +
    "                  * 43758.5453);\n" +
    "  c += (n - 0.5) * uGrain * 0.12;\n" +
    "  gl_FragColor = vec4(max(c, 0.0), 1.0);\n" +
    "}\n";

  /* ---- tiny GL + mat4 helpers ------------------------------------------- */

  function compile(type, source) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      var log = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error("shader: " + log);
    }
    return shader;
  }

  function program(vsSource, fsSource, attribName) {
    var p = gl.createProgram();
    gl.attachShader(p, compile(gl.VERTEX_SHADER, vsSource));
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fsSource));
    // Every draw call binds its sole attribute at location 0.
    gl.bindAttribLocation(p, 0, attribName);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error("link: " + gl.getProgramInfoLog(p));
    }
    var uniforms = {};
    var n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < n; i++) {
      var info = gl.getActiveUniform(p, i);
      uniforms[info.name] = gl.getUniformLocation(p, info.name);
    }
    return { p: p, u: uniforms };
  }

  function mat4Perspective(out, fovy, aspect, near, far) {
    var f = 1 / Math.tan(fovy / 2);
    var nf = 1 / (near - far);
    out.set([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
    return out;
  }

  function mat4Multiply(out, a, b) {
    var r = new Float32Array(16);
    for (var c = 0; c < 4; c++) {
      for (var row = 0; row < 4; row++) {
        r[c * 4 + row] =
          a[row] * b[c * 4] +
          a[4 + row] * b[c * 4 + 1] +
          a[8 + row] * b[c * 4 + 2] +
          a[12 + row] * b[c * 4 + 3];
      }
    }
    out.set(r);
    return out;
  }

  function mat4RotX(out, rad) {
    var s = Math.sin(rad);
    var c = Math.cos(rad);
    out.set([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]);
    return out;
  }

  function mat4RotY(out, rad) {
    var s = Math.sin(rad);
    var c = Math.cos(rad);
    out.set([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]);
    return out;
  }

  function mat4Translation(out, x, y, z) {
    out.set([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]);
    return out;
  }

  /* ---- programs, geometry, render targets ------------------------------- */

  var progPoints, progDown, progBlur, progComposite;
  function buildPrograms() {
    progPoints = program(POINT_VS, POINT_FS, "aGrid");
    progDown = program(QUAD_VS, DOWNSAMPLE_FS, "aPos");
    progBlur = program(QUAD_VS, BLUR_FS, "aPos");
    progComposite = program(QUAD_VS, COMPOSITE_FS, "aPos");
  }
  try {
    buildPrograms();
  } catch (error) {
    document.documentElement.classList.add("wave-dead");
    setStats("STATIC BACKDROP · SHADER UNSUPPORTED");
    return;
  }

  // One big triangle covering the screen.
  var quadBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 3, -1, -1, 3]),
    gl.STATIC_DRAW
  );

  var maxPointSize = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)[1] || 64;

  var gridBuffer = gl.createBuffer();
  var gridCols = 0;
  var gridRows = 0;
  var levelIndex = pickInitialLevel();

  function pickInitialLevel() {
    if (pinnedGrid) return -1;
    var coarse =
      Math.min(window.screen.width, window.screen.height) < 760 ||
      navigator.maxTouchPoints > 1;
    var memory = navigator.deviceMemory || 8;
    if (coarse || memory <= 4) return 2;
    return 0;
  }

  function buildGrid() {
    var size = pinnedGrid || CONFIG.levels[levelIndex];
    gridCols = size[0];
    gridRows = size[1];
    var data = new Uint16Array(gridCols * gridRows * 2);
    var k = 0;
    for (var j = 0; j < gridRows; j++) {
      for (var i = 0; i < gridCols; i++) {
        data[k++] = i;
        data[k++] = j;
      }
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, gridBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    state.count = gridCols * gridRows;
    refreshStats();
  }

  // Offscreen targets: full-res scene + two quarter-res bloom ping-pongs.
  var halfFloatType = null;
  if (isGL2) {
    if (gl.getExtension("EXT_color_buffer_float")) halfFloatType = gl.HALF_FLOAT;
  } else {
    var extHalf = gl.getExtension("OES_texture_half_float");
    if (extHalf && gl.getExtension("OES_texture_half_float_linear")) {
      halfFloatType = extHalf.HALF_FLOAT_OES;
    }
  }

  var targets = { scene: null, bloomA: null, bloomB: null };

  function makeTarget(w, h, type) {
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var internal = gl.RGBA;
    if (isGL2 && type === gl.HALF_FLOAT) internal = gl.RGBA16F;
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, gl.RGBA, type, null);
    var fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(
      gl.FRAMEBUFFER,
      gl.COLOR_ATTACHMENT0,
      gl.TEXTURE_2D,
      tex,
      0
    );
    var ok =
      gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    if (!ok) {
      gl.deleteTexture(tex);
      gl.deleteFramebuffer(fbo);
      return null;
    }
    return { tex: tex, fbo: fbo, w: w, h: h };
  }

  function destroyTarget(t) {
    if (!t) return;
    gl.deleteTexture(t.tex);
    gl.deleteFramebuffer(t.fbo);
  }

  function allocTargets(w, h) {
    destroyTarget(targets.scene);
    destroyTarget(targets.bloomA);
    destroyTarget(targets.bloomB);
    var bw = Math.max(1, Math.round(w / 4));
    var bh = Math.max(1, Math.round(h / 4));
    var type = halfFloatType;
    if (type !== null) {
      targets.scene = makeTarget(w, h, type);
      targets.bloomA = targets.scene && makeTarget(bw, bh, type);
      targets.bloomB = targets.bloomA && makeTarget(bw, bh, type);
    }
    state.hdr = !!targets.bloomB;
    if (!targets.bloomB) {
      // Half float unrenderable here: rebuild the chain at 8 bits.
      destroyTarget(targets.scene);
      destroyTarget(targets.bloomA);
      targets.scene = makeTarget(w, h, gl.UNSIGNED_BYTE);
      targets.bloomA = targets.scene && makeTarget(bw, bh, gl.UNSIGNED_BYTE);
      targets.bloomB = targets.bloomA && makeTarget(bw, bh, gl.UNSIGNED_BYTE);
    }
    if (!targets.bloomB) {
      // No renderable target at all: bow out to the static backdrop.
      dead = true;
      document.documentElement.classList.add("wave-dead");
      setStats("STATIC BACKDROP · NO RENDER TARGET");
      return;
    }
    refreshStats();
  }

  /* ---- sizing ------------------------------------------------------------ */

  var viewW = 0;
  var viewH = 0;
  var dpr = 1;
  var pointScale = 1;
  var projection = new Float32Array(16);
  var needResize = true;

  function resize() {
    var cssW = Math.max(1, canvas.clientWidth);
    var cssH = Math.max(1, canvas.clientHeight);
    // One scale for both axes, so the upscaled pixels stay square. Capping by
    // width rather than by area keeps the pixel size stable as the viewport
    // gets taller, which is what a fixed full-page layer does on scroll.
    dpr = Math.min(
      window.devicePixelRatio || 1,
      CONFIG.dprCap,
      pixelWidth / cssW
    );
    var w = Math.max(1, Math.round(cssW * dpr));
    var h = Math.max(1, Math.round(cssH * dpr));
    if (w === viewW && h === viewH && !needResize) return;
    viewW = canvas.width = w;
    viewH = canvas.height = h;
    pointScale = h / 2 / Math.tan(CONFIG.fovY / 2);
    mat4Perspective(projection, CONFIG.fovY, w / h, CONFIG.near, CONFIG.far);
    allocTargets(w, h);
    needResize = false;
  }

  window.addEventListener("resize", function () {
    needResize = true;
    measureSections();
    scheduleFrame();
  });

  /* ---- pointer ----------------------------------------------------------- */

  var pointerOk = window.matchMedia("(hover: hover)").matches;
  var pointerNdc = { x: 0, y: 0 };
  var eased = { yaw: 0, pitch: 0, px: 0, pz: -14, strength: 0 };
  var pointerActive = false;

  if (pointerOk) {
    window.addEventListener(
      "pointermove",
      function (event) {
        // No scheduleFrame here: the animated loop re-arms itself, and in
        // the static (reduced-motion) mode a pointer must not trigger
        // full re-renders of an intentionally still frame.
        pointerNdc.x = (event.clientX / window.innerWidth) * 2 - 1;
        pointerNdc.y = 1 - (event.clientY / window.innerHeight) * 2;
        pointerActive = true;
      },
      { passive: true }
    );
    document.documentElement.addEventListener("pointerleave", function () {
      pointerActive = false;
    });
    window.addEventListener("blur", function () {
      pointerActive = false;
    });
  }

  function easePointer() {
    var k = 0.045;
    var targetYaw = pointerActive ? -pointerNdc.x * CONFIG.parallaxYaw : 0;
    var targetPitch = pointerActive ? pointerNdc.y * CONFIG.parallaxPitch : 0;
    eased.yaw += (targetYaw - eased.yaw) * k;
    eased.pitch += (targetPitch - eased.pitch) * k;

    // Cast the pointer onto the ground plane for the local swell.
    var strengthTarget = 0;
    if (pointerActive && viewH > 0) {
      var tanF = Math.tan(CONFIG.fovY / 2);
      var dx = pointerNdc.x * tanF * (viewW / viewH);
      var dy = pointerNdc.y * tanF;
      var pitch = CONFIG.basePitch + eased.pitch;
      // Rotate (dx, dy, -1) by camera pitch about X, then yaw about Y.
      var cy = Math.cos(pitch);
      var sy = Math.sin(pitch);
      var vy = dy * cy + sy;
      var vz = dy * sy - cy;
      var yaw = eased.yaw;
      var cyaw = Math.cos(yaw);
      var syaw = Math.sin(yaw);
      var vx = dx * cyaw + vz * syaw;
      vz = -dx * syaw + vz * cyaw;
      if (vy < -0.02) {
        var hit = CONFIG.camHeight / -vy;
        if (hit < 60) {
          eased.px += (vx * hit - eased.px) * k;
          eased.pz += (vz * hit - eased.pz) * k;
          strengthTarget = CONFIG.pointerStrength;
        }
      }
    }
    eased.strength += (strengthTarget - eased.strength) * 0.03;
  }

  /* ---- motion & contrast preferences ------------------------------------ */

  var mediaReduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  function prefersStatic() {
    return (
      frozenTime !== null ||
      mediaReduced.matches ||
      document.documentElement.getAttribute("data-a11y-motion") === "reduce"
    );
  }

  function contrastHigh() {
    return (
      document.documentElement.getAttribute("data-a11y-contrast") === "high"
    );
  }

  if (mediaReduced.addEventListener) {
    mediaReduced.addEventListener("change", modeChanged);
  }
  new MutationObserver(modeChanged).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-a11y-motion", "data-a11y-contrast"],
  });

  function modeChanged() {
    running = false; // re-evaluated by scheduleFrame
    scheduleFrame();
  }

  /* ---- scroll director ---------------------------------------------------
     Sections carry data-wave="preset"; as they cross the viewport the field
     lerps between these states. Fields: amp and bright scale the base
     amplitude/intensity; order, flow, crystal feed the shader morphs;
     warmth tints crests; ground lerps the composite base from pure black
     (0) to the design system's ledger ink (1). */

  var PRESETS = {
    sea: { amp: 1.0, order: 0.0, flow: 0.0, crystal: 0.0, bright: 1.0, warmth: 0.2, ground: 0.0 },
    condense: { amp: 0.6, order: 0.5, flow: 0.25, crystal: 0.0, bright: 0.45, warmth: 0.22, ground: 0.75 },
    order: { amp: 0.35, order: 0.75, flow: 0.25, crystal: 0.0, bright: 0.28, warmth: 0.22, ground: 1.0 },
    stream: { amp: 0.4, order: 0.8, flow: 1.0, crystal: 0.0, bright: 0.55, warmth: 0.35, ground: 1.0 },
    crystal: { amp: 0.35, order: 1.0, flow: 0.15, crystal: 1.0, bright: 0.5, warmth: 0.55, ground: 1.0 },
    quiet: { amp: 0.3, order: 0.5, flow: 0.1, crystal: 0.0, bright: 0.2, warmth: 0.2, ground: 1.0 },
    gridquiet: { amp: 0.22, order: 0.7, flow: 0.1, crystal: 0.35, bright: 0.28, warmth: 0.25, ground: 1.0 },
    dark: { amp: 0.15, order: 0.4, flow: 0.0, crystal: 0.0, bright: 0.1, warmth: 0.15, ground: 1.0 },
    ember: { amp: 0.6, order: 0.25, flow: 0.1, crystal: 0.0, bright: 0.42, warmth: 0.35, ground: 1.0 },
  };
  var PRESET_KEYS = ["amp", "order", "flow", "crystal", "bright", "warmth", "ground"];
  var GROUND_INK = [11 / 255, 16 / 255, 32 / 255]; // --ink #0b1020

  var sections = []; // { center, preset } in document coordinates
  var pageHeight = 0;
  var fieldNow = blankPreset(PRESETS.sea);
  var dominantName = "sea";

  function blankPreset(from) {
    var out = {};
    for (var i = 0; i < PRESET_KEYS.length; i++) {
      out[PRESET_KEYS[i]] = from[PRESET_KEYS[i]];
    }
    return out;
  }

  function measureSections() {
    var nodes = document.querySelectorAll("[data-wave]");
    var list = [];
    var scrollTop = window.pageYOffset || 0;
    for (var i = 0; i < nodes.length; i++) {
      var preset = PRESETS[nodes[i].getAttribute("data-wave")];
      if (!preset) continue;
      var rect = nodes[i].getBoundingClientRect();
      list.push({
        center: scrollTop + rect.top + rect.height / 2,
        preset: preset,
        name: nodes[i].getAttribute("data-wave"),
      });
    }
    list.sort(function (a, b) {
      return a.center - b.center;
    });
    sections = list;
    pageHeight = document.documentElement.scrollHeight;
  }

  // Blend of the two presets whose section centers bracket the viewport
  // focus. Returns the target values and the dominant preset's name.
  function directorTarget() {
    if (!sections.length) {
      dominantName = "sea";
      return PRESETS.sea;
    }
    var focus = (window.pageYOffset || 0) + window.innerHeight * 0.55;
    var below = sections[0];
    var above = null;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].center <= focus) {
        above = sections[i];
      } else {
        below = sections[i];
        break;
      }
      below = sections[i];
    }
    if (!above) {
      dominantName = sections[0].name;
      return sections[0].preset;
    }
    if (above === below) {
      dominantName = above.name;
      return above.preset;
    }
    var span = below.center - above.center;
    var raw = span > 0 ? (focus - above.center) / span : 1;
    var k = raw * raw * (3 - 2 * raw); // smoothstep
    dominantName = k < 0.5 ? above.name : below.name;
    var out = {};
    for (var j = 0; j < PRESET_KEYS.length; j++) {
      var key = PRESET_KEYS[j];
      out[key] = above.preset[key] + (below.preset[key] - above.preset[key]) * k;
    }
    return out;
  }

  function directorTick(snap) {
    // Layout can shift under us (fonts, images); re-measure when the
    // document height moves.
    if (document.documentElement.scrollHeight !== pageHeight) {
      measureSections();
    }
    var target = directorTarget();
    var k = snap ? 1 : 0.08;
    for (var i = 0; i < PRESET_KEYS.length; i++) {
      var key = PRESET_KEYS[i];
      fieldNow[key] += (target[key] - fieldNow[key]) * k;
    }
  }

  /* ---- interaction pulses -------------------------------------------------
     UI elements fire bright fronts that sweep +x through the field. At most
     three live at once; each decays over ~4 seconds. */

  var PULSE_SPEED = 9.0; // world units per second
  var pulses = []; // { x, born }

  function emitPulse(x01) {
    if (prefersStatic() || contrastHigh() || dead) return;
    pulses.push({
      x: (Math.min(Math.max(x01, 0), 1) - 0.5) * CONFIG.spanX,
      born: waveTime,
    });
    if (pulses.length > 3) pulses.shift();
  }
  state.emitPulse = emitPulse;

  var pulseFronts = new Float32Array(3);
  var pulseStrengths = new Float32Array(3);

  function updatePulses() {
    for (var i = 0; i < 3; i++) {
      pulseFronts[i] = 0;
      pulseStrengths[i] = 0;
    }
    for (var j = pulses.length - 1; j >= 0; j--) {
      var age = waveTime - pulses[j].born;
      if (age > 4 || age < 0) {
        pulses.splice(j, 1);
        continue;
      }
      pulseFronts[j] = pulses[j].x + age * PULSE_SPEED;
      pulseStrengths[j] = 1.3 * Math.exp(-age * 0.9);
    }
    state.pulses = pulses.length;
  }

  // Wire the governed-loop cards and the primary CTA to the field. Cards
  // map left-to-right onto the lattice, so a hover fires from "its"
  // station; the CTA ripples from center. Hover-capable devices only.
  function wireInteractions() {
    if (!pointerOk) return;
    var cards = document.querySelectorAll(".loop-track li");
    for (var i = 0; i < cards.length; i++) {
      (function (index, total) {
        cards[index].addEventListener(
          "pointerenter",
          function () {
            emitPulse((index + 0.5) / total);
          },
          { passive: true }
        );
      })(i, cards.length);
    }
    var ctas = document.querySelectorAll(".button-wave");
    for (var c = 0; c < ctas.length; c++) {
      ctas[c].addEventListener(
        "pointerenter",
        function () {
          emitPulse(0.5);
        },
        { passive: true }
      );
    }
  }

  // In static (reduced-motion) mode the field re-renders only when the
  // scroll lands in a section with a different state — a discrete,
  // user-initiated update, not an animation.
  var staticRenderedFor = "";
  window.addEventListener(
    "scroll",
    function () {
      if (!sections.length || dead || contrastHigh()) return;
      if (prefersStatic() && dominantNameAtFocus() !== staticRenderedFor) {
        scheduleFrame();
      }
    },
    { passive: true }
  );

  function dominantNameAtFocus() {
    directorTarget();
    return dominantName;
  }

  /* ---- render ------------------------------------------------------------ */

  var viewMatrix = new Float32Array(16);
  var vp = new Float32Array(16);
  var scratchA = new Float32Array(16);
  var scratchB = new Float32Array(16);

  function drawScene(time) {
    resize();
    if (dead) return;

    gl.disable(gl.DEPTH_TEST);
    gl.bindFramebuffer(gl.FRAMEBUFFER, targets.scene.fbo);
    gl.viewport(0, 0, viewW, viewH);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    // view = RotX(-pitch) * RotY(-yaw) * T(0, -camHeight, 0)
    mat4RotY(scratchA, -eased.yaw);
    mat4Translation(scratchB, 0, -CONFIG.camHeight, 0);
    mat4Multiply(viewMatrix, scratchA, scratchB);
    mat4RotX(scratchA, -(CONFIG.basePitch + eased.pitch));
    mat4Multiply(viewMatrix, scratchA, viewMatrix);
    mat4Multiply(vp, projection, viewMatrix);

    gl.useProgram(progPoints.p);
    var u = progPoints.u;
    gl.uniformMatrix4fv(u.uVP, false, vp);
    gl.uniform2f(u.uGridN, gridCols - 1, gridRows - 1);
    gl.uniform3f(u.uSpan, CONFIG.spanX, CONFIG.z0, CONFIG.spanZ);
    gl.uniform1f(u.uTime, time);
    gl.uniform1f(u.uAmp, CONFIG.amp * fieldNow.amp);
    gl.uniform1f(u.uCamY, CONFIG.camHeight);
    gl.uniform3f(u.uPointer, eased.px, eased.pz, eased.strength);
    gl.uniform3f(u.uMorph, fieldNow.order, fieldNow.flow, fieldNow.crystal);
    updatePulses();
    // Drivers report array uniforms as either "name[0]" or bare "name".
    gl.uniform1fv(u["uPulseF[0]"] || u.uPulseF, pulseFronts);
    gl.uniform1fv(u["uPulseS[0]"] || u.uPulseS, pulseStrengths);
    gl.uniform2f(
      u.uSizeRange,
      CONFIG.sizeMin * dpr,
      Math.min(CONFIG.sizeMax * dpr, maxPointSize)
    );
    gl.uniform1f(u.uPointScale, pointScale);
    gl.uniform1f(u.uDotWorld, CONFIG.dotWorld);
    gl.uniform2f(u.uDof, CONFIG.focusDist, CONFIG.dofStrength * dpr);
    gl.uniform4f(
      u.uFade,
      CONFIG.fadeStart,
      CONFIG.fadeEnd,
      CONFIG.nearFadeStart,
      CONFIG.nearFadeEnd
    );
    gl.uniform1f(u.uIntensity, CONFIG.intensity * fieldNow.bright);
    gl.uniform1f(u.uWarmth, fieldNow.warmth);

    gl.bindBuffer(gl.ARRAY_BUFFER, gridBuffer);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.UNSIGNED_SHORT, false, 0, 0);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    gl.drawArrays(gl.POINTS, 0, gridCols * gridRows);
    gl.disable(gl.BLEND);

    // Bloom chain: downsample to quarter res, then two separable blurs.
    quadPass(progDown, targets.bloomA, function (uq) {
      gl.uniform2f(uq.uTexel, 1 / viewW, 1 / viewH);
      bindTex(uq.uTex, targets.scene.tex, 0);
    });
    blur(targets.bloomA, targets.bloomB, 1, 0);
    blur(targets.bloomB, targets.bloomA, 0, 1);
    blur(targets.bloomA, targets.bloomB, 2, 0);
    blur(targets.bloomB, targets.bloomA, 0, 2);

    // Composite to the screen.
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, viewW, viewH);
    gl.useProgram(progComposite.p);
    var uc = progComposite.u;
    bindTex(uc.uScene, targets.scene.tex, 0);
    bindTex(uc.uBloom, targets.bloomA.tex, 1);
    gl.uniform1f(uc.uExposure, CONFIG.exposure);
    gl.uniform1f(uc.uBloomStrength, CONFIG.bloom);
    gl.uniform1f(uc.uVignette, CONFIG.vignette);
    gl.uniform1f(uc.uGrain, CONFIG.grain);
    gl.uniform1f(uc.uTime, time % 64.0);
    gl.uniform2f(uc.uRes, viewW, viewH);
    gl.uniform3f(
      uc.uGround,
      GROUND_INK[0] * fieldNow.ground,
      GROUND_INK[1] * fieldNow.ground,
      GROUND_INK[2] * fieldNow.ground
    );
    drawQuad();
  }

  function quadPass(prog, target, setUniforms) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo);
    gl.viewport(0, 0, target.w, target.h);
    gl.useProgram(prog.p);
    setUniforms(prog.u);
    drawQuad();
  }

  function blur(from, to, dx, dy) {
    quadPass(progBlur, to, function (uq) {
      bindTex(uq.uTex, from.tex, 0);
      gl.uniform2f(uq.uDir, dx / from.w, dy / from.h);
    });
  }

  function drawQuad() {
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function bindTex(location, tex, unit) {
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.uniform1i(location, unit);
  }

  /* ---- animation loop with a frame-time governor ------------------------- */

  var dead = false;
  var running = false;
  var rafId = 0;
  var waveTime = 0;
  var lastNow = 0;
  var slowFrames = 0;
  var sampleFrames = 0;

  function frame(now) {
    rafId = 0;
    if (dead) return;
    if (contrastHigh()) {
      state.mode = "off";
      setStats("FIELD PAUSED · HIGH CONTRAST");
      running = false;
      return;
    }
    if (prefersStatic()) {
      // One deterministic still frame per field state; re-armed by a mode
      // change, a resize, a new ?t value, or scrolling into a section
      // with a different data-wave preset.
      running = false;
      state.mode = "static";
      eased.yaw = 0;
      eased.pitch = 0;
      eased.strength = 0;
      // Pulses age by waveTime, which stands still here — a pulse fired
      // before the toggle would otherwise bake a frozen streak into every
      // later still frame. The still frame is the pure field state.
      pulses.length = 0;
      directorTick(true);
      staticRenderedFor = dominantName;
      drawScene(frozenTime !== null ? frozenTime : CONFIG.staticTime);
      state.frame++;
      refreshStats();
      return;
    }

    state.mode = "animated";
    var rawMs = running ? now - lastNow : 0;
    if (!running) {
      running = true;
      refreshStats();
    }
    lastNow = now;
    waveTime += Math.min(rawMs / 1000, 0.05);

    easePointer();
    directorTick(false);
    drawScene(waveTime);
    if (dead) return;
    state.lastFrameMs = rawMs;
    state.frame++;

    // Frame-time governor: sustained sub-42fps pacing steps the lattice
    // down one density level. rAF deltas include GPU backpressure, which
    // draw-call submit time does not.
    if (adaptEnabled && levelIndex >= 0 && levelIndex < CONFIG.levels.length - 1) {
      if (rawMs > 0) {
        sampleFrames++;
        if (rawMs > 24) slowFrames++;
      }
      if (sampleFrames >= 48) {
        if (slowFrames > 30) {
          levelIndex++;
          buildGrid();
        }
        sampleFrames = 0;
        slowFrames = 0;
      }
    }

    rafId = window.requestAnimationFrame(frame);
  }

  function scheduleFrame() {
    if (!rafId && !dead && inView && !document.hidden) {
      rafId = window.requestAnimationFrame(frame);
    }
  }

  // Both pages now pin the canvas to the viewport, so this almost never
  // unschedules anything — it remains as a guard for any future page that
  // embeds the canvas in a scrollable region.
  var inView = true;
  if (typeof IntersectionObserver === "function") {
    new IntersectionObserver(function (entries) {
      inView = entries[entries.length - 1].isIntersecting;
      if (!inView) {
        if (rafId) {
          window.cancelAnimationFrame(rafId);
          rafId = 0;
        }
        running = false;
      } else {
        scheduleFrame();
      }
    }).observe(canvas);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (rafId) {
        window.cancelAnimationFrame(rafId);
        rafId = 0;
      }
      running = false;
    } else {
      scheduleFrame();
    }
  });

  /* ---- context loss ------------------------------------------------------ */

  canvas.addEventListener("webglcontextlost", function (event) {
    event.preventDefault();
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
    running = false;
    // Show the static CSS backdrop while the context is gone; restoration
    // takes the class back off. Without this the dead canvas keeps covering
    // the fallback with its last (or a blank) frame.
    document.documentElement.classList.add("wave-dead");
    setStats("STATIC BACKDROP · CONTEXT LOST");
  });

  canvas.addEventListener("webglcontextrestored", function () {
    try {
      dead = false;
      document.documentElement.classList.remove("wave-dead");
      buildPrograms();
      quadBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 3, -1, -1, 3]),
        gl.STATIC_DRAW
      );
      gridBuffer = gl.createBuffer();
      buildGrid();
      needResize = true;
      scheduleFrame();
    } catch (error) {
      // Stay dead: without this flag a later scheduled frame would issue
      // GL calls against a context that never came back.
      dead = true;
      document.documentElement.classList.add("wave-dead");
      setStats("STATIC BACKDROP · CONTEXT LOST");
    }
  });

  /* ---- stats readout ----------------------------------------------------- */

  function refreshStats() {
    var label =
      state.count.toLocaleString("en-US") +
      " PARTICLES · " +
      (isGL2 ? "WEBGL2" : "WEBGL") +
      (state.hdr ? " · HDR" : "");
    if (state.mode === "static") label += " · STATIC";
    setStats(label);
  }

  function setStats(text) {
    if (statsEl) statsEl.textContent = text;
  }

  /* ---- go ---------------------------------------------------------------- */

  buildGrid();
  measureSections();
  wireInteractions();
  scheduleFrame();
})();
