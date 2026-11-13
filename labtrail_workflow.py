"""Demo workflow glue: turns extraction output into reviewed, charted, shareable records.

This module holds the small amount of policy the demo needs (analyte names, a
versioned unit table, and how a patient decision becomes an AcceptedObservation)
so that the Streamlit UI in app.py stays thin and the rules stay testable.
"""

import difflib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

from labtrail_algorithms import (
    ComparabilityAttribute,
    build_share_payload_projection,
    compare_adjacent_observations,
    normalize_value,
    prepare_share_content_selection,
    serialize_share_payload_projection,
    ShareContentSelection,
)
from models import (
    AcceptedObservation,
    CandidateExtraction,
    CandidateExtractionResult,
    ClientShareGrant,
    ContextNote,
)


# Versioned reference data. Patients choose the analyte; these tables only
# suggest a mapping and convert units deterministically.
RULE_VERSION = "demo-units-2026.11"

ANALYTE_LABELS: dict[str, str] = {
    "GLUCOSE": "Glucose",
    "HBA1C": "Hemoglobin A1c",
    "LDL_C": "LDL Cholesterol",
}
_ANALYTE_ALIASES: dict[str, str] = {
    "glucose": "GLUCOSE",
    "glucose, serum": "GLUCOSE",
    "hemoglobin a1c": "HBA1C",
    "hba1c": "HBA1C",
    "a1c": "HBA1C",
    "ldl cholesterol": "LDL_C",
    "ldl-c": "LDL_C",
    "ldl": "LDL_C",
}
CANONICAL_UNITS: dict[str, str] = {
    "GLUCOSE": "mg/dL",
    "HBA1C": "%",
    "LDL_C": "mg/dL",
}
# Factors multiply the original value. HbA1c mmol/mol -> % is affine
# (NGSP = 0.0915 * IFCC + 2.15), so it is intentionally absent: such values are
# kept as reported and listed as unconvertible instead of being plotted.
CONVERSION_FACTORS: dict[tuple[str, str, str], Decimal] = {
    ("GLUCOSE", "mmol/L", "mg/dL"): Decimal("18.016"),
    ("LDL_C", "mmol/L", "mg/dL"): Decimal("38.67"),
}

COMPARABILITY_LABELS: dict[str, str] = {
    "source_lab": "different lab",
    "method_note": "different method",
    "unit_basis": "different reported unit",
    "reference_interval": "different reference range",
}

# FR-13: LabTrail organizes values and must never interpret them. These are the
# only sentences allowed to mention interpretation, and only to disclaim it.
SIDEBAR_NOTICE = (
    "Synthetic data only. LabTrail organizes results; it does not diagnose or give "
    "medical advice."
)
TIMELINE_NOTICE = (
    "LabTrail shows what changed between results; it does not say whether a value "
    "is good or bad."
)
_NON_INTERPRETATION_NOTICES = (SIDEBAR_NOTICE, TIMELINE_NOTICE)
_INTERPRETIVE_PATTERN = re.compile(
    r"\b(?:abnormal\w*|normal|healthy|unhealthy|risk\w*|diagnos\w*|prognos\w*"
    r"|elevated|dangerous|concerning|worr\w*|improv\w*|worse\w*|better"
    r"|recommend\w*|you should|see a doctor|treatment\w*|optimal|critical"
    r"|high|low|good|bad)\b",
    re.IGNORECASE,
)


def find_interpretive_language(text: str) -> list[str]:
    """Return interpretive words in system-generated text (FR-13 scanner).

    The fixed non-interpretation notices are exempt. Patient-authored note text
    should not be passed in; the rule applies to what LabTrail itself says.
    """
    for notice in _NON_INTERPRETATION_NOTICES:
        text = text.replace(notice, " ")
    return [match.group(0) for match in _INTERPRETIVE_PATTERN.finditer(text)]


_SHARE_SCHEMA = json.loads(
    Path(__file__).with_name("share_payload_schema_v1.json").read_text(encoding="utf-8")
)
_SHARE_VALIDATOR = Draft202012Validator(_SHARE_SCHEMA, format_checker=FormatChecker())


@dataclass(frozen=True)
class ReportMetadataSuggestion:
    """OCR-derived hints the patient must confirm; never trusted on their own."""

    source_lab: str | None
    patient_name: str | None
    collected: date | None


