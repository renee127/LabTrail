import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_workflow import (
    accept_candidate,
    analyte_timeline,
    build_share,
    is_result_like,
    suggest_analyte_code,
    suggest_report_metadata,
)
from models import CandidateExtraction, ContextNote


SAMPLES = Path(__file__).resolve().parents[1] / "samples"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def _candidate(**overrides) -> CandidateExtraction:
    fields = dict(
        candidate_id=uuid4(), report_id=uuid4(), extraction_job_id=uuid4(),
        row_index=0, raw_analyte_name="Glucose", proposed_analyte_code="UNMAPPED",
        raw_value="104", raw_unit="mg/dL", reference_interval="70-99",
        method_note=None, specimen_type=None, abnormal_flag=None, page_number=1,
        bounding_box=(0.1, 0.4, 0.6, 0.42), region_available=True,
        field_confidences={"analyte": 0.9, "value": 0.9, "unit": 0.9, "region": 0.9},
        model_identifier="test", model_version="1", prompt_hash="0" * 64,
        extracted_at=NOW, routing_status="MANDATORY_EXCEPTION_REVIEW",
        lifecycle_status="CANDIDATE",
    )
    fields.update(overrides)
    return CandidateExtraction(**fields)


def _accept(candidate, *, code="GLUCOSE", lab="Lab A", collected=date(2026, 2, 10), **fields):
    return accept_candidate(
        candidate,
        analyte_code=code,
        value=fields.get("value", candidate.raw_value),
        unit=fields.get("unit", candidate.raw_unit),
        reference_interval=fields.get("ref", candidate.reference_interval),
        source_lab=lab,
        collected=collected,
        accepted_at=NOW,
    )


@pytest.mark.parametrize(
    ("printed", "code"),
    [
        ("Glucose", "GLUCOSE"),
        ("Hemoglobin  A1c", "HBA1C"),
        ("LDL Cholesterol", "LDL_C"),
        ("Hemoglobin Aic", "HBA1C"),
        ("LDL Cholestero!", "LDL_C"),
        ("LDL Cholesterol]", "LDL_C"),
        ("Sodium", None),
        ("HDL Cholesterol", None),
    ],
)
def test_suggest_analyte_code(printed, code) -> None:
    assert suggest_analyte_code(printed) == code


def test_accept_as_extracted_keeps_provenance_and_converts_directly() -> None:
    observation = _accept(_candidate())
    assert observation.provenance_flag == "AS_EXTRACTED"
    assert observation.conversion_outcome == "CANONICAL_DIRECT"
    assert observation.canonical_value == Decimal("104")


def test_patient_correction_is_recorded() -> None:
    observation = _accept(_candidate(), value="140")
    assert observation.provenance_flag == "CORRECTED_BY_PATIENT"
    assert observation.original_value == "140"


def test_mmol_glucose_is_converted_with_versioned_factor() -> None:
    observation = _accept(_candidate(raw_value="6.3", raw_unit="mmol/L"))
    assert observation.conversion_outcome == "CONVERTED_DETERMINISTIC"
    assert observation.canonical_unit == "mg/dL"
    assert observation.canonical_value == Decimal("6.3") * Decimal("18.016")


def test_ifcc_hba1c_is_retained_not_converted() -> None:
    observation = _accept(
        _candidate(raw_analyte_name="Hemoglobin A1c", raw_value="44", raw_unit="mmol/mol"),
        code="HBA1C",
    )
    assert observation.conversion_outcome == "UNCONVERTIBLE_RETAINED"
    assert observation.canonical_value is None
    assert observation.original_value == "44"


def test_accept_requires_a_supported_analyte_and_numeric_value() -> None:
    with pytest.raises(ValueError):
        _accept(_candidate(), code="(choose)")
    with pytest.raises(ValueError):
        _accept(_candidate(), value="high")


def test_timeline_orders_by_date_and_flags_comparability_changes() -> None:
    later = _accept(_candidate(raw_value="6.3", raw_unit="mmol/L", reference_interval="3.9-5.5"),
                    lab="Lab B", collected=date(2026, 5, 18))
    earlier = _accept(_candidate())
    points = analyte_timeline([later, earlier], "GLUCOSE")
    assert [p.observation for p in points] == [earlier, later]
    assert points[0].changes_from_previous == ()
    assert set(points[1].changes_from_previous) == {"source_lab", "unit_basis", "reference_interval"}


def test_retracted_values_leave_the_timeline_and_shares() -> None:
    kept = _accept(_candidate())
    retracted = _accept(_candidate(), collected=date(2026, 3, 1))
    retracted.lifecycle_status = "RETRACTED_BY_PATIENT"
    assert [p.observation for p in analyte_timeline([kept, retracted], "GLUCOSE")] == [kept]
    _, selection, _ = build_share(
        observations=[kept, retracted], notes=[], analytes=["GLUCOSE"],
        range_start=date(2026, 1, 1), range_end=date(2026, 12, 31),
        recipient_name="Dr. Test", recipient_email="dr@example.org",
        now=NOW, valid_for=timedelta(days=14),
    )
    assert selection.observations == (kept,)


def test_share_is_scoped_and_schema_valid() -> None:
    glucose = _accept(_candidate())
    glucose_b = _accept(_candidate(raw_value="6.3", raw_unit="mmol/L"), lab="Lab B", collected=date(2026, 5, 18))
    ldl = _accept(_candidate(raw_analyte_name="LDL Cholesterol", raw_value="131"), code="LDL_C")
    out_of_range = _accept(_candidate(), collected=date(2025, 6, 1))
    in_scope_note = ContextNote(uuid4(), "MEDICATION_CHANGE", date(2026, 3, 1), "Started med", ["GLUCOSE"], NOW, NOW)
    mixed_note = ContextNote(uuid4(), "DIET", date(2026, 3, 1), "Diet", ["GLUCOSE", "LDL_C"], NOW, NOW)

    _, selection, payload = build_share(
        observations=[glucose_b, ldl, out_of_range, glucose], notes=[in_scope_note, mixed_note],
        analytes=["GLUCOSE"], range_start=date(2026, 1, 1), range_end=date(2026, 12, 31),
        recipient_name="Dr. Test", recipient_email="dr@example.org",
        now=NOW, valid_for=timedelta(days=14),
    )
    data = json.loads(payload)
    assert [o["observation_id"] for o in data["observations"]] == [str(glucose.observation_id), str(glucose_b.observation_id)]
    assert data["observations"][0]["comparability_status"] == "NOT_EVALUATED"
    assert data["observations"][1]["comparability_status"] == "COMPARED"
    assert [n["note_id"] for n in data["context_notes"]] == [str(in_scope_note.note_id)]
    assert "dr@example.org" not in payload.decode()


def test_sample_reports_extract_and_suggest_metadata() -> None:
    truth = json.loads((SAMPLES / "ground_truth.json").read_text())
    for report in truth:
        result = extract_document_ocr_line_candidates(
            source_bytes=(SAMPLES / report["file"]).read_bytes(), mime_type="application/pdf",
            report_id=uuid4(), extraction_job_id=uuid4(), extracted_at=NOW,
        )
        meta = suggest_report_metadata(result)
        assert meta.source_lab == report["source_lab"]
        assert meta.patient_name == report["patient_name"]
        assert meta.collected == date.fromisoformat(report["collected"])
        rows = [(c.raw_analyte_name, c.raw_value, c.raw_unit, c.reference_interval)
                for c in result.candidates if is_result_like(c)]
        expected = [(r["analyte"], r["value"], r["unit"], r["reference_interval"]) for r in report["results"]]
        assert rows == expected
