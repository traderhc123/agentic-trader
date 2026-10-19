import { ArrowRight, Copy, ExternalLink, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, LinkButton } from "../components/ui/button";
import { Disclosure } from "../components/ui/disclosure";
import { Field, Input, Select } from "../components/ui/field";
import { QR } from "../components/ui/qr";
import { Msg, err, info, ok, type MsgT } from "../components/ui/tag";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import { copyToClipboard } from "../lib/hooks";
import { BackBtn, Spacer } from "./steps";
import { Block, Notice, StepCard, useWiz } from "./Wizard";

const PASSES: { days: number; tag?: string }[] = [{ days: 1, tag: "Try it" }, { days: 5 }, { days: 20 }, { days: 60 }];

interface Invoice {
  bolt11: string;
  qr?: number[][] | null;
  quote: string;
  need: number;
}

export function SourceStep() {
  const { st, done, isDone } = useWiz();
  const [src, setSrc] = useState<string>(st?.source || "agenthc");
  const [scope, setScope] = useState("main");
  const [srcUrl, setSrcUrl] = useState("");
  const [lnUrl, setLnUrl] = useState("");
  const [lnKey, setLnKey] = useState("");
  const [hcKey, setHcKey] = useState("");
  const [msg, setMsg] = useState<MsgT>(null);
  const [advOpen, setAdvOpen] = useState(!!st?.wallet?.connected);

  // pass purchase
  const [sizes, setSizes] = useState<number[] | null>(null); // null = server not asked / unknown → show all
  const [selDays, setSelDays] = useState<number | null>(null);
  const [passMsg, setPassMsg] = useState<MsgT>(null);
  const [inv, setInv] = useState<Invoice | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [copied, setCopied] = useState("");
  const passTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const helpTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Show only the pass sizes the feed server sells right now (it can take
  // sizes off sale while its node can't receive that large a payment). If the
  // server can't be asked, every card stays visible. An empty list means
  // every size is paused. Re-checked every 3 minutes.
  useEffect(() => {
    const load = () =>
      api<{ options?: number[] }>("/api/pass/options")
        .then((r) => Array.isArray(r.options) && setSizes(r.options))
        .catch(() => {});
    load();
    const id = setInterval(load, 180000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (st?.pass) checkPass();
    return () => {
      if (passTimer.current) clearInterval(passTimer.current);
      if (helpTimer.current) clearTimeout(helpTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stopPoll = () => {
    if (passTimer.current) clearInterval(passTimer.current);
    passTimer.current = null;
  };

  async function checkPass() {
    const r = await api("/api/pass/status");
    if (r.status === "active") {
      stopPoll();
      setInv(null);
      const n = Number(r.days_left);
      const d = Number.isFinite(n) && r.days_left != null ? `${n} signal day${n === 1 ? "" : "s"}` : "? signal days";
      setPassMsg(ok(`Access active: ${d} left`));
    } else if (r.status === "pending") setPassMsg(info("Waiting for payment…"));
    else if (r.status === "unpaid") {
      stopPoll();
      setInv(null);
      setPassMsg(err("That invoice expired unpaid. Pick a pass again."));
    } else if (r.status === "exits_only")
      setPassMsg(err("Your signal days are used up. Exits still come through for a few days. Buy more above."));
    else if (r.status === "error" && r.error) setPassMsg(err(r.error));
    else if (r.status === "expired" || r.status === "used_up") setPassMsg(err("Your pass has run out. Buy more days above."));
  }

  const buyPass = async (days: number) => {
    setSelDays(days);
    setPassMsg(info("Creating invoice…"));
    const r = await api("/api/pass/buy", { days });
    if (!r.ok) {
      setPassMsg(err(r.error));
      if (r.refresh_sizes) {
        setSelDays(null);
        api<{ options?: number[] }>("/api/pass/options")
          .then((o) => Array.isArray(o.options) && setSizes(o.options))
          .catch(() => {});
      }
      if (r.unavailable) setAdvOpen(true);
      return;
    }
    const carried = r.carried ? ` · plus ${r.carried} unused day${Number(r.carried) === 1 ? "" : "s"} carried over` : "";
    setInv({
      bolt11: r.bolt11,
      qr: r.qr,
      quote: `${days} signal day${days > 1 ? "s" : ""}: ${Number(r.amount_sats).toLocaleString()} sats${r.amount_usd ? ` (~$${r.amount_usd})` : ""}${carried}`,
      need: Math.ceil((r.amount_sats || 0) * 1.01) + 50,
    });
    setCopied("");
    setShowHelp(false);
    if (helpTimer.current) clearTimeout(helpTimer.current);
    helpTimer.current = setTimeout(() => passTimer.current && setShowHelp(true), 120000);
    setPassMsg(info("Waiting for payment…"));
    stopPoll();
    passTimer.current = setInterval(checkPass, 3000);
  };

  const save = async () => {
    const r = await api("/api/source", {
      source: src,
      scope,
      lnbits_url: lnUrl,
      lnbits_key: lnKey,
      agenthc_key: hcKey,
      source_url: srcUrl,
    });
    setMsg(r.ok ? ok(r.note) : err(r.error));
    if (r.ok) done("source");
  };

  const shown = PASSES.filter((p) => sizes == null || sizes.includes(p.days));

  return (
    <StepCard
      id="source"
      title="Signal source"
      restored={isDone("source") ? st?.notes?.source || (st?.source ? "Source saved: " + st.source : undefined) : undefined}
      footer={
        <>
          <span className="text-xs text-muted">Your choice of source, not investment advice.</span>
          <Msg m={msg} />
          <Spacer />
          <BackBtn />
          <Button variant="primary" className="btn-main" onClick={save}>
            Save and continue
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </>
      }
    >
      <Block label="Feed" desc="Where your agent's trade ideas come from.">
        <Field htmlFor="src" label="Source">
          <Select id="src" value={src} onChange={(e) => setSrc(e.target.value)}>
            <option value="agenthc">AgentHC Agentic Day Trade Ideas (journal feed, paid in sats)</option>
            <option value="manual">My own commands (commands.jsonl)</option>
            <option value="url">A JSON feed URL I provide</option>
          </Select>
        </Field>
      </Block>

      {src === "agenthc" && (
        <>
          <Block label="Signals" desc="How many of AgentHC's ideas to follow.">
            <Field
              htmlFor="hc-scope"
              label="Which trade signals?"
              help={'"Other trades" follows AgentHC\'s broader shadow journal. Your daily entry cap and per-trade size still apply to every signal.'}
            >
              <Select id="hc-scope" value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="main">Main pick only: one high-conviction trade a day (default)</option>
                <option value="all">Main pick + "other trades": wider journal, several entries a day</option>
              </Select>
            </Field>
          </Block>

          <Block label="Access" desc="Prepay signal days in sats over Lightning.">
            <p className="m-0 max-w-[72ch] text-sm leading-relaxed text-fg2">
              One Lightning payment from the wallet you already use (Cash App, Strike, Phoenix, Alby…).{" "}
              <b className="text-fg">About $10 per signal day</b>, priced in sats.
            </p>
            <dl className="m-0 grid grid-cols-3 gap-px overflow-hidden rounded border border-line bg-line text-xs max-sm:grid-cols-1">
              {[
                ["Billing", "A day is only used when your agent actually receives a trade signal (an entry or an exit) that day. Quiet days are free."],
                ["Carry-over", "Unused days carry over when you buy more."],
                ["Custody", "The agent never holds money. You pay each invoice from your own wallet."],
              ].map(([k, v]) => (
                <div key={k} className="bg-panel2 px-3 py-2.5">
                  <dt className="mb-1 font-semibold text-fg">{k}</dt>
                  <dd className="m-0 leading-relaxed text-fg2">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="m-0 text-xs text-muted">
              "Main pick only" uses a day only for main-pick signals; adding "other trades" (above) uses days on those too.
            </p>

            <div role="group" aria-label="Signal day passes" className="grid grid-cols-[repeat(auto-fit,minmax(130px,1fr))] gap-2">
              {shown.map((p) => (
                <button
                  key={p.days}
                  type="button"
                  data-days={p.days}
                  onClick={() => buyPass(p.days)}
                  aria-pressed={selDays === p.days}
                  className={cn(
                    "flex min-w-0 flex-col items-start rounded-md border px-3 pb-2.5 pt-3 text-left transition-colors",
                    selDays === p.days
                      ? "border-accent bg-accent/[0.08] shadow-[inset_0_0_0_1px_rgb(var(--accent))]"
                      : "border-line2 bg-panel2 hover:border-accent/50 hover:bg-raise",
                  )}
                >
                  <span className="h-4 text-2xs font-medium text-accent">{p.tag || ""}</span>
                  <span className="mt-1 font-mono text-2xl font-medium leading-none tracking-[-0.02em] text-fg">{p.days}</span>
                  <span className="mt-1 text-xs text-muted">signal day{p.days > 1 ? "s" : ""}</span>
                  <span className="mt-3 w-full border-t border-line pt-2 font-mono text-xs text-fg2">≈ ${p.days * 10}</span>
                </button>
              ))}
            </div>
            {sizes != null && sizes.length === 0 && (
              <p role="status" className="m-0 text-sm text-muted">
                Passes are paused for a little while: our node is refilling so it can receive the payment. This page checks again
                every few minutes.
              </p>
            )}
            {passMsg && (
              <p className="m-0 text-sm">
                <Msg m={passMsg} />
              </p>
            )}

            {inv && (
              <div className="space-y-3">
                <div className="flex gap-5 rounded-md border border-line2 bg-sink p-4 max-sm:flex-col max-sm:items-center">
                  <QR matrix={inv.qr} />
                  <div className="w-full min-w-0 flex-1">
                    <p className="m-0 font-mono text-base font-medium text-fg">{inv.quote}</p>
                    <pre
                      tabIndex={0}
                      aria-label="Lightning invoice"
                      className="scroll-thin mt-3 max-h-16 overflow-auto whitespace-pre-wrap break-all rounded border border-line bg-panel px-2.5 py-2 font-mono text-[11px] leading-normal text-fg2"
                    >
                      {inv.bolt11}
                    </pre>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <Button
                        onClick={async () =>
                          setCopied((await copyToClipboard(inv.bolt11)) ? "Copied" : "Clipboard blocked. Select the invoice text above and copy it.")
                        }
                      >
                        <Copy className="h-3.5 w-3.5" />
                        Copy invoice
                      </Button>
                      <LinkButton href={"lightning:" + inv.bolt11}>
                        <Zap className="h-3.5 w-3.5" />
                        Open in my Lightning wallet app
                      </LinkButton>
                      {copied && <span className="text-xs text-up">{copied}</span>}
                    </div>
                    <p className="m-0 mt-3 text-xs leading-relaxed text-muted">
                      Scan the QR with your phone's wallet, or copy the invoice into any Lightning app. This page updates by itself
                      once you pay (the invoice is good for 1 hour).
                    </p>
                  </div>
                </div>
                {showHelp && (
                  <Notice>
                    <b>Payment not going through?</b> Nothing has arrived yet. Most failures happen inside the paying wallet, before
                    the payment leaves it:
                    <ul className="mb-0 mt-1.5 list-disc space-y-1 pl-5">
                      <li>
                        Your wallet needs <b>{inv.need.toLocaleString()} sats</b> available (the invoice plus a little for network fees).
                      </li>
                      <li>
                        <b>Cash App:</b> add money to your Cash App balance (or buy the bitcoin) first, then pay from that balance.
                        Payments that pull from your bank often fail before they're sent. Cash App also limits Lightning payments:
                        try the <b>1 signal day</b> pass first, or pay from <b>Strike</b> or <b>Phoenix</b>.
                      </li>
                      <li>
                        Still stuck? Use <b>Advanced: auto-pay from a wallet the agent controls</b> below, or open the dashboard's{" "}
                        <b>Help</b> panel and send the support report.
                      </li>
                    </ul>
                  </Notice>
                )}
              </div>
            )}

            <Disclosure
              id="adv-wallet"
              open={advOpen}
              onToggle={setAdvOpen}
              summary="Advanced: auto-pay each day from a Lightning wallet the agent controls, or use an API key"
            >
              <AgentWallet />
              <p className="m-0 text-xs text-muted">Your own LNbits wallet:</p>
              <Field htmlFor="ln-url" label="LNbits instance URL">
                <Input id="ln-url" placeholder="https://…" value={lnUrl} onChange={(e) => setLnUrl(e.target.value)} />
              </Field>
              <Field htmlFor="ln-key" label="LNbits wallet admin key">
                <Input id="ln-key" type="password" placeholder="Admin key" value={lnKey} onChange={(e) => setLnKey(e.target.value)} />
              </Field>
              <div className="flex items-center gap-3 text-xs text-faint before:h-px before:flex-1 before:bg-line after:h-px after:flex-1 after:bg-line">
                or
              </div>
              <Field htmlFor="hc-key" label="AgentHC Premium API key">
                <Input id="hc-key" type="password" placeholder="Premium API key" value={hcKey} onChange={(e) => setHcKey(e.target.value)} />
              </Field>
            </Disclosure>
          </Block>
        </>
      )}

      {src === "url" && (
        <Block>
          <Field htmlFor="src-url-input" label="Feed URL">
            <Input id="src-url-input" placeholder="https://my-feed.example.com/events" value={srcUrl} onChange={(e) => setSrcUrl(e.target.value)} />
          </Field>
        </Block>
      )}
      {src === "manual" && (
        <Block>
          <p className="m-0 text-sm text-muted">
            You'll append JSON lines to <code className="rounded-sm bg-raise px-1 font-mono text-xs text-fg2">~/.agentic-trader/commands.jsonl</code>.
            See the README.
          </p>
        </Block>
      )}
    </StepCard>
  );
}

// ── agent-held wallet (advanced) ────────────────────────────────────────────
function AgentWallet() {
  const { st } = useWiz();
  const [made, setMade] = useState<{ label: string; page?: string } | null>(
    st?.wallet?.connected ? { label: "Wallet connected", page: st.wallet.page } : null,
  );
  const [mwMsg, setMwMsg] = useState<MsgT>(null);
  const [warn, setWarn] = useState("");
  const [sats, setSats] = useState("16000");
  const satsEdited = useRef(false);
  const [quote, setQuote] = useState("");
  const [note, setNote] = useState("");
  const [inv, setInv] = useState<{ bolt11: string; qr?: number[][] | null } | null>(null);
  const [bal, setBal] = useState<number | null>(null);
  const [copied, setCopied] = useState("");
  const [recurring, setRecurring] = useState(!!st?.recurring);
  const balTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const quoteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (balTimer.current) clearInterval(balTimer.current);
      if (quoteTimer.current) clearTimeout(quoteTimer.current);
    },
    [],
  );

  const updateQuote = async (s: string) => {
    const n = parseInt(s || "0", 10);
    if (!n) return setQuote("");
    try {
      const r = await api("/api/wallet/quote?sats=" + n);
      if (!r.ok) return setQuote("");
      let t = `${n.toLocaleString()} sats ≈ $${r.usd.toFixed(2)} (BTC $${Math.round(r.btc_usd).toLocaleString()}) · covers ~${r.day_passes} day-pass${r.day_passes === 1 ? "" : "es"} at ~$10/day`;
      if (r.day_pass_sats) t += `. One day pass ≈ ${r.day_pass_sats.toLocaleString()} sats`;
      if (r.invoice_cap) t += ` · instance max ${r.invoice_cap.toLocaleString()} sats per invoice`;
      setQuote(t);
    } catch {
      setQuote("");
    }
  };

  // Price-aware default: ONE ~$10 day pass at the CURRENT BTC price (clamped
  // to the instance's per-invoice cap) — unless the user already typed.
  useEffect(() => {
    if (!made) return;
    api("/api/wallet/quote?sats=0")
      .then((r) => {
        let s = sats;
        if (r && r.ok && r.suggested_sats && !satsEdited.current) {
          s = String(r.suggested_sats);
          setSats(s);
        }
        updateQuote(s);
      })
      .catch(() => {});
    api("/api/wallet/balance")
      .then((b) => b && b.ok && setBal(b.sats))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!!made]);

  const create = async () => {
    setMwMsg(info("Creating…"));
    const r = await api("/api/wallet/create", {});
    if (r.ok) {
      setMwMsg(null);
      setMade({ label: "Created", page: r.page });
      if (r.warning) setWarn(r.warning);
    } else setMwMsg(err(r.error));
  };

  const fund = async () => {
    const r = await api("/api/wallet/fund", { sats });
    if (!r.ok) return setMwMsg(err(r.error));
    setInv({ bolt11: r.bolt11, qr: r.qr });
    setNote(r.note || "");
    setCopied("");
    if (balTimer.current) clearInterval(balTimer.current);
    balTimer.current = setInterval(async () => {
      const b = await api("/api/wallet/balance");
      if (b.ok) setBal(b.sats);
    }, 4000);
  };

  const toggleRecurring = async () => {
    const r = await api("/api/wallet/recurring", { on: !recurring });
    if (r.ok) setRecurring(!!r.on);
  };

  return (
    <div className="space-y-3">
      <p className="m-0 text-sm text-muted">
        The agent pays about $10 each market day from a wallet it holds the keys to. Only for people who want hands-off daily payments.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={create}>Create my agent's wallet for me</Button>
        {made && (
          <span className="text-xs text-up">
            {made.label}
            {made.page && (
              <a href={made.page} target="_blank" rel="noopener" className="ml-2 inline-flex items-center gap-1 text-accent">
                Open wallet page <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </span>
        )}
        <Msg m={mwMsg} />
      </div>
      {warn && <p className="m-0 text-sm text-down">⚠ {warn}</p>}

      {made && (
        <div className="space-y-3 rounded border border-line bg-panel2 p-4">
          <p className="m-0 text-sm text-fg2">
            Wallet ready. Now give your agent some sats: pay this invoice from any Lightning app (Strike, Cash App, Phoenix, Alby…).
          </p>
          <Field htmlFor="fund-sats" label="Amount in sats (a day pass is about $10 of bitcoin)">
            <div className="flex gap-2">
              <Input
                id="fund-sats"
                type="number"
                min={1000}
                value={sats}
                className="font-mono"
                onChange={(e) => {
                  satsEdited.current = true;
                  setSats(e.target.value);
                  if (quoteTimer.current) clearTimeout(quoteTimer.current);
                  const v = e.target.value;
                  quoteTimer.current = setTimeout(() => updateQuote(v), 400);
                }}
              />
              <Button onClick={fund} className="h-9">
                Show invoice
              </Button>
            </div>
          </Field>
          {quote && <p className="m-0 font-mono text-xs text-muted">{quote}</p>}
          {note && <p className="m-0 text-sm text-up">{note}</p>}
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={toggleRecurring} variant={recurring ? "primary" : "default"} aria-pressed={recurring}>
              Recurring day-pass: {recurring ? "On" : "Off"}
            </Button>
            <span className="min-w-[220px] flex-1 text-xs text-muted">
              On = the agent buys each day's ~$10 pass from its wallet automatically (never above your auto-pay cap). Off = it never
              spends without you.
            </span>
          </div>
          {inv && (
            <div className="flex gap-4 max-sm:flex-col max-sm:items-center">
              <QR matrix={inv.qr} />
              <div className="w-full min-w-0 flex-1 space-y-2">
                <pre
                  tabIndex={0}
                  aria-label="Lightning invoice"
                  className="scroll-thin m-0 max-h-16 overflow-auto whitespace-pre-wrap break-all rounded border border-line bg-panel px-2.5 py-2 font-mono text-[11px] text-fg2"
                >
                  {inv.bolt11}
                </pre>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    onClick={async () =>
                      setCopied((await copyToClipboard(inv.bolt11)) ? "Copied" : "Clipboard blocked. Select the invoice text above and copy it.")
                    }
                  >
                    <Copy className="h-3.5 w-3.5" />
                    Copy invoice
                  </Button>
                  <LinkButton href={"lightning:" + inv.bolt11}>
                    <Zap className="h-3.5 w-3.5" />
                    Open in my Lightning wallet app
                  </LinkButton>
                  {copied && <span className="text-xs text-up">{copied}</span>}
                </div>
                <p className="m-0 text-xs text-muted">Updates automatically after you pay.</p>
              </div>
            </div>
          )}
          {bal != null && (
            <p className="m-0 text-sm text-fg2">
              Balance: <span className="font-mono text-fg">{bal.toLocaleString()}</span> sats
            </p>
          )}
          <Notice>
            Hosted wallet on demo.lnbits.com: a public <b>test</b> server (custodial; it can be reset). Keep only a few days of passes
            there, or use your own instance below.
          </Notice>
        </div>
      )}
    </div>
  );
}
