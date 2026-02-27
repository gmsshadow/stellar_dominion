"""
Stellar Dominion - PDF Report Export

Renders plaintext turn reports to A4 portrait PDFs with:
- Monospace font throughout
- Map blocks rendered at a smaller font size so 31x31 grids fit
- Map block left-whitespace trimmed for cleaner layout
- Body text at a comfortable reading size

Based on export_report_pdf.py by ChatGPT, integrated into the engine.
"""

from pathlib import Path

try:
    from reportlab.platypus import SimpleDocTemplate, Preformatted
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


def is_available():
    """Check if PDF export is available (reportlab installed)."""
    return HAS_REPORTLAB


def _register_monospace_font():
    """
    Prefer DejaVu Sans Mono (good Unicode coverage).
    Fallback to built-in Courier if not present.
    """
    dejavu = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    if Path(dejavu).exists():
        font_name = "DejaVuSansMono"
        pdfmetrics.registerFont(TTFont(font_name, dejavu))
        return font_name
    return "Courier"


def _trim_common_left_indent(lines):
    """Remove common minimum leading spaces from non-blank lines."""
    indents = []
    for ln in lines:
        if ln.strip() == "":
            continue
        indents.append(len(ln) - len(ln.lstrip(" ")))
    if not indents:
        return lines
    min_indent = min(indents)
    if min_indent <= 0:
        return lines
    return [ln[min_indent:] if ln.strip() != "" else ln for ln in lines]


def _split_body_and_maps(text):
    """
    Split report text into segments of (text, kind) where kind is 'body' or 'map'.
    Map blocks run from 'Surface Map:' to just before 'Planetary Data:'.
    System maps run from the grid header line to the last grid row.
    """
    lines = text.splitlines(keepends=True)
    segments = []
    buf = []
    i = 0

    while i < len(lines):
        buf.append(lines[i])

        # Surface map detection (planet terrain maps)
        if "Surface Map:" in lines[i]:
            segments.append(("".join(buf), "body"))
            buf = []
            i += 1

            map_block = []
            while i < len(lines) and "Planetary Data:" not in lines[i]:
                map_block.append(lines[i])
                i += 1

            map_block = _trim_common_left_indent(map_block)
            if map_block:
                segments.append(("".join(map_block), "map"))
            continue

        # System map detection (25x25 grid maps from SYSTEMSCAN)
        stripped = lines[i].strip()
        if stripped.startswith("A  B  C  D  E"):
            # Flush body up to but not including this line
            if len(buf) > 1:
                segments.append(("".join(buf[:-1]), "body"))
            map_block = [buf[-1]]  # Start with the header line
            i += 1
            while i < len(lines):
                row_stripped = lines[i].strip()
                # Grid rows start with 2-digit number
                if row_stripped and row_stripped[:2].isdigit():
                    map_block.append(lines[i])
                    i += 1
                else:
                    break
            map_block = _trim_common_left_indent(map_block)
            if map_block:
                segments.append(("".join(map_block), "map"))
            buf = []
            continue

        i += 1

    if buf:
        segments.append(("".join(buf), "body"))

    return segments


def text_to_pdf(text, output_path, font_size=10.0, map_font_size=7.0, margin_mm=7.0):
    """
    Render a plaintext report string to an A4 PDF file.

    Args:
        text: The full report text (monospace formatted)
        output_path: Path for the output PDF file
        font_size: Font size for body text (default 10pt)
        map_font_size: Font size for map blocks (default 7pt)
        margin_mm: Page margin in millimetres (default 7mm)

    Returns:
        Path to the created PDF, or None if reportlab not available.
    """
    if not HAS_REPORTLAB:
        return None

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    font_name = _register_monospace_font()

    mm_to_pt = 72.0 / 25.4
    margin_pt = margin_mm * mm_to_pt

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=margin_pt,
        rightMargin=margin_pt,
        topMargin=margin_pt,
        bottomMargin=margin_pt,
    )

    body_style = ParagraphStyle(
        "Body",
        fontName=font_name,
        fontSize=font_size,
        leading=font_size,
    )
    map_style = ParagraphStyle(
        "Map",
        fontName=font_name,
        fontSize=map_font_size,
        leading=map_font_size,
    )

    segments = _split_body_and_maps(text)

    story = []
    for seg_text, kind in segments:
        style = map_style if kind == "map" else body_style
        story.append(Preformatted(seg_text, style, maxLineLength=100000))

    doc.build(story)
    return output_path


def report_file_to_pdf(txt_path, pdf_path=None, **kwargs):
    """
    Convert a .txt report file to PDF.

    If pdf_path is None, uses the same name with .pdf extension.
    Returns the PDF path, or None if reportlab not available.
    """
    txt_path = Path(txt_path)
    if pdf_path is None:
        pdf_path = txt_path.with_suffix('.pdf')

    text = txt_path.read_text(encoding='utf-8')
    return text_to_pdf(text, pdf_path, **kwargs)
