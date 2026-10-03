from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import (
    GardenCatalogResponse,
    GardenItemOut,
    GardenPurchaseRequest,
    GardenPurchaseResponse,
)
from app.services.garden import GardenService

router = APIRouter(prefix="/garden", tags=["garden"])


def _credit_progress(user: User, settings: Settings) -> tuple[int, int, int]:
    interval = max(1, settings.task_credit_interval)
    lifetime = user.tasks_completed_lifetime or 0
    # When lifetime is a multiple of interval, the next reward is a full interval away.
    until_next = interval - (lifetime % interval)
    return lifetime, interval, until_next


@router.get("/catalog", response_model=GardenCatalogResponse)
async def garden_catalog(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> GardenCatalogResponse:
    service = GardenService(db)
    owned = await service.owned_skus(user.id)
    lifetime, interval, until_next = _credit_progress(user, settings)
    return GardenCatalogResponse(
        credit_balance=user.credit_balance,
        items=[GardenItemOut(**row) for row in service.catalog_with_owned(owned)],
        tasks_completed_lifetime=lifetime,
        task_credit_interval=interval,
        tasks_until_next_credit=until_next,
    )


@router.get("/inventory", response_model=List[GardenItemOut])
async def garden_inventory(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[GardenItemOut]:
    service = GardenService(db)
    rows = await service.inventory(user)
    return [GardenItemOut(**row) for row in service.describe_owned([r.sku for r in rows])]


@router.post("/purchase", response_model=GardenPurchaseResponse)
async def garden_purchase(
    body: GardenPurchaseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GardenPurchaseResponse:
    service = GardenService(db)
    await service.purchase(user, body.sku)
    owned = await service.owned_skus(user.id)
    return GardenPurchaseResponse(
        credit_balance=user.credit_balance,
        owned_skus=sorted(owned),
        item=GardenItemOut(**service.describe_owned([body.sku])[0]),
    )
