import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from labtrail_algorithms import (
    build_share_payload_projection,
    prepare_share_content_selection,
    serialize_share_payload_projection,
)
from models import AcceptedObservation, ClientShareGrant, ContextNote


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "share_payload_schema_v1.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


@pytest.fixture
def valid_payload() -> dict[str, Any]:
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    grant = ClientShareGrant(
        grant_id=UUID(int=1),
        recipient_name="Synthetic Clinician",
        recipient_identity_hash="synthetic-recipient-hash",
        granted_analytes=["GLUCOSE"],
        range_start=date(2026, 1, 1),
        range_end=date(2026, 11, 1),
        created_at=now,
        expires_at=now + timedelta(days=365),
        local_status="PREPARING_CLIENT",
    )
    observation = AcceptedObservation(
        observation_id=UUID(int=2),
        candidate_id=UUID(int=3),
        report_id=UUID(int=4),
        analyte_code="GLUCOSE",
        canonical_value=Decimal("98.6"),
        canonical_unit="mg/dL",
        original_value="98.6",
        original_unit="mg/dL",
        conversion_factor=Decimal("1"),
        conversion_outcome="CANONICAL_DIRECT",
        unconvertible_reason=None,
        reference_interval="70-99 mg/dL",
        method_note="Synthetic method",
        source_lab="Synthetic Lab",
        collection_datetime=date(2026, 11, 1),
        page_number=1,
        bounding_box=(0.1, 0.2, 0.4, 0.3),
        provenance_flag="AS_EXTRACTED",
        lifecycle_status="ACCEPTED",
        rule_version="synthetic-rules-v1",
        accepted_at=now,
    )
    note = ContextNote(
        note_id=UUID(int=5),
        category="FREE_TEXT",
        note_date=date(2026, 11, 1),
        body="Synthetic context note",
        analyte_codes=["GLUCOSE"],
        created_at=now,
        updated_at=now,
    )
    selection = prepare_share_content_selection(
        observations=(observation,),
        notes=(note,),
        grant=grant,
        as_of=now,
    )
    payload = build_share_payload_projection(selection)
    return json.loads(serialize_share_payload_projection(payload))


def test_schema_is_valid_and_generated_payload_conforms(
    valid_payload: dict[str, Any],
) -> None:
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(valid_payload)


def test_schema_rejects_non_accepted_review_status(
    valid_payload: dict[str, Any],
) -> None:
    valid_payload["observations"][0]["review_status"] = "PENDING_REVIEW"

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_payload)


def test_schema_accepts_local_wall_time_collection(
    valid_payload: dict[str, Any],
) -> None:
    valid_payload["observations"][0]["collection_datetime"] = (
        "2026-11-01T10:30:00"
    )

    VALIDATOR.validate(valid_payload)


def test_schema_rejects_comparison_without_predecessor(
    valid_payload: dict[str, Any],
) -> None:
    observation = valid_payload["observations"][0]
    observation["comparability_status"] = "COMPARED"

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_payload)


def test_schema_rejects_out_of_bounds_source_region(
    valid_payload: dict[str, Any],
) -> None:
    valid_payload["observations"][0]["source_bounding_box"] = [
        0.1,
        0.2,
        1.1,
        0.3,
    ]

    with pytest.raises(ValidationError):
        VALIDATOR.validate(valid_payload)
