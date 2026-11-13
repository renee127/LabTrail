"""Conceptual domain classes for LabTrail."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from typing import Literal
from uuid import UUID


def _validate_bounding_box(
    bounding_box: tuple[float, float, float, float] | None,
) -> None:
    if bounding_box is None:
        return
    if not isinstance(bounding_box, tuple) or len(bounding_box) != 4:
        raise ValueError("Bounding box must contain exactly four coordinates.")
    x_min, y_min, x_max, y_max = bounding_box
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or not 0.0 <= value <= 1.0
            for value in bounding_box
        )
        or x_min >= x_max
        or y_min >= y_max
    ):
        raise ValueError(
            "Bounding box must be [x_min, y_min, x_max, y_max] with normalized, "
            "increasing coordinates."
        )


@dataclass
class PatientAccount:
    """Owns a patient's record and client-side key and recovery setup."""

    patient_id: UUID
    login_kdf_salt: bytes
    wrapped_pmk_login: bytes
    pmk_login_nonce: bytes
    recovery_configured: bool
    created_at: datetime
    wrapped_pmk_recovery: bytes | None = None
    pmk_recovery_nonce: bytes | None = None

    def __post_init__(self) -> None:
        has_recovery_wrapper = self.wrapped_pmk_recovery is not None
        has_recovery_nonce = self.pmk_recovery_nonce is not None
        if has_recovery_wrapper != has_recovery_nonce:
            raise ValueError("Recovery wrapper and nonce must be present together.")
        if self.recovery_configured != has_recovery_wrapper:
            raise ValueError(
                "Recovery wrapper and nonce must be present exactly when recovery is configured."
            )


@dataclass
class ConfirmedIdentity:
    """Records a patient-confirmed identity rendering used for lab matching."""

    identity_id: UUID
    lab_identifier: str
    asserted_name_exact: str
    confirmed_at: datetime


@dataclass
class SourceReport:
    """Represents an immutable uploaded report and completable metadata."""

    report_id: UUID
    sha256_hash: str
    mime_type: str
    page_count: int
    encrypted_file_blob: bytes
    uploaded_at: datetime
    source_lab: str | None = None
    collection_datetime: date | datetime | None = None
    report_datetime: date | datetime | None = None
    ordering_clinician: str | None = None
    report_type: str | None = None
    asserted_patient_name: str | None = None
    confirmed_identity_id: UUID | None = None


@dataclass
class CandidateExtraction:
    """Holds unverified extracted results with confidence and source location."""

    candidate_id: UUID
    report_id: UUID
    extraction_job_id: UUID
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
    bounding_box: tuple[float, float, float, float] | None
    region_available: bool
    field_confidences: dict[str, float]
    model_identifier: str
    model_version: str
    prompt_hash: str
    extracted_at: datetime
    routing_status: Literal["STANDARD_REVIEW", "MANDATORY_EXCEPTION_REVIEW"]
    lifecycle_status: Literal[
        "CANDIDATE", "ACCEPTED", "REJECTED", "RETRACTED_BY_PATIENT"
    ]

    def __post_init__(self) -> None:
        if self.row_index < 0:
            raise ValueError("Candidate row index cannot be negative.")
        if self.page_number < 1:
            raise ValueError("Candidate page number must be positive.")
        if not self.raw_analyte_name:
            raise ValueError("Candidate analyte name cannot be empty.")
        if self.routing_status not in {
            "STANDARD_REVIEW",
            "MANDATORY_EXCEPTION_REVIEW",
        }:
            raise ValueError("Candidate routing status is not recognized.")
        if self.lifecycle_status not in {
            "CANDIDATE",
            "ACCEPTED",
            "REJECTED",
            "RETRACTED_BY_PATIENT",
        }:
            raise ValueError("Candidate lifecycle status is not recognized.")
        if self.region_available != (self.bounding_box is not None):
            raise ValueError(
                "A bounding box must be present exactly when the region is available."
            )
        _validate_bounding_box(self.bounding_box)

        expected_confidences = {"analyte", "value", "unit", "region"}
        if set(self.field_confidences) != expected_confidences:
            raise ValueError("Field confidences must include analyte, value, unit, and region.")
        if any(
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not isfinite(score)
            or not 0.0 <= score <= 1.0
            for score in self.field_confidences.values()
        ):
            raise ValueError("Field confidence scores must be between 0 and 1.")


