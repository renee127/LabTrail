"""Score the OCR extraction prototype against hand-labeled synthetic reports.

Each synthetic report is rendered under several conditions (native PDF, a second
layout, simulated scans, simulated phone photos). Every extracted row is compared
with ground truth field by field. Results go to stdout and docs/benchmark_results.md.

Run from the repository root:  python3 scripts/benchmark_extraction.py
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytesseract
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from labtrail_extraction import extract_document_ocr_line_candidates  # noqa: E402
from labtrail_redaction import rasterize_pdf_page  # noqa: E402
from labtrail_workflow import (  # noqa: E402
    is_result_like,
    suggest_analyte_code,
    suggest_report_metadata,
)
from make_synthetic_reports import PATIENT_NAME, REPORTS, render_report  # noqa: E402


RESULTS_PATH = ROOT / "docs" / "benchmark_results.md"
DEGRADED_SEEDS = (1, 2, 3)
EXTRACTED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- conditions

def _to_jpeg(image: Image.Image, quality: int) -> bytes:
    out = BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _add_noise(image: Image.Image, sigma: float, rng: np.random.Generator) -> Image.Image:
    pixels = np.asarray(image.convert("L"), dtype=np.float32)
    pixels += rng.normal(0, sigma, pixels.shape)
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))


def simulate_scan(pdf: bytes, seed: int) -> tuple[bytes, str]:
    """Flatbed scan: 200 dpi, slight skew, mild blur and noise, JPEG."""
    rng = np.random.default_rng(seed)
    page = Image.open(BytesIO(rasterize_pdf_page(pdf, 1, dpi=200)))
    page = page.rotate(rng.uniform(-1.5, 1.5), expand=True, fillcolor="white")
    page = page.filter(ImageFilter.GaussianBlur(0.6))
    page = _add_noise(page, 12, rng)
    return _to_jpeg(page, 70), "image/jpeg"


def simulate_photo(pdf: bytes, seed: int) -> tuple[bytes, str]:
    """Phone photo: low resolution, more skew, uneven lighting, blur, heavy JPEG."""
    rng = np.random.default_rng(seed)
    page = Image.open(BytesIO(rasterize_pdf_page(pdf, 1, dpi=110))).convert("L")
    page = page.rotate(rng.uniform(-3.5, 3.5), expand=True, fillcolor=235)
    pixels = np.asarray(page, dtype=np.float32)
    height, width = pixels.shape
    shade = np.linspace(1.0, 0.65, width)[None, :] * np.linspace(1.0, 0.85, height)[:, None]
    page = Image.fromarray(np.clip(pixels * shade, 0, 255).astype(np.uint8))
    page = page.filter(ImageFilter.GaussianBlur(0.9))
    page = _add_noise(page, 10, rng)
    return _to_jpeg(page, 55), "image/jpeg"


@dataclass(frozen=True)
class Condition:
    name: str
    description: str
    layout: str
    seeds: tuple[int, ...]
    make: Callable[[bytes, int], tuple[bytes, str]]


CONDITIONS = (
    Condition("pdf", "Native PDF, 4-column table", "table", (0,),
              lambda pdf, _: (pdf, "application/pdf")),
    Condition("pdf-flagged", "Native PDF, H/L flag column before units", "flagged", (0,),
              lambda pdf, _: (pdf, "application/pdf")),
    Condition("scan", "Simulated scan: 200 dpi, ±1.5° skew, blur, noise, JPEG q70",
              "table", DEGRADED_SEEDS, simulate_scan),
    Condition("photo", "Simulated phone photo: 110 dpi, ±3.5° skew, shading, blur, JPEG q55",
              "table", DEGRADED_SEEDS, simulate_photo),
    Condition("scan-flagged", "Simulated scan of the flagged layout", "flagged",
              DEGRADED_SEEDS, simulate_scan),
)


# ---------------------------------------------------------------- scoring

FIELDS = ("value", "unit", "reference_interval")


@dataclass
class Score:
    truth_rows: int = 0
    found: int = 0
    correct: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    row_exact: int = 0
    extra_rows: int = 0
    metadata_checks: int = 0
    metadata_correct: int = 0
    misses: list[str] = field(default_factory=list)

    def pct(self, numerator: int, denominator: int | None = None) -> str:
        denominator = self.truth_rows if denominator is None else denominator
        return f"{100 * numerator / denominator:.0f}%" if denominator else "n/a"


def score_document(score: Score, data: bytes, mime: str, report: dict, label: str) -> None:
    result = extract_document_ocr_line_candidates(
        source_bytes=data, mime_type=mime, report_id=uuid4(),
        extraction_job_id=uuid4(), extracted_at=EXTRACTED_AT,
    )
    meta = suggest_report_metadata(result)
    for ok in (
        meta.source_lab == report["lab"],
        meta.patient_name == PATIENT_NAME,
        meta.collected == date.fromisoformat(report["collected"]),
    ):
        score.metadata_checks += 1
        score.metadata_correct += ok

    rows = [c for c in result.candidates if is_result_like(c)]
    unmatched = list(rows)
    for name, value, unit, interval, _flag in report["rows"]:
        score.truth_rows += 1
        code = suggest_analyte_code(name)
        match = next(
            (c for c in unmatched if suggest_analyte_code(c.raw_analyte_name) == code),
            None,
        )
        if match is None:
            near = next(
                (c.raw_analyte_name for c in result.candidates
                 if name.split()[0].lower() in c.raw_analyte_name.lower()),
                "(no line)",
            )
            score.misses.append(f"{label}: {name} not found as a result row; OCR line: “{near}”")
            continue
        unmatched.remove(match)
        score.found += 1
        got = {"value": match.raw_value, "unit": match.raw_unit,
               "reference_interval": match.reference_interval}
        want = {"value": value, "unit": unit, "reference_interval": interval}
        wrong = [f for f in FIELDS if got[f] != want[f]]
        for f in FIELDS:
            score.correct[f] += f not in wrong
        if wrong:
            score.misses.append(
                f"{label}: {name} " + ", ".join(f"{f} expected “{want[f]}” got “{got[f]}”" for f in wrong)
            )
        else:
            score.row_exact += 1
    score.extra_rows += len(unmatched)


def run() -> dict[str, Score]:
    scores: dict[str, Score] = {}
    for condition in CONDITIONS:
        score = scores[condition.name] = Score()
        for report in REPORTS:
            pdf = render_report(report, condition.layout)
            for seed in condition.seeds:
                data, mime = condition.make(pdf, seed)
                label = f"{condition.name}/{report['file'].split('_')[0]}" + (f"/seed{seed}" if seed else "")
                score_document(score, data, mime, report, label)
    return scores


# ---------------------------------------------------------------- report

def render(scores: dict[str, Score]) -> str:
    lines = [
        "# Extraction benchmark results",
        "",
        f"Generated {datetime.now():%Y-%m-%d} by `scripts/benchmark_extraction.py` "
        f"with Tesseract {pytesseract.get_tesseract_version()}.",
        "",
        "The extractor is the conservative OCR-line prototype in `labtrail_extraction.py`. "
        "Every report is synthetic (3 fictional labs × 3 results). Degraded conditions are "
        f"rendered with seeds {', '.join(map(str, DEGRADED_SEEDS))}, so each covers 27 result rows; "
        "native PDF conditions cover 9.",
        "",
        "**Row exact** means value, unit, and reference range all match ground truth. "
        "The design target (NFR-09) is ≥ 98% for machine PDFs and ≥ 90% for scans/photos.",
        "",
        "| Condition | Description | Rows | Found | Value | Unit | Ref. range | Row exact | Extra rows | Metadata |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in CONDITIONS:
        s = scores[condition.name]
        lines.append(
            f"| `{condition.name}` | {condition.description} | {s.truth_rows} | {s.pct(s.found)} | "
            f"{s.pct(s.correct['value'])} | {s.pct(s.correct['unit'])} | "
            f"{s.pct(s.correct['reference_interval'])} | **{s.pct(s.row_exact)}** | {s.extra_rows} | "
            f"{s.pct(s.metadata_correct, s.metadata_checks)} |"
        )
    lines += ["", "## Misses", ""]
    misses = [m for c in CONDITIONS for m in scores[c.name].misses]
    lines += [f"- {m}" for m in misses] or ["None."]
    lines += [
        "",
        "## Caveats",
        "",
        "- Nine distinct result rows and one font; real reports vary far more.",
        "- Scans and photos are simulated, not captured with real devices.",
        "- Every extracted row still requires patient review, so misses become review work, "
        "not silent errors.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    markdown = render(run())
    RESULTS_PATH.parent.mkdir(exist_ok=True)
    RESULTS_PATH.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
