# -*- coding: utf-8 -*-
"""
W1 测试：fb_acquire_task dry_run 验证
不执行加好友，只测试 搜索→导航→分类→claim 全链路。
用设备 8DWOF6CYY5R8YHX8，搜索 3 个关键词。
"""
import sys, io, logging, json, os, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/w1_acquire_dryrun.log", encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

from src.app_automation.fb_acquire_task import facebook_acquire_from_keyword

DEVICE = "8DWOF6CYY5R8YHX8"
KEYWORDS = ["Yumi Tanaka", "Keiko Suzuki", "Mieko Ishikawa"]

log.info("=== W1 dry_run 测试开始 ===")
log.info("设备: %s  关键词: %s", DEVICE, KEYWORDS)

result = facebook_acquire_from_keyword(
    device_id=DEVICE,
    persona_key="jp_female_midlife",
    max_searches=3,
    max_adds=0,       # 不加好友
    dry_run=True,
    keywords=KEYWORDS,
)

log.info("=== 结果 ===")
log.info(json.dumps(result, ensure_ascii=False, indent=2))
print()
print("=== W1 dry_run 完成 ===")
print(f"搜索: {result.get('searches')}/3")
print(f"导航成功: {result.get('nav_ok')}")
print(f"L1通过: {result.get('l1_pass')}")
print(f"L2命中: {result.get('l2_match')}")
print(f"claim成功: {result.get('claimed')}")
print(f"错误: {result.get('errors')}")
