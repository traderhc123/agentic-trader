// Settings: connections (broker, AI helper, alerts) and this computer
// (start at login, keep awake, move, stop), plus this dashboard (theme, the
// way back to the classic one). Everything is changed here, in /v2:
// connecting or replacing a broker is confirmed against the server's
// preview first; the rest saves straight away. After every save the page
// reads the settings again and shows those.
import { useEffect, useId, useRef, useState } from "react";
import type { AiModels, AiProviders, BrokerForm, Preview, Secret, SettingsView, SignInDone } from "../../lib/settingsApi";
import { FieldError, ReviewDialog, Switch, askPreview } from "../Forms";
import { brokerLabel } from "../lib/derive";
import { API, getJson, send } from "../lib/http";
import { MOVE_TOOL, RH_OPTIONS_URL, brokerName, signInOutcome } from "../lib/settings";
import { badgeFor, updateLine } from "../lib/update";
import { UPDATE } from "../lib/copy";
import { UNKNOWN, etDayKey, etTime, etWhen, usd } from "../lib/format";
import { Dialog, Icon, Stamp } from "../ui";
import type { Page } from "../App";
import { Row } from "./Pages";

const SOURCE_NAMES: Record<string, string> = { agenthc: "AgentHC Day Trade Ideas" };
const sourceName = (id: string) => (Object.prototype.hasOwnProperty.call(SOURCE_NAMES, id) ? SOURCE_NAMES[id] : id);
const plain = (t: string, strong = true) => (
  <span className="tag tag-plain" style={strong ? { color: "var(--ink)" } : undefined}>
    {t}
  </span>
);
const secretWords = (x: Secret | undefined) => (!x?.set ? "Not set" : x.last4 ? `Set, ending ${x.last4}` : "Set");
const isSecret = (x: unknown): x is Secret => !!x && typeof x === "object" && "set" in (x as object);

function cantEdit(page: Page): string {
  if (page.link === "lost") return "This page can't reach the agent, so nothing can be changed right now.";
  if (!page.settings.view) return page.settings.error || "Reading your settings…";
  return "";
}

// ── broker ──────────────────────────────────────────────────────────────────

type BrokerStep =
  | { kind: "pick" }
  | { kind: "review"; payload: Record<string, unknown>; preview: Preview; rh: boolean; name: string }
  | { kind: "signin"; url: string; since: number }
  | { kind: "ended"; text: string; warn: boolean };

function BrokerDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [forms, setForms] = useState<BrokerForm[] | null>(null);
  const [pick, setPick] = useState<string>(v.broker.id ?? "robinhood");
  const [vals, setVals] = useState<Record<string, string | boolean>>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<BrokerStep>({ kind: "pick" });
  const [waitWords, setWaitWords] = useState("Waiting for you to finish signing in at Robinhood…");
  const timer = useRef<ReturnType<typeof setInterval>>();
  const id = useId();
  useEffect(() => {
    getJson<{ brokers: BrokerForm[] }>(API.brokers).then((r) => setForms(r.ok && Array.isArray(r.body.brokers) ? r.body.brokers : []));
    return () => clearInterval(timer.current);
  }, []);
  const form = forms?.find((f) => f.id === pick);
  // a broker's switches start from what is saved (its own broker) or its default
  useEffect(() => {
    if (!form) return setVals({});
    const out: Record<string, string | boolean> = {};
    for (const f of form.fields) {
      const saved = v.broker.id === form.id ? v.broker.fields[f.id] : undefined;
      out[f.id] = f.type === "checkbox" ? (typeof saved === "boolean" ? saved : f.default === true) : "";
    }
    setVals(out);
  }, [form, v.broker]);
  const replacing = !!v.broker.id;

  const review = async () => {
    setErr("");
    setBusy(true);
    const rh = pick === "robinhood";
    const payload = rh ? {} : { broker: pick, values: vals };
    const a = await askPreview(rh ? API.robinhood : API.brokerConnect, payload);
    setBusy(false);
    if (a.kind === "review") setStep({ kind: "review", payload, preview: a.preview, rh, name: rh ? "Robinhood" : brokerName(pick) });
    else setErr(a.kind === "error" ? a.error : a.note);
  };

  if (step.kind === "review")
    return (
      <ReviewDialog<SignInDone & { note?: string }>
        path={step.rh ? API.robinhood : API.brokerConnect}
        payload={step.payload}
        first={step.preview}
        title={replacing && v.broker.id !== (step.rh ? "robinhood" : pick) ? `Replace ${brokerName(v.broker.id)} with ${step.name}?` : `Connect ${step.name}?`}
        lead={step.rh ? "You sign in at Robinhood in a new tab. Nothing changes here until you finish there." : "The agent checks the keys with the broker before it saves them."}
        cancel={replacing ? `Keep ${brokerName(v.broker.id)}` : "Not now"}
        confirm={step.rh ? "Sign in at Robinhood" : `Connect ${step.name}`}
        live={page.s.mode === "live"}
        onClose={onClose}
        onDone={async (r) => {
          if (step.rh) {
            const { url, signin_seq: since } = r as unknown as SignInDone;
            if (typeof url !== "string" || !/^https:\/\//.test(url) || typeof since !== "number") {
              setStep({ kind: "pick" });
              setErr("The agent did not hand back a Robinhood sign-in address. Nothing was changed. Try again.");
              return;
            }
            setStep({ kind: "signin", url, since });
            clearInterval(timer.current);
            timer.current = setInterval(async () => {
              const nv = await page.settings.reload();
              const out = signInOutcome(since, nv?.broker.signin);
              if (out.kind === "wait") return setWaitWords(nv ? "Waiting for you to finish signing in at Robinhood…" : "Waiting for you to finish signing in at Robinhood… (this page can't reach the agent right now; it keeps trying)");
              clearInterval(timer.current);
              page.refresh();
              if (out.kind === "done") {
                page.say(true, out.text);
                return onClose();
              }
              // a warning or a failure stays on screen until the owner has read it
              setStep({ kind: "ended", text: out.text, warn: out.kind === "warn" });
            }, 3000);
            return;
          }
          const nv = await page.settings.reload();
          page.refresh();
          const note = (r as { note?: string }).note;
          page.say(true, nv ? `Connected. Broker: ${brokerName(nv.broker.id)}${nv.broker.paper ? ", paper-trading account" : ""}.${note ? ` The broker said: ${note}` : ""}` : "Connected. This page could not read your settings back yet.");
          onClose();
        }}
      />
    );

  if (step.kind === "ended")
    return (
      <Dialog
        title={step.warn ? "Connected, but options are off" : "Robinhood was not connected"}
        onClose={onClose}
        foot={
          <>
            <button className="btn" onClick={onClose} data-autofocus>
              Close
            </button>
            {step.warn && (
              <a className="btn btn-primary" href={RH_OPTIONS_URL} target="_blank" rel="noopener noreferrer">
                <Icon name="ext" small />
                Apply for options at Robinhood
              </a>
            )}
          </>
        }
      >
        <div className={`callout ${step.warn ? "callout-warn" : "callout-bad"}`} role="alert">
          <p className={step.warn ? undefined : "err"}>
            <Icon name="alert" small />
            {step.text}
          </p>
        </div>
      </Dialog>
    );

  if (step.kind === "signin")
    return (
      <Dialog
        title="Finish signing in at Robinhood"
        lead="Open the sign-in, approve the connection at Robinhood, then come back to this window. It updates by itself."
        onClose={onClose}
        foot={
          <>
            <button className="btn" onClick={onClose} data-autofocus>
              Close
            </button>
            <a className="btn btn-primary" href={step.url} target="_blank" rel="noopener noreferrer">
              <Icon name="ext" small />
              Open the Robinhood sign-in
            </a>
          </>
        }
      >
        <p className="wait" role="status" aria-live="polite">
          <span className="spin" aria-hidden="true" />
          {waitWords}
        </p>
      </Dialog>
    );

  return (
    <Dialog
      title={replacing ? "Change broker" : "Connect a broker"}
      lead="The account the agent places real orders in when it is LIVE. Connecting never switches Practice or LIVE."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={review} disabled={busy || forms == null}>
            {busy ? "Asking the agent…" : "Review"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-b`}>Broker</label>
        <select id={`${id}-b`} className="input" value={pick} onChange={(e) => (setPick(e.target.value), setErr(""))}>
          <option value="robinhood">Robinhood (sign in at Robinhood)</option>
          {(forms ?? []).map((f) => (
            <option key={f.id} value={f.id}>
              {f.name}
            </option>
          ))}
        </select>
        {forms == null && <span className="help">Reading the list of brokers…</span>}
      </div>
      {form?.fields.map((f) =>
        f.type === "checkbox" ? (
          <label key={f.id} className="check">
            <input type="checkbox" checked={vals[f.id] === true} onChange={(e) => setVals((x) => ({ ...x, [f.id]: e.target.checked }))} />
            <span>{f.label}</span>
          </label>
        ) : (
          <div key={f.id} className="field">
            <label htmlFor={`${id}-${f.id}`}>{f.label}</label>
            <input
              id={`${id}-${f.id}`}
              className="input"
              type={f.type === "password" ? "password" : "text"}
              autoComplete="off"
              spellCheck={false}
              value={String(vals[f.id] ?? "")}
              onChange={(e) => setVals((x) => ({ ...x, [f.id]: e.target.value }))}
            />
            {v.broker.id === form.id && isSecret(v.broker.fields[f.id]) && (v.broker.fields[f.id] as Secret).set && (
              <span className="help">Saved now: {secretWords(v.broker.fields[f.id] as Secret)}. Connecting again needs it typed in again.</span>
            )}
          </div>
        ),
      )}
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

// ── AI helper ───────────────────────────────────────────────────────────────

function AiDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [prov, setProv] = useState<AiProviders["providers"] | null>(null);
  const [provider, setProvider] = useState(v.ai.provider ?? "");
  const [key, setKey] = useState("");
  const [base, setBase] = useState("");
  const [models, setModels] = useState<string[] | null>(null);
  const [model, setModel] = useState(v.ai.model ?? "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  useEffect(() => {
    getJson<AiProviders>(API.aiProviders).then((r) => setProv(r.ok ? r.body.providers : {}));
  }, []);
  const keepKey = provider === v.ai.provider && v.ai.key.set;
  const find = async () => {
    setErr("");
    setBusy(true);
    const r = await send<{ models: string[]; suggested: string; provider: string }>(API.aiModels, { provider, key, base_url: base });
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    const m = r as unknown as AiModels & { ok: true };
    setModels(m.models);
    if (m.provider && m.provider !== provider) setProvider(m.provider);
    setModel(m.models.includes(model) ? model : m.suggested);
  };
  const save = async () => {
    if (!model) return setErr("Pick a model first (Find models lists them).");
    setErr("");
    setBusy(true);
    const r = await send(API.ai, { provider, key, model, base_url: base });
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    const nv = await page.settings.reload();
    page.refresh();
    page.say(true, nv ? `Saved. AI helper: ${nv.ai.name ?? nv.ai.provider ?? "connected"}${nv.ai.model ? `, ${nv.ai.model}` : ""}.` : "Saved. This page could not read your settings back yet.");
    onClose();
  };
  return (
    <Dialog
      title="AI helper"
      lead="The AI helper checks your written rules before each new trade and answers questions. It never places orders. Saving tries the model once first."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={save} disabled={busy || !models}>
            {busy ? "Asking…" : "Save"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-p`}>Provider</label>
        <select id={`${id}-p`} className="input" value={provider} onChange={(e) => (setProvider(e.target.value), setModels(null))}>
          <option value="">Work it out from the key</option>
          {Object.entries(prov ?? {}).map(([k, p]) => (
            <option key={k} value={k}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor={`${id}-k`}>API key</label>
        <input
          id={`${id}-k`}
          className="input"
          type="password"
          autoComplete="off"
          value={key}
          placeholder={keepKey ? "Leave empty to keep the saved key" : ""}
          onChange={(e) => (setKey(e.target.value), setModels(null))}
        />
        {keepKey && <span className="help">Saved now: {secretWords(v.ai.key)}.</span>}
        {provider && prov?.[provider]?.keys_url && (
          <span className="help">
            Get a key at <span className="nowrap">{prov[provider].keys_url}</span>
          </span>
        )}
      </div>
      {provider === "custom" && (
        <div className="field">
          <label htmlFor={`${id}-u`}>Endpoint (https://)</label>
          <input id={`${id}-u`} className="input" autoComplete="off" value={base} onChange={(e) => (setBase(e.target.value), setModels(null))} />
        </div>
      )}
      {models ? (
        <div className="field">
          <label htmlFor={`${id}-m`}>Model</label>
          <select id={`${id}-m`} className="input" value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div>
          <button className="btn" onClick={find} disabled={busy}>
            Find models
          </button>
        </div>
      )}
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

// ── alerts ──────────────────────────────────────────────────────────────────

const ALERTS: ["discord" | "ntfy" | "tg_token" | "tg_chat", string, string][] = [
  ["discord", "Discord webhook", "https://discord.com/api/webhooks/…"],
  ["ntfy", "ntfy topic", "a private topic name"],
  ["tg_token", "Telegram bot token", ""],
  ["tg_chat", "Telegram chat id", ""],
];

function AlertsDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const v = page.settings.view as SettingsView;
  const [text, setText] = useState<Record<string, string>>({});
  const [drop, setDrop] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const save = async () => {
    const payload: Record<string, string | null> = {};
    for (const [k] of ALERTS) {
      if (drop[k]) payload[k] = null;
      else if ((text[k] ?? "").trim()) payload[k] = text[k].trim();
    }
    if (payload.discord && !payload.discord.startsWith("https://")) return setErr("The Discord webhook is an address starting with https://.");
    if (!Object.keys(payload).length) return setErr("Nothing to change: type a new value, or tick Remove.");
    setErr("");
    setBusy(true);
    const r = await send(API.alerts, payload);
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    const nv = await page.settings.reload();
    page.say(true, nv ? `Saved. Alerts: ${ALERTS.filter(([k]) => nv.alerts[k].set).map(([, l]) => l).join(", ") || "none"}.` : "Saved. This page could not read your settings back yet.");
    onClose();
  };
  return (
    <Dialog
      title="Alerts"
      lead="Where the agent sends a message when something needs you. Leave a field empty to keep what is saved."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={save} disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      {ALERTS.map(([k, label, ph]) => (
        <div key={k} className="field">
          <label htmlFor={`${id}-${k}`}>{label}</label>
          <input
            id={`${id}-${k}`}
            className="input"
            type={k === "tg_chat" ? "text" : "password"}
            autoComplete="off"
            spellCheck={false}
            placeholder={ph}
            disabled={!!drop[k]}
            value={text[k] ?? ""}
            onChange={(e) => setText((x) => ({ ...x, [k]: e.target.value }))}
          />
          <span className="help">Saved now: {secretWords(v.alerts[k])}.</span>
          {v.alerts[k].set && (
            <label className="check">
              <input type="checkbox" checked={!!drop[k]} onChange={(e) => setDrop((x) => ({ ...x, [k]: e.target.checked }))} />
              <span>Remove it</span>
            </label>
          )}
        </div>
      ))}
      <FieldError id={`${id}-e`} text={err} />
    </Dialog>
  );
}

// ── this computer ───────────────────────────────────────────────────────────

function ComputerSwitch({ page, k, act, label }: { page: Page; k: "autostart" | "keep_awake"; act: string; label: string }) {
  const v = page.settings.view;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const on = !v ? null : k === "autostart" ? v.computer.autostart.enabled : v.computer.keep_awake;
  const save = async (next: boolean) => {
    setBusy(true);
    setErr("");
    const r = await send<{ autostart_note?: string }>(API.computer, { [k]: next });
    if (!r.ok) {
      setBusy(false);
      return setErr(r.error);
    }
    const nv = await page.settings.reload();
    setBusy(false);
    const now = nv ? (k === "autostart" ? nv.computer.autostart.enabled : nv.computer.keep_awake) : null;
    const note = (r as { autostart_note?: string }).autostart_note;
    page.say(true, `Saved. ${label}: ${now == null ? "not read back yet" : now ? "on" : "off"}.${note ? ` ${note}` : ""}`);
  };
  return (
    <>
      <Switch act={act} label={label} on={on} busy={busy} disabled={!!cantEdit(page)} onChange={save} />
      <FieldError id={`${k}-e`} text={err} />
    </>
  );
}

/** The page load's update check, said quietly: the band's "Update
 *  available" button is the one place it is installed from. */
function UpdateRow({ page }: { page: Page }) {
  const u = page.update;
  const line = updateLine(u.check, u.phase, u.detail);
  const offered = line.review && badgeFor(u.check, u.phase, !page.stale) != null;
  return (
    <Row
      label={UPDATE.rowLabel}
      exp={
        <span className="upd-said">
          {line.text}
          {offered ? ` Press “${UPDATE.badgeShort}” at the top of the window to review it.` : ""}
        </span>
      }
    />
  );
}

// ── the page ────────────────────────────────────────────────────────────────

export function SettingsView({ page }: { page: Page }) {
  const s = page.s;
  const b = s.broker;
  const bad = b.connected === false;
  const today = etDayKey(s.as_of);
  const a = s.agent;
  const v = page.settings.view;
  const why = cantEdit(page);
  const [open, setOpen] = useState<"broker" | "ai" | "alerts" | null>(null);
  const reload = page.settings.reload;
  useEffect(() => {
    reload();
  }, [reload]);
  const auto = v?.computer.autostart;
  return (
    <div className="view">
      <div className="view-h">
        <div>
          <h1>Settings</h1>
          <p>Connections and this computer, changed here. Nothing on this page switches Practice or LIVE, or buys or sells anything.</p>
        </div>
        {page.stale && <Stamp at={page.asOf} />}
      </div>
      {why && page.link !== "lost" && !v && (
        <p className="small muted" role="status">
          {why}
        </p>
      )}
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>Connections</h2>
        </div>
        <Row
          label={`Broker: ${brokerLabel(s)}`}
          exp={
            bad ? (
              <>
                <b style={{ color: "var(--bad)" }}>Not connected{b.since ? ` since ${etTime(b.since)}` : ""}.</b> The agent cannot buy or sell
                until it is connected again.{b.last_error ? ` The broker's last answer: “${b.last_error}”.` : ""}
              </>
            ) : b.connected == null ? (
              "This broker connection does not report whether it is up."
            ) : (
              <>
                {b.last_answer ? `Answered at ${etTime(b.last_answer)}. ` : ""}
                {b.buying_power != null ? `Cash available ${usd(b.buying_power)}. ` : ""}
                {b.paper_account ? "This is the broker's paper-trading account." : ""}
              </>
            )
          }
          val={bad ? <span className="tag tag-bad">Not connected</span> : b.connected == null ? <span className="tag tag-unk">Unknown</span> : plain("Connected")}
          act={
            <button className={`btn btn-sm ${bad ? "btn-primary" : ""}`} data-act="broker" aria-haspopup="dialog" disabled={!!why} title={why || undefined} onClick={() => setOpen("broker")}>
              {bad ? "Connect again…" : v?.broker.id ? "Change broker…" : "Connect a broker…"}
            </button>
          }
        />
        <Row
          label="AI helper"
          exp={
            !v
              ? UNKNOWN
              : v.ai.connected
                ? `${v.ai.name ?? v.ai.provider}${v.ai.model ? `, model ${v.ai.model}` : ""}${v.ai.endpoint ? ` at ${v.ai.endpoint}` : ""}. Key: ${secretWords(v.ai.key).toLowerCase()}.`
                : "Not connected. Your written rules need it; nothing else does."
          }
          val={v ? plain(v.ai.connected ? "Connected" : "Not connected", v.ai.connected) : undefined}
          act={
            <button className="btn btn-sm" data-act="ai" aria-haspopup="dialog" disabled={!!why} title={why || undefined} onClick={() => setOpen("ai")}>
              {v?.ai.connected ? "Change…" : "Connect…"}
            </button>
          }
        />
        <Row
          label="Alerts"
          exp={
            !v
              ? UNKNOWN
              : ALERTS.some(([k]) => v.alerts[k].set)
                ? `Set: ${ALERTS.filter(([k]) => v.alerts[k].set).map(([, l]) => l).join(", ")}.`
                : "None set. The agent tells you things only on this dashboard."
          }
          act={
            <button className="btn btn-sm" data-act="alerts" aria-haspopup="dialog" disabled={!!why} title={why || undefined} onClick={() => setOpen("alerts")}>
              Change…
            </button>
          }
        />
        <Row
          label={`Signals${s.signals.source ? `: ${sourceName(s.signals.source)}` : ""}`}
          exp={s.signals.last_signal ? `Newest signal seen ${etWhen(s.signals.last_signal, today)}.` : "No signal seen yet."}
          val={plain(s.signals.reading ? "Reading" : "Not reading", s.signals.reading)}
          act={
            <button className="btn btn-sm" data-go="signals" onClick={() => page.go("signals")}>
              Signals & pass
            </button>
          }
        />
      </section>
      <section className={`panel ${page.stale ? "stale" : ""}`}>
        <div className="ph">
          <h2>This computer</h2>
        </div>
        <Row
          label="Agent"
          exp={
            !a.running
              ? "No agent is running behind this page. Start it with the button at the top of the window."
              : `${a.started_at ? `Started ${etWhen(a.started_at, today)}. ` : ""}${a.last_heartbeat ? `Last report at ${etTime(a.last_heartbeat)}.` : "No report yet."}`
          }
          val={!a.running ? <span className="tag tag-bad">Stopped</span> : a.answering ? plain("Running") : <span className="tag tag-unk">Not answering</span>}
        />
        <Row
          label="Start at login"
          exp={
            !v
              ? UNKNOWN
              : !auto?.supported
                ? "Not available on this computer: start the agent yourself after a restart."
                : `Starts the agent when you log in to this computer${auto.how ? ` (${auto.how})` : ""}.`
          }
          act={auto?.supported || !v ? <ComputerSwitch page={page} k="autostart" act="autostart" label="Start at login" /> : undefined}
        />
        <Row label="Keep awake" exp="Stops this computer from sleeping while the agent runs, so it doesn't miss signals." act={<ComputerSwitch page={page} k="keep_awake" act="keep-awake" label="Keep awake" />} />
        <Row
          label="Icon and window frame"
          exp="The frame around this window shows the mode without reading anything."
          val={
            <span className="legendrow">
              <span>
                <i className="glyph glyph-live" />
                LIVE
              </span>
              <span>
                <i className="glyph glyph-prac" />
                Practice
              </span>
              <span>
                <i className="glyph glyph-unk" />
                Not answering
              </span>
            </span>
          }
        />
        <UpdateRow page={page} />
        <Row
          label="Move to another computer"
          exp="Packs this setup into one passphrase-locked file for the other computer. Opens the move tool, which is in the classic dashboard."
          act={
            <a className="btn btn-sm" data-act="move" href={MOVE_TOOL}>
              <Icon name="ext" small />
              Open the move tool
            </a>
          }
        />
        {a.running && (
          <Row
            label="Stop the agent completely"
            exp="Nothing is bought, sold or watched until you start it again. Open positions stay open at your broker."
            act={
              <button className="btn btn-sm" data-act="stop" onClick={() => page.act("stop")} disabled={page.busy || page.link !== "ok"}>
                Stop the agent…
              </button>
            }
          />
        )}
      </section>
      <section className="panel">
        <div className="ph">
          <h2>This dashboard</h2>
        </div>
        <Row
          label="Classic dashboard"
          exp="The dashboard you had before is still there, with the question box, help, tips and the wallet."
          act={
            <a className="btn btn-sm" data-act="classic" href="/dash">
              Back to the classic dashboard
            </a>
          }
        />
        <Row
          label="Theme"
          exp="Navy (the classic dashboard's) or light."
          act={
            <button className="btn btn-sm" data-act="theme" onClick={page.toggleTheme}>
              <Icon name={page.dark ? "sun" : "moon"} small />
              {page.dark ? "Light theme" : "Dark theme"}
            </button>
          }
        />
      </section>
      {open === "broker" && v && <BrokerDialog page={page} onClose={() => setOpen(null)} />}
      {open === "ai" && v && <AiDialog page={page} onClose={() => setOpen(null)} />}
      {open === "alerts" && v && <AlertsDialog page={page} onClose={() => setOpen(null)} />}
    </div>
  );
}
