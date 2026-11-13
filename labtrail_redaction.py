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
import pypdfium2 as pdfium
from pytesseract import Output
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from labtrail_algorithms import prepare_share_content_selection
from models import (
    AcceptedObservation,
    CandidateExtraction,
    CandidateExtractionResult,
    ClientShareGrant,
)


MAX_RASTER_PIXELS = 40_000_000
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


def _boxes_intersect(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _verify_redacted_page_ocr(
    image: Image.Image,
    mask_regions: tuple[tuple[UUID, tuple[int, int, int, int]], ...],
    masked_candidates: tuple[CandidateExtraction, ...],
    granted_regions: tuple[tuple[int, int, int, int], ...] = (),
) -> WithheldReason | None:
    """OCR the flattened page and fail closed on any sign of ungranted text.

    Two checks run. (1) Any word inside a mask other than the REDACTED label is a
    leak. (2) No masked field's text may appear anywhere outside the granted rows.
    Words inside granted rows are excluded from check (2) only, because the patient
    chose to show them and they legitimately repeat tokens such as units
    ("mg/dL") that also appear in masked rows.
    """
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
        " ".join(
            text
            for text, left, top, width, height in ocr_words
            if not any(
                _boxes_intersect((left, top, left + width, top + height), region)
                for region in granted_regions
            )
        )
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


def rasterize_pdf_page(
    pdf_bytes: bytes,
    page_number: int,
    *,
    dpi: int = 150,
) -> bytes:
    """Render one PDF page to a metadata-free RGB PNG for the redaction prototype."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
        raise ValueError("PDF source must be non-empty bytes.")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("Page number must be a positive integer.")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 600:
        raise ValueError("Rasterization DPI must be an integer from 72 to 600.")

    document = None
    page = None
    bitmap = None
    try:
        document = pdfium.PdfDocument(pdf_bytes)
        if page_number > len(document):
            raise ValueError("Page number exceeds the PDF page count.")
        page = document[page_number - 1]
        width_points, height_points = page.get_size()
        scale = dpi / 72
        if (
            not isfinite(width_points)
            or not isfinite(height_points)
            or width_points <= 0
            or height_points <= 0
        ):
            raise ValueError("PDF page dimensions must be finite and positive.")
        raster_width = ceil(width_points * scale)
        raster_height = ceil(height_points * scale)
        if raster_width * raster_height > MAX_RASTER_PIXELS:
            raise ValueError("PDF page dimensions exceed the rasterization limit.")
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().convert("RGB")
        output = BytesIO()
        image.save(output, format="PNG", optimize=False)
        return output.getvalue()
    except (pdfium.PdfiumError, OSError) as error:
        raise ValueError("PDF source could not be rendered.") from error
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        if document is not None:
            document.close()


def redact_source_page(
    *,
    page_raster: bytes,
    report_id: UUID,
    page_number: int,
    extraction_result: CandidateExtractionResult,
    observations: Iterable[AcceptedObservation],
    grant: ClientShareGrant,
    as_of: datetime,
    region_confidence_threshold: float,
    mask_padding_pixels: int,
) -> SourcePageRedactionResult:
    """Mask every ungranted extracted row and emit a metadata-free PNG.

    This prototype accepts an already-rasterized page and a validated extraction
    result. The inventory is supplied by the extraction service; that service is
    not implemented here, so this result is never delivery-ready. OCR checks the
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
    if not isinstance(extraction_result, CandidateExtractionResult):
        raise ValueError("A validated candidate extraction result is required.")
    if extraction_result.report_id != report_id:
        raise ValueError("Extraction result must belong to the requested report.")
    source_page_sha256 = sha256(page_raster).hexdigest()

    page_inventory = next(
        (
            inventory
            for inventory in extraction_result.page_inventories
            if inventory.page_number == page_number
        ),
        None,
    )
    if (
        page_inventory is None
        or page_inventory.inventory_status != "COMPLETE"
        or page_inventory.source_page_sha256 != source_page_sha256
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")

    page_candidates = tuple(
        candidate
        for candidate in extraction_result.candidates
        if candidate.page_number == page_number
    )
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
    if any(
        candidate.extraction_job_id != extraction_result.extraction_job_id
        for candidate in page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if len({candidate.candidate_id for candidate in page_candidates}) != len(
        page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if len({candidate.row_index for candidate in page_candidates}) != len(
        page_candidates
    ):
        return _withheld(report_id, page_number, "ROW_COVERAGE_UNVERIFIED")
    if page_inventory.result_row_indices != tuple(
        sorted(candidate.row_index for candidate in page_candidates)
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

    granted_regions = tuple(
        _pixel_bounds(candidate_by_id[candidate_id].bounding_box, width, height, 0)
        for candidate_id in selected_by_candidate
    )
    ocr_withheld_reason = _verify_redacted_page_ocr(
        flattened,
        tuple(mask_regions),
        masked_candidates,
        granted_regions,
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
