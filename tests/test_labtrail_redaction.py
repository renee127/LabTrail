from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import pytesseract
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

from labtrail_redaction import PageRowCoverageEvidence, redact_source_page
from models import AcceptedObservation, CandidateExtraction, ClientShareGrant


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
REPORT_ID = UUID(int=1)
GRANT = ClientShareGrant(
    grant_id=UUID(int=2),
    recipient_name="Synthetic Clinician",
    recipient_identity_hash="synthetic-recipient-hash",
    granted_analytes=["GLUCOSE"],
    range_start=date(2026, 1, 1),
    range_end=date(2026, 11, 1),
    created_at=NOW,
    expires_at=NOW + timedelta(days=365),
    local_status="PREPARING_CLIENT",
)


def _candidate(
    candidate_id: int,
    *,
    analyte_code: str,
    row_index: int,
    bounding_box: tuple[float, float, float, float] | None,
    lifecycle_status: str,
    raw_value: str = "1.0",
    region_confidence: float = 0.99,
) -> CandidateExtraction:
    return CandidateExtraction(
        candidate_id=UUID(int=candidate_id),
        report_id=REPORT_ID,
        extraction_job_id=UUID(int=3),
        row_index=row_index,
        raw_analyte_name=analyte_code,
        proposed_analyte_code=analyte_code,
        raw_value=raw_value,
        raw_unit="unit",
        reference_interval=None,
        method_note=None,
        specimen_type=None,
        abnormal_flag=None,
        page_number=1,
        bounding_box=bounding_box,
        region_available=bounding_box is not None,
        field_confidences={
            "analyte": 0.99,
            "value": 0.99,
            "unit": 0.99,
            "region": region_confidence,
        },
        model_identifier="synthetic-model",
        model_version="1",
        prompt_hash="0" * 64,
        extracted_at=NOW,
        routing_status="STANDARD_REVIEW",
        lifecycle_status=lifecycle_status,
    )


def _accepted_observation(
    candidate: CandidateExtraction,
    *,
    analyte_code: str,
) -> AcceptedObservation:
    return AcceptedObservation(
        observation_id=UUID(int=20),
        candidate_id=candidate.candidate_id,
        report_id=candidate.report_id,
        analyte_code=analyte_code,
        canonical_value=Decimal("1.0"),
        canonical_unit="unit",
        original_value="1.0",
        original_unit="unit",
        conversion_factor=Decimal("1"),
        conversion_outcome="CANONICAL_DIRECT",
        unconvertible_reason=None,
        reference_interval=None,
        method_note=None,
        source_lab="Synthetic Lab",
        collection_datetime=date(2026, 11, 1),
        page_number=candidate.page_number,
        bounding_box=candidate.bounding_box,
        provenance_flag="AS_EXTRACTED",
        lifecycle_status="ACCEPTED",
        rule_version="synthetic-rules-v1",
        accepted_at=NOW,
    )


def _synthetic_page() -> bytes:
    page = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=48)
    draw.text((60, 80), "GLUCOSE 98", fill="black", font=font)
    draw.text((650, 80), "SODIUM 999", fill="black", font=font)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("sensitive", "synthetic metadata")
    output = BytesIO()
    page.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def _redact(
    candidates: tuple[CandidateExtraction, ...],
    observations: tuple[AcceptedObservation, ...],
    *,
    row_coverage_evidence: PageRowCoverageEvidence | None = None,
):
    page_raster = _synthetic_page()
    if row_coverage_evidence is None:
        first_candidate = candidates[0]
        row_coverage_evidence = PageRowCoverageEvidence(
            report_id=first_candidate.report_id,
            page_number=first_candidate.page_number,
            extraction_job_id=first_candidate.extraction_job_id,
            source_page_sha256=sha256(page_raster).hexdigest(),
            expected_row_indices=tuple(
                sorted(candidate.row_index for candidate in candidates)
            ),
        )
    return redact_source_page(
        page_raster=page_raster,
        report_id=REPORT_ID,
        page_number=1,
        candidates=candidates,
        observations=observations,
        grant=GRANT,
        as_of=NOW,
        region_confidence_threshold=0.9,
        mask_padding_pixels=2,
        row_coverage_evidence=row_coverage_evidence,
    )


