import { useState } from "react";
import { Button } from "../components/ui/button";
import { ConfirmDialog } from "../components/ui/dialog";
import { Field, Input } from "../components/ui/field";
import { Panel } from "../components/ui/panel";
import { Msg, err, info, ok, type MsgT } from "../components/ui/tag";
import { api } from "../lib/api";

// "Move to another computer": pack this setup into one passphrase-locked file,
// then retire this computer so two machines never trade the same account.
export function MovePanel({ onClose }: { onClose: () => void }) {
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [file, setFile] = useState<{ name: string; kb: number } | null>(null);
  const [msg, setMsg] = useState<MsgT>(null);
  const [doneMsg, setDoneMsg] = useState<MsgT>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  const create = async () => {
    if (pw.length < 8) return setMsg(err("Pick a passphrase of at least 8 characters."));
    if (pw !== pw2) return setMsg(err("The two passphrases don't match."));
    setBusy(true);
    setMsg(info("Packing your setup…"));
    try {
      const r = await api<{ ok: boolean; error?: string; filename: string; kb: number }>("/api/move/export", { passphrase: pw });
      if (!r.ok) return setMsg(err(r.error || "Couldn't create the file."));
      setFile({ name: r.filename, kb: r.kb });
      setMsg(ok("Downloading your move file…"));
      window.location.href = "/api/move/download";
    } catch {
      setMsg(err("Could not reach the agent on this computer."));
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    const r = await api<{ ok: boolean; note?: string; error?: string }>("/api/move/finish", {});
    setDoneMsg(r.ok ? ok("Done: " + (r.note || "this computer is retired.")) : err(r.error || "Couldn't finish."));
  };

  return (
    <Panel
      id="move"
      title="Move to another computer"
      right={
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      }
      bodyClass="p-4 space-y-4"
    >
      <p className="m-0 text-sm text-fg2">
        Packs everything (settings, broker login, prepaid pass, wallet, open positions, trade history, rules) into one file
        locked with a passphrase you pick. You open it on the new computer.
      </p>

      <ol className="m-0 list-decimal space-y-4 pl-5 text-sm text-fg2">
        <li className="space-y-2">
          <b className="text-fg">Create your move file</b>
          <Field htmlFor="mv-pw" label="Passphrase (8+ characters; you'll need it on the new computer)">
            <Input id="mv-pw" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} />
          </Field>
          <Field htmlFor="mv-pw2" label="Type it again">
            <Input
              id="mv-pw2"
              type="password"
              autoComplete="new-password"
              value={pw2}
              onChange={(e) => setPw2(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create()}
            />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={create} disabled={busy}>
              Create move file
            </Button>
            {file && (
              <a href="/api/move/download" className="text-accent">
                Download again
              </a>
            )}
            <Msg m={msg} />
          </div>
          {file && (
            <p className="m-0 text-xs text-muted">
              {file.name} ({file.kb} KB). It contains your broker login and wallet keys: keep it like a password and delete it
              after the move.
            </p>
          )}
        </li>
        <li className="space-y-1">
          <b className="text-fg">On the new computer</b>
          <p className="m-0">
            Run the same one-line installer. When the setup page opens, press <b className="text-fg">Open my move file</b> in the "Moving from another computer?" box at the top,
            pick the file and type your passphrase. It starts in dry-run there; say <code className="font-mono">dry off</code> when
            you're ready.
          </p>
        </li>
        <li className="space-y-2">
          <b className="text-fg">Stop the agent here</b>
          <p className="m-0">
            Two computers running the same setup would place every order twice. After you've saved the file, stop this one.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="warn" onClick={() => setConfirm(true)} disabled={!file}>
              I've saved the file — stop the agent here
            </Button>
            <Msg m={doneMsg} />
          </div>
        </li>
      </ol>

      <p className="m-0 border-t border-line pt-3 text-sm text-fg2">
        Bringing a setup <b className="text-fg">to</b> this computer instead?{" "}
        <a href="/setup" className="text-accent">
          Open Settings
        </a>{" "}
        and press <b className="text-fg">Open my move file</b> at the top.
      </p>

      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Stop the agent on this computer?"
        confirmLabel="Stop it here"
        onConfirm={finish}
      >
        The agent here stops, start-at-login turns off, and this computer won't run it again unless you type{" "}
        <code className="font-mono">python agent.py resume-here</code>. Open positions stay in your broker account and the new
        computer takes over their exits.
      </ConfirmDialog>
    </Panel>
  );
}
