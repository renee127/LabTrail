"""Demo workflow glue: turns extraction output into reviewed, charted, shareable records.

This module holds the small amount of policy the demo needs (analyte names, a
versioned unit table, and how a patient decision becomes an AcceptedObservation)
so that the Streamlit UI in app.py stays thin and the rules stay testable.
"""

import csv
import difflib
import json
import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
ANALYTE_TABLE_PATH = Path(__file__).with_name("reference") / "lab_tests.csv"


def load_analyte_table(path: Path) -> dict[str, str]:
    """Read the analyte code -> display name table, rejecting malformed rows."""
    labels: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            code = (row.get("Analyte Code") or "").strip()
            name = (row.get("Test Name") or "").strip()
            if not re.fullmatch(r"[A-Z0-9_]+", code) or not name:
                raise ValueError(f"{path.name} line {line}: invalid analyte code or name.")
            if code in labels:
                raise ValueError(f"{path.name} line {line}: duplicate code {code}.")
            labels[code] = name
    return labels


ANALYTE_LABELS: dict[str, str] = load_analyte_table(ANALYTE_TABLE_PATH)
ANALYTE_TABLE_VERSION = (
    f"lab_tests.csv@{sha256(ANALYTE_TABLE_PATH.read_bytes()).hexdigest()[:12]}"
)
# Printed names seen on real reports that the table's names do not cover.
_EXTRA_ALIASES: dict[str, str] = {
    "glucose, serum": "GLUCOSE",
    "fasting glucose": "GLUCOSE",
    "a1c": "HBA1C",
    "ldl cholesterol": "LDL_C",
    "ldl c": "LDL_C",
    "hdl cholesterol": "HDL_C",
    "hdl c": "HDL_C",
    "vldl cholesterol": "VLDL_C",
    "cholesterol": "CHOL_TOTAL",
    "total cholesterol/hdl": "CHOL_HDL_RATIO",
    "total cholesterol / hdl": "CHOL_HDL_RATIO",
    "hemoglobin": "HGB",
    "platelets": "PLT",
    "vitamin d": "VITAMIN_D_25OH",
}
# Parenthetical text too generic to identify one test on its own.
_GENERIC_QUALIFIERS = {
    "absolute", "%", "total", "free", "urine", "stool", "am", "pm", "screen",
    "microscopic", "intact", "quantitative", "pregnancy qualitative",
}
CANONICAL_UNITS: dict[str, str] = {
    "GLUCOSE": "mg/dL",
    "HBA1C": "%",
    "CHOL_TOTAL": "mg/dL",
    "HDL_C": "mg/dL",
    "LDL_C": "mg/dL",
    "VLDL_C": "mg/dL",
    "NON_HDL_C": "mg/dL",
    "TRIGLYCERIDES": "mg/dL",
}
# Factors multiply the original value. HbA1c mmol/mol -> % is affine
# (NGSP = 0.0915 * IFCC + 2.15), so it is intentionally absent: such values are
# kept as reported and listed as unconvertible instead of being plotted.
_CHOLESTEROL_MMOL_TO_MG = Decimal("38.67")
CONVERSION_FACTORS: dict[tuple[str, str, str], Decimal] = {
    ("GLUCOSE", "mmol/L", "mg/dL"): Decimal("18.016"),
    ("CHOL_TOTAL", "mmol/L", "mg/dL"): _CHOLESTEROL_MMOL_TO_MG,
    ("HDL_C", "mmol/L", "mg/dL"): _CHOLESTEROL_MMOL_TO_MG,
    ("LDL_C", "mmol/L", "mg/dL"): _CHOLESTEROL_MMOL_TO_MG,
    ("VLDL_C", "mmol/L", "mg/dL"): _CHOLESTEROL_MMOL_TO_MG,
    ("NON_HDL_C", "mmol/L", "mg/dL"): _CHOLESTEROL_MMOL_TO_MG,
    ("TRIGLYCERIDES", "mmol/L", "mg/dL"): Decimal("88.57"),
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


_ANALYTE_NAME_PATTERN = re.compile(
    "|".join(re.escape(name) for name in sorted(ANALYTE_LABELS.values(), key=len, reverse=True)),
    re.IGNORECASE,
)


def find_interpretive_language(text: str, extra_names: Iterable[str] = ()) -> list[str]:
    """Return interpretive words in system-generated text (FR-13 scanner).

    The fixed non-interpretation notices and the names of tests (for example
    "High-Density Lipoprotein", plus any names the patient added) are exempt.
    Patient-authored note text should not be passed in; the rule applies to what
    LabTrail itself says.
    """
    for notice in _NON_INTERPRETATION_NOTICES:
        text = text.replace(notice, " ")
    for name in sorted(extra_names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    text = _ANALYTE_NAME_PATTERN.sub(" ", text)
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


def _alias_key(text: str) -> str:
    text = text.translate(_OCR_LOOKALIKES).replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip(" .:;,").lower()


def _build_aliases(labels: dict[str, str]) -> dict[str, str]:
    """Map printed-name variants to codes; drop any variant two tests share."""
    candidates: dict[str, set[str]] = {}
    for code, name in labels.items():
        variants = {name, code, re.sub(r"\s*\([^)]*\)", "", name)}
        for inner in re.findall(r"\(([^)]*)\)", name):
            if inner.strip().lower() not in _GENERIC_QUALIFIERS:
                variants.add(inner)
        for variant in variants:
            key = _alias_key(variant)
            if key:
                candidates.setdefault(key, set()).add(code)
    aliases = {key: codes.pop() for key, codes in candidates.items() if len(codes) == 1}
    aliases.update({_alias_key(k): v for k, v in _EXTRA_ALIASES.items() if v in labels})
    return aliases


_ANALYTE_ALIASES: dict[str, str] = _build_aliases(ANALYTE_LABELS)


def _fuzzy_match(key: str, aliases: Mapping[str, str]) -> str | None:
    """Close match restricted to aliases sharing the exact first word.

    Near-identical names for different tests (HDL vs LDL) are never conflated,
    and nothing is returned when two different tests are about equally close
    (Troponin I vs T).
    """
    first_word = key.split(" ", 1)[0]
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, key, alias).ratio(), alias)
            for alias in aliases
            if alias.split(" ", 1)[0] == first_word
        ),
        reverse=True,
    )
    scored = [(score, alias) for score, alias in scored if score >= 0.88]
    if not scored:
        return None
    best_code = aliases[scored[0][1]]
    if any(
        aliases[alias] != best_code and scored[0][0] - score < 0.03
        for score, alias in scored[1:]
    ):
        return None
    return best_code


