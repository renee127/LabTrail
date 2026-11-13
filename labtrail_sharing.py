"""Encrypted share delivery, recipient access, and revocation (report section 7.5).

Patient side (`send_share`): the share bundle is encrypted with AES-256-GCM under
a fresh random Grant_Key, with canonical AAD {"grant_id", "schema_version"}.
Grant_Key is split 2-of-2 by XOR into a random Server_Share, registered with the
grant service, and a Recipient_Share that travels only in the link. The
clinician also needs a 6-digit code delivered separately (FR-22).

Grant service (`ShareServer`): a stand-in for the blind grant store, kept in a
SQL database when one is configured (LABTRAIL_DATABASE_URL) or a local folder.
It holds ciphertext and Server_Share but never the Recipient_Share, so it cannot
decrypt anything. It enforces expiry, a five-attempt lockout, and revocation by
destroying Server_Share, and keeps a hash-chained access log (FR-15, FR-16).

Recipient side (`open_share`): fetches Server_Share with the code, rebuilds
Grant_Key, and decrypts; any mismatch fails closed with no plaintext.

Demo deviations from the design: the Recipient_Share is carried in a query
parameter rather than the URL fragment (Streamlit cannot read fragments), the
"server" runs inside the app, and Python cannot reliably zero key material.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
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


# ---------------------------------------------------------------- grant storage

class FileGrantStore:
    """One JSON file per grant in a local folder (default; demo and tests).

    Streamlit serves each visitor in its own thread, so read-modify-write is
    serialized with a lock to keep failed-attempt counts and revocation exact.
    """

    _lock = threading.RLock()

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.description = f"local folder {self.root.name}/ (lost when the app restarts)"

    def _path(self, grant_id: str) -> Path:
        return self.root / f"{UUID(str(grant_id))}.json"

    def read(self, grant_id: str) -> dict | None:
        path = self._path(grant_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write(self, entry: dict) -> None:
        path = self._path(entry["grant_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def create(self, entry: dict) -> None:
        with self._lock:
            if self._path(entry["grant_id"]).exists():
                raise ValueError("A grant with this id is already registered.")
            self.write(entry)

    @contextmanager
    def update(self, grant_id: str) -> Iterator[dict]:
        with self._lock:
            entry = self.read(grant_id)
            if entry is None:
                raise AccessDenied("NOT_FOUND")
            yield entry
            self.write(entry)

    def active_grant_ids(self) -> list[str]:
        ids = []
        for path in self.root.glob("*.json"):
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry["status"] == "ACTIVE":
                ids.append(entry["grant_id"])
        return ids


class SqlGrantStore:
    """Grants in a SQL database (Postgres in production, SQLite in tests).

    Each change runs in one transaction with the row locked (SELECT ... FOR
    UPDATE on Postgres), so concurrent requests cannot lose an update.
    """

    def __init__(self, url: str) -> None:
        import sqlalchemy as sa

        self._sa = sa
        url = _normalize_database_url(url)
        connect_args = {"prepare_threshold": None} if url.startswith("postgresql+psycopg") else {}
        self._engine = sa.create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self._sqlite_lock = threading.RLock() if self._engine.dialect.name == "sqlite" else None
        metadata = sa.MetaData()
        self._table = sa.Table(
            "labtrail_grants",
            metadata,
            sa.Column("grant_id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(20), nullable=False, index=True),
            sa.Column("expires_at", sa.String(40), nullable=False),
            sa.Column("entry", sa.Text, nullable=False),
        )
        metadata.create_all(self._engine)
        self.description = f"{self._engine.dialect.name} database (persistent)"

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        if self._sqlite_lock is None:
            yield
        else:
            with self._sqlite_lock:
                yield

    def read(self, grant_id: str) -> dict | None:
        t = self._table
        with self._engine.connect() as conn:
            row = conn.execute(
                self._sa.select(t.c.entry).where(t.c.grant_id == str(grant_id))
            ).first()
        return json.loads(row[0]) if row else None

    def _values(self, entry: dict) -> dict:
        return {
            "status": entry["status"],
            "expires_at": entry["expires_at"],
            "entry": json.dumps(entry, separators=(",", ":")),
        }

    def write(self, entry: dict) -> None:
        t = self._table
        with self._serialized(), self._engine.begin() as conn:
            conn.execute(t.delete().where(t.c.grant_id == entry["grant_id"]))
            conn.execute(t.insert().values(grant_id=entry["grant_id"], **self._values(entry)))

    def create(self, entry: dict) -> None:
        try:
            with self._serialized(), self._engine.begin() as conn:
                conn.execute(self._table.insert().values(grant_id=entry["grant_id"], **self._values(entry)))
        except self._sa.exc.IntegrityError as error:
            raise ValueError("A grant with this id is already registered.") from error

    @contextmanager
    def update(self, grant_id: str) -> Iterator[dict]:
        t = self._table
        with self._serialized(), self._engine.begin() as conn:
            row = conn.execute(
                self._sa.select(t.c.entry).where(t.c.grant_id == str(grant_id)).with_for_update()
            ).first()
            if row is None:
                raise AccessDenied("NOT_FOUND")
            entry = json.loads(row[0])
            yield entry
            conn.execute(t.update().where(t.c.grant_id == str(grant_id)).values(**self._values(entry)))

    def active_grant_ids(self) -> list[str]:
        t = self._table
        with self._engine.connect() as conn:
            return [r[0] for r in conn.execute(self._sa.select(t.c.grant_id).where(t.c.status == "ACTIVE"))]


def _normalize_database_url(url: str) -> str:
    """Accept provider URLs as given (postgres://, postgresql://) and use psycopg 3."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# ---------------------------------------------------------------- grant service

class ShareServer:
    """Stand-in for the blind grant service.

    Stores ciphertext and the server key half, never the recipient half, so it
    cannot read a share. Storage is a SQL database when a URL is configured
    (argument or LABTRAIL_DATABASE_URL), otherwise a local folder.
    """

    def __init__(self, root: Path | None = None, database_url: str | None = None) -> None:
        url = database_url or os.environ.get("LABTRAIL_DATABASE_URL")
        if url:
            self.store = SqlGrantStore(url)
        else:
            self.store = FileGrantStore(
                Path(root or os.environ.get("LABTRAIL_SERVER_DIR") or DEFAULT_SERVER_DIR)
            )

    @property
    def persistent(self) -> bool:
        return isinstance(self.store, SqlGrantStore)

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

    def _expire_if_due(self, entry: dict, now: datetime) -> bool:
        if entry["status"] == "ACTIVE" and now >= datetime.fromisoformat(entry["expires_at"]):
            self._destroy_share(entry, "EXPIRED", now)
            self._append_event(entry, "GRANT_EXPIRED", "SYSTEM", now)
            return True
        return False

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
        self._validate(entry)
        self.store.create(entry)

    def status(self, grant_id: UUID, now: datetime) -> str:
        entry = self.store.read(str(grant_id))
        if entry is None:
            raise AccessDenied("NOT_FOUND")
        if entry["status"] == "ACTIVE" and now >= datetime.fromisoformat(entry["expires_at"]):
            with self.store.update(str(grant_id)) as locked:
                self._expire_if_due(locked, now)
                self._validate(locked)
                return locked["status"]
        return entry["status"]

    def sweep_expired(self, now: datetime) -> int:
        """Destroy the server key half of every share past its expiry; return how many."""
        expired = 0
        for grant_id in self.store.active_grant_ids():
            with self.store.update(grant_id) as entry:
                if self._expire_if_due(entry, now):
                    expired += 1
                self._validate(entry)
        return expired

    def events(self, grant_id: UUID) -> list[dict]:
        entry = self.store.read(str(grant_id))
        if entry is None:
            raise AccessDenied("NOT_FOUND")
        return list(entry["events"])

    def revoke(self, grant_id: UUID, now: datetime) -> None:
        with self.store.update(str(grant_id)) as entry:
            if entry["status"] == "ACTIVE":
                self._destroy_share(entry, "REVOKED", now)
                self._append_event(entry, "GRANT_REVOKED", "PATIENT", now)
            self._validate(entry)

    def retrieve(
        self, grant_id: UUID, *, recipient_email: str, access_code: str, now: datetime
    ) -> tuple[bytes, bytes, bytes, str]:
        """Release (server_share, iv, ciphertext, schema_version) or raise AccessDenied.

        The whole check runs in one locked update, so every attempt is counted
        and logged even when several arrive at once.
        """
        denied: AccessDenied | None = None
        released: tuple[bytes, bytes, bytes, str] | None = None
        with self.store.update(str(grant_id)) as entry:
            self._expire_if_due(entry, now)
            if entry["status"] != "ACTIVE":
                self._append_event(entry, "ACCESS_DENIED", "RECIPIENT", now)
                denied = AccessDenied(entry["status"])
            else:
                _, salt, expected = entry["second_factor_verifier"].split("$")
                code_ok = hmac.compare_digest(
                    _hash_code(access_code.strip(), _unb64(salt)), _unb64(expected)
                )
                identity_ok = hmac.compare_digest(
                    _identity_hash(recipient_email), entry["recipient_identity_hash"]
                )
                if code_ok and identity_ok:
                    self._append_event(entry, "ACCESS_GRANTED", "RECIPIENT", now)
                    released = (
                        _unb64(entry["server_share"]),
                        _unb64(entry["iv"]),
                        _unb64(entry["bundle_ciphertext"]),
                        entry["schema_version"],
                    )
                else:
                    entry["failed_attempts"] += 1
                    self._append_event(entry, "ACCESS_DENIED", "RECIPIENT", now)
                    if entry["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
                        self._destroy_share(entry, "LOCKED", now)
                        self._append_event(entry, "GRANT_LOCKED", "SYSTEM", now)
                        denied = AccessDenied("LOCKED")
                    else:
                        denied = AccessDenied("WRONG_CREDENTIALS")
            self._validate(entry)
        if denied is not None:
            raise denied
        assert released is not None
        return released


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
