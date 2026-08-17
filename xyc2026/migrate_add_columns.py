# 一次性迁移：为已有数据库补齐代码中新增的列（避免 500：users.last_seen_at 不存在等）
# 用法：python migrate_add_columns.py
# 依赖：.env 中 DATABASE_URL 指向当前使用的库（如 PostgreSQL）
import asyncio
import os
import sys

# 确保加载 .env
if not os.environ.get("DATABASE_URL"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

from sqlalchemy import text
from database import engine

# PostgreSQL：仅当列不存在时添加（兼容旧库）
PG_MIGRATIONS = [
    ("users", "last_seen_at", "TIMESTAMP"),
    ("users", "query_count", "INTEGER DEFAULT 0"),
    ("users", "notify_on_checked", "BOOLEAN DEFAULT TRUE"),
    ("users", "privacy_settings", "JSONB"),
    ("reports", "reason", "TEXT"),
    ("reports", "category", "VARCHAR(32)"),
    ("risk_alert_subscriptions", "source", "VARCHAR(32)"),
    ("risk_alert_subscriptions", "alert_type", "VARCHAR(32) DEFAULT 'risk_change'"),
]


async def run_pg():
    async with engine.begin() as conn:
        for table, column, col_type in PG_MIGRATIONS:
            check = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
            if check.scalar():
                print(f"  [OK] {table}.{column} already exists")
                continue
            await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}'))
            print(f"  [ADD] {table}.{column}")
        # 唯一约束：risk_alert_subscriptions 改为 (subscriber_tg_id, target_tg_id, alert_type)
        try:
            await conn.execute(text("ALTER TABLE risk_alert_subscriptions DROP CONSTRAINT IF EXISTS uq_risk_alert_sub"))
            await conn.execute(
                text(
                    "ALTER TABLE risk_alert_subscriptions ADD CONSTRAINT uq_risk_alert_sub "
                    "UNIQUE (subscriber_tg_id, target_tg_id, alert_type)"
                )
            )
            print("  [OK] risk_alert_subscriptions unique constraint updated")
        except Exception as e:
            if "already exists" in str(e).lower():
                print("  [OK] uq_risk_alert_sub already updated")
            else:
                raise
        # 新表 online_status_events（若不存在则由 create_all 创建；此处仅确保存在）
        check_t = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'online_status_events'"
            )
        )
        if not check_t.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE online_status_events (
                        id SERIAL PRIMARY KEY,
                        target_tg_id BIGINT NOT NULL,
                        event_type VARCHAR(16) NOT NULL,
                        reporter_tg_id BIGINT,
                        monitor_tg_id BIGINT,
                        at_ts TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        payload JSONB
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_online_status_events_target_tg_id ON online_status_events (target_tg_id)"))
            await conn.execute(text("CREATE INDEX ix_online_status_events_event_type ON online_status_events (event_type)"))
            print("  [ADD] table online_status_events")
        else:
            print("  [OK] online_status_events already exists")
        # V2.0 交易确认表 transactions
        check_tx = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'transactions'"
            )
        )
        if not check_tx.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE transactions (
                        id SERIAL PRIMARY KEY,
                        initiator_tg_id BIGINT NOT NULL,
                        counterparty_tg_id BIGINT NOT NULL,
                        amount VARCHAR(32) NOT NULL,
                        currency VARCHAR(16) DEFAULT 'USDT',
                        memo VARCHAR(500),
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confirmed_at TIMESTAMP
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_transactions_initiator_tg_id ON transactions (initiator_tg_id)"))
            await conn.execute(text("CREATE INDEX ix_transactions_counterparty_tg_id ON transactions (counterparty_tg_id)"))
            await conn.execute(text("CREATE INDEX ix_transactions_status ON transactions (status)"))
            print("  [ADD] table transactions")
        else:
            print("  [OK] transactions already exists")
        # V2.0 被查通知待发送表
        check_nc = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'notify_checked_pending'"
            )
        )
        if not check_nc.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE notify_checked_pending (
                        id SERIAL PRIMARY KEY,
                        target_tg_id BIGINT NOT NULL,
                        querier_tg_id BIGINT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        sent_at TIMESTAMP
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_notify_checked_pending_target_tg_id ON notify_checked_pending (target_tg_id)"))
            await conn.execute(text("CREATE INDEX ix_notify_checked_pending_sent_at ON notify_checked_pending (sent_at)"))
            print("  [ADD] table notify_checked_pending")
        else:
            print("  [OK] notify_checked_pending already exists")
        # V2.0 群扫描记录表
        check_gs = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'group_scan_log'"
            )
        )
        if not check_gs.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE group_scan_log (
                        id SERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL,
                        requester_tg_id BIGINT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_group_scan_log_group_id ON group_scan_log (group_id)"))
            await conn.execute(text("CREATE INDEX ix_group_scan_log_created_at ON group_scan_log (created_at)"))
            print("  [ADD] table group_scan_log")
        else:
            print("  [OK] group_scan_log already exists")
        # 阶段 A：报告防伪指纹表 report_snapshots
        check_rs = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'report_snapshots'"
            )
        )
        if not check_rs.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE report_snapshots (
                        id SERIAL PRIMARY KEY,
                        report_hash VARCHAR(16) UNIQUE NOT NULL,
                        target_tg_id BIGINT NOT NULL,
                        content_hash VARCHAR(64) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_report_snapshots_report_hash ON report_snapshots (report_hash)"))
            await conn.execute(text("CREATE INDEX ix_report_snapshots_target_tg_id ON report_snapshots (target_tg_id)"))
            print("  [ADD] table report_snapshots")
        else:
            print("  [OK] report_snapshots already exists")
        # V2.0 任务 1.6：举报人统计表 reporter_stats
        check_rep = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'reporter_stats'"
            )
        )
        if not check_rep.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE reporter_stats (
                        reporter_tg_id BIGINT PRIMARY KEY,
                        total_reports INTEGER NOT NULL DEFAULT 0,
                        verified_count INTEGER NOT NULL DEFAULT 0,
                        rejected_count INTEGER NOT NULL DEFAULT 0,
                        last_report_at TIMESTAMP
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_reporter_stats_last_report_at ON reporter_stats (last_report_at DESC)"))
            print("  [ADD] table reporter_stats")
        else:
            print("  [OK] reporter_stats already exists")
            # P2: 增加 verified_count / rejected_count 列（若不存在）
            for col, ctype in [("verified_count", "INTEGER NOT NULL DEFAULT 0"), ("rejected_count", "INTEGER NOT NULL DEFAULT 0")]:
                check_col = await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'reporter_stats' AND column_name = :c"
                    ),
                    {"c": col},
                )
                if not check_col.scalar():
                    await conn.execute(text(f'ALTER TABLE reporter_stats ADD COLUMN "{col}" {ctype}'))
                    print(f"  [ADD] reporter_stats.{col}")
        # 绑定收款地址表 user_bound_wallets
        check_ub = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'user_bound_wallets'"
            )
        )
        if not check_ub.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE user_bound_wallets (
                        id SERIAL PRIMARY KEY,
                        tg_id BIGINT NOT NULL,
                        chain VARCHAR(32) NOT NULL,
                        address VARCHAR(128) NOT NULL,
                        label VARCHAR(64),
                        show_flow_to_public BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        verified_at TIMESTAMP,
                        CONSTRAINT uq_user_bound_wallet UNIQUE (tg_id, address, chain)
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_user_bound_wallets_tg_id ON user_bound_wallets (tg_id)"))
            await conn.execute(text("CREATE INDEX ix_user_bound_wallets_address ON user_bound_wallets (address)"))
            print("  [ADD] table user_bound_wallets")
        else:
            print("  [OK] user_bound_wallets already exists")
        # 流水快照表
        check_ws = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'wallet_transaction_snapshots'"
            )
        )
        if not check_ws.scalar():
            await conn.execute(
                text(
                    """
                    CREATE TABLE wallet_transaction_snapshots (
                        id SERIAL PRIMARY KEY,
                        wallet_id INTEGER NOT NULL,
                        tx_hash VARCHAR(128) NOT NULL,
                        chain VARCHAR(32) NOT NULL,
                        direction VARCHAR(8) NOT NULL,
                        amount DOUBLE PRECISION NOT NULL,
                        currency VARCHAR(16) DEFAULT 'USDT',
                        counterparty_masked VARCHAR(32) DEFAULT '',
                        block_time_ms BIGINT DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_wallet_tx_snapshot UNIQUE (wallet_id, tx_hash)
                    )
                    """
                )
            )
            await conn.execute(text("CREATE INDEX ix_wallet_transaction_snapshots_wallet_id ON wallet_transaction_snapshots (wallet_id)"))
            await conn.execute(text("CREATE INDEX ix_wallet_transaction_snapshots_tx_hash ON wallet_transaction_snapshots (tx_hash)"))
            await conn.execute(text("CREATE INDEX ix_wallet_transaction_snapshots_block_time_ms ON wallet_transaction_snapshots (block_time_ms DESC)"))
            print("  [ADD] table wallet_transaction_snapshots")
        else:
            print("  [OK] wallet_transaction_snapshots already exists")
    print("PostgreSQL migration done.")


