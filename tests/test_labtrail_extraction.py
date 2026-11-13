from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from uuid import UUID

import pytest
from PIL import Image, ImageDraw, ImageFont

from labtrail_extraction import (
    extract_document_ocr_line_candidates,
    extract_ocr_line_candidates,
    extract_pdf_ocr_line_candidates,
    serialize_candidate_extraction_result,
)
from labtrail_redaction import redact_source_page
from models import ClientShareGrant


def _synthetic_page() -> bytes:
    page = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=48)
    draw.text((60, 80), "GLUCOSE 98", fill="black", font=font)
    draw.text((650, 200), "SODIUM 999", fill="black", font=font)
    output = BytesIO()
    page.save(output, format="PNG")
    return output.getvalue()


def _synthetic_result_page() -> bytes:
    page = Image.new("RGB", (1200, 300), "white")
    draw = ImageDraw.Draw(page)
    draw.text(
        (60, 80),
        "GLUCOSE 98 mg/dL",
        fill="black",
        font=ImageFont.load_default(size=48),
    )
    output = BytesIO()
    page.save(output, format="PNG")
    return output.getvalue()


def _synthetic_pdf() -> bytes:
    first_page = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(first_page).text(
        (60, 80),
        "GLUCOSE 98",
        fill="black",
        font=ImageFont.load_default(size=48),
    )
    second_page = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(second_page).text(
        (60, 80),
        "SODIUM 999",
        fill="black",
        font=ImageFont.load_default(size=48),
    )
    output = BytesIO()
    first_page.save(
        output,
        format="PDF",
        save_all=True,
        append_images=[second_page],
    )
    return output.getvalue()


def _synthetic_jpeg() -> bytes:
    with Image.open(BytesIO(_synthetic_page())) as source:
        page = source.convert("RGB")
    output = BytesIO()
    page.save(output, format="JPEG")
    return output.getvalue()


def _oversized_raster() -> bytes:
    page = Image.new("1", (6401, 6250), 1)
    output = BytesIO()
    page.save(output, format="PNG")
    return output.getvalue()


def test_ocr_extractor_emits_review_candidates_and_incomplete_inventory() -> None:
    report_id = UUID(int=1)
    extraction_job_id = UUID(int=2)
    extracted_at = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

    result = extract_ocr_line_candidates(
        page_raster=_synthetic_page(),
        report_id=report_id,
        extraction_job_id=extraction_job_id,
        page_number=1,
        extracted_at=extracted_at,
    )

    assert result.report_id == report_id
    assert result.extraction_job_id == extraction_job_id
    assert len(result.candidates) == 2
    assert {candidate.raw_analyte_name.casefold() for candidate in result.candidates} == {
        "glucose 98",
        "sodium 999",
    }
    assert all(
        candidate.routing_status == "MANDATORY_EXCEPTION_REVIEW"
        and candidate.lifecycle_status == "CANDIDATE"
        and candidate.proposed_analyte_code == "UNMAPPED"
        and candidate.raw_value is None
        and candidate.raw_unit is None
        and candidate.region_available
        for candidate in result.candidates
    )
    assert [candidate.row_index for candidate in result.candidates] == [0, 1]
    assert result.page_inventories[0].result_row_indices == (0, 1)
    assert result.page_inventories[0].inventory_status == "INCOMPLETE"
    serialized = serialize_candidate_extraction_result(result)
    assert json.loads(serialized)["schema_version"] == "1"
    assert serialize_candidate_extraction_result(result) == serialized


