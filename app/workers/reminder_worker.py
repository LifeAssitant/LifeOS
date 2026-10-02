"""Background reminder worker coordinated by a PostgreSQL advisory lock."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from app.config import get_settings
from app.database import AsyncSessionLocal, engine
from app.services.notifications import ReminderScanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lifeos.reminders")

LOCK_ID = 6047161334812023


async def scan_once(settings) -> None:
    async with engine.connect() as lock_connection:
        async with lock_connection.begin():
            acquired = await lock_connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": LOCK_ID},
            )
            if not acquired:
                return

            async with AsyncSessionLocal() as session:
                sent = await ReminderScanner(session, settings).run_once()
                if sent:
                    logger.info("Dispatched %s reminder(s)", sent)


async def loop() -> None:
    settings = get_settings()
    worker_id = str(uuid.uuid4())
    logger.info("Reminder worker started id=%s poll=%ss", worker_id, settings.reminder_poll_seconds)

    while True:
        try:
            await scan_once(settings)
        except Exception:
            logger.exception("Reminder scan failed")

        await asyncio.sleep(settings.reminder_poll_seconds)


async def _main() -> None:
    try:
        await loop()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
