"""Authenticated encryption for the minimum durable lifecycle crash payload."""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class RecoveryPayloadRejected(ValueError):
    """A durable recovery payload is missing, malformed, or not authentic."""


class RecoveryCipher:
    """Seal crash-only request bytes without retaining raw human or repository content."""

    _VERSION = "v1"

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("lifecycle recovery key must contain exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64(cls, encoded: str) -> RecoveryCipher:
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("lifecycle recovery key must be canonical base64") from error
        if base64.b64encode(key).decode("ascii") != encoded:
            raise ValueError("lifecycle recovery key must be canonical base64")
        return cls(key)

    def seal(self, *, purpose: str, binding: str, plaintext: str) -> str:
        aad = self._aad(purpose, binding)
        nonce = os.urandom(12)
        encrypted = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        payload = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
        return f"{self._VERSION}:{payload}"

    def open(self, *, purpose: str, binding: str, ciphertext: str) -> str:
        prefix = f"{self._VERSION}:"
        if not ciphertext.startswith(prefix):
            raise RecoveryPayloadRejected("durable recovery payload has an unsupported version")
        try:
            raw = base64.b64decode(
                ciphertext.removeprefix(prefix),
                altchars=b"-_",
                validate=True,
            )
            if len(raw) < 29:
                raise ValueError
            plaintext = self._cipher.decrypt(
                raw[:12],
                raw[12:],
                self._aad(purpose, binding),
            )
            return plaintext.decode("utf-8")
        except (binascii.Error, InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise RecoveryPayloadRejected(
                "durable recovery payload is malformed or unauthentic"
            ) from error

    @staticmethod
    def _aad(purpose: str, binding: str) -> bytes:
        if not purpose or not binding:
            raise ValueError("recovery purpose and immutable binding are required")
        return f"planning-platform:lifecycle:{purpose}:{binding}".encode()
