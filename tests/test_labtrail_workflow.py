import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

import pytesseract
from PIL import Image
from io import BytesIO

from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_redaction import rasterize_pdf_page, redact_source_page
from labtrail_extraction import _parse_result_line
from labtrail_workflow import (
    ANALYTE_LABELS,
    auto_reject_identification_lines,
    identification_category,
    reopen_page_coverage,
    CUSTOM_CODE_PREFIX,
    add_custom_analyte,
    all_analyte_labels,
    accept_candidate,
    load_analyte_table,
    plan_chart,
    TIMELINE_NOTICE,
    SIDEBAR_NOTICE,
    confirm_page_coverage,
    find_interpretive_language,
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
        ("Sodium", "SODIUM"),
        ("HDL Cholesterol", "HDL_C"),
        ("TOTAL CHOLESTEROL", "CHOL_TOTAL"),
        ("Non-HDL Cholesterol", "NON_HDL_C"),
        ("TSH", "TSH"),
        ("Vitamin B12 (Cyanocobalamin)", "B12"),
        ("Cyanocobalamin", "B12"),
        ("Neutrophils", None),      # absolute count or percentage: ambiguous
        ("Troponin l", None),       # OCR misread, equally close to I and T
        ("Zebra Enzyme", None),
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


def test_accept_requires_a_supported_analyte_and_clean_value() -> None:
    with pytest.raises(ValueError):
        _accept(_candidate(), code="(choose)")
    with pytest.raises(ValueError, match="not a number"):
        _accept(_candidate(), value="1O4")  # letter O read for zero
    with pytest.raises(ValueError, match="unit"):
        _accept(_candidate(), unit="")


def test_hdl_is_never_suggested_as_ldl() -> None:
    assert suggest_analyte_code("HDL Cholesterol") != suggest_analyte_code("LDL Cholesterol")


@pytest.mark.parametrize("value", ["Negative", "Yellow", "Not detected", "<0.5", "> 1000"])
def test_text_and_censored_results_are_kept_as_printed(value) -> None:
    observation = _accept(_candidate(raw_value=value, raw_unit=""), code="UA_COLOR")
    assert observation.original_value == value
    assert observation.conversion_outcome == "UNCONVERTIBLE_RETAINED"
    assert observation.unconvertible_reason == "NON_NUMERIC_RESULT"


def test_unit_spelling_matches_rule_but_printed_unit_is_kept() -> None:
    observation = _accept(_candidate(raw_unit="mg/dl"))
    assert observation.conversion_outcome == "CANONICAL_DIRECT"
    assert observation.original_unit == "mg/dl"
    lipid = _accept(_candidate(raw_analyte_name="HDL Cholesterol", raw_value="1.4", raw_unit="MMOL/L"), code="HDL_C")
    assert lipid.canonical_value == Decimal("1.4") * Decimal("38.67")


def test_unitless_number_allowed_only_without_a_unit_rule() -> None:
    inr = _accept(_candidate(raw_analyte_name="INR", raw_value="1.1", raw_unit=""), code="INR")
    assert inr.unconvertible_reason == "NO_CANONICAL_UNIT_DEFINED"


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


def _reviewed_sample(name: str = "harbor_reference_2026-02-10.pdf"):
    pdf = (SAMPLES / name).read_bytes()
    result = extract_document_ocr_line_candidates(
        source_bytes=pdf, mime_type="application/pdf", report_id=uuid4(),
        extraction_job_id=uuid4(), extracted_at=NOW,
    )
    observations = []
    for c in result.candidates:
        if is_result_like(c):
            observations.append(_accept(
                c, code=suggest_analyte_code(c.raw_analyte_name), lab="Harbor",
                collected=date(2026, 2, 10),
            ))
            c.lifecycle_status = "ACCEPTED"
        else:
            c.lifecycle_status = "REJECTED"
    return pdf, result, observations


def test_page_coverage_requires_every_line_decided() -> None:
    _, result, _ = _reviewed_sample()
    result.candidates[0].lifecycle_status = "CANDIDATE"
    with pytest.raises(ValueError, match="every line"):
        confirm_page_coverage(result, 1)
    with pytest.raises(ValueError, match="not part"):
        confirm_page_coverage(result, 2)


def test_confirmed_page_is_redacted_to_only_the_granted_row() -> None:
    pdf, result, observations = _reviewed_sample()
    page = rasterize_pdf_page(pdf, 1, dpi=150)
    grant, _, _ = build_share(
        observations=observations, notes=[], analytes=["GLUCOSE"],
        range_start=date(2026, 1, 1), range_end=date(2026, 12, 31),
        recipient_name="Dr. Test", recipient_email="dr@example.org",
        now=NOW, valid_for=timedelta(days=14),
    )

    def redact(extraction_result):
        return redact_source_page(
            page_raster=page, report_id=result.report_id, page_number=1,
            extraction_result=extraction_result, observations=observations,
            grant=grant, as_of=NOW, region_confidence_threshold=0.8,
            mask_padding_pixels=4,
        )

    unconfirmed = redact(result)
    assert unconfirmed.withheld_reason == "ROW_COVERAGE_UNVERIFIED"

    redacted = redact(confirm_page_coverage(result, 1))
    assert redacted.disposition == "REDACTED_PNG"
    text = pytesseract.image_to_string(Image.open(BytesIO(redacted.redacted_png))).casefold()
    assert "glucose" in text and "104" in text
    for hidden in ("cholesterol", "131", "hemoglobin", "6.1", "jordan", "harbor"):
        assert hidden not in text


@pytest.mark.parametrize(
    "text",
    [
        "Your glucose is elevated.",
        "This value is abnormal",
        "Higher risk of diabetes",
        "Consider a diagnosis of prediabetes",
        "We recommend you see a doctor",
        "LDL looks better than last time",
        "Result is HIGH",
    ],
)
def test_interpretive_language_is_detected(text) -> None:
    assert find_interpretive_language(text)


@pytest.mark.parametrize(
    "text",
    [
        SIDEBAR_NOTICE,
        TIMELINE_NOTICE,
        "Glucose 104 mg/dL, reference range 70-99, different lab",
        "Values were normalized to mg/dL with rule demo-units-2026.11",
        "Not plotted: no supported conversion",
    ],
)
def test_neutral_text_and_fixed_notices_pass(text) -> None:
    assert find_interpretive_language(text) == []


def _row(canonical=None, unit=None, original="1", original_unit="mg/dL", reason=None):
    return {"canonical_value": canonical, "canonical_unit": unit, "original_value": original,
            "original_unit": original_unit, "unconvertible_reason": reason}


def test_chart_plan_prefers_converted_values() -> None:
    plan = plan_chart([_row(Decimal("1"), "mg/dL"), _row(reason="NO_SUPPORTED_CONVERSION")])
    assert (plan.unit, plan.as_reported, plan.plotted, plan.listed) == ("mg/dL", False, (0,), (1,))


def test_chart_plan_plots_as_reported_only_in_one_unit() -> None:
    same = [_row(original="140", original_unit="mmol/L", reason="NO_CANONICAL_UNIT_DEFINED"),
            _row(original="138", original_unit="MMOL/L", reason="NO_CANONICAL_UNIT_DEFINED"),
            _row(original="Negative", original_unit="", reason="NON_NUMERIC_RESULT")]
    plan = plan_chart(same)
    assert (plan.unit, plan.as_reported, plan.plotted, plan.listed) == ("mmol/L", True, (0, 1), (2,))
    mixed = [_row(original="140", original_unit="mmol/L", reason="NO_CANONICAL_UNIT_DEFINED"),
             _row(original="3.2", original_unit="mg/dL", reason="NO_CANONICAL_UNIT_DEFINED")]
    assert plan_chart(mixed).plotted == ()


def test_analyte_table_loads_csv_and_rejects_bad_rows(tmp_path) -> None:
    assert len(ANALYTE_LABELS) == 191 and ANALYTE_LABELS["GLUCOSE"] == "Glucose"
    good = tmp_path / "ok.csv"
    good.write_text("Analyte Code,Test Name\nNA,Sodium\n")
    assert load_analyte_table(good) == {"NA": "Sodium"}
    for body in ("Analyte Code,Test Name\nNA,Sodium\nNA,Sodium again\n",
                 "Analyte Code,Test Name\nna-1,Sodium\n",
                 "Analyte Code,Test Name\nNA,\n"):
        bad = tmp_path / "bad.csv"
        bad.write_text(body)
        with pytest.raises(ValueError):
            load_analyte_table(bad)


def test_test_names_are_not_mistaken_for_interpretation() -> None:
    assert find_interpretive_language("High-Density Lipoprotein (HDL) 55 mg/dL") == []
    assert find_interpretive_language("Very Low-Density Lipoprotein (VLDL)") == []
    assert find_interpretive_language("HDL is high") == ["high"]


def test_added_test_gets_a_stable_custom_code_and_is_reused() -> None:
    custom: dict[str, str] = {}
    code, created = add_custom_analyte("  Homocysteine  ", custom)
    assert created and code == f"{CUSTOM_CODE_PREFIX}HOMOCYSTEINE"
    assert custom == {code: "Homocysteine"}
    assert add_custom_analyte("HOMOCYSTEINE", custom) == (code, False)
    assert all_analyte_labels(custom)[code] == "Homocysteine"
    assert all_analyte_labels(custom)["GLUCOSE"] == "Glucose"


def test_adding_a_name_already_in_the_table_reuses_that_test() -> None:
    custom: dict[str, str] = {}
    assert add_custom_analyte("HDL", custom) == ("HDL_C", False)
    assert add_custom_analyte("glucose", custom) == ("GLUCOSE", False)
    assert custom == {}


def test_codes_stay_unique_when_names_differ_only_in_punctuation() -> None:
    custom: dict[str, str] = {}
    first, _ = add_custom_analyte("Vitamin K1", custom)
    second, _ = add_custom_analyte("Vitamin K 1", custom)
    third, _ = add_custom_analyte("Vitamin K-1 (phylloquinone)", custom)
    assert len({first, second, third}) == 3
    assert third.startswith(CUSTOM_CODE_PREFIX) and len(third) <= len(CUSTOM_CODE_PREFIX) + 40


@pytest.mark.parametrize("name", ["", " ", "7", "x" * 81, "Bad\x00name"])
def test_invalid_added_names_are_refused(name) -> None:
    with pytest.raises(ValueError):
        add_custom_analyte(name, {})


def test_added_tests_are_suggested_on_later_uploads() -> None:
    custom: dict[str, str] = {}
    code, _ = add_custom_analyte("Homocysteine", custom)
    assert suggest_analyte_code("Homocysteine") is None
    assert suggest_analyte_code("HOMOCYSTEINE", custom) == code
    assert suggest_analyte_code("Glucose", custom) == "GLUCOSE"  # table still wins
    # OCR misreads are matched only within names sharing the first word, the same
    # conservative rule as the reference table.
    plasma, _ = add_custom_analyte("Homocysteine Plasma", custom)
    assert suggest_analyte_code("Homocysteine Plasrna", custom) == plasma
    assert suggest_analyte_code("Homocystelne", custom) is None


def test_accept_with_added_test_charts_as_reported() -> None:
    custom: dict[str, str] = {}
    code, _ = add_custom_analyte("Homocysteine", custom)
    candidate = _candidate(raw_analyte_name="Homocysteine", raw_value="11.2", raw_unit="umol/L")
    with pytest.raises(ValueError, match="enter its name"):
        _accept(candidate, code=code)  # unknown without the patient's list
    observation = accept_candidate(
        candidate, analyte_code=code, value="11.2", unit="umol/L", reference_interval=None,
        source_lab="Lab A", collected=date(2026, 2, 10), accepted_at=NOW, custom_analytes=custom,
    )
    assert observation.analyte_code == code
    assert observation.unconvertible_reason == "NO_CANONICAL_UNIT_DEFINED"
    plan = plan_chart([{"canonical_value": None, "canonical_unit": None, "original_value": "11.2",
                        "original_unit": "umol/L", "unconvertible_reason": "NO_CANONICAL_UNIT_DEFINED"}])
    assert plan.as_reported and plan.plotted == (0,)


def test_scanner_exempts_names_the_patient_added() -> None:
    assert find_interpretive_language("High Sensitivity Panel 4") == ["High"]
    assert find_interpretive_language("High Sensitivity Panel 4", ["High Sensitivity Panel"]) == []


def _line(text: str, **overrides) -> CandidateExtraction:
    name, value, unit, interval, flag = _parse_result_line(text)
    return _candidate(raw_analyte_name=name, raw_value=value, raw_unit=unit,
                      reference_interval=interval, abnormal_flag=flag, **overrides)


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("Patient: Jordan Sample", "name"),
        ("First Name: Jordan", "name"),
        ("Mr. Saubhik Bhaumik", "name"),
        ("DOB: 01/02/1970", "date of birth"),
        ("D.O.B. 1970-01-01", "date of birth"),
        ("Age / Sex : 27 YRS / M", "age / sex"),   # parses like a result row
        ("MRN 1234567", "identifier"),
        ("Reg. no. : 1007", "identifier"),
        ("Specimen ID 5512 ABC", "identifier"),
        ("Accession #: 99-123", "identifier"),
        ("Address: 12 Main St", "address"),
        ("123 Ocean Avenue", "address"),
        ("Phone: (561) 555-0100", "contact"),
        ("Email jordan@example.org", "contact"),
        ("Cell: 561-555-0100", "contact"),
        ("Collected: 2026-02-10", "report date"),
        ("Registered on : 17/10/2024 05:32 PM", "report date"),
        ("Referred by : Dr. Sachin Patil (MBBS)", "clinician"),
        ("Dr. A. K. Asthana", "clinician"),
        ("Page 1 of 2", "page footer"),
    ],
)
def test_identification_lines_are_recognised(text, category) -> None:
    assert identification_category(_line(text)) == category


