# Extraction benchmark results

Generated 2026-11-13 by `scripts/benchmark_extraction.py` with Tesseract 5.5.4.

The extractor is the conservative OCR-line prototype in `labtrail_extraction.py`. Every report is synthetic (3 fictional labs × 3 results). Degraded conditions are rendered with seeds 1, 2, 3, so each covers 27 result rows; native PDF conditions cover 9.

**Row exact** means value, unit, and reference range all match ground truth. The design target (NFR-09) is ≥ 98% for machine PDFs and ≥ 90% for scans/photos.

| Condition | Description | Rows | Found | Value | Unit | Ref. range | Row exact | Extra rows | Metadata |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `pdf` | Native PDF, 4-column table | 9 | 100% | 100% | 100% | 100% | **100%** | 0 | 100% |
| `pdf-flagged` | Native PDF, H/L flag column before units | 9 | 100% | 100% | 100% | 100% | **100%** | 0 | 100% |
| `scan` | Simulated scan: 200 dpi, ±1.5° skew, blur, noise, JPEG q70 | 27 | 100% | 96% | 100% | 100% | **96%** | 0 | 100% |
| `photo` | Simulated phone photo: 110 dpi, ±3.5° skew, shading, blur, JPEG q55 | 27 | 89% | 81% | 48% | 89% | **44%** | 2 | 67% |
| `scan-flagged` | Simulated scan of the flagged layout | 27 | 93% | 89% | 78% | 93% | **74%** | 0 | 100% |

## Misses

- scan/st/seed1: LDL Cholesterol value expected “3.1” got “9.1”
- photo/harbor/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/harbor/seed2: Glucose unit expected “mg/dL” got “mg/dt”
- photo/harbor/seed2: LDL Cholesterol value expected “131” got “134”, unit expected “mg/dL” got “mg/d.”
- photo/harbor/seed3: Glucose unit expected “mg/dL” got “mag/at.”
- photo/st/seed1: Glucose unit expected “mmol/L” got “mmol/L.”
- photo/st/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/st/seed1: LDL Cholesterol unit expected “mmol/L” got “mmoVv/L”
- photo/st/seed2: Glucose unit expected “mmol/L” got “mmovL”
- photo/st/seed2: LDL Cholesterol unit expected “mmol/L” got “mmovL”
- photo/st/seed3: Glucose value expected “6.3” got “63”
- photo/coastal/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/coastal/seed2: Glucose unit expected “mg/dL” got “mg/dt”
- photo/coastal/seed2: LDL Cholesterol unit expected “mg/dL” got “mg/d.”
- photo/coastal/seed3: Glucose unit expected “mg/dL” got “mag/dt.”
- photo/coastal/seed3: Hemoglobin A1c unit expected “mmol/mol” got “tmmol/mol”
- scan-flagged/harbor/seed2: LDL Cholesterol unit expected “mg/dL” got “mg/dl”
- scan-flagged/harbor/seed3: LDL Cholesterol unit expected “mg/dL” got “mg/dl”
- scan-flagged/st/seed1: LDL Cholesterol value expected “3.1” got “34”
- scan-flagged/st/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero! 3:4 H mmol/L <2.6”
- scan-flagged/st/seed3: LDL Cholesterol not found as a result row; OCR line: “LDL Cholesterol a1 H mmol/L <2.6”
- scan-flagged/coastal/seed2: LDL Cholesterol unit expected “mg/dL” got “mg/dl”
- scan-flagged/coastal/seed3: LDL Cholesterol unit expected “mg/dL” got “mg/dl”

## Caveats

- Nine distinct result rows and one font; real reports vary far more.
- Scans and photos are simulated, not captured with real devices.
- Every extracted row still requires patient review, so misses become review work, not silent errors.
