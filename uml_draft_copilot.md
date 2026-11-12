# LabTrail UML Draft

## System overview

LabTrail turns patient-uploaded reports into source-linked result candidates. The patient explicitly accepts, corrects, or rejects each candidate; only accepted observations appear on longitudinal timelines. Patients can add dated context notes and share an immutable, scoped, expiring bundle with a named clinician. LabTrail organizes information but does not interpret results.

## Class choices

- **PatientAccount** represents the patient and their local record/key setup. This refines the initial `Patient` candidate.
- **SourceReport** represents an uploaded source document. This refines `LabReport`.
- **CandidateExtraction** and **AcceptedObservation** separate extracted proposals from patient-verified record values. Together they refine the initial `LabResult` candidate.
- **ClientShareGrant** records the patient's local recipient and scope choices; **GrantRecord** is the corresponding minimal server-side authorization record. Together they refine `ShareGrant`.
- **ConfirmedIdentity**, **ContextNote**, and **AuditEvent** capture identity review, dated context, and auditable patient/recipient actions required by the workflow.

The clinician is represented by a recipient identifier, not a separate class, because identity and authentication are managed by an external system.

## Class diagram

```mermaid
classDiagram
    direction LR

    class PatientAccount {
        +UUID patient_id
        +Bytes pmk_salt
        +Bytes wrapped_pmk_recovery
        +Boolean recovery_configured
        +Timestamp created_at
    }

    class ConfirmedIdentity {
        +UUID identity_id
        +String lab_identifier
        +String asserted_name_exact
        +Timestamp confirmed_at
    }

    class SourceReport {
        +UUID report_id
        +String sha256_hash
        +String mime_type
        +Integer page_count
        +String source_lab
        +Date collection_date
        +Date report_date
        +String ordering_clinician
        +String asserted_patient_name
        +Blob encrypted_file_blob
        +Timestamp uploaded_at
    }

    class CandidateExtraction {
        +UUID candidate_id
        +UUID report_id
        +String raw_analyte_name
        +String proposed_analyte_code
        +String raw_value
        +String raw_unit
        +String reference_interval
        +String specimen_type
        +String abnormal_flag
        +Integer page_number
        +JSON bounding_box
        +Boolean region_available
        +Float confidence_score
        +String model_version
        +String status
    }

    class AcceptedObservation {
        +UUID observation_id
        +UUID candidate_id
        +UUID report_id
        +String analyte_code
        +Decimal canonical_value
        +String canonical_unit
        +String original_value
        +String original_unit
        +Decimal conversion_factor
        +Boolean is_convertible
        +String unconvertible_reason
        +String reference_interval
        +String source_lab
        +Date collection_date
        +String provenance_flag
        +String lifecycle_status
        +String rule_version
        +Timestamp accepted_at
    }

    class ContextNote {
        +UUID note_id
        +String category
        +Date note_date
        +String encrypted_body
        +List~String~ analyte_codes
        +Timestamp created_at
        +Timestamp updated_at
    }

    class ClientShareGrant {
        +UUID grant_id
        +String recipient_name
        +String recipient_identity_hash
        +List~String~ granted_analytes
        +Date range_start
        +Date range_end
        +Timestamp created_at
        +Timestamp expires_at
        +String local_status
    }

    class GrantRecord {
        +UUID grant_id
        +String recipient_identity_hash
        +String second_factor_verifier_hash
        +Integer failed_attempts
        +String schema_version
        +Bytes server_share
        +Bytes iv_nonce
        +String bundle_blob_uri
        +String status
        +Timestamp created_at
        +Timestamp expires_at
        +Timestamp revoked_at
        +Timestamp lease_expires_at
    }

    class AuditEvent {
        +UUID event_id
        +String event_type
        +String actor_role
        +String actor_identifier
        +UUID target_id
        +JSON metadata
        +String prev_event_hash
        +String event_hash
        +Timestamp occurred_at
    }

    PatientAccount "1" --> "0..*" ConfirmedIdentity : confirms
    PatientAccount "1" --> "0..*" SourceReport : owns
    PatientAccount "1" --> "0..*" ContextNote : records
    PatientAccount "1" --> "0..*" ClientShareGrant : creates on device
    ClientShareGrant "1" --> "0..1" GrantRecord : registers metadata
    ClientShareGrant "0..*" --> "0..*" AcceptedObservation : snapshots selected values
    ClientShareGrant "0..*" --> "0..*" ContextNote : snapshots relevant notes
    SourceReport "1" --> "0..*" CandidateExtraction : yields
    CandidateExtraction "1" --> "0..1" AcceptedObservation : promoted to
    SourceReport "1" --> "0..*" AcceptedObservation : provenance
    CandidateExtraction "1" --> "0..*" AuditEvent : review events
    AcceptedObservation "1" --> "0..*" AuditEvent : audit events
    GrantRecord "1" --> "0..*" AuditEvent : access events
```

## Modeling notes

- The first four noun candidates map to `PatientAccount`, `SourceReport`, the `CandidateExtraction`/`AcceptedObservation` pair, and the `ClientShareGrant`/`GrantRecord` pair.
- Candidate extraction preserves the reported value, unit, and source region. An accepted observation records patient-reviewed and normalized data; an unconvertible value retains its original representation.
- `ClientShareGrant` is patient-side and contains recipient and scope choices. It copies selected observations and relevant context into an immutable encrypted bundle. Corrections or new results require a new grant; the bundle is not a live link.
- `GrantRecord` is server-side and contains only authorization, expiry, and ciphertext/key-share metadata. It must not contain recipient display names, analyte scope, date range, or clinical values in plaintext.
- Sensitive patient-owned fields are encrypted before persistence in the hosted store. Audit metadata records actions and targets, not clinical values or share scope.
