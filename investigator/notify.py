"""
Phone notification - the last mile.

Turns the reranked candidate queue into a short digest and pushes it to your
phone. Default channel is a Telegram bot (free, instant, no app to build); email
(SMTP) and ntfy are included as drop-in alternatives.

The digest itself needs NO LLM and NO BigQuery - it just re-ranks the existing
`suspicious_wallets_master.csv` and lists the top leads with a link to the full
dossiers on GitHub Pages. That keeps the twice-daily push free and reliable even
when no API keys are configured; richer dossiers are a separate, optional step.

Env (set as GitHub Secrets in CI):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   - Telegram channel
  INVESTIGATOR_SITE_URL                  - base URL of the Pages site (for links)
  NOTIFY_EMAIL_TO / SMTP_*               - email channel (optional)
  NTFY_TOPIC / NTFY_SERVER               - ntfy channel (optional)

Dry run: if the chosen channel has no credentials, the message is printed to
stdout and the function returns cleanly (so CI logs still show what would send).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from html import escape

import pandas as pd

from . import config as C
from .adaptive_triage import FeatureBuilder, LinUCB, Reranker


# ---------------------------------------------------------------------------
# Digest construction
# ---------------------------------------------------------------------------
def build_digest(master: pd.DataFrame, top_n_per_chain: int = 5,
                 site_url: str | None = None, reranker: Reranker | None = None) -> dict:
    """Return {'text_html', 'text_plain', 'n_leads'} for the top leads."""
    site_url = site_url or os.environ.get("INVESTIGATOR_SITE_URL", "").rstrip("/")
    if reranker is None:
        linucb = LinUCB.load()
        fb = FeatureBuilder(FeatureBuilder.cluster_vocab_from_master(master))
        reranker = Reranker(linucb=linucb, feature_builder=fb)
    order_kind = "adaptive (LinUCB)" if reranker.active() else "static (actionability)"

    chains = [c for c in C.CHAINS if c in set(master.get("chain", pd.Series()).unique())]
    if not chains:
        chains = sorted(master["chain"].dropna().unique().tolist()) if "chain" in master else []

    html_lines = [f"<b>AML Investigator digest</b>",
                  f"<i>Order: {order_kind}. Research leads only, not findings of guilt.</i>", ""]
    plain_lines = ["AML Investigator digest",
                   f"Order: {order_kind}. Research leads only.", ""]
    total = 0
    for chain in chains:
        chain_df = master[master["chain"] == chain]
        if not len(chain_df):
            continue
        ranked = reranker.rerank(chain_df).head(top_n_per_chain)
        html_lines.append(f"<b>{escape(chain)}</b>")
        plain_lines.append(chain.upper())
        for _, r in ranked.iterrows():
            wallet = str(r.get("wallet", ""))
            score = r.get("bandit_score")
            score_s = f"{score:.2f}" if pd.notna(score) else f"{r.get('actionability', '')}"
            flags = _flags(r)
            short = wallet[:10] + "..." + wallet[-6:] if len(wallet) > 20 else wallet
            html_lines.append(f"  • <code>{escape(short)}</code>  score {escape(str(score_s))}"
                              + (f"  <i>{escape(flags)}</i>" if flags else ""))
            plain_lines.append(f"  - {short}  score {score_s}" + (f"  [{flags}]" if flags else ""))
            total += 1
        html_lines.append("")
        plain_lines.append("")

    if site_url:
        html_lines.append(f'Full dossiers: <a href="{escape(site_url)}">{escape(site_url)}</a>')
        plain_lines.append(f"Full dossiers: {site_url}")

    return {"text_html": "\n".join(html_lines).strip(),
            "text_plain": "\n".join(plain_lines).strip(),
            "n_leads": total}


def _flags(row) -> str:
    bits = []
    if bool(row.get("has_exchange_anchor")):
        bits.append("anchor")
    if bool(row.get("is_bridge_wallet")):
        bits.append("bridge")
    if bool(row.get("hits_interesting_country")):
        bits.append("geo")
    cts = row.get("campaign_terror_score")
    try:
        if cts is not None and float(cts) >= 70:
            bits.append("CTF")
    except (TypeError, ValueError):
        pass
    return ",".join(bits)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
def _api(token: str, method: str, params: dict | None = None) -> dict:
    """Call a Telegram Bot API method and ALWAYS return the parsed response,
    even on 4xx (so `description` reaches the caller instead of being lost in
    a generic HTTPError). Redacts token when raising."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # Telegram returns useful JSON even on 4xx - read it
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = f'{{"ok":false,"description":"HTTPError {e.code}"}}'
    except Exception as e:
        return {"ok": False, "description": f"{type(e).__name__}: {e}"}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "description": f"non-JSON response: {body[:200]}"}


