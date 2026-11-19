"""Dashboard speech through ElevenLabs: the microphone, and the quote read back.

Neither needs a phone line. With ELEVENLABS_API_KEY set, the dashboard sends what the
microphone recorded to ElevenLabs speech-to-text and plays the operator's lines with
ElevenLabs text-to-speech. With the key blank, both routes answer 503 and the dashboard uses
the browser's own speech instead.

These are dashboard routes, so `LocalOnly` keeps them on this machine: a public tunnel does
not expose a way to spend the account's credits. The key is sent to api.elevenlabs.io only.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response

from app.context import AppContext, get_ctx
from app.schemas import HeardSpeech, SayRequest, SpeechStatus

ELEVENLABS_API = "https://api.elevenlabs.io"
TTS_PATH = "/v1/text-to-speech"
STT_PATH = "/v1/speech-to-text"
# "George", one of the voices every ElevenLabs account has. ELEVENLABS_OPERATOR_VOICE_ID
# replaces it.
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
TTS_FORMAT = "mp3_44100_128"
MAX_AUDIO_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 20.0


class SpeechUnavailable(RuntimeError):
    """ElevenLabs is not configured, or it refused the request."""


class Speech:
    def __init__(self, ctx: AppContext, http: httpx.AsyncClient | None = None) -> None:
        self._ctx = ctx
        self._http = http

    @property
    def available(self) -> bool:
        return bool(self._ctx.settings.elevenlabs_api_key)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.available:
            raise SpeechUnavailable("ELEVENLABS_API_KEY is not set")
        return {"xi-api-key": self._ctx.settings.elevenlabs_api_key}

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            raise SpeechUnavailable(
                f"ElevenLabs answered {response.status_code}: {response.text[:200]}"
            )
        return response

    async def say(self, text: str) -> bytes:
        """Text to MP3 audio in the operator's voice."""
        settings = self._ctx.settings
        voice = settings.elevenlabs_operator_voice_id or DEFAULT_VOICE_ID
        try:
            response = await self._client().post(
                f"{ELEVENLABS_API}{TTS_PATH}/{voice}",
                params={"output_format": TTS_FORMAT},
                headers=self._headers(),
                json={"text": text, "model_id": settings.elevenlabs_tts_model},
                timeout=TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise SpeechUnavailable(f"{type(exc).__name__}: {exc}") from exc
        return self._checked(response).content

    async def hear(self, audio: bytes, content_type: str) -> str:
        """Recorded audio to text."""
        try:
            response = await self._client().post(
                f"{ELEVENLABS_API}{STT_PATH}",
                headers=self._headers(),
                data={"model_id": self._ctx.settings.elevenlabs_stt_model},
                files={"file": ("speech", audio, content_type or "application/octet-stream")},
                timeout=TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise SpeechUnavailable(f"{type(exc).__name__}: {exc}") from exc
        body = self._checked(response).json()
        return str(body.get("text") or "").strip()


Ctx = Annotated[AppContext, Depends(get_ctx)]
router = APIRouter(prefix="/api/speech", tags=["speech"])


@router.get("", response_model=SpeechStatus)
async def speech_status(ctx: Ctx) -> SpeechStatus:
    """Whether the dashboard should use ElevenLabs or the browser's own speech."""
    return SpeechStatus(elevenlabs=ctx.speech.available)


@router.post(
    "/say",
    response_class=Response,
    responses={200: {"content": {"audio/mpeg": {}}, "description": "MP3 audio"}},
)
async def say(body: SayRequest, ctx: Ctx) -> Response:
    """Read a line aloud in the operator's voice (ElevenLabs text-to-speech)."""
    try:
        audio = await ctx.speech.say(body.text)
    except SpeechUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/hear", response_model=HeardSpeech)
async def hear(
    request: Request, ctx: Ctx, content_type: Annotated[str | None, Header()] = None
) -> HeardSpeech:
    """Turn a microphone recording into text (ElevenLabs speech-to-text). The body is the
    raw audio; the text comes back for the owner to check before anything is sent."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="no audio was sent")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="the recording is too long")
    try:
        text = await ctx.speech.hear(audio, content_type or "")
    except SpeechUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return HeardSpeech(text=text)
