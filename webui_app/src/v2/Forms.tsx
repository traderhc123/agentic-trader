// The pieces every /v2 settings form shares: the confirm dialog that shows
// the SERVER's preview (Saved now → New, what it does not do) and applies
// the change only with the preview's token, the risk line shown while the
// owner types, inline errors, and the on/off switch.
//
// Nothing here assumes a save worked: a form calls onDone only when the
// server said so, and the page then reads the settings again and shows them.
import { useId, useState, type ReactNode } from "react";
import type { Change, Direction, Preview } from "../lib/settingsApi";
import { DIRECTION_WORDS, confirmLabel, valueWords } from "./lib/settings";
import { confirmBody, send, type Sent } from "./lib/http";
import { Dialog, Icon } from "./ui";

/** "This raises your risk." while the owner types (the page's own reading;
 *  the confirm dialog shows the server's). */
export function RiskLine({ direction, means, same }: { direction: Direction; means?: string; same?: boolean }) {
  const cls = same ? "same" : direction === "raises_risk" ? "up" : direction === "lowers_risk" ? "down" : "same";
  return (
    <p className={`risk ${cls}`} aria-live="polite">
      <Icon name={same ? "dash" : direction === "raises_risk" ? "up" : direction === "lowers_risk" ? "down" : "info"} small />
      <span>
        <b>{same ? "No change yet." : DIRECTION_WORDS[direction]}</b> {means}
      </span>
    </p>
  );
}

export function FieldError({ id, text }: { id: string; text: string }) {
  if (!text) return null;
  return (
    <span className="err" id={id} role="alert">
      <Icon name="alert" small />
      {text}
    </span>
  );
}

/** Saved now → New, one block per change, as the server's preview says. */
export function ChangeLines({ changes }: { changes: Change[] }) {
  return (
    <div className="changes">
      {changes.map((c) => (
        <div key={c.key} className="change-row">
          {changes.length > 1 && <h4>{c.label}</h4>}
          <div className="change">
            <span>
              <small>Saved now</small>
              {valueWords(c.key, c.saved_now)}
            </span>
            <Icon name="chev" />
            <span className="new">
              <small>New</small>
              {valueWords(c.key, c.new)}
            </span>
          </div>
          {c.direction !== "neutral" && <RiskLine direction={c.direction} />}
        </div>
      ))}
    </div>
  );
}

/** The confirm step of a two-step write. It renders the server's preview
 *  and sends the same payload again with the preview's token. A refused
 *  token (expired, settings changed meanwhile) is said inline, with Review
 *  again, which asks the server for a fresh preview. */
export function ReviewDialog<Done>({
  path,
  payload,
  first,
  title,
  lead,
  tone,
  cancel,
  confirm,
  ack,
  live,
  children,
  onClose,
  onDone,
}: {
  path: string;
  payload: Record<string, unknown>;
  /** the server's preview, from the first POST */
  first: Preview;
  title: string;
  lead?: ReactNode;
  tone?: "live" | "prac";
  /** the safe button's words ("Keep $500") */
  cancel: string;
  /** the confirm button's words; by default named after the change ("Raise to $1,150") */
  confirm?: string;
  /** a tick box the confirm needs (going LIVE) */
  ack?: string;
  /** the owner is LIVE: say the change applies to real orders */
  live?: boolean;
  /** more facts for this kind of change, from the preview */
  children?: (p: Preview) => ReactNode;
  onClose: () => void;
  onDone: (reply: Sent<Done> & { ok: true }) => void;
}) {
  const [preview, setPreview] = useState(first);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [again, setAgain] = useState(false);
  const [ticked, setTicked] = useState(false);
  const ackId = useId();

  const go = async () => {
    setBusy(true);
    setError("");
    const r = await send<Done>(path, confirmBody(payload, preview.token, ack ? ticked : undefined));
    setBusy(false);
    if (r.ok && !("needs_confirm" in r && r.needs_confirm)) return onDone(r as Sent<Done> & { ok: true });
    if (r.ok) {
      setPreview((r as { preview: Preview }).preview);
      return;
    }
    setError(r.error);
    // the tick box was not ticked: the same review can still be confirmed
    setAgain(!("needs_acknowledge" in r && r.needs_acknowledge));
  };
  const review = async () => {
    setBusy(true);
    const r = await send<Done>(path, payload);
    setBusy(false);
    if (r.ok && "needs_confirm" in r && r.needs_confirm) {
      setPreview(r.preview);
      setError("");
      setAgain(false);
    } else if (r.ok) onDone(r as Sent<Done> & { ok: true });
    else setError(r.error);
  };

  return (
    <Dialog
      tone={tone}
      title={title}
      lead={lead}
      onClose={() => !busy && onClose()}
      foot={
        <>
          {ack && !again && (
            <label className="check ack">
              <input type="checkbox" checked={ticked} onChange={(e) => setTicked(e.target.checked)} />
              <span id={ackId}>{ack}</span>
            </label>
          )}
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            {cancel}
          </button>
          {again ? (
            <button className="btn btn-primary" onClick={review} disabled={busy}>
              Review again
            </button>
          ) : (
            <button
              className={`btn ${tone === "live" ? "btn-golive" : "btn-primary"}`}
              id="dlg-ok"
              onClick={go}
              disabled={busy || (!!ack && !ticked)}
              aria-describedby={ack ? ackId : undefined}
            >
              {busy ? "Saving…" : confirm ?? confirmLabel(preview.changes)}
            </button>
          )}
        </>
      }
    >
      <ChangeLines changes={preview.changes} />
      {children?.(preview)}
      {live && (
        <p className="small">
          <span className="tag tag-real">Real money</span> You are LIVE. This applies to real orders from the next signal.
        </p>
      )}
      {preview.does_not.length > 0 && (
        <ul className="doesnot small muted">
          {preview.does_not.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      )}
      {error && (
        <div className="callout callout-bad" role="alert">
          <p className="err">
            <Icon name="alert" small />
            {error}
          </p>
        </div>
      )}
    </Dialog>
  );
}

/** An on/off setting that saves straight away (not money-affecting). It
 *  shows the saved state; while saving it says so, and after saving it shows
 *  what the server reports. */
export function Switch({
  on,
  label,
  act,
  busy,
  disabled,
  onChange,
}: {
  /** null: not known */
  on: boolean | null;
  label: string;
  act: string;
  busy: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      className="btn btn-sm switch"
      data-act={act}
      aria-checked={on == null ? "mixed" : on}
      aria-label={label}
      disabled={busy || disabled || on == null}
      onClick={() => on != null && onChange(!on)}
    >
      <span className="knob" aria-hidden="true" />
      {busy ? "Saving…" : on == null ? "Not known" : on ? "On" : "Off"}
    </button>
  );
}

/** The first POST of a two-step write: the preview, or why there is none. */
export type Asked = { kind: "review"; preview: Preview } | { kind: "nothing"; note: string } | { kind: "error"; error: string };

export async function askPreview(path: string, payload: Record<string, unknown>): Promise<Asked> {
  const r = await send(path, payload);
  if (r.ok && "needs_confirm" in r && r.needs_confirm) return { kind: "review", preview: r.preview };
  if (r.ok) return { kind: "nothing", note: (r as { note?: string }).note || "Nothing to change: those are your settings already." };
  return { kind: "error", error: r.error };
}
