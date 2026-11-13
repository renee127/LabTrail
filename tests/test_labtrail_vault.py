import base64
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from labtrail_extraction import extract_document_ocr_line_candidates
from labtrail_redaction import rasterize_pdf_page
from labtrail_vault import VaultError, decrypt_record, encrypt_record
from labtrail_workflow import (
    accept_candidate,
    is_result_like,
    suggest_analyte_code,
    suggest_report_metadata,
)
from models import ContextNote


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "st_brigid_hospital_2026-05-18.pdf"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
FAST_KDF = {"name": "argon2id", "memory_cost_kib": 8192, "iterations": 1, "lanes": 1}
PASSPHRASE = "correct horse battery"


@pytest.fixture(scope="module")
def record() -> dict:
    pdf = SAMPLE.read_bytes()
    report_id = uuid4()
    result = extract_document_ocr_line_candidates(
        source_bytes=pdf, mime_type="application/pdf", report_id=report_id,
        extraction_job_id=uuid4(), extracted_at=NOW,
    )
    observations = []
    for c in result.candidates:
        if is_result_like(c):
            observations.append(accept_candidate(
                c, analyte_code=suggest_analyte_code(c.raw_analyte_name), value=c.raw_value,
                unit=c.raw_unit, reference_interval=c.reference_interval,
                source_lab="St. Brigid Hospital Laboratory", collected=date(2026, 5, 18),
                accepted_at=NOW,
            ))
            c.lifecycle_status = "ACCEPTED"
    note = ContextNote(uuid4(), "MEDICATION_CHANGE", date(2026, 4, 1), "Started med", ["GLUCOSE"], NOW, NOW)
    return {
        "reports": {
            report_id: {
                "filename": SAMPLE.name,
                "sha256": "a" * 64,
                "result": result,
                "pages": {1: rasterize_pdf_page(pdf, 1)},
                "suggestion": suggest_report_metadata(result),
                "confirmed": {"lab": "St. Brigid Hospital Laboratory", "collected": date(2026, 5, 18)},
            }
        },
        "observations": observations,
        "notes": [note],
        "activity": [("12:00:00", "Uploaded")],
        "confirmed_identities": {("St. Brigid Hospital Laboratory", "Jordan Sample")},
    }


def test_round_trip_restores_every_record_exactly(record) -> None:
    restored = decrypt_record(encrypt_record(record, PASSPHRASE, kdf=FAST_KDF), PASSPHRASE)
    assert restored == record
    glucose = next(o for o in restored["observations"] if o.analyte_code == "GLUCOSE")
    assert isinstance(glucose.canonical_value, Decimal)
    assert glucose.canonical_value == Decimal("6.3") * Decimal("18.016")


def test_file_contains_no_readable_record_content(record) -> None:
    data = encrypt_record(record, PASSPHRASE, kdf=FAST_KDF)
    envelope = json.loads(data)
    blob = data + base64.b64decode(envelope["ciphertext"])
    for secret in (b"Jordan", b"Glucose", b"mmol", b"St. Brigid", b"Started med", b"\x89PNG"):
        assert secret not in blob


def test_wrong_passphrase_is_rejected(record) -> None:
    data = encrypt_record(record, PASSPHRASE, kdf=FAST_KDF)
    with pytest.raises(VaultError, match="Wrong passphrase"):
        decrypt_record(data, "incorrect horse battery")


def test_any_ciphertext_change_is_detected(record) -> None:
    envelope = json.loads(encrypt_record(record, PASSPHRASE, kdf=FAST_KDF))
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[len(raw) // 2] ^= 1
    envelope["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(VaultError, match="altered"):
        decrypt_record(json.dumps(envelope).encode(), PASSPHRASE)


def test_header_is_bound_to_the_ciphertext(record) -> None:
    envelope = json.loads(encrypt_record(record, PASSPHRASE, kdf={**FAST_KDF, "iterations": 2}))
    envelope["header"]["kdf"]["iterations"] = 1  # still above the floor, but not what was used
    with pytest.raises(VaultError):
        decrypt_record(json.dumps(envelope).encode(), PASSPHRASE)


def test_weak_kdf_settings_are_refused_before_decrypting(record) -> None:
    envelope = json.loads(encrypt_record(record, PASSPHRASE, kdf=FAST_KDF))
    envelope["header"]["kdf"]["memory_cost_kib"] = 64
    with pytest.raises(VaultError, match="not allowed"):
        decrypt_record(json.dumps(envelope).encode(), PASSPHRASE)


@pytest.mark.parametrize("data", [b"", b"not json", b'{"header": {}}', b"[]"])
def test_non_vault_files_are_rejected(data) -> None:
    with pytest.raises(VaultError):
        decrypt_record(data, PASSPHRASE)


def test_short_passphrase_and_unknown_types_are_refused(record) -> None:
    with pytest.raises(ValueError, match="at least"):
        encrypt_record(record, "short", kdf=FAST_KDF)
    with pytest.raises(TypeError):
        encrypt_record({"x": object()}, PASSPHRASE, kdf=FAST_KDF)


def test_each_save_uses_a_fresh_salt_and_nonce(record) -> None:
    a = json.loads(encrypt_record(record, PASSPHRASE, kdf=FAST_KDF))
    b = json.loads(encrypt_record(record, PASSPHRASE, kdf=FAST_KDF))
    assert a["header"]["salt"] != b["header"]["salt"]
    assert a["nonce"] != b["nonce"]
