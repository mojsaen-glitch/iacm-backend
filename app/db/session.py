from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import create_engine
from app.core.config import settings

async_engine = None
AsyncSessionLocal = None
sync_engine = None

if settings.DATABASE_URL:
    async_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={"statement_cache_size": 0},
    )
    AsyncSessionLocal = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

if settings.DATABASE_URL_SYNC:
    sync_engine = create_engine(
        settings.DATABASE_URL_SYNC,
        echo=settings.DEBUG,
        pool_pre_ping=True,
    )


async def get_db() -> AsyncSession:
    if AsyncSessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
