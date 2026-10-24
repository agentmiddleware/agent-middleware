# Agent Middleware API design-partner site

Static, human-buyer-first marketing and proof surface for the governed MCP
trust-plane wedge. The public site is `https://www.agentmiddleware.dev/`; the
canonical API is `https://api.thisisatest.tech/`.

The intended buyer is a platform engineering, AI infrastructure, or security
team operating internal MCP tools. Machine discovery remains available below
the one-tool pilot funnel and through the static pointer files.

## Shared page design

All six HTML pages use `styles.css` for colors, typography, spacing, buttons,
focus states, and accessibility preferences. `partials/nav.html` and
`partials/footer.html` render into every page during the build, so navigation
and contact links also work without JavaScript. Edit those shared files once;
do not duplicate their markup in a page. Proof always opens `/proof/`, and the
Proof, Compare, and Demo key pages mark their current navigation link.

The stylesheet URL includes a content hash generated at build time. A style
change therefore refreshes consistently across cached pages. The archived
`/concept/` study stays unlisted but uses the same brand and controls, with
only its content width in `concept/concept.css`.

## Launch gate

Pilot intake is email-first: a prospect emails the tool scenario (the tool or
action, what goes wrong on retry, how they currently check whether the
action happened, the cost of one duplicate or unproven call, and the budget
owner and decision date, with synthetic or redacted examples only) and gets a written
reply. A call happens only when a scenario needs one. The deploy build
therefore refuses to emit `dist/` until both required values are provided:

- `PUBLIC_DISPLAY_NAME`: accountable public person or entity
- `PUBLIC_CONTACT_EMAIL`: monitored email address, the primary CTA on every
  page

One value is optional:

- `PUBLIC_BOOKING_URL`: absolute HTTPS booking URL. When set, a secondary
  "Book a call if your scenario needs one" link is rendered inside the
  `<!-- booking:start -->` … `<!-- booking:end -->` blocks in the page
  sources. When unset, those blocks are removed whole, so the emitted pages
  carry no empty link and no unresolved token.

The builder rejects missing, unresolved, and obviously provisional values,
and validates a booking URL as strictly as before whenever one is supplied.
These inputs must still be exercised manually before production because
syntax validation cannot prove that a mailbox is monitored or a booking
calendar works.

Primary pilot links open a draft with the five intake questions; the builder
URL-encodes the subject, body, and address. No email is sent by the site.
Optional booking links identify the accountable contact and product, and
nothing else: whatever calendar the operator runs, the note names the product
and the person, so a buyer never has to hold a second or third brand in mind
mid-pitch. That note disappears with the booking block in email-only builds.

The offer is a paid, bounded one-tool staging pilot with setup and recurring
service quoted separately after qualification. No fixed price is published.
The scope includes a partner-owned acceptance flow, onboarding and operating
measurements, and an end date with a named renewal decision owner.

## Economic fit

`/pilot/#economic-fit` uses `/pilot-fit.js` to estimate monthly avoided duplicate loss
from actions, baseline duplicate percentage, actual loss per duplicate, and
expected reduction. Inputs start blank; there is no assumed performance or
price. An optional total monthly cost enables a break-even comparison and
loss-per-duplicate threshold. Include the service quote, buyer operations, and
amortized setup/integration costs. Unknown cost stays unqualified; zero avoided
duplicates never produces an infinite threshold or a positive fit recommendation.
Values stay in the page: no storage, network request, analytics payload, or
automatic inclusion in an email. The formula and an explicitly illustrative
example remain readable without JavaScript. The $35 example is conditional on
100,000 actions, 0.1% duplicates, 80% reduction, and $2,800 total monthly cost;
it is neither a universal threshold nor a service quote.

Browser regression checks use the existing optional Playwright tooling after
building and serving `dist` locally:

```bash
node tests/test_site_pilot_fit.mjs http://127.0.0.1:8765 /absolute/path/to/playwright/index.mjs
node tests/test_site_design.mjs http://127.0.0.1:8765 /absolute/path/to/playwright/index.mjs
```

Run this command from the repository root. The optional second argument locates
an already installed Playwright module outside normal Node resolution. No
additional runtime dependency is required by the website.

```bash
cd site
PUBLIC_DISPLAY_NAME="..." \
PUBLIC_CONTACT_EMAIL="..." \
python3 build_site.py            # add PUBLIC_BOOKING_URL="https://..." to render the optional link
python3 -m http.server 8765 --directory dist
```

