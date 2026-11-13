# LabTrail
LabTrail project repository
A Patient-Controlled Longitudinal Lab Record

## Run the demo

```bash
brew install tesseract                      # OCR engine (macOS)
python3 -m pip install -r requirements-dev.txt
python3 scripts/make_synthetic_reports.py   # writes samples/*.pdf + ground_truth.json
streamlit run app.py
```

The demo walks through one end-to-end slice with synthetic data only:

1. **Upload**: load PDF/PNG/JPEG reports (or the three samples) and confirm the lab,
   collection date, and that the printed name is yours.
2. **Review**: accept, correct, or reject each extracted row next to a crop of the
   source page. Nothing enters the record automatically.
3. **Timeline**: one chart per test across labs (191 tests listed in
   `reference/lab_tests.csv`), converted with a versioned unit table, with markers when the lab, unit, or reference range changes. Values with no
   supported conversion (e.g. HbA1c in mmol/mol) are listed, not plotted.
4. **Share**: pick analytes, dates, and a recipient, then **Prepare share**. Pages you
   confirmed complete become flattened images with every other line masked (and pass
   an OCR leak check); other pages are withheld.
5. **Send now**: after ticking the consent box, the share is encrypted (AES-256-GCM,
   key split in two) and registered with a local stand-in for the grant service.
   You get a link and a separate 6-digit code. Open the link in another browser tab
   to see the clinician's read-only view (email `clinician@example.org` plus the
   code). **Revoke** in "Sent shares" ends access; the access log shows every open
   and failed attempt.

The sidebar's **Save or open your record** panel encrypts the whole record into a
`.vault` file with your passphrase (Argon2id key derivation, AES-256-GCM; the file
header is authenticated so its settings cannot be weakened) and opens it again later.

Not built (designed only): a patient master key with recovery phrase, accounts, and a
hosted server. Share links work only where this app is reachable, and the grant
service is the local `.labtrail_server/` folder.

See [docs/implementation.md](docs/implementation.md) for diagrams of what is built,
changes from the design report, and requirement-by-requirement coverage.

## Development checks

Install Tesseract OCR (for example, `brew install tesseract` on macOS), then install
the test dependencies with `python3 -m pip install -r requirements-dev.txt`.
Measure extraction accuracy with `python3 scripts/benchmark_extraction.py`
(results in [docs/benchmark_results.md](docs/benchmark_results.md); the pre-improvement
run is in [docs/benchmark_baseline.md](docs/benchmark_baseline.md)).
Run the test suite with `python3 -m pytest -q` and the synthetic manual scenario
with `python3 manual_model_check.py`.

The local Python prototype can rasterize a selected PDF page with
`rasterize_pdf_page`; this is not the final browser-side Canvas implementation.
Redaction now requires a `CandidateExtractionResult` with a page-bound row
inventory, serialized and parsed against
`candidate_extraction_result_schema_v1.json`. The trusted
Z4 extraction service is not implemented yet. The local in-memory PDF/PNG/JPEG
entry point emits OCR text-line candidates for mandatory review, but always marks
coverage incomplete; it cannot authorize redacted-page delivery. Extraction
can separate a simple numeric value and unit when both are clear on one OCR line.
It enforces the design's 25 MiB upload cap and 40-million-pixel page-raster limit;
no extraction accuracy benchmark has been met.
