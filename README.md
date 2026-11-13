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
3. **Timeline**: one chart per analyte across labs, converted with a versioned unit
   table, with markers when the lab, unit, or reference range changes. Values with no
   supported conversion (e.g. HbA1c in mmol/mol) are listed, not plotted.
4. **Share**: pick analytes, dates, and a recipient to produce a schema-validated JSON
   share. Source pages are withheld because the OCR prototype cannot prove full row
   coverage.

Not built (designed only): encryption and key management, accounts, the recipient
viewer, and revocation.

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
