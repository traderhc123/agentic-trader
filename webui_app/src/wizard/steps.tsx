import { ArrowLeft, ArrowRight, ExternalLink } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "../components/ui/button";
import { Disclosure } from "../components/ui/disclosure";
import { ChoiceRow, Field, Input, Select, Textarea } from "../components/ui/field";
import { Msg, err, info, ok, type MsgT } from "../components/ui/tag";
import { api } from "../lib/api";
import { mdLite } from "../lib/md";
import { Block, Notice, StepCard, useWiz } from "./Wizard";

export function BackBtn() {
  const { goBack } = useWiz();
  return (
    <Button onClick={goBack}>
      <ArrowLeft className="h-3.5 w-3.5" />
      Back
    </Button>
  );
}
export const Spacer = () => <span className="flex-1 max-sm:hidden" />;

// ── 1. agreement ────────────────────────────────────────────────────────────
export function ConsentStep() {
  const { st, done, isDone } = useWiz();
  const [read, setRead] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [msg, setMsg] = useState<MsgT>(null);
  const [bad, setBad] = useState<"read" | "phrase" | null>(null);
  const readRef = useRef<HTMLInputElement>(null);
  const phraseRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    if (!read) {
      setBad("read");
      setMsg(err("Check the box after reading."));
      readRef.current?.focus();
      return;
    }
    const r = await api("/api/consent", { phrase });
    setBad(r.ok ? null : "phrase");
    setMsg(r.ok ? ok("Accepted") : err(r.error));
    if (!r.ok) phraseRef.current?.focus();
    else done("consent");
  };

  return (
    <StepCard
      id="consent"
      title="Agreement"
      restored={isDone("consent") ? st?.notes?.consent || "Agreement signed, on file" : undefined}
      footer={
        <>
          <Msg m={msg} />
          <Spacer />
          <Button variant="primary" className="btn-main" onClick={submit}>
            Accept and continue
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </>
      }
    >
      <Block>
        <div
          tabIndex={0}
          role="region"
          aria-label="User agreement and disclaimer"
          className="prose-terms scroll-thin max-h-[360px] overflow-auto rounded border border-line2 bg-sink px-4 py-3 text-sm leading-relaxed text-fg2"
          dangerouslySetInnerHTML={{ __html: mdLite(st?.disclaimer || "") }}
        />
        <label className="flex cursor-pointer items-start gap-3 rounded border border-line2 bg-sink px-3 py-2.5 text-sm leading-relaxed text-fg2 has-[:checked]:border-accent/50">
          <input
            ref={readRef}
            type="checkbox"
            checked={read}
            onChange={(e) => setRead(e.target.checked)}
            aria-invalid={bad === "read" || undefined}
            className="mt-[3px] h-4 w-4 flex-none"
          />
          <span>
            I have read the entire agreement above and I accept all of its terms, including that{" "}
            <b className="text-fg">all liability is mine</b> and that no party here is a registered investment adviser.
          </span>
        </label>
        <Field
          htmlFor="c-phrase"
          label={
            <>
              Type exactly <b className="font-mono text-fg">I AGREE AND ACCEPT ALL LIABILITY</b> to accept:
            </>
          }
        >
          <Input
            ref={phraseRef}
            id="c-phrase"
            value={phrase}
            onChange={(e) => setPhrase(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            autoComplete="off"
            spellCheck={false}
            autoCapitalize="characters"
            autoCorrect="off"
            aria-invalid={bad === "phrase" || undefined}
            className="font-mono tracking-[0.04em]"
          />
        </Field>
      </Block>
    </StepCard>
  );
}

// ── 2. AI ───────────────────────────────────────────────────────────────────
interface Provider {
  name: string;
  base_url: string;
  keys_url?: string;
}
export function LlmStep() {
  const { st, done, isDone, goNext, reloadState } = useWiz();
  const [policy, setPolicy] = useState("");
  const [providers, setProviders] = useState<Record<string, Provider>>({});
  const [key, setKey] = useState("");
  const [provider, setProvider] = useState("");
  const [base, setBase] = useState("");
  const [advOpen, setAdvOpen] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [model, setModel] = useState("");
  const [findMsg, setFindMsg] = useState<MsgT>(null);
  const [msg, setMsg] = useState<MsgT>(null);
  const busy = useRef(false);
  const restored = isDone("llm") && st?.llm;

  useEffect(() => {
    api<{ providers: Record<string, Provider> }>("/api/llm/providers")
      .then((r) => setProviders(r.providers || {}))
      .catch(() => {});
  }, []);

  const keysUrl = (provider && providers[provider]?.keys_url) || "https://console.anthropic.com/settings/keys";

  const find = async (k = key) => {
    if (busy.current) return;
    busy.current = true;
    setFindMsg(info("Checking key…"));
    try {
      const r = await api("/api/llm/models", { key: k, provider, base_url: base });
      if (!r.ok) {
        setFindMsg(err(r.error));
        if (r.need_provider) setAdvOpen(true);
        return;
      }
      setProvider(r.provider);
      setFindMsg(ok(`${providers[r.provider]?.name || r.provider}: ${r.models.length} models`));
      setModels(r.models);
      setModel(r.suggested || "");
    } finally {
      busy.current = false;
    }
  };

  const connect = async () => {
    if (busy.current) return;
    busy.current = true;
    setMsg(info("Testing the model…"));
    try {
      const r = await api("/api/llm", { key, provider, base_url: base, model });
      setMsg(r.ok ? ok(`Connected. ${r.describe || ""}`) : err(r.error));
      if (r.ok) {
        setKey("");
        setModels(null);
        reloadState(); // stay here: plain-English rules unlock below
      }
    } finally {
      busy.current = false;
    }
  };

  const connected = !!st?.llm;
  const finish = async () => {
    if (policy.trim()) {
      const r = await api("/api/policy", { policy });
      if (!r.ok) return setMsg(err(r.error));
    }
    if (isDone("llm")) goNext();
    else done("llm");
  };

  return (
    <StepCard
      id="llm"
      title="Optional: connect an AI"
      restored={restored ? st?.notes?.llm || "AI connected" : undefined}
      footer={
        <>
          <Msg m={msg} />
          <Spacer />
          <BackBtn />
          {models && (
            <Button onClick={connect}>Connect this model</Button>
          )}
          <Button variant="primary" className="btn-main" onClick={finish}>
            {connected || policy.trim() ? "Continue" : "Skip, I don't need AI"}
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </>
      }
    >
      <Block>
        <p className="m-0 max-w-[72ch] text-sm leading-relaxed text-fg2">
          <b className="text-fg">Not needed to trade.</b> Your agent already trades with the limits and rules you set in Safety.
          An AI adds a help chat, questions about your agent on the dashboard, and plain-English rules. Any model works:
          Claude, ChatGPT/OpenAI, Grok, Gemini, OpenRouter, DeepSeek, Mistral, Groq, or a model running on this computer. It
          never places trades.
        </p>
      </Block>
      <Block label="API key" desc="Claude, OpenAI, Grok, Gemini, OpenRouter or a local model.">
        <Field
          htmlFor="llm-key"
          label="1. Paste an API key"
          help={
            <>
              The provider is detected from the key. Need one?{" "}
              <a href={keysUrl} target="_blank" rel="noopener" className="inline-flex items-center gap-1 text-accent">
                Get a key <ExternalLink className="h-3 w-3" />
              </a>
              . <b className="text-fg2">Claude Max or Team plan?</b> It includes monthly API credits: link them to a Claude Console
              organization (claude.ai → your plan's API credits), then create a key there. Signing in with a chat account
              (claude.ai, chatgpt.com) doesn't connect here; the API key does. Keys stay on this computer.
            </>
          }
        >
          <Input
            id="llm-key"
            type="password"
            value={key}
            autoComplete="off"
            placeholder={
              st?.llm ? "saved key on file — leave blank and press Find models to change model" : "sk-ant-…, sk-…, xai-…, AIza…, sk-or-…"
            }
            onChange={(e) => {
              setKey(e.target.value);
              // A pasted key arrives whole — look up its models without another click.
              if (e.target.value.trim().length >= 30) find(e.target.value);
            }}
            onKeyDown={(e) => e.key === "Enter" && find()}
          />
        </Field>
        <Disclosure summary="Provider, local model, or custom endpoint" open={advOpen} onToggle={setAdvOpen}>
          <Field htmlFor="llm-provider" label="Provider">
            <Select
              id="llm-provider"
              value={provider}
              onChange={(e) => {
                const id = e.target.value;
                setProvider(id);
                const p = providers[id];
                if (p) setBase(id === "custom" || id === "ollama" ? p.base_url : "");
              }}
            >
              <option value="">Detect from key</option>
              {Object.entries(providers).map(([id, p]) => (
                <option key={id} value={id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field htmlFor="llm-base" label="Endpoint URL (custom or local only)">
            <Input id="llm-base" value={base} onChange={(e) => setBase(e.target.value)} placeholder="http://localhost:11434/v1" />
          </Field>
          <p className="m-0 text-xs text-muted">
            Local models (Ollama, LM Studio) need no key. Pick "Local model", check the URL, then press Find models.
          </p>
        </Disclosure>
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary" onClick={() => find()}>
            Find models
          </Button>
          <Msg m={findMsg} />
        </div>
      </Block>
      {models && (
        <Block label="Model" desc="Pick one the key can use.">
          <Field
            htmlFor="llm-model"
            label="2. Pick a model"
            help="Bigger models follow your trading policy more reliably. Any error or unclear answer from the model means the trade is skipped."
          >
            <Input id="llm-model" list="llm-models" value={model} onChange={(e) => setModel(e.target.value)} autoComplete="off" placeholder="Start typing to filter" />
            <datalist id="llm-models">
              {models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </Field>
        </Block>
      )}
      <Block
        label="Plain-English rules"
        desc={
          connected
            ? "Optional, veto-only: your AI checks these before every entry. Anything it can't decide is skipped."
            : "Connect an AI above to use these. (Your built-in rules in Safety work without one.)"
        }
      >
        {st?.policy_on && <p className="m-0 text-sm text-fg2">You already have plain-English rules saved. Typing here replaces them.</p>}
        <Field htmlFor="pol-text" label="My trading policy" help="It can only say no to a trade, never start one.">
          <Textarea
            id="pol-text"
            rows={4}
            disabled={!connected}
            value={policy}
            onChange={(e) => setPolicy(e.target.value)}
            placeholder={"- Skip entries in the first 15 minutes after the open.\n- Skip earnings-week trades on single stocks."}
            className="font-mono text-sm"
          />
        </Field>
      </Block>
    </StepCard>
  );
}

// ── 4. broker ───────────────────────────────────────────────────────────────
interface BrokerDef {
  id: string;
  name: string;
  fields: { id: string; label: string; type?: string; default?: boolean }[];
}
export function BrokerStep() {
  const { st, done, isDone, goNext, canNext, reloadState } = useWiz();
  const [brokers, setBrokers] = useState<BrokerDef[]>([]);
  const [sel, setSel] = useState("");
  const [vals, setVals] = useState<Record<string, string | boolean>>({});
  const [rhMsg, setRhMsg] = useState<MsgT>(null);
  const [rhWarn, setRhWarn] = useState("");
  const [msg, setMsg] = useState<MsgT>(null);
  const [status, setStatus] = useState("");
  const polling = useRef(false);

  useEffect(() => {
    api<{ brokers: BrokerDef[] }>("/api/brokers")
      .then((r) => setBrokers(r.brokers || []))
      .catch(() => {});
    return () => {
      polling.current = false;
    };
  }, []);

  const b = brokers.find((x) => x.id === sel);
  const pick = (id: string) => {
    setSel(id);
    const d = brokers.find((x) => x.id === id);
    const v: Record<string, string | boolean> = {};
    d?.fields.forEach((f) => (v[f.id] = f.type === "checkbox" ? !!f.default : ""));
    setVals(v);
    setMsg(null);
  };

  const rhStart = async () => {
    const r = await api("/api/rh/start");
    if (!r.url) return setRhMsg(err(r.error));
    setRhMsg(info("Waiting for approval…"));
    window.open(r.url, "_blank");
    polling.current = true;
    const poll = async () => {
      if (!polling.current) return;
      const s = await reloadState();
      if (s?.broker) {
        setRhMsg(null);
        setStatus("Connected: Robinhood account ····" + s.broker);
        if (s.rh_warning) setRhWarn(s.rh_warning);
        done("broker");
      } else setTimeout(poll, 1500);
    };
    poll();
  };

  const connect = async () => {
    if (!b) return;
    const r = await api("/api/broker/connect", { broker: b.id, values: vals });
    setMsg(r.ok ? ok(r.note) : err(r.error));
    if (r.ok) {
      setStatus(`Connected: ${b.name || b.id}`);
      done("broker");
    }
  };

  // numeric = Robinhood account last4; otherwise a key-based broker id
  const restoredTxt =
    isDone("broker") && st?.broker ? "Broker connected: " + (/^\d+$/.test(st.broker) ? "Robinhood account ····" + st.broker : st.broker) : undefined;

  return (
    <StepCard
      id="broker"
      title="Broker: where orders execute"
      restored={restoredTxt}
      footer={
        <>
          <span className="text-xs text-muted">Orders execute at the broker you connect here.</span>
          <Spacer />
          <BackBtn />
          <Button variant="primary" className="btn-main" disabled={!canNext} onClick={goNext}>
            Continue
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </>
      }
    >
      <Block>
        <p className="m-0 max-w-[72ch] text-sm leading-relaxed text-fg2">
          Pick your platform. Robinhood is one-click sign-in; key-based platforms take API keys. Many offer a paper (simulated)
          mode, the safest way to try the agent.
        </p>
        {status && (
          <p role="status" className="m-0 text-sm text-up">
            {status}
          </p>
        )}
      </Block>
      <Block label="Robinhood" desc="One-click sign-in in a new tab.">
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={rhStart}>Connect Robinhood</Button>
          <Msg m={rhMsg} />
        </div>
        {rhWarn && <p className="m-0 text-sm text-down">{rhWarn}</p>}
      </Block>
      <Block label="Key-based platform" desc="Many offer a paper (simulated) mode.">
        <Select aria-label="Key-based platform" value={sel} onChange={(e) => pick(e.target.value)}>
          <option value="">Choose a key-based platform</option>
          {brokers.map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </Select>
        {b && (
          <>
            {b.fields.map((f) =>
              f.type === "checkbox" ? (
                <ChoiceRow key={f.id} type="checkbox" checked={!!vals[f.id]} onChange={(c) => setVals((v) => ({ ...v, [f.id]: c }))}>
                  {f.label}
                </ChoiceRow>
              ) : (
                <Field key={f.id} htmlFor={"bf-" + f.id} label={f.label}>
                  <Input
                    id={"bf-" + f.id}
                    type={f.type === "password" ? "password" : "text"}
                    placeholder={f.label}
                    autoComplete="off"
                    value={String(vals[f.id] ?? "")}
                    onChange={(e) => setVals((v) => ({ ...v, [f.id]: e.target.value }))}
                  />
                </Field>
              ),
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button variant="primary" onClick={connect}>
                Connect
              </Button>
              <Msg m={msg} />
            </div>
          </>
        )}
      </Block>
      <Block>
        <Disclosure summary="Don't see your platform? Your agent can build the connector">
          <p className="m-0 text-sm leading-relaxed text-muted">
            Every platform needs a small adapter (different APIs). Your agent can write one for any platform with a trading API.
            After setup, in the dashboard console: <Code>self-edit on</Code> then{" "}
            <Code>code: add a broker adapter for &lt;platform&gt; using their API</Code>. You review the diff before it applies; it then
            appears here automatically. moomoo, Interactive Brokers, Tastytrade, Tradier and others all work this way.
          </p>
        </Disclosure>
      </Block>
    </StepCard>
  );
}
const Code = ({ children }: { children: React.ReactNode }) => (
  <code className="rounded-sm bg-raise px-1 font-mono text-xs text-fg2">{children}</code>
);

// ── 5. sizing ───────────────────────────────────────────────────────────────
const SZ_UNITS: Record<string, [string, string]> = {
  budget: ["Dollars per trade", "e.g. 500"],
  contracts: ["Contracts per trade", "e.g. 1"],
  percent: ["Percent of account per trade", "e.g. 2"],
};
export function SizingStep() {
  const { st, done, isDone, reloadState } = useWiz();
  // Pre-fill from the saved config so re-saving never silently clears a
  // book's size. Nothing on file = no main mode picked rather than claim a
  // choice never made.
  const sd = st?.sizing_detail;
  const [mode, setMode] = useState<string>(sd?.main && SZ_UNITS[sd.main.mode] ? sd.main.mode : st?.sizing ? "" : "budget");
  const [val, setVal] = useState(sd?.main ? String(sd.main.value ?? "") : "");
  const [omode, setOmode] = useState<string>(sd?.other && SZ_UNITS[sd.other.mode] ? sd.other.mode : "same");
  const [oval, setOval] = useState(sd?.other ? String(sd.other.value ?? "") : "");
  const [msg, setMsg] = useState<MsgT>(null);
  const [scopeMsg, setScopeMsg] = useState<MsgT>(null);
  // Shadow sizing only matters when the AgentHC source asks for "other trades".
  const mainOnly = (st?.source || "agenthc") === "agenthc" && !st?.include_other_trades;
  const includeOther = async () => {
    const r = await api("/api/source/scope", { scope: "all" });
    setScopeMsg(r.ok ? ok("Signal source now includes other trades") : err(r.error));
    if (r.ok) reloadState();
  };

  const save = async () => {
    if (!mode) return setMsg(err("Choose how the main book is sized."));
    const r = await api("/api/sizing", { main: { mode, value: val }, other: { mode: omode, value: oval } });
    setMsg(r.ok ? ok("Saved") : err(r.error));
    if (r.ok) done("sizing");
  };

  const modes: [string, string][] = [
    ["budget", "Fixed dollar amount per trade (buys what fits; skips trades that exceed it)"],
    ["contracts", "Fixed number of contracts per trade"],
    ["percent", "Percent of account per trade (re-read from your broker at each entry; skips trades that exceed it)"],
  ];

  return (
    <StepCard
      id="sizing"
      title="Position sizing"
      restored={isDone("sizing") ? st?.notes?.sizing || "Sizing saved" : undefined}
      footer={
        <>
          <span className="text-xs text-muted">Your choice, not advice.</span>
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
      <Block>
        <Notice>
          Nobody here is an investment adviser and none of this software can advise position sizing. This number is{" "}
          <b>your decision</b>, made with money you can afford to lose entirely.
        </Notice>
      </Block>
      <Block label="Main book" desc="The daily main pick.">
        <div role="radiogroup" aria-label="Main book sizing" className="flex flex-col gap-2">
          {modes.map(([v, l]) => (
            <ChoiceRow key={v} type="radio" name="szmode" value={v} checked={mode === v} onChange={() => setMode(v)}>
              {l}
            </ChoiceRow>
          ))}
        </div>
        {mode && (
          <Field htmlFor="sz-val" label={SZ_UNITS[mode][0]} className="max-w-xs">
            <Input
              id="sz-val"
              type="number"
              min={0}
              step={mode === "contracts" ? 1 : "any"}
              max={mode === "percent" ? 100 : undefined}
              placeholder={SZ_UNITS[mode][1]}
              value={val}
              onChange={(e) => setVal(e.target.value)}
              className="font-mono"
            />
          </Field>
        )}
      </Block>
      <Block label="Shadow book" desc={'"Other trades". Only used if your source includes them.'}>
        {mainOnly && omode !== "same" && (
          <div className="mb-3">
            <Notice role="alert">
              <b>Your signal source is set to main pick only</b>, so this shadow-book size is never used: the agent
              doesn't receive any "other trades" signals.
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <Button size="sm" onClick={includeOther}>
                  Include other trades
                </Button>
                <Msg m={scopeMsg} />
              </div>
            </Notice>
          </div>
        )}
        <div role="radiogroup" aria-label="Shadow book sizing" className="flex flex-col gap-2">
          <ChoiceRow type="radio" name="szomode" value="same" checked={omode === "same"} onChange={() => setOmode("same")}>
            Same as the main book
          </ChoiceRow>
          {modes.map(([v, l]) => (
            <ChoiceRow key={v} type="radio" name="szomode" value={v} checked={omode === v} onChange={() => setOmode(v)}>
              {l.split(" (")[0]}
            </ChoiceRow>
          ))}
        </div>
        {omode !== "same" && (
          <Field htmlFor="sz-other-val" label={SZ_UNITS[omode][0]} className="max-w-xs">
            <Input
              id="sz-other-val"
              type="number"
              min={0}
              step={omode === "contracts" ? 1 : "any"}
              max={omode === "percent" ? 100 : undefined}
              placeholder={omode === "budget" ? "e.g. 100" : SZ_UNITS[omode][1]}
              value={oval}
              onChange={(e) => setOval(e.target.value)}
              className="font-mono"
            />
          </Field>
        )}
      </Block>
    </StepCard>
  );
}

// ── 6. safety ───────────────────────────────────────────────────────────────
export function SafetyStep() {
  const { st, done, isDone, setNext } = useWiz();
  const [dry, setDry] = useState(true);
  const [cap, setCap] = useState("5");
  const [maxLoss, setMaxLoss] = useState("");
  const [discord, setDiscord] = useState("");
  const [ntfy, setNtfy] = useState("");
  const [tgToken, setTgToken] = useState("");
  const [tgChat, setTgChat] = useState("");
  const r0 = st?.rules || {};
  const [tickers, setTickers] = useState((r0.rule_tickers || []).join(", "));
  const [skipTickers, setSkipTickers] = useState((r0.rule_skip_tickers || []).join(", "));
  const [sides, setSides] = useState(r0.rule_sides || "both");
  const [minStrike, setMinStrike] = useState(r0.rule_min_strike ? String(r0.rule_min_strike) : "");
  const [skip0dte, setSkip0dte] = useState(!!r0.rule_skip_0dte);
  const [lossStreak, setLossStreak] = useState(r0.rule_loss_streak ? String(r0.rule_loss_streak) : "");
  const [keepAwake, setKeepAwake] = useState(st?.keep_awake !== false);
  // Steps mount before /api/state arrives: seed the fields once it does, so
  // re-saving Safety never blanks rules the owner already set.
  const seeded = useRef(!!st?.rules);
  useEffect(() => {
    const r = st?.rules;
    if (!r || seeded.current) return;
    seeded.current = true;
    setTickers((r.rule_tickers || []).join(", "));
    setSkipTickers((r.rule_skip_tickers || []).join(", "));
    setSides(r.rule_sides || "both");
    setMinStrike(r.rule_min_strike ? String(r.rule_min_strike) : "");
    setSkip0dte(!!r.rule_skip_0dte);
    setLossStreak(r.rule_loss_streak ? String(r.rule_loss_streak) : "");
    setKeepAwake(st?.keep_awake !== false);
  }, [st?.rules]);
  const [msg, setMsg] = useState<MsgT>(null);

  const save = async () => {
    const r = await api("/api/safety", {
      dry,
      cap,
      max_loss: maxLoss,
      discord,
      ntfy,
      tg_token: tgToken,
      tg_chat: tgChat,
      rule_tickers: tickers,
      rule_skip_tickers: skipTickers,
      rule_sides: sides,
      rule_min_strike: minStrike,
      rule_skip_0dte: skip0dte,
      rule_loss_streak: lossStreak,
      keep_awake: keepAwake,
    });
    if (r.ok) {
      setNext(r.next || "");
      done("safety");
    } else setMsg(err(r.error));
  };

  return (
    <StepCard
      id="safety"
      title="Safety rails and your rules"
      restored={isDone("safety") ? st?.notes?.safety || "Safety saved" : undefined}
      footer={
        <>
          <Msg m={msg} />
          <Spacer />
          <BackBtn />
          <Button variant="primary" className="btn-main" onClick={save}>
            Finish setup
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </>
      }
    >
      <Block label="Mode" desc="Dry-run logs every decision and places no orders.">
        <ChoiceRow type="checkbox" id="sf-dry" checked={dry} onChange={setDry} tone={dry ? "accent" : "live"}>
          Start in <b className="text-fg">dry-run</b> mode (log actions, place no orders; recommended)
        </ChoiceRow>
        {!dry && (
          <Notice tone="live" role="alert">
            <b>LIVE mode:</b> the agent will send real orders with real money to your broker. Your decision, not advice.
          </Notice>
        )}
      </Block>
      <Block label="This computer" desc="The agent can only trade while the computer is awake.">
        <ChoiceRow type="checkbox" id="sf-awake" checked={keepAwake} onChange={setKeepAwake}>
          Keep this computer awake while the agent runs (recommended). The screen can still turn off. Closing a laptop lid
          still puts it to sleep, so keep it open and plugged in during market hours.
        </ChoiceRow>
      </Block>
      <Block label="Limits">
        <Field htmlFor="sf-cap" label="Max new entries per day" className="max-w-xs">
          <Input id="sf-cap" type="number" min={1} value={cap} onChange={(e) => setCap(e.target.value)} className="font-mono" />
        </Field>
        <Field
          htmlFor="sf-maxloss"
          label="Max daily loss in $ (optional; blank or 0 = off)"
          help="Once today's estimated realized loss reaches this, no new entries until tomorrow. Exits still run."
          className="max-w-xs"
        >
          <Input id="sf-maxloss" type="number" min={0} placeholder="e.g. 300" value={maxLoss} onChange={(e) => setMaxLoss(e.target.value)} className="font-mono" />
        </Field>
      </Block>
      <Block
        label="Your rules"
        desc="Optional. Plain settings, no AI needed. They can only skip an entry, never start one; exits always run."
      >
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field htmlFor="rl-tickers" label="Only trade these tickers" help="Blank = any ticker. Example: SPY, QQQ, NVDA">
            <Input id="rl-tickers" placeholder="any" value={tickers} onChange={(e) => setTickers(e.target.value)} className="font-mono" />
          </Field>
          <Field htmlFor="rl-skip" label="Never trade these tickers" help="Blank = none. Example: TSLA">
            <Input id="rl-skip" placeholder="none" value={skipTickers} onChange={(e) => setSkipTickers(e.target.value)} className="font-mono" />
          </Field>
          <Field htmlFor="rl-sides" label="Calls, puts or both">
            <Select id="rl-sides" value={sides} onChange={(e) => setSides(e.target.value)}>
              <option value="both">Calls and puts</option>
              <option value="calls">Calls only</option>
              <option value="puts">Puts only</option>
            </Select>
          </Field>
          <Field htmlFor="rl-minstrike" label="Skip strikes under $" help="Blank or 0 = off. Example: 10 skips very low-priced names.">
            <Input id="rl-minstrike" type="number" min={0} placeholder="off" value={minStrike} onChange={(e) => setMinStrike(e.target.value)} className="font-mono" />
          </Field>
          <Field
            htmlFor="rl-streak"
            label="Stop for the day after this many losses in a row"
            help="Blank or 0 = off. Counts exits with an estimated loss today."
          >
            <Input id="rl-streak" type="number" min={0} placeholder="off" value={lossStreak} onChange={(e) => setLossStreak(e.target.value)} className="font-mono" />
          </Field>
        </div>
        <ChoiceRow type="checkbox" id="rl-0dte" checked={skip0dte} onChange={setSkip0dte}>
          Skip contracts that expire the same day (0DTE)
        </ChoiceRow>
      </Block>
      <Block label="Notifications" desc="Any or all, optional.">
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field htmlFor="nt-discord" label="Discord webhook URL">
            <Input id="nt-discord" placeholder="https://discord.com/api/webhooks/…" value={discord} onChange={(e) => setDiscord(e.target.value)} />
          </Field>
          <Field htmlFor="nt-ntfy" label="ntfy.sh topic">
            <Input id="nt-ntfy" placeholder="my-agent-alerts" value={ntfy} onChange={(e) => setNtfy(e.target.value)} />
          </Field>
          <Field htmlFor="nt-tg-token" label="Telegram bot token">
            <Input id="nt-tg-token" placeholder="123456:ABC…" value={tgToken} onChange={(e) => setTgToken(e.target.value)} />
          </Field>
          <Field htmlFor="nt-tg-chat" label="Telegram chat id">
            <Input id="nt-tg-chat" placeholder="e.g. 123456789" value={tgChat} onChange={(e) => setTgChat(e.target.value)} />
          </Field>
        </div>
      </Block>
    </StepCard>
  );
}

// ── 7. launch ───────────────────────────────────────────────────────────────
export function LaunchStep() {
  const { next, st, reloadState } = useWiz();
  const blocked = !!st?.policy_blocks_entries;
  const [polMsg, setPolMsg] = useState<MsgT>(null);
  const turnOffPolicy = async () => {
    const r = await api("/api/policy/disable", {});
    if (!r.ok) return setPolMsg(err(r.error || "Could not turn the policy off."));
    setPolMsg(ok(r.note || "Policy turned off."));
    reloadState();
  };
  const [startMsg, setStartMsg] = useState<MsgT>(null);
  const [autostart, setAutostart] = useState(true);
  const [cash, setCash] = useState<{ known?: boolean; buying_power?: number; warning?: string; dry_run?: boolean } | null>(null);
  useEffect(() => {
    api("/api/broker/cash")
      .then((r) => setCash(r))
      .catch(() => {});
  }, []);
  const [doToken, setDoToken] = useState("");
  const [region, setRegion] = useState("nyc3");
  const [deployMsg, setDeployMsg] = useState<React.ReactNode>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const startLocal = async () => {
    setStartMsg(info("Starting…"));
    let r;
    try {
      r = await api("/api/start-local", { autostart });
    } catch {
      return setStartMsg(err("Could not reach the setup server."));
    }
    if (!r.ok) return setStartMsg(err(r.error));
    setStartMsg(info("Starting… the agent takes over this address in a few seconds."));
    // The wizard hands its port to the running agent; wait until the agent's
    // dashboard answers with a real mode, then hop over to it.
    const hop = async () => {
      try {
        const s = await fetch("/api/status");
        if (s.ok) {
          const j = await s.json();
          if (j.mode && j.mode !== "OFFLINE") return void (location.href = "/dash");
        }
      } catch {
        /* still switching over */
      }
      timers.current.push(setTimeout(hop, 1500));
    };
    timers.current.push(setTimeout(hop, 1500));
  };

  const deploy = async () => {
    setDeployMsg(<span className="text-muted">Creating server… (takes 1–3 minutes)</span>);
    const r = await api("/api/deploy", { token: doToken, region });
    if (!r.ok) return setDeployMsg(<span className="text-down">{r.error}</span>);
    const poll = async () => {
      const s = await api("/api/deploy/status");
      if (s.ip) {
        setDeployMsg(
          <div className="space-y-1.5">
            <p className="m-0 text-up">Server created at {s.ip}</p>
            <p className="m-0 text-muted">
              It's installing the agent now (about 5 minutes). You'll get the "heartbeat started" notification when it's
              running; after that this computer doesn't need to run anything. Dashboard from here:
            </p>
            <pre className="m-0 overflow-auto rounded border border-line bg-sink p-2 font-mono text-xs text-fg2">
              {`${s.ssh || `ssh -L 8721:127.0.0.1:8721 trader@${s.ip}`}\nthen open http://127.0.0.1:8721/dash`}
            </pre>
            <p className="m-0 text-muted">
              No notification after 15 minutes? Log in with the command above and run{" "}
              <code className="font-mono text-xs">sudo tail -50 /var/log/cloud-init-output.log</code>
            </p>
          </div>,
        );
        api("/api/finish", {});
      } else {
        setDeployMsg(<span className="text-muted">Server status: {s.status}. Waiting…</span>);
        timers.current.push(setTimeout(poll, 5000));
      }
    };
    poll();
  };

  return (
    <StepCard
      id="done"
      title="Launch: where should your agent live?"
      footer={
        <>
          <span className="text-xs text-muted">
            Dry-run or LIVE is what you chose in Safety. Switch any time from the dashboard. Your decision, not advice.
          </span>
          <Spacer />
          <BackBtn />
        </>
      }
    >
      <Block>
        {blocked && (
          <Notice tone="live" role="alert">
            <p className="m-0">
              <b>Your agent would never enter a trade.</b> You saved a trading policy but didn't connect an AI. The agent needs an
              AI to check your rules, so without one it skips every entry to stay safe.
            </p>
            <p className="m-0 mt-2">
              Fix it one of two ways: go back to the <b>AI step</b> and connect one, or turn the policy off (your built-in rules in Safety still apply).
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Button onClick={turnOffPolicy}>Turn my policy off</Button>
              <Msg m={polMsg} />
            </div>
          </Notice>
        )}
        {cash?.warning && (
          <Notice tone={cash.dry_run ? "warn" : "live"} role="alert">
            <p className="m-0">
              <b>Check your broker cash.</b> {cash.warning}
            </p>
            <p className="m-0 mt-2 text-muted">
              {cash.dry_run
                ? "You're starting in dry-run, so nothing is blocked yet. Add cash before you go live."
                : "Add cash in your broker's app (or lower your per-trade budget in Sizing) before starting, or every order will be rejected."}
            </p>
          </Notice>
        )}
        {next != null && !blocked && (
          <div className="space-y-2">
            <p className="m-0 text-sm text-up">Setup complete. Pick where your agent should live.</p>
            {next && (
              <Disclosure summary="…or start it from a terminal instead">
                <pre className="m-0 overflow-auto whitespace-pre-wrap rounded border border-line bg-sink p-3 font-mono text-xs text-fg2">{next}</pre>
              </Disclosure>
            )}
          </div>
        )}
        <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
          <div className="flex min-w-0 flex-col gap-3 rounded-md border border-line2 bg-panel2 p-4">
            <h3 className="m-0 text-base font-semibold">On this computer</h3>
            <p className="m-0 text-sm text-muted">
              One click: the agent starts here in the background and this page becomes its dashboard. You can close the terminal
              window and it keeps running. Keep this computer awake during market hours.
            </p>
            <ChoiceRow type="checkbox" id="autostart" checked={autostart} onChange={setAutostart}>
              Also start it automatically when I log in to this computer, and open this dashboard (already signed in)
            </ChoiceRow>
            <div className="mt-auto flex flex-wrap items-center gap-3 pt-2">
              <Button variant="primary" onClick={startLocal} disabled={blocked}>
                Start my agent now
              </Button>
              <Msg m={startMsg} />
            </div>
          </div>
          <div className="flex min-w-0 flex-col gap-3 rounded-md border border-line2 bg-panel2 p-4">
            <h3 className="m-0 text-base font-semibold">On its own cloud server</h3>
            <p className="m-0 text-sm text-muted">
              About $6 a month on <b className="text-fg2">your</b> DigitalOcean account. The agent creates the server, moves its finished
              setup there, and starts itself. It also makes a login key on this computer so you can reach the dashboard. Your token is used once and not stored.
            </p>
            <Field htmlFor="do-token" label="DigitalOcean API token (write scope)">
              <Input id="do-token" type="password" autoComplete="off" placeholder="DigitalOcean API token" value={doToken} onChange={(e) => setDoToken(e.target.value)} />
            </Field>
            <Field htmlFor="do-region" label="Region">
              <div className="flex gap-2 max-sm:flex-col">
                <Select id="do-region" value={region} onChange={(e) => setRegion(e.target.value)} className="flex-1">
                  {["nyc3", "sfo3", "tor1", "lon1", "fra1", "sgp1"].map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </Select>
                <Button onClick={deploy} className="h-9" disabled={blocked}>
                  Create my agent's server
                </Button>
              </div>
            </Field>
            {deployMsg && (
              <div role="status" aria-live="polite" className="text-sm">
                {deployMsg}
              </div>
            )}
          </div>
        </div>
      </Block>
    </StepCard>
  );
}
