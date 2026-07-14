"""P2.2 Sprint G-0 (2026-04-30): Facebook 个人主页 enrichment helper.

==============================================================================
架构定位
==============================================================================
``extract_group_members`` 提取出来的 member 字典只有 ``{"name": ...}``, 没有
bio / recent_posts / work / lives_in 等上下文。下游 P2.1 ``personalized_message.
generate_message`` 接受的 ``TargetUser`` 虽然有 bio / recent_posts 字段, 但因
为上游不填, AI 话术只能写 "我们都在群里" 之类的空话。

本模块独立于 ``facebook.py``, 提供一个组合 helper:

    enrich_member_profile(fb_automation, member, device_id) -> enriched_member

内部复用 ``facebook.view_profile`` (search → tap profile → enter) +
``facebook.read_profile_about`` (dump About 字段) + 自家 ``_extract_recent_posts``
(滚动主时间线, 提取 2-3 条 post 文本).

调用方负责:
  * 决定 enrich 哪些 members (推荐: 按 score 排序后 top-N, 避免对 30 人都跑)
  * 控制并发 (本 helper 是同步的, 一次只 enrich 一个 member)
  * 写库 (本 helper 只填字段到 dict, 不直接写 platform_profiles)

设计原则:
  * 零侵入: 不改 ``extract_group_members``, 调用方主动调
  * 容错: 每步都有 fallback, 异常不抛出, 失败时返回原 member (不破坏调用链)
  * 可单测: ``fb_automation`` 是协议式入参 (duck-typing), 单测可注入 mock
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 配置 ───────────────────────────────────────────────────────────
ENRICH_PROFILE_READ_SECONDS = 3.0   # view_profile 内部读秒数 (越小越快)
ENRICH_POST_SCROLL_TIMES = 3         # 滚动几次取 posts
ENRICH_POSTS_MAX = 3                 # 最多收集多少条 posts
ENRICH_POST_MIN_LEN = 8              # 单条 post 最小字符数 (过滤"Like/赞"等按钮)
ENRICH_POST_MAX_LEN = 280            # 单条 post 截断
ENRICH_RETURN_BACK = True            # enrich 后是否 press back 返回原页面
ENRICH_BACK_PRESS_COUNT = 2          # back 几次 (profile → search → 群)


# 屏幕上"非 post 内容"的过滤词 — 这些是 FB UI 装饰 / 按钮 / 卡片标签
# 出现在 timeline dump 里时容易被误判为 post 文本
POST_NOISE_PATTERNS = (
    "Like", "Comment", "Share", "Reply", "See more", "See translation",
    "赞", "评论", "分享", "回复", "查看更多", "查看翻译",
    "いいね", "コメント", "シェア", "返信", "もっと見る", "翻訳を見る",
    "Add Friend", "Message", "Follow", "Following",
    "加为好友", "发消息", "关注", "已关注",
    "Posts", "Photos", "Videos", "About", "Reels",
    "帖子", "照片", "视频", "简介",
    "投稿", "写真", "動画", "概要",
    "Active", "Online",  # 在线状态
    "h ago", "m ago", "d ago", "天", "小时", "分钟",  # 时间戳
)


def _is_likely_post_text(text: str) -> bool:
    """判断一条 dump 出来的文本是否像 user post 而不是 UI 装饰。

    Rules:
      - 长度 ≥ ENRICH_POST_MIN_LEN
      - 不在 POST_NOISE_PATTERNS 黑名单
      - 不全是单字符重复 (LikeLikeLike...)
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < ENRICH_POST_MIN_LEN or len(t) > ENRICH_POST_MAX_LEN * 2:
        return False
    # 黑名单匹配 — 整个文本就是 noise (而不是 post 里包含 noise 词)
    for noise in POST_NOISE_PATTERNS:
        if t == noise or (len(t) < len(noise) + 5 and noise in t):
            return False
    # 单字符重复 (UI 渲染 bug 容易产生)
    if len(set(t)) <= 2:
        return False
    return True


def _extract_recent_posts(d, max_posts: int = ENRICH_POSTS_MAX) -> List[str]:
    """从当前 profile timeline 滚动 + dump 抽取最近 posts.

    Strategy:
      * 每滚一次 dump_hierarchy 一次, 用 XMLParser 拿所有 TextView 的 text
      * 按 _is_likely_post_text 过滤
      * 去重后取前 max_posts 条 (timeline 顺序 = 时间倒序)
    """
    posts: List[str] = []
    seen = set()
    try:
        for _i in range(ENRICH_POST_SCROLL_TIMES):
            try:
                xml = d.dump_hierarchy() or ""
            except Exception:
                xml = ""
            try:
                from src.vision.screen_parser import XMLParser
                elements = XMLParser.parse(xml) if xml else []
            except Exception:
                elements = []

            for el in elements:
                text = (getattr(el, "text", "") or "").strip()
                if not text or text in seen:
                    continue
                if not _is_likely_post_text(text):
                    continue
                seen.add(text)
                # 截断超长 (FB feed 完整文本可能 1000+ 字)
                posts.append(text[:ENRICH_POST_MAX_LEN])
                if len(posts) >= max_posts:
                    return posts

            try:
                # 滚动到下一屏继续找
                d.swipe_ext("up", scale=0.6) if hasattr(d, "swipe_ext") \
                    else d.press("page_down")
            except Exception:
                pass
            time.sleep(1.0)
    except Exception as e:
        logger.debug("[fb_profile_enrichment] _extract_recent_posts 异常: %s", e)
    return posts


