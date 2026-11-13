"""Passphrase-encrypted LabTrail record file (demo stand-in for the patient vault).

The whole in-memory record is serialized, compressed, and encrypted on the
patient's machine with AES-256-GCM under a key derived from the passphrase with
Argon2id. The file header (format, version, KDF parameters, salt) is bound to the
ciphertext as AES-GCM associated data, so changing any header field makes
decryption fail instead of silently weakening the key derivation.

Not covered here (designed only): a random patient master key wrapped by the
passphrase and by a recovery phrase, key rotation, and server-side blob storage.
"""

import base64
import json
import os
import zlib
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

import models
import labtrail_workflow


FORMAT = "labtrail-vault"
VERSION = 1
MIN_PASSPHRASE_LENGTH = 10
# RFC 9106 second recommended option (64 MiB, t=3, p=4).
DEFAULT_KDF = {"name": "argon2id", "memory_cost_kib": 65536, "iterations": 3, "lanes": 4}
_MIN_KDF = {"memory_cost_kib": 8192, "iterations": 1, "lanes": 1}

# Only these dataclasses may be reconstructed from a vault file.
_DATACLASSES = {
    cls.__name__: cls
    for cls in (
        models.CandidateExtraction,
        models.CandidateExtractionResult,
        models.ExtractionPageInventory,
        models.AcceptedObservation,
        models.ContextNote,
        labtrail_workflow.ReportMetadataSuggestion,
    )
}


class VaultError(ValueError):
    """The file is not a LabTrail vault, or the passphrase is wrong, or it was altered."""


# ---------------------------------------------------------------- codec

def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return {"$t": "uuid", "v": str(value)}
    if isinstance(value, Decimal):
        return {"$t": "decimal", "v": str(value)}
    if isinstance(value, datetime):
        return {"$t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"$t": "date", "v": value.isoformat()}
    if isinstance(value, bytes):
        return {"$t": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"$t": "tuple", "v": [_encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {"$t": "set", "v": [_encode(v) for v in sorted(value, key=repr)]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {"$t": "dict", "v": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if is_dataclass(value) and type(value).__name__ in _DATACLASSES:
        return {
            "$t": "dc",
            "c": type(value).__name__,
            "v": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    raise TypeError(f"Cannot store {type(value).__name__} in a vault.")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind, payload = value.get("$t"), value.get("v")
    if kind == "uuid":
        return UUID(payload)
    if kind == "decimal":
        return Decimal(payload)
    if kind == "datetime":
        return datetime.fromisoformat(payload)
    if kind == "date":
        return date.fromisoformat(payload)
    if kind == "bytes":
        return base64.b64decode(payload, validate=True)
    if kind == "tuple":
        return tuple(_decode(v) for v in payload)
    if kind == "set":
        return {_decode(v) for v in payload}
    if kind == "dict":
        return {_decode(k): _decode(v) for k, v in payload}
    if kind == "dc" and value.get("c") in _DATACLASSES:
        return _DATACLASSES[value["c"]](**{k: _decode(v) for k, v in payload.items()})
    raise VaultError("The vault contains an unsupported record type.")


# ---------------------------------------------------------------- crypto

def _derive_key(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    return Argon2id(
        salt=salt,
        length=32,
        iterations=kdf["iterations"],
        lanes=kdf["lanes"],
        memory_cost=kdf["memory_cost_kib"],
    ).derive(passphrase.encode("utf-8"))


def _header_aad(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encrypt_record(record: dict, passphrase: str, *, kdf: dict | None = None) -> bytes:
    """Serialize, compress, and encrypt a record dict into vault file bytes."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters.")
    kdf = dict(DEFAULT_KDF if kdf is None else kdf)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": kdf,
        "salt": base64.b64encode(salt).decode("ascii"),
    }
    plaintext = zlib.compress(
        json.dumps(_encode(record), separators=(",", ":")).encode("utf-8"), level=6
    )
    ciphertext = AESGCM(_derive_key(passphrase, salt, kdf)).encrypt(
        nonce, plaintext, _header_aad(header)
    )
    return json.dumps(
        {
            "header": header,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        },
        indent=1,
    ).encode("utf-8")


def decrypt_record(data: bytes, passphrase: str) -> dict:
    """Decrypt vault file bytes; raise VaultError on any mismatch or tampering."""
    try:
        envelope = json.loads(data)
        header = envelope["header"]
        kdf = header["kdf"]
        if header["format"] != FORMAT or header["version"] != VERSION:
            raise VaultError("This is not a supported LabTrail vault file.")
        if kdf.get("name") != "argon2id" or any(
            not isinstance(kdf.get(k), int) or kdf[k] < minimum
            for k, minimum in _MIN_KDF.items()
        ):
            raise VaultError("The vault's key-derivation settings are not allowed.")
        salt = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    except VaultError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise VaultError("This is not a LabTrail vault file.") from error
    try:
        plaintext = AESGCM(_derive_key(passphrase, salt, kdf)).decrypt(
            nonce, ciphertext, _header_aad(header)
        )
    except InvalidTag as error:
        raise VaultError("Wrong passphrase, or the file has been altered.") from error
    record = _decode(json.loads(zlib.decompress(plaintext)))
    if not isinstance(record, dict):
        raise VaultError("The vault contents are malformed.")
    return record
