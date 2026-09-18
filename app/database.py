from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


_SCHEMA_PATCHES = [
    "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS supabase_user_id VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS encrypted_google_refresh_token TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_calendar_connected BOOLEAN DEFAULT FALSE NOT NULL",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS external_id VARCHAR(255)",
    """
    DO $$ BEGIN
      CREATE UNIQUE INDEX IF NOT EXISTS ix_users_supabase_user_id
        ON users (supabase_user_id)
        WHERE supabase_user_id IS NOT NULL;
    EXCEPTION WHEN others THEN NULL;
    END $$;
    """,
    """
    DO $$ BEGIN
      CREATE UNIQUE INDEX IF NOT EXISTS uq_events_user_external
        ON events (user_id, external_id)
        WHERE external_id IS NOT NULL;
    EXCEPTION WHEN others THEN NULL;
    END $$;
    """,
]


async def init_db() -> None:
    # Import models so metadata is registered.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _SCHEMA_PATCHES:
            try:
                await conn.execute(text(stmt))
            except Exception:
                # Fresh DBs / non-Postgres: create_all already applied the model.
                pass
