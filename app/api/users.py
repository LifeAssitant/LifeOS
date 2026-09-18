from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import get_secret_box
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import AISettingsUpdate, CreditsResponse, UserPublic, UserUpdate
from app.services import UserService, user_to_public

router = APIRouter(prefix="/me", tags=["users"])


@router.get("", response_model=UserPublic)
async def get_me(user: User = Depends(get_current_user)) -> UserPublic:
    return user_to_public(user)


@router.patch("", response_model=UserPublic)
async def update_me(
    payload: UserUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    updated = await UserService(db, get_secret_box()).update_profile(user, payload)
    return user_to_public(updated)


@router.put("/ai", response_model=UserPublic)
async def update_ai(
    payload: AISettingsUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    updated = await UserService(db, get_secret_box()).update_ai_settings(user, payload)
    return user_to_public(updated)


@router.get("/credits", response_model=CreditsResponse)
async def credits(user: User = Depends(get_current_user)) -> CreditsResponse:
    return CreditsResponse(credit_balance=user.credit_balance, ai_mode=user.ai_mode)
