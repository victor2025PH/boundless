# -*- coding: utf-8 -*-
"""assistant 帮助语料生成（纯函数）：help_terms + nav_schema → HelpKB 条目。

红线：语料只从**代码里已存在的事实**生成（词条/导航说明），不手写
「我以为有」的功能描述——助手编造功能是一票否决项，源头先干净。
id 稳定（term:<key> / page:<id>），重跑 = 幂等 upsert 增量更新。
"""
from __future__ import annotations


def build_term_entries() -> list[dict]:
    """help_terms 158 词条 → 帮助条目。"""
    try:
        from src.web.help_terms import HELP_TERMS
    except Exception:
        return []
    out: list[dict] = []
    for key, term in HELP_TERMS.items():
        if not isinstance(term, dict):
            continue
        zh = str(term.get("zh") or "").strip()
        if not zh:
            continue
        en = str(term.get("en") or "").strip()
        desc = str(term.get("desc") or "").strip()
        desc_en = str(term.get("desc_en") or "").strip()
        usage = str(term.get("usage") or "").strip()
        usage_en = str(term.get("usage_en") or "").strip()
        content = desc + (f"\n操作：{usage}" if usage else "")
        content_en = desc_en + (f"\nHow: {usage_en}" if usage_en else "")
        out.append(
            {
                "id": f"term:{key}",
                "title": zh,
                "title_en": en,
                "content": content,
                "content_en": content_en,
                "keywords": " ".join(x for x in (zh, en, key) if x),
                "source": "seed:help_terms",
                "path": "",
            }
        )
    return out


def build_page_entries() -> list[dict]:
    """nav_schema 页面项 → 「XX 页是做什么的 + 入口路径」条目。
    help 词条已有更细说明的页面仍各成一条（页面条目带 path 可跳转）。"""
    try:
        from src.web.nav_schema import NAV_ITEMS
    except Exception:
        return []
    try:
        from src.web.help_terms import HELP_TERMS
    except Exception:
        HELP_TERMS = {}
    out: list[dict] = []
    for item_id, item in NAV_ITEMS.items():
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        label = str(item.get("label_zh") or item_id).strip()
        if not path or not label:
            continue
        term = HELP_TERMS.get(str(item.get("help") or "")) or {}
        desc = str(term.get("desc") or "").strip()
        desc_en = str(term.get("desc_en") or "").strip()
        usage = str(term.get("usage") or "").strip()
        usage_en = str(term.get("usage_en") or "").strip()
        label_en = str(term.get("en") or "").strip()
        content = f"「{label}」页入口：{path}"
        if desc:
            content += f"\n{desc}"
        if usage:
            content += f"\n操作：{usage}"
        content_en = f'"{label_en or label}" page path: {path}'
        if desc_en:
            content_en += f"\n{desc_en}"
        if usage_en:
            content_en += f"\nHow: {usage_en}"
        cmd_keys = str(item.get("cmd_keys") or "")
        out.append(
            {
                "id": f"page:{item_id}",
                "title": f"{label}（页面入口）",
                "title_en": f"{label_en or label} (page)",
                "content": content,
                "content_en": content_en,
                "keywords": " ".join(x for x in (label, label_en, cmd_keys) if x),
                "source": "seed:nav_schema",
                "path": path,
            }
        )
    return out


def build_all_entries() -> list[dict]:
    # 第三源 how-to 任务条目（2026-08-20 评审车道补齐：金标评测实锤
    # 「怎么做 X」类命中率不足，见 howto_pack 模块 docstring）
    from src.assistant.howto_pack import build_howto_entries

    return build_term_entries() + build_page_entries() + build_howto_entries()


def seed_help_corpus_bg() -> None:
    """帮助语料后台幂等播种（2026-08-23，1.0.51「全功能开箱」）。

    背景：小智进桌面交付基线后，新装机的 assistant_help.db 是空库——球在、
    问答全是「答不上」（zhiliao 此前是运维手动跑 scripts/seed_product_help
    灌的，客户机没有这双手）。语料本就从代码生成（本模块 + howto_pack），
    随包必然与代码版本同步，故服务启动时后台 upsert 一轮：新装=首启即有货；
    升级=自动补新词条；已灌过的实例=增量无害（INSERT OR REPLACE 幂等）。

    调用点在 main.py `_maybe_seed_assistant_help`（lifecycle 启动段，
    assistant.enabled 才调）——**刻意不挂 web app 装配路径**：测试自建 app
    会反复触发、且会污染测试自己灌的语料预期（首版踩过）。
    best-effort：任何异常只打日志绝不影响启动；daemon 线程不拖停机。
    """
    import logging
    import threading

    log = logging.getLogger("ai_chat_assistant.assistant.seed")

    def _run() -> None:
        try:
            from src.assistant.help_kb import get_help_kb

            kb = get_help_kb()
            n = kb.upsert_entries(build_all_entries())
            # Q-10 #254：点明落点——小智独立库 assistant_help.db，不入用户知识库
            log.info("帮助语料自动播种完成：upsert %s 条，库内 %s 条（小智独立库 %s，不入用户 KB）",
                     n, kb.count(), getattr(kb, "_path", "assistant_help.db"))
        except Exception:
            log.warning("帮助语料自动播种失败（忽略）", exc_info=True)

    threading.Thread(target=_run, name="assistant-help-seed", daemon=True).start()


__all__ = ["build_term_entries", "build_page_entries", "build_all_entries",
           "seed_help_corpus_bg"]
