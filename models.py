"""Conceptual domain classes for LabTrail.

These classes summarize the current design; they are not yet persistence-ready
schemas and intentionally define no storage or cryptographic behavior.
"""


class PatientAccount:
    """Owns a patient's record and client-side key and recovery setup."""


class ConfirmedIdentity:
    """Records a patient-confirmed identity rendering used for lab matching."""


class SourceReport:
    """Represents an uploaded laboratory report and its source metadata."""


class CandidateExtraction:
    """Holds unverified extracted results with confidence and source location."""


class AcceptedObservation:
    """Holds a patient-reviewed result with normalization and provenance."""


class ContextNote:
    """Captures dated patient context associated with one or more analytes."""


class ClientShareGrant:
    """Records the patient's recipient, scope, expiry, and bundle choices."""


class GrantRecord:
    """Stores server-side share authorization and cryptographic metadata only."""


class AuditEvent:
    """Records an auditable patient, review, grant, or recipient action."""
