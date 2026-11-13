"""Pure algorithms that operate on LabTrail domain objects."""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite
from typing import Literal
from uuid import UUID

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
SharePageDisposition = Literal[
    "WITHHOLD_REGION_UNAVAILABLE",
    "REDACTION_REQUIRED",
]
ComparabilityStatus = Literal["NOT_EVALUATED", "COMPARED"]
SHARE_PAYLOAD_SCHEMA_VERSION = "1"


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


@dataclass(frozen=True)
class ShareSourcePagePlan:
    """A page reference that must be withheld or redacted before sharing."""

    report_id: UUID
    page_number: int
    observation_ids: tuple[UUID, ...]
    disposition: SharePageDisposition


@dataclass(frozen=True)
class ShareContentSelection:
    """Grant-scoped in-memory selection, not an encrypted or publishable bundle.

    The selected domain records remain mutable; encryption requires a separate,
    explicit payload projection and verified redacted page bytes.
    """

    grant_id: UUID
    granted_analytes: tuple[str, ...]
    range_start: date
    range_end: date
    observations: tuple[AcceptedObservation, ...]
    context_notes: tuple[ContextNote, ...]
    source_pages: tuple[ShareSourcePagePlan, ...]


@dataclass(frozen=True)
class ShareObservationPayloadV1:
    observation_id: str
    analyte_code: str
    canonical_value: str | None
    canonical_unit: str | None
    original_value: str
    original_unit: str
    conversion_factor: str | None
    conversion_outcome: str
    unconvertible_reason: str | None
    reference_interval: str | None
    method_note: str | None
    source_lab: str
    collection_datetime: str
    source_report_id: str
    source_page_number: int
    source_bounding_box: tuple[float, float, float, float] | None
    source_page_status: SharePageDisposition
    review_status: Literal["ACCEPTED"]
    reviewed_at: str
    provenance_flag: str
    rule_version: str
    comparability_status: ComparabilityStatus
    comparability_previous_observation_id: str | None
    comparability_changes: tuple[ComparabilityAttribute, ...] | None


@dataclass(frozen=True)
class ShareContextNotePayloadV1:
    note_id: str
    category: str
    note_date: str
    body: str
    analyte_codes: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SharePayloadProjectionV1:
    """Versioned structured payload projection; redacted pages are not included."""

    schema_version: str
    grant_id: str
    granted_analytes: tuple[str, ...]
    range_start: str
    range_end: str
    observations: tuple[ShareObservationPayloadV1, ...]
    context_notes: tuple[ShareContextNotePayloadV1, ...]


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


def prepare_share_content_selection(
    *,
    observations: Iterable[AcceptedObservation],
    notes: Iterable[ContextNote],
    grant: ClientShareGrant,
    as_of: datetime,
) -> ShareContentSelection:
    """Select content for a grant being prepared or rebuilt on the client.

    Source pages are never included: pages with missing granted-row regions are
    withheld, and all other referenced pages still require verified redaction.
    Observation input order is preserved for any later comparability calculation.
    """
    if grant.local_status not in {"PREPARING_CLIENT", "BUNDLE_REFRESHED"}:
        raise ValueError("Only a new or refreshed client grant can be prepared.")
    if (as_of.utcoffset() is None) != (grant.created_at.utcoffset() is None):
        raise ValueError(
            "Preparation time and grant timestamps must use compatible timezones."
        )
    if not grant.created_at <= as_of < grant.expires_at:
        raise ValueError("Grant is not within its preparation validity period.")

    selected_observations = tuple(filter_observations_for_grant(observations, grant))
    selected_notes = tuple(filter_context_notes_for_grant(notes, grant))

    page_observations: defaultdict[
        tuple[UUID, int], list[AcceptedObservation]
    ] = defaultdict(list)
    for observation in selected_observations:
        page_observations[(observation.report_id, observation.page_number)].append(
            observation
        )

    source_pages = tuple(
        ShareSourcePagePlan(
            report_id=report_id,
            page_number=page_number,
            observation_ids=tuple(
                observation.observation_id for observation in page_records
            ),
            disposition=(
                "WITHHOLD_REGION_UNAVAILABLE"
                if any(record.bounding_box is None for record in page_records)
                else "REDACTION_REQUIRED"
            ),
        )
        for (report_id, page_number), page_records in sorted(
            page_observations.items(),
            key=lambda item: (item[0][0].hex, item[0][1]),
        )
    )
    return ShareContentSelection(
        grant_id=grant.grant_id,
        granted_analytes=tuple(grant.granted_analytes),
        range_start=grant.range_start,
        range_end=grant.range_end,
        observations=selected_observations,
        context_notes=selected_notes,
        source_pages=source_pages,
    )


