# 测试用：删除所有表，重启 API 后会自动按当前模型重建（无需数据迁移）
import asyncio
from database import engine, Base
import models  # noqa: F401 — 注册表到 Base.metadata


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    print("All tables dropped. Restart API (python run_api.py) to recreate tables.")


if __name__ == "__main__":
    asyncio.run(main())