Open `http://127.0.0.1:8765/`.

## Public surfaces

- `/` — a free demo key button and one Python quickstart for `partner.echo`.
  The key panel opens on the page, keeps the key and wallet masked until
  revealed, and fills both export lines in the copyable setup block. The
  governed loop recording appears once in collapsed details. The persistent
  particle field (`/wave.js`) remains decorative and respects reduced motion
  and high contrast preferences. The waiting room arcade is still hidden
  until it can run.
- `/proof/` — portable receipt, matching key snapshot, and offline command
- `/pilot/` — paid pilot scope, intake questions, and economic fit calculator
- `/proof/transcript.json` — the recorded governed-loop transcript the
  homepage renders (see "The governed loop as evidence" below)
- `/compare/` — named competitor comparison, build-vs-buy, and fit/compliance FAQ
- `/demo-key/` — one-time demo credential display and terminal steps for the
  permit → `partner.echo` → portable receipt loop with synthetic credits;
  this is the fallback when homepage JavaScript is unavailable
- `/concept/` — unlisted landing-page design study (noindex, absent from the
  sitemap, never linked from the funnel): the single-fold treatment the
  homepage hero was promoted from, kept as the archived study. It shares
  `/wave.js` with the homepage; the renderer honors the a11y widget's
  reduced-motion and high-contrast preferences and degrades to a static CSS
  backdrop without JavaScript or WebGL
- `/404.html` — branded not-found page with links back into the site
- `/.well-known/agent.json` — marketing-origin pointer to API discovery
- `/.well-known/security.txt` — vulnerability-report contact (RFC 9116)
- `/llm.txt` and `/llms.txt` — machine bootstrap prose
- `/llms-full.txt` — long-form machine brief: boundary, vocabulary, non-claims
- `/fonts.css` and `/fonts/*.woff2` — self-hosted typography
- `/robots.txt` and `/sitemap.xml` — search discovery

`sitemap.xml` and `.well-known/security.txt` are rendered, not copied:
`@@BUILD_DATE@@` becomes the build date's `<lastmod>`, and
`@@SECURITY_TXT_EXPIRES@@` becomes one year past the build, so a deployed
`security.txt` never serves a lapsed `Expires`. Plain-text targets take the raw
contact value; HTML and XML targets take the entity-escaped one.

## The governed loop as evidence

The homepage shows the governed loop as a terminal: real requests, real
responses, and the offline verifier's real stdout. Nothing in those panels is
typed into the HTML. `build_site.py` expands three tokens from
`proof/transcript.json`:

- `@@HERO_CONSOLE@@` — the loop's spine (permit → invoke → replay → verify)
  beside the headline
- `@@LOOP_TRANSCRIPT@@` — every recorded step with its loop label, title and
  annotation, in the "Governed path" section
- `@@LIVE_VERIFICATION@@` — `b2a-verify-receipt` run against the published
  `receipt.json` and `trust-keys.json`, in the proof section

`transcript.json` is **generated**. `scripts/record_site_transcript.py` runs
the same proof as `make prove-trust-plane` against a throwaway local SQLite
gateway, records every HTTP exchange the demo makes through its helpers, keeps
the ones the page shows, and runs the SDK verifier twice — on the demo's
portable receipt and on the live one. The operator key and the minted agent
key are replaced with `$OPERATOR_API_KEY` and `$AGENT_API_KEY` before anything
is written.

The steps are kept in **loop order, not run order**. The demo denies the
out-of-scope call before it verifies the audit chain; the page walks the
eight-stage loop, so it shows the offline receipt check (06), then the audit
chain (07), then the denial (08). Every line is a real exchange either way,
and a test pins the rendered numbering so it cannot drift back into counting
backwards.

The transcript also carries a `latency` block — `samples`, `p50_ms`, `p95_ms`,
`min_ms`, `max_ms`, `mean_ms`, plus the `path`, `tool` and `transport` they
describe. The demo times a hundred fresh governed calls (each a real
reservation, dispatch, debit and receipt, under their own key and permit so
they neither share a rate-limit bucket nor touch the budget the transcript
narrates) and the build renders it under the loop as `.console-latency`. That
run is deliberately *not* recorded exchange by exchange: the demo times raw
requests with no helper in between, so only the summary is published. The
figure is measured in-process against local SQLite with a stand-in echo tool,
so the copy calls it a reference point rather than a production number — a
gateway that sits in the execution path should say what it costs in time, and
should say just as plainly what it has not measured. The build refuses a
transcript without the block rather than rendering a blank panel, and the
timing values are exempt from the staleness comparison because they
legitimately differ on every run.

