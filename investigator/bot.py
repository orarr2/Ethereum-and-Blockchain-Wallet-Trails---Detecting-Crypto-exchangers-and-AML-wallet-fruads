"""
Two-way Telegram bot for the investigator layer.

A GitHub Actions cron job (every 5 min) runs `python -m investigator.bot`,
which calls Telegram's `getUpdates`, dispatches each user command to a
handler, and replies. **No always-on server; no webhook URL needed** - the bot
lives inside the same free tier that already runs the twice-daily scan.

Persistence: none. We use Telegram's native offset acknowledgment: after
processing, we tell Telegram "everything up to update_id X is done", so the
next poll returns only newer messages. Telegram holds updates for 24 hours,
so as long as the cron runs at least once a day nothing is lost.

Security: replies ONLY to messages whose chat_id matches the configured
TELEGRAM_CHAT_ID secret. Anything else is silently ignored - if a stranger
finds the bot, they get nothing.

Commands
--------
/help,  /start          welcome + this list
/scan                   current top-3-per-chain digest (built from master)
/top   [chain] [N]      top-N leads for one chain (default: ethereum, 10)
/wallet <address>       details for one wallet from the master table
/stats                  dataset stats
"""
from __future__ import annotations

import os
import sys
from html import escape

import pandas as pd

from . import config as C
from .notify import _api, build_digest, send_telegram, send_telegram_document

_COMMANDS_HELP = (
    "<b>AML Investigator bot</b>\n"
    "Research leads only, not findings of guilt.\n\n"
    "<b>Commands</b>\n"
    "/report             full PDF report + master CSV (as attachments)\n"
    "/scan               top leads across chains (text digest)\n"
    "/top [chain] [N]    top-N for one chain (default: ethereum 10)\n"
    "/wallet &lt;addr&gt;      details for one wallet\n"
    "/stats              dataset stats\n"
    "/help               this message"
)

_CHAINS = {"ethereum", "tron", "bitcoin", "zcash"}


def _master() -> pd.DataFrame:
    return pd.read_csv(C.MASTER_CSV) if C.MASTER_CSV.exists() else pd.DataFrame()


def _short(addr: str) -> str:
    return addr[:10] + "..." + addr[-6:] if len(addr) > 20 else addr


def _flags(row) -> str:
    bits = []
    if bool(row.get("has_exchange_anchor")):
        bits.append("anchor")
    if bool(row.get("is_bridge_wallet")):
        bits.append("bridge")
    if bool(row.get("hits_interesting_country")):
        bits.append("geo")
    try:
        cts = row.get("campaign_terror_score")
        if cts is not None and float(cts) >= 70:
            bits.append("CTF")
    except (TypeError, ValueError):
        pass
    return ",".join(bits)


def _cmd_scan(_arg: str) -> str:
    m = _master()
    if not len(m):
        return "no master table available."
    d = build_digest(m, top_n_per_chain=3, site_url=os.environ.get("INVESTIGATOR_SITE_URL"))
    return d["text_html"]


def _cmd_top(args: str) -> str:
    parts = args.split()
    chain = (parts[0].lower() if parts else "ethereum").strip()
    if chain not in _CHAINS:
        return f"unknown chain {escape(chain)}. Try one of: " + ", ".join(sorted(_CHAINS))
    try:
        n = int(parts[1]) if len(parts) > 1 else 10
    except ValueError:
        n = 10
    n = max(1, min(n, 25))
    m = _master()
    sub = m[m["chain"] == chain]
    if not len(sub):
        return f"no rows for chain {escape(chain)}."
    sub = sub.sort_values("actionability", ascending=False).head(n)
    lines = [f"<b>top-{n} {escape(chain)}</b>"]
    for _, r in sub.iterrows():
        w = str(r.get("wallet", ""))
        f = _flags(r)
        lines.append(f"  • <code>{escape(_short(w))}</code>  score {r.get('actionability', '')}"
                     + (f"  <i>{escape(f)}</i>" if f else ""))
    return "\n".join(lines)


