"""Conceptual domain classes for LabTrail."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


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
    """Represents an uploaded laboratory report and its source metadata."""

    report_id: UUID
    sha256_hash: str
    mime_type: str
    page_count: int
    source_lab: str
    collection_date: date
    report_date: date
    ordering_clinician: str
    asserted_patient_name: str
    encrypted_file_blob: bytes
    uploaded_at: datetime


@dataclass
class CandidateExtraction:
    """Holds unverified extracted results with confidence and source location."""

    candidate_id: UUID
    report_id: UUID
    raw_analyte_name: str
    proposed_analyte_code: str
    raw_value: str
    raw_unit: str
    reference_interval: str
    specimen_type: str
    abnormal_flag: str
    page_number: int
    bounding_box: dict[str, object]
    region_available: bool
    confidence_score: float
    model_version: str
    status: str


@dataclass
class AcceptedObservation:
    """Holds a patient-reviewed result with normalization and provenance."""

    observation_id: UUID
    candidate_id: UUID
    report_id: UUID
    analyte_code: str
    canonical_value: Decimal
    canonical_unit: str
    original_value: str
    original_unit: str
    conversion_factor: Decimal
    is_convertible: bool
    unconvertible_reason: str
    reference_interval: str
    source_lab: str
    collection_date: date
    provenance_flag: str
    lifecycle_status: str
    rule_version: str
    accepted_at: datetime


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