```bash
make site-transcript          # re-record (≈30s; needs the app's requirements)
make site-transcript-check    # fail if the committed file is stale or hand-edited
```

Re-record whenever the demo, a router's response shape, or `receipt.json`
changes. The build refuses a transcript whose live verification names a
different receipt than the one published, so republishing the receipt without
re-recording cannot ship a verdict for the wrong artifact. The panels say in
their own footer that the loop was recorded from a local gateway run, not the
live API; the published sample receipt's verdict is the only line on the page
that comes from production, and the page dates that receipt as the historical
sample it is.

## Structured data

Each indexable page publishes one `application/ld+json` block holding a
`@graph`. One block per page is the point: separate blocks cannot reference one
another's nodes, and the subpages resolve the shared `#organization`,
`#website`, `#software`, and `#logo` definitions the homepage declares rather
than restating them and drifting. Nothing declares an offer, price, or rating,
because this stage of the product has none.

`/compare/` carries `@@FAQ_JSONLD@@` instead of a hand-written `FAQPage` node.
`build_site.py` generates that node from the page's own
`<dl class="faq-list">`, so the marked-up answers are by construction the
answers a reader sees — the mismatch Google's FAQ guidance forbids cannot be
introduced by editing one and forgetting the other. A page that carries the
token without a populated, balanced FAQ list fails the build.

Contact tokens are deliberately absent from every JSON-LD block: they are
entity-escaped for HTML targets, and entity escapes are not decoded inside a
`<script>`, so a contact value containing `&` or `"` would publish corrupted.

## Response headers and caching

`vercel.json` sends a `Content-Security-Policy` with `script-src 'self'` and no
`'unsafe-inline'`. Two files exist only to make that possible:

- `a11y-preload.js` — applies saved accessibility preferences before first
  paint. It is loaded **synchronously** in `<head>`; do not add `defer`.
- `va-init.js` — the Vercel Web Analytics queue shim, emitted only when
  `PUBLIC_ENABLE_VERCEL_ANALYTICS=true`.

If you ever add an executable inline `<script>` to a page, the CSP will block
it and `test_pages_carry_no_inline_scripts` will fail. Put the code in a
same-origin file instead.

The shared CSS and JS assets are served with `max-age=604800`, so cache busting is a **manual
query token**: every reference looks like `/styles.css?v=gateway-19`. When you
change any of those files (including `/wave.js`, `/arcade-boot.js`,
`/arcade.js`, and `/arcade.css`), bump the token in
`index.html`, `proof/index.html`, `compare/index.html`, `concept/index.html`,
`demo-key/index.html`,
`404.html`, and `build_site.py`'s `ANALYTICS_SCRIPTS`, or
returning visitors
keep the old bytes for up to a week. HTML itself carries no long-lived
`Cache-Control` rule and revalidates on every request. The concept page's own
stylesheet uses an independent `?v=concept-N` token: bump it when
`concept/concept.css` changes.

The pilot calculator uses its own `?v=pilot-2` token and the default revalidating
cache policy; bump its token when `pilot-fit.js` changes.

The demo key page's script uses its own `?v=demo-key-4` token; bump it when
`demo-key/demo-key.js` changes. `utm-capture.js` uses `?v=utm-1`; bump it when
that file changes. It is loaded on every page, including 404, so a landing URL's
`utm_*` parameters survive a later click to `/demo-key/`.

Explicit `/proof` → `/proof/`, `/compare` → `/compare/`, and
`/demo-key` → `/demo-key/` redirects match
those pages' `rel="canonical"`. Every directory page needs its own entry. Do **not** replace it with the global `trailingSlash: true`
setting: on Vercel that makes every `/.well-known/*` entry in `headers` stop
matching, so `agent.json` and `security.txt` silently fall back to
`max-age=0, must-revalidate` while every other configured path keeps its
headers. Confirmed on the deployed site.

`/pilot` redirects to `/pilot/`. The configuration is checked by the
site contract tests; verify the HTTP redirect after deployment because the
plain Python preview server does not implement Vercel routing rules.

