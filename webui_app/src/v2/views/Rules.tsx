// Rules & limits: the home of every limit, trade size and rule. Each row
// shows the saved value (GET /api/v2/settings) and today's use (the status).
// A limit or a trade size is changed in place, then confirmed in a dialog
// that shows the server's own preview; rules (which signals to take, the
// written rules) are not money-affecting and save straight away. After any
// save the page reads the settings again and shows those.
import { useEffect, useId, useState, type ReactNode } from "react";
import type { Preview, SettingsView, SizeMode, SizeSpec } from "../../lib/settingsApi";
import type { TradeSize } from "../../lib/viewModel";
import { ReviewDialog, RiskLine, FieldError, Switch, askPreview } from "../Forms";
import { API, send } from "../lib/http";
import {
  SIZE_MODE_WORDS,
  SIZE_RANGE,
  limitMeans,
  limitRange,
  lossWords,
  money,
  readNumber,
  readTickers,
  riskOf,
  savedTickers,
  sizeMeans,
  sizeWords,
  valueWords,
  type Book,
  type LimitKey,
} from "../lib/settings";
import { UNKNOWN, count, etDayKey, etWhen, plural, usd } from "../lib/format";
import { Icon, Stamp } from "../ui";
import type { Page } from "../App";
import { LimitsPanel } from "./Today";
import { Row } from "./Pages";

/** The size the agent runs on (status), when the saved one is not read yet. */
function statusSize(t: TradeSize): string {
  if (t.mode === "budget") return t.usd != null ? `${usd(t.usd, 0)} a trade` : UNKNOWN;
  if (t.mode === "percent") return t.percent != null ? `${t.percent}% of the account` : UNKNOWN;
  return t.contracts != null ? `${plural(t.contracts, "contract")} a trade` : UNKNOWN;
}

/** Whether this page can change anything right now, and why not. */
function canEdit(page: Page): string {
  if (page.link === "lost") return "This page can't reach the agent, so nothing can be changed right now.";
  if (!page.settings.view) return page.settings.error || "Reading your settings…";
  return "";
}

type Draft = { payload: Record<string, unknown>; preview: Preview };

