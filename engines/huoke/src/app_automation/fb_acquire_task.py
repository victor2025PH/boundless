# -*- coding: utf-8 -*-
"""
fb_acquire_task.py — Facebook 日本女性精准获客任务（W1-ACQ）

主流程：
  1. 从关键词列表循环搜索日文女名
  2. 进入 profile → 截图 → L1+L2 VLM 分类
  3. L2 命中 → try_claim_target 跨设备互斥
  4. 加好友（日文验证语）→ 写入 fb_targets_global
  5. 合规：搜索/加友 频率限制，account_health 熔断

依赖：
  - src/app_automation/facebook.py (FacebookAutomation)
  - src/host/fb_targets_store.py (try_claim_target, mark_status)
  - src/host/fb_profile_classifier.py (classify)
  - config/fb_target_personas.yaml
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 默认日本女性名字关键词库 ─────────────────────────────────────────
JP_FEMALE_NAMES_ROMAJI: List[str] = [
    "Yumi Tanaka", "Keiko Suzuki", "Hanako Yamada", "Noriko Sato",
    "Michiko Nakamura", "Yoko Ito", "Kazuko Kobayashi", "Fumiko Kato",
    "Hiroko Yoshida", "Reiko Yamamoto", "Sachiko Watanabe", "Tomoko Abe",
    "Kimiko Ikeda", "Ayako Hayashi", "Yoshiko Shimizu", "Masako Yamashita",
    "Chieko Matsumoto", "Hisako Ogawa", "Nobuko Inoue", "Teruko Kimura",
    "Ryoko Fujii", "Chizuko Hayashi", "Setsuko Taniguchi", "Naoko Ueda",
    "Mieko Ishikawa", "Kyoko Nishimura", "Sumiko Goto", "Haruko Mori",
    "Etsuko Saito", "Mineko Yamamoto", "Akiko Tanaka", "Yukiko Sato",
    "Mariko Watanabe", "Junko Kimura", "Miyuki Takahashi", "Aiko Yamada",
    "Noriko Hayashi", "Sachiko Ito", "Emiko Nakamura", "Mikiko Yamamoto",
    "Chiyo Kobayashi", "Yumiko Suzuki", "Hiromi Ogawa", "Kanako Saito",
    "Setsuko Ueda", "Tomoe Nishimura", "Ruriko Mori", "Natsuko Fujita",
    "Haruko Ikeda", "Kyoko Shimizu",
]

JP_FEMALE_NAMES_JA: List[str] = [
    "田中美咲", "鈴木花子", "山田由美", "佐藤智子", "伊藤恵子",
    "中村美由紀", "小林幸子", "加藤明美", "吉田香里", "松本裕子",
    "井上真由美", "木村尚子", "林みちこ", "清水美香", "山口恵美",
]


# ── 合规约束 ──────────────────────────────────────────────────────────
_SEARCH_MIN_INTERVAL_S = 12    # 搜索间最小间隔（秒）
_SEARCH_MAX_INTERVAL_S = 25    # 搜索间最大间隔（秒）
_ADD_FRIEND_MIN_INTERVAL_S = 45  # 加好友间最小间隔
_ADD_FRIEND_MAX_INTERVAL_S = 90  # 加好友间最大间隔


class AcquireTask:
    """
    Facebook 关键词获客任务。

    用法:
        task = AcquireTask(
            device_id="8DWOF6CYY5R8YHX8",
            persona_key="jp_female_midlife",
            max_searches=20,
            max_adds=5,
        )
        result = task.run()
    """

    def __init__(
        self,
        device_id: str,
        persona_key: str = "jp_female_midlife",
        max_searches: int = 20,
        max_adds: int = 5,
        dry_run: bool = False,
        keyword_list: Optional[List[str]] = None,
    ):
        self.device_id = device_id
        self.persona_key = persona_key
        self.max_searches = max_searches
        self.max_adds = max_adds
        self.dry_run = dry_run
        self.keyword_list = keyword_list or (JP_FEMALE_NAMES_ROMAJI + JP_FEMALE_NAMES_JA)

        self._fb: Any = None  # FacebookAutomation 延迟初始化

    def _get_fb(self):
        if self._fb is None:
            from src.app_automation.facebook import FacebookAutomation
            self._fb = FacebookAutomation()
        return self._fb

    # ── account_health 检查 ───────────────────────────────────────────

    def _check_health(self) -> bool:
        """检查账号健康状态，返回是否允许操作"""
        try:
            from src.host.fb_targets_store import get_account_health
            h = get_account_health(self.device_id)
            if h.get("phase") == "frozen":
                frozen_until = h.get("frozen_until", "")
                logger.warning("[acquire] 账号冻结 until=%s device=%s", frozen_until, self.device_id)
                return False
            score = int(h.get("score", 100) or 100)
            if score < 30:
                logger.warning("[acquire] 账号健康分过低 score=%d device=%s", score, self.device_id)
                return False
        except Exception as e:
            logger.debug("[acquire] health check 失败（继续）: %s", e)
        return True

    # ── 核心流程 ─────────────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """执行获客任务，返回统计结果"""
        stats = {
            "device_id": self.device_id,
            "persona_key": self.persona_key,
            "searches": 0,
            "nav_ok": 0,
            "l1_pass": 0,
            "l2_match": 0,
            "claimed": 0,
            "add_friend_ok": 0,
            "skipped_cache": 0,
            "errors": 0,
            "started_at": datetime.now().isoformat(),
        }

        if not self._check_health():
            stats["abort_reason"] = "account_frozen_or_low_health"
            return stats

        # 打乱关键词，避免固定顺序被检测
        keywords = list(self.keyword_list)
        random.shuffle(keywords)

        add_count = 0

        for kw in keywords:
            if stats["searches"] >= self.max_searches:
                break
            # dry_run 时不受加好友上限约束（实际不会真加）
            if not self.dry_run and add_count >= self.max_adds:
                logger.info("[acquire] 已达加好友上限 %d", self.max_adds)
                break

            stats["searches"] += 1
            logger.info("\n=== [%d/%d] 搜索: %r ===",
                        stats["searches"], self.max_searches, kw)

            # 随机间隔（人类化）
            time.sleep(random.uniform(_SEARCH_MIN_INTERVAL_S, _SEARCH_MAX_INTERVAL_S))

            try:
                result = self._process_one_keyword(kw, add_count)
            except Exception as e:
                logger.error("[acquire] 处理 %r 失败: %s", kw, e)
                stats["errors"] += 1
                continue

            if result.get("nav_ok"):
                stats["nav_ok"] += 1
            if result.get("l1_pass"):
                stats["l1_pass"] += 1
            if result.get("l2_match"):
                stats["l2_match"] += 1
            if result.get("claimed"):
                stats["claimed"] += 1
            if result.get("add_ok"):
                stats["add_friend_ok"] += 1
                add_count += 1
                # 加好友后等待更长时间
                time.sleep(random.uniform(_ADD_FRIEND_MIN_INTERVAL_S, _ADD_FRIEND_MAX_INTERVAL_S))
            if result.get("from_cache"):
                stats["skipped_cache"] += 1

        stats["ended_at"] = datetime.now().isoformat()
        logger.info("[acquire] 完成: %s", stats)
        return stats

    def _process_one_keyword(self, keyword: str, current_add_count: int) -> Dict[str, Any]:
        """处理单个关键词的完整流程"""
        result = {
            "keyword": keyword,
            "nav_ok": False,
            "l1_pass": False,
            "l2_match": False,
            "claimed": False,
            "add_ok": False,
            "from_cache": False,
        }

        fb = self._get_fb()

        # 1. 直接以关键词导航到 profile（内部搜索 + 点第一条）
        try:
            nav_info = fb.navigate_to_profile(keyword, device_id=self.device_id)
        except Exception as e:
            logger.warning("[acquire] navigate_to_profile 失败: %s", e)
            return result

        if not nav_info.get("ok"):
            logger.info("[acquire] navigate_to_profile ok=False: %s reason=%s",
                        keyword, nav_info.get("reason", ""))
            return result

        result["nav_ok"] = True
        display_name = nav_info.get("display_name") or keyword
        target_key = nav_info.get("target_key") or f"search:{keyword}"
        logger.info("[acquire] 导航成功: %r  target_key=%s", display_name, target_key)

        # 2. 从当前 profile 页提取文本（bio、姓名）—— 用于 L1 分类
        try:
            d = fb._u2(self.device_id)
            name_from_page, bio = fb._extract_profile_text(d)
            # 如果页面提取到更好的名字就用它
            if name_from_page and len(name_from_page) > 2:
                display_name = name_from_page
        except Exception as e:
            logger.debug("[acquire] _extract_profile_text 失败: %s", e)
            bio = ""

        # 3. 截图（L2 用）
        try:
            snap_result = fb.capture_profile_snapshots(
                device_id=self.device_id,
                shot_count=3,
                scroll_between=True,
            )
            # capture_profile_snapshots 返回 dict {"image_paths": [...], ...}
            if isinstance(snap_result, dict):
                image_paths = snap_result.get("image_paths") or []
            else:
                image_paths = list(snap_result) if snap_result else []
        except Exception as e:
            logger.warning("[acquire] capture_profile_snapshots 失败: %s", e)
            image_paths = []

        # 4. L1+L2 分类
        from src.host.fb_profile_classifier import classify
        clf = classify(
            device_id=self.device_id,
            task_id=f"acquire_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            persona_key=self.persona_key,
            target_key=target_key,
            display_name=display_name,
            bio=bio,
            username="",
            locale="ja",
            image_paths=image_paths,
            l2_image_paths=image_paths,
            do_l2=True,
            dry_run=self.dry_run,
        )

        result["from_cache"] = clf.get("from_cache", False)
        l1 = clf.get("l1") or {}
        result["l1_pass"] = bool(l1.get("pass", False)) or bool(clf.get("from_cache"))
        result["l2_match"] = bool(clf.get("match"))

        if not result["l2_match"]:
            logger.info("[acquire] L2 未命中: %r  score=%.1f", display_name, clf.get("score", 0))
            return result

        logger.info("[acquire] L2 命中: %r  score=%.1f  conf=%.2f",
                    display_name, clf.get("score", 0),
                    (clf.get("insights") or {}).get("overall_confidence", 0))

        # 5. try_claim_target（跨设备互斥）
        from src.host.fb_targets_store import try_claim_target, mark_status
        claimed, target_id = try_claim_target(
            identity_raw=target_key,
            device_id=self.device_id,
            persona_key=self.persona_key,
            source_mode="keyword",
            source_ref=keyword,
            display_name=display_name,
        )

        if not claimed:
            logger.info("[acquire] claim 失败（已被其他设备处理）: %r", display_name)
            return result

        result["claimed"] = True

        # 6. 更新 insights
        if not self.dry_run and target_id > 0:
            try:
                mark_status(
                    target_id=target_id,
                    status="qualified",
                    device_id=self.device_id,
                    extra_fields={
                        "qualified": 1,
                        "insights_json": json.dumps(clf.get("insights") or {}, ensure_ascii=False),
                        "snapshots_dir": image_paths[0].rsplit("/", 1)[0] if image_paths else "",
                    },
                )
            except Exception as e:
                logger.warning("[acquire] mark_status qualified 失败: %s", e)

        # 7. 加好友（dry_run 不执行）
        if self.dry_run:
            logger.info("[acquire] dry_run: 跳过加好友 %r", display_name)
            result["add_ok"] = True  # dry_run 视为成功
            return result

        if current_add_count >= self.max_adds:
            logger.info("[acquire] 已达加好友上限，跳过: %r", display_name)
            return result

        try:
            add_ok = fb.add_friend_with_note(
                profile_name=display_name,
                device_id=self.device_id,
                safe_mode=True,
            )
        except Exception as e:
            logger.warning("[acquire] add_friend_with_note 失败: %s", e)
            add_ok = False

        result["add_ok"] = bool(add_ok)

        if add_ok and target_id > 0:
            try:
                mark_status(
                    target_id=target_id,
                    status="friend_requested",
                    device_id=self.device_id,
                )
            except Exception as e:
                logger.warning("[acquire] mark_status friend_requested 失败: %s", e)

        return result


# ── 模块级快捷函数（供 task_dispatcher 调用）─────────────────────────

def facebook_acquire_from_keyword(
    device_id: str,
    persona_key: str = "jp_female_midlife",
    max_searches: int = 20,
    max_adds: int = 5,
    dry_run: bool = False,
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    任务入口函数：Facebook 关键词获客。

    参数:
        device_id    - ADB 设备序列号
        persona_key  - 目标画像键（默认 jp_female_midlife）
        max_searches - 本次最多搜索次数
        max_adds     - 本次最多加好友次数
        dry_run      - True 时只分类不加好友
        keywords     - 自定义关键词列表（None 使用内置列表）

    返回:
        统计字典：{searches, nav_ok, l1_pass, l2_match, claimed, add_friend_ok, errors}
    """
    task = AcquireTask(
        device_id=device_id,
        persona_key=persona_key,
        max_searches=max_searches,
        max_adds=max_adds,
        dry_run=dry_run,
        keyword_list=keywords,
    )
    return task.run()
