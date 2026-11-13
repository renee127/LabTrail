## Client Feature Request

Patients need a private, source-linked history of laboratory results collected across providers. LabTrail should let patients upload PDF or image reports, confirm report identity, review/correct/reject extracted result candidates, and view accepted results over time with provenance, context, and comparability caveats. Patients control their encryption and recovery keys, what enters the record, and any time-limited, read-only share with a named clinician. They can revoke a share or retract a previously accepted result; the system must explain that revocation cannot erase information already retrieved by a recipient. LabTrail organizes results but must not diagnose, interpret, or recommend care.

## Current Domain Classes

The current domain model contains **nine classes**. This count refers to the UML domain model, not the system's architecture components.

1. **PatientAccount** — owns the patient's record and client-side key/recovery setup.
2. **ConfirmedIdentity** — records a patient-confirmed identity rendering used to match reports from a laboratory.
3. **SourceReport** — represents an uploaded source document and its report metadata.
4. **CandidateExtraction** — holds unverified extracted result data, confidence, and source-page location.
5. **AcceptedObservation** — holds a patient-reviewed result, preserving original values alongside normalization, provenance, and lifecycle state.
6. **ContextNote** — stores dated patient context associated with one or more analytes.
7. **ClientShareGrant** — represents the patient's local recipient, scope, expiry, and immutable bundle choices.
8. **GrantRecord** — represents the server-side authorization and cryptographic metadata; it must not store plaintext clinical values or share scope.
9. **AuditEvent** — records review, grant, access, revocation, and retraction events.

The original four candidate nouns are refined by this model: `Patient` becomes `PatientAccount`; `LabReport` becomes `SourceReport`; `LabResult` is split into `CandidateExtraction` and `AcceptedObservation`; and `ShareGrant` is split into the patient-side `ClientShareGrant` and server-side `GrantRecord`. The clinician remains an external authenticated identity, not a domain class. See the [UML draft](./uml_draft_copilot.md) and [updated solution design report](../labtrail_solution_design_report_a2_updated.md).