/** The inline editor's frame: the field(s), the risk line, Review / Cancel. */
function Editor({
  children,
  risk,
  error,
  errId,
  busy,
  primary,
  onPrimary,
  onCancel,
}: {
  children: ReactNode;
  risk?: ReactNode;
  error: string;
  errId: string;
  busy: boolean;
  primary: string;
  onPrimary: () => void;
  onCancel: () => void;
}) {
  return (
    <form
      className="editor"
      onSubmit={(e) => {
        e.preventDefault();
        onPrimary();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }}
    >
      {children}
      <FieldError id={errId} text={error} />
      {risk}
      <div className="editor-row">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Asking the agent…" : primary}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function ChangeButton({ act, label, page, onClick }: { act: string; label: string; page: Page; onClick: () => void }) {
  const why = canEdit(page);
  return (
    <button className="btn btn-sm" data-act={act} aria-label={`Change ${label}`} disabled={!!why} title={why || undefined} onClick={onClick}>
      Change
    </button>
  );
}

// ── a daily limit ───────────────────────────────────────────────────────────

const LIMIT_LABEL: Record<LimitKey, string> = {
  max_entries_per_day: "New trades per day",
  max_daily_loss_usd: "Daily loss limit",
  max_open_positions: "Open positions at once",
};

function LimitRow({ page, k, exp, now }: { page: Page; k: LimitKey; exp: string; now: ReactNode }) {
  const v = page.settings.view;
  const L = page.s.limits;
  const saved: number | null = v ? (v.limits[k] ?? (k === "max_daily_loss_usd" ? null : v.limits.defaults[k as "max_entries_per_day"])) : k === "max_entries_per_day" ? L.cap : k === "max_open_positions" ? L.max_positions : L.max_daily_loss;
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const id = useId();
  const label = LIMIT_LABEL[k];
  const range = limitRange(v, k);
  const parsed = readNumber(text, range);
  const open0 = () => {
    setText(saved != null ? String(saved) : "");
    setErr("");
    setOpen(true);
    requestAnimationFrame(() => document.getElementById(id)?.focus());
  };
  const close = () => {
    setOpen(false);
    setDraft(null);
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-row="${k}"] [data-act]`)?.focus());
  };
  const review = async () => {
    if (!parsed.ok) return setErr(parsed.error);
    setBusy(true);
    setErr("");
    const payload = { [k]: parsed.value };
    const a = await askPreview(API.limits, payload);
    setBusy(false);
    if (a.kind === "review") setDraft({ payload, preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  const ctx = { mainUsd: L.trade_size.main.usd, open: L.open_positions.real + L.open_positions.practice };
  const dir = parsed.ok ? riskOf(k, saved, parsed.value) : "neutral";
  return (
    <div className="setrow" data-row={k}>
      <span className="lbl">{label}</span>
      <span className="exp">{exp}</span>
      <span className="val">
        {k === "max_daily_loss_usd" ? lossWords(saved) : count(saved)}
        <small>{now}</small>
      </span>
      <span className="act">{!open && <ChangeButton act="limits" label={label} page={page} onClick={open0} />}</span>
      {open && (
        <Editor
          error={err}
          errId={`${id}-e`}
          busy={busy}
          primary="Review change"
          onPrimary={review}
          onCancel={close}
          risk={parsed.ok ? <RiskLine direction={dir} same={parsed.value === saved || (k === "max_daily_loss_usd" && lossWords(saved) === lossWords(parsed.value))} means={limitMeans(k, parsed.value, ctx)} /> : null}
        >
          <div className="field">
            <label htmlFor={id}>New value for “{label}”</label>
            <div className="editor-row">
              <span className={k === "max_daily_loss_usd" ? "input-money" : ""}>
                {k === "max_daily_loss_usd" && <span>$</span>}
                <input
                  id={id}
                  className="input input-num"
                  inputMode={k === "max_daily_loss_usd" ? "decimal" : "numeric"}
                  autoComplete="off"
                  value={text}
                  aria-invalid={!!err}
                  aria-describedby={`${id}-h ${err ? `${id}-e` : ""}`}
                  onChange={(e) => {
                    setText(e.target.value);
                    setErr("");
                  }}
                />
              </span>
              <span className="muted small" id={`${id}-h`}>
                Saved now: <b>{k === "max_daily_loss_usd" ? lossWords(saved) : count(saved)}</b>.{" "}
                {k === "max_daily_loss_usd" ? "0 turns the limit off." : ""}
              </span>
            </div>
          </div>
        </Editor>
      )}
      {draft && (
        <ReviewDialog
          path={API.limits}
          payload={draft.payload}
          first={draft.preview}
          title={`${draft.preview.changes[0]?.direction === "lowers_risk" ? "Lower" : draft.preview.changes[0]?.direction === "raises_risk" ? "Raise" : "Change"} “${label}”?`}
          cancel={`Keep ${valueWords(k, saved).replace(/ a day$| at once$/, "")}`}
          live={page.s.mode === "live"}
          onClose={() => setDraft(null)}
          onDone={async () => {
            setDraft(null);
            setOpen(false);
            const nv = await page.settings.reload();
            page.refresh();
            page.say(true, nv ? `Saved. ${label}: ${valueWords(k, nv.limits[k] ?? null).replace(/ a day$| at once$/, "")}.` : "Saved. This page could not read your settings back yet; it shows them once it can.");
          }}
        >
          {() => parsed.ok && <p className="small">{limitMeans(k, parsed.value, ctx)}</p>}
        </ReviewDialog>
      )}
    </div>
  );
}

// ── a trade size ────────────────────────────────────────────────────────────

const SIZE_LABEL: Record<Book, string> = { main: "Trade size, main pick", other: "Trade size, other trades" };

function SizeRow({ page, book }: { page: Page; book: Book }) {
  const v = page.settings.view;
  const st = page.s.limits.trade_size[book];
  const saved: SizeSpec | null | undefined = v ? v.limits.trade_size[book] : undefined;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<SizeMode | "same">("budget");
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const id = useId();
  const label = SIZE_LABEL[book];
  const same = mode === "same";
  const parsed = same ? ({ ok: true, value: 0 } as const) : readNumber(text, SIZE_RANGE[mode]);
  const next: SizeSpec | null = same ? null : parsed.ok ? { mode, value: parsed.value } : null;
  const shown = saved !== undefined ? sizeWords(saved, book) : book === "other" && st.same_as_main ? "Same as the main pick" : statusSize(st);
  const open0 = () => {
    setMode(saved ? saved.mode : book === "other" && v ? "same" : st.mode);
    setText(saved?.value != null ? String(saved.value) : "");
    setErr("");
    setOpen(true);
    requestAnimationFrame(() => document.getElementById(`${id}-m`)?.focus());
  };
  const review = async () => {
    if (!parsed.ok) return setErr(parsed.error);
    setBusy(true);
    setErr("");
    const payload = { trade_size: { [book]: same ? { mode: "same" } : { mode, value: parsed.value } } };
    const a = await askPreview(API.limits, payload);
    setBusy(false);
    if (a.kind === "review") setDraft({ payload, preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  const dir = riskOf(`trade_size.${book}`, saved ?? null, next);
  const unchanged = same ? saved === null : !!saved && !!next && saved.mode === next.mode && saved.value === next.value;
  return (
    <div className="setrow" data-row={`size-${book}`}>
      <span className="lbl">{label}</span>
      <span className="exp">
        {book === "main"
          ? "The most the agent spends on one main-pick trade, or a fixed number of contracts, or a share of your account."
          : "The size for the provider's wider list of ideas. With no size of its own, the main-pick size applies."}
      </span>
      <span className="val">{shown}</span>
      <span className="act">{!open && <ChangeButton act="size" label={label} page={page} onClick={open0} />}</span>
      {open && (
        <Editor
          error={err}
          errId={`${id}-e`}
          busy={busy}
          primary="Review change"
          onPrimary={review}
          onCancel={() => setOpen(false)}
          risk={
            next || same ? (
              <RiskLine
                direction={dir}
                same={unchanged}
                means={same ? "The main-pick size applies to these trades too." : next ? sizeMeans(next, book) : ""}
              />
            ) : null
          }
        >
          <div className="editor-row">
            <div className="field">
              <label htmlFor={`${id}-m`}>Size by</label>
              <select id={`${id}-m`} className="input" value={mode} onChange={(e) => setMode(e.target.value as SizeMode | "same")}>
                {(Object.keys(SIZE_MODE_WORDS) as SizeMode[]).map((m) => (
                  <option key={m} value={m}>
                    {SIZE_MODE_WORDS[m]}
                  </option>
                ))}
                {book === "other" && <option value="same">Same as the main pick</option>}
              </select>
            </div>
            {!same && (
              <div className="field">
                <label htmlFor={id}>{mode === "budget" ? "Dollars per trade" : mode === "percent" ? "Percent of the account" : "Contracts per trade"}</label>
                <span className={mode === "budget" ? "input-money" : ""}>
                  {mode === "budget" && <span>$</span>}
                  <input
                    id={id}
                    className="input input-num"
                    inputMode={mode === "contracts" ? "numeric" : "decimal"}
                    autoComplete="off"
                    value={text}
                    aria-invalid={!!err}
                    aria-describedby={err ? `${id}-e` : undefined}
                    onChange={(e) => {
                      setText(e.target.value);
                      setErr("");
                    }}
                  />
                </span>
              </div>
            )}
          </div>
          <span className="muted small">
            Saved now: <b>{shown}</b>
          </span>
        </Editor>
      )}
      {draft && (
        <ReviewDialog
          path={API.limits}
          payload={draft.payload}
          first={draft.preview}
          title={`${draft.preview.changes[0]?.direction === "lowers_risk" ? "Lower" : draft.preview.changes[0]?.direction === "raises_risk" ? "Raise" : "Change"} “${label}”?`}
          cancel={`Keep ${shown.replace(/ a trade$/, "")}`}
          live={page.s.mode === "live"}
          onClose={() => setDraft(null)}
          onDone={async () => {
            setDraft(null);
            setOpen(false);
            const nv = await page.settings.reload();
            page.refresh();
            page.say(true, nv ? `Saved. ${label}: ${sizeWords(nv.limits.trade_size[book], book)}.` : "Saved. This page could not read your settings back yet; it shows them once it can.");
          }}
        >
          {() => next && <p className="small">{sizeMeans(next, book)}</p>}
        </ReviewDialog>
      )}
    </div>
  );
}

// ── which signals to take (not money-affecting: saved straight away) ───────

type RuleKey = "rule_tickers" | "rule_skip_tickers" | "rule_sides" | "rule_min_strike" | "rule_loss_streak";

const SIDES: Record<string, string> = { both: "Calls and puts", calls: "Calls only", puts: "Puts only" };

function ruleShown(v: SettingsView, k: RuleKey): string {
  const r = v.rules;
  if (k === "rule_tickers") return savedTickers(r.rule_tickers).join(", ") || "Any stock";
  if (k === "rule_skip_tickers") return savedTickers(r.rule_skip_tickers).join(", ") || "None";
  if (k === "rule_sides") return SIDES[String(r.rule_sides || "both").toLowerCase()] ?? "Calls and puts";
  if (k === "rule_min_strike") return r.rule_min_strike ? money(Number(r.rule_min_strike)) : "Off";
  return r.rule_loss_streak ? plural(Number(r.rule_loss_streak), "loss", "losses") + " in a row" : "Off";
}

function ruleText(v: SettingsView, k: RuleKey): string {
  const r = v.rules;
  if (k === "rule_tickers" || k === "rule_skip_tickers") return savedTickers(r[k]).join(", ");
  if (k === "rule_sides") return String(r.rule_sides || "both").toLowerCase();
  if (k === "rule_min_strike") return r.rule_min_strike ? String(r.rule_min_strike) : "0";
  return r.rule_loss_streak ? String(r.rule_loss_streak) : "0";
}

function readRule(k: RuleKey, text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  if (k === "rule_tickers" || k === "rule_skip_tickers") return readTickers(text);
  if (k === "rule_sides") return { ok: true, value: text };
  if (k === "rule_min_strike") return readNumber(text, { low: 0, high: 100_000, money: true, zero: "0 turns it off" });
  return readNumber(text, { low: 0, high: 100, whole: true, zero: "0 turns it off" });
}

/** Saves a rules payload straight away, reads the settings back, and says
 *  what was saved. Resolves to an error to show inline, or "". */
async function saveRules(page: Page, payload: Record<string, unknown>, what: (v: SettingsView) => string): Promise<string> {
  const r = await send(API.rules, payload);
  if (!r.ok) return r.error;
  const nv = await page.settings.reload();
  page.refresh();
  page.say(true, nv ? `Saved. ${what(nv)}` : "Saved. This page could not read your settings back yet; it shows them once it can.");
  return "";
}

const RULES: [RuleKey, string, string][] = [
  ["rule_tickers", "Only these stocks", "When this list has stocks, signals for any other stock are skipped. Empty: any stock."],
  ["rule_skip_tickers", "Never trade these stocks", "Signals for these are always skipped. It never sells anything you hold."],
  ["rule_sides", "Calls, puts or both", "A call gains when the stock rises, a put when it falls."],
  ["rule_min_strike", "Smallest strike price", "Skips contracts with a strike below this. 0: off."],
  ["rule_loss_streak", "Stop after losses in a row", "After this many losing trades in a row, no new trades until the next day. 0: off."],
];

function RuleRow({ page, k, label, exp }: { page: Page; k: RuleKey; label: string; exp: string }) {
  const v = page.settings.view;
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const save = async () => {
    const p = readRule(k, text);
    if (!p.ok) return setErr(p.error);
    setBusy(true);
    const e = await saveRules(page, { [k]: p.value }, (nv) => `${label}: ${ruleShown(nv, k)}.`);
    setBusy(false);
    if (e) setErr(e);
    else setOpen(false);
  };
  return (
    <div className="setrow" data-row={k}>
      <span className="lbl">{label}</span>
      <span className="exp">{exp}</span>
      <span className={`val sm ${k.endsWith("tickers") ? "ticks" : ""}`}>{v ? ruleShown(v, k) : UNKNOWN}</span>
      <span className="act">
        {!open && (
          <ChangeButton
            act="filters"
            label={label}
            page={page}
            onClick={() => {
              if (!v) return;
              setText(ruleText(v, k));
              setErr("");
              setOpen(true);
              requestAnimationFrame(() => document.getElementById(id)?.focus());
            }}
          />
        )}
      </span>
      {open && v && (
        <Editor error={err} errId={`${id}-e`} busy={busy} primary="Save" onPrimary={save} onCancel={() => setOpen(false)}>
          <div className="field">
            <label htmlFor={id}>{label}</label>
            {k === "rule_sides" ? (
              <select id={id} className="input" value={text} onChange={(e) => setText(e.target.value)}>
                {Object.entries(SIDES).map(([x, w]) => (
                  <option key={x} value={x}>
                    {w}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={id}
                className="input"
                autoComplete="off"
                value={text}
                inputMode={k === "rule_min_strike" || k === "rule_loss_streak" ? "decimal" : "text"}
                aria-invalid={!!err}
                aria-describedby={`${id}-h ${err ? `${id}-e` : ""}`}
                onChange={(e) => {
                  setText(e.target.value);
                  setErr("");
                }}
              />
            )}
            <span className="help" id={`${id}-h`}>
              {k.endsWith("tickers") ? "Stock symbols separated by commas, like SPY, QQQ. Leave it empty to clear the list. " : ""}
              Saved now: <b>{ruleShown(v, k)}</b>. This does not change Practice or LIVE, and does not buy or sell anything.
            </span>
          </div>
        </Editor>
      )}
    </div>
  );
}

function SkipSameDay({ page }: { page: Page }) {
  const v = page.settings.view;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  return (
    <Row
      label="Skip same-day contracts"
      exp={
        <>
          Skips contracts that expire on the day the signal arrives.
          <FieldError id="skip0-e" text={err} />
        </>
      }
      act={
        <Switch
          act="filters"
          label="Skip same-day contracts"
          on={v ? !!v.rules.rule_skip_0dte : null}
          busy={busy}
          disabled={!!canEdit(page)}
          onChange={async (on) => {
            setBusy(true);
            setErr(await saveRules(page, { rule_skip_0dte: on }, (nv) => `Skip same-day contracts: ${nv.rules.rule_skip_0dte ? "on" : "off"}.`));
            setBusy(false);
          }}
        />
      }
    />
  );
}

/** The written rules: plain English the AI helper checks before every new
 *  trade (the classic policy save; null turns them off). */
function PolicyRow({ page }: { page: Page }) {
  const v = page.settings.view;
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const policy = v?.rules.policy ?? "";
  const ai = v?.ai.connected;
  const save = async (value: string | null) => {
    if (value != null && !value.trim()) return setErr("Write at least one rule, or press Turn off to stop using written rules.");
    if (value != null && value.length > 20000) return setErr("At most 20,000 characters.");
    setBusy(true);
    const e = await saveRules(page, { policy: value }, (nv) => (nv.rules.policy ? "Your written rules are saved." : "Written rules are off."));
    setBusy(false);
    if (e) setErr(e);
    else setOpen(false);
  };
  return (
    <div className="setrow" data-row="policy">
      <span className="lbl">Your written rules</span>
      <span className="exp">
        Checked by the AI helper before every new trade. If the helper is not connected, new trades are skipped rather than taken without the
        check.
        {v?.rules.policy_blocks_entries && (
          <span className="err" style={{ marginTop: 6 }}>
            <Icon name="alert" small />
            No AI helper is connected, so every new trade is skipped. Connect one in Settings, or turn the written rules off.
          </span>
        )}
      </span>
      <span className="val sm">
        {!v ? UNKNOWN : policy ? <span className="tag tag-plain">{ai ? "On, AI helper connected" : "On, no AI helper"}</span> : "None"}
      </span>
      <span className="act">
        {!open && (
          <button
            className="btn btn-sm"
            data-act="policy"
            disabled={!!canEdit(page)}
            onClick={() => {
              setText(policy);
              setErr("");
              setOpen(true);
              requestAnimationFrame(() => document.getElementById(id)?.focus());
            }}
          >
            {policy ? "Edit" : "Write rules"}
          </button>
        )}
      </span>
      {policy && !open && <p className="rule-text">{policy}</p>}
      {open && (
        <Editor error={err} errId={`${id}-e`} busy={busy} primary="Save written rules" onPrimary={() => save(text)} onCancel={() => setOpen(false)}>
          <div className="field">
            <label htmlFor={id}>Your rules, in plain English</label>
            <textarea
              id={id}
              className="input"
              rows={6}
              value={text}
              aria-invalid={!!err}
              aria-describedby={`${id}-h ${err ? `${id}-e` : ""}`}
              placeholder={"- Skip single stocks in the week they report earnings.\n- No new trades before 9:45 AM ET."}
              onChange={(e) => {
                setText(e.target.value);
                setErr("");
              }}
            />
            <span className="help" id={`${id}-h`}>
              They can only stop new trades; the agent always sells what it holds on the exit signal. This does not change Practice or LIVE.
            </span>
          </div>
          {policy && (
            <button type="button" className="btn btn-sm" disabled={busy} onClick={() => save(null)}>
              Turn off written rules
            </button>
          )}
        </Editor>
      )}
    </div>
  );
}

// ── the page ────────────────────────────────────────────────────────────────

export function RulesView({ page }: { page: Page }) {
  const s = page.s;
  const L = s.limits;
  const live = s.mode === "live";
  const used = live ? L.used_today.real : L.used_today.practice;
  const lost = live ? L.realised_loss_today.real : L.realised_loss_today.practice;
  const n = L.open_positions.real + L.open_positions.practice;
  const mu = L.trade_size.main.usd;
  const ou = L.trade_size.other.usd;
  const perTrade = mu != null && ou != null ? Math.max(mu, ou) : null;
  // the soonest expiry sell among what is held (ISO times sort as text)
  const guard = s.positions.map((p) => p.next.expiry_guard_at).filter((x): x is string => !!x).sort()[0] ?? null;
  const reload = page.settings.reload;
  useEffect(() => {
    reload();
  }, [reload]);
  const why = canEdit(page);
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Rules & limits</h1>
          <p className="lead">
            The values saved on your agent. Changing one never switches Practice or LIVE, and never buys or sells anything. A change to a limit
            or a trade size is confirmed first.
          </p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      {live && page.link === "ok" && (
        <div className="note note-info">
          <Icon name="info" />
          <h3>You are LIVE</h3>
          <p>A change to these values applies to real orders from the next signal.</p>
        </div>
      )}
      {why && page.link !== "lost" && !page.settings.view && (
        <p className="small muted" role="status">
          {why}
        </p>
      )}
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Daily limits</h2>
        </div>
        <LimitRow page={page} k="max_entries_per_day" exp="The most new positions the agent may open in one day. Selling is never limited." now={`${count(used)} used today`} />
        <LimitRow
          page={page}
          k="max_daily_loss_usd"
          exp="When the day's closed trades have lost this much, the agent opens nothing new until the next day. It is not a stop-loss: it never sells anything."
          now={
            <>
              <span className={lost > 0 ? "loss" : ""}>{usd(lost, 0)}</span> lost today
            </>
          }
        />
        <LimitRow page={page} k="max_open_positions" exp="The most positions held at the same time, real and practice counted together." now={`${count(n)} open now`} />
      </section>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Trade size</h2>
        </div>
        <SizeRow page={page} book="main" />
        <SizeRow page={page} book="other" />
      </section>
      <div className="two">
        <section className={`panel ${page.stale ? "stale" : ""}`}>
          <div className="ph">
            <h2>With these limits</h2>
            <span className="meta">Worked out from the values above</span>
          </div>
          <div className="pb">
            <ul className="tally">
              <li>
                <span>Most put into new trades in one day</span>
                <b>{perTrade != null ? usd(L.cap * perTrade, 0) : "depends on prices"}</b>
              </li>
              <li>
                <span>Most on one trade</span>
                <b>{perTrade != null ? usd(perTrade, 0) : "depends on prices"}</b>
              </li>
              <li>
                <span>Most held at once</span>
                <b>{plural(L.max_positions, "position")}</b>
              </li>
              <li>
                <span>Cash at {s.broker.name || "your broker"}</span>
                <b>{s.broker.buying_power != null ? usd(s.broker.buying_power) : UNKNOWN}</b>
              </li>
            </ul>
            <p className="small muted" style={{ marginTop: 8 }}>
              {perTrade == null ? "Your trade size is not a dollar amount, so there is no fixed dollar ceiling to show. " : ""}
              The most put in is also the most those trades can lose: an option can lose everything paid for it. The agent has no stop-loss
              and no profit target. It sells on the exit signal or on expiry day.
            </p>
          </div>
        </section>
        <section className="panel">
          <div className="ph">
            <h2>Always on</h2>
            <span className="meta">Part of how the agent works</span>
          </div>
          <div className="pb">
            <ul className="always">
              <li>
                <Icon name="shield" />
                <span>
                  <b>Always sells what it bought when the exit signal arrives,</b> whatever your entry rules say, and while new trades are
                  paused.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Runs every entry signal through the same checks, in the same order.</b> Activity shows which one stopped a signal.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Skips rather than guesses.</b> No price, or a signal that arrived late, means no order.
                </span>
              </li>
              <li>
                <Icon name="shield" />
                <span>
                  <b>Never goes LIVE by itself.</b> Going LIVE, and every change to a limit or a trade size, needs your confirmation in this
                  window.
                </span>
              </li>
              {guard && (
                <li>
                  <Icon name="shield" />
                  <span>
                    <b>Expiry sell time.</b> For what you hold now, the next one is {etWhen(guard, etDayKey(s.as_of))}.
                  </span>
                </li>
              )}
            </ul>
          </div>
        </section>
      </div>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Which signals to take</h2>
          <span className="meta">Saved as soon as you press Save; they only ever stop new trades</span>
        </div>
        {RULES.map(([k, label, exp]) => (
          <RuleRow key={k} page={page} k={k} label={label} exp={exp} />
        ))}
        <SkipSameDay page={page} />
        <PolicyRow page={page} />
      </section>
      <LimitsPanel page={page} linkLabel={null} />
    </div>
  );
}