The proof page reads fields from `/proof/receipt.json`; it does not hard-code a
receipt ID, amount, or verification verdict. If either proof file is absent or
does not contain the receipt's matching key, the page says the artifact is not
published and makes no validity claim. Cryptographic validity comes only from
the offline verifier.

## The waiting room

This product is built for agents. During the governed loop the human has
nothing to do, so the landing page offers a way to spend that time:
`HUMANS: PRESS START` fades the page and opens a full-screen arcade with one
hundred cabinets. The launcher closes the page's own content rather than
sitting in the footer, so the joke never lands beside `Report a vulnerability`
— a security reader meeting both at once is being told two contradictory
things about how seriously this page takes itself. The whole feature lives in `/arcade.js` and `/arcade.css`,
and neither loads with the page: `/arcade-boot.js` (a few hundred bytes,
landing page only) reveals the launcher and fetches both the first time
someone presses START, hovers or focuses the control, or arrives on a
`?arcade=` deep link. The arcade is by far the heaviest asset on the site
(~145 KB gzipped), and nobody needs it until they ask for it. Once loaded,
`arcade.js` owns the launcher as it did when it loaded eagerly; the
bootstrap steps aside. The asset URLs live on the launcher's `data-arcade-script`
and `data-arcade-style` attributes so they carry the page's cache token like
every other reference.

The cabinets are declared in one `CABINETS` table, each with an `id`, a `name`,
a `genre`, a `family`, a tagline, a controls line and a `pad` layout. Every one
is a failure mode of this product's own domain played straight as a game, and
none of them names anybody's franchise — `test_arcade_cabinets_are_generic_and_unbranded`
fails the build if one ever does.

**Families, not genres, drive the filter row.** A hundred cabinets carry about
ninety distinct genres between them; ninety chips would be a worse maze than
the grid. Nine families shelve them, the stylesheet hangs each family's colour
and marquee pattern off `data-family`, and the specific genre stays on the tile
badge.

**SHOOT** (14) — `aim-drill` AIM DRILL, `artillery` ARTILLERY, `backstop` BACKSTOP, `blast-radius` BLAST RADIUS, `bullet-ledger` BULLET LEDGER, `chokepoint` CHOKEPOINT, `countersign` COUNTERSIGN, `depth-charge` DEPTH CHARGE, `hold-the-line` HOLD THE LINE, `intercept` INTERCEPT, `retry-storm` RETRY STORM, `scope-creep` SCOPE CREEP, `shard-field` SHARD FIELD, `siege-budget` SIEGE BUDGET

**ACTION** (11) — `absorb` ABSORB, `arbitration` ARBITRATION, `brute-force` BRUTE FORCE, `cold-storage` COLD STORAGE, `growth` GROWTH, `key-rotation` KEY ROTATION, `last-quorum` LAST QUORUM, `pop-the-queue` POP THE QUEUE, `race-condition` RACE CONDITION, `side-channel` SIDE CHANNEL, `swarm` SWARM

**RUN** (15) — `cavern` CAVERN, `cold-slope` COLD SLOPE, `cold-start` COLD START, `handoff` HANDOFF, `happy-path` HAPPY PATH, `invert` INVERT, `lane-hop` LANE HOP, `mine-cart` MINE CART, `rate-gate` RATE GATE, `scope-match` SCOPE MATCH, `tail-latency` TAIL LATENCY, `thrust-budget` THRUST BUDGET, `tunnel` TUNNEL, `uptime` UPTIME, `wall-jump` WALL JUMP

**PUZZLE** (19) — `append-only` APPEND-ONLY, `backpressure` BACKPRESSURE, `blast-map` BLAST MAP, `bubble-queue` BUBBLE QUEUE, `circuit-route` CIRCUIT ROUTE, `cold-move` COLD MOVE, `grid-proof` GRID PROOF, `idempotency` IDEMPOTENCY, `match-policy` MATCH POLICY, `mate-in-one` MATE IN ONE, `merge-ledger` MERGE LEDGER, `patience` PATIENCE, `pipe-permit` PIPE PERMIT, `quorum-flip` QUORUM FLIP, `reorder` REORDER, `sort-keys` SORT KEYS, `tile-audit` TILE AUDIT, `untangle` UNTANGLE, `word-lock` WORD LOCK

**TIMING** (10) — `cold-path` COLD PATH, `crossfade` CROSSFADE, `double-spend` DOUBLE SPEND, `handshake` HANDSHAKE, `heartbeat` HEARTBEAT, `nonce-burn` NONCE BURN, `replay-order` REPLAY ORDER, `slice-queue` SLICE QUEUE, `tap-order` TAP ORDER, `token-bucket` TOKEN BUCKET