@dataclass(frozen=True)
class TimelinePoint:
    observation: AcceptedObservation
    changes_from_previous: tuple[ComparabilityAttribute, ...]


_OCR_LOOKALIKES = str.maketrans({"!": "l", "|": "l", "]": "l", "[": "l"})


def suggest_analyte_code(raw_name: str) -> str | None:
    """Suggest an analyte code from the printed name, or None if unknown.

    Tolerates common OCR look-alike errors ("Cholestero!", "Hemoglobin Aic") with a
    strict similarity cutoff. This is only a suggestion; the patient confirms it.
    """
    key = re.sub(r"\s+", " ", raw_name.translate(_OCR_LOOKALIKES)).strip(" .:;,").lower()
    if key in _ANALYTE_ALIASES:
        return _ANALYTE_ALIASES[key]
    # Fuzzy matching only within aliases sharing the exact first word, so that
    # near-identical names for different tests (HDL vs LDL) are never conflated.
    first_word = key.split(" ", 1)[0]
    same_head = [alias for alias in _ANALYTE_ALIASES if alias.split(" ", 1)[0] == first_word]
    close = difflib.get_close_matches(key, same_head, n=1, cutoff=0.88)
    return _ANALYTE_ALIASES[close[0]] if close else None


def suggest_report_metadata(result: CandidateExtractionResult) -> ReportMetadataSuggestion:
    """Pull lab name, patient name, and collection date hints from OCR lines."""
    source_lab = None
    patient_name = None
    collected = None
    first_page = sorted(
        (c for c in result.candidates if c.page_number == 1),
        key=lambda c: c.row_index,
    )
    if first_page:
        source_lab = first_page[0].raw_analyte_name.strip() or None
    for candidate in result.candidates:
        text = candidate.raw_analyte_name.strip()
        patient_match = re.match(r"(?i)^patient\s*:\s*(.+)$", text)
        if patient_match and patient_name is None:
            patient_name = patient_match.group(1).strip()
        date_match = re.match(
            r"(?i)^collect(?:ed|ion date)\s*:\s*(\d{4}-\d{2}-\d{2})$", text
        )
        if date_match and collected is None:
            try:
                collected = date.fromisoformat(date_match.group(1))
            except ValueError:
                pass
    return ReportMetadataSuggestion(source_lab, patient_name, collected)


def is_result_like(candidate: CandidateExtraction) -> bool:
    """True when OCR found a value and unit on the line (worth reviewing as a result)."""
    return bool(candidate.raw_value and candidate.raw_unit)


def accept_candidate(
    candidate: CandidateExtraction,
    *,
    analyte_code: str,
    value: str,
    unit: str,
    reference_interval: str | None,
    source_lab: str,
    collected: date,
    accepted_at: datetime,
) -> AcceptedObservation:
    """Turn a patient's accept/correct decision into an AcceptedObservation.

    The patient-supplied fields are authoritative. If any differ from what the
    extractor proposed, the observation is marked CORRECTED_BY_PATIENT.
    """
    if analyte_code not in ANALYTE_LABELS:
        raise ValueError("Choose a supported analyte before accepting.")
    value = value.strip()
    unit = unit.strip()
    reference_interval = (reference_interval or "").strip() or None
    if candidate.lifecycle_status != "CANDIDATE":
        raise ValueError("Only pending candidates can be accepted.")

    conversion = normalize_value(
        analyte_code=analyte_code,
        original_value=value,
        original_unit=unit,
        canonical_units=CANONICAL_UNITS,
        conversion_factors=CONVERSION_FACTORS,
        rule_version=RULE_VERSION,
    )
    corrected = (
        value != (candidate.raw_value or "")
        or unit != (candidate.raw_unit or "")
        or reference_interval != candidate.reference_interval
    )
    return AcceptedObservation(
        observation_id=uuid4(),
        candidate_id=candidate.candidate_id,
        report_id=candidate.report_id,
        analyte_code=analyte_code,
        canonical_value=conversion.canonical_value,
        canonical_unit=conversion.canonical_unit,
        original_value=value,
        original_unit=unit,
        conversion_factor=conversion.conversion_factor,
        conversion_outcome=conversion.conversion_outcome,
        unconvertible_reason=conversion.unconvertible_reason,
        reference_interval=reference_interval,
        method_note=candidate.method_note,
        source_lab=source_lab,
        collection_datetime=collected,
        page_number=candidate.page_number,
        bounding_box=candidate.bounding_box,
        provenance_flag="CORRECTED_BY_PATIENT" if corrected else "AS_EXTRACTED",
        lifecycle_status="ACCEPTED",
        rule_version=conversion.rule_version,
        accepted_at=accepted_at,
    )


