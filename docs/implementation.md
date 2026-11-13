# LabTrail — What Is Built

This document describes the working prototype in this repository. It is
deliberately narrower than the [solution design report](../labtrail_solution_design_report_a2_updated.md):
the report describes the full architecture, and this page shows which slice of it
runs today, how the pieces fit together, and how each part is tested.

The prototype runs locally (`streamlit run app.py`) on synthetic lab reports.
It covers one end-to-end path: **upload → confirm → review → timeline → share**,
plus saving the record as an encrypted file.

## 1. Components

Solid boxes are implemented. Dashed boxes are designed in the report but not built.

```mermaid
flowchart TB
    patient([Patient])
    clinician([Clinician])

    subgraph built["Built: runs on the patient's machine"]
        app["app.py<br/>Streamlit UI"]
        workflow["labtrail_workflow.py<br/>analyte table, unit rules,<br/>accept/correct, timeline, share"]
        extraction["labtrail_extraction.py<br/>OCR line extraction"]
        algorithms["labtrail_algorithms.py<br/>review queue, normalization,<br/>comparability, share selection"]
        redaction["labtrail_redaction.py<br/>flattened page masking<br/>+ OCR leak check"]
        vault["labtrail_vault.py<br/>Argon2id + AES-256-GCM<br/>record file"]
        models["models.py<br/>domain classes"]
        schemas[("JSON schemas<br/>extraction result v1<br/>share payload v1")]
        tesseract[["Tesseract OCR"]]
    end

    subgraph planned["Designed, not built"]
        gateway["Auth gateway<br/>accounts, MFA"]
        store["Blind storage<br/>& grant service"]
        viewer["Recipient viewer"]
        revoke["5-second revocation"]
    end

    patient --> app
    app --> workflow
    app --> extraction
    app --> redaction
    app --> vault
    workflow --> algorithms
    workflow --> schemas
    extraction --> tesseract
    extraction --> schemas
    redaction --> algorithms
    redaction --> tesseract
    algorithms --> models
    workflow --> models
    vault --> models

    app -. "share JSON<br/>(downloaded today)" .-> store
    store -.-> viewer
    viewer -.-> clinician
    revoke -.-> store
    gateway -.-> store

    classDef plannedStyle stroke-dasharray: 5 5,fill:none
    class gateway,store,viewer,revoke plannedStyle
```

**Figure 1. Implemented components.** The UI only orchestrates. Every rule a
reviewer might question (what counts as a correction, how units convert, what
a share may contain) lives in plain Python modules with their own tests.

## 2. Domain classes used by the prototype

```mermaid
classDiagram
    class CandidateExtractionResult {
        report_id
        extraction_job_id
        candidates
        page_inventories
    }
    class ExtractionPageInventory {
        page_number
        source_page_sha256
        result_row_indices
        inventory_status = INCOMPLETE or COMPLETE
    }
    class CandidateExtraction {
        candidate_id
        raw_analyte_name
        raw_value
        raw_unit
        reference_interval
        abnormal_flag
        page_number
        bounding_box
        field_confidences
        routing_status
        lifecycle_status
    }
    class AcceptedObservation {
        observation_id
        analyte_code
        original_value / original_unit
        canonical_value / canonical_unit
        conversion_outcome
        reference_interval
        source_lab
        collection_datetime
        bounding_box
        provenance_flag
        lifecycle_status
        rule_version
    }
    class ContextNote {
        note_id
        category
        note_date
        body
        analyte_codes
    }
    class ClientShareGrant {
        grant_id
        recipient_name
        recipient_identity_hash
        granted_analytes
        range_start / range_end
        expires_at
    }
    class ShareContentSelection {
        observations
        context_notes
        source_pages
    }
    class SourcePageRedactionResult {
        disposition = REDACTED_PNG or WITHHELD
        withheld_reason
        redacted_png
    }

    CandidateExtractionResult "1" *-- "many" CandidateExtraction
    CandidateExtractionResult "1" *-- "1..many" ExtractionPageInventory
    CandidateExtraction "1" <.. "0..1" AcceptedObservation : accepted from
    ContextNote "many" ..> "1..many" AcceptedObservation : annotates analyte
    ClientShareGrant "1" --> "1" ShareContentSelection : scopes
    ShareContentSelection o-- AcceptedObservation
    ShareContentSelection o-- ContextNote
    SourcePageRedactionResult ..> ClientShareGrant : masks rows outside
```

