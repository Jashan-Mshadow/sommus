"""Reading PDFs as text, including scanned ones.

Course PDFs on LEARN are often scans with no text layer, which is why reading them
through the browser returned only page furniture. Text pages are extracted directly;
pages without a text layer go through macOS's own Vision OCR — offline, no API cost.
"""

from __future__ import annotations

import re
from pathlib import Path

from sommus.nodes.laptop.macos import ActionError

MAX_CHARS = 30_000
OCR_DPI = 200
TEXT_THRESHOLD = 60  # fewer characters than this on a page means it's a scan


def parse_pages(spec: str | None, total: int) -> list[int]:
    """ "1-5", "3", "2,4,7" → zero-based page indexes, clamped to the document."""
    if not spec:
        return list(range(total))
    wanted: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if match := re.fullmatch(r"(\d+)-(\d+)", part):
            start, end = (int(g) for g in match.groups())
            wanted.extend(range(start, end + 1))
        elif part.isdigit():
            wanted.append(int(part))
        else:
            raise ActionError(f"Can't read page range '{spec}'. Use forms like 1-5, 3, or 2,4,7.")
    return [p - 1 for p in wanted if 1 <= p <= total]


def ocr_image(png: bytes) -> str:
    """Recognise text in an image using macOS Vision."""
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(0)  # accurate rather than fast
    request.setUsesLanguageCorrection_(True)
    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise ActionError(f"OCR failed: {error}")
    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates:
            lines.append(candidates[0].string())
    return "\n".join(lines)


def read(path: str, pages: str | None = None, ocr: bool = True) -> tuple[str, int, int]:
    """Return (text, pages read, pages OCR'd)."""
    import pymupdf

    file = Path(path).expanduser()
    if not file.exists():
        raise ActionError(f"No file at '{path}'.")
    try:
        document = pymupdf.open(file)
    except Exception as e:
        raise ActionError(f"Couldn't open '{file.name}' as a PDF: {e}") from e

    with document:
        indexes = parse_pages(pages, document.page_count)
        if not indexes:
            raise ActionError(f"'{file.name}' has {document.page_count} pages; '{pages}' selects none of them.")
        chunks, scanned = [], 0
        for index in indexes:
            page = document[index]
            text = page.get_text().strip()
            if len(text) < TEXT_THRESHOLD and ocr:
                png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
                text = ocr_image(png).strip()
                scanned += 1
            chunks.append(f"--- page {index + 1} ---\n{text}" if text else f"--- page {index + 1} --- (blank)")
            if sum(len(c) for c in chunks) > MAX_CHARS:
                chunks.append("[truncated — ask for a narrower page range]")
                break
        return "\n\n".join(chunks), len(indexes), scanned