async def run_sqlite():
    # SQLite 的 ADD COLUMN 若列已存在会报错，逐条执行并忽略重复列错误
    for table, column, col_type in PG_MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type}'))
            print(f"  [ADD] {table}.{column}")
        except Exception as e:
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                print(f"  [OK] {table}.{column} already exists")
            else:
                raise
    # risk_alert_subscriptions：SQLite 无法 DROP CONSTRAINT，仅首次迁移时重建表
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS _migration_markers (name TEXT PRIMARY KEY)"))
        r = await conn.execute(text("SELECT 1 FROM _migration_markers WHERE name = 'risk_alert_sub_alert_type_unique'"))
        if not r.scalar():
            r2 = await conn.execute(
                text("SELECT 1 FROM pragma_table_info('risk_alert_subscriptions') WHERE name='alert_type'")
            )
            if r2.scalar():
                await conn.execute(
                    text(
                        """
                        CREATE TABLE risk_alert_subscriptions_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            subscriber_tg_id BIGINT NOT NULL,
                            target_tg_id BIGINT NOT NULL,
                            alert_type VARCHAR(32) NOT NULL DEFAULT 'risk_change',
                            preferred_lang VARCHAR(8),
                            source VARCHAR(32),
                            created_at TIMESTAMP,
                            UNIQUE(subscriber_tg_id, target_tg_id, alert_type)
                        )
                        """
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO risk_alert_subscriptions_new "
                        "(id, subscriber_tg_id, target_tg_id, alert_type, preferred_lang, source, created_at) "
                        "SELECT id, subscriber_tg_id, target_tg_id, COALESCE(alert_type, 'risk_change'), preferred_lang, source, created_at "
                        "FROM risk_alert_subscriptions"
                    )
                )
                await conn.execute(text("DROP TABLE risk_alert_subscriptions"))
                await conn.execute(text("ALTER TABLE risk_alert_subscriptions_new RENAME TO risk_alert_subscriptions"))
                await conn.execute(text("INSERT OR IGNORE INTO _migration_markers (name) VALUES ('risk_alert_sub_alert_type_unique')"))
                print("  [OK] risk_alert_subscriptions recreated with alert_type unique")
    # 新表 online_status_events
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS online_status_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_tg_id BIGINT NOT NULL,
                    event_type VARCHAR(16) NOT NULL,
                    reporter_tg_id BIGINT,
                    monitor_tg_id BIGINT,
                    at_ts TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT
                )
                """
            )
        )
        print("  [OK] online_status_events exists")
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    initiator_tg_id BIGINT NOT NULL,
                    counterparty_tg_id BIGINT NOT NULL,
                    amount VARCHAR(32) NOT NULL,
                    currency VARCHAR(16) DEFAULT 'USDT',
                    memo VARCHAR(500),
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    confirmed_at TIMESTAMP
                )
                """
            )
        )
        print("  [OK] transactions exists")
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS notify_checked_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_tg_id BIGINT NOT NULL,
                    querier_tg_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP
                )
                """
            )
        )
        print("  [OK] notify_checked_pending exists")
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS group_scan_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id BIGINT NOT NULL,
                    requester_tg_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        print("  [OK] group_scan_log exists")
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS report_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_hash VARCHAR(16) UNIQUE NOT NULL,
                    target_tg_id BIGINT NOT NULL,
                    content_hash VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        print("  [OK] report_snapshots exists")
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS reporter_stats (
                    reporter_tg_id BIGINT PRIMARY KEY,
                    total_reports INTEGER NOT NULL DEFAULT 0,
                    verified_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    last_report_at TIMESTAMP
                )
                """
            )
        )
        print("  [OK] reporter_stats exists")
        # P2: 为已有表增加 verified_count / rejected_count（若不存在）
        for col, ctype in [("verified_count", "INTEGER NOT NULL DEFAULT 0"), ("rejected_count", "INTEGER NOT NULL DEFAULT 0")]:
            try:
                await conn.execute(text(f'ALTER TABLE reporter_stats ADD COLUMN "{col}" {ctype}'))
                print(f"  [ADD] reporter_stats.{col}")
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    print(f"  [OK] reporter_stats.{col} already exists")
                else:
                    raise
    print("SQLite migration done.")


async def main():
    url = os.environ.get("DATABASE_URL", "")
    if "postgresql" in url or "postgres" in url:
        await run_pg()
    elif "sqlite" in url:
        await run_sqlite()
    else:
        print("Unknown DATABASE_URL; only PostgreSQL and SQLite are supported.", file=sys.stderr)
        sys.exit(1)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
