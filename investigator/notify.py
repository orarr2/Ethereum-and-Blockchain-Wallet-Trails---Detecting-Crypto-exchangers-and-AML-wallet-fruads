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
                 site_url: str | None = None, reranker: Reranker | None = None,
                 is_demo: bool = False) -> dict:
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

    demo_html = ["⚠️ <b>DEMO DATA</b> - sample table, not a real BigQuery run.", ""] if is_demo else []
    demo_plain = ["[DEMO DATA] sample table, not a real BigQuery run.", ""] if is_demo else []
    html_lines = [f"<b>AML Investigator digest</b>", *demo_html,
                  f"<i>Order: {order_kind}. Research leads only, not findings of guilt.</i>", ""]
    plain_lines = ["AML Investigator digest", *demo_plain,
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
def send_telegram(text_html: str, token: str | None = None, chat_id: str | None = None) -> dict:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("[notify] TELEGRAM_BOT_TOKEN/CHAT_ID not set - dry run. Message:\n")
        print(text_html)
        return {"sent": False, "reason": "no_credentials"}
    # Telegram hard limit is 4096 chars per message
    text = text_html[:4000]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            resp = json.load(r)
        return {"sent": bool(resp.get("ok")), "response": resp}
    except Exception as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


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
    is_demo = False
    if master is None:
        master, is_demo = C.load_master()
    if not len(master):
        print("[notify] no master table; nothing to send.")
        return {"sent": False, "reason": "no_master"}

    digest = build_digest(master, top_n_per_chain=top_n_per_chain, site_url=site_url,
                          is_demo=is_demo)
    if channel == "telegram":
        res = send_telegram(digest["text_html"])
    elif channel == "email":
        res = send_email(f"AML Investigator - {digest['n_leads']} leads", digest["text_plain"])
    elif channel == "ntfy":
        res = send_ntfy(digest["text_plain"])
    else:
        raise ValueError(f"unknown NOTIFY_CHANNEL={channel!r}")
    res["n_leads"] = digest["n_leads"]
    print(f"[notify] channel={channel} sent={res.get('sent')} leads={digest['n_leads']}")
    return res


if __name__ == "__main__":  # pragma: no cover
    notify()
