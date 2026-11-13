import base64
import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from labtrail_sharing import (
    MAX_FAILED_ATTEMPTS,
    AccessDenied,
    ShareDecryptionError,
    ShareServer,
    open_share,
    send_share,
)
from models import ClientShareGrant


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
EMAIL = "Clinician@Example.org"
PAYLOAD = json.dumps({"schema_version": "1", "observations": [{"analyte_code": "GLUCOSE", "original_value": "104"}]}).encode()
PNG = b"\x89PNG\r\n\x1a\nfake-page"


@pytest.fixture
def server(tmp_path) -> ShareServer:
    return ShareServer(tmp_path)


def _send(server: ShareServer, *, days: int = 14):
    grant = ClientShareGrant(
        grant_id=uuid4(), recipient_name="Dr. Test", recipient_identity_hash="h",
        granted_analytes=["GLUCOSE"], range_start=date(2026, 1, 1), range_end=date(2026, 12, 31),
        created_at=NOW, expires_at=NOW + timedelta(days=days), local_status="PREPARING_CLIENT",
    )
    sent = send_share(
        grant=grant, recipient_email=EMAIL, share_payload=PAYLOAD,
        redacted_pages=[{"label": "report.pdf p.1", "png": PNG}],
        withheld_pages=[{"label": "other.pdf p.1", "reason": "not confirmed"}],
        server=server, now=NOW,
    )
    return grant, sent


def _open(server, sent, *, code=None, email=EMAIL, share=None, now=NOW):
    return open_share(
        grant_id=sent.grant_id, recipient_share=share or sent.recipient_share,
        recipient_email=email, access_code=code or sent.access_code, server=server, now=now,
    )


def test_recipient_with_link_code_and_email_can_open(server) -> None:
    _, sent = _send(server)
    bundle = _open(server, sent, email="  clinician@example.ORG ")
    assert bundle["payload"] == json.loads(PAYLOAD)
    assert bundle["pages"][0]["png"] == PNG
    assert bundle["withheld_pages"][0]["reason"] == "not confirmed"
    assert len(sent.access_code) == 6 and sent.access_code.isdigit()


def test_server_cannot_read_the_share(server) -> None:
    _, sent = _send(server)
    entry = json.loads((server.root / f"{sent.grant_id}.json").read_text())
    ciphertext = base64.b64decode(entry["bundle_ciphertext"])
    # Random fields (keys, nonce, hashes) can contain any digits by chance, so the
    # readable-content check covers every other stored field.
    random_fields = {"bundle_ciphertext", "iv", "server_share", "second_factor_verifier",
                     "recipient_identity_hash", "events", "grant_id"}
    readable = json.dumps({k: v for k, v in entry.items() if k not in random_fields})
    readable += json.dumps([{k: v for k, v in e.items() if not k.endswith("hash")} for e in entry["events"]])
    for secret in ("GLUCOSE", "104", "Dr. Test", "clinician", sent.access_code):
        assert secret not in readable
    for secret in ("GLUCOSE", "Dr. Test", "clinician"):
        assert secret.encode() not in ciphertext
    assert set(entry) - random_fields == {
        "schema_version", "failed_attempts", "status", "created_at", "expires_at", "revoked_at",
    }
    # The server's half alone is not the key: decrypting with it fails closed.
    with pytest.raises(ShareDecryptionError):
        _open(server, sent, share=bytes(32))


@pytest.mark.parametrize("field", ["recipient_share", "ciphertext", "iv", "grant_id"])
def test_any_tampering_fails_closed(server, field) -> None:
    _, sent = _send(server)
    path = server.root / f"{sent.grant_id}.json"
    entry = json.loads(path.read_text())
    share = sent.recipient_share
    if field == "recipient_share":
        share = bytes([share[0] ^ 1]) + share[1:]
    elif field == "ciphertext":
        raw = bytearray(base64.b64decode(entry["bundle_ciphertext"]))
        raw[-1] ^= 1
        entry["bundle_ciphertext"] = base64.b64encode(bytes(raw)).decode()
    elif field == "iv":
        raw = bytearray(base64.b64decode(entry["iv"]))
        raw[0] ^= 1
        entry["iv"] = base64.b64encode(bytes(raw)).decode()
    elif field == "grant_id":
        # Move the ciphertext under a different grant id: AAD no longer matches.
        _, other = _send(server)
        other_path = server.root / f"{other.grant_id}.json"
        other_entry = json.loads(other_path.read_text())
        other_entry.update({k: entry[k] for k in ("bundle_ciphertext", "iv", "server_share")})
        other_path.write_text(json.dumps(other_entry))
        with pytest.raises(ShareDecryptionError):
            _open(server, other, share=sent.recipient_share)
        return
    path.write_text(json.dumps(entry))
    with pytest.raises(ShareDecryptionError):
        _open(server, sent, share=share)


def test_wrong_code_or_email_is_denied_and_locks_after_five(server) -> None:
    _, sent = _send(server)
    wrong = f"{(int(sent.access_code) + 1) % 10**6:06d}"
    with pytest.raises(AccessDenied, match="WRONG_CREDENTIALS"):
        _open(server, sent, email="someone@else.org")
    for _ in range(MAX_FAILED_ATTEMPTS - 2):
        with pytest.raises(AccessDenied, match="WRONG_CREDENTIALS"):
            _open(server, sent, code=wrong)
    with pytest.raises(AccessDenied, match="LOCKED"):
        _open(server, sent, code=wrong)
    with pytest.raises(AccessDenied, match="LOCKED"):
        _open(server, sent)  # even the right code no longer works
    entry = json.loads((server.root / f"{sent.grant_id}.json").read_text())
    assert entry["server_share"] is None


def test_revocation_destroys_server_share_and_blocks_access(server) -> None:
    _, sent = _send(server)
    _open(server, sent)
    server.revoke(sent.grant_id, NOW + timedelta(minutes=5))
    with pytest.raises(AccessDenied, match="REVOKED"):
        _open(server, sent, now=NOW + timedelta(minutes=5, seconds=1))
    entry = json.loads((server.root / f"{sent.grant_id}.json").read_text())
    assert entry["server_share"] is None and entry["status"] == "REVOKED"


def test_expired_share_is_denied_and_key_destroyed(server) -> None:
    _, sent = _send(server, days=1)
    with pytest.raises(AccessDenied, match="EXPIRED"):
        _open(server, sent, now=NOW + timedelta(days=1))
    assert server.status(sent.grant_id, NOW + timedelta(days=2)) == "EXPIRED"


def test_access_log_records_every_attempt_in_a_verifiable_chain(server) -> None:
    _, sent = _send(server)
    _open(server, sent)
    with pytest.raises(AccessDenied):
        _open(server, sent, code="000000" if sent.access_code != "000000" else "111111")
    server.revoke(sent.grant_id, NOW)
    with pytest.raises(AccessDenied):
        _open(server, sent)
    events = server.events(sent.grant_id)
    assert [e["event_type"] for e in events] == [
        "GRANT_REGISTERED", "ACCESS_GRANTED", "ACCESS_DENIED", "GRANT_REVOKED", "ACCESS_DENIED",
    ]
    assert ShareServer.verify_event_chain(events)
    events[1]["event_type"] = "ACCESS_DENIED"
    assert not ShareServer.verify_event_chain(events)


def test_unknown_grant_is_denied(server) -> None:
    _, sent = _send(server)
    with pytest.raises(AccessDenied, match="NOT_FOUND"):
        open_share(grant_id=uuid4(), recipient_share=sent.recipient_share, recipient_email=EMAIL,
                   access_code=sent.access_code, server=server, now=NOW)
