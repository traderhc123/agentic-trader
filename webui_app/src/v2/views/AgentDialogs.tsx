// The dialogs of Settings → The agent on this computer and → This computer:
// Stop, Start, and Move to another computer. Their buttons live in
// Settings.tsx (one home each); these are what the buttons open.
//
//   Stop   POST /api/v2/command {action: "stop"}: two steps. The server's
//          preview lists the real positions nobody sells while it is stopped
//          (the confirmation is refused if that list changed meanwhile).
//   Start  POST /api/v2/agent/start: the setup wizard's "Start my agent now".
//          Two steps when the saved mode is LIVE; straight away in practice.
//   Move   the classic move tool, in-page: the move file is made and
//          downloaded through the classic /api/move/export and
//          /api/move/download as they are; retiring this computer
//          (POST /api/v2/move/finish) and moving to a cloud server
//          (POST /api/v2/move/deploy, progress from /api/deploy/status) are
//          two steps, both irreversible.
// Nothing is assumed to have worked: what the server answers is shown, and
// the page reads the status again.
import { useEffect, useId, useRef, useState } from "react";
import type { CommandDone, DeployStatus, MoveDone, MoveFile, Preview, StartDone, Unsold } from "../../lib/settingsApi";
import { FieldError, ReviewDialog, askPreview, type Asked } from "../Forms";
import { API, getJson, send } from "../lib/http";
import { DEPLOY_REGIONS, SETUP_FROM_V2, movePassProblem, sizeWords, lossWords, unsoldWords } from "../lib/settings";
import { UNKNOWN, count } from "../lib/format";
import { Dialog, Icon, WillWont } from "../ui";
import type { Page } from "../App";

const their = (n: number, one: string, many: string) => (n === 1 ? one : many);

/** The real positions nobody sells here afterwards, as the server listed them. */
function UnsoldList({ p, after }: { p: Preview; after: string }) {
  const u: Unsold[] | null | undefined = p.unsold;
  if (u == null)
    return (
      <div className="callout callout-real">
        <p className="small">The agent could not list your real positions just now. Check them at your broker before you go on.</p>
      </div>
    );
  if (!u.length) return <p className="small muted">You have no real positions open, so nothing is left unsold.</p>;
  return (
    <div className="callout callout-real">
      <h4>
        Nobody sells {their(u.length, "this real position", `these ${count(u.length)} real positions`)} {after}
      </h4>
      <ul className="poslist">
        {u.map((x) => (
          <li key={x.id}>
            <span>{unsoldWords(x)}</span>
          </li>
        ))}
      </ul>
      <p className="small">Exit signals and the expiry sell are not acted on. Sell at your broker yourself if you need to.</p>
    </div>
  );
}

/** While the first POST asks the server for its preview, or when it gives none. */
export function Asking({ title, ask, onClose }: { title: string; ask: { kind: "asking" } | Asked; onClose: () => void }) {
  return (
    <Dialog
      title={title}
      lead={ask.kind === "asking" ? "Asking the agent what this would change…" : undefined}
      onClose={onClose}
      foot={
        <button className="btn" onClick={onClose} data-autofocus>
          {ask.kind === "asking" ? "Cancel" : "Close"}
        </button>
      }
    >
      {ask.kind === "asking" ? (
        <p className="wait" role="status">
          <span className="spin" aria-hidden="true" />
          Reading your positions and settings…
        </p>
      ) : ask.kind === "nothing" ? (
        <p role="status">{ask.note}</p>
      ) : ask.kind === "error" ? (
        <div className="callout callout-bad" role="alert">
          <p className="err">
            <Icon name="alert" small />
            {ask.error}
          </p>
        </div>
      ) : null}
    </Dialog>
  );
}

