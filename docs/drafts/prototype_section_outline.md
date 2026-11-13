# Outline: report updates for the prototype

Working notes for revising `docs/labtrail_solution_design_report_a2_updated.md`.
Facts and numbers only. Write the prose in your own words. Items marked **YOU** need
your own judgment or experience and cannot come from the code.

---

## 0. Header

- `Date:` change `10/10/26` to the submission date.
- Consider a subtitle line: "Revised with prototype implementation and validation".

---

## New §9. Prototype Implementation and Validation

Place after §8 (Implementation Plan, Verification Strategy & Traceability), before
the appendices. Target length: about 2–3 pages including tables and figures.

### 9.1 Scope of the prototype

- What it is: one end-to-end slice of the design, running locally on synthetic data.
- Flow: upload → confirm identity → review → timeline → share → send → clinician view
  → revoke; plus an encrypted record file.
- Relation to §8.1: covers parts of Increments 0–4; not hosted, no accounts.
- **YOU:** why you chose a vertical slice over building Increment 0 first
  (one sentence on time and on showing the whole user journey).

### 9.2 Technology and structure

- Python 3.13, Streamlit 1.67 (UI), Tesseract 5 (OCR), pypdfium2 (PDF text layer
  and rendering), Pillow, `cryptography` (AES-256-GCM, Argon2id), jsonschema,
  pandas and Altair (charts), ReportLab (synthetic samples), pytest, GitHub Actions.
- Modules and responsibilities (one line each): `app.py`, `labtrail_workflow.py`,
  `labtrail_extraction.py`, `labtrail_algorithms.py`, `labtrail_redaction.py`,
  `labtrail_sharing.py`, `labtrail_vault.py`, `models.py`.
- Design principle to state: the UI only orchestrates; every rule lives in a tested
  module.
- **Figure:** component diagram (docs/implementation.md, Figure 1).

### 9.3 How the design maps to the prototype

Short table. Source: docs/implementation.md §5 "Changes from the design report".

| Design (section) | Prototype | Reason |
| :--- | :--- | :--- |
| Generative-AI extraction in Z4 (§1.3, CMP-12–14) | Native PDFs: embedded text layer; scans and photos: Tesseract OCR with deskew, lighting correction, upscaling | Offline, measurable; text layer is exact for electronic reports |
| Extraction service declares page coverage | Patient confirms each page is complete after deciding every line | OCR cannot prove completeness; patient already reviews each line |
| Any masked token visible anywhere fails redaction | Tokens inside granted rows are exempt from the text search; text inside masks still fails | Shared rows repeat units ("mg/dL"), which blocked every mixed page |
| Recipient share in URL fragment (§7.5.2) | Query parameter `?k=` | Streamlit cannot read fragments; grant service still never receives it |
| Hosted Z3 with 5 s lease engine (§7.5.5) | Local folder; revocation immediate | Same key-split and fail-closed behavior without hosting |
| PMK wrapped by login and recovery keys (§7.5.1, FR-23) | Record file encrypted directly from passphrase (Argon2id + AES-256-GCM) | Shows patient-held keys without accounts |
| LOINC-aligned codes (FR-04) | 191-test versioned CSV catalog; patient can add tests | Fits the timeline; LOINC left for later |

New behavior not in the original design (list, one line each):
- Identification lines (name, DOB, MRN, address, dates, clinician, page footer)
  rejected automatically, listed with reason, restorable.
- Text and censored results ("Negative", "<0.5") kept as printed, never plotted.
- Tests without a unit rule charted in the reported unit only when all results share it.
- Ambiguous dates (05/06/2024) left blank rather than guessed.
- **YOU:** which of these came from your own testing (e.g. the real-format lipid
  report) and what you observed.

### 9.4 Requirement coverage

- Summary: of FR-01–FR-24, **14 Built, 10 Partial** in the local prototype.
- Partial, with the gap in a few words:
  - FR-02 metadata: no report date, ordering clinician, or report type
  - FR-03 extraction: no specimen type
  - FR-04 codes: internal codes, not LOINC
  - FR-06 duplicates: identical files skipped, not routed to a decision
  - FR-11 notes: no editing
  - FR-17 recipient view: no printable summary
  - FR-18 export: share JSON and encrypted record, no full JSON/CSV bundle
  - FR-21 identity: not persisted as `ConfirmedIdentity`
  - FR-23 keys: no recovery phrase
  - FR-24 retraction: sent shares not refreshed
