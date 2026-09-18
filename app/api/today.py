from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import EventOut, TaskOut, TodayResponse
from app.services import TodayService

router = APIRouter(prefix="/today", tags=["today"])


@router.get("", response_model=TodayResponse)
async def get_today(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TodayResponse:
    tasks, events = await TodayService(db).get_today(user)
    return TodayResponse(
        tasks=[TaskOut.model_validate(t) for t in tasks],
        events=[EventOut.model_validate(e) for e in events],
    )