@dataclass
class AcceptedObservation:
    """Holds a patient-reviewed result with conversion and source provenance."""

    observation_id: UUID
    candidate_id: UUID
    report_id: UUID
    analyte_code: str
    canonical_value: Decimal | None
    canonical_unit: str | None
    original_value: str
    original_unit: str
    conversion_factor: Decimal | None
    conversion_outcome: Literal[
        "CANONICAL_DIRECT",
        "CONVERTED_DETERMINISTIC",
        "UNCONVERTIBLE_RETAINED",
    ]
    unconvertible_reason: str | None
    reference_interval: str | None
    method_note: str | None
    source_lab: str
    collection_datetime: date | datetime
    page_number: int
    bounding_box: tuple[float, float, float, float] | None
    provenance_flag: Literal["AS_EXTRACTED", "CORRECTED_BY_PATIENT"]
    lifecycle_status: Literal["ACCEPTED", "RETRACTED_BY_PATIENT"]
    rule_version: str
    accepted_at: datetime

    def __post_init__(self) -> None:
        if self.conversion_outcome not in {
            "CANONICAL_DIRECT",
            "CONVERTED_DETERMINISTIC",
            "UNCONVERTIBLE_RETAINED",
        }:
            raise ValueError("Conversion outcome is not recognized.")
        if self.lifecycle_status not in {"ACCEPTED", "RETRACTED_BY_PATIENT"}:
            raise ValueError("Accepted observation lifecycle status is not recognized.")
        if self.provenance_flag not in {"AS_EXTRACTED", "CORRECTED_BY_PATIENT"}:
            raise ValueError("Accepted observation provenance flag is not recognized.")
        if self.page_number < 1:
            raise ValueError("Accepted observation page number must be positive.")
        _validate_bounding_box(self.bounding_box)

        if self.conversion_outcome == "UNCONVERTIBLE_RETAINED":
            if (
                self.canonical_value is not None
                or self.conversion_factor is not None
                or not self.unconvertible_reason
            ):
                raise ValueError(
                    "Unconvertible observations require no canonical value or factor "
                    "and a reason."
                )
            return

        if (
            not isinstance(self.canonical_value, Decimal)
            or not self.canonical_value.is_finite()
            or self.canonical_unit is None
            or not self.canonical_unit
            or not isinstance(self.conversion_factor, Decimal)
            or not self.conversion_factor.is_finite()
            or self.unconvertible_reason is not None
        ):
            raise ValueError(
                "Converted observations require finite canonical values, a canonical "
                "unit and factor, and no unconvertible reason."
            )
        if (
            self.conversion_outcome == "CANONICAL_DIRECT"
            and self.conversion_factor != Decimal("1")
        ):
            raise ValueError("Direct canonical values require a conversion factor of 1.")


@dataclass
class ContextNote:
    """Captures dated patient context associated with one or more analytes."""

    note_id: UUID
    category: str
    note_date: date
    encrypted_body: str
    analyte_codes: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class ClientShareGrant:
    """Records the patient's recipient, scope, expiry, and bundle choices."""

    grant_id: UUID
    recipient_name: str
    recipient_identity_hash: str
    granted_analytes: list[str]
    range_start: date
    range_end: date
    created_at: datetime
    expires_at: datetime
    local_status: str


@dataclass
class GrantRecord:
    """Stores server-side share authorization and cryptographic metadata only."""

    grant_id: UUID
    recipient_identity_hash: str
    second_factor_verifier_hash: str
    failed_attempts: int
    schema_version: str
    server_share: bytes
    iv_nonce: bytes
    bundle_blob_uri: str
    status: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime
    lease_expires_at: datetime


@dataclass
class AuditEvent:
    """Records an auditable patient, review, grant, or recipient action."""

    event_id: UUID
    event_type: str
    actor_role: str
    actor_identifier: str
    target_id: UUID
    metadata: dict[str, object]
    prev_event_hash: str
    event_hash: str
    occurred_at: datetime
