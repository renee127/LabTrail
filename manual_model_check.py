"""Manually inspect synthetic LabTrail domain objects and review-queue behavior.

Run with: python manual_model_check.py
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pprint import pprint
from uuid import UUID

from labtrail_algorithms import (
    classify_candidate_routing,
    compare_adjacent_observations,
    filter_context_notes_for_grant,
    filter_observations_for_grant,
    find_duplicate_observation_groups,
    find_duplicate_report_groups,
    group_observations_by_collection_day,
    normalize_value,
    partition_review_queue,
)
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

    mandatory_candidate = replace(
        candidate,
        candidate_id=UUID(int=10),
        routing_status="MANDATORY_EXCEPTION_REVIEW",
    )
    accepted_candidate = replace(
        candidate,
        candidate_id=UUID(int=11),
        lifecycle_status="ACCEPTED",
    )
    mandatory_review, standard_review = partition_review_queue(
        (candidate, mandatory_candidate, accepted_candidate)
    )
    standard_routing = classify_candidate_routing(candidate, 0.9)
    low_confidence_candidate = replace(
        candidate,
        candidate_id=UUID(int=29),
        field_confidences={
            **candidate.field_confidences,
            "value": 0.89,
        },
    )
    mandatory_routing = classify_candidate_routing(
        low_confidence_candidate, 0.9
    )
    missing_value_candidate = replace(candidate, raw_value=None)
    missing_value_routing = classify_candidate_routing(
        missing_value_candidate, 0.9
    )
    exact_threshold_candidate = replace(
        candidate,
        field_confidences={
            **candidate.field_confidences,
            "value": 0.9,
        },
    )
    exact_threshold_routing = classify_candidate_routing(
        exact_threshold_candidate, 0.9
    )

    previous_day = replace(
        observation,
        observation_id=UUID(int=12),
        collection_datetime=date(2026, 10, 31),
    )
    local_later = replace(
        observation,
        observation_id=UUID(int=13),
        collection_datetime=datetime(2026, 11, 1, 10, 0),
    )
    local_earlier = replace(
        observation,
        observation_id=UUID(int=14),
        collection_datetime=datetime(2026, 11, 1, 8, 0),
    )
    offset_later = replace(
        observation,
        observation_id=UUID(int=15),
        collection_datetime=datetime(
            2026, 11, 1, 10, 0, tzinfo=timezone(timedelta(hours=-5))
        ),
    )
    offset_earlier = replace(
        observation,
        observation_id=UUID(int=16),
        collection_datetime=datetime(
            2026, 11, 1, 9, 0, tzinfo=timezone(timedelta(hours=2))
        ),
    )
    day_groups = group_observations_by_collection_day(
        (
            offset_later,
            observation,
            local_later,
            offset_earlier,
            local_earlier,
            previous_day,
        )
    )

    duplicate_report = replace(report, report_id=UUID(int=17))
    different_report = replace(
        report,
        report_id=UUID(int=18),
        sha256_hash=sha256(b"different synthetic report").hexdigest(),
    )
    duplicate_reports = find_duplicate_report_groups(
        (report, duplicate_report, different_report)
    )

    duplicate_observation = replace(observation, observation_id=UUID(int=19))
    different_lab_observation = replace(
        observation,
        observation_id=UUID(int=20),
        source_lab="Different Synthetic Lab",
    )
    duplicate_observations = find_duplicate_observation_groups(
        (observation, duplicate_observation, different_lab_observation)
    )

    synthetic_canonical_units = {"SYNTHETIC_ANALYTE": "unit-b"}
    direct_conversion = normalize_value(
        analyte_code="SYNTHETIC_ANALYTE",
        original_value="2.50",
        original_unit="unit-b",
        canonical_units=synthetic_canonical_units,
        conversion_factors={},
        rule_version="synthetic-rules-v1",
    )
    converted_value = normalize_value(
        analyte_code="SYNTHETIC_ANALYTE",
        original_value="2.50",
        original_unit="unit-a",
        canonical_units=synthetic_canonical_units,
        conversion_factors={
            ("SYNTHETIC_ANALYTE", "unit-a", "unit-b"): Decimal("3")
        },
        rule_version="synthetic-rules-v1",
    )
    unsupported_conversion = normalize_value(
        analyte_code="SYNTHETIC_ANALYTE",
        original_value="2.50",
        original_unit="unit-c",
        canonical_units=synthetic_canonical_units,
        conversion_factors={},
        rule_version="synthetic-rules-v1",
    )
    no_canonical_unit = normalize_value(
        analyte_code="UNMAPPED_SYNTHETIC_ANALYTE",
        original_value="2.50",
        original_unit="unit-c",
        canonical_units={},
        conversion_factors={},
        rule_version="synthetic-rules-v1",
    )
    converted_observation = replace(
        observation,
        observation_id=UUID(int=21),
        analyte_code="SYNTHETIC_ANALYTE",
        original_value="2.50",
        original_unit="unit-a",
        canonical_value=Decimal("7.50"),
        canonical_unit="unit-b",
        conversion_factor=Decimal("3"),
        conversion_outcome="CONVERTED_DETERMINISTIC",
    )
    direct_observation = replace(
        converted_observation,
        observation_id=UUID(int=22),
        original_value="7.50",
        original_unit="unit-b",
        conversion_factor=Decimal("1"),
        conversion_outcome="CANONICAL_DIRECT",
    )
    changed_observation = replace(
        direct_observation,
        observation_id=UUID(int=23),
        source_lab="Other Synthetic Lab",
        method_note=None,
        reference_interval="different synthetic interval",
    )
    unit_basis_changes = compare_adjacent_observations(
        converted_observation, direct_observation
    )
    multiple_comparability_changes = compare_adjacent_observations(
        direct_observation, changed_observation
    )
    out_of_scope_observation = replace(
        observation,
        observation_id=UUID(int=24),
        analyte_code="OTHER_ANALYTE",
    )
    retracted_observation = replace(
        observation,
        observation_id=UUID(int=25),
        lifecycle_status="RETRACTED_BY_PATIENT",
    )
    out_of_range_observation = replace(
        observation,
        observation_id=UUID(int=26),
        collection_datetime=date(2025, 12, 31),
    )
    mixed_scope_note = replace(
        note,
        note_id=UUID(int=27),
        analyte_codes=["GLUCOSE", "OTHER_ANALYTE"],
    )
    out_of_range_note = replace(
        note,
        note_id=UUID(int=28),
        note_date=date(2025, 12, 31),
    )
    shared_observations = filter_observations_for_grant(
        (
            observation,
            out_of_scope_observation,
            retracted_observation,
            out_of_range_observation,
        ),
        grant,
    )
    shared_notes = filter_context_notes_for_grant(
        (note, mixed_scope_note, out_of_range_note),
        grant,
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
    assert mandatory_review == [mandatory_candidate]
    assert standard_review == [candidate]
    assert candidate.lifecycle_status == "CANDIDATE"
    assert accepted_candidate not in mandatory_review + standard_review
    assert standard_routing == "STANDARD_REVIEW"
    assert mandatory_routing == "MANDATORY_EXCEPTION_REVIEW"
    assert missing_value_routing == "MANDATORY_EXCEPTION_REVIEW"
    assert exact_threshold_routing == "STANDARD_REVIEW"
    assert [group.collection_date for group in day_groups] == [
        date(2026, 10, 31),
        date(2026, 11, 1),
    ]
    assert day_groups[1].date_only == (observation,)
    assert day_groups[1].local_datetimes == (local_earlier, local_later)
    assert day_groups[1].offset_datetimes == (offset_earlier, offset_later)
    assert duplicate_reports == [(report, duplicate_report)]
    assert duplicate_observations == [(observation, duplicate_observation)]
    assert direct_conversion.conversion_outcome == "CANONICAL_DIRECT"
    assert direct_conversion.canonical_value == Decimal("2.50")
    assert converted_value.conversion_outcome == "CONVERTED_DETERMINISTIC"
    assert converted_value.canonical_value == Decimal("7.50")
    assert converted_value.original_value == "2.50"
    assert unsupported_conversion.conversion_outcome == "UNCONVERTIBLE_RETAINED"
    assert unsupported_conversion.canonical_value is None
    assert unsupported_conversion.unconvertible_reason == "NO_SUPPORTED_CONVERSION"
    assert no_canonical_unit.canonical_unit is None
    assert unit_basis_changes == ("unit_basis",)
    assert multiple_comparability_changes == (
        "source_lab",
        "method_note",
        "reference_interval",
    )
    assert shared_observations == [observation]
    assert shared_notes == [note]

    for sample in samples:
        print(f"{type(sample).__name__}:")
        pprint(vars(sample))

    print(f"Mandatory review IDs: {[item.candidate_id for item in mandatory_review]}")
    print(f"Standard review IDs: {[item.candidate_id for item in standard_review]}")
    print(
        "Routing results: "
        f"standard={standard_routing}, "
        f"low-confidence={mandatory_routing}, "
        f"missing-value={missing_value_routing}, "
        f"threshold-boundary={exact_threshold_routing}"
    )
    for group in day_groups:
        print(
            f"Observation day {group.collection_date}: "
            f"{len(group.date_only)} date-only, "
            f"{len(group.local_datetimes)} local-time, "
            f"{len(group.offset_datetimes)} offset-aware"
        )
    print(f"Duplicate report groups: {len(duplicate_reports)}")
    print(f"Duplicate observation groups: {len(duplicate_observations)}")
    print("Synthetic direct conversion:")
    pprint(direct_conversion)
    print("Synthetic factor conversion:")
    pprint(converted_value)
    print("Unsupported synthetic conversion:")
    pprint(unsupported_conversion)
    print(f"Unit-basis comparison: {unit_basis_changes}")
    print(f"Other comparability changes: {multiple_comparability_changes}")
    print(
        f"Shared observation IDs: "
        f"{[item.observation_id for item in shared_observations]}"
    )
    print(f"Shared context-note IDs: {[item.note_id for item in shared_notes]}")
    print("Manual model and algorithm check passed.")


if __name__ == "__main__":
    main()
