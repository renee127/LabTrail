"""Conservative local OCR-line extraction prototype for LabTrail."""

import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

import numpy as np
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from pytesseract import Output
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from labtrail_redaction import MAX_RASTER_PIXELS, rasterize_pdf_page
from models import (
    CandidateExtraction,
    CandidateExtractionResult,
    ExtractionPageInventory,
)


_TESSERACT_CONFIG = "--psm 6"
# Low-resolution rasters (phone photos, ~100 dpi scans) are upscaled before OCR;
# Tesseract misreads ~11 pt text below ~130 dpi. Sharper rasters are left alone
# because resampling them softens glyphs and causes new misreads (A1c -> Aic).
_UPSCALE_BELOW_WIDTH = 1100
_OCR_TARGET_WIDTH = 2200
_MAX_DESKEW_DEGREES = 5.0
# Background brightness spread (5th-95th percentile) above which lighting is
# treated as uneven and flattened before OCR.
_UNEVEN_LIGHTING_SPREAD = 20.0
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# A PDF page with at least this many embedded characters is read from its text
# layer, which is exact, instead of relying on OCR alone.
_TEXT_LAYER_MIN_CHARS = 20
_TEXT_LAYER_MODEL = "pdf-text-layer"
_TEXT_LAYER_CONFIDENCE = 0.99
# OCR lines mostly covered by text-layer lines are duplicates and are dropped;
# the rest (text drawn as images, such as logos or letterheads) are kept so
# every visible line stays in the inventory and can be masked.
_OCR_DUPLICATE_OVERLAP = 0.3
# Side-by-side header fields ("Mr. Jordan Sample      Registered on : ...") can
# share a baseline and come out of the text layer as one line. Split at a wide gap
# only when the text after it starts like a "Label :" field, so table rows
# ("TOTAL CHOLESTEROL    180    mg/dl") are never split.
_FIELD_GAP_HEIGHTS = 2.5
_FIELD_LABEL_START = re.compile(r"^[A-Za-z][A-Za-z .'/()#-]{0,30}?\s*:")
_RESULT_LINE_PATTERN = re.compile(
    r"^(?P<analyte>.+?)\s+"
    r"(?P<value>(?:[<>≤≥]\s*)?[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)"
    r"(?:\s+(?P<flag>HH|LL|H|L|A|\*))?\s+"
    r"(?P<unit>[A-Za-zµμ%][A-Za-z0-9µμ%./^*+-]*)(?:\s+(?P<rest>.*))?$"
)
_TRAILING_FLAG_PATTERN = re.compile(r"^(?P<flag>HH|LL|H|L|A|\*)(?:\s+|$)")
_REFERENCE_INTERVAL_PATTERN = re.compile(
    r"^(?P<interval>\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?|[<>≤≥]\s*\d+(?:\.\d+)?)(?:\s|$)"
)
_RESULT_SCHEMA = json.loads(
    (Path(__file__).with_name("schemas") / "candidate_extraction_result_schema_v1.json")
    .read_text(encoding="utf-8")
)
_RESULT_VALIDATOR = Draft202012Validator(
    _RESULT_SCHEMA,
    format_checker=FormatChecker(),
)


class _CandidatePayload(TypedDict):
    candidate_id: str
    report_id: str
    extraction_job_id: str
    row_index: int
    raw_analyte_name: str
    proposed_analyte_code: str
    raw_value: str | None
    raw_unit: str | None
    reference_interval: str | None
    method_note: str | None
    specimen_type: str | None
    abnormal_flag: str | None
    page_number: int
    bounding_box: list[float] | None
    region_available: bool
    field_confidences: dict[str, float]
    model_identifier: str
    model_version: str
    prompt_hash: str
    extracted_at: str
    routing_status: Literal["STANDARD_REVIEW", "MANDATORY_EXCEPTION_REVIEW"]
    lifecycle_status: Literal[
        "CANDIDATE",
        "ACCEPTED",
        "REJECTED",
        "RETRACTED_BY_PATIENT",
    ]


class _PageInventoryPayload(TypedDict):
    page_number: int
    source_page_sha256: str
    result_row_indices: list[int]
    inventory_status: Literal["COMPLETE", "INCOMPLETE"]


class _ResultPayload(TypedDict):
    schema_version: Literal["1"]
    report_id: str
    extraction_job_id: str
    candidates: list[_CandidatePayload]
    page_inventories: list[_PageInventoryPayload]


