"""PDF reading: page ranges, text extraction, and the OCR fallback."""

import pymupdf
import pytest

from sommus.nodes.laptop import pdf
from sommus.nodes.laptop.macos import ActionError


@pytest.fixture
def text_pdf(tmp_path):
    document = pymupdf.open()
    for number in (1, 2, 3):
        page = document.new_page()
        page.insert_text((72, 72), f"Lecture {number}: conic sections and functions")
        page.insert_text((72, 96), "Enough text on the page that it counts as a real text layer, not a scan.")
    path = tmp_path / "lecture.pdf"
    document.save(path)
    return path


def test_page_ranges():
    assert pdf.parse_pages("1-3", 10) == [0, 1, 2]
    assert pdf.parse_pages("2,4", 10) == [1, 3]
    assert pdf.parse_pages("3", 10) == [2]
    assert pdf.parse_pages(None, 3) == [0, 1, 2]
    assert pdf.parse_pages("5-9", 6) == [4, 5]  # clamped to the document


def test_a_nonsense_range_is_rejected():
    with pytest.raises(ActionError, match="page range"):
        pdf.parse_pages("first two", 10)


def test_text_pages_are_read_without_ocr(text_pdf):
    text, pages, scanned = pdf.read(str(text_pdf))
    assert pages == 3 and scanned == 0
    assert "Lecture 2: conic sections" in text
    assert "--- page 3 ---" in text


def test_only_the_requested_pages_are_read(text_pdf):
    text, pages, _ = pdf.read(str(text_pdf), "2")
    assert pages == 1 and "Lecture 2" in text and "Lecture 1" not in text


def test_a_scanned_page_falls_back_to_ocr(tmp_path):
    """A page rendered as an image has no text layer — the LEARN case."""
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 100), "INSTANTANEOUS VELOCITY", fontsize=28)
    image = page.get_pixmap(dpi=150)

    scan = pymupdf.open()
    scanned_page = scan.new_page(width=image.width, height=image.height)
    scanned_page.insert_image(scanned_page.rect, stream=image.tobytes("png"))
    path = tmp_path / "scan.pdf"
    scan.save(path)

    text, _, scanned = pdf.read(str(path))
    assert scanned == 1
    assert "INSTANTANEOUS" in text.upper()


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ActionError, match="No file at"):
        pdf.read(str(tmp_path / "nope.pdf"))


def test_a_non_pdf_is_reported_clearly(tmp_path):
    fake = tmp_path / "notes.pdf"
    fake.write_text("this is not a pdf")
    with pytest.raises(ActionError, match="as a PDF"):
        pdf.read(str(fake))
