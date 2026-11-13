# LabTrail
LabTrail project repository
A Patient-Controlled Longitudinal Lab Record

## Development checks

Install Tesseract OCR (for example, `brew install tesseract` on macOS), then install
the test dependencies with `python3 -m pip install -r requirements-dev.txt`.
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
