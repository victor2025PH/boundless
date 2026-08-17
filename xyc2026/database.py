# Async DB session; SQLite (MVP) or PostgreSQL via DATABASE_URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

# Normalize driver: SQLite -> aiosqlite, PostgreSQL -> asyncpg
_db_url = settings.database_url.strip()
if _db_url.startswith("sqlite://"):
    _db_url = _db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
if "postgresql://" in _db_url and "+asyncpg" not in _db_url:
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _db_url,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