- NFRs not measured: NFR-10 (WCAG), NFR-11 (latency), NFR-12 under distributed
  load, NFR-15 (availability). State this plainly.
- Point to the full table in docs/implementation.md §6 rather than repeating it.

### 9.5 Validation

Automated tests:
- 237 tests, run on every push (GitHub Actions).
- Kinds: unit (algorithms, parsing, unit rules), cryptography and tamper tests,
  redaction checked by running OCR on the output image, and end-to-end tests that
  drive the real app (patient and clinician in two sessions: send, wrong code,
  open, revoke).
- No-interpretation check (FR-13): every page and the share JSON scanned for words
  such as "abnormal", "elevated", "risk".

Extraction benchmark (hand-labeled synthetic reports; row exact = value, unit, and
range all correct):

| Condition | Before | After | NFR-09 target |
| :--- | ---: | ---: | ---: |
| Native PDF | 100% | 100% | ≥ 98% |
| Native PDF, flag column | 11% | 100% | ≥ 98% |
| Simulated scan | 74% | 96% | ≥ 90% |
| Simulated scan, flag column | 11% | 74% | ≥ 90% |
| Simulated phone photo | 0% | 44% | ≥ 90% |

- What changed between Before and After: flag parsing, deskew, conditional
  upscaling, conditional lighting correction, OCR-tolerant name matching.
- Two attempts made results worse and were narrowed (upscaling every page; lighting
  correction on every page). Worth one sentence: it shows measurement guided changes.
- Dangerous misreads (3.1 → 9.1, 6.3 → 63) look plausible: evidence for mandatory
  review (FR-08).
- **YOU:** any manual testing you did yourself (your real-format report, anything
  you uploaded) and what you noticed.
- **YOU (if done):** usability check with real people (who, task, what happened).

### 9.6 Limitations

- Phone photos below target (44% vs 90%).
- Whole-page OCR skips sparse text on scans (letterheads, side columns); skipped
  text is not masked. Patient's page confirmation is the safeguard.
- Barcodes and QR codes are images and are never masked.
- No accounts, hosted server, or recovery phrase; links work only where the app runs.
- Synthetic data only; one font; simulated scans.
- No user testing with clinicians yet (planned in §B.2 of the original).

---

## Appendix B.1: add rows for the implementation phase

Current rows cover design only. Add rows like these (fill in the right column
yourself; it must describe what **you** decided or checked):

| # | Area | AI contribution | Your verification and decisions (**YOU**) |
| :--- | :--- | :--- | :--- |
| 16 | Prototype implementation | Claude Code wrote the Streamlit app and modules from your requests | e.g. chose vertical slice; chose encrypted link + viewer over email delivery |
| 17 | Test suite and CI | Generated tests and the GitHub Actions workflow | e.g. reviewed failures, approved behavior changes |
| 18 | Extraction benchmark | Built benchmark and OCR changes, reported before/after | e.g. reviewed misses, accepted photo limitation |
| 19 | Test catalog | Loaded your CSV, fixed two entries | e.g. supplied the 191-test list; approved label change "Fasting Glucose" → "Glucose" |
| 20 | Real-report troubleshooting | Diagnosed missing name/date; added text-layer reading | e.g. supplied the real-format report and checked results |

---

## Appendix B.2: replace "Remaining Limitations & Next Steps"

- Item 1 (prototype execution, latency): now partly done; latency still not measured.
- Item 2 (benchmark): now exists; give the numbers and the photo gap.
- Item 3 (clinician usability): still open unless you run it.
- Add: second OCR pass for skipped text; ratio rows without units; recovery phrase;
  hosting with the fragment key; LOINC.

---

## Figures available

From docs/implementation.md (render-checked Mermaid):
1. Components (built vs designed)
2. Classes used by the prototype
3. Sequence: one pass through the prototype
4. Sequence: send, open, revoke a share
5. States: line, observation, page coverage, grant

Screenshots to take yourself (**YOU**): Review page with a source crop, Timeline
with a comparability marker, a redacted shared page, the clinician view.