def _cmd_wallet(arg: str) -> str:
    addr = arg.strip()
    if not addr:
        return "usage: /wallet &lt;address&gt;"
    m = _master()
    if not len(m):
        return "no master table available."
    row = m[m["wallet"].astype(str).str.lower() == addr.lower()]
    if not len(row):
        return f"wallet <code>{escape(addr)}</code> not in the master table."
    r = row.iloc[0]
    lines = [f"<b>{escape(addr)}</b>"]
    for k in ("chain", "risk_score", "actionability", "distinct_senders", "distinct_recipients",
              "has_exchange_anchor", "anchor_exchange", "anchor_usdt",
              "is_bridge_wallet", "components_bridged",
              "campaign_terror_score", "country_codes", "hits_interesting_country"):
        if k not in r.index:
            continue
        v = r[k]
        if pd.isna(v) or v == "" or v == 0 or v is False:
            continue
        lines.append(f"  {k}: <code>{escape(str(v))}</code>")
    return "\n".join(lines)


def _cmd_stats(_arg: str) -> str:
    m = _master()
    if not len(m):
        return "no master table."
    lines = ["<b>Master stats</b>", f"total rows: {len(m):,}"]
    if "chain" in m.columns:
        for chain, cnt in m["chain"].value_counts().items():
            lines.append(f"  {escape(str(chain))}: {int(cnt):,}")
    if "actionability" in m.columns:
        lines.append(f"top actionability: {float(m['actionability'].max()):.1f}")
    if "has_exchange_anchor" in m.columns:
        lines.append(f"exchange-anchored: {int(m['has_exchange_anchor'].sum()):,}")
    if "is_bridge_wallet" in m.columns:
        lines.append(f"bridge wallets:    {int(m['is_bridge_wallet'].sum()):,}")
    if "hits_interesting_country" in m.columns:
        lines.append(f"geo hits:          {int(m['hits_interesting_country'].sum()):,}")
    return "\n".join(lines)


def _cmd_report(_arg: str) -> str:
    """Build report.pdf from report.html and send it + the master CSV as
    Telegram documents. Returns a short status message that the poll loop
    then sends as a normal text reply."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    # 1) PDF
    pdf_status = ""
    try:
        from .report_pdf import build_pdf
        pdf_path = build_pdf()
        r = send_telegram_document(
            pdf_path,
            caption=("<b>AML Investigator - full report</b>\n"
                     "Research leads only, not findings of guilt."),
            chat_id=chat_id)
        pdf_status = ("PDF sent (%d KB)" % (pdf_path.stat().st_size // 1024)
                      if r.get("sent") else f"PDF failed: {r.get('reason')}")
    except FileNotFoundError:
        pdf_status = "PDF skipped: report.html not on repo (run cell 13.6 and push)"
    except ImportError as e:
        pdf_status = f"PDF skipped: {e}"
    except Exception as e:
        pdf_status = f"PDF failed: {type(e).__name__}: {e}"

    # 2) CSV
    csv_status = ""
    if C.MASTER_CSV.exists():
        r = send_telegram_document(
            C.MASTER_CSV,
            caption=("<b>Master CSV</b>\n"
                     "Full ranked pool of suspicious wallets for follow-up "
                     "investigation."),
            chat_id=chat_id)
        csv_status = ("CSV sent (%d KB)" % (C.MASTER_CSV.stat().st_size // 1024)
                      if r.get("sent") else f"CSV failed: {r.get('reason')}")
    else:
        csv_status = "CSV skipped: no master on repo"

    return f"<b>/report</b>\n{pdf_status}\n{csv_status}"


_HANDLERS = {
    "/start":  lambda _a: _COMMANDS_HELP,
    "/help":   lambda _a: _COMMANDS_HELP,
    "/report": _cmd_report,
    "/scan":   _cmd_scan,
    "/top":    _cmd_top,
    "/wallet": _cmd_wallet,
    "/stats":  _cmd_stats,
}


def handle_command(text: str) -> str:
    text = text.strip()
    if not text.startswith("/"):
        return _COMMANDS_HELP
    cmd, _, args = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()   # strip @BotName in group chats
    fn = _HANDLERS.get(cmd)
    if fn is None:
        return f"unknown command {escape(cmd)}\n\n" + _COMMANDS_HELP
    try:
        return fn(args)
    except Exception as e:
        return f"error handling {escape(cmd)}: {type(e).__name__}: {e}"


def poll_once(long_poll_seconds: int = 0) -> dict:
    """One Telegram getUpdates -> handler dispatch cycle.

    long_poll_seconds > 0 turns this into a true LONG POLL: the request blocks
    server-side up to that many seconds waiting for a message, so latency
    between "user sends /report" and "bot replies" collapses from ~cron-interval
    to sub-second while the workflow is alive.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    expected_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not expected_chat:
        print("[bot] TELEGRAM secrets missing; nothing to poll.")
        return {"processed": 0, "reason": "no_credentials"}

    updates = _api(token, "getUpdates", {"timeout": str(int(long_poll_seconds))})
    if not updates.get("ok"):
        print(f"[bot] getUpdates failed: {updates}")
        return {"processed": 0, "reason": updates.get("description", "getUpdates_failed")}

    events = updates.get("result") or []
    if not events:
        return {"processed": 0, "ignored": 0}

    processed = 0
    ignored = 0
    max_id = 0
    for u in events:
        max_id = max(max_id, int(u["update_id"]))
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != expected_chat:
            ignored += 1
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        reply = handle_command(text)
        send_telegram(reply, chat_id=chat_id)
        processed += 1

    # Acknowledge - Telegram drops these updates from future getUpdates calls.
    _api(token, "getUpdates", {"offset": str(max_id + 1), "timeout": "0"})
    print(f"[bot] processed {processed} command(s); ignored {ignored} from other chats.")
    return {"processed": processed, "ignored": ignored}