def test_ocr_extractor_parses_simple_value_unit_lines_as_unmapped_candidates() -> None:
    result = extract_ocr_line_candidates(
        page_raster=_synthetic_result_page(),
        report_id=UUID(int=20),
        extraction_job_id=UUID(int=21),
        page_number=1,
        extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.raw_analyte_name == "GLUCOSE"
    assert candidate.raw_value == "98"
    assert candidate.raw_unit == "mg/dL"
    assert candidate.proposed_analyte_code == "UNMAPPED"
    assert candidate.routing_status == "MANDATORY_EXCEPTION_REVIEW"
    assert candidate.lifecycle_status == "CANDIDATE"
    assert result.page_inventories[0].inventory_status == "INCOMPLETE"


def test_pdf_extractor_processes_each_page_with_incomplete_coverage() -> None:
    result = extract_pdf_ocr_line_candidates(
        pdf_bytes=_synthetic_pdf(),
        report_id=UUID(int=10),
        extraction_job_id=UUID(int=11),
        extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        dpi=72,
    )

    assert [inventory.page_number for inventory in result.page_inventories] == [1, 2]
    assert all(
        inventory.inventory_status == "INCOMPLETE"
        for inventory in result.page_inventories
    )
    assert len({inventory.source_page_sha256 for inventory in result.page_inventories}) == 2
    assert {candidate.page_number for candidate in result.candidates} == {1, 2}
    assert any("glucose 98" in candidate.raw_analyte_name.casefold() for candidate in result.candidates)
    assert any("sodium 999" in candidate.raw_analyte_name.casefold() for candidate in result.candidates)


@pytest.mark.parametrize(
    ("source_bytes", "mime_type"),
    [
        (_synthetic_page(), "IMAGE/PNG"),
        (_synthetic_jpeg(), "image/jpeg"),
        (_synthetic_pdf(), "application/pdf"),
    ],
)
def test_document_extractor_dispatches_supported_formats(
    source_bytes: bytes,
    mime_type: str,
) -> None:
    result = extract_document_ocr_line_candidates(
        source_bytes=source_bytes,
        mime_type=mime_type,
        report_id=UUID(int=12),
        extraction_job_id=UUID(int=13),
        extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        dpi=72,
    )

    assert result.report_id == UUID(int=12)
    assert len(result.page_inventories) == (2 if mime_type == "application/pdf" else 1)
    assert all(
        inventory.inventory_status == "INCOMPLETE"
        for inventory in result.page_inventories
    )


def test_document_extractor_rejects_mime_type_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        extract_document_ocr_line_candidates(
            source_bytes=_synthetic_page(),
            mime_type="image/jpeg",
            report_id=UUID(int=14),
            extraction_job_id=UUID(int=15),
            extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_document_extractor_rejects_uploads_over_25_mib() -> None:
    with pytest.raises(ValueError, match="25 MB"):
        extract_document_ocr_line_candidates(
            source_bytes=b"x" * (25 * 1024 * 1024 + 1),
            mime_type="application/pdf",
            report_id=UUID(int=16),
            extraction_job_id=UUID(int=17),
            extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_document_extractor_rejects_images_over_pixel_limit() -> None:
    with pytest.raises(ValueError, match="dimensions exceed"):
        extract_document_ocr_line_candidates(
            source_bytes=_oversized_raster(),
            mime_type="image/png",
            report_id=UUID(int=18),
            extraction_job_id=UUID(int=19),
            extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_ocr_extractor_rejects_an_invalid_raster() -> None:
    with pytest.raises(ValueError, match="decodable raster"):
        extract_ocr_line_candidates(
            page_raster=b"not an image",
            report_id=UUID(int=1),
            extraction_job_id=UUID(int=2),
            page_number=1,
            extracted_at=datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
        )


def test_ocr_extractor_result_cannot_make_a_page_delivery_ready() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    page_raster = _synthetic_page()
    result = extract_ocr_line_candidates(
        page_raster=page_raster,
        report_id=UUID(int=1),
        extraction_job_id=UUID(int=2),
        page_number=1,
        extracted_at=now,
    )
    grant = ClientShareGrant(
        grant_id=UUID(int=3),
        recipient_name="Synthetic Clinician",
        recipient_identity_hash="synthetic-recipient-hash",
        granted_analytes=["GLUCOSE"],
        range_start=date(2026, 1, 1),
        range_end=date(2026, 11, 1),
        created_at=now,
        expires_at=now + timedelta(days=365),
        local_status="PREPARING_CLIENT",
    )

    redaction = redact_source_page(
        page_raster=page_raster,
        report_id=result.report_id,
        page_number=1,
        extraction_result=result,
        observations=(),
        grant=grant,
        as_of=now,
        region_confidence_threshold=0.9,
        mask_padding_pixels=2,
    )

    assert redaction.disposition == "WITHHELD"
    assert redaction.withheld_reason == "ROW_COVERAGE_UNVERIFIED"
    assert redaction.redacted_png is None
