// What practice and LIVE mean with AgentHC signals, in the owner's words.
// The same sentences as agent.py's PRACTICE_MODE_TEXT / LIVE_MODE_TEXT /
// ALPACA_PAPER_LIVE_TEXT (tests/test_practice_release.py checks they match):
// AgentHC releases a practice trade only once it has closed ("release on
// close"), and only LIVE uses signal days.

export const PRACTICE_MODE_TEXT =
  "Practice mode logs each trade only after it closes — with the time AgentHC entered and exited and its recorded result — sized to your budget and rules. It never uses a signal day or buys a pass.";

export const LIVE_MODE_TEXT =
  "Live mode trades signals in real time and uses one signal day on each trading day you receive signals.";

export const ALPACA_PAPER_LIVE_TEXT =
  "An Alpaca paper-trading account with Live selected is LIVE: it receives the signals in real time and uses signal days too.";

/** The practice sentence for a one-line banner. */
export const PRACTICE_BANNER_TEXT =
  "Each trade is logged only after it closes — with the time AgentHC entered and exited and its recorded result — sized to your budget and rules. Practice never uses a signal day or buys a pass.";
