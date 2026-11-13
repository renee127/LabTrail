# Extraction benchmark — baseline (before OCR improvements)

Generated 2026-11-12 by `scripts/benchmark_extraction.py` with Tesseract 5.5.4.

The extractor is the conservative OCR-line prototype in `labtrail_extraction.py`. Every report is synthetic (3 fictional labs × 3 results). Degraded conditions are rendered with seeds 1, 2, 3, so each covers 27 result rows; native PDF conditions cover 9.

**Row exact** means value, unit, and reference range all match ground truth. The design target (NFR-09) is ≥ 98% for machine PDFs and ≥ 90% for scans/photos.

| Condition | Description | Rows | Found | Value | Unit | Ref. range | Row exact | Extra rows | Metadata |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `pdf` | Native PDF, 4-column table | 9 | 100% | 100% | 100% | 100% | **100%** | 0 | 100% |
| `pdf-flagged` | Native PDF, H/L flag column before units | 9 | 100% | 100% | 11% | 11% | **11%** | 0 | 100% |
| `scan` | Simulated scan: 200 dpi, ±1.5° skew, blur, noise, JPEG q70 | 27 | 78% | 74% | 78% | 78% | **74%** | 5 | 100% |
| `photo` | Simulated phone photo: 110 dpi, ±3.5° skew, shading, blur, JPEG q55 | 27 | 7% | 4% | 0% | 0% | **0%** | 1 | 0% |
| `scan-flagged` | Simulated scan of the flagged layout | 27 | 74% | 70% | 11% | 11% | **11%** | 6 | 100% |

## Misses

- pdf-flagged/harbor: Glucose unit expected “mg/dL” got “H”, reference_interval expected “70-99” got “None”
- pdf-flagged/harbor: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- pdf-flagged/harbor: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- pdf-flagged/st: Glucose unit expected “mmol/L” got “H”, reference_interval expected “3.9-5.5” got “None”
- pdf-flagged/st: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- pdf-flagged/st: LDL Cholesterol unit expected “mmol/L” got “H”, reference_interval expected “<2.6” got “None”
- pdf-flagged/coastal: Hemoglobin A1c unit expected “mmol/mol” got “H”, reference_interval expected “20-38” got “None”
- pdf-flagged/coastal: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- scan/harbor/seed1: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Aic”
- scan/harbor/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero!”
- scan/st/seed1: LDL Cholesterol value expected “3.1” got “9.1”
- scan/st/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero!”
- scan/st/seed3: LDL Cholesterol not found as a result row; OCR line: “LDL Cholesterol 3:1 mmol/L <2.6”
- scan/coastal/seed1: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Aic”
- scan/coastal/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero!”
- photo/harbor/seed1: Glucose value expected “104” got “16”, unit expected “mg/dL” got “eg”, reference_interval expected “70-99” got “None”
- photo/harbor/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/harbor/seed1: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/harbor/seed2: Glucose not found as a result row; OCR line: “(no line)”
- photo/harbor/seed2: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/harbor/seed2: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/harbor/seed3: Glucose not found as a result row; OCR line: “(no line)”
- photo/harbor/seed3: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Ate “ee”
- photo/harbor/seed3: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/st/seed1: Glucose not found as a result row; OCR line: “Glucose 63 mn, RR”
- photo/st/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/st/seed1: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/st/seed2: Glucose not found as a result row; OCR line: “(no line)”
- photo/st/seed2: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin atc”
- photo/st/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero! 31 | on ee”
- photo/st/seed3: Glucose not found as a result row; OCR line: “(no line)”
- photo/st/seed3: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/st/seed3: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/coastal/seed1: Glucose not found as a result row; OCR line: “(no line)”
- photo/coastal/seed1: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/coastal/seed1: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/coastal/seed2: Glucose unit expected “mg/dL” got “a”, reference_interval expected “74-106” got “None”
- photo/coastal/seed2: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/coastal/seed2: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- photo/coastal/seed3: Glucose not found as a result row; OCR line: “(no line)”
- photo/coastal/seed3: Hemoglobin A1c not found as a result row; OCR line: “(no line)”
- photo/coastal/seed3: LDL Cholesterol not found as a result row; OCR line: “(no line)”
- scan-flagged/harbor/seed1: Glucose unit expected “mg/dL” got “H”, reference_interval expected “70-99” got “None”
- scan-flagged/harbor/seed1: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Aic”
- scan-flagged/harbor/seed1: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- scan-flagged/harbor/seed2: Glucose unit expected “mg/dL” got “H”, reference_interval expected “70-99” got “None”
- scan-flagged/harbor/seed2: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- scan-flagged/harbor/seed2: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- scan-flagged/harbor/seed3: Glucose unit expected “mg/dL” got “H”, reference_interval expected “70-99” got “None”
- scan-flagged/harbor/seed3: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- scan-flagged/harbor/seed3: LDL Cholesterol not found as a result row; OCR line: “LDL Cholesterol]”
- scan-flagged/st/seed1: Glucose unit expected “mmol/L” got “H”, reference_interval expected “3.9-5.5” got “None”
- scan-flagged/st/seed1: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Aic”
- scan-flagged/st/seed1: LDL Cholesterol value expected “3.1” got “34”, unit expected “mmol/L” got “H”, reference_interval expected “<2.6” got “None”
- scan-flagged/st/seed2: Glucose unit expected “mmol/L” got “H”, reference_interval expected “3.9-5.5” got “None”
- scan-flagged/st/seed2: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- scan-flagged/st/seed2: LDL Cholesterol not found as a result row; OCR line: “LDL Cholestero! Cg | H mmol/L <2.6”
- scan-flagged/st/seed3: Glucose unit expected “mmol/L” got “H”, reference_interval expected “3.9-5.5” got “None”
- scan-flagged/st/seed3: Hemoglobin A1c unit expected “%” got “H”, reference_interval expected “4.0-5.6” got “None”
- scan-flagged/st/seed3: LDL Cholesterol not found as a result row; OCR line: “LDL Cholesterol]”
- scan-flagged/coastal/seed1: Hemoglobin A1c not found as a result row; OCR line: “Hemoglobin Aic”
- scan-flagged/coastal/seed1: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- scan-flagged/coastal/seed2: Hemoglobin A1c unit expected “mmol/mol” got “H”, reference_interval expected “20-38” got “None”
- scan-flagged/coastal/seed2: LDL Cholesterol unit expected “mg/dL” got “H”, reference_interval expected “<100” got “None”
- scan-flagged/coastal/seed3: Hemoglobin A1c unit expected “mmol/mol” got “H”, reference_interval expected “20-38” got “None”
- scan-flagged/coastal/seed3: LDL Cholesterol not found as a result row; OCR line: “LDL Cholesterol]”

## Caveats

- Nine distinct result rows and one font; real reports vary far more.
- Scans and photos are simulated, not captured with real devices.
- Every extracted row still requires patient review, so misses become review work, not silent errors.