def serialize_candidate_extraction_result(
    result: CandidateExtractionResult,
) -> bytes:
    """Serialize a validated extraction result as deterministic UTF-8 JSON."""
    if not isinstance(result, CandidateExtractionResult):
        raise ValueError("A validated candidate extraction result is required.")

    def encode_value(value: object) -> str:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(
            f"Unsupported extraction result value: {type(value).__name__}"
        )

    return json.dumps(
        asdict(result),
        default=encode_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def parse_candidate_extraction_result(payload: bytes) -> CandidateExtractionResult:
    """Validate and parse a Z4 extraction-result JSON response."""
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("Candidate extraction response must be non-empty bytes.")
    try:
        raw_result = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Candidate extraction response is not valid UTF-8 JSON.") from error

    try:
        _RESULT_VALIDATOR.validate(raw_result)
    except ValidationError as error:
        raise ValueError("Candidate extraction response failed schema validation.") from error

    result = cast(_ResultPayload, raw_result)
    candidates = tuple(
        CandidateExtraction(
            candidate_id=UUID(candidate["candidate_id"]),
            report_id=UUID(candidate["report_id"]),
            extraction_job_id=UUID(candidate["extraction_job_id"]),
            row_index=candidate["row_index"],
            raw_analyte_name=candidate["raw_analyte_name"],
            proposed_analyte_code=candidate["proposed_analyte_code"],
            raw_value=candidate["raw_value"],
            raw_unit=candidate["raw_unit"],
            reference_interval=candidate["reference_interval"],
            method_note=candidate["method_note"],
            specimen_type=candidate["specimen_type"],
            abnormal_flag=candidate["abnormal_flag"],
            page_number=candidate["page_number"],
            bounding_box=_parse_bounding_box(candidate["bounding_box"]),
            region_available=candidate["region_available"],
            field_confidences=candidate["field_confidences"],
            model_identifier=candidate["model_identifier"],
            model_version=candidate["model_version"],
            prompt_hash=candidate["prompt_hash"],
            extracted_at=datetime.fromisoformat(candidate["extracted_at"]),
            routing_status=candidate["routing_status"],
            lifecycle_status=candidate["lifecycle_status"],
        )
        for candidate in result["candidates"]
    )
    page_inventories = tuple(
        ExtractionPageInventory(
            page_number=inventory["page_number"],
            source_page_sha256=inventory["source_page_sha256"],
            result_row_indices=tuple(inventory["result_row_indices"]),
            inventory_status=inventory["inventory_status"],
        )
        for inventory in result["page_inventories"]
    )
    return CandidateExtractionResult(
        report_id=UUID(result["report_id"]),
        extraction_job_id=UUID(result["extraction_job_id"]),
        candidates=candidates,
        page_inventories=page_inventories,
        schema_version=result["schema_version"],
    )


def _parse_bounding_box(
    value: list[float] | None,
) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if len(value) != 4:
        raise ValueError("Candidate bounding box must contain four coordinates.")
    return (value[0], value[1], value[2], value[3])


def _validate_upload_bytes(source_bytes: bytes, description: str) -> None:
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise ValueError(f"{description} must be non-empty bytes.")
    if len(source_bytes) > _MAX_UPLOAD_BYTES:
        raise ValueError("Uploaded document exceeds the 25 MB size limit.")


def _parse_result_line(
    text: str,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Split one OCR line into analyte, value, unit, reference interval, and lab flag.

    A high/low flag printed by the lab (H, L, HH, LL, A, *) may sit between the value
    and the unit or right after the unit; it is captured verbatim, never inferred.
    """
    match = _RESULT_LINE_PATTERN.fullmatch(text)
    if match is None:
        return text, None, None, None, None
    analyte = match.group("analyte").strip()
    if not analyte or not any(character.isalpha() for character in analyte):
        return text, None, None, None, None
    flag = match.group("flag")
    rest = match.group("rest") or ""
    trailing_flag = _TRAILING_FLAG_PATTERN.match(rest)
    if trailing_flag:
        flag = flag or trailing_flag.group("flag")
        rest = rest[trailing_flag.end():]
    interval_match = _REFERENCE_INTERVAL_PATTERN.match(rest)
    reference_interval = (
        interval_match.group("interval").replace(" ", "") if interval_match else None
    )
    return (
        analyte,
        match.group("value").replace(" ", ""),
        match.group("unit"),
        reference_interval,
        flag,
    )


def _estimate_skew_degrees(image: Image.Image) -> float:
    """Estimate text-line skew by maximizing the sharpness of the row profile."""
    small = image.convert("L")
    if small.width > 1000:
        small = small.resize((1000, max(1, round(small.height * 1000 / small.width))))
    ink = small.point(lambda value: 255 if value < 140 else 0)
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-_MAX_DESKEW_DEGREES, _MAX_DESKEW_DEGREES + 0.01, 0.2):
        rows = np.asarray(ink.rotate(float(angle), fillcolor=0), dtype=np.float64).sum(axis=1)
        score = float(np.sum(np.diff(rows) ** 2))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle if abs(best_angle) >= 0.3 else 0.0


def _flatten_illumination(image: Image.Image) -> Image.Image:
    """Remove uneven lighting by dividing by a heavily blurred background estimate.

    Phone photos are darker toward one side; Tesseract's global threshold then
    turns the dark side into speckle. Evenly lit pages are returned unchanged,
    because the median filter that follows would soften crisp digital text.
    """
    radius = max(15, image.width // 60)
    background = np.asarray(
        image.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32
    )
    low, high = np.percentile(background, [5, 95])
    if high - low < _UNEVEN_LIGHTING_SPREAD:
        return image
    pixels = np.asarray(image, dtype=np.float32)
    flattened = np.clip(pixels / np.maximum(background, 1.0) * 255.0, 0, 255)
    return Image.fromarray(flattened.astype(np.uint8)).filter(ImageFilter.MedianFilter(3))


def _prepare_ocr_image(image: Image.Image) -> tuple[Image.Image, float]:
    """Return a grayscale, contrast-stretched, upscaled, deskewed copy for OCR only.

    The source raster is never modified. The returned angle is the counter-clockwise
    rotation applied about the image center, so boxes can be mapped back.
    """
    prepared = ImageOps.autocontrast(image.convert("L"))
    if prepared.width < _UPSCALE_BELOW_WIDTH:
        scale = min(
            _OCR_TARGET_WIDTH / prepared.width,
            math.sqrt(MAX_RASTER_PIXELS / (prepared.width * prepared.height)),
        )
        if scale > 1.05:
            prepared = prepared.resize(
                (round(prepared.width * scale), round(prepared.height * scale)),
                Image.Resampling.LANCZOS,
            )
    prepared = _flatten_illumination(prepared)
    angle = _estimate_skew_degrees(prepared)
    if angle:
        prepared = prepared.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
    return prepared, angle


def _unrotate_box(
    box: tuple[int, int, int, int], angle: float, width: int, height: int
) -> tuple[float, float, float, float]:
    """Map a box from the deskewed OCR image back to the unrotated page, normalized."""
    left, top, right, bottom = box
    if angle:
        theta = math.radians(angle)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        cx, cy = width / 2, height / 2
        points = [
            (cx + (x - cx) * cos_t - (y - cy) * sin_t, cy + (x - cx) * sin_t + (y - cy) * cos_t)
            for x, y in ((left, top), (right, top), (left, bottom), (right, bottom))
        ]
        left = min(x for x, _ in points)
        right = max(x for x, _ in points)
        top = min(y for _, y in points)
        bottom = max(y for _, y in points)
    return (
        max(0.0, left / width),
        max(0.0, top / height),
        min(1.0, right / width),
        min(1.0, bottom / height),
    )


def _pdf_text_layer_lines(
    document: pdfium.PdfDocument, page_index: int
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Return (text, normalized box) per embedded text line, or [] if unusable.

    Rotated pages are skipped (OCR is used instead) so that boxes always line up
    with the rendered raster that redaction masks.
    """
    page = document[page_index]
    textpage = page.get_textpage()
    try:
        if page.get_rotation() % 360:
            return []
        left, bottom, right, top = page.get_cropbox()
        width, height = right - left, top - bottom
        if width <= 0 or height <= 0 or textpage.count_chars() < _TEXT_LAYER_MIN_CHARS:
            return []
        lines: list[tuple[str, tuple[float, float, float, float]]] = []
        # (character, box or None for whitespace) for the line being read
        line: list[tuple[str, tuple[float, float, float, float] | None]] = []

        def emit(segment: list[tuple[str, tuple[float, float, float, float] | None]]) -> None:
            text = re.sub(r"\s+", " ", "".join(ch for ch, _ in segment)).strip()
            boxes = [b for _, b in segment if b is not None]
            if not text or not boxes:
                return
            x0 = (min(b[0] for b in boxes) - left) / width
            x1 = (max(b[2] for b in boxes) - left) / width
            y0 = (top - max(b[3] for b in boxes)) / height
            y1 = (top - min(b[1] for b in boxes)) / height
            box = (max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1))
            if box[0] < box[2] and box[1] < box[3]:
                lines.append((text, box))

        def flush() -> None:
            inked = [(i, b) for i, (_, b) in enumerate(line) if b is not None]
            heights = sorted(b[3] - b[1] for _, b in inked)
            median = heights[len(heights) // 2] if heights else 0.0
            start = 0
            for (prev_i, prev_b), (next_i, next_b) in zip(inked, inked[1:]):
                gap = next_b[0] - prev_b[2]
                rest = "".join(ch for ch, _ in line[next_i:])
                if median and gap > _FIELD_GAP_HEIGHTS * median and _FIELD_LABEL_START.match(rest):
                    emit(line[start:next_i])
                    start = next_i
            emit(line[start:])
            line.clear()

        for index in range(textpage.count_chars()):
            character = textpage.get_text_range(index, 1)
            if character in "\r\n":
                flush()
                continue
            line.append((character, None if character.isspace() else textpage.get_charbox(index)))
        flush()
        return lines
    finally:
        textpage.close()
        page.close()


def _overlap_fraction(
    box: tuple[float, float, float, float],
    others: list[tuple[float, float, float, float]],
) -> float:
    """Share of an OCR line's width covered by text-layer lines on the same row.

    Measured along the row rather than by area, because OCR often merges two
    columns into one wide line whose box is mostly the empty gap between them.
    """
    width, height = box[2] - box[0], box[3] - box[1]
    if width <= 0 or height <= 0:
        return 1.0
    spans = sorted(
        (max(box[0], o[0]), min(box[2], o[2]))
        for o in others
        if min(box[3], o[3]) - max(box[1], o[1]) >= 0.5 * min(height, o[3] - o[1])
        and min(box[2], o[2]) > max(box[0], o[0])
    )
    covered, end = 0.0, box[0]
    for start, stop in spans:
        start = max(start, end)
        if stop > start:
            covered += stop - start
            end = stop
    return min(1.0, covered / width)


def _text_layer_candidate(
    text: str,
    box: tuple[float, float, float, float],
    *,
    report_id: UUID,
    extraction_job_id: UUID,
    page_number: int,
    extracted_at: datetime,
) -> CandidateExtraction:
    analyte_name, raw_value, raw_unit, reference_interval, abnormal_flag = _parse_result_line(text)
    confidence = _TEXT_LAYER_CONFIDENCE
    return CandidateExtraction(
        candidate_id=uuid4(),
        report_id=report_id,
        extraction_job_id=extraction_job_id,
        row_index=0,
        raw_analyte_name=analyte_name,
        proposed_analyte_code="UNMAPPED",
        raw_value=raw_value,
        raw_unit=raw_unit,
        reference_interval=reference_interval,
        method_note=None,
        specimen_type=None,
        abnormal_flag=abnormal_flag,
        page_number=page_number,
        bounding_box=box,
        region_available=True,
        field_confidences={
            "analyte": confidence,
            "value": confidence if raw_value is not None else 0.0,
            "unit": confidence if raw_unit is not None else 0.0,
            "region": confidence,
        },
        model_identifier=_TEXT_LAYER_MODEL,
        model_version=f"pdfium-{pdfium.PDFIUM_INFO.build}",
        prompt_hash=sha256(b"").hexdigest(),
        extracted_at=extracted_at,
        routing_status="MANDATORY_EXCEPTION_REVIEW",
        lifecycle_status="CANDIDATE",
    )


def extract_pdf_ocr_line_candidates(
    *,
    pdf_bytes: bytes,
    report_id: UUID,
    extraction_job_id: UUID,
    extracted_at: datetime,
    dpi: int = 150,
) -> CandidateExtractionResult:
    """Run the conservative OCR-line pass over every page in a PDF."""
    _validate_upload_bytes(pdf_bytes, "PDF source")
    if not isinstance(extracted_at, datetime) or extracted_at.utcoffset() is None:
        raise ValueError("Extraction timestamp must be timezone-aware.")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 600:
        raise ValueError("Rasterization DPI must be an integer from 72 to 600.")

    document = None
    try:
        document = pdfium.PdfDocument(pdf_bytes)
        page_count = len(document)
        if page_count < 1:
            raise ValueError("PDF source must contain at least one page.")
        text_layers = [_pdf_text_layer_lines(document, i) for i in range(page_count)]
    except (pdfium.PdfiumError, OSError) as error:
        raise ValueError("PDF source could not be opened.") from error
    finally:
        if document is not None:
            document.close()

    page_results = []
    for page_number in range(1, page_count + 1):
        raster = rasterize_pdf_page(pdf_bytes, page_number, dpi=dpi)
        ocr_result = extract_ocr_line_candidates(
            page_raster=raster,
            report_id=report_id,
            extraction_job_id=extraction_job_id,
            page_number=page_number,
            extracted_at=extracted_at,
        )
        text_lines = text_layers[page_number - 1]
        if not text_lines:
            page_results.append(ocr_result)
            continue
        text_boxes = [box for _, box in text_lines]
        merged = [
            _text_layer_candidate(
                text, box, report_id=report_id, extraction_job_id=extraction_job_id,
                page_number=page_number, extracted_at=extracted_at,
            )
            for text, box in text_lines
        ] + [
            candidate
            for candidate in ocr_result.candidates
            if candidate.bounding_box is None
            or _overlap_fraction(candidate.bounding_box, text_boxes) < _OCR_DUPLICATE_OVERLAP
        ]
        merged.sort(key=lambda c: (c.bounding_box[1], c.bounding_box[0]) if c.bounding_box else (2.0, 0.0))
        candidates = tuple(replace(c, row_index=i) for i, c in enumerate(merged))
        page_results.append(
            CandidateExtractionResult(
                report_id=report_id,
                extraction_job_id=extraction_job_id,
                candidates=candidates,
                page_inventories=(
                    ExtractionPageInventory(
                        page_number=page_number,
                        source_page_sha256=sha256(raster).hexdigest(),
                        result_row_indices=tuple(range(len(candidates))),
                        inventory_status="INCOMPLETE",
                    ),
                ),
            )
        )
    return CandidateExtractionResult(
        report_id=report_id,
        extraction_job_id=extraction_job_id,
        candidates=tuple(
            candidate
            for page_result in page_results
            for candidate in page_result.candidates
        ),
        page_inventories=tuple(
            inventory
            for page_result in page_results
            for inventory in page_result.page_inventories
        ),
    )


def extract_document_ocr_line_candidates(
    *,
    source_bytes: bytes,
    mime_type: str,
    report_id: UUID,
    extraction_job_id: UUID,
    extracted_at: datetime,
    dpi: int = 150,
) -> CandidateExtractionResult:
    """Dispatch an in-memory PDF, PNG, or JPEG to the conservative OCR prototype."""
    _validate_upload_bytes(source_bytes, "Uploaded document")
    if not isinstance(mime_type, str):
        raise ValueError("Uploaded document MIME type must be a string.")
    normalized_mime_type = mime_type.split(";", maxsplit=1)[0].strip().lower()
    if normalized_mime_type == "application/pdf":
        return extract_pdf_ocr_line_candidates(
            pdf_bytes=source_bytes,
            report_id=report_id,
            extraction_job_id=extraction_job_id,
            extracted_at=extracted_at,
            dpi=dpi,
        )

    expected_image_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
    }.get(normalized_mime_type)
    if expected_image_format is None:
        raise ValueError("Only PDF, PNG, and JPEG reports are supported.")

    try:
        with Image.open(BytesIO(source_bytes)) as image:
            if image.format != expected_image_format:
                raise ValueError("Uploaded document format does not match its MIME type.")
            if image.width * image.height > MAX_RASTER_PIXELS:
                raise ValueError("Uploaded image dimensions exceed the rasterization limit.")
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError("Uploaded image could not be decoded.") from error

    return extract_ocr_line_candidates(
        page_raster=source_bytes,
        report_id=report_id,
        extraction_job_id=extraction_job_id,
        page_number=1,
        extracted_at=extracted_at,
    )


def extract_ocr_line_candidates(
    *,
    page_raster: bytes,
    report_id: UUID,
    extraction_job_id: UUID,
    page_number: int,
    extracted_at: datetime,
) -> CandidateExtractionResult:
    """Return OCR text lines as unparsed candidates and mark page coverage incomplete.

    OCR lines are not classified as laboratory result rows. Every candidate is
    routed to mandatory review, and this prototype never asserts complete coverage.
    """
    if not isinstance(page_raster, bytes) or not page_raster:
        raise ValueError("Source page raster must be non-empty bytes.")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ValueError("Page number must be a positive integer.")
    if not isinstance(extracted_at, datetime) or extracted_at.utcoffset() is None:
        raise ValueError("Extraction timestamp must be timezone-aware.")

    try:
        with Image.open(BytesIO(page_raster)) as source:
            if source.width * source.height > MAX_RASTER_PIXELS:
                raise ValueError("Source page dimensions exceed the rasterization limit.")
            source.load()
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("Animated images cannot be used as source pages.")
            image = source.convert("RGB")
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError("Source page must be a decodable raster image.") from error

    ocr_image, skew_angle = _prepare_ocr_image(image)
    data = pytesseract.image_to_data(
        ocr_image,
        config=_TESSERACT_CONFIG,
        output_type=Output.DICT,
    )
    lines: dict[tuple[int, int, int], list[tuple[int, int, int, int, str, float]]] = (
        defaultdict(list)
    )
    for index, raw_text in enumerate(data["text"]):
        text = str(raw_text).strip()
        confidence = float(data["conf"][index])
        if not text or confidence < 0:
            continue
        line_key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        left = int(data["left"][index])
        top = int(data["top"][index])
        right = left + int(data["width"][index])
        bottom = top + int(data["height"][index])
        lines[line_key].append((left, top, right, bottom, text, confidence))

    ordered_lines = sorted(
        lines.values(),
        key=lambda words: (
            min(word[1] for word in words),
            min(word[0] for word in words),
        ),
    )
    candidates: list[CandidateExtraction] = []
    image_width, image_height = ocr_image.size
    model_version = str(pytesseract.get_tesseract_version())
    prompt_hash = sha256(b"").hexdigest()

    for words in ordered_lines:
        words.sort(key=lambda word: word[0])
        text = " ".join(word[4] for word in words)
        analyte_name, raw_value, raw_unit, reference_interval, abnormal_flag = (
            _parse_result_line(text)
        )
        left = max(0, min(word[0] for word in words))
        top = max(0, min(word[1] for word in words))
        right = min(image_width, max(word[2] for word in words))
        bottom = min(image_height, max(word[3] for word in words))
        if right <= left or bottom <= top:
            continue
        confidence = min(
            1.0,
            max(0.0, sum(word[5] for word in words) / len(words) / 100),
        )
        candidates.append(
            CandidateExtraction(
                candidate_id=uuid4(),
                report_id=report_id,
                extraction_job_id=extraction_job_id,
                row_index=len(candidates),
                raw_analyte_name=analyte_name,
                proposed_analyte_code="UNMAPPED",
                raw_value=raw_value,
                raw_unit=raw_unit,
                reference_interval=reference_interval,
                method_note=None,
                specimen_type=None,
                abnormal_flag=abnormal_flag,
                page_number=page_number,
                bounding_box=_unrotate_box(
                    (left, top, right, bottom), skew_angle, image_width, image_height
                ),
                region_available=True,
                field_confidences={
                    "analyte": confidence,
                    "value": confidence if raw_value is not None else 0.0,
                    "unit": confidence if raw_unit is not None else 0.0,
                    "region": confidence,
                },
                model_identifier="tesseract-ocr-line",
                model_version=model_version,
                prompt_hash=prompt_hash,
                extracted_at=extracted_at,
                routing_status="MANDATORY_EXCEPTION_REVIEW",
                lifecycle_status="CANDIDATE",
            )
        )

    return CandidateExtractionResult(
        report_id=report_id,
        extraction_job_id=extraction_job_id,
        candidates=tuple(candidates),
        page_inventories=(
            ExtractionPageInventory(
                page_number=page_number,
                source_page_sha256=sha256(page_raster).hexdigest(),
                result_row_indices=tuple(range(len(candidates))),
                inventory_status="INCOMPLETE",
            ),
        ),
    )