def suggest_analyte_code(
    raw_name: str, custom_analytes: Mapping[str, str] | None = None
) -> str | None:
    """Suggest an analyte code from the printed name, or None if unknown.

    Checks the reference table first, then tests the patient added. Tolerates
    common OCR look-alike errors ("Cholestero!", "Hemoglobin Aic") with a strict
    similarity cutoff. This is only a suggestion; the patient confirms it.
    """
    key = _alias_key(raw_name)
    custom_aliases = {_alias_key(name): code for code, name in (custom_analytes or {}).items()}
    for aliases in (_ANALYTE_ALIASES, custom_aliases):
        if key in aliases:
            return aliases[key]
    for aliases in (_ANALYTE_ALIASES, custom_aliases):
        match = _fuzzy_match(key, aliases)
        if match:
            return match
    return None


CUSTOM_CODE_PREFIX = "CUSTOM_"
_MAX_CUSTOM_NAME = 80


def all_analyte_labels(custom_analytes: Mapping[str, str] | None = None) -> dict[str, str]:
    """Reference-table tests plus tests the patient added."""
    return {**ANALYTE_LABELS, **(custom_analytes or {})}


def add_custom_analyte(
    name: str, custom_analytes: MutableMapping[str, str]
) -> tuple[str, bool]:
    """Add a patient-named test, or return the existing test with that name.

    Returns (code, created). A name that exactly matches a reference test, one of
    its abbreviations, or an earlier added test reuses that test rather than
    creating a near-duplicate.
    """
    clean = re.sub(r"\s+", " ", name).strip()
    if len(clean) < 2 or not any(ch.isalpha() for ch in clean):
        raise ValueError("Enter the test name as printed on the report.")
    if len(clean) > _MAX_CUSTOM_NAME:
        raise ValueError(f"Test names are limited to {_MAX_CUSTOM_NAME} characters.")
    if any(not ch.isprintable() for ch in clean):
        raise ValueError("The test name contains characters that cannot be stored.")
    key = _alias_key(clean)
    if key in _ANALYTE_ALIASES:
        return _ANALYTE_ALIASES[key], False
    for code, existing in custom_analytes.items():
        if _alias_key(existing) == key:
            return code, False
    slug = re.sub(r"[^A-Z0-9]+", "_", clean.upper()).strip("_")[:40] or "TEST"
    code, n = f"{CUSTOM_CODE_PREFIX}{slug}", 2
    while code in ANALYTE_LABELS or code in custom_analytes:
        code, n = f"{CUSTOM_CODE_PREFIX}{slug}_{n}", n + 1
    custom_analytes[code] = clean
    return code, True


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


