// What the /v2 settings routes in webui.py answer (GET /api/v2/settings and
// the POST /api/v2/... writes), plus the few classic read-only routes /v2
// reuses as they are. Written by hand from the server code; the shapes are
// pinned by tests/test_v2_settings_api.py on the server side and by
// webui_app/tests/guardian.test.mjs here. Types only: nothing here runs.

/** What a change does to the owner's risk, as the server works it out. */
export type Direction = "raises_risk" | "lowers_risk" | "neutral";

/** How a secret appears in a GET: whether it is set, and at most its last 4
 *  characters (none for a password or a short value). Never the value. */
export interface Secret {
  set: boolean;
  last4: string | null;
}

export type SizeMode = "budget" | "contracts" | "percent";
/** One book's saved trade size; null = not set (the other-trades book then
 *  sizes the same as the main pick). */
export interface SizeSpec {
  mode: SizeMode;
  value: number | null;
}

export interface SettingsView {
  ok: true;
  /** what a preview is bound to: it changes whenever a setting does */
  fingerprint: string;
  agent_running: boolean;
  /** false: config.json can't be read right now; the values are the last good read */
  settings_readable: boolean;
  mode: { mode: "live" | "practice"; explicit: boolean };
  paused: boolean;
  limits: {
    trade_size: { main: SizeSpec | null; other: SizeSpec | null };
    max_entries_per_day: number | null;
    /** null or 0: no daily loss limit */
    max_daily_loss_usd: number | null;
    max_open_positions: number | null;
    defaults: { max_entries_per_day: number; max_open_positions: number };
    /** [low, high] each limit accepts */
    allowed: Record<"max_entries_per_day" | "max_daily_loss_usd" | "max_open_positions", [number, number]>;
  };
  rules: {
    /** as saved: a list (or, from older versions, text); null when never set */
    rule_tickers: string[] | string | null;
    rule_skip_tickers: string[] | string | null;
    rule_sides: string | null;
    rule_min_strike: number | null;
    rule_skip_0dte: boolean | null;
    rule_loss_streak: number | null;
    /** rules.describe(): "SPY, QQQ only · calls only", "" when none */
    summary: string;
    /** the written rules (policy file), "" when there are none */
    policy: string;
    /** written rules with no AI to check them: every entry is skipped */
    policy_blocks_entries: boolean;
  };
  alerts: { discord: Secret; ntfy: Secret; tg_token: Secret; tg_chat: Secret };
  computer: {
    autostart: { supported: boolean; enabled: boolean | null; how: string };
    keep_awake: boolean;
  };
  ai: {
    connected: boolean;
    provider: string | null;
    name: string | null;
    model: string | null;
    /** the endpoint's site only, for a custom one */
    endpoint: string | null;
    key: Secret;
  };
  broker: {
    id: string | null;
    /** null: could not be checked */
    ready: boolean | null;
    account_mask: string | null;
    /** the broker's paper-trading account; null when no broker */
    paper: boolean | null;
    /** the connected broker's own fields: switches as true/false, keys as Secret */
    fields: Record<string, boolean | Secret>;
  };
  signals: {
    source: string | null;
    include_other_trades: boolean;
    recurring: boolean;
    wallet_connected: boolean;
    api_key: Secret;
    pass_on_file: boolean;
  };
}

/** One line of a server preview: Saved now → New. Values are as the server
 *  keeps them (numbers, a SizeSpec, a broker {id, paper}, a mode word…). */
export interface Change {
  key: string;
  label: string;
  saved_now: unknown;
  new: unknown;
  direction: Direction;
}

export interface Preview {
  changes: Change[];
  /** what the change does not do, in the server's words */
  does_not: string[];
  token: string;
  expires_in_s: number;
  /** going LIVE: the tick box is needed too, and these facts come with it */
  needs_acknowledge?: boolean;
  account?: { broker: string | null; name: string | null; account_mask: string | null; paper_account: boolean | null };
  buying_power?: number | null;
  buying_power_at?: string | null;
  trade_size?: { main: SizeSpec | null; other: SizeSpec | null };
  limits?: { max_entries_per_day: number | null; max_daily_loss_usd: number | null; max_open_positions: number | null };
  real_positions?: number | null;
  cash_low?: boolean | null;
}

/** Every /v2 write answers with one of these (HTTP 200). */
export type Reply<Done = object> =
  | { ok: true; needs_confirm: true; preview: Preview }
  | ({ ok: true; needs_confirm?: undefined; saved?: boolean; note?: string; settings?: SettingsView } & Done)
  | { ok: false; error: string; needs_acknowledge?: boolean; refresh_sizes?: boolean; unavailable?: boolean };

/** POST /api/v2/pass/buy, confirmed: the Lightning invoice to pay. */
export interface Invoice {
  bolt11: string;
  amount_sats: number | null;
  amount_usd: number | null;
  extends: boolean;
  carried: number;
  access_until: string | null;
  /** the QR code as rows of dark (1) / light (0) modules; null when not made */
  qr?: number[][] | null;
}

/** POST /api/v2/command, applied: the agent's own reply. */
export interface CommandDone {
  reply: string;
}

/** POST /api/v2/broker/robinhood, confirmed: where to sign in. */
export interface SignInDone {
  url: string;
}

/** GET /api/brokers: the key-based brokers' own field lists. */
export interface BrokerField {
  id: string;
  label: string;
  type: "text" | "password" | "checkbox" | string;
  default?: boolean | string;
  help?: string;
  placeholder?: string;
}
export interface BrokerForm {
  id: string;
  name: string;
  fields: BrokerField[];
  help?: string;
  note?: string;
}

/** GET /api/llm/providers */
export interface AiProviders {
  providers: Record<string, { name: string; base_url: string; keys_url: string }>;
}
/** POST /api/llm/models (lists models; saves nothing) */
export type AiModels = { ok: true; provider: string; models: string[]; suggested: string } | { ok: false; error: string; need_provider?: boolean };

/** GET /api/pass/options: sizes on sale now; [] = none right now; null = could not ask */
export interface PassOptions {
  options: number[] | null;
}
/** GET /api/pass/status (the classic route; it also files a paid invoice's pass) */
export interface PassCheck {
  status?: string;
  days_left?: number | null;
  error?: string;
}
/** GET /api/wallet/balance */
export type WalletBalance = { ok: true; sats: number } | { ok: false; error: string };
