// The dialogs of Settings → Code changes. Their buttons live in
// Settings.tsx (one home each); these are what the buttons open.
//
//   Review   GET /api/proposal (the classic read, as it is): the AI helper's
//            explanation, its warnings and the diff. Reject is the classic
//            POST /api/proposal/reject (it only throws the proposal away).
//            Apply is POST /api/v2/proposal/apply, two steps, bound to the
//            sha256 of the diff shown here: the server applies only the
//            proposal whose whole diff is exactly this one, made against the
//            files as they are now. A diff too large to show whole (the
//            server says truncated) is never offered for Apply.
//   Ask      POST /api/v2/proposal/request: the AI helper writes a change and
//            leaves it waiting here. The words are never run as a command.
import { useEffect, useId, useState } from "react";
import type { ApplyDone, Preview, ProposalView } from "../../lib/settingsApi";
import { FieldError, ReviewDialog, askPreview } from "../Forms";
import { API, getJson, send } from "../lib/http";
import { diffSha256 } from "../lib/settings";
import { Dialog, Icon } from "../ui";
import type { Page } from "../App";

type Shown = { kind: "reading" } | { kind: "none" } | { kind: "error"; text: string } | { kind: "shown"; p: ProposalView & { pending: true }; sha: string | null };

export function ProposalDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const [shown, setShown] = useState<Shown>({ kind: "reading" });
  const [review, setReview] = useState<Preview | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    (async () => {
      const r = await getJson<ProposalView>(API.proposal);
      if (!r.ok) return setShown({ kind: "error", text: r.error });
      if (!r.body.pending) return setShown({ kind: "none" });
      const p = r.body as ProposalView & { pending: true };
      if (typeof p.diff !== "string") return setShown({ kind: "error", text: "The agent answered in a form this page can't read. Try again." });
      setShown({ kind: "shown", p, sha: await diffSha256(p.diff) });
    })();
  }, []);

  const cut = shown.kind === "shown" && shown.p.truncated !== false;
  const apply = async () => {
    if (shown.kind !== "shown" || !shown.sha || cut) return;
    setErr("");
    setBusy(true);
    const a = await askPreview(API.proposalApply, { diff_sha256: shown.sha });
    setBusy(false);
    if (a.kind === "review") setReview(a.preview);
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  const reject = async () => {
    setErr("");
    setBusy(true);
    const r = await send(API.proposalReject, {});
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    page.refresh();
    page.say(true, "Rejected. The proposed code change was thrown away; nothing was changed.");
    onClose();
  };

  if (review && shown.kind === "shown" && shown.sha)
    return (
      <ReviewDialog<ApplyDone>
        path={API.proposalApply}
        payload={{ diff_sha256: shown.sha }}
        first={review}
        title={review.restarts ? "Apply this code change and restart the agent?" : "Apply this code change?"}
        lead="The agent's own code is changed by what its AI helper wrote. Apply it only if you have read the change and want it."
        cancel="Not now"
        confirm={review.restarts ? "Apply and restart the agent" : "Apply the change"}
        onClose={() => setReview(null)}
        onDone={(r) => {
          const reply = (r as unknown as ApplyDone).reply;
          page.refresh();
          page.say(true, typeof reply === "string" ? `The agent answered: ${reply}` : "Applied.");
          onClose();
        }}
      >
        {(p) => (
          <>
            <dl className="facts">
              <dt>Files it changes</dt>
              <dd>{p.files?.length ? p.files.join(", ") : "Not listed"}</dd>
              <dt>Then</dt>
              <dd>
                {p.restarts
                  ? "The agent restarts with the new code. This page loses the connection for a few seconds."
                  : "Nothing restarts here: the agent uses the new code the next time it starts."}
                <small>It never restarts in the middle of an order. The files it replaces are kept in a backup folder; the agent's reply names it.</small>
              </dd>
            </dl>
            {!!p.warnings?.length && (
              <div className="callout callout-warn" role="alert">
                {p.warnings.map((w) => (
                  <p key={w}>
                    <Icon name="alert" small />
                    {w}
                  </p>
                ))}
              </div>
            )}
          </>
        )}
      </ReviewDialog>
    );

  return (
    <Dialog
      title="Proposed code change"
      lead={shown.kind === "shown" ? "Written by your AI helper. Nothing changes until you apply it." : undefined}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Close
          </button>
          {shown.kind === "shown" && (
            <>
              <button className="btn" onClick={reject} disabled={busy}>
                Reject it
              </button>
              {!cut && (
                <button className="btn btn-primary" id="dlg-ok" onClick={apply} disabled={busy || !shown.sha}>
                  {busy ? "Asking the agent…" : "Review and apply…"}
                </button>
              )}
            </>
          )}
        </>
      }
    >
      {shown.kind === "reading" && (
        <p className="wait" role="status">
          <span className="spin" aria-hidden="true" />
          Reading the proposed change…
        </p>
      )}
      {shown.kind === "none" && <p role="status">No code change is waiting.</p>}
      {shown.kind === "error" && (
        <div className="callout callout-bad" role="alert">
          <p className="err">
            <Icon name="alert" small />
            {shown.text}
          </p>
        </div>
      )}
      {shown.kind === "shown" && (
        <>
          {shown.p.explanation && <p className="explain">{shown.p.explanation}</p>}
          {shown.p.warnings?.length > 0 && (
            <div className="callout callout-warn" role="alert">
              {shown.p.warnings.map((w) => (
                <p key={w}>
                  <Icon name="alert" small />
                  {w}
                </p>
              ))}
            </div>
          )}
          {cut && (
            <div className="callout callout-warn" role="alert">
              <p>
                <Icon name="alert" small />
                {shown.p.truncated
                  ? "This change is too large to show here in full: the diff below stops part way. Part of it can't be read here, so it can't be applied from this page. Reject it and ask for a smaller change."
                  : "This agent did not say whether the diff below is the whole change, so it can't be applied from this page. Update the agent, or reject the change."}
              </p>
            </div>
          )}
          <pre className="helptext helptext-long diff" tabIndex={0} aria-label="The change, as a diff">
            {shown.p.diff}
          </pre>
          {!shown.sha && !cut && (
            <p className="small">This browser can't fingerprint the change, so it can't be applied from here. Use a current browser on this computer.</p>
          )}
        </>
      )}
      <FieldError id="proposal-e" text={err} />
    </Dialog>
  );
}