# Identification and administrative fields printed on lab reports. Each pattern
# must match at the START of an OCR line (labels such as "DOB:" or "Referred by"),
# so a result line that merely mentions a word later on is never caught.
_IDENTIFICATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.IGNORECASE))
    for category, pattern in (
        ("date of birth", r"^(?:dob|d\.\s?o\.\s?b\.?|date\s+of\s+birth|birth\s*date)\b"),
        ("name", r"^(?:patient(?:'s)?\s*)?(?:first|last|middle|full|given|family|sur)?\s*name\b"),
        ("name", r"^patient\s*(?::|name\b|details\b|information\b)"),
        ("name", r"^(?:mr|mrs|ms|miss|mx|master)\.?\s+[a-z]"),
        ("age / sex", r"^(?:age|sex|gender)\b"),
        ("identifier", r"^(?:mrn|uhid|medical\s+record|patient\s*(?:id|no|number|#)|"
                       r"id\b|i\.d\.|accession|acc(?:ession)?\.?\s*(?:no|#)|"
                       r"(?:specimen|sample|lab|barcode|visit|encounter|account|order|"
                       r"requisition|member|policy|reg(?:istration)?)\.?\s*(?:id|no|number|#)\b|"
                       r"ssn\b|social\s+security|insurance)"),
        ("address", r"^(?:address|street|city|state|zip|postal\s*code|country|apt|suite)\b"),
        ("address", r"^\d+\s+[a-z].*\b(?:street|avenue|ave|road|rd|boulevard|blvd|lane|ln|"
                    r"drive|court|way|place|terrace)\b\.?"),
        ("contact", r"^(?:phone|tel(?:ephone)?|mobile|fax|e-?mail|contact)\b"),
        ("contact", r"^cell\s*(?:phone\b|no\b|number\b|#|:)"),
        ("report date", r"^(?:collected|collection|received|reported|registered|printed|"
                        r"report\s+(?:date|status)|sample\s+(?:date|time)|date|time)\b"),
        ("clinician", r"^(?:dr\.?\s+[a-z]|referred\s+by|referring|ordering|ordered\s+by|"
                      r"requested\s+by|physician|doctor|provider|attending|pathologist|"
                      r"lab\s+(?:director|incharge|in-charge)|verified\s+by|approved\s+by|"
                      r"technologist|signature)"),
        ("page footer", r"^page\s+\d+(?:\s+of\s+\d+)?\b"),
    )
)


def identification_category(
    candidate: CandidateExtraction, custom_analytes: Mapping[str, str] | None = None
) -> str | None:
    """Return why a line looks like an identification field, or None.

    A line whose name is recognised as a lab test is never classified, so a real
    result cannot be rejected by these rules.
    """
    if suggest_analyte_code(candidate.raw_analyte_name, custom_analytes):
        return None
    # Rebuild the whole line: "Page 1 of 2" parses as name "Page", value "1", unit "of".
    text = " ".join(
        part for part in (
            candidate.raw_analyte_name, candidate.raw_value, candidate.raw_unit,
            candidate.reference_interval,
        ) if part
    ).strip()
    for category, pattern in _IDENTIFICATION_PATTERNS:
        if pattern.search(text):
            return category
    return None


def auto_reject_identification_lines(
    result: CandidateExtractionResult, custom_analytes: Mapping[str, str] | None = None
) -> dict[UUID, str]:
    """Reject pending identification lines; return {candidate_id: category}.

    Rejection is recorded so the patient can see each line and restore it.
    """
    rejected: dict[UUID, str] = {}
    for candidate in result.candidates:
        if candidate.lifecycle_status != "CANDIDATE":
            continue
        category = identification_category(candidate, custom_analytes)
        if category:
            candidate.lifecycle_status = "REJECTED"
            rejected[candidate.candidate_id] = category
    return rejected


_CENSORED_VALUE = re.compile(r"^[<>≤≥]\s*\d+(?:\.\d+)?$")


def _is_number(value: str) -> bool:
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _rule_unit_spelling(analyte_code: str, unit: str) -> str:
    """Match a printed unit to the rule table's spelling, ignoring case only."""
    known = {CANONICAL_UNITS.get(analyte_code)} | {
        source for (code, source, _), _f in CONVERSION_FACTORS.items() if code == analyte_code
    }
    return next((k for k in known if k and k.casefold() == unit.casefold()), unit)