export function usePreview(path: string, payload: Record<string, unknown>) {
  const [ask, setAsk] = useState<{ kind: "asking" } | Asked>({ kind: "asking" });
  useEffect(() => {
    askPreview(path, payload).then(setAsk);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return ask;
}

// ── stop ────────────────────────────────────────────────────────────────────

export function StopDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const payload = { action: "stop" };
  const ask = usePreview(API.command, payload);
  if (ask.kind !== "review") return <Asking title="Stop the agent completely?" ask={ask} onClose={onClose} />;
  return (
    <ReviewDialog<CommandDone>
      path={API.command}
      payload={payload}
      first={ask.preview}
      title="Stop the agent completely?"
      lead="Use Pause (at the top of the window) for a break from new trades. Stopping is for when you want it off."
      cancel="Keep it running"
      confirm="Stop the agent"
      onClose={onClose}
      onDone={(r) => {
        const reply = (r as unknown as CommandDone).reply;
        page.refresh();
        page.say(true, typeof reply === "string" ? `The agent answered: ${reply}` : "The agent is stopping.");
        onClose();
      }}
    >
      {(p) => (
        <>
          <UnsoldList p={p} after="while it is stopped" />
          <WillWont
            will={["Finish the cycle it is in, then shut down.", "Leave every open position exactly as it is at your broker."]}
            wont={["Answer this dashboard once it has stopped: this page then says it can't reach the agent.", "Start again by itself, unless start at login is on and this computer restarts."]}
          />
        </>
      )}
    </ReviewDialog>
  );
}

// ── start ───────────────────────────────────────────────────────────────────

function started(page: Page, r: StartDone) {
  page.say(
    true,
    `Starting the agent. This page loses the connection for a few seconds while it hands over, then shows the agent's own report.${r.autostart_note ? ` ${r.autostart_note}` : ""}`,
  );
  page.refresh();
}

/** Start in practice: straight away (the server starts it on this POST).
 *  The saved mode is LIVE: the server's preview first. */
export function StartDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const ask = usePreview(API.agentStart, {});
  // practice: the first POST already started it (no preview to show)
  const once = useRef(false);
  useEffect(() => {
    if (ask.kind === "nothing" && !once.current) {
      once.current = true;
      started(page, {});
      onClose();
    }
  }, [ask]); // eslint-disable-line react-hooks/exhaustive-deps
  if (ask.kind !== "review") return <Asking title="Start the agent" ask={ask} onClose={onClose} />;
  return (
    <ReviewDialog<StartDone>
      path={API.agentStart}
      payload={{}}
      first={ask.preview}
      tone="live"
      title="Start the agent in LIVE mode: real money"
      lead="The saved mode is LIVE. From the next signal that passes your limits, the agent places real orders in your broker account."
      cancel="Not now"
      confirm="Start in LIVE mode"
      onClose={onClose}
      onDone={(r) => {
        started(page, r as unknown as StartDone);
        onClose();
      }}
    >
      {(p) => (
        <dl className="facts">
          <dt>Each trade</dt>
          <dd>{sizeWords(p.trade_size?.main ?? null, "main")}</dd>
          <dt>New trades per day</dt>
          <dd>{p.limits?.max_entries_per_day != null ? count(p.limits.max_entries_per_day) : UNKNOWN}</dd>
          <dt>Daily loss limit</dt>
          <dd>{lossWords(p.limits?.max_daily_loss_usd)}</dd>
          <dt>Real positions open now</dt>
          <dd>{p.real_positions != null ? count(p.real_positions) : UNKNOWN}</dd>
        </dl>
      )}
    </ReviewDialog>
  );
}

// ── move to another computer ────────────────────────────────────────────────

type MoveStep =
  | { kind: "form" }
  | { kind: "finish"; preview: Preview }
  | { kind: "cloud"; payload: { token: string; region: string }; preview: Preview }
  | { kind: "cloud-watch"; note: string };

/** The cloud server's progress (the classic poll, every 5 s). */
function CloudWatch({ note, onClose }: { note: string; onClose: () => void }) {
  const [st, setSt] = useState<DeployStatus | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    const poll = async () => {
      const r = await getJson<DeployStatus>(API.deployStatus);
      if (!live) return;
      if (r.ok) {
        setSt(r.body);
        setErr("");
      } else setErr(r.error);
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, []);
  return (
    <Dialog
      title="Your cloud server is being made"
      lead="This computer is retired: it never runs this setup again. The server sends a notification when its agent has started (about 5 minutes)."
      onClose={onClose}
      foot={
        <button className="btn" onClick={onClose} data-autofocus>
          Close
        </button>
      }
    >
      <p className="small">{note}</p>
      <dl className="facts">
        <dt>Server</dt>
        <dd role="status" aria-live="polite">
          {st ? st.status : err || "Asking DigitalOcean…"}
        </dd>
        <dt>Address</dt>
        <dd>{st?.ip || "Not given yet"}</dd>
        {st?.ssh && (
          <>
            <dt>Open its dashboard</dt>
            <dd>
              <code>{st.ssh}</code>
              <small>Run this on your computer, then open http://127.0.0.1:8721/ there.</small>
            </dd>
          </>
        )}
      </dl>
    </Dialog>
  );
}

