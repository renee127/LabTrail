"""Text-layer extraction for native PDFs, and report metadata suggestions."""

from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_workflow import auto_reject_identification_lines, line_text, parse_report_date, suggest_report_metadata
from models import CandidateExtraction, CandidateExtractionResult, ExtractionPageInventory


NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def _two_column_pdf(rotation: int = 0) -> bytes:
    """A report laid out like many real ones: an image letterhead, the patient
    block on the left and dates on the right of the same rows, then a table."""
    banner = Image.new("RGB", (1400, 160), "white")
    ImageDraw.Draw(banner).text((40, 40), "COASTAL IMAGING LAB", fill="black",
                                font=ImageFont.load_default(size=64))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    if rotation:
        pdf.setPageRotation(rotation)
    pdf.drawImage(ImageReader(banner), 40, 700, width=530, height=60)
    rows = [("Mr. Jordan Sample", "Registered on : 16/10/2024 09:10 AM"),
            ("Age / Sex : 41 YRS / F", "Collected on : 17/10/2024"),
            ("Reg. no. : 2214", "Reported on : 18/10/2024 05:33 PM")]
    y = 660
    for left, right in rows:
        pdf.drawString(50, y, left)
        pdf.drawString(330, y, right)
        y -= 16
    for line in ("TOTAL CHOLESTEROL 180 mg/dl 125 - 200", "HDL CHOLESTEROL 55 mg/dl 35 - 80"):
        y -= 20
        pdf.drawString(50, y, line)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _extract(pdf: bytes) -> CandidateExtractionResult:
    return extract_document_ocr_line_candidates(
        source_bytes=pdf, mime_type="application/pdf", report_id=uuid4(),
        extraction_job_id=uuid4(), extracted_at=NOW,
    )


def test_native_pdf_lines_come_from_the_text_layer_one_column_at_a_time() -> None:
    result = _extract(_two_column_pdf())
    text_lines = {line_text(c) for c in result.candidates if c.model_identifier == "pdf-text-layer"}
    assert "Mr. Jordan Sample" in text_lines
    assert "Collected on : 17/10/2024" in text_lines
    assert "TOTAL CHOLESTEROL 180 mg/dl 125-200" in text_lines
    # Side-by-side fields sharing a baseline are split at the gap before "Label :".
    assert "Registered on : 16/10/2024 09:10 AM" in text_lines
    # Rows are renumbered top to bottom and the inventory lists every line.
    assert [c.row_index for c in result.candidates] == list(range(len(result.candidates)))
    assert result.page_inventories[0].result_row_indices == tuple(range(len(result.candidates)))
    assert result.page_inventories[0].inventory_status == "INCOMPLETE"


def test_two_column_report_metadata_is_found() -> None:
    result = _extract(_two_column_pdf())
    meta = suggest_report_metadata(result)
    assert meta.patient_name == "Jordan Sample"
    assert meta.collected == date(2024, 10, 17)
    rejected = auto_reject_identification_lines(result)
    reasons = {line_text(c): rejected.get(c.candidate_id) for c in result.candidates}
    assert reasons["Mr. Jordan Sample"] == "name"
    assert reasons["Age / Sex : 41 YRS"] == "age / sex"  # parser keeps name, value, unit
    assert reasons["TOTAL CHOLESTEROL 180 mg/dl 125-200"] is None


def test_ocr_keeps_image_only_text_and_drops_text_layer_duplicates(monkeypatch) -> None:
    """Text drawn as an image (letterheads) has no text layer; OCR must keep it so
    it can be masked. OCR lines repeating text-layer lines on the same row are dropped."""
    import labtrail_extraction

    real_ocr = labtrail_extraction.extract_ocr_line_candidates

    def fixed_ocr(**kwargs) -> CandidateExtractionResult:
        template = real_ocr(**kwargs).candidates[0]
        lines = (
            ("COASTAL IMAGING LAB", (0.07, 0.05, 0.6, 0.09)),            # image-only text
            ("Mr. Jordan Sample Registered on", (0.07, 0.155, 0.86, 0.17)),  # both columns merged
        )
        candidates = tuple(
            replace(template, candidate_id=uuid4(), row_index=i, raw_analyte_name=text,
                    raw_value=None, raw_unit=None, reference_interval=None, bounding_box=box)
            for i, (text, box) in enumerate(lines)
        )
        return CandidateExtractionResult(
            template.report_id, template.extraction_job_id, candidates,
            (ExtractionPageInventory(kwargs["page_number"], "a" * 64, (0, 1), "INCOMPLETE"),),
        )

    monkeypatch.setattr(labtrail_extraction, "extract_ocr_line_candidates", fixed_ocr)
    result = _extract(_two_column_pdf())
    ocr_lines = [line_text(c) for c in result.candidates if c.model_identifier != "pdf-text-layer"]
    assert ocr_lines == ["COASTAL IMAGING LAB"]
    assert line_text(result.candidates[0]) == "COASTAL IMAGING LAB"  # sorted top to bottom


