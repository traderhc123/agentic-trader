# Dashboard + setup wizard (web app)

The pages the agent serves at `http://127.0.0.1:8721/setup` and `/dash`.
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
