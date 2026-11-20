"""Campaign reports as downloadable PDF or Word (.docx) files.

Built from sent_log.csv, the same log the sender writes. Test sends
(--test-to) are never logged, so they never appear in a report.

Usage:
    python report.py                          # all campaigns, PDF
    python report.py --campaign spring-launch --format docx
    python report.py --format pdf -o report.pdf
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import bulkmail as bm

INK = (24, 28, 36)
MUTED = (96, 104, 116)
RULE = (214, 218, 224)
HEAD_FILL = (238, 241, 245)
OK = (22, 120, 72)
BAD = (176, 40, 40)


# ---------------------------------------------------------------- data

@dataclass
class Campaign:
    name: str
    first: str = ""
    last: str = ""
    # email -> (timestamp, status, detail) of the most recent attempt
    results: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    attempts: int = 0

    @property
    def sent(self) -> list[tuple[str, str, str, str]]:
        return sorted((e, *r) for e, r in self.results.items() if r[1] == "sent")

    @property
    def failed(self) -> list[tuple[str, str, str, str]]:
        return sorted((e, *r) for e, r in self.results.items() if r[1] != "sent")

    @property
    def rate(self) -> float:
        return 100.0 * len(self.sent) / len(self.results) if self.results else 0.0


def load_campaigns(campaign: str | None = None, log: Path | None = None) -> list[Campaign]:
    """Read the sent log. A later attempt for the same address replaces an earlier one,
    so an address that failed and was then sent on a re-run counts as sent."""
    log = log or bm.SENT_LOG
    if not log.exists():
        return []
    out: dict[str, Campaign] = {}
    with log.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = r.get("campaign", "")
            if campaign and name != campaign:
                continue
            c = out.setdefault(name, Campaign(name))
            ts = r.get("timestamp", "")
            c.first = min(c.first or ts, ts)
            c.last = max(c.last, ts)
            c.attempts += 1
            c.results[r.get("email", "").lower()] = (ts, r.get("status", ""), r.get("detail", ""))
    return sorted(out.values(), key=lambda c: c.last, reverse=True)


def campaign_names(log: Path | None = None) -> list[str]:
    return [c.name for c in load_campaigns(log=log)]


def nice_time(ts: str) -> str:
    try:
        return dt.datetime.fromisoformat(ts).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return ts


def filename(campaign: str | None, fmt: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", campaign or "all-campaigns").strip("-") or "campaign"
    return f"report-{slug}-{dt.date.today().isoformat()}.{fmt}"


def meta() -> list[tuple[str, str]]:
    name, email = bm.env("SENDER_NAME"), bm.env("SENDER_EMAIL") or bm.env("SMTP_USER")
    sender = f"{name} <{email}>" if name and email else (email or name or "-")
    return [("Sender", sender), ("Replies to", bm.env("REPLY_TO") or email or "-"),
            ("Generated", dt.datetime.now().strftime("%d %b %Y, %H:%M"))]


def summary_rows(campaigns: list[Campaign]) -> list[list[str]]:
    return [[c.name, nice_time(c.first), nice_time(c.last), str(len(c.results)),
             str(len(c.sent)), str(len(c.failed)), f"{c.rate:.1f}%"] for c in campaigns]


SUMMARY_HEAD = ["Campaign", "First send", "Last send", "Recipients", "Delivered", "Failed", "Success"]


# ---------------------------------------------------------------- Word

def build_docx(campaigns: list[Campaign], title: str) -> bytes:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2)
        s.top_margin = s.bottom_margin = Cm(1.8)
    base = doc.styles["Normal"]
    base.font.name, base.font.size = "Calibri", Pt(10)

    def shade(cell, rgb):
        tc = cell._tc.get_or_add_tcPr()
        el = OxmlElement("w:shd")
        el.set(qn("w:val"), "clear")
        el.set(qn("w:fill"), "%02X%02X%02X" % rgb)
        tc.append(el)

    def table(head: list[str], rows: list[list[str]], status_col: int | None = None):
        t = doc.add_table(rows=1, cols=len(head))
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, h in enumerate(head):
            cell = t.rows[0].cells[i]
            cell.text = ""
            run = cell.paragraphs[0].add_run(h)
            run.bold = True
            shade(cell, HEAD_FILL)
        for row in rows:
            cells = t.add_row().cells
            for i, v in enumerate(row):
                run = cells[i].paragraphs[0].add_run(v)
                if i == status_col:
                    run.font.color.rgb = RGBColor(*(OK if v == "sent" else BAD))
        # repeat header row on each page
        trpr = t.rows[0]._tr.get_or_add_trPr()
        hdr = OxmlElement("w:tblHeader")
        hdr.set(qn("w:val"), "true")
        trpr.append(hdr)
        doc.add_paragraph()

    doc.add_heading(title, level=0)
    for p in meta():
        para = doc.add_paragraph()
        para.paragraph_format.space_after = Pt(0)
        k = para.add_run(f"{p[0]}: ")
        k.bold = True
        para.add_run(p[1])
    doc.add_paragraph()

    if not campaigns:
        doc.add_paragraph("No real sends have been logged yet. Test sends are not recorded.")
    else:
        doc.add_heading("Summary", level=1)
        totals = (sum(len(c.results) for c in campaigns), sum(len(c.sent) for c in campaigns),
                  sum(len(c.failed) for c in campaigns))
        doc.add_paragraph(f"{len(campaigns)} campaign(s), {totals[0]} recipient(s): "
                          f"{totals[1]} delivered, {totals[2]} failed.")
        table(SUMMARY_HEAD, summary_rows(campaigns))

        for c in campaigns:
            doc.add_page_break()
            doc.add_heading(f"Campaign: {c.name}", level=1)
            doc.add_paragraph(
                f"Sent between {nice_time(c.first)} and {nice_time(c.last)}. "
                f"{len(c.results)} recipient(s), {len(c.sent)} delivered, {len(c.failed)} failed "
                f"({c.rate:.1f}% success, {c.attempts} attempt(s) in total).")
            if c.failed:
                doc.add_heading("Failed deliveries", level=2)
                table(["Email", "Time", "Reason"],
                      [[e, nice_time(ts), d or "-"] for e, ts, _, d in c.failed])
            doc.add_heading("All recipients", level=2)
            table(["Email", "Time", "Result"],
                  [[e, nice_time(ts), s] for e, ts, s, _ in sorted(c.sent + c.failed)], status_col=2)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

def _pdf_font(pdf) -> tuple[str, bool]:
    """Use a Unicode TrueType font when one is available so non-English names print;
    otherwise fall back to Helvetica (Latin-1 only)."""
    windir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [
        (windir / "arial.ttf", windir / "arialbd.ttf"),
        (windir / "segoeui.ttf", windir / "segoeuib.ttf"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/Library/Fonts/Arial Unicode.ttf"), Path("/Library/Fonts/Arial Unicode.ttf")),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            pdf.add_font("Body", "", str(regular))
            pdf.add_font("Body", "B", str(bold))
            return "Body", True
    return "Helvetica", False


def build_pdf(campaigns: list[Campaign], title: str) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    class Doc(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font(font, "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 6, f"{title}  |  Page {self.page_no()}/{{nb}}", align="C")

    pdf = Doc(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=18)
    font, unicode_ok = _pdf_font(pdf)
    clean = (lambda s: s) if unicode_ok else (lambda s: s.encode("latin-1", "replace").decode("latin-1"))
    pdf.alias_nb_pages()
    pdf.add_page()

    def heading(text: str, size: int, gap: float = 3):
        pdf.set_font(font, "B", size)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, size * 0.5, clean(text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(gap)

    def para(text: str, color=INK):
        pdf.set_font(font, "", 10)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 5.2, clean(text), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    def table(head: list[str], rows: list[list[str]], widths: tuple, status_col: int | None = None):
        pdf.set_font(font, "", 9)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*RULE)
        style = FontFace(emphasis="BOLD", fill_color=HEAD_FILL)
        with pdf.table(col_widths=widths, headings_style=style, line_height=5.5,
                       borders_layout="HORIZONTAL_LINES", text_align="LEFT",
                       padding=1.5) as t:
            hr = t.row()
            for h in head:
                hr.cell(clean(h))
            for row in rows:
                r = t.row()
                for i, v in enumerate(row):
                    if i == status_col:
                        r.cell(clean(v), style=FontFace(color=OK if v == "sent" else BAD, emphasis="BOLD"))
                    else:
                        r.cell(clean(v))
        pdf.ln(4)

    heading(title, 20, 2)
    pdf.set_font(font, "", 10)
    for k, v in meta():
        pdf.set_text_color(*MUTED)
        pdf.cell(24, 5.5, clean(k))
        pdf.set_text_color(*INK)
        pdf.cell(0, 5.5, clean(v), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_draw_color(*RULE)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    if not campaigns:
        para("No real sends have been logged yet. Test sends are not recorded.", MUTED)
    else:
        heading("Summary", 14)
        totals = (sum(len(c.results) for c in campaigns), sum(len(c.sent) for c in campaigns),
                  sum(len(c.failed) for c in campaigns))
        para(f"{len(campaigns)} campaign(s), {totals[0]} recipient(s): "
             f"{totals[1]} delivered, {totals[2]} failed.")
        table(SUMMARY_HEAD, summary_rows(campaigns), (30, 27, 27, 20, 20, 16, 18))

        for c in campaigns:
            pdf.add_page()
            heading(f"Campaign: {c.name}", 15)
            para(f"Sent between {nice_time(c.first)} and {nice_time(c.last)}. "
                 f"{len(c.results)} recipient(s), {len(c.sent)} delivered, {len(c.failed)} failed "
                 f"({c.rate:.1f}% success, {c.attempts} attempt(s) in total).")
            if c.failed:
                heading("Failed deliveries", 12, 2)
                table(["Email", "Time", "Reason"],
                      [[e, nice_time(ts), d or "-"] for e, ts, _, d in c.failed], (55, 32, 87))
            heading("All recipients", 12, 2)
            table(["Email", "Time", "Result"],
                  [[e, nice_time(ts), s] for e, ts, s, _ in sorted(c.sent + c.failed)],
                  (95, 50, 29), status_col=2)

    return bytes(pdf.output())


# ---------------------------------------------------------------- entry points

def build(fmt: str, campaign: str | None = None, log: Path | None = None) -> tuple[bytes, str]:
    """Return (file bytes, suggested filename) for 'pdf' or 'docx'."""
    if fmt not in ("pdf", "docx"):
        raise ValueError("Format must be 'pdf' or 'docx'.")
    campaigns = load_campaigns(campaign, log)
    if campaign and not campaigns:
        raise ValueError(f"No logged sends for campaign '{campaign}'.")
    title = f"Campaign report: {campaign}" if campaign else "Email campaign report"
    data = (build_pdf if fmt == "pdf" else build_docx)(campaigns, title)
    return data, filename(campaign, fmt)


def main() -> None:
    bm.load_dotenv()
    p = argparse.ArgumentParser(description="Create a campaign report from sent_log.csv.")
    p.add_argument("--campaign", help="Only this campaign (default: all)")
    p.add_argument("--format", choices=["pdf", "docx"], default="pdf")
    p.add_argument("-o", "--output", type=Path, help="Output file (default: reports/<auto name>)")
    p.add_argument("--log", type=Path, help=argparse.SUPPRESS)
    args = p.parse_args()
    try:
        data, name = build(args.format, args.campaign, args.log)
    except ValueError as e:
        raise SystemExit(str(e))
    out = args.output or (bm.HERE / "reports" / name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