**MANAGE** (15) — `block-store` BLOCK STORE, `bridge-build` BRIDGE BUILD, `catalog` CATALOG, `drop-stack` DROP STACK, `factory-line` FACTORY LINE, `harvest-window` HARVEST WINDOW, `lift-sla` LIFT SLA, `long-poll` LONG POLL, `on-call` ON CALL, `pet-agent` PET AGENT, `route-table` ROUTE TABLE, `service-menu` SERVICE MENU, `spin-plates` SPIN PLATES, `tap-forge` TAP FORGE, `ticket-queue` TICKET QUEUE

**SPORT** (9) — `bank-shot` BANK SHOT, `breaker` BREAKER, `checkout` CHECKOUT, `draw-weight` DRAW WEIGHT, `one-under` ONE UNDER, `spot-kick` SPOT KICK, `strike-quota` STRIKE QUOTA, `swish-rate` SWISH RATE, `tilt` TILT

**DRIVE** (4) — `drift-queue` DRIFT QUEUE, `orbital` ORBITAL, `soft-landing` SOFT LANDING, `throughput` THROUGHPUT

**QUEST** (3) — `deck-of-scopes` DECK OF SCOPES, `escalation` ESCALATION, `least-privilege` LEAST PRIVILEGE

Each cabinet also declares the touch layout it wants. The layouts are named
after the *inputs a cabinet reads* rather than its genre — `dpad+fire`,
`lr+fire`, `ud+fire`, `dpad`, `lr`, `ud`, `lanes`, `tap` — so two cabinets that
read the same keys always feel the same under a thumb.
`test_arcade_pads_are_layouts_the_shell_can_build` checks each one against the
layouts the shell can actually build, and cross-checks the pad against the
controls line in both directions: a cabinet naming SPACE must have an action
key, and one that never mentions the vertical arrows must not be given them.

## The cabinet toolkit

Twenty-five cabinets could each hand-roll their own `fillRect` calls; a hundred
cannot. Four shared pieces carry the visuals, and every cabinet after the first
twenty-five is built out of them:

- **Sprites** are authored as rows of characters (`.` transparent, everything
  else a palette index) and compiled once into horizontal runs. A 16x16 sprite
  is about 40 fill calls instead of 256, which at twenty sprites a frame is the
  difference between a cabinet and a slideshow. Palette entries name ink keys
  rather than hex, so everything drawn through them follows the high-contrast
  switch for free.
- **Particles** come from one fixed pool per cabinet, reused round-robin. A
  system that allocates per emit is the easiest way to make a fixed-timestep
  canvas stutter every few seconds when the collector runs.
- **`fx`** carries shake, colour flash, hitstop and floating numbers. All four
  are suppressed under reduced motion; shake in particular is the kind of
  involuntary movement that preference exists to stop.
- **Backdrops**: starfield, parallax bands, horizon grid and a chunky vignette.
  A cabinet drawn on flat `bg` reads as a prototype no matter how good its
  sprites are.

## Touch controls

The arcade shipped keyboard-first with a pointer bolted on. On a phone the
finger covers the thing it is steering, and a swipe latch cannot express "hold
left while firing" — which most of this roster needs. There is now a real
button pad under the screen:

- Buttons are **held, not tapped**, with the pointer captured so a finger that
  slides off a key still releases it. A stuck direction is the worst bug this
  layer can have.
- They are real `<button>`s, so a keyboard or switch user can reach them; a
  click (no pointer sequence) pulses the slot for a few simulation steps the
  way a canvas tap already does.
- The pad defaults **on** for coarse pointers, remembers the choice in
  `localStorage`, and can be toggled by anyone. Every cabinet stays fully
  playable on the arrows and space.
- Targets are 3.5rem (56px), well over the 44px floor, and the pad sits under
  the screen rather than over it. Portrait phones cap the canvas at `46dvh` so
  the whole stage fits without the mobile browser chrome cropping the pad.

The three first-person cabinets share one raycast engine — a grid map, a DDA per
column, and billboarded sprites resolved against a per-column depth buffer, so
an enemy behind a wall corner is clipped column by column rather than
all-or-nothing. Firing is **hitscan**: at this resolution a travelling bullet
is a single pixel nobody can see. Columns are drawn `COLUMN_W` (4px) wide
rather than one pixel each, which is two things at once — a quarter of the fill
calls, and the chunky vertical banding the rest of the design system is built
out of. A one-pixel column on a 320-wide canvas renders a smooth wall, which
would look wrong next to everything else on the page.