def test_redacts_ungranted_row_and_emits_metadata_free_png() -> None:
    granted = _candidate(
        4,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted = _candidate(
        5,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.15, 0.95, 0.65),
        lifecycle_status="CANDIDATE",
        raw_value="999",
    )

    result = _redact(
        (granted, ungranted),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    with Image.open(BytesIO(_synthetic_page())) as source:
        source_text = pytesseract.image_to_string(source, config="--psm 6")
    assert "sodium" in source_text.casefold()
    assert "999" in source_text
    assert result.disposition == "REDACTED_PNG"
    assert result.withheld_reason is None
    assert result.masked_candidate_ids == (ungranted.candidate_id,)
    assert result.delivery_ready is False
    assert result.redacted_png is not None
    with Image.open(BytesIO(result.redacted_png)) as redacted:
        assert redacted.format == "PNG"
        assert redacted.mode == "RGB"
        assert "sensitive" not in redacted.info
        assert redacted.getpixel((1100, 160)) == (0, 0, 0)
        redacted_text = pytesseract.image_to_string(redacted, config="--psm 6")
        redacted_allowed_region = redacted.crop((20, 20, 560, 200)).tobytes()
    assert "sodium" not in redacted_text.casefold()
    assert "999" not in redacted_text
    with Image.open(BytesIO(_synthetic_page())) as source:
        assert redacted_allowed_region == source.crop((20, 20, 560, 200)).tobytes()


def test_withholds_page_when_ocr_finds_unmasked_ungranted_text() -> None:
    granted = _candidate(
        15,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted_with_inaccurate_region = _candidate(
        16,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.8, 0.15, 0.97, 0.65),
        lifecycle_status="CANDIDATE",
        raw_value="999",
    )

    result = _redact(
        (granted, ungranted_with_inaccurate_region),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "OCR_LEAK_DETECTED"
    assert result.redacted_png is None


def test_withholds_page_when_ocr_cannot_recognize_redaction_label(
    monkeypatch,
) -> None:
    granted = _candidate(
        17,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted = _candidate(
        18,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.15, 0.95, 0.65),
        lifecycle_status="CANDIDATE",
        raw_value="999",
    )
    monkeypatch.setattr(
        "labtrail_redaction.pytesseract.image_to_data",
        lambda *args, **kwargs: {
            "text": [],
            "left": [],
            "top": [],
            "width": [],
            "height": [],
        },
    )

    result = _redact(
        (granted, ungranted),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "OCR_VERIFICATION_FAILED"
    assert result.redacted_png is None


def test_withholds_page_when_row_coverage_is_unverified() -> None:
    granted = _candidate(
        6,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )

    page_raster = _synthetic_page()
    evidence = PageRowCoverageEvidence(
        report_id=REPORT_ID,
        page_number=1,
        extraction_job_id=granted.extraction_job_id,
        source_page_sha256=sha256(page_raster).hexdigest(),
        expected_row_indices=(0, 1),
    )
    result = _redact(
        (granted,),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
        row_coverage_evidence=evidence,
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "ROW_COVERAGE_UNVERIFIED"
    assert result.redacted_png is None


def test_withholds_page_when_coverage_evidence_is_missing() -> None:
    granted = _candidate(
        19,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted = _candidate(
        20,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.15, 0.95, 0.65),
        lifecycle_status="CANDIDATE",
        raw_value="999",
    )

    result = redact_source_page(
        page_raster=_synthetic_page(),
        report_id=REPORT_ID,
        page_number=1,
        candidates=(granted, ungranted),
        observations=(_accepted_observation(granted, analyte_code="GLUCOSE"),),
        grant=GRANT,
        as_of=NOW,
        region_confidence_threshold=0.9,
        mask_padding_pixels=2,
        row_coverage_evidence=None,
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "ROW_COVERAGE_UNVERIFIED"
    assert result.redacted_png is None


def test_withholds_duplicate_candidate_identifiers() -> None:
    granted = _candidate(
        14,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    duplicate_id = _candidate(
        14,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.15, 0.95, 0.65),
        lifecycle_status="CANDIDATE",
    )

    result = _redact(
        (granted, duplicate_id),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "ROW_COVERAGE_UNVERIFIED"
    assert result.redacted_png is None


def test_withholds_page_when_any_row_region_is_missing() -> None:
    granted = _candidate(
        7,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted_without_region = _candidate(
        8,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=None,
        lifecycle_status="CANDIDATE",
        raw_value="999",
        region_confidence=0.1,
    )

    result = _redact(
        (granted, ungranted_without_region),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "REGION_UNAVAILABLE"
    assert result.redacted_png is None


def test_withholds_granted_row_page_when_its_region_is_missing() -> None:
    granted_without_region = _candidate(
        11,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=None,
        lifecycle_status="ACCEPTED",
        region_confidence=0.1,
    )

    result = _redact(
        (granted_without_region,),
        (
            _accepted_observation(
                granted_without_region,
                analyte_code="GLUCOSE",
            ),
        ),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "REGION_UNAVAILABLE"
    assert result.redacted_png is None


def test_withholds_page_when_any_row_region_is_unreliable() -> None:
    granted = _candidate(
        9,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted_low_confidence = _candidate(
        10,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.15, 0.95, 0.65),
        lifecycle_status="CANDIDATE",
        raw_value="999",
        region_confidence=0.5,
    )

    result = _redact(
        (granted, ungranted_low_confidence),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "REGION_UNRELIABLE"
    assert result.redacted_png is None


def test_withholds_page_when_redaction_label_does_not_fit() -> None:
    granted = _candidate(
        12,
        analyte_code="GLUCOSE",
        row_index=0,
        bounding_box=(0.05, 0.15, 0.4, 0.65),
        lifecycle_status="ACCEPTED",
    )
    ungranted_small_region = _candidate(
        13,
        analyte_code="SODIUM",
        row_index=1,
        bounding_box=(0.5, 0.3, 0.6, 0.4),
        lifecycle_status="CANDIDATE",
    )

    result = _redact(
        (granted, ungranted_small_region),
        (_accepted_observation(granted, analyte_code="GLUCOSE"),),
    )

    assert result.disposition == "WITHHELD"
    assert result.withheld_reason == "REDACTION_LABEL_DOES_NOT_FIT"
    assert result.redacted_png is None
