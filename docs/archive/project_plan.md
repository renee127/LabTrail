# LabTrail Project Plan and Estimate

This estimate follows the seven implementation increments in the [solution design report](../labtrail_solution_design_report_a2_updated.md). The scope is an academic/prototype implementation using synthetic data, not a production deployment.

| Phase | Main work and validation | Effort estimate |
|---|---|---:|
| 0. Foundation and crypto vault | App setup, authentication, key/recovery flow, encrypted storage, audit chain; recovery and storage checks | 160–240 hours |
| 1. Import and review | Report upload, ephemeral extraction, identity confirmation, accept/correct/reject flow; synthetic extraction benchmarks | 160–240 hours |
| 2. Normalization and timeline | Versioned analyte/unit rules, duplicate and comparability handling, context notes; conversion and rendering checks | 120–200 hours |
| 3. Sharing and redaction | Scoped bundles, flattened page masking, recipient viewer and verification; OCR leakage and cryptographic tamper checks | 200–300 hours |
| 4. Revocation and retraction | Access logging, key-share destruction, retraction and fail-closed behavior; verify the five-second revocation target | 120–180 hours |
| 5. Export | JSON/CSV and source-file export; schema and round-trip checks | 40–80 hours |
| 6. Hardening and final validation | Accessibility, performance, privacy/boundary review, regression and end-to-end checks | 120–200 hours |
| Integration and release fixes | Cross-phase integration, defect correction, final verification | 120–200 hours |

## Total estimate

**Approximately 1,050–1,650 person-hours**, including testing and validation.

- **One experienced developer working steadily:** approximately 7–13 months.
- **A small team of 2–3 people:** approximately 4–7 calendar months, depending on availability and integration delays.

These are order-of-magnitude planning estimates. A production deployment would require additional independent security assessment, operational readiness, and legal/privacy review; those activities are not included.

## Core algorithm plan

The algorithm helpers are implemented outside the domain dataclasses in `labtrail_algorithms.py`. Their synthetic manual scenario is in `manual_model_check.py`.

| Algorithm | Planned behavior |
|---|---|
| Review queue | Apply an injected confidence cutoff to pending candidates: missing or below-threshold value/unit confidence requires mandatory review; preserve existing mandatory routing. Partition only `CANDIDATE` items into mandatory and standard queues, preserve input order, and never accept automatically. |
| Observation chronology | Group by the recorded calendar day. Keep date-only values unpositioned within the day; sort offset-aware values by instant and no-offset local values by wall time in separate groups. |
| Duplicate detection | Group identical report hashes and matching `(analyte, collection time, source lab)` observations for patient review; do not merge or discard automatically. |
| Unit normalization | Use only explicitly supplied versioned canonical-unit and conversion-factor tables. Preserve original values, apply no built-in factors, and retain unsupported conversions with a reason. |
| Comparability | Compare adjacent same-analyte observations and report changes in source lab, method note, unit basis, or reference interval without clinical interpretation. A change to either original or canonical unit is a unit-basis change. |
| Share selection | During client-side preparation or refresh, include only accepted observations matching the grant analyte and inclusive date range. Include a context note only when it is in range and every associated analyte is in the grant scope. Mark pages with a missing granted-row region for withholding; all other source pages still require verified redaction. This in-memory selection is not an encrypted or publishable bundle. |
| Share payload projection | Project selected structured values, provenance, review status, eligible notes, page-availability status, and caller-supplied unambiguous comparability pairs into schema v1; serialize as deterministic compact UTF-8 JSON and check its shape against `share_payload_schema_v1.json`. The projection has no page bytes and is not ready to encrypt or deliver. |
| Raster redaction prototype | Mask ungranted extracted rows with `REDACTED`, re-encode pages as metadata-free PNG, and withhold pages if row coverage is unconfirmed or any region is missing, unreliable, or too small for the label. This Python prototype does not rasterize PDFs or verify OCR leakage and is not delivery-ready. |

The low-confidence cutoff `tau_conf` remains configurable and must be calibrated against the labeled synthetic benchmark before release; no numeric value is assumed. The design does not specify clinical summary metrics, so no result totals or interpretive summaries are planned until they are explicitly defined.