def poll_loop(duration_seconds: int = 540, long_poll_seconds: int = 25) -> dict:
    """Long-poll Telegram in a tight loop for `duration_seconds` total.

    GitHub Actions throttles a `*/5` cron on public repos to roughly once per
    hour (empirically observed). One short poll per invocation therefore covers
    a 1-second window every 60 minutes - useless. This loop stays open for the
    whole workflow lifetime, doing 25-second long polls, so a user command sent
    at any time during the run gets a sub-second reply.
    """
    import time
    # Bail out immediately when creds are missing - a dry-run loop that spins
    # for 9 minutes printing "missing" once per iteration is pure noise.
    if not (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            and os.environ.get("TELEGRAM_CHAT_ID", "").strip()):
        print("[bot] TELEGRAM secrets missing; loop aborted (dry run).")
        return {"processed": 0, "reason": "no_credentials"}

    end = time.time() + max(1, int(duration_seconds))
    total = {"processed": 0, "ignored": 0}
    print(f"[bot] entering poll loop for ~{duration_seconds}s (long-poll {long_poll_seconds}s per call)")
    consecutive_errors = 0
    while time.time() < end:
        try:
            r = poll_once(long_poll_seconds=long_poll_seconds)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            backoff = min(30, 2 * consecutive_errors)
            print(f"[bot] poll_once error #{consecutive_errors}: {type(e).__name__}: {e}; "
                  f"sleeping {backoff}s and retrying")
            time.sleep(backoff)
            continue
        # If getUpdates itself fails (e.g. temporary API error), sleep before
        # retrying so we don't hammer Telegram at 1000 Hz.
        if r.get("reason") and r["reason"] not in ("", "no_credentials"):
            time.sleep(3)
        total["processed"] += r.get("processed", 0)
        total["ignored"] += r.get("ignored", 0)
    print(f"[bot] loop finished. total processed={total['processed']} ignored={total['ignored']}")
    return total


if __name__ == "__main__":  # pragma: no cover
    duration = int(os.environ.get("BOT_LOOP_SECONDS", "540"))   # 9 min default
    long_poll = int(os.environ.get("BOT_LONG_POLL_SECONDS", "25"))
    if duration > 0:
        poll_loop(duration_seconds=duration, long_poll_seconds=long_poll)
    else:
        poll_once()
    sys.exit(0)
