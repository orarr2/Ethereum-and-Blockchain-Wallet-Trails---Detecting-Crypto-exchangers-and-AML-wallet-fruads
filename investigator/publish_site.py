"""
Build a static GitHub Pages site from the generated dossiers.

Converts each `outputs/dossiers/<chain>/<addr>.md` into a styled, self-contained
HTML page and writes an index that links them all, into a Pages-ready `site/`
directory. The Telegram/email digest links to this site so the full dossier
(with its evidence table and reasoning trace) is one tap away on the phone.

The markdown converter handles exactly the constructs `dossier_writer` emits
(h1/h2, blockquote, tables, fenced code, bold/italic, links, hr) - no external
markdown dependency required.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import config as C

_CSS = """
:root{color-scheme:light dark;}
body{font:15px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
 max-width:900px;margin:0 auto;padding:24px;background:#0e1116;color:#e6edf3;}
a{color:#58a6ff;text-decoration:none;} a:hover{text-decoration:underline;}
h1{font-size:22px;border-bottom:1px solid #2d333b;padding-bottom:8px;}
h2{font-size:17px;color:#58a6ff;margin-top:28px;}
blockquote{margin:6px 0;padding:4px 12px;border-left:3px solid #d29922;
 color:#d29922;background:rgba(210,153,34,.08);font-size:13px;}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px;overflow-x:auto;display:block;}
th,td{border:1px solid #2d333b;padding:6px 10px;text-align:left;white-space:nowrap;}
th{background:#161b22;color:#8b949e;text-transform:uppercase;font-size:11px;}
code{background:#161b22;padding:1px 5px;border-radius:4px;font-size:12.5px;}
pre{background:#161b22;border:1px solid #2d333b;border-radius:8px;padding:12px;
 overflow-x:auto;font-size:12px;} pre code{background:none;padding:0;}
.muted{color:#8b949e;font-size:12px;} hr{border:none;border-top:1px solid #2d333b;margin:24px 0;}
.back{display:inline-block;margin-bottom:16px;font-size:13px;}
.card{background:#161b22;border:1px solid #2d333b;border-radius:8px;padding:10px 14px;margin:8px 0;}
"""


def _inline(text: str) -> str:
    """Escape then re-apply inline markdown (links, bold, italic, code)."""
    # protect inline code first
    codes = []
    def _stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes)-1}\x00"
    text = re.sub(r"`([^`]+)`", _stash, text)
    text = html.escape(text)
    # links [t](u)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                  lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))])}</code>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue
        if line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>"); i += 1; continue
        if line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>"); i += 1; continue
        if line.startswith("> "):
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>"); i += 1; continue
        if line.strip() == "---":
            out.append("<hr>"); i += 1; continue
        if line.lstrip().startswith("|") and "|" in line:
            tbl = []
            while i < n and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i]); i += 1
            out.append(_table(tbl)); continue
        if not line.strip():
            i += 1; continue
        # paragraph (gather consecutive non-blank, non-special lines)
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#|>|\||```|---)", lines[i].lstrip()):
            para.append(lines[i]); i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def _table(rows: list) -> str:
    def cells(r):
        return [c.strip() for c in r.strip().strip("|").split("|")]
    parsed = [cells(r) for r in rows]
    # drop the |---|---| separator row
    body = [r for r in parsed if not all(set(c) <= set("-: ") for c in r)]
    if not body:
        return ""
    head, *rest = body
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _page(title: str, body_html: str, back: str | None = None) -> str:
    back_link = f'<a class="back" href="{back}">&larr; all dossiers</a>' if back else ""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
            f"{back_link}{body_html}</body></html>")


def build(dossiers_dir: Path | None = None, site_dir: Path | None = None) -> dict:
    dossiers_dir = Path(dossiers_dir) if dossiers_dir else C.DOSSIERS_DIR
    site_dir = Path(site_dir) if site_dir else (C.OUTPUTS_DIR / "site")
    site_dir.mkdir(parents=True, exist_ok=True)

    md_files = sorted(dossiers_dir.glob("*/*.md"))
    entries = []
    for md_path in md_files:
        chain = md_path.parent.name
        addr = md_path.stem
        rel = f"{chain}/{addr}.html"
        (site_dir / chain).mkdir(parents=True, exist_ok=True)
        body = md_to_html(md_path.read_text(encoding="utf-8"))
        (site_dir / rel).write_text(_page(f"{addr} ({chain})", body, back="../index.html"),
                                    encoding="utf-8")
        entries.append({"chain": chain, "address": addr, "href": rel})

    # index grouped by chain
    meta = {}
    if C.DOSSIER_INDEX_PATH.exists():
        try:
            meta = json.loads(C.DOSSIER_INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    gen = meta.get("generated", "")
    demo_banner = ('<div style="background:#3d2b00;border:1px solid #d29922;color:#f0c674;'
                   'padding:8px 12px;border-radius:6px;margin:8px 0;font-size:13px;">'
                   'DEMO DATA - built from the committed sample table (no real BigQuery run). '
                   'Add BQ + LLM keys for live leads.</div>') if meta.get("demo") else ""
    sections = []
    by_chain: dict = {}
    for e in entries:
        by_chain.setdefault(e["chain"], []).append(e)
    for chain in sorted(by_chain):
        cards = "".join(
            f'<div class="card"><a href="{e["href"]}"><code>{html.escape(e["address"])}</code></a></div>'
            for e in by_chain[chain])
        sections.append(f"<h2>{html.escape(chain)} ({len(by_chain[chain])})</h2>{cards}")
    body = (f"<h1>AML Investigator dossiers</h1>{demo_banner}"
            f"<p class='muted'>{'Generated ' + html.escape(gen) + '. ' if gen else ''}"
            f"{len(entries)} dossiers. Research leads only, not findings of guilt.</p>"
            + ("".join(sections) if sections else "<p class='muted'>No dossiers yet.</p>"))
    (site_dir / "index.html").write_text(_page("AML Investigator dossiers", body), encoding="utf-8")

    # .nojekyll so Pages serves the files as-is
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"[publish_site] wrote {len(entries)} dossier pages + index -> {site_dir}")
    return {"site_dir": str(site_dir), "n_pages": len(entries)}


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(build(), indent=2))