def enrich_member_profile(fb_automation: Any,
                          member: Dict[str, Any],
                          device_id: str,
                          *,
                          extract_posts: bool = True,
                          press_back_on_done: bool = ENRICH_RETURN_BACK,
                          ) -> Dict[str, Any]:
    """对一个 member 进 profile 页抓 bio + recent_posts, 返回 enriched dict.

    Args:
        fb_automation: FacebookAutomation 实例 (需要有 view_profile,
            read_profile_about, _u2 方法)
        member: 至少含 ``{"name": ...}`` 的字典
        device_id: 真机 ID
        extract_posts: 是否拉 recent_posts (False 时只拿 About 字段, 更快)
        press_back_on_done: 完成后是否 press back 返回原页 (extract_group_members
            场景需要 True 才能继续滚成员列表; 独立调用场景可设 False)

    Returns:
        member 字典 (浅拷贝并补充字段):
            * ``bio``: str       从 About.raw_about 提取
            * ``recent_posts``: List[str]
            * ``work``: str      About.work
            * ``lives_in``: str  About.lives_in
            * ``enriched``: bool 是否成功 enrich (失败时 = False, 字段为空)
            * ``enrich_error``: str  失败原因 (成功时 = "")
    """
    enriched = dict(member)  # 浅拷贝, 不污染调用方
    enriched.setdefault("bio", "")
    enriched.setdefault("recent_posts", [])
    enriched.setdefault("work", "")
    enriched.setdefault("lives_in", "")
    enriched["enriched"] = False
    enriched["enrich_error"] = ""

    name = (member.get("name") or "").strip()
    if not name:
        enriched["enrich_error"] = "no_name"
        return enriched
    if not device_id:
        enriched["enrich_error"] = "no_device_id"
        return enriched

    try:
        # Step 1: search → tap → 进 profile (复用 facebook.view_profile)
        if not fb_automation.view_profile(name,
                                           read_seconds=ENRICH_PROFILE_READ_SECONDS,
                                           device_id=device_id):
            enriched["enrich_error"] = "view_profile_failed"
            return enriched

        # Step 2: 拿 About 信息 (work / lives_in / raw_about)
        about: Dict[str, Any] = {}
        try:
            about = fb_automation.read_profile_about(device_id=device_id) or {}
        except Exception as e:
            logger.debug("[fb_profile_enrichment] read_profile_about 异常: %s", e)
        # raw_about 截断为 bio (≤ 200 字, 给 LLM prompt 用)
        raw_bio = (about.get("raw_about") or "").strip()
        enriched["bio"] = raw_bio[:200]
        enriched["work"] = (about.get("work") or "").strip()
        enriched["lives_in"] = (about.get("lives_in") or "").strip()

        # Step 3: 抽 recent_posts
        if extract_posts:
            try:
                d = fb_automation._u2(device_id)
                # 切回 Posts tab (read_profile_about 切到了 About)
                try:
                    fb_automation.smart_tap("Posts tab on profile",
                                             device_id=device_id)
                    time.sleep(1.0)
                except Exception:
                    pass
                enriched["recent_posts"] = _extract_recent_posts(d)
            except Exception as e:
                logger.debug("[fb_profile_enrichment] posts 抽取异常: %s", e)

        enriched["enriched"] = True
    except Exception as e:
        logger.warning("[fb_profile_enrichment] enrich 失败 name=%r: %s", name, e)
        enriched["enrich_error"] = f"exception:{type(e).__name__}"
    finally:
        # Step 4: 退回原页面 (调用方循环 enrich 多个 member 时必须)
        if press_back_on_done and device_id:
            try:
                d = fb_automation._u2(device_id)
                for _ in range(ENRICH_BACK_PRESS_COUNT):
                    try:
                        d.press("back")
                        time.sleep(0.6)
                    except Exception:
                        break
            except Exception:
                pass

    return enriched


def enrich_top_members(fb_automation: Any,
                       members: List[Dict[str, Any]],
                       device_id: str,
                       top_n: int = 5,
                       sort_key: Optional[Callable[[Dict[str, Any]], float]] = None,
                       ) -> List[Dict[str, Any]]:
    """对 members 列表里 top-N 高分目标做 enrich, 返回更新后的整列表.

    members 中没排进 top-N 的项不被 enrich, 但仍保留在结果里 (字段为空)。

    Args:
        sort_key: members 排序键, 默认按 ``m.get("score", 0)`` 倒序 (高分优先)。
            可改为按 tier 或自定义。
    """
    if not members:
        return members
    if top_n <= 0:
        return members

    # 排序
    key_fn = sort_key or (lambda m: float(m.get("score") or 0))
    indexed = list(enumerate(members))
    indexed.sort(key=lambda iv: key_fn(iv[1]), reverse=True)

    # 取 top-N 的 index
    top_indexes = {i for i, _ in indexed[:top_n]}

    out: List[Dict[str, Any]] = []
    for idx, m in enumerate(members):
        if idx in top_indexes:
            try:
                em = enrich_member_profile(fb_automation, m, device_id)
                out.append(em)
            except Exception as e:
                logger.warning("[enrich_top_members] enrich 失败 idx=%d: %s",
                                idx, e)
                out.append(m)
        else:
            out.append(m)
    return out
