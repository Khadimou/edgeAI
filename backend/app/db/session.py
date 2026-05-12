from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

def _build_url(raw: str) -> str:
    url = raw.replace("postgresql://", "postgresql+asyncpg://")
    url = url.replace("postgres://", "postgresql+asyncpg://")
    return url

_connect_args = {}
if "sslmode=require" in settings.database_url:
    _connect_args = {"ssl": True}

engine = create_async_engine(
    _build_url(settings.database_url.split("?")[0]),
    connect_args=_connect_args,
    pool_size=10,
    max_overflow=20,
    echo=settings.environment == "development",
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