export function MoveDialog({ page, onClose }: { page: Page; onClose: () => void }) {
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [file, setFile] = useState<MoveFile | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState("");
  const [region, setRegion] = useState(DEPLOY_REGIONS[0][0]);
  const [cloudErr, setCloudErr] = useState("");
  const [step, setStep] = useState<MoveStep>({ kind: "form" });
  const id = useId();
  const running = page.s.agent.running;

  const create = async () => {
    const bad = movePassProblem(pw, pw2);
    if (bad) return setErr(bad);
    setErr("");
    setBusy(true);
    const r = await send<MoveFile>(API.moveExport, { passphrase: pw });
    setBusy(false);
    if (!r.ok) return setErr(r.error);
    const f = r as unknown as MoveFile;
    setFile({ filename: f.filename, kb: f.kb });
    // the browser saves it: the classic download, as it is
    location.href = API.moveDownload;
  };
  const finish = async () => {
    setErr("");
    setBusy(true);
    const a = await askPreview(API.moveFinish, {});
    setBusy(false);
    if (a.kind === "review") setStep({ kind: "finish", preview: a.preview });
    else setErr(a.kind === "error" ? a.error : a.note);
  };
  const cloud = async () => {
    if (!token.trim()) return setCloudErr("Paste a DigitalOcean API token first.");
    setCloudErr("");
    setBusy(true);
    const payload = { token: token.trim(), region };
    const a = await askPreview(API.moveDeploy, payload);
    setBusy(false);
    if (a.kind === "review") setStep({ kind: "cloud", payload, preview: a.preview });
    else setCloudErr(a.kind === "error" ? a.error : a.note);
  };

  if (step.kind === "finish")
    return (
      <ReviewDialog<MoveDone>
        path={API.moveFinish}
        payload={{}}
        first={step.preview}
        title="Retire this computer?"
        lead={`Only after you saved the move file. ${running ? "The agent here stops, s" : "S"}tart at login turns off, and this computer never runs this setup again unless you type python agent.py resume-here.`}
        cancel="Not yet"
        confirm={running ? "Retire it and stop the agent here" : "Retire this computer"}
        onClose={() => setStep({ kind: "form" })}
        onDone={(r) => {
          page.refresh();
          page.say(true, `Done: ${(r as unknown as MoveDone).note || "this computer is retired."}`);
          onClose();
        }}
      >
        {(p) => <UnsoldList p={p} after="here once it is retired, until you start the agent on the new computer" />}
      </ReviewDialog>
    );
  if (step.kind === "cloud")
    return (
      <ReviewDialog<MoveDone>
        path={API.moveDeploy}
        payload={step.payload}
        first={step.preview}
        title="Move to a new cloud server?"
        lead="The agent makes a server in your DigitalOcean account, copies this setup to it, and retires this computer."
        cancel="Not now"
        confirm="Make the server and move"
        onClose={() => setStep({ kind: "form" })}
        onDone={(r) => {
          setToken("");
          page.refresh();
          setStep({ kind: "cloud-watch", note: (r as unknown as MoveDone).note || "" });
        }}
      >
        {(p) => <UnsoldList p={p} after="until the server's agent has started (about 5 minutes)" />}
      </ReviewDialog>
    );
  if (step.kind === "cloud-watch") return <CloudWatch note={step.note} onClose={onClose} />;

  return (
    <Dialog
      title="Move to another computer"
      lead="Packs everything (settings, broker login, signal pass, wallet, open positions, trade history, rules) into one file locked with a passphrase you pick. You open it on the new computer."
      onClose={() => !busy && onClose()}
      foot={
        <button className="btn" onClick={onClose} disabled={busy} data-autofocus>
          Close
        </button>
      }
    >
      <ol className="steps">
        <li>
          <h4>Create your move file</h4>
          <div className="field">
            <label htmlFor={`${id}-p1`}>Passphrase (8 characters or more; you need it on the new computer)</label>
            <input id={`${id}-p1`} className="input" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor={`${id}-p2`}>Type it again</label>
            <input
              id={`${id}-p2`}
              className="input"
              type="password"
              autoComplete="new-password"
              value={pw2}
              onChange={(e) => setPw2(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create()}
            />
          </div>
          <div className="row-acts">
            <button className="btn btn-primary" onClick={create} disabled={busy}>
              {busy && !file ? "Packing…" : "Create the move file"}
            </button>
            {file && (
              <a className="btn" href={API.moveDownload}>
                <Icon name="in" small />
                Download again
              </a>
            )}
          </div>
          {file && (
            <p className="small muted">
              {file.filename} ({count(file.kb)} KB). It holds your broker login and wallet keys: keep it like a password, and delete it after the move.
            </p>
          )}
        </li>
        <li>
          <h4>On the new computer</h4>
          <p className="small">
            Run the same one-line installer. When the setup page opens, press Open my move file, pick the file and type your passphrase. It starts in
            practice there; switch it to LIVE when you are ready. Stop this computer first (step 3): two computers running one setup place every order twice.
          </p>
        </li>
        <li>
          <h4>Retire this computer</h4>
          <p className="small">After you saved the file. {running ? "The agent here stops." : ""}</p>
          <div className="row-acts">
            <button className="btn" onClick={finish} disabled={busy || !file}>
              I saved the file: retire this computer…
            </button>
          </div>
        </li>
      </ol>
      <FieldError id={`${id}-e`} text={err} />
      <section className="subsec">
        <h4>Or move to a new cloud server</h4>
        <p className="small">
          The agent makes a small server in your own DigitalOcean account (billed by DigitalOcean), copies this setup to it and retires this computer.
          The token is used once and never shown again.
        </p>
        <div className="field">
          <label htmlFor={`${id}-t`}>DigitalOcean API token (read and write)</label>
          <input id={`${id}-t`} className="input" type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor={`${id}-r`}>Region</label>
          <select id={`${id}-r`} className="input" value={region} onChange={(e) => setRegion(e.target.value)}>
            {DEPLOY_REGIONS.map(([k, name]) => (
              <option key={k} value={k}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div className="row-acts">
          <button className="btn" onClick={cloud} disabled={busy}>
            Review the cloud move…
          </button>
        </div>
        <FieldError id={`${id}-ce`} text={cloudErr} />
      </section>
      <p className="small muted">
        Bringing a setup to this computer instead? <a href={SETUP_FROM_V2}>Open my move file in setup</a>.
      </p>
    </Dialog>
  );
}
