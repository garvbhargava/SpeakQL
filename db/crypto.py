"""Encrypting external connection credentials at rest (Backend Plan §9.3).

An owner who registers an external database hands over a username and a
password. Those are stored encrypted, decrypted in exactly one place
(`db/tenant_engine.py`, at the moment an engine is built), and never returned
by any endpoint -- including the one that lists connections, whose response
model has no field they could occupy.

Fernet (AES-128-CBC with an HMAC-SHA256) from `cryptography`: authenticated
encryption, so a ciphertext that has been tampered with fails to decrypt
rather than decrypting to something plausible.

The key is derived from SECRET_KEY with HKDF and a fixed context string, so
the same secret never serves two purposes -- the JWT signing key and the
credential key are different keys even though one variable produces both.
Rotating SECRET_KEY therefore makes stored credentials unreadable, which is
the correct failure: re-register the connection rather than leave old
credentials decryptable with a retired secret.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_CONTEXT = b"speakql/connection-credentials/v1"


class CredentialError(RuntimeError):
    """Stored credentials could not be decrypted. Re-register the connection."""


def _fernet(secret_key: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_CONTEXT,
    ).derive(secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def seal(secret_key: str, dsn: str) -> bytes:
    """Encrypt a connection string for storage."""
    return _fernet(secret_key).encrypt(dsn.encode())


def unseal(secret_key: str, cipher: bytes) -> str:
    """Decrypt a stored connection string. Raises CredentialError."""
    if not cipher:
        raise CredentialError("this connection has no stored credentials")
    try:
        return _fernet(secret_key).decrypt(bytes(cipher)).decode()
    except InvalidToken as exc:
        # Wrong key (SECRET_KEY rotated) or tampered ciphertext. Either way
        # the right answer is to stop, not to guess.
        raise CredentialError(
            "stored credentials could not be decrypted; register the "
            "connection again"
        ) from exc