Controls in both are **turn-and-walk, not mouselook**. The arcade binds arrows,
WASD and space plus a single pointer; there is no strafe key and no pointer
lock to take, so a mouselook camera would have nothing to read. A pointer drag
steers instead, because the overlay is reachable on a phone.

`makeCaster` names solid tiles explicitly — `#` wall, `=` boundary marker, `!`
hot wall — and treats **every other character as open floor**. Do not invert
that default. Defaulting to solid silently turns a level that uses any other
character for floor into a block of stone, and the cabinet then reports the
floor "cleared" on its first frame, because nothing can spawn in a map with no
open cells. That is a real bug this engine already shipped once.

The cabinet-select screen is an **attract screen**: a head (`.arcade-select-head`,
carrying the title and a `CREDITS ∞ · FREE PLAY · ALSO METERED` line), a genre
filter row, the tile grid, and an attract-mode strip beneath it. The filter
chips are derived from `CABINETS` itself — `ALL` plus each distinct `genre` in
roster order — so a cabinet introducing a new genre grows the row on its own
rather than needing a second list kept in step by hand. Filtering sets `hidden`
on a tile rather than toggling a CSS class, and that choice is load-bearing
twice over: `hidden` removes the tile from the tab order as well as the
layout, so a filtered-out cabinet cannot be reached by keyboard, and the
stylesheet never has to know that filtering exists.

Arrow keys walk the tile grid while the select screen is up; Space is
deliberately left alone so it still activates the focused tile the way a button
should. The vertical step is measured at runtime by counting the tiles sharing
the first tile's `offsetTop`, not assumed from the desktop layout, because the
grid reflows to a single column on a phone — a hard-coded row width would jump
the focus ring three tiles at a time there.

Per-cabinet bests live in `localStorage` under `amw-arcade-best`, next to the
accessibility panel's own key, and are shown on each tile (`BEST n`, or
`UNPLAYED`). Every read and write is wrapped in `try`/`catch`: a private
window, a browser set to block site data, a full quota, and a corrupted value
each throw or return junk here, and **none of those is a reason for the waiting
room to fail to open** — a lost joke score costs nothing, a launcher that
throws costs the whole feature. A run that beats the stored value says so on
the run-complete screen, and says explicitly that it is stored in this browser
only, because nothing about this is a leaderboard.

**Attract mode** runs a real cabinet instance into a second canvas under the
grid, driven by a demo hand whose inputs are re-rolled a few times a second, so
it reads as somebody playing rather than as a scripted replay. It is a real
cabinet rather than a recording because that is the one piece of arcade
furniture CSS cannot fake. The demo cycles on death or after about fourteen
seconds — some cabinets are survivable enough that a mediocre hand would sit on
one board forever — and it draws only from the cabinets the active genre filter
leaves visible. Under reduced motion it draws one still frame and the
loop never starts: an attract loop is precisely the unrequested motion that
preference exists to suppress.

Rules it is built to:

- **Generic by construction.** No arcade trademark, character, or company name
  appears in either file. Every cabinet is named after a failure mode of this
  product's own domain instead, and
  `test_arcade_cabinets_are_generic_and_unbranded` fails the build if a brand
  name is ever added.
- **Fake receipts must look fake.** A run ends by issuing a prop receipt that
  says `SIMULATED · NOT A REAL RECEIPT` in its own body and links to `/proof/`
  for a real one. This site's only real claim is that a receipt is verifiable;
  a convincing fake would undercut it, so
  `test_arcade_receipts_are_marked_simulated` guards the disclaimer.
- **Progressive enhancement.** The launcher ships with `hidden` and is revealed
  only once `arcade.js` runs, so a visitor without JavaScript never sees a
  control that does nothing.
- **The particle field pauses.** Opening the arcade sets `display: none` on
  `.wave-canvas`. `/wave.js` already unschedules its frame loop when an
  `IntersectionObserver` reports that canvas out of view, so the renderer stops
  on open and resumes on close without either file importing the other.