**Figure 2. Classes the running prototype creates.** `models.py` also defines
`PatientAccount`, `ConfirmedIdentity`, `SourceReport`, `GrantRecord`, and
`AuditEvent` from the report's domain model. They are validated by unit tests
but not used by the demo, because accounts, server storage, and the audit chain
are not built.

## 3. End-to-end sequence

```mermaid
sequenceDiagram
    autonumber
    actor P as Patient
    participant UI as app.py
    participant X as labtrail_extraction
    participant W as labtrail_workflow
    participant A as labtrail_algorithms
    participant R as labtrail_redaction
    participant V as labtrail_vault

    P->>UI: Upload PDF or image
    UI->>X: extract_document_ocr_line_candidates()
    X-->>UI: candidates (all mandatory review), page inventory INCOMPLETE
    UI->>W: suggest_report_metadata()
    UI-->>P: Lab, date, printed name for confirmation
    P->>UI: Confirm report is mine

    loop Each extracted line
        UI-->>P: Proposed values beside a crop of the source page
        P->>UI: Accept, correct, or reject
        UI->>W: accept_candidate()
        W->>A: normalize_value()
        A-->>W: direct, converted, or unconvertible
        W-->>UI: AcceptedObservation (AS_EXTRACTED or CORRECTED_BY_PATIENT)
    end

    opt Patient confirms page is complete
        UI->>W: confirm_page_coverage()
        W-->>UI: page inventory COMPLETE
    end

    P->>UI: Open timeline
    UI->>W: analyte_timeline()
    W->>A: compare_adjacent_observations()
    UI-->>P: Chart, comparability markers, unconvertible values listed

    P->>UI: Choose analytes, dates, recipient
    UI->>W: build_share()
    W->>A: prepare_share_content_selection()
    W->>A: build_share_payload_projection()
    W-->>UI: Schema-validated share JSON
    UI->>R: redact_source_page() for each referenced page
    R-->>UI: Masked PNG, or WITHHELD with a reason

    P->>UI: Save record with passphrase
    UI->>V: encrypt_record()
    V-->>P: .vault file
```

**Figure 3. One pass through the prototype.** Nothing enters the record without
the patient's explicit accept or correct decision inside the loop, and a source page is shared only if the
patient confirmed it complete and the masked image passes the OCR leak check.

## 4. States

```mermaid
stateDiagram-v2
    state "Extracted line" as line {
        [*] --> CANDIDATE : OCR extraction
        CANDIDATE --> ACCEPTED : patient accepts or corrects
        CANDIDATE --> REJECTED : patient rejects
    }
    state "Accepted observation" as obs {
        ACCEPTED_OBS : ACCEPTED, on timeline and shareable
        RETRACTED_BY_PATIENT : RETRACTED, hidden from timeline and new shares
        [*] --> ACCEPTED_OBS : created on accept
        ACCEPTED_OBS --> RETRACTED_BY_PATIENT : patient retracts
    }
    state "Page coverage" as page {
        [*] --> INCOMPLETE : OCR cannot prove all rows found
        INCOMPLETE --> COMPLETE : every line decided and patient confirms
    }
```

**Figure 4. State changes the prototype enforces.** Only a patient action moves
a line out of `CANDIDATE` or a page to `COMPLETE`.

## 5. Changes from the design report

| Area | Report | Prototype | Why |
|---|---|---|---|
| Page coverage | Extraction service declares a page's rows complete | Patient confirms after deciding every line | OCR cannot prove it found every row; the patient already reviews each line against the page image |
| Redaction leak check | Any masked token visible anywhere fails the page | Tokens inside granted rows are ignored by the text search; text inside masks still fails | Shared rows legitimately repeat tokens such as "mg/dL", which made every mixed page fail |
| Extraction | Model-based extraction service | Local Tesseract OCR with preprocessing | Runs offline on synthetic data; measured in the benchmark |
| Key management | Random master key wrapped by login and recovery keys | Record file encrypted directly with a passphrase-derived key | Shows patient-held keys without accounts or recovery |
| Platform | Browser client with server zones | Single local Streamlit app | Keeps the slice end-to-end within the project timeline |

## 6. Requirement coverage

Status: **Built** works end to end in the demo; **Partial** works with the stated gap;
**Not built** is design only.

