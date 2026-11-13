"""Unit tests for the pure algorithms in labtrail_algorithms.py."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

import manual_model_check
from labtrail_algorithms import (
    build_share_payload_projection,
    classify_candidate_routing,
    compare_adjacent_observations,
    filter_context_notes_for_grant,
    filter_observations_for_grant,
    find_duplicate_observation_groups,
    find_duplicate_report_groups,
    group_observations_by_collection_day,
    normalize_value,
    partition_review_queue,
    prepare_share_content_selection,
)
from models import (
    AcceptedObservation,
    CandidateExtraction,
    ClientShareGrant,
    ContextNote,
    SourceReport,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
CANONICAL = {"GLUCOSE": "mg/dL"}
FACTORS = {("GLUCOSE", "mmol/L", "mg/dL"): Decimal("18.016")}


def candidate(**overrides) -> CandidateExtraction:
    fields = dict(
        candidate_id=uuid4(), report_id=uuid4(), extraction_job_id=uuid4(),
        row_index=0, raw_analyte_name="Glucose", proposed_analyte_code="GLUCOSE",
        raw_value="98", raw_unit="mg/dL", reference_interval=None, method_note=None,
        specimen_type=None, abnormal_flag=None, page_number=1,
        bounding_box=(0.1, 0.1, 0.5, 0.15), region_available=True,
        field_confidences={"analyte": 0.95, "value": 0.95, "unit": 0.95, "region": 0.95},
        model_identifier="test", model_version="1", prompt_hash="0" * 64,
        extracted_at=NOW, routing_status="STANDARD_REVIEW", lifecycle_status="CANDIDATE",
    )
    fields.update(overrides)
    return CandidateExtraction(**fields)


def observation(**overrides) -> AcceptedObservation:
    fields = dict(
        observation_id=uuid4(), candidate_id=uuid4(), report_id=uuid4(),
        analyte_code="GLUCOSE", canonical_value=Decimal("98"), canonical_unit="mg/dL",
        original_value="98", original_unit="mg/dL", conversion_factor=Decimal("1"),
        conversion_outcome="CANONICAL_DIRECT", unconvertible_reason=None,
        reference_interval="70-99", method_note=None, source_lab="Lab A",
        collection_datetime=date(2026, 3, 1), page_number=1,
        bounding_box=(0.1, 0.1, 0.5, 0.15), provenance_flag="AS_EXTRACTED",
        lifecycle_status="ACCEPTED", rule_version="test-1", accepted_at=NOW,
    )
    fields.update(overrides)
    return AcceptedObservation(**fields)


def grant(**overrides) -> ClientShareGrant:
    fields = dict(
        grant_id=uuid4(), recipient_name="Dr. Test", recipient_identity_hash="hash",
        granted_analytes=["GLUCOSE"], range_start=date(2026, 1, 1),
        range_end=date(2026, 6, 30), created_at=NOW,
        expires_at=NOW + timedelta(days=14), local_status="PREPARING_CLIENT",
    )
    fields.update(overrides)
    return ClientShareGrant(**fields)


def note(note_date: date, codes: list[str]) -> ContextNote:
    return ContextNote(uuid4(), "FREE_TEXT", note_date, "note", codes, NOW, NOW)


# --- review queue ---------------------------------------------------------

def test_partition_preserves_order_and_skips_decided_candidates() -> None:
    m1 = candidate(routing_status="MANDATORY_EXCEPTION_REVIEW")
    s1 = candidate()
    done = candidate(lifecycle_status="ACCEPTED")
    m2 = candidate(routing_status="MANDATORY_EXCEPTION_REVIEW")
    mandatory, standard = partition_review_queue([m1, s1, done, m2])
    assert mandatory == [m1, m2]
    assert standard == [s1]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "STANDARD_REVIEW"),
        ({"raw_value": None}, "MANDATORY_EXCEPTION_REVIEW"),
        ({"raw_unit": " "}, "MANDATORY_EXCEPTION_REVIEW"),
        (
            {"field_confidences": {"analyte": 0.95, "value": 0.5, "unit": 0.95, "region": 0.95}},
            "MANDATORY_EXCEPTION_REVIEW",
        ),
        ({"routing_status": "MANDATORY_EXCEPTION_REVIEW"}, "MANDATORY_EXCEPTION_REVIEW"),
    ],
)
def test_routing_sends_missing_or_low_confidence_fields_to_mandatory_review(overrides, expected) -> None:
    assert classify_candidate_routing(candidate(**overrides), 0.9) == expected


@pytest.mark.parametrize("threshold", [-0.1, 1.1, float("nan"), True])
def test_routing_rejects_invalid_thresholds(threshold) -> None:
    with pytest.raises(ValueError):
        classify_candidate_routing(candidate(), threshold)


def test_routing_rejects_already_decided_candidates() -> None:
    with pytest.raises(ValueError):
        classify_candidate_routing(candidate(lifecycle_status="REJECTED"), 0.9)


# --- unit normalization ---------------------------------------------------

def _normalize(value: str, unit: str, code: str = "GLUCOSE"):
    return normalize_value(
        analyte_code=code, original_value=value, original_unit=unit,
        canonical_units=CANONICAL, conversion_factors=FACTORS, rule_version="test-1",
    )


def test_normalize_direct_converted_and_unsupported() -> None:
    direct = _normalize("98", "mg/dL")
    assert (direct.conversion_outcome, direct.canonical_value) == ("CANONICAL_DIRECT", Decimal("98"))

    converted = _normalize("5.5", "mmol/L")
    assert converted.conversion_outcome == "CONVERTED_DETERMINISTIC"
    assert converted.canonical_value == Decimal("99.0880")
    assert converted.original_value == "5.5"

    unsupported = _normalize("5.5", "mEq/L")
    assert unsupported.conversion_outcome == "UNCONVERTIBLE_RETAINED"
    assert unsupported.unconvertible_reason == "NO_SUPPORTED_CONVERSION"
    assert unsupported.canonical_value is None

    unknown = _normalize("1", "g/L", code="SODIUM")
    assert unknown.unconvertible_reason == "NO_CANONICAL_UNIT_DEFINED"


@pytest.mark.parametrize("value", ["", "high", "NaN", "Infinity"])
def test_normalize_rejects_non_numeric_values(value) -> None:
    with pytest.raises(ValueError):
        _normalize(value, "mg/dL")


def test_normalize_rejects_non_positive_factor() -> None:
    with pytest.raises(ValueError):
        normalize_value(
            analyte_code="GLUCOSE", original_value="5", original_unit="mmol/L",
            canonical_units=CANONICAL,
            conversion_factors={("GLUCOSE", "mmol/L", "mg/dL"): Decimal("0")},
            rule_version="test-1",
        )


# --- comparability --------------------------------------------------------

def test_comparability_reports_each_changed_attribute() -> None:
    first = observation()
    assert compare_adjacent_observations(first, observation()) == ()
    changed = observation(
        source_lab="Lab B", method_note="enzymatic", original_unit="mmol/L",
        original_value="5.5", reference_interval="3.9-5.5",
    )
    assert compare_adjacent_observations(first, changed) == (
        "source_lab", "method_note", "unit_basis", "reference_interval",
    )


def test_comparability_requires_same_analyte() -> None:
    with pytest.raises(ValueError):
        compare_adjacent_observations(observation(), observation(analyte_code="LDL_C"))


# --- share selection ------------------------------------------------------

def test_grant_filter_uses_analyte_scope_inclusive_dates_and_lifecycle() -> None:
    g = grant()
    on_start = observation(collection_datetime=date(2026, 1, 1))
    on_end = observation(collection_datetime=datetime(2026, 6, 30, 23, 59))
    before = observation(collection_datetime=date(2025, 12, 31))
    other_analyte = observation(analyte_code="LDL_C")
    retracted = observation(lifecycle_status="RETRACTED_BY_PATIENT")
    assert filter_observations_for_grant(
        [on_start, before, other_analyte, retracted, on_end], g
    ) == [on_start, on_end]


def test_notes_need_every_analyte_in_scope() -> None:
    g = grant()
    ok = note(date(2026, 2, 1), ["GLUCOSE"])
    mixed = note(date(2026, 2, 1), ["GLUCOSE", "LDL_C"])
    late = note(date(2026, 7, 1), ["GLUCOSE"])
    assert filter_context_notes_for_grant([ok, mixed, late], g) == [ok]


def test_selection_withholds_pages_missing_a_region() -> None:
    report = uuid4()
    boxed = observation(report_id=report, page_number=1)
    unboxed = observation(report_id=report, page_number=2, bounding_box=None)
    selection = prepare_share_content_selection(
        observations=[boxed, unboxed], notes=[], grant=grant(), as_of=NOW
    )
    dispositions = {p.page_number: p.disposition for p in selection.source_pages}
    assert dispositions == {1: "REDACTION_REQUIRED", 2: "WITHHOLD_REGION_UNAVAILABLE"}


@pytest.mark.parametrize(
    ("grant_overrides", "as_of"),
    [
        ({"local_status": "ACTIVE"}, NOW),
        ({}, NOW + timedelta(days=15)),
        ({}, NOW - timedelta(seconds=1)),
        ({}, NOW.replace(tzinfo=None)),
    ],
)
def test_selection_refuses_invalid_grant_state_or_time(grant_overrides, as_of) -> None:
    with pytest.raises(ValueError):
        prepare_share_content_selection(
            observations=[], notes=[], grant=grant(**grant_overrides), as_of=as_of
        )


def test_projection_rejects_non_adjacent_comparability_pairs() -> None:
    a, b, c = (observation(collection_datetime=date(2026, m, 1)) for m in (1, 2, 3))
    selection = prepare_share_content_selection(
        observations=[a, b, c], notes=[], grant=grant(), as_of=NOW
    )
    projection = build_share_payload_projection(
        selection, comparability_predecessors={b.observation_id: a.observation_id}
    )
    statuses = [o.comparability_status for o in projection.observations]
    assert statuses == ["NOT_EVALUATED", "COMPARED", "NOT_EVALUATED"]
    with pytest.raises(ValueError):
        build_share_payload_projection(
            selection, comparability_predecessors={c.observation_id: a.observation_id}
        )


# --- chronology -----------------------------------------------------------

def test_day_grouping_keeps_time_precisions_separate() -> None:
    day = date(2026, 3, 1)
    date_only = observation(collection_datetime=day)
    local_late = observation(collection_datetime=datetime(2026, 3, 1, 15, 0))
    local_early = observation(collection_datetime=datetime(2026, 3, 1, 8, 0))
    utc_late = observation(collection_datetime=datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc))
    est_early = observation(
        collection_datetime=datetime(2026, 3, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    )
    next_day = observation(collection_datetime=date(2026, 3, 2))

    groups = group_observations_by_collection_day(
        [next_day, local_late, utc_late, date_only, est_early, local_early]
    )
    assert [g.collection_date for g in groups] == [day, date(2026, 3, 2)]
    first = groups[0]
    assert first.date_only == (date_only,)
    assert first.local_datetimes == (local_early, local_late)
    assert first.offset_datetimes == (est_early, utc_late)


# --- duplicates -----------------------------------------------------------

def test_duplicate_reports_grouped_by_hash_not_removed() -> None:
    def report(digest: str) -> SourceReport:
        return SourceReport(uuid4(), digest, "application/pdf", 1, b"x", NOW)

    a, b, c = report("a" * 64), report("a" * 64), report("b" * 64)
    assert find_duplicate_report_groups([a, b, c]) == [(a, b)]


def test_duplicate_observations_match_analyte_time_and_lab() -> None:
    a = observation()
    b = observation(original_value="99")
    other_lab = observation(source_lab="Lab B")
    assert find_duplicate_observation_groups([a, other_lab, b]) == [(a, b)]


# --- legacy manual scenario ----------------------------------------------

def test_manual_model_check_scenario_passes(capsys) -> None:
    manual_model_check.main()
    assert "Manual model and algorithm check passed." in capsys.readouterr().out