- **Accessibility.** The overlay is a focus-trapped `role="dialog"` with
  `aria-modal`, Escape closes it, focus returns to the launcher, and the page
  behind it goes `inert`. Reduced motion drops the decorative parts — the boot
  sequence prints at once and the scanlines and hover lifts are removed —
  while gameplay motion stays, since entering is an explicit opt-in. High
  contrast switches the cabinet palette rather than hiding the feature.

`?arcade=<cabinet-id>` opens straight into that cabinet — `?arcade=1` opens the
select screen — and `window.__amwArcade` exposes
`open`/`close`/`start`/`select`/`press`/`aim`/`step`/`state` so headless checks
can advance the simulation deterministically instead of racing a frame budget.
Use `aim()` rather than `press()` for the pointer: `pointerX`/`pointerY` hold a
number or `null`, and coercing them to booleans pins a cabinet's player at the
clamp floor. Cabinet ids are the hundred listed above.

## Typography

Fonts are **self-hosted**, not loaded from Google. A third-party font CDN costs
two cross-origin handshakes on the critical path — `fonts.googleapis.com` for
the CSS, then `fonts.gstatic.com` for the files — and puts an outside party
between a visitor and a page whose whole pitch is that you can verify things
yourself. Serving them here also lets the CSP stay `style-src 'self';
font-src 'self'` with no external host.

`fonts/` and `fonts.css` are **generated**. To add a family or weight, edit
`FAMILIES` in `vendor_fonts.py`, then:

```bash
cd site
python3 vendor_fonts.py          # refetch and rewrite fonts/ + fonts.css
python3 vendor_fonts.py --check  # verify the committed output matches upstream
```

Only `latin` and `latin-ext` are vendored, and each face keeps upstream's
`unicode-range`, so a browser downloads only the subsets a page actually uses.
Libre Franklin and Public Sans are variable fonts: one file per subset serves
every weight, which is why their filenames carry no weight.

All five families are OFL 1.1, and `fonts/OFL.txt` carries the **full license
text**, not a link to it — plain text so the notice deploys alongside the fonts
it covers. The bare-link shortcut the OFL FAQ tolerates applies to fonts
embedded in a document or bundled inside a program; serving `/fonts/*.woff2` as
standalone files is plain redistribution, so condition 2 applies in full. The
copyright lines are verbatim from each family's upstream `OFL.txt` — including
the Reserved Font Names on IBM Plex and Press Start 2P, which the other three
do not reserve. Adding a family to `FAMILIES` ships its woff2 files, so its
notice has to land in `OFL.txt` in the same change: condition 2 is about the
files actually served, not about the stylesheet that references them.

Filenames carry a content hash, so `vercel.json` serves `/fonts/*.woff2`
`immutable` for a year and a re-vendored font can never be served stale. The
stylesheet's own cache key is generated the same way — `@@FONTS_CSS_VERSION@@`
becomes a digest of `fonts.css` at build time. It must not become a manual
token: a visitor holding a week-old `fonts.css` would request the hashed woff2
files that re-vendoring has already deleted, and get 404s until it expired. That
also means preloads cannot be hand-written: `vendor_fonts.py` emits
`fonts.manifest.json`, and `build_site.py` expands `@@FONT_PRELOADS@@` in each
page from it. Edit `PRELOAD` in `vendor_fonts.py` to change which faces are
preloaded.

Preloads cover every face in the first viewport. Instrument Serif is the
display face (the `h1` and every `h2`), so its single 400 face is preloaded
first: a fallback flash there is a layout change, not a font swap, since no
system serif shares its metrics. Libre Franklin carries card and tile
titles and Public Sans the body; both are variable, so one file each is
enough. IBM Plex Mono carries every label, kicker, badge, code span and
receipt row; it is static, so weights 400, 500 and 600 are three separate
files and all three are preloaded. Press Start 2P is deliberately not
preloaded: the page wears it only on the `HUMANS: PRESS START` control that
closes the page and inside the arcade overlay, both far below the fold. A preload must carry `crossorigin` even
same-origin, or the browser discards it and fetches the file twice. The `404`
page preloads nothing on purpose — it is `noindex` and mostly serves scanners.

Adding a family or weight to `styles.css` without adding it to `FAMILIES` fails
`test_font_stylesheet_and_files_agree` rather than letting the browser
synthesise the weight. An interrupted vendoring run that empties `fonts/` fails
the launch gate rather than deploying a stylesheet whose every `src` 404s.

## Brand graphics

