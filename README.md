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
The raster redaction prototype requires page-bound row-coverage evidence from an
independent inventory; that producer is not implemented yet, so pages are not
delivery-ready even when a synthetic test supplies matching evidence.
