from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.encryption import get_secret_box
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import ChatMessageOut, ChatSendRequest, ChatUndoRequest, TranscribeResponse
from app.services import GeminiChatService, UserService

MAX_AUDIO_BYTES = 8 * 1024 * 1024

router = APIRouter(prefix="/chat", tags=["chat"])


def _chat_service(db: AsyncSession, settings: Settings) -> GeminiChatService:
    return GeminiChatService(db, settings, UserService(db, get_secret_box()))


@router.get("/messages", response_model=List[ChatMessageOut])
async def list_messages(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> List[ChatMessageOut]:
    messages = await _chat_service(db, settings).history(user)
    return [ChatMessageOut.model_validate(m) for m in messages]


@router.post("/send", response_model=ChatMessageOut)
async def send_message(
    payload: ChatSendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ChatMessageOut:
    message = await _chat_service(db, settings).send(
        user, payload.message, timezone_name=payload.timezone
    )
    return ChatMessageOut.model_validate(message)


@router.post("/stream")
async def stream_message(
    payload: ChatSendRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    service = _chat_service(db, settings)

    async def event_generator():
        async for chunk in service.stream_tokens(
            user, payload.message, timezone_name=payload.timezone
        ):
            yield f"data: {chunk}\n\n"
        yield 'data: {"type":"close"}\n\n'

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TranscribeResponse:
    audio = await file.read()
    if not audio or len(audio) < 200:
        raise HTTPException(status_code=400, detail="That clip was too quiet. Tap the mic, speak, then tap again to send.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Voice clip is too long. Try a shorter message.")
    mime = file.content_type or "application/octet-stream"
    name = (file.filename or "").lower()
    if mime in {"application/octet-stream", "binary/octet-stream"}:
        if name.endswith(".webm"):
            mime = "audio/webm"
        elif name.endswith(".m4a") or name.endswith(".mp4"):
            mime = "audio/mp4"
        elif name.endswith(".wav"):
            mime = "audio/wav"
        elif name.endswith(".mp3"):
            mime = "audio/mpeg"
        elif name.endswith(".ogg"):
            mime = "audio/ogg"
        elif name.endswith(".aac"):
            mime = "audio/aac"
        elif name.endswith(".caf"):
            mime = "audio/x-caf"
    text = await _chat_service(db, settings).transcribe_audio(user, audio, mime)
    if not text:
        raise HTTPException(
            status_code=422,
            detail="I couldn't hear anything. Tap the mic, speak, then tap again to send.",
        )
    return TranscribeResponse(text=text)


@router.post("/undo", response_model=dict)
async def undo_action(
    payload: ChatUndoRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    await _chat_service(db, settings).undo_action(
        user, payload.message_id, payload.action_index
    )
    return {"ok": True}
