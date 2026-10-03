from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserGardenItem


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    description: str
    category: str
    credit_cost: int


# Static shop catalog — skins and side props; task growth stays free.
# Prices stay small so finishing ~50 tasks (1 credit) meaningfully progresses the shelf.
GARDEN_CATALOG: tuple[CatalogItem, ...] = (
    CatalogItem(
        sku="wheelbarrow",
        name="Wheelbarrow",
        description="A tidy cart parked near the beds.",
        category="decor",
        credit_cost=5,
    ),
    CatalogItem(
        sku="wind_chimes",
        name="Wind chimes",
        description="Light chimes hanging from the fence.",
        category="decor",
        credit_cost=5,
    ),
    CatalogItem(
        sku="mushroom_ring",
        name="Mushroom ring",
        description="A quiet circle of spotted caps.",
        category="flora",
        credit_cost=5,
    ),
    CatalogItem(
        sku="birdbath",
        name="Birdbath",
        description="A shallow stone basin by the flowers.",
        category="decor",
        credit_cost=8,
    ),
    CatalogItem(
        sku="birdhouse",
        name="Birdhouse",
        description="A little house on a post by the oak.",
        category="decor",
        credit_cost=8,
    ),
    CatalogItem(
        sku="pond_lilies",
        name="Extra lilies",
        description="More pads and blooms for the pond.",
        category="flora",
        credit_cost=8,
    ),
    CatalogItem(
        sku="lantern_copper",
        name="Copper lantern",
        description="Warm copper glow for the evening path.",
        category="lighting",
        credit_cost=8,
    ),
    CatalogItem(
        sku="bench_willow",
        name="Willow bench",
        description="A curved wooden bench with a softer seat.",
        category="seating",
        credit_cost=10,
    ),
    CatalogItem(
        sku="lantern_moon",
        name="Moon lantern",
        description="A pale lantern that softens the night.",
        category="lighting",
        credit_cost=10,
    ),
    CatalogItem(
        sku="topiary",
        name="Topiary ball",
        description="A clipped green sphere by the gate.",
        category="flora",
        credit_cost=10,
    ),
    CatalogItem(
        sku="bench_stone",
        name="Stone bench",
        description="A cool stone perch beside the path.",
        category="seating",
        credit_cost=12,
    ),
    CatalogItem(
        sku="lights_fairy",
        name="Fairy lights",
        description="Tiny lights for the cottage eaves.",
        category="lighting",
        credit_cost=12,
    ),
    CatalogItem(
        sku="trellis",
        name="Flower trellis",
        description="A climbing frame with soft blooms.",
        category="decor",
        credit_cost=12,
    ),
    CatalogItem(
        sku="hammock",
        name="Garden hammock",
        description="A striped hammock between the trees.",
        category="seating",
        credit_cost=15,
    ),
    CatalogItem(
        sku="fireflies",
        name="Firefly jar",
        description="Soft drifting sparks after dusk.",
        category="lighting",
        credit_cost=15,
    ),
    CatalogItem(
        sku="fountain",
        name="Tiny fountain",
        description="A bubbling stone spout near the path.",
        category="decor",
        credit_cost=15,
    ),
)

_CATALOG_BY_SKU = {item.sku: item for item in GARDEN_CATALOG}


def get_catalog_item(sku: str) -> CatalogItem | None:
    return _CATALOG_BY_SKU.get(sku)


class GardenService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def owned_skus(self, user_id: UUID) -> set[str]:
        result = await self.db.execute(
            select(UserGardenItem.sku).where(UserGardenItem.user_id == user_id)
        )
        return set(result.scalars().all())

    async def inventory(self, user: User) -> List[UserGardenItem]:
        result = await self.db.execute(
            select(UserGardenItem)
            .where(UserGardenItem.user_id == user.id)
            .order_by(UserGardenItem.created_at.asc())
        )
        return list(result.scalars().all())

    def catalog_with_owned(self, owned: Iterable[str]) -> list[dict]:
        owned_set = set(owned)
        return [
            {
                "sku": item.sku,
                "name": item.name,
                "description": item.description,
                "category": item.category,
                "credit_cost": item.credit_cost,
                "owned": item.sku in owned_set,
            }
            for item in GARDEN_CATALOG
        ]

    async def purchase(self, user: User, sku: str) -> UserGardenItem:
        item = get_catalog_item(sku)
        if item is None:
            raise HTTPException(status_code=404, detail="That garden item is not in the store.")

        owned = await self.owned_skus(user.id)
        if sku in owned:
            raise HTTPException(status_code=409, detail="You already own this garden item.")

        if user.credit_balance < item.credit_cost:
            raise HTTPException(
                status_code=402,
                detail="Not enough credits. Buy more credits, then come back to the garden store.",
            )

        user.credit_balance -= item.credit_cost
        row = UserGardenItem(user_id=user.id, sku=sku)
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        await self.db.refresh(user)
        return row

    def describe_owned(self, skus: Sequence[str]) -> list[dict]:
        out: list[dict] = []
        for sku in skus:
            item = get_catalog_item(sku)
            if item is None:
                continue
            out.append(
                {
                    "sku": item.sku,
                    "name": item.name,
                    "description": item.description,
                    "category": item.category,
                    "credit_cost": item.credit_cost,
                    "owned": True,
                }
            )
        return out
