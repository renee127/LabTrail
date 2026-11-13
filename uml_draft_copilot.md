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
        +Bytes login_kdf_salt
        +Bytes wrapped_pmk_login
        +Bytes pmk_login_nonce
        +Boolean recovery_configured
        +Bytes? wrapped_pmk_recovery
        +Bytes? pmk_recovery_nonce
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
        +Optional~String~ source_lab
        +Optional~DateOrDateTime~ collection_datetime
        +Optional~DateOrDateTime~ report_datetime
        +Optional~String~ ordering_clinician
        +Optional~String~ report_type
        +Optional~String~ asserted_patient_name
        +Blob encrypted_file_blob
        +Timestamp uploaded_at
        +Optional~UUID~ confirmed_identity_id
    }

    class CandidateExtraction {
        +UUID candidate_id
        +UUID report_id
        +UUID extraction_job_id
        +Integer row_index
        +String raw_analyte_name
        +String proposed_analyte_code
        +Optional~String~ raw_value
        +Optional~String~ raw_unit
        +Optional~String~ reference_interval
        +Optional~String~ method_note
        +Optional~String~ specimen_type
        +Optional~String~ abnormal_flag
        +Integer page_number
        +Optional~BoundingBox~ bounding_box
        +Boolean region_available
        +JSON field_confidences
        +String model_identifier
        +String model_version
        +String prompt_hash
        +Timestamp extracted_at
        +String routing_status
        +String lifecycle_status
    }

    class AcceptedObservation {
        +UUID observation_id
        +UUID candidate_id
        +UUID report_id
        +String analyte_code
        +Optional~Decimal~ canonical_value
        +Optional~String~ canonical_unit
        +String original_value
        +String original_unit
        +Optional~Decimal~ conversion_factor
        +String conversion_outcome
        +Optional~String~ unconvertible_reason
        +Optional~String~ reference_interval
        +Optional~String~ method_note
        +String source_lab
        +DateOrDateTime collection_datetime
        +Integer page_number
        +Optional~BoundingBox~ bounding_box
        +String provenance_flag
        +String lifecycle_status
        +String rule_version
        +Timestamp accepted_at
    }

    class ContextNote {
        +UUID note_id
        +String category
        +Date note_date
        +String body
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
        +Optional~Bytes~ server_share
        +Bytes iv_nonce
        +String bundle_blob_uri
        +String status
        +Timestamp created_at
        +Timestamp expires_at
        +Optional~Timestamp~ revoked_at
        +Optional~Timestamp~ lease_expires_at
    }

    class AuditEvent {
        +UUID event_id
        +String event_type
        +String actor_role
        +String actor_identifier
        +UUID target_id
        +Optional~Bytes~ details_ciphertext
        +Optional~Bytes~ details_nonce
        +Optional~String~ prev_event_hash
        +String event_hash
        +Timestamp occurred_at
    }

    PatientAccount "1" --> "0..*" ConfirmedIdentity : confirms
    PatientAccount "1" --> "0..*" SourceReport : owns
    ConfirmedIdentity "0..1" <-- "0..*" SourceReport : matched rendering
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

- `ContextNote.category` is one of `MEDICATION_CHANGE`, `DIET`, `ILLNESS`, `ACTIVITY`, or `FREE_TEXT`. Each note is linked to one or more unique analyte codes, and `updated_at` cannot precede `created_at`.
- `ContextNote` is a Z1 in-memory model. The persistence layer encrypts the entire serialized note, including category, date, and analyte associations, before storing it in Z3.
- `ClientShareGrant` requires a named recipient, a non-empty unique analyte scope, an ordered date range, and expiry after creation but no more than five calendar years later. `local_status` follows the grant lifecycle states in Figure 6.
- `GrantRecord.status` follows Figure 6 after registration. An active record has a 32-byte `server_share`; locked, expired, revoked, and bundle-refreshing records do not retain it. `revoked_at` is present only for `REVOKED`; `lease_expires_at` is optional and cannot exceed `expires_at`. `iv_nonce` is a 12-byte AES-GCM nonce, and `bundle_blob_uri` points to ciphertext stored in Z3.
- `AuditEvent` contains no unrestricted plaintext metadata. Optional correction details are encrypted on Z1; `details_ciphertext` and its 12-byte nonce are either both present or both absent. The event chain hashes the canonical public envelope and encrypted details; hashes are lowercase 64-character SHA-256 hex digests, and the genesis event has no `prev_event_hash`. `actor_identifier` is an opaque verified identity; patient views resolve patient identity from `PatientAccount` and recipient identity/grant scope from the matching `ClientShareGrant`.
- The first four noun candidates map to `PatientAccount`, `SourceReport`, the `CandidateExtraction`/`AcceptedObservation` pair, and the `ClientShareGrant`/`GrantRecord` pair.
- `PatientAccount` stores only the passphrase KDF salt and wrapped PMK ciphertext/nonces, never plaintext keys or passphrases. Recovery ciphertext and its nonce are both absent when recovery is disabled and both present when it is enabled.
- `SourceReport` keeps its uploaded artifact and content hash immutable while allowing report metadata to be incomplete. Optional collection/report values preserve date-only or date-time precision, including a timezone offset only when the source provides one; confirmed reports link to the patient-scoped `ConfirmedIdentity` for that laboratory rendering.
- `CandidateExtraction` retains nullable source fields, including `method_note` for comparability, four named field-confidence scores, and the extraction job/model/version/prompt-hash provenance. Its normalized bounding box is either absent with `region_available = false` or `[x_min, y_min, x_max, y_max]` in the range 0–1. `routing_status` only routes review; `lifecycle_status` starts at `CANDIDATE` and never implies automatic acceptance.
- Candidate extraction preserves the reported value, unit, and source region. An accepted observation records patient-reviewed and normalized data; an unconvertible value retains its original representation.
- `AcceptedObservation.conversion_outcome` is `CANONICAL_DIRECT`, `CONVERTED_DETERMINISTIC`, or `UNCONVERTIBLE_RETAINED`. Unconvertible values have no canonical value or factor and require an explicit reason. Source page and optional bounding box are snapshotted for one-action provenance; a missing box denotes `REGION_UNAVAILABLE`. Its lifecycle (`ACCEPTED` or `RETRACTED_BY_PATIENT`) is distinct from review provenance (`AS_EXTRACTED` or `CORRECTED_BY_PATIENT`), and its collection value preserves date-only or date-time precision. Optional source `method_note` is retained for comparability checks.
- `ClientShareGrant` is patient-side and contains recipient and scope choices. It copies selected observations and relevant context into an immutable encrypted bundle. Corrections or new results require a new grant; the bundle is not a live link.
- `GrantRecord` is server-side and contains only authorization, expiry, and ciphertext/key-share metadata. It must not contain recipient display names, analyte scope, date range, or clinical values in plaintext.
- Sensitive patient-owned fields are encrypted before persistence in the hosted store. The server audit envelope contains no plaintext clinical values or grant scope; patient views resolve patient identity from `PatientAccount` and grant scope/recipient display identity from `ClientShareGrant`.
