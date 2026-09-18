from typing import List

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.encryption import get_secret_box
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import ChatMessageOut, ChatSendRequest, ChatUndoRequest
from app.services import GeminiChatService, UserService

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
