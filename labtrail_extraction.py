"""Conservative local OCR-line extraction prototype for LabTrail."""

import json
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, UnidentifiedImageError
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
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_RESULT_LINE_PATTERN = re.compile(
    r"^(?P<analyte>.+?)\s+"
    r"(?P<value>(?:[<>≤≥]\s*)?[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s+"
    r"(?P<unit>[A-Za-zµμ%][A-Za-z0-9µμ%./^*+-]*)(?:\s+.*)?$"
)
_RESULT_SCHEMA = json.loads(
    Path(__file__)
    .with_name("candidate_extraction_result_schema_v1.json")
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


def _parse_result_line(text: str) -> tuple[str, str | None, str | None]:
    match = _RESULT_LINE_PATTERN.fullmatch(text)
    if match is None:
        return text, None, None
    analyte = match.group("analyte").strip()
    if not analyte or not any(character.isalpha() for character in analyte):
        return text, None, None
    return analyte, match.group("value").replace(" ", ""), match.group("unit")


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
    except (pdfium.PdfiumError, OSError) as error:
        raise ValueError("PDF source could not be opened.") from error
    finally:
        if document is not None:
            document.close()

    page_results = tuple(
        extract_ocr_line_candidates(
            page_raster=rasterize_pdf_page(pdf_bytes, page_number, dpi=dpi),
            report_id=report_id,
            extraction_job_id=extraction_job_id,
            page_number=page_number,
            extracted_at=extracted_at,
        )
        for page_number in range(1, page_count + 1)
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

    data = pytesseract.image_to_data(
        image,
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
    image_width, image_height = image.size
    model_version = str(pytesseract.get_tesseract_version())
    prompt_hash = sha256(b"").hexdigest()

    for words in ordered_lines:
        words.sort(key=lambda word: word[0])
        text = " ".join(word[4] for word in words)
        analyte_name, raw_value, raw_unit = _parse_result_line(text)
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
                reference_interval=None,
                method_note=None,
                specimen_type=None,
                abnormal_flag=None,
                page_number=page_number,
                bounding_box=(
                    left / image_width,
                    top / image_height,
                    right / image_width,
                    bottom / image_height,
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