def preflight_telegram(token: str | None = None, chat_id: str | None = None) -> dict:
    """Prove BOTH secrets are valid before we build a digest.
      * getMe    - proves the token opens a real bot.
      * getChat  - proves the chat_id is one the bot can talk to.
    Returns {ok, bot_name, chat_kind, problem} - `problem` is empty on success
    and names the exact broken piece otherwise."""
    token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    out = {"ok": False, "bot_name": "", "chat_kind": "", "problem": ""}
    if not token:
        out["problem"] = "TELEGRAM_BOT_TOKEN secret is missing or empty"
        return out
    if not chat_id:
        out["problem"] = "TELEGRAM_CHAT_ID secret is missing or empty"
        return out
    me = _api(token, "getMe")
    if not me.get("ok"):
        out["problem"] = f"getMe failed - TELEGRAM_BOT_TOKEN is invalid ({me.get('description','?')})"
        return out
    out["bot_name"] = (me.get("result") or {}).get("username", "?")
    chat = _api(token, "getChat", {"chat_id": chat_id})
    if not chat.get("ok"):
        out["problem"] = (f"getChat failed - TELEGRAM_CHAT_ID is invalid or the bot "
                          f"has not been messaged from this chat ({chat.get('description','?')})")
        return out
    out["chat_kind"] = (chat.get("result") or {}).get("type", "?")
    out["ok"] = True
    return out


def send_telegram(text_html: str, token: str | None = None, chat_id: str | None = None) -> dict:
    token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        which = "BOTH" if not token and not chat_id else ("TOKEN" if not token else "CHAT_ID")
        print(f"[notify] TELEGRAM {which} secret missing - dry run. Message:\n")
        print(text_html)
        return {"sent": False, "reason": f"no_credentials:{which}"}
    text = text_html[:4000]   # Telegram hard limit is 4096 chars
    resp = _api(token, "sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    ok = bool(resp.get("ok"))
    return {"sent": ok, "response": resp,
            "reason": ("" if ok else resp.get("description", "unknown"))}


def send_email(subject: str, text_plain: str) -> dict:
    to = os.environ.get("NOTIFY_EMAIL_TO", "")
    host = os.environ.get("SMTP_HOST", "")
    if not to or not host:
        print("[notify] SMTP not configured - dry run.")
        return {"sent": False, "reason": "no_credentials"}
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(text_plain, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", to))
    msg["To"] = to
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
            if user:
                s.login(user, pw)
            s.send_message(msg)
        return {"sent": True}
    except Exception as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


def send_ntfy(text_plain: str, title: str = "AML Investigator") -> dict:
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        print("[notify] NTFY_TOPIC not set - dry run.")
        return {"sent": False, "reason": "no_credentials"}
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        req = urllib.request.Request(f"{server}/{topic}", data=text_plain.encode("utf-8"),
                                     headers={"Title": title, "Tags": "mag"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return {"sent": True}
    except Exception as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


def notify(master: pd.DataFrame | None = None, channel: str | None = None,
           top_n_per_chain: int = 5, site_url: str | None = None) -> dict:
    """Build the digest and send it on the chosen channel (default: telegram)."""
    channel = (channel or os.environ.get("NOTIFY_CHANNEL", "telegram")).lower()
    if master is None:
        master = pd.read_csv(C.MASTER_CSV) if C.MASTER_CSV.exists() else pd.DataFrame()
    if not len(master):
        print("[notify] no master table; nothing to send.")
        return {"sent": False, "reason": "no_master"}

    digest = build_digest(master, top_n_per_chain=top_n_per_chain, site_url=site_url)
    if channel == "telegram":
        res = send_telegram(digest["text_html"])
    elif channel == "email":
        res = send_email(f"AML Investigator - {digest['n_leads']} leads", digest["text_plain"])
    elif channel == "ntfy":
        res = send_ntfy(digest["text_plain"])
    else:
        raise ValueError(f"unknown NOTIFY_CHANNEL={channel!r}")
    res["n_leads"] = digest["n_leads"]
    reason = res.get("reason", "")
    print(f"[notify] channel={channel} sent={res.get('sent')} leads={digest['n_leads']}"
          + (f" reason={reason!r}" if reason else ""))
    if not res.get("sent") and "response" in res:
        # surface the API's own words - useful when the reason is truncated
        print(f"[notify] telegram api response: {res['response']}")
    return res


if __name__ == "__main__":  # pragma: no cover
    import sys
    res = notify()
    # Exit non-zero when creds ARE set but the message wasn't delivered - so the
    # CI step turns red and stops lying that a silent failure was a success. If
    # credentials are missing (no_credentials), stay green - that is a dry-run.
    if not res.get("sent") and not str(res.get("reason", "")).startswith("no_credentials"):
        sys.exit(1)
