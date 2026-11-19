"""A software WebAuthn authenticator for tests: a real P-256 key that signs real assertions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

USER_PRESENT = 0x01
USER_VERIFIED = 0x04
ATTESTED_CREDENTIAL = 0x40


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self._sign_count = 0

    def _authenticator_data(self, rp_id: str, flags: int, attested: bool) -> bytes:
        self._sign_count += 1
        data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
        data += self._sign_count.to_bytes(4, "big")
        if attested:
            numbers = self._key.public_key().public_numbers()
            cose_key = cbor2.dumps(
                {
                    1: 2,  # kty: EC2
                    3: -7,  # alg: ES256
                    -1: 1,  # crv: P-256
                    -2: numbers.x.to_bytes(32, "big"),
                    -3: numbers.y.to_bytes(32, "big"),
                }
            )
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big")
            data += self.credential_id + cose_key
        return data

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict[str, Any], origin: str) -> dict[str, Any]:
        """What `navigator.credentials.create()` returns, as JSON."""
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = self._authenticator_data(
            options["rp"]["id"], USER_PRESENT | USER_VERIFIED | ATTESTED_CREDENTIAL, attested=True
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
            },
            "clientExtensionResults": {},
        }

    def get(self, options: dict[str, Any], origin: str) -> dict[str, Any]:
        """What `navigator.credentials.get()` returns, as JSON."""
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = self._authenticator_data(
            options["rpId"], USER_PRESENT | USER_VERIFIED, attested=False
        )
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
            },
            "clientExtensionResults": {},
        }
