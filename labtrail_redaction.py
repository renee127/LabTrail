"""Fail-closed raster redaction prototype for synthetic LabTrail share pages."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from math import ceil, floor, isfinite
from typing import Literal
from uuid import UUID

import pytesseract
from pytesseract import Output
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from labtrail_algorithms import prepare_share_content_selection
from models import AcceptedObservation, CandidateExtraction, ClientShareGrant


PageDisposition = Literal["REDACTED_PNG", "WITHHELD"]
WithheldReason = Literal[
    "NO_GRANTED_OBSERVATION",
    "REGION_UNAVAILABLE",
    "REGION_UNRELIABLE",
    "ROW_COVERAGE_UNVERIFIED",
    "REDACTION_LABEL_DOES_NOT_FIT",
    "OCR_LEAK_DETECTED",
    "OCR_VERIFICATION_FAILED",
]


@dataclass(frozen=True)
class SourcePageRedactionResult:
    report_id: UUID
    page_number: int
    disposition: PageDisposition
    withheld_reason: WithheldReason | None
    redacted_png: bytes | None
    masked_candidate_ids: tuple[UUID, ...]
    delivery_ready: Literal[False] = False

    def __post_init__(self) -> None:
        if self.disposition == "WITHHELD":
            if self.redacted_png is not None or self.withheld_reason is None:
                raise ValueError("Withheld pages cannot contain image bytes.")
        elif self.redacted_png is None or self.withheld_reason is not None:
            raise ValueError("Redacted pages require PNG bytes and no withheld reason.")


@dataclass(frozen=True)
class PageRowCoverageEvidence:
    """Page-bound row inventory supplied by a trusted extraction pipeline."""

    report_id: UUID
    page_number: int
    extraction_job_id: UUID
    source_page_sha256: str
    expected_row_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_number, bool)
            or not isinstance(self.page_number, int)
            or self.page_number < 1
        ):
            raise ValueError("Coverage evidence page number must be positive.")
        if (
            not isinstance(self.source_page_sha256, str)
            or len(self.source_page_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.source_page_sha256
            )
        ):
            raise ValueError("Coverage evidence requires a lowercase SHA-256 digest.")
        if (
            not isinstance(self.expected_row_indices, tuple)
            or not self.expected_row_indices
            or any(
                isinstance(row_index, bool)
                or not isinstance(row_index, int)
                or row_index < 0
                for row_index in self.expected_row_indices
            )
            or tuple(sorted(set(self.expected_row_indices)))
            != self.expected_row_indices
        ):
            raise ValueError("Expected row indices must be unique, sorted, and non-negative.")


def _withheld(
    report_id: UUID,
    page_number: int,
    reason: WithheldReason,
    masked_candidate_ids: tuple[UUID, ...] = (),
) -> SourcePageRedactionResult:
    return SourcePageRedactionResult(
        report_id=report_id,
        page_number=page_number,
        disposition="WITHHELD",
        withheld_reason=reason,
        redacted_png=None,
        masked_candidate_ids=masked_candidate_ids,
    )


def _pixel_bounds(
    box: tuple[float, float, float, float],
    width: int,
    height: int,
    padding: int,
) -> tuple[int, int, int, int]:
    x_min, y_min, x_max, y_max = box
    return (
        max(0, floor(x_min * width) - padding),
        max(0, floor(y_min * height) - padding),
        min(width - 1, ceil(x_max * width) - 1 + padding),
        min(height - 1, ceil(y_max * height) - 1 + padding),
    )


def _normalize_ocr_text(value: str) -> str:
    return " ".join(
        "".join(
            character if character.isalnum() else " "
            for character in value.casefold()
        ).split()
    )


def _verify_redacted_page_ocr(
    image: Image.Image,
    mask_regions: tuple[tuple[UUID, tuple[int, int, int, int]], ...],
    masked_candidates: tuple[CandidateExtraction, ...],
) -> WithheldReason | None:
    if not mask_regions:
        return None

    data = pytesseract.image_to_data(
        image,
        config="--psm 6",
        output_type=Output.DICT,
    )
    ocr_words = [
        (
            str(text),
            int(data["left"][index]),
            int(data["top"][index]),
            int(data["width"][index]),
            int(data["height"][index]),
        )
        for index, text in enumerate(data["text"])
        if str(text).strip()
    ]
    recognized_text = _normalize_ocr_text(
        " ".join(text for text, *_ in ocr_words)
    )

    for candidate in masked_candidates:
        for field in (
            candidate.raw_analyte_name,
            candidate.raw_value,
            candidate.raw_unit,
            candidate.reference_interval,
            candidate.method_note,
            candidate.specimen_type,
            candidate.abnormal_flag,
        ):
            normalized_field = _normalize_ocr_text(field) if field else ""
            if normalized_field and normalized_field in recognized_text:
                return "OCR_LEAK_DETECTED"

    recognized_labels: set[UUID] = set()
    for candidate_id, (left, top, right, bottom) in mask_regions:
        for text, word_left, word_top, word_width, word_height in ocr_words:
            word_right = word_left + word_width
            word_bottom = word_top + word_height
            intersects_mask = (
                word_left <= right
                and word_right >= left
                and word_top <= bottom
                and word_bottom >= top
            )
            normalized = _normalize_ocr_text(text)
            if intersects_mask and normalized == "redacted":
                recognized_labels.add(candidate_id)
            elif intersects_mask and normalized:
                return "OCR_LEAK_DETECTED"

    if recognized_labels != {candidate_id for candidate_id, _ in mask_regions}:
        return "OCR_VERIFICATION_FAILED"
    return None


def redact_source_page(
    *,
    page_raster: bytes,
    report_id: UUID,
    page_number: int,
    candidates: Iterable[CandidateExtraction],
    observations: Iterable[AcceptedObservation],
    grant: ClientShareGrant,
    as_of: datetime,
    region_confidence_threshold: float,
    mask_padding_pixels: int,
    row_coverage_evidence: PageRowCoverageEvidence | None,
) -> SourcePageRedactionResult:
    """Mask every ungranted extracted row and emit a metadata-free PNG.

    This prototype accepts an already-rasterized page. Coverage evidence must bind
    an independent row inventory to the page and extraction job. No such inventory
    producer exists here, and this result is never delivery-ready. OCR checks the
    flattened output for candidate text but is not proof of OCR completeness.
    """
    if page_number < 1:
        raise ValueError("Page number must be positive.")
    if (
        isinstance(region_confidence_threshold, bool)
        or not isinstance(region_confidence_threshold, (int, float))
        or not isfinite(region_confidence_threshold)
        or not 0.0 <= region_confidence_threshold <= 1.0
    ):
        raise ValueError("Region confidence threshold must be finite and from 0 to 1.")
    if (
        isinstance(mask_padding_pixels, bool)
        or not isinstance(mask_padding_pixels, int)
        or mask_padding_pixels < 0
    ):
        raise ValueError("Mask padding must be a non-negative integer.")
    if not isinstance(page_raster, bytes) or not page_raster:
        raise ValueError("Source page raster must be non-empty bytes.")
    source_page_sha256 = sha256(page_raster).hexdigest()

    page_candidates = tuple(candidates)
    if not page_candidates:
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if any(
        candidate.report_id != report_id or candidate.page_number != page_number
        for candidate in page_candidates
    ):
        raise ValueError("Candidates must belong to the requested source page.")
    if any(
        isinstance(candidate.row_index, bool)
        or not isinstance(candidate.row_index, int)
        or candidate.row_index < 0
        for candidate in page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if len({candidate.extraction_job_id for candidate in page_candidates}) != 1:
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if len({candidate.candidate_id for candidate in page_candidates}) != len(
        page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if len({candidate.row_index for candidate in page_candidates}) != len(
        page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if (
        row_coverage_evidence is not None
        and not isinstance(row_coverage_evidence, PageRowCoverageEvidence)
    ):
        raise ValueError("Row coverage evidence has an invalid type.")
    extraction_job_id = page_candidates[0].extraction_job_id
    if (
        row_coverage_evidence is None
        or row_coverage_evidence.report_id != report_id
        or row_coverage_evidence.page_number != page_number
        or row_coverage_evidence.extraction_job_id != extraction_job_id
        or row_coverage_evidence.source_page_sha256 != source_page_sha256
        or row_coverage_evidence.expected_row_indices
        != tuple(sorted(candidate.row_index for candidate in page_candidates))
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")

    selection = prepare_share_content_selection(
        observations=observations,
        notes=(),
        grant=grant,
        as_of=as_of,
    )
    selected_by_candidate: dict[UUID, AcceptedObservation] = {}
    for observation in selection.observations:
        if (
            observation.report_id == report_id
            and observation.page_number == page_number
        ):
            if observation.candidate_id in selected_by_candidate:
                raise ValueError(
                    "A candidate cannot identify multiple granted observations."
                )
            selected_by_candidate[observation.candidate_id] = observation
    if not selected_by_candidate:
        return _withheld(report_id, page_number, "NO_GRANTED_OBSERVATION")

    candidate_by_id = {candidate.candidate_id: candidate for candidate in page_candidates}
    if not set(selected_by_candidate).issubset(candidate_by_id):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")

    for candidate in page_candidates:
        if not candidate.region_available or candidate.bounding_box is None:
            return _withheld(report_id, page_number, "REGION_UNAVAILABLE")
        if candidate.field_confidences["region"] < region_confidence_threshold:
            return _withheld(report_id, page_number, "REGION_UNRELIABLE")
        observation = selected_by_candidate.get(candidate.candidate_id)
        if observation is not None and (
            candidate.lifecycle_status != "ACCEPTED"
            or observation.bounding_box != candidate.bounding_box
        ):
            return _withheld(report_id, page_number, "REGION_UNRELIABLE")

    masked_candidates = tuple(
        candidate
        for candidate in page_candidates
        if candidate.candidate_id not in selected_by_candidate
    )

    try:
        with Image.open(BytesIO(page_raster)) as source:
            source.load()
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("Animated images cannot be used as source pages.")
            width, height = source.size
            rgba = source.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError("Source page must be a decodable raster image.") from error
    if width < 1 or height < 1:
        raise ValueError("Source page image dimensions must be positive.")

    flattened = Image.new("RGB", (width, height), "white")
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    draw = ImageDraw.Draw(flattened)
    font = ImageFont.load_default(size=24)
    label = "REDACTED"
    text_box = draw.textbbox((0, 0), label, font=font)
    label_width = text_box[2] - text_box[0]
    label_height = text_box[3] - text_box[1]

    mask_regions = []
    for candidate in masked_candidates:
        if candidate.bounding_box is None:
            return _withheld(report_id, page_number, "REGION_UNAVAILABLE")
        bounds = _pixel_bounds(
            candidate.bounding_box,
            width,
            height,
            mask_padding_pixels,
        )
        left, top, right, bottom = bounds
        if (
            right - left + 1 < label_width + 2
            or bottom - top + 1 < label_height + 2
        ):
            return _withheld(
                report_id,
                page_number,
                "REDACTION_LABEL_DOES_NOT_FIT",
                tuple(candidate.candidate_id for candidate in masked_candidates),
            )
        mask_regions.append((candidate.candidate_id, bounds))

    for _, (left, top, right, bottom) in mask_regions:
        draw.rectangle((left, top, right, bottom), fill=(0, 0, 0))
        text_left = left + max(1, (right - left + 1 - label_width) // 2)
        text_top = top + max(1, (bottom - top + 1 - label_height) // 2)
        draw.text(
            (text_left - text_box[0], text_top - text_box[1]),
            label,
            fill=(255, 255, 255),
            font=font,
        )

    ocr_withheld_reason = _verify_redacted_page_ocr(
        flattened,
        tuple(mask_regions),
        masked_candidates,
    )
    if ocr_withheld_reason is not None:
        return _withheld(
            report_id,
            page_number,
            ocr_withheld_reason,
            tuple(candidate_id for candidate_id, _ in mask_regions),
        )

    output = BytesIO()
    flattened.save(output, format="PNG", optimize=False)
    return SourcePageRedactionResult(
        report_id=report_id,
        page_number=page_number,
        disposition="REDACTED_PNG",
        withheld_reason=None,
        redacted_png=output.getvalue(),
        masked_candidate_ids=tuple(
            candidate_id for candidate_id, _ in mask_regions
        ),
    )
