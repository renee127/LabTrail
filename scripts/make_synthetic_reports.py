"""Generate the synthetic LabTrail demo reports and their hand-labeled ground truth.

All labs, people, and values are fictional. The three reports are designed to
exercise the comparability problems LabTrail targets:

* the same analyte reported in different units (mg/dL vs mmol/L),
* a reference interval that changes between labs, and
* an HbA1c value in IFCC mmol/mol, which a multiply-by-factor rule cannot
  convert to NGSP %, so it must be retained as unconvertible.

Run from the repository root:  python3 scripts/make_synthetic_reports.py
"""

import json
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "samples"
PATIENT_NAME = "Jordan Sample"

REPORTS = [
    {
        "file": "harbor_reference_2026-02-10.pdf",
        "lab": "Harbor Reference Laboratory",
        "collected": "2026-02-10",
        "rows": [
            ("Glucose", "104", "mg/dL", "70-99"),
            ("Hemoglobin A1c", "6.1", "%", "4.0-5.6"),
            ("LDL Cholesterol", "131", "mg/dL", "<100"),
        ],
    },
    {
        "file": "st_brigid_hospital_2026-05-18.pdf",
        "lab": "St. Brigid Hospital Laboratory",
        "collected": "2026-05-18",
        "rows": [
            ("Glucose", "6.3", "mmol/L", "3.9-5.5"),
            ("Hemoglobin A1c", "6.3", "%", "4.0-5.6"),
            ("LDL Cholesterol", "3.1", "mmol/L", "<2.6"),
        ],
    },
    {
        "file": "coastal_specialty_2026-09-03.pdf",
        "lab": "Coastal Specialty Clinic Lab",
        "collected": "2026-09-03",
        "rows": [
            ("Glucose", "97", "mg/dL", "74-106"),
            ("Hemoglobin A1c", "44", "mmol/mol", "20-38"),
            ("LDL Cholesterol", "104", "mg/dL", "<100"),
        ],
    },
]


def draw_report(path: Path, report: dict) -> None:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.setTitle(f"{report['lab']} report")
    _, height = letter
    y = height - 72

    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, y, report["lab"])
    y -= 22
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, y, "SYNTHETIC TEST DOCUMENT - NOT A REAL PATIENT")
    y -= 30
    pdf.drawString(72, y, f"Patient: {PATIENT_NAME}")
    y -= 18
    pdf.drawString(72, y, f"Collected: {report['collected']}")
    y -= 36

    columns = (72, 260, 330, 420)
    pdf.setFont("Helvetica-Bold", 11)
    for x, label in zip(columns, ("Test", "Result", "Units", "Reference")):
        pdf.drawString(x, y, label)
    y -= 22
    pdf.setFont("Helvetica", 11)
    for row in report["rows"]:
        for x, cell in zip(columns, row):
            pdf.drawString(x, y, cell)
        y -= 20

    pdf.showPage()
    pdf.save()


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ground_truth = []
    for report in REPORTS:
        draw_report(OUTPUT_DIR / report["file"], report)
        ground_truth.append(
            {
                "file": report["file"],
                "source_lab": report["lab"],
                "patient_name": PATIENT_NAME,
                "collected": report["collected"],
                "results": [
                    {
                        "analyte": name,
                        "value": value,
                        "unit": unit,
                        "reference_interval": interval,
                    }
                    for name, value, unit, interval in report["rows"]
                ],
            }
        )
    (OUTPUT_DIR / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(REPORTS)} reports and ground_truth.json to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