def build_share_payload_projection(
    selection: ShareContentSelection,
    *,
    comparability_predecessors: Mapping[UUID, UUID] | None = None,
) -> SharePayloadProjectionV1:
    """Project selected records into explicit v1 fields without serializing pages.

    Mapped comparability pairs must be adjacent for that analyte in selection order.
    An unmapped observation is marked NOT_EVALUATED, never as an unchanged result.
    """
    observations_by_id = {
        observation.observation_id: observation
        for observation in selection.observations
    }
    if len(observations_by_id) != len(selection.observations):
        raise ValueError("Share selection contains duplicate observation identifiers.")

    page_status_by_observation_id: dict[UUID, SharePageDisposition] = {}
    for page in selection.source_pages:
        for observation_id in page.observation_ids:
            observation = observations_by_id.get(observation_id)
            if observation is None or (
                observation.report_id != page.report_id
                or observation.page_number != page.page_number
            ):
                raise ValueError("Source-page plans must reference selected observations.")
            if observation_id in page_status_by_observation_id:
                raise ValueError("An observation cannot have multiple source-page plans.")
            page_status_by_observation_id[observation_id] = page.disposition
    if set(page_status_by_observation_id) != set(observations_by_id):
        raise ValueError("Every selected observation requires a source-page plan.")

    observations_by_analyte: defaultdict[
        str, list[AcceptedObservation]
    ] = defaultdict(list)
    for observation in selection.observations:
        observations_by_analyte[observation.analyte_code].append(observation)
    positions_by_id = {
        observation.observation_id: index
        for analyte_observations in observations_by_analyte.values()
        for index, observation in enumerate(analyte_observations)
    }

    predecessors = comparability_predecessors or {}
    comparison_changes: dict[UUID, tuple[ComparabilityAttribute, ...]] = {}
    for observation_id, previous_id in predecessors.items():
        observation = observations_by_id.get(observation_id)
        previous = observations_by_id.get(previous_id)
        if observation is None or previous is None:
            raise ValueError("Comparability references must be in the share selection.")
        if observation.analyte_code != previous.analyte_code:
            raise ValueError("Comparability references must use the same analyte.")
        if positions_by_id[previous_id] != positions_by_id[observation_id] - 1:
            raise ValueError(
                "Comparability references must be adjacent in selection order."
            )
        comparison_changes[observation_id] = compare_adjacent_observations(
            previous, observation
        )

    observation_payloads = tuple(
        ShareObservationPayloadV1(
            observation_id=str(observation.observation_id),
            analyte_code=observation.analyte_code,
            canonical_value=(
                str(observation.canonical_value)
                if observation.canonical_value is not None
                else None
            ),
            canonical_unit=observation.canonical_unit,
            original_value=observation.original_value,
            original_unit=observation.original_unit,
            conversion_factor=(
                str(observation.conversion_factor)
                if observation.conversion_factor is not None
                else None
            ),
            conversion_outcome=observation.conversion_outcome,
            unconvertible_reason=observation.unconvertible_reason,
            reference_interval=observation.reference_interval,
            method_note=observation.method_note,
            source_lab=observation.source_lab,
            collection_datetime=observation.collection_datetime.isoformat(),
            source_report_id=str(observation.report_id),
            source_page_number=observation.page_number,
            source_bounding_box=observation.bounding_box,
            source_page_status=page_status_by_observation_id[
                observation.observation_id
            ],
            review_status="ACCEPTED",
            reviewed_at=observation.accepted_at.isoformat(),
            provenance_flag=observation.provenance_flag,
            rule_version=observation.rule_version,
            comparability_status=(
                "COMPARED"
                if observation.observation_id in comparison_changes
                else "NOT_EVALUATED"
            ),
            comparability_previous_observation_id=(
                str(predecessors[observation.observation_id])
                if observation.observation_id in predecessors
                else None
            ),
            comparability_changes=comparison_changes.get(
                observation.observation_id
            ),
        )
        for observation in selection.observations
    )
    note_payloads = tuple(
        ShareContextNotePayloadV1(
            note_id=str(note.note_id),
            category=note.category,
            note_date=note.note_date.isoformat(),
            body=note.body,
            analyte_codes=tuple(note.analyte_codes),
            created_at=note.created_at.isoformat(),
            updated_at=note.updated_at.isoformat(),
        )
        for note in selection.context_notes
    )
    return SharePayloadProjectionV1(
        schema_version=SHARE_PAYLOAD_SCHEMA_VERSION,
        grant_id=str(selection.grant_id),
        granted_analytes=selection.granted_analytes,
        range_start=selection.range_start.isoformat(),
        range_end=selection.range_end.isoformat(),
        observations=observation_payloads,
        context_notes=note_payloads,
    )


def serialize_share_payload_projection(
    payload: SharePayloadProjectionV1,
) -> bytes:
    """Serialize a structured projection deterministically as compact UTF-8 JSON."""
    return json.dumps(
        asdict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


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
