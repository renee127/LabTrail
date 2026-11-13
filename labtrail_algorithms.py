"""Pure algorithms that operate on LabTrail domain objects."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite
from typing import Literal

from models import (
    AcceptedObservation,
    CandidateExtraction,
    ClientShareGrant,
    ContextNote,
    SourceReport,
)


ConversionOutcome = Literal[
    "CANONICAL_DIRECT",
    "CONVERTED_DETERMINISTIC",
    "UNCONVERTIBLE_RETAINED",
]
ComparabilityAttribute = Literal[
    "source_lab",
    "method_note",
    "unit_basis",
    "reference_interval",
]
ReviewRouting = Literal["STANDARD_REVIEW", "MANDATORY_EXCEPTION_REVIEW"]


@dataclass(frozen=True)
class ObservationDayGroup:
    """Keep incomparable collection-time precisions separate for one calendar day."""

    collection_date: date
    date_only: tuple[AcceptedObservation, ...]
    local_datetimes: tuple[AcceptedObservation, ...]
    offset_datetimes: tuple[AcceptedObservation, ...]


@dataclass(frozen=True)
class ConversionResult:
    """A normalization proposal, not an accepted patient observation."""

    original_value: str
    original_unit: str
    canonical_value: Decimal | None
    canonical_unit: str | None
    conversion_factor: Decimal | None
    conversion_outcome: ConversionOutcome
    unconvertible_reason: str | None
    rule_version: str


def partition_review_queue(
    candidates: Iterable[CandidateExtraction],
) -> tuple[list[CandidateExtraction], list[CandidateExtraction]]:
    """Return pending candidates as mandatory and standard review queues.

    The returned lists preserve input order. Routing status is assigned upstream;
    this function neither applies a confidence threshold nor changes candidate state.
    """
    mandatory_review: list[CandidateExtraction] = []
    standard_review: list[CandidateExtraction] = []

    for candidate in candidates:
        if candidate.lifecycle_status != "CANDIDATE":
            continue
        if candidate.routing_status == "MANDATORY_EXCEPTION_REVIEW":
            mandatory_review.append(candidate)
        elif candidate.routing_status == "STANDARD_REVIEW":
            standard_review.append(candidate)
        else:
            raise ValueError(
                f"Unrecognized candidate routing status: {candidate.routing_status}"
            )

    return mandatory_review, standard_review


def classify_candidate_routing(
    candidate: CandidateExtraction,
    confidence_threshold: float,
) -> ReviewRouting:
    """Classify a pending candidate without accepting it or embedding a cutoff."""
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not isfinite(confidence_threshold)
        or not 0.0 <= confidence_threshold <= 1.0
    ):
        raise ValueError("Confidence threshold must be a finite number from 0 to 1.")
    if candidate.lifecycle_status != "CANDIDATE":
        raise ValueError("Only pending candidates can be routed for review.")
    if candidate.routing_status == "MANDATORY_EXCEPTION_REVIEW":
        return "MANDATORY_EXCEPTION_REVIEW"
    if candidate.routing_status != "STANDARD_REVIEW":
        raise ValueError(f"Unrecognized routing status: {candidate.routing_status}")
    if (
        not candidate.raw_value
        or not candidate.raw_value.strip()
        or not candidate.raw_unit
        or not candidate.raw_unit.strip()
        or candidate.field_confidences["value"] < confidence_threshold
        or candidate.field_confidences["unit"] < confidence_threshold
    ):
        return "MANDATORY_EXCEPTION_REVIEW"
    return "STANDARD_REVIEW"


def normalize_value(
    *,
    analyte_code: str,
    original_value: str,
    original_unit: str,
    canonical_units: Mapping[str, str],
    conversion_factors: Mapping[tuple[str, str, str], Decimal],
    rule_version: str,
) -> ConversionResult:
    """Apply an explicitly supplied versioned unit rule without accepting a value.

    Conversion-factor keys are (analyte_code, source_unit, canonical_unit), and
    factors multiply the original value to produce the canonical value.
    """
    if not analyte_code.strip():
        raise ValueError("Analyte code cannot be empty.")
    if not original_value.strip():
        raise ValueError("Original value cannot be empty.")
    if not original_unit.strip():
        raise ValueError("Original unit cannot be empty.")
    if not rule_version.strip():
        raise ValueError("Rule version cannot be empty.")

    try:
        numeric_value = Decimal(original_value)
    except InvalidOperation as error:
        raise ValueError("Original value must be a valid decimal number.") from error
    if not numeric_value.is_finite():
        raise ValueError("Original value must be finite.")

    canonical_unit = canonical_units.get(analyte_code)
    if canonical_unit is None:
        return ConversionResult(
            original_value=original_value,
            original_unit=original_unit,
            canonical_value=None,
            canonical_unit=None,
            conversion_factor=None,
            conversion_outcome="UNCONVERTIBLE_RETAINED",
            unconvertible_reason="NO_CANONICAL_UNIT_DEFINED",
            rule_version=rule_version,
        )
    if not canonical_unit.strip():
        raise ValueError("Canonical unit cannot be empty.")

    if original_unit == canonical_unit:
        return ConversionResult(
            original_value=original_value,
            original_unit=original_unit,
            canonical_value=numeric_value,
            canonical_unit=canonical_unit,
            conversion_factor=Decimal("1"),
            conversion_outcome="CANONICAL_DIRECT",
            unconvertible_reason=None,
            rule_version=rule_version,
        )

    factor = conversion_factors.get(
        (analyte_code, original_unit, canonical_unit)
    )
    if factor is None:
        return ConversionResult(
            original_value=original_value,
            original_unit=original_unit,
            canonical_value=None,
            canonical_unit=canonical_unit,
            conversion_factor=None,
            conversion_outcome="UNCONVERTIBLE_RETAINED",
            unconvertible_reason="NO_SUPPORTED_CONVERSION",
            rule_version=rule_version,
        )
    if not isinstance(factor, Decimal) or not factor.is_finite() or factor <= 0:
        raise ValueError("Conversion factor must be a positive finite Decimal.")

    with localcontext() as context:
        context.prec = max(
            context.prec,
            len(numeric_value.as_tuple().digits) + len(factor.as_tuple().digits),
        )
        canonical_value = numeric_value * factor

    return ConversionResult(
        original_value=original_value,
        original_unit=original_unit,
        canonical_value=canonical_value,
        canonical_unit=canonical_unit,
        conversion_factor=factor,
        conversion_outcome="CONVERTED_DETERMINISTIC",
        unconvertible_reason=None,
        rule_version=rule_version,
    )


def compare_adjacent_observations(
    previous: AcceptedObservation,
    current: AcceptedObservation,
) -> tuple[ComparabilityAttribute, ...]:
    """Name source comparability fields that differ between same-analyte values.

    The caller supplies adjacent observations in a comparable chronological sequence;
    this function does not sort or infer clinical significance.
    """
    if previous.analyte_code != current.analyte_code:
        raise ValueError("Comparability can only be checked for the same analyte.")

    changes: list[ComparabilityAttribute] = []
    if previous.source_lab != current.source_lab:
        changes.append("source_lab")
    if previous.method_note != current.method_note:
        changes.append("method_note")
    if (
        previous.original_unit != current.original_unit
        or previous.canonical_unit != current.canonical_unit
    ):
        changes.append("unit_basis")
    if previous.reference_interval != current.reference_interval:
        changes.append("reference_interval")

    return tuple(changes)


def filter_observations_for_grant(
    observations: Iterable[AcceptedObservation],
    grant: ClientShareGrant,
) -> list[AcceptedObservation]:
    """Keep accepted observations inside the grant's analyte and inclusive date scope."""
    analyte_scope = set(grant.granted_analytes)
    selected: list[AcceptedObservation] = []

    for observation in observations:
        if (
            observation.lifecycle_status != "ACCEPTED"
            or observation.analyte_code not in analyte_scope
        ):
            continue
        collection_value = observation.collection_datetime
        collection_date = (
            collection_value.date()
            if isinstance(collection_value, datetime)
            else collection_value
        )
        if grant.range_start <= collection_date <= grant.range_end:
            selected.append(observation)

    return selected