| Req. | Summary | Status | Where | Tests |
|---|---|---|---|---|
| FR-01 | Upload PDF, PNG, JPEG | Built (25 MiB and pixel limits) | `app.ingest`, `extract_document_ocr_line_candidates` | extraction, smoke |
| FR-02 | Register report metadata | Partial: lab, collection date, printed name; no report date, ordering clinician, or type | `suggest_report_metadata`, Upload page | workflow |
| FR-03 | Extract candidate rows with location | Partial: analyte, value, unit, range, flag, page, box; no specimen type | `labtrail_extraction` | extraction, benchmark |
| FR-04 | Map analytes to codes | Partial: three internal codes suggested; patient chooses; not LOINC | `suggest_analyte_code` | workflow |
| FR-05 | Deterministic unit outcomes | Built | `normalize_value`, versioned `CONVERSION_FACTORS` | algorithms, workflow |
| FR-06 | Duplicate detection | Partial: identical files skipped with a warning; duplicate results flagged on timeline | `app.ingest`, `find_duplicate_observation_groups` | algorithms |
| FR-07 | Comparability markers | Built | `compare_adjacent_observations`, `analyte_timeline` | algorithms, workflow, smoke |
| FR-08 | No automatic acceptance | Built: every OCR line is mandatory review | `partition_review_queue`, Review page | algorithms, smoke |
| FR-09 | Accept, correct, reject with provenance | Built (activity log is local, not hash-chained) | `accept_candidate` | workflow, smoke |
| FR-10 | Link value to source region | Partial: crop shown during review; not yet clickable from the timeline | Review page `_crop` | — |
| FR-11 | Context notes | Partial: create and link to analytes; no editing | Timeline page | workflow (share scoping) |
| FR-12 | Chronological analyte view | Built | Timeline page, `analyte_timeline` | workflow, smoke |
| FR-13 | No interpretation | Partial: no summaries or interpretive text exist; no automated prohibited-phrase scanner | — | — |
| FR-14 | Scoped, expiring grant | Built | `build_share`, `ClientShareGrant` | workflow, schema |
| FR-15 | Revocation within 5 s | Not built | — | — |
| FR-16 | Access log | Not built | — | — |
| FR-17 | Recipient read-only view | Not built: share is a downloadable JSON file | — | — |
| FR-18 | Full export | Partial: share JSON (schema v1) and encrypted record file; no JSON/CSV bundle with source files | `build_share`, `labtrail_vault` | schema, vault |
| FR-19 | Flattened, masked source pages | Built, after patient-confirmed coverage | `redact_source_page`, `confirm_page_coverage` | redaction, workflow, smoke |
| FR-20 | Withhold unreliable pages | Built | `redact_source_page` | redaction, workflow |
| FR-21 | Confirm identity per lab rendering | Partial: confirmed once per lab and printed name; `ConfirmedIdentity` not persisted separately | Upload page | smoke |
| FR-22 | Recipient second factor | Not built | — | — |
| FR-23 | Patient-held keys and recovery | Partial: passphrase-encrypted record file; no recovery phrase | `labtrail_vault` | vault |
| FR-24 | Retraction | Partial: retracted values leave the timeline and new shares; no active bundles to refresh | Timeline page | workflow |

Test files: `tests/test_labtrail_algorithms.py` (algorithms), `test_labtrail_workflow.py`
(workflow), `test_labtrail_extraction.py` (extraction), `test_labtrail_redaction.py`
(redaction), `test_labtrail_vault.py` (vault), `test_share_payload_schema.py` and
`test_candidate_extraction_schema.py` (schema), `test_app_smoke.py` (smoke). Extraction
accuracy is measured separately in [benchmark_results.md](benchmark_results.md).

## 7. Validation summary

- **115 automated tests** run on every push (GitHub Actions), including a test that
  drives the real app through upload, review, timeline, redacted share, and
  encrypted save.
- **Extraction accuracy** (row exact: value, unit, and range all correct): 100% on
  native PDFs in both layouts, 96% on simulated scans, 74% on scans with a flag
  column, 44% on simulated phone photos. Photos are below the design's 90% target.
- **Redaction** is verified by running OCR on the output image: the granted row
  is readable and no other row, name, or lab is.
- **Encryption** is verified by tamper tests: a wrong passphrase, one flipped
  ciphertext bit, or an edited header all fail to open.

## 8. Known limitations

- OCR can misread a digit and still look plausible (3.1 read as 9.1). Mandatory
  patient review is the safeguard, not the extractor.
- Text that OCR misses entirely is not in the line list, so it is not masked;
  the patient's page confirmation is the safeguard.
- The benchmark uses one font and simulated scans; real reports vary more.
- No accounts, server, recipient view, revocation, or recovery phrase.
