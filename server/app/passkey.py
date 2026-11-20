"""Passkey (WebAuthn) confirmation for large approvals.

Above PASSKEY_THRESHOLD_USD an approval needs two things: the spoken amount and challenge word
on the call, and a passkey assertion from the owner's dashboard. The assertion's challenge is
tied to one approval request, so it cannot be replayed for another.

One passkey is registered per installation, on first use. Registering a second one is refused;
remove the row from the `passkeyrow` table to start again.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.context import AppContext, get_ctx
from app.db import PasskeyRow, utcnow
from app.schemas import OkResponse

CHALLENGE_TTL_S = 120
LOCAL_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:8787",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8787",
)


class PasskeyError(Exception):
    """A passkey step failed. The message is safe to show."""


class JsonBody(BaseModel):
    """A WebAuthn credential as the browser serialises it."""

    model_config = ConfigDict(extra="allow")


class AssertOptionsRequest(BaseModel):
    request_id: str


class AssertVerifyRequest(BaseModel):
    request_id: str
    credential: dict[str, Any] = Field(description="PublicKeyCredential.toJSON() from the browser")


class RegisterVerifyRequest(BaseModel):
    credential: dict[str, Any] = Field(description="PublicKeyCredential.toJSON() from the browser")


class PasskeyStatus(BaseModel):
    registered: bool


@dataclass
class _Challenge:
    value: bytes
    origin: str
    expires: float


class Passkeys:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._challenges: dict[str, _Challenge] = {}

    # ------------------------------------------------------------------ helpers

    def allowed_origins(self) -> tuple[str, ...]:
        public = self._ctx.settings.public_base_url.rstrip("/")
        return (*LOCAL_ORIGINS, public) if public else LOCAL_ORIGINS

    def _origin(self, origin: str | None) -> tuple[str, str]:
        """Validate the browser's Origin header and return it with its relying-party id."""
        if not origin or origin.rstrip("/") not in self.allowed_origins():
            raise PasskeyError("passkeys are only available from the Switchboard dashboard")
        host = urlparse(origin).hostname
        if not host:
            raise PasskeyError("unreadable origin")
        return origin.rstrip("/"), host

    def _credential(self) -> PasskeyRow | None:
        with self._ctx.db.session() as session:
            return session.exec(select(PasskeyRow)).first()

    def registered(self) -> bool:
        return self._credential() is not None

    def _issue(self, key: str, origin: str) -> bytes:
        now = time.monotonic()
        self._challenges = {k: c for k, c in self._challenges.items() if c.expires > now}
        challenge = secrets.token_bytes(32)
        self._challenges[key] = _Challenge(challenge, origin, now + CHALLENGE_TTL_S)
        return challenge

    def _take(self, key: str) -> _Challenge:
        challenge = self._challenges.pop(key, None)
        if challenge is None or challenge.expires <= time.monotonic():
            raise PasskeyError("the passkey prompt expired; start again")
        return challenge

    # ------------------------------------------------------------------ registration

    def registration_options(self, origin_header: str | None) -> dict[str, Any]:
        origin, rp_id = self._origin(origin_header)
        if self.registered():
            raise PasskeyError("a passkey is already registered for this installation")
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="Switchboard",
            user_name="owner",
            user_id=b"switchboard-owner",
            user_display_name="Switchboard owner",
            challenge=self._issue("register", origin),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return dict(json.loads(options_to_json(options)))

    def register(self, credential: dict[str, Any], origin_header: str | None) -> None:
        origin, rp_id = self._origin(origin_header)
        if self.registered():
            raise PasskeyError("a passkey is already registered for this installation")
        challenge = self._take("register")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge.value,
                expected_rp_id=rp_id,
                expected_origin=origin,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            raise PasskeyError(f"the passkey could not be verified: {exc}") from exc
        with self._ctx.db.session() as session:
            session.add(
                PasskeyRow(
                    credential_id=bytes_to_base64url(verified.credential_id),
                    public_key=bytes_to_base64url(verified.credential_public_key),
                    sign_count=verified.sign_count,
                    created=utcnow(),
                )
            )
            session.commit()

    # ------------------------------------------------------------------ assertion

    def assertion_options(self, request_id: str, origin_header: str | None) -> dict[str, Any]:
        origin, rp_id = self._origin(origin_header)
        row = self._ctx.approvals.get(request_id)
        if row is None or row.status != "pending":
            raise PasskeyError("that request is not waiting for approval")
        if not row.passkey_required:
            raise PasskeyError("that request does not need a passkey")
        credential = self._credential()
        if credential is None:
            raise PasskeyError("no passkey is registered yet")
        options = generate_authentication_options(
            rp_id=rp_id,
            challenge=self._issue(f"assert:{request_id}", origin),
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(credential.credential_id))
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return dict(json.loads(options_to_json(options)))

    async def confirm(
        self, request_id: str, credential: dict[str, Any], origin_header: str | None
    ) -> OkResponse:
        origin, rp_id = self._origin(origin_header)
        stored = self._credential()
        if stored is None:
            raise PasskeyError("no passkey is registered yet")
        challenge = self._take(f"assert:{request_id}")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge.value,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=base64url_to_bytes(stored.public_key),
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as exc:
            raise PasskeyError(f"the passkey could not be verified: {exc}") from exc
        stored.sign_count = verified.new_sign_count
        with self._ctx.db.session() as session:
            session.add(stored)
            session.commit()
        return await self._ctx.approvals.confirm_passkey(request_id)


router = APIRouter(prefix="/api/passkey", tags=["passkey"])
Ctx = Annotated[AppContext, Depends(get_ctx)]
Origin = Annotated[str | None, Header()]


def _refuse(exc: PasskeyError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/status", response_model=PasskeyStatus)
async def passkey_status(ctx: Ctx) -> PasskeyStatus:
    return PasskeyStatus(registered=ctx.passkeys.registered())


@router.post("/register/options", response_model=JsonBody)
async def register_options(ctx: Ctx, origin: Origin = None) -> dict[str, Any]:
    """Options for `navigator.credentials.create()`."""
    try:
        return ctx.passkeys.registration_options(origin)
    except PasskeyError as exc:
        raise _refuse(exc) from exc


@router.post("/register/verify", response_model=OkResponse)
async def register_verify(
    body: RegisterVerifyRequest, ctx: Ctx, origin: Origin = None
) -> OkResponse:
    try:
        ctx.passkeys.register(body.credential, origin)
    except PasskeyError as exc:
        raise _refuse(exc) from exc
    return OkResponse(ok=True, status="registered")


@router.post("/assert/options", response_model=JsonBody)
async def assert_options(
    body: AssertOptionsRequest, ctx: Ctx, origin: Origin = None
) -> dict[str, Any]:
    """Options for `navigator.credentials.get()`, bound to one approval request."""
    try:
        return ctx.passkeys.assertion_options(body.request_id, origin)
    except PasskeyError as exc:
        raise _refuse(exc) from exc


@router.post("/assert/verify", response_model=OkResponse)
async def assert_verify(body: AssertVerifyRequest, ctx: Ctx, origin: Origin = None) -> OkResponse:
    """Verify the assertion; with the spoken approval, this completes a large approval."""
    try:
        return await ctx.passkeys.confirm(body.request_id, body.credential, origin)
    except PasskeyError as exc:
        raise _refuse(exc) from exc
