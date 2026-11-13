"""Encrypted share delivery, recipient access, and revocation (report section 7.5).

Patient side (`send_share`): the share bundle is encrypted with AES-256-GCM under
a fresh random Grant_Key, with canonical AAD {"grant_id", "schema_version"}.
Grant_Key is split 2-of-2 by XOR into a random Server_Share, registered with the
grant service, and a Recipient_Share that travels only in the link. The
clinician also needs a 6-digit code delivered separately (FR-22).

Grant service (`ShareServer`): a file-backed stand-in for the blind grant store.
It holds ciphertext and Server_Share but never the Recipient_Share, so it cannot
decrypt anything. It enforces expiry, a five-attempt lockout, and revocation by
destroying Server_Share, and keeps a hash-chained access log (FR-15, FR-16).

Recipient side (`open_share`): fetches Server_Share with the code, rebuilds
Grant_Key, and decrypts; any mismatch fails closed with no plaintext.

Demo deviations from the design: the Recipient_Share is carried in a query
parameter rather than the URL fragment (Streamlit cannot read fragments), the
"server" is a local folder, and Python cannot reliably zero key material.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from models import AuditEvent, ClientShareGrant, GrantRecord


SCHEMA_VERSION = "labtrail.sharebundle.v1"
MAX_FAILED_ATTEMPTS = 5
DEFAULT_SERVER_DIR = Path(__file__).with_name(".labtrail_server")


class AccessDenied(Exception):
    """The grant service refused to release its key share."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShareDecryptionError(Exception):
    """The bundle could not be authenticated; no plaintext was released."""