def filter_context_notes_for_grant(
    notes: Iterable[ContextNote],
    grant: ClientShareGrant,
) -> list[ContextNote]:
    """Keep in-range notes only when all their analytes are within grant scope."""
    analyte_scope = set(grant.granted_analytes)
    return [
        note
        for note in notes
        if grant.range_start <= note.note_date <= grant.range_end
        and set(note.analyte_codes).issubset(analyte_scope)
    ]


def group_observations_by_collection_day(
    observations: Iterable[AcceptedObservation],
) -> list[ObservationDayGroup]:
    """Group observations by recorded day without inventing time or timezone data.

    Date-only observations remain unpositioned within the day. Local datetimes are
    ordered by wall time, and offset-aware datetimes by UTC instant; those groups are
    not ordered relative to one another.
    """
    date_only: defaultdict[date, list[AcceptedObservation]] = defaultdict(list)
    local_datetimes: defaultdict[
        date, list[tuple[datetime, AcceptedObservation]]
    ] = defaultdict(list)
    offset_datetimes: defaultdict[
        date, list[tuple[datetime, AcceptedObservation]]
    ] = defaultdict(list)
    collection_dates: set[date] = set()

    for observation in observations:
        collection_value = observation.collection_datetime
        if isinstance(collection_value, datetime):
            collection_date = collection_value.date()
            if collection_value.utcoffset() is None:
                local_datetimes[collection_date].append(
                    (collection_value, observation)
                )
            else:
                offset_datetimes[collection_date].append(
                    (collection_value.astimezone(timezone.utc), observation)
                )
        else:
            collection_date = collection_value
            date_only[collection_date].append(observation)
        collection_dates.add(collection_date)

    return [
        ObservationDayGroup(
            collection_date=collection_date,
            date_only=tuple(date_only[collection_date]),
            local_datetimes=tuple(
                observation
                for _, observation in sorted(
                    local_datetimes[collection_date], key=lambda entry: entry[0]
                )
            ),
            offset_datetimes=tuple(
                observation
                for _, observation in sorted(
                    offset_datetimes[collection_date], key=lambda entry: entry[0]
                )
            ),
        )
        for collection_date in sorted(collection_dates)
    ]


def find_duplicate_report_groups(
    reports: Iterable[SourceReport],
) -> list[tuple[SourceReport, ...]]:
    """Return same-content report groups without merging or removing reports.

    Call with reports from one patient's vault; duplicate findings are for patient
    review, not automatic suppression.
    """
    reports_by_hash: defaultdict[str, list[SourceReport]] = defaultdict(list)
    for report in reports:
        reports_by_hash[report.sha256_hash].append(report)

    return [
        tuple(group)
        for group in reports_by_hash.values()
        if len(group) > 1
    ]


def find_duplicate_observation_groups(
    observations: Iterable[AcceptedObservation],
) -> list[tuple[AcceptedObservation, ...]]:
    """Return matching analyte/time/lab groups for explicit patient review."""
    observations_by_key: defaultdict[
        tuple[str, date | datetime, str], list[AcceptedObservation]
    ] = defaultdict(list)
    for observation in observations:
        key = (
            observation.analyte_code,
            observation.collection_datetime,
            observation.source_lab,
        )
        observations_by_key[key].append(observation)

    return [
        tuple(group)
        for group in observations_by_key.values()
        if len(group) > 1
    ]
