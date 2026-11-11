# Dashboard + setup wizard (web app)

The pages the agent serves at `http://127.0.0.1:8721/setup`, `/dash` and `/v2`.
React + TypeScript + Tailwind, built with Vite.

**Users never need Node.** The build output in `../webui_dist/` is committed,
and `webui.py` serves it. If that folder is missing, or you open a page with
`?ui=classic`, the agent falls back to the built-in pages in `webui.py`.

## Change the UI

```bash
cd webui_app
npm ci
npm run build        # type-checks, then writes ../webui_dist
```

Commit `webui_app/` changes **and** the rebuilt `webui_dist/` together.
The build is reproducible: the same source gives byte-identical files, so a
reviewer can rebuild and diff.

Live-reload while editing: start the agent (it listens on 8721), open its
login link once so the browser has the session cookie, then `npm run dev`
and open `http://127.0.0.1:5173/dash` (or `/setup`). Use 127.0.0.1, not
localhost, so the cookie applies. API calls proxy to 8721.

## The typed status (`/api/v2/status`)

`src/lib/viewModel.ts` is the type of what `GET /api/v2/status` and
`GET /api/v2/activity` return. It is **generated** from the schema in
`../view_model.py` — never edit it by hand:

```bash
python view_model.py --ts     # from the repo folder; no Node needed
```

A test fails when the file and the Python schema differ.

## The redesigned dashboard (`/v2`)

`src/v2/` is a second entry in this app: the redesigned dashboard, served at
`/v2` next to the classic one (`/dash`, still the default). The two link to
each other and share no code at run time.

- It reads `GET /api/v2/status` every 5 seconds and renders only what that
  says. `null` is shown as "not known yet", never as a number. Older Activity
  rows come from `/api/v2/activity`.
- Settings are changed in /v2 itself, through the typed settings API
  (`GET /api/v2/settings`, `POST /api/v2/settings/...`, `/api/v2/broker/...`,
  `/api/v2/pass/buy`, `/api/v2/command`; client in `src/v2/lib/http.ts`,
  answer types in `src/lib/settingsApi.ts`). A money-affecting change is
  confirmed in a dialog that shows the server's own preview and is applied
  only with its token; after every save the page reads the settings back.
  Only the first-run wizard (`/setup?from=v2`, which comes back to /v2) and
  the move tool (`/dash#move`) are outside /v2.
- One fact, one place; one action, one home: `src/v2/lib/screens.ts` says
  which page owns each control, and `notes()` in `src/v2/lib/derive.ts`
  states each condition once (the agent's own message about it is folded
  into the dashboard's note by condition code). The Node tests check both
  over every fixture state and scan the sources for `data-act` / `data-go`.
- Every code the status can carry is turned into words in one file,
  `src/v2/lib/copy.ts`. What the band, the vitals and the headline say is
  worked out in `src/v2/lib/derive.ts`. Both are plain functions with tests.
- Styles are plain CSS (`src/v2/guardian.css`), not Tailwind. The colours
  have one meaning each: solid blue = real money / LIVE, dashed violet
  = practice, grey = unknown, red = a fault or a loss, green = a gain.

`npm run build` builds both: the classic app first (`vite.config.ts`), then
`/v2` on its own (`vite.v2.config.ts`, into `webui_dist/v2.html` and
`assets/v2-*`), so a change to one never rewrites the other's files.

See it without an agent or a broker (development only; the product has no
demo switch):

```bash
python tests/guardian_dev_server.py        # after `npm run build`
# http://127.0.0.1:8799/v2?state=live      (or practice, paused-new-trades,
#   state-hold, broker-disconnected, limit-hit, low-cash, not-answering,
#   stopped, empty-first-run, fetch-failed, stress … the page at / lists them)
```

Each state is a real view-model, built by `view_model.build_status` from the
inputs in `tests/guardian_states.py`.

Tests: `tests/test_webui_v2.py` (the route, the committed build, the states)
runs under pytest. It also runs `webui_app/tests/guardian.test.mjs` with
Node's own test runner when Node 22.6+ is installed (no packages needed;
skipped otherwise): every reason code and entry check has words, and every
state keeps to the rules above.

## Rules this app follows

- No network loads at runtime: fonts (IBM Plex, via @fontsource) and all
  code are bundled. The server sends a strict Content-Security-Policy.
- It only calls the agent's own `/api/*` endpoints; behaviour (what each
  button does, the go-live confirmation, the consent phrase) matches the
  built-in pages.
- `webui_app/` and `webui_dist/` are FOUNDATION folders for self-edit
  (`self_edit.FOUNDATION_DIRS`): the agent can never rewrite its own UI.

## Third-party

Charts: [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)
(Apache-2.0; the chart shows TradingView's attribution logo as its license asks).