def page_lines_pending(result: CandidateExtractionResult, page_number: int) -> int:
    """Number of extracted lines on a page still awaiting a patient decision."""
    return sum(
        1
        for c in result.candidates
        if c.page_number == page_number and c.lifecycle_status == "CANDIDATE"
    )


def confirm_page_coverage(
    result: CandidateExtractionResult, page_number: int
) -> CandidateExtractionResult:
    """Record the patient's confirmation that a page's line list is complete.

    Design change from the report (which assigned coverage to the extraction
    service): the OCR prototype cannot prove it found every row, so after the
    patient has decided every line and compared the list with the page image, the
    patient attests coverage. Only then may the page be redacted for sharing;
    the redactor's OCR leak check still runs on the result.
    """
    if not any(inv.page_number == page_number for inv in result.page_inventories):
        raise ValueError("That page is not part of this report.")
    if page_lines_pending(result, page_number):
        raise ValueError("Accept or reject every line on the page first.")
    return replace(
        result,
        page_inventories=tuple(
            replace(inv, inventory_status="COMPLETE")
            if inv.page_number == page_number
            else inv
            for inv in result.page_inventories
        ),
    )


def _collection_date(observation: AcceptedObservation) -> date:
    value = observation.collection_datetime
    return value.date() if isinstance(value, datetime) else value


def chronological(observations: Iterable[AcceptedObservation]) -> list[AcceptedObservation]:
    """Accepted (not retracted) observations ordered by collection date, stable."""
    return sorted(
        (o for o in observations if o.lifecycle_status == "ACCEPTED"),
        key=_collection_date,
    )


def analyte_timeline(
    observations: Iterable[AcceptedObservation], analyte_code: str
) -> list[TimelinePoint]:
    """One analyte's history with comparability changes versus the previous value."""
    series = [o for o in chronological(observations) if o.analyte_code == analyte_code]
    points: list[TimelinePoint] = []
    for index, observation in enumerate(series):
        changes = (
            compare_adjacent_observations(series[index - 1], observation)
            if index
            else ()
        )
        points.append(TimelinePoint(observation, changes))
    return points


def recipient_identity_hash(recipient_email: str) -> str:
    return sha256(recipient_email.strip().lower().encode("utf-8")).hexdigest()


def build_share(
    *,
    observations: Iterable[AcceptedObservation],
    notes: Iterable[ContextNote],
    analytes: list[str],
    range_start: date,
    range_end: date,
    recipient_name: str,
    recipient_email: str,
    now: datetime,
    valid_for: timedelta,
) -> tuple[ClientShareGrant, ShareContentSelection, bytes]:
    """Select grant-scoped content and return schema-validated share JSON bytes.

    The bytes are the structured payload only. In the full design they would be
    encrypted on the client before upload; this demo does not encrypt them.
    """
    grant = ClientShareGrant(
        grant_id=uuid4(),
        recipient_name=recipient_name,
        recipient_identity_hash=recipient_identity_hash(recipient_email),
        granted_analytes=list(analytes),
        range_start=range_start,
        range_end=range_end,
        created_at=now,
        expires_at=now + valid_for,
        local_status="PREPARING_CLIENT",
    )
    selection = prepare_share_content_selection(
        observations=chronological(observations),
        notes=notes,
        grant=grant,
        as_of=now,
    )
    previous_by_analyte: dict[str, UUID] = {}
    predecessors: dict[UUID, UUID] = {}
    for observation in selection.observations:
        previous = previous_by_analyte.get(observation.analyte_code)
        if previous is not None:
            predecessors[observation.observation_id] = previous
        previous_by_analyte[observation.analyte_code] = observation.observation_id

    payload = serialize_share_payload_projection(
        build_share_payload_projection(selection, comparability_predecessors=predecessors)
    )
    _SHARE_VALIDATOR.validate(json.loads(payload))
    return grant, selection, payload