def _retained(value: str, unit: str, reason: str):
    return {
        "canonical_value": None, "canonical_unit": None, "conversion_factor": None,
        "conversion_outcome": "UNCONVERTIBLE_RETAINED", "unconvertible_reason": reason,
        "rule_version": RULE_VERSION,
    }


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
    custom_analytes: Mapping[str, str] | None = None,
) -> AcceptedObservation:
    """Turn a patient's accept/correct decision into an AcceptedObservation.

    The patient-supplied fields are authoritative. If any differ from what the
    extractor proposed, the observation is marked CORRECTED_BY_PATIENT.
    """
    if analyte_code not in ANALYTE_LABELS and analyte_code not in (custom_analytes or {}):
        raise ValueError("Choose a test from the list, or enter its name, before accepting.")
    value = value.strip()
    unit = unit.strip()
    reference_interval = (reference_interval or "").strip() or None
    if candidate.lifecycle_status != "CANDIDATE":
        raise ValueError("Only pending candidates can be accepted.")

    if not value:
        raise ValueError("Enter the value printed on the report.")
    if _is_number(value):
        if not unit and analyte_code in CANONICAL_UNITS:
            raise ValueError("Enter the unit printed on the report.")
        if not unit:
            conversion = _retained(value, unit, "NO_CANONICAL_UNIT_DEFINED")
        else:
            result = normalize_value(
                analyte_code=analyte_code,
                original_value=value,
                original_unit=_rule_unit_spelling(analyte_code, unit),
                canonical_units=CANONICAL_UNITS,
                conversion_factors=CONVERSION_FACTORS,
                rule_version=RULE_VERSION,
            )
            conversion = {
                "canonical_value": result.canonical_value,
                "canonical_unit": result.canonical_unit,
                "conversion_factor": result.conversion_factor,
                "conversion_outcome": result.conversion_outcome,
                "unconvertible_reason": result.unconvertible_reason,
                "rule_version": result.rule_version,
            }
    elif _CENSORED_VALUE.match(value) or not any(ch.isdigit() for ch in value):
        # Text results ("Negative", "Yellow") and limits ("<0.5") are kept as printed.
        conversion = _retained(value, unit, "NON_NUMERIC_RESULT")
    else:
        raise ValueError(
            f"“{value}” is not a number. Correct any misread characters, or enter the "
            "result as text exactly as printed."
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
        canonical_value=conversion["canonical_value"],
        canonical_unit=conversion["canonical_unit"],
        original_value=value,
        original_unit=unit,
        conversion_factor=conversion["conversion_factor"],
        conversion_outcome=conversion["conversion_outcome"],
        unconvertible_reason=conversion["unconvertible_reason"],
        reference_interval=reference_interval,
        method_note=candidate.method_note,
        source_lab=source_lab,
        collection_datetime=collected,
        page_number=candidate.page_number,
        bounding_box=candidate.bounding_box,
        provenance_flag="CORRECTED_BY_PATIENT" if corrected else "AS_EXTRACTED",
        lifecycle_status="ACCEPTED",
        rule_version=conversion["rule_version"],
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


def reopen_page_coverage(
    result: CandidateExtractionResult, page_number: int
) -> CandidateExtractionResult:
    """Withdraw a page's completeness confirmation (e.g. a line was restored for review)."""
    return replace(
        result,
        page_inventories=tuple(
            replace(inv, inventory_status="INCOMPLETE")
            if inv.page_number == page_number
            else inv
            for inv in result.page_inventories
        ),
    )


def line_text(candidate: CandidateExtraction) -> str:
    """The OCR line as one string, for display."""
    return " ".join(
        part for part in (
            candidate.raw_analyte_name, candidate.raw_value, candidate.abnormal_flag,
            candidate.raw_unit, candidate.reference_interval,
        ) if part
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


@dataclass(frozen=True)
class ChartPlan:
    """Which values of one analyte can be drawn on a single axis, and in what unit."""

    unit: str | None
    as_reported: bool
    plotted: tuple[int, ...]
    listed: tuple[int, ...]


def plan_chart(rows: list[dict]) -> ChartPlan:
    """Decide what to plot from rows with canonical/original value and unit keys.

    Converted values share the canonical unit and are plotted. If the analyte has
    no unit rule at all, numeric values are plotted as reported only when every one
    uses the same unit; nothing is ever converted without a versioned rule.
    Everything not plotted is listed so the patient still sees it.
    """
    converted = [i for i, r in enumerate(rows) if r["canonical_value"] is not None]
    if converted:
        unit = rows[converted[0]]["canonical_unit"]
        listed = tuple(i for i in range(len(rows)) if i not in converted)
        return ChartPlan(unit, False, tuple(converted), listed)
    numeric = [
        i for i, r in enumerate(rows)
        if r["unconvertible_reason"] == "NO_CANONICAL_UNIT_DEFINED" and _is_number(r["original_value"])
    ]
    units = {rows[i]["original_unit"].casefold() for i in numeric}
    if numeric and len(units) == 1:
        listed = tuple(i for i in range(len(rows)) if i not in numeric)
        return ChartPlan(rows[numeric[0]]["original_unit"], True, tuple(numeric), listed)
    return ChartPlan(None, False, (), tuple(range(len(rows))))


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
