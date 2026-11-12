"""Notifications — tell the human what the agent did, the moment it does it.

Autonomous + silent is a bad combination. Configure any (or all) of:

  - Discord webhook:  config `discord_webhook_url`
    (channel settings -> Integrations -> Webhooks -> New Webhook)
  - ntfy.sh topic:    config `ntfy_topic` (pick a hard-to-guess topic name,
    then subscribe in the ntfy app; no signup needed)
  - Telegram:         config `telegram_bot_token` + `telegram_chat_id`
    (@BotFather to create a bot; message it once, then get chat id from
    https://api.telegram.org/bot<TOKEN>/getUpdates)
  - Phone app:        automatic for phones paired with `agent.py pair`

notify() is fail-soft: a broken webhook never interrupts trading.
"""

import requests

_TIMEOUT = 10


def notify(cfg, text, priority=None):
    """Send `text` to every configured channel. Never raises.

    priority="high": urgent — ntfy delivers it at its top priority; the
    other channels have no priority setting and get the same text."""
    sent = False
    url = cfg.get("discord_webhook_url")
    if url:
        try:
            requests.post(url, json={"content": text[:1900]}, timeout=_TIMEOUT)
            sent = True
        except Exception:
            pass
    topic = cfg.get("ntfy_topic")
    if topic:
        headers = {"Title": "agentic-trader"}
        if priority == "high":
            headers.update({"Priority": "urgent", "Tags": "warning"})
        try:
            requests.post(f"https://ntfy.sh/{topic}",
                          data=text[:4000].encode(),
                          headers=headers, timeout=_TIMEOUT)
            sent = True
        except Exception:
            pass
    token = cfg.get("telegram_bot_token")
    chat = cfg.get("telegram_chat_id")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000]},
                          timeout=_TIMEOUT)
            sent = True
        except Exception:
            pass
    if _expo_push(text, priority):
        sent = True
    return sent


_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _expo_push(text, priority=None):
    """Push `text` to every paired phone (remote_access). Never raises.

    Goes through Expo's push service, which hands it to Apple / Google; the
    phone app registered its token when it paired. Only the alert text is
    sent, never keys or account data."""
    try:
        import remote_access
        tokens = remote_access.push_tokens()
    except Exception:
        return False
    if not tokens:
        return False
    first, _, rest = text.strip().partition("\n")
    msgs = [{"to": t, "title": first[:100] or "agentic-trader",
             "body": (rest.strip() or first)[:900], "sound": "default",
             "priority": "high" if priority == "high" else "default"}
            for t in tokens]
    try:
        requests.post(_EXPO_PUSH_URL, json=msgs, timeout=_TIMEOUT,
                      headers={"Accept": "application/json"})
        return True
    except Exception:
        return False


def setup(cfg):
    """Interactive wizard step for notifications."""
    print("\n-- Optional: notifications --")
    print("Get a message on every action, veto, and error (recommended —")
    print("never run an autonomous trader silently).")
    v = input("Discord webhook URL (blank to skip): ").strip()
    if v:
        cfg["discord_webhook_url"] = v
    v = input("ntfy.sh topic (blank to skip): ").strip()
    if v:
        cfg["ntfy_topic"] = v
    v = input("Telegram bot token (blank to skip): ").strip()
    if v:
        cfg["telegram_bot_token"] = v
        cfg["telegram_chat_id"] = input("Telegram chat id: ").strip()
    if notify(cfg, "agentic-trader: notifications configured ✓"):
        print("Test notification sent — check your channel.")
    return cfg