`styles.css` is the palette's source of truth. `favicon.svg` and
`social-card.svg` draw exclusively from its `:root` tokens — ink ground,
text-light letterform and headline, gold seal marks (the same mark the nav
brand carries). The favicon is a 16×16 pixel letterform, because a favicon
is a pixel grid whatever the page looks like; the social card is set in the
page's own Instrument Serif and IBM Plex Mono on soft corners, matching the
pages it previews.
`test_brand_graphics_use_the_design_system_palette` fails if either file
reintroduces an off-system color; that is how the original graphics
drifted, keeping a retired charcoal-and-ember palette long after the pages
moved on, so tab icon and link preview advertised a different product than the
page that loaded.

`social-card.png` — the 1200×630 raster the `og:image`/`twitter:image` tags
serve, because link crawlers do not rasterize SVG — is **generated**:

```bash
cd site
python3 render_social_card.py    # rasterize social-card.svg → social-card.png
```

The script (stdlib-only, like `vendor_fonts.py`) inlines the SVG into a shim
page, loads the vendored woff2 faces from `fonts/`, and screenshots it with
headless Chromium, so the card's Instrument Serif headline and IBM Plex Mono
labels are the site's own typography rather than an exporting machine's
substitutes. It prefers a Playwright `headless_shell` build (found under
`$PLAYWRIGHT_BROWSERS_PATH`; or point `$CHROMIUM` at any binary), whose
viewport matches `--window-size`; a full-UI Chromium reserves toolbar
height inside that size, so the script probes the render's bottom row for the
ink ground and refuses a short viewport instead of committing a card with a
blank band. Re-run it whenever `social-card.svg` or the vendored fonts
change, and commit the SVG and PNG together. Neither the SVG nor the script
deploys; `dist/` gets only the PNG.

The same tokens are resolved into the two surfaces that cannot import
`styles.css`: `static/dashboard.html` (the API origin's self-contained
operator index) and `app/services/approval_card.py` (permit-approval email
and hosted card, where mail clients drop `:root` and custom properties).
When the palette moves, re-resolve both —
`test_resolved_palette_surfaces_stay_within_the_stylesheet` fails on any
literal hex or rgba hue the stylesheet does not itself use. `/concept/` imports
the shared design system directly, including the accessibility controls.

## Analytics

Vercel Web Analytics records page views and three non-PII event names:
`email_click` (the primary intake CTA and the address links), `proof_click`,
and `booking_click`, which can only fire when the optional
`PUBLIC_BOOKING_URL` was configured at build time. Event payloads never
include the email address, booking URL, receipt fields, or link destination.

The `/_vercel/insights/script.js` loader is emitted only when
`PUBLIC_ENABLE_VERCEL_ANALYTICS=true` is set at build time; the default build
omits it. The flag accepts `true`, `false` (case-insensitive), or
being unset — any other value fails the build. Vercel serves that script only for projects whose Web Analytics is
enabled in the dashboard — deploying the tag without that produces a 404 plus
a MIME-type refusal in the browser console on every page load. Enable Web
Analytics for the Vercel project first, then set the environment variable.

## Deployment

Keep `vercel.json`; this change intentionally does not migrate configuration
formats.

If a merge to `main` ever fails to produce a production deployment (observed
2026-08-19: the push built and passed CI on GitHub while Vercel never started
a production build — a missed webhook), deploy the missing commit rather than
reaching for **Redeploy**: Redeploy rebuilds the selected deployment's own
commit, and with the webhook missed the latest `main` deployment is still the
previous commit, so redeploying it ships the stale tree again. Push a fresh
commit to `main` (a docs-only change is enough) to re-fire the webhook, or
create a deployment for the current `main` head explicitly (`vercel deploy
--prod`, or the dashboard's **Create Deployment** flow). HTML carries no
long-lived cache rule, so the fix is visible as soon as the new deployment
goes live. The linked Vercel project uses `site/` as its root directory and runs
the stdlib-only `python3 build_site.py` command before serving `dist/`. The
dependency-free `package.json` gives Vercel's static builder an explicit build
entrypoint; without it, a standalone JavaScript asset can be mistaken for the
entrypoint and the contact gate can be skipped.

The apex domains, `www.thisisatest.tech`, and known Vercel aliases redirect to
`https://www.agentmiddleware.dev/`. `www.agentmiddleware.dev` is not a redirect
source. Marketing discovery redirects target the
custom API origin. The infrastructure provider hostname remains a compatibility
origin only and must not appear on customer-facing pages.
