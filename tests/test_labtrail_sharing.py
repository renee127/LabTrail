import base64
import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from labtrail_sharing import (
    MAX_FAILED_ATTEMPTS,
    _normalize_database_url,
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


_BACKENDS = ["folder", "sqlite"] + (["postgres"] if os.environ.get("LABTRAIL_TEST_DATABASE_URL") else [])


@pytest.fixture(params=_BACKENDS)
def server(request, tmp_path) -> ShareServer:
    """Every sharing test runs against each storage backend."""
    if request.param == "folder":
        return ShareServer(tmp_path)
    if request.param == "sqlite":
        return ShareServer(database_url=f"sqlite:///{tmp_path / 'grants.db'}")
    server = ShareServer(database_url=os.environ["LABTRAIL_TEST_DATABASE_URL"])
    with server.store._engine.begin() as conn:  # isolate tests sharing one database
        conn.execute(server.store._table.delete())
    return server


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
        server=server, now=NOW, analyte_names={"GLUCOSE": "Glucose"},
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
    assert bundle["analyte_names"] == {"GLUCOSE": "Glucose"}
    assert bundle["payload"] == json.loads(PAYLOAD)
    assert bundle["pages"][0]["png"] == PNG
    assert bundle["withheld_pages"][0]["reason"] == "not confirmed"
    assert len(sent.access_code) == 6 and sent.access_code.isdigit()


def test_server_cannot_read_the_share(server) -> None:
    _, sent = _send(server)
    entry = server.store.read(str(sent.grant_id))
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
    entry = server.store.read(str(sent.grant_id))
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
        other_entry = server.store.read(str(other.grant_id))
        other_entry.update({k: entry[k] for k in ("bundle_ciphertext", "iv", "server_share")})
        server.store.write(other_entry)
        with pytest.raises(ShareDecryptionError):
            _open(server, other, share=sent.recipient_share)
        return
    server.store.write(entry)
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
    entry = server.store.read(str(sent.grant_id))
    assert entry["server_share"] is None


def test_revocation_destroys_server_share_and_blocks_access(server) -> None:
    _, sent = _send(server)
    _open(server, sent)
    server.revoke(sent.grant_id, NOW + timedelta(minutes=5))
    with pytest.raises(AccessDenied, match="REVOKED"):
        _open(server, sent, now=NOW + timedelta(minutes=5, seconds=1))
    entry = server.store.read(str(sent.grant_id))
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


def test_concurrent_wrong_codes_are_all_counted_and_lock_once(server) -> None:
    """Visitors are served in parallel threads; no failed attempt may be lost."""
    _, sent = _send(server)
    wrong = f"{(int(sent.access_code) + 1) % 10**6:06d}"
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            _open(server, sent, code=wrong)
        except AccessDenied as denied:
            outcomes.append(denied.reason)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entry = server.store.read(str(sent.grant_id))
    assert entry["failed_attempts"] == MAX_FAILED_ATTEMPTS
    assert entry["status"] == "LOCKED" and entry["server_share"] is None
    assert outcomes.count("LOCKED") == 8 - (MAX_FAILED_ATTEMPTS - 1)
    assert [e["event_type"] for e in entry["events"]].count("GRANT_LOCKED") == 1
    assert ShareServer.verify_event_chain(entry["events"])


def test_sweep_destroys_keys_of_expired_shares_nobody_opened(server) -> None:
    _, short = _send(server, days=1)
    _, long = _send(server, days=30)
    assert server.sweep_expired(NOW + timedelta(hours=12)) == 0
    assert server.sweep_expired(NOW + timedelta(days=2)) == 1
    expired = server.store.read(str(short.grant_id))
    assert expired["status"] == "EXPIRED" and expired["server_share"] is None
    assert server.store.read(str(long.grant_id))["status"] == "ACTIVE"
    assert server.sweep_expired(NOW + timedelta(days=3)) == 0  # already handled


def test_shares_survive_a_restart_with_a_database(tmp_path) -> None:
    """A new ShareServer on the same database (an app restart) still opens the share."""
    url = f"sqlite:///{tmp_path / 'grants.db'}"
    _, sent = _send(ShareServer(database_url=url))
    restarted = ShareServer(database_url=url)
    assert restarted.persistent
    assert _open(restarted, sent)["payload"] == json.loads(PAYLOAD)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("sqlite:///grants.db", "sqlite:///grants.db"),
    ],
)
def test_provider_database_urls_are_normalized(given, expected) -> None:
    assert _normalize_database_url(given) == expected
