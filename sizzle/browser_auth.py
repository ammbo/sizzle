"""Browser auth: encrypt/decrypt Playwright storageState for authenticated app capture.

The live-view session management (Xvfb, x11vnc, websockify, Chromium) lives in the
login worker FC function, not here. This module handles encryption at rest and the
data contract for encrypted state blobs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

_ENCRYPTION_KEY_ENV = "SIZZLE_STATE_ENCRYPTION_KEY"


@dataclass
class EncryptedBrowserState:
    """Encrypted Playwright storageState, stored in OSS."""

    run_id: str
    ciphertext_b64: str
    created_at: float
    ttl_seconds: int = 3600
    origin: str = ""

    def is_expired(self) -> bool:
        return time.time() > self.created_at + self.ttl_seconds

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "ciphertext_b64": self.ciphertext_b64,
                "created_at": self.created_at,
                "ttl_seconds": self.ttl_seconds,
                "origin": self.origin,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "EncryptedBrowserState":
        data = json.loads(raw)
        return cls(**data)


def _encryption_key() -> bytes:
    key = os.environ.get(_ENCRYPTION_KEY_ENV, "")
    if key:
        return key.encode()
    raise RuntimeError(
        f"{_ENCRYPTION_KEY_ENV} must be set. Generate with: "
        "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
    )


def encrypt_state(state_json: str) -> str:
    """Encrypt a storageState JSON string. Returns base64-encoded ciphertext."""
    f = Fernet(_encryption_key())
    return f.encrypt(state_json.encode()).decode()


def decrypt_state(ciphertext_b64: str) -> str:
    """Decrypt a storageState ciphertext. Returns the JSON string."""
    f = Fernet(_encryption_key())
    return f.decrypt(ciphertext_b64.encode()).decode()


def encrypt_and_wrap(
    state_json: str,
    run_id: str,
    origin: str,
    ttl_seconds: int = 3600,
) -> EncryptedBrowserState:
    """Encrypt storageState and return a wrapped EncryptedBrowserState."""
    return EncryptedBrowserState(
        run_id=run_id,
        ciphertext_b64=encrypt_state(state_json),
        created_at=time.time(),
        ttl_seconds=ttl_seconds,
        origin=origin,
    )


def decrypt_and_load(encrypted: EncryptedBrowserState) -> dict:
    """Decrypt an EncryptedBrowserState and return the storageState dict.

    Raises RuntimeError if the state has expired.
    """
    if encrypted.is_expired():
        raise RuntimeError(
            f"Browser state for run {encrypted.run_id} has expired "
            f"({encrypted.ttl_seconds}s TTL)"
        )
    return json.loads(decrypt_state(encrypted.ciphertext_b64))
