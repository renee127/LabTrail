"""Manually construct and inspect synthetic LabTrail domain objects.

Run with: python manual_model_check.py
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pprint import pprint
from uuid import UUID

from models import (
    AcceptedObservation,
    AuditEvent,
    CandidateExtraction,
    ClientShareGrant,
    ConfirmedIdentity,
    ContextNote,
    GrantRecord,
    PatientAccount,
    SourceReport,
)


def main() -> None:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    patient = PatientAccount(
        patient_id=UUID(int=1),
        login_kdf_salt=b"s" * 16,
        wrapped_pmk_login=b"synthetic-wrapped-login-key",
        pmk_login_nonce=b"l" * 12,
        recovery_configured=False,
        created_at=now,
    )
    identity = ConfirmedIdentity(
        identity_id=UUID(int=2),
        lab_identifier="SYNTHETIC-LAB-001",
        asserted_name_exact="Sample Patient",
        confirmed_at=now,
    )
    report = SourceReport(
        report_id=UUID(int=3),
        sha256_hash=sha256(b"synthetic report").hexdigest(),
        mime_type="application/pdf",
        page_count=1,
        encrypted_file_blob=b"synthetic-encrypted-report",
        uploaded_at=now,
        source_lab="Synthetic Lab",
        collection_datetime=date(2026, 11, 1),
        confirmed_identity_id=identity.identity_id,
    )
    candidate = CandidateExtraction(
        candidate_id=UUID(int=4),
        report_id=report.report_id,
        extraction_job_id=UUID(int=5),
        row_index=0,
        raw_analyte_name="Glucose",
        proposed_analyte_code="GLUCOSE",
        raw_value="98.6",
        raw_unit="mg/dL",
        reference_interval="70-99 mg/dL",
        method_note="Synthetic method",
        specimen_type="Serum",
        abnormal_flag=None,
        page_number=1,
        bounding_box=(0.1, 0.2, 0.4, 0.3),
        region_available=True,
        field_confidences={
            "analyte": 0.99,
            "value": 0.98,
            "unit": 0.97,
            "region": 0.95,
        },
        model_identifier="sample-model",
        model_version="1.0",
        prompt_hash=sha256(b"synthetic prompt").hexdigest(),
        extracted_at=now,
        routing_status="STANDARD_REVIEW",
        lifecycle_status="CANDIDATE",
    )
    observation = AcceptedObservation(
        observation_id=UUID(int=6),
        candidate_id=candidate.candidate_id,
        report_id=report.report_id,
        analyte_code="GLUCOSE",
        canonical_value=Decimal("98.6"),
        canonical_unit="mg/dL",
        original_value="98.6",
        original_unit="mg/dL",
        conversion_factor=Decimal("1"),
        conversion_outcome="CANONICAL_DIRECT",
        unconvertible_reason=None,
        reference_interval="70-99 mg/dL",
        method_note="Synthetic method",
        source_lab="Synthetic Lab",
        collection_datetime=date(2026, 11, 1),
        page_number=1,
        bounding_box=candidate.bounding_box,
        provenance_flag="AS_EXTRACTED",
        lifecycle_status="ACCEPTED",
        rule_version="synthetic-rules-v1",
        accepted_at=now,
    )
    note = ContextNote(
        note_id=UUID(int=7),
        category="FREE_TEXT",
        note_date=date(2026, 11, 1),
        body="Synthetic manual-check note",
        analyte_codes=["GLUCOSE"],
        created_at=now,
        updated_at=now,
    )
    grant = ClientShareGrant(
        grant_id=UUID(int=8),
        recipient_name="Sample Clinician",
        recipient_identity_hash="synthetic-recipient-hash",
        granted_analytes=["GLUCOSE"],
        range_start=date(2026, 1, 1),
        range_end=date(2026, 11, 1),
        created_at=now,
        expires_at=now + timedelta(days=365),
        local_status="ACTIVE",
    )
    grant_record = GrantRecord(
        grant_id=grant.grant_id,
        recipient_identity_hash=grant.recipient_identity_hash,
        second_factor_verifier_hash="synthetic-verifier-hash",
        failed_attempts=0,
        schema_version="1",
        server_share=b"k" * 32,
        iv_nonce=b"n" * 12,
        bundle_blob_uri="synthetic://encrypted-bundle",
        status="ACTIVE",
        created_at=now,
        expires_at=grant.expires_at,
        revoked_at=None,
        lease_expires_at=None,
    )
    audit_event = AuditEvent(
        event_id=UUID(int=9),
        event_type="OBSERVATION_ACCEPTED",
        actor_role="PATIENT",
        actor_identifier="synthetic-patient-id",
        target_id=observation.observation_id,
        details_ciphertext=None,
        details_nonce=None,
        prev_event_hash=None,
        event_hash=sha256(b"synthetic audit event").hexdigest(),
        occurred_at=now,
    )

    samples = (
        patient,
        identity,
        report,
        candidate,
        observation,
        note,
        grant,
        grant_record,
        audit_event,
    )
    assert len(samples) == 9
    assert report.collection_datetime == date(2026, 11, 1)
    assert candidate.bounding_box == (0.1, 0.2, 0.4, 0.3)
    assert observation.canonical_value == Decimal("98.6")
    assert note.analyte_codes == ["GLUCOSE"]
    assert grant_record.server_share == b"k" * 32
    assert audit_event.prev_event_hash is None

    for sample in samples:
        print(f"{type(sample).__name__}:")
        pprint(vars(sample))

    print("Manual model check passed for all nine domain classes.")


if __name__ == "__main__":
    main()
