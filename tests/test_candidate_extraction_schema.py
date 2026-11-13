import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from models import (
    CandidateExtraction,
    CandidateExtractionResult,
    ExtractionPageInventory,
)


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "candidate_extraction_result_schema_v1.json"
)
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


@pytest.fixture
def valid_result_payload() -> dict[str, Any]:
    report_id = UUID(int=1)
    job_id = UUID(int=2)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    candidate = CandidateExtraction(
        candidate_id=UUID(int=3),
        report_id=report_id,
        extraction_job_id=job_id,
        row_index=0,
        raw_analyte_name="Glucose",
        proposed_analyte_code="GLUCOSE",
        raw_value="98.6",
        raw_unit="mg/dL",
        reference_interval="70-99 mg/dL",
        method_note=None,
        specimen_type="Serum",
        abnormal_flag=None,
        page_number=1,
        bounding_box=(0.1, 0.2, 0.4, 0.3),
        region_available=True,
        field_confidences={
            "analyte": 0.99,
            "value": 0.98,
            "unit": 0.97,
            "region": 0.96,
        },
        model_identifier="synthetic-model",
        model_version="1",
        prompt_hash="a" * 64,
        extracted_at=now,
        routing_status="STANDARD_REVIEW",
        lifecycle_status="CANDIDATE",
    )
    result = CandidateExtractionResult(
        report_id=report_id,
        extraction_job_id=job_id,
        candidates=(candidate,),
        page_inventories=(
            ExtractionPageInventory(
                page_number=1,
                source_page_sha256="b" * 64,
                result_row_indices=(0,),
                inventory_status="COMPLETE",
            ),
        ),
    )
    return _json_value(asdict(result))


def test_schema_is_valid_and_model_result_conforms(
    valid_result_payload: dict[str, Any],
) -> None:
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(valid_result_payload)


def test_schema_rejects_candidate_without_required_confidence(
    valid_result_payload: dict[str, Any],
) -> None:
    del valid_result_payload["candidates"][0]["field_confidences"]["region"]

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_result_payload)


def test_schema_rejects_mismatched_region_availability(
    valid_result_payload: dict[str, Any],
) -> None:
    valid_result_payload["candidates"][0]["region_available"] = False

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_result_payload)


def test_schema_rejects_unknown_fields(valid_result_payload: dict[str, Any]) -> None:
    valid_result_payload["unreviewed_text"] = "unexpected"

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_result_payload)