@pytest.mark.parametrize(
    "text",
    [
        "Glucose 104 mg/dL 70-99", "Sodium 140 mmol/L", "Total Protein 7.1 g/dL",
        "Specific Gravity 1.020", "Free T4 1.2 ng/dL", "Base Excess -1 mmol/L",
        "Harbor Reference Laboratory", "St. Brigid Hospital Laboratory",
        "Test Result Units Reference", "SYNTHETIC TEST DOCUMENT - NOT A REAL PATIENT",
        "cell membranes and in blood plasma. It is an essential component",
    ],
)
def test_results_and_lab_names_are_never_auto_rejected(text) -> None:
    assert identification_category(_line(text)) is None


def test_a_test_the_patient_added_is_never_auto_rejected() -> None:
    line = _line("Age Adjusted Score 12 pts")
    assert identification_category(line) == "age / sex"
    custom: dict[str, str] = {}
    add_custom_analyte("Age Adjusted Score", custom)
    assert identification_category(line, custom) is None


def test_auto_reject_only_touches_pending_lines_and_reports_why() -> None:
    _, result, _ = _reviewed_sample()
    for c in result.candidates:
        c.lifecycle_status = "CANDIDATE"
    rejected = auto_reject_identification_lines(result)
    by_text = {c.raw_analyte_name: rejected.get(c.candidate_id) for c in result.candidates}
    assert by_text["Patient: Jordan Sample"] == "name"
    assert by_text["Collected: 2026-02-10"] == "report date"
    assert by_text["Glucose"] is None
    assert all(c.lifecycle_status == "REJECTED" for c in result.candidates if c.candidate_id in rejected)
    assert auto_reject_identification_lines(result) == {}  # nothing pending is re-processed


def test_restoring_a_line_withdraws_page_confirmation() -> None:
    _, result, _ = _reviewed_sample()
    confirmed = confirm_page_coverage(result, 1)
    assert reopen_page_coverage(confirmed, 1).page_inventories[0].inventory_status == "INCOMPLETE"
