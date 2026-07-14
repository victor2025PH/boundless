# -*- coding: utf-8 -*-
"""
W2 测试：fb_greet_task dry_run 验证
注入 1 条 friended 测试数据，测试打招呼生成 + 禁词检查 + 审计记录。
"""
import sys, io, logging, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/w2_greet_dryrun.log", encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

from src.host.database import get_conn
from src.host.fb_targets_store import try_claim_target, mark_status
from datetime import datetime, timedelta

DEVICE = "8DWOF6CYY5R8YHX8"

# 1. 注入测试目标（模拟已加好友 48h 前）
log.info("注入测试数据...")
ok, tid = try_claim_target(
    identity_raw="test_greet_w2_001",
    device_id=DEVICE,
    persona_key="jp_female_midlife",
    source_mode="keyword",
    source_ref="Mieko Ishikawa",
    display_name="Mieko Ishikawa",
)
log.info("claim: ok=%s tid=%s", ok, tid)

if tid > 0:
    # 标记为 friended（48 小时前）
    friended_time = (datetime.now() - timedelta(hours=73)).strftime("%Y-%m-%d %H:%M:%S")
    mark_status(
        target_id=tid,
        status="friended",
        device_id=DEVICE,
        extra_fields={
            "friended_at": friended_time,
            "insights_json": json.dumps({
                "gender": "female",
                "age_band": "40s",
                "is_japanese": True,
                "overall_confidence": 0.90,
                "topics": ["料理", "旅行", "手芸"],
            }, ensure_ascii=False),
            "qualified": 1,
        },
    )
    log.info("已标记为 friended  friended_at=%s", friended_time)

# 2. 运行打招呼任务（dry_run）
log.info("\n=== W2 dry_run 测试开始 ===")
from src.app_automation.fb_greet_task import facebook_jp_female_greet

result = facebook_jp_female_greet(
    device_id=DEVICE,
    persona_key="jp_female_midlife",
    max_greets=3,
    dry_run=True,
)

print()
print("=== W2 dry_run 结果 ===")
print(json.dumps(result, ensure_ascii=False, indent=2))

# 3. 检查审计表
with get_conn() as c:
    rows = c.execute(
        "SELECT target_identity, generated_text, sent_ok, sent_at FROM fb_outbound_messages ORDER BY id DESC LIMIT 5"
    ).fetchall()
print()
print(f"[fb_outbound_messages 最近 {len(rows)} 条]")
for r in rows:
    print(f"  target={r[0]!r}  ok={r[2]}  text={r[1][:50] if r[1] else ''}  at={r[3]}")
