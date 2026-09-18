"""Background reminder worker.

Uses Redis as a distributed lock so only one scanner runs at a time.
Run: python -m app.workers.reminder_worker
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.config import get_settings
from app.core.redis_client import close_redis, get_redis
from app.database import AsyncSessionLocal
from app.services.notifications import ReminderScanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lifeos.reminders")

LOCK_KEY = "lifeos:reminder_lock"


async def loop() -> None:
    settings = get_settings()
    worker_id = str(uuid.uuid4())
    logger.info("Reminder worker started id=%s poll=%ss", worker_id, settings.reminder_poll_seconds)

    while True:
        acquired = False
        try:
            r = await get_redis()
            acquired = bool(
                await r.set(LOCK_KEY, worker_id, nx=True, ex=settings.reminder_poll_seconds + 10)
            )
            if acquired:
                async with AsyncSessionLocal() as session:
                    sent = await ReminderScanner(session, settings).run_once()
                    if sent:
                        logger.info("Dispatched %s reminder(s)", sent)
        except Exception:
            logger.exception("Reminder scan failed")
        finally:
            if acquired:
                try:
                    r = await get_redis()
                    current = await r.get(LOCK_KEY)
                    if current == worker_id:
                        await r.delete(LOCK_KEY)
                except Exception:
                    logger.exception("Failed to release reminder lock")

        await asyncio.sleep(settings.reminder_poll_seconds)


async def _main() -> None:
    try:
        await loop()
    finally:
        await close_redis()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