/** Ask the AI helper for a change: it is proposed, never run. */
export function RequestDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const ask = async () => {
    const t = text.trim();
    if (!t) return setErr("Describe the change you want.");
    if (t.length > 4000) return setErr("At most 4,000 characters.");
    setErr("");
    setBusy(true);
    const r = await send<{ note?: string }>(API.proposalRequest, { request: t });
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    page.refresh();
    page.say(true, `${(r as { note?: string }).note || "Proposal ready."} Review it under Code changes.`);
    onClose();
  };
  return (
    <Dialog
      title="Ask for a code change"
      lead="Describe what you want changed, for example a connector for another broker. Your AI helper writes it, and it waits here for you to read. Nothing changes until you apply it."
      onClose={() => !busy && onClose()}
      foot={
        <>
          <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
            Cancel
          </button>
          <button className="btn btn-primary" id="dlg-ok" onClick={ask} disabled={busy}>
            {busy ? "Writing…" : "Ask the AI helper"}
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor={`${id}-t`}>The change you want</label>
        <textarea
          id={`${id}-t`}
          className="input"
          rows={5}
          maxLength={4000}
          value={text}
          disabled={busy}
          onChange={(e) => setText(e.target.value)}
          aria-invalid={!!err || undefined}
          aria-describedby={`${id}-h${err ? ` ${id}-e` : ""}`}
        />
        <span className="help" id={`${id}-h`}>
          It is a request to the AI helper only: words like “pause” or “go live” here are never run as commands.
        </span>
        <FieldError id={`${id}-e`} text={err} />
      </div>
      {busy && (
        <p className="wait" role="status" aria-live="polite">
          <span className="spin" aria-hidden="true" />
          Your AI helper is writing the change. This can take several minutes; keep this open.
        </p>
      )}
    </Dialog>
  );
}
