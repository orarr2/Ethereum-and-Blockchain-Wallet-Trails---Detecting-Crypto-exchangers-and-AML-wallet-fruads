"""
report_pdf - wrap the notebook-produced report.html as a PDF.

The notebook's section 13.6 writes a self-contained `report.html` (no JS, no
external assets, no external fonts) via `report_html.build_report(...)`. That
report already has the KPI header, top-50 actionable leads, bridge wallets,
country breakdown, exchange anchors, co-funding clusters, and NBCTF matches.

This module does NOT re-author any of that content. It converts the existing
HTML to PDF with weasyprint (which renders the report's dark theme, KPI
cards, and tables faithfully). The delivery layer then attaches the PDF to
Telegram along with the master CSV.

CI installs the required cairo/pango system libraries; the workflow YAML
handles that. Locally on Linux you can `pip install weasyprint` and use as-is.
"""
from __future__ import annotations

from pathlib import Path

from . import config as C


def build_pdf(html_path: Path | None = None, out_path: Path | None = None) -> Path:
    """Convert `report.html` -> PDF. Returns the written path.

    Defaults: read <project root>/report.html, write to
    investigator/outputs/report.pdf.
    """
    html_path = Path(html_path) if html_path else (C.PROJECT_ROOT / "report.html")
    out_path = Path(out_path) if out_path else (C.OUTPUTS_DIR / "report.pdf")
    if not html_path.exists():
        raise FileNotFoundError(
            f"source report.html not found at {html_path}. "
            "Run the notebook cell 13.6 (report_html.build_report) and push."
        )
    try:
        from weasyprint import HTML
    except ImportError as e:
        raise ImportError(
            "weasyprint is not installed. CI installs it plus its cairo/pango "
            "system libs automatically. Locally on Linux: `pip install weasyprint`."
        ) from e
    out_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(str(html_path), base_url=str(html_path.parent)).write_pdf(str(out_path))
    print(f"[report_pdf] {html_path.name} -> {out_path} "
          f"({out_path.stat().st_size:,} bytes)")
    return out_path


if __name__ == "__main__":  # pragma: no cover
    build_pdf()
