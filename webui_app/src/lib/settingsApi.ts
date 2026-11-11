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
    /** open the dashboard after login (the chat's "open at login"); null: not known */
    open_dashboard_at_login: boolean | null;
    /** the dashboard "/" lands on */
    dashboard: Dashboard;
  };
  /** the expiry sell and its alert (HH:MM ET; null = off); null: not known */
  expiry: Expiry | null;
  code: { self_edit_enabled: boolean };
  /** the agreement on file (its text: GET /api/v2/agreement) */
  consent: Consent;
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
    /** how the last Robinhood sign-in ended (seq 0: none since the agent started) */
    signin: SignInEnd;
    /** the classic wizard's Robinhood warning, as a code (options not turned on) */
    warning: "options_off" | null;
  };
  signals: {
    source: string | null;
    include_other_trades: boolean;
    /** practice mode's "what it would have taken" preview is on */
    dry_run_preview: boolean;
    recurring: boolean;
    wallet_connected: boolean;
    api_key: Secret;
    pass_on_file: boolean;
    /** signal-day purchases on file by id: paid, or an invoice that can still be paid
     *  (an invoice that expired unpaid is in neither) */
    purchases: Purchases;
  };
}

export type Dashboard = "classic" | "v2";

export interface Expiry {
  sell_et: string | null;
  alert_et: string | null;
  /** today's times (an early close moves both) */
  effective_today: { sell_et: string | null; alert_et: string | null; close_et: string | null; early_close: boolean };
  /** the classic status line ("sells at 15:45 ET, alert at 15:50 ET") */
  described: string;
  allowed: [string, string];
}

export interface Consent {
  /** the agreement on file is the current one */
  accepted: boolean;
  on_file: boolean;
  accepted_at: string | null;
  terms_version: string | null;
  current_terms_version: string;
}

/** A real position a stop or a move leaves with nobody selling it here. */
export interface Unsold {
  id: string;
  label: string | null;
  contract: { symbol: string; expiry: string; strike: number; side: "C" | "P" } | null;
  qty: number | null;
  expires_today: boolean;
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
  /** stop / move: the real positions nobody sells here afterwards; null: not known */
  unsold?: Unsold[] | null;
  /** move: whether an agent runs behind this page (it stops) */
  agent_running?: boolean;
  /** start: the saved mode it starts in */
  mode?: "live" | "practice";
}

/** Every /v2 write answers with one of these (HTTP 200). */
export type Reply<Done = object> =
  | { ok: true; needs_confirm: true; preview: Preview }
  | ({ ok: true; needs_confirm?: undefined; saved?: boolean; note?: string; settings?: SettingsView } & Done)
  | { ok: false; error: string; needs_acknowledge?: boolean; refresh_sizes?: boolean; unavailable?: boolean };

export interface Purchases {
  paid: string[];
  waiting: string[];
}

/** POST /api/v2/pass/buy, confirmed: the Lightning invoice to pay. */
export interface Invoice {
  /** this purchase's id in SettingsView.signals.purchases */
  purchase?: string;
  bolt11: string;
  amount_sats: number | null;
  amount_usd: number | null;
  extends: boolean;
  carried: number;
  access_until: string | null;
  /** the QR code as rows of dark (1) / light (0) modules; null when not made */
  qr?: number[][] | null;
}

/** POST /api/v2/agent/start, applied (start_local's answer). */
export interface StartDone {
  autostart_note?: string;
}

/** POST /api/move/export (classic, reused): the move file is ready to download. */
export interface MoveFile {
  filename: string;
  kb: number;
}

/** POST /api/v2/move/finish or /api/v2/move/deploy, applied. */
export interface MoveDone {
  note: string;
  here?: string;
  retired?: boolean;
}

/** GET /api/deploy/status (classic, reused): the cloud server's progress. */
export interface DeployStatus {
  status: string;
  ip: string;
  ssh?: string;
  error?: string;
}

/** POST /api/v2/command, applied: the agent's own reply. */
export interface CommandDone {
  reply: string;
}

/** POST /api/v2/broker/robinhood, confirmed: where to sign in, and the
 *  sign-in count it starts from (the next finished sign-in is seq + 1). */
export interface SignInDone {
  url: string;
  signin_seq: number;
}

/** How the last Robinhood sign-in ended. seq moves with every finished
 *  sign-in, even one that changed nothing in the settings. */
export interface SignInEnd {
  seq: number;
  /** true: an account was saved; false: none (error says why when it failed); null: none yet */
  connected: boolean | null;
  account_mask: string | null;
  /** options trading is not turned on in that account: Robinhood rejects the orders */
  options_off: boolean;
  error: string | null;
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
