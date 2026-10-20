import { useState } from "react";
import { Button } from "../components/ui/button";
import { Field, Input } from "../components/ui/field";
import { Msg, err, info, ok, type MsgT } from "../components/ui/tag";
import { api } from "../lib/api";

function readAsBase64(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(f);
  });
}

// New computer: unpack a move file made with the dashboard's
// "Move computer" button. Every setup step is filled in from it.
export function MoveImport({ running = false }: { running?: boolean }) {
  const [file, setFile] = useState<File | null>(null);
  const [pw, setPw] = useState("");
  const [msg, setMsg] = useState<MsgT>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const run = async (replace = false) => {
    if (!file) return setMsg(err("Pick your move file first."));
    if (!pw) return setMsg(err("Type the passphrase you chose."));
    setBusy(true);
    setMsg(info("Unpacking…"));
    try {
      const data = await readAsBase64(file);
      const r = await api<{ ok: boolean; error?: string; needs_replace?: boolean; was_live?: boolean; restarting?: boolean }>("/api/move/import", {
        data,
        passphrase: pw,
        replace,
      });
      if (r.needs_replace) {
        setBusy(false);
        if (window.confirm("This computer already has a setup. Replace it with the one from your move file?")) return run(true);
        return setMsg(err("Nothing changed."));
      }
      if (!r.ok) return setMsg(err(r.error || "Import failed."));
      setMsg(
        ok(
          "Your setup is here. It starts in dry-run: once the old computer is off, say 'dry off' in the dashboard" +
            (r.was_live ? " to go live again." : " when you're ready.") +
            (r.restarting ? " Your agent is restarting with it…" : " Opening the last step…"),
        ),
      );
      setTimeout(() => window.location.reload(), r.restarting ? 8000 : 3000);
    } catch {
      setMsg(err("Couldn't read that file."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-labelledby="mv-title"
      className="rounded-md border border-line2 border-l-2 border-l-accent bg-panel px-5 py-4"
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h2 id="mv-title" className="m-0 text-base font-semibold">
            Moving from another computer?
          </h2>
          <p className="m-0 mt-1 text-sm text-fg2">
            Open the move file you made with <b className="text-fg">Move computer</b> on the old computer's dashboard. Every step
            below fills in, including your broker login and prepaid pass.
            {running && " Your agent here pauses, takes the moved setup, and restarts with it."}
          </p>
        </div>
        {!open && (
          <Button variant="primary" onClick={() => setOpen(true)}>
            Open my move file
          </Button>
        )}
      </div>
      {open && (
        <div className="mt-4 space-y-3">
          <Field htmlFor="mv-file" label="Move file (.atmove)">
            <input
              id="mv-file"
              type="file"
              accept=".atmove,application/octet-stream"
              onChange={(e) => setFile(e.target.files?.[0] || null)}
              className="block text-sm text-fg2"
            />
          </Field>
          <Field htmlFor="mv-pass" label="Passphrase">
            <Input id="mv-pass" type="password" autoComplete="off" value={pw} onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
          </Field>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={() => run()} disabled={busy}>
              Open move file
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Msg m={msg} />
          </div>
        </div>
      )}
    </section>
  );
}