@dataclass(frozen=True)
class SentShare:
    grant_id: UUID
    recipient_share: bytes
    access_code: str


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_aad(grant_id: UUID, schema_version: str = SCHEMA_VERSION) -> bytes:
    """Public, non-PHI associated data binding the ciphertext to its grant."""
    return json.dumps(
        {"grant_id": str(grant_id), "schema_version": schema_version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _xor(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise ValueError("Key shares must be the same length.")
    return bytes(x ^ y for x, y in zip(a, b))


def _hash_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(code.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _identity_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- grant service

class ShareServer:
    """File-backed stand-in for the blind grant service (one JSON file per grant)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or os.environ.get("LABTRAIL_SERVER_DIR") or DEFAULT_SERVER_DIR)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- storage
    def _path(self, grant_id: UUID) -> Path:
        return self.root / f"{UUID(str(grant_id))}.json"

    def _load(self, grant_id: UUID) -> dict:
        path = self._path(grant_id)
        if not path.exists():
            raise AccessDenied("NOT_FOUND")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, entry: dict) -> None:
        self._validate(entry)
        path = self._path(UUID(entry["grant_id"]))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _validate(entry: dict) -> GrantRecord:
        """Every stored state must satisfy the GrantRecord invariants."""
        return GrantRecord(
            grant_id=UUID(entry["grant_id"]),
            recipient_identity_hash=entry["recipient_identity_hash"],
            second_factor_verifier_hash=entry["second_factor_verifier"],
            failed_attempts=entry["failed_attempts"],
            schema_version=entry["schema_version"],
            server_share=_unb64(entry["server_share"]) if entry["server_share"] else None,
            iv_nonce=_unb64(entry["iv"]),
            bundle_blob_uri=f"grant:{entry['grant_id']}",
            status=entry["status"],
            created_at=datetime.fromisoformat(entry["created_at"]),
            expires_at=datetime.fromisoformat(entry["expires_at"]),
            revoked_at=datetime.fromisoformat(entry["revoked_at"]) if entry["revoked_at"] else None,
            lease_expires_at=None,
        )

    # -- audit chain (FR-16)
    @staticmethod
    def _append_event(entry: dict, event_type: str, actor_role: str, now: datetime) -> None:
        previous = entry["events"][-1]["event_hash"] if entry["events"] else None
        body = {
            "event_type": event_type,
            "actor_role": actor_role,
            "target_id": entry["grant_id"],
            "occurred_at": now.isoformat(),
            "prev_event_hash": previous,
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        AuditEvent(
            event_id=UUID(int=len(entry["events"]) + 1),
            event_type=event_type,
            actor_role=actor_role,
            actor_identifier=actor_role,
            target_id=UUID(entry["grant_id"]),
            details_ciphertext=None,
            details_nonce=None,
            prev_event_hash=previous,
            event_hash=digest,
            occurred_at=now,
        )
        entry["events"].append({**body, "event_hash": digest})

    @staticmethod
    def verify_event_chain(events: list[dict]) -> bool:
        previous = None
        for event in events:
            body = {k: event[k] for k in ("event_type", "actor_role", "target_id", "occurred_at", "prev_event_hash")}
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if event["prev_event_hash"] != previous or event["event_hash"] != digest:
                return False
            previous = digest
        return True

    # -- state changes
    @staticmethod
    def _destroy_share(entry: dict, status: str, now: datetime) -> None:
        entry["server_share"] = None
        entry["status"] = status
        if status == "REVOKED":
            entry["revoked_at"] = now.isoformat()

    def register(
        self,
        *,
        grant_id: UUID,
        server_share: bytes,
        iv: bytes,
        bundle_ciphertext: bytes,
        recipient_identity_hash: str,
        access_code: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        if self._path(grant_id).exists():
            raise ValueError("A grant with this id is already registered.")
        salt = os.urandom(16)
        entry = {
            "grant_id": str(grant_id),
            "schema_version": SCHEMA_VERSION,
            "server_share": _b64(server_share),
            "iv": _b64(iv),
            "bundle_ciphertext": _b64(bundle_ciphertext),
            "recipient_identity_hash": recipient_identity_hash,
            "second_factor_verifier": f"scrypt${_b64(salt)}${_b64(_hash_code(access_code, salt))}",
            "failed_attempts": 0,
            "status": "ACTIVE",
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "revoked_at": None,
            "events": [],
        }
        self._append_event(entry, "GRANT_REGISTERED", "PATIENT", created_at)
        self._save(entry)

    def status(self, grant_id: UUID, now: datetime) -> str:
        entry = self._load(grant_id)
        if entry["status"] == "ACTIVE" and now >= datetime.fromisoformat(entry["expires_at"]):
            self._destroy_share(entry, "EXPIRED", now)
            self._append_event(entry, "GRANT_EXPIRED", "SYSTEM", now)
            self._save(entry)
        return entry["status"]

    def events(self, grant_id: UUID) -> list[dict]:
        return list(self._load(grant_id)["events"])

    def revoke(self, grant_id: UUID, now: datetime) -> None:
        entry = self._load(grant_id)
        if entry["status"] != "ACTIVE":
            return
        self._destroy_share(entry, "REVOKED", now)
        self._append_event(entry, "GRANT_REVOKED", "PATIENT", now)
        self._save(entry)

    def retrieve(
        self, grant_id: UUID, *, recipient_email: str, access_code: str, now: datetime
    ) -> tuple[bytes, bytes, bytes, str]:
        """Release (server_share, iv, ciphertext, schema_version) or raise AccessDenied."""
        status = self.status(grant_id, now)
        entry = self._load(grant_id)
        if status != "ACTIVE":
            self._append_event(entry, "ACCESS_DENIED", "RECIPIENT", now)
            self._save(entry)
            raise AccessDenied(status)

        _, salt, expected = entry["second_factor_verifier"].split("$")
        code_ok = hmac.compare_digest(_hash_code(access_code.strip(), _unb64(salt)), _unb64(expected))
        identity_ok = hmac.compare_digest(
            _identity_hash(recipient_email), entry["recipient_identity_hash"]
        )
        if not (code_ok and identity_ok):
            entry["failed_attempts"] += 1
            self._append_event(entry, "ACCESS_DENIED", "RECIPIENT", now)
            if entry["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
                self._destroy_share(entry, "LOCKED", now)
                self._append_event(entry, "GRANT_LOCKED", "SYSTEM", now)
                self._save(entry)
                raise AccessDenied("LOCKED")
            self._save(entry)
            raise AccessDenied("WRONG_CREDENTIALS")

        self._append_event(entry, "ACCESS_GRANTED", "RECIPIENT", now)
        self._save(entry)
        return (
            _unb64(entry["server_share"]),
            _unb64(entry["iv"]),
            _unb64(entry["bundle_ciphertext"]),
            entry["schema_version"],
        )


# ---------------------------------------------------------------- patient side

def send_share(
    *,
    grant: ClientShareGrant,
    recipient_email: str,
    share_payload: bytes,
    redacted_pages: list[dict],
    withheld_pages: list[dict],
    server: ShareServer,
    now: datetime,
    analyte_names: dict[str, str] | None = None,
) -> SentShare:
    """Encrypt the bundle on the client, split the key, and register with the server.

    redacted_pages: [{"label": str, "png": bytes}]; withheld_pages: [{"label", "reason"}].
    analyte_names maps each shared code to its display name, so the recipient sees
    names for tests the patient added, which no reference table contains.
    """
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "recipient_name": grant.recipient_name,
        "expires_at": grant.expires_at.isoformat(),
        "sent_at": now.isoformat(),
        "payload": json.loads(share_payload),
        "pages": [{"label": p["label"], "png": _b64(p["png"])} for p in redacted_pages],
        "withheld_pages": withheld_pages,
        "analyte_names": dict(analyte_names or {}),
    }
    plaintext = zlib.compress(json.dumps(bundle, separators=(",", ":")).encode("utf-8"))

    grant_key = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    ciphertext = AESGCM(grant_key).encrypt(iv, plaintext, canonical_aad(grant.grant_id))
    server_share = os.urandom(32)
    recipient_share = _xor(grant_key, server_share)
    access_code = f"{secrets.randbelow(10**6):06d}"

    server.register(
        grant_id=grant.grant_id,
        server_share=server_share,
        iv=iv,
        bundle_ciphertext=ciphertext,
        recipient_identity_hash=_identity_hash(recipient_email),
        access_code=access_code,
        created_at=now,
        expires_at=grant.expires_at,
    )
    del grant_key, plaintext  # best effort; Python cannot guarantee zeroization
    return SentShare(grant.grant_id, recipient_share, access_code)


# ---------------------------------------------------------------- recipient side

def open_share(
    *,
    grant_id: UUID,
    recipient_share: bytes,
    recipient_email: str,
    access_code: str,
    server: ShareServer,
    now: datetime,
) -> dict:
    """Retrieve the server share with the code, rebuild the key, and decrypt.

    Raises AccessDenied if the server refuses, ShareDecryptionError if anything
    (link share, ciphertext, nonce, grant id, schema version) was altered.
    """
    server_share, iv, ciphertext, schema_version = server.retrieve(
        grant_id, recipient_email=recipient_email, access_code=access_code, now=now
    )
    try:
        grant_key = _xor(server_share, recipient_share)
        plaintext = AESGCM(grant_key).decrypt(
            iv, ciphertext, canonical_aad(grant_id, schema_version)
        )
    except (InvalidTag, ValueError) as error:
        raise ShareDecryptionError("The share could not be verified.") from error
    bundle = json.loads(zlib.decompress(plaintext))
    for page in bundle["pages"]:
        page["png"] = _unb64(page["png"])
    return bundle