def test_rotated_pages_fall_back_to_ocr_so_masks_line_up() -> None:
    result = _extract(_two_column_pdf(rotation=90))
    assert all(c.model_identifier != "pdf-text-layer" for c in result.candidates)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-02-10", date(2026, 2, 10)),
        ("17/10/2024 05:32 PM", date(2024, 10, 17)),   # day first, unambiguous
        ("10/17/2024", date(2024, 10, 17)),            # month first, unambiguous
        ("17.10.2024", date(2024, 10, 17)),
        ("07/07/2024", date(2024, 7, 7)),
        ("17-Oct-2024", date(2024, 10, 17)),
        ("17 October 2024", date(2024, 10, 17)),
        ("Oct 17, 2024", date(2024, 10, 17)),
        ("05/06/2024", None),                          # 5 June or May 6: not guessed
        ("31/02/2024", None),                          # impossible date
        ("pending", None),
    ],
)
def test_report_dates(text, expected) -> None:
    assert parse_report_date(text) == expected


def _result_from_lines(lines: list[str]) -> CandidateExtractionResult:
    report_id, job = uuid4(), uuid4()
    candidates = tuple(
        CandidateExtraction(
            uuid4(), report_id, job, i, text, "UNMAPPED", None, None, None, None, None, None, 1,
            (0.05, 0.02 + i * 0.03, 0.6, 0.04 + i * 0.03), True,
            {"analyte": 0.9, "value": 0.0, "unit": 0.0, "region": 0.9}, "t", "1", "0" * 64, NOW,
            "MANDATORY_EXCEPTION_REVIEW", "CANDIDATE",
        )
        for i, text in enumerate(lines)
    )
    return CandidateExtractionResult(
        report_id, job, candidates,
        (ExtractionPageInventory(1, "a" * 64, tuple(range(len(lines))), "INCOMPLETE"),),
    )


@pytest.mark.parametrize(
    ("lines", "name"),
    [
        (["Patient: Jordan Sample"], "Jordan Sample"),
        (["Patient Name : Jane Q. Doe"], "Jane Q. Doe"),
        (["Name: Jane Doe Age: 40 Sex: F"], "Jane Doe"),
        (["Mrs. Ana Lopez"], "Ana Lopez"),
        (["Mr. Saubhik Bhaumik UNUM <> Oe"], "Saubhik Bhaumik"),    # OCR merged a barcode
        (["MR. JOHN DOE"], "JOHN DOE"),
        # A labelled name wins over a staff signature with a title.
        (["Lab results", "Patient Name: Jane Doe", "Mr. Sachin Sharma"], "Jane Doe"),
        # Without a label, the earliest title line (top of page) is used.
        (["Mr. Saubhik Bhaumik", "TOTAL CHOLESTEROL", "Mr. Sachin Sharma"], "Saubhik Bhaumik"),
        (["Dr. A. K. Asthana"], None),                               # clinicians are not patients
        (["Glucose 104 mg/dL"], None),
    ],
)
def test_patient_name_suggestions(lines, name) -> None:
    assert suggest_report_metadata(_result_from_lines(lines)).patient_name == name


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Collected: 2026-02-10", date(2026, 2, 10)),
        ("Collected on : 17/10/2024", date(2024, 10, 17)),
        ("Collection Date: Oct 17, 2024", date(2024, 10, 17)),
        ("Date Collected 17-Oct-2024 08:15", date(2024, 10, 17)),
        ("Specimen collected: 2024-10-17", date(2024, 10, 17)),
        ("Received on : 17/10/2024", None),                         # not the collection date
        ("Collected on : 05/06/2024", None),                        # ambiguous order
    ],
)
def test_collection_date_suggestions(line, expected) -> None:
    assert suggest_report_metadata(_result_from_lines([line])).collected == expected
