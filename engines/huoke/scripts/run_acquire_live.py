# -*- coding: utf-8 -*-
"""
正式获客任务（非 dry_run）— 第二轮，growth phase，实际加好友
- 设备：8DWOF6CYY5R8YHX8
- 搜索关键词：5 个新的日本女性名字
- 最多加好友：2 人（保守测试）

压测：默认开启 MOBILE_AUTO_FB_COMPLIANCE_RELAXED（跳过 FB 搜索/加好友等
ComplianceGuard 限额检查，仍写入 action_log）。生产跑法请先设：
  set MOBILE_AUTO_FB_COMPLIANCE_RELAXED=0
"""
import os
import sys, io, logging, json

# 须在 import src.* 之前设置，ComplianceGuard 才会按测试模式跳过 FB 限额
os.environ.setdefault("MOBILE_AUTO_FB_COMPLIANCE_RELAXED", "1")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/acquire_live.log", encoding='utf-8', mode='a'),
    ]
)
log = logging.getLogger(__name__)

from src.app_automation.fb_acquire_task import facebook_acquire_from_keyword

DEVICE = "8DWOF6CYY5R8YHX8"
KEYWORDS = [
    "Yuko Watanabe",
    "Junko Hayashi",
    "Sachiko Ito",
    "Haruko Nakamura",
    "Reiko Yamada",
]

log.info("=== 正式获客任务第二轮（growth phase）===")
log.info("设备: %s  关键词: %s", DEVICE, KEYWORDS)
log.info("配置: max_searches=5  max_adds=2  dry_run=False")

result = facebook_acquire_from_keyword(
    device_id=DEVICE,
    persona_key="jp_female_midlife",
    max_searches=5,
    max_adds=2,
    dry_run=False,
    keywords=KEYWORDS,
)

print()
print("=" * 50)
print("=== 获客任务完成 ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
print()
print(f"搜索次数:    {result.get('searches', 0)}")
print(f"导航成功:    {result.get('nav_ok', 0)}")
print(f"L1 通过:    {result.get('l1_pass', 0)}")
print(f"L2 命中:    {result.get('l2_match', 0)}")
print(f"claim 成功: {result.get('claimed', 0)}")
print(f"加好友成功:  {result.get('add_friend_ok', 0)}")
print(f"错误次数:    {result.get('errors', 0)}")
if result.get('abort_reason'):
    print(f"中止原因:   {result.get('abort_reason')}")
