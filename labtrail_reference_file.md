# LabTrail Project Reference

## Purpose

LabTrail is a patient-controlled record for laboratory results collected across providers. It turns uploaded reports into a source-linked, chronological record that patients review and control. It organizes information; it does not interpret results or advise on care.

## Core user workflow

1. The patient uploads a PDF or image report and completes any missing report or identity details.
2. The system extracts result rows as **candidates**, preserving original text, units, report page, and region.
3. Deterministic rules map analytes, convert supported units, detect duplicates, and flag comparability changes. Unsupported conversions remain visible in their original units but do not enter canonical plots.
4. The patient explicitly accepts, corrects, or rejects each candidate. Only accepted observations appear in timelines; every decision is auditable.
5. The patient may add dated context notes to relevant analyte timelines.
6. To share, the patient selects a named clinician, analytes, date range, and expiry. LabTrail creates a read-only, immutable bundle with redacted source pages. The patient can revoke the grant.

## Domain model

- **PatientAccount** — owns the patient's record, identity confirmations, notes, and grants.
- **SourceReport** — the immutable uploaded source document and its provenance.
- **CandidateExtraction** — unverified extracted data, including confidence and source location.
- **AcceptedObservation** — patient-reviewed result data, including original and canonical values, conversion details, provenance, and lifecycle state.
- **ConfirmedIdentity** — a patient-confirmed rendering of identity from a laboratory.
- **ContextNote** — dated patient-authored context associated with analyte timelines.
- **ClientShareGrant** — patient-side recipient and scope choices, including the immutable bundle snapshot.
- **GrantRecord** — minimal server-side authorization and key-share metadata; it must not expose recipient display name, analyte scope, date range, or clinical values in plaintext.
- **AuditEvent** — immutable record of review, grant, access, revocation, and retraction actions.

The original noun candidates map as follows: `Patient` → `PatientAccount`, `LabReport` → `SourceReport`, `LabResult` → `CandidateExtraction` plus `AcceptedObservation`, and `ShareGrant` → `ClientShareGrant` plus `GrantRecord`. See the [UML draft](./uml_draft_copilot.md).

## Architecture and trust boundaries

- **Z1 — Patient and recipient browsers:** hold patient-controlled keys; encrypt/decrypt records and grants; perform deterministic normalization, review, redaction, and rendering.
- **Z2 — Edge gateway:** terminates TLS, authenticates sessions, applies rate limits, and routes requests.
- **Z3 — Blind storage and grant enforcement:** stores ciphertext, server-held share material, minimal authorization metadata, and append-only audit events. It must not be able to decrypt patient records or share bundles.
- **Z4 — Ephemeral extraction:** classifies and extracts uploaded reports only during an active upload, returns candidate data to Z1, and retains no persistent report content.

The browser-to-server trust assumption matters: a compromised host could serve altered client code. Use the signed/reproducible client-build, SRI, and CSP controls in the design. Z4 is a deliberate, transient plaintext-processing exception; enforce its zero-retention and access boundaries.

## Security and privacy rules

- Encrypt patient records, source files, and share bundles client-side before persistent server storage; use TLS for transport.
- Follow the key hierarchy and direct AES-256-GCM, per-grant 2-of-2 XOR share design in [the security architecture](./labtrail_security_architecture_updated_section_7_5.md). Use established cryptographic APIs; do not invent or implement primitives.
- Use Argon2id for the human-chosen login passphrase. The opt-in 24-word recovery phrase encodes a generated 256-bit secret, so derive its recovery key with HKDF-SHA-256 and purpose-specific versioned `info`; store only the AES-GCM-wrapped PMK and nonce in Z3. The recovery phrase/key never leaves the client. Recovery ciphertext and nonce must both be present exactly when recovery is configured.
- Keep recipient share material in the URL fragment, not HTTP requests. Treat the full link as sensitive: the fragment may be visible to email clients, browser history, or page scripts. Link possession alone is not authorization; require the specified clinician verification and per-session second factor.
- Keep share scope on the patient client and inside the encrypted bundle. Send only required authorization, expiry, ciphertext, and server-share metadata to Z3.
- Enforce expiry at `expires_at`. After an accepted revocation request, all gateways must deny further server access within five seconds; if current grant status cannot be verified, fail closed. Active-viewer closure is best effort.
- Revocation cannot erase data, keys, or ciphertext already downloaded or viewed. Disclose this before grant creation.
- Use synthetic or explicitly self-authorized data for all development, tests, and demonstrations. Do not ingest third-party patient data.

## Non-negotiable behavior

- No candidate enters the accepted record without a recorded patient action.
- Never guess, coerce, or silently drop a missing or unconvertible value; retain and label exceptions.
- Every displayed value must resolve to its source report and page. Withhold a recipient source page if its redaction region is missing or unreliable.
- Recipient source pages must be flattened, text-free rasters with all ungranted rows removed or masked; verify that ungranted text cannot be recovered.
- Accepted values are not silently recomputed when reference tables change. Version rules and preserve review history.
- Do not generate diagnoses, risk scores, clinical alerts, causal explanations, or treatment recommendations.

## Recommended implementation sequence

1. **Foundation and vault:** project setup, client key/recovery flow, authentication, encrypted storage, and append-only audit.
2. **Import and review:** report registration, ephemeral extraction, identity confirmation, candidate review, and explicit accept/correct/reject.
3. **Normalization and timeline:** versioned analyte/unit rules, duplicate and comparability handling, context notes, provenance, and non-interpretive summaries.
4. **Sharing:** client-side bundle creation, flattened redaction, split-key encryption, recipient authentication, and read-only viewer.
5. **Revocation and retraction:** authoritative grant checks, expiry, five-second revocation, fail-closed behavior, access logs, and post-share retraction.
6. **Export and hardening:** open-format export, accessibility, performance, privacy, and capability-boundary checks.

The core prototype is the first five steps. Use the [updated design report](./labtrail_solution_design_report_a2_updated.md) for the full increment plan and requirement traceability.

## Verification gates

- Test every supported upload type with synthetic fixtures; measure extraction against the report's stated accuracy thresholds.
- Verify that accepted observations always have explicit patient review events and resolvable provenance.
- Test conversion, unconvertible values, duplicate detection, comparability boundaries, and versioned-rule behavior.
- OCR and text-extract recipient renders to confirm no ungranted result leaks; verify missing-region pages are withheld.
- Test authentication, second-factor lockout, expiry at the exact deadline, revocation under stale caches and network failures, and server-side denial within five seconds.
- Confirm that server storage, logs, backups, and error reporting do not contain plaintext clinical data or share scope.
- Run privacy, accessibility, performance, and non-diagnostic language checks before each release.

## Scope boundaries

The prototype does not include automated portal/EHR imports or write-back, caregiver/proxy access, trusted-contact key splitting, multi-device synchronization, messaging, diagnosis, or treatment guidance. Keep these out of the implementation unless the requirements and threat model are deliberately revised.

## Project reference documents

- [Updated solution design report](./labtrail_solution_design_report_a2_updated.md) — requirements, architecture, interfaces, state models, implementation increments, and verification.
- [Security architecture](./labtrail_security_architecture_updated_section_7_5.md) — key hierarchy, share protocol, trust assumptions, and revocation limits.
- [UML draft](./uml_draft_copilot.md) — domain classes and relationships.
- [Project specification](./labtrail_spec.md) — concise client feature request and initial candidate nouns.
